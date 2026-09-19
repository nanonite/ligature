"""Canonical assumption registry (chainlink #59).

Real on-disk workspaces, matching tests/test_ligature_install.py's style.
Exercises the staged migration explicitly: phase A (registry present,
nothing else changes; old workspaces unaffected), phase B (dual resolution
+ non-blocking deprecation for the legacy composite), phase C
(deterministic reference migration, reviewed boundaries untouched), and the
phase-D version mechanism. Also covers old-only / new-only / mixed
workspaces and hash invalidation when registry text changes.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import assumption_registry as ar  # noqa: E402
import pipeline  # noqa: E402
from schema_utils import make_validator  # noqa: E402

REGISTRY_SCHEMA = json.loads((ROOT / "docs" / "assumption-registry-schema.json").read_text())
BOUNDARY_SCHEMA = json.loads((ROOT / "docs" / "boundary-contract-schema.json").read_text())
VALID_BOUNDARY = json.loads(
    (
        ROOT / "tests" / "fixtures" / "boundary_contracts" / "valid_crate" / "specs" / "_boundaries"
        / "scheduler_dispatch__to__task_queue_pop_ready.json"
    ).read_text()
)

TEXT = "The scheduler dispatches exactly one ready task per tick."
HASH = ar.canonical_assumption_hash(TEXT)
OTHER_TEXT = "The scheduler may dispatch more than one ready task per tick."
OTHER_HASH = ar.canonical_assumption_hash(OTHER_TEXT)


def entry(assumption_id="ASM-scheduler-dispatch", text=TEXT, tracking_issue="chainlink:713", content_hash=None, provenance=None):
    return {
        "assumption_id": assumption_id,
        "canonical_text": text,
        "content_hash": content_hash if content_hash is not None else ar.canonical_assumption_hash(text),
        "tracking_issue": tracking_issue,
        "provenance": provenance or [{"action": "added", "actor": "alice", "at": "2026-09-18"}],
    }


def registry_doc(*entries, schema_version="1.0"):
    return {"schema_version": schema_version, "entries": list(entries)}


def legacy_ref(boundary_id="scheduler_dispatch__to__task_queue_pop_ready", tracking_issue="chainlink:713", assumption_hash=None):
    return {
        "boundary_id": boundary_id,
        "tracking_issue": tracking_issue,
        "assumption_hash": assumption_hash if assumption_hash is not None else HASH,
    }


def boundary_with(*refs, boundary_id="scheduler_dispatch__to__task_queue_pop_ready"):
    assumptions = []
    for r in refs:
        if "assumption_id" in r:
            assumptions.append(dict(r))
        else:
            assumptions.append(dict(r, boundary_id=r.get("boundary_id", boundary_id)))
    return {"boundary_id": boundary_id, "assumptions": assumptions}


def manifest_with(*refs, name="WP-1"):
    return {
        "work_package": name,
        "definition_of_done": {
            "trusted_assumptions": [
                {"assumption_ref": r, "risk": "high", "mitigations": [{"kind": "human-risk-acceptance", "reference": "X"}]}
                for r in refs
            ]
        },
    }


class WorkspaceFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def write_registry(self, *entries, filename="registry.json", schema_version="1.0"):
        path = self.workspace / "specs" / "_assumptions" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(registry_doc(*entries, schema_version=schema_version), indent=2))
        return path

    def write_manifest(self, data, name="WP-1.json"):
        path = self.workspace / "ci" / "manifest" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
        return path

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = pipeline.main(["--workspace", str(self.workspace), *args])
        return code, out.getvalue(), err.getvalue()

    def check(self, boundaries=None, manifests=None):
        return ar.check_assumption_registry(self.workspace, boundaries=boundaries, manifests=manifests)

    def errors(self, findings):
        return [f for f in findings if f.severity == "error"]

    def infos(self, findings):
        return [f for f in findings if f.severity == "info"]


class CanonicalHashTest(unittest.TestCase):
    def test_hash_is_stable_across_whitespace_and_line_endings(self):
        self.assertEqual(
            ar.canonical_assumption_hash("a  \r\nb \r\n"),
            ar.canonical_assumption_hash("a\nb"),
        )

    def test_hash_changes_when_the_text_changes(self):
        self.assertNotEqual(ar.canonical_assumption_hash(TEXT), ar.canonical_assumption_hash(OTHER_TEXT))

    def test_valid_registry_document_passes_its_schema(self):
        validator = make_validator(REGISTRY_SCHEMA)
        errors = list(validator.iter_errors(registry_doc(entry())))
        self.assertEqual(errors, [], [e.message for e in errors])

    def test_schema_rejects_a_bad_assumption_id(self):
        validator = make_validator(REGISTRY_SCHEMA)
        self.assertTrue(list(validator.iter_errors(registry_doc(entry(assumption_id="not-an-id")))))


class PhaseAOldWorkspaceTest(WorkspaceFixture):
    def test_no_registry_means_no_findings_even_with_composite_refs(self):
        """Old-workspace-only: the registry is not adopted yet, so the gate
        adds nothing and the existing composite mechanism stays authoritative."""
        findings = self.check(
            boundaries=[boundary_with(legacy_ref())],
            manifests=[(Path("ci/manifest/WP-1.json"), manifest_with(legacy_ref()))],
        )
        self.assertEqual(findings, [])

    def test_composite_reference_still_validates_against_the_extended_schema(self):
        validator = make_validator(BOUNDARY_SCHEMA)
        doc = json.loads(json.dumps(VALID_BOUNDARY))
        doc["assumptions"] = [legacy_ref()]
        errors = list(validator.iter_errors(doc))
        self.assertEqual(errors, [], [e.message for e in errors])


class PhaseBNewWorkspaceTest(WorkspaceFixture):
    def test_registry_id_reference_resolves_cleanly(self):
        self.write_registry(entry())
        findings = self.check(
            boundaries=[boundary_with({"assumption_id": "ASM-scheduler-dispatch"})],
            manifests=[(Path("ci/manifest/WP-1.json"), manifest_with({"assumption_id": "ASM-scheduler-dispatch"}))],
        )
        self.assertEqual(self.errors(findings), [])
        self.assertEqual(findings, [])

    def test_registry_id_reference_is_schema_valid(self):
        validator = make_validator(BOUNDARY_SCHEMA)
        doc = json.loads(json.dumps(VALID_BOUNDARY))
        doc["assumptions"] = [{"assumption_id": "ASM-scheduler-dispatch"}]
        errors = list(validator.iter_errors(doc))
        self.assertEqual(errors, [], [e.message for e in errors])

    def test_dangling_registry_id_is_an_error(self):
        self.write_registry(entry())
        findings = self.check(boundaries=[boundary_with({"assumption_id": "ASM-does-not-exist"})])
        self.assertEqual(len(self.errors(findings)), 1)
        self.assertIn("dangling", self.errors(findings)[0].reason)

    def test_ambiguous_id_across_registry_files_is_an_error(self):
        self.write_registry(entry(), filename="a.json")
        self.write_registry(entry(text=OTHER_TEXT), filename="b.json")
        findings = self.check(boundaries=[boundary_with({"assumption_id": "ASM-scheduler-dispatch"})])
        reasons = " ".join(f.reason for f in self.errors(findings))
        self.assertIn("ambiguous", reasons)

    def test_registry_entry_hash_disagreeing_with_its_text_is_an_error(self):
        self.write_registry(entry(content_hash="sha256:" + "0" * 64))
        findings = self.check()
        self.assertTrue(any("recomputed" in f.reason for f in self.errors(findings)))

    def test_composite_missing_from_the_registry_is_an_error(self):
        self.write_registry(entry())
        findings = self.check(boundaries=[boundary_with(legacy_ref(tracking_issue="chainlink:999"))])
        self.assertTrue(any("missing" in f.reason for f in self.errors(findings)))

    def test_composite_hash_disagreeing_with_registry_is_an_error(self):
        self.write_registry(entry(text=TEXT))
        # Same tracking_issue, but the composite declares a different hash.
        findings = self.check(boundaries=[boundary_with(legacy_ref(assumption_hash=OTHER_HASH))])
        self.assertTrue(any("hash-disagreeing" in f.reason for f in self.errors(findings)))

    def test_legacy_composite_is_a_non_blocking_deprecation(self):
        self.write_registry(entry())
        findings = self.check(boundaries=[boundary_with(legacy_ref())])
        self.assertEqual(self.errors(findings), [])
        deprecations = self.infos(findings)
        self.assertEqual(len(deprecations), 1)
        self.assertIn("deprecation", deprecations[0].reason)
        self.assertIn("legacy composite", deprecations[0].reason)


class MixedWorkspaceTest(WorkspaceFixture):
    def test_mixed_composite_and_registry_refs_only_deprecate_the_composite(self):
        self.write_registry(entry())
        findings = self.check(
            boundaries=[
                boundary_with(legacy_ref(), boundary_id="a__to__b"),
                boundary_with({"assumption_id": "ASM-scheduler-dispatch"}, boundary_id="c__to__d"),
            ]
        )
        self.assertEqual(self.errors(findings), [])
        self.assertEqual(len(self.infos(findings)), 1)
        self.assertIn("legacy composite", self.infos(findings)[0].reason)


class PhaseCMigrationTest(WorkspaceFixture):
    def test_dry_run_reports_but_does_not_write(self):
        self.write_registry(entry())
        manifest_path = self.write_manifest(manifest_with(legacy_ref()))
        before = manifest_path.read_text()
        report = ar.migrate_assumption_refs(self.workspace)
        self.assertFalse(report.apply)
        self.assertEqual(len(report.changed), 1)
        self.assertIn("ASM-scheduler-dispatch", report.changed[0].new_ref["assumption_id"])
        self.assertEqual(manifest_path.read_text(), before)

    def test_apply_requires_a_reviewer(self):
        self.write_registry(entry())
        self.write_manifest(manifest_with(legacy_ref()))
        with self.assertRaises(ar.RegistryError):
            ar.migrate_assumption_refs(self.workspace, apply=True)

    def test_apply_rewrites_generated_work_package_references(self):
        self.write_registry(entry())
        manifest_path = self.write_manifest(manifest_with(legacy_ref()))
        report = ar.migrate_assumption_refs(self.workspace, apply=True, reviewer="alice")
        self.assertEqual(report.reviewer, "alice")
        rewritten = json.loads(manifest_path.read_text())
        ref = rewritten["definition_of_done"]["trusted_assumptions"][0]["assumption_ref"]
        self.assertEqual(ref, {"assumption_id": "ASM-scheduler-dispatch"})
        # The rewritten reference now validates and resolves with no findings.
        self.assertEqual(self.check(manifests=[(manifest_path, rewritten)]), [])

    def test_migration_is_idempotent(self):
        self.write_registry(entry())
        manifest_path = self.write_manifest(manifest_with(legacy_ref()))
        ar.migrate_assumption_refs(self.workspace, apply=True, reviewer="alice")
        after = manifest_path.read_text()
        second = ar.migrate_assumption_refs(self.workspace, apply=True, reviewer="alice")
        self.assertEqual(second.changed, [])
        self.assertEqual(manifest_path.read_text(), after)

    def test_reviewed_boundary_content_is_never_rewritten(self):
        self.write_registry(entry())
        boundary_path = self.workspace / "crates" / "a" / "specs" / "_boundaries" / "a__to__b.json"
        boundary_path.parent.mkdir(parents=True, exist_ok=True)
        boundary_path.write_text(json.dumps(boundary_with(legacy_ref(), boundary_id="a__to__b")))
        before = boundary_path.read_text()
        report = ar.migrate_assumption_refs(self.workspace, apply=True, reviewer="alice")
        self.assertEqual(boundary_path.read_text(), before)
        self.assertTrue(report.requires_human_approval)
        self.assertEqual(report.requires_human_approval[0][0], boundary_path)

    def test_cli_dry_run_and_apply(self):
        self.write_registry(entry())
        manifest_path = self.write_manifest(manifest_with(legacy_ref()))

        code, out, _ = self.run_cli("migrate", "--assumptions")
        self.assertEqual(code, 0)
        self.assertIn("dry-run", out)
        self.assertIn("rewrite", out)

        code, _, err = self.run_cli("migrate", "--assumptions", "--apply")
        self.assertEqual(code, 2)
        self.assertIn("--reviewer", err)

        code, out, _ = self.run_cli("migrate", "--assumptions", "--apply", "--reviewer", "alice")
        self.assertEqual(code, 0)
        ref = json.loads(manifest_path.read_text())["definition_of_done"]["trusted_assumptions"][0]["assumption_ref"]
        self.assertEqual(ref, {"assumption_id": "ASM-scheduler-dispatch"})

    def test_migration_without_a_registry_is_refused(self):
        self.write_manifest(manifest_with(legacy_ref()))
        code, _, err = self.run_cli("migrate", "--assumptions", "--apply", "--reviewer", "alice")
        self.assertEqual(code, 2)
        self.assertIn("no assumption registry", err)


class PhaseDVersionTest(WorkspaceFixture):
    def test_version_support_mechanism(self):
        self.assertTrue(ar.legacy_refs_supported("1.0"))
        self.assertFalse(ar.legacy_refs_supported("2.0"))
        self.assertFalse(ar.legacy_refs_supported("not-a-version"))

    def test_future_major_registry_is_refused_and_legacy_refs_error(self):
        self.write_registry(entry(), schema_version="2.0")
        findings = self.check(boundaries=[boundary_with(legacy_ref())])
        reasons = " ".join(f.reason for f in self.errors(findings))
        self.assertIn("not supported", reasons)
        self.assertIn("no longer supported", reasons)

    def test_migration_refuses_a_future_major_registry(self):
        self.write_registry(entry(), schema_version="2.0")
        self.write_manifest(manifest_with(legacy_ref()))
        code, _, err = self.run_cli("migrate", "--assumptions", "--apply", "--reviewer", "alice")
        self.assertEqual(code, 2)
        self.assertIn("refusing", err)


class PromotionHashInvalidationTest(WorkspaceFixture):
    def test_editing_registry_text_without_resigning_is_an_error(self):
        self.write_registry(entry())
        path = self.workspace / "specs" / "_assumptions" / "registry.json"
        doc = json.loads(path.read_text())
        doc["entries"][0]["canonical_text"] = OTHER_TEXT  # text changed, hash not re-signed
        path.write_text(json.dumps(doc))
        findings = self.check()
        self.assertTrue(any("recomputed" in f.reason for f in self.errors(findings)))

    def test_resigning_hash_and_appending_provenance_restores_validity(self):
        self.write_registry(entry())
        path = self.workspace / "specs" / "_assumptions" / "registry.json"
        doc = json.loads(path.read_text())
        doc["entries"][0]["canonical_text"] = OTHER_TEXT
        doc["entries"][0]["content_hash"] = ar.canonical_assumption_hash(OTHER_TEXT)
        doc["entries"][0]["provenance"].append({"action": "changed", "actor": "bob", "at": "2026-09-19", "note": "clarified"})
        path.write_text(json.dumps(doc))
        self.assertEqual(self.check(), [])

    def test_a_composite_declaring_the_old_hash_is_hash_disagreeing_after_an_edit(self):
        self.write_registry(entry(text=OTHER_TEXT))  # registry now carries the new text/hash
        findings = self.check(boundaries=[boundary_with(legacy_ref(assumption_hash=HASH))])
        self.assertTrue(any("hash-disagreeing" in f.reason for f in self.errors(findings)))


class CliValidateIntegrationTest(WorkspaceFixture):
    def _descriptor(self):
        descriptor = json.loads(
            (ROOT / "schemas" / "examples" / "project-descriptor.greenfield.example.json").read_text()
        )
        descriptor["crates"] = [{"crate_dir": "crates/a", "contracts_crate": "contracts", "specs_search_root": "crates"}]
        (self.workspace / "project-descriptor.json").write_text(json.dumps(descriptor))

    def _boundary(self, ref):
        path = self.workspace / "crates" / "a" / "specs" / "_boundaries" / "scheduler_dispatch__to__task_queue_pop_ready.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(json.dumps(VALID_BOUNDARY))
        if "assumption_id" in ref:
            data["assumptions"] = [dict(ref)]
        else:
            data["assumptions"] = [dict(ref, boundary_id=ref.get("boundary_id", data["boundary_id"]))]
        path.write_text(json.dumps(data))

    def test_validate_reports_a_dangling_registry_reference_through_the_cli(self):
        self._descriptor()
        self.write_registry(entry())
        self._boundary({"assumption_id": "ASM-missing"})
        code, out, _ = self.run_cli("validate")
        self.assertEqual(code, 1)
        self.assertIn("dangling", out)

    def test_validate_is_clean_after_migrating_to_a_registry_id(self):
        self._descriptor()
        self.write_registry(entry())
        self._boundary({"assumption_id": "ASM-scheduler-dispatch"})
        code, out, _ = self.run_cli("validate")
        self.assertEqual(code, 0, out)

    def test_validate_deprecates_a_legacy_composite_without_failing(self):
        self._descriptor()
        self.write_registry(entry())
        self._boundary(legacy_ref())
        code, out, _ = self.run_cli("validate")
        self.assertEqual(code, 0, out)
        self.assertIn("deprecation", out)

    def test_check_surfaces_a_dangling_registry_reference(self):
        """The G21 gate is reachable from the loop driver (#56's `check`),
        not only from `validate`."""
        self._descriptor()
        self.write_registry(entry())
        self.write_manifest(manifest_with({"assumption_id": "ASM-missing"}))
        code, out, _ = self.run_cli("check", "--json")
        self.assertEqual(code, 1)
        document = json.loads(out)
        self.assertTrue(any(f["gate_id"] == "G21" for f in document["findings"]))


if __name__ == "__main__":
    unittest.main()
