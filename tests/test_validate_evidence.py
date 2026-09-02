import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from validate_evidence import (  # noqa: E402
    find_evidence_files,
    load_validator,
    main,
    valid_evidence_ids,
    validate,
    validate_data,
    validate_workspace,
)

EVIDENCE_PATH = Path("evidence/E-0143.json")


def load_valid() -> dict:
    return {
        "schema_version": "1.0",
        "id": "E-0143",
        "kind": "source-artifact",
        "claim": "pop_ready returns None only when no task has deadline <= now",
        "origin": {
            "repository": "https://example.com/beast-rs",
            "commit": "a1b2c3d",
            "symbol": "TaskQueue::pop_ready",
            "path": "src/queue.cpp",
            "content_hash": "sha256:" + "0" * 64,
            "line_hint": "118-160",
        },
        "semantic_disposition": "required",
        "lifecycle": "accepted",
        "confidence": "high",
        "mode": "P",
    }


def run(data: dict, path: Path = EVIDENCE_PATH):
    return validate_data(path, data, load_validator())


class ValidEvidenceTest(unittest.TestCase):
    def test_valid_evidence_has_no_findings(self):
        findings = run(load_valid())
        self.assertEqual(findings, [], [str(f) for f in findings])


class G1aFailureTest(unittest.TestCase):
    def test_missing_claim_is_rejected(self):
        """plan.md §11: 'The claim field is required.'"""
        data = load_valid()
        del data["claim"]
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_empty_claim_is_rejected(self):
        data = load_valid()
        data["claim"] = ""
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_bad_kind_enum_value_is_rejected(self):
        data = load_valid()
        data["kind"] = "vibes"
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_bad_semantic_disposition_enum_value_is_rejected(self):
        data = load_valid()
        data["semantic_disposition"] = "vibes"
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_bad_lifecycle_enum_value_is_rejected(self):
        data = load_valid()
        data["lifecycle"] = "vibes"
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_semantic_disposition_and_lifecycle_are_independent_axes(self):
        """plan.md §11: splitting the two axes stops 'aspirational' acting
        as both authority and lifecycle state -- confirmed by every valid
        combination of the two independently-enumerated fields validating."""
        for disposition in ["required", "incidental", "bug-compat", "unspecified"]:
            for lifecycle in ["accepted", "aspirational", "deferred", "rejected", "out-of-scope"]:
                data = load_valid()
                data["semantic_disposition"] = disposition
                data["lifecycle"] = lifecycle
                findings = run(data)
                self.assertEqual(findings, [], f"{disposition}/{lifecycle}: {[str(f) for f in findings]}")

    def test_missing_origin_field_is_rejected(self):
        for field in ["repository", "commit", "symbol", "path", "content_hash", "line_hint"]:
            data = load_valid()
            del data["origin"][field]
            findings = run(data)
            self.assertTrue(any(f.gate == "G1a" for f in findings), f"{field}: expected rejection")

    def test_bad_content_hash_pattern_is_rejected(self):
        data = load_valid()
        data["origin"]["content_hash"] = "not-a-hash"
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_single_line_hint_is_accepted(self):
        data = load_valid()
        data["origin"]["line_hint"] = "118"
        findings = run(data)
        self.assertEqual(findings, [], [str(f) for f in findings])

    def test_malformed_line_hint_is_rejected(self):
        data = load_valid()
        data["origin"]["line_hint"] = "abc"
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_review_field_is_rejected(self):
        """docs/evidence-schema.json deliberately has no review property --
        evidence is Stage 0, LLM-proposed, non-normative (plan.md §7.2's
        human-checkpoint list names 'evidence-conflict resolution', not
        evidence itself)."""
        data = load_valid()
        data["review"] = {"reviewer": "alice", "reviewed_at": "2026-08-30"}
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))


class NamingTest(unittest.TestCase):
    def test_filename_stem_mismatch_is_rejected(self):
        data = load_valid()
        findings = run(data, path=Path("evidence/wrong-id.json"))
        self.assertTrue(any("does not match id" in f.reason for f in findings), [str(f) for f in findings])

    def test_nested_under_evidence_dir_is_rejected(self):
        data = load_valid()
        findings = run(data, path=Path("evidence/nested/E-0143.json"))
        self.assertTrue(any("not flat" in f.reason for f in findings), [str(f) for f in findings])

    def test_non_json_suffix_is_rejected(self):
        data = load_valid()
        findings = run(data, path=Path("evidence/E-0143.yaml"))
        self.assertTrue(any("must be a .json file" in f.reason for f in findings), [str(f) for f in findings])


class FindEvidenceFilesTest(unittest.TestCase):
    def test_missing_root_raises(self):
        with self.assertRaises(FileNotFoundError):
            find_evidence_files(Path("/nonexistent/does/not/exist"))

    def test_finds_flat_json_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "evidence"
            d.mkdir(parents=True)
            (d / "E-0001.json").write_text("{}")
            found = find_evidence_files(Path(tmp))
            self.assertEqual(len(found), 1)


class ValidateWorkspaceTest(unittest.TestCase):
    """validate_workspace(): the descriptor-driven scan pipeline.py's
    cmd_validate_evidence uses -- discovers candidates workspace-wide,
    then rejects any that don't sit directly under the exact canonical
    evidence/ directory (evidence is workspace-level, not crate-scoped)."""

    def test_missing_workspace_root_raises(self):
        with self.assertRaises(FileNotFoundError):
            validate_workspace(Path("/nonexistent"), Path("/nonexistent/evidence"))

    def test_finds_and_validates_files_in_the_canonical_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace_root = Path(tmp)
            canonical = workspace_root / "evidence"
            canonical.mkdir(parents=True)
            (canonical / "E-0143.json").write_text(json.dumps(load_valid()))
            findings = validate_workspace(workspace_root, canonical)
        self.assertEqual(findings, [], [str(f) for f in findings])

    def test_mislocated_but_otherwise_valid_artifact_is_rejected_by_location_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace_root = Path(tmp)
            stray = workspace_root / "docs" / "evidence"
            stray.mkdir(parents=True)
            (stray / "E-0143.json").write_text(json.dumps(load_valid()))
            canonical = workspace_root / "evidence"  # never created
            findings = validate_workspace(workspace_root, canonical)
        self.assertTrue(
            any("not directly under the canonical directory" in f.reason for f in findings),
            [str(f) for f in findings],
        )


class ValidEvidenceIdsTest(unittest.TestCase):
    """Used by validate_conflict_resolution.py's own cross-reference."""

    def test_missing_dir_returns_empty_set(self):
        self.assertEqual(valid_evidence_ids(Path("/nonexistent")), set())

    def test_loads_ids_from_valid_correctly_located_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "evidence"
            d.mkdir()
            (d / "E-0143.json").write_text(json.dumps(load_valid()))
            self.assertEqual(valid_evidence_ids(d), {"E-0143"})

    def test_skips_unparseable_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "evidence"
            d.mkdir()
            (d / "garbage.json").write_text("not json [[[")
            self.assertEqual(valid_evidence_ids(d), set())

    def test_schema_invalid_evidence_is_not_trusted(self):
        """External review, high severity: a file containing only
        {"id": "E-0143"} (missing every other required field) previously
        satisfied a conflict-resolution's cross-reference. Reproduced
        directly before fixing -- must now be excluded entirely."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "evidence"
            d.mkdir()
            (d / "E-0143.json").write_text(json.dumps({"id": "E-0143"}))
            self.assertEqual(valid_evidence_ids(d), set())

    def test_mislocated_or_misnamed_evidence_is_not_trusted(self):
        """A file that would fail check_naming (id != filename stem) is
        excluded even though it's otherwise schema-valid."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "evidence"
            d.mkdir()
            (d / "wrong-name.json").write_text(json.dumps(load_valid()))
            self.assertEqual(valid_evidence_ids(d), set())


class StandaloneCliMainTest(unittest.TestCase):
    def test_main_passes_for_valid_fixture_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "evidence"
            d.mkdir(parents=True)
            (d / "E-0143.json").write_text(json.dumps(load_valid()))
            rc = main([tmp])
        self.assertEqual(rc, 0)

    def test_main_fails_for_naming_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "evidence"
            d.mkdir(parents=True)
            (d / "wrong-id.json").write_text(json.dumps(load_valid()))
            rc = main([tmp])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
