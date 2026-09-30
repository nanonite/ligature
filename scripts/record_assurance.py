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
  * `claim`, `evidence`, `support` -- read from the verifier's own proof
    certificate (why3find `proof.json`, the versioned "proof witness"
    `cargo creusot` leaves next to the Coma program it discharged). A goal
    counts as proved only when its certificate node carries a prover
    result; a `null` node is a stuck/incomplete subgoal -- why3find's own
    semantics are "the goal is marked stuck and all its parent goals are
    marked incomplete" -- so one anywhere means the certificate does not
    establish the obligation, and nothing is written.
  * `evidence.kind` / `evidence.verifier` / `evidence.harness` -- the
    manifest's own `definition_of_done.provided_guarantees[].harness`,
    through `bridge_harness.EVIDENCE_KIND_BY_VERIFIER`'s single mapping.
    Only `creusot` has a certificate reader here: an evidence kind is
    derived from evidence actually read, never asserted from a harness
    name this command cannot follow.
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

The declaration a human does make -- `--proof Obligation=path` -- is
checked, not trusted:

  * the obligation must be one this manifest actually provides (a record
    for anything else satisfies nothing and belongs to no report);
  * the certificate must sit under a directory named for the
    obligation's own concept (`Weekday.C1` -> a `weekday/` path segment,
    the same snake_case the `<Concept>.C<n>` id grammar encodes), so the
    mapping cannot point at an unrelated concept's proofs;
  * the certificate must not be older than the Coma program it
    certifies (`<dir>.coma`), because a certificate written before the
    program it claims to discharge says nothing about the file on disk.

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
does not establish the obligation (stuck goals, a stale certificate, a
record satisfies() would not accept -- the check ran and found a real
problem), 2 invalid input (a malformed `--proof`, a missing or misplaced
manifest, an evidence path that is not a proof certificate).
"""
from __future__ import annotations

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

# The only harness whose proof evidence this reader can follow. Not
# "every harness that maps to an evidence kind" (bridge_harness knows
# kani/verus too): recording a kani or verus result from a why3find
# certificate would be inventing evidence, which is the one thing this
# module exists not to do.
SUPPORTED_HARNESSES = ("creusot",)

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
    record = {
        "schema_version": "1.0",
        "claim": {"kind": CLAIM_KIND, "result": "pass"},
        "evidence": {
            "kind": evidence_kind,
            "verifier": harness,
            "harness": harness,
            "scope": {"proof_targets": ",".join(sorted({c.relative for c in certificates}))},
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


def record_assurance(workspace: Path, target: str, proof_specs: list[str]) -> RecordOutcome:
    """Assemble and write one work package's assurance report.

    Every refusal happens BEFORE the write: a failure means no mutation,
    the same guarantee atomic_write gives the write itself."""
    if not workspace.is_dir():
        raise InvalidRecordInput(
            f"workspace does not exist or is not a directory: {workspace}"
        )
    workspace = workspace.resolve()
    work_package, manifest, _manifest_path = resolve_manifest(workspace, target)
    declared = parse_proof_specs(proof_specs)

    guarantees = manifest["definition_of_done"]["provided_guarantees"]
    provided = {entry["obligation_id"]: entry for entry in guarantees}
    if len(provided) != len(guarantees):
        raise InvalidRecordInput(
            f"{work_package} declares the same obligation twice -- which body meets the contract "
            "would depend on file order"
        )
    unknown = sorted(set(declared) - set(provided))
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
    for obligation_id in sorted(declared, key=lambda obligation_id: order[obligation_id]):
        certificates = [
            read_certificate(
                certificate_path_for(workspace, spec, obligation_id), workspace, obligation_id
            )
            for spec in declared[obligation_id]
        ]
        guarantee = provided[obligation_id]
        record, evidence_kind = _record_for(guarantee, certificates, provenance)
        result = satisfies(guarantee["required_assurance"], record)
        if not result:
            raise EvidenceNotEstablished(
                f"{obligation_id}: the record this certificate yields would not satisfy the "
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
