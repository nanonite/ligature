# External evidence extension point

Ligature keeps its first-class verifier vocabulary — `creusot-deductive-check`,
`verus-deductive-check`, and `kani-bounded-model-check` — because each has a
reader that re-opens the verifier's own result. A tool outside that set can
still participate through one generic, versioned extension point instead of
being squeezed into a deductive kind or left out of the workflow entirely.

## 1. Declare the tool in the project descriptor

`project-descriptor.json` may carry an optional `external_evidence` array. Each
entry declares one evidence kind and its complete binding:

```json
{
  "external_evidence": [
    {
      "schema_version": "1.0",
      "kind": "external-dudect-constant-time",
      "tool": "dudect",
      "tool_version": "0.2.0",
      "tool_sha256": "sha256:…",
      "command": "dudect --input crates/scheduler/src/lib.rs --output result.json",
      "result_format": "ligature-external-evidence-result/1.0",
      "claims": ["timing-independence", "side-channel-resistance"],
      "closure_ceiling": "statistical"
    }
  ]
}
```

The `external-` namespace is reserved away from the built-in kinds. A descriptor
may not redeclare a built-in kind, and a kind may appear only once — a reliance
references one declaration by kind, so duplicates would make the intended tool
depend on list order.

A reliance or work-package guarantee references the declared kind exactly like
a built-in one:

```json
"required_assurance": {
  "required_claims": ["timing-independence"],
  "accepted_evidence_kinds": ["external-dudect-constant-time"]
}
```

A work-package manifest's `provided_guarantees[].harness` is the external kind
itself. `validate-work-package`/G14 refuse an undeclared kind; an external
harness whose required assurance does not accept that exact kind is refused
too, because the record it produces could never satisfy the requirement.

## 2. Produce the versioned result

The tool (or a small wrapper) emits a
`ligature-external-evidence-result/1.0` document described by
`docs/external-evidence-result-schema.json`. The important fields are:

- `obligation_id` — exactly one obligation this observation speaks for;
- `evidence_kind` — the declared `external-<name>` kind;
- `claim` — one of the non-functional claims, and `pass`/`fail`;
- `tool` — name, version, and SHA-256 of the tool build;
- `source.sha256` — digest over the exact Rust source snapshot under the work
  package's allowed write set, computed the same way Verus/Kani source binding
  is computed;
- `closure_ceiling` — `statistical` or `observational`;
- `result` — the tool-specific payload, retained for reviewers.

`record-assurance --external-result <obligation>=<path>` is the intake. It
re-reads the result, checks the format version, obligation, evidence kind,
pinned tool identity, claim vocabulary, closure ceiling, and current source
snapshot, and refuses a result older than the sources it observes. The written
achieved record carries all of those bindings in `evidence.scope`
(`external_result_sha256`, `external_tool`, `external_tool_version`,
`external_tool_sha256`, `external_source_sha256`, `external_claim`,
`closure_ceiling`, plus `proof_targets = external-result:<path>`).

## 3. What G14 reports and what it can never claim

G14 re-opens the same result through the same reader. A changed result byte, a
changed source file, or a changed declared tool version/hash blocks the
cluster; it never re-uses a result the descriptor no longer pins. External
records appear in `gate-g14 --json` under `evaluated_obligation_records` /
`rechecked_proof_artifacts` with `evidence.kind`, `evidence.verifier` (the tool
identity), `evidence.harness` (the external kind), and the binding keys above
in `evidence.scope`.

The closure consequence is explicit:

- an external record has a non-functional claim (`timing-independence` or
  `side-channel-resistance`) that is disjoint from `postcondition-holds` and
  `callee-precondition-established`; `satisfies()` compares claim kinds
  exactly, so an external observation can never satisfy a deductive
  requirement by accident;
- external evidence is neither deductive nor bounded-exhaustive. A cluster
  whose transitive closure contains any external evidence may not declare
  `closure_kind: deductive` or `closure_kind: bounded`; G14 blocks those and
  points at `closure_kind: partial`, the honest declaration of a partially
  verified cluster.

The extension point can therefore add a tool without widening the first-class
vocabulary or weakening what a deductive kind means. Promoting a tool to
first-class later remains a normal Ligature release: add a dedicated reader,
a built-in evidence kind, and the stronger closure semantics that reader
actually supports.
