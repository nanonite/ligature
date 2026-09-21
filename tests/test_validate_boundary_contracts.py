import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from validate_boundary_contracts import (  # noqa: E402
    check_assumption_identity_collisions,
    validate,
    load_validator,
    valid_boundary_edges_from_crate,
)

FIXTURES = ROOT / "tests" / "fixtures" / "boundary_contracts"

VALID_INSTANCE = {
    "schema_version": "1.0",
    "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
    "caller": {"concept": "Scheduler", "method": "dispatch"},
    "callee": {"concept": "TaskQueue", "method": "pop_ready"},
    "callee_guarantees": ["TaskQueue.C003"],
    "review": {"reviewer": "alice", "reviewed_at": "2026-08-25"},
}


def gates_hit(findings) -> set:
    return {f.gate for f in findings}


class BoundaryContractSchemaReviewFieldTest(unittest.TestCase):
    """Review finding (round 2, medium severity): review authority wasn't
    schema-enforced -- reviewer: "" and reviewed_at: "not-a-date" both
    validated."""

    @classmethod
    def setUpClass(cls):
        cls.validator = load_validator()

    def test_valid_instance_has_no_errors(self):
        self.assertEqual(list(self.validator.iter_errors(VALID_INSTANCE)), [])

    def test_empty_reviewer_is_rejected(self):
        instance = json.loads(json.dumps(VALID_INSTANCE))
        instance["review"]["reviewer"] = ""
        self.assertTrue(list(self.validator.iter_errors(instance)))

    def test_malformed_reviewed_at_is_rejected(self):
        instance = json.loads(json.dumps(VALID_INSTANCE))
        instance["review"]["reviewed_at"] = "not-a-date"
        self.assertTrue(list(self.validator.iter_errors(instance)))


class ValidateBoundaryContractsTest(unittest.TestCase):
    def test_missing_root_raises_instead_of_reporting_clean(self):
        """Review finding (round 2, high severity): Path.glob() on a
        nonexistent directory returns [] with no error, so a typo'd
        crate_dir used to produce 'OK: all boundary contracts pass' --
        indistinguishable from a real, fully-clean scan."""
        with self.assertRaises(FileNotFoundError):
            validate(FIXTURES / "this_directory_does_not_exist")

    def test_valid_boundary_has_no_findings(self):
        findings = validate(
            FIXTURES / "valid_crate",
            specs_search_root=FIXTURES / "valid_crate" / "specs",
        )
        self.assertEqual(findings, [], [str(f) for f in findings])

    def test_g1a_missing_review_is_rejected(self):
        findings = validate(FIXTURES / "g1a_fail")
        self.assertIn("G1a", gates_hit(findings))
        # Should not proceed to G1b/G2+ on a schema-invalid document.
        self.assertNotIn("G1b", gates_hit(findings))
        self.assertNotIn("G2+", gates_hit(findings))

    def test_g1b_filename_body_mismatch_is_rejected(self):
        findings = validate(FIXTURES / "g1b_filename_mismatch")
        self.assertIn("G1b", gates_hit(findings))
        self.assertTrue(any("does not match caller/callee" in f.reason for f in findings))

    def test_g1b_duplicate_tracking_issue_is_rejected(self):
        findings = validate(FIXTURES / "g1b_duplicate_tracking_issue")
        self.assertIn("G1b", gates_hit(findings))
        self.assertTrue(any("used by 2 assumptions" in f.reason for f in findings))

    def test_g2_plus_adversary_as_guarantee_is_rejected(self):
        """The deliberate-failure regression fixture (chainlink #12):
        proves G2+ actually fails before it's trusted to pass."""
        findings = validate(FIXTURES / "g2_plus_adversary_as_guarantee")
        self.assertIn("G2+", gates_hit(findings))
        self.assertTrue(any("adversary case, not a guarantee" in f.reason for f in findings))

    def test_g2_plus_role_mismatch_is_rejected(self):
        findings = validate(FIXTURES / "g2_plus_role_mismatch")
        self.assertIn("G2+", gates_hit(findings))
        self.assertTrue(any("role safety" in f.reason for f in findings))

    def test_g2_plus_applies_to_mismatch_is_rejected_when_resolvable(self):
        fixture = FIXTURES / "g2_plus_applies_to_mismatch"
        findings = validate(fixture, specs_search_root=fixture / "specs")
        self.assertIn("G2+", gates_hit(findings))
        self.assertTrue(any("applies_to" in f.reason for f in findings))

    def test_g2_plus_applies_to_check_degrades_gracefully_without_search_root(self):
        """Without --specs-search-root, the applies_to mismatch is
        unverifiable -- not a false pass, not a crash, and (D4) not silent
        either: a visible, non-blocking info finding."""
        fixture = FIXTURES / "g2_plus_applies_to_mismatch"
        findings = validate(fixture, specs_search_root=None)
        errors = [f for f in findings if f.severity == "error"]
        infos = [f for f in findings if f.severity == "info"]
        self.assertEqual(errors, [], [str(f) for f in errors])
        self.assertEqual(len(infos), 1)
        self.assertIn("unverifiable", infos[0].reason)
        self.assertIn("no --specs-search-root given", infos[0].reason)


class G2PlusConceptResolutionTest(unittest.TestCase):
    """Chainlink #69: G2+'s concept resolver must skip underscore-prefixed
    artifact directories, exactly as the witness/G18/pilot-cluster resolvers
    already do. A witness spec carries its own top-level `concept`, so an
    unfiltered scan reports a spurious ambiguity the moment a witness shares
    a concept name with a boundary's callee -- the collision #52's pilot
    avoided only by witnessing a different concept. Narrowing the candidate
    set must not disable the ambiguity check itself."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        specs = self.root / "specs"
        (specs / "_boundaries").mkdir(parents=True)
        (specs / "task_queue.json").write_text(
            json.dumps(
                {
                    "concept": "TaskQueue",
                    "constraints": [
                        {
                            "id": "C003",
                            "english": "pop_ready returns None only when no task has deadline <= now",
                            "logic": "true",
                            "kind": "postcondition",
                            "applies_to": ["pop_ready"],
                        }
                    ],
                }
            )
        )
        (specs / "_boundaries" / "scheduler_dispatch__to__task_queue_pop_ready.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
                    "caller": {"concept": "Scheduler", "method": "dispatch"},
                    "callee": {"concept": "TaskQueue", "method": "pop_ready"},
                    "callee_guarantees": ["TaskQueue.C003"],
                    "review": {"reviewer": "alice", "reviewed_at": "2026-08-25"},
                }
            )
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _add_witness_naming_task_queue(self):
        witness_dir = self.root / "specs" / "_witnesses"
        witness_dir.mkdir()
        (witness_dir / "task_queue.load_factor.json").write_text(
            json.dumps({"concept": "TaskQueue", "witness_id": "W-TQ-LOAD-FACTOR"})
        )

    def test_a_witness_sharing_the_callee_concept_is_skipped_not_ambiguous(self):
        self._add_witness_naming_task_queue()
        findings = validate(self.root, specs_search_root=self.root / "specs")
        self.assertEqual(findings, [], [str(f) for f in findings])

    def test_two_concept_specs_outside_artifact_dirs_still_report_ambiguity(self):
        self._add_witness_naming_task_queue()
        (self.root / "specs" / "task_queue_copy.json").write_text(
            json.dumps({"concept": "TaskQueue", "constraints": []})
        )
        findings = validate(self.root, specs_search_root=self.root / "specs")
        errors = [f for f in findings if f.severity == "error"]
        self.assertTrue(
            any("ambiguous" in f.reason.lower() or "more than one" in f.reason for f in errors),
            [str(f) for f in findings],
        )


class ValidBoundaryEdgesFromCrateTest(unittest.TestCase):
    """valid_boundary_edges_from_crate(): the coverage set
    scripts/validate_interaction.py's check_r2_coverage trusts as
    'covered by a boundary' (chainlink #46)."""

    def test_valid_boundary_contributes_its_edge(self):
        covered = valid_boundary_edges_from_crate(
            FIXTURES / "valid_crate",
            FIXTURES / "valid_crate" / "specs" / "_boundaries",
            FIXTURES / "valid_crate" / "specs",
        )
        self.assertEqual(covered, {("Scheduler", "dispatch", "TaskQueue", "pop_ready")})

    def test_schema_invalid_boundary_contributes_no_coverage(self):
        covered = valid_boundary_edges_from_crate(
            FIXTURES / "g1a_fail",
            FIXTURES / "g1a_fail" / "specs" / "_boundaries",
            None,
        )
        self.assertEqual(covered, set())

    def test_info_only_g2_plus_finding_does_not_disqualify_coverage(self):
        """External review self-check: an earlier version of this
        function treated ANY finding (including info-severity) as
        disqualifying, so a boundary with a legitimate info-only G2+
        finding (here: specs_search_root=None, "applies_to
        unverifiable") was silently dropped from R2 coverage even though
        it's a genuinely valid, reviewed boundary contract. Reproduced
        directly before fixing -- this fixture is the same one
        ValidateBoundaryContractsTest.test_g2_plus_applies_to_check_degrades_gracefully_without_search_root
        uses to prove that finding really is info-only, not error."""
        fixture = FIXTURES / "g2_plus_applies_to_mismatch"
        covered = valid_boundary_edges_from_crate(fixture, fixture / "specs" / "_boundaries", None)
        self.assertNotEqual(covered, set(), "a valid boundary with only an info-severity finding must still count")

    def test_mislocated_boundary_contributes_no_coverage(self):
        """A boundary contract that exists but sits outside the exact
        canonical directory must not silently grant R2 coverage --
        mirrors validate_crate()'s own location-anchoring discipline."""
        with tempfile.TemporaryDirectory() as tmp:
            crate_root = Path(tmp)
            stray = crate_root / "not_specs" / "_boundaries"
            stray.mkdir(parents=True)
            (stray / "scheduler_dispatch__to__task_queue_pop_ready.json").write_text(json.dumps(VALID_INSTANCE))
            canonical = crate_root / "specs" / "_boundaries"  # never created
            covered = valid_boundary_edges_from_crate(crate_root, canonical, None)
        self.assertEqual(covered, set())


def boundary_with_assumption(boundary_id: str, tracking_issue: str, assumption_hash: str) -> dict:
    return {
        "boundary_id": boundary_id,
        "assumptions": [
            {
                "boundary_id": boundary_id,
                "tracking_issue": tracking_issue,
                "assumption_hash": assumption_hash,
            }
        ],
    }


HASH_A = "sha256:" + "1" * 64
HASH_B = "sha256:" + "2" * 64


class AssumptionIdentityCollisionTest(unittest.TestCase):
    """chainlink #6: composite assumption identity (boundary_id,
    tracking_issue, assumption_hash) is workspace-wide; G1b's own
    tracking-issue-uniqueness check is scoped to one boundary file."""

    def test_the_same_issue_and_hash_across_boundaries_is_not_a_collision(self):
        """plan.md §8.4's own first registry trigger, 'an assumption
        spans boundaries' -- left unflagged, but only because no
        disagreement is observable, not because a matching hash proves
        the underlying assumption text is actually identical (nothing
        in this pipeline stores canonical assumption text to check
        that against)."""
        boundaries = [
            boundary_with_assumption("a__to__b", "chainlink:713", HASH_A),
            boundary_with_assumption("c__to__d", "chainlink:713", HASH_A),
        ]
        self.assertEqual(check_assumption_identity_collisions(boundaries), [])

    def test_the_same_issue_with_different_hashes_is_a_collision(self):
        boundaries = [
            boundary_with_assumption("a__to__b", "chainlink:713", HASH_A),
            boundary_with_assumption("c__to__d", "chainlink:713", HASH_B),
        ]
        findings = check_assumption_identity_collisions(boundaries)
        self.assertEqual(len(findings), 1)
        self.assertIn("chainlink:713", findings[0].reason)
        self.assertIn("2 distinct assumption_hash values", findings[0].reason)
        self.assertIn("a__to__b", findings[0].reason)
        self.assertIn("c__to__d", findings[0].reason)

    def test_different_issues_never_collide_with_each_other(self):
        boundaries = [
            boundary_with_assumption("a__to__b", "chainlink:1", HASH_A),
            boundary_with_assumption("c__to__d", "chainlink:2", HASH_B),
        ]
        self.assertEqual(check_assumption_identity_collisions(boundaries), [])

    def test_a_single_boundary_alone_is_never_a_collision(self):
        boundaries = [boundary_with_assumption("a__to__b", "chainlink:713", HASH_A)]
        self.assertEqual(check_assumption_identity_collisions(boundaries), [])

    def test_assumptions_with_no_tracking_issue_or_hash_are_ignored_not_crashed_on(self):
        boundaries = [
            {"boundary_id": "a__to__b", "assumptions": [{"boundary_id": "a__to__b"}]},
            boundary_with_assumption("c__to__d", "chainlink:713", HASH_A),
        ]
        self.assertEqual(check_assumption_identity_collisions(boundaries), [])

    def test_three_boundaries_two_of_which_collide_names_only_the_colliding_pair(self):
        boundaries = [
            boundary_with_assumption("a__to__b", "chainlink:713", HASH_A),
            boundary_with_assumption("c__to__d", "chainlink:713", HASH_B),
            boundary_with_assumption("e__to__f", "chainlink:999", HASH_A),
        ]
        findings = check_assumption_identity_collisions(boundaries)
        self.assertEqual(len(findings), 1)
        self.assertIn("chainlink:713", findings[0].reason)
        self.assertNotIn("chainlink:999", findings[0].reason)


if __name__ == "__main__":
    unittest.main()
