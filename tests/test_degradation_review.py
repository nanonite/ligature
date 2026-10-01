"""Unit tests for scripts/degradation_review.py (chainlink #96).

#94's pilot finding, reproduced at the unit level here rather than
through the CLI: a degradation record's own `review` block is
self-asserted data, and nothing before this module checked it against
the approval audit log or required a human ruling over the record's
exact bytes. These tests exercise the shared facts in isolation --
missing logs, a forged/unlogged reviewer, a rejected ruling, a ruling
over a stale hash, and the valid case -- independent of #97's gate-g14
wiring, which does not exist yet.
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from degradation_review import (  # noqa: E402
    degradation_record_gaps,
    degradation_review_provenance_gaps,
    degradation_ruling_gaps,
)
from generate_promotion_receipt import compute_artifact_manifest  # noqa: E402

RECORD_RELATIVE = "specs/_closure/scheduler-core.degradation.json"
REVIEW = {"reviewer": "alice", "reviewed_at": "2026-09-04"}


def valid_degradation(review: dict = REVIEW) -> dict:
    return {
        "schema_version": "1.0",
        "cluster": "scheduler-core",
        "failed_conditions": ["single_verifier_system"],
        "affected_edges": ["scheduler_dispatch__to__task_queue_pop_ready"],
        "ceiling": "harness-tested",
        "capability_gap": "CG1",
        "tracking_issue": "chainlink:713",
        "review": dict(review),
    }


def _write_record(workspace: Path, data: dict, relative: str = RECORD_RELATIVE) -> Path:
    path = workspace / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path


def _record_approval(
    workspace: Path,
    relative: str,
    review: dict,
    review_log: Path,
    *,
    reviewer: str | None = None,
    reviewed_at: str | None = None,
) -> None:
    """Append the audit entry review_checkpoint.approve() would have
    written for this artifact -- the sanctioned approve path's own
    writer is exercised elsewhere (tests/test_review_checkpoint.py);
    this is fixture setup for tests whose subject is the provenance
    READER, mirroring tests/test_generate_promotion_receipt.py's own
    _record_approval helper."""
    review_log.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "target_path": str(workspace / relative),
        "classification": "new",
        "reviewer": review["reviewer"] if reviewer is None else reviewer,
        "reviewed_at": review["reviewed_at"] if reviewed_at is None else reviewed_at,
        "validation": "checked",
        "logged_at": datetime.now(timezone.utc).isoformat(),
    }
    with review_log.open("a") as stream:
        stream.write(json.dumps(entry) + "\n")


def _record_ruling(
    workspace: Path,
    relative: str,
    verdict: str,
    ruling_log: Path,
    *,
    reviewer: str = "alice",
    ruled_at: str = "2026-09-05",
    manifest: list[dict] | None = None,
) -> None:
    """Append a ruling entry shaped exactly as generate_promotion_receipt.
    record_ruling() writes one, without needing that function's own
    project-descriptor dependency -- compute_artifact_manifest() (what
    record_ruling() itself calls) needs no descriptor for an ordinary,
    non-witness file."""
    ruling_log.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "artifacts": manifest if manifest is not None else compute_artifact_manifest(workspace, [relative]),
        "verdict": verdict,
        "reviewer": reviewer,
        "ruled_at": ruled_at,
        "logged_at": datetime.now(timezone.utc).isoformat(),
    }
    with ruling_log.open("a") as stream:
        stream.write(json.dumps(entry) + "\n")


class DegradationReviewProvenanceGapsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.review_log = self.workspace / "ci" / "results" / "review_log.jsonl"
        self.record_path = _write_record(self.workspace, valid_degradation())

    def tearDown(self):
        self.tmp.cleanup()

    def _gaps(self, **overrides):
        record = overrides.pop("record", valid_degradation())
        return degradation_review_provenance_gaps(
            self.workspace, overrides.pop("record_path", self.record_path), record, self.review_log
        )

    def test_missing_review_log_is_a_gap(self):
        gaps = self._gaps()
        self.assertTrue(gaps)
        self.assertIn(str(self.review_log), gaps[0])

    def test_a_hand_written_review_block_with_no_matching_approval_entry_is_a_gap(self):
        """#94's exact reproduction: a review block naming a reviewer who
        never approved anything through the sanctioned path."""
        self.review_log.parent.mkdir(parents=True, exist_ok=True)
        self.review_log.write_text("")  # log exists but has nothing in it
        gaps = self._gaps(record=valid_degradation({"reviewer": "nobody-reviewed-this", "reviewed_at": "1999-01-01"}))
        self.assertTrue(gaps)
        self.assertIn("nobody-reviewed-this", gaps[0])

    def test_reviewer_mismatch_against_a_real_log_entry_is_a_gap(self):
        _record_approval(self.workspace, RECORD_RELATIVE, {"reviewer": "bob", "reviewed_at": "2026-09-04"}, self.review_log)
        gaps = self._gaps()  # record's own review names alice, log only has bob
        self.assertTrue(gaps)

    def test_matching_approval_entry_has_no_gap(self):
        _record_approval(self.workspace, RECORD_RELATIVE, REVIEW, self.review_log)
        self.assertEqual(self._gaps(), [])

    def test_record_with_no_review_block_is_a_gap_not_a_crash(self):
        broken = valid_degradation()
        del broken["review"]
        gaps = self._gaps(record=broken)
        self.assertTrue(gaps)
        self.assertIn("no usable review block", gaps[0])

    def test_absolute_and_workspace_relative_record_paths_agree(self):
        _record_approval(self.workspace, RECORD_RELATIVE, REVIEW, self.review_log)
        absolute_gaps = self._gaps(record_path=self.record_path.resolve())
        relative_gaps = self._gaps(record_path=Path(RECORD_RELATIVE))
        self.assertEqual(absolute_gaps, [])
        self.assertEqual(relative_gaps, [])


class DegradationRulingGapsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.ruling_log = self.workspace / "ci" / "results" / "human_rulings.jsonl"
        self.record_path = _write_record(self.workspace, valid_degradation())

    def tearDown(self):
        self.tmp.cleanup()

    def _gaps(self):
        return degradation_ruling_gaps(self.workspace, self.record_path, self.ruling_log)

    def test_missing_ruling_log_is_a_gap(self):
        gaps = self._gaps()
        self.assertTrue(gaps)
        self.assertIn(str(self.ruling_log), gaps[0])

    def test_ruling_log_with_no_entry_for_this_artifact_is_a_gap(self):
        self.ruling_log.parent.mkdir(parents=True, exist_ok=True)
        self.ruling_log.write_text("")
        gaps = self._gaps()
        self.assertTrue(gaps)
        self.assertIn(RECORD_RELATIVE, gaps[0])

    def test_rejected_ruling_is_a_gap(self):
        _record_ruling(self.workspace, RECORD_RELATIVE, "rejected", self.ruling_log)
        gaps = self._gaps()
        self.assertTrue(gaps)
        self.assertIn("REJECTED", gaps[0])

    def test_ruling_over_a_stale_hash_is_a_gap(self):
        """Ratify, then edit the record -- the ruling no longer covers
        what is actually on disk (#82's own hash-pinning discipline,
        applied here to a degradation record instead of a promotion's
        artifact set)."""
        _record_ruling(self.workspace, RECORD_RELATIVE, "ratified", self.ruling_log)
        edited = valid_degradation()
        edited["tracking_issue"] = "chainlink:999999"
        _write_record(self.workspace, edited)
        gaps = self._gaps()
        self.assertTrue(gaps)
        self.assertIn("different version", gaps[0])

    def test_ratified_ruling_over_current_bytes_has_no_gap(self):
        _record_ruling(self.workspace, RECORD_RELATIVE, "ratified", self.ruling_log)
        self.assertEqual(self._gaps(), [])

    def test_unrecognized_verdict_is_a_gap(self):
        _record_ruling(
            self.workspace, RECORD_RELATIVE, "ratified", self.ruling_log,
            manifest=compute_artifact_manifest(self.workspace, [RECORD_RELATIVE]),
        )
        # overwrite with a bogus verdict directly, bypassing record_ruling()'s own closed vocabulary check
        self.ruling_log.write_text(
            json.dumps(
                {
                    "artifacts": compute_artifact_manifest(self.workspace, [RECORD_RELATIVE]),
                    "verdict": "probably-fine",
                    "reviewer": "alice",
                    "ruled_at": "2026-09-05",
                }
            )
            + "\n"
        )
        gaps = self._gaps()
        self.assertTrue(gaps)
        self.assertIn("probably-fine", gaps[0])

    def test_latest_ruling_for_the_artifact_wins(self):
        _record_ruling(self.workspace, RECORD_RELATIVE, "rejected", self.ruling_log, ruled_at="2026-09-01")
        _record_ruling(self.workspace, RECORD_RELATIVE, "ratified", self.ruling_log, ruled_at="2026-09-05")
        self.assertEqual(self._gaps(), [])


class DegradationRecordGapsTest(unittest.TestCase):
    """degradation_record_gaps(): both checks run together, the entry
    point a gate is expected to call."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.review_log = self.workspace / "ci" / "results" / "review_log.jsonl"
        self.ruling_log = self.workspace / "ci" / "results" / "human_rulings.jsonl"
        self.record_path = _write_record(self.workspace, valid_degradation())

    def tearDown(self):
        self.tmp.cleanup()

    def _gaps(self):
        return degradation_record_gaps(
            self.workspace, self.record_path, valid_degradation(), self.review_log, self.ruling_log
        )

    def test_neither_log_existing_reports_both_gaps(self):
        gaps = self._gaps()
        self.assertEqual(len(gaps), 2)

    def test_provenanced_but_unruled_reports_only_the_ruling_gap(self):
        _record_approval(self.workspace, RECORD_RELATIVE, REVIEW, self.review_log)
        gaps = self._gaps()
        self.assertEqual(len(gaps), 1)
        self.assertIn(RECORD_RELATIVE, gaps[0])

    def test_provenanced_and_ratified_has_no_gaps(self):
        _record_approval(self.workspace, RECORD_RELATIVE, REVIEW, self.review_log)
        _record_ruling(self.workspace, RECORD_RELATIVE, "ratified", self.ruling_log)
        self.assertEqual(self._gaps(), [])

    def test_unprovenanced_and_rejected_reports_both_gaps(self):
        _record_ruling(self.workspace, RECORD_RELATIVE, "rejected", self.ruling_log)
        gaps = self._gaps()
        self.assertEqual(len(gaps), 2)


if __name__ == "__main__":
    unittest.main()
