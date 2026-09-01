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

    def test_artifact_under_mislocated_directory_is_not_scanned(self):
        """External review, medium severity: the scan used to search for
        any directory named _interactions anywhere in the crate, not just
        <crate_dir>/specs/_interactions. The fixture here has NO
        specs/_interactions at all -- only a computed-eligibility-broken
        artifact under not_specs/_interactions/. Before the fix this was
        found and failed; anchored to the exact canonical directory, it's
        never examined, and a crate with no interactions yet is OK."""
        self.assertEqual(self._run(INTERACTION_FIXTURES / "mislocated"), 0)


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

    def test_artifact_under_mislocated_directory_is_not_scanned(self):
        """Mirrors CmdValidateInteractionIntegrationTest's equivalent --
        the fixture has no specs/_exemptions at all, only a
        naming-mismatched artifact under not_specs/_exemptions/."""
        self.assertEqual(self._run(EXEMPTION_FIXTURES / "mislocated"), 0)


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
        end to end through approve, not just validate_interaction.py directly."""
        target = self.workspace / "crate_a" / "specs" / "_interactions" / "I-SCHED-TQ-001.json"
        target.with_suffix(".json.draft").write_text(json.dumps({
            "schema_version": "1.0",
            "interaction_id": "I-SCHED-TQ-001",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "edge_class": ["stateful"],
            "eligibility": "boundary-required",
            "rationale": "dispatch relies on pop_ready's return discipline",
        }))
        rc = self._run("approve", str(target), "--reviewer", "alice")
        self.assertEqual(rc, 0)
        self.assertTrue(target.exists())

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
        }))
        rc = self._run("approve", str(target), "--reviewer", "alice")
        self.assertEqual(rc, 1)
        self.assertFalse(target.exists())

    def test_approve_valid_exemption_succeeds(self):
        """#16: same dispatcher extension, for exemption targets."""
        target = self.workspace / "crate_a" / "specs" / "_exemptions" / "I-SCHED-TQ-002.json"
        target.with_suffix(".json.draft").write_text(json.dumps({
            "schema_version": "1.0",
            "interaction_id": "I-SCHED-TQ-002",
            "rationale": "Prototype scaffolding boundary, tracked for removal (chainlink:#41)",
        }))
        rc = self._run("approve", str(target), "--reviewer", "alice")
        self.assertEqual(rc, 0)
        self.assertTrue(target.exists())

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


if __name__ == "__main__":
    unittest.main()
