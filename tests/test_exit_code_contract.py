"""Tests for scripts/exit_codes.py (docs/exit-code-contract.md, chainlink
#55). Precedence-order tests over the pure resolver -- not integration
tests against pipeline.py's own dispatch, which today still collapses most
of this into exit 1 (see the contract doc's own "Known drift" section;
that reconciliation is left to #56/#57/#58, not this task).
"""
from __future__ import annotations

import itertools
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import exit_codes  # noqa: E402


class ExitCodeValuesTest(unittest.TestCase):
    def test_five_contract_codes_are_0_through_4(self):
        self.assertEqual(exit_codes.CLEAN, 0)
        self.assertEqual(exit_codes.BLOCKING_FINDINGS, 1)
        self.assertEqual(exit_codes.INVALID_INPUT, 2)
        self.assertEqual(exit_codes.HUMAN_DECISION_REQUIRED, 3)
        self.assertEqual(exit_codes.BACKEND_UNAVAILABLE, 4)


class ResolvePrecedenceTest(unittest.TestCase):
    def test_empty_set_is_clean(self):
        self.assertEqual(exit_codes.resolve([]), exit_codes.CLEAN)

    def test_each_single_condition_maps_to_its_own_code(self):
        cases = {
            "invalid_input": exit_codes.INVALID_INPUT,
            "blocking_findings": exit_codes.BLOCKING_FINDINGS,
            "backend_unavailable": exit_codes.BACKEND_UNAVAILABLE,
            "human_decision_required": exit_codes.HUMAN_DECISION_REQUIRED,
        }
        for condition, expected in cases.items():
            with self.subTest(condition=condition):
                self.assertEqual(exit_codes.resolve([condition]), expected)

    def test_invalid_input_outranks_everything(self):
        others = ["blocking_findings", "backend_unavailable", "human_decision_required"]
        for r in range(len(others) + 1):
            for combo in itertools.combinations(others, r):
                with self.subTest(combo=combo):
                    self.assertEqual(
                        exit_codes.resolve({"invalid_input", *combo}), exit_codes.INVALID_INPUT
                    )

    def test_blocking_findings_outranks_backend_and_human_decision(self):
        for combo in [
            {"blocking_findings", "backend_unavailable"},
            {"blocking_findings", "human_decision_required"},
            {"blocking_findings", "backend_unavailable", "human_decision_required"},
        ]:
            with self.subTest(combo=combo):
                self.assertEqual(exit_codes.resolve(combo), exit_codes.BLOCKING_FINDINGS)

    def test_backend_unavailable_outranks_human_decision_required(self):
        self.assertEqual(
            exit_codes.resolve({"backend_unavailable", "human_decision_required"}),
            exit_codes.BACKEND_UNAVAILABLE,
        )

    def test_full_precedence_chain_all_four_present(self):
        self.assertEqual(
            exit_codes.resolve(
                {"invalid_input", "blocking_findings", "backend_unavailable", "human_decision_required"}
            ),
            exit_codes.INVALID_INPUT,
        )

    def test_order_and_duplicates_in_input_do_not_matter(self):
        a = exit_codes.resolve(["human_decision_required", "blocking_findings", "blocking_findings"])
        b = exit_codes.resolve(["blocking_findings", "human_decision_required"])
        self.assertEqual(a, b)
        self.assertEqual(a, exit_codes.BLOCKING_FINDINGS)

    def test_unrecognized_condition_raises_rather_than_silently_ignored(self):
        with self.assertRaises(ValueError):
            exit_codes.resolve(["blocking_findings", "made_up_condition"])

    def test_exhaustive_powerset_always_returns_a_valid_code(self):
        conditions = sorted(exit_codes.CONDITION_NAMES)
        valid_codes = {
            exit_codes.CLEAN, exit_codes.BLOCKING_FINDINGS, exit_codes.INVALID_INPUT,
            exit_codes.HUMAN_DECISION_REQUIRED, exit_codes.BACKEND_UNAVAILABLE,
        }
        for r in range(len(conditions) + 1):
            for combo in itertools.combinations(conditions, r):
                self.assertIn(exit_codes.resolve(combo), valid_codes)


if __name__ == "__main__":
    unittest.main()
