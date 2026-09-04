import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from validate_bridge import (  # noqa: E402
    find_bridge_files,
    load_validator,
    main,
    validate,
    validate_crate,
    validate_data,
)

BRIDGE_PATH = Path("crates/scheduler/specs/_bridges/BR-SCHED-TQ-001.json")
BOUNDARY_ID = "scheduler_dispatch__to__task_queue_pop_ready"


def load_valid() -> dict:
    return {
        "schema_version": "1.0",
        "bridge_id": "BR-SCHED-TQ-001",
        "boundary_id": BOUNDARY_ID,
        "callee_requirement": "TaskQueue.C001",
        "available_contract_facts": [
            {"obligation_id": "Scheduler.C010", "role": "caller-precondition"},
            {"obligation_id": "Scheduler.I001", "role": "invariant"},
        ],
        "target_expression": "TaskQueue.C001(args, callee_state)",
        "protocol_class": "pairwise",
        "bridge_logic": {
            "bindings": {"caller_self": "Scheduler", "args": {"now": "Time"}},
            "premises": ["caller_self.ready(args.now)", "Scheduler.I001(caller_self)"],
            "conclusion": {"obligation_id": "TaskQueue.C001"},
        },
        "review": {"reviewer": "alice", "reviewed_at": "2026-08-30"},
    }


def default_boundaries_by_id() -> dict:
    """The boundary load_valid()'s bridge references, matching by id --
    used as run()'s default so G1a/G1b-focused tests aren't incidentally
    polluted by the cross-reference check; tests of the cross-reference
    itself override this explicitly."""
    return {BOUNDARY_ID: {"callee_guarantees": ["TaskQueue.C001"]}}


_UNSET = object()


def run(data: dict, path: Path = BRIDGE_PATH, boundaries_by_id=_UNSET):
    if boundaries_by_id is _UNSET:
        boundaries_by_id = default_boundaries_by_id()
    return validate_data(path, data, load_validator(), boundaries_by_id)


class ValidBridgeTest(unittest.TestCase):
    def test_valid_bridge_has_no_findings(self):
        findings = run(load_valid())
        self.assertEqual(findings, [], [str(f) for f in findings])


class G1aFailureTest(unittest.TestCase):
    def test_missing_required_field_is_rejected(self):
        data = load_valid()
        del data["target_expression"]
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_empty_target_expression_is_rejected(self):
        data = load_valid()
        data["target_expression"] = ""
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_missing_review_is_rejected(self):
        data = load_valid()
        del data["review"]
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_bad_bridge_id_pattern_is_rejected(self):
        data = load_valid()
        data["bridge_id"] = "not-a-valid-id"
        findings = run(data, path=Path("crates/scheduler/specs/_bridges/not-a-valid-id.json"))
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_caller_postcondition_role_is_rejected(self):
        """The core structural enforcement of plan.md §8.2's semantic
        rule: a caller postcondition only holds after the caller
        returns, so it can never be an available call-site fact."""
        data = load_valid()
        data["available_contract_facts"].append(
            {"obligation_id": "Scheduler.C099", "role": "caller-postcondition"}
        )
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_unknown_top_level_field_is_rejected(self):
        data = load_valid()
        data["extra_field"] = "nope"
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_premises_must_be_non_empty(self):
        data = load_valid()
        data["bridge_logic"]["premises"] = []
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))


class NamingTest(unittest.TestCase):
    def test_filename_stem_mismatch_is_rejected(self):
        data = load_valid()
        findings = run(data, path=Path("crates/scheduler/specs/_bridges/BR-WRONG-001.json"))
        self.assertTrue(any("does not match bridge_id" in f.reason for f in findings), [str(f) for f in findings])

    def test_nested_under_bridges_dir_is_rejected(self):
        data = load_valid()
        findings = run(data, path=Path("crates/scheduler/specs/_bridges/nested/BR-SCHED-TQ-001.json"))
        self.assertTrue(any("not flat" in f.reason for f in findings), [str(f) for f in findings])

    def test_non_json_suffix_is_rejected(self):
        data = load_valid()
        findings = run(data, path=Path("crates/scheduler/specs/_bridges/BR-SCHED-TQ-001.yaml"))
        self.assertTrue(any("must be a .json file" in f.reason for f in findings), [str(f) for f in findings])


class ConclusionConsistencyTest(unittest.TestCase):
    """G1b, self-contained: callee_requirement and
    bridge_logic.conclusion.obligation_id must name the same
    obligation -- a human reviewing callee_requirement could otherwise
    approve a bridge whose typed logic proves something else."""

    def test_mismatched_conclusion_is_rejected(self):
        data = load_valid()
        data["bridge_logic"]["conclusion"]["obligation_id"] = "TaskQueue.C099"
        findings = run(data)
        self.assertTrue(
            any("does not match bridge_logic.conclusion.obligation_id" in f.reason for f in findings),
            [str(f) for f in findings],
        )

    def test_matching_conclusion_is_accepted(self):
        data = load_valid()
        findings = run(data)
        self.assertFalse(any("conclusion.obligation_id" in f.reason for f in findings))


class BoundaryCrossReferenceTest(unittest.TestCase):
    """G2: boundary_id must resolve to a real, valid boundary contract,
    and callee_requirement must be one of that boundary's own
    callee_guarantees."""

    def test_no_context_supplied_is_an_info_note_not_a_failure(self):
        findings = run(load_valid(), boundaries_by_id=None)
        self.assertFalse(any(f.gate == "G2" and f.severity == "error" for f in findings))
        self.assertTrue(any(f.gate == "G2" and f.severity == "info" for f in findings))

    def test_dangling_boundary_id_is_rejected(self):
        findings = run(load_valid(), boundaries_by_id={})
        self.assertTrue(
            any("does not resolve to any real boundary contract" in f.reason for f in findings),
            [str(f) for f in findings],
        )

    def test_callee_requirement_not_among_guarantees_is_rejected(self):
        findings = run(load_valid(), boundaries_by_id={BOUNDARY_ID: {"callee_guarantees": ["TaskQueue.C099"]}})
        self.assertTrue(
            any("not one of boundary_id" in f.reason for f in findings),
            [str(f) for f in findings],
        )

    def test_matching_boundary_is_accepted(self):
        findings = run(load_valid(), boundaries_by_id=default_boundaries_by_id())
        self.assertEqual(findings, [], [str(f) for f in findings])


class FindBridgeFilesTest(unittest.TestCase):
    def test_missing_root_raises(self):
        with self.assertRaises(FileNotFoundError):
            find_bridge_files(Path("/nonexistent/does/not/exist"))

    def test_finds_flat_json_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_bridges"
            d.mkdir(parents=True)
            (d / "BR-X-001.json").write_text("{}")
            found = find_bridge_files(Path(tmp))
            self.assertEqual(len(found), 1)


class ValidateReportsNonJsonFilesTest(unittest.TestCase):
    def test_recursive_scan_reports_unparseable_non_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_bridges"
            d.mkdir(parents=True)
            (d / "BR-X-001.yaml").write_text("not: valid: json: [[[")
            findings = validate(Path(tmp))
        self.assertTrue(findings, "a malformed non-.json artifact must be reported, not silently skipped")

    def test_recursive_scan_reports_well_formed_json_with_wrong_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_bridges"
            d.mkdir(parents=True)
            (d / "BR-SCHED-TQ-001.yaml").write_text(json.dumps(load_valid()))
            findings = validate(Path(tmp))
        self.assertTrue(any("must be a .json file" in f.reason for f in findings), [str(f) for f in findings])


class ValidateCrateTest(unittest.TestCase):
    """validate_crate(): the descriptor-driven scan pipeline.py's
    cmd_validate_bridge uses -- discovers candidates crate-wide, then
    rejects any that don't sit directly under the crate's exact
    canonical directory."""

    def test_missing_crate_root_raises(self):
        with self.assertRaises(FileNotFoundError):
            validate_crate(Path("/nonexistent"), Path("/nonexistent/specs/_bridges"), {})

    def test_finds_and_validates_files_in_the_canonical_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            crate_root = Path(tmp)
            canonical = crate_root / "specs" / "_bridges"
            canonical.mkdir(parents=True)
            (canonical / "BR-SCHED-TQ-001.json").write_text(json.dumps(load_valid()))
            findings = validate_crate(crate_root, canonical, default_boundaries_by_id())
        self.assertEqual(findings, [], [str(f) for f in findings])

    def test_mislocated_but_otherwise_valid_artifact_is_rejected_by_location_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            crate_root = Path(tmp)
            stray = crate_root / "not_specs" / "_bridges"
            stray.mkdir(parents=True)
            (stray / "BR-SCHED-TQ-001.json").write_text(json.dumps(load_valid()))
            canonical = crate_root / "specs" / "_bridges"  # never created
            findings = validate_crate(crate_root, canonical, default_boundaries_by_id())
        self.assertTrue(
            any("not directly under the canonical directory" in f.reason for f in findings),
            [str(f) for f in findings],
        )

    def test_correctly_located_artifact_is_not_flagged_by_a_stray_sibling(self):
        with tempfile.TemporaryDirectory() as tmp:
            crate_root = Path(tmp)
            canonical = crate_root / "specs" / "_bridges"
            canonical.mkdir(parents=True)
            (canonical / "BR-SCHED-TQ-001.json").write_text(json.dumps(load_valid()))
            stray = crate_root / "not_specs" / "_bridges"
            stray.mkdir(parents=True)
            (stray / "BR-WRONG-001.json").write_text(json.dumps(load_valid()))
            findings = validate_crate(crate_root, canonical, default_boundaries_by_id())
        self.assertTrue(any("not directly under the canonical directory" in f.reason for f in findings))
        self.assertFalse(any(f.path.name == "BR-SCHED-TQ-001.json" for f in findings))


class StandaloneCliMainTest(unittest.TestCase):
    def test_main_passes_for_valid_fixture_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_bridges"
            d.mkdir(parents=True)
            (d / "BR-SCHED-TQ-001.json").write_text(json.dumps(load_valid()))
            rc = main([tmp])
        self.assertEqual(rc, 0)

    def test_main_fails_for_naming_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_bridges"
            d.mkdir(parents=True)
            (d / "BR-WRONG-001.json").write_text(json.dumps(load_valid()))
            rc = main([tmp])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
