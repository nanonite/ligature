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


if __name__ == "__main__":
    unittest.main()
