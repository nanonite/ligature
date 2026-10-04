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
from generate_promotion_receipt import compute_artifact_manifest  # noqa: E402
from validate_closure import CONDITION_KEYS, load_degradation_records  # noqa: E402

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

# The one cluster these tests' degradation records hang off. A bare
# `{"cluster": ...}` profile is not schema-valid, and chainlink #99's loader
# refuses a record beside one -- "there is nothing for it to be a departure
# from" -- so every test that expects a record to COUNT has to write the
# schema-valid pair, exactly as a real workspace does.
DEGRADED_CLUSTER = "mcmc-chain"

# artifact-kind -> a body the artifact discovery recognizes as that kind.
# Discovery keys on directory and extension; the identity field is what
# scripts/write_set.py's own recognition additionally requires, so
# ArtifactDirectoryConsistencyTest plants a body that satisfies both rather
# than a file that is merely in the right directory.
ARTIFACT_BODIES = {
    "boundary": {"boundary_id": "b"},
    "interaction": {"interaction_id": "i"},
    "exemption": {"interaction_id": "i"},
    "protocol-debt": {"interaction_id": "i"},
    "bridge": {"bridge_id": "br"},
    "witness": {"witness_id": "w"},
    "closure": {"cluster": "c"},
    "degradation-record": {"cluster": "c"},
    "gold-set": {"cluster": "c"},
    "evidence": {"id": "e"},
    "conflict-resolution": {"conflict_id": "c"},
    "promotion": {"promotion_id": "PROM-C-001"},
    "callsites": {"report_id": "r"},
    "work-package": {"work_package": "WP-1"},
}


def minimal_closure_profile(cluster: str = DEGRADED_CLUSTER, **conditions) -> dict:
    """A schema-valid closure profile declaring `single_verifier_system` as
    FAILING (and every other condition as holding), so a record naming that
    one condition beside it is a consistent pair rather than a stale
    excuse. `conditions` overrides individual condition bits -- that is how
    a test makes a record stale without hand-writing a second profile."""
    profile = {
        "schema_version": "1.0",
        "cluster": cluster,
        "closure_kind": "bounded",
        "work_packages": ["WP-MCMC-001"],
        "conditions": {
            "single_verifier_system": False,
            "owning_verifier": "creusot",
            "protocol_class_all_pairwise": True,
            "unresolved_indirect_calls_at_or_above_medium": 0,
            "generic_callees_type_universal_or_creusot_owned": True,
            "transitive_assumptions_within_policy": True,
            "scc_wellfoundedness_discharged": "not-applicable",
        },
        "review": {"reviewer": "alice", "reviewed_at": "2026-09-04"},
    }
    profile["conditions"].update(conditions)
    return profile


def accepted_record(
    cluster: str = DEGRADED_CLUSTER,
    failed_conditions=("single_verifier_system",),
    **overrides,
) -> dict:
    """A schema-valid degradation record -- the shape #99's loader accepts,
    and therefore the only shape whose `failed_conditions` `status` may
    report as a limitation."""
    record = {
        "schema_version": "1.0",
        "cluster": cluster,
        # Left exactly as given: a test that deliberately writes a
        # non-array `failed_conditions` must get one back, not
        # `list("...")` -- the shape it is testing is the shape that
        # reaches the loader.
        "failed_conditions": (
            list(failed_conditions) if isinstance(failed_conditions, (list, tuple)) else failed_conditions
        ),
        "affected_edges": ["mcmc__to__sample_prior"],
        "ceiling": "documented",
        "tracking_issue": "chainlink:101",
        "review": {"reviewer": "alice", "reviewed_at": "2026-09-04"},
    }
    record.update(overrides)
    return record


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
        # The descriptor declares compatibility_policy.reliance_policy_path
        # (docs/reliance-policy.md) -- a real workspace has that file
        # (`ligature init` installs it), so the fixture writes it too;
        # otherwise every check run reports the declared policy document
        # as missing (chainlink #78).
        self.write(
            "docs/reliance-policy.md",
            (ROOT / "docs" / "reliance-policy.template.md").read_text().replace(
                "# Reliance policy — `<project name>`", "# Reliance policy — `example-greenfield`", 1
            ),
        )
        return descriptor

    def init_workspace(self, mode="greenfield", name="myproj", crates=("crates/a",)):
        """A real `ligature init` -- which records the four gate_integrity
        hashes in a manifest, pinning them -- then the descriptor edits
        `write_descriptor` applies on top, so `check` runs against a
        pinned installation rather than an unpinned hand-written one
        (chainlink #75). Tests whose scenario is orthogonal to gate
        integrity use this to keep testing their own behavior.

        The installed policy template's `Policy version:` marker is stamped
        afterwards (chainlink #113): `init` deliberately installs it
        unfilled, and an unfilled one is a blocking `policy-version-marker`
        finding for `check` and a non-zero `doctor`. A workspace a real
        project reaches has had that one human step performed, and a test
        about anything else must not be measuring it."""
        code, out = self.run_cli("init", "--mode", mode, "--name", name)
        self.assertEqual(code, 0)
        code, out = self.run_cli(
            "accept-policy", "--reviewer", "real-reviewer", "--version", "reliance-policy@1.0"
        )
        self.assertEqual(code, 0, out)
        descriptor = json.loads(self.descriptor_path.read_text())
        descriptor["crates"] = [
            {"crate_dir": crate, "contracts_crate": "contracts", "specs_search_root": "crates"}
            for crate in crates
        ]
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

    def gate_g14(self) -> tuple[int, str]:
        """`pipeline.py gate-g14`'s own exit code and printed report, so the
        cross-command tests below compare what an operator sees from each
        command rather than three readings of one function's return value."""
        return self.run_cli("gate-g14")

    def validate_closure(self) -> tuple[int, str]:
        """`pipeline.py validate-closure`, for the same reason. Run through
        `pipeline.main` rather than the module's own `main()` so the
        workspace and descriptor flags are the ones an operator passes."""
        return self.run_cli("validate-closure")

    def approve_record(self, relative: str, reviewer: str = "alice", reviewed_at: str = "2026-09-04") -> None:
        """Append the audit entry review_checkpoint.approve() writes for a
        degradation record, so its `review` block is provenanced
        (chainlink #97). The log is workspace-scoped, which is why this
        writes under the fixture's own workspace rather than a tmpdir."""
        log = self.workspace / "ci" / "results" / "review_log.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as stream:
            stream.write(
                json.dumps({
                    "target_path": str(self.workspace / relative),
                    "classification": "new",
                    "reviewer": reviewer,
                    "reviewed_at": reviewed_at,
                    "validation": "checked",
                    "logged_at": f"{reviewed_at}T12:00:00+00:00",
                }) + "\n"
            )

    def rule_on_record(self, relative: str, verdict: str = "ratified") -> None:
        """Append the human ruling generate_promotion_receipt.record_ruling()
        writes over the record's CURRENT bytes -- the hash comes from the
        same compute_artifact_manifest() the gate re-computes with, so
        ratify-then-edit stops covering the record (chainlink #82/#97)."""
        log = self.workspace / "ci" / "results" / "human_rulings.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as stream:
            stream.write(
                json.dumps({
                    "artifacts": compute_artifact_manifest(self.workspace, [relative]),
                    "verdict": verdict,
                    "reviewer": "alice",
                    "ruled_at": "2026-09-05",
                    "logged_at": "2026-09-05T12:00:00+00:00",
                }) + "\n"
            )

    def write_accepted_record(self, relative: str, record: dict) -> Path:
        """A record that may actually RELEASE a cluster under chainlink
        #97: written, then provenanced, then ratified over its exact bytes.
        A plain `write()` of a record is correctly not enough -- #97 made a
        self-asserted `review` block unable to excuse anything by itself."""
        path = self.write(relative, record)
        self.approve_record(relative)
        self.rule_on_record(relative)
        return path

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

    def test_an_accepted_record_reports_limitations_and_change_request(self):
        # chainlink #101: a record reaches `limitations=` only if
        # validate_closure's loader (#99) accepts it, so this fixture writes
        # the schema-valid pair a real workspace has. The bare
        # `{"cluster": ...}` profile and record this test used to write are
        # BOTH refused by `validate-closure`, and the pre-#101 `_clusters`
        # reported the refused record's `failed_conditions` as declared
        # limitations anyway -- on a workspace two other commands failed at
        # 1. The record is now genuinely accepted, so its excuse IS a
        # limitation; the cluster's `state` is gate-g14's own verdict on
        # this fixture's (deliberately evidence-free) closure, which has
        # nothing to do with the record.
        self.write_descriptor()
        self.write("specs/_closure/mcmc-chain.json", minimal_closure_profile())
        self.write_accepted_record(
            "specs/_closure/mcmc-chain.degradation.json", accepted_record()
        )
        _, doc = self.status()
        cluster = next(c for c in doc["clusters"] if c["cluster"] == "mcmc-chain")
        self.assertEqual(cluster["closure_kind"], "bounded")
        self.assertEqual(cluster["state"], "blocked")
        self.assertEqual(cluster["limitations"], ["single_verifier_system"])
        self.assertEqual([c["target"] for c in doc["change_requests"]], ["specs/_closure/mcmc-chain.json"])
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")

    def test_an_info_note_is_not_a_limitation_of_a_closing_cluster(self):
        """chainlink #93: `_clusters` collected the condition tag off
        EVERY gate-g14 finding into `limitations=`, including the info
        note itself -- so `status` printed `limitations=['
        generic_callees_type_universal_or_creusot_owned']` on exactly
        the cluster the same output called VERIFIED and closed. An info
        note reports a checked state; only findings that still limit the
        cluster (error/degraded) belong in the list, and a degradation
        record's own failed_conditions, unchanged, still lead."""
        import gate_g14

        self.write_descriptor()
        self.write(
            "specs/_closure/mcmc-chain.json", {"cluster": "mcmc-chain", "closure_kind": "deductive"}
        )
        outcome = gate_g14.ClusterOutcome(
            cluster="mcmc-chain",
            status="closes",
            closure_kind="deductive",
            work_packages=1,
            obligations=1,
            cycles=[],
            findings=[
                gate_g14.Finding(
                    "G14", "mcmc-chain",
                    "generic_callees_type_universal_or_creusot_owned is VERIFIED from artifact "
                    "data, not taken on declaration",
                    severity="info",
                    condition="generic_callees_type_universal_or_creusot_owned",
                ),
                gate_g14.Finding(
                    "G14", "mcmc-chain",
                    "a failing condition nobody excused",
                    condition="single_verifier_system",
                ),
            ],
        )
        real_gate_workspace = gate_g14.gate_workspace
        gate_g14.gate_workspace = lambda workspace, descriptor: ([outcome], [])
        try:
            _, doc = self.status()
        finally:
            gate_g14.gate_workspace = real_gate_workspace
        cluster = next(c for c in doc["clusters"] if c["cluster"] == "mcmc-chain")
        self.assertEqual(cluster["state"], "closes")
        self.assertEqual(cluster["limitations"], ["single_verifier_system"])
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")

    def test_a_partially_verified_cluster_kind_is_echoed(self):
        """chainlink #85: a closure profile declaring `partial` must flow
        through `status --json` and still validate against the
        project-state schema -- the echo must not reject the value
        `validate-closure` accepts."""
        self.write_descriptor()
        self.write("specs/_closure/mcmc-chain.json", {"cluster": "mcmc-chain", "closure_kind": "partial"})
        _, doc = self.status()
        cluster = next(c for c in doc["clusters"] if c["cluster"] == "mcmc-chain")
        self.assertEqual(cluster["closure_kind"], "partial")
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
        # chainlink #75 Part 3: the manifest records no gate hashes, so
        # gate integrity is `unpinned` and installation_manifest.state
        # drops from "current" to "unknown" -- a consumer reading only
        # `status --json` is no longer told the installation is current
        # while its gate pins are unverifiable.
        self.assertEqual(doc["installation_manifest"]["state"], "unknown")
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
        # chainlink #75 Part 3: no gate hashes are recorded, so the state
        # drops to "unknown" even though the recorded versions match.
        self.assertEqual(doc["installation_manifest"]["state"], "unknown")
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


class CanonicalDegradationRecordLoaderTest(WorkspaceFixture):
    """chainlink #99: `status` must read degradation records through the
    one validated loader, not by parsing whatever sits next to the
    closure profile.

    #99 does not rewire `_clusters` (chainlink #101 does that); what it
    owes this command is the guarantee the wiring relies on -- the loader
    reaches a defensible verdict on exactly the records `_clusters`
    currently reads off disk, including the ones it reads without any
    validation at all."""

    RECORD = "specs/_closure/mcmc-chain.degradation.json"

    def load(self):
        return load_degradation_records(self.workspace)

    def degraded_record(self, **overrides):
        """A schema-valid record over the fixture's minimal profile. The
        profile `WorkspaceFixture` writes is itself schema-minimal, so
        this is the record shape a real workspace's `status` sees."""
        return accepted_record(**overrides)

    def minimal_profile(self):
        """A schema-valid closure profile declaring `single_verifier_system`
        as FAILING, so the record beside it is a consistent pair rather
        than a stale excuse. The bare `{"cluster": ...}` profiles other
        tests in this module write are not schema-valid, and a record
        beside an unreadable profile is refused for exactly that
        reason."""
        self.write_descriptor()
        self.write("specs/_closure/mcmc-chain.json", minimal_closure_profile())

    def test_a_valid_record_is_the_one_the_loader_hands_status(self):
        self.minimal_profile()
        self.write(self.RECORD, self.degraded_record())
        records = self.load()
        self.assertEqual(records.record_for("mcmc-chain"), self.degraded_record())
        self.assertEqual(records.invalid, {})

    def test_a_record_status_today_reads_without_validation_is_refused(self):
        """`ligature status` currently lists a record's
        `failed_conditions` as the cluster's `limitations=` with no
        validation whatsoever -- so a malformed or schema-invalid record
        is reported as a declared limitation. The loader refuses exactly
        what `validate-closure` refuses, which is what #101 wires in."""
        self.minimal_profile()
        self.write(self.RECORD, self.degraded_record(ceiling="creusot-deductive-check"))
        records = self.load()
        self.assertEqual(records.record_for("mcmc-chain"), None)
        reasons = records.reasons_for(self.workspace / self.RECORD)
        self.assertTrue(any("creusot-deductive-check" in r for r in reasons), reasons)
        self.assertEqual(
            {p.name for p in records.invalid}, {"mcmc-chain.degradation.json"}
        )

    def test_a_malformed_failed_conditions_is_refused_rather_than_iterated(self):
        """`[str(c) for c in degradation.get("failed_conditions", []) or []]`
        iterates whatever is there: a string became one limitation per
        character. The loader refuses the record instead, so a caller can
        never read that list without a guarantee about its shape."""
        self.minimal_profile()
        self.write(self.RECORD, self.degraded_record(failed_conditions="single_verifier_system"))
        records = self.load()
        self.assertEqual(records.record_for("mcmc-chain"), None)
        self.assertTrue(
            any("failed_conditions must be an array" in r
                for r in records.reasons_for(self.workspace / self.RECORD))
        )

    def test_a_record_with_no_profile_is_refused_not_reported_degraded(self):
        self.write_descriptor()
        self.write(self.RECORD, self.degraded_record())
        records = self.load()
        self.assertEqual(records.record_for("mcmc-chain"), None)
        self.assertTrue(
            any("no closure profile beside it" in r
                for r in records.reasons_for(self.workspace / self.RECORD))
        )

    def test_a_record_with_no_closure_directory_at_all_is_named_by_location(self):
        """`_clusters` reads the record sitting next to the closure
        profile, so a record mislocated away from `specs/_closure/` is
        invisible to it. The loader is where that becomes nameable: it
        must report the record `validate-closure` refuses rather than
        returning an empty result that reads as 'nothing is declared'
        (chainlink #101 is what consumes this)."""
        self.write_descriptor()
        self.write("docs/_closure/mcmc-chain.degradation.json", self.degraded_record())
        self.assertFalse((self.workspace / "specs" / "_closure").exists())

        records = self.load()
        self.assertEqual(records.valid, {})
        mislocated = self.workspace / "docs/_closure/mcmc-chain.degradation.json"
        self.assertIn(mislocated, records.invalid)
        self.assertTrue(
            any("not directly under the canonical directory" in r for r in records.reasons_for(mislocated))
        )


class StatusReadsRecordsThroughTheCanonicalLoaderTest(WorkspaceFixture):
    """chainlink #101: `status`/`check` derive a cluster's `limitations` and
    its degraded/unknown state ONLY from degradation records chainlink
    #99's loader accepts -- the same records gate-g14 acts on (#100).

    The pre-#101 `_clusters` opened `<cluster>.degradation.json` beside the
    profile and took whatever parsed, with no validation at all. So on a
    workspace where `validate-closure` exits 1 and gate-g14 refuses to
    release, `status` reported a cluster as `degraded` under a list of
    "limitations" that could include one entry per CHARACTER of a string
    `failed_conditions`. Each test below therefore pins the same fact from
    three commands: what the loader refuses is a limitation for nobody.

    The cross-command shape matters more than any single assertion: a
    refused record must leave `status` reporting exactly what it reports on
    a workspace where no degradation is declared at all (that is what
    `limitations_with_no_record_at_all` measures), while the two commands
    that DO refuse it say so out loud with their own exit codes.
    """

    RECORD = "specs/_closure/mcmc-chain.degradation.json"

    def setUp(self):
        super().setUp()
        self.write_descriptor()
        self.write("specs/_closure/mcmc-chain.json", minimal_closure_profile())

    def cluster(self) -> dict:
        _, doc = self.status()
        cluster = next(c for c in doc["clusters"] if c["cluster"] == DEGRADED_CLUSTER)
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")
        return cluster

    def limitations_with_no_record_at_all(self) -> list[str]:
        """The same workspace with the record taken away: what `status`
        reports when no degradation is declared. Compare against this
        rather than against a hard-coded list, so the assertion is about
        the record's absence having no effect rather than about which
        conditions this fixture's (deliberately evidence-free) closure
        happens to trip gate-g14 on."""
        (self.workspace / self.RECORD).unlink(missing_ok=True)
        return self.cluster()["limitations"]

    def assertNoCommandReleasesTheRecord(self, *expected_fragments):
        """The two commands that judge a record must both fail, and neither
        may print a cluster as released under it."""
        code, printed = self.validate_closure()
        self.assertEqual(code, 1, printed)
        for fragment in expected_fragments:
            self.assertIn(fragment, printed)
        gate_code, gate_printed = self.gate_g14()
        self.assertNotEqual(gate_code, 0, gate_printed)
        self.assertNotIn(
            "released under an accepted degradation record",
            gate_printed,
            "a record no command may act on must never be printed as an accepted release",
        )

    def test_an_accepted_record_is_the_one_status_reports_as_a_limitation(self):
        """The control: a record all three commands accept is still a
        limitation, and a `degraded`/`blocked` state is reported rather than
        the `unknown` fallback for a cluster that declares nothing."""
        self.write_accepted_record(self.RECORD, accepted_record())

        code, printed = self.validate_closure()
        self.assertEqual(code, 0, printed)
        gate_printed = self.gate_g14()[1]
        self.assertNotIn(
            "refused by `pipeline.py validate-closure`",
            gate_printed,
            "gate-g14 must see this as the cluster's record, not as one it has to refuse",
        )

        cluster = self.cluster()
        self.assertEqual(cluster["limitations"], ["single_verifier_system"])
        self.assertNotEqual(
            cluster["state"], "unknown", "a cluster with an accepted record is not in the unknown fallback"
        )

    def test_a_stale_excuse_is_a_limitation_of_none_of_the_three_commands(self):
        """G17's record-side direction: the record names
        `scc_wellfoundedness_discharged`, which this profile declares as
        holding ('not-applicable'). Refusing the record for that takes its
        one legitimate excuse (`single_verifier_system`) with it -- one
        record is one excuse list, not two independently votable ones."""
        self.write(
            self.RECORD,
            accepted_record(
                failed_conditions=["single_verifier_system", "scc_wellfoundedness_discharged"]
            ),
        )

        self.assertNoCommandReleasesTheRecord("a stale excuse")

        self.assertEqual(self.cluster()["limitations"], self.limitations_with_no_record_at_all())

    def test_a_string_failed_conditions_is_never_iterated_as_limitations(self):
        """The pilot's exact wrong output: `_clusters` ran
        `[str(c) for c in degradation.get("failed_conditions", []) or []]`
        over whatever sat on disk, so a record whose failed_conditions was
        the string "single_verifier_system" reported twenty-one limitations
        -- 's', 'i', 'n', ... -- on a cluster `validate-closure` refused at
        all."""
        self.write(self.RECORD, accepted_record(failed_conditions="single_verifier_system"))

        self.assertNoCommandReleasesTheRecord(
            "failed_conditions must be an array of closure-condition keys"
        )

        cluster = self.cluster()
        self.assertEqual(
            [c for c in cluster["limitations"] if c not in CONDITION_KEYS],
            [],
            f"a limitation must be a closure-condition key, not a character: {cluster['limitations']}",
        )
        self.assertNotIn("s", cluster["limitations"])
        self.assertEqual(cluster["limitations"], self.limitations_with_no_record_at_all())

    def test_a_schema_invalid_record_is_not_a_limitation_either(self):
        """Schema-invalid for a field the loader does not otherwise police:
        `ceiling` is not one of the schema's values. The refusal is a
        property of the record, so the record excuses nothing."""
        self.write(self.RECORD, accepted_record(ceiling="creusot-deductive-check"))

        self.assertNoCommandReleasesTheRecord("is not one of")

        self.assertEqual(self.cluster()["limitations"], self.limitations_with_no_record_at_all())

    def test_a_record_filed_outside_the_closure_directory_is_not_a_limitation(self):
        """#99's `misplaced` half, consumed here: a record filed away from
        specs/_closure/ is refused by location, so it is not the record
        beside the profile either -- and it is the workspace's only record,
        the shape where silence would read as "no degradation declared"."""
        self.write("docs/_closure/mcmc-chain.degradation.json", accepted_record())

        self.assertNoCommandReleasesTheRecord("not directly under the canonical directory")

        self.assertEqual(self.cluster()["limitations"], self.limitations_with_no_record_at_all())

    def test_check_reports_the_same_closure_state_as_status(self):
        """`check` builds its cluster facts from the same `Analysis`, so the
        degraded/unknown state it derives for a cluster is the one `status`
        reports. Asserted on the stale-excuse workspace, where the two
        commands an operator runs after a refusal are `check` and
        `gate-g14`."""
        self.write(
            self.RECORD,
            accepted_record(
                failed_conditions=["single_verifier_system", "scc_wellfoundedness_discharged"]
            ),
        )

        _, doc = self.check()
        stub = next(c for c in doc["change_request_stubs"] if "mcmc-chain" in c["change_request_id"])
        status_cluster = self.cluster()
        self.assertIn(
            f"is {status_cluster['state']}",
            stub["summary"],
            f"the change-request stub must describe the state status reports: {stub['summary']!r}",
        )
        self.assertIn(
            str(status_cluster["limitations"]),
            stub["summary"],
            "the stub's limitation list is the status report's, item for item",
        )


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
            write_set={},
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
        # chainlink #75: an empty workspace has no descriptor, so gate
        # integrity is `unknown` and check fails closed on BOTH conditions
        # -- invalid_input (no descriptor to check) and
        # gate_integrity_failed (nothing is pinned) -- exiting 5, the new
        # top-precedence code.
        self.assertEqual(code, 5)
        self.assertValid(CONSOLIDATED_CHECK_SCHEMA, doc, "consolidated-check")
        self.assertFalse(doc["mutated_workspace"])
        self.assertEqual(
            doc["result"],
            {
                "exit_code": 5,
                "conditions": ["blocking_findings", "gate_integrity_failed", "invalid_input"],
            },
        )
        self.assertIsNone(doc["next_action"])

    def test_valid_descriptor_no_findings_is_clean(self):
        self.init_workspace()
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
        self.init_workspace()
        self.write("crates/a/specs/_boundaries/broken.json", "{not json")
        code, doc = self.check()
        self.assertEqual(code, 1)
        self.assertValid(CONSOLIDATED_CHECK_SCHEMA, doc, "consolidated-check")
        self.assertIn("blocking_findings", doc["result"]["conditions"])
        self.assertTrue(any(f["severity"] == "high" for f in doc["findings"]))

    def test_staged_draft_file_is_inert_not_a_finding(self):
        """Chainlink #90: a staged `crates/a/specs/_interactions/
        I-x.json.draft` (what `draft 3 interaction-drafting` writes -- no
        `review` block, only approve adds one) used to surface through
        `_run_standalone_validators` as one medium finding per staged
        draft with authority human-decision-pending, adding
        human_decision_required to check's conditions -- while the
        equivalent staged BOUNDARY draft was already invisible, because
        validate_boundary_contracts.validate() skips non-.json. Both
        validators now share the `.draft` convention: neither reports
        staged drafts, and neither counts them."""
        self.init_workspace()
        staged = {
            "schema_version": "1.0",
            "interaction_id": "I-SCHED-TQ-001",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "edge_class": ["stateful", "cross-verifier"],
            "eligibility": "boundary-required",
            "rationale": "staged, not yet approved",
            "evidence_links": ["E-0143"],
            "protocol_class": "pairwise",
            "realization": {
                "requirement": "required",
                "config_scope": {"target": "x86_64-unknown-linux-gnu", "features": ["default"], "cfg": []},
            },
            # deliberately no `review` -- that is what draft stages
        }
        self.write("crates/a/specs/_interactions/I-SCHED-TQ-001.json.draft", staged)
        code, doc = self.check()
        self.assertValid(CONSOLIDATED_CHECK_SCHEMA, doc, "consolidated-check")
        self.assertEqual(doc["findings"], [], [str(f) for f in doc["findings"]])
        self.assertEqual(code, 0)
        self.assertNotIn("human_decision_required", doc["result"]["conditions"])

    def test_staged_drafts_of_every_remaining_kind_are_inert_not_findings(self):
        """Chainlink #110: #90 fixed this for interaction and boundary
        drafts and left the other four draft-capable kinds collecting
        theirs, so the swisstable-verus pilot's Stage 0-3 set (6 bridges,
        2 witnesses, 1 conflict resolution) made `validate-bridge`,
        `validate-witness` and `validate-conflict-resolution` exit 1 and
        pinned `check --json` at exit 3 with nine medium
        human-decision-pending findings -- each one a staged draft, and
        none of them distinguishable from a genuine schema break. Every
        kind now shares one rule: a staged draft is a pending artifact,
        inert until `approve` promotes it."""
        self.init_workspace()
        staged = {
            "crates/a/specs/_bridges/BR-SCHED-TQ-001.json.draft": {
                "schema_version": "1.0",
                "bridge_id": "BR-SCHED-TQ-001",
                "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
                "callee_requirement": "TaskQueue.C003",
                "available_contract_facts": [
                    {"obligation_id": "Scheduler.C010", "role": "caller-precondition"},
                ],
                "target_expression": "TaskQueue.C003(args, callee_state)",
                "protocol_class": "pairwise",
                "bridge_logic": {
                    "bindings": {"caller_self": "Scheduler"},
                    "premises": ["Scheduler.I001(caller_self)"],
                    "conclusion": {"obligation_id": "TaskQueue.C003"},
                },
                # deliberately no `review` -- that is what draft stages
            },
            "crates/a/specs/_witnesses/task_queue.load_factor.json.draft": {
                "schema_version": "1.0",
                "witness_id": "W-TQ-LOAD-FACTOR",
                "concept": "TaskQueue",
                "query": "load_factor",
                "fixture": {
                    "fixture_id": "FX-QUEUE-BOTTOM-ROW",
                    "seed": 0,
                    "description": "8-slot queue, 3 ready tasks front-loaded",
                },
                "renderer": "scalar_field_svg",
                "expectation": {
                    "renderer": "scalar_field_svg",
                    "coverage_region": "bottom-row",
                    "value_distribution": "must-vary",
                    "fixture_family": "FX-BOTTOM-ROW",
                },
                "determinism": {
                    "value_hash": "sha256:" + "b" * 64,
                    "claim": "byte-identical-across-runs",
                    "platforms": ["x86_64-unknown-linux-gnu"],
                },
                "output": {
                    "path": "docs/witnesses/task_queue.load_factor.svg",
                    "render_hash": "sha256:" + "c" * 64,
                    "renderer_actual": "scalar_field_svg",
                },
            },
            "crates/a/specs/_exemptions/I-SCHED-TQ-001.json.draft": {
                "schema_version": "1.0",
                "interaction_id": "I-SCHED-TQ-001",
                "rationale": "Prototype scaffolding boundary, tracked for removal (chainlink:#41)",
            },
            "crates/a/specs/_protocol_debt/I-SCHED-TQ-001.json.draft": {
                "schema_version": "1.0",
                "interaction_id": "I-SCHED-TQ-001",
                "rationale": "Handshake protocol not yet modeled",
                "no_promoted_obligation_depends_on_protocol": True,
                "no_work_package_touches_its_path": True,
                "no_release_claim_includes_it": True,
                "tracking_issue": "chainlink:#99",
            },
            "specs/_conflicts/EC-004.json.draft": {
                "schema_version": "1.0",
                "conflict_id": "EC-004",
                "evidence": ["E-0143", "E-0201"],
                "status": "resolved",
                "resolution": {
                    "selected_authority": "E-0201",
                    "disposition_of_other": "incidental",
                    "rationale": "compatibility policy: do not preserve the legacy defect",
                },
            },
        }
        for relative, body in staged.items():
            self.write(relative, body)
        code, doc = self.check()
        self.assertValid(CONSOLIDATED_CHECK_SCHEMA, doc, "consolidated-check")
        self.assertEqual(doc["findings"], [], [str(f) for f in doc["findings"]])
        self.assertEqual(code, 0)
        self.assertNotIn("human_decision_required", doc["result"]["conditions"])

    def test_draft_is_a_human_decision_not_a_mechanized_failure(self):
        self.init_workspace()
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
        self.init_workspace()
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


class GateIntegrityCheckTest(WorkspaceFixture):
    """chainlink #75: `check` fails closed on installation integrity.

    Before #75, `check --json` exited 0 with empty findings while a
    gate-pinned file was tampered or deleted -- the plan's
    "check exits 0" criterion could not report a modified gate
    definition. These tests pin the new behavior: a non-pinned
    gate_integrity state produces exit 5, the gate_integrity_failed
    condition, and a finding naming the drifted or missing path; and
    `status --json`'s installation_manifest.state drops its "current"
    verdict whenever gate integrity is not pinned."""

    def test_tampered_gate_pinned_file_fails_check_closed(self):
        """Part 1, exact reproduction: appending a comment after the
        authority-region END marker trips gate_integrity's hash drift,
        and check must now fail closed naming the path."""
        self.init_workspace()
        skill = self.workspace / ".codex" / "skills" / "ligature" / "SKILL.md"
        skill.write_text(skill.read_text() + "\n<!-- TAMPERED -->\n")
        code, doc = self.check()
        self.assertEqual(code, 5)
        self.assertValid(CONSOLIDATED_CHECK_SCHEMA, doc, "consolidated-check")
        self.assertEqual(doc["result"]["exit_code"], 5)
        self.assertIn("gate_integrity_failed", doc["result"]["conditions"])
        integrity = [f for f in doc["findings"] if f["gate_id"] == "gate-integrity"]
        self.assertEqual(len(integrity), 1)
        self.assertEqual(integrity[0]["subject"], ".codex/skills/ligature/SKILL.md")
        self.assertIn("hash drift", integrity[0]["reason"])
        self.assertEqual(integrity[0]["severity"], "high")
        self.assertEqual(integrity[0]["authority"], "mechanized-gate")

    def test_missing_gate_pinned_file_fails_check_closed(self):
        """Part 1, deletion variant: a deleted gate schema is named by
        the finding, not just by status/doctor."""
        self.init_workspace()
        (self.workspace / ".ligature" / "schemas" / "consolidated-check.schema.json").unlink()
        code, doc = self.check()
        self.assertEqual(code, 5)
        self.assertValid(CONSOLIDATED_CHECK_SCHEMA, doc, "consolidated-check")
        self.assertIn("gate_integrity_failed", doc["result"]["conditions"])
        integrity = [f for f in doc["findings"] if f["gate_id"] == "gate-integrity"]
        self.assertEqual(len(integrity), 1)
        self.assertEqual(
            integrity[0]["subject"], ".ligature/schemas/consolidated-check.schema.json"
        )
        self.assertIn("missing", integrity[0]["reason"])

    def test_unpinned_workspace_fails_check_closed(self):
        """A descriptor that declares gate paths with no manifest
        recording their hashes is `unpinned` -- check fails closed with
        a workspace-level finding (no individual path to name)."""
        self.write_descriptor()
        code, doc = self.check()
        self.assertEqual(code, 5)
        self.assertValid(CONSOLIDATED_CHECK_SCHEMA, doc, "consolidated-check")
        self.assertIn("gate_integrity_failed", doc["result"]["conditions"])
        integrity = [f for f in doc["findings"] if f["gate_id"] == "gate-integrity"]
        self.assertEqual(len(integrity), 1)
        self.assertIn("no installation manifest records their hashes", integrity[0]["reason"])

    def test_pinned_workspace_has_no_gate_integrity_finding(self):
        """The flip side: a clean init pins every path, so check reports
        no gate-integrity finding and exits 0 (nothing else outstanding)."""
        self.init_workspace()
        code, doc = self.check()
        self.assertEqual(code, 0)
        self.assertEqual(doc["result"]["conditions"], [])
        self.assertFalse([f for f in doc["findings"] if f["gate_id"] == "gate-integrity"])

    def test_status_installation_state_drops_current_when_gate_integrity_drifts(self):
        """Part 3: status --json must not report the installation as
        current while a gate-pinned file has been tampered."""
        self.init_workspace()
        skill = self.workspace / ".codex" / "skills" / "ligature" / "SKILL.md"
        skill.write_text(skill.read_text() + "\n<!-- TAMPERED -->\n")
        _, doc = self.status()
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")
        self.assertEqual(doc["gate_integrity"]["state"], "drifted")
        self.assertEqual(doc["installation_manifest"]["state"], "drifted")

    def test_status_installation_state_drops_current_when_unpinned(self):
        """Part 3: a manifest that records no gate hashes leaves the
        installation state at "unknown", not "current"."""
        self.write_descriptor()
        self.write(
            "ci/manifest/installation.json",
            {
                "installed_product_version": project_state.PRODUCT_VERSION,
                "installed_schema_versions": {"project-state": "1.0"},
            },
        )
        _, doc = self.status()
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")
        self.assertEqual(doc["gate_integrity"]["state"], "unpinned")
        self.assertEqual(doc["installation_manifest"]["state"], "unknown")

    def test_status_installation_state_current_when_pinned(self):
        """The flip side of Part 3: a clean init reports current -- the
        state only drops when gate integrity is not pinned."""
        self.init_workspace()
        _, doc = self.status()
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")
        self.assertEqual(doc["gate_integrity"]["state"], "pinned")
        self.assertEqual(doc["installation_manifest"]["state"], "current")


class DescriptorPlaceholderTest(WorkspaceFixture):
    """Chainlink #67: a descriptor still holding the shipped init-template
    values is flagged by `check` (with a human-decision next action), while
    a genuinely edited one is not -- including a legitimate coincidental
    match on a value a real project could choose (`verifier_policy.default`)."""

    def write_init_descriptor(self, mode: str, name: str = "myproj") -> dict:
        """Exactly what `ligature init` writes for `mode`: a real init, so
        the four gate_integrity paths are pinned in a manifest and `check`
        isolates the placeholder behavior under test rather than also
        failing closed on an unpinned workspace (chainlink #75). The
        policy template's `Policy version:` marker is stamped too
        (chainlink #113), so an unfilled marker is not a second, unrelated
        finding in a test about descriptor placeholders."""
        code, _ = self.run_cli("init", "--mode", mode, "--name", name)
        self.assertEqual(code, 0)
        code, out = self.run_cli("accept-policy", "--reviewer", "real-reviewer", "--version", "reliance-policy@1.0")
        self.assertEqual(code, 0, out)
        return json.loads(self.descriptor_path.read_text())

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
        # chainlink #75: the invalid descriptor makes gate integrity
        # `unknown`, so check also fails the installation/gate-integrity
        # gate -- exit 5 with gate_integrity_failed alongside the
        # invalid_input and blocking_findings conditions.
        self.assertEqual(code, 5)
        self.assertValid(CONSOLIDATED_CHECK_SCHEMA, doc, "consolidated-check")
        self.assertEqual(
            doc["result"]["conditions"],
            ["blocking_findings", "gate_integrity_failed", "invalid_input"],
        )
        # The P0 diagnostic plus the workspace-level gate-integrity
        # finding (the invalid descriptor makes the pins unreadable).
        self.assertEqual(len(doc["findings"]), 2)
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
        # chainlink #75: exit 5 (gate_integrity_failed outranks
        # invalid_input); the next_action is unchanged -- the descriptor
        # edit is still the recommended fix.
        self.assertEqual(code, 5)
        self.assertValid(CONSOLIDATED_CHECK_SCHEMA, doc, "consolidated-check")
        self.assertIsNotNone(doc["next_action"])
        self.assertEqual(doc["next_action"]["kind"], "human-decision")
        self.assertIsNone(doc["next_action"]["command"])
        self.assertNotIn("action_id", doc["next_action"])
        self.assertIn("commit", doc["next_action"]["description"])
        self.assertIn("project-descriptor.json", doc["next_action"]["description"])

    def test_a_typo_is_distinguishable_from_an_unknown_key(self):
        """chainlink #106: `zzz_not_a_real_field` (a key the schema never
        had) and `closure_kinds` (a near miss of one it does have)
        produced a byte-identical `unexpected property '<key>'` reason,
        so the user had to eyeball the whole permitted list to recover
        the intended key. `check` must carry the did-you-mean on the
        finding and in the next_action that names the edit to make."""
        self.write_example_descriptor(
            "project-descriptor.port.example.json",
            lambda d: d.__setitem__("closure_kinds", ["deductive"]),
        )
        code, doc = self.check()
        self.assertEqual(code, 5)
        self.assertValid(CONSOLIDATED_CHECK_SCHEMA, doc, "consolidated-check")
        reason = next(f["reason"] for f in doc["findings"] if f["gate_id"] == "P0")
        self.assertIn("unexpected property 'closure_kinds'", reason)
        self.assertIn("did you mean 'closure_kind'?", reason)
        self.assertIn("did you mean 'closure_kind'?", doc["next_action"]["description"])

    def test_an_unknown_key_still_gets_no_guess(self):
        """The other half: a key with no near miss in the schema keeps
        chainlink #73's message exactly, so a caller matching on it sees
        no change in the case that never had a better answer to give."""
        self.write_example_descriptor(
            "project-descriptor.port.example.json",
            lambda d: d.__setitem__("zzz_not_a_real_field", "x"),
        )
        code, doc = self.check()
        self.assertEqual(code, 5)
        self.assertValid(CONSOLIDATED_CHECK_SCHEMA, doc, "consolidated-check")
        reason = next(f["reason"] for f in doc["findings"] if f["gate_id"] == "P0")
        self.assertIn("unexpected property 'zzz_not_a_real_field'", reason)
        self.assertNotIn("did you mean", reason)

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
        # chainlink #75: no descriptor means nothing is pinned, so check
        # fails closed on gate integrity too -- exit 5. The absent-vs-
        # invalid distinction the test exists for is untouched: the only
        # finding is the workspace-level gate-integrity one, and no
        # descriptor-level finding is produced.
        self.assertEqual(code, 5)
        self.assertValid(CONSOLIDATED_CHECK_SCHEMA, doc, "consolidated-check")
        self.assertIsNone(doc["next_action"])
        self.assertEqual(len(doc["findings"]), 1)
        self.assertEqual(doc["findings"][0]["gate_id"], "gate-integrity")
        self.assertEqual(doc["findings"][0]["subject"], "(workspace)")
        self.assertEqual(doc["findings"][0]["reason"], "no project descriptor present")

    def test_unparseable_descriptor_is_reported_as_a_finding(self):
        self.descriptor_path.write_text("{not json")
        code, doc = self.check()
        # chainlink #75: exit 5 -- the unparseable descriptor is both an
        # invalid input and an unverifiable gate-integrity pin list.
        self.assertEqual(code, 5)
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
        # chainlink #75: exit 5 (gate_integrity_failed outranks
        # invalid_input); the diagnostic content is unchanged.
        self.assertEqual(code, 5)
        reason = doc["findings"][0]["reason"]
        self.assertIn("closure_kinds", reason)
        self.assertIn("closure_kind", reason)  # the permitted alternative the pilot wanted
        self.assertIn("permitted:", reason)

    def test_verifier_policy_closure_kind_is_rejected_with_the_verifier_enum(self):
        """The pilot's second probe shape: `verifier_policy.closure_kind`
        is not the field -- the diagnostic must say what verifier_policy
        does permit, not just "invalid".

        chainlink #104 makes this a strictly better answer than before.
        While `verifier_policy` was open, `closure_kind` was accepted as
        a key and failed only on its VALUE, so the line said "'deductive'
        is not one of ['kani','creusot','verus']" -- which named neither
        the field the pilot wanted nor the fact that the key was wrong at
        all. It is now rejected as a key, and the line names both: the
        permitted set, and `closure_kind`'s real home at the top level.
        """
        self.write_example_descriptor(
            "project-descriptor.greenfield.example.json",
            lambda d: d["verifier_policy"].__setitem__("closure_kind", "deductive"),
        )
        code, doc = self.check()
        # chainlink #75: exit 5 (gate_integrity_failed outranks
        # invalid_input); the diagnostic content is unchanged.
        self.assertEqual(code, 5)
        reason = doc["findings"][0]["reason"]
        self.assertIn("unexpected property 'closure_kind'", reason)
        self.assertIn("permitted: clusters, default, supporting", reason)
        # The near-miss machinery points at where the key the pilot
        # actually wanted lives, rather than at a verifier enum it never
        # asked for.
        self.assertIn("did you mean '$.closure_kind'", reason)

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

    def test_partial_closure_intent_is_read_back_by_status(self):
        """chainlink #85: a project that knows it cannot achieve full
        deductive closure declares `partial` intent in the descriptor; the
        status echo must carry it and the document must still validate,
        instead of the echo rejecting the value the descriptor accepted."""
        descriptor = self.write_example_descriptor(
            "project-descriptor.port.example.json",
            lambda d: d.__setitem__("closure_kind", "partial"),
        )
        self.assertEqual(descriptor["closure_kind"], "partial")
        _, doc = self.status()
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")
        self.assertEqual(doc["descriptor"]["state"], "present-valid")
        self.assertEqual(doc["descriptor"]["closure_kind"], "partial")

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
        # The rendered descriptor declares
        # compatibility_policy.reliance_policy_path (docs/reliance-policy.md);
        # a real workspace has that file, so the fixture writes it too --
        # otherwise the #78 policy-path finding fires alongside the #67
        # placeholder finding and the count below is 2, not 1.
        self.write(
            "docs/reliance-policy.md",
            (ROOT / "docs" / "reliance-policy.template.md").read_text().replace(
                "# Reliance policy — `<project name>`", "# Reliance policy — `myproj`", 1
            ),
        )
        code, doc = self.check()
        self.assertValid(CONSOLIDATED_CHECK_SCHEMA, doc, "consolidated-check")
        p0 = [f for f in doc["findings"] if f["gate_id"] == "P0"]
        self.assertEqual(len(p0), 1)
        self.assertIn("review.reviewer", p0[0]["reason"])


class DescriptorPolicyPathTest(WorkspaceFixture):
    """chainlink #78: the descriptor's
    `compatibility_policy.reliance_policy_path` was read by nothing -- a
    pointer naming a nonexistent file, or a path outside the project root,
    validated as `present-valid` and left `check` exit 0. The field is the
    project's own declaration of where its policy lives, so `check`
    validates it semantically (existence + containment) and fails closed,
    and the accept commands default to it rather than second-guessing it.
    """

    def test_a_declared_policy_path_that_does_not_exist_fails_check(self):
        self.init_workspace()
        descriptor = json.loads(self.descriptor_path.read_text())
        descriptor["compatibility_policy"]["reliance_policy_path"] = "docs/no-such-policy.md"
        self.descriptor_path.write_text(json.dumps(descriptor))
        code, doc = self.check()
        # init_workspace pins the gate hashes, so the policy-path finding
        # is the run's own signal: exit 1 (blocking findings), not 5 (the
        # gate-integrity fail-closed code an unpinned workspace would give).
        self.assertEqual(code, 1)
        self.assertIn("blocking_findings", doc["result"]["conditions"])
        findings = [f for f in doc["findings"] if "reliance_policy_path" in f["reason"]]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "high")
        self.assertEqual(findings[0]["subject"], "docs/no-such-policy.md")
        self.assertIn("does not exist", findings[0]["reason"])

    def test_a_declared_policy_path_outside_the_project_root_fails_check(self):
        self.init_workspace()
        descriptor = json.loads(self.descriptor_path.read_text())
        descriptor["compatibility_policy"]["reliance_policy_path"] = "/etc/hostname"
        self.descriptor_path.write_text(json.dumps(descriptor))
        code, doc = self.check()
        self.assertEqual(code, 1)
        findings = [f for f in doc["findings"] if "reliance_policy_path" in f["reason"]]
        self.assertEqual(len(findings), 1)
        self.assertIn("outside the project root", findings[0]["reason"])

    def test_a_declared_policy_path_that_exists_inside_the_root_is_clean(self):
        """The happy path: the declared policy document exists (init
        installed it), so no policy-path finding fires."""
        self.init_workspace()
        code, doc = self.check()
        self.assertFalse(
            [f for f in doc["findings"] if "reliance_policy_path" in f["reason"]],
            "a resolvable in-root policy path must not produce a finding",
        )


class VerifierPolicyEchoTest(WorkspaceFixture):
    """chainlink #76: no command in the v1.0 surface reported the effective
    verifier policy -- `status --json` and `check --json` contained zero
    occurrences of the string "verifier" in either the valid or the invalid
    case, so the field a pilot is named after was observable only by
    opening the descriptor file. `status --json` now reads the policy back
    in the same shape the pipeline consumes it (gate g9 resolves
    `clusters.get(cluster, policy["default"])`): `default`, the per-cluster
    overrides under `clusters`, and the declared multi-verifier
    composition under `supporting`.

    chainlink #104 removed the reason this echo had to normalize: while
    `verifier_policy` was an open object, "every key that is not `default`
    or `supporting`" was the only available definition of a per-cluster
    override, so the typo #76's mitigation was built to expose (`defualt`)
    was indistinguishable here from a real cluster name. The echo now
    reads the declared `clusters` object."""

    def write_example_descriptor(self, example: str, mutate) -> dict:
        descriptor = json.loads((EXAMPLES / example).read_text())
        mutate(descriptor)
        self.descriptor_path.write_text(json.dumps(descriptor))
        return descriptor

    def test_status_echoes_the_effective_verifier_policy(self):
        descriptor = self.write_descriptor()
        descriptor["verifier_policy"] = {
            "default": "verus",
            "clusters": {"crypto-mixed": "kani"},
            "supporting": ["kani"],
        }
        self.descriptor_path.write_text(json.dumps(descriptor))
        _, doc = self.status()
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")
        self.assertEqual(doc["descriptor"]["state"], "present-valid")
        self.assertEqual(
            doc["descriptor"]["verifier_policy"],
            {
                "default": "verus",
                "clusters": {"crypto-mixed": "kani"},
                "supporting": ["kani"],
            },
        )

    def test_status_echoes_the_declared_policy(self):
        self.write_descriptor()
        _, doc = self.status()
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")
        # The greenfield example declares one per-cluster override
        # ("clusters": {"scheduling": "kani"}) -- the echo carries it in
        # the shape gate g9 consumes.
        self.assertEqual(
            doc["descriptor"]["verifier_policy"],
            {"default": "creusot", "clusters": {"scheduling": "kani"}, "supporting": []},
        )

    def test_status_echoes_an_empty_clusters_map_when_none_is_declared(self):
        """`clusters` is optional, so a single-verifier project declares
        only `default`. The echo reports that as an empty map rather than
        omitting the key or, as it would have before #104, folding some
        other key into it -- so `clusters` always means what it says."""
        self.write_example_descriptor(
            "project-descriptor.port.example.json",
            lambda d: d.__setitem__("verifier_policy", {"default": "creusot"}),
        )
        _, doc = self.status()
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")
        self.assertEqual(doc["descriptor"]["state"], "present-valid")
        self.assertEqual(
            doc["descriptor"]["verifier_policy"],
            {"default": "creusot", "clusters": {}, "supporting": []},
        )

    def test_a_misspelled_key_no_longer_reaches_the_echo_as_a_cluster(self):
        """Defect 1's hazard, closed (chainlink #104). #76's mitigation was
        to make the typo visible under `clusters` as an override for a
        cluster literally named 'defualt' -- but the descriptor still
        validated, so `status` reported `present-valid` and `check` exited
        0 on a policy whose default was not what the user wrote. The
        descriptor is now `present-invalid`, so there is no effective
        policy to report, and the finding names the key it looks like."""
        self.write_example_descriptor(
            "project-descriptor.greenfield.example.json",
            lambda d: d["verifier_policy"].__setitem__("defualt", "kani"),
        )
        _, doc = self.status()
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")
        self.assertEqual(doc["descriptor"]["state"], "present-invalid")
        self.assertIsNone(doc["descriptor"]["verifier_policy"])
        summaries = [f["summary"] for f in doc["open_findings"]]
        self.assertTrue(
            any("unexpected property 'defualt'" in s for s in summaries),
            summaries,
        )
        self.assertTrue(any("did you mean 'default'?" in s for s in summaries), summaries)

    def test_status_verifier_policy_is_null_for_an_invalid_descriptor(self):
        self.write_example_descriptor(
            "project-descriptor.port.example.json",
            lambda d: d["verifier_policy"].__setitem__("default", "creusot-rust"),
        )
        _, doc = self.status()
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")
        self.assertEqual(doc["descriptor"]["state"], "present-invalid")
        self.assertIsNone(doc["descriptor"]["verifier_policy"])

    def test_status_verifier_policy_is_omitted_for_an_absent_descriptor(self):
        """Same discipline as mode/schema_version/closure_kind: the echo
        is present (null) when the descriptor file exists but is invalid,
        and omitted when there is no descriptor at all."""
        _, doc = self.status()
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")
        self.assertEqual(doc["descriptor"]["state"], "absent")
        self.assertNotIn("verifier_policy", doc["descriptor"])

    def test_status_text_distinguishes_present_invalid_from_absent(self):
        """chainlink #76 Defect 6: both states printed 'no project
        descriptor present'. The human-readable status line and the
        gate-integrity detail must tell a descriptor that is present but
        invalid from one that is not there at all."""
        code, out = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("descriptor: absent", out)
        self.assertIn("no project descriptor present", out)
        self.write_example_descriptor(
            "project-descriptor.port.example.json",
            lambda d: d["verifier_policy"].__setitem__("default", "creusot-rust"),
        )
        code, out = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("descriptor: present-invalid", out)
        self.assertIn("present but invalid", out)
        self.assertIn("$.verifier_policy.default", out)
        self.assertNotIn("descriptor: absent", out)


class BoundaryG2PlusSearchRootTest(WorkspaceFixture):
    """chainlink #81: the G2+ applies_to check was permanently degraded to
    "applies_to unverifiable -- no --specs-search-root given" on every
    boundary contract, because `_run_standalone_validators` (the path
    `status` and `check` both take) called
    `validate_boundary_contracts.validate(workspace)` with no
    specs_search_root, and the descriptor's crates[].specs_search_root --
    already in the schema, already required, already filled in real roots --
    was read by nothing on this path. The check can now never succeed, so a
    wrong or dangling constraint id is indistinguishable from a correct one
    in every command reachable from the CLI."""

    def _write_concept_spec(self, constraint_id="C003", applies_to=("pop_ready",)):
        self.write(
            "crates/a/specs/task_queue.json",
            {
                "concept": "TaskQueue",
                "constraints": [
                    {
                        "id": constraint_id,
                        "english": "pop_ready returns None only when no task has deadline <= now",
                        "logic": "true",
                        "kind": "postcondition",
                        "applies_to": list(applies_to),
                    }
                ],
            },
        )

    def test_status_resolves_callee_guarantees_against_the_descriptor_search_root(self):
        """The exact reproduction of chainlink #81: with the descriptor
        declaring crates[].specs_search_root and a concept spec present,
        `status --json` must NOT report 'applies_to unverifiable -- no
        --specs-search-root given' -- the id resolves and the applies_to
        check runs."""
        self.write_descriptor()
        self._write_concept_spec()
        self.write("crates/a/specs/_boundaries/scheduler_dispatch__to__task_queue_pop_ready.json", BOUNDARY)
        _, doc = self.status()
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")
        g2_findings = [f for f in doc["open_findings"] if f["gate_id"] == "G2+"]
        self.assertEqual(g2_findings, [], [f["summary"] for f in g2_findings])

    def test_check_resolves_callee_guarantees_against_the_descriptor_search_root(self):
        """Same fix on the `check` path -- the consolidated check must not
        carry the permanent info finding either."""
        self.write_descriptor()
        self._write_concept_spec()
        self.write("crates/a/specs/_boundaries/scheduler_dispatch__to__task_queue_pop_ready.json", BOUNDARY)
        code, doc = self.check()
        self.assertValid(CONSOLIDATED_CHECK_SCHEMA, doc, "consolidated-check")
        g2_findings = [f for f in doc["findings"] if f["gate_id"] == "G2+"]
        self.assertEqual(g2_findings, [], [f["reason"] for f in g2_findings])

    def test_a_dangling_constraint_id_is_an_error_not_an_unverifiable_note(self):
        """chainlink #81 direction 3: when the search root IS available and
        the spec resolved but the id matches no constraint in it, that is a
        real dangling reference -- a defect in the boundary contract, not an
        unverifiable note. Previously this was info-severity, so `check`
        exited 0 and a wrong id was indistinguishable from a correct one."""
        self.write_descriptor()
        self._write_concept_spec(constraint_id="C003")
        self.write(
            "crates/a/specs/_boundaries/scheduler_dispatch__to__task_queue_pop_ready.json",
            {**BOUNDARY, "callee_guarantees": ["TaskQueue.C999"]},
        )
        _, doc = self.status()
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")
        g2_findings = [f for f in doc["open_findings"] if f["gate_id"] == "G2+"]
        self.assertEqual(len(g2_findings), 1, [f["summary"] for f in g2_findings])
        self.assertEqual(g2_findings[0]["severity"], "high")
        self.assertIn("dangling reference", g2_findings[0]["summary"])

    def test_an_applies_to_mismatch_is_an_error_when_the_search_root_resolves(self):
        """The applies_to check itself: a constraint that does not apply to
        the boundary's callee method is a real error once the search root is
        available (this already worked via `cmd_validate`; it must also work
        through the `status`/`check` path)."""
        self.write_descriptor()
        self._write_concept_spec(applies_to=("other_method",))
        self.write("crates/a/specs/_boundaries/scheduler_dispatch__to__task_queue_pop_ready.json", BOUNDARY)
        _, doc = self.status()
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")
        g2_findings = [f for f in doc["open_findings"] if f["gate_id"] == "G2+"]
        self.assertEqual(len(g2_findings), 1, [f["summary"] for f in g2_findings])
        self.assertEqual(g2_findings[0]["severity"], "high")
        self.assertIn("applies_to", g2_findings[0]["summary"])

    def test_without_a_descriptor_the_check_still_degrades_to_info(self):
        """The fallback is unchanged: with no descriptor at all there is no
        search root to resolve against, so the applies_to check degrades to
        its info-severity 'unverifiable' finding -- a missing descriptor is
        not a boundary-contract defect, and every other command already
        reports it."""
        self.write("crates/a/specs/_boundaries/scheduler_dispatch__to__task_queue_pop_ready.json", BOUNDARY)
        _, doc = self.status()
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")
        g2_findings = [f for f in doc["open_findings"] if f["gate_id"] == "G2+"]
        self.assertEqual(len(g2_findings), 1, [f["summary"] for f in g2_findings])
        self.assertEqual(g2_findings[0]["severity"], "info")
        self.assertIn("no --specs-search-root given", g2_findings[0]["summary"])


class ArtifactDirectoryConsistencyTest(WorkspaceFixture):
    """`project_state.artifact_dirs()` names the locations the pipeline
    writes its own artifacts into, and scripts/write_set.py's protected-root
    enforcement (chainlink #103) is defined by it: a protected file the
    pipeline does not own is an audit line, and a protected file in a
    location it does own that the artifact system does not recognize is a
    violation. That only holds while the registry and
    `_discover_artifact_files` -- the discovery that decides what an
    artifact IS -- agree in both directions.

    The registry cannot drive the discovery (the discovery keeps
    per-location rules: the `*.degradation.json` split in `specs/_closure`,
    the work-package discriminator in `ci/manifest`), so the agreement is
    asserted rather than refactored into. Drift in either direction is the
    hole chainlink #103 reports, reopened: a kind the registry forgot
    becomes silently un-audited, and a kind it invents becomes a violation
    for content the pipeline legitimately writes.
    """

    def descriptor(self) -> dict:
        return json.loads(self.descriptor_path.read_text())

    def write(self, relative: str, data: str = "{}") -> Path:
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
        return path

    def discovered(self) -> dict[str, str]:
        """relative path -> artifact kind, for every artifact on disk."""
        return {
            path.relative_to(self.workspace).as_posix(): kind
            for kind, path in project_state._discover_artifact_files(
                self.workspace, self.descriptor()
            )
        }

    def planted(self) -> list[tuple[str, str]]:
        """Every (relative path, artifact kind) the registry implies, with
        `specs/_closure` planted under both filenames because it is the one
        location whose kind depends on the filename."""
        planted: list[tuple[str, str]] = []
        for rel_dir, kind in project_state.artifact_dirs(self.workspace, self.descriptor()):
            if kind == "closure":
                planted.append((f"{rel_dir}/x.json", "closure"))
                planted.append((f"{rel_dir}/cluster.degradation.json", "degradation-record"))
            else:
                planted.append((f"{rel_dir}/x.json", kind))
        return planted

    def test_every_registered_artifact_directory_is_one_discovery_visits(self):
        self.write_descriptor(crates=("crates/a",))
        for rel_path, kind in self.planted():
            with self.subTest(directory=rel_path.rsplit("/", 1)[0]):
                self.write(rel_path, json.dumps(ARTIFACT_BODIES[kind]))
                self.assertEqual(self.discovered().get(rel_path), kind)

    def test_every_directory_discovery_visits_is_registered(self):
        self.write_descriptor(crates=("crates/a",))
        registered = {
            rel_dir
            for rel_dir, _kind in project_state.artifact_dirs(self.workspace, self.descriptor())
        }
        for rel_path, kind in self.planted():
            self.write(rel_path, json.dumps(ARTIFACT_BODIES[kind]))
        visited = {rel.rsplit("/", 1)[0] for rel in self.discovered()}
        self.assertTrue(visited)
        self.assertEqual(visited - registered, set())

    def test_a_declared_crates_kind_dirs_follow_its_crate_dir(self):
        """The per-crate half is derived from _CRATE_ARTIFACT_DIRS and the
        descriptor's own crate_dir -- the two inputs that can disagree."""
        self.write_descriptor(crates=("crates/a", "crates/b"))
        registered = {
            rel_dir
            for rel_dir, _kind in project_state.artifact_dirs(self.workspace, self.descriptor())
        }
        self.assertIn("crates/a/specs/_boundaries", registered)
        self.assertIn("crates/b/specs/_witnesses", registered)
        self.assertNotIn("crates/c/specs/_boundaries", registered)
        for crate_dir in ("crates/a", "crates/b"):
            self.write(
                f"{crate_dir}/specs/_boundaries/x.json", json.dumps(ARTIFACT_BODIES["boundary"])
            )
        self.assertIn("crates/a/specs/_boundaries/x.json", self.discovered())
        self.assertIn("crates/b/specs/_boundaries/x.json", self.discovered())

    def test_an_empty_artifact_directory_is_still_the_pipelines_own(self):
        """`ci/manifest/` holding only the ownership manifest is still the
        directory the pipeline writes work packages into -- which is exactly
        why a hand-placed `ci/manifest/PROBE.json` is a decidable
        protected write rather than unattributable (chainlink #103)."""
        self.write_descriptor(crates=("crates/a",))
        registered = {
            rel_dir
            for rel_dir, _kind in project_state.artifact_dirs(self.workspace, self.descriptor())
        }
        self.assertIn("ci/manifest", registered)
        self.assertEqual(self.discovered(), {})


if __name__ == "__main__":
    unittest.main()
