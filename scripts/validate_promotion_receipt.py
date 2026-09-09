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
  - A canonical crate-scoped witness spec (<crate_dir>/specs/_witnesses/
    *.json) is hashed with validate_witness.witness_promotion_digest
    (canonical JSON over the spec, output.render_hash excluded) instead
    of a plain byte hash (plan.md §16.5, chainlink #35) -- a
    rendering-only regeneration must never revoke acceptance, while a
    fixture or determinism.value_hash change still does. Recognizing
    this requires an optional project descriptor (see below); every
    other artifact keeps the original plain-byte-hash comparison
    unconditionally.

Every check besides witness recognition needs only workspace_root,
which is never optional -- no "missing search root" class of gap to
guard against there. Witness recognition alone takes an OPTIONAL
descriptor (unlike validate_work_package.py's mandatory-choice
boundary-directory flags, since most receipts have no witnesses at
all): omitted, and no artifact_manifest entry sits under a directory
literally named "_witnesses", this module's original,
witness-unaware behavior is reproduced exactly; omitted while such an
entry IS present, that entry fails closed rather than being silently
trusted as an ordinary byte-hashed artifact or silently ignored.
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
from project_descriptor import ProjectDescriptorError  # noqa: E402
from project_descriptor import load_project_descriptor  # noqa: E402
from schema_utils import make_validator  # noqa: E402
from validate_witness import CANONICAL_DIR_NAME as WITNESS_DIR_NAME  # noqa: E402
from validate_witness import load_validator as load_witness_validator  # noqa: E402
from validate_witness import validate_data as validate_witness_data  # noqa: E402
from validate_witness import witness_dir_for  # noqa: E402
from validate_witness import witness_promotion_digest  # noqa: E402

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


def check_naming(path: Path, data: dict, workspace_root: Path) -> list[Finding]:
    """One promotion receipt per cluster, living at the canonical
    specs/_promotions/<cluster>.json|yaml -- the same discipline as
    boundary_id==stem plus the boundary-directory anchoring pipeline.py
    applies to boundary contracts (plan.md §2), established here as this
    pipeline's own convention rather than a directly-quoted plan.md rule
    (the worked example names and places the file this way but doesn't
    spell out the rule in prose the way it does for boundaries).

    A prior version only checked the filename stem against cluster,
    without checking the receipt itself resolves inside the workspace or
    lives under the canonical directory -- a receipt at an arbitrary path
    like /tmp/scheduling.json validated cleanly against a fixture
    workspace it wasn't even part of."""
    findings: list[Finding] = []

    if path.stem != data["cluster"]:
        findings.append(
            Finding(
                "G1b", path,
                f"filename stem {path.stem!r} does not match cluster {data['cluster']!r}",
            )
        )

    resolved = path.resolve()
    workspace_resolved = workspace_root.resolve()
    try:
        resolved.relative_to(workspace_resolved)
    except ValueError:
        findings.append(
            Finding("G1b", path, f"receipt does not resolve inside the workspace {workspace_resolved}")
        )
        return findings  # canonical-directory check below is meaningless if this failed

    canonical_dir = (workspace_root / "specs" / "_promotions").resolve()
    if resolved.parent != canonical_dir:
        findings.append(
            Finding(
                "G1b", path,
                f"receipt is not directly under the canonical specs/_promotions/ directory "
                f"({canonical_dir}) -- found at {resolved.parent}",
            )
        )

    return findings


def _pattern_escapes_workspace(pattern: str) -> bool:
    if pattern.startswith("/"):
        return True
    return ".." in pattern.split("/")


def _sha256(file_path: Path) -> str:
    return "sha256:" + hashlib.sha256(file_path.read_bytes()).hexdigest()


def _load_structured_artifact(resolved: Path) -> dict | None:
    """Best-effort load for the one-way-reference check below -- returns
    None (not an error) for anything that isn't a dict-shaped JSON/YAML
    document, since e.g. a .rs source file or a .md policy doc is a
    legitimate artifact_manifest entry with nothing to check here.
    Deliberately covers .yaml/.yml, not just .json: a review round found
    the original version only ever inspected .json artifacts, so a
    correctly-hashed YAML artifact with its own promotion_id field
    passed with zero findings -- the §7.1 rule is about the artifact
    being normative, not about which serialization it happens to use."""
    try:
        if resolved.suffix == ".json":
            data = json.loads(resolved.read_text())
        elif resolved.suffix in (".yaml", ".yml"):
            data = yaml.safe_load(resolved.read_text())
        else:
            return None
    except (json.JSONDecodeError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _witness_promotion_hash_if_applicable(
    descriptor: dict | None, workspace_root: Path, entry_path: str, resolved: Path
) -> tuple[str | None, Finding | None]:
    """Mirrors generate_promotion_receipt.py's own function of the same
    name exactly (chainlink #35) -- the SAME rule
    (validate_witness.witness_promotion_digest, validated first with
    validate_witness.py's own G1a/G1b/G2) must decide what "the
    witness's promotion-relevant content" means on both the generation
    and the validation side, or the two could silently disagree about
    what invalidates a receipt.

    Returns (hash_or_None, finding_or_None): a non-None finding means
    "fail closed here, do not fall back to an ordinary byte hash" --
    used for a path that LOOKS like a witness (its parent directory is
    literally named "_witnesses") but cannot be trusted as one: no
    descriptor to confirm crate anchoring, a directory that doesn't
    match any declared crate's canonical witness_dir_for, or a spec
    that fails its own validator."""
    looks_like_witness = resolved.parent.name == WITNESS_DIR_NAME
    if descriptor is None:
        if looks_like_witness:
            return None, Finding(
                "7.1", resolved,
                f"{entry_path!r} sits under a {WITNESS_DIR_NAME!r} directory but no project "
                "descriptor was supplied to confirm it is a real crate's canonical witness directory",
            )
        return None, None

    for crate in descriptor["crates"]:
        if resolved.parent != witness_dir_for(crate, workspace_root):
            continue
        specs_search_root = workspace_root / crate["specs_search_root"]
        try:
            data = json.loads(resolved.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return None, Finding("7.1", resolved, f"{entry_path!r} is not readable JSON: {e}")
        if not isinstance(data, dict):
            return None, Finding("7.1", resolved, f"{entry_path!r} is not a JSON object")
        witness_findings = validate_witness_data(resolved, data, load_witness_validator(), specs_search_root)
        errors = [f for f in witness_findings if f.severity == "error"]
        if errors:
            return None, Finding(
                "7.1", resolved,
                f"{entry_path!r} does not pass its own witness validator: "
                + "; ".join(f.reason for f in errors),
            )
        return witness_promotion_digest(data), None

    if looks_like_witness:
        return None, Finding(
            "7.1", resolved,
            f"{entry_path!r} sits under a {WITNESS_DIR_NAME!r} directory but not at any declared "
            "crate's own canonical witness directory",
        )
    return None, None


def check_artifact_manifest(
    path: Path, data: dict, workspace_root: Path, descriptor: dict | None = None
) -> list[Finding]:
    findings: list[Finding] = []
    workspace_resolved = workspace_root.resolve()
    receipt_resolved = path.resolve()
    seen_resolved: dict[Path, str] = {}  # resolved path -> the first entry_path that named it

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

        if resolved in seen_resolved:
            # The manifest "states the exact set" (plan.md §7.1) -- a
            # repeated entry, or two different path strings that resolve
            # to the same real file (e.g. a symlink alias), isn't a
            # second artifact. Checked by resolved identity, not string
            # equality, since schema-level uniqueItems on the path string
            # wouldn't catch an alias.
            findings.append(
                Finding(
                    "7.1", path,
                    f"artifact_manifest lists {entry_path!r} and {seen_resolved[resolved]!r}, which "
                    "resolve to the same file -- the manifest states the exact artifact set, not a "
                    "set with duplicates or aliases",
                )
            )
            continue
        seen_resolved[resolved] = entry_path

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

        witness_hash, witness_finding = _witness_promotion_hash_if_applicable(
            descriptor, workspace_root, entry_path, resolved
        )
        if witness_finding is not None:
            findings.append(witness_finding)
            continue

        actual = witness_hash or _sha256(resolved)
        if actual != entry["hash"]:
            findings.append(
                Finding(
                    "7.1", path,
                    f"{entry_path!r} hash mismatch -- declared {entry['hash']}, actual {actual}: "
                    "acceptance is revoked (plan.md §7.1: any listed file changing invalidates the receipt)",
                )
            )
            continue

        artifact_data = _load_structured_artifact(resolved)
        if artifact_data is not None and "promotion_id" in artifact_data:
            findings.append(
                Finding(
                    "7.1", path,
                    f"{entry_path!r} carries a promotion_id field -- references are one-way "
                    "(plan.md §7.1: normative artifacts carry review blocks and never a "
                    "promotion_id; only generated reports cite one)",
                )
            )

    return findings


def validate_data(
    path: Path, data: dict, validator: Draft202012Validator, workspace_root: Path,
    descriptor: dict | None = None,
) -> list[Finding]:
    """`descriptor` (chainlink #35, optional and backward-compatible)
    recognizes canonical crate-scoped witness specs in
    artifact_manifest and checks them with
    validate_witness.witness_promotion_digest instead of a plain byte
    hash -- see check_artifact_manifest/_witness_promotion_hash_if_applicable.
    Omitting it reproduces this function's original, witness-unaware
    behavior exactly, except that a `_witnesses`-shaped path is then
    refused rather than silently trusted (see that function's own
    docstring)."""
    g1a = gate_g1a(path, data, validator)
    if g1a:
        return g1a

    findings: list[Finding] = []
    findings.extend(check_naming(path, data, workspace_root))
    findings.extend(check_artifact_manifest(path, data, workspace_root, descriptor))
    return findings


def _load_receipt(path: Path) -> dict:
    text = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        return yaml.safe_load(text)
    return json.loads(text)


def validate_file(
    path: Path, validator: Draft202012Validator, workspace_root: Path, descriptor: dict | None = None
) -> list[Finding]:
    try:
        data = _load_receipt(path)
    except (json.JSONDecodeError, yaml.YAMLError) as e:
        return [Finding("G1a", path, f"invalid {path.suffix or 'JSON'}: {e}")]
    return validate_data(path, data, validator, workspace_root, descriptor)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("receipt", type=Path, help="Path to a promotion receipt .json or .yaml file")
    parser.add_argument("--workspace-root", type=Path, default=Path("."))
    parser.add_argument(
        "--descriptor",
        type=Path,
        default=None,
        help="Project descriptor, to recognize canonical crate-scoped witness specs in "
        "artifact_manifest (chainlink #35). Defaults to <workspace-root>/project-descriptor.json "
        "if that file exists; omitted entirely (no witness recognition) if it does not, so a "
        "workspace with no witnesses needs no descriptor.",
    )
    args = parser.parse_args(argv)

    descriptor = None
    descriptor_path = args.descriptor or (args.workspace_root / "project-descriptor.json")
    if args.descriptor is not None or descriptor_path.is_file():
        try:
            descriptor = load_project_descriptor(descriptor_path)
        except (ProjectDescriptorError, OSError, json.JSONDecodeError) as e:
            print(f"error: cannot read project descriptor {descriptor_path}: {e}", file=sys.stderr)
            return 2

    validator = load_validator()
    findings = validate_file(args.receipt, validator, args.workspace_root, descriptor)

    if not findings:
        print("OK: promotion receipt passes G1a and §7.1 checks")
        return 0

    print(f"FAIL: {len(findings)} finding(s)")
    for f in findings:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
