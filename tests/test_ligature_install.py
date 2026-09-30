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
import os
import stat
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import adjudicator  # noqa: E402
import ligature_install  # noqa: E402
import pipeline  # noqa: E402
import validate_work_package  # noqa: E402
from schema_utils import make_validator  # noqa: E402

DESCRIPTOR_SCHEMA = json.loads((ROOT / "schemas" / "project-descriptor.schema.json").read_text())
SKILL_REL = ".codex/skills/ligature/SKILL.md"
MANAGED_SCHEMA_REL = ".ligature/schemas/project-state.schema.json"
# chainlink #84: which scripts `init` must ship is G13's requirement to
# declare, not this file's -- derived from the gate's own constant so the
# two cannot drift apart (tests/test_inventory_drift.py pins the third
# declaration, the inventory flag, to the same set).
RENDERER_SCRIPTS = tuple(validate_work_package.WITNESS_RENDERER_INTEGRITY_PATHS)


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

    def test_initialized_files_get_the_umask_derived_mode(self):
        """Chainlink #63, end to end through the CLI: every file init writes
        must get what a umask-respecting open() would have produced, not
        mkstemp's 0600. A non-default umask (0o027 -> 0640) makes a
        hardcoded 0644/0664 fail too."""
        saved = os.umask(0o027)
        try:
            code, _, _ = self.init("greenfield", name="myproj")
        finally:
            os.umask(saved)
        self.assertEqual(code, 0)
        expected = 0o666 & ~0o027
        for rel in (
            "project-descriptor.json",
            "docs/reliance-policy.md",
            SKILL_REL,
            ".ligature/schemas/project-state.schema.json",
            "ci/manifest/installation.json",
            *RENDERER_SCRIPTS,
        ):
            self.assertEqual(stat.S_IMODE((self.workspace / rel).stat().st_mode), expected, rel)

    def test_port_init_installs_the_port_descriptor_shape(self):
        code, _, _ = self.init("port", name="myport")
        self.assertEqual(code, 0)
        self.assert_descriptor_valid("port")

    def test_init_installs_all_three_bundled_schemas(self):
        """chainlink #75 Part 4: `init` writes the descriptor schema into
        `.ligature/schemas/` alongside the other two, so the schema that
        governs the user-owned `project-descriptor.json` is attested as
        installed (`installed_schema_versions`) AND present on disk --
        previously it was attested at 1.0 while absent, leaving the
        descriptor's permitted properties discoverable only by trial and
        error."""
        code, _, _ = self.init("port", name="myport")
        self.assertEqual(code, 0)
        installed = self.workspace / ".ligature" / "schemas" / "project-descriptor.schema.json"
        self.assertTrue(installed.is_file())
        self.assertEqual(installed.read_text(), (ROOT / "schemas" / "project-descriptor.schema.json").read_text())
        paths = {f["path"]: f for f in self.manifest()["files"]}
        self.assertEqual(paths[".ligature/schemas/project-descriptor.schema.json"]["ownership"], "managed")
        self.assertEqual(paths[".ligature/schemas/project-descriptor.schema.json"]["expected_hash"], "sha256:" + hashlib.sha256(installed.read_bytes()).hexdigest())
        # doctor inventories it as an unchanged managed file.
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 0)
        self.assertIn("unchanged  .ligature/schemas/project-descriptor.schema.json", out)

    def test_init_installs_the_descriptor_schema_into_the_project_root(self):
        """chainlink #76: the schema that governs the user-owned descriptor
        is shipped into the project root itself, not only into
        `.ligature/schemas/` -- the root cause recorded on #73/#74, where a
        black-box probe of the v1.0 surface could not discover the
        descriptor's permitted shape (the verifier enum in particular)
        from anything `init` wrote, because the schema lived only inside
        the binary's attested resource bundle."""
        code, _, _ = self.init("port", name="myport")
        self.assertEqual(code, 0)
        installed = self.workspace / "schemas" / "project-descriptor.schema.json"
        self.assertTrue(installed.is_file())
        self.assertEqual(installed.read_text(), (ROOT / "schemas" / "project-descriptor.schema.json").read_text())
        paths = {f["path"]: f for f in self.manifest()["files"]}
        self.assertEqual(paths["schemas/project-descriptor.schema.json"]["ownership"], "managed")
        self.assertEqual(paths["schemas/project-descriptor.schema.json"]["expected_hash"], "sha256:" + hashlib.sha256(installed.read_bytes()).hexdigest())
        # doctor inventories it as an unchanged managed file.
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 0)
        self.assertIn("unchanged  schemas/project-descriptor.schema.json", out)

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
        # chainlink #78: the reliance policy is a NORMATIVE user-owned
        # document, so the edit is reported as drift (non-zero exit, the
        # accept-policy note) -- but it is still never overwritten, and the
        # recorded base is never silently re-based to the edited content.
        self.assertEqual(code, 1)
        self.assertIn("installation: drifted", out)
        self.assertIn("DRIFTED  docs/reliance-policy.md", out)
        self.assertIn("accept-policy", out)
        self.assertEqual(self.read("docs/reliance-policy.md"), edited)
        record = next(f for f in self.manifest()["files"] if f["path"] == "docs/reliance-policy.md")
        self.assertNotEqual(record["base_hash"], "sha256:" + hashlib.sha256(edited.encode()).hexdigest())


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
        # chainlink #75: the verdict is derived from the manifest's recorded
        # authority_hash, so a tampered region reads "drifted" (the old
        # self-consistency check printed "MISMATCH" for the same fact).
        self.assertIn("skill authority hash: drifted", out)
        self.assertNotIn("MISMATCH", out)

    def test_manifest_records_a_verifiable_authority_hash(self):
        self.init("greenfield", name="myproj")
        text = self.read(SKILL_REL)
        self.assertEqual(
            ligature_install.declared_authority_hash(text),
            ligature_install.computed_authority_hash(text),
        )
        record = next(f for f in self.manifest()["files"] if f["path"] == SKILL_REL)
        self.assertEqual(record["authority_hash"], ligature_install.computed_authority_hash(text))

    def test_doctor_detects_tampering_outside_the_authority_region(self):
        """chainlink #75 Part 2, exact reproduction: appending a comment
        AFTER the authority-region END marker leaves the region (and its
        self-consistency) untouched, so the old declared-vs-computed check
        attested the tampered file as verified on the line directly above
        that file's CONFLICT line. The verdict is now derived from the
        manifest's recorded authority_hash plus the file's conflict state,
        so `verified` and `CONFLICT` can never both be true."""
        self.init("greenfield", name="myproj")
        skill = self.workspace / SKILL_REL
        skill.write_text(skill.read_text() + "\n<!-- TAMPERED -->\n")
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("skill authority hash: drifted", out)
        self.assertIn("CONFLICT  .codex/skills/ligature/SKILL.md", out)
        # The attestation line no longer contradicts the inventory line.
        self.assertNotIn("skill authority hash: verified", out)

    def test_doctor_reports_missing_authority_when_the_skill_file_is_absent(self):
        """chainlink #75 Part 2: a deleted skill file must read `missing`,
        never `verified` -- the attestation attests the file listed
        directly below it."""
        self.init("greenfield", name="myproj")
        (self.workspace / SKILL_REL).unlink()
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("skill authority hash: missing", out)
        self.assertIn("MISSING  .codex/skills/ligature/SKILL.md", out)

    def test_doctor_verifies_a_clean_install(self):
        """The flip side of the #75 verdict derivation: an untouched skill
        file's region hash matches the record, so the attestation reads
        `verified` -- the word the pre-#75 code also printed for a clean
        install, now derived from the recorded authority_hash."""
        self.init("greenfield", name="myproj")
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 0)
        self.assertIn("skill authority hash: verified", out)


class WitnessRendererScriptsInstallTest(InstallFixture):
    """chainlink #84: `validate-work-package`'s G13 requires a
    `gate_integrity` entry for EACH of `scripts/witness_renderer.py` and
    `scripts/xml_escape.py` whose runner resolves to a real file inside
    the workspace -- unconditional for every manifest, project-agnostic
    (plan.md §16.5, chainlink #35). v1.0's `init` installed neither, so
    no pilot root even had a `scripts/` directory and Stage 7 could only
    ever report `gate_integrity runner not found`.

    What `init` ships is the product's OWN implementation,
    byte-identical, recorded as a managed file: a locally edited renderer
    is a CONFLICT `migrate --force` recovers explicitly -- never a
    silently accepted substitution, and never a stand-in that would let a
    fake pass through the gate meant to protect real evidence."""

    def _installed_sha(self, rel: str) -> str:
        return "sha256:" + hashlib.sha256((self.workspace / rel).read_bytes()).hexdigest()

    def test_fresh_init_installs_both_scripts_byte_identical_to_the_product(self):
        code, out, _ = self.init("greenfield", name="myproj")
        self.assertEqual(code, 0, out)
        records = {f["path"]: f for f in self.manifest()["files"]}
        for rel in RENDERER_SCRIPTS:
            target = self.workspace / rel
            self.assertTrue(target.is_file(), rel)
            self.assertEqual(target.read_bytes(), (ROOT / rel).read_bytes(), rel)
            record = records[rel]
            self.assertEqual(record["ownership"], "managed", rel)
            self.assertEqual(record["base_hash"], self._installed_sha(rel), rel)
            self.assertEqual(record["expected_hash"], self._installed_sha(rel), rel)

    def test_installed_scripts_do_not_make_the_write_set_dirty(self):
        """`scripts/**` is in every example descriptor's protected_roots,
        and the installed files are product-managed -- so `init` must not
        leave a freshly initialized workspace with an out-of-set or
        unvouched file under scripts/."""
        code, out, _ = self.init("greenfield", name="myproj")
        self.assertEqual(code, 0, out)
        code, out, _ = self.run_cli("write-set-check")
        self.assertEqual(code, 0, out)
        self.assertIn("write set is clean", out)

    def test_rerun_is_unchanged_and_a_local_edit_is_a_conflict(self):
        self.init("greenfield", name="myproj")
        code, out, _ = self.init("greenfield", name="myproj")
        self.assertEqual(code, 0, out)
        for rel in RENDERER_SCRIPTS:
            self.assertIn(f"unchanged  {rel}", out)

        edited = self.read("scripts/witness_renderer.py") + "\n# local edit\n"
        (self.workspace / "scripts" / "witness_renderer.py").write_text(edited)
        code, out, _ = self.init("greenfield", name="myproj")
        self.assertEqual(code, 1, out)
        self.assertIn("CONFLICT  scripts/witness_renderer.py", out)
        # never overwritten: a substituted renderer is exactly what G13 exists to catch
        self.assertEqual(self.read("scripts/witness_renderer.py"), edited)

        code, out, _ = self.run_cli("migrate", "--force", "scripts/witness_renderer.py")
        self.assertEqual(code, 0, out)
        self.assertEqual(
            (self.workspace / "scripts" / "witness_renderer.py").read_bytes(),
            (ROOT / "scripts" / "witness_renderer.py").read_bytes(),
        )
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 0, out)
        self.assertIn("installation: current", out)

    def test_a_workspace_initialized_before_84_gains_the_scripts_on_a_plain_rerun(self):
        """The upgrade path every existing pilot workspace takes: no
        `scripts/` directory and no ownership-manifest record for one. A
        plain `init` rerun creates both files -- no migrate dance, no
        hand-copying out of the binary."""
        self.init("greenfield", name="myproj")
        for rel in RENDERER_SCRIPTS:
            (self.workspace / rel).unlink()
        (self.workspace / "scripts").rmdir()
        manifest = self.manifest()
        manifest["files"] = [f for f in manifest["files"] if f["path"] not in RENDERER_SCRIPTS]
        self.write_manifest(manifest)

        code, out, _ = self.init("greenfield", name="myproj")
        self.assertEqual(code, 0, out)
        records = {f["path"]: f for f in self.manifest()["files"]}
        for rel in RENDERER_SCRIPTS:
            self.assertIn(f"create  {rel}", out)
            self.assertEqual((self.workspace / rel).read_bytes(), (ROOT / rel).read_bytes(), rel)
            # recorded as managed from here on, so later drift is detectable
            self.assertEqual(records[rel]["ownership"], "managed", rel)

        code, out, _ = self.run_cli("status", "--json")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["gate_integrity"]["state"], "pinned")


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


class DoctorDescriptorSchemaTest(InstallFixture):
    """Chainlink #74: `doctor` validates the user-owned project descriptor
    against the product's own `schemas/project-descriptor.schema.json` --
    the same schema and validator `check` fails closed on -- and fails closed
    itself (exit 2, the exit-code contract's invalid-input code) on the
    states `check` reports `invalid_input` for, so a workspace the pipeline
    cannot check is never reported as a healthy, current installation."""

    def invalidate_descriptor(self, mutate):
        path = self.workspace / "project-descriptor.json"
        data = json.loads(path.read_text())
        mutate(data)
        path.write_text(json.dumps(data, indent=2) + "\n")

    def test_doctor_annotates_a_valid_descriptor(self):
        self.init("greenfield", name="myproj")
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 0)
        self.assertIn("  user-owned  project-descriptor.json  schema: valid", out)

    def test_doctor_reports_a_schema_invalid_descriptor_and_exits_non_zero(self):
        self.init("greenfield", name="myproj")
        self.invalidate_descriptor(lambda d: d.__setitem__("bogus_key", "x"))
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 2)
        self.assertIn("  user-owned  project-descriptor.json  schema: invalid (see check for detail)", out)
        # The installation itself is still current; the descriptor is the
        # fail-closed condition, exactly as `check` reports it.
        self.assertIn("installation: current", out)

    def test_doctor_reports_an_invalid_enum_value_and_exits_non_zero(self):
        self.init("greenfield", name="myproj")
        self.invalidate_descriptor(lambda d: d.__setitem__("closure_kind", "bogus"))
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 2)
        self.assertIn("schema: invalid (see check for detail)", out)

    def test_doctor_reports_a_missing_descriptor_and_exits_non_zero(self):
        self.init("greenfield", name="myproj")
        (self.workspace / "project-descriptor.json").unlink()
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 2)
        self.assertIn("  MISSING  project-descriptor.json  schema: absent", out)

    def test_doctor_reports_a_non_json_descriptor_and_exits_non_zero(self):
        self.init("greenfield", name="myproj")
        (self.workspace / "project-descriptor.json").write_text("{not json")
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 2)
        self.assertIn("schema: invalid (see check for detail)", out)

    def test_doctor_and_check_agree_on_the_fail_closed_workspace(self):
        self.init("greenfield", name="myproj")
        self.invalidate_descriptor(lambda d: d.__setitem__("bogus_key", "x"))
        doctor_code, _, _ = self.run_cli("doctor")
        check_code, check_out, _ = self.run_cli("check", "--json")
        # Both commands fail closed on the same workspace (chainlink #74's
        # agreement invariant), but not with the same code since chainlink
        # #75: `check` additionally fails the installation/gate-integrity
        # gate -- an invalid descriptor makes the gate pins unreadable, so
        # gate_integrity reports "unknown" and check exits 5 (the new
        # top-precedence condition) while doctor's own descriptor-schema
        # check still exits 2, the invalid-input code.
        self.assertEqual(doctor_code, 2)
        self.assertEqual(check_code, 5)
        document = json.loads(check_out)
        self.assertEqual(document["result"]["exit_code"], 5)
        self.assertIn("gate_integrity_failed", document["result"]["conditions"])
        self.assertIn("invalid_input", document["result"]["conditions"])

    def test_doctor_on_an_uninitialized_workspace_is_unchanged(self):
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 0)
        self.assertIn("installation: not-initialized", out)
        self.assertNotIn("schema:", out)

    def test_migrate_also_annotates_the_descriptor(self):
        self.init("greenfield", name="myproj")
        self.invalidate_descriptor(lambda d: d.__setitem__("bogus_key", "x"))
        code, out, _ = self.run_cli("migrate")
        self.assertEqual(code, 0)
        self.assertIn("  user-owned  project-descriptor.json  schema: invalid (see check for detail)", out)

    def test_descriptor_schema_report_marks_an_unreadable_file(self):
        self.init("greenfield", name="myproj")
        with mock.patch.object(Path, "read_text", side_effect=OSError("permission denied")):
            report = ligature_install.descriptor_schema_report(self.workspace, "project-descriptor.json")
        self.assertEqual(report.state, "unreadable")


class NormativePolicyDriftTest(InstallFixture):
    """chainlink #78: a user-owned normative document (the reliance policy)
    cannot be drift-checked, and the ownership manifest records hashes for
    it that are never compared -- so `doctor`, `check` and `status` all
    report healthy before and after an unreviewed edit that inverts the
    document's own resolution rule. These tests pin the fixed behavior:
    on-disk content is compared against the manifest's recorded base_hash,
    drift is reported by all three commands, the recorded base moves only
    through the explicit `accept-policy` path, and the descriptor's
    `compatibility_policy.reliance_policy_path` is validated (exists, inside
    the project root) and consumed as the accept commands' default."""

    POLICY = "docs/reliance-policy.md"

    def edited_policy(self) -> str:
        """The installed policy plus an unreviewed governance edit and a
        filled-in version marker (the template's own placeholder line is
        not a valid marker, so accept-policy refuses until it is replaced)."""
        text = self.read(self.POLICY)
        text = text.replace(
            "Policy version: `<policy-name>@<major>.<minor>`",
            "Policy version: `reliance-policy@1.2`",
        )
        return text + "\n## Unreviewed change\n\nWeaken the resolution rule.\n"

    def accept_policy(self, *extra):
        return self.run_cli("accept-policy", "--reviewer", "alice", *extra)

    def policy_record(self) -> dict:
        return next(f for f in self.manifest()["files"] if f["path"] == self.POLICY)

    def test_doctor_reports_an_unreviewed_policy_edit_as_drift(self):
        """The defect report's exact scenario: append a section that
        inverts the resolution rule -- `doctor` must report DRIFTED and
        exit 1, not `installation: current` / `user-owned` / exit 0."""
        self.init("greenfield", name="myproj")
        edited = self.edited_policy()
        (self.workspace / self.POLICY).write_text(edited)
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("installation: drifted", out)
        self.assertIn(f"DRIFTED  {self.POLICY}", out)
        self.assertIn("accept-policy", out)
        # The file is never overwritten, and the recorded base is never
        # silently re-based to the edited content.
        self.assertEqual(self.read(self.POLICY), edited)
        self.assertNotEqual(
            self.policy_record()["base_hash"],
            "sha256:" + hashlib.sha256(edited.encode()).hexdigest(),
        )

    def test_doctor_reports_a_deleted_policy_as_missing(self):
        self.init("greenfield", name="myproj")
        (self.workspace / self.POLICY).unlink()
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("installation: drifted", out)
        self.assertIn(f"MISSING  {self.POLICY}", out)

    def test_check_fails_on_policy_drift_and_names_the_accept_path(self):
        self.init("greenfield", name="myproj")
        (self.workspace / self.POLICY).write_text(self.edited_policy())
        code, out, _ = self.run_cli("check", "--json")
        self.assertEqual(code, 1)
        document = json.loads(out)
        self.assertIn("blocking_findings", document["result"]["conditions"])
        drift = [f for f in document["findings"] if f["gate_id"] == "policy-drift"]
        self.assertEqual(len(drift), 1)
        self.assertEqual(drift[0]["subject"], self.POLICY)
        self.assertEqual(drift[0]["severity"], "high")
        self.assertIn("accept-policy", drift[0]["reason"])

    def test_check_fails_on_a_deleted_policy(self):
        self.init("greenfield", name="myproj")
        (self.workspace / self.POLICY).unlink()
        code, out, _ = self.run_cli("check", "--json")
        self.assertEqual(code, 1)
        document = json.loads(out)
        drift = [f for f in document["findings"] if f["gate_id"] == "policy-drift"]
        self.assertEqual(len(drift), 1)
        self.assertIn("missing", drift[0]["reason"])

    def test_status_reports_the_drifted_installation(self):
        """A consumer reading only `status --json` must not be told the
        installation is current while the governance document it pins is
        edited or missing."""
        self.init("greenfield", name="myproj")
        code, out, _ = self.run_cli("status", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["installation_manifest"]["state"], "current")

        (self.workspace / self.POLICY).write_text(self.edited_policy())
        code, out, _ = self.run_cli("status", "--json")
        self.assertEqual(code, 0)  # status itself is a read-only query
        document = json.loads(out)
        self.assertEqual(document["installation_manifest"]["state"], "drifted")
        self.assertTrue(
            any(f["gate_id"] == "policy-drift" for f in document["open_findings"]),
            "the drift must be an open finding, not only a state transition",
        )

    def test_init_rerun_reports_drift_without_rebasing(self):
        self.init("greenfield", name="myproj")
        edited = self.edited_policy()
        (self.workspace / self.POLICY).write_text(edited)
        code, out, _ = self.init("greenfield", name="myproj")
        self.assertEqual(code, 1)
        self.assertIn("installation: drifted", out)
        self.assertIn(f"DRIFTED  {self.POLICY}", out)
        self.assertEqual(self.read(self.POLICY), edited)
        self.assertNotEqual(
            self.policy_record()["base_hash"],
            "sha256:" + hashlib.sha256(edited.encode()).hexdigest(),
        )

    def test_migrate_does_not_rebase_the_recorded_base_either(self):
        """`migrate --upgrade` is the explicit recovery path for MANAGED
        files; a normative user-owned document's base must move only
        through `accept-policy`."""
        self.init("greenfield", name="myproj")
        edited = self.edited_policy()
        (self.workspace / self.POLICY).write_text(edited)
        code, out, _ = self.run_cli("migrate", "--upgrade")
        self.assertEqual(code, 1)
        self.assertIn("installation: drifted", out)
        self.assertEqual(self.read(self.POLICY), edited)
        self.assertNotEqual(
            self.policy_record()["base_hash"],
            "sha256:" + hashlib.sha256(edited.encode()).hexdigest(),
        )

    def test_accept_policy_records_the_reviewed_change(self):
        """The documented accept path: a reviewed edit stops reading as
        drift, and the recorded base moves to the reviewed content."""
        self.init("greenfield", name="myproj")
        edited = self.edited_policy()
        (self.workspace / self.POLICY).write_text(edited)
        code, out, _ = self.accept_policy()
        self.assertEqual(code, 0)
        self.assertIn("accepted policy: " + self.POLICY, out)
        self.assertIn("reviewer: alice", out)
        self.assertIn("policy version: reliance-policy@1.2", out)
        self.assertEqual(
            self.policy_record()["base_hash"],
            "sha256:" + hashlib.sha256(edited.encode()).hexdigest(),
        )
        # The drift signal clears -- all three commands report healthy.
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 0)
        self.assertIn("installation: current", out)
        self.assertIn(f"user-owned  {self.POLICY}", out)
        code, out, _ = self.run_cli("check", "--json")
        document = json.loads(out)
        self.assertFalse([f for f in document["findings"] if f["gate_id"] == "policy-drift"])

    def test_accept_policy_defaults_to_the_descriptors_declaration(self):
        """`--policy-path` defaults to the descriptor's
        `compatibility_policy.reliance_policy_path`."""
        self.init("greenfield", name="myproj")
        (self.workspace / self.POLICY).write_text(self.edited_policy())
        code, out, _ = self.run_cli("accept-policy", "--reviewer", "alice")
        self.assertEqual(code, 0)
        self.assertIn("accepted policy: " + self.POLICY, out)

    def test_accept_policy_refuses_a_path_that_disagrees_with_the_descriptor(self):
        self.init("greenfield", name="myproj")
        (self.workspace / self.POLICY).write_text(self.edited_policy())
        code, _, err = self.accept_policy("--policy-path", "docs/other-policy.md")
        self.assertEqual(code, 2)
        self.assertIn("disagrees with the project descriptor", err)
        # Nothing was recorded.
        self.assertNotEqual(
            self.policy_record()["base_hash"],
            "sha256:" + hashlib.sha256(self.read(self.POLICY).encode()).hexdigest(),
        )

    def test_accept_policy_requires_a_reviewer(self):
        """`--reviewer` is a required argument (argparse enforces it with
        exit 2 before the command runs) -- no default, no LLM-supplied
        value."""
        self.init("greenfield", name="myproj")
        (self.workspace / self.POLICY).write_text(self.edited_policy())
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli("accept-policy")
        self.assertEqual(ctx.exception.code, 2)

    def test_accept_policy_refuses_an_unrecorded_path(self):
        """With no descriptor declaration to disagree with, an explicit path
        the manifest does not record is refused -- the manifest records
        what `ligature init` installed."""
        self.init("greenfield", name="myproj")
        descriptor = json.loads(self.read("project-descriptor.json"))
        del descriptor["compatibility_policy"]
        (self.workspace / "project-descriptor.json").write_text(json.dumps(descriptor))
        code, _, err = self.accept_policy("--policy-path", "docs/other-policy.md")
        self.assertEqual(code, 2)
        self.assertIn("does not record", err)

    def test_accept_policy_refuses_a_non_normative_path(self):
        """The descriptor is user-owned but NOT normative -- its integrity
        mechanism is schema validation, not a hash pin."""
        self.init("greenfield", name="myproj")
        descriptor = json.loads(self.read("project-descriptor.json"))
        del descriptor["compatibility_policy"]
        (self.workspace / "project-descriptor.json").write_text(json.dumps(descriptor))
        code, _, err = self.accept_policy("--policy-path", "project-descriptor.json")
        self.assertEqual(code, 2)
        self.assertIn("not a normative policy document", err)

    def test_accept_policy_refuses_a_missing_file(self):
        self.init("greenfield", name="myproj")
        (self.workspace / self.POLICY).unlink()
        code, _, err = self.accept_policy()
        self.assertEqual(code, 2)
        self.assertIn("missing", err)

    def test_accept_policy_refuses_a_document_without_a_version_marker(self):
        """The recorded hash must always correspond to a policy that can
        yield a policy_version -- the same convention accept-promotion
        reads."""
        self.init("greenfield", name="myproj")
        (self.workspace / self.POLICY).write_text("# Reliance policy\n\nNo marker.\n")
        code, _, err = self.accept_policy()
        self.assertEqual(code, 2)
        self.assertIn("Policy version", err)

    def test_accept_policy_on_an_uninitialized_workspace_is_an_error(self):
        code, _, err = self.accept_policy()
        self.assertEqual(code, 2)
        self.assertIn("not initialized", err)

    def test_accept_policy_refuses_an_incompatible_manifest(self):
        self.init("greenfield", name="myproj")
        manifest = self.manifest()
        manifest["manifest_schema_version"] = "2.0"
        self.write_manifest(manifest)
        code, _, err = self.accept_policy()
        self.assertEqual(code, 2)
        self.assertIn("incompatible", err)

    def test_a_legacy_manifest_without_a_base_hash_adopts_the_on_disk_content_once(self):
        """A pre-#78 workspace records no base_hash for the policy: the
        first init/migrate adopts the on-disk content (so the workspace
        becomes drift-checkable rather than permanently unverifiable), and
        only a LATER edit reads as drift."""
        self.init("greenfield", name="myproj")
        manifest = self.manifest()
        for record in manifest["files"]:
            if record["path"] == self.POLICY:
                del record["base_hash"]
        self.write_manifest(manifest)
        edited = self.edited_policy()
        (self.workspace / self.POLICY).write_text(edited)

        code, out, _ = self.init("greenfield", name="myproj")
        self.assertEqual(code, 0)
        self.assertIn("installation: current", out)
        self.assertEqual(
            self.policy_record()["base_hash"],
            "sha256:" + hashlib.sha256(edited.encode()).hexdigest(),
        )

        # A further edit now reads as drift against the adopted base.
        further = edited + "\n## Another unreviewed change\n"
        (self.workspace / self.POLICY).write_text(further)
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("installation: drifted", out)


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


def _render_with_stamp(path: str, marker: str):
    """The real renderer with one managed file's content changed -- stands in
    for a different build's registry/templates producing different bytes."""
    real = ligature_install.render_entry

    def render(entry, mode, name):
        text = real(entry, mode, name)
        if entry.path == path:
            return text + f"\n{marker}\n"
        return text

    return render


class AdjudicatorConflictFreezesInstallTest(InstallFixture):
    """Chainlink #68: an adjudicator conflict freezes the whole install, not
    just the pin. A refused rerun must not rewrite managed-file content from
    its own (untrusted) registry, nor bump the recorded product/schema
    versions to its own; only an explicit `migrate --upgrade` applies them.
    A genuinely new file ("create") still installs."""

    H_A = "sha256:" + "a" * 64
    H_B = "sha256:" + "b" * 64
    PROMPT_REL = ".ligature/prompts/stage-0-evidence-intake.md"
    REFUSED_VERSION = "9.9.9-refused"
    STAMP = "refused-binary content"

    def run_as(self, content_hash, *args, kind="zipapp", product_version=None, render=None):
        identity = _identity(
            content_hash,
            kind=kind,
            version=product_version or ligature_install.PRODUCT_VERSION,
        )
        patches = [mock.patch.object(adjudicator, "current_identity", return_value=identity)]
        if product_version is not None:
            patches.append(mock.patch.object(ligature_install, "PRODUCT_VERSION", product_version))
        if render is not None:
            patches.append(mock.patch.object(ligature_install, "render_entry", render))
        with ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            return self.run_cli(*args)

    def init_as(self, content_hash, *, product_version=None, render=None):
        return self.run_as(
            content_hash,
            "init",
            "--mode",
            "greenfield",
            "--name",
            "myproj",
            product_version=product_version,
            render=render,
        )

    def test_conflict_refuses_upgrade_content_and_version_bump(self):
        self.init_as(self.H_A)
        old_prompt = self.read(self.PROMPT_REL)
        old_product = self.manifest()["installed_product_version"]
        old_schemas = self.manifest()["installed_schema_versions"]

        code, out, _ = self.init_as(
            self.H_B,
            product_version=self.REFUSED_VERSION,
            render=_render_with_stamp(self.PROMPT_REL, self.STAMP),
        )

        self.assertEqual(code, 1)
        self.assertIn("adjudicator pin mismatch", out)
        self.assertIn("installation: conflict", out)
        self.assertIn("upgrade", out)  # the refused upgrade is still reported
        self.assertNotIn(self.STAMP, self.read(self.PROMPT_REL))
        self.assertEqual(self.read(self.PROMPT_REL), old_prompt)
        self.assertEqual(self.manifest()["installed_product_version"], old_product)
        self.assertNotEqual(self.manifest()["installed_product_version"], self.REFUSED_VERSION)
        self.assertEqual(self.manifest()["installed_schema_versions"], old_schemas)
        self.assertEqual(self.pinned_hash(), self.H_A)
        self.assertEqual(self.manifest()["gate_hashes"]["@adjudicator"], self.H_A)

    def test_conflict_refuses_a_version_bump_without_any_content_change(self):
        self.init_as(self.H_A)
        old_product = self.manifest()["installed_product_version"]
        old_schemas = self.manifest()["installed_schema_versions"]

        code, out, _ = self.init_as(self.H_B, product_version=self.REFUSED_VERSION)

        self.assertEqual(code, 1)
        self.assertIn("installation: conflict", out)
        self.assertNotEqual(old_product, self.REFUSED_VERSION)
        self.assertEqual(self.manifest()["installed_product_version"], old_product)
        self.assertEqual(self.manifest()["installed_schema_versions"], old_schemas)
        self.assertEqual(self.pinned_hash(), self.H_A)

    def test_migrate_upgrade_applies_the_refused_content_and_version(self):
        self.init_as(self.H_A)
        old_prompt = self.read(self.PROMPT_REL)
        render = _render_with_stamp(self.PROMPT_REL, self.STAMP)
        self.assertEqual(
            self.init_as(self.H_B, product_version=self.REFUSED_VERSION, render=render)[0],
            1,
        )
        self.assertEqual(self.read(self.PROMPT_REL), old_prompt)  # refused, still frozen

        code, out, _ = self.run_as(
            self.H_B,
            "migrate",
            "--upgrade",
            product_version=self.REFUSED_VERSION,
            render=render,
        )

        self.assertEqual(code, 0, out)
        self.assertIn(self.STAMP, self.read(self.PROMPT_REL))
        self.assertEqual(self.manifest()["installed_product_version"], self.REFUSED_VERSION)
        self.assertEqual(self.pinned_hash(), self.H_B)

    def test_conflict_still_installs_a_genuinely_new_file(self):
        self.init_as(self.H_A)
        (self.workspace / self.PROMPT_REL).unlink()

        code, out, _ = self.init_as(self.H_B)

        self.assertEqual(code, 1)
        self.assertIn("installation: conflict", out)
        self.assertTrue((self.workspace / self.PROMPT_REL).is_file())

    def pinned_hash(self) -> str | None:
        return self.manifest()["adjudicator"]["content_hash"]


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
