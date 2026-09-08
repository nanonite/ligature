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

A first version of this gate compared a witness spec's declared
value_hash against whatever canonical result already happened to sit at
ci/results/witnesses/<witness_id>.json, trusting that "regeneration"
had occurred somewhere upstream (external review, high severity, twice
over):

  * nothing enforced that the file was ever actually refreshed -- if a
    producer's real behavior drifted but CI never re-ran it, the stale
    file still matched the promoted hash and the gate passed. An
    unenforced upstream step establishes nothing.
  * the join was on witness_id alone. A spec's fixture_id, seed,
    concept or query could change while its declared value_hash stayed
    untouched, and a stale result with the OLD identity would still be
    accepted as "the" result for the new spec.

Fixed by making regeneration unconditional and internal to this gate,
the way `verifier_backends` dispatch is external-but-mandatory for G9:
`witness_backend` (project descriptor, chainlink #30) names ONE
pluggable command. `gate_workspace()` invokes it FRESH, every run, for
every genuinely valid witness spec, passing that spec's OWN current
`--witness-id`/`--concept`/`--query`/`--fixture-id`/`--seed`/`--renderer`
as arguments -- so a regenerated result's identity is constructed FROM
the current spec and cannot silently drift from it. With no backend
configured, nothing was actually re-evaluated this run, and the gate
blocks rather than falling back to trusting a file on disk. As defense
in depth against a misbehaving backend that ignores its own arguments,
the regenerated document's own identity fields are still compared
against the spec before its hash is trusted at all -- including
`renderer_actual` against the requested `renderer` (external review,
medium severity: that field is deliberately EXCLUDED from `value_hash`
by §16.1's own rule, "the same numbers from a different renderer are
the same fact," so nothing in the hash comparison alone would ever
catch a backend that silently ignored `--renderer` and reported a
different, still schema-valid one instead -- exactly the
declared/actual mismatch §16.2 makes a hard error at generation time
for chainlink #28's `generate_witness.py`; this fresh regeneration now
gets the identical check). The subprocess also runs with `cwd=workspace`
(external review, medium severity: an earlier version ran it in the
caller's own directory, so invoking `pipeline.py --workspace
/elsewhere gate-g19` from a different cwd left a producer unable to
reliably resolve fixture/source files relative to the workspace it was
meant to inspect, or resolving them against whatever checkout happened
to be underfoot instead).

This gate deliberately does not persist the regenerated result to
ci/results/witnesses/ -- gates in this codebase check, they do not
mutate normative or generated artifacts as a side effect (gate_g9.py's
own gate_workspace() draws the identical line against check_bridges()).
Keeping that artifact current for chainlink #28's renderer remains a
separate workflow.

Reusing the existing hashing and validation contracts
--------------------------------------------------------
The regenerated document is validated with `validate_witness.py`'s own
`validate_result_data` -- the same G1a/G1b bar `load_results_by_witness`
applies to a stored result, including the value_hash/value_domain
recomputation via `witness_result.py`'s `compute_value_hash`. This gate
never re-implements that hashing; it only reads the already-validated
`value_hash` field off a document that has just been proven internally
self-consistent, and compares it against the witness spec's normative
`determinism.value_hash`. `render_hash` is never read anywhere in this
module -- not compared, not touched -- which a test proves directly (a
changed `render_hash` alone must never block) rather than by absence.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_summary import pass_line  # noqa: E402
from validate_witness import find_witness_files  # noqa: E402
from validate_witness import load_result_validator  # noqa: E402
from validate_witness import load_validator as load_witness_validator  # noqa: E402
from validate_witness import validate_data as validate_witness_data  # noqa: E402
from validate_witness import validate_result_data  # noqa: E402
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


def collect_valid_witness_entries(descriptor: dict, workspace: Path) -> list[tuple[Path, dict]]:
    """Every genuinely valid witness spec in the workspace, as
    (resolved path, data) pairs -- validate_witness.py's own G1a/G1b/G2
    bar, crate by crate the same way its CLI does (a witness's
    canonical directory and its query cross-reference are both
    crate-scoped). No witness_id collapsing or ambiguity handling here
    -- that is collect_valid_witness_specs()'s job, built on top of
    this.

    Exposed separately (chainlink #31, external review) because
    gate_g20.py's candidate-aware promotion check needs to
    replace/insert an entry by PATH, not by witness_id: collapsing by
    witness_id FIRST would let a candidate whose witness_id happens to
    collide with a DIFFERENT file's silently overwrite that file's
    entry in the resulting dict instead of being caught as a
    duplicate."""
    validator = load_witness_validator()
    entries: list[tuple[Path, dict]] = []
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
            entries.append((path.resolve(), data))
    return entries


def collect_valid_witness_specs(descriptor: dict, workspace: Path) -> tuple[dict[str, dict], list[Finding]]:
    """Every genuinely valid witness spec in the workspace, keyed by
    witness_id -- built from collect_valid_witness_entries() above.

    A witness_id found valid in more than one FILE is ambiguous --
    excluded, not first-wins, whether the duplicates sit in the same
    crate or different ones (external review, medium severity: an
    earlier version only compared the set of owning CRATE NAMES, so two
    valid files in the SAME crate both silently fell through to
    whichever sorted first). The same "ambiguous, never first-wins"
    choice gate_g18.py already makes for a (concept, query) pair and
    gate_g14.py makes for a cross-crate bridge_id collision."""
    by_id: dict[str, list[dict]] = {}
    for _, data in collect_valid_witness_entries(descriptor, workspace):
        by_id.setdefault(data["witness_id"], []).append(data)

    specs: dict[str, dict] = {}
    findings: list[Finding] = []
    for witness_id, entries in sorted(by_id.items()):
        if len(entries) > 1:
            findings.append(
                Finding(
                    "G19", witness_id,
                    f"{len(entries)} genuinely valid witness specs declare this witness_id -- which "
                    "one is authoritative cannot be determined",
                )
            )
            continue
        specs[witness_id] = entries[0]
    return specs, findings


def regenerate_witness(
    spec: dict, backend: dict, workspace: Path, runner=subprocess.run
) -> tuple[dict | None, list[str], int, str]:
    """Invoke the configured witness_backend command fresh, passing the
    identifying fields FROM THE CURRENT SPEC as CLI arguments -- so a
    freshly regenerated result's identity is constructed from those
    exact values rather than read back from some other, possibly stale,
    source. Returns (document, argv, exit_code, error); document is
    None for every failure mode, the same shape gate_g9.py's dispatch()
    uses for the identical reason: a producer that failed to run has
    not established anything, and treating that as a value would turn
    an infrastructure problem into a determinism claim.

    Run with cwd=workspace (external review, medium severity: an
    earlier version ran the backend in the CALLER's directory, so
    invoking `pipeline.py --workspace /elsewhere gate-g19` from a
    different cwd left a producer unable to reliably resolve
    fixture/source files relative to the workspace it was meant to
    inspect, or resolving them against whatever checkout happened to be
    underfoot instead)."""
    argv = backend["command"].split() + [
        "--witness-id", spec["witness_id"],
        "--concept", spec["concept"],
        "--query", spec["query"],
        "--fixture-id", spec["fixture"]["fixture_id"],
        "--seed", str(spec["fixture"]["seed"]),
        "--renderer", spec["renderer"],
    ]
    try:
        completed = runner(argv, capture_output=True, text=True, cwd=workspace)
    except OSError as e:
        return None, argv, -1, f"could not invoke {argv[0]!r}: {e}"
    if completed.returncode != 0:
        return None, argv, completed.returncode, (
            f"exited {completed.returncode}: {(completed.stderr or '').strip()[:400]}"
        )
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as e:
        return None, argv, completed.returncode, f"output was not JSON: {e}"
    if not isinstance(document, dict):
        return None, argv, completed.returncode, "output was not a JSON object"
    return document, argv, completed.returncode, ""


def identity_mismatches(spec: dict, document: dict) -> list[str]:
    """Fields of `document` (a canonical result, freshly regenerated OR
    read from disk) that disagree with what `spec` currently declares:
    witness_id, concept, query, fixture_id, seed, and renderer_actual.

    Factored out (external review, chainlink #31) so gate_g20.py can
    apply the identical check to a STORED result before trusting its
    value_domain for a degeneracy check -- G20 reads
    load_results_by_witness()'s already-on-disk results rather than
    regenerating, so nothing about G19's own fresh-dispatch design
    protects it from a stale result left over from an earlier spec
    revision (different fixture_id/seed/concept/query) that still
    happens to validate against itself.

    renderer_actual is included despite being excluded from value_hash
    by design (§16.1's own "the same numbers from a different renderer
    are the same fact" rule) -- nothing about a value_hash comparison
    alone would ever catch a backend or a stale file reporting a
    different, still schema-valid renderer instead. That is exactly the
    declared/actual mismatch §16.2 makes a hard error at generation time
    (chainlink #28's generate_witness.py); this check applies the
    identical discipline wherever a result is trusted at all."""
    expected_identity = {
        "witness_id": spec["witness_id"],
        "concept": spec["concept"],
        "query": spec["query"],
        "fixture_id": spec["fixture"]["fixture_id"],
        "seed": spec["fixture"]["seed"],
        "renderer_actual": spec["renderer"],
    }
    return sorted(field for field, expected in expected_identity.items() if document.get(field) != expected)


def gate_workspace(workspace: Path, descriptor: dict, runner=subprocess.run) -> tuple[list[Finding], int]:
    """G19 proper. Returns (findings, witness specs checked)."""
    findings: list[Finding] = []

    specs, spec_findings = collect_valid_witness_specs(descriptor, workspace)
    findings.extend(spec_findings)
    backend = descriptor.get("witness_backend")
    result_validator = load_result_validator()

    for witness_id, spec in sorted(specs.items()):
        if backend is None:
            findings.append(
                Finding(
                    "G19", witness_id,
                    "no witness_backend configured in the project descriptor -- a determinism claim "
                    "cannot be verified without an actual regeneration this run, so this gate cannot "
                    "record a pass for a query that was never re-evaluated",
                )
            )
            continue

        document, _, _, error = regenerate_witness(spec, backend, workspace, runner)
        if document is None:
            findings.append(
                Finding("G19", witness_id, f"witness_backend failed to regenerate this witness: {error}")
            )
            continue

        result_findings = validate_result_data(Path(f"{witness_id}.json"), document, result_validator)
        result_errors = [f for f in result_findings if f.severity == "error"]
        if result_errors:
            findings.append(
                Finding(
                    "G19", witness_id,
                    "the regenerated result is not genuinely valid: "
                    + "; ".join(f.reason for f in result_errors),
                )
            )
            continue

        mismatched = identity_mismatches(spec, document)
        if mismatched:
            findings.append(
                Finding(
                    "G19", witness_id,
                    f"the regenerated result's {', '.join(sorted(mismatched))} does not match the "
                    "current witness spec -- witness_backend ignored (or was passed) arguments other "
                    "than what this spec currently declares, so its value_hash cannot be trusted as "
                    "this witness's determinism check",
                )
            )
            continue

        declared = spec["determinism"]["value_hash"]
        regenerated_hash = document["value_hash"]
        if declared != regenerated_hash:
            findings.append(
                Finding(
                    "G19", witness_id,
                    f"determinism.value_hash {declared} does not match the regenerated value_hash "
                    f"{regenerated_hash} -- the declared byte-identical-across-runs claim does not "
                    "hold; render_hash is never part of this comparison",
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
