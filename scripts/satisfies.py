#!/usr/bin/env python3
"""The satisfies() mechanism (plan.md §8.5 step 4, chainlink #23).

    satisfies(required_profile, achieved_record, context)

plan.md's own framing: "satisfies() mechanism is ordinary engineering.
But WHICH achieved assurance is sufficient for WHICH obligation ... is an
irreducible risk decision. Profile authorship is versioned, owned human
governance." That governance is not new machinery this module builds --
`required_profile` (a `required_assurance` block) already only exists on
a normative file that goes through the existing draft/approve human
checkpoint (docs/interaction-schema.json's reliances[], or a work-package
manifest's definition_of_done[], both already reviewed artifacts;
plan.md §7.2 also lists "assurance-profile authorship" directly in its
human-sign-off list). This module is the "ordinary engineering" half
only: a deterministic comparison between an already-authored
required_profile and a machine-recorded achieved_record
(docs/achieved-assurance-schema.json, chainlink #23's other half).

Two schemas in this codebase already define required_assurance
independently and non-identically: docs/interaction-schema.json's
version requires minimum_scope/trust_policy; docs/work-package-manifest-
schema.json's version (the one G14/§8.5 actually calls satisfies()
against, per its own required_assurance worked examples) makes both
optional. Pre-existing, not this issue's to reconcile. satisfies()
therefore does not validate `required_profile` against either schema
internally -- that is the caller's job, against whichever schema
governs wherever the dict came from -- and instead treats every
required_profile key defensively: required_claims/accepted_evidence_kinds
absent or empty means nothing can ever match (fails closed, not a
crash); minimum_scope absent means no scope constraint was declared
(vacuously satisfied, since there is nothing to violate); trust_policy
absent means NO assumption is allowed (fails closed the other
direction -- an allow-list's absence is not "anything goes").

`achieved_record` IS validated internally against
docs/achieved-assurance-schema.json, since this module owns that schema
and there is exactly one canonical shape for it (no divergence to
reconcile) -- the same "must be genuinely valid, not just present" bar
every cross-reference in this pipeline applies (see e.g.
validate_boundary_contracts.py's load_boundaries_by_id). A schema-invalid
achieved_record can never satisfy anything, regardless of what its
individual fields happen to contain.

`context` is accepted (the full satisfies(required_profile,
achieved_record, context) signature plan.md §8.5 names) but unused here
by design: applying CG1/CG3/CG4-style verifier-ceiling policy across a
whole cluster is closure-profile territory (plan.md §4, G14 itself,
chainlink #25) -- a broader, later mechanism this module does not
attempt to anticipate, the same boundary chainlink #22 drew around
harness generation. Once #25 defines what belongs in `context`, callers
pass it through unchanged; this function's signature already matches."""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from schema_utils import make_validator  # noqa: E402

ACHIEVED_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "docs" / "achieved-assurance-schema.json"


def load_achieved_schema() -> dict:
    return json.loads(ACHIEVED_SCHEMA_PATH.read_text())


def load_achieved_validator():
    return make_validator(load_achieved_schema())


@dataclass
class SatisfiesResult:
    holds: bool
    reasons: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.holds


def satisfies(required_profile: dict, achieved_record: dict, context: dict | None = None) -> SatisfiesResult:
    """context is accepted for signature parity with plan.md §8.5 step 4
    but not yet used -- see this module's own docstring for why."""
    del context

    achieved_errors = list(load_achieved_validator().iter_errors(achieved_record))
    if achieved_errors:
        return SatisfiesResult(
            holds=False,
            reasons=[f"achieved_record is not schema-valid: {e.message}" for e in achieved_errors],
        )

    reasons: list[str] = []

    support_status = achieved_record["support"]["status"]
    if support_status != "supported":
        reasons.append(f"support.status is {support_status!r}, not 'supported'")

    claim = achieved_record["claim"]
    if claim["result"] != "pass":
        reasons.append(f"claim.result is {claim['result']!r}, not 'pass'")
    required_claims = required_profile.get("required_claims") or []
    if claim["kind"] not in required_claims:
        reasons.append(
            f"claim.kind {claim['kind']!r} is not among required_profile.required_claims {required_claims!r}"
        )

    evidence = achieved_record["evidence"]
    accepted_evidence_kinds = required_profile.get("accepted_evidence_kinds") or []
    if evidence["kind"] not in accepted_evidence_kinds:
        reasons.append(
            f"evidence.kind {evidence['kind']!r} is not among "
            f"required_profile.accepted_evidence_kinds {accepted_evidence_kinds!r}"
        )

    minimum_scope = required_profile.get("minimum_scope") or {}
    achieved_scope = evidence["scope"]
    for key, required_value in minimum_scope.items():
        achieved_value = achieved_scope.get(key)
        if achieved_value != required_value:
            reasons.append(
                f"minimum_scope[{key!r}] requires {required_value!r}, "
                f"achieved evidence.scope has {achieved_value!r}"
            )

    trust_policy = required_profile.get("trust_policy")
    assumptions_allowed = set((trust_policy or {}).get("assumptions_allowed") or [])
    for assumption in achieved_record["trust"]["assumptions"]:
        if assumption not in assumptions_allowed:
            reasons.append(
                f"trust.assumptions includes {assumption!r}, not in "
                f"required_profile.trust_policy.assumptions_allowed {sorted(assumptions_allowed)!r}"
                + ("" if trust_policy is not None else " (required_profile declares no trust_policy at all)")
            )

    return SatisfiesResult(holds=not reasons, reasons=reasons)
