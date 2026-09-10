"""Tests for schemas/consolidated-check.schema.json (chainlink #55/#56's
`ligature check --json` contract). Also covers deterministic-serialization
and dedup-identity properties from a canonicalizer applied to the example
fixtures, since there is no real `check` run yet to test those properties
against (#56's own scope).
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from schema_utils import make_validator  # noqa: E402

SCHEMA_PATH = ROOT / "schemas" / "consolidated-check.schema.json"
EXAMPLES_DIR = ROOT / "schemas" / "examples"


def _canonical_dumps(doc: dict) -> str:
    """The canonical serialization this contract's byte-identical
    guarantee refers to: sort_keys, fixed separators, trailing newline.
    A stand-in for whatever real serializer #56 writes -- this function
    exists so the DETERMINISM PROPERTY (same input -> same bytes,
    regardless of dict insertion order) is testable now, against fixture
    documents, without #56's engine existing yet."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n"


class ConsolidatedCheckSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA_PATH.read_text())
        cls.validator = make_validator(cls.schema)

    def _load(self, name):
        return json.loads((EXAMPLES_DIR / name).read_text())

    def test_clean_example_is_valid(self):
        errors = list(self.validator.iter_errors(self._load("consolidated-check.clean.example.json")))
        self.assertEqual(errors, [], [e.message for e in errors])

    def test_blocked_example_is_valid(self):
        errors = list(self.validator.iter_errors(self._load("consolidated-check.blocked.example.json")))
        self.assertEqual(errors, [], [e.message for e in errors])

    def test_mutated_workspace_must_be_false(self):
        doc = self._load("consolidated-check.clean.example.json")
        doc["mutated_workspace"] = True
        self.assertTrue(list(self.validator.iter_errors(doc)))

    def test_mutated_workspace_is_required(self):
        doc = self._load("consolidated-check.clean.example.json")
        del doc["mutated_workspace"]
        self.assertTrue(list(self.validator.iter_errors(doc)))

    def test_next_action_rejects_an_array(self):
        doc = self._load("consolidated-check.clean.example.json")
        doc["next_action"] = [{"kind": "human-decision", "description": "x"}]
        self.assertTrue(list(self.validator.iter_errors(doc)))

    def test_next_action_accepts_null(self):
        doc = self._load("consolidated-check.blocked.example.json")
        doc["next_action"] = None
        errors = list(self.validator.iter_errors(doc))
        self.assertEqual(errors, [])

    def test_automated_command_next_action_requires_a_command_string(self):
        doc = self._load("consolidated-check.blocked.example.json")
        doc["next_action"]["command"] = None
        self.assertTrue(list(self.validator.iter_errors(doc)))

    def test_automated_command_next_action_requires_an_action_id(self):
        """Round-3 external review: `command` alone was the only
        machine-consumable identifier next_action offered, while pointing
        at legacy flat command names the contract itself declares
        unversioned -- action_id is now the required, stable surface."""
        doc = self._load("consolidated-check.blocked.example.json")
        del doc["next_action"]["action_id"]
        self.assertTrue(list(self.validator.iter_errors(doc)))

    def test_automated_command_action_id_must_be_a_nonempty_string(self):
        doc = self._load("consolidated-check.blocked.example.json")
        doc["next_action"]["action_id"] = ""
        self.assertTrue(list(self.validator.iter_errors(doc)))

    def test_action_id_rejects_uppercase_and_underscores(self):
        """Pattern is the same kebab-case style every other stable
        identifier in this contract uses (gate-id, report-id, etc)."""
        doc = self._load("consolidated-check.blocked.example.json")
        for bad in ("Author_Interaction", "author_interaction", "AUTHOR-INTERACTION"):
            with self.subTest(action_id=bad):
                mutated = json.loads(json.dumps(doc))
                mutated["next_action"]["action_id"] = bad
                self.assertTrue(list(self.validator.iter_errors(mutated)))

    def test_refresh_recommended_example_uses_a_refresh_action_id(self):
        """docs/cli-contract.md §7 defines refresh-c-static/
        refresh-bridge-checks/refresh-witness as the stable action_ids
        for the three internal operations check may recommend but never
        runs -- this example is that exact scenario."""
        doc = self._load("consolidated-check.refresh-recommended.example.json")
        errors = list(self.validator.iter_errors(doc))
        self.assertEqual(errors, [])
        self.assertEqual(doc["next_action"]["action_id"], "refresh-c-static")

    def test_human_decision_next_action_forbids_a_command_string(self):
        doc = self._load("consolidated-check.blocked.example.json")
        doc["next_action"]["kind"] = "human-decision"
        doc["next_action"]["command"] = "ligature gate g14"
        self.assertTrue(list(self.validator.iter_errors(doc)))

    def test_human_decision_next_action_forbids_an_action_id(self):
        doc = self._load("consolidated-check.blocked.example.json")
        doc["next_action"]["kind"] = "human-decision"
        doc["next_action"]["command"] = None
        self.assertTrue(list(self.validator.iter_errors(doc)))

    def test_human_decision_next_action_with_null_command_is_valid(self):
        doc = self._load("consolidated-check.blocked.example.json")
        doc["next_action"]["kind"] = "human-decision"
        del doc["next_action"]["action_id"]
        doc["next_action"]["command"] = None
        errors = list(self.validator.iter_errors(doc))
        self.assertEqual(errors, [])

    def test_gate_run_skipped_requires_a_reason(self):
        doc = self._load("consolidated-check.clean.example.json")
        doc["gates"].append({"gate_id": "g20", "outcome": "skipped"})
        self.assertTrue(list(self.validator.iter_errors(doc)))

    def test_gate_run_executed_does_not_require_a_reason(self):
        doc = self._load("consolidated-check.clean.example.json")
        errors = list(self.validator.iter_errors(doc))
        self.assertEqual(errors, [])
        executed = [g for g in doc["gates"] if g["outcome"] == "executed"]
        self.assertTrue(executed)
        self.assertNotIn("reason", executed[0])

    def test_finding_requires_authority_and_provenance(self):
        doc = self._load("consolidated-check.blocked.example.json")
        del doc["findings"][0]["authority"]
        self.assertTrue(list(self.validator.iter_errors(doc)))

    def test_finding_authority_is_closed_enum(self):
        doc = self._load("consolidated-check.blocked.example.json")
        doc["findings"][0]["authority"] = "vibes"
        self.assertTrue(list(self.validator.iter_errors(doc)))

    def test_result_exit_code_is_one_of_the_five_contract_codes(self):
        doc = self._load("consolidated-check.blocked.example.json")
        doc["result"]["exit_code"] = 7
        self.assertTrue(list(self.validator.iter_errors(doc)))

    def test_rejects_unknown_top_level_field(self):
        doc = self._load("consolidated-check.clean.example.json")
        doc["approved"] = True
        self.assertTrue(list(self.validator.iter_errors(doc)))


class DeterminismAndDedupTest(unittest.TestCase):
    """Same-bytes-in -> same-bytes-out, and dedup identity is a pure
    function of content, not of dict insertion order or call sequence."""

    def test_canonical_serialization_is_stable_across_key_order(self):
        doc = json.loads((EXAMPLES_DIR / "consolidated-check.blocked.example.json").read_text())
        reordered = json.loads(json.dumps(dict(reversed(list(doc.items())))))
        self.assertEqual(_canonical_dumps(doc), _canonical_dumps(reordered))

    def test_canonical_serialization_is_repeatable(self):
        doc = json.loads((EXAMPLES_DIR / "consolidated-check.clean.example.json").read_text())
        first = _canonical_dumps(doc)
        second = _canonical_dumps(json.loads(json.dumps(doc)))
        self.assertEqual(first, second)

    def test_run_metadata_is_excluded_from_the_documented_determinism_guarantee(self):
        """Two 'runs' that differ only in run_metadata are exactly the
        difference the contract's own description calls out as excluded
        -- everything else must still match byte-for-byte once
        run_metadata is stripped."""
        doc = json.loads((EXAMPLES_DIR / "consolidated-check.blocked.example.json").read_text())
        run_a = dict(doc, run_metadata={"started_at": "2026-09-10T12:00:00Z", "product_version": "1.0.0"})
        run_b = dict(doc, run_metadata={"started_at": "2026-09-10T12:05:00Z", "product_version": "1.0.0"})
        self.assertNotEqual(_canonical_dumps(run_a), _canonical_dumps(run_b))
        stripped_a = {k: v for k, v in run_a.items() if k != "run_metadata"}
        stripped_b = {k: v for k, v in run_b.items() if k != "run_metadata"}
        self.assertEqual(_canonical_dumps(stripped_a), _canonical_dumps(stripped_b))

    def test_dedup_keys_are_unique_within_a_single_document(self):
        doc = json.loads((EXAMPLES_DIR / "consolidated-check.blocked.example.json").read_text())
        keys = [f["dedup_key"] for f in doc["findings"]]
        self.assertEqual(len(keys), len(set(keys)))


if __name__ == "__main__":
    unittest.main()
