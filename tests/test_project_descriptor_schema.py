"""Regression tests for schemas/project-descriptor.schema.json (plan.md §1.1).

Per plan.md §12 (G1a) and the epic's own hard rule: canonical examples must
pass their schema in CI, and hand-written examples are untrustworthy against
additionalProperties: false unless actually checked.
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from project_descriptor import schema_diagnostics  # noqa: E402
from schema_utils import make_validator  # noqa: E402

SCHEMA_PATH = ROOT / "schemas" / "project-descriptor.schema.json"
EXAMPLES_DIR = ROOT / "schemas" / "examples"


class ProjectDescriptorSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA_PATH.read_text())
        cls.validator = make_validator(cls.schema)

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

    def test_empty_reviewer_is_rejected(self):
        """Review finding (round 2, medium severity): reviewer had no
        minLength, so review: {reviewer: "", reviewed_at: ...} validated
        -- an empty string is not a reviewer."""
        instance = json.loads(
            (EXAMPLES_DIR / "project-descriptor.greenfield.example.json").read_text()
        )
        instance["review"]["reviewer"] = ""
        errors = list(self.validator.iter_errors(instance))
        self.assertTrue(errors, "schema accepted an empty reviewer")

    def test_malformed_reviewed_at_is_rejected(self):
        """Review finding (round 2, medium severity): format: date is
        annotation-only unless a format_checker is wired in, which no call
        site did -- reviewed_at: "not-a-date" validated. Now enforced both
        by an actual format checker (schema_utils.make_validator) and a
        structural pattern, so this holds even if a future validator is
        built by hand without the shared helper."""
        instance = json.loads(
            (EXAMPLES_DIR / "project-descriptor.greenfield.example.json").read_text()
        )
        instance["review"]["reviewed_at"] = "not-a-date"
        errors = list(self.validator.iter_errors(instance))
        self.assertTrue(errors, "schema accepted reviewed_at: 'not-a-date'")

    def test_greenfield_mode_forbids_port_source(self):
        """Review finding D6: the if/then required port_source in port
        mode but nothing forbade a stray port_source under greenfield."""
        instance = json.loads(
            (EXAMPLES_DIR / "project-descriptor.greenfield.example.json").read_text()
        )
        instance["port_source"] = {
            "repository": "should not be here",
            "language": "cpp",
            "oracle_build_command": "make",
        }
        errors = list(self.validator.iter_errors(instance))
        self.assertTrue(errors, "schema accepted port_source under mode: greenfield")

    def test_unknown_verifier_is_rejected(self):
        instance = json.loads(
            (EXAMPLES_DIR / "project-descriptor.greenfield.example.json").read_text()
        )
        instance["verifier_policy"]["default"] = "not-a-real-verifier"
        errors = list(self.validator.iter_errors(instance))
        self.assertTrue(errors, "schema accepted an unknown verifier name")

    def test_closure_kind_is_accepted(self):
        """chainlink #73: the date-creusot pilot had no way to declare the
        intended closure_kind in the descriptor -- every shape it tried
        (top-level closure_kind, verifier_policy.closure_kind, a closure
        object, verifier_policy.closure_kinds) was rejected, so the intent
        could only be recorded outside the tool. The top-level field is
        the documented place."""
        for kind in ("deductive", "bounded"):
            with self.subTest(closure_kind=kind):
                instance = json.loads(
                    (EXAMPLES_DIR / "project-descriptor.greenfield.example.json").read_text()
                )
                instance["closure_kind"] = kind
                errors = list(self.validator.iter_errors(instance))
                self.assertEqual(errors, [], [e.message for e in errors])

    def test_closure_kind_rejects_value_outside_the_enum(self):
        instance = json.loads(
            (EXAMPLES_DIR / "project-descriptor.greenfield.example.json").read_text()
        )
        instance["closure_kind"] = "speculative"
        errors = list(self.validator.iter_errors(instance))
        self.assertTrue(errors, "schema accepted a closure_kind outside deductive/bounded")

    def test_closure_kind_rejects_non_string(self):
        instance = json.loads(
            (EXAMPLES_DIR / "project-descriptor.greenfield.example.json").read_text()
        )
        instance["closure_kind"] = 1
        errors = list(self.validator.iter_errors(instance))
        self.assertTrue(errors, "schema accepted a non-string closure_kind")


class SchemaDiagnosticsTest(unittest.TestCase):
    """chainlink #73: the load-time diagnostic for an invalid descriptor
    names the offending property -- with its JSON path and the permitted
    alternatives -- instead of jsonschema's raw messages, which bury the
    rejected key in prose ("Additional properties are not allowed
    ('commit' was unexpected)") or print the whole descriptor as the
    failing instance."""

    def setUp(self):
        self.example = json.loads(
            (EXAMPLES_DIR / "project-descriptor.port.example.json").read_text()
        )

    def test_valid_descriptor_has_no_diagnostics(self):
        self.assertEqual(schema_diagnostics(self.example), [])

    def test_unknown_property_names_json_path_and_permitted_alternatives(self):
        data = json.loads(json.dumps(self.example))
        data["port_source"]["commit"] = "abc123"
        self.assertEqual(
            schema_diagnostics(data),
            [
                "$.port_source: unexpected property 'commit'; "
                "permitted: language, oracle_build_command, repository"
            ],
        )

    def test_unknown_top_level_property_names_the_permitted_top_level_keys(self):
        data = json.loads(json.dumps(self.example))
        data["closure_kinds"] = ["deductive"]
        (line,) = schema_diagnostics(data)
        self.assertTrue(line.startswith("$:"))  # the top-level object's JSON path
        self.assertIn("unexpected property 'closure_kinds'", line)
        self.assertIn("closure_kind", line)  # the near-miss the pilot wanted

    def test_enum_violation_names_the_permitted_values(self):
        data = json.loads(json.dumps(self.example))
        data["mode"] = "hybrid"
        lines = schema_diagnostics(data)
        self.assertIn("$.mode: 'hybrid' is not one of ['greenfield', 'port']", lines)

    def test_missing_required_property_is_named(self):
        data = json.loads(json.dumps(self.example))
        del data["write_set"]
        self.assertEqual(
            schema_diagnostics(data),
            ["$: missing required property 'write_set'"],
        )

    def test_multiple_problems_each_get_a_line(self):
        data = json.loads(json.dumps(self.example))
        data["port_source"]["commit"] = "abc123"
        data["verifier_policy"]["default"] = "dafny"
        lines = schema_diagnostics(data)
        self.assertEqual(len(lines), 2)
        self.assertIn("$.port_source", lines[0])
        self.assertIn("$.verifier_policy.default", lines[1])


if __name__ == "__main__":
    unittest.main()
