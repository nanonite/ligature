"""Cross-document consistency regression tests (chainlink #55 review
round 2). The first round of #55's contracts had three internal
contradictions that no schema or unit test caught -- each document was
internally well-formed, but two documents (or a document and a schema)
disagreed with each other about the same fact. These tests assert the
specific resolved facts directly, in plain text, so a future edit that
reintroduces any of the three contradictions fails immediately instead of
needing another external review pass to notice.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

CLI_CONTRACT = (ROOT / "docs" / "cli-contract.md").read_text()
TRUST_BOUNDARIES = (ROOT / "docs" / "trust-and-compatibility-boundaries.md").read_text()
CONSOLIDATED_CHECK_SCHEMA = json.loads((ROOT / "schemas" / "consolidated-check.schema.json").read_text())


class CheckReadOnlyConsistencyTest(unittest.TestCase):
    """Round-1 defect: docs/cli-contract.md said `check` "orchestrates
    [extract-c-static/check-bridges/render-witness] internally as
    needed" (all three write), while docs/trust-and-compatibility-
    boundaries.md said `check` never writes, and
    consolidated-check.schema.json required `mutated_workspace: const
    false`. All three cannot hold at once. Resolution: `check` is
    strictly read-only; it may only recommend one of the three via
    next_action, never run it."""

    def test_schema_still_requires_mutated_workspace_false(self):
        mw = CONSOLIDATED_CHECK_SCHEMA["properties"]["mutated_workspace"]
        self.assertEqual(mw.get("const"), False)

    def test_cli_contract_no_longer_claims_check_orchestrates_internally(self):
        self.assertNotIn("orchestrates these internally as needed", CLI_CONTRACT)

    def test_cli_contract_states_check_never_runs_them_automatically(self):
        normalized = " ".join(CLI_CONTRACT.split())
        self.assertIn("never invokes any of these three automatically", normalized)

    def test_trust_boundaries_still_lists_check_as_read_only(self):
        read_only_section = TRUST_BOUNDARIES.split("## 4. Which commands are read-only", 1)[1]
        read_only_section = read_only_section.split("## 5.", 1)[0]
        self.assertIn("`check`", read_only_section)


class StatusDoctorConsistencyTest(unittest.TestCase):
    """Round-1 defect: §1's grammar table already listed `ligature status`
    as the v1.0 project-state query, while §8 said legacy `status` keeps
    the OLD capability-summary meaning "through product version 1.x" --
    i.e. the same bare name was assigned two different meanings under the
    same version. Resolution: `status` means project state
    unconditionally from the first packaged release; there is no version
    window promising the old meaning under the bare name."""

    def test_no_transitional_1x_window_promise_remains(self):
        self.assertNotIn("through product version 1.x", CLI_CONTRACT)

    def test_status_reserved_unconditionally_for_project_state(self):
        section_8 = CLI_CONTRACT.split("## 8. The `status` stub", 1)[1]
        section_8 = section_8.split("## 9.", 1)[0]
        self.assertIn("unconditionally", section_8)

    def test_status_alias_not_governed_by_the_2_0_0_alias_floor(self):
        section_9 = CLI_CONTRACT.split("## 9. Compatibility alias policy", 1)[1]
        section_9 = section_9.split("## 10.", 1)[0]
        self.assertIn("`status` is explicitly **not** governed by this section", section_9)


class ReportWriteAuthorityConsistencyTest(unittest.TestCase):
    """Round-1 defect: docs/trust-and-compatibility-boundaries.md §4
    listed `report <report-id>` as read-only, then §5 said two of its
    report-ids (feature-ledger, contact-sheet) write files -- a direct
    contradiction. A third report-id (gold-set-measurement) also writes
    by default and was omitted from §5 entirely. Resolution: `report` is
    removed from the read-only list; §5 enumerates all three writing
    report-ids plus their (no-authority-required) write targets."""

    def test_report_removed_from_the_read_only_list(self):
        """Check only the actual enumeration sentence, not the whole §4 --
        §4 also explicitly says report is NOT read-only (mentioning the
        name to rule it out), which would false-positive a substring
        check over the full section."""
        read_only_section = TRUST_BOUNDARIES.split("## 4. Which commands are read-only", 1)[1]
        enumeration_line = read_only_section.strip().splitlines()[0]
        self.assertNotIn("report", enumeration_line)
        self.assertIn("`report <report-id>` is **not** in this read-only list", read_only_section)

    def test_write_table_lists_all_three_writing_report_ids(self):
        write_section = TRUST_BOUNDARIES.split("## 5. Which operations may write", 1)[1]
        write_section = write_section.split("## 6.", 1)[0]
        for report_id in ("report feature-ledger", "report contact-sheet", "report gold-set-measurement"):
            with self.subTest(report_id=report_id):
                self.assertIn(report_id, write_section)

    def test_cli_contract_report_table_has_a_writes_column(self):
        section_6 = CLI_CONTRACT.split("## 6. `report <report-id>`", 1)[1]
        section_6 = section_6.split("## 7.", 1)[0]
        self.assertIn("| writes?", section_6)
        self.assertIn("no — prints", section_6)


if __name__ == "__main__":
    unittest.main()
