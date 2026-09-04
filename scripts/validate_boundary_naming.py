#!/usr/bin/env python3
"""Validate boundary-artifact naming and layout (plan.md §2 / chainlink #8).

Rule, byte-for-byte:
  crates/*/specs/_boundaries/<caller_concept>_<caller_method>__to__<callee_concept>_<callee_method>.json
- Flat files only. A boundary artifact nested any deeper than directly inside
  a `_boundaries/` directory is silently ignored by validate_boundary_contracts.py's
  glob("*.json") today -- so nesting isn't a style nit, it's an artifact that
  stops being checked at all. This validator treats it as a hard error instead
  of letting it go quiet.
- Doubled underscore on both sides of `to`. A single underscore does not match.
- boundary_id (inside the JSON) must equal the filename stem, exactly.

This checks naming and layout only -- not the boundary contract's JSON Schema
(G1a) or semantics (G1b, e.g. computed-eligibility mismatch), which depend on
concept-to-code exposing stable obligation identifiers and are tracked
separately (see docs/concept-to-code-modifications.md gap #6).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_summary import pass_line  # noqa: E402

# <segment>__to__<segment>, where each segment is snake_case with no leading/
# trailing/doubled underscore of its own (that would make `__to__` ambiguous
# to detect) and is not itself the literal word "to".
_SEGMENT = r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*"
FILENAME_RE = re.compile(rf"^{_SEGMENT}__to__{_SEGMENT}$")


@dataclass
class Violation:
    path: Path
    reason: str

    def __str__(self) -> str:
        return f"{self.path}: {self.reason}"


def find_boundary_files(root: Path) -> list[Path]:
    """Every file anywhere under a `_boundaries` directory, at any depth --
    deliberately over-broad so nested placements surface as violations
    instead of being silently invisible to this check too.

    Raises if `root` doesn't exist: Path.glob() on a missing directory
    returns an empty iterator with no error, which used to mean a typo'd
    crate_dir in a project descriptor produced "OK: 0 findings" -- silently
    indistinguishable from a real, fully-clean scan (external review
    finding, high severity). A root that exists and legitimately has no
    _boundaries directories yet is fine and returns []; a root that doesn't
    exist at all is a caller mistake, not a clean project."""
    if not root.is_dir():
        raise FileNotFoundError(f"boundary scan root does not exist or is not a directory: {root}")
    return [p for p in root.glob("**/_boundaries/**/*") if p.is_file()]


def check_file(path: Path, data: dict | None = None) -> list[Violation]:
    """`data`, if given, is used instead of re-reading `path` -- lets a
    caller that already parsed the JSON (validate_boundary_contracts.py)
    avoid a redundant disk read, and lets a not-yet-written draft be
    checked against its intended target path before anything is written."""
    violations: list[Violation] = []

    if path.parent.name != "_boundaries":
        violations.append(
            Violation(
                path,
                "not flat -- must live directly inside a _boundaries/ directory, "
                f"found nested under {path.parent}",
            )
        )

    if path.suffix != ".json":
        violations.append(Violation(path, f"not a .json file (suffix: {path.suffix!r})"))
        return violations  # no point parsing JSON below

    stem = path.stem
    if not FILENAME_RE.match(stem):
        violations.append(
            Violation(
                path,
                f"filename stem {stem!r} does not match "
                "<caller_concept>_<caller_method>__to__<callee_concept>_<callee_method> "
                "(doubled underscore required on both sides of 'to')",
            )
        )

    if data is None:
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            violations.append(Violation(path, f"invalid JSON: {e}"))
            return violations

    if not isinstance(data, dict) or "boundary_id" not in data:
        violations.append(Violation(path, "missing required 'boundary_id' field"))
    elif data["boundary_id"] != stem:
        violations.append(
            Violation(
                path,
                f"boundary_id {data['boundary_id']!r} != filename stem {stem!r}",
            )
        )

    return violations


def validate(root: Path) -> list[Violation]:
    violations: list[Violation] = []
    for path in find_boundary_files(root):
        violations.extend(check_file(path))
    return violations


def count_discovered(root: Path) -> int:
    """The candidate set this module's own scan walks, counted for
    the honest pass line (chainlink #48). Wraps find_boundary_files()
    rather than re-deriving its glob, so the count can never drift
    from the set actually validated."""
    return len(find_boundary_files(root))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root",
        type=Path,
        help="Workspace or crate root to scan for **/_boundaries/**/*",
    )
    args = parser.parse_args(argv)

    try:
        violations = validate(args.root)
        discovered = count_discovered(args.root)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if not violations:
        print(pass_line(discovered, "boundary artifacts", "naming/layout checks", args.root))
        return 0

    print(f"FAIL: {len(violations)} boundary naming/layout violation(s)")
    for v in violations:
        print(f"  - {v}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
