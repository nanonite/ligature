"""Packaging attestation primitives (chainlink #57).

Unit-level coverage of the packaged-resource abstraction and the
executable's self-attestation: source-checkout resource discovery is
cwd-independent, a built zipapp self-verifies, its content hash is stable
across rebuilds, and tampering with a bundled member makes verification
fail closed. The end-to-end "run the artifact with the source checkout
unavailable" proof lives in tests/test_zipapp_out_of_checkout.py.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import adjudicator  # noqa: E402
import build_zipapp  # noqa: E402
import resources  # noqa: E402


def _run_artifact(artifact: Path, cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", str(artifact), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


class ResourceDiscoveryTest(unittest.TestCase):
    def test_source_checkout_is_not_packaged(self):
        self.assertFalse(resources.is_packaged())
        self.assertEqual(adjudicator.current_identity()["kind"], "source-checkout")

    def test_resource_path_resolves_a_known_schema(self):
        path = resources.resource_path("schemas", "project-state.schema.json")
        self.assertTrue(path.is_file())
        self.assertIn("schema_version", json.loads(path.read_text())["required"])

    def test_resource_access_is_cwd_independent(self):
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                path = resources.resource_path("docs", "boundary-contract-schema.json")
                self.assertTrue(path.is_file())
                self.assertEqual(json.loads(path.read_text())["properties"]["schema_version"]["const"], "1.0")
            finally:
                os.chdir(original)

    def test_source_identity_is_unknown_never_true(self):
        info = adjudicator.current_identity()
        self.assertEqual(info["verified"], "unknown")
        self.assertIsNone(info["content_hash"])
        self.assertTrue(info["interpreter_ok"])
        self.assertFalse(adjudicator.identity_ok(info))


class BuildAttestationTest(unittest.TestCase):
    """One build shared by the tests in this class (building is the slow
    part); each test only reads or minimally mutates the artifact."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls._tmp.name)
        cls.result = build_zipapp.build(cls.out)
        cls.artifact = Path(cls.result["artifact"])

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_release_files_are_produced(self):
        for name in ("ligature.pyz", "SHA256SUMS", "PROVENANCE.json", "NOTICE", "THIRD_PARTY_NOTICES.txt"):
            self.assertTrue((self.out / name).is_file(), name)

    def test_artifact_self_verifies(self):
        proc = _run_artifact(self.artifact, self.out, "version", "--verify")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("attested: true", proc.stdout)
        self.assertIn(self.result["content_hash"], proc.stdout)

    def test_doctor_verifies_from_the_artifact(self):
        with tempfile.TemporaryDirectory() as workspace:
            proc = _run_artifact(self.artifact, self.out, "--workspace", workspace, "doctor")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("attested: true", proc.stdout)

    def test_embedded_attestation_covers_the_manifest(self):
        with zipfile.ZipFile(self.artifact) as zf:
            names = {n for n in zf.namelist() if not n.endswith("/")}
            self.assertIn(adjudicator.ATTESTATION_ARCNAME, names)
            attestation = json.loads(zf.read(adjudicator.ATTESTATION_ARCNAME))
        self.assertEqual(attestation["content_hash"], self.result["content_hash"])
        self.assertEqual(set(attestation["bundle_manifest"]), names - {adjudicator.ATTESTATION_ARCNAME})
        self.assertEqual(attestation["required_python"], adjudicator.required_python_string())
        self.assertTrue(attestation["bundled_schemas"])

    def test_rebuild_has_the_same_content_hash(self):
        with tempfile.TemporaryDirectory() as other:
            second = build_zipapp.build(Path(other))
        self.assertEqual(second["content_hash"], self.result["content_hash"])

    def test_tampered_member_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tampered = Path(tmp) / "tampered.pyz"
            with zipfile.ZipFile(self.artifact) as source, zipfile.ZipFile(tampered, "w", zipfile.ZIP_DEFLATED) as dest:
                for item in source.infolist():
                    data = source.read(item.filename)
                    if item.filename == "resources.py":
                        data += b"\n# tampered\n"
                    dest.writestr(item, data)
            proc = _run_artifact(tampered, Path(tmp), "version", "--verify")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("attested: false", proc.stdout)

    def test_bundle_is_derived_from_the_inventory(self):
        inventory = build_zipapp.load_inventory(ROOT)
        entries = build_zipapp.bundle_entries(inventory)
        arcnames = {arc for _src, arc in entries}

        # every bundle-disposition inventory entry ships ...
        for module in inventory["python_modules"]:
            if module["disposition"] in build_zipapp.BUNDLE_DISPOSITIONS:
                self.assertIn(Path(module["path"]).name, arcnames)
        for key in ("schemas", "schema_examples", "prompts", "documentation_and_templates"):
            for entry in inventory[key]:
                if entry["disposition"] in build_zipapp.BUNDLE_DISPOSITIONS:
                    self.assertIn(f"ligature_data/{entry['path']}", arcnames)
        for asset in inventory["vendored_runtime_assets"]:
            if asset.get("required_at_runtime"):
                self.assertIn(f"ligature_data/{asset['path']}", arcnames)

        # ... and nothing that is documentation/test-only/excluded does
        self.assertIn("pipeline.py", arcnames)
        self.assertNotIn("build_zipapp.py", arcnames)
        self.assertNotIn("test_packaging_attestation.py", arcnames)
        self.assertNotIn("ligature_data/docs/packaging.md", arcnames)


class SourceProvenanceRenderTest(unittest.TestCase):
    """The rendered source-provenance line (chainlink #64). Deterministic and
    git-free, so the dirty/clean/unknown distinction is checked directly."""

    def test_dirty_tree_is_surfaced_never_hidden(self):
        line = adjudicator.render_source_provenance(
            {
                "kind": "zipapp",
                "source_commit": "abc1234",
                "source_dirty": True,
                "working_tree_diff_hash": "sha256:" + "a" * 64,
            }
        )
        self.assertIsNotNone(line)
        self.assertIn("abc1234", line)
        self.assertIn("DIRTY working tree", line)
        self.assertIn("sha256:" + "a" * 64, line)

    def test_clean_tree_is_reported_clean(self):
        line = adjudicator.render_source_provenance(
            {"kind": "zipapp", "source_commit": "abc1234", "source_dirty": False}
        )
        self.assertIn("clean working tree", line)

    def test_unknown_tree_state_is_not_reported_clean(self):
        line = adjudicator.render_source_provenance(
            {"kind": "zipapp", "source_commit": "abc1234", "source_dirty": None}
        )
        self.assertIn("unknown", line)
        self.assertNotIn("clean", line)

    def test_source_checkout_has_no_build_provenance_line(self):
        self.assertIsNone(
            adjudicator.render_source_provenance(
                {"kind": "source-checkout", "source_commit": None, "source_dirty": None}
            )
        )


class GitStateTest(unittest.TestCase):
    """`build_zipapp._git_state` against a controlled temporary repository,
    so clean/dirty detection is proven rather than assumed from this repo's
    own (frequently dirty) working tree."""

    def _init_repo(self, root: Path) -> None:
        def git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)

        git("init")
        git("config", "user.email", "probe@example.invalid")
        git("config", "user.name", "probe")
        (root / "tracked.txt").write_text("one\n")
        git("add", "tracked.txt")
        git("commit", "-m", "initial")

    def test_outside_a_repository_is_unknown_not_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = build_zipapp._git_state(Path(tmp))
        self.assertIsNone(state["commit"])
        self.assertIsNone(state["dirty"])
        self.assertIsNone(state["diff_hash"])

    def test_clean_then_dirty_tracked_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)

            clean = build_zipapp._git_state(root)
            self.assertIsNotNone(clean["commit"])
            self.assertFalse(clean["dirty"])
            self.assertIsNone(clean["diff_hash"])

            (root / "tracked.txt").write_text("two\n")
            dirty = build_zipapp._git_state(root)
            self.assertTrue(dirty["dirty"])
            self.assertRegex(dirty["diff_hash"], r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(dirty["diff_hash"], build_zipapp._working_tree_diff_hash(root))

            (root / "tracked.txt").write_text("three\n")
            self.assertNotEqual(build_zipapp._git_state(root)["diff_hash"], dirty["diff_hash"])

    def test_untracked_file_is_dirty_and_changes_the_diff_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init_repo(root)

            (root / "untracked.txt").write_text("first\n")
            first = build_zipapp._git_state(root)
            self.assertTrue(first["dirty"])
            self.assertRegex(first["diff_hash"], r"^sha256:[0-9a-f]{64}$")

            (root / "untracked.txt").write_text("second\n")
            self.assertNotEqual(build_zipapp._git_state(root)["diff_hash"], first["diff_hash"])


@unittest.skipIf(build_zipapp._git_state(ROOT)["commit"] is None, "not a git checkout")
class DirtyTreeBuildTest(unittest.TestCase):
    """A deliberately dirtied working tree must be recorded honestly, end to
    end, in both PROVENANCE.json and the bundled attestation, and surfaced by
    `version --verify` -- not silently labeled clean (chainlink #64)."""

    def test_dirty_build_is_recorded_and_surfaced(self):
        probe = ROOT / "tests" / f"_dirty_probe_{os.getpid()}.tmp"
        probe.write_text("deliberate uncommitted change\n")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                result = build_zipapp.build(out)
                artifact = Path(result["artifact"])

                provenance = json.loads((out / "PROVENANCE.json").read_text())
                self.assertIs(provenance["source_dirty"], True)
                self.assertEqual(provenance["source_commit"], build_zipapp._git_state(ROOT)["commit"])
                self.assertRegex(provenance["working_tree_diff_hash"], r"^sha256:[0-9a-f]{64}$")

                with zipfile.ZipFile(artifact) as zf:
                    attestation = json.loads(zf.read(adjudicator.ATTESTATION_ARCNAME))
                for field in ("source_commit", "source_dirty", "working_tree_diff_hash"):
                    self.assertEqual(attestation[field], provenance[field], field)

                proc = _run_artifact(artifact, out, "version", "--verify")
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn("attested: true", proc.stdout)
                self.assertIn("DIRTY working tree", proc.stdout)
                self.assertIn(provenance["working_tree_diff_hash"], proc.stdout)
        finally:
            probe.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
