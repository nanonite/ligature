import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from validate_exemption import find_exemption_files, load_validator, main, validate_data  # noqa: E402

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
