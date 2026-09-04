import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from validate_closure import (  # noqa: E402
    CONDITION_KEYS,
    closure_dir_for,
    cluster_for_path,
    count_discovered,
    find_closure_files,
    is_degradation_path,
    load_cluster_artifacts,
    load_validators,
    main,
    validate,
    validate_data,
    validate_draft_data,
)

PROFILE_PATH = Path("specs/_closure/scheduler-core.json")
DEGRADATION_PATH = Path("specs/_closure/scheduler-core.degradation.json")

REVIEW = {"reviewer": "alice", "reviewed_at": "2026-09-04"}


def valid_profile() -> dict:
    return {
        "schema_version": "1.0",
        "cluster": "scheduler-core",
        "closure_kind": "deductive",
        "work_packages": ["WP-SCHED-001"],
        "conditions": {
            "single_verifier_system": True,
            "owning_verifier": "creusot",
            "protocol_class_all_pairwise": True,
            "unresolved_indirect_calls_at_or_above_medium": 0,
            "generic_callees_type_universal_or_creusot_owned": True,
            "transitive_assumptions_within_policy": True,
            "scc_wellfoundedness_discharged": "not-applicable",
        },
        "review": dict(REVIEW),
    }


def valid_degradation() -> dict:
    return {
        "schema_version": "1.0",
        "cluster": "scheduler-core",
        "failed_conditions": ["single_verifier_system"],
        "affected_edges": ["scheduler_dispatch__to__task_queue_pop_ready"],
        "ceiling": "harness-tested",
        "capability_gap": "CG1",
        "tracking_issue": "chainlink:713",
        "review": dict(REVIEW),
    }


def findings_for(data: dict, path: Path) -> list[str]:
    return [str(f) for f in validate_data(path, data, load_validators())]


class ProfileSchemaTest(unittest.TestCase):
    def test_canonical_profile_passes(self):
        self.assertEqual(findings_for(valid_profile(), PROFILE_PATH), [])

    def test_every_condition_key_is_required(self):
        for key in CONDITION_KEYS:
            with self.subTest(condition=key):
                profile = valid_profile()
                del profile["conditions"][key]
                self.assertTrue(findings_for(profile, PROFILE_PATH))

    def test_unknown_condition_is_rejected(self):
        profile = valid_profile()
        profile["conditions"]["looks_fine_to_me"] = True
        self.assertTrue(findings_for(profile, PROFILE_PATH))

    def test_closure_kind_vocabulary_is_closed(self):
        profile = valid_profile()
        profile["closure_kind"] = "proved"
        self.assertTrue(findings_for(profile, PROFILE_PATH))

    def test_a_cluster_must_name_its_work_packages(self):
        profile = valid_profile()
        profile["work_packages"] = []
        self.assertTrue(findings_for(profile, PROFILE_PATH))

    def test_scc_discharge_kind_vocabulary_is_closed(self):
        profile = valid_profile()
        profile["scc_discharges"] = [
            {"members": ["A.C001", "B.C002"], "kind": "reviewed-and-fine", "argument": "x" * 45}
        ]
        self.assertTrue(findings_for(profile, PROFILE_PATH))

    def test_a_discharge_needs_an_actual_argument(self):
        profile = valid_profile()
        profile["scc_discharges"] = [
            {"members": ["A.C001", "B.C002"], "kind": "step-index", "argument": "it terminates"}
        ]
        self.assertTrue(findings_for(profile, PROFILE_PATH))

    def test_a_well_formed_discharge_passes(self):
        profile = valid_profile()
        profile["conditions"]["scc_wellfoundedness_discharged"] = True
        profile["scc_discharges"] = [
            {
                "members": ["Scheduler.C001", "TaskQueue.C003"],
                "kind": "decreasing-measure",
                "argument": (
                    "Each traversal of the cycle consumes one queued task, so the multiset of "
                    "pending tasks strictly decreases and no instantaneous circular dependence "
                    "remains."
                ),
            }
        ]
        self.assertEqual(findings_for(profile, PROFILE_PATH), [])

    def test_scc_condition_accepts_only_true_false_or_not_applicable(self):
        profile = valid_profile()
        profile["conditions"]["scc_wellfoundedness_discharged"] = "probably"
        self.assertTrue(findings_for(profile, PROFILE_PATH))

    def test_profile_may_not_carry_a_review_block_as_a_draft(self):
        findings = validate_draft_data(PROFILE_PATH, valid_profile(), load_validators(draft=True))
        self.assertTrue(any("must not include its own `review` block" in str(f) for f in findings))

    def test_a_review_less_draft_is_otherwise_valid(self):
        profile = valid_profile()
        del profile["review"]
        self.assertEqual(
            [str(f) for f in validate_draft_data(PROFILE_PATH, profile, load_validators(draft=True))], []
        )


class DegradationSchemaTest(unittest.TestCase):
    def test_canonical_record_passes(self):
        self.assertEqual(findings_for(valid_degradation(), DEGRADATION_PATH), [])

    def test_failed_conditions_vocabulary_is_exactly_the_condition_keys(self):
        for key in CONDITION_KEYS:
            with self.subTest(condition=key):
                record = valid_degradation()
                record["failed_conditions"] = [key]
                self.assertEqual(findings_for(record, DEGRADATION_PATH), [])

    def test_a_record_cannot_excuse_something_that_is_not_a_closure_condition(self):
        # The whole limit on what a degradation record can do: there is no
        # vocabulary in which a human accepts "the proof is missing".
        record = valid_degradation()
        record["failed_conditions"] = ["obligation_unprovable"]
        self.assertTrue(findings_for(record, DEGRADATION_PATH))

    def test_a_record_must_locate_its_shortfall(self):
        record = valid_degradation()
        record["affected_edges"] = []
        self.assertTrue(findings_for(record, DEGRADATION_PATH))

    def test_ceiling_is_never_a_verification_method(self):
        record = valid_degradation()
        record["ceiling"] = "creusot-deductive-check"
        self.assertTrue(findings_for(record, DEGRADATION_PATH))

    def test_tracking_issue_shape_is_enforced(self):
        record = valid_degradation()
        record["tracking_issue"] = "someone should look at this"
        self.assertTrue(findings_for(record, DEGRADATION_PATH))

    def test_capability_gap_is_optional_but_typed(self):
        record = valid_degradation()
        del record["capability_gap"]
        self.assertEqual(findings_for(record, DEGRADATION_PATH), [])
        record["capability_gap"] = "CG9"
        self.assertTrue(findings_for(record, DEGRADATION_PATH))


class NamingTest(unittest.TestCase):
    def test_filename_dispatch_matches_plans_own_two_examples(self):
        self.assertFalse(is_degradation_path(PROFILE_PATH))
        self.assertTrue(is_degradation_path(DEGRADATION_PATH))
        self.assertEqual(cluster_for_path(PROFILE_PATH), "scheduler-core")
        self.assertEqual(cluster_for_path(DEGRADATION_PATH), "scheduler-core")

    def test_profile_cluster_must_match_the_filename(self):
        findings = findings_for(valid_profile(), Path("specs/_closure/other-cluster.json"))
        self.assertTrue(any("its filename says" in f for f in findings))

    def test_degradation_cluster_must_match_the_filename_without_the_suffix(self):
        record = valid_degradation()
        record["cluster"] = "scheduler-core-degradation"
        self.assertTrue(any("its filename says" in f for f in findings_for(record, DEGRADATION_PATH)))

    def test_nested_placement_is_rejected(self):
        findings = findings_for(valid_profile(), Path("specs/_closure/nested/scheduler-core.json"))
        self.assertTrue(any("not flat" in f for f in findings))


class G17Test(unittest.TestCase):
    """plan.md §12's G17 row, both clauses."""

    def test_deductive_is_refused_for_a_kani_owned_cluster(self):
        profile = valid_profile()
        profile["conditions"]["owning_verifier"] = "kani"
        findings = findings_for(profile, PROFILE_PATH)
        self.assertTrue(any("G17" in f and "bounded" in f for f in findings))

    def test_bounded_is_fine_for_a_kani_owned_cluster(self):
        profile = valid_profile()
        profile["conditions"]["owning_verifier"] = "kani"
        profile["closure_kind"] = "bounded"
        self.assertEqual(findings_for(profile, PROFILE_PATH), [])


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        self.canonical = closure_dir_for(self.workspace)
        self.canonical.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, relative: str, data: dict) -> Path:
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return path

    def test_a_clean_pair_passes(self):
        self.write("specs/_closure/scheduler-core.json", valid_profile())
        self.assertEqual([str(f) for f in validate(self.workspace)], [])

    def test_stale_excuse_is_rejected(self):
        # The record says single_verifier_system failed; the profile says
        # it holds. The cluster reads as degraded after the gap closed.
        self.write("specs/_closure/scheduler-core.json", valid_profile())
        self.write("specs/_closure/scheduler-core.degradation.json", valid_degradation())
        findings = [str(f) for f in validate(self.workspace)]
        self.assertTrue(any("stale excuse" in f for f in findings))

    def test_undeclared_degradation_is_rejected(self):
        profile = valid_profile()
        profile["conditions"]["single_verifier_system"] = False
        self.write("specs/_closure/scheduler-core.json", profile)
        findings = [str(f) for f in validate(self.workspace)]
        self.assertTrue(any("undeclared degradation" in f for f in findings))

    def test_a_matching_pair_is_consistent(self):
        profile = valid_profile()
        profile["conditions"]["single_verifier_system"] = False
        profile["closure_kind"] = "bounded"
        self.write("specs/_closure/scheduler-core.json", profile)
        self.write("specs/_closure/scheduler-core.degradation.json", valid_degradation())
        self.assertEqual([str(f) for f in validate(self.workspace)], [])

    def test_non_zero_unresolved_count_is_a_failing_condition(self):
        profile = valid_profile()
        profile["conditions"]["unresolved_indirect_calls_at_or_above_medium"] = 2
        self.write("specs/_closure/scheduler-core.json", profile)
        findings = [str(f) for f in validate(self.workspace)]
        self.assertTrue(any("unresolved_indirect_calls_at_or_above_medium" in f for f in findings))

    def test_a_record_without_a_profile_is_rejected(self):
        self.write("specs/_closure/ghost.degradation.json", {**valid_degradation(), "cluster": "ghost"})
        findings = [str(f) for f in validate(self.workspace)]
        self.assertTrue(any("no closure profile beside it" in f for f in findings))

    def test_two_profiles_for_one_cluster_are_rejected(self):
        self.write("specs/_closure/scheduler-core.json", valid_profile())
        stray = copy.deepcopy(valid_profile())
        self.write("specs/_closure/scheduler-core.json.bak", stray)
        findings = [str(f) for f in validate(self.workspace)]
        self.assertTrue(findings)

    def test_mislocated_artifact_is_found_and_rejected_by_location(self):
        self.write("docs/_closure/scheduler-core.json", valid_profile())
        findings = [str(f) for f in validate(self.workspace)]
        self.assertTrue(any("not directly under the canonical directory" in f for f in findings))

    def test_load_cluster_artifacts_omits_invalid_profiles(self):
        broken = valid_profile()
        broken["closure_kind"] = "deductive"
        broken["conditions"]["owning_verifier"] = "kani"  # G17
        self.write("specs/_closure/scheduler-core.json", broken)
        self.assertEqual(load_cluster_artifacts(self.workspace), {})

    def test_load_cluster_artifacts_indexes_by_cluster_and_kind(self):
        profile = valid_profile()
        profile["conditions"]["single_verifier_system"] = False
        profile["closure_kind"] = "bounded"
        self.write("specs/_closure/scheduler-core.json", profile)
        self.write("specs/_closure/scheduler-core.degradation.json", valid_degradation())
        loaded = load_cluster_artifacts(self.workspace)
        self.assertEqual(sorted(loaded["scheduler-core"]), ["degradation", "profile"])

    def test_empty_scan_reports_honestly(self):
        self.assertEqual(count_discovered(self.workspace), 0)
        self.assertEqual(main([str(self.workspace)]), 0)

    def test_find_closure_files_needs_a_real_root(self):
        with self.assertRaises(FileNotFoundError):
            find_closure_files(self.workspace / "nope")

    def test_cli_fails_on_an_inconsistent_pair(self):
        self.write("specs/_closure/scheduler-core.json", valid_profile())
        self.write("specs/_closure/scheduler-core.degradation.json", valid_degradation())
        self.assertEqual(main([str(self.workspace)]), 1)


if __name__ == "__main__":
    unittest.main()
