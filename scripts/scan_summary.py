#!/usr/bin/env python3
"""The one place a scanning validator's success line is written.

chainlink #48. Every discover-then-reject validator in this codebase used
to print the same shape of pass line -- "OK: all evidence records pass
G1a/G1b" -- with no way for a reader (or CI) to tell "I checked real
artifacts and they passed" from "I discovered nothing at all". An empty
workspace produced a clean, confident OK. That is precisely the "zero
findings while checking zero things" failure mode this codebase's own
docstrings warn about, arrived at from the reporting side instead of the
scanning side.

The remedy is a wording distinction, not an exit-code change: an empty
workspace or a crate with none of an artifact kind is a legitimate state
(unlike scripts/gate_r1_g16.py's empty reconciliation, where nothing to
reconcile means a Stage 8A gate would be claiming coverage it never
computed -- that one fails closed). So `pass_line` keeps rc 0 in both
cases and says which situation produced it, and a non-empty pass now
carries the count it was computed from.

Centralized deliberately: the old shape existed at 17 call sites across
scripts/ and pipeline.py, which is exactly how it stayed wrong
everywhere at once. A new validator gets the honest wording by calling
this, not by remembering to.

Each scanning module owns a `count_discovered()` that wraps its OWN
discovery function, so the count can never drift from the set the
validator actually scanned -- a caller re-deriving the glob itself is
how the two silently diverge.
"""
from __future__ import annotations

from pathlib import Path


def pass_line(discovered: int, artifacts: str, checks: str, scanned_under: Path | str) -> str:
    """The success line for a scan that produced no error findings.

    `artifacts` is the plural noun this validator scans ("evidence
    records", "bridges"); `checks` is what passing means for it
    ("G1a/G1b"); `scanned_under` is the root actually walked, named so a
    reader can see whether the scan was pointed where they meant.
    """
    if discovered == 0:
        return f"OK (nothing to check: no {artifacts} discovered under {scanned_under})"
    return f"OK: all {artifacts} pass {checks} ({discovered} discovered)"
