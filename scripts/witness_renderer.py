#!/usr/bin/env python3
"""The renderer contract: fail loudly, never substitute.

plan.md §16.1/§16.2, chainlink #28. §16.2 names the rule and draws the
boundary in the same breath: "renderer_actual != declared renderer ->
hard error AT GENERATION TIME, NOT THIS GATE" -- the mismatch check
itself belongs to G20 (chainlink #31); what #28 owns is making sure
generation can never produce a mismatch to catch in the first place,
except as a defense-in-depth backstop against a badly written renderer.

The contract, stated as code rather than prose
------------------------------------------------
A renderer is a function of one canonical result (docs/witness-result-schema.json)
that either:
  (a) produces an SVG rendering appropriate to the result's `kind`, or
  (b) raises `RendererError` naming exactly why it could not.

There is no third option. No renderer function in this module contains a
branch that says "if I can't do a field plot, draw a table instead" --
that branch is precisely the "silently substitute a degraded fallback"
plan.md forbids, and it is not merely disallowed by policy here; it does
not exist in the code, the same "total or rejecting" shape chainlink #47
gave the bridge-logic compiler. A future renderer that adds such a branch
is a contract violation review should catch as a diff, not a runtime
condition this module tries to detect.

Two structural defenses against a mismatch reaching disk
----------------------------------------------------------
1. `render()` dispatches to EXACTLY the function registered under the
   declared name -- there is no "closest match" or default renderer to
   silently fall back to. An unregistered name is an immediate
   `RendererError`, before any rendering is attempted.
2. Every renderer function reports its OWN identity as a literal
   constant in its body, not by echoing the name it was called under.
   `render()` cross-checks the two and raises if they disagree. This
   catches the one class of bug fail-loud kind-checking cannot: a
   registry wired to the wrong function (e.g. a copy-paste error that
   left `RENDERERS["scalar_field_svg"]` pointing at `render_series_svg`).
   `renderer_actual` in the written artifact is therefore an independent
   observation of what ran, not an echo of what was requested -- which
   is what makes G20's later declared/actual comparison (#31) meaningful
   rather than tautological.

No renderer here uses a graphics library: this codebase's only runtime
dependency is jsonschema (requirements.txt), and adding one for a
handful of rects and text elements would be a real dependency for a
cosmetic, non-normative artifact (`render_hash` never gates -- plan.md
§16.1). SVG is produced by fixed string templates over values already
in the canonical result; any additional numbers the templates need
(bar heights, cell shading) are computed with `Decimal` and formatted to
a FIXED number of places, never `canonical_number()`'s round-trip
encoding -- that guarantee exists for the normative `value_hash` alone,
and reusing it here would be borrowing a promise this module does not
need to keep. `render_hash` is deliberately non-normative (plan.md
§16.1), so nothing here needs to survive a renderer-library upgrade.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

SVG_WIDTH = 640
SVG_HEIGHT = 160
CELL_SIZE = 48
CELL_GAP = 4


class RendererError(Exception):
    """Raised whenever a renderer cannot honestly produce its declared
    rendering. Never caught and papered over inside this module -- a
    caller sees exactly this, and exactly why, or gets a real SVG."""


@dataclass(frozen=True)
class RenderOutput:
    renderer_actual: str
    svg: str

    @property
    def render_hash(self) -> str:
        """Change-tracking only, never normative (plan.md §16.1) --
        callers must not compare this across runs the way value_hash is
        compared; it exists so a regenerated picture's bytes can be
        diffed, nothing more."""
        return "sha256:" + hashlib.sha256(self.svg.encode("utf-8")).hexdigest()


def _require(result: dict, *keys: str, where: str) -> None:
    missing = [key for key in keys if key not in result]
    if missing:
        raise RendererError(f"{where}: result is missing {missing!r} -- not a shape this renderer accepts")


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _svg_wrapper(width: int, height: int, title: str, body: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">\n'
        f"  <title>{_escape(title)}</title>\n"
        f"{body}"
        "</svg>\n"
    )


def render_scalar_svg(result: dict) -> RenderOutput:
    """Handles `kind: scalar` only. plan.md §16.1's `expectation.coverage_region:
    single-cell` witnesses are exactly this shape."""
    if result.get("kind") != "scalar":
        raise RendererError(
            f"scalar_svg only renders kind 'scalar', not {result.get('kind')!r} -- rendering a "
            "different shape as a single value would misrepresent it, so this fails instead"
        )
    _require(result, "value", where="scalar_svg")
    value = result["value"]
    body = (
        f'  <rect x="0" y="0" width="{SVG_WIDTH}" height="{SVG_HEIGHT}" fill="#f4f4f4" '
        f'stroke="#333333"/>\n'
        f'  <text x="{SVG_WIDTH // 2}" y="{SVG_HEIGHT // 2}" font-size="32" '
        f'text-anchor="middle" dominant-baseline="middle">{_escape(value)}</text>\n'
    )
    return RenderOutput("scalar_svg", _svg_wrapper(SVG_WIDTH, SVG_HEIGHT, "scalar witness", body))


def render_series_svg(result: dict) -> RenderOutput:
    """Handles `kind: series` only -- a bar per point, height proportional
    to the point's position within [min, max] of the series itself. A
    single-point series (min == max) renders every bar full height rather
    than dividing by zero; that is a rendering decision, not a claim
    about degeneracy -- G20 (#31) is where a constant series is judged
    against the declared `value_distribution`, not here."""
    if result.get("kind") != "series":
        raise RendererError(
            f"series_svg only renders kind 'series', not {result.get('kind')!r} -- a field or a "
            "single scalar drawn as a bar sequence would misrepresent its own shape"
        )
    _require(result, "points", where="series_svg")
    points = result["points"]
    if not points:
        raise RendererError("series_svg: a series with no points has nothing to draw")

    values = [Decimal(point["value"]) for point in points]
    minimum, maximum = min(values), max(values)
    span = maximum - minimum

    bar_width = max(1, (SVG_WIDTH - CELL_GAP * (len(points) + 1)) // len(points))
    bars = []
    for index, (point, value) in enumerate(zip(points, values)):
        ratio = Decimal(1) if span == 0 else (value - minimum) / span
        bar_height = int((ratio * (SVG_HEIGHT - 24)).to_integral_value())
        x = CELL_GAP + index * (bar_width + CELL_GAP)
        y = SVG_HEIGHT - 24 - bar_height
        bars.append(
            f'  <rect x="{x}" y="{y}" width="{bar_width}" height="{bar_height}" fill="#3366cc"/>\n'
            f'  <text x="{x + bar_width // 2}" y="{SVG_HEIGHT - 8}" font-size="10" '
            f'text-anchor="middle">{_escape(point["key"])}</text>\n'
        )
    return RenderOutput(
        "series_svg", _svg_wrapper(SVG_WIDTH, SVG_HEIGHT, "series witness", "".join(bars))
    )


def render_scalar_field_svg(result: dict) -> RenderOutput:
    """Handles `kind: grid` only -- a 2-D field of shaded cells, what
    `coverage_region` in a witness spec's `expectation` is a statement
    about. Sparse by design (docs/witness-result-schema.json's own note:
    "a feature defined only along a corridor has cells only there"): a
    row/column pair with no cell is rendered as an explicit
    not-covered hatch, never silently left blank in a way that could be
    mistaken for a zero-valued cell -- a missing value and a zero value
    are different facts, and this renderer does not blur them."""
    if result.get("kind") != "grid":
        raise RendererError(
            f"scalar_field_svg only renders kind 'grid', not {result.get('kind')!r} -- this is "
            "exactly the fallback this contract forbids: drawing a series or a scalar as a field "
            "would be a degraded rendering standing in for the declared shape"
        )
    _require(result, "rows", "columns", "cells", where="scalar_field_svg")
    rows, columns = result["rows"], result["columns"]
    if rows < 1 or columns < 1:
        raise RendererError(f"scalar_field_svg: a {rows}x{columns} grid has no cells to place")

    by_position = {(cell["row"], cell["column"]): cell["value"] for cell in result["cells"]}
    values = [Decimal(v) for v in by_position.values()]
    minimum = min(values) if values else Decimal(0)
    maximum = max(values) if values else Decimal(0)
    span = maximum - minimum

    width = columns * CELL_SIZE + (columns + 1) * CELL_GAP
    height = rows * CELL_SIZE + (rows + 1) * CELL_GAP
    cells_svg = []
    for row in range(rows):
        for column in range(columns):
            x = CELL_GAP + column * (CELL_SIZE + CELL_GAP)
            y = CELL_GAP + row * (CELL_SIZE + CELL_GAP)
            value = by_position.get((row, column))
            if value is None:
                cells_svg.append(
                    f'  <rect x="{x}" y="{y}" width="{CELL_SIZE}" height="{CELL_SIZE}" '
                    f'fill="none" stroke="#999999" stroke-dasharray="4,3"/>\n'
                )
                continue
            decimal_value = Decimal(value)
            ratio = Decimal("0.5") if span == 0 else (decimal_value - minimum) / span
            intensity = int((ratio * 200).to_integral_value())
            color = f"#{255 - intensity:02x}{255 - intensity:02x}ff"
            cells_svg.append(
                f'  <rect x="{x}" y="{y}" width="{CELL_SIZE}" height="{CELL_SIZE}" fill="{color}" '
                f'stroke="#333333"/>\n'
                f'  <text x="{x + CELL_SIZE // 2}" y="{y + CELL_SIZE // 2}" font-size="10" '
                f'text-anchor="middle" dominant-baseline="middle">{_escape(value)}</text>\n'
            )
    return RenderOutput(
        "scalar_field_svg", _svg_wrapper(width, height, "scalar field witness", "".join(cells_svg))
    )


RENDERERS: dict[str, Callable[[dict], RenderOutput]] = {
    "scalar_svg": render_scalar_svg,
    "series_svg": render_series_svg,
    "scalar_field_svg": render_scalar_field_svg,
}


def render(renderer_name: str, result: dict, registry: dict | None = None) -> RenderOutput:
    """The one dispatch point. `registry` is injectable so a test can
    prove the self-report cross-check actually catches a mis-wired
    renderer, without that renderer ever shipping in `RENDERERS`.

    Never falls back: an unregistered name is refused outright, and a
    registered function that raises `RendererError` propagates it
    unchanged. There is no code path here that produces output for a
    name whose function failed or does not exist."""
    registry = RENDERERS if registry is None else registry
    renderer_fn = registry.get(renderer_name)
    if renderer_fn is None:
        raise RendererError(
            f"no renderer registered under {renderer_name!r} (known: {sorted(registry)!r}) -- an "
            "unknown declared renderer is refused outright, never approximated by the closest match"
        )
    output = renderer_fn(result)
    if output.renderer_actual != renderer_name:
        raise RendererError(
            f"renderer registered under {renderer_name!r} reported its own identity as "
            f"{output.renderer_actual!r} -- the registry key and the function's self-report must "
            "agree, or renderer_actual would not be an independent observation of what ran"
        )
    return output
