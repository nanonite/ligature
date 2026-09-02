import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from validate_conflict_resolution import (  # noqa: E402
    find_conflict_files,
    load_validator,
    main,
    validate,
    validate_data,
    validate_workspace,
)

CONFLICT_PATH = Path("specs/_conflicts/EC-004.json")


def load_valid() -> dict:
    return {
        "schema_version": "1.0",
        "conflict_id": "EC-004",
        "evidence": ["E-0143", "E-0201"],
        "status": "resolved",
        "resolution": {
            "selected_authority": "E-0201",
            "disposition_of_other": "incidental",
            "rationale": "compatibility policy: do not preserve the legacy defect",
        },
        "review": {"reviewer": "alice", "reviewed_at": "2026-08-30"},
    }


def run(data: dict, path: Path = CONFLICT_PATH, evidence_ids=None):
    return validate_data(path, data, load_validator(), evidence_ids)


class ValidConflictResolutionTest(unittest.TestCase):
    def test_valid_resolved_conflict_has_no_findings(self):
        findings = run(load_valid(), evidence_ids={"E-0143", "E-0201"})
        self.assertEqual(findings, [], [str(f) for f in findings])

    def test_valid_unresolved_conflict_has_only_the_expected_g11_finding(self):
        data = {
            "schema_version": "1.0",
            "conflict_id": "EC-004",
            "evidence": ["E-0143", "E-0201"],
            "status": "unresolved",
        }
        findings = run(data, evidence_ids={"E-0143", "E-0201"})
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].gate, "G11")


class G1aFailureTest(unittest.TestCase):
    def test_resolved_without_resolution_is_rejected(self):
        """The schema's own if/then: status == resolved requires resolution
        + review -- enforced structurally, matching plan.md §1.1's
        mode: port + port_source precedent."""
        data = load_valid()
        del data["resolution"]
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_resolved_without_review_is_rejected(self):
        data = load_valid()
        del data["review"]
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_too_few_evidence_ids_is_rejected(self):
        """A conflict needs at least two evidence items in tension."""
        data = load_valid()
        data["evidence"] = ["E-0143"]
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_bad_status_enum_value_is_rejected(self):
        data = load_valid()
        data["status"] = "vibes"
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_bad_disposition_of_other_enum_value_is_rejected(self):
        data = load_valid()
        data["resolution"]["disposition_of_other"] = "vibes"
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_promotion_id_field_is_rejected(self):
        data = load_valid()
        data["promotion_id"] = "PROM-SCHED-001"
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))


class SelectedAuthorityMembershipTest(unittest.TestCase):
    """plan.md §11's worked example: resolution.selected_authority (E-0201)
    is one of the ids listed in evidence ([E-0143, E-0201])."""

    def test_selected_authority_not_in_evidence_list_is_rejected(self):
        data = load_valid()
        data["resolution"]["selected_authority"] = "E-9999"
        findings = run(data, evidence_ids={"E-0143", "E-0201", "E-9999"})
        self.assertTrue(
            any("is not one of this record's own evidence ids" in f.reason for f in findings),
            [str(f) for f in findings],
        )

    def test_selected_authority_in_evidence_list_is_accepted(self):
        data = load_valid()
        findings = run(data, evidence_ids={"E-0143", "E-0201"})
        self.assertFalse(any("selected_authority" in f.reason for f in findings))


class EvidenceCrossReferenceTest(unittest.TestCase):
    """External-review-style discipline applied proactively: a conflict
    record referencing a nonexistent evidence id."""

    def test_no_context_supplied_is_an_info_note_not_a_failure(self):
        findings = run(load_valid(), evidence_ids=None)
        self.assertFalse(any(f.severity == "error" and "dangling reference" in f.reason for f in findings))
        self.assertTrue(any(f.severity == "info" for f in findings))

    def test_dangling_evidence_id_is_rejected(self):
        findings = run(load_valid(), evidence_ids={"E-0201"})  # E-0143 missing
        self.assertTrue(
            any("does not resolve to any real evidence record" in f.reason for f in findings),
            [str(f) for f in findings],
        )

    def test_both_ids_present_is_accepted(self):
        findings = run(load_valid(), evidence_ids={"E-0143", "E-0201"})
        self.assertFalse(any("dangling reference" in f.reason for f in findings))


class G11UnresolvedConflictTest(unittest.TestCase):
    """plan.md gate table §12, G11: 'unresolved evidence conflict' blocks
    promotion. plan.md §11's own words: 'G11 blocks unresolved conflicts
    only.'"""

    def test_unresolved_status_is_a_g11_error(self):
        data = {
            "schema_version": "1.0",
            "conflict_id": "EC-004",
            "evidence": ["E-0143", "E-0201"],
            "status": "unresolved",
        }
        findings = run(data, evidence_ids={"E-0143", "E-0201"})
        self.assertTrue(any(f.gate == "G11" and f.severity == "error" for f in findings))

    def test_resolved_status_has_no_g11_finding(self):
        findings = run(load_valid(), evidence_ids={"E-0143", "E-0201"})
        self.assertFalse(any(f.gate == "G11" for f in findings))


class NamingTest(unittest.TestCase):
    def test_filename_stem_mismatch_is_rejected(self):
        data = load_valid()
        findings = run(data, path=Path("specs/_conflicts/wrong-id.json"))
        self.assertTrue(
            any("does not match conflict_id" in f.reason for f in findings), [str(f) for f in findings]
        )

    def test_nested_under_conflicts_dir_is_rejected(self):
        data = load_valid()
        findings = run(data, path=Path("specs/_conflicts/nested/EC-004.json"))
        self.assertTrue(any("not flat" in f.reason for f in findings), [str(f) for f in findings])

    def test_non_json_suffix_is_rejected(self):
        data = load_valid()
        findings = run(data, path=Path("specs/_conflicts/EC-004.yaml"))
        self.assertTrue(any("must be a .json file" in f.reason for f in findings), [str(f) for f in findings])


class FindConflictFilesTest(unittest.TestCase):
    def test_missing_root_raises(self):
        with self.assertRaises(FileNotFoundError):
            find_conflict_files(Path("/nonexistent/does/not/exist"))

    def test_finds_flat_json_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_conflicts"
            d.mkdir(parents=True)
            (d / "EC-001.json").write_text("{}")
            found = find_conflict_files(Path(tmp))
            self.assertEqual(len(found), 1)


class ValidateWorkspaceTest(unittest.TestCase):
    def test_missing_workspace_root_raises(self):
        with self.assertRaises(FileNotFoundError):
            validate_workspace(Path("/nonexistent"), Path("/nonexistent/specs/_conflicts"), set())

    def test_finds_and_validates_files_in_the_canonical_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace_root = Path(tmp)
            canonical = workspace_root / "specs" / "_conflicts"
            canonical.mkdir(parents=True)
            (canonical / "EC-004.json").write_text(json.dumps(load_valid()))
            findings = validate_workspace(workspace_root, canonical, {"E-0143", "E-0201"})
        self.assertEqual(findings, [], [str(f) for f in findings])

    def test_mislocated_but_otherwise_valid_artifact_is_rejected_by_location_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace_root = Path(tmp)
            stray = workspace_root / "not_specs" / "_conflicts"
            stray.mkdir(parents=True)
            (stray / "EC-004.json").write_text(json.dumps(load_valid()))
            canonical = workspace_root / "specs" / "_conflicts"  # never created
            findings = validate_workspace(workspace_root, canonical, {"E-0143", "E-0201"})
        self.assertTrue(
            any("not directly under the canonical directory" in f.reason for f in findings),
            [str(f) for f in findings],
        )


class StandaloneCliMainTest(unittest.TestCase):
    def test_main_passes_for_valid_fixture_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_conflicts"
            d.mkdir(parents=True)
            (d / "EC-004.json").write_text(json.dumps(load_valid()))
            rc = main([tmp])
        self.assertEqual(rc, 0)

    def test_main_fails_for_unresolved_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_conflicts"
            d.mkdir(parents=True)
            data = {
                "schema_version": "1.0",
                "conflict_id": "EC-004",
                "evidence": ["E-0143", "E-0201"],
                "status": "unresolved",
            }
            (d / "EC-004.json").write_text(json.dumps(data))
            rc = main([tmp])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
