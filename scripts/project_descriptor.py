#!/usr/bin/env python3
"""Shared project-descriptor loading (plan.md §1.1).

Factored out of pipeline.py so both it and validate_work_package.py can
load a project descriptor and derive each crate's canonical boundary
directory without importing from each other -- pipeline.py already
imports from validate_work_package.py (its draft/validate commands), so
the reverse direction would be a circular import.

`read_descriptor_text` is the single point where a descriptor reaches the
filesystem: every command that needs one goes through
`load_project_descriptor`, so an absent or unreadable descriptor becomes
one actionable line there rather than a `FileNotFoundError` traceback out
of each command that happens to call it (chainlink #108).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resources  # noqa: E402
from schema_utils import make_validator  # noqa: E402

DESCRIPTOR_SCHEMA_PATH = resources.resource_path("schemas", "project-descriptor.schema.json")


class ProjectDescriptorError(Exception):
    """The project descriptor failed to load: it is not readable as a file,
    it is not valid JSON, or it does not satisfy
    schemas/project-descriptor.schema.json.

    `diagnostics` carries one actionable line per problem (see
    schema_diagnostics), so a caller can name the offending property
    instead of only saying the descriptor is invalid (chainlink #73).

    The message is always self-contained and user-facing: every CLI
    boundary that catches this exception prints `error: {e}` and exits,
    so the message has to name the file itself (chainlink #108)."""

    def __init__(self, message: str, diagnostics: list[str] | None = None):
        super().__init__(message)
        self.diagnostics = list(diagnostics or [])


# A proper prefix shorter than this is not a near miss -- `c` and `cr`
# are prefixes of plenty of unrelated names, and a suggestion that is
# wrong is worse than none, because the user stops looking.
_NEAR_MISS_MIN_PREFIX = 3


def _edit_distance(left: str, right: str) -> int:
    """Damerau-Levenshtein (optimal string alignment) distance: the least
    number of insertions, deletions, substitutions and *adjacent*
    transpositions turning `left` into `right`.

    Adjacency is the whole reason this is not plain Levenshtein.
    `defualt` -- the misspelling the date-creusot pilot actually typed
    (chainlink #76) -- is one transposition from `default` but two
    substitutions under Levenshtein, so a near-miss budget calibrated
    on single insertion/deletion typos (`closure_kinds` ->
    `closure_kind`) would either miss the transposition or have to be
    loosened enough to start guessing at unrelated keys. Two full
    rows of state per edit is affordable here: these are descriptor
    key names, not documents."""
    if left == right:
        return 0
    rows, cols = len(left), len(right)
    distance = [[0] * (cols + 1) for _ in range(rows + 1)]
    for index in range(rows + 1):
        distance[index][0] = index
    for index in range(cols + 1):
        distance[0][index] = index
    for row in range(1, rows + 1):
        for col in range(1, cols + 1):
            substitution = 0 if left[row - 1] == right[col - 1] else 1
            distance[row][col] = min(
                distance[row - 1][col] + 1,
                distance[row][col - 1] + 1,
                distance[row - 1][col - 1] + substitution,
            )
            if (
                row > 1
                and col > 1
                and left[row - 1] == right[col - 2]
                and left[row - 2] == right[col - 1]
            ):
                distance[row][col] = min(distance[row][col], distance[row - 2][col - 2] + 1)
    return distance[rows][cols]


def _key_gap(unexpected: str, candidate: str) -> int | None:
    """How far `unexpected` sits from `candidate` if the two are near
    misses of each other, else None.

    Three deliberate loosenings of the raw edit count, each because the
    loosened class is a distinct way people mistype a key:

      * case is folded -- the descriptor's keys are lowercase
        snake_case, so `Mode` is a slip of the same key, not a new one;
      * `_` is optionally ignored -- `oracle_build_command` and
        `oraclebuildcommand` are the same name to whoever typed the
        second one;
      * a proper prefix counts as one edit however long the omitted
        tail is -- `closure` -> `closure_kind` is one intent, not five
        edits, and it is the shape the pilot probed with (chainlink
        #73's regression case).

    The budget itself is deliberately tight -- two edits, or one for
    keys of four characters or fewer, where two edits is half the
    string. A suggestion the user cannot act on costs them the very
    thing this diagnostic exists to give back: the ability to tell a
    typo from a key that does not exist."""
    left, right = unexpected.lower(), candidate.lower()
    if left == right:
        return 0
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    gap = min(
        _edit_distance(left, right),
        _edit_distance(left.replace("_", ""), right.replace("_", "")),
    )
    if len(shorter) >= _NEAR_MISS_MIN_PREFIX and longer.startswith(shorter):
        gap = min(gap, 1)
    budget = 1 if min(len(left), len(right)) <= 4 else 2
    return gap if gap <= budget else None


def _nearest_key(unexpected: str, candidates: list[str]) -> tuple[int, str] | None:
    """The closest candidate to `unexpected` and the gap to it, else
    None.

    Ties break on the name itself, so the suggestion a descriptor gets
    is a function of its content alone -- the same descriptor reports
    the same line on every machine and every run, which is the
    determinism every other fact this module reports already has."""
    scored = [
        (gap, name) for name in candidates if (gap := _key_gap(unexpected, name)) is not None
    ]
    return min(scored) if scored else None


def _resolve_local_ref(root: dict, ref: str) -> dict:
    """Resolve a local JSON pointer (`#/$defs/verifier`) against the root
    schema. Anything else -- a remote `$id`, an unresolvable pointer --
    resolves to an empty schema, which contributes no property names, so
    an external ref costs nothing and guesses nothing."""
    if not ref.startswith("#/"):
        return {}
    node = root
    for token in ref[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and token in node:
            node = node[token]
        elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
            node = node[int(token)]
        else:
            return {}
    return node if isinstance(node, dict) else {}


def _schema_property_paths(
    schema: object,
    root: dict,
    path: str = "$",
    _seen: frozenset[int] = frozenset(),
) -> list[tuple[str, str]]:
    """Every property the descriptor schema names, as (JSON path, name).

    Read off the schema rather than kept as a hand-maintained list, so a
    did-you-mean pointing at a key the schema has since dropped cannot
    go stale -- naming a path the schema no longer accepts is worse than
    naming nothing.

    Only `properties` contributes names: an open object (the
    `verifier_policy` per-cluster overrides, chainlink #76) accepts any
    key, so there is no correct home to point a rejected key at, and a
    key array's element schema contributes no key of its own either."""
    if not isinstance(schema, dict) or id(schema) in _seen:
        return []
    seen = _seen | {id(schema)}
    if "$ref" in schema:
        return _schema_property_paths(_resolve_local_ref(root, schema["$ref"]), root, path, seen)
    found: list[tuple[str, str]] = []
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, subschema in properties.items():
            child = f"{path}.{name}"
            found.append((child, name))
            found.extend(_schema_property_paths(subschema, root, child, seen))
    items = schema.get("items")
    if isinstance(items, dict):
        found.extend(_schema_property_paths(items, root, f"{path}[]", seen))
    for keyword in ("if", "then", "else", "allOf", "anyOf", "oneOf"):
        node = schema.get(keyword)
        for subschema in node if isinstance(node, list) else [node]:
            found.extend(_schema_property_paths(subschema, root, path, seen))
    return found


def _misplaced_key(
    unexpected: str, path: str, property_paths: list[tuple[str, str]]
) -> tuple[str, str] | None:
    """(the real key's JSON path, the object that key belongs to) when
    `unexpected` is a near miss of a property the descriptor schema names
    somewhere OTHER than the object being checked, else None.

    This is the case the pilot's own probe matrix ran into: it wanted
    `verifier_policy.default` and wrote `defualt` at the top level, so
    the key is genuinely not permitted where it was written, but naming
    only the top-level permitted list ("did you mean `crates`?") would
    send the user after the wrong key entirely. Saying where the key
    they were reaching for actually lives is the only actionable answer,
    and it is the same answer for every misplaced key."""
    best: tuple[int, str, str] | None = None
    for json_path, name in property_paths:
        home = json_path.rsplit(".", 1)[0] if "." in json_path else "$"
        if home == path:
            continue
        gap = _key_gap(unexpected, name)
        if gap is None:
            continue
        if best is None or (gap, json_path) < (best[0], best[1]):
            best = (gap, json_path, home)
    return (best[1], best[2]) if best else None


def _did_you_mean(
    unexpected: list[str], path: str, permitted: list[str], property_paths: list[tuple[str, str]]
) -> str:
    """The trailing `; did you mean ...?` clause for the rejected keys, or
    "" when there is nothing to add (chainlink #106).

    The three failure kinds -- a key the schema never had, a mistyped key
    that is one of the permitted ones, and a real key written in the wrong
    object -- used to produce one undifferentiated "unexpected property"
    line, so the user had to eyeball a flat alphabetical dump of every
    permitted name to recover what a single suggestion would have said.

    Empty for a genuinely unknown key, so the chainlink #73 diagnostic --
    quoted verbatim in docs/mode-p-cli-flow.md, and the message a caller
    may already match on -- is byte-identical in exactly the case that
    never had a better answer to give."""
    clauses: list[str] = []
    for key in unexpected:
        nearest = _nearest_key(key, permitted)
        if nearest is not None:
            suggestion = f"{nearest[1]!r}"
            note = ""
        else:
            misplaced = _misplaced_key(key, path, property_paths)
            if misplaced is None:
                continue
            suggestion = f"{misplaced[0]!r}"
            note = f"a property of {misplaced[1]}, not of {path}"
        if len(unexpected) > 1:
            clauses.append(f"{suggestion} (for {key!r}" + (f"; {note}" if note else "") + ")")
        else:
            clauses.append(f"{suggestion} ({note})" if note else suggestion)
    return f"; did you mean {', '.join(clauses)}?" if clauses else ""


def _diagnostic_line(error, property_paths: list[tuple[str, str]]) -> str:
    """One actionable line for a schema violation: the offending value's
    JSON path, what was wrong, the permitted alternatives, and -- when the
    rejected key is a near miss of one the schema does name -- which one
    it looks like (chainlink #73, #106).

    The generic fallback is jsonschema's own message, already prefixed
    with the JSON path; the special cases below exist because the raw
    messages name neither the rejected property nor what was allowed:
    additionalProperties reports the whole object as "$" with the
    unexpected key buried in prose, and the if/then/else guard prints the
    entire descriptor as the "instance"."""
    path = error.json_path
    validator_name = error.validator
    schema = error.schema if isinstance(error.schema, dict) else {}
    if validator_name == "additionalProperties" and isinstance(error.instance, dict):
        properties = schema.get("properties", {})
        unexpected = [key for key in error.instance if key not in properties]
        if unexpected:
            names = ", ".join(repr(key) for key in unexpected)
            permitted = sorted(properties)
            allowed = ", ".join(permitted) if permitted else "(none)"
            return (
                f"{path}: unexpected propert{'y' if len(unexpected) == 1 else 'ies'} "
                f"{names}; permitted: {allowed}"
                + _did_you_mean(unexpected, path, permitted, property_paths)
            )
    if validator_name == "enum" and "enum" in schema:
        return f"{path}: {error.instance!r} is not one of {schema['enum']}"
    if validator_name == "const" and "const" in schema:
        return f"{path}: must be {schema['const']!r} (got {error.instance!r})"
    if validator_name == "required" and isinstance(error.instance, dict):
        missing = [name for name in (error.validator_value or []) if name not in error.instance]
        if missing:
            names = ", ".join(repr(name) for name in missing)
            return f"{path}: missing required propert{'y' if len(missing) == 1 else 'ies'} {names}"
    if validator_name == "not":
        forbidden = schema.get("not", schema)
        return f"{path}: should not be valid under {forbidden}"
    return f"{path}: {error.message}"


def schema_diagnostics(data: dict) -> list[str]:
    """One actionable line per schema violation in `data`, in validator
    order, deduplicated (chainlink #73).

    `check`/`status` surface these so an invalid project descriptor
    names the offending property -- with its JSON path, the permitted
    alternatives, and the near-miss the rejected key looks like -- instead
    of reporting `conditions: [invalid_input]` next to an empty findings
    list, which left a descriptor typo indistinguishable from a missing
    descriptor (chainlink #73, #106).

    The schema's own property paths are read once here rather than per
    rejected key, so the did-you-mean for a misplaced key is available to
    every line without re-walking the schema for each one."""
    schema = json.loads(DESCRIPTOR_SCHEMA_PATH.read_text())
    validator = make_validator(schema)
    property_paths = _schema_property_paths(schema, schema)
    lines: list[str] = []
    for error in validator.iter_errors(data):
        line = _diagnostic_line(error, property_paths)
        if line not in lines:
            lines.append(line)
    return lines


def read_descriptor_text(path: Path) -> str:
    """Read `path` as text, or raise `ProjectDescriptorError` naming it.

    The one place a descriptor reaches the filesystem (chainlink #108).
    Every command that needs a descriptor goes through
    `load_project_descriptor`, so guarding here is what stops an
    uninitialized workspace from reaching the user as a raw
    `FileNotFoundError` traceback through `extract-c-static`, `validate`,
    or any of the other commands that call the same loader -- a single
    guard, rather than a per-command `try`/`except` that two commands
    could word differently.

    The message stays inside the family the rest of the surface already
    uses for this exact failure (`error: cannot read project descriptor
    <path>: ...`, chainlink #83), so the surfaces that wrap a call site
    with their own prefix and the ones that print `error: {e}` say the
    same thing. The missing-file case additionally names the command that
    creates the file, because in the overwhelmingly common case the path
    that is missing *is* the workspace default an uninitialized workspace
    never had -- `ligature migrate --upgrade` already reports the same
    condition as `workspace is not initialized; run ligature init ...`.
    """
    try:
        return path.read_text()
    except FileNotFoundError:
        raise ProjectDescriptorError(
            f"cannot read project descriptor {path}: no such file -- run `ligature init --mode <mode>` "
            "to create one, or pass --descriptor <path> to name a descriptor that exists",
            diagnostics=[f"does not exist: {path}"],
        ) from None
    except IsADirectoryError:
        raise ProjectDescriptorError(
            f"cannot read project descriptor {path}: it is a directory, expected a file",
            diagnostics=[f"is a directory, expected a file: {path}"],
        ) from None
    except UnicodeDecodeError as exc:
        raise ProjectDescriptorError(
            f"cannot read project descriptor {path}: not valid UTF-8 text: {exc}",
            diagnostics=[f"not valid UTF-8 text: {exc}"],
        ) from None
    except OSError as exc:
        # Belt and braces for what the three handlers above cannot see: a
        # permission denial, a path replaced between the caller's existence
        # check and this read.
        raise ProjectDescriptorError(
            f"cannot read project descriptor {path}: {exc}",
            diagnostics=[f"cannot read: {exc}"],
        ) from None


def load_project_descriptor(path: Path) -> dict:
    schema = json.loads(DESCRIPTOR_SCHEMA_PATH.read_text())
    validator = make_validator(schema)

    text = read_descriptor_text(path)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProjectDescriptorError(
            f"project descriptor {path} is not valid JSON: {exc}",
            diagnostics=[f"not valid JSON: {exc}"],
        ) from exc
    errors = list(validator.iter_errors(data))
    if errors:
        diagnostics = schema_diagnostics(data)
        raise ProjectDescriptorError(
            f"project descriptor {path} is invalid:\n"
            + "\n".join(f"  - {line}" for line in diagnostics),
            diagnostics=diagnostics,
        )
    return data


def is_underscore_artifact_path(path: Path, specs_search_root: Path) -> bool:
    """True when `path` has an underscore-prefixed path component relative
    to `specs_search_root` -- i.e. it lives inside one of the canonical
    artifact directories this module names (`_boundaries`,
    `_interactions`, `_bridges`, `_exemptions`, `_protocol_debt`,
    `_conflicts`; also `_witnesses`), not in the concept-spec tree itself.

    Cross-file concept resolution must exclude these: several artifacts
    (witness specs, bridge specs) carry their own top-level `concept`
    field, so an unfiltered scan mistakes an artifact that merely names a
    concept for the concept spec that defines it, and reports a spurious
    ambiguity when both exist. One shared predicate so
    validate_witness.py, gate_g18.py, select_pilot_cluster.py, and
    validate_boundary_contracts.py cannot drift apart on the convention
    (chainlink #69)."""
    return any(part.startswith("_") for part in path.relative_to(specs_search_root).parts)


def boundary_dir_for(crate: dict, workspace: Path) -> Path:
    """plan.md's canonical layout, §2: crates/*/specs/_boundaries/*.json --
    always this exact path relative to the crate root, not a separate
    descriptor field."""
    return (workspace / crate["crate_dir"] / "specs" / "_boundaries").resolve()


def boundary_dirs_for_descriptor(descriptor: dict, workspace: Path) -> list[Path]:
    return [boundary_dir_for(crate, workspace) for crate in descriptor["crates"]]


def interaction_dir_for(crate: dict, workspace: Path) -> Path:
    """plan.md §5.1's canonical layout: crates/*/specs/_interactions/*.json,
    the same discipline as boundary_dir_for."""
    return (workspace / crate["crate_dir"] / "specs" / "_interactions").resolve()


def interaction_dirs_for_descriptor(descriptor: dict, workspace: Path) -> list[Path]:
    return [interaction_dir_for(crate, workspace) for crate in descriptor["crates"]]


def exemption_dir_for(crate: dict, workspace: Path) -> Path:
    """plan.md §5.2's canonical layout: crates/*/specs/_exemptions/*.json,
    the same discipline as boundary_dir_for."""
    return (workspace / crate["crate_dir"] / "specs" / "_exemptions").resolve()


def exemption_dirs_for_descriptor(descriptor: dict, workspace: Path) -> list[Path]:
    return [exemption_dir_for(crate, workspace) for crate in descriptor["crates"]]


def protocol_debt_dir_for(crate: dict, workspace: Path) -> Path:
    """plan.md §5.3's canonical layout: crates/*/specs/_protocol_debt/*.json,
    the same discipline as boundary_dir_for."""
    return (workspace / crate["crate_dir"] / "specs" / "_protocol_debt").resolve()


def bridge_dir_for(crate: dict, workspace: Path) -> Path:
    """plan.md §8.2's canonical layout: crates/*/specs/_bridges/*.json,
    the same discipline as boundary_dir_for. Crate-scoped, not
    workspace-level, since a bridge is tied to one boundary_id, which is
    itself always in the same crate as its boundary contract."""
    return (workspace / crate["crate_dir"] / "specs" / "_bridges").resolve()


def bridge_dirs_for_descriptor(descriptor: dict, workspace: Path) -> list[Path]:
    return [bridge_dir_for(crate, workspace) for crate in descriptor["crates"]]


def protocol_debt_dirs_for_descriptor(descriptor: dict, workspace: Path) -> list[Path]:
    return [protocol_debt_dir_for(crate, workspace) for crate in descriptor["crates"]]


def evidence_dir_for(workspace: Path) -> Path:
    """plan.md §11's canonical layout: evidence/*.json. Unlike every dir
    helper above, this takes no `crate` -- evidence is workspace-level,
    not crate-scoped (plan.md §7's own artifact_manifest worked example
    lists evidence/E-0143.json with no crate prefix, alongside
    docs/reliance-policy.md, also workspace-level)."""
    return (workspace / "evidence").resolve()


def conflict_dir_for(workspace: Path) -> Path:
    """plan.md §11's canonical layout: specs/_conflicts/*.json. Also
    workspace-level, matching evidence_dir_for's own scope -- see that
    function's docstring."""
    return (workspace / "specs" / "_conflicts").resolve()
