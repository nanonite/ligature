import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from extract_c_static import EXTRACTOR_VERSION, compute_source_binding, extract_crate  # noqa: E402
from validate_callsites import (  # noqa: E402
    callsite_report_dir_for,
    find_report_files,
    load_reports,
    load_validator,
    main,
    shape_index_resolver,
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
        "schema_version": "1.1",
        "report_id": "crates_scheduler",
        "crate_dir": "crates/scheduler",
        "source_binding": {
            "algorithm": "sha256",
            "digest": "sha256:" + "a" * 64,
            "file_count": 1,
        },
        "coverage_scope": {
            "extractor": "coarse-syntactic-callgraph",
            "extractor_version": EXTRACTOR_VERSION,
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


def bind_report(report: dict, workspace: Path) -> dict:
    crate_root = workspace / report["crate_dir"]
    crate_root.mkdir(parents=True, exist_ok=True)
    report["schema_version"] = "1.1"
    report["coverage_scope"]["extractor_version"] = EXTRACTOR_VERSION
    report["source_binding"] = compute_source_binding(
        crate_root, report["crate_dir"], report["config_scope"]
    )
    return report


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


class CryptoMixedPilotRegressionTest(unittest.TestCase):
    """The captured 1.4.1 crypto-mixed report was extracted before its
    crate sources existed and claimed an empty call graph. Keep the exact
    report as a fixture so a later schema or loader change cannot silently
    turn that historical observation back into accepted zero coverage."""

    def test_pre_source_pilot_report_is_rejected_with_extract_action(self):
        fixture = ROOT / "tests/fixtures/callsites/crypto-mixed-pre-source.json"
        self.assertEqual(
            hashlib.sha256(fixture.read_bytes()).hexdigest(),
            "20845eb0132a8e7ee1c6b3714a2b0f3640ac3dd6e5ea056c33b4b9f4b241d857",
        )
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            crate_root = workspace / "crates/crypto-mixed-core"
            crate_root.mkdir(parents=True)
            reports = callsite_report_dir_for(workspace)
            reports.mkdir(parents=True)
            report_path = reports / fixture.name
            report_path.write_bytes(fixture.read_bytes())
            manifests = workspace / "ci/manifest"
            manifests.mkdir(parents=True)
            (manifests / "WP-AEAD.json").write_text(json.dumps({
                "functions": [f"crypto_mixed::aead::function_{index}" for index in range(1, 8)],
                "write_policy": {"allowed_write_set": ["crates/crypto-mixed-core/src/**"]},
            }))
            (manifests / "WP-PRIMITIVES.json").write_text(json.dumps({
                "functions": [f"crypto_mixed::primitives::function_{index}" for index in range(1, 7)],
                "write_policy": {"allowed_write_set": ["crates/crypto-mixed-core/src/**"]},
            }))

            findings = validate_workspace(workspace, reports)
            rendered = "\n".join(str(finding) for finding in findings)
            self.assertIn("legacy C_static report has no source binding", rendered)
            self.assertIn(
                "ligature extract-c-static --target x86_64-unknown-linux-gnu", rendered
            )
            self.assertEqual(load_reports(workspace), [])


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


class CalleeShapeRecomputationTest(unittest.TestCase):
    """chainlink #107. The recorded `callee_shape` is the only thing that
    stops R1 blocking a cross-concept call absent from I, so it is
    re-derived from the crate's own sources rather than trusted -- the same
    discipline `callsite_coverage` gets, and rejected in both directions."""

    SOURCE = """\
pub struct Year(pub i16);

impl Year {
    pub fn ok(&self) -> bool {
        self.0 != i16::MIN
    }
    pub fn advance(&self) -> i16 {
        let mut y = self.0;
        y += 1;
        y
    }
}

pub struct Ymd {
    probe: fn() -> bool,
}

impl Ymd {
    pub fn check(&self) -> bool {
        Year::ok(&Year(2026)) && Year::advance(&Year(2026)) > 0
    }
    pub fn run(&self) -> bool {
        let probe = self.probe;
        probe() && Year::ok(&Year(2026))
    }
}
"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        crate_root = self.workspace / "crates" / "dates"
        (crate_root / "src").mkdir(parents=True)
        (crate_root / "src" / "lib.rs").write_text(self.SOURCE)
        self.report = extract_crate(
            crate_root,
            self.workspace,
            "crates_dates",
            "crates/dates",
            {"target": "x86_64-unknown-linux-gnu", "features": [], "cfg": []},
            None,
        )
        self.resolve = shape_index_resolver(self.workspace)

    def tearDown(self):
        self._tmp.cleanup()

    def site(self, method: str) -> dict:
        for record in self.report["callsites"]:
            if record.get("callee", {}).get("method") == method:
                return record
        raise AssertionError(f"no resolved call into {method!r}")

    def findings(self, report: dict | None = None) -> list[str]:
        path = Path("ci/results/c_static") / f"{self.report['report_id']}.json"
        return [
            str(f)
            for f in validate_data(
                path,
                self.report if report is None else report,
                load_validator(),
                self.resolve,
            )
        ]

    def test_the_extracted_claim_is_re_derived_not_disputed(self):
        self.assertEqual(self.findings(), [])

    def test_a_predicate_callee_is_claimed_and_pinned(self):
        site = self.site("ok")
        self.assertEqual(site["callee_shape"], "value-domain-inquiry")
        self.assertRegex(site["callee_shape_evidence"]["syntax_hash"], r"^sha256:[0-9a-f]{64}$")

    def test_a_callee_that_computes_something_is_claimed_general(self):
        site = self.site("advance")
        self.assertEqual(site["callee_shape"], "general")
        self.assertNotIn("callee_shape_evidence", site)

    def test_claiming_a_relaxation_the_source_does_not_support_is_rejected(self):
        site = self.site("advance")
        site["callee_shape"] = "value-domain-inquiry"
        site["callee_shape_evidence"] = {"syntax_hash": "sha256:" + "a" * 64}
        self.assertTrue(any("disagrees with the 'general' recomputed" in f for f in self.findings()))

    def test_understating_a_relaxation_is_rejected_too(self):
        # Symmetry with computed eligibility: a shape downgraded to
        # `general` invents a blocker the source does not justify.
        site = self.site("ok")
        site["callee_shape"] = "general"
        del site["callee_shape_evidence"]
        self.assertTrue(
            any("disagrees with the 'value-domain-inquiry' recomputed" in f for f in self.findings())
        )

    def test_a_claim_pinned_to_a_body_that_has_since_changed_is_rejected(self):
        self.site("ok")["callee_shape_evidence"]["syntax_hash"] = "sha256:" + "b" * 64
        self.assertTrue(any("but that method's own source now hashes to" in f for f in self.findings()))

    def test_a_relaxation_must_arrive_pinned(self):
        del self.site("ok")["callee_shape_evidence"]
        self.assertTrue(self.findings())

    def test_a_general_claim_may_not_carry_a_pin(self):
        self.site("advance")["callee_shape_evidence"] = {"syntax_hash": "sha256:" + "c" * 64}
        self.assertTrue(self.findings())

    def test_a_shape_on_an_unresolved_call_is_rejected(self):
        for record in self.report["callsites"]:
            if record["call_class"] != "definite-direct-call":
                record["callee_shape"] = "general"
        self.assertTrue(self.findings())

    def test_a_claim_about_a_method_the_crate_does_not_define_is_rejected(self):
        site = self.site("ok")
        site["callee"]["method"] = "invented"
        self.assertTrue(any("not defined anywhere in the sources" in f for f in self.findings()))

    def test_a_claim_that_cannot_be_re_derived_at_all_is_rejected(self):
        # No sources to check it against is not the same as nothing to
        # complain about: an unverifiable claim is not a computed one.
        report = copy.deepcopy(self.report)
        report["crate_dir"] = "crates/not-on-disk"
        report["callsite_coverage"] = {"discovered": 2, "resolved": 2, "unresolved": 0}
        self.assertTrue(any("could not be read" in f for f in self.findings(report)))

    def test_a_report_with_no_recorded_shape_is_left_alone(self):
        # A report extracted before the classification existed is not a
        # finding: R1 reads the absence as `general`, which is the safe
        # direction, and invalidating an honest observation would only
        # force a re-extraction that changes nothing about it.
        report = copy.deepcopy(self.report)
        for record in report["callsites"]:
            record.pop("callee_shape", None)
            record.pop("callee_shape_evidence", None)
        self.assertEqual(self.findings(report), [])

    def test_a_fabricated_claim_never_reaches_the_gate(self):
        site = self.site("advance")
        site["callee_shape"] = "value-domain-inquiry"
        site["callee_shape_evidence"] = {"syntax_hash": "sha256:" + "d" * 64}
        self.write_report(self.report)
        self.assertEqual(load_reports(self.workspace), [])

    def test_a_re_derived_claim_does_reach_the_gate(self):
        self.write_report(self.report)
        self.assertEqual(
            [r["report_id"] for _, r in load_reports(self.workspace)], ["crates_dates"]
        )

    def write_report(self, report: dict) -> None:
        canonical = callsite_report_dir_for(self.workspace)
        canonical.mkdir(parents=True, exist_ok=True)
        (canonical / f"{report['report_id']}.json").write_text(json.dumps(report))


class SourceBindingFreshnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.crate = self.workspace / "crates" / "scheduler"
        self.crate.mkdir(parents=True)
        self.report_dir = callsite_report_dir_for(self.workspace)
        self.report_dir.mkdir(parents=True)
        self.report_path = self.report_dir / "crates_scheduler.json"

    def tearDown(self):
        self.tmp.cleanup()

    def extract(self) -> dict:
        return extract_crate(
            self.crate,
            self.workspace,
            "crates_scheduler",
            "crates/scheduler",
            {"target": "x86_64-unknown-linux-gnu", "features": [], "cfg": []},
        )

    def write_report(self, report: dict) -> None:
        self.report_path.write_text(json.dumps(report))

    def findings(self) -> list[str]:
        return [str(f) for f in validate_workspace(self.workspace, self.report_dir)]

    def test_source_change_after_extraction_is_refused(self):
        source = self.crate / "src" / "lib.rs"
        source.parent.mkdir()
        source.write_text("pub struct Scheduler; impl Scheduler { pub fn step(&self) {} }\n")
        self.write_report(self.extract())
        self.assertEqual(self.findings(), [])
        source.write_text(
            "pub struct Scheduler; impl Scheduler { pub fn step(&self) { self.next(); } "
            "pub fn next(&self) {} }\n"
        )
        findings = self.findings()
        self.assertTrue(any("source binding is stale" in item for item in findings), findings)
        self.assertTrue(any("ligature extract-c-static --target x86_64-unknown-linux-gnu" in item for item in findings), findings)

    def test_empty_pre_source_observation_with_manifest_owned_function_is_suspect(self):
        report = self.extract()
        self.assertEqual(report["attribution_scope"]["local_concepts"], [])
        self.assertEqual(report["callsites"], [])
        self.write_report(report)
        manifest_dir = self.workspace / "ci" / "manifest"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "WP-SCHEDULER.json").write_text(json.dumps({
            "functions": ["scheduler::Scheduler::step"],
            "write_policy": {"allowed_write_set": ["crates/scheduler/src/**"]},
        }))
        findings = self.findings()
        self.assertTrue(any("assigns functions to this crate" in item for item in findings), findings)

        source = self.crate / "src" / "lib.rs"
        source.parent.mkdir()
        source.write_text("pub struct Scheduler; impl Scheduler { pub fn step(&self) {} }\n")
        findings = self.findings()
        self.assertTrue(any("source binding is stale" in item for item in findings), findings)

    def test_fresh_extraction_is_accepted(self):
        source = self.crate / "src" / "lib.rs"
        source.parent.mkdir()
        source.write_text("pub struct Scheduler; impl Scheduler { pub fn step(&self) {} }\n")
        self.write_report(self.extract())
        self.assertEqual(self.findings(), [])

    def test_legacy_unbound_report_fails_with_exact_remediation(self):
        source = self.crate / "src" / "lib.rs"
        source.parent.mkdir()
        source.write_text("pub struct Scheduler; impl Scheduler { pub fn step(&self) {} }\n")
        report = self.extract()
        report["schema_version"] = "1.0"
        report.pop("source_binding")
        self.write_report(report)
        findings = self.findings()
        self.assertTrue(any("legacy C_static report has no source binding" in item for item in findings), findings)
        self.assertTrue(any("ligature extract-c-static --target x86_64-unknown-linux-gnu" in item for item in findings), findings)


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
        bind_report(report, self.workspace)
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
