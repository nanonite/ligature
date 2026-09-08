"""Feature ledger generator (chainlink #34).

plan.md §16.4. Built on real workspaces on disk, reusing the exact
fixture helpers G18/G19/G20's own test suites already established --
concept specs, witness specs, canonical results, a real witness_backend
dispatch via the stand-in producer, and closure profiles -- for the
same reason every other gate test file in this codebase gives: a test
handing the generator pre-loaded dicts would not be testing the
generator anyone runs.
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import atomic_write  # noqa: E402
import generate_feature_ledger as gfl  # noqa: E402
from gate_g14 import work_package_manifest_dir_for  # noqa: E402
from validate_witness import snake_case, witness_dir_for  # noqa: E402
from witness_result import build_result, encode_grid, witness_result_dir_for  # noqa: E402
from test_gate_g14 import manifest  # noqa: E402
from test_gate_g19 import PRODUCER_BACKEND, run_producer  # noqa: E402
from test_validate_closure import valid_profile  # noqa: E402
from test_validate_witness import concept_spec, witness_spec  # noqa: E402


def descriptor_with(*crate_dirs: str, backend_command: str | None = None) -> dict:
    descriptor = {
        "crates": [
            {"crate_dir": crate_dir, "contracts_crate": "contracts", "specs_search_root": "crates"}
            for crate_dir in crate_dirs
        ]
    }
    if backend_command is not None:
        descriptor["witness_backend"] = {"command": backend_command}
    return descriptor


class Workspace:
    def __init__(self, root: Path, crate_dir: str = "crates/scheduler"):
        self.root = root
        self.crate_dir = crate_dir
        self.crate = {"crate_dir": crate_dir, "contracts_crate": "contracts", "specs_search_root": "crates"}
        (self.root / crate_dir / "specs").mkdir(parents=True, exist_ok=True)

    def write(self, relative: str, data) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) if isinstance(data, (dict, list)) else data)
        return path

    def write_concept_spec(
        self, cluster: str = "scheduling", witness_required: bool = True,
        filename: str = "task_queue.json", extra_queries: tuple[dict, ...] = (),
    ) -> Path:
        spec = concept_spec()
        spec["cluster"] = cluster
        spec["queries"][0]["witness_required"] = witness_required
        spec["queries"].extend(extra_queries)
        return self.write(f"{self.crate_dir}/specs/{filename}", spec)

    def write_witness(self, data: dict | None = None) -> Path:
        data = data if data is not None else witness_spec(value_hash=run_producer()["value_hash"])
        witness_dir_for(self.crate, self.root).mkdir(parents=True, exist_ok=True)
        stem = f"{snake_case(data['concept'])}.{data['query']}"
        return self.write(f"{self.crate_dir}/specs/_witnesses/{stem}.json", data)

    def write_result(self, data: dict) -> Path:
        directory = witness_result_dir_for(self.root)
        directory.mkdir(parents=True, exist_ok=True)
        return self.write(str((directory / f"{data['witness_id']}.json").relative_to(self.root)), data)

    def write_rendering(self, relative: str = "docs/witnesses/task_queue.load_factor.svg") -> Path:
        return self.write(relative, "<svg></svg>")

    def write_closure_profile(self, cluster: str, closure_kind: str = "deductive") -> Path:
        profile = valid_profile()
        profile["cluster"] = cluster
        profile["closure_kind"] = closure_kind
        profile["work_packages"] = ["WP-X"]
        return self.write(f"specs/_closure/{cluster}.json", profile)

    def write_manifest_and_report(self, work_package: str = "WP-X", obligation_records=()) -> None:
        m = manifest(work_package)
        self.write(str((work_package_manifest_dir_for(self.root) / f"{work_package}.json").relative_to(self.root)), m)
        report = {
            "schema_version": "1.0",
            "work_package": work_package,
            "obligation_records": list(obligation_records),
            "bridge_records": [],
        }
        self.write(m["report"]["emit"], report)


def happy_path_witness_and_rendering(ws: Workspace) -> dict:
    """A genuinely fully-checked green witness: matching spec, rendering,
    AND a stored canonical result -- G20's own check_must_vary reads a
    STORED result (load_results_by_witness), independent of G19's live
    regeneration dispatch, so a "happy path" that only wrote the spec
    and rendering would make G20 silently skip (no stored result to
    check) rather than genuinely pass, and "ok" would be true for the
    wrong reason."""
    baseline = run_producer()
    spec = witness_spec(value_hash=baseline["value_hash"])
    ws.write_witness(spec)
    ws.write_rendering()
    ws.write_result(baseline)
    return spec


class GateTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ws = Workspace(self.root)
        self.descriptor = descriptor_with("crates/scheduler", backend_command=PRODUCER_BACKEND)

    def tearDown(self):
        self._tmp.cleanup()

    def generate(self, descriptor: dict | None = None) -> dict:
        return gfl.generate_ledger(self.root, descriptor or self.descriptor)

    def feature(self, ledger: dict, name: str = "TaskQueue.load_factor") -> dict:
        for entry in ledger["features"]:
            if entry["feature"] == name:
                return entry
        raise AssertionError(f"{name} not found in {ledger['features']!r}")


class HappyPathTest(GateTestCase):
    def test_a_fully_clean_feature_is_observed_but_stays_unsupported(self):
        # The core regression this whole module exists to prevent: a
        # fully green witness must NEVER upgrade assurance_status.
        self.ws.write_concept_spec()
        happy_path_witness_and_rendering(self.ws)
        ledger = self.generate()
        entry = self.feature(ledger)
        self.assertTrue(entry["witness_present"])
        self.assertTrue(entry["implementation_observed"])
        self.assertEqual(entry["determinism"], "pass")
        self.assertEqual(entry["degeneracy"], "ok")
        self.assertEqual(entry["assurance_status"], "unsupported")
        self.assertEqual(entry["owning_cluster"], "scheduling")

    def test_schema_valid_output(self):
        self.ws.write_concept_spec()
        happy_path_witness_and_rendering(self.ws)
        ledger = self.generate()
        self.assertEqual(list(gfl.load_validator().iter_errors(ledger)), [])


class MixedDeclarationTest(GateTestCase):
    def test_an_undeclared_query_is_invisible_to_the_ledger(self):
        self.ws.write_concept_spec(witness_required=False)
        ledger = self.generate()
        self.assertEqual(ledger["features"], [])

    def test_mixed_witnessed_and_unwitnessed_queries_only_the_declared_one_appears(self):
        self.ws.write_concept_spec(extra_queries=(
            {"english": "How deep is the queue right now?", "rust_sig": "fn depth(&self) -> usize", "pure": True},
        ))
        happy_path_witness_and_rendering(self.ws)
        ledger = self.generate()
        names = [f["feature"] for f in ledger["features"]]
        self.assertEqual(names, ["TaskQueue.load_factor"])

    def test_zero_declared_features_is_a_clean_empty_ledger(self):
        ledger = self.generate()
        self.assertEqual(ledger["features"], [])
        self.assertEqual(list(gfl.load_validator().iter_errors(ledger)), [])


class G18StateTest(GateTestCase):
    def test_no_witness_at_all_leaves_every_column_honest(self):
        self.ws.write_concept_spec()
        ledger = self.generate()
        entry = self.feature(ledger)
        self.assertFalse(entry["witness_present"])
        self.assertFalse(entry["implementation_observed"])
        self.assertEqual(entry["determinism"], "not-checked")
        self.assertEqual(entry["degeneracy"], "not-checked")

    def test_a_valid_witness_with_no_rendering_is_not_present(self):
        self.ws.write_concept_spec()
        baseline = run_producer()
        self.ws.write_witness(witness_spec(value_hash=baseline["value_hash"]))
        # No write_rendering() -- G18 must catch the missing rendering.
        ledger = self.generate()
        entry = self.feature(ledger)
        self.assertFalse(entry["witness_present"])
        self.assertFalse(entry["implementation_observed"])


class G19StateTest(GateTestCase):
    def test_no_backend_configured_is_not_checked_not_failed(self):
        self.ws.write_concept_spec()
        happy_path_witness_and_rendering(self.ws)
        descriptor = descriptor_with("crates/scheduler")  # no witness_backend
        ledger = self.generate(descriptor)
        entry = self.feature(ledger)
        self.assertTrue(entry["witness_present"])
        self.assertEqual(entry["determinism"], "not-checked")
        self.assertFalse(entry["implementation_observed"])

    def test_a_drifted_producer_fails_determinism(self):
        self.ws.write_concept_spec()
        baseline = run_producer()
        spec = witness_spec(value_hash=baseline["value_hash"])
        self.ws.write_witness(spec)
        self.ws.write_rendering()
        drifted_descriptor = descriptor_with(
            "crates/scheduler", backend_command=f"{PRODUCER_BACKEND} --ready 5"
        )
        ledger = self.generate(drifted_descriptor)
        entry = self.feature(ledger)
        self.assertTrue(entry["witness_present"])
        self.assertEqual(entry["determinism"], "fail")
        self.assertFalse(entry["implementation_observed"])

    def test_a_stale_identity_mismatched_result_is_not_trusted(self):
        # Requirement #2's own scenario: a regenerated result must never
        # be trusted merely because its witness_id matches. Reuses
        # gate_g19.identity_mismatches (imported transitively through
        # gate_g19_workspace, not re-derived) via an injected backend
        # that ignores the fixture_id it was actually asked to
        # regenerate for -- a plain hash mismatch alone (the drifted-
        # producer test above) cannot distinguish this from an ordinary
        # determinism failure; only a genuinely mismatched identity does.
        self.ws.write_concept_spec()
        baseline = run_producer()
        spec = witness_spec(value_hash=baseline["value_hash"])
        self.ws.write_witness(spec)
        self.ws.write_rendering()

        stale = run_producer(fixture_id="FX-SOME-OTHER-FIXTURE")

        def misbehaving_runner(argv, **kwargs):
            class Completed:
                returncode = 0
                stdout = json.dumps(stale)
                stderr = ""
            return Completed()

        ledger = gfl.generate_ledger(self.root, self.descriptor, runner=misbehaving_runner)
        entry = self.feature(ledger)
        self.assertTrue(entry["witness_present"])
        self.assertEqual(entry["determinism"], "fail")
        self.assertFalse(entry["implementation_observed"])


class G20StateTest(GateTestCase):
    def test_a_constant_result_declared_must_vary_warns_degeneracy(self):
        # G20's own check_must_vary reads the STORED canonical result
        # from disk (load_results_by_witness), independent of G19's
        # live regeneration dispatch -- both must be satisfied here:
        # the stored result for the degeneracy check, and a matching
        # witness_backend so G19's own determinism check passes too,
        # isolating degeneracy as the only failing column.
        self.ws.write_concept_spec()
        constant_result = run_producer("--constant")
        spec = witness_spec(value_hash=constant_result["value_hash"])
        self.ws.write_witness(spec)
        self.ws.write_rendering()
        self.ws.write_result(constant_result)
        constant_backend = descriptor_with(
            "crates/scheduler", backend_command=f"{PRODUCER_BACKEND} --constant"
        )
        ledger = self.generate(constant_backend)
        entry = self.feature(ledger)
        self.assertEqual(entry["determinism"], "pass")
        self.assertEqual(entry["degeneracy"], "warn")
        self.assertFalse(entry["implementation_observed"])


class ClosureKindTest(GateTestCase):
    def test_closure_kind_is_reported_independently_of_witness_state(self):
        # The whole point of the two-column discipline applied to a
        # THIRD column: closure_kind must be visible even when the
        # witness itself is not present at all.
        self.ws.write_concept_spec(cluster="scheduling")
        self.ws.write_closure_profile("scheduling", closure_kind="bounded")
        ledger = self.generate()
        entry = self.feature(ledger)
        self.assertFalse(entry["witness_present"])
        self.assertEqual(entry["closure_kind"], "bounded")

    def test_no_closure_profile_is_reported_as_n_a(self):
        self.ws.write_concept_spec(cluster="scheduling")
        ledger = self.generate()
        self.assertEqual(self.feature(ledger)["closure_kind"], "n/a")

    def test_a_fully_observed_feature_can_still_have_no_closure_profile(self):
        self.ws.write_concept_spec()
        happy_path_witness_and_rendering(self.ws)
        ledger = self.generate()
        entry = self.feature(ledger)
        self.assertTrue(entry["implementation_observed"])
        self.assertEqual(entry["closure_kind"], "n/a")


class AssuranceStatusTest(GateTestCase):
    def test_resolve_assurance_status_is_always_unsupported_today(self):
        # No mechanical link between a witnessed query and a pipeline
        # obligation exists anywhere in this codebase -- see
        # generate_feature_ledger.py's own module docstring. Documents
        # the CURRENT, honest behavior directly.
        for concept, query in [("TaskQueue", "load_factor"), ("Scheduler", "dispatch"), ("X", "y")]:
            with self.subTest(concept=concept, query=query):
                self.assertEqual(gfl.resolve_assurance_status(concept, query), "unsupported")

    def test_schema_accepts_every_declared_assurance_status_value(self):
        # tested/bounded/deductive are reserved for a future linking
        # convention -- the schema must already accept them even though
        # this generator never currently produces them, so a future
        # implementation change is a behavior change, not a schema one.
        validator = gfl.load_validator()
        for status in ("unsupported", "tested", "bounded", "deductive"):
            with self.subTest(status=status):
                ledger = {
                    "schema_version": "1.0",
                    "generated_from": {
                        "concept_specs_hash": "sha256:" + "a" * 64,
                        "witness_specs_hash": "sha256:" + "a" * 64,
                        "assurance_results_hash": "sha256:" + "a" * 64,
                    },
                    "features": [{
                        "feature": "TaskQueue.load_factor",
                        "witness_required": True,
                        "witness_present": False,
                        "implementation_observed": False,
                        "determinism": "not-checked",
                        "degeneracy": "not-checked",
                        "assurance_status": status,
                        "owning_cluster": "scheduling",
                        "closure_kind": "n/a",
                    }],
                }
                self.assertEqual(list(validator.iter_errors(ledger)), [])

    def test_assurance_results_hash_reflects_a_genuinely_valid_report(self):
        self.ws.write_concept_spec()
        self.ws.write_manifest_and_report()
        without = self.generate()["generated_from"]["assurance_results_hash"]
        self.ws.write_manifest_and_report(obligation_records=[
            {
                "obligation_id": "TaskQueue.C001",
                "record": {
                    "schema_version": "1.0",
                    "claim": {"kind": "callee-precondition-established", "result": "pass"},
                    "evidence": {
                        "kind": "creusot-deductive-check", "verifier": "creusot", "harness": "h",
                        "scope": {"input_domain": "all"},
                    },
                    "trust": {"assumptions": []},
                    "support": {"status": "supported"},
                    "config": {
                        "toolchain": "nightly-2026-05-01", "target": "x86_64-unknown-linux-gnu",
                        "features": ["default"],
                    },
                },
            },
        ])
        with_record = self.generate()["generated_from"]["assurance_results_hash"]
        self.assertNotEqual(without, with_record)


class AmbiguityAndConflictTest(GateTestCase):
    def test_an_ambiguous_declaration_across_two_concept_specs_is_invisible(self):
        self.ws.write_concept_spec()
        self.ws.write_concept_spec(filename="task_queue_dup.json")
        ledger = self.generate()
        # collect_declared_features excludes the ambiguous declaration
        # outright -- G18's own established boundary, reused here.
        self.assertEqual(ledger["features"], [])

    def test_an_ambiguous_witness_id_across_crates_leaves_the_feature_unobserved(self):
        self.ws.write_concept_spec()
        baseline = run_producer()
        spec = witness_spec(value_hash=baseline["value_hash"])
        self.ws.write_witness(spec)
        self.ws.write_rendering()
        other = Workspace(self.root, crate_dir="crates/other")
        other.write_witness(spec)
        descriptor = descriptor_with("crates/scheduler", "crates/other", backend_command=PRODUCER_BACKEND)
        ledger = self.generate(descriptor)
        entry = self.feature(ledger)
        self.assertFalse(entry["implementation_observed"])


class DeterminismAndHashingTest(GateTestCase):
    def test_repeated_generation_over_the_same_inputs_is_byte_identical(self):
        self.ws.write_concept_spec()
        happy_path_witness_and_rendering(self.ws)
        first = json.dumps(self.generate(), sort_keys=True)
        second = json.dumps(self.generate(), sort_keys=True)
        self.assertEqual(first, second)

    def test_feature_ordering_is_deterministic_regardless_of_discovery_order(self):
        self.ws.write_concept_spec(extra_queries=(
            {"english": "How deep is the queue right now?", "rust_sig": "fn depth(&self) -> usize", "pure": True,
             "witness_required": True},
        ))
        ledger = self.generate()
        names = [f["feature"] for f in ledger["features"]]
        self.assertEqual(names, sorted(names))

    def test_changing_a_witness_spec_changes_the_witness_specs_hash(self):
        self.ws.write_concept_spec()
        happy_path_witness_and_rendering(self.ws)
        before = self.generate()["generated_from"]["witness_specs_hash"]
        spec = witness_spec(value_hash=run_producer()["value_hash"])
        spec["fixture"]["description"] = "a materially different description"
        self.ws.write_witness(spec)
        after = self.generate()["generated_from"]["witness_specs_hash"]
        self.assertNotEqual(before, after)

    def test_changing_a_concept_spec_changes_the_concept_specs_hash(self):
        self.ws.write_concept_spec(cluster="scheduling")
        before = self.generate()["generated_from"]["concept_specs_hash"]
        self.ws.write_concept_spec(cluster="numeric-kernel")
        after = self.generate()["generated_from"]["concept_specs_hash"]
        self.assertNotEqual(before, after)


class WriteLedgerTest(GateTestCase):
    def test_write_ledger_creates_the_file_at_the_canonical_path(self):
        self.ws.write_concept_spec()
        happy_path_witness_and_rendering(self.ws)
        destination = gfl.write_ledger(self.root, self.descriptor)
        self.assertEqual(destination, self.root / "ci" / "results" / "feature_ledger.json")
        self.assertTrue(destination.is_file())

    def test_atomic_write_failure_leaves_the_previous_ledger_unchanged_and_no_temp_file(self):
        self.ws.write_concept_spec()
        happy_path_witness_and_rendering(self.ws)
        first = gfl.write_ledger(self.root, self.descriptor)
        original = first.read_bytes()

        with patch("atomic_write.os.fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                gfl.write_ledger(self.root, self.descriptor)

        self.assertEqual(first.read_bytes(), original)
        leftovers = list(first.parent.glob(".feature_ledger.json.*.tmp"))
        self.assertEqual(leftovers, [])

    def test_a_schema_invalid_generated_ledger_is_never_written(self):
        self.ws.write_concept_spec()
        happy_path_witness_and_rendering(self.ws)
        with patch.object(gfl, "generate_ledger", return_value={"not": "a valid ledger"}):
            with self.assertRaises(gfl.GenerationError):
                gfl.write_ledger(self.root, self.descriptor)
        self.assertFalse((self.root / "ci" / "results" / "feature_ledger.json").exists())


class CliTest(GateTestCase):
    def _run(self, *args) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = gfl.main(list(args))
        return code, buffer.getvalue()

    def test_cli_end_to_end_reports_the_feature_count(self):
        self.ws.write_concept_spec()
        happy_path_witness_and_rendering(self.ws)
        (self.root / "project-descriptor.json").write_text(json.dumps(self.descriptor))
        code, printed = self._run(str(self.root))
        self.assertEqual(code, gfl.EXIT_OK)
        self.assertIn("1 declared feature(s)", printed)

    def test_cli_zero_features_reports_honestly(self):
        (self.root / "project-descriptor.json").write_text(json.dumps(self.descriptor))
        code, printed = self._run(str(self.root))
        self.assertEqual(code, gfl.EXIT_OK)
        self.assertIn("0 declared features", printed)
        self.assertIn("nothing to check", printed)

    def test_cli_refuses_a_missing_workspace(self):
        code, _ = self._run(str(self.root / "nope"))
        self.assertEqual(code, gfl.EXIT_INPUT_ERROR)

    def test_cli_refuses_a_missing_descriptor(self):
        code, _ = self._run(str(self.root))
        self.assertEqual(code, gfl.EXIT_INPUT_ERROR)


if __name__ == "__main__":
    unittest.main()
