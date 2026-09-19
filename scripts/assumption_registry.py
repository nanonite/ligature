#!/usr/bin/env python3
"""Canonical assumption registry (chainlink #59, plan.md §8.4).

Today's only assumption-identity mechanism is the composite
`{boundary_id, tracking_issue, assumption_hash}` (docs/boundary-contract-
schema.json's `assumption_ref` $def, reused by work-package manifests).
`assumption_hash` is a bare DECLARED string -- nothing in this pipeline
stored the assumption's text or recomputed the hash from it, so a match
proved only that the same string was typed at each site. This module
introduces a governed registry of assumptions whose `content_hash` is
RECOMPUTED from stored `canonical_text`, then resolves references against
it.

Staged migration, exactly the issue's phases:

* **Phase A** (this module, `check_assumption_registry` with no registry
  present returns nothing): add and populate the registry; every declared
  reference is validated against it. Existing composite references remain
  fully valid and authoritative -- nothing changes about what is
  authoritative.
* **Phase B** (`DualResolution`): a reference is EITHER a registry id
  (`{"assumption_id": "ASM-..."}`) or the legacy composite; both resolve.
  A legacy composite now emits a visible, non-blocking `info` finding.
* **Phase C** (`migrate_assumption_refs`): deterministic rewrite of
  generated/work-package references from composite to registry id. It
  NEVER rewrites a human-reviewed boundary contract's own reviewed
  `assumptions[]`; those are reported as requiring the normal human
  approve checkpoint. `--apply` additionally requires an explicit
  `--reviewer`, mirroring accept-promotion.
* **Phase D** (`legacy_refs_supported`, `ASSUMPTION_REF_LEGACY_REMOVAL_VERSION`):
  legacy composite resolution is declared removed only in a future
  breaking registry schema major. A registry declaring that major makes a
  legacy composite an ERROR, and an unknown/unsupported major is itself
  refused rather than reinterpreted -- the same explicit-version,
  refuse-on-mismatch discipline `scripts/ligature_install.py` uses for
  `manifest_schema_version`.

Authority boundary, stated honestly: a registry reference resolves to
exactly one entry and the entry's content hash is recomputed from its own
text, so a hash match is a real content binding here -- unlike the bare
declared composite hash. That still establishes only "this text hashes to
this value", never that the assumption is true, adequate, or approved;
approval remains the human `assumption_ref`/review checkpoints.
`validate_boundary_contracts.check_assumption_identity_collisions()` stays
as defense in depth and is not removed or superseded.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from atomic_write import write_atomically  # noqa: E402
from schema_utils import make_validator  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "docs" / "assumption-registry-schema.json"

REGISTRY_DIR_PARTS = ("specs", "_assumptions")
WORKSPACE_MANIFEST_DIRS = ("ci", "manifest")

# The reference-resolution era this product implements. Registry documents
# declaring a different major are refused (see legacy_refs_supported).
ASSUMPTION_REF_SCHEMA_VERSION = "1.0"
ASSUMPTION_REF_LEGACY_REMOVAL_VERSION = "2.0"

ASSUMPTION_ID_RE = re.compile(r"^ASM-[a-z0-9][a-z0-9-]*$")
CONTENT_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class RegistryError(Exception):
    """A refused operation: no registry to migrate against, an
    incompatible registry major, or a missing reviewer on `--apply`."""


@dataclass
class Finding:
    gate: str
    path: Path
    reason: str
    severity: str = "error"  # "error" (blocking) | "info" (visible, non-blocking)

    def __str__(self) -> str:
        return f"[{self.gate}/{self.severity}] {self.path}: {self.reason}"


# ---------------------------------------------------------------------------
# Canonical content + version mechanism
# ---------------------------------------------------------------------------
def canonical_assumption_text(text: str) -> str:
    """The canonical form a `content_hash` is computed over: CRLF/CR
    normalised to LF, each line right-stripped, surrounding blank lines
    removed. A whitespace-only reformat does not invalidate a hash; any
    change to actual words does."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in normalized.splitlines()).strip()


def canonical_assumption_hash(text: str) -> str:
    canonical = canonical_assumption_text(text)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _major(version: str) -> int | None:
    match = re.match(r"^([0-9]+)\.", str(version))
    return int(match.group(1)) if match else None


def legacy_refs_supported(registry_schema_version: str) -> bool:
    """Phase D mechanism. True while the registry declares an era before
    the removal version. An unknown/unparseable version is NOT assumed
    supported -- the caller reports it as incompatible rather than
    reinterpreting it."""
    major = _major(registry_schema_version)
    removal_major = _major(ASSUMPTION_REF_LEGACY_REMOVAL_VERSION)
    if major is None or removal_major is None:
        return False
    return major < removal_major


def _load_validator():
    return make_validator(json.loads(SCHEMA_PATH.read_text()))


# ---------------------------------------------------------------------------
# Registry loading
# ---------------------------------------------------------------------------
def registry_dir(workspace: Path) -> Path:
    return workspace.joinpath(*REGISTRY_DIR_PARTS)


@dataclass
class RegistryIndex:
    entries_by_id: dict[str, list[dict]] = field(default_factory=dict)
    docs: list[dict] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    present: bool = False
    legacy_supported: bool = True

    def resolve_id(self, assumption_id: str) -> list[dict]:
        return self.entries_by_id.get(assumption_id, [])


def load_registry(workspace: Path) -> RegistryIndex:
    """Load every `specs/_assumptions/*.json` document. Version is checked
    BEFORE schema validation so a future-major registry is reported as
    incompatible rather than as a v1.0 schema violation. Returns an empty
    index (present=False) when no registry exists -- phase A's own
    "old-workspace-only" state, which changes nothing."""
    index = RegistryIndex()
    directory = registry_dir(workspace)
    if not directory.is_dir():
        return index
    paths = sorted(p for p in directory.glob("*.json") if p.is_file())
    if not paths:
        return index
    index.present = True
    validator = _load_validator()
    for path in paths:
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            index.findings.append(Finding("G21", path, f"registry document is not valid JSON: {exc}"))
            continue
        if not isinstance(data, dict):
            index.findings.append(Finding("G21", path, "registry document is not a JSON object"))
            continue
        version = data.get("schema_version")
        if not legacy_refs_supported(str(version)):
            index.legacy_supported = False
            index.findings.append(
                Finding(
                    "G21", path,
                    f"registry schema_version {version!r} is not supported by this product "
                    f"(implements {ASSUMPTION_REF_SCHEMA_VERSION}, legacy composite support removed at "
                    f"{ASSUMPTION_REF_LEGACY_REMOVAL_VERSION}) -- refusing to reinterpret it",
                )
            )
            index.docs.append(data)
            _collect_entries(data, path, index)
            continue
        errors = list(validator.iter_errors(data))
        if errors:
            index.findings.append(
                Finding("G21", path, "registry document fails G1a: " + "; ".join(e.message for e in errors))
            )
            continue
        index.docs.append(data)
        _collect_entries(data, path, index)
    _report_duplicate_ids(index)
    return index


def _collect_entries(data: dict, path: Path, index: RegistryIndex) -> None:
    for entry in data.get("entries", []) or []:
        if not isinstance(entry, dict):
            continue
        entry = dict(entry)
        entry.setdefault("_source", str(path))
        aid = entry.get("assumption_id")
        if isinstance(aid, str) and ASSUMPTION_ID_RE.match(aid):
            index.entries_by_id.setdefault(aid, []).append(entry)
        # Recompute the content hash from the stored text -- this is the
        # whole point of the registry versus a bare declared hash.
        text = entry.get("canonical_text")
        stored = entry.get("content_hash")
        if isinstance(text, str):
            if not canonical_assumption_text(text):
                index.findings.append(
                    Finding("G21", path, f"{aid!r}: canonical_text is empty after canonicalisation")
                )
            elif isinstance(stored, str) and stored != canonical_assumption_hash(text):
                index.findings.append(
                    Finding(
                        "G21", path,
                        f"{aid!r}: declared content_hash {stored!r} does not match the hash "
                        f"recomputed from its canonical_text ({canonical_assumption_hash(text)}) -- "
                        "the entry's text changed without a re-signed hash",
                    )
                )


def _report_duplicate_ids(index: RegistryIndex) -> None:
    for aid, entries in sorted(index.entries_by_id.items()):
        if len(entries) > 1:
            sources = sorted({e.get("_source", "?") for e in entries})
            index.findings.append(
                Finding(
                    "G21", Path(f"assumption_id:{aid}"),
                    f"assumption_id {aid!r} is declared by {len(entries)} registry entries across "
                    f"{sources} -- every reference to it is ambiguous",
                )
            )


# ---------------------------------------------------------------------------
# Reference discovery
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Reference:
    ref: dict
    source: Path
    label: str

    @property
    def is_registry(self) -> bool:
        return "assumption_id" in self.ref

    def describe(self) -> str:
        if self.is_registry:
            return f"assumption_id={self.ref.get('assumption_id')!r}"
        return (
            f"boundary_id={self.ref.get('boundary_id')!r} "
            f"tracking_issue={self.ref.get('tracking_issue')!r} "
            f"assumption_hash={self.ref.get('assumption_hash')!r}"
        )


def _boundary_references(workspace: Path, boundaries: list[dict] | None) -> list[Reference]:
    refs: list[Reference] = []
    if boundaries is None:
        boundaries = []
        for path in sorted(workspace.glob("**/specs/_boundaries/*.json")):
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(data, dict):
                boundaries.append(data)
    for boundary in boundaries:
        bid = boundary.get("boundary_id", "?")
        for assumption in boundary.get("assumptions", []) or []:
            if isinstance(assumption, dict):
                refs.append(
                    Reference(assumption, Path(f"boundary:{bid}"), f"boundary {bid!r} assumptions[]")
                )
    return refs


def _manifest_references(workspace: Path, manifests: list[tuple[Path, dict]] | None) -> list[Reference]:
    refs: list[Reference] = []
    if manifests is None:
        manifests = []
        directory = workspace.joinpath(*WORKSPACE_MANIFEST_DIRS)
        if directory.is_dir():
            for path in sorted(directory.iterdir()):
                if path.suffix not in (".json", ".yaml", ".yml"):
                    continue
                data = _load_manifest_data(path)
                if isinstance(data, dict):
                    manifests.append((path, data))
    for path, data in manifests:
        dod = (data.get("definition_of_done") or {}) if isinstance(data, dict) else {}
        for entry in dod.get("trusted_assumptions", []) or []:
            if isinstance(entry, dict) and isinstance(entry.get("assumption_ref"), dict):
                refs.append(Reference(entry["assumption_ref"], path, f"{path.name} trusted_assumptions[]"))
    return refs


def _load_manifest_data(path: Path):
    if path.suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError:
            return None
        try:
            return yaml.safe_load(path.read_text())
        except Exception:
            return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


# ---------------------------------------------------------------------------
# Phase B gate
# ---------------------------------------------------------------------------
def _resolve(index: RegistryIndex, reference: Reference) -> list[Finding]:
    ref = reference.ref
    if reference.is_registry:
        aid = ref.get("assumption_id")
        candidates = index.resolve_id(str(aid))
        if not candidates:
            return [
                Finding("G21", reference.source, f"dangling reference: {reference.label} names {reference.describe()}, which is not in the registry")
            ]
        if len(candidates) > 1:
            return [
                Finding("G21", reference.source, f"ambiguous reference: {reference.label} {reference.describe()} resolves to {len(candidates)} registry entries")
            ]
        return []

    tracking = ref.get("tracking_issue")
    declared = ref.get("assumption_hash")
    matching = [
        entry
        for entries in index.entries_by_id.values()
        for entry in entries
        if entry.get("tracking_issue") == tracking
    ]
    same_hash = [entry for entry in matching if isinstance(entry.get("content_hash"), str)]
    exact = [entry for entry in same_hash if _entry_hash(entry) == declared]
    if len(exact) == 1:
        return []
    if len(exact) > 1:
        return [
            Finding("G21", reference.source, f"ambiguous reference: {reference.label} {reference.describe()} matches {len(exact)} registry entries")
        ]
    if same_hash:
        return [
            Finding(
                "G21", reference.source,
                f"hash-disagreeing reference: {reference.label} declares assumption_hash {declared!r}, but "
                f"the registry entry for tracking_issue {tracking!r} recomputes to "
                f"{sorted({_entry_hash(e) for e in same_hash})} -- the declared hash is not the registry's "
                "canonical content hash",
            )
        ]
    return [
        Finding(
            "G21", reference.source,
            f"missing reference: {reference.label} {reference.describe()} has no registry entry -- "
            "populate the registry for every declared assumption_ref (phase A)",
        )
    ]


def _entry_hash(entry: dict) -> str | None:
    text = entry.get("canonical_text")
    if not isinstance(text, str):
        return None
    return canonical_assumption_hash(text)


def check_assumption_registry(
    workspace: Path,
    *,
    boundaries: list[dict] | None = None,
    manifests: list[tuple[Path, dict]] | None = None,
) -> list[Finding]:
    """The new gate. Returns [] in phase A's old-workspace-only state (no
    registry). With a registry present:
      * a registry-ID reference that names an unknown id is DANGLING,
      * a reference matching more than one entry is AMBIGUOUS,
      * a composite whose declared hash is not the registry's recomputed
        canonical hash is HASH-DISAGREEING,
      * a composite with no corresponding entry is MISSING,
      * a legacy composite emits a non-blocking deprecation `info`
        finding while legacy support is declared, and an ERROR once a
        registry declares the phase-D removal major.
    """
    index = load_registry(workspace)
    if not index.present:
        return []
    findings = list(index.findings)
    references = _boundary_references(workspace, boundaries) + _manifest_references(workspace, manifests)
    for reference in references:
        if not reference.is_registry:
            if index.legacy_supported:
                findings.append(
                    Finding(
                        "G21", reference.source,
                        f"deprecation: {reference.label} uses the legacy composite assumption_ref "
                        f"({reference.describe()}); prefer the registry form {{'assumption_id': 'ASM-...'}} "
                        "and run `ligature migrate --assumptions`",
                        severity="info",
                    )
                )
            else:
                findings.append(
                    Finding(
                        "G21", reference.source,
                        f"legacy composite assumption_ref is no longer supported by registry era "
                        f"{ASSUMPTION_REF_LEGACY_REMOVAL_VERSION}: {reference.label} {reference.describe()}",
                    )
                )
        findings.extend(_resolve(index, reference))
    return findings


# ---------------------------------------------------------------------------
# Phase C migration (references only, never reviewed content)
# ---------------------------------------------------------------------------
@dataclass
class MigrationChange:
    path: Path
    label: str
    old_ref: dict
    new_ref: dict


@dataclass
class MigrationReport:
    apply: bool
    reviewer: str | None
    changed: list[MigrationChange] = field(default_factory=list)
    unchanged: list[Path] = field(default_factory=list)
    unresolved: list[Finding] = field(default_factory=list)
    requires_human_approval: list[tuple[Path, str]] = field(default_factory=list)
    rewritten_files: list[Path] = field(default_factory=list)


def _rewrite_refs(data: dict, index: RegistryIndex, unresolved: list[Finding], label_path: Path) -> tuple[dict, list[MigrationChange]]:
    """Return (possibly-rewritten manifest data, changes). Only rewrites
    `definition_of_done.trusted_assumptions[].assumption_ref` composites
    that resolve uniquely."""
    rewritten = copy.deepcopy(data)
    changes: list[MigrationChange] = []
    dod = rewritten.get("definition_of_done")
    if not isinstance(dod, dict):
        return rewritten, changes
    for entry in dod.get("trusted_assumptions", []) or []:
        ref = entry.get("assumption_ref") if isinstance(entry, dict) else None
        if not isinstance(ref, dict) or "assumption_id" in ref:
            continue
        reference = Reference(ref, label_path, f"{label_path.name} trusted_assumptions[]")
        findings = _resolve(index, reference)
        if any(f.severity == "error" for f in findings):
            unresolved.extend(findings)
            continue
        exact = [
            e
            for entries in index.entries_by_id.values()
            for e in entries
            if e.get("tracking_issue") == ref.get("tracking_issue") and _entry_hash(e) == ref.get("assumption_hash")
        ]
        if len(exact) != 1:
            continue
        new_ref = {"assumption_id": exact[0]["assumption_id"]}
        entry["assumption_ref"] = new_ref
        changes.append(MigrationChange(label_path, f"{label_path.name} trusted_assumptions[]", dict(ref), new_ref))
    return rewritten, changes


def migrate_assumption_refs(
    workspace: Path,
    *,
    apply: bool = False,
    reviewer: str | None = None,
) -> MigrationReport:
    index = load_registry(workspace)
    if not index.present:
        raise RegistryError(
            "no assumption registry present (specs/_assumptions/*.json); phase A must land before phase C"
        )
    if not index.legacy_supported:
        raise RegistryError(
            f"registry declares a schema major that removed legacy composite support "
            f"({ASSUMPTION_REF_LEGACY_REMOVAL_VERSION}); refusing to migrate"
        )
    if apply and not reviewer:
        raise RegistryError("--apply requires an explicit --reviewer (a human triggers the rewrite)")

    report = MigrationReport(apply=apply, reviewer=reviewer)

    # Reviewed boundary contracts: NEVER rewritten here. Their own
    # `assumptions[]` is human-reviewed content; migrating it is a change
    # that must go through the normal approve checkpoint.
    for boundary_data, source in _reviewed_boundary_refs(workspace):
        for ref in boundary_data:
            report.requires_human_approval.append((source, _ref_label(ref)))

    manifests_dir = workspace.joinpath(*WORKSPACE_MANIFEST_DIRS)
    if not manifests_dir.is_dir():
        return report
    for path in sorted(manifests_dir.iterdir()):
        if path.suffix not in (".json", ".yaml", ".yml"):
            continue
        data = _load_manifest_data(path)
        if not isinstance(data, dict):
            continue
        rewritten, changes = _rewrite_refs(data, index, report.unresolved, path)
        if not changes:
            report.unchanged.append(path)
            continue
        if apply:
            if path.suffix in (".yaml", ".yml"):
                import yaml  # type: ignore
                content = yaml.safe_dump(rewritten, sort_keys=False)
            else:
                content = json.dumps(rewritten, indent=2) + "\n"
            write_atomically(path, content)
            report.rewritten_files.append(path)
        report.changed.extend(changes)
    return report


def _reviewed_boundary_refs(workspace: Path):
    for path in sorted(workspace.glob("**/specs/_boundaries/*.json")):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("assumptions"), list):
            yield data["assumptions"], path


def _ref_label(ref: dict) -> str:
    if "assumption_id" in ref:
        return f"assumption_id={ref['assumption_id']!r}"
    return (
        f"boundary_id={ref.get('boundary_id')!r} tracking_issue={ref.get('tracking_issue')!r} "
        f"assumption_hash={ref.get('assumption_hash')!r}"
    )


def render_migration_text(report: MigrationReport) -> str:
    mode = "apply" if report.apply else "dry-run"
    lines = [f"assumption-ref migration ({mode})"]
    for change in report.changed:
        lines.append(f"  rewrite  {change.path}: {_ref_label(change.old_ref)} -> {change.new_ref['assumption_id']}")
    for path in report.unchanged:
        lines.append(f"  unchanged  {path}")
    for path, label in report.requires_human_approval:
        lines.append(f"  requires-human-approval  {path}: {label} (reviewed boundary content, not auto-rewritten)")
    for finding in report.unresolved:
        lines.append(f"  UNRESOLVED  {finding}")
    if report.apply:
        lines.append(f"  reviewer: {report.reviewer}")
        lines.append(f"  files rewritten: {len(report.rewritten_files)}")
    return "\n".join(lines) + "\n"
