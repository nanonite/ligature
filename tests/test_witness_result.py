"""The canonical witness result: encoding, serialization, hashing (#27).

plan.md §16.1's reason for existing is that SVG bytes move when the value
does not. These tests pin the other half of that argument: the canonical
result moves when — and only when — the value does.
"""
import json
import subprocess
import sys
import unittest
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
