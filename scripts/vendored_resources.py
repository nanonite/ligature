"""Central registry of vendored runtime resources (chainlink #55 round 3).

External review found the prior drift-detection approach -- an AST scanner
matching `pathlib` `/`-join chains for a "vendor" segment -- was
syntax-specific (it would miss `joinpath()`, `os.path.join()`, an f-string,
an import alias, or the future `importlib.resources` form #57 is expected
to introduce) and only counted a reference as real if the path it resolved
to already existed on disk, so a reference to a not-yet-created resource
could evade detection on both sides at once.

Both problems share one fix: stop inferring "what's required" by pattern-
matching how code happens to be written, and declare it here instead, once,
explicitly. Every runtime access to a vendored resource is expected to go
through `vendored_resource_path()` rather than constructing its own
`Path(...) / "vendor" / ...` chain -- `scripts/select_pilot_cluster.py` is
the one caller today. `tests/test_inventory_drift.py` then checks two
syntax-independent things instead of scanning for a join pattern: (1) this
registry's own declared paths exist on disk, matched bidirectionally
against `docs/implementation-inventory.json`'s `required_at_runtime`
entries, and (2) no OTHER `scripts/*.py` file constructs its own path
through a `vendor` segment at all -- a lightweight AST scan kept only as a
bypass detector now, not as the source of truth for what is required.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# name -> workspace-relative path. THE authoritative declaration of which
# vendor/ files are required at runtime. Add an entry here -- and only
# here -- when a new call site needs one; do not hand-build a Path chain
# at the call site itself (see the module docstring and
# tests/test_inventory_drift.py's bypass check).
VENDORED_RESOURCES: dict[str, str] = {
    "concept_to_code_spec_schema": "vendor/concept-to-code/schemas/spec.schema.json",
}


class UnregisteredVendoredResourceError(KeyError):
    pass


def vendored_resource_path(name: str) -> Path:
    """Resolve a registered vendored resource by its stable NAME, never by
    hand-building a path at the call site. #57's future packaged-resource
    discovery is expected to replace this function's own body (today:
    `Path(__file__)`-relative; tomorrow: `importlib.resources` or
    equivalent), not every call site that uses it."""
    try:
        relative = VENDORED_RESOURCES[name]
    except KeyError:
        raise UnregisteredVendoredResourceError(
            f"no vendored resource registered under {name!r} -- see VENDORED_RESOURCES in this module"
        ) from None
    return ROOT / relative
