# Stage 3 (closure, Stage 8C) — closure-profile drafting

One-shot template, invoked as `draft 3 closure-profile-drafting <target>`.
Structured-output contract, not an interactive session: the caller
(`scripts/pipeline.py draft`) parses stdout directly and runs it
through `scripts/validate_closure.py`'s immediate G1a/G1b feedback before
it's ever staged for human review. The profile **declares** what the
cluster's transitive closure looks like; it does not decide it.

Every condition you declare is a claim to be checked, not an input to be
trusted: `gate g14` recomputes each one from the closure's own manifests,
ledgers and bridges and rejects disagreement in BOTH directions, and
`validate-closure` checks this profile against its degradation record
(G17). Declare what the workspace's artifacts actually show.

## Output contract

Output **only** the JSON object for the closure profile. No markdown code
fence, no explanation before or after, no partial output if you are unsure
— in that case output nothing and the caller treats an
empty/unparseable response as a hard failure to retry or escalate, never
as a declaration about a cluster.

```json
{
  "schema_version": "1.0",
  "cluster": "<{{cluster}}, verbatim>",
  "closure_kind": "deductive | bounded | partial",
  "work_packages": ["<work-package manifest id, e.g. WP-SCHED-001>"],
  "conditions": {
    "single_verifier_system": true,
    "owning_verifier": "creusot | kani | verus",
    "protocol_class_all_pairwise": true,
    "unresolved_indirect_calls_at_or_above_medium": 0,
    "generic_callees_type_universal_or_creusot_owned": true,
    "transitive_assumptions_within_policy": true,
    "scc_wellfoundedness_discharged": "not-applicable"
  }
}
```

All seven `conditions` keys are required and `additionalProperties` is
false inside the block — no aliases, no omissions. Do **not** emit a
`review` field: see "review block" at the end of this document.

## Inputs

- `{{cluster}}` — the target cluster id, precomputed by the caller. Must
  equal the eventual filename stem (G1b, `scripts/validate_closure.py`).
- Re-entry only (plan.md §6 diagram: change requests/drift findings route
  back to Stage 0/3): `{{prior_artifact}}` (the last drafted profile for
  this cluster, if any) and `{{finding}}` (the specific gate failure or
  drift report that triggered this re-entry — e.g. a `gate g14`
  recomputation disagreement, or a `validate-closure` G17 finding). When
  both are present, revise `{{prior_artifact}}` to address `{{finding}}`
  specifically; do not redraft from scratch and do not touch anything the
  finding didn't flag.

## Task

Given the cluster and the finding that triggered this draft, draft the
closure profile for `{{cluster}}`.

### 1. `closure_kind` — the uniform claim about the whole closure

From plan.md §4's table, and matching the evidence kinds actually present
across the transitive closure:

- `deductive` — Creusot/Verus-owned (universal over inputs, modulo SMT
  completeness and trusted specs).
- `bounded` — Kani-owned (holds only within unwind/harness bounds and
  enumerated monomorphizations).
- `partial` — the cluster cannot achieve full deductive closure and says
  so (chainlink #85) instead of rounding the claim up or down.

`deductive` and `bounded` are claims about EVERY obligation in the
closure; `partial` claims strictly less than either, never more. A Kani
result anywhere in the closure makes `deductive` a G17 hard error, not a
rounding-up.

### 2. `work_packages` — what the cluster is made of

The `ci/manifest/<id>.json` manifests whose definition of done makes up
this cluster — at least one. Derive membership from the workspace's own
manifests, never from the cluster's name; the transitive closure G14
computes starts exactly here.

### 3. `conditions` — declared, then recomputed

- `single_verifier_system` — one verifier system across the whole closure
  (false is plan.md §3's CG1 case).
- `owning_verifier` — the cluster's own verifier: `creusot`, `kani` or
  `verus`, matching the manifests' declared harnesses.
- `protocol_class_all_pairwise` — every bridge in the closure pairwise.
- `unresolved_indirect_calls_at_or_above_medium` — the count
  `scripts/gate_r1_g16.py` reports, an integer ≥ 0 (this one is read from
  the R1/G16 gate, never recomputed here).
- `generic_callees_type_universal_or_creusot_owned` — plan.md §3's CG3.
  Declare it `true` only when the closure's own records establish it;
  `gate g14` computes it from the workspace's work-package artifacts and
  refuses a declaration the artifacts contradict (chainlink #86/#93).
- `transitive_assumptions_within_policy` — every assumption anywhere in
  the closure satisfies the cluster's entry requirements.
- `scc_wellfoundedness_discharged` — `not-applicable` when the closure is
  acyclic (claiming `true` there would be a vacuous discharge, which
  §8.5's own objection to a vacuous OK rejects); `true` when a cycle
  exists, in which case the profile also needs an `scc_discharges` entry
  per non-trivial SCC.

### 4. `scc_discharges` — omit unless the closure has a cycle

Optional. One entry per non-trivial strongly-connected component when
`conditions.scc_wellfoundedness_discharged` is `true`; absent (or
`not-applicable`) for an acyclic closure. An entry naming an SCC that does
not exist is rejected as stale.

## Schema

`docs/closure-profile-schema.json` — validate against this exactly.
`additionalProperties`: false throughout; do not add fields it doesn't
declare, however useful they seem.

## Filename

`specs/_closure/<cluster>.json` — the caller derives the filename from
`{{cluster}}`, not the other way round. Closure profiles are
**workspace-level**, not crate-scoped: a cluster spans crates by
construction (plan.md §4), so filing its profile under one crate would
misrepresent what closed. A degradation record for the same cluster, if
any, is `specs/_closure/<cluster>.degradation.json` and must agree with
this profile on every condition (G17, both directions).

## review block

Omit the `review` field entirely. Your output is a draft
(`scripts/review_checkpoint.py stage_draft` takes it as-is); `review` is
added only by `approve`, from an explicit human-supplied reviewer. Do not
guess a reviewer name or date, and do not claim a review happened.

## Write set

The closure profile lands in the workspace-level `specs/_closure/`
directory — a canonical pipeline location the project descriptor's
`write_set` accounts for (workspace-level `specs/_<kind>/`), never a
free-form write. `write_set` (`allowed_roots` / `protected_roots`) is what
declares where this workspace may be written at all, and
`ligature write-set-check` reports any file outside `allowed_roots` that
no declaration accounts for. Write only through this draft → `approve`
path; if a finding implies writing anywhere else, surface it instead of
writing.
