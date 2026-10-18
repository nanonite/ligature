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
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import build_zipapp  # noqa: E402
from schema_utils import make_validator  # noqa: E402

DESCRIPTOR_SCHEMA = json.loads((ROOT / "schemas" / "project-descriptor.schema.json").read_text())
#: The write-grant record schema (#114). Read from the checkout here, and
#: asserted to be in the built archive below: the bundle's file list is
#: derived from docs/implementation-inventory.json, so a schema that is not
#: inventory-tracked does not ship -- and a shipped binary whose grant records
#: nobody can validate is an audit trail with no published shape.
GRANT_SCHEMA = json.loads((ROOT / "docs" / "write-grant-schema.json").read_text())
#: The scaffold receipt schema (#115). Same reasoning as the grant schema above:
#: a published shape a consumer parses must ship, and the bundle's file list
#: comes from the inventory -- so a receipt schema nobody tracks does not ship.
SCAFFOLD_SCHEMA = json.loads((ROOT / "docs" / "scaffold-receipt-schema.json").read_text())
CARGO_PROFILE_SCHEMA = json.loads((ROOT / "docs" / "cargo-profile-audit-schema.json").read_text())
CARGO_VERUS_PROFILE_SCHEMA = json.loads((ROOT / "docs" / "cargo-verus-profile-audit-schema.json").read_text())


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

    def test_write_set_check_rejects_a_probe_inside_a_protected_root(self):
        """chainlink #103, end to end through the packaged artifact and
        `python3 -I` with no source tree: a file planted inside
        `ci/manifest/**` -- a protected_roots path that 1.1.0 through
        1.2.1 reported as `write set: clean` with `violations: []` -- is a
        blocking `protected-write` violation naming the matched pattern."""
        self.assertEqual(self.init_port().returncode, 0)
        descriptor = self.workspace / "project-descriptor.json"
        declared = json.loads(descriptor.read_text())
        # The pilot's own protected_roots: ci/manifest/** among them.
        declared["write_set"] = {
            "allowed_roots": ["rust/*/src/", "rust/*/tests/"],
            "protected_roots": ["rust/*/specs/**", "ci/manifest/**"],
        }
        descriptor.write_text(json.dumps(declared, indent=2) + "\n")
        probe = self.workspace / "ci" / "manifest" / "PROBE.json"
        probe.write_text("{}")
        proc = self.run_artifact("--workspace", str(self.workspace), "write-set-check")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("write set: violations", proc.stdout)
        self.assertIn("protected-write", proc.stdout)
        self.assertIn("ci/manifest/PROBE.json", proc.stdout)
        self.assertIn("ci/manifest/**", proc.stdout)
        json_proc = self.run_artifact("--workspace", str(self.workspace), "write-set-check", "--json")
        document = json.loads(json_proc.stdout)["write_set"]
        self.assertEqual(document["state"], "violations")
        self.assertEqual(
            [(v["path"], v["kind"]) for v in document["violations"]],
            [("ci/manifest/PROBE.json", "protected-write")],
        )

    def test_status_kani_record_is_fast_and_does_not_spawn_cargo(self):
        """The installed artifact's default status reads recorded Kani
        evidence and checks its source/harness hashes without launching
        Cargo, even when a Kani record is present."""
        sys.path.insert(0, str(ROOT / "tests"))
        from test_record_assurance import KaniRecordAssuranceTest
        from unittest.mock import patch

        fixture = KaniRecordAssuranceTest("test_status_reads_fresh_kani_record_without_spawning_verifier")
        fixture.setUp()
        try:
            with patch(
                "record_assurance.shutil.which",
                side_effect=lambda executable: f"/usr/bin/{executable}",
            ), patch(
                "record_assurance.subprocess.run",
                side_effect=fixture.fake_kani(),
            ):
                fixture.record()
            fixture.prepare_status_workspace()
            shutil.copytree(fixture.root, self.workspace, dirs_exist_ok=True)
        finally:
            fixture.doCleanups()

        fake_bin = self.outside / "fake-tools"
        fake_bin.mkdir()
        cargo_marker = self.outside / "cargo-was-launched"
        cargo = fake_bin / "cargo"
        cargo.write_text(
            "#!/bin/sh\nprintf called > " + str(cargo_marker) + "\nexit 0\n"
        )
        cargo.chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
        proc = subprocess.run(
            [
                sys.executable,
                "-I",
                str(self.artifact),
                "--workspace",
                str(self.workspace),
                "status",
                "--json",
            ],
            cwd=self.outside,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        document = json.loads(proc.stdout)
        self.assertEqual(document["schema_version"], "1.1")
        self.assertEqual(document["verification"]["mode"], "recorded")
        self.assertFalse(cargo_marker.exists(), "default status must not spawn Cargo/Kani")

    def test_bundled_contract_is_reachable_after_init_and_141_upgrade(self):
        """#167: the installed executable exposes its own bundled contract,
        both after a fresh init and after upgrading a 1.4.1 workspace."""
        old_artifact = ROOT / "releases" / "v1.4.1" / "ligature.pyz"
        self.assertTrue(old_artifact.is_file(), "1.4.1 fixture bundle is required")

        initialized = self.run_artifact(
            "--workspace", str(self.workspace), "init", "--mode", "port", "--name", "contract-test"
        )
        self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
        plain = self.run_artifact("contract")
        self.assertEqual(plain.returncode, 0, plain.stdout + plain.stderr)
        expected = (ROOT / "docs" / "cli-contract.md").read_text()
        self.assertEqual(plain.stdout, expected)

        encoded = self.run_artifact("contract", "--json")
        self.assertEqual(encoded.returncode, 0, encoded.stdout + encoded.stderr)
        envelope = json.loads(encoded.stdout)
        self.assertEqual(envelope["format"], "ligature.cli-contract.v1")
        self.assertEqual(envelope["grammar_version"], "v1.0")
        self.assertEqual(envelope["contract"], expected)
        self.assertEqual(
            envelope["sha256"], "sha256:" + hashlib.sha256(expected.encode("utf-8")).hexdigest()
        )

        with tempfile.TemporaryDirectory() as temp:
            upgraded_workspace = Path(temp).resolve()
            prior = subprocess.run(
                [sys.executable, "-I", str(old_artifact), "--workspace", str(upgraded_workspace),
                 "init", "--mode", "port", "--name", "contract-upgrade"],
                cwd=self.outside, capture_output=True, text=True,
            )
            self.assertEqual(prior.returncode, 0, prior.stdout + prior.stderr)
            migrated = subprocess.run(
                [sys.executable, "-I", str(self.artifact), "--workspace", str(upgraded_workspace),
                 "migrate", "--upgrade", "--json"],
                cwd=self.outside, capture_output=True, text=True,
            )
            self.assertEqual(migrated.returncode, 0, migrated.stdout + migrated.stderr)
            after_upgrade = subprocess.run(
                [sys.executable, "-I", str(self.artifact), "contract"],
                cwd=self.outside, capture_output=True, text=True,
            )
            self.assertEqual(after_upgrade.returncode, 0, after_upgrade.stdout + after_upgrade.stderr)
            self.assertEqual(after_upgrade.stdout, expected)

    def test_bridge_harness_artifact_contract_through_packaged_binary(self):
        """#168: the packaged G9 generator, G9 gate, and write-set check
        agree about the exact generated harness bytes; missing and edited
        harnesses remain actionable violations."""
        sys.path.insert(0, str(ROOT / "tests"))
        from test_gate_g9 import GenerationTest, bridge_records_for

        fixture = GenerationTest("test_generation_writes_a_harness_and_a_check_record")
        fixture.setUp()
        try:
            descriptor = dict(fixture.descriptor)
            descriptor["write_set"] = {
                "allowed_roots": ["crates/**", "verif/**"],
                "protected_roots": ["ci/harness/**"],
            }
            (fixture.root / "project-descriptor.json").write_text(json.dumps(descriptor, indent=2) + "\n")
            shutil.copytree(fixture.root, self.workspace, dirs_exist_ok=True)
        finally:
            fixture.doCleanups()

        generated = self.run_artifact("--workspace", str(self.workspace), "check-bridges")
        self.assertEqual(generated.returncode, 0, generated.stdout + generated.stderr)
        self.assertIn("[ligature] bridge BR-SCHED-TQ-001", generated.stderr)
        # Workspace's prebuilt WP-A report is a fixture stub. Align its
        # bridge evidence with the actual successful record, as the source
        # gate tests do, before asking G9 to consume that report.
        assurance_path = self.workspace / "ci/results/WP-A.json"
        assurance = json.loads(assurance_path.read_text())
        assurance["bridge_records"] = bridge_records_for(self.workspace)
        assurance_path.write_text(json.dumps(assurance, indent=2) + "\n")
        gated = self.run_artifact("--workspace", str(self.workspace), "gate-g9")
        self.assertEqual(gated.returncode, 0, gated.stdout + gated.stderr)
        clean = self.run_artifact("--workspace", str(self.workspace), "write-set-check", "--json")
        self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
        self.assertEqual(json.loads(clean.stdout)["write_set"]["state"], "clean")

        harness = self.workspace / "ci/harness/BR-SCHED-TQ-001.creusot.rs"
        harness.write_text(harness.read_text() + "// edited after generation\n")
        edited = self.run_artifact("--workspace", str(self.workspace), "write-set-check", "--json")
        self.assertEqual(edited.returncode, 1, edited.stdout + edited.stderr)
        self.assertIn("ci/harness/BR-SCHED-TQ-001.creusot.rs", edited.stdout)
        self.assertIn("protected-write", edited.stdout)

        harness.unlink()
        missing = self.run_artifact("--workspace", str(self.workspace), "gate-g9")
        self.assertEqual(missing.returncode, 1, missing.stdout + missing.stderr)
        self.assertIn("no generated harness", missing.stdout + missing.stderr)
        self.assertIn("ligature.pyz check-bridges", missing.stdout + missing.stderr)

    def test_the_write_grant_flow_runs_end_to_end_through_the_packaged_binary(self):
        """chainlink #114 end to end through the packaged artifact and
        `python3 -I` with no source tree: the sanctioned protected-root write,
        the grant that authorizes it, the fail-closed paths around it, and the
        record the binary wrote on disk.

        #103's enforcement shipped in three releases before `write-set-check`
        reported the probe below at all, and #114's remedy is only real if it
        works in the artifact users actually run -- including the bundled
        `docs/write-grant-schema.json`, which the bundle takes from the
        inventory rather than a hand-maintained list. So this test asserts
        that schema is in the archive, and that the line the packaged binary
        appended validates against it."""
        with zipfile.ZipFile(self.artifact) as zf:
            bundled = {n for n in zf.namelist() if not n.endswith("/")}
        self.assertIn(
            "ligature_data/docs/write-grant-schema.json",
            bundled,
            "the write-grant schema must ship: the bundle is derived from the inventory",
        )
        self.assertEqual(self.init_port().returncode, 0)
        descriptor = self.workspace / "project-descriptor.json"
        declared = json.loads(descriptor.read_text())
        declared["write_set"] = {
            "allowed_roots": ["rust/*/src/", "rust/*/tests/"],
            "protected_roots": ["rust/*/specs/**", "ci/manifest/**"],
        }
        descriptor.write_text(json.dumps(declared, indent=2) + "\n")
        probe = self.workspace / "ci" / "manifest" / "WP-114.json"
        probe.write_text("{}")

        def violations(issue=None):
            args = ["--workspace", str(self.workspace), "write-set-check", "--json"]
            if issue is not None:
                args += ["--issue", str(issue)]
            document = json.loads(self.run_artifact(*args).stdout)["write_set"]
            return [(v["path"], v["kind"]) for v in document["violations"]], document

        # 1. before: the sanctioned write is the blocking finding it always was
        before, _ = violations(114)
        self.assertEqual(before, [("ci/manifest/WP-114.json", "protected-write")])

        # 2. the grant: one append-only line, and the record it wrote is the
        #    one the command printed and the bundled schema accepts
        issued = self.run_artifact(
            "--workspace", str(self.workspace), "authorize-write", "--issue", "114",
            "--path", "ci/manifest/WP-114.json", "--op", "write", "--issuer", "operator",
            "--one-shot", "--json",
        )
        self.assertEqual(issued.returncode, 0, issued.stdout + issued.stderr)
        record = json.loads(issued.stdout)
        ledger = self.workspace / "ci" / "results" / "protected-writes.jsonl"
        lines = ledger.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0]), record)
        validator = make_validator(GRANT_SCHEMA)
        self.assertEqual(
            [e.message for e in validator.iter_errors(record)], [],
            "the packaged binary wrote a record its own bundled schema rejects",
        )

        # 3. after: clean, and reported as the sanctioned write it is -- with
        #    the one-shot limit stated rather than implied
        after, document = violations(114)
        self.assertEqual(after, [])
        entry = document["authorized_writes"][0]
        self.assertEqual((entry["grant_id"], entry["status"], entry["one_shot"]),
                         (record["grant_id"], "spent", True))
        self.assertEqual(document["grants"]["entries"][0]["status"], "active")
        self.assertIn("counts no edits", document["details"])

        # 4. the fail-closed paths, same artifact: no issue named, another
        #    issue, and a grant that has not been issued yet
        for scope in (None, 113):
            with self.subTest(issue=scope):
                self.assertEqual(violations(scope)[0], before)
        future = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat()
        refused = self.run_artifact(
            "--workspace", str(self.workspace), "authorize-write", "--issue", "114",
            "--path", "ci/manifest/WP-115.json", "--op", "write", "--issuer", "operator",
            "--issued-at", future,
        )
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn("is in the future", refused.stderr)
        self.assertNotIn("Traceback", refused.stderr)
        self.assertEqual(len(ledger.read_text().splitlines()), 1)

    def test_a_missing_crate_bootstraps_through_the_packaged_binary(self):
        """chainlink #115 end to end through the packaged artifact and
        `python3 -I` with no source tree: a port-mode pilot whose declared crate
        does not exist yet, bootstrapped with a capability record scoped to that
        one manifest and nothing else.

        #114's remedy is only real in the artifact users actually run, and #115
        is the half that turns it from "a human hand-wrote a protected file" into
        a reproducible bootstrap. So this test asserts the bundled
        `docs/scaffold-receipt-schema.json` is in the archive, that the receipt
        the packaged binary emitted validates against it, and -- the acceptance
        criterion itself -- that the `src/lib.rs` write needed no capability
        record at all, so nobody was granted raw `Cargo.toml` scope to get here.
        """
        with zipfile.ZipFile(self.artifact) as zf:
            bundled = {n for n in zf.namelist() if not n.endswith("/")}
        self.assertIn(
            "ligature_data/docs/scaffold-receipt-schema.json",
            bundled,
            "the scaffold receipt schema must ship: the bundle is derived from the inventory",
        )
        self.assertEqual(self.init_port().returncode, 0)
        descriptor = self.workspace / "project-descriptor.json"
        declared = json.loads(descriptor.read_text())
        # `init --mode port` already declares crates/<name>-core; make its
        # manifest a protected root, as a real project does.
        declared["write_set"] = {
            "allowed_roots": ["crates/*/src/", "crates/*/tests/"],
            "protected_roots": ["crates/*/specs/**", "ci/manifest/**", "crates/*/Cargo.toml"],
        }
        descriptor.write_text(json.dumps(declared, indent=2) + "\n")
        crate = declared["crates"][0]["crate_dir"]
        manifest_rel = f"{crate}/Cargo.toml"

        # 1. before: nothing exists, and the write this command would make is
        #    not authorized -- the refusal names the issuing command.
        refused = self.run_artifact(
            "--workspace", str(self.workspace), "scaffold-crate", "--issue", "115", "--crate", crate,
        )
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn("no active write grant covers", refused.stderr)
        self.assertIn("authorize-write --issue 115", refused.stderr)
        self.assertNotIn("Traceback", refused.stderr)
        self.assertFalse((self.workspace / manifest_rel).exists())

        # 2. the grant, for that one manifest and nothing else.
        issued = self.run_artifact(
            "--workspace", str(self.workspace), "authorize-write", "--issue", "115",
            "--path", manifest_rel, "--op", "write", "--issuer", "operator",
            "--one-shot", "--json",
        )
        self.assertEqual(issued.returncode, 0, issued.stdout + issued.stderr)
        grant = json.loads(issued.stdout)

        # 3. the scaffold: two files, one grant, and a receipt the bundled
        #    schema accepts.
        done = self.run_artifact(
            "--workspace", str(self.workspace), "scaffold-crate", "--issue", "115",
            "--crate", crate, "--json",
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        receipt = json.loads(done.stdout)
        self.assertEqual(
            [e.message for e in make_validator(SCAFFOLD_SCHEMA).iter_errors(receipt)], [],
            "the packaged binary emitted a receipt its own bundled schema rejects",
        )
        manifest_entry, skeleton_entry = receipt["generated"]
        self.assertEqual((manifest_entry["role"], manifest_entry["state"]), ("manifest", "created"))
        self.assertTrue(manifest_entry["protected"])
        self.assertEqual(manifest_entry["grant_id"], grant["grant_id"])
        self.assertEqual((skeleton_entry["role"], skeleton_entry["state"]),
                         ("source-skeleton", "created"))
        self.assertFalse(skeleton_entry["protected"])
        self.assertIsNone(skeleton_entry["grant_id"])
        self.assertTrue((self.workspace / manifest_rel).is_file())
        self.assertTrue((self.workspace / crate / "src" / "lib.rs").is_file())
        self.assertEqual(receipt["write_set"]["state"], "clean")

        # 4. idempotent: a re-run is `unchanged` and writes nothing.
        again = self.run_artifact(
            "--workspace", str(self.workspace), "scaffold-crate", "--issue", "115",
            "--crate", crate, "--json",
        )
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(json.loads(again.stdout)["outcome"], "unchanged")
        self.assertEqual(len(self.workspace.joinpath("ci/results/protected-writes.jsonl")
                             .read_text().splitlines()), 1)

        # 5. the fail-closed paths, same artifact: an undeclared crate, and a
        #    crate this workspace has no authority for.
        for name, fragment in (
            ("not-a-crate", "is not a crate this workspace's project descriptor declares"),
            ("../escape", "escapes the workspace"),
        ):
            with self.subTest(crate=name):
                proc = self.run_artifact(
                    "--workspace", str(self.workspace), "scaffold-crate",
                    "--issue", "115", "--crate", name,
                )
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertIn(fragment, proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)

    def test_typed_cargo_profile_configuration_runs_in_the_packaged_binary(self):
        """The new profile editor and its strict audit schema ship together."""
        with zipfile.ZipFile(self.artifact) as zf:
            bundled = {name for name in zf.namelist() if not name.endswith("/")}
        self.assertIn("configure_cargo_profile.py", bundled)
        self.assertIn("ligature_data/docs/cargo-profile-audit-schema.json", bundled)
        self.assertEqual(self.init_port().returncode, 0)

        descriptor = self.workspace / "project-descriptor.json"
        declared = json.loads(descriptor.read_text())
        declared["write_set"] = {
            "allowed_roots": ["crates/*/src/", "crates/*/tests/"],
            "protected_roots": ["Cargo.toml", "crates/*/Cargo.toml"],
        }
        descriptor.write_text(json.dumps(declared, indent=2) + "\n")
        manifest = self.workspace / "Cargo.toml"
        source = self.workspace / "src" / "lib.rs"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("pub fn marker() {}\n")
        manifest.write_text(
            "[package]\n"
            'name = "profile-pilot"\n'
            'version = "0.1.0"\n'
            'edition = "2021"\n'
            "\n"
            "[profile.release]\n"
            "opt-level = 1 # keep this comment\n"
            "debug = false\n"
        )
        original = manifest.read_text()

        issued = self.run_artifact(
            "--workspace", str(self.workspace), "authorize-write",
            "--issue", "121", "--path", "Cargo.toml", "--op", "write",
            "--issuer", "operator", "--one-shot", "--json",
        )
        self.assertEqual(issued.returncode, 0, issued.stdout + issued.stderr)
        grant = json.loads(issued.stdout)

        configured = self.run_artifact(
            "--workspace", str(self.workspace), "configure-cargo-profile",
            "--issue", "121", "--profile", "release", "--grant-id", grant["grant_id"],
            "--opt-level", "3", "--lto", "thin", "--json",
        )
        self.assertEqual(configured.returncode, 0, configured.stdout + configured.stderr)
        record = json.loads(configured.stdout)
        self.assertEqual(
            [error.message for error in make_validator(CARGO_PROFILE_SCHEMA).iter_errors(record)],
            [],
        )
        self.assertEqual(record["grant_id"], grant["grant_id"])
        self.assertEqual(record["profile"], "release")
        self.assertEqual(record["changes"], {"opt-level": 3, "lto": "thin"})
        self.assertEqual(record["validation"]["cargo_metadata"], "passed")
        self.assertNotIn("git_commit", record)
        self.assertNotIn("diff", record)
        self.assertIn("opt-level = 3 # keep this comment", manifest.read_text())
        self.assertIn("debug = false", manifest.read_text())
        self.assertTrue(manifest.read_text().startswith(original[: original.index("[profile.release]")]))
        self.assertFalse((self.workspace / "Cargo.lock").exists())

    def test_cargo_verus_crate_profile_runs_from_the_packaged_binary(self):
        """The crate-scoped command and its schema ship in the isolated zipapp."""
        with zipfile.ZipFile(self.artifact) as zf:
            bundled = {name for name in zf.namelist() if not name.endswith("/")}
        self.assertIn("configure_crate_profile.py", bundled)
        self.assertIn("ligature_data/docs/cargo-verus-profile-audit-schema.json", bundled)
        self.assertEqual(self.init_port().returncode, 0)

        descriptor_path = self.workspace / "project-descriptor.json"
        descriptor = json.loads(descriptor_path.read_text())
        descriptor["crates"] = [{
            "crate_dir": "rust/demo-core",
            "contracts_crate": "contracts",
            "specs_search_root": "rust",
        }]
        descriptor["write_set"] = {
            "allowed_roots": ["rust/*/src/", "rust/*/tests/"],
            "protected_roots": ["rust/*/Cargo.toml"],
        }
        descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n")

        crate = self.workspace / "rust/demo-core"
        crate.mkdir(parents=True)
        manifest = crate / "Cargo.toml"
        manifest.write_text(
            '[package]\nname = "demo-core"\nversion = "0.1.0"\nedition = "2021"\n'
        )
        crate_lock = crate / "Cargo.lock"
        crate_lock.write_text("# preserved\nversion = 4\n")

        fake_bin = self.workspace / "fake-bin"
        fake_bin.mkdir()
        cargo = fake_bin / "cargo"
        cargo.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys, tomllib\n"
            "args = sys.argv[1:]\n"
            "if args and args[0] == 'metadata':\n"
            "    manifest = pathlib.Path(args[args.index('--manifest-path') + 1]).resolve()\n"
            "    data = tomllib.loads(manifest.read_text())\n"
            "    package = data['package']\n"
            "    project_manifest = pathlib.Path.cwd() / 'Cargo.toml'\n"
            "    workspace_root = pathlib.Path.cwd() if project_manifest.is_file() and '[workspace]' in project_manifest.read_text() else manifest.parent\n"
            "    print(json.dumps({'workspace_root': str(workspace_root), 'packages': [{\n"
            "        'name': package['name'], 'version': package['version'],\n"
            "        'id': package['name'] + '@' + package['version'],\n"
            "        'manifest_path': str(manifest), 'metadata': package.get('metadata', {})\n"
            "    }]}))\n"
        )
        cargo.chmod(0o755)
        rustc = fake_bin / "rustc"
        rustc.write_text(
            "#!/usr/bin/env python3\n"
            "print('rustc 1.88.0 (fake)')\n"
            "print('host: x86_64-unknown-linux-gnu')\n"
            "print('release: 1.88.0')\n"
        )
        rustc.chmod(0o755)
        verus = fake_bin / "verus"
        verus.write_text(
            "#!/usr/bin/env python3\n"
            "print('{\"verus\": {\"version\": \"0.2025.10.0\", \"toolchain\": \"1.88.0-x86_64-unknown-linux-gnu\"}}')\n"
        )
        verus.chmod(0o755)
        vstd = fake_bin / "vstd"
        vstd.mkdir()
        (vstd / "Cargo.toml").write_text(
            '[package]\nname = "vstd"\nversion = "0.2025.10.0"\nedition = "2021"\n'
        )

        issued = self.run_artifact(
            "--workspace", str(self.workspace), "authorize-write",
            "--issue", "125", "--path", "rust/demo-core/Cargo.toml",
            "--op", "write", "--issuer", "operator", "--json",
        )
        self.assertEqual(issued.returncode, 0, issued.stdout + issued.stderr)
        grant = json.loads(issued.stdout)["grant_id"]
        original_lock = crate_lock.read_bytes()
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{fake_bin}{os.pathsep}{old_path}"
        try:
            configured = self.run_artifact(
                "--workspace", str(self.workspace), "configure-crate",
                "--issue", "125", "--crate", "demo-core",
                "--profile", "cargo-verus", "--grant-id", grant, "--json",
            )
        finally:
            os.environ["PATH"] = old_path
        self.assertEqual(configured.returncode, 0, configured.stdout + configured.stderr)
        record = json.loads(configured.stdout)
        self.assertEqual(list(make_validator(CARGO_VERUS_PROFILE_SCHEMA).iter_errors(record)), [])
        self.assertEqual(record["path"], "rust/demo-core/Cargo.toml")
        self.assertEqual(record["validation"]["cargo_lock_preserved"], "passed")
        self.assertEqual(crate_lock.read_bytes(), original_lock)
        self.assertFalse((self.workspace / "Cargo.toml").exists())
        self.assertFalse((self.workspace / "Cargo.lock").exists())

    def test_version_verify_runs_without_the_source_tree(self):
        proc = self.run_artifact("version", "--verify")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("attested: true", proc.stdout)
        self.assertIn(self.build_result["content_hash"], proc.stdout)
        # resources were located inside the copied archive, not the checkout
        self.assertIn(str(self.artifact), proc.stdout)
        self.assertNotIn(str(ROOT), proc.stdout)

    def test_schema_catalog_and_export_work_offline_from_the_packaged_binary(self):
        """#157: packaged users can discover, inspect, and export the closure
        and degradation schemas with the exact installed build identity, with
        no checkout or network fallback."""
        with zipfile.ZipFile(self.artifact) as zf:
            bundled = {name for name in zf.namelist() if not name.endswith("/")}
        self.assertIn("schema_cli.py", bundled)
        self.assertIn("ligature_data/docs/closure-profile-schema.json", bundled)
        self.assertIn("ligature_data/docs/degradation-record-schema.json", bundled)

        listing = self.run_artifact("schema", "list", "--json")
        self.assertEqual(listing.returncode, 0, listing.stdout + listing.stderr)
        catalog = json.loads(listing.stdout)
        self.assertEqual(catalog["format"], "ligature.schema-list.v1")
        self.assertTrue(catalog["adjudicator"]["verified"])
        self.assertTrue(catalog["product_version"])
        rows = {row["name"]: row for row in catalog["schemas"]}
        self.assertEqual(rows["closure-profile"]["related_schema_names"], ["degradation-record"])
        self.assertEqual(rows["degradation-record"]["related_schema_names"], ["closure-profile"])
        self.assertTrue(rows["closure-profile"]["sha256"].startswith("sha256:"))

        shown = self.run_artifact("schema", "show", "closure-profile", "--json")
        self.assertEqual(shown.returncode, 0, shown.stdout + shown.stderr)
        payload = json.loads(shown.stdout)
        self.assertEqual(payload["adjudicator"], catalog["adjudicator"])
        self.assertEqual(payload["schema"]["name"], "closure-profile")
        self.assertIn("$schema", payload["schema"]["document"])
        self.assertEqual([item["name"] for item in payload["related_schemas"]], ["degradation-record"])

        output = self.outside / "degradation-record.export.json"
        exported = self.run_artifact(
            "schema", "export", "degradation-record", "--output", str(output)
        )
        self.assertEqual(exported.returncode, 0, exported.stdout + exported.stderr)
        exported_payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(exported_payload["format"], "ligature.schema-export.v1")
        self.assertEqual(exported_payload["adjudicator"], catalog["adjudicator"])
        self.assertEqual(exported_payload["schema"]["name"], "degradation-record")
        self.assertEqual([item["name"] for item in exported_payload["related_schemas"]], ["closure-profile"])

        refused = self.run_artifact(
            "schema", "export", "degradation-record", "--output", str(output)
        )
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("refusing to overwrite", refused.stderr)

    def test_g14_scope_and_evidence_report_works_through_the_packaged_binary(self):
        """#159: both G14 CLI spellings report the evidence actually checked
        by the packaged artifact, and the shipped schema validates it without
        a source-checkout fallback."""
        with zipfile.ZipFile(self.artifact) as zf:
            bundled = {name for name in zf.namelist() if not name.endswith("/")}
        self.assertIn("ligature_data/docs/g14-report-schema.json", bundled)

        sys.path.insert(0, str(ROOT / "tests"))
        from test_gate_g14 import Workspace  # noqa: E402

        workspace_fixture = Workspace(self.workspace)
        schema = json.loads((ROOT / "docs" / "g14-report-schema.json").read_text())
        for args in (("gate-g14", "--json"), ("gate", "g14", "--json")):
            with self.subTest(args=args):
                proc = self.run_artifact("--workspace", str(self.workspace), *args)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                payload = json.loads(proc.stdout)
                errors = list(make_validator(schema).iter_errors(payload))
                self.assertEqual(errors, [], [error.message for error in errors])
                self.assertEqual(payload["format"], "ligature.g14-report.v1")
                cluster = payload["clusters"][0]
                self.assertEqual(cluster["work_packages"], ["WP-A", "WP-B", "WP-C"])
                self.assertEqual(cluster["scope"]["counts"]["evaluated_obligations"], 3)
                self.assertEqual(len(cluster["rechecked_proof_artifacts"]), 3)
                self.assertTrue(all(item["proof_artifact_references"] for item in cluster["rechecked_proof_artifacts"]))

        # #158: an isolated provided contract has no dependency edge, so it
        # must still count as declared and evaluated in the packaged result.
        workspace_fixture.patch(
            "ci/manifest/WP-A.json",
            lambda manifest: manifest["definition_of_done"].update({
                "required_guarantees": [], "required_preconditions_to_establish": [],
            }),
        )
        workspace_fixture.patch("ci/results/WP-A.json", lambda report: report["bridge_records"].clear())
        isolated = self.run_artifact("--workspace", str(self.workspace), "gate-g14", "--json")
        self.assertEqual(isolated.returncode, 0, isolated.stdout + isolated.stderr)
        scope = json.loads(isolated.stdout)["clusters"][0]["scope"]
        self.assertEqual(scope["kind"], "obligations-only")
        self.assertEqual(
            (scope["counts"]["provided_obligations"], scope["counts"]["required_obligations"],
             scope["counts"]["evaluated_obligations"]),
            (1, 0, 1),
        )

    def test_r1_g16_report_distinguishes_warning_acceptance_from_reconciliation(self):
        """#160: a real C_static value-domain inquiry may remain accepted by
        the default policy, but the packaged gate must say its interaction is
        undeclared and reconciliation is incomplete."""
        with zipfile.ZipFile(self.artifact) as zf:
            bundled = {name for name in zf.namelist() if not name.endswith("/")}
        self.assertIn("ligature_data/docs/r1-g16-report-schema.json", bundled)

        crate = self.workspace / "crates" / "scheduler"
        (crate / "src").mkdir(parents=True)
        (crate / "specs" / "_interactions").mkdir(parents=True)
        (crate / "src" / "lib.rs").write_text(
            """pub struct Month;\n\
pub struct Year;\n\
impl Month {\n\
    pub fn is_valid(year: u32) -> bool { Year::is_leap(year) }\n\
}\n\
impl Year {\n\
    pub fn is_leap(year: u32) -> bool { year % 4 == 0 }\n\
}\n"""
        )
        descriptor = json.loads((ROOT / "schemas" / "examples" / "project-descriptor.greenfield.example.json").read_text())
        descriptor["crates"] = [{
            "crate_dir": "crates/scheduler",
            "contracts_crate": "contracts",
            "specs_search_root": "crates",
        }]
        (self.workspace / "project-descriptor.json").write_text(json.dumps(descriptor))

        extracted = self.run_artifact(
            "--workspace", str(self.workspace), "extract-c-static", "--target", "x86_64-unknown-linux-gnu"
        )
        self.assertEqual(extracted.returncode, 0, extracted.stdout + extracted.stderr)
        result = self.run_artifact("--workspace", str(self.workspace), "gate-r1-g16", "--json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        schema = json.loads((ROOT / "docs" / "r1-g16-report-schema.json").read_text())
        errors = list(make_validator(schema).iter_errors(payload))
        self.assertEqual(errors, [], [error.message for error in errors])
        self.assertEqual(payload["gate_acceptance"], "accepted")
        self.assertEqual(payload["summary"]["source_resolution"], "complete")
        self.assertEqual(payload["summary"]["interaction_reconciliation"], "incomplete")
        self.assertEqual(payload["summary"]["counts"]["undeclared_warnings"], 1)

        text_result = self.run_artifact("--workspace", str(self.workspace), "gate", "r1-g16")
        self.assertEqual(text_result.returncode, 0, text_result.stdout + text_result.stderr)
        self.assertIn("Gate acceptance: accepted (exit 0)", text_result.stdout)
        self.assertIn("interaction reconciliation: incomplete", text_result.stdout)
        self.assertIn("undeclared warnings=1", text_result.stdout)

        descriptor["interaction_policy"] = {"require_complete_reconciliation": True}
        (self.workspace / "project-descriptor.json").write_text(json.dumps(descriptor))
        strict = self.run_artifact("--workspace", str(self.workspace), "gate-r1-g16", "--json")
        self.assertEqual(strict.returncode, 1, strict.stdout + strict.stderr)
        strict_report = json.loads(strict.stdout)
        self.assertEqual(strict_report["gate_acceptance"], "blocked")
        self.assertEqual(strict_report["summary"]["counts"]["undeclared_warnings"], 1)
        self.assertTrue(any(
            "require_complete_reconciliation" in finding["reason"]
            for finding in strict_report["findings"]
        ))

    def test_c_static_source_binding_fails_closed_through_packaged_gates(self):
        """#169: source, gates, and status/check in the exact zipapp all
        reject stale, pre-source, and legacy observations, while a fresh
        extraction remains consumable."""
        sys.path.insert(0, str(ROOT / "tests"))
        from test_gate_g14 import Workspace  # noqa: E402

        fixture = Workspace(self.workspace)
        source = self.workspace / "crates/scheduler/src/lib.rs"
        original_source = source.read_text()
        source.unlink()

        empty_extract = self.run_artifact(
            "--workspace", str(self.workspace), "extract-c-static", "--target", "x86_64-unknown-linux-gnu"
        )
        self.assertEqual(empty_extract.returncode, 0, empty_extract.stdout + empty_extract.stderr)
        self.assertIn("0 discovered", empty_extract.stdout)

        for command in ("gate-r1-g16", "gate-g14"):
            with self.subTest(stage="empty-observation", command=command):
                result = self.run_artifact("--workspace", str(self.workspace), command, "--json")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("reports no local concepts or call sites", result.stdout)
                self.assertIn("ligature extract-c-static --target x86_64-unknown-linux-gnu", result.stdout)

        source.write_text(original_source)
        for command in ("gate-r1-g16", "gate-g14"):
            with self.subTest(stage="source-added", command=command):
                result = self.run_artifact("--workspace", str(self.workspace), command, "--json")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("source binding is stale", result.stdout)
                self.assertIn("ligature extract-c-static --target x86_64-unknown-linux-gnu", result.stdout)

        fresh = self.run_artifact(
            "--workspace", str(self.workspace), "extract-c-static", "--target", "x86_64-unknown-linux-gnu"
        )
        self.assertEqual(fresh.returncode, 0, fresh.stdout + fresh.stderr)
        for command in ("gate-r1-g16", "gate-g14"):
            with self.subTest(stage="fresh", command=command):
                result = self.run_artifact("--workspace", str(self.workspace), command, "--json")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        source.write_text(original_source + "\nimpl Scheduler { pub fn edited(&self) {} }\n")
        for command in ("gate-r1-g16", "gate-g14"):
            with self.subTest(stage="edited-source", command=command):
                result = self.run_artifact("--workspace", str(self.workspace), command, "--json")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("source binding is stale", result.stdout)

        report_path = self.workspace / "ci/results/c_static/crates_scheduler.json"
        legacy = json.loads(report_path.read_text())
        legacy["schema_version"] = "1.0"
        legacy.pop("source_binding")
        report_path.write_text(json.dumps(legacy, indent=2))
        for command in ("gate-r1-g16", "gate-g14"):
            with self.subTest(stage="legacy-unbound", command=command):
                result = self.run_artifact("--workspace", str(self.workspace), command, "--json")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("legacy C_static report has no source binding", result.stdout)
                self.assertIn("ligature extract-c-static --target x86_64-unknown-linux-gnu", result.stdout)

        for command in ("check", "status"):
            with self.subTest(stage="read-model", command=command):
                result = self.run_artifact("--workspace", str(self.workspace), command, "--json")
                payload = json.loads(result.stdout)
                self.assertIn("legacy C_static report has no source binding", json.dumps(payload))

    def test_doctor_verifies_before_init(self):
        proc = self.run_artifact("--workspace", str(self.workspace), "doctor")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("attested: true", proc.stdout)
        self.assertIn("plan-driven Stage 7 manifest generation", proc.stdout)
        self.assertIn("automatic promoted-I/O synthesis", proc.stdout)
        self.assertIn("does not infer package scope", proc.stdout)

    def test_doctor_rejects_descriptor_write_set_overlap_from_the_artifact(self):
        self.assertEqual(self.init_port().returncode, 0)
        descriptor_path = self.workspace / "project-descriptor.json"
        descriptor = json.loads(descriptor_path.read_text())
        descriptor["write_set"]["allowed_roots"].append("docs/*.md")
        descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n")

        doctor = self.run_artifact("--workspace", str(self.workspace), "doctor")
        self.assertEqual(doctor.returncode, 2, doctor.stdout + doctor.stderr)
        for text in (
            "schema: invalid",
            "allowed_roots pattern 'docs/*.md'",
            "protected_roots pattern 'docs/reliance-policy.md'",
            "narrow or otherwise revise the patterns",
        ):
            self.assertIn(text, doctor.stdout)
        self.assertIn("installation: current", doctor.stdout)

    def test_fixture_value_acceptance_is_explicit_and_hash_bound_in_the_artifact(self):
        self.assertEqual(self.init_port().returncode, 0)
        descriptor_path = self.workspace / "project-descriptor.json"
        descriptor = json.loads(descriptor_path.read_text())
        descriptor["review"]["reviewer"] = "operator"
        descriptor["port_source"]["repository"] = "/srv/crypto-mixed"
        descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n")

        before = self.run_artifact("--workspace", str(self.workspace), "check", "--json")
        self.assertIn("port_source.oracle_build_command", before.stdout)
        accepted = self.run_artifact(
            "--workspace", str(self.workspace), "accept-fixture-value",
            "--path", "port_source.oracle_build_command", "--reviewer", "operator",
            "--accepted-at", "2026-10-01",
        )
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        descriptor = json.loads(descriptor_path.read_text())
        receipt = descriptor["review"]["accepted_fixture_values"][0]
        self.assertEqual(receipt["path"], "port_source.oracle_build_command")
        self.assertRegex(receipt["value_sha256"], r"^sha256:[a-f0-9]{64}$")

        after = self.run_artifact("--workspace", str(self.workspace), "check", "--json")
        self.assertNotIn("port_source.oracle_build_command", after.stdout)
        descriptor["review"]["accepted_fixture_values"][0]["value_sha256"] = "sha256:" + "0" * 64
        descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n")
        stale = self.run_artifact("--workspace", str(self.workspace), "check", "--json")
        self.assertIn("port_source.oracle_build_command", stale.stdout)

    def test_declared_crate_root_escape_is_rejected_by_packaged_check_and_extraction(self):
        self.assertEqual(self.init_port().returncode, 0)
        descriptor_path = self.workspace / "project-descriptor.json"
        descriptor = json.loads(descriptor_path.read_text())
        outside = self.workspace.parent / f"{self.workspace.name}-outside"
        outside.mkdir()
        try:
            (outside / "src").mkdir()
            (outside / "src" / "lib.rs").write_text(
                "pub fn outer(value: u8) -> u8 { inner(value) }\nfn inner(value: u8) -> u8 { value }\n"
            )
            original = descriptor["crates"][0]["crate_dir"]
            (self.workspace / original / "src").mkdir(parents=True, exist_ok=True)
            (self.workspace / original / "src" / "lib.rs").write_text(
                "pub fn local(value: u8) -> u8 { helper(value) }\nfn helper(value: u8) -> u8 { value }\n"
            )
            escape_link = self.workspace / "crates" / "outside-link"
            escape_link.parent.mkdir(parents=True, exist_ok=True)
            escape_link.symlink_to(outside, target_is_directory=True)

            for crate_dir in (str(outside), f"../{outside.name}", "crates/outside-link"):
                with self.subTest(crate_dir=crate_dir):
                    descriptor["crates"][0]["crate_dir"] = crate_dir
                    descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n")
                    check = self.run_artifact("--workspace", str(self.workspace), "check", "--json")
                    self.assertEqual(check.returncode, 5, check.stdout + check.stderr)
                    self.assertIn("crate_dir", check.stdout)
                    self.assertTrue(
                        "workspace-relative" in check.stdout or "resolves outside the workspace" in check.stdout,
                        check.stdout,
                    )
                    extract = self.run_artifact(
                        "--workspace", str(self.workspace), "extract-c-static",
                        "--target", "x86_64-unknown-linux-gnu",
                    )
                    self.assertEqual(extract.returncode, 1, extract.stdout + extract.stderr)
                    self.assertIn("crate_dir", extract.stderr)
                    self.assertFalse((self.workspace / "ci" / "results" / "c_static").exists())

            descriptor["crates"][0]["crate_dir"] = "crates/missing-core"
            descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n")
            missing = self.run_artifact("--workspace", str(self.workspace), "check", "--json")
            self.assertNotEqual(missing.returncode, 5, missing.stdout + missing.stderr)
            self.assertIn("does not exist or is not a directory", missing.stdout)
            missing_extract = self.run_artifact(
                "--workspace", str(self.workspace), "extract-c-static",
                "--target", "x86_64-unknown-linux-gnu",
            )
            self.assertEqual(missing_extract.returncode, 1, missing_extract.stdout + missing_extract.stderr)
            self.assertIn("crate root does not exist", missing_extract.stderr)

            descriptor["crates"][0]["crate_dir"] = original
            descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n")
            valid = self.run_artifact("--workspace", str(self.workspace), "check", "--json")
            self.assertNotEqual(valid.returncode, 5, valid.stdout + valid.stderr)

            inside_link = self.workspace / "crates" / "inside-link"
            inside_link.symlink_to(self.workspace / original, target_is_directory=True)
            descriptor["crates"][0]["crate_dir"] = "crates/inside-link"
            descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n")
            internal = self.run_artifact("--workspace", str(self.workspace), "check", "--json")
            self.assertNotEqual(internal.returncode, 5, internal.stdout + internal.stderr)
        finally:
            shutil.rmtree(outside)

    def test_installed_boundary_prompt_uses_project_policy_path(self):
        self.assertEqual(self.init_port().returncode, 0)
        relative = ".ligature/prompts/stage-3-boundary-drafting.md"
        prompt_path = self.workspace / relative
        prompt = prompt_path.read_text()
        self.assertIn("docs/reliance-policy.md", prompt)
        self.assertIn("the project file is the policy authority", prompt)
        self.assertNotIn("copied from docs/reliance-policy.template.md", prompt)

        stale = prompt.replace(
            "contents read from this project's", "copied from docs/reliance-policy.template.md"
        )
        prompt_path.write_text(stale)
        manifest_path = self.workspace / "ci" / "manifest" / "installation.json"
        manifest = json.loads(manifest_path.read_text())
        record = next(item for item in manifest["files"] if item["path"] == relative)
        stale_hash = "sha256:" + hashlib.sha256(stale.encode("utf-8")).hexdigest()
        record["base_hash"] = stale_hash
        record["expected_hash"] = stale_hash
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        migrated = self.run_artifact("--workspace", str(self.workspace), "migrate", "--upgrade")
        self.assertEqual(migrated.returncode, 0, migrated.stdout + migrated.stderr)
        repaired = prompt_path.read_text()
        self.assertIn("docs/reliance-policy.md", repaired)
        self.assertNotIn("copied from docs/reliance-policy.template.md", repaired)

    def test_draft_scope_refusal_happens_before_backend_invocation(self):
        self.assertEqual(self.init_port().returncode, 0)
        marker = self.workspace / "backend-was-invoked"
        backend = self.workspace / "backend.py"
        backend.write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('called')\n"
            "print('{}')\n"
        )
        descriptor_path = self.workspace / "project-descriptor.json"
        descriptor = json.loads(descriptor_path.read_text())
        descriptor["llm_backend"] = {
            "kind": "claude", "command": f"{sys.executable} {backend}"
        }
        descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n")
        target = self.workspace / "crates" / "myport-core" / "rogue" / "evil.json"
        proc = self.run_artifact(
            "--workspace", str(self.workspace), "draft", "0", "evidence-intake", str(target),
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("outside every allowed_roots pattern", proc.stderr)
        self.assertIn("before prompt rendering", proc.stderr)
        self.assertFalse(marker.exists())
        self.assertFalse(target.with_suffix(".json.draft").exists())

    def test_search_root_constraint_resolution_and_missing_id_guidance_from_the_artifact(self):
        self.assertEqual(self.init_port().returncode, 0)
        policy = self.run_artifact(
            "--workspace", str(self.workspace), "accept-policy", "--reviewer", "operator",
            "--version", "reliance-policy@1.0",
        )
        self.assertEqual(policy.returncode, 0, policy.stdout + policy.stderr)
        crate_specs = self.workspace / "crates" / "myport-core" / "specs"
        boundaries = crate_specs / "_boundaries"
        boundaries.mkdir(parents=True)
        concept = json.loads(
            (ROOT / "tests/fixtures/boundary_contracts/valid_crate/specs/task_queue.json").read_text()
        )
        (crate_specs / "task_queue.json").write_text(json.dumps(concept, indent=2) + "\n")
        boundary = json.loads(
            (ROOT / "tests/fixtures/boundary_contracts/valid_crate/specs/_boundaries/"
             "scheduler_dispatch__to__task_queue_pop_ready.json").read_text()
        )
        target = boundaries / "scheduler_dispatch__to__task_queue_pop_ready.json"
        target.write_text(json.dumps(boundary, indent=2) + "\n")

        resolved = self.run_artifact("--workspace", str(self.workspace), "status", "--json")
        self.assertEqual(resolved.returncode, 0, resolved.stdout + resolved.stderr)
        state = json.loads(resolved.stdout)
        self.assertFalse(any(
            finding["gate_id"] == "G2+" and "unverifiable" in finding["summary"]
            for finding in state["open_findings"]
        ))

        del concept["constraints"][0]["id"]
        (crate_specs / "task_queue.json").write_text(json.dumps(concept, indent=2) + "\n")
        missing_id = self.run_artifact("--workspace", str(self.workspace), "status", "--json")
        self.assertEqual(missing_id.returncode, 0, missing_id.stdout + missing_id.stderr)
        state = json.loads(missing_id.stdout)
        guidance = next(
            finding["summary"] for finding in state["open_findings"]
            if finding["gate_id"] == "G2+" and "stable id" in finding["summary"]
        )
        for text in (
            "TaskQueue.C003", "requested constraint id is 'C003'", "constraints[].id",
            "ligature draft 3 concept-to-code", "ligature approve --reviewer",
            "Do not derive the id from array order or source text",
        ):
            self.assertIn(text, guidance)

    def test_pending_human_ruling_is_the_next_action_in_packaged_check(self):
        initialized = self.run_artifact(
            "--workspace", str(self.workspace), "init", "--mode", "greenfield", "--name", "myport",
        )
        self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
        policy = self.run_artifact(
            "--workspace", str(self.workspace), "accept-policy", "--reviewer", "operator",
            "--version", "reliance-policy@1.0",
        )
        self.assertEqual(policy.returncode, 0, policy.stdout + policy.stderr)
        descriptor_path = self.workspace / "project-descriptor.json"
        descriptor = json.loads(descriptor_path.read_text())
        descriptor["review"]["reviewer"] = "operator"
        descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n")
        for field in ("review.reviewed_at",):
            accepted = self.run_artifact(
                "--workspace", str(self.workspace), "accept-fixture-value", "--path", field,
                "--reviewer", "operator", "--accepted-at", "2026-10-01",
            )
            self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        relative = "crates/myport-core/specs/_boundaries/scheduler_dispatch__to__task_queue_pop_ready.json"
        artifact = self.workspace / relative
        artifact.parent.mkdir(parents=True)
        boundary = json.loads(
            (ROOT / "tests/fixtures/boundary_contracts/valid_crate/specs/_boundaries/"
             "scheduler_dispatch__to__task_queue_pop_ready.json").read_text()
        )
        boundary["review"] = {"reviewer": "reviewer-label", "reviewed_at": "2026-08-25"}
        artifact.write_text(json.dumps(boundary, indent=2) + "\n")
        review_log = self.workspace / "ci" / "results" / "review_log.jsonl"
        review_log.parent.mkdir(parents=True)
        review_log.write_text(json.dumps({
            "target_path": str(artifact), "classification": "new", "reviewer": "reviewer-label",
            "reviewed_at": "2026-08-25", "validation": "checked",
            "logged_at": "2026-08-25T12:00:00+00:00",
        }) + "\n")

        check = self.run_artifact("--workspace", str(self.workspace), "check", "--json")
        self.assertIn(check.returncode, (1, 3), check.stdout + check.stderr)
        document = json.loads(check.stdout)
        action = document["next_action"]
        self.assertEqual(action["kind"], "human-decision")
        self.assertIsNone(action["command"])
        self.assertNotIn("action_id", action)
        self.assertIn(relative, action["description"])
        self.assertIn("ligature record-ruling", action["description"])
        self.assertNotIn("reviewer-label", action["description"])

    def test_interaction_draft_rejects_dangling_evidence_and_verifier_policy_mismatch(self):
        self.assertEqual(self.init_port().returncode, 0)
        descriptor_path = self.workspace / "project-descriptor.json"
        descriptor = json.loads(descriptor_path.read_text())
        descriptor["verifier_policy"] = {"default": "kani"}
        descriptor["write_set"] = {"allowed_roots": [], "protected_roots": ["crates/*/specs/**"]}
        backend = self.workspace / "backend.py"
        payload_path = self.workspace / "interaction.json"
        descriptor["llm_backend"] = {
            "kind": "claude", "command": f"{sys.executable} {backend}"
        }
        descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n")
        backend.write_text(
            "import json\nfrom pathlib import Path\n"
            f"print(Path({str(payload_path)!r}).read_text())\n"
        )
        specs = self.workspace / "crates" / "myport-core" / "specs"
        specs.mkdir(parents=True)
        for name, concept in (("task_queue", "TaskQueue"), ("scheduler", "Scheduler")):
            source = json.loads(
                (ROOT / "tests/fixtures/boundary_contracts/valid_crate/specs/task_queue.json").read_text()
            )
            source["concept"] = concept
            source["verifier"] = "kani"
            (specs / f"{name}.json").write_text(json.dumps(source, indent=2) + "\n")
        payload = json.loads(
            (ROOT / "tests/fixtures/interactions/valid/specs/_interactions/I-SCHED-TQ-001.json").read_text()
        )
        payload.pop("review", None)
        payload["evidence_links"] = ["E-0143"]
        payload["reliances"][0]["required_assurance"]["accepted_evidence_kinds"] = [
            "creusot-deductive-check"
        ]
        payload_path.write_text(json.dumps(payload))
        target = specs / "_interactions" / "I-SCHED-TQ-021.json"
        common = (
            "--workspace", str(self.workspace), "draft", "3", "interaction-drafting", str(target),
            "--var", "interaction_id=I-SCHED-TQ-021", "--var", "caller_concept=Scheduler",
            "--var", "caller_method=dispatch", "--var", "callee_concept=TaskQueue",
            "--var", "callee_method=pop_ready", "--var", "caller_spec={}", "--var", "callee_spec={}",
            "--var", "target_triple=x86_64-unknown-linux-gnu", "--var", "prior_artifact=(none)",
            "--var", "finding=(none)",
        )
        dangling = self.run_artifact(*common)
        self.assertEqual(dangling.returncode, 1, dangling.stdout + dangling.stderr)
        self.assertIn("[G4/error]", dangling.stdout)
        self.assertIn("E-0143", dangling.stdout)
        self.assertFalse(target.with_suffix(".json.draft").exists())

        evidence = json.loads((ROOT / "tests/fixtures/evidence/valid/evidence/E-0143.json").read_text())
        evidence_dir = self.workspace / "evidence"
        evidence_dir.mkdir()
        (evidence_dir / "E-0143.json").write_text(json.dumps(evidence, indent=2) + "\n")
        payload["interaction_id"] = "I-SCHED-TQ-022"
        payload_path.write_text(json.dumps(payload))
        target = specs / "_interactions" / "I-SCHED-TQ-022.json"
        common = tuple(
            str(value).replace("I-SCHED-TQ-021", "I-SCHED-TQ-022") if isinstance(value, str) else value
            for value in common
        )
        policy = self.run_artifact(*common)
        self.assertEqual(policy.returncode, 1, policy.stdout + policy.stderr)
        self.assertIn("G2-policy", policy.stdout)
        self.assertIn("kani", policy.stdout.lower())
        self.assertFalse(target.with_suffix(".json.draft").exists())

    def test_compile_time_evidence_is_preserved_without_becoming_assurance(self):
        self.assertEqual(self.init_port().returncode, 0)
        descriptor_path = self.workspace / "project-descriptor.json"
        descriptor = json.loads(descriptor_path.read_text())
        descriptor["write_set"] = {"allowed_roots": ["evidence/**"], "protected_roots": []}
        backend = self.workspace / "backend.py"
        backend.write_text(
            "import json\nprint(json.dumps({"
            "'schema_version':'1.0','id':'E-9002','kind':'compile-time',"
            "'claim':'compiler assertion for queue capacity',"
            "'origin':{'repository':'https://example.com/repro','commit':'a1b2c3d',"
            "'symbol':'TaskQueue::CAPACITY_ASSERTION','path':'src/queue.rs',"
            "'content_hash':'sha256:" + "1" * 64 + "','line_hint':'24-31'},"
            "'semantic_disposition':'required','lifecycle':'accepted','confidence':'high','mode':'R'}))\n"
        )
        descriptor["llm_backend"] = {
            "kind": "claude", "command": f"{sys.executable} {backend}"
        }
        descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n")
        target = self.workspace / "evidence" / "E-9002.json"
        draft = self.run_artifact(
            "--workspace", str(self.workspace), "draft", "0", "evidence-intake", str(target),
            "--var", "source_material=compile-time assertion source and preserved compiler output",
            "--var", "project_descriptor={}", "--var", "prior_artifact=(none)", "--var", "finding=(none)",
        )
        self.assertEqual(draft.returncode, 0, draft.stdout + draft.stderr)
        staged = target.with_suffix(".json.draft")
        staged_data = json.loads(staged.read_text())
        self.assertEqual(staged_data["kind"], "compile-time")
        self.assertEqual(staged_data["origin"]["content_hash"], "sha256:" + "1" * 64)
        promoted = self.run_artifact("--workspace", str(self.workspace), "promote-evidence", str(target))
        self.assertEqual(promoted.returncode, 0, promoted.stdout + promoted.stderr)
        record = json.loads(target.read_text())
        self.assertEqual(record["kind"], "compile-time")
        self.assertNotIn("review", record)

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
        # chainlink #84: the artifact alone must also ship -- and `init`
        # must install -- the two scripts G13 hash-pins. The bundle keeps
        # them under ligature_data/ (what resource_root() exposes) as well
        # as at the archive root (what the product imports); if that
        # second copy were missing, only this run would notice.
        for rel in ("scripts/witness_renderer.py", "scripts/xml_escape.py"):
            installed = self.workspace / rel
            self.assertTrue(installed.is_file(), rel)
            self.assertEqual(installed.read_bytes(), (ROOT / rel).read_bytes(), rel)

        status = self.run_artifact("--workspace", str(self.workspace), "status", "--json")
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        document = json.loads(status.stdout)
        self.assertEqual(document["gate_integrity"]["state"], "pinned")
        self.assertEqual(document["binary_identity"]["verified"], "true")
        self.assertEqual(document["binary_identity"]["content_hash"], self.build_result["content_hash"])
        # chainlink #72: the ownership manifest init just wrote is
        # ownership metadata, not a work-package artifact -- status must
        # not classify it as an invalid work-package.
        self.assertEqual(document["installation_manifest"]["state"], "current")
        self.assertFalse(
            [a for a in document["artifacts"] if a["path"] == "ci/manifest/installation.json"]
        )

    def test_invalid_descriptor_diagnostic_from_the_artifact_alone(self):
        """chainlink #73 end-to-end through the packaged binary: an invalid
        project descriptor must produce a finding naming the offending
        property (JSON path + permitted alternatives) and a non-null
        next_action, and status gate integrity must report the descriptor
        as present-but-invalid rather than absent."""
        self.assertEqual(self.init_port().returncode, 0)
        descriptor = json.loads((self.workspace / "project-descriptor.json").read_text())
        descriptor["port_source"]["commit"] = "abc123"
        (self.workspace / "project-descriptor.json").write_text(json.dumps(descriptor))

        status = self.run_artifact("--workspace", str(self.workspace), "status", "--json")
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        document = json.loads(status.stdout)
        self.assertEqual(document["descriptor"]["state"], "present-invalid")
        self.assertIn("present but invalid", document["gate_integrity"]["details"])
        self.assertIn("commit", document["gate_integrity"]["details"])

        check = self.run_artifact("--workspace", str(self.workspace), "check", "--json")
        # chainlink #75: the invalid descriptor makes gate integrity
        # `unknown`, so check also fails the installation/gate-integrity
        # gate -- exit 5 with gate_integrity_failed alongside invalid_input.
        self.assertEqual(check.returncode, 5, check.stdout + check.stderr)
        document = json.loads(check.stdout)
        self.assertEqual(document["result"]["exit_code"], 5)
        self.assertIn("gate_integrity_failed", document["result"]["conditions"])
        self.assertTrue(document["findings"])
        self.assertIn("$.port_source", document["findings"][0]["reason"])
        self.assertIn("permitted: language, oracle_build_command, repository", document["findings"][0]["reason"])
        self.assertIsNotNone(document["next_action"])
        self.assertEqual(document["next_action"]["kind"], "human-decision")

    def test_descriptor_typo_gets_a_did_you_mean_from_the_artifact_alone(self):
        """chainlink #106 end-to-end through the packaged binary: a
        near-miss key and a key the schema never had produced a
        byte-identical `unexpected property '<key>'` reason, so
        recovering the intended key meant eyeballing the whole permitted
        list. The packaged binary must now say which key the typo looks
        like -- and must still not guess at the key with no near miss."""
        self.assertEqual(self.init_port().returncode, 0)
        path = self.workspace / "project-descriptor.json"
        descriptor = json.loads(path.read_text())

        descriptor["closure_kinds"] = ["deductive"]
        path.write_text(json.dumps(descriptor))
        check = self.run_artifact("--workspace", str(self.workspace), "check", "--json")
        self.assertEqual(check.returncode, 5, check.stdout + check.stderr)
        reason = json.loads(check.stdout)["findings"][0]["reason"]
        self.assertIn("unexpected property 'closure_kinds'", reason)
        self.assertIn("did you mean 'closure_kind'?", reason)

        del descriptor["closure_kinds"]
        descriptor["zzz_not_a_real_field"] = "x"
        path.write_text(json.dumps(descriptor))
        check = self.run_artifact("--workspace", str(self.workspace), "check", "--json")
        self.assertEqual(check.returncode, 5, check.stdout + check.stderr)
        reason = json.loads(check.stdout)["findings"][0]["reason"]
        self.assertIn("unexpected property 'zzz_not_a_real_field'", reason)
        self.assertNotIn("did you mean", reason)

    def test_declared_closure_kind_validates_from_the_artifact_alone(self):
        """chainlink #73 gap 1 end-to-end: the descriptor schema the
        packaged binary enforces accepts a declared closure_kind, and
        status reads it back."""
        self.assertEqual(self.init_port().returncode, 0)
        descriptor = json.loads((self.workspace / "project-descriptor.json").read_text())
        descriptor["closure_kind"] = "bounded"
        (self.workspace / "project-descriptor.json").write_text(json.dumps(descriptor))

        status = self.run_artifact("--workspace", str(self.workspace), "status", "--json")
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        document = json.loads(status.stdout)
        self.assertEqual(document["descriptor"]["state"], "present-valid")
        self.assertEqual(document["descriptor"]["closure_kind"], "bounded")

    def test_verifier_policy_typo_is_rejected_by_the_artifact_alone(self):
        """chainlink #104 end-to-end through the packaged binary: the issue's
        exact repro. `{"default": "creusot", "defualt": "kani"}` validated,
        `check` exited 0 with `findings: []`, and the typo was
        indistinguishable from a real key -- and a `fallback` key that 1.1.1
        had rejected regressed to accepted. Both must now be refused, with
        the diagnostic naming the permitted keys and the near miss."""
        self.assertEqual(self.init_port().returncode, 0)
        path = self.workspace / "project-descriptor.json"
        descriptor = json.loads(path.read_text())

        descriptor["verifier_policy"]["defualt"] = "kani"
        path.write_text(json.dumps(descriptor))
        check = self.run_artifact("--workspace", str(self.workspace), "check", "--json")
        self.assertEqual(check.returncode, 5, check.stdout + check.stderr)
        document = json.loads(check.stdout)
        self.assertIn("invalid_input", document["result"]["conditions"])
        reason = document["findings"][0]["reason"]
        self.assertIn("unexpected property 'defualt'", reason)
        self.assertIn("permitted: clusters, default, supporting", reason)
        self.assertIn("did you mean 'default'?", reason)

        # The key that 1.1.1 refused and 1.2.x accepted again.
        del descriptor["verifier_policy"]["defualt"]
        descriptor["verifier_policy"]["fallback"] = "creusot"
        path.write_text(json.dumps(descriptor))
        check = self.run_artifact("--workspace", str(self.workspace), "check", "--json")
        self.assertEqual(check.returncode, 5, check.stdout + check.stderr)
        document = json.loads(check.stdout)
        self.assertIn("invalid_input", document["result"]["conditions"])
        self.assertIn("unexpected property 'fallback'", document["findings"][0]["reason"])

    def test_a_per_cluster_override_still_validates_through_the_artifact(self):
        """The property chainlink #104 had to preserve when it closed the
        object: a per-cluster override is still expressible, under
        `clusters`, and `status --json` reads it back -- so closing the
        object did not cost the P3 crypto-mixed pilot (`verus` + `kani`) the
        only thing it was ever able to declare."""
        self.assertEqual(self.init_port().returncode, 0)
        path = self.workspace / "project-descriptor.json"
        descriptor = json.loads(path.read_text())
        descriptor["verifier_policy"] = {
            "default": "verus",
            "clusters": {"crypto-mixed": "kani"},
            "supporting": ["kani"],
        }
        path.write_text(json.dumps(descriptor))
        self.assertEqual(list(make_validator(DESCRIPTOR_SCHEMA).iter_errors(descriptor)), [])

        status = self.run_artifact("--workspace", str(self.workspace), "status", "--json")
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        document = json.loads(status.stdout)
        self.assertEqual(document["descriptor"]["state"], "present-valid")
        self.assertEqual(
            document["descriptor"]["verifier_policy"],
            {"default": "verus", "clusters": {"crypto-mixed": "kani"}, "supporting": ["kani"]},
        )

    def test_a_cluster_key_that_is_not_a_cluster_name_is_rejected(self):
        """The one deliberately open object left in the descriptor, and the
        reason its keys are constrained: `Crypto_Mixed` could never name a
        cluster in any artifact, so accepting it as an override would mean
        accepting a declaration nothing will ever read."""
        self.assertEqual(self.init_port().returncode, 0)
        path = self.workspace / "project-descriptor.json"
        descriptor = json.loads(path.read_text())
        descriptor["verifier_policy"]["clusters"] = {"Crypto_Mixed": "kani"}
        path.write_text(json.dumps(descriptor))
        check = self.run_artifact("--workspace", str(self.workspace), "check", "--json")
        self.assertEqual(check.returncode, 5, check.stdout + check.stderr)
        reason = json.loads(check.stdout)["findings"][0]["reason"]
        self.assertIn("$.verifier_policy.clusters.Crypto_Mixed", reason)

    def test_a_missing_staged_draft_is_one_line_through_the_packaged_binary(self):
        """chainlink #102 end-to-end through the packaged binary, which is
        where the date-creusot pilot met it (`date-ligature-101`, mirrored
        from full-ligature-port chainlink #106): `approve`,
        `approve-pair` and `approve-exemption-pair` read `<target>.draft`
        without checking it was there, so a typo'd target or a re-run of an
        approval whose draft the first one consumed came out as a
        ~15-frame `FileNotFoundError` traceback naming a file the caller was
        never told about. Absolute target paths are required here: a target
        resolves against the process cwd, and this class deliberately runs
        from outside the checkout."""
        self.assertEqual(self.init_port().returncode, 0)
        specs = self.workspace / "crates" / "myport-core" / "specs"
        boundary = specs / "_boundaries" / "does-not-exist.json"
        interaction = specs / "_interactions" / "I-probe.json"
        debt = specs / "_protocol_debt" / "I-probe.json"
        exemption = specs / "_exemptions" / "I-probe.json"
        for directory in (boundary.parent, interaction.parent, debt.parent, exemption.parent):
            directory.mkdir(parents=True, exist_ok=True)

        cases = (
            ("approve", (boundary,), 1),
            ("approve-pair", (interaction, debt), 2),
            ("approve-exemption-pair", (interaction, exemption), 2),
        )
        for verb, targets, promoted in cases:
            with self.subTest(command=verb):
                proc = self.run_artifact(
                    "--workspace", str(self.workspace), verb,
                    *(str(target) for target in targets), "--reviewer", "x",
                )
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertEqual(proc.stdout, "")
                (line,) = proc.stderr.strip().splitlines()
                self.assertTrue(line.startswith("error: nothing was promoted: no staged draft at "), line)
                self.assertNotIn("Traceback", proc.stderr)
                # Every draft the command would have read is named, so the
                # paired verbs cannot report half the reason.
                for target in targets:
                    self.assertIn(f"{target}.draft", line)
                self.assertIn(f"promotes all {promoted} drafts" if promoted > 1 else
                              "promotes an already-staged draft", line)
                for target in targets:
                    self.assertFalse(target.exists())

        # The pilot's own control, in the same release and the same tree:
        # `promote-evidence` already refused this same condition, which is
        # what made the three promote verbs' silence a defect rather than a
        # design choice.
        evidence = self.workspace / "evidence" / "E-99.json"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        proc = self.run_artifact("--workspace", str(self.workspace), "promote-evidence", str(evidence))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("no staged draft at", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

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

    def adopt_current_descriptor_shape(self) -> None:
        """Move the workspace's descriptor onto the current
        `verifier_policy` shape, the way an operator upgrading an existing
        workspace has to.

        `old_build`'s `init` writes the pre-chainlink-#104 shape -- a
        per-cluster override as a sibling key of `default` -- which the
        current schema rejects. That is the point of #104 (an arbitrary
        key beside `default` is indistinguishable from a typo), not a
        defect in this test, and it is deliberately NOT papered over by
        the product: the descriptor is user-owned, so `migrate` reports a
        normative user-owned document as drifted rather than rewriting it
        (chainlink #78), and the load-time diagnostic names the permitted
        keys. These tests are about the adjudicator pin, so they bring the
        descriptor current themselves and say so."""
        path = self.workspace / "project-descriptor.json"
        descriptor = json.loads(path.read_text())
        policy = descriptor.get("verifier_policy", {})
        overrides = {
            key: value
            for key, value in policy.items()
            if key not in ("default", "clusters", "supporting")
        }
        if overrides:
            policy.setdefault("clusters", {}).update(overrides)
            for key in overrides:
                del policy[key]
            descriptor["verifier_policy"] = policy
            path.write_text(json.dumps(descriptor, indent=2) + "\n")

    def stamp_policy(self, build: dict) -> subprocess.CompletedProcess:
        """`accept-policy --version` against the packaged artifact: `init`
        installs the reliance policy template with its `Policy version:`
        marker still a placeholder, which `doctor` treats as a blocking
        condition (chainlink #113), so a test asserting `doctor` exit 0 has
        to perform that one human step first -- through the artifact, which
        is also the only place the packaged binary's own copy of the command
        is exercised."""
        return self.run_artifact(
            build["artifact"],
            "accept-policy", "--reviewer", "alice", "--version", "reliance-policy@1.0",
        )

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
        self.adopt_current_descriptor_shape()
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
        self.adopt_current_descriptor_shape()
        self.assertEqual(self.init(self.new_build).returncode, 1)  # refused
        proc = self.run_artifact(self.new_build["artifact"], "migrate", "--upgrade")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.pinned_hash(), self.new_build["content_hash"])
        # Stamped through the NEW artifact: `old_build` predates
        # `accept-policy --version` entirely, and the policy template it
        # installs is still the one `doctor` (chainlink #113) refuses to
        # call clean.
        stamped = self.stamp_policy(self.new_build)
        self.assertEqual(stamped.returncode, 0, stamped.stdout + stamped.stderr)

        doctor = self.run_artifact(self.new_build["artifact"], "doctor")
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        self.assertIn("adjudicator pin: pinned", doctor.stdout)


if __name__ == "__main__":
    unittest.main()
