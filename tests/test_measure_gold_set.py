"""Gold-set measurement (chainlink #26).

The scenario every test below builds is the one that makes the issue's
point: an interaction set that proposes an edge the human rejects
(precision < 1), misses an edge C_static did see (missed-but-proposed),
and misses an edge nothing could see (omission). "R2 passed" says
nothing about any of the three.
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from measure_gold_set import (  # noqa: E402
    EXIT_BLOCKED,
    EXIT_OK,
    INDEPENDENT_SOURCES,
    c_static_edges,
    interaction_edges,
    load_measurement_validator,
    measure_workspace,
    measurement_dir_for,
    report,
)
from test_validate_gold_set import gold_set  # noqa: E402

REVIEW = {"reviewer": "alice", "reviewed_at": "2026-09-04"}


def interaction(interaction_id, caller, callee) -> dict:
    return {
        "schema_version": "1.0",
        "interaction_id": interaction_id,
        "caller": {"concept": caller[0], "method": caller[1]},
        "callee": {"concept": callee[0], "method": callee[1]},
        "edge_class": ["stateful"],
        "eligibility": "boundary-required",
        "rationale": "declared for the measurement fixture",
        "protocol_class": "pairwise",
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
            "config_scope": {"target": "x86_64-unknown-linux-gnu", "features": [], "cfg": []},
        },
        "review": dict(REVIEW),
    }


def callsite_report(edges) -> dict:
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
        "callsites": [
            {
                "callsite_id": f"CS-{caller[0].upper()}-{caller[1].upper()}-{index + 1:03d}",
                "call_class": "definite-direct-call",
                "caller": {"concept": caller[0], "method": caller[1]},
                "callee": {"concept": callee[0], "method": callee[1]},
                "source": {
                    "path": "crates/scheduler/src/lib.rs",
                    "symbol": f"{caller[0]}::{caller[1]}",
                    "line": 10 + index,
                    "syntax_hash": "sha256:" + "a" * 64,
                },
                "risk_tier_source": "not-applicable",
            }
            for index, (caller, callee) in enumerate(edges)
        ],
        "callsite_coverage": {"discovered": len(edges), "resolved": len(edges), "unresolved": 0},
    }


class MeasurementTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        self.descriptor = json.loads(
            (ROOT / "schemas" / "examples" / "project-descriptor.greenfield.example.json").read_text()
        )
        self.descriptor["crates"] = [
            {"crate_dir": "crates/scheduler", "contracts_crate": "contracts", "specs_search_root": "crates"}
        ]
        self.write("project-descriptor.json", self.descriptor)
        self.write("specs/_gold_sets/scheduler-core.json", gold_set())
        # I proposes one gold edge and one edge the human rejects
        self.write(
            "crates/scheduler/specs/_interactions/scheduler_dispatch__to__task_queue_pop_ready.json",
            interaction(
                "scheduler_dispatch__to__task_queue_pop_ready",
                ("Scheduler", "dispatch"), ("TaskQueue", "pop_ready"),
            ),
        )
        self.write(
            "crates/scheduler/specs/_interactions/scheduler_dispatch__to__clock_now.json",
            interaction("scheduler_dispatch__to__clock_now", ("Scheduler", "dispatch"), ("Clock", "now")),
        )
        # C_static sees the push_back edge I missed
        self.write(
            "ci/results/c_static/crates_scheduler.json",
            callsite_report([(("Scheduler", "dispatch"), ("TaskQueue", "push_back"))]),
        )

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, relative: str, data: dict) -> Path:
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
        return path

    def measure(self, write=True):
        return measure_workspace(self.workspace, self.descriptor, write=write)

    def single(self):
        measurements, findings = self.measure()
        self.assertEqual(len(measurements), 1, [str(f) for f in findings])
        return measurements[0], findings


class MetricsTest(MeasurementTestCase):
    def test_the_three_metrics(self):
        measurement, _ = self.single()
        counts = measurement["counts"]
        self.assertEqual(counts["gold_edges"], 3)
        self.assertEqual(counts["proposed_edges"], 2)
        self.assertEqual(counts["true_positives"], 1)
        self.assertEqual(counts["false_positives"], 1)
        self.assertEqual(counts["false_negatives"], 2)
        self.assertEqual(counts["omitted"], 1)
        self.assertEqual(counts["missed_but_proposed"], 1)
        self.assertEqual(measurement["metrics"]["precision"], 0.5)
        self.assertEqual(measurement["metrics"]["recall"], round(1 / 3, 4))

    def test_the_omitted_edge_is_the_one_nothing_could_see(self):
        measurement, _ = self.single()
        omitted = measurement["omitted_edges"]
        self.assertEqual(len(omitted), 1)
        self.assertEqual(omitted[0]["callee"], {"concept": "TaskQueue", "method": "drain"})
        self.assertEqual(omitted[0]["proposed_by"], ["human-promotion"])
        self.assertIn("trait object", omitted[0]["rationale"])

    def test_missed_but_proposed_is_a_different_finding_from_omission(self):
        measurement, _ = self.single()
        missed = measurement["missed_but_proposed_edges"]
        self.assertEqual(len(missed), 1)
        self.assertEqual(missed[0]["callee"], {"concept": "TaskQueue", "method": "push_back"})
        self.assertIn("c-static-extraction", missed[0]["proposed_by"])

    def test_a_proposal_the_human_rejects_is_reported_as_such(self):
        measurement, _ = self.single()
        self.assertEqual(
            [e["callee"] for e in measurement["unmatched_proposals"]], [{"concept": "Clock", "method": "now"}]
        )

    def test_r1_direction_is_not_counted_here(self):
        # C_static proposes push_back and I does not have it: that is R1's
        # "realized call absent from I" (gate-r1-g16), and it must not
        # appear among this measurement's unmatched proposals.
        measurement, _ = self.single()
        for entry in measurement["unmatched_proposals"]:
            self.assertNotEqual(entry["callee"], {"concept": "TaskQueue", "method": "push_back"})

    def test_out_of_scope_proposals_are_not_scored(self):
        self.write(
            "crates/scheduler/specs/_interactions/clock_now__to__task_queue_pop_ready.json",
            interaction(
                "clock_now__to__task_queue_pop_ready", ("Clock", "now"), ("TaskQueue", "pop_ready")
            ),
        )
        measurement, _ = self.single()
        # Clock is not an examined concept, so the new edge changes nothing
        self.assertEqual(measurement["counts"]["proposed_edges"], 2)
        self.assertEqual(measurement["metrics"]["precision"], 0.5)

    def test_a_perfect_interaction_set_scores_one(self):
        for name, callee in (
            ("scheduler_dispatch__to__task_queue_push_back", ("TaskQueue", "push_back")),
            ("scheduler_shutdown__to__task_queue_drain", ("TaskQueue", "drain")),
        ):
            caller = ("Scheduler", "shutdown" if "shutdown" in name else "dispatch")
            self.write(
                f"crates/scheduler/specs/_interactions/{name}.json", interaction(name, caller, callee)
            )
        (self.workspace / "crates/scheduler/specs/_interactions/scheduler_dispatch__to__clock_now.json").unlink()
        measurement, _ = self.single()
        self.assertEqual(measurement["metrics"]["precision"], 1.0)
        self.assertEqual(measurement["metrics"]["recall"], 1.0)
        self.assertEqual(measurement["counts"]["omitted"], 0)


class SourceRegisterTest(MeasurementTestCase):
    def test_every_candidate_source_plan_names_is_listed_with_its_real_status(self):
        measurement, _ = self.single()
        by_source = {entry["source"]: entry for entry in measurement["sources"]}
        for source in INDEPENDENT_SOURCES:
            self.assertIn(source, by_source)
        self.assertEqual(by_source["c-static-extraction"]["status"], "available")
        self.assertEqual(by_source["s-graph"]["status"], "not-built")
        self.assertEqual(by_source["critic-pass"]["status"], "not-built")
        self.assertEqual(by_source["mode-p-source-extraction"]["status"], "not-applicable-to-track")
        # It is unbuilt in this codebase, not merely irrelevant here --
        # the ModePPilotClusterTest below pins the OTHER half of this
        # status (a real Mode P workspace still reports not-built, never
        # available, since chainlink #5 does not exist either).
        self.assertNotEqual(by_source["mode-p-source-extraction"]["status"], "available")

    def test_the_interaction_set_is_listed_and_marked_not_independent(self):
        measurement, _ = self.single()
        entry = next(e for e in measurement["sources"] if e["source"] == "interaction-set")
        self.assertFalse(entry["independent"])

    def test_the_gold_set_does_not_corroborate_itself(self):
        # human-promotion proposes every gold edge; if it counted as
        # corroboration, omission would be structurally zero.
        measurement, _ = self.single()
        entry = next(e for e in measurement["sources"] if e["source"] == "human-promotion")
        self.assertFalse(entry["independent"])
        self.assertEqual(entry["proposed"], 3)
        self.assertEqual(measurement["counts"]["omitted"], 1)

    def test_without_a_c_static_report_everything_missing_is_an_omission(self):
        (self.workspace / "ci/results/c_static/crates_scheduler.json").unlink()
        measurement, _ = self.single()
        entry = next(e for e in measurement["sources"] if e["source"] == "c-static-extraction")
        self.assertEqual(entry["status"], "not-built")
        self.assertEqual(measurement["counts"]["omitted"], 2)
        self.assertEqual(measurement["counts"]["missed_but_proposed"], 0)


MODE_P_GOLD_SET_FIXTURE = (
    ROOT / "tests" / "fixtures" / "gold_sets" / "valid" / "specs" / "_gold_sets" / "scheduler-core-port.json"
)


def port_descriptor() -> dict:
    """A schema-valid `mode: port` descriptor -- the shape
    schemas/examples/project-descriptor.port.example.json pins, with
    `crates` overridden to match this test module's own fixture data.
    `port_source` is required by the schema's own if/then whenever
    `mode == "port"`."""
    descriptor = json.loads(
        (ROOT / "schemas" / "examples" / "project-descriptor.port.example.json").read_text()
    )
    descriptor["crates"] = [
        {"crate_dir": "crates/scheduler", "contracts_crate": "contracts", "specs_search_root": "crates"}
    ]
    return descriptor


class ModePPilotClusterTest(unittest.TestCase):
    """The issue's own acceptance criterion, read literally: "at least one
    pilot cluster per test track (C2)". plan.md §11 names two test
    tracks -- Mode R (greenfield) and Mode P (a foreign-language source
    port validated by differential testing, chainlink #5) -- and the
    Mode R pilot alone does not satisfy "per test track".

    This workspace's own project descriptor is greenfield, and chainlink
    #5 (the Mode P port track itself) is not built in this codebase --
    so, matching this repo's own precedent for an unbuilt-but-necessary
    piece (tests/fixtures/callsites/'s never-compiled Rust, chainlink
    #24; tests/fixtures/bridges/verifier/fake_verifier.py, chainlink
    #47), this is a fixture pilot cluster: a real `mode: port` project
    descriptor, a real reviewed Mode P gold set
    (scheduler-core-port.json), and real interaction/C_static data,
    proving scripts/measure_gold_set.py measures a Mode P cluster
    correctly -- track-derivation, the track-match rejection, and the
    mode-p-source-extraction status all included. It does not implement
    Mode P itself; it proves the measurement tool is track-agnostic
    where it should be and track-aware exactly where plan.md §11 says it
    must be."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        self.descriptor = port_descriptor()
        self.write("project-descriptor.json", self.descriptor)
        self.write(
            "specs/_gold_sets/scheduler-core-port.json",
            json.loads(MODE_P_GOLD_SET_FIXTURE.read_text()),
        )
        # Same shape as the Mode R pilot's own I: one gold edge proposed,
        # one edge the human rejects (a false positive), one gold edge
        # (push_back) missed by I but visible to C_static, and one gold
        # edge (drain, through a trait object) omitted by every source.
        self.write(
            "crates/scheduler/specs/_interactions/scheduler_dispatch__to__task_queue_pop_ready.json",
            interaction(
                "scheduler_dispatch__to__task_queue_pop_ready",
                ("Scheduler", "dispatch"), ("TaskQueue", "pop_ready"),
            ),
        )
        self.write(
            "crates/scheduler/specs/_interactions/scheduler_dispatch__to__clock_now.json",
            interaction("scheduler_dispatch__to__clock_now", ("Scheduler", "dispatch"), ("Clock", "now")),
        )
        self.write(
            "ci/results/c_static/crates_scheduler.json",
            callsite_report([(("Scheduler", "dispatch"), ("TaskQueue", "push_back"))]),
        )

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, relative: str, data: dict) -> Path:
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
        return path

    def measure(self, write=True):
        return measure_workspace(self.workspace, self.descriptor, write=write)

    def single(self):
        measurements, findings = self.measure()
        self.assertEqual(len(measurements), 1, [str(f) for f in findings])
        return measurements[0], findings

    def test_the_track_is_derived_from_mode_port(self):
        measurement, _ = self.single()
        self.assertEqual(measurement["track"], "mode-p")
        self.assertEqual(measurement["cluster"], "scheduler-core-port")

    def test_precision_recall_and_omission_match_the_mode_r_pilots_own_shape(self):
        # The measurement machinery is track-agnostic by design; this is
        # the same worked example the Mode R pilot pins (0.5 / 0.3333 /
        # 1 of 3), reproduced against a mode: port descriptor and a
        # track: mode-p gold set to prove it actually runs end to end
        # for the Mode P track, not merely that the code compiles for it.
        measurement, _ = self.single()
        self.assertEqual(measurement["metrics"]["precision"], 0.5)
        self.assertEqual(measurement["metrics"]["recall"], round(1 / 3, 4))
        self.assertEqual(measurement["counts"]["omitted"], 1)
        self.assertEqual(measurement["counts"]["missed_but_proposed"], 1)

    def test_the_omitted_edge_is_the_one_only_differential_testing_could_catch(self):
        measurement, _ = self.single()
        omitted = measurement["omitted_edges"]
        self.assertEqual(len(omitted), 1)
        self.assertEqual(omitted[0]["callee"], {"concept": "TaskQueue", "method": "drain"})
        self.assertIn("virtual dispatch", omitted[0]["rationale"])

    def test_mode_p_source_extraction_is_not_built_here_either(self):
        # The medium-severity fix: chainlink #5 does not exist in this
        # codebase, so even a genuine Mode P workspace reports
        # not-built, never available -- a source with no tooling behind
        # it contributes nothing, and "available" would overstate how
        # many independent sources were actually consulted.
        measurement, _ = self.single()
        entry = next(e for e in measurement["sources"] if e["source"] == "mode-p-source-extraction")
        self.assertEqual(entry["status"], "not-built")
        self.assertEqual(entry["proposed"], 0)

    def test_c_static_extraction_is_still_available_regardless_of_track(self):
        # The one real independent source this codebase has is not
        # itself track-specific -- it reads a C_static report the same
        # way for Mode P as for Mode R.
        measurement, _ = self.single()
        entry = next(e for e in measurement["sources"] if e["source"] == "c-static-extraction")
        self.assertEqual(entry["status"], "available")

    def test_a_mode_r_gold_set_is_refused_against_a_mode_p_descriptor(self):
        # The inverse of HonestyTest's own mode-p-against-mode-r case:
        # confirms the track-match rejection runs both directions, not
        # only the one this suite happened to build first. Swap in the
        # Mode R pilot's own gold set (track: mode-r), under ITS OWN
        # filename -- a filename/cluster mismatch would be rejected by
        # G1b before the track check ever ran, which would prove nothing
        # about track matching.
        (self.workspace / "specs" / "_gold_sets" / "scheduler-core-port.json").unlink()
        self.write("specs/_gold_sets/scheduler-core.json", gold_set())  # track: mode-r
        measurements, findings = self.measure()
        self.assertEqual(measurements, [])
        self.assertTrue(any("would score the wrong track" in str(f) for f in findings))

    def test_the_measurement_artifact_is_written_and_schema_valid(self):
        self.measure()
        path = measurement_dir_for(self.workspace) / "scheduler-core-port.json"
        self.assertTrue(path.is_file())
        data = json.loads(path.read_text())
        self.assertEqual(list(load_measurement_validator().iter_errors(data)), [])

    def test_the_printed_report_names_the_mode_p_track(self):
        measurements, findings = self.measure()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = report(measurements, findings, self.workspace)
        printed = buffer.getvalue()
        self.assertEqual(code, EXIT_OK)
        self.assertIn("scheduler-core-port", printed)
        self.assertIn("mode-p", printed)


class HonestyTest(MeasurementTestCase):
    def test_an_empty_interaction_set_gives_undefined_precision_not_one(self):
        for path in (self.workspace / "crates/scheduler/specs/_interactions").iterdir():
            path.unlink()
        measurements, findings = self.measure()
        self.assertEqual(measurements[0]["metrics"]["precision"], "undefined")
        self.assertTrue(any("has not achieved perfect precision" in str(f) for f in findings))

    def test_no_gold_set_fails_closed(self):
        (self.workspace / "specs/_gold_sets/scheduler-core.json").unlink()
        measurements, findings = self.measure()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = report(measurements, findings, self.workspace)
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertIn("not a good score", buffer.getvalue())

    def test_a_gold_set_for_another_track_is_refused(self):
        data = gold_set()
        data["track"] = "mode-p"
        self.write("specs/_gold_sets/scheduler-core.json", data)
        measurements, findings = self.measure()
        self.assertEqual(measurements, [])
        self.assertTrue(any("would score the wrong track" in str(f) for f in findings))

    def test_the_printed_summary_states_how_few_sources_were_consulted(self):
        measurements, findings = self.measure()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = report(measurements, findings, self.workspace)
        printed = buffer.getvalue()
        self.assertEqual(code, EXIT_OK)
        self.assertIn(f"1 of {len(INDEPENDENT_SOURCES)} independent", printed)
        self.assertIn("lower bound", printed)

    def test_the_report_never_calls_omission_a_gate(self):
        measurements, findings = self.measure()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            report(measurements, findings, self.workspace)
        self.assertIn("finding to read, not a gate", buffer.getvalue())


class ArtifactTest(MeasurementTestCase):
    def test_the_measurement_is_written_and_schema_valid(self):
        self.measure()
        path = measurement_dir_for(self.workspace) / "scheduler-core.json"
        self.assertTrue(path.is_file())
        data = json.loads(path.read_text())
        self.assertEqual(list(load_measurement_validator().iter_errors(data)), [])

    def test_no_write_leaves_the_workspace_alone(self):
        self.measure(write=False)
        self.assertFalse(measurement_dir_for(self.workspace).exists())

    def test_the_measurement_names_the_human_it_scores_against(self):
        measurement, _ = self.single()
        self.assertEqual(measurement["gold_set_review"]["reviewer"], "alice")

    def test_measurement_is_deterministic(self):
        first, _ = self.measure(write=False)
        second, _ = self.measure(write=False)
        self.assertEqual(json.dumps(first), json.dumps(second))


class SourceHelperTest(MeasurementTestCase):
    def test_interaction_edges_reads_only_valid_reviewed_interactions(self):
        broken = interaction("broken", ("Scheduler", "dispatch"), ("TaskQueue", "pop_ready"))
        del broken["review"]
        self.write("crates/scheduler/specs/_interactions/broken.json", broken)
        edges = interaction_edges(self.workspace, self.descriptor)
        self.assertEqual(len(edges), 2)

    def test_c_static_edges_skips_intra_concept_and_unresolved_call_sites(self):
        report_data = callsite_report([
            (("Scheduler", "dispatch"), ("TaskQueue", "push_back")),
            (("Scheduler", "dispatch"), ("Scheduler", "validate")),
        ])
        report_data["callsites"].append({
            "callsite_id": "CS-SCHEDULER-DISPATCH-003",
            "call_class": "possible-dispatch",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee_method": "drain",
            "unresolved_expression": "self.queue.drain(...)",
            "source": {
                "path": "crates/scheduler/src/lib.rs",
                "symbol": "Scheduler::dispatch",
                "line": 30,
                "syntax_hash": "sha256:" + "a" * 64,
            },
            "risk_tier": "medium",
            "risk_tier_source": "extractor-default",
        })
        report_data["callsite_coverage"] = {"discovered": 3, "resolved": 2, "unresolved": 1}
        self.write("ci/results/c_static/crates_scheduler.json", report_data)

        edges, reports = c_static_edges(self.workspace)
        self.assertEqual(reports, 1)
        self.assertEqual(edges, {("Scheduler", "dispatch", "TaskQueue", "push_back")})


if __name__ == "__main__":
    unittest.main()
