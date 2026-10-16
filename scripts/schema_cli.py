#!/usr/bin/env python3
"""Offline inspection and export of schemas shipped with this Ligature build.

The resource inventory is also bundled and is the same registry the zipapp
builder uses. A schema export carries the installed Ligature identity and
hashes of the exact schema documents, so copied files retain their provenance
without a source checkout or network lookup.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from atomic_write import write_atomically
import adjudicator
import resources

INVENTORY_PATH = "docs/implementation-inventory.json"
_BUNDLE_DISPOSITIONS = frozenset({"runtime-required", "installed-template"})
_RELATED_SCHEMAS = {
    "closure-profile": ("degradation-record",),
    "degradation-record": ("closure-profile",),
}


class SchemaError(Exception):
    pass


def _schema_name(resource_path: str) -> str:
    name = Path(resource_path).name
    for suffix in (".schema.json", "-schema.json", ".json"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _identity() -> dict:
    info = adjudicator.current_identity()
    return {
        "product_name": info["product_name"],
        "product_version": info["version"],
        "adjudicator": {
            "kind": info["kind"],
            "verified": info["verified"],
            "content_hash": info["content_hash"],
            "source_commit": info["source_commit"],
        },
    }


def schema_catalog() -> dict[str, dict]:
    """Load actual JSON Schema documents that this build ships.

    `implementation-inventory.json` itself appears in the builder's resource
    list because it drives bundling, but it is project data, not a JSON Schema;
    documents without a top-level `$schema` declaration are omitted here.
    """
    inventory_path = resources.resource_path(INVENTORY_PATH)
    try:
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaError(f"cannot read bundled schema inventory {INVENTORY_PATH}: {exc}") from exc

    catalog: dict[str, dict] = {}
    for entry in inventory.get("schemas", []):
        if entry.get("disposition") not in _BUNDLE_DISPOSITIONS:
            continue
        relative_path = entry.get("path")
        if not isinstance(relative_path, str):
            continue
        path = resources.resource_path(*Path(relative_path).parts)
        try:
            raw = path.read_bytes()
            document = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SchemaError(f"cannot read bundled schema {relative_path}: {exc}") from exc
        if not isinstance(document, dict) or not isinstance(document.get("$schema"), str):
            # The implementation inventory is bundled as the authoritative
            # resource index; it is not itself a JSON Schema document.
            continue
        name = _schema_name(relative_path)
        if name in catalog:
            raise SchemaError(f"bundled schema name {name!r} is ambiguous")
        catalog[name] = {
            "name": name,
            "path": relative_path,
            "schema_version": adjudicator.schema_version_of(document),
            "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "related_schema_names": list(_RELATED_SCHEMAS.get(name, ())),
            "document": document,
        }
    if not catalog:
        raise SchemaError("the installed bundle contains no inspectable JSON Schemas")
    return catalog


def _schema_payload(name: str, catalog: dict[str, dict]) -> dict:
    schema = catalog.get(name)
    if schema is None:
        available = ", ".join(sorted(catalog))
        raise SchemaError(f"unknown schema {name!r}; run `ligature schema list` (available: {available})")
    related = []
    for related_name in schema["related_schema_names"]:
        item = catalog.get(related_name)
        if item is None:
            raise SchemaError(
                f"schema {name!r} references bundled schema {related_name!r}, but that schema is missing"
            )
        related.append({key: value for key, value in item.items() if key != "related_schema_names"})
    primary = {key: value for key, value in schema.items() if key != "related_schema_names"}
    return {
        "format": "ligature.schema-export.v1",
        **_identity(),
        "schema": primary,
        "related_schemas": related,
    }


def render_schema_list(as_json: bool = False) -> str:
    catalog = schema_catalog()
    identity = _identity()
    entries = [
        {key: value for key, value in catalog[name].items() if key != "document"}
        for name in sorted(catalog)
    ]
    if as_json:
        return json.dumps(
            {"format": "ligature.schema-list.v1", **identity, "schemas": entries},
            indent=2,
            sort_keys=True,
        ) + "\n"

    adjudicator_info = identity["adjudicator"]
    lines = [
        f"Ligature {identity['product_version']} schema catalog",
        f"adjudicator: {adjudicator_info['kind']} (verified: {adjudicator_info['verified']})",
        f"bundle SHA-256: {adjudicator_info['content_hash'] or '(unattested source checkout)'}",
        f"{len(entries)} bundled JSON Schema document(s):",
    ]
    for entry in entries:
        refs = f"; related: {', '.join(entry['related_schema_names'])}" if entry["related_schema_names"] else ""
        lines.append(
            f"  {entry['name']}  schema={entry['schema_version']}  {entry['sha256']}  "
            f"{entry['path']}{refs}"
        )
    return "\n".join(lines) + "\n"


def render_schema(name: str, as_json: bool = False) -> str:
    payload = _schema_payload(name, schema_catalog())
    if as_json:
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"

    identity = payload["adjudicator"]
    schema = payload["schema"]
    lines = [
        f"Ligature {payload['product_version']} schema export",
        f"adjudicator: {identity['kind']} (verified: {identity['verified']})",
        f"bundle SHA-256: {identity['content_hash'] or '(unattested source checkout)'}",
        f"schema: {schema['name']}  version={schema['schema_version']}  {schema['sha256']}",
        f"resource: {schema['path']}",
    ]
    if payload["related_schemas"]:
        for related in payload["related_schemas"]:
            lines.extend((
                f"--- related schema: {related['name']}  version={related['schema_version']}  {related['sha256']} ---",
                json.dumps(related["document"], indent=2, sort_keys=True),
            ))
    lines.extend(("--- JSON Schema document ---", json.dumps(schema["document"], indent=2, sort_keys=True)))
    return "\n".join(lines) + "\n"


def export_schema(name: str, output: Path) -> str:
    payload = _schema_payload(name, schema_catalog())
    target = output.expanduser()
    if target.exists() or target.is_symlink():
        raise SchemaError(f"refusing to overwrite existing schema export {target}")
    try:
        write_atomically(target, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    except OSError as exc:
        raise SchemaError(f"cannot write schema export {target}: {exc}") from exc
    return (
        f"exported {name} schema from Ligature {payload['product_version']} to {target}\n"
        f"  schema hash: {payload['schema']['sha256']}\n"
        f"  bundle hash: {payload['adjudicator']['content_hash'] or '(unattested source checkout)'}\n"
    )
