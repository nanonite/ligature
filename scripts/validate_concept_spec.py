#!/usr/bin/env python3
"""Concept-spec validation, at draft time and at approve time (chainlink
#105).

A concept spec (`<crate_dir>/specs/<snake_case(concept)>.json`) is the root
of this pipeline's obligation vocabulary: its `constraints[].id` values are
what a boundary contract's `callee_guarantees: [TaskQueue.C003]` resolves
against (G2+), its `queries[]` are what a witness spec's G2 cross-reference
resolves against, and its `queries[].witness_required` is the declared
feature set G18 measures coverage over. Until #105 the only way one could
ever exist was `draft 3 concept-to-code`, which stages
`<target>.json.draft` -- and no command in the pipeline could promote it:
`approve`'s dispatcher had no concept-spec branch and answered
`no validator recognizes target`. A `.draft` suffix matches no consumer's
`*.json` glob, so the staged spec was invisible to all of them: G2+ skipped
every `applies_to` it would have verified (silently reporting
`applies_to unverifiable` at info severity instead of the errors it should
have raised), witness `approve` failed closed on `no concept spec under the
search root declares concept ...`, and G18/G19/G20 had no reachable input.
The only way out was hand-writing a file into a `protected_root`, which the
installed skill's authority boundary 5 forbids an agent from doing.

This module is the validation half of the promotion path #105 adds: the
approved form of a concept spec is validated by the SAME extended schema
the draft form is, plus a required `review` block, so `approve` -- and only
`approve`, with a named human `--reviewer` -- can write one.

Why a review block rather than a mechanical `promote-concept`
--------------------------------------------------------------
Evidence records are promoted mechanically (`promote-evidence`, #79)
because they are non-normative: nothing downstream is gated on what an
evidence record says. A concept spec is the opposite -- plan.md §7.2's own
human-sign-off list names "new concepts" first, and the pilot that
reported #105 measured the cost of the gap directly: with no promoted
concept spec, four boundary contracts whose
`callee_guarantee.applies_to` did not cover their callee method passed
G1a/G1b at draft time and `approve`d at exit 0, silently, because the
check that would have rejected them could not resolve the concept.
Promoting a spec no human has seen would let exactly that failure be
authored rather than signed off, so the human checkpoint is kept and the
schema is extended instead -- docs/concept-to-code-modifications.md gap
#7, the same in-memory-extension posture gaps #5 (#33) and #6 (#40)
already established, with the vendored submodule untouched.

The `review` `$def` itself is this pipeline's own, read from
docs/boundary-contract-schema.json's own `$defs.review` at call time rather
than copied: a review block that is one shape in one schema and a
different shape in another would make `accept-promotion`'s provenance check
(#82), which reads the block off accepted artifacts, answer a question
about a shape that artifact does not actually carry.

Deliberately NOT checked here, and why
--------------------------------------
Cross-artifact concept resolution -- "two valid specs declare `TaskQueue`"
-- belongs to `select_pilot_cluster.discover_concepts()`, which already
fails closed on it for every consumer of a concept spec (ambiguous, never
first-wins), and to `validate_witness.resolve_query()` for the per-witness
question. Neither is this module's business, and both consume the promoted
spec independently of how it got there. What this module owns is exactly
the document: G1a against the extended schema, G1b for the one naming rule
nothing else enforces, and the draft-time prohibition on a model
authoring its own `review` block.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resources  # noqa: E402
from schema_utils import make_validator  # noqa: E402
from validate_witness import snake_case  # noqa: E402
from vendored_resources import vendored_resource_path  # noqa: E402

CONCEPT_SPEC_SCHEMA_PATH = vendored_resource_path("concept_to_code_spec_schema")

# The two decided upstream extensions (docs/concept-to-code-modifications.md
# gaps #5 and #6), each read as its own proposed-patch artifact so the local
# copy cannot drift from what is actually being proposed upstream.
WITNESS_REQUIRED_PROPOSAL_PATH = resources.resource_path(
    "docs", "concept-to-code-witness-required-schema.json"
)
CONSTRAINT_ID_PROPOSAL_PATH = resources.resource_path(
    "docs", "concept-to-code-constraint-id-schema.json"
)

# Where this pipeline's own `review` block is defined once (gap #7,
# chainlink #105). Every other artifact schema carries a byte-identical
# `$defs.review`, so the definition is READ from the first of them rather
# than copied into this module: a second hand-copied definition is a second
# answer to "what does a review block look like", and #82's provenance check
# reads the block off artifacts of many schemas at once.
REVIEW_DEFINITION_PATH = resources.resource_path("docs", "boundary-contract-schema.json")


@dataclass
class Finding:
    gate: str
    path: Path
    reason: str
    severity: str = "error"

    def __str__(self) -> str:
        return f"[{self.gate}/{self.severity}] {self.path}: {self.reason}"


def load_concept_spec_schema() -> dict:
    """The real, live vendored schema, read fresh every call
    (`vendor/concept-to-code` is a read-only submodule pin -- this reads
    it, never edits it, and never caches a copy that could go stale
    against it). Unextended: see `load_extended_concept_spec_schema()`
    for the version this pipeline actually validates against."""
    return json.loads(CONCEPT_SPEC_SCHEMA_PATH.read_text())


def review_definition() -> dict:
    """This pipeline's own `review` block shape, read from the schema that
    has always defined it (see REVIEW_DEFINITION_PATH's own note)."""
    schema = json.loads(REVIEW_DEFINITION_PATH.read_text())
    return json.loads(json.dumps(schema["$defs"]["review"]))


def load_extended_concept_spec_schema() -> dict:
    """`load_concept_spec_schema()`'s result, patched so a concept spec
    MAY declare `queries[].witness_required` and `constraints[].id` without
    being rejected (chainlink #69's investigation into
    docs/limitations.md finding F3: G2+ and gate g18 already read these
    two fields when present, but this pipeline validated against the bare
    vendored schema, whose `additionalProperties: false` rejects both --
    so no single concept spec could satisfy all three tools at once), and
    MAY carry this pipeline's own `review` block (gap #7, chainlink #105,
    so `approve` can promote a concept spec and the ids it declares stop
    being unverifiable). See this module's docstring for why #105 extends
    the schema rather than adding a mechanical promotion verb.

    `witness_required` is added exactly as chainlink #33 decided it
    upstream: optional, default `false`. `constraint.id` is added as
    OPTIONAL here, even though chainlink #40 decided it should be
    REQUIRED once actually applied upstream
    (docs/concept-to-code-constraint-id-schema.json is the faithful record
    of that decision). That's a deliberate divergence, not an oversight:
    #40's own text warns a required `id` needs "a backfill... onto every
    existing constraint in any project already using concept-to-code"
    before it can be required without breaking every spec written before
    the backfill. Requiring it here, today, would make this rubric reject
    every concept spec that hasn't done that backfill -- a compatibility
    regression, not the fix F3 asks for. A project MAY start assigning
    `id`s now (and should, to get G2+'s concrete guarantee resolution
    instead of its `no_ids_in_spec` non-blocking fallback); nothing here
    requires it yet.

    `review` is OPTIONAL in the extended schema for the same reason the
    other two are: this function backs BOTH the draft-time validator (a
    draft correctly has no review block yet -- `approve` attaches it) and
    the discovery-time validator (which must keep accepting a spec on disk
    however it got there). `load_approved_concept_spec_schema()` is the one
    that requires it."""
    schema = load_concept_spec_schema()
    schema = json.loads(json.dumps(schema))  # deep copy; never mutate the cached vendored read

    witness_required_proposal = json.loads(WITNESS_REQUIRED_PROPOSAL_PATH.read_text())
    schema["$defs"]["query"]["properties"]["witness_required"] = witness_required_proposal[
        "properties"
    ]["witness_required"]

    constraint_id_proposal = json.loads(CONSTRAINT_ID_PROPOSAL_PATH.read_text())
    schema["$defs"]["constraint"]["properties"]["id"] = constraint_id_proposal["properties"]["id"]
    # Deliberately NOT added to $defs["constraint"]["required"] -- see the
    # docstring above.

    schema["$defs"]["review"] = review_definition()
    schema["properties"]["review"] = {"$ref": "#/$defs/review"}

    return schema


def load_approved_concept_spec_schema() -> dict:
    """The extended schema with `review` REQUIRED -- the approved form.
    Every other artifact type in this pipeline carries a required
    `review: {reviewer, reviewed_at}` block, and
    review_checkpoint.approve() is the only thing that attaches one; a
    concept spec that reached its target path any other way has not been
    through the human checkpoint, and G2+/G2/G18 resolve its ids and
    queries as fact, so that is exactly the artifact that must not exist
    un-reviewed (chainlink #105)."""
    schema = load_extended_concept_spec_schema()
    if "review" not in schema["required"]:
        schema["required"].append("review")
    return schema


def load_draft_validator() -> Draft202012Validator:
    """Stage 0/3 immediate feedback: `review` not yet present (and rejected
    outright by `check_no_draft_review` when a model writes one anyway)."""
    return make_validator(load_extended_concept_spec_schema())


def load_validator() -> Draft202012Validator:
    """Approve-time: the approved form, `review` required."""
    return make_validator(load_approved_concept_spec_schema())


def expected_filename(data: dict) -> str:
    """`<snake_case(concept)>.json` -- concept-to-code's OWN snake_case
    (vendor/concept-to-code/emit_stubs.py), copied rather than re-derived
    (plan.md §2: copying beat re-deriving it last time -- HTTPClient ->
    h_t_t_p_client), and the path the vendored schema's own
    `implements[].crate` description says a trait-kind concept is resolved
    at. A spec whose filename disagrees with its own `concept` is invisible
    to `spec_workspace.py`'s lookup and names a different artifact to every
    reader of the directory."""
    return f"{snake_case(data['concept'])}.json"


def gate_g1a(path: Path, data: dict, validator: Draft202012Validator) -> list[Finding]:
    return [Finding("G1a", path, e.message) for e in validator.iter_errors(data)]


def check_naming(path: Path, data: dict) -> list[Finding]:
    expected = expected_filename(data)
    if path.name != expected:
        return [
            Finding(
                "G1b", path,
                f"filename {path.name!r} does not match this spec's own concept "
                f"{data['concept']!r} ({expected!r}) -- concept specs are named by their "
                "concept, using concept-to-code's own snake_case, and that name is what "
                "resolves them",
            )
        ]
    return []


def check_no_draft_review(path: Path, data: dict) -> list[Finding]:
    """A Stage 0/3 draft must never carry its own `review` block --
    review_checkpoint.approve() is the only path that attaches one, after
    an explicit, non-empty human reviewer signs off (see its own
    docstring). A model that authors `review` itself is asserting a
    sign-off that never happened, and for a concept spec that sign-off is
    load-bearing: it is what makes `accept-promotion` (#82) able to prove
    the spec a promotion was built over was reviewed."""
    if "review" in data:
        return [
            Finding(
                "G1b", path,
                "draft must not include its own `review` block -- review is only "
                "attached by approve(), after a human reviewer signs off",
            )
        ]
    return []


def validate_draft_data(path: Path, data: dict, validator: Draft202012Validator) -> list[Finding]:
    """Stage 0/3 immediate feedback (plan.md §6.1). `validator` must come
    from load_draft_validator(), not load_validator() -- `review` isn't
    required yet at draft time, and a model-supplied one is refused rather
    than accepted. Deliberately excludes everything cross-artifact: which
    concept a `callee_guarantee` or a witness query resolves to depends on
    OTHER specs and is checked at approve time and by the gates, not here
    (the same boundary every other validate_<type>.py module draws)."""
    review_check = check_no_draft_review(path, data)
    if review_check:
        return review_check
    g1a = gate_g1a(path, data, validator)
    if g1a:
        return g1a
    return check_naming(path, data)


def validate_data(path: Path, data: dict, validator: Draft202012Validator) -> list[Finding]:
    """Approve-time. `validator` must come from load_validator(), whose
    schema requires `review`: `data` here already carries the block
    review_checkpoint.approve() injected, so a missing one is a dispatcher
    bug, not a draft to be corrected."""
    g1a = gate_g1a(path, data, validator)
    if g1a:
        return g1a
    return check_naming(path, data)