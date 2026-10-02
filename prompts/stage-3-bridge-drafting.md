# Stage 3 (reliance / O) — bridge specification drafting

One-shot template. Structured-output contract, not an interactive session:
the caller (`scripts/pipeline.py draft`) parses stdout directly and runs it
through `scripts/validate_bridge.py`'s immediate G1a/G1b feedback before it's
ever staged for human review. Counterpart to
`stage-3-boundary-drafting.md` for the call-site implication layer (plan.md
§8.2/§8.3, chainlink #22).

## Output contract

Output **only** the JSON object for the bridge specification. No markdown
code fence, no explanation before or after, no partial output if you are
unsure — in that case output nothing and the caller treats an
empty/unparseable response as a hard failure to retry or escalate, never as
an empty proof.

```json
{
  "schema_version": "1.0",
  "bridge_id": "<{{bridge_id}}, verbatim>",
  "boundary_id": "<{{boundary_id}}, verbatim>",
  "callee_requirement": "<CalleeConcept>.<constraint id>",
  "available_contract_facts": [
    {
      "obligation_id": "<Concept>.<constraint id>",
      "role": "caller-precondition | invariant | prior-callee-postcondition | trusted-environment-assumption"
    }
  ],
  "required_local_facts": [
    {
      "fact_id": "<snake_case fact name>",
      "expression": "<the local fact's expression>"
    }
  ],
  "target_expression": "<the concrete call being discharged>",
  "protocol_class": "pairwise | non-pairwise",
  "bridge_logic": {
    "bindings": {"<snake_case name>": "<type>"},
    "premises": ["<predicate application>"],
    "conclusion": {"obligation_id": "<CalleeConcept>.<constraint id>"}
  }
}
```

`required_local_facts` is optional — omit it entirely when every fact the
bridge needs is already a contract fact. Do **not** emit a `review` field:
see "review block" at the end of this document.

## Inputs

- `{{bridge_id}}` — the target artifact id, precomputed by the caller. Must
  equal the eventual filename stem exactly, same discipline as
  `boundary_id`/`interaction_id`.
- `{{boundary_id}}` — the boundary contract this bridge proves a call-site
  implication for. Must reference a real, promoted boundary contract in the
  same crate (G2, checked at approve time).
- `{{caller_spec}}` — the caller's concept-to-code spec JSON (queries,
  commands, constraints).
- `{{callee_spec}}` — the callee's concept-to-code spec JSON.
- `{{boundary_contract}}` — the boundary contract JSON this bridge discharges
  a callee_guarantees entry for.
- Re-entry only (plan.md §6 diagram: change requests/drift findings route
  back to Stage 0/3): `{{prior_artifact}}` (the last drafted bridge
  specification, if any) and `{{finding}}` (the specific gate failure or
  drift report that triggered this re-entry — e.g. a G2 finding from
  `scripts/validate_bridge.py`). When both are present, revise
  `{{prior_artifact}}` to address `{{finding}}` specifically; do not
  redraft from scratch and do not touch anything the finding didn't flag.

## Task

Given the boundary contract and the caller/callee concept specs, draft the
bridge specification for the call site that discharges
`{{callee_requirement}}`.

### 1. `callee_requirement` and `bridge_logic.conclusion.obligation_id`

These two fields must name the **same** obligation exactly (G1b, checked
structurally at draft time). `callee_requirement` is the human-readable
summary; `bridge_logic.conclusion.obligation_id` is the machine-checkable
claim. They can never silently diverge.

`callee_requirement` must be one of `{{boundary_contract}}`'s own
`callee_guarantees` entries — a bridge cannot discharge an obligation the
boundary contract never declared (G2, checked at approve time).

### 2. `available_contract_facts` — what is genuinely available at the call site

plan.md §8.2: the real obligation is that caller preconditions AND caller
invariants AND path condition AND prior call results IMPLY the callee
precondition. Each fact's `role` must be one of:

- `caller-precondition` — a precondition the caller's own contract requires
  before this call.
- `invariant` — an invariant the caller's own contract maintains.
- `prior-callee-postcondition` — a postcondition of a *previous* callee call
  that has already returned.
- `trusted-environment-assumption` — an assumption about the environment
  (e.g. a trusted allocator, a hardware guarantee).

**Deliberately excluded: `caller-postcondition`.** A caller postcondition
only holds *after* the caller returns — it can never be a fact available
*before* the callee is invoked. The schema's enum makes this structurally
impossible to write; do not try to work around it.

### 3. `bridge_logic` — the typed proof obligation (plan.md §8.3)

`bridge_logic` is compiled to a verifier harness by
`scripts/bridge_harness.py`, over a deliberately tiny expression
fragment. Two rules constrain what you may write here, and **both are
checked before the artifact is staged** (`validate_bridge` G1b, at
`draft` and again at `approve`) — an artifact that breaks either is
refused with the compiler's own reason rather than promoted and failed
later by `check-bridges`/`gate-g9`:

1. **`premises` entries are predicate applications only.** No operators
   (`==`, `<`, `>=`, `+`, `&&`, …), no literals, no quantifiers, no
   negation. Each entry is one application, written as
   `concept_obligation(args…)` or `some_binding.method(args…)`.
2. **`bindings` keys are snake_case** — top-level and nested alike
   (`caller_self: Scheduler`, never `callerSelf` or `GROUP_WIDTH`). A
   binding becomes a harness *parameter name*, so its name is the name
   the verifier will see.

The fragment, exactly as the compiler accepts it:

```
expression  := obligation | call | path
obligation  := Concept '.' Code '(' [args] ')'      e.g. Scheduler.I001(caller_self)
call        := path '(' [args] ')'                  e.g. caller_self.ready(args.now)
path        := ident ('.' ident)*                   e.g. args.now
args        := expression (',' expression)*
```

There is a temptation worth naming: `premises` is a **list**, and the
list *is* the conjunction. So two assumptions means two entries —

```json
"premises": ["caller_self.ready(args.now)", "Scheduler.I001(caller_self)"]
```

— not one entry containing `&&`. `&&` inside an entry is neither
needed nor accepted, and is the single most common way a bridge that
reads correctly fails to compile.

Two further consequences of the same compiler, worth knowing before
you draft rather than after you are refused: every name a premise
mentions must be declared in `bindings` (or be a
`required_local_facts[].fact_id`), and an obligation applied in a
premise must appear in `available_contract_facts` — a premise citing a
fact the bridge never declared as available is plan.md §8.2's central
error (the caller-postcondition case), and it is refused too.

Field by field:

- `bindings` — name → type. Each binding's value is either a bare type
  name (e.g. `caller_self: Scheduler`) or a nested named group of typed
  sub-bindings (e.g. `args: {now: Time}`). Every name is snake_case.
- `premises` — at least one predicate application (see the fragment
  above). These are the logical premises that, together with the
  available contract facts, imply the conclusion.
- `conclusion.obligation_id` — must equal `callee_requirement` exactly.

### 4. `protocol_class`

`"pairwise"` for a normal single caller-call-to-single-callee-response edge.
`"non-pairwise"` only for a genuinely temporal/multi-step protocol (plan.md
§5.3) — same vocabulary as the interaction schema's own field.

### 5. `target_expression`

The concrete call being discharged, e.g. `TaskQueue.pop_ready(args,
callee_state)`. Free text at the schema level.

## Schema

`docs/bridge-schema.json` — validate against this exactly.
`additionalProperties`: false throughout (including nested objects); do not
add fields it doesn't declare, however useful they seem.

## Filename and bridge_id

`bridge_id` must equal the eventual filename stem exactly:
`{{bridge_id}}` — the caller derives the filename from it, not the other
way around. The pattern is `^BR-[A-Z][A-Z0-9-]*-[0-9]{3}$`.

## review block

Omit the `review` field entirely. Your output is a draft
(`scripts/review_checkpoint.py stage_draft` takes it as-is); `review` is
added only by `approve`, from an explicit human-supplied reviewer. Do not
guess a reviewer name or date, and do not claim a review happened.

## Write set

The bridge specification lands in the crate's `specs/_bridges/` directory —
a `protected_roots` path. That directory is written only through this
draft → `approve` path, never by hand and never by writing the file
directly: the project descriptor's `write_set` declares the spec trees
off-limits to free-form writes, and `ligature write-set-check` reports any
file that appears outside `allowed_roots` (the crate `src/` and `tests/`
trees) without a declaration accounting for it. If a finding implies
writing anywhere else, surface it instead of writing.
