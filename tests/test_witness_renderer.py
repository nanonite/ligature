"""The renderer contract (chainlink #28): fail loudly, never substitute.

plan.md §16.1/§16.2: "A renderer must never silently substitute a
degraded fallback ... it must fail loudly the moment it cannot handle
the declared shape." Most of these tests are therefore about what each
renderer REFUSES -- a renderer that quietly accepted anything would make
`renderer_actual` meaningless, the same shape #47's bridge-logic compiler
tests take against a compiler that accepts anything.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from witness_renderer import (  # noqa: E402
    RENDERERS,
    RendererError,
    render,
    render_scalar_field_svg,
    render_scalar_svg,
    render_series_svg,
)
from witness_result import encode_grid, encode_scalar, encode_series  # noqa: E402

GRID = encode_grid(1, 4, [(0, 0, 0.125), (0, 1, 0.25), (0, 2, 0.375), (0, 3, 0.5)])
SERIES = encode_series([("a", 1.0), ("b", 2.0), ("c", 3.0)])
SCALAR = encode_scalar(0.5)


class DispatchTest(unittest.TestCase):
    def test_every_registered_renderer_reports_its_own_registry_key(self):
        for name in RENDERERS:
            with self.subTest(renderer=name):
                fixture = {"scalar_svg": SCALAR, "series_svg": SERIES, "scalar_field_svg": GRID}[name]
                output = render(name, fixture)
                self.assertEqual(output.renderer_actual, name)

    def test_an_unregistered_renderer_is_refused_not_approximated(self):
        with self.assertRaises(RendererError) as caught:
            render("some_made_up_renderer", GRID)
        self.assertIn("no renderer registered", str(caught.exception))

    def test_a_renderer_error_propagates_unchanged_through_dispatch(self):
        with self.assertRaises(RendererError):
            render("scalar_field_svg", SERIES)

    def test_a_misregistered_renderer_is_caught_by_the_self_report_cross_check(self):
        # A registry wired to the wrong function -- e.g. a copy-paste
        # error that left one name pointing at another renderer's
        # implementation. This never ships in RENDERERS; the point is
        # that IF it did, dispatch would refuse it rather than silently
        # writing an artifact whose renderer_actual lied about what ran.
        miswired = {"scalar_field_svg": render_series_svg}
        with self.assertRaises(RendererError) as caught:
            render("scalar_field_svg", SERIES, registry=miswired)
        self.assertIn("reported its own identity as", str(caught.exception))

    def test_render_hash_is_change_tracking_only_and_computed_from_the_svg_bytes(self):
        import hashlib

        output = render("scalar_svg", SCALAR)
        expected = "sha256:" + hashlib.sha256(output.svg.encode("utf-8")).hexdigest()
        self.assertEqual(output.render_hash, expected)


class ScalarSvgTest(unittest.TestCase):
    def test_renders_a_scalar(self):
        output = render_scalar_svg(SCALAR)
        self.assertEqual(output.renderer_actual, "scalar_svg")
        self.assertIn("0.5", output.svg)
        self.assertTrue(output.svg.startswith("<svg"))

    def test_refuses_a_series(self):
        with self.assertRaises(RendererError) as caught:
            render_scalar_svg(SERIES)
        self.assertIn("only renders kind 'scalar'", str(caught.exception))

    def test_refuses_a_grid(self):
        with self.assertRaises(RendererError):
            render_scalar_svg(GRID)

    def test_refuses_a_malformed_scalar_missing_its_value(self):
        with self.assertRaises(RendererError):
            render_scalar_svg({"kind": "scalar"})

    def test_the_value_text_is_escaped(self):
        # values are always canonical decimal strings in practice, but
        # the renderer must not assume that of arbitrary input it is
        # handed directly (as opposed to through validate_witness.py).
        output = render_scalar_svg({"kind": "scalar", "value": "<script>"})
        self.assertNotIn("<script>", output.svg)
        self.assertIn("&lt;script&gt;", output.svg)


class SeriesSvgTest(unittest.TestCase):
    def test_renders_a_series(self):
        output = render_series_svg(SERIES)
        self.assertEqual(output.renderer_actual, "series_svg")
        for key in ("a", "b", "c"):
            self.assertIn(f">{key}<", output.svg)

    def test_refuses_a_scalar(self):
        with self.assertRaises(RendererError):
            render_series_svg(SCALAR)

    def test_refuses_a_grid(self):
        with self.assertRaises(RendererError):
            render_series_svg(GRID)

    def test_refuses_an_empty_series(self):
        with self.assertRaises(RendererError):
            render_series_svg({"kind": "series", "points": []})

    def test_a_constant_series_does_not_divide_by_zero(self):
        # min == max -- a rendering decision (every bar full height), not
        # a degeneracy verdict; G20 (#31) is where must-vary is judged.
        constant = encode_series([("a", 1.0), ("b", 1.0)])
        output = render_series_svg(constant)
        self.assertIn("<svg", output.svg)


class ScalarFieldSvgTest(unittest.TestCase):
    def test_renders_a_grid(self):
        output = render_scalar_field_svg(GRID)
        self.assertEqual(output.renderer_actual, "scalar_field_svg")
        self.assertEqual(output.svg.count("<rect"), 4)  # one <rect> per cell, 1x4 grid
        self.assertEqual(output.svg.count("<text"), 4)  # every cell has a value to label

    def test_refuses_a_series(self):
        with self.assertRaises(RendererError) as caught:
            render_scalar_field_svg(SERIES)
        self.assertIn("only renders kind 'grid'", str(caught.exception))
        self.assertIn("degraded rendering", str(caught.exception))

    def test_refuses_a_scalar(self):
        with self.assertRaises(RendererError):
            render_scalar_field_svg(SCALAR)

    def test_a_sparse_grid_renders_missing_cells_distinctly(self):
        # "a feature defined only along a corridor has cells only there"
        # (docs/witness-result-schema.json) -- missing must not read as
        # zero.
        sparse = encode_grid(2, 2, [(0, 0, 1.0)])
        output = render_scalar_field_svg(sparse)
        self.assertIn("stroke-dasharray", output.svg)  # the not-covered hatch
        self.assertEqual(output.svg.count("<text"), 1)  # only the one real value is labeled

    def test_a_constant_grid_does_not_divide_by_zero(self):
        constant = encode_grid(1, 3, [(0, 0, 2.0), (0, 1, 2.0), (0, 2, 2.0)])
        output = render_scalar_field_svg(constant)
        self.assertIn("<svg", output.svg)

    def test_dimensions_scale_with_the_grid_shape(self):
        small = render_scalar_field_svg(encode_grid(1, 1, [(0, 0, 1.0)]))
        large = render_scalar_field_svg(encode_grid(3, 3, [(r, c, 1.0) for r in range(3) for c in range(3)]))
        self.assertNotEqual(small.svg, large.svg)


class DeterminismTest(unittest.TestCase):
    """render_hash is never normative (plan.md §16.1), but the renderer
    should still be deterministic for its own sake -- a non-deterministic
    renderer would make change-tracking noisy for no reason."""

    def test_the_same_result_renders_to_identical_bytes(self):
        self.assertEqual(render_scalar_field_svg(GRID).svg, render_scalar_field_svg(GRID).svg)

    def test_a_different_value_changes_the_bytes(self):
        other = encode_grid(1, 4, [(0, 0, 0.999), (0, 1, 0.25), (0, 2, 0.375), (0, 3, 0.5)])
        self.assertNotEqual(render_scalar_field_svg(GRID).svg, render_scalar_field_svg(other).svg)


if __name__ == "__main__":
    unittest.main()
