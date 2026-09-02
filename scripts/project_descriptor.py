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
from schema_utils import make_validator  # noqa: E402

DESCRIPTOR_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "project-descriptor.schema.json"


class ProjectDescriptorError(Exception):
    pass


def load_project_descriptor(path: Path) -> dict:
    schema = json.loads(DESCRIPTOR_SCHEMA_PATH.read_text())
    validator = make_validator(schema)

    data = json.loads(path.read_text())
    errors = list(validator.iter_errors(data))
    if errors:
        raise ProjectDescriptorError(
            f"project descriptor {path} is invalid:\n"
            + "\n".join(f"  - {e.message}" for e in errors)
        )
    return data


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
