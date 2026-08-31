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


if __name__ == "__main__":
    unittest.main()
