import argparse
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import pipeline  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "boundary_contracts"
PROMPTS = ROOT / "prompts"


class RenderPromptTest(unittest.TestCase):
    def test_substitutes_variables(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write("Hello {{name}}, your value is {{value}}.")
            path = Path(f.name)
        try:
            out = pipeline.render_prompt(path, {"name": "world", "value": "42"})
            self.assertEqual(out, "Hello world, your value is 42.")
        finally:
            path.unlink()

    def test_malformed_placeholder_raises_instead_of_passing_through_silently(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write("Bad placeholder: {{snake_case(name)}}.")
            path = Path(f.name)
        try:
            with self.assertRaises(pipeline.PipelineError):
                pipeline.render_prompt(path, {})
        finally:
            path.unlink()

    def test_undefined_variable_raises(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write("Hello {{name}}.")
            path = Path(f.name)
        try:
            with self.assertRaises(pipeline.PipelineError):
                pipeline.render_prompt(path, {})
        finally:
            path.unlink()

    def test_does_not_choke_on_literal_json_braces_in_stage_0_template(self):
        variables = {
            "source_material": "(source)",
            "project_descriptor": "(descriptor)",
            "prior_artifact": "(none)",
            "finding": "(none)",
        }
        out = pipeline.render_prompt(PROMPTS / "stage-0-evidence-intake.md", variables)
        # The embedded worked-example JSON block's own braces/keys must
        # survive untouched -- this is the real risk case, since
        # str.format() would have choked on it (single braces).
        self.assertIn('"semantic_disposition": "required | incidental | bug-compat | unspecified"', out)
        self.assertNotRegex(out, r"\{\{[^}]*\}\}")

    def test_stage_3_template_renders_with_no_leftover_placeholders(self):
        variables = {
            "caller_concept": "Scheduler",
            "caller_method": "dispatch",
            "callee_concept": "TaskQueue",
            "callee_method": "pop_ready",
            "caller_concept_snake": "scheduler",
            "callee_concept_snake": "task_queue",
            "caller_spec": "{}",
            "callee_spec": "{}",
            "reliance_policy": "(policy text)",
            "prior_artifact": "(none)",
            "finding": "(none)",
        }
        out = pipeline.render_prompt(PROMPTS / "stage-3-boundary-drafting.md", variables)
        self.assertNotRegex(out, r"\{\{[^}]*\}\}")
        self.assertIn("scheduler_dispatch__to__task_queue_pop_ready", out)

    def test_stage_3_interaction_template_renders_with_no_leftover_placeholders(self):
        variables = {
            "interaction_id": "I-SCHED-TQ-010",
            "caller_concept": "Scheduler",
            "caller_method": "dispatch",
            "callee_concept": "TaskQueue",
            "callee_method": "pop_ready",
            "caller_spec": "{}",
            "callee_spec": "{}",
            "target_triple": "x86_64-unknown-linux-gnu",
            "prior_artifact": "(none)",
            "finding": "(none)",
        }
        out = pipeline.render_prompt(PROMPTS / "stage-3-interaction-drafting.md", variables)
        self.assertNotRegex(out, r"\{\{[^}]*\}\}")
        self.assertIn("I-SCHED-TQ-010.json", out)


class InvokeLlmBackendTest(unittest.TestCase):
    def test_manual_backend_raises(self):
        with self.assertRaises(pipeline.PipelineError):
            pipeline.invoke_llm_backend({"kind": "manual"}, "prompt text")

    def test_pipes_prompt_via_stdin_and_returns_stdout(self):
        captured = {}

        def fake_runner(argv, input, capture_output, text):
            captured["argv"] = argv
            captured["input"] = input
            return SimpleNamespace(returncode=0, stdout='{"ok": true}', stderr="")

        out = pipeline.invoke_llm_backend({"kind": "claude"}, "the prompt", runner=fake_runner)
        self.assertEqual(out, '{"ok": true}')
        self.assertEqual(captured["argv"], ["claude", "-p"])
        self.assertEqual(captured["input"], "the prompt")

    def test_nonzero_exit_raises_with_stderr(self):
        def failing_runner(argv, input, capture_output, text):
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")

        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.invoke_llm_backend({"kind": "codex"}, "prompt", runner=failing_runner)
        self.assertIn("boom", str(ctx.exception))

    def test_custom_command_override_is_used(self):
        captured = {}

        def fake_runner(argv, input, capture_output, text):
            captured["argv"] = argv
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")

        pipeline.invoke_llm_backend({"kind": "claude", "command": "claude -p --model sonnet"}, "x", runner=fake_runner)
        self.assertEqual(captured["argv"], ["claude", "-p", "--model", "sonnet"])


class ParseLlmJsonOutputTest(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(pipeline.parse_llm_json_output('{"a": 1}'), {"a": 1})

    def test_json_fenced_output_is_unwrapped(self):
        raw = '```json\n{"a": 1}\n```'
        self.assertEqual(pipeline.parse_llm_json_output(raw), {"a": 1})

    def test_garbage_raises_with_raw_output_included(self):
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.parse_llm_json_output("not json at all")
        self.assertIn("not json at all", str(ctx.exception))


_STUB_BACKEND_TEMPLATE = """
import json
import re
import sys

prompt = sys.stdin.read()
m = re.search(r"CLAIM_SOURCE: ([^`]+)", prompt)
claim = m.group(1).strip() if m else "MISSING"

record = __FIELDS__
record["claim"] = claim
print("```json")
print(json.dumps(record))
print("```")
"""


class CmdDraftEndToEndTest(unittest.TestCase):
    """Backend-plumbing coverage for cmd_draft: render_prompt ->
    invoke_llm_backend -> parse_llm_json_output -> stage_draft -> the
    immediate G1a/G1b feedback plan.md §6.1 requires ("the output ...
    is immediately run through G1a/G1b for fast local feedback"). Every
    other integration test in this file (CmdApproveIntegrationTest et
    al.) hand-constructs the artifact dict in Python and calls
    stage_draft()/approve() directly, skipping generation entirely --
    this class runs the real chain through a real subprocess
    (llm_backend.kind: claude with a `command` override, the same
    mechanism a real backend would use -- see
    InvokeLlmBackendTest.test_custom_command_override_is_used), with a
    stub script standing in for the model. The stub derives `claim` from
    a marker embedded in the rendered prompt's source_material variable,
    so that one field is proven to have flowed through render_prompt ->
    subprocess stdin -> stdout -> parse_llm_json_output, not asserted
    from a value hardcoded in the test.

    What this class does NOT cover: whether prompts/stage-0-evidence-intake.md's
    documented output contract actually matches docs/evidence-schema.json
    field-for-field -- the stub hardcodes every field but `claim`, so a
    template that drifted from the schema (dropped a required field from
    its own prose, e.g.) would not make these tests fail. See
    EvidenceTemplateSchemaContractTest below for that check. Only
    evidence (#20) has a Stage 0 template today -- the other five
    I-schema types have no draft template yet, tracked as follow-up
    chainlink issues rather than covered here."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        (self.workspace / "evidence").mkdir(parents=True)
        self.descriptor_path = self.workspace / "project-descriptor.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _write_stub_backend(self, fields: dict) -> Path:
        script = _STUB_BACKEND_TEMPLATE.replace("__FIELDS__", json.dumps(fields))
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(script)
            path = Path(f.name)
        self.addCleanup(path.unlink)
        return path

    def _write_descriptor(self, stub_path: Path) -> None:
        descriptor = dict(VALID_DESCRIPTOR)
        descriptor["llm_backend"] = {"kind": "claude", "command": f"{sys.executable} {stub_path}"}
        self.descriptor_path.write_text(json.dumps(descriptor))

    def _run_draft(self, target: Path) -> int:
        return pipeline.main([
            "--workspace", str(self.workspace),
            "--descriptor", str(self.descriptor_path),
            "draft", "0", "evidence-intake", str(target),
            "--var", "source_material=CLAIM_SOURCE: pop_ready returns None only when no task has deadline <= now",
            "--var", "project_descriptor={}",
            "--var", "prior_artifact=(none)",
            "--var", "finding=(none)",
        ])

    def test_generated_evidence_via_real_subprocess_backend_passes_the_real_validator(self):
        stub = self._write_stub_backend({
            "schema_version": "1.0",
            "id": "E-9001",
            "kind": "source-artifact",
            "origin": {
                "repository": "https://example.com/repro",
                "commit": "a1b2c3d",
                "symbol": "TaskQueue::pop_ready",
                "path": "src/queue.cpp",
                "content_hash": "sha256:" + "0" * 64,
                "line_hint": "118-160",
            },
            "semantic_disposition": "required",
            "lifecycle": "accepted",
            "confidence": "high",
            "mode": "R",
        })
        self._write_descriptor(stub)
        target = self.workspace / "evidence" / "E-9001.json"

        # rc == 0 is itself the assertion that cmd_draft's own immediate
        # G1a/G1b check passed -- not re-derived here via a second,
        # independent validate_data() call (that would test the validator
        # again, not cmd_draft's wiring to it).
        rc = self._run_draft(target)
        self.assertEqual(rc, 0)

        draft_path = target.with_suffix(".json.draft")
        self.assertTrue(draft_path.exists())
        data = json.loads(draft_path.read_text())
        # Proves the claim genuinely flowed prompt -> subprocess -> parsed
        # output, not a value asserted straight from a hand-built fixture.
        self.assertEqual(data["claim"], "pop_ready returns None only when no task has deadline <= now")

    def test_generated_evidence_missing_required_field_is_rejected_by_cmd_draft_itself(self):
        """External review, high severity: cmd_draft used to stage a draft
        and return 0 unconditionally, never running G1a/G1b at all --
        parse_llm_json_output/stage_draft succeeding was treated as
        success regardless of whether the generated content was
        schema-valid. This reproduced that gap directly (a stub omitting
        `origin`, required by docs/evidence-schema.json, previously
        returned rc == 0 from cmd_draft; only a separate, manual
        validate_data() call in this test ever caught it) before
        _select_draft_validate_fn wired the check into cmd_draft itself.
        The draft must still be written -- this is fast local feedback
        for correction, not a promotion refusal; only approve() decides
        what gets promoted."""
        stub = self._write_stub_backend({
            "schema_version": "1.0",
            "id": "E-9002",
            "kind": "source-artifact",
            "semantic_disposition": "required",
            "lifecycle": "accepted",
            "confidence": "high",
            "mode": "R",
        })
        self._write_descriptor(stub)
        target = self.workspace / "evidence" / "E-9002.json"

        rc = self._run_draft(target)
        self.assertEqual(rc, 1)

        draft_path = target.with_suffix(".json.draft")
        self.assertTrue(draft_path.exists(), "invalid draft must still be retained for correction")
        data = json.loads(draft_path.read_text())
        self.assertNotIn("origin", data)


class EvidenceTemplateSchemaContractTest(unittest.TestCase):
    """prompts/stage-0-evidence-intake.md documents its own output
    contract in prose -- nothing mechanically keeps that prose in sync
    with docs/evidence-schema.json. CmdDraftEndToEndTest's stub hardcodes
    every field but `claim`, so a template that silently dropped a
    required field from its own worked-example JSON block would not be
    caught there (external review, medium severity). This checks the
    one thing that matters for that drift: every field
    docs/evidence-schema.json actually requires is still named as a key
    at the correct nesting depth in the template's own "Output contract"
    JSON block.

    External review, low severity, second pass: the first version
    collected every quoted key across the whole fenced block into one
    flat set, so it would have passed even if a required `origin` field
    moved to the top level or appeared under some other unrelated key --
    it checked presence, not structure. Fixed by parsing the fenced block
    as real JSON (every value in it is already a quoted string, including
    the "a | b | c" enum-style placeholders, so it parses as-is) and
    checking each field exists at its actual schema location, not just
    somewhere in the document."""

    def test_template_output_contract_names_every_schema_required_field_at_the_right_nesting(self):
        schema = json.loads((ROOT / "docs" / "evidence-schema.json").read_text())
        template_text = (PROMPTS / "stage-0-evidence-intake.md").read_text()

        fence = re.search(r"```json\n(.*?)\n```", template_text, re.DOTALL)
        self.assertIsNotNone(fence, "template has no fenced output-contract JSON block")
        contract = json.loads(fence.group(1))

        for field in schema["required"]:
            self.assertIn(
                field, contract, f"schema requires {field!r} at the top level but the template's contract omits it"
            )
        origin_schema = schema["$defs"]["origin"]
        self.assertIsInstance(
            contract.get("origin"), dict,
            "schema's `origin` is a nested object but the template's contract doesn't nest it under `origin`",
        )
        for field in origin_schema["required"]:
            self.assertIn(
                field, contract["origin"],
                f"schema requires origin.{field!r} but the template contract's origin object omits it",
            )


class SelectDraftValidateFnTest(unittest.TestCase):
    """External review, high severity, second pass: cmd_draft's immediate
    validation (see CmdDraftEndToEndTest) initially delegated every
    non-evidence target to _select_validate_fn, the SAME dispatcher
    approve() uses -- but boundary/interaction/exemption/protocol-debt/
    conflict-resolution schemas all require `review` at the top level,
    and a Stage 0/3 draft never has one yet (review is only attached by
    review_checkpoint.approve(), after a human reviewer signs off). That
    meant EVERY otherwise-valid, review-less draft for those five types
    failed immediate validation unconditionally, and Stage-4-only
    cross-file gates (G2+, G15, G11, cross-references) ran at draft time
    too. Reproduced directly for boundary, interaction, and a resolved
    conflict-resolution draft before _select_draft_validate_fn was given
    dedicated, per-type draft validators (scripts/validate_*.py's own
    validate_draft_data()).

    These tests exercise pipeline._select_draft_validate_fn directly
    rather than through the full cmd_draft CLI, since only evidence and
    boundary (prompts/stage-3-boundary-drafting.md) have a real Stage 0/3
    prompt template today -- interaction/exemption/protocol-debt/
    conflict-resolution templates are #41-#44, follow-up work. See
    CmdDraftBoundaryEndToEndTest below for the CLI-level equivalent of
    the boundary cases here."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        (self.workspace / "crate_a" / "specs" / "_boundaries").mkdir(parents=True)
        (self.workspace / "crate_a" / "specs" / "_interactions").mkdir(parents=True)
        (self.workspace / "crate_a" / "specs" / "_exemptions").mkdir(parents=True)
        (self.workspace / "crate_a" / "specs" / "_protocol_debt").mkdir(parents=True)
        (self.workspace / "crate_a" / "specs" / "_bridges").mkdir(parents=True)
        (self.workspace / "evidence").mkdir(parents=True)
        (self.workspace / "specs" / "_conflicts").mkdir(parents=True)
        self._write_real_evidence("E-0143")
        self._write_real_evidence("E-0201")

    def tearDown(self):
        self.tmp.cleanup()

    def _write_real_evidence(self, evidence_id: str) -> None:
        (self.workspace / "evidence" / f"{evidence_id}.json").write_text(json.dumps({
            "schema_version": "1.0",
            "id": evidence_id,
            "kind": "source-artifact",
            "claim": "placeholder claim",
            "origin": {
                "repository": "https://example.com/repro",
                "commit": "a1b2c3d",
                "symbol": "TaskQueue::pop_ready",
                "path": "src/queue.cpp",
                "content_hash": "sha256:" + "0" * 64,
                "line_hint": "118-160",
            },
            "semantic_disposition": "required",
            "lifecycle": "accepted",
            "confidence": "high",
            "mode": "R",
        }))

    def _findings(self, target: Path, data: dict):
        validate_fn = pipeline._select_draft_validate_fn(target, self.workspace, VALID_DESCRIPTOR)
        return validate_fn(target, data)

    def test_valid_pre_review_boundary_draft_passes(self):
        """External review's exact repro: 'Boundary draft: rejected
        because review is missing.'"""
        target = self.workspace / "crate_a" / "specs" / "_boundaries" / "scheduler_dispatch__to__task_queue_pop_ready.json"
        data = {
            "schema_version": "1.0",
            "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "callee_guarantees": ["TaskQueue.C003"],
        }
        self.assertEqual(self._findings(target, data), [])

    def test_boundary_draft_with_model_supplied_review_is_rejected(self):
        target = self.workspace / "crate_a" / "specs" / "_boundaries" / "scheduler_dispatch__to__task_queue_pop_ready.json"
        data = {
            "schema_version": "1.0",
            "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "callee_guarantees": ["TaskQueue.C003"],
            "review": {"reviewer": "a-model-should-not-write-this", "reviewed_at": "2026-09-02"},
        }
        findings = self._findings(target, data)
        self.assertTrue(any("must not include its own" in f.reason for f in findings), [str(f) for f in findings])

    def test_boundary_draft_g2_plus_adversary_case_not_flagged_at_draft_time(self):
        """G2+ needs specs_search_root (cross-file) and is excluded from
        immediate feedback -- an adversary-case guarantee that approve()
        rejects (CmdApproveIntegrationTest.
        test_g2_plus_failure_is_refused_and_writes_nothing) is not this
        function's job."""
        target = self.workspace / "crate_a" / "specs" / "_boundaries" / "scheduler_dispatch__to__task_queue_pop_ready.json"
        data = {
            "schema_version": "1.0",
            "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "callee_guarantees": ["TaskQueue.A006"],
        }
        self.assertEqual(self._findings(target, data), [])

    def test_valid_pre_review_interaction_draft_passes(self):
        """External review's exact repro: 'Interaction draft: rejected
        because review is missing.'"""
        target = self.workspace / "crate_a" / "specs" / "_interactions" / "I-SCHED-TQ-001.json"
        data = {
            "schema_version": "1.0",
            "interaction_id": "I-SCHED-TQ-001",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "edge_class": ["stateful"],
            "eligibility": "boundary-required",
            "rationale": "dispatch relies on pop_ready's return discipline",
            "protocol_class": "pairwise",
            "realization": REALIZATION,
        }
        self.assertEqual(self._findings(target, data), [])

    def test_interaction_draft_with_model_supplied_review_is_rejected(self):
        target = self.workspace / "crate_a" / "specs" / "_interactions" / "I-SCHED-TQ-001.json"
        data = {
            "schema_version": "1.0",
            "interaction_id": "I-SCHED-TQ-001",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "edge_class": ["stateful"],
            "eligibility": "boundary-required",
            "rationale": "dispatch relies on pop_ready's return discipline",
            "protocol_class": "pairwise",
            "realization": REALIZATION,
            "review": {"reviewer": "a-model-should-not-write-this", "reviewed_at": "2026-09-02"},
        }
        findings = self._findings(target, data)
        self.assertTrue(any("must not include its own" in f.reason for f in findings), [str(f) for f in findings])

    def test_interaction_draft_computed_eligibility_mismatch_still_caught(self):
        """check_computed_eligibility is G1b, self-contained -- still
        part of immediate feedback (plan.md's own "cheap ... eligibility
        checks" language)."""
        target = self.workspace / "crate_a" / "specs" / "_interactions" / "I-SCHED-TQ-001.json"
        data = {
            "schema_version": "1.0",
            "interaction_id": "I-SCHED-TQ-001",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "edge_class": ["stateful"],
            "eligibility": "ignore",
            "rationale": "dispatch relies on pop_ready's return discipline",
            "protocol_class": "pairwise",
            "realization": REALIZATION,
        }
        findings = self._findings(target, data)
        self.assertTrue(
            any("does not match the value computed" in f.reason for f in findings), [str(f) for f in findings]
        )

    def test_interaction_draft_non_pairwise_with_no_debt_record_not_flagged_at_draft_time(self):
        """G15 needs cross-file protocol-debt context and is excluded
        from immediate feedback -- approve() still rejects it (see
        test_approve_non_pairwise_interaction_with_no_debt_record_is_refused)."""
        target = self.workspace / "crate_a" / "specs" / "_interactions" / "I-SCHED-TQ-004.json"
        data = {
            "schema_version": "1.0",
            "interaction_id": "I-SCHED-TQ-004",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "edge_class": ["stateful"],
            "eligibility": "boundary-required",
            "rationale": "dispatch relies on pop_ready's return discipline",
            "protocol_class": "non-pairwise",
            "realization": REALIZATION,
            "reliances": [{
                "obligation_id": "TaskQueue.C003",
                "required_assurance": {
                    "required_claims": ["postcondition-holds"],
                    "accepted_evidence_kinds": ["creusot-deductive-check"],
                    "minimum_scope": {"input_domain": "queue_len_le_8"},
                    "trust_policy": {"assumptions_allowed": []},
                },
            }],
        }
        self.assertEqual(self._findings(target, data), [])

    def test_valid_pre_review_exemption_draft_passes(self):
        target = self.workspace / "crate_a" / "specs" / "_exemptions" / "I-SCHED-TQ-002.json"
        data = {
            "schema_version": "1.0",
            "interaction_id": "I-SCHED-TQ-002",
            "rationale": "Prototype scaffolding boundary, tracked for removal (chainlink:#41)",
        }
        self.assertEqual(self._findings(target, data), [])

    def test_exemption_draft_with_model_supplied_review_is_rejected(self):
        target = self.workspace / "crate_a" / "specs" / "_exemptions" / "I-SCHED-TQ-002.json"
        data = {
            "schema_version": "1.0",
            "interaction_id": "I-SCHED-TQ-002",
            "rationale": "Prototype scaffolding boundary, tracked for removal (chainlink:#41)",
            "review": {"reviewer": "a-model-should-not-write-this", "reviewed_at": "2026-09-02"},
        }
        findings = self._findings(target, data)
        self.assertTrue(any("must not include its own" in f.reason for f in findings), [str(f) for f in findings])

    def test_valid_pre_review_protocol_debt_draft_passes(self):
        target = self.workspace / "crate_a" / "specs" / "_protocol_debt" / "I-SCHED-TQ-003.json"
        data = {
            "schema_version": "1.0",
            "interaction_id": "I-SCHED-TQ-003",
            "rationale": "Multi-step handshake protocol, not yet modeled",
            "no_promoted_obligation_depends_on_protocol": True,
            "no_work_package_touches_its_path": True,
            "no_release_claim_includes_it": True,
            "tracking_issue": "chainlink:#99",
        }
        self.assertEqual(self._findings(target, data), [])

    def test_protocol_debt_draft_for_nonexistent_interaction_not_flagged_at_draft_time(self):
        """check_interaction_cross_reference needs interactions_by_id
        (cross-file, gate G2) -- not this function's job."""
        target = self.workspace / "crate_a" / "specs" / "_protocol_debt" / "I-DOES-NOT-EXIST.json"
        data = {
            "schema_version": "1.0",
            "interaction_id": "I-DOES-NOT-EXIST",
            "rationale": "Multi-step handshake protocol, not yet modeled",
            "no_promoted_obligation_depends_on_protocol": True,
            "no_work_package_touches_its_path": True,
            "no_release_claim_includes_it": True,
            "tracking_issue": "chainlink:#99",
        }
        self.assertEqual(self._findings(target, data), [])

    def test_valid_pre_review_resolved_conflict_draft_passes(self):
        """External review's exact repro: 'Resolved conflict draft:
        rejected because review is missing' -- the schema's own
        status=='resolved' if/then requires resolution AND review; a
        Stage 3 proposal legitimately has the former without the latter
        yet."""
        target = self.workspace / "specs" / "_conflicts" / "EC-004.json"
        data = {
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
        self.assertEqual(self._findings(target, data), [])

    def test_unresolved_conflict_draft_not_flagged_by_g11_at_draft_time(self):
        """G11 ('only unresolved conflicts block') is plan.md's own Stage
        4 promotion gate -- an unresolved draft is a legitimate Stage 3
        output (surfacing the conflict for a human), not something to
        reject before it's even staged."""
        target = self.workspace / "specs" / "_conflicts" / "EC-004.json"
        data = {
            "schema_version": "1.0",
            "conflict_id": "EC-004",
            "evidence": ["E-0143", "E-0201"],
            "status": "unresolved",
        }
        self.assertEqual(self._findings(target, data), [])

    def test_conflict_draft_with_model_supplied_review_is_rejected(self):
        target = self.workspace / "specs" / "_conflicts" / "EC-004.json"
        data = {
            "schema_version": "1.0",
            "conflict_id": "EC-004",
            "evidence": ["E-0143", "E-0201"],
            "status": "resolved",
            "resolution": {
                "selected_authority": "E-0201",
                "disposition_of_other": "incidental",
                "rationale": "compatibility policy: do not preserve the legacy defect",
            },
            "review": {"reviewer": "a-model-should-not-write-this", "reviewed_at": "2026-09-02"},
        }
        findings = self._findings(target, data)
        self.assertTrue(any("must not include its own" in f.reason for f in findings), [str(f) for f in findings])

    def test_conflict_draft_dangling_evidence_not_flagged_at_draft_time(self):
        """check_evidence_cross_reference needs evidence_ids (cross-file)
        -- not this function's job at draft time; approve() still catches
        it (see test_approve_conflict_resolution_with_dangling_evidence_is_refused)."""
        target = self.workspace / "specs" / "_conflicts" / "EC-005.json"
        data = {
            "schema_version": "1.0",
            "conflict_id": "EC-005",
            "evidence": ["E-9999", "E-0201"],
            "status": "unresolved",
        }
        self.assertEqual(self._findings(target, data), [])

    def test_valid_pre_review_bridge_draft_passes(self):
        target = self.workspace / "crate_a" / "specs" / "_bridges" / "BR-SCHED-TQ-001.json"
        data = {
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
        self.assertEqual(self._findings(target, data), [])

    def test_bridge_draft_with_model_supplied_review_is_rejected(self):
        target = self.workspace / "crate_a" / "specs" / "_bridges" / "BR-SCHED-TQ-001.json"
        data = {
            "schema_version": "1.0",
            "bridge_id": "BR-SCHED-TQ-001",
            "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
            "callee_requirement": "TaskQueue.C003",
            "available_contract_facts": [],
            "target_expression": "TaskQueue.pop_ready(args, callee_state)",
            "protocol_class": "pairwise",
            "bridge_logic": {
                "bindings": {"caller_self": "Scheduler"},
                "premises": ["caller_self.ready()"],
                "conclusion": {"obligation_id": "TaskQueue.C003"},
            },
            "review": {"reviewer": "a-model-should-not-write-this", "reviewed_at": "2026-09-02"},
        }
        findings = self._findings(target, data)
        self.assertTrue(any("must not include its own" in f.reason for f in findings), [str(f) for f in findings])

    def test_bridge_draft_conclusion_mismatch_still_caught(self):
        """check_conclusion_consistency is G1b, self-contained -- still
        part of immediate feedback, mirroring
        test_interaction_draft_computed_eligibility_mismatch_still_caught."""
        target = self.workspace / "crate_a" / "specs" / "_bridges" / "BR-SCHED-TQ-001.json"
        data = {
            "schema_version": "1.0",
            "bridge_id": "BR-SCHED-TQ-001",
            "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
            "callee_requirement": "TaskQueue.C003",
            "available_contract_facts": [],
            "target_expression": "TaskQueue.pop_ready(args, callee_state)",
            "protocol_class": "pairwise",
            "bridge_logic": {
                "bindings": {"caller_self": "Scheduler"},
                "premises": ["caller_self.ready()"],
                "conclusion": {"obligation_id": "TaskQueue.C099"},
            },
        }
        findings = self._findings(target, data)
        self.assertTrue(
            any("does not match bridge_logic.conclusion.obligation_id" in f.reason for f in findings),
            [str(f) for f in findings],
        )

    def test_bridge_draft_dangling_boundary_not_flagged_at_draft_time(self):
        """G2 needs boundaries_by_id (cross-file) -- not this function's
        job at draft time; validate-bridge still catches it (see
        CmdValidateBridgeIntegrationTest.test_dangling_boundary_fails)."""
        target = self.workspace / "crate_a" / "specs" / "_bridges" / "BR-SCHED-TQ-001.json"
        data = {
            "schema_version": "1.0",
            "bridge_id": "BR-SCHED-TQ-001",
            "boundary_id": "does_not_exist",
            "callee_requirement": "TaskQueue.C003",
            "available_contract_facts": [],
            "target_expression": "TaskQueue.pop_ready(args, callee_state)",
            "protocol_class": "pairwise",
            "bridge_logic": {
                "bindings": {"caller_self": "Scheduler"},
                "premises": ["caller_self.ready()"],
                "conclusion": {"obligation_id": "TaskQueue.C003"},
            },
        }
        self.assertEqual(self._findings(target, data), [])


class CmdDraftBoundaryEndToEndTest(unittest.TestCase):
    """CLI-level counterpart to SelectDraftValidateFnTest's boundary
    cases, exercising the real cmd_draft path (prompts/stage-3-boundary-drafting.md
    already exists, unlike interaction/exemption/protocol-debt/
    conflict-resolution) through a real subprocess stub backend --
    proves the fix holds through the actual entrypoint, not just a
    direct call to the dispatcher."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        (self.workspace / "crate_a" / "specs" / "_boundaries").mkdir(parents=True)
        self.descriptor_path = self.workspace / "project-descriptor.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _write_stub_backend(self) -> Path:
        script = (
            "import sys\n"
            "sys.stdin.read()\n"
            'print(\'{"schema_version": "1.0", "boundary_id": '
            '"scheduler_dispatch__to__task_queue_pop_ready", '
            '"caller": {"concept": "Scheduler", "method": "dispatch"}, '
            '"callee": {"concept": "TaskQueue", "method": "pop_ready"}, '
            "\"callee_guarantees\": [\"TaskQueue.C003\"]}')\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(script)
            path = Path(f.name)
        self.addCleanup(path.unlink)
        return path

    def test_valid_pre_review_boundary_draft_passes_through_cmd_draft_itself(self):
        stub = self._write_stub_backend()
        descriptor = dict(VALID_DESCRIPTOR)
        descriptor["llm_backend"] = {"kind": "claude", "command": f"{sys.executable} {stub}"}
        self.descriptor_path.write_text(json.dumps(descriptor))
        target = self.workspace / "crate_a" / "specs" / "_boundaries" / "scheduler_dispatch__to__task_queue_pop_ready.json"

        rc = pipeline.main([
            "--workspace", str(self.workspace),
            "--descriptor", str(self.descriptor_path),
            "draft", "3", "boundary-drafting", str(target),
            "--var", "caller_concept=Scheduler",
            "--var", "caller_method=dispatch",
            "--var", "callee_concept=TaskQueue",
            "--var", "callee_method=pop_ready",
            "--var", "caller_concept_snake=scheduler",
            "--var", "callee_concept_snake=task_queue",
            "--var", "caller_spec={}",
            "--var", "callee_spec={}",
            "--var", "reliance_policy=(policy text)",
            "--var", "prior_artifact=(none)",
            "--var", "finding=(none)",
        ])
        self.assertEqual(rc, 0)
        self.assertTrue(target.with_suffix(".json.draft").exists())


_INTERACTION_STUB_BACKEND_TEMPLATE = """
import json
import re
import sys

prompt = sys.stdin.read()
m = re.search(r"must equal `?([A-Za-z0-9_-]+)\\.json`?", prompt)
interaction_id = m.group(1) if m else "MISSING"

record = __FIELDS__
record["interaction_id"] = interaction_id
print(json.dumps(record))
"""


class CmdDraftInteractionEndToEndTest(unittest.TestCase):
    """CLI-level end-to-end coverage for chainlink #41: cmd_draft's real
    Stage 3 path (prompts/stage-3-interaction-drafting.md) through a real
    subprocess stub backend, mirroring CmdDraftBoundaryEndToEndTest and
    CmdDraftEndToEndTest exactly. The stub derives `interaction_id` from
    a marker the template itself produces after substitution (the
    "Filename" section's "must equal {{interaction_id}}.json exactly"),
    so that field is proven to have flowed through render_prompt ->
    subprocess stdin -> stdout -> parse_llm_json_output, not asserted
    from a value hardcoded in the test.

    cmd_draft's own immediate validation (_select_draft_validate_fn ->
    validate_interaction.validate_draft_data) deliberately excludes G15
    and R2 -- both are cross-file Stage 4 concerns -- so a generated
    interaction draft does not need a covering boundary contract or
    protocol-debt record to pass cmd_draft itself; only G1a/G1b (schema,
    naming, computed eligibility, reliance-obligation uniqueness) apply
    here. Full Stage 4 coverage is exercised separately by
    CmdApproveIntegrationTest's own interaction tests."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        (self.workspace / "crate_a" / "specs" / "_interactions").mkdir(parents=True)
        self.descriptor_path = self.workspace / "project-descriptor.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _write_stub_backend(self, fields: dict) -> Path:
        script = _INTERACTION_STUB_BACKEND_TEMPLATE.replace("__FIELDS__", json.dumps(fields))
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(script)
            path = Path(f.name)
        self.addCleanup(path.unlink)
        return path

    def _write_descriptor(self, stub_path: Path) -> None:
        descriptor = dict(VALID_DESCRIPTOR)
        descriptor["llm_backend"] = {"kind": "claude", "command": f"{sys.executable} {stub_path}"}
        self.descriptor_path.write_text(json.dumps(descriptor))

    def _run_draft(self, target: Path) -> int:
        return pipeline.main([
            "--workspace", str(self.workspace),
            "--descriptor", str(self.descriptor_path),
            "draft", "3", "interaction-drafting", str(target),
            "--var", f"interaction_id={target.stem}",
            "--var", "caller_concept=Scheduler",
            "--var", "caller_method=dispatch",
            "--var", "callee_concept=TaskQueue",
            "--var", "callee_method=pop_ready",
            "--var", "caller_spec={}",
            "--var", "callee_spec={}",
            "--var", "target_triple=x86_64-unknown-linux-gnu",
            "--var", "prior_artifact=(none)",
            "--var", "finding=(none)",
        ])

    def test_generated_boundary_required_interaction_passes_cmd_drafts_own_validation(self):
        stub = self._write_stub_backend({
            "schema_version": "1.0",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "edge_class": ["stateful"],
            "eligibility": "boundary-required",
            "rationale": "dispatch's postcondition depends on pop_ready's return discipline",
            "reliances": [
                {
                    "obligation_id": "TaskQueue.C003",
                    "required_assurance": {
                        "required_claims": ["postcondition-holds"],
                        "accepted_evidence_kinds": ["creusot-deductive-check"],
                        "minimum_scope": {"input_domain": "queue_len_le_8"},
                        "trust_policy": {"assumptions_allowed": []},
                    },
                }
            ],
            "protocol_class": "pairwise",
            "realization": {
                "requirement": "required",
                "config_scope": {
                    "target": "x86_64-unknown-linux-gnu",
                    "features": ["default"],
                    "cfg": [],
                },
            },
        })
        self._write_descriptor(stub)
        target = self.workspace / "crate_a" / "specs" / "_interactions" / "I-SCHED-TQ-010.json"

        rc = self._run_draft(target)
        self.assertEqual(rc, 0)

        draft_path = target.with_suffix(".json.draft")
        self.assertTrue(draft_path.exists())
        data = json.loads(draft_path.read_text())
        # Proves interaction_id genuinely flowed prompt -> subprocess ->
        # parsed output, not a value asserted straight from a hand-built
        # fixture.
        self.assertEqual(data["interaction_id"], "I-SCHED-TQ-010")

    def test_generated_interaction_with_computed_eligibility_mismatch_is_rejected_by_cmd_draft_itself(self):
        """Negative control: a stub backend that declares eligibility
        inconsistent with edge_class (G1b's computed-eligibility check,
        plan.md §5.2) must be rejected by cmd_draft's own immediate
        validation, not just by a later manual check -- proves the
        positive test's rc == 0 is a real pass, not a vacuously
        permissive check. The draft must still be written (fast local
        feedback for correction, not a promotion refusal)."""
        stub = self._write_stub_backend({
            "schema_version": "1.0",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "edge_class": ["stateful"],  # computes to boundary-required
            "eligibility": "ignore",  # deliberately wrong
            "rationale": "dispatch's postcondition depends on pop_ready's return discipline",
            "protocol_class": "pairwise",
            "realization": {
                "requirement": "required",
                "config_scope": {
                    "target": "x86_64-unknown-linux-gnu",
                    "features": ["default"],
                    "cfg": [],
                },
            },
        })
        self._write_descriptor(stub)
        target = self.workspace / "crate_a" / "specs" / "_interactions" / "I-SCHED-TQ-011.json"

        rc = self._run_draft(target)
        self.assertEqual(rc, 1)

        draft_path = target.with_suffix(".json.draft")
        self.assertTrue(draft_path.exists(), "invalid draft must still be retained for correction")


class InteractionTemplateSchemaContractTest(unittest.TestCase):
    """prompts/stage-3-interaction-drafting.md documents its own output
    contract in a worked JSON example -- nothing mechanically keeps that
    prose in sync with docs/interaction-schema.json. External review,
    medium severity: the template's own positive E2E test stub supplies
    schema_version directly, so it could not have caught the template
    itself never telling the model to emit it. Mirrors
    EvidenceTemplateSchemaContractTest's parse-the-fence-as-real-JSON,
    check-at-correct-nesting approach, extended for interaction's nested
    realization/reliance structures and its one deliberately omitted
    field."""

    @staticmethod
    def _load_contract() -> dict:
        template_text = (PROMPTS / "stage-3-interaction-drafting.md").read_text()
        fence = re.search(r"```json\n(.*?)\n```", template_text, re.DOTALL)
        assert fence is not None, "template has no fenced output-contract JSON block"
        return json.loads(fence.group(1))

    def test_template_output_contract_names_every_schema_required_field_at_the_right_nesting(self):
        schema = json.loads((ROOT / "docs" / "interaction-schema.json").read_text())
        contract = self._load_contract()

        # `review` is deliberately excluded from model output (see the
        # template's own "review block" section) even though the schema
        # requires it -- review_checkpoint.approve() is the only thing
        # that ever attaches one.
        for field in schema["required"]:
            if field == "review":
                continue
            self.assertIn(
                field, contract, f"schema requires {field!r} at the top level but the template's contract omits it"
            )

        role_schema = schema["$defs"]["role"]
        for role_name in ("caller", "callee"):
            self.assertIsInstance(contract.get(role_name), dict, f"schema's {role_name} is a nested object")
            for field in role_schema["required"]:
                self.assertIn(
                    field, contract[role_name], f"schema requires {role_name}.{field!r}"
                )

        realization_schema = schema["$defs"]["realization"]
        self.assertIsInstance(contract.get("realization"), dict, "schema's realization is a nested object")
        for field in realization_schema["required"]:
            self.assertIn(
                field, contract["realization"], f"schema requires realization.{field!r}"
            )
        config_scope_schema = realization_schema["properties"]["config_scope"]
        self.assertIsInstance(
            contract["realization"].get("config_scope"), dict, "schema's realization.config_scope is a nested object"
        )
        for field in config_scope_schema["required"]:
            self.assertIn(
                field, contract["realization"]["config_scope"],
                f"schema requires realization.config_scope.{field!r}",
            )

        self.assertIsInstance(contract.get("reliances"), list, "schema's reliances is an array")
        self.assertTrue(contract["reliances"], "contract's reliances example must be non-empty to check its shape")
        reliance_example = contract["reliances"][0]
        reliance_schema = schema["$defs"]["reliance"]
        for field in reliance_schema["required"]:
            self.assertIn(field, reliance_example, f"schema requires reliances[].{field!r}")

        required_assurance_schema = schema["$defs"]["required_assurance"]
        self.assertIsInstance(
            reliance_example.get("required_assurance"), dict, "schema's reliances[].required_assurance is a nested object"
        )
        for field in required_assurance_schema["required"]:
            self.assertIn(
                field, reliance_example["required_assurance"],
                f"schema requires reliances[].required_assurance.{field!r}",
            )

        trust_policy_schema = required_assurance_schema["properties"]["trust_policy"]
        self.assertIsInstance(
            reliance_example["required_assurance"].get("trust_policy"), dict,
            "schema's reliances[].required_assurance.trust_policy is a nested object",
        )
        for field in trust_policy_schema["required"]:
            self.assertIn(
                field, reliance_example["required_assurance"]["trust_policy"],
                f"schema requires reliances[].required_assurance.trust_policy.{field!r}",
            )

    def test_template_output_contract_schema_version_matches_the_schemas_const(self):
        """A key named schema_version being present isn't the same claim
        as it holding the value the schema actually pins -- external
        review, low severity: the first version of this test only
        checked presence."""
        schema = json.loads((ROOT / "docs" / "interaction-schema.json").read_text())
        contract = self._load_contract()
        self.assertEqual(contract.get("schema_version"), schema["properties"]["schema_version"]["const"])

    def test_template_does_not_instruct_the_model_to_author_its_own_review_block(self):
        contract = self._load_contract()
        self.assertNotIn("review", contract, "the model must never author its own review block")


class CmdValidateIntegrationTest(unittest.TestCase):
    def test_validate_over_the_valid_fixture_crate(self):
        descriptor = json.loads(
            (ROOT / "schemas" / "examples" / "project-descriptor.greenfield.example.json").read_text()
        )
        descriptor["crates"] = [
            {
                "crate_dir": ".",
                "contracts_crate": "contracts",
                "specs_search_root": "specs",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            descriptor_path = Path(tmp) / "project-descriptor.json"
            descriptor_path.write_text(json.dumps(descriptor))

            args = argparse.Namespace(
                workspace=FIXTURES / "valid_crate",
                descriptor=descriptor_path,
            )
            self.assertEqual(pipeline.cmd_validate(args), 0)

    def test_validate_over_the_adversary_fixture_fails(self):
        descriptor = json.loads(
            (ROOT / "schemas" / "examples" / "project-descriptor.greenfield.example.json").read_text()
        )
        descriptor["crates"] = [
            {"crate_dir": ".", "contracts_crate": "contracts", "specs_search_root": "specs"}
        ]
        with tempfile.TemporaryDirectory() as tmp:
            descriptor_path = Path(tmp) / "project-descriptor.json"
            descriptor_path.write_text(json.dumps(descriptor))

            args = argparse.Namespace(
                workspace=FIXTURES / "g2_plus_adversary_as_guarantee",
                descriptor=descriptor_path,
            )
            self.assertEqual(pipeline.cmd_validate(args), 1)


INTERACTION_FIXTURES = ROOT / "tests" / "fixtures" / "interactions"
EXEMPTION_FIXTURES = ROOT / "tests" / "fixtures" / "exemptions"


def _single_crate_descriptor_path(tmp: str) -> Path:
    """Same greenfield-example-plus-single-dot-crate pattern used by
    CmdValidateIntegrationTest, factored out since both the interaction
    and exemption integration tests below need it too."""
    descriptor = json.loads(
        (ROOT / "schemas" / "examples" / "project-descriptor.greenfield.example.json").read_text()
    )
    descriptor["crates"] = [{"crate_dir": ".", "contracts_crate": "contracts", "specs_search_root": "specs"}]
    descriptor_path = Path(tmp) / "project-descriptor.json"
    descriptor_path.write_text(json.dumps(descriptor))
    return descriptor_path


class CmdValidateInteractionIntegrationTest(unittest.TestCase):
    """Exercised through pipeline.main() end to end from the start, real
    argv included -- an untested CLI entrypoint is exactly how a wiring
    gap ships invisibly regardless of library-level coverage."""

    def _run(self, workspace: Path) -> int:
        with tempfile.TemporaryDirectory() as tmp:
            descriptor_path = _single_crate_descriptor_path(tmp)
            return pipeline.main(
                ["--workspace", str(workspace), "--descriptor", str(descriptor_path), "validate-interaction"]
            )

    def test_valid_fixture_crate_passes(self):
        self.assertEqual(self._run(INTERACTION_FIXTURES / "valid"), 0)

    def test_computed_eligibility_mismatch_fails(self):
        self.assertEqual(self._run(INTERACTION_FIXTURES / "invalid_mismatch"), 1)

    def test_artifact_under_mislocated_directory_is_rejected(self):
        """External review, medium severity, SECOND pass: an earlier fix
        anchored the scan to ONLY the canonical directory, which stopped
        a mislocated artifact from being wrongly validated but also
        stopped it from being examined at all -- a crate with no
        specs/_interactions and a fully schema-valid artifact under
        not_specs/_interactions/ still reported OK, reproducing the same
        zero-findings outcome by omission instead of false acceptance.
        The fixture artifact here is otherwise completely valid (correct
        schema, correct computed eligibility, correct filename) -- only
        its location is wrong, proving location alone causes rejection."""
        self.assertEqual(self._run(INTERACTION_FIXTURES / "mislocated"), 1)

    def test_boundary_required_interaction_with_no_boundary_or_exemption_is_rejected(self):
        """Chainlink #46 (R2), end to end through cmd_validate_interaction:
        a boundary-required interaction with no sibling _boundaries or
        _exemptions directory at all must fail closed, not report OK."""
        self.assertEqual(self._run(INTERACTION_FIXTURES / "uncovered_r2"), 1)


class CmdValidateExemptionIntegrationTest(unittest.TestCase):
    def _run(self, workspace: Path) -> int:
        with tempfile.TemporaryDirectory() as tmp:
            descriptor_path = _single_crate_descriptor_path(tmp)
            return pipeline.main(
                ["--workspace", str(workspace), "--descriptor", str(descriptor_path), "validate-exemption"]
            )

    def test_valid_fixture_crate_passes(self):
        self.assertEqual(self._run(EXEMPTION_FIXTURES / "valid"), 0)

    def test_naming_mismatch_fails(self):
        self.assertEqual(self._run(EXEMPTION_FIXTURES / "invalid_naming"), 1)

    def test_artifact_under_mislocated_directory_is_rejected(self):
        """Mirrors CmdValidateInteractionIntegrationTest's equivalent --
        the fixture artifact under not_specs/_exemptions/ is otherwise
        fully valid (correct schema, correct filename); only its
        location is wrong."""
        self.assertEqual(self._run(EXEMPTION_FIXTURES / "mislocated"), 1)


PROTOCOL_DEBT_FIXTURES = ROOT / "tests" / "fixtures" / "protocol_debt"


class CmdValidateProtocolDebtIntegrationTest(unittest.TestCase):
    def _run(self, workspace: Path) -> int:
        with tempfile.TemporaryDirectory() as tmp:
            descriptor_path = _single_crate_descriptor_path(tmp)
            return pipeline.main(
                ["--workspace", str(workspace), "--descriptor", str(descriptor_path), "validate-protocol-debt"]
            )

    def test_valid_fixture_crate_passes(self):
        self.assertEqual(self._run(PROTOCOL_DEBT_FIXTURES / "valid"), 0)

    def test_false_attestation_fails(self):
        self.assertEqual(self._run(PROTOCOL_DEBT_FIXTURES / "invalid_false_attestation"), 1)

    def test_artifact_under_mislocated_directory_is_rejected(self):
        self.assertEqual(self._run(PROTOCOL_DEBT_FIXTURES / "mislocated"), 1)


BRIDGE_FIXTURES = ROOT / "tests" / "fixtures" / "bridges"


class CmdValidateBridgeIntegrationTest(unittest.TestCase):
    def _run(self, workspace: Path) -> int:
        with tempfile.TemporaryDirectory() as tmp:
            descriptor_path = _single_crate_descriptor_path(tmp)
            return pipeline.main(
                ["--workspace", str(workspace), "--descriptor", str(descriptor_path), "validate-bridge"]
            )

    def test_valid_fixture_crate_passes(self):
        self.assertEqual(self._run(BRIDGE_FIXTURES / "valid"), 0)

    def test_dangling_boundary_fails(self):
        self.assertEqual(self._run(BRIDGE_FIXTURES / "dangling_boundary"), 1)

    def test_artifact_under_mislocated_directory_is_rejected(self):
        self.assertEqual(self._run(BRIDGE_FIXTURES / "mislocated"), 1)


EVIDENCE_FIXTURES = ROOT / "tests" / "fixtures" / "evidence"
CONFLICT_RESOLUTION_FIXTURES = ROOT / "tests" / "fixtures" / "conflict_resolution"


class CmdValidateEvidenceIntegrationTest(unittest.TestCase):
    """Evidence is workspace-level, not crate-scoped -- no project
    descriptor is loaded by cmd_validate_evidence at all, so no
    --descriptor flag is needed here (unlike every crate-scoped
    validate-* subcommand above)."""

    def _run(self, workspace: Path) -> int:
        return pipeline.main(["--workspace", str(workspace), "validate-evidence"])

    def test_valid_fixture_passes(self):
        self.assertEqual(self._run(EVIDENCE_FIXTURES / "valid"), 0)

    def test_artifact_under_mislocated_directory_is_rejected(self):
        self.assertEqual(self._run(EVIDENCE_FIXTURES / "mislocated"), 1)


class CmdValidateConflictResolutionIntegrationTest(unittest.TestCase):
    def _run(self, workspace: Path) -> int:
        return pipeline.main(["--workspace", str(workspace), "validate-conflict-resolution"])

    def test_valid_resolved_fixture_passes(self):
        self.assertEqual(self._run(CONFLICT_RESOLUTION_FIXTURES / "valid"), 0)

    def test_unresolved_conflict_fails_g11(self):
        self.assertEqual(self._run(CONFLICT_RESOLUTION_FIXTURES / "unresolved"), 1)


WP_FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "work_packages" / "valid"
PROMOTION_FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "promotions" / "valid"


class CmdValidatePromotionIntegrationTest(unittest.TestCase):
    """Exercised through pipeline.main() end to end from the start --
    the review chain on validate-work-package found that an untested CLI
    entrypoint is exactly how a wiring gap ships invisibly regardless of
    how well-tested the underlying library functions are."""

    def _run(self, *args):
        return pipeline.main(["--workspace", str(PROMOTION_FIXTURE_ROOT), "validate-promotion", *args])

    def test_valid_receipt_passes(self):
        receipt = PROMOTION_FIXTURE_ROOT / "specs" / "_promotions" / "scheduling.json"
        rc = self._run(str(receipt))
        self.assertEqual(rc, 0)

    def test_yaml_receipt_passes(self):
        receipt = PROMOTION_FIXTURE_ROOT / "specs" / "_promotions" / "scheduling.yaml"
        rc = self._run(str(receipt))
        self.assertEqual(rc, 0)

    def test_hash_mismatch_fails(self):
        data = json.loads((PROMOTION_FIXTURE_ROOT / "specs" / "_promotions" / "scheduling.json").read_text())
        data["artifact_manifest"][0]["hash"] = "sha256:" + "0" * 64
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            bad_receipt = Path(f.name)
        try:
            rc = self._run(str(bad_receipt))
            self.assertEqual(rc, 1)
        finally:
            bad_receipt.unlink()


class CmdAcceptPromotionIntegrationTest(unittest.TestCase):
    """Exercised through pipeline.main() end to end -- chainlink #45's own
    scope. Uses its own isolated tempdir workspace rather than the shared
    PROMOTION_FIXTURE_ROOT, since accept-promotion writes a real file
    (and a real audit log) and must never touch the checked-in fixture
    tree other tests in this file rely on, or this repository's own
    ci/results/review_log.jsonl."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        (self.workspace / "project-descriptor.json").write_text(json.dumps({
            "schema_version": "1.0",
            "project": {"name": "repro", "crate_naming_convention": "^repro-[a-z]+"},
            "mode": "greenfield",
            "crates": [
                {
                    "crate_dir": "crates/scheduler",
                    "contracts_crate": "contracts",
                    "specs_search_root": "crates/scheduler/specs",
                }
            ],
            "verifier_policy": {"default": "creusot"},
            "compatibility_policy": {"reliance_policy_path": "docs/reliance-policy.md"},
            "write_set": {"allowed_roots": [], "protected_roots": []},
            "gate_integrity": [],
            "review": {"reviewer": "repro", "reviewed_at": "2026-08-27"},
        }))
        (self.workspace / "docs").mkdir()
        (self.workspace / "docs" / "reliance-policy.md").write_text(
            "# Reliance policy\n\n"
            "Schema version this policy targets: `1.0`.\n"
            "Owner: `platform-team`.\n"
            "Policy version: `reliance-policy@1.2`\n"
        )
        boundary_dir = self.workspace / "crates" / "scheduler" / "specs" / "_boundaries"
        boundary_dir.mkdir(parents=True)
        (boundary_dir / "scheduler_dispatch__to__task_queue_pop_ready.json").write_text(json.dumps({
            "schema_version": "1.0",
            "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "callee_guarantees": ["TaskQueue.C003"],
            "review": {"reviewer": "alice", "reviewed_at": "2026-08-30"},
        }))
        self.artifacts = [
            "docs/reliance-policy.md",
            "crates/scheduler/specs/_boundaries/scheduler_dispatch__to__task_queue_pop_ready.json",
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *args):
        return pipeline.main(["--workspace", str(self.workspace), "accept-promotion", *args])

    def test_generates_a_receipt_that_validate_promotion_then_accepts(self):
        rc = self._run(
            "scheduling",
            "--reviewer", "alice",
            "--policy-path", "docs/reliance-policy.md",
            "--artifact", self.artifacts[0],
            "--artifact", self.artifacts[1],
            "--accepted-at", "2026-09-05",
        )
        self.assertEqual(rc, 0)

        receipt_path = self.workspace / "specs" / "_promotions" / "scheduling.json"
        self.assertTrue(receipt_path.exists())
        data = json.loads(receipt_path.read_text())
        self.assertEqual(data["promotion_id"], "PROM-SCHEDULING-001")
        self.assertEqual(data["schema_versions"], {"boundary": "1.0"})
        self.assertEqual(data["policy_version"], "reliance-policy@1.2")

        validate_rc = pipeline.main(
            ["--workspace", str(self.workspace), "validate-promotion", str(receipt_path)]
        )
        self.assertEqual(validate_rc, 0)

    def test_audit_log_is_written_inside_the_workspace_not_this_repository(self):
        """External review, medium severity: the CLI used to rely on
        accept_promotion()'s cwd-relative default, so accepting a
        promotion for an external/temporary workspace wrote its audit
        trail into this repository's own ci/results/review_log.jsonl."""
        repo_log = ROOT / "ci" / "results" / "review_log.jsonl"
        repo_log_size_before = repo_log.stat().st_size if repo_log.exists() else None

        rc = self._run(
            "scheduling",
            "--reviewer", "alice",
            "--policy-path", "docs/reliance-policy.md",
            "--artifact", self.artifacts[0],
            "--artifact", self.artifacts[1],
        )
        self.assertEqual(rc, 0)

        workspace_log = self.workspace / "ci" / "results" / "review_log.jsonl"
        self.assertTrue(workspace_log.exists())
        repo_log_size_after = repo_log.stat().st_size if repo_log.exists() else None
        self.assertEqual(repo_log_size_before, repo_log_size_after)

    def test_empty_reviewer_is_refused(self):
        rc = self._run(
            "scheduling",
            "--reviewer", "",
            "--policy-path", "docs/reliance-policy.md",
            "--artifact", self.artifacts[0],
            "--artifact", self.artifacts[1],
        )
        self.assertEqual(rc, 1)
        self.assertFalse((self.workspace / "specs" / "_promotions" / "scheduling.json").exists())

    def test_bad_artifact_path_is_refused(self):
        rc = self._run(
            "scheduling",
            "--reviewer", "alice",
            "--policy-path", "does/not/exist.md",
            "--artifact", "does/not/exist.md",
        )
        self.assertEqual(rc, 1)
        self.assertFalse((self.workspace / "specs" / "_promotions" / "scheduling.json").exists())

    def test_policy_document_with_no_version_marker_is_refused(self):
        """External review, high severity, third pass: a claimed
        policy_version with nothing in the file to back it previously
        passed with zero findings."""
        (self.workspace / "docs" / "reliance-policy.md").write_text("# Reliance policy\n\nNo marker.\n")
        rc = self._run(
            "scheduling",
            "--reviewer", "alice",
            "--policy-path", "docs/reliance-policy.md",
            "--artifact", self.artifacts[0],
            "--artifact", self.artifacts[1],
        )
        self.assertEqual(rc, 1)
        self.assertFalse((self.workspace / "specs" / "_promotions" / "scheduling.json").exists())

    def test_malformed_artifact_under_a_kind_directory_is_refused(self):
        """External review, high severity, third pass: a boundary
        contract containing only {"schema_version": "999.0", "garbage":
        true} previously produced a receipt declaring "boundary":
        "999.0" with zero findings."""
        garbage_path = (
            self.workspace / "crates" / "scheduler" / "specs" / "_boundaries"
            / "scheduler_dispatch__to__task_queue_pop_ready.json"
        )
        garbage_path.write_text(json.dumps({"schema_version": "999.0", "garbage": True}))
        rc = self._run(
            "scheduling",
            "--reviewer", "alice",
            "--policy-path", "docs/reliance-policy.md",
            "--artifact", self.artifacts[0],
            "--artifact", self.artifacts[1],
        )
        self.assertEqual(rc, 1)
        self.assertFalse((self.workspace / "specs" / "_promotions" / "scheduling.json").exists())

    def test_no_recognized_artifact_kind_is_refused(self):
        """schema_versions can't be computed from a set with no
        boundary/interaction/exemption/protocol_debt/evidence/
        conflict_resolution artifact in it."""
        rc = self._run(
            "scheduling",
            "--reviewer", "alice",
            "--policy-path", "docs/reliance-policy.md",
            "--artifact", self.artifacts[0],
        )
        self.assertEqual(rc, 1)
        self.assertFalse((self.workspace / "specs" / "_promotions" / "scheduling.json").exists())

    def test_reviewed_exemption_referencing_a_nonexistent_interaction_is_refused(self):
        """External review, high severity, fourth pass: a hand-authored
        review block on an exemption is not enough -- the interaction it
        names must actually resolve via the real crate's own
        _interactions/ directory, not degrade to a non-blocking info
        finding for lack of cross-file context."""
        exemption_dir = self.workspace / "crates" / "scheduler" / "specs" / "_exemptions"
        exemption_dir.mkdir(parents=True)
        (exemption_dir / "I-DOES-NOT-EXIST.json").write_text(json.dumps({
            "schema_version": "1.0",
            "interaction_id": "I-DOES-NOT-EXIST",
            "rationale": "Prototype scaffolding boundary, tracked for removal (chainlink:#41)",
            "review": {"reviewer": "alice", "reviewed_at": "2026-08-30"},
        }))
        rc = self._run(
            "scheduling",
            "--reviewer", "alice",
            "--policy-path", "docs/reliance-policy.md",
            "--artifact", self.artifacts[0],
            "--artifact", self.artifacts[1],
            "--artifact", "crates/scheduler/specs/_exemptions/I-DOES-NOT-EXIST.json",
        )
        self.assertEqual(rc, 1)
        self.assertFalse((self.workspace / "specs" / "_promotions" / "scheduling.json").exists())

    def test_boundary_at_a_non_canonical_directory_is_not_trusted(self):
        """External review, medium severity, fourth pass: a schema-valid
        boundary at junk/_boundaries/<id>.json -- not the descriptor's
        real crate boundary directory -- must not have its
        schema_version trusted."""
        stray_dir = self.workspace / "junk" / "_boundaries"
        stray_dir.mkdir(parents=True)
        (stray_dir / "scheduler_dispatch__to__task_queue_pop_ready.json").write_text(json.dumps({
            "schema_version": "1.0",
            "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "callee_guarantees": ["TaskQueue.C003"],
            "review": {"reviewer": "alice", "reviewed_at": "2026-08-30"},
        }))
        rc = self._run(
            "scheduling",
            "--reviewer", "alice",
            "--policy-path", "docs/reliance-policy.md",
            "--artifact", self.artifacts[0],
            "--artifact", self.artifacts[1],
            "--artifact", "junk/_boundaries/scheduler_dispatch__to__task_queue_pop_ready.json",
        )
        self.assertEqual(rc, 0)
        data = json.loads((self.workspace / "specs" / "_promotions" / "scheduling.json").read_text())
        self.assertEqual(data["schema_versions"], {"boundary": "1.0"})


class CmdValidateWorkPackageIntegrationTest(unittest.TestCase):
    """Exercised through pipeline.main() end to end, matching the pattern
    used for validate/approve -- not just the underlying library call."""

    def _run(self, *args):
        return pipeline.main(["--workspace", str(WP_FIXTURE_ROOT), "validate-work-package", *args])

    def test_valid_manifest_passes(self):
        manifest = WP_FIXTURE_ROOT / "ci" / "manifest" / "WP-SCHED-001.json"
        rc = self._run(
            str(manifest), "--specs-search-root", str(WP_FIXTURE_ROOT / "crates" / "scheduler" / "specs")
        )
        self.assertEqual(rc, 0)

    def test_omitting_search_root_defaults_to_workspace_and_still_resolves_assumptions(self):
        """Not 'degrades to info and passes anyway' -- omitting
        --specs-search-root defaults it to --workspace (external review,
        high severity: the flag used to default to None, which let
        assumption-ref resolution silently not run at all). The check
        still genuinely executes and succeeds here because the fixture's
        boundary really is reachable from the workspace root."""
        manifest = WP_FIXTURE_ROOT / "ci" / "manifest" / "WP-SCHED-001.json"
        rc = self._run(str(manifest))
        self.assertEqual(rc, 0)

    def test_search_root_pointed_somewhere_the_boundary_cannot_be_found_fails(self):
        """Proves the check actually runs rather than silently passing --
        an empty directory can never contain the referenced boundary."""
        manifest = WP_FIXTURE_ROOT / "ci" / "manifest" / "WP-SCHED-001.json"
        with tempfile.TemporaryDirectory() as empty_dir:
            rc = self._run(str(manifest), "--specs-search-root", empty_dir)
            self.assertEqual(rc, 1)

    def test_manifest_with_bad_gate_hash_fails(self):
        data = json.loads((WP_FIXTURE_ROOT / "ci" / "manifest" / "WP-SCHED-001.json").read_text())
        data["gate_integrity"][0]["hash"] = "sha256:" + "0" * 64
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            bad_manifest = Path(f.name)
        try:
            rc = self._run(str(bad_manifest))
            self.assertEqual(rc, 1)
        finally:
            bad_manifest.unlink()


class LoadProjectDescriptorTest(unittest.TestCase):
    def test_valid_descriptor_loads(self):
        data = pipeline.load_project_descriptor(
            ROOT / "schemas" / "examples" / "project-descriptor.greenfield.example.json"
        )
        self.assertEqual(data["mode"], "greenfield")

    def test_invalid_descriptor_raises_with_message(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"schema_version": "1.0"}, f)  # missing everything else required
            path = Path(f.name)
        try:
            with self.assertRaises(pipeline.PipelineError):
                pipeline.load_project_descriptor(path)
        finally:
            path.unlink()


VALID_DESCRIPTOR = {
    "schema_version": "1.0",
    "project": {"name": "repro", "crate_naming_convention": "^repro-[a-z]+"},
    "mode": "greenfield",
    "crates": [
        {"crate_dir": "crate_a", "contracts_crate": "contracts", "specs_search_root": "crate_a/specs"}
    ],
    "verifier_policy": {"default": "creusot"},
    "compatibility_policy": {"reliance_policy_path": "docs/reliance-policy.md"},
    "write_set": {"allowed_roots": [], "protected_roots": []},
    "gate_integrity": [],
    "review": {"reviewer": "repro", "reviewed_at": "2026-08-27"},
}

REALIZATION = {
    "requirement": "required",
    "config_scope": {"target": "x86_64-unknown-linux-gnu", "features": ["default"], "cfg": []},
}


class CmdApproveIntegrationTest(unittest.TestCase):
    """Requested directly by the third review pass: real cmd_approve
    integration tests for G1a failure, G2+ failure, and a mislocated
    boundary -- the actual reproduction they used (a boundary under a
    typo'd _boundary/ directory, singular, was approved with zero gating
    because the dispatcher fell back to SKIP_VALIDATION for anything it
    didn't recognize)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        (self.workspace / "crate_a" / "specs" / "_boundaries").mkdir(parents=True)
        (self.workspace / "crate_a" / "specs" / "_interactions").mkdir(parents=True)
        (self.workspace / "crate_a" / "specs" / "_exemptions").mkdir(parents=True)
        (self.workspace / "crate_a" / "specs" / "_protocol_debt").mkdir(parents=True)
        # Workspace-level, deliberately NOT under crate_a/ -- proves the
        # dispatcher recognizes it without any crate membership (chainlink
        # #20: conflict-resolution records aren't crate-scoped at all).
        (self.workspace / "evidence").mkdir(parents=True)
        (self.workspace / "specs" / "_conflicts").mkdir(parents=True)
        self.descriptor_path = self.workspace / "project-descriptor.json"
        self.descriptor_path.write_text(json.dumps(VALID_DESCRIPTOR))

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *args):
        return pipeline.main(
            ["--workspace", str(self.workspace), "--descriptor", str(self.descriptor_path), *args]
        )

    def test_g1a_failure_is_refused_and_writes_nothing(self):
        target = self.workspace / "crate_a" / "specs" / "_boundaries" / "scheduler_dispatch__to__task_queue_pop_ready.json"
        target.with_suffix(".json.draft").write_text(
            json.dumps({"boundary_id": "broken", "review": {"reviewer": "alice", "reviewed_at": "2026-08-26"}})
        )
        rc = self._run("approve", str(target), "--reviewer", "alice")
        self.assertEqual(rc, 1)
        self.assertFalse(target.exists())

    def test_g2_plus_failure_is_refused_and_writes_nothing(self):
        target = self.workspace / "crate_a" / "specs" / "_boundaries" / "scheduler_dispatch__to__task_queue_pop_ready.json"
        target.with_suffix(".json.draft").write_text(json.dumps({
            "schema_version": "1.0",
            "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "callee_guarantees": ["TaskQueue.A006"],
        }))
        rc = self._run("approve", str(target), "--reviewer", "alice")
        self.assertEqual(rc, 1)
        self.assertFalse(target.exists())

    def test_mislocated_boundary_under_typo_directory_is_refused_not_silently_approved(self):
        """The actual reproduction: a boundary artifact placed under
        _boundary/ (singular -- missing the trailing s) used to match
        nothing in the dispatcher, fall back to SKIP_VALIDATION, and
        promote successfully with zero gating. Must now be refused."""
        typo_dir = self.workspace / "crate_a" / "specs" / "_boundary"
        typo_dir.mkdir(parents=True)
        target = typo_dir / "scheduler_dispatch__to__task_queue_pop_ready.json"
        target.with_suffix(".json.draft").write_text(
            json.dumps({"boundary_id": "broken", "review": {"reviewer": "alice", "reviewed_at": "2026-08-26"}})
        )
        rc = self._run("approve", str(target), "--reviewer", "alice")
        self.assertEqual(rc, 1)
        self.assertFalse(target.exists(), "mislocated boundary was promoted with no gate -- the exact bug reported")

    _VALID_PAYLOAD = {
        "schema_version": "1.0",
        "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
        "caller": {"concept": "Scheduler", "method": "dispatch"},
        "callee": {"concept": "TaskQueue", "method": "pop_ready"},
        "callee_guarantees": ["TaskQueue.C003"],
    }

    def test_valid_boundary_under_wrong_parent_directory_is_refused(self):
        """Fourth review pass: "_boundaries" in target.parts matched a
        _boundaries component ANYWHERE in the path, not the crate's actual
        declared layout -- crate_a/not_specs/_boundaries/x.json (not even
        under specs/) matched and promoted a structurally valid boundary."""
        wrong_root = self.workspace / "crate_a" / "not_specs" / "_boundaries"
        wrong_root.mkdir(parents=True)
        target = wrong_root / "scheduler_dispatch__to__task_queue_pop_ready.json"
        target.with_suffix(".json.draft").write_text(json.dumps(self._VALID_PAYLOAD))
        rc = self._run("approve", str(target), "--reviewer", "alice")
        self.assertEqual(rc, 1)
        self.assertFalse(target.exists(), "boundary under the wrong parent directory was promoted")

    def test_valid_boundary_under_nested_boundaries_directory_is_refused(self):
        """specs/nested/_boundaries/ -- G1b's own 'flat' check doesn't
        catch this either, since the file DOES sit directly inside a
        directory literally named _boundaries; it has no opinion on where
        that _boundaries directory itself sits relative to specs/."""
        nested_root = self.workspace / "crate_a" / "specs" / "nested" / "_boundaries"
        nested_root.mkdir(parents=True)
        target = nested_root / "scheduler_dispatch__to__task_queue_pop_ready.json"
        target.with_suffix(".json.draft").write_text(json.dumps(self._VALID_PAYLOAD))
        rc = self._run("approve", str(target), "--reviewer", "alice")
        self.assertEqual(rc, 1)
        self.assertFalse(target.exists(), "boundary under a nested _boundaries directory was promoted")

    def test_valid_draft_is_approved(self):
        target = self.workspace / "crate_a" / "specs" / "_boundaries" / "scheduler_dispatch__to__task_queue_pop_ready.json"
        target.with_suffix(".json.draft").write_text(json.dumps({
            "schema_version": "1.0",
            "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "callee_guarantees": ["TaskQueue.C003"],
        }))
        rc = self._run("approve", str(target), "--reviewer", "alice", "--reviewed-at", "2026-08-27")
        self.assertEqual(rc, 0)
        self.assertTrue(target.exists())

    def test_approve_valid_interaction_succeeds(self):
        """#16: _select_validate_fn's dispatcher extended to recognize
        interaction targets too, not just boundary contracts -- exercised
        end to end through approve, not just validate_interaction.py
        directly. Includes a reliance since G2++ (#17) requires at least
        one for a boundary-required edge."""
        self._write_promoted_boundary()
        target = self.workspace / "crate_a" / "specs" / "_interactions" / "I-SCHED-TQ-001.json"
        target.with_suffix(".json.draft").write_text(json.dumps({
            "schema_version": "1.0",
            "interaction_id": "I-SCHED-TQ-001",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "edge_class": ["stateful"],
            "eligibility": "boundary-required",
            "rationale": "dispatch relies on pop_ready's return discipline",
            "reliances": [
                {
                    "obligation_id": "TaskQueue.C003",
                    "required_assurance": {
                        "required_claims": ["postcondition-holds"],
                        "accepted_evidence_kinds": ["creusot-deductive-check"],
                        "minimum_scope": {"input_domain": "queue_len_le_8"},
                        "trust_policy": {"assumptions_allowed": []},
                    },
                }
            ],
            "protocol_class": "pairwise",
            "realization": REALIZATION,
        }))
        rc = self._run("approve", str(target), "--reviewer", "alice")
        self.assertEqual(rc, 0)
        self.assertTrue(target.exists())

    def test_approve_boundary_required_interaction_without_reliances_is_refused(self):
        """G2++ (#17), exercised end to end through approve: a
        boundary-required interaction declaring no reliances must be
        refused, not silently promoted. realization is present so the
        refusal is unambiguously attributable to the missing reliance,
        not an incidental #18 schema violation."""
        target = self.workspace / "crate_a" / "specs" / "_interactions" / "I-SCHED-TQ-001.json"
        target.with_suffix(".json.draft").write_text(json.dumps({
            "schema_version": "1.0",
            "interaction_id": "I-SCHED-TQ-001",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "edge_class": ["stateful"],
            "eligibility": "boundary-required",
            "rationale": "dispatch relies on pop_ready's return discipline",
            "protocol_class": "pairwise",
            "realization": REALIZATION,
        }))
        rc = self._run("approve", str(target), "--reviewer", "alice")
        self.assertEqual(rc, 1)
        self.assertFalse(target.exists())

    def test_approve_interaction_with_wrong_computed_eligibility_is_refused(self):
        target = self.workspace / "crate_a" / "specs" / "_interactions" / "I-SCHED-TQ-001.json"
        target.with_suffix(".json.draft").write_text(json.dumps({
            "schema_version": "1.0",
            "interaction_id": "I-SCHED-TQ-001",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "edge_class": ["stateful"],
            "eligibility": "ignore",
            "rationale": "dispatch relies on pop_ready's return discipline",
            "protocol_class": "pairwise",
            "realization": REALIZATION,
        }))
        rc = self._run("approve", str(target), "--reviewer", "alice")
        self.assertEqual(rc, 1)
        self.assertFalse(target.exists())

    def test_approve_interaction_with_reliances_succeeds(self):
        """#17: reliances[].required_assurance, exercised end to end
        through approve, not just the schema/G1b unit tests."""
        self._write_promoted_boundary()
        target = self.workspace / "crate_a" / "specs" / "_interactions" / "I-SCHED-TQ-001.json"
        target.with_suffix(".json.draft").write_text(json.dumps({
            "schema_version": "1.0",
            "interaction_id": "I-SCHED-TQ-001",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "edge_class": ["stateful"],
            "eligibility": "boundary-required",
            "rationale": "dispatch relies on pop_ready's return discipline",
            "reliances": [
                {
                    "obligation_id": "TaskQueue.C003",
                    "required_assurance": {
                        "required_claims": ["postcondition-holds"],
                        "accepted_evidence_kinds": ["creusot-deductive-check"],
                        "minimum_scope": {"input_domain": "queue_len_le_8"},
                        "trust_policy": {"assumptions_allowed": []},
                    },
                }
            ],
            "protocol_class": "pairwise",
            "realization": REALIZATION,
        }))
        rc = self._run("approve", str(target), "--reviewer", "alice")
        self.assertEqual(rc, 0)
        self.assertTrue(target.exists())

    def test_approve_boundary_required_interaction_with_no_coverage_is_refused(self):
        """Chainlink #46 (R2), end to end through approve: no covering
        boundary contract or reviewed exemption anywhere in the crate --
        deliberately does NOT call self._write_promoted_boundary()."""
        target = self.workspace / "crate_a" / "specs" / "_interactions" / "I-SCHED-TQ-001.json"
        target.with_suffix(".json.draft").write_text(json.dumps({
            "schema_version": "1.0",
            "interaction_id": "I-SCHED-TQ-001",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "edge_class": ["stateful"],
            "eligibility": "boundary-required",
            "rationale": "dispatch relies on pop_ready's return discipline",
            "reliances": [
                {
                    "obligation_id": "TaskQueue.C003",
                    "required_assurance": {
                        "required_claims": ["postcondition-holds"],
                        "accepted_evidence_kinds": ["creusot-deductive-check"],
                        "minimum_scope": {"input_domain": "queue_len_le_8"},
                        "trust_policy": {"assumptions_allowed": []},
                    },
                }
            ],
            "protocol_class": "pairwise",
            "realization": REALIZATION,
        }))
        rc = self._run("approve", str(target), "--reviewer", "alice")
        self.assertEqual(rc, 1)
        self.assertFalse(target.exists())

    def test_approve_interaction_with_type_split_violation_is_refused(self):
        """A verification-method value in required_claims must be
        refused by approve, not just by the standalone validator."""
        target = self.workspace / "crate_a" / "specs" / "_interactions" / "I-SCHED-TQ-001.json"
        target.with_suffix(".json.draft").write_text(json.dumps({
            "schema_version": "1.0",
            "interaction_id": "I-SCHED-TQ-001",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "edge_class": ["stateful"],
            "eligibility": "boundary-required",
            "rationale": "dispatch relies on pop_ready's return discipline",
            "reliances": [
                {
                    "obligation_id": "TaskQueue.C003",
                    "required_assurance": {
                        "required_claims": ["kani-bounded-model-check"],
                        "accepted_evidence_kinds": ["creusot-deductive-check"],
                        "minimum_scope": {"input_domain": "queue_len_le_8"},
                        "trust_policy": {"assumptions_allowed": []},
                    },
                }
            ],
            "protocol_class": "pairwise",
            "realization": REALIZATION,
        }))
        rc = self._run("approve", str(target), "--reviewer", "alice")
        self.assertEqual(rc, 1)
        self.assertFalse(target.exists())

    def test_approve_interaction_without_realization_is_refused(self):
        """#18: realization.requirement + config_scope is required on
        every interaction (unlike reliances, unconditionally -- plan.md's
        issue text is 'each interaction edge declares', not conditioned
        on eligibility). Exercised end to end through approve."""
        target = self.workspace / "crate_a" / "specs" / "_interactions" / "I-SCHED-TQ-001.json"
        target.with_suffix(".json.draft").write_text(json.dumps({
            "schema_version": "1.0",
            "interaction_id": "I-SCHED-TQ-001",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "edge_class": ["pure-data-type-reference"],
            "eligibility": "inform",
            "rationale": "dispatch relies on pop_ready's return discipline",
        }))
        rc = self._run("approve", str(target), "--reviewer", "alice")
        self.assertEqual(rc, 1)
        self.assertFalse(target.exists())

    def test_approve_valid_exemption_succeeds(self):
        """#16: same dispatcher extension, for exemption targets."""
        self._write_real_interaction("I-SCHED-TQ-002", "pairwise")
        target = self.workspace / "crate_a" / "specs" / "_exemptions" / "I-SCHED-TQ-002.json"
        target.with_suffix(".json.draft").write_text(json.dumps({
            "schema_version": "1.0",
            "interaction_id": "I-SCHED-TQ-002",
            "rationale": "Prototype scaffolding boundary, tracked for removal (chainlink:#41)",
        }))
        rc = self._run("approve", str(target), "--reviewer", "alice")
        self.assertEqual(rc, 0)
        self.assertTrue(target.exists())

    def _write_promoted_boundary(self) -> None:
        """A real, already-promoted (not .draft) boundary contract
        covering Scheduler.dispatch -> TaskQueue.pop_ready -- satisfies
        R2 (chainlink #46) for the boundary-required interaction fixtures
        this class reuses across many tests, all of which declare that
        same edge."""
        target = (
            self.workspace / "crate_a" / "specs" / "_boundaries"
            / "scheduler_dispatch__to__task_queue_pop_ready.json"
        )
        target.write_text(json.dumps({
            "schema_version": "1.0",
            "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "callee_guarantees": ["TaskQueue.C003"],
            "review": {"reviewer": "alice", "reviewed_at": "2026-08-30"},
        }))

    def _write_real_interaction(self, interaction_id: str, protocol_class: str) -> None:
        """A real, already-promoted interaction file -- not a .draft --
        for protocol-debt cross-reference tests to resolve against.
        Mirrors how a debt record is normally filed against an
        interaction that already exists on disk."""
        target = self.workspace / "crate_a" / "specs" / "_interactions" / f"{interaction_id}.json"
        target.write_text(json.dumps({
            "schema_version": "1.0",
            "interaction_id": interaction_id,
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "edge_class": ["stateful"],
            "eligibility": "boundary-required",
            "rationale": "dispatch relies on pop_ready's return discipline",
            "reliances": [
                {
                    "obligation_id": "TaskQueue.C003",
                    "required_assurance": {
                        "required_claims": ["postcondition-holds"],
                        "accepted_evidence_kinds": ["creusot-deductive-check"],
                        "minimum_scope": {"input_domain": "queue_len_le_8"},
                        "trust_policy": {"assumptions_allowed": []},
                    },
                }
            ],
            "protocol_class": protocol_class,
            "realization": REALIZATION,
            "review": {"reviewer": "alice", "reviewed_at": "2026-08-30"},
        }))

    def _stage_non_pairwise_pair(self, interaction_id: str = "I-SCHED-TQ-006") -> tuple[Path, Path]:
        interaction_target = self.workspace / "crate_a" / "specs" / "_interactions" / f"{interaction_id}.json"
        debt_target = self.workspace / "crate_a" / "specs" / "_protocol_debt" / f"{interaction_id}.json"
        interaction_target.with_suffix(".json.draft").write_text(json.dumps({
            "schema_version": "1.0",
            "interaction_id": interaction_id,
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "edge_class": ["stateful"],
            "eligibility": "boundary-required",
            "rationale": "dispatch relies on pop_ready's return discipline",
            "reliances": [
                {
                    "obligation_id": "TaskQueue.C003",
                    "required_assurance": {
                        "required_claims": ["postcondition-holds"],
                        "accepted_evidence_kinds": ["creusot-deductive-check"],
                        "minimum_scope": {"input_domain": "queue_len_le_8"},
                        "trust_policy": {"assumptions_allowed": []},
                    },
                }
            ],
            "protocol_class": "non-pairwise",
            "realization": REALIZATION,
        }))
        debt_target.with_suffix(".json.draft").write_text(json.dumps({
            "schema_version": "1.0",
            "interaction_id": interaction_id,
            "rationale": "Multi-step handshake protocol, not yet modeled",
            "no_promoted_obligation_depends_on_protocol": True,
            "no_work_package_touches_its_path": True,
            "no_release_claim_includes_it": True,
            "tracking_issue": "chainlink:#99",
        }))
        return interaction_target, debt_target

    def test_approve_pair_bootstraps_non_pairwise_interaction_and_debt_atomically(self):
        """The two new artifacts validate against each other in one
        transaction; neither side must already be approved."""
        self._write_promoted_boundary()
        interaction_target, debt_target = self._stage_non_pairwise_pair()
        rc = self._run(
            "approve-pair",
            str(interaction_target),
            str(debt_target),
            "--reviewer",
            "alice",
            "--reviewed-at",
            "2026-09-01",
        )
        self.assertEqual(rc, 0)
        self.assertTrue(interaction_target.exists())
        self.assertTrue(debt_target.exists())
        self.assertFalse(interaction_target.with_suffix(".json.draft").exists())
        self.assertFalse(debt_target.with_suffix(".json.draft").exists())
        self.assertEqual(json.loads(interaction_target.read_text())["review"]["reviewer"], "alice")
        self.assertEqual(json.loads(debt_target.read_text())["review"]["reviewer"], "alice")

    def test_approve_pair_refuses_the_whole_pair_when_one_draft_is_invalid(self):
        interaction_target, debt_target = self._stage_non_pairwise_pair()
        debt_draft = debt_target.with_suffix(".json.draft")
        debt_data = json.loads(debt_draft.read_text())
        debt_data["no_work_package_touches_its_path"] = False
        debt_draft.write_text(json.dumps(debt_data))

        rc = self._run("approve-pair", str(interaction_target), str(debt_target), "--reviewer", "alice")
        self.assertEqual(rc, 1)
        self.assertFalse(interaction_target.exists())
        self.assertFalse(debt_target.exists())
        self.assertTrue(interaction_target.with_suffix(".json.draft").exists())
        self.assertTrue(debt_target.with_suffix(".json.draft").exists())

    def test_approve_pair_refuses_mismatched_body_ids_before_granting_coverage(self):
        """A debt record for an existing I-B must not be able to cover a
        new I-A merely because both are supplied to the pair command."""
        self._write_real_interaction("I-B", "non-pairwise")
        interaction_target, unused_debt_target = self._stage_non_pairwise_pair("I-A")
        unused_debt_target.with_suffix(".json.draft").unlink()
        debt_target = self.workspace / "crate_a" / "specs" / "_protocol_debt" / "I-B.json"
        debt_target.with_suffix(".json.draft").write_text(json.dumps({
            "schema_version": "1.0",
            "interaction_id": "I-B",
            "rationale": "Multi-step handshake protocol, not yet modeled",
            "no_promoted_obligation_depends_on_protocol": True,
            "no_work_package_touches_its_path": True,
            "no_release_claim_includes_it": True,
            "tracking_issue": "chainlink:#99",
        }))

        rc = self._run("approve-pair", str(interaction_target), str(debt_target), "--reviewer", "alice")
        self.assertEqual(rc, 1)
        self.assertFalse(interaction_target.exists())
        self.assertFalse(debt_target.exists())
        self.assertTrue(interaction_target.with_suffix(".json.draft").exists())
        self.assertTrue(debt_target.with_suffix(".json.draft").exists())

    def _stage_interaction_exemption_pair(self, interaction_id: str = "I-SCHED-TQ-007") -> tuple[Path, Path]:
        """A boundary-required interaction with NO covering boundary
        contract anywhere -- its only possible R2 coverage is the paired
        exemption. Deliberately does NOT call self._write_promoted_boundary()."""
        interaction_target = self.workspace / "crate_a" / "specs" / "_interactions" / f"{interaction_id}.json"
        exemption_target = self.workspace / "crate_a" / "specs" / "_exemptions" / f"{interaction_id}.json"
        interaction_target.with_suffix(".json.draft").write_text(json.dumps({
            "schema_version": "1.0",
            "interaction_id": interaction_id,
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "edge_class": ["stateful"],
            "eligibility": "boundary-required",
            "rationale": "dispatch relies on pop_ready's return discipline",
            "reliances": [
                {
                    "obligation_id": "TaskQueue.C003",
                    "required_assurance": {
                        "required_claims": ["postcondition-holds"],
                        "accepted_evidence_kinds": ["creusot-deductive-check"],
                        "minimum_scope": {"input_domain": "queue_len_le_8"},
                        "trust_policy": {"assumptions_allowed": []},
                    },
                }
            ],
            "protocol_class": "pairwise",
            "realization": REALIZATION,
        }))
        exemption_target.with_suffix(".json.draft").write_text(json.dumps({
            "schema_version": "1.0",
            "interaction_id": interaction_id,
            "rationale": "Prototype scaffolding boundary, tracked for removal (chainlink:#46)",
        }))
        return interaction_target, exemption_target

    def test_approve_single_interaction_is_refused_when_only_a_reviewed_exemption_would_cover_it(self):
        """External review's exact repro, order 1: R2 requires an
        already-promoted exemption, so a lone interaction approval fails
        closed even though the eventual plan is to cover it by
        exemption, not a boundary."""
        interaction_target, exemption_target = self._stage_interaction_exemption_pair()
        rc = self._run("approve", str(interaction_target), "--reviewer", "alice")
        self.assertEqual(rc, 1)
        self.assertFalse(interaction_target.exists())

    def test_approve_single_exemption_is_refused_without_an_already_promoted_interaction(self):
        """External review's exact repro, order 2: the exemption's own
        cross-reference only ever consults promoted interactions, never a
        draft, so a lone exemption approval fails closed as a dangling
        reference even though the interaction it names is staged right
        alongside it."""
        interaction_target, exemption_target = self._stage_interaction_exemption_pair()
        rc = self._run("approve", str(exemption_target), "--reviewer", "alice")
        self.assertEqual(rc, 1)
        self.assertFalse(exemption_target.exists())

    def test_approve_exemption_pair_bootstraps_interaction_and_exemption_atomically(self):
        """The fix: approve-exemption-pair breaks the cycle the two tests
        above reproduce -- neither artifact is promoted until both
        validate against a transaction view containing the other."""
        interaction_target, exemption_target = self._stage_interaction_exemption_pair()
        rc = self._run(
            "approve-exemption-pair", str(interaction_target), str(exemption_target), "--reviewer", "alice",
            "--reviewed-at", "2026-09-03",
        )
        self.assertEqual(rc, 0)
        self.assertTrue(interaction_target.exists())
        self.assertTrue(exemption_target.exists())
        self.assertFalse(interaction_target.with_suffix(".json.draft").exists())
        self.assertFalse(exemption_target.with_suffix(".json.draft").exists())
        self.assertEqual(json.loads(interaction_target.read_text())["review"]["reviewer"], "alice")
        self.assertEqual(json.loads(exemption_target.read_text())["review"]["reviewer"], "alice")

    def test_approve_exemption_pair_refuses_the_whole_pair_when_the_exemption_draft_is_invalid(self):
        interaction_target, exemption_target = self._stage_interaction_exemption_pair()
        exemption_draft = exemption_target.with_suffix(".json.draft")
        exemption_data = json.loads(exemption_draft.read_text())
        del exemption_data["rationale"]
        exemption_draft.write_text(json.dumps(exemption_data))

        rc = self._run(
            "approve-exemption-pair", str(interaction_target), str(exemption_target), "--reviewer", "alice"
        )
        self.assertEqual(rc, 1)
        self.assertFalse(interaction_target.exists())
        self.assertFalse(exemption_target.exists())
        self.assertTrue(interaction_target.with_suffix(".json.draft").exists())
        self.assertTrue(exemption_target.with_suffix(".json.draft").exists())

    def test_approve_exemption_pair_refuses_mismatched_body_ids_before_granting_coverage(self):
        """An exemption for an existing I-B must not be able to cover a
        new I-A merely because both are supplied to the pair command."""
        self._write_real_interaction("I-B", "pairwise")
        interaction_target, unused_exemption_target = self._stage_interaction_exemption_pair("I-A")
        unused_exemption_target.with_suffix(".json.draft").unlink()
        exemption_target = self.workspace / "crate_a" / "specs" / "_exemptions" / "I-B.json"
        exemption_target.with_suffix(".json.draft").write_text(json.dumps({
            "schema_version": "1.0",
            "interaction_id": "I-B",
            "rationale": "Prototype scaffolding boundary, tracked for removal (chainlink:#46)",
        }))

        rc = self._run(
            "approve-exemption-pair", str(interaction_target), str(exemption_target), "--reviewer", "alice"
        )
        self.assertEqual(rc, 1)
        self.assertFalse(interaction_target.exists())
        self.assertFalse(exemption_target.exists())
        self.assertTrue(interaction_target.with_suffix(".json.draft").exists())
        self.assertTrue(exemption_target.with_suffix(".json.draft").exists())

    def test_approve_valid_protocol_debt_succeeds(self):
        """#19: _select_validate_fn's dispatcher extended to recognize
        protocol-debt targets too, exercised end to end through approve.
        A real, already-promoted non-pairwise interaction exists for it
        to resolve against."""
        self._write_real_interaction("I-SCHED-TQ-003", "non-pairwise")
        target = self.workspace / "crate_a" / "specs" / "_protocol_debt" / "I-SCHED-TQ-003.json"
        target.with_suffix(".json.draft").write_text(json.dumps({
            "schema_version": "1.0",
            "interaction_id": "I-SCHED-TQ-003",
            "rationale": "Multi-step handshake protocol, not yet modeled",
            "no_promoted_obligation_depends_on_protocol": True,
            "no_work_package_touches_its_path": True,
            "no_release_claim_includes_it": True,
            "tracking_issue": "chainlink:#99",
        }))
        rc = self._run("approve", str(target), "--reviewer", "alice")
        self.assertEqual(rc, 0)
        self.assertTrue(target.exists())

    def test_approve_protocol_debt_for_nonexistent_interaction_is_refused(self):
        """External review, high severity: a debt record naming a
        nonexistent interaction previously approved with zero findings.
        No interaction file is written here at all."""
        target = self.workspace / "crate_a" / "specs" / "_protocol_debt" / "I-DOES-NOT-EXIST.json"
        target.with_suffix(".json.draft").write_text(json.dumps({
            "schema_version": "1.0",
            "interaction_id": "I-DOES-NOT-EXIST",
            "rationale": "Multi-step handshake protocol, not yet modeled",
            "no_promoted_obligation_depends_on_protocol": True,
            "no_work_package_touches_its_path": True,
            "no_release_claim_includes_it": True,
            "tracking_issue": "chainlink:#99",
        }))
        rc = self._run("approve", str(target), "--reviewer", "alice")
        self.assertEqual(rc, 1)
        self.assertFalse(target.exists())

    def test_approve_protocol_debt_for_schema_invalid_interaction_is_refused(self):
        """A JSON file with an interaction_id is not enough to satisfy the
        debt cross-reference; the referenced interaction must pass its full
        schema and approval-state checks."""
        interaction = self.workspace / "crate_a" / "specs" / "_interactions" / "I-X-001.json"
        interaction.write_text(json.dumps({"interaction_id": "I-X-001", "protocol_class": "non-pairwise"}))
        target = self.workspace / "crate_a" / "specs" / "_protocol_debt" / "I-X-001.json"
        target.with_suffix(".json.draft").write_text(json.dumps({
            "schema_version": "1.0",
            "interaction_id": "I-X-001",
            "rationale": "Multi-step handshake protocol, not yet modeled",
            "no_promoted_obligation_depends_on_protocol": True,
            "no_work_package_touches_its_path": True,
            "no_release_claim_includes_it": True,
            "tracking_issue": "chainlink:#99",
        }))
        rc = self._run("approve", str(target), "--reviewer", "alice")
        self.assertEqual(rc, 1)
        self.assertFalse(target.exists())

    def test_approve_protocol_debt_for_pairwise_interaction_is_refused(self):
        """External review, high severity: a debt record for what is
        actually a pairwise interaction previously approved with zero
        findings."""
        self._write_real_interaction("I-SCHED-TQ-004", "pairwise")
        target = self.workspace / "crate_a" / "specs" / "_protocol_debt" / "I-SCHED-TQ-004.json"
        target.with_suffix(".json.draft").write_text(json.dumps({
            "schema_version": "1.0",
            "interaction_id": "I-SCHED-TQ-004",
            "rationale": "Multi-step handshake protocol, not yet modeled",
            "no_promoted_obligation_depends_on_protocol": True,
            "no_work_package_touches_its_path": True,
            "no_release_claim_includes_it": True,
            "tracking_issue": "chainlink:#99",
        }))
        rc = self._run("approve", str(target), "--reviewer", "alice")
        self.assertEqual(rc, 1)
        self.assertFalse(target.exists())

    def test_approve_non_pairwise_interaction_with_no_debt_record_is_refused(self):
        """G15, the interaction side of the same cross-reference: a
        non-pairwise interaction with no valid protocol-debt record
        anywhere in the crate must be refused at approve time, not
        silently promoted."""
        target = self.workspace / "crate_a" / "specs" / "_interactions" / "I-SCHED-TQ-005.json"
        target.with_suffix(".json.draft").write_text(json.dumps({
            "schema_version": "1.0",
            "interaction_id": "I-SCHED-TQ-005",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "edge_class": ["stateful"],
            "eligibility": "boundary-required",
            "rationale": "dispatch relies on pop_ready's return discipline",
            "reliances": [
                {
                    "obligation_id": "TaskQueue.C003",
                    "required_assurance": {
                        "required_claims": ["postcondition-holds"],
                        "accepted_evidence_kinds": ["creusot-deductive-check"],
                        "minimum_scope": {"input_domain": "queue_len_le_8"},
                        "trust_policy": {"assumptions_allowed": []},
                    },
                }
            ],
            "protocol_class": "non-pairwise",
            "realization": REALIZATION,
        }))
        rc = self._run("approve", str(target), "--reviewer", "alice")
        self.assertEqual(rc, 1)
        self.assertFalse(target.exists())

    def test_approve_protocol_debt_with_false_attestation_is_refused(self):
        target = self.workspace / "crate_a" / "specs" / "_protocol_debt" / "I-SCHED-TQ-003.json"
        target.with_suffix(".json.draft").write_text(json.dumps({
            "schema_version": "1.0",
            "interaction_id": "I-SCHED-TQ-003",
            "rationale": "Multi-step handshake protocol, not yet modeled",
            "no_promoted_obligation_depends_on_protocol": True,
            "no_work_package_touches_its_path": False,
            "no_release_claim_includes_it": True,
            "tracking_issue": "chainlink:#99",
        }))
        rc = self._run("approve", str(target), "--reviewer", "alice")
        self.assertEqual(rc, 1)
        self.assertFalse(target.exists())

    def _write_real_evidence(self, evidence_id: str) -> None:
        (self.workspace / "evidence" / f"{evidence_id}.json").write_text(json.dumps({
            "schema_version": "1.0",
            "id": evidence_id,
            "kind": "source-artifact",
            "claim": "some proposition",
            "origin": {
                "repository": "https://example.com/repo",
                "commit": "a1b2c3d",
                "symbol": "Some::symbol",
                "path": "src/lib.rs",
                "content_hash": "sha256:" + "0" * 64,
                "line_hint": "1-10",
            },
            "semantic_disposition": "required",
            "lifecycle": "accepted",
            "confidence": "high",
            "mode": "P",
        }))

    def test_approve_valid_conflict_resolution_succeeds(self):
        """#20: conflict-resolution records are workspace-level, not
        crate-scoped, deliberately NOT under crate_a/ -- proves the
        dispatcher recognizes the target without any crate membership at
        all (external review lesson from #19's own G15: don't assume a
        single-crate_dir='.' test fixture generalizes)."""
        self._write_real_evidence("E-0143")
        self._write_real_evidence("E-0201")
        target = self.workspace / "specs" / "_conflicts" / "EC-004.json"
        target.with_suffix(".json.draft").write_text(json.dumps({
            "schema_version": "1.0",
            "conflict_id": "EC-004",
            "evidence": ["E-0143", "E-0201"],
            "status": "resolved",
            "resolution": {
                "selected_authority": "E-0201",
                "disposition_of_other": "incidental",
                "rationale": "compatibility policy: do not preserve the legacy defect",
            },
        }))
        rc = self._run("approve", str(target), "--reviewer", "alice")
        self.assertEqual(rc, 0)
        self.assertTrue(target.exists())

    def test_approve_unresolved_conflict_is_refused(self):
        """G11: 'only unresolved conflicts block' -- exercised end to end
        through approve, not just the standalone validator."""
        self._write_real_evidence("E-0143")
        self._write_real_evidence("E-0201")
        target = self.workspace / "specs" / "_conflicts" / "EC-004.json"
        target.with_suffix(".json.draft").write_text(json.dumps({
            "schema_version": "1.0",
            "conflict_id": "EC-004",
            "evidence": ["E-0143", "E-0201"],
            "status": "unresolved",
        }))
        rc = self._run("approve", str(target), "--reviewer", "alice")
        self.assertEqual(rc, 1)
        self.assertFalse(target.exists())

    def test_approve_conflict_resolution_with_dangling_evidence_is_refused(self):
        target = self.workspace / "specs" / "_conflicts" / "EC-004.json"
        target.with_suffix(".json.draft").write_text(json.dumps({
            "schema_version": "1.0",
            "conflict_id": "EC-004",
            "evidence": ["E-0143", "E-0201"],
            "status": "resolved",
            "resolution": {
                "selected_authority": "E-0201",
                "disposition_of_other": "incidental",
                "rationale": "compatibility policy: do not preserve the legacy defect",
            },
        }))
        rc = self._run("approve", str(target), "--reviewer", "alice")
        self.assertEqual(rc, 1)
        self.assertFalse(target.exists())

    def test_approve_refuses_non_json_interaction_target(self):
        """Fifth review pass: a schema-valid interaction approved as
        *.yaml matched the dispatcher by directory alone and was written
        straight through to a non-.json path. Reproduced directly with
        real JSON content saved under a .yaml-suffixed target."""
        target = self.workspace / "crate_a" / "specs" / "_interactions" / "I-SCHED-TQ-001.yaml"
        target.with_suffix(".yaml.draft").write_text(json.dumps({
            "schema_version": "1.0",
            "interaction_id": "I-SCHED-TQ-001",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "edge_class": ["stateful"],
            "eligibility": "boundary-required",
            "rationale": "dispatch relies on pop_ready's return discipline",
        }))
        rc = self._run("approve", str(target), "--reviewer", "alice")
        self.assertEqual(rc, 1)
        self.assertFalse(target.exists(), "a non-.json target was approved and written through")


class TargetContainmentTest(unittest.TestCase):
    """Review finding (round 2, medium severity): draft/approve accepted
    args.target verbatim, with no check it belonged to the workspace or
    any declared crate at all."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        (self.workspace / "crate_a").mkdir()
        self.descriptor = {
            "crates": [
                {"crate_dir": "crate_a", "contracts_crate": "contracts", "specs_search_root": "crate_a/specs"}
            ]
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_target_inside_declared_crate_is_accepted(self):
        target = self.workspace / "crate_a" / "specs" / "_boundaries" / "a__to__b.json"
        pipeline._require_target_in_workspace(target, self.workspace, self.descriptor)  # no raise

    def test_target_outside_any_declared_crate_is_refused(self):
        target = self.workspace / "crate_b" / "specs" / "_boundaries" / "a__to__b.json"
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline._require_target_in_workspace(target, self.workspace, self.descriptor)
        self.assertIn("does not belong to any crate", str(ctx.exception))

    def test_target_outside_the_workspace_entirely_is_refused(self):
        outside = self.workspace.parent / "definitely_not_the_workspace" / "x.json"
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline._require_target_in_workspace(outside, self.workspace, self.descriptor)
        self.assertIn("outside the workspace", str(ctx.exception))

    def test_path_traversal_out_of_workspace_is_refused(self):
        traversal = self.workspace / "crate_a" / ".." / ".." / "etc" / "passwd"
        with self.assertRaises(pipeline.PipelineError):
            pipeline._require_target_in_workspace(traversal, self.workspace, self.descriptor)

    def test_workspace_level_conflict_resolution_target_is_accepted(self):
        """Not under crate_a/ at all -- proves the workspace-level
        recognition, not just crate membership."""
        target = self.workspace / "specs" / "_conflicts" / "EC-004.json"
        pipeline._require_target_in_workspace(target, self.workspace, self.descriptor)  # no raise

    def test_workspace_level_evidence_target_is_accepted(self):
        """External review, medium severity: evidence/ was missing from
        this same workspace-level recognition -- draft refused the
        canonical evidence/E-0001.json target with a real (non-'.')
        crate_dir. Reproduced directly before fixing."""
        target = self.workspace / "evidence" / "E-0001.json"
        pipeline._require_target_in_workspace(target, self.workspace, self.descriptor)  # no raise


if __name__ == "__main__":
    unittest.main()
