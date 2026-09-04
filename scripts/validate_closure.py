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
           no record covering it is an undeclared degradation);
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
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_summary import pass_line  # noqa: E402
from schema_utils import make_validator  # noqa: E402
from schema_utils import make_validator_without_required  # noqa: E402

DOCS = Path(__file__).resolve().parent.parent / "docs"
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
    g1a = gate_g1a(path, data, validator)
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
    surfaces as a G1b violation instead of going unchecked."""
    if not root.is_dir():
        raise FileNotFoundError(f"closure scan root does not exist or is not a directory: {root}")
    return [p for p in root.glob(f"**/{CANONICAL_DIR_NAME}/**/*") if p.is_file()]


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


def load_cluster_artifacts(workspace: Path) -> dict[str, dict[str, tuple[Path, dict]]]:
    """Every fully valid (zero error-severity finding) closure artifact in
    the canonical directory, indexed by cluster then kind -- the "must be
    genuinely valid, not just present" bar every cross-reference in this
    pipeline applies. scripts/gate_g14.py consumes this, so a
    schema-invalid or misnamed profile can never reach the closure
    computation and be treated as a declaration of anything."""
    canonical = closure_dir_for(workspace)
    result: dict[str, dict[str, tuple[Path, dict]]] = {}
    if not canonical.is_dir():
        return result
    validators = load_validators()
    for path in sorted(p for p in canonical.iterdir() if p.is_file()):
        if any(f.severity == "error" for f in validate_file(path, validators)):
            continue
        data = json.loads(path.read_text())
        kind = "degradation" if is_degradation_path(path) else "profile"
        result.setdefault(data["cluster"], {}).setdefault(kind, (path, data))
    return result


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
