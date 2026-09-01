#!/usr/bin/env python3
"""Interaction (I) validation: G1a (schema), G1b (naming + COMPUTED
eligibility + reliance-obligation uniqueness), G2++ (declared assurance
requirement present), G15 (non-pairwise protocol coverage).

plan.md §5.1/§5.2/§5.3/§8.1, gate table §12, chainlink #16/#17/#19.
Phases run in order -- G1b/G2++/G15 on a schema-invalid document isn't
meaningful.

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
  G15   -- non-pairwise protocol without artifact or valid debt record
           blocks promotion (plan.md gate table, §12). A "protocol
           artifact" has no schema yet (docs/protocol-debt-schema.json's
           own description explains why), so the only checkable coverage
           mechanism today is a valid protocol-debt record naming this
           interaction. Fail-closed cross-crate check: the caller (see
           `validate_crate`, always required there) supplies the set of
           interaction_ids covered by a fully valid debt record; a
           `non-pairwise` interaction whose own id isn't in that set is
           rejected. `validate_data`/`validate_file`/`validate` default
           this to `None`, which is NOT the same as "covered" -- it
           degrades to a visible info-severity note ("not checked"), not
           a silent pass, since the single-directory standalone CLI
           genuinely cannot see a sibling `_protocol_debt/` directory
           unless told to.

Deliberately out of scope here (later M3 issues): realization/config_scope
(#18, already landed) is implemented; R2's cross-reference check that an
eligible edge is actually covered by a boundary or a reviewed exemption
remains deferred (needs scripts/validate_exemption.py too, same shape as
G15 above but not yet wired in) -- G2++ only checks that an assurance
requirement is *declared*, not that it resolves to a real boundary or
obligation, which is R2's and G2's job respectively.
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


def check_g15_protocol_coverage(
    path: Path, data: dict, valid_debt_interaction_ids: set[str] | None
) -> list[Finding]:
    """plan.md gate table §12: G15 -- 'non-pairwise protocol without
    artifact or valid debt record' blocks promotion. External review,
    high severity: this was previously deferred entirely (assigned to
    chainlink #21 in NOT_YET_IMPLEMENTED), but #21 is only the I-schema
    milestone gate, not an issue that itself implements gates -- #19's
    own title is 'protocol classification + protocol-debt records, FAIL
    CLOSED', so the gate belongs here. Reproduced directly before fixing:
    a valid interaction switched to protocol_class: non-pairwise produced
    zero findings with no coverage artifact at all.

    `valid_debt_interaction_ids=None` means the caller couldn't determine
    coverage (e.g. the standalone single-directory CLI, which has no
    sibling `_protocol_debt/` directory to consult) -- reported as a
    visible info note, not a silent pass. `validate_crate` (the
    descriptor-driven crate scan pipeline.py uses) always supplies a real
    set, so the crate-wide and approve-time checks are genuinely fail
    closed: an empty set correctly rejects every non-pairwise interaction
    in a crate with no debt records at all, and a set missing this
    specific interaction_id correctly rejects just this one."""
    if data["protocol_class"] != "non-pairwise":
        return []
    if valid_debt_interaction_ids is None:
        return [
            Finding(
                "G15", path,
                "protocol_class is non-pairwise but protocol-debt coverage was not "
                "checked -- no protocol-debt context was supplied to this validator run",
                severity="info",
            )
        ]
    if data["interaction_id"] not in valid_debt_interaction_ids:
        return [
            Finding(
                "G15", path,
                f"protocol_class is non-pairwise but no valid protocol-debt record covers "
                f"interaction_id {data['interaction_id']!r} (plan.md gate table §12: G15 -- "
                "non-pairwise protocol without artifact or valid debt record blocks promotion)",
            )
        ]
    return []


def load_interactions_by_id(interactions_dir: Path) -> dict[str, dict]:
    """Best-effort load of every interaction directly under
    `interactions_dir`, keyed by `interaction_id` -- used to build the
    protocol-debt cross-reference (G15, and validate_protocol_debt.py's
    symmetric check) in both directions. Silently skips anything that
    isn't valid JSON or lacks a string `interaction_id`: full G1a/G1b/
    G2++ validation of these is `validate_crate`'s own job, not this
    loader's -- a schema-invalid interaction can't be a valid coverage
    target either way, so it's simply absent from the lookup rather than
    raising here."""
    result: dict[str, dict] = {}
    if not interactions_dir.is_dir():
        return result
    for path in sorted(interactions_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("interaction_id"), str):
            result[data["interaction_id"]] = data
    return result


def validate_data(
    path: Path,
    data: dict,
    validator: Draft202012Validator,
    valid_debt_interaction_ids: set[str] | None = None,
) -> list[Finding]:
    g1a = gate_g1a(path, data, validator)
    if g1a:
        return g1a

    findings: list[Finding] = []
    findings.extend(check_naming(path, data))
    findings.extend(check_computed_eligibility(path, data))
    findings.extend(check_reliance_obligation_uniqueness(path, data))
    findings.extend(check_g2_plus_plus(path, data))
    findings.extend(check_g15_protocol_coverage(path, data, valid_debt_interaction_ids))
    return findings


def validate_file(
    path: Path, validator: Draft202012Validator, valid_debt_interaction_ids: set[str] | None = None
) -> list[Finding]:
    try:
        text = path.read_text()
    except UnicodeDecodeError as e:
        return [Finding("G1a", path, f"not readable as UTF-8 text: {e}")]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return [Finding("G1a", path, f"invalid JSON: {e}")]
    return validate_data(path, data, validator, valid_debt_interaction_ids)


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


def validate(root: Path, valid_debt_interaction_ids: set[str] | None = None) -> list[Finding]:
    """Recursive, unanchored scan for the standalone CLI: finds every
    `_interactions` directory anywhere under `root`. External review,
    high severity: this used to silently `continue` past any non-.json
    file instead of validating it, so a malformed or wrong-extension
    artifact under a real `_interactions` directory produced an overall
    OK with zero findings -- every file found is now validated (and
    check_naming's own suffix check, above, catches the well-formed-JSON-
    but-wrong-extension case that would otherwise slip past validate_file's
    JSON-parse step).

    `valid_debt_interaction_ids` defaults to None -- this single-root scan
    has no sibling `_protocol_debt/` directory concept, so G15 degrades to
    a visible info note rather than either a silent pass or an incorrect
    hard failure. Only the descriptor-driven `validate_crate` (pipeline.py)
    makes G15 a real fail-closed check."""
    validator = load_validator()
    findings: list[Finding] = []
    for path in find_interaction_files(root):
        findings.extend(validate_file(path, validator, valid_debt_interaction_ids))
    return findings


def validate_crate(crate_root: Path, canonical_dir: Path, valid_debt_interaction_ids: set[str]) -> list[Finding]:
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
        findings.extend(validate_file(path, validator, valid_debt_interaction_ids))
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

    # G15 degrades to an info-severity note here (this single-root scan has
    # no sibling _protocol_debt/ directory concept) -- it must never count
    # as a failure, only a visible non-blocking note, same discipline
    # validate_boundary_contracts.py's main() already uses for its own
    # info-severity findings.
    errors = [f for f in findings if f.severity == "error"]
    infos = [f for f in findings if f.severity == "info"]

    if infos:
        print(f"INFO: {len(infos)} non-blocking finding(s)")
        for f in infos:
            print(f"  - {f}")

    if not errors:
        print("OK: all interactions pass G1a/G1b (incl. computed eligibility)")
        return 0

    print(f"FAIL: {len(errors)} finding(s)")
    for f in errors:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
