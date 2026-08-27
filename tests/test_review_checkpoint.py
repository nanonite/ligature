import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import review_checkpoint  # noqa: E402
from review_checkpoint import (  # noqa: E402
    SKIP_VALIDATION,
    ApprovalRefused,
    approve,
    auto_promote_if_mechanical,
    classify,
    requires_human_checkpoint,
    stage_draft,
)
from validate_boundary_contracts import load_validator, validate_data  # noqa: E402


class ReviewCheckpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = self.root / "specs" / "_boundaries" / "a__to__b.json"
        self.log = self.root / "review_log.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_time_creation_is_never_mechanical(self):
        c = classify(None, {"boundary_id": "a__to__b"})
        self.assertEqual(c, "new")
        self.assertTrue(requires_human_checkpoint(c))

    def test_semantic_change_requires_checkpoint(self):
        old = {"boundary_id": "a__to__b", "callee_guarantees": ["B.C001"], "review": {"reviewer": "alice", "reviewed_at": "2026-08-01"}}
        new = {"boundary_id": "a__to__b", "callee_guarantees": ["B.C002"], "review": {"reviewer": "alice", "reviewed_at": "2026-08-01"}}
        c = classify(old, new)
        self.assertEqual(c, "semantic")
        self.assertTrue(requires_human_checkpoint(c))

    def test_reformatting_only_change_against_approved_prior_is_mechanical(self):
        old = {"boundary_id": "a__to__b", "callee_guarantees": ["B.C001"], "review": {"reviewer": "alice", "reviewed_at": "2026-08-01"}}
        new = {"boundary_id": "a__to__b", "callee_guarantees": ["B.C001"], "review": {"reviewer": "someone-else", "reviewed_at": "2026-09-01"}}
        c = classify(old, new)
        self.assertEqual(c, "mechanical")
        self.assertFalse(requires_human_checkpoint(c))

    def test_change_against_never_approved_prior_is_not_mechanical(self):
        # Prior version exists but was never actually approved (reviewer
        # empty) -- there is no approved baseline to carve out against.
        old = {"boundary_id": "a__to__b", "callee_guarantees": ["B.C001"], "review": {"reviewer": "", "reviewed_at": ""}}
        new = {"boundary_id": "a__to__b", "callee_guarantees": ["B.C001"], "review": {"reviewer": "alice", "reviewed_at": "2026-08-01"}}
        c = classify(old, new)
        self.assertEqual(c, "semantic")

    def test_approve_requires_nonempty_reviewer(self):
        draft = stage_draft({"boundary_id": "a__to__b"}, self.target)
        with self.assertRaises(ValueError):
            approve(draft, self.target, reviewer="", review_log=self.log)

    def test_approve_writes_target_and_removes_draft_and_logs(self):
        draft = stage_draft({"boundary_id": "a__to__b"}, self.target)
        result = approve(
            draft, self.target, reviewer="alice", reviewed_at="2026-08-25",
            review_log=self.log, validate_fn=SKIP_VALIDATION,
        )

        self.assertFalse(draft.exists())
        self.assertTrue(self.target.exists())
        written = json.loads(self.target.read_text())
        self.assertEqual(written["review"], {"reviewer": "alice", "reviewed_at": "2026-08-25"})
        self.assertEqual(result.classification, "new")

        log_lines = self.log.read_text().strip().splitlines()
        self.assertEqual(len(log_lines), 1)
        entry = json.loads(log_lines[0])
        self.assertEqual(entry["reviewer"], "alice")
        self.assertEqual(entry["validation"], "skipped")  # SKIP_VALIDATION was passed above

    def test_audit_log_distinguishes_checked_from_skipped_validation(self):
        """Third review pass: 'a skipped approval is indistinguishable
        from a validated approval' in the persisted record. Both must be
        visible in the log, not just gated in code."""
        target_checked = self.root / "specs" / "_boundaries" / "scheduler_dispatch__to__task_queue_pop_ready.json"
        draft_checked = stage_draft({
            "schema_version": "1.0",
            "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "callee_guarantees": ["TaskQueue.C003"],
        }, target_checked)
        validator = load_validator()
        approve(
            draft_checked, target_checked, reviewer="alice", reviewed_at="2026-08-27",
            review_log=self.log, validate_fn=lambda p, d: validate_data(p, d, validator, specs_search_root=None),
        )

        target_skipped = self.root / "other" / "x.json"
        draft_skipped = stage_draft({"boundary_id": "a__to__b"}, target_skipped)
        approve(draft_skipped, target_skipped, reviewer="bob", reviewed_at="2026-08-27", review_log=self.log, validate_fn=SKIP_VALIDATION)

        entries = [json.loads(line) for line in self.log.read_text().strip().splitlines()]
        self.assertEqual(entries[0]["validation"], "checked")
        self.assertEqual(entries[1]["validation"], "skipped")

    def test_approve_without_validate_fn_raises_instead_of_silently_skipping(self):
        """Review finding D1 (round 2): validate_fn used to default to
        None, which silently meant 'skip validation' -- any caller that
        forgot to wire one in approved with no gate at all. Omitting it
        entirely must now be a hard TypeError, not a silent bypass."""
        draft = stage_draft({"boundary_id": "a__to__b"}, self.target)
        with self.assertRaises(TypeError):
            approve(draft, self.target, reviewer="alice", reviewed_at="2026-08-25", review_log=self.log)
        self.assertFalse(self.target.exists())  # nothing was written

    def test_auto_promote_without_validate_fn_raises(self):
        draft = stage_draft({"boundary_id": "a__to__b"}, self.target)
        approve(draft, self.target, reviewer="alice", reviewed_at="2026-08-01", review_log=self.log, validate_fn=SKIP_VALIDATION)
        draft2 = stage_draft(
            {"boundary_id": "a__to__b", "review": {"reviewer": "bot", "reviewed_at": "2026-08-02"}}, self.target
        )
        with self.assertRaises(TypeError):
            auto_promote_if_mechanical(draft2, self.target, review_log=self.log)

    def test_approve_refuses_a_g2_plus_failing_draft(self):
        """Review finding D1: approve() used to promote a draft with no
        gate at all. Now it must refuse before writing anything."""
        target = self.root / "crate_a" / "specs" / "_boundaries" / "scheduler_dispatch__to__task_queue_pop_ready.json"
        bad_draft = {
            "schema_version": "1.0",
            "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "callee_guarantees": ["TaskQueue.A006"],  # adversary case -- G2+ must reject
        }
        draft = stage_draft(bad_draft, target)

        validator = load_validator()

        def validate_fn(path, data):
            return validate_data(path, data, validator, specs_search_root=None)

        with self.assertRaises(ApprovalRefused) as ctx:
            approve(draft, target, reviewer="alice", reviewed_at="2026-08-25", review_log=self.log, validate_fn=validate_fn)

        self.assertIn("adversary case", str(ctx.exception))
        self.assertFalse(target.exists())  # nothing was promoted
        self.assertTrue(draft.exists())  # draft is untouched, still there to fix
        self.assertFalse(self.log.exists())  # no log entry for a refused approval

    def test_auto_promote_refuses_semantic_change(self):
        # Seed an approved target first.
        draft = stage_draft({"boundary_id": "a__to__b", "callee_guarantees": ["B.C001"]}, self.target)
        approve(draft, self.target, reviewer="alice", reviewed_at="2026-08-01", review_log=self.log, validate_fn=SKIP_VALIDATION)

        # Now stage a semantically different draft -- must not auto-promote.
        draft2 = stage_draft({"boundary_id": "a__to__b", "callee_guarantees": ["B.C002"]}, self.target)
        result = auto_promote_if_mechanical(draft2, self.target, review_log=self.log, validate_fn=SKIP_VALIDATION)
        self.assertIsNone(result)
        self.assertTrue(draft2.exists())  # untouched, still needs a human

    def test_auto_promote_allows_mechanical_change_and_logs_it(self):
        draft = stage_draft({"boundary_id": "a__to__b", "callee_guarantees": ["B.C001"]}, self.target)
        approve(draft, self.target, reviewer="alice", reviewed_at="2026-08-01", review_log=self.log, validate_fn=SKIP_VALIDATION)

        # Identical except the review block -- purely mechanical.
        draft2 = stage_draft(
            {"boundary_id": "a__to__b", "callee_guarantees": ["B.C001"], "review": {"reviewer": "bot", "reviewed_at": "2026-08-02"}},
            self.target,
        )
        result = auto_promote_if_mechanical(draft2, self.target, review_log=self.log, validate_fn=SKIP_VALIDATION)
        self.assertIsNotNone(result)
        self.assertEqual(result.classification, "mechanical")
        self.assertFalse(draft2.exists())

        log_lines = self.log.read_text().strip().splitlines()
        self.assertEqual(len(log_lines), 2)  # original approval + auto-promote
        self.assertIn("auto-promoted", json.loads(log_lines[1])["reviewer"])


class StandaloneCliCannotPromoteTest(unittest.TestCase):
    """Third review pass (2026-08-27): --skip-validation on the standalone
    CLI was itself the bypass -- explicit didn't make it safe, it just
    made an unsafe path opt-in instead of the default. The standalone CLI
    no longer offers `approve` at all; promotion only happens through
    `pipeline.py approve`, which always resolves a real validator or
    refuses. classify/diff remain -- they're read-only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = self.root / "specs" / "_boundaries" / "a__to__b.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_approve_is_not_a_recognized_subcommand(self):
        draft = stage_draft({"boundary_id": "a__to__b"}, self.target)
        with self.assertRaises(SystemExit):
            review_checkpoint.main(["approve", str(draft), str(self.target), "--reviewer", "alice"])
        self.assertFalse(self.target.exists())

    def test_classify_still_works(self):
        draft = stage_draft({"boundary_id": "a__to__b"}, self.target)
        rc = review_checkpoint.main(["classify", str(draft), str(self.target)])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
