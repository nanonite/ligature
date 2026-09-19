#!/usr/bin/env python3
"""Evidence record validation: G1a (schema), G1b (naming).

plan.md §11, chainlink #20. Two phases, run in order -- G1b on a
schema-invalid document isn't meaningful.

  G1a  -- draft-2020-12 JSON Schema validation against
          docs/evidence-schema.json.
  G1b  -- repo-semantic checks: filename/layout (flat inside the
          workspace-level `evidence/` directory, id == filename stem,
          same discipline as boundary_id/interaction_id).

Evidence is workspace-level, not crate-scoped: plan.md §7's own
artifact_manifest worked example lists `evidence/E-0143.json` with no
crate prefix, alongside `docs/reliance-policy.md` (also workspace-level).
This means the scan/anchoring functions below take a single workspace
root and a single canonical directory, not a per-crate loop the way
validate_interaction.py/validate_exemption.py/validate_protocol_debt.py
do -- there is exactly one `evidence/` directory per workspace, not one
per crate.

Evidence records deliberately have NO `review` block (see
docs/evidence-schema.json's description: plan.md §7.2's human-checkpoint
list names "evidence-conflict resolution", not evidence itself -- Stage 0
evidence intake is LLM-proposed and non-normative). Consequently this
artifact type is NOT wired into pipeline.py's `approve` dispatcher:
`review_checkpoint.approve()` unconditionally injects a `review` block
into whatever it promotes (see its own docstring), which this schema's
`additionalProperties: false` would reject outright. Evidence records can
be staged via `pipeline.py draft` (Stage 0) but have no promotion path
through `approve()` -- that's a deliberate scope boundary, not an
oversight, the same kind of boundary already drawn for
manifest/promotion-receipt *generation* in `pipeline.py`'s
`NOT_YET_IMPLEMENTED`.

Deliberately out of scope here: G4/G5 (evidence tracing to
obligations/grounding) -- neither is named in chainlink #20's own
description, and both would need cross-referencing interaction
`evidence_links` (#16) to evidence ids, a deeper cross-artifact-type
concern like R2's, not this schema's.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resources  # noqa: E402
from schema_utils import make_validator  # noqa: E402
from scan_summary import pass_line  # noqa: E402

SCHEMA_PATH = resources.resource_path("docs", "evidence-schema.json")


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

    if path.parent.name != "evidence":
        findings.append(
            Finding(
                "G1b", path,
                "not flat -- must live directly inside the workspace-level evidence/ "
                f"directory, found nested under {path.parent}",
            )
        )

    if path.suffix != ".json":
        findings.append(
            Finding("G1b", path, f"must be a .json file, found suffix {path.suffix!r}")
        )

    if path.stem != data["id"]:
        findings.append(
            Finding("G1b", path, f"filename stem {path.stem!r} does not match id {data['id']!r}")
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


def find_evidence_files(root: Path) -> list[Path]:
    """Every file anywhere under an `evidence` directory, at any depth --
    mirrors find_interaction_files/find_exemption_files/find_protocol_debt_files
    so nested placements surface as G1b violations instead of going
    unchecked.

    Raises if `root` doesn't exist: a typo'd workspace path must be a loud
    failure, not a silent 'OK: 0 findings' (same discipline established
    throughout this pipeline)."""
    if not root.is_dir():
        raise FileNotFoundError(f"evidence scan root does not exist or is not a directory: {root}")
    return [p for p in root.glob("**/evidence/**/*") if p.is_file()]


def validate(root: Path) -> list[Finding]:
    """The standalone CLI's entry point. `root` is always the workspace
    root itself here (unlike scripts/validate_interaction.py's standalone
    CLI, which has no crate-boundary concept and is deliberately given
    only an ambiguous scan root) -- there is exactly one legitimate
    `evidence` directory to anchor to: `root / "evidence"`. Delegates
    directly to validate_workspace() so the standalone CLI gets the exact
    same discover-then-reject-by-location behavior, not a separate,
    weaker unanchored scan.

    External review, medium severity: this used to only check a found
    file's IMMEDIATE parent name ('evidence'), never where that directory
    itself sat relative to `root` -- a schema-valid artifact under
    `<root>/docs/evidence/E-0143.json` matched and passed with zero
    findings. Reproduced directly before fixing."""
    return validate_workspace(root, root / "evidence")


def validate_workspace(workspace_root: Path, canonical_dir: Path) -> list[Finding]:
    """The descriptor-driven scan pipeline.py's cmd_validate_evidence uses:
    discovers every evidence-shaped candidate anywhere under
    `workspace_root` (same workspace-wide find_evidence_files discovery the
    standalone CLI uses), then only fully validates the ones that sit
    directly in the workspace's *exact* canonical directory
    (project_descriptor.evidence_dir_for()). Anything else is reported as
    mislocated by an explicit G1b finding -- discovered first, then
    rejected by location, so a mislocated artifact is neither wrongly
    accepted nor invisible (the same discipline validate_interaction.py's/
    validate_exemption.py's/validate_protocol_debt.py's own validate_crate()
    functions use, learned across #16's three review rounds: an
    anchor-only-the-canonical-directory scan silently stops examining
    anything outside it, reproducing the same zero-findings outcome by
    omission instead of false acceptance)."""
    canonical_resolved = canonical_dir.resolve()
    validator = load_validator()
    findings: list[Finding] = []
    for path in sorted(find_evidence_files(workspace_root)):
        if path.resolve().parent != canonical_resolved:
            findings.append(
                Finding(
                    "G1b", path,
                    f"evidence artifact is not directly under the canonical directory "
                    f"{canonical_resolved} -- found under {path.resolve().parent}",
                )
            )
            continue
        findings.extend(validate_file(path, validator))
    return findings


def valid_evidence_ids(evidence_dir: Path) -> set[str]:
    """The set of ids from evidence records directly under `evidence_dir`
    that are themselves fully G1a/G1b valid -- used by
    scripts/validate_conflict_resolution.py's own dangling-reference
    check, mirroring scripts/validate_protocol_debt.py's own
    valid_interaction_ids_from_crate().

    External review, high severity: the previous version (load_evidence_ids)
    trusted any parseable JSON object with a string `id` field -- a file
    containing only `{"id": "E-0143"}` (missing every other required
    field, and never checked for correct naming/placement) satisfied a
    conflict-resolution's cross-reference and let `approve` commit a
    resolved conflict referencing it. Reproduced directly before fixing.
    Fixed by requiring validate_file() to return zero findings before an
    id is trusted -- the same "must be genuinely valid, not just present"
    bar #19's protocol-debt coverage already applies.

    Duplicate ids across different files are excluded rather than trusted
    ambiguously (same discipline as G2+'s own duplicate-concept handling)
    -- check_naming's own flat+id==stem requirement makes this
    structurally rare within one non-recursive directory listing (two
    different filenames can't both stem to the same id and still pass),
    but the guard costs nothing and doesn't assume that invariant holds
    forever."""
    result: dict[str, int] = {}
    if not evidence_dir.is_dir():
        return set()
    validator = load_validator()
    for path in sorted(evidence_dir.glob("*.json")):
        if validate_file(path, validator):
            continue
        data = json.loads(path.read_text())
        result[data["id"]] = result.get(data["id"], 0) + 1
    return {evidence_id for evidence_id, count in result.items() if count == 1}


def count_discovered(root: Path) -> int:
    """The candidate set this module's own scan walks, counted for
    the honest pass line (chainlink #48). Wraps find_evidence_files()
    rather than re-deriving its glob, so the count can never drift
    from the set actually validated."""
    return len(find_evidence_files(root))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=Path, help="Workspace root to scan for **/evidence/**/*.json")
    args = parser.parse_args(argv)

    try:
        findings = validate(args.root)
        discovered = count_discovered(args.root)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if not findings:
        print(pass_line(discovered, "evidence records", "G1a/G1b", args.root))
        return 0

    print(f"FAIL: {len(findings)} finding(s)")
    for f in findings:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
