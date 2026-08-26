import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from validate_boundary_naming import validate  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "boundaries"


class ValidateBoundaryNamingTest(unittest.TestCase):
    def test_missing_root_raises_instead_of_reporting_clean(self):
        with self.assertRaises(FileNotFoundError):
            validate(FIXTURES / "this_directory_does_not_exist")

    def test_valid_layout_has_no_violations(self):
        violations = validate(FIXTURES / "valid")
        self.assertEqual(violations, [], [str(v) for v in violations])

    def test_nested_file_is_a_violation(self):
        violations = validate(FIXTURES / "invalid_nested")
        self.assertTrue(violations)
        self.assertTrue(any("not flat" in str(v) for v in violations))

    def test_single_underscore_around_to_is_a_violation(self):
        violations = validate(FIXTURES / "invalid_single_underscore")
        self.assertTrue(violations)
        self.assertTrue(any("does not match" in str(v) for v in violations))

    def test_boundary_id_mismatch_is_a_violation(self):
        violations = validate(FIXTURES / "invalid_id_mismatch")
        self.assertTrue(violations)
        self.assertTrue(any("!=" in str(v) for v in violations))


if __name__ == "__main__":
    unittest.main()
