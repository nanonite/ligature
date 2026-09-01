#!/usr/bin/env python3
"""Boundary-required exemption validation: G1a (schema), G1b (naming).

plan.md §5.2/§7.2, chainlink #16. A reviewed exemption is a separate
object standing in for a boundary artifact -- it never carries a
promotion_id (references are one-way, plan.md §7.1) and it never lives
nested inside the boundary or interaction it relates to.

Deliberately out of scope here (chainlink #21): cross-referencing that
interaction_id names a real, eligible I edge, and that R2's coverage
requirement (boundary OR reviewed exemption) is actually satisfied --
that needs both scripts/validate_interaction.py and this module together
and belongs to the R2 gate, not this one.
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

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "docs" / "exemption-schema.json"


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


def check_naming(path: Path, data: dict) -> list[Finding]:
    findings: list[Finding] = []

    if path.parent.name != "_exemptions":
        findings.append(
            Finding(
                "G1b", path,
                "not flat -- must live directly inside a _exemptions/ directory, "
                f"found nested under {path.parent}",
            )
        )

    if path.suffix != ".json":
        findings.append(
            Finding("G1b", path, f"must be a .json file, found suffix {path.suffix!r}")
        )

    if path.stem != data["interaction_id"]:
        findings.append(
            Finding(
                "G1b", path,
                f"filename stem {path.stem!r} does not match interaction_id {data['interaction_id']!r}",
            )
        )

    return findings


def validate_data(path: Path, data: dict, validator: Draft202012Validator) -> list[Finding]:
    g1a = gate_g1a(path, data, validator)
    if g1a:
        return g1a
    return check_naming(path, data)


def validate_file(path: Path, validator: Draft202012Validator) -> list[Finding]:
    try:
        text = path.read_text()
    except UnicodeDecodeError as e:
        return [Finding("G1a", path, f"not readable as UTF-8 text: {e}")]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return [Finding("G1a", path, f"invalid JSON: {e}")]
    return validate_data(path, data, validator)


def find_exemption_files(root: Path) -> list[Path]:
    """Every file anywhere under a `_exemptions` directory, at any depth
    -- mirrors find_interaction_files/find_boundary_files so nested
    placements surface as G1b violations instead of going unchecked."""
    if not root.is_dir():
        raise FileNotFoundError(f"exemption scan root does not exist or is not a directory: {root}")
    return [p for p in root.glob("**/_exemptions/**/*") if p.is_file()]


def validate(root: Path) -> list[Finding]:
    """Recursive, unanchored scan for the standalone CLI. External review,
    high severity: this used to silently `continue` past any non-.json
    file, so a malformed or wrong-extension artifact under a real
    `_exemptions` directory produced an overall OK -- every file found is
    now validated (check_naming's own suffix check, above, catches the
    well-formed-JSON-but-wrong-extension case)."""
    validator = load_validator()
    findings: list[Finding] = []
    for path in find_exemption_files(root):
        findings.extend(validate_file(path, validator))
    return findings


def validate_dir(exemptions_dir: Path) -> list[Finding]:
    """Validate every file under a crate's *exact*, already-known
    canonical `_exemptions` directory -- no search for a directory
    literally named `_exemptions` anywhere in the crate. Used by
    pipeline.py's descriptor-driven scan (project_descriptor.exemption_dir_for()).
    External review, medium severity: the crate-wide recursive scan above
    only ever checked a found file's IMMEDIATE parent name, never where
    that `_exemptions` directory itself sat relative to the crate root --
    a schema-valid artifact under `<crate>/not_specs/_exemptions/` matched
    and passed with zero findings. Mirrors validate_interaction.py's
    validate_dir(): a crate legitimately having no exemptions yet is not
    an error and returns []; only a missing crate root, checked by the
    caller, is a config mistake worth failing loud on."""
    if not exemptions_dir.is_dir():
        return []
    validator = load_validator()
    findings: list[Finding] = []
    for path in sorted(p for p in exemptions_dir.glob("**/*") if p.is_file()):
        findings.extend(validate_file(path, validator))
    return findings


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=Path, help="Workspace or crate root to scan for **/_exemptions/**/*.json")
    args = parser.parse_args(argv)

    try:
        findings = validate(args.root)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if not findings:
        print("OK: all exemptions pass G1a/G1b")
        return 0

    print(f"FAIL: {len(findings)} finding(s)")
    for f in findings:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
