"""G19 witness determinism gate (chainlink #30).

plan.md §16.2: regenerate, compare value_hash -- never render_hash.
Built on real workspaces on disk, dispatching to the REAL stand-in
producer (tests/fixtures/witnesses/stand_in_producer.py) via a
project-descriptor `witness_backend`, the same precedent
test_gate_g9.py sets with `verifier_backends` pointed at
fake_verifier.py: the dispatch machinery under test is real, the
producer is visibly a stand-in.
"""
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from gate_g19 import (  # noqa: E402
    EXIT_BLOCKED,
    EXIT_INPUT_ERROR,
    EXIT_OK,
    collect_valid_witness_specs,
    gate_workspace,
    main,
    regenerate_witness,
    report_findings,
)
from validate_witness import snake_case, witness_dir_for  # noqa: E402
from test_validate_witness import concept_spec, witness_spec  # noqa: E402

PRODUCER = ROOT / "tests" / "fixtures" / "witnesses" / "stand_in_producer.py"

DEFAULT_IDENTITY = {
    "witness_id": "W-TQ-LOAD-FACTOR",
    "concept": "TaskQueue",
    "query": "load_factor",
    "fixture_id": "FX-QUEUE-BOTTOM-ROW",
    "seed": 0,
    "renderer": "scalar_field_svg",
}


def run_producer(*extra_args: str, **identity_overrides) -> dict:
    """Invoke the REAL stand-in producer exactly the way gate_g19.py's
    own regenerate_witness() does, so a test's notion of "what the
    producer currently outputs" can never silently drift from what the
    gate itself would compute."""
    identity = {**DEFAULT_IDENTITY, **identity_overrides}
    argv = [
        sys.executable, str(PRODUCER), *extra_args,
        "--witness-id", identity["witness_id"],
        "--concept", identity["concept"],
        "--query", identity["query"],
        "--fixture-id", identity["fixture_id"],
        "--seed", str(identity["seed"]),
        "--renderer", identity["renderer"],
    ]
    completed = subprocess.run(argv, capture_output=True, text=True, check=True)
    return json.loads(completed.stdout)


def descriptor_with(*crate_dirs: str, backend_command: str | None = None) -> dict:
    descriptor = {
        "crates": [
            {"crate_dir": crate_dir, "contracts_crate": "contracts", "specs_search_root": "crates"}
            for crate_dir in crate_dirs
        ]
    }
    if backend_command is not None:
        descriptor["witness_backend"] = {"command": backend_command}
    return descriptor


PRODUCER_BACKEND = f"{sys.executable} {PRODUCER}"


class Workspace:
    def __init__(self, root: Path, crate_dir: str = "crates/scheduler"):
        self.root = root
        self.crate_dir = crate_dir
        self.crate = {"crate_dir": crate_dir, "contracts_crate": "contracts", "specs_search_root": "crates"}
        (self.root / crate_dir / "specs").mkdir(parents=True, exist_ok=True)

    def write(self, relative: str, data) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) if isinstance(data, (dict, list)) else data)
        return path

    def write_concept_spec(self, filename: str = "task_queue.json") -> Path:
        return self.write(f"{self.crate_dir}/specs/{filename}", concept_spec())

    def write_witness(self, data: dict | None = None) -> Path:
        data = data if data is not None else witness_spec(value_hash=run_producer()["value_hash"])
        witness_dir_for(self.crate, self.root).mkdir(parents=True, exist_ok=True)
        stem = f"{snake_case(data['concept'])}.{data['query']}"
        return self.write(f"{self.crate_dir}/specs/_witnesses/{stem}.json", data)


class GateTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ws = Workspace(self.root)
        self.descriptor = descriptor_with("crates/scheduler", backend_command=PRODUCER_BACKEND)

    def tearDown(self):
        self._tmp.cleanup()

    def gate(self, descriptor: dict | None = None, runner=subprocess.run):
        return gate_workspace(self.root, descriptor or self.descriptor, runner=runner)

    def errors(self, findings) -> list[str]:
        return [str(f) for f in findings if f.severity == "error"]


class DeterminismTest(GateTestCase):
    def test_a_deterministic_regeneration_passes(self):
        # The spec's declared hash comes from an actual producer run;
        # gate_workspace() re-runs the SAME real producer with the
        # SAME arguments and must get the same answer.
        baseline = run_producer()
        self.ws.write_concept_spec()
        self.ws.write_witness(witness_spec(value_hash=baseline["value_hash"]))
        findings, discovered = self.gate()
        self.assertEqual(findings, [])
        self.assertEqual(discovered, 1)

    def test_a_changed_value_hash_blocks(self):
        # The spec was authored/reviewed against the producer's
        # original behavior (ready=3, the default). Since then the
        # producer's real behavior has drifted (--ready 5) -- exactly
        # the scenario external review named: "producer behavior
        # changes." gate_workspace() must catch it because it always
        # re-runs the producer, never trusts a stale file.
        baseline = run_producer()
        drifted = run_producer("--ready", "5")
        self.assertNotEqual(baseline["value_hash"], drifted["value_hash"])
        self.ws.write_concept_spec()
        self.ws.write_witness(witness_spec(value_hash=baseline["value_hash"]))
        descriptor = descriptor_with(
            "crates/scheduler", backend_command=f"{PRODUCER_BACKEND} --ready 5"
        )
        findings, discovered = self.gate(descriptor)
        self.assertEqual(discovered, 1)
        self.assertTrue(
            any("does not match the regenerated value_hash" in e for e in self.errors(findings))
        )

    def test_a_changed_render_hash_alone_is_ignored(self):
        # G19's whole point: byte-identical-across-runs is a claim about
        # the VALUE, not the picture. A renderer-library upgrade or a
        # locale change can move render_hash with zero effect on the
        # measured value, and this must never block G19.
        baseline = run_producer()
        self.ws.write_concept_spec()
        spec = witness_spec(value_hash=baseline["value_hash"])
        spec["output"]["render_hash"] = "sha256:" + "f" * 64
        self.ws.write_witness(spec)
        findings, discovered = self.gate()
        self.assertEqual(findings, [])
        self.assertEqual(discovered, 1)

    def test_no_backend_configured_blocks(self):
        # External review, high severity: an earlier version, given no
        # way to actually re-run a producer, fell back to trusting
        # whatever canonical result already sat on disk -- an unenforced
        # "upstream regeneration" step establishes nothing. Now: no
        # backend, no regeneration, no pass.
        baseline = run_producer()
        self.ws.write_concept_spec()
        self.ws.write_witness(witness_spec(value_hash=baseline["value_hash"]))
        descriptor = descriptor_with("crates/scheduler")
        findings, discovered = self.gate(descriptor)
        self.assertEqual(discovered, 1)
        self.assertTrue(any("no witness_backend configured" in e for e in self.errors(findings)))

    def test_a_crashing_backend_blocks(self):
        self.ws.write_concept_spec()
        self.ws.write_witness(witness_spec(value_hash="sha256:" + "a" * 64))
        descriptor = descriptor_with(
            "crates/scheduler", backend_command=f"{sys.executable} /no/such/producer.py"
        )
        findings, discovered = self.gate(descriptor)
        self.assertTrue(
            any("failed to regenerate this witness" in e for e in self.errors(findings))
        )

    def test_a_backend_that_ignores_its_own_arguments_is_caught_by_the_identity_check(self):
        # External review, high severity (second half): the join used
        # to be on witness_id alone. Even with dispatch now mandatory, a
        # misbehaving backend could still echo back a DIFFERENT
        # fixture_id/seed/concept/query than what it was asked to
        # evaluate. A fixed, injected runner simulates exactly that --
        # something dispatch alone cannot rule out, which is why the
        # identity check is a separate, explicit step.
        self.ws.write_concept_spec()
        self.ws.write_witness(witness_spec(value_hash="sha256:" + "a" * 64))

        stale = run_producer(fixture_id="FX-SOME-OTHER-FIXTURE")

        def fake_runner(argv, **kwargs):
            class Completed:
                returncode = 0
                stdout = json.dumps(stale)
                stderr = ""
            return Completed()

        findings, discovered = self.gate(runner=fake_runner)
        self.assertTrue(
            any("does not match the current witness spec" in e for e in self.errors(findings))
        )

    def test_a_backend_producing_a_schema_invalid_result_does_not_count_as_a_regeneration(self):
        self.ws.write_concept_spec()
        self.ws.write_witness(witness_spec(value_hash="sha256:" + "a" * 64))

        def broken_runner(argv, **kwargs):
            class Completed:
                returncode = 0
                stdout = json.dumps({"not": "a canonical result"})
                stderr = ""
            return Completed()

        findings, discovered = self.gate(runner=broken_runner)
        self.assertTrue(
            any("not genuinely valid" in e for e in self.errors(findings))
        )

    def test_zero_witness_specs_passes_with_the_honest_zero_wording(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            findings, discovered = self.gate()
            code = report_findings(findings, discovered, self.root)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(discovered, 0)
        self.assertIn("nothing to check", buffer.getvalue())


class AmbiguityTest(GateTestCase):
    def test_two_valid_witness_files_in_the_same_crate_sharing_a_witness_id_are_ambiguous(self):
        # External review, medium severity: an earlier version only
        # compared the set of owning CRATE NAMES, so two duplicate valid
        # files in the SAME crate both silently fell through to
        # whichever sorted first. The filename stem is derived from
        # (concept, query), not witness_id, so two files sharing one
        # witness_id but declaring DIFFERENT queries (each individually
        # valid, each with its own correctly-matching filename) is the
        # reachable way this happens in one crate -- two files with the
        # SAME (concept, query) would collide on disk as the same path.
        spec = concept_spec()
        spec["queries"].append(
            {"english": "How deep is the queue right now?", "rust_sig": "fn depth(&self) -> usize", "pure": True}
        )
        self.ws.write("crates/scheduler/specs/task_queue.json", spec)

        baseline = run_producer()
        self.ws.write_witness(witness_spec(value_hash=baseline["value_hash"]))
        second = witness_spec(value_hash=baseline["value_hash"])
        second["query"] = "depth"
        second["output"]["path"] = "docs/witnesses/task_queue.depth.svg"
        self.ws.write_witness(second)

        findings, discovered = self.gate()
        self.assertTrue(
            any("genuinely valid witness specs declare this witness_id" in e for e in self.errors(findings))
        )
        self.assertEqual(discovered, 0)

    def test_the_same_witness_id_valid_in_two_crates_is_ambiguous(self):
        self.ws.write_concept_spec()
        baseline = run_producer()
        spec = witness_spec(value_hash=baseline["value_hash"])
        self.ws.write_witness(spec)
        other = Workspace(self.root, crate_dir="crates/other")
        other.write_witness(spec)
        descriptor = descriptor_with(
            "crates/scheduler", "crates/other", backend_command=PRODUCER_BACKEND
        )
        findings, discovered = self.gate(descriptor)
        self.assertTrue(
            any("genuinely valid witness specs declare this witness_id" in e for e in self.errors(findings))
        )
        self.assertEqual(discovered, 0)


class DiscoveryUnitTest(GateTestCase):
    def test_collect_valid_witness_specs_excludes_a_schema_invalid_spec(self):
        self.ws.write_concept_spec()
        broken = witness_spec(value_hash="sha256:" + "a" * 64)
        del broken["determinism"]
        self.ws.write_witness(broken)
        specs, findings = collect_valid_witness_specs(self.descriptor, self.root)
        self.assertEqual(specs, {})


class RegenerateWitnessUnitTest(unittest.TestCase):
    def test_a_real_regeneration_round_trips_through_the_real_schema(self):
        spec = witness_spec(value_hash="sha256:" + "a" * 64)
        document, argv, exit_code, error = regenerate_witness(spec, {"command": PRODUCER_BACKEND})
        self.assertEqual(error, "")
        self.assertEqual(exit_code, 0)
        self.assertEqual(document["witness_id"], spec["witness_id"])
        self.assertIn("--witness-id", argv)


class ReportingTest(GateTestCase):
    def test_a_passing_gate_reports_the_count(self):
        baseline = run_producer()
        self.ws.write_concept_spec()
        self.ws.write_witness(witness_spec(value_hash=baseline["value_hash"]))
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            findings, discovered = self.gate()
            code = report_findings(findings, discovered, self.root)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("1 discovered", buffer.getvalue())

    def test_a_blocked_gate_exits_non_zero_and_lists_findings(self):
        self.ws.write_concept_spec()
        self.ws.write_witness(witness_spec(value_hash="sha256:" + "a" * 64))
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            findings, discovered = self.gate()
            code = report_findings(findings, discovered, self.root)
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertIn("FAIL: 1 finding(s)", buffer.getvalue())

    def test_cli_end_to_end(self):
        baseline = run_producer()
        self.ws.write_concept_spec()
        self.ws.write_witness(witness_spec(value_hash=baseline["value_hash"]))
        (self.root / "project-descriptor.json").write_text(json.dumps(self.descriptor))
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main([str(self.root)])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("1 discovered", buffer.getvalue())

    def test_cli_refuses_a_missing_workspace(self):
        self.assertEqual(main([str(self.root / "nope")]), EXIT_INPUT_ERROR)

    def test_cli_refuses_a_missing_descriptor(self):
        self.assertEqual(main([str(self.root)]), EXIT_INPUT_ERROR)


if __name__ == "__main__":
    unittest.main()
