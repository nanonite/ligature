#!/usr/bin/env python3
"""Work-package manifest validation (plan.md §10/§10.1, chainlink #14).

The artifact boundary between this pipeline and the orchestrator that
implements the Rust code -- this schema IS the interface spec for
"outputs to pass to an orchestrator." G1a (schema) first, then the
§10.1 validator rules that are genuinely mechanical today:

  - gate_integrity hashes match the real files on disk BEFORE any gate
    runs (G13 pre-flight) -- computed and compared, not just structurally
    shaped.
  - allowed_write_set and protected_write_set don't literally overlap.
  - every trusted_assumptions[].assumption_ref resolves to a real boundary
    contract's own assumptions[] entry with a matching tracking_issue and
    assumption_hash (not just a plausible-looking string).
  - harness names contain no wildcard characters (schema-enforced,
    exact_harness_name $def).

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


def check_write_set_disjointness(path: Path, data: dict) -> list[Finding]:
    allowed = set(data["write_policy"]["allowed_write_set"])
    protected = set(data["write_policy"]["protected_write_set"])
    overlap = allowed & protected
    if not overlap:
        return []
    return [
        Finding(
            "G13",
            path,
            f"{sorted(overlap)} listed in both allowed_write_set and protected_write_set",
        )
    ]


def _sha256(file_path: Path) -> str:
    return "sha256:" + hashlib.sha256(file_path.read_bytes()).hexdigest()


def check_gate_integrity(path: Path, data: dict, workspace_root: Path) -> list[Finding]:
    """G13 pre-flight: every gate implementation hash must match before
    any gate runs. An implementing agent that can edit scripts/ can
    otherwise make a wrong feature look right."""
    findings: list[Finding] = []
    for entry in data["gate_integrity"]:
        runner_path = workspace_root / entry["runner"]
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
    issue + hash. Not just schema-shaped -- actually resolved against the
    referenced boundary contract's own assumptions[] entries."""
    findings: list[Finding] = []
    assumptions = data["definition_of_done"]["trusted_assumptions"]
    if not assumptions:
        return findings

    if specs_search_root is None:
        return [
            Finding(
                "10.1",
                path,
                f"{len(assumptions)} trusted_assumptions unverifiable -- no specs_search_root given",
                severity="info",
            )
        ]

    for entry in assumptions:
        ref = entry["assumption_ref"]
        boundary_id = ref["boundary_id"]
        candidates = sorted(specs_search_root.glob(f"**/_boundaries/{boundary_id}.json"))
        if not candidates:
            findings.append(
                Finding(
                    "10.1", path,
                    f"assumption_ref {boundary_id!r} does not resolve to any boundary contract",
                )
            )
            continue

        boundary = json.loads(candidates[0].read_text())
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


def validate_file(
    path: Path, validator: Draft202012Validator, workspace_root: Path, specs_search_root: Path | None = None
) -> list[Finding]:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        return [Finding("G1a", path, f"invalid JSON: {e}")]
    return validate_data(path, data, validator, workspace_root, specs_search_root)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("manifest", type=Path, help="Path to a work-package manifest JSON file")
    parser.add_argument("--workspace-root", type=Path, default=Path("."))
    parser.add_argument("--specs-search-root", type=Path, default=None)
    args = parser.parse_args(argv)

    validator = load_validator()
    findings = validate_file(args.manifest, validator, args.workspace_root, args.specs_search_root)
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
