#!/usr/bin/env python3
"""Contact sheet generator (chainlink #32): docs/witnesses/_contact_sheet.svg.

plan.md §16.3. Same tier as the feature ledger (§16.4, chainlink #34):
a GENERATED, READ-ONLY review projection, derived entirely from
already-normative or already-generated artifacts, never itself
normative. It is what a human actually looks at at the moment of
promotion -- which is exactly why §16.3 names two fixed controls, not
because the underlying data is unreliable but because a grid of
uniformly green panels reads as proof whether or not it is one:

  1. A fixed banner, exact bytes: "∃-witness evidence — not
     verification."
  2. Each panel carries the owning obligation's `assurance_status` and
     the cluster's `closure_kind` beside the witness/determinism/
     degeneracy dispositions -- a reviewer must see "witness green /
     assurance unsupported" side by side, never read one as implying
     the other.

Never trusts a stale ci/results/feature_ledger.json
------------------------------------------------------
Every panel's status columns come from a fresh call to
`generate_feature_ledger.generate_ledger()` -- the exact same function
`write_ledger()` uses -- never from reading whatever JSON file happens
to already sit on disk. `generate_ledger()` itself is a fresh
recomputation every call (its own docstring: "never a cache of a
previous run"), so this module inherits that guarantee rather than
re-deriving it. The generated ledger is validated against its own
schema before use (`generate_feature_ledger.load_validator()`, reused
directly) -- the same "genuinely valid, not just present" bar
`write_ledger()` already applies before writing it to disk, applied
here before building a review artifact over it.

The two-axis discipline is a rendering rule, not a new computation
------------------------------------------------------------------------
This module computes NOTHING about witness/determinism/degeneracy/
assurance/closure state -- every one of those six facts is read
verbatim off a `generate_ledger()` feature entry. What this module owns
is keeping two rendering axes visually distinct so neither can be
mistaken for the other:

  * `witness_present` / `implementation_observed` / `determinism` /
    `degeneracy` are drawn as colored traffic-light pills (green/amber/
    red/grey) -- a WEAK LIVENESS fact, never a correctness claim.
  * `assurance_status` and `closure_kind` are drawn as plain bordered
    text boxes in a completely disjoint, non-traffic-light palette
    (`AXIS_FILL`/`AXIS_STROKE`/`AXIS_TEXT`) with an explicit
    "assurance:"/"closure:" text prefix -- NEVER colored green,
    regardless of value, so a fully green witness panel cannot be
    scanned as "assurance established" by someone reading colors
    rather than words. This is the concrete mechanism behind plan.md
    §16.3's own rule and chainlink #34's "never merged, never inferred"
    two-column discipline, extended from a JSON field pair to a visual
    one.

Witness rendering: embedded, and rendered from the SAME fresh
evaluation the status columns come from
-----------------------------------------------------------------------
Each panel's thumbnail is embedded directly as a base64
`data:image/svg+xml` `<image>`, never a relative file reference, so
the contact sheet stays a single self-contained artifact -- but WHAT
gets embedded is not simply "whatever bytes happen to already sit at
the witness's output.path" (external review, high severity: an
earlier version did exactly that, trusting `witness_present` alone as
license to read and embed the on-disk file unverified. Reproduced two
ways: a currently-passing witness whose on-disk SVG was stale/
unrelated to what the fresh regeneration actually measured, embedded
as-is beside an honestly green status row; and a currently-passing
witness whose output.path did not even contain valid SVG, embedded
as-is with no placeholder). Fixed as follows, in order of preference:

  1. Whenever `evaluate_ledger()`'s own per-feature evaluation
     produced a genuinely valid, identity-matching regenerated
     `document` (the SAME document determinism/degeneracy were
     computed from -- `FeatureEvaluation.document`, see
     generate_feature_ledger.py), the thumbnail is RENDERED FRESH from
     it: `witness_renderer.render(witness["renderer"], document
     ["result"])`. This is the identical dispatch chainlink #28's own
     `generate_witness.py` makes, over data already in memory -- no
     subprocess, no second witness_backend execution, so this never
     doubles the (potentially expensive, potentially side-effecting)
     external regeneration `evaluate_ledger()` already ran once.
     `identity_mismatches()` (applied inside `check_witness_determinism`
     before `document` is ever returned non-None) already guarantees
     `document["renderer_actual"] == witness["renderer"]`, so this
     dispatch is never a declared/actual mismatch by construction; a
     `RendererError` regardless (a misbehaving backend's result
     content did not actually match its own declared kind) falls back
     to the placeholder rather than propagating, exactly like a
     missing rendering.
  2. Only when no fresh document exists at all (no `witness_backend`
     configured -- determinism itself is honestly `not-checked` in
     this case too) does this module fall back to the on-disk file at
     `witness["output"]["path"]` -- and even then, it is never trusted
     blindly: the bytes must parse as well-formed XML with an `<svg>`
     root before being embedded, or the placeholder is used instead.
     This is the "at minimum" floor: a `witness_backend`-less workspace
     cannot prove the CONTENT is current, but it can still refuse to
     embed something that is not even a picture.
  3. `witness_present: false`, or `witness` is `None`, or every case
     above fails: an explicit dashed-border "rendering unavailable"
     placeholder box. The panel itself is never dropped for a missing
     or untrustworthy rendering -- coverage of the declared feature set
     must stay visible even when what is being reported is absence.

Never a promotion input
---------------------------
Exactly like the feature ledger (§16.6's boundary): this file
participates in no `satisfies()` call, no assurance computation, no
closure computation, and no promotion authority. Nothing in
`pipeline.py`'s `approve()`/`_select_validate_fn` dispatcher recognizes
`docs/witnesses/_contact_sheet.svg` as a normative artifact type, and
nothing here writes to any path `approve()` does recognize.

Determinism
--------------
Panel order is `generate_ledger()`'s own feature order (sorted by
`(concept, query)`, never filesystem discovery order). Every numeric
layout constant is a fixed integer; the only variable-length inputs
(feature identity strings, and each embedded witness SVG's own bytes,
themselves produced by `witness_renderer.py`'s fixed templates) are
escaped and concatenated verbatim, never reformatted -- base64 is a
pure deterministic function of bytes. No timestamp, hostname, or other
environment-dependent value appears anywhere in the output. Two calls
over identical workspace state produce byte-identical SVG text.

Reuses rather than duplicates
---------------------------------
`generate_feature_ledger.evaluate_ledger`/`load_validator` for every
status fact AND every witness/document pair a panel needs -- the
single shared evaluation snapshot described above, never a second
`witness_backend` dispatch; `witness_renderer.render`/`RendererError`
(chainlink #28) to render a thumbnail from a fresh document exactly
the way `generate_witness.py` itself does; `atomic_write.
write_atomically` for the same "failure means no mutation" I/O
guarantee chainlink #28 established; `xml_escape.escape_xml_text`
(chainlink #32, factored out of `witness_renderer.py`'s own private
`_escape` rather than duplicated a second time -- the identical "one
module imports another's helper" precedent `atomic_write.py` already
set).
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from atomic_write import write_atomically  # noqa: E402
from generate_feature_ledger import GenerationError  # noqa: E402
from generate_feature_ledger import evaluate_ledger  # noqa: E402
from generate_feature_ledger import load_validator as load_ledger_validator  # noqa: E402
from witness_renderer import RendererError  # noqa: E402
from witness_renderer import render  # noqa: E402
from xml_escape import escape_xml_text  # noqa: E402

CONTACT_SHEET_RELATIVE_PATH = ("docs", "witnesses", "_contact_sheet.svg")

BANNER_TEXT = "∃-witness evidence — not verification."

EXIT_OK = 0
EXIT_GENERATION_FAILED = 1
EXIT_INPUT_ERROR = 2

# Layout -- every value a fixed integer, no run-to-run variation possible.
MARGIN = 12
BANNER_HEIGHT = 40
PANEL_WIDTH = 760
PANEL_HEIGHT = 132
PANEL_GAP = MARGIN
SHEET_WIDTH = PANEL_WIDTH + 2 * MARGIN
THUMB_WIDTH = 160
THUMB_HEIGHT = 100
THUMB_X = PANEL_WIDTH - THUMB_WIDTH - 20
THUMB_Y = (PANEL_HEIGHT - THUMB_HEIGHT) // 2

# Traffic-light palette -- witness/determinism/degeneracy columns ONLY.
GREEN = "#2e7d32"
AMBER = "#b26a00"
RED = "#c62828"
GREY = "#757575"
PILL_TEXT_COLOR = "#ffffff"

# Deliberately disjoint from the traffic-light palette above --
# assurance_status and closure_kind must never be readable as a green
# pass, at any value.
AXIS_FILL = "#eef1f7"
AXIS_STROKE = "#1a237e"
AXIS_TEXT = "#1a237e"

BANNER_FILL = "#1a1a2e"
BANNER_TEXT_COLOR = "#ffffff"
PANEL_FILL = "#ffffff"
PANEL_STROKE = "#cccccc"
IDENTITY_TEXT_COLOR = "#111111"
PLACEHOLDER_STROKE = "#999999"
PLACEHOLDER_TEXT_COLOR = "#666666"


def contact_sheet_path_for(workspace: Path) -> Path:
    return workspace.joinpath(*CONTACT_SHEET_RELATIVE_PATH)


def _pill_svg(x: int, y: int, width: int, height: int, fill: str, label: str) -> str:
    """A colored traffic-light badge -- witness/implementation/
    determinism/degeneracy columns only. Never used for
    assurance_status or closure_kind (see `_axis_box_svg`)."""
    return (
        f'    <rect x="{x}" y="{y}" width="{width}" height="{height}" rx="6" fill="{fill}"/>\n'
        f'    <text x="{x + width // 2}" y="{y + height // 2}" font-size="12" '
        f'fill="{PILL_TEXT_COLOR}" text-anchor="middle" dominant-baseline="middle">'
        f"{escape_xml_text(label)}</text>\n"
    )


def _axis_box_svg(x: int, y: int, width: int, height: int, label: str) -> str:
    """assurance_status / closure_kind -- a plain bordered text box in a
    fixed, non-traffic-light palette regardless of the value shown, so
    it can never be scanned as a green pass. `label` already carries
    its own "assurance:"/"closure:" prefix so the axis is unmistakable
    even without color."""
    return (
        f'    <rect x="{x}" y="{y}" width="{width}" height="{height}" rx="3" '
        f'fill="{AXIS_FILL}" stroke="{AXIS_STROKE}"/>\n'
        f'    <text x="{x + 8}" y="{y + height // 2}" font-size="12" fill="{AXIS_TEXT}" '
        f'dominant-baseline="middle">{escape_xml_text(label)}</text>\n'
    )


def _thumbnail_svg(x: int, y: int, svg_bytes: bytes | None) -> str:
    if svg_bytes is None:
        return (
            f'    <rect x="{x}" y="{y}" width="{THUMB_WIDTH}" height="{THUMB_HEIGHT}" fill="none" '
            f'stroke="{PLACEHOLDER_STROKE}" stroke-dasharray="4,3"/>\n'
            f'    <text x="{x + THUMB_WIDTH // 2}" y="{y + THUMB_HEIGHT // 2}" font-size="11" '
            f'fill="{PLACEHOLDER_TEXT_COLOR}" text-anchor="middle" dominant-baseline="middle">'
            "rendering unavailable</text>\n"
        )
    encoded = base64.b64encode(svg_bytes).decode("ascii")
    data_uri = f"data:image/svg+xml;base64,{encoded}"
    return (
        f'    <rect x="{x}" y="{y}" width="{THUMB_WIDTH}" height="{THUMB_HEIGHT}" fill="#ffffff" '
        f'stroke="#333333"/>\n'
        f'    <image x="{x}" y="{y}" width="{THUMB_WIDTH}" height="{THUMB_HEIGHT}" '
        f'preserveAspectRatio="xMidYMid meet" xlink:href="{data_uri}" href="{data_uri}"/>\n'
    )


def _witness_pill(x: int, y: int, present: bool) -> str:
    label = "witness: present" if present else "witness: absent"
    return _pill_svg(x, y, 130, 22, GREEN if present else RED, label)


def _implementation_pill(x: int, y: int, observed: bool) -> str:
    label = f"implementation_observed: {str(observed).lower()}"
    return _pill_svg(x, y, 210, 22, GREEN if observed else GREY, label)


def _determinism_pill(x: int, y: int, determinism: str) -> str:
    fill = {"pass": GREEN, "fail": RED, "not-checked": GREY}.get(determinism, GREY)
    return _pill_svg(x, y, 150, 22, fill, f"determinism: {determinism}")


def _degeneracy_pill(x: int, y: int, degeneracy: str) -> str:
    fill = {"ok": GREEN, "warn": AMBER, "not-checked": GREY}.get(degeneracy, GREY)
    return _pill_svg(x, y, 150, 22, fill, f"degeneracy: {degeneracy}")


def panel_svg(y: int, feature: dict, svg_bytes: bytes | None) -> str:
    """One feature's full panel, positioned with its top-left origin at
    sheet-absolute (MARGIN, y). Every field is read verbatim off a
    `generate_ledger()` feature entry -- this function computes no
    witness/determinism/degeneracy/assurance/closure fact itself."""
    left = MARGIN + 12
    parts = [f'  <g transform="translate({MARGIN},{y})">\n']
    parts.append(
        f'    <rect x="0" y="0" width="{PANEL_WIDTH}" height="{PANEL_HEIGHT}" '
        f'fill="{PANEL_FILL}" stroke="{PANEL_STROKE}"/>\n'
    )
    parts.append(
        f'    <text x="12" y="24" font-size="16" font-weight="bold" fill="{IDENTITY_TEXT_COLOR}">'
        f'{escape_xml_text(feature["feature"])}</text>\n'
    )
    parts.append(_witness_pill(12, 40, feature["witness_present"]))
    parts.append(_implementation_pill(154, 40, feature["implementation_observed"]))
    parts.append(_determinism_pill(12, 70, feature["determinism"]))
    parts.append(_degeneracy_pill(174, 70, feature["degeneracy"]))
    parts.append(_axis_box_svg(12, 100, 220, 22, f"assurance: {feature['assurance_status']}"))
    parts.append(
        _axis_box_svg(244, 100, 200, 22, f"cluster: {feature['owning_cluster']} / closure: {feature['closure_kind']}")
    )
    parts.append(_thumbnail_svg(THUMB_X, THUMB_Y, svg_bytes))
    parts.append("  </g>\n")
    return "".join(parts)


def _empty_panel_svg(y: int) -> str:
    return (
        f'  <g transform="translate({MARGIN},{y})">\n'
        f'    <rect x="0" y="0" width="{PANEL_WIDTH}" height="{PANEL_HEIGHT}" '
        f'fill="{PANEL_FILL}" stroke="{PANEL_STROKE}"/>\n'
        f'    <text x="12" y="{PANEL_HEIGHT // 2}" font-size="14" fill="{IDENTITY_TEXT_COLOR}" '
        'dominant-baseline="middle">0 declared features -- nothing to check</text>\n'
        "  </g>\n"
    )


def _banner_svg(width: int) -> str:
    text = escape_xml_text(BANNER_TEXT)
    return (
        f'  <rect x="0" y="0" width="{width}" height="{BANNER_HEIGHT}" fill="{BANNER_FILL}"/>\n'
        f'  <text x="{width // 2}" y="{BANNER_HEIGHT // 2}" font-size="18" font-weight="bold" '
        f'fill="{BANNER_TEXT_COLOR}" text-anchor="middle" dominant-baseline="middle">{text}</text>\n'
    )


def _wrap_svg(width: int, height: int, body: str) -> str:
    title = escape_xml_text("Contact sheet — Stage 4.5 review surface")
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{width}" height="{height}" viewBox="0 0 {width} {height}">\n'
        f"  <title>{title}</title>\n"
        f"{body}"
        "</svg>\n"
    )


def _is_well_formed_svg(raw: bytes) -> bool:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return False
    return root.tag.endswith("svg")


def _thumbnail_bytes_for(evaluation, workspace: Path) -> bytes | None:
    """See this module's own docstring ("Witness rendering: embedded,
    and rendered from the SAME fresh evaluation the status columns come
    from") for why this is not a plain `read_bytes()` of
    `witness["output"]["path"]`."""
    if not evaluation.entry["witness_present"] or evaluation.witness is None:
        return None
    witness = evaluation.witness

    if evaluation.document is not None:
        try:
            output = render(witness["renderer"], evaluation.document["result"])
        except RendererError:
            return None
        return output.svg.encode("utf-8")

    rendering_path = workspace / witness["output"]["path"]
    try:
        raw = rendering_path.read_bytes()
    except OSError:
        return None
    return raw if _is_well_formed_svg(raw) else None


def generate_contact_sheet(workspace: Path, descriptor: dict, runner=subprocess.run) -> tuple[str, int]:
    """Returns (svg_text, declared_feature_count). Raises GenerationError
    -- the SAME exception `generate_feature_ledger.generate_ledger` and
    `write_ledger` raise -- for every condition that leaves the
    underlying ledger unavailable (an ambiguous declaration, or a
    generated ledger that fails its own schema, the identical
    "genuinely valid, not just present" bar `write_ledger` already
    applies before ever writing one to disk)."""
    evaluations, generated_from = evaluate_ledger(workspace, descriptor, runner=runner)
    ledger = {
        "schema_version": "1.0",
        "generated_from": generated_from,
        "features": [evaluation.entry for evaluation in evaluations],
    }
    errors = list(load_ledger_validator().iter_errors(ledger))
    if errors:
        raise GenerationError(
            "generated feature ledger is not schema-valid, refusing to build a contact sheet over "
            f"it: {errors[0].message}"
        )

    panels: list[str] = []
    y = BANNER_HEIGHT + MARGIN
    for evaluation in evaluations:
        svg_bytes = _thumbnail_bytes_for(evaluation, workspace)
        panels.append(panel_svg(y, evaluation.entry, svg_bytes))
        y += PANEL_HEIGHT + PANEL_GAP

    if not evaluations:
        panels.append(_empty_panel_svg(y))
        y += PANEL_HEIGHT + PANEL_GAP

    body = _banner_svg(SHEET_WIDTH) + "".join(panels)
    svg = _wrap_svg(SHEET_WIDTH, y, body)
    return svg, len(evaluations)


def write_contact_sheet(workspace: Path, descriptor: dict, runner=subprocess.run) -> tuple[Path, int]:
    """Generate and write atomically -- a failure partway through
    leaves the previous complete contact sheet byte-identical and no
    temporary file behind (atomic_write.write_atomically, chainlink
    #28's own guarantee)."""
    svg, count = generate_contact_sheet(workspace, descriptor, runner=runner)
    destination = contact_sheet_path_for(workspace)
    write_atomically(destination, svg)
    return destination, count


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--descriptor", type=Path, default=None)
    args = parser.parse_args(argv)

    if not args.workspace.is_dir():
        print(f"error: workspace root does not exist or is not a directory: {args.workspace}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    descriptor_path = args.descriptor or (args.workspace / "project-descriptor.json")
    try:
        descriptor = json.loads(descriptor_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: cannot read project descriptor {descriptor_path}: {e}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    try:
        destination, count = write_contact_sheet(args.workspace, descriptor)
    except GenerationError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_GENERATION_FAILED

    if count == 0:
        print(f"wrote {destination} (0 declared features -- nothing to check)")
    else:
        print(f"wrote {destination} ({count} declared feature(s))")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
