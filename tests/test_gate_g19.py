"""G19 witness determinism gate (chainlink #30).

plan.md §16.2: regenerate, compare value_hash -- never render_hash.
Built as real workspaces on disk -- a concept spec, a witness spec under
specs/_witnesses/, and a canonical result under ci/results/witnesses/ --
for the same reason test_gate_g14.py and test_gate_g18.py give: a test
handing the gate pre-loaded dicts would not be testing the gate anyone
runs.
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

from gate_g19 import (  # noqa: E402
    EXIT_BLOCKED,
    EXIT_INPUT_ERROR,
    EXIT_OK,
    collect_valid_witness_specs,
    gate_workspace,
    main,
    report_findings,
)
from validate_witness import snake_case, witness_dir_for  # noqa: E402
from witness_result import build_result, encode_grid, witness_result_dir_for  # noqa: E402
from test_validate_witness import canonical_result, concept_spec, witness_spec  # noqa: E402


def descriptor_with(*crate_dirs: str) -> dict:
    return {
        "crates": [
            {"crate_dir": crate_dir, "contracts_crate": "contracts", "specs_search_root": "crates"}
            for crate_dir in crate_dirs
        ]
    }


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
        data = data if data is not None else witness_spec()
        witness_dir_for(self.crate, self.root).mkdir(parents=True, exist_ok=True)
        stem = f"{snake_case(data['concept'])}.{data['query']}"
        return self.write(f"{self.crate_dir}/specs/_witnesses/{stem}.json", data)

    def write_result(self, data: dict | None = None) -> Path:
        data = data if data is not None else canonical_result()
        directory = witness_result_dir_for(self.root)
        directory.mkdir(parents=True, exist_ok=True)
        return self.write(str((directory / f"{data['witness_id']}.json").relative_to(self.root)), data)


def drifted_result() -> dict:
    """A canonical result that is internally self-consistent (its own
    value_hash matches its own content -- otherwise load_results_by_witness
    would reject it before G19 ever sees it) but measures DIFFERENT values
    than witness_spec()'s frozen determinism.value_hash was computed over
    -- the shape a real regeneration takes when a producer's output has
    actually drifted."""
    return build_result(
        witness_id="W-TQ-LOAD-FACTOR",
        concept="TaskQueue",
        query="load_factor",
        fixture_id="FX-QUEUE-BOTTOM-ROW",
        seed=0,
        renderer_actual="scalar_field_svg",
        result=encode_grid(1, 4, [(0, 0, 0.5), (0, 1, 0.5), (0, 2, 0.5), (0, 3, 0.5)]),
    )


class GateTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ws = Workspace(self.root)
        self.descriptor = descriptor_with("crates/scheduler")

    def tearDown(self):
        self._tmp.cleanup()

    def gate(self, descriptor: dict | None = None):
        return gate_workspace(self.root, descriptor or self.descriptor)

    def errors(self, findings) -> list[str]:
        return [str(f) for f in findings if f.severity == "error"]


class DeterminismTest(GateTestCase):
    def test_a_deterministic_regeneration_passes(self):
        self.ws.write_concept_spec()
        self.ws.write_witness()
        self.ws.write_result()
        findings, discovered = self.gate()
        self.assertEqual(findings, [])
        self.assertEqual(discovered, 1)

    def test_a_changed_value_hash_blocks(self):
        # The result was "regenerated" (a fresh, internally consistent
        # canonical result on disk) but its measured values -- and
        # therefore its value_hash -- differ from what the reviewed
        # witness spec froze at promotion time.
        self.ws.write_concept_spec()
        self.ws.write_witness()
        drifted = drifted_result()
        self.assertNotEqual(drifted["value_hash"], witness_spec()["determinism"]["value_hash"])
        self.ws.write_result(drifted)
        findings, discovered = self.gate()
        self.assertEqual(discovered, 1)
        self.assertTrue(
            any("does not match the regenerated result's value_hash" in e for e in self.errors(findings))
        )

    def test_a_changed_render_hash_alone_is_ignored(self):
        # G19's whole point: byte-identical-across-runs is a claim about
        # the VALUE, not the picture. A renderer-library upgrade or a
        # locale change can move render_hash with zero effect on the
        # measured value, and this must never block G19.
        self.ws.write_concept_spec()
        spec = witness_spec()
        spec["output"]["render_hash"] = "sha256:" + "f" * 64
        self.assertNotEqual(spec["output"]["render_hash"], witness_spec()["output"]["render_hash"])
        self.ws.write_witness(spec)
        self.ws.write_result()
        findings, discovered = self.gate()
        self.assertEqual(findings, [])
        self.assertEqual(discovered, 1)

    def test_a_witness_with_no_regenerated_result_blocks(self):
        self.ws.write_concept_spec()
        self.ws.write_witness()
        findings, discovered = self.gate()
        self.assertEqual(discovered, 1)
        self.assertTrue(
            any("no genuinely valid canonical result" in e for e in self.errors(findings))
        )

    def test_a_result_that_fails_its_own_validation_does_not_count_as_a_regeneration(self):
        # A result whose own value_hash disagrees with its own content is
        # rejected by validate_witness.py's own G1b before G19 ever sees
        # it -- exercised here via load_results_by_witness, not
        # re-implemented.
        self.ws.write_concept_spec()
        self.ws.write_witness()
        tampered = canonical_result()
        tampered["value_hash"] = "sha256:" + "0" * 64
        self.ws.write_result(tampered)
        findings, discovered = self.gate()
        self.assertTrue(
            any("no genuinely valid canonical result" in e for e in self.errors(findings))
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
    def test_the_same_witness_id_valid_in_two_crates_is_ambiguous(self):
        self.ws.write_concept_spec()
        self.ws.write_witness()
        self.ws.write_result()
        other = Workspace(self.root, crate_dir="crates/other")
        other.write_witness()
        descriptor = descriptor_with("crates/scheduler", "crates/other")
        findings, discovered = self.gate(descriptor)
        self.assertTrue(any("more than one crate" in e for e in self.errors(findings)))
        self.assertEqual(discovered, 0)


class DiscoveryUnitTest(GateTestCase):
    def test_collect_valid_witness_specs_excludes_a_schema_invalid_spec(self):
        self.ws.write_concept_spec()
        broken = witness_spec()
        del broken["determinism"]
        self.ws.write_witness(broken)
        specs, findings = collect_valid_witness_specs(self.descriptor, self.root)
        self.assertEqual(specs, {})


class ReportingTest(GateTestCase):
    def test_a_passing_gate_reports_the_count(self):
        self.ws.write_concept_spec()
        self.ws.write_witness()
        self.ws.write_result()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            findings, discovered = self.gate()
            code = report_findings(findings, discovered, self.root)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("1 discovered", buffer.getvalue())

    def test_a_blocked_gate_exits_non_zero_and_lists_findings(self):
        self.ws.write_concept_spec()
        self.ws.write_witness()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            findings, discovered = self.gate()
            code = report_findings(findings, discovered, self.root)
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertIn("FAIL: 1 finding(s)", buffer.getvalue())

    def test_cli_end_to_end(self):
        self.ws.write_concept_spec()
        self.ws.write_witness()
        self.ws.write_result()
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
