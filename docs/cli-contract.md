# Ligature CLI contract — grammar v1.0

Chainlink #55. This is the stable public surface `ligature` (the future
packaged binary, #57) and `python3 scripts/pipeline.py` (today's source
checkout entrypoint) both commit to. The real `status`/`check` bodies were
implemented by #56; `init`, `doctor`'s install diagnostics, and `migrate`
by #58; `version` and `doctor`'s binary attestation by #57. This
document specifies the grammar and the mapping every registered
`pipeline.py` subcommand (`scripts/pipeline.py:build_parser()`,
cross-checked by `tests/test_inventory_drift.py` and
`tests/test_cli_contract.py`) resolves to, so the remaining bodies are
implemented against a fixed target instead of inventing one mid-build.

Module and function names (`scripts/*.py`, `cmd_*`) are explicitly **not**
frozen by this contract — only the strings in the tables below are public
API. A future rename of `scripts/gate_g14.py` or `cmd_gate_g14` is an
internal refactor; a rename of `gate g14` is a breaking CLI change.

## 1. Grammar

Two levels: `ligature <top-level-verb> [<argument>] [flags...]`.

```
ligature init [--mode greenfield|port]      # #58
ligature doctor                              # #57/#58
ligature version [--verify]                  # #57
ligature status [--json]                     # #56 -- project-state query
ligature check [--json] [next]               # #56 -- consolidated gate run + next action
ligature write-set-check [--json] [--issue N]  # #77, #103, #114 -- read-only write-set conformance
ligature authorize-write --issue N --path P --op K --issuer <name>  # #114 -- record one issue-scoped capability for a sanctioned protected-root write
ligature scaffold-crate --issue N --crate <name>  # #115 -- deterministic Cargo.toml + canonical src skeleton for a missing declared crate of a port-mode project
ligature generate-work-package --plan <path> --issue N --toolchain <value> --target <triple> [--feature <name> ...]  # #122 -- derive and authorize ci/manifest/WP-*.json from validated workspace authority
ligature accept-policy --reviewer <name> [--version <name>@<major>.<minor>]  # #78, #113 -- record a reviewed governance change
ligature promote-evidence <target>           # #79 -- mechanically promote a staged evidence draft
ligature record-ruling --reviewer <name> --verdict ratified|rejected --artifact <path>  # #82 -- the human-ruling gate accept-promotion enforces
ligature record-assurance <work-package> --proof <obligation>=<path> [...]  # #87 -- assemble a work package's assurance report from verifier proof certificates
ligature validate <artifact-kind> <target>   # deterministic, per-artifact
ligature gate <gate-id> [args...]            # deterministic, cross-artifact
ligature draft <artifact-kind> <target>      # Stage 0/3, one-shot LLM
ligature approve <operation> <target...>     # human checkpoint
ligature migrate [--upgrade|--prune|--force <path>]  # #58 installed-file recovery
ligature migrate --assumptions [--apply --reviewer <name>]  # #59 phase-C reference migration
ligature report <report-id>                  # extension verb, see §3
```

`report` is one addition beyond the ten verbs #55's own issue text names.
It exists because four of today's real commands (`select-pilot-cluster`,
`measure-gold-set`, `generate-feature-ledger`, `generate-contact-sheet`)
are none of validate/gate/draft/approve/check: each produces a
**human-facing artifact or ranking that is not a pass/fail gate, not an
artifact validator, and never itself subject to promotion/review**.
Forcing them under `check` would misrepresent one-time planning output
(pilot-cluster ranking) and standing review surfaces (the contact sheet)
as part of `check`'s per-run gate loop; giving them no top-level verb at
all would violate #55's own instruction not to silently lose reporting
commands. `report` is the minimal, explicitly justified extension, and
every verb still has exactly one job.

The verbs added since are of one kind each, and are named here so the
grammar is not read as a frozen list: `promote-evidence` (#79) and
`record-assurance` (#87) each own one promotion or projection no existing
verb could perform; `record-ruling` (#82) and `accept-policy` (#78/#113)
each own one explicit human checkpoint on an artifact the pipeline cannot
promote without it; `write-set-check` (#77/#103) and `authorize-write`
(#114) are the write-set pair — one read-only conformance report, one
record of the capability that report consumes — and `authorize-write`
exists because #103's enforcement left a *sanctioned* protected-root write
with no route out (§8). `scaffold-crate` (#115) is the generator that
record then feeds: a port-mode pilot whose declared crate does not exist
has no manifest, so nothing downstream can run, and the only supported
bootstrap was a human hand-writing a protected-root file. `generate-work-package`
(#122) consumes the same bounded capability to create the canonical Stage 7
manifest from validated authority. None of them is a supervisory verb, and
none changed the two-level grammar.

`generate-work-package` (#122) is the Stage 7 producer for the canonical
`ci/manifest/WP-*.json` consumed by validation and gates. It reads a JSON or
YAML plan, validates descriptor-backed closure, promotion, boundary,
interaction and bridge inputs with their existing validators, and derives
the manifest using the field-to-source map in `work_package_manifest.py`.
`--issue` must match both the plan and an active grant; `--toolchain`,
`--target` and repeatable `--feature` provide configuration provenance, with
target/features checked against selected interaction scopes. The output path
is always `ci/manifest/<work_package>.json`; callers cannot supply a path or
manifest bytes. A new file is atomically written only after the derived data
passes `validate-work-package` and an exact-path `write` grant is proven.

Successful runs append a strict `work-package-generation` event to
`ci/results/protected-writes.jsonl`, recording issue, grant id, path,
before/after hashes and command result. The grant reader ignores this event
as authority. An identical rerun is recorded as `unchanged` without touching
the manifest. A grant already recorded as creating a manifest cannot create
it again if the file is later removed; a new write requires a new grant.
Conflicting bytes, missing/expired/mismatched grants, invalid inputs and
invalid audit history fail closed. `--json` prints either the audit record or
a refusal object with a stable error code, message and required inputs.

`scaffold-crate` is a top-level verb rather than a flag on `authorize-write`
because the two own different halves of one operation and must not be
fusable: `authorize-write` decides *whether* a protected write is authorized
and records that fact; `scaffold-crate` writes the bytes and verifies them
against the record. Folding the generator into the issuer would put the
content of a crate manifest on the same command as the human checkpoint that
authorizes writing it, which is the arrangement #114 was opened to avoid.
It is also the reason the verb carries no `--content` flag: the manifest is a
pure function of the descriptor, so there is nothing for a caller to supply
and nothing for a checkpoint to police (§8).

`report` is **not** uniformly read-only — see §6's own write column. This
was wrongly stated as a blanket "read-only" property in an earlier
revision of this contract; it is corrected here because #56/#57 need the
per-report-id truth, not a category label that doesn't hold for three of
the four report-ids.

`--workspace` and `--descriptor` remain global flags on every subcommand,
unchanged from today's `build_parser()`. A subcommand that accepts a
global flag must act on it: a flag accepted and then ignored is worse
than one rejected, because the operator reads the verdict as being about
the file they named (#109).

`record-assurance` (#87) is a top-level verb rather than a flag on
`gate g14` because the two are the two halves of one artifact with three
different jobs between them: `gate g14` *reads* the achieved side,
`record-assurance` *writes* it, and `check` must stay read-only (§7). It
is the producer `docs/assurance-report-schema.json` never had for its
obligation half (the bridge half already has
`gate_g9.bridge_records_for()`): it assembles a work package's report at
that manifest's own `report.emit` path from the verifier's own proof
certificates, so `gate g14` can check whether obligations are discharged
instead of reporting "recorded no achieved assurance" for a workspace
whose proofs are complete (the date-creusot pilot, chainlink #87). Every
field of the record is derived from the certificate and the manifest --
never asserted -- and the one thing the caller does declare, which
certificate is about which obligation (`--proof <obligation>=<path>`,
repeatable), is checked rather than trusted: the obligation must be one
the manifest provides, and the certificate must sit under a directory
named for that obligation's own concept. It writes nothing on a refusal
and exits 0 (recorded) / 1 (the evidence does not establish the
obligation) / 2 (invalid input), per docs/exit-code-contract.md.

`gate g14` re-opens what `record-assurance` wrote rather than taking it
on declaration (chainlink #92): every obligation record's
`evidence.scope.proof_targets` is resolved and re-read as the why3find
certificate it claims to be -- workspace containment, the obligation's
concept directory, a non-empty `proofs` section with no stuck subgoal
and at least one proved goal, nothing older than its Coma program -- and
every derived field (`evidence.kind`/`verifier`/`harness` from the
manifest's own `harness`, `config` from its `provenance`) is re-derived
from that manifest and must agree. A record that names no certificate,
whose certificate cannot be re-opened or no longer establishes the
obligation, or that its manifest contradicts blocks the cluster with a
finding naming the field, exit 1; no degradation record excuses these
(`failed_conditions` holds closure-condition keys only), and the check
is read-only -- `gate g14` opens certificates, it never writes them.

The CG3 verdict `gate g14` prints (`generic_callees_type_universal_or_
creusot_owned` is *VERIFIED from artifact data*) is computed from the
whole workspace's work-package artifacts, not only the closure profile's
list (chainlink #93): every obligation and bridge in the closure AND in
every work package it reaches through a declared `depends_on`
(transitively, with a target resolving to no manifest failing closed)
must have its own achieved record, and ownership must be creusot's
positively -- every record in every readable assurance ledger under the
workspace, and every declared guarantee harness in `ci/manifest`, is
required to be creusot. Anything else (a non-creusot record or harness
anywhere, an unrecorded obligation, an unreadable ledger, a dangling
`depends_on`) withholds the verdict and reports the condition as the
capability gap it is: exit 1 unless a degradation record names the
condition with `capability_gap: CG3` and a tracking issue, with which
the cluster is released as degraded (exit 0), never as closed. An
`info`-severity note tagged with a condition -- including the VERIFIED
note itself -- is a reported state, never a `limitations=` entry in
`ligature status`; only findings that still limit the cluster are.

A degradation record may release a cluster only once its OWN acceptance
is provenanced and ruled on (chainlink #97, shared with
`accept-promotion` via `scripts/degradation_review.py`): its `review`
block must match an entry the sanctioned `approve` path appended to this
workspace's `ci/results/review_log.jsonl`, and a `ratified` human
ruling must cover the record's exact current bytes in
`ci/results/human_rulings.jsonl` (record it with `ligature record-ruling
--reviewer <name> --verdict ratified --artifact <path>`; re-run it after
ANY edit, since the ruling is pinned to those bytes). A record that is
hand-written, unruled, rejected, or stale against its own ruling excuses
nothing at all -- the cluster stays BLOCKED with both reasons named, and
`released under an accepted degradation record` is never printed. The
G17 recomputation and the ceiling/`tracking_issue` rules are unchanged
for a record that does clear both checks.

A record may only be acted on at all if `validate-closure` accepts it:
`gate g14` reads records only through `validate_closure.
load_degradation_records()`, so the two commands share one notion of
which records count (chainlink #99/#100). A record `validate-closure`
refuses -- one naming a stale excuse its own profile contradicts (G17),
one that is schema-invalid or whose `failed_conditions` is not an array
of condition keys, one whose cluster disagrees with its filename, one
with no valid profile beside it, or one filed outside `specs/_closure/`
-- is named as an error and excuses nothing, so `gate g14` exits 1 on
every one of them and never prints a cluster as released under it. A
record held but unusable is never the same as a workspace declaring no
degradation at all, and the two commands' exit status agrees on all of
these. The single documented exception is chainlink #86's CG3 key:
`generic_callees_type_universal_or_creusot_owned` is the one condition
no profile/record comparison can settle, so a record naming it beside a
`true` declaration is the tracking record a capability gap requires and
stays valid; `gate g14` alone rejects that one as stale once the
workspace's own artifacts verify the condition, which is the only case
of `gate g14` refusing where `validate-closure` accepts.

`ligature status` and `ligature check` read degradation records through
that same loader (chainlink #101), so all three commands share one notion
of which records count. A cluster's `limitations=` and its
`degraded`/`unknown` state come only from a record the loader accepts; a
record it refuses contributes neither, and `status` then reports exactly
what it reports on a workspace that declares no degradation at all.
`limitations=` entries are always closure-condition keys: a record whose
`failed_conditions` is a string rather than an array is refused, never
iterated into one limitation per character. The reason a record was
refused is reported by the two commands that do the refusing --
`validate-closure` runs inside `check`, as does `gate g14` -- so it is
not restated as a limitation here. Closure PROFILES are unchanged:
`status` still derives `closure_kind` and the cluster list from the
profile files under `specs/_closure/`, which were never a record-side
concern.

There is no `ligature start` or other supervisory verb that runs the phase
loop autonomously. `approve`/`promote` are human-authority checkpoints
(`approve()` already requires an explicit `_REQUIRED` `validate_fn`); a
command that drove the loop itself would either stop at every such
checkpoint, adding nothing over an agent calling `check`/`next`, act, and
`check`/`next` again, or cross the human-approval boundary silently. The
binary stays a passive oracle -- `status` for state, `check`/`next` for the
single recommended next action -- and `.codex/skills/ligature/SKILL.md`
(#58) is what instructs the agent to drive that loop.

## 2. `validate <artifact-kind>`

All thirteen `G1a`/`G1b`(+) legacy `validate-*` commands become one verb,
`<artifact-kind>` selecting which. `target`/`manifest`/`receipt` positional
arguments and each command's own flags carry over unchanged.

| artifact-kind | legacy flat command | notes |
|---|---|---|
| `boundary` | `validate` | bare `validate` was ambiguous once other kinds existed; `boundary` names what it actually checks (G1a/G1b/G2+) |
| `interaction` | `validate-interaction` | includes computed eligibility, G2++, G15, R2 |
| `exemption` | `validate-exemption` | |
| `protocol-debt` | `validate-protocol-debt` | |
| `bridge` | `validate-bridge` | G1a/G1b/G2 |
| `evidence` | `validate-evidence` | workspace-level |
| `conflict-resolution` | `validate-conflict-resolution` | workspace-level, G11 |
| `work-package` | `validate-work-package` | takes `manifest` positional + `--specs-search-root` |
| `promotion` | `validate-promotion` | takes `receipt` positional |
| `callsites` | `validate-callsites` | workspace-level, over C_static reports |
| `witness` | `validate-witness` | G1a/G1b/G2 |
| `gold-set` | `validate-gold-set` | |
| `closure` | `validate-closure` | G1a/G1b/G17 |

For the two path-taking kinds, `work-package` and `promotion`, the
positional `manifest`/`receipt` path is checked before anything else the
command would load: a missing path or a directory is refused with one
line on stderr naming the path and the kind of file the command expected
(`error: manifest not found: <path>`, `error: receipt not found: <path>`,
`error: manifest path is a directory, expected a file: <path>`), exit 1,
never a Python traceback (chainlink #83). `validate-work-package` and
`validate-promotion` -- the §10 aliases for `validate work-package` /
`validate promotion` -- report exactly this line: an alias is routing,
never a second error policy (§9).

A staged `<target>.json.draft` (what `draft` writes, promoted by
`approve`) is **not** an artifact under review: for every kind `draft`
can stage, the `validate` that owns that kind skips `*.draft` in
discovery, so a staged draft is neither reported nor counted until it is
approved. `validate interaction` and `validate boundary` have done so
since chainlink #90, `validate evidence` since #79, and `validate
closure` since #98; chainlink #110 extended the same rule to the five
that were still collecting their own — `validate bridge`, `validate
witness`, `validate exemption`, `validate protocol-debt` and `validate
conflict-resolution` — and to the boundary naming/layout scan, which
reported a staged boundary draft as "not a .json file (suffix: '.draft')"
while `validate` ignored the same file. The rule is one definition,
`review_checkpoint.is_staged_draft()`, that each such discovery function
filters on, so the next kind added inherits it. The distinction is the
suffix, not the absence of `review`: an unreviewed artifact at its real
`<id>.json` path is still validated and reported -- `check` normalizes
that exact case to a pending human decision rather than a mechanized
failure (chainlink #90). Gold sets and C_static reports are outside it
only because no `draft` template produces one: they have no staging step
to be confused with.

Closure profiles and degradation records (`specs/_closure/<cluster>.json`
/ `<cluster>.degradation.json`) go through `draft`/`approve` like every
other normative artifact type (chainlink #98) -- workspace-level, not
crate-scoped, dispatched the same way conflict-resolution records
already are. Before this, hand-writing the target file directly was the
only way either could ever exist at all, which is exactly how #94's
pilot finding produced a degradation record with a fabricated `review`
block and no corresponding approval: there was no sanctioned path for
#96/#97's provenance checks to ever see satisfied. `draft` gives
immediate G1a/G1b feedback with `review` not yet required and rejects a
model- or human-supplied `review` block outright (the same checkpoint
discipline every other draft-capable artifact type carries); `approve`
re-validates with `review` required, writes it, and appends the
matching entry to `ci/results/review_log.jsonl` that `review_checkpoint.
review_provenance_gaps()` -- and so `gate g14`'s own provenance check,
chainlink #97 -- requires before a degradation record may release
anything. Approve-time validation is deliberately G1a/G1b only, never
G17's cross-artifact profile/record consistency: G17 is inherently a
two-artifact question, and `validate-closure` / `gate g14` already run
it over the whole cluster and fail closed in both directions, so a
transient inconsistency between two independently-approved artifacts
(most visibly, a brand-new cluster's first profile declaring a failing
condition before its degradation record is drafted and approved) is
caught there rather than deadlocked here.

## 3. `gate <gate-id>`

The six standalone cross-artifact gates become one verb. Gates that are
checked *inline* inside a `validate <kind>` run (G1a, G1b, G2/G2+/G2++,
G11, G15, R2 — see `docs/implementation-inventory.json`'s `gates` array)
are **not** independently invocable subcommands today and this contract
does not add them as one; they remain reachable only via the `validate`
call that exercises them.

| gate-id | legacy flat command |
|---|---|
| `r1-g16` | `gate-r1-g16` |
| `g9` | `gate-g9` |
| `g14` | `gate-g14` |
| `g18` | `gate-g18` |
| `g19` | `gate-g19` |
| `g20` | `gate-g20` |

`gate r1-g16` is three-valued (0 / 1 / 3) and since chainlink #107 also
prints one severity that is reported **without** blocking: an undeclared
cross-concept `definite-direct-call` whose callee is a computed
`value-domain-inquiry` (§9.1's second table row, and the `WARN:` section of
its output) exits 0 while being carried by `check --json` as a non-blocking
`medium` finding. It is not counted as `checked`, and it is not a claim that
the workspace has nothing to look at — see `docs/exit-code-contract.md`'s
code 0.

## 4. `draft <artifact-kind>`

Already effectively nested: `draft <stage> <template_name> <target>` keeps
its shape unchanged, `template_name` filling the `<artifact-kind>` slot.
No legacy flat commands to reconcile — `draft` was never flat.

`template_name` is resolved through the one registry in
`scripts/draft_templates.py` (chainlink #105), which is also what
`draft --help` prints and what `init` copies into `.ligature/prompts/`;
`ligature draft --help` lists **every** shipped template, what it drafts,
where the artifact belongs, and which command promotes it:

| stage | template | drafts | target | promoted by |
|---|---|---|---|---|
| 0 | `evidence-intake` | evidence record | `evidence/<id>.json` | `promote-evidence` (mechanical) |
| 3 | `concept-to-code` | concept spec | `<crate_dir>/specs/<snake_case(concept)>.json` | `approve` |
| 3 | `boundary-drafting` | boundary contract | `<crate_dir>/specs/_boundaries/<boundary_id>.json` | `approve` |
| 3 | `interaction-drafting` | interaction spec | `<crate_dir>/specs/_interactions/<interaction_id>.json` | `approve <interaction> <protocol-debt>` (pair) |
| 3 | `bridge-drafting` | bridge specification | `<crate_dir>/specs/_bridges/<bridge_id>.json` | `approve` |
| 3 | `witness-drafting` | witness specification | `<crate_dir>/specs/_witnesses/<snake_case(concept)>.<query>.json` | `approve` |
| 3 | `exemption-drafting` | exemption record | `<crate_dir>/specs/_exemptions/<interaction_id>.json` | `approve-exemption-pair` |
| 3 | `protocol-debt-drafting` | protocol-debt record | `<crate_dir>/specs/_protocol_debt/<interaction_id>.json` | `approve` (pair) |
| 3 | `conflict-resolution-drafting` | conflict-resolution record | `specs/_conflicts/<id>.json` (workspace-level) | `approve` |
| 3 | `closure-profile-drafting` | closure profile | `specs/_closure/<cluster>.json` (workspace-level) | `approve` |
| 3 | `degradation-drafting` | degradation record | `specs/_closure/<cluster>.degradation.json` (workspace-level) | `approve` |

A name not in the registry is refused with the complete list for the stage
asked for (and, when the name belongs to the other stage, which stage it
belongs to) — one line at exit 1. It used to be
`error: no template at /tmp/ligature-data-XXXX/prompts/stage-3-<name>.md`:
a path the caller never typed, in a directory that did not exist when they
ran the command, naming no alternative, after `--help` had listed two of
the eleven. Every `prompts/*.md` is in the registry, enforced by a test, so
a template cannot ship undiscoverable and cannot be discoverable unshipped.

## 5. `approve <operation>`

| operation | legacy flat command | authority |
|---|---|---|
| `draft` | `approve` | `--reviewer` (required), single target |
| `pair` | `approve-pair` | `--reviewer`, interaction + protocol-debt, atomic |
| `exemption-pair` | `approve-exemption-pair` | `--reviewer`, interaction + exemption, atomic (R2 bootstrap) |
| `promotion` | `accept-promotion` | `--reviewer` + `--policy-path` + repeatable `--artifact`; Stage 4.5's own deterministic, no-LLM generator |

`draft` promotes every template in §4's table except evidence, whose
promotion path is `promote-evidence` (§10). Concept specs joined that set
in #105: until then `draft 3 concept-to-code` staged a file nothing could
promote, and `approve` refused a concept spec verbatim with
`no validator recognizes target ...`. A concept spec is normative — plan.md
§7.2's own human-sign-off list names "new concepts" first, its
`constraints[].id` is what a boundary contract's `callee_guarantees`
resolves against (G2+), its `queries[]` what a witness spec's G2
cross-reference resolves against, and its `queries[].witness_required` the
declared feature set G18 measures coverage over — so it is promoted by
`approve` with a human `--reviewer`, like every other normative artifact,
and its schema carries this pipeline's own `review` block
(`docs/concept-to-code-modifications.md` gap #7) rather than getting a
mechanical promotion path like evidence. The consequence is the point of
the fix: with a promoted spec present, `approve` on a boundary contract now
resolves `applies_to` and refuses one whose guarantee does not cover the
callee method. Without one it approves at exit 0 and reports `applies_to`
merely "unverifiable" — a missing gate that is silent, and silent exactly
where a human is signing off.

**Implemented by #102.** Every operation promotes an *already-staged*
draft: `approve <operation>` reads `<target>.draft` and never writes
content itself. The pilot (date-creusot `date-ligature-101`, mirrored from
full-ligature-port chainlink #106) met the resulting gap as a ~15-frame
`FileNotFoundError` traceback at exit 1 — nothing on stdout, and a message
naming `<target>.json.draft`, a file the caller was never told about, so a
typo'd target and a re-run of an approval read as a corrupt installation.
Three states reach it and all three now read as one line:

```
error: nothing was promoted: no staged draft at <target>.draft -- this command promotes an already-staged draft and never writes content itself: stage one with `ligature draft <stage> <template> <target>` (`ligature draft --help` lists every template), or copy the target itself to <target>.draft
```

— a target nobody drafted; a target whose draft the promotion that created
it already consumed (the ordinary idempotency question, since every
pipeline step that is safe to retry asks it); and a record hand-written
straight to its target path, which is the state the `gate-g14` remediation
sends an operator to fix. The paired verbs promote all-or-none, so they say
so — `promotes all 2 drafts as one transaction` — and name every draft that
is *absent* in the same line, leaving a staged sibling exactly where it was
for the retry. The preflight lives in
`review_checkpoint.require_staged_drafts()`, the one function all three
promotion paths open with, so the three verbs cannot word the same refusal
differently; the `no staged draft at` opening is deliberately the one
`promote-evidence` already answered this identical condition with (§10). No
refusal writes anything, and none appends to
`ci/results/review_log.jsonl`: that trail records review events, and no
review happened. Exit is **1** through the same `PipelineError` fold (#83).

`promotion` is the one operation whose inputs must already carry both
requirements of #82 before anything is written:

- **provenance** — `accept-promotion` reads every accepted artifact's own
  `review` block back against the approval audit log
  (`ci/results/review_log.jsonl`, the append-only trail `approve` writes)
  and refuses when no matching approval entry exists — so `--reviewer`
  names *who is accepting the promotion*, never who approved the
  artifacts being promoted, and a `review` block that was merely typed
  into a file cannot pass. Artifacts with no `review` block (the reliance
  policy document, evidence records) have nothing to prove *here*, which
  is exactly the hole the second requirement fills;
- **a human ruling** — every artifact in the manifest must also carry a
  `ratified` verdict over its current content in
  `ci/results/human_rulings.jsonl`, recorded by `record-ruling --reviewer
  <name> --verdict ratified|rejected --artifact <path> [--artifact
  <path> ...]` (§1, §10). Provenance cannot ask whether a human has ruled
  on the accepted *set*: an unattended agent can run `approve` itself (its
  audit entries are indistinguishable), and records promoted outside any
  tool path have no review block to check. A `rejected` ruling, a missing
  ruling, or one recorded over an older version of the file all refuse.

A refusal exits 1 per the exit-code contract, names the offending
artifacts, and writes no receipt and no audit entry.

## 6. `report <report-id>`

| report-id | legacy flat command | writes? |
|---|---|---|
| `pilot-cluster` | `select-pilot-cluster` | no — prints a ranking/report to stdout only |
| `gold-set-measurement` | `measure-gold-set` | **yes**, by default (`ci/results/gold_set/`); today's `--no-write` flag carries over and suppresses it |
| `feature-ledger` | `generate-feature-ledger` | **yes** — `ci/results/feature_ledger.json`, a generated projection |
| `contact-sheet` | `generate-contact-sheet` | **yes** — `docs/witnesses/_contact_sheet.svg`, a generated projection |

Three of the four report-ids write a **generated projection**: a
derived, regenerate-from-source file that is never hand-edited, never
itself promoted, and never a gate input on its own (plan.md §16.5's own
"review projections, never promotion inputs" boundary — see
`docs/trust-and-compatibility-boundaries.md` §5 for the full write-vs-
authority table). `report` is therefore explicitly **not** a read-only
verb as a category; §4 of the trust-boundaries document lists only the
genuinely read-only top-level verbs, and `report` is not among them.

## 7. Internal operations `check` may recommend but never runs

Three commands are data-refresh/dispatch steps that a `gate` depends on:
`extract-c-static` (feeds `gate r1-g16`), `check-bridges` (feeds
`gate g9`), `render-witness` (feeds `gate g18`/`g19` and the `report`
generators). All three write to the workspace (`ci/results/c_static/`,
`ci/results/bridge_checks/`, a witness's declared `output.path`
respectively).

**`check` is strictly read-only (see `docs/trust-and-compatibility-
boundaries.md` §4, `schemas/consolidated-check.schema.json`'s
`mutated_workspace: const false`) and never invokes any of these three
automatically.** An earlier revision of this contract claimed `check`
performed this orchestration on its own as a normal part of running —
that was wrong, and contradicted both the trust-boundaries document and the schema's own
`mutated_workspace` guarantee at the same time; #56 cannot implement all
three promises simultaneously, so this contract now commits to exactly
one: `check` reads, it never writes. When a gate's prerequisite data is
missing or stale, `check` reports that gate's `outcome` as `blocked` with
a `reason` naming the specific missing prerequisite, and — when nothing
more severe is already blocking — may *recommend* running the refresh via
`next_action` (`kind: automated-command`). Recommending a command is not
running it; the operator (human or an outer orchestration layer that is
explicitly not `check` itself) decides whether to run it.

**The stable part of that recommendation is `action_id`, not `command`.**
A round-3 external review found the first version of this fix still
pointed the ONLY machine-consumable field (`command`) at these same three
unversioned flat names — a consumer trying to key off next_action had
nothing stable to key off after all. Resolved by splitting the two
concerns `schemas/consolidated-check.schema.json`'s `next_action` now
exposes separately:

| stable `action_id` (v1.0, closed `enum`) | what it recommends | today's `command` value (advisory only, not covered by any stability promise) |
|---|---|---|
| `refresh-c-static` | run `extract-c-static` | `ligature extract-c-static [...args]` |
| `refresh-bridge-checks` | run `check-bridges` | `ligature check-bridges` |
| `refresh-witness` | run `render-witness` | `ligature render-witness <witness_id> --renderer <...>` |
| `author-interaction` | draft an interaction spec (see `schemas/examples/consolidated-check.blocked.example.json`) | `ligature draft interaction <target>` |
| `promote-evidence` | promote a staged evidence draft (chainlink #79) | `ligature promote-evidence <target>` |

`action_id` is required whenever `next_action.kind` is `automated-command`
and forbidden otherwise, and is schema-closed to exactly the five values
above as of v1.0 (round-4 external review: an earlier revision left this
field an open pattern, which let any well-formed but undefined string —
`invented-unstable-action` was the confirmed repro — validate; that is
not actually a versioned vocabulary, so it's closed with a JSON Schema
`enum` instead). `command` is always present and non-null whenever `kind`
is `automated-command` — its VALUE carries no stability guarantee and may
point at a legacy flat name that changes shape, but the field itself is
never omitted or null for this kind (a still-earlier revision of this
paragraph claimed `command` "may be null" here too, which directly
contradicted the schema's own conditional; that claim is removed, not the
requirement). Automated tooling consuming `check --json` output MUST
branch on `action_id`, never parse or pattern-match `command`. Extending
this enum with a new value is an additive, minor schema-version change,
same discipline as any other enumeration in this contract; #56 is
expected to add more as `check`'s own recommendation logic grows (e.g.
for further `validate`/`gate`/`approve` follow-ups), each one landing
here and in the schema together, never one without the other.

The three flat commands themselves are **not** promoted to stable public
API by this contract — no removal version is promised because they are
not being deprecated, they are being kept as the only way to actually
perform the refresh `check` can merely name. Their existing flat names
keep working (CI matrices that parallelize C_static extraction by target
triple, for instance, still need direct invocation) but that surface is
explicitly unversioned: it may change shape without notice, and is not
covered by §9's alias-removal policy — this is exactly why `action_id`,
not `command`, is the contract surface.

## 8. The `status` stub → `doctor`

Today's `status` (`cmd_status`, pipeline.py:1909) prints a **capability
manifest** — which flat commands exist and which stages remain
unimplemented. That is categorically different from #56's real
`ligature status`, which will report **project state** (per-artifact
lifecycle, per-obligation assurance, per-cluster closure — see
`schemas/project-state.schema.json`). Reusing the name for both would let
a capability list stand in for a project-state report, exactly the
conflation #55 was opened to prevent.

Resolution: the existing capability summary moves to `doctor`'s
capability-reporting half (`doctor` also owns install/version diagnostics
under #57/#58; a capability listing is one more thing a doctor reports).
**`status` is reserved exclusively for project state, unconditionally,
from the product's first packaged release** — there is no version window
in which the bare name means anything else. No packaged `ligature`
binary has ever shipped (#57 is what would ship one), so there is no
real compatibility obligation to honor a "`status` = capability list"
meaning under any version umbrella; inventing a 1.x transition window for
a meaning that has never been publicly released was itself the defect an
earlier revision of this contract had, alongside contradicting §1's own
grammar table, which already lists `ligature status` as the #56
project-state query.

Today's literal `python3 scripts/pipeline.py status` invocation (source
checkout only, pre-packaging) is not covered by this contract's
compatibility-alias policy (§9) at all — it is this repository's own
development-time behavior, not a released product's public API.

**Implemented by #56.** `status` now prints real project state
(`cli-contract §1`'s grammar, `schemas/project-state.schema.json`) and
`check` prints the read-only consolidated check
(`schemas/consolidated-check.schema.json`); this is the change that
rewired `cmd_status`. The old capability text did not disappear — it moved
to `doctor`, which now owns it (alongside the install/version diagnostics
#57/#58 will add), so there is no version window and no deprecation notice
owed: `status` means project state unconditionally, and `doctor` means the
capability manifest unconditionally.

**Implemented by #58.** `doctor` also owns the install/version
diagnostics the resolution above anticipated. It reads the ownership
manifest at `ci/manifest/installation.json`, classifies every installed
file (`unchanged`/`upgrade`/`conflict`/`missing`/`obsolete`/`user-owned`),
and — because the `ligature` skill's authority section is canonical,
hash-pinned product content — recomputes that authority region's hash and
verifies it. A modified managed file (including an edited authority
region) makes `doctor` exit non-zero; it never auto-repairs. `init`
installs, `migrate` is the explicit, human-invoked recovery path.

**Implemented by #64.** `doctor` and `version --verify` also report the
running build's source provenance from its embedded attestation: the
`source_commit`, and for a build made from a dirty working tree a `source:`
line naming it `DIRTY working tree` with a `working_tree_diff_hash` over the
uncommitted state. A dirty build is still a self-consistent, correctly
attested artifact, so this is reported, not treated as an integrity
failure; `PROVENANCE.json` carries the same three fields.

**Implemented by #74.** `doctor` also validates the user-owned
`project-descriptor.json` against the product's own
`schemas/project-descriptor.schema.json` -- the same schema and validator
`check` fails closed on -- and annotates the descriptor's `user-owned`
inventory line with its schema state (`schema: valid`, `schema: invalid
(see check for detail)`, `schema: absent`, or `schema: unreadable`). A
descriptor state `check` reports `invalid_input` for (invalid, absent, or
unreadable) makes `doctor` exit **2** -- the exit-code contract's
invalid-input code, checked before every other condition per its
precedence -- so a workspace the pipeline cannot check is never reported
as a healthy, current installation. `check` remains the detail surface
for the offending property; `doctor` never repairs the descriptor (it is
user-owned, and `migrate --force` refuses it).

**Implemented by #109.** That descriptor gate follows the global
`--descriptor` flag, which `doctor` had been accepting and ignoring: it
resolved the descriptor from `ci/manifest/installation.json`'s own
`descriptor_path`, so `--descriptor <invalid>.json doctor` printed
`schema: valid` and exited **0** -- a clean verdict about a file it never
opened -- while `status`, `check` and `write-set-check` all failed closed
on that same file. Four commands could not be made to agree about which
descriptor was in force, and the disagreement was a false pass on the
command an operator is most likely to reach for as a pre-flight gate.

`doctor` now validates the descriptor `--descriptor` names, resolved
exactly as the other commands resolve it (as given: absolute, or relative
to the cwd, and not required to be inside the workspace), and reports it
as its own `descriptor in force: <path>  schema: <state>` line above the
inventory. The inventory is unchanged: `--descriptor` selects the
descriptor doctor *validates*, never the one `init` installed, so a
`user-owned` line for the installed descriptor and a gate line for a
candidate are two statements about two files rather than one silently
standing in for the other. Naming the installed descriptor is not an
override -- the two paths are compared after resolution, so
`--descriptor <workspace>/project-descriptor.json doctor` is byte-identical
to plain `doctor`. A flag that was never supplied is not an override
either: `--descriptor`'s filled-in default is the workspace default, and
treating it as an operator's choice would make a never-initialized
workspace fail closed on a descriptor file it never had.
`migrate` deliberately keeps recovering the descriptor the manifest
recorded -- it recovers what `init` installed, not what a caller is
trialling -- so it is never handed the flag.

**Implemented by #108.** Every command that loads the project descriptor
loads it through one shared guard, so a workspace with no
`project-descriptor.json` is reported the same way everywhere instead of
only where some caller happened to pre-check the file.
`extract-c-static` and `validate` used to reach the user as a raw
`FileNotFoundError` traceback at exit 1 -- so did `check-bridges`,
`gate-g9`, `gate-g14`/`g18`/`g19`/`g20`, `generate-feature-ledger`,
`generate-contact-sheet`, `measure-gold-set`, `select-pilot-cluster`,
`draft`, `approve` and `promote-evidence`, which left a worker with a
traceback as the only signal that a pilot root was not initialized yet
(the swisstable-verus pilot's `swisstable-verus-mcp-007`). The condition
is now one line on stderr and nothing on stdout --

```
error: cannot read project descriptor <path>: no such file -- run `ligature init --mode <mode>` to create one, or pass --descriptor <path> to name a descriptor that exists
```

-- naming both the file the command wanted and the command that creates
it, which is the message `migrate --upgrade` already gave for the same
root ("workspace is not initialized; run `ligature init --mode <mode>`
first"). Exit is **1** through the pipeline's documented `PipelineError`
fold (#83) and **2** from the standalone CLIs, which already did
(`measure-gold-set`, `select-pilot-cluster`, `validate-work-package`,
`validate-promotion-receipt`); the exit-code contract's invalid-input code
2 is the target for every surface, and `pipeline.py`'s dispatch does not
reach it yet for the reason `docs/exit-code-contract.md` records as known
drift (#56/#57/#58). Because the guard's message names the file itself,
the two standalone call sites that used to wrap it in their own
`cannot read project descriptor` prefix -- which printed the path and the
phrase twice for one missing file -- now print the guard's line
unchanged. A missing file, a directory and undecodable bytes are three
distinct conditions with three distinct lines, so a `--descriptor` naming
a directory is never answered with advice to run `init`. The commands that
report an absent descriptor as data -- `status` (`descriptor: absent`,
exit 0), `check` (`invalid_input`, exit 5) and `write-set-check`
(`descriptor: absent`, exit 2) -- are unchanged: they never needed the
file, so they never had the crash. Related but fixed elsewhere: the same
crash *shape* on a different input -- a missing `<target>.draft` sibling
for `approve` / `approve-pair` / `approve-exemption-pair` -- is
chainlink #102's, and that pre-flight belongs to those commands rather
than to the descriptor loader (§5).

**Implemented by #75.** `check` also fails closed on installation
integrity: when the descriptor's `gate_integrity` pins are drifted,
missing, or unverifiable against the ownership manifest's recorded hashes
(`status --json`'s `gate_integrity.state` is anything but `pinned`),
`check` reports the `gate_integrity_failed` condition and exits **5** --
the exit-code contract's new top-precedence code, so a compromised gate
definition voids every other signal the run could produce -- with one
high-severity finding per drifted or missing path naming it (or a single
workspace-level finding when no individual path can be named). The same
run drops `status --json`'s `installation_manifest.state` from `current`
to `drifted`/`unknown`, so the machine-readable status no longer reports a
clean installation whose gate definitions have been altered. `doctor`'s
`skill authority hash` line is derived from the manifest's recorded
`authority_hash` (plus the file's conflict state), so a tampered
skill-authority file is never attested as `verified` on the line directly
above that file's `CONFLICT` line, and `init` installs
`schemas/project-descriptor.schema.json` into `.ligature/schemas/`
alongside the other two bundled schemas.

**Implemented by #77.** `ligature write-set-check` makes the descriptor's
`write_set` (`allowed_roots` / `protected_roots`) machine-checked rather
than merely declared: it reports the files in the workspace that are
outside every `allowed_roots` pattern and not otherwise accounted for by
the ownership manifest, a declared user-owned document, a pipeline
artifact, a declared crate's `specs/` tree, or a canonical pipeline
location (`ci/`, `evidence/`, workspace-level `specs/_<kind>/`,
`docs/witnesses/`) -- the date-creusot pilot's `rust/rogue/evil.rs`
repro, which every v1.0 command reported nothing for. It also reports the
files inside `protected_roots` that nothing vouches for (the
protected-surface audit, non-blocking), and flags the vacuous
declaration shapes the schema accepts -- an `allowed_roots` pattern
matching every path (`"**"`) and an empty `protected_roots` -- as
violations of the write set's own purpose. `status --json` carries the
verdict as `write_set.state` (`clean` / `violations` / `unknown`),
`check --json` carries one high-severity `write-set` finding per
violation (so `check` exits 1 on an out-of-set file, the same
blocking-findings code every other high-severity finding uses), and the
command itself exits 0/1/2 per the exit-code contract. The same change
closes the descriptor-level `gate_integrity` path-escape gap: an entry
resolving outside the workspace (absolute, or a `../` traversal) is
refused rather than hashed against someone else's file -- the discipline
`validate_work_package.check_gate_integrity` already applies to
work-package manifests.

**`protected_roots` is enforced (#103).** #77 shipped the protected half
as an *accounted-for* category, and two location carve-outs made it
invisible rather than merely soft: any file under `ci/` (a canonical
pipeline location) and any file under a declared crate's `specs/` tree
passed for free. The date-creusot pilot planted three files inside its own
`protected_roots` -- `ci/manifest/PROBE.json` and two spec-tree probes --
and `write-set-check` reported `write set: clean`, `violations: []`, exit
0, output byte-identical to the no-probe baseline, in three consecutive
releases (1.1.0 through 1.2.1). A file inside a `protected_roots` pattern
is now a **violation** unless a declaration vouches for that specific
file: product-managed in the ownership manifest, a declared user-owned
document, the ownership manifest itself (which records the managed set and
so cannot vouch for its own path out of it), or a **pipeline artifact** --
a file the pipeline's own artifact system recognizes, by the same
discriminators `status` uses (the kind's identity field; the
`work_package` / `definition_of_done` keys for a work-package manifest).
Both carve-outs are gone. The two violation classes are reported
separately, each labelled, because only the second is a breach of the
installed skill's authority boundary 5:

as an *accounted-for* category, and two location carve-outs made it
invisible rather than merely soft: any file under `ci/` (a canonical
pipeline location) and any file under a declared crate's `specs/` tree
passed for free. The date-creusot pilot planted three files inside its own
`protected_roots` -- `ci/manifest/PROBE.json` and two spec-tree probes --
and `write-set-check` reported `write set: clean`, `violations: []`, exit
0, output byte-identical to the no-probe baseline, in three consecutive
releases (1.1.0 through 1.2.1). A file inside a `protected_roots` pattern
is now a **violation** unless a declaration vouches for that specific
file: product-managed in the ownership manifest, a declared user-owned
document, the ownership manifest itself (which records the managed set and
so cannot vouch for its own path out of it), or a **pipeline artifact** --
a file the pipeline's own artifact system recognizes, by the same
discriminators `status` uses (the kind's identity field; the
`work_package` / `definition_of_done` keys for a work-package manifest).
Both carve-outs are gone. The two violation classes are reported
separately, each labelled, because only the second is a breach of the
installed skill's authority boundary 5:

| class | meaning | blocking |
|---|---|---|
| `out-of-set` | written where no root permits at all | yes (exit 1) |
| `protected-write` | written inside a protected root the pipeline owns, in a file the artifact system does not recognize | yes (exit 1) |
| `declaration` | a write-set shape that enforces nothing (`allowed_roots: ["**"]`, `protected_roots: []`) | yes (exit 1) |
| *(audit)* | a protected file in a location the pipeline writes nothing into (a project's own `Cargo.toml`, `.github/`, `scripts/`) | **no** -- see below |

**What stays non-blocking, and why.** A protected file in a location the
pipeline writes nothing into is reported in `protected_unvouched` and in
a per-pattern `protected_surface`, but does not fail the check: nothing in
a workspace distinguishes the project's own `Cargo.toml` from one an agent
wrote, so failing closed on it would leave every mature workspace
permanently red and teach the operator to ignore the command. The audit is
now *complete* (no carve-out hides any of it) and `details` states in
words that the verdict does not cover those files, so a `clean` verdict can
no longer be read as "boundary 5 holds everywhere". The report also asks
that `clean` be distinguishable from "found nothing to check", which
`protected_surface` answers per pattern: every declared `protected_roots`
pattern is reported with how many files it covered, how many are
violations, and how many are unattributed; and a pattern matching no file
at all is named as such (`protected_root (matched no file): ...`), because a
protected root nothing matches protects nothing on disk and is not a
surface that was checked. Recognition of a pipeline artifact is not a
second hand-kept list of "which paths the pipeline writes": it comes from
`project_state._discover_artifact_files`, its kind -> identity-field table,
and a new `project_state.artifact_dirs()`, so the protected surface is
judged by the artifact system the pipeline actually runs and cannot drift
from it (asserted in both directions by
`ArtifactDirectoryConsistencyTest`). `<target>.json.draft` inside an
artifact directory is accounted for too -- `review_checkpoint.stage_draft`,
reached through `ligature draft`, is the pipeline's own command writing
inside a protected tree, and a staged draft is inert until `approve`
promotes it. A file *nested below* an artifact directory is audited rather
than called a violation, because the artifact discriminators disagree
about depth (`_discover_artifact_files` reads one level deep;
`validate_interaction.find_interaction_files` walks any depth deliberately,
so a nested placement surfaces as its own G1b violation) and its
provenance is not decidable in one place. The unclosed half of the
boundary is labelled rather than silent; what closes it is the same
discipline #105 and #113 applied to their own dead ends (an explicit
human-checkpointed command that writes the protected artifact, never an
agent's hand edit).

**Implemented by #114.** That last sentence named the shape of the remedy
without saying what to do about a protected write that is *sanctioned* --
which is not the same problem as a protected write that should not happen
at all. #113 hit it exactly (a workspace whose normative policy document is
a `protected_root` and whose only supported fix was a hand-edit no agent
could make), and #107 hit it from the other end (an interaction spec whose
only home is a `protected_root` an agent is forbidden to write). Enforcement
with no exit is enforcement that pushes the work out of the tool, and both
of the routes left were worse than the defect: widen `allowed_roots`, which
deletes the boundary the write is supposed to live inside, or hand-edit a
file, which is precisely what the conformance check cannot tell apart from
an intrusion.

`ligature authorize-write` is the third route: a **tool-mediated,
issue-scoped capability record** authorizing one bounded protected-root
write, appended to `ci/results/protected-writes.jsonl`, which
`write-set-check` consumes grants for conformance reporting, and
`scaffold-crate`/`generate-work-package` consume grants for their own bounded
generator writes; no command can use a grant outside its issue, path and op.

| flag | what it binds |
|---|---|
| `--issue N` | the issue this grant is scoped to (positive integer, required) |
| `--path P` | the exact workspace-relative path, or an explicitly bounded pattern |
| `--op K` | `write` (create or replace the content -- the only operation the check consumes) or `delete` (recorded for the audit trail; a file's absence is not observable here) |
| `--ttl S` / `--one-shot` | `expires_at = issued_at + S` (default 900s, ceiling 86400s); `--one-shot` binds one exact file instead of a standing grant, and is reported `spent` — an attribution, not a consumed count (see below) |
| `--issuer NAME` / `--issuer-kind human\|supervisor` | the named issuer, and which lane it issued in |
| `--note` / `--issued-at` | audit metadata only, and the timestamp the expiry is computed from (a timestamp in the future is refused: a grant cannot be issued for a time that has not happened) |

What a grant cannot do, each of them recomputed on read rather than trusted
from the record's own field -- the same discipline #112 applied to a
bridge's `callee_shape`:

* **not widen `allowed_roots`.** `authorize-write` refuses a path no
  declared `protected_roots` pattern covers *and* a path `allowed_roots`
  already permits, and the ledger is consulted on the protected branch only,
  so a grant can never excuse an out-of-set write even if a line were
  appended to the ledger by hand.
* **not authorize another operation.** `op` is a closed vocabulary and only
  the operation named is authorized; a `delete` grant cannot make an
  existing file clean.
* **not outlive its TTL.** The reader recomputes `expires_at` from
  `issued_at + ttl_seconds` *and* rechecks the TTL's own bounds
  (`1 <= ttl_seconds <= 86400`), so a hand-written line naming a year of
  authority with a self-consistent `expires_at` is rejected as well as a
  hand-edited one; an expired record authorizes nothing (it is reported as
  `expired`, not dropped). The other end of the window is closed too: a
  record whose `issued_at` is still ahead of the clock the checking run
  decides against is reported `not-yet-issued` and authorizes nothing — the
  only relation the reader recomputes (`now < expires_at`) would otherwise
  call a future-dated line unexpired — and `authorize-write` refuses to
  record one in the first place. Both boundaries are inclusive and both are
  decided by arithmetic against the run's own clock, so expiry and
  non-issue are deterministic: the same command over the same bytes reports
  the same verdict, with nothing to sleep for and no clock race.
* **not cover another issue or path.** A grant authorizes its own issue
  only, which is why `write-set-check`, `status` and `check` all take
  `--issue N` and all honor the same records; with no `--issue`, no grant is
  consulted at all and the verdict is exactly what it was before #114.
* **not be edited in place.** `grant_id` is a hash of the fields the
  grant's authority is of, so moving the authorization to another path,
  issue, operation, TTL or issuer under a preserved id is rejected. A
  repeated id in the ledger is a `duplicate`, and `authorize-write` refuses
  to record one at all, which makes a retry of the issuing command
  idempotent rather than a way to stack grants.

**The issuer is a named identity, never prose.** `--issuer` is required with
no default. `--issuer-kind human` (the default) is a human checkpoint on the
same terms as `approve`/`accept-policy`/`record-ruling` -- the installed
skill says an agent must never run it. `--issuer-kind supervisor` is the
automation lane, reachable only for an identity the descriptor explicitly
declares in `write_set.authorized_supervisors`, a machine-checkable
whitelist `write-set-check` re-verifies *every time it honors a record* --
so withdrawing the identity from the descriptor stops the grant on the next
run, and a hand-written record naming a supervisor nobody declared
authorizes nothing. `--note` is recorded and excluded from the grant id: no
prose field is consulted when deciding whether a grant authorizes a write,
and it cannot invalidate a real record either.

**Reported, not excused.** A protected write an active grant covered is
clean *and* leaves `authorized_writes[]` in `write-set-check --json` with its
grant id, issue, operation, issuer, expiry and status (`active`, or `spent`
for a `--one-shot` grant), counts under its pattern's
`protected_surface[].authorized`, and is named in `details`. The ledger
itself is carried in `grants[]` with every record's status
(`active`/`expired`/`not-yet-issued`/`other-issue`/`unverified-issuer`/
`duplicate`/`no-issue-named`/`ledger-protected`) and every line that claims
to be a grant and is not usable with the reason -- including when the ledger
cannot be read at all, which yields no grant rather than an unproven one,
and names the reason in `grants.error` rather than only the word
`unreadable`.

**What `spent` is, and what it is not.** A `--one-shot` grant binds one
exact file (never a pattern), and the write it authorizes is reported
`spent` rather than `active` in `authorized_writes[]`. The *grant* stays
`active` in `grants[].entries[]` until it expires, because until then it
still covers the one path it names -- `spent` is about the write, not about
the grant. That attribution is the whole of what a one-shot grant enforces.
A workspace snapshot observes which files exist, not how many times each was
written, so `write-set-check` cannot tell the write a one-shot grant
authorized from a later edit to the same file -- and it says so, in
`details`, on every surface that carries it, rather than letting the word
`spent` imply a counter it never kept. The bound that does apply is the
grant's own TTL: the attribution stands until the grant expires, which is
why a one-shot entry carries its `expires_at` like any other and why a file
needing further sanctioned writes needs a further grant (each its own
append-only record). This is the same kind of residual `protected_unvouched`
is reported under, and it is a deliberate limit rather than an oversight:
enforcing a one-shot *count* would mean writing the observation down, and
`check`'s machine-readable `mutated_workspace: const false` guarantee
(`schemas/consolidated-check.schema.json`) is not available to spend on it.

**A trail inside a protected root is not a capability record.** A declared
`protected_roots` pattern covering `ci/results/` is a workspace
`authorize-write` refuses to record a grant in, so a record read out of one
cannot have come from the command whose rules `write-set-check` enforces. The
consumer therefore holds the same half of that rule rather than trusting the
writer to be the one that wrote: every record in such a ledger is reported
`ledger-protected` and authorizes nothing, and the pattern is carried in
`grants.ledger_protected`. The case that makes it load-bearing is a grant
naming the ledger's *own* path — honored, it would move that file out of the
protected-surface audit and report a declared protected root over
`ci/results/` as one that was checked and found vouched, which is exactly the
"a canonical pipeline location excuses a protected root" mistake #103
closed.

**The trust model, stated.** The ledger is an audit trail, not a signature:
the same model `record-ruling`/`human_rulings.jsonl` and
`approve`/`review_log.jsonl` already use, with the same residual -- an entry
*appended* by someone with write access to `ci/results/` is
indistinguishable from one `authorize-write` wrote. What #114 adds over that
is the recomputation above (an existing entry cannot be edited into a
different authorization) and one refusal that removes the residual's sharpest
edge: `authorize-write` refuses to record anything when the ledger itself is
inside a declared protected root, so a workspace cannot reach a state where
every append to its own capability trail is a boundary breach.

| surface | with `--issue N` | without |
|---|---|---|
| `write-set-check` | an active grant for that issue authorizes its own path/op | the pre-#114 verdict; `details` says no grant was consulted |
| `status --json` / `check --json` | the same write-set state, through the same call | the same |
| `authorize-write` | records the grant, exit 0 | records nothing, exit 2, one line naming the refused condition |
| `scaffold-crate` | consumes the record covering this manifest and writes it | writes nothing, exit 2, one line naming the issuing command |
| `generate-work-package` | consumes an active exact-path `write` grant and records the write | writes no manifest |

`--issue` is the same flag with the same meaning on all five surfaces, so
none of them can report a different write-set verdict about the same
workspace and the same run; a `--issue` no grant can name (anything that is
not a positive integer, which `authorize-write` refuses) matches no record
and yields the same fail-closed verdict rather than a permissive one.

Stage 7 generation also consumes only an active exact-path `write` grant,
but it additionally records the successful write in this same ledger. The
grant remains the capability; the generation event is a use record that
prevents a deleted output from being recreated by replaying an old grant.
This use record does not make one-shot grants count writes for the general
filesystem walk, and `write-set-check` continues to report grants using its
existing status vocabulary.

**The generator that record feeds (chainlink #115).** All of the above makes
a sanctioned protected write *authorizable*; it says nothing about what the
bytes should be. A port-mode pilot whose declared crate does not exist yet has
no `Cargo.toml`, so `extract-c-static`, `validate` and `gate g14` all have
nothing to read, and until #115 the only supported bootstrap was a human
writing a `protected_roots` file by hand under `authorize-write` -- which
stalls the worker and yields a manifest nobody can reproduce.
`ligature scaffold-crate --issue N --crate <name>` is the missing half.

**It writes exactly two files, and they are not the same kind of write.** The
manifest (`<crate_dir>/Cargo.toml`) is a protected-root write and requires an
active issue-scoped `write` grant at exactly that path, read through
`write_authorization`'s own ledger reader and status rules -- the same ones
`write-set-check` consumes, so the verb cannot believe a grant is live when the
conformance check would report it `expired`, `not-yet-issued`, `other-issue`,
`duplicate`, `unverified-issuer` or `ledger-protected`. The source skeleton
(`<crate_dir>/src/lib.rs`) is inside `allowed_roots`, needs no capability record
at all, and is never overwritten once it exists. **That asymmetry is the
acceptance criterion, made literal:** a missing-crate pilot bootstraps without
anyone holding raw `Cargo.toml` write scope, because the one protected write it
does need is bounded to one file, one operation, one issue and one expiry --
and the `src/lib.rs` write needs none.

**Nothing is supplied.** There is no `--content`/`--manifest`/`--path` flag and
no way to name a target other than a declared crate: both files are pure
functions of the descriptor's declared `crate_dir` plus two module constants
(`version = 0.1.0`, `edition = 2021`), with no clock, no ambient file and no
neighbouring crate read into the decision. So a caller has nothing to inject, a
checkpoint has nothing extra to police, and the emitted bytes are checkable by
a consumer rather than asserted in prose. The verb writes no implementation
code and no normative policy or spec content, and refuses outright a target
that lands in a canonical pipeline artifact location, a declared crate's
`specs/` tree, or the pinned upstream checkout -- judged by `write_set`'s own
predicates, so the refusal cannot disagree with the conformance check about
which paths are spec artifacts.

**Idempotent, or it fails clearly.** A manifest already on disk holding exactly
the rendered bytes is reported `unchanged` and nothing is written; the second
run's `before_hash` equals the first's `after_hash`, which is what makes
determinism checkable. A manifest already on disk with *different* bytes is
refused, never overwritten -- a manifest is a build input somebody may depend
on. An existing `src/lib.rs` is reported `preserved` with its hashes and left
alone, because that content belongs to the implementing agent.

**Verified, not asserted.** Before reporting success the verb re-derives every
claim it makes and records each as a named check in its receipt
(`docs/scaffold-receipt-schema.json`): `descriptor` (via `doctor`'s own gate,
read against the file this run used, #109's discipline), `manifest` (re-parsed
as TOML and compared byte for byte with what the renderer claims to have
written), `source-skeleton`, `write-set` (`write_set.check_write_set`'s **own**
verdict, including whether it attributes this write to *this* grant id -- the
proof the capability record actually covered the write rather than merely
existing), and `doctor` (`ligature_install.inspect()`). Four are blocking;
`doctor` is recorded rather than enforced beyond its descriptor gate, because
this verb writes none of the files `migrate`/`accept-policy` own, and making a
pre-existing condition fail a bootstrap would hide which command fixes it. A
blocking check that fails exits 1 with the receipt still printed: at that point
the files are on disk, and the honest report is the one that says so.

| flag | what it binds |
|---|---|
| `--issue N` | the issue this scaffold is for (positive integer, required) -- the same issue the authorizing record was issued for |
| `--crate NAME` | the crate, as this workspace's descriptor declares it: the `crate_dir` or its final segment, never anything else |
| `--grant-id GW-ID` | optional, and a check rather than a filter: pins which record must authorize the manifest, and a mismatch is refused rather than silently substituted |
| `--json` | the versioned receipt: issue, grant consumed, both generated paths with before/after hashes, and every check's result |

### Stage 7 manifest generation (chainlink #122)

`ligature generate-work-package --plan <path> --issue N --toolchain <value>
--target <triple> [--feature <name> ...] [--grant-id GW-ID] [--json]` is the
Stage 7 writer. The plan is a JSON or YAML `WorkPackageRequest`; it selects
scope but cannot supply normative content. Descriptor-backed canonical
closure profiles, promotion receipts, boundaries, interactions and bridges
are loaded only after their existing validators accept them. The manifest
deriver's field/source table is the content policy; the command runs
`validate-work-package` over the result before attempting a write.

The only output path is `ci/manifest/<work_package>.json`, where the package
id comes from the plan. The plan's `issue` must equal `--issue`; the grant
must be active for that same issue, have `op: write`, and name that exact
canonical path as an exact grant. Pattern grants are not accepted. The target
is atomically created, never replaced: an identical file is a no-op, while
different existing bytes are left untouched. The grant id, issue, path,
before/after hashes and successful command result are appended as a strict
`work-package-generation` event in `ci/results/protected-writes.jsonl`.
`write-set-check` ignores that event as a capability. A previous successful
create prevents the same grant from recreating a deleted file; an identical
rerun remains a no-op and is itself auditable.

`--toolchain`, `--target` and repeatable `--feature` are provenance inputs;
the target and features must agree with the selected interactions'
`realization.config_scope`. The base commit is read from the workspace Git
HEAD. `--json` prints the audit event on success; a refusal returns exit 2
with `error.code`, `error.message` and `error.required_inputs`. The human
surface names the refused input and points to the required grant command.

**Implemented by #78.** A user-owned normative document (`docs/reliance-policy.md`)
could not be drift-checked, and the ownership manifest recorded
`base_hash`/`expected_hash` for it that were never compared -- the
standing governance document could be rewritten, including to contradict
its own fixed resolution table, while `doctor`, `check` and `status`
all reported healthy. The on-disk content of a normative user-owned
document is now compared against the manifest's reviewed `base_hash`:
a mismatch or a missing file makes `doctor` report `DRIFTED`/`MISSING`
and exit 1, makes `check` emit a high-severity `policy-drift` finding
and exit 1, and drops `status --json`'s `installation_manifest.state`
from `current` to `drifted` with the drift carried as an open finding.
The recorded base moves only through the new explicit accept path
`ligature accept-policy --reviewer <name>` (analogous to
`accept-promotion`): `init`/`migrate` never re-base a normative
user-owned file to whatever is on disk, and the document must carry
exactly one `Policy version: <name>@<major>.<minor>` marker line -- the
same convention `accept-promotion` reads -- so the recorded hash always
corresponds to a policy that can yield a policy_version. The
descriptor's `compatibility_policy.reliance_policy_path` pointer was
also read by nothing: it is now validated (`check` fails closed when it
names a nonexistent file or a path outside the project root) and
consumed as the default for both `accept-policy --policy-path` and
`accept-promotion --policy-path`, with an explicit path that disagrees
with the declaration refused. A legacy manifest that records no
`base_hash` adopts the on-disk content once, so a pre-#78 workspace
becomes drift-checkable rather than permanently unverifiable.

**Closed by #113.** The marker line that requirement rests on made
Stage 4.5 unreachable in every workspace. `init` installs
`docs/reliance-policy.md` as the template, placeholders and all, so the
`Policy version:` line it carries is the literal
`<policy-name>@<major>.<minor>` placeholder — which yields no
`policy_version`. Both accept commands refuse such a document:
`accept-promotion` with `must have exactly one ... marker line -- found
0`, before it writes anything, and `accept-policy` — the one command
whose job is to accept this document — because the marker it required was
the marker it should have established. Meanwhile the document is a
`protected_root` in the installed descriptor and the hash-pinned skill
forbids any agent from writing into a protected root, so the only
supported way out of the deadlock was a hand-edit no agent could make. The
pilot measured it (chainlink #114, swisstable-verus, ligature 1.2.1): a
completely correct Stage 4.5 sequence — 14 boundary contracts, 15
interactions, 6 bridges and 1 conflict resolution, all approved at exit 0
— died at `accept-promotion`.

Two halves, both in the command that already owns the document:

* **`accept-policy --version <name>@<major>.<minor>`** stamps the single
  `Policy version:` line and records the stamped document as the reviewed
  `base_hash`, in one atomic write. That line and nothing else in the
  document changes; the value is validated against the same pattern the
  marker line itself accepts, so this command cannot produce a document
  `accept-promotion` would later refuse. It is also how a real version is
  bumped on a substantive policy change. Like `approve`, `accept-promotion`
  and `record-ruling`, it is a human checkpoint — never run it on a human's
  behalf. Without `--version` nothing is written but the manifest, and a
  document with no well-formed marker is still refused. A document with no
  marker line at all, or with more than one, is refused rather than
  guessed at.
* **Early reporting.** An unstamped marker is reported where it can be
  acted on long before Stage 4.5: on the document's own line of the
  `init`/`doctor`/`migrate` inventory (with the command named), as a
  non-zero `doctor` exit, and as a high-severity blocking
  `policy-version-marker` finding in `check --json` / `status --json`.
  `init` itself still exits 0 and still reports `installation: current`:
  an unfilled template is not installation drift — nothing conflicted and
  nothing was modified — so the two installation verdicts keep the meaning
  they had, and the outstanding condition is the finding. A *missing*
  document remains `policy-drift`'s own signal, so one defect never
  produces two findings.

## 9. Compatibility alias policy

Every legacy flat command not listed in §7 (internal) becomes a
compatibility alias for its stable nested equivalent (§2 validate, §3
gate, §5 approve, §6 report). An alias:

- Accepts exactly the same arguments and flags as it does today.
- Produces byte-identical stdout/stderr and the same exit code as invoking
  the stable nested form directly (§ exit-code contract,
  `docs/exit-code-contract.md`) — a compatibility alias is a routing
  decision, never a second implementation that can drift from the first.
- Is removed no earlier than product version **2.0.0**. Aliases are not
  planned for removal before then; 2.0.0 is a floor, not a committed
  target date.

`status` is explicitly **not** governed by this section — see §8's own
resolution, which reserves the bare name for project state from the
first release rather than treating it as an alias with a removal floor.

## 10. Legacy command → disposition (complete, 47/47)

Every command `scripts/pipeline.py:build_parser()` registers today,
mapped to exactly one disposition. `tests/test_cli_contract.py` asserts
this table has exactly one row per registered command name and that the
set of names matches `pipeline.registered_commands()` exactly.

| legacy command | disposition |
|---|---|
| `write-set-check` | stable; read-only write-set conformance report -- files outside `allowed_roots` (`out-of-set`) and files inside `protected_roots` that no declaration vouches for (`protected-write`), relative to the project descriptor, with the verdict also carried as `status --json`'s `write_set.state` and as high-severity `write-set` findings in `check --json`. The protected surface is reported per declared pattern, including patterns matching no file (#77, #103, §8). `--issue N` scopes the write grants it consumes to one issue; with no `--issue`, no grant is consulted. `authorized_writes[]` reports what a grant covered (including a `--one-shot` grant's `spent` attribution) and `grants[]` carries the whole ledger with each record's status, so the audit is a machine-readable surface rather than a claim (#114, §8) |
| `authorize-write` | stable; records ONE issue-scoped capability authorizing a single sanctioned write into a declared `protected_roots` path, as an append-only entry in `ci/results/protected-writes.jsonl` -- the third route out of the dead end #103's enforcement left (widen `allowed_roots`, hand-edit outside every tool, or record a bound grant; §8, #114). The record binds the exact path or an explicitly bounded pattern, the operation (`write`|`delete`), the issue, `issued_at`/`expires_at`, the one-shot flag, the named issuer, the grant id (a hash of that binding, recomputed on read) and audit metadata. Refused, writing nothing, for a path no `protected_roots` pattern covers, one `allowed_roots` already permits, an absolute/escaping/vacuous/directory path, an undeclared `--issuer-kind supervisor`, a TTL outside `[1, 86400]`, an `--issued-at` in the future, `--one-shot` on a pattern, a replayed grant id, or a ledger that itself sits inside a protected root. A human checkpoint in the `human` lane; the `supervisor` lane needs the identity declared in `write_set.authorized_supervisors`. Exits 0/2 per docs/exit-code-contract.md |
| `scaffold-crate` | stable; writes a MISSING crate's `Cargo.toml` and canonical `src/lib.rs`, deterministically and from the project descriptor alone, in a Mode P (`port`) workspace -- the generator half of `authorize-write`'s capability record (§8, #115). Exactly two files: the manifest, which is a protected-root write and requires an active issue-scoped `write` grant at exactly that path (read through the same `write_authorization` reader `write-set-check` consumes), and the skeleton, which is inside `allowed_roots`, needs no capability record, and is never overwritten once it exists. There is no flag for manifest content and no flag naming a target: both files are pure functions of the descriptor's declared `crate_dir`, and the command accepts only a descriptor-declared crate name. Refused, writing nothing, for a crate no `crates[]` declares (or one two of them declare alike), an absolute/escaping/glob name, a non-`port` mode, a target inside a canonical pipeline artifact location, a declared crate's `specs/` tree or the pinned upstream checkout, a manifest no `protected_roots` pattern covers or `allowed_roots` already permits, no active grant (with the status that stopped each nearby record named), a `--grant-id` mismatch, or an existing manifest whose bytes differ. Idempotent by hash: identical bytes are `unchanged` and nothing is written. Emits a versioned receipt (`docs/scaffold-receipt-schema.json`) recording the issue, the grant consumed, both generated paths with before/after hashes, and the result of every named check -- including offline Cargo workspace membership and `write_set.check_write_set`'s OWN verdict on the write just made. Exits 0/1/2 per docs/exit-code-contract.md |
| `generate-work-package` | stable; reads a JSON/YAML Stage 7 plan and descriptor-backed canonical closure, promotion, boundary, interaction and bridge inputs, derives the manifest with scripts/work_package_manifest.py, and writes only `ci/manifest/<work_package>.json` after the derived document passes `validate-work-package` and an active exact-path `write` grant for the same issue/path is verified (#122). The issue must match the plan; toolchain/target/features are explicit and selected interaction configuration is checked. Refused, writing nothing, for an invalid/mismatched plan, invalid authoritative inputs, missing/expired/wrong-issue/wrong-operation/wrong-path/replayed grant, a conflicting target, a symlink/escaping target or invalid audit history. Identical bytes are `unchanged`. Each successful run appends a strict event with issue, grant id, path, before/after hashes and command result to `ci/results/protected-writes.jsonl`; this event is ignored by the grant reader and is used only to prevent replay after output deletion. `--json` emits that event or a structured error with stable code, message and required inputs; exits 0/2 per docs/exit-code-contract.md |
| `validate` | alias → `validate boundary` |
| `validate-interaction` | alias → `validate interaction` |
| `validate-exemption` | alias → `validate exemption` |
| `validate-protocol-debt` | alias → `validate protocol-debt` |
| `validate-bridge` | alias → `validate bridge` |
| `validate-evidence` | alias → `validate evidence` |
| `validate-conflict-resolution` | alias → `validate conflict-resolution` |
| `validate-work-package` | alias → `validate work-package` |
| `validate-promotion` | alias → `validate promotion` |
| `validate-callsites` | alias → `validate callsites` |
| `validate-witness` | alias → `validate witness` |
| `validate-gold-set` | alias → `validate gold-set` |
| `validate-closure` | alias → `validate closure` |
| `gate-r1-g16` | alias → `gate r1-g16` |
| `gate-g9` | alias → `gate g9` |
| `gate-g14` | alias → `gate g14` |
| `gate-g18` | alias → `gate g18` |
| `gate-g19` | alias → `gate g19` |
| `gate-g20` | alias → `gate g20` |
| `draft` | stable (already nested; no flat legacy form) |
| `approve` | alias → `approve draft` |
| `approve-pair` | alias → `approve pair` |
| `approve-exemption-pair` | alias → `approve exemption-pair` |
| `accept-promotion` | alias → `approve promotion` |
| `accept-policy` | stable; records a reviewed change to the normative reliance-policy document as the manifest's reviewed `base_hash` (the explicit accept path for governance drift; `--policy-path` defaults to the descriptor's `compatibility_policy.reliance_policy_path`, and a disagreeing explicit path is refused) (#78, §8). `--version <name>@<major>.<minor>` stamps the document's single `Policy version:` marker line first — that line and nothing else — so the template `init` installs can reach an accepted state, and be re-versioned, without a hand-edit of a `protected_root`; a human checkpoint like `approve` and `accept-promotion` (#113, §8) |
| `promote-evidence` | stable; mechanically promotes a staged evidence draft (`evidence/<id>.json.draft`) to its target and records the move in `ci/results/evidence_promotions.jsonl` -- the Stage 0 promotion path `approve` cannot provide (evidence carries no `review` block, so `review_checkpoint.approve()` cannot promote it). No `--reviewer`: evidence is non-normative, so promotion is mechanical, not a human checkpoint (#79, §1) |
| `record-ruling` | stable; records an explicit human verdict (`ratified`\|`rejected`) over an exact artifact set in `ci/results/human_rulings.jsonl` -- the human-ruling gate `accept-promotion` enforces: no receipt is minted until every artifact in the accepted set carries a `ratified` ruling over its current content (#82, §5) |
| `record-assurance` | stable; assembles a work package's assurance report at its manifest's own `report.emit` path from the verifier's proof certificates (`proof.json`) and writes it atomically -- the achieved side `gate g14` reads, and the producer the obligation half of `docs/assurance-report-schema.json` never had (#87, §1). Every record field is derived (certificate + manifest provenance); the caller's only declaration, `--proof <obligation>=<path>`, is checked (obligation provided by this manifest; certificate under a directory named for the obligation's concept; certificate not older than its Coma program) and a certificate with a stuck subgoal records nothing. A feature ledger already sitting at that path is replaced with a warning naming the collision; unrecognizable content is refused. Exits 0/1/2 per docs/exit-code-contract.md |
| `select-pilot-cluster` | alias → `report pilot-cluster` |
| `measure-gold-set` | alias → `report gold-set-measurement` |
| `generate-feature-ledger` | alias → `report feature-ledger` |
| `generate-contact-sheet` | alias → `report contact-sheet` |
| `extract-c-static` | internal operation `check` may recommend, never runs (§7) |
| `check-bridges` | internal operation `check` may recommend, never runs (§7) |
| `render-witness` | internal operation `check` may recommend, never runs (§7) |
| `status` | stable project-state query (#56, §8) — reserved unconditionally, not covered by §9's floor |
| `check` | stable read-only consolidated gate run + one recommended next action (#56, §7) |
| `doctor` | stable capability/install-diagnostics report; owns the capability text `status` used to print, verifies the skill authority hash (§8, #58), reports/verifies the executable's own build attestation (#57), and fails closed on the descriptor `--descriptor` names (§8, #74/#109) |
| `version` | stable; reports product version and the executable's build identity, `--verify` recomputes and checks the embedded attestation, and both report the build's source provenance (`source_commit`, and `source_dirty`/`working_tree_diff_hash` for a dirty tree) (§1, §8, #57, #64) |
| `gate` | stable; nested cross-artifact gate runner, `gate <gate-id>` dispatches the six standalone gates in §3 (#57 grammar v1.0) |
| `report` | stable; nested reporting verb, `report <report-id>` dispatches the four report generators in §6 (#57 grammar v1.0) |
| `init` | stable; installs mode-correct managed files + versioned skill into a target repo, plus the two witness-renderer scripts G13 hash-pins (#84, §8, #58) |
| `migrate` | stable; explicit recovery/upgrade for installed managed files (#58) and `--assumptions` reference migration (#59 phase C). `--upgrade` also refreshes the manifest's `gate_hashes` (the pins `gate_integrity` checks) from the actually installed managed files, updates only managed installation metadata, preserves the user-owned descriptor and normative policy, keeps a locally modified managed file's old pin instead of adopting the drifted bytes, and `--json` emits the machine-readable audit result (old/new version, changed paths, hashes, required human action) so `doctor`, `status --json`, `check --json` and `write-set-check` all read the same post-migration state (#117) |

No command from today's registered set is deliberately unsupported —
every one of the 45 has a nested home, an internal-operation classification,
or (for `status`) a documented retirement. This table is exhaustive by
construction: `tests/test_cli_contract.py` fails if
`pipeline.registered_commands()` ever contains a name absent from it, or
this table ever names a command that command registration removed.

## 11. Versioning

This document is grammar **v1.0**. A breaking change to the grammar
(removing/renaming a top-level verb, changing an artifact-kind/gate-id
vocabulary in an incompatible way) requires a new major grammar version and
follows the alias-removal floor in §9. Additive changes (a new
artifact-kind, a new gate-id, a new report-id) are minor and do not require
a version bump — they extend an already-open enumeration.
