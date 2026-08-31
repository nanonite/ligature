import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from validate_promotion_receipt import validate_data, validate_file, load_validator  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
