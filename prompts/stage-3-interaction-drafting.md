# Stage 3 (reliance / I) — interaction drafting

One-shot template. Structured-output contract, not an interactive session:
the caller (`scripts/pipeline.py draft`) parses stdout directly and runs it
through `scripts/validate_interaction.py`'s immediate G1a/G1b feedback
before it's ever staged for human review. Counterpart to
`stage-3-boundary-drafting.md` for the interaction (I) side of the reliance
layer (plan.md §5.1-§5.3, chainlink #16/#17/#18/#19).

## Output contract

Output **only** the JSON object for the interaction record. No markdown
code fence, no explanation before or after, no partial output if you are
unsure — in that case output nothing and the caller treats an
empty/unparseable response as a hard failure to retry or escalate, never as
an empty guarantee.

```json
{
  "schema_version": "1.0",
  "interaction_id": "<{{interaction_id}}, verbatim>",
  "caller": {"concept": "<PascalCase concept>", "method": "<snake_case method>"},
  "callee": {"concept": "<PascalCase concept>", "method": "<snake_case method>"},
  "edge_class": ["cross-verifier | cross-crate-public-api | stateful | error-panic-boundary | ownership-transfer | numeric-domain-boundary | pure-data-type-reference | import-only | marker-type | phantom-type"],
  "eligibility": "boundary-required | inform | ignore",
  "rationale": "<the one-sentence reason this edge exists and behaves as described>",
  "evidence_links": ["E-<n>"],
  "reliances": [
    {
      "obligation_id": "<CalleeConcept>.<constraint id>",
      "required_assurance": {
        "required_claims": ["callee-precondition-established | postcondition-holds"],
        "accepted_evidence_kinds": ["kani-bounded-model-check | creusot-deductive-check | verus-deductive-check"],
        "minimum_scope": {"<verifier-specific key, e.g. input_domain>": "<value>"},
        "trust_policy": {"assumptions_allowed": []}
      }
    }
  ],
  "protocol_class": "pairwise | non-pairwise",
  "realization": {
    "requirement": "required | optional | feature-gated | platform-gated | test-only | fallback-only",
    "config_scope": {
      "target": "<{{target_triple}}, verbatim>",
      "features": ["default"],
      "cfg": []
    }
  }
}
```

`evidence_links` and `reliances` are both genuinely optional (see §2 and §5
below for exactly when to include or omit each) — the block above shows
their shape when present, not a claim that every field is always required.
Do **not** emit a `review` field: see "review block" at the end of this
document.

## Inputs

- `{{interaction_id}}` — the target artifact id, precomputed by the caller
  (unlike a boundary contract's `boundary_id`, `interaction_id` has no
  fixed derivation from caller/callee — it is assigned once and must equal
  the eventual filename stem exactly, same discipline as `boundary_id`).
- `{{caller_concept}}` / `{{caller_method}}` / `{{callee_concept}}` /
  `{{callee_method}}` — the interaction being drafted.
- `{{caller_spec}}` — the caller's concept-to-code spec JSON (queries,
  commands, constraints).
- `{{callee_spec}}` — the callee's concept-to-code spec JSON.
- `{{target_triple}}` — this project's default Rust target triple (e.g.
  `x86_64-unknown-linux-gnu`), for `realization.config_scope.target`.
- Re-entry only (plan.md §6 diagram: change requests/drift findings route
  back to Stage 0/3): `{{prior_artifact}}` (the last drafted interaction
  record, if any) and `{{finding}}` (the specific gate failure or drift
  report that triggered this re-entry — e.g. a G2++ or R2 finding from
  `scripts/validate_interaction.py`). When both are present, revise
  `{{prior_artifact}}` to address `{{finding}}` specifically; do not
  redraft from scratch and do not touch anything the finding didn't flag.

## Task

Given the caller and callee concept specs, draft the interaction record for
`{{caller_concept}}::{{caller_method}} -> {{callee_concept}}::{{callee_method}}`.

### 1. `edge_class` and `eligibility` — eligibility is COMPUTED, never hand-set

Choose every `edge_class` value that genuinely describes this edge (at
least one, from the enum below), based on what `{{caller_spec}}` and
`{{callee_spec}}` show about the call: does it cross a verification
boundary, mutate callee state, transfer ownership, touch a numeric domain,
etc.

`eligibility` is then **computed** from `edge_class` by this exact rule
(`scripts/validate_interaction.py`'s own `compute_eligibility`) — do not
choose it independently, and do not let it disagree with what `edge_class`
implies, or G1b rejects the record outright:

1. If any of `edge_class` is one of `cross-verifier`,
   `cross-crate-public-api`, `stateful`, `error-panic-boundary`,
   `ownership-transfer`, `numeric-domain-boundary` → `eligibility` is
   `"boundary-required"`.
2. Else if any of `edge_class` is `pure-data-type-reference` or
   `import-only` → `eligibility` is `"inform"`.
3. Else if any of `edge_class` is `marker-type` or `phantom-type` →
   `eligibility` is `"ignore"`.

Rule 1 takes priority over rule 2, which takes priority over rule 3 — if
`edge_class` mixes classes from more than one bucket, the earliest
matching bucket wins. When genuinely unsure whether an edge belongs in the
boundary-required bucket, prefer including a boundary-required class: a
false negative here (marking a real dependency `inform`/`ignore`) is worse
than a false positive that a human catches at `approve`.

### 2. `reliances` — only when `eligibility` is `"boundary-required"`

If `eligibility` is `"boundary-required"`, `reliances` must be present and
non-empty (G2++, plan.md gate table §12) — at least one entry naming a real
postcondition or invariant of the callee that the caller's own correctness
depends on. If `eligibility` is `"inform"` or `"ignore"`, omit `reliances`
entirely — there is nothing to declare a reliance on, and an empty array is
treated identically to an omitted field, never required.

Each reliance:

- `obligation_id` — `<CalleeConcept>.<constraint id>`, referencing a real
  constraint in `{{callee_spec}}` (same shape as a boundary contract's
  `callee_guarantees` entries). If the callee spec has no constraint with a
  stable `id` field yet (`docs/concept-to-code-modifications.md` gap #6),
  do not invent one — omit *that specific* reliance rather than fabricate
  an id, as long as at least one other genuine, stably-identified
  obligation still fills `reliances` for a boundary-required edge.

  If `eligibility` is `"boundary-required"` and **every** real obligation
  you can identify lacks a stable id — so omitting them all would leave
  `reliances` empty, which G2++ rejects for a boundary-required edge — do
  not emit an interaction with a fabricated id or an empty `reliances`
  either. Neither is a valid draft: fabricating an id asserts a constraint
  that doesn't exist, and an empty `reliances` on a boundary-required edge
  is a schema violation you can see coming. Apply this document's own
  top-level policy instead — output nothing, so the caller treats it as a
  hard failure to retry or escalate, surfacing the missing stable-id
  dependency to a human rather than staging a draft you already know is
  invalid.
- `required_assurance` — plan.md §8.1's type split, enforced structurally
  by disjoint enums. Do not mix vocabularies between the two arrays below;
  neither field's enum contains the other's values:
  - `required_claims` (at least one): what must be true —
    `callee-precondition-established` or `postcondition-holds`.
  - `accepted_evidence_kinds` (at least one): what kind of check would
    establish it — `kani-bounded-model-check`, `creusot-deductive-check`,
    or `verus-deductive-check`.
  - `minimum_scope` — an object with at least one verifier-specific key
    (e.g. `input_domain`, `feature_set`); the vocabulary is open, not
    fixed across verifiers.
  - `trust_policy.assumptions_allowed` — an array (possibly empty) of
    assumption ids this reliance is allowed to depend on.

### 3. `realization` — required on every interaction, regardless of eligibility

- `requirement` — one of `required`, `optional`, `feature-gated`,
  `platform-gated`, `test-only`, `fallback-only`. Judge this from
  `{{caller_spec}}`/`{{callee_spec}}` (is the call behind a feature flag,
  a platform cfg, only reachable in tests, a fallback path); default to
  `required` when the call is an unconditional part of the caller's
  behavior.
- `config_scope.target` — `{{target_triple}}`, verbatim.
- `config_scope.features` / `config_scope.cfg` — the feature flags / cfg
  predicates this edge was analyzed under. Default to `["default"]` and
  `[]` respectively unless the specs indicate this edge is specifically
  gated behind something else.

### 4. `protocol_class` — required on every interaction

`"pairwise"` for a normal single caller-call-to-single-callee-response
edge. `"non-pairwise"` only for a genuinely temporal/multi-step protocol
(plan.md §5.3) — a handshake, a multi-call sequence with ordering
invariants, anything a single boundary contract can't fully capture. Most
edges are `"pairwise"`; do not choose `"non-pairwise"` unless the specs
actually show a multi-step protocol.

A `"non-pairwise"` classification requires a covering protocol-debt record
(`docs/protocol-debt-schema.json`) before this interaction can be approved
alone — `pipeline.py approve-pair` is the atomic bootstrap for a new
non-pairwise interaction and its protocol-debt record together (this
template does not draft the protocol-debt record itself).

### 5. `rationale` and `evidence_links`

`rationale` (required, non-empty): the one-sentence reason this edge exists
and behaves as described — not a restatement of the schema fields.
`evidence_links` (optional): ids of evidence records (`E-<n>`,
`docs/evidence-schema.json`) that ground this interaction, if any already
exist. Omit rather than invent an id that doesn't correspond to real
evidence.

## Schema

`docs/interaction-schema.json` — validate against this exactly.
`additionalProperties: false` throughout (including nested objects); do not
add fields it doesn't declare, however useful they seem.

## Filename

The eventual filename must equal `{{interaction_id}}.json` exactly, flat
inside the crate's `_interactions/` directory. `interaction_id` in the JSON
body must equal `{{interaction_id}}` verbatim.

## review block

Omit the `review` field entirely. Your output is a draft
(`scripts/review_checkpoint.py stage_draft` takes it as-is); `review` is
added only by `approve`/`approve-pair`, from an explicit human-supplied
reviewer. Do not guess a reviewer name or date, and do not claim a review
happened.
