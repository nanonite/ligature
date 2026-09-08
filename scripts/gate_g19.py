#!/usr/bin/env python3
"""G19: witness determinism gate.

plan.md §16.2's G19 row: "Regenerate, compare value_hash -- never
render_hash." Stage 8A / CI, hard error. Cheap and worth its own gate:
determinism is a real contract on the renderer, not a heuristic.

Unlike G18 (#29), this gate is not scoped to the declared
witness_required feature set -- every genuinely valid, promoted witness
spec carries its own `determinism.value_hash` claim, and every one of
them is checked, the same way G9 checks every promoted bridge rather
than only the ones a manifest happens to reference.

What "regenerate" means here
-----------------------------
There is no generic pluggable "witness producer backend" in the project
descriptor (unlike `verifier_backends` for chainlink #47) -- a
witness's value comes from evaluating a real query on a real fixture,
which this repository cannot do generically the way `check-bridges`
dispatches to a configured verifier command. Regeneration is therefore
the same split chainlink #47 already drew between `check_bridges()`
(generates) and `gate_workspace()` (checks): something upstream of this
gate -- a real project's CI step, or in this repository's own tests,
the stand-in producer (`tests/fixtures/witnesses/stand_in_producer.py`)
-- re-runs the fixture and writes a fresh canonical result to
`ci/results/witnesses/<witness_id>.json`. This gate is purely the
"compare" half. A witness spec with no corresponding result on disk has
nothing to check its determinism claim against, which is exactly as
blocking as a stale one: `byte-identical-across-runs` is not
established by a run that never happened.

Reusing the existing hashing and validation contracts
--------------------------------------------------------
`validate_witness.py`'s own `load_results_by_witness` already applies
the "genuinely valid, not just present" bar to every canonical result:
its own G1a/G1b recomputes `value_hash` from the raw hashed fields
(witness_id, concept, query, fixture_id, seed, result) via
`witness_result.py`'s `compute_value_hash`, and rejects the file
outright on disagreement. By the time a result comes out of
`load_results_by_witness`, its own `value_hash` field is therefore
already guaranteed to equal that recomputation -- this gate does not
re-hash anything itself; it reads that already-validated field and
compares it against the witness spec's own normative
`determinism.value_hash`. `render_hash` is never read anywhere in this
module -- not compared, not touched -- which a test proves directly
(a changed `render_hash` alone must never block) rather than by
absence.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_summary import pass_line  # noqa: E402
from validate_witness import find_witness_files  # noqa: E402
from validate_witness import load_results_by_witness  # noqa: E402
from validate_witness import load_validator as load_witness_validator  # noqa: E402
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


def collect_valid_witness_specs(descriptor: dict, workspace: Path) -> tuple[dict[str, dict], list[Finding]]:
    """Every genuinely valid witness spec in the workspace, keyed by
    witness_id -- validate_witness.py's own G1a/G1b/G2 bar, crate by
    crate the same way its CLI does (a witness's canonical directory
    and its query cross-reference are both crate-scoped). A witness_id
    found valid under more than one crate is ambiguous -- excluded, not
    first-wins, the same "ambiguous, never first-wins" choice
    gate_g18.py already makes for a (concept, query) pair and gate_g14.py
    makes for a cross-crate bridge_id collision."""
    validator = load_witness_validator()
    by_id: dict[str, list[tuple[str, dict]]] = {}
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
            by_id.setdefault(data["witness_id"], []).append((crate["crate_dir"], data))

    specs: dict[str, dict] = {}
    findings: list[Finding] = []
    for witness_id, entries in sorted(by_id.items()):
        crates = sorted({crate_dir for crate_dir, _ in entries})
        if len(crates) > 1:
            findings.append(
                Finding(
                    "G19", witness_id,
                    "a genuinely valid witness spec for this witness_id exists under more than one "
                    f"crate ({', '.join(crates)}) -- which one is authoritative cannot be determined",
                )
            )
            continue
        specs[witness_id] = entries[0][1]
    return specs, findings


def gate_workspace(workspace: Path, descriptor: dict) -> tuple[list[Finding], int]:
    """G19 proper. Returns (findings, witness specs checked)."""
    findings: list[Finding] = []

    specs, spec_findings = collect_valid_witness_specs(descriptor, workspace)
    findings.extend(spec_findings)
    results = load_results_by_witness(workspace)

    for witness_id, spec in sorted(specs.items()):
        result = results.get(witness_id)
        if result is None:
            findings.append(
                Finding(
                    "G19", witness_id,
                    "no genuinely valid canonical result at ci/results/witnesses/ to regenerate and "
                    "check the determinism claim against -- byte-identical-across-runs is not "
                    "established by a run that never happened",
                )
            )
            continue
        declared = spec["determinism"]["value_hash"]
        regenerated = result["value_hash"]
        if declared != regenerated:
            findings.append(
                Finding(
                    "G19", witness_id,
                    f"determinism.value_hash {declared} does not match the regenerated result's "
                    f"value_hash {regenerated} -- the declared byte-identical-across-runs claim does "
                    "not hold; render_hash is never part of this comparison",
                )
            )

    return findings, len(specs)


def report_findings(findings: list[Finding], discovered: int, workspace: Path) -> int:
    errors = [f for f in findings if f.severity == "error"]

    if not errors:
        print(pass_line(discovered, "witness specs", "G19 determinism (regenerated value_hash)", workspace))
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
