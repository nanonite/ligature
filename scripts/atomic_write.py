#!/usr/bin/env python3
"""Atomic text writes, shared across every generator that must honor
"failure means no mutation" at the I/O layer, not just the logic layer.

Factored out of generate_witness.py (chainlink #28's own atomic-write
contract) rather than re-derived a second time for
generate_feature_ledger.py (chainlink #34) -- the same "one module
imports another's helper rather than re-deriving it" precedent this
codebase applies throughout (e.g. gate_g9.py importing from gate_g14.py,
gate_g20.py importing from gate_g19.py).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


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
    the old complete file or the new complete file, never a mixture."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, destination)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
