import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from validate_interaction import (  # noqa: E402
    compute_eligibility,
    find_interaction_files,
    load_validator,
    main,
    validate_data,
)

INTERACTION_PATH = Path("crates/scheduler/specs/_interactions/I-SCHED-TQ-001.json")


def load_valid() -> dict:
    return {
        "schema_version": "1.0",
        "interaction_id": "I-SCHED-TQ-001",
        "caller": {"concept": "Scheduler", "method": "dispatch"},
        "callee": {"concept": "TaskQueue", "method": "pop_ready"},
        "edge_class": ["stateful", "cross-verifier"],
        "eligibility": "boundary-required",
        "rationale": "dispatch's postcondition depends on pop_ready's return discipline",
        "evidence_links": ["E-0143"],
        "review": {"reviewer": "alice", "reviewed_at": "2026-08-30"},
    }


def run(data: dict, path: Path = INTERACTION_PATH):
    return validate_data(path, data, load_validator())


class ComputeEligibilityTest(unittest.TestCase):
    """plan.md §5.2's table, checked directly: boundary-required wins over
    inform, which wins over ignore, when an edge carries more than one class."""

    def test_each_boundary_required_class_alone(self):
        for cls in [
            "cross-verifier", "cross-crate-public-api", "stateful",
            "error-panic-boundary", "ownership-transfer", "numeric-domain-boundary",
        ]:
            self.assertEqual(compute_eligibility([cls]), "boundary-required", cls)

    def test_each_inform_class_alone(self):
        for cls in ["pure-data-type-reference", "import-only"]:
            self.assertEqual(compute_eligibility([cls]), "inform", cls)

    def test_each_ignore_class_alone(self):
        for cls in ["marker-type", "phantom-type"]:
            self.assertEqual(compute_eligibility([cls]), "ignore", cls)

    def test_boundary_required_wins_over_inform_and_ignore(self):
        self.assertEqual(
            compute_eligibility(["marker-type", "pure-data-type-reference", "stateful"]),
            "boundary-required",
        )

    def test_inform_wins_over_ignore(self):
        self.assertEqual(compute_eligibility(["marker-type", "import-only"]), "inform")


class ValidInteractionTest(unittest.TestCase):
    def test_valid_boundary_required_has_no_findings(self):
        findings = run(load_valid())
        self.assertEqual(findings, [], [str(f) for f in findings])

    def test_valid_inform_has_no_findings(self):
        data = load_valid()
        data["interaction_id"] = "I-SCHED-TQ-002"
        data["edge_class"] = ["pure-data-type-reference"]
        data["eligibility"] = "inform"
        findings = run(data, path=Path("crates/scheduler/specs/_interactions/I-SCHED-TQ-002.json"))
        self.assertEqual(findings, [], [str(f) for f in findings])

    def test_valid_ignore_has_no_findings(self):
        data = load_valid()
        data["interaction_id"] = "I-SCHED-TQ-003"
        data["edge_class"] = ["marker-type"]
        data["eligibility"] = "ignore"
        findings = run(data, path=Path("crates/scheduler/specs/_interactions/I-SCHED-TQ-003.json"))
        self.assertEqual(findings, [], [str(f) for f in findings])


class G1aFailureTest(unittest.TestCase):
    def test_missing_required_field_is_rejected(self):
        data = load_valid()
        del data["rationale"]
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_unknown_edge_class_is_rejected(self):
        data = load_valid()
        data["edge_class"] = ["not-a-real-class"]
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_empty_edge_class_is_rejected(self):
        data = load_valid()
        data["edge_class"] = []
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_bad_eligibility_enum_value_is_rejected(self):
        data = load_valid()
        data["eligibility"] = "sometimes"
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))


class NamingTest(unittest.TestCase):
    def test_filename_stem_mismatch_is_rejected(self):
        data = load_valid()
        findings = run(data, path=Path("crates/scheduler/specs/_interactions/wrong-name.json"))
        self.assertTrue(
            any("does not match interaction_id" in f.reason for f in findings), [str(f) for f in findings]
        )

    def test_nested_under_interactions_dir_is_rejected(self):
        data = load_valid()
        findings = run(data, path=Path("crates/scheduler/specs/_interactions/nested/I-SCHED-TQ-001.json"))
        self.assertTrue(any("not flat" in f.reason for f in findings), [str(f) for f in findings])


class ComputedEligibilityMismatchTest(unittest.TestCase):
    def test_understated_eligibility_is_rejected(self):
        """plan.md §5.2: 'a proposing model cannot mark a stateful
        cross-verifier edge ignore.'"""
        data = load_valid()
        data["eligibility"] = "ignore"
        findings = run(data)
        self.assertTrue(
            any("does not match the value computed" in f.reason for f in findings), [str(f) for f in findings]
        )

    def test_overstated_eligibility_is_also_rejected(self):
        """Disagreement is rejected in both directions, not just under-claiming."""
        data = load_valid()
        data["edge_class"] = ["marker-type"]
        data["eligibility"] = "boundary-required"
        findings = run(data)
        self.assertTrue(
            any("does not match the value computed" in f.reason for f in findings), [str(f) for f in findings]
        )


class FindInteractionFilesTest(unittest.TestCase):
    def test_missing_root_raises(self):
        with self.assertRaises(FileNotFoundError):
            find_interaction_files(Path("/nonexistent/does/not/exist"))

    def test_finds_flat_json_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_interactions"
            d.mkdir(parents=True)
            (d / "I-X-001.json").write_text("{}")
            found = find_interaction_files(Path(tmp))
            self.assertEqual(len(found), 1)


class StandaloneCliMainTest(unittest.TestCase):
    """validate_interaction.main() itself, not just pipeline.main() --
    same discipline established for validate_promotion_receipt.py: an
    untested entrypoint is exactly how a wiring gap ships invisibly."""

    def test_main_passes_for_valid_fixture_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_interactions"
            d.mkdir(parents=True)
            (d / "I-SCHED-TQ-001.json").write_text(json.dumps(load_valid()))
            rc = main([tmp])
        self.assertEqual(rc, 0)

    def test_main_fails_for_computed_eligibility_mismatch(self):
        data = load_valid()
        data["eligibility"] = "ignore"
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_interactions"
            d.mkdir(parents=True)
            (d / "I-SCHED-TQ-001.json").write_text(json.dumps(data))
            rc = main([tmp])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
