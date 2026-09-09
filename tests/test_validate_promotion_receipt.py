import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from validate_promotion_receipt import validate_data, validate_file, load_validator  # noqa: E402
from validate_witness import witness_promotion_digest  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))
from test_validate_witness import concept_spec, witness_spec  # noqa: E402

FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "promotions" / "valid"
RECEIPT_PATH = FIXTURE_ROOT / "specs" / "_promotions" / "scheduling.json"
YAML_RECEIPT_PATH = FIXTURE_ROOT / "specs" / "_promotions" / "scheduling.yaml"


def load_valid() -> dict:
    return json.loads(RECEIPT_PATH.read_text())


def run(data: dict, workspace_root: Path = FIXTURE_ROOT, receipt_path: Path = RECEIPT_PATH):
    validator = load_validator()
    return validate_data(receipt_path, data, validator, workspace_root)


class ValidPromotionReceiptTest(unittest.TestCase):
    def test_valid_receipt_has_no_findings(self):
        findings = run(load_valid())
        self.assertEqual(findings, [], [str(f) for f in findings])

    def test_canonical_yaml_receipt_validates_too(self):
        validator = load_validator()
        findings = validate_file(YAML_RECEIPT_PATH, validator, FIXTURE_ROOT)
        self.assertEqual(findings, [], [str(f) for f in findings])


class G1aFailureTest(unittest.TestCase):
    def test_missing_required_field_is_rejected(self):
        data = load_valid()
        del data["policy_version"]
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_bad_promotion_id_format_is_rejected(self):
        data = load_valid()
        data["promotion_id"] = "not-a-real-id"
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_bad_hash_format_is_rejected(self):
        data = load_valid()
        data["artifact_manifest"][0]["hash"] = "not-a-sha256"
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))


class NamingTest(unittest.TestCase):
    def test_filename_cluster_mismatch_is_rejected(self):
        data = load_valid()
        data["cluster"] = "some-other-cluster"
        findings = run(data)
        self.assertTrue(any(f.gate == "G1b" for f in findings), [str(f) for f in findings])

    def test_receipt_outside_workspace_is_rejected(self):
        """External review, medium severity: a receipt at an arbitrary
        path like /tmp/scheduling.json validated cleanly against a
        fixture workspace it wasn't even part of."""
        data = load_valid()
        findings = run(data, receipt_path=Path("/tmp/scheduling.json"))
        self.assertTrue(
            any("does not resolve inside the workspace" in f.reason for f in findings),
            [str(f) for f in findings],
        )

    def test_receipt_not_under_canonical_directory_is_rejected(self):
        """A receipt that does resolve inside the workspace but isn't
        directly under specs/_promotions/ must still be rejected -- the
        prior version only ever checked the filename stem."""
        data = load_valid()
        findings = run(data, receipt_path=FIXTURE_ROOT / "scheduling.json")
        self.assertTrue(
            any("canonical specs/_promotions/ directory" in f.reason for f in findings),
            [str(f) for f in findings],
        )


class ArtifactManifestTest(unittest.TestCase):
    def test_hash_mismatch_is_revoked(self):
        data = load_valid()
        data["artifact_manifest"][0]["hash"] = "sha256:" + "0" * 64
        findings = run(data)
        self.assertTrue(any("acceptance is revoked" in f.reason for f in findings), [str(f) for f in findings])

    def test_missing_file_is_rejected(self):
        data = load_valid()
        data["artifact_manifest"][0]["path"] = "does/not/exist.json"
        findings = run(data)
        self.assertTrue(any("does not exist" in f.reason for f in findings), [str(f) for f in findings])

    def test_absolute_path_is_rejected(self):
        data = load_valid()
        data["artifact_manifest"][0]["path"] = "/etc/hosts"
        findings = run(data)
        self.assertTrue(any("not workspace-relative" in f.reason for f in findings), [str(f) for f in findings])

    def test_traversal_path_is_rejected(self):
        data = load_valid()
        data["artifact_manifest"][0]["path"] = "../outside/x.json"
        findings = run(data)
        self.assertTrue(any("not workspace-relative" in f.reason for f in findings), [str(f) for f in findings])

    def test_self_reference_is_rejected(self):
        """plan.md §7.1: 'the receipt is not in its own manifest.'"""
        import hashlib
        data = load_valid()
        receipt_hash = "sha256:" + hashlib.sha256(RECEIPT_PATH.read_bytes()).hexdigest()
        data["artifact_manifest"].append({
            "path": "specs/_promotions/scheduling.json",
            "hash": receipt_hash,
        })
        findings = run(data)
        self.assertTrue(any("own path" in f.reason for f in findings), [str(f) for f in findings])

    def test_referenced_artifact_carrying_promotion_id_is_rejected(self):
        """plan.md §7.1: references are one-way -- normative artifacts
        carry review blocks and never a promotion_id."""
        import hashlib
        data = load_valid()
        bad_path = FIXTURE_ROOT / "specs" / "bad_artifact_with_promotion_id.json"
        bad_hash = "sha256:" + hashlib.sha256(bad_path.read_bytes()).hexdigest()
        data["artifact_manifest"].append({
            "path": "specs/bad_artifact_with_promotion_id.json",
            "hash": bad_hash,
        })
        findings = run(data)
        self.assertTrue(any("carries a promotion_id field" in f.reason for f in findings), [str(f) for f in findings])

    def test_yaml_referenced_artifact_carrying_promotion_id_is_rejected(self):
        """External review, high severity: one-way-reference enforcement
        only ever inspected .json artifacts. A correctly-hashed YAML
        artifact carrying its own promotion_id passed with zero findings
        -- plan.md §7.1's rule applies to normative artifacts regardless
        of serialization format."""
        import hashlib
        data = load_valid()
        bad_path = FIXTURE_ROOT / "specs" / "bad_artifact_with_promotion_id.yaml"
        bad_hash = "sha256:" + hashlib.sha256(bad_path.read_bytes()).hexdigest()
        data["artifact_manifest"].append({
            "path": "specs/bad_artifact_with_promotion_id.yaml",
            "hash": bad_hash,
        })
        findings = run(data)
        self.assertTrue(any("carries a promotion_id field" in f.reason for f in findings), [str(f) for f in findings])

    def test_duplicate_manifest_entry_is_rejected(self):
        """External review, medium severity: appending an identical copy
        of an existing manifest entry produced zero findings -- the
        manifest states the exact artifact set, not a set with
        duplicates. Tracked by resolved path identity, since schema-level
        uniqueItems on the path string wouldn't catch a path-string alias
        resolving to the same real file."""
        data = load_valid()
        data["artifact_manifest"].append(dict(data["artifact_manifest"][0]))
        findings = run(data)
        self.assertTrue(
            any("resolve to the same file" in f.reason for f in findings),
            [str(f) for f in findings],
        )


class StandaloneCliMainTest(unittest.TestCase):
    """External review, low severity: the three CLI integration tests in
    tests/test_pipeline.py invoke pipeline.main(), never
    validate_promotion_receipt.main() itself -- an untested standalone
    entrypoint is exactly how a wiring gap ships invisibly regardless of
    library-level coverage."""

    def test_main_passes_for_valid_receipt(self):
        import validate_promotion_receipt
        rc = validate_promotion_receipt.main(
            [str(RECEIPT_PATH), "--workspace-root", str(FIXTURE_ROOT)]
        )
        self.assertEqual(rc, 0)

    def test_main_fails_for_schema_invalid_receipt(self):
        import validate_promotion_receipt
        data = load_valid()
        del data["policy_version"]
        with tempfile.TemporaryDirectory() as tmp:
            bad_receipt = Path(tmp) / "specs" / "_promotions" / "scheduling.json"
            bad_receipt.parent.mkdir(parents=True)
            bad_receipt.write_text(json.dumps(data))
            rc = validate_promotion_receipt.main(
                [str(bad_receipt), "--workspace-root", str(tmp)]
            )
        self.assertEqual(rc, 1)

    def test_main_fails_for_receipt_outside_workspace(self):
        """The exact reproduction from the external review: a validly
        hashed receipt copied to /tmp validated cleanly via the
        standalone CLI because main() never anchored the receipt path
        itself."""
        import validate_promotion_receipt
        rc = validate_promotion_receipt.main(
            [str(RECEIPT_PATH), "--workspace-root", "/tmp"]
        )
        self.assertEqual(rc, 1)


WITNESS_DESCRIPTOR = {
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

WITNESS_RELATIVE_PATH = "crates/scheduler/specs/_witnesses/task_queue.load_factor.json"


class WitnessArtifactManifestTest(unittest.TestCase):
    """chainlink #35: check_artifact_manifest recognizes a canonical
    crate-scoped witness spec and compares its promotion-digest hash
    (validate_witness.witness_promotion_digest) instead of a plain byte
    hash. Self-contained tempdir fixture -- deliberately not the shared
    tests/fixtures/promotions/valid/ tree, which has no witnesses and no
    project descriptor at all."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        specs_dir = self.workspace / "crates" / "scheduler" / "specs"
        (specs_dir / "_witnesses").mkdir(parents=True)
        concept = concept_spec()
        concept["cluster"] = "scheduling"
        concept["queries"][0]["witness_required"] = True
        (specs_dir / "task_queue.json").write_text(json.dumps(concept))
        self.spec = witness_spec()
        (specs_dir / "_witnesses" / "task_queue.load_factor.json").write_text(json.dumps(self.spec))
        self.receipt_path = self.workspace / "specs" / "_promotions" / "scheduling.json"
        self.receipt_path.parent.mkdir(parents=True)
        self.receipt = {
            "schema": "promotion-receipt/1.0",
            "promotion_id": "PROM-SCHEDULING-001",
            "cluster": "scheduling",
            "reviewer": "alice",
            "policy_version": "reliance-policy@1.0",
            "schema_versions": {"witness": "1.0"},
            "accepted_at": "2026-09-08",
            "artifact_manifest": [
                {"path": WITNESS_RELATIVE_PATH, "hash": witness_promotion_digest(self.spec)},
            ],
        }

    def tearDown(self):
        self.tmp.cleanup()

    def _validate(self, receipt=None, descriptor=WITNESS_DESCRIPTOR):
        return validate_data(self.receipt_path, receipt or self.receipt, load_validator(), self.workspace, descriptor)

    def test_the_correct_promotion_digest_passes(self):
        findings = self._validate()
        self.assertEqual(findings, [], [str(f) for f in findings])

    def test_omitting_the_required_witness_entry_is_rejected(self):
        """External review, high severity: the completeness calculation
        (required_witness_paths) must be shared with the validator, not
        enforced only during accept_promotion() -- removing the
        required witness entry from an otherwise valid receipt used to
        produce zero findings, even with the descriptor supplied."""
        (self.workspace / "docs").mkdir()
        content = b"reliance policy\n"
        (self.workspace / "docs" / "reliance-policy.md").write_bytes(content)
        receipt = dict(self.receipt)
        receipt["artifact_manifest"] = [
            {"path": "docs/reliance-policy.md", "hash": "sha256:" + hashlib.sha256(content).hexdigest()},
        ]
        findings = self._validate(receipt)
        self.assertTrue(
            any(
                f.gate == "7.1" and "not included in this promotion's artifact_manifest" in f.reason
                and WITNESS_RELATIVE_PATH in f.reason
                for f in findings
            ),
            [str(f) for f in findings],
        )

    def test_completeness_is_skipped_without_a_descriptor(self):
        """The same optionality every other witness-aware check in this
        module already has: with no descriptor at all, the required set
        cannot be computed, so a receipt omitting the witness entirely
        is not flagged for completeness (though a witness-shaped entry
        that IS present would still fail closed, per
        _witness_promotion_hash_if_applicable's own behavior)."""
        (self.workspace / "docs").mkdir()
        content = b"reliance policy\n"
        (self.workspace / "docs" / "reliance-policy.md").write_bytes(content)
        receipt = dict(self.receipt)
        receipt["artifact_manifest"] = [
            {"path": "docs/reliance-policy.md", "hash": "sha256:" + hashlib.sha256(content).hexdigest()},
        ]
        findings = self._validate(receipt, descriptor=None)
        self.assertEqual(findings, [], [str(f) for f in findings])

    def test_completeness_is_scoped_to_the_receipts_own_cluster(self):
        """A witness declared in a different cluster than the receipt's
        own must not be required -- required_witness_paths() itself is
        cluster-scoped, and check_required_witnesses() passes the
        receipt's own `cluster` field through, not a hardcoded one."""
        (self.workspace / "docs").mkdir()
        content = b"reliance policy\n"
        (self.workspace / "docs" / "reliance-policy.md").write_bytes(content)
        receipt = dict(self.receipt)
        receipt["cluster"] = "other-cluster"
        receipt["artifact_manifest"] = [
            {"path": "docs/reliance-policy.md", "hash": "sha256:" + hashlib.sha256(content).hexdigest()},
        ]
        findings = self._validate(receipt)
        self.assertFalse(
            any(f.gate == "7.1" and "not included in this promotion's artifact_manifest" in f.reason
                for f in findings),
            [str(f) for f in findings],
        )

    def test_a_concept_spec_with_no_cluster_field_fails_closed(self):
        """External review, high severity: owning_cluster_for()'s own
        'unknown' fallback for a missing cluster field would silently
        exclude a declared feature from every cluster's required set --
        removing cluster from the concept spec, then removing the
        witness from artifact_manifest, used to yield zero findings
        even with the descriptor supplied."""
        specs_dir = self.workspace / "crates" / "scheduler" / "specs"
        concept = json.loads((specs_dir / "task_queue.json").read_text())
        del concept["cluster"]
        (specs_dir / "task_queue.json").write_text(json.dumps(concept))

        (self.workspace / "docs").mkdir()
        content = b"reliance policy\n"
        (self.workspace / "docs" / "reliance-policy.md").write_bytes(content)
        receipt = dict(self.receipt)
        receipt["artifact_manifest"] = [
            {"path": "docs/reliance-policy.md", "hash": "sha256:" + hashlib.sha256(content).hexdigest()},
        ]
        findings = self._validate(receipt)
        self.assertTrue(
            any(f.gate == "7.1" and "cannot determine the required witness set" in f.reason for f in findings),
            [str(f) for f in findings],
        )

    def test_a_schema_invalid_cluster_string_fails_closed(self):
        """External review, high severity: a non-empty-but-out-of-grammar
        cluster value (e.g. "SCHEDULING", uppercase) passed an
        isinstance-and-non-empty check, but no schema-valid receipt
        `cluster` (docs/promotion-receipt-schema.json's own
        ^[a-z][a-z0-9-]*$) could ever equal it -- setting the concept
        spec's own cluster to "SCHEDULING" and removing the witness
        entry again used to produce zero findings."""
        specs_dir = self.workspace / "crates" / "scheduler" / "specs"
        concept = json.loads((specs_dir / "task_queue.json").read_text())
        concept["cluster"] = "SCHEDULING"
        (specs_dir / "task_queue.json").write_text(json.dumps(concept))

        (self.workspace / "docs").mkdir()
        content = b"reliance policy\n"
        (self.workspace / "docs" / "reliance-policy.md").write_bytes(content)
        receipt = dict(self.receipt)
        receipt["artifact_manifest"] = [
            {"path": "docs/reliance-policy.md", "hash": "sha256:" + hashlib.sha256(content).hexdigest()},
        ]
        findings = self._validate(receipt)
        self.assertTrue(
            any(f.gate == "7.1" and "cannot determine the required witness set" in f.reason for f in findings),
            [str(f) for f in findings],
        )

    def test_a_symlinked_generated_svg_manifest_entry_is_still_refused(self):
        """External review, medium severity: classifying a generated
        review projection by its RESOLVED identity alone let a symlink
        planted at the canonical docs/witnesses/*.svg location, but
        pointing outside it, bypass refusal entirely."""
        (self.workspace / "docs" / "witnesses").mkdir(parents=True)
        ordinary = self.workspace / "ordinary_file.txt"
        ordinary_bytes = b"not a witness rendering"
        ordinary.write_bytes(ordinary_bytes)
        decoy = self.workspace / "docs" / "witnesses" / "decoy.svg"
        decoy.symlink_to(ordinary)

        receipt = dict(self.receipt)
        receipt["artifact_manifest"] = list(self.receipt["artifact_manifest"]) + [
            {
                "path": "docs/witnesses/decoy.svg",
                "hash": "sha256:" + hashlib.sha256(ordinary_bytes).hexdigest(),
            },
        ]
        findings = self._validate(receipt)
        self.assertTrue(
            any(f.gate == "7.1" and "review projection" in f.reason for f in findings),
            [str(f) for f in findings],
        )

    def test_a_plain_byte_hash_of_the_witness_file_is_rejected(self):
        """Proves the comparison is genuinely against the promotion
        digest, not silently falling back to a byte hash for a witness
        entry: the RAW file's own byte hash must NOT satisfy this check."""
        byte_hash = "sha256:" + hashlib.sha256(json.dumps(self.spec).encode()).hexdigest()
        receipt = dict(self.receipt)
        receipt["artifact_manifest"] = [{"path": WITNESS_RELATIVE_PATH, "hash": byte_hash}]
        findings = self._validate(receipt)
        self.assertTrue(any("acceptance is revoked" in f.reason for f in findings), [str(f) for f in findings])

    def test_render_hash_only_change_on_disk_still_passes(self):
        changed = dict(self.spec)
        changed["output"] = dict(self.spec["output"])
        changed["output"]["render_hash"] = "sha256:" + "4" * 64
        (self.workspace / WITNESS_RELATIVE_PATH).write_text(json.dumps(changed))
        findings = self._validate()
        self.assertEqual(findings, [], [str(f) for f in findings])

    def test_fixture_id_change_on_disk_revokes_acceptance(self):
        changed = json.loads(json.dumps(self.spec))
        changed["fixture"]["fixture_id"] = "FX-CHANGED"
        (self.workspace / WITNESS_RELATIVE_PATH).write_text(json.dumps(changed))
        findings = self._validate()
        self.assertTrue(any("acceptance is revoked" in f.reason for f in findings), [str(f) for f in findings])

    def test_an_invalid_witness_spec_fails_closed(self):
        broken = dict(self.spec)
        del broken["determinism"]
        (self.workspace / WITNESS_RELATIVE_PATH).write_text(json.dumps(broken))
        receipt = dict(self.receipt)
        receipt["artifact_manifest"] = [{"path": WITNESS_RELATIVE_PATH, "hash": witness_promotion_digest(broken)}]
        findings = self._validate(receipt)
        self.assertTrue(any("witness validator" in f.reason for f in findings), [str(f) for f in findings])

    def test_witness_shaped_path_with_no_descriptor_fails_closed(self):
        findings = self._validate(descriptor=None)
        self.assertTrue(
            any("no project descriptor was supplied" in f.reason for f in findings), [str(f) for f in findings]
        )

    def test_witness_shaped_path_outside_any_declared_crate_fails_closed(self):
        stray_dir = self.workspace / "junk" / "_witnesses"
        stray_dir.mkdir(parents=True)
        (stray_dir / "task_queue.load_factor.json").write_text(json.dumps(self.spec))
        receipt = dict(self.receipt)
        receipt["artifact_manifest"] = [
            {"path": "junk/_witnesses/task_queue.load_factor.json", "hash": witness_promotion_digest(self.spec)},
        ]
        findings = self._validate(receipt)
        self.assertTrue(
            any("not at any declared crate's own canonical witness directory" in f.reason for f in findings),
            [str(f) for f in findings],
        )

    def test_a_non_witness_path_is_unaffected_by_a_descriptor_being_present(self):
        """Backward compatibility: an ordinary artifact (not under a
        directory literally named _witnesses) keeps the exact plain
        byte-hash comparison regardless of whether a descriptor is
        supplied."""
        (self.workspace / "docs").mkdir()
        content = b"reliance policy\n"
        (self.workspace / "docs" / "reliance-policy.md").write_bytes(content)
        receipt = dict(self.receipt)
        receipt["artifact_manifest"] = [
            {"path": WITNESS_RELATIVE_PATH, "hash": witness_promotion_digest(self.spec)},
            {"path": "docs/reliance-policy.md", "hash": "sha256:" + hashlib.sha256(content).hexdigest()},
        ]
        findings = self._validate(receipt)
        self.assertEqual(findings, [], [str(f) for f in findings])


class StandaloneCliDescriptorTest(unittest.TestCase):
    """The standalone CLI's own --descriptor flag and its soft default
    (chainlink #35): existing witness-free fixtures must behave
    identically whether or not the flag is passed."""

    def test_default_descriptor_path_is_used_when_present(self):
        import validate_promotion_receipt

        tmp = tempfile.TemporaryDirectory()
        try:
            workspace = Path(tmp.name)
            specs_dir = workspace / "crates" / "scheduler" / "specs"
            (specs_dir / "_witnesses").mkdir(parents=True)
            concept = concept_spec()
            concept["cluster"] = "scheduling"
            concept["queries"][0]["witness_required"] = True
            (specs_dir / "task_queue.json").write_text(json.dumps(concept))
            spec = witness_spec()
            (specs_dir / "_witnesses" / "task_queue.load_factor.json").write_text(json.dumps(spec))
            (workspace / "project-descriptor.json").write_text(json.dumps(WITNESS_DESCRIPTOR))
            receipt_path = workspace / "specs" / "_promotions" / "scheduling.json"
            receipt_path.parent.mkdir(parents=True)
            receipt_path.write_text(json.dumps({
                "schema": "promotion-receipt/1.0",
                "promotion_id": "PROM-SCHEDULING-001",
                "cluster": "scheduling",
                "reviewer": "alice",
                "policy_version": "reliance-policy@1.0",
                "schema_versions": {"witness": "1.0"},
                "accepted_at": "2026-09-08",
                "artifact_manifest": [
                    {"path": WITNESS_RELATIVE_PATH, "hash": witness_promotion_digest(spec)},
                ],
            }))
            rc = validate_promotion_receipt.main([str(receipt_path), "--workspace-root", str(workspace)])
            self.assertEqual(rc, 0)
        finally:
            tmp.cleanup()

    def test_witness_free_fixture_is_unaffected_by_missing_descriptor(self):
        import validate_promotion_receipt
        rc = validate_promotion_receipt.main([str(RECEIPT_PATH), "--workspace-root", str(FIXTURE_ROOT)])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
