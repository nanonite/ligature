#!/usr/bin/env python3
"""Shared XML/SVG text escaping.

Factored out of witness_renderer.py's own private `_escape` (chainlink
#28) rather than duplicated a second time for generate_contact_sheet.py
(chainlink #32) -- the same "one module imports another's helper rather
than re-deriving it" precedent atomic_write.py already set for I/O
(chainlink #34's own docstring names it: "the same cross-gate-module
reuse precedent gate_g9.py already set").
"""
from __future__ import annotations


def escape_xml_text(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
