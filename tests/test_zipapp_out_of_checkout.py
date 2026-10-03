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

        doctor = self.run_artifact(self.new_build["artifact"], "doctor")
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        self.assertIn("adjudicator pin: pinned", doctor.stdout)


if __name__ == "__main__":
    unittest.main()
