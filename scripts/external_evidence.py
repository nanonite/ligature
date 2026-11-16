#!/usr/bin/env python3
"""Generic, versioned external-evidence extension point (chainlink #171).

Ligature's first-class assurance evidence kinds are a closed set: Creusot and
Verus deductive checks and Kani bounded model checks.  That is the right
default -- each has a reader that re-opens the verifier's own result -- but it
left no sanctioned way to integrate an evidence tool such as dudect constant
time testing.  Adding a tool by widening the closed enum would make every
call site responsible for knowing the tool and would blur statistical
evidence into the deductive vocabulary.

This module is the shared, deliberately small extension point:

* a project descriptor may declare one or more ``external_evidence`` entries,
  each with a stable ``external-<name>`` evidence kind, a pinned tool name,
  version and SHA-256, the command that produces the result, the result
  format version, the claims the tool is allowed to establish, and the
  closure ceiling it can support;
* an interaction reliance or work-package guarantee refers to that kind
  exactly like a built-in evidence kind;
* the result itself is a versioned document
  (``docs/external-evidence-result-schema.json``) that binds the observation
  to one obligation, the tool identity, and the exact source bytes it ran
  against.

The claim vocabulary is disjoint from the deductive claims on purpose.  A
``timing-independence`` or ``side-channel-resistance`` claim can never
satisfy a requirement that asks for ``postcondition-holds`` because
``satisfies()`` compares the claim kind exactly and the two vocabularies do
not overlap.  External evidence is likewise never promoted to a deductive or
bounded closure kind: G14 refuses those declared kinds when any external
evidence participates in the closure and points at the honest ``partial``
kind instead.

This module owns declarations and the result schema only.  Reading a result
belongs to ``record_assurance`` / ``gate_g14`` because those readers already
raise the pipeline's own exit-code-bearing exceptions.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resources  # noqa: E402
from schema_utils import make_validator  # noqa: E402

EXTERNAL_KIND_PREFIX = "external-"
EXTERNAL_KIND_PATTERN = r"^external-[a-z0-9]+(?:-[a-z0-9]+)*$"
EXTERNAL_KIND_RE = re.compile(EXTERNAL_KIND_PATTERN)

#: Claims a project may attach to an external evidence declaration.  These
#: are non-functional properties, deliberately disjoint from the deductive
#: claims below.
EXTERNAL_CLAIM_KINDS = (
    "timing-independence",
    "side-channel-resistance",
)

#: The claims a first-class verifier establishes.  Kept here so policy code
#: can ask whether a reliance still requires owner-verifier evidence without
#: importing the whole verifier module.
DEDUCTIVE_CLAIM_KINDS = (
    "callee-precondition-established",
    "postcondition-holds",
)

#: The strongest closure statement external evidence can contribute to.  A
#: statistical test is not a proof over all inputs (deductive) and is not a
#: bounded-exhaustive model check (bounded); an observation is weaker still.
EXTERNAL_CLOSURE_CEILINGS = (
    "statistical",
    "observational",
)

EXTERNAL_RESULT_FORMAT = "ligature-external-evidence-result/1.0"
EXTERNAL_RESULT_SCHEMA_PATH = resources.resource_path(
    "docs", "external-evidence-result-schema.json"
)

BUILTIN_EVIDENCE_KINDS = frozenset(
    {
        "kani-bounded-model-check",
        "creusot-deductive-check",
        "verus-deductive-check",
    }
)


def is_external_evidence_kind(value: object) -> bool:
    """True for a well-formed ``external-<name>`` evidence kind."""
    return isinstance(value, str) and bool(EXTERNAL_KIND_RE.fullmatch(value))


def declarations(descriptor: object) -> list[dict]:
    """The descriptor's external evidence declarations, or [] when absent.

    Shape errors are JSON Schema's job (``external_evidence_diagnostics``
    still names duplicate kinds, which a schema cannot express); this helper
    only returns entries that are mappings so callers never crash on a
    partially invalid descriptor.
    """
    value = descriptor.get("external_evidence") if isinstance(descriptor, dict) else None
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def declarations_by_kind(descriptor: object) -> dict[str, dict]:
    return {
        entry["kind"]: entry
        for entry in declarations(descriptor)
        if isinstance(entry.get("kind"), str)
    }


def external_kinds(descriptor: object) -> set[str]:
    return set(declarations_by_kind(descriptor))


def declaration_for(descriptor: object, kind: str) -> dict | None:
    return declarations_by_kind(descriptor).get(kind)


def declaration_diagnostics(descriptor: object) -> list[str]:
    """Project-level constraints a JSON Schema cannot state.

    ``kind`` is the identity a reliance references, so a second declaration
    with the same kind would make which tool/result is meant depend on list
    order.  A declaration may also not reuse a built-in evidence kind: that
    vocabulary belongs to the first-class readers and must stay disjoint so
    a declaration cannot override what ``creusot-deductive-check`` means.
    """
    entries = declarations(descriptor)
    diagnostics: list[str] = []
    seen: dict[str, int] = {}
    for index, entry in enumerate(entries):
        kind = entry.get("kind")
        if not isinstance(kind, str) or not kind:
            continue  # JSON Schema reports the missing/typed field.
        if kind in BUILTIN_EVIDENCE_KINDS:
            diagnostics.append(
                f"external_evidence[{index}].kind {kind!r} is a built-in evidence kind; "
                "external tools must use the external-<name> namespace"
            )
        seen[kind] = seen.get(kind, 0) + 1
    for kind, count in seen.items():
        if count > 1:
            diagnostics.append(
                f"external_evidence declares evidence kind {kind!r} {count} times; "
                "a reliance references one declaration by kind, so it must be unique"
            )
    return diagnostics


def load_result_schema() -> dict:
    return json.loads(EXTERNAL_RESULT_SCHEMA_PATH.read_text())


def load_result_validator():
    return make_validator(load_result_schema())
