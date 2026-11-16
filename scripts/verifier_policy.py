"""Shared interpretation of the descriptor's verifier ownership policy.

The descriptor chooses one owner for each cluster and may name supporting
verifiers. Both Stage 3 interaction validation and Stage 7 assurance
derivation use the same evidence-kind mapping and compatibility rule.

chainlink #171 adds one orthogonal axis: a project may declare external
evidence tools in the descriptor's ``external_evidence[]`` list. Those kinds
are additional evidence a reliance may intentionally accept, but they are not
verifier systems and never replace the owner's evidence when a deductive
claim is required.
"""
from __future__ import annotations

from collections.abc import Mapping

from external_evidence import DEDUCTIVE_CLAIM_KINDS, external_kinds

VERIFIER_EVIDENCE_KIND = {
    "kani": "kani-bounded-model-check",
    "creusot": "creusot-deductive-check",
    "verus": "verus-deductive-check",
}


def policy_for_cluster(descriptor: Mapping, cluster: str) -> dict:
    policy = descriptor.get("verifier_policy") or {}
    owning = (policy.get("clusters") or {}).get(cluster, policy.get("default"))
    supporting = tuple(policy.get("supporting") or ())
    return {
        "owning": owning,
        "supporting": supporting,
        # Declared external kinds are project-wide, not cluster-scoped: a
        # tool that establishes a timing property may support any cluster
        # that explicitly names its kind in a reliance.
        "external_kinds": tuple(sorted(external_kinds(descriptor))),
    }


def assurance_policy_mismatches(assurance: Mapping, policy: Mapping) -> tuple[list[str], bool]:
    """Return unsupported evidence kinds and whether required owner evidence is absent.

    A supporting verifier may add an evidence kind, but cannot replace the
    cluster's declared owner. This is the same rule Stage 7 applies when it
    derives a manifest's required assurance.

    A project-declared external evidence kind (chainlink #171) is allowed
    alongside the owner's kinds. It is not, however, a substitute for the
    owner when the requirement asks for a deductive claim: the owner-kind
    presence check therefore applies only to requirements whose claim
    vocabulary is deductive. A purely non-functional requirement (for
    example timing-independence) may intentionally accept only its external
    kind.
    """
    allowed_verifiers = {policy.get("owning"), *(policy.get("supporting") or ())}
    allowed_kinds = {
        VERIFIER_EVIDENCE_KIND[verifier]
        for verifier in allowed_verifiers
        if verifier in VERIFIER_EVIDENCE_KIND
    }
    allowed_kinds.update(policy.get("external_kinds") or ())
    kinds = list(assurance.get("accepted_evidence_kinds") or ())
    unexpected = sorted({kind for kind in kinds if kind not in allowed_kinds})
    owning_kind = VERIFIER_EVIDENCE_KIND.get(str(policy.get("owning")))
    required_claims = list(assurance.get("required_claims") or ())
    claims_require_owner = (
        not required_claims
        or any(claim in DEDUCTIVE_CLAIM_KINDS for claim in required_claims)
    )
    owner_missing = (
        claims_require_owner
        and owning_kind is not None
        and owning_kind not in kinds
    )
    return unexpected, owner_missing


def verifier_is_declared(verifier: str, policy: Mapping) -> bool:
    return verifier in {policy.get("owning"), *(policy.get("supporting") or ())}
