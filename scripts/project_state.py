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

import exit_codes  # noqa: E402
from project_descriptor import ProjectDescriptorError  # noqa: E402
from project_descriptor import load_project_descriptor  # noqa: E402
from schema_utils import make_validator  # noqa: E402
from schema_utils import make_validator_without_required  # noqa: E402

PRODUCT_NAME = "ligature"
# The running product's own version. #57 owns real release versioning;
# until a packaged build exists the honest value is the same
# "0.0.0-unreleased" the empty project-state example uses.
PRODUCT_VERSION = "0.0.0-unreleased"

ROOT = Path(__file__).resolve().parent.parent
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


def _gate_integrity(workspace: Path, descriptor: dict | None, installation: dict) -> dict:
    if descriptor is None:
        return {"state": "unknown", "details": "no project descriptor present"}
    entries = descriptor.get("gate_integrity") or []
    paths = [e.get("path") for e in entries if isinstance(e, dict) and e.get("path")]
    if not paths:
        return {"state": "unknown", "details": "descriptor declares no gate_integrity paths"}
    recorded = installation.get("gate_hashes") if isinstance(installation, dict) else None
    if installation.get("state") == "not-initialized" or not isinstance(recorded, dict):
        return {
            "state": "unpinned",
            "details": f"{len(paths)} gate path(s) declared; no installation manifest records their hashes",
        }
    drifted = []
    for rel in paths:
        target = workspace / rel
        if not target.is_file():
            drifted.append(f"{rel}: missing")
        elif recorded.get(rel) not in (None, _sha256_file(target)):
            drifted.append(f"{rel}: hash drift")
    if drifted:
        return {"state": "drifted", "details": "; ".join(drifted)}
    return {"state": "pinned", "details": f"{len(paths)} gate path(s) match the installed manifest"}


# ---------------------------------------------------------------------------
# Artifact discovery + lifecycle
# ---------------------------------------------------------------------------
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
    _walk(workspace / "ci" / "manifest", "work-package")
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
    observations: dict
    conditions: set[str] = field(default_factory=set)

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


def _run_standalone_validators(workspace: Path) -> list[NormalizedFinding]:
    findings: list[NormalizedFinding] = []
    for module_name, _ in _VALIDATE_MODULES:
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
    return findings


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
    if descriptor_path.is_file():
        try:
            descriptor = load_project_descriptor(descriptor_path)
            descriptor_state = "present-valid"
        except (ProjectDescriptorError, ValueError) as exc:
            descriptor_state = "present-invalid"
            descriptor_error = str(exc)
        except OSError as exc:
            descriptor_state = "unknown"
            descriptor_error = str(exc)

    promotions = _promotion_manifest(workspace)
    installation = _installation_manifest(workspace)
    artifacts = _artifacts(workspace, descriptor, promotions)
    obligations = _obligations(workspace, descriptor)
    clusters = _clusters(workspace, descriptor)
    observations = _observations(workspace, descriptor)
    gate_integrity = _gate_integrity(workspace, descriptor, installation)

    findings = _run_standalone_validators(workspace)
    gate_runs, gate_findings, witness_backend = _run_gates(workspace, descriptor)
    findings.extend(gate_findings)
    findings.extend(_lifecycle_findings(artifacts))
    for finding in findings:
        finding.subject = _relativize(finding.subject, workspace)

    conditions: set[str] = set()
    if descriptor_state in ("absent", "present-invalid", "unknown"):
        conditions.add("invalid_input")
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
        artifacts=artifacts,
        obligations=obligations,
        clusters=clusters,
        findings=findings,
        gate_runs=gate_runs,
        installation=installation,
        gate_integrity=gate_integrity,
        observations=observations,
        conditions=conditions,
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

    return {
        "schema_version": "1.0",
        "product": {"name": PRODUCT_NAME, "version": PRODUCT_VERSION},
        "installation_manifest": analysis.installation,
        "descriptor": descriptor_doc,
        "binary_identity": {"verified": "unknown", "content_hash": None, "version": PRODUCT_VERSION},
        "artifacts": analysis.artifacts,
        "obligations": analysis.obligations,
        "clusters": analysis.clusters,
        "open_findings": [open_findings[key] for key in sorted(open_findings)],
        "human_decisions": [human_decisions[key] for key in sorted(human_decisions)],
        "change_requests": change_requests,
        "gate_integrity": analysis.gate_integrity,
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
        # No valid project descriptor: nothing mechanized can be
        # recommended, and the action_id enum has no "init". Honest null.
        return None

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
