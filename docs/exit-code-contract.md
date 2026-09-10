# Ligature exit-code contract — v1.0

Chainlink #55. Reconciles two exit-code conventions that exist side by side
in the codebase today:

- Every standalone `scripts/validate_*.py`/`scripts/gate_*.py` `main()`
  (each independently invocable, e.g. `python3 scripts/validate_evidence.py`)
  returns **2** for a precondition/usage error (missing workspace, bad
  `--interactions` syntax, `FileNotFoundError`) and **0**/**1** for
  pass/findings, per `scan_summary.py`'s shared pattern.
- `scripts/pipeline.py`'s own dispatcher (`main()`) catches every
  `PipelineError` — which today covers BOTH "workspace does not exist" AND
  "no C_static reports, run extract-c-static first" AND ordinary findings —
  and collapses all of them to a single exit **1**. `cmd_gate_r1_g16` is the
  one exception, returning a genuine three-valued 0/1/3.

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
| **0** | Clean / success. Ran to completion, nothing to report. | universal today |
| **1** | Blocking findings. The input was valid and checkable; the check ran and found a deterministic, unambiguous problem (schema violation, G1b naming mismatch, unresolved-critical R1/G16, etc). | every `validate-*`/`gate-*`'s own findings path |
| **2** | Invalid input or product/install state. The requested operation could not even be attempted: missing workspace/descriptor, malformed CLI arguments, a missing prerequisite artifact directory ("no C_static reports, run extract-c-static first"), an incompatible installed product/schema version (#57/#58). | every standalone script's own `main()` |
| **3** | Human decision required. The check ran to completion and found nothing that can be resolved mechanically — only a disposition only a human can make (`gate-r1-g16`'s medium risk tier: neither clearly safe nor definitely blocking). | `cmd_gate_r1_g16` today |
| **4** | Missing/unavailable external backend. An operation needed an external system that isn't there or didn't respond usably — no `llm_backend` configured for `draft`, a configured backend that errored, a verifier dispatch target unreachable for `check-bridges`/`gate-g9`. Distinct from 2: the *request* was well-formed, the *environment it needs* wasn't available. | not distinguished today — currently folds into 1 via `PipelineError` (see "Known drift" below) |

Values 5+ are reserved; this contract does not assign them.

## Precedence

When a single command run could report more than one of these
simultaneously, the **highest-precedence condition present wins** and is
the code returned (never a bitmask, never "the last one computed"):

```
2  (invalid input/state)         highest — nothing else can be trusted
1  (blocking findings)
4  (missing/unavailable backend)
3  (human decision required)
0  (clean/success)               lowest
```

Rationale for the middle two: a genuine blocking finding is unambiguous
and actionable without any further information, so it outranks "an
external backend was unavailable" — you don't need the backend to know the
workspace has a real problem. "Backend unavailable" in turn outranks
"human decision required": exit 3 is a *complete, precise* diagnosis (the
mechanized checks ran fully and the only remaining gap is a human call);
exit 4 means the mechanized checks could not even finish running, which is
a less complete state than 3 and must not be silently reported as if it
were the more precise one.

`scripts/exit_codes.py` implements this precedence as a pure function,
`resolve(conditions: frozenset[str]) -> int`, over the same five names
(`invalid_input`, `blocking_findings`, `backend_unavailable`,
`human_decision_required`, plus the empty set meaning clean) so #56's
`check`/`status` implementation calls one shared resolver instead of
re-deriving this table. It has no side effects and does not read the
filesystem or invoke any gate — it is pure precedence arithmetic over a
set the caller already computed, tested directly by
`tests/test_exit_code_contract.py`.

## Compatibility aliases

Per `docs/cli-contract.md` §9, every compatibility alias (e.g. `validate`
for `validate boundary`) MUST return the same code its stable nested form
would for identical input — an alias is routing, never a second exit-code
policy.

## Known drift (not fixed by this task)

`pipeline.py`'s current `except PipelineError: return 1` collapses what
should be exit 2 (workspace/descriptor missing, precondition artifacts
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
scope boundary (inventory and contracts, not the state engine).
