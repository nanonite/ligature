#!/usr/bin/env python3
"""Evidence conflict resolution validation: G1a (schema), G1b (naming +
selected-authority membership + dangling evidence reference), G11
(unresolved conflicts block).

plan.md §11, gate table row G11, chainlink #20. Workspace-level, like
scripts/validate_evidence.py -- not crate-scoped (see that module's own
docstring for why, and the specs/_conflicts/EC-004.json placement in
plan.md §7's artifact_manifest worked example, a sibling of
evidence/E-0143.json with no crate prefix).

  G1a  -- draft-2020-12 JSON Schema validation against
          docs/conflict-resolution-schema.json, including the
          status == "resolved" => resolution + review requirement
          (enforced structurally via the schema's own if/then).
  G1b  -- repo-semantic checks: filename/layout (flat inside the
          workspace-level specs/_conflicts/ directory, conflict_id ==
          filename stem), resolution.selected_authority must be one of
          the ids listed in this record's own `evidence` array (not
          expressible as a plain JSON Schema cross-field constraint), and
          -- when the caller supplies a real evidence-id lookup -- every
          id in `evidence` must resolve to a real evidence record
          (dangling-reference check, mirrors
          scripts/validate_protocol_debt.py's own G2-labeled check).
  G11  -- plan.md's own words: "only unresolved conflicts block."
          status != "resolved" is a hard error at Stage 4 (chainlink #20's
          description names this explicitly, unlike G4/G5 which
          scripts/validate_evidence.py's docstring explains are out of
          scope).

The dangling-evidence-reference check accepts an optional
`evidence_ids: set[str] | None` lookup at the validate_data/validate_file
layer -- None means "not checked in this context" (a visible info-
severity note, never a silent pass), for composition by any caller that
genuinely doesn't have the context.

External review, medium severity: the standalone CLI's own `validate()`/
`main()` used to leave this as None unconditionally, so a resolved
conflict in a workspace with no evidence/ directory at all -- or any
missing referenced id -- printed "OK" with only a non-blocking info
note. This was never structurally necessary the way it is for
scripts/validate_interaction.py's standalone CLI (which is deliberately
given only a `root` with no crate-boundary concept to resolve protocol-
debt coverage against): evidence and conflict-resolution are BOTH
*always* direct workspace-level siblings of the exact same `root`
argument, so `root / "evidence"` is unambiguous, not something this CLI
needs an extra flag or crate context to find. `validate()` now derives
`valid_evidence_ids(root / "evidence")` by default whenever the caller
doesn't supply an explicit set, making the standalone CLI genuinely
fail-closed too, not just the workspace-aware validate_workspace()
(pipeline.py's cmd_validate_conflict_resolution) a caller can already
reach.
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
from validate_evidence import valid_evidence_ids  # noqa: E402

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "docs" / "conflict-resolution-schema.json"


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

    if path.parent.name != "_conflicts":
        findings.append(
            Finding(
                "G1b", path,
                "not flat -- must live directly inside the workspace-level "
                f"specs/_conflicts/ directory, found nested under {path.parent}",
            )
        )

    if path.suffix != ".json":
        findings.append(
            Finding("G1b", path, f"must be a .json file, found suffix {path.suffix!r}")
        )

    if path.stem != data["conflict_id"]:
        findings.append(
            Finding(
                "G1b", path,
                f"filename stem {path.stem!r} does not match conflict_id {data['conflict_id']!r}",
            )
        )

    return findings


def check_selected_authority_membership(path: Path, data: dict) -> list[Finding]:
    """plan.md §11's worked example: resolution.selected_authority
    (E-0201) is one of the ids listed in evidence ([E-0143, E-0201]).
    Selecting an authority that was never even part of the conflict is
    incoherent."""
    resolution = data.get("resolution")
    if resolution is None:
        return []
    selected_authority = resolution.get("selected_authority")
    if selected_authority is None:
        return []
    if selected_authority not in data.get("evidence", []):
        return [
            Finding(
                "G1b", path,
                f"resolution.selected_authority {selected_authority!r} is not one of "
                f"this record's own evidence ids {data.get('evidence')!r}",
            )
        ]
    return []


def check_evidence_cross_reference(
    path: Path, data: dict, evidence_ids: set[str] | None
) -> list[Finding]:
    if evidence_ids is None:
        return [
            Finding(
                "G1b", path,
                "evidence cross-reference was not checked -- no evidence context "
                "was supplied to this validator run",
                severity="info",
            )
        ]
    findings: list[Finding] = []
    for evidence_id in data.get("evidence", []):
        if evidence_id not in evidence_ids:
            findings.append(
                Finding(
                    "G1b", path,
                    f"evidence id {evidence_id!r} does not resolve to any real "
                    "evidence record -- dangling reference",
                )
            )
    return findings


def check_g11_unresolved_conflict(path: Path, data: dict) -> list[Finding]:
    """plan.md gate table §12, G11: 'unresolved evidence conflict' blocks
    promotion at Stage 4. plan.md §11's own words: 'G11 blocks unresolved
    conflicts only' -- resolved ones pass this check (though may still be
    flagged by check_selected_authority_membership/check_evidence_cross_reference
    above if malformed)."""
    if data["status"] != "resolved":
        return [
            Finding(
                "G11", path,
                f"status is {data['status']!r} -- unresolved evidence conflicts block "
                "promotion (plan.md gate table §12: G11)",
            )
        ]
    return []


def validate_data(
    path: Path, data: dict, validator: Draft202012Validator, evidence_ids: set[str] | None = None
) -> list[Finding]:
    g1a = gate_g1a(path, data, validator)
    if g1a:
        return g1a

    findings: list[Finding] = []
    findings.extend(check_naming(path, data))
    findings.extend(check_selected_authority_membership(path, data))
    findings.extend(check_evidence_cross_reference(path, data, evidence_ids))
    findings.extend(check_g11_unresolved_conflict(path, data))
    return findings


def validate_file(
    path: Path, validator: Draft202012Validator, evidence_ids: set[str] | None = None
) -> list[Finding]:
    try:
        text = path.read_text()
    except UnicodeDecodeError as e:
        return [Finding("G1a", path, f"not readable as UTF-8 text: {e}")]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return [Finding("G1a", path, f"invalid JSON: {e}")]
    return validate_data(path, data, validator, evidence_ids)


def find_conflict_files(root: Path) -> list[Path]:
    """Every file anywhere under a `_conflicts` directory, at any depth --
    mirrors scripts/validate_evidence.py's own find_evidence_files."""
    if not root.is_dir():
        raise FileNotFoundError(f"conflict-resolution scan root does not exist or is not a directory: {root}")
    return [p for p in root.glob("**/_conflicts/**/*") if p.is_file()]


def validate(root: Path, evidence_ids: set[str] | None = None) -> list[Finding]:
    """Recursive, unanchored scan for the standalone CLI.

    External review, medium severity: `evidence_ids` used to be left as
    None unconditionally here, so a resolved conflict in a workspace with
    no evidence/ directory at all printed OK with only a non-blocking
    info note -- reproduced directly before fixing. Unlike
    scripts/validate_interaction.py's own standalone CLI (which has no
    crate-boundary concept to resolve protocol-debt coverage against),
    evidence and conflict-resolution are BOTH always direct workspace-
    level siblings of this same `root` -- `root / "evidence"` is
    unambiguous. If the caller doesn't supply an explicit `evidence_ids`,
    it's now derived from that sibling directory, making this scan
    genuinely fail-closed by default; an explicit `set()` or a real set
    still works exactly as before for composition by other callers."""
    if evidence_ids is None:
        evidence_ids = valid_evidence_ids(root / "evidence")
    validator = load_validator()
    findings: list[Finding] = []
    for path in find_conflict_files(root):
        findings.extend(validate_file(path, validator, evidence_ids))
    return findings


def validate_workspace(
    workspace_root: Path, canonical_dir: Path, evidence_ids: set[str]
) -> list[Finding]:
    """The descriptor-driven scan pipeline.py's
    cmd_validate_conflict_resolution uses: discovers every conflict-
    resolution-shaped candidate anywhere under `workspace_root`, then only
    fully validates the ones that sit directly in the workspace's *exact*
    canonical directory. Anything else is reported as mislocated by an
    explicit G1b finding, mirroring
    scripts/validate_evidence.py's/validate_interaction.py's own
    validate_crate()-style functions."""
    canonical_resolved = canonical_dir.resolve()
    validator = load_validator()
    findings: list[Finding] = []
    for path in sorted(find_conflict_files(workspace_root)):
        if path.resolve().parent != canonical_resolved:
            findings.append(
                Finding(
                    "G1b", path,
                    f"conflict-resolution artifact is not directly under the canonical "
                    f"directory {canonical_resolved} -- found under {path.resolve().parent}",
                )
            )
            continue
        findings.extend(validate_file(path, validator, evidence_ids))
    return findings


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=Path, help="Workspace root to scan for **/_conflicts/**/*.json")
    args = parser.parse_args(argv)

    try:
        findings = validate(args.root)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    errors = [f for f in findings if f.severity == "error"]
    infos = [f for f in findings if f.severity == "info"]

    if infos:
        print(f"INFO: {len(infos)} non-blocking finding(s)")
        for f in infos:
            print(f"  - {f}")

    if not errors:
        print("OK: all conflict-resolution records pass G1a/G1b/G11")
        return 0

    print(f"FAIL: {len(errors)} finding(s)")
    for f in errors:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
