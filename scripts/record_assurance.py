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
    `#[verifier::proof_note("ligature:<obligation>")]`. Verus must report
    whole-crate success, and the named function's JSON must contain that
    exact note in `obligation_proof_notes` but not `failed_proof_notes`.
    Aggregate verified/error counts alone never establish a guarantee.
  * `evidence.kind` / `evidence.verifier` / `evidence.harness` -- the
    manifest's own `definition_of_done.provided_guarantees[].harness`,
    through `bridge_harness.EVIDENCE_KIND_BY_VERIFIER`'s single mapping.
    `creusot` and `verus` have readers for their own formats; an evidence
    kind is derived from evidence actually read, never asserted from a
    harness name this command cannot follow.
  * `config` -- the manifest's provenance block (toolchain, target,
    features), which is where a work package already declares how it was
    built.
  * `trust.assumptions` -- always empty. No reader exists here for an
    assumption a proof relied on, and an empty list is the only answer
    that cannot claim one; satisfies() fails any assumption outside the
    required profile's allow-list.
  * `evidence.scope` -- the certificate paths themselves (what was
    actually checked). Deliberately NOT a copy of the requirement's
    `minimum_scope`: copying the required domain into the achievement
    would assert coverage the certificate never states. A provided
    guarantee that declares `minimum_scope` is therefore REFUSED, with
    satisfies()'s own reasons printed, rather than satisfied by copying.

The declaration a human does make is checked, not trusted. Creusot uses
`--proof Obligation=path`; Verus uses `--verus-function Obligation=function`
with one shared `--verus-json` result file:

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
    hashes and re-reads the same JSON result.

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
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from atomic_write import write_atomically  # noqa: E402
from bridge_harness import EVIDENCE_KIND_BY_VERIFIER  # noqa: E402
from gate_g14 import load_assurance_report_validator  # noqa: E402
from gate_g14 import looks_like_work_package_manifest  # noqa: E402
from gate_g14 import work_package_manifest_dir_for  # noqa: E402
from generate_feature_ledger import load_validator as load_feature_ledger_validator  # noqa: E402
from satisfies import load_achieved_validator  # noqa: E402
from satisfies import satisfies  # noqa: E402
from validate_work_package import load_validator as load_work_package_validator  # noqa: E402

PROOF_FILE_NAME = "proof.json"
COMA_SUFFIX = ".coma"

# A harness is supported only when this module has a reader for its actual
# output format. Verus uses its official `--output-json` result, never a
# why3find certificate; Kani remains unsupported here.
SUPPORTED_HARNESSES = ("creusot", "verus")
VERUS_TARGET_PREFIX = "verus-json:"
VERUS_NOTE_PREFIX = "ligature:"

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
    path: Path, workspace: Path, obligation_id: str, function: str
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
    )


def read_proof_target(
    workspace: Path,
    target: str,
    obligation_id: str,
    expected_verus_hash: str | None = None,
    expected_harness: str | None = None,
    expected_verus_version: str | None = None,
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
        certificate = read_verus_function_result(path, workspace, obligation_id, function)
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
        return certificate
    if expected_harness == "verus":
        raise InvalidRecordInput(
            f"{target!r} is a why3find certificate but the achieved record names harness 'verus'"
        )
    if expected_verus_hash is not None:
        raise InvalidRecordInput(
            "a Verus output hash was recorded for a non-Verus proof target"
        )
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
    required = guarantee["required_assurance"]
    if harness not in SUPPORTED_HARNESSES:
        raise InvalidRecordInput(
            f"{obligation_id}: harness {harness!r} has no proof-certificate reader here "
            f"(supported: {', '.join(SUPPORTED_HARNESSES)}) -- an evidence kind must be derived "
            "from evidence that was actually read, never asserted from a name"
        )
    expected_format = "verus-json" if harness == "verus" else "why3find"
    if not certificates or any(certificate.evidence_format != expected_format for certificate in certificates):
        raise InvalidRecordInput(
            f"{obligation_id}: harness {harness!r} requires {expected_format} evidence; "
            "the supplied proof input uses another verifier format"
        )
    evidence_kind = EVIDENCE_KIND_BY_VERIFIER[harness]
    accepted = required.get("accepted_evidence_kinds") or []
    if evidence_kind not in accepted:
        raise InvalidRecordInput(
            f"{obligation_id}: required_assurance.accepted_evidence_kinds {accepted!r} does not "
            f"accept {evidence_kind!r}, which is what this certificate is -- recording it would "
            "produce a record satisfies() rejects"
        )
    required_claims = required.get("required_claims") or []
    if CLAIM_KIND not in required_claims:
        raise InvalidRecordInput(
            f"{obligation_id}: required_assurance.required_claims {required_claims!r} does not "
            f"require {CLAIM_KIND!r}, so a proven postcondition is not what this guarantee asks "
            "for"
        )
    scope = {"proof_targets": ",".join(sorted({c.relative for c in certificates}))}
    if harness == "verus":
        hashes = {certificate.content_hash for certificate in certificates}
        versions = {
            certificate.verifier_version
            for certificate in certificates
            if certificate.verifier_version
        }
        if len(hashes) != 1 or None in hashes:
            raise InvalidRecordInput(
                f"{obligation_id}: Verus functions must resolve from one unchanged JSON result file"
            )
        scope["verus_output_sha256"] = next(iter(hashes))
        if len(versions) == 1:
            scope["verus_version"] = next(iter(versions))
    record = {
        "schema_version": "1.0",
        "claim": {"kind": CLAIM_KIND, "result": "pass"},
        "evidence": {
            "kind": evidence_kind,
            "verifier": harness,
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
    if not declared and not verus_functions:
        raise InvalidRecordInput(
            "no proof evidence given -- pass --proof for each Creusot obligation, or "
            "--verus-json with --verus-function for each Verus obligation"
        )
    if verus_functions and not verus_json:
        raise InvalidRecordInput("--verus-function requires --verus-json <output.json>")
    if verus_json and not verus_functions:
        raise InvalidRecordInput("--verus-json requires at least one --verus-function mapping")
    if set(declared) & set(verus_functions):
        overlap = sorted(set(declared) & set(verus_functions))
        raise InvalidRecordInput(
            f"obligations {overlap!r} have both --proof and --verus-function evidence"
        )
    verus_path = verus_json_path_for(workspace, verus_json) if verus_json else None

    guarantees = manifest["definition_of_done"]["provided_guarantees"]
    provided = {entry["obligation_id"]: entry for entry in guarantees}
    if len(provided) != len(guarantees):
        raise InvalidRecordInput(
            f"{work_package} declares the same obligation twice -- which body meets the contract "
            "would depend on file order"
        )
    unknown = sorted((set(declared) | set(verus_functions)) - set(provided))
    if unknown:
        raise InvalidRecordInput(
            f"{', '.join(unknown)} is not a provided guarantee of {work_package} (it provides "
            f"{sorted(provided)}) -- only the work package that provides an obligation records it"
        )

    provenance = manifest["provenance"]
    # Record in the manifest's own obligation order, so a report's entries
    # read back the way the manifest declares them.
    order = {obligation_id: index for index, obligation_id in enumerate(provided)}
    certificates_by_obligation: dict[str, list[Certificate]] = {}
    records: dict[str, dict] = {}
    kinds: dict[str, str] = {}
    requested = set(declared) | set(verus_functions)
    for obligation_id in sorted(requested, key=lambda obligation_id: order[obligation_id]):
        guarantee = provided[obligation_id]
        harness = guarantee["harness"]
        if harness == "verus":
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
                read_verus_function_result(verus_path, workspace, obligation_id, function)
                for function in verus_functions[obligation_id]
            ]
        else:
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
    )
