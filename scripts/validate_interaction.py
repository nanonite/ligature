#!/usr/bin/env python3
"""Interaction (I) validation: G1a (schema), G1b (naming + COMPUTED eligibility).

plan.md §5.1/§5.2, chainlink #16. Two phases, run in order -- G1b on a
schema-invalid document isn't meaningful.

  G1a  -- draft-2020-12 JSON Schema validation against
          docs/interaction-schema.json.
  G1b  -- repo-semantic checks: filename/layout (flat inside a
          _interactions/ directory, interaction_id == filename stem, same
          discipline as boundary_id) plus the COMPUTED-eligibility check:
          eligibility is derived from edge_class per plan.md §5.2's table,
          never hand-set, and disagreement between the derived value and
          the stored one is rejected. A proposing model cannot mark a
          stateful cross-verifier edge ignore.

Deliberately out of scope here (later M3 issues): reliances[].required_assurance
(#17), realization/config_scope (#18), protocol_class (#19), and R2's
cross-reference check that an eligible edge is actually covered by a
boundary or a reviewed exemption (#21, needs scripts/validate_exemption.py
too).
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
from schema_utils import make_validator  # noqa: E402

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "docs" / "interaction-schema.json"

# plan.md §5.2. The three buckets partition the schema's edge_class enum
# exactly -- every enum value belongs to exactly one bucket, so any
# schema-valid edge_class array (minItems: 1, enum-only items) always
# resolves to a computed eligibility below. Priority order matters where
# an edge carries more than one class: boundary-required is the most
# conservative requirement and wins over inform, which wins over ignore.
BOUNDARY_REQUIRED_CLASSES = {
    "cross-verifier",
    "cross-crate-public-api",
    "stateful",
    "error-panic-boundary",
    "ownership-transfer",
    "numeric-domain-boundary",
}
INFORM_CLASSES = {"pure-data-type-reference", "import-only"}
IGNORE_CLASSES = {"marker-type", "phantom-type"}


@dataclass
class Finding:
    gate: str
    path: Path
    reason: str
    severity: str = "error"

    def __str__(self) -> str:
        return f"[{self.gate}/{self.severity}] {self.path}: {self.reason}"


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def load_validator() -> Draft202012Validator:
    return make_validator(load_schema())


def gate_g1a(path: Path, data: dict, validator: Draft202012Validator) -> list[Finding]:
    return [Finding("G1a", path, e.message) for e in validator.iter_errors(data)]


def compute_eligibility(edge_class: list[str]) -> str:
    classes = set(edge_class)
    if classes & BOUNDARY_REQUIRED_CLASSES:
        return "boundary-required"
    if classes & INFORM_CLASSES:
        return "inform"
    if classes & IGNORE_CLASSES:
        return "ignore"
    raise ValueError(
        f"edge_class {edge_class!r} contains no class from any known bucket -- "
        "this should be unreachable once G1a's enum has already passed"
    )


def check_naming(path: Path, data: dict) -> list[Finding]:
    findings: list[Finding] = []

    if path.parent.name != "_interactions":
        findings.append(
            Finding(
                "G1b", path,
                "not flat -- must live directly inside a _interactions/ directory, "
                f"found nested under {path.parent}",
            )
        )

    if path.stem != data["interaction_id"]:
        findings.append(
            Finding(
                "G1b", path,
                f"filename stem {path.stem!r} does not match interaction_id {data['interaction_id']!r}",
            )
        )

    return findings


def check_computed_eligibility(path: Path, data: dict) -> list[Finding]:
    computed = compute_eligibility(data["edge_class"])
    stored = data["eligibility"]
    if stored != computed:
        return [
            Finding(
                "G1b", path,
                f"eligibility {stored!r} does not match the value computed from "
                f"edge_class {data['edge_class']!r} (plan.md §5.2: computed {computed!r}) "
                "-- eligibility is never hand-set, and disagreement between the "
                "computed and stored value is rejected",
            )
        ]
    return []


def validate_data(path: Path, data: dict, validator: Draft202012Validator) -> list[Finding]:
    g1a = gate_g1a(path, data, validator)
    if g1a:
        return g1a

    findings: list[Finding] = []
    findings.extend(check_naming(path, data))
    findings.extend(check_computed_eligibility(path, data))
    return findings


def validate_file(path: Path, validator: Draft202012Validator) -> list[Finding]:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        return [Finding("G1a", path, f"invalid JSON: {e}")]
    return validate_data(path, data, validator)


def find_interaction_files(root: Path) -> list[Path]:
    """Every file anywhere under a `_interactions` directory, at any depth --
    deliberately over-broad (mirrors find_boundary_files) so nested
    placements surface as G1b violations instead of being silently
    invisible to a glob("*.json") that only looks one level down.

    Raises if `root` doesn't exist: a typo'd crate_dir in a project
    descriptor must be a loud failure, not a silent 'OK: 0 findings'
    (same discipline established for boundary contracts)."""
    if not root.is_dir():
        raise FileNotFoundError(f"interaction scan root does not exist or is not a directory: {root}")
    return [p for p in root.glob("**/_interactions/**/*") if p.is_file()]


def validate(root: Path) -> list[Finding]:
    validator = load_validator()
    findings: list[Finding] = []
    for path in find_interaction_files(root):
        if path.suffix != ".json":
            continue
        findings.extend(validate_file(path, validator))
    return findings


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=Path, help="Workspace or crate root to scan for **/_interactions/**/*.json")
    args = parser.parse_args(argv)

    try:
        findings = validate(args.root)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if not findings:
        print("OK: all interactions pass G1a/G1b (incl. computed eligibility)")
        return 0

    print(f"FAIL: {len(findings)} finding(s)")
    for f in findings:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
