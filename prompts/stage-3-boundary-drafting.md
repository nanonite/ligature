# Stage 3 (reliance / O) — boundary contract drafting

One-shot template. Structured-output contract, not an interactive session:
the caller (`scripts/validate_boundary_contracts.py` via #37) parses stdout
directly. Counterpart to concept-to-code's `prompts/step-a-coanalysis.md`,
but for the cross-concept reliance layer this pipeline owns — concept-to-code's
own prompts stay interactive-session-oriented and are out of scope to change.

Covers the current Prototype A scope only: a single boundary contract (O).
The I-schema and bridge-spec templates exist now
(`prompts/stage-3-interaction-drafting.md`,
`prompts/stage-3-bridge-drafting.md`) and are drafted by their own
`draft 3 <name>` stage. What a bridge discharges is declared *here* (see
Task); the bridge's own content is never yours to emit.

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
  from `docs/reliance-policy.template.md`), resolution rule included. This
  document is **standing governance, not a one-time artifact**: it is
  user-owned, and its on-disk content is drift-checked against the reviewed
  hash the ownership manifest records — if it was edited without review,
  `check` reports a `policy-drift` finding and the installation reads as
  drifted. Never edit it as a side effect of drafting; a governance change
  goes through `ligature accept-policy --reviewer <name>` (chainlink #78).
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
`callee_guarantees`. `callee_guarantees` is this boundary's record of what
the call depends on, and it holds two roles that mean opposite things. Both
go here, referenced as `<CalleeConcept>.<constraint id>`:

- A callee **postcondition or invariant** the caller's own correctness
  depends on is a guarantee: the callee provides it once the call returns.
- A callee **precondition** the caller must establish before the call is a
  **caller obligation, not a guarantee** — the callee neither provides it
  nor checks it. Declare it here because this is where the call's reliance
  surface is declared and nowhere else: a bridge's `callee_requirement` must
  be one of this boundary's own `callee_guarantees` entries (G2, checked at
  approve time), so a precondition recorded anywhere other than here is one
  that no bridge can discharge. `scripts/validate_boundary_contracts.py`
  reads the referenced constraint's own `kind` and reports a precondition
  entry as a caller obligation the bridge discharges, not as something the
  callee guarantees — declare it, do not work around the gate.
- The **caller's own** obligations never go here. A caller precondition or
  invariant belongs to the caller's contract and is cited by a bridge in its
  `available_contract_facts`, not declared as something this call relies on
  from the callee (G2+ rejects an entry naming the caller's concept).
- An **adversary case** (`A*`-shaped) is evidence, never a guarantee. Do not
  put one in `callee_guarantees` under any circumstance, even if it looks
  like the closest match to what the caller depends on — say so is missing
  a real postcondition/invariant to cite instead, don't substitute.

An **empty** `callee_guarantees` is not the way to avoid any of this. It
says the caller's correctness depends on nothing of the callee's and that
there is no precondition to establish, which is a claim, not an omission —
and it is the shape that lets a real gap pass unnoticed. Leave it empty only
when the callee spec genuinely declares no constraint with a stable `id` yet
(the gap #6 case below).

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

## Write set

The boundary contract lands in the crate's `specs/_boundaries/` directory —
a `protected_roots` path. That directory is written only through this
draft → `approve` path, never by hand and never by writing the file
directly: the project descriptor's `write_set` declares the spec trees
off-limits to free-form writes, and `ligature write-set-check` reports a
file hand-written into this directory as a blocking `protected-write`
violation, exactly as it reports a file outside `allowed_roots` (the
crate `src/` and `tests/` trees) as `out-of-set`. If a finding implies
writing anywhere else, surface it instead of writing.

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
