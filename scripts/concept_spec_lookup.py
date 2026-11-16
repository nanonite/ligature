#!/usr/bin/env python3
"""Authoritative, descriptor-rooted discovery of concept-to-code specs.

Cross-artifact validators must not each invent a different rule for finding a
concept spec. This resolver uses the declared search root as authority, checks
candidate documents with Ligature's extended concept-spec schema, and returns
diagnostics that distinguish missing roots/references from invalid,
ambiguous, or misplaced declarations. A spec outside the declared root is
reported only as a diagnostic; it is never used as a fallback.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_SKIP_DIRS = frozenset({".git", ".ligature", "__pycache__", "target", "node_modules"})
_SPEC_SHAPE_KEYS = frozenset({
    "queries", "commands", "constraints", "adversary_table", "english_description",
    "trait_ref", "variants", "implements",
})


@dataclass(frozen=True)
class ConceptSpecLookup:
    status: str
    concept: str
    search_root: Path | None
    document: dict | None = None
    path: Path | None = None
    candidates: tuple[Path, ...] = ()
    detail: str | None = None

    def diagnostic(self) -> str:
        root = str(self.search_root) if self.search_root is not None else "(not supplied)"
        paths = ", ".join(str(path) for path in self.candidates)
        if self.status == "no_search_root":
            return (
                "no --specs-search-root given and no specs_search_root supplied; set "
                "crates[].specs_search_root in project-descriptor.json and rerun the check"
            )
        if self.status == "search_root_missing":
            return (
                f"declared specs_search_root {root!r} does not exist or is not a directory; "
                "correct crates[].specs_search_root or create the declared directory"
            )
        if self.status == "spec_not_found":
            return (
                f"no valid concept spec declares {self.concept!r} under declared "
                f"specs_search_root {root!r}; add the concept spec under that root or correct "
                "crates[].specs_search_root. Ligature will not infer constraint or query IDs"
            )
        if self.status == "mislocated":
            return (
                f"concept spec candidate(s) for {self.concept!r} are outside declared "
                f"specs_search_root {root!r}: {paths}; move the canonical concept spec under "
                "the declared root or update crates[].specs_search_root. The outside candidate "
                "is not used as a fallback"
            )
        if self.status == "ambiguous":
            return (
                f"more than one spec under declared specs_search_root {root!r} "
                f"declares {self.concept!r}: {paths}; the valid concept-spec declarations "
                "cannot be resolved unambiguously; retain one authoritative declaration"
            )
        if self.status == "invalid":
            return (
                f"concept spec candidate for {self.concept!r} under declared "
                f"specs_search_root {root!r} is invalid at {paths}: {self.detail}; repair it "
                "against the bundled concept-to-code schema, then rerun the check"
            )
        return f"concept {self.concept!r} resolution failed under {root!r}"


@lru_cache(maxsize=1)
def _concept_spec_validator():
    # Lazy import avoids an import cycle: validate_concept_spec imports the
    # witness module for concept-to-code's canonical snake_case function.
    from validate_concept_spec import load_validator

    return load_validator()


def _expected_filename(concept: str) -> str:
    from validate_concept_spec import expected_filename

    return expected_filename({"concept": concept})


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _json_paths(root: Path):
    for current, dirs, filenames in os.walk(root, followlinks=False):
        dirs[:] = sorted(
            name for name in dirs
            if name not in _SKIP_DIRS and not (Path(current) / name).is_symlink()
        )
        for filename in sorted(filenames):
            path = Path(current) / filename
            if path.suffix == ".json" and not path.is_symlink():
                yield path


def _looks_like_concept_spec(data: object, expected_filename: str, path: Path) -> bool:
    return (
        isinstance(data, dict)
        and isinstance(data.get("concept"), str)
        and (bool(_SPEC_SHAPE_KEYS.intersection(data)) or path.name == expected_filename)
    )


def _read_candidate(path: Path, concept: str, expected_filename: str):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        if path.name == expected_filename:
            return "invalid", None, f"cannot read JSON ({exc})"
        return "irrelevant", None, None
    if not _looks_like_concept_spec(data, expected_filename, path) or data.get("concept") != concept:
        return "irrelevant", None, None
    errors = list(_concept_spec_validator().iter_errors(data))
    if errors:
        return "invalid", data, errors[0].message
    if path.name != expected_filename:
        return "invalid", data, f"filename must be {_expected_filename(concept)!r}"
    return "valid", data, None


def iter_concept_spec_candidates(root: Path):
    """Yield ``(path, data)`` for every JSON document under ``root`` that is
    shaped like a concept spec.

    This is the discovery half of :func:`find_concept_spec` without a
    concept name to search for, so reporting surfaces can validate each
    declared concept spec once instead of guessing which concepts a
    workspace has and looking each one up. The same predicates apply:
    underscore-prefixed artifact directories are excluded, symlinks are
    never followed, and a document must declare a string ``concept`` plus
    either one of the concept-spec shape keys or the canonical filename
    for that concept. Files that are not readable JSON are skipped here;
    a caller that needs to report an unreadable expected-named file can
    still call :func:`find_concept_spec` with the concept it expects."""
    resolved_root = root.resolve()
    if not resolved_root.is_dir():
        return
    for path in _json_paths(resolved_root):
        resolved = path.resolve()
        if not _is_within(resolved, resolved_root):
            continue
        try:
            relative = path.relative_to(resolved_root)
        except ValueError:
            continue
        if any(part.startswith("_") for part in relative.parts):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or not isinstance(data.get("concept"), str):
            continue
        expected = _expected_filename(data["concept"])
        if _SPEC_SHAPE_KEYS.intersection(data) or path.name == expected:
            yield path, data


def find_concept_spec(
    concept: str,
    specs_search_root: Path | None,
    workspace_root: Path | None = None,
) -> ConceptSpecLookup:
    """Find exactly one schema-valid concept spec under the declared root.

    `workspace_root` is used only to explain that an otherwise plausible
    declaration exists elsewhere. No candidate outside `specs_search_root`
    can become authoritative through this diagnostic scan.
    """
    if specs_search_root is None:
        return ConceptSpecLookup("no_search_root", concept, None)
    root = specs_search_root.resolve()
    if not root.is_dir():
        return ConceptSpecLookup("search_root_missing", concept, root)

    expected = _expected_filename(concept)
    valid: list[tuple[Path, dict]] = []
    invalid: list[tuple[Path, str]] = []
    misplaced_inside: list[Path] = []
    for path in _json_paths(root):
        resolved = path.resolve()
        if not _is_within(resolved, root):
            continue
        kind, data, detail = _read_candidate(path, concept, expected)
        if kind == "irrelevant":
            # A malformed file named exactly as this concept is still an
            # invalid candidate even though it cannot be parsed.
            if path.name == expected and detail is not None:
                invalid.append((path, detail))
            continue
        if any(part.startswith("_") for part in path.relative_to(root).parts):
            misplaced_inside.append(path)
        elif kind == "valid":
            valid.append((path, data))
        else:
            invalid.append((path, detail or "invalid concept spec"))

    if len(valid) > 1:
        return ConceptSpecLookup(
            "ambiguous", concept, root,
            candidates=tuple(path for path, _ in valid),
        )
    if valid and invalid:
        candidates = tuple(sorted([path for path, _ in valid] + [path for path, _ in invalid]))
        return ConceptSpecLookup(
            "ambiguous", concept, root, candidates=candidates,
            detail="valid and invalid declarations coexist",
        )
    if len(valid) == 1:
        path, data = valid[0]
        return ConceptSpecLookup("resolved", concept, root, data, path)
    if invalid:
        path, detail = sorted(invalid, key=lambda item: str(item[0]))[0]
        return ConceptSpecLookup(
            "invalid", concept, root,
            candidates=tuple(path for path, _ in sorted(invalid, key=lambda item: str(item[0]))),
            detail=detail,
        )
    if misplaced_inside:
        return ConceptSpecLookup(
            "mislocated", concept, root,
            candidates=tuple(sorted(misplaced_inside)),
            detail="candidate is inside an underscore-prefixed artifact directory",
        )

    outside: list[Path] = []
    if workspace_root is not None:
        workspace = workspace_root.resolve()
        if _is_within(root, workspace):
            for path in _json_paths(workspace):
                resolved = path.resolve()
                if not _is_within(resolved, workspace) or _is_within(resolved, root):
                    continue
                kind, _data, _detail = _read_candidate(path, concept, expected)
                if kind in ("valid", "invalid"):
                    outside.append(path)
    if outside:
        return ConceptSpecLookup("mislocated", concept, root, candidates=tuple(sorted(outside)))
    return ConceptSpecLookup("spec_not_found", concept, root)
