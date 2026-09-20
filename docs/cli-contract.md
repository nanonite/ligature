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
commands. `report` is the minimal, explicitly justified extension — the
grammar stays "small," now eleven verbs instead of ten, and every verb
still has exactly one job.

`report` is **not** uniformly read-only — see §6's own write column. This
was wrongly stated as a blanket "read-only" property in an earlier
revision of this contract; it is corrected here because #56/#57 need the
per-report-id truth, not a category label that doesn't hold for three of
the four report-ids.

`--workspace` and `--descriptor` remain global flags on every subcommand,
unchanged from today's `build_parser()`.

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

## 4. `draft <artifact-kind>`

Already effectively nested: `draft <stage> <template_name> <target>` keeps
its shape unchanged, `template_name` filling the `<artifact-kind>` slot
(`evidence-intake`, `boundary-drafting`, `interaction-drafting` today, one
per `prompts/*.md`). No legacy flat commands to reconcile — `draft` was
never flat.

## 5. `approve <operation>`

| operation | legacy flat command | authority |
|---|---|---|
| `draft` | `approve` | `--reviewer` (required), single target |
| `pair` | `approve-pair` | `--reviewer`, interaction + protocol-debt, atomic |
| `exemption-pair` | `approve-exemption-pair` | `--reviewer`, interaction + exemption, atomic (R2 bootstrap) |
| `promotion` | `accept-promotion` | `--reviewer` + `--policy-path` + repeatable `--artifact`; Stage 4.5's own deterministic, no-LLM generator |

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

`action_id` is required whenever `next_action.kind` is `automated-command`
and forbidden otherwise, and is schema-closed to exactly the four values
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

## 10. Legacy command → disposition (complete, 39/39)

Every command `scripts/pipeline.py:build_parser()` registers today,
mapped to exactly one disposition. `tests/test_cli_contract.py` asserts
this table has exactly one row per registered command name and that the
set of names matches `pipeline.registered_commands()` exactly.

| legacy command | disposition |
|---|---|
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
| `select-pilot-cluster` | alias → `report pilot-cluster` |
| `measure-gold-set` | alias → `report gold-set-measurement` |
| `generate-feature-ledger` | alias → `report feature-ledger` |
| `generate-contact-sheet` | alias → `report contact-sheet` |
| `extract-c-static` | internal operation `check` may recommend, never runs (§7) |
| `check-bridges` | internal operation `check` may recommend, never runs (§7) |
| `render-witness` | internal operation `check` may recommend, never runs (§7) |
| `status` | stable project-state query (#56, §8) — reserved unconditionally, not covered by §9's floor |
| `check` | stable read-only consolidated gate run + one recommended next action (#56, §7) |
| `doctor` | stable capability/install-diagnostics report; owns the capability text `status` used to print, verifies the skill authority hash (§8, #58), and reports/verifies the executable's own build attestation (#57) |
| `version` | stable; reports product version and the executable's build identity, `--verify` recomputes and checks the embedded attestation, and both report the build's source provenance (`source_commit`, and `source_dirty`/`working_tree_diff_hash` for a dirty tree) (§1, §8, #57, #64) |
| `gate` | stable; nested cross-artifact gate runner, `gate <gate-id>` dispatches the six standalone gates in §3 (#57 grammar v1.0) |
| `report` | stable; nested reporting verb, `report <report-id>` dispatches the four report generators in §6 (#57 grammar v1.0) |
| `init` | stable; installs mode-correct managed files + versioned skill into a target repo (§8, #58) |
| `migrate` | stable; explicit recovery/upgrade for installed managed files (#58) and `--assumptions` reference migration (#59 phase C) |

No command from today's registered set is deliberately unsupported —
every one of the 39 has a nested home, an internal-operation classification,
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
