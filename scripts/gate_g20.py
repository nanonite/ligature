#!/usr/bin/env python3
"""G20: degenerate-witness gate against the declared expectation.

plan.md §12's gate table row for G20 and §16.2, chainlink #31:
"degenerate witness -- renderer actual != declared (hard error at
generation, not this gate); declared-expectation mismatch
(distribution/fixture/coverage_region). Stage 4.5, warn; block at
promotion if unresolved." "All-constant output" is only ever a defect
relative to a DECLARATION, never a global heuristic (§16.2's own
opening line) -- a single-cell fixture is supposed to be constant, and
a full-field one is not.

Two checks, both WARN severity
---------------------------------
A real defect the report must surface loudly, but not one that should
hard-block a Stage 4/CI run on its own -- the gate table's own "block
at promotion if unresolved" language names a DIFFERENT, later
enforcement point (the review-checkpoint promotion gate) that does not
yet exist for witness specs: `specs/_witnesses/` is not currently a
recognized artifact type in `pipeline.py`'s own
`_select_validate_fn`/`approve()` dispatcher, so there is no promotion
step today for this gate's findings to plug into. Until that
integration is built, this gate's own job is to make the finding exist
and be impossible to miss: print it clearly and exit with a THIRD,
distinct code (`EXIT_WARN`), mirroring `gate_r1_g16.py`'s own
`EXIT_DECISION_REQUIRED = 3` for its identical "not blocked, but never
silently passed either" tier, rather than collapsing into either a
clean 0 or a hard-blocked 1.

  * `expectation.value_distribution: must-vary` but the witness's
    genuinely valid canonical result measures only ONE distinct value
    (`value_domain.distinct_values == 1`). A witness with no genuinely
    valid canonical result at all is not checked here -- chainlink
    #30's G19 already owns "no result to check against" as its own
    hard-blocking concern, and reporting it again here would be the
    same fact under two gates.

  * `coverage_region` inconsistent with `fixture_family` -- reuses
    `validate_witness.py`'s own `check_family_consistency` rather than
    reimplementing it. That function already exists there, described in
    §16.1 as "exactly §16.2's coverage_region inconsistent with
    fixture_family", but is only ever exercised by `validate_crate()`'s
    own crate-scoped, hard-error G1b pass -- neither G18 nor G19's own
    witness collection calls `validate_crate()` at all (they call
    `validate_data()` per file), so this cross-witness check was never
    actually wired into a gate anyone runs by default. Run here
    WORKSPACE-WIDE (a `fixture_family` name is a bare string with no
    crate namespace, so two witnesses in different crates sharing one
    family name must agree too) over every genuinely valid witness
    spec, and re-reported at G20's own WARN severity rather than
    `validate_witness.py`'s G1b/error, since the gate table names this
    check G20's tier, not `validate-witness`'s.

Reuses rather than duplicates
--------------------------------
`collect_valid_witness_specs` (ambiguity handling included) is imported
directly from `gate_g19.py` rather than re-implemented a third time --
the same "one gate module imports another's collection helper rather
than re-deriving it" precedent `gate_g9.py` already sets by importing
`load_bridges`/`load_manifests` from `gate_g14.py`. An ambiguous
`witness_id` (the same duplicate-FILE-count check gate_g19.py's own
external review fixed) is still an ERROR here, not a warning -- it is a
structural identity problem, not a degeneracy one, and blocks outright
the same way G18/G19 already treat it.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gate_g19 import collect_valid_witness_specs  # noqa: E402
from scan_summary import pass_line  # noqa: E402
from validate_witness import check_family_consistency  # noqa: E402
from validate_witness import load_results_by_witness  # noqa: E402

EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_INPUT_ERROR = 2
EXIT_WARN = 3


@dataclass
class Finding:
    gate: str
    subject: str
    reason: str
    severity: str = "error"

    def __str__(self) -> str:
        return f"[{self.gate}/{self.severity}] {self.subject}: {self.reason}"


def check_must_vary(specs: dict[str, dict], results: dict[str, dict]) -> list[Finding]:
    """expectation.value_distribution: must-vary but the genuinely valid
    canonical result measures only one distinct value. Skips any
    witness with no genuinely valid result at all -- chainlink #30's
    G19 already hard-blocks that as its own concern, and this gate does
    not repeat it."""
    findings: list[Finding] = []
    for witness_id, spec in sorted(specs.items()):
        if spec["expectation"]["value_distribution"] != "must-vary":
            continue
        result = results.get(witness_id)
        if result is None:
            continue
        if result["value_domain"]["distinct_values"] == 1:
            findings.append(
                Finding(
                    "G20", witness_id,
                    "expectation.value_distribution is must-vary but the current canonical result "
                    "measures only one distinct value -- constant output is only ever a defect "
                    "relative to a declaration, and this witness declared it must vary",
                    severity="warn",
                )
            )
    return findings


def check_fixture_family_consistency(specs: dict[str, dict]) -> list[Finding]:
    """coverage_region inconsistent with fixture_family, workspace-wide
    -- reuses validate_witness.py's own check_family_consistency rather
    than reimplementing it, re-reported at G20's own warn severity."""
    entries = [(Path(f"{witness_id}.json"), spec) for witness_id, spec in sorted(specs.items())]
    return [
        Finding("G20", str(finding.path), finding.reason, severity="warn")
        for finding in check_family_consistency(entries)
    ]


def gate_workspace(workspace: Path, descriptor: dict) -> tuple[list[Finding], int]:
    """G20 proper. Returns (findings, witness specs checked)."""
    findings: list[Finding] = []

    specs, spec_findings = collect_valid_witness_specs(descriptor, workspace)
    findings.extend(spec_findings)
    results = load_results_by_witness(workspace)

    findings.extend(check_must_vary(specs, results))
    findings.extend(check_fixture_family_consistency(specs))

    return findings, len(specs)


def report_findings(findings: list[Finding], discovered: int, workspace: Path) -> int:
    """Three-valued, mirroring gate_r1_g16.py's own EXIT_DECISION_REQUIRED
    tier: a structural (ambiguity) problem blocks outright; a degeneracy
    warning is never a silent pass but is not a hard CI block either."""
    errors = [f for f in findings if f.severity == "error"]
    warns = [f for f in findings if f.severity == "warn"]

    if errors:
        print(f"FAIL: {len(errors)} finding(s)")
        for finding in errors:
            print(f"  - {finding}")
        return EXIT_BLOCKED

    if warns:
        print(f"WARN: {len(warns)} finding(s) -- must be resolved before promotion")
        for finding in warns:
            print(f"  - {finding}")
        return EXIT_WARN

    print(pass_line(
        discovered, "witness specs", "G20 degeneracy (must-vary, fixture-family consistency)", workspace
    ))
    return EXIT_OK


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
