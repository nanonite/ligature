#!/usr/bin/env python3
"""The Stage 0/3 draft template registry (chainlink #105).

One list, one source of truth, for three surfaces that used to each keep
their own hand-maintained answer and each disagree with the others:

  1. `ligature draft --help`, which named two of the templates it accepts
     (`e.g. evidence-intake, boundary-drafting`) -- a pilot following only
     the help text had no way to learn that nine more existed except by
     guessing names and reading the failure;
  2. `draft`'s own refusal for an unrecognized name, which was a bare
     `no template at <extracted-tmpdir>/prompts/stage-3-bridge-drafting.md`
     -- a path the user never named, in a directory that did not exist when
     they ran the command, naming no alternative;
  3. `ligature init`'s copy of every prompt into `.ligature/prompts/`,
     which `ligature_install.py` listed by hand and had therefore stopped
     copying two templates (#98's closure-profile and degradation
     templates) without anyone noticing -- a `draft` that works on a
     packaged binary and an `init`ed workspace disagreeing about which
     prompts exist is exactly the drift this registry removes.

Each entry carries the stage, the artifact the template drafts, where the
artifact belongs, and which command promotes it -- the promotion verb is
the part a caller cannot guess, and the part chainlink #105's report was
about: a `draft` whose output nothing can promote (`concept-to-code`, for
one, until #105 gave `approve` a concept-spec branch) is a dead end that
nothing states.
"""
from __future__ import annotations

from dataclasses import dataclass

# `draft`'s stage argument is a closed {0, 3} enum (argparse's `choices`),
# unchanged by #105: plan.md §6.1 defines the Stage 0/3 one-shot drafting
# step, and no other stage drafts a single artifact this way.
STAGES = ("0", "3")


@dataclass(frozen=True)
class DraftTemplate:
    stage: str
    name: str
    produces: str
    target: str
    promoted_by: str

    @property
    def filename(self) -> str:
        """The prompt's own filename. Derived, never stored: the registry's
        whole value is that the filename, the name `draft` accepts, and the
        help text cannot be three separate truths."""
        return f"stage-{self.stage}-{self.name}.md"


# Ordered by stage, then by the order the pipeline reaches for them, which
# is the order `draft --help` prints.
DRAFT_TEMPLATES: tuple[DraftTemplate, ...] = (
    DraftTemplate(
        stage="0",
        name="evidence-intake",
        produces="evidence record",
        target="evidence/<id>.json",
        promoted_by="promote-evidence (mechanical -- evidence is non-normative, so no reviewer)",
    ),
    DraftTemplate(
        stage="3",
        name="concept-to-code",
        produces="concept spec",
        target="<crate_dir>/specs/<snake_case(concept)>.json",
        promoted_by="approve (--reviewer; chainlink #105)",
    ),
    DraftTemplate(
        stage="3",
        name="boundary-drafting",
        produces="boundary contract",
        target="<crate_dir>/specs/_boundaries/<boundary_id>.json",
        promoted_by="approve (--reviewer)",
    ),
    DraftTemplate(
        stage="3",
        name="interaction-drafting",
        produces="interaction spec",
        target="<crate_dir>/specs/_interactions/<interaction_id>.json",
        promoted_by="approve --reviewer <name> <interaction> <protocol-debt> (pair, atomic); "
        "approve-exemption-pair for an exemption bootstrap",
    ),
    DraftTemplate(
        stage="3",
        name="bridge-drafting",
        produces="bridge specification",
        target="<crate_dir>/specs/_bridges/<bridge_id>.json",
        promoted_by="approve (--reviewer)",
    ),
    DraftTemplate(
        stage="3",
        name="witness-drafting",
        produces="witness specification",
        target="<crate_dir>/specs/_witnesses/<snake_case(concept)>.<query>.json",
        promoted_by="approve (--reviewer)",
    ),
    DraftTemplate(
        stage="3",
        name="exemption-drafting",
        produces="exemption record",
        target="<crate_dir>/specs/_exemptions/<interaction_id>.json",
        promoted_by="approve --reviewer <name> <interaction> <exemption> (exemption-pair, atomic)",
    ),
    DraftTemplate(
        stage="3",
        name="protocol-debt-drafting",
        produces="protocol-debt record",
        target="<crate_dir>/specs/_protocol_debt/<interaction_id>.json",
        promoted_by="approve --reviewer <name> <interaction> <protocol-debt> (pair, atomic)",
    ),
    DraftTemplate(
        stage="3",
        name="conflict-resolution-drafting",
        produces="conflict-resolution record",
        target="specs/_conflicts/<conflict_id>.json (workspace-level)",
        promoted_by="approve (--reviewer)",
    ),
    DraftTemplate(
        stage="3",
        name="closure-profile-drafting",
        produces="closure profile",
        target="specs/_closure/<cluster>.json (workspace-level)",
        promoted_by="approve (--reviewer)",
    ),
    DraftTemplate(
        stage="3",
        name="degradation-drafting",
        produces="degradation record",
        target="specs/_closure/<cluster>.degradation.json (workspace-level)",
        promoted_by="approve (--reviewer)",
    ),
)


def names_for_stage(stage: str) -> tuple[str, ...]:
    return tuple(t.name for t in DRAFT_TEMPLATES if t.stage == stage)


def find(stage: str, name: str) -> DraftTemplate | None:
    for template in DRAFT_TEMPLATES:
        if template.stage == stage and template.name == name:
            return template
    return None


def prompt_filenames() -> tuple[str, ...]:
    """Every registered prompt's filename, for `init` to copy into
    `.ligature/prompts/` (chainlink #84). Derived from the registry rather
    than hand-listed, so a template added to DRAFT_TEMPLATES is installed
    and one removed from it is uninstalled -- which is how
    `ligature_install.py`'s own list came to omit two shipped templates."""
    return tuple(template.filename for template in DRAFT_TEMPLATES)


def unknown_template_error(stage: str, name: str) -> str:
    """The refusal for a name this registry does not have, built from the
    registry so it always offers the complete set for the stage asked for
    (and says so when the name belongs to the OTHER stage -- `draft 0
    boundary-drafting` and `draft 3 boundary-drafting` are different
    mistakes with different fixes, and the old message could not tell them
    apart).

    The "belongs to the other stage" test is deliberately generous about
    what counts as the name: the registered name, the prompt's filename,
    what it drafts, and where the artifact goes all name the same
    template, and a caller who reached for any of the four gets the same
    answer."""
    other_stage = next((s for s in STAGES if s != stage), None)
    elsewhere = [
        template.name
        for template in DRAFT_TEMPLATES
        if template.stage != stage
        and name in {template.name, template.filename, template.produces, template.target}
    ]
    lines = [f"no stage-{stage} template named {name!r} -- `ligature draft --help` lists every one."]
    lines.append(f"stage {stage} templates: {', '.join(names_for_stage(stage))}")
    if elsewhere:
        lines.append(
            f"{name!r} is a stage-{other_stage} template, not a stage-{stage} one: "
            f"{', '.join(sorted(elsewhere))}"
        )
    elif other_stage:
        lines.append(f"stage {other_stage} templates: {', '.join(names_for_stage(other_stage))}")
    return " ".join(lines)


def format_registry_help() -> str:
    """The `draft --help` epilog: every template, what it drafts, where the
    artifact belongs, and what promotes it. This is the answer to "how do I
    find out what I can draft" that `e.g. evidence-intake,
    boundary-drafting` was not."""
    name_width = max(len(template.name) for template in DRAFT_TEMPLATES)
    produces_width = max(len(template.produces) for template in DRAFT_TEMPLATES)
    indent = " " * (2 + len("stage 0") + 2 + name_width + 2 + produces_width + 4)
    lines = ["templates (draft <stage> <template> <target> --var key=value):"]
    for template in DRAFT_TEMPLATES:
        lines.append(
            f"  stage {template.stage}  {template.name.ljust(name_width)}  "
            f"{template.produces.ljust(produces_width)}  ->  {template.target}"
        )
        lines.append(f"{indent}promoted by: {template.promoted_by}")
    lines.append("")
    lines.append(
        "Every template in prompts/ is listed here, and `draft` refuses any other "
        "name with this list rather than a bare 'no template at ...'."
    )
    return "\n".join(lines)