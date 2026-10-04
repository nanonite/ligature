"""Cross-document consistency regression tests (chainlink #55 review
rounds 2 and 3). Round 2 found three internal contradictions no schema or
unit test caught -- each document was internally well-formed, but two
documents (or a document and a schema) disagreed with each other about the
same fact. Round 3 found a fourth, subtler one: the round-2 FIX for
`check`'s read-only status still pointed its only machine-consumable
field (`command`) at command names this same contract declares
unversioned, so the "fix" hadn't actually given a consumer anything
stable to act on. These tests assert the specific resolved facts
directly, in plain text, so a future edit can't reintroduce any of them
without another external review pass to notice.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import exit_codes  # noqa: E402

CLI_CONTRACT = (ROOT / "docs" / "cli-contract.md").read_text()
TRUST_BOUNDARIES = (ROOT / "docs" / "trust-and-compatibility-boundaries.md").read_text()
CONSOLIDATED_CHECK_SCHEMA = json.loads((ROOT / "schemas" / "consolidated-check.schema.json").read_text())

# chainlink #111: every document that states where a callee PRECONDITION
# belongs. Read from disk per test so a repository-state change is what a
# failure reports, not a stale module-level copy.
PRECONDITION_RULE_SOURCES = {
    "prompts/stage-3-boundary-drafting.md": ROOT / "prompts" / "stage-3-boundary-drafting.md",
    "prompts/stage-3-bridge-drafting.md": ROOT / "prompts" / "stage-3-bridge-drafting.md",
    "docs/reliance-policy.template.md": ROOT / "docs" / "reliance-policy.template.md",
    "docs/boundary-contract-schema.json": ROOT / "docs" / "boundary-contract-schema.json",
    "docs/bridge-schema.json": ROOT / "docs" / "bridge-schema.json",
}


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


class NextActionStableIdentifierConsistencyTest(unittest.TestCase):
    """Round-3 defect: the round-2 fix for check's read-only status still
    recommended the three internal operations via `next_action.command`
    alone -- the exact three names §7 itself declares unversioned and
    changeable without notice. A consumer had nothing stable to key off.
    Resolution: `action_id` is now the required, stable, versioned
    identifier for an automated-command next_action; `command` is
    explicitly advisory-only and forbidden from being the sole
    machine-consumable surface."""

    def test_schema_requires_action_id_for_automated_command(self):
        next_action_schema = CONSOLIDATED_CHECK_SCHEMA["$defs"]["next_action"]
        automated_command_branch = next_action_schema["allOf"][0]
        self.assertEqual(
            automated_command_branch["if"]["properties"]["kind"]["const"], "automated-command"
        )
        self.assertIn("action_id", automated_command_branch["then"]["required"])

    def test_schema_command_description_says_not_a_stability_guarantee(self):
        command_field = CONSOLIDATED_CHECK_SCHEMA["$defs"]["next_action"]["properties"]["command"]
        self.assertIn("NOT a stability guarantee", command_field["description"])

    def test_schema_action_id_description_names_it_the_contract_surface(self):
        action_id_field = CONSOLIDATED_CHECK_SCHEMA["$defs"]["next_action"]["properties"]["action_id"]
        self.assertIn("machine-consumable contract surface", action_id_field["description"])

    def test_cli_contract_defines_the_three_refresh_action_ids(self):
        section_7 = CLI_CONTRACT.split("## 7. Internal operations", 1)[1]
        section_7 = section_7.split("## 8.", 1)[0]
        for action_id in ("refresh-c-static", "refresh-bridge-checks", "refresh-witness"):
            with self.subTest(action_id=action_id):
                self.assertIn(action_id, section_7)

    def test_cli_contract_tells_consumers_to_branch_on_action_id_not_command(self):
        normalized = " ".join(CLI_CONTRACT.split())
        self.assertIn("MUST branch on `action_id`, never", normalized)


class ActionIdVocabularyConsistencyTest(unittest.TestCase):
    """Round-4 external review, two findings against round 3's own fix:
    (a) action_id was schema-open to any kebab-case string -- an invented
    value not in docs/cli-contract.md §7's table still validated; (b) the
    `command` field's description claimed it "may become null" for an
    automated-command action while the schema's own conditional always
    required it non-null -- a direct self-contradiction. Both resolved:
    action_id is now a closed `enum`, and the false nullability claim is
    removed from both the schema description and this doc's own prose."""

    def test_schema_action_id_is_a_closed_enum(self):
        action_id_field = CONSOLIDATED_CHECK_SCHEMA["$defs"]["next_action"]["properties"]["action_id"]
        self.assertIn("enum", action_id_field)
        self.assertNotIn("pattern", action_id_field)

    def test_schema_enum_matches_cli_contract_table(self):
        """The schema's enum and the doc's table must name the exact same
        set -- one governing the other silently is exactly the kind of
        two-sources-of-truth gap this whole test file exists to prevent."""
        schema_enum = set(CONSOLIDATED_CHECK_SCHEMA["$defs"]["next_action"]["properties"]["action_id"]["enum"])
        section_7 = CLI_CONTRACT.split("## 7. Internal operations", 1)[1]
        section_7 = section_7.split("## 8.", 1)[0]
        for action_id in schema_enum:
            with self.subTest(action_id=action_id):
                self.assertIn(f"`{action_id}`", section_7)

    def test_cli_contract_no_longer_says_command_may_be_null(self):
        self.assertNotIn('command may be non-null, may be null', CLI_CONTRACT)

    def test_cli_contract_states_command_always_present_for_automated_command(self):
        normalized = " ".join(CLI_CONTRACT.split())
        self.assertIn("never omitted or null for this kind", normalized)


class ExitCodeConditionVocabularyConsistencyTest(unittest.TestCase):
    """chainlink #75: the consolidated-check schema's `conditions` enum
    and scripts/exit_codes.py's CONDITION_NAMES must name the exact same
    set -- a condition the resolver knows but the schema rejects (or vice
    versa) is exactly the two-sources-of-truth gap this file exists to
    prevent."""

    def test_schema_conditions_enum_matches_exit_codes_condition_names(self):
        schema_enum = set(CONSOLIDATED_CHECK_SCHEMA["properties"]["result"]["properties"]["conditions"]["items"]["enum"])
        self.assertEqual(schema_enum, set(exit_codes.CONDITION_NAMES))

    def test_schema_exit_code_enum_matches_exit_codes_contract_codes(self):
        schema_codes = set(CONSOLIDATED_CHECK_SCHEMA["properties"]["result"]["properties"]["exit_code"]["enum"])
        self.assertEqual(
            schema_codes,
            {
                exit_codes.CLEAN,
                exit_codes.BLOCKING_FINDINGS,
                exit_codes.INVALID_INPUT,
                exit_codes.HUMAN_DECISION_REQUIRED,
                exit_codes.BACKEND_UNAVAILABLE,
                exit_codes.GATE_INTEGRITY_FAILED,
            },
        )


class CalleePreconditionPlacementConsistencyTest(unittest.TestCase):
    """chainlink #111: the exact shape of contradiction this file exists to
    catch, in the two templates that produce the artifacts it happened
    between. `prompts/stage-3-boundary-drafting.md` said a callee
    precondition the caller must establish does NOT belong in
    `callee_guarantees`; `prompts/stage-3-bridge-drafting.md` requires a
    bridge's `callee_requirement` to be one of the boundary's own
    `callee_guarantees` (G2, enforced by scripts/validate_bridge.py). Two
    installed, hash-pinned prompts, each internally coherent, jointly
    unsatisfiable: the boundary approved and the bridge that discharged its
    precondition was refused at approve time, which is what the swisstable-
    verus pilot reported.

    Five documents state this rule -- the two templates, the boundary
    contract schema's own `callee_guarantees` description, the bridge
    schema's own `callee_requirement` description, and the reliance-policy
    template's resolution table that `init` installs as the project's
    standing governance -- plus the boundary template's own re-entry prose.
    Each is asserted to place a callee precondition in `callee_guarantees`
    as the caller obligation a bridge discharges, and each is asserted NOT
    to still forbid it, so a future edit to any one of them cannot
    reintroduce the split without a test failing.
    """

    # The sentences the boundary template and the boundary schema shipped
    # with, in the form they shipped with. Either reappearing anywhere is
    # the defect returning. Scoped to those exact sentences deliberately:
    # "never a `callee_guarantees` entry" is still correct for the caller's
    # OWN obligations, which is a different prohibition (asserted separately,
    # on the row it belongs to).
    RETIRED = (
        "it belongs in a bridge specification (not yet in scope this stage)",
        "Never a callee precondition (-> bridge spec instead)",
        "callee **precondition** | bridge specification",
    )

    def _texts(self):
        # The schemas are compared as their own JSON text so a retired
        # phrase is caught whether it sits in prose or in an escaped string.
        return {
            name: (json.dumps(json.loads(path.read_text())) if path.suffix == ".json" else path.read_text())
            for name, path in PRECONDITION_RULE_SOURCES.items()
        }

    def test_no_document_still_forbids_a_precondition_in_callee_guarantees(self):
        for name, text in self._texts().items():
            for retired in self.RETIRED:
                with self.subTest(source=name):
                    self.assertNotIn(retired, text)

    def test_the_policy_tables_precondition_row_places_it_and_keeps_the_other_prohibitions(self):
        """`init` installs this template as the project's standing
        governance, so its table is the normative statement an operator
        reads. Read the rows rather than matching prose: the callee
        precondition row must place the entry in `callee_guarantees` without
        forbidding it, the caller's-own row must keep forbidding it (that one
        is correct and unrelated to #111), and the adversary row is
        unchanged."""
        text = (ROOT / "docs" / "reliance-policy.template.md").read_text()
        rows = {
            key: next(line for line in text.splitlines() if line.startswith(f"| {key}"))
            for key in (
                "callee **precondition**",
                "callee **postcondition**",
                "adversary case",
                "the **caller's own**",
            )
        }
        precondition_row = rows["callee **precondition**"]
        self.assertIn("`callee_guarantees`", precondition_row)
        self.assertNotIn("never", precondition_row)
        self.assertIn("caller", precondition_row)
        self.assertIn("bridge", precondition_row)
        # the rows #111 did not touch still say what they always said
        self.assertIn("`callee_guarantees`", rows["callee **postcondition**"])
        self.assertIn("**never**", rows["adversary case"])
        self.assertIn("never a `callee_guarantees` entry", rows["the **caller's own**"])

    def test_every_document_says_the_precondition_is_declared_here_and_discharged_by_a_bridge(self):
        for name, text in self._texts().items():
            normalized = " ".join(text.split())
            with self.subTest(source=name):
                self.assertIn("precondition", normalized)
                # the obligation the caller must establish, not a guarantee
                self.assertTrue(
                    "caller obligation" in normalized or "caller must establish" in normalized,
                    f"{name} never says who holds a precondition",
                )
                # and a bridge is what discharges it
                self.assertIn("bridge", normalized)

    def test_the_boundary_and_bridge_templates_name_each_other_on_this_rule(self):
        """Neither template may again be the only document that knows the
        rule: the author reading one is told where the other half is
        decided, which is what lets a model correct itself at draft time
        rather than at approve time."""
        boundary_text = " ".join(
            (ROOT / "prompts" / "stage-3-boundary-drafting.md").read_text().split()
        )
        bridge_text = " ".join(
            (ROOT / "prompts" / "stage-3-bridge-drafting.md").read_text().split()
        )
        self.assertIn("stage-3-bridge-drafting.md", boundary_text)
        self.assertIn("stage-3-boundary-drafting.md", bridge_text)

    def test_the_gates_and_the_documents_agree_about_the_requirement(self):
        """The mechanical half: validate_bridge's own G2 message is the
        document an author sees when the templates did not stop them, so it
        has to point at the same resolution rather than restating the
        impossibility it used to report."""
        from validate_boundary_contracts import OBLIGATION_ROLE, callee_obligation_role
        from validate_bridge import check_boundary_cross_reference

        boundary_id = "scheduler_dispatch__to__task_queue_pop_ready"
        findings = check_boundary_cross_reference(
            Path("crates/scheduler/specs/_bridges/BR-SCHED-TQ-001.json"),
            {"boundary_id": boundary_id, "callee_requirement": "TaskQueue.C001"},
            {boundary_id: {"callee_guarantees": ["TaskQueue.C002"]}},
        )
        self.assertEqual(len(findings), 1, [str(f) for f in findings])
        self.assertEqual(findings[0].severity, "error")
        self.assertIn("re-draft the boundary contract", findings[0].reason)

        # ...and the boundary gate's role classification is the one the
        # documents describe: a precondition is a caller obligation.
        self.assertEqual(callee_obligation_role({"kind": "precondition"}), OBLIGATION_ROLE)


if __name__ == "__main__":
    unittest.main()
