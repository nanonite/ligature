# Stage 3 (reliance / G15) — protocol-debt record drafting

One-shot template. Structured-output contract, not an interactive session:
the caller (`scripts/pipeline.py draft`) parses stdout directly and runs it
through `scripts/validate_protocol_debt.py`'s immediate G1a/G1b feedback
before it's ever staged for human review. Counterpart to
`stage-3-boundary-drafting.md` for the protocol-debt side of G15 (plan.md
§5.3, gate table row G15).

## Output contract

Output **only** the JSON object for the protocol-debt record. No markdown
code fence, no explanation before or after, no partial output if you are
unsure — in that case output nothing and the caller treats an
empty/unparseable response as a hard failure to retry or escalate, never as
an empty scope cut.

```json
{
  "schema_version": "1.0",
  "interaction_id": "<{{interaction_id}}, verbatim>",
  "rationale": "<why this non-pairwise interaction's protocol is out of scope>",
  "no_promoted_obligation_depends_on_protocol": true,
  "no_work_package_touches_its_path": true,
  "no_release_claim_includes_it": true,
  "tracking_issue": "<issue tracking the missing protocol support>"
}
```

Do **not** emit a `review` field: see "review block" at the end of this
document.

## Inputs

- `{{interaction_id}}` — the non-pairwise interaction this scope cut
  covers. Must equal the eventual filename stem exactly (G1b, same
  discipline as `boundary_id`/`interaction_id`). One debt record per
  interaction.
- `{{interaction}}` — the interaction record JSON this debt record covers,
  if it already exists.
- Re-entry only (plan.md §6 diagram: change requests/drift findings route
  back to Stage 0/3): `{{prior_artifact}}` (the last drafted protocol-debt
  record, if any) and `{{finding}}` (the specific gate failure or drift
  report that triggered this re-entry — e.g. a G15 finding from
  `scripts/validate_protocol_debt.py`). When both are present, revise
  `{{prior_artifact}}` to address `{{finding}}` specifically; do not
  redraft from scratch and do not touch anything the finding didn't flag.

## Task

Given the interaction record, draft the protocol-debt record for
`{{interaction_id}}`.

### What a protocol-debt record is

A non-pairwise interaction (`protocol_class: non-pairwise`) requires an
external protocol artifact or a protocol-debt record — this is the latter:
a reviewed out-of-scope declaration. plan.md §5.3 is explicit it "suffices
only when all five hold":

1. No promoted obligation depends on the protocol.
2. No work package touches its path.
3. No release claim includes it.
4. A human signed the scope cut.
5. A tracking issue records the missing support.

### The three boolean attestations

`no_promoted_obligation_depends_on_protocol`, `no_work_package_touches_its_path`,
and `no_release_claim_includes_it` are all `const: true` in the schema — a
record can never be FILED admitting one of the three is false. These are
**human-signed attestations**, authoritative upon review (plan.md §7.2
establishes review as the terminal authority for every human checkpoint in
this system). They are not independently re-derived against promoted state
by the schema — that burden is on the named reviewer.

Set all three to `true`. Do not set any of them to `false` — the schema
rejects it, and filing a record that admits a condition is false is not a
valid scope cut.

### `rationale`

Required, non-empty. State why this non-pairwise interaction's protocol is
out of scope — the actual reason a human reviewer would accept the scope
cut, not just "it's deferred".

### `tracking_issue`

Required, non-empty. plan.md §5.3, condition 5: "a tracking issue records
the missing support." Name the issue (e.g. `chainlink:#99`).

## Schema

`docs/protocol-debt-schema.json` — validate against this exactly.
`additionalProperties`: false throughout; do not add fields it doesn't
declare, however useful they seem.

## Filename and interaction_id

`interaction_id` must equal the eventual filename stem exactly:
`{{interaction_id}}` — the caller derives the filename from it, not the
other way around.

## review block

Omit the `review` field entirely. Your output is a draft
(`scripts/review_checkpoint.py stage_draft` takes it as-is); `review` is
added only by `approve`/`approve-pair`, from an explicit human-supplied
reviewer. Do not guess a reviewer name or date, and do not claim a review
happened.

## Write set

The protocol-debt record lands in the crate's `specs/_protocol_debt/`
directory — a `protected_roots` path. That directory is written only
through this draft → `approve`/`approve-pair` path, never by hand and never
by writing the file directly: the project descriptor's `write_set` declares
the spec trees off-limits to free-form writes, and `ligature
write-set-check` reports a file hand-written into this directory as a
blocking `protected-write` violation, exactly as it reports a file
outside `allowed_roots` (the crate `src/` and `tests/` trees) as
`out-of-set`.
If a finding implies writing anywhere else, surface it instead of writing.

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
