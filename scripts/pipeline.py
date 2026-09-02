#!/usr/bin/env python3
"""The pipeline CLI entrypoint (plan.md §6.1, chainlink #37).

One command, invoked against a project workspace, spanning the whole
pipeline -- not a two-tool split between an LLM-run authoring session and a
separately-invoked deterministic tool. Reads the project structure
descriptor for all configuration.

Implemented now, against schemas that actually exist:
  draft     Stage 0/3 -- render a prompt template, dispatch to the
            configured one-shot LLM backend, stage the result as a draft.
            Never calls a backend for anything past this command.
  approve   The review/approval checkpoint (#39) -- the path that writes a
            single draft to its target path.
  approve-pair  The explicit transactional checkpoint for a new non-pairwise
            interaction and its protocol-debt record, whose cross-references
            require both artifacts to be accepted together.
  validate  Stage 4 (G1a/G1b/G2+) over boundary contracts (#9/#11) --
            fully deterministic, no LLM calls, by construction.
  validate-work-package  Stage 7's own schema + §10.1 checks (#14) over a
            work-package manifest. Standalone, not routed through
            draft/approve -- a manifest is machine-generated at Stage 7
            from already-promoted content, not LLM-drafted and
            human-reviewed the way a boundary contract is.
  validate-promotion  Stage 4.5's schema + §7.1 checks (#15) over a
            promotion receipt: every artifact_manifest path is workspace-
            anchored, every declared hash is verified against the real
            file, the receipt is never in its own manifest, and no listed
            artifact carries a promotion_id back (references are
            one-way). Standalone, not routed through draft/approve --
            the receipt's reviewer/accepted_at fields are top-level, not
            the nested review: {} shape approve() writes, and receipt
            *generation* (reading a promoted artifact set and computing
            this) is separate, not-yet-built work, same boundary as
            validate-work-package's own manifest generator.
  validate-interaction  Stage 4's G1a/G1b/G2++/G15 over interaction (I)
            specs (#16/#17/#19): schema plus COMPUTED eligibility (plan.md
            §5.2) -- eligibility is derived from edge_class, never
            hand-set, and disagreement between the derived and stored
            value is rejected -- plus G15 (fail closed): a non-pairwise
            interaction with no valid protocol-debt record covering it is
            rejected, cross-referenced live against the crate's
            _protocol_debt/ directory.
  validate-exemption  Stage 4's G1a/G1b over boundary-required exemption
            objects (#16): schema plus naming (interaction_id == filename
            stem). Does not yet cross-reference that the named interaction
            is real or actually eligible -- that is R2's job, still
            deferred (see NOT_YET_IMPLEMENTED).
  validate-protocol-debt  Stage 4's G1a/G1b over protocol-debt records
            (#19): schema plus naming plus interaction cross-reference --
            a debt record naming a nonexistent interaction, or one whose
            protocol_class is actually pairwise, is rejected, cross-
            referenced live against the crate's _interactions/ directory.
  validate-evidence  Stage 4's G1a/G1b over evidence records (#20):
            schema plus naming. Workspace-level, not crate-scoped (one
            evidence/ directory per workspace, not per crate). Never
            routed through draft/approve -- evidence carries no review
            block (plan.md §7.2's human-checkpoint list names
            "evidence-conflict resolution", not evidence itself).
  validate-conflict-resolution  Stage 4's G1a/G1b/G11 over evidence
            conflict-resolution records (#20): schema (incl. the
            status == resolved => resolution + review requirement),
            selected-authority membership in the record's own evidence
            list, a live dangling-evidence-reference check, and G11 --
            "only unresolved conflicts block" (plan.md §11's own words) --
            enforced directly, not deferred. Also workspace-level; IS
            routed through draft/approve, since it does carry a review
            block and is in §7.2's checkpoint list.

Not yet implemented -- the schemas these stages need don't exist yet
(tracked as the named chainlink issues, not guessed at here):
  emission, attach, manifest and promotion-receipt *generation* (the
  schemas/validators exist as of #14/#15; the generators that read
  promoted I/O and emit these don't, since they need the rest of the
  I-schema machinery M3 builds), Stage 8A-8C (#22-#26, M4). `pipeline
  status` reports this honestly instead of a stage silently no-op'ing.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from project_descriptor import ProjectDescriptorError  # noqa: E402
from project_descriptor import boundary_dir_for as _boundary_dir_for  # noqa: E402
from project_descriptor import boundary_dirs_for_descriptor  # noqa: E402
from project_descriptor import conflict_dir_for as _conflict_dir_for  # noqa: E402
from project_descriptor import evidence_dir_for as _evidence_dir_for  # noqa: E402
from project_descriptor import exemption_dir_for as _exemption_dir_for  # noqa: E402
from project_descriptor import interaction_dir_for as _interaction_dir_for  # noqa: E402
from project_descriptor import load_project_descriptor as _load_project_descriptor  # noqa: E402
from project_descriptor import protocol_debt_dir_for as _protocol_debt_dir_for  # noqa: E402
from review_checkpoint import ApprovalRefused  # noqa: E402
from review_checkpoint import approve as checkpoint_approve  # noqa: E402
from review_checkpoint import approve_pair as checkpoint_approve_pair  # noqa: E402
from review_checkpoint import stage_draft  # noqa: E402
from validate_boundary_contracts import load_validator as load_boundary_validator  # noqa: E402
from validate_boundary_contracts import validate as validate_boundaries  # noqa: E402
from validate_boundary_contracts import validate_data as validate_boundary_data  # noqa: E402
from validate_conflict_resolution import load_validator as load_conflict_resolution_validator  # noqa: E402
from validate_conflict_resolution import validate_data as validate_conflict_resolution_data  # noqa: E402
from validate_conflict_resolution import validate_workspace as validate_conflict_resolution_workspace  # noqa: E402
from validate_evidence import load_evidence_ids  # noqa: E402
from validate_evidence import validate_workspace as validate_evidence_workspace  # noqa: E402
from validate_exemption import load_validator as load_exemption_validator  # noqa: E402
from validate_exemption import validate_crate as validate_exemption_crate  # noqa: E402
from validate_exemption import validate_data as validate_exemption_data  # noqa: E402
from validate_interaction import load_interactions_by_id  # noqa: E402
from validate_interaction import load_validator as load_interaction_validator  # noqa: E402
from validate_interaction import validate_crate as validate_interaction_crate  # noqa: E402
from validate_interaction import validate_data as validate_interaction_data  # noqa: E402
from validate_promotion_receipt import load_validator as load_promotion_validator  # noqa: E402
from validate_protocol_debt import load_validator as load_protocol_debt_validator  # noqa: E402
from validate_protocol_debt import valid_interaction_ids_from_crate  # noqa: E402
from validate_protocol_debt import validate_crate as validate_protocol_debt_crate  # noqa: E402
from validate_protocol_debt import validate_data as validate_protocol_debt_data  # noqa: E402
from validate_promotion_receipt import validate_file as validate_promotion_file  # noqa: E402
from validate_work_package import load_validator as load_work_package_validator  # noqa: E402
from validate_work_package import validate_file as validate_work_package_file  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "prompts"

NOT_YET_IMPLEMENTED = {
    "R2 coverage": "not yet implemented -- every eligible I edge covered by an O artifact or a reviewed "
    "exemption; needs #16's interaction/exemption validators, which exist, cross-referenced against each "
    "other, which doesn't yet. (G15's own coverage check, the structurally identical case for non-pairwise "
    "protocol classification, IS implemented -- see cmd_validate_interaction/cmd_validate_protocol_debt.)",
    "G4/G5 evidence tracing/grounding": "not yet implemented -- required/bug-compat evidence tracing to "
    "nothing (G4) and ungrounded obligations (G5) both need cross-referencing interaction evidence_links "
    "(#16) to evidence ids (#20), which exist individually but aren't cross-referenced against each other yet",
    "emission": "M3's I-schema is now complete (#16-#20); emission (Stage 5) itself is still not built",
    "attach": "M3's I-schema is now complete (#16-#20); attach (Stage 6) itself is still not built",
    "manifest generation": "#14's schema+validator exist (`validate-work-package`); M3's I-schema is now "
    "complete (#16-#20), but the generator that reads promoted I/O and emits a manifest from it is still not built",
    "promotion-receipt generation": "#15's schema+validator exist (`validate-promotion`); M3's I-schema is "
    "now complete (#16-#20), but the generator that reads an accepted artifact set and computes a receipt "
    "from it is still not built",
    "8A": "#22-#26 (M4 -- bridge/closure track)",
    "8B": "#22-#26 (M4)",
    "8C": "#25 (M4 -- G14 transitive closure)",
}

_TEMPLATE_VAR = re.compile(r"\{\{(\w+)\}\}")


class PipelineError(Exception):
    pass


def load_project_descriptor(path: Path) -> dict:
    """Thin wrapper: the real logic lives in project_descriptor.py, shared
    with validate_work_package.py (which can't import this module back --
    pipeline.py already imports from validate_work_package.py, so the
    reverse would be circular). Re-raises as PipelineError so main()'s
    existing exception handling doesn't need to know about a second
    exception type."""
    try:
        return _load_project_descriptor(path)
    except ProjectDescriptorError as e:
        raise PipelineError(str(e))


def render_prompt(template_path: Path, variables: dict[str, str]) -> str:
    """{{var}} substitution -- deliberately not str.format(), since the
    templates embed literal JSON with single braces."""
    text = template_path.read_text()

    def _sub(match: re.Match) -> str:
        key = match.group(1)
        if key not in variables:
            raise PipelineError(f"template {template_path} references undefined variable {{{{{key}}}}}")
        return variables[key]

    rendered = _TEMPLATE_VAR.sub(_sub, text)

    # A malformed placeholder (e.g. containing a paren or space) won't
    # match _TEMPLATE_VAR's \w+ and would otherwise pass through silently
    # instead of raising -- catch that class of template-authoring bug
    # here rather than shipping a prompt with a literal {{...}} in it.
    leftover = re.search(r"\{\{[^}]*\}\}", rendered)
    if leftover:
        raise PipelineError(
            f"template {template_path} has an unresolved placeholder "
            f"{leftover.group(0)!r} that didn't match \\w+ -- fix the "
            "template, don't add ad hoc syntax to render_prompt"
        )

    return rendered


def invoke_llm_backend(backend: dict, prompt: str, runner=subprocess.run) -> str:
    """Pluggable one-shot backend (plan.md §6.1). Prompt is always piped
    via stdin regardless of backend, to avoid fighting each tool's own
    argument-length/quoting conventions for a long structured prompt.
    `runner` is injectable for testing -- never actually shells out in a
    unit test."""
    kind = backend["kind"]
    if kind == "manual":
        raise PipelineError(
            "llm_backend.kind is 'manual' -- print the prompt yourself and "
            "call stage_draft() with the result; invoke_llm_backend() does "
            "not run anything for manual mode."
        )

    default_commands = {
        "claude": ["claude", "-p"],
        "codex": ["codex", "exec"],
        "opencode": ["opencode", "run"],
    }
    if kind not in default_commands:
        raise PipelineError(f"unknown llm_backend.kind {kind!r}")

    command = backend.get("command")
    argv = command.split() if command else default_commands[kind]

    result = runner(argv, input=prompt, capture_output=True, text=True)
    if result.returncode != 0:
        raise PipelineError(
            f"LLM backend {argv} exited {result.returncode}: {result.stderr}"
        )
    return result.stdout


def parse_llm_json_output(raw: str) -> dict:
    """The templates require JSON-only output. Tolerates exactly one
    deviation: the *entire* response wrapped in a single markdown fence
    (```json ... ``` with nothing else before or after) -- the single most
    common way models violate "JSON only" while still being unambiguous
    about what to extract. Anything looser (prose before/after the fence,
    multiple fences) is deliberately NOT unwrapped and hard-fails instead --
    review finding D8: an earlier version of this docstring over-claimed
    tolerance here. That failure is acceptable-by-design (retry/escalate,
    plan.md §16.1's "no partial output if you are unsure" principle), not a
    bug to paper over by guessing which fenced block was the real answer."""
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*\n(.*)\n```$", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise PipelineError(
            f"LLM backend output was not valid JSON (after fence-stripping): {e}\n"
            f"raw output was:\n{raw}"
        )


def cmd_validate(args: argparse.Namespace) -> int:
    descriptor = load_project_descriptor(args.descriptor)
    findings_total = []
    for crate in descriptor["crates"]:
        crate_root = args.workspace / crate["crate_dir"]
        specs_search_root = args.workspace / crate["specs_search_root"]
        try:
            findings_total.extend(validate_boundaries(crate_root, specs_search_root))
        except FileNotFoundError as e:
            # A crate_dir typo in the descriptor must be a loud failure,
            # not a silent "0 boundaries found, all clean" (external
            # review finding, high severity).
            raise PipelineError(f"crate {crate['crate_dir']!r} in the project descriptor: {e}")

    errors = [f for f in findings_total if f.severity == "error"]
    infos = [f for f in findings_total if f.severity == "info"]

    if infos:
        print(f"INFO: {len(infos)} non-blocking finding(s)")
        for f in infos:
            print(f"  - {f}")

    if not errors:
        print("OK: all boundary contracts pass G1a/G1b/G2+")
        return 0

    print(f"FAIL: {len(errors)} finding(s)")
    for f in errors:
        print(f"  - {f}")
    return 1


def cmd_validate_work_package(args: argparse.Namespace) -> int:
    # Default to the workspace root, never None -- omitting --specs-search-root
    # must not silently disable assumption-ref resolution (external review,
    # high severity: a real manifest with trusted_assumptions printed OK
    # via this exact code path without the check ever running).
    specs_search_root = args.specs_search_root if args.specs_search_root is not None else args.workspace

    # Restrict trusted-assumption resolution to the project descriptor's
    # own declared <crate_dir>/specs/_boundaries directories -- otherwise
    # a schema-shaped JSON file dropped anywhere under a directory named
    # _boundaries counts as "a real boundary contract" (external review:
    # reproduced with junk/not-a-crate/_boundaries/anything.json).
    descriptor = load_project_descriptor(args.descriptor)
    allowed_boundary_dirs = boundary_dirs_for_descriptor(descriptor, args.workspace)

    validator = load_work_package_validator()
    findings = validate_work_package_file(
        args.manifest, validator, args.workspace, specs_search_root, allowed_boundary_dirs
    )
    errors = [f for f in findings if f.severity == "error"]
    infos = [f for f in findings if f.severity == "info"]

    if infos:
        print(f"INFO: {len(infos)} non-blocking finding(s)")
        for f in infos:
            print(f"  - {f}")

    if not errors:
        print("OK: work package manifest passes G1a and §10.1 checks")
        return 0

    print(f"FAIL: {len(errors)} finding(s)")
    for f in errors:
        print(f"  - {f}")
    return 1


def cmd_validate_promotion(args: argparse.Namespace) -> int:
    validator = load_promotion_validator()
    findings = validate_promotion_file(args.receipt, validator, args.workspace)

    if not findings:
        print("OK: promotion receipt passes G1a and §7.1 checks")
        return 0

    print(f"FAIL: {len(findings)} finding(s)")
    for f in findings:
        print(f"  - {f}")
    return 1


def _require_crate_root_exists(crate: dict, workspace: Path) -> Path:
    crate_root = workspace / crate["crate_dir"]
    if not crate_root.is_dir():
        raise PipelineError(
            f"crate {crate['crate_dir']!r} in the project descriptor: crate root does not "
            f"exist or is not a directory: {crate_root}"
        )
    return crate_root


def cmd_validate_interaction(args: argparse.Namespace) -> int:
    # Discovers candidates crate-wide (any directory literally named
    # _interactions, at any depth) and rejects any that don't sit
    # directly under the crate's exact <crate_dir>/specs/_interactions
    # directory (project_descriptor.interaction_dir_for) -- external
    # review, medium severity, SECOND pass: an earlier anchored-only-the-
    # canonical-directory fix stopped a mislocated artifact from being
    # wrongly validated, but also stopped it from ever being looked at,
    # reproducing the same "zero findings" outcome by omission instead of
    # false acceptance. validate_interaction_crate() discovers first, then
    # rejects by location, so a mislocated artifact is neither accepted
    # nor invisible.
    # G15 (non-pairwise protocol coverage) is fail-closed here, not
    # deferred: for each crate, load its real interactions, use them to
    # find which protocol-debt records are themselves fully valid (their
    # own cross-reference to a real, non-pairwise interaction checked),
    # and pass that coverage set into the interaction scan so a
    # non-pairwise interaction with no valid debt record is rejected --
    # external review, high severity: this was previously assigned to
    # chainlink #21 in NOT_YET_IMPLEMENTED, but #21 is only the I-schema
    # milestone gate (all of #15-#20 landed), not an issue that itself
    # implements gates; #19's own title says "fail closed."
    descriptor = load_project_descriptor(args.descriptor)
    findings_total = []
    for crate in descriptor["crates"]:
        crate_root = _require_crate_root_exists(crate, args.workspace)
        interactions_by_id = load_interactions_by_id(_interaction_dir_for(crate, args.workspace))
        valid_debt_interaction_ids = valid_interaction_ids_from_crate(
            crate_root, _protocol_debt_dir_for(crate, args.workspace), interactions_by_id
        )
        findings_total.extend(
            validate_interaction_crate(
                crate_root, _interaction_dir_for(crate, args.workspace), valid_debt_interaction_ids
            )
        )

    if not findings_total:
        print("OK: all interactions pass G1a/G1b (incl. computed eligibility) and G15 protocol coverage")
        return 0

    print(f"FAIL: {len(findings_total)} finding(s)")
    for f in findings_total:
        print(f"  - {f}")
    return 1


def cmd_validate_exemption(args: argparse.Namespace) -> int:
    # Same discover-then-reject-by-location scan as
    # cmd_validate_interaction, for <crate_dir>/specs/_exemptions.
    descriptor = load_project_descriptor(args.descriptor)
    findings_total = []
    for crate in descriptor["crates"]:
        crate_root = _require_crate_root_exists(crate, args.workspace)
        findings_total.extend(validate_exemption_crate(crate_root, _exemption_dir_for(crate, args.workspace)))

    if not findings_total:
        print("OK: all exemptions pass G1a/G1b")
        return 0

    print(f"FAIL: {len(findings_total)} finding(s)")
    for f in findings_total:
        print(f"  - {f}")
    return 1


def cmd_validate_protocol_debt(args: argparse.Namespace) -> int:
    # Same discover-then-reject-by-location scan as
    # cmd_validate_interaction/cmd_validate_exemption, for
    # <crate_dir>/specs/_protocol_debt, plus the same interaction
    # cross-reference cmd_validate_interaction's G15 check relies on.
    descriptor = load_project_descriptor(args.descriptor)
    findings_total = []
    for crate in descriptor["crates"]:
        crate_root = _require_crate_root_exists(crate, args.workspace)
        interactions_by_id = load_interactions_by_id(_interaction_dir_for(crate, args.workspace))
        findings_total.extend(
            validate_protocol_debt_crate(
                crate_root, _protocol_debt_dir_for(crate, args.workspace), interactions_by_id
            )
        )

    if not findings_total:
        print("OK: all protocol-debt records pass G1a/G1b (incl. interaction cross-reference)")
        return 0

    print(f"FAIL: {len(findings_total)} finding(s)")
    for f in findings_total:
        print(f"  - {f}")
    return 1


def _require_workspace_root_exists(workspace: Path) -> Path:
    if not workspace.is_dir():
        raise PipelineError(f"workspace root does not exist or is not a directory: {workspace}")
    return workspace


def cmd_validate_evidence(args: argparse.Namespace) -> int:
    # Evidence is workspace-level, not crate-scoped (see
    # project_descriptor.evidence_dir_for's own docstring) -- one scan,
    # not a per-crate loop the way validate-interaction/-exemption/
    # -protocol-debt work. No project descriptor is needed at all: there
    # is nothing crate-specific to resolve.
    workspace_root = _require_workspace_root_exists(args.workspace)
    findings = validate_evidence_workspace(workspace_root, _evidence_dir_for(workspace_root))

    if not findings:
        print("OK: all evidence records pass G1a/G1b")
        return 0

    print(f"FAIL: {len(findings)} finding(s)")
    for f in findings:
        print(f"  - {f}")
    return 1


def cmd_validate_conflict_resolution(args: argparse.Namespace) -> int:
    # Same workspace-level scope as cmd_validate_evidence. G11 (unresolved
    # conflicts block) and the evidence cross-reference are both real,
    # fail-closed checks here -- not deferred the way #19's G15 initially
    # (and wrongly) was.
    workspace_root = _require_workspace_root_exists(args.workspace)
    evidence_ids = load_evidence_ids(_evidence_dir_for(workspace_root))
    findings = validate_conflict_resolution_workspace(
        workspace_root, _conflict_dir_for(workspace_root), evidence_ids
    )

    errors = [f for f in findings if f.severity == "error"]
    infos = [f for f in findings if f.severity == "info"]

    if infos:
        print(f"INFO: {len(infos)} non-blocking finding(s)")
        for f in infos:
            print(f"  - {f}")

    if not errors:
        print("OK: all conflict-resolution records pass G1a/G1b/G11")
        return 0

    print(f"FAIL: {len(errors)} finding(s)")
    for f in errors:
        print(f"  - {f}")
    return 1


def cmd_draft(args: argparse.Namespace) -> int:
    descriptor = load_project_descriptor(args.descriptor)
    _require_target_in_workspace(args.target, args.workspace, descriptor)
    backend = descriptor.get("llm_backend", {"kind": "manual"})

    template_path = PROMPTS / f"stage-{args.stage}-{args.template_name}.md"
    if not template_path.exists():
        raise PipelineError(f"no template at {template_path}")

    variables = dict(pair.split("=", 1) for pair in args.var)
    prompt = render_prompt(template_path, variables)

    if backend["kind"] == "manual":
        print(prompt)
        print("\n--- paste the model's JSON-only response, then EOF (ctrl-D) ---", file=sys.stderr)
        raw = sys.stdin.read()
    else:
        raw = invoke_llm_backend(backend, prompt)

    data = parse_llm_json_output(raw)
    draft_path = stage_draft(data, args.target)
    print(f"staged draft: {draft_path}")
    return 0


def _crate_for(target: Path, workspace: Path, descriptor: dict) -> dict | None:
    for crate in descriptor["crates"]:
        crate_root = (workspace / crate["crate_dir"]).resolve()
        try:
            target.resolve().relative_to(crate_root)
        except ValueError:
            continue
        return crate
    return None


def _specs_search_root_for(target: Path, workspace: Path, descriptor: dict) -> Path | None:
    crate = _crate_for(target, workspace, descriptor)
    return workspace / crate["specs_search_root"] if crate else None


def _require_target_in_workspace(target: Path, workspace: Path, descriptor: dict) -> None:
    """Draft/approve targets used to be accepted verbatim -- args.target
    with no check it belonged to the workspace or any declared crate at
    all (external review finding, medium severity). A path outside the
    project's own declared scope is refused outright, not just silently
    processed.

    Chainlink #20: conflict-resolution records are workspace-level, not
    crate-scoped (project_descriptor.conflict_dir_for's own docstring --
    plan.md §7's artifact_manifest worked example places
    specs/_conflicts/EC-004.json with no crate prefix). The crate-only
    check below would incorrectly refuse a legitimate conflict-resolution
    target in any project whose crate_dir isn't literally "." -- fixed by
    also accepting the one recognized workspace-level artifact location,
    not just crate membership."""
    try:
        target.resolve().relative_to(workspace.resolve())
    except ValueError:
        raise PipelineError(f"target {target} is outside the workspace {workspace} -- refusing")
    if _crate_for(target, workspace, descriptor) is not None:
        return
    if target.resolve().parent == _conflict_dir_for(workspace):
        return
    raise PipelineError(
        f"target {target} does not belong to any crate declared in the project "
        "descriptor, and is not a recognized workspace-level artifact location "
        "either -- refusing to draft/approve outside a declared scope"
    )


def _select_validate_fn(target: Path, workspace: Path, descriptor: dict):
    """Dispatch by the specs/_<kind>/ directory convention used throughout
    plan.md -- extensible to interaction/witness/etc. validators once M3/M4
    give them schemas; only boundary contracts exist to validate today.

    Raises for anything this dispatcher doesn't recognize -- a third review
    pass (2026-08-27) found the previous version returned SKIP_VALIDATION
    for *any* unmatched path, which converted "unknown artifact type" into
    a silent bypass: a boundary placed under a typo'd `_boundary/` (missing
    the trailing s) matched nothing, got SKIP_VALIDATION, and was approved
    with zero gating. Reproduced end to end. This dispatcher's job is to
    recognize known artifact types and refuse everything else outright --
    "no validator exists yet" must be a hard stop on the approval path, not
    a reason to let it through. When M3/M4 add real validators for other
    artifact types, they extend this if/elif chain; until then, this
    pipeline simply cannot approve those artifact types, which is correct.

    A FOURTH review pass (2026-08-30) found that "recognized" was still too
    loose: `"_boundaries" in target.parts` matches a `_boundaries` component
    ANYWHERE in the path, not the crate's actual declared layout --
    `crate_a/not_specs/_boundaries/x.json` and `crate_a/specs/nested/_boundaries/x.json`
    both matched and promoted successfully. Neither is flagged by G1b's own
    "flat" check either, since that only checks the file sits directly
    inside a directory literally named `_boundaries` -- it has no opinion
    on where `_boundaries` itself sits. Fixed by anchoring to the exact
    expected path: target.parent must equal <crate_dir>/specs/_boundaries,
    not merely contain that name somewhere upstream.

    A FIFTH review pass (2026-09-01) found this directory-anchoring check
    never looked at the target's own suffix: a schema-valid interaction or
    exemption approved as e.g. `_interactions/I-X-001.yaml` matched by
    directory alone, got a real validator, and was written straight
    through to a `.yaml` path -- despite every schema/docstring in this
    codebase documenting `*.json` as the canonical extension for all three
    artifact types. Reproduced end to end via `approve`. Fixed by refusing
    any non-.json target up front, before the directory match even runs."""
    if target.suffix != ".json":
        raise PipelineError(
            f"target {target} is not a .json file -- this pipeline only "
            "recognizes .json artifacts for boundary contracts, interactions, "
            "exemptions, protocol-debt records, and conflict-resolution records"
        )
    # Chainlink #20: conflict-resolution records are workspace-level, not
    # crate-scoped -- checked before the crate-anchored block below, not
    # nested inside it, since a target here need not belong to any crate
    # at all (see _require_target_in_workspace's own note on the same gap).
    if target.resolve().parent == _conflict_dir_for(workspace):
        validator = load_conflict_resolution_validator()
        evidence_ids = load_evidence_ids(_evidence_dir_for(workspace))
        return lambda path, data: validate_conflict_resolution_data(path, data, validator, evidence_ids)
    crate = _crate_for(target, workspace, descriptor)
    if crate is not None:
        resolved_parent = target.resolve().parent
        if resolved_parent == _boundary_dir_for(crate, workspace):
            validator = load_boundary_validator()
            specs_search_root = _specs_search_root_for(target, workspace, descriptor)
            return lambda path, data: validate_boundary_data(path, data, validator, specs_search_root)
        if resolved_parent == _interaction_dir_for(crate, workspace):
            validator = load_interaction_validator()
            crate_root = (workspace / crate["crate_dir"]).resolve()
            interactions_by_id = load_interactions_by_id(_interaction_dir_for(crate, workspace))
            valid_debt_interaction_ids = valid_interaction_ids_from_crate(
                crate_root, _protocol_debt_dir_for(crate, workspace), interactions_by_id
            )
            return lambda path, data: validate_interaction_data(path, data, validator, valid_debt_interaction_ids)
        if resolved_parent == _exemption_dir_for(crate, workspace):
            validator = load_exemption_validator()
            return lambda path, data: validate_exemption_data(path, data, validator)
        if resolved_parent == _protocol_debt_dir_for(crate, workspace):
            validator = load_protocol_debt_validator()
            interactions_by_id = load_interactions_by_id(_interaction_dir_for(crate, workspace))
            return lambda path, data: validate_protocol_debt_data(path, data, validator, interactions_by_id)
    raise PipelineError(
        f"no validator recognizes target {target} -- this pipeline only "
        "validates boundary contracts at <crate_dir>/specs/_boundaries/*.json, "
        "interactions at <crate_dir>/specs/_interactions/*.json, exemptions "
        "at <crate_dir>/specs/_exemptions/*.json, protocol-debt records at "
        "<crate_dir>/specs/_protocol_debt/*.json, and conflict-resolution records "
        "at specs/_conflicts/*.json (workspace-level) today. Refusing to "
        "draft/approve an artifact type or location it cannot mechanically "
        "gate, rather than silently skipping validation for it."
    )


def _select_pair_validate_fn(
    interaction_target: Path,
    protocol_debt_target: Path,
    workspace: Path,
    descriptor: dict,
):
    """Build the transaction validator for a new non-pairwise I + debt pair.

    Single-artifact validators intentionally consult only promoted sibling
    artifacts.  This callback is the explicit bootstrap path: it validates
    both drafts against a combined view containing the candidate interaction
    and candidate debt record, while still requiring each side's own full
    schema/naming/review gates.
    """
    interaction_crate = _crate_for(interaction_target, workspace, descriptor)
    debt_crate = _crate_for(protocol_debt_target, workspace, descriptor)
    if interaction_crate is None or debt_crate is None:
        raise PipelineError("paired approval targets must belong to declared crates")
    if interaction_crate["crate_dir"] != debt_crate["crate_dir"]:
        raise PipelineError("paired interaction and protocol-debt targets must belong to the same crate")

    interaction_dir = _interaction_dir_for(interaction_crate, workspace)
    debt_dir = _protocol_debt_dir_for(interaction_crate, workspace)
    if interaction_target.suffix != ".json" or interaction_target.resolve().parent != interaction_dir:
        raise PipelineError(
            f"paired approval interaction target must be exactly under {interaction_dir} as a .json file"
        )
    if protocol_debt_target.suffix != ".json" or protocol_debt_target.resolve().parent != debt_dir:
        raise PipelineError(
            f"paired approval protocol-debt target must be exactly under {debt_dir} as a .json file"
        )

    crate_root = (workspace / interaction_crate["crate_dir"]).resolve()
    interaction_validator = load_interaction_validator()
    debt_validator = load_protocol_debt_validator()

    def validate_pair(candidates: dict[Path, dict]) -> dict[Path, list]:
        candidate_interaction = candidates[interaction_target]
        candidate_debt = candidates[protocol_debt_target]

        interaction_id = candidate_interaction.get("interaction_id")
        debt_interaction_id = candidate_debt.get("interaction_id")
        if not (
            isinstance(interaction_id, str)
            and interaction_id == debt_interaction_id
            and interaction_target.stem == interaction_id
            and protocol_debt_target.stem == interaction_id
        ):
            raise ApprovalRefused(
                "paired approval requires the interaction and protocol-debt body IDs "
                "and filename stems to match; refusing to grant candidate G15 coverage"
            )

        interactions_by_id = load_interactions_by_id(interaction_dir)

        candidate_id = interaction_id
        if isinstance(candidate_id, str) and candidate_id in interactions_by_id.duplicate_ids:
            raise ApprovalRefused(
                f"interaction_id {candidate_id!r} has duplicate canonical candidates; refusing paired approval"
            )

        # If this is an update, replace the old promoted version in the
        # transaction view.  A new pair has no entry to replace.
        transaction_interactions = dict(interactions_by_id)
        if isinstance(candidate_id, str):
            transaction_interactions.pop(candidate_id, None)

        existing_coverage = valid_interaction_ids_from_crate(crate_root, debt_dir, interactions_by_id)
        interaction_coverage = set(existing_coverage)
        if isinstance(candidate_id, str):
            # The debt draft is validated below in the same transaction.  It
            # is safe to let G15 see this candidate ID here because the pair
            # is not committed unless the debt draft also passes.
            interaction_coverage.add(candidate_id)

        interaction_findings = validate_interaction_data(
            interaction_target, candidate_interaction, interaction_validator, interaction_coverage
        )
        interaction_errors = [
            finding for finding in interaction_findings if getattr(finding, "severity", "error") == "error"
        ]

        debt_findings: list = []
        if not interaction_errors and isinstance(candidate_id, str):
            transaction_interactions[candidate_id] = candidate_interaction
            debt_findings = validate_protocol_debt_data(
                protocol_debt_target, candidate_debt, debt_validator, transaction_interactions
            )

        return {
            interaction_target: interaction_findings,
            protocol_debt_target: debt_findings,
        }

    return validate_pair


def cmd_approve(args: argparse.Namespace) -> int:
    descriptor = load_project_descriptor(args.descriptor)
    _require_target_in_workspace(args.target, args.workspace, descriptor)
    validate_fn = _select_validate_fn(args.target, args.workspace, descriptor)

    draft_path = args.target.with_suffix(args.target.suffix + ".draft")
    try:
        result = checkpoint_approve(
            draft_path, args.target, reviewer=args.reviewer, reviewed_at=args.reviewed_at, validate_fn=validate_fn
        )
    except ApprovalRefused as e:
        raise PipelineError(str(e))
    print(f"approved: {result.target_path} ({result.classification}) by {result.reviewer} at {result.reviewed_at}")
    return 0


def cmd_approve_pair(args: argparse.Namespace) -> int:
    descriptor = load_project_descriptor(args.descriptor)
    _require_target_in_workspace(args.interaction_target, args.workspace, descriptor)
    _require_target_in_workspace(args.protocol_debt_target, args.workspace, descriptor)
    validate_fn = _select_pair_validate_fn(
        args.interaction_target, args.protocol_debt_target, args.workspace, descriptor
    )

    targets = (args.interaction_target, args.protocol_debt_target)
    drafts = tuple(target.with_suffix(target.suffix + ".draft") for target in targets)
    try:
        results = checkpoint_approve_pair(
            drafts,
            targets,
            reviewer=args.reviewer,
            reviewed_at=args.reviewed_at,
            validate_fn=validate_fn,
        )
    except ApprovalRefused as e:
        raise PipelineError(str(e))
    for result in results:
        print(f"approved: {result.target_path} ({result.classification}) by {result.reviewer} at {result.reviewed_at}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    print(
        "Implemented: draft (Stage 0/3), approve (checkpoint), "
        "approve-pair (transactional interaction + protocol-debt checkpoint), "
        "validate (Stage 4 G1a/G1b/G2+), "
        "validate-interaction (Stage 4 G1a/G1b + computed eligibility + G15), "
        "validate-exemption (Stage 4 G1a/G1b naming), "
        "validate-protocol-debt (Stage 4 G1a/G1b + interaction cross-reference), "
        "validate-evidence (Stage 4 G1a/G1b, workspace-level), "
        "validate-conflict-resolution (Stage 4 G1a/G1b/G11, workspace-level), "
        "validate-work-package (Stage 7 schema + §10.1, standalone), "
        "validate-promotion (Stage 4.5 schema + §7.1, standalone)"
    )
    print("Not yet implemented:")
    for stage, ref in NOT_YET_IMPLEMENTED.items():
        print(f"  - {stage}: {ref}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", type=Path, default=Path("."))
    parser.add_argument(
        "--descriptor",
        type=Path,
        default=None,
        help="Defaults to <workspace>/project-descriptor.json",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    validate_p = sub.add_parser("validate", help="Stage 4: G1a/G1b/G2+ over boundary contracts")
    validate_p.set_defaults(func=cmd_validate)

    validate_interaction_p = sub.add_parser(
        "validate-interaction", help="Stage 4: G1a/G1b + computed eligibility over interaction (I) specs"
    )
    validate_interaction_p.set_defaults(func=cmd_validate_interaction)

    validate_exemption_p = sub.add_parser(
        "validate-exemption", help="Stage 4: G1a/G1b over boundary-required exemption objects"
    )
    validate_exemption_p.set_defaults(func=cmd_validate_exemption)

    validate_protocol_debt_p = sub.add_parser(
        "validate-protocol-debt", help="Stage 4: G1a/G1b over protocol-debt records"
    )
    validate_protocol_debt_p.set_defaults(func=cmd_validate_protocol_debt)

    validate_evidence_p = sub.add_parser(
        "validate-evidence", help="Stage 4: G1a/G1b over evidence records (workspace-level)"
    )
    validate_evidence_p.set_defaults(func=cmd_validate_evidence)

    validate_conflict_resolution_p = sub.add_parser(
        "validate-conflict-resolution",
        help="Stage 4: G1a/G1b/G11 over evidence conflict-resolution records (workspace-level)",
    )
    validate_conflict_resolution_p.set_defaults(func=cmd_validate_conflict_resolution)

    validate_wp_p = sub.add_parser(
        "validate-work-package", help="Stage 7: schema + §10.1 checks over a work-package manifest"
    )
    validate_wp_p.add_argument("manifest", type=Path)
    validate_wp_p.add_argument(
        "--specs-search-root",
        type=Path,
        default=None,
        help="Root to resolve trusted_assumptions[].assumption_ref against (optional).",
    )
    validate_wp_p.set_defaults(func=cmd_validate_work_package)

    validate_promo_p = sub.add_parser(
        "validate-promotion", help="Stage 4.5: schema + §7.1 checks over a promotion receipt"
    )
    validate_promo_p.add_argument("receipt", type=Path)
    validate_promo_p.set_defaults(func=cmd_validate_promotion)

    draft_p = sub.add_parser("draft", help="Stage 0/3: one-shot LLM draft")
    draft_p.add_argument("stage", choices=["0", "3"])
    draft_p.add_argument("template_name", help="e.g. evidence-intake, boundary-drafting")
    draft_p.add_argument("target", type=Path, help="Target artifact path the draft will eventually promote to")
    draft_p.add_argument("--var", action="append", default=[], help="key=value, repeatable")
    draft_p.set_defaults(func=cmd_draft)

    approve_p = sub.add_parser("approve", help="Review checkpoint: promote a draft")
    approve_p.add_argument("target", type=Path)
    approve_p.add_argument("--reviewer", required=True)
    approve_p.add_argument("--reviewed-at", default=None)
    approve_p.set_defaults(func=cmd_approve)

    approve_pair_p = sub.add_parser(
        "approve-pair",
        help="Review and atomically promote a new non-pairwise interaction plus its protocol-debt record",
    )
    approve_pair_p.add_argument("interaction_target", type=Path)
    approve_pair_p.add_argument("protocol_debt_target", type=Path)
    approve_pair_p.add_argument("--reviewer", required=True)
    approve_pair_p.add_argument("--reviewed-at", default=None)
    approve_pair_p.set_defaults(func=cmd_approve_pair)

    status_p = sub.add_parser("status", help="What this CLI can and can't do yet")
    status_p.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    if args.descriptor is None:
        args.descriptor = args.workspace / "project-descriptor.json"

    try:
        return args.func(args)
    except PipelineError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
