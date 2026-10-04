import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from validate_closure import (  # noqa: E402
    CONDITION_KEYS,
    check_failed_conditions_shape,
    closure_dir_for,
    cluster_for_path,
    count_discovered,
    find_closure_files,
    is_degradation_path,
    load_cluster_artifacts,
    load_degradation_records,
    load_validators,
    main,
    validate,
    validate_data,
    validate_draft_data,
)

PROFILE_PATH = Path("specs/_closure/scheduler-core.json")
DEGRADATION_PATH = Path("specs/_closure/scheduler-core.degradation.json")
CLOSURE_FIXTURES = ROOT / "tests" / "fixtures" / "closure"

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
        """Exactly three values (chainlink #85): the two uniform claims
        over the whole closure plus `partial`. `mixed` and `degraded` are
        rejected on purpose, not overlooked -- `degraded` already names
        gate g14's outcome for a cluster released under a degradation
        record, and the evidence composition `mixed` would name is
        already carried by conditions.single_verifier_system."""
        profile = valid_profile()
        for kind in ("proved", "mixed", "degraded"):
            with self.subTest(closure_kind=kind):
                profile["closure_kind"] = kind
                self.assertTrue(findings_for(profile, PROFILE_PATH))

    def test_partial_is_an_accepted_closure_kind(self):
        """chainlink #85's own repro: a pilot that cannot achieve full
        deductive closure declares `partial` in specs/_closure/<cluster>.json
        and `validate-closure` must accept it at G1a instead of refusing
        the only honest state it can declare."""
        for kind in ("partial", "deductive", "bounded"):
            with self.subTest(closure_kind=kind):
                profile = valid_profile()
                profile["closure_kind"] = kind
                self.assertEqual(findings_for(profile, PROFILE_PATH), [])

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

    def test_partial_is_fine_for_a_kani_owned_cluster(self):
        """`partial` claims strictly less than `deductive`, so the G17
        clause that refuses `deductive` over a Kani owner has nothing to
        refuse (chainlink #85)."""
        profile = valid_profile()
        profile["conditions"]["owning_verifier"] = "kani"
        profile["closure_kind"] = "partial"
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

    def test_a_cg3_tracking_record_is_not_a_stale_excuse(self):
        """chainlink #86: a record naming
        generic_callees_type_universal_or_creusot_owned beside a `true`
        declaration is the tracking record plan.md §3 requires of every
        capability gap (CG3), not a stale excuse -- this module cannot
        see closure evidence either way, so whether such a record is
        still needed, or has gone stale, is gate_g14's to decide from
        the closure's own achieved records. Every OTHER condition keeps
        the stale-excuse rule exactly as it was."""
        record = valid_degradation()
        record["failed_conditions"] = ["generic_callees_type_universal_or_creusot_owned"]
        record["ceiling"] = "per-instantiation"
        record["capability_gap"] = "CG3"
        self.write("specs/_closure/scheduler-core.json", valid_profile())
        self.write("specs/_closure/scheduler-core.degradation.json", record)
        self.assertEqual([str(f) for f in validate(self.workspace)], [])

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


class DegradationRecordLoaderTest(unittest.TestCase):
    """chainlink #99: ONE loader for specs/_closure/*.degradation.json.

    Every command that acts on a degradation record used to open the
    closure directory and parse whatever it found there, so what a
    record was worth depended on which command was asking. The loader
    below answers one question -- may this record be acted on as this
    cluster's declared degradation -- and answers it from validation
    alone, refusing schema-invalid records, malformed
    `failed_conditions`, records whose excuse the profile contradicts,
    and every record validate-closure itself excludes (mislocated,
    misnamed, profileless), each refusal carrying its reason.
    """

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

    def write_pair(self, profile: dict | None = None, record: dict | None = None):
        """A cluster whose profile declares single_verifier_system as
        FAILING, so a record naming it is a consistent pair rather than a
        stale excuse -- the default 'the record is fine' case every
        refusal test below departs from by changing exactly one thing."""
        if profile is None:
            profile = valid_profile()
            profile["conditions"]["single_verifier_system"] = False
            profile["closure_kind"] = "bounded"
        if record is None:
            record = valid_degradation()
        self.write("specs/_closure/scheduler-core.json", profile)
        self.write("specs/_closure/scheduler-core.degradation.json", record)

    def copy_fixture(self, case: str) -> None:
        """Lay a regression fixture's tree into the workspace root,
        files and all -- the fixture IS a specs/_closure/ directory,
        because where the record sits is part of what it tests."""
        shutil.copytree(CLOSURE_FIXTURES / case, self.workspace, dirs_exist_ok=True)

    def fixture_record_path(self, case: str, name: str) -> Path:
        return self.workspace / "specs" / "_closure" / name

    def reasons(self, records, relative: str = "specs/_closure/scheduler-core.degradation.json") -> list[str]:
        return records.reasons_for(self.workspace / relative)

    # -- the valid case ---------------------------------------------------

    def test_a_consistent_pair_yields_the_record(self):
        self.write_pair()
        records = load_degradation_records(self.workspace)
        self.assertEqual(sorted(records.valid), ["scheduler-core"])
        self.assertEqual(records.invalid, {})
        self.assertEqual(records.record_for("scheduler-core"), valid_degradation())

    def test_every_returned_record_carries_a_usable_failed_conditions(self):
        """The guarantee a consumer is entitled to: a record in `valid`
        has an array of real condition keys, so `set(record[
        "failed_conditions"])` and `[str(c) for c in ...]` -- what
        gate_g14 and `ligature status` each do -- read conditions, not
        characters."""
        self.write_pair()
        records = load_degradation_records(self.workspace)
        for _, record in records.valid.values():
            self.assertIsInstance(record["failed_conditions"], list)
            for key in record["failed_conditions"]:
                self.assertIn(key, CONDITION_KEYS)

    def test_a_cg3_tracking_record_stays_valid(self):
        """chainlink #86 survives the loader unchanged: a record naming
        generic_callees_type_universal_or_creusot_owned beside a `true`
        declaration is the tracking record plan.md §3 requires, not a
        stale excuse."""
        record = valid_degradation()
        record["failed_conditions"] = ["generic_callees_type_universal_or_creusot_owned"]
        record["ceiling"] = "per-instantiation"
        record["capability_gap"] = "CG3"
        self.write_pair(record=record)
        records = load_degradation_records(self.workspace)
        self.assertTrue(records.is_valid("scheduler-core"))

    def test_a_workspace_with_no_closure_directory_is_empty_not_an_error(self):
        self._tmp.cleanup()
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        records = load_degradation_records(self.workspace)
        self.assertEqual((records.valid, records.invalid, len(records)), ({}, {}, 0))

    # -- refusals ---------------------------------------------------------

    def test_a_stale_excuse_is_refused(self):
        """The record says single_verifier_system failed; the profile
        says it holds. Acting on it would keep a closed gap reading as
        degraded -- so the record excuses nothing, and says why."""
        self.write_pair(profile=valid_profile(), record=valid_degradation())
        records = load_degradation_records(self.workspace)
        self.assertEqual(records.valid, {})
        self.assertEqual(records.record_for("scheduler-core"), None)
        self.assertTrue(any("stale excuse" in r for r in self.reasons(records)))

    def test_a_schema_invalid_record_is_refused(self):
        record = valid_degradation()
        record["ceiling"] = "creusot-deductive-check"  # a method, not a ceiling
        self.write_pair(record=record)
        records = load_degradation_records(self.workspace)
        self.assertEqual(records.valid, {})
        self.assertTrue(any("G1a" in r for r in self.reasons(records)))

    def test_a_malformed_failed_conditions_is_refused(self):
        """Every way the excuse vocabulary can be unusable. A string is
        the sharp one: iterating it yields characters, which is how a
        cluster comes to read as excused by 's', 'i', 'n', ... ."""
        for value in (
            "single_verifier_system",
            [1],
            ["obligation_unprovable"],
            [],
            ["single_verifier_system", "single_verifier_system"],
        ):
            with self.subTest(failed_conditions=value):
                record = valid_degradation()
                record["failed_conditions"] = value
                self.write_pair(record=record)
                records = load_degradation_records(self.workspace)
                self.assertEqual(records.valid, {})
                self.assertTrue(any("failed_conditions" in r for r in self.reasons(records)))

    def test_check_failed_conditions_shape_names_what_is_wrong(self):
        for value, expected in (
            ("single_verifier_system", "the string"),
            ({"condition": True}, "a dict"),
            (7, "the scalar"),
            ([], "failed_conditions is empty"),
            ([3], "must be closure-condition key strings"),
            (["nonsense"], "not a closure condition key"),
            (["single_verifier_system"] * 2, "more than once"),
        ):
            with self.subTest(failed_conditions=value):
                findings = check_failed_conditions_shape(DEGRADATION_PATH, {"failed_conditions": value})
                self.assertTrue(
                    any(expected in str(f) for f in findings),
                    f"expected {expected!r} among {[str(f) for f in findings]}",
                )

    def test_a_record_with_no_profile_beside_it_is_refused(self):
        self.write("specs/_closure/ghost.degradation.json", {**valid_degradation(), "cluster": "ghost"})
        records = load_degradation_records(self.workspace)
        self.assertEqual(records.valid, {})
        self.assertTrue(
            any("no closure profile beside it" in r for r in records.reasons_for(
                self.workspace / "specs/_closure/ghost.degradation.json"
            ))
        )

    def test_a_record_beside_an_invalid_profile_is_refused(self):
        """A degradation is a departure from a DECLARED profile. An
        unreadable one declares nothing, so the record beside it cannot
        be checked against anything."""
        self.write("specs/_closure/scheduler-core.json", {**valid_profile(), "closure_kind": "proved"})
        self.write("specs/_closure/scheduler-core.degradation.json", valid_degradation())
        records = load_degradation_records(self.workspace)
        self.assertEqual(records.valid, {})
        self.assertTrue(any("no closure profile beside it" in r for r in self.reasons(records)))

    def test_a_mislocated_record_is_refused_and_named(self):
        self.write_pair()
        self.write("docs/_closure/scheduler-core.degradation.json", valid_degradation())
        records = load_degradation_records(self.workspace)
        self.assertEqual(sorted(records.valid), ["scheduler-core"])
        self.assertTrue(
            any(
                "not directly under the canonical directory" in r
                for r in records.reasons_for(self.workspace / "docs/_closure/scheduler-core.degradation.json")
            )
        )

    def test_a_mislocated_record_is_named_when_there_is_no_closure_directory(self):
        """A workspace with no specs/_closure/ at all is exactly where a
        mislocated record is most likely to be the ONLY record there is.
        The loader's location rejection must not be reachable only
        through the canonical directory's existence: an empty result here
        would read as 'no degradation is declared' off a workspace that
        holds one, while `validate()` on the same workspace names it."""
        self._tmp.cleanup()
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        self.write(
            "docs/_closure/ghost.degradation.json",
            {**valid_degradation(), "cluster": "ghost"},
        )
        self.assertFalse(closure_dir_for(self.workspace).exists())

        records = load_degradation_records(self.workspace)
        self.assertEqual(records.valid, {})
        self.assertEqual(records.record_for("ghost"), None)
        reasons = records.reasons_for(self.workspace / "docs/_closure/ghost.degradation.json")
        self.assertTrue(reasons, "a refused record must not be silently absent")
        self.assertTrue(any("not directly under the canonical directory" in r for r in reasons), reasons)
        self.assertEqual(
            {p.name for p in records.invalid},
            {Path(f.path).name for f in validate(self.workspace)
             if Path(f.path).name.endswith(".degradation.json")},
        )

    def test_a_misnamed_record_is_refused(self):
        self.write_pair()
        record = valid_degradation()
        record["cluster"] = "scheduler-core-degradation"
        self.write("specs/_closure/other.degradation.json", record)
        records = load_degradation_records(self.workspace)
        self.assertEqual(sorted(records.valid), ["scheduler-core"])
        self.assertTrue(
            any("its filename says" in r for r in records.reasons_for(
                self.workspace / "specs/_closure/other.degradation.json"
            ))
        )

    def test_an_undeclared_degradation_is_the_profiles_finding_not_the_records(self):
        """A profile failing a condition nothing excuses is the profile's
        own G17 finding. The loader must not refuse a record for another
        artifact's defect -- it can only report that there is no record."""
        profile = valid_profile()
        profile["conditions"]["single_verifier_system"] = False
        profile["closure_kind"] = "bounded"
        self.write("specs/_closure/scheduler-core.json", profile)
        records = load_degradation_records(self.workspace)
        self.assertEqual(records.record_for("scheduler-core"), None)
        self.assertEqual(records.invalid, {})
        self.assertTrue(any("undeclared degradation" in str(f) for f in validate(self.workspace)))

    def test_no_record_declared_is_distinguishable_from_a_refused_record(self):
        """The distinction the structured result exists for: both leave
        `valid` empty, and only one of them has a reason to report."""
        self.write("specs/_closure/scheduler-core.json", valid_profile())
        declared_none = load_degradation_records(self.workspace)
        self.assertEqual(declared_none.valid, {})
        self.assertEqual(declared_none.invalid, {})

        self.write_pair(profile=valid_profile(), record=valid_degradation())  # stale excuse
        refused = load_degradation_records(self.workspace)
        self.assertEqual(refused.valid, {})
        self.assertEqual(len(refused.invalid), 1)

    # -- the issue's two regression fixtures ------------------------------

    def test_the_schema_invalid_fixture_is_refused_and_excuses_nothing(self):
        """The pilot record, with the claim a ceiling cannot make
        smuggled in beside it: `verifier` is not a property of a
        degradation record, so the record is refused at G1a -- and since
        a refused record excuses nothing, the profile's own failing
        conditions come back as undeclared degradations."""
        self.copy_fixture("schema_invalid")
        records = load_degradation_records(self.workspace)
        self.assertEqual(records.valid, {})
        reasons = records.reasons_for(self.fixture_record_path("schema_invalid", "date-creusot-core.degradation.json"))
        self.assertTrue(any("verifier" in r for r in reasons), reasons)
        findings = [str(f) for f in validate(self.workspace)]
        self.assertEqual(len([f for f in findings if "undeclared degradation" in f]), 2, findings)

    def test_the_stale_fixture_is_refused_as_a_stale_excuse(self):
        self.copy_fixture("stale")
        records = load_degradation_records(self.workspace)
        self.assertEqual(records.valid, {})
        reasons = records.reasons_for(self.fixture_record_path("stale", "scheduler-core.degradation.json"))
        self.assertTrue(any("stale excuse" in r for r in reasons), reasons)

    def test_the_loader_agrees_with_validate_closure_on_both_fixtures(self):
        """One rule, two commands: whatever validate-closure refuses,
        the loader refuses, and neither can drift into a second opinion
        about what a record is worth."""
        for case in ("schema_invalid", "stale"):
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as tmp:
                    workspace = Path(tmp)
                    shutil.copytree(CLOSURE_FIXTURES / case, workspace, dirs_exist_ok=True)
                    refused_by_gate = {
                        Path(f.path)
                        for f in validate(workspace)
                        if Path(f.path).name.endswith(".degradation.json")
                    }
                    records = load_degradation_records(workspace)
                    self.assertTrue(refused_by_gate)
                    self.assertEqual(
                        {p.name for p in records.invalid}, {p.name for p in refused_by_gate}
                    )


class DraftExclusionTest(unittest.TestCase):
    """Chainlink #98: a staged `<target>.json.draft` -- what `pipeline.py
    draft` writes for a closure profile or degradation record now that
    specs/_closure/ is wired into its dispatchers -- must be inert here,
    the same `.draft` convention find_interaction_files()/
    find_evidence_files() already apply (chainlink #90). Before this fix,
    neither find_closure_files() nor _scan_closure_dir()'s own iterdir
    excluded it, so a staged draft (deliberately missing `review`, as
    every draft is) would be scanned, fail check_naming()'s "must be a
    .json file" / schema's "review is required" checks, and come back as
    a spurious G1a/G1b error on work staged exactly as the tool
    prescribes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        self.canonical = closure_dir_for(self.workspace)
        self.canonical.mkdir(parents=True)
        profile = valid_profile()
        (self.canonical / "scheduler-core.json").write_text(json.dumps(profile))

    def tearDown(self):
        self._tmp.cleanup()

    def _stage_draft(self, name: str, data: dict) -> Path:
        draft_path = self.canonical / name
        draft_path.write_text(json.dumps(data))
        return draft_path

    def test_find_closure_files_excludes_a_staged_draft(self):
        self._stage_draft("scheduler-core.degradation.json.draft", {"cluster": "scheduler-core"})
        found = find_closure_files(self.workspace)
        self.assertEqual({p.name for p in found}, {"scheduler-core.json"})

    def test_count_discovered_excludes_a_staged_draft(self):
        self.assertEqual(count_discovered(self.workspace), 1)
        self._stage_draft("scheduler-core.degradation.json.draft", {"cluster": "scheduler-core"})
        self.assertEqual(count_discovered(self.workspace), 1)

    def test_a_staged_degradation_draft_is_not_reported_by_validate(self):
        draft = valid_degradation()
        del draft["review"]  # a draft never carries one yet
        self._stage_draft("scheduler-core.degradation.json.draft", draft)
        findings = [str(f) for f in validate(self.workspace)]
        self.assertEqual(findings, [])

    def test_a_staged_profile_draft_does_not_shadow_the_approved_profile(self):
        """A draft re-proposing a change to an already-approved profile
        must not occupy that cluster's 'profile' slot ahead of the real
        artifact, nor be reported at all."""
        draft = valid_profile()
        draft["closure_kind"] = "bounded"
        del draft["review"]
        self._stage_draft("scheduler-core.json.draft", draft)
        artifacts = load_cluster_artifacts(self.workspace)
        self.assertEqual(artifacts["scheduler-core"]["profile"][1]["closure_kind"], "deductive")
        self.assertEqual([str(f) for f in validate(self.workspace)], [])

    def test_load_degradation_records_ignores_a_staged_draft(self):
        draft = valid_degradation()
        del draft["review"]
        self._stage_draft("scheduler-core.degradation.json.draft", draft)
        records = load_degradation_records(self.workspace)
        self.assertEqual(records.valid, {})
        self.assertEqual(records.invalid, {})


if __name__ == "__main__":
    unittest.main()
