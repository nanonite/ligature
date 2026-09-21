"""id on concept-to-code's constraint $def (chainlink #40).

docs/concept-to-code-modifications.md gap #6, decided: add a REQUIRED
`id: string` (pattern ^C\\d{3}$) to concept-to-code's `constraint` $def.
vendor/concept-to-code is a read-only submodule pin -- nothing here edits
it. The proposed patch instead lives as its own artifact,
docs/concept-to-code-constraint-id-schema.json, so the extension's shape
can be regression-tested before the upstream maintainer ever applies it.
DriftGuardTest keeps that proposal honest: every field in it other than
`id` must match the live vendored schema's `constraint` $def exactly,
field for field, so this proposal can never silently drift from what it
is actually proposing a patch against.

Unlike gap #5's witness_required (optional, additive), this field is
required: an existing constraint with no `id` is correctly REJECTED by
this proposed schema, not accepted -- that asymmetry with
test_concept_to_code_witness_required_schema.py is the point, not a gap
in this file, and ExistingDocumentBackwardIncompatibilityTest below
proves it against real documents rather than only asserting it in prose.
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from schema_utils import make_validator  # noqa: E402

VENDOR_SPEC_SCHEMA = ROOT / "vendor" / "concept-to-code" / "schemas" / "spec.schema.json"
PROPOSED_SCHEMA = ROOT / "docs" / "concept-to-code-constraint-id-schema.json"


def vendor_constraint_def() -> dict:
    return json.loads(VENDOR_SPEC_SCHEMA.read_text())["$defs"]["constraint"]


def proposed_schema() -> dict:
    return json.loads(PROPOSED_SCHEMA.read_text())


def valid_constraint(**overrides) -> dict:
    data = {
        "english": "pop_ready returns None only when no task has deadline <= now",
        "logic": "true",
        "id": "C003",
    }
    data.update(overrides)
    return data


class DriftGuardTest(unittest.TestCase):
    """The proposed schema is a real patch against a real file -- it must
    stay byte-for-byte faithful to what it's patching, or the proposal
    itself is no longer honest about what it's asking for."""

    def test_the_base_fields_are_copied_verbatim_from_the_vendored_schema(self):
        vendor = vendor_constraint_def()
        proposed = proposed_schema()
        self.assertEqual(proposed["type"], vendor["type"])
        self.assertEqual(proposed["additionalProperties"], vendor["additionalProperties"])
        self.assertFalse(proposed["additionalProperties"])

        # `id` is added to `required` too -- that's the one place the
        # required list itself is expected to differ, since the whole
        # point of this proposal is making id mandatory.
        proposed_required = set(proposed["required"])
        self.assertEqual(proposed_required - {"id"}, set(vendor["required"]))
        self.assertIn("id", proposed_required)

        proposed_base_properties = dict(proposed["properties"])
        id_field = proposed_base_properties.pop("id")
        self.assertEqual(proposed_base_properties, vendor["properties"])

        self.assertEqual(id_field["type"], "string")
        self.assertNotIn("id", vendor["properties"])

    def test_the_vendored_submodule_itself_is_unmodified(self):
        self.assertNotIn('"id"', json.dumps(vendor_constraint_def()))


class SchemaValidityTest(unittest.TestCase):
    def setUp(self):
        self.validator = make_validator(proposed_schema())

    def errors(self, data: dict) -> list[str]:
        return [e.message for e in self.validator.iter_errors(data)]

    def test_a_correctly_patterned_id_is_valid(self):
        self.assertEqual(self.errors(valid_constraint(id="C001")), [])
        self.assertEqual(self.errors(valid_constraint(id="C999")), [])

    def test_omitting_id_is_rejected(self):
        data = valid_constraint()
        del data["id"]
        self.assertTrue(self.errors(data))

    def test_a_malformed_id_is_rejected(self):
        for bad in ("c003", "C3", "C0003", "C00A", "003", ""):
            with self.subTest(value=bad):
                self.assertTrue(self.errors(valid_constraint(id=bad)))

    def test_a_non_string_id_is_rejected(self):
        for bad in (3, None, [], {}, True):
            with self.subTest(value=bad):
                self.assertTrue(self.errors(valid_constraint(id=bad)))

    def test_an_unrelated_additional_property_is_still_rejected(self):
        self.assertTrue(self.errors(valid_constraint(bogus_field="x")))


class ExistingDocumentBackwardIncompatibilityTest(unittest.TestCase):
    """The inverse of gap #5's ExistingDocumentCompatibilityTest, on
    purpose: unlike witness_required, this extension is NOT additive.
    Every real constraint in this codebase's own vendored fixtures
    predates the id field and has none, so the proposed (required-id)
    schema correctly rejects every one of them today -- proving the
    "requires a backfill" caveat in docs/concept-to-code-modifications.md
    gap #6 is a real, mechanically-observable fact about existing
    documents, not just a sentence in a markdown file."""

    def setUp(self):
        self.validator = make_validator(proposed_schema())

    def test_every_constraint_in_every_vendored_fixture_spec_is_rejected_for_missing_id(self):
        vendor_root = ROOT / "vendor" / "concept-to-code"
        specs = [
            p for p in vendor_root.rglob("*.json")
            if "fixtures" in p.parts or "specs" in p.parts
        ]
        checked = 0
        for path in specs:
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(data, dict) or not isinstance(data.get("constraints"), list):
                continue
            for constraint in data["constraints"]:
                if not isinstance(constraint, dict):
                    continue
                with self.subTest(path=str(path.relative_to(vendor_root)), english=constraint.get("english")):
                    self.assertNotIn("id", constraint)
                    self.assertTrue([e.message for e in self.validator.iter_errors(constraint)])
                    checked += 1
        # Guards against the walk silently finding nothing (a moved
        # fixture directory, or every real fixture already carrying an id
        # some other way, would otherwise pass this test vacuously and
        # stop proving what it claims to prove).
        self.assertGreater(checked, 0)

    def test_the_same_constraint_with_an_id_backfilled_validates(self):
        vendor_root = ROOT / "vendor" / "concept-to-code"
        specs = [p for p in vendor_root.rglob("*.json") if "fixtures" in p.parts or "specs" in p.parts]
        for path in specs:
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(data, dict) or not isinstance(data.get("constraints"), list):
                continue
            for constraint in data["constraints"]:
                if isinstance(constraint, dict):
                    backfilled = {**constraint, "id": "C001"}
                    self.assertEqual([e.message for e in self.validator.iter_errors(backfilled)], [])
                    return
        self.fail("no real vendored constraint found to backfill against")


if __name__ == "__main__":
    unittest.main()
