#!/usr/bin/env python3
"""Safe, idempotent installation of Ligature-owned files into a target
repository (chainlink #58).

`ligature init` writes a small, versioned set of product-owned files into
an independent target repo: the mode-correct project descriptor, the
reliance-policy template, the installed v1.0 output schemas and prompt
templates, and `.codex/skills/ligature/SKILL.md`. It also writes a
versioned **ownership manifest** at `ci/manifest/installation.json`
recording, for every installed file:

* its ownership (`managed` = product-owned, `user` = a template the target
  repo's maintainers own after creation),
* the **base hash** (the content the product last installed -- the merge
  base for the eventual three-way comparison),
* the **current expected hash** (what the running product expects now),
* for the skill, the **authority-region hash** that `doctor` verifies.

Design rules, from the issue and this codebase's own disciplines:

* **Atomic.** Every write goes through `atomic_write.write_atomically`;
  a failure partway through leaves the previous file byte-identical and no
  temporary file behind.
* **Safe.** Workspace-relative paths only; absolute paths and `..` are
  refused, and no write is ever made through a symlinked directory or to a
  symlinked destination. Every destination is re-checked for containment
  inside the resolved workspace.
* **Idempotent.** Rerunning `init` over a clean install writes nothing and
  reports every file `unchanged`. A locally modified managed file is a
  `conflict`, never silently overwritten; `migrate` is the explicit
  recovery path.
* **Three-way aware.** `inspect()` classifies each managed file against
  (base, on-disk, freshly rendered expected): `current`, `upgrade`,
  `conflict`, `missing`, or `obsolete`.

The module deliberately owns no CLI parsing -- `pipeline.py`'s
`cmd_init`/`cmd_doctor`/`cmd_migrate` call into it so the argparse surface
stays in the one place `tests/test_inventory_drift.py` already checks.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adjudicator  # noqa: E402
import resources  # noqa: E402
from atomic_write import write_atomically  # noqa: E402
from project_descriptor import schema_diagnostics  # noqa: E402
from project_state import KNOWN_SCHEMA_VERSIONS  # noqa: E402
from project_state import PRODUCT_VERSION  # noqa: E402

ROOT = resources.resource_root()

MANIFEST_SCHEMA_VERSION = "1.0"
MANIFEST_RELATIVE_PATH = "ci/manifest/installation.json"
SKILL_VERSION = "1.0"
SKILL_RELATIVE_PATH = ".codex/skills/ligature/SKILL.md"

AUTHORITY_BEGIN = "<!-- LIGATURE-AUTHORITY-BEGIN"
AUTHORITY_END = "<!-- LIGATURE-AUTHORITY-END -->"
AUTHORITY_HASH_TOKEN = "__LIGATURE_AUTHORITY_HASH__"
_AUTHORITY_HASH_RE = re.compile(r"LIGATURE-AUTHORITY-BEGIN sha256:([0-9a-f]{64})")

# The reliance policy's own version-marker convention
# (docs/reliance-policy.template.md): exactly one
# "Policy version: <name>@<major>.<minor>" line. Shared by
# generate_promotion_receipt.extract_policy_version (which reads
# policy_version from it at promotion time) and accept_policy below (which
# refuses to record a hash for a document that cannot yield one), so the two
# can never disagree about what a well-formed policy document is
# (chainlink #78).
POLICY_VERSION_MARKER_RE = re.compile(
    r"^Policy version:\s*`?([a-z][a-z0-9-]*@[0-9]+(?:\.[0-9]+)*)`?\s*$", re.MULTILINE
)

MANAGED = "managed"
USER = "user"

# The managed product paths pinned by the installed descriptor's
# gate_integrity list and recorded in the manifest's gate_hashes, so #56's
# `status` can report gate integrity as `pinned` rather than `unpinned`.
#
# `@adjudicator` is the #57 extension: the executable is itself the trusted
# adjudicator, so the descriptor pins the identity of the process that
# installed the workspace, not only the repository files it copied. Its
# recorded value is the running build's content hash, or the sentinel
# `unattested` when the workspace was initialized from a source checkout
# (which has no build to pin). scripts/project_state.py compares that pin
# against the adjudicator actually running and fails closed on a mismatch or
# an unattested adjudicator.
_GATE_PINNED_PATHS = (
    SKILL_RELATIVE_PATH,
    ".ligature/schemas/project-state.schema.json",
    ".ligature/schemas/consolidated-check.schema.json",
    adjudicator.ADJUDICATOR_PIN_TOKEN,
)

ADJUDICATOR_PIN_TOKEN = adjudicator.ADJUDICATOR_PIN_TOKEN
UNATTESTED = adjudicator.UNATTESTED

_PROMPTS = (
    "stage-0-evidence-intake.md",
    "stage-3-boundary-drafting.md",
    "stage-3-interaction-drafting.md",
    "stage-3-bridge-drafting.md",
    "stage-3-witness-drafting.md",
    "stage-3-exemption-drafting.md",
    "stage-3-protocol-debt-drafting.md",
    "stage-3-conflict-resolution-drafting.md",
    "stage-3-concept-to-code.md",
)


class InstallError(Exception):
    """A refused operation: an unsafe path, a symlink, an incompatibility,
    or an attempt to act on an uninitialized workspace."""


@dataclass(frozen=True)
class RegistryEntry:
    path: str
    source: Path | None
    ownership: str
    kind: str  # "descriptor" | "policy" | "skill" | "copy"
    skill: bool = False
    # A user-owned file that is a NORMATIVE input to the pipeline (the
    # reliance policy, and any future governance doc): its on-disk content
    # is drift-checked against the manifest's recorded base_hash, and an
    # unreviewed change is reported rather than silently adopted
    # (chainlink #78). The descriptor is user-owned but NOT normative --
    # its integrity mechanism is schema validation (#74), not a hash pin,
    # because it is meant to be freely edited by the project.
    normative: bool = False


def file_registry(mode: str, name: str, descriptor_rel: str) -> list[RegistryEntry]:
    if mode not in ("greenfield", "port"):
        raise InstallError(f"unknown init mode {mode!r}; expected 'greenfield' or 'port'")
    descriptor_example = ROOT / "schemas" / "examples" / f"project-descriptor.{mode}.example.json"
    entries = [
        RegistryEntry(descriptor_rel, descriptor_example, USER, "descriptor"),
        RegistryEntry(
            "docs/reliance-policy.md", ROOT / "docs" / "reliance-policy.template.md", USER, "policy", normative=True
        ),
        RegistryEntry(SKILL_RELATIVE_PATH, ROOT / "docs" / "ligature-skill.template.md", MANAGED, "skill", skill=True),
        # chainlink #76: the schema that governs the user-owned descriptor
        # is also shipped into the project root itself, not only into
        # .ligature/schemas/ -- a black-box probe of the v1.0 surface could
        # not discover the descriptor's permitted shape (the verifier enum
        # in particular) from anything `init` wrote, because the schema
        # lived only inside the binary's attested resource bundle.
        RegistryEntry("schemas/project-descriptor.schema.json", ROOT / "schemas" / "project-descriptor.schema.json", MANAGED, "copy"),
        RegistryEntry(".ligature/schemas/project-descriptor.schema.json", ROOT / "schemas" / "project-descriptor.schema.json", MANAGED, "copy"),
        RegistryEntry(".ligature/schemas/project-state.schema.json", ROOT / "schemas" / "project-state.schema.json", MANAGED, "copy"),
        RegistryEntry(".ligature/schemas/consolidated-check.schema.json", ROOT / "schemas" / "consolidated-check.schema.json", MANAGED, "copy"),
        # chainlink #84: `validate-work-package`'s G13 requires a
        # gate_integrity entry for EACH of these two paths with a runner
        # that resolves to a REAL file inside the workspace --
        # unconditionally, for every manifest (plan.md §16.5, chainlink
        # #35, check_witness_renderer_integrity) -- but v1.0's `init`
        # installed neither, so no shipped CLI could ever satisfy the
        # gate (the date-creusot pilot's Stage 7 blocker; the pilot
        # worked around it with placeholder zero hashes rather than
        # fabricating a renderer, which would have laundered a fake pass
        # through the very gate meant to protect it).
        #
        # Installed MANAGED, never user-owned: the renderer is
        # gate-adjacent code, so a locally modified copy is a CONFLICT
        # that `migrate --force` recovers explicitly -- never a template
        # the target repo is free to edit. Their path set is pinned to
        # `validate_work_package.WITNESS_RENDERER_INTEGRITY_PATHS` by a
        # test, so G13's requirements and what `init` ships cannot drift
        # apart. `scripts/xml_escape.py` ships alongside because
        # witness_renderer.py imports it directly for its own text
        # escaping -- pinning only the entrypoint would leave the helper
        # it executes unpinned.
        RegistryEntry("scripts/witness_renderer.py", ROOT / "scripts" / "witness_renderer.py", MANAGED, "copy"),
        RegistryEntry("scripts/xml_escape.py", ROOT / "scripts" / "xml_escape.py", MANAGED, "copy"),
    ]
    for prompt in _PROMPTS:
        entries.append(RegistryEntry(f".ligature/prompts/{prompt}", ROOT / "prompts" / prompt, MANAGED, "copy"))
    return entries


# ---------------------------------------------------------------------------
# Hashing + rendering
# ---------------------------------------------------------------------------
def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_authority(body: str) -> str:
    """The canonical form of the SKILL.md authority region: each line
    right-stripped, leading/trailing blank lines removed, newlines
    normalised. Both install-time hashing and doctor-time verification use
    exactly this function, so a whitespace-only reformat of the region is
    the only edit that does not trip the pin."""
    return "\n".join(line.rstrip() for line in body.splitlines()).strip()


def authority_region(text: str) -> str:
    try:
        start = text.index(AUTHORITY_BEGIN)
        start = text.index("\n", start) + 1
        end = text.index(AUTHORITY_END, start)
    except ValueError as exc:
        raise InstallError("SKILL.md is missing its authority-region markers") from exc
    return canonical_authority(text[start:end])


def declared_authority_hash(text: str) -> str:
    match = _AUTHORITY_HASH_RE.search(text)
    if match is None:
        raise InstallError("SKILL.md authority-region marker carries no hash")
    return "sha256:" + match.group(1)


def computed_authority_hash(text: str) -> str:
    return _sha256_text(authority_region(text))


def render_skill() -> str:
    template = (ROOT / "docs" / "ligature-skill.template.md").read_text()
    return template.replace(AUTHORITY_HASH_TOKEN, computed_authority_hash(template))


def _render_descriptor(mode: str, name: str) -> str:
    source = ROOT / "schemas" / "examples" / f"project-descriptor.{mode}.example.json"
    data = json.loads(source.read_text())
    data["project"]["name"] = name
    data["project"]["crate_naming_convention"] = f"^{name}-[a-z]+"
    data["crates"] = [
        {"crate_dir": f"crates/{name}-core", "contracts_crate": "contracts", "specs_search_root": "crates"}
    ]
    data["gate_integrity"] = [{"path": p} for p in _GATE_PINNED_PATHS]
    return json.dumps(data, indent=2) + "\n"


# The top-level keys `_render_descriptor` overwrites. A descriptor that is
# byte-identical to the shipped example outside exactly these keys was never
# hand-edited after `init` (chainlink #67).
_DESCRIPTOR_INIT_KEYS = ("project", "crates", "gate_integrity")


def _example_descriptor_for(mode: object) -> dict | None:
    if mode not in ("greenfield", "port"):
        return None
    path = ROOT / "schemas" / "examples" / f"project-descriptor.{mode}.example.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _dig(data: object, *keys: str) -> object:
    node = data
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _matches_example_outside_init_keys(descriptor: dict, example: dict) -> bool:
    ignored = set(_DESCRIPTOR_INIT_KEYS)
    return (
        {k: v for k, v in descriptor.items() if k not in ignored}
        == {k: v for k, v in example.items() if k not in ignored}
    )


def descriptor_placeholder_fields(descriptor: dict) -> list[str]:
    """The descriptor fields still holding the shipped example-fixture
    value(s) -- i.e. the Stage P0 template `init` copied in and nobody
    replaced (chainlink #67).

    Only unambiguous placeholders are reported on their own:
    `review.reviewer` (the literal `"example-reviewer"`) and, for
    `mode: port`, `port_source.repository` (the literal example path). A
    value a real project can legitimately choose by coincidence --
    `verifier_policy.default: "creusot"` is the shipped default -- is
    reported only when the descriptor is byte-identical to the example
    everywhere `_render_descriptor` does not overwrite it, so a project
    that happens to pick the same verifier never false-positives on that
    field alone."""
    example = _example_descriptor_for(descriptor.get("mode"))
    if example is None:
        return []
    fields: list[str] = []
    reviewer = _dig(example, "review", "reviewer")
    if isinstance(reviewer, str) and _dig(descriptor, "review", "reviewer") == reviewer:
        fields.append("review.reviewer")
    if descriptor.get("mode") == "port":
        repository = _dig(example, "port_source", "repository")
        if isinstance(repository, str) and _dig(descriptor, "port_source", "repository") == repository:
            fields.append("port_source.repository")
    if _matches_example_outside_init_keys(descriptor, example):
        verifier = _dig(example, "verifier_policy", "default")
        if isinstance(verifier, str) and _dig(descriptor, "verifier_policy", "default") == verifier:
            fields.append("verifier_policy.default")
    return fields


def _render_policy(name: str) -> str:
    text = (ROOT / "docs" / "reliance-policy.template.md").read_text()
    return text.replace("# Reliance policy — `<project name>`", f"# Reliance policy — `{name}`", 1)


def render_entry(entry: RegistryEntry, mode: str, name: str) -> str:
    if entry.kind == "descriptor":
        return _render_descriptor(mode, name)
    if entry.kind == "policy":
        return _render_policy(name)
    if entry.kind == "skill":
        return render_skill()
    assert entry.source is not None
    return entry.source.read_text()


def entry_authority_hash(entry: RegistryEntry, mode: str, name: str) -> str | None:
    if not entry.skill:
        return None
    return computed_authority_hash(render_entry(entry, mode, name))


# ---------------------------------------------------------------------------
# Safe path resolution + writes
# ---------------------------------------------------------------------------
def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def safe_target(workspace: Path, rel: str) -> Path:
    """Resolve `rel` under `workspace`, refusing anything that could escape
    it or redirect a write through a symlink."""
    pure = PurePosixPath(rel)
    if pure.is_absolute() or not pure.parts or any(part in ("", ".", "..") for part in pure.parts):
        raise InstallError(f"refusing unsafe workspace-relative path: {rel!r}")
    workspace_real = workspace.resolve()
    if not workspace_real.is_dir():
        raise InstallError(f"workspace is not a directory: {workspace}")
    directory = workspace_real
    for part in pure.parts[:-1]:
        directory = directory / part
        if directory.is_symlink():
            raise InstallError(f"refusing to write through symlinked directory: {rel}")
        if directory.exists() and not directory.is_dir():
            raise InstallError(f"path component is not a directory: {directory}")
    final = directory / pure.parts[-1]
    if final.is_symlink():
        raise InstallError(f"refusing to write to a symlinked destination: {rel}")
    if not _is_within(final.parent.resolve(), workspace_real):
        raise InstallError(f"destination escapes the workspace: {rel}")
    return final


def _write(workspace: Path, rel: str, content: str) -> None:
    write_atomically(safe_target(workspace, rel), content)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
def manifest_path(workspace: Path) -> Path:
    return workspace / MANIFEST_RELATIVE_PATH


def load_manifest(workspace: Path) -> dict | None:
    path = manifest_path(workspace)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise InstallError(f"installation manifest is unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise InstallError("installation manifest is not a JSON object")
    return data


def _write_manifest(workspace: Path, manifest: dict) -> None:
    _write(workspace, MANIFEST_RELATIVE_PATH, json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def _manifest_files(manifest: dict | None) -> dict[str, dict]:
    if manifest is None:
        return {}
    return {entry["path"]: entry for entry in manifest.get("files", []) if isinstance(entry, dict) and "path" in entry}


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------
@dataclass
class FilePlan:
    path: str
    ownership: str
    outcome: str  # create|unchanged|upgrade|conflict|user-owned|drifted|missing|obsolete
    content: str | None
    base_hash: str | None
    expected_hash: str | None
    authority_hash: str | None = None
    reason: str = ""
    normative: bool = False


@dataclass
class InstallReport:
    mode: str
    product_name: str
    status: str
    files: list[FilePlan] = field(default_factory=list)
    obsolete: list[str] = field(default_factory=list)
    authority_state: str | None = None
    messages: list[str] = field(default_factory=list)
    descriptor_path: str = "project-descriptor.json"
    descriptor_schema: DescriptorSchemaReport | None = None

    @property
    def conflicts(self) -> list[FilePlan]:
        return [f for f in self.files if f.outcome in ("conflict", "missing")]

    @property
    def has_writes(self) -> bool:
        return any(f.content is not None for f in self.files)


def _classify_managed(disk: str | None, base: str | None, expected: str) -> str:
    if disk is None:
        return "missing"
    if disk == expected:
        return "unchanged"
    if base is not None and disk == base:
        return "upgrade"
    return "conflict"


def plan_install(
    workspace: Path,
    mode: str,
    name: str,
    descriptor_rel: str,
    manifest: dict | None,
) -> InstallReport:
    registry = file_registry(mode, name, descriptor_rel)
    records = _manifest_files(manifest)
    report = InstallReport(mode=mode, product_name=name, status="current", descriptor_path=descriptor_rel)
    for entry in registry:
        rendered = render_entry(entry, mode, name)
        expected = _sha256_text(rendered)
        authority = entry_authority_hash(entry, mode, name)
        record = records.get(entry.path)
        base = record.get("base_hash") if record else None
        target = safe_target(workspace, entry.path)
        disk = _sha256_file(target) if target.is_file() else None
        if entry.ownership == USER:
            if disk is None:
                outcome, content = "create", rendered
            elif entry.normative and base is not None and disk != base:
                # chainlink #78: a normative user-owned document whose bytes
                # differ from the reviewed (manifest-recorded) content is
                # DRIFTED -- reported, never silently overwritten and never
                # silently re-based. `accept-policy` is the only path that
                # moves the recorded base.
                outcome, content = "drifted", None
            else:
                outcome, content = "user-owned", None
            report.files.append(
                FilePlan(
                    entry.path, USER, outcome, content, base or (expected if content else disk), expected,
                    reason="user-owned template; never overwritten", normative=entry.normative,
                )
            )
            continue
        if disk is None:
            outcome, content = "create", rendered
        elif disk == expected:
            outcome, content = "unchanged", None
        elif base is not None and disk == base:
            outcome, content = "upgrade", rendered
        else:
            outcome, content = "conflict", None
        report.files.append(FilePlan(entry.path, MANAGED, outcome, content, base, expected, authority))
    known = {entry.path for entry in registry}
    for path, record in records.items():
        if record.get("ownership") == MANAGED and path not in known:
            report.obsolete.append(path)
    report.authority_state = _authority_verdict(workspace, manifest, _skill_plan(report))
    report.status = _overall_status(report)
    return report


def _skill_plan(report: InstallReport) -> FilePlan | None:
    """The skill file's inventory plan, or None when the report does not
    carry one (a not-initialized or incompatible workspace)."""
    return next((f for f in report.files if f.path == SKILL_RELATIVE_PATH), None)


def _authority_verdict(workspace: Path, manifest: dict | None, plan: FilePlan | None) -> str | None:
    """The `skill authority hash` attestation doctor prints (chainlink #75):
    `verified`, `drifted`, or `missing` -- derived from the manifest's
    recorded `authority_hash` for the skill file, never from the file's own
    self-consistency alone.

    * `verified` -- the on-disk skill file is byte-identical to the installed
      one, and its authority region's hash matches the record.
    * `drifted` -- the file was modified after install (the inventory's
      `conflict` outcome -- the edit may have landed OUTSIDE the authority
      region, where the old declared-vs-computed self-consistency check
      could not see it and attested a tampered file as verified on the line
      directly above that file's CONFLICT line), or the region's hash no
      longer matches the record.
    * `missing` -- the skill file is absent from the workspace.
    * None -- no manifest to attest against; no line is printed.

    The attestation attests the file listed directly below it, so `verified`
    and that file's CONFLICT can never both be true."""
    target = workspace / SKILL_RELATIVE_PATH
    if not target.is_file():
        return "missing"
    if plan is not None and plan.outcome == "conflict":
        return "drifted"
    try:
        text = target.read_text()
        current = computed_authority_hash(text)
    except (InstallError, OSError, UnicodeDecodeError):
        return "drifted"
    recorded = None
    if manifest is not None:
        record = _manifest_files(manifest).get(SKILL_RELATIVE_PATH)
        if isinstance(record, dict):
            recorded = record.get("authority_hash")
    if recorded is not None:
        return "verified" if recorded == current else "drifted"
    # A manifest that records no authority hash for the skill (hand-written
    # or predating the field): fall back to the file's own self-consistency,
    # the only attestation the older record supports.
    try:
        return "verified" if declared_authority_hash(text) == current else "drifted"
    except InstallError:
        return "drifted"


def _overall_status(report: InstallReport) -> str:
    managed = [f for f in report.files if f.ownership == MANAGED]
    if any(f.outcome == "conflict" for f in managed):
        return "conflict"
    if report.authority_state == "drifted":
        return "conflict"
    if any(f.outcome == "missing" for f in managed):
        return "drifted"
    # chainlink #78: a normative user-owned document (the reliance policy)
    # that is missing or drifted from its reviewed content makes the whole
    # installation `drifted` -- `installation: current` is not evidence that
    # any user-owned file is the file that was reviewed.
    if any(f.outcome in ("missing", "drifted") for f in report.files if f.ownership == USER and f.normative):
        return "drifted"
    if any(f.outcome == "upgrade" for f in managed):
        return "upgradable"
    return "current"


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------
def apply_plan(
    workspace: Path,
    report: InstallReport,
    manifest: dict | None,
    *,
    adjudicator_record: dict | None = None,
    installed_product_version: str | None = None,
    installed_schema_versions: dict | None = None,
) -> dict:
    """Write every pending create/upgrade, then rebuild and atomically
    write the ownership manifest. Never writes a conflict.

    `adjudicator_record` is the identity to pin; the default (`None`) is
    the running executable. `init` passes the previously recorded record
    when a rerun's binary has changed, so the conflict is reported without
    rewriting the trusted pin (chainlink #65); the explicit recovery paths
    (`migrate --upgrade`/`--force`) leave it as the running identity.

    `installed_product_version`/`installed_schema_versions` are the version
    values to record; the default (`None`) is the running binary's. On the
    same conflict path `init` passes the previously recorded values, so a
    refused rerun cannot report a version its preserved pin does not
    correspond to (chainlink #68)."""
    records = dict(_manifest_files(manifest))
    for plan in report.files:
        if plan.content is not None:
            _write(workspace, plan.path, plan.content)
    files = []
    for plan in report.files:
        target = workspace / plan.path
        disk = _sha256_file(target) if target.is_file() else None
        if plan.outcome == "conflict":
            base = plan.base_hash
        elif plan.ownership == USER and plan.normative and plan.base_hash is not None:
            # chainlink #78: a normative user-owned document's recorded base
            # moves ONLY through the explicit accept path (`ligature
            # accept-policy`). Re-basing it to whatever is on disk here would
            # silently clear the drift signal the manifest exists to keep --
            # the same silent-acceptance defect, one layer down. (A legacy
            # manifest with no recorded base still adopts the on-disk content
            # once, so a pre-#78 workspace becomes drift-checkable rather
            # than permanently unverifiable.)
            base = plan.base_hash
        else:
            base = disk
        files.append(
            {
                "path": plan.path,
                "ownership": plan.ownership,
                "base_hash": base,
                "expected_hash": plan.expected_hash,
                "authority_hash": plan.authority_hash,
            }
        )
    for path in report.obsolete:
        record = records.get(path, {})
        files.append(
            {
                "path": path,
                "ownership": MANAGED,
                "base_hash": record.get("base_hash"),
                "expected_hash": record.get("expected_hash"),
                "obsolete": True,
            }
        )
    if adjudicator_record is None:
        adjudicator_record = running_adjudicator_record()
    adjudicator_pin = adjudicator_record.get("content_hash") or UNATTESTED
    gate_hashes = {}
    for rel in _GATE_PINNED_PATHS:
        if rel == ADJUDICATOR_PIN_TOKEN:
            gate_hashes[rel] = adjudicator_pin
            continue
        target = workspace / rel
        if target.is_file():
            gate_hashes[rel] = _sha256_file(target)
    if installed_product_version is None:
        installed_product_version = PRODUCT_VERSION
    if installed_schema_versions is None:
        installed_schema_versions = {**KNOWN_SCHEMA_VERSIONS, "skill": SKILL_VERSION}
    new_manifest = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "product_version": installed_product_version,
        "installed_product_version": installed_product_version,
        "installed_skill_version": SKILL_VERSION,
        "installed_schema_versions": installed_schema_versions,
        "mode": report.mode,
        "project_name": report.product_name,
        "descriptor_path": report.descriptor_path,
        "files": files,
        "gate_hashes": gate_hashes,
        "adjudicator": adjudicator_record,
    }
    _write_manifest(workspace, new_manifest)
    return new_manifest


def _describe_adjudicator_hash(content_hash: str | None) -> str:
    return content_hash if content_hash else f"{UNATTESTED} (source checkout)"


def adjudicator_pin_conflict(manifest: dict | None) -> str | None:
    """The conflict message for a rerun whose running executable identity
    differs from the one recorded at init, or `None` when there is nothing
    to refuse.

    A pre-#57 manifest records no adjudicator at all: there is no trusted
    value to preserve, so recording one now strengthens the pin rather than
    silently replacing it. A recorded identity equal to the running one --
    including both being an unattested source checkout, where both hashes
    are `None` -- is the ordinary idempotent rerun. Every other case
    (packaged A -> packaged B, packaged -> source checkout, or source
    checkout -> packaged) changes a value that `doctor`/`status`/`check`
    hold the workspace to, so `init` must report a conflict and leave the
    pin alone rather than rewrite it; `migrate --upgrade`/`--force` is the
    deliberate, explicit re-pin path (chainlink #65)."""
    if manifest is None:
        return None
    recorded = manifest.get("adjudicator")
    if not isinstance(recorded, dict):
        return None
    recorded_hash = recorded.get("content_hash")
    running_hash = adjudicator.identity_hash()
    if recorded_hash == running_hash:
        return None
    return (
        "adjudicator pin mismatch: the ownership manifest records "
        f"{_describe_adjudicator_hash(recorded_hash)} but this executable is "
        f"{_describe_adjudicator_hash(running_hash)} -- refusing to silently re-pin it. "
        "If the binary swap is deliberate, re-pin explicitly with "
        "`ligature migrate --upgrade` (or `migrate --force <path>`)"
    )


def init_workspace(workspace: Path, mode: str, name: str, descriptor_rel: str) -> InstallReport:
    if not workspace.is_dir():
        raise InstallError(f"target workspace does not exist: {workspace}")
    if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
        raise InstallError(
            f"invalid project name {name!r}; must match ^[a-z][a-z0-9-]*$ "
            "(the descriptor schema's own project.name pattern)"
        )
    if mode not in ("greenfield", "port"):
        raise InstallError(f"unknown init mode {mode!r}; expected 'greenfield' or 'port'")
    manifest = load_manifest(workspace)
    if manifest is not None:
        _require_compatible(manifest)
    report = plan_install(workspace, mode, name, descriptor_rel, manifest)
    conflict = adjudicator_pin_conflict(manifest)
    if conflict is None:
        apply_plan(workspace, report, manifest)
        # The attestation describes the install just applied, not the
        # pre-write disk: a fresh init's skill file does not exist until
        # apply_plan writes it, and a refused upgrade must not be attested
        # against bytes the product no longer ships (chainlink #75).
        report.authority_state = _authority_verdict(
            workspace, load_manifest(workspace), _skill_plan(report)
        )
        report.status = _overall_status(report)
        return report
    # A rerun under a different executable: the whole install is frozen, not
    # just the adjudicator pin (#68). Managed-file "upgrade" content and the
    # recorded product/schema versions all describe what the *refused* binary
    # would have installed; writing any of them would leave the workspace's
    # content and version claims ahead of the pin it still trusts. Refuse them
    # together, exactly as #65 refuses the pin, until `migrate --upgrade`
    # (or `--force`) is the operator's explicit choice. "create" outcomes
    # still proceed (a genuinely new file has nothing to refuse), and
    # "unchanged"/"user-owned" are untouched as always.
    report.status = "conflict"
    report.messages.append(conflict)
    for plan in report.files:
        if plan.outcome == "upgrade":
            plan.content = None
    recorded_product, recorded_schemas = _recorded_install_versions(manifest)
    apply_plan(
        workspace,
        report,
        manifest,
        adjudicator_record=manifest.get("adjudicator"),
        installed_product_version=recorded_product,
        installed_schema_versions=recorded_schemas,
    )
    # Same post-write attestation as the normal path above; the forced
    # `conflict` status is left as set -- the pin refusal is the headline
    # there, not the skill file's own state.
    report.authority_state = _authority_verdict(
        workspace, load_manifest(workspace), _skill_plan(report)
    )
    return report


def _recorded_install_versions(manifest: dict) -> tuple[str, dict]:
    """The product/schema versions an already-initialized workspace records,
    so a refused (`init` conflict-path) rerun preserves them rather than
    adopting the running, untrusted binary's (chainlink #68). Falls back to
    the running values only if the manifest genuinely predates the fields."""
    product = manifest.get("installed_product_version") or manifest.get("product_version")
    if not isinstance(product, str) or not product:
        product = PRODUCT_VERSION
    schemas = manifest.get("installed_schema_versions")
    if not isinstance(schemas, dict) or not schemas:
        schemas = {**KNOWN_SCHEMA_VERSIONS, "skill": SKILL_VERSION}
    return product, schemas


# ---------------------------------------------------------------------------
# Inspection (doctor / migrate --status)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DescriptorSchemaReport:
    """The workspace's user-owned project descriptor validated against the
    product's own `schemas/project-descriptor.schema.json` -- the same schema
    and validator `check`/`status` read (chainlink #74).

    States: `valid` (parses as a JSON object and satisfies the schema),
    `invalid` (parses but violates it -- `diagnostics` carries one
    actionable line per violation, or the file is not valid JSON / not a
    JSON object), `absent` (no file at the manifest's descriptor path),
    `unreadable` (the file exists but cannot be read). `check` reports
    `invalid_input` for every state except `valid`, so those are exactly
    the states `doctor` fails closed on."""

    state: str
    diagnostics: list[str] = field(default_factory=list)


def descriptor_schema_report(workspace: Path, descriptor_rel: str) -> DescriptorSchemaReport:
    """Read-only schema check of the user-owned descriptor. Never repairs
    and never writes: the descriptor is user-owned, `migrate --force`
    refuses it, and `check` remains the detail surface for the offending
    property -- this only decides whether the workspace is fail-closed."""
    try:
        target = safe_target(workspace, descriptor_rel)
    except InstallError as exc:
        return DescriptorSchemaReport("unreadable", [str(exc)])
    if not target.is_file():
        return DescriptorSchemaReport("absent")
    try:
        text = target.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        return DescriptorSchemaReport("unreadable", [str(exc)])
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return DescriptorSchemaReport("invalid", [f"not valid JSON: {exc}"])
    if not isinstance(data, dict):
        return DescriptorSchemaReport("invalid", ["descriptor is not a JSON object"])
    diagnostics = schema_diagnostics(data)
    if diagnostics:
        return DescriptorSchemaReport("invalid", diagnostics)
    return DescriptorSchemaReport("valid")


def _require_compatible(manifest: dict) -> None:
    version = manifest.get("manifest_schema_version")
    if version != MANIFEST_SCHEMA_VERSION:
        raise InstallError(
            f"installation manifest schema {version!r} is incompatible with this product "
            f"({MANIFEST_SCHEMA_VERSION}); run a matching `ligature migrate`"
        )


def inspect(workspace: Path) -> InstallReport:
    manifest = load_manifest(workspace)
    if manifest is None:
        return InstallReport(mode="", product_name="", status="not-initialized")
    if manifest.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION:
        report = InstallReport(
            mode=str(manifest.get("mode", "")),
            product_name=str(manifest.get("project_name", "")),
            status="incompatible",
        )
        report.messages.append(
            f"manifest schema {manifest.get('manifest_schema_version')!r} != {MANIFEST_SCHEMA_VERSION}"
        )
        return report
    mode = str(manifest.get("mode", ""))
    name = str(manifest.get("project_name", ""))
    descriptor_rel = _manifest_descriptor_rel(manifest)
    registry = file_registry(mode, name, descriptor_rel)
    records = _manifest_files(manifest)
    report = InstallReport(mode=mode, product_name=name, status="current", descriptor_path=descriptor_rel)
    for entry in registry:
        rendered = render_entry(entry, mode, name)
        expected = _sha256_text(rendered)
        record = records.get(entry.path, {})
        base = record.get("base_hash")
        target = safe_target(workspace, entry.path)
        disk = _sha256_file(target) if target.is_file() else None
        if entry.ownership == USER:
            if disk is None:
                outcome = "missing"
            elif entry.normative and base is not None and disk != base:
                outcome = "drifted"
            else:
                outcome = "user-owned"
            report.files.append(FilePlan(entry.path, USER, outcome, None, base, expected, normative=entry.normative))
            continue
        outcome = _classify_managed(disk, base, expected)
        report.files.append(
            FilePlan(entry.path, MANAGED, outcome, None, base, expected, entry_authority_hash(entry, mode, name))
        )
    known = {entry.path for entry in registry}
    for path, record in records.items():
        if record.get("ownership") == MANAGED and path not in known:
            report.obsolete.append(path)
    report.authority_state = _authority_verdict(workspace, manifest, _skill_plan(report))
    report.descriptor_schema = descriptor_schema_report(workspace, descriptor_rel)
    report.status = _overall_status(report)
    return report


# ---------------------------------------------------------------------------
# Migration / recovery
# ---------------------------------------------------------------------------
def migrate(
    workspace: Path,
    *,
    upgrade: bool = False,
    prune: bool = False,
    force: tuple[str, ...] = (),
) -> InstallReport:
    manifest = load_manifest(workspace)
    if manifest is None:
        raise InstallError("workspace is not initialized; run `ligature init --mode <mode>` first")
    _require_compatible(manifest)
    mode = str(manifest.get("mode", ""))
    name = str(manifest.get("project_name", ""))
    descriptor_rel = _manifest_descriptor_rel(manifest)
    messages: list[str] = []

    if upgrade or force:
        registry = {entry.path: entry for entry in file_registry(mode, name, descriptor_rel)}
        if upgrade:
            plan = plan_install(workspace, mode, name, descriptor_rel, manifest)
            for file_plan in plan.files:
                if file_plan.outcome in ("create", "upgrade") and file_plan.content is not None:
                    _write(workspace, file_plan.path, file_plan.content)
        for path in force:
            entry = registry.get(path)
            if entry is None or entry.ownership != MANAGED:
                raise InstallError(f"--force names a path that is not a managed file: {path}")
            _write(workspace, path, render_entry(entry, mode, name))
        # Recompute against the now-current disk and rewrite the manifest
        # (writes nothing further: every touched file is now `unchanged`).
        apply_plan(workspace, plan_install(workspace, mode, name, descriptor_rel, load_manifest(workspace)), manifest)

    if prune:
        manifest = load_manifest(workspace) or manifest
        registry_paths = {entry.path for entry in file_registry(mode, name, descriptor_rel)}
        records = _manifest_files(manifest)
        remaining = []
        for path, record in records.items():
            if record.get("ownership") == MANAGED and path not in registry_paths:
                try:
                    target = safe_target(workspace, path)
                except InstallError as exc:
                    messages.append(f"refusing to prune unsafe manifest path {path!r}: {exc}")
                    remaining.append(record)
                    continue
                if target.is_file() and record.get("base_hash") not in (None, _sha256_file(target)):
                    messages.append(f"obsolete but locally modified; not pruned: {path}")
                    remaining.append(record)
                    continue
                if target.is_file():
                    target.unlink()
                continue
            remaining.append(record)
        manifest = dict(manifest)
        manifest["files"] = remaining
        _write_manifest(workspace, manifest)

    report = inspect(workspace)
    report.messages = messages + report.messages
    return report


def _manifest_descriptor_rel(manifest: dict) -> str:
    explicit = manifest.get("descriptor_path")
    if isinstance(explicit, str) and explicit:
        return explicit
    for path in _manifest_files(manifest):
        if path.endswith("project-descriptor.json"):
            return path
    return "project-descriptor.json"


# ---------------------------------------------------------------------------
# Normative user-owned document drift + acceptance (chainlink #78)
# ---------------------------------------------------------------------------
def normative_drift_records(workspace: Path) -> list[dict]:
    """Per-file drift for normative user-owned documents (chainlink #78):
    one record per normative registry entry (today: the reliance policy)
    whose on-disk content is missing from the workspace or differs from the
    manifest's recorded `base_hash` -- the reviewed content.

    Each record is `{"path", "state" ("missing"|"drifted"),
    "recorded_hash", "current_hash"}`. Empty when the workspace is not
    initialized, the manifest is incompatible, or nothing has drifted. An
    unreadable manifest yields no records rather than an exception, so
    `check`/`status` stay honest instead of crashing; `doctor` remains the
    detail surface for a corrupt manifest."""
    try:
        manifest = load_manifest(workspace)
    except InstallError:
        return []
    if manifest is None:
        return []
    if manifest.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION:
        return []
    try:
        mode = str(manifest.get("mode", ""))
        name = str(manifest.get("project_name", ""))
        descriptor_rel = _manifest_descriptor_rel(manifest)
        registry = file_registry(mode, name, descriptor_rel)
    except InstallError:
        return []
    records = _manifest_files(manifest)
    drift: list[dict] = []
    for entry in registry:
        if not entry.normative:
            continue
        record = records.get(entry.path, {})
        base = record.get("base_hash")
        try:
            target = safe_target(workspace, entry.path)
        except InstallError:
            continue
        disk = _sha256_file(target) if target.is_file() else None
        if disk is None:
            drift.append({"path": entry.path, "state": "missing", "recorded_hash": base, "current_hash": None})
        elif base is not None and disk != base:
            drift.append({"path": entry.path, "state": "drifted", "recorded_hash": base, "current_hash": disk})
    return drift


def _declared_policy_path(workspace: Path, manifest: dict) -> str | None:
    """The descriptor's `compatibility_policy.reliance_policy_path`, or
    None when the descriptor is absent, unreadable, schema-invalid, or
    declares no usable value. Read-only: the descriptor is user-owned and
    never repaired here (chainlink #78 -- the field is the project's own
    declaration of where its policy lives, and the accept commands default
    to it rather than second-guessing it)."""
    descriptor_rel = _manifest_descriptor_rel(manifest)
    try:
        target = safe_target(workspace, descriptor_rel)
    except InstallError:
        return None
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    compat = data.get("compatibility_policy")
    if not isinstance(compat, dict):
        return None
    value = compat.get("reliance_policy_path")
    if isinstance(value, str) and value:
        return value
    return None


def accept_policy(
    workspace: Path,
    reviewer: str,
    policy_path: str | None = None,
) -> dict:
    """`ligature accept-policy` (chainlink #78): the documented accept path
    for an intentional governance change, analogous to `accept-promotion`.

    Records the current on-disk content of a normative user-owned document
    (the reliance policy) as its reviewed `base_hash` in the ownership
    manifest, so a reviewed edit stops reading as drift. The recorded base
    moves through this function and nothing else -- `init`/`migrate` never
    re-base a normative user-owned file to whatever is on disk (see
    `apply_plan`), which is what makes the drift signal trustworthy.

    `policy_path` defaults to the descriptor's
    `compatibility_policy.reliance_policy_path`; an explicit path that
    disagrees with the declaration is refused, so a workspace cannot
    quietly promote against a different policy document than the one it
    declares. The document must carry exactly one
    `Policy version: <name>@<major>.<minor>` marker line -- the same
    convention `accept-promotion` reads -- so the recorded hash always
    corresponds to a policy that can yield a policy_version.

    Refuses (InstallError) before touching the manifest when: the workspace
    is not initialized; no path can be resolved; the path is not a
    manifest-recorded, registry-normative, user-owned file; the file is
    missing; the marker line is absent/ambiguous; or the reviewer is empty.
    """
    if not reviewer:
        raise InstallError("accept-policy requires a non-empty reviewer -- no default, no LLM-supplied value")
    manifest = load_manifest(workspace)
    if manifest is None:
        raise InstallError("workspace is not initialized; run `ligature init --mode <mode>` first")
    _require_compatible(manifest)
    declared = _declared_policy_path(workspace, manifest)
    if policy_path is None:
        if declared is None:
            raise InstallError(
                "no --policy-path given and the project descriptor declares no "
                "compatibility_policy.reliance_policy_path to default to"
            )
        policy_path = declared
    elif declared is not None and policy_path != declared:
        raise InstallError(
            f"--policy-path {policy_path!r} disagrees with the project descriptor's "
            f"compatibility_policy.reliance_policy_path {declared!r}"
        )
    record = _manifest_files(manifest).get(policy_path)
    if record is None:
        raise InstallError(
            f"accept-policy names a path the ownership manifest does not record: {policy_path!r} "
            "(the manifest records what `ligature init` installed; a policy file it does not "
            "record was never installed by the product)"
        )
    if record.get("ownership") != USER:
        raise InstallError(f"accept-policy names a path that is not user-owned: {policy_path!r}")
    mode = str(manifest.get("mode", ""))
    name = str(manifest.get("project_name", ""))
    registry = file_registry(mode, name, _manifest_descriptor_rel(manifest))
    entry = next((e for e in registry if e.path == policy_path), None)
    if entry is None or not entry.normative:
        raise InstallError(
            f"accept-policy names a path that is not a normative policy document: {policy_path!r}"
        )
    target = safe_target(workspace, policy_path)
    if not target.is_file():
        raise InstallError(f"policy document is missing from the workspace: {policy_path}")
    try:
        text = target.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        raise InstallError(f"policy document {policy_path!r} is unreadable: {exc}") from exc
    matches = POLICY_VERSION_MARKER_RE.findall(text)
    if len(matches) != 1:
        raise InstallError(
            f"{policy_path!r} must have exactly one 'Policy version: <name>@<major>.<minor>' marker "
            f"line (docs/reliance-policy.template.md's own convention) -- found {len(matches)}"
        )
    previous = record.get("base_hash")
    accepted = _sha256_file(target)
    record["base_hash"] = accepted
    _write_manifest(workspace, manifest)
    return {
        "path": policy_path,
        "reviewer": reviewer,
        "policy_version": matches[0],
        "previous_hash": previous,
        "accepted_hash": accepted,
    }


# ---------------------------------------------------------------------------
# Executable (adjudicator) trust pin
# ---------------------------------------------------------------------------
def running_adjudicator_record() -> dict:
    """The identity `init`/`migrate` record in the ownership manifest, so a
    later run can verify the executable that installed this workspace."""
    info = adjudicator.current_identity()
    return {
        "kind": info["kind"],
        "version": info["version"],
        "content_hash": info["content_hash"],
        "required_python": info["required_python"],
        "python": info["python"],
        "platform": info["platform"],
    }


def inspect_adjudicator_pin(workspace: Path) -> dict:
    """Compare the running executable's identity against the one recorded
    when the workspace was initialized. Read-only; never repairs.

    States: `not-installed` (no manifest), `unpinned` (manifest predates
    #57 and records no adjudicator), `pinned` (running identity matches the
    recorded one), `unattested` (a packaged pin is present but the runner is
    an unattested source checkout, or vice versa), `mismatch` (both are
    packaged builds but the content hashes differ -- the executable was
    replaced). `doctor` fails closed on `unattested`/`mismatch`.
    """
    manifest = load_manifest(workspace)
    if manifest is None:
        return {"state": "not-installed", "details": "no installation manifest"}
    recorded = manifest.get("adjudicator")
    if not isinstance(recorded, dict):
        return {
            "state": "unpinned",
            "details": "manifest records no adjudicator identity (pre-#57 install)",
        }
    running = adjudicator.current_identity()
    recorded_hash = recorded.get("content_hash")
    running_hash = running.get("content_hash")
    if recorded_hash is None:
        if running["kind"] == "source-checkout" and recorded.get("version") == adjudicator.PRODUCT_VERSION:
            return {
                "state": "pinned",
                "details": "workspace initialized by an unattested source checkout (no build hash to pin)",
            }
        return {
            "state": "unattested",
            "details": "workspace pinned to an unattested source checkout; running adjudicator differs",
        }
    if running["kind"] != "zipapp" or running_hash is None:
        return {
            "state": "unattested",
            "details": "workspace pins a packaged adjudicator; running executable is an unattested source checkout",
        }
    if running_hash != recorded_hash:
        return {
            "state": "mismatch",
            "details": f"recorded {recorded_hash}, running {running_hash}",
        }
    return {"state": "pinned", "details": "running executable matches the recorded adjudicator"}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
_OUTCOME_GLYPH = {
    "create": "create",
    "unchanged": "unchanged",
    "upgrade": "upgrade",
    "conflict": "CONFLICT",
    "user-owned": "user-owned",
    "drifted": "DRIFTED",
    "missing": "MISSING",
    "obsolete": "obsolete",
}


def _descriptor_schema_note(schema: DescriptorSchemaReport) -> str:
    """The schema-state annotation on the descriptor's inventory line
    (chainlink #74). `doctor` inventories `project-descriptor.json` as
    user-owned, so its schema state belongs on that line: a workspace
    `check` treats as fail-closed `invalid_input` must never be reported as
    a healthy, current installation. `check` stays the detail surface -- the
    note points there rather than duplicating its per-property diagnostics,
    and the descriptor is user-owned, so nothing here repairs it."""
    if schema.state == "valid":
        return "  schema: valid"
    if schema.state == "invalid":
        return "  schema: invalid (see check for detail)"
    if schema.state == "absent":
        return "  schema: absent"
    return "  schema: unreadable"


def render_report_text(report: InstallReport) -> str:
    lines = [f"installation: {report.status}"]
    if report.mode:
        lines.append(f"mode: {report.mode}  project: {report.product_name}")
    if report.authority_state is not None:
        lines.append(f"skill authority hash: {report.authority_state}")
    for plan in sorted(report.files, key=lambda p: p.path):
        line = f"  {_OUTCOME_GLYPH.get(plan.outcome, plan.outcome):>9}  {plan.path}"
        if plan.path == report.descriptor_path and report.descriptor_schema is not None:
            line += _descriptor_schema_note(report.descriptor_schema)
        elif plan.outcome == "drifted":
            line += "  (unreviewed change; record it with `ligature accept-policy --reviewer <name>`)"
        lines.append(line)
    for path in sorted(report.obsolete):
        lines.append(f"  {'obsolete':>9}  {path}")
    for message in report.messages:
        lines.append(f"  note: {message}")
    return "\n".join(lines) + "\n"


def default_project_name(workspace: Path) -> str:
    raw = workspace.resolve().name.lower()
    slug = re.sub(r"[^a-z0-9-]+", "-", raw).strip("-")
    slug = re.sub(r"-+", "-", slug)
    if not slug or not slug[0].isalpha():
        slug = f"ligature-{slug}" if slug else "ligature-project"
    return slug
