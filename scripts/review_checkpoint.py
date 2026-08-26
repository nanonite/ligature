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
from typing import Literal

Classification = Literal["new", "mechanical", "semantic"]

REVIEW_LOG_DEFAULT = Path("ci/results/review_log.jsonl")


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
) -> ApprovalResult:
    if not reviewer:
        raise ValueError("approve() requires a non-empty reviewer -- no default, no LLM-supplied value")

    draft_data = json.loads(draft_path.read_text())
    old_data = _load(target_path)
    result_classification = classify(old_data, draft_data)

    reviewed_at = reviewed_at or datetime.now(timezone.utc).date().isoformat()
    draft_data["review"] = {"reviewer": reviewer, "reviewed_at": reviewed_at}

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
                    "logged_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            + "\n"
        )

    return ApprovalResult(target_path, result_classification, reviewer, reviewed_at)


def auto_promote_if_mechanical(
    draft_path: Path, target_path: Path, review_log: Path = REVIEW_LOG_DEFAULT
) -> ApprovalResult | None:
    """Only path allowed to write target_path without a human-supplied
    reviewer -- and only when classify() says the change is mechanical
    against an already-approved prior version. Still logged, never silent."""
    draft_data = json.loads(draft_path.read_text())
    old_data = _load(target_path)
    classification = classify(old_data, draft_data)
    if classification != "mechanical":
        return None

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
                    "logged_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            + "\n"
        )

    return ApprovalResult(target_path, "mechanical", prior_review.get("reviewer"), prior_review.get("reviewed_at"))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    classify_p = sub.add_parser("classify", help="Show whether a draft needs a human checkpoint")
    classify_p.add_argument("draft", type=Path)
    classify_p.add_argument("target", type=Path)

    diff_p = sub.add_parser("diff", help="Show the diff a reviewer would see")
    diff_p.add_argument("draft", type=Path)
    diff_p.add_argument("target", type=Path)

    approve_p = sub.add_parser("approve", help="Record human approval, promote draft to target")
    approve_p.add_argument("draft", type=Path)
    approve_p.add_argument("target", type=Path)
    approve_p.add_argument("--reviewer", required=True)
    approve_p.add_argument("--reviewed-at", default=None)

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

    if args.command == "approve":
        result = approve(args.draft, args.target, args.reviewer, args.reviewed_at)
        print(f"approved: {result.target_path} ({result.classification}) by {result.reviewer} at {result.reviewed_at}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
