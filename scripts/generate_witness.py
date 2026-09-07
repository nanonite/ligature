#!/usr/bin/env python3
"""Witness generation: dispatch a canonical result through the renderer
contract, and write nothing but a well-formed picture.

plan.md §16.1/§16.2, chainlink #28. This is the "generation time" half of
the sentence G20's own gate table entry names: "renderer_actual !=
declared renderer -> hard error at generation, not this gate." That hard
error lives here -- scripts/witness_renderer.py refuses to produce a
mismatched or degraded rendering, and this module is what makes that
refusal visible on the command line and structurally incapable of
leaving a partial artifact behind.

Chronology, and why this does not read a witness spec
-------------------------------------------------------
A witness spec's `output` block (path/render_hash/renderer_actual) is
required by docs/witness-spec-schema.json, which means those values must
exist BEFORE a spec can be drafted -- the natural order is: produce the
canonical result (chainlink #27), generate its rendering (this module),
THEN author the witness spec quoting both. Requiring an already-existing
spec here would have the generator depend on the artifact it is a
precondition for. So this module takes a witness_id and a declared
renderer name directly, reads the ALREADY-VALIDATED canonical result at
ci/results/witnesses/<witness_id>.json (chainlink #27's
load_results_by_witness -- "genuinely valid, not just present"), and
prints the `output` block ready to paste into a draft.

It also never mutates a witness spec on disk. A witness spec is
NORMATIVE and reviewed; a generator quietly rewriting output.render_hash
on an already-promoted spec would edit around review the same way a
model authoring its own `review` block would -- see
scripts/validate_witness.py's own check_no_draft_review.

Fail loud, in this module's own terms
---------------------------------------
Three ways this refuses to write a file:
  * the canonical result does not exist, or is not genuinely valid
    (chainlink #27's own "genuinely valid, not just present" bar);
  * the declared renderer is unregistered;
  * the declared renderer cannot handle the result's shape, or a
    misregistration is caught by witness_renderer.render()'s
    self-report cross-check.
In every one of those cases this writes NOTHING -- not a partial SVG, not
a placeholder, not a degraded picture under an honest label. A witness
whose generation failed has no rendering at all, which is the only
honest state for it to be in.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_witness import load_results_by_witness  # noqa: E402
from validate_witness import snake_case  # noqa: E402
from witness_renderer import RendererError  # noqa: E402
from witness_renderer import render  # noqa: E402

EXIT_OK = 0
EXIT_GENERATION_FAILED = 1
EXIT_INPUT_ERROR = 2


class GenerationError(Exception):
    pass


def output_path_for(concept: str, query: str) -> str:
    """docs/witnesses/<snake_case(concept)>.<query>.svg -- the exact path
    scripts/validate_witness.py's check_output_path requires a promoted
    witness spec to declare, computed the same way so the two can never
    silently diverge."""
    return f"docs/witnesses/{snake_case(concept)}.{query}.svg"


def generate(workspace: Path, witness_id: str, renderer_name: str) -> dict:
    """Returns the `output` block for the caller to paste into a witness
    spec draft, and writes the SVG as a side effect. Raises
    GenerationError -- writing nothing -- for every failure mode this
    module owns."""
    results = load_results_by_witness(workspace)
    result_document = results.get(witness_id)
    if result_document is None:
        raise GenerationError(
            f"no genuinely valid canonical result for witness_id {witness_id!r} under "
            f"ci/results/witnesses/ -- run the fixture producer first, then "
            "`pipeline.py validate-witness --results` to confirm it is valid before generating "
            "from it"
        )

    try:
        rendered = render(renderer_name, result_document["result"])
    except RendererError as e:
        raise GenerationError(
            f"renderer {renderer_name!r} could not render witness {witness_id!r}: {e}"
        )

    path = output_path_for(result_document["concept"], result_document["query"])
    destination = workspace / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(rendered.svg)

    return {
        "path": path,
        "render_hash": rendered.render_hash,
        "renderer_actual": rendered.renderer_actual,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("witness_id", help="e.g. W-TQ-LOAD-FACTOR")
    parser.add_argument("--renderer", required=True, help="the DECLARED renderer to dispatch to")
    parser.add_argument("--workspace", type=Path, default=Path("."))
    args = parser.parse_args(argv)

    if not args.workspace.is_dir():
        print(f"error: workspace root does not exist or is not a directory: {args.workspace}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    try:
        output = generate(args.workspace, args.witness_id, args.renderer)
    except GenerationError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_GENERATION_FAILED

    print(json.dumps(output, indent=2))
    print(f"wrote {args.workspace / output['path']}", file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
