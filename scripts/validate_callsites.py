#!/usr/bin/env python3
"""C_static report validation: G1a (schema) and G1b (repo semantics).

plan.md §9, chainlink #24. A C_static report is an *observation*, not a
normative artifact -- it carries no `review` block and never goes through
draft/approve (plan.md §7.2's human-checkpoint list names risk decisions
and closure acceptance, not the extraction itself). What it still needs
is the same mechanical discipline every other artifact in this codebase
gets before anything is allowed to consume it.

  G1a -- draft-2020-12 JSON Schema validation against
         docs/callsite-schema.json, which is where the
         extractor_backing/completeness_claim honesty pair is enforced
         structurally: a `syntactic` extractor may only claim
         `discovered-lower-bound` (chainlink #24, "no regex-only
         completeness claim").
  G1b -- repo semantics:
         * filename/layout -- flat inside a `c_static/` directory,
           report_id == filename stem, the same discipline boundary_id /
           interaction_id / bridge_id already follow.
         * RECOMPUTED callsite_coverage -- discovered/resolved/unresolved
           are derived from `callsites` and disagreement is rejected in
           both directions, exactly the way G1b treats computed
           eligibility in I (plan.md §5.2). A stored count is never
           trusted: the whole value of this report is that its coverage
           numbers are honest, and a hand-edited `unresolved: 0` would
           buy a closure-profile condition (plan.md §4) for free.
         * callsite_id uniqueness, and agreement between a callsite_id
           and the caller it claims -- a fabricated id pointing at a
           different method would silently move a human risk decision
           (extract_c_static.py --previous carries human tiers by id).
         * source.symbol agreement with the caller role.
         * a definite-direct-call whose callee concept is outside the
           report's own declared local_concepts universe -- a resolved
           callee the report says it does not scope is a contradiction,
           not a resolution.
         * RECOMPUTED callee_shape (chainlink #107) -- the classification
           of a resolved call's callee is computed by the extractor from
           the callee's own source and pinned to that body's token hash,
           and G1b re-derives both from the crate's sources rather than
           trusting either. This is what lets gate_r1_g16 read the claim at
           all: a hand-edited `callee_shape: value-domain-inquiry` would be
           the one way to ask R1 not to block a cross-concept call without
           declaring the interaction, so the claim has to be checkable
           rather than assertable. Re-deriving needs the workspace root, so
           it runs wherever a caller has one (`validate_workspace`, and
           `load_reports` -- the latter being exactly what the gate
           consumes); a caller validating one report with no workspace in
           hand skips the check rather than reporting a disagreement it
           could not compute.

Reconciling any of this against I (R1) and applying the unresolved risk
policy (G16) is scripts/gate_r1_g16.py's job, not this module's -- the
same separation validate_interaction.py draws from R2's own gate.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resources  # noqa: E402
from extract_c_static import GENERAL_CALL  # noqa: E402
from extract_c_static import VALUE_DOMAIN_INQUIRY  # noqa: E402
from extract_c_static import CalleeShape  # noqa: E402
from extract_c_static import ExtractionError  # noqa: E402
from extract_c_static import callee_shape_index  # noqa: E402
from schema_utils import make_validator  # noqa: E402
from scan_summary import pass_line  # noqa: E402

SCHEMA_PATH = resources.resource_path("docs", "callsite-schema.json")
CANONICAL_DIR_NAME = "c_static"

# crate_dir -> (concept, method) -> CalleeShape, or None where the crate's
# sources cannot be read at all. Callable rather than a mapping so one
# workspace scan parses each crate once however many reports name it.
ShapeIndexFor = Callable[[str], "dict[tuple[str, str], CalleeShape] | None"]


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


def check_naming(path: Path, data: dict) -> list[Finding]:
    findings: list[Finding] = []

    if path.parent.name != CANONICAL_DIR_NAME:
        findings.append(
            Finding(
                "G1b", path,
                f"not flat -- must live directly inside a {CANONICAL_DIR_NAME}/ directory, "
                f"found under {path.parent}",
            )
        )

    if path.suffix != ".json":
        findings.append(Finding("G1b", path, f"must be a .json file, found suffix {path.suffix!r}"))

    if path.stem != data["report_id"]:
        findings.append(
            Finding(
                "G1b", path,
                f"filename stem {path.stem!r} does not match report_id {data['report_id']!r}",
            )
        )

    return findings


def recompute_coverage(callsites: list[dict]) -> dict:
    resolved = sum(1 for c in callsites if c.get("call_class") == "definite-direct-call")
    return {
        "discovered": len(callsites),
        "resolved": resolved,
        "unresolved": len(callsites) - resolved,
    }


def check_computed_coverage(path: Path, data: dict) -> list[Finding]:
    """Never trust the stored counts. Rejected in BOTH directions: an
    understated `unresolved` hides a limitation, and an overstated one
    invents a blocker -- the same symmetry G1b applies to computed
    eligibility, where an edge overstated as boundary-required is
    rejected exactly like one understated as ignore."""
    stored = data["callsite_coverage"]
    computed = recompute_coverage(data["callsites"])
    if stored == computed:
        return []
    return [
        Finding(
            "G1b", path,
            f"callsite_coverage {stored!r} disagrees with the counts recomputed from "
            f"callsites {computed!r} -- coverage is computed, never stored-and-trusted",
        )
    ]


def shape_index_resolver(workspace: Path) -> ShapeIndexFor:
    """crate_dir -> the callee-shape index re-derived from that crate's
    sources, or None where they cannot be read. Memoised per crate_dir so a
    workspace holding several reports for one crate parses that crate once.
    Reading the crate is the whole cost of this check, and it is spent only
    on reports that actually claim a shape: the resolver is lazy."""
    cache: dict[str, dict[tuple[str, str], CalleeShape] | None] = {}

    def resolve(crate_dir: str) -> dict[tuple[str, str], CalleeShape] | None:
        if crate_dir not in cache:
            try:
                cache[crate_dir] = callee_shape_index(workspace / crate_dir)
            except (ExtractionError, OSError):
                cache[crate_dir] = None
        return cache[crate_dir]

    return resolve


def check_computed_callee_shape(path: Path, data: dict, resolve: ShapeIndexFor) -> list[Finding]:
    """Re-derive every recorded callee_shape from the crate's sources
    (chainlink #107). The classification is what lets R1 report a
    cross-concept call absent from I without blocking on it, so a claim that
    cannot be re-derived is a claim that was never computed -- and one that
    disagrees is a report describing a call site the source does not have.

    Rejected in BOTH directions, for the same reason computed coverage is:
    a shape understated as `general` invents a blocker the source does not
    justify, exactly as an overstated `unresolved` does.

    A record with NO recorded shape is not a finding. That is a report
    extracted before the classification existed, and reading it as
    `general` is what keeps it safe (docs/callsite-schema.json's
    `callee_shape` note) without invalidating an observation whose counts
    were honest."""
    crate_dir = data["crate_dir"]
    findings: list[Finding] = []

    for record in data["callsites"]:
        recorded = record.get("callee_shape")
        if recorded is None:
            continue
        callsite_id = record["callsite_id"]
        callee = record["callee"]
        shapes = resolve(crate_dir)

        if shapes is None:
            findings.append(
                Finding(
                    "G1b", path,
                    f"{callsite_id}: callee_shape {recorded!r} is recorded for "
                    f"{callee['concept']}::{callee['method']} but the sources of crate "
                    f"{crate_dir!r} could not be read, so the claim cannot be re-derived -- "
                    "a computed claim that cannot be re-derived was not computed",
                )
            )
            continue

        derived = shapes.get((callee["concept"], callee["method"]))
        if derived is None:
            if recorded != GENERAL_CALL:
                findings.append(
                    Finding(
                        "G1b", path,
                        f"{callsite_id}: callee_shape {recorded!r} is recorded for "
                        f"{callee['concept']}::{callee['method']}, which is not defined "
                        f"anywhere in the sources of crate {crate_dir!r}",
                    )
                )
            continue
        if derived.shape != recorded:
            findings.append(
                Finding(
                    "G1b", path,
                    f"{callsite_id}: callee_shape {recorded!r} for "
                    f"{callee['concept']}::{callee['method']} disagrees with the "
                    f"{derived.shape!r} recomputed from that method's own source -- a "
                    "callee shape is computed, never stored-and-trusted",
                )
            )
            continue
        if recorded == VALUE_DOMAIN_INQUIRY:
            pinned = (record.get("callee_shape_evidence") or {}).get("syntax_hash")
            if pinned != derived.syntax_hash:
                findings.append(
                    Finding(
                        "G1b", path,
                        f"{callsite_id}: the value-domain-inquiry claim for "
                        f"{callee['concept']}::{callee['method']} is pinned to {pinned!r}, "
                        f"but that method's own source now hashes to {derived.syntax_hash!r}",
                    )
                )

    return findings


def _expected_id_prefix(caller: dict) -> str:
    return f"CS-{caller['concept'].upper()}-{caller['method'].upper().replace('_', '-')}-"


def check_callsite_identity(path: Path, data: dict) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[str] = set()
    local_concepts = set(data["attribution_scope"]["local_concepts"])

    for record in data["callsites"]:
        callsite_id = record["callsite_id"]
        if callsite_id in seen:
            findings.append(
                Finding("G1b", path, f"duplicate callsite_id {callsite_id!r}")
            )
        seen.add(callsite_id)

        caller = record["caller"]
        prefix = _expected_id_prefix(caller)
        if not callsite_id.startswith(prefix):
            findings.append(
                Finding(
                    "G1b", path,
                    f"callsite_id {callsite_id!r} does not identify its own caller "
                    f"{caller['concept']}::{caller['method']} (expected prefix {prefix!r}) -- "
                    "a human risk tier is carried across re-extraction by this id",
                )
            )

        symbol = record["source"]["symbol"]
        expected_symbol = f"{caller['concept']}::{caller['method']}"
        if symbol != expected_symbol:
            findings.append(
                Finding(
                    "G1b", path,
                    f"source.symbol {symbol!r} does not match the caller role "
                    f"{expected_symbol!r}",
                )
            )

        callee = record.get("callee")
        if callee is not None and callee["concept"] not in local_concepts:
            findings.append(
                Finding(
                    "G1b", path,
                    f"{callsite_id}: callee concept {callee['concept']!r} is not in this "
                    f"report's own local_concepts {sorted(local_concepts)!r} -- a resolved "
                    "callee outside the declared concept universe is a contradiction",
                )
            )

    return findings


def validate_data(
    path: Path,
    data: dict,
    validator: Draft202012Validator,
    resolve_shapes: ShapeIndexFor | None = None,
) -> list[Finding]:
    g1a = gate_g1a(path, data, validator)
    if g1a:
        return g1a
    findings: list[Finding] = []
    findings.extend(check_naming(path, data))
    findings.extend(check_computed_coverage(path, data))
    findings.extend(check_callsite_identity(path, data))
    if resolve_shapes is not None:
        # Only a caller holding the workspace root can re-derive a callee
        # shape (see check_computed_callee_shape); a caller without one skips
        # the check rather than reporting a disagreement it cannot compute.
        findings.extend(check_computed_callee_shape(path, data, resolve_shapes))
    return findings


def validate_file(
    path: Path,
    validator: Draft202012Validator,
    resolve_shapes: ShapeIndexFor | None = None,
) -> list[Finding]:
    try:
        text = path.read_text()
    except UnicodeDecodeError as e:
        return [Finding("G1a", path, f"not readable as UTF-8 text: {e}")]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return [Finding("G1a", path, f"invalid JSON: {e}")]
    if not isinstance(data, dict):
        return [Finding("G1a", path, "top-level value is not a JSON object")]
    return validate_data(path, data, validator, resolve_shapes)


def find_report_files(root: Path) -> list[Path]:
    """Every file anywhere under a `c_static` directory, at any depth --
    mirrors find_bridge_files/find_exemption_files so a nested placement
    surfaces as a G1b violation instead of going unchecked."""
    if not root.is_dir():
        raise FileNotFoundError(f"C_static scan root does not exist or is not a directory: {root}")
    return [p for p in root.glob(f"**/{CANONICAL_DIR_NAME}/**/*") if p.is_file()]


def validate_workspace(root: Path, canonical_dir: Path) -> list[Finding]:
    """Discover crate-wide (or workspace-wide), then reject by location --
    the same discover-then-reject-by-location shape every validate_<type>.py
    module uses, and for the same reason: a scan anchored only to the
    canonical directory stops a mislocated report from being wrongly
    trusted, but also stops it from ever being looked at."""
    canonical_resolved = canonical_dir.resolve()
    validator = load_validator()
    resolve_shapes = shape_index_resolver(root)
    findings: list[Finding] = []
    for path in sorted(find_report_files(root)):
        if path.resolve().parent != canonical_resolved:
            findings.append(
                Finding(
                    "G1b", path,
                    f"C_static report is not directly under the canonical directory "
                    f"{canonical_resolved} -- found under {path.resolve().parent}",
                )
            )
            continue
        findings.extend(validate_file(path, validator, resolve_shapes))
    return findings


def validate(root: Path) -> list[Finding]:
    """Standalone CLI entry: `root` is always the workspace root, so this
    delegates to the same anchored discover-then-reject scan pipeline.py
    uses rather than a separate, weaker unanchored one (the layout gap a
    2026-09-02 review found in validate_evidence/validate_conflict_
    resolution's own standalone validate())."""
    return validate_workspace(root, callsite_report_dir_for(root))


def callsite_report_dir_for(workspace: Path) -> Path:
    """ci/results/c_static/*.json. Workspace-level, not crate-scoped: a
    C_static report is a generated CI observation living beside
    ci/results/review_log.jsonl and the feature ledger (plan.md §16), not
    a spec under crates/*/specs/** -- which the project descriptor's own
    write_set marks protected precisely because specs are hand-authored
    and promoted. Each report names its crate in `crate_dir`."""
    return (workspace / "ci" / "results" / CANONICAL_DIR_NAME).resolve()


def load_reports(workspace: Path) -> list[tuple[Path, dict]]:
    """Every fully valid (zero error-severity finding) report in the
    canonical directory -- the same "must be genuinely valid, not just
    present" bar every cross-reference in this pipeline applies before
    trusting an artifact. gate_r1_g16.py consumes this, so a report that
    fails G1a/G1b can never reach R1/G16 and be reconciled as if its
    counts meant something."""
    canonical = callsite_report_dir_for(workspace)
    if not canonical.is_dir():
        return []
    validator = load_validator()
    resolve_shapes = shape_index_resolver(workspace)
    reports: list[tuple[Path, dict]] = []
    for path in sorted(p for p in canonical.iterdir() if p.is_file()):
        findings = validate_file(path, validator, resolve_shapes)
        if any(f.severity == "error" for f in findings):
            continue
        reports.append((path, json.loads(path.read_text())))
    return reports


def count_discovered(root: Path) -> int:
    """The candidate set this module's own scan walks, counted for
    the honest pass line (chainlink #48). Wraps find_report_files()
    rather than re-deriving its glob, so the count can never drift
    from the set actually validated."""
    return len(find_report_files(root))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=Path, help="Workspace root holding ci/results/c_static/*.json")
    args = parser.parse_args(argv)

    try:
        findings = validate(args.root)
        discovered = count_discovered(args.root)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    errors = [f for f in findings if f.severity == "error"]
    infos = [f for f in findings if f.severity == "info"]

    if infos:
        print(f"INFO: {len(infos)} non-blocking finding(s)")
        for f in infos:
            print(f"  - {f}")

    if not errors:
        print(pass_line(
            discovered, "C_static reports",
            "G1a/G1b (incl. recomputed callsite coverage and callee shapes)",
            args.root,
        ))
        return 0

    print(f"FAIL: {len(errors)} finding(s)")
    for f in errors:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
