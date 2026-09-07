"""The canonical witness result: encoding, serialization, hashing (#27).

plan.md §16.1's reason for existing is that SVG bytes move when the value
does not. These tests pin the other half of that argument: the canonical
result moves when — and only when — the value does.
"""
import json
import subprocess
import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from witness_result import (  # noqa: E402
    CANONICALIZATION_RULE,
    HASHED_FIELDS,
    WitnessResultError,
    build_result,
    canonical_number,
    canonical_payload,
    compute_value_domain,
    compute_value_hash,
    encode_grid,
    encode_scalar,
    encode_series,
    is_canonical_decimal,
    is_canonically_encoded,
    values_of,
)

PRODUCER = ROOT / "tests" / "fixtures" / "witnesses" / "stand_in_producer.py"


def result_document(**overrides) -> dict:
    document = build_result(
        witness_id="W-TQ-LOAD-FACTOR",
        concept="TaskQueue",
        query="load_factor",
        fixture_id="FX-QUEUE-BOTTOM-ROW",
        seed=0,
        renderer_actual="scalar_field_svg",
        result=encode_grid(1, 4, [(0, 0, 0.125), (0, 1, 0.25), (0, 2, 0.375), (0, 3, 0.5)]),
    )
    document.update(overrides)
    return document


class CanonicalNumberTest(unittest.TestCase):
    """The float rule: hashing never formats a float, because a canonical
    result carries no JSON numbers for measured values."""

    def test_shortest_round_trip_is_preserved(self):
        self.assertEqual(canonical_number(0.1), "0.1")
        self.assertEqual(canonical_number(0.125), "0.125")
        self.assertEqual(canonical_number(1 / 3), "0.3333333333333333")

    def test_every_encoding_round_trips_exactly(self):
        for value in (0.1, 1 / 3, 1e-300, 2.5e17, 123456.789, -0.0625):
            with self.subTest(value=value):
                self.assertEqual(float(canonical_number(value)), value)

    def test_one_spelling_of_zero(self):
        self.assertEqual(canonical_number(0.0), "0")
        self.assertEqual(canonical_number(-0.0), "0")
        self.assertEqual(canonical_number(0), "0")

    def test_an_integral_float_and_its_integer_encode_identically(self):
        # Same value; a hash that told them apart would report a
        # difference nobody made.
        self.assertEqual(canonical_number(3.0), canonical_number(3))
        self.assertEqual(canonical_number(3.0), "3")

    def test_exponents_are_normalized(self):
        encoded = canonical_number(1e-300)
        self.assertNotIn("+", encoded)
        self.assertNotIn("e0", encoded)
        self.assertTrue(is_canonical_decimal(encoded), encoded)

    def test_every_encoding_matches_the_schema_pattern(self):
        for value in (0, 1, -1, 0.5, -0.5, 1e300, 1e-300, 2 ** 53, 1 / 7):
            with self.subTest(value=value):
                self.assertTrue(is_canonical_decimal(canonical_number(value)), canonical_number(value))

    def test_nan_and_infinity_are_refused(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(WitnessResultError):
                    canonical_number(value)

    def test_a_boolean_is_not_a_measured_value(self):
        with self.assertRaises(WitnessResultError):
            canonical_number(True)

    def test_a_string_is_not_silently_passed_through(self):
        with self.assertRaises(WitnessResultError):
            canonical_number("0.5")


class SerializationTest(unittest.TestCase):
    def test_the_payload_is_exactly_the_declared_hashed_fields(self):
        payload = json.loads(canonical_payload(result_document()))
        self.assertEqual(sorted(payload), sorted(HASHED_FIELDS))

    def test_the_payload_excludes_how_the_value_was_produced_or_shown(self):
        payload = canonical_payload(result_document())
        for excluded in ("renderer_actual", "value_domain", "value_hash", "canonicalization"):
            self.assertNotIn(excluded, payload)

    def test_the_payload_is_sorted_and_whitespace_free(self):
        payload = canonical_payload(result_document())
        self.assertNotIn(" ", payload)
        self.assertTrue(payload.startswith('{"concept":'))

    def test_a_document_missing_a_hashed_field_cannot_be_canonicalized(self):
        document = result_document()
        del document["seed"]
        with self.assertRaises(WitnessResultError):
            canonical_payload(document)


class ValueHashTest(unittest.TestCase):
    def test_the_same_values_hash_the_same(self):
        self.assertEqual(result_document()["value_hash"], result_document()["value_hash"])

    def test_a_different_renderer_does_not_change_the_hash(self):
        # The whole point of the split: value_hash is renderer-independent.
        baseline = result_document()
        other = build_result(
            "W-TQ-LOAD-FACTOR", "TaskQueue", "load_factor", "FX-QUEUE-BOTTOM-ROW", 0,
            "some_other_renderer",
            encode_grid(1, 4, [(0, 0, 0.125), (0, 1, 0.25), (0, 2, 0.375), (0, 3, 0.5)]),
        )
        self.assertEqual(baseline["value_hash"], other["value_hash"])

    def test_a_different_fixture_changes_the_hash(self):
        other = build_result(
            "W-TQ-LOAD-FACTOR", "TaskQueue", "load_factor", "FX-OTHER", 0, "scalar_field_svg",
            encode_grid(1, 4, [(0, 0, 0.125), (0, 1, 0.25), (0, 2, 0.375), (0, 3, 0.5)]),
        )
        self.assertNotEqual(result_document()["value_hash"], other["value_hash"])

    def test_a_different_seed_changes_the_hash(self):
        other = build_result(
            "W-TQ-LOAD-FACTOR", "TaskQueue", "load_factor", "FX-QUEUE-BOTTOM-ROW", 1,
            "scalar_field_svg",
            encode_grid(1, 4, [(0, 0, 0.125), (0, 1, 0.25), (0, 2, 0.375), (0, 3, 0.5)]),
        )
        self.assertNotEqual(result_document()["value_hash"], other["value_hash"])

    def test_one_changed_value_changes_the_hash(self):
        other = build_result(
            "W-TQ-LOAD-FACTOR", "TaskQueue", "load_factor", "FX-QUEUE-BOTTOM-ROW", 0,
            "scalar_field_svg",
            encode_grid(1, 4, [(0, 0, 0.125), (0, 1, 0.25), (0, 2, 0.375), (0, 3, 0.625)]),
        )
        self.assertNotEqual(result_document()["value_hash"], other["value_hash"])

    def test_the_hash_is_over_the_payload_it_reports(self):
        import hashlib

        document = result_document()
        expected = "sha256:" + hashlib.sha256(canonical_payload(document).encode()).hexdigest()
        self.assertEqual(document["value_hash"], expected)

    def test_build_result_derives_the_hash_rather_than_accepting_one(self):
        document = build_result(
            "W-TQ-LOAD-FACTOR", "TaskQueue", "load_factor", "FX-QUEUE-BOTTOM-ROW", 0,
            "scalar_field_svg", encode_scalar(0.5),
        )
        self.assertEqual(document["value_hash"], compute_value_hash(document))
        self.assertEqual(document["canonicalization"]["rule"], CANONICALIZATION_RULE)


class ResultKindTest(unittest.TestCase):
    def test_scalar(self):
        result = encode_scalar(0.375)
        self.assertEqual(values_of(result), ["0.375"])
        self.assertEqual(compute_value_domain(result)["distinct_values"], 1)

    def test_series_order_is_significant(self):
        first = encode_series([("a", 1.0), ("b", 2.0)])
        second = encode_series([("b", 2.0), ("a", 1.0)])
        self.assertNotEqual(json.dumps(first), json.dumps(second))

    def test_grid_cells_are_sorted(self):
        result = encode_grid(2, 2, [(1, 1, 4.0), (0, 1, 2.0), (0, 0, 1.0), (1, 0, 3.0)])
        self.assertEqual(
            [(cell["row"], cell["column"]) for cell in result["cells"]],
            [(0, 0), (0, 1), (1, 0), (1, 1)],
        )

    def test_a_repeated_cell_is_refused(self):
        with self.assertRaises(WitnessResultError):
            encode_grid(2, 2, [(0, 0, 1.0), (0, 0, 2.0)])

    def test_a_cell_outside_the_grid_is_refused(self):
        with self.assertRaises(WitnessResultError):
            encode_grid(1, 2, [(0, 5, 1.0)])

    def test_a_sparse_grid_is_allowed(self):
        # A feature defined only along a corridor has cells only there --
        # which is what makes coverage_region a statement about the
        # result rather than about the picture.
        result = encode_grid(3, 3, [(2, 0, 1.0), (2, 1, 2.0)])
        self.assertEqual(len(result["cells"]), 2)


class ValueDomainTest(unittest.TestCase):
    def test_the_domain_is_compared_numerically_not_lexically(self):
        # "10" < "9" as strings; a domain computed that way would make a
        # degeneracy check nonsense.
        domain = compute_value_domain(encode_grid(1, 2, [(0, 0, 9.0), (0, 1, 10.0)]))
        self.assertEqual(domain["minimum"], "9")
        self.assertEqual(domain["maximum"], "10")

    def test_a_constant_result_has_one_distinct_value(self):
        domain = compute_value_domain(encode_grid(1, 3, [(0, 0, 1.0), (0, 1, 1.0), (0, 2, 1.0)]))
        self.assertEqual(domain["distinct_values"], 1)

    def test_a_non_canonical_value_is_refused(self):
        with self.assertRaises(WitnessResultError):
            compute_value_domain({"kind": "scalar", "value": "0.10"})

    def test_a_non_canonically_encoded_value_is_refused_even_if_decimal_shaped(self):
        # "1e1" passes is_canonical_decimal's own pattern -- it must
        # still be refused, since canonical_number() would never itself
        # produce it for the value ten.
        with self.assertRaises(WitnessResultError):
            compute_value_domain({"kind": "scalar", "value": "1e1"})

    def test_distinct_values_counts_numbers_not_spellings(self):
        # External review, high severity, reproduced exactly this way: a
        # constant grid with half its cells spelled "10" and half spelled
        # "1e1" reported distinct_values: 2 before this fix, letting a
        # future degeneracy check (#31) miss real degeneracy.
        constant = encode_grid(1, 2, [(0, 0, 10.0), (0, 1, 10.0)])
        constant["cells"][1]["value"] = "1e1"
        with self.assertRaises(WitnessResultError):
            # Now refused outright at the encoding-check step -- a
            # canonical result cannot carry two spellings of one number
            # at all, which is the stronger and more direct fix.
            compute_value_domain(constant)


class IsCanonicallyEncodedTest(unittest.TestCase):
    """The round-trip check is_canonical_decimal's shape-only pattern
    cannot provide on its own."""

    def test_the_shortest_spelling_is_accepted(self):
        self.assertTrue(is_canonically_encoded("10"))
        self.assertTrue(is_canonically_encoded("0.125"))
        self.assertTrue(is_canonically_encoded("0"))

    def test_a_redundant_exponent_is_refused(self):
        # "1e1" is shaped like a decimal (is_canonical_decimal accepts
        # it) but is not what canonical_number() would produce for ten.
        self.assertTrue(is_canonical_decimal("1e1"))
        self.assertFalse(is_canonically_encoded("1e1"))

    def test_a_genuine_exponent_spelling_is_accepted(self):
        # canonical_number() itself uses an exponent for magnitudes repr
        # would -- confirm the round-trip check accepts its own output.
        encoded = canonical_number(1e300)
        self.assertIn("e", encoded)
        self.assertTrue(is_canonically_encoded(encoded))

    def test_every_encoding_canonical_number_produces_round_trips(self):
        for value in (
            0, 1, -1, 0.5, -0.5, 1e300, 1e-300, 2 ** 53, 1 / 7, 3.0,
            # External review, high severity: the boundary immediately
            # past 2**53, where float64 can no longer represent every
            # integer exactly. canonical_number()'s own int branch (see
            # DecimalInputTest and IntegerPrecisionTest below) preserves
            # these exactly; the round-trip check must not silently
            # reparse the resulting digit string through float() and
            # reject what canonical_number() itself just produced.
            2 ** 53 + 1, 2 ** 64, 10 ** 30 + 1, -(2 ** 53 + 1),
        ):
            with self.subTest(value=value):
                self.assertTrue(is_canonically_encoded(canonical_number(value)))

    def test_a_non_decimal_string_is_refused(self):
        self.assertFalse(is_canonically_encoded("not-a-number"))
        self.assertFalse(is_canonically_encoded("0.10"))

    def test_nan_and_infinity_spellings_are_refused(self):
        self.assertFalse(is_canonically_encoded("inf"))
        self.assertFalse(is_canonically_encoded("nan"))


class IntegerPrecisionTest(unittest.TestCase):
    """canonical_number()'s `int` branch preserves an integer exactly
    beyond float64's ~2**53 precision ceiling; is_canonically_encoded()
    must verify that against the SAME int-valued reparse, not against a
    float() reparse that would silently round it first.

    External review, high severity: reproduced with
    canonical_number(2**53 + 1) == "9007199254740993", which
    is_canonically_encoded then rejected -- float("9007199254740993")
    rounds to 9007199254740992.0 (2**53, not 2**53 + 1), a different
    number, so the round-trip comparison failed against output
    canonical_number() had only just produced."""

    def test_the_first_integer_float64_cannot_represent_exactly(self):
        value = 2 ** 53 + 1
        encoded = canonical_number(value)
        self.assertEqual(encoded, "9007199254740993")
        self.assertNotEqual(float(encoded), value)  # confirms float() really does round it
        self.assertTrue(is_canonically_encoded(encoded))

    def test_a_negative_integer_past_the_boundary(self):
        encoded = canonical_number(-(2 ** 53 + 1))
        self.assertTrue(is_canonically_encoded(encoded))

    def test_a_much_larger_integer(self):
        encoded = canonical_number(10 ** 30 + 1)
        self.assertEqual(encoded, "1000000000000000000000000000001")
        self.assertTrue(is_canonically_encoded(encoded))

    def test_2_pow_53_itself_is_still_accepted(self):
        # The exact boundary value, representable in both branches --
        # confirms the fix did not merely shift where it breaks.
        self.assertTrue(is_canonically_encoded(canonical_number(2 ** 53)))

    def test_a_witness_result_over_a_large_integer_builds_successfully(self):
        # The reviewer's own end-to-end reproduction:
        # build_result(..., encode_scalar(2**53 + 1)) raised.
        document = build_result(
            "W-X", "X", "y", "FX-1", 0, "scalar_svg", encode_scalar(2 ** 53 + 1)
        )
        self.assertEqual(document["result"]["value"], "9007199254740993")
        self.assertEqual(document["value_hash"], compute_value_hash(document))


class DecimalInputTest(unittest.TestCase):
    """canonical_number()'s Decimal convenience path -- correct to
    downcast to f64 (this scheme is scoped to f64-precision values), but
    must not silently turn a nonzero value into exactly zero."""

    def test_an_ordinary_decimal_downcasts_to_its_nearest_float(self):
        # Precision loss beyond ~17 significant figures is the downcast
        # working as designed, not a bug -- must not be over-corrected
        # into rejecting the common case.
        self.assertEqual(canonical_number(Decimal("0.1")), "0.1")
        self.assertEqual(canonical_number(Decimal("10")), "10")

    def test_underflow_to_zero_is_refused(self):
        # External review, high severity: float(Decimal("1e-400")) is
        # exactly 0.0 with no exception -- a genuinely nonzero value
        # silently became a different value, not merely a less precise
        # spelling of the same one.
        with self.assertRaises(WitnessResultError):
            canonical_number(Decimal("1e-400"))

    def test_a_decimal_that_is_genuinely_zero_is_accepted(self):
        self.assertEqual(canonical_number(Decimal("0")), "0")
        self.assertEqual(canonical_number(Decimal("0.0")), "0")

    def test_overflow_is_still_refused_via_the_existing_infinity_check(self):
        # float(Decimal("1e400")) is +inf; already caught by the
        # pre-existing isinf check, confirmed here so the two failure
        # modes (overflow, underflow) are both pinned in one place.
        with self.assertRaises(WitnessResultError):
            canonical_number(Decimal("1e400"))


class HashedFieldsTest(unittest.TestCase):
    """canonicalization.hashed_fields is checked against the rule's own
    fixed set, not read as a per-document instruction -- see
    canonical_payload's own docstring."""

    def test_the_declared_set_matching_the_rule_is_accepted(self):
        document = build_result(
            "W-X", "X", "y", "FX-1", 0, "scalar_svg", encode_scalar(0.5)
        )
        self.assertEqual(set(document["canonicalization"]["hashed_fields"]), set(HASHED_FIELDS))
        # canonical_payload succeeds silently -- no exception.
        canonical_payload(document)

    def test_a_narrower_declared_set_is_refused(self):
        # External review, medium severity, reproduced exactly this way:
        # a document claiming hashed_fields: ["result"] previously passed
        # validation even though value_hash was computed over all six
        # fields -- defeating the self-describing hash contract.
        document = build_result(
            "W-X", "X", "y", "FX-1", 0, "scalar_svg", encode_scalar(0.5)
        )
        document["canonicalization"]["hashed_fields"] = ["result"]
        with self.assertRaises(WitnessResultError):
            canonical_payload(document)

    def test_a_reordered_but_equal_set_is_still_accepted(self):
        # Order is documentary only -- canonical_payload's own output is
        # always key-sorted regardless of hashed_fields' array order.
        document = build_result(
            "W-X", "X", "y", "FX-1", 0, "scalar_svg", encode_scalar(0.5)
        )
        document["canonicalization"]["hashed_fields"] = list(reversed(HASHED_FIELDS))
        canonical_payload(document)  # no exception


class StandInProducerTest(unittest.TestCase):
    """The producer is a visible stand-in; the machinery it feeds is real."""

    def run_producer(self, *args) -> dict:
        completed = subprocess.run(
            [sys.executable, str(PRODUCER), *args], capture_output=True, text=True, check=True
        )
        return json.loads(completed.stdout)

    def test_it_emits_a_result_whose_hash_checks_out(self):
        document = self.run_producer()
        self.assertEqual(document["value_hash"], compute_value_hash(document))
        self.assertEqual(document["value_domain"], compute_value_domain(document["result"]))

    def test_it_is_deterministic_across_runs(self):
        self.assertEqual(self.run_producer()["value_hash"], self.run_producer()["value_hash"])

    def test_a_constant_run_collapses_the_domain(self):
        # What a degeneracy check (#31) will read: distinct_values == 1
        # against a declared value_distribution of must-vary.
        document = self.run_producer("--constant")
        self.assertEqual(document["value_domain"]["distinct_values"], 1)
        self.assertNotEqual(document["value_hash"], self.run_producer()["value_hash"])

    def test_a_degraded_renderer_reports_itself(self):
        document = self.run_producer("--renderer", "text_table_strip")
        self.assertEqual(document["renderer_actual"], "text_table_strip")


if __name__ == "__main__":
    unittest.main()
