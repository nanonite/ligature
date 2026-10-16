"""Tests over docs/cli-contract.md's legacy-command mapping table (chainlink
#55). Parses the markdown table directly rather than hand-copying it into
Python, so a future edit to the doc that breaks its own invariants (a
duplicated command, a stable name collision) is caught without anyone
having to remember to update a parallel Python copy.
"""
from __future__ import annotations

import re
import sys
import unittest
import io
import argparse
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import pipeline  # noqa: E402

CONTRACT_PATH = ROOT / "docs" / "cli-contract.md"

_ROW_RE = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*(.+?)\s*\|\s*$")


def _parse_mapping_table() -> list[tuple[str, str]]:
    """Section 10's table: | legacy command | disposition |. Returns
    [(command, disposition_text), ...] in document order."""
    text = CONTRACT_PATH.read_text()
    section = text.split("## 10. Legacy command", 1)[1]
    section = section.split("\n## 11.", 1)[0]
    rows = []
    for line in section.splitlines():
        m = _ROW_RE.match(line)
        if m:
            rows.append((m.group(1), m.group(2)))
    return rows


def _parse_section(heading: str) -> str:
    """The body of a `## ` section, bounded by the next `## ` heading -- the
    same bounding technique `_parse_mapping_table` uses to isolate §10. Raises
    if the heading is absent, so a renamed or deleted section fails loudly."""
    text = CONTRACT_PATH.read_text()
    section = text.split(heading, 1)[1]
    return section.split("\n## ", 1)[0]


class LegacyCommandMappingTest(unittest.TestCase):
    def setUp(self):
        self.rows = _parse_mapping_table()
        self.registered = set(pipeline.registered_commands())

    def test_table_is_nonempty(self):
        self.assertTrue(self.rows)

    def test_every_row_names_a_currently_registered_command(self):
        names = [name for name, _ in self.rows]
        unknown = set(names) - self.registered
        self.assertEqual(unknown, set(), f"table names commands argparse no longer registers: {sorted(unknown)}")

    def test_every_registered_command_has_exactly_one_row(self):
        names = [name for name, _ in self.rows]
        self.assertEqual(set(names), self.registered, "table and registered commands disagree")
        # exactly one row per name -- no duplicates
        seen = set()
        dupes = set()
        for name in names:
            if name in seen:
                dupes.add(name)
            seen.add(name)
        self.assertEqual(dupes, set(), f"duplicate rows for: {sorted(dupes)}")

    def test_every_row_has_a_nonempty_disposition(self):
        for name, disposition in self.rows:
            self.assertTrue(disposition.strip(), f"{name} has an empty disposition cell")

    def test_documented_inventory_count_matches_parser(self):
        header = re.search(r"## 10\. Legacy command.*\(complete, (\d+)/(\d+)\)", CONTRACT_PATH.read_text())
        self.assertIsNotNone(header, "section 10 must publish its registered-command count")
        self.assertEqual(tuple(map(int, header.groups())), (len(self.registered), len(self.registered)))


class CurrentExitAndJsonContractTest(unittest.TestCase):
    """The current-dispatcher table is separate from the aspirational shared
    exit-code vocabulary. Keep its command and --json inventory in sync with
    argparse so help, docs, and the binary surface cannot silently drift."""

    def _table_rows(self):
        text = (ROOT / "docs" / "exit-code-contract.md").read_text()
        section = text.split("## Registered command behavior", 1)[1]
        section = section.split("\n## ", 1)[0]
        rows = []
        for line in section.splitlines():
            if not line.startswith("|") or line.startswith("|---"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) != 3 or cells[0] == "command(s)":
                continue
            names = re.findall(r"`([^`]+)`", cells[0])
            rows.extend((name, cells[2]) for name in names)
        return rows

    def _parsers_within(self, parser):
        yield parser
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for child in action.choices.values():
                    yield from self._parsers_within(child)

    def test_every_registered_command_has_one_exit_and_json_profile(self):
        registered = pipeline.registered_commands()
        rows = self._table_rows()
        names = [name for name, _json in rows]
        self.assertEqual(set(names), set(registered))
        self.assertEqual(len(names), len(set(names)), "a command appears in more than one profile")

        actual_json = {
            name
            for name, parser in registered.items()
            if any(
                "--json" in action.option_strings
                for nested in self._parsers_within(parser)
                for action in nested._actions
            )
        }
        documented_json = {name for name, support in rows if support == "yes"}
        self.assertEqual(documented_json, actual_json)
        self.assertEqual(
            actual_json,
            {
                "migrate", "status", "check", "write-set-check", "authorize-write",
                "scaffold-crate", "configure-crate", "configure-cargo-profile",
                "generate-work-package", "schema", "gate-g14", "gate-r1-g16", "gate",
            },
        )

    def test_top_level_and_command_help_point_to_the_contract(self):
        parser = pipeline.build_parser()
        self.assertIn("Exit codes and JSON support are command-specific", parser.format_help())
        for name, command_parser in pipeline.registered_commands(parser).items():
            with self.subTest(command=name):
                self.assertIn("Exit codes and JSON support are command-specific", command_parser.format_help())

    def test_exit_contract_names_warning_and_human_decision_code_three(self):
        contract = (ROOT / "docs" / "exit-code-contract.md").read_text()
        self.assertIn("`gate-r1-g16`", contract)
        self.assertIn("`gate-g20`", contract)
        self.assertIn("visible warning(s)", contract)


class ConceptSearchRootHelpTest(unittest.TestCase):
    def test_pipeline_help_names_descriptor_search_root_for_cross_references(self):
        parser = pipeline.build_parser()
        for command in ("validate", "validate-witness", "status", "check"):
            with self.subTest(command=command):
                output = io.StringIO()
                with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                    parser.parse_args([command, "--help"])
                self.assertEqual(raised.exception.code, 0)
                self.assertIn("specs_search_root", output.getvalue())

    def test_schema_subcommand_help_describes_offline_bundled_resource_scope(self):
        parser = pipeline.build_parser()
        for argv, expected in (
            (["schema", "--help"], "checkout"),
            (["schema", "list", "--help"], "JSON Schemas shipped"),
            (["schema", "show", "--help"], "exact bundled schema"),
            (["schema", "export", "--help"], "existing paths are refused"),
        ):
            with self.subTest(argv=argv):
                output = io.StringIO()
                with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                    parser.parse_args(argv)
                self.assertEqual(raised.exception.code, 0)
                self.assertIn(expected, output.getvalue())

    def test_cli_contract_documents_exact_root_and_no_fallback(self):
        section = _parse_section("## Concept-spec search roots (#149)")
        self.assertIn("crates[].specs_search_root", section)
        self.assertIn("do not fall back", section.lower())
        self.assertIn("Ligature does not invent", section)


class StableCommandUniquenessTest(unittest.TestCase):
    """Every stable nested command this contract defines (validate <kind>,
    gate <gate-id>, approve <operation>, report <report-id>) must name a
    distinct (verb, argument) pair -- no two legacy commands may alias to
    the same stable destination unless they are genuinely the same
    operation (accept-promotion and status are the only 1:1 renames; every
    other alias target below is used by exactly one legacy row)."""

    ALIAS_ARROW_RE = re.compile(r"alias\s*(?:→|->)\s*`?([a-z][a-z0-9 -]*[a-z0-9])`?")

    def test_alias_targets_are_unique_except_documented_exceptions(self):
        rows = _parse_mapping_table()
        targets: dict[str, list[str]] = {}
        for name, disposition in rows:
            m = self.ALIAS_ARROW_RE.search(disposition)
            if not m:
                continue
            target = m.group(1).split(",")[0].split(" (")[0].strip()
            targets.setdefault(target, []).append(name)
        dupes = {t: names for t, names in targets.items() if len(names) > 1}
        self.assertEqual(dupes, {}, f"more than one legacy command aliases to the same stable target: {dupes}")


class NoAutonomousStartInvariantTest(unittest.TestCase):
    """docs/cli-contract.md §1 documents that the binary is a passive oracle:
    there is no `ligature start` or other supervisory verb that runs the phase
    loop autonomously. Pin both halves of that invariant -- the CLI must not
    register such a verb, and the prose asserting it must live in §1 (not
    merely somewhere in the file)."""

    def test_start_is_not_a_registered_command(self):
        registered = pipeline.registered_commands()
        self.assertNotIn(
            "start",
            registered,
            "docs/cli-contract.md §1 forbids a `ligature start` command, but "
            "pipeline.registered_commands() now registers one",
        )

    def test_no_start_invariant_is_stated_in_section_1(self):
        grammar = _parse_section("## 1. Grammar")
        self.assertRegex(
            grammar,
            r"(?i)no\s+`?ligature start`?",
            "docs/cli-contract.md §1 must state the no-ligature-start "
            "invariant; the sentence is missing from (or has moved out of) "
            "the Grammar section",
        )


if __name__ == "__main__":
    unittest.main()
