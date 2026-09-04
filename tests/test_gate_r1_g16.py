import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from gate_r1_g16 import (  # noqa: E402
    EXIT_BLOCKED,
    EXIT_DECISION_REQUIRED,
    EXIT_OK,
    HONEST_COVERAGE_STATEMENT,
    coverage_statement,
    gate_workspace,
    main,
    reconcile,
    report_findings,
    unresolved_at_or_above_medium,
)
from validate_callsites import callsite_report_dir_for  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))
from test_validate_callsites import valid_report  # noqa: E402

REPORT_PATH = Path("ci/results/c_static/crates_scheduler.json")

FORBIDDEN_CLAIM = "all call sites resolved"


def interaction(
    interaction_id="scheduler_dispatch__to__task_queue_pop_ready",
    caller=("Scheduler", "dispatch"),
    callee=("TaskQueue", "pop_ready"),
    target="x86_64-unknown-linux-gnu",
    features=None,
    cfg=None,
) -> dict:
    return {
        "schema_version": "1.0",
        "interaction_id": interaction_id,
        "caller": {"concept": caller[0], "method": caller[1]},
        "callee": {"concept": callee[0], "method": callee[1]},
        "edge_class": ["stateful"],
        "eligibility": "boundary-required",
        "rationale": "declared",
        "protocol_class": "pairwise",
        # G2++ requires a boundary-required edge to declare its assurance
        # requirement; load_interactions_by_id only trusts fully valid
        # interactions, so an under-specified one would never reach R1.
        "reliances": [
            {
                "obligation_id": "TaskQueue.C003",
                "required_assurance": {
                    "required_claims": ["postcondition-holds"],
                    "accepted_evidence_kinds": ["creusot-deductive-check"],
                    "minimum_scope": {"input_domain": "all"},
                    "trust_policy": {"assumptions_allowed": []},
                },
            }
        ],
        "realization": {
            "requirement": "required",
            "config_scope": {
                "target": target,
                "features": features or [],
                "cfg": cfg or [],
            },
        },
        "review": {"reviewer": "alice", "reviewed_at": "2026-09-04"},
    }


def interactions(*records) -> dict:
    return {r["interaction_id"]: r for r in records}


def only_resolved(report: dict) -> dict:
    report["callsites"] = [c for c in report["callsites"] if c["call_class"] == "definite-direct-call"]
    report["callsite_coverage"] = {"discovered": 1, "resolved": 1, "unresolved": 0}
    return report


def gate(report: dict, known: dict):
    return reconcile(REPORT_PATH, report, known)


def texts(findings, gate_name=None, severity=None) -> list[str]:
    return [
        str(f) for f in findings
        if (gate_name is None or f.gate == gate_name) and (severity is None or f.severity == severity)
    ]


class R1DriftTest(unittest.TestCase):
    def test_definite_cross_concept_call_absent_from_i_blocks(self):
        findings, counts = gate(only_resolved(valid_report()), interactions())
        self.assertTrue(any("absent from I" in t for t in texts(findings, "R1", "error")))
        self.assertEqual(counts.checked, 0)

    def test_declared_interaction_is_checked_not_flagged(self):
        findings, counts = gate(only_resolved(valid_report()), interactions(interaction()))
        self.assertEqual(texts(findings, severity="error"), [])
        self.assertEqual(counts.checked, 1)

    def test_intra_concept_call_is_an_ignored_internal_helper(self):
        report = only_resolved(valid_report())
        report["callsites"][0]["callee"] = {"concept": "Scheduler", "method": "validate"}
        findings, counts = gate(report, interactions())
        self.assertEqual(texts(findings, severity="error"), [])
        self.assertTrue(any("internal helper" in t for t in texts(findings, "R1", "info")))
        self.assertEqual(counts.checked, 1)

    def test_intended_edge_with_no_realized_call_is_information_not_a_failure(self):
        report = only_resolved(valid_report())
        report["callsites"] = []
        report["callsite_coverage"] = {"discovered": 0, "resolved": 0, "unresolved": 0}
        findings, _ = gate(report, interactions(interaction()))
        self.assertEqual(texts(findings, severity="error"), [])
        self.assertTrue(any("intent without realization" in t for t in texts(findings, "R1", "info")))


class ConfigurationCompatibilityTest(unittest.TestCase):
    def test_target_mismatch_is_not_a_match(self):
        findings, _ = gate(
            only_resolved(valid_report()),
            interactions(interaction(target="aarch64-apple-darwin")),
        )
        self.assertTrue(any("compatible configuration" in t for t in texts(findings, "R1", "error")))

    def test_feature_difference_is_visible_but_not_blocking(self):
        findings, _ = gate(
            only_resolved(valid_report()),
            interactions(interaction(features=["fast-path"])),
        )
        self.assertEqual(texts(findings, severity="error"), [])
        self.assertTrue(any("features=" in t for t in texts(findings, "R1", "info")))


class RiskTierPolicyTest(unittest.TestCase):
    """plan.md §9.1: critical/high block, medium is a human decision, low
    is a visible accepted limitation. Never a blanket rule in either
    direction."""

    def unresolved_report(self, tier: str, call_class="unresolved-indirect-call") -> dict:
        report = valid_report()
        site = report["callsites"][1]
        site["call_class"] = call_class
        if call_class != "possible-dispatch":
            site.pop("callee_method", None)
        site["risk_tier"] = tier
        if tier != "medium":
            site["risk_tier_source"] = "human"
            site["risk_review"] = {"reviewer": "alice", "reviewed_at": "2026-09-04"}
        return report

    def dispositions(self, tier: str) -> list[str]:
        findings, _ = gate(self.unresolved_report(tier), interactions(interaction()))
        return [f.severity for f in findings if f.gate == "G16"]

    def test_critical_blocks(self):
        self.assertEqual(self.dispositions("critical"), ["error"])

    def test_high_blocks(self):
        self.assertEqual(self.dispositions("high"), ["error"])

    def test_medium_is_a_human_decision(self):
        self.assertEqual(self.dispositions("medium"), ["decision"])

    def test_low_is_a_visible_accepted_limitation(self):
        self.assertEqual(self.dispositions("low"), ["info"])

    def test_unresolved_never_counts_toward_coverage(self):
        _, counts = gate(self.unresolved_report("low"), interactions(interaction()))
        self.assertEqual(counts.unresolved, 1)
        self.assertEqual(counts.checked, 1)
        self.assertEqual(counts.discovered, 2)

    def test_dispatch_matching_no_intended_edge_is_tiered_by_r1_too(self):
        report = self.unresolved_report("high", call_class="possible-dispatch")
        findings, _ = gate(report, interactions(interaction()))
        self.assertTrue(any("matches no intended edge" in t for t in texts(findings, "R1", "error")))

    def test_dispatch_consistent_with_i_is_consistency_not_resolution(self):
        report = self.unresolved_report("low", call_class="possible-dispatch")
        report["callsites"][1]["callee_method"] = "pop_ready"
        findings, counts = gate(report, interactions(interaction()))
        self.assertTrue(any("consistency is not a resolution" in t for t in texts(findings, "R1", "info")))
        self.assertEqual(counts.unresolved, 1)

    def test_closure_profile_condition_counts_medium_and_above(self):
        self.assertEqual(unresolved_at_or_above_medium(self.unresolved_report("low")), 0)
        self.assertEqual(unresolved_at_or_above_medium(self.unresolved_report("medium")), 1)
        self.assertEqual(unresolved_at_or_above_medium(self.unresolved_report("critical")), 1)


class HonestReportingTest(unittest.TestCase):
    """plan.md §9: "all discovered call sites resolved", never "all call
    sites resolved" -- the extractor's own claim is a lower bound, so
    undiscovered call sites always remain possible (CG2)."""

    def test_the_only_coverage_claim_is_about_discovered_call_sites(self):
        report = only_resolved(valid_report())
        _, counts = gate(report, interactions(interaction()))
        statement = coverage_statement(counts)
        self.assertIn(HONEST_COVERAGE_STATEMENT, statement)
        self.assertNotIn(f" {FORBIDDEN_CLAIM}", statement)
        self.assertTrue(statement.startswith("all discovered"))

    def test_unresolved_sites_are_stated_as_a_fraction_of_discovered(self):
        findings, counts = gate(valid_report(), interactions(interaction()))
        statement = coverage_statement(counts)
        self.assertNotIn(HONEST_COVERAGE_STATEMENT, statement)
        self.assertIn("of 2 discovered call sites are unresolved", statement)

    def test_no_printed_output_ever_claims_full_coverage(self):
        scenarios = [
            (only_resolved(valid_report()), interactions(interaction())),
            (valid_report(), interactions(interaction())),
            (valid_report(), interactions()),
            (only_resolved(valid_report()), interactions()),
        ]
        for report, known in scenarios:
            findings, counts = gate(copy.deepcopy(report), known)
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                report_findings(findings, counts)
            printed = buffer.getvalue()
            self.assertNotIn(f" {FORBIDDEN_CLAIM}", printed)
            self.assertNotIn(f"\n{FORBIDDEN_CLAIM}", printed)


class ExitCodeTest(unittest.TestCase):
    def run_gate(self, report: dict, known: dict) -> int:
        findings, counts = gate(report, known)
        with redirect_stdout(io.StringIO()):
            return report_findings(findings, counts)

    def test_clean_reconciliation_passes(self):
        self.assertEqual(self.run_gate(only_resolved(valid_report()), interactions(interaction())), EXIT_OK)

    def test_medium_risk_is_neither_a_pass_nor_a_block(self):
        code = self.run_gate(valid_report(), interactions(interaction()))
        self.assertEqual(code, EXIT_DECISION_REQUIRED)
        self.assertNotIn(code, (EXIT_OK, EXIT_BLOCKED))

    def test_drift_blocks(self):
        self.assertEqual(self.run_gate(only_resolved(valid_report()), interactions()), EXIT_BLOCKED)

    def test_a_block_is_reported_alongside_outstanding_decisions(self):
        buffer = io.StringIO()
        findings, counts = gate(valid_report(), interactions())
        with redirect_stdout(buffer):
            code = report_findings(findings, counts)
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertIn("awaiting a human risk decision", buffer.getvalue())


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        canonical = callsite_report_dir_for(self.workspace)
        canonical.mkdir(parents=True)
        self.canonical = canonical
        self.interactions_dir = self.workspace / "crates" / "scheduler" / "specs" / "_interactions"
        self.interactions_dir.mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def write_report(self, report: dict) -> None:
        (self.canonical / f"{report['report_id']}.json").write_text(json.dumps(report))

    def write_interaction(self, record: dict) -> None:
        (self.interactions_dir / f"{record['interaction_id']}.json").write_text(json.dumps(record))

    def test_report_naming_an_unknown_crate_is_an_error_not_a_skip(self):
        report = valid_report()
        report["crate_dir"] = "crates/ghost"
        self.write_report(report)
        findings, _ = gate_workspace(self.workspace, {"crates/scheduler": self.interactions_dir})
        self.assertTrue(any("not one of the project descriptor's own crates" in t for t in texts(findings)))

    def test_reconciles_against_the_crate_named_by_the_report(self):
        self.write_report(only_resolved(valid_report()))
        self.write_interaction(interaction())
        findings, counts = gate_workspace(self.workspace, {"crates/scheduler": self.interactions_dir})
        self.assertEqual(texts(findings, severity="error"), [])
        self.assertEqual(counts.checked, 1)

    def test_an_invalid_report_never_reaches_reconciliation(self):
        broken = valid_report()
        broken["callsite_coverage"] = {"discovered": 2, "resolved": 2, "unresolved": 0}
        self.write_report(broken)
        findings, counts = gate_workspace(self.workspace, {"crates/scheduler": self.interactions_dir})
        self.assertEqual(findings, [])
        self.assertEqual(counts.discovered, 0)

    def test_cli_requires_reports_to_exist(self):
        empty = Path(self._tmp.name) / "empty"
        empty.mkdir()
        self.assertEqual(main([str(empty), "--interactions", f"crates/scheduler={self.interactions_dir}"]), 2)

    def test_cli_rejects_a_malformed_interactions_mapping(self):
        self.write_report(only_resolved(valid_report()))
        self.assertEqual(main([str(self.workspace), "--interactions", "no-equals-sign"]), 2)

    def test_cli_end_to_end(self):
        self.write_report(only_resolved(valid_report()))
        self.write_interaction(interaction())
        with redirect_stdout(io.StringIO()):
            code = main([str(self.workspace), "--interactions", f"crates/scheduler={self.interactions_dir}"])
        self.assertEqual(code, EXIT_OK)


if __name__ == "__main__":
    unittest.main()
