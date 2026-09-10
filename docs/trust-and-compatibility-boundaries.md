# Trust and compatibility boundaries — v1.0

Chainlink #55. What is promised, what is not, and who may write what.

## 1. What is stable public API

- The grammar in `docs/cli-contract.md` (top-level verbs, artifact-kind /
  gate-id / operation / report-id vocabularies, the legacy-alias mapping).
- `schemas/project-state.schema.json` (`ligature status --json`) and
  `schemas/consolidated-check.schema.json` (`ligature check --json`), each
  independently versioned by their own `schema_version`.
- `docs/exit-code-contract.md`'s five codes and their precedence order.
- Every schema under `docs/*-schema.json` and `schemas/*.schema.json` that
  a `validate <kind>` command checks an artifact against — these are
  already versioned (`schema_version` fields, e.g.
  `docs/closure-profile-schema.json`'s `"const": "1.0"`) and that practice
  continues.

## 2. What is explicitly NOT stable public API

- Python module paths and names under `scripts/*.py`.
- Individual gate *implementation* function/file names (`cmd_gate_g14`,
  `gate_g14_workspace`, etc.) — the gate-id `g14` is public; where its code
  lives is not.
- The internal-operation flat commands in `docs/cli-contract.md` §7
  (`extract-c-static`, `check-bridges`, `render-witness`) beyond the
  transition window described there.
- `docs/implementation-inventory.json`'s own field values for `module` /
  `func` (informational cross-references, explicitly not frozen — see the
  schema's own description).

## 3. Which output schemas are versioned compatibility contracts

`project-state` (1.0) and `consolidated-check` (1.0), plus every artifact
schema already in `docs/`/`schemas/`. A breaking change to any of these
requires a new `schema_version` and a migration note; #59 owns the
specific migration mechanics for the assumption registry, not this
document, but the versioning discipline here applies uniformly.

`docs/implementation-inventory.json` + its schema is versioned
(`inventory_version`) but is **not** a compatibility contract in the same
sense — it describes this repository's own current contents and is
expected to change every time a runtime file is added, removed, or
reclassified. Its stability guarantee is "drift-tested," not "frozen."

## 4. Which commands are read-only

`status`, `check`, every `validate <kind>`, every `gate <gate-id>`, every
`report <report-id>`, and `doctor`/`version` (once implemented). None of
these write to the workspace under any circumstances. This is enforced
today by construction (none of the corresponding `cmd_*` functions call
any write path) and will be schema-enforced going forward for `check` via
`consolidated-check.schema.json`'s required `mutated_workspace: false`
field.

## 5. Which operations may write, and under what authority

| operation | writes | authority |
|---|---|---|
| `draft <kind>` | a new draft artifact | none required — Stage 0/3, LLM-proposed, explicitly non-normative until reviewed |
| `approve <op>` | promotes a draft (attaches `review`) | `--reviewer` (required, human identity) |
| `init` (#58) | installs owned files into a target repo | operator running the command; must not silently overwrite user-owned content |
| `migrate` (#59) | rewrites legacy references to registry form | operator running the command, staged (phase A-D per #59), never rewrites a reviewed artifact without its own human checkpoint |

No other command writes. `report <report-id>` generators
(`generate-feature-ledger`, `generate-contact-sheet`) write only to
`ci/results/`/`docs/witnesses/_contact_sheet.svg` — generated,
read-only-by-humans projections, never a reviewed/promoted artifact.

## 6. `status`/`check` cannot approve or promote

Neither command has, or will have, any code path that attaches a `review`
block, writes to a `_promotions/` receipt, or otherwise changes an
artifact's lifecycle. `consolidated-check.schema.json`'s `next_action` can
only ever *name* a command to run next (`kind: automated-command`'s
`command` string) — the schema has no field through which `check` itself
claims to have performed that action. Producing a recommendation is not
performing it.

## 7. LLM recommendations are advisory

Any finding or next-action entry whose `authority` is `llm-advisory`
(`consolidated-check.schema.json`) carries no blocking weight on its own —
it cannot be the sole reason `result.exit_code` is non-zero. Only
`mechanized-gate` and `human-decision-pending` authorities can drive a
blocking exit code; `llm-advisory` findings are informational until a
mechanized gate or a human confirms them. This mirrors plan.md's own
Stage 0/3 discipline: LLM output is a draft proposal, never itself a
passing check.

## 8. Human checkpoints remain human

`approve <op>` requires `--reviewer` on every path (plan.md §7.2); no
future `check`/`status` output can substitute for it. A `human_decision`
entry in `project-state.schema.json` moves from `pending` to `recorded`
only through a real human review event (which `approve`/`accept-promotion`
already record via `review`/`--reviewer`), never through a mechanized gate
run reclassifying it on its own.

## 9. Witnesses and differential agreement falsify, never prove

Per plan.md §16 (G18/G19/G20) and the codebase's own repeated discipline
(`witness_renderer.py`: "fail loudly, never substitute"; G19: regenerate
and compare `value_hash`, never `render_hash`): a witness or a differential
test that AGREES with an expectation is evidence the expectation was not
falsified in this instance, not a proof of universal correctness or
equivalence. `project-state.schema.json` keeps `generated_observations`
(witness summaries: determinism, degeneracy) structurally separate from
`obligations[].achieved_assurance` for exactly this reason — nothing in
either schema lets a witness observation stand in for an assurance
dimension.

## 10. Binary attestation is specified here, implemented under #57

`project-state.schema.json`'s `binary_identity` object (`verified`,
`content_hash`, `version`) is the shape #57's `ligature version --verify`
and `doctor` are expected to populate. This task does not implement that
verification — `verified: "unknown"` is the only honest value any current
invocation (`python3 scripts/pipeline.py`, no build hash to check) can
report, and every example in `schemas/examples/` reflects that: the
`empty` example uses `"unknown"`; the `mixed` example uses `"true"` only
as an illustration of what a real #57 verification result would look
like, not a claim that verification exists today.

## 11. Assumption-registry migration is specified under #59

Nothing in this task's schemas or CLI contract invents assumption-registry
resolution, dual-resolution of legacy `boundary_id`/`tracking_issue`/
`assumption_hash` composites, or the phase A-D migration sequence — all of
that is #59's own scope. Where `project-state.schema.json` or
`consolidated-check.schema.json` need to reference an assumption
(currently: nowhere directly — obligations reference `obligation_id`, not
assumption identity), #59 extends these schemas with its own minor version
bump rather than this task guessing its shape now.

## 12. Ambiguities explicitly deferred

- Exact `required_assurance`/`achieved_assurance` dimension vocabulary
  (`project-state.schema.json`'s `assurance_dimension.dimension` is an
  open string, not a closed enum) — #56 owns closing it once the real
  computation exists to enumerate against.
- Whether `check`'s gate-discovery is purely descriptor-driven or also
  consults installed-manifest state — #56.
- The real shape of `--json`'s relationship to the human-readable form
  (same command, `--json` flag, vs. separate rendering path) — #56.
- Packaged-resource discovery replacing `Path(__file__)` (named directly
  in `docs/implementation-inventory.json`'s `vendored_runtime_assets`
  entry for `vendor/concept-to-code/schemas/spec.schema.json`) — #57.
- The ownership-manifest three-way-comparison mechanics
  (`installation_manifest` here only models the STATE such a manifest
  produces, not how it's computed or stored) — #58.
