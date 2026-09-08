"""Witness generation end to end (chainlink #28): the "fail loudly, write
nothing" contract, one layer up from the renderer itself.

Reuses tests/test_witness_result.py's build_result()-shaped documents
directly, and writes them at the exact ci/results/witnesses/<witness_id>.json
convention chainlink #27 established, since generate_witness.py's whole
job is reading that convention honestly.

Two contract gaps an external review found and this file now covers
directly (both fixed in generate_witness.py):
  * a requested renderer that disagreed with the canonical result's own
    self-recorded `renderer_actual` was never checked, so a schema-valid
    result claiming e.g. `series_svg` could be rendered successfully
    under a *different* requested renderer, leaving two on-disk records
    of "what actually ran" that contradict each other;
  * the SVG was written directly to its final path, so an I/O failure
    partway through the write (disk full, process killed) could leave a
    truncated file sitting where a previously good rendering used to be
    -- "failure means no mutation" was only true at the logic layer, not
    the I/O layer.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import atomic_write  # noqa: E402
import generate_witness  # noqa: E402
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


def load_factor_result(values=None, renderer_actual="scalar_field_svg") -> dict:
    values = values or [0.125, 0.25, 0.375, 0.5]
    return build_result(
        witness_id="W-TQ-LOAD-FACTOR",
        concept="TaskQueue",
        query="load_factor",
        fixture_id="FX-QUEUE-BOTTOM-ROW",
        seed=0,
        renderer_actual=renderer_actual,
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


class RendererActualAgreementTest(GenerationTestCase):
    """The high-severity gap: a requested renderer that disagrees with the
    canonical result's own `renderer_actual` must be rejected before any
    rendering is attempted, never allowed to produce a schema-valid but
    self-contradictory pair of on-disk records."""

    def test_a_disagreeing_renderer_is_rejected(self):
        # The fixture's own result claims scalar_field_svg (see
        # load_factor_result's default); requesting series_svg -- a
        # renderer that WOULD have failed for an unrelated reason too,
        # since the result's kind is grid -- must be rejected for the
        # disagreement, not (only) the shape mismatch.
        with self.assertRaises(GenerationError) as caught:
            generate(self.workspace, "W-TQ-LOAD-FACTOR", "series_svg")
        message = str(caught.exception)
        self.assertIn("disagrees with", message)
        self.assertIn("scalar_field_svg", message)
        self.assertIn("series_svg", message)

    def test_the_disagreement_is_caught_even_when_the_requested_renderer_COULD_have_rendered_the_shape(self):
        # Prove this is a genuine pre-check, not an accident of the shape
        # mismatch: build a result whose KIND is scalar (which scalar_svg
        # can render just fine) but whose self-recorded renderer_actual
        # claims scalar_field_svg. Requesting scalar_svg would succeed at
        # the shape level -- and must still be refused, because it
        # disagrees with what the result itself says produced it.
        mismatched = build_result(
            witness_id="W-TQ-LOAD-FACTOR", concept="TaskQueue", query="load_factor",
            fixture_id="FX-QUEUE-BOTTOM-ROW", seed=0,
            renderer_actual="scalar_field_svg",
            result={"kind": "scalar", "value": "0.5"},
        )
        self.write_result(mismatched)
        with self.assertRaises(GenerationError) as caught:
            generate(self.workspace, "W-TQ-LOAD-FACTOR", "scalar_svg")
        self.assertIn("disagrees with", str(caught.exception))
        self.assertFalse((self.workspace / "docs" / "witnesses").exists())

    def test_a_matching_renderer_succeeds(self):
        output = generate(self.workspace, "W-TQ-LOAD-FACTOR", "scalar_field_svg")
        self.assertEqual(output["renderer_actual"], "scalar_field_svg")

    def test_a_self_consistent_but_shape_incompatible_result_still_fails_at_render_time(self):
        # The OTHER boundary: when the requested renderer DOES match the
        # result's own claim, but that claim was itself wrong for the
        # data's actual shape (a producer that mislabeled itself), the
        # failure must still surface -- via the renderer's own kind
        # check, not the agreement check, since the two disagree in
        # substance even though the strings match.
        self_inconsistent = build_result(
            witness_id="W-TQ-LOAD-FACTOR", concept="TaskQueue", query="load_factor",
            fixture_id="FX-QUEUE-BOTTOM-ROW", seed=0,
            renderer_actual="series_svg",
            result=encode_grid(1, 4, [(0, 0, 0.125), (0, 1, 0.25), (0, 2, 0.375), (0, 3, 0.5)]),
        )
        self.write_result(self_inconsistent)
        with self.assertRaises(GenerationError) as caught:
            generate(self.workspace, "W-TQ-LOAD-FACTOR", "series_svg")
        self.assertIn("could not render", str(caught.exception))
        self.assertFalse((self.workspace / "docs" / "witnesses").exists())

    def test_no_witness_ships_with_two_renderers_claiming_the_same_result(self):
        # Sanity check on the test fixtures themselves: encode_series and
        # encode_grid results used across this suite carry distinct
        # kinds, so "disagreement" tests above are exercising the field
        # this gap is about, not an accidental kind mismatch.
        series_result = build_result(
            witness_id="W-X", concept="X", query="y", fixture_id="FX-1", seed=0,
            renderer_actual="series_svg", result=encode_series([("a", 1.0)]),
        )
        self.assertEqual(series_result["result"]["kind"], "series")


class AtomicWriteTest(GenerationTestCase):
    """The medium-severity gap: a write that fails partway through must
    never leave a truncated file at the destination, and must never
    leave a stray temporary file behind either way."""

    def svg_dir(self) -> Path:
        return self.workspace / "docs" / "witnesses"

    def tmp_files(self) -> list[Path]:
        directory = self.svg_dir()
        return [p for p in directory.glob("*.tmp")] if directory.is_dir() else []

    def test_no_temporary_file_remains_after_a_successful_write(self):
        generate(self.workspace, "W-TQ-LOAD-FACTOR", "scalar_field_svg")
        self.assertEqual(self.tmp_files(), [])
        self.assertEqual(len(list(self.svg_dir().iterdir())), 1)

    def test_a_failure_during_the_write_leaves_no_stray_temp_file(self):
        with patch("atomic_write.os.fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                generate(self.workspace, "W-TQ-LOAD-FACTOR", "scalar_field_svg")
        self.assertEqual(self.tmp_files(), [])

    def test_a_failure_during_the_write_does_not_create_a_truncated_destination(self):
        with patch("atomic_write.os.fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                generate(self.workspace, "W-TQ-LOAD-FACTOR", "scalar_field_svg")
        self.assertFalse((self.workspace / "docs" / "witnesses" / "task_queue.load_factor.svg").exists())

    def test_a_failure_during_regeneration_leaves_the_prior_good_file_byte_identical(self):
        first = generate(self.workspace, "W-TQ-LOAD-FACTOR", "scalar_field_svg")
        before = (self.workspace / first["path"]).read_bytes()
        self.write_result(load_factor_result([0.9, 0.9, 0.9, 0.9]))
        with patch("atomic_write.os.fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                generate(self.workspace, "W-TQ-LOAD-FACTOR", "scalar_field_svg")
        after = (self.workspace / first["path"]).read_bytes()
        self.assertEqual(before, after)
        self.assertEqual(self.tmp_files(), [])

    def test_the_temporary_file_is_created_in_the_same_directory_as_the_destination(self):
        # Same-directory placement is what makes the final os.replace an
        # atomic rename rather than a cross-filesystem copy -- assert it
        # directly against the low-level helper rather than only
        # inferring it from cleanup behaviour.
        seen_dirs = []
        real_mkstemp = atomic_write.tempfile.mkstemp

        def spy(*args, **kwargs):
            seen_dirs.append(kwargs.get("dir"))
            return real_mkstemp(*args, **kwargs)

        with patch("atomic_write.tempfile.mkstemp", side_effect=spy):
            generate(self.workspace, "W-TQ-LOAD-FACTOR", "scalar_field_svg")
        destination = self.workspace / "docs" / "witnesses" / "task_queue.load_factor.svg"
        self.assertEqual(seen_dirs, [destination.parent])


class WriteAtomicallyUnitTest(unittest.TestCase):
    """`atomic_write.write_atomically` on its own, away from the rest of generation."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.destination = Path(self._tmp.name) / "out.svg"

    def tearDown(self):
        self._tmp.cleanup()

    def test_content_round_trips(self):
        atomic_write.write_atomically(self.destination, "<svg>hello</svg>")
        self.assertEqual(self.destination.read_text(), "<svg>hello</svg>")

    def test_creates_parent_directories(self):
        nested = Path(self._tmp.name) / "a" / "b" / "out.svg"
        atomic_write.write_atomically(nested, "x")
        self.assertEqual(nested.read_text(), "x")

    def test_a_failure_leaves_no_temp_file_and_propagates(self):
        with patch("atomic_write.os.fsync", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                atomic_write.write_atomically(self.destination, "content")
        self.assertEqual(list(self.destination.parent.glob("*.tmp")), [])
        self.assertFalse(self.destination.exists())

    def test_a_failure_does_not_disturb_a_pre_existing_file(self):
        self.destination.write_text("original")
        with patch("atomic_write.os.fsync", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                atomic_write.write_atomically(self.destination, "replacement")
        self.assertEqual(self.destination.read_text(), "original")


class FailureWritesNothingTest(GenerationTestCase):
    """The load-bearing property: every failure mode leaves the workspace
    exactly as it was, never a partial or degraded artifact."""

    def svg_dir(self) -> Path:
        return self.workspace / "docs" / "witnesses"

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
