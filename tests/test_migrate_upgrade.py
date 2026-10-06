"""Black-box upgrade tests from a prior release (chainlink #117).

`migrate --upgrade` is the mechanical refresh path for a release that
changed managed templates, schemas, prompts, or the hash-pinned skill
authority: it must re-pin `gate_integrity` from the actually installed
managed files, update only managed installation metadata (the
ownership manifest), preserve the user-owned descriptor and normative
policy, refuse to adopt a locally modified managed file as the new pin,
and emit a machine-readable audit result. Every surface -- doctor,
status --json, check --json, write-set-check --json -- must agree about
the workspace after the migration, and rerunning must be safe and
idempotent.

These tests run the real prior-release artifact
(releases/v1.1.1/ligature.pyz) for the `init` and the current source
tree for the upgrade, in separate processes -- the black-box proof that
a workspace initialized by an older product migrates to this one.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRIOR = ROOT / "releases" / "v1.1.1" / "ligature.pyz"
PIPELINE = ROOT / "scripts" / "pipeline.py"
MANIFEST = "ci/manifest/installation.json"
GATE_SCHEMA = ".ligature/schemas/project-state.schema.json"


def sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


class MigrateUpgradeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name).resolve()
        sys.path.insert(0, str(ROOT / "scripts"))
        import adjudicator  # noqa: PLC0415

        self.product_version = adjudicator.PRODUCT_VERSION

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(PIPELINE), "--workspace", str(self.workspace), *args],
            cwd=cwd or self.workspace,
            capture_output=True,
            text=True,
        )

    def run_prior(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(PRIOR), "--workspace", str(self.workspace), *args],
            capture_output=True,
            text=True,
        )

    def manifest(self) -> dict:
        return json.loads((self.workspace / MANIFEST).read_text())

    def init_prior(self) -> None:
        proc = self.run_prior("init", "--mode", "greenfield", "--name", "myproj")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

    def fix_descriptor_for_current_schema(self) -> None:
        """The user-owned descriptor from v1.1.1 carries the pre-#104
        `verifier_policy.scheduling` key the current closed schema
        rejects. Remove it -- a legitimate user-owned edit -- so the
        healthy post-upgrade surfaces can be asserted."""
        descriptor = self.workspace / "project-descriptor.json"
        data = json.loads(descriptor.read_text())
        data.get("verifier_policy", {}).pop("scheduling", None)
        # A real reviewer value, not the shipped example placeholder.
        if isinstance(data.get("review"), dict):
            data["review"]["reviewer"] = "alice"
        descriptor.write_text(json.dumps(data, indent=2) + "\n")

    def test_upgrade_refreshes_gate_integrity_and_audit_is_machine_readable(self):
        self.init_prior()
        descriptor_bytes = (self.workspace / "project-descriptor.json").read_bytes()
        policy_bytes = (self.workspace / "docs" / "reliance-policy.md").read_bytes()
        old_manifest = self.manifest()
        old_gate_hashes = dict(old_manifest["gate_hashes"])
        self.assertEqual(old_manifest["installed_product_version"], "1.1.1")

        proc = self.run_cli("migrate", "--upgrade", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        audit = json.loads(proc.stdout)

        # Old/new version, changed paths, hashes, required human action.
        self.assertEqual(audit["action"], "migrate --upgrade")
        self.assertEqual(audit["installed_product_version"], {"old": "1.1.1", "new": self.product_version})
        self.assertEqual(audit["product_version"]["new"], self.product_version)
        self.assertTrue(audit["changed_paths"], "an upgrade from 1.1.1 must change managed paths")
        by_path = {p["path"]: p for p in audit["changed_paths"]}
        self.assertIn(GATE_SCHEMA, by_path)
        self.assertIn("old_base_hash", by_path[GATE_SCHEMA])
        self.assertIn("new_base_hash", by_path[GATE_SCHEMA])
        self.assertIn("required_human_action", audit)
        self.assertEqual(audit["required_human_action"], [])
        self.assertEqual(audit["conflicts"], [])

        # gate_integrity hashes are refreshed from the actually installed
        # managed files.
        new_manifest = self.manifest()
        self.assertEqual(new_manifest["installed_product_version"], self.product_version)
        for rel, recorded in new_manifest["gate_hashes"].items():
            if rel == "@adjudicator":
                continue
            self.assertEqual(recorded, sha256_file(self.workspace / rel), rel)
        self.assertNotEqual(old_gate_hashes.get(GATE_SCHEMA), new_manifest["gate_hashes"][GATE_SCHEMA])

        # Only managed installation metadata changed: the user-owned
        # descriptor and normative policy are byte-identical.
        self.assertEqual((self.workspace / "project-descriptor.json").read_bytes(), descriptor_bytes)
        self.assertEqual((self.workspace / "docs" / "reliance-policy.md").read_bytes(), policy_bytes)
        self.assertEqual(
            [f["path"] for f in new_manifest["files"] if f["ownership"] == "user"],
            [f["path"] for f in old_manifest["files"] if f["ownership"] == "user"],
        )

    def test_rerun_is_safe_and_idempotent_and_surfaces_agree(self):
        self.init_prior()
        first = self.run_cli("migrate", "--upgrade", "--json")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.fix_descriptor_for_current_schema()
        stamp = self.run_cli("accept-policy", "--reviewer", "alice", "--version", "reliance-policy@1.0")
        self.assertEqual(stamp.returncode, 0, stamp.stderr + stamp.stdout)
        manifest_after_first = self.manifest()

        second = self.run_cli("migrate", "--upgrade", "--json")
        self.assertEqual(second.returncode, 0, second.stderr)
        audit = json.loads(second.stdout)
        self.assertEqual(audit["result"], "current")
        # The only change is the descriptor re-base after the test's own
        # user-owned edit: metadata tracking only, no managed rewrite.
        self.assertEqual([p["path"] for p in audit["changed_paths"]], ["project-descriptor.json"])
        self.assertEqual(audit["changed_paths"][0]["outcome"], "user-owned")
        self.assertEqual(audit["gate_hashes"], {})
        self.assertEqual(audit["conflicts"], [])
        snapshot = {p.name: sha256_file(p) for p in self.workspace.rglob("*") if p.is_file()}
        after_second_manifest = self.manifest()

        third = self.run_cli("migrate", "--upgrade", "--json")
        self.assertEqual(third.returncode, 0, third.stderr)
        third_audit = json.loads(third.stdout)
        self.assertEqual(third_audit["result"], "current")
        self.assertEqual(third_audit["changed_paths"], [])
        self.assertEqual(third_audit["gate_hashes"], {})
        self.assertEqual(third_audit["conflicts"], [])
        self.assertEqual(self.manifest(), after_second_manifest)
        after = {p.name: sha256_file(p) for p in self.workspace.rglob("*") if p.is_file()}
        self.assertEqual(snapshot, after, "rerunning the upgrade must write nothing")

        # doctor / status --json / check --json / write-set-check agree on
        # a healthy, current, pinned workspace after migration.
        doctor = self.run_cli("doctor")
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        self.assertIn("installation: current", doctor.stdout)

        status = self.run_cli("status", "--json")
        self.assertEqual(status.returncode, 0, status.stderr)
        status_doc = json.loads(status.stdout)
        self.assertEqual(status_doc["gate_integrity"]["state"], "pinned")
        self.assertEqual(status_doc["installation_manifest"]["state"], "current")

        check = self.run_cli("check", "--json")
        self.assertEqual(check.returncode, 0, check.stderr)
        check_doc = json.loads(check.stdout)
        self.assertEqual(check_doc["result"]["exit_code"], 0)
        blocking = [f for f in check_doc["findings"] if f["gate_id"] in ("gate-integrity", "installation")]
        self.assertEqual(blocking, [])

        wsc = self.run_cli("write-set-check", "--json")
        self.assertEqual(wsc.returncode, 0, wsc.stderr)
        wsc_doc = json.loads(wsc.stdout)
        self.assertEqual(wsc_doc["descriptor"]["state"], "present-valid")
        self.assertEqual(wsc_doc["write_set"]["state"], "clean")

    def test_local_managed_file_conflict_is_detected_not_adopted(self):
        self.init_prior()
        # Upgrade to current first so the workspace is otherwise healthy,
        # then make a local edit to a gate-pinned managed file.
        proc = self.run_cli("migrate", "--upgrade")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.fix_descriptor_for_current_schema()
        stamp = self.run_cli("accept-policy", "--reviewer", "alice", "--version", "reliance-policy@1.0")
        self.assertEqual(stamp.returncode, 0, stamp.stderr + stamp.stdout)

        target = self.workspace / GATE_SCHEMA
        target.write_text(target.read_text() + "\n// locally edited, not the product's bytes\n")
        pinned_before = self.manifest()["gate_hashes"][GATE_SCHEMA]

        proc = self.run_cli("migrate", "--upgrade", "--json")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        audit = json.loads(proc.stdout)
        self.assertEqual(audit["result"], "conflict")
        self.assertEqual([c["path"] for c in audit["conflicts"]], [GATE_SCHEMA])
        self.assertEqual(audit["conflicts"][0]["recorded_base_hash"], pinned_before)
        self.assertTrue(audit["required_human_action"])
        self.assertIn(GATE_SCHEMA, audit["required_human_action"][0]["action"])

        # The conflicting bytes are not overwritten, and -- critically --
        # their hash is NOT adopted as the pin: the recorded gate hash is
        # still the product's, so the drift stays visible everywhere.
        self.assertIn("locally edited", target.read_text())
        self.assertEqual(self.manifest()["gate_hashes"][GATE_SCHEMA], pinned_before)

        doctor = self.run_cli("doctor")
        self.assertEqual(doctor.returncode, 1, doctor.stdout)
        self.assertIn("installation: conflict", doctor.stdout)

        status = self.run_cli("status", "--json")
        status_doc = json.loads(status.stdout)
        self.assertEqual(status_doc["gate_integrity"]["state"], "drifted")
        self.assertIn(GATE_SCHEMA, status_doc["gate_integrity"]["details"])

        check = self.run_cli("check", "--json")
        self.assertNotEqual(check.returncode, 0)
        check_doc = json.loads(check.stdout)
        self.assertTrue(
            any(f["gate_id"] == "gate-integrity" and GATE_SCHEMA in f["reason"] for f in check_doc["findings"]),
            check_doc["findings"],
        )

        # The audit stays identical on a rerun: safe to re-read, nothing
        # silently re-based.
        again = self.run_cli("migrate", "--upgrade", "--json")
        self.assertEqual(again.returncode, 1)
        again_audit = json.loads(again.stdout)
        self.assertEqual(again_audit["result"], "conflict")
        self.assertEqual(again_audit["conflicts"], audit["conflicts"])
        self.assertEqual(self.manifest()["gate_hashes"][GATE_SCHEMA], pinned_before)


if __name__ == "__main__":
    unittest.main()
