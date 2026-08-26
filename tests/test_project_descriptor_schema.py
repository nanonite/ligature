"""Regression tests for schemas/project-descriptor.schema.json (plan.md §1.1).

Per plan.md §12 (G1a) and the epic's own hard rule: canonical examples must
pass their schema in CI, and hand-written examples are untrustworthy against
additionalProperties: false unless actually checked.
"""
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schemas" / "project-descriptor.schema.json"
EXAMPLES_DIR = ROOT / "schemas" / "examples"


class ProjectDescriptorSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA_PATH.read_text())
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema)

    def test_greenfield_example_is_valid(self):
        instance = json.loads(
            (EXAMPLES_DIR / "project-descriptor.greenfield.example.json").read_text()
        )
        errors = list(self.validator.iter_errors(instance))
        self.assertEqual(errors, [], [e.message for e in errors])

    def test_port_example_is_valid(self):
        instance = json.loads(
            (EXAMPLES_DIR / "project-descriptor.port.example.json").read_text()
        )
        errors = list(self.validator.iter_errors(instance))
        self.assertEqual(errors, [], [e.message for e in errors])

    def test_rejects_unknown_top_level_field(self):
        instance = json.loads(
            (EXAMPLES_DIR / "project-descriptor.greenfield.example.json").read_text()
        )
        instance["unexpected_field"] = "should be rejected"
        errors = list(self.validator.iter_errors(instance))
        self.assertTrue(errors, "schema accepted an undeclared top-level field")

    def test_rejects_missing_required_field(self):
        instance = json.loads(
            (EXAMPLES_DIR / "project-descriptor.greenfield.example.json").read_text()
        )
        del instance["write_set"]
        errors = list(self.validator.iter_errors(instance))
        self.assertTrue(errors, "schema accepted a missing required field")

    def test_port_mode_requires_port_source(self):
        instance = json.loads(
            (EXAMPLES_DIR / "project-descriptor.greenfield.example.json").read_text()
        )
        instance["mode"] = "port"
        errors = list(self.validator.iter_errors(instance))
        self.assertTrue(
            errors, "mode: port validated without port_source (if/then not enforced)"
        )

    def test_greenfield_mode_does_not_require_port_source(self):
        instance = json.loads(
            (EXAMPLES_DIR / "project-descriptor.greenfield.example.json").read_text()
        )
        self.assertEqual(instance["mode"], "greenfield")
        self.assertNotIn("port_source", instance)
        errors = list(self.validator.iter_errors(instance))
        self.assertEqual(errors, [])

    def test_unknown_verifier_is_rejected(self):
        instance = json.loads(
            (EXAMPLES_DIR / "project-descriptor.greenfield.example.json").read_text()
        )
        instance["verifier_policy"]["default"] = "not-a-real-verifier"
        errors = list(self.validator.iter_errors(instance))
        self.assertTrue(errors, "schema accepted an unknown verifier name")


if __name__ == "__main__":
    unittest.main()
