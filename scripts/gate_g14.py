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

CG3 (chainlink #86, derivation made sound in chainlink #93), the one
condition with no artifact behind it that can ALWAYS settle it:
`generic_callees_type_universal_or_creusot_owned`. Which callees are
generic is type information nothing in this pipeline carries, so the
condition is never taken on its declaration alone. It is computed from
the workspace's own work-package artifacts whenever those artifacts
determine it -- see generic_callees_verification(), which reads the
closure AND its declared `depends_on` dependencies per obligation, and
every manifest and assurance ledger anywhere in `ci/` positively (only
creusot-owned evidence and harnesses establish the verdict, never the
mere absence of the Kani kind, chainlink #93) -- and when they do not
(a non-creusot result or harness anywhere, an unrecorded obligation, a
declared dependency that resolves to no manifest), it is a declared
capability gap: an error the gate refuses to pass silently, excused only
by a degradation record that names the condition and carries a tracking
issue, which reports the cluster as degraded under that ceiling rather
than closed. A tracking record the artifacts have outgrown is rejected as
stale, the same two-direction discipline G17 applies to profile and
record where no closure evidence is visible.

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
  degraded  a declared degradation record whose OWN acceptance is
            provenanced and ruled on (chainlink #97) covers exactly the
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

And, chainlink #97, a record that cannot prove its OWN acceptance
excuses nothing either, so the cluster stays blocked rather than being
released under a "degradation" nobody authorized: the record's `review`
block must match an entry the sanctioned approve path appended to this
workspace's ci/results/review_log.jsonl, and a `ratified` human ruling
must cover the record's EXACT current bytes in
ci/results/human_rulings.jsonl (both shared with `accept-promotion` via
degradation_review.py, so ratify-then-edit-one-byte stops covering the
record exactly as it already does for a promotion's artifact set). The
word "accepted" in the degraded summary is load-bearing: a hand-written
review block is indistinguishable on disk from an approved one, so
before this check nothing in this pipeline could tell the two apart.

And, chainlink #100, this gate reads records ONLY through
validate_closure.load_degradation_records() -- chainlink #99's one
validated discovery-and-validation path -- never through the raw
artifact index it scans closure profiles from. The two halves of that
result are both load-bearing here, and neither is a convenience:

  * `valid` is the ONLY source of a record this gate may act on. Before
    #100 the gate took whatever `load_cluster_artifacts_with_invalid()`
    returned in a cluster's "degradation" slot -- per-file validation
    alone, with none of the cross-artifact G17 pass -- so a record
    validate-closure REFUSED was still accepted here: one naming a stale
    excuse beside its own profile downgraded findings and printed
    `released under an accepted degradation record` at exit 0 while
    `validate-closure` failed the same workspace at exit 1, and a record
    mislocated out of specs/_closure/ was not merely refused but never
    looked at, on a workspace whose only record was in the wrong place.
  * `invalid` is what this gate reports, one error per refused record,
    so the two commands' exit status agrees on every one of these
    conditions instead of disagreeing by accident of parsing.

Exactly one refusal is NOT reported, and it is the documented CG3
exception (chainlink #86) rather than an omission: a
generic_callees_type_universal_or_creusot_owned record beside a `true`
declaration is the tracking record plan.md §3 demands of a capability
gap, so the loader keeps it valid and only THIS gate can call it stale,
from closure evidence no profile/record comparison can see. That is the
one condition on which gate-g14 may refuse where validate-closure
accepts; the reverse never happens.

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
import resources  # noqa: E402
from bridge_harness import EVIDENCE_KIND_BY_VERIFIER  # noqa: E402
from gate_r1_g16 import unresolved_at_or_above_medium  # noqa: E402
from review_checkpoint import review_log_path  # noqa: E402
from project_descriptor import boundary_dir_for, bridge_dir_for  # noqa: E402
from satisfies import satisfies  # noqa: E402
from schema_utils import make_validator  # noqa: E402
from validate_boundary_contracts import load_boundaries_by_id  # noqa: E402
from validate_bridge import find_bridge_files  # noqa: E402
from validate_bridge import load_validator as load_bridge_validator  # noqa: E402
from validate_bridge import validate_data as validate_bridge_data  # noqa: E402
from validate_callsites import load_reports as load_callsite_reports  # noqa: E402
from validate_closure import CONDITION_KEYS  # noqa: E402
from validate_closure import DegradationRecords  # noqa: E402
from validate_closure import closure_dir_for  # noqa: E402
from validate_closure import is_degradation_path  # noqa: E402
from validate_closure import load_cluster_artifacts_with_invalid  # noqa: E402
from validate_closure import load_degradation_records  # noqa: E402
from validate_work_package import load_validator as load_work_package_validator  # noqa: E402

DOCS = resources.resource_path("docs")
ASSURANCE_REPORT_SCHEMA_PATH = DOCS / "assurance-report-schema.json"

MANIFEST_DIR = ("ci", "manifest")

DEDUCTIVE_EVIDENCE_KINDS = {"creusot-deductive-check", "verus-deductive-check"}
BOUNDED_EVIDENCE_KINDS = {"kani-bounded-model-check"}
VERIFIER_BY_EVIDENCE_KIND = {
    "creusot-deductive-check": "creusot",
    "verus-deductive-check": "verus",
    "kani-bounded-model-check": "kani",
}

GENERIC_CALLEES_CONDITION = "generic_callees_type_universal_or_creusot_owned"

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


def looks_like_work_package_manifest(data: object) -> bool:
    """Discriminator between a work-package manifest and the other,
    unrelated files that share ci/manifest/ -- in particular the ownership
    manifest `ligature init` installs at ci/manifest/installation.json
    (chainlink #66), whose top-level keys are manifest_schema_version,
    adjudicator, files, gate_hashes and installed_product_version.

    A work-package manifest always carries at least one of these
    top-level keys; the ownership manifest carries none. Discriminating
    on shape rather than on the reserved filename means a future second
    reserved file in the same directory cannot be misread either -- and
    because `definition_of_done` is checked as well as `work_package`, a
    genuinely malformed manifest that has lost the `schema` key (the one
    installation.json also lacks) is still validated, not silently
    skipped."""
    return isinstance(data, dict) and ("work_package" in data or "definition_of_done" in data)


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
        if not looks_like_work_package_manifest(data):
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
    """The achieved side, one report per work package handed in (the
    closure, plus every work package the cluster declares as a
    `depends_on` dependency -- chainlink #93), at the path that work
    package's own manifest declares (report.emit).
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
                    "nothing achieved has been recorded for this work package; record one from "
                    "the verifier's own proof certificates with `ligature record-assurance "
                    "<work-package> --proof <obligation>=<path>` (docs/cli-contract.md §1)",
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
                        "assurance for it -- record one from this work package's proof "
                        "certificates with `ligature record-assurance "
                        f"{work_package} --proof {obligation}=<path>`",
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
                    "which recorded no achieved assurance for it -- the providing work package "
                    "records its own evidence with `ligature record-assurance "
                    f"{provider} --proof {obligation}=<path>`",
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


def check_record_certificates(
    workspace: Path, closure: Closure, reports: dict[str, dict]
) -> list[Finding]:
    """chainlink #92: every obligation record's proof certificate is
    RE-OPENED here, the same way record-assurance checked it before
    recording it -- so "VERIFIED from artifact data" can never mean "a
    JSON file says so".

    `record-assurance` derives each record from the verifier's own
    why3find certificate: it resolves the declared path, requires it to
    sit under a directory named for the obligation's concept, and reads
    the certificate itself (a non-empty `proofs` section, every subgoal
    proved, nothing stale). But nothing on the READ side ever re-opened
    it: the gate took the record's own `evidence.scope.proof_targets` at
    face value, so any schema-valid edit -- a path that does not exist,
    a certificate with `"proofs": {}` -- still closed the cluster while
    write-set-check called the edit clean. A record is a claim ABOUT a
    certificate; only re-checking the certificate turns the claim back
    into evidence.

    So each obligation record in the closure's reports must name at
    least one certificate (`record-assurance` never writes one without
    `proof_targets` -- an absent field is a record nothing can re-open,
    which fails closed rather than being skipped), and each named
    certificate has to survive record-assurance's own reader again:
    containment, existence, the concept anchor, and a real why3find
    certificate that establishes the obligation. Bridge records are
    deliberately out of scope: they carry no `proof_targets` -- their
    evidence is the generated harness check, which `gate_g9` already
    cross-checks against the harness it recompiles to (gate_g9's own
    recorded-check comparison, not this gate's).

    The findings carry no `condition` tag: a degradation record's
    `failed_conditions` vocabulary is closure CONDITION keys only, so a
    record whose certificate cannot be re-checked keeps blocking with a
    degradation record present -- an absence of evidence, never a
    ceiling (see this module's docstring)."""
    # Local import: record_assurance imports this module at load time
    # (for the report validator), so the dependency has to be resolved
    # after both modules are initialized -- the same direction
    # record_assurance uses for gate_g9's bridge half.
    from record_assurance import (  # noqa: E402
        RecordAssuranceError,
        certificate_path_for,
        read_certificate,
    )

    findings: list[Finding] = []
    for work_package in closure.work_packages:
        report = reports.get(work_package)
        if report is None:
            continue
        for entry in report["obligation_records"]:
            obligation = entry["obligation_id"]
            record_ref = f"{work_package}:{obligation}"
            record = entry["record"]
            evidence = record.get("evidence") if isinstance(record, dict) else None
            scope = evidence.get("scope") if isinstance(evidence, dict) else None
            targets = scope.get("proof_targets") if isinstance(scope, dict) else None
            if not isinstance(targets, str) or not targets.strip():
                findings.append(
                    Finding(
                        "G14", record_ref,
                        "the recorded achievement names no proof certificate "
                        "(evidence.scope.proof_targets is absent or empty) -- record-assurance "
                        "always writes one, so this record points at nothing this gate could "
                        "re-open, and an achievement nothing can be re-checked against is a JSON "
                        "file saying so, not achieved assurance (chainlink #92). Re-record it "
                        f"from the certificate with `ligature record-assurance {work_package} "
                        f"--proof {obligation}=<path>`",
                    )
                )
                continue
            for target in targets.split(","):
                target = target.strip()
                if not target:
                    findings.append(
                        Finding(
                            "G14", record_ref,
                            "the recorded achievement's evidence.scope.proof_targets carries an "
                            "empty certificate path between its commas -- record-assurance never "
                            "joins one that way, so part of this record names nothing to re-open "
                            "(chainlink #92). Re-record it from the certificate with `ligature "
                            f"record-assurance {work_package} --proof {obligation}=<path>`",
                        )
                    )
                    continue
                try:
                    certificate = certificate_path_for(workspace, target, obligation)
                except RecordAssuranceError as e:
                    findings.append(
                        Finding(
                            "G14", record_ref,
                            f"the recorded achievement names proof certificate {target!r}, which "
                            "fails the check record-assurance applies before recording it: "
                            f"{e}. A certificate this gate cannot even re-open (missing, outside "
                            "the workspace, or not under this obligation's concept directory) is "
                            "a claim, never achieved assurance (chainlink #92). Re-record it "
                            f"with `ligature record-assurance {work_package} "
                            f"--proof {obligation}=<path>`",
                        )
                    )
                    continue
                try:
                    read_certificate(certificate, workspace, obligation)
                except RecordAssuranceError as e:
                    findings.append(
                        Finding(
                            "G14", record_ref,
                            f"the recorded achievement names proof certificate {target!r}, which "
                            f"does not re-establish {obligation} when re-read: {e}. "
                            "record-assurance counted the proved goals before recording; a "
                            "certificate that no longer carries them (or never did) cannot back "
                            "the record it was recorded from (chainlink #92). Re-run the "
                            "verifier and re-record with `ligature record-assurance "
                            f"{work_package} --proof {obligation}=<path>`",
                        )
                    )
    return findings


def check_record_derivation(
    closure: Closure, manifests: dict[str, dict], reports: dict[str, dict]
) -> list[Finding]:
    """chainlink #92, the rest of the record: record-assurance doesn't
    copy the certificate, it DERIVES the record -- evidence kind,
    verifier and harness from the manifest's own `harness` for that
    guarantee (bridge_harness.EVIDENCE_KIND_BY_VERIFIER's single
    mapping, never a name the record makes up), config from the
    manifest's `provenance`. The gate took those derivations at face
    value the way it took the certificate: flipping evidence.verifier
    creusot->kani, evidence.harness creusot->kani, or config.toolchain
    to "handwritten" still closed as "VERIFIED from artifact data, not
    taken on declaration". So each derived field is RE-derived from the
    same source record-assurance used, and the record has to agree: a
    record naming a verifier its manifest never declared, an evidence
    kind its declared harness does not produce, or a config that is not
    the provenance of the run that supposedly produced it is a claim
    the artifacts contradict -- the same cross-check gate_g9 already
    applies to a report's bridge_records against the harness it
    recompiles to, pointed here at the obligation half.

    A record for an obligation this manifest does NOT provide is
    deliberately left to the certificate check alone: record-assurance
    preserves such entries when a guarantee is dropped and re-recording
    cannot remove them, so there is no manifest value to re-derive from
    and refusing on absence would be a block with no remediation. Every
    record this manifest does provide is held to the manifest.

    No `condition` tag, same as the certificate findings: a degradation
    record excuses closure conditions, never a record its own manifest
    contradicts."""
    findings: list[Finding] = []
    for work_package in closure.work_packages:
        report = reports.get(work_package)
        if report is None:
            continue
        manifest = manifests[work_package]
        guarantees = {
            entry["obligation_id"]: entry
            for entry in manifest["definition_of_done"]["provided_guarantees"]
        }
        provenance = manifest["provenance"]
        expected_config = {
            "toolchain": provenance["toolchain"],
            "target": provenance["target"],
            "features": list(provenance["features"]),
        }
        for entry in report["obligation_records"]:
            obligation = entry["obligation_id"]
            guarantee = guarantees.get(obligation)
            if guarantee is None:
                continue  # nothing to derive from; the certificate check still ran
            record_ref = f"{work_package}:{obligation}"
            record = entry["record"]
            harness = guarantee["harness"]
            evidence = record.get("evidence") if isinstance(record, dict) else None
            evidence = evidence if isinstance(evidence, dict) else {}
            config = record.get("config") if isinstance(record, dict) else None

            mismatches: list[str] = []
            observed_verifier = evidence.get("verifier")
            if observed_verifier != harness:
                mismatches.append(
                    f"evidence.verifier is {observed_verifier!r}, but this manifest declares "
                    f"harness {harness!r} for {obligation} -- record-assurance derives the "
                    "verifier from exactly that field"
                )
            observed_harness = evidence.get("harness")
            if observed_harness != harness:
                mismatches.append(
                    f"evidence.harness is {observed_harness!r}, but the manifest's own harness "
                    f"for {obligation} is {harness!r}"
                )
            expected_kind = EVIDENCE_KIND_BY_VERIFIER.get(harness)
            observed_kind = evidence.get("kind")
            if expected_kind is None:
                mismatches.append(
                    f"the manifest's own harness {harness!r} maps to no evidence kind "
                    f"({sorted(EVIDENCE_KIND_BY_VERIFIER)}) -- record-assurance refuses a harness "
                    "with no reader, so no record could have been derived from it"
                )
            elif observed_kind != expected_kind:
                mismatches.append(
                    f"evidence.kind is {observed_kind!r}, but harness {harness!r} produces "
                    f"{expected_kind!r} -- an evidence kind is derived from the verifier, never "
                    "asserted beside it"
                )
            if config != expected_config:
                mismatches.append(
                    f"config is {config!r}, not the manifest's own provenance "
                    f"(toolchain={expected_config['toolchain']!r}, "
                    f"target={expected_config['target']!r}, "
                    f"features={expected_config['features']!r}) -- record-assurance takes config "
                    "from the provenance block of the run that produced the proof"
                )
            if mismatches:
                findings.append(
                    Finding(
                        "G14", record_ref,
                        "the recorded achievement disagrees with the manifest it would have "
                        "been derived from: " + "; ".join(mismatches)
                        + " (chainlink #92). Re-record it with `ligature record-assurance "
                        f"{work_package} --proof {obligation}=<path>` so the fields come from "
                        "the manifest and certificate again",
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
            record_ref = f"{work_package}:{entry.get('obligation_id') or entry.get('bridge_id')}"

            # chainlink #91: the report schema types each `record` only as an
            # object (docs/assurance-report-schema.json defers the achieved
            # shape to satisfies()), so nothing upstream of this sweep has
            # yet required trust.assumptions to be an id list. Hashing it
            # blindly is a TypeError -- a traceback where the operator
            # needs a finding -- so every shape this field can take is
            # answered with a finding that names it, and the sweep then
            # fails closed: an assumption this gate cannot even read is an
            # assumption it cannot hold against any trust policy.
            trust = record.get("trust")
            if not isinstance(trust, dict):
                findings.append(
                    Finding(
                        "G14", record_ref,
                        f"achieved record's trust is {trust!r}, not an object holding an "
                        "assumptions id list -- the transitive trust sweep cannot hold a value "
                        "of this shape against any entry requirement's trust policy, so it "
                        "fails closed (docs/achieved-assurance-schema.json types trust as an "
                        "object with trust.assumptions: string ids)",
                        condition="transitive_assumptions_within_policy",
                    )
                )
                continue
            raw = trust.get("assumptions")
            if not isinstance(raw, list):
                findings.append(
                    Finding(
                        "G14", record_ref,
                        f"achieved record's trust.assumptions is {raw!r}, not a list of "
                        "assumption ids -- the transitive trust sweep cannot hold a value of "
                        "this shape against any entry requirement's trust policy, so it fails "
                        "closed (docs/achieved-assurance-schema.json types trust.assumptions "
                        "as an id list)",
                        condition="transitive_assumptions_within_policy",
                    )
                )
                continue
            for index, assumption in enumerate(raw):
                if not isinstance(assumption, str):
                    findings.append(
                        Finding(
                            "G14", record_ref,
                            f"achieved record's trust.assumptions[{index}] is {assumption!r}, "
                            "not an assumption id string -- an entry of this shape cannot be "
                            "compared with any trust policy's assumptions_allowed, so the "
                            "transitive sweep fails closed on it instead ("
                            "docs/achieved-assurance-schema.json types each entry as a string)",
                            condition="transitive_assumptions_within_policy",
                        )
                    )
                    continue
                assumptions.setdefault(assumption, []).append(record_ref)

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
    ambiguous, never first-wins.

    A THIRD review pass found a bridge stored at a noncanonical path
    (e.g. `crates/scheduler/not_specs/_bridges/BR-SCHED-TQ-001.json`)
    still resolved and closed a cluster with no finding at all --
    `find_bridge_files` discovers `**/_bridges/**/*` recursively, with
    no anchor to the crate's actual declared layout, and nothing here
    ever checked one. `validate_bridge.py`'s own `validate_crate`
    already draws this exact line (its docstring: discover crate-wide so
    a mislocated artifact is actually found, then reject it by location
    alone, rather than anchoring the scan itself and reproducing the
    same zero-findings outcome by omission) -- `load_bridges` just never
    applied it. Fixed by requiring `path.resolve().parent` equal the
    crate's `bridge_dir_for()` exactly before a discovered file is
    treated as a candidate at all: a mislocated file is reported (G1b)
    and excluded from both the ambiguity tracking above and the
    resolvable set, the same as `validate_crate` already does for the
    standalone CLI."""
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
        canonical_bridge_dir = bridge_dir_for(crate, workspace)
        for path in sorted(find_bridge_files(crate_root)):
            if path.resolve().parent != canonical_bridge_dir:
                findings.append(
                    Finding(
                        "G1b", path,
                        "bridge artifact is not directly under the canonical directory "
                        f"{canonical_bridge_dir} -- found under {path.resolve().parent}",
                    )
                )
                continue
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


def depends_on_scope(closure: Closure, manifests: dict[str, dict]) -> tuple[list[str], list[str]]:
    """chainlink #93: the OTHER edge a work-package manifest declares.
    `compute_closure` follows required_guarantees (obligation edges);
    plan.md §10 generates a work-package-level `depends_on` list beside
    them, and nothing used to read it -- so a Kani-owned work package the
    cluster explicitly declared a dependency on was "ruled out" by never
    being looked at. CG3's claim is about every generic callee the
    cluster rests on, so it has to walk both edges, resolved against the
    whole project's manifests rather than only the closure profile's
    `work_packages` list.

    Returns `(reached, unresolved)`: every work package transitively
    named through `depends_on` from the closure's own work packages and
    NOT already in the closure itself, and every named target that
    resolves to no schema-valid work-package manifest. An unresolved
    target is never silently skipped: a declared dependency the gate
    cannot read is exactly where a Kani-owned generic callee could sit,
    so it fails closed -- generic_callees_verification refuses the
    verdict -- rather than falling out of scope."""
    reached: list[str] = []
    unresolved: list[str] = []
    seen = set(closure.work_packages)
    frontier = list(closure.work_packages)
    while frontier:
        work_package = frontier.pop(0)
        for dependency in manifests[work_package].get("depends_on") or []:
            if dependency in seen:
                continue
            seen.add(dependency)
            if dependency in manifests:
                reached.append(dependency)
                frontier.append(dependency)
            elif dependency not in unresolved:
                unresolved.append(dependency)
    return reached, unresolved


def generic_callees_verification(
    closure: Closure,
    reports: dict[str, dict],
    manifests: dict[str, dict],
    workspace: Path,
) -> tuple[bool, str]:
    """CG3's own question, answered from the artifact data or refused
    honestly (chainlink #86, derivation made sound in chainlink #93):
    (verifiable, the reason it is not).

    `generic_callees_type_universal_or_creusot_owned` is the one
    condition no artifact can ALWAYS settle -- which callees are generic
    is type information nothing in this pipeline carries (CG3,
    plan.md §3), so the `type_universal` disjunct has no authoring path
    at all. The disjunct that CAN be established from artifacts is
    `creusot_owned`, and the pilot report behind #93 showed the 1.1.1
    form establishing it from a proxy that read less than the claim:

      * the claim covers every generic callee the cluster can REACH, so
        the scan walks BOTH dependency edges: the obligation closure
        compute_closure follows, and every `depends_on` the manifests
        declare (depends_on_scope), read against the whole project's
        work-package manifests and assurance ledgers -- not only the
        records of the work packages the closure profile's
        `work_packages` list happened to seed (issue legs C1/C5: a
        Kani-owned work package reached only through `depends_on` used
        to read VERIFIED without its manifest or ledger ever being
        opened);
      * premise (a) is evaluated PER OBLIGATION, not per work package:
        every obligation and bridge in that scope has its own achieved
        record, and the verdict is withheld while any does not (issue
        leg B2 printed VERIFIED at the same moment the gate blocked the
        cluster for "recorded no achieved assurance"); and
      * ownership is a POSITIVE test, never "not visibly Kani": every
        achieved record anywhere in the workspace's ledgers and every
        declared guarantee harness in its manifests must be
        creusot-owned (issue leg B7: a verus-owned cluster satisfied the
        `creusot_owned` disjunct by merely not being Kani -- verus
        establishes neither disjunct, and a harness name no verifier
        here maps owns nothing).

    Anything else -- a non-creusot result or harness anywhere in the
    workspace, an unrecorded obligation, a report that cannot be read, a
    declared `depends_on` resolving to no manifest -- is NOT verified
    and must never read as checked: the returned reason is exactly what
    the gate prints beside the gap it then requires a tracking record
    for. The claim stays relative to the artifacts this workspace can
    name, never a pipeline-wide one (plan.md §4)."""
    if not closure.work_packages:
        return False, "the closure itself contains no work package at all"

    reached, unresolved = depends_on_scope(closure, manifests)
    if unresolved:
        return False, (
            "a declared depends_on names "
            + ", ".join(sorted(unresolved))
            + ", which resolves to no schema-valid work-package manifest in this workspace -- a "
            "declared dependency the gate cannot read is exactly where a Kani-owned generic "
            "callee could sit, so it fails closed rather than out of scope (CG3, chainlink #93)"
        )

    scope = [*closure.work_packages, *reached]
    missing_reports = sorted(work_package for work_package in scope if work_package not in reports)
    if missing_reports:
        return False, (
            "no readable assurance report for "
            + ", ".join(missing_reports)
            + " (this verdict's scope is the closure AND its declared depends_on dependencies) -- "
            "evidence nobody wrote down cannot be ruled out as a Kani result (CG3, chainlink #93)"
        )

    # Premise (a), per obligation (chainlink #93): the closure used to
    # need only >=1 record PER WORK PACKAGE, so one surviving record
    # carried a verdict about obligations nothing was written down for.
    unrecorded: list[str] = []
    for work_package in scope:
        report = reports[work_package]
        done = manifests[work_package]["definition_of_done"]
        recorded = {entry["obligation_id"] for entry in report["obligation_records"]}
        for entry in done["provided_guarantees"]:
            if entry["obligation_id"] not in recorded:
                unrecorded.append(f"{work_package}:{entry['obligation_id']}")
        recorded_bridges = {entry["bridge_id"] for entry in report["bridge_records"]}
        for entry in done["required_preconditions_to_establish"]:
            if entry["bridge_id"] not in recorded_bridges:
                unrecorded.append(f"{work_package}:{entry['bridge_id']} (bridge)")
        if not (report["obligation_records"] or report["bridge_records"]):
            unrecorded.append(f"{work_package} (no achieved assurance record at all)")
    unrecorded.extend(
        f"{obligation} (required by {work_package}, provided by no work package in this workspace)"
        for work_package, obligation in closure.unsupported
    )
    if unrecorded:
        return False, (
            "no achieved assurance record for "
            + ", ".join(unrecorded)
            + " -- premise (a) is evaluated per obligation, not per work package (chainlink #93): "
            "until every obligation and bridge the closure and its declared dependencies cover has "
            "a recorded achievement, a Kani result nobody wrote down cannot be ruled out (CG3)"
        )

    # Ownership, read positively from EVERY work-package artifact in the
    # workspace -- manifests and ledgers alike -- because nothing bounds
    # a generic callee to the work packages the closure profile named
    # (CG2's own point: the call graph is not knowable here).
    validator = load_assurance_report_validator()
    kinds: set[str] = set()
    offending: list[str] = []
    unreadable: list[str] = []
    for work_package in sorted(manifests):
        report = reports.get(work_package)
        if report is None:
            emit = manifests[work_package]["report"]["emit"]
            path = (workspace / emit).resolve()
            if not path.is_file():
                continue  # nothing recorded; the declared harness below still speaks for it
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                unreadable.append(work_package)
                continue
            if (
                not isinstance(data, dict)
                or list(validator.iter_errors(data))
                or data.get("work_package") != work_package
            ):
                unreadable.append(work_package)
                continue
            report = data
        for entry in report["obligation_records"] + report["bridge_records"]:
            label = entry.get("obligation_id") or entry.get("bridge_id", "?")
            record = entry.get("record")
            evidence = record.get("evidence") if isinstance(record, dict) else None
            kind = evidence.get("kind") if isinstance(evidence, dict) else None
            kind = kind if isinstance(kind, str) else f"<unreadable kind {kind!r}>"
            kinds.add(kind)
            if kind != "creusot-deductive-check":
                offending.append(f"{work_package}:{label} ({kind})")
    if unreadable:
        return False, (
            "the assurance report of "
            + ", ".join(sorted(unreadable))
            + " sits at its manifest's own report.emit path but cannot be read as a schema-valid "
            "report -- records this gate cannot read cannot be ruled out Kani-owned "
            "(CG3, chainlink #93)"
        )
    if offending:
        return False, (
            "the workspace's own achieved records include "
            f"{sorted(k for k in kinds if k != 'creusot-deductive-check')!r} results at "
            + ", ".join(offending)
            + " -- creusot ownership is established positively, never by the mere absence of the "
            "Kani kind: Kani verifies generics per monomorphization, and the type_universal "
            "disjunct needs type information no artifact in this pipeline carries "
            "(CG3, plan.md §3, chainlink #93)"
        )

    non_creusot_harnesses = [
        f"{work_package}:{entry['obligation_id']} (harness {entry.get('harness')!r})"
        for work_package in sorted(manifests)
        for entry in manifests[work_package]["definition_of_done"]["provided_guarantees"]
        if entry.get("harness") != "creusot"
    ]
    if non_creusot_harnesses:
        return False, (
            "the project's work-package manifests declare a non-creusot guarantee harness at "
            + ", ".join(non_creusot_harnesses)
            + " -- the creusot_owned disjunct is established from creusot-owned declarations, and "
            "a harness that is merely 'not Kani' (verus, or a name no verifier here maps) owns "
            "nothing this verdict can vouch for (CG3, plan.md §3, chainlink #93)"
        )
    return True, ""


def recompute_conditions(
    closure: Closure,
    reports: dict[str, dict],
    bridges_pairwise: bool,
    assumptions_within_policy: bool,
    unresolved_at_or_above_medium_count: int | None,
    has_cycle: bool,
    all_cycles_discharged: bool,
    *,
    manifests: dict[str, dict],
    workspace: Path,
) -> dict[str, object]:
    """Every condition this gate can compute, computed.

    The other six conditions are computed from the closure and its
    observations exactly as before. The seventh,
    generic_callees_type_universal_or_creusot_owned (CG3), is computed
    only when the workspace's own artifacts determine it --
    generic_callees_verification walks the closure AND its declared
    `depends_on` dependencies per obligation, and every manifest and
    assurance ledger in `ci/` for creusot-owned evidence and harnesses
    (chainlink #93) -- and otherwise stays OUT of this dict with its
    refusal reason under `_cg3_unverified`: an unverifiable condition is
    a capability gap gate_cluster reports and demands a tracking record
    for (chainlink #86), never a checked one -- and
    check_declared_conditions skips keys that are absent, so a
    declaration the artifacts cannot speak to is never refuted by
    silence either. Silence accepted a false `true` before #86; it must
    not start rejecting an honest one now."""
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
    verifiable, reason = generic_callees_verification(closure, reports, manifests, workspace)
    if verifiable:
        computed[GENERIC_CALLEES_CONDITION] = True
    else:
        computed["_cg3_unverified"] = reason
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
    contains a Kani result three hops down.

    `partial` (chainlink #85) deliberately has no branch here. The two
    uniform kinds are claims about EVERY obligation in the closure, so
    evidence kinds can refute one (a Kani result anywhere makes
    `deductive` false) or under-claim the other (`bounded` over an
    all-deductive closure hides that it could close deductively).
    `partial` is the opposite declaration -- only part of the closure is
    verified -- so it claims strictly less than either uniform kind and
    no evidence-kind combination can over-claim through it; and whether
    it is justified is a coverage question (which obligations lack
    records), which this function cannot answer from kinds and whose
    answer is already emitted by the missing-record findings that own
    it. A `partial` profile is therefore reported as declared
    (ClusterOutcome.summary()) and policed everywhere else exactly like
    any other kind."""
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


def check_degradation_provenance(
    workspace: Path,
    cluster: str,
    degradation: tuple[Path, dict] | None,
    descriptor: dict | None,
) -> tuple[list[Finding], dict | None]:
    """chainlink #97, the gate side of #96: a degradation record may
    excuse a closure condition only once its own acceptance is
    provenanced and ruled on.

    #94's pilot finding was that this gate's release route --
    "released under an accepted degradation record" -- consulted
    neither the approval audit log nor a human-ruling log, so a
    hand-written record whose `review` block named a reviewer who never
    reviewed anything released a cluster exactly as a genuinely
    reviewed one would, at exit 0, calling it *accepted*. The two
    shared checks that close that are chainlink #96's
    (degradation_review.degradation_record_gaps): the record's review
    block must match an entry the sanctioned approve path appended to
    this workspace's ci/results/review_log.jsonl, and a `ratified`
    human ruling must cover the record's EXACT current bytes in
    ci/results/human_rulings.jsonl. Both are reused rather than
    re-derived, which is also what keeps the two gates agreeing: the
    ruling's hash is compute_artifact_manifest()'s, the same routine
    `record-ruling` writes with, so ratify-then-edit-one-byte stops
    covering the record exactly as it already does for a promotion's
    artifact set (chainlink #82).

    Returns (findings, accepted_record). `accepted_record` is the
    record itself when it passes BOTH checks and `None` when it does
    not -- and the caller passes that `None` to apply_degradation(), so
    a record that cannot prove its own acceptance excuses nothing at
    all. That is deliberately stronger than merely appending an error:
    an unprovenanced record would otherwise still downgrade the very
    findings it names to "degraded", and the gate would print a
    cluster as released under an accepted degradation record while also
    printing that the record was never accepted. Every finding here is
    UNTAGGED (`condition=None`) for the same reason -- a record cannot
    name its own provenance gap in `failed_conditions`, whose vocabulary
    is closure CONDITION keys only, and must not be able to excuse it.

    A workspace with no degradation record returns ([], None) and is
    entirely unaffected: there is nothing to release under. Both log
    paths are workspace-scoped and shared with the writers
    (review_checkpoint.review_log_path / ruling_log_path), so the gate
    can never be pointed at a different log than `approve` and
    `record-ruling` write to -- the cwd-relative lesson #45 and #82
    already learned twice in this pipeline."""
    if degradation is None:
        return [], None
    record_path, record = degradation

    # Local import, and NOT the acyclic graph chainlink #96 predicted for
    # this edge: generate_promotion_receipt imports validate_promotion_
    # receipt, which imports generate_feature_ledger, which imports THIS
    # module for load_assurance_report_validator -- so reaching
    # degradation_review from gate_g14's top level closes a real cycle
    # (gate_g14 -> degradation_review -> generate_promotion_receipt ->
    # validate_promotion_receipt -> generate_feature_ledger -> gate_g14)
    # and raises ImportError on a partially initialized module. Resolved
    # here, after both modules are initialized, exactly as
    # check_record_certificates() above resolves its record_assurance
    # half for the same reason.
    from degradation_review import degradation_record_gaps  # noqa: E402
    from generate_promotion_receipt import ruling_log_path  # noqa: E402

    gaps = degradation_record_gaps(
        workspace,
        record_path,
        record,
        review_log_path(workspace),
        ruling_log_path(workspace),
        descriptor,
    )
    if not gaps:
        return [], record

    relative = record_path
    try:
        relative = record_path.resolve().relative_to(workspace.resolve())
    except ValueError:
        pass  # outside the workspace: print the absolute path rather than a wrong relative one
    # ONE finding carrying every gap, not one per gap: both checks have to
    # be fixed before the record counts, and repeating the shared "why" and
    # the shared remedy paragraph once per gap buried the cluster's actual
    # closure findings under the same paragraph three times.
    listed = "; ".join(gaps)
    return [
        Finding(
            "G14",
            f"{cluster} ({relative})",
            "this cluster's degradation record cannot be acted on, so it excuses nothing and the "
            f"cluster stays blocked: {listed}. Nothing about a `review` block distinguishes one the "
            "sanctioned approve path wrote from one typed into the file by hand, and a record no "
            "human ruled on covers no ceiling -- `accept-promotion` already gates both of these on "
            "the artifacts it promotes (chainlink #82), and this gate now gates them on the record "
            "that releases a cluster (chainlink #96/#97). Stage the record first -- `ligature draft` "
            "writes the `<target>.json.draft` that `approve` promotes (chainlink #98); writing the "
            "target path directly leaves no draft to approve, so there is nothing for `approve` to "
            "attach its review block to -- then record the review through `ligature approve` and "
            "the ruling through `ligature record-ruling --reviewer <name> --verdict ratified "
            "--artifact <path>`. Re-run the ruling after ANY edit to the record: it is pinned to "
            "the record's exact bytes.",
        )
    ], None


def apply_degradation(findings: list[Finding], degradation: dict | None) -> list[Finding]:
    """A degradation record downgrades exactly the findings tagged with a
    condition it names -- and nothing else. Untagged findings (an
    unsatisfied requirement, an unsupported dependency, a missing
    record, a mis-declared bit) keep their severity with a record
    present, because `failed_conditions` can only name closure
    conditions: there is no vocabulary in which a human accepts "the
    proof is missing" as a ceiling.

    `degradation` here is the ACCEPTED record -- check_degradation_
    provenance()'s second return value, which is None unless the
    record's review block is provenanced AND a ratified human ruling
    covers its current bytes (chainlink #97). A record that fails
    either check reaches this function as None, so it downgrades
    nothing."""
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
    degradation: tuple[Path, dict] | None,
    manifests: dict[str, dict],
    providers: dict[str, str],
    bridges: dict[str, dict],
    callsite_unresolved: int | None,
    unresolved_medium_plus: int | None,
    descriptor: dict | None = None,
) -> ClusterOutcome:
    """Gate one cluster. `degradation` is the (path, record) pair
    validate_closure's own load_cluster_artifacts_with_invalid() returns
    for this cluster's record, or None -- the path is part of the
    contract, not decoration: chainlink #97's provenance gate hashes the
    record's own bytes at that path to check its human ruling, and a
    record dict with no path could only ever be checked by trusting the
    dict."""

    findings: list[Finding] = []

    # chainlink #97: establish whether this cluster's degradation record
    # is ACCEPTED at all before anything downstream consults it. The
    # second return value is the only form of `degradation` the rest of
    # this function may use -- an unprovenanced or unruled record reaches
    # the CG3 staleness check and apply_degradation() as the record data
    # (so its own stale-record diagnostic still fires) but never as a
    # release. Placed first so the reason a cluster cannot be released is
    # the first thing an operator reading the findings sees.
    degradation_findings, accepted_degradation = check_degradation_provenance(
        workspace, cluster, degradation, descriptor
    )
    findings.extend(degradation_findings)
    degradation_record = degradation[1] if degradation is not None else None

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
    # chainlink #93: the achieved side is loaded for everything the
    # cluster DECLARES it depends on, not just the obligation closure --
    # `depends_on` is a manifest's own dependency edge, and a report at a
    # declared dependency's report.emit path is evidence CG3 reads (a
    # missing one blocks here, exactly like any closure work package's).
    dependency_reached, _ = depends_on_scope(closure, manifests)
    scope = Closure(work_packages=[*closure.work_packages, *dependency_reached])
    reports, report_findings = load_assurance_reports(workspace, scope, manifests)
    findings.extend(report_findings)

    # chainlink #92: re-open every certificate a record points at, and
    # re-derive every field record-assurance derives from the manifest,
    # before any of the record's claims are evaluated -- satisfies() can
    # only compare the record's own words against a requirement's words.
    findings.extend(check_record_certificates(workspace, closure, reports))
    findings.extend(check_record_derivation(closure, manifests, reports))

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
        manifests=manifests,
        workspace=workspace,
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

    # chainlink #86: the CG3 condition is never accepted on its
    # declaration alone. Three outcomes, in the order the evidence
    # allows them: the workspace's artifacts determine it -- the closure
    # AND its declared depends_on dependencies, per obligation, with
    # creusot-owned evidence and harnesses project-wide (chainlink #93),
    # reported as verified from artifact data, and a profile bit
    # disagreeing with it is already refused by check_declared_conditions
    # above, in both directions; the artifacts do not determine it and
    # the profile declares it false (a stated gap -- the pre-existing,
    # excusable finding); the artifacts do not determine it and the
    # profile declares it true (THE false declaration #86 names: an
    # error, so a false `true` can never pass silently, excused only by
    # a degradation record naming the condition -- with one the cluster
    # is released as a declared gap under a tracked ceiling, without one
    # it stays BLOCKED). The fourth direction lives here rather than in
    # G17: a tracking record the artifacts have outgrown is stale, and
    # only this gate can see that, because profile and record alone can
    # never tell an unverifiable declaration from a verified one.
    generic_declared = profile["conditions"][GENERIC_CALLEES_CONDITION]
    if GENERIC_CALLEES_CONDITION in computed:
        if generic_declared is True:
            findings.append(
                Finding(
                    "G14", cluster,
                    "generic_callees_type_universal_or_creusot_owned is VERIFIED from artifact "
                    "data, not taken on declaration: every obligation and bridge in the closure "
                    "AND its declared depends_on dependencies has a recorded achievement, the "
                    f"closure's own records are {computed['_evidence_kinds']}, and every other "
                    "achieved record and declared guarantee harness in this workspace's "
                    "work-package manifests and ledgers is creusot-owned as well -- with no "
                    "non-creusot assurance anywhere those artifacts can name, no generic callee "
                    "this cluster declares a dependency on is Kani-owned "
                    "(CG3, plan.md §3, chainlink #93)",
                    severity="info",
                    condition=GENERIC_CALLEES_CONDITION,
                )
            )
        if degradation_record is not None and GENERIC_CALLEES_CONDITION in degradation_record["failed_conditions"]:
            findings.append(
                Finding(
                    "G14", cluster,
                    "the cluster's degradation record still names "
                    f"{GENERIC_CALLEES_CONDITION!r} in failed_conditions, but this workspace's "
                    "artifacts verify the condition -- a stale tracking record keeps a closed gap "
                    "reading as degraded; remove it and shut the tracking issue "
                    f"({degradation_record['tracking_issue']})",
                )
            )
    elif generic_declared is not True:
        findings.append(
            Finding(
                "G14", cluster,
                "generic_callees_type_universal_or_creusot_owned is declared as not holding (CG3: "
                "Kani verifies generics per monomorphization)",
                condition=GENERIC_CALLEES_CONDITION,
            )
        )
    else:
        findings.append(
            Finding(
                "G14", cluster,
                "generic_callees_type_universal_or_creusot_owned is declared as true, but this "
                f"workspace's artifacts cannot verify it: {computed['_cg3_unverified']}. That is a "
                "capability gap, not a checked condition (plan.md §3): an unverifiable declaration "
                "is never a free pass, and releasing this cluster needs a degradation record "
                "naming this condition with a tracking issue",
                condition=GENERIC_CALLEES_CONDITION,
            )
        )

    # chainlink #97: `accepted_degradation`, never the raw record -- a
    # record whose review block is unprovenanced or whose bytes no
    # ratified ruling covers downgrades nothing here, so the cluster it
    # was supposed to release stays BLOCKED with both reasons visible
    # (the provenance gap above, and the unexcused condition finding).
    findings = apply_degradation(findings, accepted_degradation)

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


def check_degradation_record_validity(
    workspace: Path,
    records: DegradationRecords,
    already_named: set[Path],
) -> list[Finding]:
    """chainlink #100: every degradation record chainlink #99's canonical
    loader REFUSED, as an error-severity workspace finding carrying the
    loader's own reasons.

    `already_named` is the set of record paths this gate has already
    emitted a finding for, and every refusal is filtered through it so a
    record is named ONCE. Two of #49's diagnostics predate the loader
    and are kept rather than replaced, because both say something the
    loader's reasons do not:

      * a record whose own file did not validate is already named as
        `ignored as invalid and excluded from closure computation`, and
        the loader's reasons for it are the same G1a/G1b findings
        `validate-closure` prints -- restating them here would print one
        refusal twice;
      * a valid record beside a cluster with no valid profile is already
        named against the CLUSTER (`has a degradation record but no valid
        closure profile`), which is the fact that matters here: there is
        no closure to release, so the record's own refusal is a detail
        under it rather than a second reason.

    Everything else the loader refused gets this finding. Two conditions
    reach it that no other diagnostic in this module covered, and both
    used to be silent or actively wrong:

      * a record naming a STALE excuse beside its own profile (G17's
        record-side direction). validate-closure fails the workspace;
        before #100 this gate never ran that check, so the record
        downgraded the very findings it named and the cluster was
        reported as released under an accepted degradation record at exit
        0. The finding is UNCONDITIONAL -- not one tagged with a
        condition -- because `failed_conditions` is a closed vocabulary
        of closure-condition keys and a record must not be able to excuse
        the fact that it is stale;
      * a record filed OUTSIDE specs/_closure/. validate-closure
        discovers workspace-wide and rejects it by location; this gate's
        own scan only ever read the canonical directory, so on a
        workspace whose only record was mislocated the gate reported
        `OK: 1 cluster(s) close` at exit 0 -- reading "no degradation is
        declared" off a workspace that declares one. The record is named
        here with the path it was found at, so the two commands' exit
        status agrees.

    Every finding is error-severity and untagged, which is what makes
    report_outcomes() return EXIT_BLOCKED: a refused record excuses
    nothing, so the conditions it would have covered come back unexcused
    as their own errors and the cluster stays blocked regardless. That
    is the fail-closed reading, and it is the whole point -- the record
    is reported as present-and-unusable rather than quietly treated as
    absent."""
    findings: list[Finding] = []
    for path, reasons in sorted(records.invalid.items()):
        if path in already_named:
            continue
        try:
            subject = path.resolve().relative_to(workspace.resolve())
        except ValueError:
            subject = path  # outside the workspace: name the real path rather than a wrong relative one
        listed = "; ".join(str(r) for r in reasons)
        findings.append(
            Finding(
                "G14", str(subject),
                "this degradation record is refused by `pipeline.py validate-closure`, so it "
                f"excuses nothing and every closure condition it names comes back unexcused: "
                f"{listed}. Run `pipeline.py validate-closure` for the full report; fix the "
                "record (or the profile beside it) and re-run this gate. A record this workspace "
                "holds but may not act on is never the same as a workspace that declares no "
                "degradation at all (chainlink #49, #99, #100).",
            )
        )
    return findings


def gate_workspace(workspace: Path, descriptor: dict) -> tuple[list[ClusterOutcome], list[Finding]]:
    """Every cluster with a valid closure profile. Workspace-level
    findings (unreadable manifests, ambiguous providers) are returned
    separately: they are not any one cluster's fault, and attributing
    them to whichever cluster happened to be first would be arbitrary.

    chainlink #49: a closure artifact excluded by
    load_cluster_artifacts_with_invalid() because its own validation
    produced an error-severity finding still gets a workspace finding
    naming it -- fail-closed exclusion from closure computation is
    correct (validate_closure.py's own "genuinely valid, not just
    present" bar), but dropping the reason on the floor is not. Reuses
    validate_closure.py's own single validation pass rather than
    re-validating each artifact here to recover the diagnostic.

    chainlink #100: degradation RECORDS come from #99's canonical
    validated loader, not from this scan's artifact index, and that is
    the only difference in where a record is read from. Closure
    PROFILES still come from load_cluster_artifacts_with_invalid(),
    which #99 left with its exact prior signature and behavior and which
    no record-side rule depends on.

    Two scans, deliberately, rather than one function returning both.
    They share validate_closure's `_scan_closure_dir()` underneath, so
    the RULES are still applied once by one module; what is duplicated is
    a directory walk of a handful of JSON files, and the alternative
    would be widening #99's loader -- whose shape chainlink #101 also
    consumes from `project_state` -- to carry profiles it has no
    business validating. Correctness of the shared verdict does not
    depend on the walk being shared, only on the rules being."""
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
    artifacts, invalid_artifacts = load_cluster_artifacts_with_invalid(workspace)
    # chainlink #100: the ONE validated record result. `records.valid` is
    # what a cluster's degradation may come from below -- the raw
    # `entry["degradation"]` this gate used to read is deliberately never
    # consulted, because it carries per-file validation only and would
    # re-admit exactly the records the loader refuses.
    records = load_degradation_records(workspace)

    already_named: set[Path] = set()
    for path in invalid_artifacts:
        if is_degradation_path(path):
            # #49's diagnostic, kept verbatim for a record whose own file
            # did not validate; the loader's reasons for it are the same
            # findings validate-closure prints, so naming it again would
            # print the refusal twice.
            already_named.add(path)
        workspace_findings.append(
            Finding(
                "G14", str(path),
                "ignored as invalid and excluded from closure computation -- run "
                "`pipeline.py validate-closure` for the detailed finding(s) that invalidated it",
            )
        )
    for cluster in sorted(artifacts):
        entry = artifacts[cluster]
        if "profile" not in entry:
            if "degradation" in entry:
                already_named.add(entry["degradation"][0])
            workspace_findings.append(
                Finding(
                    "G14", cluster,
                    "has a degradation record but no valid closure profile -- there is nothing for "
                    "the degradation to be a departure from",
                )
            )
            continue
        _, profile = entry["profile"]
        # chainlink #97: the WHOLE (path, record) pair, not just the
        # record -- the provenance gate needs the record's real path to
        # hash the bytes its human ruling must cover. The descriptor
        # travels with it for compute_artifact_manifest()'s own
        # witness-aware hashing (a no-op for a degradation record, which
        # is never a witness spec, but keeps the two gates hashing by one
        # routine rather than two).
        #
        # chainlink #100: read from the loader's `valid` half. Absent
        # from it means the record is either absent, or present and
        # refused -- and the refused case is reported by
        # check_degradation_record_validity() below, so neither can read
        # as the other.
        degradation = records.valid.get(cluster)
        outcomes.append(
            gate_cluster(
                workspace, cluster, profile, degradation, manifests, providers, bridges,
                callsite_unresolved, unresolved_medium_plus, descriptor,
            )
        )
    workspace_findings.extend(
        check_degradation_record_validity(workspace, records, already_named)
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
