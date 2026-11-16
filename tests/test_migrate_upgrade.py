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
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRIOR = ROOT / "releases" / "v1.1.1" / "ligature.pyz"
PIPELINE = ROOT / "scripts" / "pipeline.py"
MANIFEST = "ci/manifest/installation.json"
GATE_SCHEMA = ".ligature/schemas/project-state.schema.json"
WORK_PACKAGE_FIXTURE = ROOT / "tests" / "fixtures" / "work_packages" / "valid" / "ci" / "manifest" / "WP-SCHED-001.json"
CONCEPT_SPEC_FIXTURE = ROOT / "tests" / "fixtures" / "boundary_contracts" / "valid_crate" / "specs" / "task_queue.json"


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
        # The old template used recursive patterns for Cargo/build/Kani
        # files. The current descriptor contract rejects those because they
        # overlap the explicitly writable src/ and tests/ trees. Preserve the
        # same intended protected files with precise crate-root patterns.
        write_set = data.get("write_set", {})
        protected = set(write_set.get("protected_roots", []))
        protected.discard("**/Cargo.toml")
        protected.discard("**/build.rs")
        protected.discard("**/kani.toml")
        protected.update(
            {
                "Cargo.toml",
                "crates/*/Cargo.toml",
                "build.rs",
                "crates/*/build.rs",
                "kani.toml",
                "crates/*/kani.toml",
            }
        )
        write_set["protected_roots"] = sorted(protected)
        # A real reviewer value, not the shipped example placeholder.
        if isinstance(data.get("review"), dict):
            data["review"]["reviewer"] = "alice"
            data["review"]["reviewed_at"] = date.today().isoformat()
        descriptor.write_text(json.dumps(data, indent=2) + "\n")
        # Current check also reports missing declared crate roots as a Stage
        # 8A prerequisite. Keep this migration fixture focused on installation
        # state by providing the empty project crate it declares.
        for crate in data.get("crates", []):
            crate_root = self.workspace / crate["crate_dir"]
            src = crate_root / "src"
            src.mkdir(parents=True, exist_ok=True)
            (src / "lib.rs").write_text("// migration fixture crate\n")

    def test_upgrade_refreshes_gate_integrity_and_audit_is_machine_readable(self):
        self.init_prior()
        descriptor_bytes = (self.workspace / "project-descriptor.json").read_bytes()
        policy_bytes = (self.workspace / "docs" / "reliance-policy.md").read_bytes()
        old_manifest = self.manifest()
        old_gate_hashes = dict(old_manifest["gate_hashes"])
        self.assertEqual(old_manifest["installed_product_version"], "1.1.1")

        proc = self.run_cli("migrate", "--upgrade", "--json")
        # chainlink #175: the write succeeds, but the pre-#104 descriptor is
        # schema-invalid under this release, so migrate exits fail-closed and
        # names the repair in the audit.
        self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
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
        self.assertEqual(audit["conflicts"], [])

        # chainlink #175: the user-owned descriptor is not written; its
        # manifest record was the tenth changed path. The audit says so, and
        # it surfaces the descriptor's post-upgrade schema invalidity instead
        # of leaving that to a later doctor run.
        self.assertNotIn("project-descriptor.json", audit["files_written"])
        self.assertEqual(audit["user_owned_files"]["written"], [])
        self.assertEqual(by_path["project-descriptor.json"]["change_kind"], "manifest-record")
        self.assertFalse(by_path["project-descriptor.json"]["file_written"])
        post = audit["post_upgrade_validation"]
        self.assertEqual(post["state"], "findings")
        self.assertEqual(post["descriptor"]["state"], "invalid")
        self.assertEqual(
            [f["path"] for f in post["findings"]],
            ["project-descriptor.json"],
        )
        self.assertTrue(
            any(item["path"] == "project-descriptor.json" for item in audit["required_human_action"])
        )

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
        # The pre-#104 descriptor is invalid under this release, so the
        # successful write still exits fail-closed with the repair named.
        self.assertEqual(first.returncode, 1)
        first_audit = json.loads(first.stdout)
        self.assertEqual(first_audit["post_upgrade_validation"]["state"], "findings")
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
        self.assertEqual(proc.returncode, 1, proc.stderr)
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


class PostUpgradeValidationTest(unittest.TestCase):
    """chainlink #175: `migrate --upgrade` reports what the upgrade did not
    write but a newer release now rejects, and says which user-owned
    changes were real writes versus ownership-manifest record updates."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(PIPELINE), "--workspace", str(self.workspace), *args],
            cwd=self.workspace,
            capture_output=True,
            text=True,
            check=False,
        )

    def init(self) -> None:
        proc = self.run_cli("init", "--mode", "greenfield", "--name", "myproj")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

    def _write_invalid_work_package(self) -> None:
        path = self.workspace / "ci" / "manifest" / "WP-SCHED-001.json"
        data = json.loads(WORK_PACKAGE_FIXTURE.read_text())
        # The 1.4.3 #173 breaking change: arbitrary function identifiers are
        # no longer accepted as a harness declaration.
        data["definition_of_done"]["provided_guarantees"][0][
            "harness"
        ] = "from_parts_accepts_exactly_canonical_identifiers"
        path.write_text(json.dumps(data, indent=2) + "\n")

    def _write_pre_105_concept_spec(self) -> None:
        path = self.workspace / "crates" / "task_queue.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(CONCEPT_SPEC_FIXTURE.read_text())
        # Pre-#105 specs predate the required review block.
        data.pop("review", None)
        path.write_text(json.dumps(data, indent=2) + "\n")

    def test_audit_names_invalid_descriptor_work_package_and_concept_spec(self):
        self.init()
        descriptor = self.workspace / "project-descriptor.json"
        original_descriptor = descriptor.read_bytes()
        data = json.loads(descriptor.read_text())
        # The 1.3.4 #133 disjoint write-set rule: an allowed root that can
        # name the same path as a protected root invalidates the descriptor.
        data["write_set"]["protected_roots"].append("crates/*/src/**")
        descriptor.write_text(json.dumps(data, indent=2) + "\n")
        self._write_invalid_work_package()
        self._write_pre_105_concept_spec()

        proc = self.run_cli("migrate", "--upgrade", "--json")
        self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
        audit = json.loads(proc.stdout)
        self.assertEqual(audit["result"], "current")
        post = audit["post_upgrade_validation"]
        self.assertEqual(post["state"], "findings")
        self.assertEqual(post["descriptor"]["state"], "invalid")
        self.assertEqual(post["summary"]["descriptor"], 1)
        self.assertEqual(post["summary"]["work_package_manifests"], 1)
        self.assertEqual(post["summary"]["concept_specs"], 1)
        surfaces = {finding["surface"] for finding in post["findings"]}
        self.assertEqual(
            surfaces,
            {"descriptor", "work-package-manifest", "concept-spec"},
        )
        paths = {finding["path"] for finding in post["findings"]}
        self.assertEqual(
            paths,
            {
                "project-descriptor.json",
                "ci/manifest/WP-SCHED-001.json",
                "crates/task_queue.json",
            },
        )
        self.assertTrue(
            any("overlaps" in finding["reason"] for finding in post["descriptor"]["findings"])
        )
        work_package = post["work_package_manifests"][0]
        self.assertEqual(work_package["state"], "invalid")
        self.assertTrue(
            any("unsupported verifier/harness" in finding["reason"] for finding in work_package["findings"])
        )
        concept = post["concept_specs"][0]
        self.assertEqual(concept["state"], "invalid")
        self.assertTrue(
            any("'review' is a required property" in finding["reason"] for finding in concept["findings"])
        )
        rha_paths = {item["path"] for item in audit["required_human_action"]}
        self.assertIn("project-descriptor.json", rha_paths)
        self.assertIn("ci/manifest/WP-SCHED-001.json", rha_paths)
        self.assertIn("crates/task_queue.json", rha_paths)
        self.assertTrue(
            any("`ligature check --json`" in item["action"] for item in audit["required_human_action"])
        )

        # Once the three user-authored inputs are repaired, a rerun is
        # clean and exits 0: the fail-closed exit was the finding, not the
        # successful upgrade write.
        descriptor.write_bytes(original_descriptor)
        (self.workspace / "ci" / "manifest" / "WP-SCHED-001.json").unlink()
        (self.workspace / "crates" / "task_queue.json").unlink()
        proc = self.run_cli("migrate", "--upgrade", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        audit = json.loads(proc.stdout)
        self.assertEqual(audit["post_upgrade_validation"]["state"], "clean")
        self.assertEqual(audit["required_human_action"], [])

    def test_user_owned_descriptor_is_rebased_not_rewritten(self):
        self.init()
        descriptor = self.workspace / "project-descriptor.json"
        data = json.loads(descriptor.read_text())
        data["review"]["reviewer"] = "alice"
        descriptor.write_text(json.dumps(data, indent=2) + "\n")
        original = descriptor.read_bytes()

        proc = self.run_cli("migrate", "--upgrade", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        audit = json.loads(proc.stdout)
        self.assertEqual(descriptor.read_bytes(), original)
        self.assertNotIn("project-descriptor.json", audit["files_written"])
        self.assertEqual(audit["user_owned_files"]["written"], [])
        self.assertIn("project-descriptor.json", audit["user_owned_files"]["records_rebased"])
        changed = {entry["path"]: entry for entry in audit["changed_paths"]}
        self.assertFalse(changed["project-descriptor.json"]["file_written"])
        self.assertEqual(changed["project-descriptor.json"]["change_kind"], "manifest-record")

        proc = self.run_cli("migrate", "--upgrade", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        audit = json.loads(proc.stdout)
        self.assertEqual(audit["changed_paths"], [])
        self.assertEqual(audit["files_written"], [])
        self.assertEqual(audit["user_owned_files"]["written"], [])
        self.assertEqual(audit["user_owned_files"]["records_rebased"], [])


if __name__ == "__main__":
    unittest.main()
