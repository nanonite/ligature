#!/usr/bin/env python3
"""Bridge specification validation: G1a (schema), G1b (naming + internal
consistency), G2 (boundary cross-reference).

plan.md §8.2/§8.3, chainlink #22. Three phases, run in order -- G1b/G2 on
a schema-invalid document isn't meaningful.

  G1a -- draft-2020-12 JSON Schema validation against
         docs/bridge-schema.json, including bridge_logic's typed
         bindings/premises/conclusion form (plan.md §8.3) replacing
         prose bridge_logic.
  G1b -- repo-semantic checks: filename/layout (flat inside a
         _bridges/ directory, bridge_id == filename stem, same
         discipline as boundary_id/interaction_id), plus internal
         consistency: callee_requirement must equal
         bridge_logic.conclusion.obligation_id exactly -- the
         human-readable summary field and the machine-checkable
         conclusion can never silently diverge.
  G2  -- reference integrity: boundary_id must resolve to a real,
         valid, promoted boundary contract in the same crate
         (scripts/validate_boundary_contracts.py's own
         load_boundaries_by_id -- "must be genuinely valid, not just
         present", the same trust bar every other cross-reference in
         this pipeline applies), and callee_requirement must be one of
         that boundary's own callee_guarantees -- a bridge cannot
         discharge an obligation the boundary contract never declared.

Deliberately out of scope here (chainlink #22's own follow-up, not yet
filed as a schema concern): generating a verifier harness from
bridge_logic. plan.md §15's own open items list this as unresolved
design territory ("does the typed bridge expression language need a
formal semantics document, or is 'compiles to a harness the owning
verifier checks' sufficient") -- this module only fixes the
representation harness generation would eventually compile from, the
same boundary #45 drew between the promotion-receipt *schema* (#15) and
its *generator* (built separately, later, once the representation was
settled).
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
from schema_utils import make_validator_without_required  # noqa: E402
from validate_boundary_contracts import load_boundaries_by_id  # noqa: E402

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "docs" / "bridge-schema.json"


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

    if path.parent.name != "_bridges":
        findings.append(
            Finding(
                "G1b", path,
                "not flat -- must live directly inside a _bridges/ directory, "
                f"found nested under {path.parent}",
            )
        )

    if path.suffix != ".json":
        findings.append(
            Finding("G1b", path, f"must be a .json file, found suffix {path.suffix!r}")
        )

    if path.stem != data["bridge_id"]:
        findings.append(
            Finding(
                "G1b", path,
                f"filename stem {path.stem!r} does not match bridge_id {data['bridge_id']!r}",
            )
        )

    return findings


def check_conclusion_consistency(path: Path, data: dict) -> list[Finding]:
    """callee_requirement (the human-readable summary of what this bridge
    discharges) and bridge_logic.conclusion.obligation_id (the
    machine-checkable claim) must name the same obligation -- otherwise
    a human reviewing callee_requirement could approve a bridge whose
    typed logic actually proves something else entirely."""
    conclusion = (data.get("bridge_logic") or {}).get("conclusion") or {}
    conclusion_obligation_id = conclusion.get("obligation_id")
    callee_requirement = data.get("callee_requirement")
    if conclusion_obligation_id != callee_requirement:
        return [
            Finding(
                "G1b", path,
                f"callee_requirement {callee_requirement!r} does not match "
                f"bridge_logic.conclusion.obligation_id {conclusion_obligation_id!r} -- "
                "the summary field and the typed conclusion must name the same obligation",
            )
        ]
    return []


def check_boundary_cross_reference(
    path: Path, data: dict, boundaries_by_id: dict[str, dict] | None
) -> list[Finding]:
    """G2: boundary_id must resolve to a real, valid, promoted boundary
    contract, and callee_requirement must be one of that boundary's own
    callee_guarantees -- a bridge cannot discharge an obligation the
    boundary contract never declared. Mirrors
    scripts/validate_protocol_debt.py's own G2-labeled check exactly,
    including its None-means-unchecked handling.

    `boundaries_by_id=None` means the caller couldn't determine which
    boundaries exist -- a visible info note, not a silent pass.
    `validate_crate` always supplies a real lookup."""
    if boundaries_by_id is None:
        return [
            Finding(
                "G2", path,
                "boundary cross-reference was not checked -- no boundary context "
                "was supplied to this validator run",
                severity="info",
            )
        ]

    boundary_id = data["boundary_id"]
    boundary = boundaries_by_id.get(boundary_id)
    if boundary is None:
        return [
            Finding(
                "G2", path,
                f"boundary_id {boundary_id!r} does not resolve to any real "
                "boundary contract -- dangling reference",
            )
        ]

    callee_requirement = data["callee_requirement"]
    if callee_requirement not in boundary.get("callee_guarantees", []):
        return [
            Finding(
                "G2", path,
                f"callee_requirement {callee_requirement!r} is not one of boundary_id "
                f"{boundary_id!r}'s own callee_guarantees {boundary.get('callee_guarantees')!r} -- "
                "a bridge cannot discharge an obligation the boundary contract never declared",
            )
        ]
    return []


def check_no_draft_review(path: Path, data: dict) -> list[Finding]:
    """A Stage 0/3 draft must never carry its own `review` block --
    review_checkpoint.approve() is the only path that attaches one, after
    an explicit, non-empty human reviewer signs off (see its own
    docstring). A model that authors `review` itself is asserting a
    sign-off that never happened."""
    if "review" in data:
        return [
            Finding(
                "G1b", path,
                "draft must not include its own `review` block -- review is only "
                "attached by approve() after a human reviewer signs off",
            )
        ]
    return []


def load_draft_validator() -> Draft202012Validator:
    return make_validator_without_required(load_schema(), "review")


def validate_draft_data(path: Path, data: dict, validator: Draft202012Validator) -> list[Finding]:
    """Stage 0/3 immediate feedback (plan.md §6.1). `validator` must come
    from load_draft_validator(), not load_validator() -- `review` isn't
    required yet at draft time. Includes callee_requirement/conclusion
    consistency (G1b, self-contained). Deliberately excludes G2 (needs
    boundaries_by_id, a cross-file Stage 4 concern) -- mirrors every
    other validate_<type>.py module's own validate_draft_data()."""
    review_check = check_no_draft_review(path, data)
    if review_check:
        return review_check
    g1a = gate_g1a(path, data, validator)
    if g1a:
        return g1a
    findings: list[Finding] = []
    findings.extend(check_naming(path, data))
    findings.extend(check_conclusion_consistency(path, data))
    return findings


def validate_data(
    path: Path, data: dict, validator: Draft202012Validator, boundaries_by_id: dict[str, dict] | None = None
) -> list[Finding]:
    g1a = gate_g1a(path, data, validator)
    if g1a:
        return g1a
    findings: list[Finding] = []
    findings.extend(check_naming(path, data))
    findings.extend(check_conclusion_consistency(path, data))
    findings.extend(check_boundary_cross_reference(path, data, boundaries_by_id))
    return findings


def validate_file(
    path: Path, validator: Draft202012Validator, boundaries_by_id: dict[str, dict] | None = None
) -> list[Finding]:
    try:
        text = path.read_text()
    except UnicodeDecodeError as e:
        return [Finding("G1a", path, f"not readable as UTF-8 text: {e}")]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return [Finding("G1a", path, f"invalid JSON: {e}")]
    return validate_data(path, data, validator, boundaries_by_id)


def find_bridge_files(root: Path) -> list[Path]:
    """Every file anywhere under a `_bridges` directory, at any depth --
    mirrors find_protocol_debt_files/find_exemption_files so nested
    placements surface as G1b violations instead of going unchecked."""
    if not root.is_dir():
        raise FileNotFoundError(f"bridge scan root does not exist or is not a directory: {root}")
    return [p for p in root.glob("**/_bridges/**/*") if p.is_file()]


def validate(root: Path, boundaries_by_id: dict[str, dict] | None = None) -> list[Finding]:
    """Recursive, unanchored scan for the standalone CLI. Every file
    found is validated -- a malformed or wrong-extension artifact under a
    real `_bridges` directory is reported, not silently skipped
    (check_naming's own suffix check catches the well-formed-JSON-but-
    wrong-extension case).

    `boundaries_by_id` defaults to None -- this single-root scan has no
    sibling `_boundaries/` directory concept, so the cross-reference
    check degrades to a visible info note. Only the descriptor-driven
    `validate_crate` (pipeline.py) makes it a real fail-closed check."""
    validator = load_validator()
    findings: list[Finding] = []
    for path in find_bridge_files(root):
        findings.extend(validate_file(path, validator, boundaries_by_id))
    return findings


def validate_crate(crate_root: Path, canonical_dir: Path, boundaries_by_id: dict[str, dict]) -> list[Finding]:
    """The descriptor-driven scan pipeline.py's cmd_validate_bridge uses:
    discovers every bridge-shaped candidate anywhere under `crate_root`
    (same crate-wide find_bridge_files discovery the standalone CLI
    uses), then only fully validates the ones that sit directly in the
    crate's *exact* canonical directory
    (project_descriptor.bridge_dir_for()). Anything else is reported as
    mislocated by an explicit G1b finding -- mirrors every other
    validate_<type>.py module's own validate_crate() exactly, including
    their review history: a scan anchored to ONLY the canonical
    directory would stop a mislocated artifact from being wrongly
    accepted, but would also stop it from ever being examined at all,
    reproducing the same zero-findings outcome by omission. This
    discovers crate-wide precisely so a mislocated one is actually
    found, then rejects it by location alone."""
    canonical_resolved = canonical_dir.resolve()
    validator = load_validator()
    findings: list[Finding] = []
    for path in sorted(find_bridge_files(crate_root)):
        if path.resolve().parent != canonical_resolved:
            findings.append(
                Finding(
                    "G1b", path,
                    f"bridge artifact is not directly under the canonical directory "
                    f"{canonical_resolved} -- found under {path.resolve().parent}",
                )
            )
            continue
        findings.extend(validate_file(path, validator, boundaries_by_id))
    return findings


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=Path, help="Workspace or crate root to scan for **/_bridges/**/*.json")
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
        print("OK: all bridges pass G1a/G1b (incl. conclusion consistency) and G2 boundary cross-reference")
        return 0

    print(f"FAIL: {len(errors)} finding(s)")
    for f in errors:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
