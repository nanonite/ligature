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
