#!/usr/bin/env python3
"""Shared project-descriptor loading (plan.md §1.1).

Factored out of pipeline.py so both it and validate_work_package.py can
load a project descriptor and derive each crate's canonical boundary
directory without importing from each other -- pipeline.py already
imports from validate_work_package.py (its draft/validate commands), so
the reverse direction would be a circular import.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resources  # noqa: E402
from schema_utils import make_validator  # noqa: E402

DESCRIPTOR_SCHEMA_PATH = resources.resource_path("schemas", "project-descriptor.schema.json")


class ProjectDescriptorError(Exception):
    """The project descriptor failed to load: either it is not valid JSON
    or it does not satisfy schemas/project-descriptor.schema.json.

    `diagnostics` carries one actionable line per problem (see
    schema_diagnostics), so a caller can name the offending property
    instead of only saying the descriptor is invalid (chainlink #73)."""

    def __init__(self, message: str, diagnostics: list[str] | None = None):
        super().__init__(message)
        self.diagnostics = list(diagnostics or [])


def _diagnostic_line(error) -> str:
    """One actionable line for a schema violation: the offending value's
    JSON path, what was wrong, and -- where the schema itself states
    them -- the permitted alternatives (chainlink #73).

    The generic fallback is jsonschema's own message, already prefixed
    with the JSON path; the special cases below exist because the raw
    messages name neither the rejected property nor what was allowed:
    additionalProperties reports the whole object as "$" with the
    unexpected key buried in prose, and the if/then/else guard prints the
    entire descriptor as the "instance"."""
    path = error.json_path
    validator_name = error.validator
    schema = error.schema if isinstance(error.schema, dict) else {}
    if validator_name == "additionalProperties" and isinstance(error.instance, dict):
        properties = schema.get("properties", {})
        unexpected = [key for key in error.instance if key not in properties]
        if unexpected:
            names = ", ".join(repr(key) for key in unexpected)
            permitted = sorted(properties)
            allowed = ", ".join(permitted) if permitted else "(none)"
            return (
                f"{path}: unexpected propert{'y' if len(unexpected) == 1 else 'ies'} "
                f"{names}; permitted: {allowed}"
            )
    if validator_name == "enum" and "enum" in schema:
        return f"{path}: {error.instance!r} is not one of {schema['enum']}"
    if validator_name == "const" and "const" in schema:
        return f"{path}: must be {schema['const']!r} (got {error.instance!r})"
    if validator_name == "required" and isinstance(error.instance, dict):
        missing = [name for name in (error.validator_value or []) if name not in error.instance]
        if missing:
            names = ", ".join(repr(name) for name in missing)
            return f"{path}: missing required propert{'y' if len(missing) == 1 else 'ies'} {names}"
    if validator_name == "not":
        forbidden = schema.get("not", schema)
        return f"{path}: should not be valid under {forbidden}"
    return f"{path}: {error.message}"


def schema_diagnostics(data: dict) -> list[str]:
    """One actionable line per schema violation in `data`, in validator
    order, deduplicated (chainlink #73).

    `check`/`status` surface these so an invalid project descriptor
    names the offending property -- with its JSON path and the permitted
    alternatives -- instead of reporting `conditions: [invalid_input]`
    next to an empty findings list, which left a descriptor typo
    indistinguishable from a missing descriptor."""
    schema = json.loads(DESCRIPTOR_SCHEMA_PATH.read_text())
    validator = make_validator(schema)
    lines: list[str] = []
    for error in validator.iter_errors(data):
        line = _diagnostic_line(error)
        if line not in lines:
            lines.append(line)
    return lines


def load_project_descriptor(path: Path) -> dict:
    schema = json.loads(DESCRIPTOR_SCHEMA_PATH.read_text())
    validator = make_validator(schema)

    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ProjectDescriptorError(
            f"project descriptor {path} is not valid JSON: {exc}",
            diagnostics=[f"not valid JSON: {exc}"],
        ) from exc
    errors = list(validator.iter_errors(data))
    if errors:
        diagnostics = schema_diagnostics(data)
        raise ProjectDescriptorError(
            f"project descriptor {path} is invalid:\n"
            + "\n".join(f"  - {line}" for line in diagnostics),
            diagnostics=diagnostics,
        )
    return data


def is_underscore_artifact_path(path: Path, specs_search_root: Path) -> bool:
    """True when `path` has an underscore-prefixed path component relative
    to `specs_search_root` -- i.e. it lives inside one of the canonical
    artifact directories this module names (`_boundaries`,
    `_interactions`, `_bridges`, `_exemptions`, `_protocol_debt`,
    `_conflicts`; also `_witnesses`), not in the concept-spec tree itself.

    Cross-file concept resolution must exclude these: several artifacts
    (witness specs, bridge specs) carry their own top-level `concept`
    field, so an unfiltered scan mistakes an artifact that merely names a
    concept for the concept spec that defines it, and reports a spurious
    ambiguity when both exist. One shared predicate so
    validate_witness.py, gate_g18.py, select_pilot_cluster.py, and
    validate_boundary_contracts.py cannot drift apart on the convention
    (chainlink #69)."""
    return any(part.startswith("_") for part in path.relative_to(specs_search_root).parts)


def boundary_dir_for(crate: dict, workspace: Path) -> Path:
    """plan.md's canonical layout, §2: crates/*/specs/_boundaries/*.json --
    always this exact path relative to the crate root, not a separate
    descriptor field."""
    return (workspace / crate["crate_dir"] / "specs" / "_boundaries").resolve()


def boundary_dirs_for_descriptor(descriptor: dict, workspace: Path) -> list[Path]:
    return [boundary_dir_for(crate, workspace) for crate in descriptor["crates"]]


def interaction_dir_for(crate: dict, workspace: Path) -> Path:
    """plan.md §5.1's canonical layout: crates/*/specs/_interactions/*.json,
    the same discipline as boundary_dir_for."""
    return (workspace / crate["crate_dir"] / "specs" / "_interactions").resolve()


def interaction_dirs_for_descriptor(descriptor: dict, workspace: Path) -> list[Path]:
    return [interaction_dir_for(crate, workspace) for crate in descriptor["crates"]]


def exemption_dir_for(crate: dict, workspace: Path) -> Path:
    """plan.md §5.2's canonical layout: crates/*/specs/_exemptions/*.json,
    the same discipline as boundary_dir_for."""
    return (workspace / crate["crate_dir"] / "specs" / "_exemptions").resolve()


def exemption_dirs_for_descriptor(descriptor: dict, workspace: Path) -> list[Path]:
    return [exemption_dir_for(crate, workspace) for crate in descriptor["crates"]]


def protocol_debt_dir_for(crate: dict, workspace: Path) -> Path:
    """plan.md §5.3's canonical layout: crates/*/specs/_protocol_debt/*.json,
    the same discipline as boundary_dir_for."""
    return (workspace / crate["crate_dir"] / "specs" / "_protocol_debt").resolve()


def bridge_dir_for(crate: dict, workspace: Path) -> Path:
    """plan.md §8.2's canonical layout: crates/*/specs/_bridges/*.json,
    the same discipline as boundary_dir_for. Crate-scoped, not
    workspace-level, since a bridge is tied to one boundary_id, which is
    itself always in the same crate as its boundary contract."""
    return (workspace / crate["crate_dir"] / "specs" / "_bridges").resolve()


def bridge_dirs_for_descriptor(descriptor: dict, workspace: Path) -> list[Path]:
    return [bridge_dir_for(crate, workspace) for crate in descriptor["crates"]]


def protocol_debt_dirs_for_descriptor(descriptor: dict, workspace: Path) -> list[Path]:
    return [protocol_debt_dir_for(crate, workspace) for crate in descriptor["crates"]]


def evidence_dir_for(workspace: Path) -> Path:
    """plan.md §11's canonical layout: evidence/*.json. Unlike every dir
    helper above, this takes no `crate` -- evidence is workspace-level,
    not crate-scoped (plan.md §7's own artifact_manifest worked example
    lists evidence/E-0143.json with no crate prefix, alongside
    docs/reliance-policy.md, also workspace-level)."""
    return (workspace / "evidence").resolve()


def conflict_dir_for(workspace: Path) -> Path:
    """plan.md §11's canonical layout: specs/_conflicts/*.json. Also
    workspace-level, matching evidence_dir_for's own scope -- see that
    function's docstring."""
    return (workspace / "specs" / "_conflicts").resolve()
