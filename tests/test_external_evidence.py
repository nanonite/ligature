"""Generic, versioned external-evidence extension point (chainlink #171).

The fixture here is a dudect-style timing observation for one obligation.
It exercises the whole extension point: a project descriptor declares the
external-<name> kind and pinned tool identity, an interaction-style
required_assurance references it, record-assurance ingests the versioned
result and binds it to the current Rust source bytes, and G14 re-reads the
same binding -- refusing a stale source, changed result bytes, or a changed
tool declaration.  It also pins the vocabulary boundary: external evidence
can never satisfy a deductive claim.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gate_g14  # noqa: E402
import pipeline  # noqa: E402
from external_evidence import (  # noqa: E402
    EXTERNAL_RESULT_FORMAT,
    is_external_evidence_kind,
    load_result_validator,
)
from project_descriptor import (  # noqa: E402
    ProjectDescriptorError,
    load_project_descriptor,
)
from record_assurance import (  # noqa: E402
    EvidenceNotEstablished,
    InvalidRecordInput,
    record_assurance,
    rust_source_snapshot,  # noqa: E402
)
from satisfies import satisfies  # noqa: E402
from validate_work_package import check_external_evidence_declarations  # noqa: E402
from verifier_policy import (  # noqa: E402
    assurance_policy_mismatches,
    policy_for_cluster,
)

WORK_PACKAGE = "WP-CRYPTO-MIXED"
OBLIGATION = "ChaCha20.C003"
CLUSTER = "crypto-mixed"
EMIT = f"ci/results/{WORK_PACKAGE}.json"
EXTERNAL_KIND = "external-dudect-constant-time"
TOOL = "dudect"
TOOL_VERSION = "0.2.0"
TOOL_SHA = "sha256:" + "a" * 64
SOURCE_REL = "crates/scheduler/src/lib.rs"
RESULT_REL = "ci/results/external/dudect-chacha20.json"


def descriptor_document(external: dict | None = None) -> dict:
    descriptor = json.loads(
        (ROOT / "schemas" / "examples" / "project-descriptor.greenfield.example.json").read_text()
    )
    descriptor["crates"] = [
        {
            "crate_dir": "crates/scheduler",
            "contracts_crate": "contracts",
            "specs_search_root": "crates",
        }
    ]
    if external is not None:
        descriptor["external_evidence"] = [external]
    return descriptor


def declaration() -> dict:
    return {
        "schema_version": "1.0",
        "kind": EXTERNAL_KIND,
        "tool": TOOL,
        "tool_version": TOOL_VERSION,
        "tool_sha256": TOOL_SHA,
        "command": "dudect --input crates/scheduler/src/lib.rs --output result.json",
        "result_format": EXTERNAL_RESULT_FORMAT,
        "claims": ["timing-independence", "side-channel-resistance"],
        "closure_ceiling": "statistical",
    }


def manifest(
    *,
    harness: str = EXTERNAL_KIND,
    claims=("timing-independence",),
    evidence=(EXTERNAL_KIND,),
) -> dict:
    return {
        "schema": "work-package-manifest/1.0",
        "work_package": WORK_PACKAGE,
        "issue": "chainlink:171",
        "depends_on": [],
        "coupling_notes": [],
        "scheduling_rule": "reverse-dependency-order",
        "functions": ["scheduler::chacha20"],
        "obligations": [OBLIGATION],
        "provenance": {
            "base_commit": "a1b2c3d",
            "promotion_id": "PROM-CRYPTO-001",
            "artifact_set_hash": "sha256:" + "2" * 64,
            "toolchain": "nightly-2026-05-01",
            "target": "x86_64-unknown-linux-gnu",
            "features": ["default"],
        },
        "gate_integrity": [{"runner": "scripts/gate_g14.py", "hash": "sha256:" + "3" * 64}],
        "write_policy": {
            "allowed_write_set": ["crates/scheduler/src/"],
            "protected_write_set": ["crates/*/specs/**"],
            "on_conflict": "raise_change_request",
        },
        "definition_of_done": {
            "provided_guarantees": [
                {
                    "obligation_id": OBLIGATION,
                    "required_assurance": {
                        "required_claims": list(claims),
                        "accepted_evidence_kinds": list(evidence),
                    },
                    "harness": harness,
                }
            ],
            "required_preconditions_to_establish": [],
            "required_guarantees": [],
            "trusted_assumptions": [],
        },
        "gates": [{"id": "timing", "runner": "dudect", "args": []}],
        "failure_policy": {
            "verifier_timeout": "raise_change_request(kind=budget_or_assumption)",
            "obligation_unprovable": "raise_change_request(kind=contract_revision)",
            "missing_callee_guarantee": "raise_change_request(kind=missing_contract)",
            "unintended_call_edge": "raise_change_request(kind=interaction_revision)",
            "unresolved_callsite": "raise_change_request(kind=callsite_unresolved)",
            "forbidden": ["weaken_or_delete_contract"],
        },
        "report": {"emit": EMIT, "per_obligation_assurance_record": True},
    }


def result_document(
    source_hash: str,
    *,
    obligation: str = OBLIGATION,
    evidence_kind: str = EXTERNAL_KIND,
    claim: str = "timing-independence",
    claim_result: str = "pass",
    tool_sha: str = TOOL_SHA,
    tool_version: str = TOOL_VERSION,
    ceiling: str = "statistical",
) -> dict:
    return {
        "format": EXTERNAL_RESULT_FORMAT,
        "obligation_id": obligation,
        "evidence_kind": evidence_kind,
        "claim": {"kind": claim, "result": claim_result},
        "tool": {"name": TOOL, "version": tool_version, "sha256": tool_sha},
        "source": {"sha256": source_hash},
        "closure_ceiling": ceiling,
        "command": "dudect --input crates/scheduler/src/lib.rs",
        "scope": {"statistical_confidence": "0.99"},
        "result": {"t_value": "4.21"},
    }


class ExternalEvidenceWorkspace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / "crates" / "scheduler" / "src").mkdir(parents=True)
        (self.root / "crates" / "scheduler" / "src" / "lib.rs").write_text(
            "pub fn chacha20() {}\n"
        )
        self.descriptor = descriptor_document(declaration())
        (self.root / "project-descriptor.json").write_text(
            json.dumps(self.descriptor, indent=2) + "\n"
        )
        self.manifest_data = manifest()
        (self.root / "ci" / "manifest").mkdir(parents=True)
        (self.root / "ci" / "manifest" / f"{WORK_PACKAGE}.json").write_text(
            json.dumps(self.manifest_data, indent=2) + "\n"
        )

    def source_hash(self) -> str:
        return rust_source_snapshot(self.root, self.manifest_data)[0]

    def write_result(self, document: dict | None = None, *, path: str = RESULT_REL) -> Path:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(document or result_document(self.source_hash()), indent=2) + "\n"
        )
        return target

    def record(self):
        return record_assurance(
            self.root,
            WORK_PACKAGE,
            [],
            descriptor_path=self.root / "project-descriptor.json",
            external_result_specs=[f"{OBLIGATION}={RESULT_REL}"],
        )

    def report(self) -> dict:
        return json.loads((self.root / EMIT).read_text())

    def gate_findings(self, descriptor: dict | None = None):
        reports = {WORK_PACKAGE: self.report()}
        manifests = {WORK_PACKAGE: self.manifest_data}
        closure = gate_g14.Closure(work_packages=[WORK_PACKAGE])
        active = descriptor if descriptor is not None else self.descriptor
        certificate_findings = gate_g14.check_record_certificates(
            self.root,
            closure,
            reports,
            manifests,
            revalidate_kani=False,
            descriptor=active,
        )
        derivation_findings = gate_g14.check_record_derivation(
            closure, manifests, reports, active
        )
        return certificate_findings + derivation_findings


class ExternalEvidenceIntakeTest(ExternalEvidenceWorkspace):
    def test_result_is_recorded_with_its_tool_source_and_ceiling_binding(self):
        self.write_result()
        outcome = self.record()
        self.assertEqual(outcome.report_relative, EMIT)
        record = self.report()["obligation_records"][0]["record"]
        self.assertEqual(record["claim"]["kind"], "timing-independence")
        self.assertEqual(record["claim"]["result"], "pass")
        self.assertEqual(record["evidence"]["kind"], EXTERNAL_KIND)
        self.assertEqual(record["evidence"]["verifier"], TOOL)
        self.assertEqual(record["evidence"]["harness"], EXTERNAL_KIND)
        scope = record["evidence"]["scope"]
        self.assertEqual(scope["closure_ceiling"], "statistical")
        self.assertEqual(scope["external_tool"], TOOL)
        self.assertEqual(scope["external_tool_version"], TOOL_VERSION)
        self.assertEqual(scope["external_tool_sha256"], TOOL_SHA)
        self.assertEqual(scope["external_source_sha256"], self.source_hash())
        self.assertEqual(scope["external_claim"], "timing-independence")
        self.assertTrue(scope["proof_targets"].startswith("external-result:"))
        self.assertEqual(scope["statistical_confidence"], "0.99")
        self.assertRegex(scope["external_result_sha256"], r"^sha256:[a-f0-9]{64}$")

    def test_g14_re_reads_a_fresh_result_without_findings(self):
        self.write_result()
        self.record()
        self.assertEqual(self.gate_findings(), [])

    def test_result_that_fails_its_claim_is_not_recorded(self):
        self.write_result(result_document(self.source_hash(), claim_result="fail"))
        with self.assertRaises(EvidenceNotEstablished):
            self.record()
        self.assertFalse((self.root / EMIT).exists())


class ExternalEvidenceStalenessTest(ExternalEvidenceWorkspace):
    def test_changed_source_after_recording_is_refused_by_g14(self):
        self.write_result()
        self.record()
        source = self.root / SOURCE_REL
        source.write_text("pub fn chacha20() { /* changed */ }\n")
        findings = self.gate_findings()
        self.assertTrue(
            any("source files" in finding.reason or "re-establish" in finding.reason for finding in findings),
            [finding.reason for finding in findings],
        )

    def test_result_older_than_the_source_it_observes_is_refused_at_intake(self):
        result = self.write_result()
        source = self.root / SOURCE_REL
        os.utime(result, (1_000_000, 1_000_000))
        os.utime(source, (2_000_000, 2_000_000))
        with self.assertRaises(EvidenceNotEstablished):
            self.record()
        self.assertFalse((self.root / EMIT).exists())

    def test_changed_result_bytes_after_recording_are_refused_by_g14(self):
        result = self.write_result()
        self.record()
        result.write_text(result.read_text() + "\n")
        findings = self.gate_findings()
        self.assertTrue(
            any("changed after external evidence was recorded" in finding.reason for finding in findings),
            [finding.reason for finding in findings],
        )


class ExternalEvidenceToolBindingTest(ExternalEvidenceWorkspace):
    def test_changed_declared_tool_hash_is_refused_by_g14(self):
        self.write_result()
        self.record()
        changed = descriptor_document(declaration())
        changed["external_evidence"][0]["tool_sha256"] = "sha256:" + "b" * 64
        findings = self.gate_findings(changed)
        self.assertTrue(findings)
        self.assertTrue(
            any(
                "tool identity" in finding.reason
                or "tool_sha256" in finding.reason
                or "not the recorded" in finding.reason
                for finding in findings
            ),
            [finding.reason for finding in findings],
        )

    def test_undeclared_external_kind_is_refused_by_g14(self):
        self.write_result()
        self.record()
        changed = descriptor_document(None)
        findings = self.gate_findings(changed)
        self.assertTrue(findings)
        self.assertTrue(
            any("declares no external_evidence entry" in finding.reason for finding in findings),
            [finding.reason for finding in findings],
        )


class ExternalEvidenceVocabularyTest(ExternalEvidenceWorkspace):
    def test_external_claim_cannot_satisfy_a_deductive_requirement(self):
        self.write_result()
        self.record()
        record = self.report()["obligation_records"][0]["record"]
        deductive = {
            "required_claims": ["postcondition-holds"],
            "accepted_evidence_kinds": [EXTERNAL_KIND],
            "trust_policy": {"assumptions_allowed": []},
        }
        result = satisfies(deductive, record)
        self.assertFalse(result)
        self.assertTrue(any("claim.kind" in reason for reason in result.reasons))

    def test_intake_refuses_a_manifest_that_asks_for_a_deductive_claim(self):
        self.manifest_data = manifest(
            claims=("postcondition-holds",),
            evidence=(EXTERNAL_KIND, "creusot-deductive-check"),
        )
        (self.root / "ci" / "manifest" / f"{WORK_PACKAGE}.json").write_text(
            json.dumps(self.manifest_data, indent=2) + "\n"
        )
        self.write_result()
        with self.assertRaises(InvalidRecordInput):
            self.record()

    def test_external_kind_must_be_declared_and_non_functional_claims_need_no_owner_kind(self):
        assurance = {
            "required_claims": ["timing-independence"],
            "accepted_evidence_kinds": [EXTERNAL_KIND],
        }
        policy = policy_for_cluster(self.descriptor, CLUSTER)
        self.assertEqual(assurance_policy_mismatches(assurance, policy), ([], False))

        undeclared = descriptor_document(None)
        unexpected, _owner_missing = assurance_policy_mismatches(
            assurance, policy_for_cluster(undeclared, CLUSTER)
        )
        self.assertEqual(unexpected, [EXTERNAL_KIND])

        deductive = {
            "required_claims": ["postcondition-holds"],
            "accepted_evidence_kinds": [EXTERNAL_KIND],
        }
        _unexpected, owner_missing = assurance_policy_mismatches(deductive, policy)
        self.assertTrue(owner_missing)

    def test_closure_kind_deductive_or_bounded_is_refused_with_external_evidence(self):
        computed = {"_evidence_kinds": [EXTERNAL_KIND]}
        for declared in ("deductive", "bounded"):
            findings = gate_g14.check_closure_kind(
                {"cluster": CLUSTER, "closure_kind": declared}, computed
            )
            self.assertTrue(findings, declared)
            self.assertIn("partial", findings[0].reason)

    def test_partial_closure_is_not_refuted_by_external_evidence(self):
        computed = {"_evidence_kinds": [EXTERNAL_KIND]}
        self.assertEqual(
            gate_g14.check_closure_kind(
                {"cluster": CLUSTER, "closure_kind": "partial"}, computed
            ),
            [],
        )


class ExternalEvidenceCliTest(ExternalEvidenceWorkspace):
    def _run(self, *args: str) -> tuple[int, str, str]:
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = pipeline.main(["--workspace", str(self.root), *args])
        return code, out.getvalue(), err.getvalue()

    def test_cli_records_and_reports_an_external_observation(self):
        self.write_result()
        code, out, err = self._run(
            "record-assurance",
            WORK_PACKAGE,
            "--external-result",
            f"{OBLIGATION}={RESULT_REL}",
        )
        self.assertEqual(code, 0, err)
        self.assertIn("recorded assurance: " + EMIT, out)
        self.assertIn(f"{OBLIGATION}: {EXTERNAL_KIND} supported -- external observation", out)
        self.assertNotIn("Traceback", out + err)

    def test_cli_stale_result_exits_1_without_writing(self):
        result = self.write_result()
        os.utime(result, (1_000_000, 1_000_000))
        os.utime(self.root / SOURCE_REL, (2_000_000, 2_000_000))
        code, out, err = self._run(
            "record-assurance",
            WORK_PACKAGE,
            "--external-result",
            f"{OBLIGATION}={RESULT_REL}",
        )
        self.assertEqual(code, 1)
        self.assertIn("error: ", err)
        self.assertNotIn("Traceback", out + err)
        self.assertFalse((self.root / EMIT).exists())


class ExternalEvidenceDeclarationTest(unittest.TestCase):
    def test_duplicate_declaration_kind_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            descriptor = descriptor_document(declaration())
            descriptor["external_evidence"].append(declaration())
            path = root / "project-descriptor.json"
            path.write_text(json.dumps(descriptor) + "\n")
            with self.assertRaises(ProjectDescriptorError) as raised:
                load_project_descriptor(path)
            self.assertIn("declares evidence kind", str(raised.exception))

    def test_builtin_kind_cannot_be_redeclared_as_external(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            external = declaration()
            external["kind"] = "creusot-deductive-check"
            descriptor = descriptor_document(external)
            path = root / "project-descriptor.json"
            path.write_text(json.dumps(descriptor) + "\n")
            with self.assertRaises(ProjectDescriptorError) as raised:
                load_project_descriptor(path)
            self.assertIn("kind", str(raised.exception))

    def test_work_package_validation_resolves_external_kinds_against_the_descriptor(self):
        path = Path("ci/manifest/WP-CRYPTO-MIXED.json")
        declared = descriptor_document(declaration())
        self.assertEqual(
            check_external_evidence_declarations(path, manifest(), declared), []
        )
        undeclared = descriptor_document(None)
        findings = check_external_evidence_declarations(path, manifest(), undeclared)
        self.assertTrue(findings)
        self.assertTrue(
            any("declares no external_evidence entry" in finding.reason for finding in findings)
        )
        mismatch = manifest(evidence=("creusot-deductive-check",))
        findings = check_external_evidence_declarations(path, mismatch, declared)
        self.assertTrue(
            any("does not accept that exact kind" in finding.reason for finding in findings)
        )

    def test_result_format_is_versioned_and_closed(self):
        validator = load_result_validator()
        good = result_document("sha256:" + "c" * 64)
        self.assertEqual(list(validator.iter_errors(good)), [])
        good["format"] = "ligature-external-evidence-result/2.0"
        self.assertFalse(list(validator.iter_errors(good)))
        good["format"] = "dudect/raw"
        self.assertTrue(list(validator.iter_errors(good)))
        self.assertTrue(is_external_evidence_kind(EXTERNAL_KIND))
        self.assertFalse(is_external_evidence_kind("dudect"))
