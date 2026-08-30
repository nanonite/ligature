import argparse
import json
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


if __name__ == "__main__":
    unittest.main()
