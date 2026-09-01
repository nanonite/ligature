import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from validate_protocol_debt import (  # noqa: E402
    find_protocol_debt_files,
    load_validator,
    main,
    validate,
    validate_crate,
    validate_data,
)

PROTOCOL_DEBT_PATH = Path("crates/scheduler/specs/_protocol_debt/I-SCHED-TQ-001.json")


def load_valid() -> dict:
    return {
        "schema_version": "1.0",
        "interaction_id": "I-SCHED-TQ-001",
        "rationale": "Multi-step handshake protocol, not yet modeled; no promoted work depends on it yet",
        "no_promoted_obligation_depends_on_protocol": True,
        "no_work_package_touches_its_path": True,
        "no_release_claim_includes_it": True,
        "tracking_issue": "chainlink:#99",
        "review": {"reviewer": "alice", "reviewed_at": "2026-08-30"},
    }


def run(data: dict, path: Path = PROTOCOL_DEBT_PATH):
    return validate_data(path, data, load_validator())


class ValidProtocolDebtTest(unittest.TestCase):
    def test_valid_protocol_debt_has_no_findings(self):
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

    def test_missing_tracking_issue_is_rejected(self):
        data = load_valid()
        del data["tracking_issue"]
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_empty_tracking_issue_is_rejected(self):
        data = load_valid()
        data["tracking_issue"] = ""
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_promotion_id_field_is_rejected(self):
        """additionalProperties: false -- a protocol-debt record never
        carries a promotion_id of its own (references are one-way,
        plan.md §7.1)."""
        data = load_valid()
        data["promotion_id"] = "PROM-SCHED-001"
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_each_attestation_field_must_be_true_not_merely_present(self):
        """plan.md §5.3: the record 'suffices only when all five hold' --
        a record cannot be filed admitting one of the three externally-
        facing conditions is false. const: true rejects False outright."""
        for field in [
            "no_promoted_obligation_depends_on_protocol",
            "no_work_package_touches_its_path",
            "no_release_claim_includes_it",
        ]:
            data = load_valid()
            data[field] = False
            findings = run(data)
            self.assertTrue(any(f.gate == "G1a" for f in findings), f"{field}: expected rejection")

    def test_each_attestation_field_is_required(self):
        for field in [
            "no_promoted_obligation_depends_on_protocol",
            "no_work_package_touches_its_path",
            "no_release_claim_includes_it",
        ]:
            data = load_valid()
            del data[field]
            findings = run(data)
            self.assertTrue(any(f.gate == "G1a" for f in findings), f"{field}: expected rejection")


class NamingTest(unittest.TestCase):
    def test_filename_stem_mismatch_is_rejected(self):
        data = load_valid()
        findings = run(data, path=Path("crates/scheduler/specs/_protocol_debt/wrong-name.json"))
        self.assertTrue(
            any("does not match interaction_id" in f.reason for f in findings), [str(f) for f in findings]
        )

    def test_nested_under_protocol_debt_dir_is_rejected(self):
        data = load_valid()
        findings = run(data, path=Path("crates/scheduler/specs/_protocol_debt/nested/I-SCHED-TQ-001.json"))
        self.assertTrue(any("not flat" in f.reason for f in findings), [str(f) for f in findings])

    def test_non_json_suffix_is_rejected(self):
        data = load_valid()
        findings = run(data, path=Path("crates/scheduler/specs/_protocol_debt/I-SCHED-TQ-001.yaml"))
        self.assertTrue(any("must be a .json file" in f.reason for f in findings), [str(f) for f in findings])


class FindProtocolDebtFilesTest(unittest.TestCase):
    def test_missing_root_raises(self):
        with self.assertRaises(FileNotFoundError):
            find_protocol_debt_files(Path("/nonexistent/does/not/exist"))

    def test_finds_flat_json_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_protocol_debt"
            d.mkdir(parents=True)
            (d / "I-X-001.json").write_text("{}")
            found = find_protocol_debt_files(Path(tmp))
            self.assertEqual(len(found), 1)


class ValidateReportsNonJsonFilesTest(unittest.TestCase):
    def test_recursive_scan_reports_unparseable_non_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_protocol_debt"
            d.mkdir(parents=True)
            (d / "I-X-001.yaml").write_text("not: valid: json: [[[")
            findings = validate(Path(tmp))
        self.assertTrue(findings, "a malformed non-.json artifact must be reported, not silently skipped")

    def test_recursive_scan_reports_well_formed_json_with_wrong_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_protocol_debt"
            d.mkdir(parents=True)
            (d / "I-SCHED-TQ-001.yaml").write_text(json.dumps(load_valid()))
            findings = validate(Path(tmp))
        self.assertTrue(any("must be a .json file" in f.reason for f in findings), [str(f) for f in findings])


class ValidateCrateTest(unittest.TestCase):
    """validate_crate(): the descriptor-driven scan pipeline.py's
    cmd_validate_protocol_debt uses -- discovers candidates crate-wide,
    then rejects any that don't sit directly under the crate's exact
    canonical directory."""

    def test_missing_crate_root_raises(self):
        with self.assertRaises(FileNotFoundError):
            validate_crate(Path("/nonexistent"), Path("/nonexistent/specs/_protocol_debt"))

    def test_finds_and_validates_files_in_the_canonical_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            crate_root = Path(tmp)
            canonical = crate_root / "specs" / "_protocol_debt"
            canonical.mkdir(parents=True)
            (canonical / "I-SCHED-TQ-001.json").write_text(json.dumps(load_valid()))
            findings = validate_crate(crate_root, canonical)
        self.assertEqual(findings, [], [str(f) for f in findings])

    def test_mislocated_but_otherwise_valid_artifact_is_rejected_by_location_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            crate_root = Path(tmp)
            stray = crate_root / "not_specs" / "_protocol_debt"
            stray.mkdir(parents=True)
            (stray / "I-SCHED-TQ-001.json").write_text(json.dumps(load_valid()))
            canonical = crate_root / "specs" / "_protocol_debt"  # never created
            findings = validate_crate(crate_root, canonical)
        self.assertTrue(
            any("not directly under the canonical directory" in f.reason for f in findings),
            [str(f) for f in findings],
        )

    def test_correctly_located_artifact_is_not_flagged_by_a_stray_sibling(self):
        with tempfile.TemporaryDirectory() as tmp:
            crate_root = Path(tmp)
            canonical = crate_root / "specs" / "_protocol_debt"
            canonical.mkdir(parents=True)
            (canonical / "I-SCHED-TQ-001.json").write_text(json.dumps(load_valid()))
            stray = crate_root / "not_specs" / "_protocol_debt"
            stray.mkdir(parents=True)
            (stray / "wrong-name.json").write_text(json.dumps(load_valid()))
            findings = validate_crate(crate_root, canonical)
        self.assertTrue(any("not directly under the canonical directory" in f.reason for f in findings))
        self.assertFalse(any(f.path.name == "I-SCHED-TQ-001.json" for f in findings))


class StandaloneCliMainTest(unittest.TestCase):
    def test_main_passes_for_valid_fixture_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_protocol_debt"
            d.mkdir(parents=True)
            (d / "I-SCHED-TQ-001.json").write_text(json.dumps(load_valid()))
            rc = main([tmp])
        self.assertEqual(rc, 0)

    def test_main_fails_for_naming_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_protocol_debt"
            d.mkdir(parents=True)
            (d / "wrong-name.json").write_text(json.dumps(load_valid()))
            rc = main([tmp])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
