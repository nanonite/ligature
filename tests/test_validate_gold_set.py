import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from validate_gold_set import (  # noqa: E402
    count_discovered,
    find_gold_set_files,
    gold_set_dir_for,
    load_draft_validator,
    load_gold_sets,
    load_validator,
    main,
    validate,
    validate_data,
    validate_draft_data,
)

FIXTURE = ROOT / "tests" / "fixtures" / "gold_sets" / "valid" / "specs" / "_gold_sets" / "scheduler-core.json"
GOLD_PATH = Path("specs/_gold_sets/scheduler-core.json")


def gold_set() -> dict:
    return json.loads(FIXTURE.read_text())


def findings_for(data: dict, path: Path = GOLD_PATH) -> list[str]:
    return [str(f) for f in validate_data(path, data, load_validator())]


class SchemaTest(unittest.TestCase):
    def test_the_checked_in_gold_set_is_valid(self):
        self.assertEqual(findings_for(gold_set()), [])

    def test_a_gold_set_must_have_edges(self):
        data = gold_set()
        data["edges"] = []
        self.assertTrue(findings_for(data))

    def test_track_vocabulary_is_closed(self):
        data = gold_set()
        data["track"] = "mode-x"
        self.assertTrue(findings_for(data))

    def test_completeness_claim_is_always_a_lower_bound(self):
        data = gold_set()
        data["curation_scope"]["completeness_claim"] = "exhaustive"
        self.assertTrue(findings_for(data))

    def test_there_is_no_vocabulary_for_deriving_an_edge_from_the_interaction_set(self):
        # The anti-circularity guard: a curator who worked from I has to
        # write something false rather than merely omit something.
        data = gold_set()
        data["edges"][0]["derived_from"] = ["interaction-set"]
        self.assertTrue(findings_for(data))

        data = gold_set()
        data["curation_scope"]["method"] = ["reading-the-interaction-set"]
        self.assertTrue(findings_for(data))

    def test_an_edge_needs_a_rationale(self):
        data = gold_set()
        del data["edges"][0]["rationale"]
        self.assertTrue(findings_for(data))

    def test_expected_eligibility_is_optional_but_typed(self):
        data = gold_set()
        del data["edges"][0]["expected_eligibility"]
        self.assertEqual(findings_for(data), [])
        data["edges"][0]["expected_eligibility"] = "probably-fine"
        self.assertTrue(findings_for(data))


class ScopeAndProvenanceTest(unittest.TestCase):
    def test_an_edge_outside_the_examined_scope_is_rejected(self):
        data = gold_set()
        data["edges"][0]["caller"] = {"concept": "Clock", "method": "now"}
        findings = findings_for(data)
        self.assertTrue(any("outside curation_scope.examined_concepts" in f for f in findings))

    def test_an_edge_derived_by_an_unclaimed_method_is_rejected(self):
        data = gold_set()
        data["edges"][0]["derived_from"] = ["interview"]
        findings = findings_for(data)
        self.assertTrue(any("does not claim" in f for f in findings))

    def test_an_identical_duplicate_edge_is_rejected_by_the_schema(self):
        data = gold_set()
        data["edges"].append(copy.deepcopy(data["edges"][0]))
        self.assertTrue(findings_for(data))

    def test_the_same_edge_twice_with_a_different_rationale_is_rejected_by_g1b(self):
        # uniqueItems cannot see this one: the entries differ, the EDGE
        # does not, and recall would count it twice.
        data = gold_set()
        duplicate = copy.deepcopy(data["edges"][0])
        duplicate["rationale"] = "a second reason for the same edge"
        data["edges"].append(duplicate)
        findings = findings_for(data)
        self.assertTrue(any("duplicate gold edge" in f for f in findings))

    def test_an_intra_concept_edge_is_rejected(self):
        data = gold_set()
        data["edges"][0]["callee"] = {"concept": "Scheduler", "method": "validate"}
        findings = findings_for(data)
        self.assertTrue(any("intra-concept" in f for f in findings))


class NamingTest(unittest.TestCase):
    def test_cluster_must_match_the_filename(self):
        findings = findings_for(gold_set(), Path("specs/_gold_sets/other.json"))
        self.assertTrue(any("does not match cluster" in f for f in findings))

    def test_nested_placement_is_rejected(self):
        findings = findings_for(gold_set(), Path("specs/_gold_sets/nested/scheduler-core.json"))
        self.assertTrue(any("not flat" in f for f in findings))


class DraftTest(unittest.TestCase):
    def test_a_draft_may_not_carry_its_own_review(self):
        findings = validate_draft_data(GOLD_PATH, gold_set(), load_draft_validator())
        self.assertTrue(any("audits nothing" in str(f) for f in findings))

    def test_a_review_less_draft_is_otherwise_valid(self):
        data = gold_set()
        del data["review"]
        self.assertEqual(
            [str(f) for f in validate_draft_data(GOLD_PATH, data, load_draft_validator())], []
        )


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        gold_set_dir_for(self.workspace).mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, relative: str, data: dict) -> Path:
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return path

    def test_the_fixture_workspace_passes(self):
        self.write("specs/_gold_sets/scheduler-core.json", gold_set())
        self.assertEqual([str(f) for f in validate(self.workspace)], [])
        self.assertEqual(count_discovered(self.workspace), 1)

    def test_a_mislocated_gold_set_is_found_and_rejected_by_location(self):
        self.write("docs/_gold_sets/scheduler-core.json", gold_set())
        findings = [str(f) for f in validate(self.workspace)]
        self.assertTrue(any("not directly under the canonical directory" in f for f in findings))

    def test_load_gold_sets_omits_invalid_ones(self):
        broken = gold_set()
        broken["edges"][0]["caller"] = {"concept": "Clock", "method": "now"}
        self.write("specs/_gold_sets/scheduler-core.json", broken)
        self.assertEqual(load_gold_sets(self.workspace), {})

    def test_load_gold_sets_indexes_by_cluster(self):
        self.write("specs/_gold_sets/scheduler-core.json", gold_set())
        self.assertEqual(sorted(load_gold_sets(self.workspace)), ["scheduler-core"])

    def test_empty_scan_reports_honestly(self):
        self.assertEqual(main([str(self.workspace)]), 0)

    def test_find_gold_set_files_needs_a_real_root(self):
        with self.assertRaises(FileNotFoundError):
            find_gold_set_files(self.workspace / "nope")

    def test_cli_fails_on_an_invalid_gold_set(self):
        broken = gold_set()
        broken["edges"][0]["derived_from"] = ["interview"]
        self.write("specs/_gold_sets/scheduler-core.json", broken)
        self.assertEqual(main([str(self.workspace)]), 1)


if __name__ == "__main__":
    unittest.main()
