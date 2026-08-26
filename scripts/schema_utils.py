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
