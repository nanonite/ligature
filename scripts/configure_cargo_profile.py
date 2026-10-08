#!/usr/bin/env python3
"""Configure typed Cargo profile fields without reserializing Cargo.toml.

The command changes only named scalar values under one root `[profile.NAME]`
table. It preserves all other TOML bytes and values. Git remains the source of
diffs, history, and rollback; this module's audit event records the grant, the
profile request, and the validators that passed.
"""
from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # Python 3.10 uses the backport declared in requirements.txt.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised by Python 3.10 CI.
    import tomli as tomllib  # type: ignore[no-redef]

sys.path.insert(0, str(Path(__file__).resolve().parent))

import atomic_write  # noqa: E402
import resources  # noqa: E402
import write_authorization  # noqa: E402
from project_descriptor import ProjectDescriptorError, load_project_descriptor  # noqa: E402
from schema_utils import make_validator  # noqa: E402

AUDIT_EVENT = "cargo-profile-configuration"
AUDIT_SCHEMA_VERSION = "1.0"
MANIFEST_REL = "Cargo.toml"
PROFILE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
TABLE_HEADER_RE = re.compile(r"^\s*\[\[?.+\]\]?\s*(?:#.*)?$")
PROFILE_HEADER_RE_TEMPLATE = r"^\s*\[\s*profile\s*\.\s*{}\s*\]\s*(?:#.*)?$"

PROFILE_KEY_ORDER = (
    "inherits",
    "opt-level",
    "debug",
    "split-debuginfo",
    "strip",
    "debug-assertions",
    "overflow-checks",
    "lto",
    "panic",
    "incremental",
    "codegen-units",
    "rpath",
)

BOOLEAN_KEYS = frozenset(
    {"debug-assertions", "overflow-checks", "incremental", "rpath"}
)
PROFILE_STRING_VALUES = {
    "split-debuginfo": frozenset({"off", "packed", "unpacked"}),
    "panic": frozenset({"unwind", "abort"}),
}
STRING_OR_BOOL_VALUES = {
    "lto": frozenset({"thin", "fat"}),
    "strip": frozenset({"none", "debuginfo", "symbols"}),
}


class ProfileConfigurationError(Exception):
    """A fail-closed refusal with a stable JSON error code."""

    def __init__(
        self, code: str, message: str, required_inputs: list[str] | None = None
    ):
        super().__init__(message)
        self.code = code
        self.required_inputs = required_inputs or []


def parse_debug(value: str) -> bool | int | str:
    """Parse Cargo's typed `debug` setting: bool or integer 0, 1, or 2."""
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        number = int(value)
    except ValueError:
        return value.lower()  # rejected by typed validation for a JSON refusal
    return number


def parse_opt_level(value: str) -> int | str:
    """Parse Cargo's integer and size-oriented optimization levels."""
    if value in {"s", "z"}:
        return value
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError("expected 0, 1, 2, 3, s, or z") from exc


def parse_bool_or_string_option(value: str) -> bool | str:
    """Parse `--lto` and `--strip` values while preserving Cargo's types."""
    lowered = value.lower()
    if lowered in {"off", "false"}:
        return False
    if lowered in {"on", "true"}:
        return True
    return lowered


def _validate_profile_name(profile: object) -> str:
    if not isinstance(profile, str) or not PROFILE_NAME_RE.fullmatch(profile):
        raise ProfileConfigurationError(
            "invalid-profile",
            f"profile {profile!r} is not a supported Cargo profile name",
            ["--profile dev|release|test|bench|<Cargo profile name>"],
        )
    return profile


def _validate_changes(changes: object, profile: str) -> dict[str, Any]:
    if not isinstance(changes, dict) or not changes:
        raise ProfileConfigurationError(
            "missing-profile-options",
            "select at least one typed profile setting to configure",
            ["one or more of --opt-level, --debug, --lto, --panic, --codegen-units, etc."],
        )
    unknown = sorted(set(changes) - set(PROFILE_KEY_ORDER))
    if unknown:
        raise ProfileConfigurationError(
            "unsupported-profile-setting",
            "unsupported profile setting(s): " + ", ".join(unknown),
            ["only the typed options listed in `ligature configure-cargo-profile --help`"],
        )
    normalized = dict(changes)
    for key, value in normalized.items():
        valid = False
        if key == "inherits":
            valid = (
                isinstance(value, str)
                and PROFILE_NAME_RE.fullmatch(value) is not None
                and value != profile
            )
        elif key == "opt-level":
            valid = (
                (isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 3)
                or (isinstance(value, str) and value in {"s", "z"})
            )
        elif key == "debug":
            valid = (
                isinstance(value, bool)
                or (isinstance(value, int) and not isinstance(value, bool) and value in {0, 1, 2})
            )
        elif key == "codegen-units":
            valid = (
                isinstance(value, int)
                and not isinstance(value, bool)
                and 1 <= value <= 256
            )
        elif key in BOOLEAN_KEYS:
            valid = isinstance(value, bool)
        elif key in PROFILE_STRING_VALUES:
            valid = isinstance(value, str) and value in PROFILE_STRING_VALUES[key]
        elif key in STRING_OR_BOOL_VALUES:
            valid = isinstance(value, bool) or (
                isinstance(value, str) and value in STRING_OR_BOOL_VALUES[key]
            )
        if not valid:
            raise ProfileConfigurationError(
                "invalid-profile-setting",
                f"{key}={value!r} has the wrong type or is outside Cargo's supported value domain",
                [f"a typed, supported value for `{key}`"],
            )
    return {key: normalized[key] for key in PROFILE_KEY_ORDER if key in normalized}


def _toml_loads(text: str, *, what: str) -> dict[str, Any]:
    try:
        data = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ProfileConfigurationError(
            "invalid-cargo-toml", f"{what} is not valid TOML: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ProfileConfigurationError("invalid-cargo-toml", f"{what} must be a TOML table")
    return data


def _line_context(lines: list[str]) -> tuple[list[int], list[bool]]:
    """Return TOML table-header lines and lines beginning at value depth 0.

    This small lexical pass distinguishes actual table headers from lines in
    multiline arrays and strings, so an unrelated multiline setting cannot
    make the targeted profile table boundary drift.
    """
    headers: list[int] = []
    neutral: list[bool] = []
    array_depth = 0
    inline_depth = 0
    multiline: str | None = None
    for line_number, line in enumerate(lines):
        at_value_depth_zero = multiline is None and array_depth == 0 and inline_depth == 0
        if at_value_depth_zero and TABLE_HEADER_RE.fullmatch(line.rstrip("\r\n")):
            headers.append(line_number)
            neutral.append(True)
            continue
        neutral.append(at_value_depth_zero)
        i = 0
        single_quote: str | None = None
        while i < len(line):
            if multiline is not None:
                end = line.find(multiline, i)
                if end < 0:
                    i = len(line)
                    continue
                multiline = None
                i = end + 3
                continue
            char = line[i]
            if single_quote is not None:
                if char == "\\" and single_quote == '"':
                    i += 2
                    continue
                if char == single_quote:
                    single_quote = None
                i += 1
                continue
            if char == "#":
                break
            if line.startswith('"""', i) or line.startswith("'''", i):
                multiline = line[i : i + 3]
                i += 3
                continue
            if char in {'"', "'"}:
                single_quote = char
            elif char == "[":
                array_depth += 1
            elif char == "]":
                array_depth = max(0, array_depth - 1)
            elif char == "{":
                inline_depth += 1
            elif char == "}":
                inline_depth = max(0, inline_depth - 1)
            i += 1
    return headers, neutral


def _find_unquoted_comment(value: str) -> int | None:
    quote: str | None = None
    multiline: str | None = None
    i = 0
    while i < len(value):
        if multiline is not None:
            end = value.find(multiline, i)
            if end < 0:
                return None
            multiline = None
            i = end + 3
            continue
        char = value[i]
        if quote is not None:
            if char == "\\" and quote == '"':
                i += 2
                continue
            if char == quote:
                quote = None
            i += 1
            continue
        if char == "#":
            return i
        if value.startswith('"""', i) or value.startswith("'''", i):
            multiline = value[i : i + 3]
            i += 3
            continue
        if char in {'"', "'"}:
            quote = char
        i += 1
    return None


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    raise ProfileConfigurationError(
        "invalid-profile-setting", f"cannot render unsupported TOML value {value!r}"
    )


def _profile_header_index(lines: list[str], headers: list[int], profile: str) -> int | None:
    wanted = re.compile(PROFILE_HEADER_RE_TEMPLATE.format(re.escape(profile)))
    matches = [
        index
        for index in headers
        if wanted.fullmatch(lines[index].rstrip("\r\n"))
    ]
    if len(matches) > 1:
        raise ProfileConfigurationError(
            "duplicate-profile-table",
            f"Cargo.toml declares `[profile.{profile}]` more than once",
        )
    return matches[0] if matches else None


def _key_assignment_pattern(key: str) -> re.Pattern[str]:
    escaped = re.escape(key)
    return re.compile(rf"^\s*(?:{escaped}|\"{escaped}\"|'{escaped}')\s*=")


def _replace_scalar_assignment(line: str, key: str, value: Any) -> str:
    equals = line.find("=")
    if equals < 0:
        raise ProfileConfigurationError("invalid-cargo-toml", f"cannot locate `{key}` assignment")
    newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
    body = line[: -len(newline)] if newline else line
    rhs = body[equals + 1 :]
    comment = _find_unquoted_comment(rhs)
    token = rhs if comment is None else rhs[:comment]
    if "\n" in token or "\r" in token or '"""' in token or "'''" in token:
        raise ProfileConfigurationError(
            "unsupported-profile-setting-source",
            f"existing `{key}` value spans multiple lines; it was left untouched",
            [f"rewrite only the `{key}` value as a single-line TOML scalar, then retry"],
        )
    trailing_start = len(token.rstrip(" \t"))
    leading = token[: len(token) - len(token.lstrip(" \t"))]
    trailing = token[trailing_start:]
    comment_text = rhs[comment:] if comment is not None else ""
    return line[: equals + 1] + leading + _toml_value(value) + trailing + comment_text + newline


def _merge_profile_text(
    original: str,
    original_data: dict[str, Any],
    profile: str,
    changes: dict[str, Any],
) -> str:
    lines = original.splitlines(keepends=True)
    headers, neutral = _line_context(lines)
    header_index = _profile_header_index(lines, headers, profile)
    profile_root = original_data.get("profile", {})
    if not isinstance(profile_root, dict):
        raise ProfileConfigurationError(
            "invalid-profile-table", "Cargo.toml's top-level `profile` value is not a table"
        )
    existing_profile = profile_root.get(profile)
    if existing_profile is not None and not isinstance(existing_profile, dict):
        raise ProfileConfigurationError(
            "unsupported-profile-table-form",
            f"profile {profile!r} is represented as an inline/dotted TOML value; no bytes were changed",
            [f"use a `[profile.{profile}]` table before configuring it"],
        )
    if existing_profile is not None and header_index is None:
        raise ProfileConfigurationError(
            "unsupported-profile-table-form",
            f"profile {profile!r} has no canonical `[profile.{profile}]` table; no bytes were changed",
            [f"convert it to a `[profile.{profile}]` table before configuring it"],
        )

    if header_index is None:
        eol = "\r\n" if "\r\n" in original else "\n"
        prefix = original
        if prefix and not prefix.endswith(("\n", "\r")):
            prefix += eol
        if prefix and not prefix.endswith(eol + eol):
            prefix += eol
        table = [f"[profile.{profile}]{eol}"]
        table.extend(f"{key} = {_toml_value(changes[key])}{eol}" for key in changes)
        return prefix + "".join(table)

    next_headers = [index for index in headers if index > header_index]
    end_index = next_headers[0] if next_headers else len(lines)
    assignment_indices: dict[str, int] = {}
    for key in changes:
        pattern = _key_assignment_pattern(key)
        matches = [
            index
            for index in range(header_index + 1, end_index)
            if neutral[index] and pattern.match(lines[index])
        ]
        if len(matches) > 1:
            raise ProfileConfigurationError(
                "duplicate-profile-setting",
                f"Cargo.toml declares `{key}` more than once in `[profile.{profile}]`",
            )
        if matches:
            index = matches[0]
            if index + 1 < len(neutral) and not neutral[index + 1]:
                raise ProfileConfigurationError(
                    "unsupported-profile-setting-source",
                    f"existing `{key}` value spans multiple lines; it was left untouched",
                    [f"rewrite only the `{key}` value as a single-line TOML scalar, then retry"],
                )
            assignment_indices[key] = index

    for key, index in assignment_indices.items():
        lines[index] = _replace_scalar_assignment(lines[index], key, changes[key])

    missing = [key for key in changes if key not in assignment_indices]
    if missing:
        eol = "\r\n" if "\r\n" in original else "\n"
        insertion = [f"{key} = {_toml_value(changes[key])}{eol}" for key in missing]
        insert_at = header_index + 1
        if not lines[header_index].endswith(("\n", "\r")):
            lines[header_index] += eol
        lines[insert_at:insert_at] = insertion
    return "".join(lines)


def _sha256_text(text: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validate_audit_row(row: dict[str, Any], line: int = 0) -> None:
    schema = json.loads(
        resources.resource_path("docs", "cargo-profile-audit-schema.json").read_text()
    )
    errors = sorted(make_validator(schema).iter_errors(row), key=lambda error: list(error.path))
    if errors:
        details = "; ".join(error.message for error in errors[:4])
        raise ProfileConfigurationError(
            "ledger-invalid",
            f"protected-write ledger line {line} has an invalid Cargo profile audit event: {details}",
        )


def _ledger_path(workspace: Path) -> Path:
    path = write_authorization.grant_ledger_path(workspace)
    try:
        canonical = path.resolve()
        canonical.relative_to(workspace)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProfileConfigurationError(
            "unsafe-ledger-path", f"protected-write ledger escapes the workspace: {path}"
        ) from exc
    if path.is_symlink() or canonical != workspace / write_authorization.GRANT_LEDGER_REL:
        raise ProfileConfigurationError(
            "unsafe-ledger-path", f"protected-write ledger has symlinked components: {path}"
        )
    return path


def _read_ledger(
    fd: int, path: Path
) -> tuple[list[dict[str, Any]], write_authorization.GrantLedger, list[dict[str, Any]]]:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        raw_bytes = b"".join(chunks)
        raw = raw_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProfileConfigurationError(
            "ledger-unreadable", f"cannot read protected-write ledger {path}: {exc}"
        ) from exc
    if raw_bytes and not raw_bytes.endswith(b"\n"):
        raise ProfileConfigurationError(
            "ledger-invalid", f"protected-write ledger {path} has an incomplete final line"
        )
    rows: list[dict[str, Any]] = []
    ledger = write_authorization.GrantLedger(path=path)
    profile_events: list[dict[str, Any]] = []
    for number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ProfileConfigurationError(
                "ledger-invalid", f"protected-write ledger line {number} is not valid JSON"
            ) from exc
        if not isinstance(row, dict):
            raise ProfileConfigurationError(
                "ledger-invalid", f"protected-write ledger line {number} is not a JSON object"
            )
        rows.append(row)
        if row.get("event") == "grant":
            grant, reason = write_authorization.parse_grant(row, number)
            if grant is None:
                raise ProfileConfigurationError(
                    "ledger-invalid",
                    f"protected-write ledger line {number} is not a usable grant: {reason}",
                )
            ledger.entries.append(grant)
        elif row.get("event") == AUDIT_EVENT:
            _validate_audit_row(row, number)
            profile_events.append(row)
    return rows, ledger, profile_events


def _authorize(
    ledger: write_authorization.GrantLedger,
    descriptor: dict[str, Any],
    issue: int,
    expected_grant_id: str | None,
    *,
    now: datetime | None,
) -> write_authorization.Grant:
    write_set = descriptor.get("write_set")
    write_set = write_set if isinstance(write_set, dict) else {}
    protected_roots = [value for value in write_set.get("protected_roots", []) if isinstance(value, str)]
    allowed_roots = [value for value in write_set.get("allowed_roots", []) if isinstance(value, str)]
    allowed = write_authorization.protecting_pattern(allowed_roots, MANIFEST_REL)
    protected = write_authorization.protecting_pattern(protected_roots, MANIFEST_REL)
    if allowed is not None:
        raise ProfileConfigurationError(
            "manifest-not-protected",
            f"{MANIFEST_REL} is already allowed by `{allowed}`; it does not need a protected-write grant",
        )
    if protected is None:
        raise ProfileConfigurationError(
            "manifest-not-protected",
            f"no protected_roots pattern covers {MANIFEST_REL}; authorize-write cannot grant this path",
            [f"authorize-write --issue {issue} --path {MANIFEST_REL} --op write --issuer <name>"],
        )
    ledger_protected = write_authorization.ledger_protected_by(
        protected_roots, write_authorization.GRANT_LEDGER_REL
    )
    if ledger_protected is not None:
        raise ProfileConfigurationError(
            "ledger-protected",
            f"protected-write ledger is under protected_roots pattern {ledger_protected!r}, so it cannot authorize writes",
        )
    statuses = ledger.statuses(
        issue=issue,
        supervisors=write_authorization.supervisor_authorities(descriptor),
        now=now,
    )
    matching = [grant for grant, _status in statuses if grant.covers(MANIFEST_REL)]
    if expected_grant_id is not None:
        matching = [grant for grant in matching if grant.grant_id == expected_grant_id]
    usable = [
        grant
        for grant, status in statuses
        if status == write_authorization.STATUS_ACTIVE
        and grant.issue == issue
        and grant.path == MANIFEST_REL
        and grant.path_kind == write_authorization.PATH_KIND_EXACT
        and grant.op == write_authorization.OP_WRITE
        and (expected_grant_id is None or grant.grant_id == expected_grant_id)
    ]
    if not usable:
        nearby = ", ".join(
            f"{grant.grant_id}:{status} ({grant.op} {grant.path})"
            for grant, status in statuses
            if grant.covers(MANIFEST_REL)
        )
        if expected_grant_id and not matching:
            detail = f"grant {expected_grant_id!r} does not exactly authorize {MANIFEST_REL} for issue {issue}"
        else:
            detail = f"no active exact write grant authorizes {MANIFEST_REL} for issue {issue}"
        if nearby:
            detail += f"; matching records: {nearby}"
        raise ProfileConfigurationError(
            "missing-or-invalid-grant",
            detail,
            [f"authorize-write --issue {issue} --path {MANIFEST_REL} --op write --issuer <name>"],
        )
    return usable[-1]


def _cargo_metadata(workspace: Path, manifest_path: Path) -> str:
    argv = [
        "cargo",
        "metadata",
        "--no-deps",
        "--format-version",
        "1",
        "--offline",
        "--manifest-path",
        str(manifest_path),
    ]
    try:
        result = subprocess.run(
            argv,
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise ProfileConfigurationError(
            "cargo-unavailable", "cargo is not available on PATH", ["install Cargo and retry"]
        ) from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProfileConfigurationError(
            "cargo-validation-failed", f"cargo metadata could not validate Cargo.toml: {exc}",
            ["a Cargo workspace that validates with `cargo metadata --no-deps --offline`"],
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ProfileConfigurationError(
            "cargo-validation-failed",
            "cargo metadata rejected the configured manifest"
            + (f": {detail[-1200:]!r}" if detail else ""),
            ["a Cargo manifest accepted by `cargo metadata --no-deps --offline`"],
        )
    return "cargo metadata --no-deps --format-version 1 --offline"


def configure_profile(
    workspace_root: Path,
    *,
    issue: int,
    profile: str,
    changes: dict[str, Any],
    descriptor_path: Path | None = None,
    grant_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Merge selected profile settings into the workspace-root Cargo.toml."""
    if isinstance(issue, bool) or not isinstance(issue, int) or issue < 1:
        raise ProfileConfigurationError("invalid-issue", "--issue must be a positive integer")
    profile = _validate_profile_name(profile)
    changes = _validate_changes(changes, profile)
    workspace = workspace_root.resolve()
    if not workspace.is_dir():
        raise ProfileConfigurationError("invalid-workspace", f"workspace is not a directory: {workspace}")
    if descriptor_path is None:
        descriptor_path = workspace / "project-descriptor.json"
    try:
        descriptor = load_project_descriptor(descriptor_path)
    except (OSError, ProjectDescriptorError) as exc:
        raise ProfileConfigurationError("invalid-descriptor", f"cannot load project descriptor: {exc}") from exc

    manifest_path = workspace / MANIFEST_REL
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ProfileConfigurationError(
            "invalid-cargo-manifest-path",
            f"workspace root must contain a regular, non-symlink {MANIFEST_REL}",
            ["a workspace-root Cargo.toml"],
        )
    if manifest_path.resolve() != workspace / MANIFEST_REL:
        raise ProfileConfigurationError(
            "unsafe-cargo-manifest-path", f"{MANIFEST_REL} resolves outside the workspace"
        )

    ledger_path = _ledger_path(workspace)
    try:
        ledger_fd = os.open(
            ledger_path, os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        )
    except FileNotFoundError as exc:
        raise ProfileConfigurationError(
            "missing-or-invalid-grant",
            f"no protected-write ledger exists, so no grant can authorize {MANIFEST_REL}",
            [f"authorize-write --issue {issue} --path {MANIFEST_REL} --op write --issuer <name>"],
        ) from exc
    except OSError as exc:
        raise ProfileConfigurationError(
            "ledger-unreadable", f"cannot open protected-write ledger {ledger_path}: {exc}"
        ) from exc

    original_text: str | None = None
    rendered_text: str | None = None
    updated_file = False
    original_ledger_size = 0
    try:
        fcntl.flock(ledger_fd, fcntl.LOCK_EX)
        ledger_stat = os.stat(ledger_path, follow_symlinks=False)
        fd_stat = os.fstat(ledger_fd)
        if not stat.S_ISREG(fd_stat.st_mode) or (ledger_stat.st_dev, ledger_stat.st_ino) != (
            fd_stat.st_dev,
            fd_stat.st_ino,
        ) or fd_stat.st_nlink != 1:
            raise ProfileConfigurationError("unsafe-ledger-path", "protected-write ledger is not a stable regular file")
        original_ledger_size = fd_stat.st_size
        _rows, ledger, history = _read_ledger(ledger_fd, ledger_path)
        grant = _authorize(ledger, descriptor, issue, grant_id, now=now)
        if grant.one_shot and any(
            event["grant_id"] == grant.grant_id and event["result"] == "updated"
            for event in history
        ):
            raise ProfileConfigurationError(
                "grant-replayed",
                f"one-shot grant {grant.grant_id} already authorized a Cargo.toml update; issue a new grant",
            )

        try:
            source_bytes = manifest_path.read_bytes()
            original_text = source_bytes.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ProfileConfigurationError(
                "cargo-manifest-unreadable", f"cannot read {manifest_path}: {exc}"
            ) from exc
        original_data = _toml_loads(original_text, what=MANIFEST_REL)
        expected_data = copy.deepcopy(original_data)
        expected_profile_root = expected_data.setdefault("profile", {})
        if not isinstance(expected_profile_root, dict):
            raise ProfileConfigurationError(
                "invalid-profile-table", "Cargo.toml's top-level `profile` value is not a table"
            )
        expected_profile = expected_profile_root.setdefault(profile, {})
        if not isinstance(expected_profile, dict):
            raise ProfileConfigurationError(
                "unsupported-profile-table-form",
                f"profile {profile!r} is not a standard table; no bytes were changed",
            )
        expected_profile.update(changes)
        rendered_text = _merge_profile_text(original_text, original_data, profile, changes)
        candidate_data = _toml_loads(rendered_text, what="configured Cargo.toml")
        if candidate_data != expected_data:
            raise ProfileConfigurationError(
                "unrelated-settings-changed",
                "the profile edit would change TOML values outside the requested settings; no bytes were written",
            )
        outcome = "updated" if rendered_text != original_text else "unchanged"

        # A cooperating concurrent run serializes on the ledger lock. This
        # byte check also catches ordinary edits between the read and replace.
        if manifest_path.read_bytes() != source_bytes:
            raise ProfileConfigurationError(
                "cargo-manifest-changed",
                "Cargo.toml changed while profile settings were being prepared; no write was made",
            )
        if outcome == "updated":
            atomic_write.write_atomically(manifest_path, rendered_text)
            updated_file = True

        try:
            cargo_validator = _cargo_metadata(workspace, manifest_path)
        except ProfileConfigurationError:
            if updated_file and manifest_path.read_bytes() == rendered_text.encode("utf-8"):
                atomic_write.write_atomically(manifest_path, original_text)
                updated_file = False
            raise

        final_text = manifest_path.read_bytes().decode("utf-8")
        final_data = _toml_loads(final_text, what="Cargo.toml after validation")
        if final_data != expected_data:
            if updated_file and final_text == rendered_text:
                atomic_write.write_atomically(manifest_path, original_text)
                updated_file = False
            raise ProfileConfigurationError(
                "cargo-manifest-changed",
                "Cargo.toml changed during Cargo validation; no audit event was written",
            )

        record = {
            "event": AUDIT_EVENT,
            "schema_version": AUDIT_SCHEMA_VERSION,
            "issue": issue,
            "grant_id": grant.grant_id,
            "issuer": grant.issuer,
            "issuer_kind": grant.issuer_kind,
            "path": MANIFEST_REL,
            "profile": profile,
            "changes": changes,
            "result": outcome,
            "validation": {
                "toml_parse": "passed",
                "requested_profile_values": "passed",
                "unrelated_settings_preserved": "passed",
                "cargo_metadata": "passed",
                "cargo_validator": cargo_validator,
            },
            "recorded_at": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        _validate_audit_row(record)
        encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        try:
            written = os.write(ledger_fd, encoded)
            if written != len(encoded):
                os.ftruncate(ledger_fd, original_ledger_size)
                raise OSError(f"short audit write ({written} of {len(encoded)} bytes)")
            os.fsync(ledger_fd)
        except OSError as exc:
            try:
                os.ftruncate(ledger_fd, original_ledger_size)
                os.fsync(ledger_fd)
            except OSError:
                pass
            if updated_file and manifest_path.read_bytes() == rendered_text.encode("utf-8"):
                atomic_write.write_atomically(manifest_path, original_text)
                updated_file = False
            raise ProfileConfigurationError(
                "audit-write-failed",
                f"could not record Cargo profile authorization and validation in {ledger_path}: {exc}",
            ) from exc
        return record
    except OSError as exc:
        if updated_file and original_text is not None and rendered_text is not None:
            try:
                if manifest_path.read_bytes() == rendered_text.encode("utf-8"):
                    atomic_write.write_atomically(manifest_path, original_text)
            except OSError:
                pass
        raise ProfileConfigurationError(
            "filesystem-error", f"Cargo profile configuration could not safely access a required path: {exc}"
        ) from exc
    finally:
        try:
            fcntl.flock(ledger_fd, fcntl.LOCK_UN)
        finally:
            os.close(ledger_fd)


def error_document(error: ProfileConfigurationError) -> dict[str, Any]:
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


def render_text(record: dict[str, Any]) -> str:
    changed = ", ".join(f"{key}={value!r}" for key, value in record["changes"].items())
    return "\n".join(
        (
            f"cargo profile: {record['profile']}",
            f"result: {record['result']}",
            f"settings: {changed}",
            f"grant: {record['grant_id']} (issue {record['issue']}, issuer {record['issuer']})",
            "validation: TOML merge and Cargo metadata passed",
            "Git retains the file diff, history, and rollback; no Git history is copied into the Ligature audit.",
        )
    )
