import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from generate_promotion_receipt import (  # noqa: E402
    PromotionReceiptError,
    accept_promotion,
    build_receipt,
    compute_artifact_manifest,
    compute_promotion_id,
    compute_schema_versions,
    extract_policy_version,
)
from review_checkpoint import ApprovalRefused  # noqa: E402
from validate_promotion_receipt import load_validator, validate_file  # noqa: E402

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

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        _write_descriptor(self.workspace)
        self.boundary_bytes = json.dumps(VALID_BOUNDARY).encode()
        _write_json(
            self.workspace, f"crates/scheduler/specs/_boundaries/{VALID_BOUNDARY['boundary_id']}.json",
            VALID_BOUNDARY,
        )
        self.policy_bytes = POLICY_TEXT.encode()
        (self.workspace / "docs").mkdir()
        (self.workspace / "docs" / "reliance-policy.md").write_bytes(self.policy_bytes)
        self.review_log = self.workspace / "ci" / "results" / "review_log.jsonl"
        self.artifact_paths = [
            "docs/reliance-policy.md",
            f"crates/scheduler/specs/_boundaries/{VALID_BOUNDARY['boundary_id']}.json",
        ]

    def tearDown(self):
        self.tmp.cleanup()

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
        target_path = self._accept()
        findings = validate_file(target_path, load_validator(), self.workspace)
        self.assertEqual(findings, [], [str(f) for f in findings])

    def test_appends_an_audit_log_entry(self):
        self.assertFalse(self.review_log.exists())
        target_path = self._accept()
        self.assertTrue(self.review_log.exists())
        entry = json.loads(self.review_log.read_text().strip().splitlines()[-1])
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
        workspace_root, never outside it."""
        default_log = self.workspace / "ci" / "results" / "review_log.jsonl"
        self.assertFalse(default_log.exists())
        self._accept(review_log=None)
        self.assertTrue(default_log.exists())

    def test_descriptor_path_defaults_to_workspace_project_descriptor_json(self):
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

    def test_audit_log_failure_leaves_no_receipt_installed(self):
        """External review, high severity: the receipt used to be
        written before the audit append, so a failure appending the
        audit entry left an accepted-but-unaudited receipt behind.
        Reproduced here with a directory in place of review_log (the
        reviewer's own repro) -- accept_promotion() must raise and
        write nothing."""
        review_log = self.workspace / "ci" / "results" / "review_log.jsonl"
        review_log.parent.mkdir(parents=True)
        review_log.mkdir()  # a directory where the log file should be
        with self.assertRaises(Exception):
            self._accept(review_log=review_log)
        self.assertFalse((self.workspace / "specs" / "_promotions" / "scheduling.json").exists())

    def test_audit_log_failure_does_not_disturb_a_prior_receipt(self):
        """The rollback/transaction discipline must also hold on a
        re-acceptance: if the audit append fails, the PRIOR receipt (if
        any) must be left exactly as it was, not replaced with a
        half-written or missing file."""
        first = self._accept()
        original_bytes = first.read_bytes()

        review_log = self.workspace / "ci" / "results" / "review_log.jsonl"
        review_log.unlink()
        review_log.mkdir()  # now a directory -- the next append will fail
        with self.assertRaises(Exception):
            self._accept(accepted_at="2026-09-06", review_log=review_log)
        self.assertEqual(first.read_bytes(), original_bytes)

    def test_reaccepting_the_same_cluster_overwrites_the_prior_receipt(self):
        """Re-generating a receipt for the same cluster (e.g. after the
        accepted set changed) is expected, not an error -- mirrors
        approve()'s own overwrite-on-reapprove behavior. promotion_id
        stays stable across the re-acceptance since it's derived purely
        from cluster."""
        first = self._accept()
        second = self._accept(accepted_at="2026-09-06")
        self.assertEqual(first, second)
        data = json.loads(second.read_text())
        self.assertEqual(data["accepted_at"], "2026-09-06")
        self.assertEqual(data["promotion_id"], "PROM-SCHEDULING-001")


if __name__ == "__main__":
    unittest.main()
