import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from satisfies import satisfies  # noqa: E402


def required_profile(**overrides) -> dict:
    profile = {
        "required_claims": ["callee-precondition-established"],
        "accepted_evidence_kinds": ["kani-bounded-model-check"],
        "minimum_scope": {"input_domain": "queue_len_le_8"},
        "trust_policy": {"assumptions_allowed": []},
    }
    profile.update(overrides)
    return profile


def achieved_record(**overrides) -> dict:
    record = {
        "schema_version": "1.0",
        "claim": {"kind": "callee-precondition-established", "result": "pass"},
        "evidence": {
            "kind": "kani-bounded-model-check",
            "verifier": "kani",
            "harness": "dispatch_C002_bridge",
            "scope": {"input_domain": "queue_len_le_8", "unwind": "8"},
        },
        "trust": {"assumptions": []},
        "support": {"status": "supported"},
        "config": {"toolchain": "nightly-2026-05-01", "target": "x86_64-unknown-linux-gnu", "features": ["default"]},
    }
    record.update(overrides)
    return record


class SatisfiesPositiveTest(unittest.TestCase):
    def test_fully_matching_record_satisfies(self):
        result = satisfies(required_profile(), achieved_record())
        self.assertTrue(result.holds, result.reasons)
        self.assertTrue(bool(result))
        self.assertEqual(result.reasons, [])

    def test_extra_achieved_scope_keys_beyond_minimum_scope_are_fine(self):
        """minimum_scope declares a floor, not an exact-match set --
        evidence.scope may carry additional keys (e.g. unwind, harness_hash)
        the required_profile never mentions."""
        result = satisfies(
            required_profile(minimum_scope={"input_domain": "queue_len_le_8"}),
            achieved_record(evidence={
                "kind": "kani-bounded-model-check", "verifier": "kani", "harness": "h",
                "scope": {"input_domain": "queue_len_le_8", "unwind": "8", "harness_hash": "sha256:abc"},
            }),
        )
        self.assertTrue(result.holds, result.reasons)

    def test_no_minimum_scope_declared_is_vacuously_satisfied(self):
        """required_profile with minimum_scope entirely absent (the
        work-package-manifest schema variant, where it's optional) --
        no scope constraint was declared, so there is nothing to
        violate."""
        profile = required_profile()
        del profile["minimum_scope"]
        result = satisfies(profile, achieved_record())
        self.assertTrue(result.holds, result.reasons)


class SatisfiesAchievedRecordInvalidTest(unittest.TestCase):
    """achieved_record must itself be schema-valid -- a malformed record
    can never satisfy anything, regardless of what its fields say."""

    def test_missing_required_field_never_satisfies(self):
        record = achieved_record()
        del record["config"]
        result = satisfies(required_profile(), record)
        self.assertFalse(result.holds)
        self.assertTrue(any("not schema-valid" in r for r in result.reasons), result.reasons)

    def test_unknown_claim_kind_never_satisfies(self):
        record = achieved_record(claim={"kind": "not-a-real-claim", "result": "pass"})
        result = satisfies(required_profile(), record)
        self.assertFalse(result.holds)
        self.assertTrue(any("not schema-valid" in r for r in result.reasons), result.reasons)


class SatisfiesSupportTest(unittest.TestCase):
    def test_unsupported_status_never_satisfies_even_if_everything_else_matches(self):
        """plan.md §16.4: full witness coverage with zero verifier results
        is still unsupported -- an unconditional failure, not something the
        other fields can outweigh."""
        record = achieved_record(support={"status": "unsupported"})
        result = satisfies(required_profile(), record)
        self.assertFalse(result.holds)
        self.assertTrue(any("support.status" in r for r in result.reasons), result.reasons)


class SatisfiesClaimTest(unittest.TestCase):
    def test_claim_result_fail_is_rejected(self):
        record = achieved_record(claim={"kind": "callee-precondition-established", "result": "fail"})
        result = satisfies(required_profile(), record)
        self.assertFalse(result.holds)
        self.assertTrue(any("claim.result" in r for r in result.reasons), result.reasons)

    def test_claim_kind_not_among_required_claims_is_rejected(self):
        record = achieved_record(claim={"kind": "postcondition-holds", "result": "pass"})
        result = satisfies(required_profile(required_claims=["callee-precondition-established"]), record)
        self.assertFalse(result.holds)
        self.assertTrue(any("claim.kind" in r for r in result.reasons), result.reasons)

    def test_empty_required_claims_never_satisfies(self):
        result = satisfies(required_profile(required_claims=[]), achieved_record())
        self.assertFalse(result.holds)


class SatisfiesEvidenceKindTest(unittest.TestCase):
    def test_evidence_kind_not_among_accepted_kinds_is_rejected(self):
        record = achieved_record(evidence={
            "kind": "creusot-deductive-check", "verifier": "creusot", "harness": "h",
            "scope": {"input_domain": "queue_len_le_8"},
        })
        result = satisfies(required_profile(accepted_evidence_kinds=["kani-bounded-model-check"]), record)
        self.assertFalse(result.holds)
        self.assertTrue(any("evidence.kind" in r for r in result.reasons), result.reasons)


class SatisfiesScopeTest(unittest.TestCase):
    def test_missing_scope_key_is_rejected(self):
        record = achieved_record(evidence={
            "kind": "kani-bounded-model-check", "verifier": "kani", "harness": "h",
            "scope": {"unwind": "8"},  # missing input_domain
        })
        result = satisfies(required_profile(minimum_scope={"input_domain": "queue_len_le_8"}), record)
        self.assertFalse(result.holds)
        self.assertTrue(any("minimum_scope['input_domain']" in r for r in result.reasons), result.reasons)

    def test_mismatched_scope_value_is_rejected(self):
        record = achieved_record(evidence={
            "kind": "kani-bounded-model-check", "verifier": "kani", "harness": "h",
            "scope": {"input_domain": "queue_len_le_64"},
        })
        result = satisfies(required_profile(minimum_scope={"input_domain": "queue_len_le_8"}), record)
        self.assertFalse(result.holds)

    def test_no_fuzzy_numeric_ordering_is_inferred(self):
        """Scope values are opaque strings -- 'unwind: 16' does not
        satisfy a required 'unwind: 8' by numeric >= reasoning, since the
        vocabulary is not typed and no ordering is declared."""
        record = achieved_record(evidence={
            "kind": "kani-bounded-model-check", "verifier": "kani", "harness": "h",
            "scope": {"input_domain": "queue_len_le_8", "unwind": "16"},
        })
        result = satisfies(
            required_profile(minimum_scope={"input_domain": "queue_len_le_8", "unwind": "8"}), record
        )
        self.assertFalse(result.holds)


class SatisfiesTrustTest(unittest.TestCase):
    def test_disallowed_assumption_is_rejected(self):
        record = achieved_record(trust={"assumptions": ["chainlink:713"]})
        result = satisfies(required_profile(trust_policy={"assumptions_allowed": []}), record)
        self.assertFalse(result.holds)
        self.assertTrue(any("trust.assumptions" in r for r in result.reasons), result.reasons)

    def test_allowed_assumption_is_accepted(self):
        record = achieved_record(trust={"assumptions": ["chainlink:713"]})
        result = satisfies(required_profile(trust_policy={"assumptions_allowed": ["chainlink:713"]}), record)
        self.assertTrue(result.holds, result.reasons)

    def test_missing_trust_policy_allows_no_assumptions(self):
        """trust_policy absent (the work-package-manifest schema variant,
        where it's optional) means the allow-list is empty, not
        unrestricted -- fails closed the same direction missing
        minimum_scope fails open, deliberately, since one is a floor and
        the other is an allow-list."""
        profile = required_profile()
        del profile["trust_policy"]
        record = achieved_record(trust={"assumptions": ["chainlink:713"]})
        result = satisfies(profile, record)
        self.assertFalse(result.holds)

    def test_missing_trust_policy_with_no_assumptions_still_satisfies(self):
        profile = required_profile()
        del profile["trust_policy"]
        result = satisfies(profile, achieved_record())
        self.assertTrue(result.holds, result.reasons)


class SatisfiesContextIgnoredTest(unittest.TestCase):
    def test_context_argument_accepted_but_has_no_effect(self):
        result_without = satisfies(required_profile(), achieved_record())
        result_with = satisfies(required_profile(), achieved_record(), context={"anything": "goes"})
        self.assertEqual(result_without.holds, result_with.holds)


if __name__ == "__main__":
    unittest.main()
