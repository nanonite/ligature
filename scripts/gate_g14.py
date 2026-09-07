#!/usr/bin/env python3
"""G14: release closure over the TRANSITIVE assurance dependency graph,
with the CG6 well-foundedness discharge.

plan.md §8.5, §4 and §12's G14/G17 rows, chainlink #25. Stage 8C, the
last gate.

plan.md §8.5's own six steps, in order, and where each one lives here:

  1. Load all required-guarantee dependencies      load_manifests / build_provider_index
  2. Compute the TRANSITIVE dependency closure     compute_closure
  3. Detect cycles                                 strongly_connected_components
  4. Evaluate each requirement with satisfies()    check_requirements
  5. Apply trust policy to EVERY assumption
     in the closure                                check_transitive_assumptions
  6. Fail if any required dependency is
     unsupported or below policy                   both of the above

The opening case that motivates all of it: "Direct A->B checking passes
while a C-level assumption sits below policy." Step 5 is therefore NOT
the same thing as running satisfies() on each edge -- each edge's own
trust_policy is checked by step 4, and step 5 additionally holds every
assumption anywhere in the closure against the CLUSTER'S OWN entry
requirements. An assumption three hops down that the cluster's entry
policy would never have allowed fails here even though every individual
edge passed.

For cycles (CG6): mutual satisfaction is not soundness. An O-SCC closes
only with all of -- every body meets its provided contracts, every bridge
requirement passes, every assumption satisfies policy, AND an explicit
well-foundedness discharge naming that exact SCC (step index, decreasing
measure, or temporal stratification). The discharge is prose this gate
cannot check the correctness of; what it checks is that one exists, that
it covers the SCC actually computed, and that it was reviewed. A
discharge naming an SCC that does not exist is rejected as stale rather
than ignored.

Outcome, per cluster and never globally (plan.md §4: "Never a global
pipeline guarantee"):

  closes    every check passes; the profile's closure_kind is reported
            with it, because two clusters with identical condition bits
            carry different guarantees.
  degraded  a declared, reviewed degradation record covers exactly the
            closure conditions that failed; the cluster does NOT close,
            it is released under a named ceiling with a tracking issue.
            Exit 0 -- "a declared state, not a failure" (plan.md §4) --
            and the wording never says closed.
  blocked   anything else. Note what a degradation record can never
            excuse: its failed_conditions vocabulary is the closure
            CONDITION keys only, so an unsatisfied requirement, an
            unsupported dependency, a missing achieved record, a failed
            bridge, or a mis-declared condition bit keeps blocking with
            a record present. Those are absences of evidence, not
            ceilings a human can accept.

`satisfies(required_profile, achieved_record, context)`'s third parameter
is defined here (chainlink #23 left it accepted-but-unused, naming #25 as
the issue that would define it): see closure_context(). It carries the
cluster-level facts a verifier-ceiling policy would need -- which cluster,
which declared kind, which work package required this of which other, and
how deep in the closure the edge sits. satisfies() still ignores it; what
matters is that callers now pass one well-defined shape, and every
finding this gate emits can name the closure path an edge failed on.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gate_r1_g16 import unresolved_at_or_above_medium  # noqa: E402
from project_descriptor import boundary_dir_for  # noqa: E402
from satisfies import satisfies  # noqa: E402
from schema_utils import make_validator  # noqa: E402
from validate_boundary_contracts import load_boundaries_by_id  # noqa: E402
from validate_bridge import find_bridge_files  # noqa: E402
from validate_bridge import load_validator as load_bridge_validator  # noqa: E402
from validate_bridge import validate_data as validate_bridge_data  # noqa: E402
from validate_callsites import load_reports as load_callsite_reports  # noqa: E402
from validate_closure import CONDITION_KEYS  # noqa: E402
from validate_closure import closure_dir_for  # noqa: E402
from validate_closure import load_cluster_artifacts  # noqa: E402
from validate_work_package import load_validator as load_work_package_validator  # noqa: E402

DOCS = Path(__file__).resolve().parent.parent / "docs"
ASSURANCE_REPORT_SCHEMA_PATH = DOCS / "assurance-report-schema.json"

MANIFEST_DIR = ("ci", "manifest")

DEDUCTIVE_EVIDENCE_KINDS = {"creusot-deductive-check", "verus-deductive-check"}
BOUNDED_EVIDENCE_KINDS = {"kani-bounded-model-check"}
VERIFIER_BY_EVIDENCE_KIND = {
    "creusot-deductive-check": "creusot",
    "verus-deductive-check": "verus",
    "kani-bounded-model-check": "kani",
}

PER_CLUSTER_NOTE = (
    "closure is a per-cluster property with a kind; this gate never asserts a pipeline-wide "
    "guarantee (plan.md §4)"
)

EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_INPUT_ERROR = 2


@dataclass
class Finding:
    gate: str
    subject: str
    reason: str
    severity: str = "error"  # "error" | "degraded" | "info"
    condition: str | None = None

    def __str__(self) -> str:
        tag = f" [{self.condition}]" if self.condition else ""
        return f"[{self.gate}/{self.severity}]{tag} {self.subject}: {self.reason}"


@dataclass
class Closure:
    """The transitive result: which work packages were reached, which
    requirement edges were evaluated, and the obligation-level graph the
    SCC detection runs over."""
    work_packages: list[str] = field(default_factory=list)
    obligation_edges: dict[str, set[str]] = field(default_factory=dict)
    requirement_edges: list[tuple[str, str, dict, int]] = field(default_factory=list)
    unsupported: list[tuple[str, str]] = field(default_factory=list)
    depth_by_obligation: dict[str, int] = field(default_factory=dict)


def work_package_manifest_dir_for(workspace: Path) -> Path:
    """ci/manifest/<work_package>.json -- the layout the work-package
    manifest fixture and plan.md §10.1's own `protected_write_set`
    (`ci/manifest/**`) already use. Not crate-scoped: a work package is
    an orchestration unit, and §10's own unit is one issue / one owner /
    one worktree / one PR, not one crate."""
    return (workspace.joinpath(*MANIFEST_DIR)).resolve()


def load_assurance_report_validator():
    return make_validator(json.loads(ASSURANCE_REPORT_SCHEMA_PATH.read_text()))


def load_manifests(workspace: Path) -> tuple[dict[str, dict], list[Finding]]:
    """Every schema-valid work-package manifest in ci/manifest.

    Schema validity only: the full §10.1 rule set (gate-integrity
    hashing, write-set anchoring, assumption resolution) is
    `validate-work-package`'s job and needs inputs this gate has no
    business re-deriving. A manifest that fails G1a is skipped WITH a
    finding, never silently -- a closure computed over a manifest this
    gate could not read is a closure over a graph nobody has seen."""
    findings: list[Finding] = []
    manifests: dict[str, dict] = {}
    directory = work_package_manifest_dir_for(workspace)
    if not directory.is_dir():
        return manifests, findings

    validator = load_work_package_validator()
    for path in sorted(p for p in directory.iterdir() if p.is_file() and p.suffix == ".json"):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            findings.append(Finding("G14", str(path), f"manifest is not readable JSON: {e}"))
            continue
        errors = list(validator.iter_errors(data))
        if errors:
            findings.append(
                Finding(
                    "G14", str(path),
                    f"manifest is not schema-valid ({errors[0].message}) -- run "
                    "`pipeline.py validate-work-package` on it; it takes no part in any closure "
                    "until it passes",
                )
            )
            continue
        work_package = data["work_package"]
        if work_package != path.stem:
            findings.append(
                Finding(
                    "G14", str(path),
                    f"manifest declares work_package {work_package!r} but its filename says "
                    f"{path.stem!r} -- the id a closure profile names must identify one file",
                )
            )
            continue
        if work_package in manifests:
            findings.append(Finding("G14", work_package, "duplicate work-package manifest id"))
            continue
        manifests[work_package] = data
    return manifests, findings


def build_provider_index(manifests: dict[str, dict]) -> tuple[dict[str, str], list[Finding]]:
    """obligation_id -> the work package that provides it.

    Two work packages providing the same obligation is a hard error, not
    a first-wins pick: which body actually meets the contract would then
    depend on directory iteration order, and the closure would be over a
    graph that isn't the one on disk."""
    providers: dict[str, str] = {}
    findings: list[Finding] = []
    for work_package in sorted(manifests):
        for entry in manifests[work_package]["definition_of_done"]["provided_guarantees"]:
            obligation = entry["obligation_id"]
            if obligation in providers:
                findings.append(
                    Finding(
                        "G14", obligation,
                        f"provided by more than one work package ({providers[obligation]} and "
                        f"{work_package}) -- an ambiguous provider makes the closure graph "
                        "depend on file order",
                    )
                )
                continue
            providers[obligation] = work_package
    return providers, findings


def compute_closure(
    seed_work_packages: list[str], manifests: dict[str, dict], providers: dict[str, str]
) -> Closure:
    """plan.md §8.5 steps 1-2. Breadth-first from the cluster's own work
    packages along required_guarantees, following each required
    obligation to the work package that provides it -- INCLUDING work
    packages outside the declared cluster, which is the entire point of
    a transitive closure: a dependency's dependency's assumption is
    still this cluster's problem."""
    closure = Closure()
    seen: set[str] = set()
    frontier: list[tuple[str, int]] = [(wp, 0) for wp in seed_work_packages if wp in manifests]

    while frontier:
        work_package, depth = frontier.pop(0)
        if work_package in seen:
            continue
        seen.add(work_package)
        closure.work_packages.append(work_package)
        done = manifests[work_package]["definition_of_done"]
        provided = [entry["obligation_id"] for entry in done["provided_guarantees"]]
        for entry in done["required_guarantees"]:
            required = entry["obligation_id"]
            closure.requirement_edges.append((work_package, required, entry["required_assurance"], depth))
            for own in provided:
                closure.obligation_edges.setdefault(own, set()).add(required)
            closure.depth_by_obligation[required] = min(
                closure.depth_by_obligation.get(required, depth + 1), depth + 1
            )
            provider = providers.get(required)
            if provider is None:
                closure.unsupported.append((work_package, required))
                continue
            if provider not in seen:
                frontier.append((provider, depth + 1))
    return closure


def strongly_connected_components(edges: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan, iterative (a deep obligation chain must not blow the Python
    stack in a release gate). Returns every component; callers decide
    what counts as a cycle -- a single-node component IS one when the
    node depends on itself."""
    index_of: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    stack: list[str] = []
    result: list[list[str]] = []
    counter = 0

    nodes = sorted(set(edges) | {t for targets in edges.values() for t in targets})
    for root in nodes:
        if root in index_of:
            continue
        work: list[tuple[str, list[str]]] = [(root, sorted(edges.get(root, ())))]
        index_of[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack[root] = True
        while work:
            node, successors = work[-1]
            if successors:
                child = successors.pop(0)
                if child not in index_of:
                    index_of[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack[child] = True
                    work.append((child, sorted(edges.get(child, ()))))
                elif on_stack.get(child):
                    low[node] = min(low[node], index_of[child])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index_of[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack[member] = False
                    component.append(member)
                    if member == node:
                        break
                result.append(sorted(component))
    return result


def cycles(closure: Closure) -> list[list[str]]:
    """The non-trivial strongly-connected components: size > 1, or a
    single obligation that depends on itself. Anything else is an
    ordinary DAG node and needs no discharge."""
    found: list[list[str]] = []
    for component in strongly_connected_components(closure.obligation_edges):
        if len(component) > 1:
            found.append(component)
        elif component and component[0] in closure.obligation_edges.get(component[0], set()):
            found.append(component)
    return sorted(found)


def closure_context(
    cluster: str,
    profile: dict,
    requiring_work_package: str,
    providing_work_package: str | None,
    obligation_id: str,
    depth: int,
) -> dict:
    """The `context` argument of satisfies(required_profile,
    achieved_record, context) -- defined here, as chainlink #23's own
    docstring said it would be by #25.

    What belongs in it is what a CLUSTER-level policy would need and a
    single edge cannot know: which cluster this edge is being evaluated
    for, the closure kind that cluster claims (a Kani result inside a
    cluster claiming `deductive` is a cluster-level contradiction, not an
    edge-level one -- CG1/CG3), who required this of whom, and how far
    down the closure the edge sits (plan.md §8.5's motivating case is a
    depth-2 assumption, invisible to any depth-1 check).

    satisfies() still ignores it by design: a required_profile is
    authored governance, and silently strengthening or weakening it from
    ambient cluster facts would move a risk decision out of the reviewed
    artifact that owns it. The context exists so that policy can be
    applied HERE, in the gate, with the edge's own provenance attached to
    every finding."""
    return {
        "cluster": cluster,
        "declared_closure_kind": profile["closure_kind"],
        "declared_owning_verifier": profile["conditions"]["owning_verifier"],
        "requiring_work_package": requiring_work_package,
        "providing_work_package": providing_work_package,
        "obligation_id": obligation_id,
        "closure_depth": depth,
    }


def load_assurance_reports(
    workspace: Path, closure: Closure, manifests: dict[str, dict]
) -> tuple[dict[str, dict], list[Finding]]:
    """The achieved side, one report per work package in the closure, at
    the path that work package's own manifest declares (report.emit).
    Missing, unreadable, schema-invalid, or belonging to a different work
    package are all hard errors: an obligation with no achieved record is
    not "not yet checked", it is a requirement with nothing behind it."""
    findings: list[Finding] = []
    reports: dict[str, dict] = {}
    validator = load_assurance_report_validator()

    for work_package in closure.work_packages:
        manifest = manifests[work_package]
        emit = manifest["report"]["emit"]
        path = (workspace / emit).resolve()
        if not path.is_file():
            findings.append(
                Finding(
                    "G14", work_package,
                    f"no assurance report at its manifest's own report.emit path ({emit}) -- "
                    "nothing achieved has been recorded for this work package",
                )
            )
            continue
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            findings.append(Finding("G14", work_package, f"assurance report {emit} is not readable JSON: {e}"))
            continue
        errors = list(validator.iter_errors(data))
        if errors:
            findings.append(
                Finding("G14", work_package, f"assurance report {emit} is not schema-valid: {errors[0].message}")
            )
            continue
        if data["work_package"] != work_package:
            findings.append(
                Finding(
                    "G14", work_package,
                    f"assurance report {emit} declares work_package {data['work_package']!r} -- "
                    "a report from another work package cannot satisfy this one's obligations",
                )
            )
            continue
        reports[work_package] = data
    return reports, findings


def _obligation_record(report: dict | None, obligation_id: str) -> dict | None:
    if report is None:
        return None
    for entry in report["obligation_records"]:
        if entry["obligation_id"] == obligation_id:
            return entry["record"]
    return None


def _bridge_record(report: dict | None, bridge_id: str) -> dict | None:
    if report is None:
        return None
    for entry in report["bridge_records"]:
        if entry["bridge_id"] == bridge_id:
            return entry["record"]
    return None


def check_provided_guarantees(
    cluster: str, profile: dict, closure: Closure, manifests: dict[str, dict], reports: dict[str, dict]
) -> list[Finding]:
    """"Every body meets its provided contracts" (plan.md §8.5's cycle
    clause, but required of every node, cyclic or not): a work package's
    own provided_guarantees carry their own required_assurance, and the
    record it emitted for each has to satisfy it."""
    findings: list[Finding] = []
    for work_package in closure.work_packages:
        report = reports.get(work_package)
        for entry in manifests[work_package]["definition_of_done"]["provided_guarantees"]:
            obligation = entry["obligation_id"]
            record = _obligation_record(report, obligation)
            if record is None:
                findings.append(
                    Finding(
                        "G14", obligation,
                        f"{work_package} provides this obligation but recorded no achieved "
                        "assurance for it",
                    )
                )
                continue
            context = closure_context(cluster, profile, work_package, work_package, obligation, 0)
            result = satisfies(entry["required_assurance"], record, context)
            if not result:
                findings.append(
                    Finding(
                        "G14", obligation,
                        f"{work_package}'s own provided guarantee is not met by what it achieved: "
                        + "; ".join(result.reasons),
                    )
                )
    return findings


def check_requirements(
    cluster: str, profile: dict, closure: Closure, providers: dict[str, str], reports: dict[str, dict]
) -> list[Finding]:
    """plan.md §8.5 steps 4 and 6, over every edge in the TRANSITIVE
    closure rather than the cluster's direct edges only."""
    findings: list[Finding] = []

    for work_package, obligation in closure.unsupported:
        findings.append(
            Finding(
                "G14", obligation,
                f"required by {work_package} but provided by no work package in this workspace -- "
                "an unsupported dependency (plan.md §8.5 step 6)",
            )
        )

    for work_package, obligation, required_assurance, depth in closure.requirement_edges:
        provider = providers.get(obligation)
        if provider is None:
            continue  # already reported as unsupported
        record = _obligation_record(reports.get(provider), obligation)
        if record is None:
            findings.append(
                Finding(
                    "G14", obligation,
                    f"required by {work_package} at closure depth {depth}, provided by {provider}, "
                    "which recorded no achieved assurance for it",
                )
            )
            continue
        context = closure_context(cluster, profile, work_package, provider, obligation, depth)
        result = satisfies(required_assurance, record, context)
        if not result:
            findings.append(
                Finding(
                    "G14", obligation,
                    f"{work_package} -> {obligation} (provided by {provider}, closure depth "
                    f"{depth}) does not satisfy the requirement: " + "; ".join(result.reasons),
                )
            )
    return findings


def entry_trust_policies(closure: Closure, manifests: dict[str, dict], seed: list[str]) -> list[tuple[str, set[str]]]:
    """The cluster's OWN entry requirements' allow-lists -- what step 5
    holds the whole closure to. A missing trust_policy means no
    assumption is allowed, matching satisfies()'s own fail-closed reading
    of an absent allow-list."""
    policies: list[tuple[str, set[str]]] = []
    for work_package in seed:
        manifest = manifests.get(work_package)
        if manifest is None:
            continue
        done = manifest["definition_of_done"]
        for entry in done["provided_guarantees"] + done["required_guarantees"]:
            allowed = ((entry["required_assurance"].get("trust_policy") or {}).get("assumptions_allowed")) or []
            policies.append((entry["obligation_id"], set(allowed)))
    return policies


def check_transitive_assumptions(
    closure: Closure, manifests: dict[str, dict], reports: dict[str, dict], seed: list[str]
) -> list[Finding]:
    """plan.md §8.5 step 5, and the section's motivating failure: "Direct
    A->B checking passes while a C-level assumption sits below policy."

    Every assumption appearing in any achieved record anywhere in the
    closure is held against every entry requirement's allow-list -- not
    against the allow-list of the edge it happens to sit on, which step 4
    already did."""
    findings: list[Finding] = []
    policies = entry_trust_policies(closure, manifests, seed)

    assumptions: dict[str, list[str]] = {}
    for work_package in closure.work_packages:
        report = reports.get(work_package)
        if report is None:
            continue
        for entry in report["obligation_records"] + report["bridge_records"]:
            record = entry["record"]
            for assumption in ((record.get("trust") or {}).get("assumptions") or []):
                assumptions.setdefault(assumption, []).append(
                    f"{work_package}:{entry.get('obligation_id') or entry.get('bridge_id')}"
                )

    for assumption in sorted(assumptions):
        for obligation_id, allowed in policies:
            if assumption not in allowed:
                findings.append(
                    Finding(
                        "G14", assumption,
                        f"assumption is relied on at {', '.join(sorted(assumptions[assumption]))} "
                        f"but is not in the trust policy of this cluster's entry requirement "
                        f"{obligation_id} ({sorted(allowed)!r}) -- trust policy applies across the "
                        "whole closure, not only the edge that carries it",
                        condition="transitive_assumptions_within_policy",
                    )
                )
                break
    return findings


def load_bridges(workspace: Path, descriptor: dict) -> tuple[dict[str, dict], list[Finding]]:
    """Every fully valid bridge specification in the workspace, by
    bridge_id -- the "genuinely valid, not just present" bar, INCLUDING
    G2: `boundary_id` must resolve to a real, valid, promoted boundary
    contract in the bridge's own crate.

    An earlier version scanned the whole workspace in one pass and called
    `validate_bridge_data(path, data, validator)` with no
    `boundaries_by_id` argument, which defaults to `None` --
    `check_boundary_cross_reference`'s own documented behavior for `None`
    is "not checked", a visible info note, never a blocking finding
    (external review, high severity: a cluster was reproduced closing
    successfully even though its bridge's `boundary_id` resolved
    nowhere). "Every bridge must pass before closure" is meaningless if
    the boundary half of a bridge's own G2 check never ran.

    Fixed by scanning CRATE BY CRATE, the same way
    `pipeline.py`'s `cmd_validate_bridge` does: each crate's boundaries
    are loaded once via `load_boundaries_by_id`, and only bridges
    directly under that crate are validated against them -- preserving
    `validate_bridge.py`'s own "must be in the same crate" scoping rather
    than merging every crate's boundaries into one dict, which would let
    a bridge's `boundary_id` resolve against a DIFFERENT crate's boundary
    of the same name and silently defeat that scoping instead of fixing
    the original gap.

    A SECOND review pass found that fix was incomplete: per-crate
    VALIDATION was correctly scoped, but the RESULT was still merged into
    one flat `bridges: dict[str, dict]` keyed only by `bridge_id`, with
    `setdefault` -- first-crate-wins. That reintroduces cross-crate
    substitution one step later: if crate A's own bridge fails validation
    (e.g. its boundary contract is removed) while crate B happens to
    supply a DIFFERENT, independently valid bridge under the SAME
    bridge_id, crate A's requirement resolves to crate B's bridge, and a
    cluster built entirely from crate A's own work packages was
    reproduced closing successfully over crate B's substitute. Work
    packages carry no explicit crate association today (`functions[]` are
    Rust module paths, not a crate pointer), so true crate-qualified
    resolution through a work package's own requirement is not available
    without a larger schema change. What IS available now: `bridge_id` is
    supposed to be a workspace-wide identifier naming exactly one bridge,
    the same way `boundary_id`/`interaction_id` are -- so a bridge_id
    DISCOVERED (a real file whose `bridge_id` field names it, regardless
    of whether that particular copy is itself individually valid) under
    more than one crate is not "the first valid one wins," it is an
    identity collision, and is refused everywhere rather than resolved
    from whichever crate happened to validate. This is the same choice
    #14 already made for a work package providing an obligation twice:
    ambiguous, never first-wins."""
    bridges: dict[str, dict] = {}
    discovered_in: dict[str, set[str]] = {}
    findings: list[Finding] = []
    if not workspace.is_dir():
        return bridges, findings
    validator = load_bridge_validator()
    for crate in descriptor["crates"]:
        crate_root = (workspace / crate["crate_dir"]).resolve()
        if not crate_root.is_dir():
            continue
        specs_search_root = workspace / crate["specs_search_root"]
        boundaries_by_id = load_boundaries_by_id(boundary_dir_for(crate, workspace), specs_search_root)
        for path in sorted(find_bridge_files(crate_root)):
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(data, dict) or not isinstance(data.get("bridge_id"), str):
                continue
            bridge_id = data["bridge_id"]
            discovered_in.setdefault(bridge_id, set()).add(crate["crate_dir"])
            if any(
                f.severity == "error"
                for f in validate_bridge_data(path, data, validator, boundaries_by_id)
            ):
                continue
            bridges.setdefault(bridge_id, data)

    for bridge_id in sorted(discovered_in):
        crates = sorted(discovered_in[bridge_id])
        if len(crates) > 1:
            findings.append(
                Finding(
                    "G14", bridge_id,
                    f"discovered under more than one crate ({', '.join(crates)}) -- bridge_id is a "
                    "workspace-wide identifier naming exactly one bridge, so this is an identity "
                    "collision, not a choice between them; resolving it from either crate would let "
                    "one crate's bridge silently stand in for a different crate's requirement of the "
                    "same id",
                )
            )
            bridges.pop(bridge_id, None)
    return bridges, findings


def check_bridges(
    cluster: str,
    profile: dict,
    closure: Closure,
    manifests: dict[str, dict],
    reports: dict[str, dict],
    bridges: dict[str, dict],
    callsite_unresolved: int | None,
) -> list[Finding]:
    """"Every bridge requirement passes" (plan.md §8.5's cycle clause,
    required of the whole closure here). Three separate things, kept
    separate because they fail for different reasons:

      * the bridge spec resolves and is valid;
      * its achieved record satisfies the manifest's required_assurance
        for it;
      * `callsite_requirement: all-discovered-resolved` actually holds
        against the C_static observation (chainlink #24). That is
        STRICTER than the closure condition
        unresolved_indirect_calls_at_or_above_medium == 0: a low-tier
        unresolved call site is an accepted limitation for G16 and still
        breaks a manifest's demand that every discovered call site be
        resolved. Both are honest; neither implies the other."""
    findings: list[Finding] = []
    for work_package in closure.work_packages:
        report = reports.get(work_package)
        for entry in manifests[work_package]["definition_of_done"]["required_preconditions_to_establish"]:
            bridge_id = entry["bridge_id"]
            spec = bridges.get(bridge_id)
            if spec is None:
                findings.append(
                    Finding(
                        "G14", bridge_id,
                        f"required by {work_package} but resolves to no valid bridge specification "
                        "in this workspace",
                    )
                )
            elif spec["protocol_class"] != "pairwise":
                findings.append(
                    Finding(
                        "G14", bridge_id,
                        f"protocol_class is {spec['protocol_class']!r}: a non-pairwise protocol is "
                        "not closed over by a pairwise bridge argument",
                        condition="protocol_class_all_pairwise",
                    )
                )

            record = _bridge_record(report, bridge_id)
            if record is None:
                findings.append(
                    Finding(
                        "G14", bridge_id,
                        f"{work_package} must establish this bridge but recorded no achieved "
                        "assurance for it",
                    )
                )
            else:
                context = closure_context(cluster, profile, work_package, work_package, bridge_id, 0)
                result = satisfies(entry["required_assurance"], record, context)
                if not result:
                    findings.append(
                        Finding(
                            "G14", bridge_id,
                            f"{work_package}'s bridge requirement is not met: " + "; ".join(result.reasons),
                        )
                    )

            if entry["callsite_requirement"] == "all-discovered-resolved":
                if callsite_unresolved is None:
                    findings.append(
                        Finding(
                            "G14", bridge_id,
                            f"{work_package} requires all discovered call sites resolved, but no "
                            "C_static observation exists to check that against -- run "
                            "`pipeline.py extract-c-static`; an unobserved call graph is not a "
                            "resolved one",
                        )
                    )
                elif callsite_unresolved > 0:
                    findings.append(
                        Finding(
                            "G14", bridge_id,
                            f"{work_package} requires all discovered call sites resolved, but the "
                            f"C_static observation reports {callsite_unresolved} unresolved "
                            "(chainlink #24; note this is stricter than the cluster's "
                            "at-or-above-medium condition)",
                        )
                    )
    return findings


def check_cycles(profile: dict, closure: Closure) -> tuple[list[Finding], list[list[str]]]:
    """CG6. Every non-trivial SCC needs a discharge naming exactly its
    members; every discharge needs an SCC to belong to."""
    findings: list[Finding] = []
    found = cycles(closure)
    discharges = profile.get("scc_discharges") or []
    discharged = [frozenset(d["members"]) for d in discharges]

    for component in found:
        if frozenset(component) not in discharged:
            findings.append(
                Finding(
                    "G14", " <-> ".join(component),
                    "obligations form a cycle with no well-foundedness discharge naming exactly "
                    "these members -- mutual satisfaction is not soundness (CG6): an SCC closes "
                    "only with a step index, a decreasing measure, or a temporal stratification "
                    "showing no instantaneous circular dependence",
                    condition="scc_wellfoundedness_discharged",
                )
            )

    computed = [frozenset(component) for component in found]
    for discharge in discharges:
        if frozenset(discharge["members"]) not in computed:
            findings.append(
                Finding(
                    "G14", " <-> ".join(sorted(discharge["members"])),
                    f"scc_discharges names a {discharge['kind']} discharge for a cycle that does "
                    "not exist in the computed closure -- a stale discharge asserts something "
                    "about a graph that has since changed",
                )
            )
    return findings, found


def observed_verifiers(closure: Closure, reports: dict[str, dict]) -> tuple[set[str], set[str]]:
    """(evidence kinds, verifier systems) actually present in the closure's
    achieved records -- the ground truth `single_verifier_system`,
    `owning_verifier` and `closure_kind` are recomputed against."""
    kinds: set[str] = set()
    for work_package in closure.work_packages:
        report = reports.get(work_package)
        if report is None:
            continue
        for entry in report["obligation_records"] + report["bridge_records"]:
            kind = ((entry["record"].get("evidence") or {}).get("kind"))
            if isinstance(kind, str):
                kinds.add(kind)
    return kinds, {VERIFIER_BY_EVIDENCE_KIND[k] for k in kinds if k in VERIFIER_BY_EVIDENCE_KIND}


def recompute_conditions(
    closure: Closure,
    reports: dict[str, dict],
    bridges_pairwise: bool,
    assumptions_within_policy: bool,
    unresolved_at_or_above_medium_count: int | None,
    has_cycle: bool,
    all_cycles_discharged: bool,
) -> dict[str, object]:
    """Every condition this gate can compute, computed. The one absent
    key is generic_callees_type_universal_or_creusot_owned (CG3): no
    artifact in this pipeline carries the type information it needs, so
    it stays a human declaration and is reported as declared-not-verified
    rather than being quietly counted as checked."""
    kinds, verifiers = observed_verifiers(closure, reports)
    computed: dict[str, object] = {
        "single_verifier_system": len(verifiers) <= 1,
        "protocol_class_all_pairwise": bridges_pairwise,
        "transitive_assumptions_within_policy": assumptions_within_policy,
        "scc_wellfoundedness_discharged": (all_cycles_discharged if has_cycle else "not-applicable"),
    }
    if len(verifiers) == 1:
        computed["owning_verifier"] = next(iter(verifiers))
    if unresolved_at_or_above_medium_count is not None:
        computed["unresolved_indirect_calls_at_or_above_medium"] = unresolved_at_or_above_medium_count
    computed["_evidence_kinds"] = sorted(kinds)
    return computed


def check_declared_conditions(profile: dict, computed: dict) -> list[Finding]:
    """Declared bits are claims to check, not inputs to trust -- the same
    discipline G1b applies to computed eligibility in I, and a
    disagreement is rejected in both directions.

    A mis-declared bit is NEVER excusable by a degradation record: the
    record excuses a condition that genuinely fails, and a profile
    asserting something untrue of its own closure is a different kind of
    wrong."""
    findings: list[Finding] = []
    declared = profile["conditions"]
    for key in CONDITION_KEYS:
        if key not in computed:
            continue
        if declared[key] != computed[key]:
            findings.append(
                Finding(
                    "G17", profile["cluster"],
                    f"conditions.{key} is declared as {declared[key]!r} but the computed closure "
                    f"says {computed[key]!r} -- conditions are recomputed, never stored-and-trusted",
                )
            )
    return findings


def check_closure_kind(profile: dict, computed: dict) -> list[Finding]:
    """G17's first clause against the CLOSURE, not just the profile's own
    two fields (validate_closure.py already checks those against each
    other): a profile may declare owning_verifier: creusot and
    closure_kind: deductive perfectly consistently while its closure
    contains a Kani result three hops down."""
    kinds = set(computed.get("_evidence_kinds") or [])
    if profile["closure_kind"] == "deductive" and (kinds & BOUNDED_EVIDENCE_KINDS):
        return [
            Finding(
                "G17", profile["cluster"],
                f"closure_kind is 'deductive' but the transitive closure contains "
                f"{sorted(kinds & BOUNDED_EVIDENCE_KINDS)!r} -- a bounded result anywhere in the "
                "closure makes the cluster's guarantee bounded (plan.md §4)",
            )
        ]
    if profile["closure_kind"] == "bounded" and kinds and not (kinds & BOUNDED_EVIDENCE_KINDS):
        return [
            Finding(
                "G17", profile["cluster"],
                f"closure_kind is 'bounded' but every result in the closure is deductive "
                f"({sorted(kinds)!r}) -- under-claiming is still a mis-declared profile, and hides "
                "that the cluster could close deductively",
                severity="info",
            )
        ]
    return []


@dataclass
class ClusterOutcome:
    cluster: str
    status: str  # "closes" | "degraded" | "blocked"
    closure_kind: str
    work_packages: int
    obligations: int
    cycles: list[list[str]]
    findings: list[Finding]

    def summary(self) -> str:
        if self.status == "closes":
            return (
                f"cluster {self.cluster!r} closes: closure_kind={self.closure_kind}, "
                f"{self.work_packages} work package(s), {self.obligations} obligation(s) in the "
                f"transitive closure, {len(self.cycles)} cycle(s) discharged"
            )
        if self.status == "degraded":
            return (
                f"cluster {self.cluster!r} does NOT close: degraded under an accepted "
                f"degradation record (declared closure_kind={self.closure_kind})"
            )
        return f"cluster {self.cluster!r} is BLOCKED: closure not established"


def apply_degradation(findings: list[Finding], degradation: dict | None) -> list[Finding]:
    """A degradation record downgrades exactly the findings tagged with a
    condition it names -- and nothing else. Untagged findings (an
    unsatisfied requirement, an unsupported dependency, a missing
    record, a mis-declared bit) keep their severity with a record
    present, because `failed_conditions` can only name closure
    conditions: there is no vocabulary in which a human accepts "the
    proof is missing" as a ceiling."""
    if degradation is None:
        return findings
    excused = set(degradation["failed_conditions"])
    result: list[Finding] = []
    for finding in findings:
        if finding.severity == "error" and finding.condition in excused:
            result.append(
                Finding(
                    finding.gate, finding.subject,
                    finding.reason + f" -- accepted as degradation under ceiling "
                    f"{degradation['ceiling']!r} ({degradation['tracking_issue']})",
                    "degraded", finding.condition,
                )
            )
        else:
            result.append(finding)
    return result


def gate_cluster(
    workspace: Path,
    cluster: str,
    profile: dict,
    degradation: dict | None,
    manifests: dict[str, dict],
    providers: dict[str, str],
    bridges: dict[str, dict],
    callsite_unresolved: int | None,
    unresolved_medium_plus: int | None,
) -> ClusterOutcome:
    findings: list[Finding] = []

    missing = [wp for wp in profile["work_packages"] if wp not in manifests]
    for work_package in missing:
        findings.append(
            Finding(
                "G14", work_package,
                f"cluster {cluster!r} names this work package, but no valid manifest for it was "
                f"found in {work_package_manifest_dir_for(workspace)}",
            )
        )
    seed = [wp for wp in profile["work_packages"] if wp in manifests]

    closure = compute_closure(seed, manifests, providers)
    reports, report_findings = load_assurance_reports(workspace, closure, manifests)
    findings.extend(report_findings)

    findings.extend(check_provided_guarantees(cluster, profile, closure, manifests, reports))
    findings.extend(check_requirements(cluster, profile, closure, providers, reports))

    assumption_findings = check_transitive_assumptions(closure, manifests, reports, seed)
    findings.extend(assumption_findings)

    bridge_findings = check_bridges(
        cluster, profile, closure, manifests, reports, bridges, callsite_unresolved
    )
    findings.extend(bridge_findings)

    cycle_findings, found_cycles = check_cycles(profile, closure)
    findings.extend(cycle_findings)

    computed = recompute_conditions(
        closure,
        reports,
        bridges_pairwise=not any(f.condition == "protocol_class_all_pairwise" for f in bridge_findings),
        assumptions_within_policy=not assumption_findings,
        unresolved_at_or_above_medium_count=unresolved_medium_plus,
        has_cycle=bool(found_cycles),
        all_cycles_discharged=not any(
            f.condition == "scc_wellfoundedness_discharged" for f in cycle_findings
        ),
    )
    findings.extend(check_declared_conditions(profile, computed))
    findings.extend(check_closure_kind(profile, computed))

    # The conditions the gate could not compute, and the ones that
    # compute to a failing value, become their own findings -- tagged, so
    # a degradation record can excuse exactly these and nothing else.
    if computed.get("single_verifier_system") is False:
        findings.append(
            Finding(
                "G14", cluster,
                "the transitive closure spans more than one verifier system "
                f"({computed['_evidence_kinds']!r}) -- cross-verifier composition has no soundness "
                "theorem (CG1)",
                condition="single_verifier_system",
            )
        )
    if unresolved_medium_plus is None:
        findings.append(
            Finding(
                "G14", cluster,
                "no C_static observation exists, so unresolved_indirect_calls_at_or_above_medium "
                "could not be read from the R1/G16 gate -- run `pipeline.py extract-c-static`",
            )
        )
    elif unresolved_medium_plus > 0:
        findings.append(
            Finding(
                "G14", cluster,
                f"{unresolved_medium_plus} unresolved call site(s) at or above medium risk "
                "(chainlink #24's own count, read from gate_r1_g16 rather than recomputed)",
                condition="unresolved_indirect_calls_at_or_above_medium",
            )
        )
    if profile["conditions"]["generic_callees_type_universal_or_creusot_owned"] is not True:
        findings.append(
            Finding(
                "G14", cluster,
                "generic_callees_type_universal_or_creusot_owned is declared as not holding (CG3: "
                "Kani verifies generics per monomorphization)",
                condition="generic_callees_type_universal_or_creusot_owned",
            )
        )
    else:
        findings.append(
            Finding(
                "G14", cluster,
                "generic_callees_type_universal_or_creusot_owned is a HUMAN DECLARATION this gate "
                "cannot verify -- no artifact in this pipeline carries the type information it "
                "would need (CG3). It is not a checked condition, and is reported every run so it "
                "never reads as one",
                severity="info",
                condition="generic_callees_type_universal_or_creusot_owned",
            )
        )

    findings = apply_degradation(findings, degradation)

    errors = [f for f in findings if f.severity == "error"]
    degraded = [f for f in findings if f.severity == "degraded"]
    if errors:
        status = "blocked"
    elif degraded:
        status = "degraded"
    else:
        status = "closes"

    return ClusterOutcome(
        cluster=cluster,
        status=status,
        closure_kind=profile["closure_kind"],
        work_packages=len(closure.work_packages),
        obligations=len(
            set(closure.obligation_edges)
            | {target for targets in closure.obligation_edges.values() for target in targets}
        ),
        cycles=found_cycles,
        findings=findings,
    )


def gate_workspace(workspace: Path, descriptor: dict) -> tuple[list[ClusterOutcome], list[Finding]]:
    """Every cluster with a valid closure profile. Workspace-level
    findings (unreadable manifests, ambiguous providers) are returned
    separately: they are not any one cluster's fault, and attributing
    them to whichever cluster happened to be first would be arbitrary."""
    workspace_findings: list[Finding] = []

    manifests, manifest_findings = load_manifests(workspace)
    workspace_findings.extend(manifest_findings)
    providers, provider_findings = build_provider_index(manifests)
    workspace_findings.extend(provider_findings)
    bridges, bridge_findings = load_bridges(workspace, descriptor)
    workspace_findings.extend(bridge_findings)

    reports = load_callsite_reports(workspace)
    if reports:
        callsite_unresolved = sum(r["callsite_coverage"]["unresolved"] for _, r in reports)
        unresolved_medium_plus = sum(unresolved_at_or_above_medium(r) for _, r in reports)
    else:
        callsite_unresolved = None
        unresolved_medium_plus = None

    outcomes: list[ClusterOutcome] = []
    artifacts = load_cluster_artifacts(workspace)
    for cluster in sorted(artifacts):
        entry = artifacts[cluster]
        if "profile" not in entry:
            workspace_findings.append(
                Finding(
                    "G14", cluster,
                    "has a degradation record but no valid closure profile -- there is nothing for "
                    "the degradation to be a departure from",
                )
            )
            continue
        _, profile = entry["profile"]
        degradation = entry["degradation"][1] if "degradation" in entry else None
        outcomes.append(
            gate_cluster(
                workspace, cluster, profile, degradation, manifests, providers, bridges,
                callsite_unresolved, unresolved_medium_plus,
            )
        )
    return outcomes, workspace_findings


def report_outcomes(outcomes: list[ClusterOutcome], workspace_findings: list[Finding]) -> int:
    """Per-cluster reporting only. There is deliberately no aggregate
    "everything closed" line: closure is a per-cluster property with a
    kind (plan.md §4), and a summary sentence over several clusters is
    exactly the global guarantee this pipeline refuses to make."""
    for finding in workspace_findings:
        print(f"  - {finding}")

    if not outcomes:
        print(
            "FAIL: no closure profile discovered -- there is no cluster to close, and an empty "
            "closure is not a release (run `pipeline.py validate-closure` if you expected one)"
        )
        return EXIT_BLOCKED

    for outcome in sorted(outcomes, key=lambda o: o.cluster):
        print(outcome.summary())
        for finding in outcome.findings:
            print(f"  - {finding}")

    print(PER_CLUSTER_NOTE)

    blocked = [o for o in outcomes if o.status == "blocked"]
    if blocked or any(f.severity == "error" for f in workspace_findings):
        print(f"FAIL: {len(blocked)} cluster(s) blocked, {len(outcomes) - len(blocked)} not blocked")
        return EXIT_BLOCKED

    degraded = [o for o in outcomes if o.status == "degraded"]
    closed = [o for o in outcomes if o.status == "closes"]
    print(
        f"OK: {len(closed)} cluster(s) close under their declared profile, "
        f"{len(degraded)} released under an accepted degradation record"
    )
    return EXIT_OK


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("workspace", type=Path, help="Workspace root holding specs/_closure/ and ci/manifest/")
    parser.add_argument("--descriptor", type=Path, default=None)
    args = parser.parse_args(argv)

    if not args.workspace.is_dir():
        print(f"error: workspace root does not exist or is not a directory: {args.workspace}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    if not closure_dir_for(args.workspace).is_dir():
        print(
            f"error: no closure directory at {closure_dir_for(args.workspace)} -- a release gate "
            "with nothing to close is not a pass",
            file=sys.stderr,
        )
        return EXIT_INPUT_ERROR
    descriptor_path = args.descriptor or (args.workspace / "project-descriptor.json")
    try:
        descriptor = json.loads(descriptor_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: cannot read project descriptor {descriptor_path}: {e}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    outcomes, workspace_findings = gate_workspace(args.workspace, descriptor)
    return report_outcomes(outcomes, workspace_findings)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
