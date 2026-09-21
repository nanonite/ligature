#!/usr/bin/env python3
"""G18: witness coverage gate.

plan.md §16.2's G18 row: "Every query marked witness_required has a
witness spec and a generated rendering." Stage 4, hard error.

Coverage is relative to the DECLARED feature set, exactly as R2 proves
coverage relative to accepted I and no further (plan.md §15's open
item, resolved by chainlink #33): a query nobody marked
witness_required is invisible to this gate. Completeness of the feature
set itself stays a human/promotion concern.

Reading witness_required without the vendored schema
------------------------------------------------------
witness_required is not in the vendored concept-to-code schema yet
(chainlink #33, reported upstream as docs/concept-to-code-modifications.md
gap #5 -- decided, never applied to the read-only submodule pin). That
does not block this gate: nothing in this codebase runs concept-to-code's
own JSON Schema validator against a concept spec document at all --
emit_stubs.py reads query fields with plain `.get()`
(vendor/concept-to-code/emit_stubs.py:192-201), and this codebase's own
G2 cross-reference in validate_witness.py already reads `pure` the same
untyped way (`candidate.get("pure") is not True`). This gate reads
`witness_required` by the identical direct-field-access discipline,
requiring the literal value `True` -- the same strictness `pure`
already gets, and the same strictness docs/concept-to-code-witness-
required-schema.json's proposed `type: boolean` would enforce once
upstream applies it. The pending upstream schema change is a concern
for concept-to-code's own authoring tooling; it has no bearing on how
this gate discovers the declared set today.

Discovery
---------
`discover_declared_features` scans each declared `specs_search_root`
for concept specs (mirroring validate_witness.py's own `resolve_query`
scan: skip underscore-prefixed artifact directories, since a witness
spec carries its own top-level `concept` field and an unfiltered scan
would find itself; sort so a result never depends on filesystem order)
and collects one `(concept, query_name)` pair per query with
`witness_required is True`, using `query_name_from_sig` -- the same
name extraction `resolve_query` itself uses, factored out rather than
re-derived. The SAME `(concept, query_name)` pair declared `true` in
more than one concept spec is ambiguous (which declaration is
authoritative cannot be determined) and is reported rather than
resolved from whichever file sorted first -- the same "ambiguous, never
first-wins" choice chainlink #25's `load_bridges` already makes for a
cross-crate `bridge_id` collision and #14 makes for a duplicate
obligation provider.

Coverage
--------
Checked against the "genuinely valid, not just present" bar this
codebase applies everywhere a cross-reference is trusted:
`collect_valid_witnesses` keeps only witness specs that pass
validate_witness.py's own G1a/G1b/G2 in full (a schema-invalid or
dangling witness does not count as coverage -- `validate-witness`
already reports exactly what is wrong with it; this gate only needs the
yes/no outcome), keyed by `(concept, query)` and, symmetrically with the
declared side, treated as ambiguous -- excluded, not first-wins -- if a
genuinely valid witness for the same `(concept, query)` is found under
more than one crate's canonical `_witnesses/` directory. That ambiguity
check is scoped to the declared feature set, same as everything else
this gate does: two crates each holding a perfectly ordinary duplicate
witness for a query nobody marked `witness_required` is not this gate's
business, and must not block it (external review, medium severity, on
an earlier version that raised the finding regardless of whether the
query was declared -- reproduced with zero declared features still
returning a "more than one crate" error).

A declared feature with a valid, unambiguous witness spec still needs
its declared rendering to actually exist on disk at `output.path`
(workspace-relative, plan.md §16.1) -- `generate_witness.py`'s own
atomic-write contract (chainlink #28) is what makes checking that path
meaningful: a file there is either the complete rendering or absent,
never partial, so this gate can trust a plain existence check.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from project_descriptor import is_underscore_artifact_path  # noqa: E402
from scan_summary import pass_line  # noqa: E402
from validate_witness import find_witness_files  # noqa: E402
from validate_witness import load_validator as load_witness_validator  # noqa: E402
from validate_witness import query_name_from_sig  # noqa: E402
from validate_witness import snake_case  # noqa: E402
from validate_witness import validate_data as validate_witness_data  # noqa: E402
from validate_witness import witness_dir_for  # noqa: E402

EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_INPUT_ERROR = 2


@dataclass
class Finding:
    gate: str
    subject: str
    reason: str
    severity: str = "error"

    def __str__(self) -> str:
        return f"[{self.gate}/{self.severity}] {self.subject}: {self.reason}"


def discover_declared_features(specs_search_root: Path) -> list[tuple[str, str, Path]]:
    """(concept, query_name, spec_path) for every query with
    witness_required: true under specs_search_root. Strict identity
    (`is True`), the same discipline gate_g2's own `pure` check applies
    -- absent, false, or a non-boolean value is not a declaration."""
    features: list[tuple[str, str, Path]] = []
    if not specs_search_root.is_dir():
        return features
    for spec_path in sorted(specs_search_root.glob("**/*.json")):
        if is_underscore_artifact_path(spec_path, specs_search_root):
            continue
        try:
            spec = json.loads(spec_path.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(spec, dict) or not isinstance(spec.get("concept"), str):
            continue
        if not any(key in spec for key in ("queries", "commands", "constraints", "english_description")):
            continue
        for query in spec.get("queries", []) or []:
            if not isinstance(query, dict) or query.get("witness_required") is not True:
                continue
            name = query_name_from_sig(query.get("rust_sig", ""))
            if name:
                features.append((spec["concept"], name, spec_path))
    return features


def collect_declared_features(descriptor: dict, workspace: Path) -> tuple[dict[tuple[str, str], Path], list[Finding]]:
    search_roots = sorted({(workspace / crate["specs_search_root"]).resolve() for crate in descriptor["crates"]})
    by_feature: dict[tuple[str, str], list[Path]] = {}
    for root in search_roots:
        for concept, query, spec_path in discover_declared_features(root):
            by_feature.setdefault((concept, query), []).append(spec_path)

    declared: dict[tuple[str, str], Path] = {}
    findings: list[Finding] = []
    for (concept, query), spec_paths in sorted(by_feature.items()):
        unique_paths = sorted(set(spec_paths))
        if len(unique_paths) > 1:
            findings.append(
                Finding(
                    "G18", f"{concept}.{query}",
                    "witness_required: true is declared in more than one concept spec "
                    f"({', '.join(str(p) for p in unique_paths)}) -- which declaration is "
                    "authoritative cannot be determined",
                )
            )
            continue
        declared[(concept, query)] = unique_paths[0]
    return declared, findings


def collect_valid_witnesses(
    descriptor: dict, workspace: Path, declared_keys: set[tuple[str, str]]
) -> tuple[dict[tuple[str, str], dict], list[Finding]]:
    """Every genuinely valid witness spec in the workspace, keyed by
    (concept, query) -- validate_witness.py's own G1a/G1b/G2 bar, crate
    by crate the same way its CLI does (a witness's canonical directory
    and its query cross-reference are both crate-scoped). A mislocated
    or otherwise invalid witness is not reported again here --
    validate-witness already says exactly what is wrong with it; this
    gate only needs to know it does not count as coverage.

    The cross-crate ambiguity check below is scoped to `declared_keys`
    (external review, medium severity: an earlier version raised it for
    every genuinely valid witness in the workspace regardless of
    whether its query was ever marked witness_required, so two crates
    each holding an unrelated, perfectly ordinary duplicate witness for
    an UNDECLARED query blocked the gate even with zero declared
    features -- reproduced with declared == 0 still returning a "more
    than one crate" error. That contradicts this module's own stated
    scope and plan.md:1072: coverage, and everything this gate checks,
    is relative to the declared feature set; an undeclared query must
    stay invisible to it, ambiguity findings included)."""
    validator = load_witness_validator()
    by_feature: dict[tuple[str, str], list[tuple[str, dict]]] = {}
    for crate in descriptor["crates"]:
        crate_root = (workspace / crate["crate_dir"]).resolve()
        if not crate_root.is_dir():
            continue
        canonical_dir = witness_dir_for(crate, workspace)
        specs_search_root = workspace / crate["specs_search_root"]
        for path in sorted(find_witness_files(crate_root)):
            if path.resolve().parent != canonical_dir:
                continue
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            file_findings = validate_witness_data(path, data, validator, specs_search_root)
            if any(f.severity == "error" for f in file_findings):
                continue
            key = (data["concept"], data["query"])
            by_feature.setdefault(key, []).append((crate["crate_dir"], data))

    witnesses: dict[tuple[str, str], dict] = {}
    findings: list[Finding] = []
    for (concept, query), entries in sorted(by_feature.items()):
        crates = sorted({crate_dir for crate_dir, _ in entries})
        if len(crates) > 1:
            if (concept, query) in declared_keys:
                findings.append(
                    Finding(
                        "G18", f"{concept}.{query}",
                        f"a genuinely valid witness resolving to this query exists under more than "
                        f"one crate ({', '.join(crates)}) -- which one is authoritative cannot be "
                        "determined",
                    )
                )
            continue
        witnesses[(concept, query)] = entries[0][1]
    return witnesses, findings


def gate_workspace(workspace: Path, descriptor: dict) -> tuple[list[Finding], int]:
    """G18 proper. Returns (findings, declared features discovered)."""
    findings: list[Finding] = []

    declared, declare_findings = collect_declared_features(descriptor, workspace)
    findings.extend(declare_findings)
    witnesses, witness_findings = collect_valid_witnesses(descriptor, workspace, set(declared))
    findings.extend(witness_findings)

    for concept, query in sorted(declared):
        feature = f"{concept}.{query}"
        witness = witnesses.get((concept, query))
        if witness is None:
            findings.append(
                Finding(
                    "G18", feature,
                    "witness_required: true but no genuinely valid witness spec resolves to this "
                    f"query -- author specs/_witnesses/{snake_case(concept)}.{query}.json "
                    "(plan.md §16.1)",
                )
            )
            continue
        rendering_path = workspace / witness["output"]["path"]
        if not rendering_path.is_file():
            findings.append(
                Finding(
                    "G18", feature,
                    "witness spec is valid but its declared rendering does not exist on disk at "
                    f"{witness['output']['path']} -- run `pipeline.py render-witness "
                    f"{witness['witness_id']} --renderer {witness['renderer']}`",
                )
            )

    return findings, len(declared)


def report_findings(findings: list[Finding], discovered: int, workspace: Path) -> int:
    errors = [f for f in findings if f.severity == "error"]

    if not errors:
        print(pass_line(discovered, "declared witness_required features", "G18 witness coverage", workspace))
        return EXIT_OK

    print(f"FAIL: {len(errors)} finding(s)")
    for finding in errors:
        print(f"  - {finding}")
    return EXIT_BLOCKED


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--descriptor", type=Path, default=None)
    args = parser.parse_args(argv)

    if not args.workspace.is_dir():
        print(f"error: workspace root does not exist or is not a directory: {args.workspace}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    descriptor_path = args.descriptor or (args.workspace / "project-descriptor.json")
    try:
        descriptor = json.loads(descriptor_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: cannot read project descriptor {descriptor_path}: {e}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    findings, discovered = gate_workspace(args.workspace, descriptor)
    return report_findings(findings, discovered, args.workspace)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
