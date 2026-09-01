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
    validate,
    validate_crate,
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


def load_valid_reliance() -> dict:
    return {
        "obligation_id": "TaskQueue.C003",
        "required_assurance": {
            "required_claims": ["postcondition-holds"],
            "accepted_evidence_kinds": ["creusot-deductive-check"],
            "minimum_scope": {"input_domain": "queue_len_le_8", "feature_set": "default"},
            "trust_policy": {"assumptions_allowed": []},
        },
    }


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

    def test_non_json_suffix_is_rejected(self):
        """External review, high severity: check_naming never checked the
        file suffix, so a schema-valid interaction at I-SCHED-TQ-001.yaml
        produced zero findings."""
        data = load_valid()
        findings = run(data, path=Path("crates/scheduler/specs/_interactions/I-SCHED-TQ-001.yaml"))
        self.assertTrue(any("must be a .json file" in f.reason for f in findings), [str(f) for f in findings])


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


class RelianceRequiredAssuranceTest(unittest.TestCase):
    """#17: reliances[].required_assurance, applying plan.md §8.1's
    claim / evidence-method / scope / trust type split."""

    def test_reliances_is_optional(self):
        """An inform/ignore-eligible edge has nothing to declare a
        reliance on -- omitting the field entirely must still validate."""
        data = load_valid()
        self.assertNotIn("reliances", data)
        findings = run(data)
        self.assertEqual(findings, [], [str(f) for f in findings])

    def test_valid_reliance_has_no_findings(self):
        data = load_valid()
        data["reliances"] = [load_valid_reliance()]
        findings = run(data)
        self.assertEqual(findings, [], [str(f) for f in findings])

    def test_bad_obligation_id_pattern_is_rejected(self):
        data = load_valid()
        reliance = load_valid_reliance()
        reliance["obligation_id"] = "not-a-real-obligation-id"
        data["reliances"] = [reliance]
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_empty_required_claims_is_rejected(self):
        data = load_valid()
        reliance = load_valid_reliance()
        reliance["required_assurance"]["required_claims"] = []
        data["reliances"] = [reliance]
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_empty_accepted_evidence_kinds_is_rejected(self):
        data = load_valid()
        reliance = load_valid_reliance()
        reliance["required_assurance"]["accepted_evidence_kinds"] = []
        data["reliances"] = [reliance]
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_missing_minimum_scope_is_rejected(self):
        data = load_valid()
        reliance = load_valid_reliance()
        del reliance["required_assurance"]["minimum_scope"]
        data["reliances"] = [reliance]
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_missing_trust_policy_is_rejected(self):
        data = load_valid()
        reliance = load_valid_reliance()
        del reliance["required_assurance"]["trust_policy"]
        data["reliances"] = [reliance]
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_evidence_method_value_in_required_claims_is_rejected(self):
        """The core type-split proof: a verification-method value
        (kani-bounded-model-check) is not a member of required_claims'
        enum, so it can never land in the claim field."""
        data = load_valid()
        reliance = load_valid_reliance()
        reliance["required_assurance"]["required_claims"] = ["kani-bounded-model-check"]
        data["reliances"] = [reliance]
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_claim_value_in_accepted_evidence_kinds_is_rejected(self):
        """Mirror of the above, the other direction: a claim value
        (postcondition-holds) is not a member of accepted_evidence_kinds'
        enum, so bridge-checked/bounded-model-check-style values can
        never share a field with claim values."""
        data = load_valid()
        reliance = load_valid_reliance()
        reliance["required_assurance"]["accepted_evidence_kinds"] = ["postcondition-holds"]
        data["reliances"] = [reliance]
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_retired_bridge_checked_value_is_rejected_everywhere(self):
        """plan.md §8.1: bridge-checked is retired -- it must not appear
        in either enum, not just be excluded from one of them."""
        data = load_valid()
        claims_reliance = load_valid_reliance()
        claims_reliance["required_assurance"]["required_claims"] = ["bridge-checked"]
        evidence_reliance = load_valid_reliance()
        evidence_reliance["obligation_id"] = "TaskQueue.C004"
        evidence_reliance["required_assurance"]["accepted_evidence_kinds"] = ["bridge-checked"]
        data["reliances"] = [claims_reliance, evidence_reliance]
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_duplicate_obligation_id_across_reliances_is_rejected(self):
        data = load_valid()
        data["reliances"] = [load_valid_reliance(), load_valid_reliance()]
        findings = run(data)
        self.assertTrue(
            any("appears in 2 reliances" in f.reason for f in findings), [str(f) for f in findings]
        )

    def test_different_obligation_ids_are_not_flagged(self):
        data = load_valid()
        second = load_valid_reliance()
        second["obligation_id"] = "TaskQueue.C004"
        data["reliances"] = [load_valid_reliance(), second]
        findings = run(data)
        self.assertEqual(findings, [], [str(f) for f in findings])


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


class ValidateReportsNonJsonFilesTest(unittest.TestCase):
    """External review, high severity: validate() (the recursive,
    unanchored scan behind the standalone CLI) used to silently `continue`
    past any non-.json file under a real _interactions directory -- a
    malformed or wrong-extension artifact produced an overall OK with
    zero findings instead of being reported."""

    def test_recursive_scan_reports_unparseable_non_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_interactions"
            d.mkdir(parents=True)
            (d / "I-X-001.yaml").write_text("not: valid: json: [[[")
            findings = validate(Path(tmp))
        self.assertTrue(findings, "a malformed non-.json artifact must be reported, not silently skipped")

    def test_recursive_scan_reports_well_formed_json_with_wrong_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_interactions"
            d.mkdir(parents=True)
            (d / "I-SCHED-TQ-001.yaml").write_text(json.dumps(load_valid()))
            findings = validate(Path(tmp))
        self.assertTrue(any("must be a .json file" in f.reason for f in findings), [str(f) for f in findings])


class ValidateCrateTest(unittest.TestCase):
    """validate_crate(): the descriptor-driven scan pipeline.py's
    cmd_validate_interaction uses -- discovers candidates crate-wide (like
    validate()/find_interaction_files()), then rejects any that don't sit
    directly under the crate's exact canonical directory."""

    def test_missing_crate_root_raises(self):
        with self.assertRaises(FileNotFoundError):
            validate_crate(Path("/nonexistent"), Path("/nonexistent/specs/_interactions"))

    def test_finds_and_validates_files_in_the_canonical_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            crate_root = Path(tmp)
            canonical = crate_root / "specs" / "_interactions"
            canonical.mkdir(parents=True)
            (canonical / "I-SCHED-TQ-001.json").write_text(json.dumps(load_valid()))
            findings = validate_crate(crate_root, canonical)
        self.assertEqual(findings, [], [str(f) for f in findings])

    def test_mislocated_but_otherwise_valid_artifact_is_rejected_by_location_alone(self):
        """External review, medium severity, SECOND pass: a first fix
        (validate_dir(), now removed) only scanned the canonical
        directory, so a mislocated artifact was never even looked at and
        the scan reported OK -- reproducing the same zero-findings
        outcome the original review objected to, just via omission
        instead of false acceptance. This artifact is fully schema-valid,
        with correct computed eligibility and correct filename -- only
        its directory is wrong, proving location alone causes rejection."""
        with tempfile.TemporaryDirectory() as tmp:
            crate_root = Path(tmp)
            stray = crate_root / "not_specs" / "_interactions"
            stray.mkdir(parents=True)
            (stray / "I-SCHED-TQ-001.json").write_text(json.dumps(load_valid()))
            canonical = crate_root / "specs" / "_interactions"  # never created
            findings = validate_crate(crate_root, canonical)
        self.assertTrue(
            any("not directly under the canonical directory" in f.reason for f in findings),
            [str(f) for f in findings],
        )

    def test_correctly_located_artifact_is_not_flagged_by_a_stray_sibling(self):
        """The canonical artifact still validates normally even when an
        unrelated mislocated one also exists elsewhere in the crate."""
        with tempfile.TemporaryDirectory() as tmp:
            crate_root = Path(tmp)
            canonical = crate_root / "specs" / "_interactions"
            canonical.mkdir(parents=True)
            (canonical / "I-SCHED-TQ-001.json").write_text(json.dumps(load_valid()))
            stray = crate_root / "not_specs" / "_interactions"
            stray.mkdir(parents=True)
            data = load_valid()
            data["interaction_id"] = "I-SCHED-TQ-002"
            data["eligibility"] = "ignore"
            (stray / "I-SCHED-TQ-002.json").write_text(json.dumps(data))
            findings = validate_crate(crate_root, canonical)
        self.assertTrue(any("not directly under the canonical directory" in f.reason for f in findings))
        self.assertFalse(any(f.path.name == "I-SCHED-TQ-001.json" for f in findings))


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
