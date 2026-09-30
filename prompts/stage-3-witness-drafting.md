# Stage 3 (witness / §16) — witness specification drafting

One-shot template. Structured-output contract, not an interactive session:
the caller (`scripts/pipeline.py draft`) parses stdout directly and runs it
through `scripts/validate_witness.py`'s immediate G1a/G1b feedback before
it's ever staged for human review. Counterpart to
`stage-3-boundary-drafting.md` for the witness layer (plan.md §16.1,
chainlink #27/#31).

## Output contract

Output **only** the JSON object for the witness specification. No markdown
code fence, no explanation before or after, no partial output if you are
unsure — in that case output nothing and the caller treats an
empty/unparseable response as a hard failure to retry or escalate, never as
an empty expectation.

```json
{
  "schema_version": "1.0",
  "witness_id": "<{{witness_id}}, verbatim>",
  "concept": "<PascalCase concept>",
  "query": "<snake_case query method name>",
  "fixture": {
    "fixture_id": "FX-<...>",
    "seed": 0,
    "description": "<what this fixture establishes>"
  },
  "renderer": "<snake_case renderer name>",
  "expectation": {
    "renderer": "<same snake_case renderer name>",
    "coverage_region": "full-grid | bottom-row | corridor | single-cell | project-defined",
    "value_distribution": "must-vary | constant-allowed",
    "fixture_family": "FX-<...>"
  },
  "determinism": {
    "value_hash": "sha256:<64 hex chars>",
    "claim": "byte-identical-across-runs | byte-identical-on-declared-platforms",
    "platforms": ["<rust target triple>"]
  },
  "output": {
    "path": "docs/witnesses/<snake_case(concept)>.<query>.svg",
    "render_hash": "sha256:<64 hex chars>",
    "renderer_actual": "<same snake_case renderer name>"
  }
}
```

Do **not** emit a `review` field: see "review block" at the end of this
document.

## Inputs

- `{{witness_id}}` — the target artifact id, precomputed by the caller.
  Must be unique across the workspace (G1b). Deliberately NOT derived from
  the filename — plan.md §16.1's own example abbreviates TaskQueue to
  `W-TQ-…`, so a derivation rule would either reject the plan's example or
  invent an abbreviation scheme nobody declared.
- `{{concept}}` — the PascalCase concept name, matching the concept-to-code
  spec's own `concept` field.
- `{{query}}` — the queried method's name, as embedded in the concept
  spec's own `rust_sig`. Must resolve to a query with `pure: true` in the
  concept's spec (G2, checked at approve time).
- `{{concept_spec}}` — the concept-to-code spec JSON for `{{concept}}`
  (queries, commands, constraints).
- Re-entry only (plan.md §6 diagram: change requests/drift findings route
  back to Stage 0/3): `{{prior_artifact}}` (the last drafted witness
  specification, if any) and `{{finding}}` (the specific gate failure or
  drift report that triggered this re-entry — e.g. a G18/G19/G20 finding).
  When both are present, revise `{{prior_artifact}}` to address
  `{{finding}}` specifically; do not redraft from scratch and do not touch
  anything the finding didn't flag.

## Task

Given the concept spec, draft the witness specification for
`{{concept}}.{{query}}`.

### 1. `fixture` — the determinism contract's foundation

- `fixture_id` — pattern `^FX-[A-Z0-9]+(?:-[A-Z0-9]+)*$`. A fixture family
  is DEFINED BY the witnesses that declare it; there is no separate
  registry artifact.
- `seed` — required even when the fixture is fully deterministic and ignores
  it: a witness whose seed is implicit cannot be reproduced by someone who
  did not write it, and reproduction is the entire determinism contract.
- `description` — what this fixture establishes, in restricted English.

### 2. `renderer` — declared, never substituted

The renderer this witness DECLARES. `output.renderer_actual` records what
actually ran, and the two are compared — a renderer silently substituting a
degraded fallback (a text strip standing in for a field plot) is the failure
this pairing exists to make impossible to hide. All three of `renderer`,
`expectation.renderer`, and `output.renderer_actual` must be the same value
(G1b, checked structurally at draft time).

### 3. `expectation` — DECLARE intent, do not infer it (plan.md §16 A2 correction)

- `coverage_region` — which part of the fixture's domain the feature is
  expected to be defined over. "All-constant output" is correct for a
  single-cell fixture and a defect for a full-field one, so a global anomaly
  detector both false-positives and false-negatives. Declare what you
  expect, don't infer it from the rendering.
- `value_distribution` — `must-vary` when a constant result is a defect
  here; `constant-allowed` when it isn't. Checked by G20 against the
  canonical result's own recomputed value_domain, never against a heuristic
  over the rendering.
- `fixture_family` — the family this fixture belongs to. Every witness
  declaring the same family must declare the same `coverage_region` (G1b,
  checked at approve time).

### 4. `determinism` — the NORMATIVE contract

- `value_hash` — over the canonical result (docs/witness-result-schema.json),
  computed by `scripts/witness_result.py`'s documented serialization.
  Renderer-independent by construction. This is the field G19 regenerates
  and compares — pin the data, not the projection.
- `claim` — `byte-identical-across-runs` or
  `byte-identical-on-declared-platforms`. The second value exists because
  `platforms` is a list: a witness that only claims stability on the
  platforms it names is making a weaker and often truer claim.
- `platforms` — Rust target triples, same pattern as an interaction's
  `realization.config_scope.target`. At least one, unique.

### 5. `output` — the generated rendering

- `path` — must be `docs/witnesses/<snake_case(concept)>.<query>.svg`
  (G1b, checked structurally at draft time). Constrained so a witness
  cannot quietly point at a hand-drawn picture somewhere else.
- `render_hash` — change-tracking only; never gates promotion (plan.md
  §16.5). SVG bytes move with float formatting, locale, attribute order and
  library version while the computed value does not.
- `renderer_actual` — what actually ran. Must equal `renderer` (G1b).

## Schema

`docs/witness-spec-schema.json` — validate against this exactly.
`additionalProperties`: false throughout (including nested objects); do not
add fields it doesn't declare, however useful they seem.

## Filename

The eventual filename must equal `<snake_case(concept)>.<query>.json`
exactly, flat inside the crate's `_witnesses/` directory. The snake_case
conversion uses concept-to-code's OWN rule (vendor/concept-to-code/
emit_stubs.py:snake_case), copied rather than re-derived — plan.md §2
records what re-deriving it cost last time (HTTPClient -> h_t_t_p_client).
`witness_id` in the JSON body must equal `{{witness_id}}` verbatim.

## review block

Omit the `review` field entirely. Your output is a draft
(`scripts/review_checkpoint.py stage_draft` takes it as-is); `review` is
added only by `approve`, from an explicit human-supplied reviewer. Do not
guess a reviewer name or date, and do not claim a review happened.

## Write set

The witness specification lands in the crate's `specs/_witnesses/` directory
— a `protected_roots` path. That directory is written only through this
draft → `approve` path, never by hand and never by writing the file
directly: the project descriptor's `write_set` declares the spec trees
off-limits to free-form writes, and `ligature write-set-check` reports any
file that appears outside `allowed_roots` (the crate `src/` and `tests/`
trees) without a declaration accounting for it. If a finding implies
writing anywhere else, surface it instead of writing.
