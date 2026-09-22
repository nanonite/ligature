#!/usr/bin/env python3
"""Atomic text writes, shared across every generator that must honor
"failure means no mutation" at the I/O layer, not just the logic layer.

Factored out of generate_witness.py (chainlink #28's own atomic-write
contract) rather than re-derived a second time for
generate_feature_ledger.py (chainlink #34) -- the same "one module
imports another's helper rather than re-deriving it" precedent this
codebase applies throughout (e.g. gate_g9.py importing from gate_g14.py,
gate_g20.py importing from gate_g19.py).

File modes (chainlink #63): `tempfile.mkstemp` always creates its temp
file 0600, and `os.replace` carries that mode onto the destination, so
before this fix every file written through here ended up owner-only --
invisible until a real, persistent target was initialized (#60's
neargye-workspace acceptance run). The mode is now normalized *after* the
atomic rename, so the rename's no-partial-write guarantee is untouched:
a genuinely new file gets what a normal umask-respecting `open()` would
have produced (`0o666 & ~umask`), while an existing file keeps the mode
it already had (an operator who deliberately made a managed file
group-writable must not have that silently reverted by a later upgrade).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _umask_default_mode() -> int:
    """The mode a normal, umask-respecting `open()` would give a new file:
    `0o666` masked by the process umask. Read via the `os.umask(0)` /
    `os.umask(saved)` round-trip (there is no direct peek), which is safe
    here because this codebase is single-threaded -- `umask` is
    process-global in CPython, so a concurrent thread could observe the
    momentary 0 (confirmed: no threading/multiprocessing anywhere under
    scripts/ or tests/)."""
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


def write_atomically(destination: Path, content: str) -> None:
    """Write `content` to `destination` such that any failure partway
    through -- disk full, process killed, an interrupted syscall -- leaves
    whatever was already at `destination` completely untouched, never
    truncated or partially overwritten, and no temporary file left behind.

    Writes to a temporary file in the SAME directory as `destination`
    first (a cross-filesystem temp dir would make the final replace a
    copy, not a rename, reopening exactly the window this exists to
    close), flushes and fsyncs it, then atomically replaces the
    destination with `os.replace` -- POSIX guarantees that call is atomic
    for a rename within one filesystem, so a reader can only ever observe
    the old complete file or the new complete file, never a mixture.

    The destination's mode is then normalized (see the module docstring):
    a pre-existing destination keeps its own permission bits; a genuinely
    new one gets the umask-derived default. `os.chmod` runs after
    `os.replace`, never on the temp file before it, so the mode fix cannot
    widen a mkstemp-then-crash window and cannot disturb the rename's
    atomicity guarantee."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Only the nine permission bits are carried forward. An operator's
        # deliberate 0640/0664 stays, but setuid/setgid/sticky are NOT
        # propagated onto wholly replaced content -- that would be a
        # privilege footgun, not preservation of intent.
        existing_mode: int | None = os.stat(destination).st_mode & 0o777
    except FileNotFoundError:
        existing_mode = None
    fd, tmp_name = tempfile.mkstemp(dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, destination)
        os.chmod(destination, existing_mode if existing_mode is not None else _umask_default_mode())
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
