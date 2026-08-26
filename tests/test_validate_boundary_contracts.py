import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from validate_boundary_contracts import validate  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "boundary_contracts"


def gates_hit(findings) -> set:
    return {f.gate for f in findings}


class ValidateBoundaryContractsTest(unittest.TestCase):
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
        unverifiable, not a false pass or a crash."""
        fixture = FIXTURES / "g2_plus_applies_to_mismatch"
        findings = validate(fixture, specs_search_root=None)
        self.assertEqual(findings, [], [str(f) for f in findings])


if __name__ == "__main__":
    unittest.main()
