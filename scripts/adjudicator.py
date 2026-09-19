#!/usr/bin/env python3
"""The executable's own identity and self-attestation (chainlink #57).

The trust model, stated once and implemented here so `version --verify`,
`doctor`, and a target repo's `gate_integrity` all read the same values:

* A packaged `ligature` zipapp embeds `ligature_data/BUILD_ATTESTATION.json`
  listing, for every archive member (itself excluded), a sha256, plus a
  `content_hash` over that manifest's canonical JSON. Changing any shipped
  byte, adding a file, or removing one changes `content_hash`.
* `version --verify` (and `doctor`) recompute the manifest from the running
  archive and compare. A mismatch is `verified: "false"` and a non-zero
  exit -- the executable refuses to be a trusted adjudicator when it cannot
  prove its own identity.
* A source checkout has no build to verify: `verified: "unknown"` is the
  only honest value (see docs/trust-and-compatibility-boundaries.md §10).
  It is never reported as `true`.
* `ligature init` records the adjudicator that installed a workspace in the
  ownership manifest. From then on `gate_integrity` pins that identity, not
  only the copied repository files, and an unattested or mismatched
  adjudicator fails closed (scripts/ligature_install.py,
  scripts/project_state.py).

The `content_hash` deliberately hashes member *contents*, not the raw zip
container, so it is independent of zip metadata/compression and reproducible
across rebuilds.
"""
from __future__ import annotations

import hashlib
import json
import platform
import sys
import zipfile
from pathlib import Path

import resources

PRODUCT_NAME = "ligature"
# #57 produces the first packaged build; the version string itself is
# deliberately unchanged here so the existing project-state examples remain
# honest about a pre-release artifact.
PRODUCT_VERSION = "0.0.0-unreleased"

# The interpreter floor this packaging contract supports and needs `doctor`
# to report rather than silently assume. `importlib.resources.files` (the
# packaged-resource API this distribution uses) is 3.9+; the union type
# syntax used in runtime annotations settles on 3.10.
REQUIRED_PYTHON: tuple[int, int] = (3, 10)
REQUIRED_PLATFORM = "any"

ATTESTATION_ARCNAME = "ligature_data/BUILD_ATTESTATION.json"

# gate_integrity pin token and the sentinel recorded when a workspace was
# initialized by an unattested source checkout rather than a packaged build.
ADJUDICATOR_PIN_TOKEN = "@adjudicator"
UNATTESTED = "unattested"


def required_python_string() -> str:
    return "{}.{}".format(*REQUIRED_PYTHON)


def interpreter_status(python_version: tuple[int, ...] | None = None) -> tuple[bool, str]:
    info = python_version or sys.version_info
    current = (info[0], info[1])
    current_str = "{}.{}.{}".format(info[0], info[1], info[2])
    return current >= REQUIRED_PYTHON, current_str


def archive_path() -> Path | None:
    """The running zipapp's own path, or None from a source checkout."""
    if not resources.is_packaged():
        return None
    candidate = Path(sys.argv[0])
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()
    if candidate.is_file() and zipfile.is_zipfile(candidate):
        return candidate
    return None


def canonical_manifest(manifest: dict[str, str]) -> str:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"))


def hash_manifest(manifest: dict[str, str]) -> str:
    return "sha256:" + hashlib.sha256(canonical_manifest(manifest).encode("utf-8")).hexdigest()


def compute_content_hash(archive: Path) -> tuple[str, dict[str, str]]:
    """(content_hash, {arcname: 'sha256:...'}) over every regular member of
    the archive except the attestation itself."""
    manifest: dict[str, str] = {}
    with zipfile.ZipFile(archive) as zf:
        for name in zf.namelist():
            if name.endswith("/") or name == ATTESTATION_ARCNAME:
                continue
            manifest[name] = "sha256:" + hashlib.sha256(zf.read(name)).hexdigest()
    return hash_manifest(manifest), manifest


def embedded_attestation() -> dict | None:
    """The build attestation this distribution carries, or None when
    running from a source checkout (which carries none)."""
    if not resources.is_packaged():
        return None
    try:
        return json.loads(resources.resource_text("BUILD_ATTESTATION.json"))
    except (FileNotFoundError, OSError, ValueError, KeyError):
        return None


def schema_version_of(data: dict) -> str:
    """Best-effort read of a JSON Schema document's own version: the
    project convention is a top-level `schema_version`, but several
    schemas encode it as `properties.schema_version.const`."""
    value = data.get("schema_version")
    if isinstance(value, str):
        return value
    prop = data.get("properties", {})
    if isinstance(prop, dict):
        schema_version = prop.get("schema_version")
        if isinstance(schema_version, dict):
            const = schema_version.get("const")
            if isinstance(const, str):
                return const
            enum = schema_version.get("enum")
            if isinstance(enum, list) and len(enum) == 1:
                return str(enum[0])
    return "unknown"


def discover_schema_versions(root: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for path in sorted(root.rglob("*.json")):
        if "schema" not in path.name:
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            found[path.relative_to(root).as_posix()] = schema_version_of(data)
    return found


def current_identity() -> dict:
    """Describe (and, when packaged, verify) the process that is running.
    Read-only: never writes to a workspace."""
    packaged = resources.is_packaged()
    interpreter_ok, python_str = interpreter_status()
    info: dict = {
        "kind": "zipapp" if packaged else "source-checkout",
        "product_name": PRODUCT_NAME,
        "version": PRODUCT_VERSION,
        "required_python": required_python_string(),
        "python": python_str,
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "required": REQUIRED_PLATFORM,
        },
        "interpreter_ok": interpreter_ok,
        "verified": "unknown",
        "content_hash": None,
        "archive_path": None,
        "bundled_schemas": {},
        "file_count": None,
        "errors": [],
    }
    if not packaged:
        info["bundled_schemas"] = discover_schema_versions(resources.resource_root())
        return info

    archive = archive_path()
    if archive is None:
        info["verified"] = "false"
        info["errors"].append("packaged run could not locate its own zip archive")
        return info
    info["archive_path"] = str(archive)
    try:
        content_hash, manifest = compute_content_hash(archive)
    except (OSError, zipfile.BadZipFile) as exc:
        info["verified"] = "false"
        info["errors"].append(f"could not hash the running archive: {exc}")
        return info
    info["content_hash"] = content_hash
    info["file_count"] = len(manifest)
    embedded = embedded_attestation()
    if embedded is None:
        info["verified"] = "false"
        info["errors"].append("the archive embeds no build attestation")
    else:
        info["bundled_schemas"] = embedded.get("bundled_schemas", {})
        if embedded.get("content_hash") != content_hash:
            info["verified"] = "false"
            info["errors"].append(
                "archive content does not match its embedded build hash -- the executable is not attested"
            )
        elif embedded.get("product_version") != PRODUCT_VERSION:
            info["verified"] = "false"
            info["errors"].append(
                "embedded build version does not match the running product version"
            )
        else:
            info["verified"] = "true"
    if not interpreter_ok:
        info["errors"].append(
            f"python {python_str} is below the required {required_python_string()}"
        )
    return info


def identity_ok(info: dict) -> bool:
    """A packaged build must both self-verify and run on a supported
    interpreter. A source checkout is never 'ok' for --verify (unknown)."""
    return info["verified"] == "true" and bool(info["interpreter_ok"])


def identity_hash() -> str | None:
    """The content hash to pin in a target repo's `gate_integrity`, or None
    from an unattested source checkout."""
    info = current_identity()
    return info.get("content_hash")


def render_identity_text(info: dict, *, show_schemas: bool = False) -> str:
    lines = [
        f"{info['product_name']} {info['version']}",
        f"  adjudicator: {info['kind']} (attested: {info['verified']})",
        f"  content hash: {info['content_hash'] or '(none -- source checkout)'}",
        f"  bundle: {info['file_count']} file(s)" if info["file_count"] is not None else "  bundle: (source checkout)",
        f"  bundled schemas: {len(info['bundled_schemas'])}",
        f"  required python: >= {info['required_python']} (running {info['python']})",
        f"  platform: {info['platform']['system']}/{info['platform']['machine']} "
        f"(required: {info['platform']['required']})",
    ]
    if info.get("archive_path"):
        lines.append(f"  artifact: {info['archive_path']}")
    if show_schemas and info["bundled_schemas"]:
        lines.append("  bundled schemas:")
        for name in sorted(info["bundled_schemas"]):
            lines.append(f"    {name}: {info['bundled_schemas'][name]}")
    for error in info["errors"]:
        lines.append(f"  ERROR: {error}")
    return "\n".join(lines) + "\n"
