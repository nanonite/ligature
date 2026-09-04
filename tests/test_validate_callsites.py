import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from validate_callsites import (  # noqa: E402
    callsite_report_dir_for,
    find_report_files,
    load_reports,
    load_validator,
    main,
    validate,
    validate_data,
    validate_workspace,
)

REPORT_PATH = Path("ci/results/c_static/crates_scheduler.json")

RESOLVED_SITE = {
    "callsite_id": "CS-SCHEDULER-DISPATCH-001",
    "call_class": "definite-direct-call",
    "caller": {"concept": "Scheduler", "method": "dispatch"},
    "callee": {"concept": "TaskQueue", "method": "pop_ready"},
    "source": {
        "path": "crates/scheduler/src/lib.rs",
        "symbol": "Scheduler::dispatch",
        "line": 24,
        "syntax_hash": "sha256:" + "a" * 64,
    },
    "risk_tier_source": "not-applicable",
}

DISPATCH_SITE = {
    "callsite_id": "CS-SCHEDULER-DISPATCH-002",
    "call_class": "possible-dispatch",
    "caller": {"concept": "Scheduler", "method": "dispatch"},
    "callee_method": "push_back",
    "unresolved_expression": "self.queue.push_back(...)",
    "source": {
        "path": "crates/scheduler/src/lib.rs",
        "symbol": "Scheduler::dispatch",
        "line": 27,
        "syntax_hash": "sha256:" + "a" * 64,
    },
    "risk_tier": "medium",
    "risk_tier_source": "extractor-default",
}


def valid_report() -> dict:
    return {
        "schema_version": "1.0",
        "report_id": "crates_scheduler",
        "crate_dir": "crates/scheduler",
        "coverage_scope": {
            "extractor": "coarse-syntactic-callgraph",
            "extractor_version": "0.1.0",
            "extractor_backing": "syntactic",
            "supported_call_forms": ["associated-path-call"],
            "unsupported_call_forms": ["macro-expanded-call"],
            "completeness_claim": "discovered-lower-bound",
        },
        "config_scope": {"target": "x86_64-unknown-linux-gnu", "features": [], "cfg": []},
        "attribution_scope": {
            "scanned_item_kinds": ["inherent-impl-method", "trait-impl-method"],
            "local_concepts": ["Scheduler", "TaskQueue"],
            "unattributed_call_sites": 2,
            "out_of_scope_call_sites": 4,
            "macro_invocations": 1,
        },
        "callsites": [copy.deepcopy(RESOLVED_SITE), copy.deepcopy(DISPATCH_SITE)],
        "callsite_coverage": {"discovered": 2, "resolved": 1, "unresolved": 1},
    }


def findings_for(report: dict, path: Path = REPORT_PATH) -> list[str]:
    return [str(f) for f in validate_data(path, report, load_validator())]


class ValidReportTest(unittest.TestCase):
    def test_canonical_report_passes(self):
        self.assertEqual(findings_for(valid_report()), [])


class SchemaHonestyTest(unittest.TestCase):
    """chainlink #24's "no regex-only completeness claim", enforced by the
    schema itself so no future extractor can promote its own claim."""

    def test_syntactic_extractor_may_not_claim_soundness(self):
        report = valid_report()
        report["coverage_scope"]["completeness_claim"] = "sound-for-supported-forms"
        self.assertTrue(findings_for(report))

    def test_compiler_backed_extractor_may_claim_soundness(self):
        report = valid_report()
        report["coverage_scope"]["extractor_backing"] = "compiler"
        report["coverage_scope"]["completeness_claim"] = "sound-for-supported-forms"
        self.assertEqual(findings_for(report), [])

    def test_unknown_completeness_claim_is_rejected(self):
        report = valid_report()
        report["coverage_scope"]["completeness_claim"] = "complete"
        self.assertTrue(findings_for(report))

    def test_report_may_not_carry_a_review_block(self):
        # A C_static report is an observation, not a promoted artifact.
        report = valid_report()
        report["review"] = {"reviewer": "alice", "reviewed_at": "2026-09-04"}
        self.assertTrue(findings_for(report))


class CallsiteShapeTest(unittest.TestCase):
    def test_resolved_call_may_not_carry_a_risk_tier(self):
        report = valid_report()
        report["callsites"][0]["risk_tier"] = "low"
        self.assertTrue(findings_for(report))

    def test_unresolved_call_must_carry_a_risk_tier(self):
        report = valid_report()
        del report["callsites"][1]["risk_tier"]
        self.assertTrue(findings_for(report))

    def test_possible_dispatch_may_not_name_a_callee_concept(self):
        report = valid_report()
        report["callsites"][1]["callee"] = {"concept": "TaskQueue", "method": "push_back"}
        self.assertTrue(findings_for(report))

    def test_possible_dispatch_must_name_the_method_it_did_resolve(self):
        report = valid_report()
        del report["callsites"][1]["callee_method"]
        self.assertTrue(findings_for(report))

    def test_unresolved_indirect_call_may_not_name_a_method(self):
        report = valid_report()
        site = report["callsites"][1]
        site["call_class"] = "unresolved-indirect-call"
        self.assertTrue(findings_for(report))

    def test_human_tier_requires_a_review_block(self):
        report = valid_report()
        report["callsites"][1]["risk_tier_source"] = "human"
        self.assertTrue(findings_for(report))

    def test_extractor_default_may_not_carry_a_review_block(self):
        report = valid_report()
        report["callsites"][1]["risk_review"] = {"reviewer": "alice", "reviewed_at": "2026-09-04"}
        self.assertTrue(findings_for(report))

    def test_human_tier_with_a_review_block_passes(self):
        report = valid_report()
        report["callsites"][1]["risk_tier"] = "low"
        report["callsites"][1]["risk_tier_source"] = "human"
        report["callsites"][1]["risk_review"] = {"reviewer": "alice", "reviewed_at": "2026-09-04"}
        self.assertEqual(findings_for(report), [])

    def test_review_date_format_is_enforced(self):
        report = valid_report()
        report["callsites"][1]["risk_tier_source"] = "human"
        report["callsites"][1]["risk_review"] = {"reviewer": "alice", "reviewed_at": "not-a-date"}
        self.assertTrue(findings_for(report))


class ComputedCoverageTest(unittest.TestCase):
    """Coverage is recomputed, never trusted -- the same discipline G1b
    applies to computed eligibility in I."""

    def test_understated_unresolved_is_rejected(self):
        report = valid_report()
        report["callsite_coverage"] = {"discovered": 2, "resolved": 2, "unresolved": 0}
        findings = findings_for(report)
        self.assertTrue(any("disagrees with the counts recomputed" in f for f in findings))

    def test_overstated_unresolved_is_rejected(self):
        report = valid_report()
        report["callsite_coverage"] = {"discovered": 2, "resolved": 0, "unresolved": 2}
        self.assertTrue(findings_for(report))

    def test_discovered_must_match_the_list_length(self):
        report = valid_report()
        report["callsite_coverage"]["discovered"] = 3
        self.assertTrue(findings_for(report))

    def test_empty_report_is_internally_consistent(self):
        report = valid_report()
        report["callsites"] = []
        report["callsite_coverage"] = {"discovered": 0, "resolved": 0, "unresolved": 0}
        self.assertEqual(findings_for(report), [])


class IdentityTest(unittest.TestCase):
    def test_duplicate_callsite_id_is_rejected(self):
        report = valid_report()
        report["callsites"][1]["callsite_id"] = report["callsites"][0]["callsite_id"]
        self.assertTrue(any("duplicate callsite_id" in f for f in findings_for(report)))

    def test_callsite_id_must_identify_its_own_caller(self):
        report = valid_report()
        report["callsites"][0]["callsite_id"] = "CS-TASKQUEUE-POP-READY-001"
        self.assertTrue(any("does not identify its own caller" in f for f in findings_for(report)))

    def test_symbol_must_match_the_caller_role(self):
        report = valid_report()
        report["callsites"][0]["source"]["symbol"] = "TaskQueue::pop_ready"
        self.assertTrue(any("does not match the caller role" in f for f in findings_for(report)))

    def test_resolved_callee_outside_the_declared_universe_is_rejected(self):
        report = valid_report()
        report["attribution_scope"]["local_concepts"] = ["Scheduler"]
        self.assertTrue(any("local_concepts" in f for f in findings_for(report)))

    def test_snake_case_method_maps_to_a_hyphenated_id(self):
        report = valid_report()
        report["callsites"][0]["caller"]["method"] = "pop_ready"
        report["callsites"][0]["source"]["symbol"] = "Scheduler::pop_ready"
        report["callsites"][0]["callsite_id"] = "CS-SCHEDULER-POP-READY-001"
        self.assertEqual(findings_for(report), [])


class NamingTest(unittest.TestCase):
    def test_filename_stem_must_match_report_id(self):
        findings = findings_for(valid_report(), Path("ci/results/c_static/other.json"))
        self.assertTrue(any("does not match report_id" in f for f in findings))

    def test_nested_placement_is_rejected(self):
        findings = findings_for(valid_report(), Path("ci/results/c_static/nested/crates_scheduler.json"))
        self.assertTrue(any("not flat" in f for f in findings))

    def test_non_json_suffix_is_rejected(self):
        report = valid_report()
        report["report_id"] = "crates_scheduler.json"
        findings = findings_for(report, Path("ci/results/c_static/crates_scheduler.json.bak"))
        self.assertTrue(any("must be a .json file" in f for f in findings))


class WorkspaceScanTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        self.canonical = callsite_report_dir_for(self.workspace)
        self.canonical.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, relative: str, report: dict) -> Path:
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report))
        return path

    def test_canonical_report_passes_the_workspace_scan(self):
        self.write("ci/results/c_static/crates_scheduler.json", valid_report())
        self.assertEqual(validate(self.workspace), [])

    def test_mislocated_report_is_found_and_rejected_by_location(self):
        # Discovered crate-wide precisely so it is not silently skipped.
        self.write("ci/other/c_static/crates_scheduler.json", valid_report())
        findings = [str(f) for f in validate(self.workspace)]
        self.assertTrue(any("not directly under the canonical directory" in f for f in findings))

    def test_invalid_json_is_reported_not_skipped(self):
        path = self.canonical / "crates_scheduler.json"
        path.write_text("{ not json")
        self.assertTrue([str(f) for f in validate(self.workspace)])

    def test_find_report_files_needs_a_real_root(self):
        with self.assertRaises(FileNotFoundError):
            find_report_files(self.workspace / "nope")

    def test_load_reports_omits_reports_that_fail_g1a_g1b(self):
        self.write("ci/results/c_static/crates_scheduler.json", valid_report())
        bad = valid_report()
        bad["report_id"] = "crates_other"
        bad["callsite_coverage"] = {"discovered": 2, "resolved": 2, "unresolved": 0}
        self.write("ci/results/c_static/crates_other.json", bad)
        loaded = load_reports(self.workspace)
        self.assertEqual([r["report_id"] for _, r in loaded], ["crates_scheduler"])

    def test_cli_reports_pass_and_fail(self):
        self.write("ci/results/c_static/crates_scheduler.json", valid_report())
        self.assertEqual(main([str(self.workspace)]), 0)
        broken = valid_report()
        broken["callsite_coverage"]["unresolved"] = 0
        self.write("ci/results/c_static/crates_scheduler.json", broken)
        self.assertEqual(main([str(self.workspace)]), 1)

    def test_cli_on_a_missing_root_is_an_error(self):
        self.assertEqual(main([str(self.workspace / "nope")]), 2)

    def test_validate_workspace_accepts_an_explicit_canonical_dir(self):
        self.write("ci/results/c_static/crates_scheduler.json", valid_report())
        self.assertEqual(validate_workspace(self.workspace, self.canonical), [])


if __name__ == "__main__":
    unittest.main()
