import hashlib
import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import generate_promotion_receipt  # noqa: E402
from generate_promotion_receipt import (  # noqa: E402
    PromotionReceiptError,
    accept_promotion,
    build_receipt,
    collect_review_blocks,
    compute_artifact_manifest,
    compute_promotion_id,
    compute_schema_versions,
    extract_policy_version,
    record_ruling,
    required_witness_paths,
    ruling_gaps,
)
from review_checkpoint import ApprovalRefused  # noqa: E402
from review_checkpoint import SKIP_VALIDATION  # noqa: E402
from review_checkpoint import approve  # noqa: E402
from review_checkpoint import review_provenance_gaps  # noqa: E402
from review_checkpoint import stage_draft  # noqa: E402
from validate_promotion_receipt import RequiredWitnessError  # noqa: E402
from validate_promotion_receipt import load_validator, validate_file  # noqa: E402
from validate_witness import witness_promotion_digest  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))
from test_validate_evidence import load_valid as valid_evidence_record  # noqa: E402
from test_validate_witness import concept_spec, witness_spec  # noqa: E402

DESCRIPTOR = {
    "schema_version": "1.0",
    "project": {"name": "repro", "crate_naming_convention": "^repro-[a-z]+"},
    "mode": "greenfield",
    "crates": [
        {
            "crate_dir": "crates/scheduler",
            "contracts_crate": "contracts",
            "specs_search_root": "crates/scheduler/specs",
        }
    ],
    "verifier_policy": {"default": "creusot"},
    "compatibility_policy": {"reliance_policy_path": "docs/reliance-policy.md"},
    "write_set": {"allowed_roots": [], "protected_roots": []},
    "gate_integrity": [],
    "review": {"reviewer": "repro", "reviewed_at": "2026-08-27"},
}

VALID_BOUNDARY = {
    "schema_version": "1.0",
    "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
    "caller": {"concept": "Scheduler", "method": "dispatch"},
    "callee": {"concept": "TaskQueue", "method": "pop_ready"},
    "callee_guarantees": ["TaskQueue.C003"],
    "review": {"reviewer": "alice", "reviewed_at": "2026-08-30"},
}

VALID_INTERACTION = {
    "schema_version": "1.0",
    "interaction_id": "I-SCHED-TQ-001",
    "caller": {"concept": "Scheduler", "method": "dispatch"},
    "callee": {"concept": "TaskQueue", "method": "pop_ready"},
    "edge_class": ["pure-data-type-reference"],
    "eligibility": "inform",
    "rationale": "informational only",
    "protocol_class": "pairwise",
    "realization": {
        "requirement": "required",
        "config_scope": {"target": "x86_64-unknown-linux-gnu", "features": [], "cfg": []},
    },
    "review": {"reviewer": "alice", "reviewed_at": "2026-08-30"},
}

VALID_EXEMPTION = {
    "schema_version": "1.0",
    "interaction_id": "I-SCHED-TQ-002",
    "rationale": "Prototype scaffolding boundary, tracked for removal (chainlink:#41)",
    "review": {"reviewer": "alice", "reviewed_at": "2026-08-30"},
}

POLICY_TEXT = (
    "# Reliance policy\n\n"
    "Schema version this policy targets: `1.0`.\n"
    "Owner: `platform-team`.\n"
    "Policy version: `reliance-policy@1.2`\n"
)


def _write_descriptor(workspace: Path, descriptor: dict = DESCRIPTOR) -> Path:
    path = workspace / "project-descriptor.json"
    path.write_text(json.dumps(descriptor))
    return path


def _write_json(workspace: Path, relative: str, data: dict) -> str:
    path = workspace / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return relative


def _record_approval(
    workspace: Path,
    artifact_path: str,
    review: dict,
    *,
    reviewer=None,
    reviewed_at=None,
    classification: str = "new",
    entry_target_path: str | None = None,
) -> Path:
    """Append the approval audit entry review_checkpoint.approve() would
    have written when this fixture artifact was approved (chainlink
    #82), so tests whose subject is NOT the provenance guard can keep
    exercising it as a fixture precondition. The sanctioned write path
    itself is exercised end to end by test_pipeline.py's
    CmdAcceptPromotionProvenanceIntegrationTest (real `approve` through
    pipeline.main()), and the guard's own refusal cases live in
    ReviewProvenanceTest below -- this is setup, not a bypass under
    test."""
    log = workspace / "ci" / "results" / "review_log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "target_path": entry_target_path or str(workspace / artifact_path),
        "classification": classification,
        "reviewer": review["reviewer"] if reviewer is None else reviewer,
        "reviewed_at": review["reviewed_at"] if reviewed_at is None else reviewed_at,
        "validation": "checked",
        "logged_at": datetime.now(timezone.utc).isoformat(),
    }
    with log.open("a") as stream:
        stream.write(json.dumps(entry) + "\n")
    return log


@contextmanager
def _failing_audit_fsync():
    """Fail the FIRST os.fsync accept_promotion() performs -- the audit
    append's own fsync -- then let the rollback's fsync through, so the
    prepared-entry rollback runs for real (chainlink #82 keeps #45's
    transactional coverage honest: the original repro put a directory
    in place of review_log, which now trips the provenance READ first
    and is a different, earlier refusal)."""
    real_fsync = generate_promotion_receipt.os.fsync
    calls = {"count": 0}

    def flaky_fsync(fd: int):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError(28, "No space left on device")
        return real_fsync(fd)

    with mock.patch.object(generate_promotion_receipt.os, "fsync", flaky_fsync):
        yield


class ComputeArtifactManifestTest(unittest.TestCase):
    """compute_artifact_manifest(): real file discovery and hashing, not
    a stub -- chainlink #45's own scope explicitly asks for hashes
    computed against real file bytes, not asserted equal to a
    hand-written value."""

    def test_computes_real_hashes_not_a_hand_written_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "docs").mkdir()
            content = b"reliance policy contents\n"
            (workspace / "docs" / "reliance-policy.md").write_bytes(content)
            manifest = compute_artifact_manifest(workspace, ["docs/reliance-policy.md"])
        expected_hash = "sha256:" + hashlib.sha256(content).hexdigest()
        self.assertEqual(manifest, [{"path": "docs/reliance-policy.md", "hash": expected_hash}])

    def test_preserves_input_order_and_covers_multiple_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "a.json").write_bytes(b"{}")
            (workspace / "b.json").write_bytes(b"{}")
            manifest = compute_artifact_manifest(workspace, ["b.json", "a.json"])
        self.assertEqual([entry["path"] for entry in manifest], ["b.json", "a.json"])

    def test_rejects_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PromotionReceiptError):
                compute_artifact_manifest(Path(tmp), ["/etc/passwd"])

    def test_rejects_dotdot_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PromotionReceiptError):
                compute_artifact_manifest(Path(tmp), ["../outside.json"])

    def test_rejects_symlink_escaping_the_workspace_after_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            outside = Path(tmp) / "outside.json"
            outside.write_text("{}")
            (workspace / "escape.json").symlink_to(outside)
            with self.assertRaises(PromotionReceiptError):
                compute_artifact_manifest(workspace, ["escape.json"])

    def test_rejects_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PromotionReceiptError):
                compute_artifact_manifest(Path(tmp), ["does/not/exist.json"])

    def test_rejects_aliased_duplicate_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "a.json").write_text("{}")
            with self.assertRaises(PromotionReceiptError):
                compute_artifact_manifest(workspace, ["a.json", "./a.json"])


class ComputePromotionIdTest(unittest.TestCase):
    """compute_promotion_id(): mechanical, derived from `cluster` alone.
    External review, high severity: the previous version accepted
    promotion_id as a free-form CLI argument -- a value completely
    unrelated to the cluster ("PROM-WRONG-999") passed the real validator
    with zero findings. That input no longer exists; this is the
    replacement."""

    def test_derives_from_cluster_deterministically(self):
        self.assertEqual(compute_promotion_id("scheduling"), "PROM-SCHEDULING-001")

    def test_is_pure_and_repeatable(self):
        self.assertEqual(compute_promotion_id("scheduling"), compute_promotion_id("scheduling"))

    def test_different_clusters_get_different_ids(self):
        self.assertNotEqual(compute_promotion_id("scheduling"), compute_promotion_id("mcmc-chain"))

    def test_hyphenated_cluster_produces_a_schema_valid_id(self):
        promotion_id = compute_promotion_id("mcmc-chain")
        self.assertRegex(promotion_id, r"^PROM-[A-Z][A-Z0-9-]*-[0-9]{3}$")
        self.assertEqual(promotion_id, "PROM-MCMC-CHAIN-001")


class ComputeSchemaVersionsTest(unittest.TestCase):
    """compute_schema_versions(): every accepted artifact at a
    descriptor-derived CANONICAL directory is validated -- against its
    own real validator, WITH real cross-file context -- before its
    schema_version is trusted. External review findings across two
    rounds:

      round 3, high: a malformed boundary contract containing only
      {"schema_version": "999.0", "garbage": true} previously produced a
      receipt declaring "boundary": "999.0" with zero findings.

      round 4, high: even after round 3's fix, cross-file context
      (interactions_by_id, R2 coverage, G15 coverage) was never
      supplied, so a *reviewed* exemption referencing a nonexistent
      interaction still passed -- only the schema's own required
      `review` field needed satisfying by hand.

      round 4, medium: kind was matched by directory *basename* alone, so
      a schema-valid boundary at `junk/_boundaries/<id>.json` -- nowhere
      near the descriptor's real boundary directory -- still had its
      schema_version trusted."""

    def test_reads_the_real_schema_version_from_a_genuinely_valid_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            path = _write_json(
                workspace, f"crates/scheduler/specs/_boundaries/{VALID_BOUNDARY['boundary_id']}.json",
                VALID_BOUNDARY,
            )
            versions = compute_schema_versions(workspace, DESCRIPTOR, [path])
        self.assertEqual(versions, {"boundary": "1.0"})

    def test_malformed_artifact_at_a_kind_directory_is_refused_not_silently_trusted(self):
        """The round-3 repro: {"schema_version": "999.0", "garbage":
        true} at the real canonical _boundaries/ directory must be
        refused outright, not silently excluded (it would still be in
        artifact_manifest) and not trusted for its self-declared
        schema_version."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            path = _write_json(
                workspace,
                f"crates/scheduler/specs/_boundaries/{VALID_BOUNDARY['boundary_id']}.json",
                {"schema_version": "999.0", "garbage": True},
            )
            with self.assertRaises(PromotionReceiptError):
                compute_schema_versions(workspace, DESCRIPTOR, [path])

    def test_artifact_at_a_non_canonical_directory_sharing_a_kind_basename_is_ignored(self):
        """The round-4 repro: a fully schema-valid boundary at
        junk/_boundaries/<id>.json -- not the descriptor's real crate
        boundary directory -- must not have its schema_version trusted.
        It also must not be treated as an error (it's simply not a
        recognized kind instance, same as any other opaque accepted
        file) -- compute_schema_versions() only fails when there is NO
        recognized kind anywhere in the accepted set at all."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            stray_path = _write_json(
                workspace, f"junk/_boundaries/{VALID_BOUNDARY['boundary_id']}.json", VALID_BOUNDARY
            )
            real_path = _write_json(
                workspace,
                f"crates/scheduler/specs/_boundaries/{VALID_BOUNDARY['boundary_id']}.json",
                VALID_BOUNDARY,
            )
            versions = compute_schema_versions(workspace, DESCRIPTOR, [stray_path, real_path])
        self.assertEqual(versions, {"boundary": "1.0"})

    def test_reviewed_exemption_referencing_a_nonexistent_interaction_is_refused(self):
        """The round-4 repro, verbatim: a reviewed exemption naming an
        interaction_id that doesn't exist anywhere in the crate's real
        _interactions/ directory must be refused -- G2's cross-reference
        now runs for real against the crate's actual promoted
        interactions, not degraded to a non-blocking info finding."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            # No _interactions/ directory at all -- I-SCHED-TQ-002 cannot
            # possibly exist.
            path = _write_json(
                workspace, "crates/scheduler/specs/_exemptions/I-SCHED-TQ-002.json", VALID_EXEMPTION
            )
            with self.assertRaises(PromotionReceiptError):
                compute_schema_versions(workspace, DESCRIPTOR, [path])

    def test_exemption_referencing_a_real_boundary_required_interaction_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            boundary_required_interaction = dict(VALID_INTERACTION)
            boundary_required_interaction["interaction_id"] = "I-SCHED-TQ-002"
            boundary_required_interaction["edge_class"] = ["stateful"]
            boundary_required_interaction["eligibility"] = "boundary-required"
            boundary_required_interaction["reliances"] = [{
                "obligation_id": "TaskQueue.C003",
                "required_assurance": {
                    "required_claims": ["postcondition-holds"],
                    "accepted_evidence_kinds": ["creusot-deductive-check"],
                    "minimum_scope": {"input_domain": "queue_len_le_8"},
                    "trust_policy": {"assumptions_allowed": []},
                },
            }]
            _write_json(
                workspace, "crates/scheduler/specs/_interactions/I-SCHED-TQ-002.json",
                boundary_required_interaction,
            )
            exemption_path = _write_json(
                workspace, "crates/scheduler/specs/_exemptions/I-SCHED-TQ-002.json", VALID_EXEMPTION
            )
            versions = compute_schema_versions(workspace, DESCRIPTOR, [exemption_path])
        self.assertEqual(versions, {"exemption": "1.0"})

    def test_non_kind_artifacts_contribute_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            path = _write_json(
                workspace,
                f"crates/scheduler/specs/_boundaries/{VALID_BOUNDARY['boundary_id']}.json",
                VALID_BOUNDARY,
            )
            (workspace / "docs").mkdir()
            (workspace / "docs" / "reliance-policy.md").write_text("# policy\n")
            versions = compute_schema_versions(workspace, DESCRIPTOR, [path, "docs/reliance-policy.md"])
        self.assertEqual(versions, {"boundary": "1.0"})

    def test_multiple_kinds_are_all_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            boundary_path = _write_json(
                workspace,
                f"crates/scheduler/specs/_boundaries/{VALID_BOUNDARY['boundary_id']}.json",
                VALID_BOUNDARY,
            )
            interaction_path = _write_json(
                workspace, "crates/scheduler/specs/_interactions/I-SCHED-TQ-001.json", VALID_INTERACTION
            )
            versions = compute_schema_versions(workspace, DESCRIPTOR, [boundary_path, interaction_path])
        self.assertEqual(versions, {"boundary": "1.0", "interaction": "1.0"})

    def test_disagreeing_versions_for_the_same_kind_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            first_path = _write_json(
                workspace,
                f"crates/scheduler/specs/_boundaries/{VALID_BOUNDARY['boundary_id']}.json",
                VALID_BOUNDARY,
            )
            second = {
                "schema_version": "2.0",
                "boundary_id": "scheduler_dispatch__to__task_queue_enqueue",
                "caller": {"concept": "Scheduler", "method": "dispatch"},
                "callee": {"concept": "TaskQueue", "method": "enqueue"},
                "callee_guarantees": ["TaskQueue.C003"],
                "review": {"reviewer": "alice", "reviewed_at": "2026-08-30"},
            }
            second_path = _write_json(
                workspace, "crates/scheduler/specs/_boundaries/scheduler_dispatch__to__task_queue_enqueue.json",
                second,
            )
            with self.assertRaises(PromotionReceiptError):
                compute_schema_versions(workspace, DESCRIPTOR, [first_path, second_path])

    def test_no_recognized_kind_anywhere_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "docs").mkdir()
            (workspace / "docs" / "reliance-policy.md").write_text("# policy\n")
            with self.assertRaises(PromotionReceiptError):
                compute_schema_versions(workspace, DESCRIPTOR, ["docs/reliance-policy.md"])


class ExtractPolicyVersionTest(unittest.TestCase):
    """extract_policy_version(): mechanically read from the accepted
    policy document's own content, requiring exactly one marker line.
    External review findings across two rounds: round 3, high -- the
    previous version only checked that a same-named file existed, so
    "reliance-policy@9.9" passed with docs/reliance-policy.md present
    regardless of what the file actually said. round 4, low -- once
    content was read, the first regex match was used, so a document
    declaring two different "Policy version:" lines was silently
    accepted as whichever came first."""

    def test_reads_the_real_marker_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "docs").mkdir()
            (workspace / "docs" / "reliance-policy.md").write_text(POLICY_TEXT)
            version = extract_policy_version(workspace, "docs/reliance-policy.md")
        self.assertEqual(version, "reliance-policy@1.2")

    def test_missing_marker_is_a_hard_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "docs").mkdir()
            (workspace / "docs" / "reliance-policy.md").write_text("# Reliance policy\n\nNo marker here.\n")
            with self.assertRaises(PromotionReceiptError):
                extract_policy_version(workspace, "docs/reliance-policy.md")

    def test_missing_file_is_a_hard_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PromotionReceiptError):
                extract_policy_version(Path(tmp), "docs/reliance-policy.md")

    def test_marker_without_backticks_is_also_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "docs").mkdir()
            (workspace / "docs" / "reliance-policy.md").write_text("Policy version: reliance-policy@2.0\n")
            version = extract_policy_version(workspace, "docs/reliance-policy.md")
        self.assertEqual(version, "reliance-policy@2.0")

    def test_two_conflicting_markers_are_refused(self):
        """The reviewer's exact repro: a policy containing both
        reliance-policy@1.2 and reliance-policy@9.9 must not be silently
        accepted as whichever came first."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "docs").mkdir()
            (workspace / "docs" / "reliance-policy.md").write_text(
                "Policy version: `reliance-policy@1.2`\n"
                "...\n"
                "Policy version: `reliance-policy@9.9`\n"
            )
            with self.assertRaises(PromotionReceiptError):
                extract_policy_version(workspace, "docs/reliance-policy.md")

    def test_two_identical_markers_are_also_refused(self):
        """Exactly one marker is required, full stop -- agreement between
        duplicates doesn't make the document well-formed."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "docs").mkdir()
            (workspace / "docs" / "reliance-policy.md").write_text(
                "Policy version: `reliance-policy@1.2`\n"
                "Policy version: `reliance-policy@1.2`\n"
            )
            with self.assertRaises(PromotionReceiptError):
                extract_policy_version(workspace, "docs/reliance-policy.md")


class BuildReceiptTest(unittest.TestCase):
    def test_defaults_accepted_at_to_today(self):
        receipt = build_receipt(
            promotion_id="PROM-SCHEDULING-001", cluster="scheduling", reviewer="alice",
            policy_version="reliance-policy@1.2", schema_versions={"boundary": "1.0"},
            artifact_manifest=[],
        )
        self.assertEqual(receipt["accepted_at"], datetime.now(timezone.utc).date().isoformat())

    def test_explicit_accepted_at_is_used_verbatim(self):
        receipt = build_receipt(
            promotion_id="PROM-SCHEDULING-001", cluster="scheduling", reviewer="alice",
            policy_version="reliance-policy@1.2", schema_versions={"boundary": "1.0"},
            artifact_manifest=[], accepted_at="2026-01-01",
        )
        self.assertEqual(receipt["accepted_at"], "2026-01-01")

    def test_shape_has_top_level_reviewer_and_no_nested_review_block(self):
        """review_checkpoint.approve() writes a NESTED review block; this
        schema requires TOP-LEVEL reviewer/accepted_at and forbids
        additional properties -- a nested review would be rejected
        outright. build_receipt() must never produce one."""
        receipt = build_receipt(
            promotion_id="PROM-SCHEDULING-001", cluster="scheduling", reviewer="alice",
            policy_version="reliance-policy@1.2", schema_versions={"boundary": "1.0"},
            artifact_manifest=[], accepted_at="2026-01-01",
        )
        self.assertEqual(receipt["schema"], "promotion-receipt/1.0")
        self.assertEqual(receipt["reviewer"], "alice")
        self.assertEqual(receipt["accepted_at"], "2026-01-01")
        self.assertNotIn("review", receipt)


class AcceptPromotionTest(unittest.TestCase):
    """accept_promotion(): the full Stage 4.5 operation, real file
    discovery over a fixture crate tree, end to end against the real
    validator -- chainlink #45's own scope."""

    BOUNDARY_ARTIFACT = f"crates/scheduler/specs/_boundaries/{VALID_BOUNDARY['boundary_id']}.json"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        _write_descriptor(self.workspace)
        self.boundary_bytes = json.dumps(VALID_BOUNDARY).encode()
        _write_json(self.workspace, self.BOUNDARY_ARTIFACT, VALID_BOUNDARY)
        self.policy_bytes = POLICY_TEXT.encode()
        (self.workspace / "docs").mkdir()
        (self.workspace / "docs" / "reliance-policy.md").write_bytes(self.policy_bytes)
        self.review_log = self.workspace / "ci" / "results" / "review_log.jsonl"
        self.artifact_paths = [
            "docs/reliance-policy.md",
            self.BOUNDARY_ARTIFACT,
        ]
        # Chainlink #82's human-ruling gate: every acceptance below also
        # needs a `ratified` ruling over the accepted set. Seeded here so
        # these tests keep exercising whatever else they are about; the
        # gate's own refusals live in HumanRulingGateTest below.
        record_ruling(
            self.workspace,
            self.artifact_paths,
            reviewer="alice",
            verdict="ratified",
            descriptor_path=self.workspace / "project-descriptor.json",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_boundary_approval(self, artifact_path: str | None = None, **overrides) -> Path:
        """Record the boundary fixture's own review block in the approval
        audit log, exactly as review_checkpoint.approve() would have
        written it (chainlink #82): without this, EVERY acceptance in
        this class is refused, since a `review` block with no approval
        entry behind it is what the new guard exists to refuse. Pass
        `artifact_path` to record the entry against some other file."""
        return _record_approval(
            self.workspace,
            self.BOUNDARY_ARTIFACT if artifact_path is None else artifact_path,
            VALID_BOUNDARY["review"],
            **overrides,
        )

    def _accept(self, **overrides):
        kwargs = dict(
            workspace_root=self.workspace,
            cluster="scheduling",
            reviewer="alice",
            policy_path="docs/reliance-policy.md",
            artifact_paths=self.artifact_paths,
            accepted_at="2026-09-05",
            review_log=self.review_log,
        )
        kwargs.update(overrides)
        return accept_promotion(**kwargs)

    def test_writes_a_receipt_with_computed_metadata_and_real_hashes(self):
        self._seed_boundary_approval()
        target_path = self._accept()
        self.assertEqual(target_path, self.workspace / "specs" / "_promotions" / "scheduling.json")
        self.assertTrue(target_path.exists())

        data = json.loads(target_path.read_text())
        expected_boundary_hash = "sha256:" + hashlib.sha256(self.boundary_bytes).hexdigest()
        expected_policy_hash = "sha256:" + hashlib.sha256(self.policy_bytes).hexdigest()
        self.assertEqual(
            data["artifact_manifest"],
            [
                {"path": self.artifact_paths[0], "hash": expected_policy_hash},
                {"path": self.artifact_paths[1], "hash": expected_boundary_hash},
            ],
        )
        self.assertEqual(data["promotion_id"], "PROM-SCHEDULING-001")
        self.assertEqual(data["schema_versions"], {"boundary": "1.0"})
        self.assertEqual(data["policy_version"], "reliance-policy@1.2")
        self.assertEqual(data["reviewer"], "alice")
        self.assertEqual(data["accepted_at"], "2026-09-05")
        self.assertNotIn("review", data)

    def test_generated_receipt_passes_the_real_validator_end_to_end(self):
        self._seed_boundary_approval()
        target_path = self._accept()
        findings = validate_file(target_path, load_validator(), self.workspace)
        self.assertEqual(findings, [], [str(f) for f in findings])

    def test_appends_an_audit_log_entry(self):
        self._seed_boundary_approval()
        size_before = self.review_log.stat().st_size
        target_path = self._accept()
        self.assertTrue(self.review_log.exists())
        # The seeded approval entry is still there, and the promotion's
        # own audit entry was APPENDED after it (append-only, never a
        # rewrite of the trail that proves provenance).
        self.assertGreater(self.review_log.stat().st_size, size_before)
        lines = self.review_log.read_text().strip().splitlines()
        self.assertEqual(len(lines), 2)
        entry = json.loads(lines[-1])
        self.assertEqual(entry["target_path"], str(target_path))
        self.assertEqual(entry["reviewer"], "alice")
        self.assertEqual(entry["reviewed_at"], "2026-09-05")
        self.assertEqual(entry["validation"], "checked")

    def test_review_log_defaults_inside_the_workspace_not_the_process_cwd(self):
        """External review, medium severity: the old default
        (review_checkpoint.REVIEW_LOG_DEFAULT) is cwd-relative, so
        accepting a promotion for an external workspace silently wrote
        its audit trail into whatever repo the CLI happened to run
        from. Omitting review_log entirely must land inside
        workspace_root, never outside it. The approval entry that now
        proves provenance (chainlink #82) is seeded first, so what is
        asserted here is WHERE the promotion's own new entry went --
        not whether the file could start from nothing."""
        default_log = self.workspace / "ci" / "results" / "review_log.jsonl"
        self._seed_boundary_approval()
        workspace_size_before = default_log.stat().st_size
        repo_log = ROOT / "ci" / "results" / "review_log.jsonl"
        repo_size_before = repo_log.stat().st_size if repo_log.exists() else None

        self._accept(review_log=None)

        self.assertGreater(default_log.stat().st_size, workspace_size_before)
        repo_size_after = repo_log.stat().st_size if repo_log.exists() else None
        self.assertEqual(repo_size_before, repo_size_after)

    def test_descriptor_path_defaults_to_workspace_project_descriptor_json(self):
        self._seed_boundary_approval()
        target_path = self._accept(descriptor_path=None)
        self.assertTrue(target_path.exists())

    def test_unrelated_metadata_inputs_no_longer_exist(self):
        """There is no promotion_id/schema_versions/policy_version
        parameter left to pass an unrelated value through --
        accept_promotion() computes all three. Confirms the function
        signature itself, not just behavior."""
        import inspect
        params = inspect.signature(accept_promotion).parameters
        self.assertNotIn("promotion_id", params)
        self.assertNotIn("schema_versions", params)
        self.assertNotIn("policy_version", params)

    def test_malformed_boundary_artifact_is_refused_end_to_end(self):
        """The round-3 repro, run through the full operation: a boundary
        contract containing only {"schema_version": "999.0", "garbage":
        true} must refuse the whole acceptance, not produce a receipt
        declaring "boundary": "999.0"."""
        garbage_path = (
            self.workspace / "crates" / "scheduler" / "specs" / "_boundaries"
            / f"{VALID_BOUNDARY['boundary_id']}.json"
        )
        garbage_path.write_text(json.dumps({"schema_version": "999.0", "garbage": True}))
        with self.assertRaises(PromotionReceiptError):
            self._accept()
        self.assertFalse((self.workspace / "specs" / "_promotions" / "scheduling.json").exists())
        self.assertFalse(self.review_log.exists())

    def test_reviewed_exemption_referencing_a_nonexistent_interaction_is_refused_end_to_end(self):
        """The round-4 repro, run through the full operation: a
        hand-authored review block on an exemption is not enough to
        satisfy R2/G2 -- the interaction it names must actually exist
        and be resolvable via the real crate's own _interactions/
        directory."""
        exemption_path = _write_json(
            self.workspace, "crates/scheduler/specs/_exemptions/I-SCHED-TQ-999.json",
            {**VALID_EXEMPTION, "interaction_id": "I-SCHED-TQ-999"},
        )
        with self.assertRaises(PromotionReceiptError):
            self._accept(artifact_paths=self.artifact_paths + [exemption_path])
        self.assertFalse((self.workspace / "specs" / "_promotions" / "scheduling.json").exists())
        self.assertFalse(self.review_log.exists())

    def test_boundary_at_a_non_canonical_directory_is_not_trusted_end_to_end(self):
        """The round-4 repro, run through the full operation: a
        schema-valid boundary at junk/_boundaries/<id>.json must not
        have its schema_version trusted -- accept_promotion() should
        still succeed here (the real boundary elsewhere in the accepted
        set is sufficient), but the stray copy contributes nothing."""
        stray_path = _write_json(
            self.workspace, f"junk/_boundaries/{VALID_BOUNDARY['boundary_id']}.json", VALID_BOUNDARY
        )
        # Both copies carry VALID_BOUNDARY's own review block, so both
        # need an approval entry behind them (chainlink #82) -- the
        # stray copy is hashed as an opaque artifact, but a review block
        # is still a review block wherever the file sits.
        self._seed_boundary_approval()
        self._seed_boundary_approval(artifact_path=stray_path)
        # ...and the human-ruling gate needs the stray copy ruled too:
        # a ruling covers exactly the paths it names (chainlink #82).
        record_ruling(
            self.workspace,
            self.artifact_paths + [stray_path],
            reviewer="alice",
            verdict="ratified",
            descriptor_path=self.workspace / "project-descriptor.json",
        )
        target_path = self._accept(artifact_paths=self.artifact_paths + [stray_path])
        data = json.loads(target_path.read_text())
        self.assertEqual(data["schema_versions"], {"boundary": "1.0"})

    def test_policy_document_with_no_version_marker_is_refused_end_to_end(self):
        """A claimed policy_version with nothing in the file to back it
        must refuse, not produce a receipt with an unverified value."""
        (self.workspace / "docs" / "reliance-policy.md").write_text("# Reliance policy\n\nNo marker.\n")
        with self.assertRaises(PromotionReceiptError):
            self._accept()
        self.assertFalse((self.workspace / "specs" / "_promotions" / "scheduling.json").exists())
        self.assertFalse(self.review_log.exists())

    def test_policy_path_not_in_accepted_artifacts_is_refused(self):
        with self.assertRaises(PromotionReceiptError):
            self._accept(policy_path="docs/some-other-policy.md")
        self.assertFalse((self.workspace / "specs" / "_promotions" / "scheduling.json").exists())
        self.assertFalse(self.review_log.exists())

    def test_empty_reviewer_is_refused_before_any_file_is_touched(self):
        with self.assertRaises(ValueError):
            self._accept(reviewer="")
        self.assertFalse((self.workspace / "specs" / "_promotions" / "scheduling.json").exists())
        self.assertFalse(self.review_log.exists())

    def test_bad_artifact_path_is_refused_before_any_file_is_written(self):
        with self.assertRaises(PromotionReceiptError):
            self._accept(artifact_paths=["does/not/exist.json"], policy_path="does/not/exist.json")
        self.assertFalse((self.workspace / "specs" / "_promotions" / "scheduling.json").exists())
        self.assertFalse(self.review_log.exists())

    def test_missing_project_descriptor_is_refused(self):
        (self.workspace / "project-descriptor.json").unlink()
        with self.assertRaises(Exception):
            self._accept()
        self.assertFalse((self.workspace / "specs" / "_promotions" / "scheduling.json").exists())

    def test_unreadable_review_log_fails_closed_before_writing(self):
        """The directory-in-place-of-review_log repro (chainlink #45's
        own reviewer payload). It used to fail at the audit *append*;
        the review-provenance read (chainlink #82) now reaches the log
        first and refuses it as unprovable -- either way the acceptance
        is refused and nothing is written."""
        review_log = self.workspace / "ci" / "results" / "review_log.jsonl"
        review_log.parent.mkdir(parents=True, exist_ok=True)
        review_log.mkdir()  # a directory where the log file should be
        with self.assertRaises(PromotionReceiptError) as caught:
            self._accept(review_log=review_log)
        self.assertIn("approval audit log", str(caught.exception))
        self.assertFalse((self.workspace / "specs" / "_promotions" / "scheduling.json").exists())

    def test_audit_append_failure_leaves_no_receipt_installed(self):
        """External review, high severity: the receipt used to be
        written before the audit append, so a failure appending the
        audit entry left an accepted-but-unaudited receipt behind.
        Reproduced here by failing the append's own fsync (the original
        #45 repro -- a directory in place of review_log -- is covered
        above, where the provenance read now catches it first):
        accept_promotion() must raise, roll the prepared entry back,
        and write no receipt."""
        self._seed_boundary_approval()
        with self.assertRaises(Exception):
            with _failing_audit_fsync():
                self._accept()
        self.assertFalse((self.workspace / "specs" / "_promotions" / "scheduling.json").exists())
        # The seeded approval entry -- the trail provenance depends on --
        # is intact and alone: the rollback removed only the entry this
        # failed append prepared.
        self.assertEqual(len(self.review_log.read_text().strip().splitlines()), 1)

    def test_audit_append_failure_does_not_disturb_a_prior_receipt(self):
        """The rollback/transaction discipline must also hold on a
        re-acceptance: if the audit append fails, the PRIOR receipt (if
        any) must be left exactly as it was, not replaced with a
        half-written or missing file."""
        self._seed_boundary_approval()
        first = self._accept()
        original_bytes = first.read_bytes()
        log_before = self.review_log.read_bytes()

        with self.assertRaises(Exception):
            with _failing_audit_fsync():
                self._accept(accepted_at="2026-09-06")

        self.assertEqual(first.read_bytes(), original_bytes)
        self.assertEqual(self.review_log.read_bytes(), log_before)

    def test_reaccepting_the_same_cluster_overwrites_the_prior_receipt(self):
        """Re-generating a receipt for the same cluster (e.g. after the
        accepted set changed) is expected, not an error -- mirrors
        approve()'s own overwrite-on-reapprove behavior. promotion_id
        stays stable across the re-acceptance since it's derived purely
        from cluster."""
        self._seed_boundary_approval()
        first = self._accept()
        second = self._accept(accepted_at="2026-09-06")
        self.assertEqual(first, second)
        data = json.loads(second.read_text())
        self.assertEqual(data["accepted_at"], "2026-09-06")
        self.assertEqual(data["promotion_id"], "PROM-SCHEDULING-001")


class ReviewProvenanceTest(unittest.TestCase):
    """chainlink #82: the review-provenance guard itself. The exact
    defect was that `accept-promotion --reviewer <any string>` minted a
    Stage 4.5 receipt over every accepted artifact's review block
    without ever asking where those blocks came from -- here, both
    directions are pinned: a block with no approval behind it refuses
    (nothing written), and a block whose approval IS recorded is still
    accepted."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        _write_descriptor(self.workspace)
        _write_json(self.workspace, AcceptPromotionTest.BOUNDARY_ARTIFACT, VALID_BOUNDARY)
        (self.workspace / "docs").mkdir()
        (self.workspace / "docs" / "reliance-policy.md").write_text(POLICY_TEXT)
        self.review_log = self.workspace / "ci" / "results" / "review_log.jsonl"
        self.artifact_paths = ["docs/reliance-policy.md", AcceptPromotionTest.BOUNDARY_ARTIFACT]
        self.boundary_path = self.workspace / AcceptPromotionTest.BOUNDARY_ARTIFACT
        # Seed the human-ruling gate too (chainlink #82): these tests are
        # about PROVENANCE, and the ruling gate runs after it, so a
        # provenance refusal still fires first while a provenance pass
        # does not then bounce off a missing ruling.
        record_ruling(
            self.workspace,
            self.artifact_paths,
            reviewer="alice",
            verdict="ratified",
            descriptor_path=self.workspace / "project-descriptor.json",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _accept(self, **overrides):
        kwargs = dict(
            workspace_root=self.workspace,
            cluster="scheduling",
            reviewer="alice",
            policy_path="docs/reliance-policy.md",
            artifact_paths=self.artifact_paths,
            accepted_at="2026-09-10",
            review_log=self.review_log,
        )
        kwargs.update(overrides)
        return accept_promotion(**kwargs)

    def _refused(self, **overrides) -> str:
        with self.assertRaises(PromotionReceiptError) as caught:
            self._accept(**overrides)
        self.assertFalse((self.workspace / "specs" / "_promotions" / "scheduling.json").exists())
        return str(caught.exception)

    def test_a_hand_written_review_block_with_no_approval_entry_is_refused(self):
        """THE defect repro: a fully valid accepted set (real hashes, real
        schema_versions, real policy marker, validator-clean receipt)
        whose only review block was typed into the file, plus whatever
        `--reviewer` string the caller supplied. Refused, with the
        artifact named -- and no audit entry appended, since the refusal
        happens before the write transaction."""
        message = self._refused()
        self.assertIn(AcceptPromotionTest.BOUNDARY_ARTIFACT, message)
        self.assertIn("approval provenance", message)
        self.assertFalse(self.review_log.exists())

    def test_an_approval_recorded_through_the_real_approve_path_is_accepted(self):
        """The sanctioned path, end to end: review_checkpoint.approve()
        writes the artifact's review block AND the audit entry in one
        call, which is exactly what the guard reads back. Nothing in
        this test seeds the log by hand."""
        draft_path = stage_draft(json.loads(self.boundary_path.read_text()), self.boundary_path)
        approve(
            draft_path,
            self.boundary_path,
            reviewer="alice",
            reviewed_at="2026-09-10",
            review_log=self.review_log,
            validate_fn=SKIP_VALIDATION,
        )
        # approve() rewrote the block, so the file's bytes changed and the
        # ruling seeded in setUp is stale by design (chainlink #82's gate
        # binds a ruling to the version it ruled on) -- re-rule, as a human
        # would after approving.
        record_ruling(
            self.workspace,
            self.artifact_paths,
            reviewer="alice",
            verdict="ratified",
            descriptor_path=self.workspace / "project-descriptor.json",
        )
        receipt = self._accept()
        self.assertTrue(receipt.exists())
        self.assertEqual(
            json.loads(self.boundary_path.read_text())["review"], {"reviewer": "alice", "reviewed_at": "2026-09-10"}
        )

    def test_editing_the_reviewer_after_approval_is_refused(self):
        """Provenance is about the block, not just the path: re-writing
        review.reviewer after the approval happened (naming whoever is
        convenient) no longer matches the entry that approval wrote."""
        _record_approval(self.workspace, AcceptPromotionTest.BOUNDARY_ARTIFACT, VALID_BOUNDARY["review"])
        tampered = dict(VALID_BOUNDARY)
        tampered["review"] = {"reviewer": "whoever-is-convenient", "reviewed_at": "2026-08-30"}
        self.boundary_path.write_text(json.dumps(tampered))
        message = self._refused()
        self.assertIn("whoever-is-convenient", message)

    def test_editing_the_reviewed_date_after_approval_is_refused(self):
        _record_approval(self.workspace, AcceptPromotionTest.BOUNDARY_ARTIFACT, VALID_BOUNDARY["review"])
        tampered = dict(VALID_BOUNDARY)
        tampered["review"] = {"reviewer": "alice", "reviewed_at": "2026-09-01"}
        self.boundary_path.write_text(json.dumps(tampered))
        with self.assertRaises(PromotionReceiptError):
            self._accept()
        self.assertFalse((self.workspace / "specs" / "_promotions" / "scheduling.json").exists())

    def test_an_approval_entry_for_a_different_artifact_proves_nothing(self):
        _record_approval(
            self.workspace,
            "crates/scheduler/specs/_boundaries/somebody_else.json",
            VALID_BOUNDARY["review"],
        )
        message = self._refused()
        self.assertIn(AcceptPromotionTest.BOUNDARY_ARTIFACT, message)

    def test_a_relative_approval_entry_still_resolves_into_this_workspace(self):
        """Approvals recorded with a workspace-relative target_path (the
        natural form when the CLI runs inside the workspace) must match,
        not silently fail as 'some other path'."""
        _record_approval(
            self.workspace,
            AcceptPromotionTest.BOUNDARY_ARTIFACT,
            VALID_BOUNDARY["review"],
            entry_target_path=AcceptPromotionTest.BOUNDARY_ARTIFACT,
        )
        self.assertTrue(self._accept().exists())

    def test_a_mechanical_carry_forward_entry_proves_the_review_it_carried(self):
        """auto_promote_if_mechanical() deliberately leaves the artifact's
        own review block untouched -- the prior human review IS the
        approval being carried forward -- and records that in its entry's
        own reviewer field instead."""
        _record_approval(
            self.workspace,
            AcceptPromotionTest.BOUNDARY_ARTIFACT,
            VALID_BOUNDARY["review"],
            classification="mechanical",
            reviewer=f"auto-promoted (carried forward from {VALID_BOUNDARY['review']['reviewer']!r})",
        )
        self.assertTrue(self._accept().exists())

    def test_a_damaged_audit_log_fails_closed(self):
        """A log that cannot be read as the append-only approval trail
        it claims to be proves nothing, so it refuses rather than
        degrading to 'no entry found, treat the block as fine'."""
        self.review_log.parent.mkdir(parents=True, exist_ok=True)
        self.review_log.write_text(
            json.dumps(
                {
                    "target_path": str(self.boundary_path),
                    "classification": "new",
                    "reviewer": "alice",
                    "reviewed_at": "2026-08-30",
                }
            )
            + "\n"
            + "this line is not JSON\n"
        )
        message = self._refused()
        self.assertIn("not valid JSON", message)

    def test_an_entry_missing_the_approval_shape_is_not_proof(self):
        """An arbitrary JSON object dropped into the log (a stray line,
        an unrelated audit trail sharing the file) is not an approval
        entry -- every writer in review_checkpoint emits all four of
        target_path/classification/reviewer/reviewed_at."""
        self.review_log.parent.mkdir(parents=True, exist_ok=True)
        self.review_log.write_text(
            json.dumps(
                {
                    "target_path": str(self.boundary_path),
                    "reviewer": "alice",
                    "reviewed_at": "2026-08-30",
                }
            )
            + "\n"
        )
        with self.assertRaises(PromotionReceiptError):
            self._accept()
        self.assertFalse((self.workspace / "specs" / "_promotions" / "scheduling.json").exists())

    def test_collect_review_blocks_only_reports_artifacts_that_carry_one(self):
        """The policy document (a normative markdown file with no review
        block at all) has nothing to prove and must never be reported as
        a review-bearing artifact -- only the boundary contract is."""
        reviewed = collect_review_blocks(self.workspace, self.artifact_paths)
        self.assertEqual(sorted(reviewed), [AcceptPromotionTest.BOUNDARY_ARTIFACT])
        self.assertEqual(reviewed[AcceptPromotionTest.BOUNDARY_ARTIFACT], VALID_BOUNDARY["review"])

    def test_nothing_to_prove_is_not_a_refusal(self):
        """An empty set of review-bearing artifacts leaves no gaps -- the
        guard refuses unprovenanced blocks, not promotions in general."""
        self.assertEqual(review_provenance_gaps(self.workspace, {}, self.review_log), [])


class HumanRulingGateTest(unittest.TestCase):
    """chainlink #82's second remedy -- the human-ruling gate. Provenance
    asks where a review block came from; this asks whether a human has
    ruled on the accepted SET, which is the pilot's own constraint
    ("#14's accept-promotion must not run before rulings 1 and 2 land")
    and the only thing that covers artifacts provenance cannot see:
    blocks approved by an unattended agent, and records with no review
    block at all."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        _write_descriptor(self.workspace)
        _write_json(self.workspace, AcceptPromotionTest.BOUNDARY_ARTIFACT, VALID_BOUNDARY)
        (self.workspace / "docs").mkdir()
        (self.workspace / "docs" / "reliance-policy.md").write_text(POLICY_TEXT)
        # Provenance already satisfied, so every refusal below is the
        # RULING gate's and nothing else's (except the one test that
        # deliberately takes provenance away).
        _record_approval(self.workspace, AcceptPromotionTest.BOUNDARY_ARTIFACT, VALID_BOUNDARY["review"])
        self.artifact_paths = ["docs/reliance-policy.md", AcceptPromotionTest.BOUNDARY_ARTIFACT]
        self.ruling_log = self.workspace / "ci" / "results" / "human_rulings.jsonl"
        self.review_log = self.workspace / "ci" / "results" / "review_log.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _accept(self, **overrides):
        kwargs = dict(
            workspace_root=self.workspace,
            cluster="scheduling",
            reviewer="alice",
            policy_path="docs/reliance-policy.md",
            artifact_paths=self.artifact_paths,
            accepted_at="2026-09-12",
            review_log=self.review_log,
            ruling_log=self.ruling_log,
        )
        kwargs.update(overrides)
        return accept_promotion(**kwargs)

    def _rule(self, **overrides) -> dict:
        kwargs = dict(
            workspace_root=self.workspace,
            artifact_paths=self.artifact_paths,
            reviewer="alice",
            verdict="ratified",
            descriptor_path=self.workspace / "project-descriptor.json",
            ruling_log=self.ruling_log,
        )
        kwargs.update(overrides)
        return record_ruling(**kwargs)

    def _refused(self, **overrides) -> str:
        receipt = self.workspace / "specs" / "_promotions" / "scheduling.json"
        had_receipt = receipt.exists()
        with self.assertRaises(PromotionReceiptError) as caught:
            self._accept(**overrides)
        # Nothing NEW written: a receipt left over from an earlier
        # successful acceptance in the same test must be untouched, and a
        # refusal must never install one.
        self.assertEqual(receipt.exists(), had_receipt)
        return str(caught.exception)

    def test_a_promotion_nobody_ruled_on_is_refused(self):
        """The gate at rest: a fully valid accepted set -- real hashes,
        provenanced review blocks, validator-clean receipt -- with no
        recorded ruling anywhere. Refused, with the command that records
        one named in the message, and no ruling log created on the way
        out."""
        message = self._refused()
        self.assertIn("record-ruling", message)
        self.assertIn(AcceptPromotionTest.BOUNDARY_ARTIFACT, message)
        self.assertIn("docs/reliance-policy.md", message)
        self.assertFalse(self.ruling_log.exists())

    def test_a_ratified_ruling_over_the_accepted_set_is_accepted(self):
        entry = self._rule()
        self.assertEqual(entry["verdict"], "ratified")
        receipt = self._accept()
        self.assertTrue(receipt.exists())

    def test_a_rejected_ruling_blocks_the_promotion(self):
        """A recorded NO is a refusal, not a silent absence -- the
        pilot's ruling 1 has exactly this shape."""
        self._rule(verdict="rejected")
        message = self._refused()
        self.assertIn("REJECTED", message)
        self.assertIn(AcceptPromotionTest.BOUNDARY_ARTIFACT, message)

    def test_a_ruling_over_an_older_version_of_an_artifact_does_not_carry_over(self):
        """Rulings are bound to the bytes they ruled on: ratify, then
        change the file, and the ruling no longer applies until re-ruled.
        The policy document is edited here because it carries no review
        block, so provenance is untouched and the stale ruling is
        unambiguously what refused."""
        self._rule()
        policy = self.workspace / "docs" / "reliance-policy.md"
        policy.write_text(policy.read_text() + "\nA later edit.\n")
        message = self._refused()
        self.assertIn("different version", message)

    def test_a_partial_ruling_covers_only_the_artifacts_it_names(self):
        self._rule(artifact_paths=["docs/reliance-policy.md"])
        message = self._refused()
        self.assertIn(AcceptPromotionTest.BOUNDARY_ARTIFACT, message)
        self.assertNotIn("docs/reliance-policy.md:", message)

    def test_an_artifact_with_no_review_block_still_needs_a_ruling(self):
        """The pilot's ruling 2, in miniature: evidence records carry no
        `review` block, so the provenance check is silent about them by
        design -- the ruling gate is what still requires a human to rule
        on them before they are promoted."""
        evidence_path = "evidence/E-0143.json"
        _write_json(self.workspace, evidence_path, valid_evidence_record())
        self.artifact_paths.append(evidence_path)
        message = self._refused()
        self.assertIn(evidence_path, message)
        self._rule()
        self.assertTrue(self._accept().exists())

    def test_a_damaged_rulings_log_fails_closed(self):
        self.ruling_log.parent.mkdir(parents=True, exist_ok=True)
        self.ruling_log.write_text("this line is not JSON\n")
        message = self._refused()
        self.assertIn("not valid JSON", message)

    def test_an_unrecognized_verdict_cannot_satisfy_the_gate(self):
        """record_ruling() refuses unknown verdicts outright, so only a
        hand-written entry can put one in the log -- and the gate must
        not treat "some verdict" as "a ruling"."""
        self.ruling_log.parent.mkdir(parents=True, exist_ok=True)
        self.ruling_log.write_text(
            json.dumps(
                {
                    "artifacts": compute_artifact_manifest(
                        self.workspace, self.artifact_paths, DESCRIPTOR
                    ),
                    "verdict": "maybe",
                    "reviewer": "alice",
                    "ruled_at": "2026-09-12",
                }
            )
            + "\n"
        )
        message = self._refused()
        self.assertIn("maybe", message)

    def test_the_latest_ruling_wins_in_either_direction(self):
        """The log is append-only, so a human supersedes an earlier
        decision by recording a new one -- never by editing history."""
        self._rule(verdict="rejected")
        self.assertIn("REJECTED", self._refused())
        self._rule(verdict="ratified")
        self.assertTrue(self._accept().exists())
        self._rule(verdict="rejected")
        self.assertIn("REJECTED", self._refused())

    def test_provenance_is_checked_before_the_ruling_gate(self):
        """Take the provenance trail away as well and the refusal is
        provenance's, not the ruling gate's -- ordering matters, since a
        genuinely unprovenanced review block must be reported for what it
        is rather than as bookkeeping."""
        self.review_log.unlink()
        message = self._refused()
        self.assertIn("approval provenance", message)
        self.assertNotIn("record-ruling", message)

    def test_a_recorded_ruling_carries_the_same_hashes_the_receipt_will(self):
        """record_ruling() must use compute_artifact_manifest() -- the
        receipt's own routine -- otherwise the gate would compare hashes
        the two sides derive differently (witness promotion digests above
        all)."""
        entry = self._rule()
        expected = compute_artifact_manifest(self.workspace, self.artifact_paths, DESCRIPTOR)
        self.assertEqual(entry["artifacts"], expected)
        self.assertEqual([a["path"] for a in entry["artifacts"]], self.artifact_paths)

    def test_record_ruling_refuses_an_empty_reviewer(self):
        with self.assertRaises(ValueError):
            self._rule(reviewer="")
        self.assertFalse(self.ruling_log.exists())

    def test_record_ruling_refuses_an_unknown_verdict(self):
        with self.assertRaises(PromotionReceiptError):
            self._rule(verdict="probably-fine")
        self.assertFalse(self.ruling_log.exists())

    def test_record_ruling_refuses_a_malformed_ruled_at_date(self):
        with self.assertRaises(PromotionReceiptError):
            self._rule(ruled_at="soon")
        self.assertFalse(self.ruling_log.exists())

    def test_record_ruling_refuses_a_missing_artifact_before_writing_anything(self):
        with self.assertRaises(PromotionReceiptError):
            self._rule(artifact_paths=["does/not/exist.json"])
        self.assertFalse(self.ruling_log.exists())

    def test_record_ruling_fails_closed_without_a_project_descriptor(self):
        (self.workspace / "project-descriptor.json").unlink()
        with self.assertRaises(Exception):
            self._rule()
        self.assertFalse(self.ruling_log.exists())

    def test_ruling_gaps_is_vacuous_for_an_empty_manifest(self):
        self.assertEqual(ruling_gaps(self.workspace, [], self.ruling_log), [])


TASK_QUEUE_WITNESS_PATH = "crates/scheduler/specs/_witnesses/task_queue.load_factor.json"
TASK_QUEUE_CONCEPT_PATH = "crates/scheduler/specs/task_queue.json"


def _write_declared_feature(
    workspace: Path, witness_overrides: dict | None = None, cluster: str = "scheduling"
) -> None:
    """A declared (witness_required: true) TaskQueue.load_factor feature
    plus its genuinely valid witness spec, under DESCRIPTOR's own
    crates/scheduler crate -- reused by every witness-promotion-integrity
    test below rather than re-derived per test. `cluster` (chainlink
    #35's own required completeness scoping) is written onto the
    concept spec's own top-level `cluster` field, matching
    generate_feature_ledger.owning_cluster_for()'s reading of it --
    without this, required_witness_paths() would attribute the feature
    to no cluster at all ("unknown") and never require it."""
    concept = concept_spec()
    concept["queries"][0]["witness_required"] = True
    concept["cluster"] = cluster
    _write_json(workspace, TASK_QUEUE_CONCEPT_PATH, concept)
    witness = witness_spec()
    if witness_overrides:
        witness.update(witness_overrides)
    _write_json(workspace, TASK_QUEUE_WITNESS_PATH, witness)


class WitnessPromotionIntegrityTest(unittest.TestCase):
    """chainlink #35: witness specs and their determinism.value_hash
    join artifact_manifest -- a fixture or value change invalidates the
    receipt, output.render_hash alone never does, and a declared feature
    with no properly-included witness refuses generation outright."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        _write_descriptor(self.workspace)
        # Deliberately no boundary contract in this fixture:
        # validate_boundary_contracts.py's own G2+ concept-spec scan
        # (specs_search_root.glob("**/*.json"), unfiltered by
        # underscore-prefixed directories) would otherwise also match
        # the witness spec's own top-level `concept` field as a SECOND
        # "TaskQueue" concept spec and report it ambiguous -- a latent
        # gap in that unrelated module, out of this issue's scope to
        # fix. A witness-only accepted set is sufficient here:
        # compute_schema_versions only needs ONE recognized kind to
        # produce a non-empty schema_versions, and "witness" now
        # qualifies on its own.
        (self.workspace / "docs").mkdir()
        (self.workspace / "docs" / "reliance-policy.md").write_text(POLICY_TEXT)
        _write_declared_feature(self.workspace)
        # The witness spec carries its own review block, so it needs the
        # approval entry behind it before ANY acceptance here can succeed
        # (chainlink #82); no test in this class inspects the log itself.
        _record_approval(self.workspace, TASK_QUEUE_WITNESS_PATH, witness_spec()["review"])
        self.artifact_paths = [
            "docs/reliance-policy.md",
            TASK_QUEUE_WITNESS_PATH,
        ]
        # ...and the human-ruling gate needs a ruling over that same set
        # (chainlink #82) before any acceptance here can succeed either.
        record_ruling(
            self.workspace,
            self.artifact_paths,
            reviewer="alice",
            verdict="ratified",
            descriptor_path=self.workspace / "project-descriptor.json",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _accept(self, **overrides):
        kwargs = dict(
            workspace_root=self.workspace,
            cluster="scheduling",
            reviewer="alice",
            policy_path="docs/reliance-policy.md",
            artifact_paths=self.artifact_paths,
            accepted_at="2026-09-08",
        )
        kwargs.update(overrides)
        return accept_promotion(**kwargs)

    def test_a_valid_promotion_containing_the_required_witness_spec_passes(self):
        target_path = self._accept()
        data = json.loads(target_path.read_text())
        witness_entry = next(e for e in data["artifact_manifest"] if e["path"] == TASK_QUEUE_WITNESS_PATH)
        self.assertEqual(witness_entry["hash"], witness_promotion_digest(witness_spec()))
        self.assertEqual(data["schema_versions"]["witness"], "1.0")
        findings = validate_file(target_path, load_validator(), self.workspace, DESCRIPTOR)
        self.assertEqual(findings, [], [str(f) for f in findings])

    def test_omitting_the_required_witness_spec_is_refused(self):
        with self.assertRaises(PromotionReceiptError) as caught:
            self._accept(artifact_paths=self.artifact_paths[:-1])
        self.assertIn(TASK_QUEUE_WITNESS_PATH, str(caught.exception))
        self.assertFalse((self.workspace / "specs" / "_promotions" / "scheduling.json").exists())

    def test_a_declared_feature_with_no_witness_at_all_is_refused(self):
        (self.workspace / TASK_QUEUE_WITNESS_PATH).unlink()
        with self.assertRaises(PromotionReceiptError):
            self._accept(artifact_paths=self.artifact_paths[:-1])

    def test_an_invalid_witness_spec_is_refused(self):
        broken = witness_spec()
        del broken["determinism"]
        _write_json(self.workspace, TASK_QUEUE_WITNESS_PATH, broken)
        with self.assertRaises(PromotionReceiptError):
            self._accept()
        self.assertFalse((self.workspace / "specs" / "_promotions" / "scheduling.json").exists())

    def test_an_ambiguously_resolved_witness_is_refused(self):
        """Two genuinely valid witness specs declaring the same
        witness_id under different crates -- gate_g19.collect_valid_witness_entries'
        own ambiguity handling, reused rather than re-derived, must
        still catch this."""
        other_descriptor = dict(DESCRIPTOR)
        other_descriptor["crates"] = list(DESCRIPTOR["crates"]) + [
            {"crate_dir": "crates/other", "contracts_crate": "contracts", "specs_search_root": "crates/other/specs"}
        ]
        _write_descriptor(self.workspace, other_descriptor)
        other_concept = concept_spec()
        other_concept["queries"][0]["witness_required"] = True
        other_concept["cluster"] = "scheduling"
        _write_json(self.workspace, "crates/other/specs/task_queue.json", other_concept)
        _write_json(self.workspace, "crates/other/specs/_witnesses/task_queue.load_factor.json", witness_spec())
        with self.assertRaises(PromotionReceiptError):
            self._accept()

    def test_fixture_id_change_revokes_acceptance(self):
        target_path = self._accept()
        original = target_path.read_bytes()
        spec = witness_spec()
        spec["fixture"]["fixture_id"] = "FX-CHANGED"
        _write_json(self.workspace, TASK_QUEUE_WITNESS_PATH, spec)
        findings = validate_file(target_path, load_validator(), self.workspace, DESCRIPTOR)
        self.assertTrue(any("acceptance is revoked" in f.reason for f in findings), [str(f) for f in findings])
        self.assertEqual(target_path.read_bytes(), original)

    def test_seed_change_revokes_acceptance(self):
        target_path = self._accept()
        spec = witness_spec()
        spec["fixture"]["seed"] = 42
        _write_json(self.workspace, TASK_QUEUE_WITNESS_PATH, spec)
        findings = validate_file(target_path, load_validator(), self.workspace, DESCRIPTOR)
        self.assertTrue(any("acceptance is revoked" in f.reason for f in findings), [str(f) for f in findings])

    def test_expectation_change_revokes_acceptance(self):
        target_path = self._accept()
        spec = witness_spec()
        spec["expectation"]["coverage_region"] = "full-grid"
        _write_json(self.workspace, TASK_QUEUE_WITNESS_PATH, spec)
        findings = validate_file(target_path, load_validator(), self.workspace, DESCRIPTOR)
        self.assertTrue(any("acceptance is revoked" in f.reason for f in findings), [str(f) for f in findings])

    def test_renderer_identity_change_revokes_acceptance(self):
        target_path = self._accept()
        spec = witness_spec()
        spec["renderer"] = "series_svg"
        spec["expectation"]["renderer"] = "series_svg"
        spec["output"]["renderer_actual"] = "series_svg"
        _write_json(self.workspace, TASK_QUEUE_WITNESS_PATH, spec)
        findings = validate_file(target_path, load_validator(), self.workspace, DESCRIPTOR)
        self.assertTrue(any("acceptance is revoked" in f.reason for f in findings), [str(f) for f in findings])

    def test_determinism_value_hash_change_revokes_acceptance(self):
        target_path = self._accept()
        spec = witness_spec(value_hash="sha256:" + "5" * 64)
        _write_json(self.workspace, TASK_QUEUE_WITNESS_PATH, spec)
        findings = validate_file(target_path, load_validator(), self.workspace, DESCRIPTOR)
        self.assertTrue(any("acceptance is revoked" in f.reason for f in findings), [str(f) for f in findings])

    def test_render_hash_alone_does_not_revoke_acceptance(self):
        target_path = self._accept()
        spec = witness_spec()
        spec["output"]["render_hash"] = "sha256:" + "3" * 64
        _write_json(self.workspace, TASK_QUEUE_WITNESS_PATH, spec)
        findings = validate_file(target_path, load_validator(), self.workspace, DESCRIPTOR)
        self.assertEqual(findings, [], [str(f) for f in findings])

    def test_required_witness_paths_never_names_a_generated_projection(self):
        required = required_witness_paths(DESCRIPTOR, self.workspace, "scheduling")
        self.assertEqual(set(required), {TASK_QUEUE_WITNESS_PATH})

    def test_generated_svg_contact_sheet_and_ledger_bytes_are_refused_as_promotion_inputs(self):
        """plan.md §16.6's own boundary: generated witness SVGs,
        docs/witnesses/_contact_sheet.svg, and ci/results/feature_ledger.json
        are review projections, never promotion inputs. External review,
        medium severity: an earlier version silently accepted these as
        ordinary byte-hashed artifacts once a caller listed them --
        proving they got hashed at all is proof they WERE gating (the
        opposite of what this test used to claim), since a receipt
        listing one would then have its acceptance revoked whenever
        that picture or the ledger regenerated. compute_artifact_manifest
        must instead refuse to include any of them, outright, before
        computing anything."""
        (self.workspace / "docs" / "witnesses").mkdir(parents=True)
        svg_bytes = b"<svg></svg>"
        (self.workspace / "docs" / "witnesses" / "task_queue.load_factor.svg").write_bytes(svg_bytes)
        (self.workspace / "docs" / "witnesses" / "_contact_sheet.svg").write_bytes(svg_bytes)
        (self.workspace / "ci" / "results").mkdir(parents=True, exist_ok=True)
        ledger_bytes = b'{"schema_version": "1.0", "generated_from": {}, "features": []}'
        (self.workspace / "ci" / "results" / "feature_ledger.json").write_bytes(ledger_bytes)

        for offending_path in (
            "docs/witnesses/task_queue.load_factor.svg",
            "docs/witnesses/_contact_sheet.svg",
            "ci/results/feature_ledger.json",
        ):
            with self.assertRaises(PromotionReceiptError) as caught:
                compute_artifact_manifest(self.workspace, [offending_path], DESCRIPTOR)
            self.assertIn("review projection", str(caught.exception))

    def test_accept_promotion_refuses_a_listed_contact_sheet_end_to_end(self):
        (self.workspace / "docs" / "witnesses").mkdir(parents=True)
        (self.workspace / "docs" / "witnesses" / "_contact_sheet.svg").write_bytes(b"<svg></svg>")
        with self.assertRaises(PromotionReceiptError):
            self._accept(artifact_paths=self.artifact_paths + ["docs/witnesses/_contact_sheet.svg"])
        self.assertFalse((self.workspace / "specs" / "_promotions" / "scheduling.json").exists())

    def test_a_declared_feature_in_a_different_cluster_is_not_required(self):
        """External review, medium severity: an earlier version computed
        the required set workspace-wide, ignoring cluster, so a
        promotion for "scheduling" wrongly required witnesses belonging
        to unrelated clusters. A concept spec's own top-level `cluster`
        field -- already read the identical way by
        generate_feature_ledger.owning_cluster_for(), reused here rather
        than re-derived -- is what scopes the requirement."""
        other_concept = {
            "concept": "OtherConcept",
            "cluster": "other-cluster",
            "queries": [
                {
                    "english": "An unrelated query.", "rust_sig": "fn other_query(&self) -> f64",
                    "pure": True, "witness_required": True,
                },
            ],
            "constraints": [],
        }
        _write_json(self.workspace, "crates/scheduler/specs/other_concept.json", other_concept)
        other_witness = witness_spec()
        other_witness["witness_id"] = "W-OC-OTHER-QUERY"
        other_witness["concept"] = "OtherConcept"
        other_witness["query"] = "other_query"
        other_witness["output"]["path"] = "docs/witnesses/other_concept.other_query.svg"
        _write_json(
            self.workspace, "crates/scheduler/specs/_witnesses/other_concept.other_query.json", other_witness
        )

        required = required_witness_paths(DESCRIPTOR, self.workspace, "scheduling")
        self.assertEqual(set(required), {TASK_QUEUE_WITNESS_PATH})

        # Accepting "scheduling" succeeds without ever including the
        # other-cluster witness -- it is not required, and is not
        # silently swept in either.
        target_path = self._accept()
        self.assertTrue(target_path.exists())

    def test_a_concept_spec_with_no_cluster_field_fails_closed(self):
        """External review, high severity: owning_cluster_for()'s own
        'unknown' fallback for a missing cluster field would silently
        exclude a declared feature from every cluster's required set --
        removing cluster, then removing the witness from the accepted
        set, used to yield zero findings. This pipeline never runs
        concept-to-code's own JSON Schema validator against a concept
        spec, so a missing `cluster` is a real, reachable state."""
        concept = json.loads((self.workspace / TASK_QUEUE_CONCEPT_PATH).read_text())
        del concept["cluster"]
        _write_json(self.workspace, TASK_QUEUE_CONCEPT_PATH, concept)

        with self.assertRaises(RequiredWitnessError):
            required_witness_paths(DESCRIPTOR, self.workspace, "scheduling")

        # The full attack: cluster removed, THEN the witness omitted --
        # must still refuse, not silently produce a receipt.
        with self.assertRaises(PromotionReceiptError):
            self._accept(artifact_paths=self.artifact_paths[:-1])
        self.assertFalse((self.workspace / "specs" / "_promotions" / "scheduling.json").exists())

    def test_a_concept_spec_that_becomes_unreadable_after_declaration_fails_closed(self):
        """Defense in depth for the same fail-closed rule. A concept
        spec that is invalid JSON (or unreadable) OUTRIGHT is already
        excluded at collect_declared_features()'s own discovery step --
        gate_g18.py's own "genuinely valid, not just present" bar --
        so it never reaches required_witness_paths()'s own cluster
        loop at all; this reproduces the only way that loop's own
        read actually can fail: a race between discovery's read and
        this function's own second one."""
        from unittest.mock import patch
        concept_path = str((self.workspace / TASK_QUEUE_CONCEPT_PATH).resolve())
        real_read_text = Path.read_text
        calls = {"count": 0}

        def flaky_read_text(path_self, *args, **kwargs):
            if str(path_self) == concept_path:
                calls["count"] += 1
                if calls["count"] > 1:
                    raise OSError("simulated read failure on second read")
            return real_read_text(path_self, *args, **kwargs)

        with patch.object(Path, "read_text", flaky_read_text):
            with self.assertRaises(RequiredWitnessError):
                required_witness_paths(DESCRIPTOR, self.workspace, "scheduling")

    def test_a_schema_invalid_cluster_string_fails_closed(self):
        """External review, high severity: a non-empty-but-out-of-grammar
        cluster value (e.g. "SCHEDULING", uppercase) passed an
        isinstance-and-non-empty check, but no schema-valid receipt
        `cluster` (docs/promotion-receipt-schema.json's own
        ^[a-z][a-z0-9-]*$) could ever equal it -- the feature was
        silently excluded from every promotion's required set forever,
        the identical bug the missing-cluster fix closed, reached
        through a different input shape."""
        concept = json.loads((self.workspace / TASK_QUEUE_CONCEPT_PATH).read_text())
        concept["cluster"] = "SCHEDULING"
        _write_json(self.workspace, TASK_QUEUE_CONCEPT_PATH, concept)

        with self.assertRaises(RequiredWitnessError):
            required_witness_paths(DESCRIPTOR, self.workspace, "scheduling")

        with self.assertRaises(PromotionReceiptError):
            self._accept(artifact_paths=self.artifact_paths[:-1])
        self.assertFalse((self.workspace / "specs" / "_promotions" / "scheduling.json").exists())

    def test_a_concept_spec_that_is_not_a_json_object_fails_closed(self):
        """Low severity: valid JSON that parses to something other than
        an object (e.g. a bare array) must raise RequiredWitnessError,
        not AttributeError from calling .get() on a list."""
        from unittest.mock import patch
        concept_path = str((self.workspace / TASK_QUEUE_CONCEPT_PATH).resolve())
        real_read_text = Path.read_text
        calls = {"count": 0}

        def flaky_read_text(path_self, *args, **kwargs):
            if str(path_self) == concept_path:
                calls["count"] += 1
                if calls["count"] > 1:
                    return "[]"
            return real_read_text(path_self, *args, **kwargs)

        with patch.object(Path, "read_text", flaky_read_text):
            with self.assertRaises(RequiredWitnessError):
                required_witness_paths(DESCRIPTOR, self.workspace, "scheduling")

    def test_a_symlinked_generated_svg_path_is_still_refused(self):
        """External review, medium severity: classifying a generated
        review projection by its RESOLVED identity alone let a symlink
        planted at the canonical docs/witnesses/*.svg location, but
        pointing outside it, bypass refusal entirely -- the resolved
        target's own parent directory is not docs/witnesses, so the
        old resolved-only check missed it, accepted it as an ordinary
        byte-hashed artifact, and its later target change then revoked
        an unrelated promotion."""
        (self.workspace / "docs" / "witnesses").mkdir(parents=True)
        ordinary = self.workspace / "ordinary_file.txt"
        ordinary.write_text("not a witness rendering")
        decoy = self.workspace / "docs" / "witnesses" / "task_queue.load_factor.svg"
        decoy.symlink_to(ordinary)

        with self.assertRaises(PromotionReceiptError) as caught:
            compute_artifact_manifest(
                self.workspace, ["docs/witnesses/task_queue.load_factor.svg"], DESCRIPTOR
            )
        self.assertIn("review projection", str(caught.exception))

    def test_witness_schema_version_is_derived_only_from_a_genuinely_valid_canonical_witness(self):
        stray = witness_spec()
        _write_json(self.workspace, "junk/_witnesses/task_queue.load_factor.json", stray)
        versions = compute_schema_versions(
            self.workspace, DESCRIPTOR, self.artifact_paths + ["junk/_witnesses/task_queue.load_factor.json"]
        )
        self.assertEqual(versions["witness"], "1.0")


if __name__ == "__main__":
    unittest.main()
