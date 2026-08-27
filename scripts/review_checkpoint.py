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
  - approve() is the only path that writes to the target path. It fills in
    review.reviewer/review.reviewed_at, removes the draft, and appends an
    audit entry to ci/results/review_log.jsonl -- append-only, so a human's
    approval is traceable even if the artifact changes again later.
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
import sys
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
    draft_path = target_path.with_suffix(target_path.suffix + ".draft")
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
