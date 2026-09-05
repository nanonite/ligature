#!/usr/bin/env python3
"""Bridge harness generation: compile a promoted bridge's typed
`bridge_logic` into a verifier harness.

plan.md §8.3, chainlink #47 (split from #22, which fixed the
representation this module compiles).

plan.md §15's open item, answered here because the answer determines
what this module is
-------------------------------------------------------------------
  "Does the typed bridge expression language need a formal semantics
   document, or is 'compiles to a harness the owning verifier checks' a
   sufficient definition within a single verifier system?"

**Within a single verifier system, "compiles to a harness the owning
verifier checks" is sufficient -- but only if the compilation is TOTAL
OR REJECTING.** "The harness defines the meaning" is a vacuous answer
for a language that accepts arbitrary strings, because then the meaning
of an expression is whatever the compiler happened to emit for it, and
two readers can disagree about a document that compiled. It stops being
vacuous exactly when the compiler accepts a closed fragment and refuses
everything else with a reason. So this module defines the language by
what it compiles, and the fragment is deliberately tiny:

    expression  := obligation | call | path
    obligation  := Concept '.' Code '(' [args] ')'     e.g. Scheduler.I001(caller_self)
    call        := path '(' [args] ')'                 e.g. caller_self.ready(args.now)
    path        := ident ('.' ident)*                  e.g. args.now
    args        := expression (',' expression)*

No operators, no literals, no quantifiers, no negation. `premises` is a
LIST, and a list is the conjunction -- so `&&` inside one premise is not
needed and is not accepted. Anything outside the fragment is a compile
error naming the offending text, never a pass-through: a string this
module cannot represent is a string the harness cannot be trusted to
mean.

**Across verifier systems the answer is still no** (CG1: cross-verifier
composition has no soundness theorem). This module compiles per verifier
and says which one it compiled for; a bridge whose two sides are owned
by different systems keeps the `harness-tested` ceiling and needs a
degradation record. That boundary is enforced at the cluster level by
the closure profile's `single_verifier_system` (chainlink #25), not
here.

What this replaces
------------------
plan.md §8.3's temporary alternative: "make the harness itself normative
and hash-pinned, and report `harness-tested` -- never `bridge-checked`,
because no machine relation exists between prose and harness." The
machine relation is exactly this module: the harness is DERIVED from the
promoted `bridge_logic`, deterministically, so its hash can be
recomputed from the promoted artifact and compared against the hash a
verifier run recorded. A hash-pinned hand-written harness pins *a*
harness; a derived harness pins *this bridge's* harness.

Compilation conventions, stated because they are choices
--------------------------------------------------------
* `bindings` become the harness's typed parameters. A nested group
  (`args: {now: Time}`) is flattened to `args_now: Time`, and the path
  `args.now` renders as that parameter -- one deterministic name per
  bound value, so a premise and a signature can never disagree.
* A root identifier resolves against top-level binding names first, then
  against nested sub-binding names when that name is unambiguous across
  groups. plan.md §8.3's own example writes `now` bare while
  docs/bridge-schema.json's writes `args.now`; both are real, so both
  resolve, and an ambiguous bare name is an error rather than a guess.
* A path rooted at a scalar-typed binding (`caller_self.ready`) is
  passed through as a member access: this module has no type
  information about `Scheduler`, and the owning verifier does. It is
  checked for being ROOTED in something declared, which is the part a
  document can get wrong on its own.
* An obligation applied in a premise must be one of the bridge's own
  `available_contract_facts` -- a premise citing a fact the bridge never
  declared as available is plan.md §8.2's central error (the caller
  postcondition case), and it is refused.
* The conclusion is the obligation from `bridge_logic.conclusion`,
  applied to every harness parameter in declaration order.
  `target_expression` is deliberately NOT compiled: the field carries
  two different readings in this repo's own artifacts (an obligation
  application in plan.md §8.2, a callee call in the #22 fixture), and
  inventing a semantics for an ambiguous field is precisely the kind of
  guess the fragment above exists to prevent. #22 already ties
  `callee_requirement` to `conclusion.obligation_id`; the human-readable
  statement stays human-readable.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

VERIFIERS = ("creusot", "kani", "verus")

EVIDENCE_KIND_BY_VERIFIER = {
    "creusot": "creusot-deductive-check",
    "verus": "verus-deductive-check",
    "kani": "kani-bounded-model-check",
}

IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
CONCEPT_RE = re.compile(r"^[A-Z][A-Za-z0-9]*$")
OBLIGATION_CODE_RE = re.compile(r"^[A-Z][0-9]+$")
BINDING_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class CompileError(Exception):
    """Raised for anything outside the compilable fragment. Carrying a
    reason is the point: "the harness defines the meaning" is only a real
    answer when the compiler can say exactly what it refused and why."""


@dataclass(frozen=True)
class Path:
    segments: tuple[str, ...]


@dataclass(frozen=True)
class Call:
    target: Path
    args: tuple


@dataclass(frozen=True)
class Obligation:
    obligation_id: str
    args: tuple


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char.isspace():
            i += 1
            continue
        if char in "().,":
            tokens.append(char)
            i += 1
            continue
        match = IDENT_RE.match(text, i)
        if not match:
            raise CompileError(
                f"unexpected character {char!r} at offset {i} in {text!r} -- the compilable "
                "fragment is predicate application only: no operators, literals, quantifiers "
                "or negation"
            )
        tokens.append(match.group())
        i = match.end()
    return tokens


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.tokens = _tokenize(text)
        self.position = 0

    def peek(self) -> str | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def take(self) -> str:
        token = self.peek()
        if token is None:
            raise CompileError(f"unexpected end of expression in {self.text!r}")
        self.position += 1
        return token

    def expect(self, token: str) -> None:
        actual = self.take()
        if actual != token:
            raise CompileError(f"expected {token!r} but found {actual!r} in {self.text!r}")

    def parse(self):
        node = self.parse_expression()
        if self.peek() is not None:
            raise CompileError(
                f"trailing {self.peek()!r} in {self.text!r} -- one expression per premise; "
                "premises are a list, and the list is the conjunction"
            )
        return node

    def parse_expression(self):
        segments = [self._ident()]
        while self.peek() == ".":
            self.take()
            segments.append(self._ident())
        path = Path(tuple(segments))
        args: tuple = ()
        applied = False
        if self.peek() == "(":
            applied = True
            args = self.parse_args()
        if (
            len(segments) == 2
            and CONCEPT_RE.match(segments[0])
            and OBLIGATION_CODE_RE.match(segments[1])
        ):
            return Obligation(f"{segments[0]}.{segments[1]}", args)
        if applied:
            return Call(path, args)
        return path

    def parse_args(self) -> tuple:
        self.expect("(")
        args: list = []
        if self.peek() == ")":
            self.take()
            return ()
        while True:
            args.append(self.parse_expression())
            token = self.take()
            if token == ")":
                return tuple(args)
            if token != ",":
                raise CompileError(f"expected ',' or ')' but found {token!r} in {self.text!r}")

    def _ident(self) -> str:
        token = self.take()
        if not IDENT_RE.fullmatch(token):
            raise CompileError(f"expected an identifier but found {token!r} in {self.text!r}")
        return token


def parse_expression(text: str):
    return _Parser(text).parse()


# --------------------------------------------------------------------------
# Scope
# --------------------------------------------------------------------------

@dataclass
class Scope:
    """The harness's parameter list, derived from `bindings`."""
    parameters: list[tuple[str, str]] = field(default_factory=list)  # (name, type)
    path_to_parameter: dict[tuple[str, ...], str] = field(default_factory=dict)
    ambiguous_bare_names: set[str] = field(default_factory=set)

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.parameters]


def build_scope(bindings: dict) -> Scope:
    scope = Scope()
    bare_seen: dict[str, int] = {}

    for name, value in bindings.items():
        if not BINDING_NAME_RE.match(name):
            raise CompileError(
                f"binding name {name!r} is not snake_case -- it becomes a harness parameter name"
            )
        if isinstance(value, str):
            scope.parameters.append((name, value))
            scope.path_to_parameter[(name,)] = name
        else:
            for sub_name, sub_type in value.items():
                if not BINDING_NAME_RE.match(sub_name):
                    raise CompileError(f"binding name {name}.{sub_name!r} is not snake_case")
                parameter = f"{name}_{sub_name}"
                scope.parameters.append((parameter, sub_type))
                scope.path_to_parameter[(name, sub_name)] = parameter
                bare_seen[sub_name] = bare_seen.get(sub_name, 0) + 1

    # plan.md §8.3 writes a nested binding bare (`now`); the schema's own
    # example writes it qualified (`args.now`). Both resolve; a bare name
    # that two groups both define is an error, never a guess.
    for bare, count in bare_seen.items():
        if bare in bindings:
            continue
        if count > 1:
            scope.ambiguous_bare_names.add(bare)
            continue
        for path, parameter in list(scope.path_to_parameter.items()):
            if len(path) == 2 and path[1] == bare:
                scope.path_to_parameter[(bare,)] = parameter
    return scope


def _resolve_root(node: Path, scope: Scope, local_facts: set[str], where: str) -> None:
    root = node.segments[0]
    if root in local_facts and len(node.segments) == 1:
        return
    if root in scope.ambiguous_bare_names:
        raise CompileError(
            f"{where}: bare name {root!r} is declared by more than one binding group -- "
            "qualify it (e.g. args.now)"
        )
    if (root,) in scope.path_to_parameter or (node.segments[:2] in scope.path_to_parameter):
        return
    raise CompileError(
        f"{where}: {'.'.join(node.segments)} is rooted at {root!r}, which bridge_logic.bindings "
        f"does not declare (declared: {sorted(set(scope.names) | set(local_facts))}) -- a harness "
        "cannot pass a value it was never given"
    )


def check_expression(node, scope: Scope, available: set[str], local_facts: set[str], where: str) -> None:
    if isinstance(node, Obligation):
        if node.obligation_id not in available:
            raise CompileError(
                f"{where}: premise applies obligation {node.obligation_id!r}, which is not among "
                f"this bridge's available_contract_facts ({sorted(available)}) -- plan.md §8.2: a "
                "bridge may only assume facts actually available at the call site"
            )
        for arg in node.args:
            check_expression(arg, scope, available, local_facts, where)
        return
    if isinstance(node, Call):
        _resolve_root(node.target, scope, local_facts, where)
        for arg in node.args:
            check_expression(arg, scope, available, local_facts, where)
        return
    _resolve_root(node, scope, local_facts, where)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def render(node, scope: Scope) -> str:
    if isinstance(node, Obligation):
        concept, code = node.obligation_id.split(".")
        rendered_args = ", ".join(render(arg, scope) for arg in node.args)
        return f"{concept}::{code}({rendered_args})"
    if isinstance(node, Call):
        return f"{render_path(node.target, scope)}({', '.join(render(a, scope) for a in node.args)})"
    return render_path(node, scope)


def render_path(node: Path, scope: Scope) -> str:
    segments = node.segments
    if segments[:2] in scope.path_to_parameter:
        head = scope.path_to_parameter[segments[:2]]
        rest = segments[2:]
    elif (segments[0],) in scope.path_to_parameter:
        head = scope.path_to_parameter[(segments[0],)]
        rest = segments[1:]
    else:
        head = segments[0]
        rest = segments[1:]
    return ".".join((head, *rest))


@dataclass(frozen=True)
class Harness:
    bridge_id: str
    verifier: str
    language: str
    name: str
    source: str
    filename: str

    @property
    def sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.source.encode()).hexdigest()

    @property
    def evidence_kind(self) -> str:
        return EVIDENCE_KIND_BY_VERIFIER[self.verifier]


def harness_name(bridge_id: str) -> str:
    return "bridge_" + re.sub(r"[^a-z0-9]+", "_", bridge_id.lower()).strip("_")


def compile_bridge(bridge: dict, verifier: str) -> Harness:
    """The whole compiler: parse, scope-check, render. Deterministic --
    the same promoted bridge and verifier always produce byte-identical
    source, which is what makes the harness hash a property OF THE
    BRIDGE rather than of whoever last edited a file."""
    if verifier not in VERIFIERS:
        raise CompileError(f"unknown verifier {verifier!r} (known: {sorted(VERIFIERS)})")

    logic = bridge["bridge_logic"]
    scope = build_scope(logic["bindings"])
    available = {fact["obligation_id"] for fact in bridge.get("available_contract_facts", [])}
    local_facts = {fact["fact_id"] for fact in bridge.get("required_local_facts", [])}

    premises = []
    for index, text in enumerate(logic["premises"]):
        node = parse_expression(text)
        check_expression(node, scope, available, local_facts, f"premise[{index}]")
        premises.append(render(node, scope))

    conclusion_id = logic["conclusion"]["obligation_id"]
    concept, code = conclusion_id.split(".")
    conclusion = f"{concept}::{code}({', '.join(scope.names)})"

    source = _RENDERERS[verifier](bridge, scope, premises, conclusion, conclusion_id)
    return Harness(
        bridge_id=bridge["bridge_id"],
        verifier=verifier,
        language="rust",
        name=harness_name(bridge["bridge_id"]),
        source=source,
        filename=f"{bridge['bridge_id']}.{verifier}.rs",
    )


def _header(bridge: dict, verifier: str, conclusion_id: str) -> str:
    return (
        f"// GENERATED by scripts/bridge_harness.py -- do not edit.\n"
        f"// bridge_id: {bridge['bridge_id']}\n"
        f"// boundary_id: {bridge['boundary_id']}\n"
        f"// verifier: {verifier}\n"
        f"// conclusion: {conclusion_id}\n"
        f"// plan.md §8.2: caller preconditions ∧ invariants ∧ path condition ∧ prior call\n"
        f"// results ⟹ callee precondition. The premises below are the assumed facts; the\n"
        f"// conclusion is what the owning verifier must establish from them.\n"
    )


def _creusot(bridge: dict, scope: Scope, premises: list[str], conclusion: str, conclusion_id: str) -> str:
    parameters = ", ".join(f"{name}: {type_name}" for name, type_name in scope.parameters)
    requires = "\n".join(f"#[creusot_contracts::requires({premise})]" for premise in premises)
    return (
        _header(bridge, "creusot", conclusion_id)
        + "// An empty body is deliberate: with no code to execute, `ensures` can only be\n"
        + "// discharged from `requires`, which is exactly the call-site implication.\n"
        + f"{requires}\n"
        + f"#[creusot_contracts::ensures({conclusion})]\n"
        + f"pub fn {harness_name(bridge['bridge_id'])}({parameters}) {{}}\n"
    )


def _verus(bridge: dict, scope: Scope, premises: list[str], conclusion: str, conclusion_id: str) -> str:
    parameters = ", ".join(f"{name}: {type_name}" for name, type_name in scope.parameters)
    requires = ",\n        ".join(premises)
    return (
        _header(bridge, "verus", conclusion_id)
        + "verus! {\n"
        + f"    proof fn {harness_name(bridge['bridge_id'])}({parameters})\n"
        + f"        requires\n        {requires},\n"
        + f"        ensures\n        {conclusion},\n"
        + "    {\n    }\n}\n"
    )


def _kani(bridge: dict, scope: Scope, premises: list[str], conclusion: str, conclusion_id: str) -> str:
    lets = "\n".join(
        f"    let {name}: {type_name} = kani::any();" for name, type_name in scope.parameters
    )
    assumes = "\n".join(f"    kani::assume({premise});" for premise in premises)
    return (
        _header(bridge, "kani", conclusion_id)
        + "// Bounded: this harness holds within kani's unwind bounds and for the\n"
        + "// monomorphizations actually enumerated (CG3), which is why a kani result makes\n"
        + "// its cluster's closure_kind `bounded` (plan.md §4).\n"
        + "#[kani::proof]\n"
        + f"pub fn {harness_name(bridge['bridge_id'])}() {{\n"
        + (lets + "\n" if lets else "")
        + (assumes + "\n" if assumes else "")
        + f'    assert!({conclusion}, "{bridge["bridge_id"]}: {conclusion_id}");\n'
        + "}\n"
    )


_RENDERERS = {"creusot": _creusot, "verus": _verus, "kani": _kani}
