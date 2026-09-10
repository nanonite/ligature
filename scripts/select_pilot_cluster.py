#!/usr/bin/env python3
"""Pilot-cluster selection rubric (chainlink #4): plan.md §14.

Generalizes what used to be a hand-curated, fixed-name table (§14's old
`phylogenetic-tree` / `mcmc-chain` / `felsenstein-pruning` picks) into a
reusable, deterministic, per-project rubric: given a project descriptor
and a workspace, mechanically rank every candidate cluster and select
the first end-to-end pilot. Nothing here names a project, crate,
verifier, or cluster -- every criterion is read from validated project
artifacts discovered fresh, so the identical code selects a different
cluster for a different project.

The five criteria (plan.md §14, unchanged from the old table's own
rationale, now mechanized instead of hand-applied):

  exactly one verifier   -- every concept in the cluster declares the
                             SAME `verifier` (concept-to-code's own
                             schema-required field: kani | creusot |
                             verus).
  pairwise                -- every intra-cluster interaction (both
                             caller and callee concepts resolve
                             unambiguously to this cluster) declares
                             `protocol_class: pairwise`.
  non-generic             -- this cluster's genuinely valid, reviewed
                             closure profile (if one exists) declares
                             `conditions.generic_callees_type_universal_
                             or_creusot_owned: true`. No closure
                             profile, or a profile that does not
                             declare it, is UNKNOWN, not "assumed
                             non-generic" -- see the architectural note
                             below.
  enum-free               -- no concept in the cluster has
                             `kind: "enum"`.
  deductive-closure value -- see below. Must be > 0.

Deductive-closure value, precisely
-----------------------------------
For a cluster with exactly one declared verifier V, the deductive-
closure value is the number of intra-cluster interaction edges (both
endpoints resolving unambiguously to this cluster) when V is a
DEDUCTIVE verifier (`creusot` or `verus` -- concept-to-code's own
`verifier` enum has exactly three values; `kani` is bounded model
checking, not a deductive proof, the same distinction plan.md's own
CG1/CG3 already draw throughout this pipeline). It is 0 whenever the
cluster does not have exactly one verifier, or its sole verifier is
`kani`. A cluster needs a POSITIVE deductive-closure value to be
eligible at all -- a candidate with none has no deductive closure to
demonstrate, only a shape that happens to pass the other four gates
vacuously (e.g. zero intra-cluster interactions).

Ranking, for the pilot role specifically (plan.md's old table's own
words: "smallest closable unit -- fastest first closure"), is ASCENDING
by deductive-closure value among eligible clusters only, tie-broken by
concept_count ascending then cluster name lexicographically -- a total
order, since cluster name is unique per candidate. This module answers
only "which cluster closes first"; it does not rank a "scale" or
"generalize" role (§14's other two, historically-named slots), which
the issue this implements does not ask for.

Architectural limitation: non-generic is rarely mechanically knowable
before a pilot exists
-----------------------------------------------------------------------
`generic_callees_type_universal_or_creusot_owned` is, by this
pipeline's own established design (gate_g14.py's own CG3 comment: "a
HUMAN DECLARATION this gate cannot verify -- no artifact in this
pipeline carries the type information it would need"), never
mechanically computed anywhere in this codebase. The ONLY place it is
captured at all is a closure profile's own declared condition -- an
artifact that, by construction, does not normally exist yet for a
cluster nobody has picked as a pilot. Per this issue's own explicit
requirement ("do not assume a cluster is non-generic merely because
generic information is unavailable"), this module treats an absent or
silent closure profile as UNKNOWN and excludes the cluster on that
ground, rather than defaulting to non-generic. In practice this means
most real candidate clusters will be excluded here until a human
authors and reviews a minimal closure profile declaring at least this
one condition honestly -- a real bootstrapping cost, not a bug, and
reported plainly rather than routed around with a heuristic (e.g.
parsing `rust_sig` text for `<...>` was considered and rejected: it is
not a validated field's own declared meaning, just a guess at Rust
syntax).

Reuses rather than re-derives
------------------------------
Concept specs are validated against the real, vendored
`vendor/concept-to-code/schemas/spec.schema.json` (this module is the
first in this codebase to actually run that schema against a concept
spec document -- everywhere else reads fields directly, since nothing
else needs `cluster`/`verifier` to be schema-guaranteed present).
Interactions are discovered with `validate_interaction.find_interaction_files`
and validated with its own `validate_data` (coverage args left at their
default `None`, which downgrades G15/R2 to non-blocking info notes --
this rubric asks whether a cluster's SHAPE is closable, not whether its
boundary/protocol-debt coverage is already complete, which is the
pilot's own future work, not a precondition for picking it). Closure
profiles are read via `validate_closure.load_cluster_artifacts_with_invalid`
(chainlink #49), the same "genuinely valid, not just present" bar every
cross-reference in this pipeline applies.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from project_descriptor import ProjectDescriptorError  # noqa: E402
from project_descriptor import interaction_dir_for  # noqa: E402
from project_descriptor import load_project_descriptor  # noqa: E402
from schema_utils import make_validator  # noqa: E402
from validate_closure import load_cluster_artifacts_with_invalid  # noqa: E402
from validate_interaction import find_interaction_files  # noqa: E402
from validate_interaction import load_validator as load_interaction_validator  # noqa: E402
from validate_interaction import validate_data as validate_interaction_data  # noqa: E402

CONCEPT_SPEC_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / "vendor" / "concept-to-code" / "schemas" / "spec.schema.json"
)

# concept-to-code's own `verifier` enum is exactly {kani, creusot, verus}.
# kani is bounded model checking; creusot/verus are deductive. This is a
# classification of that fixed, schema-closed vocabulary -- not a
# preference for one project's verifier over another's.
DEDUCTIVE_VERIFIERS = frozenset({"creusot", "verus"})

EXIT_OK = 0
EXIT_NO_ELIGIBLE = 1
EXIT_INPUT_ERROR = 2


@dataclass
class Finding:
    gate: str
    subject: str
    reason: str
    severity: str = "error"

    def __str__(self) -> str:
        return f"[{self.gate}/{self.severity}] {self.subject}: {self.reason}"


@dataclass
class ConceptInfo:
    concept: str
    cluster: str
    kind: str
    verifier: str
    path: Path


@dataclass
class EdgeInfo:
    interaction_id: str
    caller_concept: str
    callee_concept: str
    protocol_class: str
    path: Path


@dataclass
class ClusterCandidate:
    cluster: str
    concept_count: int
    verifiers: tuple[str, ...]
    enum_concepts: tuple[str, ...]
    intra_edge_count: int
    non_pairwise_edges: tuple[str, ...]
    generic_status: str  # "non-generic" | "generic" | "unknown"
    deductive_closure_value: int
    eligible: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


def load_concept_spec_schema() -> dict:
    return json.loads(CONCEPT_SPEC_SCHEMA_PATH.read_text())


def load_concept_spec_validator():
    return make_validator(load_concept_spec_schema())


def discover_concepts(descriptor: dict, workspace: Path) -> tuple[dict[str, ConceptInfo], list[Finding]]:
    """Every genuinely valid, unambiguously-identified concept spec
    reachable from the descriptor's own declared `specs_search_root`s.
    Mirrors gate_g18.discover_declared_features's own candidate filter
    (dict with a string `concept` field, underscore-prefixed directories
    skipped so a witness spec's own top-level `concept` field is never
    mistaken for a concept spec) and load_bridges'/collect_declared_
    features's own "ambiguous, never first-wins" discipline: the same
    concept name declared by more than one genuinely valid spec file --
    whether or not they agree on `cluster` -- cannot be resolved to a
    single authoritative cluster and is excluded from every cluster's
    membership rather than guessed."""
    validator = load_concept_spec_validator()
    roots = sorted({(workspace / crate["specs_search_root"]).resolve() for crate in descriptor["crates"]})

    by_concept: dict[str, list[ConceptInfo]] = {}
    findings: list[Finding] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("**/*.json")):
            if any(part.startswith("_") for part in path.relative_to(root).parts):
                continue
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(data, dict) or not isinstance(data.get("concept"), str):
                continue
            errors = list(validator.iter_errors(data))
            if errors:
                findings.append(
                    Finding(
                        "PILOT", str(path),
                        f"candidate concept spec for {data['concept']!r} is schema-invalid against "
                        f"vendor/concept-to-code/schemas/spec.schema.json ({len(errors)} finding(s)): "
                        f"{errors[0].message}",
                    )
                )
                continue
            by_concept.setdefault(data["concept"], []).append(
                ConceptInfo(
                    concept=data["concept"], cluster=data["cluster"],
                    kind=data.get("kind", "struct"), verifier=data["verifier"], path=path,
                )
            )

    resolved: dict[str, ConceptInfo] = {}
    for concept, infos in sorted(by_concept.items()):
        unique_paths = sorted({info.path for info in infos})
        if len(unique_paths) > 1:
            clusters = sorted({info.cluster for info in infos})
            findings.append(
                Finding(
                    "PILOT", concept,
                    f"concept {concept!r} is declared by more than one genuinely valid concept spec "
                    f"({', '.join(str(p) for p in unique_paths)}), naming cluster(s) {clusters} -- "
                    "which declaration is authoritative cannot be determined",
                )
            )
            continue
        resolved[concept] = infos[0]
    return resolved, findings


def discover_interactions(descriptor: dict, workspace: Path) -> tuple[list[EdgeInfo], list[Finding]]:
    """Every genuinely valid interaction under each crate's own canonical
    `_interactions` directory. R2/G15 coverage is deliberately left
    unsupplied (defaults to None, which validate_interaction.validate_data
    downgrades to a non-blocking info note): this rubric asks whether a
    cluster's edges are pairwise-shaped, not whether the boundary
    contracts a pilot would go on to author already exist. A candidate
    whose own content is schema/naming/eligibility invalid, or that
    sits outside its crate's canonical directory, never reaches any
    cluster's edge count -- reported instead."""
    validator = load_interaction_validator()
    edges: list[EdgeInfo] = []
    findings: list[Finding] = []
    seen: set[Path] = set()

    for crate in descriptor["crates"]:
        crate_root = (workspace / crate["crate_dir"]).resolve()
        if not crate_root.is_dir():
            continue
        canonical = interaction_dir_for(crate, workspace)
        for path in sorted(find_interaction_files(crate_root)):
            resolved_path = path.resolve()
            if resolved_path in seen:
                continue
            seen.add(resolved_path)
            if resolved_path.parent != canonical.resolve():
                findings.append(
                    Finding(
                        "PILOT", str(path),
                        f"interaction artifact is not directly under the canonical directory "
                        f"{canonical} -- excluded from pilot-selection evidence",
                    )
                )
                continue
            try:
                text = path.read_text()
            except UnicodeDecodeError as e:
                findings.append(Finding("PILOT", str(path), f"not readable as UTF-8 text: {e}"))
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                findings.append(Finding("PILOT", str(path), f"invalid JSON: {e}"))
                continue
            file_findings = validate_interaction_data(path, data, validator)
            errors = [f for f in file_findings if f.severity == "error"]
            if errors:
                findings.append(
                    Finding(
                        "PILOT", str(path),
                        f"interaction is not genuinely valid ({len(errors)} finding(s)) -- run "
                        "`pipeline.py validate-interaction` for the detailed reason",
                    )
                )
                continue
            edges.append(
                EdgeInfo(
                    interaction_id=data["interaction_id"],
                    caller_concept=data["caller"]["concept"],
                    callee_concept=data["callee"]["concept"],
                    protocol_class=data["protocol_class"],
                    path=path,
                )
            )
    return edges, findings


def evaluate_clusters(
    concepts_by_name: dict[str, ConceptInfo],
    edges: list[EdgeInfo],
    closure_artifacts: dict[str, dict[str, tuple[Path, dict]]],
) -> list[ClusterCandidate]:
    """One ClusterCandidate per distinct cluster with at least one
    genuinely valid, unambiguously-mapped concept. Every metric is
    computed independently of every other, so a candidate's `reasons`
    can name every gate it fails, not just the first."""
    concepts_by_cluster: dict[str, list[ConceptInfo]] = {}
    for info in concepts_by_name.values():
        concepts_by_cluster.setdefault(info.cluster, []).append(info)

    intra_edges_by_cluster: dict[str, list[EdgeInfo]] = {}
    for edge in edges:
        caller = concepts_by_name.get(edge.caller_concept)
        callee = concepts_by_name.get(edge.callee_concept)
        if caller is None or callee is None:
            continue
        if caller.cluster != callee.cluster:
            continue
        intra_edges_by_cluster.setdefault(caller.cluster, []).append(edge)

    candidates: list[ClusterCandidate] = []
    for cluster in sorted(concepts_by_cluster):
        concepts = sorted(concepts_by_cluster[cluster], key=lambda c: c.concept)
        verifiers = sorted({c.verifier for c in concepts})
        enum_concepts = tuple(sorted(c.concept for c in concepts if c.kind == "enum"))
        intra_edges = sorted(intra_edges_by_cluster.get(cluster, []), key=lambda e: e.interaction_id)
        non_pairwise = tuple(sorted(e.interaction_id for e in intra_edges if e.protocol_class != "pairwise"))

        single_verifier = len(verifiers) == 1
        sole_verifier = verifiers[0] if single_verifier else None
        deductive_closure_value = (
            len(intra_edges) if (single_verifier and sole_verifier in DEDUCTIVE_VERIFIERS) else 0
        )

        profile_entry = closure_artifacts.get(cluster, {}).get("profile")
        if profile_entry is None:
            generic_status = "unknown"
        else:
            declared = profile_entry[1]["conditions"]["generic_callees_type_universal_or_creusot_owned"]
            generic_status = "non-generic" if declared is True else "generic"

        reasons: list[str] = []
        if not single_verifier:
            reasons.append(f"not exactly one verifier: {verifiers}")
        if non_pairwise:
            reasons.append(f"non-pairwise intra-cluster interaction(s): {list(non_pairwise)}")
        if generic_status == "unknown":
            reasons.append(
                "non-generic status unknown -- no genuinely valid, reviewed closure profile for this "
                "cluster declares generic_callees_type_universal_or_creusot_owned (this pipeline never "
                "computes this condition; it is a human declaration recorded only in a closure profile)"
            )
        elif generic_status == "generic":
            reasons.append(
                "declared generic by its closure profile's own "
                "generic_callees_type_universal_or_creusot_owned condition"
            )
        if enum_concepts:
            reasons.append(f"enum-kind concept(s) present: {list(enum_concepts)}")
        if deductive_closure_value == 0:
            if not intra_edges:
                reasons.append("no intra-cluster interaction edges resolve to this cluster")
            elif single_verifier and sole_verifier not in DEDUCTIVE_VERIFIERS:
                reasons.append(
                    f"sole verifier {sole_verifier!r} is not a deductive verifier "
                    "(kani is bounded model checking, not a deductive proof)"
                )
            # else: already covered by the "not exactly one verifier" reason above.

        candidates.append(
            ClusterCandidate(
                cluster=cluster,
                concept_count=len(concepts),
                verifiers=tuple(verifiers),
                enum_concepts=enum_concepts,
                intra_edge_count=len(intra_edges),
                non_pairwise_edges=non_pairwise,
                generic_status=generic_status,
                deductive_closure_value=deductive_closure_value,
                eligible=not reasons,
                reasons=tuple(reasons),
            )
        )
    return candidates


def rank_eligible(candidates: list[ClusterCandidate]) -> list[ClusterCandidate]:
    """Ascending deductive-closure value -- the pilot role wants the
    smallest closable unit (plan.md §14's own rationale: "fastest first
    closure"), tie-broken by concept_count ascending then cluster name
    lexicographically. Cluster name is unique per candidate, so this is
    a total order: no two eligible candidates can tie all the way
    through."""
    eligible = [c for c in candidates if c.eligible]
    return sorted(eligible, key=lambda c: (c.deductive_closure_value, c.concept_count, c.cluster))


def select_pilot(
    workspace: Path, descriptor: dict
) -> tuple[list[ClusterCandidate], list[ClusterCandidate], list[Finding]]:
    """Top-level orchestration. Returns (every candidate cluster sorted
    by name, eligible candidates ranked ascending, input findings sorted
    deterministically) -- never dependent on filesystem discovery order,
    since every discovery step above already sorts its own output and
    grouping is by value (dict keyed by cluster/concept name), not
    insertion order."""
    concepts_by_name, concept_findings = discover_concepts(descriptor, workspace)
    edges, edge_findings = discover_interactions(descriptor, workspace)
    findings = list(concept_findings) + list(edge_findings)

    for edge in edges:
        if edge.caller_concept not in concepts_by_name:
            findings.append(
                Finding(
                    "PILOT", edge.interaction_id,
                    f"caller concept {edge.caller_concept!r} does not resolve to any genuinely valid, "
                    "unambiguous concept spec -- excluded from every cluster's edge count",
                )
            )
        if edge.callee_concept not in concepts_by_name:
            findings.append(
                Finding(
                    "PILOT", edge.interaction_id,
                    f"callee concept {edge.callee_concept!r} does not resolve to any genuinely valid, "
                    "unambiguous concept spec -- excluded from every cluster's edge count",
                )
            )

    closure_artifacts, invalid_closure_paths = load_cluster_artifacts_with_invalid(workspace)
    for path in invalid_closure_paths:
        findings.append(
            Finding(
                "PILOT", str(path),
                "invalid closure artifact excluded from generic-status evidence -- run "
                "`pipeline.py validate-closure` for the detailed reason",
            )
        )

    candidates = evaluate_clusters(concepts_by_name, edges, closure_artifacts)
    ranked = rank_eligible(candidates)
    findings.sort(key=lambda f: (f.gate, f.subject, f.reason))
    return candidates, ranked, findings


def exit_code_for(ranked: list[ClusterCandidate], findings: list[Finding]) -> int:
    """Error-severity input findings make the whole result
    non-authoritative -- a schema-invalid concept spec, an ambiguous
    concept identity, an invalid interaction, an unresolved edge
    endpoint, or an invalid closure artifact could each be hiding a
    concept or edge that would have changed which cluster is eligible,
    or which one ranks first. A provisional ranking is still printed
    (never withheld), but EXIT_INPUT_ERROR always wins over EXIT_OK,
    even when an eligible cluster happens to remain -- the same
    "genuinely valid, not just present" bar this codebase applies
    everywhere else, applied here to the report as a whole rather than
    to one artifact."""
    if any(f.severity == "error" for f in findings):
        return EXIT_INPUT_ERROR
    return EXIT_OK if ranked else EXIT_NO_ELIGIBLE


def render_report(candidates: list[ClusterCandidate], ranked: list[ClusterCandidate], findings: list[Finding]) -> str:
    lines: list[str] = [f"Evaluated {len(candidates)} candidate cluster(s)."]
    for c in candidates:
        lines.append(
            f"- {c.cluster}: concepts={c.concept_count} verifiers={list(c.verifiers)} "
            f"intra_edges={c.intra_edge_count} non_pairwise={list(c.non_pairwise_edges)} "
            f"enum_concepts={list(c.enum_concepts)} generic={c.generic_status} "
            f"deductive_closure_value={c.deductive_closure_value} eligible={c.eligible}"
        )
        for reason in c.reasons:
            lines.append(f"    excluded: {reason}")

    errors = [f for f in findings if f.severity == "error"]
    if findings:
        lines.append(f"{len(findings)} input finding(s):")
        for f in findings:
            lines.append(f"  - {f}")

    if ranked:
        lines.append("Rank (ascending deductive-closure value, tie-break concept_count then cluster name):")
        for i, c in enumerate(ranked, start=1):
            lines.append(
                f"  {i}. {c.cluster} (deductive_closure_value={c.deductive_closure_value}, "
                f"concepts={c.concept_count})"
            )
        if errors:
            lines.append(
                f"PROVISIONAL pilot (NOT authoritative -- {len(errors)} error-severity input finding(s) above "
                f"could hide a concept or edge that would change eligibility or rank): {ranked[0].cluster}"
            )
        else:
            lines.append(f"Selected pilot: {ranked[0].cluster}")
    else:
        lines.append("No eligible candidate cluster -- every candidate was excluded (see reasons above).")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", type=Path, default=Path("."), help="Workspace root")
    parser.add_argument("--descriptor", type=Path, required=True, help="Project descriptor JSON path")
    args = parser.parse_args(argv)

    try:
        descriptor = load_project_descriptor(args.descriptor)
    except (ProjectDescriptorError, FileNotFoundError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        print(f"error: workspace root does not exist or is not a directory: {workspace}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    try:
        candidates, ranked, findings = select_pilot(workspace, descriptor)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    print(render_report(candidates, ranked, findings))
    return exit_code_for(ranked, findings)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
