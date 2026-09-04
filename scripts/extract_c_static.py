#!/usr/bin/env python3
"""Coarse C_static extraction: the minimum viable realized-call extractor.

plan.md §9, chainlink #24. Produces one docs/callsite-schema.json report
per crate: the call sites discovered in that crate's sources, each with a
source-level method identity for the caller, a stable call-site location,
and an HONEST resolution class.

What this is, exactly
---------------------
A *syntactic* extractor: a Rust lexer (comments, all string/char/raw/byte
literal forms, lifetimes) followed by a brace-depth structural pass that
tracks `impl`/`fn` context, then a token-level classification of each call
expression. It is deliberately NOT a regex over raw source -- a regex
cannot tell `foo(` inside a string literal from a real call, cannot pair
`impl` with the block it opens, and cannot walk back over a turbofish.
plan.md §9's coarse *compiler-backed* extractor, and MIR after it, remain
future work for release-critical clusters or disputed edges.

Because it has no name resolution and no types, it never claims soundness:
every report it writes carries `extractor_backing: syntactic` and
`completeness_claim: discovered-lower-bound`, which the schema itself
enforces as a pair (chainlink #24: "no regex-only completeness claim").
The reporting rule that follows from that is plan.md §9's own: **"all
discovered call sites resolved"**, never "all call sites resolved" -- see
scripts/gate_r1_g16.py, which owns the reporting side.

Scoping: C is a relation between *local concept methods*, so the concept
universe is every type with an `impl` block in the scanned sources
(recorded in attribution_scope.local_concepts). A call that cannot reach
any of those -- `HashMap::new()`, a free function, another crate's API --
is out of C's domain and is counted in out_of_scope_call_sites, not
tiered as an unresolved edge. Tiering every std call `medium` would bury
the real dispatch risk in noise, which is the same paralysis plan.md §9.1
rejects a blanket `unresolved > 0 -> block` for.

Classification (plan.md §9's three classes, exactly):

  definite-direct-call    `Type::m(..)` / `Self::m(..)` / `self.m(..)`
                          where the concept is local and owns `m`.
  possible-dispatch       `expr.m(..)` where `m` is a local concept
                          method name but the receiver's type is unknown.
                          The method name is recorded; a concept is NOT
                          invented for it.
  unresolved-indirect-call `f(..)` / `expr(..)` where the callee cannot be
                          named at all -- a bare binding is
                          indistinguishable from a fn pointer or closure
                          without name resolution.

Risk tiers: the extractor only ever emits `medium` with
`risk_tier_source: extractor-default`. It has no basis for critical/high,
and emitting `low` would silently accept a limitation nobody looked at.
Moving a tier is a human decision carrying its own review block; `--previous`
carries such human tiers across a re-extraction by callsite_id, which is
deterministic for unchanged source.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

EXTRACTOR_NAME = "coarse-syntactic-callgraph"
EXTRACTOR_VERSION = "0.1.0"
EXTRACTOR_BACKING = "syntactic"
COMPLETENESS_CLAIM = "discovered-lower-bound"

SUPPORTED_CALL_FORMS = [
    "associated-path-call",
    "self-method-call",
    "inherent-method-call-by-name",
]
UNSUPPORTED_CALL_FORMS = [
    "macro-expanded-call",
    "generated-code",
    "ffi-callback",
    "call-to-non-local-type",
]
SCANNED_ITEM_KINDS = ["inherent-impl-method", "trait-impl-method"]

DEFAULT_RISK_TIER = "medium"

CONCEPT_RE = re.compile(r"^[A-Z][A-Za-z0-9]*$")
METHOD_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Idents that can precede `(` without being a call.
NON_CALL_IDENTS = {
    "if", "while", "for", "match", "return", "in", "as", "else", "let",
    "loop", "move", "unsafe", "where", "fn", "impl", "struct", "enum",
    "trait", "mod", "use", "pub", "const", "static", "type", "ref",
    "break", "continue", "yield", "await", "dyn", "and", "or", "not",
}


class ExtractionError(Exception):
    pass


# --------------------------------------------------------------------------
# Lexing
# --------------------------------------------------------------------------

def strip_noncode(src: str) -> str:
    """Blank out comments and every literal form, preserving length and
    newlines so line numbers and offsets stay exact.

    This is the step a regex-only extractor skips, and the reason it
    cannot make an honest completeness statement: `"foo(bar)"` inside a
    string, a `//` commented-out call, and a `'a` lifetime all look like
    code to a pattern match. Handles: line comments, nested block
    comments, `"..."`, `b"..."`, raw strings with any hash count
    (`r#"..."#`, `br##"..."##`), char and byte-char literals, and
    lifetimes (which are NOT char literals and must not open one)."""
    out = list(src)
    i = 0
    n = len(src)

    def blank(start: int, end: int) -> None:
        for k in range(start, min(end, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            j = n if j == -1 else j
            blank(i, j)
            i = j
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            depth = 1
            j = i + 2
            while j < n and depth:
                if src.startswith("/*", j):
                    depth += 1
                    j += 2
                elif src.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            blank(i, j)
            i = j
            continue
        # raw strings: r"..." / r#"..."# / br##"..."##
        raw = re.match(r"(?:b?r)(#*)\"", src[i:])
        if raw and (i == 0 or not (src[i - 1].isalnum() or src[i - 1] == "_")):
            hashes = raw.group(1)
            terminator = '"' + hashes
            j = src.find(terminator, i + raw.end())
            j = n if j == -1 else j + len(terminator)
            blank(i, j)
            i = j
            continue
        if c == '"' or (c == "b" and i + 1 < n and src[i + 1] == '"'):
            j = i + (1 if c == '"' else 2)
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == '"':
                    j += 1
                    break
                j += 1
            blank(i, j)
            i = j
            continue
        if c == "'" or (c == "b" and i + 1 < n and src[i + 1] == "'"):
            start = i + (1 if c == "'" else 2)
            # A lifetime is `'ident` NOT followed by a closing quote. A
            # char literal is `'x'` or `'\n'`. Getting this backwards
            # swallows arbitrary code between two lifetimes.
            rest = src[start:]
            if src[start:start + 1] == "\\":
                j = start + 2
                while j < n and src[j] != "'":
                    j += 1
                j = min(j + 1, n)
            else:
                m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", rest)
                if m and not rest[m.end():m.end() + 1] == "'":
                    i = start + m.end()  # lifetime: leave it as code
                    continue
                j = start + 1
                if j < n and src[j] == "'":
                    j += 1
                else:
                    i = start
                    continue
            blank(i, j)
            i = j
            continue
        i += 1
    return "".join(out)


@dataclass(frozen=True)
class Token:
    kind: str  # "ident" | "num" | "punct"
    text: str
    line: int
    start: int


_TOKEN_RE = re.compile(
    r"""
      (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<num>[0-9][0-9A-Za-z_.]*)
    | (?P<punct>::|->|=>|\S)
    """,
    re.VERBOSE,
)


def tokenize(code: str) -> list[Token]:
    tokens: list[Token] = []
    line = 1
    pos = 0
    for match in _TOKEN_RE.finditer(code):
        line += code.count("\n", pos, match.start())
        pos = match.start()
        kind = match.lastgroup
        tokens.append(Token(kind, match.group(), line, match.start()))
    return tokens


# --------------------------------------------------------------------------
# Structural pass
# --------------------------------------------------------------------------

@dataclass
class MethodSpan:
    concept: str
    method: str
    sig_start: int  # token index of the `fn` keyword
    start: int  # token index of the body's opening brace
    end: int    # token index of the body's closing brace


def _impl_type(tokens: list[Token], i: int) -> tuple[str | None, int]:
    """Given the index of an `impl` ident, return (base type name, index of
    the token that opens its block, or -1). Skips the generic parameter
    list so `impl<T> Foo<T>` is Foo and not T, and honours a top-level
    `for` so `impl Trait for Foo` is Foo and not Trait."""
    j = i + 1
    angle = 0
    if j < len(tokens) and tokens[j].text == "<":
        angle = 1
        j += 1
        while j < len(tokens) and angle:
            if tokens[j].text == "<":
                angle += 1
            elif tokens[j].text == ">":
                angle -= 1
            elif tokens[j].text == ">>":
                angle -= 2
            j += 1
    header_start = j
    angle = 0
    brace = -1
    for_at = -1
    while j < len(tokens):
        text = tokens[j].text
        if text == "<":
            angle += 1
        elif text == ">":
            angle -= 1
        elif text == "for" and angle == 0 and for_at == -1:
            for_at = j
        elif text == "{" and angle == 0:
            brace = j
            break
        elif text == ";" and angle == 0:
            return None, -1
        j += 1
    if brace == -1:
        return None, -1
    scan_from = for_at + 1 if for_at != -1 else header_start
    name: str | None = None
    k = scan_from
    while k < brace:
        if tokens[k].kind == "ident":
            name = tokens[k].text
            # a path keeps walking (`crate::model::Foo`), a generic stops
            if k + 1 < brace and tokens[k + 1].text == "::":
                k += 2
                continue
            break
        if tokens[k].text in {"&", "mut"} or tokens[k].kind == "punct":
            k += 1
            continue
        k += 1
    return name, brace


def _matching_brace(tokens: list[Token], open_index: int) -> int:
    depth = 0
    for k in range(open_index, len(tokens)):
        if tokens[k].text == "{":
            depth += 1
        elif tokens[k].text == "}":
            depth -= 1
            if depth == 0:
                return k
    return len(tokens) - 1


def find_method_spans(tokens: list[Token]) -> list[MethodSpan]:
    """Every `fn` whose immediately enclosing item is an `impl` block on a
    concept-shaped type. A nested `fn`, a trait's default body, and an
    `impl` on a non-concept-shaped type (`impl Trait for &str`) are all
    deliberately excluded -- their calls are counted as unattributed
    rather than attributed to a method identity that doesn't exist."""
    spans: list[MethodSpan] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.kind == "ident" and token.text == "impl" and (
            i == 0 or tokens[i - 1].text not in {"::", "."}
        ):
            name, brace = _impl_type(tokens, i)
            if brace == -1:
                i += 1
                continue
            end = _matching_brace(tokens, brace)
            if name is not None and CONCEPT_RE.match(name):
                spans.extend(_methods_in_impl(tokens, name, brace + 1, end))
            i = end + 1
            continue
        i += 1
    return spans


def _methods_in_impl(tokens: list[Token], concept: str, start: int, end: int) -> list[MethodSpan]:
    spans: list[MethodSpan] = []
    i = start
    depth = 0
    while i < end:
        text = tokens[i].text
        if text == "{":
            depth += 1
        elif text == "}":
            depth -= 1
        elif depth == 0 and tokens[i].kind == "ident" and text == "fn":
            if i + 1 < end and tokens[i + 1].kind == "ident":
                method = tokens[i + 1].text
                body = _find_body(tokens, i + 2, end)
                if body != -1 and METHOD_RE.match(method):
                    close = _matching_brace(tokens, body)
                    spans.append(MethodSpan(concept, method, i, body, close))
                    i = close
        i += 1
    return spans


def _find_body(tokens: list[Token], start: int, end: int) -> int:
    """The `{` opening this fn's body, or -1 for a bodyless signature (a
    trait requirement) -- whose `;` must not leak into the next block."""
    angle = 0
    paren = 0
    for k in range(start, end):
        text = tokens[k].text
        if text == "(":
            paren += 1
        elif text == ")":
            paren -= 1
        elif text == "<":
            angle += 1
        elif text == ">":
            angle -= 1
        elif text == ";" and paren == 0:
            return -1
        elif text == "{" and paren == 0:
            return k
    return -1


# --------------------------------------------------------------------------
# Call classification
# --------------------------------------------------------------------------

@dataclass
class Counters:
    unattributed: int = 0
    out_of_scope: int = 0
    macros: int = 0


@dataclass
class RawCallsite:
    call_class: str
    caller_concept: str
    caller_method: str
    callee_concept: str | None
    callee_method: str | None
    expression: str
    line: int
    path: str
    syntax_hash: str


@dataclass
class CrateFacts:
    """The concept universe: types with an impl block, and the methods each
    one owns. Built over the whole crate before classification, since a
    call in file A may target a concept defined in file B."""
    concepts: set[str] = field(default_factory=set)
    methods_by_concept: dict[str, set[str]] = field(default_factory=dict)

    @property
    def all_methods(self) -> set[str]:
        return {m for ms in self.methods_by_concept.values() for m in ms}

    def add(self, spans: list[MethodSpan]) -> None:
        for span in spans:
            self.concepts.add(span.concept)
            self.methods_by_concept.setdefault(span.concept, set()).add(span.method)


def _walk_back_turbofish(tokens: list[Token], j: int) -> int:
    """`m::<T>(` -- step back over a balanced generic argument list so the
    method ident, not the `>`, is what gets classified."""
    if j < 0 or tokens[j].text != ">":
        return j
    depth = 0
    k = j
    while k >= 0:
        if tokens[k].text == ">":
            depth += 1
        elif tokens[k].text == "<":
            depth -= 1
            if depth == 0:
                return k - 2 if k >= 2 and tokens[k - 1].text == "::" else k - 1
        k -= 1
    return j


ITEM_KEYWORDS = {"fn", "struct", "enum", "trait", "union", "mod", "macro_rules"}


def attribute_ranges(tokens: list[Token]) -> list[tuple[int, int]]:
    """`#[...]` / `#![...]` spans. Attribute arguments are call-shaped
    (`#[cfg(test)]`, `#[derive(Clone)]`) but are not calls; counting them
    would inflate every honesty counter in the report."""
    ranges: list[tuple[int, int]] = []
    i = 0
    while i < len(tokens):
        if tokens[i].text == "#":
            j = i + 1
            if j < len(tokens) and tokens[j].text == "!":
                j += 1
            if j < len(tokens) and tokens[j].text == "[":
                depth = 0
                k = j
                while k < len(tokens):
                    if tokens[k].text == "[":
                        depth += 1
                    elif tokens[k].text == "]":
                        depth -= 1
                        if depth == 0:
                            break
                    k += 1
                ranges.append((i, k))
                i = k + 1
                continue
        i += 1
    return ranges


def _in_ranges(index: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= index <= end for start, end in ranges)


def count_macro_invocations(tokens: list[Token], attributes: list[tuple[int, int]]) -> int:
    """`name!(..)`, `name![..]`, `name!{..}`. A macro can expand to calls
    that are nowhere in the source token stream, so this count is a real
    blind spot, reported rather than glossed over. `#![..]` is an inner
    attribute, not a macro -- hence the ident requirement."""
    count = 0
    for i in range(1, len(tokens) - 1):
        if tokens[i].text != "!" or _in_ranges(i, attributes):
            continue
        if tokens[i - 1].kind == "ident" and tokens[i + 1].text in {"(", "[", "{"}:
            count += 1
    return count


def _local_bindings(tokens: list[Token], span: MethodSpan) -> set[str]:
    """Names bound as values in this method: parameters and `let` bindings.
    A call on one of those is a call through a value -- a fn pointer, a
    closure, a trait object in a box -- which is exactly the
    unresolved-indirect-call class, and exactly what a name-resolution-free
    extractor must not pretend to have resolved."""
    names: set[str] = set()
    for k in range(span.sig_start, span.start):
        if tokens[k].kind == "ident" and k + 1 < span.start and tokens[k + 1].text == ":":
            names.add(tokens[k].text)
    for k in range(span.start, span.end):
        if tokens[k].kind == "ident" and tokens[k].text == "let":
            j = k + 1
            if j < span.end and tokens[j].text == "mut":
                j += 1
            if j < span.end and tokens[j].kind == "ident":
                names.add(tokens[j].text)
    return names


def _expression_text(tokens: list[Token], j: int, call: int) -> str:
    """A readable rendering of the call expression -- what a human needs in
    front of them to set a risk tier deliberately."""
    start = max(0, j - 4)
    parts = [t.text for t in tokens[start:call + 1]]
    separators = {";", ",", "{", "}", "=", "(", ")", "&&", "||"}
    cut = max((k for k, part in enumerate(parts[:-1]) if part in separators), default=-1)
    parts = parts[cut + 1:]
    while parts and parts[0] in {".", "::"}:
        parts.pop(0)
    rendered = ""
    for part in parts:
        if part in {".", "::", "("} or rendered.endswith((".", "::")) or not rendered:
            rendered += part
        else:
            rendered += " " + part
    return (rendered + "...)") if rendered else "<call>"


def classify_span(
    tokens: list[Token],
    span: MethodSpan,
    facts: CrateFacts,
    rel_path: str,
    counters: Counters,
    attributes: list[tuple[int, int]] | None = None,
) -> list[RawCallsite]:
    syntax_hash = "sha256:" + hashlib.sha256(
        " ".join(t.text for t in tokens[span.start:span.end + 1]).encode()
    ).hexdigest()
    results: list[RawCallsite] = []
    own_methods = facts.methods_by_concept.get(span.concept, set())
    bindings = _local_bindings(tokens, span)
    attributes = attributes or []

    def emit(call_class: str, callee_concept, callee_method, token: Token, expression: str) -> None:
        results.append(
            RawCallsite(
                call_class=call_class,
                caller_concept=span.concept,
                caller_method=span.method,
                callee_concept=callee_concept,
                callee_method=callee_method,
                expression=expression,
                line=token.line,
                path=rel_path,
                syntax_hash=syntax_hash,
            )
        )

    for i in range(span.start, span.end + 1):
        if tokens[i].text != "(" or _in_ranges(i, attributes):
            continue
        j = _walk_back_turbofish(tokens, i - 1)
        if j < 0:
            continue
        head = tokens[j]
        expression = _expression_text(tokens, j, i)

        if head.text == ")" or head.text == "]":
            # a call applied to the result of another expression
            counters.out_of_scope += 1
            continue
        if tokens[i - 1].text == "!":
            continue  # macro invocation; counted once per file, not here
        if head.kind != "ident" or head.text in NON_CALL_IDENTS:
            continue

        name = head.text
        prev = tokens[j - 1].text if j >= 1 else ""

        if prev in ITEM_KEYWORDS:
            # a nested item's own signature, not a call
            continue

        if prev == "::":
            owner = tokens[j - 2] if j >= 2 else None
            if owner is None or owner.kind != "ident":
                counters.out_of_scope += 1
                continue
            concept = span.concept if owner.text == "Self" else owner.text
            if not METHOD_RE.match(name):
                # `Enum::Variant(x)` is construction, not a call relation
                continue
            if concept in facts.concepts and name in facts.methods_by_concept.get(concept, set()):
                emit("definite-direct-call", concept, name, head, expression)
            else:
                counters.out_of_scope += 1
            continue

        if prev == ".":
            receiver = tokens[j - 2] if j >= 2 else None
            if not METHOD_RE.match(name):
                counters.out_of_scope += 1
                continue
            if receiver is not None and receiver.text == "self" and name in own_methods:
                emit("definite-direct-call", span.concept, name, head, expression)
            elif name in facts.all_methods:
                # The method name belongs to some local concept; which
                # receiver type this is stays unknown without types.
                emit("possible-dispatch", None, name, head, expression)
            else:
                counters.out_of_scope += 1
            continue

        # bare `name(...)`. A call on a parameter or `let` binding is a
        # call through a VALUE -- fn pointer, closure, boxed trait object
        # -- which no amount of syntax can resolve, and is precisely
        # CG2's territory. A name that is also a local concept method is
        # ambiguous for the same reason. Anything else is an ordinary
        # free-function call: resolved, but not an edge between concept
        # methods, so out of C's domain rather than unresolved.
        if METHOD_RE.match(name) and (name in bindings or name in facts.all_methods):
            emit("unresolved-indirect-call", None, None, head, expression)
        else:
            counters.out_of_scope += 1

    return results


# --------------------------------------------------------------------------
# Report assembly
# --------------------------------------------------------------------------

def _callsite_id(concept: str, method: str, index: int) -> str:
    return f"CS-{concept.upper()}-{method.upper().replace('_', '-')}-{index:03d}"


def rust_sources(crate_root: Path) -> list[Path]:
    if not crate_root.is_dir():
        raise ExtractionError(f"crate root does not exist or is not a directory: {crate_root}")
    return sorted(p for p in crate_root.glob("**/*.rs") if p.is_file())


def extract_crate(
    crate_root: Path,
    workspace: Path,
    report_id: str,
    crate_dir: str,
    config_scope: dict,
    previous: dict | None = None,
) -> dict:
    sources = rust_sources(crate_root)
    parsed: list[tuple[Path, list[Token], list[MethodSpan]]] = []
    facts = CrateFacts()
    counters = Counters()

    for path in sources:
        try:
            text = path.read_text()
        except UnicodeDecodeError as e:
            raise ExtractionError(f"{path} is not readable as UTF-8 text: {e}")
        tokens = tokenize(strip_noncode(text))
        spans = find_method_spans(tokens)
        facts.add(spans)
        parsed.append((path, tokens, spans))

    raw: list[RawCallsite] = []
    for path, tokens, spans in parsed:
        rel = _relative(path, workspace)
        attributes = attribute_ranges(tokens)
        counters.macros += count_macro_invocations(tokens, attributes)
        counters.unattributed += _count_unattributed_calls(tokens, _covered_ranges(spans), attributes)
        for span in spans:
            raw.extend(classify_span(tokens, span, facts, rel, counters, attributes))

    callsites = _to_records(raw, previous)
    return {
        "schema_version": "1.0",
        "report_id": report_id,
        "crate_dir": crate_dir,
        "coverage_scope": {
            "extractor": EXTRACTOR_NAME,
            "extractor_version": EXTRACTOR_VERSION,
            "extractor_backing": EXTRACTOR_BACKING,
            "supported_call_forms": list(SUPPORTED_CALL_FORMS),
            "unsupported_call_forms": list(UNSUPPORTED_CALL_FORMS),
            "completeness_claim": COMPLETENESS_CLAIM,
        },
        "config_scope": config_scope,
        "attribution_scope": {
            "scanned_item_kinds": list(SCANNED_ITEM_KINDS),
            "local_concepts": sorted(facts.concepts),
            "unattributed_call_sites": counters.unattributed,
            "out_of_scope_call_sites": counters.out_of_scope,
            "macro_invocations": counters.macros,
        },
        "callsites": callsites,
        "callsite_coverage": coverage_counts(callsites),
    }


def coverage_counts(callsites: list[dict]) -> dict:
    resolved = sum(1 for c in callsites if c["call_class"] == "definite-direct-call")
    return {
        "discovered": len(callsites),
        "resolved": resolved,
        "unresolved": len(callsites) - resolved,
    }


def _relative(path: Path, workspace: Path) -> str:
    try:
        return str(path.resolve().relative_to(workspace.resolve()))
    except ValueError:
        return str(path)


def _covered_ranges(spans: list[MethodSpan]) -> list[tuple[int, int]]:
    return [(s.start, s.end) for s in spans]


def _count_unattributed_calls(
    tokens: list[Token], covered: list[tuple[int, int]], attributes: list[tuple[int, int]]
) -> int:
    """Call-shaped expressions outside every scanned method body. Counted,
    never attributed -- see attribution_scope's own schema description.
    An item's own signature (`fn dispatch(`, `struct Wrapper(`) and an
    attribute's arguments are call-SHAPED but are not calls; counting
    them would inflate the very number that exists to be honest."""
    count = 0
    for i, token in enumerate(tokens):
        if token.text != "(":
            continue
        if any(start <= i <= end for start, end in covered) or _in_ranges(i, attributes):
            continue
        if tokens[i - 1].text == "!":
            continue
        j = _walk_back_turbofish(tokens, i - 1)
        if j < 0:
            continue
        head = tokens[j]
        if head.kind != "ident" or head.text in NON_CALL_IDENTS:
            continue
        if j >= 1 and tokens[j - 1].text in ITEM_KEYWORDS:
            continue
        count += 1
    return count


def _to_records(raw: list[RawCallsite], previous: dict | None) -> list[dict]:
    human_tiers = _human_tiers(previous)
    seen: dict[tuple[str, str], int] = {}
    records: list[dict] = []
    for item in raw:
        key = (item.caller_concept, item.caller_method)
        seen[key] = seen.get(key, 0) + 1
        callsite_id = _callsite_id(item.caller_concept, item.caller_method, seen[key])
        record: dict = {
            "callsite_id": callsite_id,
            "call_class": item.call_class,
            "caller": {"concept": item.caller_concept, "method": item.caller_method},
            "source": {
                "path": item.path,
                "symbol": f"{item.caller_concept}::{item.caller_method}",
                "line": item.line,
                "syntax_hash": item.syntax_hash,
            },
        }
        if item.call_class == "definite-direct-call":
            record["callee"] = {"concept": item.callee_concept, "method": item.callee_method}
            record["risk_tier_source"] = "not-applicable"
        else:
            if item.call_class == "possible-dispatch":
                record["callee_method"] = item.callee_method
            record["unresolved_expression"] = item.expression
            carried = human_tiers.get(callsite_id)
            if carried is not None:
                record["risk_tier"] = carried["risk_tier"]
                record["risk_tier_source"] = "human"
                record["risk_review"] = carried["risk_review"]
            else:
                record["risk_tier"] = DEFAULT_RISK_TIER
                record["risk_tier_source"] = "extractor-default"
        records.append(record)
    return records


def _human_tiers(previous: dict | None) -> dict[str, dict]:
    """Human risk decisions survive a re-extraction; extractor defaults do
    not (a re-run must be free to re-derive its own). Keyed by
    callsite_id, which is deterministic for unchanged source."""
    if not isinstance(previous, dict):
        return {}
    carried: dict[str, dict] = {}
    for record in previous.get("callsites") or []:
        if not isinstance(record, dict):
            continue
        if record.get("risk_tier_source") != "human":
            continue
        if not isinstance(record.get("risk_review"), dict) or not record.get("risk_tier"):
            continue
        carried[record.get("callsite_id")] = {
            "risk_tier": record["risk_tier"],
            "risk_review": record["risk_review"],
        }
    return carried


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("crate_root", type=Path, help="Crate directory to scan for **/*.rs")
    parser.add_argument("--workspace", type=Path, default=Path("."))
    parser.add_argument("--crate-dir", required=True, help="The descriptor's crates[].crate_dir for this crate")
    parser.add_argument("--report-id", required=True, help="Must equal the output filename stem (G1b)")
    parser.add_argument(
        "--target", required=True,
        help="Rust target triple this observation is relative to. Required, never guessed: "
             "C is configuration-relative (plan.md §5), and an invented target makes R1's "
             "configuration comparison meaningless.",
    )
    parser.add_argument("--feature", action="append", default=[], help="Enabled cargo feature, repeatable")
    parser.add_argument("--cfg", action="append", default=[], help="Enabled cfg predicate, repeatable")
    parser.add_argument(
        "--previous", type=Path, default=None,
        help="An earlier report whose human-set risk tiers should be carried over by callsite_id",
    )
    parser.add_argument("--out", type=Path, default=None, help="Write the report here instead of stdout")
    args = parser.parse_args(argv)

    previous = None
    if args.previous is not None:
        try:
            previous = json.loads(args.previous.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(f"error: --previous {args.previous} is not readable JSON: {e}", file=sys.stderr)
            return 2

    config_scope = {
        "target": args.target,
        "features": sorted(set(args.feature)),
        "cfg": sorted(set(args.cfg)),
    }

    try:
        report = extract_crate(
            args.crate_root, args.workspace, args.report_id, args.crate_dir, config_scope, previous
        )
    except ExtractionError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    text = json.dumps(report, indent=2, sort_keys=False) + "\n"
    if args.out is None:
        print(text, end="")
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
        coverage = report["callsite_coverage"]
        print(
            f"wrote {args.out}: {coverage['discovered']} discovered, "
            f"{coverage['resolved']} resolved, {coverage['unresolved']} unresolved "
            f"({COMPLETENESS_CLAIM})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
