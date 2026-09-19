#!/usr/bin/env python3
"""Witness validation: G1a (schema), G1b (repo semantics), G2 (the query
must resolve to a pure query).

plan.md §16.1, chainlink #27. Two artifact types, on opposite sides of
this codebase's line and validated by one module because the second is
only meaningful against the first:

    specs/_witnesses/<snake_case(concept)>.<query>.json   NORMATIVE, reviewed
    ci/results/witnesses/<witness_id>.json                GENERATED, machine-emitted

  G1a -- draft-2020-12 validation against docs/witness-spec-schema.json
         and docs/witness-result-schema.json.
  G1b -- repo semantics for the spec:
         * flat inside a _witnesses/ directory; filename stem is
           `<snake_case(concept)>.<query>` using concept-to-code's OWN
           snake_case (vendor/concept-to-code/emit_stubs.py), copied not
           re-derived -- plan.md §2 records what re-deriving it cost
           (HTTPClient -> h_t_t_p_client).
         * witness_id unique across the workspace.
         * expectation.renderer == renderer, and output.renderer_actual
           == renderer. The second is plan.md §16.2's declared/actual
           comparison, and it is self-contained inside one document, so
           it belongs here rather than in a gate. HARD-FAILING AT
           GENERATION on that mismatch is the renderer contract
           (chainlink #28); this is the same fact checked on the artifact
           that records it.
         * every witness declaring one fixture_family declares the same
           coverage_region -- plan.md §16.2's "coverage_region
           inconsistent with fixture_family", made checkable without a
           registry artifact nobody writes (see the schema's own note).
         and for the result:
         * value_domain and value_hash RECOMPUTED and rejected on
           disagreement -- a stored hash is a claim, and this one is
           normative (the same computed-never-stored discipline G1b
           applies to eligibility in I and coverage in a C_static report).
         * grid cells unique and sorted; every measured value a canonical
           decimal string.
  G2  -- reference integrity: `query` must resolve to a query with
         `pure: true` in the concept's own spec under the crate's
         specs_search_root. Mirrors validate_boundary_contracts.py's
         `_resolve_constraint` exactly, including its ambiguous /
         spec_not_found statuses and its D3 lesson (match on the spec's
         own `concept` field, never on a filename). A witness over a
         non-pure method would be observing a side effect and calling it
         a value.

What this module does NOT do, deliberately:
  * G18 witness coverage (#29) -- every `witness_required` query has a
    witness. That needs the declared feature set, and `witness_required`
    is not in the vendored concept-to-code schema yet (#33, reported
    upstream in docs/concept-to-code-modifications.md gap #5, never
    applied to the submodule directly).
  * G19 determinism (#30) -- regenerate and compare against the spec's
    declared value_hash. This module checks a result against ITSELF; the
    comparison across a regeneration is that gate's.
  * G20 degeneracy (#31) -- value_distribution vs the actual domain,
    which reads value_domain.distinct_values from a result validated
    here.

And the rule none of it may bend: a witness is EVIDENCE, never assurance
(plan.md §16, §16.4). Nothing here feeds satisfies(), accepted_evidence_kinds,
or closure_kind, and a cluster with every witness green and no verifier
result is still `unsupported`.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resources  # noqa: E402
from scan_summary import pass_line  # noqa: E402
from schema_utils import make_validator  # noqa: E402
from schema_utils import make_validator_without_required  # noqa: E402
from witness_result import HASHED_FIELDS  # noqa: E402
from witness_result import compute_value_domain  # noqa: E402
from witness_result import compute_value_hash  # noqa: E402
from witness_result import is_canonically_encoded  # noqa: E402
from witness_result import values_of  # noqa: E402
from witness_result import witness_result_dir_for  # noqa: E402

DOCS = resources.resource_path("docs")
SPEC_SCHEMA_PATH = DOCS / "witness-spec-schema.json"
RESULT_SCHEMA_PATH = DOCS / "witness-result-schema.json"
CANONICAL_DIR_NAME = "_witnesses"


def snake_case(name: str) -> str:
    """concept-to-code's own rule, copied verbatim from
    vendor/concept-to-code/emit_stubs.py:snake_case rather than
    re-derived. plan.md §2: "copy the rule, do not re-derive it" -- an
    earlier re-derivation in this codebase broke on acronym concepts
    (HTTPClient -> h_t_t_p_client). The submodule is read-only, and this
    is a five-line function; importing across a vendored tree's import
    graph to get it would couple this validator to emit_stubs.py's own
    dependencies for no gain."""
    out: list[str] = []
    for i, ch in enumerate(name):
        if ch.isupper() and i and (not name[i - 1].isupper()):
            out.append("_")
        out.append(ch.lower())
    return "".join(out).replace("-", "_")


@dataclass
class Finding:
    gate: str
    path: Path
    reason: str
    severity: str = "error"

    def __str__(self) -> str:
        return f"[{self.gate}/{self.severity}] {self.path}: {self.reason}"


def load_spec_schema() -> dict:
    return json.loads(SPEC_SCHEMA_PATH.read_text())


def load_result_schema() -> dict:
    return json.loads(RESULT_SCHEMA_PATH.read_text())


def load_validator() -> Draft202012Validator:
    return make_validator(load_spec_schema())


def load_draft_validator() -> Draft202012Validator:
    return make_validator_without_required(load_spec_schema(), "review")


def load_result_validator() -> Draft202012Validator:
    return make_validator(load_result_schema())


def witness_promotion_digest(data: dict) -> str:
    """The promotion-integrity hash for a witness spec (plan.md §16.5,
    chainlink #35): canonical JSON (sorted keys, compact separators,
    the same canonical-json-v1 style scripts/witness_result.py's own
    result hashing and generate_feature_ledger.py's own artifact
    hashing already use) over the ENTIRE witness spec with only
    `output.render_hash` excluded.

    `render_hash` is change-tracking only and never gates promotion
    (plan.md §16.1) -- a rendering-only regeneration (a renderer-library
    formatting change, a re-run producing the identical picture) must
    never revoke promotion acceptance. Nothing else is excluded: fixture
    identity (`fixture.fixture_id`/`fixture.seed`), `renderer`,
    `expectation`, `output.path`, `output.renderer_actual`, and the
    normative `determinism.value_hash` all stay covered, so a fixture or
    value change DOES invalidate the receipt, exactly as plan.md §16.5
    requires -- this is a targeted exclusion of one non-normative field,
    not a general weakening of what "the witness's content" means.

    Reused verbatim by both generate_promotion_receipt.py (receipt
    generation) and validate_promotion_receipt.py (receipt validation)
    so the two can never silently disagree about what invalidates a
    witness's entry in artifact_manifest."""
    reduced = copy.deepcopy(data)
    reduced.get("output", {}).pop("render_hash", None)
    canonical = json.dumps(reduced, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def witness_dir_for(crate: dict, workspace: Path) -> Path:
    """plan.md §16.1's own path: <crate_dir>/specs/_witnesses/. Crate-
    scoped, unlike the workspace-level closure and gold-set directories:
    a witness is over ONE concept's query, and a concept lives in a
    crate, exactly as its boundary contracts and interactions do."""
    return (workspace / crate["crate_dir"] / "specs" / CANONICAL_DIR_NAME).resolve()


def expected_stem(data: dict) -> str:
    return f"{snake_case(data['concept'])}.{data['query']}"


def gate_g1a(path: Path, data: dict, validator: Draft202012Validator) -> list[Finding]:
    return [Finding("G1a", path, e.message) for e in validator.iter_errors(data)]


def check_naming(path: Path, data: dict) -> list[Finding]:
    findings: list[Finding] = []
    if path.parent.name != CANONICAL_DIR_NAME:
        findings.append(
            Finding(
                "G1b", path,
                f"not flat -- must live directly inside a {CANONICAL_DIR_NAME}/ directory, found "
                f"under {path.parent}",
            )
        )
    if path.suffix != ".json":
        findings.append(Finding("G1b", path, f"must be a .json file, found suffix {path.suffix!r}"))
    expected = expected_stem(data)
    if path.stem != expected:
        findings.append(
            Finding(
                "G1b", path,
                f"filename stem {path.stem!r} does not match "
                f"<snake_case(concept)>.<query> ({expected!r})",
            )
        )
    return findings


def check_renderer_consistency(path: Path, data: dict) -> list[Finding]:
    """The declared/actual comparison plan.md §16.2 requires, on the
    document that carries both. A renderer that silently substituted a
    degraded fallback -- a text strip standing in for a field plot --
    would have to record that it did, and this is where the record is
    read."""
    findings: list[Finding] = []
    declared = data["renderer"]
    if data["expectation"]["renderer"] != declared:
        findings.append(
            Finding(
                "G1b", path,
                f"expectation.renderer {data['expectation']['renderer']!r} does not match renderer "
                f"{declared!r} -- the duplication in plan.md §16.1's worked example is only safe if "
                "it is checked",
            )
        )
    actual = data["output"]["renderer_actual"]
    if actual != declared:
        findings.append(
            Finding(
                "G1b", path,
                f"output.renderer_actual {actual!r} is not the declared renderer {declared!r} -- a "
                "renderer must never silently substitute a degraded rendering; hard-failing at "
                "generation is the renderer contract (chainlink #28), and this artifact recording "
                "the substitution is how it stops being invisible",
            )
        )
    return findings


def check_output_path(path: Path, data: dict) -> list[Finding]:
    expected = f"docs/witnesses/{expected_stem(data)}.svg"
    if data["output"]["path"] != expected:
        return [
            Finding(
                "G1b", path,
                f"output.path {data['output']['path']!r} is not this witness's own generated "
                f"location {expected!r} (plan.md §16.1)",
            )
        ]
    return []


def query_name_from_sig(rust_sig: str) -> str:
    """concept-to-code embeds a query's name in `rust_sig` (plan.md §2's
    "de facto id"; a query has no name field of its own). Factored out
    of resolve_query so chainlink #29's G18 can discover the declared
    witness_required set with the identical extraction, rather than
    re-deriving it and risking the two silently disagreeing about what a
    query is even called."""
    return rust_sig.split("(", 1)[0].replace("fn", "", 1).strip() if "(" in rust_sig else ""


def resolve_query(concept: str, query: str, specs_search_root: Path | None) -> tuple[str, dict | None]:
    """Resolve <Concept>.<query> to a query dict in the concept's spec.

    Mirrors validate_boundary_contracts.py's `_resolve_constraint`,
    including its statuses and the D3 lesson behind them: match on the
    spec's own `concept` field, never on a filename, and sort the scan so
    a result never depends on filesystem order. A query has no name field
    of its own -- concept-to-code embeds it in `rust_sig` (plan.md §2's
    "de facto id"), so that is where the name is read from."""
    if specs_search_root is None:
        return "no_search_root", None
    if not specs_search_root.is_dir():
        return "spec_not_found", None

    matches = []
    for spec_path in sorted(specs_search_root.glob("**/*.json")):
        if any(part.startswith("_") for part in spec_path.relative_to(specs_search_root).parts):
            # Every artifact directory in this codebase is underscore-
            # prefixed (_boundaries, _interactions, _bridges, _witnesses,
            # ...), and a witness spec carries its own top-level
            # `concept` field -- so an unfiltered scan finds the witness
            # itself and reports its own concept as ambiguous.
            # validate_boundary_contracts.py's version of this scan never
            # hit that only because a boundary contract has no top-level
            # `concept`.
            continue
        try:
            spec = json.loads(spec_path.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(spec, dict) or spec.get("concept") != concept:
            continue
        if not any(key in spec for key in ("queries", "commands", "constraints", "english_description")):
            # A document naming a concept is not thereby a concept spec.
            continue
        matches.append(spec)

    if not matches:
        return "spec_not_found", None
    if len(matches) > 1:
        return "ambiguous", None

    for candidate in matches[0].get("queries", []) or []:
        if query_name_from_sig(candidate.get("rust_sig", "")) == query:
            return "resolved", candidate
    return "dangling", None


def gate_g2(path: Path, data: dict, specs_search_root: Path | None) -> list[Finding]:
    concept, query = data["concept"], data["query"]
    status, candidate = resolve_query(concept, query, specs_search_root)

    if status == "no_search_root":
        return [
            Finding(
                "G2", path,
                "the query cross-reference was not checked -- no specs_search_root was supplied to "
                "this validator run",
                severity="info",
            )
        ]
    if status == "spec_not_found":
        return [Finding("G2", path, f"no concept spec under the search root declares concept {concept!r}")]
    if status == "ambiguous":
        return [
            Finding(
                "G2", path,
                f"more than one spec under the search root declares concept {concept!r} -- which "
                "query this witness is over cannot be determined",
            )
        ]
    if status == "dangling":
        return [
            Finding(
                "G2", path,
                f"concept {concept!r} declares no query named {query!r} (queries are matched by the "
                "function name embedded in rust_sig)",
            )
        ]
    if candidate.get("pure") is not True:
        return [
            Finding(
                "G2", path,
                f"{concept}.{query} is not declared `pure: true` -- a witness over a non-pure "
                "method observes a side effect and calls it a value",
            )
        ]
    return []


def check_no_draft_review(path: Path, data: dict) -> list[Finding]:
    if "review" in data:
        return [
            Finding(
                "G1b", path,
                "draft must not include its own `review` block -- review is only attached by "
                "approve() after a human reviewer signs off",
            )
        ]
    return []


def validate_data(
    path: Path, data: dict, validator: Draft202012Validator, specs_search_root: Path | None = None
) -> list[Finding]:
    g1a = gate_g1a(path, data, validator)
    if g1a:
        return g1a
    findings = check_naming(path, data)
    findings.extend(check_renderer_consistency(path, data))
    findings.extend(check_output_path(path, data))
    findings.extend(gate_g2(path, data, specs_search_root))
    return findings


def validate_draft_data(
    path: Path, data: dict, validator: Draft202012Validator
) -> list[Finding]:
    """Stage 0/3 immediate feedback. Excludes G2 (needs a search root, a
    cross-file concern) -- mirrors every other validate_<type>.py."""
    review_check = check_no_draft_review(path, data)
    if review_check:
        return review_check
    g1a = gate_g1a(path, data, validator)
    if g1a:
        return g1a
    findings = check_naming(path, data)
    findings.extend(check_renderer_consistency(path, data))
    findings.extend(check_output_path(path, data))
    return findings


def validate_file(
    path: Path, validator: Draft202012Validator, specs_search_root: Path | None = None
) -> list[Finding]:
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
    return validate_data(path, data, validator, specs_search_root)


def check_family_consistency(specs: list[tuple[Path, dict]]) -> list[Finding]:
    """plan.md §16.2's "coverage_region inconsistent with fixture_family".

    A fixture family is defined by the witnesses that declare it (see the
    schema's own note on why there is no registry artifact), so the
    checkable property is internal consistency: two witnesses in one
    family that disagree about the region the family covers cannot both
    be right, and a later degeneracy check would be testing against a
    declaration that contradicts itself.

    Kept here, alongside check_unique_ids, since it is naturally a
    witness-collection-level check -- but chainlink #31's gate_g20.py is
    its only caller now, at WARN severity, workspace-wide. Neither
    validate_crate() nor validate() call it any more (external review,
    medium severity: they used to, which hard-failed this exact defect
    as G1b/error at ordinary Stage 4 validation before G20 could ever
    report it as its own Stage 4.5 warning -- one defect must not carry
    two disagreeing dispositions). The "G1b" gate tag on the Finding
    below is vestigial from that history; gate_g20.py discards it and
    always re-tags its own copy "G20"."""
    regions: dict[str, tuple[Path, str]] = {}
    findings: list[Finding] = []
    for path, data in specs:
        family = data["expectation"]["fixture_family"]
        region = data["expectation"]["coverage_region"]
        if family not in regions:
            regions[family] = (path, region)
            continue
        first_path, first_region = regions[family]
        if region != first_region:
            findings.append(
                Finding(
                    "G1b", path,
                    f"fixture_family {family!r} is declared with coverage_region {region!r} here "
                    f"and {first_region!r} at {first_path} -- a family whose members disagree about "
                    "the region it covers is not a declaration a degeneracy check can test against",
                )
            )
    return findings


def check_unique_ids(specs: list[tuple[Path, dict]]) -> list[Finding]:
    seen: dict[str, Path] = {}
    findings: list[Finding] = []
    for path, data in specs:
        witness_id = data["witness_id"]
        if witness_id in seen:
            findings.append(
                Finding("G1b", path, f"witness_id {witness_id!r} is already used by {seen[witness_id]}")
            )
            continue
        seen[witness_id] = path
    return findings


def find_witness_files(root: Path) -> list[Path]:
    if not root.is_dir():
        raise FileNotFoundError(f"witness scan root does not exist or is not a directory: {root}")
    return [p for p in root.glob(f"**/{CANONICAL_DIR_NAME}/**/*") if p.is_file()]


def count_discovered(root: Path) -> int:
    """The candidate set this module's own scan walks, counted for the
    honest pass line (chainlink #48)."""
    return len(find_witness_files(root))


def validate_crate(
    crate_root: Path, canonical_dir: Path, specs_search_root: Path | None = None
) -> list[Finding]:
    """Discover crate-wide, then reject by location -- the same
    discover-then-reject shape every validate_<type>.py module uses, and
    for the same reason: anchoring the scan to only the canonical
    directory would stop a mislocated witness from being wrongly trusted
    AND from ever being looked at.

    Does NOT call check_family_consistency (external review, chainlink
    #31, medium severity: it used to, which hard-failed a same-crate
    fixture-family/coverage_region disagreement as G1b/error right here
    at ordinary Stage 4 validation -- before gate_g20.py's own G20 could
    ever report the identical defect as its intended Stage 4.5 WARNING.
    One defect, two disagreeing dispositions, is not the two-stage
    lifecycle the gate table describes. gate_g20.py is this check's only
    caller now, workspace-wide rather than per-crate, since a
    fixture_family name carries no crate namespace to scope it by)."""
    canonical_resolved = canonical_dir.resolve()
    validator = load_validator()
    findings: list[Finding] = []
    valid: list[tuple[Path, dict]] = []

    for path in sorted(find_witness_files(crate_root)):
        if path.resolve().parent != canonical_resolved:
            findings.append(
                Finding(
                    "G1b", path,
                    f"witness spec is not directly under the canonical directory "
                    f"{canonical_resolved} -- found under {path.resolve().parent}",
                )
            )
            continue
        file_findings = validate_file(path, validator, specs_search_root)
        findings.extend(file_findings)
        if not any(f.severity == "error" for f in file_findings):
            valid.append((path, json.loads(path.read_text())))

    findings.extend(check_unique_ids(valid))
    return findings


def validate(root: Path, specs_search_root: Path | None = None) -> list[Finding]:
    """Standalone CLI entry: an unanchored recursive scan, since this CLI
    has no crate-boundary concept to derive one from (the same position
    scripts/validate_interaction.py's own standalone CLI is in). Does
    NOT call check_family_consistency either -- see validate_crate()'s
    own docstring; gate_g20.py is its only caller now."""
    validator = load_validator()
    findings: list[Finding] = []
    valid: list[tuple[Path, dict]] = []
    for path in sorted(find_witness_files(root)):
        file_findings = validate_file(path, validator, specs_search_root)
        findings.extend(file_findings)
        if not any(f.severity == "error" for f in file_findings):
            valid.append((path, json.loads(path.read_text())))
    findings.extend(check_unique_ids(valid))
    return findings


# --------------------------------------------------------------------------
# Canonical results
# --------------------------------------------------------------------------

def validate_result_data(path: Path, data: dict, validator: Draft202012Validator) -> list[Finding]:
    """A result is checked against ITSELF here: every measured value is a
    canonical decimal, the grid is unique and sorted, and value_domain and
    value_hash are RECOMPUTED. Comparing it to a witness spec's declared
    value_hash across a regeneration is G19's job (chainlink #30)."""
    g1a = gate_g1a(path, data, validator)
    if g1a:
        return g1a

    findings: list[Finding] = []
    if path.stem != data["witness_id"]:
        findings.append(
            Finding(
                "G1b", path,
                f"filename stem {path.stem!r} does not match witness_id {data['witness_id']!r}",
            )
        )

    declared_fields = set(data["canonicalization"]["hashed_fields"])
    if declared_fields != set(HASHED_FIELDS):
        findings.append(
            Finding(
                "G1b", path,
                f"canonicalization.hashed_fields {sorted(declared_fields)!r} does not match what "
                f"rule {data['canonicalization']['rule']!r} actually hashes "
                f"({sorted(HASHED_FIELDS)!r}) -- the schema allows any subset, but a document is not "
                "free to CHOOSE which fields backed its own value_hash after the fact; a document "
                "claiming a smaller field set than what was actually hashed would defeat the whole "
                "point of recording the field list (external review, medium severity)",
            )
        )

    result = data["result"]
    for value in values_of(result):
        if not is_canonically_encoded(value):
            findings.append(
                Finding(
                    "G1b", path,
                    f"measured value {value!r} is not canonically encoded -- either not a canonical "
                    "decimal string at all, or a spelling canonical_number() would never itself "
                    "produce for that value (e.g. '1e1' for ten, which it spells '10'); admitting a "
                    "second valid spelling of one number would let distinct_values overcount a "
                    "constant result and let two producers of the same values hash differently",
                )
            )

    if result["kind"] == "grid":
        rows, columns = result["rows"], result["columns"]
        keys = [(cell["row"], cell["column"]) for cell in result["cells"]]
        if len(set(keys)) != len(keys):
            findings.append(Finding("G1b", path, "grid has more than one value for the same cell"))
        if keys != sorted(keys):
            findings.append(
                Finding(
                    "G1b", path,
                    "grid cells are not sorted by (row, column) -- an unsorted grid makes the hash "
                    "depend on the producer's iteration order",
                )
            )
        out_of_bounds = [key for key in keys if not (0 <= key[0] < rows and 0 <= key[1] < columns)]
        if out_of_bounds:
            # encode_grid() (the Python producer helper) already refuses
            # this; nothing enforced it against an arbitrary on-disk
            # result -- a hand-edited or non-Python-produced file could
            # declare a 1x1 grid and still carry a cell at (9, 0) with
            # zero findings (external review, medium severity, reproduced
            # exactly this way).
            findings.append(
                Finding(
                    "G1b", path,
                    f"grid declares {rows}x{columns} but has cell(s) outside that range: "
                    f"{sorted(out_of_bounds)!r}",
                )
            )

    if not findings:
        computed_domain = compute_value_domain(result)
        if data["value_domain"] != computed_domain:
            findings.append(
                Finding(
                    "G1b", path,
                    f"value_domain {data['value_domain']!r} disagrees with the domain recomputed "
                    f"from the result {computed_domain!r} -- the domain is computed, never "
                    "stored-and-trusted, because a degeneracy check reads it",
                )
            )
        computed_hash = compute_value_hash(data)
        if data["value_hash"] != computed_hash:
            findings.append(
                Finding(
                    "G1b", path,
                    f"value_hash {data['value_hash']} is not the hash of this result "
                    f"({computed_hash}) -- a stored hash is a claim, and this one is normative",
                )
            )
    return findings


def validate_result_file(path: Path, validator: Draft202012Validator) -> list[Finding]:
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return [Finding("G1a", path, f"not readable JSON: {e}")]
    if not isinstance(data, dict):
        return [Finding("G1a", path, "top-level value is not a JSON object")]
    return validate_result_data(path, data, validator)


def find_result_files(workspace: Path) -> list[Path]:
    directory = witness_result_dir_for(workspace)
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.is_file())


def count_results(workspace: Path) -> int:
    return len(find_result_files(workspace))


def validate_results(workspace: Path) -> list[Finding]:
    validator = load_result_validator()
    findings: list[Finding] = []
    for path in find_result_files(workspace):
        findings.extend(validate_result_file(path, validator))
    return findings


def load_results_by_witness(workspace: Path) -> dict[str, dict]:
    """Every fully valid canonical result, by witness_id -- the "genuinely
    valid, not just present" bar. G19 (#30) and G20 (#31) consume this, so
    a result whose own hash does not check out can never become the thing
    a gate compares against."""
    validator = load_result_validator()
    results: dict[str, dict] = {}
    for path in find_result_files(workspace):
        if any(f.severity == "error" for f in validate_result_file(path, validator)):
            continue
        data = json.loads(path.read_text())
        results.setdefault(data["witness_id"], data)
    return results


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=Path, help="Workspace or crate root to scan for **/_witnesses/**/*.json")
    parser.add_argument(
        "--specs-search-root",
        type=Path,
        default=None,
        help="Root to resolve the concept spec's queries from, for the G2 pure-query check (optional).",
    )
    parser.add_argument(
        "--results", action="store_true",
        help="Also validate the canonical results under ci/results/witnesses/",
    )
    args = parser.parse_args(argv)

    try:
        findings = validate(args.root, args.specs_search_root)
        discovered = count_discovered(args.root)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.results:
        findings = findings + validate_results(args.root)
        discovered += count_results(args.root)

    errors = [f for f in findings if f.severity == "error"]
    infos = [f for f in findings if f.severity == "info"]

    if infos:
        print(f"INFO: {len(infos)} non-blocking finding(s)")
        for finding in infos:
            print(f"  - {finding}")

    if not errors:
        print(pass_line(discovered, "witness artifacts", "G1a/G1b and G2 pure-query resolution", args.root))
        return 0

    print(f"FAIL: {len(errors)} finding(s)")
    for finding in errors:
        print(f"  - {finding}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
