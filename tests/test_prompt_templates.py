"""Prompt templates (chainlink #38) can't be tested by actually invoking an
LLM here (no network/API access in this environment). Two things can be
checked without that: the templates carry the scaffolding a one-shot
contract needs, and a hand-constructed "simulated compliant output" -- what
a model following the template correctly would produce -- actually survives
the real pipeline (stage_draft -> approve -> G1a/G1b/G2+), proving the
contract is satisfiable, not just plausible-looking prose.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from review_checkpoint import approve, stage_draft  # noqa: E402
from validate_boundary_contracts import validate_file, load_schema  # noqa: E402
from jsonschema import Draft202012Validator  # noqa: E402

PROMPTS = ROOT / "prompts"


class PromptTemplateScaffoldingTest(unittest.TestCase):
    def test_stage_3_template_has_required_scaffolding(self):
        text = (PROMPTS / "stage-3-boundary-drafting.md").read_text()
        self.assertIn("Output **only** the JSON object", text)
        self.assertIn("{{finding}}", text)
        self.assertIn("{{prior_artifact}}", text)
        self.assertIn("docs/boundary-contract-schema.json", text)
        self.assertIn("adversary", text.lower())

    def test_stage_0_template_has_required_scaffolding(self):
        text = (PROMPTS / "stage-0-evidence-intake.md").read_text()
        self.assertIn("output only the json object", text.lower())
        self.assertIn("{{finding}}", text)
        self.assertIn("semantic_disposition", text)
        self.assertIn("lifecycle", text)
        self.assertIn("claim", text)


class SimulatedCompliantOutputTest(unittest.TestCase):
    """A hand-written stand-in for 'what a model following
    stage-3-boundary-drafting.md correctly would emit' -- no review block
    (per the template's instruction), pushed through the real pipeline."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = (
            self.root
            / "crate_a"
            / "specs"
            / "_boundaries"
            / "scheduler_dispatch__to__task_queue_pop_ready.json"
        )
        self.log = self.root / "review_log.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_simulated_llm_output_survives_stage_and_approve_and_validate(self):
        simulated_llm_output = {
            "schema_version": "1.0",
            "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "callee_guarantees": ["TaskQueue.C003"],
            # no "review" -- per the template's instruction
        }
        self.assertNotIn("review", simulated_llm_output)

        draft = stage_draft(simulated_llm_output, self.target)
        result = approve(draft, self.target, reviewer="test-reviewer", reviewed_at="2026-08-25", review_log=self.log)
        self.assertEqual(result.classification, "new")

        schema = load_schema()
        validator = Draft202012Validator(schema)
        findings = validate_file(self.target, validator, specs_search_root=None)
        self.assertEqual(findings, [], [str(f) for f in findings])

    def test_simulated_llm_output_that_violates_the_policy_rule_is_still_caught(self):
        """The template tells the model never to put an adversary case in
        callee_guarantees. Simulate a model that ignores that instruction --
        G2+ must still catch it after approval, since the template is
        guidance, not enforcement."""
        simulated_bad_output = {
            "schema_version": "1.0",
            "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "callee_guarantees": ["TaskQueue.A006"],
        }
        draft = stage_draft(simulated_bad_output, self.target)
        approve(draft, self.target, reviewer="test-reviewer", reviewed_at="2026-08-25", review_log=self.log)

        schema = load_schema()
        validator = Draft202012Validator(schema)
        findings = validate_file(self.target, validator, specs_search_root=None)
        self.assertTrue(any(f.gate == "G2+" for f in findings))


if __name__ == "__main__":
    unittest.main()
