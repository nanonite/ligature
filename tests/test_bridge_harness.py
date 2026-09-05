"""The bridge_logic compiler (chainlink #47).

plan.md §15's open item is answered by this module's behaviour, not by a
document: "compiles to a harness the owning verifier checks" is a
sufficient definition within one verifier system *only if* the
compilation is total or rejecting. So most of these tests are about what
the compiler REFUSES -- an accepting-anything compiler would make the
answer vacuous.
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from bridge_harness import (  # noqa: E402
    EVIDENCE_KIND_BY_VERIFIER,
    VERIFIERS,
    Call,
    CompileError,
    Obligation,
    Path as ExprPath,
    build_scope,
    compile_bridge,
    harness_name,
    parse_expression,
)

SHIPPED_BRIDGE = ROOT / "tests" / "fixtures" / "bridges" / "valid" / "specs" / "_bridges" / "BR-SCHED-TQ-001.json"


def bridge(bindings=None, premises=None, conclusion="TaskQueue.C003", available=None, local_facts=None) -> dict:
    spec = {
        "schema_version": "1.0",
        "bridge_id": "BR-SCHED-TQ-001",
        "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
        "callee_requirement": conclusion,
        "available_contract_facts": [
            {"obligation_id": obligation, "role": role}
            for obligation, role in (available or [("Scheduler.I001", "invariant")])
        ],
        "target_expression": f"{conclusion}(args, callee_state)",
        "protocol_class": "pairwise",
        "bridge_logic": {
            "bindings": bindings or {"caller_self": "Scheduler", "args": {"now": "Time"}},
            "premises": premises or ["caller_self.ready(args.now)", "Scheduler.I001(caller_self)"],
            "conclusion": {"obligation_id": conclusion},
        },
        "review": {"reviewer": "alice", "reviewed_at": "2026-08-30"},
    }
    if local_facts:
        spec["required_local_facts"] = [
            {"fact_id": fact_id, "expression": expression} for fact_id, expression in local_facts
        ]
    return spec


class GrammarTest(unittest.TestCase):
    def test_obligation_application(self):
        node = parse_expression("Scheduler.I001(caller_self)")
        self.assertIsInstance(node, Obligation)
        self.assertEqual(node.obligation_id, "Scheduler.I001")
        self.assertEqual(node.args, (ExprPath(("caller_self",)),))

    def test_member_call_on_a_path(self):
        node = parse_expression("caller_self.ready(args.now)")
        self.assertIsInstance(node, Call)
        self.assertEqual(node.target, ExprPath(("caller_self", "ready")))
        self.assertEqual(node.args, (ExprPath(("args", "now")),))

    def test_bare_path(self):
        self.assertEqual(parse_expression("queue_ready_at_call"), ExprPath(("queue_ready_at_call",)))

    def test_nested_application(self):
        node = parse_expression("Scheduler.I001(caller_self.state(), args.now)")
        self.assertEqual(len(node.args), 2)

    def test_no_arguments_is_fine(self):
        self.assertEqual(parse_expression("callee_self.invariant()").args, ())


class RejectionTest(unittest.TestCase):
    """Every one of these would otherwise compile to "whatever the emitter
    happened to produce", which is what makes "the harness is the
    semantics" a vacuous answer."""

    def refuse(self, text: str, fragment: str = ""):
        with self.assertRaises(CompileError) as caught:
            parse_expression(text)
        if fragment:
            self.assertIn(fragment, str(caught.exception))

    def test_operators_are_refused(self):
        self.refuse("queue_len <= 8", "predicate application only")

    def test_conjunction_inside_one_premise_is_refused(self):
        # premises is a list, and the list IS the conjunction
        self.refuse("a() && b()")

    def test_negation_is_refused(self):
        self.refuse("!ready(now)")

    def test_literals_are_refused(self):
        self.refuse("ready(8)")

    def test_quantifiers_are_refused_because_they_are_not_in_the_fragment(self):
        self.refuse("forall|x: int| ready(x)")

    def test_unbalanced_parentheses_are_refused(self):
        self.refuse("caller_self.ready(args.now")

    def test_trailing_junk_is_refused(self):
        self.refuse("caller_self.ready() extra", "one expression per premise")

    def test_empty_expression_is_refused(self):
        self.refuse("")


class ScopeTest(unittest.TestCase):
    def test_unbound_root_is_refused(self):
        with self.assertRaises(CompileError) as caught:
            compile_bridge(bridge(bindings={"caller_self": "Scheduler"}), "creusot")
        self.assertIn("cannot pass a value it was never given", str(caught.exception))

    def test_a_premise_may_not_assume_an_undeclared_contract_fact(self):
        # plan.md §8.2's central error class, mechanically refused.
        with self.assertRaises(CompileError) as caught:
            compile_bridge(bridge(premises=["Scheduler.C099(caller_self)"]), "creusot")
        self.assertIn("available_contract_facts", str(caught.exception))

    def test_nested_binding_may_be_written_bare(self):
        # plan.md §8.3's own example writes `now`; the schema's writes `args.now`.
        harness = compile_bridge(bridge(premises=["caller_self.ready(now)"]), "creusot")
        self.assertIn("caller_self.ready(args_now)", harness.source)

    def test_an_ambiguous_bare_name_is_refused_not_guessed(self):
        spec = bridge(
            bindings={"args": {"now": "Time"}, "other": {"now": "Time"}, "caller_self": "Scheduler"},
            premises=["caller_self.ready(now)"],
        )
        with self.assertRaises(CompileError) as caught:
            compile_bridge(spec, "creusot")
        self.assertIn("more than one binding group", str(caught.exception))

    def test_a_declared_local_fact_may_be_used_as_a_premise(self):
        spec = bridge(
            premises=["queue_ready_at_call"],
            local_facts=[("queue_ready_at_call", "queue.has_ready_at_or_before(now)")],
        )
        harness = compile_bridge(spec, "creusot")
        self.assertIn("queue_ready_at_call", harness.source)

    def test_scope_flattens_groups_to_one_parameter_each(self):
        scope = build_scope({"caller_self": "Scheduler", "args": {"now": "Time", "budget": "u32"}})
        self.assertEqual(scope.names, ["caller_self", "args_now", "args_budget"])

    def test_a_non_snake_case_binding_is_refused(self):
        with self.assertRaises(CompileError):
            build_scope({"CallerSelf": "Scheduler"})


class CompilationTest(unittest.TestCase):
    def test_every_verifier_produces_a_harness(self):
        for verifier in VERIFIERS:
            with self.subTest(verifier=verifier):
                harness = compile_bridge(bridge(), verifier)
                self.assertEqual(harness.verifier, verifier)
                self.assertEqual(harness.language, "rust")
                self.assertEqual(harness.evidence_kind, EVIDENCE_KIND_BY_VERIFIER[verifier])
                self.assertIn("GENERATED by scripts/bridge_harness.py", harness.source)

    def test_creusot_uses_requires_ensures_over_an_empty_body(self):
        source = compile_bridge(bridge(), "creusot").source
        self.assertIn("#[creusot_contracts::requires(caller_self.ready(args_now))]", source)
        self.assertIn("#[creusot_contracts::ensures(TaskQueue::C003(caller_self, args_now))]", source)
        self.assertTrue(source.rstrip().endswith("{}"))

    def test_kani_assumes_the_premises_and_asserts_the_conclusion(self):
        source = compile_bridge(bridge(), "kani").source
        self.assertIn("#[kani::proof]", source)
        self.assertIn("kani::assume(caller_self.ready(args_now));", source)
        self.assertIn("assert!(TaskQueue::C003(caller_self, args_now)", source)
        self.assertIn("Bounded", source)

    def test_verus_uses_a_proof_fn_with_requires_and_ensures(self):
        source = compile_bridge(bridge(), "verus").source
        self.assertIn("proof fn bridge_br_sched_tq_001", source)
        self.assertIn("requires", source)
        self.assertIn("ensures", source)

    def test_every_premise_reaches_the_harness(self):
        for verifier in VERIFIERS:
            source = compile_bridge(bridge(), verifier).source
            self.assertIn("caller_self.ready(args_now)", source)
            self.assertIn("Scheduler::I001(caller_self)", source)

    def test_the_conclusion_is_applied_to_every_parameter(self):
        harness = compile_bridge(
            bridge(bindings={"caller_self": "Scheduler", "args": {"now": "Time"}}), "creusot"
        )
        self.assertIn("TaskQueue::C003(caller_self, args_now)", harness.source)

    def test_an_unknown_verifier_is_refused(self):
        with self.assertRaises(CompileError):
            compile_bridge(bridge(), "smt-vibes")

    def test_harness_name_is_derived_from_the_bridge_id(self):
        self.assertEqual(harness_name("BR-SCHED-TQ-001"), "bridge_br_sched_tq_001")


class DeterminismTest(unittest.TestCase):
    """The whole machine relation rests on this: a hash is a property of
    the bridge only if the same bridge always compiles to the same bytes."""

    def test_recompilation_is_byte_identical(self):
        first = compile_bridge(bridge(), "creusot")
        second = compile_bridge(bridge(), "creusot")
        self.assertEqual(first.source, second.source)
        self.assertEqual(first.sha256, second.sha256)

    def test_different_verifiers_hash_differently(self):
        hashes = {compile_bridge(bridge(), verifier).sha256 for verifier in VERIFIERS}
        self.assertEqual(len(hashes), len(VERIFIERS))

    def test_a_changed_premise_changes_the_hash(self):
        baseline = compile_bridge(bridge(), "creusot").sha256
        changed = compile_bridge(bridge(premises=["Scheduler.I001(caller_self)"]), "creusot").sha256
        self.assertNotEqual(baseline, changed)

    def test_the_hash_is_of_the_source_it_reports(self):
        import hashlib

        harness = compile_bridge(bridge(), "kani")
        self.assertEqual(harness.sha256, "sha256:" + hashlib.sha256(harness.source.encode()).hexdigest())


class ShippedFixtureTest(unittest.TestCase):
    def test_the_repos_own_canonical_bridge_compiles(self):
        # #22's fixture is the canonical worked example; if it did not
        # compile, "compiles to a harness" would be answering a question
        # the repo's own example cannot pass.
        spec = json.loads(SHIPPED_BRIDGE.read_text())
        for verifier in VERIFIERS:
            with self.subTest(verifier=verifier):
                harness = compile_bridge(spec, verifier)
                self.assertIn("TaskQueue::C003(caller_self, args_now)", harness.source)


if __name__ == "__main__":
    unittest.main()
