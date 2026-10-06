#!/usr/bin/env python3
"""Authorized Stage 7 work-package generation (chainlink #122).

The command boundary lives here rather than in the derivation module:
``work_package_manifest`` is pure, while this module loads validated
workspace authority, checks an issue-scoped capability, writes one canonical
manifest atomically, and appends the protected-write audit event.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import resources
import validate_boundary_contracts
import validate_bridge
import validate_closure
import validate_exemption
import validate_interaction
import validate_promotion_receipt
import validate_protocol_debt
import work_package_manifest
import write_authorization
from atomic_write import write_atomically
from manifest_input import ManifestInputError, read_manifest_text
from project_descriptor import (
    boundary_dir_for,
    boundary_dirs_for_descriptor,
    bridge_dir_for,
    exemption_dir_for,
    interaction_dir_for,
    load_project_descriptor,
    protocol_debt_dir_for,
)
from schema_utils import make_validator

AUDIT_EVENT = "work-package-generation"
AUDIT_SCHEMA_VERSION = "1.0"
MANIFEST_DIR = "ci/manifest"
HASH_PREFIX = "sha256:"


class GenerationError(Exception):
    """User-facing refusal with a stable JSON error code."""

    def __init__(
        self, code: str, message: str, required_inputs: list[str] | None = None
    ):
        super().__init__(message)
        self.code = code
        self.required_inputs = required_inputs or []


@dataclass(frozen=True)
class GenerationResult:
    record: dict[str, Any]
    manifest: dict[str, Any]
    findings: list[Any]


def _is_error(finding: Any) -> bool:
    return getattr(finding, "severity", "error") == "error"


def _finding_text(findings: list[Any]) -> str:
    return "; ".join(str(finding) for finding in findings[:8])


def _git_head(workspace: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel", "HEAD"],
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GenerationError(
            "git-head-unavailable",
            f"cannot read repository HEAD for {workspace}: {exc}",
            ["a Git workspace with a committed HEAD"],
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise GenerationError(
            "git-head-unavailable",
            f"cannot read repository HEAD for {workspace}"
            + (f": {detail[-500:]!r}" if detail else ""),
            ["a Git workspace with a committed HEAD"],
        )
    lines = result.stdout.splitlines()
    if len(lines) != 2 or Path(lines[0]).resolve() != workspace.resolve():
        raise GenerationError(
            "workspace-not-git-root",
            f"--workspace must be the Git repository root (resolved Git root: "
            f"{lines[0] if lines else '(unavailable)'})",
            ["--workspace set to the repository root"],
        )
    commit = lines[1].strip().lower()
    if not commit or any(char not in "0123456789abcdef" for char in commit):
        raise GenerationError(
            "git-head-unavailable", "Git returned an invalid HEAD commit"
        )
    return commit


def _load_plan(path: Path) -> work_package_manifest.WorkPackageRequest:
    try:
        text = read_manifest_text(path, "work-package plan")
    except (ManifestInputError, UnicodeDecodeError) as exc:
        raise GenerationError(
            "invalid-plan-path", str(exc), ["--plan <JSON-or-YAML-file>"]
        ) from exc
    try:
        if path.suffix.lower() in {".yaml", ".yml"}:
            import yaml

            data = yaml.safe_load(text)
        else:
            data = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise GenerationError(
            "invalid-plan",
            f"cannot parse plan {path}: {exc}",
            ["a valid JSON or YAML plan"],
        ) from exc
    except Exception as exc:
        # PyYAML exposes YAML errors from a separate package-specific type;
        # keep parser exceptions out of the CLI traceback surface.
        if exc.__class__.__module__.startswith("yaml"):
            raise GenerationError(
                "invalid-plan",
                f"cannot parse plan {path}: {exc}",
                ["a valid JSON or YAML plan"],
            ) from exc
        raise
    try:
        return work_package_manifest.WorkPackageRequest.from_mapping(data)
    except work_package_manifest.ManifestDerivationError as exc:
        raise GenerationError(
            exc.code, str(exc), ["a schema-shaped Stage 7 plan"]
        ) from exc


def _load_authoritative_state(
    workspace: Path,
    descriptor_path: Path,
    *,
    toolchain: str,
    target: str,
    features: list[str],
) -> work_package_manifest.AuthoritativeState:
    """Load only canonical artifacts accepted by their existing validators."""
    workspace = workspace.resolve()
    try:
        descriptor = load_project_descriptor(descriptor_path)
    except Exception as exc:
        raise GenerationError(
            "invalid-descriptor",
            f"cannot load project descriptor: {exc}",
            ["a valid --descriptor"],
        ) from exc

    closure_dir = workspace / "specs" / "_closure"
    try:
        closure_findings = validate_closure.validate_workspace(workspace, closure_dir)
        closure_artifacts = validate_closure.load_cluster_artifacts(workspace)
    except (OSError, ValueError) as exc:
        raise GenerationError(
            "invalid-closure-input", f"cannot validate closure inputs: {exc}"
        ) from exc
    closure_errors = [finding for finding in closure_findings if _is_error(finding)]
    if closure_errors:
        raise GenerationError("invalid-closure-input", _finding_text(closure_errors))
    closure_profiles = tuple(
        value
        for cluster in sorted(closure_artifacts)
        for kind, (_path, value) in sorted(closure_artifacts[cluster].items())
        if kind == "profile"
    )

    promotion_dir = workspace / "specs" / "_promotions"
    promotion_receipts: list[dict[str, Any]] = []
    if promotion_dir.exists():
        if not promotion_dir.is_dir():
            raise GenerationError(
                "invalid-promotion-input",
                f"promotion path is not a directory: {promotion_dir}",
            )
        validator = validate_promotion_receipt.load_validator()
        for path in sorted(
            p
            for p in promotion_dir.iterdir()
            if p.is_file() and p.suffix.lower() in {".json", ".yaml", ".yml"}
        ):
            findings = validate_promotion_receipt.validate_file(
                path, validator, workspace, descriptor
            )
            receipt_errors = [finding for finding in findings if _is_error(finding)]
            if receipt_errors:
                raise GenerationError(
                    "invalid-promotion-input",
                    f"promotion receipt {path} is invalid: {_finding_text(receipt_errors)}",
                )
            try:
                data = validate_promotion_receipt._load_receipt(path)
            except (
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                yaml.YAMLError,
            ) as exc:
                raise GenerationError(
                    "invalid-promotion-input",
                    f"cannot read validated promotion receipt {path}: {exc}",
                ) from exc
            if isinstance(data, dict):
                promotion_receipts.append(data)

    boundaries: list[dict[str, Any]] = []
    interactions: list[dict[str, Any]] = []
    bridges: list[dict[str, Any]] = []
    for crate in descriptor.get("crates", []):
        crate_root = workspace / crate["crate_dir"]
        try:
            crate_root.resolve().relative_to(workspace)
            (workspace / crate["specs_search_root"]).resolve().relative_to(workspace)
        except (OSError, RuntimeError, ValueError) as exc:
            raise GenerationError(
                "unsafe-descriptor-path",
                f"crate or specs_search_root for {crate['crate_dir']!r} resolves outside the workspace",
            ) from exc
        if not crate_root.is_dir():
            continue
        specs_root = workspace / crate["specs_search_root"]
        boundary_dir = boundary_dir_for(crate, workspace)
        interaction_dir = interaction_dir_for(crate, workspace)
        exemption_dir = exemption_dir_for(crate, workspace)
        debt_dir = protocol_debt_dir_for(crate, workspace)
        bridge_dir = bridge_dir_for(crate, workspace)

        boundary_lookup = validate_boundary_contracts.load_boundaries_by_id(
            boundary_dir, specs_root
        )
        boundaries.extend(boundary_lookup.values())
        interaction_lookup = validate_interaction.load_interactions_by_id(
            interaction_dir
        )
        debt_ids = validate_protocol_debt.valid_interaction_ids_from_crate(
            crate_root, debt_dir, interaction_lookup
        )
        exemption_ids = validate_exemption.valid_exemption_interaction_ids_from_crate(
            crate_root, exemption_dir, interaction_lookup
        )
        boundary_edges = validate_boundary_contracts.valid_boundary_edges_from_crate(
            crate_root, boundary_dir, specs_root
        )
        interaction_findings = validate_interaction.validate_crate(
            crate_root, interaction_dir, debt_ids, boundary_edges, exemption_ids
        )
        invalid_interactions = {
            Path(finding.path).resolve()
            for finding in interaction_findings
            if _is_error(finding) and getattr(finding, "path", None) is not None
        }
        if interaction_dir.is_dir():
            for path in sorted(interaction_dir.iterdir()):
                if (
                    not path.is_file()
                    or path.suffix.lower() != ".json"
                    or path.resolve() in invalid_interactions
                ):
                    continue
                if path.stem not in interaction_lookup:
                    continue
                try:
                    data = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(data, dict):
                    interactions.append(data)

        bridge_findings = validate_bridge.validate_crate(
            crate_root, bridge_dir, boundary_lookup
        )
        invalid_bridges = {
            Path(finding.path).resolve()
            for finding in bridge_findings
            if _is_error(finding) and getattr(finding, "path", None) is not None
        }
        if bridge_dir.is_dir():
            for path in sorted(bridge_dir.iterdir()):
                if (
                    not path.is_file()
                    or path.suffix.lower() != ".json"
                    or path.resolve() in invalid_bridges
                ):
                    continue
                try:
                    data = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(data, dict):
                    bridges.append(data)

    return work_package_manifest.AuthoritativeState(
        descriptor=descriptor,
        closure_profiles=tuple(closure_profiles),
        promotion_receipts=tuple(promotion_receipts),
        boundary_contracts=tuple(boundaries),
        interactions=tuple(interactions),
        bridge_specs=tuple(bridges),
        base_commit=_git_head(workspace),
        toolchain=toolchain,
        target=target,
        features=tuple(sorted(set(features))),
        workspace_root=workspace,
    )


def _ledger_path(workspace: Path) -> Path:
    path = write_authorization.grant_ledger_path(workspace)
    resolved_root = workspace.resolve()
    try:
        path.resolve().relative_to(resolved_root)
        canonical_path = path.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise GenerationError(
            "unsafe-ledger-path", f"write-grant ledger escapes the workspace: {path}"
        ) from exc
    if path.is_symlink():
        raise GenerationError(
            "unsafe-ledger-path", f"write-grant ledger must not be a symlink: {path}"
        )
    if canonical_path != workspace / write_authorization.GRANT_LEDGER_REL:
        raise GenerationError(
            "unsafe-ledger-path",
            f"write-grant ledger path has symlinked components: {path}",
        )
    return path


def _read_ledger(
    fd: int, path: Path
) -> tuple[list[dict[str, Any]], write_authorization.GrantLedger]:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks).decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise GenerationError(
            "ledger-unreadable", f"cannot read protected-write ledger {path}: {exc}"
        ) from exc
    rows: list[dict[str, Any]] = []
    ledger = write_authorization.GrantLedger(path=path)
    for number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GenerationError(
                "ledger-invalid",
                f"protected-write ledger line {number} is invalid JSON: {exc}",
            ) from exc
        if not isinstance(row, dict):
            raise GenerationError(
                "ledger-invalid",
                f"protected-write ledger line {number} is not an object",
            )
        rows.append(row)
        if row.get("event") != "grant":
            ledger.ignored += 1
            continue
        grant, reason = write_authorization.parse_grant(row, number)
        if grant is None:
            ledger.rejected.append(
                write_authorization.RejectedGrant(
                    number, reason or "unusable grant record"
                )
            )
        else:
            ledger.entries.append(grant)
    return rows, ledger


def _validate_audit_row(row: dict[str, Any], line: int) -> None:
    if row.get("event") != AUDIT_EVENT:
        return
    schema = json.loads(
        resources.resource_path(
            "docs", "work-package-generation-audit-schema.json"
        ).read_text()
    )
    errors = sorted(
        make_validator(schema).iter_errors(row), key=lambda error: list(error.path)
    )
    if errors:
        details = "; ".join(error.message for error in errors[:4])
        raise GenerationError(
            "ledger-invalid",
            f"protected-write ledger line {line} has an invalid generation audit event: {details}",
        )
    expected_path = f"{MANIFEST_DIR}/{row['work_package']}.json"
    if row["path"] != expected_path:
        raise GenerationError(
            "ledger-invalid",
            f"protected-write ledger line {line} path does not match work_package",
        )


def _generation_history(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    history = []
    for line, row in enumerate(rows, start=1):
        _validate_audit_row(row, line)
        if row.get("event") == AUDIT_EVENT:
            history.append(row)
    return history


def _grant_for_generation(
    ledger: write_authorization.GrantLedger,
    descriptor: dict[str, Any],
    issue: int,
    target_rel: str,
    expected_grant_id: str | None,
    rows: list[dict[str, Any]],
) -> write_authorization.Grant:
    write_set = descriptor.get("write_set") or {}
    protected_roots = (
        [
            value
            for value in write_set.get("protected_roots", [])
            if isinstance(value, str)
        ]
        if isinstance(write_set, dict)
        else []
    )
    allowed_roots = (
        [
            value
            for value in write_set.get("allowed_roots", [])
            if isinstance(value, str)
        ]
        if isinstance(write_set, dict)
        else []
    )
    if write_authorization.protecting_pattern(allowed_roots, target_rel) is not None:
        raise GenerationError(
            "target-already-allowed",
            f"{target_rel} is already under allowed_roots; a protected-write grant cannot authorize an allowed path",
        )
    protected_by = write_authorization.protecting_pattern(protected_roots, target_rel)
    if protected_by is None:
        raise GenerationError(
            "target-not-protected",
            f"no declared protected_roots pattern covers {target_rel}; the generator only consumes grants for protected writes",
        )
    protected_by = write_authorization.ledger_protected_by(
        protected_roots, write_authorization.GRANT_LEDGER_REL
    )
    if protected_by:
        raise GenerationError(
            "ledger-protected",
            f"write-grant ledger is inside protected root {protected_by!r}; it cannot authorize or audit a write",
        )
    statuses = ledger.statuses(
        issue=issue,
        supervisors=write_authorization.supervisor_authorities(descriptor),
        ledger_protected=None,
    )
    duplicate_ids = {
        grant.grant_id
        for grant, status in statuses
        if status == write_authorization.STATUS_DUPLICATE and grant.covers(target_rel)
    }
    if duplicate_ids:
        raise GenerationError(
            "duplicate-grant",
            "duplicate grant record(s) cannot authorize generation: "
            + ", ".join(sorted(duplicate_ids)),
            ["one unique authorize-write record for the canonical target"],
        )
    active_exact = [
        grant
        for grant, status in statuses
        if status == write_authorization.STATUS_ACTIVE
        and grant.path_kind == write_authorization.PATH_KIND_EXACT
        and grant.path == target_rel
        and grant.op == write_authorization.OP_WRITE
    ]
    grant = active_exact[-1] if active_exact else None
    if grant is None:
        reasons = [
            f"{item.grant_id} ({status}, {item.op}, {item.path_kind})"
            for item, status in statuses
            if item.covers(target_rel)
        ]
        detail = "; nearby records: " + ", ".join(reasons) if reasons else ""
        raise GenerationError(
            "missing-or-invalid-grant",
            f"no active exact-path write grant for issue {issue} covers {target_rel}{detail}",
            [
                f"authorize-write --issue {issue} --path {target_rel} --op write --issuer <name>"
            ],
        )
    if expected_grant_id and grant.grant_id != expected_grant_id:
        raise GenerationError(
            "grant-id-mismatch",
            f"--grant-id {expected_grant_id!r} does not identify the active grant {grant.grant_id!r}",
        )
    prior = [
        entry
        for entry in _generation_history(rows)
        if entry["grant_id"] == grant.grant_id
    ]
    if any(entry["path"] != target_rel or entry["issue"] != issue for entry in prior):
        raise GenerationError(
            "grant-replayed",
            f"grant {grant.grant_id} already appears in a generation audit for another issue or path",
        )
    return grant


def _hash(raw: bytes | None) -> str | None:
    if raw is None:
        return None
    return HASH_PREFIX + hashlib.sha256(raw).hexdigest()


def _append_audit(fd: int, record: dict[str, Any]) -> None:
    encoded = (
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("ledger is not a regular file")
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("ledger append made no progress")
            view = view[written:]
        os.fsync(fd)
    except OSError as exc:
        raise GenerationError(
            "audit-write-failed", f"cannot append protected-write audit record: {exc}"
        ) from exc


def generate(
    *,
    workspace: Path,
    descriptor_path: Path,
    plan_path: Path,
    issue: int,
    toolchain: str,
    target: str,
    features: list[str],
    grant_id: str | None = None,
) -> GenerationResult:
    """Generate one validated, authorized canonical Stage 7 manifest."""
    workspace = workspace.resolve()
    if issue < 1:
        raise GenerationError(
            "invalid-issue", "--issue must be a positive integer", ["--issue N"]
        )
    if not toolchain.strip() or not target.strip():
        raise GenerationError(
            "missing-configuration",
            "toolchain and target must be non-empty",
            ["--toolchain VALUE", "--target TRIPLE"],
        )
    if len(features) != len(set(features)):
        raise GenerationError("duplicate-feature", "--feature values must be unique")

    request = _load_plan(plan_path)
    if request.issue != f"chainlink:{issue}":
        raise GenerationError(
            "issue-mismatch",
            f"plan issue {request.issue!r} does not match --issue {issue}",
            ["a plan whose issue equals --issue N"],
        )
    state = _load_authoritative_state(
        workspace,
        descriptor_path,
        toolchain=toolchain,
        target=target,
        features=features,
    )
    try:
        manifest = work_package_manifest.derive_manifest(request, state)
        rendered = work_package_manifest.render_manifest(manifest)
    except work_package_manifest.ManifestDerivationError as exc:
        raise GenerationError(
            exc.code,
            str(exc),
            [
                "valid canonical closure, promotion, interaction, boundary, and bridge inputs"
            ],
        ) from exc
    findings = work_package_manifest.validate_derived_manifest(
        manifest,
        workspace,
        specs_search_root=workspace,
        allowed_boundary_dirs=boundary_dirs_for_descriptor(state.descriptor, workspace),
    )
    errors = [finding for finding in findings if _is_error(finding)]
    if errors:
        raise GenerationError(
            "derived-manifest-invalid",
            _finding_text(errors),
            ["a derived manifest that passes validate-work-package"],
        )

    rel = f"{MANIFEST_DIR}/{request.work_package}.json"
    destination = workspace / rel
    try:
        destination.parent.resolve().relative_to(workspace)
    except ValueError as exc:
        raise GenerationError(
            "unsafe-target", f"canonical target escapes the workspace: {destination}"
        ) from exc
    if destination.parent.resolve() != workspace / MANIFEST_DIR:
        raise GenerationError(
            "unsafe-target",
            f"canonical target directory has symlinked components: {destination.parent}",
        )
    if destination.is_symlink():
        raise GenerationError(
            "unsafe-target", f"canonical target must not be a symlink: {destination}"
        )

    ledger_path = _ledger_path(workspace)
    try:
        ledger_fd = os.open(
            ledger_path, os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        )
    except FileNotFoundError as exc:
        raise GenerationError(
            "missing-or-invalid-grant",
            f"no protected-write ledger exists, so no grant can authorize {rel}",
            [
                f"authorize-write --issue {issue} --path {rel} --op write --issuer <name>"
            ],
        ) from exc
    except OSError as exc:
        raise GenerationError(
            "ledger-unreadable",
            f"cannot open protected-write ledger {ledger_path}: {exc}",
            ["an active write grant in ci/results/protected-writes.jsonl"],
        ) from exc
    created = False
    try:
        fcntl.flock(ledger_fd, fcntl.LOCK_EX)
        if not stat.S_ISREG(os.fstat(ledger_fd).st_mode):
            raise GenerationError(
                "unsafe-ledger-path",
                f"protected-write ledger is not a regular file: {ledger_path}",
            )
        ledger_stat = os.stat(ledger_path, follow_symlinks=False)
        fd_stat = os.fstat(ledger_fd)
        if (ledger_stat.st_dev, ledger_stat.st_ino) != (fd_stat.st_dev, fd_stat.st_ino):
            raise GenerationError(
                "unsafe-ledger-path",
                f"protected-write ledger changed while opening it: {ledger_path}",
            )
        if os.fstat(ledger_fd).st_nlink != 1:
            raise GenerationError(
                "unsafe-ledger-path",
                f"protected-write ledger has multiple hard links: {ledger_path}",
            )
        rows, ledger = _read_ledger(ledger_fd, ledger_path)
        grant = _grant_for_generation(
            ledger, state.descriptor, issue, rel, grant_id, rows
        )
        try:
            existing = destination.read_bytes()
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise GenerationError(
                "target-unreadable",
                f"cannot read canonical target {destination}: {exc}",
            ) from exc

        existing_hash = _hash(existing)
        after_hash = _hash(rendered)
        if existing is not None and existing != rendered:
            raise GenerationError(
                "target-conflict",
                f"{rel} already exists with different bytes; it was left untouched",
                ["a new work-package id or operator review of the existing manifest"],
            )
        history = _generation_history(rows)
        used = [entry for entry in history if entry["grant_id"] == grant.grant_id]
        if existing is None and used:
            raise GenerationError(
                "grant-replayed",
                f"grant {grant.grant_id} already authorized a prior write to {rel}; issue a new grant",
            )

        outcome = "created" if existing is None else "unchanged"
        record = {
            "event": AUDIT_EVENT,
            "schema_version": AUDIT_SCHEMA_VERSION,
            "issue": issue,
            "work_package": request.work_package,
            "grant_id": grant.grant_id,
            "path": rel,
            "before_hash": existing_hash,
            "after_hash": after_hash,
            "result": outcome,
            "command": "generate-work-package",
            "command_result": 0,
            "recorded_at": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        _validate_audit_row(record, 0)
        if existing is None:
            try:
                write_atomically(destination, rendered.decode("utf-8"))
            except OSError as exc:
                raise GenerationError(
                    "atomic-write-failed", f"could not atomically write {rel}: {exc}"
                ) from exc
            created = True
        try:
            written_bytes = destination.read_bytes()
        except OSError as exc:
            if created:
                try:
                    destination.unlink()
                except OSError:
                    pass
            raise GenerationError(
                "write-verification-failed",
                f"cannot verify generated file {rel}: {exc}",
            ) from exc
        if written_bytes != rendered:
            if created:
                try:
                    destination.unlink()
                except OSError:
                    pass
            raise GenerationError(
                "write-verification-failed",
                f"bytes at {rel} differ from the canonical renderer output; refusing to audit them",
            )
        record["after_hash"] = _hash(written_bytes)
        try:
            _append_audit(ledger_fd, record)
        except GenerationError:
            if created:
                try:
                    if (
                        destination.is_file()
                        and _hash(destination.read_bytes()) == after_hash
                    ):
                        destination.unlink()
                except OSError:
                    pass
            raise
        return GenerationResult(record=record, manifest=manifest, findings=findings)
    finally:
        try:
            fcntl.flock(ledger_fd, fcntl.LOCK_UN)
        finally:
            os.close(ledger_fd)


def error_document(error: GenerationError) -> dict[str, Any]:
    return {
        "event": AUDIT_EVENT,
        "schema_version": AUDIT_SCHEMA_VERSION,
        "state": "refused",
        "command_result": 2,
        "error": {
            "code": error.code,
            "message": str(error),
            "required_inputs": error.required_inputs,
        },
    }
