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

Cross-referencing that interaction_id names a real, non-pairwise I edge
is checked here too (a G2-labeled finding, mirroring boundary contracts'
own G2 dangling-reference gate): a debt record naming a nonexistent
interaction, or one whose protocol_class is actually pairwise, is
rejected. `validate_data`/`validate_file`/`validate` take an optional
`interactions_by_id` lookup (default None, degrading to a visible info
note rather than a silent pass); `validate_crate` (the descriptor-driven
scan) always requires a real one. G15 itself -- whether a *given*
non-pairwise interaction actually has this coverage -- is checked from
the other direction, in scripts/validate_interaction.py, since that's
where the gate table attributes the finding.

Deliberately still out of scope: independently re-verifying the record's
three attestation fields (no_promoted_obligation_depends_on_protocol,
no_work_package_touches_its_path, no_release_claim_includes_it) against
real promoted state. plan.md's own governance model treats a reviewed,
explicit attestation as authoritative throughout -- the same way boundary
contract review, exemption review, and promotion review are never
independently re-derived by their validators either (plan.md §7.2: human
sign-off is the terminal authority mechanism, not a mechanical proxy for
one). This schema enforces the attestations were explicitly made true
(const: true) and reviewed; it does not attempt partial mechanical
re-verification of some but not all three, which would imply more rigor
than actually exists.
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


def check_interaction_cross_reference(
    path: Path, data: dict, interactions_by_id: dict[str, dict] | None
) -> list[Finding]:
    """External review, high severity: previously nothing checked that
    `interaction_id` names a real interaction, or that the interaction it
    names is actually non-pairwise. A debt record naming a nonexistent
    interaction, and one filed for what is actually a pairwise
    interaction, both reproduced as passing with zero findings before
    this was added.

    `interactions_by_id=None` means the caller couldn't determine which
    interactions exist (mirrors G15's own None-means-unchecked handling
    in scripts/validate_interaction.py) -- a visible info note, not a
    silent pass. `validate_crate` always supplies a real lookup."""
    if interactions_by_id is None:
        return [
            Finding(
                "G2", path,
                "interaction cross-reference was not checked -- no interaction context "
                "was supplied to this validator run",
                severity="info",
            )
        ]

    interaction_id = data["interaction_id"]
    interaction = interactions_by_id.get(interaction_id)
    if interaction is None:
        return [
            Finding(
                "G2", path,
                f"interaction_id {interaction_id!r} does not resolve to any real "
                "interaction -- dangling reference",
            )
        ]

    protocol_class = interaction.get("protocol_class")
    if protocol_class != "non-pairwise":
        return [
            Finding(
                "G2", path,
                f"interaction_id {interaction_id!r} has protocol_class {protocol_class!r} -- "
                "a protocol-debt record only applies to a non-pairwise interaction",
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
    required yet at draft time. Excludes check_interaction_cross_reference
    (G2, needs interactions_by_id -- a cross-file Stage 4 concern)."""
    review_check = check_no_draft_review(path, data)
    if review_check:
        return review_check
    g1a = gate_g1a(path, data, validator)
    if g1a:
        return g1a
    return check_naming(path, data)


def validate_data(
    path: Path,
    data: dict,
    validator: Draft202012Validator,
    interactions_by_id: dict[str, dict] | None = None,
) -> list[Finding]:
    g1a = gate_g1a(path, data, validator)
    if g1a:
        return g1a
    findings: list[Finding] = []
    findings.extend(check_naming(path, data))
    findings.extend(check_interaction_cross_reference(path, data, interactions_by_id))
    return findings


def validate_file(
    path: Path, validator: Draft202012Validator, interactions_by_id: dict[str, dict] | None = None
) -> list[Finding]:
    try:
        text = path.read_text()
    except UnicodeDecodeError as e:
        return [Finding("G1a", path, f"not readable as UTF-8 text: {e}")]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return [Finding("G1a", path, f"invalid JSON: {e}")]
    return validate_data(path, data, validator, interactions_by_id)


def find_protocol_debt_files(root: Path) -> list[Path]:
    """Every file anywhere under a `_protocol_debt` directory, at any
    depth -- mirrors find_exemption_files/find_interaction_files so
    nested placements surface as G1b violations instead of going
    unchecked."""
    if not root.is_dir():
        raise FileNotFoundError(f"protocol-debt scan root does not exist or is not a directory: {root}")
    return [p for p in root.glob("**/_protocol_debt/**/*") if p.is_file()]


def validate(root: Path, interactions_by_id: dict[str, dict] | None = None) -> list[Finding]:
    """Recursive, unanchored scan for the standalone CLI. Every file
    found is validated -- a malformed or wrong-extension artifact under a
    real `_protocol_debt` directory is reported, not silently skipped
    (check_naming's own suffix check catches the well-formed-JSON-but-
    wrong-extension case).

    `interactions_by_id` defaults to None -- this single-root scan has no
    sibling `_interactions/` directory concept, so the cross-reference
    check degrades to a visible info note. Only the descriptor-driven
    `validate_crate` (pipeline.py) makes it a real fail-closed check."""
    validator = load_validator()
    findings: list[Finding] = []
    for path in find_protocol_debt_files(root):
        findings.extend(validate_file(path, validator, interactions_by_id))
    return findings


def validate_crate(crate_root: Path, canonical_dir: Path, interactions_by_id: dict[str, dict]) -> list[Finding]:
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
        findings.extend(validate_file(path, validator, interactions_by_id))
    return findings


def valid_interaction_ids_from_crate(
    crate_root: Path, canonical_dir: Path, interactions_by_id: dict[str, dict]
) -> set[str]:
    """The set of interaction_ids covered by a fully valid (zero-finding)
    protocol-debt record in this crate -- what
    scripts/validate_interaction.py's G15 check treats as 'covered'. Runs
    the same discover-then-validate logic as validate_crate() but returns
    coverage, not findings; used by pipeline.py to wire the two validator
    modules together without making either import the other (mirrors why
    project_descriptor.py exists: pipeline.py already imports both
    validate_interaction.py and this module, so it's the natural place to
    combine them, rather than creating a circular import between the two)."""
    canonical_resolved = canonical_dir.resolve()
    validator = load_validator()
    covered: set[str] = set()
    for path in sorted(find_protocol_debt_files(crate_root)):
        if path.resolve().parent != canonical_resolved:
            continue
        findings = validate_file(path, validator, interactions_by_id)
        if not findings:
            data = json.loads(path.read_text())
            covered.add(data["interaction_id"])
    return covered


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=Path, help="Workspace or crate root to scan for **/_protocol_debt/**/*.json")
    args = parser.parse_args(argv)

    try:
        findings = validate(args.root)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # The interaction cross-reference degrades to an info-severity note
    # here (no sibling _interactions/ directory concept in this single-root
    # scan) -- never a failure, only a visible non-blocking note.
    errors = [f for f in findings if f.severity == "error"]
    infos = [f for f in findings if f.severity == "info"]

    if infos:
        print(f"INFO: {len(infos)} non-blocking finding(s)")
        for f in infos:
            print(f"  - {f}")

    if not errors:
        print("OK: all protocol-debt records pass G1a/G1b")
        return 0

    print(f"FAIL: {len(errors)} finding(s)")
    for f in errors:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
