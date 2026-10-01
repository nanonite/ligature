#!/usr/bin/env python3
"""Shared degradation-record review-provenance and human-ruling facts
(chainlink #96, the first of three children of #94).

#94's pilot finding: `gate-g14`'s release route ("released under an
accepted degradation record") consults neither the approval audit log
nor a human-ruling log before releasing a cluster -- a degradation
record whose `review` block names a reviewer who never reviewed it
releases the cluster exactly as a genuinely reviewed one would.
`accept-promotion` already closes this exact gap for promotion receipts
(chainlink #82): a `review` block is self-asserted data, indistinguishable
on disk from one a human actually wrote, so it must be checked against
the sanctioned approve path's own audit trail
(review_checkpoint.review_provenance_gaps) -- and even a provenanced
block does not by itself show a human ruled on the artifact *as it now
reads*, which is what the separate, hash-pinned human-ruling log
(generate_promotion_receipt.ruling_gaps/record_ruling) is for.

This module extracts that same pair of checks for a degradation record
rather than a promotion receipt's artifact_manifest, reusing both
modules' real logic instead of re-deriving it:

  - degradation_review_provenance_gaps() wraps review_checkpoint.
    review_provenance_gaps() around the one review block a degradation
    record carries.
  - degradation_ruling_gaps() wraps generate_promotion_receipt.
    compute_artifact_manifest() (to get the record's own real byte
    hash -- never a caller-supplied guess) and generate_promotion_
    receipt.ruling_gaps() (to check that hash against a `ratified`
    human ruling) around the record's own path.
  - degradation_record_gaps() runs both and returns the combined list,
    the one entry point a gate is expected to call.

No import cycle: this module depends on review_checkpoint and
generate_promotion_receipt, neither of which imports validate_closure
or gate_g14 (confirmed by inspection -- generate_promotion_receipt's own
imports are project_descriptor, review_checkpoint, ligature_install and
the validate_* artifact-kind modules, none of which import closure or
gate code). scripts/gate_g14.py importing THIS module (chainlink #97,
not done here) therefore adds one more edge to an already-acyclic graph,
not a cycle.

This module deliberately does not decide what a degradation record's
`failed_conditions`/`ceiling`/`affected_edges` mean, does not recompute
the closure, and does not wire itself into any gate -- #96's own scope
is the shared facts only ("review.provenance must match the accepted
review log, and the record must have a hash-pinned ratified human
ruling over its exact artifact bytes"); #97 wires `gate-g14` to require
them before a degradation record may release a cluster."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate_promotion_receipt import PromotionReceiptError  # noqa: E402
from generate_promotion_receipt import compute_artifact_manifest  # noqa: E402
from generate_promotion_receipt import ruling_gaps as _ruling_gaps  # noqa: E402
from review_checkpoint import review_provenance_gaps as _review_provenance_gaps  # noqa: E402


def _relative_to_workspace(workspace_root: Path, record_path: Path) -> str:
    """Normalize `record_path` (absolute, as validate_closure.py's own
    load_cluster_artifacts() returns it, or already workspace-relative)
    to the plain workspace-relative string compute_artifact_manifest()
    and review_provenance_gaps() both expect. A relative path is
    resolved against `workspace_root`, never the process cwd -- the
    same cwd-relative-default lesson #45/#82 already taught
    REVIEW_LOG_DEFAULT and accept_promotion()'s own ruling_log."""
    workspace_resolved = workspace_root.resolve()
    resolved = record_path if record_path.is_absolute() else (workspace_root / record_path)
    resolved = resolved.resolve()
    return str(resolved.relative_to(workspace_resolved))


def degradation_review_provenance_gaps(
    workspace_root: Path,
    record_path: Path,
    record: dict,
    review_log: Path,
) -> list[str]:
    """Prove the degradation record's own `review` block was produced by
    review_checkpoint.py's sanctioned approve path, not hand-written
    (chainlink #82's first remedy, applied to a degradation record
    instead of a promotion receipt's artifact set).

    `record` must carry a `review` block (docs/degradation-record-schema.json
    requires one on anything but a draft, so a genuinely G1a-valid
    accepted record always does) -- one that is missing or malformed is
    reported as a gap here rather than raising, since "no review to
    provenance-check" is exactly the shape #94's reproduction describes
    (nothing stops a hand-written record from omitting what it pleases)."""
    relative_path = _relative_to_workspace(workspace_root, record_path)
    review = record.get("review")
    if not isinstance(review, dict) or not review.get("reviewer") or not review.get("reviewed_at"):
        return [
            f"{relative_path}: degradation record carries no usable review block "
            "(reviewer/reviewed_at) to provenance-check -- a record must be reviewed "
            "before it can excuse anything"
        ]
    return _review_provenance_gaps(workspace_root, {relative_path: review}, review_log)


def degradation_ruling_gaps(
    workspace_root: Path,
    record_path: Path,
    ruling_log: Path,
    descriptor: dict | None = None,
) -> list[str]:
    """Prove a human has ruled `ratified` over this exact version of the
    degradation record's own bytes (chainlink #82's second remedy,
    applied to the record itself rather than a promotion's
    artifact_manifest).

    The hash compared is compute_artifact_manifest()'s -- the same
    routine record_ruling()/accept_promotion() use -- so ratifying a
    record and then editing so much as one byte of it (the ceiling, the
    tracking issue, a reviewer typo) makes the ruling stop covering it
    until re-ruled, exactly as #82 already guarantees for a promotion's
    artifact set. `descriptor` is accepted (and passed straight through)
    only for interface symmetry with compute_artifact_manifest()'s own
    witness-aware hashing; a degradation record is never a canonical
    witness spec, so it has no effect here in practice."""
    relative_path = _relative_to_workspace(workspace_root, record_path)
    try:
        manifest = compute_artifact_manifest(workspace_root, [relative_path], descriptor)
    except PromotionReceiptError as e:
        return [f"{relative_path}: could not compute the artifact manifest to check its human ruling: {e}"]
    return _ruling_gaps(workspace_root, manifest, ruling_log)


def degradation_record_gaps(
    workspace_root: Path,
    record_path: Path,
    record: dict,
    review_log: Path,
    ruling_log: Path,
    descriptor: dict | None = None,
) -> list[str]:
    """Both checks together -- the one entry point a gate needing "is
    this degradation record's acceptance fully provenanced and ruled on"
    is expected to call. An empty list means the record's review block
    is provenanced AND a ratified human ruling covers its exact current
    bytes; any gap in either check is returned, so a caller can refuse
    on the full set of reasons at once rather than one at a time."""
    gaps = degradation_review_provenance_gaps(workspace_root, record_path, record, review_log)
    gaps.extend(degradation_ruling_gaps(workspace_root, record_path, ruling_log, descriptor))
    return gaps
