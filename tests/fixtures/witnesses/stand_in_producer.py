#!/usr/bin/env python3
"""A stand-in witness producer (chainlink #27).

There is no Rust renderer in this repository, so nothing here can
evaluate a real query on a real fixture -- the same situation as
tests/fixtures/callsites/'s never-compiled Rust (#24) and
tests/fixtures/bridges/verifier/fake_verifier.py (#47). The machinery
under test is real: scripts/witness_result.py canonicalizes the values,
computes the domain and derives the hash. This script stands in for the
thing that produces the values, and is visible as a stand-in rather than
hidden inside the module it feeds.

It evaluates one deliberately simple "query": the load factor of an
8-slot queue whose front `ready` slots are occupied, rendered as a grid
with values only along the bottom row -- which is what makes it a useful
fixture for `coverage_region: bottom-row` and `value_distribution:
must-vary`. `--constant` makes every cell equal, so a degeneracy check
(#31) has something real to fail against.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from witness_result import build_result, encode_grid  # noqa: E402

SLOTS = 8


def load_factors(ready: int, constant: bool) -> list[tuple[int, int, float]]:
    """One value per column of the bottom row: the running load factor as
    the queue fills. Integer arithmetic until the final division, so the
    only float in play is the one being witnessed."""
    cells = []
    for column in range(SLOTS):
        occupied = ready if constant else min(column + 1, ready)
        cells.append((0, column, occupied / SLOTS))
    return cells


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--witness-id", default="W-TQ-LOAD-FACTOR")
    parser.add_argument("--concept", default="TaskQueue")
    parser.add_argument("--query", default="load_factor")
    parser.add_argument("--fixture-id", default="FX-QUEUE-BOTTOM-ROW")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--renderer", default="scalar_field_svg",
                        help="what actually ran; a degraded fallback would report itself here")
    parser.add_argument("--ready", type=int, default=3)
    parser.add_argument("--constant", action="store_true")
    args = parser.parse_args(argv)

    result = encode_grid(1, SLOTS, load_factors(args.ready, args.constant))
    document = build_result(
        witness_id=args.witness_id,
        concept=args.concept,
        query=args.query,
        fixture_id=args.fixture_id,
        seed=args.seed,
        renderer_actual=args.renderer,
        result=result,
    )
    print(json.dumps(document, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
