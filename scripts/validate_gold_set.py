#!/usr/bin/env python3
"""Human gold-set validation: G1a (schema) and G1b (repo semantics).

plan.md §5.2's independence argument and §13 item 13, chainlink #26.
A gold set is a reviewed human judgement about which interaction edges
exist, used to measure what the pipeline proposed. It sits on the
reviewed side of this codebase's line (specs/_gold_sets/<cluster>.json,
review block, promoted through the same checkpoint as any other reviewed
artifact) rather than the machine-observation side, because a judgement
with no author is not evidence of anything.

  G1a -- draft-2020-12 schema validation against
         docs/gold-set-schema.json, where the anti-circularity guard
         lives: `derived_from`'s enum has no value naming the interaction
         set, so a curator who worked from I has to write something
         false rather than merely omit something.
  G1b -- repo semantics:
         * flat inside a _gold_sets/ directory; cluster == filename stem.
         * every edge's `derived_from` values appear in
           `curation_scope.method` -- an edge found by a method the
           curator never claimed to use is unexplained.
         * every edge's CALLER concept is in
           `curation_scope.examined_concepts` -- an edge outside the
           declared scope would be scored against proposals nobody was
           asked to make.
         * no duplicate edges: two entries for one edge would count
           twice in recall and make the metric depend on how many times
           someone wrote it down.
         * an edge whose caller and callee are the same concept is
           rejected: intra-concept structure is L2's territory, and
           gate-r1-g16 already treats such a call as an internal helper.
           A gold set that contains them would score the pipeline for
           not proposing edges the pipeline is right not to propose.

Whether the gold set is CORRECT is not checkable and is not claimed:
this module checks that it is well-formed, scoped, and explained.
Measurement against it is scripts/measure_gold_set.py's job -- the same
boundary validate_callsites.py draws against gate_r1_g16.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_summary import pass_line  # noqa: E402
from schema_utils import make_validator  # noqa: E402
from schema_utils import make_validator_without_required  # noqa: E402

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "docs" / "gold-set-schema.json"
CANONICAL_DIR_NAME = "_gold_sets"


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


def load_draft_validator() -> Draft202012Validator:
    return make_validator_without_required(load_schema(), "review")


def gold_set_dir_for(workspace: Path) -> Path:
    """specs/_gold_sets/. Workspace-level, like specs/_closure/ and
    specs/_conflicts/: a cluster spans crates, and so does a gold set for
    one."""
    return (workspace / "specs" / CANONICAL_DIR_NAME).resolve()


def edge_key(edge: dict) -> tuple[str, str, str, str]:
    return (
        edge["caller"]["concept"],
        edge["caller"]["method"],
        edge["callee"]["concept"],
        edge["callee"]["method"],
    )


def edge_label(edge: dict) -> str:
    caller, callee = edge["caller"], edge["callee"]
    return f"{caller['concept']}.{caller['method']} -> {callee['concept']}.{callee['method']}"


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
    if path.stem != data["cluster"]:
        findings.append(
            Finding(
                "G1b", path,
                f"filename stem {path.stem!r} does not match cluster {data['cluster']!r}",
            )
        )
    return findings


def check_edges(path: Path, data: dict) -> list[Finding]:
    findings: list[Finding] = []
    scope = data["curation_scope"]
    examined = set(scope["examined_concepts"])
    methods = set(scope["method"])
    seen: set[tuple[str, str, str, str]] = set()

    for edge in data["edges"]:
        label = edge_label(edge)
        key = edge_key(edge)
        if key in seen:
            findings.append(
                Finding(
                    "G1b", path,
                    f"duplicate gold edge {label} -- recall would count it twice, making the "
                    "metric depend on how many times someone wrote it down",
                )
            )
        seen.add(key)

        if edge["caller"]["concept"] not in examined:
            findings.append(
                Finding(
                    "G1b", path,
                    f"gold edge {label} has a caller outside curation_scope.examined_concepts "
                    f"{sorted(examined)!r} -- it would be scored against proposals nobody was "
                    "asked to make",
                )
            )

        unexplained = sorted(set(edge["derived_from"]) - methods)
        if unexplained:
            findings.append(
                Finding(
                    "G1b", path,
                    f"gold edge {label} declares derived_from {unexplained!r}, which "
                    f"curation_scope.method {sorted(methods)!r} does not claim",
                )
            )

        if edge["caller"]["concept"] == edge["callee"]["concept"]:
            findings.append(
                Finding(
                    "G1b", path,
                    f"gold edge {label} is intra-concept -- internal structure is L2's territory, "
                    "and gate-r1-g16 already treats such a call as an internal helper; scoring the "
                    "pipeline for not proposing it would penalize correct behaviour",
                )
            )
    return findings


def check_no_draft_review(path: Path, data: dict) -> list[Finding]:
    if "review" in data:
        return [
            Finding(
                "G1b", path,
                "draft must not include its own `review` block -- review is only attached by "
                "approve() after a human reviewer signs off, and a gold set a model reviewed for "
                "itself audits nothing",
            )
        ]
    return []


def validate_data(path: Path, data: dict, validator: Draft202012Validator) -> list[Finding]:
    g1a = gate_g1a(path, data, validator)
    if g1a:
        return g1a
    findings = check_naming(path, data)
    findings.extend(check_edges(path, data))
    return findings


def validate_draft_data(path: Path, data: dict, validator: Draft202012Validator) -> list[Finding]:
    review_check = check_no_draft_review(path, data)
    if review_check:
        return review_check
    return validate_data(path, data, validator)


def validate_file(path: Path, validator: Draft202012Validator) -> list[Finding]:
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
    return validate_data(path, data, validator)


def find_gold_set_files(root: Path) -> list[Path]:
    if not root.is_dir():
        raise FileNotFoundError(f"gold-set scan root does not exist or is not a directory: {root}")
    return [p for p in root.glob(f"**/{CANONICAL_DIR_NAME}/**/*") if p.is_file()]


def count_discovered(root: Path) -> int:
    """The candidate set this module's own scan walks, counted for the
    honest pass line (chainlink #48). Wraps find_gold_set_files() rather
    than re-deriving its glob, so the count can never drift from the set
    actually validated."""
    return len(find_gold_set_files(root))


def validate_workspace(root: Path, canonical_dir: Path) -> list[Finding]:
    canonical_resolved = canonical_dir.resolve()
    validator = load_validator()
    findings: list[Finding] = []
    for path in sorted(find_gold_set_files(root)):
        if path.resolve().parent != canonical_resolved:
            findings.append(
                Finding(
                    "G1b", path,
                    f"gold set is not directly under the canonical directory {canonical_resolved} "
                    f"-- found under {path.resolve().parent}",
                )
            )
            continue
        findings.extend(validate_file(path, validator))
    return findings


def validate(root: Path) -> list[Finding]:
    return validate_workspace(root, gold_set_dir_for(root))


def load_gold_sets(workspace: Path) -> dict[str, tuple[Path, dict]]:
    """Every fully valid gold set in the canonical directory, by cluster
    -- the "genuinely valid, not just present" bar every cross-reference
    in this pipeline applies. scripts/measure_gold_set.py consumes this,
    so a malformed or unscoped gold set can never become the standard
    something is scored against."""
    canonical = gold_set_dir_for(workspace)
    result: dict[str, tuple[Path, dict]] = {}
    if not canonical.is_dir():
        return result
    validator = load_validator()
    for path in sorted(p for p in canonical.iterdir() if p.is_file()):
        if any(f.severity == "error" for f in validate_file(path, validator)):
            continue
        data = json.loads(path.read_text())
        result.setdefault(data["cluster"], (path, data))
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=Path, help="Workspace root holding specs/_gold_sets/*.json")
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
        for finding in infos:
            print(f"  - {finding}")

    if not errors:
        print(pass_line(discovered, "gold sets", "G1a/G1b (scope, provenance and edge discipline)", args.root))
        return 0

    print(f"FAIL: {len(errors)} finding(s)")
    for finding in errors:
        print(f"  - {finding}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
