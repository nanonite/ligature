# Reliance policy — `<project name>`

> Copy this file to `docs/reliance-policy.md` in the target project (the
> path the project structure descriptor's `compatibility_policy.reliance_policy_path`
> points at, plan.md §1.1) and fill in the placeholders. This is **standing
> governance, not a one-time artifact** — version it like any other policy
> doc, and any future change to it goes through the same human-authority
> checkpoint as everything else in plan.md §7.2.

Schema version this policy targets: `<boundary-contract-schema version>`.
Owner: `<team or individual accountable for changes>`.

## Resolution rule (fixed — do not override)

This table resolves the schema-prose vs. G2+ conflict (plan.md §2) and is
identical across every project using this pipeline. It is not a
project-specific choice:

| Source in the concept spec | Lands in the boundary artifact as |
|---|---|
| callee **precondition** | bridge specification (plan.md §8.2) — never a `callee_guarantees` entry |
| callee **postcondition** / **invariant** relied on | `callee_guarantees` |
| adversary case (`A*`) | evidence / test seed — **never** a guarantee, never eligible for `callee_guarantees` |

G2+ (role safety, plan.md §12) enforces the third row mechanically: an
adversary-case id inside `callee_guarantees` is a hard error, not a style
warning.

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
