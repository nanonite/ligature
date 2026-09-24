"""Out-of-checkout acceptance for the packaged artifact (chainlink #57).

This is the only real proof that the `importlib.resources` migration removed
the source-checkout dependency rather than accidentally falling back to it
because the checkout happened to be present. It builds `ligature.pyz`,
copies ONLY the artifact into a temporary directory outside this repository,
and runs it there with `python3 -I` (isolated mode: no PYTHONPATH, no user
site) and a cwd outside the checkout -- including the `init --mode port`
path #60 needs. A source fallback would show up immediately as the artifact
failing to resolve its own schemas/prompts.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import build_zipapp  # noqa: E402
from schema_utils import make_validator  # noqa: E402

DESCRIPTOR_SCHEMA = json.loads((ROOT / "schemas" / "project-descriptor.schema.json").read_text())


# A revision from before init grew the re-pin guard (#65): building a
# worktree at this revision yields a genuinely different-hash artifact from
# the current working tree, without depending on a dirty-tree difference (an
# untracked file would not change content_hash at all).
OLD_COMMIT = "3c44ef7"


class ZipappOutOfCheckoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._build_tmp = tempfile.TemporaryDirectory()
        cls.build_result = build_zipapp.build(Path(cls._build_tmp.name))
        cls._outside_tmp = tempfile.TemporaryDirectory()
        cls.outside = Path(cls._outside_tmp.name).resolve()
        cls.artifact = cls.outside / "ligature.pyz"
        shutil.copyfile(cls.build_result["artifact"], cls.artifact)

    @classmethod
    def tearDownClass(cls):
        cls._build_tmp.cleanup()
        cls._outside_tmp.cleanup()

    def setUp(self):
        self._workspace_tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._workspace_tmp.name).resolve()

    def tearDown(self):
        self._workspace_tmp.cleanup()

    def run_artifact(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-I", str(self.artifact), *args],
            cwd=self.outside,
            capture_output=True,
            text=True,
        )

    def init_port(self) -> subprocess.CompletedProcess:
        return self.run_artifact("--workspace", str(self.workspace), "init", "--mode", "port", "--name", "myport")

    def test_version_verify_runs_without_the_source_tree(self):
        proc = self.run_artifact("version", "--verify")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("attested: true", proc.stdout)
        self.assertIn(self.build_result["content_hash"], proc.stdout)
        # resources were located inside the copied archive, not the checkout
        self.assertIn(str(self.artifact), proc.stdout)
        self.assertNotIn(str(ROOT), proc.stdout)

    def test_doctor_verifies_before_init(self):
        proc = self.run_artifact("--workspace", str(self.workspace), "doctor")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("attested: true", proc.stdout)

    def test_port_init_and_status_from_the_artifact_alone(self):
        proc = self.init_port()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("installation: current", proc.stdout)

        descriptor = json.loads((self.workspace / "project-descriptor.json").read_text())
        self.assertEqual(descriptor["mode"], "port")
        self.assertIn("port_source", descriptor)
        errors = list(make_validator(DESCRIPTOR_SCHEMA).iter_errors(descriptor))
        self.assertEqual(errors, [], [e.message for e in errors])
        self.assertTrue((self.workspace / ".codex" / "skills" / "ligature" / "SKILL.md").is_file())
        self.assertTrue((self.workspace / ".ligature" / "prompts" / "stage-0-evidence-intake.md").is_file())

        status = self.run_artifact("--workspace", str(self.workspace), "status", "--json")
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        document = json.loads(status.stdout)
        self.assertEqual(document["gate_integrity"]["state"], "pinned")
        self.assertEqual(document["binary_identity"]["verified"], "true")
        self.assertEqual(document["binary_identity"]["content_hash"], self.build_result["content_hash"])

    def test_init_records_the_artifact_as_the_pinned_adjudicator(self):
        self.assertEqual(self.init_port().returncode, 0)
        manifest = json.loads((self.workspace / "ci" / "manifest" / "installation.json").read_text())
        self.assertEqual(manifest["adjudicator"]["kind"], "zipapp")
        self.assertEqual(manifest["adjudicator"]["content_hash"], self.build_result["content_hash"])
        self.assertEqual(manifest["gate_hashes"]["@adjudicator"], self.build_result["content_hash"])

    def test_source_checkout_is_refused_after_a_packaged_init(self):
        """Fail closed: the manifest pins a packaged adjudicator, so an
        unattested source-checkout run must not verify it."""
        self.assertEqual(self.init_port().returncode, 0)
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "pipeline.py"), "--workspace", str(self.workspace), "doctor"],
            cwd=self.outside,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("unattested", proc.stdout)


class AdjudicatorRepinAcceptanceTest(unittest.TestCase):
    """Chainlink #65, tested with two genuinely different builds of the
    artifact rather than a mocked identity or a dirty-tree hash difference:
    a worktree at OLD_COMMIT (pre-fix) and the current working tree. First
    init pins; a same-binary rerun is a no-op; a different-binary rerun is
    refused and leaves the pin alone; the explicit `migrate --upgrade`
    re-pins."""

    @classmethod
    def setUpClass(cls):
        cls._worktree_tmp = tempfile.TemporaryDirectory()
        cls.worktree = Path(cls._worktree_tmp.name) / "worktree"
        try:
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(cls.worktree), OLD_COMMIT],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            cls._worktree_tmp.cleanup()
            raise unittest.SkipTest(f"cannot create worktree at {OLD_COMMIT}: {exc}") from exc
        # Git submodules are not materialized in a fresh worktree; the build
        # derives its bundle from the inventory, which requires the vendored
        # runtime asset. Copy the main checkout's submodule content in.
        vendor = cls.worktree / "vendor"
        vendor_asset = vendor / "concept-to-code" / "schemas" / "spec.schema.json"
        if not vendor_asset.is_file():
            shutil.rmtree(vendor, ignore_errors=True)
            shutil.copytree(ROOT / "vendor", vendor)
        cls._artifacts_tmp = tempfile.TemporaryDirectory()
        artifacts = Path(cls._artifacts_tmp.name)
        cls.old_build = build_zipapp.build(artifacts / "old", source_root=cls.worktree)
        cls.new_build = build_zipapp.build(artifacts / "new")
        assert cls.old_build["content_hash"] != cls.new_build["content_hash"], (
            "the two builds must be genuinely different for this test to mean anything"
        )

    @classmethod
    def tearDownClass(cls):
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(cls.worktree)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        cls._artifacts_tmp.cleanup()
        cls._worktree_tmp.cleanup()

    def setUp(self):
        self._workspace_tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._workspace_tmp.name).resolve()

    def tearDown(self):
        self._workspace_tmp.cleanup()

    def run_artifact(self, artifact: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-I", artifact, "--workspace", str(self.workspace), *args],
            cwd=str(Path(artifact).parent),
            capture_output=True,
            text=True,
        )

    def init(self, build: dict) -> subprocess.CompletedProcess:
        return self.run_artifact(build["artifact"], "init", "--mode", "greenfield", "--name", "repin")

    def pinned_hash(self) -> str | None:
        manifest = json.loads((self.workspace / "ci" / "manifest" / "installation.json").read_text())
        return manifest["adjudicator"]["content_hash"]

    def test_first_init_still_pins_normally(self):
        proc = self.init(self.new_build)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.pinned_hash(), self.new_build["content_hash"])

    def test_rerun_with_the_same_binary_is_a_noop_on_the_pin(self):
        self.assertEqual(self.init(self.new_build).returncode, 0)
        proc = self.init(self.new_build)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.pinned_hash(), self.new_build["content_hash"])

    def test_rerun_with_a_different_binary_refuses_to_repin(self):
        self.assertEqual(self.init(self.old_build).returncode, 0)
        proc = self.init(self.new_build)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("adjudicator pin mismatch", proc.stdout)
        self.assertIn("migrate --upgrade", proc.stdout)
        self.assertEqual(self.pinned_hash(), self.old_build["content_hash"])

        doctor = self.run_artifact(self.new_build["artifact"], "doctor")
        self.assertEqual(doctor.returncode, 1, doctor.stdout + doctor.stderr)
        self.assertIn("adjudicator pin: mismatch", doctor.stdout)

    def test_migrate_upgrade_repins_deliberately(self):
        self.assertEqual(self.init(self.old_build).returncode, 0)
        self.assertEqual(self.init(self.new_build).returncode, 1)  # refused
        proc = self.run_artifact(self.new_build["artifact"], "migrate", "--upgrade")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.pinned_hash(), self.new_build["content_hash"])

        doctor = self.run_artifact(self.new_build["artifact"], "doctor")
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        self.assertIn("adjudicator pin: pinned", doctor.stdout)


if __name__ == "__main__":
    unittest.main()
