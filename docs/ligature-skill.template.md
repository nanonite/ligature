---
name: ligature
version: "1.0"
description: Drive the Ligature reliance-graph pipeline from an agent: read project state with `ligature status`, get the one recommended next action with `ligature check next`, perform it, and repeat. Ligature is a passive oracle; the agent drives the loop, and approve/promote remain explicit human checkpoints.
triggers:
  - /ligature
  - ligature
  - reliance graph
  - phase loop
---

# Ligature Skill

Ligature is a **passive oracle** for a reliance-graph pipeline. It does not
run the phase loop for you, and it never will: there is no `ligature start`
or any other supervisory verb (`docs/cli-contract.md` §1). The agent reading
this skill drives the loop. The binary answers two questions and nothing
more:

- `ligature status` — what is the project's state right now?
- `ligature check` / `ligature check next` — what is the one recommended
  next action?

This skill is installed and versioned by `ligature init`. Its authority
section below is canonical product content and is **hash-pinned**: the
installed ownership manifest (`ci/manifest/installation.json`) records the
authority hash, and `ligature doctor` verifies it. Editing the region
between the two markers is a managed-file conflict, not a configuration
change.

<!-- LIGATURE-AUTHORITY-BEGIN __LIGATURE_AUTHORITY_HASH__ -->

## Authority boundary (canonical — do not edit)

This region is product-owned canonical content, hash-pinned in the
ownership manifest and verified by `ligature doctor`. Editing it is a
managed-file conflict; the loop below is the only sanctioned way to make
progress.

1. **LLM output is advisory.** Any `draft` proposal from a model is
   non-normative until a human reviews and approves it. A model's
   assertion is not evidence of anything.
2. **Human approval remains human.** `approve` and promotion require a
   named `--reviewer`; neither `status` nor `check` can approve, promote,
   or record a decision on a human's behalf.
3. **Witnesses and differential tests can falsify, never prove.** An
   agreeing witness run or oracle comparison is evidence the expectation
   was not falsified in that instance, not a proof of universal
   correctness or equivalence. A witness observation is never an
   assurance dimension.
4. **Generated and read-only artifacts are not hand-edited.** Reports,
   feature ledgers, contact sheets, witness renderings, and gate outputs
   are regenerated from source; editing one by hand is not a change to the
   project's state.
5. **There is no supervisory start.** The agent drives the loop; the
   binary stays a passive oracle. `check` is read-only and never runs a
   writing operation on its own — it only recommends one through
   `next_action.action_id`.

## The loop

1. **Read state.** Run `ligature status` (human-readable) or
   `ligature status --json` (the versioned project-state document).
2. **Get the one next action.** Run `ligature check next` (focused) or
   `ligature check --json` and read `next_action`. The stable identifier
   is `action_id`, never the advisory `command` string.
3. **Perform exactly that action, yourself.**
   - `kind: automated-command` — the `action_id` is one of
     `refresh-c-static`, `refresh-bridge-checks`, `refresh-witness`, or
     `author-interaction`. Run the named command. `check` did not run it.
   - `kind: human-decision` — stop and ask the human. Do not cross an
     approve/promote checkpoint on your own.
   - `kind: external-authority-missing` / `backend-unavailable` — report
     the missing authority or backend; do not fabricate a result.
4. **Close the loop.** Run `ligature check next` again. Repeat until
   `next_action` is null (`check` found nothing more to automate).
5. **Stop at every human checkpoint.** `approve` and promotion are
   explicit, human-triggered invocations. Never infer or synthesize an
   approval, never treat an agreeing witness as proof, and never hand-edit
   a generated or read-only artifact.

## Commands at a glance

| command | purpose |
|---|---|
| `ligature status [--json]` | current project state (artifact lifecycles, obligations, clusters, findings, gate integrity) |
| `ligature check [--json] [next]` | read-only consolidated gate run + exactly one recommended next action |
| `ligature validate <kind> <target>` | deterministic per-artifact check (G1a/G1b/G2+) |
| `ligature gate <gate-id> [args...]` | deterministic cross-artifact gate |
| `ligature draft <kind> <target>` | Stage 0/3 one-shot LLM draft (advisory; human review required) |
| `ligature approve <op> <target...>` | human checkpoint (requires `--reviewer`) |
| `ligature report <report-id>` | generated review projections (ledger, contact sheet); regenerate, do not hand-edit |
| `ligature doctor` | capability manifest + installed-file/version diagnostics |
| `ligature migrate [--upgrade\|--prune\|--force <path>]` | recover from managed-file conflicts and version skew |

## Managed files

`ligature init` writes a versioned ownership manifest at
`ci/manifest/installation.json`. Files under `.ligature/` and this skill
are **managed**: `ligature init` never overwrites a locally modified
managed file, and `ligature migrate` is the explicit recovery path.
`project-descriptor.json` and `docs/reliance-policy.md` are **user-owned
templates**: `init` creates them once if absent and never overwrites them.

<!-- LIGATURE-AUTHORITY-END -->
