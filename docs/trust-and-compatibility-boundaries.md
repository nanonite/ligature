# Trust and compatibility boundaries — v1.0

Chainlink #55. What is promised, what is not, and who may write what.

## 1. What is stable public API

- The grammar in `docs/cli-contract.md` (top-level verbs, artifact-kind /
  gate-id / operation / report-id vocabularies, the legacy-alias mapping).
- `schemas/project-state.schema.json` (`ligature status --json`) and
  `schemas/consolidated-check.schema.json` (`ligature check --json`), each
  independently versioned by their own `schema_version`.
- `docs/exit-code-contract.md`'s five codes and their precedence order.
- Every schema under `docs/*-schema.json` and `schemas/*.schema.json` that
  a `validate <kind>` command checks an artifact against — these are
  already versioned (`schema_version` fields, e.g.
  `docs/closure-profile-schema.json`'s `"const": "1.0"`) and that practice
  continues.

## 2. What is explicitly NOT stable public API

- Python module paths and names under `scripts/*.py`.
- Individual gate *implementation* function/file names (`cmd_gate_g14`,
  `gate_g14_workspace`, etc.) — the gate-id `g14` is public; where its code
  lives is not.
- The internal-operation flat commands in `docs/cli-contract.md` §7
  (`extract-c-static`, `check-bridges`, `render-witness`) beyond the
  transition window described there.
- `docs/implementation-inventory.json`'s own field values for `module` /
  `func` (informational cross-references, explicitly not frozen — see the
  schema's own description).

## 3. Which output schemas are versioned compatibility contracts

`project-state` (1.0) and `consolidated-check` (1.0), plus every artifact
schema already in `docs/`/`schemas/`. A breaking change to any of these
requires a new `schema_version` and a migration note; #59 owns the
specific migration mechanics for the assumption registry, not this
document, but the versioning discipline here applies uniformly.

`docs/implementation-inventory.json` + its schema is versioned
(`inventory_version`) but is **not** a compatibility contract in the same
sense — it describes this repository's own current contents and is
expected to change every time a runtime file is added, removed, or
reclassified. Its stability guarantee is "drift-tested," not "frozen."

## 4. Which commands are read-only

`status`, `check`, `write-set-check`, every `validate <kind>`, every
`gate <gate-id>`, and `doctor`/`version` (once implemented). None of
these write to the workspace under any circumstances. This is enforced
today by construction (none of the corresponding `cmd_*` functions call
any write path) and is schema-enforced going forward for `check` via
`consolidated-check.schema.json`'s required `mutated_workspace: const
false` field — which also means `check` may never internally invoke
`extract-c-static`/`check-bridges`/`render-witness` (docs/cli-contract.md
§7), since those write; `check` can only *recommend* running one of them
via `next_action`, never run it itself.

`report <report-id>` is **not** in this read-only list — see §5. Three of
its four report-ids write a generated projection file; only
`report pilot-cluster` is genuinely read-only.

`record-assurance` (#87) is **not** in this read-only list either — it
writes the one artifact `gate g14` reads, a work package's assurance
report at its manifest's own `report.emit` path (see §5).

`authorize-write` (#114) is **not** in this read-only list either — it
appends one capability record to `ci/results/protected-writes.jsonl` and
nothing else. It is the only command that can make a `write-set-check`
protected-write finding non-blocking, so it is a human checkpoint (or an
identity the descriptor explicitly whitelists) by construction: the three
read-only surfaces stay read-only, and `write-set-check --issue N` is the
only consumer of what it writes.

## 5. Which operations may write, and under what authority

| operation | writes | authority |
|---|---|---|
| `draft <kind>` | a new draft artifact | none required — Stage 0/3, LLM-proposed, explicitly non-normative until reviewed |
| `approve <op>` | promotes a draft (attaches `review`) | `--reviewer` (required, human identity). Every normative artifact type goes through it, concept specs included since #105 -- a concept spec's `constraints[].id` is what a boundary contract's `callee_guarantees` is verified against (G2+), its `queries[]` what a witness spec's G2 resolves against, and its `queries[].witness_required` the declared set G18 measures, so promoting one without a human would mean an unreviewed vocabulary decides which checks run |
| `init` (#58) | installs owned files into a target repo | operator running the command; must not silently overwrite user-owned content |
| `migrate` (#59) | rewrites legacy references to registry form | operator running the command, staged (phase A-D per #59), never rewrites a reviewed artifact without its own human checkpoint |
| `report feature-ledger` (#88) | `ci/results/feature_ledger.json` | none required — a generated projection, safe to regenerate over a previous ledger; refuses (exit 1, file untouched) to overwrite a schema-valid assurance report already at the destination — run `record-assurance` after `report feature-ledger`, or point the manifest's `report.emit` at a free `ci/results/*.json` path; see the `record-assurance` row for the mirror rule |
| `report contact-sheet` | `docs/witnesses/_contact_sheet.svg` | none required — same as above |
| `report gold-set-measurement` | `ci/results/gold_set/` (default; `--no-write` suppresses it) | none required — same as above |
| `accept-promotion` (#45, #82) | a promotion receipt under `specs/_promotions/` | `--reviewer` (required, human identity); the accepted artifact set and the policy document's own `Policy version:` line are the authority inputs, each accepted artifact's own `review` block must be provable to the sanctioned approve path (a matching entry in `ci/results/review_log.jsonl`) **and** every artifact in the set must carry a `ratified` human ruling over its current content in `ci/results/human_rulings.jsonl` -- a block that merely exists in the file, or an approval an unattended agent recorded, is not authority, and neither is silence (#82) |
| `record-ruling` (#82) | one append-only entry in `ci/results/human_rulings.jsonl` | `--reviewer` (required, human identity) plus an explicit `--verdict ratified\|rejected` over an explicit `--artifact` set; this is the human-decision event Stage 4.5 is gated on, so it is a human checkpoint exactly like `approve` -- an agent must not run it either |
| `accept-policy` (#78, #113) | the ownership manifest's reviewed `base_hash` for the normative reliance-policy document, and -- with `--version` -- that document's single `Policy version:` marker line | `--reviewer` (required, human identity); the only path through which a normative user-owned document's recorded base moves — `init`/`migrate` never re-base it (#78). `--version <name>@<major>.<minor>` writes to the document itself, and only its one marker line: the sanctioned way to reach an accepted state from the template `init` installs, whose placeholder marker yields no `policy_version` and therefore blocks every promotion. A human checkpoint for that reason, and for `accept-policy`'s own: an agent must ask the human to run it (#113) |
| `promote-evidence` (#79) | renames a staged evidence draft (`evidence/<id>.json.draft`) to its target, plus an audit entry in `ci/results/evidence_promotions.jsonl` | none required — evidence is non-normative (no `review` block, not a human checkpoint per plan.md §7.2), so promotion is mechanical (G1a/G1b re-validate + atomic rename); the human checkpoint remains on evidence-conflict-resolution, not evidence itself |
| `record-assurance` (#87) | a work package's assurance report at its manifest's own `report.emit` path (the achieved side `gate g14` reads) | none required — every record field is derived from the verifier's own proof certificate and the manifest's provenance, never from a caller's word for what was proved (docs/achieved-assurance-schema.json: a hand-authored achieved record would be indistinguishable from fabricating a verification result), so there is no `review` block to checkpoint and no human identity to record. The only caller declaration — which certificate is about which obligation — is checked (the obligation must be one this manifest provides; the certificate must sit under a directory named for that obligation's concept; it must not be older than the Coma program it certifies), and a certificate with a stuck subgoal records nothing at all. A feature ledger already occupying the path is replaced with a warning naming the collision; content that is neither artifact is refused rather than destroyed |
| `authorize-write` (#114) | one append-only capability record in `ci/results/protected-writes.jsonl`, authorizing a single write into a declared `protected_roots` path — the only thing besides a pipeline artifact that can make a `write-set-check` protected-write finding non-blocking | `--issuer NAME` (required, human identity) plus the record's own binding: `--issue N`, `--path P`, `--op write\|delete`, `--ttl` (a grant never outlives `issued_at + ttl`), `--one-shot`. In the `human` lane (the default) it is a human checkpoint exactly like `approve`/`accept-policy`/`record-ruling`, and an agent must ask the human to run it — #103's enforcement left a protected write that is genuinely *sanctioned* with only two routes, both worse than the defect: widen `allowed_roots`, which deletes the boundary, or hand-edit a file outside every tool, which the audit trail cannot tell apart from an intrusion. In the `supervisor` lane the identity must be declared in the descriptor's `write_set.authorized_supervisors` whitelist, and `write-set-check` re-verifies that on every consumption, so withdrawing the identity withdraws the grant on the next run. Nothing else is authority: no prose field is ever read when deciding whether a grant authorizes a write, and a grant cannot widen `allowed_roots`, authorize a different operation, outlive its TTL, or cover a different issue or path. The ledger is an audit trail, not a signature — see §8's closing paragraph for what that forecloses and what it does not |

`report pilot-cluster` writes nothing (stdout only) and belongs in §4's
read-only list in every respect except that it shares the `report` verb
with three operations that do write — `report` as a *verb* is therefore
mixed-authority by report-id, not uniformly read-only or uniformly
writing; docs/cli-contract.md §6 gives the per-report-id breakdown.

None of the three `report` writes is a reviewed/promoted artifact —
`is_generated_review_projection()` (§16.5/§16.6) already refuses to let
any of these three be listed in a promotion's own `artifact_manifest`.
`extract-c-static`/`check-bridges`/`render-witness`
(docs/cli-contract.md §7) also write — `ci/results/c_static/`,
`ci/results/bridge_checks/`, a witness's declared `output.path`
respectively — under the same no-authority-required, always-regenerable
discipline as the `report` writes above; they are omitted from this
table only because they are not top-level verbs (§7), not because they
don't write.

## 6. `status`/`check` cannot approve or promote

Neither command has, or will have, any code path that attaches a `review`
block, writes to a `_promotions/` receipt, or otherwise changes an
artifact's lifecycle. `consolidated-check.schema.json`'s `next_action` can
only ever *name* a command to run next (`kind: automated-command`'s
`command` string) — the schema has no field through which `check` itself
claims to have performed that action. Producing a recommendation is not
performing it.

## 7. LLM recommendations are advisory

Any finding or next-action entry whose `authority` is `llm-advisory`
(`consolidated-check.schema.json`) carries no blocking weight on its own —
it cannot be the sole reason `result.exit_code` is non-zero. Only
`mechanized-gate` and `human-decision-pending` authorities can drive a
blocking exit code; `llm-advisory` findings are informational until a
mechanized gate or a human confirms them. This mirrors plan.md's own
Stage 0/3 discipline: LLM output is a draft proposal, never itself a
passing check.

## 8. Human checkpoints remain human

`approve <op>` requires `--reviewer` on every path (plan.md §7.2); no
future `check`/`status` output can substitute for it. A `human_decision`
entry in `project-state.schema.json` moves from `pending` to `recorded`
only through a real human review event (which `approve`/`accept-promotion`
already record via `review`/`--reviewer`), never through a mechanized gate
run reclassifying it on its own.

A `review` block on its own is also not that event (#82): it is
self-asserted data, indistinguishable on disk from a block somebody
typed. `accept-promotion` therefore proves each accepted artifact's
block against `ci/results/review_log.jsonl` -- the append-only record
`approve` writes -- and refuses the promotion when no approval entry
matches, so crossing the Stage 4.5 checkpoint still requires a real
review event, not a plausible-looking field.

That still is not sufficient, and Stage 4.5 says so: an unattended
agent can run `approve` itself, leaving audit entries no provenance
check can tell apart from a human's, and artifacts with no `review`
block at all (evidence records promoted outside any tool path) are
invisible to the check by construction. So `accept-promotion` also
requires a recorded human ruling -- `record-ruling --reviewer <name>
--verdict ratified|rejected --artifact <path>...`, appended to
`ci/results/human_rulings.jsonl` -- covering every artifact of the
accepted set at its current content. Missing, `rejected`, stale (the
file changed since the ruling) or unreadable all refuse. Like `approve`,
`record-ruling` is a human checkpoint: a human ruling on a review block
written out-of-band, or on a set promoted by hand, is recorded there,
never inferred and never supplied by the agent.

The write-grant ledger (`authorize-write`, #114) is the same kind of trail
and carries the same residual, stated rather than glossed: an entry
*appended* by someone with write access to `ci/results/` is
indistinguishable from one `authorize-write` wrote — which is a boundary
breach in its own right and is exactly what the agent-facing rule forbids.
What the trail does *not* permit is an entry edited in place: `grant_id` is
a hash of the fields the grant's authority is of (issue, path, operation,
one-shot, issuer, issuer-kind, issued-at, TTL) and the consumer recomputes
it, so a record edited to authorize a different path, issue, operation or
time under a preserved id is rejected and reported. Deleting a whole line
cannot be detected by any append-only trail; what can be, and is, is that
`authorize-write` refuses to record a grant at all when the ledger itself
sits inside a declared protected root, so a workspace cannot reach a state
where every append to its own capability trail is a boundary breach. The
consumer holds the same half of that rule, because a writer-side refusal a
hand-appended line walks straight past is not a refusal: `write-set-check`
reports every record read out of such a ledger as `ledger-protected` and
honors none of them. Without it, a grant naming the ledger's own path would
let a capability trail vouch for its own presence in a declared protected
root -- the same thing `write_set` has refused since #103, when a canonical
pipeline location was allowed to account for a file the descriptor protects.

## 9. Witnesses and differential agreement falsify, never prove

Per plan.md §16 (G18/G19/G20) and the codebase's own repeated discipline
(`witness_renderer.py`: "fail loudly, never substitute"; G19: regenerate
and compare `value_hash`, never `render_hash`): a witness or a differential
test that AGREES with an expectation is evidence the expectation was not
falsified in this instance, not a proof of universal correctness or
equivalence. `project-state.schema.json` keeps `generated_observations`
(witness summaries: determinism, degeneracy) structurally separate from
`obligations[].achieved_assurance` for exactly this reason — nothing in
either schema lets a witness observation stand in for an assurance
dimension.

## 10. Binary attestation is specified here, implemented under #57

`project-state.schema.json`'s `binary_identity` object (`verified`,
`content_hash`, `version`) is the shape #57's `ligature version --verify`
and `doctor` populate. **Implemented by #57.** A packaged zipapp reports
`verified: "true"` only after recomputing its own content hash and matching
the embedded build attestation, and the interpreter/platform contract;
`verified: "unknown"` remains the only honest value from a source checkout
(`python3 scripts/pipeline.py`, no build hash to check). The same build
identity is pinned by the installed descriptor's `gate_integrity` under the
`@adjudicator` entry; an unattested or mismatched adjudicator fails closed.
See `docs/packaging.md`, whose §4 states the boundary explicitly:
`verified: "true"` is a self-consistency and pin-persistence check, not a
signature against a root of trust outside the archive -- it catches a
binary swapped in after `ligature init`, not a compromised binary at the
first `init` itself.

## 11. Assumption-registry migration is specified under #59

Nothing in this task's schemas or CLI contract invents assumption-registry
resolution, dual-resolution of legacy `boundary_id`/`tracking_issue`/
`assumption_hash` composites, or the phase A-D migration sequence — all of
that is #59's own scope. Where `project-state.schema.json` or
`consolidated-check.schema.json` need to reference an assumption
(currently: nowhere directly — obligations reference `obligation_id`, not
assumption identity), #59 extends these schemas with its own minor version
bump rather than this task guessing its shape now.

**Implemented by #59.** `docs/assumption-registry-schema.json` (v1.0) and
`scripts/assumption_registry.py` add the registry, phase-B dual-resolution
with a non-blocking deprecation finding for the legacy composite, phase-C
`ligature migrate --assumptions` reference rewriting (generated work-package
manifests only; reviewed boundary content is never auto-rewritten), and the
phase-D version mechanism (`ASSUMPTION_REF_LEGACY_REMOVAL_VERSION`).
`status`/`check` themselves still need no assumption field: the new G21
gate runs in `validate` and does not change either output schema.

## 12. Ambiguities explicitly deferred

- Exact `required_assurance`/`achieved_assurance` dimension vocabulary
  (`project-state.schema.json`'s `assurance_dimension.dimension` is an
  open string, not a closed enum) — #56 owns closing it once the real
  computation exists to enumerate against.
- Whether `check`'s gate-discovery is purely descriptor-driven or also
  consults installed-manifest state — #56.
- The real shape of `--json`'s relationship to the human-readable form
  (same command, `--json` flag, vs. separate rendering path) — #56.
- ~~Packaged-resource discovery replacing `Path(__file__)` (named directly
  in `docs/implementation-inventory.json`'s `vendored_runtime_assets`
  entry for `vendor/concept-to-code/schemas/spec.schema.json`)~~ —
  resolved by #57: every such lookup now goes through
  `scripts/resources.py`, which uses `importlib.resources` from a zipapp and
  the repository root from a source checkout. See `docs/packaging.md`.
- The ownership-manifest three-way-comparison mechanics
  (`installation_manifest` here only models the STATE such a manifest
  produces, not how it's computed or stored) — #58.
