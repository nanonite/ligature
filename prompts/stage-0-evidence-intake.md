# Stage 0 — evidence intake

One-shot template. Structured-output contract: output only the JSON object
for a single evidence record, no markdown fence, no prose before or after.

Chainlink #20 has landed as a real JSON Schema (`docs/evidence-schema.json`,
enforced by `scripts/validate_evidence.py`). The output contract below
matches it field-for-field — if the two ever disagree, the schema is
authoritative, not this prose copy.

## Output contract

```json
{
  "schema_version": "1.0",
  "id": "E-<sequential>",
  "kind": "requirement | source-artifact | observed-behavior | test | comment",
  "claim": "<the proposition this evidence supports, in one sentence — not just a hash+location. A hash and a file path identify bytes, not the claim being relied on.>",
  "origin": {
    "repository": "<from the project descriptor's port_source.repository if mode: port, otherwise this project's own repo>",
    "commit": "<commit hash>",
    "symbol": "<function/method/class name>",
    "path": "<file path>",
    "content_hash": "sha256:<...>",
    "line_hint": "<start>-<end>"
  },
  "semantic_disposition": "required | incidental | bug-compat | unspecified",
  "lifecycle": "accepted | aspirational | deferred | rejected | out-of-scope",
  "confidence": "high | medium | low",
  "mode": "P | R"
}
```

Emit `id` as the bare `E-<sequential>` value shown above (`pipeline.py`'s
`draft`/staging step derives the target filename separately) — do not
invent a `review` block: evidence records never carry one (plan.md §7.2's
human-checkpoint list names "evidence-conflict resolution," not evidence
itself), and `additionalProperties: false` rejects one outright.

## Inputs

- `{{source_material}}` — the code, comment, test, requirement doc, or
  observed-behavior trace this evidence is drawn from.
- `{{project_descriptor}}` — this project's `schemas/project-descriptor.schema.json`
  instance. Read `mode` (`greenfield` or `port`) to decide `mode: P | R`
  below, and `port_source` (if present) for `origin.repository`.
- Re-entry only: `{{prior_artifact}}` (the previously drafted evidence
  record, if revising) and `{{finding}}` (the conflict or gap that
  triggered re-entry — e.g. an unresolved evidence conflict, plan.md §11).

## Task

1. **`claim`** is required and is the whole point of this record — state
   the proposition, not a description of where it came from. "pop_ready
   returns None only when no task has deadline <= now" is a claim. "found
   in queue.cpp lines 118-160" is not — that's `origin`, not `claim`.
2. **`semantic_disposition`** (`required | incidental | bug-compat |
   unspecified`) and **`lifecycle`** (`accepted | aspirational | deferred |
   rejected | out-of-scope`) are independent axes — `aspirational` and
   `deferred` are both *lifecycle* values, not a disposition/lifecycle
   pair, so never place one in the other field. A claim can be
   `semantic_disposition: bug-compat` (a legacy defect being preserved,
   not a requirement) while its `lifecycle` is separately `deferred` (not
   yet accepted into the pipeline) — the disposition says what *kind* of
   authority the claim has if accepted; the lifecycle says whether it
   *has* been accepted yet. Neither field's value ever substitutes for
   the other's.
3. **`mode`**: `P` (differential-testing / port track) only when the
   project descriptor's `mode` is `port` **and** this evidence originates
   from the foreign-language source being ported. `R` (requirements-governed)
   otherwise. Do not default to `R` without checking the descriptor.
4. If `{{finding}}` is present, this record is a revision of
   `{{prior_artifact}}` addressing that specific conflict — do not redraft
   `origin` or `claim` unless the finding is specifically about them.
5. `confidence` reflects your own certainty in `claim`, not in the
   `origin` bytes being correctly captured (that's mechanical — either the
   hash matches or it doesn't).

## Not this template's job

Conflict *resolution* (`plan.md` §11's `conflict_id`/`resolution` shape) is
a separate, human-authored artifact — this template only produces the
evidence record itself, one record, one piece of source material. Do not
attempt to resolve a conflict with another evidence record even if you can
see one; surface it as `{{finding}}` for the next stage instead.
