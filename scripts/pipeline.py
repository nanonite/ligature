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

from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
from review_checkpoint import approve as checkpoint_approve  # noqa: E402
from review_checkpoint import stage_draft  # noqa: E402
from validate_boundary_contracts import validate as validate_boundaries  # noqa: E402

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
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

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
    """The templates require JSON-only output, but tolerate a model
    wrapping it in a markdown fence anyway -- strip one if present rather
    than failing on the single most common way models violate this."""
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
        findings_total.extend(validate_boundaries(crate_root, specs_search_root))

    if not findings_total:
        print("OK: all boundary contracts pass G1a/G1b/G2+")
        return 0

    print(f"FAIL: {len(findings_total)} finding(s)")
    for f in findings_total:
        print(f"  - {f}")
    return 1


def cmd_draft(args: argparse.Namespace) -> int:
    descriptor = load_project_descriptor(args.descriptor)
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


def cmd_approve(args: argparse.Namespace) -> int:
    draft_path = args.target.with_suffix(args.target.suffix + ".draft")
    result = checkpoint_approve(draft_path, args.target, reviewer=args.reviewer, reviewed_at=args.reviewed_at)
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
