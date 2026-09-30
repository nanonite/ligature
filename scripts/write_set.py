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
    write -- the crate `src/` and `tests/` trees),
  * under a `protected_roots` pattern (specs, CI manifests, gate scripts,
    schemas, policy docs, Cargo/build files, harnesses, toolchain pins --
    declared off-limits to the agent),
  * product-managed (listed in the ownership manifest
    `ci/manifest/installation.json`),
  * user-owned (the descriptor itself and the declared policy documents),
  * under a declared crate's `specs/` tree (spec artifacts -- the
    pipeline's own protected content, descriptor-declared per crate),
  * under a canonical pipeline location (`ci/`, `evidence/`, workspace-
    level `specs/_<kind>/`, `docs/witnesses/`) -- places the PIPELINE
    writes, not the implementing agent, so the write set does not govern
    them, or
  * inside the pinned upstream checkout (`port_source.repository`, when it
    names a directory inside the workspace) -- a declared, pinned input
    the agent never writes; the write set exists to keep implementation
    AWAY from it.

Everything else is an **out-of-set file**: a file whose presence no
declaration accounts for. The date-creusot pilot's repro -- `rust/rogue/
evil.rs`, `rust/rogue/notes.txt`, `docs/evil.md` created next to a filled
descriptor -- is exactly this category, and it was reported by nothing.

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

Files under a `protected_roots` pattern that are none of managed /
user-owned / declared-spec / canonical -- the pilot's own scripts, CI
config, Cargo.tomls -- are reported separately as
`protected_unvouched`: an audit of the protected surface (the report's
"no command reports a file ... inside `write_set.protected_roots`" half),
deliberately non-blocking, because the check cannot distinguish a
project's own protected files from an agent's intrusion into a protected
area and must not fail closed on the former.

Read-only by construction: this module walks the workspace and reads the
ownership manifest; it never writes.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Reuses validate_work_package's product-automaton glob engine rather than
# a second matcher: pattern/literal coverage is exactly pattern/pattern
# overlap with the literal side unable to wildcard, so "is this concrete
# file under this allowed/protected pattern" is the same engine chainlink
# #14's review chain hardened and #35's renderer-integrity check already
# proves coverage with.
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


@dataclass
class WriteSetViolation:
    """One write-set violation: a file no declaration accounts for, or a
    write-set declaration that enforces nothing."""

    path: str
    reason: str


@dataclass
class WriteSetReport:
    """The verdict for one workspace against its descriptor's write set.

    `state` is `clean` (every file accounted for), `violations` (out-of-
    set files or a vacuous declaration), or `unknown` (no valid project
    descriptor -- the write set cannot be evaluated). `violations` carries
    the blocking findings; `protected_unvouched` is the non-blocking
    protected-surface audit.
    """

    state: str
    details: str
    violations: list[WriteSetViolation] = field(default_factory=list)
    protected_unvouched: list[str] = field(default_factory=list)


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
    places the pipeline writes, not the implementing agent."""
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
    data = _read_json(workspace / "ci" / "manifest" / "installation.json")
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


def check_write_set(workspace: Path, descriptor: dict | None, descriptor_path: Path) -> WriteSetReport:
    """The write-set verdict for `workspace` against its descriptor.

    `descriptor` is None when no schema-valid descriptor could be loaded
    (absent, invalid, unreadable); the report is then honestly `unknown`
    -- the write set cannot be evaluated, never silently clean.
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
            )
        )

    managed = _managed_paths(workspace)
    user_owned = _user_owned_paths(workspace, descriptor, descriptor_path)
    spec_trees = _spec_tree_prefixes(descriptor)
    port_prefix = _port_source_prefix(workspace, descriptor)

    out_of_set: list[WriteSetViolation] = []
    protected_unvouched: list[str] = []
    for rel in _walk_files(workspace):
        if port_prefix is not None and _under_prefix(rel, port_prefix):
            continue
        if any(_pattern_covers_path(pattern, rel) for pattern in allowed_roots):
            continue
        if any(_pattern_covers_path(pattern, rel) for pattern in protected_roots):
            # A protected file the workspace itself vouches for (product-
            # managed, user-owned, a declared spec artifact, or a
            # pipeline-written canonical location) is accounted for; a
            # protected file nothing vouches for is the audit category.
            if (
                rel in managed
                or rel in user_owned
                or any(_under_prefix(rel, prefix) for prefix in spec_trees)
                or _is_canonical(rel)
            ):
                continue
            protected_unvouched.append(rel)
            continue
        if (
            rel in managed
            or rel in user_owned
            or any(_under_prefix(rel, prefix) for prefix in spec_trees)
            or _is_canonical(rel)
        ):
            continue
        out_of_set.append(
            WriteSetViolation(
                path=rel,
                reason=(
                    "outside every allowed_roots pattern and not accounted for by "
                    "protected_roots, the ownership manifest, a declared crate's specs/ "
                    "tree, or a canonical pipeline location"
                ),
            )
        )

    violations.extend(out_of_set)

    if violations:
        listed = ", ".join(v.path for v in violations[:_DETAILS_LIST_LIMIT])
        more = f" (+{len(violations) - _DETAILS_LIST_LIMIT} more)" if len(violations) > _DETAILS_LIST_LIMIT else ""
        details = f"{len(violations)} write-set violation(s): {listed}{more}"
        if protected_unvouched:
            details += (
                f"; {len(protected_unvouched)} file(s) inside protected roots are not "
                "product-managed (audit only)"
            )
        return WriteSetReport(
            state="violations",
            details=details,
            violations=violations,
            protected_unvouched=protected_unvouched,
        )

    details = (
        "write set is clean -- every file is accounted for by allowed_roots, protected_roots, "
        "the ownership manifest, a declared crate's specs/ tree, or a canonical pipeline location"
    )
    if protected_unvouched:
        details += (
            f"; {len(protected_unvouched)} file(s) inside protected roots are not "
            "product-managed (audit only)"
        )
    return WriteSetReport(
        state="clean",
        details=details,
        protected_unvouched=protected_unvouched,
    )
