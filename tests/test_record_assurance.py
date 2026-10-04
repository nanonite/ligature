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
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gate_g14  # noqa: E402
import pipeline  # noqa: E402
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
        self.assert_invalid([], "no --proof given")

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
