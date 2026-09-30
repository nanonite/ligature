#!/usr/bin/env python3
"""Evidence-record promotion (chainlink #79).

Stage 0 evidence has a `draft` path but, until this module, no promotion
path at all: `pipeline.py draft 0 evidence-intake evidence/E-1.json`
staged `evidence/E-1.json.draft`, and the only way to a usable evidence
record was `mv evidence/E-1.json.draft evidence/E-1.json` by hand -- a
step the authority region of the shipped skill does not sanction and no
command performed or recorded. `approve` is unavailable by design (see
scripts/validate_evidence.py's own docstring: evidence carries no
`review` block, and review_checkpoint.approve() unconditionally injects
one, which docs/evidence-schema.json's `additionalProperties: false`
rejects).

This module is the tool-owned promotion path that closes that dead end.
`promote_evidence()` is mechanical -- no human checkpoint, matching
evidence's non-normative status (plan.md §7.2's human-checkpoint list
names "evidence-conflict resolution", not evidence itself):

  1. Read the staged draft and re-validate it (G1a/G1b) against the
     TARGET path, so the naming check (id == filename stem) runs against
     the name the record will actually carry, not the `.draft` staging
     name. Refuses on any error-severity finding -- nothing is written.
  2. Append an audit entry to `ci/results/evidence_promotions.jsonl`
     (append-only, so a promotion is traceable even if the record changes
     again later -- the same discipline review_checkpoint.approve() and
     generate_promotion_receipt.accept_promotion() already apply to
     their own audit trails).
  3. Atomically rename the draft to its target (os.replace -- one
     syscall, no partial-write window), so the `.draft` suffix never
     reaches the validator.

The audit append happens BEFORE the rename and is rolled back if the
rename fails, so there is no window where a promoted record exists with
no audit entry, or an audit entry names a promotion that never happened.

Deliberately NOT wired into review_checkpoint.approve(): that function
requires a reviewer and injects a `review` block, both of which are
wrong for evidence. This is a separate, mechanical path -- the same kind
of boundary validate_evidence.py's own docstring draws for evidence
generally.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_evidence import load_validator, validate_data  # noqa: E402


class EvidencePromotionError(Exception):
    """Raised by promote_evidence() when the staged draft cannot be
    promoted -- nothing was written (the draft is left intact for
    correction)."""


def promote_evidence(
    draft_path: Path,
    target_path: Path,
    workspace_root: Path,
    promotion_log: Path | None = None,
) -> Path:
    """Promote a staged evidence draft to its target path (chainlink #79).

    `draft_path` is the staged `<target>.draft` (as review_checkpoint.
    stage_draft writes it); `target_path` is the evidence record it will
    become. The draft is re-validated (G1a/G1b) against `target_path`
    before anything is written, and the move is recorded at
    `promotion_log` (default `<workspace_root>/ci/results/evidence_
    promotions.jsonl`).

    Refuses (EvidencePromotionError) without writing anything if the draft
    is missing, unreadable, not a JSON object, or fails G1a/G1b -- the
    draft is left intact for correction, exactly as a refused approve()
    leaves its draft. Returns the target path on success."""
    if not draft_path.is_file():
        raise EvidencePromotionError(
            f"no staged draft at {draft_path} -- run "
            f"`pipeline.py draft 0 evidence-intake {target_path}` first"
        )
    try:
        text = draft_path.read_text()
    except UnicodeDecodeError as e:
        raise EvidencePromotionError(f"staged draft {draft_path} is not readable as UTF-8 text: {e}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise EvidencePromotionError(f"staged draft {draft_path} is not valid JSON: {e}")
    if not isinstance(data, dict):
        raise EvidencePromotionError(f"staged draft {draft_path} is not a JSON object")

    validator = load_validator()
    findings = validate_data(target_path, data, validator)
    errors = [f for f in findings if getattr(f, "severity", "error") == "error"]
    if errors:
        raise EvidencePromotionError(
            f"{draft_path} fails G1a/G1b, refusing to promote to {target_path}:\n"
            + "\n".join(f"  - {f}" for f in errors)
        )

    if promotion_log is None:
        promotion_log = workspace_root / "ci" / "results" / "evidence_promotions.jsonl"

    audit_line = json.dumps(
        {
            "kind": "evidence-promotion",
            "target_path": str(target_path),
            "draft_path": str(draft_path),
            "evidence_id": data.get("id"),
            "validation": "checked",
            "logged_at": datetime.now(timezone.utc).isoformat(),
        }
    ) + "\n"

    promotion_log.parent.mkdir(parents=True, exist_ok=True)
    log_existed = promotion_log.exists()
    original_log_size = promotion_log.stat().st_size if log_existed else 0
    audit_touched = False
    try:
        with promotion_log.open("a") as stream:
            audit_touched = True
            stream.write(audit_line)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        if audit_touched:
            _rollback_audit_log(promotion_log, log_existed, original_log_size)
        raise

    try:
        os.replace(draft_path, target_path)
    except Exception:
        _rollback_audit_log(promotion_log, log_existed, original_log_size)
        raise

    return target_path


def _rollback_audit_log(promotion_log: Path, existed: bool, original_size: int) -> None:
    """Undo a prepared audit append while preserving the prior log (same
    helper review_checkpoint.py and generate_promotion_receipt.py use)."""
    if existed:
        with promotion_log.open("r+b") as stream:
            stream.truncate(original_size)
            stream.flush()
            os.fsync(stream.fileno())
    else:
        promotion_log.unlink(missing_ok=True)
