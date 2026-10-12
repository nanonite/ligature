#!/usr/bin/env python3
"""Configure the declared Cargo-Verus profile of an existing crate.

The profile owns three Cargo fields only: the exact crates.io `vstd` version,
`package.metadata.verus.verify`, and the `cfg(verus_only)` check-cfg entry.
Everything else in the crate manifest is preserved byte-for-byte. Version
updates are an explicit transition from the last successful Ligature profile
event, never a guess from an arbitrary Cargo requirement.
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import fcntl
import json
import os
import re
import shutil
import shlex
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
import configure_cargo_profile as toml_text  # noqa: E402
import ligature_install  # noqa: E402
import resources  # noqa: E402
import scaffold_crate  # noqa: E402
import write_authorization  # noqa: E402
from project_descriptor import ProjectDescriptorError, load_project_descriptor  # noqa: E402
from schema_utils import make_validator  # noqa: E402

AUDIT_EVENT = "cargo-verus-profile-configuration"
AUDIT_SCHEMA_VERSION = "1.0"
PROFILE = "cargo-verus"
PROFILE_VERSION = "1.0"
VERUS_ONLY_CFG = "cfg(verus_only)"
_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){2,3}(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
_TABLE_HEADER_RE = re.compile(r"^\s*\[\s*([^\]]+?)\s*\]\s*(?:#.*)?$")
_ASSIGNMENT_RE = re.compile(r"^\s*(?:([A-Za-z0-9_-]+)|\"([^\"]+)\"|'([^']+)')\s*=")


class CrateProfileError(Exception):
    """A fail-closed refusal with a stable JSON error code."""

    def __init__(self, code: str, message: str, required_inputs: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.required_inputs = required_inputs or []


def _toml_load(text: str, what: str) -> dict[str, Any]:
    try:
        value = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise CrateProfileError("invalid-cargo-toml", f"{what} is not valid TOML: {exc}") from exc
    if not isinstance(value, dict):
        raise CrateProfileError("invalid-cargo-toml", f"{what} must be a TOML table")
    return value


def _table_path(header: str) -> tuple[str, ...] | None:
    # The profile writes only simple, literal Cargo table names. Refuse quoted
    # or otherwise unusual table syntax rather than guessing its meaning.
    pieces = tuple(piece.strip() for piece in header.split("."))
    if not pieces or any(not re.fullmatch(r"[A-Za-z0-9_-]+", piece) for piece in pieces):
        return None
    return pieces


def _headers(lines: list[str]) -> tuple[list[int], list[tuple[str, ...] | None], list[bool]]:
    indexes, paths = [], []
    header_lines, neutral = toml_text._line_context(lines)
    for index in header_lines:
        match = _TABLE_HEADER_RE.fullmatch(lines[index].rstrip("\r\n"))
        if match:
            indexes.append(index)
            paths.append(_table_path(match.group(1)))
    return indexes, paths, neutral


def _table_span(text: str, path: tuple[str, ...]) -> tuple[list[str], int, int] | None:
    lines = text.splitlines(keepends=True)
    indexes, paths, _neutral = _headers(lines)
    matches = [idx for idx, found in zip(indexes, paths) if found == path]
    if len(matches) > 1:
        raise CrateProfileError("duplicate-cargo-table", f"Cargo.toml declares '[{'.'.join(path)}]' more than once")
    if not matches:
        return None
    start = matches[0]
    end = next((idx for idx in indexes if idx > start), len(lines))
    return lines, start, end


def _eol(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _append_table(text: str, path: tuple[str, ...], assignments: list[str]) -> str:
    newline = _eol(text)
    prefix = text
    if prefix and not prefix.endswith(("\n", "\r")):
        prefix += newline
    if prefix and not prefix.endswith(newline + newline):
        prefix += newline
    return prefix + "[" + ".".join(path) + "]" + newline + "".join(
        line + newline for line in assignments
    )


def _assignment_indexes(lines: list[str], start: int, end: int, key: str) -> list[int]:
    pattern = toml_text._key_assignment_pattern(key)
    _headers_found, neutral = toml_text._line_context(lines)
    return [index for index in range(start + 1, end) if neutral[index] and pattern.match(lines[index])]


def _add_to_table(text: str, path: tuple[str, ...], assignments: list[str]) -> str:
    if not assignments:
        return text
    span = _table_span(text, path)
    if span is None:
        return _append_table(text, path, assignments)
    lines, start, end = span
    insertion = end
    while insertion > start + 1 and not lines[insertion - 1].strip():
        insertion -= 1
    newline = _eol(text)
    if not lines[start].endswith(("\n", "\r")):
        lines[start] += newline
    lines[insertion:insertion] = [line + newline for line in assignments]
    return "".join(lines)


def _set_scalar(text: str, path: tuple[str, ...], key: str, value: Any) -> str:
    span = _table_span(text, path)
    if span is None:
        return _add_to_table(text, path, [f"{key} = {toml_text._toml_value(value)}"])
    lines, start, end = span
    matches = _assignment_indexes(lines, start, end, key)
    if len(matches) > 1:
        raise CrateProfileError("duplicate-cargo-setting", f"Cargo.toml declares `{key}` more than once in '[{'.'.join(path)}]'")
    if not matches:
        return _add_to_table(text, path, [f"{key} = {toml_text._toml_value(value)}"])
    lines[matches[0]] = toml_text._replace_scalar_assignment(lines[matches[0]], key, value)
    return "".join(lines)


def _top_level_assignments(text: str, key: str) -> list[int]:
    lines = text.splitlines(keepends=True)
    indexes, _paths, neutral = _headers(lines)
    next_header = indexes[0] if indexes else len(lines)
    return [i for i in range(next_header) if neutral[i] and toml_text._key_assignment_pattern(key).match(lines[i])]


def _split_inline_members(value: str) -> list[tuple[int, int]]:
    """Ranges of top-level inline-table members, excluding braces and commas."""
    if not value.lstrip().startswith("{") or not value.rstrip().endswith("}"):
        raise CrateProfileError("unsupported-vstd-dependency", "vstd dependency must use a TOML string, a one-line inline table, or a [dependencies.vstd] table")
    start = value.index("{") + 1
    end = value.rfind("}")
    ranges: list[tuple[int, int]] = []
    member_start = start
    quote: str | None = None
    escaped = False
    square = curly = 0
    for i in range(start, end):
        ch = value[i]
        if quote is not None:
            if quote == '"' and ch == "\\" and not escaped:
                escaped = True
                continue
            if ch == quote and not escaped:
                quote = None
            escaped = False
            continue
        if ch in ('"', "'"):
            quote = ch
        elif ch == "[":
            square += 1
        elif ch == "]":
            square -= 1
        elif ch == "{":
            curly += 1
        elif ch == "}":
            curly -= 1
        elif ch == "," and square == 0 and curly == 0:
            if value[member_start:i].strip():
                ranges.append((member_start, i))
            member_start = i + 1
    if value[member_start:end].strip():
        ranges.append((member_start, end))
    return ranges


def _inline_member(value: str, name: str) -> tuple[int, int] | None:
    for start, end in _split_inline_members(value):
        member = value[start:end]
        match = _ASSIGNMENT_RE.match(member)
        found = (match.group(1) or match.group(2) or match.group(3)) if match else None
        if found == name:
            equals = member.find("=")
            left = start + equals + 1
            while left < end and value[left].isspace():
                left += 1
            right = end
            while right > left and value[right - 1].isspace():
                right -= 1
            return left, right
    return None


def _replace_inline_member(value: str, name: str, replacement: str) -> str:
    found = _inline_member(value, name)
    if found is not None:
        start, end = found
        return value[:start] + replacement + value[end:]
    end = value.rfind("}")
    members = _split_inline_members(value)
    if members:
        member_end = members[-1][1]
        trailing_comma = value.find(",", member_end, end)
        if trailing_comma >= 0:
            return value[:trailing_comma + 1] + " " + name + " = " + replacement + value[trailing_comma + 1:]
        return value[:member_end] + ", " + name + " = " + replacement + value[member_end:]
    return value[:end] + " " + name + " = " + replacement + " " + value[end:]


def _replace_assignment_rhs(line: str, key: str, replacement: str) -> str:
    match = _ASSIGNMENT_RE.match(line)
    found = (match.group(1) or match.group(2) or match.group(3)) if match else None
    if found != key:
        raise CrateProfileError("unsupported-cargo-setting", f"cannot safely locate `{key}` assignment")
    equals = line.find("=")
    newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
    body = line[:-len(newline)] if newline else line
    rhs = body[equals + 1:]
    comment = toml_text._find_unquoted_comment(rhs)
    suffix = rhs[comment:] if comment is not None else ""
    rhs_value = rhs[:comment] if comment is not None else rhs
    lead = rhs_value[:len(rhs_value) - len(rhs_value.lstrip())]
    trail = rhs_value[len(rhs_value.rstrip()):]
    return body[:equals + 1] + lead + replacement + trail + suffix + newline


def _dependency_data(document: dict[str, Any]) -> dict[str, Any]:
    dependencies = document.get("dependencies", {})
    if not isinstance(dependencies, dict):
        raise CrateProfileError("unsupported-dependencies-table", "Cargo.toml `[dependencies]` is not a table")
    return dependencies


def _target_specific_vstd(document: dict[str, Any]) -> bool:
    targets = document.get("target", {})
    if not isinstance(targets, dict):
        return False
    for target in targets.values():
        if not isinstance(target, dict):
            continue
        for kind in ("dependencies", "dev-dependencies", "build-dependencies"):
            section = target.get(kind, {})
            if isinstance(section, dict) and "vstd" in section:
                return True
    return False


def _vstd_setting(document: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    dependencies = _dependency_data(document)
    value = dependencies.get("vstd")
    if value is None:
        return None, None
    if isinstance(value, str):
        return value, None
    if not isinstance(value, dict):
        raise CrateProfileError("unsupported-vstd-dependency", "the existing `vstd` dependency is not a supported version or inline dependency table")
    if value.get("workspace") is True:
        raise CrateProfileError("workspace-vstd-dependency", "`vstd` is inherited from `[workspace.dependencies]`; configure the workspace dependency through its own explicitly authorized path")
    unsupported = sorted(key for key in ("git", "path", "registry", "branch", "tag", "rev") if key in value)
    if unsupported:
        raise CrateProfileError("conflicting-vstd-source", "the existing `vstd` dependency uses unsupported source field(s): " + ", ".join(unsupported))
    if value.get("package", "vstd") != "vstd":
        raise CrateProfileError("conflicting-vstd-package", "the `vstd` dependency key is renamed to a different package")
    version = value.get("version")
    if version is not None and not isinstance(version, str):
        raise CrateProfileError("unsupported-vstd-version", "the existing `vstd.version` is not a string")
    return version, value


def _replace_vstd_version(text: str, current: str | None, desired: str) -> str:
    """Set only `dependencies.vstd.version`, preserving all other dependency data."""
    document = _toml_load(text, "Cargo.toml")
    dependencies = _dependency_data(document)
    value = dependencies.get("vstd")
    if value is None:
        return _add_to_table(text, ("dependencies",), [f'vstd = {{ version = "{desired}" }}'])

    if isinstance(value, str):
        span = _table_span(text, ("dependencies",))
        if span is None:
            raise CrateProfileError("unsupported-vstd-dependency", "dotted or inline `dependencies.vstd` syntax cannot be updated safely")
        lines, start, end = span
        matches = _assignment_indexes(lines, start, end, "vstd")
        if len(matches) != 1:
            raise CrateProfileError("unsupported-vstd-dependency", "cannot locate the existing `vstd` dependency assignment")
        lines[matches[0]] = toml_text._replace_scalar_assignment(lines[matches[0]], "vstd", desired)
        return "".join(lines)

    table_span = _table_span(text, ("dependencies", "vstd"))
    if table_span is not None:
        lines, start, end = table_span
        matches = _assignment_indexes(lines, start, end, "version")
        if len(matches) > 1:
            raise CrateProfileError("duplicate-vstd-version", "Cargo.toml declares `vstd.version` more than once")
        if matches:
            lines[matches[0]] = toml_text._replace_scalar_assignment(lines[matches[0]], "version", desired)
            return "".join(lines)
        return _add_to_table(text, ("dependencies", "vstd"), [f'version = "{desired}"'])

    span = _table_span(text, ("dependencies",))
    if span is None:
        raise CrateProfileError("unsupported-vstd-dependency", "dotted or inline `dependencies.vstd` syntax cannot be updated safely")
    lines, start, end = span
    matches = _assignment_indexes(lines, start, end, "vstd")
    if len(matches) != 1:
        raise CrateProfileError("unsupported-vstd-dependency", "cannot locate the existing `vstd` dependency assignment")
    line = lines[matches[0]]
    equal = line.find("=")
    rhs = line[equal + 1:].strip()
    comment = toml_text._find_unquoted_comment(rhs)
    rhs = rhs[:comment].rstrip() if comment is not None else rhs
    if "\n" in rhs or "\r" in rhs or '"""' in rhs or "'''" in rhs:
        raise CrateProfileError("unsupported-vstd-dependency", "a multiline `vstd` dependency table cannot be updated safely")
    loc = _inline_member(rhs, "version")
    if loc is None:
        replacement = _replace_inline_member(rhs, "version", json.dumps(desired))
    else:
        replacement = _replace_inline_member(rhs, "version", json.dumps(desired))
    lines[matches[0]] = _replace_assignment_rhs(line, "vstd", replacement)
    return "".join(lines)


def _get_path(document: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = document
    for part in path:
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _set_nested(document: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = document
    for part in path[:-1]:
        child = node.setdefault(part, {})
        if not isinstance(child, dict):
            raise CrateProfileError("unsupported-cargo-table", f"Cargo.toml `{'.'.join(path[:-1])}` is not a table")
        node = child
    node[path[-1]] = value


def _add_cfg_entry(text: str) -> str:
    document = _toml_load(text, "Cargo.toml")
    unexpected = _get_path(document, ("lints", "rust", "unexpected_cfgs"))
    if unexpected is None:
        return _add_to_table(
            text,
            ("lints", "rust"),
            ["unexpected_cfgs = { level = \"warn\", check-cfg = ['cfg(verus_only)'] }"],
        )
    if isinstance(unexpected, str):
        return _replace_cfg_lint_scalar(text, unexpected)
    if not isinstance(unexpected, dict):
        raise CrateProfileError("unsupported-cfg-lint", "`lints.rust.unexpected_cfgs` must be a Cargo lint level or table")
    checks = unexpected.get("check-cfg")
    if checks is not None and not isinstance(checks, list):
        raise CrateProfileError("unsupported-cfg-lint", "`unexpected_cfgs.check-cfg` must be an array")
    if checks is not None and any(not isinstance(item, str) for item in checks):
        raise CrateProfileError("unsupported-cfg-lint", "`unexpected_cfgs.check-cfg` contains a non-string value")
    if VERUS_ONLY_CFG in (checks or []):
        return text

    lint_table = _table_span(text, ("lints", "rust"))
    if lint_table is None:
        # A dotted key can create this TOML value without a literal header. Do
        # not risk adding a second declaration over a source form we cannot diff.
        raise CrateProfileError("unsupported-cfg-lint", "`lints.rust` is declared with non-table TOML syntax")
    lines, start, end = lint_table
    unexpected_lines = _assignment_indexes(lines, start, end, "unexpected_cfgs")
    if len(unexpected_lines) > 1:
        raise CrateProfileError("duplicate-cfg-lint", "Cargo.toml declares `unexpected_cfgs` more than once")
    if unexpected_lines:
        line_index = unexpected_lines[0]
        line = lines[line_index]
        if line_index + 1 < end and not toml_text._line_context(lines)[1][line_index + 1]:
            raise CrateProfileError("unsupported-cfg-lint", "multiline `unexpected_cfgs` settings cannot be extended safely")
        equal = line.find("=")
        rhs = line[equal + 1:].strip()
        comment = toml_text._find_unquoted_comment(rhs)
        rhs_value = rhs[:comment].rstrip() if comment is not None else rhs
        if not rhs_value.startswith("{"):
            raise CrateProfileError("unsupported-cfg-lint", "the existing `unexpected_cfgs` source is not an inline Cargo lint table")
        replacement = _replace_inline_member(rhs_value, "check-cfg", "['cfg(verus_only)']")
        if checks:
            array = _inline_member(rhs_value, "check-cfg")
            if array is None:
                raise CrateProfileError("unsupported-cfg-lint", "cannot locate existing `check-cfg` array")
            start_value, end_value = array
            old_array = rhs_value[start_value:end_value]
            if not old_array.lstrip().startswith("[") or not old_array.rstrip().endswith("]") or "\n" in old_array or "\r" in old_array:
                raise CrateProfileError("unsupported-cfg-lint", "multiline `check-cfg` arrays cannot be extended safely")
            body = old_array.rstrip()[:-1]
            trimmed = body.rstrip()
            if trimmed and not trimmed.endswith("["):
                if not trimmed.endswith(","):
                    trimmed += ","
                trimmed += " "
            body = trimmed
            new_array = body + "'cfg(verus_only)']"
            replacement = rhs_value[:start_value] + new_array + rhs_value[end_value:]
        lines[line_index] = _replace_assignment_rhs(line, "unexpected_cfgs", replacement)
        return "".join(lines)

    nested_table = _table_span(text, ("lints", "rust", "unexpected_cfgs"))
    if nested_table is not None:
        return _add_to_table(text, ("lints", "rust", "unexpected_cfgs"), ["check-cfg = ['cfg(verus_only)']"])
    raise CrateProfileError("unsupported-cfg-lint", "cannot locate the declared `unexpected_cfgs` setting")


def _replace_cfg_lint_scalar(text: str, level: str) -> str:
    if level not in {"allow", "warn", "deny", "forbid"}:
        raise CrateProfileError("unsupported-cfg-lint", f"unsupported `unexpected_cfgs` level {level!r}")
    lint_table = _table_span(text, ("lints", "rust"))
    if lint_table is None:
        raise CrateProfileError("unsupported-cfg-lint", "cannot locate the existing `unexpected_cfgs` lint source")
    lines, start, end = lint_table
    matches = _assignment_indexes(lines, start, end, "unexpected_cfgs")
    if len(matches) != 1:
        raise CrateProfileError("duplicate-cfg-lint", "Cargo.toml declares `unexpected_cfgs` more than once")
    lines[matches[0]] = _replace_assignment_rhs(
        lines[matches[0]], "unexpected_cfgs",
        "{ level = " + json.dumps(level) + ", check-cfg = ['cfg(verus_only)'] }",
    )
    return "".join(lines)


def _ensure_cfg_lint(text: str, document: dict[str, Any], workspace_data: dict[str, Any]) -> str:
    package_lints = document.get("lints", {})
    if not isinstance(package_lints, dict):
        raise CrateProfileError("unsupported-cargo-table", "Cargo.toml `[lints]` is not a table")
    if package_lints.get("workspace") is True:
        workspace_cfgs = _get_path(workspace_data, ("workspace", "lints", "rust", "unexpected_cfgs", "check-cfg"))
        if isinstance(workspace_cfgs, list) and VERUS_ONLY_CFG in workspace_cfgs:
            return text
        raise CrateProfileError(
            "workspace-cfg-lint-missing",
            "this crate inherits workspace lints, but the workspace root does not declare cfg(verus_only); configure that root through its separately authorized manifest operation",
        )
    return _add_cfg_entry(text)


def _candidate_manifest(
    original: str,
    original_data: dict[str, Any],
    workspace_data: dict[str, Any],
    desired_version: str,
    prior_event: dict[str, Any] | None,
) -> tuple[str, dict[str, Any], list[dict[str, Any]], str]:
    if _target_specific_vstd(original_data):
        raise CrateProfileError("unsupported-target-vstd", "target-specific `vstd` dependencies are outside the cargo-verus profile write set")
    current_version, dep = _vstd_setting(original_data)
    exact_current = "=" + desired_version
    previous_version = prior_event.get("vstd_version") if prior_event else None
    previous_pin = "=" + previous_version if isinstance(previous_version, str) else None

    if current_version is not None:
        if current_version == exact_current:
            transition = "unchanged"
        elif prior_event and current_version == previous_pin:
            transition = "verus-toolchain-repin"
        else:
            raise CrateProfileError(
                "conflicting-vstd-version",
                f"existing `vstd.version` is {current_version!r}; the active Verus toolchain requires {exact_current!r}, and no prior cargo-verus profile event authorizes that transition",
                ["restore the last profile-pinned version or request a separately reviewed toolchain profile transition"],
            )
        if not current_version.startswith("="):
            raise CrateProfileError("unpinned-vstd-version", "`vstd.version` must be an exact Cargo pin beginning with `=`")
    else:
        if prior_event:
            raise CrateProfileError("profile-drift", "the prior cargo-verus profile event exists but the `vstd` dependency or version was removed")
        transition = "initial"

    if dep is not None:
        for source in ("git", "path", "registry", "branch", "tag", "rev", "workspace"):
            if source in dep:
                raise CrateProfileError("conflicting-vstd-source", f"the `vstd` dependency's `{source}` field is user-owned and unsupported by this profile")

    metadata_value = _get_path(original_data, ("package", "metadata", "verus", "verify"))
    if metadata_value is False:
        raise CrateProfileError("conflicting-verus-opt-in", "`package.metadata.verus.verify` is explicitly false")
    if metadata_value not in (None, True):
        raise CrateProfileError("unsupported-verus-metadata", "`package.metadata.verus.verify` must be a boolean")
    if prior_event and metadata_value is not True:
        raise CrateProfileError("profile-drift", "the prior cargo-verus profile event exists but package.metadata.verus.verify is no longer true")
    package_cfgs = _get_path(original_data, ("lints", "rust", "unexpected_cfgs", "check-cfg"))
    inherits_lints = _get_path(original_data, ("lints", "workspace")) is True
    workspace_cfgs = _get_path(workspace_data, ("workspace", "lints", "rust", "unexpected_cfgs", "check-cfg"))
    cfg_is_present = (
        isinstance(package_cfgs, list) and VERUS_ONLY_CFG in package_cfgs
    ) or (
        inherits_lints and isinstance(workspace_cfgs, list) and VERUS_ONLY_CFG in workspace_cfgs
    )
    if prior_event and not cfg_is_present:
        raise CrateProfileError("profile-drift", "the prior cargo-verus profile event exists but cfg(verus_only) is no longer declared")

    candidate = original
    fields: list[dict[str, Any]] = []
    if current_version != exact_current:
        candidate = _replace_vstd_version(candidate, current_version, exact_current)
        fields.append({"path": "dependencies.vstd.version", "before": current_version, "after": exact_current})

    if metadata_value is None:
        package = original_data.get("package")
        if not isinstance(package, dict):
            raise CrateProfileError("missing-package", "the existing crate manifest has no `[package]` table")
        metadata = package.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise CrateProfileError("unsupported-package-metadata", "`package.metadata` must be a TOML table to add Verus opt-in")
        verus = metadata.get("verus") if isinstance(metadata, dict) else None
        if verus is not None and not isinstance(verus, dict):
            raise CrateProfileError("unsupported-verus-metadata", "`package.metadata.verus` must be a TOML table")
        if isinstance(verus, dict):
            candidate = _add_to_table(candidate, ("package", "metadata", "verus"), ["verify = true"])
        else:
            candidate = _append_table(candidate, ("package", "metadata", "verus"), ["verify = true"])
        fields.append({"path": "package.metadata.verus.verify", "before": None, "after": True})

    before_data = _toml_load(candidate, "profile candidate")
    candidate = _ensure_cfg_lint(candidate, before_data, workspace_data)
    after_data = _toml_load(candidate, "profile candidate")
    if _get_path(before_data, ("lints", "rust", "unexpected_cfgs", "check-cfg")) != _get_path(after_data, ("lints", "rust", "unexpected_cfgs", "check-cfg")):
        old = _get_path(before_data, ("lints", "rust", "unexpected_cfgs", "check-cfg"))
        new = _get_path(after_data, ("lints", "rust", "unexpected_cfgs", "check-cfg"))
        fields.append({"path": "lints.rust.unexpected_cfgs.check-cfg", "before": old, "after": new})

    expected_data = copy.deepcopy(original_data)
    deps = expected_data.setdefault("dependencies", {})
    if not isinstance(deps, dict):
        raise CrateProfileError("unsupported-dependencies-table", "Cargo.toml `[dependencies]` is not a table")
    existing = deps.get("vstd")
    if existing is None:
        deps["vstd"] = {"version": exact_current}
    elif isinstance(existing, str):
        deps["vstd"] = exact_current
    else:
        updated = dict(existing)
        updated["version"] = exact_current
        deps["vstd"] = updated
    if metadata_value is None:
        _set_nested(expected_data, ("package", "metadata", "verus", "verify"), True)
    if not (_get_path(original_data, ("lints", "workspace")) is True and _get_path(workspace_data, ("workspace", "lints", "rust", "unexpected_cfgs", "check-cfg")) and VERUS_ONLY_CFG in _get_path(workspace_data, ("workspace", "lints", "rust", "unexpected_cfgs", "check-cfg"))):
        cfgs = _get_path(expected_data, ("lints", "rust", "unexpected_cfgs", "check-cfg"))
        if cfgs is None:
            current_lint = _get_path(expected_data, ("lints", "rust", "unexpected_cfgs"))
            if isinstance(current_lint, str):
                current_level = current_lint
                _set_nested(expected_data, ("lints", "rust", "unexpected_cfgs"), {"level": current_level, "check-cfg": [VERUS_ONLY_CFG]})
            elif current_lint is None:
                _set_nested(expected_data, ("lints", "rust", "unexpected_cfgs"), {"level": "warn", "check-cfg": [VERUS_ONLY_CFG]})
            elif isinstance(current_lint, dict):
                current_lint["check-cfg"] = [VERUS_ONLY_CFG]
        elif isinstance(cfgs, list) and VERUS_ONLY_CFG not in cfgs:
            cfgs.append(VERUS_ONLY_CFG)
    candidate_data = _toml_load(candidate, "profile candidate")
    if candidate_data != expected_data:
        raise CrateProfileError("unrelated-settings-changed", "the Cargo-Verus profile edit would change TOML values outside its declared fields; no bytes were written")
    if prior_event and not fields:
        transition = "unchanged"
    elif not prior_event:
        transition = "initial"
    return candidate, expected_data, fields, transition


def _wrapped_verus_executable(verus_binary: Path) -> Path | None:
    """Recover a literal Verus target from a simple shell wrapper.

    Nix exposes Verus through a small shell wrapper whose final command is
    ``exec /nix/store/.../verus \"$@\"``. The wrapped executable is in the
    distribution directory that also contains vstd. Parse only this literal,
    non-expanding form; never evaluate wrapper shell text.
    """
    try:
        if verus_binary.stat().st_size > 1024 * 1024:
            return None
        raw = verus_binary.read_bytes()
    except OSError:
        return None
    if not raw.startswith(b"#!"):
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None

    for line in text.splitlines():
        try:
            words = shlex.split(line.strip(), comments=False, posix=True)
        except ValueError:
            continue
        if len(words) != 3 or words[0] != "exec" or words[2] != "$@":
            continue
        candidate = Path(words[1])
        if candidate.name != "verus" or not candidate.is_absolute():
            continue
        try:
            candidate = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if candidate != verus_binary and candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _read_toolchain(verus_binary: Path, workspace: Path) -> tuple[str, str, str]:
    try:
        result = subprocess.run(
            [str(verus_binary), "--version", "--output-json"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CrateProfileError("verus-version-unavailable", f"could not query the selected Verus binary: {exc}") from exc
    if result.returncode != 0:
        raise CrateProfileError("verus-version-unavailable", f"Verus version query failed: {(result.stderr or result.stdout).strip()[-600:]}")
    version = toolchain = ""
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError:
        document = None
    if isinstance(document, dict):
        lowered = {str(key).lower().replace("_", ""): value for key, value in document.items()}
        version = lowered.get("version", "")
        toolchain = lowered.get("toolchain", "")
        nested_verus = lowered.get("verus")
        if isinstance(nested_verus, dict):
            nested = {str(key).lower().replace("_", ""): value for key, value in nested_verus.items()}
            if not version:
                version = nested.get("version", "")
            if not toolchain:
                toolchain = nested.get("toolchain", "")
    if not version:
        for line in result.stdout.splitlines():
            key, sep, value = line.partition(":")
            if sep and key.strip().lower() == "version":
                version = value.strip()
            if sep and key.strip().lower() in {"toolchain", "rust toolchain"}:
                toolchain = value.strip()
    if not isinstance(version, str) or not version or not isinstance(toolchain, str) or not toolchain:
        raise CrateProfileError("verus-version-unavailable", "`verus --version --output-json` did not report both Verus version and Rust toolchain")

    try:
        rustc = subprocess.run(
            ["rustc", "--version", "--verbose"], cwd=workspace,
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CrateProfileError("rustc-version-unavailable", f"could not query the workspace Rust toolchain: {exc}") from exc
    if rustc.returncode != 0:
        raise CrateProfileError("rustc-version-unavailable", f"rustc version query failed: {(rustc.stderr or rustc.stdout).strip()[-600:]}")
    fields = dict(line.split(":", 1) for line in rustc.stdout.splitlines() if ":" in line)
    rust_release = fields.get("release", "").strip()
    rust_host = fields.get("host", "").strip()
    expected_match = re.fullmatch(r"(\d+\.\d+\.\d+)(?:-([A-Za-z0-9_.-]+))?", toolchain)
    if not expected_match or not rust_release:
        raise CrateProfileError("toolchain-version-unavailable", f"cannot compare workspace rustc against Verus toolchain {toolchain!r}")
    expected_release, expected_host = expected_match.groups()
    if rust_release != expected_release or (expected_host and rust_host and rust_host != expected_host):
        actual = rust_release + ("-" + rust_host if rust_host else "")
        raise CrateProfileError("verifier-version-mismatch", f"workspace rustc is {actual!r}, but Verus {version!r} requires {toolchain!r}")

    binaries = [verus_binary]
    wrapped = _wrapped_verus_executable(verus_binary)
    if wrapped is not None:
        binaries.append(wrapped)
    roots = tuple(
        candidate
        for binary in binaries
        for candidate in (
            binary.parent / "vstd" / "Cargo.toml",
            binary.parent / "source" / "vstd" / "Cargo.toml",
            binary.parent.parent / "vstd" / "Cargo.toml",
            binary.parent.parent / "source" / "vstd" / "Cargo.toml",
        )
    )
    vstd_manifest = next((path for path in roots if path.is_file()), None)
    if vstd_manifest is None:
        raise CrateProfileError(
            "vstd-version-unavailable",
            "cannot derive the exact vstd version from the Verus distribution beside the selected executable or its literal shell-wrapper target",
        )
    try:
        vstd_doc = _toml_load(vstd_manifest.read_text(encoding="utf-8"), str(vstd_manifest))
    except OSError as exc:
        raise CrateProfileError("vstd-version-unavailable", f"cannot read the Verus distribution's vstd manifest: {exc}") from exc
    vstd_version = _get_path(vstd_doc, ("package", "version"))
    if not isinstance(vstd_version, str) or not _VERSION_RE.fullmatch(vstd_version):
        raise CrateProfileError("vstd-version-unavailable", "the Verus distribution's vstd Cargo.toml has no exact package version")
    return version, toolchain, vstd_version


def _verus_binary(descriptor: dict[str, Any], workspace: Path) -> Path:
    backend = descriptor.get("verifier_backends", {})
    verus = backend.get("verus", {}) if isinstance(backend, dict) else {}
    command = verus.get("command") if isinstance(verus, dict) else None
    named_verus = False
    if isinstance(command, str):
        try:
            words = shlex.split(command)
        except ValueError:
            words = []
        for word in words:
            candidate = Path(word)
            if candidate.name != "verus":
                continue
            named_verus = True
            if not candidate.is_absolute():
                candidate = workspace / candidate
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate.resolve()
        if named_verus:
            raise CrateProfileError("verus-unavailable", "the descriptor names a Verus executable that is not executable from the workspace")
    found = shutil.which("verus")
    if not found:
        raise CrateProfileError("verus-unavailable", "the Verus binary is not available on PATH and no declared verifier command names a Verus executable")
    return Path(found).resolve()


def _load_audit_schema() -> dict[str, Any]:
    return json.loads(resources.resource_path("docs", "cargo-verus-profile-audit-schema.json").read_text(encoding="utf-8"))


def _validate_audit_row(row: dict[str, Any], line: int = 0) -> None:
    errors = sorted(make_validator(_load_audit_schema()).iter_errors(row), key=lambda error: list(error.path))
    if errors:
        details = "; ".join(error.message for error in errors[:4])
        raise CrateProfileError("ledger-invalid", f"protected-write ledger line {line} has an invalid cargo-verus profile event: {details}")


def _ledger_path(workspace: Path) -> Path:
    path = write_authorization.grant_ledger_path(workspace)
    try:
        canonical = path.resolve()
        canonical.relative_to(workspace)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CrateProfileError("unsafe-ledger-path", f"protected-write ledger escapes the workspace: {path}") from exc
    if path.is_symlink() or canonical != workspace / write_authorization.GRANT_LEDGER_REL:
        raise CrateProfileError("unsafe-ledger-path", f"protected-write ledger has symlinked components: {path}")
    return path


def _read_ledger(fd: int, path: Path) -> tuple[write_authorization.GrantLedger, list[dict[str, Any]]]:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        chunks = []
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
        raw_bytes = b"".join(chunks)
        raw = raw_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CrateProfileError("ledger-unreadable", f"cannot read protected-write ledger {path}: {exc}") from exc
    if raw_bytes and not raw_bytes.endswith(b"\n"):
        raise CrateProfileError("ledger-invalid", f"protected-write ledger {path} has an incomplete final line")
    ledger = write_authorization.GrantLedger(path=path)
    profile_events: list[dict[str, Any]] = []
    for number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CrateProfileError("ledger-invalid", f"protected-write ledger line {number} is not valid JSON") from exc
        if not isinstance(row, dict):
            raise CrateProfileError("ledger-invalid", f"protected-write ledger line {number} is not a JSON object")
        if row.get("event") == "grant":
            grant, reason = write_authorization.parse_grant(row, number)
            if grant is None:
                raise CrateProfileError("ledger-invalid", f"protected-write ledger line {number} is not a usable grant: {reason}")
            ledger.entries.append(grant)
        elif row.get("event") == AUDIT_EVENT:
            _validate_audit_row(row, number)
            profile_events.append(row)
        elif row.get("event") == toml_text.AUDIT_EVENT:
            toml_text._validate_audit_row(row, number)
    return ledger, profile_events


def _authorize(
    ledger: write_authorization.GrantLedger,
    descriptor: dict[str, Any],
    manifest_rel: str,
    issue: int,
    grant_id: str | None,
) -> write_authorization.Grant:
    write_set_decl = descriptor.get("write_set", {})
    write_set_decl = write_set_decl if isinstance(write_set_decl, dict) else {}
    allowed_roots = [item for item in write_set_decl.get("allowed_roots", []) if isinstance(item, str)]
    protected_roots = [item for item in write_set_decl.get("protected_roots", []) if isinstance(item, str)]
    allowed = write_authorization.protecting_pattern(allowed_roots, manifest_rel)
    protected = write_authorization.protecting_pattern(protected_roots, manifest_rel)
    if allowed:
        raise CrateProfileError("manifest-not-protected", f"{manifest_rel} is already allowed by `{allowed}` and does not need a protected-write grant")
    if not protected:
        raise CrateProfileError("manifest-not-protected", f"no protected_roots pattern covers {manifest_rel}", [f"authorize-write --issue {issue} --path {manifest_rel} --op write --issuer <name>"])
    ledger_protected = write_authorization.ledger_protected_by(protected_roots, write_authorization.GRANT_LEDGER_REL)
    if ledger_protected:
        raise CrateProfileError("ledger-protected", f"protected-write ledger is under protected_roots pattern {ledger_protected!r}, so it cannot authorize writes")
    statuses = ledger.statuses(issue=issue, supervisors=write_authorization.supervisor_authorities(descriptor))
    matching = [grant for grant, _status in statuses if grant.covers(manifest_rel)]
    if grant_id is not None:
        matching = [grant for grant in matching if grant.grant_id == grant_id]
    usable = [
        grant for grant, status in statuses
        if status == write_authorization.STATUS_ACTIVE
        and grant.issue == issue
        and grant.path == manifest_rel
        and grant.path_kind == write_authorization.PATH_KIND_EXACT
        and grant.op == write_authorization.OP_WRITE
        and (grant_id is None or grant.grant_id == grant_id)
    ]
    if usable:
        return usable[-1]
    nearby = ", ".join(f"{grant.grant_id}:{status} ({grant.op} {grant.path})" for grant, status in statuses if grant.covers(manifest_rel))
    detail = f"no active exact write grant authorizes {manifest_rel} for issue {issue}"
    if grant_id and not matching:
        detail = f"grant {grant_id!r} does not exactly authorize {manifest_rel} for issue {issue}"
    if nearby:
        detail += f"; matching records: {nearby}"
    raise CrateProfileError("missing-or-invalid-grant", detail, [f"authorize-write --issue {issue} --path {manifest_rel} --op write --issuer <name>"])


def _run(argv: list[str], cwd: Path, *, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise CrateProfileError("validator-unavailable", f"required command is not available: {argv[0]}") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CrateProfileError("validation-failed", f"command could not complete: {' '.join(argv)}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise CrateProfileError("validation-failed", f"`{' '.join(argv)}` failed with exit {result.returncode}" + (f": {detail[-900:]}" if detail else ""))
    return result


def _metadata(workspace: Path, manifest: Path) -> dict[str, Any]:
    result = _run(["cargo", "metadata", "--no-deps", "--format-version", "1", "--manifest-path", str(manifest)], workspace, timeout=60)
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CrateProfileError("cargo-metadata-invalid", "cargo metadata returned invalid JSON") from exc
    if not isinstance(document, dict) or not isinstance(document.get("packages"), list) or not isinstance(document.get("workspace_root"), str):
        raise CrateProfileError("cargo-metadata-invalid", "cargo metadata omitted package or workspace-root data")
    target_path = manifest.resolve()
    packages = [item for item in document["packages"] if isinstance(item, dict) and Path(str(item.get("manifest_path", ""))).resolve() == target_path]
    if len(packages) != 1:
        raise CrateProfileError("crate-not-in-workspace", f"Cargo metadata did not identify exactly one package for {manifest}")
    cargo_root = Path(document["workspace_root"]).resolve()
    project_root = workspace.resolve()
    try:
        cargo_root.relative_to(project_root)
    except ValueError as exc:
        raise CrateProfileError(
            "cargo-root-outside-project",
            f"Cargo metadata resolved workspace root {cargo_root} outside the Ligature project {project_root}",
        ) from exc
    try:
        target_path.relative_to(cargo_root)
    except ValueError as exc:
        raise CrateProfileError(
            "cargo-root-not-ancestor",
            f"Cargo metadata root {cargo_root} is not an ancestor of the declared crate manifest {target_path}",
        ) from exc
    return {"document": document, "package": packages[0], "workspace_root": cargo_root}


def _prior_event(events: list[dict[str, Any]], path: str, crate: str) -> dict[str, Any] | None:
    matches = [row for row in events if row.get("path") == path and row.get("crate") == crate and row.get("profile") == PROFILE]
    return matches[-1] if matches else None


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _crate_manifest_snapshot(workspace: Path, descriptor: dict[str, Any]) -> dict[str, bytes]:
    """Snapshot the workspace and declared crate manifests so Cargo validation
    cannot silently rewrite build configuration outside the selected profile.
    """
    snapshots: dict[str, bytes] = {}
    try:
        declared = scaffold_crate.declared_crates(descriptor)
    except scaffold_crate.ScaffoldError as exc:
        raise CrateProfileError("invalid-crate-declarations", str(exc)) from exc
    manifest_rels = {"Cargo.toml", *(f"{crate_dir}/Cargo.toml" for crate_dir, _entry in declared)}
    for rel in sorted(manifest_rels):
        path = ligature_install.safe_target(workspace, rel)
        if path.is_symlink():
            raise CrateProfileError("unsafe-manifest-path", f"declared crate manifest is a symlink: {rel}")
        if path.exists() and not path.is_file():
            raise CrateProfileError("unsafe-manifest-path", f"declared crate manifest is not a regular file: {rel}")
        if path.is_file():
            snapshots[rel] = path.read_bytes()
    return snapshots


def _assert_crate_manifests_preserved(
    workspace: Path,
    descriptor: dict[str, Any],
    snapshots: dict[str, bytes],
    target_rel: str,
    expected_target: bytes,
) -> None:
    current = _crate_manifest_snapshot(workspace, descriptor)
    expected = dict(snapshots)
    expected[target_rel] = expected_target
    if current.keys() != expected.keys():
        changed = sorted(set(current) ^ set(expected))
        raise CrateProfileError("crate-manifest-set-changed", "Cargo validation changed the set of declared crate manifests: " + ", ".join(changed))
    changed = [rel for rel, raw in expected.items() if current.get(rel) != raw]
    if changed:
        raise CrateProfileError("crate-manifest-changed", "Cargo validation changed crate manifest(s) outside the requested profile: " + ", ".join(sorted(changed)))


def _restore_lock(lock_path: Path, original: bytes | None) -> None:
    if lock_path.is_symlink():
        raise CrateProfileError("unsafe-cargo-lock", f"Cargo.lock became a symlink during validation: {lock_path}")
    if original is None:
        if lock_path.exists():
            if not lock_path.is_file():
                raise CrateProfileError("unsafe-cargo-lock", f"Cargo.lock is not a regular file: {lock_path}")
            lock_path.unlink()
    else:
        try:
            text = original.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CrateProfileError("unsafe-cargo-lock", "Cargo.lock is not UTF-8 and cannot be safely restored") from exc
        atomic_write.write_atomically(lock_path, text)


def _snapshot_cargo_locks(workspace: Path, manifest_path: Path) -> dict[Path, bytes | None]:
    """Snapshot lockfiles at every possible Cargo root for this declared crate.

    Cargo's root may be the Ligature project root or any nested ancestor of the
    crate. Capture those candidates before metadata runs because Cargo may
    refresh a lockfile while resolving the selected manifest.
    """
    project_root = workspace.resolve()
    crate_root = manifest_path.resolve().parent
    try:
        crate_root.relative_to(project_root)
    except ValueError as exc:
        raise CrateProfileError(
            "unsafe-cargo-lock",
            f"declared crate directory {crate_root} is outside the Ligature project {project_root}",
        ) from exc

    snapshots: dict[Path, bytes | None] = {}
    candidate_root = crate_root
    while True:
        if (candidate_root / "Cargo.toml").is_file():
            lock_path = candidate_root / "Cargo.lock"
            if lock_path.is_symlink():
                raise CrateProfileError("unsafe-cargo-lock", f"Cargo.lock is a symlink: {lock_path}")
            if lock_path.exists():
                if not lock_path.is_file():
                    raise CrateProfileError("unsafe-cargo-lock", f"Cargo.lock is not a regular file: {lock_path}")
                original = lock_path.read_bytes()
                try:
                    original.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise CrateProfileError("unsafe-cargo-lock", f"Cargo.lock is not UTF-8 and cannot be safely restored: {lock_path}") from exc
            else:
                original = None
            snapshots[lock_path] = original
        if candidate_root == project_root:
            break
        candidate_root = candidate_root.parent
    return snapshots


def _restore_changed_cargo_locks(snapshots: dict[Path, bytes | None]) -> None:
    """Undo only lockfile changes made before Cargo's root was established."""
    for lock_path, original in snapshots.items():
        if lock_path.is_symlink():
            raise CrateProfileError("unsafe-cargo-lock", f"Cargo.lock became a symlink: {lock_path}")
        if lock_path.exists():
            if not lock_path.is_file():
                raise CrateProfileError("unsafe-cargo-lock", f"Cargo.lock is not a regular file: {lock_path}")
            current = lock_path.read_bytes()
        else:
            current = None
        if current != original:
            _restore_lock(lock_path, original)


def configure_crate_profile(
    workspace_root: Path,
    *,
    issue: int,
    crate: str,
    profile: str,
    descriptor_path: Path | None = None,
    grant_id: str | None = None,
) -> tuple[dict[str, Any], str]:
    if isinstance(issue, bool) or not isinstance(issue, int) or issue < 1:
        raise CrateProfileError("invalid-issue", "--issue must be a positive integer")
    if profile != PROFILE:
        raise CrateProfileError("unsupported-profile", f"unsupported crate profile {profile!r}; supported profiles: {PROFILE}")
    workspace = workspace_root.resolve()
    if not workspace.is_dir():
        raise CrateProfileError("invalid-workspace", f"workspace is not a directory: {workspace}")
    if descriptor_path is None:
        descriptor_path = workspace / "project-descriptor.json"
    try:
        descriptor = load_project_descriptor(descriptor_path)
    except (OSError, ProjectDescriptorError) as exc:
        raise CrateProfileError("invalid-descriptor", f"cannot load project descriptor: {exc}") from exc

    try:
        crate_dir, _entry = scaffold_crate.resolve_crate(descriptor, crate)
        target = scaffold_crate.classify_target(workspace, descriptor, crate_dir)
    except scaffold_crate.ScaffoldError as exc:
        raise CrateProfileError("invalid-crate", str(exc)) from exc
    manifest_path = ligature_install.safe_target(workspace, target.manifest_rel)
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise CrateProfileError("existing-manifest-required", f"configure-crate requires the existing declared crate manifest {target.manifest_rel}; it never creates a crate")
    if manifest_path.resolve() != workspace / target.manifest_rel:
        raise CrateProfileError("unsafe-manifest-path", f"{target.manifest_rel} resolves outside the workspace")
    try:
        source_bytes = manifest_path.read_bytes()
        original = source_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CrateProfileError("cargo-manifest-unreadable", f"cannot read {manifest_path}: {exc}") from exc
    original_data = _toml_load(original, target.manifest_rel)
    package = original_data.get("package")
    crate_name = package.get("name") if isinstance(package, dict) else None
    if crate_name != target.package:
        raise CrateProfileError("declared-package-mismatch", f"{target.manifest_rel} declares package {crate_name!r}, but descriptor crate_dir requires {target.package!r}")

    ledger_path = _ledger_path(workspace)
    try:
        ledger_fd = os.open(ledger_path, os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError as exc:
        raise CrateProfileError("missing-or-invalid-grant", f"no protected-write ledger exists, so no grant can authorize {target.manifest_rel}", [f"authorize-write --issue {issue} --path {target.manifest_rel} --op write --issuer <name>"]) from exc
    except OSError as exc:
        raise CrateProfileError("ledger-unreadable", f"cannot open protected-write ledger {ledger_path}: {exc}") from exc

    original_text: str | None = None
    rendered_text: str | None = None
    updated_file = False
    original_ledger_size = 0
    lock_path: Path | None = None
    lock_before: bytes | None = None
    lock_snapshots: dict[Path, bytes | None] = {}
    try:
        fcntl.flock(ledger_fd, fcntl.LOCK_EX)
        ledger_stat = os.stat(ledger_path, follow_symlinks=False)
        fd_stat = os.fstat(ledger_fd)
        if not stat.S_ISREG(fd_stat.st_mode) or (ledger_stat.st_dev, ledger_stat.st_ino) != (fd_stat.st_dev, fd_stat.st_ino) or fd_stat.st_nlink != 1:
            raise CrateProfileError("unsafe-ledger-path", "protected-write ledger is not a stable regular file")
        original_ledger_size = fd_stat.st_size
        ledger, events = _read_ledger(ledger_fd, ledger_path)
        grant = _authorize(ledger, descriptor, target.manifest_rel, issue, grant_id)
        crate_manifests_before = _crate_manifest_snapshot(workspace, descriptor)
        lock_snapshots = _snapshot_cargo_locks(workspace, manifest_path)
        metadata = _metadata(workspace, manifest_path)
        cargo_root = metadata["workspace_root"]
        # Cargo keeps Cargo.lock at its own workspace root. For a standalone
        # declared crate this is the crate directory, not Ligature's project
        # root, which does not need a Cargo.toml.
        lock_path = cargo_root / "Cargo.lock"
        lock_before = lock_snapshots[lock_path]

        workspace_cargo = cargo_root / "Cargo.toml"
        try:
            workspace_data = _toml_load(workspace_cargo.read_text(encoding="utf-8"), str(workspace_cargo))
        except OSError as exc:
            raise CrateProfileError("workspace-manifest-unreadable", f"cannot read Cargo workspace root {workspace_cargo}: {exc}") from exc
        verus = _verus_binary(descriptor, workspace)
        verus_version, toolchain, vstd_version = _read_toolchain(verus, workspace)
        prior = _prior_event(events, target.manifest_rel, target.package)
        rendered_text, expected_data, fields, transition = _candidate_manifest(original, original_data, workspace_data, vstd_version, prior)
        candidate_data = _toml_load(rendered_text, "configured Cargo.toml")
        if candidate_data != expected_data:
            raise CrateProfileError("unrelated-settings-changed", "the profile editor changed TOML data outside the declared fields")
        if manifest_path.read_bytes() != source_bytes:
            raise CrateProfileError("cargo-manifest-changed", "Cargo.toml changed while profile settings were being prepared; no write was made")

        diff = "".join(difflib.unified_diff(
            original.splitlines(keepends=True), rendered_text.splitlines(keepends=True),
            fromfile=target.manifest_rel, tofile=target.manifest_rel,
        ))
        outcome = "updated" if rendered_text != original else "unchanged"
        crate_events = [row for row in events if row.get("path") == target.manifest_rel and row.get("crate") == target.package]
        if outcome == "updated" and grant.one_shot and any(row.get("grant_id") == grant.grant_id and row.get("result") == "updated" for row in crate_events):
            raise CrateProfileError("grant-replayed", f"one-shot grant {grant.grant_id} already authorized a changed profile update; issue a new grant")
        before_hash = _sha256(source_bytes)

        if outcome == "updated":
            atomic_write.write_atomically(manifest_path, rendered_text)
            updated_file = True

        validation: dict[str, str] = {}
        try:
            post_metadata = _metadata(workspace, manifest_path)
            if post_metadata["workspace_root"] != cargo_root:
                raise CrateProfileError("cargo-root-changed", "Cargo workspace root changed during validation")
            verus_metadata = post_metadata["package"].get("metadata", {})
            if not isinstance(verus_metadata, dict) or verus_metadata.get("verus", {}).get("verify") is not True:
                raise CrateProfileError("cargo-metadata-profile-missing", "cargo metadata did not expose package.metadata.verus.verify=true")
            validation["cargo_metadata"] = "cargo metadata --no-deps --format-version 1 passed"
            common = ["--manifest-path", str(manifest_path), "--package", target.package]
            _run(["cargo", "check", *common], workspace)
            validation["cargo_check"] = "cargo check passed"
            _run(["cargo", "verus", "verify", *common], workspace)
            validation["verus_verify"] = "cargo verus verify passed"
            _run(["cargo", "verus", "build", *common], workspace)
            validation["verus_build"] = "cargo verus build passed"
            _run(["cargo", "test", *common], workspace)
            validation["cargo_test"] = "cargo test passed"
        except CrateProfileError as validation_error:
            _restore_lock(lock_path, lock_before)
            if updated_file and manifest_path.read_bytes() == rendered_text.encode("utf-8"):
                atomic_write.write_atomically(manifest_path, original)
                updated_file = False
            try:
                _assert_crate_manifests_preserved(
                    workspace,
                    descriptor,
                    crate_manifests_before,
                    target.manifest_rel,
                    source_bytes,
                )
            except CrateProfileError as preservation_error:
                raise preservation_error from validation_error
            raise

        _restore_lock(lock_path, lock_before)
        if lock_before is None:
            lock_preserved = not lock_path.exists()
        else:
            lock_preserved = lock_path.is_file() and not lock_path.is_symlink() and lock_path.read_bytes() == lock_before
        if not lock_preserved:
            raise CrateProfileError("cargo-lock-changed", "Cargo.lock could not be restored to its original bytes")
        lock_after = lock_path.read_bytes() if lock_path.is_file() and not lock_path.is_symlink() else None
        _assert_crate_manifests_preserved(
            workspace,
            descriptor,
            crate_manifests_before,
            target.manifest_rel,
            rendered_text.encode("utf-8"),
        )
        if manifest_path.read_bytes() != rendered_text.encode("utf-8"):
            raise CrateProfileError("cargo-manifest-changed", "Cargo.toml changed during validation; no audit event was written")
        validation["crate_manifests_preserved"] = "passed"
        validation["cargo_lock_preserved"] = "passed"
        cargo_lock_hashes = {
            "before_hash": _sha256(lock_before) if lock_before is not None else None,
            "after_hash": _sha256(lock_after) if lock_after is not None else None,
            "result": "preserved",
        }
        crate_manifest_hashes = [
            {
                "path": rel,
                "before_hash": _sha256(raw),
                "after_hash": _sha256(rendered_text.encode("utf-8") if rel == target.manifest_rel else raw),
                "result": "updated" if rel == target.manifest_rel and outcome == "updated" else "unchanged",
            }
            for rel, raw in sorted(crate_manifests_before.items())
        ]

        record = {
            "event": AUDIT_EVENT,
            "schema_version": AUDIT_SCHEMA_VERSION,
            "profile": PROFILE,
            "profile_version": PROFILE_VERSION,
            "transition": transition,
            "issue": issue,
            "grant_id": grant.grant_id,
            "issuer": grant.issuer,
            "issuer_kind": grant.issuer_kind,
            "crate": target.package,
            "crate_dir": crate_dir,
            "path": target.manifest_rel,
            "verus_version": verus_version,
            "rust_toolchain": toolchain,
            "vstd_version": vstd_version,
            "before_hash": before_hash,
            "after_hash": _sha256(rendered_text.encode("utf-8")),
            "crate_manifest_hashes": crate_manifest_hashes,
            "cargo_lock_hashes": cargo_lock_hashes,
            "changed_fields": fields,
            "result": outcome,
            "validation": validation,
            "recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
        _validate_audit_row(record)
        encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
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
                atomic_write.write_atomically(manifest_path, original)
                updated_file = False
            raise CrateProfileError("audit-write-failed", f"could not record cargo-verus profile authorization and validation in {ledger_path}: {exc}") from exc
        return record, diff
    except CrateProfileError:
        if lock_path is not None:
            try:
                _restore_lock(lock_path, lock_before)
            except CrateProfileError:
                pass
        elif lock_snapshots:
            try:
                _restore_changed_cargo_locks(lock_snapshots)
            except CrateProfileError:
                pass
        if updated_file and original is not None and rendered_text is not None:
            try:
                if manifest_path.read_bytes() == rendered_text.encode("utf-8"):
                    atomic_write.write_atomically(manifest_path, original)
            except OSError:
                pass
        raise
    except OSError as exc:
        if lock_path is not None:
            try:
                _restore_lock(lock_path, lock_before)
            except CrateProfileError:
                pass
        elif lock_snapshots:
            try:
                _restore_changed_cargo_locks(lock_snapshots)
            except CrateProfileError:
                pass
        if updated_file and original is not None and rendered_text is not None:
            try:
                if manifest_path.read_bytes() == rendered_text.encode("utf-8"):
                    atomic_write.write_atomically(manifest_path, original)
            except OSError:
                pass
        raise CrateProfileError("filesystem-error", f"cargo-verus profile configuration could not safely access a required path: {exc}") from exc
    finally:
        try:
            fcntl.flock(ledger_fd, fcntl.LOCK_UN)
        finally:
            os.close(ledger_fd)


def error_document(error: CrateProfileError) -> dict[str, Any]:
    return {
        "event": AUDIT_EVENT,
        "schema_version": AUDIT_SCHEMA_VERSION,
        "state": "refused",
        "command_result": 2,
        "error": {"code": error.code, "message": str(error), "required_inputs": error.required_inputs},
    }


def render_text(record: dict[str, Any], diff: str) -> str:
    lines = [
        f"crate: {record['crate']} ({record['crate_dir']})",
        f"profile: {record['profile']} v{record['profile_version']} ({record['transition']})",
        f"result: {record['result']}",
        "changed fields: " + (", ".join(field["path"] for field in record["changed_fields"]) or "none"),
        f"vstd: ={record['vstd_version']} (Verus {record['verus_version']}, Rust {record['rust_toolchain']})",
        f"grant: {record['grant_id']} (issue {record['issue']}, issuer {record['issuer']})",
        "validation: " + "; ".join(record["validation"].values()),
    ]
    if diff:
        lines.extend(("Cargo.toml diff:", diff.rstrip("\n")))
    else:
        lines.append("Cargo.toml diff: no changes")
    lines.append("Git retains the durable diff, history, review, and rollback.")
    return "\n".join(lines)
