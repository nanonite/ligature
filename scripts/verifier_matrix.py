#!/usr/bin/env python3
"""The versioned tested-verifier/toolchain compatibility matrix (chainlink #174).

This module is the single reader for ``docs/verifier-compatibility-matrix.json``.
It deliberately treats the matrix as data:

* every entry carries its exact verifier, Rust, solver/support-tool,
  platform and source/content pins;
* every result names its evidence, and that evidence is hashed into the
  document so a later edit is visible;
* every entry distinguishes ``intake-only``, ``synthetic`` and
  ``project-level`` results and states its exclusions;
* a Creusot entry must declare the ``cargo-creusot``/``creusot-std`` pair,
  and the pair check is recomputed here rather than trusted from the file;
* the source identity and Nix lock identity are recomputed from the pins,
  so a project can pin exactly the selection it reviewed.

The module is also used by ``build_zipapp`` to bundle the matrix's retained
evidence and to put a compact matrix digest into the build attestation. No
function here writes to a workspace.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import resources
from schema_utils import make_validator

MATRIX_RELPATH = ("docs", "verifier-compatibility-matrix.json")
MATRIX_SCHEMA_RELPATH = ("docs", "verifier-compatibility-matrix-schema.json")
MATRIX_FORMAT = "ligature.verifier-compatibility-matrix.v1"
RESULT_CLASSES = ("intake-only", "synthetic", "project-level")


class MatrixError(Exception):
    """The compatibility matrix is missing, unreadable, or internally
    inconsistent. Always fail closed: a build that cannot prove which
    toolchains it was tested with must not claim a tested matrix."""


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256_document(value) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def matrix_path(root: Path | None = None) -> Path:
    if root is not None:
        return Path(root).joinpath(*MATRIX_RELPATH)
    return resources.resource_path(*MATRIX_RELPATH)


def matrix_schema_path(root: Path | None = None) -> Path:
    if root is not None:
        return Path(root).joinpath(*MATRIX_SCHEMA_RELPATH)
    return resources.resource_path(*MATRIX_SCHEMA_RELPATH)


def matrix_bytes(root: Path | None = None) -> bytes:
    try:
        return matrix_path(root).read_bytes()
    except OSError as exc:
        raise MatrixError(f"cannot read verifier compatibility matrix {matrix_path(root)}: {exc}") from exc


def matrix_sha256(root: Path | None = None) -> str:
    return "sha256:" + hashlib.sha256(matrix_bytes(root)).hexdigest()


def load_matrix(root: Path | None = None) -> dict:
    """Load and semantically validate the bundled matrix.

    JSON Schema validation is necessary but not sufficient: pair
    compatibility, lock identity, source identity, selectable-state and
    evidence hash agreement are recomputed here because they are the
    properties this feature exists to guarantee. Any failure raises
    :class:`MatrixError`; callers surface it instead of printing a partial
    matrix.
    """
    raw = matrix_bytes(root)
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MatrixError(f"verifier compatibility matrix {matrix_path(root)} is not valid JSON: {exc}") from exc

    try:
        schema = json.loads(matrix_schema_path(root).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise MatrixError(f"cannot read verifier compatibility matrix schema: {exc}") from exc
    errors = list(make_validator(schema).iter_errors(document))
    if errors:
        details = "; ".join(
            f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
            for error in errors[:8]
        )
        raise MatrixError(f"verifier compatibility matrix is invalid: {details}")

    if document.get("format") != MATRIX_FORMAT:
        raise MatrixError(
            f"verifier compatibility matrix format must be {MATRIX_FORMAT!r}, "
            f"got {document.get('format')!r}"
        )

    seen: set[str] = set()
    for index, entry in enumerate(document["entries"]):
        entry_id = entry.get("id", f"<entry {index}>")
        if entry_id in seen:
            raise MatrixError(f"verifier compatibility matrix repeats entry id {entry_id!r}")
        seen.add(entry_id)

        declared_identity = entry.get("source_identity")
        computed_identity = source_identity(entry)
        if declared_identity != computed_identity:
            raise MatrixError(
                f"matrix entry {entry_id!r} source_identity does not match its pins: "
                f"declared {declared_identity}, computed {computed_identity}"
            )

        pair_state, pair_detail = creusot_pair_state(entry)
        if entry.get("verifier") == "creusot":
            declared = entry.get("compatibility")
            if declared is None:
                raise MatrixError(
                    f"Creusot matrix entry {entry_id!r} must declare its cargo-creusot/creusot-std pair state"
                )
            if declared.get("state") != pair_state:
                raise MatrixError(
                    f"matrix entry {entry_id!r} declares compatibility {declared.get('state')!r}, "
                    f"but the recomputed pair state is {pair_state!r}: {pair_detail}"
                )
        if entry.get("selectable") and (entry.get("status") != "tested" or pair_state == "mismatch"):
            raise MatrixError(
                f"matrix entry {entry_id!r} is selectable but is not a tested compatible toolchain"
            )

        nix = entry.get("nix")
        if nix is not None:
            computed_lock = nix_identity(nix)
            if nix.get("lock_id") != computed_lock:
                raise MatrixError(
                    f"matrix entry {entry_id!r} nix.lock_id does not match its lock/source identity: "
                    f"declared {nix.get('lock_id')}, computed {computed_lock}"
                )
            if nix.get("lock") is not None and nix.get("store") is not None:
                raise MatrixError(
                    f"matrix entry {entry_id!r} must use either a flake lock or a store identity, not both"
                )
            for input_name, flake_input in nix.get("flake_inputs", {}).items():
                if not flake_input.get("locked", {}).get("narHash"):
                    raise MatrixError(
                        f"matrix entry {entry_id!r} flake input {input_name!r} has no locked.narHash"
                    )
        if entry.get("verifier") == "creusot" and entry.get("selectable") and nix is None:
            raise MatrixError(
                f"selectable Creusot entry {entry_id!r} must publish a Nix lock/source identity"
            )
        if entry.get("verifier") == "creusot" and nix is not None and not nix.get("creusot_std_source_nar_hash"):
            raise MatrixError(
                f"Creusot entry {entry_id!r} must pin the creusot-std source NAR hash"
            )

    return document


def _source_material(entry: dict) -> dict:
    material = {
        "verifier": entry["verifier"],
        "platform": entry["platform"],
        "toolchain": entry["toolchain"],
    }
    nix = entry.get("nix")
    if nix is not None:
        material["nix"] = {
            "manager": nix["manager"],
            "flake_inputs": nix.get("flake_inputs"),
            "store": nix.get("store"),
            "lock_id": nix["lock_id"],
        }
    if entry.get("source") is not None:
        material["source"] = entry["source"]
    return material


def nix_identity(nix: dict) -> str:
    """The canonical lock id for a Nix record: the flake lock document when
    retained, otherwise the content-addressed store/source identity."""
    if nix.get("lock") is not None:
        return _sha256_document(nix["lock"])
    if nix.get("store") is not None:
        return _sha256_document(nix["store"])
    raise MatrixError("nix record has neither a flake lock nor a store identity")


def source_identity(entry: dict) -> str:
    """A stable sha256 over the exact pins a selection resolves to."""
    return _sha256_document(_source_material(entry))


def creusot_pair_state(entry: dict) -> tuple[str, str]:
    """Return (``compatible``|``mismatch``, detail) for a Creusot entry.

    The checked pair is the primary ``cargo-creusot`` version and the single
    ``role: verifier-std`` ``creusot-std`` supporting tool. Exact equality is
    deliberate: the measured compatible 0.12 pair is
    ``0.12.0-dev``/``0.12.0-dev``, while the unresolved declared shell is
    ``0.14.0-dev``/``0.14.0`` and must not be treated as tested.
    Non-Creusot entries are not applicable and return ``compatible`` so the
    caller can ignore the result.
    """
    if entry.get("verifier") != "creusot":
        return "compatible", "not a Creusot entry"
    primary = entry.get("toolchain", {}).get("primary", {})
    std_tools = [
        tool
        for tool in entry.get("toolchain", {}).get("supporting_tools", [])
        if tool.get("role") == "verifier-std" or tool.get("name") == "creusot-std"
    ]
    if len(std_tools) != 1:
        return "mismatch", f"expected exactly one creusot-std supporting tool, found {len(std_tools)}"
    primary_version = primary.get("version")
    std_version = std_tools[0].get("version")
    if primary_version != std_version:
        return (
            "mismatch",
            f"cargo-creusot {primary_version} does not exactly match creusot-std {std_version}",
        )
    return (
        "compatible",
        f"cargo-creusot {primary_version} and creusot-std {std_version} are the same measured pair",
    )


def entries_by_id(matrix: dict | None = None) -> dict[str, dict]:
    document = matrix if matrix is not None else load_matrix()
    return {entry["id"]: entry for entry in document["entries"]}


def selectable_entries(matrix: dict | None = None) -> list[dict]:
    document = matrix if matrix is not None else load_matrix()
    return [entry for entry in document["entries"] if entry.get("selectable")]


def evidence_paths(matrix: dict | None = None) -> list[str]:
    """Every retained evidence path the matrix references, in stable order."""
    document = matrix if matrix is not None else load_matrix()
    paths: list[str] = []
    for entry in document["entries"]:
        for item in entry["evidence"]:
            path = item["path"]
            if path not in paths:
                paths.append(path)
    return paths


def evidence_errors(matrix: dict | None = None, root: Path | None = None) -> list[str]:
    """Check that every referenced evidence file exists and matches its hash.

    This is separate from :func:`load_matrix` because a caller may only need
    the matrix metadata; `version --verify` and `toolchain check` include it.
    """
    document = matrix if matrix is not None else load_matrix()
    base = Path(root) if root is not None else resources.resource_root()
    errors: list[str] = []
    for entry in document["entries"]:
        for item in entry["evidence"]:
            path = base / item["path"]
            try:
                digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                errors.append(
                    f"matrix entry {entry['id']!r} evidence {item['path']!r} is missing or unreadable: {exc}"
                )
                continue
            if digest != item["sha256"]:
                errors.append(
                    f"matrix entry {entry['id']!r} evidence {item['path']!r} has {digest}, "
                    f"but the matrix pins {item['sha256']}"
                )
    return errors


def matrix_summary(matrix: dict | None = None, root: Path | None = None) -> dict:
    """A compact, JSON-safe description for `version`/`doctor` and for the
    build attestation. The full entry documents remain in the matrix file;
    this is what an operator needs to see which exact pins were tested."""
    document = matrix if matrix is not None else load_matrix()
    entries = []
    for entry in document["entries"]:
        toolchain = entry["toolchain"]
        entries.append({
            "id": entry["id"],
            "verifier": entry["verifier"],
            "status": entry["status"],
            "selectable": bool(entry["selectable"]),
            "platform": entry["platform"],
            "primary": dict(toolchain["primary"]),
            "supporting_tools": [dict(tool) for tool in toolchain["supporting_tools"]],
            "rust": dict(toolchain["rust"]),
            "coverage": dict(entry["coverage"]),
            "compatibility": dict(entry["compatibility"]) if entry.get("compatibility") else None,
            "source_identity": entry["source_identity"],
            "lock_id": (entry.get("nix") or {}).get("lock_id"),
            "flake_inputs": (entry.get("nix") or {}).get("flake_inputs"),
            "store": (entry.get("nix") or {}).get("store"),
            "evidence": [
                {
                    "id": item["id"],
                    "result_class": item["result_class"],
                    "path": item["path"],
                    "sha256": item["sha256"],
                }
                for item in entry["evidence"]
            ],
            "exclusions": list(entry["exclusions"]),
        })
    return {
        "format": document["format"],
        "matrix_version": document["matrix_version"],
        "updated_at": document["updated_at"],
        "published_by": dict(document["published_by"]),
        "sha256": matrix_sha256(root),
        "entry_count": len(entries),
        "tested_entry_count": sum(1 for entry in entries if entry["status"] == "tested"),
        "entries": entries,
    }


def render_summary(summary: dict, *, indent: str = "  ", verbose: bool = True) -> str:
    """Human-readable matrix summary for `version` and `doctor`.

    The first line always names the matrix version, content hash and tested
    count. With ``verbose`` each entry gets one compact row plus its
    evidence/result-class and exclusions, so a toolchain that launched but
    proved nothing cannot be mistaken for proof coverage.
    """
    lines = [
        (f"{indent}verifier matrix: {summary['format']} "
         f"(matrix {summary['matrix_version']}, {summary['tested_entry_count']}/"
         f"{summary['entry_count']} tested, {summary['sha256']})")
    ]
    for entry in summary["entries"]:
        primary = entry["primary"]
        support = ", ".join(
            f"{tool['name']} {tool['version']}" for tool in entry["supporting_tools"]
        )
        pair = ""
        if entry.get("compatibility"):
            pair = f" pair={entry['compatibility']['state']}"
        lock = entry.get("lock_id") or "none"
        lines.append(
            f"{indent}  {entry['id']}: {entry['verifier']} {primary['version']}"
            + (f" + {support}" if support else "")
            + f"; rust {entry['rust']['toolchain']}; "
            f"{entry['platform']['os']}/{entry['platform']['architecture']}; "
            f"status={entry['status']}{pair}; selectable={str(entry['selectable']).lower()}; "
            f"coverage=intake:{str(entry['coverage']['intake']).lower()},"
            f"synthetic:{str(entry['coverage']['synthetic']).lower()},"
            f"project-level:{str(entry['coverage']['project_level']).lower()}"
        )
        if not verbose:
            continue
        classes = ", ".join(
            f"{item['result_class']} ({item['id']}: {item['path']})"
            for item in entry["evidence"]
        )
        lines.append(f"{indent}    evidence: {classes}")
        lines.append(f"{indent}    source identity: {entry['source_identity']}; nix lock: {lock}")
        for exclusion in entry["exclusions"]:
            lines.append(f"{indent}    exclusion: {exclusion}")
    return "\n".join(lines) + "\n"
