# Stage 3 (reliance / O) — boundary contract drafting

One-shot template. Structured-output contract, not an interactive session:
the caller (`scripts/validate_boundary_contracts.py` via #37) parses stdout
directly. Counterpart to concept-to-code's `prompts/step-a-coanalysis.md`,
but for the cross-concept reliance layer this pipeline owns — concept-to-code's
own prompts stay interactive-session-oriented and are out of scope to change.

Covers the current Prototype A scope only: a single boundary contract (O).
Interaction-schema (I) and bridge-spec drafting are M3/M4 work — their
schemas don't exist yet, so no template for them exists yet either. Do not
invent one ahead of the schema.

## Output contract

Output **only** the JSON object for the boundary contract. No markdown code
fence, no explanation before or after, no partial output if you are unsure —
in that case output nothing and the caller treats an empty/unparseable
response as a hard failure to retry or escalate, never as an empty guarantee.

## Inputs

- `{{caller_concept}}` / `{{caller_method}}` / `{{callee_concept}}` /
  `{{callee_method}}` — the boundary being drafted.
- `{{caller_spec}}` — the caller's concept-to-code spec JSON (queries,
  commands, constraints).
- `{{callee_spec}}` — the callee's concept-to-code spec JSON.
- `{{reliance_policy}}` — this project's `docs/reliance-policy.md` (copied
  from `docs/reliance-policy.template.md`), resolution rule included.
- Re-entry only (plan.md §6 diagram: change requests/drift findings route
  back to Stage 0/3): `{{prior_artifact}}` (the last drafted boundary
  contract, if any) and `{{finding}}` (the specific gate failure or drift
  report that triggered this re-entry — e.g. a G2+ finding from
  `scripts/validate_boundary_contracts.py`). When both are present, revise
  `{{prior_artifact}}` to address `{{finding}}` specifically; do not
  redraft from scratch and do not touch anything the finding didn't flag.

## Task

Given the caller and callee concept specs, draft the boundary contract for
`{{caller_concept}}::{{caller_method}} -> {{callee_concept}}::{{callee_method}}`.

Apply `{{reliance_policy}}`'s resolution rule while deciding what belongs in
`callee_guarantees`:

- A callee **precondition** the caller must establish does **not** go here —
  it belongs in a bridge specification (not yet in scope this stage).
- A callee **postcondition or invariant** the caller's own correctness
  depends on **does** go here, referenced as `<CalleeConcept>.<constraint id>`.
- An **adversary case** (`A*`-shaped) is evidence, never a guarantee. Do not
  put one in `callee_guarantees` under any circumstance, even if it looks
  like the closest match to what the caller depends on — say so is missing
  a real postcondition/invariant to cite instead, don't substitute.

If the callee spec has no constraint with a stable `id` field yet (current
concept-to-code doesn't expose one — `docs/concept-to-code-modifications.md`
gap #6, not yet applied upstream), do not invent one and do not reference
that constraint. Omit it from `callee_guarantees` rather than fabricate an
id. The output stays JSON-only either way; an incomplete `callee_guarantees`
list is exactly the kind of thing the human catches at
`scripts/review_checkpoint.py diff`, not something to explain inline.

## Schema

`docs/boundary-contract-schema.json` — validate against this exactly.
`additionalProperties: false` throughout; do not add fields it doesn't
declare, however useful they seem.

## Filename and boundary_id

`boundary_id` must equal the eventual filename stem exactly:
`{{caller_concept_snake}}_{{caller_method}}__to__{{callee_concept_snake}}_{{callee_method}}`
(doubled underscore around `to`) — `{{caller_concept_snake}}` and
`{{callee_concept_snake}}` are `{{caller_concept}}`/`{{callee_concept}}`
lowercased with an underscore before each internal capital (`TaskQueue` ->
`task_queue`), precomputed by the caller, not derived by you from prose.
Get this right in the JSON body; the caller derives the filename from it,
not the other way around.

## review block

Omit the `review` field entirely. Your output is a draft
(`scripts/review_checkpoint.py stage_draft` takes it as-is); `review` is
added only by `approve`, from an explicit human-supplied reviewer. Do not
guess a reviewer name or date, and do not claim a review happened.
