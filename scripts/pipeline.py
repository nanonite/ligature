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
  approve   The review/approval checkpoint (#39) -- the only path that
            writes a draft to its target path.
  validate  Stage 4 (G1a/G1b/G2+) over boundary contracts (#9/#11) --
            fully deterministic, no LLM calls, by construction.

Not yet implemented -- the schemas these stages need don't exist yet
(tracked as the named chainlink issues, not guessed at here):
  promotion (#15/#16/#17/#18/#19/#20, M3), emission, attach, manifest
  (#14, M2), Stage 8A-8C (#22-#26, M4). `pipeline status` reports this
  honestly instead of a stage silently no-op'ing.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from review_checkpoint import ApprovalRefused  # noqa: E402
from review_checkpoint import approve as checkpoint_approve  # noqa: E402
from review_checkpoint import stage_draft  # noqa: E402
from schema_utils import make_validator  # noqa: E402
from validate_boundary_contracts import load_validator as load_boundary_validator  # noqa: E402
from validate_boundary_contracts import validate as validate_boundaries  # noqa: E402
from validate_boundary_contracts import validate_data as validate_boundary_data  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DESCRIPTOR_SCHEMA_PATH = ROOT / "schemas" / "project-descriptor.schema.json"
PROMPTS = ROOT / "prompts"

NOT_YET_IMPLEMENTED = {
    "promotion": "#15/#16/#17/#18/#19/#20 (M3 -- I-schema, evidence schema not built yet)",
    "emission": "#14 dependency chain (M2/M3)",
    "attach": "#14 dependency chain (M2/M3)",
    "manifest": "#14 (M2 -- work-package manifest schema)",
    "8A": "#22-#26 (M4 -- bridge/closure track)",
    "8B": "#22-#26 (M4)",
    "8C": "#25 (M4 -- G14 transitive closure)",
}

_TEMPLATE_VAR = re.compile(r"\{\{(\w+)\}\}")


class PipelineError(Exception):
    pass


def load_project_descriptor(path: Path) -> dict:
    schema = json.loads(DESCRIPTOR_SCHEMA_PATH.read_text())
    validator = make_validator(schema)

    data = json.loads(path.read_text())
    errors = list(validator.iter_errors(data))
    if errors:
        raise PipelineError(
            f"project descriptor {path} is invalid:\n"
            + "\n".join(f"  - {e.message}" for e in errors)
        )
    return data


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


def _boundary_dir_for(crate: dict, workspace: Path) -> Path:
    """plan.md's canonical layout, §2: crates/*/specs/_boundaries/*.json --
    always this exact path relative to the crate root, not a separate
    descriptor field."""
    return (workspace / crate["crate_dir"] / "specs" / "_boundaries").resolve()


def _require_target_in_workspace(target: Path, workspace: Path, descriptor: dict) -> None:
    """Draft/approve targets used to be accepted verbatim -- args.target
    with no check it belonged to the workspace or any declared crate at
    all (external review finding, medium severity). A path outside the
    project's own declared scope is refused outright, not just silently
    processed."""
    try:
        target.resolve().relative_to(workspace.resolve())
    except ValueError:
        raise PipelineError(f"target {target} is outside the workspace {workspace} -- refusing")
    if _crate_for(target, workspace, descriptor) is None:
        raise PipelineError(
            f"target {target} does not belong to any crate declared in the "
            "project descriptor -- refusing to draft/approve outside a declared crate"
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

    A FOURTH review pass (2026-08-27) found that "recognized" was still too
    loose: `"_boundaries" in target.parts` matches a `_boundaries` component
    ANYWHERE in the path, not the crate's actual declared layout --
    `crate_a/not_specs/_boundaries/x.json` and `crate_a/specs/nested/_boundaries/x.json`
    both matched and promoted successfully. Neither is flagged by G1b's own
    "flat" check either, since that only checks the file sits directly
    inside a directory literally named `_boundaries` -- it has no opinion
    on where `_boundaries` itself sits. Fixed by anchoring to the exact
    expected path: target.parent must equal <crate_dir>/specs/_boundaries,
    not merely contain that name somewhere upstream."""
    crate = _crate_for(target, workspace, descriptor)
    if crate is not None and target.resolve().parent == _boundary_dir_for(crate, workspace):
        validator = load_boundary_validator()
        specs_search_root = _specs_search_root_for(target, workspace, descriptor)
        return lambda path, data: validate_boundary_data(path, data, validator, specs_search_root)
    raise PipelineError(
        f"no validator recognizes target {target} -- this pipeline only "
        "validates boundary contracts at <crate_dir>/specs/_boundaries/*.json "
        "today. Refusing to draft/approve an artifact type or location it "
        "cannot mechanically gate, rather than silently skipping validation for it."
    )


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


def cmd_status(args: argparse.Namespace) -> int:
    print("Implemented: draft (Stage 0/3), approve (checkpoint), validate (Stage 4 G1a/G1b/G2+)")
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
