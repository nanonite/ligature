#!/usr/bin/env python3
"""R1 and G16: reconciling realized calls (C_static) against intended
interactions (I), with the risk-tiered unresolved policy.

plan.md §9/§9.1 and the gate table in §12, chainlink #24. Stage 8A.

  R1  "realized call absent from I -- risk-tiered" (block / warn).
      A call the code actually makes that the intent graph never
      declared is drift: either the implementation grew an edge nobody
      accepted, or I is incomplete. plan.md §9.1's table, row by row:

        definite eligible direct call  -> block
        possible eligible dispatch     -> warn, or block by risk
        unresolved indirect call       -> keep unresolved; never claim closure
        internal helper                -> ignore unless architecturally eligible

      "Internal helper" is operationalized as a call whose caller and
      callee are the same concept: intra-concept structure is L2's
      territory (a concept's own contracts), not an interaction edge.
      Cross-concept is never treated this way -- that is exactly the
      edge R1 exists to catch.

      R1 also covers plan.md §12's "realized calls matching within
      compatible configurations": a matched edge whose interaction was
      analyzed under a different target than this observation was taken
      under is not a match, it is an unexamined configuration.

  G16 "call-site coverage; unresolved risk-tiered" (block / decide /
      accept). Every unresolved call site -- possible-dispatch and
      unresolved-indirect-call alike -- carries a risk tier, and the
      tier decides the disposition:

        critical, high -> block
        medium         -> human decision (exit 3, never a silent pass)
        low            -> visible accepted limitation (info)

      plan.md §9.1's own reasoning: a blanket `unresolved > 0 -> block`
      makes every dynamic-dispatch cluster unusable, while silently
      accepting unresolved calls claims a completeness nobody has.
      Neither path permits a full-coverage claim.

The honesty rule, plan.md §9, is structural here: this module's only
coverage statement is "all discovered call sites resolved" (module
constant HONEST_COVERAGE_STATEMENT). The string "all call sites
resolved" appears nowhere it could be printed, and a test asserts that.
The reports being reconciled say the same thing in their own vocabulary:
a syntactic extractor may only claim `discovered-lower-bound`
(docs/callsite-schema.json), so "discovered" is the strongest quantifier
available anywhere in this chain -- CG2 (call-graph completeness is
undecidable) is permanent, and the response is "report, never claim
closure".

Consumed by chainlink #25: `unresolved_at_or_above_medium()` is the
closure-profile condition plan.md §4 names verbatim
(`unresolved_indirect_calls_at_or_above_medium: 0`).
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_callsites import callsite_report_dir_for  # noqa: E402
from validate_callsites import load_reports  # noqa: E402
from validate_interaction import load_interactions_by_id  # noqa: E402

HONEST_COVERAGE_STATEMENT = "all discovered call sites resolved"

DISPOSITION_BY_TIER = {
    "critical": "block",
    "high": "block",
    "medium": "decide",
    "low": "accept",
}
SEVERITY_BY_DISPOSITION = {"block": "error", "decide": "decision", "accept": "info"}

EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_DECISION_REQUIRED = 3


@dataclass
class Finding:
    gate: str
    path: Path
    reason: str
    severity: str = "error"  # "error" | "decision" | "info"

    def __str__(self) -> str:
        return f"[{self.gate}/{self.severity}] {self.path}: {self.reason}"


@dataclass
class Reconciliation:
    """plan.md §9's callsite_coverage, completed. `checked` is the count
    extraction deliberately does not produce (docs/callsite-schema.json's
    own note on the rename): it is a reconciliation outcome, known only
    here -- a definite-direct-call either matched an interaction or was
    ignored as an intra-concept helper."""
    reports: int = 0
    discovered: int = 0
    checked: int = 0
    unresolved: int = 0

    def __str__(self) -> str:
        return (
            f"reports={self.reports} discovered={self.discovered} "
            f"checked={self.checked} unresolved={self.unresolved}"
        )


def _edge_key(caller: dict, callee: dict) -> tuple[str, str, str, str]:
    return (caller["concept"], caller["method"], callee["concept"], callee["method"])


def index_interactions(interactions: dict) -> tuple[dict, set[str]]:
    """(caller, callee) role tuple -> interaction, plus every callee method
    name I mentions at all. The second index is what a possible-dispatch
    can be compared against: the method name is known, the receiving
    concept is not, so the strongest honest statement is "consistent with
    at least one intended edge" -- never a resolution."""
    by_edge: dict[tuple[str, str, str, str], dict] = {}
    callee_methods: set[str] = set()
    for data in interactions.values():
        by_edge[_edge_key(data["caller"], data["callee"])] = data
        callee_methods.add(data["callee"]["method"])
    return by_edge, callee_methods


def _tier_finding(gate: str, path: Path, record: dict, reason: str) -> Finding:
    tier = record["risk_tier"]
    disposition = DISPOSITION_BY_TIER[tier]
    source = record["risk_tier_source"]
    return Finding(
        gate, path,
        f"{record['callsite_id']} ({record['call_class']}): {reason} -- risk tier {tier} "
        f"({source}) -> {disposition}",
        SEVERITY_BY_DISPOSITION[disposition],
    )


def _check_config_compatibility(path: Path, record: dict, interaction: dict, config_scope: dict) -> list[Finding]:
    """plan.md §12's R1 wording: realized calls must match "within
    compatible configurations". A target mismatch is a hard R1 error --
    the edge was analyzed for a machine this observation says nothing
    about. Feature/cfg differences are reported but not blocking: the
    same edge legitimately exists under several feature sets, and
    blocking there would make any feature-gated crate unusable, the same
    over-blocking §9.1 rejects for unresolved calls."""
    declared = interaction["realization"]["config_scope"]
    findings: list[Finding] = []
    if declared["target"] != config_scope["target"]:
        findings.append(
            Finding(
                "R1", path,
                f"{record['callsite_id']}: interaction {interaction['interaction_id']!r} was "
                f"analyzed for target {declared['target']!r}, but this call was observed under "
                f"{config_scope['target']!r} -- a realized call only matches within a compatible "
                "configuration",
            )
        )
    if set(declared["features"]) != set(config_scope["features"]) or set(declared["cfg"]) != set(config_scope["cfg"]):
        findings.append(
            Finding(
                "R1", path,
                f"{record['callsite_id']}: interaction {interaction['interaction_id']!r} declares "
                f"features={declared['features']!r} cfg={declared['cfg']!r}, observed under "
                f"features={config_scope['features']!r} cfg={config_scope['cfg']!r}",
                "info",
            )
        )
    return findings


def reconcile(path: Path, report: dict, interactions: dict) -> tuple[list[Finding], Reconciliation]:
    by_edge, callee_methods = index_interactions(interactions)
    config_scope = report["config_scope"]
    findings: list[Finding] = []
    counts = Reconciliation(discovered=len(report["callsites"]))
    matched_edges: set[tuple[str, str, str, str]] = set()

    for record in report["callsites"]:
        call_class = record["call_class"]

        if call_class == "definite-direct-call":
            caller, callee = record["caller"], record["callee"]
            key = _edge_key(caller, callee)
            interaction = by_edge.get(key)
            if interaction is not None:
                matched_edges.add(key)
                counts.checked += 1
                findings.extend(_check_config_compatibility(path, record, interaction, config_scope))
                continue
            if caller["concept"] == callee["concept"]:
                counts.checked += 1
                findings.append(
                    Finding(
                        "R1", path,
                        f"{record['callsite_id']}: intra-concept call "
                        f"{caller['concept']}::{caller['method']} -> {callee['method']} is an "
                        "internal helper, ignored unless architecturally eligible",
                        "info",
                    )
                )
                continue
            findings.append(
                Finding(
                    "R1", path,
                    f"{record['callsite_id']}: realized call "
                    f"{caller['concept']}.{caller['method']} -> {callee['concept']}.{callee['method']} "
                    "is absent from I -- a definite direct call across concepts must be a declared "
                    "interaction (its eligibility cannot even be computed while it has no edge_class)",
                )
            )
            continue

        counts.unresolved += 1

        if call_class == "possible-dispatch":
            if record["callee_method"] in callee_methods:
                findings.append(
                    Finding(
                        "R1", path,
                        f"{record['callsite_id']}: dispatch on method "
                        f"{record['callee_method']!r} is consistent with at least one intended "
                        "edge, but the receiving concept is unresolved -- consistency is not a "
                        "resolution",
                        "info",
                    )
                )
            else:
                findings.append(
                    _tier_finding(
                        "R1", path, record,
                        f"possible dispatch on method {record['callee_method']!r} matches no "
                        "intended edge in I",
                    )
                )

        findings.append(
            _tier_finding(
                "G16", path, record,
                "call site is unresolved and can never count toward coverage",
            )
        )

    for key, interaction in by_edge.items():
        if key in matched_edges:
            continue
        findings.append(
            Finding(
                "R1", path,
                f"interaction {interaction['interaction_id']!r} ({key[0]}.{key[1]} -> {key[2]}.{key[3]}) "
                "has no realized call in this observation -- intent without realization, not a "
                "coverage failure",
                "info",
            )
        )

    return findings, counts


def unresolved_at_or_above_medium(report: dict) -> int:
    """plan.md §4's closure-profile condition, verbatim:
    `unresolved_indirect_calls_at_or_above_medium: 0`. Exposed here so
    chainlink #25's closure profile reads it from the gate that owns the
    tier policy instead of recomputing its own."""
    order = ["low", "medium", "high", "critical"]
    return sum(
        1 for c in report["callsites"]
        if c["call_class"] != "definite-direct-call"
        and order.index(c["risk_tier"]) >= order.index("medium")
    )


def coverage_statement(counts: Reconciliation) -> str:
    """The ONLY coverage sentence this module produces. plan.md §9: report
    "all discovered call sites resolved", never "all call sites
    resolved" -- the extractor's own completeness_claim is a lower bound,
    so undiscovered call sites are always possible (CG2)."""
    if counts.discovered == 0:
        # Vacuously "all resolved" is exactly the wording chainlink #48
        # found wrong in the validators, one level in: nothing was
        # discovered, so there is no coverage to claim either way.
        return "no call sites were discovered -- nothing to resolve, and no coverage claimed"
    if counts.unresolved == 0:
        return f"{HONEST_COVERAGE_STATEMENT} ({counts.discovered} discovered, {counts.checked} checked against I)"
    return (
        f"{counts.unresolved} of {counts.discovered} discovered call sites are unresolved "
        f"({counts.checked} checked against I)"
    )


def gate_workspace(workspace: Path, interaction_dir_by_crate: dict[str, Path]) -> tuple[list[Finding], Reconciliation]:
    """Reconcile every fully valid C_static report in the workspace. A
    report whose crate_dir is not one the descriptor knows is a hard
    error, not a skip: silently reconciling nothing is how a gate
    reports zero findings while checking zero things."""
    findings: list[Finding] = []
    totals = Reconciliation()
    for path, report in load_reports(workspace):
        crate_dir = report["crate_dir"]
        interaction_dir = interaction_dir_by_crate.get(crate_dir)
        if interaction_dir is None:
            findings.append(
                Finding(
                    "R1", path,
                    f"crate_dir {crate_dir!r} is not one of the project descriptor's own crates "
                    f"{sorted(interaction_dir_by_crate)!r} -- this report cannot be reconciled "
                    "against any interaction set",
                )
            )
            continue
        interactions = load_interactions_by_id(interaction_dir)
        report_findings, counts = reconcile(path, report, interactions)
        findings.extend(report_findings)
        totals.reports += 1
        totals.discovered += counts.discovered
        totals.checked += counts.checked
        totals.unresolved += counts.unresolved

    if totals.reports == 0:
        # The CLI already refuses to run with no ci/results/c_static
        # directory at all. This is the remaining shape of the same hole
        # (chainlink #48): the directory exists but holds nothing this
        # gate would trust -- every report failed G1a/G1b, or the
        # directory is empty -- and reporting a pass there is a gate
        # claiming to have reconciled something it never saw. Unlike the
        # validators, a gate fails closed on it.
        findings.append(
            Finding(
                "G16", callsite_report_dir_for(workspace),
                "no C_static report was reconciled -- the report directory holds nothing "
                "valid to check (an empty reconciliation is not a pass); run extract-c-static, "
                "and check validate-callsites for reports rejected by G1a/G1b",
            )
        )
    return findings, totals


def report_findings(findings: list[Finding], counts: Reconciliation) -> int:
    errors = [f for f in findings if f.severity == "error"]
    decisions = [f for f in findings if f.severity == "decision"]
    infos = [f for f in findings if f.severity == "info"]

    if infos:
        print(f"INFO: {len(infos)} non-blocking finding(s)")
        for f in infos:
            print(f"  - {f}")

    print(coverage_statement(counts))

    if errors:
        print(f"FAIL: {len(errors)} blocking finding(s)")
        for f in errors:
            print(f"  - {f}")
        if decisions:
            print(f"plus {len(decisions)} finding(s) awaiting a human risk decision")
            for f in decisions:
                print(f"  - {f}")
        return EXIT_BLOCKED

    if decisions:
        print(f"DECISION REQUIRED: {len(decisions)} medium-risk finding(s) need a human decision")
        for f in decisions:
            print(f"  - {f}")
        print(
            "Raise or lower each tier deliberately (risk_tier_source: human, with its own review "
            "block) and re-run -- an unexamined medium never silently passes"
        )
        return EXIT_DECISION_REQUIRED

    print("OK: R1 and G16 pass")
    return EXIT_OK


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("workspace", type=Path, help="Workspace root holding ci/results/c_static/*.json")
    parser.add_argument(
        "--interactions", action="append", default=[], required=True,
        help="crate_dir=<path to that crate's specs/_interactions>, repeatable. "
             "pipeline.py's gate-r1-g16 derives these from the project descriptor.",
    )
    args = parser.parse_args(argv)

    if not args.workspace.is_dir():
        print(f"error: workspace root does not exist or is not a directory: {args.workspace}", file=sys.stderr)
        return 2
    if not callsite_report_dir_for(args.workspace).is_dir():
        print(
            f"error: no C_static reports at {callsite_report_dir_for(args.workspace)} -- "
            "run extract_c_static.py first; an empty reconciliation is not a pass",
            file=sys.stderr,
        )
        return 2

    mapping: dict[str, Path] = {}
    for entry in args.interactions:
        if "=" not in entry:
            print(f"error: --interactions expects crate_dir=<path>, got {entry!r}", file=sys.stderr)
            return 2
        crate_dir, _, dir_path = entry.partition("=")
        mapping[crate_dir] = Path(dir_path)

    findings, counts = gate_workspace(args.workspace, mapping)
    return report_findings(findings, counts)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
