"""`ligature status` / `ligature check` over real workspaces on disk
(chainlink #56).

These tests build a genuine workspace in a temp directory -- descriptor,
artifacts, promotion receipt, degradation record, assurance report -- and
run the real CLI through `pipeline.main()`, then validate the JSON output
against the v1.0 contracts `schemas/project-state.schema.json` and
`schemas/consolidated-check.schema.json`. Nothing is pre-loaded from a
hand-authored document: every state (draft/validated/promoted/stale/
invalid/degraded) is reached by writing real files and, where the
transition matters, by the same hashing rule the promotion generator
uses.
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import ligature_install  # noqa: E402
import pipeline  # noqa: E402
import project_state  # noqa: E402
from project_state import Analysis, NormalizedFinding, _next_action  # noqa: E402
from schema_utils import make_validator  # noqa: E402

EXAMPLES = ROOT / "schemas" / "examples"
PROJECT_STATE_SCHEMA = json.loads((ROOT / "schemas" / "project-state.schema.json").read_text())
CONSOLIDATED_CHECK_SCHEMA = json.loads(
    (ROOT / "schemas" / "consolidated-check.schema.json").read_text()
)


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


BOUNDARY = {
    "schema_version": "1.0",
    "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
    "caller": {"concept": "Scheduler", "method": "dispatch"},
    "callee": {"concept": "TaskQueue", "method": "pop_ready"},
    "callee_guarantees": ["TaskQueue.C003"],
    "review": {"reviewer": "example-reviewer", "reviewed_at": "2026-08-25"},
}


class WorkspaceFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name).resolve()
        self.descriptor_path = self.workspace / "project-descriptor.json"

    def tearDown(self):
        self._tmp.cleanup()

    def write_descriptor(self, crates=("crates/a",)):
        descriptor = json.loads((EXAMPLES / "project-descriptor.greenfield.example.json").read_text())
        descriptor["crates"] = [
            {"crate_dir": crate, "contracts_crate": "contracts", "specs_search_root": "crates"}
            for crate in crates
        ]
        # A real project has replaced the init template's placeholder
        # reviewer. `verifier_policy.default` is deliberately left at the
        # example's "creusot" so these fixtures double as the coincidence
        # case: a real project that happens to pick the same verifier must
        # not be flagged on that field alone (chainlink #67).
        descriptor["review"]["reviewer"] = "real-reviewer"
        self.descriptor_path.write_text(json.dumps(descriptor))
        return descriptor

    def write(self, relative: str, data) -> Path:
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            path.write_text(data)
        else:
            path.write_text(json.dumps(data, indent=2) + "\n")
        return path

    def boundary_path(self, name="scheduler_dispatch__to__task_queue_pop_ready") -> Path:
        return self.workspace / "crates" / "a" / "specs" / "_boundaries" / f"{name}.json"

    def run_cli(self, *args):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = pipeline.main(
                ["--workspace", str(self.workspace), "--descriptor", str(self.descriptor_path), *args]
            )
        return code, buffer.getvalue()

    def status(self, *args) -> tuple[int, dict]:
        code, out = self.run_cli("status", "--json", *args)
        return code, json.loads(out)

    def check(self, *args) -> tuple[int, dict]:
        code, out = self.run_cli("check", "--json", *args)
        return code, json.loads(out)

    def assertValid(self, schema, document, validator_name="document"):
        validator = make_validator(schema)
        errors = list(validator.iter_errors(document))
        self.assertEqual(errors, [], f"{validator_name} invalid: {[e.message for e in errors]}")

    def snapshot(self) -> dict[str, str]:
        return {
            p.relative_to(self.workspace).as_posix(): _sha256(p)
            for p in self.workspace.rglob("*")
            if p.is_file()
        }


class ProjectStateDocumentTest(WorkspaceFixture):
    def test_empty_workspace_reports_absent_descriptor_and_validates(self):
        code, doc = self.status()
        self.assertEqual(code, 0)
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")
        self.assertEqual(doc["descriptor"]["state"], "absent")
        self.assertEqual(doc["artifacts"], [])
        self.assertEqual(doc["installation_manifest"]["state"], "not-initialized")

    def test_artifact_lifecycle_draft_validated_invalid_from_real_files(self):
        self.write_descriptor()
        self.write("crates/a/specs/_boundaries/scheduler_dispatch__to__task_queue_pop_ready.json", BOUNDARY)
        draft = dict(BOUNDARY)
        draft.pop("review")
        draft["boundary_id"] = "scheduler_clone__to__task_queue_new"
        self.write("crates/a/specs/_boundaries/scheduler_clone__to__task_queue_new.json", draft)
        self.write("crates/a/specs/_boundaries/broken.json", "{not json")
        _, doc = self.status()
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")
        by_path = {a["path"]: a for a in doc["artifacts"]}
        self.assertEqual(
            by_path["crates/a/specs/_boundaries/scheduler_dispatch__to__task_queue_pop_ready.json"]["lifecycle"],
            "validated",
        )
        self.assertEqual(
            by_path["crates/a/specs/_boundaries/scheduler_clone__to__task_queue_new.json"]["lifecycle"],
            "draft",
        )
        self.assertEqual(by_path["crates/a/specs/_boundaries/broken.json"]["lifecycle"], "invalid")

    def test_lifecycle_transition_draft_to_validated_to_promoted_to_stale(self):
        """A real state transition driven by writing files: a fresh
        boundary with no review is draft, adding review makes it
        validated, a promotion receipt makes it promoted, and editing the
        promoted bytes makes it stale-by-hash-drift -- never silently
        promoted again."""
        self.write_descriptor()
        self.write("crates/a/specs/_boundaries/x.json", {**BOUNDARY, "boundary_id": "x"})
        _, doc = self.status()
        self.assertEqual(doc["artifacts"][0]["lifecycle"], "validated")

        # Rewrite without review -> draft.
        self.write("crates/a/specs/_boundaries/x.json", {k: v for k, v in BOUNDARY.items() if k != "review"} | {"boundary_id": "x"})
        _, doc = self.status()
        self.assertEqual(doc["artifacts"][0]["lifecycle"], "draft")

        # Restore review and record a promotion with the real file hash.
        self.write("crates/a/specs/_boundaries/x.json", {**BOUNDARY, "boundary_id": "x"})
        promoted_hash = _sha256(self.workspace / "crates/a/specs/_boundaries/x.json")
        self.write(
            "specs/_promotions/scheduling.json",
            {
                "schema": "promotion-receipt/1.0",
                "promotion_id": "PROM-SCHEDULING-001",
                "cluster": "scheduling",
                "reviewer": "example-reviewer",
                "policy_version": "reliance-policy@0.1",
                "schema_versions": {"boundary": "1.0"},
                "accepted_at": "2026-08-31",
                "artifact_manifest": [
                    {"path": "crates/a/specs/_boundaries/x.json", "hash": promoted_hash}
                ],
            },
        )
        _, doc = self.status()
        artifact = doc["artifacts"][0]
        self.assertEqual(artifact["lifecycle"], "promoted")
        self.assertEqual(artifact["promoted_hash"], promoted_hash)

        # Change the promoted content -> stale-by-hash-drift, with both
        # hashes present so the drift is directly inspectable.
        changed = {**BOUNDARY, "boundary_id": "x", "callee_guarantees": ["TaskQueue.C003", "TaskQueue.C004"]}
        self.write("crates/a/specs/_boundaries/x.json", changed)
        _, doc = self.status()
        artifact = doc["artifacts"][0]
        self.assertEqual(artifact["lifecycle"], "stale-by-hash-drift")
        self.assertEqual(artifact["promoted_hash"], promoted_hash)
        self.assertNotEqual(artifact["content_hash"], promoted_hash)
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")

    def test_receipt_entry_with_missing_file_is_absent(self):
        self.write_descriptor()
        self.write(
            "specs/_promotions/scheduling.json",
            {
                "schema": "promotion-receipt/1.0",
                "promotion_id": "PROM-SCHEDULING-001",
                "cluster": "scheduling",
                "reviewer": "example-reviewer",
                "policy_version": "reliance-policy@0.1",
                "schema_versions": {"boundary": "1.0"},
                "accepted_at": "2026-08-31",
                "artifact_manifest": [
                    {"path": "crates/a/specs/_boundaries/gone.json", "hash": "sha256:" + "a" * 64}
                ],
            },
        )
        _, doc = self.status()
        absent = [a for a in doc["artifacts"] if a["lifecycle"] == "absent"]
        self.assertEqual(len(absent), 1)
        self.assertIsNone(absent[0]["content_hash"])
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")

    def test_required_and_achieved_assurance_are_separate_records(self):
        self.write_descriptor()
        self.write(
            "crates/a/specs/_interactions/i.json",
            {
                "interaction_id": "I-1",
                "reliances": [
                    {
                        "obligation_id": "TaskQueue.C003",
                        "required_assurance": {"accepted_evidence_kinds": ["creusot-deductive-check"]},
                    }
                ],
            },
        )
        self.write(
            "ci/manifest/WP-1.json",
            {"work_package": "WP-1", "report": {"emit": "ci/results/WP-1.json"}},
        )
        self.write(
            "ci/results/WP-1.json",
            {
                "obligation_records": [
                    {
                        "obligation_id": "TaskQueue.C003",
                        "record": {"evidence": {"kind": "kani-bounded-model-check"}, "support": {"status": "supported"}},
                    }
                ]
            },
        )
        _, doc = self.status()
        obligation = next(o for o in doc["obligations"] if o["obligation_id"] == "TaskQueue.C003")
        required_dims = {d["dimension"] for d in obligation["required_assurance"]}
        # The achieved evidence kind is reported separately; the required
        # dimension stays visible with a non-achieved status rather than
        # being collapsed into a boolean.
        self.assertEqual(required_dims, {"creusot-deductive-check"})
        self.assertEqual(
            [d["status"] for d in obligation["required_assurance"] if d["dimension"] == "creusot-deductive-check"],
            ["missing"],
        )
        self.assertIn("kani-bounded-model-check", {d["dimension"] for d in obligation["achieved_assurance"]})
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")

    def test_witness_observations_are_separate_from_assurance(self):
        self.write_descriptor()
        self.write(
            "crates/a/specs/_witnesses/W.json",
            {"witness_id": "W-TQ-LOAD-FACTOR", "determinism": "deterministic"},
        )
        _, doc = self.status()
        summaries = doc["generated_observations"]["witness_summaries"]
        self.assertEqual(summaries[0]["witness_id"], "W-TQ-LOAD-FACTOR")
        self.assertEqual(summaries[0]["determinism"], "deterministic")
        # No witness ever appears as an assurance dimension.
        for obligation in doc["obligations"]:
            for dimension in obligation["required_assurance"] + obligation["achieved_assurance"]:
                self.assertNotEqual(dimension["dimension"], "witness")
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")

    def test_degraded_cluster_reports_limitations_and_change_request(self):
        self.write_descriptor()
        self.write("specs/_closure/mcmc-chain.json", {"cluster": "mcmc-chain", "closure_kind": "bounded"})
        self.write(
            "specs/_closure/mcmc-chain.degradation.json",
            {"cluster": "mcmc-chain", "failed_conditions": ["single_verifier_system"]},
        )
        _, doc = self.status()
        cluster = next(c for c in doc["clusters"] if c["cluster"] == "mcmc-chain")
        self.assertEqual(cluster["closure_kind"], "bounded")
        self.assertEqual(cluster["state"], "degraded")
        self.assertEqual(cluster["limitations"], ["single_verifier_system"])
        self.assertEqual([c["target"] for c in doc["change_requests"]], ["specs/_closure/mcmc-chain.json"])
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")

    def test_installed_manifest_states(self):
        self.write_descriptor()
        self.assertNotEqual(self.status()[1]["installation_manifest"]["state"], "current")
        self.write(
            "ci/manifest/installation.json",
            {
                "installed_product_version": project_state.PRODUCT_VERSION,
                "installed_schema_versions": {"project-state": "1.0"},
            },
        )
        doc = self.status()[1]
        self.assertEqual(doc["installation_manifest"]["state"], "current")
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")

    def test_init_then_status_reports_no_invalid_work_package(self):
        """The exact reproduction of chainlink #72: after a successful
        `init --mode port`, status must agree with its own
        installation_manifest.state -- no artifact may classify
        ci/manifest/installation.json as an invalid work-package."""
        code, _ = self.run_cli("init", "--mode", "port", "--name", "myport")
        self.assertEqual(code, 0)
        self.assertTrue((self.workspace / "ci" / "manifest" / "installation.json").is_file())
        _, doc = self.status()
        self.assertEqual(doc["installation_manifest"]["state"], "current")
        by_path = {a["path"]: a for a in doc["artifacts"]}
        self.assertNotIn("ci/manifest/installation.json", by_path)
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")

    def test_installation_manifest_is_not_reported_as_a_work_package_artifact(self):
        """The ownership manifest shares ci/manifest/ with work-package
        manifests but is ownership metadata, not a work-package artifact
        (chainlink #66/#72)."""
        self.write_descriptor()
        self.write(
            "ci/manifest/installation.json",
            {
                "installed_product_version": project_state.PRODUCT_VERSION,
                "installed_schema_versions": {"project-state": "1.0"},
            },
        )
        self.write(
            "ci/manifest/WP-1.json",
            {"work_package": "WP-1", "report": {"emit": "ci/results/WP-1.json"}},
        )
        _, doc = self.status()
        self.assertEqual(doc["installation_manifest"]["state"], "current")
        by_path = {a["path"]: a for a in doc["artifacts"]}
        self.assertNotIn("ci/manifest/installation.json", by_path)
        self.assertEqual(by_path["ci/manifest/WP-1.json"]["kind"], "work-package")
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")

    def test_a_broken_work_package_manifest_is_still_reported_invalid(self):
        """The skip is shape-based, not directory-based: a file in
        ci/manifest/ that carries a work_package key but fails the
        work-package schema is still an honestly-invalid work-package
        artifact, not something to hide."""
        self.write_descriptor()
        self.write("ci/manifest/WP-1.json", {"work_package": "WP-1"})
        _, doc = self.status()
        by_path = {a["path"]: a for a in doc["artifacts"]}
        self.assertEqual(by_path["ci/manifest/WP-1.json"]["kind"], "work-package")
        self.assertEqual(by_path["ci/manifest/WP-1.json"]["lifecycle"], "invalid")
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")

    def test_an_unreadable_manifest_file_is_still_reported_invalid(self):
        """Unparseable content stays an honest invalid artifact rather
        than disappearing: only files that parse and are recognizably
        not work-package manifests are skipped."""
        self.write_descriptor()
        self.write("ci/manifest/broken.json", "{not json")
        _, doc = self.status()
        by_path = {a["path"]: a for a in doc["artifacts"]}
        self.assertEqual(by_path["ci/manifest/broken.json"]["kind"], "work-package")
        self.assertEqual(by_path["ci/manifest/broken.json"]["lifecycle"], "invalid")
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")

    def test_json_output_is_deterministic(self):
        self.write_descriptor()
        self.write("crates/a/specs/_boundaries/x.json", {**BOUNDARY, "boundary_id": "x"})
        _, first = self.run_cli("status", "--json")
        _, second = self.run_cli("status", "--json")
        self.assertEqual(first, second)
        self.assertTrue(first.endswith("\n"))


class NextActionSelectionTest(unittest.TestCase):
    """The documented priority is deterministic and picks exactly one
    action. Exercised directly against `_next_action` with synthetic
    analyses so each branch is isolated from gate discovery."""

    def _analysis(
        self, gate_runs=(), findings=(), descriptor=True, descriptor_state=None, descriptor_diagnostics=()
    ) -> Analysis:
        return Analysis(
            workspace=Path("/tmp"),
            descriptor={"crates": []} if descriptor else None,
            descriptor_state=descriptor_state or ("present-valid" if descriptor else "absent"),
            descriptor_error=None,
            artifacts=[],
            obligations=[],
            clusters=[],
            findings=list(findings),
            gate_runs=list(gate_runs),
            installation={},
            gate_integrity={},
            observations={},
            descriptor_diagnostics=list(descriptor_diagnostics),
        )

    def _finding(self, gate_id="r1-g16", reason="definition of done", authority="mechanized-gate"):
        return NormalizedFinding(
            gate_id=gate_id,
            severity="high",
            subject="crate::call",
            reason=reason,
            authority=authority,
            provenance="test",
        )

    def test_no_descriptor_has_no_next_action(self):
        self.assertIsNone(_next_action(self._analysis(descriptor=False)))

    def test_invalid_descriptor_recommends_fixing_it(self):
        """chainlink #73: a user-fixable input error gets a next_action
        naming the offending property -- the old behavior was an honest
        null that left a descriptor typo with no recovery path."""
        action = _next_action(
            self._analysis(
                descriptor=False,
                descriptor_state="present-invalid",
                descriptor_diagnostics=[
                    "$.port_source: unexpected property 'commit'; "
                    "permitted: language, oracle_build_command, repository"
                ],
            )
        )
        self.assertEqual(action["kind"], "human-decision")
        self.assertIsNone(action["command"])
        self.assertNotIn("action_id", action)
        self.assertIn("commit", action["description"])
        self.assertIn("project-descriptor.json", action["description"])

    def test_missing_c_static_recommends_refresh_c_static(self):
        action = _next_action(
            self._analysis(
                gate_runs=[{"gate_id": "r1-g16", "outcome": "blocked", "reason": "no C_static reports"}]
            )
        )
        self.assertEqual(action["kind"], "automated-command")
        self.assertEqual(action["action_id"], "refresh-c-static")
        self.assertIsNotNone(action["command"])

    def test_missing_bridge_checks_recommends_refresh_bridge_checks(self):
        action = _next_action(
            self._analysis(
                gate_runs=[{"gate_id": "g9", "outcome": "blocked", "reason": "check-bridges has not run"}]
            )
        )
        self.assertEqual(action["action_id"], "refresh-bridge-checks")

    def test_missing_witness_backend_recommends_refresh_witness(self):
        action = _next_action(
            self._analysis(
                gate_runs=[{"gate_id": "g19", "outcome": "blocked", "reason": "no witness backend configured"}]
            )
        )
        self.assertEqual(action["action_id"], "refresh-witness")

    def test_undeclared_call_recommends_author_interaction(self):
        action = _next_action(
            self._analysis(findings=[self._finding(reason="definite cross-concept call absent from interaction I")])
        )
        self.assertEqual(action["action_id"], "author-interaction")

    def test_human_decision_finding_yields_human_decision_kind(self):
        action = _next_action(
            self._analysis(
                findings=[self._finding(reason="medium-risk unresolved call", authority="human-decision-pending")]
            )
        )
        self.assertEqual(action["kind"], "human-decision")
        self.assertIsNone(action["command"])
        self.assertNotIn("action_id", action)

    def test_refresh_takes_priority_over_authoring(self):
        action = _next_action(
            self._analysis(
                gate_runs=[{"gate_id": "r1-g16", "outcome": "blocked", "reason": "no C_static reports"}],
                findings=[self._finding(reason="definite cross-concept call absent from interaction I")],
            )
        )
        self.assertEqual(action["action_id"], "refresh-c-static")


class ConsolidatedCheckDocumentTest(WorkspaceFixture):
    def test_empty_workspace_is_invalid_input_with_no_action(self):
        code, doc = self.check()
        self.assertEqual(code, 2)
        self.assertValid(CONSOLIDATED_CHECK_SCHEMA, doc, "consolidated-check")
        self.assertFalse(doc["mutated_workspace"])
        self.assertEqual(doc["result"], {"exit_code": 2, "conditions": ["invalid_input"]})
        self.assertIsNone(doc["next_action"])

    def test_valid_descriptor_no_findings_is_clean(self):
        self.write_descriptor()
        code, doc = self.check()
        self.assertValid(CONSOLIDATED_CHECK_SCHEMA, doc, "consolidated-check")
        self.assertFalse(doc["mutated_workspace"])
        self.assertEqual(doc["findings"], [])
        self.assertEqual(doc["result"]["exit_code"], 0)

    def test_missing_c_static_blocks_r1_g16_and_recommends_refresh(self):
        self.write_descriptor()
        code, doc = self.check()
        self.assertValid(CONSOLIDATED_CHECK_SCHEMA, doc, "consolidated-check")
        r1 = next(g for g in doc["gates"] if g["gate_id"] == "r1-g16")
        self.assertEqual(r1["outcome"], "blocked")
        self.assertIn("extract-c-static", r1["reason"])
        self.assertEqual(doc["next_action"]["action_id"], "refresh-c-static")
        # check never writes -- no C_static directory appears.
        self.assertFalse((self.workspace / "ci" / "results" / "c_static").exists())

    def test_invalid_artifact_is_a_blocking_finding(self):
        self.write_descriptor()
        self.write("crates/a/specs/_boundaries/broken.json", "{not json")
        code, doc = self.check()
        self.assertEqual(code, 1)
        self.assertValid(CONSOLIDATED_CHECK_SCHEMA, doc, "consolidated-check")
        self.assertIn("blocking_findings", doc["result"]["conditions"])
        self.assertTrue(any(f["severity"] == "high" for f in doc["findings"]))

    def test_draft_is_a_human_decision_not_a_mechanized_failure(self):
        self.write_descriptor()
        draft = {k: v for k, v in BOUNDARY.items() if k != "review"}
        self.write("crates/a/specs/_boundaries/draft.json", draft)
        code, doc = self.check()
        self.assertValid(CONSOLIDATED_CHECK_SCHEMA, doc, "consolidated-check")
        self.assertIn("human_decision_required", doc["result"]["conditions"])
        self.assertEqual(code, 3)
        self.assertTrue(
            any(f["authority"] == "human-decision-pending" for f in doc["findings"]),
            "an unapproved draft must surface as a pending human decision",
        )

    def test_stale_promoted_artifact_is_a_blocking_finding(self):
        self.write_descriptor()
        self.write("crates/a/specs/_boundaries/x.json", {**BOUNDARY, "boundary_id": "x"})
        promoted_hash = _sha256(self.boundary_path("x"))
        self.write(
            "specs/_promotions/scheduling.json",
            {
                "schema": "promotion-receipt/1.0",
                "promotion_id": "PROM-SCHEDULING-001",
                "cluster": "scheduling",
                "reviewer": "example-reviewer",
                "policy_version": "reliance-policy@0.1",
                "schema_versions": {"boundary": "1.0"},
                "accepted_at": "2026-08-31",
                "artifact_manifest": [{"path": "crates/a/specs/_boundaries/x.json", "hash": promoted_hash}],
            },
        )
        self.write(
            "crates/a/specs/_boundaries/x.json",
            {**BOUNDARY, "boundary_id": "x", "callee_guarantees": ["TaskQueue.C003", "TaskQueue.C004"]},
        )
        code, doc = self.check()
        self.assertEqual(code, 1)
        self.assertValid(CONSOLIDATED_CHECK_SCHEMA, doc, "consolidated-check")
        self.assertTrue(any("changed on disk since promotion" in f["reason"] for f in doc["findings"]))

    def test_deterministic_json_and_dedup(self):
        self.write_descriptor()
        self.write("crates/a/specs/_boundaries/broken.json", "{not json")
        _, first = self.run_cli("check", "--json")
        _, second = self.run_cli("check", "--json")
        self.assertEqual(first, second)
        doc = json.loads(first)
        keys = [f["dedup_key"] for f in doc["findings"]]
        self.assertEqual(len(keys), len(set(keys)), "findings were not deduplicated")
        self.assertValid(CONSOLIDATED_CHECK_SCHEMA, doc, "consolidated-check")

    def test_check_never_mutates_the_workspace(self):
        self.write_descriptor()
        self.write("crates/a/specs/_boundaries/x.json", {**BOUNDARY, "boundary_id": "x"})
        self.write("crates/a/specs/_boundaries/broken.json", "{not json")
        self.write("specs/_closure/mcmc-chain.json", {"cluster": "mcmc-chain", "closure_kind": "bounded"})
        before = self.snapshot()
        self.run_cli("check", "--json")
        self.run_cli("status", "--json")
        self.assertEqual(before, self.snapshot())

    def test_next_flag_prints_only_the_recommended_command(self):
        self.write_descriptor()
        _, out = self.run_cli("check", "next")
        self.assertIn("refresh-c-static", out)
        self.assertNotIn("gate g14:", out)
        self.assertNotIn("consolidated check", out)


class DescriptorPlaceholderTest(WorkspaceFixture):
    """Chainlink #67: a descriptor still holding the shipped init-template
    values is flagged by `check` (with a human-decision next action), while
    a genuinely edited one is not -- including a legitimate coincidental
    match on a value a real project could choose (`verifier_policy.default`)."""

    def write_init_descriptor(self, mode: str, name: str = "myproj") -> dict:
        """Exactly what `ligature init` writes for `mode`: the real
        `_render_descriptor` output, not a hand-rolled near-copy."""
        rendered = json.loads(ligature_install._render_descriptor(mode, name))
        self.descriptor_path.write_text(json.dumps(rendered))
        return rendered

    def p0_findings(self, doc: dict) -> list[dict]:
        return [f for f in doc["findings"] if f["gate_id"] == "P0"]

    def test_untouched_port_descriptor_is_flagged(self):
        self.write_init_descriptor("port")
        code, doc = self.check()
        self.assertValid(CONSOLIDATED_CHECK_SCHEMA, doc, "consolidated-check")
        self.assertEqual(code, 1)
        p0 = self.p0_findings(doc)
        self.assertEqual(len(p0), 1)
        self.assertIn("review.reviewer", p0[0]["reason"])
        self.assertIn("port_source.repository", p0[0]["reason"])
        self.assertEqual(p0[0]["authority"], "human-decision-pending")
        # The next action is the descriptor edit, not a downstream refresh.
        self.assertEqual(doc["next_action"]["kind"], "human-decision")
        self.assertIsNone(doc["next_action"]["command"])
        self.assertIn("project descriptor", doc["next_action"]["description"])

    def test_untouched_greenfield_descriptor_is_flagged(self):
        self.write_init_descriptor("greenfield")
        code, doc = self.check()
        self.assertEqual(code, 1)
        p0 = self.p0_findings(doc)
        self.assertEqual(len(p0), 1)
        self.assertIn("review.reviewer", p0[0]["reason"])

    def test_edited_descriptor_is_not_flagged(self):
        descriptor = self.write_init_descriptor("port")
        descriptor["review"]["reviewer"] = "real-reviewer"
        descriptor["port_source"]["repository"] = "/srv/real-project"
        self.descriptor_path.write_text(json.dumps(descriptor))
        code, doc = self.check()
        self.assertEqual(self.p0_findings(doc), [])
        self.assertEqual(code, 0)

    def test_coincidental_verifier_choice_is_not_flagged(self):
        # reviewer + repository are real, but the project happens to keep
        # the example's verifier default ("creusot"). That coincidence
        # alone must not be treated as an unedited template.
        descriptor = self.write_init_descriptor("port")
        descriptor["review"]["reviewer"] = "real-reviewer"
        descriptor["port_source"]["repository"] = "/srv/real-project"
        self.assertEqual(descriptor["verifier_policy"]["default"], "creusot")
        self.descriptor_path.write_text(json.dumps(descriptor))
        code, doc = self.check()
        self.assertEqual(self.p0_findings(doc), [])

    def test_a_strong_placeholder_left_behind_is_flagged_after_partial_edits(self):
        # The operator changed the reviewer but left the example repo path.
        descriptor = self.write_init_descriptor("port")
        descriptor["review"]["reviewer"] = "real-reviewer"
        self.descriptor_path.write_text(json.dumps(descriptor))
        self.assertEqual(
            ligature_install.descriptor_placeholder_fields(descriptor),
            ["port_source.repository"],
        )
        _, doc = self.check()
        self.assertEqual(len(self.p0_findings(doc)), 1)
        self.assertIn("port_source.repository", self.p0_findings(doc)[0]["reason"])

    def test_status_surfaces_the_placeholder_as_an_open_finding(self):
        self.write_init_descriptor("port")
        code, doc = self.status()
        self.assertEqual(code, 0)  # status is a read-only query; always 0
        self.assertTrue(any(f["gate_id"] == "P0" for f in doc["open_findings"]))

    def test_descriptor_placeholder_fields_ignores_missing_or_unknown_mode(self):
        self.assertEqual(ligature_install.descriptor_placeholder_fields({"mode": "other"}), [])
        self.assertEqual(ligature_install.descriptor_placeholder_fields({}), [])


class InvalidDescriptorDiagnosticTest(WorkspaceFixture):
    """chainlink #73: an invalid project descriptor was rejected with no
    actionable diagnostic -- `check` reported `conditions:
    [invalid_input]` next to an empty findings list and a null
    next_action, and `status`'s gate integrity claimed "no project
    descriptor present" while the descriptor file sat right there. A
    descriptor typo was indistinguishable from a missing descriptor."""

    def write_example_descriptor(self, example: str, mutate) -> dict:
        descriptor = json.loads((EXAMPLES / example).read_text())
        mutate(descriptor)
        self.descriptor_path.write_text(json.dumps(descriptor))
        return descriptor

    def test_unknown_property_finding_names_json_path_and_permitted_alternatives(self):
        """The exact reproduction of chainlink #73: `port_source.commit` --
        a natural way to pin the upstream revision. The finding must name
        the offending property, its JSON path, and the permitted
        alternatives."""
        self.write_example_descriptor(
            "project-descriptor.port.example.json",
            lambda d: d["port_source"].__setitem__("commit", "abc123"),
        )
        code, doc = self.check()
        self.assertEqual(code, 2)
        self.assertValid(CONSOLIDATED_CHECK_SCHEMA, doc, "consolidated-check")
        self.assertEqual(doc["result"]["conditions"], ["blocking_findings", "invalid_input"])
        self.assertEqual(len(doc["findings"]), 1)
        finding = doc["findings"][0]
        self.assertEqual(finding["gate_id"], "P0")
        self.assertEqual(finding["severity"], "high")
        self.assertEqual(finding["subject"], "project-descriptor.json")
        self.assertIn("$.port_source", finding["reason"])
        self.assertIn("commit", finding["reason"])
        self.assertIn("permitted: language, oracle_build_command, repository", finding["reason"])

    def test_next_action_recommends_fixing_the_descriptor(self):
        """A user-fixable input error gets a next_action naming the fix --
        the old behavior was `next_action: null` next to exit code 2."""
        self.write_example_descriptor(
            "project-descriptor.port.example.json",
            lambda d: d["port_source"].__setitem__("commit", "abc123"),
        )
        code, doc = self.check()
        self.assertEqual(code, 2)
        self.assertValid(CONSOLIDATED_CHECK_SCHEMA, doc, "consolidated-check")
        self.assertIsNotNone(doc["next_action"])
        self.assertEqual(doc["next_action"]["kind"], "human-decision")
        self.assertIsNone(doc["next_action"]["command"])
        self.assertNotIn("action_id", doc["next_action"])
        self.assertIn("commit", doc["next_action"]["description"])
        self.assertIn("project-descriptor.json", doc["next_action"]["description"])

    def test_status_gate_integrity_reports_present_but_invalid(self):
        """gate integrity claimed "no project descriptor present" while
        `descriptor.path` named the file that existed (chainlink #73)."""
        self.write_example_descriptor(
            "project-descriptor.port.example.json",
            lambda d: d["port_source"].__setitem__("commit", "abc123"),
        )
        _, doc = self.status()
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")
        self.assertEqual(doc["descriptor"]["state"], "present-invalid")
        self.assertEqual(doc["descriptor"]["path"], "project-descriptor.json")
        self.assertEqual(doc["gate_integrity"]["state"], "unknown")
        self.assertIn("present but invalid", doc["gate_integrity"]["details"])
        self.assertIn("commit", doc["gate_integrity"]["details"])
        self.assertNotEqual(doc["gate_integrity"]["details"], "no project descriptor present")
        # The same fact is an open finding in status, not only in check.
        self.assertTrue(any(f["gate_id"] == "P0" for f in doc["open_findings"]))

    def test_absent_descriptor_still_reports_no_descriptor_present(self):
        """The present-but-invalid wording must not fire for a genuinely
        absent descriptor -- the two states stay distinguishable."""
        _, doc = self.status()
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")
        self.assertEqual(doc["descriptor"]["state"], "absent")
        self.assertEqual(doc["gate_integrity"]["details"], "no project descriptor present")
        code, doc = self.check()
        self.assertEqual(code, 2)
        self.assertValid(CONSOLIDATED_CHECK_SCHEMA, doc, "consolidated-check")
        self.assertIsNone(doc["next_action"])
        self.assertEqual(doc["findings"], [])

    def test_unparseable_descriptor_is_reported_as_a_finding(self):
        self.descriptor_path.write_text("{not json")
        code, doc = self.check()
        self.assertEqual(code, 2)
        self.assertValid(CONSOLIDATED_CHECK_SCHEMA, doc, "consolidated-check")
        self.assertTrue(doc["findings"])
        self.assertIn("not valid JSON", doc["findings"][0]["reason"])
        self.assertIsNotNone(doc["next_action"])
        self.assertIn("not valid JSON", doc["next_action"]["description"])

    def test_unknown_top_level_property_lists_the_permitted_top_level_keys(self):
        """The pilot's probe shape `closure_kinds` (a near-miss of the new
        `closure_kind` field) must be rejected with the permitted
        alternatives named, so the typo is fixable in one look."""
        self.write_example_descriptor(
            "project-descriptor.greenfield.example.json",
            lambda d: d.__setitem__("closure_kinds", ["deductive"]),
        )
        code, doc = self.check()
        self.assertEqual(code, 2)
        reason = doc["findings"][0]["reason"]
        self.assertIn("closure_kinds", reason)
        self.assertIn("closure_kind", reason)  # the permitted alternative the pilot wanted
        self.assertIn("permitted:", reason)

    def test_verifier_policy_closure_kind_is_rejected_with_the_verifier_enum(self):
        """The pilot's second probe shape: `verifier_policy.closure_kind`
        is not the field -- the diagnostic must say what verifier_policy
        does permit, not just "invalid"."""
        self.write_example_descriptor(
            "project-descriptor.greenfield.example.json",
            lambda d: d["verifier_policy"].__setitem__("closure_kind", "deductive"),
        )
        code, doc = self.check()
        self.assertEqual(code, 2)
        reason = doc["findings"][0]["reason"]
        self.assertIn("$.verifier_policy.closure_kind", reason)
        self.assertIn("kani", reason)

    def test_declared_closure_kind_is_read_back_by_status(self):
        """The declared intent is recorded in the tool's own input and
        read back by status -- no out-of-band closure-intent file the
        tool cannot see at G14 time (chainlink #73)."""
        descriptor = self.write_example_descriptor(
            "project-descriptor.port.example.json",
            lambda d: d.__setitem__("closure_kind", "bounded"),
        )
        self.assertEqual(descriptor["closure_kind"], "bounded")
        _, doc = self.status()
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")
        self.assertEqual(doc["descriptor"]["state"], "present-valid")
        self.assertEqual(doc["descriptor"]["closure_kind"], "bounded")

    def test_undeclared_closure_kind_reads_back_as_null(self):
        self.write_example_descriptor("project-descriptor.port.example.json", lambda d: None)
        _, doc = self.status()
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")
        self.assertEqual(doc["descriptor"]["state"], "present-valid")
        self.assertIsNone(doc["descriptor"]["closure_kind"])

    def test_invalid_descriptor_check_is_deterministic(self):
        self.write_example_descriptor(
            "project-descriptor.port.example.json",
            lambda d: d["port_source"].__setitem__("commit", "abc123"),
        )
        _, first = self.run_cli("check", "--json")
        _, second = self.run_cli("check", "--json")
        self.assertEqual(first, second)
        self.assertTrue(first.endswith("\n"))


class DeclaredClosureKindTest(WorkspaceFixture):
    """chainlink #73 gap 1: the descriptor schema has no closure_kind
    field, so the intended closure_kind could only be recorded outside
    the tool. A declared value must validate, read back through status,
    and leave every downstream gate able to run."""

    def test_closure_kind_does_not_disturb_the_placeholder_finding(self):
        """A descriptor that declares closure_kind but is otherwise the
        untouched init template is still the #67 placeholder case --
        closure_kind is not a placeholder field, it is a declaration."""
        descriptor = json.loads(ligature_install._render_descriptor("port", "myproj"))
        descriptor["closure_kind"] = "deductive"
        self.descriptor_path.write_text(json.dumps(descriptor))
        code, doc = self.check()
        self.assertValid(CONSOLIDATED_CHECK_SCHEMA, doc, "consolidated-check")
        p0 = [f for f in doc["findings"] if f["gate_id"] == "P0"]
        self.assertEqual(len(p0), 1)
        self.assertIn("review.reviewer", p0[0]["reason"])


if __name__ == "__main__":
    unittest.main()
