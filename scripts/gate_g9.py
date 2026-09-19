#!/usr/bin/env python3
"""G9: bridge checks -- generate, dispatch, and verify.

plan.md §8.3 and §12's G9 row ("obligation or mitigation regresses;
bridge check fails", Stage 8A / CI), chainlink #47.

Two halves, deliberately separated the way #24 separated extraction from
its gate:

  check_bridges()   compile every promoted bridge to a harness
                    (scripts/bridge_harness.py), write the harness, and
                    dispatch it to the bridge's OWNING verifier, recording
                    what came back at
                    ci/results/bridge_checks/<bridge_id>.json.
  gate_workspace()  recompile from the promoted bridge and check that the
                    recorded result is about THIS bridge: the harness on
                    disk still matches what the bridge compiles to, the
                    record's harness hash matches the recomputation, the
                    verifier that ran it owns the cluster, and the claim
                    passed. Then cross-check any bridge_record in a work
                    package's assurance report against it.

The machine relation this replaces
----------------------------------
plan.md §8.3's temporary alternative: "make the harness itself normative
and hash-pinned, and report `harness-tested` -- never `bridge-checked`,
because no machine relation exists between prose and harness." A pinned
hand-written harness pins *a* harness; nothing tied it to the bridge it
claimed to discharge. Now the harness is DERIVED, so its hash is a
property of the promoted bridge_logic, and every one of the checks above
is a comparison against a recomputation rather than against a stored
value someone typed. The last of them matters most: chainlink #25's G14
consumes `bridge_records` out of a work package's assurance report, and
until now nothing stopped one of those from asserting a bridge check
that never ran.

Verifier dispatch in a workspace with no Rust
---------------------------------------------
This repository has no real crates, so nothing here can run Creusot,
Kani or Verus -- and the honest response is to build the dispatch for
real and refuse to fake the verdict. `verifier_backends` in the project
descriptor is a pluggable command per verifier, exactly like
`llm_backend` (plan.md §6.1) is for Stage 0/3. With no backend
configured for a verifier, `check-bridges` does not write a record and
says why; a missing record then blocks at the gate. The one thing this
module will never do is manufacture a pass for a verifier that never
ran. Tests drive it through a fixture verifier script, the same
precedent as tests/fixtures/callsites/'s Rust source that is never
compiled -- real machinery, exercised against a stand-in, with the
stand-in visible.

The runner protocol is one JSON object on stdout:

    {"result": "pass" | "fail",
     "scope": {"input_domain": "...", ...},
     "assumptions": ["..."],
     "config": {"toolchain": "...", "target": "...", "features": ["..."]},
     "detail": "optional human-readable note"}

`config` is required from the runner rather than filled in from the
descriptor: the toolchain that actually ran is a fact about the run, and
copying it from a declaration would be recording an intention as an
observation.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resources  # noqa: E402
from bridge_harness import CompileError  # noqa: E402
from bridge_harness import EVIDENCE_KIND_BY_VERIFIER  # noqa: E402
from bridge_harness import Harness  # noqa: E402
from bridge_harness import compile_bridge  # noqa: E402
from gate_g14 import load_bridges  # noqa: E402
from gate_g14 import load_manifests  # noqa: E402
from satisfies import load_achieved_validator  # noqa: E402
from scan_summary import pass_line  # noqa: E402
from schema_utils import make_validator  # noqa: E402
from validate_closure import load_cluster_artifacts  # noqa: E402

DOCS = resources.resource_path("docs")
BRIDGE_CHECK_SCHEMA_PATH = DOCS / "bridge-check-schema.json"

HARNESS_DIR = ("ci", "harness")
CHECK_DIR = ("ci", "results", "bridge_checks")

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


def harness_dir_for(workspace: Path) -> Path:
    """ci/harness/. Generated, never hand-edited -- the header of every
    file says so, and the gate rejects drift from a recompilation."""
    return workspace.joinpath(*HARNESS_DIR).resolve()


def bridge_check_dir_for(workspace: Path) -> Path:
    return workspace.joinpath(*CHECK_DIR).resolve()


def load_bridge_check_validator():
    return make_validator(json.loads(BRIDGE_CHECK_SCHEMA_PATH.read_text()))


def owning_verifier_by_bridge(workspace: Path, descriptor: dict) -> dict[str, str]:
    """Which verifier system owns each bridge.

    plan.md §4's verifier_policy is per CLUSTER, and a bridge does not
    name its cluster -- but a closure profile names its work packages,
    and a work package's definition_of_done names the bridges it must
    establish (required_preconditions_to_establish). That chain already
    exists, so this resolves through it rather than inventing a second
    ownership declaration. A bridge no cluster claims falls back to
    verifier_policy.default, and a bridge claimed by two clusters with
    different verifiers is left unresolved (None) -- an ambiguity the
    gate reports rather than picks a side of."""
    policy = descriptor["verifier_policy"]
    manifests, _ = load_manifests(workspace)
    clusters = load_cluster_artifacts(workspace)

    verifiers_by_bridge: dict[str, set[str]] = {}
    for cluster, entry in clusters.items():
        if "profile" not in entry:
            continue
        verifier = policy.get(cluster, policy["default"])
        for work_package in entry["profile"][1]["work_packages"]:
            manifest = manifests.get(work_package)
            if manifest is None:
                continue
            for precondition in manifest["definition_of_done"]["required_preconditions_to_establish"]:
                verifiers_by_bridge.setdefault(precondition["bridge_id"], set()).add(verifier)

    resolved: dict[str, str] = {}
    for bridge_id, verifiers in verifiers_by_bridge.items():
        if len(verifiers) == 1:
            resolved[bridge_id] = next(iter(verifiers))
    return resolved


def resolve_verifier(bridge_id: str, owned: dict[str, str], descriptor: dict) -> str:
    return owned.get(bridge_id, descriptor["verifier_policy"]["default"])


def write_harness(workspace: Path, harness: Harness) -> Path:
    path = harness_dir_for(workspace) / harness.filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(harness.source)
    return path


def dispatch(
    harness: Harness, harness_path: Path, backend: dict, runner=subprocess.run
) -> tuple[dict | None, list[str], int, str]:
    """Invoke the configured verifier command on the generated harness.

    Returns (verdict, argv, exit_code, error). A crashed or unparsable
    runner yields verdict=None: a verifier that failed to run has not
    refuted anything, and recording that as `result: fail` would turn an
    infrastructure problem into a claim about the bridge."""
    argv = backend["command"].split() + [str(harness_path)]
    try:
        completed = runner(argv, capture_output=True, text=True)
    except OSError as e:
        return None, argv, -1, f"could not invoke {argv[0]!r}: {e}"
    if completed.returncode != 0:
        return None, argv, completed.returncode, (
            f"exited {completed.returncode}: {(completed.stderr or '').strip()[:400]}"
        )
    try:
        verdict = json.loads(completed.stdout)
    except json.JSONDecodeError as e:
        return None, argv, completed.returncode, f"output was not JSON: {e}"
    if not isinstance(verdict, dict):
        return None, argv, completed.returncode, "output was not a JSON object"
    missing = [key for key in ("result", "scope", "assumptions", "config") if key not in verdict]
    if missing:
        return None, argv, completed.returncode, f"verdict is missing {missing!r}"
    if verdict["result"] not in ("pass", "fail"):
        return None, argv, completed.returncode, f"verdict result {verdict['result']!r} is not pass/fail"
    return verdict, argv, completed.returncode, ""


def build_record(harness: Harness, verdict: dict) -> dict:
    """The achieved assurance record for a bridge check. claim.kind is
    always `callee-precondition-established` -- that is what a bridge
    establishes (plan.md §8.2's implication), and it is not the runner's
    to choose. Everything else comes from the verdict."""
    scope = dict(verdict["scope"])
    scope["harness_hash"] = harness.sha256
    return {
        "schema_version": "1.0",
        "claim": {"kind": "callee-precondition-established", "result": verdict["result"]},
        "evidence": {
            "kind": harness.evidence_kind,
            "verifier": harness.verifier,
            "harness": harness.name,
            "scope": scope,
        },
        "trust": {"assumptions": list(verdict["assumptions"])},
        "support": {"status": "supported"},
        "config": verdict["config"],
    }


def check_bridges(
    workspace: Path, descriptor: dict, runner=subprocess.run
) -> tuple[list[Path], list[Finding]]:
    """Compile, write, dispatch, record. Every step that cannot be
    completed produces a finding and no record -- an absent record blocks
    at the gate, which is the honest outcome for a bridge nothing
    checked."""
    findings: list[Finding] = []
    written: list[Path] = []
    backends = descriptor.get("verifier_backends") or {}
    owned = owning_verifier_by_bridge(workspace, descriptor)
    bridges, bridge_findings = load_bridges(workspace, descriptor)
    findings.extend(bridge_findings)

    for bridge_id in sorted(bridges):
        bridge = bridges[bridge_id]
        verifier = resolve_verifier(bridge_id, owned, descriptor)
        try:
            harness = compile_bridge(bridge, verifier)
        except CompileError as e:
            findings.append(
                Finding("G9", bridge_id, f"bridge_logic does not compile for {verifier}: {e}")
            )
            continue
        harness_path = write_harness(workspace, harness)
        written.append(harness_path)

        backend = backends.get(verifier)
        if backend is None:
            findings.append(
                Finding(
                    "G9", bridge_id,
                    f"harness generated for {verifier} at {harness_path.relative_to(workspace)}, but "
                    f"the project descriptor configures no verifier_backends.{verifier} command -- "
                    "nothing was checked, so no result is recorded (the ceiling here is "
                    "harness-tested, never bridge-checked)",
                )
            )
            continue

        verdict, argv, exit_code, error = dispatch(harness, harness_path, backend, runner)
        if verdict is None:
            findings.append(
                Finding(
                    "G9", bridge_id,
                    f"verifier dispatch failed ({error}) -- a verifier that did not run has not "
                    "refuted anything, so no result is recorded",
                )
            )
            continue

        record = {
            "schema_version": "1.0",
            "bridge_id": bridge_id,
            "verifier": verifier,
            "harness": {
                "name": harness.name,
                "language": harness.language,
                "filename": harness.filename,
                "sha256": harness.sha256,
            },
            "dispatch": {"command": argv, "exit_code": exit_code},
            "record": build_record(harness, verdict),
        }
        path = bridge_check_dir_for(workspace) / f"{bridge_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2) + "\n")
        written.append(path)
    return written, findings


def load_bridge_checks(workspace: Path) -> tuple[dict[str, dict], list[Finding]]:
    """Every schema-valid bridge check record, by bridge_id. An invalid or
    misnamed record is reported, never skipped silently."""
    findings: list[Finding] = []
    records: dict[str, dict] = {}
    directory = bridge_check_dir_for(workspace)
    if not directory.is_dir():
        return records, findings

    validator = load_bridge_check_validator()
    achieved_validator = load_achieved_validator()
    for path in sorted(p for p in directory.iterdir() if p.is_file()):
        if path.suffix != ".json":
            findings.append(Finding("G9", str(path), "bridge check records are .json files"))
            continue
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            findings.append(Finding("G9", str(path), f"not readable JSON: {e}"))
            continue
        errors = list(validator.iter_errors(data))
        if errors:
            findings.append(Finding("G9", str(path), f"not schema-valid: {errors[0].message}"))
            continue
        if data["bridge_id"] != path.stem:
            findings.append(
                Finding(
                    "G9", str(path),
                    f"declares bridge_id {data['bridge_id']!r} but its filename says {path.stem!r}",
                )
            )
            continue
        achieved_errors = list(achieved_validator.iter_errors(data["record"]))
        if achieved_errors:
            findings.append(
                Finding(
                    "G9", data["bridge_id"],
                    f"its achieved assurance record is not schema-valid: {achieved_errors[0].message}",
                )
            )
            continue
        records[data["bridge_id"]] = data
    return records, findings


def _assurance_report_bridge_records(workspace: Path) -> dict[str, list[tuple[str, dict]]]:
    """Every bridge_record in every work package's assurance report,
    indexed by bridge_id -- what chainlink #25's G14 consumes, and what
    nothing until now compared against an actual check."""
    manifests, _ = load_manifests(workspace)
    found: dict[str, list[tuple[str, dict]]] = {}
    for work_package, manifest in manifests.items():
        path = (workspace / manifest["report"]["emit"]).resolve()
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        for entry in (data.get("bridge_records") or []):
            if isinstance(entry, dict) and isinstance(entry.get("bridge_id"), str):
                found.setdefault(entry["bridge_id"], []).append((work_package, entry.get("record") or {}))
    return found


def bridge_records_for(workspace: Path) -> list[dict]:
    """The recorded bridge checks in the shape a work package's assurance
    report wants (docs/assurance-report-schema.json's `bridge_records`).

    Provided so the loop can be closed the honest way round: a work
    package's CI assembles its report FROM the checks that ran, instead
    of a human writing a bridge_record and a gate later discovering it
    matches nothing. G14 consumes those records; G9's cross-check exists
    for the reports that were not assembled this way."""
    checks, _ = load_bridge_checks(workspace)
    return [
        {"bridge_id": bridge_id, "record": checks[bridge_id]["record"]}
        for bridge_id in sorted(checks)
    ]


def gate_workspace(workspace: Path, descriptor: dict) -> tuple[list[Finding], int]:
    """G9 proper: every promoted bridge must have a check that is
    demonstrably about IT. Returns (findings, bridges discovered)."""
    findings: list[Finding] = []
    bridges, bridge_findings = load_bridges(workspace, descriptor)
    findings.extend(bridge_findings)
    checks, load_findings = load_bridge_checks(workspace)
    findings.extend(load_findings)
    owned = owning_verifier_by_bridge(workspace, descriptor)
    reported = _assurance_report_bridge_records(workspace)

    for bridge_id in sorted(bridges):
        bridge = bridges[bridge_id]
        verifier = resolve_verifier(bridge_id, owned, descriptor)
        try:
            harness = compile_bridge(bridge, verifier)
        except CompileError as e:
            findings.append(
                Finding("G9", bridge_id, f"bridge_logic does not compile for {verifier}: {e}")
            )
            continue

        harness_path = harness_dir_for(workspace) / harness.filename
        if not harness_path.is_file():
            findings.append(
                Finding(
                    "G9", bridge_id,
                    f"no generated harness at {harness_path.relative_to(workspace)} -- run "
                    "`pipeline.py check-bridges`",
                )
            )
        elif harness_path.read_text() != harness.source:
            findings.append(
                Finding(
                    "G9", bridge_id,
                    f"the harness on disk differs from what this bridge compiles to -- it was "
                    "edited by hand or the bridge changed after generation; a hand-edited harness "
                    "is exactly the un-related harness the derived hash exists to rule out",
                )
            )

        check = checks.get(bridge_id)
        if check is None:
            findings.append(
                Finding(
                    "G9", bridge_id,
                    "no bridge check record -- the harness may exist, but nothing recorded a "
                    "verifier checking it, so the strongest honest claim is harness-tested, never "
                    "bridge-checked (plan.md §8.3)",
                )
            )
            continue

        if check["verifier"] != verifier:
            findings.append(
                Finding(
                    "G9", bridge_id,
                    f"was checked by {check['verifier']!r} but its cluster's owning verifier is "
                    f"{verifier!r} -- a result from another system is a cross-verifier composition "
                    "(CG1), not this bridge's check",
                )
            )
        if check["harness"]["sha256"] != harness.sha256:
            findings.append(
                Finding(
                    "G9", bridge_id,
                    f"the checked harness hash {check['harness']['sha256']} is not what this "
                    f"bridge compiles to ({harness.sha256}) -- the recorded result is about a "
                    "different harness and says nothing about this bridge",
                )
            )

        record = check["record"]
        if record["evidence"]["harness"] != harness.name:
            findings.append(
                Finding(
                    "G9", bridge_id,
                    f"the achieved record names harness {record['evidence']['harness']!r}, not the "
                    f"generated {harness.name!r}",
                )
            )
        if record["evidence"]["scope"].get("harness_hash") != harness.sha256:
            findings.append(
                Finding(
                    "G9", bridge_id,
                    "the achieved record's evidence.scope.harness_hash does not match the "
                    "recomputed harness -- satisfies() compares scope by exact string equality, so "
                    "a stale hash would otherwise pass a minimum_scope check silently",
                )
            )
        if record["claim"]["result"] != "pass":
            findings.append(
                Finding("G9", bridge_id, "bridge check failed (claim.result is not 'pass')")
            )
        if record["claim"]["kind"] != "callee-precondition-established":
            findings.append(
                Finding(
                    "G9", bridge_id,
                    f"claim.kind is {record['claim']['kind']!r} -- a bridge establishes a callee "
                    "precondition (plan.md §8.2); anything else is a different claim wearing this "
                    "bridge's name",
                )
            )
        if record["evidence"]["kind"] != EVIDENCE_KIND_BY_VERIFIER[verifier]:
            findings.append(
                Finding(
                    "G9", bridge_id,
                    f"evidence.kind {record['evidence']['kind']!r} is not the method "
                    f"{verifier} produces ({EVIDENCE_KIND_BY_VERIFIER[verifier]!r})",
                )
            )

        for work_package, reported_record in reported.get(bridge_id, []):
            mismatches = []
            if (reported_record.get("evidence") or {}).get("harness") != harness.name:
                mismatches.append("evidence.harness")
            if ((reported_record.get("evidence") or {}).get("scope") or {}).get("harness_hash") != harness.sha256:
                mismatches.append("evidence.scope.harness_hash")
            if (reported_record.get("claim") or {}).get("result") != record["claim"]["result"]:
                mismatches.append("claim.result")
            if mismatches:
                findings.append(
                    Finding(
                        "G9", bridge_id,
                        f"{work_package}'s assurance report asserts a bridge_record that disagrees "
                        f"with the recorded check on {mismatches!r} -- G14 consumes that record as "
                        "evidence a bridge passed, so a bridge_record nobody's check backs is a "
                        "claim, not a result",
                    )
                )

    for bridge_id in sorted(set(checks) - set(bridges)):
        findings.append(
            Finding(
                "G9", bridge_id,
                "a bridge check record exists for no valid promoted bridge -- a leftover result "
                "for a bridge that was deleted or never passed validate-bridge",
            )
        )

    return findings, len(bridges)


def report_findings(findings: list[Finding], discovered: int, workspace: Path) -> int:
    errors = [f for f in findings if f.severity == "error"]
    infos = [f for f in findings if f.severity == "info"]

    if infos:
        print(f"INFO: {len(infos)} non-blocking finding(s)")
        for finding in infos:
            print(f"  - {finding}")

    if discovered == 0:
        print(
            "FAIL: no promoted bridge discovered -- G9 has nothing to check, and a bridge gate "
            f"that passes over zero bridges would claim what it never ran (looked under {workspace})"
        )
        return EXIT_BLOCKED

    if not errors:
        print(pass_line(discovered, "bridges", "G9 (compiled, dispatched, and checked)", workspace))
        return EXIT_OK

    print(f"FAIL: {len(errors)} finding(s)")
    for finding in errors:
        print(f"  - {finding}")
    return EXIT_BLOCKED


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--descriptor", type=Path, default=None)
    parser.add_argument(
        "--generate", action="store_true",
        help="compile, write and dispatch first (equivalent to `pipeline.py check-bridges`)",
    )
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

    findings: list[Finding] = []
    if args.generate:
        _, generation_findings = check_bridges(args.workspace, descriptor)
        findings.extend(generation_findings)

    gate_findings, discovered = gate_workspace(args.workspace, descriptor)
    findings.extend(gate_findings)
    return report_findings(findings, discovered, args.workspace)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
