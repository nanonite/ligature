"""Schema-validity tests for docs/implementation-inventory.json itself
(chainlink #55) -- separate from tests/test_inventory_drift.py, which
checks the inventory against the live repository. This file checks the
inventory's OWN internal shape/mutation-rejection behavior, the same
pattern tests/test_project_descriptor_schema.py already uses for its
schema.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from schema_utils import make_validator  # noqa: E402

SCHEMA_PATH = ROOT / "schemas" / "implementation-inventory.schema.json"
INVENTORY_PATH = ROOT / "docs" / "implementation-inventory.json"


class ImplementationInventorySchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA_PATH.read_text())
        cls.validator = make_validator(cls.schema)

    def _load(self):
        return json.loads(INVENTORY_PATH.read_text())

    def test_real_inventory_is_valid(self):
        errors = list(self.validator.iter_errors(self._load()))
        self.assertEqual(errors, [], [e.message for e in errors])

    def test_rejects_unknown_disposition(self):
        doc = self._load()
        doc["python_modules"][0]["disposition"] = "somewhat-required"
        self.assertTrue(list(self.validator.iter_errors(doc)))

    def test_rejects_wrong_inventory_version(self):
        doc = self._load()
        doc["inventory_version"] = "2.0"
        self.assertTrue(list(self.validator.iter_errors(doc)))

    def test_rejects_missing_required_top_level_section(self):
        doc = self._load()
        del doc["gates"]
        self.assertTrue(list(self.validator.iter_errors(doc)))

    def test_python_module_path_pattern_rejects_non_scripts_path(self):
        doc = self._load()
        doc["python_modules"][0]["path"] = "somewhere/else.py"
        self.assertTrue(list(self.validator.iter_errors(doc)))

    def test_never_release_entries_require_a_reason(self):
        doc = self._load()
        del doc["never_release"][0]["reason"]
        self.assertTrue(list(self.validator.iter_errors(doc)))

    def test_gate_module_field_documents_non_frozen_names(self):
        """Not a schema-validity assertion -- a content check that the
        gates section actually says module/gate-implementation names
        aren't frozen, per plan.md's own instruction. If this drifts out
        of the description text, the contract silently loses that
        disclaimer."""
        schema_text = SCHEMA_PATH.read_text()
        self.assertIn("Not frozen as public API", schema_text)


if __name__ == "__main__":
    unittest.main()
