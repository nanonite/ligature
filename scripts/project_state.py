#!/usr/bin/env python3
"""Project-state query and consolidated-check driver (chainlink #56).

This is the real engine behind `ligature status --json` and
`ligature check --json`, computed against the two v1.0 output contracts
chainlink #55 specified but deliberately did not implement:
`schemas/project-state.schema.json` and
`schemas/consolidated-check.schema.json`.

Design rules, taken from the contracts rather than invented here:

* **Read-only.** Neither `project_state.build_project_state()` nor
  `consolidated_check.build_consolidated_check()` writes anything, and
  neither calls `extract-c-static` / `check-bridges` / `render-witness`
  (docs/cli-contract.md §7). A `consolidated-check` document always
  carries `mutated_workspace: false`; the gate functions called here are
  the read-only `gate_workspace` readers, never the writing `check_*`
  entry points.
* **Honest unknown.** Every condition this module cannot determine
  reports `unknown` (or the schema's nearest honest state), never the
  nearest-looking pass value -- matching the schema's own "never omit,
  never collapse" discipline.
* **Witness observations are not assurance.** `generated_observations`
  is populated from witness specs/results only; nothing here ever puts a
  witness into `obligations[].required_assurance`/`achieved_assurance`
  (the schema forbids `dimension == "witness"` structurally).
* **One recommended action, deterministic.** `check` emits exactly zero
  or one `next_action`, chosen by a fixed priority over the gates it ran;
  the stable surface is `action_id`, and `command` is advisory only.

The heavy lifting of finding problems is *not* reimplemented: `check`
calls the same `validate_*.validate()` / `gate_*.gate_workspace()`
functions the flat CLI already exposes, then normalizes their dataclass
findings into `consolidated-check`'s `authority`/`severity` vocabulary.
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adjudicator  # noqa: E402
import resources  # noqa: E402

import exit_codes  # noqa: E402
from project_descriptor import ProjectDescriptorError  # noqa: E402
from project_descriptor import load_project_descriptor  # noqa: E402
from schema_utils import make_validator  # noqa: E402
from schema_utils import make_validator_without_required  # noqa: E402
import write_set  # noqa: E402
from validate_evidence import find_evidence_drafts  # noqa: E402

# Re-exported from the adjudicator, which is the single source of the
# running product's identity (#57). Callers that imported these names from
# project_state keep working.
PRODUCT_NAME = adjudicator.PRODUCT_NAME
PRODUCT_VERSION = adjudicator.PRODUCT_VERSION

ROOT = resources.resource_root()
SCHEMAS = ROOT / "schemas"

# The schema versions this build knows how to emit/consume, used to
# classify an installed manifest as current/incompatible.
KNOWN_SCHEMA_VERSIONS = {
    "project-descriptor": "1.0",
    "project-state": "1.0",
    "consolidated-check": "1.0",
}

# _installed_schema_id maps an artifact-kind to the schema id recorded in
# an installation manifest's installed_schema_versions, so status can tell
# whether the installed contracts match the running product's.
MANIFEST_RELATIVE_PATH = ("ci", "manifest", "installation.json")

# kind -> (schema file, requires_review_to_be_validated, id field)
_ARTIFACT_KINDS: dict[str, tuple[str, bool, str]] = {
    "boundary": ("docs/boundary-contract-schema.json", True, "boundary_id"),
    "interaction": ("docs/interaction-schema.json", True, "interaction_id"),
    "exemption": ("docs/exemption-schema.json", True, "interaction_id"),
    "protocol-debt": ("docs/protocol-debt-schema.json", True, "interaction_id"),
    "bridge": ("docs/bridge-schema.json", True, "bridge_id"),
    "witness": ("docs/witness-spec-schema.json", True, "witness_id"),
    "closure": ("docs/closure-profile-schema.json", True, "cluster"),
    "degradation-record": ("docs/degradation-record-schema.json", True, "cluster"),
    "evidence": ("docs/evidence-schema.json", False, "id"),
    "conflict-resolution": ("docs/conflict-resolution-schema.json", False, "conflict_id"),
    "gold-set": ("docs/gold-set-schema.json", True, "cluster"),
    "callsites": ("docs/callsite-schema.json", False, "report_id"),
    "promotion": ("docs/promotion-receipt-schema.json", False, "promotion_id"),
    "work-package": ("docs/work-package-manifest-schema.json", False, "work_package"),
}

# artifact-kind -> project-state.schema.json's `kind` vocabulary. They
# coincide except for degradation records, which the schema folds under
# "closure" (a degradation record is a closure artifact).
_ARTIFACT_KIND_OUT = {
    "degradation-record": "closure",
}

# (subdir under a crate's specs/, artifact-kind)
_CRATE_ARTIFACT_DIRS = (
    ("_boundaries", "boundary"),
    ("_interactions", "interaction"),
    ("_exemptions", "exemption"),
    ("_protocol_debt", "protocol-debt"),
    ("_bridges", "bridge"),
    ("_witnesses", "witness"),
)

_SEVERITY_MAP = {
    "critical": "critical",
    "error": "high",
    "high": "high",
    "medium": "medium",
    "decision": "medium",
    "warn": "medium",
    "degraded": "medium",
    "low": "low",
    "info": "info",
}


class StateError(Exception):
    """Raised when the requested document cannot be produced at all (an
    invalid/absent workspace or descriptor). Maps to exit code 2 per
    docs/exit-code-contract.md when surfaced through `check`."""


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _rel(path: Path, workspace: Path) -> str:
    try:
        return path.resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


def _read_yaml(path: Path):
    try:
        import yaml  # type: ignore
    except ImportError:
        return None
    try:
        return yaml.safe_load(path.read_text())
    except Exception:
        return None


def _load_data(path: Path):
    if path.suffix in (".yaml", ".yml"):
        return _read_yaml(path)
    return _read_json(path)


_validator_cache: dict[tuple[str, bool], object] = {}


def _validator_for(schema_file: str, review_required: bool):
    """For a review-required artifact kind, validate against a copy of the
    schema with `review` optional: a boundary/interaction/etc. that is
    otherwise well-formed but not yet approved is a legitimate DRAFT
    (Stage 0/3), not a schema-invalid artifact. Schema validity and
    review presence are then two separate facts, which is exactly the
    distinction the lifecycle enum needs.

    The recursive strip (schema_utils.make_validator_without_required) is
    required, not cosmetic: a resolved conflict-resolution record has its
    own nested `if/then` requiring `review`, so stripping only the
    top-level `required` array would still reject a review-less resolved
    draft."""
    key = (schema_file, review_required)
    if key not in _validator_cache:
        schema = json.loads((ROOT / schema_file).read_text())
        if review_required:
            _validator_cache[key] = make_validator_without_required(schema, "review")
        else:
            _validator_cache[key] = make_validator(schema)
    return _validator_cache[key]


def _schema_ok(kind: str, path: Path, data) -> bool | None:
    """True/False when the kind's schema could be applied, None when the
    data could not even be parsed (a distinct, still-failing state)."""
    if data is None:
        return None
    schema_file, review_required, _ = _ARTIFACT_KINDS[kind]
    validator = _validator_for(schema_file, review_required)
    return not list(validator.iter_errors(data))  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Promotion receipts
# ---------------------------------------------------------------------------
def _promotion_manifest(workspace: Path) -> dict[str, dict]:
    """workspace-relative path -> {"hash", "cluster"} for every artifact
    listed in any specs/_promotions receipt. Receipts are the only way to
    know an artifact was promoted (an artifact never points at its own
    receipt, by design)."""
    manifest: dict[str, dict] = {}
    promotions = workspace / "specs" / "_promotions"
    if not promotions.is_dir():
        return manifest
    for path in sorted(promotions.iterdir()):
        if path.suffix not in (".json", ".yaml", ".yml") or not path.is_file():
            continue
        data = _load_data(path)
        if not isinstance(data, dict):
            continue
        cluster = data.get("cluster", path.stem)
        for entry in data.get("artifact_manifest", []) or []:
            if not isinstance(entry, dict):
                continue
            rel = entry.get("path")
            if isinstance(rel, str) and isinstance(entry.get("hash"), str):
                manifest[rel] = {"hash": entry["hash"], "cluster": cluster}
    return manifest


# ---------------------------------------------------------------------------
# Installation manifest / gate integrity
# ---------------------------------------------------------------------------
def _installation_manifest(workspace: Path) -> dict:
    path = workspace.joinpath(*MANIFEST_RELATIVE_PATH)
    if not path.is_file():
        return {
            "state": "not-initialized",
            "installed_product_version": None,
            "installed_schema_versions": {},
        }
    data = _read_json(path)
    if not isinstance(data, dict):
        return {
            "state": "unknown",
            "installed_product_version": None,
            "installed_schema_versions": {},
        }
    installed_product = data.get("installed_product_version") or data.get("product_version")
    schema_versions = data.get("installed_schema_versions") or data.get("schema_versions") or {}
    if not isinstance(schema_versions, dict):
        schema_versions = {}
    if installed_product != PRODUCT_VERSION:
        state = "upgradable"
    else:
        incompatible = any(
            name in KNOWN_SCHEMA_VERSIONS and version != KNOWN_SCHEMA_VERSIONS[name]
            for name, version in schema_versions.items()
        )
        state = "incompatible" if incompatible else "current"
    return {
        "state": state,
        "installed_product_version": installed_product if isinstance(installed_product, str) else None,
        "installed_schema_versions": {str(k): str(v) for k, v in schema_versions.items()},
    }


def _installation_gate_hashes(workspace: Path) -> dict:
    """The manifest's recorded gate hashes, read separately from the
    normalized installation_manifest document -- the frozen v1.0
    project-state schema has additionalProperties:false on that object, so
    the extra bookkeeping must not leak into the emitted document."""
    data = _read_json(workspace.joinpath(*MANIFEST_RELATIVE_PATH))
    if isinstance(data, dict) and isinstance(data.get("gate_hashes"), dict):
        return data["gate_hashes"]
    return {}


def _gate_integrity_evaluate(
    workspace: Path,
    descriptor: dict | None,
    installation: dict,
    descriptor_state: str = "absent",
    descriptor_diagnostics: list[str] | None = None,
) -> tuple[str, str, list[str]]:
    """(state, details, problems) for the descriptor's `gate_integrity`
    pins. `problems` is the per-path list (`"<path>: <reason>"` per drifted
    or missing path) that `check`'s installation/gate-integrity finding
    names; it is empty for the verdicts that have no individual path to
    name (`unknown`, `unpinned`)."""
    if descriptor is None:
        if descriptor_state == "present-invalid":
            # The descriptor file exists but failed schema validation; say
            # so, naming the offending property, instead of the old "no
            # project descriptor present" that made a descriptor typo
            # indistinguishable from a missing descriptor (chainlink #73).
            detail = "; ".join(descriptor_diagnostics or []) or "schema-invalid"
            return (
                "unknown",
                f"project descriptor is present but invalid: {detail}",
                [],
            )
        return "unknown", "no project descriptor present", []
    entries = descriptor.get("gate_integrity") or []
    paths = [e.get("path") for e in entries if isinstance(e, dict) and e.get("path")]
    if not paths:
        return "unknown", "descriptor declares no gate_integrity paths", []
    recorded = _installation_gate_hashes(workspace)
    if installation.get("state") == "not-initialized" or not recorded:
        return (
            "unpinned",
            f"{len(paths)} gate path(s) declared; no installation manifest records their hashes",
            [],
        )
    drifted = []
    for rel in paths:
        if rel == adjudicator.ADJUDICATOR_PIN_TOKEN:
            recorded_adj = recorded.get(rel)
            running_hash = adjudicator.current_identity().get("content_hash")
            if recorded_adj is None:
                drifted.append(f"{rel}: no recorded identity")
            elif recorded_adj == adjudicator.UNATTESTED:
                if running_hash is not None:
                    drifted.append(
                        f"{rel}: workspace was initialized by an unattested source checkout, "
                        "but the running executable is a packaged build"
                    )
            elif running_hash is None:
                drifted.append(
                    f"{rel}: workspace pins a packaged executable, but this is an unattested source checkout"
                )
            elif running_hash != recorded_adj:
                drifted.append(
                    f"{rel}: content hash drift (pinned {recorded_adj}, running {running_hash})"
                )
            continue
        target = workspace / rel
        # chainlink #77 (companion evidence on #74): a gate_integrity path
        # that resolves outside the workspace -- absolute, or a ../
        # traversal -- would be checked against someone else's file, which
        # is just as dangerous as not checking at all (the same discipline
        # validate_work_package.check_gate_integrity already applies to
        # work-package manifests). Refuse the path rather than hashing
        # whatever it points at.
        try:
            target.resolve().relative_to(workspace)
        except ValueError:
            drifted.append(f"{rel}: gate_integrity path escapes the workspace")
            continue
        if rel not in recorded:
            drifted.append(f"{rel}: no recorded hash")
        elif not target.is_file():
            drifted.append(f"{rel}: missing")
        elif recorded[rel] != _sha256_file(target):
            drifted.append(f"{rel}: hash drift")
    if drifted:
        return "drifted", "; ".join(drifted), drifted
    return "pinned", f"{len(paths)} gate path(s) match the installed manifest", []


def _gate_integrity(
    workspace: Path,
    descriptor: dict | None,
    installation: dict,
    descriptor_state: str = "absent",
    descriptor_diagnostics: list[str] | None = None,
) -> dict:
    state, details, _problems = _gate_integrity_evaluate(
        workspace, descriptor, installation, descriptor_state, descriptor_diagnostics
    )
    return {"state": state, "details": details}


def _gate_integrity_findings(gate_integrity: dict, problems: list[str]) -> list[NormalizedFinding]:
    """check's installation/gate-integrity gate (chainlink #75): one finding
    per drifted or missing gate path, naming that path, or a single
    workspace-level finding when the verdict is `unknown`/`unpinned` and no
    individual path can be named. High severity: a workspace whose gate
    definitions are tampered with, missing, or unverifiable must never
    read as a clean bill of health."""
    if problems:
        return [
            NormalizedFinding(
                gate_id="gate-integrity",
                severity="high",
                subject=problem.split(": ", 1)[0],
                reason=problem,
                authority="mechanized-gate",
                provenance="scripts/project_state.py:_gate_integrity_findings",
            )
            for problem in problems
        ]
    return [
        NormalizedFinding(
            gate_id="gate-integrity",
            severity="high",
            subject="(workspace)",
            reason=gate_integrity["details"],
            authority="mechanized-gate",
            provenance="scripts/project_state.py:_gate_integrity_findings",
        )
    ]


def _normative_drift_records(workspace: Path) -> list[dict]:
    """The drift records behind check's policy-drift gate (chainlink #78),
    computed by ligature_install (which owns the registry of normative
    user-owned documents and the manifest's recorded hashes). Imported
    lazily to avoid the import cycle (`ligature_install` imports this module
    at load time) -- the same pattern `_descriptor_placeholder_findings`
    already uses. Any failure to compute yields no records rather than an
    exception, so a corrupt manifest degrades to honest-unknown instead of
    crashing the check run."""
    try:
        from ligature_install import normative_drift_records
    except ImportError:
        return []
    try:
        return normative_drift_records(workspace)
    except Exception:
        return []


def _policy_drift_findings(records: list[dict]) -> list[NormalizedFinding]:
    """check's policy-drift gate (chainlink #78): one high-severity finding
    per normative user-owned document (the reliance policy) that is missing
    from the workspace or whose on-disk content differs from the reviewed
    hash the ownership manifest records. High severity, blocking: the
    reliance policy is the standing governance document Stage 3 boundary
    drafting applies its resolution rule from, so an unreviewed edit that
    inverts the rule it exists to enforce must never read as a clean bill
    of health. The finding names the accept path, so the fix is one
    command away."""
    findings: list[NormalizedFinding] = []
    for record in records:
        if record["state"] == "missing":
            reason = (
                f"normative user-owned document {record['path']} is missing from the workspace; "
                "restore it (or accept its removal deliberately) -- `ligature accept-policy "
                "--reviewer <name>` records a reviewed policy document"
            )
        else:
            reason = (
                f"normative user-owned document {record['path']} has drifted from its reviewed "
                f"content (recorded {record['recorded_hash']}, current {record['current_hash']}); "
                "review the change and record it with `ligature accept-policy --reviewer <name>` "
                "(chainlink #78)"
            )
        findings.append(
            NormalizedFinding(
                gate_id="policy-drift",
                severity="high",
                subject=record["path"],
                reason=reason,
                authority="mechanized-gate",
                provenance="scripts/ligature_install.py:normative_drift_records",
            )
        )
    return findings


def _descriptor_policy_path_findings(descriptor: dict | None, workspace: Path) -> list[NormalizedFinding]:
    """chainlink #78: the descriptor's
    `compatibility_policy.reliance_policy_path` was read by nothing -- a
    pointer that named a nonexistent file, or a path outside the project
    root, validated as `present-valid` and left `check` exit 0. The field
    is the project's own declaration of where its policy lives, so the
    validation is semantic (existence + containment), not schema: the
    schema stays a stable public contract, and `check` remains the detail
    surface. High severity: a workspace whose declared policy document is
    missing or unreachable cannot drift-check or promote against it."""
    if descriptor is None:
        return []
    compat = descriptor.get("compatibility_policy")
    if not isinstance(compat, dict):
        return []
    rel = compat.get("reliance_policy_path")
    if not isinstance(rel, str) or not rel:
        return []
    try:
        target = workspace / rel
        target.resolve().relative_to(workspace)
    except (ValueError, OSError):
        return [
            NormalizedFinding(
                gate_id="P0",
                severity="high",
                subject=str(rel),
                reason=(
                    f"compatibility_policy.reliance_policy_path {rel!r} resolves outside the "
                    "project root; fix the declaration to name the policy document inside the "
                    "workspace (chainlink #78)"
                ),
                authority="mechanized-gate",
                provenance="scripts/project_state.py:_descriptor_policy_path_findings",
            )
        ]
    if not target.is_file():
        return [
            NormalizedFinding(
                gate_id="P0",
                severity="high",
                subject=str(rel),
                reason=(
                    f"compatibility_policy.reliance_policy_path {rel!r} names a file that does "
                    "not exist in the workspace; restore the policy document or fix the "
                    "declaration (chainlink #78)"
                ),
                authority="mechanized-gate",
                provenance="scripts/project_state.py:_descriptor_policy_path_findings",
            )
        ]
    return []


# ---------------------------------------------------------------------------
# Artifact discovery + lifecycle
# ---------------------------------------------------------------------------
def _looks_like_work_package_manifest(data) -> bool:
    """Whether parsed ci/manifest/ content is a work-package manifest
    rather than one of the other, unrelated files that share the
    directory -- in particular the ownership manifest `ligature init`
    installs at ci/manifest/installation.json (chainlink #66, #72).

    Reuses gate_g14's discriminator so `status` and the closure gate
    cannot drift apart on what a work-package manifest is: a work-package
    manifest always carries at least one of the `work_package` or
    `definition_of_done` top-level keys, and the ownership manifest
    carries neither."""
    import gate_g14

    return gate_g14.looks_like_work_package_manifest(data)


def _discover_artifact_files(workspace: Path, descriptor: dict | None) -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []

    def _walk(directory: Path, kind: str):
        if not directory.is_dir():
            return
        for path in sorted(directory.iterdir()):
            if not path.is_file():
                continue
            if path.suffix not in (".json", ".yaml", ".yml"):
                continue
            found.append((kind, path))

    if descriptor is not None:
        for crate in descriptor.get("crates", []):
            crate_dir = crate.get("crate_dir")
            if not crate_dir:
                continue
            specs = workspace / crate_dir / "specs"
            for dirname, kind in _CRATE_ARTIFACT_DIRS:
                _walk(specs / dirname, kind)

    closure = workspace / "specs" / "_closure"
    if closure.is_dir():
        for path in sorted(closure.iterdir()):
            if not path.is_file() or path.suffix not in (".json", ".yaml", ".yml"):
                continue
            kind = "degradation-record" if path.name.endswith(".degradation.json") else "closure"
            found.append((kind, path))

    _walk(workspace / "evidence", "evidence")
    _walk(workspace / "specs" / "_conflicts", "conflict-resolution")
    _walk(workspace / "specs" / "_gold_sets", "gold-set")
    _walk(workspace / "specs" / "_promotions", "promotion")
    _walk(workspace / "ci" / "results" / "c_static", "callsites")
    # ci/manifest/ holds work-package manifests AND the ownership manifest
    # `ligature init` installs at ci/manifest/installation.json; only the
    # former are work-package artifacts. A file that parses but is
    # recognizably not a work-package manifest is skipped, while one that
    # cannot be parsed at all stays in the discovery set so status can
    # report it honestly as an invalid work-package (chainlink #66/#72).
    manifest_dir = workspace / "ci" / "manifest"
    if manifest_dir.is_dir():
        for path in sorted(manifest_dir.iterdir()):
            if not path.is_file() or path.suffix not in (".json", ".yaml", ".yml"):
                continue
            data = _load_data(path)
            if data is not None and not _looks_like_work_package_manifest(data):
                continue
            found.append(("work-package", path))
    return found


def _current_promotion_hash(kind: str, path: Path, data) -> str:
    """The hash a receipt would record for this artifact. Ordinary
    artifacts hash their bytes; a witness spec hashes its canonical
    promotion digest (render_hash excluded), exactly like
    generate_promotion_receipt does."""
    if kind == "witness" and isinstance(data, dict):
        try:
            from validate_witness import witness_promotion_digest

            return witness_promotion_digest(data)
        except Exception:
            pass
    return _sha256_file(path)


def _artifact_record(kind: str, path: Path, workspace: Path, promotions: dict) -> dict:
    data = _load_data(path)
    schema_ok = _schema_ok(kind, path, data)
    review_required = _ARTIFACT_KINDS[kind][1]
    rel = _rel(path, workspace)
    receipt = promotions.get(rel)
    promoted_hash = receipt["hash"] if receipt else None
    current_hash = _current_promotion_hash(kind, path, data)

    if receipt is not None:
        lifecycle = "promoted" if current_hash == promoted_hash else "stale-by-hash-drift"
    elif schema_ok is None or schema_ok is False:
        lifecycle = "invalid"
    elif review_required and not (isinstance(data, dict) and "review" in data):
        lifecycle = "draft"
    else:
        lifecycle = "validated"

    artifact_id_field = _ARTIFACT_KINDS[kind][2]
    artifact_id = None
    if isinstance(data, dict) and isinstance(data.get(artifact_id_field), str):
        artifact_id = data[artifact_id_field]
    if not artifact_id:
        artifact_id = path.stem

    record = {
        "artifact_id": artifact_id,
        "kind": _ARTIFACT_KIND_OUT.get(kind, kind),
        "path": rel,
        "content_hash": current_hash if lifecycle != "absent" else None,
        "lifecycle": lifecycle,
    }
    if lifecycle in ("promoted", "stale-by-hash-drift"):
        record["promoted_hash"] = promoted_hash
    return record


def _absent_artifact_record(rel: str) -> dict:
    return {
        "artifact_id": Path(rel).stem,
        "kind": _kind_from_path(rel),
        "path": rel,
        "content_hash": None,
        "lifecycle": "absent",
    }


def _kind_from_path(rel: str) -> str:
    parts = Path(rel).parts
    for part in parts:
        if part == "_boundaries":
            return "boundary"
        if part == "_interactions":
            return "interaction"
        if part == "_exemptions":
            return "exemption"
        if part == "_protocol_debt":
            return "protocol-debt"
        if part == "_bridges":
            return "bridge"
        if part == "_witnesses":
            return "witness"
        if part == "_closure":
            return "closure"
        if part == "_conflicts":
            return "conflict-resolution"
        if part == "_gold_sets":
            return "gold-set"
        if part == "_promotions":
            return "promotion"
        if part == "evidence":
            return "evidence"
    if "c_static" in parts:
        return "callsites"
    if "manifest" in parts:
        return "work-package"
    return "boundary"


def _artifacts(workspace: Path, descriptor: dict | None, promotions: dict) -> list[dict]:
    seen_paths: set[str] = set()
    records: list[dict] = []
    for kind, path in _discover_artifact_files(workspace, descriptor):
        rel = _rel(path, workspace)
        seen_paths.add(rel)
        records.append(_artifact_record(kind, path, workspace, promotions))
    # A receipt path that is no longer on disk is a recognized artifact
    # in lifecycle 'absent' -- the only way an absent artifact can be known.
    for rel in sorted(promotions):
        if rel in seen_paths:
            continue
        if not any(marker in Path(rel).parts for marker in (
            "_boundaries", "_interactions", "_exemptions", "_protocol_debt",
            "_bridges", "_witnesses", "_closure", "evidence",
        )):
            continue
        records.append(_absent_artifact_record(rel))
    records.sort(key=lambda r: (r["kind"], r["artifact_id"], r["path"]))
    return records


# ---------------------------------------------------------------------------
# Obligations (required from interaction reliances, achieved from reports)
# ---------------------------------------------------------------------------
def _obligations(workspace: Path, descriptor: dict | None) -> list[dict]:
    required: dict[str, set[str]] = {}
    achieved: dict[str, dict[str, str]] = {}

    if descriptor is not None:
        for crate in descriptor.get("crates", []):
            crate_dir = crate.get("crate_dir")
            if not crate_dir:
                continue
            interactions = workspace / crate_dir / "specs" / "_interactions"
            if not interactions.is_dir():
                continue
            for path in sorted(interactions.glob("*.json")):
                data = _read_json(path)
                if not isinstance(data, dict):
                    continue
                for reliance in data.get("reliances", []) or []:
                    if not isinstance(reliance, dict):
                        continue
                    obligation_id = reliance.get("obligation_id")
                    assurance = reliance.get("required_assurance")
                    if not isinstance(obligation_id, str) or not isinstance(assurance, dict):
                        continue
                    dims = assurance.get("accepted_evidence_kinds") or []
                    bucket = required.setdefault(obligation_id, set())
                    for dim in dims:
                        if isinstance(dim, str):
                            bucket.add(dim)

    # Achieved assurance records live in a work package's report.emit file.
    manifests_dir = workspace / "ci" / "manifest"
    report_paths: list[Path] = []
    if manifests_dir.is_dir():
        for manifest_path in sorted(manifests_dir.iterdir()):
            if manifest_path.suffix not in (".json", ".yaml", ".yml"):
                continue
            manifest = _load_data(manifest_path)
            if not isinstance(manifest, dict):
                continue
            if not _looks_like_work_package_manifest(manifest):
                continue
            emit = (manifest.get("report") or {}).get("emit")
            if isinstance(emit, str):
                report_paths.append(workspace / emit)
    for report_path in report_paths:
        report = _read_json(report_path)
        if not isinstance(report, dict):
            continue
        for entry in report.get("obligation_records", []) or []:
            if not isinstance(entry, dict):
                continue
            obligation_id = entry.get("obligation_id")
            record = entry.get("record")
            if not isinstance(obligation_id, str) or not isinstance(record, dict):
                continue
            evidence = record.get("evidence") or {}
            support = record.get("support") or {}
            dim = evidence.get("kind")
            if not isinstance(dim, str):
                continue
            status = "achieved" if support.get("status") == "supported" else "missing"
            achieved.setdefault(obligation_id, {})[dim] = status

    obligations: list[dict] = []
    for obligation_id in sorted(set(required) | set(achieved)):
        req_dims = sorted(required.get(obligation_id, set()))
        ach = achieved.get(obligation_id, {})
        required_assurance = []
        achieved_assurance = []
        for dim in req_dims:
            status = ach.get(dim, "missing")
            required_assurance.append({"dimension": dim, "status": status})
            if status == "achieved":
                achieved_assurance.append({"dimension": dim, "status": "achieved"})
        # achieved dimensions with no declared requirement are still real
        for dim, status in sorted(ach.items()):
            if dim not in req_dims:
                achieved_assurance.append({"dimension": dim, "status": status})
        obligations.append(
            {
                "obligation_id": obligation_id,
                "required_assurance": required_assurance,
                "achieved_assurance": achieved_assurance,
            }
        )
    return obligations


# ---------------------------------------------------------------------------
# Clusters
# ---------------------------------------------------------------------------
def _closure_profiles(workspace: Path) -> list[Path]:
    closure = workspace / "specs" / "_closure"
    if not closure.is_dir():
        return []
    return [
        p
        for p in sorted(closure.iterdir())
        if p.is_file()
        and p.suffix in (".json", ".yaml", ".yml")
        and not p.name.endswith(".degradation.json")
    ]


def _clusters(workspace: Path, descriptor: dict | None) -> list[dict]:
    profiles = _closure_profiles(workspace)
    if not profiles:
        return []
    outcomes: dict[str, object] = {}
    if descriptor is not None:
        try:
            import gate_g14

            cluster_outcomes, _ = gate_g14.gate_workspace(workspace, descriptor)
            outcomes = {o.cluster: o for o in cluster_outcomes}
        except Exception:
            outcomes = {}
    result = []
    for path in profiles:
        data = _read_json(path)
        cluster = None
        closure_kind = "unknown"
        if isinstance(data, dict):
            cluster = data.get("cluster")
            closure_kind = data.get("closure_kind", "unknown")
        if not isinstance(cluster, str):
            cluster = path.stem
        degradation_path = path.parent / f"{cluster}.degradation.json"
        degradation = _read_json(degradation_path) if degradation_path.is_file() else None
        outcome = outcomes.get(cluster)
        if outcome is not None:
            state = getattr(outcome, "status", "unknown")
        elif degradation is not None:
            state = "degraded"
        else:
            state = "unknown"
        limitations: list[str] = []
        if isinstance(degradation, dict):
            limitations = [str(c) for c in degradation.get("failed_conditions", []) or []]
        if not limitations and outcome is not None:
            for finding in getattr(outcome, "findings", []) or []:
                condition = getattr(finding, "condition", None)
                if condition:
                    limitations.append(str(condition))
        result.append(
            {
                "cluster": cluster,
                "closure_kind": closure_kind,
                "state": state,
                "limitations": limitations,
            }
        )
    result.sort(key=lambda c: c["cluster"])
    return result


# ---------------------------------------------------------------------------
# Generated (observation-only) data
# ---------------------------------------------------------------------------
def _observations(workspace: Path, descriptor: dict | None) -> dict:
    summaries = []
    if descriptor is not None:
        for crate in descriptor.get("crates", []):
            crate_dir = crate.get("crate_dir")
            if not crate_dir:
                continue
            witnesses = workspace / crate_dir / "specs" / "_witnesses"
            if not witnesses.is_dir():
                continue
            for path in sorted(witnesses.glob("*.json")):
                data = _read_json(path)
                if not isinstance(data, dict):
                    continue
                wid = data.get("witness_id", path.stem)
                declared = (data.get("determinism") or {}).get("class") if isinstance(
                    data.get("determinism"), dict
                ) else data.get("determinism")
                determinism = declared if declared in ("deterministic", "nondeterministic") else "unknown"
                summaries.append(
                    {"witness_id": str(wid), "determinism": determinism, "degeneracy": "unknown"}
                )
    summaries.sort(key=lambda s: s["witness_id"])
    generated_at = None
    ledger = _read_json(workspace / "ci" / "results" / "feature_ledger.json")
    if isinstance(ledger, dict):
        value = ledger.get("generated_at")
        if isinstance(value, str):
            generated_at = value
    return {"witness_summaries": summaries, "feature_ledger_generated_at": generated_at}


# ---------------------------------------------------------------------------
# Findings / gate runs
# ---------------------------------------------------------------------------
@dataclass
class NormalizedFinding:
    gate_id: str
    severity: str
    subject: str
    reason: str
    authority: str
    provenance: str
    condition: str | None = None

    def as_dict(self) -> dict:
        reason = self.reason
        if self.condition:
            reason = f"[{self.condition}] {reason}"
        dedup = _sha256_text(f"{self.gate_id}|{self.subject}|{reason}")[7:39]
        return {
            "dedup_key": dedup,
            "gate_id": self.gate_id,
            "severity": self.severity,
            "subject": self.subject or "(workspace)",
            "reason": reason,
            "authority": self.authority,
            "provenance": self.provenance,
        }


def _normalize_finding(finding, gate_id: str, provenance: str, authority: str = "mechanized-gate") -> NormalizedFinding:
    subject = getattr(finding, "subject", None) or getattr(finding, "path", "")
    severity = _SEVERITY_MAP.get(str(getattr(finding, "severity", "error")), "medium")
    reason = str(getattr(finding, "reason", ""))
    raw_severity = str(getattr(finding, "severity", ""))
    if severity == "medium" and raw_severity == "decision":
        authority = "human-decision-pending"
    # An otherwise well-formed artifact that merely lacks a top-level
    # `review` block is a legitimate Stage 0/3 draft, not a mechanized
    # failure -- the strict G1a validator sees it as a required-property
    # error, but the honest framing is "a human checkpoint is pending"
    # (approve it or discard it), which is exit code 3, not a blocking
    # finding. Match the exact jsonschema message, not a substring: a
    # `review` block that is present but malformed (e.g. a missing
    # `reviewer`) must stay a real blocking schema error.
    if reason == "'review' is a required property":
        authority = "human-decision-pending"
        severity = "medium"
    return NormalizedFinding(
        gate_id=gate_id,
        severity=severity,
        subject=str(subject),
        reason=str(getattr(finding, "reason", "")),
        authority=authority,
        provenance=provenance,
        condition=getattr(finding, "condition", None),
    )


@dataclass
class Analysis:
    workspace: Path
    descriptor: dict | None
    descriptor_state: str
    descriptor_error: str | None
    artifacts: list[dict]
    obligations: list[dict]
    clusters: list[dict]
    findings: list[NormalizedFinding]
    gate_runs: list[dict]
    installation: dict
    gate_integrity: dict
    write_set: dict
    observations: dict
    conditions: set[str] = field(default_factory=set)
    descriptor_diagnostics: list[str] = field(default_factory=list)
    pending_evidence_drafts: list[Path] = field(default_factory=list)

    @property
    def descriptor_rel(self) -> str:
        return "project-descriptor.json"


_VALIDATE_MODULES = (
    ("validate_boundary_contracts", "G1a/G1b/G2+ boundary contracts"),
    ("validate_interaction", "G1a/G1b + eligibility interactions"),
    ("validate_exemption", "G1a/G1b exemptions"),
    ("validate_protocol_debt", "G1a/G1b protocol debt"),
    ("validate_bridge", "G1a/G1b/G2 bridges"),
    ("validate_evidence", "G1a/G1b evidence"),
    ("validate_conflict_resolution", "G1a/G1b/G11 conflicts"),
    ("validate_callsites", "G1a/G1b C_static reports"),
    ("validate_witness", "G1a/G1b/G2 witnesses"),
    ("validate_gold_set", "G1a/G1b gold sets"),
    ("validate_closure", "G1a/G1b/G17 closure"),
)


def _validate_boundaries_with_descriptor(
    workspace: Path, descriptor: dict | None
) -> list[NormalizedFinding]:
    """Run validate_boundary_contracts with the descriptor's own
    crates[].specs_search_root (chainlink #81).

    Mirrors cmd_validate's own per-crate iteration: each crate's boundaries
    are scanned under that crate's crate_dir and its callee_guarantees ids
    are resolved against that crate's specs_search_root -- the same
    per-crate resolution cmd_validate_interaction and cmd_validate_bridge
    already apply. Without a descriptor (absent/invalid/unreadable) the scan
    falls back to the unanchored workspace-wide walk with no search root,
    so the applies_to check degrades to its info-severity "unverifiable"
    finding exactly as before (a missing descriptor is not a boundary-
    contract defect, and every other command already reports it)."""
    import validate_boundary_contracts

    if descriptor is None:
        try:
            raw = validate_boundary_contracts.validate(workspace)
        except FileNotFoundError:
            return []
        return [
            _normalize_finding(
                f,
                str(getattr(f, "gate", "") or "G1a/G1b/G2+"),
                "scripts/validate_boundary_contracts.py:validate",
            )
            for f in raw
        ]

    findings: list[NormalizedFinding] = []
    for crate in descriptor.get("crates", []):
        crate_dir = crate.get("crate_dir")
        specs_root = crate.get("specs_search_root")
        if not crate_dir or not specs_root:
            continue
        try:
            raw = validate_boundary_contracts.validate(workspace / crate_dir, workspace / specs_root)
        except FileNotFoundError:
            continue
        findings.extend(
            _normalize_finding(
                f,
                str(getattr(f, "gate", "") or "G1a/G1b/G2+"),
                "scripts/validate_boundary_contracts.py:validate",
            )
            for f in raw
        )
    return findings


def _run_standalone_validators(workspace: Path, descriptor: dict | None = None) -> list[NormalizedFinding]:
    findings: list[NormalizedFinding] = []
    for module_name, _ in _VALIDATE_MODULES:
        if module_name == "validate_boundary_contracts":
            # chainlink #81: G2+'s applies_to check needs the descriptor's
            # crates[].specs_search_root; pass it per-crate, the same way
            # cmd_validate/cmd_validate_interaction/cmd_validate_bridge do.
            findings.extend(_validate_boundaries_with_descriptor(workspace, descriptor))
            continue
        try:
            module = __import__(module_name)
        except Exception:
            continue
        func = getattr(module, "validate", None)
        if func is None:
            continue
        try:
            raw = func(workspace)
        except FileNotFoundError:
            continue
        except Exception:
            continue
        for finding in raw:
            gate_id = str(getattr(finding, "gate", "") or module_name.replace("validate_", "").upper())
            findings.append(
                _normalize_finding(finding, gate_id, f"scripts/{module_name}.py:validate")
            )
    # chainlink #59's G21 gate is workspace-level, not per-artifact, so it is
    # not one of the module `validate()` entry points above; surface it here
    # too so `check` (the loop driver) reports missing/ambiguous/
    # hash-disagreeing/dangling assumption-registry references.
    try:
        import assumption_registry

        for finding in assumption_registry.check_assumption_registry(workspace):
            findings.append(
                _normalize_finding(
                    finding, finding.gate, "scripts/assumption_registry.py:check_assumption_registry"
                )
            )
    except Exception:
        pass
    return findings


def _descriptor_placeholder_findings(
    descriptor: dict | None, descriptor_state: str, descriptor_path: Path
) -> list[NormalizedFinding]:
    """A descriptor that is still the Stage P0 template `init` wrote is a
    real, mechanized fact with a human-only resolution, so it is reported
    as a finding rather than folded into the installation report's
    managed-file vocabulary (which classifies product-owned files, not a
    user-owned file nobody has edited yet). `ligature_install` owns the
    example fixtures and `_render_descriptor`, so the comparison lives
    there and is imported lazily to avoid the import cycle (`ligature_install`
    imports this module at load time) -- chainlink #67."""
    if descriptor is None or descriptor_state != "present-valid":
        return []
    try:
        from ligature_install import descriptor_placeholder_fields
    except ImportError:
        return []
    fields = descriptor_placeholder_fields(descriptor)
    if not fields:
        return []
    return [
        NormalizedFinding(
            gate_id="P0",
            severity="high",
            subject=str(descriptor_path),
            reason=(
                "project descriptor is still the init template: "
                + ", ".join(fields)
                + " still hold the shipped example-fixture value(s); replace them with real "
                "project values before running any stage (chainlink #67)"
            ),
            authority="human-decision-pending",
            provenance="scripts/ligature_install.py:descriptor_placeholder_fields",
        )
    ]


def _descriptor_invalid_findings(
    descriptor_state: str, descriptor_diagnostics: list[str], descriptor_path: Path
) -> list[NormalizedFinding]:
    """A descriptor that fails schema validation is a user-fixable input
    error, so `check` names the offending property -- JSON path, what was
    wrong, and the permitted alternatives -- instead of reporting an
    empty findings list next to `conditions: [invalid_input]`, and
    `status` carries it as an open finding (chainlink #73).

    The finding is mechanized (the schema validator is a deterministic,
    recomputed fact) even though only a human can fix it; the matching
    next_action is the human-decision kind, since no CLI command can
    repair a descriptor."""
    if descriptor_state != "present-invalid":
        return []
    diagnostics = [line for line in descriptor_diagnostics if line]
    if not diagnostics:
        return []
    return [
        NormalizedFinding(
            gate_id="P0",
            severity="high",
            subject=str(descriptor_path),
            reason=(
                "project descriptor is invalid: "
                + "; ".join(diagnostics)
                + " (chainlink #73)"
            ),
            authority="mechanized-gate",
            provenance="scripts/project_descriptor.py:schema_diagnostics",
        )
    ]


def _has_c_static(workspace: Path) -> bool:
    directory = workspace / "ci" / "results" / "c_static"
    return directory.is_dir() and any(directory.glob("*.json"))


def _has_bridges(workspace: Path, descriptor: dict | None) -> bool:
    return _any_artifact_file(workspace, descriptor, "_bridges")


def _has_bridge_checks(workspace: Path) -> bool:
    directory = workspace / "ci" / "results" / "bridge_checks"
    return directory.is_dir() and any(directory.glob("*.json"))


def _has_witnesses(workspace: Path, descriptor: dict | None) -> bool:
    return _any_artifact_file(workspace, descriptor, "_witnesses")


def _any_artifact_file(workspace: Path, descriptor: dict | None, dirname: str) -> bool:
    if descriptor is not None:
        for crate in descriptor.get("crates", []):
            crate_dir = crate.get("crate_dir")
            if crate_dir and (workspace / crate_dir / "specs" / dirname).is_dir():
                if any((workspace / crate_dir / "specs" / dirname).glob("*.json")):
                    return True
    return False


def _run_gates(workspace: Path, descriptor: dict | None):
    """Run the six §3 gates read-only, returning (gate_runs, findings)."""
    runs: list[dict] = []
    findings: list[NormalizedFinding] = []
    if descriptor is None:
        for gate_id in ("r1-g16", "g9", "g14", "g18", "g19", "g20"):
            runs.append(
                {"gate_id": gate_id, "outcome": "not-applicable", "reason": "no valid project descriptor"}
            )
        return runs, findings, None

    # r1-g16
    if not _has_c_static(workspace):
        runs.append(
            {
                "gate_id": "r1-g16",
                "outcome": "blocked",
                "reason": "no C_static reports at ci/results/c_static/ -- run extract-c-static first",
            }
        )
    else:
        try:
            import gate_r1_g16

            interaction_dirs = {
                crate["crate_dir"]: workspace / crate["crate_dir"] / "specs" / "_interactions"
                for crate in descriptor.get("crates", [])
                if crate.get("crate_dir")
            }
            raw, _ = gate_r1_g16.gate_workspace(workspace, interaction_dirs)
            runs.append({"gate_id": "r1-g16", "outcome": "executed"})
            for f in raw:
                findings.append(_normalize_finding(f, "r1-g16", "scripts/gate_r1_g16.py:gate_workspace"))
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed silently
            runs.append({"gate_id": "r1-g16", "outcome": "blocked", "reason": str(exc)})

    # g9
    if not _has_bridges(workspace, descriptor):
        runs.append({"gate_id": "g9", "outcome": "not-applicable", "reason": "no bridge specifications present in this workspace"})
    elif not _has_bridge_checks(workspace):
        runs.append(
            {
                "gate_id": "g9",
                "outcome": "blocked",
                "reason": "check-bridges has not run: no ci/results/bridge_checks/ directory",
            }
        )
    else:
        try:
            import gate_g9

            raw, _ = gate_g9.gate_workspace(workspace, descriptor)
            runs.append({"gate_id": "g9", "outcome": "executed"})
            for f in raw:
                findings.append(_normalize_finding(f, "g9", "scripts/gate_g9.py:gate_workspace"))
        except Exception as exc:  # noqa: BLE001
            runs.append({"gate_id": "g9", "outcome": "blocked", "reason": str(exc)})

    # g14
    if not _closure_profiles(workspace):
        runs.append({"gate_id": "g14", "outcome": "not-applicable", "reason": "no closure profiles present"})
    else:
        try:
            import gate_g14

            outcomes, raw = gate_g14.gate_workspace(workspace, descriptor)
            runs.append({"gate_id": "g14", "outcome": "executed"})
            for outcome in outcomes:
                for f in outcome.findings:
                    findings.append(_normalize_finding(f, "g14", "scripts/gate_g14.py:gate_workspace"))
            for f in raw:
                findings.append(_normalize_finding(f, "g14", "scripts/gate_g14.py:gate_workspace"))
        except Exception as exc:  # noqa: BLE001
            runs.append({"gate_id": "g14", "outcome": "blocked", "reason": str(exc)})

    has_witnesses = _has_witnesses(workspace, descriptor)
    witness_backend = descriptor.get("witness_backend")

    # g18
    if not has_witnesses:
        runs.append({"gate_id": "g18", "outcome": "not-applicable", "reason": "no witness specifications present"})
    else:
        try:
            import gate_g18

            raw, _ = gate_g18.gate_workspace(workspace, descriptor)
            runs.append({"gate_id": "g18", "outcome": "executed"})
            for f in raw:
                findings.append(_normalize_finding(f, "g18", "scripts/gate_g18.py:gate_workspace"))
        except Exception as exc:  # noqa: BLE001
            runs.append({"gate_id": "g18", "outcome": "blocked", "reason": str(exc)})

    # g19
    if not has_witnesses:
        runs.append({"gate_id": "g19", "outcome": "not-applicable", "reason": "no witness specifications present"})
    elif not witness_backend:
        runs.append(
            {
                "gate_id": "g19",
                "outcome": "blocked",
                "reason": "no witness backend configured in the project descriptor",
            }
        )
    else:
        try:
            import gate_g19

            raw, _ = gate_g19.gate_workspace(workspace, descriptor)
            runs.append({"gate_id": "g19", "outcome": "executed"})
            for f in raw:
                findings.append(_normalize_finding(f, "g19", "scripts/gate_g19.py:gate_workspace"))
        except Exception as exc:  # noqa: BLE001
            runs.append({"gate_id": "g19", "outcome": "blocked", "reason": str(exc)})

    # g20
    if not has_witnesses:
        runs.append({"gate_id": "g20", "outcome": "not-applicable", "reason": "no witness specifications present"})
    else:
        try:
            import gate_g20

            raw, _ = gate_g20.gate_workspace(workspace, descriptor)
            runs.append({"gate_id": "g20", "outcome": "executed"})
            for f in raw:
                findings.append(_normalize_finding(f, "g20", "scripts/gate_g20.py:gate_workspace"))
        except Exception as exc:  # noqa: BLE001
            runs.append({"gate_id": "g20", "outcome": "blocked", "reason": str(exc)})

    return runs, findings, witness_backend


def _relativize(subject: str, workspace: Path) -> str:
    """Findings whose subject is an absolute path become workspace-relative,
    so the same workspace produces the same document wherever it lives."""
    try:
        path = Path(subject)
        if path.is_absolute():
            return path.resolve().relative_to(workspace.resolve()).as_posix()
    except (ValueError, OSError):
        pass
    return subject


def _lifecycle_findings(artifacts: list[dict]) -> list[NormalizedFinding]:
    """State transitions the schema-based validators cannot see: a
    promoted artifact whose bytes changed after promotion, and a receipt
    entry whose file no longer exists. Both are real, mechanized facts."""
    findings: list[NormalizedFinding] = []
    for artifact in artifacts:
        if artifact["lifecycle"] == "stale-by-hash-drift":
            findings.append(
                NormalizedFinding(
                    gate_id="promotion-integrity",
                    severity="high",
                    subject=artifact["path"],
                    reason=(
                        "promoted artifact has changed on disk since promotion "
                        f"(recorded {artifact.get('promoted_hash')}, current {artifact.get('content_hash')})"
                    ),
                    authority="mechanized-gate",
                    provenance="scripts/project_state.py:_lifecycle_findings",
                )
            )
        elif artifact["lifecycle"] == "absent":
            findings.append(
                NormalizedFinding(
                    gate_id="promotion-integrity",
                    severity="medium",
                    subject=artifact["path"],
                    reason="a promotion receipt lists this artifact but it is absent from the workspace",
                    authority="mechanized-gate",
                    provenance="scripts/project_state.py:_lifecycle_findings",
                )
            )
    return findings


def analyze(workspace: Path, descriptor_path: Path) -> Analysis:
    workspace = workspace.resolve()
    descriptor: dict | None = None
    descriptor_state = "absent"
    descriptor_error: str | None = None
    descriptor_diagnostics: list[str] = []
    if descriptor_path.is_file():
        try:
            descriptor = load_project_descriptor(descriptor_path)
            descriptor_state = "present-valid"
        except (ProjectDescriptorError, ValueError) as exc:
            descriptor_state = "present-invalid"
            descriptor_error = str(exc)
            descriptor_diagnostics = list(getattr(exc, "diagnostics", []) or [])
        except OSError as exc:
            descriptor_state = "unknown"
            descriptor_error = str(exc)

    promotions = _promotion_manifest(workspace)
    installation = _installation_manifest(workspace)
    # chainlink #78: the normative user-owned documents' drift records (the
    # reliance policy's on-disk content vs. the manifest's reviewed hash),
    # computed once here and consumed by both the installation state and
    # the policy-drift findings below.
    policy_drift_records = _normative_drift_records(workspace)
    artifacts = _artifacts(workspace, descriptor, promotions)
    obligations = _obligations(workspace, descriptor)
    clusters = _clusters(workspace, descriptor)
    observations = _observations(workspace, descriptor)
    gate_state, gate_details, gate_problems = _gate_integrity_evaluate(
        workspace, descriptor, installation, descriptor_state, descriptor_diagnostics
    )
    gate_integrity = {"state": gate_state, "details": gate_details}
    # chainlink #77: the descriptor's write_set is the enforcement boundary
    # for where the implementing agent may write -- and until now it was
    # consumed by no command and mentioned in no generated file. The same
    # run reports it here (status carries the state, check carries the
    # findings) so the boundary is machine-checked, not merely declared.
    write_set_state = write_set.check_write_set(workspace, descriptor, descriptor_path)
    write_set_doc = {"state": write_set_state.state, "details": write_set_state.details}
    # chainlink #75: installation_manifest.state must not read "current"
    # while the gate definitions it pins are drifted or unverifiable --
    # a consumer reading only `status --json` was told the installation is
    # current in a workspace whose gate definitions had been altered.
    # chainlink #78 extends the same fail-closed discipline to the
    # normative user-owned documents (the reliance policy): a consumer
    # reading only `status --json` must not be told the installation is
    # current while the governance document it pins is missing or edited.
    if installation["state"] == "current":
        if gate_state == "drifted" or policy_drift_records:
            installation["state"] = "drifted"
        elif gate_state != "pinned":
            installation["state"] = "unknown"

    findings = _run_standalone_validators(workspace, descriptor)
    # chainlink #79: a staged evidence draft is inert (validate-evidence skips
    # *.draft), so it produces no finding -- but it is still a pending artifact
    # the agent must promote. Track it here so `check next` can recommend
    # `promote-evidence` (the action_id the issue found missing) instead of
    # leaving the agent with no machine-consumable next step for it.
    pending_evidence_drafts = find_evidence_drafts(workspace)
    if gate_integrity["state"] != "pinned":
        findings.extend(_gate_integrity_findings(gate_integrity, gate_problems))
    # chainlink #78: one high-severity finding per normative user-owned
    # document that is missing or drifted from its reviewed content.
    findings.extend(_policy_drift_findings(policy_drift_records))
    # chainlink #77: one high-severity finding per write-set violation (an
    # out-of-set file, or a vacuous declaration that enforces nothing).
    # High severity: a file no declaration accounts for, or a write set
    # that declares no boundary, is exactly the enforcement-boundary
    # violation this check exists to catch -- it blocks. The
    # protected-surface audit (protected_unvouched) is deliberately not a
    # check finding: it is non-blocking and lives in the dedicated
    # `write-set-check` command's own report.
    for violation in write_set_state.violations:
        findings.append(
            NormalizedFinding(
                gate_id="write-set",
                severity="high",
                subject=violation.path,
                reason=violation.reason,
                authority="mechanized-gate",
                provenance="scripts/write_set.py:check_write_set",
            )
        )
    findings.extend(_descriptor_placeholder_findings(descriptor, descriptor_state, descriptor_path))
    findings.extend(_descriptor_invalid_findings(descriptor_state, descriptor_diagnostics, descriptor_path))
    # chainlink #78: the descriptor's reliance_policy_path pointer is
    # validated (exists + inside the project root) rather than advisory --
    # see _descriptor_policy_path_findings.
    findings.extend(_descriptor_policy_path_findings(descriptor, workspace))
    gate_runs, gate_findings, witness_backend = _run_gates(workspace, descriptor)
    findings.extend(gate_findings)
    findings.extend(_lifecycle_findings(artifacts))
    for finding in findings:
        finding.subject = _relativize(finding.subject, workspace)

    conditions: set[str] = set()
    if descriptor_state in ("absent", "present-invalid", "unknown"):
        conditions.add("invalid_input")
    if gate_integrity["state"] != "pinned":
        # chainlink #75: check fails closed on installation integrity -- a
        # gate definition that is drifted, missing, or unverifiable means
        # the check's own basis cannot be trusted, so the run reports the
        # gate-integrity condition regardless of what else it found.
        conditions.add("gate_integrity_failed")
    if any(f.severity in ("critical", "high") for f in findings):
        conditions.add("blocking_findings")
    if any(r.get("outcome") == "blocked" and "backend" in str(r.get("reason", "")) for r in gate_runs):
        conditions.add("backend_unavailable")
    if any(f.authority == "human-decision-pending" for f in findings):
        conditions.add("human_decision_required")

    return Analysis(
        workspace=workspace,
        descriptor=descriptor,
        descriptor_state=descriptor_state,
        descriptor_error=descriptor_error,
        descriptor_diagnostics=descriptor_diagnostics,
        artifacts=artifacts,
        obligations=obligations,
        clusters=clusters,
        findings=findings,
        gate_runs=gate_runs,
        installation=installation,
        gate_integrity=gate_integrity,
        write_set=write_set_doc,
        observations=observations,
        conditions=conditions,
        pending_evidence_drafts=pending_evidence_drafts,
    )


# ---------------------------------------------------------------------------
# Document builders
# ---------------------------------------------------------------------------
def build_project_state(workspace: Path, descriptor_path: Path) -> dict:
    analysis = analyze(workspace, descriptor_path)
    descriptor = analysis.descriptor
    path = _rel(descriptor_path, workspace) if descriptor_path.is_absolute() else str(descriptor_path)

    descriptor_doc = {
        "state": analysis.descriptor_state,
        "path": path,
    }
    if analysis.descriptor_state in ("present-valid", "present-invalid"):
        descriptor_doc["mode"] = descriptor.get("mode") if descriptor else None
        descriptor_doc["schema_version"] = descriptor.get("schema_version") if descriptor else None
        # The declared closure_kind intent, read back here so it is visible
        # in status output rather than living only in the descriptor file
        # (chainlink #73). null when absent, invalid, or undeclared.
        descriptor_doc["closure_kind"] = descriptor.get("closure_kind") if descriptor else None
        # The effective verifier policy, read back here so a pilot's core
        # declaration is observable in status output (chainlink #76) --
        # previously no command reported it: `status --json` and
        # `check --json` contained zero occurrences of the string
        # "verifier" in either the valid or the invalid case, so the value
        # domain (creusot | verus | kani) and the per-cluster overrides
        # were discoverable only by opening the descriptor file or probing
        # the schema by trial and error. Normalized into the shape the
        # pipeline actually consumes (gate g9 resolves
        # policy.get(cluster, policy["default"])): `default`, the
        # per-cluster overrides under `clusters`, and the declared
        # multi-verifier composition under `supporting`. null when the
        # descriptor is present but invalid; omitted when it is absent or
        # unreadable, matching mode/schema_version/closure_kind.
        policy = descriptor.get("verifier_policy") if descriptor is not None else None
        if isinstance(policy, dict):
            descriptor_doc["verifier_policy"] = {
                "default": policy.get("default"),
                "clusters": {
                    key: value
                    for key, value in policy.items()
                    if key not in ("default", "supporting")
                },
                "supporting": policy.get("supporting", []),
            }
        else:
            descriptor_doc["verifier_policy"] = None

    open_findings: dict[str, dict] = {}
    human_decisions: dict[str, dict] = {}
    for f in analysis.findings:
        normalized = f.as_dict()
        open_findings.setdefault(
            normalized["dedup_key"],
            {
                "dedup_key": normalized["dedup_key"],
                "gate_id": f.gate_id,
                "severity": f.severity,
                "subject": f.subject or "(workspace)",
                "summary": f.reason,
            },
        )
        if f.authority == "human-decision-pending":
            human_decisions.setdefault(
                normalized["dedup_key"],
                {
                    "decision_id": normalized["dedup_key"],
                    "subject": f.subject or "(workspace)",
                    "status": "pending",
                    "gate_id": f.gate_id,
                },
            )
    change_requests = _change_request_stubs(analysis)
    identity = adjudicator.current_identity()

    return {
        "schema_version": "1.0",
        "product": {"name": PRODUCT_NAME, "version": PRODUCT_VERSION},
        "installation_manifest": analysis.installation,
        "descriptor": descriptor_doc,
        "binary_identity": {
            "verified": identity["verified"],
            "content_hash": identity["content_hash"],
            "version": PRODUCT_VERSION,
        },
        "artifacts": analysis.artifacts,
        "obligations": analysis.obligations,
        "clusters": analysis.clusters,
        "open_findings": [open_findings[key] for key in sorted(open_findings)],
        "human_decisions": [human_decisions[key] for key in sorted(human_decisions)],
        "change_requests": change_requests,
        "gate_integrity": analysis.gate_integrity,
        "write_set": analysis.write_set,
        "generated_observations": analysis.observations,
    }


def _change_request_stubs(analysis: Analysis) -> list[dict]:
    stubs = []
    for cluster in analysis.clusters:
        if cluster["state"] in ("degraded", "blocked", "unknown"):
            if cluster["state"] == "unknown":
                continue
            stubs.append(
                {
                    "change_request_id": f"CR-{cluster['cluster']}-closure",
                    "status": "proposed",
                    "target": f"specs/_closure/{cluster['cluster']}.json",
                    "summary": (
                        f"cluster {cluster['cluster']!r} is {cluster['state']} under limitations "
                        f"{cluster['limitations']!r}; a human must decide whether to re-decompose, "
                        f"accept the degradation, or supply the missing evidence"
                    ),
                }
            )
    stubs.sort(key=lambda s: s["change_request_id"])
    return stubs


# ---------------------------------------------------------------------------
# Next-action selection (deterministic)
# ---------------------------------------------------------------------------
_REFRESH_BY_GATE = {
    "r1-g16": ("refresh-c-static", "ligature extract-c-static --target <target-triple>"),
    "g9": ("refresh-bridge-checks", "ligature check-bridges"),
    "g19": ("refresh-witness", "ligature render-witness <witness_id> --renderer <declared-renderer>"),
}


def _next_action(analysis: Analysis) -> dict | None:
    if analysis.descriptor is None:
        if analysis.descriptor_state == "present-invalid":
            # A user-fixable input error: name the offending property and
            # the fix, rather than the old honest-null that left a
            # descriptor typo with no recovery path at all (chainlink #73).
            # kind is human-decision (not automated-command): no CLI
            # command can repair a descriptor -- a person must edit it.
            detail = (
                "; ".join(analysis.descriptor_diagnostics)
                or analysis.descriptor_error
                or "schema-invalid"
            )
            return {
                "kind": "human-decision",
                "description": (
                    f"project descriptor is invalid: {detail}. "
                    "Fix project-descriptor.json, then re-run check."
                ),
                "command": None,
            }
        # No project descriptor at all: nothing mechanized can be
        # recommended, and the action_id enum has no "init". Honest null.
        return None

    # 0. A descriptor that is still the Stage P0 template makes every
    #    downstream recommendation premature: the project hasn't been
    #    described yet. This outranks the refresh recommendations below,
    #    which would otherwise point at an artifact of an undescribed
    #    project (chainlink #67).
    for f in analysis.findings:
        if f.gate_id == "P0":
            return {
                "kind": "human-decision",
                "description": f"{f.reason} (subject: {f.subject})",
                "command": None,
            }

    # 0.5. A staged evidence draft is inert (validate-evidence skips *.draft,
    #    chainlink #79) so it raises no finding -- but it is a ready,
    #    already-validated Stage 0 artifact, and the agent's loop (status ->
    #    check next -> perform -> repeat) had no action_id for it: `check
    #    next` named `refresh-c-static` (a Stage 8A refresh) and said
    #    nothing about the pending draft. Recommend the mechanical promotion
    #    ahead of the gate refreshes below, which are about missing
    #    prerequisites for LATER stages, not a task that is ready now. It
    #    still sits below the invalid-descriptor and P0 checks above, which
    #    are about the project being undescribed.
    if analysis.pending_evidence_drafts:
        draft = analysis.pending_evidence_drafts[0]
        target = draft.with_suffix("")  # strip the .draft staging suffix
        return {
            "kind": "automated-command",
            "action_id": "promote-evidence",
            "description": (
                f"staged evidence draft {draft.name} is pending promotion; "
                f"run `ligature promote-evidence {target}` to make it a real evidence record"
            ),
            "command": f"ligature promote-evidence {target}",
        }

    # 1. A blocked gate whose missing prerequisite is one of the three
    #    internal refresh operations check may recommend but never runs.
    for run in analysis.gate_runs:
        if run.get("outcome") == "blocked" and run["gate_id"] in _REFRESH_BY_GATE:
            action_id, command = _REFRESH_BY_GATE[run["gate_id"]]
            return {
                "kind": "automated-command",
                "action_id": action_id,
                "description": run["reason"],
                "command": command,
            }

    # 2. An undeclared cross-concept call is an authoring gap, not a refresh.
    for f in analysis.findings:
        if f.gate_id == "r1-g16" and "interaction" in f.reason.lower():
            return {
                "kind": "automated-command",
                "action_id": "author-interaction",
                "description": (
                    f"{f.reason}. Author or update the interaction spec covering {f.subject}, "
                    "then re-run gate r1-g16."
                ),
                "command": f"ligature draft interaction {f.subject}",
            }

    # 3. Nothing mechanized is outstanding -- a human decision is next.
    for f in analysis.findings:
        if f.authority == "human-decision-pending":
            return {
                "kind": "human-decision",
                "description": f"{f.reason} (subject: {f.subject}); a human risk disposition is required",
                "command": None,
            }

    for f in analysis.findings:
        if f.authority == "external-authority-missing":
            return {
                "kind": "external-authority-missing",
                "description": f"{f.reason} (subject: {f.subject})",
                "command": None,
            }

    return None


def build_consolidated_check(workspace: Path, descriptor_path: Path) -> dict:
    analysis = analyze(workspace, descriptor_path)
    findings = [f.as_dict() for f in analysis.findings]
    # Consolidate by dedup_key without double-counting; keep first
    # provenance (deterministic because findings are produced in a fixed
    # module/gate order).
    deduped: dict[str, dict] = {}
    for finding in findings:
        deduped.setdefault(finding["dedup_key"], finding)
    consolidated = sorted(deduped.values(), key=lambda f: (f["gate_id"], f["subject"], f["dedup_key"]))

    next_action = _next_action(analysis)
    return {
        "schema_version": "1.0",
        "mutated_workspace": False,
        "gates": analysis.gate_runs,
        "findings": consolidated,
        "next_action": next_action,
        "change_request_stubs": _change_request_stubs(analysis),
        "result": {
            "exit_code": exit_codes.resolve(analysis.conditions),
            "conditions": sorted(analysis.conditions),
        },
    }


def canonical_json(document: dict) -> str:
    """The bytes both schemas' determinism guarantees are stated against:
    sort_keys, compact separators, trailing newline."""
    return json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"


# ---------------------------------------------------------------------------
# Human renderers
# ---------------------------------------------------------------------------
def render_status_text(document: dict) -> str:
    lines = [
        f"{document['product']['name']} {document['product']['version']} -- project state",
        f"descriptor: {document['descriptor']['state']} ({document['descriptor']['path']})",
        f"installation manifest: {document['installation_manifest']['state']}",
        f"gate integrity: {document['gate_integrity']['state']}"
        + (f" -- {document['gate_integrity']['details']}" if document['gate_integrity'].get('details') else ""),
    ]
    by_lifecycle: dict[str, int] = {}
    for artifact in document["artifacts"]:
        by_lifecycle[artifact["lifecycle"]] = by_lifecycle.get(artifact["lifecycle"], 0) + 1
    if by_lifecycle:
        rendered = ", ".join(f"{name}={count}" for name, count in sorted(by_lifecycle.items()))
        lines.append(f"artifacts ({len(document['artifacts'])}): {rendered}")
    else:
        lines.append("artifacts: none discovered")
    unmet = [
        o
        for o in document["obligations"]
        if any(d["status"] != "achieved" for d in o["required_assurance"])
    ]
    lines.append(f"obligations: {len(document['obligations'])} declared, {len(unmet)} with an unmet required dimension")
    for cluster in document["clusters"]:
        lines.append(
            f"cluster {cluster['cluster']}: {cluster['state']} (closure_kind={cluster['closure_kind']})"
            + (f" limitations={cluster['limitations']}" if cluster["limitations"] else "")
        )
    lines.append(f"open findings: {len(document['open_findings'])}")
    for finding in document["open_findings"][:10]:
        lines.append(f"  - [{finding['severity']}] {finding['gate_id']}: {finding['summary']}")
    lines.append(f"human decisions pending: {len([d for d in document['human_decisions'] if d['status'] == 'pending'])}")
    lines.append(f"change requests: {len(document['change_requests'])}")
    for stub in document["change_requests"]:
        lines.append(f"  - {stub['change_request_id']}: {stub['target']}")
    return "\n".join(lines) + "\n"


def render_check_text(document: dict) -> str:
    lines = ["consolidated check"]
    for run in document["gates"]:
        outcome = run["outcome"]
        suffix = f" -- {run['reason']}" if run.get("reason") else ""
        lines.append(f"  gate {run['gate_id']}: {outcome}{suffix}")
    lines.append(f"findings: {len(document['findings'])}")
    for finding in document["findings"][:20]:
        lines.append(
            f"  - [{finding['severity']}/{finding['authority']}] {finding['gate_id']}: "
            f"{finding['subject']}: {finding['reason']}"
        )
    action = document["next_action"]
    if action is None:
        lines.append("next action: (none)")
    elif action["kind"] == "automated-command":
        lines.append(f"next action: [{action['action_id']}] {action['description']}")
        lines.append(f"  run: {action['command']}")
    else:
        lines.append(f"next action ({action['kind']}): {action['description']}")
    lines.append(f"change-request stubs: {len(document['change_request_stubs'])}")
    lines.append(f"result: exit_code={document['result']['exit_code']} conditions={document['result']['conditions']}")
    return "\n".join(lines) + "\n"


def render_next_action_text(document: dict) -> str:
    action = document["next_action"]
    if action is None:
        return "next action: (none)\n"
    if action["kind"] == "automated-command":
        return f"next action: [{action['action_id']}] {action['command']}\n"
    return f"next action ({action['kind']}): {action['description']}\n"
