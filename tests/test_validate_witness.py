import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from validate_witness import (  # noqa: E402
    count_discovered,
    count_results,
    load_draft_validator,
    load_result_validator,
    load_results_by_witness,
    load_validator,
    main,
    resolve_query,
    snake_case,
    validate,
    validate_crate,
    validate_data,
    validate_draft_data,
    validate_result_data,
    witness_dir_for,
)
from witness_result import build_result, encode_grid, witness_result_dir_for  # noqa: E402

PRODUCER = ROOT / "tests" / "fixtures" / "witnesses" / "stand_in_producer.py"
WITNESS_PATH = Path("crates/scheduler/specs/_witnesses/task_queue.load_factor.json")


def canonical_result() -> dict:
    return build_result(
        witness_id="W-TQ-LOAD-FACTOR",
        concept="TaskQueue",
        query="load_factor",
        fixture_id="FX-QUEUE-BOTTOM-ROW",
        seed=0,
        renderer_actual="scalar_field_svg",
        result=encode_grid(1, 4, [(0, 0, 0.125), (0, 1, 0.25), (0, 2, 0.375), (0, 3, 0.5)]),
    )


def witness_spec(value_hash: str | None = None) -> dict:
    return {
        "schema_version": "1.0",
        "witness_id": "W-TQ-LOAD-FACTOR",
        "concept": "TaskQueue",
        "query": "load_factor",
        "fixture": {
            "fixture_id": "FX-QUEUE-BOTTOM-ROW",
            "seed": 0,
            "description": "8-slot queue, 3 ready tasks front-loaded",
        },
        "renderer": "scalar_field_svg",
        "expectation": {
            "renderer": "scalar_field_svg",
            "coverage_region": "bottom-row",
            "value_distribution": "must-vary",
            "fixture_family": "FX-BOTTOM-ROW",
        },
        "determinism": {
            "value_hash": value_hash or canonical_result()["value_hash"],
            "claim": "byte-identical-across-runs",
            "platforms": ["x86_64-unknown-linux-gnu"],
        },
        "output": {
            "path": "docs/witnesses/task_queue.load_factor.svg",
            "render_hash": "sha256:" + "b" * 64,
            "renderer_actual": "scalar_field_svg",
        },
        "review": {"reviewer": "alice", "reviewed_at": "2026-09-04"},
    }


def concept_spec(pure: bool = True) -> dict:
    return {
        "concept": "TaskQueue",
        "queries": [
            {
                "english": "What fraction of the queue's slots are currently occupied?",
                "rust_sig": "fn load_factor(&self) -> f64",
                "pure": pure,
            }
        ],
        "constraints": [
            {
                "id": "C003",
                "english": "pop_ready returns None only when no task is ready",
                "logic": "true",
                "kind": "postcondition",
                "applies_to": ["pop_ready"],
            }
        ],
    }


def findings_for(data: dict, path: Path = WITNESS_PATH, search_root: Path | None = None) -> list[str]:
    """Error-severity findings only. With no search root the G2 check
    degrades to a visible info note (never a silent pass) -- asserted on
    its own below, and filtered here so a schema assertion is about the
    schema."""
    return [
        str(f)
        for f in validate_data(path, data, load_validator(), search_root)
        if f.severity == "error"
    ]


class SnakeCaseTest(unittest.TestCase):
    """plan.md §2: copy concept-to-code's rule, do not re-derive it."""

    def test_it_matches_concept_to_codes_own_implementation(self):
        sys.path.insert(0, str(ROOT / "vendor" / "concept-to-code"))
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_c2c_emit_stubs", ROOT / "vendor" / "concept-to-code" / "emit_stubs.py"
        )
        module = importlib.util.module_from_spec(spec)
        # dataclasses resolves __module__ through sys.modules while the
        # class body executes, so a module loaded by path must be
        # registered before exec_module, not after.
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        for name in ("TaskQueue", "HTTPClient", "Scheduler", "MCMCChain", "A", "PartitionedTreeLikelihood"):
            with self.subTest(concept=name):
                self.assertEqual(snake_case(name), module.snake_case(name))

    def test_the_acronym_case_the_plan_warns_about(self):
        self.assertEqual(snake_case("HTTPClient"), "httpclient")
        self.assertNotEqual(snake_case("HTTPClient"), "h_t_t_p_client")


class SpecSchemaTest(unittest.TestCase):
    def test_the_worked_example_is_valid(self):
        self.assertEqual(findings_for(witness_spec()), [])

    def test_every_top_level_field_is_required(self):
        for field in (
            "witness_id", "concept", "query", "fixture", "renderer",
            "expectation", "determinism", "output", "review",
        ):
            with self.subTest(field=field):
                data = witness_spec()
                del data[field]
                self.assertTrue(findings_for(data))

    def test_the_expectation_block_cannot_be_weakened_away(self):
        for field in ("renderer", "coverage_region", "value_distribution", "fixture_family"):
            with self.subTest(field=field):
                data = witness_spec()
                del data["expectation"][field]
                self.assertTrue(findings_for(data))

    def test_coverage_region_vocabulary_is_plans_own(self):
        for region in ("full-grid", "bottom-row", "corridor", "single-cell", "project-defined"):
            data = witness_spec()
            data["expectation"]["coverage_region"] = region
            with self.subTest(region=region):
                self.assertEqual(findings_for(data), [])
        data = witness_spec()
        data["expectation"]["coverage_region"] = "wherever"
        self.assertTrue(findings_for(data))

    def test_value_distribution_vocabulary_is_closed(self):
        data = witness_spec()
        data["expectation"]["value_distribution"] = "probably-varies"
        self.assertTrue(findings_for(data))

    def test_a_witness_carries_no_assurance_vocabulary(self):
        # §16's rule: evidence, never assurance. There must be no field
        # here that an assurance computation could read.
        schema = json.loads((ROOT / "docs" / "witness-spec-schema.json").read_text())
        text = json.dumps(schema["properties"])
        for forbidden in ("accepted_evidence_kinds", "assurance_status", "closure_kind", "required_claims"):
            self.assertNotIn(forbidden, text)

    def test_a_render_hash_is_not_where_the_determinism_contract_lives(self):
        schema = json.loads((ROOT / "docs" / "witness-spec-schema.json").read_text())
        self.assertIn("value_hash", schema["properties"]["determinism"]["properties"])
        self.assertNotIn("render_hash", schema["properties"]["determinism"]["properties"])
        self.assertIn("render_hash", schema["properties"]["output"]["properties"])


class NamingTest(unittest.TestCase):
    def test_the_stem_is_snake_concept_dot_query(self):
        findings = findings_for(witness_spec(), Path("crates/c/specs/_witnesses/taskqueue.load_factor.json"))
        self.assertTrue(any("does not match" in f for f in findings))

    def test_nested_placement_is_rejected(self):
        findings = findings_for(
            witness_spec(), Path("crates/c/specs/_witnesses/nested/task_queue.load_factor.json")
        )
        self.assertTrue(any("not flat" in f for f in findings))

    def test_the_output_path_is_this_witnesss_own(self):
        data = witness_spec()
        data["output"]["path"] = "docs/witnesses/other.thing.svg"
        self.assertTrue(any("is not this witness's own generated location" in f for f in findings_for(data)))


class RendererConsistencyTest(unittest.TestCase):
    def test_a_degraded_renderer_is_caught(self):
        data = witness_spec()
        data["output"]["renderer_actual"] = "text_table_strip"
        findings = findings_for(data)
        self.assertTrue(any("silently substitute a degraded rendering" in f for f in findings))

    def test_expectation_renderer_must_match_the_declared_renderer(self):
        data = witness_spec()
        data["expectation"]["renderer"] = "other_svg"
        findings = findings_for(data)
        self.assertTrue(any("does not match renderer" in f for f in findings))


class QueryResolutionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.search_root = Path(self._tmp.name)
        (self.search_root / "scheduler" / "specs").mkdir(parents=True)
        self.spec_path = self.search_root / "scheduler" / "specs" / "task_queue.json"
        self.spec_path.write_text(json.dumps(concept_spec()))

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_pure_query_resolves(self):
        self.assertEqual(findings_for(witness_spec(), search_root=self.search_root), [])
        status, candidate = resolve_query("TaskQueue", "load_factor", self.search_root)
        self.assertEqual(status, "resolved")
        self.assertTrue(candidate["pure"])

    def test_a_non_pure_query_is_refused(self):
        self.spec_path.write_text(json.dumps(concept_spec(pure=False)))
        findings = findings_for(witness_spec(), search_root=self.search_root)
        self.assertTrue(any("observes a side effect and calls it a value" in f for f in findings))

    def test_an_undeclared_query_is_dangling(self):
        data = witness_spec()
        data["query"] = "depth"
        data["output"]["path"] = "docs/witnesses/task_queue.depth.svg"
        findings = findings_for(
            data, Path("crates/scheduler/specs/_witnesses/task_queue.depth.json"), self.search_root
        )
        self.assertTrue(any("declares no query named 'depth'" in f for f in findings))

    def test_a_missing_concept_spec_is_refused(self):
        self.spec_path.unlink()
        findings = findings_for(witness_spec(), search_root=self.search_root)
        self.assertTrue(any("no concept spec under the search root" in f for f in findings))

    def test_two_specs_for_one_concept_are_ambiguous(self):
        (self.search_root / "other" / "specs").mkdir(parents=True)
        (self.search_root / "other" / "specs" / "task_queue.json").write_text(json.dumps(concept_spec()))
        findings = findings_for(witness_spec(), search_root=self.search_root)
        self.assertTrue(any("more than one spec" in f for f in findings))

    def test_the_witness_itself_is_not_mistaken_for_a_concept_spec(self):
        # A witness carries a top-level `concept` and lives under the
        # search root; an unfiltered scan reports its own concept as
        # ambiguous. Underscore-prefixed artifact directories are skipped.
        witness_dir = self.search_root / "scheduler" / "specs" / "_witnesses"
        witness_dir.mkdir()
        (witness_dir / "task_queue.load_factor.json").write_text(json.dumps(witness_spec()))
        self.assertEqual(resolve_query("TaskQueue", "load_factor", self.search_root)[0], "resolved")

    def test_a_document_that_merely_names_a_concept_is_not_a_spec(self):
        (self.search_root / "note.json").write_text(json.dumps({"concept": "TaskQueue"}))
        self.assertEqual(resolve_query("TaskQueue", "load_factor", self.search_root)[0], "resolved")

    def test_without_a_search_root_the_check_degrades_to_a_visible_note(self):
        findings = [str(f) for f in validate_data(WITNESS_PATH, witness_spec(), load_validator(), None)]
        self.assertTrue(any("was not checked" in f and "info" in f for f in findings))


class DraftTest(unittest.TestCase):
    def test_a_draft_may_not_carry_its_own_review(self):
        findings = validate_draft_data(WITNESS_PATH, witness_spec(), load_draft_validator())
        self.assertTrue(any("must not include its own `review` block" in str(f) for f in findings))

    def test_a_review_less_draft_is_otherwise_valid(self):
        data = witness_spec()
        del data["review"]
        self.assertEqual([str(f) for f in validate_draft_data(WITNESS_PATH, data, load_draft_validator())], [])


class ResultValidationTest(unittest.TestCase):
    def result_findings(self, data: dict, stem: str = "W-TQ-LOAD-FACTOR") -> list[str]:
        path = Path(f"ci/results/witnesses/{stem}.json")
        return [str(f) for f in validate_result_data(path, data, load_result_validator())]

    def test_a_produced_result_validates(self):
        self.assertEqual(self.result_findings(canonical_result()), [])

    def test_a_tampered_value_is_caught_by_both_recomputations(self):
        data = canonical_result()
        data["result"]["cells"][0]["value"] = "0.9"
        findings = self.result_findings(data)
        self.assertTrue(any("value_domain" in f for f in findings))
        self.assertTrue(any("is not the hash of this result" in f for f in findings))

    def test_a_tampered_hash_alone_is_caught(self):
        data = canonical_result()
        data["value_hash"] = "sha256:" + "0" * 64
        self.assertTrue(any("is not the hash of this result" in f for f in self.result_findings(data)))

    def test_a_tampered_domain_alone_is_caught(self):
        data = canonical_result()
        data["value_domain"]["distinct_values"] = 1
        self.assertTrue(any("value_domain" in f for f in self.result_findings(data)))

    def test_a_json_number_for_a_measured_value_is_refused(self):
        data = canonical_result()
        data["result"]["cells"][0]["value"] = 0.125
        self.assertTrue(self.result_findings(data))

    def test_an_unsorted_grid_is_refused(self):
        data = canonical_result()
        data["result"]["cells"].reverse()
        findings = self.result_findings(data)
        self.assertTrue(any("not sorted" in f for f in findings))

    def test_the_filename_must_be_the_witness_id(self):
        findings = self.result_findings(canonical_result(), stem="W-OTHER")
        self.assertTrue(any("does not match witness_id" in f for f in findings))

    def test_a_non_canonical_decimal_spelling_is_refused(self):
        data = canonical_result()
        data["result"]["cells"][0]["value"] = "0.1250"
        self.assertTrue(self.result_findings(data))

    def test_a_decimal_shaped_but_non_canonical_spelling_is_refused(self):
        # External review, high severity: "1e1" passes the pattern-only
        # shape check that used to gate this but is not what
        # canonical_number() would itself produce for the value ten.
        data = canonical_result()
        data["result"]["cells"][0]["value"] = "1e1"
        self.assertTrue(self.result_findings(data))

    def test_distinct_values_is_not_overcounted_by_spelling(self):
        # A constant result with one cell spelled "10" and another "1e1"
        # is a single distinct value; the per-value canonical-encoding
        # check catches the second spelling outright, which is the
        # stronger fix -- it never reaches distinct-value counting with
        # two spellings of one number in the first place.
        data = canonical_result()
        for cell in data["result"]["cells"]:
            cell["value"] = "10"
        data["result"]["cells"][-1]["value"] = "1e1"
        findings = self.result_findings(data)
        self.assertTrue(any("not canonically encoded" in f for f in findings))

    def test_a_hashed_fields_subset_is_refused(self):
        # External review, medium severity, reproduced exactly this way:
        # a document declaring hashed_fields: ["result"] passed
        # validation even though value_hash was, in truth, computed over
        # all six fields.
        data = canonical_result()
        data["canonicalization"]["hashed_fields"] = ["result"]
        findings = self.result_findings(data)
        self.assertTrue(any("does not match what rule" in f for f in findings))

    def test_a_reordered_hashed_fields_is_accepted(self):
        data = canonical_result()
        data["canonicalization"]["hashed_fields"] = list(reversed(data["canonicalization"]["hashed_fields"]))
        self.assertEqual(self.result_findings(data), [])

    def test_an_out_of_bounds_grid_cell_is_refused(self):
        # External review, medium severity, reproduced exactly this way:
        # a schema-valid 1x1 result containing cell (9, 0) passed with
        # zero findings -- only uniqueness/sortedness were checked, never
        # bounds against the grid's own declared rows/columns.
        data = build_result(
            witness_id="W-TQ-LOAD-FACTOR", concept="TaskQueue", query="load_factor",
            fixture_id="FX-QUEUE-BOTTOM-ROW", seed=0, renderer_actual="scalar_field_svg",
            result={"kind": "grid", "rows": 1, "columns": 1, "cells": [{"row": 9, "column": 0, "value": "1"}]},
        )
        findings = self.result_findings(data)
        self.assertTrue(any("outside that range" in f for f in findings))

    def test_a_grid_cell_within_bounds_is_accepted(self):
        data = build_result(
            witness_id="W-TQ-LOAD-FACTOR", concept="TaskQueue", query="load_factor",
            fixture_id="FX-QUEUE-BOTTOM-ROW", seed=0, renderer_actual="scalar_field_svg",
            result={"kind": "grid", "rows": 2, "columns": 2, "cells": [{"row": 1, "column": 1, "value": "1"}]},
        )
        self.assertEqual(self.result_findings(data), [])


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        self.crate = {"crate_dir": "crates/scheduler", "specs_search_root": "crates"}
        self.crate_root = self.workspace / "crates" / "scheduler"
        (self.crate_root / "specs").mkdir(parents=True)
        (self.crate_root / "specs" / "task_queue.json").write_text(json.dumps(concept_spec()))
        self.witness_dir = witness_dir_for(self.crate, self.workspace)
        self.witness_dir.mkdir(parents=True)
        self.write_witness(witness_spec())
        self.write_result(canonical_result())

    def tearDown(self):
        self._tmp.cleanup()

    def write_witness(self, data: dict, stem: str | None = None) -> Path:
        stem = stem or f"{snake_case(data['concept'])}.{data['query']}"
        path = self.witness_dir / f"{stem}.json"
        path.write_text(json.dumps(data, indent=2))
        return path

    def write_result(self, data: dict) -> Path:
        directory = witness_result_dir_for(self.workspace)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{data['witness_id']}.json"
        path.write_text(json.dumps(data, indent=2))
        return path

    def crate_findings(self) -> list[str]:
        return [
            str(f)
            for f in validate_crate(
                self.crate_root, self.witness_dir, self.workspace / self.crate["specs_search_root"]
            )
        ]

    def test_the_fixture_crate_passes(self):
        self.assertEqual(self.crate_findings(), [])
        self.assertEqual(count_discovered(self.crate_root), 1)
        self.assertEqual(count_results(self.workspace), 1)

    def test_a_duplicate_witness_id_is_rejected(self):
        second = witness_spec()
        second["query"] = "depth"
        second["output"]["path"] = "docs/witnesses/task_queue.depth.svg"
        self.write_witness(second)
        spec = concept_spec()
        spec["queries"].append(
            {"english": "How deep is the queue right now?", "rust_sig": "fn depth(&self) -> usize", "pure": True}
        )
        (self.crate_root / "specs" / "task_queue.json").write_text(json.dumps(spec))
        self.assertTrue(any("is already used by" in f for f in self.crate_findings()))

    def test_a_family_whose_members_disagree_about_its_region_is_not_this_validators_job(self):
        # External review, chainlink #31, medium severity: validate_crate()
        # used to call check_family_consistency() itself, hard-failing
        # this defect as G1b/error right here at ordinary Stage 4
        # validation -- before gate_g20.py's own G20 could ever report
        # the identical defect as its intended Stage 4.5 warning. One
        # defect must not carry two disagreeing dispositions. G20 is now
        # the only caller (see tests/test_gate_g20.py's own coverage of
        # this exact scenario, at WARN severity, workspace-wide).
        second = witness_spec()
        second["witness_id"] = "W-TQ-DEPTH"
        second["query"] = "depth"
        second["output"]["path"] = "docs/witnesses/task_queue.depth.svg"
        second["expectation"]["coverage_region"] = "full-grid"
        self.write_witness(second)
        spec = concept_spec()
        spec["queries"].append(
            {"english": "How deep is the queue right now?", "rust_sig": "fn depth(&self) -> usize", "pure": True}
        )
        (self.crate_root / "specs" / "task_queue.json").write_text(json.dumps(spec))
        findings = self.crate_findings()
        self.assertFalse(any("disagree about the region" in f for f in findings))

    def test_a_mislocated_witness_is_found_and_rejected_by_location(self):
        stray = self.crate_root / "not_specs" / "_witnesses"
        stray.mkdir(parents=True)
        (stray / "task_queue.load_factor.json").write_text(json.dumps(witness_spec()))
        self.assertTrue(
            any("not directly under the canonical directory" in f for f in self.crate_findings())
        )

    def test_load_results_by_witness_omits_invalid_results(self):
        broken = canonical_result()
        broken["value_hash"] = "sha256:" + "0" * 64
        self.write_result(broken)
        self.assertEqual(load_results_by_witness(self.workspace), {})

    def test_load_results_by_witness_indexes_valid_ones(self):
        self.assertEqual(sorted(load_results_by_witness(self.workspace)), ["W-TQ-LOAD-FACTOR"])

    def test_the_standalone_cli_passes_and_counts(self):
        self.assertEqual(main([str(self.crate_root), "--specs-search-root", str(self.workspace / "crates")]), 0)

    def test_the_standalone_cli_can_include_results(self):
        self.assertEqual(main([str(self.workspace), "--results"]), 0)

    def test_an_empty_scan_reports_honestly(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(main([empty]), 0)

    def test_the_standalone_cli_fails_on_a_bad_witness(self):
        data = witness_spec()
        data["output"]["renderer_actual"] = "text_table_strip"
        self.write_witness(data)
        self.assertEqual(main([str(self.crate_root)]), 1)

    def test_the_stand_in_producer_output_validates_as_a_result(self):
        completed = subprocess.run(
            [sys.executable, str(PRODUCER)], capture_output=True, text=True, check=True
        )
        document = json.loads(completed.stdout)
        self.write_result(document)
        findings = [
            str(f)
            for f in validate_result_data(
                witness_result_dir_for(self.workspace) / f"{document['witness_id']}.json",
                document,
                load_result_validator(),
            )
        ]
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
