#!/usr/bin/env python3
"""Boundary contract validation: G1a (schema), G1b (repo semantics), G2+ (role safety).

plan.md §2, §12. Three phases, run in order -- each phase only runs on
artifacts that passed the previous one, since a G1b/G2+ check on a
schema-invalid document isn't meaningful.

  G1a  -- draft-2020-12 JSON Schema validation against
          docs/boundary-contract-schema.json.
  G1b  -- repo-semantic checks: filename/layout (delegates to
          validate_boundary_naming.py) plus filename<->body consistency
          (the caller/callee concept+method encoded in the filename must
          match the caller/callee declared in the JSON body) and
          tracking-issue-per-assumption uniqueness within a boundary.
          NOTE: does not include computed-eligibility mismatch -- that's
          an I-schema (interaction) concept, out of scope until M3.
  G2+  -- role safety only (Prototype A scope, plan.md §12): callee_guarantees
          entries must name the *callee's* obligations, must never be an
          adversary case, and (when resolvable) must apply to the declared
          callee method. Never checks a declared assurance target -- that's
          deferred to the I-schema's reliances[].required_assurance (M3).

CAVEAT (docs/concept-to-code-modifications.md gap #6): callee_guarantees
entries reference a constraint by <Concept>.<id>, but concept-to-code does
not yet expose a stable `id` field on constraint. Until that upstream change
lands, cross-file resolution (used by the applies_to check) degrades to
"unverifiable" rather than failing -- see _resolve_constraint below.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
from schema_utils import make_validator  # noqa: E402
from schema_utils import make_validator_without_required  # noqa: E402
from validate_boundary_naming import find_boundary_files  # noqa: E402
from validate_boundary_naming import check_file as check_naming  # noqa: E402
from scan_summary import pass_line  # noqa: E402

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "docs" / "boundary-contract-schema.json"

# Symmetric digit-width grammar for both prefixes -- an earlier version
# hardcoded exactly 3 digits for C but let A be any length, which would
# misreport a legitimate C1234 as a sentinel/placeholder (review finding
# D5). Neither prefix's width is normatively fixed anywhere upstream, so
# there's no reason for them to disagree with each other.
OBLIGATION_RE = re.compile(r"^([A-Z][A-Za-z0-9]*)\.C([0-9]+)$")
ADVERSARY_SHAPED_RE = re.compile(r"^([A-Z][A-Za-z0-9]*)\.A[0-9]+$")


@dataclass
class Finding:
    gate: str
    path: Path
    reason: str
    severity: str = "error"  # "error" (blocking) | "info" (visible, non-blocking)

    def __str__(self) -> str:
        return f"[{self.gate}/{self.severity}] {self.path}: {self.reason}"


def _pascal_to_snake(name: str) -> str:
    """Copied verbatim from vendor/concept-to-code/emit_stubs.py's
    snake_case() (module_name = snake_case(concept) at emit_stubs.py:2090)
    -- do not re-derive this. The naive "insert _ before every capital"
    regex this used to be breaks on acronym concepts (review finding D2):
    HTTPClient -> h_t_t_p_client instead of concept-to-code's own
    httpclient. This only inserts an underscore at a lowercase-to-uppercase
    transition, so a run of capitals stays joined."""
    out: list[str] = []
    for i, ch in enumerate(name):
        if ch.isupper() and i and not name[i - 1].isupper():
            out.append("_")
        out.append(ch.lower())
    return "".join(out).replace("-", "_")


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def load_validator() -> Draft202012Validator:
    """Canonical entry point -- use this, not Draft202012Validator(load_schema())
    directly, or format: date silently stops being enforced (see schema_utils.py)."""
    return make_validator(load_schema())


def gate_g1a(path: Path, data: dict, validator: Draft202012Validator) -> list[Finding]:
    return [
        Finding("G1a", path, e.message) for e in validator.iter_errors(data)
    ]


def gate_g1b(path: Path, data: dict) -> list[Finding]:
    findings: list[Finding] = []

    # Naming/layout, from #8's validator. Pass the already-loaded data
    # through so this doesn't re-read path from disk (it also lets this
    # run against a draft that hasn't been written to its target path yet).
    findings.extend(
        Finding("G1b", v.path, v.reason) for v in check_naming(path, data)
    )

    # Filename <-> body consistency: the caller/callee encoded in the
    # filename must match caller/callee declared in the JSON body.
    caller = data.get("caller") or {}
    callee = data.get("callee") or {}
    if "concept" in caller and "method" in caller and "concept" in callee and "method" in callee:
        expected_stem = (
            f"{_pascal_to_snake(caller['concept'])}_{caller['method']}"
            "__to__"
            f"{_pascal_to_snake(callee['concept'])}_{callee['method']}"
        )
        if path.stem != expected_stem:
            findings.append(
                Finding(
                    "G1b",
                    path,
                    f"filename stem {path.stem!r} does not match caller/callee "
                    f"declared in the body (expected {expected_stem!r})",
                )
            )

    # Tracking-issue uniqueness within this boundary's assumptions.
    seen: dict[str, int] = {}
    for a in data.get("assumptions", []):
        issue = a.get("tracking_issue")
        if issue is None:
            continue
        seen[issue] = seen.get(issue, 0) + 1
    for issue, count in seen.items():
        if count > 1:
            findings.append(
                Finding(
                    "G1b",
                    path,
                    f"tracking_issue {issue!r} used by {count} assumptions in "
                    "the same boundary -- one issue per assumption",
                )
            )

    return findings


def _resolve_constraint(
    concept: str, obligation_id: str, specs_search_root: Path | None
) -> tuple[str, dict | None]:
    """Best-effort resolution of <Concept>.<id> to a constraint dict in the
    callee's concept spec. Returns (status, constraint_or_none).
    status is one of: 'resolved', 'no_search_root', 'spec_not_found',
    'ambiguous' (review finding D3 -- more than one spec file declares this
    concept), 'no_ids_in_spec' (gap #6 not applied upstream yet), 'dangling'
    (G2, not this gate's job to fail on, but reported for visibility)."""
    if specs_search_root is None:
        return "no_search_root", None

    # D3: match by the spec's own `concept` field, not by filename -- a
    # file named task_queue.json is not proof its `concept` is TaskQueue
    # (it could be a stale copy, or declare TaskQueueV2). Sort for
    # determinism regardless (glob order is filesystem-dependent).
    all_specs = sorted(specs_search_root.glob("**/*.json"))
    matches = []
    for spec_path in all_specs:
        try:
            spec = json.loads(spec_path.read_text())
        except json.JSONDecodeError:
            continue
        if isinstance(spec, dict) and spec.get("concept") == concept:
            matches.append((spec_path, spec))

    if not matches:
        return "spec_not_found", None
    if len(matches) > 1:
        return "ambiguous", None

    _, spec = matches[0]
    constraints = spec.get("constraints", [])
    if constraints and not any("id" in c for c in constraints):
        return "no_ids_in_spec", None

    for c in constraints:
        if c.get("id") == obligation_id:
            return "resolved", c
    return "dangling", None


def gate_g2_plus(
    path: Path, data: dict, specs_search_root: Path | None
) -> list[Finding]:
    findings: list[Finding] = []
    callee = data.get("callee") or {}
    callee_concept = callee.get("concept")
    callee_method = callee.get("method")

    # G1a's own pattern is deliberately loose (accepts both the C-obligation
    # and A-adversary shapes structurally, see docs/boundary-contract-schema.json)
    # so it's still meaningful to check role safety here even though this
    # only ever runs after G1a already passed -- the semantic distinction
    # (must be C, never A; must belong to the callee) is this gate's job,
    # not G1a's, which is why loosening G1a didn't make this gate redundant.
    for entry in data.get("callee_guarantees", []):
        if not isinstance(entry, str):
            continue

        adversary_match = ADVERSARY_SHAPED_RE.match(entry)
        if adversary_match:
            findings.append(
                Finding(
                    "G2+",
                    path,
                    f"{entry!r} is an adversary case, not a guarantee -- "
                    "adversary cases are evidence/test seeds only "
                    "(docs/reliance-policy.template.md resolution rule)",
                )
            )
            continue

        match = OBLIGATION_RE.match(entry)
        if not match:
            findings.append(
                Finding(
                    "G2+",
                    path,
                    f"{entry!r} is not a recognizable obligation reference "
                    "(expected <Concept>.C<digits>) -- possible sentinel/placeholder "
                    "value left in callee_guarantees",
                )
            )
            continue

        ref_concept = match.group(1)
        if callee_concept is not None and ref_concept != callee_concept:
            findings.append(
                Finding(
                    "G2+",
                    path,
                    f"{entry!r} names {ref_concept!r}, but this boundary's "
                    f"callee is {callee_concept!r} -- a guarantee must belong "
                    "to the callee, never the caller or an unrelated concept "
                    "(role safety)",
                )
            )
            continue

        status, constraint = _resolve_constraint(ref_concept, entry.split(".", 1)[1], specs_search_root)
        if status == "resolved" and constraint is not None:
            applies_to = constraint.get("applies_to") or []
            if applies_to and callee_method not in applies_to:
                findings.append(
                    Finding(
                        "G2+",
                        path,
                        f"{entry!r} applies_to {applies_to!r}, which does not "
                        f"include this boundary's callee method {callee_method!r}",
                    )
                )
        elif status == "ambiguous":
            findings.append(
                Finding(
                    "G2+",
                    path,
                    f"{entry!r}: more than one spec file under specs_search_root "
                    f"declares concept {ref_concept!r} -- cannot resolve unambiguously",
                )
            )
        elif status == "dangling":
            # Reference integrity (does the id exist at all) is G2's job,
            # not this gate's -- but still worth a non-blocking note here
            # since it's found in the course of the applies_to check anyway.
            findings.append(
                Finding(
                    "G2+",
                    path,
                    f"{entry!r} does not match any constraint id in {ref_concept}'s "
                    "resolved spec (dangling reference -- G2's concern, noted here for visibility)",
                    severity="info",
                )
            )
        elif status in ("no_search_root", "spec_not_found", "no_ids_in_spec"):
            # D4: previously a silent skip -- "0 findings" was indistinguishable
            # from "0 findings because nothing was checkable." Report it,
            # non-blocking, so gap #6's upstream state is a measurable signal
            # instead of invisible.
            reason = {
                "no_search_root": "no --specs-search-root given",
                "spec_not_found": f"no spec file under specs_search_root declares concept {ref_concept!r}",
                "no_ids_in_spec": f"{ref_concept}'s spec has constraints but none carry a stable id yet "
                "(docs/concept-to-code-modifications.md gap #6, not applied upstream)",
            }[status]
            findings.append(
                Finding(
                    "G2+",
                    path,
                    f"{entry!r}: applies_to unverifiable -- {reason}",
                    severity="info",
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
    """Stage 0/3 immediate feedback (plan.md §6.1: "immediately run
    through G1a/G1b for fast local feedback"). `validator` must come from
    load_draft_validator(), not load_validator() -- the schema's own
    `review` requirement doesn't apply yet at draft time.

    Deliberately excludes G2+ (needs specs_search_root, a cross-file
    Stage 4 concern -- external review, high severity: the first version
    of this dispatcher reused approve()'s full validate_data(), which
    both rejected every review-less draft outright via G1a and ran G2+
    at draft time; both reproduced directly before this fix). Full
    G1a/G1b/G2+ still runs unchanged at approve() time via validate_data()."""
    review_check = check_no_draft_review(path, data)
    if review_check:
        return review_check
    g1a = gate_g1a(path, data, validator)
    if g1a:
        return g1a
    return gate_g1b(path, data)


def validate_data(
    path: Path, data: dict, validator: Draft202012Validator, specs_search_root: Path | None = None
) -> list[Finding]:
    """The full G1a/G1b/G2+ chain against in-memory data, positioned at
    `path` for filename-based checks -- `path` need not exist on disk yet.
    This is what review_checkpoint.approve() calls before promoting a draft
    (review finding D1: approve() used to write straight through with no
    gate at all)."""
    g1a = gate_g1a(path, data, validator)
    if g1a:
        # G1b/G2+ need a structurally valid document to mean anything.
        return g1a

    return gate_g1b(path, data) + gate_g2_plus(path, data, specs_search_root)


def validate_file(
    path: Path, validator: Draft202012Validator, specs_search_root: Path | None
) -> list[Finding]:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        return [Finding("G1a", path, f"invalid JSON: {e}")]

    return validate_data(path, data, validator, specs_search_root)


def valid_boundary_edges_from_crate(
    crate_root: Path, canonical_dir: Path, specs_search_root: Path | None
) -> set[tuple[str | None, str | None, str | None, str | None]]:
    """The set of (caller_concept, caller_method, callee_concept,
    callee_method) edges covered by a fully valid (zero-ERROR-finding --
    G1a/G1b/G2+, which includes the schema's own `review` requirement) boundary
    contract directly under `canonical_dir` in this crate -- what R2
    (plan.md gate table §12: "eligible I edge with no boundary and no
    reviewed exemption") treats as 'covered by a boundary'.

    Mirrors scripts/validate_protocol_debt.py's own
    valid_interaction_ids_from_crate: runs the same discover-then-validate
    logic used throughout this codebase but returns coverage, not
    findings. A mislocated boundary contract (correctly rejected by G1b
    elsewhere) never contributes coverage -- only files directly under
    `canonical_dir` are considered here.

    Trust is gated on error-severity findings only, not mere non-emptiness
    -- gate_g2_plus can legitimately return an info-severity finding for
    an otherwise-valid boundary (e.g. `specs_search_root=None`, or a
    concept spec that doesn't exist yet: "applies_to unverifiable", not a
    defect in the boundary contract itself). Treating ANY finding as
    disqualifying would silently drop a genuinely valid boundary from R2
    coverage the moment G2+ had nothing to say -- reproduced directly
    (specs_search_root=None, the standalone CLI's own default) before
    fixing."""
    canonical_resolved = canonical_dir.resolve()
    validator = load_validator()
    covered: set[tuple[str | None, str | None, str | None, str | None]] = set()
    for path in sorted(find_boundary_files(crate_root)):
        if path.resolve().parent != canonical_resolved:
            continue
        findings = validate_file(path, validator, specs_search_root)
        if any(getattr(finding, "severity", "error") == "error" for finding in findings):
            continue
        data = json.loads(path.read_text())
        caller = data.get("caller") or {}
        callee = data.get("callee") or {}
        covered.add((caller.get("concept"), caller.get("method"), callee.get("concept"), callee.get("method")))
    return covered


class BoundaryLookup(dict[str, dict]):
    """A lookup plus the IDs invalidated by duplicate candidates -- mirrors
    scripts/validate_interaction.py's own InteractionLookup exactly."""

    def __init__(self):
        super().__init__()
        self.duplicate_ids: set[str] = set()


def load_boundaries_by_id(canonical_dir: Path, specs_search_root: Path | None = None) -> BoundaryLookup:
    """Load only approved, fully valid, canonically located boundary
    contracts, keyed by boundary_id -- mirrors
    scripts/validate_interaction.py's own load_interactions_by_id
    exactly (chainlink #22's own bridge cross-reference needs the same
    "must be genuinely valid, not just present" trust bar boundaries.py's
    R2 coverage set already established, but keyed by id with the full
    record retained, not just the caller/callee edge tuple
    valid_boundary_edges_from_crate returns -- a bridge's own G2 check
    needs to inspect the boundary's callee_guarantees, not just confirm
    an edge exists.

    Only considers files directly under `canonical_dir` -- a mislocated
    boundary contract (correctly rejected by G1b elsewhere) is not a
    real boundary a bridge may reference."""
    result = BoundaryLookup()
    if not canonical_dir.is_dir():
        return result

    validator = load_validator()
    candidates: dict[str, list[tuple[Path, dict]]] = {}
    for path in sorted(p for p in canonical_dir.iterdir() if p.is_file() and p.suffix != ".draft"):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("boundary_id"), str):
            candidates.setdefault(data["boundary_id"], []).append((path, data))

    result.duplicate_ids = {boundary_id for boundary_id, entries in candidates.items() if len(entries) > 1}

    for boundary_id, entries in candidates.items():
        if boundary_id in result.duplicate_ids:
            continue
        path, data = entries[0]
        findings = validate_data(path, data, validator, specs_search_root)
        if any(getattr(finding, "severity", "error") == "error" for finding in findings):
            continue
        result[boundary_id] = data
    return result


def validate(root: Path, specs_search_root: Path | None = None) -> list[Finding]:
    validator = load_validator()
    findings: list[Finding] = []
    for path in find_boundary_files(root):
        if path.suffix != ".json":
            continue
        findings.extend(validate_file(path, validator, specs_search_root))
    return findings


def count_discovered(root: Path) -> int:
    """The candidate set this module's own scan walks, counted for
    the honest pass line (chainlink #48). Wraps find_boundary_files()
    rather than re-deriving its glob, so the count can never drift
    from the set actually validated.

    validate() skips non-.json files under a _boundaries/ directory,
    so they are not discovered boundary contracts here either.
    """
    return len([p for p in find_boundary_files(root) if p.suffix == ".json"])


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=Path, help="Workspace or crate root to scan for **/_boundaries/**/*.json")
    parser.add_argument(
        "--specs-search-root",
        type=Path,
        default=None,
        help="Root to resolve callee concept specs from, for the applies_to check (optional).",
    )
    args = parser.parse_args(argv)

    try:
        findings = validate(args.root, args.specs_search_root)
        discovered = count_discovered(args.root)
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
        print(pass_line(discovered, "boundary contracts", "G1a/G1b/G2+", args.root))
        return 0

    print(f"FAIL: {len(errors)} finding(s)")
    for f in errors:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
