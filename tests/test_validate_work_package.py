import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from validate_work_package import validate_data, load_validator  # noqa: E402

FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "work_packages" / "valid"
MANIFEST_PATH = FIXTURE_ROOT / "ci" / "manifest" / "WP-SCHED-001.json"
SPECS_SEARCH_ROOT = FIXTURE_ROOT / "crates" / "scheduler" / "specs"


def load_valid() -> dict:
    return json.loads(MANIFEST_PATH.read_text())


def run(data: dict, workspace_root: Path = FIXTURE_ROOT, specs_search_root: Path | None = SPECS_SEARCH_ROOT):
    validator = load_validator()
    return validate_data(MANIFEST_PATH, data, validator, workspace_root, specs_search_root)


def errors_of(findings):
    return [f for f in findings if f.severity == "error"]


def infos_of(findings):
    return [f for f in findings if f.severity == "info"]


class ValidWorkPackageTest(unittest.TestCase):
    def test_valid_manifest_has_no_errors(self):
        findings = run(load_valid())
        self.assertEqual(errors_of(findings), [], [str(f) for f in errors_of(findings)])

    def test_valid_manifest_reports_the_two_known_gaps_as_info(self):
        findings = run(load_valid())
        infos = infos_of(findings)
        self.assertEqual(len(infos), 2)
        reasons = " ".join(f.reason for f in infos)
        self.assertIn("promotion_id", reasons)
        self.assertIn("allowed_write_set", reasons)


class G1aFailureTest(unittest.TestCase):
    def test_missing_required_field_is_rejected(self):
        data = load_valid()
        del data["gates"]
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_wildcard_harness_name_is_rejected(self):
        data = load_valid()
        data["definition_of_done"]["provided_guarantees"][0]["harness"] = "task_queue_*"
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_forbidden_action_outside_enum_is_rejected(self):
        data = load_valid()
        data["failure_policy"]["forbidden"].append("do_whatever_you_want")
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))


class GateIntegrityTest(unittest.TestCase):
    def test_hash_mismatch_is_rejected(self):
        data = load_valid()
        data["gate_integrity"][0]["hash"] = "sha256:" + "0" * 64
        findings = run(data)
        errors = errors_of(findings)
        self.assertTrue(any(f.gate == "G13" and "mismatch" in f.reason for f in errors))

    def test_missing_runner_file_is_rejected(self):
        data = load_valid()
        data["gate_integrity"][0]["runner"] = "scripts/does_not_exist.py"
        findings = run(data)
        errors = errors_of(findings)
        self.assertTrue(any(f.gate == "G13" and "not found" in f.reason for f in errors))

    def test_correct_hash_passes(self):
        findings = run(load_valid())
        self.assertFalse(any(f.gate == "G13" for f in errors_of(findings)))


class WriteSetDisjointnessTest(unittest.TestCase):
    def test_overlapping_allowed_and_protected_is_rejected(self):
        data = load_valid()
        data["write_policy"]["protected_write_set"].append("crates/scheduler/src/")
        findings = run(data)
        errors = errors_of(findings)
        self.assertTrue(any("listed in both" in f.reason for f in errors))


class TrustedAssumptionResolutionTest(unittest.TestCase):
    def test_dangling_boundary_reference_is_rejected(self):
        data = load_valid()
        data["definition_of_done"]["trusted_assumptions"][0]["assumption_ref"]["boundary_id"] = "no_such_boundary"
        findings = run(data)
        errors = errors_of(findings)
        self.assertTrue(any("does not resolve" in f.reason for f in errors))

    def test_wrong_tracking_issue_is_rejected(self):
        data = load_valid()
        data["definition_of_done"]["trusted_assumptions"][0]["assumption_ref"]["tracking_issue"] = "chainlink:999"
        findings = run(data)
        errors = errors_of(findings)
        self.assertTrue(any("no assumption in it matches" in f.reason for f in errors))

    def test_wrong_hash_is_rejected(self):
        data = load_valid()
        data["definition_of_done"]["trusted_assumptions"][0]["assumption_ref"]["assumption_hash"] = "sha256:" + "9" * 64
        findings = run(data)
        errors = errors_of(findings)
        self.assertTrue(any("no assumption in it matches" in f.reason for f in errors))

    def test_no_search_root_degrades_to_info_not_silent_pass(self):
        findings = run(load_valid(), specs_search_root=None)
        infos = infos_of(findings)
        self.assertTrue(any("trusted_assumptions unverifiable" in f.reason for f in infos))

    def test_empty_trusted_assumptions_needs_no_resolution(self):
        data = load_valid()
        data["definition_of_done"]["trusted_assumptions"] = []
        findings = run(data, specs_search_root=None)
        self.assertFalse(any("unverifiable" in f.reason for f in infos_of(findings)))


if __name__ == "__main__":
    unittest.main()
