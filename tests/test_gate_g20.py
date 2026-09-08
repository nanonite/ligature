"""G20 degenerate-witness gate (chainlink #31).

plan.md §12's gate table / §16.2: must-vary vs constant, and
fixture-family/coverage_region consistency, both WARN severity (Stage
4.5, "block at promotion if unresolved" -- mirrored here as a distinct
non-zero, non-EXIT_BLOCKED exit code, gate_r1_g16.py's own
EXIT_DECISION_REQUIRED precedent). Built on real workspaces on disk,
the same reason test_gate_g14/g18/g19.py give.
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

from gate_g20 import (  # noqa: E402
    EXIT_BLOCKED,
    EXIT_INPUT_ERROR,
    EXIT_OK,
    EXIT_WARN,
    check_fixture_family_consistency,
    check_must_vary,
    gate_workspace,
    main,
    report_findings,
)
from validate_witness import snake_case, witness_dir_for  # noqa: E402
from witness_result import build_result, encode_grid, witness_result_dir_for  # noqa: E402
from test_validate_witness import concept_spec, witness_spec  # noqa: E402


def descriptor_with(*crate_dirs: str) -> dict:
    return {
        "crates": [
            {"crate_dir": crate_dir, "contracts_crate": "contracts", "specs_search_root": "crates"}
            for crate_dir in crate_dirs
        ]
    }


def constant_result(witness_id="W-TQ-LOAD-FACTOR", concept="TaskQueue", query="load_factor",
                     fixture_id="FX-QUEUE-BOTTOM-ROW", value=0.5) -> dict:
    return build_result(
        witness_id=witness_id, concept=concept, query=query, fixture_id=fixture_id, seed=0,
        renderer_actual="scalar_field_svg",
        result=encode_grid(1, 4, [(0, 0, value), (0, 1, value), (0, 2, value), (0, 3, value)]),
    )


def varying_result(witness_id="W-TQ-LOAD-FACTOR", concept="TaskQueue", query="load_factor",
                    fixture_id="FX-QUEUE-BOTTOM-ROW") -> dict:
    return build_result(
        witness_id=witness_id, concept=concept, query=query, fixture_id=fixture_id, seed=0,
        renderer_actual="scalar_field_svg",
        result=encode_grid(1, 4, [(0, 0, 0.125), (0, 1, 0.25), (0, 2, 0.375), (0, 3, 0.5)]),
    )


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

    def write_concept_spec(self, extra_queries: tuple[dict, ...] = ()) -> Path:
        spec = concept_spec()
        spec["queries"].extend(extra_queries)
        return self.write(f"{self.crate_dir}/specs/task_queue.json", spec)

    def write_witness(self, data: dict | None = None) -> Path:
        data = data if data is not None else witness_spec(value_hash="sha256:" + "a" * 64)
        witness_dir_for(self.crate, self.root).mkdir(parents=True, exist_ok=True)
        stem = f"{snake_case(data['concept'])}.{data['query']}"
        return self.write(f"{self.crate_dir}/specs/_witnesses/{stem}.json", data)

    def write_result(self, data: dict) -> Path:
        directory = witness_result_dir_for(self.root)
        directory.mkdir(parents=True, exist_ok=True)
        return self.write(str((directory / f"{data['witness_id']}.json").relative_to(self.root)), data)


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

    def by_severity(self, findings, severity: str) -> list[str]:
        return [str(f) for f in findings if f.severity == severity]


class MustVaryTest(GateTestCase):
    def test_a_varying_result_matching_must_vary_passes(self):
        self.ws.write_concept_spec()
        self.ws.write_witness()
        self.ws.write_result(varying_result())
        findings, discovered = self.gate()
        self.assertEqual(findings, [])
        self.assertEqual(discovered, 1)

    def test_a_constant_result_declared_must_vary_warns(self):
        self.ws.write_concept_spec()
        self.ws.write_witness()
        self.ws.write_result(constant_result())
        findings, discovered = self.gate()
        self.assertEqual(discovered, 1)
        self.assertTrue(
            any("measures only one distinct value" in e for e in self.by_severity(findings, "warn"))
        )
        self.assertEqual(self.by_severity(findings, "error"), [])

    def test_constant_allowed_with_a_constant_result_is_not_flagged(self):
        spec = witness_spec(value_hash="sha256:" + "a" * 64)
        spec["expectation"]["value_distribution"] = "constant-allowed"
        self.ws.write_concept_spec()
        self.ws.write_witness(spec)
        self.ws.write_result(constant_result())
        findings, discovered = self.gate()
        self.assertEqual(findings, [])

    def test_a_must_vary_witness_with_no_result_is_not_flagged_here(self):
        # chainlink #30's G19 already hard-blocks "no genuinely valid
        # canonical result" as its own concern; G20 must not repeat it.
        self.ws.write_concept_spec()
        self.ws.write_witness()
        findings, discovered = self.gate()
        self.assertEqual(findings, [])
        self.assertEqual(discovered, 1)

    def test_a_stale_result_under_a_different_fixture_is_not_trusted(self):
        # External review, high severity: an earlier version joined
        # solely on witness_id, so a stale result left over from an
        # earlier fixture_id revision -- self-consistent, but never
        # actually about the CURRENT spec -- could make a currently
        # constant witness pass simply because the stale result happens
        # to vary. The identity check must catch this before
        # value_domain is ever trusted.
        self.ws.write_concept_spec()
        self.ws.write_witness()
        stale = varying_result(fixture_id="FX-SOME-OTHER-FIXTURE")
        self.ws.write_result(stale)
        findings, discovered = self.gate()
        self.assertEqual(discovered, 1)
        self.assertTrue(
            any("does not match the current witness spec" in e and "fixture_id" in e
                for e in self.by_severity(findings, "warn"))
        )
        self.assertEqual(self.by_severity(findings, "error"), [])


class FixtureFamilyConsistencyTest(GateTestCase):
    def setUp(self):
        super().setUp()
        self.ws.write_concept_spec(extra_queries=(
            {"english": "How deep is the queue right now?", "rust_sig": "fn depth(&self) -> usize", "pure": True},
        ))

    def second_witness(self, coverage_region: str) -> dict:
        spec = witness_spec(value_hash="sha256:" + "a" * 64)
        spec["witness_id"] = "W-TQ-DEPTH"
        spec["query"] = "depth"
        spec["output"]["path"] = "docs/witnesses/task_queue.depth.svg"
        spec["expectation"]["coverage_region"] = coverage_region
        return spec

    def test_two_witnesses_sharing_a_family_with_different_coverage_region_warns(self):
        self.ws.write_witness()  # coverage_region: bottom-row, fixture_family: FX-BOTTOM-ROW (default)
        self.ws.write_witness(self.second_witness("full-grid"))
        findings, discovered = self.gate()
        self.assertEqual(discovered, 2)
        self.assertTrue(
            any("disagree about the region" in e for e in self.by_severity(findings, "warn"))
        )
        self.assertEqual(self.by_severity(findings, "error"), [])

    def test_two_witnesses_sharing_a_family_with_the_same_coverage_region_is_not_flagged(self):
        self.ws.write_witness()
        self.ws.write_witness(self.second_witness("bottom-row"))
        findings, discovered = self.gate()
        self.assertEqual(findings, [])
        self.assertEqual(discovered, 2)


class AmbiguityTest(GateTestCase):
    def test_an_ambiguous_witness_id_blocks_even_alongside_a_warn_condition(self):
        # Errors take priority over warnings in this gate's own report:
        # a structural identity problem is a different KIND of defect
        # than a degeneracy one, and must not be buried under a warning.
        self.ws.write_concept_spec(extra_queries=(
            {"english": "How deep is the queue right now?", "rust_sig": "fn depth(&self) -> usize", "pure": True},
        ))
        spec = witness_spec(value_hash="sha256:" + "a" * 64)
        self.ws.write_witness(spec)
        duplicate = witness_spec(value_hash="sha256:" + "a" * 64)
        duplicate["query"] = "depth"
        duplicate["output"]["path"] = "docs/witnesses/task_queue.depth.svg"
        self.ws.write_witness(duplicate)  # same witness_id, different query -> ambiguous
        self.ws.write_result(constant_result())  # would also warn if reached

        findings, discovered = self.gate()
        self.assertEqual(discovered, 0)
        self.assertTrue(
            any("genuinely valid witness specs declare this witness_id" in e
                for e in self.by_severity(findings, "error"))
        )

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = report_findings(findings, discovered, self.root)
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertIn("FAIL:", buffer.getvalue())


class DiscoveryUnitTest(unittest.TestCase):
    def test_check_must_vary_ignores_a_spec_with_constant_allowed(self):
        spec = witness_spec(value_hash="sha256:" + "a" * 64)
        spec["expectation"]["value_distribution"] = "constant-allowed"
        result = constant_result()
        self.assertEqual(check_must_vary({"W-TQ-LOAD-FACTOR": spec}, {"W-TQ-LOAD-FACTOR": result}), [])

    def test_check_fixture_family_consistency_agrees_with_a_single_witness(self):
        spec = witness_spec(value_hash="sha256:" + "a" * 64)
        self.assertEqual(check_fixture_family_consistency({"W-TQ-LOAD-FACTOR": spec}), [])


class ReportingTest(GateTestCase):
    def test_zero_witness_specs_passes_with_the_honest_zero_wording(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            findings, discovered = self.gate()
            code = report_findings(findings, discovered, self.root)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(discovered, 0)
        self.assertIn("nothing to check", buffer.getvalue())

    def test_a_passing_gate_reports_the_count(self):
        self.ws.write_concept_spec()
        self.ws.write_witness()
        self.ws.write_result(varying_result())
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            findings, discovered = self.gate()
            code = report_findings(findings, discovered, self.root)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("1 discovered", buffer.getvalue())

    def test_a_warn_only_gate_exits_with_the_distinct_warn_code(self):
        self.ws.write_concept_spec()
        self.ws.write_witness()
        self.ws.write_result(constant_result())
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            findings, discovered = self.gate()
            code = report_findings(findings, discovered, self.root)
        self.assertEqual(code, EXIT_WARN)
        self.assertNotEqual(code, EXIT_OK)
        self.assertNotEqual(code, EXIT_BLOCKED)
        self.assertIn("WARN: 1 finding(s) -- must be resolved before promotion", buffer.getvalue())

    def test_cli_end_to_end(self):
        self.ws.write_concept_spec()
        self.ws.write_witness()
        self.ws.write_result(varying_result())
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
