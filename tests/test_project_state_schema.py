"""Tests for schemas/project-state.schema.json (chainlink #55/#56's
`ligature status --json` contract). This schema is not implemented against
yet -- #56 owns writing the code that produces a real document. These
tests exercise the schema itself: the two hand-authored example documents
validate, deliberate mutations are rejected, and -- the regression test
#55's own text calls for -- the OLD flat capability-summary text
(`cmd_status`'s current output) cannot be reshaped into anything this
schema accepts, proving the new contract is not merely the old one
renamed.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from schema_utils import make_validator  # noqa: E402

SCHEMA_PATH = ROOT / "schemas" / "project-state.schema.json"
EXAMPLES_DIR = ROOT / "schemas" / "examples"


class ProjectStateSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA_PATH.read_text())
        cls.validator = make_validator(cls.schema)

    def _load(self, name):
        return json.loads((EXAMPLES_DIR / name).read_text())

    def test_empty_example_is_valid(self):
        errors = list(self.validator.iter_errors(self._load("project-state.empty.example.json")))
        self.assertEqual(errors, [], [e.message for e in errors])

    def test_mixed_example_is_valid(self):
        errors = list(self.validator.iter_errors(self._load("project-state.mixed.example.json")))
        self.assertEqual(errors, [], [e.message for e in errors])

    def test_rejects_unknown_top_level_field(self):
        doc = self._load("project-state.empty.example.json")
        doc["extra_field"] = "nope"
        self.assertTrue(list(self.validator.iter_errors(doc)))

    def test_rejects_missing_required_top_level_field(self):
        doc = self._load("project-state.empty.example.json")
        del doc["gate_integrity"]
        self.assertTrue(list(self.validator.iter_errors(doc)))

    def test_rejects_wrong_schema_version(self):
        doc = self._load("project-state.empty.example.json")
        doc["schema_version"] = "2.0"
        self.assertTrue(list(self.validator.iter_errors(doc)))

    def test_artifact_lifecycle_is_closed_enum(self):
        doc = self._load("project-state.mixed.example.json")
        doc["artifacts"][0]["lifecycle"] = "some-made-up-state"
        self.assertTrue(list(self.validator.iter_errors(doc)))

    def test_artifact_lifecycle_accepts_unknown(self):
        """Explicit unknown/not-checked states must never become a schema
        rejection -- missing information is a valid, honest report. Also
        drops promoted_hash: an artifact whose lifecycle could not be
        determined has no business claiming a promotion-time hash either
        (see the lifecycle conditionals test class below)."""
        doc = self._load("project-state.mixed.example.json")
        doc["artifacts"][0]["lifecycle"] = "unknown"
        doc["artifacts"][0]["content_hash"] = None
        del doc["artifacts"][0]["promoted_hash"]
        errors = list(self.validator.iter_errors(doc))
        self.assertEqual(errors, [], [e.message for e in errors])

    def test_artifact_lifecycle_unknown_still_accepts_a_real_content_hash(self):
        """'unknown' means lifecycle could not be determined -- it does not
        mean the hash is unavailable too; a real hash may coexist with an
        unknown lifecycle (e.g. the file was readable and hashable but its
        promotion-manifest lookup failed)."""
        doc = self._load("project-state.mixed.example.json")
        doc["artifacts"][0]["lifecycle"] = "unknown"
        del doc["artifacts"][0]["promoted_hash"]
        errors = list(self.validator.iter_errors(doc))
        self.assertEqual(errors, [], [e.message for e in errors])

    def test_absent_artifact_may_have_null_content_hash(self):
        doc = self._load("project-state.mixed.example.json")
        errors = list(self.validator.iter_errors(doc))
        self.assertEqual(errors, [])
        absent = [a for a in doc["artifacts"] if a["lifecycle"] == "absent"]
        self.assertTrue(absent)
        self.assertIsNone(absent[0]["content_hash"])

    def test_malformed_content_hash_is_rejected(self):
        doc = self._load("project-state.mixed.example.json")
        doc["artifacts"][0]["content_hash"] = "not-a-hash"
        self.assertTrue(list(self.validator.iter_errors(doc)))

    def test_required_and_achieved_assurance_are_never_merged(self):
        """A dimension required but not achieved must show up as TWO
        separate records (one per array), never collapsed into a single
        'partial' status -- this test asserts the shape allows exactly
        that disagreement to be represented."""
        doc = self._load("project-state.mixed.example.json")
        obligation = doc["obligations"][0]
        required_dims = {d["dimension"] for d in obligation["required_assurance"]}
        achieved_dims = {
            d["dimension"] for d in obligation["achieved_assurance"] if d["status"] == "achieved"
        }
        self.assertIn("differential", required_dims)
        self.assertNotIn("differential", achieved_dims)
        errors = list(self.validator.iter_errors(doc))
        self.assertEqual(errors, [])

    def test_witness_is_rejected_as_an_achieved_assurance_dimension(self):
        """Round-2 external review, round 3 finding: the schema's
        DESCRIPTION said witness doesn't belong here, but nothing actually
        enforced it -- {"dimension": "witness", "status": "achieved"}
        validated with zero errors. This is the exact reported repro,
        reproduced directly and now asserted to fail."""
        doc = self._load("project-state.mixed.example.json")
        doc["obligations"][0]["achieved_assurance"].append(
            {"dimension": "witness", "status": "achieved"}
        )
        errors = list(self.validator.iter_errors(doc))
        self.assertTrue(errors, "schema accepted 'witness' as an achieved assurance dimension")

    def test_witness_is_rejected_as_a_required_assurance_dimension_too(self):
        doc = self._load("project-state.mixed.example.json")
        doc["obligations"][0]["required_assurance"].append(
            {"dimension": "witness", "status": "missing"}
        )
        errors = list(self.validator.iter_errors(doc))
        self.assertTrue(errors, "schema accepted 'witness' as a required assurance dimension")

    def test_other_dimension_names_remain_open(self):
        """The vocabulary itself stays open (#56 owns closing it) -- only
        the one 'witness' collision is schema-forbidden."""
        doc = self._load("project-state.mixed.example.json")
        doc["obligations"][0]["achieved_assurance"].append(
            {"dimension": "some-future-dimension-name", "status": "achieved"}
        )
        errors = list(self.validator.iter_errors(doc))
        self.assertEqual(errors, [])

    def test_cluster_state_and_closure_kind_have_explicit_unknown(self):
        doc = self._load("project-state.mixed.example.json")
        doc["clusters"][0]["state"] = "unknown"
        doc["clusters"][0]["closure_kind"] = "unknown"
        errors = list(self.validator.iter_errors(doc))
        self.assertEqual(errors, [])

    def test_witness_summary_never_carries_an_assurance_field(self):
        """generated_observations must not smuggle in an assurance verdict
        -- additionalProperties:false on witness_summary is what actually
        enforces this."""
        doc = self._load("project-state.mixed.example.json")
        doc["generated_observations"]["witness_summaries"][0]["assurance_status"] = "achieved"
        self.assertTrue(list(self.validator.iter_errors(doc)))

    def test_gate_integrity_state_is_closed_enum(self):
        doc = self._load("project-state.empty.example.json")
        doc["gate_integrity"]["state"] = "totally-fine-trust-me"
        self.assertTrue(list(self.validator.iter_errors(doc)))


class ArtifactLifecycleInvariantTest(unittest.TestCase):
    """Chainlink #55 review finding (medium): the lifecycle/content_hash/
    promoted_hash relationships were prose-only -- nothing in the schema
    actually enforced them, so an artifact record with 'absent' lifecycle
    and a real content_hash, or 'draft' lifecycle and a null content_hash,
    validated anyway. These tests prove the conditional rules added to the
    `artifact` $def actually reject every such combination, not just that
    the two hand-authored examples happen to be internally consistent."""

    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA_PATH.read_text())
        cls.validator = make_validator(cls.schema)

    def _artifact(self, **overrides):
        base = {
            "artifact_id": "X",
            "kind": "boundary",
            "path": "specs/_boundaries/X.json",
            "content_hash": "sha256:" + "a" * 64,
            "lifecycle": "validated",
        }
        base.update(overrides)
        return base

    def _errors(self, artifact):
        doc = json.loads((EXAMPLES_DIR / "project-state.empty.example.json").read_text())
        doc["artifacts"] = [artifact]
        return list(self.validator.iter_errors(doc))

    def test_absent_with_a_real_content_hash_is_rejected(self):
        artifact = self._artifact(lifecycle="absent", content_hash="sha256:" + "a" * 64)
        self.assertTrue(self._errors(artifact))

    def test_absent_with_null_content_hash_is_accepted(self):
        artifact = self._artifact(lifecycle="absent", content_hash=None)
        self.assertEqual(self._errors(artifact), [])

    def test_draft_with_null_content_hash_is_rejected(self):
        artifact = self._artifact(lifecycle="draft", content_hash=None)
        self.assertTrue(self._errors(artifact))

    def test_invalid_with_null_content_hash_is_rejected(self):
        """'invalid' still means the file was read and hashed -- only
        'absent' may have a null hash."""
        artifact = self._artifact(lifecycle="invalid", content_hash=None)
        self.assertTrue(self._errors(artifact))

    def test_validated_with_a_real_content_hash_is_accepted(self):
        artifact = self._artifact(lifecycle="validated", content_hash="sha256:" + "a" * 64)
        self.assertEqual(self._errors(artifact), [])

    def test_promoted_without_promoted_hash_is_rejected(self):
        artifact = self._artifact(lifecycle="promoted")
        self.assertTrue(self._errors(artifact))

    def test_stale_by_hash_drift_without_promoted_hash_is_rejected(self):
        artifact = self._artifact(lifecycle="stale-by-hash-drift")
        self.assertTrue(self._errors(artifact))

    def test_promoted_with_promoted_hash_is_accepted(self):
        artifact = self._artifact(lifecycle="promoted", promoted_hash="sha256:" + "b" * 64)
        self.assertEqual(self._errors(artifact), [])

    def test_validated_with_a_promoted_hash_present_is_rejected(self):
        """promoted_hash must not exist at all for a lifecycle that was
        never promoted -- a leftover/stale promoted_hash on a merely
        'validated' artifact is exactly the kind of contradictory state
        this conditional exists to catch."""
        artifact = self._artifact(lifecycle="validated", promoted_hash="sha256:" + "b" * 64)
        self.assertTrue(self._errors(artifact))

    def test_unknown_lifecycle_permits_either_null_or_real_content_hash(self):
        for content_hash in (None, "sha256:" + "a" * 64):
            with self.subTest(content_hash=content_hash):
                artifact = self._artifact(lifecycle="unknown", content_hash=content_hash)
                self.assertEqual(self._errors(artifact), [])

    def test_unknown_lifecycle_still_forbids_promoted_hash(self):
        artifact = self._artifact(lifecycle="unknown", promoted_hash="sha256:" + "b" * 64)
        self.assertTrue(self._errors(artifact))


class OldCapabilitySummaryCannotSatisfySchemaTest(unittest.TestCase):
    """Regression test (#55's own requirement): the OLD cmd_status output
    -- a flat list of implemented/not-yet-implemented command names as
    strings -- cannot be reshaped into a document this schema accepts. If
    this test ever starts passing, the new contract has silently regressed
    into being the old one."""

    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA_PATH.read_text())
        cls.validator = make_validator(cls.schema)

    def test_old_style_capability_list_is_rejected(self):
        old_style_document = {
            "implemented": [
                "draft", "approve", "approve-pair", "approve-exemption-pair",
                "validate", "validate-interaction", "validate-exemption",
            ],
            "not_yet_implemented": {
                "G4/G5 evidence tracing/grounding": "not yet implemented",
                "emission": "Stage 5 itself is still not built",
                "8B": "#26 (M4)",
            },
        }
        errors = list(self.validator.iter_errors(old_style_document))
        self.assertTrue(errors, "the old flat capability summary validated against the new project-state schema")

    def test_a_document_with_only_the_old_fields_added_alongside_new_ones_is_still_rejected(self):
        """Not enough to just add old fields NEXT TO a valid document --
        additionalProperties:false must reject the leftover old shape too."""
        doc = json.loads((EXAMPLES_DIR / "project-state.empty.example.json").read_text())
        doc["implemented"] = ["draft", "approve"]
        errors = list(self.validator.iter_errors(doc))
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
