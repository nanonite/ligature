# Stage 3 (§11) — evidence conflict resolution drafting

One-shot template. Structured-output contract, not an interactive session:
the caller (`scripts/pipeline.py draft`) parses stdout directly and runs it
through `scripts/validate_conflict_resolution.py`'s immediate G1a/G1b
feedback before it's ever staged for human review. Counterpart to
`stage-0-evidence-intake.md` for the conflict-resolution side of plan.md
§11 (gate table row G11).

## Output contract

Output **only** the JSON object for the conflict-resolution record. No
markdown code fence, no explanation before or after, no partial output if
you are unsure — in that case output nothing and the caller treats an
empty/unparseable response as a hard failure to retry or escalate, never as
an empty resolution.

```json
{
  "schema_version": "1.0",
  "conflict_id": "<{{conflict_id}}, verbatim>",
  "evidence": ["E-<n>", "E-<n>"],
  "status": "resolved | unresolved",
  "resolution": {
    "selected_authority": "E-<n>",
    "disposition_of_other": "required | incidental | bug-compat | unspecified",
    "rationale": "<why the selected authority wins and the other loses>"
  }
}
```

`resolution` is required when `status` is `"resolved"` (the schema's own
if/then enforces this) and must be omitted when `status` is
`"unresolved"`. Do **not** emit a `review` field: see "review block" at
the end of this document.

## Inputs

- `{{conflict_id}}` — the target artifact id, precomputed by the caller.
  Must equal the eventual filename stem exactly (G1b, same discipline as
  `boundary_id`/`interaction_id`). Pattern: `^EC-[0-9]+$`.
- `{{evidence_records}}` — the two conflicting evidence record JSONs.
- Re-entry only (plan.md §6 diagram: change requests/drift findings route
  back to Stage 0/3): `{{prior_artifact}}` (the last drafted
  conflict-resolution record, if any) and `{{finding}}` (the specific gate
  failure or drift report that triggered this re-entry — e.g. a G11 finding
  from `scripts/validate_conflict_resolution.py`). When both are present,
  revise `{{prior_artifact}}` to address `{{finding}}` specifically; do not
  redraft from scratch and do not touch anything the finding didn't flag.

## Task

Given the two conflicting evidence records, draft the conflict-resolution
record for `{{conflict_id}}`.

### 1. `evidence` — exactly two ids

plan.md §11's worked example and `resolution.disposition_of_other` (singular)
both model a **pairwise** conflict — exactly one winner, one loser. A
three-or-more-evidence conflict would leave `disposition_of_other` unable to
say which loser it describes, so the schema rejects it (exactly 2 items,
unique).

List the two conflicting evidence ids. Do not invent an id that doesn't
correspond to real evidence (G11 cross-references at approve time).

### 2. `status` — `resolved` or `unresolved`

- `"resolved"` — you can determine which evidence record is the authority
  and which is the loser. Requires `resolution` (schema if/then).
- `"unresolved"` — the conflict is surfaced for a human but not yet
  resolved. No `resolution` field. This is a legitimate Stage 3 output —
  surfacing the conflict for a human, not rejecting it before it's even
  staged.

### 3. `resolution` — required when `status` is `"resolved"`

- `selected_authority` — must be one of the ids listed in this record's own
  `evidence` array (G1b, checked at approve time). The evidence record that
  wins.
- `disposition_of_other` — the losing evidence's `semantic_disposition`
  after resolution. Same vocabulary as `docs/evidence-schema.json`'s own
  `semantic_disposition`: `required`, `incidental`, `bug-compat`, or
  `unspecified`.
- `rationale` — why the selected authority wins and the other loses. Not
  just "E-0201 is better" but the actual reason a human reviewer would
  accept the resolution.

## Schema

`docs/conflict-resolution-schema.json` — validate against this exactly.
`additionalProperties`: false throughout (including nested objects); do not
add fields it doesn't declare, however useful they seem.

## Filename and conflict_id

`conflict_id` must equal the eventual filename stem exactly:
`{{conflict_id}}` — the caller derives the filename from it, not the other
way around. Conflict-resolution records are **workspace-level**, not
crate-scoped (plan.md §11: conflicts arise between evidence records, which
aren't owned by any one crate).

## review block

Omit the `review` field entirely. Your output is a draft
(`scripts/review_checkpoint.py stage_draft` takes it as-is); `review` is
added only by `approve`, from an explicit human-supplied reviewer. Do not
guess a reviewer name or date, and do not claim a review happened.

## Write set

The conflict-resolution record lands in the workspace-level
`specs/_conflicts/` directory — a `protected_roots` path. That directory is
written only through this draft → `approve` path, never by hand and never
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
