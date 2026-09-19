# Packaging, attestation and release provenance — v1.0 (chainlink #57)

## 1. Runtime contract

`ligature` ships as a **Python zipapp** (`ligature.pyz`), built with the
stdlib `zipapp` module and run by a **system Python interpreter**:

```
python3 ligature.pyz <verb> [args...]
```

This is a deliberate choice, not a default. The project has no runtime
dependency beyond `jsonschema`/`PyYAML` and no `pyproject.toml`/`setup.py`;
a zipapp is the smallest build that fits that minimal-dependency style.
PyInstaller/Nuitka-style self-contained binaries were rejected because they
would bundle an interpreter and a much larger toolchain for a project that
does not need one. The cost of the zipapp contract — a compatible Python on
the target machine — is **not hidden**: every build records its
`required_python` floor, and `doctor`/`version --verify` check and report it
rather than assuming it.

The interpreter floor is **3.10**. `importlib.resources.files` (the
packaged-resource API this distribution uses) exists from 3.9; the runtime
union-type annotations settle the floor at 3.10. A build made on one Python
minor version runs under any interpreter at or above the floor.

## 2. Resource discovery

Before #57, runtime code found schemas/prompts/templates with
`Path(__file__).resolve().parent.parent / "docs" / ...`. That is exact only
from a source checkout; inside a zipapp `__file__` names a member *inside
the archive*, so the parent chain is not a real directory.

All 24 such lookups now go through `scripts/resources.py`:

* from the source checkout it returns the repository root (so
  `python3 scripts/pipeline.py` and the unittest suite are unchanged);
* from the zipapp it locates the bundled `ligature_data` package with
  `importlib.resources`, extracts it once per process to an absolute
  temporary directory removed at exit, and returns that real path.

Resource access never depends on the current working directory. Building the
bundle does not require a hand-maintained file list: it is derived from
`docs/implementation-inventory.json`.

## 3. Bundle manifest derivation

`scripts/build_zipapp.py` reads `docs/implementation-inventory.json` and
copies in exactly the entries whose `disposition` is:

* `runtime-required` — every runtime python module, schema, prompt, and the
  inventory document itself;
* `installed-template` — the descriptor examples, reliance-policy template
  and skill template that `ligature init` (which runs *from this artifact*)
  copies into a target repo;
* `required_at_runtime: true` in `vendored_runtime_assets` — the vendored
  concept-to-code spec schema.

`tests/test_inventory_drift.py` already fails when the inventory and the
real filesystem disagree in either direction, so deriving the bundle from
the inventory keeps that guarantee instead of duplicating it in a second
list that could drift silently.

## 4. Attestation and trust model

The executable is the trusted adjudicator. A built zipapp embeds
`ligature_data/BUILD_ATTESTATION.json` containing:

* `product_name` / `product_version`;
* `required_python` and required `platform`;
* `build_tool` / `build_toolchain`;
* `bundled_schemas` (schema path → its own declared `schema_version`);
* `bundle_manifest` — `sha256` of every archive member except the
  attestation itself;
* `content_hash` — sha256 over the canonical JSON of `bundle_manifest`.

`content_hash` covers member *contents*, not the raw zip container, so it
is independent of zip metadata/compression and reproducible across
rebuilds.

`ligature version --verify` and `ligature doctor`:

1. recompute the bundle manifest and `content_hash` from the running
   archive and compare them to the embedded attestation (any changed,
   added or removed shipped byte fails);
2. check `sys.version_info` against `required_python` and report the
   running platform against the required one;
3. report the product version and the full bundled schema set.

A **source checkout is never `verified: true`**. With no packaged build
there is nothing to hash; the only honest value is `verified: "unknown"`
(see `docs/trust-and-compatibility-boundaries.md` §10). `version --verify`
exits non-zero for a source checkout and for any packaged build whose
attestation does not match.

`ligature init` records the adjudicator that installed a workspace in
`ci/manifest/installation.json`. The installed descriptor's `gate_integrity`
list includes the `@adjudicator` pin, and `project_state`/`doctor` compare
the running executable's identity against it:

* the workspace was initialized by a **packaged** build → the running
  executable must be that same build (same `content_hash`); running from an
  unattested source checkout, or from a different build, fails closed;
* the workspace was initialized by an **unattested source checkout** → only
  an unattested source checkout verifies it ("no build hash to pin");
* a pre-#57 manifest records no adjudicator → reported `unpinned`, never
  silently treated as pinned.

**Authority boundary, stated honestly rather than left implied.**
`content_hash` is computed and verified by the same public function
(`adjudicator.hash_manifest`) in both directions -- there is no signature,
key, or external reference outside the archive itself. `verified: "true"`
therefore proves the running archive's bytes match its own embedded
manifest (nothing was truncated, partially copied, or corrupted since that
manifest was written), and that the identity now running matches whatever
was pinned at `ligature init`. It does **not** prove the build that first
produced a given `ligature.pyz` was itself trustworthy: anyone who can
write a zip archive can compute a self-consistent `content_hash` and
`BUILD_ATTESTATION.json` for arbitrary content, the same way `build_zipapp.py`
does. The genuine security property here is pin *persistence*, not pin
*origin* -- a binary swapped in under an already-initialized workspace is
caught (`tests/test_zipapp_out_of_checkout.py`'s
`test_source_checkout_is_refused_after_a_packaged_init`), but the very
first `ligature init` against a workspace trusts whatever adjudicator
happens to be running it. `PROVENANCE.json`'s `source commit` field exists
for exactly this gap: a human can cross-check it against reviewed git
history before that first `init`, the same manual trust-on-first-use step
every unsigned software distribution ultimately relies on somewhere. A
cryptographic signature tied to a key outside the archive would close this
gap; none is implemented here, and this document does not claim one.

## 5. Building a release

```
python3 scripts/build_zipapp.py --out dist
```

writes into `dist/`:

| file | purpose |
|---|---|
| `ligature.pyz` | the executable zipapp |
| `SHA256SUMS` | sha256 of the archive bytes (distribution integrity) |
| `PROVENANCE.json` | product version, artifact hash, content hash, source commit, bundle derivation and bundled schema set |
| `NOTICE` | project notice |
| `THIRD_PARTY_NOTICES.txt` | licenses of bundled vendored components |

The archive-byte hash in `SHA256SUMS` is the artifact's distribution
integrity check; the `content_hash` in the attestation is the executable's
own identity, pinned by `gate_integrity`. They are different values because
they answer different questions.

## 6. Out-of-checkout verification

`tests/test_zipapp_out_of_checkout.py` builds the artifact, copies only the
`.pyz` to a temporary directory outside the repository, and runs it with
`python3 -I` (isolated mode) and a cwd outside the checkout:

* `version --verify` must exit 0 and report `attested: true`;
* `doctor` must verify the same identity;
* `init --mode port` must install a valid target workspace from the
  artifact alone, and `status --json` must report the adjudicator pin as
  `pinned`.

Passing this test is the proof that the `importlib.resources` migration
actually removed the source-checkout dependency rather than relying on the
checkout happening to be present.
