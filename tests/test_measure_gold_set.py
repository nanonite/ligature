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
