# concept-to-code: modifications requested for the reliance-graph pipeline

Tracked as chainlink issue #3 in this workspace. `vendor/concept-to-code` is a
read-only submodule pin — nothing in this list has been applied there. This
is the change request to hand to the concept-to-code repo owner.

## Context

concept-to-code already implements this pipeline's layer 1 (concepts) and
layer 2 (intra-concept contracts): Step A (concept + co-analysis JSON), Step
B (`emit_stubs.py`), Step C (verifier round-trip). The reliance-graph
pipeline is a new layer 3 that sits above it — cross-concept interaction (I),
realized calls (C), and contractual reliance (O) — and needs concept-to-code
to expose a few things it currently doesn't, or confirms a few things it
already does correctly.

## What's already compatible — no change needed

- **`cluster` (required, top-level, `schemas/spec.schema.json`)** — already
  domain-agnostic (`parsing`, `data-model`, `numeric-kernel` are the
  documented examples). This is directly reusable as the grouping key for
  closure profiles and work-package manifests.
- **`verifier` (required, top-level)** — already per-concept. Directly
  reusable for `closure_kind` / cross-verifier-composition detection
  (CG1 in the reliance-graph plan).
- **`depends_on: [{crate, concept, reason}]`** — already a generic,
  declarative semantic-dependency field, consumed today by
  `spec_workspace.py discover/impact/graph`. This is a legitimate partial
  source for the reliance graph's S-graph (candidate interaction edges) —
  see the gap below on making that consumption machine-readable.

## Gaps

### 1. Machine-readable project descriptor

Today, project shape is entirely CLI flags: `--crate-dir`,
`--contracts-crate`, `--specs-search-root`. There is no on-disk artifact a
downstream tool can read to learn "what does this project's concept-to-code
setup look like" without re-deriving it from invocation history.

The reliance-graph pipeline's Stage 0 needs exactly this — a per-project
structure descriptor is its actual external input. Request: an optional
persisted config (e.g. `concept-to-code.toml` or `.concept-to-code/project.json`
at the workspace root) that records the crate-dir / contracts-crate /
specs-search-root bindings concept-to-code is already using per crate, so
other tooling can read the same bindings without guessing. CLI flags should
keep working as overrides.

### 2. `--json` output for `spec_workspace.py discover` / `graph` / `impact`

All three commands are `print()`-only today (confirmed:
`spec_workspace.py:194,382,359`). The reliance graph's S-graph (candidate
interaction edges) wants `depends_on` edges as structured data, not scraped
text. Request: a `--json` flag matching the one `emit_stubs.py`'s sibling
tools already use elsewhere in this ecosystem, emitting the same edges
`cmd_graph`/`cmd_discover` print today as `{concept, dependencies: [{crate,
concept, link, reason}]}`.

### 3. Origin/provenance fields for ported (Mode P) concepts — recommend *not* adding

The reliance-graph plan's evidence schema (§11) wants an `origin: {repository,
commit, symbol, path, content_hash, line_hint}` per evidence record when a
concept is being ported from a non-Rust source (the C++→Rust port test
track). concept-to-code's spec JSON has no such field today.

Recommendation: **do not add this to concept-to-code.** It belongs one layer
up, in the reliance-graph pipeline's own evidence records, which reference a
concept by crate+name. Adding source-provenance metadata to the concept spec
itself would blur the S/I/C/O separation the whole design leans on — concept-
to-code should stay concept-and-contract-only. Flagging this only so the
concept-to-code maintainer doesn't independently reach for the same field in
the same place for the same reason.

### 4. No crate-naming convention needed

An earlier draft of the reliance-graph plan hardcoded a crate-name regex
(`^beast-rs-[a-z]+`). That was never a concept-to-code constraint — crate-dir
is just a path there today — and the current plan drops it in favor of the
project descriptor (gap #1) declaring each project's own convention. No
concept-to-code change follows from this.

### 5. `witness_required` marker on a query — decided: extend the schema

The new feature-witness layer (plan §16) needs a per-query marker recording
"this is a declared feature and must have a witness." The natural spot is on
the query itself, next to `pure: true`:

```json
{ "english": "...", "rust_sig": "fn load_factor(&self) -> f64",
  "pure": true, "witness_required": true }
```

But `schemas/spec.schema.json`'s `query` `$def` has `additionalProperties:
false` (confirmed by reading the schema directly), so this is a **real
schema change** on your side, not something a consuming project can bolt on
quietly the way `depends_on` was added at the top level. Two options:

1. **Add `witness_required: boolean` (optional, default `false`) to the
   `query` `$def` in concept-to-code.** Keeps the marker co-located with the
   thing it describes; `emit_stubs.py`'s query handling (`emit_stubs.py:194`)
   only reads `pure` today and wouldn't need to change.
2. **Don't touch concept-to-code — keep the marker in a pipeline-owned
   feature-declaration list** keyed by `crate::Concept::query`, the same
   pattern used for evidence origin (gap #3): reference into concept-to-code
   by name, don't extend its schema.

**Decided: option 1.** Add `witness_required: boolean` (optional, default
`false`) to the `query` `$def` in `schemas/spec.schema.json`. No code change
required in `emit_stubs.py` — its query handling only reads `pure` today
(`emit_stubs.py:194`) and the new field is inert to it. This is the concrete
request to apply in the concept-to-code repo. Tracked as chainlink issue #33
in `ligature-workspace`.

**Implementation status (chainlink #33):** not yet applied upstream — the
vendored submodule remains an unmodified read-only pin
(`vendor/concept-to-code` at `deac5cd`, `heads/main`) and nothing in this
repository edits it. The literal proposed patch — vendor's real `query`
`$def`, field for field, plus exactly this one new optional property — is
recorded as its own artifact at
`docs/concept-to-code-witness-required-schema.json`, so the extension's own
shape is regression-tested and ready the moment the upstream maintainer
applies it, without ever touching the submodule in the meantime.
`tests/test_concept_to_code_witness_required_schema.py` proves: (1) every
field in the proposed schema other than `witness_required` is pulled
verbatim from the live vendored `query` `$def` — a drift guard, not a
one-time copy, so an upstream schema change would fail this test rather than
silently invalidate the proposal; (2) `witness_required: true` and `false`
both validate; (3) omitting the field validates and its schema `default` is
`false`; (4) every non-boolean value (`"true"`, `1`, `0`, `null`, `[]`,
`{}`) is rejected; (5) `additionalProperties: false` still rejects an
unrelated field after the extension; (6) every one of the 11 real query
objects across every vendored fixture/spec `*.json` file still validates
against the extended schema unchanged — the extension is additive, proven
against real documents rather than only hand-written ones. `emit_stubs.py`
confirmed to need no change (re-read at `emit_stubs.py:192-201`: query
handling is `.get()`-based and reads only `english`, `rust_sig`, `pure` —
an unrecognized key is inert to it either way).

### 6. Stable obligation identifier on `constraint` — decided: add `id`

Found while building the boundary-artifact schema (chainlink #9/#11): checked
`schemas/spec.schema.json`'s `command` and `constraint` `$def`s directly
(both `additionalProperties: false`) and confirmed neither has an `id` or
`name` field. A constraint is identified by nothing but its `english` text
and its position in the `constraints` array. Grepped `emit_stubs.py` and
`spec_workspace.py` for any `C00N`-style id generation — none exists either.

This matters because **the reliance-graph plan references obligations by a
stable id everywhere** — `callee_guarantees: [TaskQueue.C003]` (boundary
specs, §2), `obligations: [Chain.C002]` (work-package manifests, §10),
`reliances[].obligation_id` (interaction specs, §5.1), promotion
`artifact_manifest` entries. None of that is implementable against the
current schema: there is nothing stable to put after the dot. Positional
numbering (Nth constraint = `C00N`) is available without any concept-to-code
change but is exactly the kind of fragility this plan works hard to avoid
elsewhere (`spec_workspace.py check`'s drift detection, gate-implementation
hashing, the whole `additionalProperties: false` discipline) — reordering
constraints with zero semantic change would silently renumber every
downstream reference.

Narrower than it first looked: `query` and `command` already have a de
facto stable identifier — their `rust_sig` embeds the Rust function name
(e.g. `fn load_factor(&self) -> f64`), which is already how §16.1's witness
specs reference a query (`query: load_factor`) and is deterministic and
stable without any schema change. **`constraint` is the only one of the
three with nothing to extract** — no `rust_sig`, nothing but `english` text
and array position.

**Decided: add `id: string` (required, pattern `^C\d{3}$`) to `constraint`
in `concept-to-code`.** Author-assigned at Step A time, like every other id
in this ecosystem (`boundary_id`, `witness_id`, `promotion_id`). Stable
under reordering by construction, unlike a positional scheme. This is a real
schema change (`additionalProperties: false` on `constraint`) plus a
backfill of `id` onto every existing constraint in any project already using
concept-to-code — worth stating plainly to whoever applies it, not
minimizing. `command`/`query` need no change. Tracked as chainlink issue #40
in `ligature-workspace`.
