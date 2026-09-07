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
    broken fixture produce a perfectly reproducible hash."""
    if isinstance(value, bool):
        raise WitnessResultError("a boolean is not a measured value")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        value = float(value)
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
    return isinstance(text, str) and bool(DECIMAL_RE.match(text))


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
    as decimals, not as strings: "10" < "9" lexically and 10 > 9
    numerically, and a domain computed the first way would make a
    degeneracy check nonsense."""
    values = values_of(result)
    if not values:
        raise WitnessResultError("a result with no values has no domain")
    for value in values:
        if not is_canonical_decimal(value):
            raise WitnessResultError(
                f"{value!r} is not a canonical decimal string -- a canonical result carries no JSON "
                "numbers for measured values, so that hashing never has to format a float"
            )
    numeric = sorted(values, key=Decimal)
    return {
        "distinct_values": len(set(values)),
        "minimum": numeric[0],
        "maximum": numeric[-1],
    }


def canonical_payload(document: dict) -> str:
    """`canonical-json-v1`: sorted keys, no whitespace, exactly the hashed
    fields. Returned as text rather than bytes so a caller can show a
    reviewer precisely what was hashed."""
    missing = [field for field in HASHED_FIELDS if field not in document]
    if missing:
        raise WitnessResultError(f"cannot canonicalize: missing {missing!r}")
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
