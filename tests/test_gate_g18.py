"""G18 witness coverage gate (chainlink #29).

plan.md §16.2: every query marked witness_required has a witness spec
and a generated rendering. Built as real workspaces on disk -- a
concept spec under a crate's specs/, a witness spec under its
specs/_witnesses/, and a rendering file under docs/witnesses/ -- for
the same reason test_gate_g14.py gives: a test handing the gate
pre-loaded dicts would not be testing the gate anyone runs.
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

from gate_g18 import (  # noqa: E402
    EXIT_BLOCKED,
    EXIT_INPUT_ERROR,
    EXIT_OK,
    collect_declared_features,
    discover_declared_features,
    gate_workspace,
    main,
    report_findings,
)
from validate_witness import snake_case, witness_dir_for  # noqa: E402
from test_validate_witness import concept_spec, witness_spec  # noqa: E402


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

    def write_concept_spec(self, witness_required: bool | None = True, filename: str = "task_queue.json") -> Path:
        spec = concept_spec()
        if witness_required is None:
            spec["queries"][0].pop("witness_required", None)
        else:
            spec["queries"][0]["witness_required"] = witness_required
        return self.write(f"{self.crate_dir}/specs/{filename}", spec)

    def write_witness(self, data: dict | None = None) -> Path:
        data = data if data is not None else witness_spec()
        witness_dir_for(self.crate, self.root).mkdir(parents=True, exist_ok=True)
        stem = f"{snake_case(data['concept'])}.{data['query']}"
        return self.write(f"{self.crate_dir}/specs/_witnesses/{stem}.json", data)

    def write_rendering(self, relative: str = "docs/witnesses/task_queue.load_factor.svg") -> Path:
        return self.write(relative, "<svg></svg>")


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


class CoverageTest(GateTestCase):
    def test_a_fully_covered_feature_passes(self):
        self.ws.write_concept_spec()
        self.ws.write_witness()
        self.ws.write_rendering()
        findings, discovered = self.gate()
        self.assertEqual(findings, [])
        self.assertEqual(discovered, 1)

    def test_a_query_not_marked_witness_required_is_invisible_to_the_gate(self):
        self.ws.write_concept_spec(witness_required=False)
        findings, discovered = self.gate()
        self.assertEqual(findings, [])
        self.assertEqual(discovered, 0)

    def test_an_omitted_witness_required_is_invisible_to_the_gate(self):
        self.ws.write_concept_spec(witness_required=None)
        findings, discovered = self.gate()
        self.assertEqual(findings, [])
        self.assertEqual(discovered, 0)

    def test_a_non_boolean_witness_required_is_not_a_declaration(self):
        spec = concept_spec()
        spec["queries"][0]["witness_required"] = "true"
        self.ws.write(f"{self.ws.crate_dir}/specs/task_queue.json", spec)
        findings, discovered = self.gate()
        self.assertEqual(findings, [])
        self.assertEqual(discovered, 0)

    def test_a_declared_feature_with_no_witness_at_all_blocks(self):
        self.ws.write_concept_spec()
        findings, discovered = self.gate()
        self.assertEqual(discovered, 1)
        self.assertTrue(
            any("no genuinely valid witness spec resolves" in e for e in self.errors(findings))
        )

    def test_a_witness_that_fails_its_own_validation_does_not_count_as_coverage(self):
        # G2: the query must resolve to pure: true. A non-pure query
        # still gets its witness_required declaration, but the witness
        # spec written against it is not genuinely valid, so it must not
        # count as coverage -- the same "genuinely valid, not just
        # present" bar every cross-reference in this codebase applies.
        spec = concept_spec(pure=False)
        spec["queries"][0]["witness_required"] = True
        self.ws.write(f"{self.ws.crate_dir}/specs/task_queue.json", spec)
        self.ws.write_witness()
        self.ws.write_rendering()
        findings, discovered = self.gate()
        self.assertEqual(discovered, 1)
        self.assertTrue(
            any("no genuinely valid witness spec resolves" in e for e in self.errors(findings))
        )

    def test_a_valid_witness_with_no_rendering_on_disk_blocks(self):
        self.ws.write_concept_spec()
        self.ws.write_witness()
        findings, discovered = self.gate()
        self.assertEqual(discovered, 1)
        self.assertTrue(any("does not exist on disk" in e for e in self.errors(findings)))

    def test_a_stale_rendering_at_the_wrong_path_still_blocks(self):
        self.ws.write_concept_spec()
        self.ws.write_witness()
        self.ws.write_rendering("docs/witnesses/wrong_name.svg")
        findings, discovered = self.gate()
        self.assertTrue(any("does not exist on disk" in e for e in self.errors(findings)))


class AmbiguityTest(GateTestCase):
    def test_the_same_feature_declared_in_two_concept_specs_is_ambiguous(self):
        self.ws.write_concept_spec()
        self.ws.write_concept_spec(filename="task_queue_dup.json")
        self.ws.write_witness()
        self.ws.write_rendering()
        findings, discovered = self.gate()
        self.assertTrue(
            any("more than one concept spec" in e for e in self.errors(findings))
        )
        # Ambiguous declarations are excluded, not resolved from
        # whichever file sorted first -- ambiguous, never first-wins.
        self.assertEqual(discovered, 0)

    def test_a_valid_witness_for_the_same_query_in_two_crates_is_ambiguous(self):
        self.ws.write_concept_spec()
        self.ws.write_witness()
        self.ws.write_rendering()
        other = Workspace(self.root, crate_dir="crates/other")
        other.write_witness()
        descriptor = descriptor_with("crates/scheduler", "crates/other")
        findings, discovered = self.gate(descriptor)
        self.assertTrue(any("more than one crate" in e for e in self.errors(findings)))


class DiscoveryUnitTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, relative: str, data: dict) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return path

    def test_a_query_with_no_parseable_rust_sig_is_skipped(self):
        spec = concept_spec()
        spec["queries"][0]["rust_sig"] = "not a signature"
        self.write("crates/scheduler/specs/task_queue.json", spec)
        self.assertEqual(discover_declared_features(self.root), [])

    def test_an_underscore_prefixed_directory_is_never_scanned_as_a_concept_spec(self):
        # A witness spec carries its own top-level `concept` field --
        # scanning _witnesses/ here would find the witness itself.
        witness = witness_spec()
        self.write("crates/scheduler/specs/_witnesses/task_queue.load_factor.json", witness)
        self.assertEqual(discover_declared_features(self.root), [])

    def test_a_document_naming_a_concept_but_carrying_no_query_command_or_constraint_is_not_a_spec(self):
        self.write("crates/scheduler/specs/not_a_spec.json", {"concept": "TaskQueue"})
        self.assertEqual(discover_declared_features(self.root), [])

    def test_a_missing_search_root_yields_nothing_rather_than_raising(self):
        self.assertEqual(discover_declared_features(self.root / "nope"), [])


class ReportingTest(GateTestCase):
    def test_zero_declared_features_passes_with_the_honest_zero_wording(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            findings, discovered = self.gate()
            code = report_findings(findings, discovered, self.root)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("nothing to check", buffer.getvalue())

    def test_a_covered_feature_passes_and_reports_the_count(self):
        self.ws.write_concept_spec()
        self.ws.write_witness()
        self.ws.write_rendering()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            findings, discovered = self.gate()
            code = report_findings(findings, discovered, self.root)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("1 discovered", buffer.getvalue())

    def test_a_blocked_gate_exits_non_zero_and_lists_findings(self):
        self.ws.write_concept_spec()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            findings, discovered = self.gate()
            code = report_findings(findings, discovered, self.root)
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertIn("FAIL: 1 finding(s)", buffer.getvalue())

    def test_cli_end_to_end(self):
        self.ws.write_concept_spec()
        self.ws.write_witness()
        self.ws.write_rendering()
        self.write_descriptor()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main([str(self.root)])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("1 discovered", buffer.getvalue())

    def test_cli_refuses_a_missing_workspace(self):
        self.assertEqual(main([str(self.root / "nope")]), EXIT_INPUT_ERROR)

    def test_cli_refuses_a_missing_descriptor(self):
        self.assertEqual(main([str(self.root)]), EXIT_INPUT_ERROR)

    def write_descriptor(self) -> None:
        (self.root / "project-descriptor.json").write_text(json.dumps(self.descriptor))


if __name__ == "__main__":
    unittest.main()
