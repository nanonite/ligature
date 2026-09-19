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


if __name__ == "__main__":
    unittest.main()
