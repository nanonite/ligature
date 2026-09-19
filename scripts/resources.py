#!/usr/bin/env python3
"""Cwd-independent packaged-resource discovery (chainlink #57).

Before #57, every runtime module that needed a schema/prompt/template did
`Path(__file__).resolve().parent.parent / "docs" / ...`. That is exact only
while running from a source checkout, where `__file__` is a real file in a
real directory tree. Inside a zipapp (this project's chosen runtime
contract), `__file__` resolves to a path *inside the zip archive*, so the
parent chain points at nothing and every resource read fails.

This module is the one abstraction the rest of the runtime goes through:

* From a source checkout it returns the repository root, exactly as the old
  `parent.parent` did -- so `python3 scripts/pipeline.py` and the whole
  unittest suite keep working unchanged.
* From a packaged zipapp it locates the bundled `ligature_data` package via
  `importlib.resources` (the stdlib's own packaged-resource API), extracts
  its contents once per process to a private temporary directory, and
  returns that real directory. Callers still get a `Path` they can
  `read_text()`/`is_file()`/join, so no call site needs a second code path
  for packaged mode.

The extraction target is `tempfile.mkdtemp()` (absolute, outside the
workspace and independent of cwd); it is removed at interpreter exit. No
resource access ever depends on the current working directory.
"""
from __future__ import annotations

import atexit
import importlib.resources
import shutil
import tempfile
from pathlib import Path

_PACKAGE = "ligature_data"


def _is_source_checkout() -> bool:
    """True when this module is a real file on disk, i.e. the source
    checkout. Inside a zipapp `__file__` names a member path inside the
    archive, which is not a real file."""
    try:
        return Path(__file__).is_file()
    except OSError:
        return False


_IS_SOURCE = _is_source_checkout()
_SOURCE_ROOT = Path(__file__).resolve().parent.parent
_EXTRACTED: Path | None = None


def _copy_tree(source, destination: Path) -> None:
    for entry in source.iterdir():
        target = destination / entry.name
        if entry.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            _copy_tree(entry, target)
        else:
            target.write_bytes(entry.read_bytes())


def _extract_package() -> Path:
    """Extract the bundled resource package once, to an absolute temp dir.
    Uses `importlib.resources.files` so a zipapp, an installed wheel, or a
    plain directory all resolve identically."""
    global _EXTRACTED
    if _EXTRACTED is not None:
        return _EXTRACTED
    base = importlib.resources.files(_PACKAGE)
    destination = Path(tempfile.mkdtemp(prefix="ligature-data-"))
    _copy_tree(base, destination)
    atexit.register(shutil.rmtree, destination, ignore_errors=True)
    _EXTRACTED = destination
    return destination


def is_packaged() -> bool:
    """True when running from a packaged distribution rather than the
    source checkout. Distinct from `adjudicator`'s attestation result:
    this only says *where* the code is running from, not whether the build
    was verified."""
    return not _IS_SOURCE


def resource_root() -> Path:
    """The directory every bundled resource path is relative to. A real,
    absolute filesystem path in both modes, independent of cwd."""
    if _IS_SOURCE:
        return _SOURCE_ROOT
    return _extract_package()


def resource_path(*parts: str) -> Path:
    """Resolve a workspace-relative resource path (e.g.
    `resource_path("docs", "boundary-contract-schema.json")`)."""
    return resource_root().joinpath(*parts)


def resource_text(*parts: str) -> str:
    return resource_path(*parts).read_text()
