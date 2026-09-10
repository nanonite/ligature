# Ligature CLI contract — grammar v1.0

Chainlink #55. This is the stable public surface `ligature` (the future
packaged binary, #57) and `python3 scripts/pipeline.py` (today's source
checkout entrypoint) both commit to. It does **not** implement `init`,
`doctor`, `version`, `migrate`, or the real `status`/`check` — those bodies
are #58, #57, #57, #58, and #56 respectively. This document specifies the
grammar and the mapping every one of today's 32 registered flat
`pipeline.py` subcommands (`scripts/pipeline.py:build_parser()`,
cross-checked by `tests/test_inventory_drift.py` and
`tests/test_cli_contract.py`) resolves to, so #56/#57/#58 implement against
a fixed target instead of inventing one mid-build.

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
ligature migrate                             # #59 (schema/skill migration)
ligature report <report-id>                  # extension verb, see §3
```

`report` is one addition beyond the ten verbs #55's own issue text names.
It exists because four of today's real commands (`select-pilot-cluster`,
`measure-gold-set`, `generate-feature-ledger`, `generate-contact-sheet`)
are none of validate/gate/draft/approve/check: each produces a **read-only,
human-facing artifact or ranking that is not a pass/fail gate and not an
artifact validator**. Forcing them under `check` would misrepresent
one-time planning output (pilot-cluster ranking) and standing review
surfaces (the contact sheet) as part of `check`'s per-run gate loop; giving
them no top-level verb at all would violate #55's own instruction not to
silently lose reporting commands. `report` is the minimal, explicitly
justified extension — the grammar stays "small," now eleven verbs instead
of ten, and every verb still has exactly one job.

`--workspace` and `--descriptor` remain global flags on every subcommand,
unchanged from today's `build_parser()`.

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

| report-id | legacy flat command |
|---|---|
| `pilot-cluster` | `select-pilot-cluster` |
| `gold-set-measurement` | `measure-gold-set` |
| `feature-ledger` | `generate-feature-ledger` |
| `contact-sheet` | `generate-contact-sheet` |

## 7. Internal operations invoked by `check`

Three commands are data-refresh/dispatch steps that feed a `gate`, not
independent user-facing verbs: `extract-c-static` (feeds `gate r1-g16`),
`check-bridges` (feeds `gate g9`), `render-witness` (feeds `gate g18`/`g19`
and the `report` generators). #56's `check` orchestrates these internally
as needed before running the gates that depend on their output.

They are **not** promoted to stable public API by this contract — no
removal version is promised because they are not being deprecated, they
are being internalized. Their existing flat names keep working during the
transition (CI matrices that parallelize C_static extraction by target
triple, for instance, still need direct invocation) but that surface is
explicitly unversioned: it may change shape without notice once `check`'s
own orchestration lands, and is not covered by §9's alias-removal policy.

## 8. The `status` stub → `doctor`

Today's `status` (`cmd_status`, pipeline.py:1909) prints a **capability
manifest** — which flat commands exist and which stages remain
unimplemented. That is categorically different from #56's real
`ligature status`, which will report **project state** (per-artifact
lifecycle, per-obligation assurance, per-cluster closure — see
`schemas/project-state.schema.json`). Reusing the name for both would let
a capability list stand in for a project-state report, exactly the
conflation #55 was opened to prevent.

Resolution: the existing capability summary is renamed to `doctor`'s
capability-reporting half (`doctor` also owns install/version diagnostics
under #57/#58; a capability listing is one more thing a doctor reports).
`status` is reserved exclusively for #56.

Legacy flat `status` becomes a **compatibility alias**: it keeps emitting
today's capability text (unchanged) through product version 1.x, printing
a one-line deprecation notice pointing at `doctor`, and is removed no
earlier than product version 2.0.0 — the version boundary at which #56's
real `ligature status` ships and the bare name must mean project state
unambiguously. Before that boundary, `pipeline.py status` and
`pipeline.py doctor` (once #57/#58 implement the wrapper) are required to
produce identical output; this contract does not allow them to silently
drift into two different capability lists.

## 9. Compatibility alias policy

Every legacy flat command not listed in §7 (internal) becomes a
compatibility alias for its stable nested equivalent (§2 validate, §3
gate, §5 approve, §6 report, §8 status→doctor). An alias:

- Accepts exactly the same arguments and flags as it does today.
- Produces byte-identical stdout/stderr and the same exit code as invoking
  the stable nested form directly (§ exit-code contract,
  `docs/exit-code-contract.md`) — a compatibility alias is a routing
  decision, never a second implementation that can drift from the first.
- Is removed no earlier than product version **2.0.0**. Aliases are not
  planned for removal before then; 2.0.0 is a floor, not a committed
  target date.

## 10. Legacy command → disposition (complete, 32/32)

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
| `extract-c-static` | internal operation invoked by `check` (§7) |
| `check-bridges` | internal operation invoked by `check` (§7) |
| `render-witness` | internal operation invoked by `check` (§7) |
| `status` | alias → `doctor` (§8), until product v2.0.0 |

No command from today's registered set is deliberately unsupported —
every one of the 32 has a nested home, an internal-operation classification,
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
