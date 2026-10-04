import sys
import tempfile
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


class StagedDraftInertTest(unittest.TestCase):
    """Chainlink #110: `find_boundary_files()` is the discovery function
    validate_boundary_contracts.py shares, and that module has skipped
    staged boundary drafts since #90 -- so a staged boundary draft was
    invisible to `validate` (which only reads `*.json`) and a *naming
    violation* to this scan, which has no `review` requirement to fail on
    and therefore reported it for what it is: "not a .json file (suffix:
    '.draft')". One artifact kind, two opposite dispositions from two
    commands reading the same directory. Excluding it here makes both
    agree, and matches every other draft-capable kind."""

    def _stage_draft(self, directory: Path, stem: str) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{stem}.json.draft").write_text("{}")

    def test_a_staged_boundary_draft_is_not_a_naming_violation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._stage_draft(root / "specs" / "_boundaries", "scheduler_dispatch__to__task_queue_pop_ready")
            violations = validate(root)
        self.assertEqual([str(v) for v in violations], [])

    def test_a_real_boundary_at_the_wrong_extension_is_still_a_violation(self):
        """The skip must be exactly `.draft`: this check exists to report
        a boundary artifact that is not at `*.json`, and that must
        survive."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            d = root / "specs" / "_boundaries"
            d.mkdir(parents=True)
            (d / "scheduler_dispatch__to__task_queue_pop_ready.yaml").write_text("{}")
            violations = validate(root)
        self.assertTrue(any("not a .json file" in str(v) for v in violations), [str(v) for v in violations])


if __name__ == "__main__":
    unittest.main()
