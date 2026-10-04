---
name: ligature
version: "1.0"
description: Drive the Ligature reliance-graph pipeline from an agent: read project state with `ligature status`, get the one recommended next action with `ligature check next`, perform it, and repeat. Ligature is a passive oracle; the agent drives the loop, and approve remains an explicit human checkpoint (promote-evidence is the one mechanical exception, for non-normative Stage 0 evidence).
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
2. **Human approval remains human.** `approve` requires a named
   `--reviewer`; neither `status` nor `check` can approve, promote, or
   record a decision on a human's behalf. A `review` block you write into
   an artifact yourself is not an approval either: `accept-promotion`
   reads every accepted artifact's review block back against the
   approval audit log (`ci/results/review_log.jsonl`) and refuses the
   promotion when no `approve` entry matches it. Even a provenanced set
   is not enough on its own: `accept-promotion` also refuses until every
   artifact in it carries a `ratified` human ruling from
   `record-ruling` in `ci/results/human_rulings.jsonl`, and
   `record-ruling` is itself a human checkpoint -- never run it, and
   never run `approve` or `accept-promotion`, on a human's behalf. The
   one exception is
   `promote-evidence`, a mechanical promotion for Stage 0 evidence (which
   carries no `review` block and is non-normative, so it is not a human
   checkpoint) -- it never attaches a `review` block and never substitutes
   for a human decision. Concept specs are not that exception: a concept
   spec's constraint ids and queries are what `validate`'s G2+/witness
   cross-references resolve against, so promoting one is a human
   checkpoint like any other (#105). A `draft` you staged and never got
   promoted is invisible to every one of those checks, and the silence is
   quiet: `validate` reports the affected `applies_to` as merely
   "unverifiable" rather than rejecting it.
3. **Witnesses and differential tests can falsify, never prove.** An
   agreeing witness run or oracle comparison is evidence the expectation
   was not falsified in that instance, not a proof of universal
   correctness or equivalence. A witness observation is never an
   assurance dimension.
4. **Generated and read-only artifacts are not hand-edited.** Reports,
   feature ledgers, contact sheets, witness renderings, and gate outputs
   are regenerated from source; editing one by hand is not a change to the
   project's state.
5. **The write set is binding.** The project descriptor's `write_set`
   declares where you may write implementation files (`allowed_roots` —
   the crate `src/` and `tests/` trees) and which paths are protected
   (`protected_roots` — specs, CI manifests, gate scripts, schemas, policy
   docs, Cargo/build files, harnesses, toolchain pins). Write only under
   `allowed_roots`; never write into `protected_roots`. The pinned
   upstream checkout (`port_source.repository`) is a read-only input, not
   a write target. `ligature write-set-check` reports a file written
   outside the write set as `out-of-set` and a file written into a
   protected root as `protected-write` — both blocking, both a high-severity
   `write-set` finding in `check --json`; `status --json` carries the
   verdict as `write_set.state`. Files that legitimately belong in a
   protected root get there through a pipeline command (`draft` stages a
   `.draft` sibling; `approve`, `accept-promotion` and `accept-policy` are
   human checkpoints you may not run), never by being written by hand. A
   protected file in a location the pipeline writes nothing into — your
   project's own `Cargo.toml`, `.github/`, `scripts/` — is listed as
   `protected (not vouched by any declaration, audit only)` and does not
   block: leave it alone, and do not read a `clean` verdict as covering
   it.
   The one exception is a **sanctioned** protected write — one the issue in
   progress is authorized to make. It is authorized by a recorded,
   issue-scoped capability grant, never by a hand edit: `ligature
   authorize-write --issue <N> --path <path> --op write --issuer <name>`
   records one, and `write-set-check --issue <N>` then reports that write as
   `authorized protected write [grant <id>]` instead of as a violation. If a
   finding needs a protected write and no grant covers it, **stop and report
   it** — ask the human to run `authorize-write`, and pass `--issue <N>` to
   your own write-set check so its verdict is about the same issue. Never run
   `authorize-write` yourself: it is a human checkpoint like `approve`, and
   it is the one command that can turn a `protected-write` finding
   non-blocking. Never hand-edit `ci/results/protected-writes.jsonl` either.
6. **There is no supervisory start.** The agent drives the loop; the
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
     `refresh-c-static`, `refresh-bridge-checks`, `refresh-witness`,
     `author-interaction`, or `promote-evidence`. Run the named command.
     `check` did not run it.
   - `kind: human-decision` — stop and ask the human. Do not cross an
     approve/promote checkpoint on your own.
   - `kind: external-authority-missing` / `backend-unavailable` — report
     the missing authority or backend; do not fabricate a result.
4. **Close the loop.** Run `ligature check next` again. Repeat until
   `next_action` is null (`check` found nothing more to automate).
5. **Stop at every human checkpoint.** `approve` is an explicit,
   human-triggered invocation. Never infer or synthesize an approval,
   never treat an agreeing witness as proof, and never hand-edit a
   generated or read-only artifact. (The one mechanical exception is
   `promote-evidence`, which promotes non-normative Stage 0 evidence and
   is never a human checkpoint -- see command 2 above.)

## Commands at a glance

| command | purpose |
|---|---|
| `ligature status [--json] [--issue N]` | current project state (artifact lifecycles, obligations, clusters, findings, gate integrity, write-set conformance; `--issue N` scopes the write grants the write-set state honors, #114) |
| `ligature check [--json] [next] [--issue N]` | read-only consolidated gate run + exactly one recommended next action; `--issue N` scopes the write grants its write-set finding honors, so all three read-only surfaces agree about the same run (#114) |
| `ligature write-set-check [--json] [--issue N]` | read-only write-set conformance: files outside `allowed_roots` (`out-of-set`) and files written into `protected_roots` that no declaration vouches for (`protected-write`), both blocking; the protected surface reported per declared pattern. `--issue N` also consumes the write grants recorded for that issue (#114) |
| `ligature authorize-write --issue N --path P --op K --issuer <name>` | record one issue-scoped capability authorizing a single sanctioned write into a declared protected root, in `ci/results/protected-writes.jsonl` — the sanctioned-route exception to rule 5 (#114). A human checkpoint in the `human` lane (never run it yourself); the `supervisor` lane needs the identity declared in the descriptor's `write_set.authorized_supervisors`. A grant cannot widen `allowed_roots`, authorize another operation, outlive its TTL, or cover another issue or path |
| `ligature validate <kind> <target>` | deterministic per-artifact check (G1a/G1b/G2+) |
| `ligature gate <gate-id> [args...]` | deterministic cross-artifact gate |
| `ligature draft <stage> <template> <target>` | Stage 0/3 one-shot LLM draft (advisory; human review required). Run `ligature draft --help` for the complete template list — every shipped template, what it drafts, and which command promotes it — rather than guessing a name |
| `ligature approve <op> <target...>` | human checkpoint (requires `--reviewer`); every normative artifact, including a concept spec (#105) |
| `ligature accept-policy --reviewer <name> [--version <name>@<major>.<minor>]` | record a reviewed change to the normative reliance-policy document (`docs/reliance-policy.md`) as the manifest's reviewed base — the explicit accept path for governance drift, analogous to `accept-promotion`. `--version` stamps the document's `Policy version:` marker line (that line and nothing else) and is how the placeholder `init` ships gets replaced; a human checkpoint: ask the human to run it, never run it yourself |
| `ligature promote-evidence <target>` | mechanically promote a staged evidence draft (`evidence/<id>.json.draft`) to its target and record the move — the Stage 0 promotion path (evidence carries no `review` block, so `approve` cannot promote it; no `--reviewer` needed) |
| `ligature record-ruling --reviewer <name> --verdict ratified/rejected --artifact <path>…` | record a human ruling over an exact artifact set — the human-ruling gate `accept-promotion` enforces before it will mint a receipt (#82). A human checkpoint: never run it yourself |
| `ligature record-assurance <work-package> --proof <obligation>=<path>…` | assemble a work package's assurance report from the verifier's own proof certificates and write it to the manifest's `report.emit` path — the achieved side `gate g14` reads (#87). Never hand-write that report: every field is derived from the certificate, the mapping you declare is checked, and the command refuses when the evidence does not establish the obligation |
| `ligature report <report-id>` | generated review projections (ledger, contact sheet); regenerate, do not hand-edit — but `report feature-ledger` refuses (exit 1, file untouched) over a schema-valid assurance report, so run `record-assurance` after it (#88) |
| `ligature doctor` | capability manifest + installed-file/version diagnostics |
| `ligature migrate [--upgrade\|--prune\|--force <path>]` | recover from managed-file conflicts and version skew |

## Managed files

`ligature init` writes a versioned ownership manifest at
`ci/manifest/installation.json`. Files under `.ligature/` and this skill
are **managed**: `ligature init` never overwrites a locally modified
managed file, and `ligature migrate` is the explicit recovery path. The
two witness-renderer scripts `init` installs — `scripts/witness_renderer.py`
and `scripts/xml_escape.py`, the implementation every work-package
manifest's G13 hash-pins — are managed for the same reason: they are the
product's gate-adjacent code, so a local edit is a `CONFLICT` to resolve
with `ligature migrate --force <path>`, never a change to make by hand.
`project-descriptor.json` and `docs/reliance-policy.md` are **user-owned
templates**: `init` creates them once if absent and never overwrites them.
`docs/reliance-policy.md` is also a **normative input**: its on-disk
content is drift-checked against the reviewed hash the ownership manifest
records, so an unreviewed edit makes `doctor`, `check` and `status` report
the installation as drifted. A reviewed change is recorded with
`ligature accept-policy --reviewer <name>` — the recorded base moves
through that command and nothing else, which is what makes the drift
signal trustworthy.

That document also arrives as a template: `init` installs it with its
`Policy version:` line still the `<policy-name>@<major>.<minor>`
placeholder, and both `accept-policy` and `accept-promotion` refuse a
policy that cannot yield a `policy_version` — so no promotion can be
minted until it carries one. `ligature doctor` exits 1 and `check`
reports a blocking `policy-version-marker` finding until it does. **You
cannot fix this yourself**: the document is a `protected_root` (rule 5
above), so writing into it is forbidden to you. Report the finding and ask
the human to run `ligature accept-policy --reviewer <name> --version
<name>@<major>.<minor>`, which stamps that one line and records the result
as the reviewed content. Then re-read the ruling state before retrying a
promotion: stamping changed the document's bytes, so a human ruling taken
over the pre-stamp version no longer covers it and must be re-recorded.

<!-- LIGATURE-AUTHORITY-END -->
