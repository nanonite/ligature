#!/usr/bin/env python3
"""`ligature scaffold-crate`: a sanctioned, deterministic crate bootstrap (chainlink #115).

`protected_roots` is enforced (#103), and a crate's own `Cargo.toml` is one of
the patterns every real project declares -- `**/Cargo.toml`, `.github/**`,
`rust-toolchain.toml`, `build.rs`. #114 gave a protected write that is
*genuinely required* a route out of that dead end (an issue-scoped capability
record, `write_authorization`), but it left the *shape* of the work
unaddressed: a port-mode pilot whose declared crate does not exist yet has no
manifest, and until it does every downstream command in the chain
(`extract-c-static`, `validate`, `gate g14`) has nothing to read. The only
supported bootstrap was a human writing the manifest by hand under
`authorize-write`, which is exactly the non-deterministic, unrecorded, outside-
every-tool edit the whole capability-record mechanism exists to avoid.

So this module owns the *generator*: `ligature scaffold-crate --issue N --crate
<name>` writes the two files a missing crate needs and nothing else, deriving
both from the project descriptor alone.

**What it may write, exactly: two files.**

  * `<crate_dir>/Cargo.toml` -- a protected-root write, and the ONLY write this
    command is ever authorized for. It requires an active, issue-scoped
    `write` grant at exactly that path (`write_authorization`), recomputed by
    the same reader `write_set` uses, so the capability this command consumes
    is the same one the conformance check honors.
  * `<crate_dir>/src/lib.rs` -- inside `allowed_roots`, so it needs no grant,
    and never overwritten once it exists (see below).

That closed list is the whole of the command's write surface, and it is why it
can never write implementation code or normative policy/spec content: there is
no flag that accepts manifest text, no flag that names an extra file, and no
code path that emits anything into `specs/`, `docs/`, `evidence/` or `ci/`.
Both target paths are additionally refused when they land in one of those
locations -- checked against `write_set`'s own predicates rather than a
second, free-to-drift copy of them.

**Determinism.** `render_manifest()` and `render_source_skeleton()` are pure
functions of the descriptor's declared crate entry and the two constants
below. Nothing is read from the filesystem to decide what to emit: no clock, no
ambient manifest, no neighbouring crate, no environment. The same descriptor
therefore yields byte-identical files, and the reported `after_hash` of a
second run equals the first's `before_hash` -- which is what makes the command
idempotent rather than merely re-runnable.

**Idempotent, or it fails clearly.** A manifest already on disk with exactly
the rendered bytes is reported `unchanged` and nothing is written (exit 0). A
manifest already on disk with *different* bytes is refused outright, never
overwritten: a `Cargo.toml` is a build input somebody may already depend on,
and a scaffolder that rewrites one is the tool's version of the hand-edit it
was built to replace. `src/lib.rs` is the mirror image: an existing file there
is reported `preserved` with its hashes and left alone, because `<crate>/src/`
is inside `allowed_roots` and its content belongs to the implementing agent.

**Verified, not asserted.** After writing, this module re-derives every claim
it makes and reports each as a named check in the receipt
(`docs/scaffold-receipt-schema.json`): the descriptor is still schema-valid
(doctor's own gate, read off the file this command used); the manifest parses
as TOML and its `[package]`/`[lib]` tables say what this module says it wrote,
byte for byte; Cargo's offline metadata reader recognizes the package as a
member of its workspace; the source skeleton exists and is non-empty;
`write_set`'s own `check_write_set` reports the write it just made in
`authorized_writes` under *this* grant id -- the proof that the capability
record actually covered the write rather than merely existing; and
`ligature_install.inspect()` reports the installation's state. A blocking
check that fails makes the command exit 1 with the receipt still printed,
because the files are on disk at that point and saying so is the honest report.

**The residual, stated rather than hidden.** `doctor`'s verdict is *recorded*
for this command, not enforced beyond its descriptor gate: scaffold-crate
writes no managed file, no user-owned document and no installation manifest, so
every other doctor condition (`drifted`, `conflict`, an unfilled
`Policy version:` marker) is pre-existing and belongs to `migrate` /
`accept-policy`. Making them blocking here would make a bootstrap verb
unusable in a workspace that needs bootstrapping for unrelated reasons, and
would hide which command owns the condition. The two-file writes are
individually atomic but not jointly: a failure between them leaves the
manifest in place with no receipt, and the idempotence above is what makes the
re-run the recovery.

Read-only except for `scaffold_crate()`, which writes only those two files: no
descriptor, no ledger, no artifact, no artifact directory. Every refusal writes
nothing at all.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from atomic_write import write_atomically  # noqa: E402
from project_descriptor import load_project_descriptor  # noqa: E402

import ligature_install  # noqa: E402
import write_authorization  # noqa: E402
import write_set  # noqa: E402

#: The receipt `--json` emits. Not a persisted artifact -- this command records
#: no ledger line and writes no artifact -- but versioned like every other
#: document in `docs/`, because a consumer parsing it is parsing a published
#: shape (docs/trust-and-compatibility-boundaries.md §1).
SCAFFOLD_SCHEMA_VERSION = "1.0"

#: Only a Mode P (port) workspace gets this verb. #115's own scope is the
#: missing crate of a port-mode pilot; a greenfield project's crates come from
#: its own layout, and refusing rather than guessing keeps the verb's meaning
#: single.
MODE_PORT = "port"

#: Cargo's own manifest name, and the one canonical entry point of a library
#: crate's source tree. Both fixed rather than derived: the point of the
#: command is that the target is not a caller-supplied path.
MANIFEST_NAME = "Cargo.toml"
SOURCE_SKELETON_REL = "src/lib.rs"

#: The two values in the rendered manifest that no descriptor field states, so
#: both are constants of the generator rather than parameters. They are named
#: here rather than inlined into the renderer so the receipt can say which
#: version and edition it emitted without re-parsing the file.
SCAFFOLD_VERSION = "0.1.0"
SCAFFOLD_EDITION = "2021"

#: Cargo accepts `[A-Za-z][A-Za-z0-9_-]*` for a package name and warns about
#: more; the generated manifest carries the crate directory's own basename, so
#: a basename outside this set is refused rather than silently rewritten --
#: rewriting would make the emitted bytes depend on a transformation the
#: descriptor never asked for.
CARGO_PACKAGE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

_GLOB_META = frozenset("*?[]{}")
_DEFAULT_DESCRIPTOR_REL = "project-descriptor.json"

#: Only ever interpolated into a refusal message, so the example is a constant
#: rather than a read of the caller's descriptor.
_EXAMPLE_CRATE_DIR = "rust/date-creusot-core"

#: The two roles the generated files carry, in the closed vocabulary the
#: receipt's `generated[].role` uses. Exactly two, and the list is not a
#: parameter: it is what keeps "writes only a manifest and a source skeleton"
#: a fact about the module rather than a claim about its callers.
ROLE_MANIFEST = "manifest"
ROLE_SOURCE_SKELETON = "source-skeleton"

#: Per-file outcomes. `created` and `unchanged` describe the protected
#: manifest (written, or already exactly these bytes); `preserved` describes
#: the source skeleton only -- an existing file in `allowed_roots` that this
#: command never overwrites.
STATE_CREATED = "created"
STATE_UNCHANGED = "unchanged"
STATE_PRESERVED = "preserved"

OUTCOME_SCAFFOLDED = "scaffolded"
OUTCOME_UNCHANGED = "unchanged"

CHECK_OK = "ok"
CHECK_FAILED = "failed"

HASH_PREFIX = "sha256:"


class ScaffoldError(Exception):
    """A refusal from `scaffold-crate`, carrying the one line to print.

    Every refusal is about the *request* -- an issue that is not a positive
    integer, a workspace that is not in Mode P, a crate name the descriptor
    does not declare, a path that escapes or names a pipeline artifact
    location, a target no `protected_roots` pattern protects, a missing
    capability record, or an existing manifest with different bytes -- so the
    CLI answers it with the exit-code contract's invalid-input code 2, and a
    refusal writes nothing at all.
    """


# ---------------------------------------------------------------------------
# Rendering: pure functions of the declared crate entry
# ---------------------------------------------------------------------------
def render_manifest(package: str) -> str:
    """The crate manifest, as exact bytes.

    A pure function of `package` (the crate directory's basename) plus the two
    constants above -- no clock, no descriptor read, no filesystem probe. That
    is what makes a re-run over an unchanged workspace report `unchanged`
    rather than a diff, and it is why the command carries no flag for
    dependency or workspace-membership configuration: nothing derivable from
    the descriptor would be a guess, and nothing supplied by a caller would be
    deterministic. A real crate's dependencies come from a work package.
    """
    return (
        "# Scaffolded by `ligature scaffold-crate` (chainlink #115).\n"
        "#\n"
        "# Generated from this workspace's project descriptor. These exact bytes\n"
        "# are a pure function of the declared crate entry, so the scaffold is\n"
        "# reproducible: the same descriptor yields this manifest, and a re-run\n"
        "# over an unchanged workspace rewrites nothing.\n"
        "#\n"
        "# This file declares no behaviour and no dependency. Implementation and\n"
        "# dependency declarations belong to a work package (Stage 7) and the\n"
        "# ordinary worker path; scaffold-crate writes a crate manifest and its\n"
        "# canonical source skeleton, and nothing else.\n"
        "[package]\n"
        f'name = "{package}"\n'
        f'version = "{SCAFFOLD_VERSION}"\n'
        f'edition = "{SCAFFOLD_EDITION}"\n'
        "\n"
        "[lib]\n"
        f'path = "{SOURCE_SKELETON_REL}"\n'
    )


def render_source_skeleton(package: str, crate_dir: str) -> str:
    """The canonical `src/lib.rs`, as exact bytes.

    Doc comments and nothing else: a compile-valid crate root with no items,
    no lint attributes and no `unimplemented!()`. Two deliberate omissions, in
    opposite directions -- no `unimplemented!()` because a body that panics is
    implementation this command is not entitled to write, and no
    `#![deny(missing_docs)]`/`#![forbid(unsafe_code)]` because a scaffold that
    imposes lints on the port it is scaffolding hands the implementing agent a
    constraint nobody declared, and a port against a C oracle may legitimately
    need `unsafe`. Also a pure function of the declared crate entry.
    """
    return (
        f"//! `{package}` -- crate source skeleton.\n"
        "//!\n"
        "//! Scaffolded by `ligature scaffold-crate` (chainlink #115) from the crate\n"
        f"//! `{crate_dir}` this workspace's project descriptor declares.\n"
        "//!\n"
        "//! This file declares no behaviour: it is the canonical entry point a work\n"
        "//! package starts from, and the port itself is implemented through the\n"
        "//! ordinary worker/human path -- a Stage 7 work-package manifest, the\n"
        "//! validators and the gates -- never by the scaffolder.\n"
        "//!\n"
        "//! Once it exists it is never rewritten. `<crate>/src/` is inside\n"
        "//! `allowed_roots`, so its content belongs to the implementing agent, and a\n"
        "//! scaffold that overwrote an implementation would be the tool's version of\n"
        "//! the hand-edit this command exists to replace.\n"
    )


# ---------------------------------------------------------------------------
# Path and crate-name resolution
# ---------------------------------------------------------------------------
def _sha256_text(text: str) -> str:
    return HASH_PREFIX + hashlib.sha256(text.encode("utf-8")).hexdigest()


def literal_relative(raw: object, what: str) -> str:
    """`raw` as a normalized workspace-relative literal path, or refuse.

    The escape rules are `write_authorization.classify_granted_path`'s and
    `ligature_install.safe_target`'s, stated once here because this command
    must resolve three paths (the declared `crate_dir`, the request's crate
    name, the manifest) under one rule -- two hand-rolled loops would be free
    to disagree about which of them was safe, and the disagreement would only
    ever show up as one of them escaping the workspace.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ScaffoldError(
            f"{what} is required: name a crate this workspace's project descriptor "
            f"declares (crate_dir, e.g. {_EXAMPLE_CRATE_DIR}) or its final segment "
            f"(e.g. date-creusot-core)"
        )
    candidate = raw.strip()
    if candidate.startswith(("/", "~")):
        raise ScaffoldError(
            f"{what} {raw!r} must be workspace-relative: a crate lives inside this "
            "workspace, and an absolute path names a directory outside it"
        )
    if candidate.endswith("/"):
        raise ScaffoldError(
            f"{what} {raw!r} names a directory as if it were a crate name: pass the "
            "crate_dir or the crate name, with no trailing '/'"
        )
    segments = candidate.split("/")
    if any(segment == ".." for segment in segments):
        raise ScaffoldError(
            f"{what} {raw!r} escapes the workspace: a '..' segment is refused, not "
            "resolved (the same rule validate-work-package applies to a declared "
            "write-set pattern)"
        )
    normalized = "/".join(segment for segment in segments if segment not in ("", "."))
    if not normalized:
        raise ScaffoldError(f"{what} {raw!r} names no path")
    if any(ch in _GLOB_META for ch in normalized):
        raise ScaffoldError(
            f"{what} {raw!r} contains a glob character: scaffold-crate scaffolds one "
            "crate the descriptor declares, and a wildcard target is not a crate "
            "anyone declared"
        )
    return normalized


def declared_crates(descriptor: dict) -> list[tuple[str, dict]]:
    """Every crate the descriptor declares, as `(crate_dir, entry)` in
    descriptor order.

    Descriptor order rather than sorted order, so the list a refusal prints is
    the order the operator wrote it in, and the refusal is byte-identical on
    every run. Each `crate_dir` goes through `literal_relative()`, so a
    descriptor that declares an escaping or wildcard `crate_dir` is refused
    here rather than half-used.
    """
    entries: list[tuple[str, dict]] = []
    for entry in descriptor.get("crates") or []:
        if not isinstance(entry, dict):
            continue
        crate_dir = literal_relative(entry.get("crate_dir"), "a declared crate_dir")
        entries.append((crate_dir, entry))
    if not entries:
        raise ScaffoldError(
            "this project descriptor declares no crates, so there is no crate to "
            "scaffold: `crates[]` is required and non-empty in "
            "schemas/project-descriptor.schema.json"
        )
    return entries


def _declared_listing(declared: list[tuple[str, dict]]) -> str:
    return ", ".join(f"{crate_dir} ({crate_dir.rsplit('/', 1)[-1]})" for crate_dir, _ in declared)


def resolve_crate(descriptor: dict, requested: str) -> tuple[str, dict]:
    """The one declared crate `requested` names, as `(crate_dir, entry)`.

    Two accepted spellings and nothing else: the declared `crate_dir` verbatim,
    or its final segment. The package name is always taken from the *declared*
    `crate_dir` afterwards, never from the request, so `--crate rust/foo` and
    `--crate foo` cannot produce two different manifests for one crate.

    A name no crate declares, a name two of them declare alike, and a name
    that is a near miss of one are all refusals -- and the refusal names every
    declared crate, because "that crate is not in this workspace" is not
    actionable on its own.
    """
    declared = declared_crates(descriptor)
    want = literal_relative(requested, "--crate")

    by_dir = {crate_dir: entry for crate_dir, entry in declared}
    if want in by_dir:
        return want, by_dir[want]

    by_basename: dict[str, list[str]] = {}
    for crate_dir, _entry in declared:
        by_basename.setdefault(crate_dir.rsplit("/", 1)[-1], []).append(crate_dir)
    matches = by_basename.get(want, [])
    if len(matches) == 1:
        return matches[0], by_dir[matches[0]]
    if len(matches) > 1:
        raise ScaffoldError(
            f"--crate {requested!r} names {len(matches)} declared crates "
            f"({', '.join(matches)}): scaffold-crate scaffolds exactly one crate, so name "
            "the crate_dir"
        )
    raise ScaffoldError(
        f"--crate {requested!r} is not a crate this workspace's project descriptor "
        f"declares. Declared: {_declared_listing(declared)} -- a crate this workspace "
        "does not declare is not scaffolded here, because the scaffold's content and "
        "its grant target are both derived from the declaration"
    )


def package_name_for(crate_dir: str) -> str:
    """The Cargo package name a crate directory declares, or refuse.

    The basename, verbatim -- never a lowercased or hyphenated version of it.
    A rewrite would make the manifest's package name depend on a normalization
    the descriptor never performed, so two descriptors naming the same crate in
    different spellings would produce two different manifests for it.
    """
    basename = crate_dir.rsplit("/", 1)[-1]
    if not CARGO_PACKAGE_NAME.match(basename):
        raise ScaffoldError(
            f"crate_dir {crate_dir!r} ends in {basename!r}, which is not a Cargo package "
            f"name ({CARGO_PACKAGE_NAME.pattern}): the generated manifest declares the "
            "crate's own name, and silently rewriting a name would make the emitted "
            "bytes depend on a transformation the descriptor never asked for. Rename the "
            "crate directory"
        )
    return basename


# ---------------------------------------------------------------------------
# Target classification
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ScaffoldTarget:
    """The two files this command may write, and why each one is its kind.

    `protected`/`protected_by` and `grant_id` are `None` on the source
    skeleton precisely because it needs no capability record: it is inside
    `allowed_roots`. Carrying that on the receipt rather than leaving it
    implicit is the point of the whole exercise -- the manifest needs a grant,
    the skeleton does not, and a worker bootstrapping a missing crate therefore
    needs no raw `Cargo.toml` write scope to get started.
    """

    crate_dir: str
    package: str
    manifest_rel: str
    skeleton_rel: str
    protected_by: str


def classify_target(workspace: Path, descriptor: dict, crate_dir: str) -> ScaffoldTarget:
    """`crate_dir` as a scaffold target, refusing every location this command
    has no business writing.

    Reached before anything is generated, so a refusal costs no rendering and
    no filesystem write. The location predicates are `write_set`'s own
    (`_is_canonical`, `_spec_tree_prefixes`, `_port_source_prefix`,
    `_under_prefix`) rather than second copies of them: whether a path is a
    pipeline artifact location is decided by the module the conformance check
    walks the workspace with, and a private copy of that rule could disagree
    with it in exactly the direction that matters -- believing a file inside
    `ci/` or a `specs/` tree was ordinary.
    """
    package = package_name_for(crate_dir)
    manifest_rel = f"{crate_dir}/{MANIFEST_NAME}"
    skeleton_rel = f"{crate_dir}/{SOURCE_SKELETON_REL}"

    # The grant's own classification, not a re-derivation: if this path would
    # be bound as a glob rather than an exact file, the capability record a
    # bootstrap must consume could not be a bounded one.
    normalized, path_kind = write_authorization.classify_granted_path(manifest_rel)
    if normalized != manifest_rel or path_kind != write_authorization.PATH_KIND_EXACT:
        raise ScaffoldError(
            f"the scaffold target {manifest_rel!r} is not an exact workspace-relative "
            "file: a capability record for a bounded write must name exactly this file"
        )

    if write_set._is_canonical(manifest_rel):
        raise ScaffoldError(
            f"{manifest_rel} is inside a canonical pipeline artifact location (write_set's "
            "own predicate for it), which the artifact system accounts for by being there: "
            "scaffold-crate refuses it because a manifest the pipeline claims as its own "
            "output is not a crate manifest, and a grant naming it would be a permission "
            "with no work to do"
        )
    for spec_tree in write_set._spec_tree_prefixes(descriptor):
        if write_set._under_prefix(manifest_rel, spec_tree):
            raise ScaffoldError(
                f"{manifest_rel} is inside declared crate spec tree {spec_tree!r}: a spec "
                "tree is normative content, and scaffold-crate writes no normative policy "
                "or spec content -- the specs for this crate come from `ligature draft` and "
                "the `approve` checkpoint"
            )
    port_prefix = write_set._port_source_prefix(workspace, descriptor)
    if port_prefix is not None and write_set._under_prefix(crate_dir, port_prefix):
        raise ScaffoldError(
            f"crate_dir {crate_dir!r} is inside the pinned upstream checkout "
            f"({port_prefix}) declared as port_source.repository: that checkout is a "
            "read-only input, not a write target, and scaffolding into it would create a "
            "crate Cargo.toml that does not exist upstream"
        )

    write_set_decl = descriptor.get("write_set")
    write_set_decl = write_set_decl if isinstance(write_set_decl, dict) else {}
    protected_roots = [p for p in (write_set_decl.get("protected_roots") or []) if isinstance(p, str)]
    allowed_roots = [p for p in (write_set_decl.get("allowed_roots") or []) if isinstance(p, str)]

    allowed_overlap = write_authorization.protecting_pattern(allowed_roots, manifest_rel)
    if allowed_overlap is not None:
        raise ScaffoldError(
            f"{manifest_rel} is already under allowed_roots pattern {allowed_overlap!r}, so "
            "a write there is clean with no capability record at all: scaffold-crate only "
            "performs a protected write it was granted, and authorizing this one would be a "
            "permission-shaped object with no work to do"
        )
    protected_by = write_authorization.protecting_pattern(protected_roots, manifest_rel)
    if protected_by is None:
        raise ScaffoldError(
            f"no declared protected_roots pattern covers {manifest_rel}: a capability record "
            "authorizes a protected-root write and nothing else, so scaffold-crate cannot "
            "authorize this path and `ligature authorize-write --path "
            f"{manifest_rel}` would refuse it for the same reason. Either the crate "
            "directory is not where this workspace declares crates to be scaffolded, or "
            "this workspace does not protect crate manifests (which is the state "
            "#103's enforcement was meant to prevent, not to accommodate)"
        )
    return ScaffoldTarget(
        crate_dir=crate_dir,
        package=package,
        manifest_rel=manifest_rel,
        skeleton_rel=skeleton_rel,
        protected_by=protected_by,
    )


# ---------------------------------------------------------------------------
# The capability record
# ---------------------------------------------------------------------------
def authorizing_grant(
    workspace: Path,
    descriptor: dict,
    target: ScaffoldTarget,
    *,
    issue: int,
    expected_grant_id: str | None,
    now: datetime | None,
) -> write_authorization.Grant:
    """The active issue-scoped capability record authorizing the manifest
    write, or refuse naming what was there instead.

    Read through `write_authorization`'s own ledger reader and status rules --
    the same code `write_set` consumes -- so this command cannot come to
    believe a grant is live when the conformance check would report it
    expired, other-issue, duplicate, not-yet-issued or unverified-issuer. The
    ledger's own position is held too: a ledger inside a declared protected
    root yields `ledger-protected` and no grant, because a capability trail
    inside a protected root is not a capability record.
    """
    write_set_decl = descriptor.get("write_set")
    write_set_decl = write_set_decl if isinstance(write_set_decl, dict) else {}
    protected_roots = [p for p in (write_set_decl.get("protected_roots") or []) if isinstance(p, str)]

    ledger = write_authorization.read_grant_ledger(workspace)
    supervisors = write_authorization.supervisor_authorities(descriptor)
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
    active = [
        grant for grant, status in statuses if status == write_authorization.STATUS_ACTIVE
    ]
    grant = write_authorization.authorizing_grant(
        active, target.manifest_rel, write_authorization.OP_WRITE
    )
    if grant is not None:
        if expected_grant_id is not None and grant.grant_id != expected_grant_id:
            raise ScaffoldError(
                f"--grant-id {expected_grant_id!r} is not the record authorizing "
                f"{target.manifest_rel}: grant {grant.grant_id} (issue {grant.issue}, "
                f"issuer {grant.issuer!r}, expires {grant.expires_at}) is. Naming the grant "
                "in the request pins which record the operator meant, and a mismatch is a "
                "refusal rather than a silent substitution"
            )
        return grant

    issue_hint = (
        f"ligature authorize-write --issue {issue} --path {target.manifest_rel} "
        f"--op write --issuer <name>"
    )
    if ledger.error is not None:
        raise ScaffoldError(
            f"{ledger.error}; fix or remove the damaged ledger before scaffolding"
        )
    if ledger_protected is not None:
        raise ScaffoldError(
            f"the write-grant ledger {write_authorization.GRANT_LEDGER_REL} is inside declared "
            f"protected root {ledger_protected!r}, so no record in it authorizes anything "
            f"(write-set-check reports every one of them `{write_authorization.STATUS_LEDGER_PROTECTED}`): "
            "a capability trail inside a protected root is not a capability record"
        )
    nearby = [
        f"{grant.grant_id} ({status})"
        for grant, status in statuses
        if status != write_authorization.STATUS_ACTIVE and grant.covers(target.manifest_rel)
    ]
    reason = (
        f"no active write grant covers {target.manifest_rel} for issue {issue}"
        if not nearby
        else f"no active write grant covers {target.manifest_rel} for issue {issue}; the "
        f"record(s) that name it are {', '.join(sorted(nearby))} and each authorizes nothing"
    )
    raise ScaffoldError(
        f"{reason}. scaffold-crate writes only what a capability record authorizes, and it "
        f"never records one itself: have the issuer run `{issue_hint}`, then re-run this "
        "command with --issue "
        f"{issue}. No file was written"
    )


# ---------------------------------------------------------------------------
# The receipt
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GeneratedFile:
    """One file this run created, left alone, or found already correct.

    `before_hash` is `None` for a file that did not exist, and the pair is the
    run's own evidence that nothing outside the two declared targets moved:
    a consumer can compare `after_hash` across two runs, or against the
    descriptor that produced it, without trusting this report.
    """

    path: str
    role: str
    state: str
    before_hash: str | None
    after_hash: str
    size_bytes: int
    protected: bool
    protected_by: str | None
    grant_id: str | None

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "role": self.role,
            "state": self.state,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
            "size_bytes": self.size_bytes,
            "protected": self.protected,
            "protected_by": self.protected_by,
            "grant_id": self.grant_id,
        }


@dataclass(frozen=True)
class CheckResult:
    """One named verification this run performed.

    `blocking` says whether a failure of this check fails the command, so a
    receipt distinguishes "this run verified it" from "this run verified it
    and refuses to continue without it". `doctor` is the case that needs the
    distinction: its verdict is recorded because it is informative, and only
    its descriptor gate is blocking, because that is the one condition about
    the file this command read.
    """

    name: str
    state: str
    blocking: bool
    details: str

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "blocking": self.blocking,
            "details": self.details,
        }


@dataclass
class ScaffoldReport:
    """Everything one `scaffold-crate` run did and verified.

    `state` is `ok` when every blocking check passed, `failed` otherwise --
    the files are on disk either way at that point, so the receipt is printed
    in both cases. `outcome` is about the manifest alone (`scaffolded` vs
    `unchanged`), because a pre-existing `src/lib.rs` is preserved rather
    than re-scaffolded and must not read as a second successful write.
    """

    issue: int
    crate: str
    crate_dir: str
    mode: str
    outcome: str
    state: str
    grant: dict
    generated: list[GeneratedFile] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    write_set: dict = field(default_factory=dict)

    @property
    def blocking_failures(self) -> list[CheckResult]:
        return [c for c in self.checks if c.blocking and c.state == CHECK_FAILED]

    def as_dict(self) -> dict:
        return {
            "event": "crate-scaffold",
            "schema_version": SCAFFOLD_SCHEMA_VERSION,
            "issue": self.issue,
            "crate": self.crate,
            "crate_dir": self.crate_dir,
            "mode": self.mode,
            "outcome": self.outcome,
            "state": self.state,
            "grant": dict(self.grant),
            "generated": [entry.as_dict() for entry in self.generated],
            "checks": [entry.as_dict() for entry in self.checks],
            "write_set": dict(self.write_set),
        }


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ScaffoldError(f"cannot read {path}: {exc}") from exc


def _hash_bytes(raw: bytes) -> str:
    return HASH_PREFIX + hashlib.sha256(raw).hexdigest()


def _verify_workspace(workspace: Path, manifest_path: Path, package: str) -> CheckResult:
    """Ask Cargo whether the generated package belongs to a valid workspace.

    `metadata --no-deps --offline` parses the package and workspace manifests
    without resolving dependencies, compiling code, writing a lockfile, or
    contacting a registry. In particular, Cargo rejects a crate under an
    explicit workspace that was not listed in `members` (or `exclude`). The
    generated crate therefore cannot be reported successful merely because
    its own TOML parses while the surrounding Cargo workspace rejects it.
    """
    cargo = shutil.which("cargo")
    if cargo is None:
        return CheckResult(
            "workspace",
            CHECK_FAILED,
            True,
            "cargo is not installed; Cargo workspace membership could not be verified",
        )
    try:
        result = subprocess.run(
            [
                cargo,
                "metadata",
                "--no-deps",
                "--offline",
                "--format-version",
                "1",
                "--manifest-path",
                str(manifest_path),
            ],
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            "workspace", CHECK_FAILED, True,
            "cargo metadata --no-deps --offline timed out after 30 seconds",
        )
    except OSError as exc:
        return CheckResult(
            "workspace", CHECK_FAILED, True, f"cargo metadata could not run: {exc}"
        )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if len(detail) > 1200:
            detail = detail[-1200:]
        return CheckResult(
            "workspace",
            CHECK_FAILED,
            True,
            "cargo metadata --no-deps --offline rejected the generated package or its "
            f"workspace (exit {result.returncode})"
            + (f": {detail}" if detail else ""),
        )
    try:
        metadata = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return CheckResult(
            "workspace",
            CHECK_FAILED,
            True,
            f"cargo metadata returned invalid JSON: {exc}",
        )

    packages = metadata.get("packages")
    if not isinstance(packages, list):
        return CheckResult(
            "workspace", CHECK_FAILED, True,
            "cargo metadata returned no package list for the generated manifest",
        )
    expected_manifest = manifest_path.resolve()
    found = next(
        (
            entry for entry in packages
            if isinstance(entry, dict)
            and isinstance(entry.get("manifest_path"), str)
            and Path(entry["manifest_path"]).resolve() == expected_manifest
        ),
        None,
    )
    if found is None:
        return CheckResult(
            "workspace",
            CHECK_FAILED,
            True,
            f"cargo metadata did not include generated manifest {expected_manifest}",
        )
    if found.get("name") != package:
        return CheckResult(
            "workspace",
            CHECK_FAILED,
            True,
            f"cargo metadata reports package name {found.get('name')!r}, expected {package!r}",
        )
    if found.get("id") not in metadata.get("workspace_members", []):
        return CheckResult(
            "workspace",
            CHECK_FAILED,
            True,
            f"cargo metadata parsed {expected_manifest} but did not report {package!r} as a "
            "workspace member",
        )
    workspace_root = metadata.get("workspace_root")
    if not isinstance(workspace_root, str) or not workspace_root:
        return CheckResult(
            "workspace", CHECK_FAILED, True,
            "cargo metadata returned no Cargo workspace root",
        )
    return CheckResult(
        "workspace",
        CHECK_OK,
        True,
        f"cargo metadata --no-deps --offline recognizes {package!r} as a workspace member "
        f"(root: {Path(workspace_root).resolve()})",
    )


def _verify_descriptor(descriptor_path: Path) -> CheckResult:
    """The descriptor this run read is still schema-valid.

    Read through `ligature_install`'s own read-only descriptor gate -- the same
    function `doctor` decides fail-closed on -- rather than re-validating here,
    so scaffold-crate and `doctor` cannot disagree about whether this
    workspace's descriptor is loadable.
    """
    gate = ligature_install.descriptor_schema_report_at(descriptor_path)
    if gate.state == "valid":
        return CheckResult("descriptor", CHECK_OK, True, f"schema-valid: {descriptor_path}")
    return CheckResult(
        "descriptor",
        CHECK_FAILED,
        True,
        f"schema {gate.state}: {descriptor_path}"
        + ("; " + "; ".join(gate.diagnostics) if gate.diagnostics else ""),
    )


def _verify_manifest(path: Path, rendered: str, package: str) -> CheckResult:
    """The manifest on disk parses as TOML and says what this run wrote.

    Not a Cargo invocation -- none is available, and a build is not what this
    check is for. It re-parses the emitted bytes and compares every value it
    claims to have written against the file, so a partial write, a stale
    `after_hash`, or a renderer that drifted from its own receipt fails here
    rather than at the first downstream build.
    """
    raw = _read_bytes(path)
    if raw is None:
        return CheckResult("manifest", CHECK_FAILED, True, f"{path} does not exist after writing it")
    try:
        text = raw.decode("utf-8")
        data = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return CheckResult("manifest", CHECK_FAILED, True, f"{path} is not valid TOML: {exc}")
    if text != rendered:
        return CheckResult(
            "manifest",
            CHECK_FAILED,
            True,
            f"{path} does not hold the bytes this run rendered ({_hash_bytes(raw)}, expected "
            f"{_sha256_text(rendered)})",
        )
    problems: list[str] = []
    package_table = data.get("package")
    if not isinstance(package_table, dict):
        problems.append("no [package] table")
    else:
        if package_table.get("name") != package:
            problems.append(f"package.name is {package_table.get('name')!r}, expected {package!r}")
        if package_table.get("version") != SCAFFOLD_VERSION:
            problems.append(
                f"package.version is {package_table.get('version')!r}, expected "
                f"{SCAFFOLD_VERSION!r}"
            )
        if package_table.get("edition") != SCAFFOLD_EDITION:
            problems.append(
                f"package.edition is {package_table.get('edition')!r}, expected "
                f"{SCAFFOLD_EDITION!r}"
            )
    lib_table = data.get("lib")
    if not isinstance(lib_table, dict):
        problems.append("no [lib] table")
    elif lib_table.get("path") != SOURCE_SKELETON_REL:
        problems.append(
            f"lib.path is {lib_table.get('path')!r}, expected {SOURCE_SKELETON_REL!r}"
        )
    if problems:
        return CheckResult("manifest", CHECK_FAILED, True, f"{path}: " + "; ".join(problems))
    return CheckResult(
        "manifest",
        CHECK_OK,
        True,
        f"{path}: parses as TOML; [package] name={package} version={SCAFFOLD_VERSION} "
        f"edition={SCAFFOLD_EDITION}; [lib] path={SOURCE_SKELETON_REL}",
    )


def _verify_skeleton(path: Path, rendered: str, state: str) -> CheckResult:
    """The source skeleton exists and is non-empty.

    `created` additionally re-reads it and compares: a scaffold whose own
    skeleton came back different is a failed write, and reporting `ok` would
    be reporting the claim rather than the file. `preserved` reports the bytes
    found instead, which is the honest outcome for a file inside
    `allowed_roots` that this command does not own.
    """
    raw = _read_bytes(path)
    if raw is None:
        return CheckResult(
            "source-skeleton", CHECK_FAILED, True, f"{path} does not exist after this run"
        )
    if not raw.strip():
        return CheckResult(
            "source-skeleton", CHECK_FAILED, True, f"{path} is empty: a crate needs an entry point"
        )
    if state == STATE_PRESERVED:
        return CheckResult(
            "source-skeleton",
            CHECK_OK,
            True,
            f"{path}: preserved ({len(raw)} byte(s), {_hash_bytes(raw)}) -- it already existed "
            "and lives in allowed_roots, so this command never overwrites it",
        )
    if text_mismatch(raw, rendered):
        return CheckResult(
            "source-skeleton",
            CHECK_FAILED,
            True,
            f"{path} does not hold the bytes this run rendered ({_hash_bytes(raw)}, expected "
            f"{_sha256_text(rendered)})",
        )
    return CheckResult(
        "source-skeleton",
        CHECK_OK,
        True,
        f"{path}: created ({len(raw)} byte(s), {_hash_bytes(raw)}); the canonical entry point "
        f"declares no behaviour, so `cargo build` has nothing to compile yet",
    )


def text_mismatch(raw: bytes, rendered: str) -> bool:
    return raw.decode("utf-8", errors="replace") != rendered


def _verify_write_set(
    workspace: Path,
    descriptor: dict,
    descriptor_path: Path,
    target: ScaffoldTarget,
    grant: write_authorization.Grant,
    *,
    issue: int,
    now: datetime | None,
) -> tuple[CheckResult, dict]:
    """`write_set`'s own verdict, and whether it attributes this write to the
    grant this run consumed.

    This is the check that makes the capability record load-bearing rather than
    ceremonial: it asks the conformance check -- not this module's own reading
    of the ledger -- whether the file it just wrote is reported in
    `authorized_writes` under *this* grant id. A grant that exists but does not
    cover the write, and a write that landed without any declaration vouching
    for it, both fail here rather than being reported as a successful
    scaffold.
    """
    report = write_set.check_write_set(
        workspace, descriptor, descriptor_path, issue=issue, now=now
    )
    surface = next(
        (entry for entry in report.protected_surface if entry.pattern == target.protected_by),
        None,
    )
    write_set_view = {
        "state": report.state,
        "details": report.details,
        "protected_pattern": target.protected_by,
        "protected_surface": (
            {
                "pattern": surface.pattern,
                "files": surface.files,
                "violations": surface.violations,
                "unattributed": surface.unattributed,
                "authorized": surface.authorized,
            }
            if surface is not None
            else None
        ),
        "authorized_writes": [entry.as_dict() for entry in report.authorized_writes],
    }
    if report.state == "unknown":
        return CheckResult(
            "write-set",
            CHECK_FAILED,
            True,
            "write_set.check_write_set reported `unknown`: the write set could not be evaluated",
        ), write_set_view
    matching = [entry for entry in report.authorized_writes if entry.path == target.manifest_rel]
    if not matching:
        return CheckResult(
            "write-set",
            CHECK_FAILED,
            True,
            f"write_set.check_write_set (state {report.state}) does not report "
            f"{target.manifest_rel} in authorized_writes: the write this run made is not "
            f"attributed to any active capability record, so it is {report.state} for this "
            "workspace. Details: "
            + report.details,
        ), write_set_view
    entry = matching[-1]
    if entry.grant_id != grant.grant_id:
        return CheckResult(
            "write-set",
            CHECK_FAILED,
            True,
            f"write_set.check_write_set attributes {target.manifest_rel} to grant "
            f"{entry.grant_id}, but this run consumed {grant.grant_id}",
        ), write_set_view
    return CheckResult(
        "write-set",
        CHECK_OK,
        True,
        f"write_set.check_write_set: {report.state}; {target.manifest_rel} reported in "
        f"authorized_writes under {entry.grant_id} ({entry.status}), and protected pattern "
        f"{target.protected_by!r} counts {surface.authorized if surface else 0} authorized write(s)",
    ), write_set_view


def _verify_doctor(workspace: Path, descriptor_path: Path) -> CheckResult:
    """`ligature_install.inspect()`'s verdict, recorded.

    Blocking on the descriptor gate only, and deliberately not on the rest: this
    command writes no managed file, no user-owned document and no installation
    manifest, so `drifted`, `conflict` and an unfilled `Policy version:` marker
    are pre-existing conditions owned by `migrate` and `accept-policy`. Making
    them blocking would make a bootstrap verb unusable in a workspace that needs
    bootstrapping for unrelated reasons, and would hide which command fixes
    them. They are reported with their state instead -- the same
    record-don't-assume discipline `protected_unvouched` follows.

    The gate is evaluated against the descriptor *this run used*, forwarded
    explicitly, so it cannot be decided about a different file the way
    `doctor`'s pre-#109 descriptor resolution was (chainlink #109).
    """
    try:
        installation = ligature_install.inspect(workspace, descriptor=descriptor_path)
    except ligature_install.InstallError as exc:
        return CheckResult("doctor", CHECK_FAILED, True, f"installation cannot be inspected: {exc}")
    gate = installation.descriptor_gate
    state = gate.state if gate is not None else "absent"
    detail = (
        f"installation={installation.status}; descriptor gate={state}; "
        f"unfilled policy marker(s)={len(installation.policy_markers)}"
    )
    if state != "valid":
        return CheckResult(
            "doctor",
            CHECK_FAILED,
            True,
            f"{detail}; the descriptor this run read is not schema-valid to the installer either"
            + ("; " + "; ".join(gate.diagnostics) if gate is not None and gate.diagnostics else ""),
        )
    return CheckResult(
        "doctor",
        CHECK_OK,
        False,
        f"{detail}; recorded rather than enforced -- every condition other than the "
        "descriptor gate is pre-existing and belongs to `ligature migrate` / "
        "`ligature accept-policy`, and scaffold-crate writes none of the files they own",
    )


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------
def scaffold_crate(
    workspace: Path,
    *,
    issue: int,
    crate: str,
    grant_id: str | None = None,
    descriptor: dict | None = None,
    descriptor_path: Path | None = None,
    now: datetime | None = None,
) -> ScaffoldReport:
    """Write a missing crate's manifest and canonical source skeleton, and
    report what it did and what it verified.

    Every precondition is checked before anything is written, and every refusal
    is about the *request*, so a refusal leaves the workspace byte-identical:

    * `issue` is a positive integer -- a grant authorizes its own issue only,
      and a bootstrap that did not name one could not name the grant it needs;
    * the workspace exists and the descriptor loads (the same
      `ProjectDescriptorError` line every other command prints);
    * the descriptor is in Mode P (#115's scope);
    * `crate` names exactly one declared crate, spelled as its `crate_dir` or
      that directory's final segment -- never a path that escapes, is absolute
      or contains a glob;
    * the target is not a canonical pipeline artifact location, not inside a
      declared crate's `specs/` tree, not inside the pinned upstream checkout;
    * the target IS covered by a declared `protected_roots` pattern and is NOT
      already under `allowed_roots` -- the same two rules `authorize_write`
      enforces, so this command cannot perform a write no grant could
      authorize;
    * an active issue-scoped `write` grant at exactly the manifest path exists
      (`authorizing_grant`), and matches `--grant-id` when one is named;
    * an existing manifest holds exactly the bytes this run renders. Identical
      bytes are `unchanged` (exit 0, nothing written); different bytes are
      refused, never overwritten.

    Then it writes at most two files (the manifest; and `src/lib.rs` if absent)
    and verifies the result by re-deriving every claim -- see
    `_verify_descriptor`, `_verify_manifest`, `_verify_workspace`,
    `_verify_skeleton`, `_verify_write_set` and `_verify_doctor`.

    `descriptor`/`descriptor_path` default to `workspace/project-descriptor.json`,
    as every other command's do. `now` exists for the reason it does on
    `write_set`: expiry is otherwise untestable without a clock race.
    """
    if isinstance(issue, bool) or not isinstance(issue, int) or issue < 1:
        raise ScaffoldError(
            f"--issue {issue!r} is not a positive integer: a capability record is scoped to "
            "one issue, and scaffold-crate writes only what such a record authorizes -- so "
            "the issue it is scoped to is part of the request, not an inference"
        )
    workspace = workspace.resolve()
    if not workspace.is_dir():
        raise ScaffoldError(f"workspace {workspace} is not a directory")
    if descriptor is None:
        if descriptor_path is None:
            descriptor_path = workspace / _DEFAULT_DESCRIPTOR_REL
        descriptor = load_project_descriptor(descriptor_path)
    if descriptor_path is None:
        descriptor_path = workspace / _DEFAULT_DESCRIPTOR_REL

    mode = descriptor.get("mode")
    if mode != MODE_PORT:
        raise ScaffoldError(
            f"this workspace is mode {mode!r}, not {MODE_PORT!r}: scaffold-crate bootstraps the "
            "missing crate of a Mode P port pilot, whose crate manifest is a declared "
            "protected root with no route out of it under any other mode. A greenfield "
            "workspace's crates come from its own layout"
        )

    crate_dir, _entry = resolve_crate(descriptor, crate)
    target = classify_target(workspace, descriptor, crate_dir)

    manifest_path = ligature_install.safe_target(workspace, target.manifest_rel)
    skeleton_path = ligature_install.safe_target(workspace, target.skeleton_rel)
    for role, path in ((ROLE_MANIFEST, manifest_path), (ROLE_SOURCE_SKELETON, skeleton_path)):
        if path.is_dir():
            raise ScaffoldError(
                f"{path} is a directory, so the {role} target cannot be written there; remove or "
                "rename it and re-run"
            )

    grant = authorizing_grant(
        workspace,
        descriptor,
        target,
        issue=issue,
        expected_grant_id=grant_id,
        now=now,
    )

    manifest_text = render_manifest(target.package)
    skeleton_text = render_source_skeleton(target.package, crate_dir)
    existing_manifest = _read_bytes(manifest_path)
    if existing_manifest is not None and text_mismatch(existing_manifest, manifest_text):
        raise ScaffoldError(
            f"{target.manifest_rel} already exists with content this command did not write "
            f"({_hash_bytes(existing_manifest)}, whereas the scaffold renders "
            f"{_sha256_text(manifest_text)}): scaffold-crate never overwrites a crate "
            "manifest. A manifest is a build input somebody may already depend on, and "
            "rewriting one would be the tool's version of the hand-edit it exists to "
            "replace. If it is a scaffold from an earlier run of this command, it was "
            "rendered from a different crate_dir and the difference is a descriptor change, "
            "not a rewrite; otherwise delete it deliberately and re-run. No file was written"
        )

    # -- the writes, in the one order that matters --------------------------
    # The manifest first: it is the protected write, and a failure after it
    # leaves a workspace whose re-run is a no-op (`unchanged`), which is what
    # makes the two-write sequence recoverable rather than a half-scaffold with
    # no receipt. Each write is atomic on its own (atomic_write), and neither
    # touches a descriptor, a ledger or an artifact directory.
    existing_skeleton = _read_bytes(skeleton_path)
    if existing_manifest is None:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        write_atomically(manifest_path, manifest_text)
        manifest_state = STATE_CREATED
    else:
        manifest_state = STATE_UNCHANGED
    if existing_skeleton is None:
        skeleton_path.parent.mkdir(parents=True, exist_ok=True)
        write_atomically(skeleton_path, skeleton_text)
        skeleton_state = STATE_CREATED
    else:
        skeleton_state = STATE_PRESERVED

    final_manifest = _read_bytes(manifest_path) or b""
    final_skeleton = _read_bytes(skeleton_path) or b""
    generated = [
        GeneratedFile(
            path=target.manifest_rel,
            role=ROLE_MANIFEST,
            state=manifest_state,
            before_hash=_hash_bytes(existing_manifest) if existing_manifest is not None else None,
            after_hash=_hash_bytes(final_manifest),
            size_bytes=len(final_manifest),
            protected=True,
            protected_by=target.protected_by,
            grant_id=grant.grant_id,
        ),
        GeneratedFile(
            path=target.skeleton_rel,
            role=ROLE_SOURCE_SKELETON,
            state=skeleton_state,
            before_hash=_hash_bytes(existing_skeleton) if existing_skeleton is not None else None,
            after_hash=_hash_bytes(final_skeleton),
            size_bytes=len(final_skeleton),
            protected=False,
            protected_by=None,
            grant_id=None,
        ),
    ]

    write_set_check, write_set_view = _verify_write_set(
        workspace, descriptor, descriptor_path, target, grant, issue=issue, now=now
    )
    checks = [
        _verify_descriptor(descriptor_path),
        _verify_manifest(manifest_path, manifest_text, target.package),
        _verify_workspace(workspace, manifest_path, target.package),
        _verify_skeleton(skeleton_path, skeleton_text, skeleton_state),
        write_set_check,
        _verify_doctor(workspace, descriptor_path),
    ]
    blocking = [check for check in checks if check.blocking and check.state == CHECK_FAILED]
    report = ScaffoldReport(
        issue=issue,
        crate=target.package,
        crate_dir=crate_dir,
        mode=mode,
        outcome=OUTCOME_SCAFFOLDED if manifest_state == STATE_CREATED else OUTCOME_UNCHANGED,
        state=CHECK_FAILED if blocking else CHECK_OK,
        grant={
            "grant_id": grant.grant_id,
            "ledger": write_authorization.GRANT_LEDGER_REL,
            "line": grant.line,
            "path": grant.path,
            "path_kind": grant.path_kind,
            "op": grant.op,
            "issue": grant.issue,
            "issuer": grant.issuer,
            "issuer_kind": grant.issuer_kind,
            "issued_at": grant.issued_at,
            "expires_at": grant.expires_at,
            "one_shot": grant.one_shot,
        },
        generated=generated,
        checks=checks,
        write_set=write_set_view,
    )
    return report


def render_report_text(report: ScaffoldReport) -> str:
    """The human-readable receipt, one fact per line.

    Same information as the JSON, same order, and the same words for every
    state -- so an operator reading the text and a consumer reading `--json`
    cannot be told two different things about what happened.
    """
    grant = report.grant
    lines = [
        f"crate: {report.crate}",
        f"crate dir: {report.crate_dir}",
        f"mode: {report.mode}",
        f"issue: {report.issue}",
        f"grant: {grant['grant_id']} ({grant['op']}, {grant['issuer']} "
        f"({grant['issuer_kind']}), expires {grant['expires_at']}, ledger line {grant['line']})",
    ]
    for entry in report.generated:
        before = entry.before_hash or "absent"
        protected = (
            f"protected by {entry.protected_by!r}, authorized by {entry.grant_id}"
            if entry.protected
            else "allowed_roots: no capability record required"
        )
        lines.append(f"{entry.role}: {entry.path} ({entry.state}) -- {protected}")
        lines.append(f"  {before} -> {entry.after_hash} ({entry.size_bytes} bytes)")
    for check in report.checks:
        marker = "ok" if check.state == CHECK_OK else "FAILED"
        scope = "blocking" if check.blocking else "recorded"
        lines.append(f"check {check.name}: {marker} ({scope})")
        lines.append(f"  {check.details}")
    lines.append(f"write set: {report.write_set.get('state')}")
    lines.append(f"outcome: {report.outcome}")
    lines.append(f"state: {report.state}")
    if report.state == CHECK_OK:
        lines.append(
            "This scaffold declares no behaviour and no dependency, and no normative policy "
            "or spec content: implementation belongs to a work package (Stage 7) through the "
            "ordinary worker/human path."
        )
    else:
        lines.append(
            "A blocking check failed AFTER the files above were written, so the workspace is "
            "in the state this receipt describes rather than a clean scaffold. Re-run "
            f"`ligature scaffold-crate --issue {report.issue} --crate "
            f"{report.crate_dir}` once the named condition is fixed: the render is a pure "
            "function of the descriptor, so the re-run is idempotent."
        )
    return "\n".join(lines)
