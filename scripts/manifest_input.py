#!/usr/bin/env python3
"""The path guard shared by the two path-taking validators (chainlink #83).

`validate_work_package.validate_file()` and
`validate_promotion_receipt.validate_file()` each took their CLI path
argument straight to `Path.read_text()`, so the only thing standing
between a mistyped argument and the interpreter was pathlib itself: a
missing path escaped as `FileNotFoundError`, a directory as
`IsADirectoryError`, and both reached the user as a raw traceback. The
exit code was already 1 (fail-closed, substance right), but the error
surface named neither the offending argument nor what the command
expected -- date-ligature-030 (found by the date-creusot pilot, chainlink
#14, Stage 7 over Stage 4.5) could only see that *something* raised.

One shared guard, because the defect was one defect: both validators
read their argument through the same shape of code, so fixing each in
place would let the two commands drift apart on the message wording and
on which input shapes count as bad. This module owns both:

  - `ensure_manifest_path()` -- the cheap pre-flight a CLI runs *before*
    it loads anything else (a workspace's project descriptor, a schema),
    so the error names the argument the user actually got wrong rather
    than whatever the workspace happens to lack next.
  - `read_manifest_text()` -- the same checks immediately before the
    read, so library callers of `validate_file()` get the identical
    message without having to remember to pre-flight first.

`ManifestInputError`'s message is already user-facing and carries no
`error: ` prefix -- every CLI surface prints `error: {e}` to stderr and
returns 1, the same shape `PipelineError` gets (pipeline.main() catches
both), so the two never disagree about how a bad path is reported.
"""
from __future__ import annotations

from pathlib import Path


class ManifestInputError(Exception):
    """A manifest/receipt path argument that cannot be read as a file.

    Deliberately not an `OSError` subclass and not a `Finding`: this is
    a wiring error about the argument the command was invoked with, not
    a finding about the artifact's content (the validators' "return
    findings, never raise" idiom is about content) and not a condition
    the filesystem alone can describe -- the message has to say which
    kind of file was expected, which only the caller knows.
    """


def ensure_manifest_path(path: Path, kind: str) -> None:
    """Raise `ManifestInputError` unless `path` is a readable regular file.

    `kind` is the noun the caller's own argument is documented as ("manifest"
    for a work-package manifest, "receipt" for a promotion receipt) and is
    repeated back in every message, so one command can never claim to want
    the other's file.
    """
    if path.is_dir():
        raise ManifestInputError(f"{kind} path is a directory, expected a file: {path}")
    if not path.exists():
        raise ManifestInputError(f"{kind} not found: {path}")
    if not path.is_file():
        raise ManifestInputError(f"{kind} path is not a regular file: {path}")


def read_manifest_text(path: Path, kind: str) -> str:
    """Read `path` as text, or raise `ManifestInputError` naming it.

    The pre-flight checks run first so the common mistakes (missing,
    directory) get their specific message; the `OSError` handlers below
    are the belt-and-braces for everything the pre-flight cannot see --
    a path removed or replaced between the two calls, a permission
    denial, an unreadable special file. Nothing escapes as a bare
    OSError subclass, which is the whole point of this module.
    """
    ensure_manifest_path(path, kind)
    try:
        return path.read_text()
    except IsADirectoryError:
        raise ManifestInputError(f"{kind} path is a directory, expected a file: {path}") from None
    except FileNotFoundError:
        raise ManifestInputError(f"{kind} not found: {path}") from None
    except OSError as exc:
        raise ManifestInputError(f"cannot read {kind} {path}: {exc}") from None
