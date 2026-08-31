#!/usr/bin/env python3
"""Work-package manifest validation (plan.md §10/§10.1, chainlink #14).

The artifact boundary between this pipeline and the orchestrator that
implements the Rust code -- this schema IS the interface spec for
"outputs to pass to an orchestrator." G1a (schema) first, then the
§10.1 validator rules that are genuinely mechanical today:

  - gate_integrity hashes match the real files on disk BEFORE any gate
    runs (G13 pre-flight) -- computed and compared, not just structurally
    shaped, and the resolved path must stay inside workspace_root (an
    external review found gate_integrity accepted an absolute path or a
    ../ traversal that escaped the workspace entirely).
  - allowed_write_set and protected_write_set patterns are workspace-
    relative (check_write_set_anchoring rejects an absolute pattern or a
    literal ".." segment anywhere -- an external review found
    allowed_write_set: ["../outside-worktree/**"] passed with zero errors,
    the write-policy equivalent of the gate_integrity path escape fixed
    below) and don't overlap with each other, using real glob-pattern
    intersection (_glob_language_overlap): a product-automaton BFS
    reachability check, exact for the literal/*/** grammar this schema
    uses, applied at both the path-segment level ("**") and, within one
    segment, the character level ("*"/"?", so "docs/*-schema.json" is
    handled correctly too, not just whole-segment wildcards). Two review
    rounds found real gaps in the overlap check itself: literal-string-only
    comparison first (allowed=scripts/dummy_gate.py vs protected=scripts/**
    reported clean), then a missing "consume a token while remaining on
    the star" transition in the very first fix (scripts/subdir/tool.py vs
    scripts/** still reported clean, since ** only ever matched zero or
    exactly one segment).
  - every trusted_assumptions[].assumption_ref resolves to EXACTLY one
    real, CANONICAL boundary contract: matched by that contract's own
    declared boundary_id field (not a filename glob built from untrusted
    input -- an earlier version let boundary_id: "*" resolve successfully),
    required to pass both naming/layout (#8's check) and full G1a schema
    validation (an earlier version trusted any JSON file with a matching
    field anywhere under a directory named _boundaries -- reproduced with
    junk/not-a-crate/_boundaries/anything.json), and restricted to exactly
    the canonical crate boundary directories the caller supplies. This is
    always enforced from pipeline.py (which derives the directories from
    the project descriptor automatically); the standalone CLI below
    requires the invoker to explicitly choose --descriptor or
    --allowed-boundary-dir -- an external review found this file's own
    main() always passed allowed_boundary_dirs=None, silently
    reintroducing the "any directory named _boundaries, anywhere" gap
    outside pipeline.py even after the library-level fix landed. A first
    attempt at fixing that added a third, named opt-out flag
    (--allow-any-crate-boundary); a second review round found that flag
    was the identical trust gap behind an explicit switch (reproduced end
    to end) and it was removed rather than kept as a documented escape
    hatch -- the same call already made for review_checkpoint.py's
    standalone `approve` (#39: removed outright, not offered with
    --skip-validation). There is no way to skip this restriction from the
    standalone CLI, only ways to supply it. Missing specs_search_root entirely when
    trusted_assumptions is non-empty is a hard error, not an optional
    enrichment that degrades to a footnote -- this is one of this
    validator's three claimed mechanical guarantees.
  - harness names contain no wildcard characters (schema-enforced,
    exact_harness_name $def).

Accepts both .json and .yaml/.yml manifests -- plan.md §10's own worked
example is `ci/manifest/WP-MCMC-004.yaml`.

NOT implemented, and not silently skipped either -- both report a visible
info finding instead of passing or failing silently:
  - provenance.promotion_id resolving against a real promotion receipt:
    chainlink #15 (detached promotion receipt) doesn't exist yet, so
    there is nothing to resolve against.
  - "every owned function's source path is covered by allowed_write_set":
    functions[] are Rust module paths (e.g. scheduler::TaskQueue::pop_ready)
    with no schema-computable mapping to a file path without actually
    searching real crate source, which doesn't exist in this workspace.
    Reported as a known gap, not faked as either a pass or a fail.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
from project_descriptor import ProjectDescriptorError  # noqa: E402
from project_descriptor import boundary_dirs_for_descriptor  # noqa: E402
from project_descriptor import load_project_descriptor  # noqa: E402
from schema_utils import make_validator  # noqa: E402
from validate_boundary_naming import check_file as check_boundary_naming  # noqa: E402
from validate_boundary_naming import find_boundary_files  # noqa: E402
from validate_boundary_contracts import load_validator as load_boundary_validator  # noqa: E402

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "docs" / "work-package-manifest-schema.json"


@dataclass
class Finding:
    gate: str
    path: Path
    reason: str
    severity: str = "error"

    def __str__(self) -> str:
        return f"[{self.gate}/{self.severity}] {self.path}: {self.reason}"


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def load_validator() -> Draft202012Validator:
    return make_validator(load_schema())


def gate_g1a(path: Path, data: dict, validator: Draft202012Validator) -> list[Finding]:
    return [Finding("G1a", path, e.message) for e in validator.iter_errors(data)]


# ---------------------------------------------------------------------------
# Glob-pattern intersection for write-set overlap (finding 1, and its
# follow-up review round).
#
# A naive "does A match B" check is wrong in both directions: literal
# strings need to be tested against the OTHER side's pattern, and two
# wildcarded patterns need real intersection, not a substring/prefix
# guess -- a plain "shared static prefix" heuristic would falsely flag
# "crates/*/src/**" against "crates/*/specs/**" as overlapping (same
# prefix "crates/", genuinely disjoint subtrees).
#
# _glob_language_overlap is BFS reachability over the product automaton of
# two simple glob patterns: `star` matches zero or more arbitrary tokens
# (a Kleene star with a self-loop AND an epsilon exit -- the first review
# round's recursion only modeled the epsilon exit, so "scripts/**" never
# matched more than one segment past "scripts/"; a second, unrelated-looking
# review round caught the same missing self-loop transition), `any_one`
# matches exactly one arbitrary token, anything else must match exactly.
# This one function is reused at two levels of the same restricted glob
# grammar: path segments (star="**", any_one=None -- a bare "*" segment is
# just delegated to the character level below, where its meaning as "any
# run of characters" falls out naturally) and, within a single segment,
# characters (star="*", any_one="?"), so "docs/*-schema.json" -- an inline
# wildcard mixed with literal text inside one segment, not a whole-segment
# "*" -- is handled by the exact same mechanism instead of a second,
# separately-buggy special case.
# ---------------------------------------------------------------------------


def _glob_language_overlap(a_tokens: list, b_tokens: list, star, any_one=None) -> bool:
    from collections import deque

    target = (len(a_tokens), len(b_tokens))
    if target == (0, 0):
        return True

    def compatible(x, y) -> bool:
        if x == star or y == star or x == any_one or y == any_one:
            return True
        return x == y

    seen = {(0, 0)}
    queue = deque([(0, 0)])
    while queue:
        i, j = queue.popleft()
        successors = []
        if i < len(a_tokens) and a_tokens[i] == star:
            successors.append((i + 1, j))  # ** matches zero segments -- skip it
        if j < len(b_tokens) and b_tokens[j] == star:
            successors.append((i, j + 1))
        if i < len(a_tokens) and j < len(b_tokens) and compatible(a_tokens[i], b_tokens[j]):
            # One shared token consumed. A `star` token consumes via its
            # self-loop and stays put (it may consume more later); anything
            # else consumes exactly once and advances.
            a_next = i if a_tokens[i] == star else i + 1
            b_next = j if b_tokens[j] == star else j + 1
            successors.append((a_next, b_next))
        for s in successors:
            if s == target:
                return True
            if s not in seen:
                seen.add(s)
                queue.append(s)
    return False


def _segments_overlap(seg_a: str, seg_b: str) -> bool:
    """Character-level intersection within a single path segment -- "*"
    here means "any run of characters" (including zero, so a bare "*"
    segment correctly reduces to "matches any segment content" with no
    special-casing needed), "?" means exactly one arbitrary character."""
    if seg_a == seg_b:
        return True
    return _glob_language_overlap(list(seg_a), list(seg_b), star="*", any_one="?")


def _segments(pattern: str) -> list[str]:
    """A trailing "/" means "this directory and everything under it" --
    every allowed/protected example throughout plan.md uses that
    convention for directories (vs. an explicit "**" or a bare filename)."""
    is_dir_prefix = pattern.endswith("/")
    segs = [s for s in pattern.split("/") if s != ""]
    if is_dir_prefix:
        segs.append("**")
    return segs


def _patterns_can_overlap(a: str, b: str) -> bool:
    a_segs, b_segs = _segments(a), _segments(b)

    def path_level_compatible(seg_a: str, seg_b: str) -> bool:
        if seg_a == "**" or seg_b == "**":
            return True
        return _segments_overlap(seg_a, seg_b)

    # Path-segment level reuses the same automaton, with "**" as the
    # segment-level star and per-segment compatibility delegated to the
    # character-level check above instead of a fixed any_one token.
    target = (len(a_segs), len(b_segs))
    if target == (0, 0):
        return True
    from collections import deque

    seen = {(0, 0)}
    queue = deque([(0, 0)])
    while queue:
        i, j = queue.popleft()
        successors = []
        if i < len(a_segs) and a_segs[i] == "**":
            successors.append((i + 1, j))
        if j < len(b_segs) and b_segs[j] == "**":
            successors.append((i, j + 1))
        if i < len(a_segs) and j < len(b_segs) and path_level_compatible(a_segs[i], b_segs[j]):
            a_next = i if a_segs[i] == "**" else i + 1
            b_next = j if b_segs[j] == "**" else j + 1
            successors.append((a_next, b_next))
        for s in successors:
            if s == target:
                return True
            if s not in seen:
                seen.add(s)
                queue.append(s)
    return False


def _pattern_escapes_workspace(pattern: str) -> bool:
    """Syntactic, not filesystem resolution -- these are glob patterns
    (may contain */**), so Path.resolve() doesn't mean anything useful
    for one. An absolute pattern or a literal ".." segment anywhere is
    the write-set-pattern equivalent of the gate_integrity path escape
    (external review, high severity): "../outside-worktree/**" in
    allowed_write_set reported zero findings before this check existed."""
    if pattern.startswith("/"):
        return True
    return ".." in pattern.split("/")


def check_write_set_anchoring(path: Path, data: dict) -> list[Finding]:
    findings: list[Finding] = []
    for field in ("allowed_write_set", "protected_write_set"):
        for pattern in data["write_policy"][field]:
            if _pattern_escapes_workspace(pattern):
                findings.append(
                    Finding(
                        "G13", path,
                        f"{field}: {pattern!r} is not workspace-relative -- absolute paths "
                        "and .. segments are refused, not resolved",
                    )
                )
    return findings


def check_write_set_disjointness(path: Path, data: dict) -> list[Finding]:
    allowed = data["write_policy"]["allowed_write_set"]
    protected = data["write_policy"]["protected_write_set"]
    conflicts = [
        (a, p) for a in allowed for p in protected if _patterns_can_overlap(a, p)
    ]
    if not conflicts:
        return []
    detail = "; ".join(f"{a!r} overlaps protected {p!r}" for a, p in conflicts)
    return [
        Finding("G13", path, f"allowed_write_set overlaps protected_write_set: {detail}")
    ]


def _sha256(file_path: Path) -> str:
    return "sha256:" + hashlib.sha256(file_path.read_bytes()).hexdigest()


def check_gate_integrity(path: Path, data: dict, workspace_root: Path) -> list[Finding]:
    """G13 pre-flight: every gate implementation hash must match before
    any gate runs. An implementing agent that can edit scripts/ can
    otherwise make a wrong feature look right -- and a gate_integrity
    entry that resolves outside the workspace (absolute path, or a ../
    traversal) is checking someone else's file, not this project's, which
    is just as dangerous as not checking at all."""
    findings: list[Finding] = []
    workspace_resolved = workspace_root.resolve()
    for entry in data["gate_integrity"]:
        runner_path = (workspace_root / entry["runner"]).resolve()
        try:
            runner_path.relative_to(workspace_resolved)
        except ValueError:
            findings.append(
                Finding(
                    "G13", path,
                    f"gate_integrity runner escapes the workspace: {entry['runner']!r} "
                    f"resolves to {runner_path}, outside {workspace_resolved}",
                )
            )
            continue
        if not runner_path.is_file():
            findings.append(
                Finding("G13", path, f"gate_integrity runner not found: {entry['runner']}")
            )
            continue
        actual = _sha256(runner_path)
        if actual != entry["hash"]:
            findings.append(
                Finding(
                    "G13",
                    path,
                    f"gate_integrity hash mismatch for {entry['runner']}: "
                    f"declared {entry['hash']}, actual {actual}",
                )
            )
    return findings


def check_trusted_assumptions(
    path: Path,
    data: dict,
    specs_search_root: Path | None,
    allowed_boundary_dirs: list[Path] | None = None,
) -> list[Finding]:
    """§10.1: every assumption_ref resolves to a real boundary + tracking
    issue + hash. Resolution is by the boundary contract's own declared
    boundary_id field, matched exactly -- never by interpolating
    untrusted boundary_id text into a glob pattern (an earlier version
    did exactly that; a boundary_id of "*" matched every boundary file in
    the tree). Requires exactly one match: zero is dangling, more than
    one is ambiguous, neither is a pass.

    A candidate file only counts as "a real boundary contract" if it
    passes naming/layout (#8's check_boundary_naming: flat, __to__,
    boundary_id == filename stem) AND full G1a schema validation -- an
    external review placed a schema-shaped-enough JSON file at
    junk/not-a-crate/_boundaries/anything.json and it resolved
    successfully, because the only prior requirement was "some JSON file
    under some directory literally named _boundaries, anywhere in the
    tree" (deliberately broad for #8's own naming-violation-detection
    purpose, but wrong to inherit here where the goal is trusting the
    content). When allowed_boundary_dirs is given (pipeline.py supplies
    the project descriptor's declared <crate_dir>/specs/_boundaries
    directories), candidates are further restricted to exactly those
    directories -- the same anchoring discipline as the boundary-contract
    approval dispatcher.

    Missing specs_search_root while trusted_assumptions is non-empty is a
    hard error, not an info-severity footnote -- this check is one of
    this validator's three claimed mechanical guarantees, not a
    best-effort enrichment like the boundary applies_to check elsewhere
    in this pipeline. An earlier version degraded it to non-blocking,
    which let `validate-work-package` print OK without ever performing
    the check it claims to perform."""
    assumptions = data["definition_of_done"]["trusted_assumptions"]
    if not assumptions:
        return []

    if specs_search_root is None:
        return [
            Finding(
                "10.1", path,
                f"{len(assumptions)} trusted_assumptions present but no specs_search_root "
                "given -- assumption_ref resolution cannot be skipped, it is one of this "
                "validator's core guarantees",
            )
        ]

    try:
        boundary_files = find_boundary_files(specs_search_root)
    except FileNotFoundError as e:
        return [Finding("10.1", path, f"specs_search_root: {e}")]

    allowed_dirs_resolved = (
        {d.resolve() for d in allowed_boundary_dirs} if allowed_boundary_dirs is not None else None
    )
    boundary_validator = load_boundary_validator()

    boundaries_by_id: dict[str, list[Path]] = {}
    for bpath in boundary_files:
        if bpath.suffix != ".json":
            continue
        if allowed_dirs_resolved is not None and bpath.resolve().parent not in allowed_dirs_resolved:
            continue
        if check_boundary_naming(bpath):  # non-empty violations -> not canonical
            continue
        try:
            bdata = json.loads(bpath.read_text())
        except json.JSONDecodeError:
            continue
        if list(boundary_validator.iter_errors(bdata)):  # fails G1a -> not trustworthy content
            continue
        bid = bdata.get("boundary_id")
        if isinstance(bid, str):
            boundaries_by_id.setdefault(bid, []).append(bpath)

    findings: list[Finding] = []
    for entry in assumptions:
        ref = entry["assumption_ref"]
        boundary_id = ref["boundary_id"]
        matches = boundaries_by_id.get(boundary_id, [])

        if not matches:
            findings.append(
                Finding(
                    "10.1", path,
                    f"assumption_ref {boundary_id!r} does not resolve to any boundary contract",
                )
            )
            continue
        if len(matches) > 1:
            findings.append(
                Finding(
                    "10.1", path,
                    f"assumption_ref {boundary_id!r} resolves ambiguously to "
                    f"{len(matches)} boundary contracts declaring the same boundary_id: "
                    f"{[str(m) for m in sorted(matches)]}",
                )
            )
            continue

        boundary = json.loads(matches[0].read_text())
        boundary_assumptions = boundary.get("assumptions", [])
        match = next(
            (
                a for a in boundary_assumptions
                if a.get("tracking_issue") == ref["tracking_issue"]
                and a.get("assumption_hash") == ref["assumption_hash"]
            ),
            None,
        )
        if match is None:
            findings.append(
                Finding(
                    "10.1", path,
                    f"assumption_ref {boundary_id!r} resolves, but no assumption in it matches "
                    f"tracking_issue={ref['tracking_issue']!r} assumption_hash={ref['assumption_hash']!r}",
                )
            )
    return findings


def check_promotion_reference(path: Path, data: dict) -> list[Finding]:
    return [
        Finding(
            "10.1", path,
            f"provenance.promotion_id {data['provenance']['promotion_id']!r} cannot be resolved "
            "against a real promotion receipt yet -- chainlink #15 (detached promotion receipt) "
            "is not implemented",
            severity="info",
        )
    ]


def check_write_set_coverage_of_functions(path: Path, data: dict) -> list[Finding]:
    return [
        Finding(
            "10.1", path,
            "\"every owned function's source path is covered by allowed_write_set\" is not "
            "checked -- functions[] are Rust module paths with no schema-computable mapping to "
            "a file path without searching real crate source, which this workspace doesn't have",
            severity="info",
        )
    ]


def validate_data(
    path: Path,
    data: dict,
    validator: Draft202012Validator,
    workspace_root: Path,
    specs_search_root: Path | None = None,
    allowed_boundary_dirs: list[Path] | None = None,
) -> list[Finding]:
    g1a = gate_g1a(path, data, validator)
    if g1a:
        return g1a

    findings: list[Finding] = []
    findings.extend(check_write_set_anchoring(path, data))
    findings.extend(check_write_set_disjointness(path, data))
    findings.extend(check_gate_integrity(path, data, workspace_root))
    findings.extend(check_trusted_assumptions(path, data, specs_search_root, allowed_boundary_dirs))
    findings.extend(check_promotion_reference(path, data))
    findings.extend(check_write_set_coverage_of_functions(path, data))
    return findings


def _load_manifest(path: Path) -> dict:
    text = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        return yaml.safe_load(text)
    return json.loads(text)


def validate_file(
    path: Path,
    validator: Draft202012Validator,
    workspace_root: Path,
    specs_search_root: Path | None = None,
    allowed_boundary_dirs: list[Path] | None = None,
) -> list[Finding]:
    try:
        data = _load_manifest(path)
    except (json.JSONDecodeError, yaml.YAMLError) as e:
        return [Finding("G1a", path, f"invalid {path.suffix or 'JSON'}: {e}")]
    return validate_data(path, data, validator, workspace_root, specs_search_root, allowed_boundary_dirs)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("manifest", type=Path, help="Path to a work-package manifest .json or .yaml file")
    parser.add_argument("--workspace-root", type=Path, default=Path("."))
    parser.add_argument(
        "--specs-search-root",
        type=Path,
        default=None,
        help="Defaults to --workspace-root if not given -- omitting this never silently "
        "disables assumption-ref resolution when trusted_assumptions is non-empty.",
    )
    parser.add_argument(
        "--descriptor",
        type=Path,
        default=None,
        help="Project descriptor to derive canonical <crate_dir>/specs/_boundaries "
        "directories from -- mutually exclusive with --allowed-boundary-dir.",
    )
    parser.add_argument(
        "--allowed-boundary-dir",
        action="append",
        default=[],
        help="A canonical boundary directory to trust for assumption-ref resolution "
        "(repeatable). Mutually exclusive with --descriptor.",
    )
    args = parser.parse_args(argv)

    if args.descriptor is not None and args.allowed_boundary_dir:
        print("error: --descriptor and --allowed-boundary-dir are mutually exclusive", file=sys.stderr)
        return 2

    if args.descriptor is not None:
        try:
            descriptor = load_project_descriptor(args.descriptor)
        except ProjectDescriptorError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        allowed_boundary_dirs = boundary_dirs_for_descriptor(descriptor, args.workspace_root)
    elif args.allowed_boundary_dir:
        allowed_boundary_dirs = [Path(d) for d in args.allowed_boundary_dir]
    else:
        # No --allow-any-crate-boundary escape hatch here on purpose: a
        # second review round found the first version's opt-out was the
        # same trust gap behind a flag, reproduced it end to end, and
        # invoked the same call already made for review_checkpoint.py's
        # standalone `approve` (removed outright, not offered with
        # --skip-validation) -- an explicit bypass is still a bypass.
        # --descriptor and --allowed-boundary-dir cover every legitimate
        # standalone use; there is no third option.
        print(
            "error: one of --descriptor or --allowed-boundary-dir (repeatable) is "
            "required -- there is no way to skip canonical-crate restriction from this "
            "CLI, only a way to supply it (external review, medium severity: an earlier "
            "--allow-any-crate-boundary opt-out was found to be the same trust gap behind "
            "an explicit flag, and was removed rather than hardened)",
            file=sys.stderr,
        )
        return 2

    specs_search_root = args.specs_search_root if args.specs_search_root is not None else args.workspace_root

    validator = load_validator()
    findings = validate_file(args.manifest, validator, args.workspace_root, specs_search_root, allowed_boundary_dirs)
    errors = [f for f in findings if f.severity == "error"]
    infos = [f for f in findings if f.severity == "info"]

    if infos:
        print(f"INFO: {len(infos)} non-blocking finding(s)")
        for f in infos:
            print(f"  - {f}")

    if not errors:
        print("OK: work package manifest passes G1a and §10.1 checks")
        return 0

    print(f"FAIL: {len(errors)} finding(s)")
    for f in errors:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
