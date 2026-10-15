"""The assurance-recording path gate-g14's achieved side never had
(chainlink #87 / date-ligature-045).

The issue's shape, pinned here as the baseline every other case is
measured against: a workspace whose Creusot proofs are complete still
reports every obligation as "recorded no achieved assurance" (and the
report itself as missing), because no command could produce the report
`gate-g14` reads at the manifest's own `report.emit` path. Everything
else in this file follows from one rule -- every field of that report is
DERIVED from verifier evidence, and the caller only declares which
certificate is about which obligation, a declaration that is checked.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
RUSTC_EXECUTABLE = shutil.which("rustc")
sys.path.insert(0, str(ROOT / "scripts"))

import gate_g14  # noqa: E402
import pipeline  # noqa: E402
import record_assurance as record_assurance_module  # noqa: E402
from record_assurance import EvidenceNotEstablished  # noqa: E402
from record_assurance import InvalidRecordInput  # noqa: E402
from record_assurance import concept_segment  # noqa: E402
from record_assurance import record_assurance  # noqa: E402
from satisfies import satisfies  # noqa: E402

WORK_PACKAGE = "WP-TEST-1"
EMIT = f"ci/results/{WORK_PACKAGE}.json"
CLUSTER = "test-cluster"

PROFILE = {
    "cluster": CLUSTER,
    "closure_kind": "deductive",
    "conditions": {"owning_verifier": "creusot"},
}


def guarantee(
    obligation_id: str,
    *,
    harness: str = "creusot",
    claims=("postcondition-holds",),
    evidence=("creusot-deductive-check",),
    minimum_scope: dict | None = None,
) -> dict:
    required = {
        "required_claims": list(claims),
        "accepted_evidence_kinds": list(evidence),
    }
    if minimum_scope is not None:
        required["minimum_scope"] = minimum_scope
    return {
        "obligation_id": obligation_id,
        "required_assurance": required,
        "harness": harness,
    }


def manifest(provided: list[dict], *, emit: str = EMIT, bridges: list[str] = ()) -> dict:
    """A schema-valid work-package manifest (the shape
    tests/test_gate_g14.py builds, with `harness` settable so this file can
    exercise the reader's own harness restrictions)."""
    return {
        "schema": "work-package-manifest/1.0",
        "work_package": WORK_PACKAGE,
        "issue": "chainlink:900",
        "depends_on": [],
        "coupling_notes": [],
        "scheduling_rule": "reverse-dependency-order",
        "functions": ["test::x"],
        "obligations": [entry["obligation_id"] for entry in provided],
        "provenance": {
            "base_commit": "a1b2c3d",
            "promotion_id": "PROM-TEST-001",
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
            "provided_guarantees": provided,
            "required_preconditions_to_establish": [
                {
                    "bridge_id": bridge_id,
                    "required_assurance": {
                        "required_claims": ["callee-precondition-established"],
                        "accepted_evidence_kinds": ["creusot-deductive-check"],
                    },
                    "callsite_requirement": "all-discovered-resolved",
                }
                for bridge_id in bridges
            ],
            "required_guarantees": [],
            "trusted_assumptions": [],
        },
        "gates": [{"id": "prove", "runner": "cargo", "args": ["creusot"]}],
        "failure_policy": {
            "verifier_timeout": "raise_change_request(kind=budget_or_assumption)",
            "obligation_unprovable": "raise_change_request(kind=contract_revision)",
            "missing_callee_guarantee": "raise_change_request(kind=missing_contract)",
            "unintended_call_edge": "raise_change_request(kind=interaction_revision)",
            "unresolved_callsite": "raise_change_request(kind=callsite_unresolved)",
            "forbidden": ["weaken_or_delete_contract"],
        },
        "report": {"emit": emit, "per_obligation_assurance_record": True},
    }


def write_certificate(
    root: Path,
    rel_dir: str,
    *,
    proved: int = 1,
    stuck: int = 0,
    coma: bool = False,
    coma_newer: bool = False,
) -> Path:
    """A why3find proof certificate at `<rel_dir>/proof.json`, in the
    shape `cargo creusot` leaves behind: proved goals carry a prover
    result, an incomplete one is a `null` leaf under its tactic tree."""
    directory = root / rel_dir
    directory.mkdir(parents=True, exist_ok=True)
    goals: dict = {f"vc_proved_{i}": {"prover": "z3", "time": 0.01} for i in range(proved)}
    if stuck:
        goals["vc_partial"] = {
            "tactic": "split_vc",
            "children": [{"prover": "alt-ergo", "time": 0.02}] + [None] * stuck,
        }
    certificate = directory / "proof.json"
    certificate.write_text(
        json.dumps({"profile": [], "proofs": {"Coma": goals}}, indent=2) + "\n"
    )
    if coma:
        coma_path = directory.parent / (directory.name + ".coma")
        coma_path.write_text("(* a coma program *)\n")
        if coma_newer:
            # certificate older than the program it certifies
            os.utime(certificate, (1_000_000, 1_000_000))
            os.utime(coma_path, (2_000_000, 2_000_000))
        else:
            os.utime(coma_path, (1_000_000, 1_000_000))
            os.utime(certificate, (2_000_000, 2_000_000))
    return certificate


class RecordAssuranceTestCase(unittest.TestCase):
    """Shared workspace: one manifest providing two Weekday obligations,
    two proved certificates under a `weekday/` module directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.provided = [
            guarantee("Weekday.C1"),
            guarantee("Weekday.C2"),
        ]
        self.write_manifest()
        self.cert_c1 = write_certificate(self.root, "verif/weekday/weekday_from_days")
        self.cert_c2 = write_certificate(self.root, "verif/weekday/impl_Weekday/c_encoding")
        self.specs = [
            "Weekday.C1=verif/weekday/weekday_from_days",
            "Weekday.C2=verif/weekday/impl_Weekday/c_encoding",
        ]

    def write_manifest(self, data: dict | None = None, *, name: str = WORK_PACKAGE) -> Path:
        directory = self.root / "ci" / "manifest"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.json"
        path.write_text(json.dumps(data if data is not None else manifest(self.provided), indent=2) + "\n")
        return path

    def report(self) -> dict:
        return json.loads((self.root / EMIT).read_text())

    def report_exists(self) -> bool:
        return (self.root / EMIT).exists()

    def gate_view(self):
        """(reports, findings) exactly as gate-g14 computes them over this
        workspace -- the functions that produced the issue's five
        'recorded no achieved assurance' lines."""
        manifests, manifest_findings = gate_g14.load_manifests(self.root)
        self.assertEqual(manifest_findings, [])
        closure = gate_g14.Closure(work_packages=[WORK_PACKAGE])
        reports, findings = gate_g14.load_assurance_reports(self.root, closure, manifests)
        return manifests, closure, reports, findings


class VerusRecordAssuranceTest(RecordAssuranceTestCase):
    """#129: ingest Verus's official `--output-json` per-function map and
    re-open the same result from G14."""

    FUNCTION = "scheduler::check_dispatch"
    OBLIGATION = "Scheduler.C001"
    VERUS_VERSION = "0.2026.10.01"

    def setUp(self):
        super().setUp()
        self.provided = [
            guarantee(
                self.OBLIGATION,
                harness="verus",
                evidence=("verus-deductive-check",),
            )
        ]
        self.write_manifest()
        self.verus_path = self.root / "ci" / "results" / "verus-output.json"
        self.write_verus_json()

    def write_verus_json(
        self,
        *,
        success=True,
        proved_notes=None,
        failed_notes=(),
        function=FUNCTION,
    ):
        self.verus_path.parent.mkdir(parents=True, exist_ok=True)
        self.verus_path.write_text(
            json.dumps(
                {
                    "func-details": {
                        function: {
                            "obligation_proof_notes": (
                                [f"ligature:{self.OBLIGATION}"] if proved_notes is None else proved_notes
                            ),
                            "failed_proof_notes": list(failed_notes),
                        }
                    },
                    "verification-results": {
                        "success": success,
                        "verified": 149,
                        "errors": 0 if success else 1,
                        "is-verifying-entire-crate": True,
                    },
                    "verus-build": {"version": self.VERUS_VERSION},
                },
                indent=2,
            )
            + "\n"
        )

    def record(self):
        return record_assurance(
            self.root,
            WORK_PACKAGE,
            [],
            verus_json="ci/results/verus-output.json",
            verus_function_specs=[f"{self.OBLIGATION}={self.FUNCTION}"],
        )

    def test_per_function_json_records_verus_and_g14_reopens_it(self):
        outcome = self.record()
        self.assertEqual([entry.obligation_id for entry in outcome.recorded], [self.OBLIGATION])
        record = self.report()["obligation_records"][0]["record"]
        self.assertEqual(record["evidence"]["kind"], "verus-deductive-check")
        self.assertEqual(record["evidence"]["verifier"], "verus")
        self.assertEqual(
            record["evidence"]["scope"]["proof_targets"],
            f"verus-json:ci/results/verus-output.json#{self.FUNCTION}",
        )
        self.assertEqual(record["evidence"]["scope"]["verus_version"], self.VERUS_VERSION)

        manifests, closure, reports, findings = self.gate_view()
        self.assertEqual(findings, [])
        self.assertEqual(gate_g14.check_record_certificates(self.root, closure, reports), [])
        self.assertEqual(
            gate_g14.check_record_derivation(closure, manifests, reports), []
        )

    def test_aggregate_success_without_the_exact_function_note_is_not_evidence(self):
        self.write_verus_json(proved_notes=[])
        with self.assertRaises(EvidenceNotEstablished) as caught:
            self.record()
        self.assertIn("does not bind", str(caught.exception))
        self.assertFalse(self.report_exists())

    def test_a_failed_verus_run_is_not_evidence_even_with_a_note(self):
        self.write_verus_json(success=False)
        with self.assertRaises(EvidenceNotEstablished) as caught:
            self.record()
        self.assertIn("does not report whole-crate Verus success", str(caught.exception))
        self.assertFalse(self.report_exists())

    def test_g14_rejects_verus_json_changed_after_recording(self):
        self.record()
        self.write_verus_json(proved_notes=[f"ligature:{self.OBLIGATION}", "edited"])
        _manifests, closure, reports, findings = self.gate_view()
        self.assertEqual(findings, [])
        recheck = gate_g14.check_record_certificates(self.root, closure, reports)
        self.assertEqual(len(recheck), 1)
        self.assertIn("changed after the Verus evidence was recorded", recheck[0].reason)


class KaniRecordAssuranceTest(RecordAssuranceTestCase):
    """#132/#133: resolve and rerun one fresh Kani 0.67 exact harness."""

    HARNESS = "kani_fixture_property"
    HARNESS_IDENTITY = "kani_fixture_mod::kani_fixture_property"
    FEATURE = "kani_fixture_proof"
    OBLIGATION = "Scheduler.C001"
    TOOLCHAIN = "1.95.0-x86_64-unknown-linux-gnu"
    TARGET = "x86_64-unknown-linux-gnu"
    INPUT_DOMAIN = f"the bounded model encoded by Kani harness {HARNESS}"

    def setUp(self):
        super().setUp()
        self.provided = [
            guarantee(
                self.OBLIGATION,
                harness=self.HARNESS,
                claims=("callee-precondition-established",),
                evidence=("kani-bounded-model-check",),
                minimum_scope={"input_domain": self.INPUT_DOMAIN},
            )
        ]
        self.kani_root = self.root / "crates" / "scheduler"
        source = self.kani_root / "src" / "lib.rs"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            f'#[cfg(feature = "{self.FEATURE}")]\n'
            "pub mod kani_fixture_mod {\n"
            "    #[kani::proof]\n"
            f"    pub fn {self.HARNESS}() {{\n"
            "        let value: u8 = kani::any();\n"
            "        assert_eq!(value.wrapping_add(0), value);\n"
            "    }\n"
            "}\n"
        )
        (self.kani_root / "Cargo.toml").write_text(
            '[package]\nname = "scheduler"\nversion = "0.1.0"\nedition = "2021"\n'
            f'[features]\n{self.FEATURE} = []\n'
        )
        (self.kani_root / "Cargo.lock").write_text(
            '# This file is automatically @generated by Cargo.\nversion = 4\n\n'
            '[[package]]\nname = "scheduler"\nversion = "0.1.0"\n'
        )
        data = manifest(self.provided)
        data["provenance"]["toolchain"] = self.TOOLCHAIN
        data["provenance"]["target"] = self.TARGET
        data["provenance"]["features"] = [self.FEATURE]
        self.write_manifest(data)

    def kani_output(
        self, *, status="SUCCESS", succeeded=1, failed=0, exit_code=0, extra_checks=()
    ):
        verdict = "SUCCESSFUL" if failed == 0 else "FAILED"
        checks = [
            (
                f"{self.HARNESS_IDENTITY}.assertion.1",
                status,
                "assertion failed: tag.len() == 16",
                "src/kani_proofs.rs:140:5 in function " + self.HARNESS_IDENTITY,
            ),
            *extra_checks,
        ]
        check_blocks = []
        for number, (name, check_status, description, *location) in enumerate(checks, 1):
            block = (
                f"Check {number}: {name}\n"
                f"\t - Status: {check_status}\n"
                f"\t - Description: {json.dumps(description)}\n"
            )
            if location:
                block += f"\t - Location: {location[0]}\n"
            check_blocks.append(block)
        unreachable = sum(check[1] == "UNREACHABLE" for check in checks)
        unreachable_note = f" ({unreachable} unreachable)" if unreachable else ""
        return SimpleNamespace(
            returncode=exit_code,
            stdout=(
                f"Checking harness {self.HARNESS_IDENTITY}...\n"
                "Manual Harness Summary: Complete - "
                f"{succeeded} successfully verified harnesses, {failed} failures, 1 total.\n"
            ),
            stderr="",
            result_text=(
                "RESULTS:\n" + "\n".join(check_blocks) +
                "SUMMARY:\n"
                f" ** {failed} of {len(checks)} failed{unreachable_note}\n"
                f"VERIFICATION:- {verdict}\n"
                "Verification Time: 0.01s\n"
            ),
        )

    def fake_kani(
        self, *, proof_result=None, metadata_harnesses=None, metadata_text=None, proof_timeout=False
    ):
        proof_result = proof_result or self.kani_output()
        metadata_harnesses = (
            [self.HARNESS_IDENTITY]
            if metadata_harnesses is None
            else metadata_harnesses
        )

        def run(command, **kwargs):
            if command[-1] == "--version":
                return SimpleNamespace(returncode=0, stdout="cargo-kani 0.67.0\n", stderr="")
            if command[-1] == "-vV":
                return SimpleNamespace(
                    returncode=0,
                    stdout=f"rustc 1.95.0\nhost: {self.TARGET}\n",
                    stderr="",
                )
            if "metadata" in command:
                metadata = {
                    "packages": [
                        {
                            "manifest_path": str(Path(kwargs["cwd"]) / "Cargo.toml"),
                            "targets": [{"name": "scheduler", "kind": ["lib"]}],
                        }
                    ]
                }
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(metadata),
                    stderr="",
                )
            if "--only-codegen" in command:
                target_dir = Path(command[command.index("--target-dir") + 1])
                metadata_dir = target_dir / "kani" / self.TARGET / "debug" / "deps"
                metadata_dir.mkdir(parents=True)
                if metadata_text is None:
                    codegen_metadata = {
                        "crate_name": "scheduler",
                        "proof_harnesses": [
                            {"pretty_name": identity} for identity in metadata_harnesses
                        ],
                    }
                    metadata_text_value = json.dumps(codegen_metadata)
                else:
                    metadata_text_value = metadata_text
                (metadata_dir / "scheduler.kani-metadata.json").write_text(
                    metadata_text_value, encoding="utf-8"
                )
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if proof_timeout:
                raise subprocess.TimeoutExpired(command, 1)
            target_dir = Path(command[command.index("--target-dir") + 1])
            output_dir = target_dir / "result_output_dir"
            output_dir.mkdir(parents=True)
            (output_dir / self.HARNESS_IDENTITY).write_text(
                getattr(proof_result, "result_text", proof_result.stdout), encoding="utf-8"
            )
            return proof_result

        return run

    def record(self):
        return record_assurance_module.record_assurance(
            self.root,
            WORK_PACKAGE,
            [],
            kani_harness_specs=[f"{self.OBLIGATION}={self.HARNESS}"],
            kani_crate="crates/scheduler",
        )

    def run_cli(self):
        output, errors = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = pipeline.main(
                [
                    "--workspace", str(self.root),
                    "record-assurance", WORK_PACKAGE,
                    "--kani-crate", "crates/scheduler",
                    "--kani-harness", f"{self.OBLIGATION}={self.HARNESS}",
                ]
            )
        return code, output.getvalue(), errors.getvalue()

    def test_exact_harness_records_manifest_claim_and_g14_rechecks_run(self):
        with patch("record_assurance.shutil.which", side_effect=lambda exe: f"/usr/bin/{exe}"), patch(
            "record_assurance.subprocess.run", side_effect=self.fake_kani()
        ) as run:
            outcome = self.record()
            self.assertEqual(outcome.recorded[0].evidence_kind, "kani-bounded-model-check")
            report = self.report()
            error = list(gate_g14.load_assurance_report_validator().iter_errors(report))
            self.assertEqual(error, [])
            record = report["obligation_records"][0]["record"]
            self.assertEqual(record["claim"]["kind"], "callee-precondition-established")
            self.assertEqual(record["evidence"]["verifier"], "kani")
            self.assertEqual(record["evidence"]["harness"], self.HARNESS)
            self.assertEqual(record["evidence"]["kind"], "kani-bounded-model-check")
            self.assertEqual(record["evidence"]["scope"]["input_domain"], self.INPUT_DOMAIN)
            self.assertEqual(
                json.loads(record["evidence"]["scope"]["kani_result_summary"]),
                {
                    "total_checks": 1,
                    "successful_checks": 1,
                    "successful_claim_assertions": [
                        f"{self.HARNESS_IDENTITY}.assertion.1"
                    ],
                    "unreachable_checks": [],
                },
            )
            manifests, closure, reports, findings = self.gate_view()
            self.assertEqual(findings, [])
            self.assertEqual(gate_g14.check_record_certificates(self.root, closure, reports, manifests), [])
            self.assertEqual(gate_g14.check_record_derivation(closure, manifests, reports), [])
            self.assertEqual(
                gate_g14.check_provided_guarantees(CLUSTER, PROFILE, closure, manifests, reports),
                [],
            )
            self.assertEqual(run.call_count, 10)  # version/host/metadata/list/proof, then G14 repeats
            proof_commands = [
                call.args[0]
                for call in run.call_args_list
                if "--output-format" in call.args[0]
            ]
            codegen_commands = [
                call.args[0]
                for call in run.call_args_list
                if "--only-codegen" in call.args[0]
            ]
            self.assertEqual(len(proof_commands), 2)
            self.assertEqual(len(codegen_commands), 2)
            for command in proof_commands:
                self.assertEqual(command[command.index("--harness") + 1], self.HARNESS_IDENTITY)
                self.assertIn("--exact", command)
            for command in [*codegen_commands, *proof_commands]:
                self.assertIn("--no-default-features", command)
                self.assertEqual(command[command.index("--features") + 1], self.FEATURE)

    def test_kani_failure_is_not_recorded(self):
        failed = self.kani_output(
            status="FAILURE", succeeded=0, failed=1, exit_code=1
        )
        with patch("record_assurance.shutil.which", side_effect=lambda exe: f"/usr/bin/{exe}"), patch(
            "record_assurance.subprocess.run", side_effect=self.fake_kani(proof_result=failed)
        ):
            with self.assertRaises(EvidenceNotEstablished):
                self.record()
        self.assertFalse(self.report_exists())

    def test_successful_claim_with_unreachable_safety_checks_records_and_g14_rechecks(self):
        run = self.kani_output(
            extra_checks=(
                (
                    "poly1305::Poly1305State::update.assertion.1",
                    "UNREACHABLE",
                    "attempt to subtract with overflow",
                    "src/poly1305.rs:110:24 in function poly1305::Poly1305State::update",
                ),
                (
                    "core::num::<impl usize>::abs_diff.assertion.1",
                    "UNREACHABLE",
                    "attempt to subtract with overflow",
                    "library/core/src/num/uint_macros.rs:2852:21",
                ),
            )
        )
        with patch("record_assurance.shutil.which", side_effect=lambda exe: f"/usr/bin/{exe}"), patch(
            "record_assurance.subprocess.run", side_effect=self.fake_kani(proof_result=run)
        ) as invoked:
            outcome = self.record()
            self.assertEqual(outcome.recorded[0].goals, 1)
            summary = json.loads(
                self.report()["obligation_records"][0]["record"]["evidence"]["scope"]
                ["kani_result_summary"]
            )
            self.assertEqual(summary["successful_checks"], 1)
            self.assertEqual(len(summary["unreachable_checks"]), 2)
            self.assertEqual(
                summary["unreachable_checks"][0]["description"],
                "attempt to subtract with overflow",
            )
            manifests, closure, reports, findings = self.gate_view()
            self.assertEqual(findings, [])
            self.assertEqual(
                gate_g14.check_record_certificates(self.root, closure, reports, manifests), []
            )
            self.assertEqual(
                invoked.call_count,
                10,
                "record-assurance and G14 both rerun and parse the same successful result shape",
            )

    def test_unreachable_explicit_harness_assertion_is_vacuous_and_not_recorded(self):
        unreachable_claim = self.kani_output(status="UNREACHABLE")
        with patch("record_assurance.shutil.which", side_effect=lambda exe: f"/usr/bin/{exe}"), patch(
            "record_assurance.subprocess.run",
            side_effect=self.fake_kani(proof_result=unreachable_claim),
        ):
            with self.assertRaises(EvidenceNotEstablished) as caught:
                self.record()
        self.assertIn("UNREACHABLE and therefore vacuous", str(caught.exception))
        self.assertFalse(self.report_exists())

    def test_g14_rejects_a_report_summary_that_disagrees_with_the_fresh_run(self):
        run = self.kani_output(
            extra_checks=(
                (
                    "poly1305::Poly1305State::update.assertion.1",
                    "UNREACHABLE",
                    "attempt to subtract with overflow",
                ),
            )
        )
        with patch("record_assurance.shutil.which", side_effect=lambda exe: f"/usr/bin/{exe}"), patch(
            "record_assurance.subprocess.run", side_effect=self.fake_kani(proof_result=run)
        ):
            self.record()
        report = self.report()
        report["obligation_records"][0]["record"]["evidence"]["scope"][
            "kani_result_summary"
        ] = "{}"
        (self.root / EMIT).write_text(json.dumps(report))
        manifests, closure, reports, findings = self.gate_view()
        self.assertEqual(findings, [])
        with patch("record_assurance.shutil.which", side_effect=lambda exe: f"/usr/bin/{exe}"), patch(
            "record_assurance.subprocess.run", side_effect=self.fake_kani(proof_result=run)
        ):
            recheck = gate_g14.check_record_certificates(self.root, closure, reports, manifests)
        self.assertEqual(len(recheck), 1)
        self.assertIn("Kani check summary changed", recheck[0].reason)

    def test_g14_keeps_legacy_all_success_records_usable(self):
        with patch("record_assurance.shutil.which", side_effect=lambda exe: f"/usr/bin/{exe}"), patch(
            "record_assurance.subprocess.run", side_effect=self.fake_kani()
        ):
            self.record()
        report = self.report()
        scope = report["obligation_records"][0]["record"]["evidence"]["scope"]
        scope.pop("kani_result_summary")
        (self.root / EMIT).write_text(json.dumps(report))
        manifests, closure, reports, findings = self.gate_view()
        self.assertEqual(findings, [])
        with patch("record_assurance.shutil.which", side_effect=lambda exe: f"/usr/bin/{exe}"), patch(
            "record_assurance.subprocess.run", side_effect=self.fake_kani()
        ):
            self.assertEqual(
                gate_g14.check_record_certificates(self.root, closure, reports, manifests), []
            )

    def test_g14_requires_a_summary_when_a_legacy_rerun_has_unreachable_checks(self):
        run = self.kani_output(
            extra_checks=(
                (
                    "poly1305::Poly1305State::update.assertion.1",
                    "UNREACHABLE",
                    "attempt to subtract with overflow",
                ),
            )
        )
        with patch("record_assurance.shutil.which", side_effect=lambda exe: f"/usr/bin/{exe}"), patch(
            "record_assurance.subprocess.run", side_effect=self.fake_kani(proof_result=run)
        ):
            self.record()
        report = self.report()
        scope = report["obligation_records"][0]["record"]["evidence"]["scope"]
        scope.pop("kani_result_summary")
        (self.root / EMIT).write_text(json.dumps(report))
        manifests, closure, reports, findings = self.gate_view()
        self.assertEqual(findings, [])
        with patch("record_assurance.shutil.which", side_effect=lambda exe: f"/usr/bin/{exe}"), patch(
            "record_assurance.subprocess.run", side_effect=self.fake_kani(proof_result=run)
        ):
            recheck = gate_g14.check_record_certificates(self.root, closure, reports, manifests)
        self.assertEqual(len(recheck), 1)
        self.assertIn("no `kani_result_summary`", recheck[0].reason)

    def test_unknown_kani_status_is_rejected_as_unrecognized_output(self):
        unknown = self.kani_output(status="MAYBE")
        with patch("record_assurance.shutil.which", side_effect=lambda exe: f"/usr/bin/{exe}"), patch(
            "record_assurance.subprocess.run", side_effect=self.fake_kani(proof_result=unknown)
        ):
            with self.assertRaises(InvalidRecordInput) as caught:
                self.record()
        self.assertIn("unsupported status 'MAYBE'", str(caught.exception))
        self.assertFalse(self.report_exists())

    def test_undetermined_kani_status_is_not_recorded(self):
        undetermined = self.kani_output(status="UNDETERMINED")
        with patch("record_assurance.shutil.which", side_effect=lambda exe: f"/usr/bin/{exe}"), patch(
            "record_assurance.subprocess.run",
            side_effect=self.fake_kani(proof_result=undetermined),
        ):
            with self.assertRaises(EvidenceNotEstablished) as caught:
                self.record()
        self.assertIn("UNDETERMINED", str(caught.exception))
        self.assertFalse(self.report_exists())

    def test_cover_properties_are_not_accepted_as_assertion_evidence(self):
        cover = self.kani_output(
            extra_checks=(
                (
                    f"{self.HARNESS_IDENTITY}.cover.1",
                    "UNREACHABLE",
                    "cover condition: message_len > 0",
                ),
            )
        )
        with patch("record_assurance.shutil.which", side_effect=lambda exe: f"/usr/bin/{exe}"), patch(
            "record_assurance.subprocess.run", side_effect=self.fake_kani(proof_result=cover)
        ):
            with self.assertRaises(EvidenceNotEstablished) as caught:
                self.record()
        self.assertIn("cover results are unsupported", str(caught.exception))
        self.assertFalse(self.report_exists())

    def test_unreachable_aggregate_count_must_match_the_per_check_results(self):
        inconsistent = self.kani_output(
            extra_checks=(
                (
                    "poly1305::Poly1305State::update.assertion.1",
                    "UNREACHABLE",
                    "attempt to subtract with overflow",
                ),
            )
        )
        inconsistent.result_text = inconsistent.result_text.replace(
            "(1 unreachable)", "(0 unreachable)"
        )
        with patch("record_assurance.shutil.which", side_effect=lambda exe: f"/usr/bin/{exe}"), patch(
            "record_assurance.subprocess.run",
            side_effect=self.fake_kani(proof_result=inconsistent),
        ):
            with self.assertRaises(InvalidRecordInput) as caught:
                self.record()
        self.assertIn("aggregate unreachable count", str(caught.exception))
        self.assertFalse(self.report_exists())

    def test_missing_per_check_description_is_rejected_as_incomplete(self):
        malformed = self.kani_output()
        malformed.result_text = (
            "RESULTS:\n"
            f"Check 1: {self.HARNESS_IDENTITY}.assertion.1\n"
            "\t - Status: SUCCESS\n"
            "SUMMARY:\n ** 0 of 1 failed\nVERIFICATION:- SUCCESSFUL\n"
        )
        with patch("record_assurance.shutil.which", side_effect=lambda exe: f"/usr/bin/{exe}"), patch(
            "record_assurance.subprocess.run", side_effect=self.fake_kani(proof_result=malformed)
        ):
            with self.assertRaises(InvalidRecordInput) as caught:
                self.record()
        self.assertIn("malformed or incomplete result block", str(caught.exception))
        self.assertFalse(self.report_exists())

    def test_malformed_kani_transcript_is_rejected(self):
        malformed = SimpleNamespace(returncode=0, stdout="VERIFICATION:- SUCCESSFUL\n", stderr="")
        with patch("record_assurance.shutil.which", side_effect=lambda exe: f"/usr/bin/{exe}"), patch(
            "record_assurance.subprocess.run", side_effect=self.fake_kani(proof_result=malformed)
        ):
            with self.assertRaises(InvalidRecordInput):
                self.record()
        self.assertFalse(self.report_exists())

    def test_timeout_is_an_incomplete_proof_not_a_success(self):
        with patch("record_assurance.shutil.which", side_effect=lambda exe: f"/usr/bin/{exe}"), patch(
            "record_assurance.subprocess.run", side_effect=self.fake_kani(proof_timeout=True)
        ):
            with self.assertRaises(EvidenceNotEstablished) as caught:
                self.record()
        self.assertIn("timed out", str(caught.exception))
        self.assertFalse(self.report_exists())

    def test_manifest_harness_mismatch_is_rejected_before_kani_runs(self):
        with patch("record_assurance.shutil.which", side_effect=lambda exe: f"/usr/bin/{exe}"), patch(
            "record_assurance.subprocess.run"
        ) as run:
            with self.assertRaises(InvalidRecordInput) as caught:
                record_assurance_module.record_assurance(
                    self.root,
                    WORK_PACKAGE,
                    [],
                    kani_harness_specs=[f"{self.OBLIGATION}=kani_other_harness"],
                    kani_crate="crates/scheduler",
                )
        self.assertIn("manifest binds this obligation", str(caught.exception))
        run.assert_not_called()
        self.assertFalse(self.report_exists())

    def test_unknown_obligation_is_rejected(self):
        with patch("record_assurance.shutil.which", side_effect=lambda exe: f"/usr/bin/{exe}"), patch(
            "record_assurance.subprocess.run"
        ) as run:
            with self.assertRaises(InvalidRecordInput) as caught:
                record_assurance_module.record_assurance(
                    self.root,
                    WORK_PACKAGE,
                    [],
                    kani_harness_specs=[f"Unknown.C9={self.HARNESS}"],
                    kani_crate="crates/scheduler",
                )
        self.assertIn("is not a provided guarantee", str(caught.exception))
        run.assert_not_called()
        self.assertFalse(self.report_exists())

    def test_manifest_named_but_absent_harness_is_rejected(self):
        unknown_harness = "kani_unknown_fixture"
        self.provided = [
            guarantee(
                self.OBLIGATION,
                harness=unknown_harness,
                evidence=("kani-bounded-model-check",),
                minimum_scope={
                    "input_domain": f"the bounded model encoded by Kani harness {unknown_harness}"
                },
            )
        ]
        data = manifest(self.provided)
        data["provenance"]["toolchain"] = self.TOOLCHAIN
        data["provenance"]["target"] = self.TARGET
        self.write_manifest(data)
        with patch("record_assurance.subprocess.run") as run:
            with self.assertRaises(InvalidRecordInput) as caught:
                record_assurance_module.record_assurance(
                    self.root,
                    WORK_PACKAGE,
                    [],
                    kani_harness_specs=[f"{self.OBLIGATION}={unknown_harness}"],
                    kani_crate="crates/scheduler",
                )
        self.assertIn("exactly one explicit Rust function definition", str(caught.exception))
        run.assert_not_called()
        self.assertFalse(self.report_exists())

    def test_missing_kani_identity_is_reported_before_proof_run(self):
        with patch("record_assurance.shutil.which", side_effect=lambda exe: f"/usr/bin/{exe}"), patch(
            "record_assurance.subprocess.run", side_effect=self.fake_kani(metadata_harnesses=[])
        ) as run:
            with self.assertRaises(InvalidRecordInput) as caught:
                self.record()
        self.assertIn("no harness named", str(caught.exception))
        self.assertEqual(sum("--output-into-files" in call.args[0] for call in run.call_args_list), 0)
        self.assertFalse(self.report_exists())

    def test_ambiguous_kani_identity_is_rejected_without_selecting_one(self):
        identities = [
            self.HARNESS_IDENTITY,
            f"second_module::{self.HARNESS}",
        ]
        with patch("record_assurance.shutil.which", side_effect=lambda exe: f"/usr/bin/{exe}"), patch(
            "record_assurance.subprocess.run", side_effect=self.fake_kani(metadata_harnesses=identities)
        ) as run:
            with self.assertRaises(InvalidRecordInput) as caught:
                self.record()
        self.assertIn("is ambiguous", str(caught.exception))
        self.assertIn(self.HARNESS_IDENTITY, str(caught.exception))
        self.assertIn(identities[1], str(caught.exception))
        self.assertEqual(sum("--output-into-files" in call.args[0] for call in run.call_args_list), 0)
        self.assertFalse(self.report_exists())

    def test_malformed_kani_compiler_metadata_is_rejected(self):
        with patch("record_assurance.shutil.which", side_effect=lambda exe: f"/usr/bin/{exe}"), patch(
            "record_assurance.subprocess.run",
            side_effect=self.fake_kani(metadata_text='{"crate_name":"scheduler"'),
        ) as run:
            with self.assertRaises(InvalidRecordInput) as caught:
                self.record()
        self.assertIn("malformed JSON", str(caught.exception))
        self.assertEqual(sum("--output-into-files" in call.args[0] for call in run.call_args_list), 0)
        self.assertFalse(self.report_exists())

    def test_only_the_pinned_kani_version_is_accepted(self):
        def run(command, **kwargs):
            if command[-1] == "--version":
                return SimpleNamespace(returncode=0, stdout="cargo-kani 0.68.0\n", stderr="")
            return self.kani_output()

        with patch("record_assurance.shutil.which", side_effect=lambda exe: f"/usr/bin/{exe}"), patch(
            "record_assurance.subprocess.run", side_effect=run
        ):
            with self.assertRaises(InvalidRecordInput) as caught:
                self.record()
        self.assertIn("supports only cargo-kani 0.67.0", str(caught.exception))
        self.assertFalse(self.report_exists())

    def test_current_harness_change_makes_record_stale_in_g14(self):
        with patch("record_assurance.shutil.which", side_effect=lambda exe: f"/usr/bin/{exe}"), patch(
            "record_assurance.subprocess.run", side_effect=self.fake_kani()
        ):
            self.record()
        source = self.kani_root / "src" / "lib.rs"
        source.write_text(source.read_text().replace("wrapping_add(0)", "wrapping_add(1)"))
        manifests, closure, reports, findings = self.gate_view()
        self.assertEqual(findings, [])
        recheck = gate_g14.check_record_certificates(self.root, closure, reports, manifests)
        self.assertEqual(len(recheck), 1)
        self.assertIn("harness", recheck[0].reason)
        self.assertIn("changed after assurance was recorded", recheck[0].reason)

    def test_other_source_change_makes_record_stale_in_g14(self):
        with patch("record_assurance.shutil.which", side_effect=lambda exe: f"/usr/bin/{exe}"), patch(
            "record_assurance.subprocess.run", side_effect=self.fake_kani()
        ):
            self.record()
        other = self.kani_root / "src" / "helper.rs"
        other.write_text("pub fn changed_source() {}\n")
        manifests, closure, reports, findings = self.gate_view()
        self.assertEqual(findings, [])
        recheck = gate_g14.check_record_certificates(self.root, closure, reports, manifests)
        self.assertEqual(len(recheck), 1)
        self.assertIn("Rust source snapshot changed", recheck[0].reason)

    def test_broader_manifest_scope_is_not_laundered_by_bounded_result(self):
        broad = manifest(
            [
                guarantee(
                    self.OBLIGATION,
                    harness=self.HARNESS,
                    claims=("callee-precondition-established",),
                    evidence=("kani-bounded-model-check",),
                    minimum_scope={"input_domain": "all possible scheduler inputs"},
                )
            ]
        )
        broad["provenance"]["toolchain"] = self.TOOLCHAIN
        broad["provenance"]["target"] = self.TARGET
        broad["provenance"]["features"] = [self.FEATURE]
        self.write_manifest(broad)
        with patch("record_assurance.shutil.which", side_effect=lambda exe: f"/usr/bin/{exe}"), patch(
            "record_assurance.subprocess.run", side_effect=self.fake_kani()
        ):
            outcome = self.record()
        self.assertIn(self.OBLIGATION, outcome.scope_warnings)
        record = self.report()["obligation_records"][0]["record"]
        self.assertEqual(record["evidence"]["scope"]["input_domain"], self.INPUT_DOMAIN)
        manifests, closure, reports, findings = self.gate_view()
        self.assertEqual(findings, [])
        self.assertEqual(gate_g14.check_record_certificates(self.root, closure, reports, manifests), [])
        blocked = gate_g14.check_provided_guarantees(CLUSTER, PROFILE, closure, manifests, reports)
        self.assertEqual(len(blocked), 1)
        self.assertIn("minimum_scope['input_domain']", blocked[0].reason)

    def test_installed_kani_067_produces_a_g14_consumable_record(self):
        cargo = shutil.which("cargo")
        if cargo is None:
            self.skipTest("Cargo is not installed")
        version = subprocess.run(
            [cargo, f"+{self.TOOLCHAIN}", "kani", "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if version.returncode != 0 or version.stdout.strip() != "cargo-kani 0.67.0":
            self.skipTest("cargo-kani 0.67.0 is not installed for the test toolchain")
        with patch(
            "record_assurance.shutil.which",
            side_effect=lambda exe: cargo if exe == "cargo" else RUSTC_EXECUTABLE,
        ):
            code, output, error = self.run_cli()
            self.assertEqual(code, 0, error)
            self.assertIn("kani-bounded-model-check supported", output)
            manifests, closure, reports, findings = self.gate_view()
            self.assertEqual(findings, [])
            self.assertEqual(gate_g14.check_record_certificates(self.root, closure, reports, manifests), [])
            self.assertEqual(gate_g14.check_record_derivation(closure, manifests, reports), [])
            self.assertEqual(
                gate_g14.check_provided_guarantees(CLUSTER, PROFILE, closure, manifests, reports),
                [],
            )

    def test_cli_help_documents_kani_protocol_and_bound(self):
        output = StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as caught:
            pipeline.main(["record-assurance", "--help"])
        self.assertEqual(caught.exception.code, 0)
        help_text = " ".join(output.getvalue().split())
        help_text = re.sub(r"(\w)-\s+(\w)", r"\1-\2", help_text)
        self.assertIn("0.67.0 is supported", help_text)
        self.assertIn("JSON verdict intake", help_text)
        self.assertIn("--only-codegen", help_text)
        self.assertIn("fully qualified", help_text)
        self.assertIn("broader", help_text)
        self.assertIn("manifest minimum_scope", help_text)
        self.assertIn("result_output_dir/<qualified-harness>", help_text)
        self.assertIn("VERIFICATION:- SUCCESSFUL", help_text)
        self.assertIn("UNREACHABLE", help_text)
        self.assertIn("cover properties", help_text)
        self.assertIn("matches the recorded summary", help_text)


class TheIssuesBaselineTest(RecordAssuranceTestCase):
    """Before recording, gate-g14 reports exactly what chainlink #87
    quotes: no report at the manifest's own path, and one 'recorded no
    achieved assurance' per provided guarantee. After recording, all of it
    is gone -- that is the Expected of this issue, asserted end to end
    through the gate's own functions rather than through a re-reading of
    the report."""

    def test_gate_reports_no_achieved_assurance_before_recording(self):
        manifests, closure, reports, findings = self.gate_view()
        self.assertTrue(findings, "the issue's baseline: nothing has been recorded")
        self.assertIn("no assurance report at its manifest's own report.emit path", str(findings[0]))
        self.assertEqual(reports, {})
        provided_findings = gate_g14.check_provided_guarantees(
            CLUSTER, PROFILE, closure, manifests, reports
        )
        self.assertEqual(len(provided_findings), 2)
        for finding in provided_findings:
            self.assertIn("recorded no achieved assurance", finding.reason)

    def test_gate_accepts_the_recorded_assurance(self):
        outcome = record_assurance(self.root, WORK_PACKAGE, self.specs)
        self.assertEqual([e.obligation_id for e in outcome.recorded], ["Weekday.C1", "Weekday.C2"])
        self.assertEqual(outcome.replaced, "absent")

        manifests, closure, reports, findings = self.gate_view()
        self.assertEqual(findings, [], "the report the gate reads must be schema-valid")
        self.assertEqual(
            gate_g14.check_provided_guarantees(CLUSTER, PROFILE, closure, manifests, reports),
            [],
            "every provided guarantee must be satisfied by what was recorded",
        )
        # CG3 (#86, derivation #93) is computed from these very records
        # once they exist: closure scope, the project's manifests, and
        # the workspace root for reading any report outside the closure.
        verifiable, reason = gate_g14.generic_callees_verification(
            closure, reports, manifests, self.root
        )
        self.assertTrue(verifiable, reason)

    def test_records_are_derived_not_asserted(self):
        record_assurance(self.root, WORK_PACKAGE, self.specs)
        report = self.report()
        self.assertEqual(report["work_package"], WORK_PACKAGE)
        self.assertEqual(report["bridge_records"], [])
        by_id = {entry["obligation_id"]: entry["record"] for entry in report["obligation_records"]}
        record = by_id["Weekday.C1"]
        self.assertEqual(record["claim"], {"kind": "postcondition-holds", "result": "pass"})
        self.assertEqual(record["support"], {"status": "supported"})
        self.assertEqual(record["trust"], {"assumptions": []})
        self.assertEqual(record["evidence"]["kind"], "creusot-deductive-check")
        self.assertEqual(record["evidence"]["verifier"], "creusot")
        self.assertEqual(record["evidence"]["harness"], "creusot")
        # scope names what was checked; it never copies a required
        # minimum_scope into the achievement.
        self.assertEqual(
            record["evidence"]["scope"],
            {"proof_targets": "verif/weekday/weekday_from_days/proof.json"},
        )
        # config comes from the manifest's own provenance block.
        self.assertEqual(
            record["config"],
            {"toolchain": "nightly-2026-05-01", "target": "x86_64-unknown-linux-gnu", "features": ["default"]},
        )
        # and the record is one satisfies() accepts against the manifest.
        entry = next(e for e in self.provided if e["obligation_id"] == "Weekday.C1")
        self.assertTrue(satisfies(entry["required_assurance"], record))


class ConceptAnchorTest(unittest.TestCase):
    def test_concept_segment_is_the_snake_case_of_the_id(self):
        self.assertEqual(concept_segment("Weekday.C1"), "weekday")
        self.assertEqual(concept_segment("IsoWeek.C2"), "iso_week")
        self.assertEqual(concept_segment("YearMonthDayLast.C1"), "year_month_day_last")
        self.assertEqual(concept_segment("YearMonthDay.C2"), "year_month_day")


class RefusalsExitOneTest(RecordAssuranceTestCase):
    """Exit 1 per docs/exit-code-contract.md: the evidence was readable
    and does not establish the obligation -- a real, actionable finding,
    never a silent partial record, and never a write."""

    def assert_refused(self, specs, needle: str) -> None:
        with self.assertRaises(EvidenceNotEstablished) as caught:
            record_assurance(self.root, WORK_PACKAGE, specs)
        self.assertIn(needle, str(caught.exception))
        self.assertEqual(caught.exception.exit_code, 1)
        self.assertFalse(self.report_exists(), "a refusal must leave nothing behind")

    def test_a_stuck_subgoal_is_not_a_record(self):
        write_certificate(self.root, "verif/weekday/impl_Weekday/iso_encoding", proved=1, stuck=2)
        self.assert_refused(
            [
                "Weekday.C1=verif/weekday/weekday_from_days",
                "Weekday.C2=verif/weekday/impl_Weekday/iso_encoding",
            ],
            "stuck/incomplete",
        )

    def test_one_stuck_target_among_several_refuses_the_obligation(self):
        write_certificate(self.root, "verif/weekday/weekday_from_days_from_days", proved=1)
        write_certificate(self.root, "verif/weekday/weekday_from_days_bounded", proved=1, stuck=1)
        self.assert_refused(
            [
                "Weekday.C1=verif/weekday/weekday_from_days_from_days",
                "Weekday.C1=verif/weekday/weekday_from_days_bounded",
                "Weekday.C2=verif/weekday/impl_Weekday/c_encoding",
            ],
            "stuck/incomplete",
        )

    def test_a_certificate_older_than_its_coma_program_is_stale(self):
        write_certificate(
            self.root, "verif/weekday/weekday_from_days_stale", proved=1, coma=True, coma_newer=True
        )
        self.assert_refused(
            ["Weekday.C1=verif/weekday/weekday_from_days_stale"],
            "is older than the Coma program it certifies",
        )

    def test_a_certificate_that_proves_nothing_refuses(self):
        directory = self.root / "verif/weekday/weekday_from_days_empty"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "proof.json").write_text(json.dumps({"profile": [], "proofs": {"Coma": {}}}))
        self.assert_refused(
            ["Weekday.C1=verif/weekday/weekday_from_days_empty"],
            "proves no goal at all",
        )

    def test_a_minimum_scope_is_refused_not_copied_into_the_achievement(self):
        """A required minimum_scope is a domain claim; copying it out of
        the requirement into the record would be exactly the fabrication
        docs/achieved-assurance-schema.json forbids, so the guarantee is
        refused with satisfies()' own reasons instead."""
        self.provided = [
            guarantee(
                "Weekday.C1",
                minimum_scope={"input_domain": "every int32"},
            )
        ]
        self.write_manifest()
        with self.assertRaises(EvidenceNotEstablished) as caught:
            record_assurance(self.root, WORK_PACKAGE, ["Weekday.C1=verif/weekday/weekday_from_days"])
        self.assertIn("would not satisfy the manifest's own required_assurance", str(caught.exception))
        self.assertIn("minimum_scope", str(caught.exception))
        self.assertFalse(self.report_exists())


class InvalidInputExitTwoTest(RecordAssuranceTestCase):
    """Exit 2: the operation could not be attempted -- the argument, the
    manifest, or the evidence path is wrong before any proof is read."""

    def assert_invalid(self, specs, needle: str, *, target: str = WORK_PACKAGE) -> None:
        with self.assertRaises(InvalidRecordInput) as caught:
            record_assurance(self.root, target, specs)
        self.assertIn(needle, str(caught.exception))
        self.assertEqual(caught.exception.exit_code, 2)
        self.assertFalse(self.report_exists(), "a refusal must leave nothing behind")

    def test_no_proof_specs(self):
        self.assert_invalid([], "no proof evidence given")

    def test_malformed_spec(self):
        self.assert_invalid(["Weekday.C1"], "<obligation>=<path>")

    def test_obligation_id_grammar(self):
        self.assert_invalid(["weekday.c1=verif/weekday/weekday_from_days"], "obligation id grammar")

    def test_obligation_this_manifest_does_not_provide(self):
        self.assert_invalid(
            ["YearMonthDay.C1=verif/weekday/weekday_from_days"],
            "not a provided guarantee",
        )

    def test_evidence_path_missing(self):
        self.assert_invalid(["Weekday.C1=verif/nope"], "evidence path does not exist")

    def test_evidence_path_is_not_a_certificate_name(self):
        certificate = write_certificate(self.root, "verif/weekday/weekday_from_days")
        backup = certificate.with_name("proof.json.bak")
        backup.write_text(certificate.read_text())
        self.assert_invalid(
            ["Weekday.C1=verif/weekday/weekday_from_days/proof.json.bak"],
            "not a why3find proof certificate",
        )

    def test_directory_without_a_certificate(self):
        (self.root / "verif" / "weekday" / "empty").mkdir(parents=True, exist_ok=True)
        self.assert_invalid(["Weekday.C1=verif/weekday/empty"], "no proof.json in")

    def test_evidence_outside_the_workspace(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        elsewhere = Path(tmp.name)
        (elsewhere / "weekday").mkdir(parents=True, exist_ok=True)
        (elsewhere / "weekday" / "proof.json").write_text(json.dumps({"proofs": {"Coma": {}}}))
        self.assert_invalid(
            ["Weekday.C1=" + str(elsewhere / "weekday" / "proof.json")],
            "resolves outside the workspace",
        )

    def test_certificate_of_another_concept(self):
        write_certificate(self.root, "verif/year_month_day/impl_YearMonthDay/from_days")
        self.assert_invalid(
            ["Weekday.C1=verif/year_month_day/impl_YearMonthDay/from_days"],
            "must sit under a directory named for its concept",
        )

    def test_certificate_without_a_proof_section(self):
        directory = self.root / "verif" / "weekday" / "not_a_cert"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "proof.json").write_text(json.dumps({"hello": "world"}))
        self.assert_invalid(
            ["Weekday.C1=verif/weekday/not_a_cert"], "carries no `proofs`"
        )

    def test_certificate_with_an_unrecognized_node_shape(self):
        directory = self.root / "verif" / "weekday" / "weird"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "proof.json").write_text(json.dumps({"proofs": {"Coma": {"vc_x": 7}}}))
        self.assert_invalid(
            ["Weekday.C1=verif/weekday/weird"], "unrecognized certificate node"
        )

    def test_missing_manifest(self):
        self.assert_invalid(self.specs, "work-package manifest not found", target="WP-NOPE")

    def test_manifest_must_be_where_the_gate_reads_it(self):
        elsewhere = self.root / "somewhere" / "manifest.json"
        elsewhere.parent.mkdir(parents=True, exist_ok=True)
        elsewhere.write_text(json.dumps(manifest(self.provided)))
        self.assert_invalid(self.specs, "manifest not at the path gate-g14 reads", target=str(elsewhere))

    def test_manifest_filename_and_id_must_agree(self):
        self.write_manifest(name="WP-OTHER")
        self.assert_invalid(self.specs, "its filename says", target="WP-OTHER")

    def test_schema_invalid_manifest(self):
        data = manifest(self.provided)
        del data["provenance"]
        self.write_manifest(data)
        self.assert_invalid(self.specs, "is not schema-valid")

    def test_a_harness_with_no_certificate_reader(self):
        self.provided = [guarantee("Weekday.C1", harness="kani")]
        self.write_manifest()
        self.assert_invalid(["Weekday.C1=verif/weekday/weekday_from_days"], "no proof-certificate reader")

    def test_an_evidence_kind_the_guarantee_does_not_accept(self):
        self.provided = [guarantee("Weekday.C1", evidence=("kani-bounded-model-check",))]
        self.write_manifest()
        self.assert_invalid(["Weekday.C1=verif/weekday/weekday_from_days"], "does not accept")

    def test_a_claim_the_guarantee_does_not_require(self):
        self.provided = [guarantee("Weekday.C1", claims=("callee-precondition-established",))]
        self.write_manifest()
        self.assert_invalid(["Weekday.C1=verif/weekday/weekday_from_days"], "does not require")


class ExistingReportTest(RecordAssuranceTestCase):
    """What the manifest's report.emit path may already hold -- updated,
    replaced with a warning, or refused, never destroyed."""

    def test_a_feature_ledger_is_replaced_and_classified_as_such(self):
        ledger = {
            "schema_version": "1.0",
            "generated_from": {
                "concept_specs_hash": "sha256:" + "0" * 64,
                "witness_specs_hash": "sha256:" + "0" * 64,
                "assurance_results_hash": "sha256:" + "0" * 64,
            },
            "features": [],
        }
        (self.root / EMIT).parent.mkdir(parents=True, exist_ok=True)
        (self.root / EMIT).write_text(json.dumps(ledger))
        outcome = record_assurance(self.root, WORK_PACKAGE, self.specs)
        self.assertEqual(outcome.replaced, "feature-ledger")
        self.assertEqual(len(self.report()["obligation_records"]), 2)

    def test_unrecognized_content_at_the_emit_path_is_refused(self):
        (self.root / EMIT).parent.mkdir(parents=True, exist_ok=True)
        (self.root / EMIT).write_text(json.dumps({"something": "else"}))
        with self.assertRaises(InvalidRecordInput) as caught:
            record_assurance(self.root, WORK_PACKAGE, self.specs)
        self.assertIn("neither an assurance report nor a feature ledger", str(caught.exception))
        self.assertEqual(
            json.loads((self.root / EMIT).read_text()), {"something": "else"},
            "content this command cannot describe must survive untouched",
        )

    def test_another_work_packages_report_is_refused(self):
        (self.root / EMIT).parent.mkdir(parents=True, exist_ok=True)
        foreign = {
            "schema_version": "1.0",
            "work_package": "WP-OTHER",
            "obligation_records": [],
            "bridge_records": [],
        }
        (self.root / EMIT).write_text(json.dumps(foreign))
        with self.assertRaises(InvalidRecordInput) as caught:
            record_assurance(self.root, WORK_PACKAGE, self.specs)
        self.assertIn("for work package 'WP-OTHER'", str(caught.exception))

    def test_an_existing_report_is_updated_not_replaced(self):
        existing = {
            "schema_version": "1.0",
            "work_package": WORK_PACKAGE,
            "obligation_records": [
                {
                    "obligation_id": "Weekday.C9",
                    "record": {
                        "schema_version": "1.0",
                        "claim": {"kind": "postcondition-holds", "result": "pass"},
                        "evidence": {
                            "kind": "creusot-deductive-check",
                            "verifier": "creusot",
                            "harness": "creusot",
                            "scope": {"proof_targets": "somewhere/else/proof.json"},
                        },
                        "trust": {"assumptions": []},
                        "support": {"status": "supported"},
                        "config": {
                            "toolchain": "nightly-2026-05-01",
                            "target": "x86_64-unknown-linux-gnu",
                            "features": ["default"],
                        },
                    },
                }
            ],
            "bridge_records": [
                {
                    "bridge_id": "BR-TEST-001",
                    "record": {
                        "schema_version": "1.0",
                        "claim": {"kind": "callee-precondition-established", "result": "pass"},
                        "evidence": {
                            "kind": "creusot-deductive-check",
                            "verifier": "creusot",
                            "harness": "creusot",
                            "scope": {"proof_targets": "a/proof.json"},
                        },
                        "trust": {"assumptions": []},
                        "support": {"status": "supported"},
                        "config": {
                            "toolchain": "nightly-2026-05-01",
                            "target": "x86_64-unknown-linux-gnu",
                            "features": ["default"],
                        },
                    },
                }
            ],
        }
        (self.root / EMIT).parent.mkdir(parents=True, exist_ok=True)
        (self.root / EMIT).write_text(json.dumps(existing))
        outcome = record_assurance(self.root, WORK_PACKAGE, self.specs)
        self.assertEqual(outcome.replaced, "assurance-report")
        self.assertEqual(outcome.preserved_obligation_records, 1)
        self.assertEqual(outcome.bridge_records, 1)
        report = self.report()
        self.assertEqual(
            [entry["obligation_id"] for entry in report["obligation_records"]],
            ["Weekday.C9", "Weekday.C1", "Weekday.C2"],
        )
        self.assertEqual(report["bridge_records"][0]["bridge_id"], "BR-TEST-001")

    def test_a_required_bridge_with_no_check_is_not_invented(self):
        """plan.md §8.3's direction, one way: the report's bridge half
        comes from the checks that ran -- with no check on record there
        is no record, and gate-g9/g14 report that honestly."""
        self.provided = [guarantee("Weekday.C1")]
        self.write_manifest(manifest(self.provided, bridges=["BR-TEST-001"]))
        record_assurance(self.root, WORK_PACKAGE, ["Weekday.C1=verif/weekday/weekday_from_days"])
        self.assertEqual(self.report()["bridge_records"], [])


class BridgeOnlyAssuranceTest(RecordAssuranceTestCase):
    """A bridge-only work package has no obligation proof to map, but its
    passing G9 bridge records still belong in the report G14 consumes."""

    BRIDGE_ID = "BR-TEST-001"

    def setUp(self):
        super().setUp()
        self.provided = []
        self.write_manifest(manifest([], bridges=[self.BRIDGE_ID]))
        (self.root / "project-descriptor.json").write_text("{}\n")

    def passing_bridge_record(self):
        return {
            "schema_version": "1.0",
            "claim": {"kind": "callee-precondition-established", "result": "pass"},
            "evidence": {
                "kind": "creusot-deductive-check",
                "verifier": "creusot",
                "harness": "creusot-bridge-harness",
                "scope": {"proof_targets": "ci/harness/BR-TEST-001.rs"},
            },
            "trust": {"assumptions": []},
            "support": {"status": "supported"},
            "config": {
                "toolchain": "nightly-2026-05-01",
                "target": "x86_64-unknown-linux-gnu",
                "features": ["default"],
            },
        }

    def _run_bridge_only(self, bridge_records=None):
        records = bridge_records if bridge_records is not None else [
            {"bridge_id": self.BRIDGE_ID, "record": self.passing_bridge_record()}
        ]
        with (
            patch("project_descriptor.load_project_descriptor", return_value={}),
            patch("gate_g9.gate_workspace", return_value=([], 1)),
            patch("gate_g9.bridge_records_for", return_value=records),
        ):
            output, errors = StringIO(), StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                code = pipeline.main([
                    "--workspace", str(self.root), "record-assurance", WORK_PACKAGE,
                    "--bridges-only",
                ])
        return code, output.getvalue(), errors.getvalue()

    def test_cli_records_exact_g9_bridges_and_g14_consumes_report(self):
        extra = {"bridge_id": "BR-OTHER", "record": self.passing_bridge_record()}
        code, output, error = self._run_bridge_only(
            [
                {"bridge_id": self.BRIDGE_ID, "record": self.passing_bridge_record()},
                extra,
            ]
        )
        self.assertEqual(code, 0, error)
        self.assertIn("recorded bridge-only assurance", output)
        report = self.report()
        self.assertEqual(report["obligation_records"], [])
        self.assertEqual([entry["bridge_id"] for entry in report["bridge_records"]], [self.BRIDGE_ID])
        self.assertEqual(list(gate_g14.load_assurance_report_validator().iter_errors(report)), [])

        manifests, manifest_findings = gate_g14.load_manifests(self.root)
        self.assertEqual(manifest_findings, [])
        closure = gate_g14.Closure(work_packages=[WORK_PACKAGE])
        reports, findings = gate_g14.load_assurance_reports(self.root, closure, manifests)
        self.assertEqual(findings, [])
        self.assertEqual(
            gate_g14.check_bridges(
                CLUSTER,
                PROFILE,
                closure,
                manifests,
                reports,
                {self.BRIDGE_ID: {"protocol_class": "pairwise"}},
                0,
            ),
            [],
        )

    def test_bridge_only_refuses_missing_required_g9_record_without_writing(self):
        code, _output, error = self._run_bridge_only([])
        self.assertEqual(code, 1)
        self.assertIn("no current passing bridge record", error)
        self.assertFalse(self.report_exists())

    def test_bridge_only_refuses_g9_findings_without_writing(self):
        with (
            patch("project_descriptor.load_project_descriptor", return_value={}),
            patch(
                "gate_g9.gate_workspace",
                return_value=(["stale G9 harness"], 1),
            ),
        ):
            with self.assertRaises(EvidenceNotEstablished) as caught:
                record_assurance(self.root, WORK_PACKAGE, [], bridges_only=True)
        self.assertIn("G9 has not validated", str(caught.exception))
        self.assertFalse(self.report_exists())

    def test_bridge_only_refuses_record_that_does_not_meet_manifest_scope(self):
        data = manifest([], bridges=[self.BRIDGE_ID])
        data["definition_of_done"]["required_preconditions_to_establish"][0][
            "required_assurance"
        ]["minimum_scope"] = {"input_domain": "reviewed bounded bridge model"}
        self.write_manifest(data)
        with (
            patch("project_descriptor.load_project_descriptor", return_value={}),
            patch("gate_g9.gate_workspace", return_value=([], 1)),
            patch(
                "gate_g9.bridge_records_for",
                return_value=[{"bridge_id": self.BRIDGE_ID, "record": self.passing_bridge_record()}],
            ),
        ):
            with self.assertRaises(EvidenceNotEstablished) as caught:
                record_assurance(self.root, WORK_PACKAGE, [], bridges_only=True)
        self.assertIn("minimum_scope['input_domain']", str(caught.exception))
        self.assertFalse(self.report_exists())

    def test_bridge_only_refuses_malformed_record_without_writing(self):
        malformed = {"bridge_id": self.BRIDGE_ID, "record": {"claim": {"result": "pass"}}}
        code, _output, error = self._run_bridge_only([malformed])
        self.assertEqual(code, 1)
        self.assertIn("not a valid achieved record", error)
        self.assertFalse(self.report_exists())

    def test_bridge_only_is_refused_for_packages_with_provided_guarantees(self):
        self.write_manifest(manifest([guarantee("Weekday.C1")], bridges=[self.BRIDGE_ID]))
        with self.assertRaises(InvalidRecordInput) as caught:
            record_assurance(self.root, WORK_PACKAGE, [], bridges_only=True)
        self.assertIn("no provided guarantees", str(caught.exception))
        self.assertFalse(self.report_exists())

    def test_bridge_only_cli_help_documents_g9_requirement(self):
        output = StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as caught:
            pipeline.main(["record-assurance", "--help"])
        self.assertEqual(caught.exception.code, 0)
        help_text = " ".join(output.getvalue().split())
        self.assertIn("--bridges-only", help_text)
        self.assertIn("clean G9 validation", help_text)
        self.assertIn("passing G9 records", help_text)


class ManifestTargetTest(RecordAssuranceTestCase):
    def test_the_manifest_may_be_named_as_a_path(self):
        outcome = record_assurance(self.root, f"ci/manifest/{WORK_PACKAGE}.json", self.specs)
        self.assertEqual(outcome.work_package, WORK_PACKAGE)
        self.assertTrue(self.report_exists())


class CliIntegrationTest(RecordAssuranceTestCase):
    """The verb itself, through `pipeline.main()` -- the exit-code split
    docs/exit-code-contract.md asks for (0 recorded / 1 not established /
    2 invalid input) and one error line, never a traceback."""

    def _run(self, *args: str) -> tuple[int, str, str]:
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = pipeline.main(["--workspace", str(self.root), *args])
        return code, out.getvalue(), err.getvalue()

    def test_record_assurance_end_to_end(self):
        code, out, err = self._run(
            "record-assurance", WORK_PACKAGE, "--proof", self.specs[0], "--proof", self.specs[1]
        )
        self.assertEqual(code, 0, err)
        self.assertIn("recorded assurance: " + EMIT, out)
        self.assertIn("Weekday.C1: creusot-deductive-check supported", out)
        self.assertNotIn("Traceback", out + err)
        self.assertTrue(self.report_exists())

    def test_a_stuck_certificate_exits_1(self):
        write_certificate(self.root, "verif/weekday/impl_Weekday/iso_encoding", proved=1, stuck=1)
        code, out, err = self._run(
            "record-assurance",
            WORK_PACKAGE,
            "--proof",
            self.specs[0],
            "--proof",
            "Weekday.C2=verif/weekday/impl_Weekday/iso_encoding",
        )
        self.assertEqual(code, 1)
        self.assertIn("error: ", err)
        self.assertNotIn("Traceback", out + err)
        self.assertFalse(self.report_exists())

    def test_an_invalid_argument_exits_2(self):
        code, out, err = self._run("record-assurance", WORK_PACKAGE, "--proof", "Weekday.C1")
        self.assertEqual(code, 2)
        self.assertIn("<obligation>=<path>", err)
        self.assertNotIn("Traceback", out + err)

    def test_replacing_a_feature_ledger_is_warned_about(self):
        directory = self.root / "ci" / "results"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "feature_ledger.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "generated_from": {
                        "concept_specs_hash": "sha256:" + "0" * 64,
                        "witness_specs_hash": "sha256:" + "0" * 64,
                        "assurance_results_hash": "sha256:" + "0" * 64,
                    },
                    "features": [],
                }
            )
        )
        self.provided = [guarantee("Weekday.C1")]
        emit = "ci/results/feature_ledger.json"
        self.write_manifest(manifest(self.provided, emit=emit))
        code, out, err = self._run(
            "record-assurance", WORK_PACKAGE, "--proof", self.specs[0]
        )
        self.assertEqual(code, 0, err)
        self.assertIn("warning: ", err)
        self.assertIn("generated feature ledger", err)
        self.assertIn("report feature-ledger", err)


if __name__ == "__main__":
    unittest.main()
