# Ligature exit-code contract — v1.0

Chainlink #55. Reconciles two exit-code conventions that exist side by side
in the codebase today:

- Every standalone `scripts/validate_*.py`/`scripts/gate_*.py` `main()`
  (each independently invocable, e.g. `python3 scripts/validate_evidence.py`)
  returns **2** for a precondition/usage error (missing workspace, bad
  `--interactions` syntax, `FileNotFoundError`) and **0**/**1** for
  pass/findings, per `scan_summary.py`'s shared pattern.
- `scripts/pipeline.py`'s dispatcher (`main()`) catches an escaping
  `PipelineError`, `ManifestInputError`, or `SchemaError` and prints a
  user-facing error with exit **1**. Individual command handlers can return
  their documented command-specific codes; `cmd_gate_r1_g16` is one example,
  returning a genuine three-valued 0/1/3. `SchemaError` is used by the
  bundled-schema inspection commands (#157).

This is a real, pre-existing inconsistency, not a hypothetical one: the
same underlying condition (a missing prerequisite directory) exits 2 when
you run `validate_boundary_naming.py` directly and 1 when the identical
check runs inside `pipeline.py gate-r1-g16`'s own precondition guard. This
contract is the reconciled target both call sites converge on; it does not
itself change `pipeline.py`'s current dispatch (that belongs to whichever
of #56/#57/#58 next touches `main()`'s `except PipelineError` handling) —
`tests/test_exit_code_contract.py` tests the **specified** precedence table
below via `scripts/exit_codes.py`'s pure resolver, not `pipeline.py`'s
current behavior, which is called out below as a known, open drift.

## Codes

| code | meaning | today's precedent |
|---|---|---|
| **0** | The selected handler completed its non-failing outcome. This does not promise that a query found no findings: `status` emits a state report and exits 0 even when that report describes an invalid descriptor. Warnings may also accompany a 0 result. | `status`; clean validators and gates; successful operations |
| **1** | A command-specific failure: commonly blocking findings, an operation refusal, an unavailable backend, or a managed-file conflict. Read the command profile below; this is not a single universal category. | Most validators, gates, and commands whose `PipelineError` is caught by the dispatcher |
| **2** | Usage error from `argparse`, or a command-specific invalid-input/refusal result. Several commands intentionally use 2 at runtime; the dispatcher also maps uncategorized `PipelineError` to 1, so 2 is not a universal invalid-input code. | Parser usage; selected commands below |
| **3** | A command-specific third result. `gate-r1-g16` and its nested `gate` route use it for a pending human risk decision; `gate-g20` uses it for visible warnings. `check` uses 3 for its human-decision condition. | `gate-r1-g16`, `gate-g20`, `check` |
| **4** | `check`'s current consolidated result for a backend-unavailable condition when no higher-precedence condition is present. The general dispatcher does not currently classify every backend failure separately. | `check` |
| **5** | Gate-integrity failure. The workspace's gate definitions are drifted, missing, or unverifiable against the installation manifest's recorded hashes, so `check` cannot trust its basis. | `check` |

Values 6+ are reserved; this contract does not assign them.

## Precedence

When a single command run could report more than one of these
simultaneously, the **highest-precedence condition present wins** and is
the code returned (never a bitmask, never "the last one computed"):

```
5  (gate-integrity failure)      highest — the check's basis is compromised
2  (invalid input/state)
1  (blocking findings)
4  (missing/unavailable backend)
3  (human decision required)
0  (clean/success)               lowest
```

Rationale for the top condition: a drifted or missing gate definition
voids every other signal the run could produce — if the gates themselves
cannot be trusted, even "your input was invalid" cannot be acted on,
because the descriptor cannot be trusted to declare what is pinned in the
first place (chainlink #75). Rationale for the middle two: a genuine
blocking finding is unambiguous and actionable without any further
information, so it outranks "an external backend was unavailable" — you
don't need the backend to know the workspace has a real problem. "Backend
unavailable" in turn outranks "human decision required": exit 3 is a
*complete, precise* diagnosis (the mechanized checks ran fully and the
only remaining gap is a human call); exit 4 means the mechanized checks
could not even finish running, which is a less complete state than 3 and
must not be silently reported as if it were the more precise one.

`scripts/exit_codes.py` implements this precedence as a pure function,
`resolve(conditions: frozenset[str]) -> int`, over the same six names
(`gate_integrity_failed`, `invalid_input`, `blocking_findings`,
`backend_unavailable`, `human_decision_required`, plus the empty set
meaning clean) for consolidated `check`. `status` emits state and returns
0 when it can produce its report; it does not use this resolver. The helper
has no side effects and does not read the filesystem or invoke any gate —
it is pure precedence arithmetic over a set the caller already computed,
tested directly by `tests/test_exit_code_contract.py`.

## Registered command behavior (current dispatcher)

This section records the behavior of the installed `ligature` dispatcher,
not the aspirational shared vocabulary above. It is intentionally separate
from that target table because legacy commands still use different return
conventions. It covers every top-level name registered by
`pipeline.build_parser()`; aliases use the same handler as their nested
route. An `argparse` syntax/choice error exits 2 before a handler runs.
For runtime behavior, `pipeline.main()` catches `PipelineError`,
`ManifestInputError`, and `SchemaError` and returns 1 unless a command handles that error
itself. In particular, a failed operation may return 1 or 2 depending on
the command.

Only commands whose row says **yes** accept `--json`; other commands reject
that flag. JSON output is the command's documented schema or receipt, not a
generic envelope.

| command(s) | current handler outcome | `--json` |
|---|---|---|
| `check` | Returns `result.exit_code`: 0 clean/non-blocking, 1 blocking finding, 2 invalid input, 3 pending human decision, 4 backend unavailable, or 5 gate-integrity failure, with the shared precedence above. | yes |
| `status` | 0 when the version 1.1 project-state report is produced. State findings are data, not an exit threshold. Default status checks recorded Kani source/harness freshness without rerunning Kani; `--revalidate` opts into G14 reruns. | yes |
| `write-set-check` | 0 clean; 1 conformance violations; 2 when a valid write-set cannot be evaluated. | yes |
| `doctor` | 0 current/healthy; 1 managed-file, descriptor, or policy state needs action; 2 installation metadata is unreadable/unavailable. | no |
| `version` | 0 for a version report or a successful `--verify`; 1 when `--verify` rejects the executable attestation. | no |
| `toolchain` | 0 when `list`/`show`/`plan`/`lock` produces its report or `check` finds no selection/evidence/lock problem; 1 when `check` reports a matrix, evidence or locked-identity problem; 2 for an unknown entry, invalid selection, unreadable descriptor or invalid lock. Read-only. | yes |
| `schema` | 0 when the bundled catalog/schema is inspected or exported; 1 for an unknown bundled schema, unreadable resource, or refused export. Argparse usage errors exit 2. | yes |
| `contract` | 0 when the exact CLI contract bundled in the executable is printed as Markdown or the versioned JSON envelope. | yes |
| `init` | 0 completed without conflicts; 1 managed conflicts/drift; 2 command-handled invalid input/refusal. | no |
| `migrate` | 0 completed without conflicts; 1 managed conflicts/drift or unresolved migration; 2 command-handled invalid input/refusal. | yes |
| `validate`, `validate-interaction`, `validate-exemption`, `validate-protocol-debt`, `validate-bridge`, `validate-evidence`, `validate-conflict-resolution`, `validate-work-package`, `validate-promotion`, `validate-callsites`, `validate-witness`, `validate-gold-set`, `validate-closure` | 0 no blocking validation findings; 1 findings or a dispatcher-caught refusal/error. | no |
| `gate-g9`, `gate-g18`, `gate-g19` | 0 passed; 1 blocked/findings or dispatcher-caught prerequisite error. | no |
| `gate-g14` | 0 when no cluster is blocked; 1 when a cluster is blocked, workspace findings contain errors, or no closure profile exists. | yes |
| `gate-r1-g16` | 0 accepted under current project policy; 1 blocked or dispatcher-caught prerequisite error; 3 human decision required. Acceptance can coexist with visible warnings and incomplete interaction reconciliation. | yes |
| `gate-g20` | 0 pass; 1 structural/blocking finding; 3 visible warning(s). | no |
| `gate` | Delegates to the selected gate: code 0 or 1 for ordinary gates, plus 3 for R1/G16 human decision or G20 warnings. `gate g14 --json` emits `ligature.g14-report.v1`; `gate r1-g16 --json` emits `ligature.r1-g16-report.v1`; other gates reject `--json`. | yes |
| `report` | Delegates to the selected report: pilot selection returns 0/1/2 (eligible/no eligible/invalid report); gold-set measurement returns 0/1 (measured/unmeasurable); generated ledgers and contact sheets return 0/1 (written/refused). | no |
| `select-pilot-cluster` | 0 eligible ranking; 1 no eligible cluster; 2 invalidating input findings. A dispatcher-caught exception returns 1. | no |
| `measure-gold-set` | 0 measurement available, including when omissions are reported; 1 no valid measurement could be produced. | no |
| `generate-feature-ledger`, `generate-contact-sheet` | 0 generated; 1 refusal or generation error. | no |
| `extract-c-static`, `render-witness`, `check-bridges` | 0 operation succeeded; 1 dispatcher-caught refusal/error or failed bridge dispatch/check. | no |
| `accept-promotion`, `record-ruling`, `draft`, `approve`, `approve-pair`, `approve-exemption-pair`, `promote-evidence` | 0 operation completed; 1 refusal, failed review/checkpoint, unavailable draft backend, or dispatcher-caught error. | no |
| `accept-policy`, `accept-fixture-value` | 0 decision recorded; 2 command-handled invalid input/refusal. | no |
| `record-assurance` | 0 report written; 1 evidence does not establish the requested obligation; 2 malformed, unsupported, or otherwise invalid input. No report is written on nonzero exit. | no |
| `authorize-write` | 0 grant recorded; 2 invalid or refused grant request. | yes |
| `scaffold-crate` | 0 created/already scaffolded; 1 post-generation validation failed; 2 invalid or refused request. | yes |
| `configure-cargo-profile`, `configure-crate`, `generate-work-package` | 0 completed/unchanged; 2 command-handled invalid input or authorization refusal. | yes |

The `--json` column is checked against parser registrations by
`tests/test_cli_contract.py`. This table is an observed source-level
contract; release acceptance additionally checks representative outcomes
through the packaged binary. A source-only table is not evidence that a
deployed binary contains those behaviors.

## Compatibility aliases

Per `docs/cli-contract.md` §9, every compatibility alias (e.g. `validate`
for `validate boundary`) MUST return the same code its stable nested form
would for identical input — an alias is routing, never a second exit-code
policy.

## Known dispatcher mismatch with the shared target

`pipeline.py`'s current `except (PipelineError, ManifestInputError): return 1` collapses what
the shared target classifies as exit 2 (workspace/descriptor missing, precondition artifacts
absent) into exit 1 (blocking findings) for every command except
`gate-r1-g16`, which already special-cases its own preconditions to a
`PipelineError` too (still folding to 1 at the `main()` level rather than
2) — meaning even `gate-r1-g16` does not yet reach this contract's exit 2
for its own precondition checks; only its 0/1/3 findings-tier split is
real today. This contract records the target split (`PipelineError`
should carry enough information for `main()` to distinguish "invalid
input/state" from "blocking findings", and a new exception or flag for
"backend unavailable" is needed for exit 4); wiring it is left to
whichever of #56/#57/#58 next changes `main()`'s dispatch, per #55's own
scope boundary (inventory and contracts, not the state engine). The
registered-command matrix above records the runtime result until a separate
behavior change deliberately reconciles this mismatch.
