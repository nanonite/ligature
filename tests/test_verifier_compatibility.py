"""Tested verifier compatibility matrix and per-root toolchain selection (#174).

The tests pin the properties the issue asks for:

* the matrix is a versioned, evidence-linked record; its entry hashes,
  source identities, lock identities and Creusot pair states are recomputed
  rather than trusted;
* `version`/`doctor` surface the exact tested matrix for the running build;
* a project descriptor can select a tested entry per independent root, and
  `toolchain check` rechecks pair compatibility, lock ids and a committed
  flake.lock;
* intake-only, synthetic and project-level results stay distinct, and the
  unresolved 0.14 declared shell is an explicit non-selectable mismatch.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import adjudicator  # noqa: E402
import pipeline  # noqa: E402
import verifier_matrix  # noqa: E402

CREUSOT_ENTRY = "creusot-0.12.0-dev-nightly-2026-04-21-x86_64-linux"
VERUS_ENTRY = "verus-0.2026.05.24.ecee80a-rust-1.95.0-x86_64-linux"
KANI_ENTRY = "kani-0.67.0-cbmc-6.8.0-rust-1.95.0-x86_64-linux"
MISMATCH_ENTRY = "creusot-0.14.0-dev-declared-shell-x86_64-linux"


def run_cli(*args: str) -> tuple[int, str, str]:
    stdout, stderr = StringIO(), StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = pipeline.main(list(args))
    return code, stdout.getvalue(), stderr.getvalue()


class MatrixDocumentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matrix = verifier_matrix.load_matrix()
        cls.by_id = verifier_matrix.entries_by_id(cls.matrix)

    def test_matrix_is_versioned_and_published_by_this_build(self):
        self.assertEqual(self.matrix["format"], verifier_matrix.MATRIX_FORMAT)
        self.assertEqual(self.matrix["published_by"]["product_name"], "ligature")
        self.assertEqual(self.matrix["published_by"]["product_version"], adjudicator.PRODUCT_VERSION)

    def test_retained_evidence_hashes_match(self):
        self.assertEqual(verifier_matrix.evidence_errors(), [])

    def test_each_tested_entry_keeps_result_classes_and_exclusions(self):
        for entry in self.by_id.values():
            with self.subTest(entry=entry["id"]):
                self.assertTrue(entry["exclusions"])
                self.assertTrue(entry["evidence"])
                for item in entry["evidence"]:
                    self.assertIn(item["result_class"], verifier_matrix.RESULT_CLASSES)
                    self.assertTrue(item["path"].startswith("docs/verifier-compatibility-evidence/"))
                if entry["status"] == "tested":
                    self.assertTrue(any(entry["coverage"].values()))
                if not entry["coverage"]["project_level"]:
                    self.assertTrue(any("production" in value.lower() for value in entry["exclusions"]))

    def test_source_identity_and_lock_identity_are_recomputed(self):
        for entry in self.by_id.values():
            with self.subTest(entry=entry["id"]):
                self.assertEqual(entry["source_identity"], verifier_matrix.source_identity(entry))
                nix = entry.get("nix")
                if nix is not None:
                    self.assertEqual(nix["lock_id"], verifier_matrix.nix_identity(nix))

    def test_creusot_pair_is_exact_and_the_declared_shell_is_a_mismatch(self):
        compatible = self.by_id[CREUSOT_ENTRY]
        self.assertEqual(verifier_matrix.creusot_pair_state(compatible)[0], "compatible")
        mismatch = self.by_id[MISMATCH_ENTRY]
        state, detail = verifier_matrix.creusot_pair_state(mismatch)
        self.assertEqual(state, "mismatch")
        self.assertIn("0.14.0-dev", detail)
        self.assertIn("0.14.0", detail)
        self.assertFalse(mismatch["selectable"])
        self.assertEqual(mismatch["status"], "unverified")

    def test_tampered_source_identity_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            matrix = json.loads(json.dumps(self.matrix))
            matrix["entries"][0]["source_identity"] = "sha256:" + "0" * 64
            (root / "docs" / "verifier-compatibility-matrix.json").write_text(
                json.dumps(matrix, indent=2) + "\n"
            )
            (root / "docs" / "verifier-compatibility-matrix-schema.json").write_bytes(
                (ROOT / "docs" / "verifier-compatibility-matrix-schema.json").read_bytes()
            )
            with self.assertRaises(verifier_matrix.MatrixError) as raised:
                verifier_matrix.load_matrix(root)
        self.assertIn("source_identity does not match its pins", str(raised.exception))

    def test_measured_versions_match_the_retained_narrative(self):
        creusot = self.by_id[CREUSOT_ENTRY]["toolchain"]
        self.assertEqual(creusot["primary"]["version"], "0.12.0-dev")
        self.assertIn(
            {"name": "creusot-std", "version": "0.12.0-dev", "role": "verifier-std"},
            creusot["supporting_tools"],
        )
        self.assertEqual(creusot["rust"]["toolchain"], "nightly-2026-04-21")
        self.assertEqual(self.by_id[VERUS_ENTRY]["toolchain"]["primary"]["version"], "0.2026.05.24.ecee80a")
        self.assertEqual(self.by_id[VERUS_ENTRY]["toolchain"]["rust"]["toolchain"], "1.95.0")
        self.assertEqual(self.by_id[KANI_ENTRY]["toolchain"]["primary"]["version"], "0.67.0")
        self.assertIn(
            {"name": "CBMC", "version": "6.8.0", "role": "model-checker"},
            self.by_id[KANI_ENTRY]["toolchain"]["supporting_tools"],
        )


class VersionAndDoctorMatrixTest(unittest.TestCase):
    def test_source_checkout_identity_carries_the_matrix(self):
        info = adjudicator.current_identity()
        self.assertIsNotNone(info["verifier_matrix"], info.get("verifier_matrix_error"))
        self.assertEqual(info["matrix_evidence_errors"], [])
        rendered = adjudicator.render_identity_text(info)
        self.assertIn("verifier matrix:", rendered)
        self.assertIn(CREUSOT_ENTRY, rendered)
        self.assertIn("synthetic:true", rendered)
        self.assertIn("pair=mismatch", rendered)

    def test_version_reports_the_matrix(self):
        code, stdout, stderr = run_cli("version")
        self.assertEqual(code, 0, stderr)
        self.assertIn("verifier matrix:", stdout)
        self.assertIn(CREUSOT_ENTRY, stdout)
        self.assertIn("0.14.0-dev", stdout)

    def test_doctor_reports_the_matrix_even_without_an_installed_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            _code, stdout, stderr = run_cli("--workspace", tmp, "doctor")
        self.assertIn("verifier matrix:", stdout)
        self.assertIn(CREUSOT_ENTRY, stdout)
        self.assertNotIn("Traceback", stderr)


class ToolchainCommandTest(unittest.TestCase):
    def test_list_json_exposes_tested_and_unverified_entries(self):
        code, stdout, stderr = run_cli("toolchain", "list", "--json")
        self.assertEqual(code, 0, stderr)
        document = json.loads(stdout)
        self.assertEqual(document["format"], verifier_matrix.MATRIX_FORMAT)
        by_id = {entry["id"]: entry for entry in document["entries"]}
        self.assertTrue(by_id[CREUSOT_ENTRY]["selectable"])
        self.assertFalse(by_id[MISMATCH_ENTRY]["selectable"])
        self.assertEqual(by_id[MISMATCH_ENTRY]["compatibility"]["state"], "mismatch")

    def test_show_rejects_an_unknown_entry(self):
        code, _stdout, stderr = run_cli("toolchain", "show", "not-a-real-entry")
        self.assertEqual(code, 2)
        self.assertIn("unknown compatibility-matrix entry", stderr)

    def test_plan_and_lock_emit_the_same_identity(self):
        code, plan_text, stderr = run_cli("toolchain", "plan", "--entry", CREUSOT_ENTRY)
        self.assertEqual(code, 0, stderr)
        self.assertIn("pair: compatible", plan_text)
        self.assertIn("nix lock:", plan_text)
        self.assertIn("creusot-std source NAR:", plan_text)

        code, plan_json, stderr = run_cli("toolchain", "plan", "--entry", CREUSOT_ENTRY, "--json")
        self.assertEqual(code, 0, stderr)
        plan = json.loads(plan_json)
        self.assertEqual(plan["entry"]["id"], CREUSOT_ENTRY)
        self.assertEqual(plan["source_identity"], plan["entry"]["source_identity"])
        self.assertEqual(plan["lock_id"], plan["entry"]["nix"]["lock_id"])

        code, lock_json, stderr = run_cli("toolchain", "lock", "--entry", CREUSOT_ENTRY)
        self.assertEqual(code, 0, stderr)
        lock = json.loads(lock_json)
        self.assertEqual(lock["lock_id"], plan["lock_id"])
        self.assertEqual(lock["source_identity"], plan["source_identity"])
        self.assertEqual(lock["store"], plan["entry"]["nix"]["store"])
        self.assertIsNone(lock["lock"])
        self.assertIn("nix store wrapper:", plan_text)

    def test_plan_refuses_the_unverified_declared_shell(self):
        code, _stdout, stderr = run_cli("toolchain", "plan", "--entry", MISMATCH_ENTRY)
        self.assertEqual(code, 2)
        self.assertIn("not a tested, selectable toolchain", stderr)

    def test_plan_without_entry_requires_a_descriptor_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _stdout, stderr = run_cli(
                "--workspace", tmp, "toolchain", "plan", "--verifier", "creusot"
            )
        self.assertEqual(code, 2)
        self.assertIn("cannot read project descriptor", stderr)


class DescriptorSelectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.descriptor_path = self.workspace / "project-descriptor.json"
        self.descriptor = json.loads(
            (ROOT / "schemas" / "examples" / "project-descriptor.greenfield.example.json").read_text()
        )
        self.matrix = verifier_matrix.load_matrix()
        by_id = verifier_matrix.entries_by_id(self.matrix)
        self.creusot = by_id[CREUSOT_ENTRY]
        self.verus = by_id[VERUS_ENTRY]
        self.lock_path = self.workspace / "flake.lock"

    def tearDown(self):
        self.tmp.cleanup()

    def write_descriptor(self, selections: list[dict]):
        self.descriptor["toolchains"] = selections
        self.descriptor_path.write_text(json.dumps(self.descriptor, indent=2) + "\n")

    def selection(self, **overrides):
        selection = {
            "project_root": ".",
            "verifier": "creusot",
            "matrix_entry": CREUSOT_ENTRY,
            "lock_id": self.creusot["nix"]["lock_id"],
        }
        selection.update(overrides)
        return selection

    def verus_selection(self, **overrides):
        selection = {
            "project_root": ".",
            "verifier": "verus",
            "matrix_entry": VERUS_ENTRY,
            "lock_id": self.verus["nix"]["lock_id"],
            "nix": {"lockfile": "flake.lock"},
        }
        selection.update(overrides)
        return selection

    def write_lock(self, lock=None):
        self.lock_path.write_text(json.dumps(lock or self.verus["nix"]["lock"], indent=2) + "\n")

    def test_valid_store_identity_selection_passes(self):
        self.write_descriptor([self.selection()])
        code, stdout, stderr = run_cli(
            "--workspace", str(self.workspace), "toolchain", "check"
        )
        self.assertEqual(code, 0, stdout + stderr)
        self.assertIn("toolchains: ok", stdout)

    def test_valid_flake_lock_selection_passes(self):
        self.write_descriptor([self.verus_selection()])
        self.write_lock()
        code, stdout, stderr = run_cli(
            "--workspace", str(self.workspace), "toolchain", "check"
        )
        self.assertEqual(code, 0, stdout + stderr)
        self.assertIn("toolchains: ok", stdout)

    def test_no_selection_is_not_an_error(self):
        self.write_descriptor([])
        code, stdout, stderr = run_cli(
            "--workspace", str(self.workspace), "toolchain", "check", "--json"
        )
        self.assertEqual(code, 0, stdout + stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["state"], "ok")
        self.assertEqual(payload["selection_count"], 0)

    def test_wrong_lock_id_is_refused(self):
        self.write_descriptor([self.selection(lock_id="sha256:" + "b" * 64)])
        code, stdout, stderr = run_cli(
            "--workspace", str(self.workspace), "toolchain", "check", "--json"
        )
        self.assertEqual(code, 1, stdout + stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["state"], "invalid")
        self.assertTrue(any("lock_id" in error for error in payload["errors"]))

    def test_changed_flake_lock_is_refused(self):
        self.write_descriptor([self.verus_selection()])
        changed = json.loads(json.dumps(self.verus["nix"]["lock"]))
        changed["nodes"]["nixpkgs"]["locked"]["narHash"] = "sha256-" + "A" * 43 + "="
        self.write_lock(changed)
        code, stdout, stderr = run_cli(
            "--workspace", str(self.workspace), "toolchain", "check"
        )
        self.assertEqual(code, 1, stdout + stderr)
        self.assertIn("locked identity differs", stdout)

    def test_extra_project_lock_inputs_are_allowed(self):
        self.write_descriptor([self.verus_selection()])
        extended = json.loads(json.dumps(self.verus["nix"]["lock"]))
        extended["nodes"]["project-extra"] = {
            "locked": {"narHash": "sha256-" + "B" * 43 + "=", "type": "github"},
            "original": {"type": "github"},
        }
        extended["nodes"]["root"]["inputs"]["project-extra"] = "project-extra"
        self.write_lock(extended)
        code, stdout, stderr = run_cli(
            "--workspace", str(self.workspace), "toolchain", "check"
        )
        self.assertEqual(code, 0, stdout + stderr)
        self.assertIn("toolchains: ok", stdout)

    def test_store_identity_entry_cannot_declare_a_flake_lockfile(self):
        self.write_descriptor([self.selection(nix={"lockfile": "flake.lock"})])
        code, stdout, stderr = run_cli(
            "--workspace", str(self.workspace), "toolchain", "check"
        )
        self.assertEqual(code, 1, stdout + stderr)
        self.assertIn("has no flake lock; pin lock_id instead", stdout)

    def test_independent_roots_can_select_different_verifiers(self):
        self.write_descriptor([
            self.selection(project_root=".", verifier="creusot", matrix_entry=CREUSOT_ENTRY),
            {
                "project_root": "crates/verus",
                "verifier": "verus",
                "matrix_entry": VERUS_ENTRY,
                "lock_id": self.verus["nix"]["lock_id"],
            },
        ])
        code, stdout, stderr = run_cli(
            "--workspace", str(self.workspace), "toolchain", "check"
        )
        self.assertEqual(code, 0, stdout + stderr)
        self.assertIn("toolchains: ok", stdout)

    def test_duplicate_root_and_verifier_are_refused(self):
        self.write_descriptor([self.selection(), self.selection()])
        code, stdout, stderr = run_cli(
            "--workspace", str(self.workspace), "toolchain", "check"
        )
        self.assertEqual(code, 1, stdout + stderr)
        self.assertIn("repeats the selection", stdout)

    def test_unverified_entry_cannot_be_selected(self):
        self.write_descriptor([
            {
                "project_root": ".",
                "verifier": "creusot",
                "matrix_entry": MISMATCH_ENTRY,
            }
        ])
        code, stdout, stderr = run_cli(
            "--workspace", str(self.workspace), "toolchain", "check"
        )
        self.assertEqual(code, 1, stdout + stderr)
        self.assertIn("not a tested, selectable toolchain", stdout)

    def test_verifier_must_match_the_matrix_entry(self):
        self.write_descriptor([self.selection(verifier="kani")])
        code, stdout, stderr = run_cli(
            "--workspace", str(self.workspace), "toolchain", "check"
        )
        self.assertEqual(code, 1, stdout + stderr)
        self.assertIn("does not match matrix entry", stdout)

    def test_selection_must_be_workspace_relative(self):
        self.write_descriptor([self.selection(project_root="../outside")])
        code, stdout, stderr = run_cli(
            "--workspace", str(self.workspace), "toolchain", "check"
        )
        self.assertEqual(code, 1, stdout + stderr)
        self.assertIn("workspace-relative", stdout)


class CreusotPairUnitTest(unittest.TestCase):
    def entry(self, primary: str, std: str):
        return {
            "verifier": "creusot",
            "toolchain": {
                "primary": {"name": "cargo-creusot", "version": primary},
                "supporting_tools": [
                    {"name": "creusot-std", "version": std, "role": "verifier-std"}
                ],
                "rust": {"toolchain": "nightly"},
            },
        }

    def test_matching_pair_is_compatible(self):
        self.assertEqual(verifier_matrix.creusot_pair_state(self.entry("1.0.0", "1.0.0"))[0], "compatible")

    def test_release_versus_dev_is_a_mismatch(self):
        state, detail = verifier_matrix.creusot_pair_state(self.entry("0.14.0-dev", "0.14.0"))
        self.assertEqual(state, "mismatch")
        self.assertIn("does not exactly match", detail)

    def test_missing_or_duplicate_std_is_a_mismatch(self):
        entry = self.entry("1.0.0", "1.0.0")
        entry["toolchain"]["supporting_tools"] = []
        self.assertEqual(verifier_matrix.creusot_pair_state(entry)[0], "mismatch")
        entry = self.entry("1.0.0", "1.0.0")
        entry["toolchain"]["supporting_tools"].append(
            {"name": "creusot-std", "version": "1.0.0", "role": "verifier-std"}
        )
        self.assertEqual(verifier_matrix.creusot_pair_state(entry)[0], "mismatch")


if __name__ == "__main__":
    unittest.main()
