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

A normative user-owned document's own `Policy version:` marker is part of
that inventory (chainlink #113): the reliance policy ships as a template
whose marker line is a placeholder no promotion receipt can be computed
from, so `init`/`doctor`/`migrate` report it on the document's own line
and `accept-policy --version` is the sanctioned way to stamp it.

`inspect()` takes the global `--descriptor` flag as an optional keyword.
When the operator supplies one, it is the descriptor whose schema state
gates `doctor` -- not the one `ci/manifest/installation.json` recorded
(chainlink #109) -- and the inventory still reports what `init`
installed. `None` means the flag was not supplied, which keeps the
manifest's own `descriptor_path` in force for `migrate`.

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
import draft_templates  # noqa: E402
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
#
# The value pattern itself is factored out (chainlink #113) so the
# `--version` accept-policy stamps cannot drift from what the marker line
# accepts: one pattern, two consumers.
POLICY_VERSION_VALUE_RE = re.compile(r"[a-z][a-z0-9-]*@[0-9]+(?:\.[0-9]+)*")
POLICY_VERSION_MARKER_RE = re.compile(
    rf"^Policy version:\s*`?({POLICY_VERSION_VALUE_RE.pattern})`?\s*$", re.MULTILINE
)

# The shape of a `Policy version:` marker line, whatever value it carries:
# the words, then at most ONE value token (backticked or bare). This is the
# line `accept-policy --version` rewrites, and the line the exactly-one
# rule counts -- deliberately narrower than "any line starting with those
# words", so a sentence that merely begins `Policy version: see the change
# history` in a policy's body is neither stamped nor read as a second
# declaration. #78's acceptance rule stays exactly as it was: the marker
# regex above, and nothing else.
POLICY_VERSION_CANDIDATE_RE = re.compile(
    r"^[ \t]*Policy version:[ \t]*(?:`[^`\n]*`|[^`\s]+)?[ \t]*$", re.MULTILINE
)

# The marker states, in the vocabulary `policy_version_state` returns.
POLICY_STAMPED = "stamped"
POLICY_UNFILLED = "unfilled"
POLICY_ABSENT = "absent"
POLICY_AMBIGUOUS = "ambiguous"

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

# chainlink #105: derived from the one draft-template registry
# (`draft_templates.DRAFT_TEMPLATES`) rather than hand-listed here. This
# tuple was the third copy of "which templates exist" -- `draft --help`
# named two of them (chainlink #79's fix shipped nine and listed two) and
# this list had already stopped tracking `prompts/`: #98's
# stage-3-closure-profile-drafting.md and stage-3-degradation-drafting.md
# shipped without being copied into an initialized workspace's
# `.ligature/prompts/`, while `draft` on a packaged binary still used
# them. One registry, three surfaces (`draft --help`, `draft`'s
# unrecognized-name refusal, and this install registry), none of which
# can now name a template the others do not.
_PROMPTS = draft_templates.prompt_filenames()


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
    descriptor_override: DescriptorInForce | None = None
    # chainlink #113: the normative user-owned documents whose own
    # `Policy version:` marker is not stamped (`policy_markers`), the state
    # `init` installs the reliance policy in. Reported on the document's own
    # inventory line and made a blocking condition by `doctor`, because a
    # workspace whose policy cannot yield a `policy_version` cannot mint a
    # Stage 4.5 promotion receipt at all -- and until `accept-policy
    # --version` existed, the only way out of it was a hand-edit of a
    # `protected_root`. It does NOT change `status`: an unfilled template is
    # not installation drift (nothing conflicted, nothing was modified), so
    # `init` still exits 0 and `installation: current` still means exactly
    # what it meant before.
    policy_markers: list[dict] = field(default_factory=list)
    # chainlink #117: the machine-readable upgrade audit (old/new version,
    # changed paths, hashes, required human action). Always populated by
    # `migrate`; `None` for `init`/`inspect` reports.
    audit: dict | None = None

    def marker_record(self, path: str) -> dict | None:
        """The unfilled-marker record for `path`, or None when the document
        is stamped (or absent from the report)."""
        return next((record for record in self.policy_markers if record["path"] == path), None)

    @property
    def descriptor_gate(self) -> DescriptorSchemaReport | None:
        """The descriptor whose schema state decides whether this workspace
        is fail-closed: the one `--descriptor` named when it overrides the
        installed descriptor (chainlink #109), else the installed
        descriptor's own. `doctor` reads this rather than
        `descriptor_schema` directly so the gate can never be evaluated
        against a file the caller did not ask about."""
        if self.descriptor_override is not None:
            return self.descriptor_override.schema
        return self.descriptor_schema

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
    pending: dict[str, str] = {}
    for entry in registry:
        rendered = render_entry(entry, mode, name)
        expected = _sha256_text(rendered)
        authority = entry_authority_hash(entry, mode, name)
        record = records.get(entry.path)
        base = record.get("base_hash") if record else None
        target = safe_target(workspace, entry.path)
        disk = _sha256_file(target) if target.is_file() else None
        if disk is None:
            # Not on disk yet: the content this plan is about to write is
            # what `init` will install, and the version-marker records
            # below must report the placeholder that content carries
            # (chainlink #113) rather than saying nothing until the next
            # run.
            pending[entry.path] = rendered
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
    report.policy_markers = _unfilled_marker_records(workspace, registry, pending)
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
    # chainlink #117: a locally modified managed file is a conflict, not a
    # new trusted truth. Its previously recorded gate pin survives the
    # upgrade untouched, so `status`/`check`/`write-set-check` keep
    # reporting the drift `doctor` already reports -- instead of the
    # tampered bytes being adopted as the new pin.
    conflicted_paths = {plan.path for plan in report.files if plan.outcome == "conflict"}
    old_gate_hashes = manifest.get("gate_hashes") if isinstance(manifest, dict) else None
    if not isinstance(old_gate_hashes, dict):
        old_gate_hashes = {}
    for rel in _GATE_PINNED_PATHS:
        if rel == ADJUDICATOR_PIN_TOKEN:
            gate_hashes[rel] = adjudicator_pin
            continue
        if rel in conflicted_paths:
            if rel in old_gate_hashes:
                gate_hashes[rel] = old_gate_hashes[rel]
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
    """A project descriptor validated against the product's own
    `schemas/project-descriptor.schema.json` -- the same schema and
    validator `check`/`status` read (chainlink #74).

    States: `valid` (parses as a JSON object and satisfies the schema),
    `invalid` (parses but violates it -- `diagnostics` carries one
    actionable line per violation, or the file is not valid JSON / not a
    JSON object), `absent` (no file at the resolved descriptor path),
    `unreadable` (the file exists but cannot be read). `check` reports
    `invalid_input` for every state except `valid`, so those are exactly
    the states `doctor` fails closed on."""

    state: str
    diagnostics: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DescriptorInForce:
    """The descriptor the global `--descriptor` flag names, when it is not
    the descriptor the installation manifest recorded (chainlink #109).

    `doctor` resolved its descriptor from `ci/manifest/installation.json`
    and ignored the flag, so `--descriptor <invalid>.json doctor` printed
    `schema: valid` about a file it never opened -- a clean verdict from
    the command an operator is most likely to reach for as a pre-flight
    gate, while `status`, `check` and `write-set-check` all failed closed
    on that same file. The four commands could not be made to agree about
    which descriptor was in force.

    `installed` is the descriptor the manifest recorded and the inventory
    still lists, or `None` on a workspace that was never initialized --
    `--descriptor` selects the descriptor doctor *validates*, never the one
    `init` installed, so neither report is silently substituted for the
    other."""

    path: str
    schema: DescriptorSchemaReport
    installed: str | None = None


def descriptor_schema_report_at(target: Path) -> DescriptorSchemaReport:
    """The read-only schema check against an already-resolved path.

    Deliberately not `safe_target`: that resolution exists to keep a
    *write* inside the workspace and off a symlink, while this check only
    reads. `--descriptor` names the file every other command reads
    wherever it lives -- `status`/`check` accept one outside the workspace
    -- so doctor has to validate that same file (chainlink #109) rather
    than a workspace-relative stand-in for it."""
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


def descriptor_schema_report(workspace: Path, descriptor_rel: str) -> DescriptorSchemaReport:
    """Read-only schema check of the user-owned descriptor the manifest
    recorded. Never repairs and never writes: the descriptor is
    user-owned, `migrate --force` refuses it, and `check` remains the
    detail surface for the offending property -- this only decides
    whether the workspace is fail-closed."""
    try:
        target = safe_target(workspace, descriptor_rel)
    except InstallError as exc:
        return DescriptorSchemaReport("unreadable", [str(exc)])
    return descriptor_schema_report_at(target)


def _display_descriptor(workspace: Path, descriptor: Path) -> str:
    """How `--descriptor` is echoed in a report: workspace-relative when it
    lives inside the workspace, verbatim otherwise -- the same shape
    `status` and `write-set-check` report it in."""
    if not descriptor.is_absolute():
        return descriptor.as_posix()
    try:
        return descriptor.resolve().relative_to(Path(workspace).resolve()).as_posix()
    except (ValueError, OSError):
        return descriptor.as_posix()


def _same_file(left: Path, right: Path) -> bool:
    """Whether two paths name one file. Resolved (not compared
    lexically) so the workspace-relative path the manifest records and
    the absolute path `--descriptor` carries are recognized as the same
    file rather than as two descriptors."""
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


def descriptor_in_force(
    workspace: Path,
    descriptor: Path | None,
    installed_rel: str | None,
) -> DescriptorInForce | None:
    """The `--descriptor` override, or `None` when there is nothing to
    override.

    `None` descriptor: the flag was not supplied, so the descriptor the
    manifest recorded stays in force. A descriptor that resolves to the
    installed one is the same file under a different spelling -- not an
    override -- which keeps `ligature --descriptor project-descriptor.json
    doctor` byte-identical to plain `doctor`."""
    if descriptor is None:
        return None
    if installed_rel is not None and _same_file(Path(workspace) / installed_rel, descriptor):
        return None
    return DescriptorInForce(
        path=_display_descriptor(workspace, descriptor),
        schema=descriptor_schema_report_at(descriptor),
        installed=installed_rel,
    )


def _require_compatible(manifest: dict) -> None:
    version = manifest.get("manifest_schema_version")
    if version != MANIFEST_SCHEMA_VERSION:
        raise InstallError(
            f"installation manifest schema {version!r} is incompatible with this product "
            f"({MANIFEST_SCHEMA_VERSION}); run a matching `ligature migrate`"
        )


def inspect(workspace: Path, *, descriptor: Path | None = None) -> InstallReport:
    """Classify the installed files and validate the descriptor in force.

    `descriptor` is the global `--descriptor` flag, already resolved by the
    caller to the default when it was not supplied (`None` means "not
    supplied", so doctor never mistakes the filled-in default for an
    operator's choice -- chainlink #109). Pass it to make the descriptor
    gate follow the flag; `None` keeps the manifest's own
    `descriptor_path`, which is what `migrate` wants: it recovers what
    `init` installed, not what a caller is currently trialling."""
    manifest = load_manifest(workspace)
    if manifest is None:
        report = InstallReport(mode="", product_name="", status="not-initialized")
        report.descriptor_override = descriptor_in_force(workspace, descriptor, None)
        return report
    if manifest.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION:
        report = InstallReport(
            mode=str(manifest.get("mode", "")),
            product_name=str(manifest.get("project_name", "")),
            status="incompatible",
        )
        report.messages.append(
            f"manifest schema {manifest.get('manifest_schema_version')!r} != {MANIFEST_SCHEMA_VERSION}"
        )
        report.descriptor_override = descriptor_in_force(
            workspace, descriptor, _manifest_descriptor_rel(manifest)
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
    report.policy_markers = _unfilled_marker_records(workspace, registry, {})
    report.authority_state = _authority_verdict(workspace, manifest, _skill_plan(report))
    report.descriptor_schema = descriptor_schema_report(workspace, descriptor_rel)
    report.descriptor_override = descriptor_in_force(workspace, descriptor, descriptor_rel)
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
    # chainlink #117: capture the pre-migration state so the upgrade can
    # emit a deterministic, machine-readable audit result.
    before_manifest = json.loads(json.dumps(manifest))
    upgrade_plans: list[FilePlan] = []

    if upgrade or force:
        registry = {entry.path: entry for entry in file_registry(mode, name, descriptor_rel)}
        if upgrade:
            plan = plan_install(workspace, mode, name, descriptor_rel, manifest)
            upgrade_plans = list(plan.files)
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
    action = "migrate"
    if upgrade and prune:
        action = "migrate --upgrade --prune"
    elif upgrade:
        action = "migrate --upgrade"
    elif prune:
        action = "migrate --prune"
    elif force:
        action = "migrate --force"
    report.audit = build_migration_audit(
        workspace,
        action,
        before_manifest,
        load_manifest(workspace),
        report,
        upgrade_plans,
        list(force),
        messages,
    )
    return report


def build_migration_audit(
    workspace: Path,
    action: str,
    before: dict,
    after: dict | None,
    report: InstallReport,
    upgrade_plans: list[FilePlan],
    forced: list[str],
    messages: list[str],
) -> dict:
    """The machine-readable audit result of a `migrate` run (chainlink
    #117): old/new version, changed paths, hashes, and required human
    action. Emitted as JSON on `migrate --json`; deterministic because it
    is derived only from the pre/post manifests and the plan, never from
    clock or locale."""
    after = after or {}
    old_files = {f["path"]: f for f in before.get("files", []) if isinstance(f, dict) and "path" in f}
    new_files = {f["path"]: f for f in after.get("files", []) if isinstance(f, dict) and "path" in f}
    outcomes = {plan.path: plan.outcome for plan in upgrade_plans}

    changed_paths = []
    for path in sorted(set(old_files) | set(new_files)):
        old = old_files.get(path)
        new = new_files.get(path)
        if old == new:
            continue
        entry = {
            "path": path,
            "ownership": (new or old or {}).get("ownership"),
            "old_base_hash": (old or {}).get("base_hash"),
            "new_base_hash": (new or {}).get("base_hash"),
            "old_expected_hash": (old or {}).get("expected_hash"),
            "new_expected_hash": (new or {}).get("expected_hash"),
        }
        if path in outcomes:
            entry["outcome"] = outcomes[path]
        changed_paths.append(entry)

    old_gates = before.get("gate_hashes") if isinstance(before.get("gate_hashes"), dict) else {}
    new_gates = after.get("gate_hashes") if isinstance(after.get("gate_hashes"), dict) else {}
    gate_hashes = {}
    for path in sorted(set(old_gates) | set(new_gates)):
        if old_gates.get(path) != new_gates.get(path):
            gate_hashes[path] = {"old": old_gates.get(path), "new": new_gates.get(path)}

    old_adj = before.get("adjudicator") if isinstance(before.get("adjudicator"), dict) else {}
    new_adj = after.get("adjudicator") if isinstance(after.get("adjudicator"), dict) else {}

    def versions(key: str) -> dict:
        return {"old": before.get(key), "new": after.get(key)}

    old_skill = old_files.get(SKILL_RELATIVE_PATH, {})
    new_skill = new_files.get(SKILL_RELATIVE_PATH, {})

    conflicts = []
    required_human_action = []
    for plan in report.files:
        if plan.ownership == MANAGED and plan.outcome in ("conflict", "missing"):
            target = workspace / plan.path
            disk = None
            if target.is_file():
                try:
                    disk = _sha256_file(target)
                except OSError:
                    disk = None
            conflicts.append(
                {
                    "path": plan.path,
                    "outcome": plan.outcome,
                    "recorded_base_hash": plan.base_hash,
                    "disk_hash": disk,
                    "expected_hash": plan.expected_hash,
                }
            )
            required_human_action.append(
                {
                    "path": plan.path,
                    "action": (
                        f"resolve the local modification of managed file {plan.path}: "
                        "either restore the recorded bytes or explicitly adopt the product "
                        f"version with `ligature migrate --force {plan.path}`"
                    ),
                }
            )
        elif plan.ownership == USER and plan.normative and plan.outcome == "drifted":
            required_human_action.append(
                {
                    "path": plan.path,
                    "action": (
                        f"review the unrecorded change to the normative document {plan.path} "
                        "and accept it with `ligature accept-policy --reviewer <name>`"
                    ),
                }
            )
    for forced_path in forced:
        required_human_action = [a for a in required_human_action if a["path"] != forced_path]
    for message in messages:
        if "not pruned" in message:
            required_human_action.append({"path": None, "action": message})

    return {
        "schema_version": "1.0",
        "action": action,
        "workspace": str(workspace),
        "result": report.status,
        "product_version": {"old": before.get("product_version"), "new": after.get("product_version")},
        "installed_product_version": versions("installed_product_version"),
        "installed_schema_versions": {
            "old": before.get("installed_schema_versions"),
            "new": after.get("installed_schema_versions"),
        },
        "installed_skill_version": versions("installed_skill_version"),
        "adjudicator": {
            "old": old_adj.get("content_hash"),
            "new": new_adj.get("content_hash"),
        },
        "skill_authority_hash": {
            "old": old_skill.get("authority_hash"),
            "new": new_skill.get("authority_hash"),
        },
        "changed_paths": changed_paths,
        "gate_hashes": gate_hashes,
        "obsolete": list(report.obsolete),
        "conflicts": conflicts,
        "required_human_action": required_human_action,
        "notes": list(messages),
    }


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
def _normative_documents(workspace: Path) -> tuple[list[RegistryEntry], dict[str, dict]] | None:
    """The workspace's normative user-owned registry entries (today: the
    reliance policy) paired with the ownership manifest's own per-path
    records, or `None` when there is no usable manifest at all -- not
    initialized, incompatible schema, unreadable, or an unknown mode.

    The single lookup the drift records (#78) and the version-marker
    records (#113) below share, so the two can never disagree about which
    documents are normative or about where they live. Degrading to `None`
    rather than raising is deliberate and matches `normative_drift_records`:
    a corrupt manifest means these reports have nothing to say, not that
    `check`/`status` crash; `doctor` remains the detail surface."""
    try:
        manifest = load_manifest(workspace)
    except InstallError:
        return None
    if manifest is None:
        return None
    if manifest.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION:
        return None
    try:
        mode = str(manifest.get("mode", ""))
        name = str(manifest.get("project_name", ""))
        descriptor_rel = _manifest_descriptor_rel(manifest)
        registry = file_registry(mode, name, descriptor_rel)
    except InstallError:
        return None
    return [entry for entry in registry if entry.normative], _manifest_files(manifest)


def policy_version_state(text: str) -> str:
    """Whether a policy document can yield the `policy_version` a promotion
    receipt carries (chainlink #113), as one of:

    * `stamped` -- exactly one well-formed
      `Policy version: <name>@<major>[.<minor>]` line. The only state both
      accept commands accept.
    * `unfilled` -- the marker line is there but carries no well-formed
      value: the placeholder `init` ships
      (`<policy-name>@<major>.<minor>`), or whatever a hand-edit left.
    * `absent` -- no `Policy version:` line at all.
    * `ambiguous` -- more than one of them, which the exactly-one rule
      (#78) refuses whatever they say.

    "A `Policy version:` line" here means a marker line
    (`POLICY_VERSION_CANDIDATE_RE`): the convention's own shape, carrying
    at most one value token. Prose in a policy's body that happens to begin
    with those words is not a declaration and is not counted, so this
    reports on the same lines #78's rule was about.

    Read from the document's own content, never from a value the caller
    supplies, so `accept-policy`, `accept-promotion` and the reporting
    below cannot disagree about what a well-formed policy is."""
    candidates = POLICY_VERSION_CANDIDATE_RE.findall(text)
    if len(candidates) > 1:
        return POLICY_AMBIGUOUS
    if not candidates:
        return POLICY_ABSENT
    return POLICY_STAMPED if POLICY_VERSION_MARKER_RE.findall(text) else POLICY_UNFILLED


def policy_version_gap(text: str) -> str:
    """A one-line, actionable diagnosis of why `text` cannot yield a
    `policy_version`. Only meaningful for a non-`stamped` document; the
    caller checks `policy_version_state` first."""
    state = policy_version_state(text)
    if state == POLICY_AMBIGUOUS:
        return (
            "the policy document declares more than one `Policy version:` line -- exactly one is "
            "required, so reduce it to one (a duplicate is refused even when both lines agree)"
        )
    if state == POLICY_ABSENT:
        return (
            "the policy document has no `Policy version:` line at all -- add one as its own line "
            "(`Policy version: <name>@<major>.<minor>`) before accepting it"
        )
    return (
        "the policy document's `Policy version:` line carries no `<name>@<major>.<minor>` value "
        "(the template `init` ships the placeholder `<policy-name>@<major>.<minor>`)"
    )


def policy_marker_advice(state: str, diagnosis: str) -> str:
    """`diagnosis`, plus the sanctioned way out of it when `accept-policy
    --version` can be that way. `absent`/`ambiguous` carry no remedy the
    flag can apply -- it refuses to invent or guess a marker line -- so
    those report the defect alone."""
    if state != POLICY_UNFILLED:
        return diagnosis
    return (
        f"{diagnosis}; stamp it with `ligature accept-policy --reviewer <name> "
        "--version <name>@<major>.<minor>`"
    )


def normalize_policy_version(value: str) -> str:
    """`value` as a bare, well-formed `<name>@<major>[.<minor>]` policy
    version, or `InstallError`. Surrounding whitespace and the backticks an
    operator naturally copies out of the template are stripped; nothing
    else is. Validated against `POLICY_VERSION_VALUE_RE` -- the same
    pattern the marker line itself accepts, so a stamped marker is always
    one this function would have accepted (chainlink #113)."""
    candidate = value.strip()
    if len(candidate) > 1 and candidate.startswith("`") and candidate.endswith("`"):
        candidate = candidate[1:-1].strip()
    if not POLICY_VERSION_VALUE_RE.fullmatch(candidate):
        raise InstallError(
            f"--version {value!r} is not a policy version of the form <name>@<major>[.<minor>] "
            "(lowercase name, e.g. reliance-policy@1.2) -- the same convention the policy "
            "document's `Policy version:` line uses"
        )
    return candidate


def stamp_policy_version(text: str, version: str) -> str:
    """`text` with its single `Policy version:` line rewritten to
    `version` -- the whole of what `accept-policy --version` is allowed to
    change in a normative user-owned document (chainlink #113).

    Refuses (InstallError) rather than guessing when the document has no
    marker line to stamp (`absent`) or more than one (`ambiguous`); the
    shipped placeholder and an already-stamped version both stamp fine,
    which is what makes the flag both the way out of the template's
    placeholder and the way to bump a real version on a substantive
    change. The result is re-validated as carrying exactly one marker line
    with the requested value, so a document this function returns can
    never be one `accept-promotion` would later refuse."""
    value = normalize_policy_version(version)
    candidates = POLICY_VERSION_CANDIDATE_RE.findall(text)
    if len(candidates) > 1:
        raise InstallError(
            f"refusing to stamp --version: the policy document declares {len(candidates)} "
            "`Policy version:` lines -- exactly one may be stamped, and this command will not "
            "guess which one a human meant"
        )
    if not candidates:
        raise InstallError(
            "refusing to stamp --version: the policy document has no `Policy version:` line to "
            "stamp -- add the line yourself as its own line "
            "(`Policy version: <name>@<major>.<minor>`), then re-run "
            "`ligature accept-policy --reviewer <name> --version <name>@<major>.<minor>`"
        )
    stamped, replaced = POLICY_VERSION_CANDIDATE_RE.subn(
        lambda _match: f"Policy version: `{value}`", text, count=1
    )
    if replaced != 1 or POLICY_VERSION_MARKER_RE.findall(stamped) != [value]:
        raise InstallError(  # pragma: no cover -- invariant guard, not a reachable branch
            "internal: stamping --version did not produce exactly one well-formed marker line"
        )
    return stamped


def policy_marker_records(workspace: Path) -> list[dict]:
    """One record per normative user-owned document whose own
    `Policy version:` marker is not stamped (chainlink #113) -- the state
    `init` writes, because the policy ships as a template whose version
    line is a placeholder no promotion receipt can be computed from.

    Each record is `{"path", "state" ("unfilled"|"absent"|"ambiguous"),
    "reason"}`, where `reason` is `policy_marker_advice` over
    `policy_version_gap` -- the diagnosis plus, where the flag can apply,
    the one command that fixes it. A missing document is NOT reported
    here: that is `normative_drift_records`' own signal (#78), and
    reporting it twice would make one real problem look like two.

    Empty when the workspace is not initialized, its manifest is
    incompatible or unreadable, or every marker is stamped."""
    documents = _normative_documents(workspace)
    if documents is None:
        return []
    entries, _records = documents
    records: list[dict] = []
    for entry in entries:
        try:
            target = safe_target(workspace, entry.path)
        except InstallError:
            continue
        if not target.is_file():
            continue
        try:
            text = target.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        state = policy_version_state(text)
        if state != POLICY_STAMPED:
            records.append(
                {"path": entry.path, "state": state, "reason": policy_marker_advice(state, policy_version_gap(text))}
            )
    return records


def _unfilled_marker_records(
    workspace: Path, registry: list[RegistryEntry], pending: dict[str, str]
) -> list[dict]:
    """The same records `policy_marker_records` builds, computed from the
    in-memory report an inventory is already carrying instead of a second
    manifest load, so `init`/`doctor`/`migrate`/`status` all read the
    document they just classified.

    `pending` supplies the rendered content for a normative document that
    is not on disk yet -- the text `plan_install` is about to write. That is
    what makes a fresh `init` report the placeholder it installs instead of
    silence; the statement is equally true after the write."""
    records: list[dict] = []
    for entry in registry:
        if not entry.normative:
            continue
        text = pending.get(entry.path)
        if text is None:
            try:
                target = safe_target(workspace, entry.path)
            except InstallError:
                continue
            if not target.is_file():
                continue
            try:
                text = target.read_text()
            except (OSError, UnicodeDecodeError):
                continue
        state = policy_version_state(text)
        if state != POLICY_STAMPED:
            records.append(
                {"path": entry.path, "state": state, "reason": policy_marker_advice(state, policy_version_gap(text))}
            )
    return records


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
    documents = _normative_documents(workspace)
    if documents is None:
        return []
    entries, records = documents
    drift: list[dict] = []
    for entry in entries:
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
    version: str | None = None,
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

    `version` (chainlink #113) is the sanctioned way to reach that state
    from the template `init` ships. `init` installs the reliance policy
    with its marker line still a `<policy-name>@<major>.<minor>` placeholder,
    and both this command and `accept-promotion` refuse such a document --
    so the one documented advance into an accepted policy was reachable
    only by hand-editing a document every project declares a
    `protected_root`, which no agent may write. Given `--version`, this
    function stamps that single marker line (see `stamp_policy_version`:
    one line, nothing else in the file, atomically, before the hash is
    recorded) and accepts the result; without it, nothing is written
    except the manifest and a document with no well-formed marker is still
    refused. So a human performs the whole advance with one command, and
    a version bump on a substantive policy change is the same command.

    Refuses (InstallError) before touching the document or the manifest
    when: the workspace is not initialized; no path can be resolved; the
    path is not a manifest-recorded, registry-normative, user-owned file;
    the file is missing; `--version` is not a well-formed policy version,
    or the document has no single marker line to stamp; the marker line is
    absent/ambiguous once stamping is settled; or the reviewer is empty.
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
    previous_version = next(iter(POLICY_VERSION_MARKER_RE.findall(text)), None)
    stamped = False
    if version is not None:
        # Validated and rendered in full before anything is written, so a
        # refusal here leaves the document byte-identical -- and the
        # manifest untouched, since the write happens before the record
        # is rebuilt and re-verified below.
        updated = stamp_policy_version(text, version)
        if updated != text:
            _write(workspace, policy_path, updated)
            stamped = True
            text = updated
    matches = POLICY_VERSION_MARKER_RE.findall(text)
    if len(matches) != 1:
        state = policy_version_state(text)
        raise InstallError(
            f"{policy_path!r} must have exactly one 'Policy version: <name>@<major>.<minor>' marker "
            f"line (docs/reliance-policy.template.md's own convention) -- found {len(matches)} "
            f"({policy_marker_advice(state, policy_version_gap(text))})"
        )
    previous = record.get("base_hash")
    accepted = _sha256_file(target)
    record["base_hash"] = accepted
    _write_manifest(workspace, manifest)
    return {
        "path": policy_path,
        "reviewer": reviewer,
        "policy_version": matches[0],
        "previous_version": previous_version,
        "stamped": stamped,
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
    """The schema-state annotation on a descriptor line (chainlink #74).
    `doctor` inventories the installed `project-descriptor.json` as
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
    override = report.descriptor_override
    if override is not None:
        # chainlink #109: the descriptor the operator named with
        # `--descriptor`, stated as its own line above the inventory so the
        # verdict is never read as being about the installed descriptor --
        # and, on an initialized workspace, an explicit note that the
        # inventory below still lists what `init` installed, so neither
        # report is silently substituted for the other.
        lines.append(f"descriptor in force: {override.path}{_descriptor_schema_note(override.schema)}")
        if override.installed is not None:
            lines.append(
                f"  note: the descriptor gate follows --descriptor; {override.installed} "
                "is the descriptor ci/manifest/installation.json records, still inventoried below"
            )
    if report.authority_state is not None:
        lines.append(f"skill authority hash: {report.authority_state}")
    for plan in sorted(report.files, key=lambda p: p.path):
        line = f"  {_OUTCOME_GLYPH.get(plan.outcome, plan.outcome):>9}  {plan.path}"
        if plan.path == report.descriptor_path and report.descriptor_schema is not None:
            line += _descriptor_schema_note(report.descriptor_schema)
        elif plan.outcome == "drifted":
            line += "  (unreviewed change; record it with `ligature accept-policy --reviewer <name>`)"
        # chainlink #113: a normative document whose `Policy version:` marker
        # is still the template's placeholder cannot yield the
        # `policy_version` any promotion receipt carries, so both accept
        # commands refuse it. Named on the document's own inventory line --
        # where the file is, and the one command that fixes it -- rather than
        # as a separate headline a reader could take for a statement about
        # some other file.
        marker = report.marker_record(plan.path)
        if marker is not None:
            line += f"  ({marker['reason']})"
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
