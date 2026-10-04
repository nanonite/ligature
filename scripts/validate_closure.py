#!/usr/bin/env python3
"""Closure-profile and degradation-record validation: G1a, G1b, G17.

plan.md §4 and §12's G17 row, chainlink #25. Stage 8C. Both artifact
types live in the same workspace-level directory (specs/_closure/),
distinguished by filename exactly as plan.md's own two worked examples
write them:

    specs/_closure/<cluster>.json               closure profile
    specs/_closure/<cluster>.degradation.json   degradation record

One module for both, deliberately: G17 is a check BETWEEN them ("profile
bits inconsistent with degradation record"), so a split would force one
of the two validators to load the other's artifacts anyway.

  G1a -- draft-2020-12 schema validation against
         docs/closure-profile-schema.json / docs/degradation-record-schema.json.
  G1b -- repo semantics: flat inside a _closure/ directory; cluster ==
         filename stem (with `.degradation` stripped for a record); at
         most one profile and one record per cluster.
  G17 -- the two artifacts of one cluster must agree:
         * a degradation record may only name a condition its own
           cluster's profile actually declares as failing -- a record
           claiming `single_verifier_system` failed while the profile
           declares it true is the "profile bits inconsistent with
           degradation record" case from §12, and it is a hard error in
           both directions (a profile declaring a condition false with
           no record covering it is an undeclared degradation). One
           documented exception (chainlink #86): for
           generic_callees_type_universal_or_creusot_owned -- the CG3
           condition nothing here can verify either way -- a record
           naming it beside a `true` declaration is the tracking record
           plan.md §3 requires of every capability gap, not a stale
           excuse. gate_g14 polices staleness for that one key where the
           closure's evidence lives;
         * `closure_kind: deductive` is refused when the profile's own
           owning_verifier is kani -- §12's other G17 clause, and
           §4's whole reason for having a kind: a Kani-owned cluster is
           bounded, and must never read as full closure.

What this module deliberately does NOT do: compute the closure. Whether
the declared condition bits are TRUE of the actual dependency graph is
G14's job (scripts/gate_g14.py), which recomputes every condition it can
and rejects disagreement. This module checks the artifacts against each
other and against their schemas -- the same boundary
validate_interaction.py draws against R2's own gate.

One loader for the records, not one per command (chainlink #99).
`load_degradation_records()` is the single discovery-and-validation
path for specs/_closure/*.degradation.json: a record is `valid` only if
it is in the canonical directory, one per cluster, schema-valid, names
a real condition vocabulary in `failed_conditions`, sits beside a valid
closure profile, and is not a stale excuse beside that profile (G17's
record-side direction, with chainlink #86's one CG3 exception intact).
Every record-shaped file it refused comes back with the findings that
refused it, so a consumer can tell "no degradation is declared" from
"one is declared and may not be acted on" -- the distinction three
separate ad-hoc parsers (validate-closure's own scan, gate_g14, and
`ligature status`) used to leave to chance. Discovery is workspace-wide
and rejection is by location, so a record filed outside specs/_closure/
is named rather than dropped -- including on a workspace with no
specs/_closure/ at all, which is precisely where a mislocated record is
most likely to be the only record there is.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resources  # noqa: E402
from review_checkpoint import is_staged_draft  # noqa: E402
from scan_summary import pass_line  # noqa: E402
from schema_utils import make_validator  # noqa: E402
from schema_utils import make_validator_without_required  # noqa: E402

DOCS = resources.resource_path("docs")
PROFILE_SCHEMA_PATH = DOCS / "closure-profile-schema.json"
DEGRADATION_SCHEMA_PATH = DOCS / "degradation-record-schema.json"
CANONICAL_DIR_NAME = "_closure"
DEGRADATION_SUFFIX = ".degradation"

# The condition keys shared by both schemas. Duplicated nowhere: read out
# of the profile schema itself, so a key added there can never be silently
# unexcusable here.
CONDITION_KEYS = tuple(
    json.loads(PROFILE_SCHEMA_PATH.read_text())["properties"]["conditions"]["required"]
)


@dataclass
class Finding:
    gate: str
    path: Path
    reason: str
    severity: str = "error"

    def __str__(self) -> str:
        return f"[{self.gate}/{self.severity}] {self.path}: {self.reason}"


def load_profile_schema() -> dict:
    return json.loads(PROFILE_SCHEMA_PATH.read_text())


def load_degradation_schema() -> dict:
    return json.loads(DEGRADATION_SCHEMA_PATH.read_text())


def load_profile_validator() -> Draft202012Validator:
    return make_validator(load_profile_schema())


def load_degradation_validator() -> Draft202012Validator:
    return make_validator(load_degradation_schema())


def load_profile_draft_validator() -> Draft202012Validator:
    return make_validator_without_required(load_profile_schema(), "review")


def load_degradation_draft_validator() -> Draft202012Validator:
    return make_validator_without_required(load_degradation_schema(), "review")


def is_degradation_path(path: Path) -> bool:
    """Filename-suffix dispatch, exactly as plan.md §4 writes the two
    examples. Applied to the *stem* so `x.degradation.json` is a record
    while `x.json` is a profile, and neither can be mistaken for the
    other by content -- a file that fails its own kind's schema is
    reported against that kind, not silently retried as the other."""
    return path.stem.endswith(DEGRADATION_SUFFIX)


def cluster_for_path(path: Path) -> str:
    stem = path.stem
    return stem[: -len(DEGRADATION_SUFFIX)] if stem.endswith(DEGRADATION_SUFFIX) else stem


def gate_g1a(path: Path, data: dict, validator: Draft202012Validator) -> list[Finding]:
    return [Finding("G1a", path, e.message) for e in validator.iter_errors(data)]


def check_naming(path: Path, data: dict) -> list[Finding]:
    findings: list[Finding] = []

    if path.parent.name != CANONICAL_DIR_NAME:
        findings.append(
            Finding(
                "G1b", path,
                f"not flat -- must live directly inside a {CANONICAL_DIR_NAME}/ directory, "
                f"found under {path.parent}",
            )
        )

    if path.suffix != ".json":
        findings.append(Finding("G1b", path, f"must be a .json file, found suffix {path.suffix!r}"))

    expected_cluster = cluster_for_path(path)
    if data["cluster"] != expected_cluster:
        kind = "degradation record" if is_degradation_path(path) else "closure profile"
        findings.append(
            Finding(
                "G1b", path,
                f"{kind} declares cluster {data['cluster']!r}, but its filename says "
                f"{expected_cluster!r}",
            )
        )

    return findings


def check_no_draft_review(path: Path, data: dict) -> list[Finding]:
    """Accepting a closure profile or a degradation record is a §7.2 human
    checkpoint; a model authoring its own `review` asserts a sign-off
    that never happened. Same check every other draft-capable artifact
    type in this codebase carries."""
    if "review" in data:
        return [
            Finding(
                "G1b", path,
                "draft must not include its own `review` block -- review is only "
                "attached by approve() after a human reviewer signs off",
            )
        ]
    return []


def _describe_failed_conditions(value: object) -> str:
    """What a malformed `failed_conditions` actually is, named rather
    than guessed at. Every consumer of a record treats this field as a
    list of condition keys -- `set(record["failed_conditions"])` in
    gate_g14.apply_degradation(), a per-item `[str(c) for c in ...]` in
    project_state -- so a string here is not a cosmetic problem: it
    iterates character by character and reads as a cluster excused by
    's', 'i', 'n', ... ."""
    if isinstance(value, str):
        return f"the string {value!r}"
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return f"the scalar {value!r}"
    if isinstance(value, list):
        if not value:
            return "an empty list"
        kinds = sorted({type(item).__name__ for item in value})
        return f"a list of {', '.join(kinds)}"
    return f"a {type(value).__name__}"


def check_failed_conditions_shape(path: Path, data: dict) -> list[Finding]:
    """`failed_conditions` is a degradation record's whole excuse
    vocabulary, and it is a CLOSED one: an array of closure-profile
    CONDITION keys, nothing else. The schema already says so; this check
    exists so the guarantee a caller of load_degradation_records() gets
    does not depend on that schema alone -- every consumer of a valid
    record may treat the field as a list of known keys without
    re-deriving that for itself, and a future relaxation of the schema
    cannot silently hand them a string.

    Reported with its own message rather than only jsonschema's, so the
    reason names what the field is FOR: a record can only excuse a
    declared closure condition, never an absence of evidence."""
    if "failed_conditions" not in data:
        # A required field that is absent is G1a's finding to make; there
        # is no shape here to complain about.
        return []

    value = data["failed_conditions"]
    if not isinstance(value, list):
        return [
            Finding(
                "G1a", path,
                f"failed_conditions must be an array of closure-condition keys, found "
                f"{_describe_failed_conditions(value)} -- a degradation record's vocabulary is "
                f"a list of condition names, not text to be read",
            )
        ]

    findings: list[Finding] = []
    if not value:
        findings.append(
            Finding(
                "G1a", path,
                "failed_conditions is empty -- a record naming no condition excuses nothing and "
                "is not a record of any degradation",
            )
        )
    non_strings = [item for item in value if not isinstance(item, str)]
    if non_strings:
        findings.append(
            Finding(
                "G1a", path,
                "failed_conditions entries must be closure-condition key strings; found "
                f"{', '.join(_describe_failed_conditions(item) for item in non_strings)}",
            )
        )
    unknown = sorted({item for item in value if isinstance(item, str)} - set(CONDITION_KEYS))
    for key in unknown:
        findings.append(
            Finding(
                "G1a", path,
                f"failed_conditions names {key!r}, which is not a closure condition key -- a "
                f"record may excuse only a condition its own closure profile declares as failing",
            )
        )
    duplicates = sorted({item for item in value if isinstance(item, str) and value.count(item) > 1})
    for key in duplicates:
        findings.append(
            Finding("G1a", path, f"failed_conditions names {key!r} more than once")
        )
    return findings


def check_kind_against_owning_verifier(path: Path, data: dict) -> list[Finding]:
    """G17, first clause: 'closure profile asserts deductive while owning
    verifier is Kani'. Kani is a bounded model checker -- its result
    holds within unwind and harness bounds and for the monomorphizations
    actually enumerated (plan.md §4's table, and CG3). A profile that
    declares kani and claims `deductive` is not rounding up, it is
    claiming a different guarantee than the one it has.

    Self-contained (profile-internal), so it runs here rather than in
    G14 -- but G14 also recomputes owning_verifier from the closure's own
    achieved records, which is what stops a profile from simply declaring
    `creusot` over a Kani-verified closure."""
    if data["closure_kind"] == "deductive" and data["conditions"]["owning_verifier"] == "kani":
        return [
            Finding(
                "G17", path,
                "closure_kind is 'deductive' while conditions.owning_verifier is 'kani' -- "
                "a bounded model checker's result holds only within its unwind/harness bounds "
                "and enumerated monomorphizations, so a Kani-owned cluster closes 'bounded' "
                "(plan.md §4) or carries a degradation record",
            )
        ]
    return []


def check_profile_record_consistency(
    cluster: str,
    profile: tuple[Path, dict] | None,
    degradation: tuple[Path, dict] | None,
) -> list[Finding]:
    """G17, second clause: 'profile bits inconsistent with degradation
    record'. Checked in BOTH directions, because each direction hides a
    different thing:

      * a record naming a condition the profile declares as holding is a
        stale excuse -- the cluster reads as degraded after the gap
        closed, and nobody is told the tracking issue can be shut;
      * a profile declaring a condition false with no record covering it
        is an undeclared degradation -- the failure is visible in the
        artifact and covered by nothing, which is precisely the state
        plan.md §4 replaces with 'a declared state, not a failure'.

    The first direction has exactly one exception, chainlink #86:
    generic_callees_type_universal_or_creusot_owned is the one condition
    no artifact here can settle either way (CG3), so beside a `true`
    declaration a record naming it is the tracking record plan.md §3
    demands, and gate_g14 -- which CAN see the closure's evidence --
    both requires it when the declaration is unverifiable and rejects it
    as stale once the records verify the condition.

    `unresolved_indirect_calls_at_or_above_medium` is an integer, not a
    boolean: it 'fails' when it is non-zero (plan.md §4's own closure
    condition is that it be 0)."""
    findings: list[Finding] = []
    if profile is None:
        if degradation is not None:
            path, _ = degradation
            findings.append(
                Finding(
                    "G17", path,
                    f"degradation record for cluster {cluster!r} has no closure profile beside it "
                    f"-- a degradation is a departure from a declared profile, and without one "
                    "there is nothing it is a departure from",
                )
            )
        return findings

    profile_path, profile_data = profile
    failing = {key for key in CONDITION_KEYS if not _condition_holds(key, profile_data["conditions"])}
    excused = set(degradation[1]["failed_conditions"]) if degradation is not None else set()

    for key in sorted(excused - failing):
        if key == "generic_callees_type_universal_or_creusot_owned":
            # chainlink #86: for this ONE condition the profile bit is a
            # human declaration that nothing at profile/record level can
            # ever show closed -- no artifact here carries the type
            # information CG3 needs, and validate_closure has no closure
            # evidence either way. A record naming it beside a `true`
            # declaration is therefore the tracking record plan.md §3
            # demands of every capability gap, not a stale excuse.
            # Staleness for this key is policed by gate_g14 instead,
            # where the workspace's own artifacts CAN show the condition
            # verified -- closure records, declared depends_on
            # dependencies, project-wide manifests and ledgers
            # (chainlink #93) -- and where an unverifiable declaration
            # with no record beside it stays blocked (#86's false `true`).
            continue
        findings.append(
            Finding(
                "G17", degradation[0],
                f"failed_conditions names {key!r}, but cluster {cluster!r}'s closure profile "
                f"declares that condition as holding -- a stale excuse keeps a closed gap "
                "reading as degraded",
            )
        )
    for key in sorted(failing - excused):
        findings.append(
            Finding(
                "G17", profile_path,
                f"condition {key!r} is declared as not holding, but no degradation record for "
                f"cluster {cluster!r} covers it -- an undeclared degradation",
            )
        )
    return findings


def _condition_holds(key: str, conditions: dict) -> bool:
    value = conditions.get(key)
    if key == "unresolved_indirect_calls_at_or_above_medium":
        return value == 0
    if key == "owning_verifier":
        return True  # a name, not a claim that can fail on its own
    if key == "scc_wellfoundedness_discharged":
        return value is True or value == "not-applicable"
    return value is True


def validate_data(path: Path, data: dict, validators: dict) -> list[Finding]:
    degradation = is_degradation_path(path)
    validator = validators["degradation"] if degradation else validators["profile"]
    # `failed_conditions` is reported in its own terms FIRST, before the
    # schema's verdict, because jsonschema says "'x' is not of type
    # 'array'" without naming the field or saying why the field matters.
    # Both findings are returned when both apply: the shape check
    # complements the schema, it never replaces it.
    shape = check_failed_conditions_shape(path, data) if degradation else []
    g1a = shape + gate_g1a(path, data, validator)
    if g1a:
        return g1a
    findings = check_naming(path, data)
    if not degradation:
        findings.extend(check_kind_against_owning_verifier(path, data))
    return findings


def validate_draft_data(path: Path, data: dict, validators: dict) -> list[Finding]:
    """Stage 0/3-style immediate feedback (plan.md §6.1). Excludes G17's
    cross-artifact clause, which needs the cluster's other artifact --
    mirrors every other validate_<type>.py module's own
    validate_draft_data()."""
    review_check = check_no_draft_review(path, data)
    if review_check:
        return review_check
    return validate_data(path, data, validators)


def load_validators(draft: bool = False) -> dict:
    if draft:
        return {
            "profile": load_profile_draft_validator(),
            "degradation": load_degradation_draft_validator(),
        }
    return {"profile": load_profile_validator(), "degradation": load_degradation_validator()}


def validate_file(path: Path, validators: dict) -> list[Finding]:
    try:
        text = path.read_text()
    except UnicodeDecodeError as e:
        return [Finding("G1a", path, f"not readable as UTF-8 text: {e}")]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return [Finding("G1a", path, f"invalid JSON: {e}")]
    if not isinstance(data, dict):
        return [Finding("G1a", path, "top-level value is not a JSON object")]
    return validate_data(path, data, validators)


def find_closure_files(root: Path) -> list[Path]:
    """Every file anywhere under a `_closure` directory, at any depth --
    mirrors find_bridge_files/find_report_files so a nested placement
    surfaces as a G1b violation instead of going unchecked.

    Staged drafts (`<target>.json.draft`, written by review_checkpoint.
    stage_draft() once chainlink #98 wires closure profiles and
    degradation records into `pipeline.py draft`) are excluded, the same
    `.draft` convention find_interaction_files()/find_evidence_files()
    already apply: a draft is not yet an artifact under review, and
    without this exclusion it would be scanned, fail `check_naming()`'s
    "must be a .json file" rule, and be reported as a G1b error on work
    staged exactly as the tool prescribes -- chainlink #90's defect,
    reproduced here before this fix."""
    if not root.is_dir():
        raise FileNotFoundError(f"closure scan root does not exist or is not a directory: {root}")
    return [
        p for p in root.glob(f"**/{CANONICAL_DIR_NAME}/**/*") if p.is_file() and not is_staged_draft(p)
    ]


def count_discovered(root: Path) -> int:
    """The candidate set this module's own scan walks, counted for the
    honest pass line (chainlink #48). Wraps find_closure_files() rather
    than re-deriving its glob, so the count can never drift from the set
    actually validated."""
    return len(find_closure_files(root))


def validate_workspace(root: Path, canonical_dir: Path) -> list[Finding]:
    """Discover workspace-wide, then reject by location -- the same
    discover-then-reject-by-location shape every validate_<type>.py
    module uses, and for the same reason (a scan anchored only to the
    canonical directory stops a mislocated artifact from being wrongly
    trusted, but also stops it from ever being looked at). Adds the
    cross-artifact G17 pass over whatever validated cleanly."""
    canonical_resolved = canonical_dir.resolve()
    validators = load_validators()
    findings: list[Finding] = []
    by_cluster: dict[str, dict[str, tuple[Path, dict]]] = {}

    for path in sorted(find_closure_files(root)):
        if path.resolve().parent != canonical_resolved:
            findings.append(
                Finding(
                    "G1b", path,
                    f"closure artifact is not directly under the canonical directory "
                    f"{canonical_resolved} -- found under {path.resolve().parent}",
                )
            )
            continue
        file_findings = validate_file(path, validators)
        findings.extend(file_findings)
        if any(f.severity == "error" for f in file_findings):
            continue
        data = json.loads(path.read_text())
        kind = "degradation" if is_degradation_path(path) else "profile"
        slot = by_cluster.setdefault(data["cluster"], {})
        if kind in slot:
            findings.append(
                Finding(
                    "G1b", path,
                    f"cluster {data['cluster']!r} already has a {kind} at {slot[kind][0]} -- "
                    "a cluster closes under exactly one profile and at most one degradation record",
                )
            )
            continue
        slot[kind] = (path, data)

    for cluster in sorted(by_cluster):
        findings.extend(
            check_profile_record_consistency(
                cluster, by_cluster[cluster].get("profile"), by_cluster[cluster].get("degradation")
            )
        )
    return findings


def closure_dir_for(workspace: Path) -> Path:
    """plan.md §4's own path: specs/_closure/. Workspace-level, not
    crate-scoped -- a cluster spans crates by construction (the
    analysis-configuration cluster in §3 is the worked example), so
    filing its closure under one crate would misrepresent what closed."""
    return (workspace / "specs" / CANONICAL_DIR_NAME).resolve()


@dataclass
class _ClosureScan:
    """One pass over a workspace's canonical closure directory, with
    every artifact's disposition kept rather than discarded. The
    `rejected` map is what chainlink #49 asks for and what
    load_degradation_records() (chainlink #99) needs: a record excluded
    from the valid index is not merely absent from it, it has a reason,
    and a consumer that cannot explain the absence will read "no
    degradation record" off a workspace that does have one."""

    artifacts: dict[str, dict[str, tuple[Path, dict]]]
    rejected: dict[Path, list[Finding]]
    misplaced: dict[Path, list[Finding]]


def _scan_closure_dir(workspace: Path) -> _ClosureScan:
    """The ONE discovery-and-validation pass over
    specs/_closure/<cluster>.json / <cluster>.degradation.json, every
    rule applied here exactly once:

      * an artifact's own validation (validate_file(): G1a schema,
        G1b naming/flatness) -- a hit rejects it, with the reason kept;
      * kind dispatch by filename suffix, exactly as the two plan.md §4
        examples are written. G1b's cluster==filename rule already makes
        two artifacts of one kind for one cluster unrepresentable (a
        second file declaring the same cluster is misnamed, and is
        refused above), so the per-cluster slot needs no arbitration;
      * location -- discovery is workspace-wide and rejection is by
        location (validate_workspace()'s shape), so a record filed
        outside specs/_closure/ is reported, not ignored. This runs
        whether or not the canonical directory exists: a workspace with
        no specs/_closure/ at all is exactly where a mislocated record
        is most likely to be the only record there is, and validate()
        names it there.

    Cross-artifact G17 is deliberately NOT run here: it needs the whole
    cluster, and it is run once, by whoever owns a cluster-level
    answer (validate_workspace() for the gate itself,
    load_degradation_records() for a consumer that only wants records).

    A staged `<target>.json.draft` is excluded from this pass the same
    way find_closure_files() excludes it (chainlink #98): it is not yet
    an artifact under review, and scanning it here would both report it
    as a spurious G1b finding and let it occupy a cluster's kind slot
    ahead of the real (or not-yet-approved) artifact."""
    canonical = closure_dir_for(workspace)
    scan = _ClosureScan(artifacts={}, rejected={}, misplaced=_misplaced_records(workspace, canonical))

    if canonical.is_dir():
        validators = load_validators()
        for path in sorted(p for p in canonical.iterdir() if p.is_file() and not is_staged_draft(p)):
            file_findings = validate_file(path, validators)
            if any(f.severity == "error" for f in file_findings):
                scan.rejected[path] = file_findings
                continue
            data = json.loads(path.read_text())
            kind = "degradation" if is_degradation_path(path) else "profile"
            scan.artifacts.setdefault(data["cluster"], {}).setdefault(kind, (path, data))

    return scan


def _misplaced_records(workspace: Path, canonical: Path) -> dict[Path, list[Finding]]:
    """Every degradation record filed somewhere other than directly under
    the canonical closure directory, each with the G1b location finding
    validate_workspace() reports for it -- the same rule, reached from
    the same discovery, so the two cannot disagree about a workspace
    that holds no canonical closure directory.

    Deliberately independent of the canonical directory existing. The
    failure this guards is a caller reading an empty result as "no
    degradation is declared" while a record sits at
    docs/_closure/ghost.degradation.json: valid there is fail-closed and
    correct, but silence about the refusal is not."""
    if not workspace.is_dir():
        return {}
    canonical_resolved = canonical.resolve()
    misplaced: dict[Path, list[Finding]] = {}
    for path in sorted(find_closure_files(workspace)):
        if path.resolve().parent == canonical_resolved or not is_degradation_path(path):
            continue
        misplaced[path] = [
            Finding(
                "G1b", path,
                f"closure artifact is not directly under the canonical directory "
                f"{canonical_resolved} -- found under {path.resolve().parent}",
            )
        ]
    return misplaced


def _scan_cluster_artifacts(
    workspace: Path,
) -> tuple[dict[str, dict[str, tuple[Path, dict]]], list[Path]]:
    """The single validation pass shared by load_cluster_artifacts() and
    load_cluster_artifacts_with_invalid() -- one scan, so recovering the
    "why was this excluded" diagnostic (chainlink #49) never means
    validating a candidate artifact twice. Returns the valid-artifact
    index exactly as load_cluster_artifacts() has always returned it,
    plus the sorted paths of every canonical closure artifact excluded
    because its own validation produced an error-severity finding
    (schema-invalid, malformed JSON, unreadable, misnamed -- anything
    validate_file() flags)."""
    scan = _scan_closure_dir(workspace)
    return scan.artifacts, list(scan.rejected)


def load_cluster_artifacts(workspace: Path) -> dict[str, dict[str, tuple[Path, dict]]]:
    """Every fully valid (zero error-severity finding) closure artifact in
    the canonical directory, indexed by cluster then kind -- the "must be
    genuinely valid, not just present" bar every cross-reference in this
    pipeline applies. scripts/gate_g14.py, gate_g9.py, and
    generate_feature_ledger.py all consume this, so a schema-invalid or
    misnamed profile can never reach the closure computation and be
    treated as a declaration of anything."""
    result, _ = _scan_cluster_artifacts(workspace)
    return result


def load_cluster_artifacts_with_invalid(
    workspace: Path,
) -> tuple[dict[str, dict[str, tuple[Path, dict]]], list[Path]]:
    """load_cluster_artifacts()'s identical index, plus the paths of every
    canonical closure artifact it silently excluded. chainlink #49:
    gate_g14.py's gate_workspace() uses this (instead of
    load_cluster_artifacts()) to surface one finding per excluded
    artifact -- fail-closed exclusion from closure computation is
    correct, but dropping the reason on the floor is not."""
    return _scan_cluster_artifacts(workspace)


@dataclass
class DegradationRecords:
    """Structured result of load_degradation_records(): the records that
    may be acted on, and every record-shaped file that was found and
    refused, each with the findings that refused it.

    Both halves matter and neither is optional. `valid` alone is what a
    gate acts on (fail-closed: anything not provably valid excuses
    nothing), and `invalid` is what stops that silence being read as
    "this cluster declares no degradation" when the workspace in fact
    contains one -- the state chainlink #49 named for closure artifacts
    generally. A consumer that reports on the invalid half is honest
    about the difference; one that ignores it can only tell "no record"
    from "a record nobody may use" by accident of parsing."""

    valid: dict[str, tuple[Path, dict]]
    invalid: dict[Path, list[Finding]]

    def record_for(self, cluster: str) -> dict | None:
        """The validated record data for `cluster`, or None -- which
        covers both 'this cluster declares no degradation' and 'the one
        it declares may not be acted on'. `reasons_for()` is how a caller
        tells those two apart."""
        entry = self.valid.get(cluster)
        return entry[1] if entry is not None else None

    def path_for(self, cluster: str) -> Path | None:
        entry = self.valid.get(cluster)
        return entry[0] if entry is not None else None

    def reasons_for(self, path: Path) -> list[str]:
        """Every reason `path` was refused, as strings, for reporting."""
        return [str(f) for f in self.invalid.get(path, [])]

    def is_valid(self, cluster: str) -> bool:
        return cluster in self.valid

    def __len__(self) -> int:
        return len(self.valid)


def load_degradation_records(workspace: Path) -> DegradationRecords:
    """THE one discovery-and-validation path for
    specs/_closure/*.degradation.json (chainlink #99). Every consumer --
    validate-closure's own reporting, gate-g14, `ligature status` --
    reads records through here rather than opening
    specs/_closure/ and parsing what it finds, which is how three
    commands came to disagree about whether a given record counts.

    A record is in `valid` only when all of the following hold, each
    checked by this module's own existing rule rather than re-derived:

      1. it is a degradation record by filename suffix, directly under
         the canonical closure directory, and the only one its cluster
         can have (G1b's cluster==filename rule already refuses a
         second file declaring the same cluster);
      2. it validates against docs/degradation-record-schema.json and
         the G1b naming rules -- validate_file(), the same call
         validate-closure itself makes, so "valid here" and "valid to
         validate-closure" cannot diverge. That call includes (3);
      3. `failed_conditions` is a usable excuse vocabulary: an array of
         closure-condition keys and nothing else
         (check_failed_conditions_shape(), which validate_data() runs
         ahead of the schema so the reason names the field). A string
         here is not a formatting complaint --
         `set(record["failed_conditions"])` and
         `[str(c) for c in record["failed_conditions"]]` would read it
         character by character, so every consumer of a record in
         `valid` may use that field as a list of condition keys without
         checking it again;
      4. its cluster's closure profile validates too, and the two agree
         (G17) -- a record naming a condition the profile declares as
         holding is a STALE EXCUSE and is refused, with chainlink #86's
         single documented exception (a CG3
         generic_callees_type_universal_or_creusot_owned record beside a
         `true` declaration, which is the tracking record plan.md §3
         requires, not a stale excuse) untouched;
      5. a record with no valid profile beside it is refused, because a
         degradation is a departure from a declared profile and without
         one there is nothing it departs from.

    What this loader deliberately does NOT do: decide whether a record
    is ACCEPTED (degradation_review.degradation_record_gaps() -- who
    reviewed it, and whether a human ruled on its exact bytes) or
    recompute the closure (gate_g14). It answers one question -- which
    records may a command act on as this cluster's declared degradation
    -- and answers it from validation alone."""
    scan = _scan_closure_dir(workspace)
    invalid: dict[Path, list[Finding]] = dict(scan.misplaced)

    valid: dict[str, tuple[Path, dict]] = {}
    for cluster in sorted(scan.artifacts):
        entry = scan.artifacts[cluster]
        record = entry.get("degradation")
        if record is None:
            # No record at all. A profile failing a condition its own
            # record does not cover is the PROFILE's G17 finding (an
            # undeclared degradation), already reported by
            # validate_workspace() against the profile -- nothing to
            # refuse here, and nothing to excuse either.
            continue
        path, _ = record
        # No schema or shape check here: a record in `scan.artifacts` has
        # already cleared validate_file(), which includes
        # check_failed_conditions_shape() -- points 2 and 3 above are
        # settled by the scan, not re-derived.
        findings: list[Finding] = []
        for finding in check_profile_record_consistency(
            cluster, entry.get("profile"), record
        ):
            # G17 is checked in both directions, and only one of them is
            # the record's fault. check_profile_record_consistency()
            # already attributes each finding to the artifact it is
            # about (a stale excuse and a recordless profile are
            # reported against the record; an undeclared degradation is
            # reported against the profile), so selecting on the path
            # refuses the record for exactly its own reasons and lets
            # the profile's stand as its own finding elsewhere.
            if Path(finding.path) == path:
                findings.append(finding)
        if findings:
            invalid[path] = findings
        else:
            valid[cluster] = record

    # A record refused for anything its own file shows is already in
    # `rejected`; those refusals are its complete story, and repeating
    # the cross-artifact pass over an artifact that never validated
    # could only add noise.
    for path, findings in scan.rejected.items():
        if is_degradation_path(path):
            invalid[path] = findings

    return DegradationRecords(valid=valid, invalid=invalid)


def validate(root: Path) -> list[Finding]:
    """Standalone CLI entry: `root` is always the workspace root, so this
    delegates to the anchored workspace scan rather than a separate,
    weaker unanchored one (the layout gap a 2026-09-02 review found in
    validate_evidence/validate_conflict_resolution's own standalone
    validate())."""
    return validate_workspace(root, closure_dir_for(root))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=Path, help="Workspace root holding specs/_closure/*.json")
    args = parser.parse_args(argv)

    try:
        findings = validate(args.root)
        discovered = count_discovered(args.root)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    errors = [f for f in findings if f.severity == "error"]
    infos = [f for f in findings if f.severity == "info"]

    if infos:
        print(f"INFO: {len(infos)} non-blocking finding(s)")
        for f in infos:
            print(f"  - {f}")

    if not errors:
        print(pass_line(
            discovered, "closure artifacts", "G1a/G1b and G17 profile/record consistency", args.root
        ))
        return 0

    print(f"FAIL: {len(errors)} finding(s)")
    for f in errors:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
