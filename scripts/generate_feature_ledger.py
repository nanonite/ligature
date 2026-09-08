#!/usr/bin/env python3
"""Feature ledger generator (chainlink #34): ci/results/feature_ledger.json.

plan.md §16.4. A GENERATED, READ-ONLY projection -- same tier as the
Stage 4.5 contact sheet (§16.3): derived entirely from already-normative
or already-generated artifacts, never itself normative, never consulted
by satisfies()/assurance/closure computation, and never mutates a
witness spec or assurance record (§16.6's boundary: "not layer 3, not a
bridge, no effect on closure_kind").

The two-column discipline, never merged
------------------------------------------
The whole reason this file exists, repeated because merging the two
columns is the one failure mode it must prevent:

  * `implementation_observed` -- the WEAK liveness fact only: the body
    executed on its fixture and produced a stable, non-degenerate
    value. Usable as an orchestration signal (§15's advisory-first open
    item), never as a correctness claim.
  * `assurance_status` -- the CONTRACT is established, from the owning
    obligation's validated assurance record. A green witness proves the
    body runs on ONE fixture; it proves nothing about correctness,
    completeness over all inputs, or contract conformance.

`implementation_observed` requires genuinely valid, IDENTITY-MATCHING
witness/result data and clean G18/G19/G20 dispositions -- a stored
result is never trusted merely because its witness_id happens to match
(chainlink #30/#31's own hard-won lesson: an earlier version of G20
itself made exactly that mistake, reused here via the real gates rather
than re-derived).

Why assurance_status is always "unsupported" today
-------------------------------------------------------
A witnessed FEATURE is a concept-to-code QUERY (`<Concept>.<query>`, a
snake_case function name). An OBLIGATION this pipeline's own layer 3
tracks is a CONSTRAINT (`<Concept>.C\\d+`,
docs/achieved-assurance-schema.json's `obligation_id` pattern) -- a
different concept-to-code $def entirely (query vs. constraint), and
nothing in either schema, or anywhere else in this codebase, names a
field linking one to the other. Querying is a read accessor; an
obligation is a proof obligation over a constraint. There is currently
no mechanical way to resolve "the obligation THIS query's feature is
establishing assurance for."

Given that, and this issue's own rule ("missing or unresolved assurance
must remain unsupported; never silently upgrade it"), the honest
answer -- not a placeholder, the actual current fact about this
codebase -- is that every feature's assurance_status is `unsupported`
until a linking convention exists. `resolve_assurance_status()` below
is still written as a real resolution function, not a hardcoded
constant, so it becomes the one place a future convention plugs in;
today it always returns "unsupported" because it has nothing to
resolve against.

Reuses rather than duplicates
--------------------------------
Every discovery/identity/disposition rule here is read straight from
the function that already owns it, never re-derived: `gate_g18`'s
`collect_declared_features`/`collect_valid_witnesses`/`gate_workspace`
for the declared feature set and G18 coverage; `gate_g19`'s
`collect_valid_witness_entries`/`collect_valid_witness_specs` for
genuinely valid witness discovery (including cross-file witness_id
ambiguity) and its `check_witness_determinism` for a REAL regeneration
dispatch when a `witness_backend` is configured (factored out of
`gate_workspace()`'s own loop, chainlink #34, so a caller needing the
regenerated DOCUMENT itself -- not just a pass/fail finding -- can
reuse the identical checks); `gate_g20`'s `check_must_vary` and
`check_candidate_fixture_family_consistency` (chainlink #31's own
candidate-symmetric check, reused here for every declared feature, not
only a promotion candidate) for degeneracy, evaluated against the SAME
document `check_witness_determinism` just regenerated -- never a
separately-read, possibly stale on-disk result
(`load_results_by_witness`, which `gate_g20.gate_workspace()`'s own
standalone scan still legitimately uses for ITS OWN, differently-scoped
purpose); `validate_closure.load_cluster_artifacts` for closure
profiles; `gate_g14.load_manifests` + its own assurance-report
validator for workspace-wide assurance-report discovery;
`atomic_write.write_atomically` for the same "failure means no
mutation" I/O guarantee chainlink #28 already established.

An external review found two further gaps in an earlier version of
this reuse, both now fixed:

  * Feeding `gate_g20.check_must_vary` the SAME on-disk
    `load_results_by_witness()` results `gate_g20.gate_workspace()`
    itself uses meant a witness with NO stored result (or a STALE one
    left over from a different fixture/seed while a freshly regenerated
    result was actually constant) produced no G20 finding at all --
    read here as `degeneracy: ok` and, combined with a genuine G19
    pass, `implementation_observed: true`. Reproduced both ways.
    `degeneracy` is now derived from the identical document
    `check_witness_determinism` just regenerated (never `ok` unless
    that document exists and is clean; `not-checked` when it does not).

  * `gate_g20`'s own workspace-wide pairwise family-consistency scan
    only ever flags whichever witness sorts SECOND within a disagreeing
    family -- correct for a scan reporting SOME finding somewhere, but
    wrong for asking "is THIS declared feature clean": renaming a
    witness_id could change which of two conflicting family members
    read as green. `check_candidate_fixture_family_consistency` is now
    called once per declared feature against every OTHER genuinely
    valid witness, so every member of an inconsistent family gets its
    OWN, sort-order-independent disposition.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from atomic_write import write_atomically  # noqa: E402
from gate_g14 import load_assurance_report_validator  # noqa: E402
from gate_g14 import load_manifests  # noqa: E402
from gate_g18 import collect_declared_features  # noqa: E402
from gate_g18 import collect_valid_witnesses  # noqa: E402
from gate_g18 import gate_workspace as gate_g18_workspace  # noqa: E402
from gate_g19 import check_witness_determinism  # noqa: E402
from gate_g19 import collect_valid_witness_entries  # noqa: E402
from gate_g19 import collect_valid_witness_specs  # noqa: E402
from gate_g20 import check_candidate_fixture_family_consistency  # noqa: E402
from gate_g20 import check_must_vary  # noqa: E402
from schema_utils import make_validator  # noqa: E402
from validate_closure import load_cluster_artifacts  # noqa: E402

DOCS = Path(__file__).resolve().parent.parent / "docs"
SCHEMA_PATH = DOCS / "feature-ledger-schema.json"
LEDGER_RELATIVE_PATH = ("ci", "results", "feature_ledger.json")

EXIT_OK = 0
EXIT_GENERATION_FAILED = 1
EXIT_INPUT_ERROR = 2

NO_BACKEND_MARKER = "no witness_backend configured"


class GenerationError(Exception):
    pass


def ledger_path_for(workspace: Path) -> Path:
    return workspace.joinpath(*LEDGER_RELATIVE_PATH)


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def load_validator():
    return make_validator(load_schema())


def _canonical_json(obj) -> str:
    """canonical-json-v1 style (scripts/witness_result.py): sorted
    keys, compact separators, no trailing newline -- so the same
    logical content always serializes to the same bytes regardless of
    construction order."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _sha256_of(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash_documents(entries: list[tuple[str, dict]]) -> str:
    """entries: (relative-path-string, parsed-json-document) pairs,
    sorted by path before hashing -- the hash must never depend on
    filesystem discovery order, only on path identity and content."""
    return _sha256_of(_canonical_json(sorted(entries, key=lambda pair: pair[0])))


def _relative(path: Path, workspace: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(workspace.resolve()))
    except ValueError:
        return str(resolved)


def resolve_assurance_status(concept: str, query: str) -> str:
    """See this module's own docstring ("Why assurance_status is always
    unsupported today"): no mechanical link between a witnessed query
    and a pipeline obligation exists anywhere in this codebase, so the
    honest, non-fabricated answer is unsupported. `concept`/`query` are
    accepted (not merely `_`-discarded) so a future linking convention
    has real inputs to resolve against without changing this function's
    signature or any of its call sites."""
    return "unsupported"


def owning_cluster_for(concept_spec: dict) -> str:
    """vendor/concept-to-code's own top-level `cluster` field --
    already domain-agnostic and required by its schema (confirmed by
    reading vendor/concept-to-code/schemas/spec.schema.json directly,
    docs/concept-to-code-modifications.md's own "already compatible, no
    change needed" finding). Read directly, never re-derived. A concept
    spec loosely admitted by collect_declared_features() (this pipeline
    never runs concept-to-code's own JSON Schema validator against a
    spec document -- see docs/concept-to-code-witness-required-schema.json's
    own note) might still lack it; "unknown" is the honest fallback,
    never a guess."""
    return concept_spec.get("cluster") or "unknown"


def closure_kind_for(cluster: str, closure_artifacts: dict) -> str:
    """The cluster's genuinely valid closure profile's own declared
    closure_kind (validate_closure.load_cluster_artifacts already
    applies the "genuinely valid, not just present" bar), or "n/a" when
    none exists -- plan.md §16.4's own worked example value for a
    feature with no applicable closure. This reads a DECLARATION, not a
    live G14 closure computation: re-running G14 itself is a much
    heavier dependency (full manifest/provider/bridge graph) this
    generated projection does not need and §16.6 does not ask for."""
    entry = closure_artifacts.get(cluster)
    if entry is None or "profile" not in entry:
        return "n/a"
    return entry["profile"][1]["closure_kind"]


def generate_ledger(workspace: Path, descriptor: dict, runner=subprocess.run) -> dict:
    """Build the ledger dict (not yet validated or written). Runs the
    real G18/G19/G20 checks -- including G19's real regeneration
    dispatch when a witness_backend is configured -- so this is a fresh
    recomputation every time, never a cache of a previous run.

    Raises GenerationError outright when collect_declared_features()
    itself reports an ambiguous declaration (external review, medium
    severity: an earlier version discarded those findings and silently
    iterated only the ambiguity-collapsed map, so two specs declaring
    the same required feature produced an empty ledger and "nothing to
    check" rather than a visible problem)."""
    declared, declare_findings = collect_declared_features(descriptor, workspace)
    ambiguous_declarations = [f for f in declare_findings if f.severity == "error"]
    if ambiguous_declarations:
        raise GenerationError(
            "cannot generate a feature ledger over an ambiguous declared feature set: "
            + "; ".join(str(f) for f in ambiguous_declarations)
        )

    g18_findings, _ = gate_g18_workspace(workspace, descriptor)
    witnesses, _ = collect_valid_witnesses(descriptor, workspace, set(declared))
    all_valid_specs, g19_spec_findings = collect_valid_witness_specs(descriptor, workspace)
    closure_artifacts = load_cluster_artifacts(workspace)
    backend = descriptor.get("witness_backend")

    g18_error_subjects = {f.subject for f in g18_findings if f.severity == "error"}
    ambiguous_witness_ids = {f.subject for f in g19_spec_findings}

    features: list[dict] = []
    concept_spec_paths: set[Path] = set()

    for (concept, query), spec_path in sorted(declared.items()):
        feature_id = f"{concept}.{query}"
        concept_spec_paths.add(spec_path.resolve())

        witness = witnesses.get((concept, query))
        witness_id = witness["witness_id"] if witness else None
        witness_present = witness is not None and feature_id not in g18_error_subjects

        if witness_id is None:
            determinism = "not-checked"
            degeneracy = "not-checked"
        elif witness_id in ambiguous_witness_ids:
            # A genuinely valid (concept, query)-scoped witness whose
            # witness_id nonetheless collides with a DIFFERENT file
            # elsewhere -- a structural identity problem neither
            # determinism nor degeneracy can be meaningfully evaluated
            # over (external review's own reasoning for why an
            # ambiguous witness_id must never resolve to whichever file
            # a caller happened to look up).
            determinism = "fail"
            degeneracy = "not-checked"
        else:
            document, det_findings = check_witness_determinism(witness_id, witness, backend, workspace, runner)
            if any(NO_BACKEND_MARKER in f.reason for f in det_findings):
                determinism = "not-checked"
            elif det_findings:
                determinism = "fail"
            else:
                determinism = "pass"

            # Family consistency is a static property of the declared
            # specs, independent of whether regeneration succeeded --
            # checked directly against every OTHER genuinely valid
            # witness (chainlink #31's own check_candidate_fixture_family_consistency,
            # symmetric regardless of sort order, reused rather than
            # gate_g20's pairwise workspace scan, which only ever flags
            # whichever witness sorts second in a disagreeing pair).
            family_findings = check_candidate_fixture_family_consistency(witness_id, witness, all_valid_specs)
            if document is None:
                # External review, high severity: gate_g20.py's own
                # check_must_vary reads a STORED result from disk
                # (load_results_by_witness), completely independent of
                # G19's live regeneration -- with no stored result on
                # disk at all, or a stale/mismatched one, it silently
                # skips (no finding), which this ledger used to read as
                # "degeneracy: ok". Fixed by evaluating must-vary
                # against the SAME freshly regenerated document
                # determinism just trusted, never a separately-read
                # on-disk one; with no trustworthy document at all,
                # must-vary is honestly not-checked (family consistency
                # alone can still surface a warning).
                degeneracy = "warn" if family_findings else "not-checked"
            else:
                must_vary_findings = check_must_vary({witness_id: witness}, {witness_id: document})
                degeneracy = "warn" if (must_vary_findings or family_findings) else "ok"

        implementation_observed = witness_present and determinism == "pass" and degeneracy == "ok"

        try:
            concept_data = json.loads(spec_path.read_text())
        except (OSError, json.JSONDecodeError):
            concept_data = {}
        owning_cluster = owning_cluster_for(concept_data)
        closure_kind = closure_kind_for(owning_cluster, closure_artifacts)

        features.append({
            "feature": feature_id,
            "witness_required": True,
            "witness_present": witness_present,
            "implementation_observed": implementation_observed,
            "determinism": determinism,
            "degeneracy": degeneracy,
            "assurance_status": resolve_assurance_status(concept, query),
            "owning_cluster": owning_cluster,
            "closure_kind": closure_kind,
        })

    concept_entries: list[tuple[str, dict]] = []
    for path in sorted(concept_spec_paths):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        concept_entries.append((_relative(path, workspace), data))

    witness_entries = [
        (_relative(path, workspace), data)
        for path, data in collect_valid_witness_entries(descriptor, workspace)
    ]

    assurance_entries: list[tuple[str, dict]] = []
    manifests, _ = load_manifests(workspace)
    report_validator = load_assurance_report_validator()
    for _, manifest in sorted(manifests.items()):
        emit = manifest.get("report", {}).get("emit")
        if not emit:
            continue
        report_path = (workspace / emit).resolve()
        if not report_path.is_file():
            continue
        try:
            data = json.loads(report_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if list(report_validator.iter_errors(data)):
            continue
        assurance_entries.append((_relative(report_path, workspace), data))

    return {
        "schema_version": "1.0",
        "generated_from": {
            "concept_specs_hash": _hash_documents(concept_entries),
            "witness_specs_hash": _hash_documents(witness_entries),
            "assurance_results_hash": _hash_documents(assurance_entries),
        },
        "features": features,
    }


def write_ledger(workspace: Path, descriptor: dict, runner=subprocess.run) -> Path:
    """Generate, validate against the schema, and write atomically.
    Raises GenerationError -- writing NOTHING -- if the generated ledger
    is not schema-valid: a generated artifact this codebase's own
    schema rejects must never reach disk, the same "genuinely valid,
    not just present" discipline every other generator here applies to
    its OWN output before trusting it."""
    ledger = generate_ledger(workspace, descriptor, runner=runner)
    errors = list(load_validator().iter_errors(ledger))
    if errors:
        raise GenerationError(
            f"generated feature ledger is not schema-valid, refusing to write it: {errors[0].message}"
        )
    text = json.dumps(ledger, indent=2, sort_keys=True) + "\n"
    destination = ledger_path_for(workspace)
    write_atomically(destination, text)
    return destination


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--descriptor", type=Path, default=None)
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

    try:
        destination = write_ledger(args.workspace, descriptor)
    except GenerationError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_GENERATION_FAILED

    count = len(json.loads(destination.read_text())["features"])
    if count == 0:
        print(f"wrote {destination} (0 declared features -- nothing to check)")
    else:
        print(f"wrote {destination} ({count} declared feature(s))")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
