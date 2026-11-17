"""Prototype A integration cases (chainlink #13), specifically the three
the review named as things the hand-made fixtures wouldn't surface on their
own: an acronym concept (D2), a duplicated concept name across crates (D3),
and a callee spec with no stable id yet -- the live gap #6 state (D4).
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from validate_boundary_contracts import validate  # noqa: E402
from validate_concept_spec import load_validator  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "boundary_contracts"


class D2AcronymConceptTest(unittest.TestCase):
    def test_acronym_concepts_produce_no_filename_mismatch(self):
        """Review finding D2: the old regex-based _pascal_to_snake would
        turn HTTPClient into h_t_t_p_client and TCPConnection into
        t_c_p_connection, neither of which matches the filename this
        fixture actually uses (httpclient / tcpconnection, matching
        concept-to-code's own snake_case()). That would have produced a
        false G1b filename-mismatch finding on a perfectly valid artifact."""
        findings = validate(FIXTURES / "d2_acronym_concept")
        errors = [f for f in findings if f.severity == "error"]
        self.assertEqual(errors, [], [str(f) for f in errors])


class D3DuplicateConceptTest(unittest.TestCase):
    def test_duplicate_concept_across_crates_is_reported_ambiguous(self):
        """Review finding D3: two spec files both declaring concept
        TaskQueue under the same specs_search_root must not silently
        resolve to whichever one glob() happens to return first.

        #200: this test was red before the #179 fixture sweep, and the
        cause was the fixture, not the resolver: both `task_queue.json`
        specs predated the current concept-spec schema (they omitted
        required fields such as `schema_version`), so the lookup classified
        them as invalid candidates and reported `applies_to unverifiable`
        instead of the intended D3 ambiguity. The #179 sweep made the
        fixtures schema-valid, which is the fix; the assertions below are
        unchanged."""
        fixture = FIXTURES / "d3_duplicate_concept"
        findings = validate(fixture / "crate_a", specs_search_root=fixture)
        errors = [f for f in findings if f.severity == "error"]
        self.assertTrue(errors, "expected an ambiguous-resolution error, got none")
        self.assertTrue(any("ambiguous" in f.reason.lower() or "more than one" in f.reason for f in errors))

    def test_duplicate_concept_resolution_is_deterministic_across_repeated_runs(self):
        """Not just 'errors on ambiguity' -- glob() order is filesystem-
        dependent, so without sorting, two runs on the same tree could
        disagree about which candidate 'wins'. Run it several times and
        confirm the reported findings are identical every time."""
        fixture = FIXTURES / "d3_duplicate_concept"
        runs = [
            [str(f) for f in validate(fixture / "crate_a", specs_search_root=fixture)]
            for _ in range(5)
        ]
        self.assertTrue(all(run == runs[0] for run in runs), runs)

    def test_ambiguity_lists_every_schema_valid_candidate_in_sorted_order(self):
        """#200: the D3 ambiguity must fire for two schema-valid
        declarations and name both candidates in sorted order, independent
        of filesystem walk or creation order. The fixture-backed target
        test was red before the #179 sweep only because both fixture specs
        were schema-invalid -- the lookup classifies candidates before it
        can count them, so stale fixtures turn the ambiguity into a single
        `applies_to unverifiable` error. Building the tree here from a spec
        this test first validates against the concept-spec schema, and
        creating the `z_` crate before the `a_` crate, makes the invariant
        fail on its own terms if either fixture or resolver drifts again
        instead of depending on the fixture's directory order."""
        source = FIXTURES / "d3_duplicate_concept" / "crate_a" / "specs" / "task_queue.json"
        spec = json.loads(source.read_text(encoding="utf-8"))
        schema_errors = list(load_validator().iter_errors(spec))
        self.assertEqual(schema_errors, [], [e.message for e in schema_errors])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Reverse-sorted creation order: resolution must sort, not rely
            # on what the filesystem walk happens to return first.
            z_spec = root / "z_crate" / "specs" / "task_queue.json"
            z_spec.parent.mkdir(parents=True)
            z_spec.write_text(json.dumps(spec), encoding="utf-8")
            a_spec = root / "a_crate" / "specs" / "task_queue.json"
            a_spec.parent.mkdir(parents=True)
            a_spec.write_text(json.dumps(spec), encoding="utf-8")
            boundary = (
                root / "crate" / "specs" / "_boundaries"
                / "scheduler_dispatch__to__task_queue_pop_ready.json"
            )
            boundary.parent.mkdir(parents=True)
            shutil.copyfile(
                FIXTURES / "d3_duplicate_concept" / "crate_a" / "specs"
                / "_boundaries" / "scheduler_dispatch__to__task_queue_pop_ready.json",
                boundary,
            )
            findings = validate(root / "crate", specs_search_root=root)

        ambiguous = [
            f for f in findings
            if f.severity == "error" and "more than one spec" in f.reason
        ]
        self.assertEqual(len(ambiguous), 1, [str(f) for f in findings])
        reason = ambiguous[0].reason
        self.assertIn(str(a_spec), reason)
        self.assertIn(str(z_spec), reason)
        self.assertLess(reason.index(str(a_spec)), reason.index(str(z_spec)))
        self.assertIn("TaskQueue", reason)


class D4NoStableIdTest(unittest.TestCase):
    def test_missing_constraint_id_is_visible_not_silent(self):
        """Review finding D4 (the live gap #6 state): a callee spec whose
        constraints have no id field yet must produce a visible,
        non-blocking signal, not an indistinguishable 'all clean.'"""
        fixture = FIXTURES / "d4_no_stable_id"
        findings = validate(fixture, specs_search_root=fixture / "specs")
        errors = [f for f in findings if f.severity == "error"]
        infos = [f for f in findings if f.severity == "info"]
        self.assertEqual(errors, [], [str(f) for f in errors])
        self.assertEqual(len(infos), 1)
        self.assertIn("stable id", infos[0].reason)


if __name__ == "__main__":
    unittest.main()
