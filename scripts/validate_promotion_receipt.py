#!/usr/bin/env python3
"""Promotion receipt validation (plan.md §7.1, chainlink #15).

A detached receipt stating the *exact* accepted artifact set -- no
implicit globs. G1a (schema) first, then real mechanical checks, built in
from the start rather than discovered by review one at a time the way
scripts/validate_work_package.py's did:

  - Every artifact_manifest path is workspace-relative (no absolute path,
    no ".." segment) -- checked syntactically before any filesystem
    resolution, same discipline as check_write_set_anchoring.
  - Every artifact_manifest path resolves *inside* workspace_root even
    after resolution (guards a symlink or an unusual relative path that
    passes the syntactic check but still escapes) -- same discipline as
    check_gate_integrity's containment check.
  - Every listed file's *actual current hash* is computed and compared to
    the declared hash. A mismatch means exactly what plan.md §7.1 says it
    means: the receipt is invalidated, acceptance is revoked -- reported
    as an error, not a warning.
  - The receipt is never listed in its own artifact_manifest (plan.md
    §7.1: "the receipt is not in its own manifest").
  - One-way references: no artifact the receipt lists may itself carry a
    promotion_id field (plan.md §7.1: normative artifacts carry review
    blocks and never a promotion_id; only generated reports cite one).

No optional context and no escape-hatch flags: unlike assumption-ref
resolution in validate_work_package.py, every check here only needs
workspace_root, which is never optional, so there is no "missing search
root" class of gap to guard against and nothing to silently degrade.
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

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "docs" / "promotion-receipt-schema.json"


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
    """One promotion receipt per cluster -- filename stem must equal
    cluster, the same discipline as boundary_id==stem for boundary
    contracts (plan.md §2), applied here as this pipeline's own
    convention rather than a directly-quoted plan.md rule (the plan's
    worked example names the file this way but doesn't spell out the
    rule in prose the way it does for boundaries)."""
    if path.stem != data["cluster"]:
        return [
            Finding(
                "G1b", path,
                f"filename stem {path.stem!r} does not match cluster {data['cluster']!r}",
            )
        ]
    return []


def _pattern_escapes_workspace(pattern: str) -> bool:
    if pattern.startswith("/"):
        return True
    return ".." in pattern.split("/")


def _sha256(file_path: Path) -> str:
    return "sha256:" + hashlib.sha256(file_path.read_bytes()).hexdigest()


def check_artifact_manifest(path: Path, data: dict, workspace_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    workspace_resolved = workspace_root.resolve()
    receipt_resolved = path.resolve()

    for entry in data["artifact_manifest"]:
        entry_path = entry["path"]

        if _pattern_escapes_workspace(entry_path):
            findings.append(
                Finding(
                    "7.1", path,
                    f"artifact_manifest path {entry_path!r} is not workspace-relative -- "
                    "absolute paths and .. segments are refused, not resolved",
                )
            )
            continue

        resolved = (workspace_root / entry_path).resolve()
        try:
            resolved.relative_to(workspace_resolved)
        except ValueError:
            findings.append(
                Finding("7.1", path, f"artifact_manifest path {entry_path!r} resolves outside the workspace")
            )
            continue

        if resolved == receipt_resolved:
            findings.append(
                Finding(
                    "7.1", path,
                    f"artifact_manifest lists the receipt's own path ({entry_path!r}) -- "
                    "the receipt is not in its own manifest (plan.md §7.1)",
                )
            )
            continue

        if not resolved.is_file():
            findings.append(
                Finding("7.1", path, f"artifact_manifest path {entry_path!r} does not exist")
            )
            continue

        actual = _sha256(resolved)
        if actual != entry["hash"]:
            findings.append(
                Finding(
                    "7.1", path,
                    f"{entry_path!r} hash mismatch -- declared {entry['hash']}, actual {actual}: "
                    "acceptance is revoked (plan.md §7.1: any listed file changing invalidates the receipt)",
                )
            )
            continue

        if resolved.suffix == ".json":
            try:
                artifact_data = json.loads(resolved.read_text())
            except json.JSONDecodeError:
                artifact_data = None
            if isinstance(artifact_data, dict) and "promotion_id" in artifact_data:
                findings.append(
                    Finding(
                        "7.1", path,
                        f"{entry_path!r} carries a promotion_id field -- references are one-way "
                        "(plan.md §7.1: normative artifacts carry review blocks and never a "
                        "promotion_id; only generated reports cite one)",
                    )
                )

    return findings


def validate_data(path: Path, data: dict, validator: Draft202012Validator, workspace_root: Path) -> list[Finding]:
    g1a = gate_g1a(path, data, validator)
    if g1a:
        return g1a

    findings: list[Finding] = []
    findings.extend(check_naming(path, data))
    findings.extend(check_artifact_manifest(path, data, workspace_root))
    return findings


def _load_receipt(path: Path) -> dict:
    text = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        return yaml.safe_load(text)
    return json.loads(text)


def validate_file(path: Path, validator: Draft202012Validator, workspace_root: Path) -> list[Finding]:
    try:
        data = _load_receipt(path)
    except (json.JSONDecodeError, yaml.YAMLError) as e:
        return [Finding("G1a", path, f"invalid {path.suffix or 'JSON'}: {e}")]
    return validate_data(path, data, validator, workspace_root)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("receipt", type=Path, help="Path to a promotion receipt .json or .yaml file")
    parser.add_argument("--workspace-root", type=Path, default=Path("."))
    args = parser.parse_args(argv)

    validator = load_validator()
    findings = validate_file(args.receipt, validator, args.workspace_root)

    if not findings:
        print("OK: promotion receipt passes G1a and §7.1 checks")
        return 0

    print(f"FAIL: {len(findings)} finding(s)")
    for f in findings:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
