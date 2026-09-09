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
  - Every declared (witness_required: true) feature belonging to the
    receipt's own cluster must have its witness represented in
    artifact_manifest -- check_required_witnesses(), sharing
    required_witness_paths() verbatim with
    generate_promotion_receipt.py's own accept_promotion() (external
    review, high severity: an earlier version enforced this only at
    generation time; a receipt hand-edited afterward to drop a required
    entry produced zero findings on validation, even with a descriptor
    supplied).
  - A generated witness SVG, the contact sheet, or the feature ledger
    (plan.md §16.6: review projections, never promotion inputs) is
    refused outright if listed in artifact_manifest at all --
    is_generated_review_projection() -- rather than silently accepted
    as an ordinary byte-hashed artifact (external review, medium
    severity: an earlier version accepted these, so listing the
    contact sheet made promotion acceptance gate on a picture
    regenerating).

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
from pathlib import PurePosixPath

import yaml
from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gate_g18 import collect_declared_features  # noqa: E402
from gate_g18 import collect_valid_witnesses  # noqa: E402
from gate_g19 import collect_valid_witness_entries  # noqa: E402
from generate_feature_ledger import LEDGER_RELATIVE_PATH  # noqa: E402
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


class RequiredWitnessError(Exception):
    """Raised by required_witness_paths() when the required witness set
    itself cannot be determined: an ambiguous declaration, an
    ambiguously resolved witness, or a declared feature with no
    genuinely valid witness at all. generate_promotion_receipt.py
    catches this and re-raises it as its own PromotionReceiptError, its
    established public contract; check_required_witnesses() below
    catches it directly and turns it into an ordinary Finding, matching
    this module's own "return findings, never raise" idiom. One shared
    exception type rather than two independent ones, so both callers
    fail on the identical condition."""


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


def _witness_svg_dir(workspace_root: Path) -> Path:
    """docs/witnesses/ -- workspace-level (not crate-scoped), the fixed
    location every generated witness rendering (generate_witness.py's
    own output_path_for convention) AND the contact sheet
    (generate_contact_sheet.py's _contact_sheet.svg, chainlink #32)
    live at. There is no other legitimate use of this directory --
    every file directly under it is machine-generated."""
    return (workspace_root / "docs" / "witnesses").resolve()


def _looks_like_witness_svg_or_ledger_path(entry_path: str) -> bool:
    """Classification by the manifest's own STATED path, never
    filesystem-resolved -- a symlink planted AT the canonical
    generated-projection location (docs/witnesses/*.svg, or the exact
    feature-ledger path) but pointing OUTSIDE it must not be able to
    escape refusal just because its resolved target sits elsewhere.
    Purely lexical: `entry_path` is already guaranteed workspace-
    relative with no ".."/absolute segments by the syntactic checks
    that run before this one (in both callers), so a plain
    PurePosixPath split is meaningful and safe here without touching
    the filesystem at all."""
    parts = PurePosixPath(entry_path).parts
    if len(parts) == 3 and parts[0] == "docs" and parts[1] == "witnesses" and parts[2].endswith(".svg"):
        return True
    return parts == LEDGER_RELATIVE_PATH


def is_generated_review_projection(workspace_root: Path, entry_path: str, resolved: Path) -> bool:
    """plan.md §16.6: generated witness SVGs, the contact sheet, and
    the feature ledger (ci/results/feature_ledger.json,
    generate_feature_ledger.LEDGER_RELATIVE_PATH) are review
    projections, never promotion inputs -- they must never be
    recognized as an ordinary artifact_manifest entry, at all. External
    review, medium severity: an earlier version accepted these as
    plain byte-hashed artifacts with nothing stopping a caller from
    listing one, so including the contact sheet in artifact_manifest
    silently made promotion acceptance gate on a picture regenerating,
    contradicting this section's own "review projections, never
    promotion inputs" boundary.

    A second review pass, medium severity: classifying by RESOLVED
    identity alone let a symlink planted at the canonical
    generated-projection location (e.g. a manifest entry literally
    named docs/witnesses/task_queue.load_factor.svg) but pointing
    OUTSIDE it bypass this refusal entirely -- the resolved target's
    own parent directory is not docs/witnesses, so the resolved-only
    check missed it, the artifact was accepted and byte-hashed as
    ordinary, and changing the symlink's target then revoked an
    unrelated promotion. Classification now also checks the
    manifest's own stated path
    (_looks_like_witness_svg_or_ledger_path, purely lexical) --
    either signal alone is sufficient to refuse."""
    if _looks_like_witness_svg_or_ledger_path(entry_path):
        return True
    if resolved.parent == _witness_svg_dir(workspace_root) and resolved.suffix == ".svg":
        return True
    if resolved == (workspace_root / Path(*LEDGER_RELATIVE_PATH)).resolve():
        return True
    return False


def required_witness_paths(descriptor: dict, workspace_root: Path, cluster: str) -> dict[str, Path]:
    """Every declared (witness_required: true) feature belonging to
    `cluster`'s own canonical witness spec, workspace-relative path ->
    resolved path (chainlink #35). Discovery is reused verbatim from
    chainlink #29/#30's own descriptor-backed helpers --
    gate_g18.collect_declared_features/collect_valid_witnesses
    (declared, ambiguity-resolved) and
    gate_g19.collect_valid_witness_entries (path lookup) -- never a new
    glob or a first-wins resolution. Shared verbatim between
    generate_promotion_receipt.py (generation) and this module
    (validation, check_required_witnesses below) -- external review,
    high severity: an earlier version existed only in the generator,
    so a receipt that started complete but was later hand-edited to
    drop a required witness entry produced zero findings on
    validation, even with a descriptor supplied.

    Scoped to `cluster` (external review, medium severity: an earlier
    version was workspace-wide, requiring witnesses belonging to
    unrelated clusters -- a concept spec's top-level `cluster` field is
    required by vendor/concept-to-code's own schema). Only a declared
    feature whose OWNING CONCEPT SPEC's own `cluster` equals `cluster`
    is required; a feature declared in a different cluster imposes no
    requirement on this promotion, and `collect_valid_witnesses`'s own
    ambiguity check is scoped to the cluster-filtered key set for the
    same reason.

    Cluster attribution fails closed, deliberately NOT reusing
    generate_feature_ledger.owning_cluster_for() (a second review pass,
    high severity): that function's own "unknown" fallback for a
    missing `cluster` field is the honest, correct answer for a
    DISPLAY-only reader with nothing to gate on it, but the wrong
    answer here -- an unreadable concept spec, or one missing `cluster`
    entirely, would otherwise silently exclude its own declared feature
    from every cluster's required set (this pipeline never runs
    concept-to-code's own JSON Schema validator against a concept spec,
    so this is a real, reachable state, not a theoretical one).
    RequiredWitnessError is raised outright instead.

    Built by iterating the cluster-filtered DECLARED set, never
    `witnesses` itself -- `collect_valid_witnesses` returns every
    genuinely valid, unambiguous witness in the workspace regardless of
    whether its own query was ever declared `witness_required`, so
    iterating it directly would require witnesses for undeclared
    queries too.

    Raises RequiredWitnessError for an ambiguous declaration, an
    unreadable or cluster-less concept spec, an ambiguously resolved
    witness, or a declared feature (within this cluster) with no
    genuinely valid witness at all -- a promotion cannot represent a
    feature it cannot even locate, and every one of those is exactly
    the "applicable witness spec is missing / invalid / ambiguous"
    fail-closed condition plan.md §16.5 requires."""
    declared, declare_findings = collect_declared_features(descriptor, workspace_root)
    ambiguous_declarations = [f for f in declare_findings if f.severity == "error"]
    if ambiguous_declarations:
        raise RequiredWitnessError(
            "cannot compute the required witness set over an ambiguous declared feature set: "
            + "; ".join(str(f) for f in ambiguous_declarations)
        )

    cluster_declared: dict[tuple[str, str], Path] = {}
    for key, spec_path in declared.items():
        try:
            concept_data = json.loads(spec_path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            # External review, high severity: silently `continue`-ing
            # past an unreadable concept spec here excluded its own
            # declared feature from EVERY cluster's required set --
            # exactly as unsafe as owning_cluster_for()'s own honest
            # "unknown" fallback below, since either one lets a feature
            # quietly stop being required for any promotion at all.
            # Cluster attribution for a witness_required feature must
            # fail closed, not be skipped.
            raise RequiredWitnessError(
                f"declared (witness_required: true) feature {key[0]}.{key[1]}'s own concept spec "
                f"{spec_path} could not be read to determine its owning cluster: {e}"
            )
        declared_cluster = concept_data.get("cluster")
        if not isinstance(declared_cluster, str) or not declared_cluster:
            # owning_cluster_for()'s own "unknown" fallback is the
            # right, honest answer for a DISPLAY-only reader
            # (generate_feature_ledger.py) that has nothing to gate on
            # it -- it is the wrong answer here, where "unknown" would
            # silently exclude the feature from this (and every other)
            # promotion's required witness set. This pipeline does not
            # run concept-to-code's own JSON Schema validator against a
            # concept spec (see docs/concept-to-code-witness-required-schema.json's
            # own note), so a missing `cluster` field is a real,
            # reachable state, not a theoretical one.
            raise RequiredWitnessError(
                f"declared (witness_required: true) feature {key[0]}.{key[1]}'s own concept spec "
                f"{spec_path} does not declare a `cluster` -- cluster attribution cannot be skipped "
                "or default to 'unknown', since either would silently exclude the feature from every "
                "promotion's required witness set"
            )
        if declared_cluster == cluster:
            cluster_declared[key] = spec_path

    witnesses, witness_findings = collect_valid_witnesses(descriptor, workspace_root, set(cluster_declared))
    ambiguous_witnesses = [f for f in witness_findings if f.severity == "error"]
    if ambiguous_witnesses:
        raise RequiredWitnessError(
            "cannot compute the required witness set over an ambiguously resolved witness: "
            + "; ".join(str(f) for f in ambiguous_witnesses)
        )

    missing = sorted(
        f"{concept}.{query}" for concept, query in cluster_declared if (concept, query) not in witnesses
    )
    if missing:
        raise RequiredWitnessError(
            f"declared (witness_required: true) feature(s) in cluster {cluster!r} have no genuinely "
            "valid witness spec, so none can be represented in artifact_manifest: " + ", ".join(missing)
        )

    entries_by_key = {
        (data["concept"], data["query"]): resolved_path
        for resolved_path, data in collect_valid_witness_entries(descriptor, workspace_root)
    }

    workspace_resolved = workspace_root.resolve()
    required: dict[str, Path] = {}
    for key in cluster_declared:
        resolved_path = entries_by_key[key]
        required[str(resolved_path.relative_to(workspace_resolved))] = resolved_path
    return required


def check_required_witnesses(
    path: Path, data: dict, workspace_root: Path, descriptor: dict | None
) -> list[Finding]:
    """chainlink #35, external review, high severity: the completeness
    calculation (which witnesses THIS promotion must include) must be
    shared with the validator, not enforced only during
    accept_promotion() -- required_witness_paths() above is the single
    shared computation. Skipped (not an error) only when no descriptor
    at all is available -- the same optionality every other
    witness-aware check in this module already has (see the module
    docstring); when a descriptor IS supplied, completeness is checked
    for real, closing the exact gap the external review reproduced
    ("even with the descriptor supplied")."""
    if descriptor is None:
        return []
    try:
        required = required_witness_paths(descriptor, workspace_root, data["cluster"])
    except RequiredWitnessError as e:
        return [Finding("7.1", path, f"cannot determine the required witness set: {e}")]

    workspace_resolved = workspace_root.resolve()
    included_resolved: set[Path] = set()
    for entry in data["artifact_manifest"]:
        try:
            included_resolved.add((workspace_root / entry["path"]).resolve())
        except OSError:
            continue

    missing = sorted(rel for rel, resolved_path in required.items() if resolved_path not in included_resolved)
    if not missing:
        return []
    return [
        Finding(
            "7.1", path,
            "declared (witness_required: true) feature(s) have a genuinely valid witness spec that is "
            "not included in this promotion's artifact_manifest: " + ", ".join(missing),
        )
    ]


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

        if is_generated_review_projection(workspace_root, entry_path, resolved):
            findings.append(
                Finding(
                    "7.1", path,
                    f"artifact_manifest lists {entry_path!r}, a generated review projection (a "
                    "witness rendering, the contact sheet, or the feature ledger) -- plan.md §16.6: "
                    "these are review projections, never promotion inputs, and must never be listed "
                    "in artifact_manifest",
                )
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
    findings.extend(check_required_witnesses(path, data, workspace_root, descriptor))
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
