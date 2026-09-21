"""`ligature init` / `doctor` / `migrate` over real target workspaces
(chainlink #58).

Everything here is exercised through `pipeline.main()` against a real
temporary workspace on disk, matching tests/test_project_state.py's own
style. The scenarios are the ones #58 names explicitly: fresh
greenfield/port init, byte-identical rerun, user modification,
managed-file conflict, safe upgrade, obsolete files, interrupted writes,
symlink/path attacks, and version incompatibility.
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import adjudicator  # noqa: E402
import ligature_install  # noqa: E402
import pipeline  # noqa: E402
from schema_utils import make_validator  # noqa: E402

DESCRIPTOR_SCHEMA = json.loads((ROOT / "schemas" / "project-descriptor.schema.json").read_text())
SKILL_REL = ".codex/skills/ligature/SKILL.md"
MANAGED_SCHEMA_REL = ".ligature/schemas/project-state.schema.json"


class InstallFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = pipeline.main(["--workspace", str(self.workspace), *args])
        return code, out.getvalue(), err.getvalue()

    def init(self, mode="greenfield", name=None):
        args = ["init", "--mode", mode]
        if name:
            args += ["--name", name]
        return self.run_cli(*args)

    def manifest(self) -> dict:
        return json.loads((self.workspace / "ci" / "manifest" / "installation.json").read_text())

    def write_manifest(self, data: dict) -> None:
        (self.workspace / "ci" / "manifest" / "installation.json").write_text(json.dumps(data, indent=2))

    def read(self, rel: str) -> str:
        return (self.workspace / rel).read_text()

    def snapshot(self) -> dict[str, str]:
        return {
            p.relative_to(self.workspace).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in self.workspace.rglob("*")
            if p.is_file()
        }

    def assert_descriptor_valid(self, mode: str):
        validator = make_validator(DESCRIPTOR_SCHEMA)
        doc = json.loads(self.read("project-descriptor.json"))
        errors = list(validator.iter_errors(doc))
        self.assertEqual(errors, [], [e.message for e in errors])
        self.assertEqual(doc["mode"], mode)
        if mode == "port":
            self.assertIn("port_source", doc)
        else:
            self.assertNotIn("port_source", doc)


class FreshInitTest(InstallFixture):
    def test_greenfield_init_installs_a_valid_descriptor_and_skill(self):
        code, out, _ = self.init("greenfield", name="myproj")
        self.assertEqual(code, 0)
        self.assertIn("installation: current", out)
        self.assert_descriptor_valid("greenfield")
        self.assertTrue((self.workspace / SKILL_REL).is_file())
        self.assertTrue((self.workspace / "docs" / "reliance-policy.md").is_file())
        self.assertTrue((self.workspace / "ci" / "manifest" / "installation.json").is_file())
        self.assertTrue((self.workspace / ".ligature" / "schemas" / "project-state.schema.json").is_file())
        manifest = self.manifest()
        self.assertEqual(manifest["manifest_schema_version"], "1.0")
        self.assertEqual(manifest["mode"], "greenfield")
        self.assertEqual(manifest["project_name"], "myproj")
        self.assertEqual(manifest["installed_product_version"], ligature_install.PRODUCT_VERSION)
        paths = {f["path"]: f for f in manifest["files"]}
        self.assertEqual(paths[SKILL_REL]["ownership"], "managed")
        self.assertEqual(paths["project-descriptor.json"]["ownership"], "user")
        self.assertIsNotNone(paths[SKILL_REL]["authority_hash"])

    def test_port_init_installs_the_port_descriptor_shape(self):
        code, _, _ = self.init("port", name="myport")
        self.assertEqual(code, 0)
        self.assert_descriptor_valid("port")

    def test_project_name_defaults_to_the_workspace_directory(self):
        self.init("greenfield")
        self.assertEqual(self.manifest()["project_name"], ligature_install.default_project_name(self.workspace))

    def test_init_rejects_an_invalid_project_name(self):
        code, _, err = self.init("greenfield", name="../evil")
        self.assertEqual(code, 2)
        self.assertIn("invalid project name", err)

    def test_init_requires_an_existing_workspace(self):
        missing = self.workspace / "does-not-exist"
        with redirect_stderr(io.StringIO()):
            code = pipeline.main(["--workspace", str(missing), "init", "--mode", "greenfield"])
        self.assertEqual(code, 2)


class IdempotenceTest(InstallFixture):
    def test_rerun_is_byte_identical(self):
        self.init("greenfield", name="myproj")
        before = self.snapshot()
        code, out, _ = self.init("greenfield", name="myproj")
        self.assertEqual(code, 0)
        self.assertIn("skill authority hash: verified", out)
        self.assertEqual(before, self.snapshot())
        self.assertIn("unchanged", out)
        self.assertNotIn("CONFLICT", out)

    def test_user_owned_files_are_never_overwritten(self):
        self.init("greenfield", name="myproj")
        edited = "# my own policy\n" + self.read("docs/reliance-policy.md")
        (self.workspace / "docs" / "reliance-policy.md").write_text(edited)
        code, out, _ = self.init("greenfield", name="myproj")
        self.assertEqual(code, 0)
        self.assertEqual(self.read("docs/reliance-policy.md"), edited)
        self.assertIn("user-owned", out)


class ConflictAndAuthorityTest(InstallFixture):
    def test_managed_conflict_is_reported_and_not_overwritten(self):
        self.init("greenfield", name="myproj")
        target = self.workspace / MANAGED_SCHEMA_REL
        tampered = target.read_text() + "\n// local edit\n"
        target.write_text(tampered)

        code, out, _ = self.init("greenfield", name="myproj")
        self.assertEqual(code, 1)
        self.assertIn("CONFLICT", out)
        self.assertEqual(target.read_text(), tampered)

        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("CONFLICT", out)

    def test_migrate_force_is_the_explicit_recovery_path(self):
        self.init("greenfield", name="myproj")
        target = self.workspace / MANAGED_SCHEMA_REL
        target.write_text("locally broken")
        code, _, _ = self.run_cli("migrate", "--force", MANAGED_SCHEMA_REL)
        self.assertEqual(code, 0)
        self.assertEqual(target.read_text(), (ROOT / "schemas" / "project-state.schema.json").read_text())
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 0)
        self.assertIn("installation: current", out)

    def test_migrate_force_refuses_a_non_managed_path(self):
        self.init("greenfield", name="myproj")
        code, _, err = self.run_cli("migrate", "--force", "docs/reliance-policy.md")
        self.assertEqual(code, 2)
        self.assertIn("not a managed file", err)

    def test_doctor_detects_authority_region_tampering(self):
        self.init("greenfield", name="myproj")
        skill = self.workspace / SKILL_REL
        text = skill.read_text()
        tampered = text.replace("LLM output is advisory", "LLM output is authoritative")
        self.assertNotEqual(text, tampered)
        skill.write_text(tampered)
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("MISMATCH", out)

    def test_manifest_records_a_verifiable_authority_hash(self):
        self.init("greenfield", name="myproj")
        text = self.read(SKILL_REL)
        self.assertEqual(
            ligature_install.declared_authority_hash(text),
            ligature_install.computed_authority_hash(text),
        )
        record = next(f for f in self.manifest()["files"] if f["path"] == SKILL_REL)
        self.assertEqual(record["authority_hash"], ligature_install.computed_authority_hash(text))


class UpgradePruneIncompatibilityTest(InstallFixture):
    def _make_managed_file_look_locally_old(self, rel: str) -> str:
        """Simulate an installed-old, locally-unmodified managed file: the
        on-disk bytes and the manifest base agree, but differ from the
        running product's freshly rendered content. That is exactly the
        three-way state a safe upgrade applies to."""
        old = "old installed content\n"
        path = self.workspace / rel
        path.write_text(old)
        old_hash = "sha256:" + hashlib.sha256(old.encode()).hexdigest()
        manifest = self.manifest()
        for record in manifest["files"]:
            if record["path"] == rel:
                record["base_hash"] = old_hash
                record["expected_hash"] = old_hash
        self.write_manifest(manifest)
        return old

    def test_safe_upgrade_applies_and_returns_to_current(self):
        self.init("greenfield", name="myproj")
        self._make_managed_file_look_locally_old(MANAGED_SCHEMA_REL)

        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 0)
        self.assertIn("upgradable", out)

        code, out, _ = self.run_cli("migrate", "--upgrade")
        self.assertEqual(code, 0)
        self.assertEqual(self.read(MANAGED_SCHEMA_REL), (ROOT / "schemas" / "project-state.schema.json").read_text())
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 0)
        self.assertIn("installation: current", out)

    def test_safe_upgrade_without_flag_does_not_write(self):
        self.init("greenfield", name="myproj")
        self._make_managed_file_look_locally_old(MANAGED_SCHEMA_REL)
        before = self.read(MANAGED_SCHEMA_REL)
        code, out, _ = self.run_cli("migrate")
        self.assertEqual(code, 0)
        self.assertIn("upgrade", out)
        self.assertEqual(self.read(MANAGED_SCHEMA_REL), before)

    def test_obsolete_managed_file_is_reported_and_pruned(self):
        self.init("greenfield", name="myproj")
        obsolete_rel = ".ligature/schemas/retired.schema.json"
        content = "{}\n"
        (self.workspace / obsolete_rel).write_text(content)
        digest = "sha256:" + hashlib.sha256(content.encode()).hexdigest()
        manifest = self.manifest()
        manifest["files"].append(
            {"path": obsolete_rel, "ownership": "managed", "base_hash": digest, "expected_hash": digest}
        )
        self.write_manifest(manifest)

        code, out, _ = self.run_cli("migrate")
        self.assertEqual(code, 0)
        self.assertIn("obsolete", out)
        self.assertTrue((self.workspace / obsolete_rel).exists())

        code, _, _ = self.run_cli("migrate", "--prune")
        self.assertEqual(code, 0)
        self.assertFalse((self.workspace / obsolete_rel).exists())
        self.assertNotIn(obsolete_rel, {f["path"] for f in self.manifest()["files"]})

    def test_modified_obsolete_file_is_not_pruned(self):
        self.init("greenfield", name="myproj")
        obsolete_rel = ".ligature/schemas/retired.schema.json"
        content = "{}\n"
        path = self.workspace / obsolete_rel
        path.write_text(content)
        digest = "sha256:" + hashlib.sha256(content.encode()).hexdigest()
        manifest = self.manifest()
        manifest["files"].append(
            {"path": obsolete_rel, "ownership": "managed", "base_hash": digest, "expected_hash": digest}
        )
        self.write_manifest(manifest)
        path.write_text("{}  // edited\n")

        code, out, _ = self.run_cli("migrate", "--prune")
        self.assertEqual(code, 0)
        self.assertTrue(path.exists())
        self.assertIn("not pruned", out)

    def test_version_incompatibility_refuses_migrate(self):
        self.init("greenfield", name="myproj")
        manifest = self.manifest()
        manifest["manifest_schema_version"] = "2.0"
        self.write_manifest(manifest)

        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("incompatible", out)

        code, _, err = self.run_cli("migrate", "--upgrade")
        self.assertEqual(code, 2)
        self.assertIn("incompatible", err)
        self.assertEqual(self.manifest()["manifest_schema_version"], "2.0")

    def test_migrate_on_uninitialized_workspace_is_an_error(self):
        code, _, err = self.run_cli("migrate")
        self.assertEqual(code, 2)
        self.assertIn("not initialized", err)


class WriteSafetyTest(InstallFixture):
    def test_interrupted_write_leaves_the_previous_file_intact(self):
        self.init("greenfield", name="myproj")
        target = self.workspace / MANAGED_SCHEMA_REL
        original = target.read_text()
        with mock.patch("atomic_write.os.replace", side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                ligature_install.migrate(self.workspace, force=(MANAGED_SCHEMA_REL,))
        self.assertEqual(target.read_text(), original)
        leftovers = [p.name for p in target.parent.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_symlinked_directory_is_refused_and_target_untouched(self):
        outside = Path(self._tmp.name + "-outside")
        outside.mkdir()
        # init writes docs/reliance-policy.md; redirect `docs` outside.
        (self.workspace / "docs").symlink_to(outside, target_is_directory=True)
        code, _, err = self.init("greenfield", name="myproj")
        self.assertEqual(code, 2)
        self.assertIn("symlinked", err)
        self.assertFalse((outside / "reliance-policy.md").exists())

    def test_symlinked_managed_destination_is_refused(self):
        self.init("greenfield", name="myproj")
        target = self.workspace / MANAGED_SCHEMA_REL
        target.unlink()
        outside = self.workspace / "outside.json"
        outside.write_text("original outside\n")
        target.symlink_to(outside)
        code, _, err = self.run_cli("migrate", "--force", MANAGED_SCHEMA_REL)
        self.assertEqual(code, 2)
        self.assertIn("symlinked", err)
        self.assertEqual(outside.read_text(), "original outside\n")

    def test_path_escape_is_refused(self):
        for bad in ("../escape.txt", "/etc/passwd", "a/../../escape.txt"):
            with self.subTest(bad=bad):
                with self.assertRaises(ligature_install.InstallError):
                    ligature_install.safe_target(self.workspace, bad)

    def test_malicious_obsolete_manifest_path_is_not_pruned(self):
        self.init("greenfield", name="myproj")
        outside = Path(self._tmp.name + "-outside.txt")
        outside.write_text("do not delete\n")
        manifest = self.manifest()
        manifest["files"].append(
            {
                "path": "../" + outside.name,
                "ownership": "managed",
                "base_hash": "sha256:" + "0" * 64,
                "expected_hash": "sha256:" + "0" * 64,
            }
        )
        self.write_manifest(manifest)
        code, out, _ = self.run_cli("migrate", "--prune")
        self.assertEqual(code, 0)
        self.assertTrue(outside.exists())
        self.assertIn("unsafe", out)


class StatusIntegrationTest(InstallFixture):
    def test_status_reads_the_installed_manifest_and_pins_gate_hashes(self):
        self.init("greenfield", name="myproj")
        code, out, _ = self.run_cli("status", "--json")
        self.assertEqual(code, 0)
        document = json.loads(out)
        self.assertEqual(document["installation_manifest"]["state"], "current")
        self.assertEqual(document["installation_manifest"]["installed_product_version"], ligature_install.PRODUCT_VERSION)
        self.assertEqual(document["gate_integrity"]["state"], "pinned")


def _identity(content_hash, *, kind="zipapp", version=ligature_install.PRODUCT_VERSION) -> dict:
    """A full `adjudicator.current_identity()` shape, so both `init` (which
    records it) and `doctor` (which renders and verifies it) can run under
    a mocked binary identity."""
    return {
        "kind": kind,
        "product_name": "ligature",
        "version": version,
        "content_hash": content_hash,
        "required_python": "3.10",
        "python": "3.12.3",
        "platform": {"system": "Linux", "machine": "x86_64", "required": "any"},
        "interpreter_ok": True,
        "verified": "true" if kind == "zipapp" else "unknown",
        "archive_path": "/tmp/ligature.pyz" if kind == "zipapp" else None,
        "bundled_schemas": {},
        "file_count": 1 if kind == "zipapp" else None,
        "source_commit": None,
        "source_dirty": None,
        "working_tree_diff_hash": None,
        "errors": [],
    }


class AdjudicatorRepinTest(InstallFixture):
    """Chainlink #65: `init` reruns must not silently re-pin a workspace to
    a different executable. Managed files are still reconciled as before;
    only the trusted adjudicator identity is held, and re-pinning requires
    the explicit `migrate --upgrade`/`--force` path."""

    H_A = "sha256:" + "a" * 64
    H_B = "sha256:" + "b" * 64

    def run_as(self, content_hash, *args, kind="zipapp"):
        with mock.patch.object(
            adjudicator, "current_identity", return_value=_identity(content_hash, kind=kind)
        ):
            return self.run_cli(*args)

    def init_as(self, content_hash, *, mode="greenfield", name="myproj", kind="zipapp"):
        return self.run_as(content_hash, "init", "--mode", mode, "--name", name, kind=kind)

    def pinned_hash(self) -> str | None:
        return self.manifest()["adjudicator"]["content_hash"]

    def test_first_init_pins_the_running_binary(self):
        code, out, _ = self.init_as(self.H_A)
        self.assertEqual(code, 0)
        self.assertIn("installation: current", out)
        self.assertEqual(self.pinned_hash(), self.H_A)
        self.assertEqual(self.manifest()["gate_hashes"]["@adjudicator"], self.H_A)

    def test_rerun_with_the_same_binary_is_a_noop_on_the_pin(self):
        self.init_as(self.H_A)
        before = self.manifest()["adjudicator"]
        code, out, _ = self.init_as(self.H_A)
        self.assertEqual(code, 0)
        self.assertEqual(self.manifest()["adjudicator"], before)
        self.assertNotIn("mismatch", out)

    def test_rerun_with_a_different_binary_refuses_to_silently_repin(self):
        self.init_as(self.H_A)
        code, out, _ = self.init_as(self.H_B)
        self.assertEqual(code, 1)
        self.assertIn("adjudicator pin mismatch", out)
        self.assertIn("migrate --upgrade", out)
        self.assertEqual(self.pinned_hash(), self.H_A)
        self.assertEqual(self.manifest()["gate_hashes"]["@adjudicator"], self.H_A)

    def test_doctor_still_reports_the_refused_pin_as_a_mismatch(self):
        self.init_as(self.H_A)
        self.init_as(self.H_B)
        code, out, _ = self.run_as(self.H_B, "doctor")
        self.assertEqual(code, 1)
        self.assertIn("adjudicator pin: mismatch", out)

    def test_migrate_upgrade_is_the_explicit_repin_path(self):
        self.init_as(self.H_A)
        self.init_as(self.H_B)  # refused, pin stays A
        code, out, _ = self.run_as(self.H_B, "migrate", "--upgrade")
        self.assertEqual(code, 0)
        self.assertEqual(self.pinned_hash(), self.H_B)
        code, out, _ = self.run_as(self.H_B, "doctor")
        self.assertEqual(code, 0)
        self.assertIn("adjudicator pin: pinned", out)

    def test_source_checkout_rerun_is_a_noop(self):
        self.init_as(None, kind="source-checkout")
        before = self.manifest()["adjudicator"]
        code, out, _ = self.init_as(None, kind="source-checkout")
        self.assertEqual(code, 0)
        self.assertEqual(self.manifest()["adjudicator"], before)

    def test_source_checkout_cannot_silently_replace_a_packaged_pin(self):
        self.init_as(self.H_A)
        code, out, _ = self.init_as(None, kind="source-checkout")
        self.assertEqual(code, 1)
        self.assertIn("adjudicator pin mismatch", out)
        self.assertEqual(self.pinned_hash(), self.H_A)

    def test_a_pre_57_manifest_without_a_pin_is_pinned_on_rerun(self):
        self.init_as(self.H_A)
        manifest = self.manifest()
        del manifest["adjudicator"]
        self.write_manifest(manifest)
        code, out, _ = self.init_as(self.H_B)
        self.assertEqual(code, 0)
        self.assertEqual(self.pinned_hash(), self.H_B)


if __name__ == "__main__":
    unittest.main()
