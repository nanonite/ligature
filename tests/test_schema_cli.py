"""Offline schema catalog and export contract (Chainlink #157)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import schema_cli  # noqa: E402


class SchemaCliTest(unittest.TestCase):
    def test_catalog_includes_closure_and_degradation_with_build_identity_and_hashes(self):
        catalog = schema_cli.schema_catalog()
        self.assertIn("closure-profile", catalog)
        self.assertIn("degradation-record", catalog)
        for name in ("closure-profile", "degradation-record"):
            entry = catalog[name]
            self.assertTrue(entry["sha256"].startswith("sha256:"))
            self.assertTrue(entry["schema_version"])
            self.assertIn("$schema", entry["document"])

        closure = json.loads(schema_cli.render_schema_list(as_json=True))
        self.assertEqual(closure["format"], "ligature.schema-list.v1")
        self.assertTrue(closure["product_version"])
        self.assertIn("adjudicator", closure)
        rows = {row["name"]: row for row in closure["schemas"]}
        self.assertEqual(rows["closure-profile"]["related_schema_names"], ["degradation-record"])
        self.assertEqual(rows["degradation-record"]["related_schema_names"], ["closure-profile"])

    def test_show_and_export_include_related_documents_and_exact_schema_document(self):
        shown = json.loads(schema_cli.render_schema("closure-profile", as_json=True))
        self.assertEqual(shown["format"], "ligature.schema-export.v1")
        self.assertEqual(shown["schema"]["name"], "closure-profile")
        self.assertIn("$schema", shown["schema"]["document"])
        self.assertEqual([item["name"] for item in shown["related_schemas"]], ["degradation-record"])

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "closure-profile.export.json"
            message = schema_cli.export_schema("closure-profile", target)
            exported = json.loads(target.read_text(encoding="utf-8"))
            self.assertIn("closure-profile", message)
            self.assertEqual(exported, shown)
            with self.assertRaisesRegex(schema_cli.SchemaError, "refusing to overwrite"):
                schema_cli.export_schema("closure-profile", target)

    def test_unknown_name_fails_closed_with_catalog_guidance(self):
        with self.assertRaisesRegex(schema_cli.SchemaError, "run `ligature schema list`"):
            schema_cli.render_schema("not-a-schema", as_json=True)


if __name__ == "__main__":
    unittest.main()
