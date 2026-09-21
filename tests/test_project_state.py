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

    def _analysis(self, gate_runs=(), findings=(), descriptor=True) -> Analysis:
        return Analysis(
            workspace=Path("/tmp"),
            descriptor={"crates": []} if descriptor else None,
            descriptor_state="present-valid" if descriptor else "absent",
            descriptor_error=None,
            artifacts=[],
            obligations=[],
            clusters=[],
            findings=list(findings),
            gate_runs=list(gate_runs),
            installation={},
            gate_integrity={},
            observations={},
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


if __name__ == "__main__":
    unittest.main()
