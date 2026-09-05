"""G9: bridge generation, dispatch and verification (chainlink #47).

Built on tests/test_gate_g14.py's workspace builder -- one description of
a realistic workspace, not two -- plus a project descriptor whose
verifier_backends points at tests/fixtures/bridges/verifier/fake_verifier.py.
That stand-in is the honest answer to "what is the minimum verifier
dispatch in a repository with no Rust": the dispatch machinery is real
and the verifier is visibly a stand-in, the same precedent as
tests/fixtures/callsites/'s never-compiled Rust source.
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from bridge_harness import compile_bridge  # noqa: E402
from gate_g9 import (  # noqa: E402
    EXIT_BLOCKED,
    EXIT_OK,
    bridge_check_dir_for,
    bridge_records_for,
    check_bridges,
    gate_workspace,
    harness_dir_for,
    load_bridge_checks,
    owning_verifier_by_bridge,
    report_findings,
    resolve_verifier,
)
from test_gate_g14 import Workspace, bridge_spec  # noqa: E402

FAKE_VERIFIER = ROOT / "tests" / "fixtures" / "bridges" / "verifier" / "fake_verifier.py"
BRIDGE_ID = "BR-SCHED-TQ-001"


def descriptor_with(backends: dict | None = None, default: str = "creusot", cluster_verifier=None) -> dict:
    descriptor = json.loads(
        (ROOT / "schemas" / "examples" / "project-descriptor.greenfield.example.json").read_text()
    )
    descriptor["crates"] = [
        {"crate_dir": "crates/scheduler", "contracts_crate": "contracts", "specs_search_root": "crates"}
    ]
    descriptor["verifier_policy"] = {"default": default}
    if cluster_verifier:
        descriptor["verifier_policy"]["scheduler-core"] = cluster_verifier
    if backends is not None:
        descriptor["verifier_backends"] = backends
    return descriptor


def fake_backend(*extra_args: str) -> dict:
    command = f"python3 {FAKE_VERIFIER}"
    if extra_args:
        command += " " + " ".join(extra_args)
    return {"creusot": {"command": command}, "kani": {"command": command}, "verus": {"command": command}}


class G9TestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ws = Workspace(self.root)
        self.descriptor = descriptor_with(fake_backend())
        (self.root / "project-descriptor.json").write_text(json.dumps(self.descriptor))

    def tearDown(self):
        self._tmp.cleanup()

    def generate(self, descriptor=None):
        return check_bridges(self.root, descriptor or self.descriptor)

    def gate(self, descriptor=None):
        return gate_workspace(self.root, descriptor or self.descriptor)

    def errors(self, findings) -> list[str]:
        return [str(f) for f in findings if f.severity == "error"]

    def check_record(self) -> dict:
        return json.loads((bridge_check_dir_for(self.root) / f"{BRIDGE_ID}.json").read_text())

    def write_check_record(self, record: dict) -> None:
        (bridge_check_dir_for(self.root) / f"{BRIDGE_ID}.json").write_text(json.dumps(record, indent=2))

    def align_assurance_report(self) -> None:
        """Make WP-A's assurance report source its bridge_records from the
        checks that actually ran -- the honest way round."""
        path = self.root / "ci" / "results" / "WP-A.json"
        data = json.loads(path.read_text())
        data["bridge_records"] = bridge_records_for(self.root)
        path.write_text(json.dumps(data, indent=2))


class GenerationTest(G9TestCase):
    def test_generation_writes_a_harness_and_a_check_record(self):
        written, findings = self.generate()
        self.assertEqual(self.errors(findings), [])
        self.assertTrue((harness_dir_for(self.root) / f"{BRIDGE_ID}.creusot.rs").is_file())
        self.assertTrue((bridge_check_dir_for(self.root) / f"{BRIDGE_ID}.json").is_file())
        self.assertEqual(len(written), 2)

    def test_the_recorded_hash_is_the_hash_of_the_generated_harness(self):
        self.generate()
        harness = compile_bridge(bridge_spec(), "creusot")
        self.assertEqual(self.check_record()["harness"]["sha256"], harness.sha256)

    def test_the_record_is_a_real_achieved_assurance_record(self):
        self.generate()
        record = self.check_record()["record"]
        self.assertEqual(record["claim"]["kind"], "callee-precondition-established")
        self.assertEqual(record["claim"]["result"], "pass")
        self.assertEqual(record["evidence"]["kind"], "creusot-deductive-check")
        self.assertEqual(record["evidence"]["harness"], "bridge_br_sched_tq_001")
        self.assertEqual(record["evidence"]["scope"]["harness_hash"], self.check_record()["harness"]["sha256"])
        # config comes from the run, not from the descriptor
        self.assertEqual(record["config"]["toolchain"], "nightly-2026-05-01")

    def test_the_invocation_is_recorded(self):
        self.generate()
        dispatch = self.check_record()["dispatch"]
        self.assertIn(str(FAKE_VERIFIER), dispatch["command"])
        self.assertEqual(dispatch["exit_code"], 0)

    def test_records_load_back_cleanly(self):
        self.generate()
        checks, findings = load_bridge_checks(self.root)
        self.assertEqual(findings, [])
        self.assertEqual(sorted(checks), [BRIDGE_ID])


class NoVerifierTest(G9TestCase):
    """The honest minimum: with nothing configured to run, nothing is
    claimed."""

    def test_no_backend_generates_the_harness_but_records_nothing(self):
        descriptor = descriptor_with(backends=None)
        written, findings = self.generate(descriptor)
        self.assertTrue((harness_dir_for(self.root) / f"{BRIDGE_ID}.creusot.rs").is_file())
        self.assertFalse((bridge_check_dir_for(self.root) / f"{BRIDGE_ID}.json").exists())
        self.assertTrue(any("harness-tested, never bridge-checked" in e for e in self.errors(findings)))

    def test_a_missing_record_blocks_the_gate(self):
        self.generate(descriptor_with(backends=None))
        findings, _ = self.gate(descriptor_with(backends=None))
        self.assertTrue(any("no bridge check record" in e for e in self.errors(findings)))

    def test_a_crashed_verifier_records_nothing(self):
        _, findings = self.generate(descriptor_with(fake_backend("--crash")))
        self.assertFalse((bridge_check_dir_for(self.root) / f"{BRIDGE_ID}.json").exists())
        self.assertTrue(any("has not refuted anything" in e for e in self.errors(findings)))

    def test_an_unparsable_verdict_records_nothing(self):
        _, findings = self.generate(descriptor_with(fake_backend("--garbage")))
        self.assertFalse((bridge_check_dir_for(self.root) / f"{BRIDGE_ID}.json").exists())
        self.assertTrue(any("was not JSON" in e for e in self.errors(findings)))

    def test_a_missing_verifier_binary_records_nothing(self):
        backends = {"creusot": {"command": "definitely-not-a-real-verifier-binary"}}
        _, findings = self.generate(descriptor_with(backends))
        self.assertFalse((bridge_check_dir_for(self.root) / f"{BRIDGE_ID}.json").exists())
        self.assertTrue(any("could not invoke" in e for e in self.errors(findings)))


class GateTest(G9TestCase):
    def test_a_generated_and_aligned_workspace_passes(self):
        self.generate()
        self.align_assurance_report()
        findings, discovered = self.gate()
        self.assertEqual(self.errors(findings), [])
        self.assertEqual(discovered, 1)

    def test_a_failing_verifier_verdict_blocks(self):
        self.generate(descriptor_with(fake_backend("--verdict", "fail")))
        findings, _ = self.gate()
        self.assertTrue(any("bridge check failed" in e for e in self.errors(findings)))

    def test_a_hand_edited_harness_is_caught(self):
        self.generate()
        self.align_assurance_report()
        path = harness_dir_for(self.root) / f"{BRIDGE_ID}.creusot.rs"
        path.write_text(path.read_text().replace("ensures(", "ensures(true || "))
        findings, _ = self.gate()
        self.assertTrue(any("differs from what this bridge compiles to" in e for e in self.errors(findings)))

    def test_a_stale_hash_is_caught_when_the_bridge_changes(self):
        self.generate()
        self.align_assurance_report()
        # the bridge is edited after the check ran
        spec = bridge_spec()
        spec["bridge_logic"]["premises"] = ["Scheduler.C010(caller_self)"]
        self.ws.write("crates/scheduler/specs/_bridges/BR-SCHED-TQ-001.json", spec)
        findings, _ = self.gate()
        errors = self.errors(findings)
        self.assertTrue(any("is not what this bridge compiles to" in e for e in errors))

    def test_a_record_from_another_verifier_is_refused(self):
        self.generate()
        record = self.check_record()
        record["verifier"] = "kani"
        self.write_check_record(record)
        findings, _ = self.gate()
        self.assertTrue(any("cross-verifier composition" in e for e in self.errors(findings)))

    def test_a_record_claiming_the_wrong_kind_is_refused(self):
        self.generate()
        record = self.check_record()
        record["record"]["claim"]["kind"] = "postcondition-holds"
        self.write_check_record(record)
        findings, _ = self.gate()
        self.assertTrue(any("a bridge establishes a callee precondition" in e for e in self.errors(findings)))

    def test_a_record_whose_evidence_kind_does_not_match_the_verifier_is_refused(self):
        self.generate()
        record = self.check_record()
        record["record"]["evidence"]["kind"] = "kani-bounded-model-check"
        self.write_check_record(record)
        findings, _ = self.gate()
        self.assertTrue(any("is not the method creusot produces" in e for e in self.errors(findings)))

    def test_a_schema_invalid_record_is_reported_not_skipped(self):
        self.generate()
        record = self.check_record()
        del record["harness"]["sha256"]
        self.write_check_record(record)
        findings, _ = self.gate()
        errors = self.errors(findings)
        self.assertTrue(any("not schema-valid" in e for e in errors))
        self.assertTrue(any("no bridge check record" in e for e in errors))

    def test_a_record_for_no_promoted_bridge_is_reported(self):
        self.generate()
        record = self.check_record()
        record["bridge_id"] = "BR-GHOST-001"
        (bridge_check_dir_for(self.root) / "BR-GHOST-001.json").write_text(json.dumps(record))
        findings, _ = self.gate()
        self.assertTrue(any("leftover result" in e for e in self.errors(findings)))

    def test_an_uncompilable_bridge_blocks(self):
        spec = bridge_spec()
        spec["bridge_logic"]["premises"] = ["queue_len <= 8"]
        self.ws.write("crates/scheduler/specs/_bridges/BR-SCHED-TQ-001.json", spec)
        findings, _ = self.gate()
        self.assertTrue(any("does not compile" in e for e in self.errors(findings)))


class AssuranceReportCrossCheckTest(G9TestCase):
    """The relation #25's G14 was missing: it consumes bridge_records as
    evidence a bridge passed, and nothing checked that a record
    corresponded to a run."""

    def test_a_hand_written_bridge_record_is_refused(self):
        self.generate()
        # WP-A's report as built by the #25 fixture claims harness "h"
        findings, _ = self.gate()
        self.assertTrue(
            any("a claim, not a result" in e for e in self.errors(findings)),
            self.errors(findings),
        )

    def test_a_report_sourced_from_the_check_agrees(self):
        self.generate()
        self.align_assurance_report()
        findings, _ = self.gate()
        self.assertEqual([e for e in self.errors(findings) if "assurance report" in e], [])

    def test_a_report_asserting_a_pass_over_a_failed_check_is_refused(self):
        self.generate(descriptor_with(fake_backend("--verdict", "fail")))
        self.align_assurance_report()
        path = self.root / "ci" / "results" / "WP-A.json"
        data = json.loads(path.read_text())
        data["bridge_records"][0]["record"]["claim"]["result"] = "pass"
        path.write_text(json.dumps(data))
        findings, _ = self.gate()
        self.assertTrue(any("claim.result" in e for e in self.errors(findings)))

    def test_bridge_records_for_produces_report_shaped_entries(self):
        self.generate()
        entries = bridge_records_for(self.root)
        self.assertEqual([e["bridge_id"] for e in entries], [BRIDGE_ID])
        self.assertEqual(entries[0]["record"]["claim"]["kind"], "callee-precondition-established")


class OwningVerifierTest(G9TestCase):
    def test_the_owning_verifier_comes_from_the_cluster_that_requires_the_bridge(self):
        descriptor = descriptor_with(fake_backend(), default="kani", cluster_verifier="creusot")
        owned = owning_verifier_by_bridge(self.root, descriptor)
        self.assertEqual(owned[BRIDGE_ID], "creusot")
        self.assertEqual(resolve_verifier(BRIDGE_ID, owned, descriptor), "creusot")

    def test_an_unclaimed_bridge_falls_back_to_the_default(self):
        descriptor = descriptor_with(fake_backend(), default="verus")
        owned = owning_verifier_by_bridge(self.root, descriptor)
        self.assertEqual(resolve_verifier("BR-NOBODY-001", owned, descriptor), "verus")

    def test_the_harness_is_compiled_for_the_owning_verifier(self):
        descriptor = descriptor_with(fake_backend(), default="kani", cluster_verifier="verus")
        (self.root / "project-descriptor.json").write_text(json.dumps(descriptor))
        self.generate(descriptor)
        self.assertTrue((harness_dir_for(self.root) / f"{BRIDGE_ID}.verus.rs").is_file())
        self.assertEqual(self.check_record()["verifier"], "verus")


class ReportingTest(G9TestCase):
    def test_no_bridges_fails_closed(self):
        (self.root / "crates/scheduler/specs/_bridges/BR-SCHED-TQ-001.json").unlink()
        findings, discovered = self.gate()
        self.assertEqual(discovered, 0)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = report_findings(findings, discovered, self.root)
        self.assertEqual(code, EXIT_BLOCKED)
        self.assertIn("would claim what it never ran", buffer.getvalue())

    def test_a_clean_gate_reports_what_it_checked(self):
        self.generate()
        self.align_assurance_report()
        findings, discovered = self.gate()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = report_findings(findings, discovered, self.root)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("(1 discovered)", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
