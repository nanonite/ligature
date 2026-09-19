#!/usr/bin/env python3
"""Interaction (I) validation: G1a (schema), G1b (naming + COMPUTED
eligibility + reliance-obligation uniqueness), G2++ (declared assurance
requirement present), G15 (non-pairwise protocol coverage), R2 (eligible
edge covered by a boundary or reviewed exemption).

plan.md §5.1/§5.2/§5.3/§8.1, gate table §12, chainlink #16/#17/#19/#46.
Phases run in order -- G1b/G2++/G15/R2 on a schema-invalid document isn't
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
           interaction. Fail-closed cross-crate check: the caller supplies
           the set of interaction_ids covered by a fully valid debt record;
           a `non-pairwise` interaction whose own id isn't in that set is
           rejected. The standalone CLI derives that set recursively from
           the root's sibling `_protocol_debt/` directories, so it also
           rejects uncovered non-pairwise interactions. Lower-level
           `validate_data` calls may still omit context and receive a
           visible info-severity note ("not checked") for composition by
           callers that have not supplied a workspace/crate root.
  R2    -- eligible I edge with no boundary and no reviewed exemption
           blocks promotion (plan.md gate table, §12; chainlink #46).
           "Block by edge class": only `boundary-required` edges are in
           scope -- `inform`/`ignore` edges have nothing to cover, the
           same eligibility gate G2++ uses. Structurally identical to G15
           above: the caller supplies two coverage sources --
           `covering_boundary_edges` (scripts/validate_boundary_contracts.py's
           own valid_boundary_edges_from_crate) and
           `valid_exemption_interaction_ids` (scripts/validate_exemption.py's
           own valid_exemption_interaction_ids_from_crate, which already
           excludes any exemption with a dangling or ineligible
           interaction_id) -- either one alone is sufficient. The
           standalone CLI derives both recursively from sibling
           `_boundaries/`/`_exemptions/` directories. `pipeline.py
           approve-exemption-pair` is the bootstrap path for a new
           interaction and its covering exemption, which otherwise can't
           be approved in either order (each requires the other to
           already be promoted).

realization/config_scope (#18) is implemented; G2++ only checks that an
assurance requirement is *declared*, not that it resolves to a real
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
import resources  # noqa: E402
from schema_utils import make_validator  # noqa: E402
from schema_utils import make_validator_without_required  # noqa: E402
from scan_summary import pass_line  # noqa: E402

SCHEMA_PATH = resources.resource_path("docs", "interaction-schema.json")

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


def check_r2_coverage(
    path: Path,
    data: dict,
    covering_boundary_edges: set[tuple] | None,
    valid_exemption_interaction_ids: set[str] | None,
) -> list[Finding]:
    """plan.md gate table §12: R2 -- 'eligible I edge with no boundary
    and no reviewed exemption' blocks promotion, 'block by edge class'
    (only boundary-required edges are in scope -- inform/ignore edges
    have nothing to cover, mirroring check_g2_plus_plus's own eligibility
    gate). Chainlink #46. Structurally identical to check_g15_protocol_coverage
    above, per plan.md §11's own note ("G15 itself... is a cross-file
    coverage check, structurally identical to R2's own") -- same
    None-means-unchecked handling, same fail-closed-on-empty-set
    discipline once a real crate context is supplied.

    `covering_boundary_edges` comes from
    scripts/validate_boundary_contracts.py's own
    valid_boundary_edges_from_crate; `valid_exemption_interaction_ids`
    comes from scripts/validate_exemption.py's own
    valid_exemption_interaction_ids_from_crate (which already excludes
    any exemption with a dangling or ineligible interaction_id -- see
    that module's check_interaction_cross_reference). Either one alone
    is sufficient coverage; only an interaction with NEITHER is rejected."""
    if data["eligibility"] != "boundary-required":
        return []
    if covering_boundary_edges is None or valid_exemption_interaction_ids is None:
        return [
            Finding(
                "R2", path,
                "eligibility is boundary-required but R2 coverage was not checked -- "
                "no boundary/exemption context was supplied to this validator run",
                severity="info",
            )
        ]
    caller = data.get("caller") or {}
    callee = data.get("callee") or {}
    edge = (caller.get("concept"), caller.get("method"), callee.get("concept"), callee.get("method"))
    if edge in covering_boundary_edges:
        return []
    if data["interaction_id"] in valid_exemption_interaction_ids:
        return []
    return [
        Finding(
            "R2", path,
            f"eligibility is boundary-required but no boundary contract for this edge and no "
            f"reviewed exemption covers interaction_id {data['interaction_id']!r} "
            "(plan.md gate table §12: R2 -- eligible I edge with no boundary and no reviewed "
            "exemption blocks promotion)",
        )
    ]


class InteractionLookup(dict[str, dict]):
    """A lookup plus the IDs invalidated by duplicate candidates.

    The mapping intentionally contains *only* approved, schema-valid,
    canonically named interactions.  ``duplicate_ids`` is retained for
    callers that need to explain or preserve fail-closed behavior while the
    public mapping remains an ordinary ``dict`` for existing callers.
    """

    def __init__(self):
        super().__init__()
        self.duplicate_ids: set[str] = set()


def load_interactions_by_id(interactions_dir: Path) -> InteractionLookup:
    """Load only approved, fully valid canonical interactions.

    This lookup is used for the protocol-debt cross-reference in both
    directions.  JSON parsing alone is not sufficient: a debt record must
    never be able to treat a schema-invalid, misnamed, unreviewed, or
    duplicate interaction as real.  Invalid candidates are omitted and an
    ID appearing in more than one direct child is omitted even when one of
    those candidates would otherwise validate.
    """
    result = InteractionLookup()
    if not interactions_dir.is_dir():
        return result

    validator = load_validator()
    candidates: dict[str, list[tuple[Path, dict]]] = {}
    for path in sorted(
        p for p in interactions_dir.iterdir() if p.is_file() and p.suffix != ".draft"
    ):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("interaction_id"), str):
            candidates.setdefault(data["interaction_id"], []).append((path, data))

    result.duplicate_ids = {interaction_id for interaction_id, entries in candidates.items() if len(entries) > 1}

    for interaction_id, entries in candidates.items():
        if interaction_id in result.duplicate_ids:
            continue
        path, data = entries[0]
        # Supplying this interaction's own ID only suppresses G15.  G15 is
        # the cross-file coverage gate; the loader is establishing the
        # intrinsic validity of a target before another artifact may refer
        # to it.
        findings = validate_data(path, data, validator, {interaction_id})
        if any(getattr(finding, "severity", "error") == "error" for finding in findings):
            continue
        review = data.get("review")
        if not isinstance(review, dict) or not review.get("reviewer") or not review.get("reviewed_at"):
            continue
        result[interaction_id] = data
    return result


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
    required yet at draft time. Includes computed-eligibility and
    reliance-obligation-uniqueness (both G1b, self-contained, no
    cross-file lookup -- plan.md's own "cheap ... eligibility checks"
    language). Deliberately excludes G2++ (declared-assurance) and G15
    (protocol coverage) -- both are plan.md gate-table entries distinct
    from G1a/G1b, and G15 specifically needs cross-file protocol-debt
    context this function is never given. External review, high
    severity: the first version of this dispatcher reused approve()'s
    full validate_data(), which rejected every review-less draft outright
    via G1a; reproduced directly before this fix."""
    review_check = check_no_draft_review(path, data)
    if review_check:
        return review_check
    g1a = gate_g1a(path, data, validator)
    if g1a:
        return g1a
    findings: list[Finding] = []
    findings.extend(check_naming(path, data))
    findings.extend(check_computed_eligibility(path, data))
    findings.extend(check_reliance_obligation_uniqueness(path, data))
    return findings


def validate_data(
    path: Path,
    data: dict,
    validator: Draft202012Validator,
    valid_debt_interaction_ids: set[str] | None = None,
    covering_boundary_edges: set[tuple] | None = None,
    valid_exemption_interaction_ids: set[str] | None = None,
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
    findings.extend(check_r2_coverage(path, data, covering_boundary_edges, valid_exemption_interaction_ids))
    return findings


def validate_file(
    path: Path,
    validator: Draft202012Validator,
    valid_debt_interaction_ids: set[str] | None = None,
    covering_boundary_edges: set[tuple] | None = None,
    valid_exemption_interaction_ids: set[str] | None = None,
) -> list[Finding]:
    try:
        text = path.read_text()
    except UnicodeDecodeError as e:
        return [Finding("G1a", path, f"not readable as UTF-8 text: {e}")]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return [Finding("G1a", path, f"invalid JSON: {e}")]
    return validate_data(
        path, data, validator, valid_debt_interaction_ids, covering_boundary_edges, valid_exemption_interaction_ids
    )


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


def _standalone_debt_coverage(root: Path) -> dict[Path, set[str]]:
    """Resolve G15 coverage from canonical sibling directories below root.

    The standalone command receives a workspace or crate root, not an
    individual file directory.  Its recursive discovery can therefore see
    ``specs/_interactions`` and the sibling ``specs/_protocol_debt``.  A
    missing sibling is an empty coverage set, not an unknown context: an
    uncovered non-pairwise interaction must fail closed.  The result is
    keyed by resolved `_interactions` directory, so equal IDs in separate
    crates cannot borrow one another's coverage.
    """
    from validate_protocol_debt import valid_interaction_ids_from_crate

    coverage_by_interaction_dir: dict[Path, set[str]] = {}
    interaction_dirs = sorted(path for path in root.glob("**/_interactions") if path.is_dir())
    for interactions_dir in interaction_dirs:
        resolved_interactions_dir = interactions_dir.resolve()
        coverage_by_interaction_dir[resolved_interactions_dir] = set()
        interactions_by_id = load_interactions_by_id(interactions_dir)
        debt_dir = interactions_dir.parent / "_protocol_debt"
        if debt_dir.is_dir():
            coverage_by_interaction_dir[resolved_interactions_dir].update(
                valid_interaction_ids_from_crate(root, debt_dir, interactions_by_id)
            )
    return coverage_by_interaction_dir


def _standalone_r2_coverage(root: Path) -> tuple[dict[Path, set[tuple]], dict[Path, set[str]]]:
    """Resolve R2 coverage from canonical sibling directories below root
    -- mirrors _standalone_debt_coverage exactly, for R2's two coverage
    sources instead of G15's one. A missing `_boundaries`/`_exemptions`
    sibling contributes an empty set for that source, not an unknown
    context: an uncovered eligible interaction must fail closed. Keyed
    by resolved `_interactions` directory, so separate crates can't
    borrow one another's coverage."""
    from validate_boundary_contracts import valid_boundary_edges_from_crate
    from validate_exemption import valid_exemption_interaction_ids_from_crate

    boundary_edges_by_dir: dict[Path, set[tuple]] = {}
    exemption_ids_by_dir: dict[Path, set[str]] = {}
    interaction_dirs = sorted(path for path in root.glob("**/_interactions") if path.is_dir())
    for interactions_dir in interaction_dirs:
        resolved_interactions_dir = interactions_dir.resolve()
        boundary_edges_by_dir[resolved_interactions_dir] = set()
        exemption_ids_by_dir[resolved_interactions_dir] = set()
        boundary_dir = interactions_dir.parent / "_boundaries"
        if boundary_dir.is_dir():
            boundary_edges_by_dir[resolved_interactions_dir].update(
                valid_boundary_edges_from_crate(root, boundary_dir, None)
            )
        exemption_dir = interactions_dir.parent / "_exemptions"
        if exemption_dir.is_dir():
            interactions_by_id = load_interactions_by_id(interactions_dir)
            exemption_ids_by_dir[resolved_interactions_dir].update(
                valid_exemption_interaction_ids_from_crate(root, exemption_dir, interactions_by_id)
            )
    return boundary_edges_by_dir, exemption_ids_by_dir


def _interaction_dir_for_path(path: Path) -> Path | None:
    """Return the nearest `_interactions` ancestor for a discovered file."""
    for parent in (path.parent, *path.parents):
        if parent.name == "_interactions":
            return parent.resolve()
    return None


def validate(
    root: Path,
    valid_debt_interaction_ids: set[str] | None = None,
    covering_boundary_edges: set[tuple] | None = None,
    valid_exemption_interaction_ids: set[str] | None = None,
) -> list[Finding]:
    """Recursive, unanchored scan for the standalone CLI: finds every
    `_interactions` directory anywhere under `root`. External review,
    high severity: this used to silently `continue` past any non-.json
    file instead of validating it, so a malformed or wrong-extension
    artifact under a real `_interactions` directory produced an overall
    OK with zero findings -- every file found is now validated (and
    check_naming's own suffix check, above, catches the well-formed-JSON-
    but-wrong-extension case that would otherwise slip past validate_file's
    JSON-parse step).

    If no explicit coverage is supplied, this root-level scan derives
    per-directory coverage from sibling `_protocol_debt/`/`_boundaries/`/
    `_exemptions/` directories recursively.  The lower-level
    `validate_data` API retains its optional-context info note, but this
    CLI entrypoint is given enough context to fail closed.
    """
    validator = load_validator()
    coverage_by_interaction_dir = None
    if valid_debt_interaction_ids is None:
        coverage_by_interaction_dir = _standalone_debt_coverage(root)
    r2_by_interaction_dir = None
    if covering_boundary_edges is None or valid_exemption_interaction_ids is None:
        r2_by_interaction_dir = _standalone_r2_coverage(root)
    findings: list[Finding] = []
    for path in find_interaction_files(root):
        if coverage_by_interaction_dir is None:
            coverage = valid_debt_interaction_ids
        else:
            interaction_dir = _interaction_dir_for_path(path)
            coverage = coverage_by_interaction_dir.get(interaction_dir, set())
        if r2_by_interaction_dir is None:
            edges, exemption_ids = covering_boundary_edges, valid_exemption_interaction_ids
        else:
            interaction_dir = _interaction_dir_for_path(path)
            boundary_edges_by_dir, exemption_ids_by_dir = r2_by_interaction_dir
            edges = boundary_edges_by_dir.get(interaction_dir, set())
            exemption_ids = exemption_ids_by_dir.get(interaction_dir, set())
        findings.extend(validate_file(path, validator, coverage, edges, exemption_ids))
    return findings


def validate_crate(
    crate_root: Path,
    canonical_dir: Path,
    valid_debt_interaction_ids: set[str],
    covering_boundary_edges: set[tuple],
    valid_exemption_interaction_ids: set[str],
) -> list[Finding]:
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
        findings.extend(
            validate_file(
                path, validator, valid_debt_interaction_ids, covering_boundary_edges,
                valid_exemption_interaction_ids,
            )
        )
    return findings


def count_discovered(root: Path) -> int:
    """The candidate set this module's own scan walks, counted for
    the honest pass line (chainlink #48). Wraps find_interaction_files()
    rather than re-deriving its glob, so the count can never drift
    from the set actually validated."""
    return len(find_interaction_files(root))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=Path, help="Workspace or crate root to scan for **/_interactions/**/*.json")
    args = parser.parse_args(argv)

    try:
        findings = validate(args.root)
        discovered = count_discovered(args.root)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # Keep the severity split for callers that explicitly supplied a
    # lower-level context, while the normal root-level path above is
    # fail-closed for G15.
    errors = [f for f in findings if f.severity == "error"]
    infos = [f for f in findings if f.severity == "info"]

    if infos:
        print(f"INFO: {len(infos)} non-blocking finding(s)")
        for f in infos:
            print(f"  - {f}")

    if not errors:
        print(pass_line(
            discovered, "interactions",
            "G1a/G1b (incl. computed eligibility), G15 protocol coverage, and R2 coverage",
            args.root,
        ))
        return 0

    print(f"FAIL: {len(errors)} finding(s)")
    for f in errors:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
