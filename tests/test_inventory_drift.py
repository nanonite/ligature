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

import ast
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from schema_utils import make_validator  # noqa: E402
import pipeline  # noqa: E402
import vendored_resources  # noqa: E402

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


_VENDOR_PATH_LITERAL_RE = re.compile(r"vendor/[\w.\-]+(?:/[\w.\-]+)*")


def _path_join_segments(node: ast.AST) -> list[str] | None:
    """Unwind a chain of `x / "a" / "b" / ...` BinOp(Div) nodes into an
    ordered list of the right-hand string segments. Returns None if any
    segment in the chain isn't a plain string literal (a dynamic segment
    can't be resolved statically, so such a chain is not a candidate)."""
    segments: list[str] = []
    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        right = node.right
        if not (isinstance(right, ast.Constant) and isinstance(right.value, str)):
            return None
        segments.insert(0, right.value)
        node = node.left
    return segments


def _call_string_args(node: ast.Call) -> list[str]:
    return [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]


def _docstring_constant_ids(tree: ast.AST) -> set[int]:
    """id() of every Constant node that IS a module/function/class
    docstring -- excluded from the literal-string bypass check below,
    since prose like 'validated against the real, vendored
    `vendor/concept-to-code/schemas/spec.schema.json`' legitimately names
    the file for a human reader without constructing a path. This is a
    deliberate, narrow carve-out: it does not exempt an ordinary string
    constant used as a runtime value (an f-string building an error
    message that embeds the same path text, for instance, is NOT a
    docstring and is still caught -- see the round-3 fix to
    select_pilot_cluster.py's own error message, rewritten to interpolate
    the resolved path object instead of repeating the literal string, so
    it no longer trips this same check it used to fail)."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                ids.add(id(node.body[0].value))
    return ids


def _vendor_bypass_offenses_in(script: Path) -> list[str]:
    """Every way this ONE script could construct or embed a path through a
    "vendor" segment WITHOUT going through vendored_resource_path():

    1. A pathlib `/`-chain with "vendor" as one segment
       (`Path(...) / "vendor" / ...`).
    2. A `.joinpath(...)` or `os.path.join(...)` call with "vendor" as one
       of its string arguments, OR a single argument that already
       contains "vendor/<segment>" (`joinpath("vendor", "x")`,
       `os.path.join("vendor/x")`).
    3. A `Path(...)` call whose argument is EITHER exactly "vendor"
       (`Path("vendor")`, typically the base of a `/`-chain continuing
       outside the call -- `Path("vendor") / "pkg" / "x"` -- which form 1
       alone does not catch, since `_path_join_segments` only walks the
       chain's own `/` nodes and treats the call at its base as opaque,
       never inspecting that call's own argument) OR a single string
       containing "vendor/<segment>" (`Path("vendor/concept-to-code/x")`).
    4. Any OTHER non-docstring string constant anywhere in the file
       (including inside an f-string's literal segments) matching
       `vendor/<segment>` -- the catch-all for constructions 1-3 don't
       cover (a bare `x = "vendor/foo/bar.json"` assignment, for
       instance).

    Docstrings are excluded (see _docstring_constant_ids) -- this is a
    real, deliberate scope boundary: prose mentioning a vendor/ path by
    name does not construct one. Everything else is in scope; round 3's
    review found the previous version of this check only covered form 1;
    round 4 found form 3 didn't catch `Path("vendor")` alone (only
    `Path("vendor/x")`), missing exactly the hybrid chain example above --
    confirmed directly (`_vendor_bypass_offenses_in` returned `[]` for it)
    before the `a == "vendor"` branch below was added."""
    tree = ast.parse(script.read_text())
    docstring_ids = _docstring_constant_ids(tree)
    offenses: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            segments = _path_join_segments(node)
            if segments and "vendor" in segments:
                offenses.append(f"path-join chain: {'/'.join(segments)}")

        elif isinstance(node, ast.Call):
            func = node.func
            is_joinpath = isinstance(func, ast.Attribute) and func.attr == "joinpath"
            is_os_path_join = (
                isinstance(func, ast.Attribute)
                and func.attr == "join"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "path"
            )
            is_path_call = isinstance(func, ast.Name) and func.id == "Path"
            if is_joinpath or is_os_path_join:
                args = _call_string_args(node)
                if "vendor" in args or any("vendor/" in a for a in args):
                    offenses.append(f"{func.attr}() call: {args}")
            elif is_path_call:
                args = _call_string_args(node)
                if any(a == "vendor" or "vendor/" in a for a in args):
                    offenses.append(f"Path() call: {args}")

        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstring_ids
            and _VENDOR_PATH_LITERAL_RE.search(node.value)
        ):
            offenses.append(f"string literal: {node.value!r}")

    return offenses


def _vendored_resource_path_call_sites_in(script: Path) -> tuple[set[str], list[str]]:
    """Every vendored_resource_path(...) call site in this ONE script
    (bare or module-qualified, e.g. vendored_resources.vendored_resource_path),
    split into (literal_keys, dynamic_call_descriptions). A call whose
    first argument is not a plain string constant -- a variable, an
    f-string, a concatenation -- cannot be resolved statically, and
    round-4 external review correctly flagged that the previous version
    of this scanner silently SKIPPED such a call instead of treating it
    as a finding: a dynamic argument defeats the entire point of a
    registry whose contract is "every access is a literal, auditable key"
    just as surely as bypassing the registry altogether does. There is no
    such call anywhere in this codebase today; if one is ever added, this
    function reports it as a dynamic call site rather than staying silent
    about it, and the test below fails the build on any non-empty list."""
    tree = ast.parse(script.read_text())
    keys: set[str] = set()
    dynamic: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_call = (isinstance(func, ast.Name) and func.id == "vendored_resource_path") or (
            isinstance(func, ast.Attribute) and func.attr == "vendored_resource_path"
        )
        if not is_call:
            continue
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            keys.add(node.args[0].value)
        else:
            dynamic.append(f"{script.name}:{node.lineno}")
    return keys, dynamic


class VendoredResourceRegistryTest(unittest.TestCase):
    """Round-3 external review: the previous AST scanner was the SOURCE OF
    TRUTH for "what's required," which is exactly the syntax-specific,
    existence-gated approach the review flagged. scripts/vendored_resources.py
    is now that source of truth instead -- an explicit, hand-maintained
    registry every runtime call site is expected to go through. Round 4
    found this class still didn't connect call sites to the registry (an
    unused registry entry, or a call naming an unregistered key, could
    both go unnoticed) and that the bypass detector recognized only the
    pathlib `/`-chain form. Both are fixed here: `test_registry_keys_
    match_call_sites` closes the first gap, and `_vendor_bypass_offenses_in`
    (used by `test_no_other_script_bypasses_the_registry`) now covers
    joinpath(), os.path.join(), single-argument Path(...), and a general
    non-docstring string-literal scan, not just the one join syntax."""

    def test_registry_matches_inventory_required_set(self):
        inventory = _load_inventory()
        required_paths = {
            e["path"] for e in inventory["vendored_runtime_assets"] if e["required_at_runtime"]
        }
        registered_paths = set(vendored_resources.VENDORED_RESOURCES.values())
        self.assertEqual(
            registered_paths - required_paths, set(),
            f"vendored_resources.py registers path(s) the inventory does not mark required_at_runtime: {sorted(registered_paths - required_paths)}",
        )
        self.assertEqual(
            required_paths - registered_paths, set(),
            f"inventory marks required_at_runtime but vendored_resources.py does not register it: {sorted(required_paths - registered_paths)}",
        )

    def test_every_registered_resource_exists_on_disk(self):
        for name in vendored_resources.VENDORED_RESOURCES:
            path = vendored_resources.vendored_resource_path(name)
            self.assertTrue(path.is_file(), f"registered resource {name!r} does not exist: {path}")

    def test_unregistered_resource_name_raises(self):
        with self.assertRaises(vendored_resources.UnregisteredVendoredResourceError):
            vendored_resources.vendored_resource_path("not-a-real-resource-name")

    def test_registry_keys_match_call_sites(self):
        """Bidirectional: every vendored_resource_path("key") call site
        across scripts/*.py must name a key that's actually registered
        (an unregistered key would raise at runtime -- caught here
        statically, before that), and every registered key must be
        called from at least one real call site (an unused registry
        entry is exactly as much drift as a missing one)."""
        called_keys: set[str] = set()
        for script in (ROOT / "scripts").glob("*.py"):
            keys, _dynamic = _vendored_resource_path_call_sites_in(script)
            called_keys |= keys
        registered_keys = set(vendored_resources.VENDORED_RESOURCES)
        self.assertEqual(
            called_keys - registered_keys, set(),
            f"vendored_resource_path() called with unregistered key(s): {sorted(called_keys - registered_keys)}",
        )
        self.assertEqual(
            registered_keys - called_keys, set(),
            f"registered but never called from any scripts/*.py call site: {sorted(registered_keys - called_keys)}",
        )

    def test_no_dynamic_vendored_resource_path_arguments(self):
        """Round-4 external review: a call site whose argument can't be
        resolved statically was previously SKIPPED by the key-matching
        check above, silently -- which defeats the registry's whole
        premise (every access is a literal, auditable key) exactly as
        much as bypassing it outright. Fails the build instead."""
        dynamic_sites: list[str] = []
        for script in (ROOT / "scripts").glob("*.py"):
            _keys, dynamic = _vendored_resource_path_call_sites_in(script)
            dynamic_sites.extend(dynamic)
        self.assertEqual(
            dynamic_sites, [],
            f"vendored_resource_path() called with a non-literal argument at: {dynamic_sites} "
            f"-- every call must pass a literal string key",
        )

    def test_no_other_script_bypasses_the_registry(self):
        """The registry is only durable if it's the ONE place a "vendor"
        path segment gets constructed or embedded. Verified directly
        across three rounds: a scratch script re-adding a bypass -- a
        pathlib `/`-chain, a bare `Path("vendor/x")` string literal, a
        `joinpath()`/`os.path.join()` call, and (round 4's own finding)
        `Path("vendor") / "pkg" / "x"`'s hybrid form -- was confirmed to
        trip this test each time before being removed."""
        offenders = {}
        for script in (ROOT / "scripts").glob("*.py"):
            if script.name == "vendored_resources.py":
                continue
            offenses = _vendor_bypass_offenses_in(script)
            if offenses:
                offenders[script.name] = offenses
        self.assertEqual(
            offenders, {},
            f"these scripts construct or embed a path through a 'vendor' segment instead of calling "
            f"vendored_resources.vendored_resource_path(): {offenders}",
        )


class VendorBypassDetectorUnitTest(unittest.TestCase):
    """Unit-level tests of `_vendor_bypass_offenses_in` itself, against
    synthetic scripts written to a temp directory -- independent of
    whatever scripts/*.py currently contains, so each known bypass form
    (and each known NON-bypass form) is pinned permanently, not just
    exercised incidentally by whatever real code happens to exist today.

    `test_hybrid_path_call_base_is_detected` is the round-4 external
    review's own reported repro: `Path("vendor") / "pkg" / "file.json"`
    reproduced directly and confirmed `_vendor_bypass_offenses_in`
    returned `[]` for it before the `Path(...)` call check was widened
    from "argument contains vendor/" to "argument equals vendor, or
    contains vendor/"."""

    def _offenses_for(self, source: str) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "scratch.py"
            script.write_text(source)
            return _vendor_bypass_offenses_in(script)

    def test_hybrid_path_call_base_is_detected(self):
        offenses = self._offenses_for(
            'from pathlib import Path\nX = Path("vendor") / "pkg" / "file.json"\n'
        )
        self.assertTrue(offenses, "Path(\"vendor\") / \"pkg\" / \"file.json\" was not detected")

    def test_plain_binop_chain_is_detected(self):
        offenses = self._offenses_for(
            'from pathlib import Path\nX = Path(__file__) / "vendor" / "pkg" / "file.json"\n'
        )
        self.assertTrue(offenses)

    def test_single_arg_path_call_is_detected(self):
        offenses = self._offenses_for('from pathlib import Path\nX = Path("vendor/pkg/file.json")\n')
        self.assertTrue(offenses)

    def test_joinpath_call_is_detected(self):
        offenses = self._offenses_for(
            'from pathlib import Path\nX = Path(__file__).joinpath("vendor", "pkg", "file.json")\n'
        )
        self.assertTrue(offenses)

    def test_os_path_join_call_is_detected(self):
        offenses = self._offenses_for('import os\nX = os.path.join("vendor", "pkg", "file.json")\n')
        self.assertTrue(offenses)

    def test_bare_string_literal_is_detected(self):
        offenses = self._offenses_for('X = "vendor/pkg/file.json"\n')
        self.assertTrue(offenses)

    def test_docstring_mention_is_not_detected(self):
        offenses = self._offenses_for(
            '"""Validated against vendor/concept-to-code/schemas/spec.schema.json."""\nX = 1\n'
        )
        self.assertEqual(offenses, [])

    def test_registry_style_call_is_not_detected(self):
        offenses = self._offenses_for(
            'from vendored_resources import vendored_resource_path\n'
            'X = vendored_resource_path("concept_to_code_spec_schema")\n'
        )
        self.assertEqual(offenses, [])

    def test_unrelated_path_is_not_detected(self):
        offenses = self._offenses_for('from pathlib import Path\nX = Path(__file__) / "docs" / "foo.json"\n')
        self.assertEqual(offenses, [])


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
