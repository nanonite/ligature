import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from extract_c_static import (  # noqa: E402
    COMPLETENESS_CLAIM,
    DEFAULT_RISK_TIER,
    EXTRACTOR_BACKING,
    ExtractionError,
    count_macro_invocations,
    attribute_ranges,
    extract_crate,
    find_method_spans,
    main,
    strip_noncode,
    tokenize,
)
from validate_callsites import load_validator, validate_data  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "callsites" / "crate_scheduler" / "src" / "lib.rs"

CONFIG_SCOPE = {"target": "x86_64-unknown-linux-gnu", "features": [], "cfg": []}


def extract_fixture(previous=None, source: str | None = None) -> dict:
    """Extract from the checked-in Rust fixture (or an inline override),
    laid out under a temporary workspace so paths in the report are
    workspace-relative the way a real run's are."""
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        crate_root = workspace / "crates" / "scheduler"
        (crate_root / "src").mkdir(parents=True)
        (crate_root / "src" / "lib.rs").write_text(source if source is not None else FIXTURE.read_text())
        return extract_crate(
            crate_root, workspace, "crates_scheduler", "crates/scheduler", CONFIG_SCOPE, previous
        )


def by_id(report: dict) -> dict[str, dict]:
    return {c["callsite_id"]: c for c in report["callsites"]}


class StripNonCodeTest(unittest.TestCase):
    """The step that separates this from a regex-only extractor. Each case
    below is one a pattern match over raw source gets wrong."""

    def test_line_and_block_comments_are_blanked(self):
        code = strip_noncode("let a = 1; // hidden_line()\n/* hidden_block() */ visible();")
        self.assertNotIn("hidden_line", code)
        self.assertNotIn("hidden_block", code)
        self.assertIn("visible()", code)

    def test_nested_block_comment(self):
        code = strip_noncode("/* outer /* inner */ still */ real.call();")
        self.assertIn("real.call()", code)
        self.assertNotIn("inner", code)

    def test_string_and_raw_string_contents_are_blanked(self):
        code = strip_noncode('let s = "x.call()"; let r = r#"y.call()"#; z.call();')
        self.assertNotIn("x", code)
        self.assertNotIn("y", code)
        self.assertIn("z.call()", code)

    def test_lifetime_is_not_a_char_literal(self):
        # `'a` followed later by another `'` would let a naive scanner
        # swallow every token in between, silently losing real calls.
        code = strip_noncode("impl<'a> Foo<'a> { fn f(&'a self) { self.g(); } }")
        self.assertIn("self.g()", code)

    def test_char_literal_is_blanked(self):
        code = strip_noncode("let c = '('; real.call();")
        self.assertIn("real.call()", code)

    def test_length_and_lines_are_preserved(self):
        src = 'a();\n// comment\n"string"\nb();\n'
        code = strip_noncode(src)
        self.assertEqual(len(code), len(src))
        self.assertEqual(code.count("\n"), src.count("\n"))


class ClassificationTest(unittest.TestCase):
    def setUp(self):
        self.report = extract_fixture()
        self.sites = by_id(self.report)

    def test_associated_path_call_is_definite(self):
        site = self.sites["CS-SCHEDULER-DISPATCH-001"]
        self.assertEqual(site["call_class"], "definite-direct-call")
        self.assertEqual(site["callee"], {"concept": "TaskQueue", "method": "pop_ready"})
        self.assertEqual(site["source"]["symbol"], "Scheduler::dispatch")

    def test_self_call_resolves_to_the_enclosing_concept(self):
        matches = [
            c for c in self.report["callsites"]
            if c["call_class"] == "definite-direct-call"
            and c["callee"] == {"concept": "Scheduler", "method": "validate"}
        ]
        self.assertEqual(len(matches), 1)

    def test_method_call_on_a_field_is_possible_dispatch(self):
        matches = [c for c in self.report["callsites"] if c["call_class"] == "possible-dispatch"]
        self.assertTrue(matches)
        site = matches[0]
        # The method NAME is known; a concept is never invented for it.
        self.assertEqual(site["callee_method"], "push_back")
        self.assertNotIn("callee", site)
        self.assertIn("push_back", site["unresolved_expression"])

    def test_call_through_a_binding_is_unresolved_indirect(self):
        matches = [c for c in self.report["callsites"] if c["call_class"] == "unresolved-indirect-call"]
        self.assertTrue(matches)
        self.assertNotIn("callee", matches[0])
        self.assertNotIn("callee_method", matches[0])

    def test_calls_inside_comments_and_strings_are_not_discovered(self):
        for site in self.report["callsites"]:
            self.assertNotEqual(site.get("callee_method"), "never_called")
            self.assertNotIn("never_called", site.get("unresolved_expression", ""))

    def test_trait_impl_methods_are_scanned(self):
        self.assertIn("CS-SCHEDULER-CLONE-001", self.sites)

    def test_enum_variant_construction_is_not_a_call(self):
        for site in self.report["callsites"]:
            self.assertNotEqual(site.get("callee", {}).get("method"), "Ready")

    def test_turbofish_does_not_hide_the_method(self):
        report = extract_fixture(source=(
            "pub struct A; pub struct B;\n"
            "impl A { pub fn go(&self) { B::make::<u8>(); } }\n"
            "impl B { pub fn make<T>() {} }\n"
        ))
        callees = [c.get("callee") for c in report["callsites"]]
        self.assertIn({"concept": "B", "method": "make"}, callees)

    def test_calls_to_non_local_types_are_out_of_scope_not_unresolved(self):
        # HashMap::new() is a resolved call that simply is not an edge
        # between local concept methods -- tiering it `medium` would bury
        # the real dispatch risk in noise.
        self.assertGreater(self.report["attribution_scope"]["out_of_scope_call_sites"], 0)
        for site in self.report["callsites"]:
            self.assertNotEqual(site.get("callee", {}).get("concept"), "HashMap")


class HonestyTest(unittest.TestCase):
    def setUp(self):
        self.report = extract_fixture()

    def test_a_syntactic_extractor_never_claims_soundness(self):
        scope = self.report["coverage_scope"]
        self.assertEqual(scope["extractor_backing"], EXTRACTOR_BACKING)
        self.assertEqual(scope["extractor_backing"], "syntactic")
        self.assertEqual(scope["completeness_claim"], COMPLETENESS_CLAIM)
        self.assertEqual(scope["completeness_claim"], "discovered-lower-bound")
        self.assertNotEqual(scope["completeness_claim"], "sound-for-supported-forms")

    def test_unsupported_call_forms_are_declared(self):
        self.assertIn("macro-expanded-call", self.report["coverage_scope"]["unsupported_call_forms"])

    def test_unattributed_and_macro_blind_spots_are_counted(self):
        scope = self.report["attribution_scope"]
        # the fixture's free function `run` makes calls that have no
        # concept method identity, and its println! is a macro
        self.assertGreater(scope["unattributed_call_sites"], 0)
        self.assertGreater(scope["macro_invocations"], 0)

    def test_item_signatures_are_not_counted_as_calls(self):
        report = extract_fixture(source="pub struct A;\nimpl A { pub fn go(&self, n: u64) {} }\n")
        self.assertEqual(report["attribution_scope"]["unattributed_call_sites"], 0)
        self.assertEqual(report["callsite_coverage"]["discovered"], 0)

    def test_attribute_arguments_are_not_counted_as_calls(self):
        report = extract_fixture(source=(
            "#[derive(Clone)]\npub struct A;\n"
            "impl A { #[inline] pub fn go(&self) {} }\n"
        ))
        self.assertEqual(report["attribution_scope"]["unattributed_call_sites"], 0)
        self.assertEqual(report["attribution_scope"]["macro_invocations"], 0)

    def test_local_concept_universe_is_recorded(self):
        self.assertEqual(
            self.report["attribution_scope"]["local_concepts"], ["Scheduler", "TaskQueue"]
        )

    def test_coverage_counts_agree_with_the_callsites(self):
        coverage = self.report["callsite_coverage"]
        callsites = self.report["callsites"]
        resolved = sum(1 for c in callsites if c["call_class"] == "definite-direct-call")
        self.assertEqual(coverage["discovered"], len(callsites))
        self.assertEqual(coverage["resolved"], resolved)
        self.assertEqual(coverage["unresolved"], len(callsites) - resolved)
        self.assertGreater(coverage["unresolved"], 0)


class RiskTierTest(unittest.TestCase):
    def setUp(self):
        self.report = extract_fixture()

    def test_extractor_only_ever_emits_a_medium_default(self):
        unresolved = [c for c in self.report["callsites"] if c["call_class"] != "definite-direct-call"]
        self.assertTrue(unresolved)
        for site in unresolved:
            self.assertEqual(site["risk_tier"], DEFAULT_RISK_TIER)
            self.assertEqual(site["risk_tier"], "medium")
            self.assertEqual(site["risk_tier_source"], "extractor-default")
            self.assertNotIn("risk_review", site)

    def test_resolved_calls_carry_no_tier(self):
        for site in self.report["callsites"]:
            if site["call_class"] == "definite-direct-call":
                self.assertNotIn("risk_tier", site)
                self.assertEqual(site["risk_tier_source"], "not-applicable")

    def test_human_tier_survives_re_extraction(self):
        first = extract_fixture()
        target = next(c for c in first["callsites"] if c["call_class"] != "definite-direct-call")
        target["risk_tier"] = "low"
        target["risk_tier_source"] = "human"
        target["risk_review"] = {"reviewer": "alice", "reviewed_at": "2026-09-04"}

        second = extract_fixture(previous=first)
        carried = by_id(second)[target["callsite_id"]]
        self.assertEqual(carried["risk_tier"], "low")
        self.assertEqual(carried["risk_tier_source"], "human")
        self.assertEqual(carried["risk_review"], {"reviewer": "alice", "reviewed_at": "2026-09-04"})

    def test_extractor_defaults_are_re_derived_not_carried(self):
        first = extract_fixture()
        for site in first["callsites"]:
            if site["call_class"] != "definite-direct-call":
                site["risk_tier"] = "critical"  # never signed off by anyone
        second = extract_fixture(previous=first)
        for site in second["callsites"]:
            if site["call_class"] != "definite-direct-call":
                self.assertEqual(site["risk_tier"], "medium")

    def test_a_previous_report_without_a_review_block_is_not_trusted(self):
        first = extract_fixture()
        target = next(c for c in first["callsites"] if c["call_class"] != "definite-direct-call")
        target["risk_tier"] = "low"
        target["risk_tier_source"] = "human"  # but no risk_review
        second = extract_fixture(previous=first)
        self.assertEqual(by_id(second)[target["callsite_id"]]["risk_tier"], "medium")


class DeterminismTest(unittest.TestCase):
    def test_re_extraction_of_unchanged_source_is_byte_identical(self):
        self.assertEqual(json.dumps(extract_fixture()), json.dumps(extract_fixture()))

    def test_callsite_ids_identify_their_caller(self):
        for site in extract_fixture()["callsites"]:
            caller = site["caller"]
            prefix = f"CS-{caller['concept'].upper()}-{caller['method'].upper().replace('_', '-')}-"
            self.assertTrue(site["callsite_id"].startswith(prefix))


class SchemaConformanceTest(unittest.TestCase):
    def test_generated_report_passes_g1a_g1b(self):
        report = extract_fixture()
        path = Path("ci/results/c_static/crates_scheduler.json")
        findings = validate_data(path, report, load_validator())
        self.assertEqual([str(f) for f in findings], [])


class HelperTest(unittest.TestCase):
    def test_find_method_spans_skips_free_functions_and_trait_defaults(self):
        tokens = tokenize(strip_noncode(
            "fn free() { }\n"
            "trait T { fn defaulted(&self) { } }\n"
            "pub struct A;\n"
            "impl A { fn real(&self) { } }\n"
        ))
        spans = find_method_spans(tokens)
        self.assertEqual([(s.concept, s.method) for s in spans], [("A", "real")])

    def test_impl_generic_parameter_is_not_mistaken_for_the_type(self):
        tokens = tokenize(strip_noncode("impl<T> Wrapper<T> { fn get(&self) {} }"))
        self.assertEqual([s.concept for s in find_method_spans(tokens)], ["Wrapper"])

    def test_trait_impl_names_the_type_not_the_trait(self):
        tokens = tokenize(strip_noncode("impl Clone for Wrapper { fn clone(&self) -> Self { } }"))
        self.assertEqual([s.concept for s in find_method_spans(tokens)], ["Wrapper"])

    def test_bodyless_trait_signature_does_not_capture_a_later_block(self):
        tokens = tokenize(strip_noncode(
            "trait T { fn required(&self); }\n pub struct A;\n impl A { fn real(&self) {} }"
        ))
        self.assertEqual([(s.concept, s.method) for s in find_method_spans(tokens)], [("A", "real")])

    def test_macro_counting_ignores_inner_attributes(self):
        tokens = tokenize(strip_noncode("#![allow(dead_code)]\nfn f() { println!(\"x\"); vec![1]; }"))
        self.assertEqual(count_macro_invocations(tokens, attribute_ranges(tokens)), 2)


class CliTest(unittest.TestCase):
    def test_writes_a_report_and_reports_its_own_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            crate = workspace / "crates" / "scheduler" / "src"
            crate.mkdir(parents=True)
            (crate / "lib.rs").write_text(FIXTURE.read_text())
            out = workspace / "ci" / "results" / "c_static" / "crates_scheduler.json"
            code = main([
                str(workspace / "crates" / "scheduler"),
                "--workspace", str(workspace),
                "--crate-dir", "crates/scheduler",
                "--report-id", "crates_scheduler",
                "--target", "x86_64-unknown-linux-gnu",
                "--out", str(out),
            ])
            self.assertEqual(code, 0)
            report = json.loads(out.read_text())
            self.assertEqual(report["report_id"], out.stem)
            self.assertEqual(report["config_scope"]["target"], "x86_64-unknown-linux-gnu")

    def test_missing_crate_root_is_an_error_not_an_empty_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ExtractionError):
                extract_crate(
                    Path(tmp) / "nope", Path(tmp), "r", "crates/nope", CONFIG_SCOPE, None
                )


if __name__ == "__main__":
    unittest.main()
