"""witness_required on concept-to-code's query $def (chainlink #33).

docs/concept-to-code-modifications.md gap #5, decided: add an optional
`witness_required: boolean` (default false) to concept-to-code's `query`
$def, co-located with `pure`. vendor/concept-to-code is a read-only
submodule pin -- nothing here edits it. The proposed patch instead lives
as its own artifact, docs/concept-to-code-witness-required-schema.json,
so the extension's shape can be regression-tested before the upstream
maintainer ever applies it. DriftGuardTest keeps that proposal honest:
every field in it other than witness_required must match the live
vendored schema's `query` $def exactly, field for field, so this
proposal can never silently drift from what it is actually proposing a
patch against.
"""
import json
import sys
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from schema_utils import make_validator  # noqa: E402

VENDOR_SPEC_SCHEMA = ROOT / "vendor" / "concept-to-code" / "schemas" / "spec.schema.json"
PROPOSED_SCHEMA = ROOT / "docs" / "concept-to-code-witness-required-schema.json"


def vendor_query_def() -> dict:
    return json.loads(VENDOR_SPEC_SCHEMA.read_text())["$defs"]["query"]


def proposed_schema() -> dict:
    return json.loads(PROPOSED_SCHEMA.read_text())


def valid_query(**overrides) -> dict:
    data = {
        "english": "How many components does the kernel have?",
        "rust_sig": "fn component_count(&self) -> usize",
        "pure": True,
    }
    data.update(overrides)
    return data


class DriftGuardTest(unittest.TestCase):
    """The proposed schema is a real patch against a real file -- it must
    stay byte-for-byte faithful to what it's patching, or the proposal
    itself is no longer honest about what it's asking for."""

    def test_the_base_fields_are_copied_verbatim_from_the_vendored_schema(self):
        vendor = vendor_query_def()
        proposed = proposed_schema()
        self.assertEqual(proposed["type"], vendor["type"])
        self.assertEqual(proposed["required"], vendor["required"])
        self.assertEqual(proposed["additionalProperties"], vendor["additionalProperties"])
        self.assertFalse(proposed["additionalProperties"])

        proposed_base_properties = dict(proposed["properties"])
        witness_required = proposed_base_properties.pop("witness_required")
        self.assertEqual(proposed_base_properties, vendor["properties"])

        self.assertEqual(witness_required["type"], "boolean")
        self.assertNotIn("witness_required", vendor["properties"])

    def test_the_vendored_submodule_itself_is_unmodified(self):
        # The whole point of proposing a patch rather than applying one:
        # the real schema concept-to-code's own tooling reads from must
        # still have no knowledge of witness_required.
        self.assertNotIn("witness_required", json.dumps(vendor_query_def()))


class SchemaValidityTest(unittest.TestCase):
    def setUp(self):
        self.validator = make_validator(proposed_schema())

    def errors(self, data: dict) -> list[str]:
        return [e.message for e in self.validator.iter_errors(data)]

    def test_witness_required_true_is_valid(self):
        self.assertEqual(self.errors(valid_query(witness_required=True)), [])

    def test_witness_required_false_is_valid(self):
        self.assertEqual(self.errors(valid_query(witness_required=False)), [])

    def test_omitting_witness_required_is_valid_and_means_false(self):
        data = valid_query()
        self.assertNotIn("witness_required", data)
        self.assertEqual(self.errors(data), [])
        self.assertEqual(
            proposed_schema()["properties"]["witness_required"]["default"], False
        )

    def test_a_non_boolean_witness_required_is_rejected(self):
        for bad in ("true", 1, 0, None, [], {}, "false"):
            with self.subTest(value=bad):
                self.assertTrue(self.errors(valid_query(witness_required=bad)))

    def test_an_unrelated_additional_property_is_still_rejected(self):
        # additionalProperties: false must survive the extension --
        # adding one legitimate optional field is not license for the
        # $def to start accepting arbitrary ones.
        self.assertTrue(self.errors(valid_query(bogus_field="x")))


class ExistingDocumentCompatibilityTest(unittest.TestCase):
    """Every query document that validates against vendor's schema today
    must keep validating once witness_required is added -- the extension
    is additive by construction, and this proves it against real
    documents, not just hand-written fixtures."""

    def setUp(self):
        self.validator = make_validator(proposed_schema())

    def test_every_query_in_every_vendored_fixture_spec_remains_valid(self):
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
            if not isinstance(data, dict) or not isinstance(data.get("queries"), list):
                continue
            for query in data["queries"]:
                if not isinstance(query, dict):
                    continue
                with self.subTest(path=str(path.relative_to(vendor_root)), rust_sig=query.get("rust_sig")):
                    self.assertEqual(
                        [e.message for e in self.validator.iter_errors(query)], []
                    )
                    checked += 1
        # Guards against the walk silently finding nothing (a moved
        # fixture directory would otherwise pass this test vacuously).
        self.assertGreater(checked, 0)

    def test_a_document_still_valid_against_the_real_vendored_schema_also_validates_here(self):
        vendor_validator = Draft202012Validator(vendor_query_def())
        proposed = valid_query()
        self.assertEqual(list(vendor_validator.iter_errors(proposed)), [])
        self.assertEqual(self.errors_against_proposed(proposed), [])

    def errors_against_proposed(self, data: dict) -> list[str]:
        return [e.message for e in self.validator.iter_errors(data)]


if __name__ == "__main__":
    unittest.main()
