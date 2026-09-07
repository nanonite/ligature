"""Witness generation end to end (chainlink #28): the "fail loudly, write
nothing" contract, one layer up from the renderer itself.

Reuses tests/test_witness_result.py's build_result()-shaped documents
directly, and writes them at the exact ci/results/witnesses/<witness_id>.json
convention chainlink #27 established, since generate_witness.py's whole
job is reading that convention honestly.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from generate_witness import (  # noqa: E402
    EXIT_GENERATION_FAILED,
    EXIT_INPUT_ERROR,
    EXIT_OK,
    GenerationError,
    generate,
    main,
    output_path_for,
)
from witness_result import build_result, encode_grid, encode_series, witness_result_dir_for  # noqa: E402


def load_factor_result(values=None) -> dict:
    values = values or [0.125, 0.25, 0.375, 0.5]
    return build_result(
        witness_id="W-TQ-LOAD-FACTOR",
        concept="TaskQueue",
        query="load_factor",
        fixture_id="FX-QUEUE-BOTTOM-ROW",
        seed=0,
        renderer_actual="scalar_field_svg",
        result=encode_grid(1, len(values), [(0, i, v) for i, v in enumerate(values)]),
    )


class GenerationTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        self.write_result(load_factor_result())

    def tearDown(self):
        self._tmp.cleanup()

    def write_result(self, document: dict) -> Path:
        directory = witness_result_dir_for(self.workspace)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{document['witness_id']}.json"
        path.write_text(json.dumps(document, indent=2))
        return path


class OutputPathTest(unittest.TestCase):
    def test_matches_validate_witnesss_own_computation(self):
        # Must never silently diverge from check_output_path's own
        # expected_stem() in scripts/validate_witness.py, or a generated
        # SVG could sit somewhere a promoted spec's own path check
        # rejects.
        sys.path.insert(0, str(ROOT / "scripts"))
        from validate_witness import expected_stem

        path = output_path_for("TaskQueue", "load_factor")
        self.assertEqual(path, f"docs/witnesses/{expected_stem({'concept': 'TaskQueue', 'query': 'load_factor'})}.svg")


class SuccessTest(GenerationTestCase):
    def test_writes_the_svg_and_returns_the_output_block(self):
        output = generate(self.workspace, "W-TQ-LOAD-FACTOR", "scalar_field_svg")
        self.assertEqual(output["path"], "docs/witnesses/task_queue.load_factor.svg")
        self.assertTrue(output["render_hash"].startswith("sha256:"))
        self.assertEqual(output["renderer_actual"], "scalar_field_svg")
        self.assertTrue((self.workspace / output["path"]).is_file())

    def test_the_returned_render_hash_matches_the_written_bytes(self):
        import hashlib

        output = generate(self.workspace, "W-TQ-LOAD-FACTOR", "scalar_field_svg")
        written = (self.workspace / output["path"]).read_bytes()
        self.assertEqual(output["render_hash"], "sha256:" + hashlib.sha256(written).hexdigest())

    def test_generation_is_deterministic(self):
        first = generate(self.workspace, "W-TQ-LOAD-FACTOR", "scalar_field_svg")
        second = generate(self.workspace, "W-TQ-LOAD-FACTOR", "scalar_field_svg")
        self.assertEqual(first, second)

    def test_regeneration_overwrites_the_previous_svg(self):
        first = generate(self.workspace, "W-TQ-LOAD-FACTOR", "scalar_field_svg")
        self.write_result(load_factor_result([0.9, 0.9, 0.9, 0.9]))
        second = generate(self.workspace, "W-TQ-LOAD-FACTOR", "scalar_field_svg")
        self.assertNotEqual(first["render_hash"], second["render_hash"])
        self.assertEqual(first["path"], second["path"])


class FailureWritesNothingTest(GenerationTestCase):
    """The load-bearing property: every failure mode leaves the workspace
    exactly as it was, never a partial or degraded artifact."""

    def svg_dir(self) -> Path:
        return self.workspace / "docs" / "witnesses"

    def test_an_incompatible_renderer_writes_nothing(self):
        with self.assertRaises(GenerationError) as caught:
            generate(self.workspace, "W-TQ-LOAD-FACTOR", "series_svg")
        self.assertIn("could not render", str(caught.exception))
        self.assertFalse(self.svg_dir().exists())

    def test_an_unregistered_renderer_writes_nothing(self):
        with self.assertRaises(GenerationError):
            generate(self.workspace, "W-TQ-LOAD-FACTOR", "not_a_real_renderer")
        self.assertFalse(self.svg_dir().exists())

    def test_an_unknown_witness_id_writes_nothing(self):
        with self.assertRaises(GenerationError) as caught:
            generate(self.workspace, "W-GHOST", "scalar_field_svg")
        self.assertIn("no genuinely valid canonical result", str(caught.exception))
        self.assertFalse(self.svg_dir().exists())

    def test_a_schema_invalid_canonical_result_is_treated_as_absent(self):
        # load_results_by_witness (chainlink #27) already enforces
        # "genuinely valid, not just present" -- confirm generation
        # inherits that bar rather than reading a tampered file directly.
        broken = load_factor_result()
        broken["value_hash"] = "sha256:" + "0" * 64
        self.write_result(broken)
        with self.assertRaises(GenerationError):
            generate(self.workspace, "W-TQ-LOAD-FACTOR", "scalar_field_svg")
        self.assertFalse(self.svg_dir().exists())

    def test_a_failed_regeneration_does_not_delete_a_prior_good_svg(self):
        # Regeneration failing must not destroy the last known-good
        # rendering -- "writes nothing" means no MUTATION, not "clears
        # the slate".
        first = generate(self.workspace, "W-TQ-LOAD-FACTOR", "scalar_field_svg")
        before = (self.workspace / first["path"]).read_bytes()
        with self.assertRaises(GenerationError):
            generate(self.workspace, "W-TQ-LOAD-FACTOR", "series_svg")
        after = (self.workspace / first["path"]).read_bytes()
        self.assertEqual(before, after)


class CliTest(GenerationTestCase):
    def run_cli(self, *args) -> tuple[int, str, str]:
        import io
        from contextlib import redirect_stderr, redirect_stdout

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_success_prints_the_output_block_as_json_on_stdout(self):
        code, out, err = self.run_cli(
            "W-TQ-LOAD-FACTOR", "--renderer", "scalar_field_svg", "--workspace", str(self.workspace)
        )
        self.assertEqual(code, EXIT_OK)
        parsed = json.loads(out)
        self.assertEqual(parsed["renderer_actual"], "scalar_field_svg")
        self.assertIn("wrote", err)

    def test_generation_failure_exits_nonzero_with_a_stderr_message(self):
        code, out, err = self.run_cli(
            "W-TQ-LOAD-FACTOR", "--renderer", "series_svg", "--workspace", str(self.workspace)
        )
        self.assertEqual(code, EXIT_GENERATION_FAILED)
        self.assertEqual(out, "")
        self.assertIn("error:", err)

    def test_a_missing_workspace_is_an_input_error(self):
        code, out, err = self.run_cli(
            "W-TQ-LOAD-FACTOR", "--renderer", "scalar_field_svg",
            "--workspace", str(self.workspace / "nope"),
        )
        self.assertEqual(code, EXIT_INPUT_ERROR)


if __name__ == "__main__":
    unittest.main()
