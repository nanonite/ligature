"""G14 closure gate (chainlink #25).

Each scenario is built as a real workspace on disk -- manifests in
ci/manifest/, achieved assurance in the report.emit path each manifest
declares, bridges under a crate's _bridges/, a C_static report, and a
closure profile in specs/_closure/ -- because every one of those
locations is part of what the gate is asserting, and a test that handed
the gate pre-loaded dicts would not be testing the gate anyone runs.
"""
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

from gate_g14 import (  # noqa: E402
    EXIT_BLOCKED,
    EXIT_INPUT_ERROR,
    EXIT_OK,
    PER_CLUSTER_NOTE,
    build_provider_index,
    closure_context,
    compute_closure,
    cycles,
    gate_workspace,
    load_manifests,
    main,
    report_outcomes,
    strongly_connected_components,
    work_package_manifest_dir_for,
)

sys.path.insert(0, str(ROOT / "tests"))
from test_validate_closure import valid_degradation, valid_profile  # noqa: E402

REVIEW = {"reviewer": "alice", "reviewed_at": "2026-09-04"}


def required_assurance(
    claims=("postcondition-holds",),
    kinds=("creusot-deductive-check",),
    scope=None,
    allowed=(),
) -> dict:
    profile = {
        "required_claims": list(claims),
        "accepted_evidence_kinds": list(kinds),
        "trust_policy": {"assumptions_allowed": list(allowed)},
    }
    if scope:
        profile["minimum_scope"] = scope
    return profile


def achieved(kind="creusot-deductive-check", claim="postcondition-holds", assumptions=(), scope=None) -> dict:
    verifier = {
        "creusot-deductive-check": "creusot",
        "kani-bounded-model-check": "kani",
        "verus-deductive-check": "verus",
    }[kind]
    return {
        "schema_version": "1.0",
        "claim": {"kind": claim, "result": "pass"},
        "evidence": {
            "kind": kind,
            "verifier": verifier,
            "harness": "h",
            "scope": scope or {"input_domain": "all"},
        },
        "trust": {"assumptions": list(assumptions)},
        "support": {"status": "supported"},
        "config": {
            "toolchain": "nightly-2026-05-01",
            "target": "x86_64-unknown-linux-gnu",
            "features": ["default"],
        },
    }


def manifest(work_package, provided=(), required=(), bridges=()) -> dict:
    return {
        "schema": "work-package-manifest/1.0",
        "work_package": work_package,
        "issue": "chainlink:900",
        "depends_on": [],
        "coupling_notes": [],
        "scheduling_rule": "reverse-dependency-order",
        "functions": ["scheduler::x"],
        "obligations": [obligation for obligation, _ in provided],
        "provenance": {
            "base_commit": "a1b2c3d",
            "promotion_id": "PROM-SCHED-001",
            "artifact_set_hash": "sha256:" + "2" * 64,
            "toolchain": "nightly-2026-05-01",
            "target": "x86_64-unknown-linux-gnu",
            "features": ["default"],
        },
        "gate_integrity": [{"runner": "scripts/gate_g14.py", "hash": "sha256:" + "3" * 64}],
        "write_policy": {
            "allowed_write_set": ["crates/scheduler/src/"],
            "protected_write_set": ["crates/*/specs/**"],
            "on_conflict": "raise_change_request",
        },
        "definition_of_done": {
            "provided_guarantees": [
                {
                    "obligation_id": obligation,
                    "required_assurance": assurance,
                    "harness": "h_" + obligation.replace(".", "_"),
                }
                for obligation, assurance in provided
            ],
            "required_preconditions_to_establish": [
                {
                    "bridge_id": bridge_id,
                    "required_assurance": required_assurance(("callee-precondition-established",)),
                    "callsite_requirement": "all-discovered-resolved",
                }
                for bridge_id in bridges
            ],
            "required_guarantees": [
                {"obligation_id": obligation, "assume_during_check": True, "required_assurance": assurance}
                for obligation, assurance in required
            ],
            "trusted_assumptions": [],
        },
        "gates": [{"id": "prove", "runner": "cargo", "args": ["creusot"]}],
        "failure_policy": {
            "verifier_timeout": "raise_change_request(kind=budget_or_assumption)",
            "obligation_unprovable": "raise_change_request(kind=contract_revision)",
            "missing_callee_guarantee": "raise_change_request(kind=missing_contract)",
            "unintended_call_edge": "raise_change_request(kind=interaction_revision)",
            "unresolved_callsite": "raise_change_request(kind=callsite_unresolved)",
            "forbidden": ["weaken_or_delete_contract"],
        },
        "report": {"emit": f"ci/results/{work_package}.json", "per_obligation_assurance_record": True},
    }


def bridge_spec(bridge_id="BR-SCHED-TQ-001", protocol_class="pairwise") -> dict:
    return {
        "schema_version": "1.0",
        "bridge_id": bridge_id,
        "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
        "callee_requirement": "TaskQueue.C001",
        "available_contract_facts": [{"obligation_id": "Scheduler.C010", "role": "caller-precondition"}],
        "target_expression": "TaskQueue.C001(args, callee_state)",
        "protocol_class": protocol_class,
        "bridge_logic": {
            # `args` is declared because the premise below uses it: #47's
            # compiler is total-or-rejecting, so an under-declared binding
            # set is a compile error, not a shrug.
            "bindings": {"caller_self": "Scheduler", "args": {"now": "Time"}},
            "premises": ["caller_self.ready(args.now)"],
            "conclusion": {"obligation_id": "TaskQueue.C001"},
        },
        "review": dict(REVIEW),
    }


def callsite_report(unresolved=0, risk_tier="medium") -> dict:
    callsites = []
    for index in range(unresolved):
        callsites.append({
            "callsite_id": f"CS-SCHEDULER-DISPATCH-{index + 1:03d}",
            "call_class": "unresolved-indirect-call",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "unresolved_expression": "hook(...)",
            "source": {
                "path": "crates/scheduler/src/lib.rs",
                "symbol": "Scheduler::dispatch",
                "line": 10 + index,
                "syntax_hash": "sha256:" + "a" * 64,
            },
            "risk_tier": risk_tier,
            "risk_tier_source": "extractor-default" if risk_tier == "medium" else "human",
            **(
                {}
                if risk_tier == "medium"
                else {"risk_review": dict(REVIEW)}
            ),
        })
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
            "unattributed_call_sites": 0,
            "out_of_scope_call_sites": 0,
            "macro_invocations": 0,
        },
        "callsites": callsites,
        "callsite_coverage": {"discovered": len(callsites), "resolved": 0, "unresolved": len(callsites)},
    }


class Workspace:
    """A three-work-package chain: WP-A -> WP-B -> WP-C, so every test
    below is exercising a genuinely TRANSITIVE closure (the cluster names
    WP-A only; WP-C is two hops away and still fully checked)."""

    def __init__(self, root: Path):
        self.root = root
        self.write("ci/manifest/WP-A.json", manifest(
            "WP-A",
            provided=[("Scheduler.C001", required_assurance())],
            required=[("TaskQueue.C003", required_assurance())],
            bridges=["BR-SCHED-TQ-001"],
        ))
        self.write("ci/manifest/WP-B.json", manifest(
            "WP-B",
            provided=[("TaskQueue.C003", required_assurance())],
            required=[("Clock.C007", required_assurance())],
        ))
        self.write("ci/manifest/WP-C.json", manifest(
            "WP-C", provided=[("Clock.C007", required_assurance())]
        ))
        self.write("ci/results/WP-A.json", {
            "schema_version": "1.0",
            "work_package": "WP-A",
            "obligation_records": [{"obligation_id": "Scheduler.C001", "record": achieved()}],
            "bridge_records": [
                {
                    "bridge_id": "BR-SCHED-TQ-001",
                    "record": achieved(claim="callee-precondition-established"),
                }
            ],
        })
        self.write("ci/results/WP-B.json", {
            "schema_version": "1.0",
            "work_package": "WP-B",
            "obligation_records": [{"obligation_id": "TaskQueue.C003", "record": achieved()}],
            "bridge_records": [],
        })
        self.write("ci/results/WP-C.json", {
            "schema_version": "1.0",
            "work_package": "WP-C",
            "obligation_records": [{"obligation_id": "Clock.C007", "record": achieved()}],
            "bridge_records": [],
        })
        self.write("crates/scheduler/specs/_bridges/BR-SCHED-TQ-001.json", bridge_spec())
        self.write("ci/results/c_static/crates_scheduler.json", callsite_report())
        profile = valid_profile()
        profile["work_packages"] = ["WP-A"]
        self.write("specs/_closure/scheduler-core.json", profile)

    def write(self, relative: str, data: dict) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
        return path

    def read(self, relative: str) -> dict:
        return json.loads((self.root / relative).read_text())

    def patch(self, relative: str, mutate) -> None:
        data = self.read(relative)
        mutate(data)
        self.write(relative, data)

    def outcomes(self):
        return gate_workspace(self.root)

    def cluster(self, name="scheduler-core"):
        outcomes, workspace_findings = self.outcomes()
        found = next(o for o in outcomes if o.cluster == name)
        return found, workspace_findings

    def run(self) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            outcomes, workspace_findings = self.outcomes()
            code = report_outcomes(outcomes, workspace_findings)
        return code, buffer.getvalue()


class GateTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def errors(self, outcome) -> list[str]:
        return [str(f) for f in outcome.findings if f.severity == "error"]

    def degraded(self, outcome) -> list[str]:
        return [str(f) for f in outcome.findings if f.severity == "degraded"]


class HappyPathTest(GateTestCase):
    def test_a_complete_chain_closes(self):
        outcome, workspace_findings = self.ws.cluster()
        self.assertEqual(self.errors(outcome), [])
        self.assertEqual([str(f) for f in workspace_findings], [])
        self.assertEqual(outcome.status, "closes")

    def test_the_closure_is_transitive_not_direct(self):
        # The profile names WP-A only; WP-C is reached through WP-B.
        outcome, _ = self.ws.cluster()
        self.assertEqual(outcome.work_packages, 3)
        self.assertEqual(outcome.obligations, 3)

    def test_the_closure_kind_is_reported_with_the_outcome(self):
        outcome, _ = self.ws.cluster()
        self.assertIn("closure_kind=deductive", outcome.summary())


class TransitiveTrustPolicyTest(GateTestCase):
    """plan.md §8.5's opening case, which is the whole reason G14 is not
    a loop over direct edges: "Direct A->B checking passes while a
    C-level assumption sits below policy"."""

    def allow_deep_assumption_on_every_direct_edge(self):
        self.ws.patch("ci/manifest/WP-B.json", lambda d: d["definition_of_done"]["required_guarantees"][0][
            "required_assurance"]["trust_policy"].update({"assumptions_allowed": ["clock_monotonic"]}))
        self.ws.patch("ci/manifest/WP-C.json", lambda d: d["definition_of_done"]["provided_guarantees"][0][
            "required_assurance"]["trust_policy"].update({"assumptions_allowed": ["clock_monotonic"]}))
        self.ws.patch("ci/results/WP-C.json", lambda d: d["obligation_records"][0]["record"]["trust"].update(
            {"assumptions": ["clock_monotonic"]}))

    def test_a_depth_two_assumption_fails_even_when_every_edge_passes(self):
        self.allow_deep_assumption_on_every_direct_edge()
        outcome, _ = self.ws.cluster()
        errors = self.errors(outcome)
        self.assertTrue(any("trust policy applies across the whole closure" in e for e in errors))
        self.assertTrue(any("closure depth" not in e and "clock_monotonic" in e for e in errors))
        self.assertEqual(outcome.status, "blocked")

    def test_the_same_assumption_inside_policy_closes(self):
        self.allow_deep_assumption_on_every_direct_edge()
        self.ws.patch("ci/manifest/WP-A.json", lambda d: [
            entry["required_assurance"]["trust_policy"].update({"assumptions_allowed": ["clock_monotonic"]})
            for entry in d["definition_of_done"]["provided_guarantees"] + d["definition_of_done"]["required_guarantees"]
        ])
        outcome, _ = self.ws.cluster()
        self.assertEqual(self.errors(outcome), [])
        self.assertEqual(outcome.status, "closes")


class DependencyTest(GateTestCase):
    def test_an_unsupported_dependency_blocks(self):
        (self.ws.root / "ci" / "manifest" / "WP-C.json").unlink()
        outcome, _ = self.ws.cluster()
        self.assertTrue(any("unsupported dependency" in e for e in self.errors(outcome)))

    def test_a_missing_achieved_record_blocks(self):
        self.ws.patch("ci/results/WP-C.json", lambda d: d["obligation_records"].clear())
        outcome, _ = self.ws.cluster()
        self.assertTrue(any("recorded no achieved assurance" in e for e in self.errors(outcome)))

    def test_a_missing_assurance_report_blocks(self):
        (self.ws.root / "ci" / "results" / "WP-C.json").unlink()
        outcome, _ = self.ws.cluster()
        self.assertTrue(any("no assurance report" in e for e in self.errors(outcome)))

    def test_a_report_from_another_work_package_is_refused(self):
        self.ws.patch("ci/results/WP-C.json", lambda d: d.update({"work_package": "WP-Z"}))
        outcome, _ = self.ws.cluster()
        self.assertTrue(any("cannot satisfy this one's obligations" in e for e in self.errors(outcome)))

    def test_an_unsatisfied_requirement_deep_in_the_closure_blocks(self):
        self.ws.patch("ci/results/WP-C.json", lambda d: d["obligation_records"][0]["record"]["claim"].update(
            {"result": "fail"}))
        outcome, _ = self.ws.cluster()
        self.assertTrue(any("closure depth 1" in e for e in self.errors(outcome)))

    def test_an_unsupported_support_status_blocks(self):
        self.ws.patch("ci/results/WP-C.json", lambda d: d["obligation_records"][0]["record"]["support"].update(
            {"status": "unsupported"}))
        outcome, _ = self.ws.cluster()
        self.assertTrue(any("support.status" in e for e in self.errors(outcome)))

    def test_two_providers_for_one_obligation_is_a_workspace_error(self):
        self.ws.write("ci/manifest/WP-D.json", manifest(
            "WP-D", provided=[("Clock.C007", required_assurance())]
        ))
        _, workspace_findings = self.ws.cluster()
        self.assertTrue(any("ambiguous provider" in str(f) for f in workspace_findings))

    def test_an_unreadable_manifest_is_reported_not_skipped(self):
        (self.ws.root / "ci" / "manifest" / "WP-C.json").write_text("{ not json")
        _, workspace_findings = self.ws.cluster()
        self.assertTrue(any("not readable JSON" in str(f) for f in workspace_findings))

    def test_a_manifest_whose_id_disagrees_with_its_filename_is_refused(self):
        self.ws.patch("ci/manifest/WP-C.json", lambda d: d.update({"work_package": "WP-OTHER"}))
        _, workspace_findings = self.ws.cluster()
        self.assertTrue(any("its filename says" in str(f) for f in workspace_findings))


class BridgeTest(GateTestCase):
    def test_a_failing_bridge_blocks(self):
        self.ws.patch("ci/results/WP-A.json", lambda d: d["bridge_records"][0]["record"]["claim"].update(
            {"result": "fail"}))
        outcome, _ = self.ws.cluster()
        self.assertTrue(any("bridge requirement is not met" in e for e in self.errors(outcome)))

    def test_a_missing_bridge_record_blocks(self):
        self.ws.patch("ci/results/WP-A.json", lambda d: d["bridge_records"].clear())
        outcome, _ = self.ws.cluster()
        self.assertTrue(any("recorded no achieved assurance for it" in e for e in self.errors(outcome)))

    def test_a_dangling_bridge_id_blocks(self):
        (self.ws.root / "crates/scheduler/specs/_bridges/BR-SCHED-TQ-001.json").unlink()
        outcome, _ = self.ws.cluster()
        self.assertTrue(any("no valid bridge specification" in e for e in self.errors(outcome)))

    def test_a_non_pairwise_bridge_fails_the_protocol_condition(self):
        self.ws.write(
            "crates/scheduler/specs/_bridges/BR-SCHED-TQ-001.json",
            bridge_spec(protocol_class="non-pairwise"),
        )
        outcome, _ = self.ws.cluster()
        self.assertTrue(any("protocol_class_all_pairwise" in e for e in self.errors(outcome)))

    def test_callsite_requirement_is_stricter_than_the_medium_condition(self):
        # A low-tier unresolved call site is an accepted limitation for
        # G16 (its at-or-above-medium count stays 0) and still breaks a
        # manifest's demand that every DISCOVERED call site be resolved.
        self.ws.write("ci/results/c_static/crates_scheduler.json", callsite_report(unresolved=1, risk_tier="low"))
        outcome, _ = self.ws.cluster()
        errors = self.errors(outcome)
        self.assertTrue(any("requires all discovered call sites resolved" in e for e in errors))
        self.assertFalse(any("at or above medium risk" in e for e in errors))

    def test_no_c_static_observation_at_all_blocks(self):
        (self.ws.root / "ci/results/c_static/crates_scheduler.json").unlink()
        outcome, _ = self.ws.cluster()
        errors = self.errors(outcome)
        self.assertTrue(any("an unobserved call graph is not a resolved one" in e for e in errors))
        self.assertTrue(any("could not be read from the R1/G16 gate" in e for e in errors))


class UnresolvedCallsiteConditionTest(GateTestCase):
    def test_the_medium_plus_count_comes_from_the_r1_g16_gate(self):
        self.ws.write("ci/results/c_static/crates_scheduler.json", callsite_report(unresolved=2))
        self.ws.patch(
            "specs/_closure/scheduler-core.json",
            lambda d: d["conditions"].update({"unresolved_indirect_calls_at_or_above_medium": 2}),
        )
        outcome, _ = self.ws.cluster()
        errors = self.errors(outcome)
        self.assertTrue(any("2 unresolved call site(s) at or above medium risk" in e for e in errors))
        # declared 2 == computed 2, so this is a failing condition, not a
        # mis-declared one
        self.assertFalse(any("recomputed, never stored-and-trusted" in e for e in errors))


class CycleTest(GateTestCase):
    """CG6: mutual satisfaction is not soundness."""

    def make_cycle(self):
        self.ws.patch("ci/manifest/WP-C.json", lambda d: d["definition_of_done"].update({
            "required_guarantees": [{
                "obligation_id": "Scheduler.C001",
                "assume_during_check": True,
                "required_assurance": required_assurance(),
            }]
        }))

    def test_an_undischarged_cycle_blocks(self):
        self.make_cycle()
        outcome, _ = self.ws.cluster()
        self.assertTrue(any("mutual satisfaction is not soundness" in e for e in self.errors(outcome)))
        self.assertEqual(len(outcome.cycles), 1)

    def test_a_discharged_cycle_closes(self):
        self.make_cycle()
        self.ws.patch("specs/_closure/scheduler-core.json", lambda d: (
            d["conditions"].update({"scc_wellfoundedness_discharged": True}),
            d.update({"scc_discharges": [{
                "members": ["Clock.C007", "Scheduler.C001", "TaskQueue.C003"],
                "kind": "decreasing-measure",
                "argument": (
                    "Each traversal consumes one queued task, so the multiset of pending tasks "
                    "strictly decreases; no instantaneous circular dependence remains."
                ),
            }]}),
        ))
        outcome, _ = self.ws.cluster()
        self.assertEqual(self.errors(outcome), [])
        self.assertEqual(outcome.status, "closes")

    def test_a_discharge_must_name_the_whole_scc(self):
        self.make_cycle()
        self.ws.patch("specs/_closure/scheduler-core.json", lambda d: (
            d["conditions"].update({"scc_wellfoundedness_discharged": True}),
            d.update({"scc_discharges": [{
                "members": ["Scheduler.C001", "TaskQueue.C003"],
                "kind": "step-index",
                "argument": "A step index decreases on every traversal of this pair of obligations.",
            }]}),
        ))
        outcome, _ = self.ws.cluster()
        self.assertTrue(any("naming exactly these members" in e for e in self.errors(outcome)))

    def test_a_stale_discharge_is_rejected(self):
        self.ws.patch("specs/_closure/scheduler-core.json", lambda d: d.update({"scc_discharges": [{
            "members": ["Ghost.C001", "Phantom.C002"],
            "kind": "temporal-stratification",
            "argument": "These two obligations are stratified across ticks and never interleave.",
        }]}))
        outcome, _ = self.ws.cluster()
        self.assertTrue(any("does not exist in the computed closure" in e for e in self.errors(outcome)))

    def test_acyclic_closure_must_declare_not_applicable(self):
        self.ws.patch(
            "specs/_closure/scheduler-core.json",
            lambda d: d["conditions"].update({"scc_wellfoundedness_discharged": True}),
        )
        outcome, _ = self.ws.cluster()
        self.assertTrue(any("'not-applicable'" in e for e in self.errors(outcome)))


class ClosureKindTest(GateTestCase):
    def test_a_kani_result_deep_in_the_closure_refutes_deductive(self):
        self.ws.patch("ci/manifest/WP-C.json", lambda d: d["definition_of_done"]["provided_guarantees"][0][
            "required_assurance"].update({"accepted_evidence_kinds": ["kani-bounded-model-check"]}))
        self.ws.patch("ci/manifest/WP-B.json", lambda d: d["definition_of_done"]["required_guarantees"][0][
            "required_assurance"].update({"accepted_evidence_kinds": ["kani-bounded-model-check"]}))
        self.ws.patch("ci/results/WP-C.json", lambda d: d["obligation_records"][0].update(
            {"record": achieved(kind="kani-bounded-model-check")}))
        outcome, _ = self.ws.cluster()
        errors = self.errors(outcome)
        self.assertTrue(any("makes the cluster's guarantee bounded" in e for e in errors))
        self.assertTrue(any("cross-verifier composition has no soundness theorem" in e for e in errors))


class DeclaredConditionTest(GateTestCase):
    def test_a_mis_declared_condition_is_rejected(self):
        self.ws.patch(
            "specs/_closure/scheduler-core.json",
            lambda d: d["conditions"].update({"protocol_class_all_pairwise": False}),
        )
        outcome, _ = self.ws.cluster()
        self.assertTrue(any("recomputed, never stored-and-trusted" in e for e in self.errors(outcome)))

    def test_the_cg3_condition_is_reported_as_declared_not_verified(self):
        outcome, _ = self.ws.cluster()
        infos = [str(f) for f in outcome.findings if f.severity == "info"]
        self.assertTrue(any("HUMAN DECLARATION this gate cannot verify" in i for i in infos))

    def test_a_declared_false_cg3_condition_is_a_failing_condition(self):
        self.ws.patch(
            "specs/_closure/scheduler-core.json",
            lambda d: d["conditions"].update({"generic_callees_type_universal_or_creusot_owned": False}),
        )
        outcome, _ = self.ws.cluster()
        self.assertTrue(any("per monomorphization" in e for e in self.errors(outcome)))


class DegradationTest(GateTestCase):
    def add_record(self, failed_conditions, ceiling="harness-tested"):
        record = valid_degradation()
        record["failed_conditions"] = failed_conditions
        record["ceiling"] = ceiling
        self.ws.write("specs/_closure/scheduler-core.degradation.json", record)

    def test_a_named_condition_is_degraded_not_blocked(self):
        self.ws.write(
            "crates/scheduler/specs/_bridges/BR-SCHED-TQ-001.json",
            bridge_spec(protocol_class="non-pairwise"),
        )
        self.ws.patch(
            "specs/_closure/scheduler-core.json",
            lambda d: d["conditions"].update({"protocol_class_all_pairwise": False}),
        )
        self.add_record(["protocol_class_all_pairwise"])
        outcome, _ = self.ws.cluster()
        self.assertEqual(self.errors(outcome), [])
        self.assertEqual(outcome.status, "degraded")
        self.assertTrue(any("accepted as degradation under ceiling" in d for d in self.degraded(outcome)))

    def test_a_degraded_cluster_never_reads_as_closed(self):
        self.ws.write(
            "crates/scheduler/specs/_bridges/BR-SCHED-TQ-001.json",
            bridge_spec(protocol_class="non-pairwise"),
        )
        self.ws.patch(
            "specs/_closure/scheduler-core.json",
            lambda d: d["conditions"].update({"protocol_class_all_pairwise": False}),
        )
        self.add_record(["protocol_class_all_pairwise"])
        outcome, _ = self.ws.cluster()
        self.assertIn("does NOT close", outcome.summary())
        self.assertNotIn("closes:", outcome.summary())

    def test_a_record_cannot_excuse_a_missing_proof(self):
        self.ws.patch("ci/results/WP-C.json", lambda d: d["obligation_records"][0]["record"]["claim"].update(
            {"result": "fail"}))
        self.add_record(["transitive_assumptions_within_policy"])
        outcome, _ = self.ws.cluster()
        self.assertTrue(self.errors(outcome))
        self.assertEqual(outcome.status, "blocked")

    def test_a_record_cannot_excuse_an_undischarged_cycle_it_does_not_name(self):
        self.ws.patch("ci/manifest/WP-C.json", lambda d: d["definition_of_done"].update({
            "required_guarantees": [{
                "obligation_id": "Scheduler.C001",
                "assume_during_check": True,
                "required_assurance": required_assurance(),
            }]
        }))
        self.add_record(["single_verifier_system"])
        outcome, _ = self.ws.cluster()
        self.assertTrue(any("mutual satisfaction is not soundness" in e for e in self.errors(outcome)))

    def test_a_record_can_accept_an_undischarged_cycle_when_it_names_it(self):
        # CG6 is a capability gap like any other: a human may accept the
        # ceiling, but only by naming the condition and the issue.
        self.ws.patch("ci/manifest/WP-C.json", lambda d: d["definition_of_done"].update({
            "required_guarantees": [{
                "obligation_id": "Scheduler.C001",
                "assume_during_check": True,
                "required_assurance": required_assurance(),
            }]
        }))
        self.ws.patch(
            "specs/_closure/scheduler-core.json",
            lambda d: d["conditions"].update({"scc_wellfoundedness_discharged": False}),
        )
        self.add_record(["scc_wellfoundedness_discharged"], ceiling="human-risk-acceptance")
        outcome, _ = self.ws.cluster()
        self.assertEqual(self.errors(outcome), [])
        self.assertEqual(outcome.status, "degraded")


class ReportingTest(GateTestCase):
    def test_no_clusters_fails_closed(self):
        (self.ws.root / "specs/_closure/scheduler-core.json").unlink()
        code, printed = self.ws.run()
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertIn("an empty closure is not a release", printed)

    def test_the_gate_never_asserts_a_global_guarantee(self):
        code, printed = self.ws.run()
        self.assertEqual(code, EXIT_OK)
        self.assertIn(PER_CLUSTER_NOTE, printed)
        for forbidden in ("pipeline closed", "everything closed", "all clusters closed", "release is closed"):
            self.assertNotIn(forbidden, printed)

    def test_a_blocked_cluster_exits_non_zero(self):
        (self.ws.root / "ci/results/WP-C.json").unlink()
        code, printed = self.ws.run()
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertIn("BLOCKED", printed)

    def test_a_degraded_cluster_exits_zero_and_says_so(self):
        self.ws.write(
            "crates/scheduler/specs/_bridges/BR-SCHED-TQ-001.json",
            bridge_spec(protocol_class="non-pairwise"),
        )
        self.ws.patch(
            "specs/_closure/scheduler-core.json",
            lambda d: d["conditions"].update({"protocol_class_all_pairwise": False}),
        )
        record = valid_degradation()
        record["failed_conditions"] = ["protocol_class_all_pairwise"]
        self.ws.write("specs/_closure/scheduler-core.degradation.json", record)
        code, printed = self.ws.run()
        self.assertEqual(code, EXIT_OK)
        self.assertIn("released under an accepted degradation record", printed)

    def test_cli_refuses_a_workspace_with_no_closure_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(main([tmp]), EXIT_INPUT_ERROR)

    def test_cli_refuses_a_missing_workspace(self):
        self.assertEqual(main([str(self.ws.root / "nope")]), EXIT_INPUT_ERROR)

    def test_cli_end_to_end(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main([str(self.ws.root)]), EXIT_OK)


class GraphAlgorithmTest(unittest.TestCase):
    """The closure machinery itself, away from any workspace."""

    def test_scc_finds_a_simple_cycle(self):
        components = strongly_connected_components({"a": {"b"}, "b": {"c"}, "c": {"a"}})
        self.assertIn(["a", "b", "c"], components)

    def test_scc_separates_independent_components(self):
        components = strongly_connected_components({"a": {"b"}, "b": {"a"}, "c": {"d"}, "d": set()})
        self.assertIn(["a", "b"], components)
        self.assertIn(["c"], components)
        self.assertIn(["d"], components)

    def test_scc_handles_a_deep_chain_without_recursion(self):
        # A release gate must not fall over on a long dependency chain.
        depth = 3000
        edges = {f"n{i}": {f"n{i + 1}"} for i in range(depth)}
        components = strongly_connected_components(edges)
        self.assertEqual(len(components), depth + 1)

    def test_a_self_dependency_counts_as_a_cycle(self):
        from gate_g14 import Closure

        closure = Closure(obligation_edges={"A.C001": {"A.C001"}})
        self.assertEqual(cycles(closure), [["A.C001"]])

    def test_closure_context_carries_the_cluster_facts_an_edge_cannot_know(self):
        context = closure_context(
            "scheduler-core", valid_profile(), "WP-A", "WP-B", "TaskQueue.C003", 2
        )
        self.assertEqual(context["cluster"], "scheduler-core")
        self.assertEqual(context["declared_closure_kind"], "deductive")
        self.assertEqual(context["declared_owning_verifier"], "creusot")
        self.assertEqual(context["requiring_work_package"], "WP-A")
        self.assertEqual(context["providing_work_package"], "WP-B")
        self.assertEqual(context["obligation_id"], "TaskQueue.C003")
        self.assertEqual(context["closure_depth"], 2)

    def test_satisfies_still_ignores_the_context(self):
        # #23's design decision, unchanged by #25 defining the shape: a
        # required_profile is authored governance, and ambient cluster
        # facts must not silently strengthen or weaken it.
        from satisfies import satisfies

        profile = required_assurance()
        record = achieved()
        without = satisfies(profile, record, None)
        with_context = satisfies(
            profile, record, closure_context("c", valid_profile(), "WP-A", "WP-B", "X.C001", 9)
        )
        self.assertEqual((without.holds, without.reasons), (with_context.holds, with_context.reasons))


class ManifestLoadingTest(GateTestCase):
    def test_manifest_directory_is_ci_manifest(self):
        self.assertEqual(
            work_package_manifest_dir_for(self.ws.root), (self.ws.root / "ci" / "manifest").resolve()
        )

    def test_all_three_manifests_load(self):
        manifests, findings = load_manifests(self.ws.root)
        self.assertEqual(sorted(manifests), ["WP-A", "WP-B", "WP-C"])
        self.assertEqual(findings, [])

    def test_provider_index_maps_every_provided_obligation(self):
        manifests, _ = load_manifests(self.ws.root)
        providers, findings = build_provider_index(manifests)
        self.assertEqual(
            providers, {"Scheduler.C001": "WP-A", "TaskQueue.C003": "WP-B", "Clock.C007": "WP-C"}
        )
        self.assertEqual(findings, [])

    def test_compute_closure_follows_dependencies_outside_the_cluster(self):
        manifests, _ = load_manifests(self.ws.root)
        providers, _ = build_provider_index(manifests)
        closure = compute_closure(["WP-A"], manifests, providers)
        self.assertEqual(sorted(closure.work_packages), ["WP-A", "WP-B", "WP-C"])
        self.assertEqual(closure.depth_by_obligation["Clock.C007"], 2)
        self.assertEqual(closure.unsupported, [])

    def test_a_workspace_with_no_manifests_reports_every_dependency_unsupported(self):
        for name in ("WP-A", "WP-B", "WP-C"):
            (self.ws.root / "ci" / "manifest" / f"{name}.json").unlink()
        outcome, _ = self.ws.cluster()
        self.assertTrue(any("no valid manifest for it was found" in e for e in self.errors(outcome)))


if __name__ == "__main__":
    unittest.main()
