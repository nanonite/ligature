"""Tests for scripts/promote_evidence.py (chainlink #79) -- the mechanical
evidence-draft promotion path, and for validate_evidence.py's draft-skip
discovery the same issue required.

promote_evidence() is the tool-owned promotion path that closes the Stage 0
dead end: `draft` staged `evidence/E-1.json.draft`, `approve` refused
evidence by design (no `review` block), and the only way to a usable record
was an unsanctioned hand `mv`. These tests pin the promotion contract:
validate-then-rename-then-record, refusing (writing nothing) on any
G1a/G1b failure, and the audit trail.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from promote_evidence import EvidencePromotionError, promote_evidence  # noqa: E402


def load_valid() -> dict:
    return {
        "schema_version": "1.0",
        "id": "E-0143",
        "kind": "source-artifact",
        "claim": "pop_ready returns None only when no task has deadline <= now",
        "origin": {
            "repository": "https://example.com/beast-rs",
            "commit": "a1b2c3d",
            "symbol": "TaskQueue::pop_ready",
            "path": "src/queue.cpp",
            "content_hash": "sha256:" + "0" * 64,
            "line_hint": "118-160",
        },
        "semantic_disposition": "required",
        "lifecycle": "accepted",
        "confidence": "high",
        "mode": "P",
    }


class PromoteEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.evidence_dir = self.workspace / "evidence"
        self.evidence_dir.mkdir(parents=True)
        self.target = self.evidence_dir / "E-0143.json"
        self.draft = self.evidence_dir / "E-0143.json.draft"
        self.log = self.workspace / "ci" / "results" / "evidence_promotions.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _stage(self, data: dict) -> None:
        self.draft.write_text(json.dumps(data, indent=2) + "\n")

    def test_promotes_a_valid_draft(self):
        self._stage(load_valid())
        result = promote_evidence(self.draft, self.target, self.workspace, self.log)
        self.assertEqual(result, self.target)
        self.assertTrue(self.target.is_file())
        self.assertFalse(self.draft.exists(), "draft must be consumed by the rename")
        self.assertEqual(json.loads(self.target.read_text()), load_valid())

    def test_writes_an_audit_entry(self):
        self._stage(load_valid())
        promote_evidence(self.draft, self.target, self.workspace, self.log)
        self.assertTrue(self.log.is_file())
        entry = json.loads(self.log.read_text())
        self.assertEqual(entry["kind"], "evidence-promotion")
        self.assertEqual(entry["target_path"], str(self.target))
        self.assertEqual(entry["draft_path"], str(self.draft))
        self.assertEqual(entry["evidence_id"], "E-0143")
        self.assertEqual(entry["validation"], "checked")
        self.assertIn("logged_at", entry)

    def test_refuses_a_missing_draft_and_writes_nothing(self):
        with self.assertRaises(EvidencePromotionError) as ctx:
            promote_evidence(self.draft, self.target, self.workspace, self.log)
        self.assertIn("no staged draft", str(ctx.exception))
        self.assertFalse(self.target.exists())
        self.assertFalse(self.log.exists(), "a refused promotion writes no audit entry")

    def test_refuses_invalid_json(self):
        self.draft.write_text("not json [[[")
        with self.assertRaises(EvidencePromotionError) as ctx:
            promote_evidence(self.draft, self.target, self.workspace, self.log)
        self.assertIn("not valid JSON", str(ctx.exception))
        self.assertTrue(self.draft.exists(), "refused draft is left intact for correction")
        self.assertFalse(self.target.exists())

    def test_refuses_a_non_object_draft(self):
        self.draft.write_text("[1, 2, 3]")
        with self.assertRaises(EvidencePromotionError) as ctx:
            promote_evidence(self.draft, self.target, self.workspace, self.log)
        self.assertIn("not a JSON object", str(ctx.exception))
        self.assertTrue(self.draft.exists())

    def test_refuses_a_schema_invalid_draft(self):
        data = load_valid()
        del data["origin"]  # required by docs/evidence-schema.json
        self._stage(data)
        with self.assertRaises(EvidencePromotionError) as ctx:
            promote_evidence(self.draft, self.target, self.workspace, self.log)
        self.assertIn("G1a/G1b", str(ctx.exception))
        self.assertTrue(self.draft.exists(), "refused draft is left intact for correction")
        self.assertFalse(self.target.exists())
        self.assertFalse(self.log.exists())

    def test_refuses_a_draft_with_a_review_block(self):
        """docs/evidence-schema.json's additionalProperties: false rejects a
        review block -- evidence is non-normative, never a human checkpoint."""
        data = load_valid()
        data["review"] = {"reviewer": "alice", "reviewed_at": "2026-09-25"}
        self._stage(data)
        with self.assertRaises(EvidencePromotionError) as ctx:
            promote_evidence(self.draft, self.target, self.workspace, self.log)
        self.assertIn("G1a/G1b", str(ctx.exception))
        self.assertTrue(self.draft.exists())

    def test_refuses_a_draft_whose_id_does_not_match_the_target(self):
        """The naming check runs against the TARGET path, so a draft whose id
        disagrees with the target's stem is refused rather than promoted into
        a misnamed record."""
        data = load_valid()
        data["id"] = "E-9999"
        self._stage(data)
        with self.assertRaises(EvidencePromotionError) as ctx:
            promote_evidence(self.draft, self.target, self.workspace, self.log)
        self.assertIn("G1a/G1b", str(ctx.exception))
        self.assertTrue(self.draft.exists())
        self.assertFalse(self.target.exists())

    def test_promotes_a_revision_over_an_existing_target(self):
        """Evidence records can be revised (the stage-0 prompt's prior_artifact
        flow); promoting a revised draft over an existing target is allowed."""
        self._stage(load_valid())
        promote_evidence(self.draft, self.target, self.workspace, self.log)
        revised = load_valid()
        revised["confidence"] = "medium"
        self._stage(revised)
        promote_evidence(self.draft, self.target, self.workspace, self.log)
        self.assertEqual(json.loads(self.target.read_text())["confidence"], "medium")
        entries = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(len(entries), 2, "both promotions are recorded")

    def test_default_log_path_is_under_ci_results(self):
        self._stage(load_valid())
        promote_evidence(self.draft, self.target, self.workspace)
        default_log = self.workspace / "ci" / "results" / "evidence_promotions.jsonl"
        self.assertTrue(default_log.is_file())


if __name__ == "__main__":
    unittest.main()
