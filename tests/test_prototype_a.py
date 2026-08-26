"""Prototype A integration cases (chainlink #13), specifically the three
the review named as things the hand-made fixtures wouldn't surface on their
own: an acronym concept (D2), a duplicated concept name across crates (D3),
and a callee spec with no stable id yet -- the live gap #6 state (D4).
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from validate_boundary_contracts import validate  # noqa: E402

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
        resolve to whichever one glob() happens to return first."""
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
