#!/usr/bin/env python3
"""Human review/approval checkpoint (plan.md §7.2, chainlink #39).

Every normative artifact in this pipeline already carries a required
`review: {reviewer, reviewed_at}` block (project descriptor, boundary
contracts, and every schema after it). This mechanism uses that block as
the pending/approved signal instead of inventing separate state:

  - A draft artifact (LLM-backend output, or a human edit) is staged next
    to its target path as `<target>.draft.json`.
  - classify() decides whether the draft can be auto-promoted (the §7.2
    "versioned policy" carve-out: mechanical, no semantic text change, no
    assurance-target decrease, no new assumption, all hashes still bound)
    or needs a human checkpoint. First-time creation is never mechanical --
    there is nothing to diff a first version against.
  - approve() writes one target, while approve_pair() writes a mutually
    dependent pair as one transaction. Both fill in
    review.reviewer/review.reviewed_at, remove their drafts, and append audit
    entries to ci/results/review_log.jsonl -- append-only, so a human's
    approval is traceable even if an artifact changes again later.
  - All three promotion paths (approve(), approve_pair(),
    auto_promote_if_mechanical()) open with the same fail-closed preflight,
    require_staged_drafts() (chainlink #102). Every promotion reads
    `<target>.draft`, and reading it unconditionally meant a missing draft
    -- a typo'd target, a target whose draft the promotion that created it
    already consumed, or a record hand-written to its target path -- came
    out as a raw FileNotFoundError traceback at exit 1, naming a file the
    caller was never told about. One shared guard, one wording, and no
    per-command copy to drift: the same shape `promote_evidence` already
    answered and chainlink #83 established for manifests, receipts and
    descriptors.
  - review_provenance_gaps() is this module's READER for that log. A
    `review` block is pure data -- a hand-written one is byte-identical
    to one approve() wrote -- so a gate that trusts the block trusts
    whoever typed it. Reading the log back is what lets `accept-promotion`
    prove an accepted artifact's review block came from one of the
    writers above before minting a Stage 4.5 receipt over it
    (chainlink #82).
  - approve() runs mechanical validation (a caller-supplied validate_fn,
    e.g. validate_boundary_contracts.validate_data) against the draft
    *before* writing anything, and refuses to promote on any error-severity
    finding (review finding D1: an earlier version wrote straight through
    with no gate at all -- a human could approve a schema-invalid or
    role-unsafe draft straight into the tree, which breaks the plan's own
    ordering, §4.5: mechanically-valid -> semantically-reviewed -> accepted).
    validate_fn is generic on purpose -- this module has no business
    knowing boundary-contract-specific gate logic; the caller wires in
    whichever validator matches the artifact type being approved. It is
    NOT optional: there is no default, only a real validator or the
    explicit SKIP_VALIDATION sentinel (a second review pass, 2026-08-26,
    found the original None default silently meant "skip" -- see
    SKIP_VALIDATION's own docstring). A third pass (2026-08-27) further
    found that pipeline.py's own dispatcher was defaulting to
    SKIP_VALIDATION for any artifact type it didn't recognize, which was
    the same bypass one level up -- fixed there by making the dispatcher
    raise on an unrecognized target instead.

An LLM backend may recommend approval (plan.md §7.2: "An LLM critic may
recommend; it is never the sole authority") but nothing in this module
lets that recommendation substitute for the reviewer/reviewed_at values
approve() requires as real arguments -- there is no code path that fills
them in from anything other than an explicit call.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

Classification = Literal["new", "mechanical", "semantic"]

REVIEW_LOG_DEFAULT = Path("ci/results/review_log.jsonl")

# Any object with .severity ("error" | "info") and a __str__ -- deliberately
# not validate_boundary_contracts.Finding, to keep this module decoupled
# from any one artifact type's gate logic.
ValidateFn = Callable[[Path, dict], list]
ValidatePairFn = Callable[[dict[Path, dict]], dict[Path, list]]


class _Sentinel:
    def __init__(self, label: str):
        self._label = label

    def __repr__(self) -> str:
        return self._label


# validate_fn used to default to None, which silently meant "skip
# validation" -- any caller that forgot to wire one in (including the
# standalone CLI below) approved drafts with no gate at all (external
# review finding, high severity: "approval fails open"). There is no
# default now: a caller must pass either a real validator or this sentinel,
# explicitly, to skip validation on purpose. Forgetting is now a TypeError,
# not a silent bypass.
SKIP_VALIDATION = _Sentinel("SKIP_VALIDATION")
_REQUIRED = _Sentinel("_REQUIRED (internal -- means 'not supplied')")


class ApprovalRefused(Exception):
    """Raised by approve() when validate_fn reports an error-severity
    finding against the draft -- nothing was written."""


class DraftNotStaged(Exception):
    """Raised when a promotion is asked to promote a `<target>.draft` that
    is not there -- nothing was written (chainlink #102).

    Deliberately NOT an ApprovalRefused subclass, and not a
    FileNotFoundError: those two both mean "there was a draft and it was
    wrong", while this means "there was never a draft to judge". The three
    conditions an operator actually hits this by are all the same from
    here -- they named the TARGET where a DRAFT belongs, they re-ran an
    approval whose draft the first approval consumed, or the artifact was
    hand-written to its target path (the remediation gate g14 prints,
    chainlink #94/#97) -- so all three get one line that names the draft
    and the command that stages one, instead of a ~15-frame traceback
    pointing at a file the caller was never told about."""


def draft_path_for(target_path: Path) -> Path:
    """Where a target's staged draft lives: `<target>.draft`.

    The one spelling of that name, shared by stage_draft() (which writes
    it), every promotion verb (which reads it) and pipeline.py's own
    commands -- it used to be open-coded in five places across the two
    files, which is how #102's preflight ended up on one promotion verb
    and not the other two."""
    return target_path.with_suffix(target_path.suffix + ".draft")


def no_staged_drafts_message(pairs: list[tuple[Path, Path]], promoted: int) -> str:
    """The one operator-facing message for every promotion verb that finds
    no staged draft (chainlink #102).

    `pairs` is every (draft_path, target_path) whose draft is missing, so
    the paired verbs -- which promote all-or-none -- name every draft that
    is absent in one line instead of stopping at the first, and name only
    those: a pair whose other draft IS staged has one thing to fix, and
    listing the present one would send the operator to restage it.
    `promoted` is how many drafts the command promotes in total (1 for
    `approve`, 2 for the paired verbs), so the sentence stays true when
    only some of them are absent.

    One wording for the whole class, deliberately: `promote-evidence` already
    answered the identical condition with the same `no staged draft at
    <path>` opening, and a second phrasing for the same mistake is the
    drift this message exists to remove."""
    drafts = ", ".join(str(draft_path) for draft_path, _ in pairs)
    if promoted == 1:
        draft_path, target_path = pairs[0]
        remedy = (
            f"stage one with `ligature draft <stage> <template> {target_path}` "
            f"(`ligature draft --help` lists every template), or copy the target "
            f"itself to {draft_path}"
        )
        shape = "this command promotes an already-staged draft and never writes content itself"
    else:
        remedy = (
            "stage the missing one with `ligature draft <stage> <template> <target>` "
            "(`ligature draft --help` lists every template)"
        )
        shape = (
            f"this command promotes all {promoted} drafts as one transaction and "
            "never writes content itself"
        )
    return f"nothing was promoted: no staged draft at {drafts} -- {shape}: {remedy}"


def require_staged_drafts(draft_paths: tuple[Path, ...], target_paths: tuple[Path, ...]) -> None:
    """Fail closed unless every draft the promotion is about to read is
    staged (chainlink #102).

    Called at the top of all three promotion paths -- approve(),
    approve_pair() and auto_promote_if_mechanical() -- so no caller of this
    module can reach a promotion through a raw FileNotFoundError, which is
    what `approve`, `approve-pair` and `approve-exemption-pair` all did in
    1.2.0/1.2.1. A draft that is absent because a prior promotion consumed
    it is not an error to recover from: the target is already approved, and
    the sanctioned way to change it again is a fresh `draft`.

    `strict=True` on the pairing is deliberate: a caller that passed fewer
    target paths than draft paths would otherwise have the surplus drafts
    silently skipped, which is a preflight that passes because it did not
    look -- the same shape as a validation default that means 'skip'."""
    missing = [
        (draft_path, target_path)
        for draft_path, target_path in zip(draft_paths, target_paths, strict=True)
        if not draft_path.is_file()
    ]
    if missing:
        raise DraftNotStaged(no_staged_drafts_message(missing, len(draft_paths)))


def _error_findings(findings: list) -> list:
    return [finding for finding in findings if getattr(finding, "severity", "error") == "error"]


def _without_review(data: dict) -> dict:
    return {k: v for k, v in data.items() if k != "review"}


def classify(old_data: dict | None, new_data: dict) -> Classification:
    """plan.md §7.2's auto-promote carve-out, operationalized:
    mechanical only if a prior *approved* version exists (review.reviewer
    already set) and every field except `review` is byte-identical to it.
    Anything else -- first-time creation, or any semantic field changed --
    requires the human checkpoint."""
    if old_data is None:
        return "new"

    old_review = old_data.get("review") or {}
    if not old_review.get("reviewer"):
        # Prior version was itself never approved -- can't carve out an
        # already-approved baseline that doesn't exist.
        return "semantic"

    if _without_review(old_data) == _without_review(new_data):
        return "mechanical"

    return "semantic"


def requires_human_checkpoint(classification: Classification) -> bool:
    return classification != "mechanical"


def diff_artifact(old_data: dict | None, new_data: dict) -> str:
    old_text = json.dumps(old_data, indent=2, sort_keys=True).splitlines() if old_data else []
    new_text = json.dumps(new_data, indent=2, sort_keys=True).splitlines()
    return "\n".join(
        difflib.unified_diff(old_text, new_text, fromfile="prior", tofile="draft", lineterm="")
    )


@dataclass
class ApprovalResult:
    target_path: Path
    classification: Classification
    reviewer: str | None
    reviewed_at: str | None


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def stage_draft(draft_data: dict, target_path: Path) -> Path:
    """Write a candidate artifact to its draft path, never the target
    path directly -- the target path is only ever written by approve()."""
    draft_path = draft_path_for(target_path)
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(json.dumps(draft_data, indent=2) + "\n")
    return draft_path


def approve(
    draft_path: Path,
    target_path: Path,
    reviewer: str,
    reviewed_at: str | None = None,
    review_log: Path = REVIEW_LOG_DEFAULT,
    validate_fn: ValidateFn | _Sentinel = _REQUIRED,
) -> ApprovalResult:
    if not reviewer:
        raise ValueError("approve() requires a non-empty reviewer -- no default, no LLM-supplied value")
    if validate_fn is _REQUIRED:
        raise TypeError(
            "approve() requires validate_fn -- pass a real validator, or "
            "review_checkpoint.SKIP_VALIDATION to explicitly skip it. "
            "There is no default that silently skips validation."
        )

    require_staged_drafts((draft_path,), (target_path,))

    draft_data = json.loads(draft_path.read_text())
    old_data = _load(target_path)
    result_classification = classify(old_data, draft_data)

    reviewed_at = reviewed_at or datetime.now(timezone.utc).date().isoformat()
    draft_data["review"] = {"reviewer": reviewer, "reviewed_at": reviewed_at}

    if validate_fn is not SKIP_VALIDATION:
        findings = validate_fn(target_path, draft_data)
        errors = [f for f in findings if getattr(f, "severity", "error") == "error"]
        if errors:
            raise ApprovalRefused(
                f"{draft_path} fails validation, refusing to promote to {target_path}:\n"
                + "\n".join(f"  - {f}" for f in errors)
            )

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(json.dumps(draft_data, indent=2) + "\n")
    draft_path.unlink()

    review_log.parent.mkdir(parents=True, exist_ok=True)
    with review_log.open("a") as f:
        f.write(
            json.dumps(
                {
                    "target_path": str(target_path),
                    "classification": result_classification,
                    "reviewer": reviewer,
                    "reviewed_at": reviewed_at,
                    # Third review pass (2026-08-27): a skipped approval
                    # used to be indistinguishable from a validated one in
                    # the audit trail itself. Record which happened.
                    "validation": "skipped" if validate_fn is SKIP_VALIDATION else "checked",
                    "logged_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            + "\n"
        )

    return ApprovalResult(target_path, result_classification, reviewer, reviewed_at)


def _rollback_audit_log(review_log: Path, existed: bool, original_size: int) -> None:
    """Undo a prepared audit append while preserving the prior log."""
    if existed:
        with review_log.open("r+b") as stream:
            stream.truncate(original_size)
            stream.flush()
            os.fsync(stream.fileno())
    else:
        review_log.unlink(missing_ok=True)


def approve_pair(
    draft_paths: tuple[Path, Path],
    target_paths: tuple[Path, Path],
    reviewer: str,
    reviewed_at: str | None = None,
    review_log: Path = REVIEW_LOG_DEFAULT,
    validate_fn: ValidatePairFn | _Sentinel = _REQUIRED,
) -> tuple[ApprovalResult, ApprovalResult]:
    """Approve two mutually dependent drafts as one reviewed transaction.

    Both drafts receive the same human review, both are validated against
    the caller's combined in-memory view, and neither target is written
    until validation for both succeeds.  The target files are then replaced
    from prepared temporary files; a failed replacement rolls back any
    target already replaced, preserving the all-or-none contract for normal
    filesystem failures.

    This is intentionally separate from ``approve``.  A single-artifact
    approval must continue to require all cross-references to already be
    approved, while an explicit pair is the only bootstrap path for two
    artifacts whose validity depends on each other.
    """
    if not reviewer:
        raise ValueError("approve_pair() requires a non-empty reviewer")
    if validate_fn is _REQUIRED:
        raise TypeError("approve_pair() requires a real pair validator")
    if validate_fn is SKIP_VALIDATION:
        raise ValueError("approve_pair() does not permit skipped validation")
    if (
        len(draft_paths) != 2
        or len(target_paths) != 2
        or len(set(target_paths)) != 2
        or len(set(draft_paths)) != 2
    ):
        raise ValueError("approve_pair() requires two distinct draft and target paths")

    require_staged_drafts(draft_paths, target_paths)

    draft_data_by_target: dict[Path, dict] = {}
    old_data_by_target: dict[Path, dict | None] = {}
    draft_bytes: dict[Path, bytes] = {}
    for draft_path, target_path in zip(draft_paths, target_paths):
        draft_bytes[draft_path] = draft_path.read_bytes()
        draft_data = json.loads(draft_bytes[draft_path].decode("utf-8"))
        if not isinstance(draft_data, dict):
            raise ApprovalRefused(f"{draft_path} does not contain a JSON object")
        draft_data_by_target[target_path] = draft_data
        old_data_by_target[target_path] = _load(target_path)

    classifications = {
        target_path: classify(old_data_by_target[target_path], draft_data_by_target[target_path])
        for target_path in target_paths
    }
    reviewed_at = reviewed_at or datetime.now(timezone.utc).date().isoformat()
    for draft_data in draft_data_by_target.values():
        draft_data["review"] = {"reviewer": reviewer, "reviewed_at": reviewed_at}

    findings_by_target = validate_fn(draft_data_by_target)
    if set(findings_by_target) != set(target_paths):
        raise ApprovalRefused(
            "paired approval validator did not return findings for exactly both target paths; refusing to promote"
        )
    errors: list[tuple[Path, list]] = []
    for target_path in target_paths:
        target_errors = _error_findings(findings_by_target[target_path])
        if target_errors:
            errors.append((target_path, target_errors))
    if errors:
        details = "\n".join(
            f"  - {finding}" for _, target_findings in errors for finding in target_findings
        )
        raise ApprovalRefused(f"paired drafts fail validation, refusing to promote either target:\n{details}")

    payloads = {
        target_path: json.dumps(draft_data_by_target[target_path], indent=2) + "\n"
        for target_path in target_paths
    }

    audit_text = "".join(
        json.dumps(
            {
                "target_path": str(target_path),
                "classification": classifications[target_path],
                "reviewer": reviewer,
                "reviewed_at": reviewed_at,
                "validation": "checked",
                "paired": True,
                "logged_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        + "\n"
        for target_path in target_paths
    )

    for target_path in target_paths:
        target_path.parent.mkdir(parents=True, exist_ok=True)

    # Prepare the audit append before touching either target.  This catches
    # invalid/unwritable log paths while the drafts are still intact.  If a
    # later target or draft operation fails, the append is truncated back to
    # its original length in the rollback below.
    log_existed = review_log.exists()
    original_log_size = review_log.stat().st_size if log_existed else 0
    audit_touched = False
    try:
        review_log.parent.mkdir(parents=True, exist_ok=True)
        with review_log.open("a") as stream:
            audit_touched = True
            stream.write(audit_text)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        if audit_touched:
            _rollback_audit_log(review_log, log_existed, original_log_size)
        raise

    temporary_paths: dict[Path, Path] = {}
    original_bytes: dict[Path, bytes | None] = {}
    replaced: list[Path] = []
    deleted_drafts: list[Path] = []
    try:
        for target_path in target_paths:
            original_bytes[target_path] = target_path.read_bytes() if target_path.exists() else None
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{target_path.name}.", suffix=".pending", dir=target_path.parent
            )
            temporary_path = Path(temporary_name)
            temporary_paths[target_path] = temporary_path
            with os.fdopen(fd, "w") as stream:
                stream.write(payloads[target_path])
                stream.flush()
                os.fsync(stream.fileno())

        for target_path in target_paths:
            os.replace(temporary_paths[target_path], target_path)
            replaced.append(target_path)

        for draft_path in draft_paths:
            draft_path.unlink()
            deleted_drafts.append(draft_path)
    except Exception:
        for target_path in reversed(replaced):
            prior = original_bytes[target_path]
            if prior is None:
                target_path.unlink(missing_ok=True)
            else:
                target_path.write_bytes(prior)
        for draft_path in deleted_drafts:
            draft_path.write_bytes(draft_bytes[draft_path])
        _rollback_audit_log(review_log, log_existed, original_log_size)
        raise
    finally:
        for temporary_path in temporary_paths.values():
            temporary_path.unlink(missing_ok=True)

    return (
        ApprovalResult(target_paths[0], classifications[target_paths[0]], reviewer, reviewed_at),
        ApprovalResult(target_paths[1], classifications[target_paths[1]], reviewer, reviewed_at),
    )


def auto_promote_if_mechanical(
    draft_path: Path,
    target_path: Path,
    review_log: Path = REVIEW_LOG_DEFAULT,
    validate_fn: ValidateFn | _Sentinel = _REQUIRED,
) -> ApprovalResult | None:
    """Only path allowed to write target_path without a human-supplied
    reviewer -- and only when classify() says the change is mechanical
    against an already-approved prior version. Still logged, never silent.
    Also gated on validate_fn (D1) for defense in depth, even though a
    mechanical change is byte-identical to an already-approved baseline --
    that baseline could have been approved before validate_fn existed.
    Same explicit-opt-out-only rule as approve(): pass a real validator or
    SKIP_VALIDATION, never nothing."""
    if validate_fn is _REQUIRED:
        raise TypeError(
            "auto_promote_if_mechanical() requires validate_fn -- pass a "
            "real validator, or review_checkpoint.SKIP_VALIDATION to "
            "explicitly skip it."
        )

    require_staged_drafts((draft_path,), (target_path,))

    draft_data = json.loads(draft_path.read_text())
    old_data = _load(target_path)
    classification = classify(old_data, draft_data)
    if classification != "mechanical":
        return None

    if validate_fn is not SKIP_VALIDATION:
        findings = validate_fn(target_path, draft_data)
        errors = [f for f in findings if getattr(f, "severity", "error") == "error"]
        if errors:
            raise ApprovalRefused(
                f"{draft_path} fails validation, refusing to auto-promote to {target_path}:\n"
                + "\n".join(f"  - {f}" for f in errors)
            )

    prior_review = old_data.get("review", {}) if old_data else {}
    target_path.write_text(json.dumps(draft_data, indent=2) + "\n")
    draft_path.unlink()

    review_log.parent.mkdir(parents=True, exist_ok=True)
    with review_log.open("a") as f:
        f.write(
            json.dumps(
                {
                    "target_path": str(target_path),
                    "classification": "mechanical",
                    "reviewer": f"auto-promoted (carried forward from {prior_review.get('reviewer')!r})",
                    "reviewed_at": prior_review.get("reviewed_at"),
                    "validation": "skipped" if validate_fn is SKIP_VALIDATION else "checked",
                    "logged_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            + "\n"
        )

    return ApprovalResult(target_path, "mechanical", prior_review.get("reviewer"), prior_review.get("reviewed_at"))


# The four fields every writer in this module puts on an audit entry
# (approve(), approve_pair(), auto_promote_if_mechanical(), and
# generate_promotion_receipt.accept_promotion(), which mirrors this
# module's transaction discipline). An entry missing any of them did not
# come from those writers, so it never counts as proof below.
APPROVAL_ENTRY_FIELDS = ("target_path", "classification", "reviewer", "reviewed_at")


def _entry_path_matches(entry_path: str, workspace_root: Path, artifact_resolved: Path) -> bool:
    """Compare an audit entry's target_path against an accepted
    artifact's real location. Entries name the target exactly as the
    approving call received it -- an absolute path, or one relative to
    the workspace it was approved in -- so a relative entry is resolved
    against `workspace_root` before comparing resolved paths (the same
    resolve()-then-compare discipline apply()'s own target anchoring and
    validate_promotion_receipt's own containment checks use, so a `..`
    or an unusual spelling cannot alias its way to a match)."""
    try:
        path = Path(entry_path)
    except (TypeError, ValueError):
        return False
    if not path.is_absolute():
        path = workspace_root / path
    return path.resolve() == artifact_resolved


def _entry_carries_review(entry: dict, reviewer, reviewed_at) -> bool:
    """True when this audit entry records exactly the review block an
    artifact carries: same reviewer, same reviewed_at.

    The one deliberate exception is auto_promote_if_mechanical()'s
    entry, which leaves the artifact's own `review` block untouched (the
    whole point of the §7.2 mechanical carve-out is that the prior
    human review carries forward) and records the carry-forward in its
    own `reviewer` field instead -- so a mechanical entry written by
    this module is matched when it carries that exact reviewer forward
    at the same reviewed_at."""
    entry_reviewer = entry.get("reviewer")
    if entry_reviewer == reviewer:
        return entry.get("reviewed_at") == reviewed_at
    return (
        entry.get("classification") == "mechanical"
        and entry_reviewer == f"auto-promoted (carried forward from {reviewer!r})"
        and entry.get("reviewed_at") == reviewed_at
    )


def review_provenance_gaps(
    workspace_root: Path,
    reviewed_artifacts: dict[str, dict],
    review_log: Path,
) -> list[str]:
    """Prove that every review block in `reviewed_artifacts` was
    produced by this module's sanctioned approve path (chainlink #82).

    `reviewed_artifacts` maps a workspace-relative accepted-artifact path
    to the `review` block that artifact itself carries; `review_log` is
    the append-only audit log approve()/approve_pair()/
    auto_promote_if_mechanical() write to. Returns one message per
    review block with no matching approval entry (plus at most one for
    the log itself when it cannot be read at all); an empty list means
    every review block is provenanced.

    Why this exists: the `review` block a normative artifact carries is
    pure data -- `{"reviewer": ..., "reviewed_at": ...}` is exactly what
    a hand-written JSON file contains, and nothing on disk distinguishes
    it from the same block approve() wrote. A gate that accepts the
    block on its own authority is therefore unenforceable: any string at
    all satisfies `--reviewer`, which is the Stage 4.5 defect this
    function closes (an unattended agent's review blocks, ruled
    unauthorized, were enough to mint a promotion receipt over them).
    The audit log is the only record that a review event happened
    through the sanctioned path, so it -- not the block's self-assertion
    -- is what proves provenance.

    Deliberately fail-closed, with no default and no cwd-relative path
    (the same lesson generate_promotion_receipt.accept_promotion()
    already learned about REVIEW_LOG_DEFAULT): a missing, unreadable, or
    unparseable log proves nothing, so it produces a gap rather than an
    implicit pass. This is also where an entry whose `target_path`
    cannot be resolved to this workspace is refused: a review recorded
    somewhere else is not a review of this artifact.

    Trust model, stated plainly: this log is an audit trail, not a
    signature -- anyone who can write the workspace can append to it.
    What the check forecloses is the silent path (a review block that
    merely *exists* standing in for a recorded approval event); a
    deliberate forgery is a deliberate, logged act, and re-approval
    through the real path remains the sanctioned way to record a human
    ruling on an out-of-band review block."""
    if not reviewed_artifacts:
        return []

    try:
        text = review_log.read_text()
    except FileNotFoundError:
        return [
            f"no approval audit log at {review_log} -- {len(reviewed_artifacts)} accepted artifact(s) "
            "carry a review block, and only an entry in that log (written by `approve`) proves the "
            "block came from the sanctioned approve path rather than being hand-written: "
            + ", ".join(sorted(reviewed_artifacts))
        ]
    except OSError as e:
        return [f"approval audit log {review_log} cannot be read, so no review block can be proven: {e}"]

    entries: list[dict] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            return [
                f"approval audit log {review_log} line {lineno} is not valid JSON, so the log cannot be "
                "read as the approval trail it is -- fix or remove the damaged line before promoting"
            ]
        if not isinstance(entry, dict) or any(field not in entry for field in APPROVAL_ENTRY_FIELDS):
            # Not an approval entry at all -- skipped rather than an
            # error: an artifact whose only candidate entry looks like
            # this ends up unprovenanced below, which is the fail-closed
            # outcome anyway, and unrelated lines in the log are not
            # this check's business to reject.
            continue
        entries.append(entry)

    gaps: list[str] = []
    for artifact_path in sorted(reviewed_artifacts):
        review = reviewed_artifacts[artifact_path]
        reviewer = review.get("reviewer")
        reviewed_at = review.get("reviewed_at")
        artifact_resolved = (workspace_root / artifact_path).resolve()
        matched = any(
            _entry_path_matches(entry["target_path"], workspace_root, artifact_resolved)
            and _entry_carries_review(entry, reviewer, reviewed_at)
            for entry in entries
        )
        if not matched:
            gaps.append(
                f"{artifact_path}: its own review block (reviewer={reviewer!r}, reviewed_at={reviewed_at!r}) "
                f"has no matching approval entry in {review_log} -- a review block that merely exists in "
                "the artifact proves nothing about how it got there; re-run the review through the "
                "sanctioned checkpoint (`approve`, which appends that entry) to record the human ruling"
            )
    return gaps


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__ + "\n\nThis standalone CLI is read-only (classify/diff) on purpose -- "
        "it has no artifact-type knowledge, so it cannot select a validator "
        "for an arbitrary target. Promotion always goes through `pipeline.py "
        "approve`, which dispatches to the right validator by artifact type "
        "and refuses anything it doesn't recognize (a third review pass, "
        "2026-08-27, found the previous version's --skip-validation escape "
        "hatch here was itself the bypass).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    classify_p = sub.add_parser("classify", help="Show whether a draft needs a human checkpoint")
    classify_p.add_argument("draft", type=Path)
    classify_p.add_argument("target", type=Path)

    diff_p = sub.add_parser("diff", help="Show the diff a reviewer would see")
    diff_p.add_argument("draft", type=Path)
    diff_p.add_argument("target", type=Path)

    args = parser.parse_args(argv)

    if args.command == "classify":
        draft_data = json.loads(args.draft.read_text())
        old_data = _load(args.target)
        c = classify(old_data, draft_data)
        print(c)
        print("requires human checkpoint:" , requires_human_checkpoint(c))
        return 0

    if args.command == "diff":
        draft_data = json.loads(args.draft.read_text())
        old_data = _load(args.target)
        print(diff_artifact(old_data, draft_data) or "(no prior version -- new artifact)")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
