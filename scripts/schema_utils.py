#!/usr/bin/env python3
"""Shared JSON Schema validator construction.

Centralized because `jsonschema.Draft202012Validator(schema)` does NOT
enforce the `format` keyword unless a format_checker is explicitly passed
-- draft 2020-12 treats format as annotation-only by default. An earlier
version of this codebase constructed validators ad hoc at 7 call sites
across scripts/ and tests/, none of them passing a format_checker, so
reviewed_at: "not-a-date" silently validated everywhere (external review
finding, medium severity). One helper, used everywhere, so a new call site
can't reintroduce the same gap by omission.
"""
from __future__ import annotations

from jsonschema import Draft202012Validator


def make_validator(schema: dict) -> Draft202012Validator:
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def make_validator_without_required(schema: dict, *fields: str) -> Draft202012Validator:
    """Same as make_validator(), but treats each name in `fields` as
    schema-optional regardless of the source schema's own `required`
    list -- recursively, everywhere a `required` array appears (top
    level, and inside any `if`/`then`/`allOf`/`$defs`/etc.), not just at
    the schema's root.

    Built for Stage 0/3 draft-time validation: a freshly generated draft
    correctly lacks `review` -- review_checkpoint.approve() is the only
    thing that attaches one, after a human reviewer signs off (see its
    own docstring) -- so checking the rest of the schema without
    tripping on that one intentionally-absent field is different from
    the schema being wrong. The recursive walk matters for schemas like
    docs/conflict-resolution-schema.json, where status == "resolved"
    triggers a nested `if/then` with its OWN `required: [resolution,
    review]` -- stripping only the top-level `required` array would
    leave that conditional still rejecting a review-less resolved
    draft."""
    def _strip(node):
        if isinstance(node, dict):
            node = dict(node)
            if isinstance(node.get("required"), list):
                node["required"] = [f for f in node["required"] if f not in fields]
            for key, value in node.items():
                node[key] = _strip(value)
            return node
        if isinstance(node, list):
            return [_strip(item) for item in node]
        return node

    return make_validator(_strip(schema))
