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


if __name__ == "__main__":
    unittest.main()
