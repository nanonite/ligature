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
    GENERAL_CALL,
    VALUE_DOMAIN_INQUIRY,
    ExtractionError,
    callee_shape_index,
    classify_callee_shape,
    count_macro_invocations,
    attribute_ranges,
    extract_crate,
    find_method_spans,
    main,
    method_syntax_hash,
    strip_noncode,
    tokenize,
)
from validate_callsites import load_validator, shape_index_resolver, validate_data  # noqa: E402

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


def shape_of(body: str, returns: str = "bool", signature: str = "&self", before_fn: str = "") -> str:
    """Classify one method of a one-concept crate, exactly as the extractor
    would while writing a report about it."""
    source = f"pub struct Y;\nimpl Y {{ {before_fn}pub fn probe({signature}) -> {returns} {{ {body} }} }}\n"
    tokens = tokenize(strip_noncode(source))
    for span in find_method_spans(tokens):
        if span.method == "probe":
            return classify_callee_shape(tokens, span)
    raise AssertionError(f"no probe method in {source!r}")


class CalleeShapeTest(unittest.TestCase):
    """chainlink #107's rung: a cross-concept call into a callee that cannot
    carry a boundary is reported by R1 rather than blocking on it. What
    makes that safe is a CLOSED grammar over the callee's own body -- the
    cases it proves, and (the more interesting half) the constructs that
    stop it."""

    def test_a_field_comparison_is_a_value_domain_inquiry(self):
        self.assertEqual(shape_of("self.0 != 0"), VALUE_DOMAIN_INQUIRY)

    def test_a_constant_on_the_other_side_is_a_value_domain_inquiry(self):
        # The pilot's own Year::ok, whose sentinel is a path to a constant.
        self.assertEqual(shape_of("self.0 != i16::MIN"), VALUE_DOMAIN_INQUIRY)

    def test_a_range_check_is_a_value_domain_inquiry(self):
        # Month::ok.
        self.assertEqual(shape_of("1 <= self.0 && self.0 <= 12"), VALUE_DOMAIN_INQUIRY)

    def test_the_three_way_leap_rule_is_a_value_domain_inquiry(self):
        # Year::is_leap.
        self.assertEqual(
            shape_of("(self.0 % 4 == 0 && self.0 % 100 != 0) || self.0 % 400 == 0"),
            VALUE_DOMAIN_INQUIRY,
        )

    def test_a_parameter_is_a_value_the_caller_already_held(self):
        self.assertEqual(shape_of("n > 3", signature="&self, n: u8"), VALUE_DOMAIN_INQUIRY)

    def test_a_cast_is_still_the_callees_own_data(self):
        self.assertEqual(shape_of("n as u32 > 3", signature="&self, n: u8"), VALUE_DOMAIN_INQUIRY)

    def test_a_let_binding_computed_from_own_fields_is_still_one(self):
        self.assertEqual(shape_of("let n = self.0; n <= 31"), VALUE_DOMAIN_INQUIRY)

    def test_a_comparison_is_not_mistaken_for_an_assignment(self):
        # `>=`, `<=`, `==` and `!=` are two tokens each, so the assignment
        # rule has to look at both neighbours or every comparison would be
        # disqualifying.
        self.assertEqual(shape_of("self.0 >= 4 && self.1 == 0 && self.2 != 1"), VALUE_DOMAIN_INQUIRY)

    def test_a_callee_that_calls_something_is_general(self):
        self.assertEqual(shape_of("self.check()"), GENERAL_CALL)

    def test_a_macro_is_general_because_its_expansion_is_not_in_the_token_stream(self):
        self.assertEqual(shape_of("matches!(self.0, 1..=12)"), GENERAL_CALL)

    def test_an_associated_constant_is_not_a_macro(self):
        self.assertEqual(shape_of("Self::MIN_DAYS > self.0"), VALUE_DOMAIN_INQUIRY)

    def test_a_callee_that_mutates_is_general(self):
        self.assertEqual(shape_of("let n = 0; n += 1; n > 0"), GENERAL_CALL)

    def test_a_field_assignment_is_general(self):
        self.assertEqual(shape_of("self.0 = 1; true"), GENERAL_CALL)

    def test_indexing_is_general_because_it_can_panic(self):
        self.assertEqual(shape_of("self.days[0] > 0"), GENERAL_CALL)

    def test_a_loop_is_general(self):
        self.assertEqual(shape_of("while self.0 < 3 { } false"), GENERAL_CALL)

    def test_an_unknown_free_identifier_is_general_because_nothing_resolves_it(self):
        self.assertEqual(shape_of("threshold < self.0"), GENERAL_CALL)

    def test_a_non_bool_return_is_general(self):
        self.assertEqual(shape_of("self.0", returns="u8"), GENERAL_CALL)

    def test_an_unsafe_callee_is_general(self):
        self.assertEqual(shape_of("true", before_fn="unsafe "), GENERAL_CALL)

    def test_an_async_callee_is_general(self):
        self.assertEqual(shape_of("true", before_fn="async "), GENERAL_CALL)

    def test_a_closure_is_general(self):
        self.assertEqual(shape_of("(|n: u8| n > 0)(self.0)"), GENERAL_CALL)

    def test_the_checked_in_fixture_classifies_its_own_callees(self):
        index = callee_shape_index(FIXTURE.parent.parent)
        # pop_ready is `self.items.pop().unwrap_or(now)` -- a call, so general.
        self.assertEqual(index[("TaskQueue", "pop_ready")].shape, GENERAL_CALL)
        # validate is `now > 0` -- a predicate over what the caller passed.
        self.assertEqual(index[("Scheduler", "validate")].shape, VALUE_DOMAIN_INQUIRY)

    def test_the_pin_is_the_callee_body_hash(self):
        index = callee_shape_index(FIXTURE.parent.parent)
        tokens = tokenize(strip_noncode(FIXTURE.read_text()))
        span = next(
            s for s in find_method_spans(tokens) if (s.concept, s.method) == ("Scheduler", "validate")
        )
        self.assertEqual(index[("Scheduler", "validate")].syntax_hash, method_syntax_hash(tokens, span))

    def test_an_unreadable_crate_raises_rather_than_reporting_no_callees(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ExtractionError):
                callee_shape_index(Path(tmp) / "no-such-crate")



class ShapeInReportTest(unittest.TestCase):
    def setUp(self):
        self.report = extract_fixture()
        self.sites = by_id(self.report)

    def test_every_resolved_call_records_its_callees_shape(self):
        for site in self.report["callsites"]:
            if site["call_class"] != "definite-direct-call":
                continue
            self.assertIn(site["callee_shape"], {VALUE_DOMAIN_INQUIRY, GENERAL_CALL})

    def test_only_a_value_domain_inquiry_carries_a_pin(self):
        by_callee = {
            (s.get("callee") or {}).get("concept"): s
            for s in self.report["callsites"] if s["call_class"] == "definite-direct-call"
        }
        pop_ready = [s for s in self.sites.values() if s.get("callee", {}).get("method") == "pop_ready"][0]
        self.assertEqual(pop_ready["callee_shape"], GENERAL_CALL)
        self.assertNotIn("callee_shape_evidence", pop_ready)
        validate = [s for s in self.sites.values() if s.get("callee", {}).get("method") == "validate"][0]
        self.assertEqual(validate["callee_shape"], VALUE_DOMAIN_INQUIRY)
        self.assertIn("callee_shape_evidence", validate)
        self.assertTrue(by_callee)

    def test_an_unresolved_call_carries_no_shape(self):
        for site in self.report["callsites"]:
            if site["call_class"] == "definite-direct-call":
                continue
            self.assertNotIn("callee_shape", site)


class SchemaConformanceTest(unittest.TestCase):
    def test_generated_report_passes_g1a_g1b(self):
        report = extract_fixture()
        path = Path("ci/results/c_static/crates_scheduler.json")
        findings = validate_data(path, report, load_validator())
        self.assertEqual([str(f) for f in findings], [])

    def test_generated_report_passes_the_re_derived_callee_shapes(self):
        # The full round trip: extract from a workspace that is still there,
        # then re-derive every recorded shape from those same sources.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            crate_root = workspace / "crates" / "scheduler"
            (crate_root / "src").mkdir(parents=True)
            (crate_root / "src" / "lib.rs").write_text(FIXTURE.read_text())
            report = extract_crate(
                crate_root, workspace, "crates_scheduler", "crates/scheduler", CONFIG_SCOPE, None
            )
            findings = validate_data(
                Path("ci/results/c_static/crates_scheduler.json"),
                report,
                load_validator(),
                shape_index_resolver(workspace),
            )
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
