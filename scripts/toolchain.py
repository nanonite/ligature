#!/usr/bin/env python3
"""Per-project-root Nix toolchain selection for Ligature workspaces (#174).

A project descriptor may declare::

    "toolchains": [
      {
        "project_root": ".",
        "verifier": "creusot",
        "matrix_entry": "creusot-0.12.0-dev-nightly-2026-04-21-x86_64-linux",
        "lock_id": "sha256:...",              # optional exact matrix pin
        "nix": {"lockfile": "flake.lock"}     # optional lock check
      }
    ]

The selection names an exact entry from the tested compatibility matrix.
This module resolves it, rechecks the Creusot/creusot-std pair, compares an
optional committed ``flake.lock`` against the matrix's lock identity, and
renders a deterministic plan or lock document. It never writes a workspace
file and never runs Nix; the emitted identities are what a project commits
and what a later command can compare.
"""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath, PureWindowsPath

import verifier_matrix


class ToolchainError(Exception):
    pass


def _relative_project_root(raw: str, workspace: Path) -> Path:
    posix = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    if posix.is_absolute() or windows.is_absolute() or windows.drive or ".." in posix.parts:
        raise ToolchainError(
            f"project_root {raw!r} must be workspace-relative and may not use an absolute path "
            "or parent traversal"
        )
    candidate = Path(workspace).resolve().joinpath(*posix.parts)
    try:
        candidate.resolve(strict=False).relative_to(Path(workspace).resolve())
    except (OSError, RuntimeError, ValueError):
        raise ToolchainError(
            f"project_root {raw!r} resolves outside the workspace (including through a symlink)"
        )
    return candidate


def _lockfile_path(raw: str, workspace: Path) -> Path:
    posix = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    if posix.is_absolute() or windows.is_absolute() or windows.drive or ".." in posix.parts:
        raise ToolchainError(
            f"toolchain nix.lockfile {raw!r} must be workspace-relative and may not use an "
            "absolute path or parent traversal"
        )
    return Path(workspace).joinpath(*posix.parts)


def selections(descriptor: dict) -> list[dict]:
    declared = descriptor.get("toolchains")
    if declared is None:
        return []
    if not isinstance(declared, list):
        raise ToolchainError("project descriptor toolchains must be an array")
    return declared


def _compare_node(name: str, expected: dict, actual: dict) -> list[str]:
    problems: list[str] = []
    if "locked" in expected and actual.get("locked") != expected["locked"]:
        problems.append(
            f"flake.lock node {name!r} locked identity differs from the matrix pin"
        )
    if "inputs" in expected:
        expected_inputs = expected["inputs"]
        actual_inputs = actual.get("inputs", {})
        for input_name, target in expected_inputs.items():
            if actual_inputs.get(input_name) != target:
                problems.append(
                    f"flake.lock node {name!r} input {input_name!r} points at "
                    f"{actual_inputs.get(input_name)!r}, expected {target!r}"
                )
    return problems


def flake_lock_matches(expected_lock: dict, actual_lock: dict) -> list[str]:
    """Compare the matrix's lock subset against a committed flake.lock.

    Extra nodes in the project lock are allowed (a project may add its own
    inputs); every node, locked identity and input edge the matrix pins must
    match exactly.
    """
    problems: list[str] = []
    if actual_lock.get("version") != expected_lock.get("version"):
        problems.append(
            f"flake.lock version is {actual_lock.get('version')!r}, "
            f"expected {expected_lock.get('version')!r}"
        )
    expected_nodes = expected_lock["nodes"]
    actual_nodes = actual_lock.get("nodes", {})
    for name, expected_node in expected_nodes.items():
        actual_node = actual_nodes.get(name)
        if not isinstance(actual_node, dict):
            problems.append(f"flake.lock is missing matrix-pinned node {name!r}")
            continue
        problems.extend(_compare_node(name, expected_node, actual_node))
    root_name = expected_lock.get("root")
    expected_root = expected_nodes.get(root_name)
    actual_root = actual_nodes.get(root_name)
    if isinstance(expected_root, dict) and isinstance(actual_root, dict):
        problems.extend(_compare_node(str(root_name), expected_root, actual_root))
    return problems


def selection_errors(descriptor: dict, workspace: Path, matrix: dict | None = None) -> list[str]:
    document = matrix if matrix is not None else verifier_matrix.load_matrix()
    by_id = verifier_matrix.entries_by_id(document)
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for index, selection in enumerate(selections(descriptor)):
        prefix = f"toolchains[{index}]"
        if not isinstance(selection, dict):
            errors.append(f"{prefix} must be an object")
            continue
        raw_root = selection.get("project_root")
        verifier = selection.get("verifier")
        entry_id = selection.get("matrix_entry")
        if not isinstance(raw_root, str) or not raw_root:
            errors.append(f"{prefix}.project_root must be a non-empty string")
            continue
        try:
            _relative_project_root(raw_root, workspace)
        except ToolchainError as exc:
            errors.append(f"{prefix}.project_root: {exc}")
            continue
        if not isinstance(verifier, str):
            errors.append(f"{prefix}.verifier must be one of kani, creusot, verus")
            continue
        if not isinstance(entry_id, str) or not entry_id:
            errors.append(f"{prefix}.matrix_entry must be a non-empty string")
            continue
        key = (raw_root, verifier)
        if key in seen:
            errors.append(f"{prefix} repeats the selection for root {raw_root!r} and verifier {verifier!r}")
        seen.add(key)

        entry = by_id.get(entry_id)
        if entry is None:
            errors.append(f"{prefix}.matrix_entry {entry_id!r} is not in the bundled compatibility matrix")
            continue
        if entry["verifier"] != verifier:
            errors.append(
                f"{prefix}.verifier {verifier!r} does not match matrix entry {entry_id!r} "
                f"(verifier {entry['verifier']!r})"
            )
        if entry["status"] != "tested" or not entry.get("selectable"):
            errors.append(
                f"{prefix}.matrix_entry {entry_id!r} is not a tested, selectable toolchain "
                f"(status {entry['status']!r}, selectable {entry['selectable']!r})"
            )
        pair_state, pair_detail = verifier_matrix.creusot_pair_state(entry)
        if entry["verifier"] == "creusot" and pair_state != "compatible":
            errors.append(f"{prefix}.matrix_entry {entry_id!r} has an incompatible Creusot pair: {pair_detail}")

        declared_lock = selection.get("lock_id")
        if declared_lock is not None:
            entry_lock = (entry.get("nix") or {}).get("lock_id")
            if not isinstance(declared_lock, str):
                errors.append(f"{prefix}.lock_id must be a sha256:... string")
            elif entry_lock is None:
                errors.append(f"{prefix}.lock_id is set but matrix entry {entry_id!r} has no Nix lock identity")
            elif declared_lock != entry_lock:
                errors.append(
                    f"{prefix}.lock_id {declared_lock!r} does not match matrix entry {entry_id!r} "
                    f"lock {entry_lock!r}"
                )

        nix = selection.get("nix")
        if nix is not None:
            if not isinstance(nix, dict):
                errors.append(f"{prefix}.nix must be an object")
                continue
            raw_lockfile = nix.get("lockfile")
            if not isinstance(raw_lockfile, str) or not raw_lockfile:
                errors.append(f"{prefix}.nix.lockfile must be a non-empty workspace-relative path")
                continue
            entry_lock_document = (entry.get("nix") or {}).get("lock")
            if entry_lock_document is None:
                errors.append(
                    f"{prefix}.nix.lockfile is declared but matrix entry {entry_id!r} has no "
                    "flake lock; pin lock_id instead"
                )
                continue
            try:
                lockfile = _lockfile_path(raw_lockfile, workspace)
            except ToolchainError as exc:
                errors.append(f"{prefix}.nix.lockfile: {exc}")
                continue
            try:
                actual_lock = json.loads(lockfile.read_text())
            except OSError as exc:
                errors.append(f"{prefix}.nix.lockfile {raw_lockfile!r} is missing or unreadable: {exc}")
                continue
            except json.JSONDecodeError as exc:
                errors.append(f"{prefix}.nix.lockfile {raw_lockfile!r} is not valid JSON: {exc}")
                continue
            for problem in flake_lock_matches(entry_lock_document, actual_lock):
                errors.append(f"{prefix}.nix.lockfile {raw_lockfile!r}: {problem}")
    return errors


def find_selection(descriptor: dict, project_root: str, verifier: str | None) -> dict:
    matches = [
        selection
        for selection in selections(descriptor)
        if selection.get("project_root") == project_root
        and (verifier is None or selection.get("verifier") == verifier)
    ]
    if not matches:
        scope = f"verifier {verifier!r}" if verifier else "any verifier"
        raise ToolchainError(
            f"no toolchain selection for project_root {project_root!r} and {scope}; "
            "add one to project-descriptor.json toolchains[] or pass --entry"
        )
    if len(matches) > 1:
        ids = ", ".join(sorted(str(item.get("matrix_entry")) for item in matches))
        raise ToolchainError(
            f"project_root {project_root!r} has more than one matching toolchain selection ({ids}); "
            "pass --verifier or --entry"
        )
    return matches[0]


def resolve_entry(
    descriptor: dict | None,
    workspace: Path,
    *,
    project_root: str = ".",
    verifier: str | None = None,
    entry_id: str | None = None,
    matrix: dict | None = None,
) -> tuple[dict, dict]:
    document = matrix if matrix is not None else verifier_matrix.load_matrix()
    by_id = verifier_matrix.entries_by_id(document)
    if entry_id is not None:
        entry = by_id.get(entry_id)
        if entry is None:
            raise ToolchainError(f"unknown compatibility-matrix entry {entry_id!r}")
        if verifier is not None and entry["verifier"] != verifier:
            raise ToolchainError(
                f"matrix entry {entry_id!r} is for verifier {entry['verifier']!r}, not {verifier!r}"
            )
    else:
        if descriptor is None:
            raise ToolchainError("a selection needs project-descriptor.json or an explicit --entry")
        selection = find_selection(descriptor, project_root, verifier)
        entry_id = selection.get("matrix_entry")
        entry = by_id.get(str(entry_id))
        if entry is None:
            raise ToolchainError(
                f"selected matrix entry {entry_id!r} is not in the bundled compatibility matrix"
            )
        if verifier is not None and entry["verifier"] != verifier:
            raise ToolchainError(
                f"selection matrix entry {entry_id!r} is for verifier {entry['verifier']!r}, not {verifier!r}"
            )
    if entry["status"] != "tested" or not entry.get("selectable"):
        raise ToolchainError(
            f"matrix entry {entry['id']!r} is not a tested, selectable toolchain "
            f"(status {entry['status']!r}, selectable {entry['selectable']!r})"
        )
    pair_state, pair_detail = verifier_matrix.creusot_pair_state(entry)
    if entry["verifier"] == "creusot" and pair_state != "compatible":
        raise ToolchainError(f"matrix entry {entry['id']!r} has an incompatible Creusot pair: {pair_detail}")
    return entry, document


def plan_document(entry: dict, project_root: str, matrix: dict | None = None) -> dict:
    document = matrix if matrix is not None else verifier_matrix.load_matrix()
    nix = entry.get("nix") or {}
    return {
        "format": "ligature.toolchain-plan.v1",
        "matrix": {
            "format": document["format"],
            "matrix_version": document["matrix_version"],
            "sha256": verifier_matrix.matrix_sha256(),
        },
        "project_root": project_root,
        "entry": entry,
        "verifier": entry["verifier"],
        "platform": entry["platform"],
        "source_identity": entry["source_identity"],
        "lock_id": nix.get("lock_id"),
        "flake_inputs": nix.get("flake_inputs"),
        "lock": nix.get("lock"),
        "store": nix.get("store"),
        "creusot_std_source_nar_hash": nix.get("creusot_std_source_nar_hash"),
    }


def lock_document(entry: dict) -> dict:
    nix = entry.get("nix")
    if nix is None:
        raise ToolchainError(
            f"matrix entry {entry['id']!r} publishes no Nix lock/source identity; "
            "only its selection metadata can be used"
        )
    return {
        "format": "ligature.toolchain-lock.v1",
        "entry_id": entry["id"],
        "verifier": entry["verifier"],
        "platform": entry["platform"],
        "lock_id": nix["lock_id"],
        "source_identity": entry["source_identity"],
        "flake_inputs": nix.get("flake_inputs"),
        "lock": nix.get("lock"),
        "store": nix.get("store"),
        "creusot_std_source_nar_hash": nix.get("creusot_std_source_nar_hash"),
    }


def render_plan_text(plan: dict) -> str:
    entry = plan["entry"]
    toolchain = entry["toolchain"]
    support = ", ".join(
        f"{tool['name']} {tool['version']}" for tool in toolchain["supporting_tools"]
    )
    lines = [
        f"Ligature toolchain plan for project root {plan['project_root']!r}",
        f"  matrix: {plan['matrix']['format']} {plan['matrix']['matrix_version']} ({plan['matrix']['sha256']})",
        f"  entry: {entry['id']}",
        f"  verifier: {entry['verifier']} {toolchain['primary']['version']}"
        + (f" + {support}" if support else ""),
        f"  rust: {toolchain['rust']['toolchain']}"
        + (f" (rustc {toolchain['rust'].get('rustc')}, {toolchain['rust'].get('commit')})"
           if toolchain["rust"].get("rustc") else ""),
        (f"  platform: {entry['platform']['os']}/{entry['platform']['architecture']} "
         f"({entry['platform']['target']})"),
        f"  pair: {entry.get('compatibility', {}).get('state', 'not-applicable')}"
        + (f" -- {entry['compatibility']['detail']}" if entry.get("compatibility") else ""),
        f"  source identity: {plan['source_identity']}",
        f"  nix lock: {plan['lock_id'] or '(none published)'}",
    ]
    for name, flake_input in (plan.get("flake_inputs") or {}).items():
        locked = flake_input["locked"]
        rev = locked.get("rev") or locked.get("url") or locked.get("narHash")
        lines.append(f"  nix input {name}: {flake_input['url']} -> {rev} ({locked['narHash']})")
    store = plan.get("store") or {}
    for name in ("wrapper", "creusot_source"):
        if name in store:
            item = store[name]
            lines.append(
                f"  nix store {name}: {item['path']} ({item['nar_hash']})"
                + (f" ca={item['ca']}" if item.get("ca") else "")
            )
    if plan.get("creusot_std_source_nar_hash"):
        lines.append(f"  creusot-std source NAR: {plan['creusot_std_source_nar_hash']}")
    for item in entry["evidence"]:
        lines.append(
            f"  evidence: {item['result_class']} {item['id']} ({item['path']} {item['sha256']})"
        )
    for exclusion in entry["exclusions"]:
        lines.append(f"  exclusion: {exclusion}")
    return "\n".join(lines) + "\n"


def render_entry_text(entry: dict) -> str:
    plan = plan_document(entry, ".")
    lines = [f"Ligature tested toolchain {entry['id']}", *render_plan_text(plan).splitlines()[1:]]
    return "\n".join(lines) + "\n"


def render_lock_text(document: dict) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"
