import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from validate_boundary_contracts import validate, load_validator  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "boundary_contracts"

VALID_INSTANCE = {
    "schema_version": "1.0",
    "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
    "caller": {"concept": "Scheduler", "method": "dispatch"},
    "callee": {"concept": "TaskQueue", "method": "pop_ready"},
    "callee_guarantees": ["TaskQueue.C003"],
    "review": {"reviewer": "alice", "reviewed_at": "2026-08-25"},
}


def gates_hit(findings) -> set:
    return {f.gate for f in findings}


class BoundaryContractSchemaReviewFieldTest(unittest.TestCase):
    """Review finding (round 2, medium severity): review authority wasn't
    schema-enforced -- reviewer: "" and reviewed_at: "not-a-date" both
    validated."""

    @classmethod
    def setUpClass(cls):
        cls.validator = load_validator()

    def test_valid_instance_has_no_errors(self):
        self.assertEqual(list(self.validator.iter_errors(VALID_INSTANCE)), [])

    def test_empty_reviewer_is_rejected(self):
        instance = json.loads(json.dumps(VALID_INSTANCE))
        instance["review"]["reviewer"] = ""
        self.assertTrue(list(self.validator.iter_errors(instance)))

    def test_malformed_reviewed_at_is_rejected(self):
        instance = json.loads(json.dumps(VALID_INSTANCE))
        instance["review"]["reviewed_at"] = "not-a-date"
        self.assertTrue(list(self.validator.iter_errors(instance)))


class ValidateBoundaryContractsTest(unittest.TestCase):
    def test_missing_root_raises_instead_of_reporting_clean(self):
        """Review finding (round 2, high severity): Path.glob() on a
        nonexistent directory returns [] with no error, so a typo'd
        crate_dir used to produce 'OK: all boundary contracts pass' --
        indistinguishable from a real, fully-clean scan."""
        with self.assertRaises(FileNotFoundError):
            validate(FIXTURES / "this_directory_does_not_exist")

    def test_valid_boundary_has_no_findings(self):
        findings = validate(
            FIXTURES / "valid_crate",
            specs_search_root=FIXTURES / "valid_crate" / "specs",
        )
        self.assertEqual(findings, [], [str(f) for f in findings])

    def test_g1a_missing_review_is_rejected(self):
        findings = validate(FIXTURES / "g1a_fail")
        self.assertIn("G1a", gates_hit(findings))
        # Should not proceed to G1b/G2+ on a schema-invalid document.
        self.assertNotIn("G1b", gates_hit(findings))
        self.assertNotIn("G2+", gates_hit(findings))

    def test_g1b_filename_body_mismatch_is_rejected(self):
        findings = validate(FIXTURES / "g1b_filename_mismatch")
        self.assertIn("G1b", gates_hit(findings))
        self.assertTrue(any("does not match caller/callee" in f.reason for f in findings))

    def test_g1b_duplicate_tracking_issue_is_rejected(self):
        findings = validate(FIXTURES / "g1b_duplicate_tracking_issue")
        self.assertIn("G1b", gates_hit(findings))
        self.assertTrue(any("used by 2 assumptions" in f.reason for f in findings))

    def test_g2_plus_adversary_as_guarantee_is_rejected(self):
        """The deliberate-failure regression fixture (chainlink #12):
        proves G2+ actually fails before it's trusted to pass."""
        findings = validate(FIXTURES / "g2_plus_adversary_as_guarantee")
        self.assertIn("G2+", gates_hit(findings))
        self.assertTrue(any("adversary case, not a guarantee" in f.reason for f in findings))

    def test_g2_plus_role_mismatch_is_rejected(self):
        findings = validate(FIXTURES / "g2_plus_role_mismatch")
        self.assertIn("G2+", gates_hit(findings))
        self.assertTrue(any("role safety" in f.reason for f in findings))

    def test_g2_plus_applies_to_mismatch_is_rejected_when_resolvable(self):
        fixture = FIXTURES / "g2_plus_applies_to_mismatch"
        findings = validate(fixture, specs_search_root=fixture / "specs")
        self.assertIn("G2+", gates_hit(findings))
        self.assertTrue(any("applies_to" in f.reason for f in findings))

    def test_g2_plus_applies_to_check_degrades_gracefully_without_search_root(self):
        """Without --specs-search-root, the applies_to mismatch is
        unverifiable -- not a false pass, not a crash, and (D4) not silent
        either: a visible, non-blocking info finding."""
        fixture = FIXTURES / "g2_plus_applies_to_mismatch"
        findings = validate(fixture, specs_search_root=None)
        errors = [f for f in findings if f.severity == "error"]
        infos = [f for f in findings if f.severity == "info"]
        self.assertEqual(errors, [], [str(f) for f in errors])
        self.assertEqual(len(infos), 1)
        self.assertIn("unverifiable", infos[0].reason)
        self.assertIn("no --specs-search-root given", infos[0].reason)


if __name__ == "__main__":
    unittest.main()
