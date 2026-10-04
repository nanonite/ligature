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
import os
import shutil
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
    check_closure_kind,
    closure_context,
    compute_closure,
    cycles,
    gate_workspace,
    load_manifests,
    looks_like_work_package_manifest,
    main,
    report_outcomes,
    review_log_path,
    strongly_connected_components,
    work_package_manifest_dir_for,
)
import pipeline  # noqa: E402
import review_checkpoint  # noqa: E402
import validate_closure  # noqa: E402
from generate_promotion_receipt import compute_artifact_manifest  # noqa: E402
from validate_closure import load_degradation_records  # noqa: E402
from generate_promotion_receipt import record_ruling  # noqa: E402
from generate_promotion_receipt import ruling_log_path  # noqa: E402
from record_assurance import concept_segment  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))
from test_validate_closure import valid_degradation, valid_profile  # noqa: E402

REVIEW = {"reviewer": "alice", "reviewed_at": "2026-09-04"}


def default_descriptor() -> dict:
    """A minimal, schema-valid descriptor naming the single crate every
    fixture in this module lives under. Built locally rather than
    imported from test_gate_g9.py's own descriptor_with(), which imports
    FROM this module -- importing it back would be circular."""
    descriptor = json.loads(
        (ROOT / "schemas" / "examples" / "project-descriptor.greenfield.example.json").read_text()
    )
    descriptor["crates"] = [
        {"crate_dir": "crates/scheduler", "contracts_crate": "contracts", "specs_search_root": "crates"}
    ]
    return descriptor


def boundary_contract() -> dict:
    """The boundary BR-SCHED-TQ-001 (see bridge_spec() below) declares as
    its own boundary_id, with callee_guarantees covering that bridge's
    callee_requirement -- without this, load_bridges' own G2 check
    (chainlink #25's fix) rejects the bridge as a dangling reference and
    every "happy path" fixture in this module would fail closed instead
    of closing."""
    return {
        "schema_version": "1.0",
        "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
        "caller": {"concept": "Scheduler", "method": "dispatch"},
        "callee": {"concept": "TaskQueue", "method": "pop_ready"},
        "callee_guarantees": ["TaskQueue.C001"],
        "review": dict(REVIEW),
    }


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


def certificate_path(obligation: str) -> str:
    """Where this fixture keeps the why3find certificate for an
    obligation: `verif/<concept>/proof.json`, under a directory named
    for the obligation's concept -- exactly the anchor
    record-assurance's `certificate_path_for` requires before recording
    and gate-g14 re-checks on every run (chainlink #92)."""
    return f"verif/{concept_segment(obligation)}/proof.json"


def achieved(
    kind="creusot-deductive-check", claim="postcondition-holds", assumptions=(), scope=None,
    proof_targets=None,
) -> dict:
    verifier = {
        "creusot-deductive-check": "creusot",
        "kani-bounded-model-check": "kani",
        "verus-deductive-check": "verus",
    }[kind]
    if not scope:
        # An obligation record is backed by a certificate (chainlink
        # #92): gate-g14 re-opens every proof_targets before trusting
        # the record, so the fixture records one the same way
        # record-assurance always does. Callers with no certificate to
        # name (a pure satisfies() unit test, a bridge record) keep the
        # inert scope -- those never pass through gate_cluster.
        scope = {"proof_targets": proof_targets} if proof_targets else {"input_domain": "all"}
    return {
        "schema_version": "1.0",
        "claim": {"kind": claim, "result": "pass"},
        "evidence": {
            "kind": kind,
            "verifier": verifier,
            # record-assurance derives harness (like verifier and kind)
            # from the manifest's own harness -- gate-g14 re-derives it
            # the same way (chainlink #92).
            "harness": verifier,
            "scope": scope,
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
                    # The verifier this work package records its evidence
                    # with -- record-assurance derives evidence.kind,
                    # verifier and harness from exactly this field, and
                    # gate-g14 re-derives them against it (chainlink #92).
                    "harness": "creusot",
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
        self.descriptor = default_descriptor()
        self.write("project-descriptor.json", self.descriptor)
        self.write(
            "crates/scheduler/specs/_boundaries/scheduler_dispatch__to__task_queue_pop_ready.json",
            boundary_contract(),
        )
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
            "obligation_records": [
                {
                    "obligation_id": "Scheduler.C001",
                    "record": achieved(proof_targets=certificate_path("Scheduler.C001")),
                }
            ],
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
            "obligation_records": [
                {
                    "obligation_id": "TaskQueue.C003",
                    "record": achieved(proof_targets=certificate_path("TaskQueue.C003")),
                }
            ],
            "bridge_records": [],
        })
        self.write("ci/results/WP-C.json", {
            "schema_version": "1.0",
            "work_package": "WP-C",
            "obligation_records": [
                {
                    "obligation_id": "Clock.C007",
                    "record": achieved(proof_targets=certificate_path("Clock.C007")),
                }
            ],
            "bridge_records": [],
        })
        self.write("crates/scheduler/specs/_bridges/BR-SCHED-TQ-001.json", bridge_spec())
        self.write("ci/results/c_static/crates_scheduler.json", callsite_report())
        profile = valid_profile()
        profile["work_packages"] = ["WP-A"]
        self.write("specs/_closure/scheduler-core.json", profile)
        # The certificates the records above name: gate-g14 re-opens
        # every proof_targets (chainlink #92), so the fixture needs the
        # real why3find files on disk, under each obligation's concept
        # directory, or the happy path would fail closed.
        for obligation in ("Scheduler.C001", "TaskQueue.C003", "Clock.C007"):
            self.write_certificate(obligation)

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

    def write_certificate(self, obligation: str) -> Path:
        """A why3find proof certificate for an obligation at
        `verif/<concept>/proof.json`, in the shape `cargo creusot` leaves
        behind (a proved goal carries a prover result) -- the artifact
        record-assurance reads before recording and gate-g14 re-opens on
        every run (chainlink #92)."""
        path = self.root / certificate_path(obligation)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "profile": [],
            "proofs": {"Coma": {"vc_default": {"prover": "z3", "time": 0.01}}},
        }))
        return path

    DEGRADATION_RECORD = "specs/_closure/scheduler-core.degradation.json"

    def approve_record(self, relative=None, review=None) -> None:
        """Append the audit entry review_checkpoint.approve() would have
        written for a degradation record, so the record's own `review`
        block is PROVENANCED (chainlink #97).

        The log is workspace-scoped (review_checkpoint.review_log_path),
        which is the whole point of approve_record living here rather
        than in a shared tmpdir: a gate reading a different log than the
        one approve() appends to is the cwd-relative bug this pipeline has
        already paid for twice."""
        relative = relative or self.DEGRADATION_RECORD
        review = review or REVIEW
        log = self.root / "ci" / "results" / "review_log.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as stream:
            stream.write(json.dumps({
                "target_path": str(self.root / relative),
                "classification": "new",
                "reviewer": review["reviewer"],
                "reviewed_at": review["reviewed_at"],
                "validation": "checked",
                "logged_at": "2026-09-04T12:00:00+00:00",
            }) + "\n")

    def rule_on_record(self, relative=None, verdict="ratified") -> None:
        """Append the human ruling generate_promotion_receipt.
        record_ruling() would have written over the record's CURRENT
        bytes -- its `artifacts[].hash` comes from the same
        compute_artifact_manifest() the gate re-computes with, so
        ratify-then-edit stops covering the record (chainlink #82's
        hash-pinning, now load-bearing for a degradation record too)."""
        relative = relative or self.DEGRADATION_RECORD
        log = self.root / "ci" / "results" / "human_rulings.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as stream:
            stream.write(json.dumps({
                "artifacts": compute_artifact_manifest(self.root, [relative]),
                "verdict": verdict,
                "reviewer": "alice",
                "ruled_at": "2026-09-05",
                "logged_at": "2026-09-05T12:00:00+00:00",
            }) + "\n")

    def write_accepted_record(self, record, relative=None, verdict="ratified") -> Path:
        """A degradation record that may actually RELEASE a cluster
        under chainlink #97: written, then provenanced against the
        approval log, then ratified over its exact bytes. Every test
        below that expects a `degraded` outcome goes through here, so
        the release route those tests exercise is the real one -- a
        plain `write()` of a record is now, correctly, not enough."""
        relative = relative or self.DEGRADATION_RECORD
        path = self.write(relative, record)
        self.approve_record(relative)
        self.rule_on_record(relative, verdict)
        return path

    def outcomes(self):
        return gate_workspace(self.root, self.descriptor)

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


def make_closure_kani_only(ws: Workspace) -> None:
    """Every achieved record in the closure -- obligation and bridge
    alike -- a Kani result, with the profile agreeing that Kani owns the
    cluster and the kind is `bounded`. This is the shape where CG3's
    question is live and the artifacts cannot answer it: which callees
    are generic is type information nothing here carries.

    Module level rather than a method on `DeclaredConditionTest` so the
    CG3 exception's own test class (chainlink #100) builds the same
    workspace the declared-bit tests do, instead of a second,
    drifting copy of this rewrite."""
    for name in ("WP-A", "WP-B", "WP-C"):
        ws.patch(f"ci/manifest/{name}.json", lambda d: [
            entry["required_assurance"].update(
                {"accepted_evidence_kinds": ["kani-bounded-model-check"]}
            )
            for entry in (
                d["definition_of_done"]["provided_guarantees"]
                + d["definition_of_done"]["required_guarantees"]
                + d["definition_of_done"]["required_preconditions_to_establish"]
            )
        ])
        # The manifest declares the kani harness for the guarantees it
        # now records kani evidence for -- the field gate-g14
        # re-derives evidence.verifier/kind/harness from
        # (chainlink #92).
        ws.patch(f"ci/manifest/{name}.json", lambda d: [
            entry.update({"harness": "kani"})
            for entry in d["definition_of_done"]["provided_guarantees"]
        ])
        ws.patch(f"ci/results/{name}.json", lambda d: [
            entry["record"]["evidence"].update(
                {"kind": "kani-bounded-model-check", "verifier": "kani", "harness": "kani"}
            )
            for entry in d["obligation_records"] + d["bridge_records"]
        ])
    ws.patch(
        "specs/_closure/scheduler-core.json",
        lambda d: (
            d.update({"closure_kind": "bounded"}),
            d["conditions"].update({"owning_verifier": "kani"}),
        ),
    )


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


class MalformedTrustAssumptionsTest(GateTestCase):
    """chainlink #91: docs/assurance-report-schema.json types each
    `record` only as an object (satisfies() owns the achieved shape), so
    the transitive sweep in gate_g14 can reach `trust.assumptions` before
    anything has required it to be an id list. Hashing such a value is a
    TypeError -- a traceback where the operator needs a finding -- so
    every unreadable shape has to come back as a G14 finding naming the
    field, still blocking, with no Python in the output."""

    def object_shaped_assumption(self):
        """The issue's own repro: an achieved record whose
        trust.assumptions holds an object instead of an id string."""
        self.ws.patch("ci/results/WP-C.json", lambda d: d["obligation_records"][0]["record"]["trust"].update(
            {"assumptions": [{"assumption_id": "A-SMOKE", "statement": "unproved", "justification": "probe"}]}))

    def test_an_object_shaped_assumption_is_a_finding_naming_the_field(self):
        self.object_shaped_assumption()
        outcome, _ = self.ws.cluster()
        errors = self.errors(outcome)
        self.assertTrue(any(
            "trust.assumptions[0]" in e and "'A-SMOKE'" in e and "WP-C:Clock.C007" in e for e in errors
        ), errors)
        self.assertEqual(outcome.status, "blocked")

    def test_the_gate_reports_it_through_the_cli_without_a_traceback(self):
        self.object_shaped_assumption()
        code, printed = self.ws.run()
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertNotIn("Traceback", printed)
        self.assertIn("trust.assumptions[0]", printed)

    def test_an_assumptions_field_that_is_not_a_list_is_reported(self):
        self.ws.patch("ci/results/WP-C.json", lambda d: d["obligation_records"][0]["record"]["trust"].update(
            {"assumptions": {"A-SMOKE": "unproved"}}))
        outcome, _ = self.ws.cluster()
        errors = self.errors(outcome)
        self.assertTrue(any(
            "trust.assumptions is" in e and "not a list" in e and "WP-C:Clock.C007" in e for e in errors
        ), errors)
        self.assertEqual(outcome.status, "blocked")

    def test_a_non_string_entry_is_reported_even_though_it_would_hash(self):
        # 7 is hashable, so the pre-fix sweep survived it only to die in
        # sorted() against the string ids; the shape check answers it too.
        self.ws.patch("ci/results/WP-C.json", lambda d: d["obligation_records"][0]["record"]["trust"].update(
            {"assumptions": [7]}))
        outcome, _ = self.ws.cluster()
        errors = self.errors(outcome)
        self.assertTrue(any("trust.assumptions[0] is 7" in e for e in errors), errors)
        self.assertEqual(outcome.status, "blocked")

    def test_an_unreadable_entry_fails_the_declared_condition_closed(self):
        self.object_shaped_assumption()
        outcome, _ = self.ws.cluster()
        errors = self.errors(outcome)
        self.assertTrue(any(
            e.startswith("[G17/error]") and "transitive_assumptions_within_policy is declared as True"
            in e for e in errors
        ), errors)

    def test_an_id_string_entry_keeps_its_exact_existing_diagnostics(self):
        # Control case from the issue: the same edit with a string id is
        # still judged against the policy, not mistaken for a malformed one.
        self.ws.patch("ci/results/WP-C.json", lambda d: d["obligation_records"][0]["record"]["trust"].update(
            {"assumptions": ["A-SMOKE"]}))
        outcome, _ = self.ws.cluster()
        errors = self.errors(outcome)
        self.assertTrue(any("trust.assumptions includes 'A-SMOKE'" in e for e in errors), errors)
        self.assertTrue(any("trust policy applies across the whole closure" in e for e in errors), errors)
        self.assertFalse(any("not an assumption id string" in e for e in errors), errors)
        self.assertEqual(outcome.status, "blocked")


class RecordCertificateRecheckTest(GateTestCase):
    """chainlink #92: gate-g14 took every achieved record's
    `evidence.scope.proof_targets` at face value. record-assurance
    re-read the real why3find certificate before recording (path
    containment, the concept anchor, proved goals), but nothing on the
    READ side ever opened it again -- so any schema-valid edit still
    closed the cluster as "VERIFIED from artifact data, not taken on
    declaration" while write-set-check called the edit clean. Each test
    below is one of the issue's own single-field edits: the record stays
    schema-valid, only the certificate behind it is wrong."""

    def nonexistent_proof_target(self):
        self.ws.patch("ci/results/WP-C.json", lambda d: d["obligation_records"][0]["record"]["evidence"][
            "scope"].update({"proof_targets": "rust/NOPE/does/not/exist/proof.json"}))

    def test_the_issue_repro_of_a_nonexistent_proof_target_blocks(self):
        self.nonexistent_proof_target()
        outcome, _ = self.ws.cluster()
        errors = self.errors(outcome)
        self.assertTrue(any(
            "proof certificate" in e and "does not exist" in e and "WP-C:Clock.C007" in e for e in errors
        ), errors)
        self.assertEqual(outcome.status, "blocked")

    def test_a_certificate_with_no_proved_goals_blocks(self):
        # The issue's second edit: proof_targets pointing at a REAL
        # proof.json -- right name, right concept directory -- whose
        # `proofs` section is empty.
        (self.ws.root / certificate_path("Clock.C007")).write_text(
            json.dumps({"profile": [], "proofs": {}})
        )
        outcome, _ = self.ws.cluster()
        errors = self.errors(outcome)
        self.assertTrue(any("carries no `proofs`" in e and "WP-C:Clock.C007" in e for e in errors), errors)
        self.assertEqual(outcome.status, "blocked")

    def test_a_certificate_with_a_stuck_subgoal_blocks(self):
        # A null leaf is why3find's own "stuck subgoal" -- record-assurance
        # refuses to record it, and the gate must refuse to trust a record
        # that claims it now.
        (self.ws.root / certificate_path("Clock.C007")).write_text(
            json.dumps({"profile": [], "proofs": {"Coma": {"vc_partial": None}}})
        )
        outcome, _ = self.ws.cluster()
        errors = self.errors(outcome)
        self.assertTrue(any("stuck/incomplete" in e and "WP-C:Clock.C007" in e for e in errors), errors)
        self.assertEqual(outcome.status, "blocked")

    def test_a_record_naming_no_certificate_at_all_blocks(self):
        # Deleting the field is the same fabrication as pointing it
        # nowhere: record-assurance never writes a record without
        # proof_targets, so a record with nothing behind it fails closed
        # instead of being skipped.
        self.ws.patch("ci/results/WP-C.json", lambda d: d["obligation_records"][0]["record"]["evidence"][
            "scope"].update({"proof_targets": ""}))
        outcome, _ = self.ws.cluster()
        errors = self.errors(outcome)
        self.assertTrue(any(
            "names no proof certificate" in e and "WP-C:Clock.C007" in e for e in errors
        ), errors)
        self.assertEqual(outcome.status, "blocked")

    def test_a_certificate_from_another_concepts_directory_blocks(self):
        # A real, proved certificate that belongs to `scheduler`, not
        # `clock` -- record-assurance's concept anchor, re-checked.
        elsewhere = self.ws.root / "verif" / "scheduler" / "proof.json"
        elsewhere.parent.mkdir(parents=True, exist_ok=True)
        elsewhere.write_text(json.dumps({"profile": [], "proofs": {"Coma": {"vc_x": {"prover": "z3"}}}}))
        self.ws.patch("ci/results/WP-C.json", lambda d: d["obligation_records"][0]["record"]["evidence"][
            "scope"].update({"proof_targets": "verif/scheduler/proof.json"}))
        outcome, _ = self.ws.cluster()
        errors = self.errors(outcome)
        self.assertTrue(any(
            "must sit under a directory named for its concept" in e and "WP-C:Clock.C007" in e
            for e in errors
        ), errors)
        self.assertEqual(outcome.status, "blocked")

    def test_a_certificate_outside_the_workspace_blocks(self):
        self.ws.patch("ci/results/WP-C.json", lambda d: d["obligation_records"][0]["record"]["evidence"][
            "scope"].update({"proof_targets": "/definitely/outside/proof.json"}))
        outcome, _ = self.ws.cluster()
        errors = self.errors(outcome)
        self.assertTrue(any("resolves outside the workspace" in e for e in errors), errors)
        self.assertEqual(outcome.status, "blocked")

    def test_a_certificate_older_than_its_coma_program_blocks(self):
        # The same staleness refusal record-assurance applies: a
        # certificate that no longer describes what is on disk is not
        # re-openable as evidence, wherever it is read from.
        certificate = self.ws.root / certificate_path("Clock.C007")
        certificate.write_text(json.dumps({"profile": [], "proofs": {"Coma": {"vc_x": {"prover": "z3"}}}}))
        coma = certificate.parent.parent / (certificate.parent.name + ".coma")
        coma.write_text("(* a coma program *)\n")
        os.utime(coma, (2_000_000, 2_000_000))
        os.utime(certificate, (1_000_000, 1_000_000))
        outcome, _ = self.ws.cluster()
        errors = self.errors(outcome)
        self.assertTrue(any("older than the Coma program" in e and "WP-C:Clock.C007" in e
                            for e in errors), errors)
        self.assertEqual(outcome.status, "blocked")

    def test_the_gate_reports_it_through_the_cli_without_a_traceback(self):
        self.nonexistent_proof_target()
        code, printed = self.ws.run()
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertNotIn("Traceback", printed)
        self.assertIn("proof certificate", printed)
        self.assertIn("chainlink #92", printed)

    def test_a_degradation_record_cannot_excuse_it(self):
        # failed_conditions vocabulary is closure CONDITION keys only --
        # there is no vocabulary in which a human accepts "the proof is
        # missing" as a ceiling, record present or not.
        self.ws.write("specs/_closure/scheduler-core.degradation.json", valid_degradation())
        self.nonexistent_proof_target()
        outcome, _ = self.ws.cluster()
        errors = self.errors(outcome)
        self.assertTrue(any("proof certificate" in e for e in errors), errors)
        self.assertEqual(outcome.status, "blocked")

    def test_the_untouched_records_still_close(self):
        # The control case: certificates on disk re-open cleanly, and
        # bridge records -- which carry no proof_targets by design (G9
        # cross-checks those) -- are not dragged in.
        outcome, _ = self.ws.cluster()
        self.assertEqual(self.errors(outcome), [])
        self.assertEqual(outcome.status, "closes")


class RecordDerivationRecheckTest(GateTestCase):
    """chainlink #92, the record's derived fields: record-assurance
    derives evidence.kind/verifier/harness from the manifest's own
    `harness` and config from the manifest's `provenance` -- the gate
    took those at face value the way it took the certificate, so each of
    the issue's remaining single-field edits (all measured exit 0) now
    has to be re-derived and refused."""

    def flip(self, field, value):
        self.ws.patch("ci/results/WP-C.json", lambda d: d["obligation_records"][0]["record"][
            "evidence"].update({field: value}))

    def test_a_flipped_evidence_verifier_blocks(self):
        self.flip("verifier", "kani")
        outcome, _ = self.ws.cluster()
        errors = self.errors(outcome)
        self.assertTrue(any(
            "evidence.verifier is 'kani'" in e and "harness 'creusot'" in e and "WP-C:Clock.C007" in e
            for e in errors
        ), errors)
        self.assertEqual(outcome.status, "blocked")

    def test_a_flipped_evidence_harness_blocks(self):
        self.flip("harness", "kani")
        outcome, _ = self.ws.cluster()
        errors = self.errors(outcome)
        self.assertTrue(any(
            "evidence.harness is 'kani'" in e and "WP-C:Clock.C007" in e for e in errors
        ), errors)
        self.assertEqual(outcome.status, "blocked")

    def test_a_handwritten_toolchain_blocks(self):
        self.ws.patch("ci/results/WP-C.json", lambda d: d["obligation_records"][0]["record"][
            "config"].update({"toolchain": "handwritten"}))
        outcome, _ = self.ws.cluster()
        errors = self.errors(outcome)
        self.assertTrue(any(
            "config is" in e and "'handwritten'" in e and "provenance" in e and "WP-C:Clock.C007" in e
            for e in errors
        ), errors)
        self.assertEqual(outcome.status, "blocked")

    def test_an_evidence_kind_its_declared_harness_does_not_produce_blocks(self):
        # satisfies() already refuses a kind no requirement accepts;
        # this is the derivation half -- the kind is checked against the
        # manifest's harness, not only against the requirement's words.
        self.flip("kind", "verus-deductive-check")
        outcome, _ = self.ws.cluster()
        errors = self.errors(outcome)
        self.assertTrue(any(
            "evidence.kind is 'verus-deductive-check'" in e and "produces 'creusot-deductive-check'" in e
            for e in errors
        ), errors)
        self.assertEqual(outcome.status, "blocked")

    def test_a_record_the_manifest_cannot_vouch_for_is_left_to_the_certificate_check(self):
        # Deliberate boundary: a record for an obligation this manifest
        # does not provide has no manifest value to re-derive from
        # (record-assurance preserves such entries when a guarantee is
        # dropped), so it gets no derivation finding -- while its
        # certificate, anchored by the obligation's own concept, is
        # still re-opened like any other.
        self.ws.patch("ci/results/WP-C.json", lambda d: d["obligation_records"].append({
            "obligation_id": "Foreign.C9",
            "record": achieved(proof_targets="rust/NOPE/gone/proof.json"),
        }))
        outcome, _ = self.ws.cluster()
        errors = self.errors(outcome)
        self.assertTrue(any("Foreign.C9" in e and "does not exist" in e for e in errors), errors)
        self.assertFalse(any("disagrees with the manifest" in e and "Foreign.C9" in e for e in errors),
                         errors)

    def test_the_gate_reports_a_derivation_mismatch_through_the_cli(self):
        self.flip("verifier", "kani")
        code, printed = self.ws.run()
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertNotIn("Traceback", printed)
        self.assertIn("evidence.verifier", printed)
        self.assertIn("chainlink #92", printed)


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

    def test_a_bridge_whose_boundary_id_resolves_nowhere_blocks(self):
        # External review, high severity: load_bridges() used to validate
        # a bridge with no boundary context at all (boundaries_by_id
        # defaulted to None), which degrades G2 to a non-blocking info
        # note -- a cluster was reproduced closing successfully even
        # though its bridge's boundary_id resolved nowhere. Deleting the
        # only boundary contract in the fixture reproduces exactly that:
        # the bridge itself is otherwise perfectly well-formed.
        (
            self.ws.root
            / "crates/scheduler/specs/_boundaries/scheduler_dispatch__to__task_queue_pop_ready.json"
        ).unlink()
        outcome, _ = self.ws.cluster()
        self.assertEqual(outcome.status, "blocked")
        self.assertTrue(any("no valid bridge specification" in e for e in self.errors(outcome)))

    def test_a_bridge_whose_boundary_lacks_the_callee_guarantee_blocks(self):
        # The other half of G2: the boundary exists and is otherwise
        # valid, but never declares the specific guarantee this bridge
        # claims to discharge.
        boundary = boundary_contract()
        boundary["callee_guarantees"] = ["TaskQueue.C099"]
        self.ws.write(
            "crates/scheduler/specs/_boundaries/scheduler_dispatch__to__task_queue_pop_ready.json",
            boundary,
        )
        outcome, _ = self.ws.cluster()
        self.assertEqual(outcome.status, "blocked")
        self.assertTrue(any("no valid bridge specification" in e for e in self.errors(outcome)))

    def test_load_bridges_scopes_a_bridges_boundary_check_to_its_own_crate(self):
        # A bridge's boundary_id must resolve within the SAME crate
        # (validate_bridge.py's own docstring) -- a merged cross-crate
        # boundaries_by_id would let a bridge in one crate silently
        # resolve against a same-named boundary declared in another,
        # defeating that scoping instead of just fixing the original gap.
        # Two crates, two DIFFERENTLY-named bridges (so the cross-crate
        # bridge_id ambiguity check below stays out of the way of what
        # this test is isolating): only ONE crate has the matching
        # boundary contract, so the other crate's bridge must fail its
        # own G2 check rather than resolve via the first crate's boundary.
        from gate_g14 import load_bridges

        descriptor = copy.deepcopy(self.ws.descriptor)
        descriptor["crates"].append(
            {"crate_dir": "crates/other", "contracts_crate": "contracts", "specs_search_root": "crates"}
        )
        self.ws.write(
            "crates/other/specs/_bridges/BR-OTHER-TQ-001.json",
            bridge_spec(bridge_id="BR-OTHER-TQ-001"),
        )
        # deliberately no boundary contract under crates/other/specs/_boundaries/

        bridges, findings = load_bridges(self.ws.root, descriptor)
        self.assertIn("BR-SCHED-TQ-001", bridges)
        self.assertNotIn("BR-OTHER-TQ-001", bridges)
        self.assertFalse(findings)

    def test_a_bridge_id_discovered_in_two_crates_is_ambiguous_not_first_wins(self):
        # External review, high severity: an earlier fix scoped G2
        # VALIDATION per crate but still merged the RESULT into one flat
        # dict keyed only by bridge_id via setdefault -- first-crate-wins.
        # Reproduced: WP-A (crate scheduler) requires BR-SCHED-TQ-001; its
        # own crate's boundary contract is removed, invalidating its
        # bridge; a second crate supplies an independently VALID bridge
        # under the same bridge_id. The scheduler cluster used to close
        # by resolving against the other crate's substitute. bridge_id is
        # a workspace-wide identifier naming exactly one bridge, so this
        # must be an ambiguous-identity hard error, never a silent
        # substitution -- the same choice already made for a work package
        # providing an obligation twice.
        (
            self.ws.root
            / "crates/scheduler/specs/_boundaries/scheduler_dispatch__to__task_queue_pop_ready.json"
        ).unlink()

        descriptor = copy.deepcopy(self.ws.descriptor)
        descriptor["crates"].append(
            {"crate_dir": "crates/other", "contracts_crate": "contracts", "specs_search_root": "crates"}
        )
        self.ws.write(
            "crates/other/specs/_boundaries/scheduler_dispatch__to__task_queue_pop_ready.json",
            boundary_contract(),
        )
        self.ws.write("crates/other/specs/_bridges/BR-SCHED-TQ-001.json", bridge_spec())
        self.ws.descriptor = descriptor
        self.ws.write("project-descriptor.json", descriptor)

        from gate_g14 import load_bridges

        bridges, findings = load_bridges(self.ws.root, descriptor)
        self.assertNotIn("BR-SCHED-TQ-001", bridges)
        self.assertTrue(any("more than one crate" in str(f) for f in findings))

        outcome, workspace_findings = self.ws.cluster()
        self.assertEqual(outcome.status, "blocked")
        all_errors = self.errors(outcome) + [str(f) for f in workspace_findings if f.severity == "error"]
        self.assertTrue(any("no valid bridge specification" in e for e in all_errors))

    def test_a_bridge_at_a_noncanonical_path_is_rejected_not_silently_accepted(self):
        # External review, high severity: find_bridge_files discovers
        # **/_bridges/**/* recursively, with no anchor to the crate's
        # actual declared layout (crates/*/specs/_bridges/), and
        # load_bridges never checked one -- an otherwise perfectly
        # well-formed bridge moved to
        # crates/scheduler/not_specs/_bridges/BR-SCHED-TQ-001.json still
        # resolved and closed the cluster with zero findings.
        # validate_bridge.py's own validate_crate already anchors this
        # exact way for the standalone CLI; load_bridges just never
        # applied the same check.
        canonical = self.ws.root / "crates/scheduler/specs/_bridges/BR-SCHED-TQ-001.json"
        data = json.loads(canonical.read_text())
        canonical.unlink()
        self.ws.write("crates/scheduler/not_specs/_bridges/BR-SCHED-TQ-001.json", data)

        outcome, workspace_findings = self.ws.cluster()
        self.assertEqual(outcome.status, "blocked")
        self.assertTrue(any("no valid bridge specification" in e for e in self.errors(outcome)))
        self.assertTrue(
            any("not directly under the canonical directory" in str(f) for f in workspace_findings)
        )

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
        # A work package recording kani evidence declares the kani
        # harness -- gate-g14 re-derives evidence.verifier/kind from
        # exactly this field (chainlink #92).
        self.ws.patch("ci/manifest/WP-C.json", lambda d: d["definition_of_done"]["provided_guarantees"][0].update(
            {"harness": "kani"}))
        self.ws.patch("ci/manifest/WP-B.json", lambda d: d["definition_of_done"]["required_guarantees"][0][
            "required_assurance"].update({"accepted_evidence_kinds": ["kani-bounded-model-check"]}))
        self.ws.patch("ci/results/WP-C.json", lambda d: d["obligation_records"][0].update(
            {"record": achieved(kind="kani-bounded-model-check",
                                proof_targets=certificate_path("Clock.C007"))}))
        outcome, _ = self.ws.cluster()
        errors = self.errors(outcome)
        self.assertTrue(any("makes the cluster's guarantee bounded" in e for e in errors))
        self.assertTrue(any("cross-verifier composition has no soundness theorem" in e for e in errors))

    def test_partial_is_reported_with_the_outcome(self):
        """chainlink #85: a partially-validated closure declares `partial`
        and the gate reports the declared kind like any other, without a
        new branch refusing it."""
        self.ws.patch("specs/_closure/scheduler-core.json", lambda d: d.update({"closure_kind": "partial"}))
        outcome, _ = self.ws.cluster()
        self.assertEqual(self.errors(outcome), [])
        self.assertEqual(outcome.status, "closes")
        self.assertIn("closure_kind=partial", outcome.summary())

    def test_partial_is_never_refuted_from_evidence_kinds(self):
        """The same closure that refutes `deductive` (a Kani result three
        hops down) says nothing against `partial`: that check polices the
        uniform kinds' rounding, and `partial` claims strictly less than
        either. Everything `partial` describes is still policed by the
        findings that own it -- here, the cross-verifier condition."""
        self.ws.patch("specs/_closure/scheduler-core.json", lambda d: d.update({"closure_kind": "partial"}))
        self.ws.patch("ci/manifest/WP-C.json", lambda d: d["definition_of_done"]["provided_guarantees"][0][
            "required_assurance"].update({"accepted_evidence_kinds": ["kani-bounded-model-check"]}))
        self.ws.patch("ci/manifest/WP-C.json", lambda d: d["definition_of_done"]["provided_guarantees"][0].update(
            {"harness": "kani"}))
        self.ws.patch("ci/manifest/WP-B.json", lambda d: d["definition_of_done"]["required_guarantees"][0][
            "required_assurance"].update({"accepted_evidence_kinds": ["kani-bounded-model-check"]}))
        self.ws.patch("ci/results/WP-C.json", lambda d: d["obligation_records"][0].update(
            {"record": achieved(kind="kani-bounded-model-check",
                                proof_targets=certificate_path("Clock.C007"))}))
        outcome, _ = self.ws.cluster()
        self.assertFalse(any("closure_kind" in e for e in self.errors(outcome)))
        # the shortfall is still visible, just not as a kind refutation
        self.assertTrue(any("cross-verifier composition has no soundness theorem" in e
                            for e in self.errors(outcome)))

    def test_check_closure_kind_returns_nothing_for_partial(self):
        """Unit-level contract of the missing branch: neither a mixed nor
        an all-deductive closure produces a finding against `partial`."""
        profile = {"cluster": "scheduler-core", "closure_kind": "partial"}
        for kinds in (["creusot-deductive-check", "kani-bounded-model-check"],
                      ["creusot-deductive-check"],
                      ["kani-bounded-model-check"]):
            with self.subTest(evidence_kinds=kinds):
                self.assertEqual(check_closure_kind(profile, {"_evidence_kinds": kinds}), [])


class DeclaredConditionTest(GateTestCase):
    """chainlink #86: a declared condition bit is a claim to check.
    Six of the seven are recomputed outright; the seventh (CG3) is
    recomputed from artifact data whenever those data determine it, and
    is otherwise a capability gap the gate refuses to pass without a
    tracking record -- never a free pass, in either direction."""

    CG3 = "generic_callees_type_universal_or_creusot_owned"

    def make_closure_kani_only(self) -> None:
        make_closure_kani_only(self.ws)

    def cg3_record(self, failed_conditions) -> dict:
        record = valid_degradation()
        record["failed_conditions"] = list(failed_conditions)
        record["ceiling"] = "per-instantiation"
        record["capability_gap"] = "CG3"
        record["tracking_issue"] = "chainlink:86"
        return record

    def test_an_unaccepted_cg3_record_still_reports_its_staleness(self):
        """The stale-record diagnostic reads the record as DATA, so it
        fires whether or not that record was ever accepted -- an
        operator holding an unprovenanced record still needs to be told
        it is stale too, not just that it was never reviewed
        (chainlink #97)."""
        self.ws.write(self.ws.DEGRADATION_RECORD, self.cg3_record([self.CG3]))
        outcome, _ = self.ws.cluster()
        errors = self.errors(outcome)
        self.assertTrue(any("stale tracking record" in e for e in errors), errors)
        self.assertTrue(any("no approval audit log" in e for e in errors), errors)
        self.assertEqual(self.degraded(outcome), [])
        self.assertEqual(outcome.status, "blocked")

    def test_a_mis_declared_condition_is_rejected(self):
        self.ws.patch(
            "specs/_closure/scheduler-core.json",
            lambda d: d["conditions"].update({"protocol_class_all_pairwise": False}),
        )
        outcome, _ = self.ws.cluster()
        self.assertTrue(any("recomputed, never stored-and-trusted" in e for e in self.errors(outcome)))

    def test_the_cg3_condition_is_verified_from_artifact_data(self):
        """#86's first remedy: the all-deductive fixture determines the
        condition -- every record in the closure is a creusot result,
        so nothing it rests on is verified per monomorphization -- and
        the gate says VERIFIED, not HUMAN DECLARATION."""
        outcome, _ = self.ws.cluster()
        infos = [str(f) for f in outcome.findings if f.severity == "info"]
        self.assertTrue(any("VERIFIED from artifact data" in i for i in infos), infos)
        self.assertFalse(any("HUMAN DECLARATION" in i for i in infos), infos)
        self.assertEqual(self.errors(outcome), [])
        self.assertEqual(outcome.status, "closes")

    def test_a_false_cg3_declaration_is_rejected_when_the_artifacts_determine_it(self):
        """The other direction of #86: over a closure with no Kani
        result anywhere, `false` is not an honest gap, it contradicts
        the records -- and a mis-declared bit is never excusable by a
        degradation record."""
        self.ws.patch(
            "specs/_closure/scheduler-core.json",
            lambda d: d["conditions"].update({self.CG3: False}),
        )
        self.ws.write("specs/_closure/scheduler-core.degradation.json", self.cg3_record([self.CG3]))
        outcome, _ = self.ws.cluster()
        self.assertTrue(
            any("recomputed, never stored-and-trusted" in e for e in self.errors(outcome))
        )
        self.assertEqual(outcome.status, "blocked")

    def test_a_true_cg3_declaration_no_artifact_can_verify_blocks(self):
        """#86's own reproduction: a closure profile declaring
        `generic_callees: true` over a Kani-only closure used to pass
        with nothing checked. The condition is now a gap the gate
        refuses to accept unverified."""
        self.make_closure_kani_only()
        outcome, _ = self.ws.cluster()
        errors = self.errors(outcome)
        self.assertTrue(any("cannot verify it" in e for e in errors), errors)
        self.assertTrue(any("never a free pass" in e and "tracking issue" in e for e in errors), errors)
        self.assertEqual(outcome.status, "blocked")

    def test_a_tracked_cg3_gap_is_degraded_and_reported_with_its_record(self):
        """#86's second remedy: the same declaration, beside a
        degradation record naming the condition with capability_gap CG3
        and a tracking issue, is released as a declared gap -- reported
        explicitly, with the record, never silently accepted. The record
        is itself accepted (chainlink #97): provenanced and ratified."""
        self.make_closure_kani_only()
        self.ws.write_accepted_record(self.cg3_record([self.CG3]))
        outcome, _ = self.ws.cluster()
        self.assertEqual(self.errors(outcome), [])
        self.assertEqual(outcome.status, "degraded")
        self.assertTrue(
            any("accepted as degradation under ceiling 'per-instantiation' (chainlink:86)" in d
                for d in self.degraded(outcome)),
            [str(f) for f in outcome.findings],
        )
        self.assertIn("does NOT close", outcome.summary())

    def test_an_unrecorded_work_package_makes_cg3_unverifiable(self):
        """No record at all is not evidence of no Kani result: a work
        package with a cleared report cannot vouch for what verified
        it, so the condition is a gap here too (and the missing record
        blocks on its own as well)."""
        self.ws.patch("ci/results/WP-C.json", lambda d: d["obligation_records"].clear())
        outcome, _ = self.ws.cluster()
        errors = self.errors(outcome)
        self.assertTrue(any("cannot verify it" in e and "WP-C" in e for e in errors), errors)

    def test_a_declared_false_cg3_condition_is_a_failing_condition(self):
        """The pre-#86 path, preserved where it belongs: a Kani-only
        cluster stating the CG3 gap declares it false, and that is an
        excusable failing condition, not a mis-declaration."""
        self.make_closure_kani_only()
        self.ws.patch(
            "specs/_closure/scheduler-core.json",
            lambda d: d["conditions"].update({self.CG3: False}),
        )
        outcome, _ = self.ws.cluster()
        self.assertTrue(any("per monomorphization" in e for e in self.errors(outcome)))
        self.assertFalse(any("recomputed, never stored-and-trusted" in e for e in self.errors(outcome)))

    def test_a_cg3_tracking_record_the_evidence_outgrew_is_stale(self):
        """The two-direction discipline applied where the evidence
        lives: profile and record alone cannot tell an unverifiable
        declaration from a verified one, so gate_g14 rejects the record
        as stale the moment the closure's own records verify the
        condition -- and the record must not be able to excuse itself.
        The record is ACCEPTED (chainlink #97), so this leg is about
        staleness alone rather than about a record that could never
        release anything."""
        self.ws.write_accepted_record(self.cg3_record([self.CG3]))
        outcome, _ = self.ws.cluster()
        errors = self.errors(outcome)
        self.assertTrue(any("stale tracking record" in e for e in errors), errors)
        self.assertTrue(any("chainlink:86" in e for e in errors), errors)
        self.assertEqual(outcome.status, "blocked")


class Cg3SoundnessTest(GateTestCase):
    """chainlink #93: the pilot's 19-leg harness reduced the 1.1.1
    `VERIFIED from artifact data` verdict to a two-part proxy -- >=1
    record per work package NAMED IN THE CLOSURE PROFILE, and no Kani
    KIND string among those records -- and every leg below is one place
    that proxy read less than the claim. The verdict now walks both
    dependency edges (required guarantees AND declared `depends_on`),
    evaluates record presence per obligation, and requires creusot
    ownership positively, from the whole workspace's manifests and
    ledgers."""

    CG3 = "generic_callees_type_universal_or_creusot_owned"

    def verified_infos(self, outcome) -> list[str]:
        return [
            str(f) for f in outcome.findings
            if f.severity == "info" and "VERIFIED from artifact data" in str(f)
        ]

    def kani_work_package(self, ledger: bool = True) -> None:
        """The issue's WP-KANI-OTHER: a schema-valid Kani-owned work
        package -- `harness: kani` on its provided guarantee -- plus its
        `kani-bounded-model-check` ledger, exactly as legs C1/C2/C5
        mutate only the PLACEMENT of."""
        self.ws.write("ci/manifest/WP-KANI.json", manifest(
            "WP-KANI",
            provided=[("Generic.C001", required_assurance(kinds=("kani-bounded-model-check",)))],
        ))
        self.ws.patch("ci/manifest/WP-KANI.json", lambda d: [
            entry.update({"harness": "kani"})
            for entry in d["definition_of_done"]["provided_guarantees"]
        ])
        if ledger:
            # named and written together: leg C2 puts this work package
            # INTO the closure, where gate-g14 re-opens the certificate
            # behind every record (chainlink #92) -- the placement legs
            # C1/C5 use never re-opens it, but the fixture is one shape.
            self.ws.write_certificate("Generic.C001")
            self.ws.write("ci/results/WP-KANI.json", {
                "schema_version": "1.0",
                "work_package": "WP-KANI",
                "obligation_records": [{
                    "obligation_id": "Generic.C001",
                    "record": achieved(
                        kind="kani-bounded-model-check",
                        proof_targets=certificate_path("Generic.C001"),
                    ),
                }],
                "bridge_records": [],
            })

    def assert_cg3_blocked_naming(self, outcome, needle: str) -> None:
        errors = self.errors(outcome)
        self.assertFalse(self.verified_infos(outcome), self.verified_infos(outcome))
        self.assertTrue(
            any(self.CG3 in e and needle in e for e in errors), errors
        )
        self.assertEqual(outcome.status, "blocked")

    def test_a_kani_work_package_reached_only_through_depends_on_blocks_the_verdict(self):
        """Leg C5, the issue's headline: the Creusot work package
        DECLARES its dependency on the Kani-owned one, and the gate used
        to print VERIFIED without ever opening its manifest or ledger --
        `gate-g14 | grep -c WP-KANI-OTHER` returned 0."""
        self.kani_work_package()
        self.ws.patch("ci/manifest/WP-A.json", lambda d: d.update({"depends_on": ["WP-KANI"]}))
        outcome, _ = self.ws.cluster()
        self.assert_cg3_blocked_naming(outcome, "WP-KANI")
        # the issue's own probe, end to end through the CLI
        code, printed = self.ws.run()
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertIn("WP-KANI", printed)

    def test_the_same_work_package_named_nowhere_still_fails_the_verdict_closed(self):
        """Leg C1: placement in the closure PROFILE's list was the only
        placement the scan read, so the same Kani-owned work package
        sitting unreferenced in the project verified too. Nothing bounds
        a generic callee to the profile's list (CG2: the call graph is
        not knowable here), so the scan reads every manifest and ledger
        the workspace has."""
        self.kani_work_package()
        outcome, _ = self.ws.cluster()
        self.assert_cg3_blocked_naming(outcome, "WP-KANI")

    def test_a_kani_manifest_alone_fails_the_verdict_without_any_ledger(self):
        """The declaration half of the same scan: ownership is read
        positively from each manifest's own `harness`, so a Kani-owned
        work package with no ledger at all -- nothing to read a kind
        string from -- is still a Kani owner, never an empty scan."""
        self.kani_work_package(ledger=False)
        outcome, _ = self.ws.cluster()
        self.assert_cg3_blocked_naming(outcome, "harness 'kani'")

    def test_the_control_closure_profile_list_placement_is_still_caught(self):
        """Leg C2, the issue's control: the same work package ADDED to
        the closure profile's work_packages still blocks -- the fix
        widens the scan, it does not loosen the closure's own."""
        self.kani_work_package()
        self.ws.patch(
            "specs/_closure/scheduler-core.json",
            lambda d: d["work_packages"].append("WP-KANI"),
        )
        outcome, _ = self.ws.cluster()
        self.assert_cg3_blocked_naming(outcome, "WP-KANI")

    def test_one_missing_obligation_record_withholds_the_verdict(self):
        """Leg B2: premise (a) used to be >=1 record PER WORK PACKAGE,
        so WP-A losing its obligation record -- while its bridge record
        survives -- still printed VERIFIED at the same moment the gate
        blocked the cluster for 'recorded no achieved assurance'.
        Evaluated per obligation, the verdict moves with the record."""
        self.ws.patch("ci/results/WP-A.json", lambda d: d["obligation_records"].clear())
        outcome, _ = self.ws.cluster()
        self.assert_cg3_blocked_naming(outcome, "WP-A:Scheduler.C001")
        self.assertTrue(
            any("recorded no achieved assurance" in e for e in self.errors(outcome)),
            self.errors(outcome),
        )

    def test_a_missing_bridge_record_withholds_the_verdict_too(self):
        """The bridge half of premise (a): a required precondition with
        no bridge record blocks the cluster (G9's own finding), and the
        verdict must not read VERIFIED beside it either."""
        self.ws.patch("ci/results/WP-A.json", lambda d: d["bridge_records"].clear())
        outcome, _ = self.ws.cluster()
        self.assert_cg3_blocked_naming(outcome, "WP-A:BR-SCHED-TQ-001")

    def test_a_verus_owned_closure_does_not_satisfy_the_creusot_owned_disjunct(self):
        """Leg B7: relabelling every record creusot -> verus used to
        leave CG3 printing VERIFIED ('none of them is a Kani result')
        while the condition's own name says creusot_owned. Verus is
        neither disjunct -- type_universal has no authoring path (leg 4)
        -- so ownership is now required positively, and this cluster's
        ONLY error is the CG3 gap: declaration and computation agree
        everywhere else, isolating the disjunct exactly as the leg
        does."""
        for name in ("WP-A", "WP-B", "WP-C"):
            self.ws.patch(f"ci/manifest/{name}.json", lambda d: (
                [
                    entry.update({"harness": "verus"})
                    for entry in d["definition_of_done"]["provided_guarantees"]
                ],
                [
                    entry["required_assurance"].update(
                        {"accepted_evidence_kinds": ["verus-deductive-check"]}
                    )
                    for entry in (
                        d["definition_of_done"]["provided_guarantees"]
                        + d["definition_of_done"]["required_guarantees"]
                        + d["definition_of_done"]["required_preconditions_to_establish"]
                    )
                ],
            ))
            self.ws.patch(f"ci/results/{name}.json", lambda d: [
                entry["record"]["evidence"].update({
                    "kind": "verus-deductive-check", "verifier": "verus", "harness": "verus",
                })
                for entry in d["obligation_records"] + d["bridge_records"]
            ])
        self.ws.patch(
            "specs/_closure/scheduler-core.json",
            lambda d: d["conditions"].update({"owning_verifier": "verus"}),
        )
        outcome, _ = self.ws.cluster()
        errors = self.errors(outcome)
        self.assert_cg3_blocked_naming(outcome, "verus-deductive-check")
        self.assertFalse(
            any("recomputed, never stored-and-trusted" in e for e in errors), errors
        )

    def test_the_advertised_relief_path_stays_authorable(self):
        """The gap message advertises one relief: a degradation record
        naming the condition with a tracking issue. It must work on the
        new failure modes too -- a Kani-owned dependency reached only
        through `depends_on` (leg C5) releases the cluster as DEGRADED,
        exit 0, never as closed, and the reason is still named. The
        record is ACCEPTED (chainlink #97), so this still exercises the
        advertised relief rather than the refusal."""
        self.kani_work_package()
        self.ws.patch("ci/manifest/WP-A.json", lambda d: d.update({"depends_on": ["WP-KANI"]}))
        record = valid_degradation()
        record["failed_conditions"] = [self.CG3]
        record["ceiling"] = "per-instantiation"
        record["capability_gap"] = "CG3"
        record["tracking_issue"] = "chainlink:93"
        self.ws.write_accepted_record(record)
        outcome, _ = self.ws.cluster()
        self.assertFalse(self.verified_infos(outcome), self.verified_infos(outcome))
        self.assertEqual(self.errors(outcome), [])
        self.assertEqual(outcome.status, "degraded")
        self.assertTrue(
            any(self.CG3 in d for d in self.degraded(outcome)), [str(f) for f in outcome.findings]
        )
        self.assertIn("does NOT close", outcome.summary())

    def test_a_dangling_depends_on_fails_the_verdict_closed(self):
        """A `depends_on` naming no manifest at all used to be invisible
        to every gate: the closure only follows obligation edges. The
        dependency the gate cannot read is exactly where a Kani-owned
        generic callee could sit, so it fails closed rather than out of
        scope."""
        self.ws.patch("ci/manifest/WP-A.json", lambda d: d.update({"depends_on": ["WP-GONE"]}))
        outcome, _ = self.ws.cluster()
        self.assert_cg3_blocked_naming(outcome, "WP-GONE")

    def test_a_declared_dependency_with_no_ledger_blocks_on_its_missing_assurance(self):
        """A declared dependency is a declared RELIANCE: its achieved
        side is loaded like a closure work package's, so a Kani-owned
        manifest with no report blocks on the missing report itself,
        with the work package named."""
        self.kani_work_package(ledger=False)
        self.ws.patch("ci/manifest/WP-A.json", lambda d: d.update({"depends_on": ["WP-KANI"]}))
        outcome, _ = self.ws.cluster()
        errors = self.errors(outcome)
        self.assertTrue(
            any("no assurance report" in e and "WP-KANI" in e for e in errors), errors
        )
        self.assertEqual(outcome.status, "blocked")

    def test_an_unreadable_ledger_outside_the_closure_fails_the_verdict_closed(self):
        """A report that exists but cannot be read is not an absence of
        records -- it is records nobody can rule Kani-owned. Fail
        closed, naming the work package."""
        self.ws.write("ci/manifest/WP-EXTRA.json", manifest(
            "WP-EXTRA", provided=[("Other.C001", required_assurance())]
        ))
        (self.ws.root / "ci" / "results" / "WP-EXTRA.json").write_text("{ not json")
        outcome, _ = self.ws.cluster()
        self.assert_cg3_blocked_naming(outcome, "WP-EXTRA")

    def test_an_unreached_crusot_work_package_leaves_the_verdict_verified(self):
        """The control for the widened scan: a creusot-owned work
        package the cluster never names -- no manifest edge to it, no
        ledger recorded yet -- changes nothing, because its declared
        ownership is exactly what the verdict requires. The widened scan
        is strict about ownership, not about existence."""
        self.ws.write("ci/manifest/WP-EXTRA.json", manifest(
            "WP-EXTRA", provided=[("Other.C001", required_assurance())]
        ))
        outcome, _ = self.ws.cluster()
        self.assertTrue(self.verified_infos(outcome), [str(f) for f in outcome.findings])
        self.assertEqual(self.errors(outcome), [])
        self.assertEqual(outcome.status, "closes")


class DegradationTest(GateTestCase):
    def add_record(self, failed_conditions, ceiling="harness-tested"):
        """An ACCEPTED degradation record: written, provenanced against
        the approval log, and ratified over its exact bytes
        (chainlink #97). Every `degraded` assertion below is about the
        ceiling rules, so the record has to clear the provenance gate to
        reach them at all."""
        record = valid_degradation()
        record["failed_conditions"] = failed_conditions
        record["ceiling"] = ceiling
        self.ws.write_accepted_record(record)

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
        self.ws.write_accepted_record(record)
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


class InvalidClosureArtifactDiagnosticsTest(GateTestCase):
    """chainlink #49: an invalid closure artifact is fail-closed excluded
    from closure computation, but must not be dropped silently -- a
    workspace finding names the file and points to validate-closure."""

    def test_invalid_profile_is_named_explicitly(self):
        self.ws.patch(
            "specs/_closure/scheduler-core.json",
            lambda d: d["conditions"].update({"owning_verifier": "kani"}),  # closure_kind stays
        )  # deductive -- G17: a Kani-owned cluster can never claim deductive closure.
        outcomes, workspace_findings = self.ws.outcomes()
        self.assertEqual(outcomes, [])
        reasons = [str(f) for f in workspace_findings]
        self.assertTrue(
            any("scheduler-core.json" in r and "ignored as invalid" in r for r in reasons), reasons
        )
        self.assertTrue(any("pipeline.py validate-closure" in r for r in reasons), reasons)

    def test_invalid_degradation_record_is_named_while_condition_findings_remain(self):
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
        del record["affected_edges"]  # schema-required field missing -- G1a invalid
        self.ws.write("specs/_closure/scheduler-core.degradation.json", record)

        outcome, workspace_findings = self.ws.cluster()

        self.assertEqual(outcome.status, "blocked")
        self.assertTrue(
            any("protocol_class_all_pairwise" in e for e in self.errors(outcome)),
            "the real closure-condition finding must stay visible, not be swallowed by the "
            "invalid-degradation diagnostic",
        )
        reasons = [str(f) for f in workspace_findings]
        self.assertTrue(
            any("scheduler-core.degradation.json" in r and "ignored as invalid" in r for r in reasons),
            reasons,
        )

    def test_valid_degradation_without_valid_profile_retains_existing_diagnostic(self):
        (self.ws.root / "specs" / "_closure" / "scheduler-core.json").unlink()
        self.ws.write("specs/_closure/scheduler-core.degradation.json", valid_degradation())

        outcomes, workspace_findings = self.ws.outcomes()

        self.assertEqual(outcomes, [])
        reasons = [str(f) for f in workspace_findings]
        self.assertTrue(any("has a degradation record but no valid closure profile" in r for r in reasons))
        self.assertFalse(any("ignored as invalid" in r for r in reasons), reasons)

    def test_valid_profile_degradation_pair_has_no_invalid_artifact_finding(self):
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
        self.ws.write_accepted_record(record)

        outcome, workspace_findings = self.ws.cluster()

        self.assertEqual(workspace_findings, [])
        self.assertEqual(outcome.status, "degraded")

    def test_malformed_profile_never_reaches_gate_cluster(self):
        """A profile missing nearly every required field would crash
        gate_cluster() on its first dict access if it were ever passed
        through -- reaching outcomes == [] with no exception is itself
        the proof it never got there."""
        self.ws.write(
            "specs/_closure/scheduler-core.json",
            {"schema_version": "1.0", "cluster": "scheduler-core"},
        )
        outcomes, workspace_findings = self.ws.outcomes()
        self.assertEqual(outcomes, [])
        self.assertTrue(any("ignored as invalid" in str(f) for f in workspace_findings))


class CanonicalDegradationRecordLoaderTest(GateTestCase):
    """chainlink #99: gate-g14's own workspaces, read through the ONE
    validated degradation-record loader.

    #99 did not rewire gate-g14 (chainlink #100 does that); what it owes
    this gate is the guarantee the wiring will rely on -- the loader and
    validate-closure reach the same verdict about every record in a real
    gate workspace, so the release route has one notion of what a record
    is worth rather than one per command."""

    def load(self):
        return load_degradation_records(self.ws.root)

    def gate_refused_records(self) -> set[str]:
        return {
            Path(f.path).name
            for f in validate_closure.validate(self.ws.root)
            if Path(f.path).name.endswith(".degradation.json")
        }

    def assertLoaderAgreesWithTheGate(self):
        records = self.load()
        self.assertEqual(
            {p.name for p in records.invalid},
            self.gate_refused_records(),
            "the loader and validate-closure must refuse exactly the same records",
        )
        return records

    def test_a_mislocated_record_is_named_when_no_closure_directory_exists(self):
        """A gate workspace whose ONLY degradation record is mislocated,
        with no specs/_closure/ at all: the gate's own canonical scan
        finds nothing there, so a loader that only ever looked inside
        the canonical directory would report 'no degradation declared'
        on a workspace that holds one. validate-closure names it by
        location, and so must the loader -- the cross-check above is
        what would silently stop holding."""
        shutil.rmtree(self.ws.root / "specs" / "_closure")
        self.ws.write("docs/_closure/scheduler-core.degradation.json", valid_degradation())
        self.assertFalse((self.ws.root / "specs" / "_closure").exists())

        records = self.assertLoaderAgreesWithTheGate()
        self.assertEqual(records.valid, {})
        self.assertTrue(
            any(
                "not directly under the canonical directory" in r
                for r in records.reasons_for(self.ws.root / "docs/_closure/scheduler-core.degradation.json")
            )
        )

    def test_a_workspace_with_no_records_at_all_reports_neither_side(self):
        """The control for the case above: with no records anywhere, both
        halves stay empty, so a named refusal can never be confused with
        'nothing is declared'."""
        self.assertEqual((self.load().valid, self.load().invalid), ({}, {}))

    def degraded_pair(self, accept=False, **overrides):
        """A cluster with one genuinely failing closure condition and a
        record covering it -- the shape the gate's degraded-release route
        acts on. `accept=True` runs the record through the real approval
        and ruling path, so a test asserting a `degraded` outcome
        exercises the gate's release route rather than a bypass."""
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
        record.update(overrides)
        if accept:
            self.ws.write_accepted_record(record)
        else:
            self.ws.write(self.ws.DEGRADATION_RECORD, record)
        return record

    def test_a_consistent_pair_loads_as_the_one_valid_record(self):
        self.degraded_pair()
        records = self.assertLoaderAgreesWithTheGate()
        self.assertEqual(sorted(records.valid), ["scheduler-core"])
        self.assertEqual(
            records.record_for("scheduler-core")["failed_conditions"],
            ["protocol_class_all_pairwise"],
        )

    def test_the_loader_yields_exactly_the_pair_gate_g14_already_uses(self):
        """Same records, same refusals, so wiring gate-g14 onto this
        loader (chainlink #100) changes nothing about a clean workspace:
        the cluster is released as degraded under its accepted record,
        exactly as before."""
        self.degraded_pair(accept=True)
        outcome, _ = self.ws.cluster()
        self.assertEqual(outcome.status, "degraded")
        self.assertEqual(self.errors(outcome), [])
        self.assertEqual(
            [f.condition for f in outcome.findings if f.severity == "degraded"],
            ["protocol_class_all_pairwise"],
        )

        records = self.load()
        self.assertEqual(sorted(records.valid), ["scheduler-core"])
        self.assertEqual(records.invalid, {})

    def test_a_cg3_tracking_record_stays_valid_here_too(self):
        """chainlink #86's one exception is the loader's too: a CG3 record
        beside a `true` declaration is the tracking record plan.md §3
        requires, and gate-g14 -- not the loader -- decides staleness for
        that key from the closure's own records."""
        record = valid_degradation()
        record["failed_conditions"] = ["generic_callees_type_universal_or_creusot_owned"]
        record["ceiling"] = "per-instantiation"
        record["capability_gap"] = "CG3"
        self.ws.write(self.ws.DEGRADATION_RECORD, record)
        records = self.assertLoaderAgreesWithTheGate()
        self.assertTrue(records.is_valid("scheduler-core"))

    def test_a_malformed_failed_conditions_is_refused_with_its_own_reason(self):
        self.degraded_pair(failed_conditions="protocol_class_all_pairwise")
        records = self.assertLoaderAgreesWithTheGate()
        self.assertEqual(records.valid, {})
        reasons = records.reasons_for(self.ws.root / self.ws.DEGRADATION_RECORD)
        self.assertTrue(
            any("failed_conditions must be an array of closure-condition keys" in r for r in reasons),
            reasons,
        )

    def test_a_schema_invalid_record_is_refused_and_names_its_own_field(self):
        self.degraded_pair()
        record = json.loads((self.ws.root / self.ws.DEGRADATION_RECORD).read_text())
        record["verifier"] = "creusot-deductive-check"
        self.ws.write(self.ws.DEGRADATION_RECORD, record)
        records = self.assertLoaderAgreesWithTheGate()
        self.assertEqual(records.valid, {})
        reasons = records.reasons_for(self.ws.root / self.ws.DEGRADATION_RECORD)
        self.assertTrue(any("verifier" in r for r in reasons), reasons)

    def test_a_record_beside_an_invalid_profile_is_refused(self):
        self.degraded_pair()
        self.ws.patch(
            "specs/_closure/scheduler-core.json",
            lambda d: d["conditions"].update({"owning_verifier": "kani"}),  # G17: deductive + kani
        )
        records = self.assertLoaderAgreesWithTheGate()
        self.assertEqual(records.valid, {})
        self.assertTrue(
            any(
                "no closure profile beside it" in r
                for r in records.reasons_for(self.ws.root / self.ws.DEGRADATION_RECORD)
            ),
            "an unreadable profile declares nothing, so a record beside it can be checked "
            "against nothing",
        )

    def test_no_record_declared_is_distinguishable_from_a_refused_one(self):
        """The distinction #99's structured result exists for, on a real
        gate workspace: an empty valid index means either 'no degradation
        is declared' or 'one is declared and unusable', and only the
        invalid half tells them apart."""
        self.assertEqual((self.load().valid, self.load().invalid), ({}, {}))

        self.degraded_pair()
        self.ws.write(
            self.ws.DEGRADATION_RECORD,
            {**json.loads((self.ws.root / self.ws.DEGRADATION_RECORD).read_text()),
             "ceiling": "not-a-ceiling"},
        )
        records = self.load()
        self.assertEqual(records.valid, {})
        self.assertEqual(len(records.invalid), 1)


class GateHonorsCanonicalRecordValidationTest(GateTestCase):
    """chainlink #100: gate-g14 acts on records ONLY through chainlink
    #99's canonical validated loader, so a record `validate-closure`
    refuses can never release a cluster here.

    The pilot shape this closes is concrete. gate-g14 used to read a
    record out of the raw artifact index
    `load_cluster_artifacts_with_invalid()` returns, which carries
    per-file validation only -- none of the cross-artifact G17 pass, and
    no notion of a record filed in the wrong place at all. Two
    consequences, both reproduced below as the first two tests:

      * a record naming a stale excuse beside its own profile
        (validate-closure's G17) was accepted here: it downgraded the
        findings it named, the cluster printed `released under an
        accepted degradation record`, and gate-g14 exited 0 on a
        workspace `validate-closure` failed at 1;
      * a record filed outside specs/_closure/ was not refused but never
        looked at, so a workspace whose ONLY degradation record was
        mislocated read as `OK: 1 cluster(s) close` at exit 0.

    The load (`valid` is the only thing acted on) and the report
    (`invalid` is named, one error per record) are both load-bearing
    here, and the third test below asserts the exit status of the two
    commands agrees on every refusal -- which is what stops the second
    class of bug, a gate that quietly disagrees with its own validator.
    """

    def validate_closure_exit(self) -> int:
        """`pipeline.py validate-closure`'s own exit code, from its own
        CLI -- not an inspection of its findings, so the agreement
        asserted below is between the two commands an operator runs, not
        between this gate and a reading of the same list."""
        with redirect_stdout(io.StringIO()):
            return validate_closure.main([str(self.ws.root)])

    def assertBothCommandsRefuse(self, *expected_fragments):
        code, printed = self.ws.run()
        self.assertEqual(code, EXIT_BLOCKED, printed)
        self.assertEqual(self.validate_closure_exit(), 1, "validate-closure must fail too")
        self.assertNotIn(
            "released under an accepted degradation record",
            printed,
            "a record this gate may not act on must never be printed as an accepted release",
        )
        for fragment in expected_fragments:
            self.assertIn(fragment, printed)
        return printed

    def stale_excuse_pair(self) -> dict:
        """A cluster whose `protocol_class_all_pairwise` condition
        genuinely fails and is correctly recorded -- plus a SECOND
        excuse, `scc_wellfoundedness_discharged`, that its own profile
        declares as holding ('not-applicable'). The second is the stale
        excuse validate-closure refuses, and refusing the record for it
        means the FIRST -- legitimate -- excuse goes with it: one record
        is one excuse list, not two independently votable ones."""
        self.ws.write(
            "crates/scheduler/specs/_bridges/BR-SCHED-TQ-001.json",
            bridge_spec(protocol_class="non-pairwise"),
        )
        self.ws.patch(
            "specs/_closure/scheduler-core.json",
            lambda d: d["conditions"].update({"protocol_class_all_pairwise": False}),
        )
        record = valid_degradation()
        record["failed_conditions"] = [
            "protocol_class_all_pairwise",
            "scc_wellfoundedness_discharged",
        ]
        self.ws.write_accepted_record(record)
        return record

    def test_a_refused_record_is_never_acted_on_as_an_accepted_degradation(self):
        self.stale_excuse_pair()

        outcome, workspace_findings = self.ws.cluster()

        # The pilot's exact wrong answer: the record excused the very
        # finding it legitimately named, and the cluster was released.
        self.assertEqual(outcome.status, "blocked")
        self.assertEqual(self.degraded(outcome), [])
        self.assertTrue(
            any("protocol_class_all_pairwise" in e for e in self.errors(outcome)),
            "the condition the record named must come back unexcused as its own error",
        )
        self.assertTrue(
            any("stale excuse" in r for r in self._reasons(workspace_findings)),
            [str(f) for f in workspace_findings],
        )

    def test_a_refused_record_blocks_the_gate_through_the_cli(self):
        self.stale_excuse_pair()

        code, printed = self.ws.run()

        self.assertEqual(code, EXIT_BLOCKED)
        self.assertIn("FAIL:", printed)
        self.assertNotIn("released under an accepted degradation record", printed)

    def test_a_mislocated_record_is_named_rather_than_never_seen(self):
        """The other half of #99's `misplaced` map, and the case where
        gate-g14 and validate-closure disagreed most loudly: a record is
        only a record under specs/_closure/, and one filed elsewhere is
        refused by location -- so it is named at the path it was FOUND
        at. `report_outcomes()` prints workspace findings before the
        cluster lines, which is why this assertion looks at the whole
        printed output rather than at the outcome's own findings."""
        self.ws.write("docs/_closure/scheduler-core.degradation.json", valid_degradation())

        printed = self.assertBothCommandsRefuse(
            "not directly under the canonical directory",
        )
        self.assertIn("docs/_closure/scheduler-core.degradation.json", printed)

    def test_a_mislocated_record_still_blocks_beside_a_usable_one(self):
        """A refused record cannot be outvoted by a second, good one:
        the gate's own answer is per cluster, and one cluster may hold
        exactly one record, so a second one anywhere in the workspace is
        either the same record filed wrong or nothing this gate can
        validate."""
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
        self.ws.write_accepted_record(record)
        self.ws.write("docs/_closure/scheduler-core.degradation.json", valid_degradation())

        self.assertBothCommandsRefuse("not directly under the canonical directory")

    def test_the_two_commands_agree_on_every_refused_record(self):
        """Exit-status agreement, over every way this fixture can make a
        record unusable -- the property #100 is really asserting. Each
        case is built on a workspace whose ONLY other fault would leave
        it releasing at exit 0, so a gate that ignored the refusal could
        not pass this by accident.

        The CG3 exception is deliberately absent from this table and is
        covered separately below: a generic_callees record beside a
        `true` declaration is NOT a refusal, because no profile/record
        comparison can tell an unverifiable declaration from a verified
        one."""
        def schema_invalid(ws):
            record = valid_degradation()
            record["failed_conditions"] = ["protocol_class_all_pairwise"]
            record["verifier"] = "creusot-deductive-check"  # G1a: additionalProperties: false
            ws.write(ws.DEGRADATION_RECORD, record)

        def failed_conditions_is_a_string(ws):
            record = valid_degradation()
            record["failed_conditions"] = "protocol_class_all_pairwise"
            ws.write(ws.DEGRADATION_RECORD, record)

        def failed_conditions_names_an_unknown_key(ws):
            record = valid_degradation()
            record["failed_conditions"] = ["looks_fine_to_me"]
            ws.write(ws.DEGRADATION_RECORD, record)

        def misnamed_cluster(ws):
            record = valid_degradation()
            record["cluster"] = "not-the-filename"
            ws.write(ws.DEGRADATION_RECORD, record)

        def no_valid_profile_beside_it(ws):
            record = valid_degradation()
            record["failed_conditions"] = ["protocol_class_all_pairwise"]
            ws.write(ws.DEGRADATION_RECORD, record)
            (ws.root / "specs/_closure/scheduler-core.json").unlink()

        cases = {
            "a stale excuse beside its own profile":
                lambda ws: self.stale_excuse_pair(),
            "a schema-invalid record": schema_invalid,
            "a failed_conditions string": failed_conditions_is_a_string,
            "an unknown condition key": failed_conditions_names_an_unknown_key,
            "a record whose cluster disagrees with its filename": misnamed_cluster,
            "a record with no valid profile beside it": no_valid_profile_beside_it,
        }
        for label, build in cases.items():
            with self.subTest(condition=label):
                self.setUp()
                build(self.ws)
                code, printed = self.ws.run()
                self.assertEqual(code, EXIT_BLOCKED, printed)
                self.assertEqual(self.validate_closure_exit(), 1, label)
                self.assertNotIn("released under an accepted degradation record", printed)

    def test_a_usable_record_still_releases_as_degraded(self):
        """The control for the whole class: wiring the gate onto the
        loader changes nothing about a workspace whose record
        `validate-closure` accepts. If this ever regresses, the fix has
        over-refused rather than under-refused."""
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
        self.ws.write_accepted_record(record)

        outcome, workspace_findings = self.ws.cluster()

        self.assertEqual(workspace_findings, [])
        self.assertEqual(outcome.status, "degraded")
        self.assertEqual(self.errors(outcome), [])
        self.assertEqual(
            [f.condition for f in outcome.findings if f.severity == "degraded"],
            ["protocol_class_all_pairwise"],
        )
        code, printed = self.ws.run()
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(self.validate_closure_exit(), 0)
        self.assertIn("released under an accepted degradation record", printed)

    def _reasons(self, findings) -> list[str]:
        return [str(f) for f in findings]


class GateOwnsTheCg3StalenessExceptionTest(GateTestCase):
    """The ONE place gate-g14 may refuse a record `validate-closure`
    accepts (chainlink #86, preserved through #99 and #100).

    `generic_callees_type_universal_or_creusot_owned` is the condition
    no profile/record comparison can settle in either direction: whether
    a callee is generic is type information nothing here carries. So the
    loader deliberately does NOT treat a CG3 record beside a `true`
    declaration as a stale excuse -- it is the tracking record plan.md
    §3 demands of a capability gap -- and staleness for that one key is
    decided HERE, from closure evidence, the moment the workspace's own
    artifacts verify the condition.

    These tests exist so the exception stays explicit rather than
    becoming an accident: if #100 had made the gate refuse CG3 records
    the way it refuses every other one, the check would pass and the
    pipeline would lose the ability to release a tracked CG3 gap at all.
    """

    CG3 = "generic_callees_type_universal_or_creusot_owned"

    def cg3_record(self) -> dict:
        record = valid_degradation()
        record["failed_conditions"] = [self.CG3]
        record["ceiling"] = "per-instantiation"
        record["capability_gap"] = "CG3"
        record["tracking_issue"] = "chainlink:86"
        return record

    def test_a_cg3_record_beside_a_true_declaration_is_valid_to_the_loader(self):
        """The loader's own half of the exception: `valid`, not
        `invalid`, so the gate below gets the chance to decide this one
        key itself instead of being told what to think of it."""
        self.ws.write(self.ws.DEGRADATION_RECORD, self.cg3_record())

        records = load_degradation_records(self.ws.root)
        self.assertTrue(records.is_valid("scheduler-core"), records.reasons_for(
            self.ws.root / self.ws.DEGRADATION_RECORD
        ))
        self.assertEqual(records.invalid, {})

    def test_a_tracked_cg3_gap_still_releases_as_degraded(self):
        """And the gate's half: the record reaches apply_degradation()
        through the loader's `valid` index and releases the cluster
        under its ceiling. Before #100 this path went through the raw
        artifact index; it must go on working afterwards, or the loader
        wiring would have silently retired #86's only remedy."""
        make_closure_kani_only(self.ws)
        self.ws.write_accepted_record(self.cg3_record())

        outcome, workspace_findings = self.ws.cluster()

        self.assertEqual(workspace_findings, [])
        self.assertEqual(self.errors(outcome), [])
        self.assertEqual(outcome.status, "degraded")
        self.assertTrue(
            any("accepted as degradation under ceiling 'per-instantiation' (chainlink:86)" in d
                for d in self.degraded(outcome)),
            [str(f) for f in outcome.findings],
        )
        code, printed = self.ws.run()
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(validate_closure.main([str(self.ws.root)]), 0)

    def test_the_exception_is_the_only_way_the_two_commands_disagree(self):
        """The direction of the exception, stated once so it cannot be
        quietly widened: on a workspace whose artifacts VERIFY CG3, the
        accepted CG3 record is valid to `validate-closure` (exit 0) and
        is refused by gate-g14 as stale (exit 1). This is the single
        documented case of the gate being stricter, and the assertion is
        about the disagreement existing and being about CG3 -- not about
        it being absent, which `test_the_two_commands_agree_on_every_
        refused_record` already covers for every other record."""
        self.ws.write_accepted_record(self.cg3_record())

        records = load_degradation_records(self.ws.root)
        self.assertEqual(records.invalid, {})
        with redirect_stdout(io.StringIO()):
            self.assertEqual(validate_closure.main([str(self.ws.root)]), 0)

        outcome, _ = self.ws.cluster()
        self.assertEqual(outcome.status, "blocked")
        self.assertTrue(
            any("stale tracking record" in e for e in self.errors(outcome)),
            self.errors(outcome),
        )
        code, printed = self.ws.run()
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertNotIn("released under an accepted degradation record", printed)


class DegradationProvenanceTest(GateTestCase):
    """chainlink #97, end to end through gate-g14: a degradation record
    releases a cluster only after the shared #96 checks pass -- its
    `review` block matches an entry in the approval audit log, and a
    `ratified` human ruling covers the record's exact current bytes.

    Every leg here is one the #94 pilot measured at exit 0 with the
    message "released under an accepted degradation record": a
    fabricated reviewer, no human_rulings.jsonl at all, a ruling over a
    hash the record has since outgrown, and a rejected ruling. The
    fixture failure they share is a non-pairwise bridge, so the cluster
    has a genuinely failing closure condition for the record to excuse
    -- i.e. these test the release ROUTE, not a cluster that was going
    to block anyway."""

    def make_pairwise_failure(self, condition="protocol_class_all_pairwise"):
        """The failing condition every record below is written to name,
        so a released cluster and a blocked one differ ONLY by whether
        the record was accepted."""
        self.ws.write(
            "crates/scheduler/specs/_bridges/BR-SCHED-TQ-001.json",
            bridge_spec(protocol_class="non-pairwise"),
        )
        self.ws.patch(
            "specs/_closure/scheduler-core.json",
            lambda d: d["conditions"].update({condition: False}),
        )
        return condition

    def record_naming(self, condition, **overrides) -> dict:
        record = valid_degradation()
        record["failed_conditions"] = [condition]
        record.update(overrides)
        return record

    def assertRefusedAndUnexcused(self, outcome, *expected_fragments):
        """The refusal has two halves, and a gate that only does the
        first is still wrong: the provenance gap must be named, AND the
        record must excuse nothing -- the condition finding it names has
        to survive as an ERROR rather than be downgraded to `degraded`.
        Otherwise the gate would print a cluster as released under an
        accepted degradation record while also printing that the record
        was never accepted."""
        errors = self.errors(outcome)
        for fragment in expected_fragments:
            self.assertTrue(
                any(fragment in e for e in errors),
                f"expected {fragment!r} among the errors; got: {errors}",
            )
        self.assertEqual(self.degraded(outcome), [], "an unaccepted record must excuse nothing")
        self.assertEqual(outcome.status, "blocked")

    # -- leg 1: the issue's fabricated reviewer ----------------------------

    def test_a_fabricated_reviewer_blocks_instead_of_releasing_the_cluster(self):
        """#94's exact record: review names someone who never reviewed
        anything, tracking issue names an issue nobody filed. The review
        block is schema-valid -- reviewer non-empty, reviewed_at a real
        date -- and nothing on disk distinguishes it from an approved
        one, which is exactly why it cannot be trusted."""
        condition = self.make_pairwise_failure()
        self.ws.write(
            self.ws.DEGRADATION_RECORD,
            self.record_naming(
                condition,
                affected_edges=["invented-edge-that-does-not-exist"],
                tracking_issue="chainlink:999999",
                review={"reviewer": "nobody-reviewed-this", "reviewed_at": "1999-01-01"},
            ),
        )
        # An audit log that exists but holds nothing, and a ruling that
        # DOES cover the record: so the only thing wrong is the
        # fabricated review, and the record must be refused for that
        # reason alone rather than for the absence of a log.
        log = self.ws.root / "ci" / "results" / "review_log.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("")
        self.ws.rule_on_record()
        outcome, _ = self.ws.cluster()
        self.assertRefusedAndUnexcused(outcome, "nobody-reviewed-this", "no matching approval entry")

    def test_an_empty_review_log_blocks_a_record_whose_block_merely_exists(self):
        """The log exists but proves nothing. Fail closed, exactly as
        review_provenance_gaps() does for a promotion's accepted set."""
        condition = self.make_pairwise_failure()
        self.ws.write(self.ws.DEGRADATION_RECORD, self.record_naming(condition))
        (self.ws.root / "ci" / "results" / "review_log.jsonl").write_text("")
        self.ws.rule_on_record()
        outcome, _ = self.ws.cluster()
        self.assertRefusedAndUnexcused(outcome, "no matching approval entry")

    def test_a_reviewer_mismatch_against_a_real_approval_entry_blocks(self):
        """Not a forgery of the log -- a REAL approval entry for a
        different human. The record's own block still has to match one."""
        condition = self.make_pairwise_failure()
        self.ws.write(self.ws.DEGRADATION_RECORD, self.record_naming(condition))
        self.ws.approve_record(review={"reviewer": "bob", "reviewed_at": REVIEW["reviewed_at"]})
        self.ws.rule_on_record()
        outcome, _ = self.ws.cluster()
        self.assertRefusedAndUnexcused(outcome, "no matching approval entry")

    # -- leg 2: the issue's missing human_rulings.jsonl --------------------

    def test_no_human_rulings_log_at_all_blocks(self):
        """#94: no human_rulings.jsonl exists in the workspace and the
        release succeeds anyway. Provenance alone cannot cover this --
        an unattended agent can run `approve` itself -- which is why
        accept-promotion added the ruling gate and why this gate needs
        it too."""
        condition = self.make_pairwise_failure()
        self.ws.write(self.ws.DEGRADATION_RECORD, self.record_naming(condition))
        self.ws.approve_record()
        outcome, _ = self.ws.cluster()
        self.assertRefusedAndUnexcused(outcome, "no human ruling log")
        self.assertFalse((self.ws.root / "ci" / "results" / "human_rulings.jsonl").exists())

    def test_an_unruled_artifact_in_an_existing_log_blocks(self):
        """The log exists and holds a ruling -- for something else. A
        ruling is an event over a named set, not a workspace-wide mood."""
        condition = self.make_pairwise_failure()
        self.ws.write(self.ws.DEGRADATION_RECORD, self.record_naming(condition))
        self.ws.approve_record()
        log = self.ws.root / "ci" / "results" / "human_rulings.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("")
        outcome, _ = self.ws.cluster()
        self.assertRefusedAndUnexcused(outcome, "no human ruling recorded for this artifact")

    # -- leg 3: the issue's wrong hash -------------------------------------

    def test_a_ruling_over_a_hash_the_record_outgrew_blocks(self):
        """Ratify, then edit the record by one byte -- here the tracking
        issue, which is the field the pilot report called regex-only.
        #82's hash-pinning already guaranteed this for a promotion's
        artifact set; #97 is what makes it load-bearing here."""
        condition = self.make_pairwise_failure()
        self.ws.write(self.ws.DEGRADATION_RECORD, self.record_naming(condition))
        self.ws.approve_record()
        self.ws.rule_on_record()
        self.ws.patch(
            self.ws.DEGRADATION_RECORD,
            lambda d: d.update({"tracking_issue": "chainlink:999999"}),
        )
        outcome, _ = self.ws.cluster()
        self.assertRefusedAndUnexcused(outcome, "different version of this artifact")

    def test_a_weakened_ceiling_after_ratification_blocks(self):
        """The pilot's own field table: ceiling is enforced as an enum
        only, so `documented` (the weakest value -- "we wrote it down")
        released identically to human-risk-acceptance. With the ruling
        pinned to the record's bytes, downgrading the ceiling after the
        fact is exactly the edit the ruling stops covering."""
        condition = self.make_pairwise_failure()
        self.ws.write(
            self.ws.DEGRADATION_RECORD,
            self.record_naming(condition, ceiling="human-risk-acceptance"),
        )
        self.ws.approve_record()
        self.ws.rule_on_record()
        self.ws.patch(self.ws.DEGRADATION_RECORD, lambda d: d.update({"ceiling": "documented"}))
        outcome, _ = self.ws.cluster()
        self.assertRefusedAndUnexcused(outcome, "different version of this artifact")

    def test_a_rejected_ruling_blocks(self):
        condition = self.make_pairwise_failure()
        self.ws.write_accepted_record(self.record_naming(condition), verdict="rejected")
        outcome, _ = self.ws.cluster()
        self.assertRefusedAndUnexcused(outcome, "REJECTED")

    # -- leg 4: the valid ruling keeps the existing release behaviour ------

    def test_a_provenanced_and_ratified_record_still_releases_as_degraded(self):
        """The control. Nothing about the release ROUTE changed: a valid
        record still downgrades exactly the conditions it names, still
        exits 0, and still never says closed. G17 recomputation and the
        ceiling/tracking-issue wording are untouched."""
        condition = self.make_pairwise_failure()
        self.ws.write_accepted_record(self.record_naming(condition))
        outcome, workspace_findings = self.ws.cluster()
        self.assertEqual(workspace_findings, [])
        self.assertEqual(self.errors(outcome), [])
        self.assertEqual(outcome.status, "degraded")
        self.assertTrue(
            any("accepted as degradation under ceiling 'harness-tested' (chainlink:713)" in d
                for d in self.degraded(outcome)),
            [str(f) for f in outcome.findings],
        )
        self.assertIn("does NOT close", outcome.summary())
        self.assertNotIn("closes:", outcome.summary())

    def test_a_valid_record_exits_zero_through_the_cli(self):
        condition = self.make_pairwise_failure()
        self.ws.write_accepted_record(self.record_naming(condition))
        code, printed = self.ws.run()
        self.assertEqual(code, EXIT_OK)
        self.assertIn("released under an accepted degradation record", printed)

    def test_the_gate_refusal_exits_non_zero_through_the_cli(self):
        """#94 measured exit 0 with "released under an accepted
        degradation record". The same record, unreviewed, must exit
        blocked and must never print that sentence."""
        condition = self.make_pairwise_failure()
        self.ws.write(
            self.ws.DEGRADATION_RECORD,
            self.record_naming(
                condition,
                review={"reviewer": "nobody-reviewed-this", "reviewed_at": "1999-01-01"},
            ),
        )
        code, printed = self.ws.run()
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertNotIn("released under an accepted degradation record", printed)
        self.assertIn("no approval audit log", printed)
        self.assertIn("no human ruling log", printed)

    def test_the_real_ruling_writer_produces_a_record_this_gate_accepts(self):
        """The control that keeps the fixture honest. Every other leg
        here hand-writes the two log entries, which would still pass if
        this gate read a DIFFERENT log than the tools that write them --
        the cwd-relative bug #45 and #82 already paid for twice. So one
        leg goes through generate_promotion_receipt.record_ruling()
        itself and an audit entry shaped exactly as
        review_checkpoint.approve() writes it, and asserts the release
        still happens. If the gate's two log paths ever drift from the
        sanctioned writers', this is the test that fails."""
        condition = self.make_pairwise_failure()
        record = self.record_naming(condition)
        self.ws.write(self.ws.DEGRADATION_RECORD, record)
        self.ws.approve_record()
        entry = record_ruling(
            self.ws.root,
            [self.ws.DEGRADATION_RECORD],
            reviewer="alice",
            verdict="ratified",
            descriptor_path=self.ws.root / "project-descriptor.json",
            ruled_at="2026-09-05",
        )
        self.assertEqual(
            [a["path"] for a in entry["artifacts"]], [self.ws.DEGRADATION_RECORD]
        )
        outcome, _ = self.ws.cluster()
        self.assertEqual(self.errors(outcome), [])
        self.assertEqual(outcome.status, "degraded")

    def test_the_gates_log_paths_are_the_ones_the_sanctioned_writers_use(self):
        """The drift guard, stated as path equality rather than inferred
        from behaviour. `approve` appends to
        pipeline._workspace_review_log, `record-ruling` appends wherever
        its own default resolves, and gate-g14 reads two paths -- all
        three must be the same two files, or this gate would be reading
        a log nobody writes (the cwd-relative bug #45 and #82 already
        paid for twice). Asserted against the real writer's own default,
        not against a copy of its path string."""
        # The review log: cmd_approve's, the shared resolver's, and the
        # file itself.
        self.assertEqual(
            pipeline._workspace_review_log(self.ws.root),
            review_checkpoint.review_log_path(self.ws.root),
        )
        self.assertEqual(
            review_log_path(self.ws.root),
            self.ws.root / "ci" / "results" / "review_log.jsonl",
        )
        # The ruling log: record_ruling() called with NO ruling_log
        # override writes to exactly the path gate-g14 reads.
        record_ruling(
            self.ws.root, ["project-descriptor.json"],
            reviewer="alice", verdict="ratified",
            descriptor_path=self.ws.root / "project-descriptor.json",
        )
        self.assertEqual(
            ruling_log_path(self.ws.root),
            self.ws.root / "ci" / "results" / "human_rulings.jsonl",
        )
        self.assertTrue(ruling_log_path(self.ws.root).is_file())

    # -- the gate must not have become the thing it replaced --------------

    def test_the_provenance_gap_cannot_be_excused_by_the_record_naming_it(self):
        """`failed_conditions` is a closed vocabulary of closure
        CONDITION keys, so there is no spelling of "this record was never
        reviewed" a record could put in its own list -- but that is worth
        proving rather than assuming, because the whole point of running
        the check BEFORE apply_degradation() is that no vocabulary can
        reach it."""
        condition = self.make_pairwise_failure()
        record = self.record_naming(condition)
        record["failed_conditions"] = [condition, "review_provenance", "degradation_record_gaps"]
        self.ws.write(self.ws.DEGRADATION_RECORD, record)
        outcome, _ = self.ws.cluster()
        self.assertEqual(self.degraded(outcome), [])
        self.assertEqual(outcome.status, "blocked")

    def test_a_workspace_with_no_degradation_record_is_unaffected(self):
        """The gate must not have created a new way to fail: the record is
        the only artifact these two logs are consulted about, so a
        workspace with no record closes exactly as it did before #97."""
        outcome, workspace_findings = self.ws.cluster()
        self.assertEqual(self.errors(outcome), [])
        self.assertEqual(workspace_findings, [])
        self.assertEqual(outcome.status, "closes")

    def test_g17_recomputation_still_refuses_a_mis_declared_bit_beside_a_valid_record(self):
        """The provenance gate must not have weakened the recomputation
        it sits in front of: an accepted record covering a condition
        still cannot excuse a profile bit that contradicts the closure
        (no degradation record ever could)."""
        condition = self.make_pairwise_failure("single_verifier_system")
        self.ws.patch(
            "ci/manifest/WP-C.json",
            lambda d: d["definition_of_done"].update({
                "required_guarantees": [{
                    "obligation_id": "Scheduler.C001",
                    "assume_during_check": True,
                    "required_assurance": required_assurance(),
                }]
            }),
        )
        self.ws.write_accepted_record(self.record_naming(condition))
        outcome, _ = self.ws.cluster()
        self.assertTrue(
            any("mutual satisfaction is not soundness" in e for e in self.errors(outcome)),
            [str(f) for f in outcome.findings],
        )
        self.assertEqual(outcome.status, "blocked")


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

    def write_installation_manifest(self) -> None:
        """`ligature init`'s ownership manifest, at its fixed path
        ci/manifest/installation.json -- an unrelated schema that shares
        the directory and filename suffix with work-package manifests
        (chainlink #66)."""
        self.ws.write("ci/manifest/installation.json", {
            "manifest_schema_version": "1.0",
            "adjudicator": {
                "kind": "zipapp",
                "version": "1.0.0",
                "content_hash": "sha256:" + "0" * 64,
            },
            "descriptor_path": "project-descriptor.json",
            "files": [
                {
                    "path": "project-descriptor.json",
                    "ownership": "user",
                    "base_hash": "sha256:" + "1" * 64,
                    "expected_hash": "sha256:" + "1" * 64,
                    "authority_hash": None,
                }
            ],
            "gate_hashes": {"gate-g14": "sha256:" + "3" * 64},
            "installed_product_version": "1.0.0",
        })

    def test_the_discriminator_recognizes_an_ownership_manifest_not_a_work_package_one(self):
        self.assertFalse(looks_like_work_package_manifest({
            "manifest_schema_version": "1.0",
            "adjudicator": {},
            "files": [],
            "gate_hashes": {},
            "installed_product_version": "1.0.0",
        }))
        self.assertTrue(looks_like_work_package_manifest({"work_package": "WP-A"}))

    def test_the_installed_ownership_manifest_is_not_read_as_a_work_package_manifest(self):
        self.write_installation_manifest()
        manifests, findings = load_manifests(self.ws.root)
        self.assertEqual(sorted(manifests), ["WP-A", "WP-B", "WP-C"])
        self.assertEqual(findings, [])

    def test_the_ownership_manifest_does_not_make_an_otherwise_closing_cluster_fail(self):
        self.write_installation_manifest()
        code, output = self.ws.run()
        self.assertEqual(code, EXIT_OK, output)
        outcome, workspace_findings = self.ws.cluster()
        self.assertEqual([str(f) for f in workspace_findings], [])
        self.assertEqual(outcome.status, "closes")

    def test_a_work_package_manifest_missing_the_schema_key_still_fails_closed(self):
        # `schema` is the required top-level key installation.json also
        # lacks; discriminating on it would silently swallow this
        # genuinely broken work-package manifest, so the discriminator
        # must not key on it alone.
        self.write_installation_manifest()
        self.ws.patch("ci/manifest/WP-C.json", lambda d: d.pop("schema"))
        _, workspace_findings = self.ws.cluster()
        self.assertTrue(
            any("WP-C.json" in str(f) and "not schema-valid" in str(f) for f in workspace_findings)
        )


if __name__ == "__main__":
    unittest.main()
