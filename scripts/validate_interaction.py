#!/usr/bin/env python3
"""Interaction (I) validation: G1a (schema), G1b (naming + COMPUTED
eligibility + reliance-obligation uniqueness), G2++ (declared assurance
requirement present).

plan.md §5.1/§5.2/§8.1, gate table §12, chainlink #16/#17. Phases run in
order -- G1b/G2++ on a schema-invalid document isn't meaningful.

  G1a   -- draft-2020-12 JSON Schema validation against
           docs/interaction-schema.json, including §8.1's claim /
           evidence-method / scope / trust type split on
           reliances[].required_assurance (enforced structurally via
           disjoint enums, not custom code).
  G1b   -- repo-semantic checks: filename/layout (flat inside a
           _interactions/ directory, interaction_id == filename stem,
           same discipline as boundary_id), the COMPUTED-eligibility
           check (eligibility is derived from edge_class per plan.md
           §5.2's table, never hand-set, disagreement rejected in both
           directions), and reliance-obligation uniqueness (no two
           reliances in one interaction may name the same obligation_id).
  G2++  -- declared assurance requirement present in I (plan.md's gate
           table, §12): a `boundary-required` interaction must declare
           at least one reliance -- deferred by chainlink #11 until #17's
           reliances[].required_assurance landed, and enforced here now
           that it has. `inform`/`ignore` edges have nothing to declare
           and are exempt (reliances is schema-optional precisely for
           that case).

Deliberately out of scope here (later M3 issues): realization/config_scope
(#18), protocol_class (#19), and R2's cross-reference check that an
eligible edge is actually covered by a boundary or a reviewed exemption
(#21, needs scripts/validate_exemption.py too) -- G2++ only checks that
an assurance requirement is *declared*, not that it resolves to a real
boundary or obligation, which is R2's and G2's job respectively.
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


def check_reliance_obligation_uniqueness(path: Path, data: dict) -> list[Finding]:
    """plan.md §8.1 (#17): one required_assurance per obligation_id within
    a single interaction -- two reliance entries for the same obligation
    could otherwise carry conflicting requirements with no way to tell
    which one governs. Mirrors validate_boundary_contracts.py's
    tracking_issue-uniqueness check."""
    seen: dict[str, int] = {}
    for reliance in data.get("reliances", []):
        obligation_id = reliance.get("obligation_id")
        if obligation_id is None:
            continue
        seen[obligation_id] = seen.get(obligation_id, 0) + 1

    findings: list[Finding] = []
    for obligation_id, count in seen.items():
        if count > 1:
            findings.append(
                Finding(
                    "G1b", path,
                    f"obligation_id {obligation_id!r} appears in {count} reliances -- "
                    "one required_assurance per obligation per interaction",
                )
            )
    return findings


def check_g2_plus_plus(path: Path, data: dict) -> list[Finding]:
    """plan.md's gate table (§12): G2++ -- 'declared assurance requirement
    present in I' (after §5.1 lands, not Prototype A). Chainlink #11
    shipped G2+ scoped to role safety only and explicitly deferred this
    check until #17's reliances[].required_assurance landed; enforced
    here now that it has.

    Only `boundary-required` edges are obligated: `inform`/`ignore` edges
    have nothing to declare a reliance on, which is exactly why
    `reliances` is schema-optional (plan.md §5.1) rather than always
    required. This is not R2's job (#21) -- R2 checks whether an
    eligible interaction is *covered* by a boundary artifact or reviewed
    exemption; G2++ checks, independently, that the interaction itself
    *declares* an assurance requirement at all. An empty `reliances: []`
    is treated the same as an entirely omitted field -- both declare
    nothing."""
    if data["eligibility"] != "boundary-required":
        return []
    if not data.get("reliances"):
        return [
            Finding(
                "G2++", path,
                "eligibility is boundary-required but reliances declares no "
                "required_assurance (plan.md gate table §12: G2++, declared "
                "assurance requirement present in I)",
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
    findings.extend(check_reliance_obligation_uniqueness(path, data))
    findings.extend(check_g2_plus_plus(path, data))
    return findings


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
    """Recursive, unanchored scan for the standalone CLI: finds every
    `_interactions` directory anywhere under `root`. External review,
    high severity: this used to silently `continue` past any non-.json
    file instead of validating it, so a malformed or wrong-extension
    artifact under a real `_interactions` directory produced an overall
    OK with zero findings -- every file found is now validated (and
    check_naming's own suffix check, above, catches the well-formed-JSON-
    but-wrong-extension case that would otherwise slip past validate_file's
    JSON-parse step)."""
    validator = load_validator()
    findings: list[Finding] = []
    for path in find_interaction_files(root):
        findings.extend(validate_file(path, validator))
    return findings


def validate_crate(crate_root: Path, canonical_dir: Path) -> list[Finding]:
    """The descriptor-driven scan pipeline.py's cmd_validate_interaction
    uses: discovers every interaction-shaped candidate anywhere under
    `crate_root` (same crate-wide `find_interaction_files` discovery the
    standalone CLI uses -- any directory literally named `_interactions`,
    at any depth), then only fully validates the ones that sit directly
    in the crate's *exact* canonical directory (project_descriptor's
    interaction_dir_for()). Anything else is reported as mislocated, by
    an explicit G1b finding -- not silently accepted as if it were real,
    and not silently invisible either.

    External review, medium severity, SECOND pass: a first fix
    (validate_dir(), now removed) anchored the scan to ONLY the canonical
    directory and stopped scanning anywhere else. That stopped a
    mislocated artifact from being wrongly VALIDATED, but it also meant
    the scan simply never looked at `<crate>/not_specs/_interactions/`
    at all -- a schema-valid artifact placed there still produced an
    overall OK, reproducing the exact 'zero findings' outcome the
    original review objected to, just via omission instead of false
    acceptance. This discovers candidates crate-wide precisely so a
    mislocated one is actually found, then rejects it by location alone,
    regardless of whether its own content would otherwise be valid."""
    canonical_resolved = canonical_dir.resolve()
    validator = load_validator()
    findings: list[Finding] = []
    for path in sorted(find_interaction_files(crate_root)):
        if path.resolve().parent != canonical_resolved:
            findings.append(
                Finding(
                    "G1b", path,
                    f"interaction artifact is not directly under the canonical directory "
                    f"{canonical_resolved} -- found under {path.resolve().parent}",
                )
            )
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
