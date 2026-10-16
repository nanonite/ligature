"""Shared interpretation of the descriptor's verifier ownership policy.

The descriptor chooses one owner for each cluster and may name supporting
verifiers. Both Stage 3 interaction validation and Stage 7 assurance
derivation use the same evidence-kind mapping and compatibility rule.
"""
from __future__ import annotations

from collections.abc import Mapping


VERIFIER_EVIDENCE_KIND = {
    "kani": "kani-bounded-model-check",
    "creusot": "creusot-deductive-check",
    "verus": "verus-deductive-check",
}


def policy_for_cluster(descriptor: Mapping, cluster: str) -> dict:
    policy = descriptor.get("verifier_policy") or {}
    owning = (policy.get("clusters") or {}).get(cluster, policy.get("default"))
    supporting = tuple(policy.get("supporting") or ())
    return {"owning": owning, "supporting": supporting}


def assurance_policy_mismatches(assurance: Mapping, policy: Mapping) -> tuple[list[str], bool]:
    """Return unsupported evidence kinds and whether owner evidence is absent.

    A supporting verifier may add an evidence kind, but cannot replace the
    cluster's declared owner. This is the same rule Stage 7 applies when it
    derives a manifest's required assurance.
    """
    allowed_verifiers = {policy.get("owning"), *(policy.get("supporting") or ())}
    allowed_kinds = {
        VERIFIER_EVIDENCE_KIND[verifier]
        for verifier in allowed_verifiers
        if verifier in VERIFIER_EVIDENCE_KIND
    }
    kinds = list(assurance.get("accepted_evidence_kinds") or ())
    unexpected = sorted({kind for kind in kinds if kind not in allowed_kinds})
    owning_kind = VERIFIER_EVIDENCE_KIND.get(str(policy.get("owning")))
    owner_missing = owning_kind is not None and owning_kind not in kinds
    return unexpected, owner_missing


def verifier_is_declared(verifier: str, policy: Mapping) -> bool:
    return verifier in {policy.get("owning"), *(policy.get("supporting") or ())}
