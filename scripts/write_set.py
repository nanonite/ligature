#!/usr/bin/env python3
"""Write-set conformance check (chainlink #77).

`project-descriptor.json`'s `write_set` (`allowed_roots` /
`protected_roots`) was consumed by nothing in v1.0: no command reported a
file outside `allowed_roots` or inside `protected_roots`, the descriptor
accepted every shape of the object (including `allowed_roots: ["**"]` and
`protected_roots: []`, rejecting only the object's total removal), and the
strings `write_set`/`allowed_roots`/`protected_roots` appeared in none of
the files `init` generates. The write set is the only thing the descriptor
says keeps an implementation inside its crate's `src/` and `tests/` and
away from the pinned upstream checkout, `ci/manifest/**` and the specs --
so an unenforced write set is indistinguishable from an unenforced-and-
undeclared one.

This module is the enforcement boundary's read-only check. It answers one
question: **is every file in the workspace accounted for by a declaration
the workspace itself makes?** A file is accounted for when it is

  * under an `allowed_roots` pattern (where the implementing agent may
    write -- the crate `src/` and `tests/` trees). `allowed_roots` wins:
    a path the descriptor explicitly permits is never also a violation,
  * product-managed (listed in the ownership manifest
    `ci/manifest/installation.json`),
  * user-owned (the descriptor itself and the declared policy documents),
  * the ownership manifest itself (which records the managed set and so
    cannot vouch for its own path out of it),
  * a pipeline artifact -- a file the pipeline's own artifact system
    recognizes (see `_pipeline_artifacts`),
  * inside the pinned upstream checkout (`port_source.repository`, when it
    names a directory inside the workspace) -- a declared, pinned input
    the agent never writes; the write set exists to keep implementation
    AWAY from it.

Everything else is either an **out-of-set file** (outside every root, no
declaration accounts for it -- the date-creusot pilot's repro, `rust/rogue/
evil.rs`, `rust/rogue/notes.txt`, `docs/evil.md` next to a filled
descriptor, which every v1.0 command reported nothing for) or a
**protected write** (chainlink #103).

`protected_roots` is the half of the boundary #77 shipped as an
"accounted for" category and never enforced. A file matching a
`protected_roots` pattern is no longer accounted for by *being* in one: it
is a violation unless a declaration vouches for that specific file, and
the two carve-outs that made every file under `ci/` or a declared crate's
`specs/` tree pass automatically are gone. Three files the date-creusot
pilot planted inside protected roots -- `ci/manifest/PROBE.json` and two
spec-tree probes -- were reported by nothing at all, `write set: clean`
with `violations: []` and output byte-identical to the no-probe baseline,
in three consecutive releases (1.1.0 through 1.2.1). The remedy the report
named is the one the skill's authority boundary 5 already claims: those
files are a *boundary-5 breach*, distinct from being out of set, and the
report carries them as their own class.

**How much of the protected surface can be enforced, and how much cannot.**
A protected file is a *decidable* violation when it sits where the
pipeline itself writes artifacts -- a declared crate's `specs/<kind>/`
tree, `ci/manifest/`, `evidence/`, `specs/_closure/`, `specs/_promotions/`,
`ci/results/c_static/` and the rest of `project_state`'s own artifact
discovery. Every file in those locations belongs to the artifact system, so
a file there the artifact system does not recognize -- by the very
discriminators `status` uses -- was not put there by the pipeline. That is
a `protected-write` **violation**: blocking, exit 1, one high-severity
`write-set` finding per file.

A protected file in a location the pipeline writes *nothing* into -- a
project's own `Cargo.toml`, `.github/`, `scripts/`, `rust-toolchain.toml`,
`build.rs`, `tests/harnesses/`, `docs/*-schema.json` -- is a different
problem, and the report is right that it was invisible, but the answer is
not a violation: nothing in the workspace distinguishes the project's own
`Cargo.toml` from one an agent wrote, so failing closed on it would make
every mature workspace permanently red and teach the operator to ignore
the command. Those are reported as `protected_unvouched`, still
non-blocking, but now **complete** (no carve-out hides any of them),
counted per declared `protected_roots` pattern, and named in `details`
together with the patterns that matched no file at all -- so "clean" can
no longer be read as "I found nothing to check" (the report's third
suggestion). One more file lands in that audit rather than the blocking
class: a file *nested below* an artifact directory. The artifact
discriminators disagree about depth (`_discover_artifact_files` reads one
level deep; `validate_interaction.find_interaction_files` deliberately
walks any depth so a nested placement surfaces as its own G1b violation),
so whether a nested file is pipeline output is not a question this check
can decide -- it is reported, not silently passed, and not called a
breach it cannot prove.

That residual is the one half of the boundary this check cannot close on
its own, and it is now labelled rather than silent.

Two further declaration-quality findings, because the pilot's probe matrix
showed the descriptor accepts shapes that declare no boundary at all:

  * an `allowed_roots` pattern that matches every path (`"**"`, `"*/**"`)
    makes the check vacuous -- no file can ever be out of set;
  * an empty `protected_roots` protects nothing.

Both are reported as violations of the write set's own purpose rather than
rejected by the schema: descriptor schemas are a stable public contract
(docs/trust-and-compatibility-boundaries.md §1), and the codebase's own
precedent for exactly this situation (chainlink #67's Stage-P0
placeholder) is a conservative finding, not a schema rule.

**Sanctioned protected writes (chainlink #114).** Enforcement with no exit
is enforcement that pushes work out of the tool: a protected-root write that
is genuinely required has, until #114, only two routes -- widen
`allowed_roots`, which deletes the boundary the write is supposed to live
inside, or have somebody edit the file outside every tool, which is
precisely what this check cannot tell apart from an intrusion. So an
`issue`-scoped capability record (`ligature authorize-write`, owned by
`write_authorization`) can now authorize one such write, and this module is
the only consumer:

  * grants are consulted **on the protected branch only** -- a grant can
    never excuse an out-of-set write, and `authorize-write` refuses a path
    no declared `protected_roots` pattern covers in the first place, so
    "a grant widens `allowed_roots`" is refused twice over;
  * a grant authorizes **its own issue only**, so the check must be told
    which issue the run is for (`--issue N`); with no issue named, no grant
    is consulted at all and the verdict is exactly what it was before #114,
    which is the fail-closed reading of a binding this check cannot confirm;
  * the grant's own `op` must be the operation observed (`write`): a
    `delete` grant cannot make an existing file clean;
  * an authorized write is **reported, not excused silently** -- it appears
    in `authorized_writes` with its grant id, issuer, issue and expiry, and
    its pattern's `protected_surface` entry counts it under `authorized`, so
    a `clean` verdict over a protected root a grant was used on still says
    so. An expired, replayed, other-issue or unverified-issuer grant
    authorizes nothing and is reported with that status;
* **a trail inside a protected root is not a capability record.** A
    declared `protected_roots` pattern covering `ci/results/` is a workspace
    `authorize-write` refuses to record a grant in, so a record read out of
    one was not written by the command whose rules this check enforces. This
    module holds the same rule on the read side rather than trusting the
    writer to have been the one that wrote it: every record in such a ledger
    is reported `ledger-protected` and none of them authorizes anything --
    without it, a line appended by hand would authorize the writes it names
    *and* a grant naming the ledger's own path would make the trail vouch for
    its own presence in the protected surface it must stay outside of. That
    is this module's own #103 discipline applied to the newest accounting
    category: a canonical pipeline location never excuses a declared
    protected root.

Read-only by construction: this module walks the workspace and reads the
ownership manifest and the grant ledger; it never writes.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Reuses validate_work_package's product-automaton glob engine rather than
# a second matcher: pattern/literal coverage is exactly pattern/pattern
# overlap with the literal side unable to wildcard, so "is this concrete
# file under this allowed/protected pattern" is the same engine chainlink
# #14's review chain hardened and #35's renderer-integrity check already
# proves coverage with.
import write_authorization  # noqa: E402
from review_checkpoint import DRAFT_SUFFIX as _DRAFT_SUFFIX  # noqa: E402
from validate_work_package import _pattern_covers_path  # noqa: E402

# Directories pruned from the walk: VCS internals and Python bytecode
# caches are not agent-written content, and a pilot workspace's `.git/`
# alone would otherwise dominate every report.
_SKIP_DIRS = frozenset({".git", "__pycache__"})

# The pipeline's own canonical artifact and output locations (plan.md
# §10/§11's fixed layout). The write set governs where the implementing
# agent writes; these are where the pipeline writes, so files there are
# accounted for by the pipeline rather than by the write set.
_CANONICAL_PREFIXES = (
    "ci/",               # work-package manifests, results, harnesses
    "evidence/",         # evidence records (plan.md §11)
    "docs/witnesses/",   # contact-sheet projection
)

_DETAILS_LIST_LIMIT = 10

# The ownership manifest's own path. It is the declaration that vouches for
# every product-managed file, so it cannot vouch for itself out of its own
# `files` list -- and it is what the pilot protected (`ci/manifest/**`)
# while an agent could have replaced it wholesale. Named here rather than
# reached through project_state's MANIFEST_RELATIVE_PATH so this module
# keeps no import-time dependency on the heavier state builder.
_OWNERSHIP_MANIFEST_REL = "ci/manifest/installation.json"

# A staged draft lives inside the protected artifact location it will be
# promoted into, is inert to every consumer (every artifact-kind discovery
# function excludes it -- chainlink #79/#90/#98/#110), and is written by
# the pipeline's own command -- so it is pipeline output, not a boundary
# breach. The suffix itself is review_checkpoint's DRAFT_SUFFIX (imported
# above), the one spelling of the name stage_draft() writes and
# is_staged_draft() decides by.

# Violation classes, carried in the report so a caller can tell an
# out-of-set file from a write into a protected root. Only the second is a
# breach of the installed skill's authority boundary 5 ("write only under
# allowed_roots; never write into protected_roots"): an out-of-set file is
# written where no root permits at all.
CLASS_OUT_OF_SET = "out-of-set"
CLASS_PROTECTED_WRITE = "protected-write"
CLASS_DECLARATION = "declaration"


@dataclass
class WriteSetViolation:
    """One write-set violation: a file no declaration accounts for, a write
    into a protected root, or a write-set declaration that enforces nothing.

    `kind` is the report's class vocabulary -- `out-of-set`,
    `protected-write`, `declaration` -- so the two halves of the boundary
    are reported as distinct classes rather than one undifferentiated list.
    """

    path: str
    reason: str
    kind: str = CLASS_OUT_OF_SET


@dataclass
class ProtectedGlob:
    """One declared `protected_roots` pattern and what it covers.

    `files` is how many workspace files the pattern matched, `violations`
    how many of those are blocking `protected-write` findings, `authorized`
    how many a write grant covered (chainlink #114), and `unattributed` how
    many are the non-blocking audit. A pattern with `files == 0` is
    reported by name: a protected root nothing matches is a declaration that
    protects nothing on disk, and must not read as a protected root that was
    checked and found clean.
    """

    pattern: str
    files: int = 0
    violations: int = 0
    unattributed: int = 0
    authorized: int = 0


@dataclass
class AuthorizedWrite:
    """One protected-root write an active write grant covered (chainlink
    #114) -- reported rather than excused, so a `clean` verdict names the
    grant whose authority made it clean.

    `status` is `active` for a standing grant and `spent` for a `--one-shot`
    one: a one-shot grant authorized the single write it names and
    authorizes nothing further, which a snapshot of the workspace can say
    (the file it authorized is present) even though it cannot count the
    edits made to that file afterwards. That limit is the same one
    `protected_unvouched` is reported under, and it is stated rather than
    assumed.
    """

    path: str
    grant_id: str
    issue: int
    op: str
    issuer: str
    issuer_kind: str
    issued_at: str
    expires_at: str
    one_shot: bool
    status: str

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "grant_id": self.grant_id,
            "issue": self.issue,
            "op": self.op,
            "issuer": self.issuer,
            "issuer_kind": self.issuer_kind,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "one_shot": self.one_shot,
            "status": self.status,
        }


@dataclass
class GrantAudit:
    """The write-grant ledger as this run consumed it (chainlink #114): where
    it is, whether it could be read, the issue the run named, every usable
    record with its status (`active`, `expired`, `other-issue`,
    `unverified-issuer`, `duplicate`, `no-issue-named`, `ledger-protected`),
    and the lines that claim to be grants and are not usable.

    Carried in full whether or not any grant authorized anything: a ledger
    nobody looked at is how an authorization surface goes stale unnoticed.

    Two run-wide reasons for consuming no grant are carried as fields rather
    than only as prose, because both are machine-actionable and a consumer
    reading the JSON cannot act on a sentence:

    * `error` -- why the ledger could not be read at all (`state:
      "unreadable"`). An unreadable trail proves no authorization, so it
      yields none, and the reason is reported instead of dropped: "the ledger
      is broken" is not something an operator can fix.
    * `ledger_protected` -- the declared `protected_roots` pattern covering
      the ledger itself, when there is one. `authorize-write` refuses to
      record a grant there, so a record found in one was not written by the
      command whose rules this check enforces, and honoring it would let a
      trail inside a protected root authorize the very writes -- including
      its own presence -- that the declaration protects.
    """

    ledger: str | None = None
    state: str = "absent"
    issue: int | None = None
    entries: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    ignored: int = 0
    error: str | None = None
    ledger_protected: str | None = None

    def as_dict(self) -> dict:
        return {
            "ledger": self.ledger,
            "state": self.state,
            "issue": self.issue,
            "entries": list(self.entries),
            "rejected": list(self.rejected),
            "ignored": self.ignored,
            "error": self.error,
            "ledger_protected": self.ledger_protected,
        }


@dataclass
class WriteSetReport:
    """The verdict for one workspace against its descriptor's write set.

    `state` is `clean` (every file accounted for), `violations` (out-of-set
    files, writes into a protected root with no active grant, or a vacuous
    declaration), or `unknown` (no valid project descriptor -- the write set
    cannot be evaluated). `violations` carries the blocking findings;
    `protected_unvouched` is the non-blocking protected-surface audit,
    `protected_surface` reports the same surface per declared pattern,
    `authorized_writes` the protected writes an active grant covered, and
    `grants` the grant ledger this run consumed.
    """

    state: str
    details: str
    violations: list[WriteSetViolation] = field(default_factory=list)
    protected_unvouched: list[str] = field(default_factory=list)
    protected_surface: list[ProtectedGlob] = field(default_factory=list)
    authorized_writes: list[AuthorizedWrite] = field(default_factory=list)
    grants: GrantAudit = field(default_factory=GrantAudit)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


def _rel_to_workspace(path: Path, workspace: Path) -> str:
    """`path` as a workspace-relative posix string, for comparison with the
    walk's own relative paths. An absolute descriptor outside the workspace
    cannot be relativized and falls back to its absolute form (the same
    honest fallback project_state._rel uses)."""
    try:
        return path.resolve().relative_to(workspace).as_posix()
    except ValueError:
        return path.as_posix()


def _walk_files(workspace: Path) -> list[str]:
    """Every workspace-relative posix path of a regular file, sorted for
    determinism. VCS internals and bytecode caches are pruned; symlinks are
    skipped (a symlink can point outside the workspace -- hashing it would
    be checking someone else's file, the same discipline the gate_integrity
    and write-set-pattern checks apply)."""
    rels: list[str] = []
    for root, dirs, filenames in os.walk(workspace, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        for name in sorted(filenames):
            path = Path(root) / name
            if path.is_symlink():
                continue
            rels.append(path.relative_to(workspace).as_posix())
    return sorted(rels)


def _under_prefix(rel: str, prefix: str) -> bool:
    return rel == prefix or rel.startswith(prefix + "/")


def _is_canonical(rel: str) -> bool:
    """Whether `rel` is one of the pipeline's own canonical locations --
    places the pipeline writes, not the implementing agent.

    Only ever an accounting for a file OUTSIDE every protected root (the
    protected branch below runs first, so a declared protected root is
    never excused by this heuristic). Chainlink #103: `ci/` being a
    canonical prefix is exactly what made `ci/manifest/PROBE.json` -- a
    file inside the pilot's `ci/manifest/**` protected root -- pass with no
    line of output at all."""
    if any(rel.startswith(prefix) for prefix in _CANONICAL_PREFIXES):
        return True
    # Workspace-level pipeline spec artifacts: specs/_<kind>/...
    # (_boundaries, _interactions, _exemptions, _protocol_debt, _bridges,
    # _witnesses, _closure, _conflicts, _gold_sets, _promotions). Crate-
    # level underscore dirs are accounted for by the declared spec-tree
    # check instead.
    parts = rel.split("/")
    return len(parts) >= 2 and parts[0] == "specs" and parts[1].startswith("_")


def _matches_every_path(pattern: str) -> bool:
    """Whether a glob pattern matches every possible path (`"**"`,
    `"*/**"`, ...). Such an `allowed_roots` entry makes the write set
    vacuous: no file can ever be out of set, so the boundary it declares
    enforces nothing (chainlink #77's `allowed_roots: ["**"]` probe)."""
    segments = [s for s in pattern.split("/") if s not in ("", ".")]
    if pattern.endswith("/"):
        segments.append("**")
    return (
        bool(segments)
        and segments[-1] == "**"
        and all(s == "*" or s == "**" for s in segments)
    )


def _managed_paths(workspace: Path) -> set[str]:
    """workspace-relative paths the ownership manifest records as
    product-managed (ligature_install owns the manifest). User-owned
    entries are accounted for separately, via the descriptor's own
    declarations."""
    data = _read_json(workspace / _OWNERSHIP_MANIFEST_REL)
    if not isinstance(data, dict):
        return set()
    files = data.get("files")
    if not isinstance(files, list):
        return set()
    return {
        entry["path"]
        for entry in files
        if isinstance(entry, dict)
        and isinstance(entry.get("path"), str)
        and entry.get("ownership") == "managed"
    }


def _parent_dir(rel: str) -> str:
    """`rel`'s containing directory as a workspace-relative path ("" when the
    file sits at the workspace root)."""
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def _is_staged_draft(rel: str, artifact_dirs: set[str]) -> bool:
    """Whether `rel` is a staged draft -- `<target>.json.draft` -- sitting in
    one of the locations the pipeline writes artifacts into.

    `review_checkpoint.stage_draft` (reached through `ligature draft`) is
    the pipeline's own command writing inside a protected artifact tree,
    and the staged file is inert until `approve` / `promote-evidence`
    promotes it: every artifact-kind discovery function excludes it
    (chainlink #79/#90/#98/#110), so it is a pending artifact, not a
    hand-placed one. Recognized by the pipeline's own suffix convention
    and restricted to the artifact directories, so a `.draft` file in,
    say, `scripts/` gets no exemption.
    """
    if not rel.endswith(_DRAFT_SUFFIX):
        return False
    staged = rel[: -len(_DRAFT_SUFFIX)]
    if not staged.endswith((".json", ".yaml", ".yml")):
        return False
    return _parent_dir(staged) in artifact_dirs


def _pipeline_artifacts(workspace: Path, descriptor: dict | None) -> tuple[set[str], set[str]]:
    """The files the pipeline's own artifact system owns, and the
    directories it owns them in.

    Both come from `project_state` -- `artifact_dirs()` for the locations
    the pipeline writes into and `_discover_artifact_files` for the files it
    actually wrote -- plus that module's own kind -> identity-field table
    and `gate_g14.looks_like_work_package_manifest`, the same discriminators
    `status` uses. Reusing them is the point: a second hand-kept list of
    "which paths the pipeline writes" would be free to drift from the
    artifact system the pilot actually runs, and would re-open the hole
    chainlink #103 reports the moment a kind was added.

    A discovered file is recognized only when it carries its kind's
    identity field (`boundary_id`, `interaction_id`, `report_id`, ...) --
    or, for a work-package manifest, the `work_package` /
    `definition_of_done` key that discriminates it from the ownership
    manifest sharing its directory. Discovery alone is too coarse to vouch
    for a file: it takes every `*.json` under a crate's `_boundaries/`,
    which is how the pilot's `specs/_boundaries/PROBE.json` passed. Shape
    is the discriminator, not schema validity -- whether a recognized
    artifact is well-formed is another command's finding, and this check
    is about provenance.

    The import is inside the function, for the same reason
    `project_state._looks_like_work_package_manifest` imports `gate_g14`
    there: `project_state` imports this module at module scope, so a
    module-level import back would be circular.
    """
    from project_state import (
        _ARTIFACT_KINDS,
        _discover_artifact_files,
        _load_data,
        artifact_dirs,
    )

    owned_dirs = {rel_dir for rel_dir, _kind in artifact_dirs(workspace, descriptor)}
    recognized: set[str] = set()
    for kind, path in _discover_artifact_files(workspace, descriptor):
        rel = _rel_to_workspace(path, workspace)
        data = _load_data(path)
        if not isinstance(data, dict):
            # Unparseable: not something the pipeline's own writers emit.
            continue
        if kind == "work-package":
            from gate_g14 import looks_like_work_package_manifest

            if not looks_like_work_package_manifest(data):
                continue
        else:
            id_field = _ARTIFACT_KINDS.get(kind, (None, None, None))[2]
            if not isinstance(data.get(id_field), str):
                continue
        recognized.add(rel)
    return recognized, owned_dirs


def _user_owned_paths(workspace: Path, descriptor: dict, descriptor_path: Path) -> set[str]:
    """Paths the descriptor itself owns: the descriptor file and the
    declared policy documents (user-owned templates `init` creates once and
    never overwrites)."""
    paths = {_rel_to_workspace(descriptor_path, workspace)}
    compat = descriptor.get("compatibility_policy")
    if isinstance(compat, dict):
        for key in ("reliance_policy_path", "witness_policy_path"):
            value = compat.get(key)
            if isinstance(value, str) and value:
                paths.add(value)
    return paths


def _spec_tree_prefixes(descriptor: dict) -> list[str]:
    """Each declared crate's `specs/` tree -- the pipeline's own protected
    spec artifacts, descriptor-declared per crate."""
    prefixes: list[str] = []
    for crate in descriptor.get("crates", []):
        if not isinstance(crate, dict):
            continue
        crate_dir = crate.get("crate_dir")
        if isinstance(crate_dir, str) and crate_dir:
            prefixes.append(f"{crate_dir.rstrip('/')}/specs")
    return prefixes


def _port_source_prefix(workspace: Path, descriptor: dict) -> str | None:
    """The workspace-relative prefix of the pinned upstream checkout, when
    `port_source.repository` names a directory inside the workspace.

    The checkout is a declared, pinned input the implementing agent never
    writes -- the write set exists to keep implementation AWAY from it -- so
    its own files are not out-of-set writes. URLs, absolute paths outside
    the workspace, and the unfilled example placeholder name no real
    directory and exclude nothing.
    """
    port_source = descriptor.get("port_source")
    if not isinstance(port_source, dict):
        return None
    repo = port_source.get("repository")
    if not isinstance(repo, str) or not repo:
        return None
    candidate = Path(repo)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(workspace)
    except (ValueError, OSError):
        return None
    if resolved == workspace or not resolved.is_dir():
        return None
    return resolved.relative_to(workspace).as_posix()


def _protected_surface_details(surface: list[ProtectedGlob], unattributed: list[str]) -> str:
    """The protected-surface clause appended to `details`.

    Chainlink #103's third suggestion: a `clean` verdict over a protected
    surface must be distinguishable from one where the check found nothing
    to look at. So the clause is present whenever protected roots are
    declared and names (a) how many files the declared patterns actually
    cover, (b) how many of those no declaration vouches for -- with the
    reason it is not blocking, so the audit cannot read as coverage, and
    (c) every declared pattern that matched no file at all, which protects
    nothing on disk and is not a surface that was checked.
    """
    if not surface:
        return ""
    clauses: list[str] = []
    covered = sum(entry.files for entry in surface)
    if covered:
        clauses.append(
            f"protected surface: {covered} file(s) inside {len(surface)} declared "
            "protected_roots pattern(s)"
        )
    if unattributed:
        clauses.append(
            f"{len(unattributed)} of them in locations the pipeline writes nothing into "
            "are vouched for by no declaration (audit only -- the check cannot attribute "
            "them, so this verdict does not cover them)"
        )
    unmatched = [entry.pattern for entry in surface if entry.files == 0]
    if unmatched:
        listed = ", ".join(unmatched[:_DETAILS_LIST_LIMIT])
        more = (
            f" (+{len(unmatched) - _DETAILS_LIST_LIMIT} more)"
            if len(unmatched) > _DETAILS_LIST_LIMIT
            else ""
        )
        clauses.append(
            f"protected_roots pattern(s) matching no file in this workspace: {listed}{more}"
        )
    return "; " + "; ".join(clauses) if clauses else ""


def _grant_details(audit: GrantAudit, authorized: list[AuthorizedWrite]) -> str:
    """The write-grant clause appended to `details` (chainlink #114).

    Reported whichever way it goes. When a grant authorized a write, the
    verdict says so and names the issue it was scoped to -- a `clean` over a
    protected root a grant was spent on must not read as "nothing happened
    there". When grants exist but none was consulted, the clause names the
    condition: the flag that would consume them, or the run-wide reason none
    of them could be (an unreadable ledger, with its cause; a ledger inside a
    declared protected root). Each is stated where it is observed rather than
    at the moment someone tries the write.
    """
    clauses: list[str] = []
    if authorized:
        grant_ids: list[str] = []
        for entry in authorized:
            if entry.grant_id not in grant_ids:
                grant_ids.append(entry.grant_id)
        listed = ", ".join(grant_ids[:_DETAILS_LIST_LIMIT])
        more = (
            f" (+{len(grant_ids) - _DETAILS_LIST_LIMIT} more)"
            if len(grant_ids) > _DETAILS_LIST_LIMIT
            else ""
        )
        clauses.append(
            f"{len(authorized)} protected write(s) authorized by {len(grant_ids)} active write "
            f"grant(s) [{listed}{more}] for issue {audit.issue} (recorded in {audit.ledger})"
        )
    if audit.state == "unreadable":
        # The reason travels with the clause: "the ledger is unreadable" is a
        # condition to fix, and dropping the cause would leave the report
        # saying only that something is wrong with a file nobody can now open.
        clauses.append(
            "the write-grant ledger could not be read, so no write grant was honored"
            + (f" ({audit.error})" if audit.error else "")
        )
    elif audit.ledger_protected is not None:
        clauses.append(
            f"the write-grant ledger {audit.ledger} is inside declared protected root "
            f"{audit.ledger_protected!r}, where ligature authorize-write refuses to record a grant, "
            f"so no grant recorded in it was honored: a capability trail inside a protected root is "
            "not a capability record, whoever appended its lines"
        )
    elif audit.entries and audit.issue is None:
        clauses.append(
            f"{len(audit.entries)} write grant(s) recorded in {audit.ledger} were not consulted: "
            "a grant is scoped to one issue, so pass --issue <N> to consume the grants recorded "
            "for the issue this run is about"
        )
    if audit.rejected:
        clauses.append(
            f"{len(audit.rejected)} line(s) of {audit.ledger} claim to be write grants and are not "
            "usable, so they authorize nothing"
        )
    return "; " + "; ".join(clauses) if clauses else ""


def _grant_audit(
    workspace: Path,
    descriptor: dict | None,
    *,
    issue: int | None,
    now: datetime | None,
) -> tuple[GrantAudit, list[write_authorization.Grant]]:
    """The ledger as this run consumes it, and the records active for it.

    Read once per workspace rather than once per file: the statuses depend on
    the clock, the issue, the descriptor's supervisor whitelist and the
    descriptor's own protected roots, none of which varies within a walk, and
    re-deriving them per file would make the verdict quadratic in the number
    of grants.

    The descriptor's `protected_roots` are consulted here, and not only for
    the file being judged: they are also what decides whether the ledger may
    be consumed at all. A declared pattern covering `ci/results/` is a
    workspace `authorize-write` refuses to record a grant in, so a record read
    out of one cannot have come from the command whose rules this check
    enforces -- and honoring it would make the trail vouch for its own
    presence in a protected root.
    """
    ledger = write_authorization.read_grant_ledger(workspace)
    supervisors = write_authorization.supervisor_authorities(descriptor)
    write_set = descriptor.get("write_set") if isinstance(descriptor, dict) else None
    write_set = write_set if isinstance(write_set, dict) else {}
    protected_roots = [
        pattern for pattern in (write_set.get("protected_roots") or []) if isinstance(pattern, str)
    ]
    ledger_protected = (
        write_authorization.ledger_protected_by(protected_roots, write_authorization.GRANT_LEDGER_REL)
        if ledger.path is not None
        else None
    )
    statuses = ledger.statuses(
        issue=issue,
        supervisors=supervisors,
        now=now,
        ledger_protected=ledger_protected,
    )
    audit = GrantAudit(
        ledger=write_authorization.GRANT_LEDGER_REL if ledger.path is not None else None,
        state=ledger.state,
        issue=issue,
        entries=[
            {
                "grant_id": grant.grant_id,
                "issue": grant.issue,
                "path": grant.path,
                "path_kind": grant.path_kind,
                "op": grant.op,
                "one_shot": grant.one_shot,
                "issuer": grant.issuer,
                "issuer_kind": grant.issuer_kind,
                "issued_at": grant.issued_at,
                "expires_at": grant.expires_at,
                "line": grant.line,
                "status": status,
            }
            for grant, status in statuses
        ],
        rejected=[entry.as_dict() for entry in ledger.rejected],
        ignored=ledger.ignored,
        error=ledger.error,
        ledger_protected=ledger_protected,
    )
    active = [grant for grant, status in statuses if status == write_authorization.STATUS_ACTIVE]
    return audit, active


def _authorized_write(grant: write_authorization.Grant, rel: str) -> AuthorizedWrite:
    """The report entry for one protected write a grant covered. A one-shot
    grant is `spent` the moment the file it authorized is present: it
    authorized that single write and nothing further, which is the most a
    snapshot can honestly say."""
    return AuthorizedWrite(
        path=rel,
        grant_id=grant.grant_id,
        issue=grant.issue,
        op=grant.op,
        issuer=grant.issuer,
        issuer_kind=grant.issuer_kind,
        issued_at=grant.issued_at,
        expires_at=grant.expires_at,
        one_shot=grant.one_shot,
        status="spent" if grant.one_shot else "active",
    )


def check_write_set(
    workspace: Path,
    descriptor: dict | None,
    descriptor_path: Path,
    *,
    issue: int | None = None,
    now: datetime | None = None,
) -> WriteSetReport:
    """The write-set verdict for `workspace` against its descriptor.

    `descriptor` is None when no schema-valid descriptor could be loaded
    (absent, invalid, unreadable); the report is then honestly `unknown`
    -- the write set cannot be evaluated, never silently clean.

    `issue` (chainlink #114) is the issue this run is about, and it is what
    scopes every write grant the check consumes: a grant authorizes its own
    issue only, so with no issue named none is consulted and the verdict is
    the pre-#114 one. `now` exists for the same reason `issued_at` does on
    the writer -- expiry is otherwise untestable without a clock race.
    """
    workspace = workspace.resolve()
    if descriptor is None:
        return WriteSetReport(
            state="unknown",
            details="no valid project descriptor present -- the write set cannot be evaluated",
        )

    write_set = descriptor.get("write_set")
    if not isinstance(write_set, dict):
        write_set = {}
    allowed_roots = [p for p in (write_set.get("allowed_roots") or []) if isinstance(p, str)]
    protected_roots = [p for p in (write_set.get("protected_roots") or []) if isinstance(p, str)]

    # Declaration-quality findings (chainlink #77): shapes the schema
    # accepts that declare no enforcement boundary at all.
    violations: list[WriteSetViolation] = []
    for pattern in allowed_roots:
        if _matches_every_path(pattern):
            violations.append(
                WriteSetViolation(
                    path=_rel_to_workspace(descriptor_path, workspace),
                    reason=(
                        f"allowed_roots pattern {pattern!r} matches every path -- the write set "
                        "declares no boundary, so no file can ever be out of set (chainlink #77)"
                    ),
                    kind=CLASS_DECLARATION,
                )
            )
    if not protected_roots:
        violations.append(
            WriteSetViolation(
                path=_rel_to_workspace(descriptor_path, workspace),
                reason=(
                    "protected_roots is empty -- no path is declared protected, so the write "
                    "set's protected half enforces nothing (chainlink #77)"
                ),
                kind=CLASS_DECLARATION,
            )
        )

    managed = _managed_paths(workspace)
    user_owned = _user_owned_paths(workspace, descriptor, descriptor_path)
    spec_trees = _spec_tree_prefixes(descriptor)
    port_prefix = _port_source_prefix(workspace, descriptor)
    pipeline_artifact_paths, pipeline_artifact_dirs = _pipeline_artifacts(workspace, descriptor)
    # chainlink #114: the write-grant ledger, read once. `active` is empty
    # whenever no issue was named -- a grant authorizes its own issue only,
    # so a run that does not say which issue it is about consumes none.
    grant_audit, active_grants = _grant_audit(workspace, descriptor, issue=issue, now=now)
    grant_clause = (
        f" and no active write grant in {write_authorization.GRANT_LEDGER_REL} covers this path"
        f" for issue {issue}"
        if issue is not None
        else " and no write grant is consulted for a run that named no issue"
    )

    # Per declared protected pattern, so a pattern that protects nothing on
    # disk is named rather than indistinguishable from one that was checked
    # and found clean (chainlink #103's third suggestion).
    surface: dict[str, ProtectedGlob] = {
        pattern: ProtectedGlob(pattern=pattern) for pattern in protected_roots
    }

    out_of_set: list[WriteSetViolation] = []
    protected_writes: list[WriteSetViolation] = []
    protected_unvouched: list[str] = []
    authorized_writes: list[AuthorizedWrite] = []
    for rel in _walk_files(workspace):
        if port_prefix is not None and _under_prefix(rel, port_prefix):
            continue
        if any(_pattern_covers_path(pattern, rel) for pattern in allowed_roots):
            # An explicitly allowed path is permitted, so it is not also a
            # breach of a protected root the descriptor happened to overlap.
            # Deliberately ahead of the grant lookup: a grant authorizes a
            # PROTECTED-root write, so it has nothing to say about a path the
            # descriptor already permits -- and `authorize-write` refuses to
            # record one for such a path at all.
            continue
        # What vouches for this specific file, independent of where it sits.
        # `protected_roots` is NOT in this list: being inside one is the
        # thing to be enforced, never the reason a file passes.
        vouched = (
            rel in managed
            or rel in user_owned
            or rel == _OWNERSHIP_MANIFEST_REL
            or rel in pipeline_artifact_paths
            or _is_staged_draft(rel, pipeline_artifact_dirs)
        )
        protected_pattern = next(
            (pattern for pattern in protected_roots if _pattern_covers_path(pattern, rel)),
            None,
        )
        if protected_pattern is not None:
            entry = surface[protected_pattern]
            entry.files += 1
            if vouched:
                continue
            # chainlink #114: a sanctioned protected-root write, authorized by
            # an active grant for this issue. Reported, not excused silently:
            # it leaves `authorized_writes` with its grant id and counts under
            # its pattern's `authorized`, so a clean verdict says which grant
            # made it clean. Checked before the decidable/undecidable split,
            # because a grant answers the same question in both halves --
            # who vouches for this file -- and only its own report differs.
            grant = write_authorization.authorizing_grant(
                active_grants, rel, write_authorization.OP_WRITE
            )
            if grant is not None:
                entry.authorized += 1
                authorized_writes.append(_authorized_write(grant, rel))
                continue
            if _parent_dir(rel) in pipeline_artifact_dirs:
                # Decidable: this is a location the pipeline's own artifact
                # system owns, and it does not recognize the file, so the
                # pipeline did not put it here (chainlink #103).
                entry.violations += 1
                protected_writes.append(
                    WriteSetViolation(
                        path=rel,
                        reason=(
                            f"inside protected_roots pattern {protected_pattern!r} and outside "
                            "every allowed_roots pattern, in a location the pipeline writes its "
                            "own artifacts into -- no declaration vouches for it"
                            + grant_clause
                            + ", so it is a write into a protected root (chainlink #103, #114)"
                        ),
                        kind=CLASS_PROTECTED_WRITE,
                    )
                )
                continue
            # Undecidable provenance: nothing in the workspace separates a
            # project's own protected file from an agent's. Reported, not
            # failed closed on -- see the module docstring.
            entry.unattributed += 1
            protected_unvouched.append(rel)
            continue
        if (
            vouched
            or any(_under_prefix(rel, prefix) for prefix in spec_trees)
            or _is_canonical(rel)
        ):
            continue
        out_of_set.append(
            WriteSetViolation(
                path=rel,
                reason=(
                    "outside every allowed_roots pattern and not accounted for by "
                    "the ownership manifest, a declared user-owned document, a "
                    "pipeline artifact, a declared crate's specs/ tree, or a "
                    "canonical pipeline location"
                ),
            kind=CLASS_OUT_OF_SET,
            )
        )

    violations.extend(protected_writes)
    violations.extend(out_of_set)

    protected_surface = [surface[pattern] for pattern in protected_roots]
    surface_details = _protected_surface_details(protected_surface, protected_unvouched)
    grant_details = _grant_details(grant_audit, authorized_writes)

    if violations:
        listed = ", ".join(v.path for v in violations[:_DETAILS_LIST_LIMIT])
        more = f" (+{len(violations) - _DETAILS_LIST_LIMIT} more)" if len(violations) > _DETAILS_LIST_LIMIT else ""
        counts: list[str] = []
        if protected_writes:
            counts.append(f"{len(protected_writes)} write(s) into a protected root")
        if out_of_set:
            counts.append(f"{len(out_of_set)} file(s) outside the write set")
        if len(violations) > len(protected_writes) + len(out_of_set):
            counts.append(f"{len(violations) - len(protected_writes) - len(out_of_set)} declaration finding(s)")
        details = f"{len(violations)} write-set violation(s): {listed}{more}"
        if counts:
            details += f" ({'; '.join(counts)})"
        details += surface_details
        details += grant_details
        return WriteSetReport(
            state="violations",
            details=details,
            violations=violations,
            protected_unvouched=protected_unvouched,
            protected_surface=protected_surface,
            authorized_writes=authorized_writes,
            grants=grant_audit,
        )

    details = (
        "write set is clean -- every file is accounted for by allowed_roots, the ownership "
        "manifest, a declared user-owned document, a pipeline artifact, an active write grant "
        "(chainlink #114), a declared crate's specs/ tree, or a canonical pipeline location"
    )
    details += surface_details
    details += grant_details
    return WriteSetReport(
        state="clean",
        details=details,
        protected_unvouched=protected_unvouched,
        protected_surface=protected_surface,
        authorized_writes=authorized_writes,
        grants=grant_audit,
    )
