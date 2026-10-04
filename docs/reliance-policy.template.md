# Reliance policy — `<project name>`

> Copy this file to `docs/reliance-policy.md` in the target project (the
> path the project structure descriptor's `compatibility_policy.reliance_policy_path`
> points at, plan.md §1.1) and fill in the placeholders. This is **standing
> governance, not a one-time artifact** — version it like any other policy
> doc, and any future change to it goes through the same human-authority
> checkpoint as everything else in plan.md §7.2.

Schema version this policy targets: `<boundary-contract-schema version>`.
Owner: `<team or individual accountable for changes>`.
Policy version: `<policy-name>@<major>.<minor>`

Fill in the `Policy version:` line above with a real value (e.g. `reliance-policy@1.2`),
as its own line, exactly as shown. `ligature accept-promotion` parses this exact line to
compute a promotion receipt's `policy_version` field (chainlink #45) and refuses to
generate a receipt if it's missing or malformed — and so does `ligature accept-policy`,
which is what records a reviewed change to this document.

The supported way to set that line is the command itself:

```
ligature accept-policy --reviewer <name> --version reliance-policy@1.2
```

It stamps that one line and records the result as the reviewed content of this
document (chainlink #113) — a human checkpoint, like `approve` and
`accept-promotion`. Bump the version the same way on every substantive change to the
resolution rule or this project's own additions below; that is the mechanical
enforcement of "version it like any other policy doc" above, not optional decoration.
Until it carries a value, `ligature doctor` exits 1 and `ligature check` reports a
blocking `policy-version-marker` finding naming this command.

## Resolution rule (fixed — do not override)

This table resolves the schema-prose vs. G2+ conflict (plan.md §2) and is
identical across every project using this pipeline. It is not a
project-specific choice:

| Source in the concept spec | Lands in the boundary artifact as |
|---|---|
| callee **precondition** | `callee_guarantees`, recorded as the obligation the **caller** must establish before the call — not a guarantee the callee provides — and discharged by the bridge specification (plan.md §8.2). A bridge's `callee_requirement` must be one of the boundary's own `callee_guarantees` entries (G2), so an obligation recorded nowhere there is one no bridge can discharge |
| callee **postcondition** / **invariant** relied on | `callee_guarantees` |
| adversary case (`A*`) | evidence / test seed — **never** a guarantee, never eligible for `callee_guarantees` |
| the **caller's own** precondition / invariant | the caller's own contract — never a `callee_guarantees` entry; a bridge cites it in `available_contract_facts` |

G2+ (role safety, plan.md §12) enforces the third row mechanically: an
adversary-case id inside `callee_guarantees` is a hard error, not a style
warning. It also reads each entry's own `kind` and reports a `precondition`
entry as the caller obligation of the first row rather than letting the
field name imply the second — one definition
(`scripts/validate_boundary_contracts.py`'s `callee_obligation_role()`),
read by the gate that reports the role, so "is this a guarantee or an
obligation?" is answered from the artifacts and not from a reader's memory.
An **empty** `callee_guarantees` is therefore a claim, not an omission: it
declares that this call depends on nothing of the callee's and has no
precondition to establish.

## Project-specific additions

Everything below this line is this project's own governance, additive to
the fixed rule above — it can narrow or clarify, but never contradict or
weaken the resolution table.

- **Domain-specific evidence sources.** `<e.g., which oracle/reference
  implementation counts as authoritative for Mode P differential testing,
  if this project uses the port track (plan.md §11)>`
- **Additional role-safety checks specific to this project's domain**, if
  any, beyond the ones G2+ already enforces unconditionally.
- **Escalation path** for a boundary artifact that appears to need a rule
  this policy doesn't cover — who decides, and how it gets folded back into
  this document once decided (not left as an undocumented one-off).

## Change history

| Version | Date | Reviewer | Change |
|---|---|---|---|
| `0.1` | `<date>` | `<human>` | Initial adoption from the pipeline template |
