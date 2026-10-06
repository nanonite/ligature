"""Deterministic planning and rendering for Stage 7 work-package manifests (#121).

This is the *derivation* half of the Stage 7 generator: it turns a
non-authoritative work-package plan plus the workspace's validated
authoritative artifacts into the canonical ``work-package-manifest/1.0``
shape and its serialized bytes. It is deliberately side-effect free --
it never writes under ``ci/manifest``, never creates an authorization
record, and never mutates the workspace. The authorized writer that
consumes this layer is a separate task (chainlink #122); connecting it to
the Stage 7/8 gates is #123.

Two inputs, two different kinds of authority
--------------------------------------------

``WorkPackageRequest`` is the *plan*: identity and scope only (which work
package, which issue, which functions, which obligations/bridges/
assumptions this package owns). It is authoritative for the *selection*
-- the pipeline never invents which obligations a package owns -- but it
is not authoritative for the *content* of any normative value. A plan
that names an obligation the authoritative artifacts do not define is a
hard error, never a value to invent.

``AuthoritativeState`` is the bundle of already-validated project
artifacts the content is read from: the project descriptor, the cluster's
closure profile, the cluster's promotion receipt, the boundary contracts,
the interaction specs, the bridge specs, and (optionally) a declared
gate-hash map. Every value in the output is either a constant of the
schema, a reference resolved against one of those artifacts, or a value
computed mechanically from one (a hash, a canonical report path, a
canonical sort order).

Field -> authoritative source (and precedence)
-----------------------------------------------

The table is also exposed at runtime as :data:`FIELD_SOURCES` so a
consumer can read the mapping instead of the module:

==============================  ======================================================  =========================================
manifest field                  authoritative source                                       precedence / rule
==============================  ======================================================  =========================================
``schema``                      constant ``work-package-manifest/1.0``                      fixed
``work_package``                ``WorkPackageRequest.work_package``                        plan wins; pattern-validated
``issue``                       ``WorkPackageRequest.issue``                               plan wins; ``chainlink:N``
``depends_on``                  ``WorkPackageRequest`` refs                                 plan selects; must resolve inside the
                                                                                            closure profile's ``work_packages``
``coupling_notes``              ``WorkPackageRequest.coupling_notes``                      plan wins (advisory, never a merge order)
``scheduling_rule``             ``WorkPackageRequest`` or the default                      plan overrides the documented default
``functions``                   ``WorkPackageRequest.functions``                           plan selects; must name a declared crate
``obligations``                 derived flat index                                          sorted union of provided obligations + bridges
``provenance.promotion_id``     promotion receipt (exactly one per cluster)                receipt wins; missing/ambiguous refused
``provenance.artifact_set_hash`` promotion receipt's ``artifact_manifest``                 recomputed; stale receipt refused
``provenance.base_commit``      ``AuthoritativeState.base_commit`` (VCS HEAD)              caller supplies the real commit
``provenance.toolchain``        ``AuthoritativeState.toolchain``                           caller supplies the workspace pin
``provenance.target``           ``AuthoritativeState.target``                              caller supplies / interaction config scope
``provenance.features``         ``AuthoritativeState.features``                            caller supplies / interaction config scope
``gate_integrity``              descriptor ``gate_integrity`` + fixed renderer paths       real file hash wins over a declared one;
                                                                                            a declared mismatch is stale and refused
``write_policy.allowed_*``      ``WorkPackageRequest.allowed_write_set``                   plan narrows the descriptor default
``write_policy.protected_*``    ``WorkPackageRequest.protected_write_set``                 plan wins; validated against the descriptor
``definition_of_done.*``        interaction specs (``reliances[]``) + bridge specs         content from the reliance; plan selects
``trusted_assumptions``         boundary contracts / assumption registry + plan           ref resolves authoritatively; risk/mitigations
                                                                                            are the plan's reviewed declaration
``gates``                       ``WorkPackageRequest.gates``                               plan wins; ids unique, names exact
``failure_policy``              constant (plan.md §10)                                     fixed
``report.emit``                 derived from ``work_package``                              canonical ``ci/results/<WP>.json``
==============================  ======================================================  =========================================

Rejections
----------

``derive_manifest`` refuses, with an actionable :class:`ManifestDerivationError`:

* an unsupported field in a plan mapping (:meth:`WorkPackageRequest.from_mapping`);
* a duplicate identifier anywhere a set is declared (functions, dependencies,
  obligations, bridges, gates, write-set patterns, gate runners, features,
  assumption references);
* a dangling dependency (a ``depends_on`` entry that resolves to no work
  package in the cluster's closure profile) or a self-dependency;
* a self-referential guarantee (an obligation this package both provides and
  requires);
* a stale integrity hash (a declared gate hash that no longer matches the file
  on disk, or a promotion artifact whose current bytes no longer match the
  receipt);
* a missing or ambiguous authoritative input (no closure profile / receipt /
  interaction / bridge for a referenced id, or more than one);
* a contradiction between the resolved assurance and the descriptor's declared
  verifier policy for the cluster.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import validate_work_package

# ---------------------------------------------------------------------------
# Constants. Every one is a named fact about the manifest contract rather
# than a literal buried in the assembly code.
# ---------------------------------------------------------------------------

SCHEMA_ID = "work-package-manifest/1.0"

#: Canonical report location. Derived, never a caller-supplied path: a
#: package's assurance report has exactly one home.
REPORT_DIR = "ci/results"

ON_CONFLICT = "raise_change_request"
CALLSITE_REQUIREMENT = "all-discovered-resolved"

SCHEDULING_RULES = ("reverse-dependency-order", "parallel-modular-assumptions")
DEFAULT_SCHEDULING_RULE = "reverse-dependency-order"

#: Which evidence kind a declared verifier is authoritative for. A resolved
#: assurance whose accepted kinds fall outside this map for the cluster's
#: owning (and supporting) verifiers is contradictory with the descriptor.
VERIFIER_EVIDENCE_KIND = {
    "kani": "kani-bounded-model-check",
    "creusot": "creusot-deductive-check",
    "verus": "verus-deductive-check",
}

CLAIM_KINDS = ("callee-precondition-established", "postcondition-holds")
EVIDENCE_KINDS = tuple(VERIFIER_EVIDENCE_KIND.values())
MITIGATION_KINDS = (
    "test",
    "monitor",
    "proof",
    "static-analysis",
    "environment-control",
    "human-risk-acceptance",
    "witness",
)
RISK_LEVELS = ("low", "medium", "high", "critical")
FORBIDDEN_ACTIONS = (
    "weaken_or_delete_contract",
    "delete_or_stub_harness",
    "add_trusted_assumption",
    "edit_protected_paths",
    "modify_gate_implementation",
    "change_features_or_toolchain",
    "disable_test_registration",
)

#: plan.md §10's fixed failure policy. Not project-configurable: a package
#: that could soften its own failure response would be the self-certifying
#: gate the plan's determinism boundary exists to prevent.
FAILURE_POLICY: dict[str, Any] = {
    "verifier_timeout": "raise_change_request(kind=budget_or_assumption)",
    "obligation_unprovable": "raise_change_request(kind=contract_revision)",
    "missing_callee_guarantee": "raise_change_request(kind=missing_contract)",
    "unintended_call_edge": "raise_change_request(kind=interaction_revision)",
    "unresolved_callsite": "raise_change_request(kind=callsite_unresolved)",
    "forbidden": list(FORBIDDEN_ACTIONS),
}

_WORK_PACKAGE_RE = re.compile(r"^WP-[A-Z][A-Z0-9-]*$")
_ISSUE_RE = re.compile(r"^chainlink:[0-9]+$")
_FUNCTION_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(::[a-zA-Z_][a-zA-Z0-9_]*)+$")
_OBLIGATION_RE = re.compile(r"^[A-Z][A-Za-z0-9]*\.[A-Z][0-9]+$")
_BRIDGE_RE = re.compile(r"^BR-[A-Z][A-Z0-9-]*$")
_HARNESS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CLUSTER_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_BASE_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: The pipeline's own gate-adjacent files. `validate_work_package` requires
#: every manifest to pin them; a manifest this layer derives must therefore
#: pin them too, from the same constant the validator uses.
FIXED_GATE_RUNNERS = tuple(validate_work_package.WITNESS_RENDERER_INTEGRITY_PATHS)


class ManifestDerivationError(Exception):
    """A work-package manifest could not be derived from the given plan and
    authoritative state. The message is user-facing and names what was
    missing, ambiguous, contradictory, or stale; ``code`` is a stable,
    machine-checkable classification of the refusal."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# The documented source map.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldSource:
    field: str
    source: str
    rule: str


FIELD_SOURCES: tuple[FieldSource, ...] = (
    FieldSource("schema", "constant", "fixed work-package-manifest/1.0"),
    FieldSource("work_package", "plan", "plan selects; pattern-validated"),
    FieldSource("issue", "plan", "plan selects; chainlink:N"),
    FieldSource(
        "depends_on",
        "plan + closure profile",
        "plan selects; must resolve inside closure_profile.work_packages",
    ),
    FieldSource(
        "coupling_notes", "plan", "advisory scheduling input, never a merge instruction"
    ),
    FieldSource(
        "scheduling_rule", "plan, defaulted", "plan overrides the documented default"
    ),
    FieldSource(
        "functions", "plan", "plan selects; leading module must name a declared crate"
    ),
    FieldSource(
        "obligations", "derived", "sorted union of provided obligations and bridge ids"
    ),
    FieldSource(
        "provenance.promotion_id",
        "promotion receipt",
        "exactly one receipt per cluster; missing/ambiguous refused",
    ),
    FieldSource(
        "provenance.artifact_set_hash",
        "promotion receipt",
        "recomputed from artifact_manifest; stale receipt refused",
    ),
    FieldSource(
        "provenance.base_commit", "state (VCS HEAD)", "caller supplies the real commit"
    ),
    FieldSource(
        "provenance.toolchain", "state", "caller supplies the workspace toolchain pin"
    ),
    FieldSource(
        "provenance.target",
        "state / interaction config scope",
        "caller supplies; must agree across the package's interactions",
    ),
    FieldSource(
        "provenance.features",
        "state / interaction config scope",
        "caller supplies; must agree across the package's interactions",
    ),
    FieldSource(
        "gate_integrity",
        "descriptor + fixed renderer paths",
        "real file hash wins; a declared mismatch is stale",
    ),
    FieldSource(
        "write_policy.allowed_write_set",
        "plan",
        "plan narrows the descriptor's allowed_roots",
    ),
    FieldSource(
        "write_policy.protected_write_set",
        "plan",
        "plan declares; validated against the descriptor",
    ),
    FieldSource(
        "definition_of_done.provided_guarantees",
        "interaction reliances + plan",
        "content from the reliance; plan selects and names the harness",
    ),
    FieldSource(
        "definition_of_done.required_preconditions_to_establish",
        "bridge specs + interaction reliances",
        "claim kind fixed by the bridge role; evidence from the reliance",
    ),
    FieldSource(
        "definition_of_done.required_guarantees",
        "interaction reliances + plan",
        "content from the reliance; plan selects",
    ),
    FieldSource(
        "definition_of_done.trusted_assumptions",
        "boundary contracts / registry + plan",
        "ref resolves authoritatively; risk/mitigations are the plan's reviewed declaration",
    ),
    FieldSource("gates", "plan", "ids unique, harness/runner names exact"),
    FieldSource("failure_policy", "constant (plan.md §10)", "fixed"),
    FieldSource(
        "report.emit", "derived from work_package", "canonical ci/results/<WP>.json"
    ),
)


# ---------------------------------------------------------------------------
# The plan (request).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateSpec:
    id: str
    runner: str
    args: tuple[str, ...] = ()


_PLAN_FIELDS = (
    "work_package",
    "issue",
    "cluster",
    "functions",
    "depends_on",
    "coupling_notes",
    "provided_obligations",
    "required_obligations",
    "bridges",
    "assumptions",
    "harnesses",
    "gates",
    "scheduling_rule",
    "allowed_write_set",
    "protected_write_set",
)
_ASSUMPTION_ENTRY_FIELDS = {"assumption_ref", "risk", "mitigations"}
_MITIGATION_FIELDS = {"kind", "reference"}
_ASSUMPTION_REFERENCE_FIELDS = {
    "assumption_id",
    "boundary_id",
    "tracking_issue",
    "assumption_hash",
}


def _sequence_field(data: Mapping[str, Any], name: str) -> tuple[Any, ...]:
    value = data.get(name, ())
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Sequence):
        raise ManifestDerivationError(
            "invalid-plan", f"plan field {name!r} must be an array"
        )
    return tuple(value)


def _string_field(data: Mapping[str, Any], name: str) -> str:
    value = data.get(name, "")
    if not isinstance(value, str):
        raise ManifestDerivationError(
            "invalid-plan", f"plan field {name!r} must be a string"
        )
    return value


@dataclass(frozen=True)
class WorkPackageRequest:
    """The plan: identity and scope, with references into authoritative state.

    Construct directly, or via :meth:`from_mapping` when the plan arrives as
    JSON/YAML so an unsupported field is refused by name rather than
    silently ignored."""

    work_package: str
    issue: str
    cluster: str
    functions: tuple[str, ...]
    provided_obligations: tuple[str, ...] = ()
    required_obligations: tuple[str, ...] = ()
    bridges: tuple[str, ...] = ()
    assumptions: tuple[Mapping[str, Any], ...] = ()
    harnesses: Mapping[str, str] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    coupling_notes: tuple[str, ...] = ()
    gates: tuple[GateSpec, ...] = ()
    scheduling_rule: str | None = None
    allowed_write_set: tuple[str, ...] = ()
    protected_write_set: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> WorkPackageRequest:
        if not isinstance(data, Mapping):
            raise ManifestDerivationError(
                "invalid-plan", "work-package plan must be a mapping"
            )
        if any(not isinstance(key, str) for key in data):
            raise ManifestDerivationError(
                "invalid-plan", "work-package plan keys must be strings"
            )
        unsupported = sorted(set(data) - set(_PLAN_FIELDS))
        if unsupported:
            raise ManifestDerivationError(
                "unsupported-field",
                f"unsupported work-package plan field(s): {', '.join(unsupported)}; "
                f"permitted: {', '.join(_PLAN_FIELDS)}",
            )
        gates = tuple(_gate_from_mapping(g) for g in _sequence_field(data, "gates"))
        assumptions = _sequence_field(data, "assumptions")
        if any(not isinstance(entry, Mapping) for entry in assumptions):
            raise ManifestDerivationError(
                "invalid-plan", "assumptions entries must be mappings"
            )
        harnesses = data.get("harnesses", {})
        if not isinstance(harnesses, Mapping):
            raise ManifestDerivationError(
                "invalid-plan", "plan field 'harnesses' must be a mapping"
            )
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in harnesses.items()
        ):
            raise ManifestDerivationError(
                "invalid-plan",
                "harnesses must map obligation id strings to harness name strings",
            )
        return cls(
            work_package=_string_field(data, "work_package"),
            issue=_string_field(data, "issue"),
            cluster=_string_field(data, "cluster"),
            functions=_sequence_field(data, "functions"),
            provided_obligations=_sequence_field(data, "provided_obligations"),
            required_obligations=_sequence_field(data, "required_obligations"),
            bridges=_sequence_field(data, "bridges"),
            assumptions=assumptions,
            harnesses=dict(data.get("harnesses", {})),
            depends_on=_sequence_field(data, "depends_on"),
            coupling_notes=_sequence_field(data, "coupling_notes"),
            gates=gates,
            scheduling_rule=data.get("scheduling_rule"),
            allowed_write_set=_sequence_field(data, "allowed_write_set"),
            protected_write_set=_sequence_field(data, "protected_write_set"),
        )


def _gate_from_mapping(data: Mapping[str, Any]) -> GateSpec:
    if not isinstance(data, Mapping):
        raise ManifestDerivationError(
            "invalid-plan", f"gate entry must be a mapping, got {data!r}"
        )
    unsupported = sorted(set(data) - {"id", "runner", "args"})
    if unsupported:
        raise ManifestDerivationError(
            "unsupported-field",
            f"unsupported gate field(s): {', '.join(unsupported)}; permitted: id, runner, args",
        )
    gate_id = data.get("id", "")
    runner = data.get("runner", "")
    if not isinstance(gate_id, str) or not isinstance(runner, str):
        raise ManifestDerivationError(
            "invalid-plan", "gate id and runner must be strings"
        )
    args = data.get("args", ())
    if isinstance(args, (str, bytes, Mapping)) or not isinstance(args, Sequence):
        raise ManifestDerivationError(
            "invalid-plan", f"gate {gate_id!r} args must be an array"
        )
    if any(not isinstance(arg, str) for arg in args):
        raise ManifestDerivationError(
            "invalid-plan", f"gate {gate_id!r} args must contain strings"
        )
    return GateSpec(id=gate_id, runner=runner, args=tuple(args))


# ---------------------------------------------------------------------------
# The authoritative state.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthoritativeState:
    """Validated project artifacts a manifest is derived from.

    ``workspace_root`` is optional: with it, gate hashes and promotion
    artifacts are re-read from disk (so a stale declared hash is caught); a
    pure in-memory derivation instead requires every gate hash to be
    supplied in ``gate_hashes``."""

    descriptor: Mapping[str, Any]
    closure_profiles: tuple[Mapping[str, Any], ...] = ()
    promotion_receipts: tuple[Mapping[str, Any], ...] = ()
    boundary_contracts: tuple[Mapping[str, Any], ...] = ()
    interactions: tuple[Mapping[str, Any], ...] = ()
    bridge_specs: tuple[Mapping[str, Any], ...] = ()
    base_commit: str = ""
    toolchain: str = ""
    target: str = ""
    features: tuple[str, ...] = ()
    gate_hashes: Mapping[str, str] = field(default_factory=dict)
    workspace_root: Path | None = None


# ---------------------------------------------------------------------------
# Small validation helpers.
# ---------------------------------------------------------------------------


def _reject_duplicates(
    values: Sequence[Any], kind: str, key: Callable[[Any], str] | None = None
) -> None:
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in values:
        canonical = key(value) if key is not None else str(value)
        if canonical in seen and canonical not in duplicates:
            duplicates.append(canonical)
        seen.add(canonical)
    if duplicates:
        raise ManifestDerivationError(
            "duplicate-identifier",
            f"duplicate {kind}: {', '.join(sorted(duplicates))}",
        )


def _validate_request_shape(request: WorkPackageRequest) -> None:
    if not isinstance(request, WorkPackageRequest):
        raise ManifestDerivationError(
            "invalid-plan", "request must be a WorkPackageRequest"
        )
    string_fields = {
        "work_package": request.work_package,
        "issue": request.issue,
        "cluster": request.cluster,
    }
    for name, value in string_fields.items():
        if not isinstance(value, str):
            raise ManifestDerivationError(
                "invalid-plan", f"plan field {name!r} must be a string"
            )
    for name in (
        "functions",
        "provided_obligations",
        "required_obligations",
        "bridges",
        "depends_on",
        "coupling_notes",
        "allowed_write_set",
        "protected_write_set",
    ):
        values = getattr(request, name)
        if isinstance(values, (str, bytes, Mapping)) or not isinstance(
            values, Sequence
        ):
            raise ManifestDerivationError(
                "invalid-plan", f"plan field {name!r} must be an array"
            )
        if any(not isinstance(value, str) for value in values):
            raise ManifestDerivationError(
                "invalid-plan", f"plan field {name!r} must contain only strings"
            )
    if not isinstance(request.harnesses, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in request.harnesses.items()
    ):
        raise ManifestDerivationError(
            "invalid-plan",
            "harnesses must map obligation id strings to harness name strings",
        )
    if request.scheduling_rule is not None and not isinstance(
        request.scheduling_rule, str
    ):
        raise ManifestDerivationError(
            "invalid-plan", "scheduling_rule must be a string"
        )
    if isinstance(request.gates, (str, bytes, Mapping)) or not isinstance(
        request.gates, Sequence
    ):
        raise ManifestDerivationError(
            "invalid-plan", "plan field 'gates' must be an array"
        )
    for gate in request.gates:
        if (
            not isinstance(gate, GateSpec)
            or not isinstance(gate.id, str)
            or not isinstance(gate.runner, str)
        ):
            raise ManifestDerivationError(
                "invalid-plan", "each gate must have string id and runner fields"
            )
        if isinstance(gate.args, (str, bytes, Mapping)) or not isinstance(
            gate.args, Sequence
        ):
            raise ManifestDerivationError(
                "invalid-plan", f"gate {gate.id!r} args must be an array"
            )
        if any(not isinstance(arg, str) for arg in gate.args):
            raise ManifestDerivationError(
                "invalid-plan", f"gate {gate.id!r} args must contain strings"
            )
    if isinstance(request.assumptions, (str, bytes, Mapping)) or not isinstance(
        request.assumptions, Sequence
    ):
        raise ManifestDerivationError(
            "invalid-plan", "plan field 'assumptions' must be an array"
        )


def _validate_authoritative_shape(state: AuthoritativeState) -> None:
    if not isinstance(state, AuthoritativeState):
        raise ManifestDerivationError(
            "invalid-authoritative-state", "state must be an AuthoritativeState"
        )
    if not isinstance(state.descriptor, Mapping):
        raise ManifestDerivationError(
            "invalid-authoritative-state",
            "project descriptor must be a validated mapping",
        )
    crates = state.descriptor.get("crates")
    if isinstance(crates, (str, bytes, Mapping)) or not isinstance(crates, Sequence):
        raise ManifestDerivationError(
            "invalid-authoritative-state", "project descriptor crates must be an array"
        )
    if any(not isinstance(crate, Mapping) for crate in crates):
        raise ManifestDerivationError(
            "invalid-authoritative-state",
            "project descriptor crates entries must be mappings",
        )
    for field_name in (
        "closure_profiles",
        "promotion_receipts",
        "boundary_contracts",
        "interactions",
        "bridge_specs",
    ):
        values = getattr(state, field_name)
        if isinstance(values, (str, bytes, Mapping)) or not isinstance(
            values, Sequence
        ):
            raise ManifestDerivationError(
                "invalid-authoritative-state",
                f"{field_name} must be an array of validated artifacts",
            )
        if any(not isinstance(value, Mapping) for value in values):
            raise ManifestDerivationError(
                "invalid-authoritative-state", f"{field_name} entries must be mappings"
            )
    if isinstance(state.features, (str, bytes, Mapping)) or not isinstance(
        state.features, Sequence
    ):
        raise ManifestDerivationError(
            "invalid-authoritative-state", "features must be an array of strings"
        )
    if any(not isinstance(feature, str) for feature in state.features):
        raise ManifestDerivationError(
            "invalid-authoritative-state", "features must be an array of strings"
        )
    if not isinstance(state.gate_hashes, Mapping):
        raise ManifestDerivationError(
            "invalid-authoritative-state", "gate_hashes must be a path-to-hash mapping"
        )
    if state.workspace_root is not None and not isinstance(state.workspace_root, Path):
        raise ManifestDerivationError(
            "invalid-authoritative-state",
            "workspace_root must be a pathlib.Path or None",
        )


def _require(value: Any, code: str, message: str) -> Any:
    if value is None or value == "" or value == [] or value == {}:
        raise ManifestDerivationError(code, message)
    return value


def _require_match(
    pattern: re.Pattern[str], value: str, code: str, message: str
) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise ManifestDerivationError(code, message)
    return value


def _single_match(
    items: Iterable[Mapping[str, Any]],
    identifier: Callable[[Mapping[str, Any]], Any],
    wanted: str,
    *,
    kind: str,
) -> Mapping[str, Any]:
    if any(not isinstance(item, Mapping) for item in items):
        raise ManifestDerivationError(
            "invalid-authoritative-state", f"{kind} sources must be validated mappings"
        )
    matches = [item for item in items if identifier(item) == wanted]
    if not matches:
        raise ManifestDerivationError(
            "missing-source",
            f"no {kind} found for {wanted!r} in the authoritative state",
        )
    if len(matches) > 1:
        raise ManifestDerivationError(
            "ambiguous-source",
            f"{len(matches)} {kind}s match {wanted!r}; the authoritative source must be unique",
        )
    return matches[0]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


# ---------------------------------------------------------------------------
# Resolution against authoritative artifacts.
# ---------------------------------------------------------------------------


def _resolve_closure_profile(
    state: AuthoritativeState, cluster: str
) -> Mapping[str, Any]:
    return _single_match(
        state.closure_profiles,
        lambda p: p.get("cluster"),
        cluster,
        kind="closure profile",
    )


def _resolve_receipt(state: AuthoritativeState, cluster: str) -> Mapping[str, Any]:
    return _single_match(
        state.promotion_receipts,
        lambda r: r.get("cluster"),
        cluster,
        kind="promotion receipt",
    )


def _resolve_bridge_spec(
    state: AuthoritativeState, bridge_id: str
) -> Mapping[str, Any]:
    return _single_match(
        state.bridge_specs,
        lambda b: b.get("bridge_id"),
        bridge_id,
        kind="bridge specification",
    )


def _reliance_for_obligation(
    state: AuthoritativeState, obligation_id: str
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The single authoritative ``required_assurance`` declared for
    ``obligation_id`` by the interaction specs' ``reliances[]``. Zero is
    missing; more than one *distinct* assurance is contradictory."""
    found = _matching_reliances(state, obligation_id)
    if not found:
        raise ManifestDerivationError(
            "missing-source",
            f"no interaction reliance declares required_assurance for obligation "
            f"{obligation_id!r}; the derivation will not invent one",
        )
    distinct = {_canonical(reliance.get("required_assurance")) for _, reliance in found}
    if len(distinct) > 1:
        interactions = ", ".join(sorted(str(i.get("interaction_id")) for i, _ in found))
        raise ManifestDerivationError(
            "contradictory-source",
            f"obligation {obligation_id!r} is declared with conflicting required_assurance by "
            f"{interactions}",
        )
    return found[0]


def _resolve_obligation_assurance(
    state: AuthoritativeState, obligation_id: str
) -> dict[str, Any]:
    _, reliance = _reliance_for_obligation(state, obligation_id)
    return dict(reliance["required_assurance"])


def _matching_reliances(
    state: AuthoritativeState, obligation_id: str
) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    return [
        (interaction, reliance)
        for interaction in state.interactions
        for reliance in interaction.get("reliances") or ()
        if reliance.get("obligation_id") == obligation_id
    ]


def _validate_interaction_config_scope(
    state: AuthoritativeState, request: WorkPackageRequest
) -> None:
    _reject_duplicates(state.features, "feature")
    obligation_ids = set(request.provided_obligations) | set(
        request.required_obligations
    )
    for bridge_id in request.bridges:
        bridge = _resolve_bridge_spec(state, bridge_id)
        obligation_ids.add(str(bridge.get("callee_requirement")))
    scopes: dict[str, tuple[str, tuple[str, ...]]] = {}
    for obligation_id in sorted(obligation_ids):
        for interaction, _ in _matching_reliances(state, obligation_id):
            realization = interaction.get("realization")
            scope = (
                realization.get("config_scope")
                if isinstance(realization, Mapping)
                else None
            )
            interaction_id = str(interaction.get("interaction_id"))
            if not isinstance(scope, Mapping):
                raise ManifestDerivationError(
                    "missing-source",
                    f"interaction {interaction_id!r} has no config_scope for selected "
                    f"obligation {obligation_id!r}",
                )
            target = scope.get("target")
            features = scope.get("features")
            if not isinstance(target, str) or not target:
                raise ManifestDerivationError(
                    "missing-source",
                    f"interaction {interaction_id!r} has no target in config_scope",
                )
            if isinstance(features, (str, bytes, Mapping)) or not isinstance(
                features, Sequence
            ):
                raise ManifestDerivationError(
                    "invalid-authoritative-state",
                    f"interaction {interaction_id!r} config_scope.features must be an array",
                )
            if any(not isinstance(feature, str) or not feature for feature in features):
                raise ManifestDerivationError(
                    "invalid-authoritative-state",
                    f"interaction {interaction_id!r} config_scope.features must contain non-empty strings",
                )
            _reject_duplicates(features, f"features for {interaction_id}")
            scopes[interaction_id] = (target, tuple(sorted(features)))
    if not scopes:
        return
    if len(set(scopes.values())) > 1:
        detail = ", ".join(
            f"{name}={scope!r}" for name, scope in sorted(scopes.items())
        )
        raise ManifestDerivationError(
            "contradictory-source",
            f"selected interactions disagree on target/features config_scope: {detail}",
        )
    expected = (state.target, tuple(sorted(state.features)))
    actual = next(iter(scopes.values()))
    if actual != expected:
        raise ManifestDerivationError(
            "contradictory-source",
            f"provenance target/features {expected!r} do not match selected interaction "
            f"config_scope {actual!r}",
        )


def _resolve_bridge_assurance(
    state: AuthoritativeState, bridge: Mapping[str, Any]
) -> dict[str, Any]:
    """A bridge discharges a callee *precondition*, so its claim kind is fixed
    by its role (plan.md §8.2); the accepted evidence kinds come from the
    authoritative reliance on the bridge's own ``callee_requirement``."""
    _, reliance = _reliance_for_obligation(state, str(bridge.get("callee_requirement")))
    reliance_assurance = reliance["required_assurance"]
    assurance: dict[str, Any] = {
        "required_claims": ["callee-precondition-established"],
        "accepted_evidence_kinds": list(reliance_assurance["accepted_evidence_kinds"]),
    }
    for optional in ("minimum_scope", "trust_policy"):
        if optional in reliance_assurance:
            assurance[optional] = reliance_assurance[optional]
    return assurance


def _assumption_ref_key(ref: Mapping[str, Any]) -> str:
    if "assumption_id" in ref:
        return str(ref["assumption_id"])
    return f"{ref.get('boundary_id')}:{ref.get('tracking_issue')}:{ref.get('assumption_hash')}"


def _validate_assumption_ref(state: AuthoritativeState, ref: Mapping[str, Any]) -> None:
    if not isinstance(ref, Mapping):
        raise ManifestDerivationError(
            "invalid-plan", "assumption_ref must be a mapping"
        )
    if "assumption_id" in ref:
        if set(ref) != {"assumption_id"}:
            raise ManifestDerivationError(
                "invalid-plan",
                "assumption_ref with assumption_id may contain no other fields",
            )
        _require_match(
            re.compile(r"^ASM-[a-z0-9][a-z0-9-]*$"),
            ref["assumption_id"],
            "invalid-plan",
            f"assumption_id {ref['assumption_id']!r} is not an ASM- id",
        )
        return
    if set(ref) != {"boundary_id", "tracking_issue", "assumption_hash"}:
        unexpected = sorted(set(ref) - _ASSUMPTION_REFERENCE_FIELDS)
        missing = sorted(
            {"boundary_id", "tracking_issue", "assumption_hash"} - set(ref)
        )
        detail = []
        if missing:
            detail.append(f"missing {', '.join(missing)}")
        if unexpected:
            detail.append(f"unsupported {', '.join(unexpected)}")
        raise ManifestDerivationError(
            "invalid-plan",
            "legacy assumption_ref must contain exactly boundary_id, "
            "tracking_issue, and assumption_hash (" + "; ".join(detail) + ")",
        )
    boundary_id = ref["boundary_id"]
    if not isinstance(boundary_id, str) or not boundary_id:
        raise ManifestDerivationError(
            "invalid-plan", "assumption_ref boundary_id must be a non-empty string"
        )
    boundary = _single_match(
        state.boundary_contracts,
        lambda b: b.get("boundary_id"),
        boundary_id,
        kind="boundary contract",
    )
    tracking = ref["tracking_issue"]
    digest = ref["assumption_hash"]
    if not isinstance(tracking, str) or not tracking or not isinstance(digest, str):
        raise ManifestDerivationError(
            "invalid-plan",
            "assumption_ref tracking_issue and assumption_hash must be strings",
        )
    _require_match(
        _SHA256_RE, digest, "invalid-plan", "assumption_hash must be sha256:<64 hex>"
    )
    match = next(
        (
            a
            for a in boundary.get("assumptions") or ()
            if a.get("tracking_issue") == tracking
            and a.get("assumption_hash") == digest
        ),
        None,
    )
    if match is None:
        raise ManifestDerivationError(
            "dangling-reference",
            f"assumption_ref {boundary_id!r} resolves to a boundary contract but no assumption "
            f"in it matches tracking_issue={tracking!r} assumption_hash={digest!r}",
        )


# ---------------------------------------------------------------------------
# Derivation.
# ---------------------------------------------------------------------------


def _verifier_policy_for_cluster(
    descriptor: Mapping[str, Any], cluster: str
) -> dict[str, Any]:
    policy = descriptor.get("verifier_policy") or {}
    owning = (policy.get("clusters") or {}).get(cluster, policy.get("default"))
    supporting = tuple(policy.get("supporting") or ())
    return {"owning": owning, "supporting": supporting}


def _check_assurance_against_policy(
    assurance: Mapping[str, Any], policy: Mapping[str, Any], subject: str
) -> None:
    allowed_verifiers = {policy["owning"], *policy["supporting"]}
    allowed_kinds = {
        VERIFIER_EVIDENCE_KIND[v]
        for v in allowed_verifiers
        if v in VERIFIER_EVIDENCE_KIND
    }
    kinds = list(assurance.get("accepted_evidence_kinds") or ())
    unexpected = [k for k in kinds if k not in allowed_kinds]
    if unexpected:
        raise ManifestDerivationError(
            "contradictory-source",
            f"{subject} accepts evidence kind(s) {unexpected} that no verifier declared for "
            f"cluster {policy['owning']!r} (owning) or its supporting verifiers "
            f"{sorted(policy['supporting'])!r} can produce",
        )
    owning_kind = VERIFIER_EVIDENCE_KIND.get(str(policy["owning"]))
    if owning_kind and owning_kind not in kinds:
        raise ManifestDerivationError(
            "contradictory-source",
            f"{subject} does not accept the evidence kind of the cluster's declared owning "
            f"verifier {policy['owning']!r} ({owning_kind})",
        )


def _derive_functions(
    request: WorkPackageRequest, descriptor: Mapping[str, Any]
) -> list[str]:
    _require(
        request.functions,
        "missing-source",
        "plan declares no functions; a work package must own at least one",
    )
    _reject_duplicates(request.functions, "function")
    crate_names = {Path(crate["crate_dir"]).name for crate in descriptor["crates"]}
    functions = []
    for function in request.functions:
        _require_match(
            _FUNCTION_RE,
            function,
            "invalid-plan",
            f"function {function!r} is not a Rust path (module::Type::method)",
        )
        if function.split("::")[0] not in crate_names:
            raise ManifestDerivationError(
                "dangling-reference",
                f"function {function!r} is not owned by any crate the descriptor declares "
                f"({', '.join(sorted(crate_names))})",
            )
        functions.append(function)
    return sorted(functions)


def _derive_dependencies(
    request: WorkPackageRequest, closure_profile: Mapping[str, Any]
) -> list[str]:
    _reject_duplicates(request.depends_on, "dependency")
    known = set(closure_profile.get("work_packages") or ())
    for dependency in request.depends_on:
        _require_match(
            _WORK_PACKAGE_RE,
            dependency,
            "invalid-plan",
            f"dependency {dependency!r} is not a WP- identifier",
        )
        if dependency == request.work_package:
            raise ManifestDerivationError(
                "dangling-dependency",
                f"work package {request.work_package!r} depends on itself",
            )
        if dependency not in known:
            raise ManifestDerivationError(
                "dangling-dependency",
                f"dependency {dependency!r} resolves to no work package in cluster "
                f"{closure_profile.get('cluster')!r} (declared: {sorted(known)})",
            )
    return sorted(request.depends_on)


def _segment_language_is_subset(candidate: str, container: str) -> bool:
    """Prove inclusion for the validator's `*`/`?` segment grammar.

    This intentionally rejects a few valid but difficult-to-prove glob
    relationships rather than widening an agent's write authority.
    """
    if candidate == container:
        return True
    alphabet = sorted(
        {
            char
            for pattern in (candidate, container)
            for char in pattern
            if char not in "*?"
        }
    ) + ["\0"]

    def closure(pattern: str, positions: frozenset[int]) -> frozenset[int]:
        expanded = set(positions)
        pending = list(positions)
        while pending:
            position = pending.pop()
            if (
                position < len(pattern)
                and pattern[position] == "*"
                and position + 1 not in expanded
            ):
                expanded.add(position + 1)
                pending.append(position + 1)
        return frozenset(expanded)

    def advance(pattern: str, positions: frozenset[int], char: str) -> frozenset[int]:
        next_positions = set()
        for position in positions:
            if position == len(pattern):
                continue
            token = pattern[position]
            if token == "*":
                next_positions.add(position)
            elif token == "?" or token == char:
                next_positions.add(position + 1)
        return closure(pattern, frozenset(next_positions))

    start = (closure(candidate, frozenset({0})), closure(container, frozenset({0})))
    pending = [start]
    seen = {start}
    while pending:
        candidate_positions, container_positions = pending.pop()
        if (
            len(candidate) in candidate_positions
            and len(container) not in container_positions
        ):
            return False
        for char in alphabet:
            next_candidate = advance(candidate, candidate_positions, char)
            if not next_candidate:
                continue
            next_container = advance(container, container_positions, char)
            pair = (next_candidate, next_container)
            if pair not in seen:
                seen.add(pair)
                pending.append(pair)
    return True


def _pattern_is_subset(candidate: str, container: str) -> bool:
    candidate_parts = validate_work_package._segments(candidate)
    container_parts = validate_work_package._segments(container)
    candidate_index = 0
    container_index = 0
    while candidate_index < len(candidate_parts) and container_index < len(
        container_parts
    ):
        candidate_segment = candidate_parts[candidate_index]
        container_segment = container_parts[container_index]
        if container_segment == "**":
            return container_index == len(container_parts) - 1
        if candidate_segment == "**":
            if container_segment == "**":
                candidate_index += 1
                container_index += 1
                continue
            return False
        if not _segment_language_is_subset(candidate_segment, container_segment):
            return False
        candidate_index += 1
        container_index += 1
    if candidate_index == len(candidate_parts):
        return all(part == "**" for part in container_parts[container_index:])
    return False


def _derive_write_policy(
    request: WorkPackageRequest, descriptor: Mapping[str, Any]
) -> dict[str, Any]:
    for field_name, patterns in (
        ("allowed_write_set", request.allowed_write_set),
        ("protected_write_set", request.protected_write_set),
    ):
        _require(patterns, "missing-source", f"plan declares no {field_name}")
        _reject_duplicates(patterns, field_name)
        for pattern in patterns:
            if validate_work_package._pattern_escapes_workspace(pattern):
                raise ManifestDerivationError(
                    "invalid-plan",
                    f"{field_name} pattern {pattern!r} is not workspace-relative -- absolute "
                    "paths and .. segments are refused, not resolved",
                )
    allowed, protected = request.allowed_write_set, request.protected_write_set
    descriptor_write_set = descriptor.get("write_set")
    if not isinstance(descriptor_write_set, Mapping):
        raise ManifestDerivationError(
            "missing-source", "project descriptor has no validated write_set mapping"
        )
    descriptor_allowed = descriptor_write_set.get("allowed_roots")
    descriptor_protected = descriptor_write_set.get("protected_roots")
    if (
        not isinstance(descriptor_allowed, Sequence)
        or isinstance(descriptor_allowed, (str, bytes))
        or not descriptor_allowed
    ):
        raise ManifestDerivationError(
            "missing-source", "project descriptor declares no write_set.allowed_roots"
        )
    if not isinstance(descriptor_protected, Sequence) or isinstance(
        descriptor_protected, (str, bytes)
    ):
        raise ManifestDerivationError(
            "missing-source",
            "project descriptor has no write_set.protected_roots array",
        )
    for label, patterns in (
        ("allowed_roots", descriptor_allowed),
        ("protected_roots", descriptor_protected),
    ):
        if any(not isinstance(pattern, str) or not pattern for pattern in patterns):
            raise ManifestDerivationError(
                "invalid-authoritative-state",
                f"descriptor write_set.{label} must contain non-empty strings",
            )
        _reject_duplicates(patterns, f"descriptor write_set.{label}")
        if any(
            validate_work_package._pattern_escapes_workspace(pattern)
            for pattern in patterns
        ):
            raise ManifestDerivationError(
                "invalid-authoritative-state",
                f"descriptor write_set.{label} contains a workspace-escaping pattern",
            )
    for pattern in allowed:
        if not any(_pattern_is_subset(pattern, root) for root in descriptor_allowed):
            raise ManifestDerivationError(
                "invalid-plan",
                f"allowed_write_set pattern {pattern!r} widens descriptor allowed_roots; "
                "choose a pattern provably contained by one declared root",
            )
    for root in descriptor_protected:
        if not any(_pattern_is_subset(root, pattern) for pattern in protected):
            raise ManifestDerivationError(
                "invalid-plan",
                f"protected_write_set does not preserve descriptor protected_root {root!r}",
            )
    conflicts = [
        (a, p)
        for a in allowed
        for p in protected
        if validate_work_package._patterns_can_overlap(a, p)
    ]
    if conflicts:
        detail = "; ".join(f"{a!r} overlaps {p!r}" for a, p in conflicts)
        raise ManifestDerivationError(
            "self-referential-guarantee",
            f"allowed_write_set overlaps protected_write_set: {detail}",
        )
    _check_witness_renderer_coverage(allowed, protected)
    return {
        "allowed_write_set": sorted(allowed),
        "protected_write_set": sorted(protected),
        "on_conflict": ON_CONFLICT,
    }


def _check_witness_renderer_coverage(
    allowed: Sequence[str], protected: Sequence[str]
) -> None:
    for runner in FIXED_GATE_RUNNERS:
        if not any(
            validate_work_package._pattern_covers_path(p, runner) for p in protected
        ):
            raise ManifestDerivationError(
                "invalid-plan",
                f"gate-adjacent file {runner!r} is not covered by protected_write_set; the "
                "derived manifest could never pass G13",
            )
        overlapping = [
            p for p in allowed if validate_work_package._pattern_covers_path(p, runner)
        ]
        if overlapping:
            raise ManifestDerivationError(
                "invalid-plan",
                f"gate-adjacent file {runner!r} is covered by allowed_write_set "
                f"({', '.join(sorted(overlapping))}); gate code must never be writable",
            )


def _derive_gate_integrity(state: AuthoritativeState) -> list[dict[str, str]]:
    declared_hashes: dict[str, str] = {}
    descriptor_entries = state.descriptor.get("gate_integrity") or ()
    if isinstance(descriptor_entries, (str, bytes, Mapping)) or not isinstance(
        descriptor_entries, Sequence
    ):
        raise ManifestDerivationError(
            "invalid-authoritative-state", "descriptor gate_integrity must be an array"
        )
    descriptor_runners: set[str] = set()
    for entry in descriptor_entries:
        if not isinstance(entry, Mapping):
            raise ManifestDerivationError(
                "invalid-authoritative-state",
                "descriptor gate_integrity entries must be mappings",
            )
        runner = entry.get("path")
        if not isinstance(runner, str) or not runner:
            raise ManifestDerivationError(
                "invalid-authoritative-state",
                "descriptor gate_integrity entries need a string path",
            )
        if runner in descriptor_runners:
            raise ManifestDerivationError(
                "duplicate-identifier", f"duplicate gate_integrity runner: {runner}"
            )
        descriptor_runners.add(runner)
    if not isinstance(state.gate_hashes, Mapping):
        raise ManifestDerivationError(
            "invalid-authoritative-state", "gate_hashes must be a path-to-hash mapping"
        )
    undeclared_hashes = (
        set(state.gate_hashes) - descriptor_runners - set(FIXED_GATE_RUNNERS)
    )
    if undeclared_hashes:
        raise ManifestDerivationError(
            "invalid-authoritative-state",
            "gate hashes were supplied for paths absent from descriptor.gate_integrity and "
            f"the fixed renderer set: {', '.join(sorted(map(str, undeclared_hashes)))}",
        )
    for runner, digest in state.gate_hashes.items():
        if not isinstance(runner, str) or not isinstance(digest, str):
            raise ManifestDerivationError(
                "invalid-authoritative-state",
                "gate_hashes keys and values must be strings",
            )
        declared_hashes[runner] = digest
    runners = sorted(descriptor_runners | set(FIXED_GATE_RUNNERS))
    entries: list[dict[str, str]] = []
    for runner in runners:
        posix_runner = PurePosixPath(runner)
        if (
            validate_work_package._pattern_escapes_workspace(runner)
            or posix_runner.is_absolute()
            or "\\" in runner
            or any(part in ("", ".", "..") for part in runner.split("/"))
        ):
            raise ManifestDerivationError(
                "invalid-plan", f"gate runner {runner!r} is not workspace-relative"
            )
        declared = declared_hashes.get(runner)
        if state.workspace_root is not None:
            root = state.workspace_root.resolve()
            path = (root / runner).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                raise ManifestDerivationError(
                    "invalid-plan", f"gate runner {runner!r} escapes the workspace"
                ) from None
            if not path.is_file():
                raise ManifestDerivationError(
                    "missing-source", f"gate runner {runner!r} does not exist on disk"
                )
            actual = _sha256_file(path)
            if declared is not None and declared != actual:
                raise ManifestDerivationError(
                    "stale-integrity-hash",
                    f"gate_integrity hash for {runner!r} is stale: declared {declared}, actual {actual}",
                )
            digest = actual
        else:
            if declared is None:
                raise ManifestDerivationError(
                    "missing-source",
                    f"no declared gate hash for {runner!r} and no workspace_root to compute one",
                )
            digest = declared
        _require_match(
            _SHA256_RE,
            digest,
            "invalid-plan",
            f"gate hash for {runner!r} is not sha256:<64 hex>",
        )
        entries.append({"runner": runner, "hash": digest})
    return entries


def _derive_artifact_set_hash(
    state: AuthoritativeState, receipt: Mapping[str, Any]
) -> str:
    manifest = list(receipt.get("artifact_manifest") or ())
    if not manifest:
        raise ManifestDerivationError(
            "missing-source", "promotion receipt has an empty artifact_manifest"
        )
    if any(not isinstance(entry, Mapping) for entry in manifest):
        raise ManifestDerivationError(
            "invalid-authoritative-state",
            "promotion artifact_manifest entries must be mappings",
        )
    ordered = sorted(
        ({"path": e.get("path"), "hash": e.get("hash")} for e in manifest),
        key=lambda e: str(e["path"]),
    )
    paths: set[str] = set()
    resolved_paths: set[Path] = set()
    for entry in ordered:
        artifact_path = entry["path"]
        if (
            not isinstance(artifact_path, str)
            or not artifact_path
            or "\\" in artifact_path
        ):
            raise ManifestDerivationError(
                "invalid-authoritative-state",
                "promotion artifact path must be a normalized relative path",
            )
        pure_path = PurePosixPath(artifact_path)
        if pure_path.is_absolute() or any(
            part in ("", ".", "..") for part in artifact_path.split("/")
        ):
            raise ManifestDerivationError(
                "invalid-authoritative-state",
                f"promotion artifact path {artifact_path!r} escapes or is not normalized",
            )
        if artifact_path in paths:
            raise ManifestDerivationError(
                "duplicate-identifier",
                f"duplicate promotion artifact path: {artifact_path}",
            )
        paths.add(artifact_path)
        _require_match(
            _SHA256_RE,
            entry["hash"],
            "invalid-authoritative-state",
            f"promotion artifact {entry['path']!r} has a malformed hash",
        )
        if state.workspace_root is not None:
            root = state.workspace_root.resolve()
            path = (root / artifact_path).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                raise ManifestDerivationError(
                    "invalid-authoritative-state",
                    f"promotion artifact {artifact_path!r} resolves outside the workspace",
                ) from None
            if path in resolved_paths:
                raise ManifestDerivationError(
                    "duplicate-identifier",
                    f"promotion artifact paths resolve to the same file: {artifact_path!r}",
                )
            resolved_paths.add(path)
            if not path.is_file():
                raise ManifestDerivationError(
                    "missing-source",
                    f"promotion artifact {artifact_path!r} is not on disk",
                )
            actual = _sha256_file(path)
            if actual != entry["hash"]:
                raise ManifestDerivationError(
                    "stale-integrity-hash",
                    f"promotion artifact {artifact_path!r} changed since acceptance: receipt "
                    f"declares {entry['hash']}, actual {actual} -- acceptance is revoked",
                )
    return _sha256_bytes(_canonical(ordered).encode("utf-8"))


def _derive_provenance(
    state: AuthoritativeState, receipt: Mapping[str, Any]
) -> dict[str, Any]:
    _require_match(
        _BASE_COMMIT_RE,
        state.base_commit,
        "missing-source",
        "no base_commit supplied; the derivation will not invent a VCS revision",
    )
    promotion_id = _require(
        receipt.get("promotion_id"),
        "missing-source",
        f"promotion receipt for cluster {receipt.get('cluster')!r} declares no promotion_id",
    )
    _require_match(
        re.compile(r"^PROM-[A-Z][A-Z0-9-]*-[0-9]{3}$"),
        str(promotion_id),
        "invalid-plan",
        f"promotion_id {promotion_id!r} is malformed",
    )
    if not isinstance(state.toolchain, str) or not state.toolchain:
        raise ManifestDerivationError(
            "missing-source", "no toolchain supplied for provenance"
        )
    if not isinstance(state.target, str) or not state.target:
        raise ManifestDerivationError(
            "missing-source", "no target supplied for provenance"
        )
    _reject_duplicates(state.features, "feature")
    for feature in state.features:
        if not isinstance(feature, str) or not feature:
            raise ManifestDerivationError(
                "invalid-authoritative-state", "feature names must be non-empty strings"
            )
    return {
        "base_commit": state.base_commit,
        "promotion_id": promotion_id,
        "artifact_set_hash": _derive_artifact_set_hash(state, receipt),
        "toolchain": state.toolchain,
        "target": state.target,
        "features": sorted(state.features),
    }


def _derive_provided_guarantees(
    state: AuthoritativeState, request: WorkPackageRequest, policy: Mapping[str, Any]
) -> list[dict[str, Any]]:
    _reject_duplicates(request.provided_obligations, "provided obligation")
    unused_harnesses = sorted(
        set(request.harnesses) - set(request.provided_obligations)
    )
    if unused_harnesses:
        raise ManifestDerivationError(
            "unsupported-field",
            f"harnesses name obligations not provided by this package: {', '.join(unused_harnesses)}",
        )
    provided = []
    for obligation_id in sorted(request.provided_obligations):
        _require_match(
            _OBLIGATION_RE,
            obligation_id,
            "invalid-plan",
            f"provided obligation {obligation_id!r} is not <Concept>.<id>",
        )
        harness = request.harnesses.get(obligation_id)
        if harness is None:
            raise ManifestDerivationError(
                "missing-source",
                f"provided obligation {obligation_id!r} has no exact harness name in the plan",
            )
        _require_match(
            _HARNESS_RE,
            harness,
            "invalid-plan",
            f"harness {harness!r} for {obligation_id!r} must be an exact Rust identifier, no wildcards",
        )
        assurance = _resolve_obligation_assurance(state, obligation_id)
        _check_assurance_against_policy(
            assurance, policy, f"provided guarantee {obligation_id!r}"
        )
        provided.append(
            {
                "obligation_id": obligation_id,
                "required_assurance": assurance,
                "harness": harness,
            }
        )
    return provided


def _derive_required_guarantees(
    state: AuthoritativeState, request: WorkPackageRequest, policy: Mapping[str, Any]
) -> list[dict[str, Any]]:
    _reject_duplicates(request.required_obligations, "required obligation")
    required = []
    for obligation_id in sorted(request.required_obligations):
        _require_match(
            _OBLIGATION_RE,
            obligation_id,
            "invalid-plan",
            f"required obligation {obligation_id!r} is not <Concept>.<id>",
        )
        assurance = _resolve_obligation_assurance(state, obligation_id)
        _check_assurance_against_policy(
            assurance, policy, f"required guarantee {obligation_id!r}"
        )
        required.append(
            {
                "obligation_id": obligation_id,
                "assume_during_check": True,
                "required_assurance": assurance,
            }
        )
    return required


def _derive_preconditions(
    state: AuthoritativeState, request: WorkPackageRequest, policy: Mapping[str, Any]
) -> list[dict[str, Any]]:
    _reject_duplicates(request.bridges, "bridge")
    preconditions = []
    for bridge_id in sorted(request.bridges):
        _require_match(
            _BRIDGE_RE,
            bridge_id,
            "invalid-plan",
            f"bridge {bridge_id!r} is not a BR- identifier",
        )
        bridge = _resolve_bridge_spec(state, bridge_id)
        assurance = _resolve_bridge_assurance(state, bridge)
        _check_assurance_against_policy(assurance, policy, f"bridge {bridge_id!r}")
        preconditions.append(
            {
                "bridge_id": bridge_id,
                "required_assurance": assurance,
                "callsite_requirement": CALLSITE_REQUIREMENT,
            }
        )
    return preconditions


def _derive_assumptions(
    state: AuthoritativeState, request: WorkPackageRequest
) -> list[dict[str, Any]]:
    normalized: list[tuple[str, Mapping[str, Any]]] = []
    for entry in request.assumptions:
        if not isinstance(entry, Mapping):
            raise ManifestDerivationError(
                "invalid-plan", f"assumption entry must be a mapping, got {entry!r}"
            )
        if set(entry) != _ASSUMPTION_ENTRY_FIELDS:
            unsupported = sorted(set(entry) - _ASSUMPTION_ENTRY_FIELDS)
            missing = sorted(_ASSUMPTION_ENTRY_FIELDS - set(entry))
            detail = []
            if missing:
                detail.append(f"missing {', '.join(missing)}")
            if unsupported:
                detail.append(f"unsupported {', '.join(unsupported)}")
            raise ManifestDerivationError(
                "invalid-plan",
                "assumption entry must contain exactly assumption_ref, risk, "
                "and mitigations (" + "; ".join(detail) + ")",
            )
        ref = entry["assumption_ref"]
        if not isinstance(ref, Mapping):
            raise ManifestDerivationError(
                "invalid-plan", "assumption entry has no assumption_ref mapping"
            )
        normalized.append((_assumption_ref_key(ref), entry))
    _reject_duplicates([key for key, _ in normalized], "assumption_ref")
    assumptions = []
    for key, entry in sorted(normalized, key=lambda pair: pair[0]):
        ref = entry.get("assumption_ref")
        _validate_assumption_ref(state, ref)
        risk = entry.get("risk")
        if risk not in RISK_LEVELS:
            raise ManifestDerivationError(
                "invalid-plan",
                f"assumption {_assumption_ref_key(ref)!r} has risk {risk!r}; "
                f"permitted: {', '.join(RISK_LEVELS)}",
            )
        raw_mitigations = entry.get("mitigations")
        if isinstance(raw_mitigations, (str, bytes, Mapping)) or not isinstance(
            raw_mitigations, Sequence
        ):
            raise ManifestDerivationError(
                "invalid-plan", f"assumption {key!r} mitigations must be an array"
            )
        mitigations = []
        for mitigation in raw_mitigations:
            if (
                not isinstance(mitigation, Mapping)
                or set(mitigation) != _MITIGATION_FIELDS
            ):
                raise ManifestDerivationError(
                    "invalid-plan",
                    f"assumption {key!r} mitigation must contain exactly kind and reference",
                )
            if not isinstance(mitigation["kind"], str) or not isinstance(
                mitigation["reference"], str
            ):
                raise ManifestDerivationError(
                    "invalid-plan",
                    f"assumption {key!r} mitigation kind and reference must be strings",
                )
            if not mitigation["reference"]:
                raise ManifestDerivationError(
                    "invalid-plan",
                    f"assumption {key!r} mitigation reference must be non-empty",
                )
            mitigations.append(dict(mitigation))
        if not mitigations:
            raise ManifestDerivationError(
                "missing-source",
                f"assumption {_assumption_ref_key(ref)!r} declares no mitigations",
            )
        for mitigation in mitigations:
            if mitigation.get("kind") not in MITIGATION_KINDS:
                raise ManifestDerivationError(
                    "invalid-plan",
                    f"assumption {_assumption_ref_key(ref)!r} has mitigation kind "
                    f"{mitigation.get('kind')!r}; permitted: {', '.join(MITIGATION_KINDS)}",
                )
        mitigations.sort(key=lambda item: (item["kind"], item["reference"]))
        assumptions.append(
            {"assumption_ref": dict(ref), "risk": risk, "mitigations": mitigations}
        )
    return assumptions


def _derive_gates(request: WorkPackageRequest) -> list[dict[str, Any]]:
    _require(request.gates, "missing-source", "plan declares no gates")
    _reject_duplicates(request.gates, "gate id", key=lambda g: g.id)
    gates = []
    for gate in request.gates:
        _require_match(
            re.compile(r"^[a-z][a-z0-9_-]*$"),
            gate.id,
            "invalid-plan",
            f"gate id {gate.id!r} must match ^[a-z][a-z0-9_-]*$",
        )
        _require(gate.runner, "invalid-plan", f"gate {gate.id!r} has an empty runner")
        gates.append({"id": gate.id, "runner": gate.runner, "args": list(gate.args)})
    return sorted(gates, key=lambda g: g["id"])


def _derive_obligations_index(request: WorkPackageRequest) -> list[str]:
    return sorted(set(request.provided_obligations) | set(request.bridges))


def derive_manifest(
    request: WorkPackageRequest, state: AuthoritativeState
) -> dict[str, Any]:
    """Assemble the canonical manifest document from the plan and the
    authoritative state. Pure: no filesystem writes, no clock, no ambient
    state. Raises :class:`ManifestDerivationError` on any refusal."""
    _validate_request_shape(request)
    _validate_authoritative_shape(state)
    _require_match(
        _WORK_PACKAGE_RE,
        request.work_package,
        "invalid-plan",
        f"work_package {request.work_package!r} must match WP-[A-Z][A-Z0-9-]*",
    )
    _require_match(
        _ISSUE_RE,
        request.issue,
        "invalid-plan",
        f"issue {request.issue!r} must be chainlink:<n>",
    )
    _require_match(
        _CLUSTER_RE,
        request.cluster,
        "invalid-plan",
        f"cluster {request.cluster!r} is not a cluster name",
    )
    closure_profile = _resolve_closure_profile(state, request.cluster)
    receipt = _resolve_receipt(state, request.cluster)
    known_packages = list(closure_profile.get("work_packages") or ())
    if request.work_package not in known_packages:
        raise ManifestDerivationError(
            "dangling-reference",
            f"work package {request.work_package!r} is not listed in cluster "
            f"{request.cluster!r}'s closure profile ({sorted(known_packages)})",
        )
    policy = _verifier_policy_for_cluster(state.descriptor, request.cluster)
    _require(
        policy["owning"],
        "missing-source",
        "descriptor declares no verifier for this cluster",
    )
    closure_owner = (closure_profile.get("conditions") or {}).get("owning_verifier")
    if closure_owner is not None and closure_owner != policy["owning"]:
        raise ManifestDerivationError(
            "contradictory-source",
            f"closure profile declares owning_verifier {closure_owner!r} but the descriptor's "
            f"verifier_policy assigns {policy['owning']!r}",
        )
    _validate_interaction_config_scope(state, request)

    provided = _derive_provided_guarantees(state, request, policy)
    required = _derive_required_guarantees(state, request, policy)
    provided_ids = {entry["obligation_id"] for entry in provided}
    required_ids = {entry["obligation_id"] for entry in required}
    overlap = sorted(provided_ids & required_ids)
    if overlap:
        raise ManifestDerivationError(
            "self-referential-guarantee",
            f"work package {request.work_package!r} both provides and requires {overlap}; a "
            "package cannot be its own supplier",
        )

    manifest = {
        "schema": SCHEMA_ID,
        "work_package": request.work_package,
        "issue": request.issue,
        "depends_on": _derive_dependencies(request, closure_profile),
        "coupling_notes": sorted(set(request.coupling_notes)),
        "scheduling_rule": request.scheduling_rule or DEFAULT_SCHEDULING_RULE,
        "functions": _derive_functions(request, state.descriptor),
        "obligations": _derive_obligations_index(request),
        "provenance": _derive_provenance(state, receipt),
        "gate_integrity": _derive_gate_integrity(state),
        "write_policy": _derive_write_policy(request, state.descriptor),
        "definition_of_done": {
            "provided_guarantees": provided,
            "required_preconditions_to_establish": _derive_preconditions(
                state, request, policy
            ),
            "required_guarantees": required,
            "trusted_assumptions": _derive_assumptions(state, request),
        },
        "gates": _derive_gates(request),
        "failure_policy": json.loads(json.dumps(FAILURE_POLICY)),
        "report": {
            "emit": f"{REPORT_DIR}/{request.work_package}.json",
            "per_obligation_assurance_record": True,
        },
    }
    if manifest["scheduling_rule"] not in SCHEDULING_RULES:
        raise ManifestDerivationError(
            "invalid-plan",
            f"scheduling_rule {manifest['scheduling_rule']!r} is not one of {list(SCHEDULING_RULES)}",
        )
    _assert_schema_valid(manifest)
    return manifest


def render_manifest(manifest: Mapping[str, Any]) -> bytes:
    """The canonical serialized bytes: sorted keys, two-space indent, one
    trailing newline, UTF-8. Two equal documents always render byte-identically."""
    return (
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def derive_and_render(request: WorkPackageRequest, state: AuthoritativeState) -> bytes:
    return render_manifest(derive_manifest(request, state))


def validate_derived_manifest(
    manifest: Mapping[str, Any],
    workspace_root: Path,
    specs_search_root: Path | None = None,
    allowed_boundary_dirs: list[Path] | None = None,
) -> list[Any]:
    """Run the repository's own work-package validator over a derived
    manifest, so a consumer can prove the output passes G1a and §10.1
    rather than taking this module's word for it."""
    path = Path(f"{REPORT_DIR}/{manifest.get('work_package', 'WP-UNKNOWN')}.json")
    return validate_work_package.validate_data(
        path,
        dict(manifest),
        validate_work_package.load_validator(),
        workspace_root,
        specs_search_root if specs_search_root is not None else workspace_root,
        allowed_boundary_dirs,
    )


def _assert_schema_valid(manifest: Mapping[str, Any]) -> None:
    errors = validate_work_package.gate_g1a(
        Path("manifest.json"), dict(manifest), validate_work_package.load_validator()
    )
    if errors:
        raise ManifestDerivationError(
            "invalid-derived-manifest",
            "the derived document failed work-package-manifest/1.0: "
            + "; ".join(f.reason for f in errors),
        )
