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
        the documented place. #85 extends the vocabulary with `partial`
        so a pilot that cannot achieve full deductive closure can declare
        that intent too."""
        for kind in ("deductive", "bounded", "partial"):
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
        self.assertTrue(
            errors, "schema accepted a closure_kind outside deductive/bounded/partial"
        )

    def test_closure_kind_rejects_non_string(self):
        instance = json.loads(
            (EXAMPLES_DIR / "project-descriptor.greenfield.example.json").read_text()
        )
        instance["closure_kind"] = 1
        errors = list(self.validator.iter_errors(instance))
        self.assertTrue(errors, "schema accepted a non-string closure_kind")


class VerifierPolicyTest(unittest.TestCase):
    """chainlink #76: `verifier_policy` was the only open object in the
    descriptor, its value domain (creusot | verus | kani) was an accident of
    `additionalProperties` -- undiscoverable and unreported by any command --
    and a second or supporting verifier could not be expressed at all,
    while an arbitrary extra key holding a second enum member validated
    silently and was then discarded by every reader."""

    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA_PATH.read_text())
        cls.validator = make_validator(cls.schema)

    def _port_descriptor(self) -> dict:
        return json.loads(
            (EXAMPLES_DIR / "project-descriptor.port.example.json").read_text()
        )

    def test_verifier_policy_key_set_is_documented(self):
        """The open object's key set is documented in the schema itself:
        'default' (required), per-cluster override keys, and 'supporting'
        as the declared multi-verifier shape (chainlink #76)."""
        description = self.schema["properties"]["verifier_policy"]["description"]
        self.assertIn("default", description)
        self.assertIn("cluster", description)
        self.assertIn("supporting", description)

    def test_verifier_enum_domain_is_documented(self):
        """The value domain is discoverable from the schema, not only from
        a rejection message: the $defs/verifier description names the three
        permitted values and the exact-match rule (chainlink #76)."""
        description = self.schema["$defs"]["verifier"]["description"]
        for verifier in ("creusot", "verus", "kani"):
            self.assertIn(verifier, description)

    def test_supporting_array_is_the_real_multi_verifier_shape(self):
        """The P3 crypto-mixed pilot's verus + kani in one workspace, and
        the epic's 'Kani supporting evidence' probe, have a descriptor-level
        home: a documented list, not an arbitrary enum-valued key
        (chainlink #76)."""
        instance = self._port_descriptor()
        instance["verifier_policy"] = {"default": "verus", "supporting": ["kani"]}
        errors = list(self.validator.iter_errors(instance))
        self.assertEqual(errors, [], [e.message for e in errors])

    def test_supporting_rejects_a_bare_string(self):
        """The fake-write from chainlink #76 Defect 3 --
        `verifier_policy.supporting: "kani"` (a string, not a list) -- is
        now a schema violation, not a silently discarded declaration."""
        instance = self._port_descriptor()
        instance["verifier_policy"] = {"default": "verus", "supporting": "kani"}
        errors = list(self.validator.iter_errors(instance))
        self.assertTrue(errors, "schema accepted supporting as a bare string")

    def test_supporting_rejects_a_non_member(self):
        instance = self._port_descriptor()
        instance["verifier_policy"] = {"default": "verus", "supporting": ["dafny"]}
        errors = list(self.validator.iter_errors(instance))
        self.assertTrue(errors, "schema accepted a non-member in supporting")

    def test_per_cluster_override_is_still_accepted(self):
        """gate_g9.py resolves `policy.get(cluster, policy["default"])`,
        so per-cluster override keys must stay expressible -- the fix
        documents the key set rather than closing the object (chainlink
        #76)."""
        instance = self._port_descriptor()
        instance["verifier_policy"] = {"default": "creusot", "crypto-mixed": "kani"}
        errors = list(self.validator.iter_errors(instance))
        self.assertEqual(errors, [], [e.message for e in errors])

    def test_misspelled_key_is_accepted_as_a_cluster_override(self):
        """The documented behavior for the Defect 1 hazard: an arbitrary
        key is a per-cluster override, so `defualt: "kani"` validates as an
        override for a cluster literally named 'defualt'. Closing the
        object would break per-cluster overrides, so the hazard is
        mitigated instead: the key set is documented on the schema, and
        `status --json` echoes the policy so the typo is visible under
        `clusters` rather than silent (chainlink #76)."""
        instance = self._port_descriptor()
        instance["verifier_policy"]["defualt"] = "kani"
        errors = list(self.validator.iter_errors(instance))
        self.assertEqual(errors, [], [e.message for e in errors])


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

    def test_unknown_key_and_invalid_value_are_distinguished(self):
        """chainlink #76 Defect 3: 'unknown key' and 'invalid value' were
        the same undifferentiated present-invalid / invalid_input. The
        diagnostic text must say which happened: an unexpected property
        names the permitted alternatives; an enum violation names the
        permitted values (chainlink #73's distinction, pinned for the
        verifier_policy field the pilot is named after)."""
        data = json.loads(json.dumps(self.example))
        data["verifier_policy"]["zzz_not_a_real_key"] = "nope"
        (line,) = schema_diagnostics(data)
        self.assertIn("$.verifier_policy.zzz_not_a_real_key", line)
        self.assertIn("is not one of", line)
        for verifier in ("kani", "creusot", "verus"):
            self.assertIn(verifier, line)

    def test_unknown_key_in_a_closed_object_names_permitted_alternatives(self):
        """The other half of the distinction: a key the schema does not
        know in a CLOSED object is an 'unexpected property', not a bad
        value -- the two failure kinds never collapse into one message
        (chainlink #76)."""
        data = json.loads(json.dumps(self.example))
        data["compatibility_policy"]["zzz"] = "kani"
        (line,) = schema_diagnostics(data)
        self.assertIn("unexpected property 'zzz'", line)
        self.assertIn("permitted:", line)


if __name__ == "__main__":
    unittest.main()
