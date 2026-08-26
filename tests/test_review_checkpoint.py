import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from review_checkpoint import (  # noqa: E402
    approve,
    auto_promote_if_mechanical,
    classify,
    requires_human_checkpoint,
    stage_draft,
)


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
        result = approve(draft, self.target, reviewer="alice", reviewed_at="2026-08-25", review_log=self.log)

        self.assertFalse(draft.exists())
        self.assertTrue(self.target.exists())
        written = json.loads(self.target.read_text())
        self.assertEqual(written["review"], {"reviewer": "alice", "reviewed_at": "2026-08-25"})
        self.assertEqual(result.classification, "new")

        log_lines = self.log.read_text().strip().splitlines()
        self.assertEqual(len(log_lines), 1)
        entry = json.loads(log_lines[0])
        self.assertEqual(entry["reviewer"], "alice")

    def test_auto_promote_refuses_semantic_change(self):
        # Seed an approved target first.
        draft = stage_draft({"boundary_id": "a__to__b", "callee_guarantees": ["B.C001"]}, self.target)
        approve(draft, self.target, reviewer="alice", reviewed_at="2026-08-01", review_log=self.log)

        # Now stage a semantically different draft -- must not auto-promote.
        draft2 = stage_draft({"boundary_id": "a__to__b", "callee_guarantees": ["B.C002"]}, self.target)
        result = auto_promote_if_mechanical(draft2, self.target, review_log=self.log)
        self.assertIsNone(result)
        self.assertTrue(draft2.exists())  # untouched, still needs a human

    def test_auto_promote_allows_mechanical_change_and_logs_it(self):
        draft = stage_draft({"boundary_id": "a__to__b", "callee_guarantees": ["B.C001"]}, self.target)
        approve(draft, self.target, reviewer="alice", reviewed_at="2026-08-01", review_log=self.log)

        # Identical except the review block -- purely mechanical.
        draft2 = stage_draft(
            {"boundary_id": "a__to__b", "callee_guarantees": ["B.C001"], "review": {"reviewer": "bot", "reviewed_at": "2026-08-02"}},
            self.target,
        )
        result = auto_promote_if_mechanical(draft2, self.target, review_log=self.log)
        self.assertIsNotNone(result)
        self.assertEqual(result.classification, "mechanical")
        self.assertFalse(draft2.exists())

        log_lines = self.log.read_text().strip().splitlines()
        self.assertEqual(len(log_lines), 2)  # original approval + auto-promote
        self.assertIn("auto-promoted", json.loads(log_lines[1])["reviewer"])


if __name__ == "__main__":
    unittest.main()
