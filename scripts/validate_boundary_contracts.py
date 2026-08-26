#!/usr/bin/env python3
"""Boundary contract validation: G1a (schema), G1b (repo semantics), G2+ (role safety).

plan.md §2, §12. Three phases, run in order -- each phase only runs on
artifacts that passed the previous one, since a G1b/G2+ check on a
schema-invalid document isn't meaningful.

  G1a  -- draft-2020-12 JSON Schema validation against
          docs/boundary-contract-schema.json.
  G1b  -- repo-semantic checks: filename/layout (delegates to
          validate_boundary_naming.py) plus filename<->body consistency
          (the caller/callee concept+method encoded in the filename must
          match the caller/callee declared in the JSON body) and
          tracking-issue-per-assumption uniqueness within a boundary.
          NOTE: does not include computed-eligibility mismatch -- that's
          an I-schema (interaction) concept, out of scope until M3.
  G2+  -- role safety only (Prototype A scope, plan.md §12): callee_guarantees
          entries must name the *callee's* obligations, must never be an
          adversary case, and (when resolvable) must apply to the declared
          callee method. Never checks a declared assurance target -- that's
          deferred to the I-schema's reliances[].required_assurance (M3).

CAVEAT (docs/concept-to-code-modifications.md gap #6): callee_guarantees
entries reference a constraint by <Concept>.<id>, but concept-to-code does
not yet expose a stable `id` field on constraint. Until that upstream change
lands, cross-file resolution (used by the applies_to check) degrades to
"unverifiable" rather than failing -- see _resolve_constraint below.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_boundary_naming import find_boundary_files  # noqa: E402
from validate_boundary_naming import check_file as check_naming  # noqa: E402

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "docs" / "boundary-contract-schema.json"

OBLIGATION_RE = re.compile(r"^([A-Z][A-Za-z0-9]*)\.C([0-9]{3})$")
ADVERSARY_SHAPED_RE = re.compile(r"^([A-Z][A-Za-z0-9]*)\.A[0-9]+$")


@dataclass
class Finding:
    gate: str
    path: Path
    reason: str

    def __str__(self) -> str:
        return f"[{self.gate}] {self.path}: {self.reason}"


def _pascal_to_snake(name: str) -> str:
    s = re.sub(r"(?<!^)(?=[A-Z])", "_", name)
    return s.lower()


def load_schema() -> dict:
    schema = json.loads(SCHEMA_PATH.read_text())
    Draft202012Validator.check_schema(schema)
    return schema


def gate_g1a(path: Path, data: dict, validator: Draft202012Validator) -> list[Finding]:
    return [
        Finding("G1a", path, e.message) for e in validator.iter_errors(data)
    ]


def gate_g1b(path: Path, data: dict) -> list[Finding]:
    findings: list[Finding] = []

    # Naming/layout, from #8's validator.
    findings.extend(
        Finding("G1b", v.path, v.reason) for v in check_naming(path)
    )

    # Filename <-> body consistency: the caller/callee encoded in the
    # filename must match caller/callee declared in the JSON body.
    caller = data.get("caller") or {}
    callee = data.get("callee") or {}
    if "concept" in caller and "method" in caller and "concept" in callee and "method" in callee:
        expected_stem = (
            f"{_pascal_to_snake(caller['concept'])}_{caller['method']}"
            "__to__"
            f"{_pascal_to_snake(callee['concept'])}_{callee['method']}"
        )
        if path.stem != expected_stem:
            findings.append(
                Finding(
                    "G1b",
                    path,
                    f"filename stem {path.stem!r} does not match caller/callee "
                    f"declared in the body (expected {expected_stem!r})",
                )
            )

    # Tracking-issue uniqueness within this boundary's assumptions.
    seen: dict[str, int] = {}
    for a in data.get("assumptions", []):
        issue = a.get("tracking_issue")
        if issue is None:
            continue
        seen[issue] = seen.get(issue, 0) + 1
    for issue, count in seen.items():
        if count > 1:
            findings.append(
                Finding(
                    "G1b",
                    path,
                    f"tracking_issue {issue!r} used by {count} assumptions in "
                    "the same boundary -- one issue per assumption",
                )
            )

    return findings


def _resolve_constraint(
    concept: str, obligation_id: str, specs_search_root: Path | None
) -> tuple[str, dict | None]:
    """Best-effort resolution of <Concept>.<id> to a constraint dict in the
    callee's concept spec. Returns (status, constraint_or_none).
    status is one of: 'resolved', 'no_search_root', 'spec_not_found',
    'no_ids_in_spec' (gap #6 not applied upstream yet), 'dangling' (G2, not
    this gate's job to fail on, but reported for visibility)."""
    if specs_search_root is None:
        return "no_search_root", None

    candidates = list(specs_search_root.glob(f"**/{_pascal_to_snake(concept)}.json"))
    if not candidates:
        return "spec_not_found", None

    spec = json.loads(candidates[0].read_text())
    constraints = spec.get("constraints", [])
    if constraints and not any("id" in c for c in constraints):
        return "no_ids_in_spec", None

    for c in constraints:
        if c.get("id") == obligation_id:
            return "resolved", c
    return "dangling", None


def gate_g2_plus(
    path: Path, data: dict, specs_search_root: Path | None
) -> list[Finding]:
    findings: list[Finding] = []
    callee = data.get("callee") or {}
    callee_concept = callee.get("concept")
    callee_method = callee.get("method")

    # callee_guarantees may contain raw (pre-G1a-filtered) strings -- this
    # gate runs independently so it can give a specific diagnosis for the
    # named regression case (an adversary case used as a guarantee) rather
    # than a generic schema-pattern mismatch.
    for entry in data.get("callee_guarantees", []):
        if not isinstance(entry, str):
            continue

        adversary_match = ADVERSARY_SHAPED_RE.match(entry)
        if adversary_match:
            findings.append(
                Finding(
                    "G2+",
                    path,
                    f"{entry!r} is an adversary case, not a guarantee -- "
                    "adversary cases are evidence/test seeds only "
                    "(docs/reliance-policy.template.md resolution rule)",
                )
            )
            continue

        match = OBLIGATION_RE.match(entry)
        if not match:
            findings.append(
                Finding(
                    "G2+",
                    path,
                    f"{entry!r} is not a recognizable obligation reference "
                    "(expected <Concept>.C###) -- possible sentinel/placeholder "
                    "value left in callee_guarantees",
                )
            )
            continue

        ref_concept = match.group(1)
        if callee_concept is not None and ref_concept != callee_concept:
            findings.append(
                Finding(
                    "G2+",
                    path,
                    f"{entry!r} names {ref_concept!r}, but this boundary's "
                    f"callee is {callee_concept!r} -- a guarantee must belong "
                    "to the callee, never the caller or an unrelated concept "
                    "(role safety)",
                )
            )
            continue

        status, constraint = _resolve_constraint(ref_concept, entry.split(".", 1)[1], specs_search_root)
        if status == "resolved" and constraint is not None:
            applies_to = constraint.get("applies_to") or []
            if applies_to and callee_method not in applies_to:
                findings.append(
                    Finding(
                        "G2+",
                        path,
                        f"{entry!r} applies_to {applies_to!r}, which does not "
                        f"include this boundary's callee method {callee_method!r}",
                    )
                )
        # 'no_search_root' / 'spec_not_found' / 'no_ids_in_spec' / 'dangling'
        # are all silently skipped here by design -- ref-integrity (dangling)
        # is G2's job, and the others are honest "can't verify yet" states,
        # not G2+ failures.

    return findings


def validate_file(
    path: Path, validator: Draft202012Validator, specs_search_root: Path | None
) -> list[Finding]:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        return [Finding("G1a", path, f"invalid JSON: {e}")]

    g1a = gate_g1a(path, data, validator)
    if g1a:
        # G1b/G2+ need a structurally valid document to mean anything.
        return g1a

    return gate_g1b(path, data) + gate_g2_plus(path, data, specs_search_root)


def validate(root: Path, specs_search_root: Path | None = None) -> list[Finding]:
    schema = load_schema()
    validator = Draft202012Validator(schema)
    findings: list[Finding] = []
    for path in find_boundary_files(root):
        if path.suffix != ".json":
            continue
        findings.extend(validate_file(path, validator, specs_search_root))
    return findings


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=Path, help="Workspace or crate root to scan for **/_boundaries/**/*.json")
    parser.add_argument(
        "--specs-search-root",
        type=Path,
        default=None,
        help="Root to resolve callee concept specs from, for the applies_to check (optional).",
    )
    args = parser.parse_args(argv)

    findings = validate(args.root, args.specs_search_root)
    if not findings:
        print("OK: all boundary contracts pass G1a/G1b/G2+")
        return 0

    print(f"FAIL: {len(findings)} finding(s)")
    for f in findings:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
