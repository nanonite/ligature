"""Prompt templates (chainlink #38) can't be tested by actually invoking an
LLM here (no network/API access in this environment). Two things can be
checked without that: the templates carry the scaffolding a one-shot
contract needs, and a hand-constructed "simulated compliant output" -- what
a model following the template correctly would produce -- actually survives
the real pipeline (stage_draft -> approve -> G1a/G1b/G2+), proving the
contract is satisfiable, not just plausible-looking prose.
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import pipeline  # noqa: E402
from review_checkpoint import SKIP_VALIDATION, approve, stage_draft  # noqa: E402
from validate_boundary_contracts import validate_file, validate_data, load_validator  # noqa: E402

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

    def test_stage_3_bridge_template_has_required_scaffolding(self):
        text = (PROMPTS / "stage-3-bridge-drafting.md").read_text()
        self.assertIn("Output **only** the JSON object", text)
        self.assertIn("{{finding}}", text)
        self.assertIn("{{prior_artifact}}", text)
        self.assertIn("docs/bridge-schema.json", text)
        self.assertIn("caller-postcondition", text.lower())

    def test_bridge_template_states_the_compilable_fragment_rules(self):
        """chainlink #112: the template that produces bridge_logic has to
        state the rules the tool enforces on it. It documented
        `premises` only as "premise expression" and never mentioned that
        `bindings` must be snake_case, so a model following it exactly
        produced artifacts G9 refused."""
        text = (PROMPTS / "stage-3-bridge-drafting.md").read_text()
        # rule 1: predicate applications only
        self.assertIn("predicate applications only", text)
        self.assertIn("No operators", text)
        self.assertIn("quantifiers", text)
        self.assertIn("negation", text)
        # the list is the conjunction, so && inside one entry is wrong
        self.assertIn("list *is* the conjunction", text)
        self.assertIn("&&", text)
        # rule 2: snake_case bindings, named with the pilot's own counterexample
        self.assertIn("keys are snake_case", text)
        self.assertIn("GROUP_WIDTH", text)
        # both said to be ENFORCED, and before approval rather than at G9
        self.assertIn("before the artifact is staged", text)
        self.assertIn("approve", text)

    def test_bridge_template_documents_the_fragment_the_compiler_accepts(self):
        """The rules restated as the grammar the compiler actually
        accepts, so the model is not left to infer it from prose."""
        text = (PROMPTS / "stage-3-bridge-drafting.md").read_text()
        for line in (
            "expression  := obligation | call | path",
            "obligation  := Concept '.' Code '(' [args] ')'",
            "call        := path '(' [args] ')'",
            "path        := ident ('.' ident)*",
            "args        := expression (',' expression)*",
        ):
            with self.subTest(line=line):
                self.assertIn(line, text)


    def test_stage_3_witness_template_has_required_scaffolding(self):
        text = (PROMPTS / "stage-3-witness-drafting.md").read_text()
        self.assertIn("Output **only** the JSON object", text)
        self.assertIn("{{finding}}", text)
        self.assertIn("{{prior_artifact}}", text)
        self.assertIn("docs/witness-spec-schema.json", text)
        self.assertIn("value_hash", text)

    def test_stage_3_exemption_template_has_required_scaffolding(self):
        text = (PROMPTS / "stage-3-exemption-drafting.md").read_text()
        self.assertIn("Output **only** the JSON object", text)
        self.assertIn("{{finding}}", text)
        self.assertIn("{{prior_artifact}}", text)
        self.assertIn("docs/exemption-schema.json", text)
        self.assertIn("rationale", text)

    def test_stage_3_protocol_debt_template_has_required_scaffolding(self):
        text = (PROMPTS / "stage-3-protocol-debt-drafting.md").read_text()
        self.assertIn("Output **only** the JSON object", text)
        self.assertIn("{{finding}}", text)
        self.assertIn("{{prior_artifact}}", text)
        self.assertIn("docs/protocol-debt-schema.json", text)
        self.assertIn("tracking_issue", text)

    def test_stage_3_conflict_resolution_template_has_required_scaffolding(self):
        text = (PROMPTS / "stage-3-conflict-resolution-drafting.md").read_text()
        self.assertIn("Output **only** the JSON object", text)
        self.assertIn("{{finding}}", text)
        self.assertIn("{{prior_artifact}}", text)
        self.assertIn("docs/conflict-resolution-schema.json", text)
        self.assertIn("selected_authority", text)

    def test_stage_3_concept_to_code_template_has_required_scaffolding(self):
        text = (PROMPTS / "stage-3-concept-to-code.md").read_text()
        self.assertIn("Output **only** the JSON object", text)
        self.assertIn("{{finding}}", text)
        self.assertIn("{{prior_artifact}}", text)
        self.assertIn("spec.schema.json", text)
        self.assertIn("constraint", text)


class WriteSetDeclarationTest(unittest.TestCase):
    """chainlink #77: the write set was consumed by no command AND
    mentioned in no file `init` generates -- not the hash-pinned skill
    authority region, not any of the three prompts -- so the one
    declaration that keeps an implementation inside its crate's `src/`
    and `tests/` was invisible to the agent that would violate it. These
    tests pin the agent-facing half of the fix: every generated
    agent-facing document states that the write set exists and is
    binding."""

    def test_skill_declares_the_write_set_binding(self):
        text = (ROOT / "docs" / "ligature-skill.template.md").read_text()
        self.assertIn("write_set", text)
        self.assertIn("allowed_roots", text)
        self.assertIn("protected_roots", text)
        # binding, not advisory: the agent is told to write only under
        # allowed_roots and never into protected_roots
        self.assertIn("Write only under", text)
        self.assertIn("never write into `protected_roots`", text)
        # the enforcement command is in the skill's command table
        self.assertIn("write-set-check", text)

    def test_every_prompt_mentions_the_write_set(self):
        for name in (
            "stage-0-evidence-intake.md",
            "stage-3-boundary-drafting.md",
            "stage-3-interaction-drafting.md",
            "stage-3-bridge-drafting.md",
            "stage-3-witness-drafting.md",
            "stage-3-exemption-drafting.md",
            "stage-3-protocol-debt-drafting.md",
            "stage-3-conflict-resolution-drafting.md",
            "stage-3-concept-to-code.md",
        ):
            with self.subTest(prompt=name):
                text = (PROMPTS / name).read_text()
                self.assertIn("write_set", text)
                self.assertIn("allowed_roots", text)
                self.assertIn("protected_roots", text)


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

        validator = load_validator()

        def validate_fn(path, data):
            return validate_data(path, data, validator, specs_search_root=None)

        draft = stage_draft(simulated_llm_output, self.target)
        # (D1) approve() itself is now gated -- exercise the real path, not
        # just a standalone validate_file call after the fact.
        result = approve(
            draft, self.target, reviewer="test-reviewer", reviewed_at="2026-08-25",
            review_log=self.log, validate_fn=validate_fn,
        )
        self.assertEqual(result.classification, "new")

        findings = validate_file(self.target, validator, specs_search_root=None)
        errors = [f for f in findings if f.severity == "error"]
        self.assertEqual(errors, [], [str(f) for f in errors])

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
        # Deliberately SKIP_VALIDATION here -- this test is specifically
        # about the downstream validator catching what an ungated approve()
        # let through, i.e. defense in depth. #14 is where a bare approve()
        # call like this becomes impossible in the real pipeline (always
        # routed through pipeline.py's validate_fn wiring).
        approve(
            draft, self.target, reviewer="test-reviewer", reviewed_at="2026-08-25",
            review_log=self.log, validate_fn=SKIP_VALIDATION,
        )

        validator = load_validator()
        findings = validate_file(self.target, validator, specs_search_root=None)
        self.assertTrue(any(f.gate == "G2+" for f in findings))


class SimulatedBridgeOutputTest(unittest.TestCase):
    """A hand-written stand-in for 'what a model following
    stage-3-bridge-drafting.md correctly would emit' -- no review block
    (per the template's instruction), pushed through the real pipeline."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = (
            self.root
            / "crate_a"
            / "specs"
            / "_bridges"
            / "BR-SCHED-TQ-001.json"
        )
        self.log = self.root / "review_log.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_simulated_bridge_output_survives_stage_and_draft_validation(self):
        simulated_llm_output = {
            "schema_version": "1.0",
            "bridge_id": "BR-SCHED-TQ-001",
            "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
            "callee_requirement": "TaskQueue.C003",
            "available_contract_facts": [
                {"obligation_id": "Scheduler.C010", "role": "caller-precondition"},
            ],
            "target_expression": "TaskQueue.pop_ready(args, callee_state)",
            "protocol_class": "pairwise",
            "bridge_logic": {
                "bindings": {"caller_self": "Scheduler"},
                "premises": ["caller_self.ready()"],
                "conclusion": {"obligation_id": "TaskQueue.C003"},
            },
        }
        self.assertNotIn("review", simulated_llm_output)

        from validate_bridge import load_draft_validator, validate_draft_data

        validator = load_draft_validator()
        findings = validate_draft_data(self.target, simulated_llm_output, validator)
        errors = [f for f in findings if f.severity == "error"]
        self.assertEqual(errors, [], [str(f) for f in errors])

    def test_a_model_that_misses_the_fragment_rules_is_refused_at_draft(self):
        """The defect chainlink #112 reported, reproduced against the
        gate: the shape the template's field-by-field text used to
        invite. It passed draft-time G1a/G1b before this fix (neither
        rule was documented), so `draft` printed its OK line and
        `approve` wrote a human's review block onto it -- and only G9,
        reached afterwards, refused it."""
        simulated_bad_output = {
            "schema_version": "1.0",
            "bridge_id": "BR-SCHED-TQ-001",
            "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
            "callee_requirement": "TaskQueue.C003",
            "available_contract_facts": [
                {"obligation_id": "Scheduler.C010", "role": "caller-precondition"},
            ],
            "target_expression": "TaskQueue.pop_ready(args, callee_state)",
            "protocol_class": "pairwise",
            "bridge_logic": {
                "bindings": {"GROUP_WIDTH": "usize"},
                "premises": ["self.capacity() >= GROUP_WIDTH"],
                "conclusion": {"obligation_id": "TaskQueue.C003"},
            },
        }

        from validate_bridge import load_draft_validator, validate_draft_data

        findings = validate_draft_data(self.target, simulated_bad_output, load_draft_validator())
        errors = [str(f) for f in findings if f.severity == "error"]
        self.assertTrue(errors, "the bad bridge must not pass draft-time feedback")
        self.assertTrue(any("outside the compilable fragment" in e for e in errors), errors)

class SimulatedWitnessOutputTest(unittest.TestCase):
    """A hand-written stand-in for 'what a model following
    stage-3-witness-drafting.md correctly would emit' -- no review block
    (per the template's instruction), pushed through the real pipeline."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = (
            self.root
            / "crate_a"
            / "specs"
            / "_witnesses"
            / "task_queue.load_factor.json"
        )
        self.log = self.root / "review_log.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_simulated_witness_output_survives_stage_and_draft_validation(self):
        simulated_llm_output = {
            "schema_version": "1.0",
            "witness_id": "W-TQ-LOAD-FACTOR",
            "concept": "TaskQueue",
            "query": "load_factor",
            "fixture": {
                "fixture_id": "FX-TQ-LOAD-FACTOR-001",
                "seed": 42,
                "description": "A fixture with a known load factor",
            },
            "renderer": "scalar_field_svg",
            "expectation": {
                "renderer": "scalar_field_svg",
                "coverage_region": "full-grid",
                "value_distribution": "must-vary",
                "fixture_family": "FX-TQ-LOAD-FACTOR-001",
            },
            "determinism": {
                "value_hash": "sha256:" + "a" * 64,
                "claim": "byte-identical-across-runs",
                "platforms": ["x86_64-unknown-linux-gnu"],
            },
            "output": {
                "path": "docs/witnesses/task_queue.load_factor.svg",
                "render_hash": "sha256:" + "b" * 64,
                "renderer_actual": "scalar_field_svg",
            },
        }
        self.assertNotIn("review", simulated_llm_output)

        from validate_witness import load_draft_validator, validate_draft_data

        validator = load_draft_validator()
        findings = validate_draft_data(self.target, simulated_llm_output, validator)
        errors = [f for f in findings if f.severity == "error"]
        self.assertEqual(errors, [], [str(f) for f in errors])


class SimulatedExemptionOutputTest(unittest.TestCase):
    """A hand-written stand-in for 'what a model following
    stage-3-exemption-drafting.md correctly would emit' -- no review block
    (per the template's instruction), pushed through the real pipeline."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = (
            self.root
            / "crate_a"
            / "specs"
            / "_exemptions"
            / "I-SCHED-TQ-002.json"
        )
        self.log = self.root / "review_log.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_simulated_exemption_output_survives_stage_and_draft_validation(self):
        simulated_llm_output = {
            "schema_version": "1.0",
            "interaction_id": "I-SCHED-TQ-002",
            "rationale": "Prototype scaffolding boundary, tracked for removal",
        }
        self.assertNotIn("review", simulated_llm_output)

        from validate_exemption import load_draft_validator, validate_draft_data

        validator = load_draft_validator()
        findings = validate_draft_data(self.target, simulated_llm_output, validator)
        errors = [f for f in findings if f.severity == "error"]
        self.assertEqual(errors, [], [str(f) for f in errors])


class SimulatedProtocolDebtOutputTest(unittest.TestCase):
    """A hand-written stand-in for 'what a model following
    stage-3-protocol-debt-drafting.md correctly would emit' -- no review
    block (per the template's instruction), pushed through the real
    pipeline."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = (
            self.root
            / "crate_a"
            / "specs"
            / "_protocol_debt"
            / "I-SCHED-TQ-003.json"
        )
        self.log = self.root / "review_log.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_simulated_protocol_debt_output_survives_stage_and_draft_validation(self):
        simulated_llm_output = {
            "schema_version": "1.0",
            "interaction_id": "I-SCHED-TQ-003",
            "rationale": "Multi-step handshake protocol, not yet modeled",
            "no_promoted_obligation_depends_on_protocol": True,
            "no_work_package_touches_its_path": True,
            "no_release_claim_includes_it": True,
            "tracking_issue": "chainlink:#99",
        }
        self.assertNotIn("review", simulated_llm_output)

        from validate_protocol_debt import load_draft_validator, validate_draft_data

        validator = load_draft_validator()
        findings = validate_draft_data(self.target, simulated_llm_output, validator)
        errors = [f for f in findings if f.severity == "error"]
        self.assertEqual(errors, [], [str(f) for f in errors])


class SimulatedConflictResolutionOutputTest(unittest.TestCase):
    """A hand-written stand-in for 'what a model following
    stage-3-conflict-resolution-drafting.md correctly would emit' -- no
    review block (per the template's instruction), pushed through the real
    pipeline."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = (
            self.root
            / "specs"
            / "_conflicts"
            / "EC-004.json"
        )
        self.log = self.root / "review_log.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_simulated_conflict_resolution_output_survives_stage_and_draft_validation(self):
        simulated_llm_output = {
            "schema_version": "1.0",
            "conflict_id": "EC-004",
            "evidence": ["E-0143", "E-0201"],
            "status": "resolved",
            "resolution": {
                "selected_authority": "E-0201",
                "disposition_of_other": "incidental",
                "rationale": "compatibility policy: do not preserve the legacy defect",
            },
        }
        self.assertNotIn("review", simulated_llm_output)

        from validate_conflict_resolution import load_draft_validator, validate_draft_data

        validator = load_draft_validator()
        findings = validate_draft_data(self.target, simulated_llm_output, validator)
        errors = [f for f in findings if f.severity == "error"]
        self.assertEqual(errors, [], [str(f) for f in errors])


class SimulatedConceptSpecOutputTest(unittest.TestCase):
    """A hand-written stand-in for 'what a model following
    stage-3-concept-to-code.md correctly would emit' -- no review block
    (per the template's instruction), pushed through the real pipeline."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = (
            self.root
            / "crate_a"
            / "specs"
            / "task_queue.json"
        )
        self.log = self.root / "review_log.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_simulated_concept_spec_output_survives_stage_and_draft_validation(self):
        simulated_llm_output = {
            "schema_version": "1.0",
            "concept": "TaskQueue",
            "cluster": "data-model",
            "english_description": "A priority queue of tasks ordered by deadline",
            "verifier": "creusot",
            "queries": [
                {
                    "english": "Returns the number of tasks in the queue",
                    "rust_sig": "fn len(&self) -> usize",
                    "pure": True,
                    "witness_required": False,
                },
            ],
            "commands": [
                {
                    "english": "Adds a task to the queue",
                    "rust_sig": "fn push(&mut self, task: Task)",
                },
            ],
            "constraints": [
                {
                    "english": "The queue is never empty after a push",
                    "logic": "self.len() > 0",
                    "kind": "postcondition",
                    "source": "hand",
                    "id": "C001",
                },
            ],
            "adversary_table": [
                {
                    "scenario": "Pushing a task with a deadline in the past",
                    "violates": "deadline ordering",
                    "resolution": "reject",
                },
            ],
        }
        self.assertNotIn("review", simulated_llm_output)

        from select_pilot_cluster import load_concept_spec_validator

        validator = load_concept_spec_validator()
        findings = pipeline._validate_concept_spec_draft_data(
            self.target, simulated_llm_output, validator
        )
        errors = [f for f in findings if f.severity == "error"]
        self.assertEqual(errors, [], [str(f) for f in errors])


if __name__ == "__main__":
    unittest.main()
