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
  - allowed_write_set and protected_write_set don't overlap, using real
    glob-pattern intersection -- an external review found the original
    version only checked literal string equality, so "scripts/dummy_gate.py"
    (allowed) against "scripts/**" (protected) reported zero findings.
  - every trusted_assumptions[].assumption_ref resolves to EXACTLY one
    real boundary contract, matched by that contract's own declared
    boundary_id field (not a filename glob built from untrusted input --
    an external review changed a boundary_id to "*" and it resolved
    successfully), with an assumptions[] entry matching the declared
    tracking_issue and assumption_hash. Missing entirely when
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
from schema_utils import make_validator  # noqa: E402
from validate_boundary_naming import find_boundary_files  # noqa: E402

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
# Glob-pattern intersection for write-set overlap (finding 1).
#
# A naive "does A match B" check is wrong in both directions: literal
# strings need to be tested against the OTHER side's pattern, and two
# wildcarded patterns need real intersection, not a substring/prefix
# guess -- a plain "shared static prefix" heuristic would falsely flag
# "crates/*/src/**" against "crates/*/specs/**" as overlapping (same
# prefix "crates/", genuinely disjoint subtrees). This is a small
# segment-by-segment product-automaton check over the restricted glob
# grammar this schema actually uses (literal segments, "*" for one
# segment, "**" for zero or more), which is exact for that grammar.
# ---------------------------------------------------------------------------


def _segments(pattern: str) -> list[str]:
    """A trailing "/" means "this directory and everything under it" --
    every allowed/protected example throughout plan.md uses that
    convention for directories (vs. an explicit "**" or a bare filename)."""
    is_dir_prefix = pattern.endswith("/")
    segs = [s for s in pattern.split("/") if s != ""]
    if is_dir_prefix:
        segs.append("**")
    return segs


def _segment_compatible(x: str, y: str) -> bool:
    if x in ("*", "**") or y in ("*", "**"):
        return True
    return x == y


def _patterns_can_overlap(a: str, b: str) -> bool:
    a_segs, b_segs = _segments(a), _segments(b)
    memo: dict[tuple[int, int], bool] = {}

    def rec(i: int, j: int) -> bool:
        key = (i, j)
        if key in memo:
            return memo[key]
        if i == len(a_segs) and j == len(b_segs):
            result = True
        elif i == len(a_segs):
            result = all(s == "**" for s in b_segs[j:])
        elif j == len(b_segs):
            result = all(s == "**" for s in a_segs[i:])
        else:
            result = (
                (_segment_compatible(a_segs[i], b_segs[j]) and rec(i + 1, j + 1))
                or (a_segs[i] == "**" and rec(i + 1, j))
                or (b_segs[j] == "**" and rec(i, j + 1))
            )
        memo[key] = result
        return result

    return rec(0, 0)


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


def check_trusted_assumptions(path: Path, data: dict, specs_search_root: Path | None) -> list[Finding]:
    """§10.1: every assumption_ref resolves to a real boundary + tracking
    issue + hash. Resolution is by the boundary contract's own declared
    boundary_id field, matched exactly -- never by interpolating
    untrusted boundary_id text into a glob pattern (an earlier version
    did exactly that; a boundary_id of "*" matched every boundary file in
    the tree). Requires exactly one match: zero is dangling, more than
    one is ambiguous, neither is a pass.

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

    boundaries_by_id: dict[str, list[Path]] = {}
    for bpath in boundary_files:
        if bpath.suffix != ".json":
            continue
        try:
            bdata = json.loads(bpath.read_text())
        except json.JSONDecodeError:
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
) -> list[Finding]:
    g1a = gate_g1a(path, data, validator)
    if g1a:
        return g1a

    findings: list[Finding] = []
    findings.extend(check_write_set_disjointness(path, data))
    findings.extend(check_gate_integrity(path, data, workspace_root))
    findings.extend(check_trusted_assumptions(path, data, specs_search_root))
    findings.extend(check_promotion_reference(path, data))
    findings.extend(check_write_set_coverage_of_functions(path, data))
    return findings


def _load_manifest(path: Path) -> dict:
    text = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        return yaml.safe_load(text)
    return json.loads(text)


def validate_file(
    path: Path, validator: Draft202012Validator, workspace_root: Path, specs_search_root: Path | None = None
) -> list[Finding]:
    try:
        data = _load_manifest(path)
    except (json.JSONDecodeError, yaml.YAMLError) as e:
        return [Finding("G1a", path, f"invalid {path.suffix or 'JSON'}: {e}")]
    return validate_data(path, data, validator, workspace_root, specs_search_root)


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
    args = parser.parse_args(argv)

    specs_search_root = args.specs_search_root if args.specs_search_root is not None else args.workspace_root

    validator = load_validator()
    findings = validate_file(args.manifest, validator, args.workspace_root, specs_search_root)
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
