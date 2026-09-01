#!/usr/bin/env python3
"""Protocol-debt record validation: G1a (schema), G1b (naming).

plan.md §5.3/§7.2, gate table row G15, chainlink #19. A reviewed
protocol-debt record is a separate object standing in for an external
protocol artifact when an interaction's protocol_class is non-pairwise --
it never carries a promotion_id (references are one-way, plan.md §7.1)
and it never lives nested inside the interaction it relates to. A normal
boundary exemption (docs/exemption-schema.json) is never sufficient for
a temporal/protocol obligation (plan.md §5.3), so this is a distinct
artifact type, not a reuse of exemptions.

Deliberately out of scope here: cross-referencing that interaction_id
names a real, non-pairwise I edge, and that G15's coverage requirement
(protocol artifact OR protocol-debt record) is actually satisfied --
that needs scripts/validate_interaction.py and this module together and
belongs to the G15 gate, not this one. Also deliberately out of scope:
independently re-verifying the record's three attestation fields
(no_promoted_obligation_depends_on_protocol,
no_work_package_touches_its_path, no_release_claim_includes_it) against
real promoted state -- this schema only enforces they were explicitly
attested true (const: true), the same boundary #16 drew for R2/exemptions.
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

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "docs" / "protocol-debt-schema.json"


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

    if path.parent.name != "_protocol_debt":
        findings.append(
            Finding(
                "G1b", path,
                "not flat -- must live directly inside a _protocol_debt/ directory, "
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


def find_protocol_debt_files(root: Path) -> list[Path]:
    """Every file anywhere under a `_protocol_debt` directory, at any
    depth -- mirrors find_exemption_files/find_interaction_files so
    nested placements surface as G1b violations instead of going
    unchecked."""
    if not root.is_dir():
        raise FileNotFoundError(f"protocol-debt scan root does not exist or is not a directory: {root}")
    return [p for p in root.glob("**/_protocol_debt/**/*") if p.is_file()]


def validate(root: Path) -> list[Finding]:
    """Recursive, unanchored scan for the standalone CLI. Every file
    found is validated -- a malformed or wrong-extension artifact under a
    real `_protocol_debt` directory is reported, not silently skipped
    (check_naming's own suffix check catches the well-formed-JSON-but-
    wrong-extension case)."""
    validator = load_validator()
    findings: list[Finding] = []
    for path in find_protocol_debt_files(root):
        findings.extend(validate_file(path, validator))
    return findings


def validate_crate(crate_root: Path, canonical_dir: Path) -> list[Finding]:
    """The descriptor-driven scan pipeline.py's cmd_validate_protocol_debt
    uses: discovers every protocol-debt-shaped candidate anywhere under
    `crate_root` (same crate-wide find_protocol_debt_files discovery the
    standalone CLI uses), then only fully validates the ones that sit
    directly in the crate's *exact* canonical directory
    (project_descriptor.protocol_debt_dir_for()). Anything else is
    reported as mislocated by an explicit G1b finding -- mirrors
    validate_exemption.py's/validate_interaction.py's validate_crate()
    exactly, including their review history: a scan anchored to ONLY the
    canonical directory would stop a mislocated artifact from being
    wrongly accepted, but would also stop it from ever being examined at
    all, reproducing the same zero-findings outcome by omission. This
    discovers crate-wide precisely so a mislocated one is actually found,
    then rejects it by location alone."""
    canonical_resolved = canonical_dir.resolve()
    validator = load_validator()
    findings: list[Finding] = []
    for path in sorted(find_protocol_debt_files(crate_root)):
        if path.resolve().parent != canonical_resolved:
            findings.append(
                Finding(
                    "G1b", path,
                    f"protocol-debt artifact is not directly under the canonical directory "
                    f"{canonical_resolved} -- found under {path.resolve().parent}",
                )
            )
            continue
        findings.extend(validate_file(path, validator))
    return findings


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=Path, help="Workspace or crate root to scan for **/_protocol_debt/**/*.json")
    args = parser.parse_args(argv)

    try:
        findings = validate(args.root)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if not findings:
        print("OK: all protocol-debt records pass G1a/G1b")
        return 0

    print(f"FAIL: {len(findings)} finding(s)")
    for f in findings:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
