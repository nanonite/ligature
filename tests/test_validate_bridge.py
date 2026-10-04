import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from bridge_harness import CompileError  # noqa: E402
from bridge_harness import VERIFIERS  # noqa: E402
from bridge_harness import compile_bridge  # noqa: E402
from validate_bridge import (  # noqa: E402
    count_discovered,
    find_bridge_files,
    load_draft_validator,
    load_validator,
    main,
    validate,
    validate_crate,
    validate_data,
    validate_draft_data,
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

    def test_a_precondition_declared_by_its_boundary_is_accepted(self):
        """chainlink #111, the pilot's second shape: `callee_requirement`
        naming a callee PRECONDITION the caller must establish (C001 here),
        declared by the boundary it discharges. The boundary template used to
        forbid exactly this entry, so the pair of documents could not both
        be satisfied -- and this half is what a bridge that checks a
        precondition is for."""
        boundaries = {BOUNDARY_ID: {"callee_guarantees": ["TaskQueue.C001"]}}
        findings = run(load_valid(), boundaries_by_id=boundaries)
        self.assertEqual(findings, [], [str(f) for f in findings])

    def test_a_precondition_the_boundary_omitted_is_refused_and_names_the_boundary(self):
        """The pilot's first shape, reproduced: the boundary carries only the
        postcondition, so approving the bridge fails with a bare 'the boundary
        never declared it'. The refusal now names the remedy, which is the
        boundary contract and not this artifact -- renaming
        `callee_requirement`, or pointing the bridge at a boundary that
        happens to declare it, would discharge an obligation the reviewed
        reliance record does not contain."""
        boundaries = {BOUNDARY_ID: {"callee_guarantees": ["TaskQueue.C002"]}}
        findings = run(load_valid(), boundaries_by_id=boundaries)
        errors = [str(f) for f in findings if f.severity == "error"]
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("TaskQueue.C001", errors[0])
        self.assertIn("TaskQueue.C002", errors[0])
        self.assertIn("re-draft the boundary contract", errors[0])
        self.assertIn("not this bridge", errors[0])


class CompilableFragmentTest(unittest.TestCase):
    """G1b, chainlink #112: bridge_logic must lie inside the fragment
    scripts/bridge_harness.py compiles, so an artifact G9 will certainly
    reject can never be staged and approved.

    Each case below is one the swisstable-verus pilot actually promoted:
    six bridges whose premises were comparisons and whose bindings were
    named GROUP_WIDTH, every command up to and including validate-bridge
    at exit 0, failing only at G9 against an already-signed review."""

    def assertRefused(self, data: dict, *expected: str):
        findings = run(data)
        errors = [str(f) for f in findings if f.severity == "error"]
        self.assertTrue(errors, "expected a finding, got none")
        self.assertTrue(all(f.gate == "G1b" for f in findings), errors)
        for fragment in expected:
            self.assertTrue(any(fragment in e for e in errors), errors)

    def test_equality_operator_in_a_premise_is_refused(self):
        data = load_valid()
        data["bridge_logic"]["premises"] = ["self.ctrl(self.capacity()) == SENTINEL"]
        self.assertRefused(data, "outside the compilable fragment", "predicate application only")

    def test_ordering_operator_in_a_premise_is_refused(self):
        data = load_valid()
        data["bridge_logic"]["premises"] = ["slot < self.capacity()"]
        self.assertRefused(data, "predicate application only")

    def test_arithmetic_in_a_premise_is_refused(self):
        data = load_valid()
        data["bridge_logic"]["premises"] = ["caller_self.mask + 1"]
        self.assertRefused(data, "predicate application only")

    def test_a_conjunction_inside_one_premise_is_refused(self):
        """premises is a LIST and the list is the conjunction, so `&&`
        inside one entry is neither needed nor accepted -- the pilot's
        mistake most likely to look harmless."""
        data = load_valid()
        data["bridge_logic"]["premises"] = ["caller_self.ready(args.now) && Scheduler.I001(caller_self)"]
        self.assertRefused(data, "predicate application only")

    def test_a_non_snake_case_binding_name_is_refused(self):
        data = load_valid()
        data["bridge_logic"]["bindings"] = {"GROUP_WIDTH": "usize"}
        self.assertRefused(data, "outside the compilable fragment", "is not snake_case")

    def test_a_non_snake_case_nested_binding_name_is_refused(self):
        data = load_valid()
        data["bridge_logic"]["bindings"] = {"args": {"NOW": "Time"}}
        self.assertRefused(data, "is not snake_case")

    def test_a_premise_rooted_in_an_undeclared_binding_is_refused(self):
        """The same compiler, same gate: a premise the harness could not
        be handed a value for is a premise G9 will refuse."""
        data = load_valid()
        data["bridge_logic"]["premises"] = ["callee_state.ready()"]
        self.assertRefused(data, "bindings")

    def test_a_premise_applying_an_unavailable_obligation_is_refused(self):
        data = load_valid()
        data["bridge_logic"]["premises"] = ["Scheduler.I099(caller_self)"]
        self.assertRefused(data, "available_contract_facts")

    def test_a_bridge_inside_the_fragment_still_passes(self):
        findings = run(load_valid())
        self.assertEqual([str(f) for f in findings], [])

    def test_the_draft_validator_applies_the_same_rule(self):
        """The draft path is where a model first emits this field, so a
        " gate that only ran at approve would still stage the artifact
        and print the pass line `draft` reports."""
        data = load_valid()
        del data["review"]
        data["bridge_logic"]["premises"] = ["caller_self.capacity() >= 8"]
        findings = validate_draft_data(BRIDGE_PATH, data, load_draft_validator())
        errors = [str(f) for f in findings if f.severity == "error"]
        self.assertTrue(any("outside the compilable fragment" in e for e in errors), errors)

    def test_the_control_bridge_compiles_for_every_verifier_alike(self):
        """Why the rule is reachable at this gate at all: what it checks is
        verifier-INDEPENDENT, so the artifact either clears the fragment
        for creusot, kani and verus together or for none of them. There is
        no per-verifier verdict to wait for, which is what makes refusing
        before approval possible rather than merely early."""
        data = load_valid()
        self.assertEqual(run(data), [])
        for verifier in VERIFIERS:
            with self.subTest(verifier=verifier):
                harness = compile_bridge(data, verifier)
                self.assertIn(data['callee_requirement'].replace('.', '::'), harness.source)

    def test_an_operator_premise_is_refused_for_every_verifier_alike(self):
        data = load_valid()
        data['bridge_logic']['premises'] = ['caller_self.ready(args.now) && Scheduler.I001(caller_self)']
        for verifier in VERIFIERS:
            with self.subTest(verifier=verifier):
                with self.assertRaises(CompileError):
                    compile_bridge(data, verifier)
        self.assertRefused(data, 'predicate application only')


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


class StagedDraftInertTest(unittest.TestCase):
    """Chainlink #110: `draft 3 bridge-drafting` stages
    `<target>.json.draft` WITHOUT a `review` block (only `approve`
    attaches one), and `draft` itself reports "OK: generated draft passes
    G1a/G1b immediate checks" -- yet every bridge scan collected that file
    and failed it on `'review' is a required property` at G1a, so
    `validate-bridge` exited 1 on the output the tool prescribes and
    `check --json` gained one medium finding plus a
    `human_decision_required` condition per staged bridge, with no
    mechanical way to tell a review-in-progress from a schema break.
    validate_interaction/validate_boundary_contracts have skipped staged
    drafts since #90; this pins the same convention for bridges.

    A staged draft is inert (neither reported nor counted) everywhere
    find_bridge_files() is the discovery point -- the standalone
    validate(), validate_crate() (what `validate-bridge` runs) and
    count_discovered() -- while an unreviewed bridge at its REAL
    `<id>.json` path is still a reported draft-lifecycle record."""

    def _staged_draft_dir(self, tmp: str, directory: Path) -> Path:
        directory.mkdir(parents=True)
        draft = load_valid()
        draft.pop("review")  # exactly what stage-3-bridge-drafting produces
        (directory / "BR-SCHED-TQ-001.json.draft").write_text(json.dumps(draft))
        return directory

    def test_find_bridge_files_excludes_staged_drafts(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._staged_draft_dir(tmp, Path(tmp) / "specs" / "_bridges")
            (d / "BR-SCHED-TQ-002.json").write_text(json.dumps(load_valid()))
            found = find_bridge_files(Path(tmp))
        self.assertEqual([p.name for p in found], ["BR-SCHED-TQ-002.json"])

    def test_recursive_scan_reports_nothing_for_a_staged_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._staged_draft_dir(tmp, Path(tmp) / "specs" / "_bridges")
            findings = validate(Path(tmp))
        self.assertEqual(findings, [], [str(f) for f in findings])

    def test_staged_draft_is_not_counted_as_discovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._staged_draft_dir(tmp, Path(tmp) / "specs" / "_bridges")
            self.assertEqual(count_discovered(Path(tmp)), 0)

    def test_validate_crate_reports_nothing_for_a_staged_draft(self):
        """cmd_validate_bridge's own path -- the exact command the pilot
        report's EXIT 1 came from."""
        with tempfile.TemporaryDirectory() as tmp:
            crate_root = Path(tmp)
            canonical = crate_root / "specs" / "_bridges"
            self._staged_draft_dir(tmp, canonical)
            findings = validate_crate(crate_root, canonical, default_boundaries_by_id())
        self.assertEqual(findings, [], [str(f) for f in findings])

    def test_the_cli_exits_zero_on_a_staged_draft(self):
        """The reported symptom: `validate-bridge` exit 1 with
        [G1a/error] "'review' is a required property" against the draft."""
        with tempfile.TemporaryDirectory() as tmp:
            self._staged_draft_dir(tmp, Path(tmp) / "specs" / "_bridges")
            rc = main([tmp])
        self.assertEqual(rc, 0)

    def test_staged_draft_in_a_mislocated_directory_is_still_inert(self):
        """Consistency, not just the happy path: a draft under a wrong
        directory is still a pending draft, and stage_draft only accepts
        canonical targets, so `approve` could never promote it."""
        with tempfile.TemporaryDirectory() as tmp:
            self._staged_draft_dir(tmp, Path(tmp) / "not_specs" / "_bridges")
            findings = validate(Path(tmp))
        self.assertEqual(findings, [], [str(f) for f in findings])

    def test_an_unreviewed_real_json_is_still_reported(self):
        """The skip must be exactly `.draft`: an unreviewed artifact at
        its real `<id>.json` path is a draft-LIFECYCLE record (check
        normalizes it to a pending human decision), not a staging file."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_bridges"
            d.mkdir(parents=True)
            unreviewed = load_valid()
            unreviewed.pop("review")
            (d / "BR-SCHED-TQ-001.json").write_text(json.dumps(unreviewed))
            findings = validate(Path(tmp))
        self.assertTrue(
            any("'review' is a required property" in f.reason for f in findings),
            [str(f) for f in findings],
        )

    def test_wrong_extension_file_is_still_reported(self):
        """Guards the earlier external-review fix (a malformed or
        wrong-extension artifact under _bridges must surface) from being
        collateral damage of the draft skip."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_bridges"
            d.mkdir(parents=True)
            (d / "BR-SCHED-TQ-001.yaml").write_text(json.dumps(load_valid()))
            findings = validate(Path(tmp))
        self.assertTrue(
            any("must be a .json file" in f.reason for f in findings),
            [str(f) for f in findings],
        )


if __name__ == "__main__":
    unittest.main()
