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

Two checks, both WARN severity at Stage 4/CI -- promoted to a hard
block at Stage 4.5
--------------------------------------------------------------------
A real defect the Stage 4/CI report must surface loudly, but not one
that should hard-block that run on its own; the gate table's own
"block at promotion if unresolved" is a SEPARATE, later enforcement
point. This module realizes both halves of that sentence with ONE set
of findings rather than two mechanisms: `gate_workspace()` reports them
at WARN (never a silent pass, never a hard CI block -- see `EXIT_WARN`
below), and `validate_witness_for_approval()` is the `validate_fn`
`pipeline.py`'s `approve()` now dispatches to for a witness spec
promotion, which re-checks the SAME findings and promotes exactly the
ones naming the witness being promoted from WARN to ERROR before
`approve()`'s own "refuse on any error" rule sees them. "Warn early,
block at promotion" is the identical finding at two severities
depending on WHEN it is checked, not two different code paths that
could silently drift apart.

  * `expectation.value_distribution: must-vary` but the witness's
    genuinely valid canonical result measures only ONE distinct value
    (`value_domain.distinct_values == 1`). A witness with no genuinely
    valid canonical result at all is not checked here -- chainlink
    #30's G19 already owns "no result to check against" as its own
    hard-blocking concern, and reporting it again here would be the
    same fact under two gates. The result's own identity
    (concept/query/fixture_id/seed/renderer_actual) is checked against
    the CURRENT spec before its `value_domain` is trusted at all
    (`gate_g19.identity_mismatches`, reused rather than reimplemented --
    external review, high severity: an earlier version joined solely on
    `witness_id`, so a stale result left over from an earlier spec
    revision, self-consistent but describing a DIFFERENT fixture/seed/
    concept/query, could make a currently-constant witness pass by
    borrowing a varying result that was never actually about it).

  * `coverage_region` inconsistent with `fixture_family` -- reuses
    `validate_witness.py`'s own `check_family_consistency` rather than
    reimplementing it, run WORKSPACE-WIDE (a `fixture_family` name is a
    bare string with no crate namespace, so two witnesses in different
    crates sharing one family name must agree too) over every genuinely
    valid witness spec. `validate_witness.py`'s own `validate_crate()`
    no longer calls this (external review, medium severity: it used to,
    which hard-failed a same-crate inconsistency as G1b/error at
    ordinary Stage 4 validation, before G20 could ever report it as its
    own Stage 4.5 warning -- the two-stage disposition the gate table
    describes needs exactly one severity for this defect, not two that
    disagree). G20 is now this check's only caller.

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
from gate_g19 import identity_mismatches  # noqa: E402
from scan_summary import pass_line  # noqa: E402
from validate_witness import check_family_consistency  # noqa: E402
from validate_witness import load_results_by_witness  # noqa: E402
from validate_witness import validate_data as validate_witness_data  # noqa: E402

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
    not repeat it.

    A result's own identity is checked against the CURRENT spec before
    its value_domain is trusted (external review, high severity: an
    earlier version joined solely on witness_id -- load_results_by_witness()
    already proves a result is self-consistent, but never that it is
    ABOUT the spec it is being compared against, so a stale result left
    over from an earlier fixture_id/seed/concept/query revision could
    silently make a currently-constant witness look like it passes)."""
    findings: list[Finding] = []
    for witness_id, spec in sorted(specs.items()):
        if spec["expectation"]["value_distribution"] != "must-vary":
            continue
        result = results.get(witness_id)
        if result is None:
            continue
        mismatched = identity_mismatches(spec, result)
        if mismatched:
            findings.append(
                Finding(
                    "G20", witness_id,
                    f"the stored canonical result's {', '.join(mismatched)} does not match the "
                    "current witness spec -- a result describing a different fixture/seed/concept/"
                    "query cannot be trusted for this witness's must-vary check, regenerate it first "
                    "(pipeline.py gate-g19)",
                    severity="warn",
                )
            )
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
    than reimplementing it, re-reported at G20's own warn severity.
    Subject is the bare witness_id (not a synthetic path), matching
    check_must_vary's own subject shape so a caller filtering findings
    by witness_id (validate_witness_for_approval below) needs only one
    comparison rule for both checks."""
    entries = [(Path(f"{witness_id}.json"), spec) for witness_id, spec in sorted(specs.items())]
    return [
        Finding("G20", finding.path.stem, finding.reason, severity="warn")
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


def validate_witness_for_approval(
    path: Path, data: dict, validator, specs_search_root: Path | None, workspace: Path, descriptor: dict
) -> list:
    """The validate_fn pipeline.py's approve() dispatches to for a
    witness spec promotion (chainlink #31) -- the two-stage lifecycle
    plan.md's gate table describes ("warn" at Stage 4/CI, "block at
    promotion if unresolved" at Stage 4.5) made real as one mechanism
    rather than two.

    First, validate_witness.py's own G1a/G1b/G2 (validate_data) -- a
    degeneracy check on a schema-invalid or dangling witness isn't
    meaningful, the same short-circuit validate_data() itself already
    applies internally between its own G1a and G1b/G2. If that already
    fails, G20 does not even run.

    Otherwise, runs G20's full workspace scan (a fixture-family
    disagreement is inherently cross-witness, so the scan cannot be
    narrowed to one document ahead of time) and keeps only the findings
    naming THIS witness_id, promoted from WARN to ERROR -- approve()'s
    own rule ("refuse on any error-severity finding") then does the
    actual blocking, with no change to approve() itself. A finding
    about a DIFFERENT witness (e.g. the other half of a fixture-family
    disagreement) is not surfaced through THIS promotion at all: it is
    real, and `pipeline.py gate-g20`'s own workspace-wide run still
    reports it, but promoting one witness is not grounds to refuse it
    over a defect naming a different one."""
    findings = validate_witness_data(path, data, validator, specs_search_root)
    if any(f.severity == "error" for f in findings):
        return findings

    witness_id = data["witness_id"]
    g20_findings, _ = gate_workspace(workspace, descriptor)
    for finding in g20_findings:
        if finding.subject != witness_id:
            continue
        severity = "error" if finding.severity == "warn" else finding.severity
        findings.append(Finding(finding.gate, finding.subject, finding.reason, severity))
    return findings


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
