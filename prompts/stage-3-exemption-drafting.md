# Stage 3 (reliance / R2) — boundary-required exemption drafting

One-shot template. Structured-output contract, not an interactive session:
the caller (`scripts/pipeline.py draft`) parses stdout directly and runs it
through `scripts/validate_exemption.py`'s immediate G1a/G1b feedback before
it's ever staged for human review. Counterpart to
`stage-3-boundary-drafting.md` for the exemption side of R2 (plan.md §5.2/
§7.2, gate table row R2).

## Output contract

Output **only** the JSON object for the exemption. No markdown code fence,
no explanation before or after, no partial output if you are unsure — in
that case output nothing and the caller treats an empty/unparseable
response as a hard failure to retry or escalate, never as an empty
exemption.

```json
{
  "schema_version": "1.0",
  "interaction_id": "<{{interaction_id}}, verbatim>",
  "rationale": "<why this boundary-required edge is exempted>"
}
```

Do **not** emit a `review` field: see "review block" at the end of this
document.

## Inputs

- `{{interaction_id}}` — the I edge this exemption covers. Must equal the
  eventual filename stem exactly (G1b, same discipline as
  `boundary_id`/`interaction_id`). One exemption per interaction.
- `{{interaction}}` — the interaction record JSON this exemption covers, if
  it already exists.
- Re-entry only (plan.md §6 diagram: change requests/drift findings route
  back to Stage 0/3): `{{prior_artifact}}` (the last drafted exemption, if
  any) and `{{finding}}` (the specific gate failure or drift report that
  triggered this re-entry — e.g. an R2 finding from
  `scripts/validate_exemption.py`). When both are present, revise
  `{{prior_artifact}}` to address `{{finding}}` specifically; do not
  redraft from scratch and do not touch anything the finding didn't flag.

## Task

Given the interaction record, draft the exemption for
`{{interaction_id}}`.

### What an exemption is

A reviewed exemption stands in for a boundary artifact when R2 checks that
every eligible I edge is covered — but only for a **non-temporal,
boundary-required** edge (plan.md §5.3: a normal boundary exemption is
never sufficient for a temporal/protocol obligation, which needs a protocol
artifact or protocol-debt record instead, #19).

An exemption is a **human authority decision** (plan.md §7.2: "protocol
exemptions and scope cuts" require sign-off), never a promotion_id
(references are one-way, plan.md §7.1).

### `rationale`

Required, non-empty. State why this boundary-required edge is exempted —
not just "it's exempt" but the actual reason a human reviewer would accept
the scope cut. The rationale is the human's justification, not a
restatement of the schema fields.

## Schema

`docs/exemption-schema.json` — validate against this exactly.
`additionalProperties`: false throughout; do not add fields it doesn't
declare, however useful they seem.

## Filename and interaction_id

`interaction_id` must equal the eventual filename stem exactly:
`{{interaction_id}}` — the caller derives the filename from it, not the
other way around.

## review block

Omit the `review` field entirely. Your output is a draft
(`scripts/review_checkpoint.py stage_draft` takes it as-is); `review` is
added only by `approve`/`approve-exemption-pair`, from an explicit
human-supplied reviewer. Do not guess a reviewer name or date, and do not
claim a review happened.

## Write set

The exemption lands in the crate's `specs/_exemptions/` directory — a
`protected_roots` path. That directory is written only through this
draft → `approve`/`approve-exemption-pair` path, never by hand and never by
writing the file directly: the project descriptor's `write_set` declares
the spec trees off-limits to free-form writes, and `ligature
write-set-check` reports a file hand-written into this directory as a
blocking `protected-write` violation, exactly as it reports a file
outside `allowed_roots` (the crate `src/` and `tests/` trees) as
`out-of-set`.
If a finding implies writing anywhere else, surface it instead of writing.
