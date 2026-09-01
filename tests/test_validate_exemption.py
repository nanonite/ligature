import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from validate_exemption import find_exemption_files, load_validator, main, validate, validate_data, validate_dir  # noqa: E402

EXEMPTION_PATH = Path("crates/scheduler/specs/_exemptions/I-SCHED-TQ-001.json")


def load_valid() -> dict:
    return {
        "schema_version": "1.0",
        "interaction_id": "I-SCHED-TQ-001",
        "rationale": "Prototype scaffolding boundary, tracked for removal before release (chainlink:#41)",
        "review": {"reviewer": "alice", "reviewed_at": "2026-08-30"},
    }


def run(data: dict, path: Path = EXEMPTION_PATH):
    return validate_data(path, data, load_validator())


class ValidExemptionTest(unittest.TestCase):
    def test_valid_exemption_has_no_findings(self):
        findings = run(load_valid())
        self.assertEqual(findings, [], [str(f) for f in findings])


class G1aFailureTest(unittest.TestCase):
    def test_missing_required_field_is_rejected(self):
        data = load_valid()
        del data["rationale"]
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_empty_rationale_is_rejected(self):
        data = load_valid()
        data["rationale"] = ""
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_missing_review_is_rejected(self):
        data = load_valid()
        del data["review"]
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_promotion_id_field_is_rejected(self):
        """additionalProperties: false -- an exemption never carries a
        promotion_id of its own (references are one-way, plan.md §7.1)."""
        data = load_valid()
        data["promotion_id"] = "PROM-SCHED-001"
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))


class NamingTest(unittest.TestCase):
    def test_filename_stem_mismatch_is_rejected(self):
        data = load_valid()
        findings = run(data, path=Path("crates/scheduler/specs/_exemptions/wrong-name.json"))
        self.assertTrue(
            any("does not match interaction_id" in f.reason for f in findings), [str(f) for f in findings]
        )

    def test_nested_under_exemptions_dir_is_rejected(self):
        data = load_valid()
        findings = run(data, path=Path("crates/scheduler/specs/_exemptions/nested/I-SCHED-TQ-001.json"))
        self.assertTrue(any("not flat" in f.reason for f in findings), [str(f) for f in findings])

    def test_non_json_suffix_is_rejected(self):
        """External review, high severity: check_naming never checked the
        file suffix, so a schema-valid exemption at I-SCHED-TQ-001.yaml
        produced zero findings."""
        data = load_valid()
        findings = run(data, path=Path("crates/scheduler/specs/_exemptions/I-SCHED-TQ-001.yaml"))
        self.assertTrue(any("must be a .json file" in f.reason for f in findings), [str(f) for f in findings])


class FindExemptionFilesTest(unittest.TestCase):
    def test_missing_root_raises(self):
        with self.assertRaises(FileNotFoundError):
            find_exemption_files(Path("/nonexistent/does/not/exist"))

    def test_finds_flat_json_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_exemptions"
            d.mkdir(parents=True)
            (d / "I-X-001.json").write_text("{}")
            found = find_exemption_files(Path(tmp))
            self.assertEqual(len(found), 1)


class ValidateReportsNonJsonFilesTest(unittest.TestCase):
    """External review, high severity: validate() used to silently
    `continue` past any non-.json file under a real _exemptions
    directory, producing an overall OK with zero findings."""

    def test_recursive_scan_reports_unparseable_non_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_exemptions"
            d.mkdir(parents=True)
            (d / "I-X-001.yaml").write_text("not: valid: json: [[[")
            findings = validate(Path(tmp))
        self.assertTrue(findings, "a malformed non-.json artifact must be reported, not silently skipped")

    def test_recursive_scan_reports_well_formed_json_with_wrong_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_exemptions"
            d.mkdir(parents=True)
            (d / "I-SCHED-TQ-001.yaml").write_text(json.dumps(load_valid()))
            findings = validate(Path(tmp))
        self.assertTrue(any("must be a .json file" in f.reason for f in findings), [str(f) for f in findings])


class ValidateDirTest(unittest.TestCase):
    """validate_dir(): the anchored scan pipeline.py's cmd_validate_exemption
    uses -- given a crate's exact canonical directory, not a crate-wide
    recursive search for anything named _exemptions."""

    def test_missing_directory_returns_no_findings(self):
        self.assertEqual(validate_dir(Path("/nonexistent/specs/_exemptions")), [])

    def test_finds_and_validates_files_directly_in_the_given_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "_exemptions"
            d.mkdir()
            (d / "I-SCHED-TQ-001.json").write_text(json.dumps(load_valid()))
            findings = validate_dir(d)
        self.assertEqual(findings, [], [str(f) for f in findings])

    def test_does_not_search_outside_the_given_directory(self):
        """External review, medium severity, the exact anchoring
        guarantee: a sibling _exemptions directory elsewhere in the
        crate is never consulted, unlike validate()'s crate-wide search."""
        with tempfile.TemporaryDirectory() as tmp:
            stray = Path(tmp) / "not_specs" / "_exemptions"
            stray.mkdir(parents=True)
            (stray / "wrong-name.json").write_text(json.dumps(load_valid()))  # would fail G1b if examined
            canonical = Path(tmp) / "specs" / "_exemptions"
            findings = validate_dir(canonical)
        self.assertEqual(findings, [])


class StandaloneCliMainTest(unittest.TestCase):
    def test_main_passes_for_valid_fixture_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_exemptions"
            d.mkdir(parents=True)
            (d / "I-SCHED-TQ-001.json").write_text(json.dumps(load_valid()))
            rc = main([tmp])
        self.assertEqual(rc, 0)

    def test_main_fails_for_naming_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_exemptions"
            d.mkdir(parents=True)
            (d / "wrong-name.json").write_text(json.dumps(load_valid()))
            rc = main([tmp])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
