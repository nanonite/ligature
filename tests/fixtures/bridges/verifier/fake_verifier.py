#!/usr/bin/env python3
"""A stand-in verifier for exercising bridge dispatch (chainlink #47).

This repository has no Rust crates, so no real Creusot/Kani/Verus run is
possible here -- the same situation as tests/fixtures/callsites/, whose
Rust source exists to pin extractor behaviour and is never compiled. The
dispatch machinery in scripts/gate_g9.py is real; this script stands in
for the verifier at the end of it, and is visible as a stand-in rather
than hidden inside the gate.

It reads the generated harness (so a caller can tell it apart from a
runner that never opened the file) and prints one verdict object, the
protocol scripts/gate_g9.py documents. `--verdict fail` and
`--assumption X` let a test drive the outcomes G9 must react to.
"""
import argparse
import json
import sys
from pathlib import Path


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("harness", type=Path)
    parser.add_argument("--verdict", default="pass", choices=["pass", "fail"])
    parser.add_argument("--assumption", action="append", default=[])
    parser.add_argument("--toolchain", default="nightly-2026-05-01")
    parser.add_argument("--garbage", action="store_true", help="print something that is not a verdict")
    parser.add_argument("--crash", action="store_true")
    args = parser.parse_args(argv)

    if args.crash:
        print("verifier exploded", file=sys.stderr)
        return 3
    if args.garbage:
        print("all good!")
        return 0
    source = args.harness.read_text()
    print(json.dumps({
        "result": args.verdict,
        "scope": {"input_domain": "all", "harness_lines": str(len(source.splitlines()))},
        "assumptions": args.assumption,
        "config": {
            "toolchain": args.toolchain,
            "target": "x86_64-unknown-linux-gnu",
            "features": ["default"],
        },
        "detail": "stand-in verifier; no proof was performed",
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
