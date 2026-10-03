# Stage 3 (closure, Stage 8C) — degradation-record drafting

One-shot template, invoked as `draft 3 degradation-drafting <target>`.
Structured-output contract, not an interactive session: the caller
(`scripts/pipeline.py draft`) parses stdout directly and runs it
through `scripts/validate_closure.py`'s immediate G1a/G1b feedback before
it's ever staged for human review. It proposes a **ceiling** — the strongest
thing a cluster whose closure condition does not hold may claim — never the
condition itself, and never the review of it.

A degradation record is not a passing grade. `gate g14` releases a cluster
as `degraded` (never `closes`) under one, and only once the record itself
is accepted: its `review` block must match an entry the sanctioned
`approve` path wrote to `ci/results/review_log.jsonl`, AND a `ratified`
human ruling must cover the record's exact bytes in
`ci/results/human_rulings.jsonl` (chainlink #94/#97, the same two guards
`accept-promotion` runs for chainlink #82). Everything below is therefore
a proposal a human reviews, not a claim that takes effect on output.

## Output contract

Output **only** the JSON object for the degradation record. No markdown
code fence, no explanation before or after, no partial output if you are
unsure — in that case output nothing and the caller treats an
empty/unparseable response as a hard failure to retry or escalate, never
as an accepted shortfall.

```json
{
  "schema_version": "1.0",
  "cluster": "<{{cluster}}, verbatim>",
  "failed_conditions": ["<closure condition key that does not hold>"],
  "affected_edges": ["<boundary_id or bridge_id the shortfall lands on>"],
  "ceiling": "documented | tested | harness-tested | assumed | per-instantiation | human-risk-acceptance",
  "capability_gap": "CG1 | CG2 | CG3 | CG4 | CG5 | CG6",
  "tracking_issue": "<e.g. chainlink:713>"
}
```

`capability_gap` is optional — omit it when the shortfall is ordinary
project debt rather than one of plan.md §3's permanent gaps; do **not**
emit a `review` field: see "review block" at the end of this document.

## Inputs

- `{{cluster}}` — the target cluster id, precomputed by the caller. Must
  equal the eventual filename stem with `.degradation` stripped (G1b,
  `scripts/validate_closure.py`) and the `cluster` of the closure profile
  it sits beside.
- Re-entry only (plan.md §6 diagram: change requests/drift findings route
  back to Stage 0/3): `{{prior_artifact}}` (the last drafted record for
  this cluster, if any) and `{{finding}}` (the specific gate failure that
  triggered this re-entry — the `gate g14` finding naming the closure
  condition that does not hold, or the `validate-closure` G17 finding
  naming the missing record). When both are present, revise
  `{{prior_artifact}}` to address `{{finding}}` specifically; do not
  redraft from scratch and do not touch anything the finding didn't flag.

## Task

Given the cluster and the finding that triggered this draft, draft the
degradation record for `{{cluster}}`.

### 1. `failed_conditions` — exactly the conditions that do not hold

The enum is **exactly the seven keys of `docs/closure-profile-schema.json`'s
`conditions` block** — nothing else may appear:

`single_verifier_system`, `owning_verifier`, `protocol_class_all_pairwise`,
`unresolved_indirect_calls_at_or_above_medium`,
`generic_callees_type_universal_or_creusot_owned`,
`transitive_assumptions_within_policy`, `scc_wellfoundedness_discharged`.

Name only conditions the finding shows failing. G17 checks both directions
between this list and the profile: naming a condition the profile declares
as holding is a stale excuse (an error, and `gate g14` will refuse the
record), and a condition the profile declares as failing with no record
covering it is an undeclared degradation (also an error). One exception
(chainlink #86): `generic_callees_type_universal_or_creusot_owned` may be
named beside a `true` declaration as the tracking record plan.md §3
requires of a CG3 gap — that is the one case where the profile says the
condition holds and this record still names it.

A record can excuse a *condition* and nothing else: no achieved record, no
proof, no missing evidence is ever in this vocabulary, and `gate g14` keeps
those blocking with a record present (plan.md §4).

### 2. `affected_edges` — the edges the shortfall lands on

At least one `boundary_id` or `bridge_id` from the cluster's own
`specs/_boundaries/` / `specs/_bridges/` artifacts — the edges the failing
condition actually touches. This array is non-empty by construction: a
degradation with no located edge is an unfalsifiable claim about the whole
cluster, which is exactly what plan.md §4 refuses. Never invent an edge id
to fill it: an id that names no real interaction or bridge is a
fabrication, and nothing downstream cross-references it for you.

### 3. `ceiling` — the strongest permitted claim

The ceiling is a **limit on the claim, not a verification method**. From
plan.md §3's Ceiling column and §8.1's vocabulary:

- `documented` — weakest: "we wrote it down". Use only when nothing
  stronger is true.
- `tested` / `harness-tested` — a bounded amount of checking backs it;
  CG1 (cross-verifier composition) is `harness-tested`.
- `assumed` / `per-instantiation` / `human-risk-acceptance` — the
  remaining vocabulary; CG3 (generics under Kani) is `per-instantiation`,
  CG5 (non-falsifiable assumptions) is `human-risk-acceptance`.

Pick the ceiling the evidence actually supports, not the one the finding
would like. Overstating a ceiling is the same defect as understating a
condition.

### 4. `tracking_issue` — where the accepting stops being permanent

plan.md §3's response to every capability gap is "degradation record +
tracking issue": this record says what was accepted, the issue is where
accepting stops being permanent. Pattern `^[a-z]+:[A-Za-z0-9_.-]+$`
(e.g. `chainlink:713`). Name a real, open issue that tracks the shortfall;
`chainlink:999999`-shaped placeholders that name nothing are fabrication.

## Schema

`docs/degradation-record-schema.json` — validate against this exactly.
`additionalProperties`: false throughout; do not add fields it doesn't
declare, however useful they seem.

## Filename

`specs/_closure/<cluster>.degradation.json` — the caller derives the
filename from `{{cluster}}`, not the other way round. Degradation records
are **workspace-level**, not crate-scoped: a cluster spans crates by
construction (plan.md §4), so filing its record under one crate would
misrepresent what closed.

## review block

Omit the `review` field entirely. Your output is a draft
(`scripts/review_checkpoint.py stage_draft` takes it as-is); `review` is
added only by `approve`, from an explicit human-supplied reviewer. Do not
guess a reviewer name or date, and do not claim a review happened. After
`approve` and a `record-ruling` verdict of `ratified`, the record is
accepted — and re-running `record-ruling` is required after **any** later
edit, because the ruling is pinned to the record's exact bytes and an edit
as small as a ceiling change stops covering it (chainlink #94/#97).

## Write set

The degradation record lands in the workspace-level `specs/_closure/`
directory — a canonical pipeline location the project descriptor's
`write_set` accounts for (workspace-level `specs/_<kind>/`), never a
free-form write. `write_set` (`allowed_roots` / `protected_roots`) is what
declares where this workspace may be written at all, and
`ligature write-set-check` reports any file outside `allowed_roots` as
`out-of-set`, and any file hand-written into this directory -- which is a
protected location the pipeline owns -- as a blocking `protected-write`
violation. Write only through this draft → `approve` path;
if a finding implies writing anywhere else, surface it instead of writing.
