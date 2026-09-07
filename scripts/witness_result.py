#!/usr/bin/env python3
"""The canonical witness result: encoding, serialization, and value_hash.

plan.md §16.1, chainlink #27. This is the artifact the determinism
contract is actually about, and until now it did not exist: "the renderer
does not currently emit a canonical numeric result separate from the
SVG."

Why a separate artifact at all
------------------------------
§16.1: "SVG serialization determinism is fragile -- float-to-string
formatting, locale, path-coordinate rounding, attribute ordering, font
metrics, and renderer-library version all perturb the bytes with no
change to the computed value." Hashing the picture would couple a
promotion receipt to rendering incidentals. So `value_hash` is taken over
a canonical RESULT: the numbers the query produced on the fixture, with
no rendering in them at all.

The float rule -- the load-bearing decision
-------------------------------------------
A canonical result contains **no JSON numbers for measured values**.
Every measured value is a decimal STRING (docs/witness-result-schema.json's
`decimal`), produced by whatever computed it. The consequence is that
hashing never formats a float: it hashes strings that are already fixed.

That matters because float formatting is exactly the platform-dependent
step §16.1 warns about, and a canonicalizer that formats floats would
pull it straight back into the normative hash -- the same hazard, one
layer down, and much harder to see. Confining formatting to the producer
leaves it where G19 (chainlink #30) can catch it: regenerate, compare the
hash. A producer whose formatting drifts fails a determinism gate, which
is a true statement about that producer, instead of silently changing a
normative hash.

`canonical_number()` below is the reference encoder for a Python
producer: shortest round-trip for f64 (`repr`, which is
correctly-rounded and round-trips exactly), then normalization -- one
spelling of zero, no '+' in exponents, no leading zeros in exponents, no
trailing '.0' ambiguity. A producer in another language must emit the
same strings for the same doubles; the thing that proves it does is G19,
not a comment.

Serialization (`canonical-json-v1`)
-----------------------------------
JSON, UTF-8, keys sorted, `(',', ':')` separators, no trailing newline,
over exactly the fields the artifact names in `canonicalization.hashed_fields`:
witness_id, concept, query, fixture_id, seed, result. Identity of the
fact plus the fact. Excluded: renderer_actual, value_domain, paths, and
anything else describing HOW the value was obtained or shown -- the same
numbers from a different renderer are the same fact and must hash the
same; the same numbers from a different fixture are a different fact and
must not.

Boundary
--------
This module computes and checks; it does not gate and does not render.
  * Comparing a result's hash to a witness spec's declared
    `determinism.value_hash` is G19's job (chainlink #30).
  * Refusing a degraded renderer at generation time, and recording
    `renderer_actual`, is the renderer contract (chainlink #28) -- this
    module defines the FIELD and carries it through so #28 has something
    to write and #31 has something to compare.
  * Degeneracy against the declared expectation is G20 (chainlink #31),
    which reads `value_domain.distinct_values` from here.
Same line scripts/validate_closure.py draws against scripts/gate_g14.py.

There is no renderer in this repository, and this module does not pretend
otherwise: `produce_result()` takes the values from a producer (a fixture
evaluation) and canonicalizes them. tests/fixtures/witnesses/ carries a
visible stand-in producer, the same precedent as #47's fake_verifier.py
and #24's never-compiled Rust.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from decimal import Decimal
from pathlib import Path

CANONICALIZATION_RULE = "canonical-json-v1"
HASHED_FIELDS = ("witness_id", "concept", "query", "fixture_id", "seed", "result")

# No trailing zeros in the fraction: "0.10" and "0.1" are the same number,
# and two spellings of one number would hash differently.
DECIMAL_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?(?:e-?[1-9][0-9]*)?$")

RESULT_DIR = ("ci", "results", "witnesses")


class WitnessResultError(Exception):
    pass


def witness_result_dir_for(workspace: Path) -> Path:
    return workspace.joinpath(*RESULT_DIR).resolve()


def canonical_number(value) -> str:
    """Encode one measured value as a canonical decimal string.

    Reference implementation of docs/witness-result-schema.json's
    `decimal`. `repr` of a Python float is the shortest string that
    round-trips exactly, which is the only float spelling that is both
    lossless and stable; everything after that is normalization so two
    encoders cannot disagree about spelling the same number:

      * one spelling of zero ("0", never "-0" or "0.0");
      * no exponent unless repr produced one, and then no '+' and no
        leading zeros in it;
      * integral floats lose the trailing ".0" so 3.0 and 3 encode
        identically -- they are the same value, and a hash that told
        them apart would report a difference nobody made.

    NaN and ±Infinity are refused outright: a witness whose value is not
    a number has no stable value to pin, and encoding one would let a
    broken fixture produce a perfectly reproducible hash.

    A `Decimal` argument is downcast to the nearest float64 -- correct
    and intentional, since this scheme is scoped to f64-precision
    measured values (plan.md §16.1: a real query's own Rust signature
    returns `f64`), not arbitrary-precision decimals. Losing digits
    beyond float64's ~17 significant figures is that downcast working as
    designed. What is NOT designed, and was silent before this check
    existed (external review, high severity): `float()` does not raise
    on a magnitude float64 cannot represent at all -- it returns ±inf on
    overflow (already caught below, since inf has no canonical form) and
    exactly `0.0` on underflow, with nothing to tell a genuinely nonzero
    Decimal apart from a real zero. `Decimal('1e-400')` silently became
    the string `"0"`, indistinguishable from a witness whose true value
    is zero -- a different value than the one actually computed, not
    merely a less precise spelling of it."""
    if isinstance(value, bool):
        raise WitnessResultError("a boolean is not a measured value")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        as_float = float(value)
        if value != 0 and as_float == 0.0:
            raise WitnessResultError(
                f"{value} underflows to 0.0 as an f64 -- silently encoding a nonzero value as "
                "exactly zero would misrepresent what was actually computed, not just lose precision"
            )
        value = as_float
    if not isinstance(value, float):
        raise WitnessResultError(f"cannot encode {value!r} ({type(value).__name__}) as a measured value")
    if math.isnan(value) or math.isinf(value):
        raise WitnessResultError(
            f"{value!r} has no canonical decimal form -- a witness whose value is not a number has "
            "no stable value to pin"
        )
    if value == 0:
        return "0"

    text = repr(value)
    mantissa, _, exponent = text.partition("e")
    if mantissa.endswith(".0"):
        mantissa = mantissa[:-2]
    if not exponent:
        return mantissa
    sign = "-" if exponent.startswith("-") else ""
    digits = exponent.lstrip("+-").lstrip("0") or "0"
    if digits == "0":
        return mantissa
    return f"{mantissa}e{sign}{digits}"


def is_canonical_decimal(text: str) -> bool:
    """Shape only: does `text` look like a canonical decimal at all. Not
    sufficient on its own -- see `is_canonically_encoded`, which this
    codebase's actual validation uses."""
    return isinstance(text, str) and bool(DECIMAL_RE.match(text))


def is_canonically_encoded(text: str) -> bool:
    """Whether `text` is EXACTLY what `canonical_number()` would produce
    for its own numeric value -- not merely shaped like a decimal.

    `is_canonical_decimal` alone accepts "1e1" for the value ten, which
    `canonical_number()` itself would spell "10" (its float `repr` never
    needs an exponent for so small a magnitude). Two producers computing
    the identical float could then emit two different, individually
    pattern-valid strings for one number -- silently breaking "the same
    fact hashes the same," the whole point of canonicalization, and (one
    layer down) letting `distinct_values` overcount, since a set of raw
    strings sees "10" and "1e1" as different elements even though they
    are one value (external review, high severity: reproduced with a
    constant grid, half its cells spelled the shortest way and half
    spelled with a redundant exponent, `distinct_values: 2`).

    This is a round-trip check, not a second, looser pattern: parse
    `text` back to the value it denotes and ask whether re-encoding that
    value reproduces `text` verbatim. A string a producer's own
    `canonical_number()` actually emitted always round-trips -- that is
    what "shortest string that round-trips exactly" means -- so this
    never rejects genuine output, only a spelling nothing in this
    codebase would have produced.

    The parse must match `canonical_number()`'s OWN dispatch, not always
    go through `float`: a plain integer spelling (no '.', no 'e') is
    reparsed as an `int`, the same branch `canonical_number()` itself
    takes for integer input, which is exactly why that branch exists --
    to preserve an integer exactly beyond float64's ~2**53 precision
    ceiling. Reparsing such a string through `float()` unconditionally
    would silently round it before the comparison ever ran, incorrectly
    rejecting exact integer output `canonical_number()` legitimately
    produced (external review, high severity: reproduced with
    `canonical_number(2**53 + 1) == "9007199254740993"`, which this
    function then rejected because `float("9007199254740993")` rounds to
    `9007199254740992.0`, a different number)."""
    if not is_canonical_decimal(text):
        return False
    if "." not in text and "e" not in text:
        try:
            value = int(text)
        except ValueError:
            return False
    else:
        try:
            value = float(text)
        except (ValueError, OverflowError):
            return False
    try:
        return canonical_number(value) == text
    except WitnessResultError:
        return False


def values_of(result: dict) -> list[str]:
    """Every measured value in a result, in document order -- the order
    that is also hashed, so a producer emitting them in a different order
    has a determinism defect rather than a formatting one."""
    kind = result.get("kind")
    if kind == "scalar":
        return [result["value"]]
    if kind == "series":
        return [point["value"] for point in result["points"]]
    if kind == "grid":
        return [cell["value"] for cell in result["cells"]]
    raise WitnessResultError(f"unknown result kind {kind!r}")


def compute_value_domain(result: dict) -> dict:
    """Recomputed on every validation, never trusted as stored. Compared
    as decimals, not as strings throughout -- for ordering ("10" < "9"
    lexically and 10 > 9 numerically, and a domain computed the first way
    would make a degeneracy check nonsense) and, just as much, for
    DISTINCTNESS: "10" and "1e1" are two strings and one value, and
    `distinct_values` must count the value (external review, high
    severity -- see `is_canonically_encoded`'s own docstring for the
    reproduction). Requiring every value to be canonically encoded, not
    merely decimal-shaped, is what makes counting distinct STRINGS safe
    at all: once every value is provably the one spelling its number has,
    a set of the spellings and a set of the numbers are the same count,
    and this still compares by Decimal explicitly rather than leaning on
    that equivalence, so the guarantee is not silently lost if the
    encoding check above it ever loosens."""
    values = values_of(result)
    if not values:
        raise WitnessResultError("a result with no values has no domain")
    for value in values:
        if not is_canonically_encoded(value):
            raise WitnessResultError(
                f"{value!r} is not canonically encoded -- either not a canonical decimal string at "
                "all, or a spelling canonical_number() would never itself produce for that value "
                "(e.g. '1e1' for ten, which canonical_number() spells '10') -- a canonical result "
                "carries no JSON numbers for measured values, and admitting a second valid spelling "
                "of one number defeats that discipline just as surely as a JSON number would"
            )
    numeric = sorted(values, key=Decimal)
    return {
        "distinct_values": len({Decimal(value) for value in values}),
        "minimum": numeric[0],
        "maximum": numeric[-1],
    }


def canonical_payload(document: dict) -> str:
    """`canonical-json-v1`: sorted keys, no whitespace, exactly the hashed
    fields. Returned as text rather than bytes so a caller can show a
    reviewer precisely what was hashed.

    `document["canonicalization"]["hashed_fields"]`, when present, is not
    a per-document CHOICE of which fields to hash -- rule
    `canonical-json-v1` hashes exactly `HASHED_FIELDS`, always, and the
    declared list exists so a future rule change is a visible version
    bump, never a silent re-hash (this module's own docstring). So the
    declared list is CHECKED against the one true set this rule hashes,
    not read as an instruction: hashing a document's declared subset
    instead of the real set would let it claim a smaller footprint than
    what its `value_hash` was actually computed over (external review,
    medium severity -- reproduced with a document declaring
    `hashed_fields: ["result"]` while `value_hash` was, in truth, over
    all six fields, defeating the entire point of recording the list).
    Order is not part of the comparison -- the payload below is
    key-sorted regardless of what order `hashed_fields` names them in,
    so a reordering is not a meaningfully different set."""
    missing = [field for field in HASHED_FIELDS if field not in document]
    if missing:
        raise WitnessResultError(f"cannot canonicalize: missing {missing!r}")
    declared = (document.get("canonicalization") or {}).get("hashed_fields")
    if declared is not None and set(declared) != set(HASHED_FIELDS):
        raise WitnessResultError(
            f"canonicalization.hashed_fields {declared!r} does not match what this rule actually "
            f"hashes ({list(HASHED_FIELDS)!r})"
        )
    payload = {field: document[field] for field in HASHED_FIELDS}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_value_hash(document: dict) -> str:
    return "sha256:" + hashlib.sha256(canonical_payload(document).encode("utf-8")).hexdigest()


def build_result(
    witness_id: str,
    concept: str,
    query: str,
    fixture_id: str,
    seed: int,
    renderer_actual: str,
    result: dict,
) -> dict:
    """Assemble a canonical result artifact, with its domain and hash
    computed -- never accepted from a caller. A producer supplies the
    values; the hash is this module's to derive, so no producer can hand
    over numbers and a hash that do not correspond."""
    document = {
        "schema_version": "1.0",
        "witness_id": witness_id,
        "concept": concept,
        "query": query,
        "fixture_id": fixture_id,
        "seed": seed,
        "renderer_actual": renderer_actual,
        "canonicalization": {"rule": CANONICALIZATION_RULE, "hashed_fields": list(HASHED_FIELDS)},
        "result": result,
        "value_domain": compute_value_domain(result),
    }
    document["value_hash"] = compute_value_hash(document)
    return document


def encode_scalar(value) -> dict:
    return {"kind": "scalar", "value": canonical_number(value)}


def encode_series(points) -> dict:
    return {
        "kind": "series",
        "points": [{"key": key, "value": canonical_number(value)} for key, value in points],
    }


def encode_grid(rows: int, columns: int, cells) -> dict:
    """`cells` is an iterable of (row, column, value). Sorted here because
    the schema requires sorted, unique cells: an unsorted grid would make
    the hash depend on the producer's iteration order, and two values for
    one cell has no meaning."""
    encoded = []
    seen = set()
    for row, column, value in cells:
        if (row, column) in seen:
            raise WitnessResultError(f"cell ({row}, {column}) appears more than once")
        seen.add((row, column))
        if not (0 <= row < rows and 0 <= column < columns):
            raise WitnessResultError(f"cell ({row}, {column}) is outside a {rows}x{columns} grid")
        encoded.append({"row": row, "column": column, "value": canonical_number(value)})
    encoded.sort(key=lambda cell: (cell["row"], cell["column"]))
    return {"kind": "grid", "rows": rows, "columns": columns, "cells": encoded}
