"""Drift test for docs/implementation-inventory.json (chainlink #55).

The inventory is hand-curated (role/disposition are judgment calls no
mechanical walk can make), so this test does not try to regenerate it. It
instead recomputes, from the actual repository state and from
scripts/pipeline.py's own registered argparse parser, every enumerable set
the inventory claims to cover, and fails the moment the inventory and the
repository disagree in EITHER direction -- an omission (something real that
the inventory doesn't mention) or a stale entry (something the inventory
lists that no longer exists). This is what #55's own text means by "a
drift test against the actual registered CLI/resources", not a hand-typed
count that can go stale silently.

Milestone data (M0-M4) is cross-checked manually against `chainlink
milestone list` at authoring time, not by this test -- shelling out to an
external issue-tracker binary would make the pytest suite depend on
`.chainlink/` state and a local `chainlink` install, which the rest of
this suite deliberately does not require.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from schema_utils import make_validator  # noqa: E402
import pipeline  # noqa: E402

INVENTORY_PATH = ROOT / "docs" / "implementation-inventory.json"
INVENTORY_SCHEMA_PATH = ROOT / "schemas" / "implementation-inventory.schema.json"


def _load_inventory():
    return json.loads(INVENTORY_PATH.read_text())


class InventorySchemaTest(unittest.TestCase):
    def test_inventory_validates_against_its_own_schema(self):
        schema = json.loads(INVENTORY_SCHEMA_PATH.read_text())
        validator = make_validator(schema)
        errors = list(validator.iter_errors(_load_inventory()))
        self.assertEqual(errors, [], [e.message for e in errors])


class CliCommandDriftTest(unittest.TestCase):
    """The inventory's cli_commands array vs. pipeline.py's own
    build_parser() -- the single source of truth for what argparse
    actually dispatches."""

    def setUp(self):
        self.inventory = _load_inventory()
        self.registered = pipeline.registered_commands()

    def test_every_registered_command_is_in_the_inventory(self):
        inventory_names = {c["name"] for c in self.inventory["cli_commands"]}
        missing = set(self.registered) - inventory_names
        self.assertEqual(missing, set(), f"registered but not inventoried: {sorted(missing)}")

    def test_inventory_has_no_stale_command_entries(self):
        inventory_names = {c["name"] for c in self.inventory["cli_commands"]}
        stale = inventory_names - set(self.registered)
        self.assertEqual(stale, set(), f"inventoried but no longer registered: {sorted(stale)}")

    def test_registered_command_count_matches_counts_field(self):
        self.assertEqual(
            self.inventory["counts"]["registered_cli_command_count"], len(self.registered)
        )


class PythonModuleDriftTest(unittest.TestCase):
    def setUp(self):
        self.inventory = _load_inventory()

    def _on_disk(self):
        return {
            f"scripts/{p.name}"
            for p in (ROOT / "scripts").glob("*.py")
        }

    def test_every_script_module_is_inventoried(self):
        inventoried = {m["path"] for m in self.inventory["python_modules"]}
        on_disk = self._on_disk()
        missing = on_disk - inventoried
        self.assertEqual(missing, set(), f"scripts/*.py not in inventory: {sorted(missing)}")

    def test_no_stale_python_module_entries(self):
        inventoried = {m["path"] for m in self.inventory["python_modules"]}
        on_disk = self._on_disk()
        stale = inventoried - on_disk
        self.assertEqual(stale, set(), f"inventoried scripts/*.py no longer on disk: {sorted(stale)}")


class SchemaAndPromptDriftTest(unittest.TestCase):
    def setUp(self):
        self.inventory = _load_inventory()

    def test_every_docs_json_schema_is_inventoried(self):
        on_disk = {f"docs/{p.name}" for p in (ROOT / "docs").glob("*.json")}
        inventoried = {e["path"] for e in self.inventory["schemas"] if e["path"].startswith("docs/")}
        self.assertEqual(on_disk - inventoried, set())
        self.assertEqual(inventoried - on_disk, set())

    def test_every_docs_markdown_doc_is_inventoried(self):
        on_disk = {f"docs/{p.name}" for p in (ROOT / "docs").glob("*.md")}
        inventoried = {e["path"] for e in self.inventory["documentation_and_templates"]}
        self.assertEqual(on_disk - inventoried, set())
        self.assertEqual(inventoried - on_disk, set())

    def test_every_top_level_schema_file_is_inventoried(self):
        on_disk = {f"schemas/{p.name}" for p in (ROOT / "schemas").glob("*.schema.json")}
        on_disk |= {"schemas/project-descriptor.schema.json"}
        inventoried = {e["path"] for e in self.inventory["schemas"] if e["path"].startswith("schemas/")}
        self.assertEqual(on_disk - inventoried, set(), f"missing from inventory: {sorted(on_disk - inventoried)}")
        self.assertEqual(inventoried - on_disk, set(), f"stale inventory entries: {sorted(inventoried - on_disk)}")

    def test_every_schema_example_is_inventoried(self):
        on_disk = {f"schemas/examples/{p.name}" for p in (ROOT / "schemas" / "examples").glob("*.json")}
        inventoried = {e["path"] for e in self.inventory["schema_examples"]}
        self.assertEqual(on_disk - inventoried, set())
        self.assertEqual(inventoried - on_disk, set())

    def test_every_prompt_is_inventoried(self):
        on_disk = {f"prompts/{p.name}" for p in (ROOT / "prompts").glob("*.md")}
        inventoried = {e["path"] for e in self.inventory["prompts"]}
        self.assertEqual(on_disk - inventoried, set())
        self.assertEqual(inventoried - on_disk, set())


class TestAndFixtureDriftTest(unittest.TestCase):
    def setUp(self):
        self.inventory = _load_inventory()

    def test_every_test_file_is_inventoried(self):
        on_disk = {f"tests/{p.name}" for p in (ROOT / "tests").glob("test_*.py")}
        inventoried = {e["path"] for e in self.inventory["tests"]}
        self.assertEqual(on_disk - inventoried, set(), f"missing from inventory: {sorted(on_disk - inventoried)}")
        self.assertEqual(inventoried - on_disk, set(), f"stale inventory entries: {sorted(inventoried - on_disk)}")

    def test_test_count_matches_counts_field(self):
        on_disk = list((ROOT / "tests").glob("test_*.py"))
        self.assertEqual(self.inventory["counts"]["test_count"], len(on_disk))

    def test_every_fixture_directory_is_inventoried(self):
        on_disk = {
            f"tests/fixtures/{p.name}"
            for p in (ROOT / "tests" / "fixtures").iterdir()
            if p.is_dir()
        }
        inventoried = {e["path"] for e in self.inventory["fixtures"]}
        self.assertEqual(on_disk - inventoried, set(), f"missing from inventory: {sorted(on_disk - inventoried)}")
        self.assertEqual(inventoried - on_disk, set(), f"stale inventory entries: {sorted(inventoried - on_disk)}")

    def test_every_inventory_entry_has_test_only_disposition(self):
        for e in self.inventory["tests"]:
            self.assertEqual(e["disposition"], "test-only", e["path"])
        for e in self.inventory["fixtures"]:
            self.assertEqual(e["disposition"], "test-only", e["path"])


class VendoredAssetDriftTest(unittest.TestCase):
    """Only checks the ONE file scripts/select_pilot_cluster.py actually
    opens at runtime exists where the inventory says it does -- see
    scripts/select_pilot_cluster.py's own SPEC_SCHEMA_PATH-equivalent
    constant. Does not attempt to enumerate the whole vendored submodule
    tree (it is explicitly out-of-distribution; see the inventory's own
    'documentation' disposition for the rest of vendor/concept-to-code)."""

    def test_the_one_runtime_required_vendored_file_exists(self):
        inventory = _load_inventory()
        required = [
            e for e in inventory["vendored_runtime_assets"]
            if e["required_at_runtime"] is True
        ]
        self.assertEqual(len(required), 1, required)
        path = ROOT / required[0]["path"]
        self.assertTrue(path.is_file(), f"{path} does not exist on disk")

    def test_only_the_expected_path_is_marked_required(self):
        inventory = _load_inventory()
        required_paths = {
            e["path"] for e in inventory["vendored_runtime_assets"] if e["required_at_runtime"]
        }
        self.assertEqual(required_paths, {"vendor/concept-to-code/schemas/spec.schema.json"})


class CountsConsistencyTest(unittest.TestCase):
    """counts.by_disposition and counts.total_entries must be the actual
    tallies over the arrays in this same document -- a hand-edited count
    that silently drifted from its own arrays would defeat the point of
    having machine-readable counts at all."""

    def test_by_disposition_and_total_entries_match_the_arrays(self):
        inventory = _load_inventory()
        arrays = [
            "python_modules", "schemas", "schema_examples",
            "documentation_and_templates", "prompts", "tests", "fixtures",
            "vendored_runtime_assets",
        ]
        tally: dict[str, int] = {}
        total = 0
        for key in arrays:
            for entry in inventory[key]:
                tally[entry["disposition"]] = tally.get(entry["disposition"], 0) + 1
                total += 1
        self.assertEqual(inventory["counts"]["by_disposition"], tally)
        self.assertEqual(inventory["counts"]["total_entries"], total)


if __name__ == "__main__":
    unittest.main()
