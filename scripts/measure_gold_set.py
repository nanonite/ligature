#!/usr/bin/env python3
"""Gold-set measurement: precision, recall, and omission.

plan.md §5.2's independence argument and §13 item 13, chainlink #26.

Why this exists, in plan.md's own words: "R2 proves coverage relative to
accepted I; it does not prove I complete. Since Stage 3 generates both I
and O, independent candidate sources are mandatory." R2 can pass on an
interaction set that is missing half the edges that really exist, and
nothing inside the pipeline would notice, because every gate downstream
of Stage 3 reasons *relative to* the accepted I. This module is the
measurement that makes that failure visible, against a human gold set
the pipeline did not produce.

Three numbers, and the third is the point
-----------------------------------------
  precision  proposed edges (I, within the curated scope) that the gold
             set also contains.
  recall     gold edges that I proposed.
  omission   gold edges that NO source proposed -- not I, not any
             available independent candidate source. Precision and
             recall score the proposals that were made; omission counts
             the ones nobody made, and it is the number that makes "R2
             passed, therefore I is complete" unreadable.

A fourth, reported beside omission because the two failures are
different: `missed_but_proposed` -- gold edges absent from I that an
independent source DID propose. The signal existed and the pipeline did
not use it. That is a fixable pipeline defect; omission is a gap in the
candidate sources themselves.

Direction discipline
--------------------
An edge proposed by C_static that is NOT in I is R1's territory
(chainlink #24's gate-r1-g16: "realized call absent from I"), and it is
deliberately not counted here. #26 runs the other way: gold edges nobody
proposed. The two are kept apart in the schema, in this module, and in
the report -- conflating them would let a good R1 score read as evidence
about completeness, which is exactly the confusion this issue exists to
prevent.

Which candidate sources are real, honestly
------------------------------------------
plan.md §5.2 names seven independent sources. Two of them exist as
tooling in this codebase today; the rest do not, and this module records
each one's status rather than letting an unbuilt source contribute a
silent zero:

  c-static-extraction        AVAILABLE (chainlink #24) -- realized
                             cross-concept calls from a C_static report.
  human-promotion            AVAILABLE -- the gold set itself is a
                             human's proposal, so it is listed as a
                             source that proposed every gold edge, and
                             excluded from corroboration (a gold set
                             corroborating itself would make omission
                             structurally zero).
  s-graph                    NOT BUILT -- gen_concept_graph.py is not in
                             this repository.
  tests-and-traces           NOT BUILT.
  data-flow-analysis         NOT BUILT.
  requirements               NOT BUILT.
  critic-pass                NOT BUILT.
  mode-p-source-extraction   NOT APPLICABLE to a greenfield (mode-r)
                             workspace; it is the Mode P track's own
                             source, chainlink #5.
  interaction-set            AVAILABLE, and NOT independent: it is the
                             artifact under audit. Listed so a reader
                             can see it was not counted as corroboration.

So an omission count from this prototype is a LOWER BOUND computed
against one independent source, over a gold set that is itself a human
lower bound. Every report says so in `sources[]` and every printed
summary says so in words. A number that looks like a verdict, and is
not, is worse than no number.

This is a measurement, not a gate
---------------------------------
A non-zero omission count does not block anything: it is a finding about
the pipeline's proposal quality that a human reads. What DOES fail is
being unable to measure -- no gold set, a gold set for a track this
workspace is not running, or a scope in which nothing was proposed so
precision is undefined. A measurement that cannot be computed must never
read as a good score (chainlink #48's discipline, applied to an audit
rather than a validator).
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from project_descriptor import interaction_dir_for  # noqa: E402
from project_descriptor import load_project_descriptor  # noqa: E402
from schema_utils import make_validator  # noqa: E402
from validate_callsites import load_reports as load_callsite_reports  # noqa: E402
from validate_gold_set import edge_label  # noqa: E402
from validate_gold_set import load_gold_sets  # noqa: E402
from validate_interaction import load_interactions_by_id  # noqa: E402

DOCS = Path(__file__).resolve().parent.parent / "docs"
MEASUREMENT_SCHEMA_PATH = DOCS / "gold-set-measurement-schema.json"

MEASUREMENT_DIR = ("ci", "results", "gold_set")

TRACK_BY_MODE = {"greenfield": "mode-r", "port": "mode-p"}

INDEPENDENT_SOURCES = (
    "c-static-extraction",
    "s-graph",
    "tests-and-traces",
    "data-flow-analysis",
    "requirements",
    "critic-pass",
    "mode-p-source-extraction",
)

NOT_BUILT = {
    "s-graph": "gen_concept_graph.py is not in this repository (plan.md §5's S column)",
    "tests-and-traces": "no test/trace ingestion exists yet",
    "data-flow-analysis": "no data-flow analysis exists yet",
    "requirements": "no requirements ingestion exists yet",
    "critic-pass": "no separate critic pass exists yet",
}

EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_INPUT_ERROR = 2

Edge = tuple[str, str, str, str]


@dataclass
class Finding:
    subject: str
    reason: str
    severity: str = "error"  # "error" | "info"

    def __str__(self) -> str:
        return f"[#26/{self.severity}] {self.subject}: {self.reason}"


@dataclass
class SourceResult:
    source: str
    status: str
    independent: bool
    edges: set = field(default_factory=set)
    detail: str = ""

    def as_json(self) -> dict:
        payload = {
            "source": self.source,
            "status": self.status,
            "independent": self.independent,
            "proposed": len(self.edges),
        }
        if self.detail:
            payload["detail"] = self.detail
        return payload


def measurement_dir_for(workspace: Path) -> Path:
    return workspace.joinpath(*MEASUREMENT_DIR).resolve()


def load_measurement_validator():
    return make_validator(json.loads(MEASUREMENT_SCHEMA_PATH.read_text()))


def _role(concept: str, method: str) -> dict:
    return {"concept": concept, "method": method}


def _edge_json(edge: Edge, proposed_by: list[str], rationale: str | None = None) -> dict:
    payload = {
        "caller": _role(edge[0], edge[1]),
        "callee": _role(edge[2], edge[3]),
        "proposed_by": sorted(proposed_by),
    }
    if rationale:
        payload["rationale"] = rationale
    return payload


def _label(edge: Edge) -> str:
    return f"{edge[0]}.{edge[1]} -> {edge[2]}.{edge[3]}"


def interaction_edges(workspace: Path, descriptor: dict) -> set:
    """Every edge the accepted interaction set proposes -- the artifact
    under audit. Only fully valid, reviewed interactions count, the same
    bar every cross-reference in this pipeline applies."""
    edges: set = set()
    for crate in descriptor["crates"]:
        for interaction in load_interactions_by_id(interaction_dir_for(crate, workspace)).values():
            edges.add(
                (
                    interaction["caller"]["concept"],
                    interaction["caller"]["method"],
                    interaction["callee"]["concept"],
                    interaction["callee"]["method"],
                )
            )
    return edges


def c_static_edges(workspace: Path) -> tuple[set, int]:
    """Realized cross-concept calls from the C_static observation
    (chainlink #24). Intra-concept calls are excluded for the same reason
    gate-r1-g16 treats them as internal helpers -- they are not
    interaction edges -- and the unresolved classes are excluded because
    they name no callee concept at all: a possible-dispatch cannot
    propose an edge, only the absence of one.

    Returns (edges, reports read), so an empty result can be told apart
    from no observation at all."""
    edges: set = set()
    reports = load_callsite_reports(workspace)
    for _, report in reports:
        for callsite in report["callsites"]:
            if callsite["call_class"] != "definite-direct-call":
                continue
            caller, callee = callsite["caller"], callsite["callee"]
            if caller["concept"] == callee["concept"]:
                continue
            edges.add((caller["concept"], caller["method"], callee["concept"], callee["method"]))
    return edges, len(reports)


def build_sources(workspace: Path, descriptor: dict, gold_edges: dict, track: str) -> list[SourceResult]:
    """Every source plan.md §5.2 names, with its real status here. An
    unbuilt source contributes `not-built`, never a silent zero -- "no
    source proposed this edge" must not quietly mean "the one source we
    built did not propose it"."""
    results: list[SourceResult] = []

    results.append(
        SourceResult(
            "interaction-set", "available", False, interaction_edges(workspace, descriptor),
            "the artifact under audit; never counted as corroboration",
        )
    )

    c_static, report_count = c_static_edges(workspace)
    results.append(
        SourceResult(
            "c-static-extraction",
            "available" if report_count else "not-built",
            True,
            c_static,
            f"{report_count} C_static report(s) read (chainlink #24)" if report_count
            else "no C_static report on disk -- run `pipeline.py extract-c-static`",
        )
    )

    results.append(
        SourceResult(
            "human-promotion", "available", False, set(gold_edges),
            "the gold set itself; excluded from corroboration, since a standard that corroborates "
            "itself makes omission structurally zero",
        )
    )

    for source, detail in NOT_BUILT.items():
        results.append(SourceResult(source, "not-built", True, set(), detail))

    results.append(
        SourceResult(
            "mode-p-source-extraction",
            "available" if track == "mode-p" else "not-applicable-to-track",
            True,
            set(),
            "Mode P's own candidate source (chainlink #5); this workspace runs "
            f"{track}" if track != "mode-p" else "Mode P source extraction is not built yet (chainlink #5)",
        )
    )
    return results


def _ratio(numerator: int, denominator: int):
    if denominator == 0:
        return "undefined"
    return round(numerator / denominator, 4)


def measure_cluster(
    workspace: Path, descriptor: dict, cluster: str, gold_set: dict, track: str
) -> tuple[dict, list[Finding]]:
    findings: list[Finding] = []
    scope = gold_set["curation_scope"]
    examined = set(scope["examined_concepts"])

    gold_edges: dict = {}
    for edge in gold_set["edges"]:
        key = (
            edge["caller"]["concept"],
            edge["caller"]["method"],
            edge["callee"]["concept"],
            edge["callee"]["method"],
        )
        gold_edges[key] = edge.get("rationale", "")

    sources = build_sources(workspace, descriptor, gold_edges, track)
    in_scope = lambda edge: edge[0] in examined  # noqa: E731

    proposals_by_edge: dict = {}
    for source in sources:
        for edge in source.edges:
            if in_scope(edge):
                proposals_by_edge.setdefault(edge, set()).add(source.source)

    proposed = {edge for edge in next(s for s in sources if s.source == "interaction-set").edges if in_scope(edge)}
    gold = {edge for edge in gold_edges if in_scope(edge)}
    corroborating = {s.source for s in sources if s.independent and s.status == "available"}

    true_positives = gold & proposed
    false_positives = proposed - gold
    false_negatives = gold - proposed

    omitted = {
        edge for edge in false_negatives
        if not (proposals_by_edge.get(edge, set()) & corroborating)
    }
    missed_but_proposed = false_negatives - omitted

    measurement = {
        "schema_version": "1.0",
        "cluster": cluster,
        "track": track,
        "gold_set_review": {
            "reviewer": gold_set["review"]["reviewer"],
            "reviewed_at": gold_set["review"]["reviewed_at"],
        },
        "scope": {
            "examined_concepts": sorted(examined),
            "completeness_claim": scope["completeness_claim"],
        },
        "sources": [source.as_json() for source in sources],
        "counts": {
            "gold_edges": len(gold),
            "proposed_edges": len(proposed),
            "true_positives": len(true_positives),
            "false_positives": len(false_positives),
            "false_negatives": len(false_negatives),
            "omitted": len(omitted),
            "missed_but_proposed": len(missed_but_proposed),
        },
        "metrics": {
            "precision": _ratio(len(true_positives), len(proposed)),
            "recall": _ratio(len(true_positives), len(gold)),
            "omission_rate": _ratio(len(omitted), len(gold)),
        },
        "omitted_edges": [
            _edge_json(edge, sorted(proposals_by_edge.get(edge, set())), gold_edges[edge])
            for edge in sorted(omitted)
        ],
        "missed_but_proposed_edges": [
            _edge_json(edge, sorted(proposals_by_edge.get(edge, set())), gold_edges[edge])
            for edge in sorted(missed_but_proposed)
        ],
        "unmatched_proposals": [
            _edge_json(edge, sorted(proposals_by_edge.get(edge, set())))
            for edge in sorted(false_positives)
        ],
    }

    if not proposed:
        findings.append(
            Finding(
                cluster,
                "the interaction set proposes nothing within the curated scope, so precision is "
                "undefined -- a pipeline that proposed nothing has not achieved perfect precision",
            )
        )
    if not gold:
        findings.append(
            Finding(
                cluster,
                "no gold edge falls within the curated scope -- nothing was measured",
            )
        )
    for edge in sorted(omitted):
        findings.append(
            Finding(
                cluster,
                f"OMISSION: {_label(edge)} is in the gold set and no candidate source proposed it "
                f"({gold_edges[edge]})",
                "info",
            )
        )
    for edge in sorted(missed_but_proposed):
        findings.append(
            Finding(
                cluster,
                f"MISSED: {_label(edge)} is absent from I but was proposed by "
                f"{sorted(proposals_by_edge.get(edge, set()) & corroborating)!r} -- the signal "
                "existed and the pipeline did not use it",
                "info",
            )
        )
    return measurement, findings


def measure_workspace(
    workspace: Path, descriptor: dict, write: bool = True
) -> tuple[list[dict], list[Finding]]:
    findings: list[Finding] = []
    measurements: list[dict] = []
    track = TRACK_BY_MODE[descriptor["mode"]]
    validator = load_measurement_validator()

    for cluster, (path, gold_set) in sorted(load_gold_sets(workspace).items()):
        if gold_set["track"] != track:
            findings.append(
                Finding(
                    cluster,
                    f"gold set declares track {gold_set['track']!r} but this workspace's descriptor "
                    f"mode {descriptor['mode']!r} is {track} -- measuring it here would score the "
                    "wrong track",
                )
            )
            continue
        measurement, cluster_findings = measure_cluster(workspace, descriptor, cluster, gold_set, track)
        errors = list(validator.iter_errors(measurement))
        if errors:
            findings.append(
                Finding(cluster, f"computed measurement is not schema-valid: {errors[0].message}")
            )
            continue
        findings.extend(cluster_findings)
        measurements.append(measurement)
        if write:
            out = measurement_dir_for(workspace) / f"{cluster}.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(measurement, indent=2) + "\n")
    return measurements, findings


def report(measurements: list[dict], findings: list[Finding], workspace: Path) -> int:
    infos = [f for f in findings if f.severity == "info"]
    errors = [f for f in findings if f.severity == "error"]

    for finding in infos:
        print(f"  - {finding}")

    if not measurements:
        print(
            "FAIL: no gold set could be measured -- an audit with nothing to compare against is "
            f"not a good score (looked under {workspace}/specs/_gold_sets)"
        )
        for finding in errors:
            print(f"  - {finding}")
        return EXIT_BLOCKED

    for measurement in measurements:
        counts, metrics = measurement["counts"], measurement["metrics"]
        available = [s["source"] for s in measurement["sources"] if s["status"] == "available" and s["independent"]]
        print(
            f"cluster {measurement['cluster']!r} ({measurement['track']}): "
            f"precision={metrics['precision']} recall={metrics['recall']} "
            f"omission={counts['omitted']}/{counts['gold_edges']} "
            f"(missed-but-proposed={counts['missed_but_proposed']})"
        )
        print(
            f"  measured against {len(available)} of {len(INDEPENDENT_SOURCES)} independent "
            f"candidate sources ({sorted(available)}); the gold set is itself a "
            f"{measurement['scope']['completeness_claim']}, so this omission count is a lower bound"
        )

    if errors:
        print(f"FAIL: {len(errors)} finding(s) prevented a measurement")
        for finding in errors:
            print(f"  - {finding}")
        return EXIT_BLOCKED

    print(
        f"OK: {len(measurements)} cluster(s) measured. Omission is a finding to read, not a gate: "
        "it says what no candidate source proposed, which is what makes 'R2 passed, therefore I is "
        "complete' unreadable"
    )
    return EXIT_OK


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--descriptor", type=Path, default=None)
    parser.add_argument("--no-write", action="store_true", help="measure without writing ci/results/gold_set/")
    args = parser.parse_args(argv)

    if not args.workspace.is_dir():
        print(f"error: workspace root does not exist or is not a directory: {args.workspace}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    try:
        descriptor = load_project_descriptor(args.descriptor or (args.workspace / "project-descriptor.json"))
    except Exception as e:  # noqa: BLE001 -- ProjectDescriptorError or OSError
        print(f"error: cannot read project descriptor: {e}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    measurements, findings = measure_workspace(args.workspace, descriptor, write=not args.no_write)
    return report(measurements, findings, args.workspace)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
