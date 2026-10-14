#!/usr/bin/env python3
"""Assurance recording (chainlink #87): assemble a work package's
assurance report from verifier proof evidence.

The gap this closes
-------------------
`gate-g14` evaluates every obligation against an ACHIEVED record it finds
at the owning manifest's own `report.emit` path (plan.md §8.5, chainlink
#25's docs/assurance-report-schema.json), and nothing in the product could
ever write one. The date-creusot pilot's Creusot proofs were complete
(`cargo creusot` over rust/date-creusot-core/verif/) and gate-g14 still
reported all five obligations as "recorded no achieved assurance", with no
`assure`/`record-proof` verb able to change it -- the report path held an
empty feature-ledger projection instead, and `gate_g9.bridge_records_for()`
(the honest-direction helper plan.md §8.3 asks for) existed only for the
BRIDGE half of the same report. This is the obligation half.

Both schemas are explicit about why nobody may simply type the answer:
docs/achieved-assurance-schema.json records what was achieved "never
hand-authored or human-reviewed ... a human freely authoring what was
'achieved' would be indistinguishable from fabricating a verification
result", and docs/assurance-report-schema.json is "machine-emitted, never
hand-authored". So every field of the record this module writes is
DERIVED, and the caller only declares the one thing no artifact in this
pipeline can know: WHICH certificate discharges WHICH obligation.

What is derived, from where
---------------------------
  * `claim`, `evidence`, `support` -- read from verifier output. Creusot
    uses why3find `proof.json`. Verus uses its official `--output-json`
    result, with each guarantee bound to a named function by a
    `#![verifier::proof_note("ligature:<obligation>")]` inside its
    `ensures` clause. Verus must report
    whole-crate success, and the named function's JSON must contain that
    exact note in `obligation_proof_notes` but not `failed_proof_notes`.
    Aggregate verified/error counts alone never establish a guarantee.
    Kani 0.67 has no JSON verdict export; Ligature runs the manifest's exact
    Rust harness itself and requires a complete, all-success Kani 0.67
    `--output-into-files` per-harness result and complete run summary. G14
    repeats that run against current inputs.
  * `evidence.kind` / `evidence.verifier` / `evidence.harness` -- the
    manifest's own `definition_of_done.provided_guarantees[].harness`,
    through `bridge_harness.EVIDENCE_KIND_BY_VERIFIER`'s single mapping.
    `creusot` and `verus` have readers for their own formats; `kani_...`
    names one exact Kani harness and is accepted only after Ligature reruns
    it. An evidence kind is derived from evidence actually checked, never
    asserted from a harness name this command cannot follow.
  * `config` -- the manifest's provenance block (toolchain, target,
    features), which is where a work package already declares how it was
    built.
  * `trust.assumptions` -- always empty. No reader exists here for an
    assumption a proof relied on, and an empty list is the only answer
    that cannot claim one; satisfies() fails any assumption outside the
    required profile's allow-list.
  * `evidence.scope` -- the proof targets and verifier-specific hashes
    (what was actually checked). Deliberately NOT a copy of the
    requirement's `minimum_scope`: copying the required domain into the
    achievement would assert coverage the certificate never states. Kani
    records the bounded model as belonging to its exact named, hashed
    harness; if that scope is narrower than the manifest requires, the
    truthful record is kept and G14 reports the unsatisfied scope.

The declaration a human does make is checked, not trusted. Creusot uses
`--proof Obligation=path`; Verus uses `--verus-function Obligation=function`
with one shared `--verus-json` result file; Kani uses
`--kani-harness Obligation=harness` and `--kani-crate package-dir`:

  * the obligation must be one this manifest actually provides (a record
    for anything else satisfies nothing and belongs to no report);
  * the certificate must sit under a directory named for the
    obligation's own concept (`Weekday.C1` -> a `weekday/` path segment,
    the same snake_case the `<Concept>.C<n>` id grammar encodes), so the
    mapping cannot point at an unrelated concept's proofs;
  * the certificate must not be older than the Coma program it
    certifies (`<dir>.coma`), because a certificate written before the
    program it claims to discharge says nothing about the file on disk.
  * Verus function names must exactly match a key in `func-details`, and
    the corresponding proof-note marker must be present as proved; G14
    hashes and re-reads the same JSON result. Rust sources in the manifest's
    allowed write set must be no newer than that result; their source
    snapshot is recorded and rechecked by G14.
  * The Kani harness must exactly equal the manifest's `harness` value,
    begin with `kani_`, and have one explicit function definition in the
    manifest's allowed Rust write set. Ligature accepts only cargo-kani
    0.67.0 and reruns `cargo +<manifest-toolchain> kani -Z unstable-options
    --manifest-path <package>/Cargo.toml --harness <name> --exact
    --output-format regular --output-into-files --target-dir <temporary-dir>`
    (with the manifest features). It reads the fresh text result at
    `result_output_dir/<harness>` and checks Kani's stdout summary. Kani's
    documented host target must match
    manifest provenance; Cargo metadata must pass `--locked`. The record
    pins the source and harness hashes, and G14 repeats the exact run.
    Kani bounded scope is not promoted to an unbounded claim. Ligature does
    not infer Rust harness semantics: the `input_domain` names the exact
    bounded model defined by the hashed harness, so any broader manifest
    `minimum_scope` remains unsatisfied.

Report placement and the feature-ledger collision
-------------------------------------------------
The report is written to the manifest's own `report.emit` path -- the
only path gate-g14 reads. An existing assurance report for the same work
package is updated in place (its `bridge_records`, and obligation entries
this run does not record, survive); an existing feature ledger -- what
some workspaces point `report.emit` at, including the pilot, which is why
gate-g14 reported a non-schema-valid report at
`ci/results/feature_ledger.json` -- is replaced with a warning naming the
collision; anything else at that path is refused rather than destroyed.

Exit codes follow docs/exit-code-contract.md: 0 recorded, 1 the evidence
does not establish the obligation (stuck goals, unsuccessful Verus run,
missing proof note, a stale certificate, a record satisfies() would not
accept -- the check ran and found a real problem), 2 invalid input (a
malformed mapping, a missing or misplaced manifest, or input that is not
recognized verifier output).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from atomic_write import write_atomically  # noqa: E402
from bridge_harness import EVIDENCE_KIND_BY_VERIFIER  # noqa: E402
from bridge_harness import verifier_for_manifest_harness  # noqa: E402
from gate_g14 import load_assurance_report_validator  # noqa: E402
from gate_g14 import looks_like_work_package_manifest  # noqa: E402
from gate_g14 import work_package_manifest_dir_for  # noqa: E402
from generate_feature_ledger import load_validator as load_feature_ledger_validator  # noqa: E402
from satisfies import load_achieved_validator  # noqa: E402
from satisfies import satisfies  # noqa: E402
from validate_work_package import load_validator as load_work_package_validator  # noqa: E402
from validate_work_package import _pattern_covers_path  # noqa: E402

PROOF_FILE_NAME = "proof.json"
COMA_SUFFIX = ".coma"

# A verifier is supported only when this module has a reader for its actual
# result. Creusot uses why3find proof.json; Verus uses official --output-json;
# Kani 0.67 is rerun by Ligature because that release has no JSON verdict API.
SUPPORTED_HARNESSES = ("creusot", "verus", "kani")
VERUS_TARGET_PREFIX = "verus-json:"
KANI_TARGET_PREFIX = "kani-run:"
VERUS_NOTE_PREFIX = "ligature:"
KANI_VERSION = "cargo-kani 0.67.0"
KANI_TIMEOUT_SECONDS = 900

CLAIM_KIND = "postcondition-holds"

OBLIGATION_RE = re.compile(r"^[A-Z][A-Za-z0-9]*\.C[0-9]+$")

EXIT_OK = 0
EXIT_NOT_ESTABLISHED = 1
EXIT_INVALID_INPUT = 2

LEDGER_COLLISION_NOTE = (
    "`report feature-ledger` writes the same path and would replace this report in turn -- "
    "point the manifest's report.emit at a free ci/results/*.json path (e.g. "
    "ci/results/<work-package>.json) to separate the two artifacts"
)


class RecordAssuranceError(Exception):
    """Base for every refusal; `exit_code` is what the CLI returns."""

    exit_code = EXIT_INVALID_INPUT


class InvalidRecordInput(RecordAssuranceError):
    """The requested operation could not be attempted (exit 2)."""

    exit_code = EXIT_INVALID_INPUT


class EvidenceNotEstablished(RecordAssuranceError):
    """The evidence was readable and does not establish the obligation (exit 1)."""

    exit_code = EXIT_NOT_ESTABLISHED


@dataclass(frozen=True)
class Certificate:
    """One verifier proof certificate, reduced to what it establishes."""

    path: Path
    relative: str
    proved: int
    goal_count: int
    evidence_format: str = "why3find"
    content_hash: str | None = None
    verifier_version: str | None = None
    source_hash: str | None = None
    harness_hash: str | None = None


@dataclass(frozen=True)
class RecordedObligation:
    obligation_id: str
    certificates: list[Certificate]
    evidence_kind: str

    @property
    def goals(self) -> int:
        return sum(certificate.proved for certificate in self.certificates)

    @property
    def targets(self) -> list[str]:
        return [certificate.relative for certificate in self.certificates]


@dataclass(frozen=True)
class RecordOutcome:
    work_package: str
    report_path: Path
    report_relative: str
    recorded: list[RecordedObligation] = field(default_factory=list)
    preserved_obligation_records: int = 0
    bridge_records: int = 0
    replaced: str = "absent"  # "absent" | "assurance-report" | "feature-ledger"
    scope_warnings: dict[str, list[str]] = field(default_factory=dict)


def _relative(path: Path, workspace: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(workspace.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _contains(root: Path, path: Path) -> bool:
    """root itself, or something under it -- after resolution, so a `../`
    traversal or an absolute escape cannot pass (the containment discipline
    validate_work_package.check_gate_integrity applies to runner paths)."""
    root = root.resolve()
    path = path.resolve()
    return path == root or root in path.parents


def concept_of(obligation_id: str) -> str:
    """`Weekday.C1` -> `Weekday`: the concept half of the id grammar
    docs/achieved-assurance-schema.json's `obligation_id` pattern encodes."""
    return obligation_id.split(".", 1)[0]


def concept_segment(obligation_id: str) -> str:
    """`YearMonthDayLast.C1` -> `year_month_day_last`: the snake_case a
    Rust module directory of that concept is named after -- creusot lays
    `verif/<pkg>/<module>/...` out from the module the proofs belong to,
    which is the only machine link between an obligation id and a place
    on disk this workspace can offer."""
    concept = concept_of(obligation_id)
    with_underscores = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", concept)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", with_underscores).lower()


def resolve_manifest(workspace: Path, target: str) -> tuple[str, dict, Path]:
    """(work_package, manifest, path) for an id or a path.

    A path must BE the file gate-g14 reads -- `ci/manifest/<id>.json` --
    because recording assurance into a report no gate will load is worse
    than refusing: the caller would believe the obligation was discharged.
    A relative path is WORKSPACE-relative, the same rule `--proof` paths
    follow, so `--workspace elsewhere` behaves the same from any working
    directory. The id form resolves to exactly the same file, validated
    with the same validator and the same filename/id agreement rule
    `load_manifests` applies, so what this command records is what that
    gate reads."""
    if not workspace.is_dir():
        raise InvalidRecordInput(
            f"workspace does not exist or is not a directory: {workspace}"
        )
    workspace = workspace.resolve()
    manifest_dir = work_package_manifest_dir_for(workspace)

    as_path = Path(target)
    if as_path.suffix == ".json" or "/" in target:
        # A path is WORKSPACE-relative, the same rule --proof paths follow,
        # so `--workspace elsewhere record-assurance ci/manifest/WP-X.json`
        # means the same thing from any working directory.
        candidate = (workspace / as_path) if not as_path.is_absolute() else as_path
        candidate = candidate.resolve()
    else:
        candidate = manifest_dir / f"{target}.json"

    if not _contains(manifest_dir, candidate):
        raise InvalidRecordInput(
            f"manifest not at the path gate-g14 reads: expected "
            f"{_relative(manifest_dir / candidate.name, workspace)}, got "
            f"{_relative(candidate, workspace)} -- a report recorded against a manifest "
            "outside ci/manifest/ is a report no gate will ever load"
        )
    if not candidate.is_file():
        hint = f" (no work package named {target!r} under ci/manifest/)" if "/" not in target else ""
        raise InvalidRecordInput(
            f"work-package manifest not found: {_relative(candidate, workspace)}{hint}"
        )
    try:
        data = json.loads(candidate.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise InvalidRecordInput(
            f"manifest {_relative(candidate, workspace)} is not readable JSON: {e}"
        ) from e
    if not looks_like_work_package_manifest(data):
        raise InvalidRecordInput(
            f"{_relative(candidate, workspace)} is not a work-package manifest "
            "(no `work_package` / `definition_of_done` key)"
        )
    errors = list(load_work_package_validator().iter_errors(data))
    if errors:
        raise InvalidRecordInput(
            f"manifest {_relative(candidate, workspace)} is not schema-valid "
            f"({errors[0].message}) -- run `validate work-package` on it first"
        )
    work_package = data["work_package"]
    if work_package != candidate.stem:
        raise InvalidRecordInput(
            f"manifest declares work_package {work_package!r} but its filename says "
            f"{candidate.stem!r} -- the id a closure profile names must identify one file"
        )
    return work_package, data, candidate


def parse_proof_specs(specs: list[str]) -> dict[str, list[str]]:
    """`--proof <obligation>=<path>` (repeatable) -> obligation -> paths,
    in the order given, duplicates collapsed. Every failure here is a
    malformed argument, never an evidence problem."""
    if not specs:
        raise InvalidRecordInput(
            "no --proof given -- nothing to record. Pass "
            "`--proof <obligation>=<path-to-proof-certificate>` once per obligation "
            "(a path may be a directory holding proof.json, or the certificate itself)"
        )
    mapping: dict[str, list[str]] = {}
    for spec in specs:
        obligation, sep, path = spec.partition("=")
        obligation, path = obligation.strip(), path.strip()
        if not sep or not obligation or not path:
            raise InvalidRecordInput(
                f"--proof {spec!r} is not of the form <obligation>=<path> (e.g. "
                "--proof Weekday.C1=rust/date-creusot-core/verif/.../weekday_from_days)"
            )
        if not OBLIGATION_RE.match(obligation):
            raise InvalidRecordInput(
                f"--proof obligation {obligation!r} does not match the `<Concept>.C<n>` "
                "obligation id grammar"
            )
        paths = mapping.setdefault(obligation, [])
        if path not in paths:
            paths.append(path)
    return mapping


def parse_verus_function_specs(specs: list[str]) -> dict[str, list[str]]:
    """Parse repeatable ``--verus-function OBLIGATION=FUNCTION`` values.

    The function is the exact key in Verus's official ``func-details`` JSON
    object. Multiple functions may be attached to one obligation; every one
    must report that obligation's dedicated proof note as established.
    """
    mapping: dict[str, list[str]] = {}
    for spec in specs:
        obligation, sep, function = spec.partition("=")
        obligation, function = obligation.strip(), function.strip()
        if not sep or not obligation or not function:
            raise InvalidRecordInput(
                f"--verus-function {spec!r} is not of the form "
                "<obligation>=<function-name> (e.g. "
                "--verus-function Scheduler.C001=scheduler::check_invariant)"
            )
        if not OBLIGATION_RE.match(obligation):
            raise InvalidRecordInput(
                f"--verus-function obligation {obligation!r} does not match the "
                "`<Concept>.C<n>` obligation id grammar"
            )
        if "," in function or "#" in function:
            raise InvalidRecordInput(
                f"--verus-function name {function!r} contains a reserved delimiter"
            )
        functions = mapping.setdefault(obligation, [])
        if function not in functions:
            functions.append(function)
    return mapping


def parse_kani_harness_specs(specs: list[str]) -> dict[str, str]:
    """Parse repeatable ``--kani-harness OBLIGATION=HARNESS`` mappings.

    Kani's harness name is checked against the manifest's exact ``harness``
    value before Ligature invokes Kani. The narrow ``kani_`` identifier
    grammar matches the supported Rust proof-harness convention and keeps
    the command line from becoming an unchecked Cargo argument channel.
    """
    mapping: dict[str, str] = {}
    for spec in specs:
        obligation, sep, harness = spec.partition("=")
        obligation, harness = obligation.strip(), harness.strip()
        if not sep or not obligation or not harness:
            raise InvalidRecordInput(
                f"--kani-harness {spec!r} is not of the form "
                "<obligation>=<harness-name> (e.g. "
                "--kani-harness ChaCha20.C002=kani_chacha20_output_length)"
            )
        if not OBLIGATION_RE.match(obligation):
            raise InvalidRecordInput(
                f"--kani-harness obligation {obligation!r} does not match the "
                "`<Concept>.C<n>` obligation id grammar"
            )
        if not re.fullmatch(r"kani_[A-Za-z0-9_]+", harness):
            raise InvalidRecordInput(
                f"Kani harness name {harness!r} is unsupported; Kani 0.67 intake "
                "accepts exact bare Rust function names beginning `kani_`"
            )
        if obligation in mapping and mapping[obligation] != harness:
            raise InvalidRecordInput(
                f"--kani-harness maps {obligation} to more than one harness"
            )
        mapping[obligation] = harness
    return mapping


def kani_crate_root(workspace: Path, spec: str) -> Path:
    """Resolve one Cargo package directory inside the workspace."""
    raw = Path(spec)
    path = (raw if raw.is_absolute() else workspace / raw).resolve()
    if not _contains(workspace, path):
        raise InvalidRecordInput(
            f"Kani crate {spec!r} resolves outside the workspace"
        )
    if not path.is_dir() or not (path / "Cargo.toml").is_file():
        raise InvalidRecordInput(
            f"Kani crate {spec!r} must be a directory containing Cargo.toml"
        )
    relative = _relative(path, workspace)
    if "," in relative or "#" in relative:
        raise InvalidRecordInput(
            f"Kani crate path {relative!r} contains a reserved proof-target delimiter"
        )
    return path


def certificate_path_for(workspace: Path, spec: str, obligation_id: str) -> Path:
    """Resolve one `--proof` path to the proof.json that is the evidence,
    refusing anything that is not one. The concept anchor is checked here:
    `Weekday.C1`'s certificate must live under a directory named `weekday`,
    so a mapping cannot point at an unrelated concept's proofs."""
    raw = Path(spec)
    path = (raw if raw.is_absolute() else workspace / raw).resolve()
    if not _contains(workspace, path):
        raise InvalidRecordInput(
            f"evidence path {spec!r} resolves outside the workspace -- proof evidence must be "
            "part of the workspace that records it"
        )
    if not path.exists():
        raise InvalidRecordInput(f"evidence path does not exist: {_relative(path, workspace)}")
    if path.is_dir():
        certificate = path / PROOF_FILE_NAME
        if not certificate.is_file():
            raise InvalidRecordInput(
                f"no {PROOF_FILE_NAME} in {_relative(path, workspace)}/ -- this command reads "
                "why3find proof certificates (the proof witness `cargo creusot` writes)"
            )
        path = certificate
    if path.name != PROOF_FILE_NAME:
        raise InvalidRecordInput(
            f"{_relative(path, workspace)} is not a why3find proof certificate (expected a "
            f"directory holding {PROOF_FILE_NAME}, or the {PROOF_FILE_NAME} file itself)"
        )
    if concept_segment(obligation_id) not in set(path.parent.parts):
        raise InvalidRecordInput(
            f"proof evidence for {obligation_id} must sit under a directory named for its "
            f"concept ({concept_segment(obligation_id)!r}) -- {_relative(path, workspace)} does "
            "not, so it cannot be shown to be about this obligation"
        )
    return path


def verus_json_path_for(workspace: Path, spec: str) -> Path:
    """Resolve an official Verus ``--output-json`` file inside workspace."""
    raw = Path(spec)
    path = (raw if raw.is_absolute() else workspace / raw).resolve()
    if not _contains(workspace, path):
        raise InvalidRecordInput(
            f"Verus JSON path {spec!r} resolves outside the workspace -- verifier evidence "
            "must be part of the workspace that records it"
        )
    if not path.is_file():
        raise InvalidRecordInput(
            f"Verus JSON result is not a file: {_relative(path, workspace)}"
        )
    if path.suffix.lower() != ".json":
        raise InvalidRecordInput(
            f"{_relative(path, workspace)} is not a JSON result file from Verus `--output-json`"
        )
    relative = _relative(path, workspace)
    if "," in relative or "#" in relative:
        raise InvalidRecordInput(
            f"Verus JSON path {relative!r} contains a reserved target delimiter"
        )
    return path


_VERUS_SOURCE_SKIP_DIRS = frozenset(
    {".git", "target", "node_modules", "__pycache__"}
)


def rust_source_snapshot(workspace: Path, manifest: dict) -> tuple[str, int, str | None]:
    """Hash Rust files covered by this work package's allowed write set.

    Verus's JSON does not carry a source hash, and Kani 0.67 has no JSON
    verdict export. Bound tracked source files by the work package's
    declared write roots so both readers can identify exactly which Rust
    inputs were current when the evidence was checked.
    """
    write_policy = manifest.get("write_policy")
    patterns = write_policy.get("allowed_write_set") if isinstance(write_policy, dict) else None
    if not isinstance(patterns, list) or not all(isinstance(pattern, str) for pattern in patterns):
        raise InvalidRecordInput(
            "manifest has no usable write_policy.allowed_write_set for Verus source freshness"
        )
    normalized_patterns = []
    for pattern in patterns:
        normalized = pattern.rstrip("/")
        if pattern.endswith("/"):
            normalized += "/**"
        elif normalized and not any(char in normalized for char in "*?"):
            candidate = (workspace / normalized).resolve()
            if candidate.is_dir():
                normalized += "/**"
        normalized_patterns.append(normalized)

    sources: list[tuple[str, Path]] = []

    def fail_on_walk_error(error: OSError) -> None:
        raise InvalidRecordInput(
            f"cannot scan Rust sources under the workspace: {error}"
        ) from error

    for directory, dirnames, filenames in os.walk(workspace, onerror=fail_on_walk_error):
        dirnames[:] = sorted(name for name in dirnames if name not in _VERUS_SOURCE_SKIP_DIRS)
        for filename in filenames:
            if not filename.endswith(".rs"):
                continue
            candidate = Path(directory) / filename
            relative = candidate.relative_to(workspace).as_posix()
            if not any(_pattern_covers_path(pattern, relative) for pattern in normalized_patterns):
                continue
            resolved = candidate.resolve()
            if not _contains(workspace, resolved):
                raise InvalidRecordInput(
                    f"Verus source {relative} resolves outside the workspace -- source freshness "
                    "cannot be established"
                )
            sources.append((relative, resolved))

    digest = hashlib.sha256()
    newest_mtime_ns = 0
    newest_path: str | None = None
    for relative, path in sorted(sources):
        try:
            content = path.read_bytes()
            stat = path.stat()
        except OSError as exc:
            raise InvalidRecordInput(f"cannot read Verus source {relative}: {exc}") from exc
        relative_bytes = relative.encode("utf-8")
        digest.update(len(relative_bytes).to_bytes(8, "big"))
        digest.update(relative_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
        if stat.st_mtime_ns > newest_mtime_ns:
            newest_mtime_ns = stat.st_mtime_ns
            newest_path = relative
    return "sha256:" + digest.hexdigest(), newest_mtime_ns, newest_path


def _normalized_write_patterns(manifest: dict, workspace: Path) -> list[str]:
    write_policy = manifest.get("write_policy")
    patterns = write_policy.get("allowed_write_set") if isinstance(write_policy, dict) else None
    if not isinstance(patterns, list) or not all(isinstance(pattern, str) for pattern in patterns):
        raise InvalidRecordInput(
            "manifest has no usable write_policy.allowed_write_set for verifier source freshness"
        )
    normalized_patterns = []
    for pattern in patterns:
        normalized = pattern.rstrip("/")
        if pattern.endswith("/"):
            normalized += "/**"
        elif normalized and not any(char in normalized for char in "*?"):
            candidate = (workspace / normalized).resolve()
            if candidate.is_dir():
                normalized += "/**"
        normalized_patterns.append(normalized)
    return normalized_patterns


def _source_is_in_write_set(path: Path, workspace: Path, manifest: dict) -> bool:
    relative = path.resolve().relative_to(workspace.resolve()).as_posix()
    return any(
        _pattern_covers_path(pattern, relative)
        for pattern in _normalized_write_patterns(manifest, workspace)
    )


def kani_harness_source(
    workspace: Path, crate_root: Path, manifest: dict, harness: str
) -> tuple[Path, str]:
    """Find one explicit ``fn kani_*`` source in the manifest's writable Rust set.

    This intentionally supports ordinary named proof functions only. Macro-
    generated or multiply-defined harnesses are refused instead of guessed.
    """
    matcher = re.compile(rf"(?m)^\s*(?:pub(?:\([^)]*\))?\s+)?fn\s+{re.escape(harness)}\s*\(")
    matches: list[tuple[Path, bytes]] = []
    for directory, dirnames, filenames in os.walk(crate_root):
        dirnames[:] = sorted(name for name in dirnames if name not in _VERUS_SOURCE_SKIP_DIRS)
        for filename in sorted(filenames):
            if not filename.endswith(".rs"):
                continue
            path = (Path(directory) / filename).resolve()
            if not _contains(workspace, path):
                raise InvalidRecordInput(
                    f"Kani source {path} resolves outside the workspace"
                )
            if not _source_is_in_write_set(path, workspace, manifest):
                continue
            try:
                raw = path.read_bytes()
                content = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise InvalidRecordInput(f"cannot read Kani Rust source {path}: {exc}") from exc
            if matcher.search(content):
                matches.append((path, raw))
    if len(matches) != 1:
        raise InvalidRecordInput(
            f"Kani harness {harness!r} must have exactly one explicit Rust function "
            "definition under the manifest's allowed_write_set in the selected crate; "
            f"found {len(matches)}"
        )
    path, raw = matches[0]
    return path, "sha256:" + hashlib.sha256(raw).hexdigest()


def require_fresh_verus_json(
    path: Path, workspace: Path, source_snapshot: tuple[str, int, str | None]
) -> None:
    """Refuse a result written before any scoped Rust source it could prove."""
    _source_hash, newest_source_mtime_ns, newest_source = source_snapshot
    if newest_source is None:
        return
    try:
        result_mtime_ns = path.stat().st_mtime_ns
    except OSError as exc:
        raise InvalidRecordInput(
            f"cannot stat Verus JSON result {_relative(path, workspace)}: {exc}"
        ) from exc
    if newest_source_mtime_ns > result_mtime_ns:
        raise EvidenceNotEstablished(
            f"{newest_source} is newer than the Verus JSON result "
            f"{_relative(path, workspace)} -- the result does not describe the current Rust "
            "sources; re-run Verus and record assurance again"
        )


def verus_target(workspace: Path, path: Path, function: str) -> str:
    """Stable workspace-relative target stored for G14's independent reread."""
    relative = _relative(path, workspace)
    if "," in relative or "#" in relative or not function or "," in function or "#" in function:
        raise InvalidRecordInput("Verus JSON proof target contains a reserved delimiter")
    return f"{VERUS_TARGET_PREFIX}{relative}#{function}"


def parse_verus_target(target: str) -> tuple[str, str]:
    if not target.startswith(VERUS_TARGET_PREFIX):
        raise InvalidRecordInput(f"{target!r} is not a Verus JSON proof target")
    path, separator, function = target[len(VERUS_TARGET_PREFIX):].rpartition("#")
    if not separator or not path or not function or "," in target:
        raise InvalidRecordInput(
            f"{target!r} is not a valid Verus target; expected "
            "verus-json:<workspace-relative-json-path>#<function-name>"
        )
    return path, function


def kani_target(workspace: Path, crate_root: Path, harness: str) -> str:
    relative = _relative(crate_root, workspace)
    if "," in relative or "#" in relative or not re.fullmatch(r"kani_[A-Za-z0-9_]+", harness):
        raise InvalidRecordInput("Kani proof target contains an unsupported delimiter or harness")
    return f"{KANI_TARGET_PREFIX}{relative}#{harness}"


def parse_kani_target(target: str) -> tuple[str, str]:
    if not target.startswith(KANI_TARGET_PREFIX):
        raise InvalidRecordInput(f"{target!r} is not a Ligature Kani-run proof target")
    crate_path, separator, harness = target[len(KANI_TARGET_PREFIX):].rpartition("#")
    if not separator or not crate_path or not harness or "," in target:
        raise InvalidRecordInput(
            f"{target!r} is not a valid Kani target; expected "
            "kani-run:<workspace-relative-crate-dir>#<harness-name>"
        )
    return crate_path, harness


def parse_kani_067_result(
    result_text: str, driver_output: str, returncode: int, harness: str
) -> int:
    """Recognize only the complete Kani 0.67 regular-output verdict.

    Kani 0.67 has no JSON verdict interface. We do not ingest the detached
    CBMC build files or scrape a success-looking line: this parser is paired
    with Ligature's own exact-harness rerun and requires the complete
    per-check, aggregate, and manual-harness success summaries.
    """
    if not isinstance(result_text, str) or not isinstance(driver_output, str):
        raise InvalidRecordInput("Kani 0.67 returned non-text output")
    harness_headers = re.findall(r"(?m)^Checking harness (.+)\.\.\.\s*$", driver_output)
    if harness_headers != [harness]:
        raise InvalidRecordInput(
            f"Kani 0.67 output did not identify exactly the requested harness {harness!r}"
        )
    if result_text.count("RESULTS:") != 1:
        raise InvalidRecordInput("Kani 0.67 per-harness result has no unique RESULTS section")
    check_headers = re.findall(r"(?m)^Check [0-9]+: .+$", result_text)
    statuses = re.findall(r"(?m)^\s+- Status: ([A-Z_]+)\s*$", result_text)
    if not check_headers or len(statuses) != len(check_headers):
        raise InvalidRecordInput(
            "Kani 0.67 output has no complete per-check status list; incomplete results are refused"
        )
    if any(status != "SUCCESS" for status in statuses):
        raise EvidenceNotEstablished(
            f"Kani did not prove every check in harness {harness!r}: {statuses!r}"
        )
    check_count = len(check_headers)
    summary_match = re.search(r"(?m)^\s*\*\* ([0-9]+) of ([0-9]+) failed\s*$", result_text)
    manual_match = re.search(
        r"(?m)^Manual Harness Summary:\s*Complete - ([0-9]+) successfully verified "
        r"harnesses, ([0-9]+) failures, ([0-9]+) total\.\s*$",
        driver_output,
    )
    if summary_match is None or manual_match is None:
        if returncode != 0 or "VERIFICATION:- FAILED" in result_text:
            raise EvidenceNotEstablished(
                f"Kani 0.67 did not complete harness {harness!r} successfully"
            )
        raise InvalidRecordInput("Kani 0.67 output is missing a complete success summary")
    summary_failed, summary_total = map(int, summary_match.groups())
    succeeded_harnesses, failed_harnesses, total_harnesses = map(int, manual_match.groups())
    if summary_total != check_count:
        raise InvalidRecordInput(
            "Kani 0.67 aggregate check count does not match its per-check results"
        )
    if returncode != 0 or summary_failed != 0 or failed_harnesses != 0 or succeeded_harnesses != 1:
        raise EvidenceNotEstablished(
            f"Kani did not prove harness {harness!r} (exit={returncode}, "
            f"failed_checks={summary_failed}, failed_harnesses={failed_harnesses})"
        )
    if total_harnesses != 1:
        raise InvalidRecordInput(
            f"Kani exact-harness run unexpectedly covered {total_harnesses} harnesses"
        )
    if result_text.count("VERIFICATION:- SUCCESSFUL") != 1:
        if "VERIFICATION:- FAILED" in result_text:
            raise EvidenceNotEstablished(f"Kani reported harness {harness!r} as failed")
        raise InvalidRecordInput("Kani 0.67 output has no unique successful verification verdict")
    return check_count


def run_kani_067(crate_root: Path, manifest: dict, harness: str) -> int:
    """Rerun one named harness with the manifest's Cargo config and Kani 0.67."""
    cargo = shutil.which("cargo")
    if cargo is None:
        raise InvalidRecordInput("cargo is unavailable; Ligature cannot rerun Kani safely")
    provenance = manifest.get("provenance")
    if not isinstance(provenance, dict):
        raise InvalidRecordInput("manifest has no provenance for the Kani invocation")
    toolchain = provenance.get("toolchain")
    target = provenance.get("target")
    features = provenance.get("features")
    if (
        not isinstance(toolchain, str)
        or not re.fullmatch(r"[A-Za-z0-9_.+-]+", toolchain)
        or not isinstance(target, str)
        or not re.fullmatch(r"[A-Za-z0-9_.+-]+", target)
        or not isinstance(features, list)
        or not all(isinstance(feature, str) and re.fullmatch(r"[A-Za-z0-9_+-]+", feature) for feature in features)
    ):
        raise InvalidRecordInput("manifest Kani toolchain, target, or features are malformed")

    base = [cargo, f"+{toolchain}", "kani"]
    try:
        version_result = subprocess.run(
            [*base, "--version"], cwd=crate_root, capture_output=True, text=True,
            timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InvalidRecordInput(f"cannot query the configured Kani version: {exc}") from exc
    version_output = (version_result.stdout or "") + (version_result.stderr or "")
    if version_result.returncode != 0 or version_output.strip() != KANI_VERSION:
        raise InvalidRecordInput(
            f"Kani intake supports only {KANI_VERSION}; configured toolchain reported "
            f"{version_output.strip()!r}"
        )

    rustc = shutil.which("rustc")
    if rustc is None:
        raise InvalidRecordInput("rustc is unavailable; Ligature cannot check Kani's target host")
    try:
        rustc_result = subprocess.run(
            [rustc, f"+{toolchain}", "-vV"], cwd=crate_root,
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InvalidRecordInput(f"cannot inspect the manifest Rust toolchain: {exc}") from exc
    if rustc_result.returncode != 0:
        raise InvalidRecordInput(
            f"cannot inspect Rust toolchain {toolchain!r}: "
            f"{(rustc_result.stderr or rustc_result.stdout).strip()}"
        )
    host_match = re.search(r"(?m)^host: (\S+)$", rustc_result.stdout or "")
    if host_match is None or host_match.group(1) != target:
        observed_host = host_match.group(1) if host_match else "unknown"
        raise InvalidRecordInput(
            f"Kani 0.67 checks the Rust toolchain host, not a separate --target; "
            f"manifest target {target!r} does not match toolchain host {observed_host!r}"
        )

    metadata_command = [
        cargo, f"+{toolchain}", "metadata", "--format-version", "1", "--locked",
        "--manifest-path", str(crate_root / "Cargo.toml"),
    ]
    if "default" not in features:
        metadata_command.append("--no-default-features")
    explicit_features = [feature for feature in features if feature != "default"]
    if explicit_features:
        metadata_command.extend(["--features", ",".join(explicit_features)])
    try:
        metadata_result = subprocess.run(
            metadata_command, cwd=crate_root, capture_output=True, text=True,
            timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InvalidRecordInput(f"cannot validate locked Cargo inputs for Kani: {exc}") from exc
    if metadata_result.returncode != 0:
        detail = (metadata_result.stderr or metadata_result.stdout or "").strip()
        raise InvalidRecordInput(
            "Cargo metadata with --locked failed before Kani could run; resolve the manifest "
            f"and lockfile first ({detail})"
        )

    command = [
        *base,
        "-Z", "unstable-options",
        "--manifest-path", str(crate_root / "Cargo.toml"),
        "--harness", harness,
        "--exact",
        "--output-format", "regular",
        "--output-into-files",
    ]
    if "default" not in features:
        command.append("--no-default-features")
    explicit_features = [feature for feature in features if feature != "default"]
    if explicit_features:
        command.extend(["--features", ",".join(explicit_features)])
    with tempfile.TemporaryDirectory(prefix="ligature-kani-067-") as target_dir:
        command.extend(["--target-dir", target_dir])
        try:
            result = subprocess.run(
                command,
                cwd=crate_root,
                capture_output=True,
                text=True,
                timeout=KANI_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise EvidenceNotEstablished(
                f"Kani {KANI_VERSION} timed out after {KANI_TIMEOUT_SECONDS}s for "
                f"{harness!r}; no complete proof result was produced"
            ) from exc
        except OSError as exc:
            raise InvalidRecordInput(f"cannot invoke Kani for {harness!r}: {exc}") from exc
        output = (result.stdout or "") + (result.stderr or "")
        result_path = Path(target_dir) / "result_output_dir" / harness
        if result_path.is_symlink() or not result_path.is_file() or not _contains(Path(target_dir), result_path):
            if result.returncode != 0:
                raise EvidenceNotEstablished(
                    f"Kani did not produce a per-harness result file for {harness!r}"
                )
            raise InvalidRecordInput(
                f"Kani 0.67 did not write the expected per-harness result file "
                f"result_output_dir/{harness}"
            )
        try:
            result_text = result_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise InvalidRecordInput(
                f"Kani per-harness result {result_path.name!r} is not readable UTF-8: {exc}"
            ) from exc
    return parse_kani_067_result(result_text, output, result.returncode, harness)


def read_kani_result(
    workspace: Path,
    crate_root: Path,
    manifest: dict,
    obligation_id: str,
    harness: str,
    expected_source_hash: str | None = None,
    expected_harness_hash: str | None = None,
) -> Certificate:
    """Rerun Kani 0.67 and bind its complete result to current Rust sources."""
    source_hash, _mtime, _newest = rust_source_snapshot(workspace, manifest)
    harness_path, harness_hash = kani_harness_source(workspace, crate_root, manifest, harness)
    if expected_harness_hash is not None and harness_hash != expected_harness_hash:
        raise EvidenceNotEstablished(
            f"Kani harness {harness!r} changed after assurance was recorded for "
            f"{obligation_id}; re-run Kani and record assurance again"
        )
    if expected_source_hash is not None and source_hash != expected_source_hash:
        raise EvidenceNotEstablished(
            f"Rust source snapshot changed after Kani assurance was recorded for "
            f"{obligation_id}; re-run Kani and record assurance again"
        )
    checks = run_kani_067(crate_root, manifest, harness)
    final_source_hash, _mtime, _newest = rust_source_snapshot(workspace, manifest)
    _final_harness_path, final_harness_hash = kani_harness_source(
        workspace, crate_root, manifest, harness
    )
    if final_source_hash != source_hash or final_harness_hash != harness_hash:
        raise EvidenceNotEstablished(
            f"Rust source or Kani harness changed while verifying {obligation_id}; "
            "rerun Kani and record assurance again"
        )
    return Certificate(
        path=harness_path,
        relative=kani_target(workspace, crate_root, harness),
        proved=checks,
        goal_count=checks,
        evidence_format="kani-0.67-run",
        verifier_version=KANI_VERSION,
        source_hash=source_hash,
        harness_hash=harness_hash,
    )


def read_certificate(path: Path, workspace: Path, obligation_id: str) -> Certificate:
    """Read one certificate and decide what it establishes.

    Three refusals, in order: the certificate is stale (the Coma program
    it certifies was regenerated after it), it is not readable as a
    why3find certificate at all, or it is readable and does NOT establish
    the obligation (a stuck subgoal, or no proved goal). Nothing here ever
    returns a partially-proved certificate as if it were a complete one."""
    relative = _relative(path, workspace)
    coma = path.parent.parent / (path.parent.name + COMA_SUFFIX)
    if coma.is_file() and coma.stat().st_mtime > path.stat().st_mtime:
        raise EvidenceNotEstablished(
            f"{relative} is older than the Coma program it certifies "
            f"({_relative(coma, workspace)}) -- the certificate does not describe what is on "
            "disk; re-run the verifier so the certificate is regenerated"
        )
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise InvalidRecordInput(f"{relative} is not readable JSON: {e}") from e
    proofs = data.get("proofs") if isinstance(data, dict) else None
    if not isinstance(proofs, dict) or not proofs:
        raise InvalidRecordInput(
            f"{relative} carries no `proofs` -- this is not a why3find proof certificate"
        )

    counter = {"proved": 0, "goals": 0}
    stuck: list[str] = []

    def walk(node: object, goal: str) -> None:
        if node is None:
            # why3find stores an incomplete/stuck subgoal as null: "the
            # goal is marked stuck and all its parent goals are marked
            # incomplete" (why3find README, proof-search failure).
            stuck.append(goal)
            counter["goals"] += 1
            return
        if not isinstance(node, dict):
            raise InvalidRecordInput(
                f"{relative}: goal {goal} has an unrecognized certificate node "
                f"({type(node).__name__}) -- this reader refuses a shape it cannot vouch for"
            )
        if isinstance(node.get("prover"), str):
            counter["proved"] += 1
            counter["goals"] += 1
            return
        children = node.get("children")
        if not isinstance(children, list):
            raise InvalidRecordInput(
                f"{relative}: goal {goal} has neither a prover result nor tactic children -- "
                "this reader refuses a certificate shape it cannot vouch for"
            )
        # A tactic node is not a terminal outcome: its children are, so it
        # is not counted -- `goals` is the number of terminals, proved or
        # stuck, never a sum double-counting the tree's shape.
        for index, child in enumerate(children):
            walk(child, f"{goal}#{index}")

    for module, goals in proofs.items():
        if not isinstance(goals, dict):
            raise InvalidRecordInput(
                f"{relative}: section {module!r} is not a goal map -- this reader refuses a "
                "certificate shape it cannot vouch for"
            )
        for goal, node in goals.items():
            walk(node, f"{module}.{goal}")

    goal_count = counter["goals"]
    if stuck:
        shown = ", ".join(stuck[:5])
        more = f" (+{len(stuck) - 5} more)" if len(stuck) > 5 else ""
        raise EvidenceNotEstablished(
            f"{relative} does not establish {obligation_id}: {len(stuck)} of {goal_count} "
            f"subgoal(s) are stuck/incomplete -- {shown}{more}. Re-run the verifier; a "
            "certificate with an incomplete subgoal is not an achieved record"
        )
    if counter["proved"] == 0:
        raise EvidenceNotEstablished(
            f"{relative} proves no goal at all, so it establishes nothing for {obligation_id}"
        )
    return Certificate(
        path=path, relative=relative, proved=counter["proved"], goal_count=goal_count
    )


def read_verus_function_result(
    path: Path,
    workspace: Path,
    obligation_id: str,
    function: str,
    source_hash: str | None = None,
) -> Certificate:
    """Read Verus's official JSON and bind one obligation to one function.

    The top-level result must describe a successful whole-crate run. The
    per-function entry must contain the obligation's exact Ligature
    ``proof_note`` in ``obligation_proof_notes`` and must not list it among
    ``failed_proof_notes``. Verus's documented ``func-details`` format
    exposes proof notes per function; the note is the stable bridge between
    a Ligature guarantee and the specific Verus proof obligation.
    """
    relative = _relative(path, workspace)
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidRecordInput(
            f"{relative} is not a readable Verus `--output-json` result: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise InvalidRecordInput(f"{relative} is not a Verus JSON object")

    verification = data.get("verification-results")
    if not isinstance(verification, dict):
        raise InvalidRecordInput(
            f"{relative} has no official `verification-results` object; expected Verus "
            "`--output-json` output"
        )
    if verification.get("is-verifying-entire-crate") is not True:
        raise EvidenceNotEstablished(
            f"{relative} is not a whole-crate Verus run -- a function-filtered or partial "
            "result cannot establish a work-package guarantee"
        )
    if verification.get("success") is not True:
        raise EvidenceNotEstablished(
            f"{relative} does not report whole-crate Verus success "
            f"(success={verification.get('success')!r}, errors={verification.get('errors')!r})"
        )
    errors = verification.get("errors")
    verified = verification.get("verified")
    if (
        type(errors) is not int
        or type(verified) is not int
        or verified < 0
    ):
        raise InvalidRecordInput(
            f"{relative} has malformed whole-crate verification counts"
        )
    if errors != 0:
        raise EvidenceNotEstablished(
            f"{relative} reports Verus errors={errors}; a successful whole-crate result "
            "must have zero errors"
        )
    build = data.get("verus-build")
    version = build.get("version") if isinstance(build, dict) else None
    if not isinstance(version, str) or not version:
        version = data.get("verus-version") or data.get("version")
    if not isinstance(version, str) or not version:
        times = data.get("times-ms")
        timing_build = times.get("verus-build") if isinstance(times, dict) else None
        version = timing_build.get("version") if isinstance(timing_build, dict) else None
    if not isinstance(version, str) or not version:
        version = None

    function_details = data.get("func-details")
    if not isinstance(function_details, dict):
        raise InvalidRecordInput(
            f"{relative} has no official `func-details` function map"
        )
    details = function_details.get(function)
    if not isinstance(details, dict):
        raise EvidenceNotEstablished(
            f"{relative} has no per-function result for {function!r}; use the exact function "
            "key printed in Verus's `func-details` JSON"
        )
    proved_notes = details.get("obligation_proof_notes")
    failed_notes = details.get("failed_proof_notes")
    if not isinstance(proved_notes, list) or not all(isinstance(note, str) for note in proved_notes):
        raise InvalidRecordInput(
            f"{relative} has malformed `obligation_proof_notes` for {function!r}"
        )
    if not isinstance(failed_notes, list) or not all(isinstance(note, str) for note in failed_notes):
        raise InvalidRecordInput(
            f"{relative} has malformed `failed_proof_notes` for {function!r}"
        )

    required_note = f"{VERUS_NOTE_PREFIX}{obligation_id}"
    if required_note in failed_notes:
        raise EvidenceNotEstablished(
            f"{relative} reports {required_note!r} as a failed proof note in {function!r}"
        )
    if required_note not in proved_notes:
        raise EvidenceNotEstablished(
            f"{relative} does not bind {function!r} to {obligation_id}: expected the exact "
            f"Verus proof note {required_note!r} in `obligation_proof_notes`"
        )
    target = verus_target(workspace, path, function)
    return Certificate(
        path=path,
        relative=target,
        proved=1,
        goal_count=1,
        evidence_format="verus-json",
        content_hash="sha256:" + hashlib.sha256(raw).hexdigest(),
        verifier_version=version,
        source_hash=source_hash,
    )


def read_proof_target(
    workspace: Path,
    target: str,
    obligation_id: str,
    expected_verus_hash: str | None = None,
    expected_harness: str | None = None,
    expected_verus_version: str | None = None,
    expected_verus_source_hash: str | None = None,
    expected_kani_source_hash: str | None = None,
    expected_kani_harness_hash: str | None = None,
    expected_kani_version: str | None = None,
    source_manifest: dict | None = None,
) -> Certificate:
    """Re-open one stored target, dispatching by its explicit evidence kind."""
    if target.startswith(VERUS_TARGET_PREFIX):
        if expected_harness not in (None, "verus"):
            raise InvalidRecordInput(
                f"{target!r} is Verus JSON evidence but the achieved record names "
                f"harness {expected_harness!r}"
            )
        path_spec, function = parse_verus_target(target)
        path = verus_json_path_for(workspace, path_spec)
        if source_manifest is None:
            raise InvalidRecordInput(
                f"{target!r} has no manifest source scope; G14 cannot confirm the Verus result "
                "still matches the Rust sources"
            )
        source_snapshot = rust_source_snapshot(workspace, source_manifest)
        require_fresh_verus_json(path, workspace, source_snapshot)
        certificate = read_verus_function_result(
            path, workspace, obligation_id, function, source_hash=source_snapshot[0]
        )
        if expected_verus_hash is None:
            raise InvalidRecordInput(
                f"{target!r} has no recorded Verus output hash; the evidence cannot be matched "
                "to the result G14 re-opened"
            )
        if expected_verus_hash is not None and certificate.content_hash != expected_verus_hash:
            raise EvidenceNotEstablished(
                f"{_relative(path, workspace)} changed after the Verus evidence was recorded "
                f"for {obligation_id} -- expected {expected_verus_hash}, found "
                f"{certificate.content_hash}; re-run Verus and record assurance again"
            )
        if expected_verus_version is not None and certificate.verifier_version != expected_verus_version:
            raise EvidenceNotEstablished(
                f"{_relative(path, workspace)} reports Verus version "
                f"{certificate.verifier_version!r}, not the recorded "
                f"{expected_verus_version!r}"
            )
        if expected_verus_source_hash is None:
            raise InvalidRecordInput(
                f"{target!r} has no recorded Verus source hash; G14 cannot confirm the result "
                "still matches the Rust sources"
            )
        if certificate.source_hash != expected_verus_source_hash:
            raise EvidenceNotEstablished(
                f"Rust source files covered by the manifest changed after Verus evidence was "
                f"recorded for {obligation_id} -- expected {expected_verus_source_hash}, found "
                f"{certificate.source_hash}; re-run Verus and record assurance again"
            )
        return certificate
    if target.startswith(KANI_TARGET_PREFIX):
        if not isinstance(expected_harness, str) or not expected_harness.startswith("kani_"):
            raise InvalidRecordInput(
                f"{target!r} is Kani-run evidence but the achieved record does not name a "
                "manifest Kani harness"
            )
        crate_spec, harness = parse_kani_target(target)
        if harness != expected_harness:
            raise EvidenceNotEstablished(
                f"Kani target harness {harness!r} does not match the manifest harness "
                f"{expected_harness!r} for {obligation_id}"
            )
        if source_manifest is None:
            raise InvalidRecordInput(
                f"{target!r} has no manifest source scope; G14 cannot rerun Kani against the "
                "current work-package inputs"
            )
        if expected_kani_source_hash is None or expected_kani_harness_hash is None:
            raise InvalidRecordInput(
                f"{target!r} has no recorded Rust-source and harness hashes; G14 cannot bind "
                "the Kani rerun to the evidence that was recorded"
            )
        if expected_kani_version != KANI_VERSION:
            raise InvalidRecordInput(
                f"{target!r} does not record the supported Kani version {KANI_VERSION!r}"
            )
        crate_root = kani_crate_root(workspace, crate_spec)
        return read_kani_result(
            workspace,
            crate_root,
            source_manifest,
            obligation_id,
            harness,
            expected_source_hash=expected_kani_source_hash,
            expected_harness_hash=expected_kani_harness_hash,
        )
    if expected_harness is not None and verifier_for_manifest_harness(expected_harness) == "kani":
        raise InvalidRecordInput(
            f"{target!r} is not a Kani rerun target but the achieved record names Kani harness "
            f"{expected_harness!r}"
        )
    if expected_harness == "verus":
        raise InvalidRecordInput(
            f"{target!r} is a why3find certificate but the achieved record names harness 'verus'"
        )
    if expected_verus_hash is not None:
        raise InvalidRecordInput(
            "a Verus output hash was recorded for a non-Verus proof target"
        )
    if expected_verus_source_hash is not None:
        raise InvalidRecordInput("a Verus source hash was recorded for a non-Verus proof target")
    if any(value is not None for value in (expected_kani_source_hash, expected_kani_harness_hash, expected_kani_version)):
        raise InvalidRecordInput("Kani source or harness scope was recorded for non-Kani evidence")
    path = certificate_path_for(workspace, target, obligation_id)
    return read_certificate(path, workspace, obligation_id)


def _record_for(
    guarantee: dict, certificates: list[Certificate], provenance: dict
) -> tuple[dict, str]:
    """The achieved record for one provided guarantee, every field derived
    (see this module's docstring), with `config` taken from the manifest's
    own provenance block: a work package declares how it was built there,
    and a record's config must describe the run that produced the proof.

    Raises InvalidRecordInput when the manifest's own declaration says
    such a record could never satisfy it (a harness with no reader, an
    evidence kind the guarantee does not accept, a claim it does not
    require)."""
    obligation_id = guarantee["obligation_id"]
    harness = guarantee["harness"]
    verifier = verifier_for_manifest_harness(harness)
    required = guarantee["required_assurance"]
    if verifier not in SUPPORTED_HARNESSES:
        raise InvalidRecordInput(
            f"{obligation_id}: harness {harness!r} has no proof-certificate reader or Kani runner here "
            "(supported verifiers: creusot, verus, and exact `kani_...` harnesses) -- "
            "an evidence kind must be derived from evidence actually checked, never asserted "
            "from a name"
        )
    if verifier == "kani" and not harness.startswith("kani_"):
        raise InvalidRecordInput(
            f"{obligation_id}: harness {harness!r} has no proof-certificate reader or exact "
            "named Kani runner; Kani intake requires the manifest's `kani_...` function name"
        )
    expected_format = {
        "creusot": "why3find",
        "verus": "verus-json",
        "kani": "kani-0.67-run",
    }[verifier]
    if not certificates or any(certificate.evidence_format != expected_format for certificate in certificates):
        raise InvalidRecordInput(
            f"{obligation_id}: manifest harness {harness!r} requires {expected_format} evidence; "
            "the supplied proof input uses another verifier format"
        )
    evidence_kind = EVIDENCE_KIND_BY_VERIFIER[verifier]
    accepted = required.get("accepted_evidence_kinds") or []
    if evidence_kind not in accepted:
        raise InvalidRecordInput(
            f"{obligation_id}: required_assurance.accepted_evidence_kinds {accepted!r} does not "
            f"accept {evidence_kind!r}, which is what this certificate is -- recording it would "
            "produce a record satisfies() rejects"
        )
    required_claims = required.get("required_claims") or []
    claim_kind = CLAIM_KIND
    if verifier == "kani":
        if len(required_claims) != 1 or required_claims[0] not in (
            "callee-precondition-established", "postcondition-holds"
        ):
            raise InvalidRecordInput(
                f"{obligation_id}: Kani intake needs exactly one supported manifest claim; "
                f"found {required_claims!r}"
            )
        claim_kind = required_claims[0]
    elif CLAIM_KIND not in required_claims:
        raise InvalidRecordInput(
            f"{obligation_id}: required_assurance.required_claims {required_claims!r} does not "
            f"require {CLAIM_KIND!r}, so a proven postcondition is not what this guarantee asks "
            "for"
        )
    scope = {"proof_targets": ",".join(sorted({c.relative for c in certificates}))}
    if verifier == "verus":
        hashes = {certificate.content_hash for certificate in certificates}
        source_hashes = {certificate.source_hash for certificate in certificates}
        versions = {
            certificate.verifier_version
            for certificate in certificates
            if certificate.verifier_version
        }
        if len(hashes) != 1 or None in hashes:
            raise InvalidRecordInput(
                f"{obligation_id}: Verus functions must resolve from one unchanged JSON result file"
            )
        if len(source_hashes) != 1 or None in source_hashes:
            raise InvalidRecordInput(
                f"{obligation_id}: Verus source inputs could not be bound to the result"
            )
        scope["verus_output_sha256"] = next(iter(hashes))
        scope["verus_source_sha256"] = next(iter(source_hashes))
        if len(versions) == 1:
            scope["verus_version"] = next(iter(versions))
    elif verifier == "kani":
        if len(certificates) != 1:
            raise InvalidRecordInput(
                f"{obligation_id}: Kani records bind one manifest obligation to exactly one "
                "named harness per invocation"
            )
        certificate = certificates[0]
        if not certificate.source_hash or not certificate.harness_hash:
            raise InvalidRecordInput(
                f"{obligation_id}: Kani source and harness hashes are required"
            )
        scope["input_domain"] = f"the bounded model encoded by Kani harness {harness}"
        scope["kani_source_sha256"] = certificate.source_hash
        scope["kani_harness_sha256"] = certificate.harness_hash
        scope["kani_version"] = certificate.verifier_version or KANI_VERSION
    record = {
        "schema_version": "1.0",
        "claim": {"kind": claim_kind, "result": "pass"},
        "evidence": {
            "kind": evidence_kind,
            "verifier": verifier,
            "harness": harness,
            "scope": scope,
        },
        "trust": {"assumptions": []},
        "support": {"status": "supported"},
        "config": {
            "toolchain": provenance["toolchain"],
            "target": provenance["target"],
            "features": list(provenance["features"]),
        },
    }
    return record, evidence_kind


def _empty_report(work_package: str) -> dict:
    return {
        "schema_version": "1.0",
        "work_package": work_package,
        "obligation_records": [],
        "bridge_records": [],
    }


def _existing_report(
    workspace: Path, report_path: Path, work_package: str
) -> tuple[dict, str]:
    """What the manifest's report.emit path currently holds, classified:
    absent, this work package's own assurance report (updated in place),
    a generated feature ledger (replaced, with the caller warned), or
    content this command must not destroy (refused)."""
    relative = _relative(report_path, workspace)
    if not report_path.exists():
        return _empty_report(work_package), "absent"
    if not report_path.is_file():
        raise InvalidRecordInput(f"report.emit path is not a file: {relative}")
    try:
        data = json.loads(report_path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise InvalidRecordInput(
            f"report.emit path ({relative}) holds unreadable JSON: {e} -- refusing to replace a "
            "file this command cannot describe"
        ) from e
    if not list(load_assurance_report_validator().iter_errors(data)):
        if data["work_package"] != work_package:
            raise InvalidRecordInput(
                f"report.emit path ({relative}) holds an assurance report for work package "
                f"{data['work_package']!r}, not {work_package!r} -- one path cannot answer for "
                "two work packages"
            )
        return data, "assurance-report"
    if not list(load_feature_ledger_validator().iter_errors(data)):
        return _empty_report(work_package), "feature-ledger"
    raise InvalidRecordInput(
        f"report.emit path ({relative}) holds content that is neither an assurance report nor a "
        "feature ledger -- refusing to replace it; move the manifest's report.emit to a free "
        "ci/results/*.json path"
    )


def _bridge_records_for(workspace: Path, manifest: dict, base: dict) -> list[dict]:
    """`bridge_records` for the assembled report: whatever the existing
    report already carried (preserved, never re-derived from nothing),
    plus this manifest's own required preconditions for which
    `gate_g9.bridge_records_for()` has a check on record -- plan.md §8.3's
    honest direction, so the bridge half of the report keeps coming from
    the checks that ran rather than from this command's author."""
    entries = list(base.get("bridge_records") or [])
    seen = {entry.get("bridge_id") for entry in entries if isinstance(entry, dict)}
    required = manifest["definition_of_done"]["required_preconditions_to_establish"]
    if not required:
        return entries
    from gate_g9 import bridge_records_for  # local import: the report's bridge half only

    available = {entry["bridge_id"]: entry for entry in bridge_records_for(workspace)}
    for entry in required:
        bridge_id = entry["bridge_id"]
        if bridge_id in seen or bridge_id not in available:
            continue
        entries.append({"bridge_id": bridge_id, "record": available[bridge_id]["record"]})
        seen.add(bridge_id)
    return entries


def record_assurance(
    workspace: Path,
    target: str,
    proof_specs: list[str],
    verus_json: str | None = None,
    verus_function_specs: list[str] | None = None,
    kani_harness_specs: list[str] | None = None,
    kani_crate: str | None = None,
) -> RecordOutcome:
    """Assemble and write one work package's assurance report.

    Every refusal happens BEFORE the write: a failure means no mutation,
    the same guarantee atomic_write gives the write itself."""
    if not workspace.is_dir():
        raise InvalidRecordInput(
            f"workspace does not exist or is not a directory: {workspace}"
        )
    workspace = workspace.resolve()
    work_package, manifest, _manifest_path = resolve_manifest(workspace, target)
    declared = parse_proof_specs(proof_specs) if proof_specs else {}
    verus_functions = parse_verus_function_specs(verus_function_specs or [])
    kani_harnesses = parse_kani_harness_specs(kani_harness_specs or [])
    if not declared and not verus_functions and not kani_harnesses:
        raise InvalidRecordInput(
            "no proof evidence given -- pass --proof for Creusot, --verus-json with "
            "--verus-function for Verus, or --kani-crate with --kani-harness for Kani"
        )
    if verus_functions and not verus_json:
        raise InvalidRecordInput("--verus-function requires --verus-json <output.json>")
    if verus_json and not verus_functions:
        raise InvalidRecordInput("--verus-json requires at least one --verus-function mapping")
    if kani_harnesses and not kani_crate:
        raise InvalidRecordInput("--kani-harness requires --kani-crate <package-directory>")
    if kani_crate and not kani_harnesses:
        raise InvalidRecordInput("--kani-crate requires at least one --kani-harness mapping")
    evidence_maps = [set(declared), set(verus_functions), set(kani_harnesses)]
    overlaps = sorted(
        obligation_id
        for index, mapping in enumerate(evidence_maps)
        for other in evidence_maps[index + 1:]
        for obligation_id in mapping & other
    )
    if overlaps:
        raise InvalidRecordInput(
            f"obligations {overlaps!r} have evidence declared for multiple verifier paths"
        )
    verus_path = verus_json_path_for(workspace, verus_json) if verus_json else None
    verus_sources = None
    if verus_path is not None:
        verus_sources = rust_source_snapshot(workspace, manifest)
        require_fresh_verus_json(verus_path, workspace, verus_sources)

    guarantees = manifest["definition_of_done"]["provided_guarantees"]
    provided = {entry["obligation_id"]: entry for entry in guarantees}
    if len(provided) != len(guarantees):
        raise InvalidRecordInput(
            f"{work_package} declares the same obligation twice -- which body meets the contract "
            "would depend on file order"
        )
    unknown = sorted((set(declared) | set(verus_functions) | set(kani_harnesses)) - set(provided))
    if unknown:
        raise InvalidRecordInput(
            f"{', '.join(unknown)} is not a provided guarantee of {work_package} (it provides "
            f"{sorted(provided)}) -- only the work package that provides an obligation records it"
        )

    kani_root = kani_crate_root(workspace, kani_crate) if kani_crate else None
    for obligation_id, mapped_harness in kani_harnesses.items():
        declared_harness = provided[obligation_id]["harness"]
        if (
            verifier_for_manifest_harness(declared_harness) != "kani"
            or not declared_harness.startswith("kani_")
        ):
            raise InvalidRecordInput(
                f"{obligation_id}: manifest harness {declared_harness!r} is not a supported "
                "Kani harness; --kani-harness only maps manifest `kani_...` names"
            )
        if mapped_harness != declared_harness:
            raise InvalidRecordInput(
                f"{obligation_id}: --kani-harness names {mapped_harness!r}, but the manifest "
                f"binds this obligation to {declared_harness!r}"
            )

    provenance = manifest["provenance"]
    # Record in the manifest's own obligation order, so a report's entries
    # read back the way the manifest declares them.
    order = {obligation_id: index for index, obligation_id in enumerate(provided)}
    certificates_by_obligation: dict[str, list[Certificate]] = {}
    records: dict[str, dict] = {}
    kinds: dict[str, str] = {}
    scope_warnings: dict[str, list[str]] = {}
    requested = set(declared) | set(verus_functions) | set(kani_harnesses)
    for obligation_id in sorted(requested, key=lambda obligation_id: order[obligation_id]):
        guarantee = provided[obligation_id]
        harness = guarantee["harness"]
        verifier = verifier_for_manifest_harness(harness)
        if verifier == "verus":
            if obligation_id in kani_harnesses:
                raise InvalidRecordInput(
                    f"{obligation_id}: manifest harness is Verus; use --verus-json with "
                    "--verus-function instead of --kani-harness"
                )
            if obligation_id in declared:
                raise InvalidRecordInput(
                    f"{obligation_id}: harness is verus; use --verus-json with "
                    "--verus-function instead of --proof"
                )
            if verus_path is None:
                raise InvalidRecordInput(
                    f"{obligation_id}: Verus evidence requires --verus-json <output.json>"
                )
            certificates = [
                read_verus_function_result(
                    verus_path,
                    workspace,
                    obligation_id,
                    function,
                    source_hash=verus_sources[0] if verus_sources is not None else None,
                )
                for function in verus_functions[obligation_id]
            ]
        elif verifier == "kani" and harness.startswith("kani_"):
            if obligation_id in declared:
                raise InvalidRecordInput(
                    f"{obligation_id}: manifest names Kani harness {harness!r}; use "
                    "--kani-crate with --kani-harness instead of --proof"
                )
            if obligation_id in verus_functions:
                raise InvalidRecordInput(
                    f"{obligation_id}: --verus-function can only map Verus guarantees"
                )
            if kani_root is None:
                raise InvalidRecordInput(
                    f"{obligation_id}: Kani evidence requires --kani-crate <package-directory>"
                )
            certificates = [
                read_kani_result(
                    workspace,
                    kani_root,
                    manifest,
                    obligation_id,
                    kani_harnesses[obligation_id],
                )
            ]
        else:
            if obligation_id in kani_harnesses:
                raise InvalidRecordInput(
                    f"{obligation_id}: manifest harness is {harness!r}; "
                    "--kani-harness can only map Kani guarantees"
                )
            if obligation_id in verus_functions:
                raise InvalidRecordInput(
                    f"{obligation_id}: harness is {harness!r}; "
                    "--verus-function can only map Verus guarantees"
                )
            certificates = [
                read_certificate(
                    certificate_path_for(workspace, spec, obligation_id), workspace, obligation_id
                )
                for spec in declared[obligation_id]
            ]
        record, evidence_kind = _record_for(guarantee, certificates, provenance)
        result = satisfies(guarantee["required_assurance"], record)
        if not result:
            if verifier == "kani" and all(
                reason.startswith("minimum_scope[") for reason in result.reasons
            ):
                # Preserve the real, narrower bounded result. G14 will read
                # this record and report the unchanged manifest's scope
                # mismatch; recording never upgrades that result to closure.
                scope_warnings[obligation_id] = list(result.reasons)
            else:
                raise EvidenceNotEstablished(
                    f"{obligation_id}: the record this verifier evidence yields would not satisfy the "
                    "manifest's own required_assurance -- "
                    + "; ".join(result.reasons)
                    + ". A requirement this reader cannot honestly state (for instance a "
                    "minimum_scope it would have to copy out of the requirement) is refused, not "
                    "guessed"
                )
        certificates_by_obligation[obligation_id] = certificates
        records[obligation_id] = record
        kinds[obligation_id] = evidence_kind

    report_path = (workspace / manifest["report"]["emit"]).resolve()
    if not _contains(workspace, report_path):
        raise InvalidRecordInput(
            f"manifest's report.emit ({manifest['report']['emit']}) resolves outside the "
            "workspace"
        )
    base, replaced = _existing_report(workspace, report_path, work_package)

    preserved = [
        entry for entry in base["obligation_records"] if entry["obligation_id"] not in records
    ]
    obligation_records = preserved + [
        {"obligation_id": obligation_id, "record": records[obligation_id]}
        for obligation_id in provided
        if obligation_id in records
    ]
    bridge_records = _bridge_records_for(workspace, manifest, base)

    report = {
        "schema_version": "1.0",
        "work_package": work_package,
        "obligation_records": obligation_records,
        "bridge_records": bridge_records,
    }
    # Validate what is about to be written, both halves, and refuse rather
    # than emit a report gate-g14 would fail closed on.
    errors = list(load_assurance_report_validator().iter_errors(report))
    if errors:
        raise EvidenceNotEstablished(
            f"assembled assurance report is not schema-valid: {errors[0].message}"
        )
    record_errors = [
        error
        for entry in report["obligation_records"]
        for error in load_achieved_validator().iter_errors(entry["record"])
    ]
    if record_errors:
        raise EvidenceNotEstablished(
            f"assembled achieved record is not schema-valid: {record_errors[0].message}"
        )

    if verus_path is not None and verus_sources is not None:
        final_sources = rust_source_snapshot(workspace, manifest)
        require_fresh_verus_json(verus_path, workspace, final_sources)
        if final_sources[0] != verus_sources[0]:
            raise EvidenceNotEstablished(
                "Rust source files changed while assurance was being assembled -- re-run Verus "
                "and record assurance again"
            )
        try:
            final_output_hash = "sha256:" + hashlib.sha256(verus_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise InvalidRecordInput(
                f"cannot re-read Verus JSON result {_relative(verus_path, workspace)}: {exc}"
            ) from exc
        recorded_output_hashes = {
            certificate.content_hash
            for certificates in certificates_by_obligation.values()
            for certificate in certificates
        }
        if recorded_output_hashes != {final_output_hash}:
            raise EvidenceNotEstablished(
                "Verus JSON changed while assurance was being assembled -- re-run Verus and "
                "record assurance again"
            )

    kani_certificates = [
        certificate
        for certificates in certificates_by_obligation.values()
        for certificate in certificates
        if certificate.evidence_format == "kani-0.67-run"
    ]
    if kani_certificates:
        final_source_hash, _mtime, _newest = rust_source_snapshot(workspace, manifest)
        for obligation_id, certificates in certificates_by_obligation.items():
            for certificate in certificates:
                if certificate.evidence_format != "kani-0.67-run":
                    continue
                _crate_spec, harness = parse_kani_target(certificate.relative)
                _path, final_harness_hash = kani_harness_source(
                    workspace, kani_root, manifest, harness
                )
                if certificate.source_hash != final_source_hash or certificate.harness_hash != final_harness_hash:
                    raise EvidenceNotEstablished(
                        f"Rust source or Kani harness changed while assurance was being assembled "
                        f"for {obligation_id}; rerun Kani and record assurance again"
                    )

    write_atomically(report_path, json.dumps(report, indent=2, sort_keys=True) + "\n")

    return RecordOutcome(
        work_package=work_package,
        report_path=report_path,
        report_relative=_relative(report_path, workspace),
        recorded=[
            RecordedObligation(
                obligation_id=obligation_id,
                certificates=certificates_by_obligation[obligation_id],
                evidence_kind=kinds[obligation_id],
            )
            for obligation_id in provided
            if obligation_id in records
        ],
        preserved_obligation_records=len(preserved),
        bridge_records=len(bridge_records),
        replaced=replaced,
        scope_warnings=scope_warnings,
    )
