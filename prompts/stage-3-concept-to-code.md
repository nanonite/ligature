# Stage 3 (concept / §1) — concept-to-code concept specification drafting

One-shot template. Structured-output contract, not an interactive session:
the caller (`scripts/pipeline.py draft`) parses stdout directly and runs it
through the concept-spec schema validator for immediate G1a feedback before
it's ever staged. Counterpart to `stage-3-boundary-drafting.md` for the
concept layer (plan.md §1, concept-to-code's Step A).

## Output contract

Output **only** the JSON object for the concept specification. No markdown
code fence, no explanation before or after, no partial output if you are
unsure — in that case output nothing and the caller treats an
empty/unparseable response as a hard failure to retry or escalate, never as
an empty spec.

```json
{
  "schema_version": "1.0",
  "concept": "<PascalCase concept name>",
  "kind": "struct | trait | enum",
  "cluster": "<kebab-case cluster name>",
  "english_description": "<restricted-English description, min 20 chars>",
  "verifier": "kani | creusot | verus",
  "queries": [
    {
      "english": "<restricted-English question, min 10 chars>",
      "rust_sig": "fn <name>(&self) -> <type>",
      "pure": true,
      "witness_required": false
    }
  ],
  "commands": [
    {
      "english": "<restricted-English imperative, min 10 chars>",
      "rust_sig": "fn <name>(&mut self, ...) -> <type>"
    }
  ],
  "constraints": [
    {
      "english": "<restrained-English invariant/precondition/postcondition, min 10 chars>",
      "logic": "<Rust-boolean-subset expression, min 3 chars>",
      "kind": "invariant | precondition | postcondition",
      "source": "hand | daikon | llmlift | oracle-doc",
      "applies_to": ["<query or command name>"],
      "id": "C<nnn>"
    }
  ],
  "adversary_table": [
    {
      "scenario": "<concrete bad input or edge case, min 10 chars>",
      "violates": "<constraint or behavior the scenario attempts to break, min 3 chars>",
      "resolution": "<expected handling: reject, normalize, panic-free error, or preserve reference behavior, min 3 chars>"
    }
  ],
  "source_references": [
    {
      "kind": "paper | oracle-class | oracle-file | reference-implementation | issue | other",
      "citation_or_path": "<citation or path, min 3 chars>",
      "notes": "<optional notes>"
    }
  ],
  "supplementary_imports": ["<rust use-path>"],
  "depends_on": [
    {
      "crate": "<kebab-case crate name>",
      "concept": "<PascalCase concept name>",
      "reason": "<why this concept depends on the named one, min 10 chars>"
    }
  ]
}
```

`source_references`, `supplementary_imports`, and `depends_on` are all
optional — omit them when not applicable. `queries`, `commands`,
`constraints`, and `adversary_table` are required for `kind: struct` (the
default) and `kind: trait`; `kind: enum` requires `trait_ref` and
`variants` instead. Do **not** emit a `review` field: `ligature approve`
attaches it, after a human reviewer signs off.

## Inputs

- `{{concept}}` — the PascalCase concept name.
- `{{cluster}}` — the kebab-case domain cluster that groups related
  concepts (e.g. `parsing`, `data-model`, `numeric-kernel`).
- `{{verifier}}` — the verifier target: `kani`, `creusot`, or `verus`.
- `{{source_material}}` — the code, comment, test, requirement doc, or
  observed-behavior trace this concept is drawn from.
- Re-entry only (plan.md §6 diagram: change requests/drift findings route
  back to Stage 0/3): `{{prior_artifact}}` (the last drafted concept spec,
  if any) and `{{finding}}` (the specific gate failure or drift report that
  triggered this re-entry). When both are present, revise
  `{{prior_artifact}}` to address `{{finding}}` specifically; do not
  redraft from scratch and do not touch anything the finding didn't flag.

## Task

Given the source material, draft the concept specification for
`{{concept}}`.

### 1. `concept` and `cluster`

- `concept` — PascalCase, matching concept-to-code's own `concept` field
  pattern (`^[A-Z][A-Za-z0-9]*$`). This is not a Rust type declaration.
- `cluster` — kebab-case (`^[a-z][a-z0-9-]*$`), domain-agnostic. The
  grouping key for closure profiles and work-package manifests.

### 2. `verifier`

One of `kani`, `creusot`, or `verus` — the verifier target used when
generated stubs are checked. Every concept in a cluster must declare the
same verifier.

### 3. `queries` and `commands`

- `queries` — constructors and observational behaviors. Each query's
  `rust_sig` must take `&self` and return a value; `pure` is `true` by
  construction (Step B rejects mutable query signatures).
- `commands` — state-changing behaviors and constructors. Each command's
  `rust_sig` takes `&mut self` or is an associated function.

A query has no name field of its own — concept-to-code embeds the name in
`rust_sig` (plan.md §2's "de facto id"). The function name embedded in
`rust_sig` is how witness specs reference a query.

### 4. `constraints` — invariants, preconditions, postconditions

Each constraint has:
- `english` — restricted-English statement (min 10 chars).
- `logic` — Rust-boolean-subset expression (min 3 chars).
- `kind` — `invariant`, `precondition`, or `postcondition` (default
  `invariant`).
- `source` — `hand`, `daikon`, `llmlift`, or `oracle-doc` (default
  `hand`). `daikon` means trace-inferred by an external invariant-mining
  tool; constraints with this tag must be reviewed before promotion.
- `applies_to` — optional query or command names this constraint applies
  to.
- `id` — **author-assigned stable obligation identifier**, pattern
  `^C\d{3}$` (e.g. `C001`, `C002`). Required once chainlink #40 is applied
  upstream; optional today but strongly recommended — the reliance-graph
  pipeline references obligations by stable id everywhere
  (`callee_guarantees: [TaskQueue.C003]`, `reliances[].obligation_id`,
  work-package `obligations`). Assign ids sequentially starting from
  `C001`. Stable under reordering by construction, unlike a positional
  scheme.

### 5. `adversary_table` — required counterexample table

Step A cannot advance while this is empty. Each entry:
- `scenario` — concrete bad input, edge case, or reference-implementation
  divergence attempt (min 10 chars).
- `violates` — constraint or behavior the scenario attempts to break (min 3
  chars).
- `resolution` — expected handling: reject, normalize, panic-free error, or
  preserve reference-implementation behavior (min 3 chars).

### 6. `depends_on` — optional semantic dependencies

Purely declarative. Semantic dependencies on other concepts not already
captured by `implements`/`trait_ref`/`variants` or `supplementary_imports`.
Each entry names the depended-on concept's crate and concept, plus a
`reason` (min 10 chars) stating the actual assumption — not just a citation.

## Schema

`vendor/concept-to-code/schemas/spec.schema.json` — validate against this
exactly. `additionalProperties`: false throughout (including nested
objects); do not add fields it doesn't declare, however useful they seem.

The `witness_required` field on queries, the `id` field on constraints, and
the `review` block are this pipeline's own extensions (chainlink #33, #40
and #105), recorded at `docs/concept-to-code-witness-required-schema.json`,
`docs/concept-to-code-constraint-id-schema.json` and
`docs/concept-to-code-modifications.md` gap #7. They are accepted as
optional by this pipeline's drafting and discovery tooling (`review` is
required only in the approved form); the vendored schema itself does not
yet include them.

## Filename

The eventual filename must equal `<snake_case(concept)>.json` exactly, flat
inside the crate's `specs/` directory (NOT in a `_`-prefixed subdirectory —
concept specs live directly under `specs/`, unlike boundary/interaction/
bridge/witness artifacts). The snake_case conversion uses concept-to-code's
OWN rule (vendor/concept-to-code/emit_stubs.py:snake_case), copied rather
than re-derived — plan.md §2 records what re-deriving it cost last time
(HTTPClient -> h_t_t_p_client). This is enforced, not just documented: G1b
at draft and at approve time refuses a spec whose filename disagrees with
its own `concept` (chainlink #105), because that name is how every
consumer of this spec resolves it.

## review block

A concept spec is promoted by `ligature approve --reviewer <human>
<target>`, which attaches the `review` block after a human signs off —
never by this template. Do not emit a `review` field: the draft validator
refuses one outright, because a model that writes its own `review` is
asserting a sign-off that never happened, and for a concept spec that
sign-off is load-bearing (it is what lets `accept-promotion` prove the
spec a promotion was built over was reviewed).

This is not ceremony. Every gate downstream resolves ids and queries
against the promoted spec: a boundary contract's `callee_guarantees:
[TaskQueue.C003]` is checked against this spec's `constraints`, a witness
spec's `query` against this spec's `queries`, and G18's coverage against
`queries[].witness_required`. A spec that never gets promoted is a spec
none of them can see — and the resulting silence is quiet: `approve` on a
boundary contract passes at exit 0 with `applies_to` reported merely
"unverifiable" rather than rejected, because there was no spec to check it
against.

## Write set

The concept spec lands in the crate's `specs/` directory — a
`protected_roots` path. That directory is written only through this draft
path, never by hand and never by writing the file directly: the project
descriptor's `write_set` declares the spec trees off-limits to free-form
writes, and `ligature write-set-check` reports a file hand-written into
this directory as a blocking `protected-write` violation, exactly as it
reports a file outside `allowed_roots` (the crate `src/` and `tests/`
trees) as `out-of-set`. If a finding implies writing anywhere else,
surface it instead of writing.

The one exception is a **sanctioned** protected write: one the issue in
progress is authorized to make. It is authorized by a recorded,
issue-scoped capability grant (`ligature authorize-write --issue <N>
--path <path> --op write --issuer <name>`, recorded in
`ci/results/protected-writes.jsonl`), and `ligature write-set-check
--issue <N>` reports such a write as `authorized protected write [grant
<id>]` rather than as a `protected-write` violation. If the work in hand
needs a protected write that no grant covers, stop and report it — ask
the human to record the grant. Never run `authorize-write` yourself (it is
a human checkpoint like `approve`) and never hand-edit the grant ledger.
