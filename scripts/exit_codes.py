"""The reconciled exit-code contract (plan.md's own CLI, chainlink #55,
docs/exit-code-contract.md). A pure precedence resolver only -- it does not
read the filesystem, run a gate, or change what any `cmd_*` in pipeline.py
returns today. #56/#57/#58 are expected to call `resolve()` from their own
`check`/`status` dispatch instead of re-deriving this table by hand; this
module makes that table exist in exactly one place so it can't drift
between whichever of them touches exit codes next.
"""
from __future__ import annotations

CLEAN = 0
BLOCKING_FINDINGS = 1
INVALID_INPUT = 2
HUMAN_DECISION_REQUIRED = 3
BACKEND_UNAVAILABLE = 4

# Highest precedence first -- see docs/exit-code-contract.md's own
# rationale for why 4 outranks 3 (a complete diagnosis beats an
# incomplete one) and 1 outranks 4 (an unambiguous finding needs no
# further information to act on).
_PRECEDENCE: tuple[tuple[str, int], ...] = (
    ("invalid_input", INVALID_INPUT),
    ("blocking_findings", BLOCKING_FINDINGS),
    ("backend_unavailable", BACKEND_UNAVAILABLE),
    ("human_decision_required", HUMAN_DECISION_REQUIRED),
)

CONDITION_NAMES = frozenset(name for name, _ in _PRECEDENCE)


def resolve(conditions) -> int:
    """`conditions` is any iterable of condition names drawn from
    CONDITION_NAMES (order and duplicates don't matter -- it's converted to
    a set). Returns the single highest-precedence code; CLEAN (0) if the
    set is empty. Raises ValueError on an unrecognized condition name
    rather than silently ignoring a typo."""
    condition_set = frozenset(conditions)
    unknown = condition_set - CONDITION_NAMES
    if unknown:
        raise ValueError(f"unrecognized exit-code condition(s): {sorted(unknown)}")
    for name, code in _PRECEDENCE:
        if name in condition_set:
            return code
    return CLEAN
