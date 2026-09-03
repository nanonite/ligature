#!/usr/bin/env python3
"""Boundary-required exemption validation: G1a (schema), G1b (naming).

plan.md §5.2/§7.2, chainlink #16. A reviewed exemption is a separate
object standing in for a boundary artifact -- it never carries a
promotion_id (references are one-way, plan.md §7.1) and it never lives
nested inside the boundary or interaction it relates to.

Chainlink #46 (R2, plan.md gate table §12): check_interaction_cross_reference
below is R2's reference-integrity half -- an exemption only makes sense
if it names a real, eligible interaction (mirrors
scripts/validate_protocol_debt.py's own G2-labeled cross-reference
exactly). R2's *coverage* half (does an eligible interaction actually
have a covering boundary or reviewed exemption) lives in
scripts/validate_interaction.py's check_r2_coverage, fed by
valid_exemption_interaction_ids_from_crate below -- the two modules
together implement R2, neither alone.
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
    required yet at draft time."""
    review_check = check_no_draft_review(path, data)
    if review_check:
        return review_check
    g1a = gate_g1a(path, data, validator)
    if g1a:
        return g1a
    return check_naming(path, data)


def check_interaction_cross_reference(
    path: Path, data: dict, interactions_by_id: dict[str, dict] | None
) -> list[Finding]:
    """R2's reference-integrity half (plan.md gate table §12; chainlink
    #46): an exemption only makes sense if it names a real, eligible
    interaction. Mirrors scripts/validate_protocol_debt.py's own
    G2-labeled check_interaction_cross_reference exactly, including its
    None-means-unchecked handling.

    `interactions_by_id=None` means the caller couldn't determine which
    interactions exist -- a visible info note, not a silent pass.
    `validate_crate` always supplies a real lookup."""
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

    eligibility = interaction.get("eligibility")
    if eligibility != "boundary-required":
        return [
            Finding(
                "G2", path,
                f"interaction_id {interaction_id!r} has eligibility {eligibility!r} -- "
                "an exemption only applies to a boundary-required interaction",
            )
        ]
    return []


def validate_data(
    path: Path, data: dict, validator: Draft202012Validator, interactions_by_id: dict[str, dict] | None = None
) -> list[Finding]:
    g1a = gate_g1a(path, data, validator)
    if g1a:
        return g1a
    findings: list[Finding] = []
    findings.extend(check_naming(path, data))
    findings.extend(check_interaction_cross_reference(path, data, interactions_by_id))
    return findings


def valid_exemption_interaction_ids_from_crate(
    crate_root: Path, canonical_dir: Path, interactions_by_id: dict[str, dict]
) -> set[str]:
    """The set of interaction_ids covered by a fully valid (zero-finding
    -- G1a/G1b/G2, incl. the schema's own `review` requirement) exemption
    directly under `canonical_dir` in this crate -- what
    scripts/validate_interaction.py's check_r2_coverage treats as
    'covered by a reviewed exemption'. Mirrors
    scripts/validate_protocol_debt.py's own
    valid_interaction_ids_from_crate exactly: an exemption naming a
    dangling or ineligible interaction_id (rejected by
    check_interaction_cross_reference above) never contributes coverage,
    which is how a dangling/ineligible exemption reference is rejected
    for R2 purposes, not by a separate mechanism."""
    canonical_resolved = canonical_dir.resolve()
    validator = load_validator()
    covered: set[str] = set()
    for path in sorted(find_exemption_files(crate_root)):
        if path.resolve().parent != canonical_resolved:
            continue
        findings = validate_file(path, validator, interactions_by_id)
        if not findings:
            data = json.loads(path.read_text())
            covered.add(data["interaction_id"])
    return covered


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


def find_exemption_files(root: Path) -> list[Path]:
    """Every file anywhere under a `_exemptions` directory, at any depth
    -- mirrors find_interaction_files/find_boundary_files so nested
    placements surface as G1b violations instead of going unchecked."""
    if not root.is_dir():
        raise FileNotFoundError(f"exemption scan root does not exist or is not a directory: {root}")
    return [p for p in root.glob("**/_exemptions/**/*") if p.is_file()]


def _standalone_interaction_lookup(root: Path) -> dict[Path, dict[str, dict]]:
    """Resolve the interaction lookup for R2's cross-reference from
    canonical sibling directories below root -- mirrors
    scripts/validate_interaction.py's own _standalone_debt_coverage
    exactly (same rationale: the standalone command receives a workspace
    or crate root, not an individual file directory, so its recursive
    discovery can see `specs/_exemptions` and the sibling
    `specs/_interactions`). A missing sibling is an empty lookup, not an
    unknown context: a dangling exemption reference must fail closed.
    Keyed by resolved `_exemptions` directory, so separate crates can't
    borrow one another's interactions."""
    from validate_interaction import load_interactions_by_id

    lookup_by_exemption_dir: dict[Path, dict[str, dict]] = {}
    exemption_dirs = sorted(path for path in root.glob("**/_exemptions") if path.is_dir())
    for exemptions_dir in exemption_dirs:
        resolved_exemptions_dir = exemptions_dir.resolve()
        interactions_dir = exemptions_dir.parent / "_interactions"
        lookup_by_exemption_dir[resolved_exemptions_dir] = (
            load_interactions_by_id(interactions_dir) if interactions_dir.is_dir() else {}
        )
    return lookup_by_exemption_dir


def _exemption_dir_for_path(path: Path) -> Path | None:
    """Return the nearest `_exemptions` ancestor for a discovered file."""
    for parent in (path.parent, *path.parents):
        if parent.name == "_exemptions":
            return parent.resolve()
    return None


def validate(root: Path, interactions_by_id: dict[str, dict] | None = None) -> list[Finding]:
    """Recursive, unanchored scan for the standalone CLI. External review,
    high severity: this used to silently `continue` past any non-.json
    file, so a malformed or wrong-extension artifact under a real
    `_exemptions` directory produced an overall OK -- every file found is
    now validated (check_naming's own suffix check, above, catches the
    well-formed-JSON-but-wrong-extension case).

    If no explicit lookup is supplied, this root-level scan derives one
    per directory from sibling `_interactions/` directories (mirrors
    scripts/validate_interaction.py's own valid_debt_interaction_ids
    handling in its validate()) -- this CLI entrypoint is given enough
    context to fail closed."""
    validator = load_validator()
    lookup_by_exemption_dir = None
    if interactions_by_id is None:
        lookup_by_exemption_dir = _standalone_interaction_lookup(root)
    findings: list[Finding] = []
    for path in find_exemption_files(root):
        if lookup_by_exemption_dir is None:
            lookup = interactions_by_id
        else:
            exemption_dir = _exemption_dir_for_path(path)
            lookup = lookup_by_exemption_dir.get(exemption_dir, {})
        findings.extend(validate_file(path, validator, lookup))
    return findings


def validate_crate(crate_root: Path, canonical_dir: Path, interactions_by_id: dict[str, dict]) -> list[Finding]:
    """The descriptor-driven scan pipeline.py's cmd_validate_exemption
    uses: discovers every exemption-shaped candidate anywhere under
    `crate_root` (same crate-wide find_exemption_files discovery the
    standalone CLI uses), then only fully validates the ones that sit
    directly in the crate's *exact* canonical directory
    (project_descriptor.exemption_dir_for()). Anything else is reported
    as mislocated by an explicit G1b finding.

    Mirrors validate_interaction.py's validate_crate() exactly, including
    its second-review-pass history: an earlier fix (validate_dir(), now
    removed) anchored the scan to only the canonical directory, which
    stopped a mislocated artifact from being wrongly accepted but also
    stopped it from ever being examined at all -- a schema-valid artifact
    under `<crate>/not_specs/_exemptions/` still produced an overall OK.
    This discovers crate-wide precisely so a mislocated one is actually
    found, then rejects it by location alone."""
    canonical_resolved = canonical_dir.resolve()
    validator = load_validator()
    findings: list[Finding] = []
    for path in sorted(find_exemption_files(crate_root)):
        if path.resolve().parent != canonical_resolved:
            findings.append(
                Finding(
                    "G1b", path,
                    f"exemption artifact is not directly under the canonical directory "
                    f"{canonical_resolved} -- found under {path.resolve().parent}",
                )
            )
            continue
        findings.extend(validate_file(path, validator, interactions_by_id))
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

    errors = [f for f in findings if f.severity == "error"]
    infos = [f for f in findings if f.severity == "info"]

    if infos:
        print(f"INFO: {len(infos)} non-blocking finding(s)")
        for f in infos:
            print(f"  - {f}")

    if not errors:
        print("OK: all exemptions pass G1a/G1b (incl. interaction cross-reference)")
        return 0

    print(f"FAIL: {len(errors)} finding(s)")
    for f in errors:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
