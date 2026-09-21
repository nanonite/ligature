# Mode P (porting project) CLI flow — intended state machine

This document exists so that future work on error handling and misuse
cases can be scoped by a single question: **does this command respect its
position in the flow below?** It is not a new contract — `docs/cli-contract.md`
remains the authority on grammar and per-command semantics,
`docs/trust-and-compatibility-boundaries.md` on read/write authority, and
`plan.md` §6 on the underlying stage model (Stage 0 through Stage 8C).
This document maps CLI commands directly onto **plan.md's own stage
numbers** — it does not invent a parallel numbering. `ligature init --mode
port` is the entry point; the worked example throughout is
`<path-to-neargye-workspace>` (chainlink #52), whose real,
on-disk layout is used below — not a hypothetical.

Every stage tag below is taken verbatim from the pipeline's own source
(`cmd_gate_g18`/`cmd_gate_g20`/`cmd_gate_g19`'s docstrings,
`scripts/gate_g14.py`, `cmd_doctor`'s capability text, `docs/cli-contract.md`
§2/§3/§6/§7), not inferred. Where a command's actual stage differs from
where it's invocable in practice, both are stated separately rather than
picking one.

Everything here is Mode P (`mode: port` — a foreign-language source port
validated by differential testing, plan.md §1.1, §11). Mode R
(`mode: greenfield`) shares the same CLI and most of the same stages;
where it diverges (Stage 8B is `G10R` instead of differential testing, no
`third_party`/oracle artifacts) is noted but not the focus here.

## 1. Three authority regimes, not one

Every command sits in exactly one of three regimes (plan.md §6.1,
`docs/trust-and-compatibility-boundaries.md` §4–§7). Misuse analysis
should ask, for each command, which regime it's in — a command that
behaves as if it were in a different regime than documented is the shape
almost every gap found so far (§6) actually took:

- **Deterministic, read-only.** `status`, `check`, every `validate <kind>`,
  every `gate <gate-id>`, `doctor`, `version`. Never write, by construction
  — `check`'s own schema requires `mutated_workspace: const false`. No LLM
  ever runs here. A finding from this regime is `mechanized-gate` authority
  and can legitimately block.
- **LLM-side, non-normative.** `draft <kind>` only. Produces a `.draft`
  file, never a target path directly, never authoritative until reviewed.
  Output from this regime is `llm-advisory` authority and can **never** be
  the sole reason an exit code is non-zero (`trust-and-compatibility-
  boundaries.md` §7).
- **Explicit authority, three different kinds — do not collapse them.**
  `approve <op>` requires `--reviewer` (a **named human**, logged to
  `ci/results/review_log.jsonl`) — this is a human checkpoint, including
  `approve promotion`, which is mechanically deterministic *and* requires
  `--reviewer`; "deterministic" and "requires an explicit reviewer" are not
  mutually exclusive, and describing `approve promotion` as merely "no
  LLM" undersells that it is also a human checkpoint. `init` and `migrate`
  require only **the operator running the command**, no named-reviewer
  audit trail — but see §5, since `init` and `migrate --force` differ in
  whether that operator authority is actually gated. The three writing
  `report` sub-verbs (`feature-ledger`, `contact-sheet`,
  `gold-set-measurement`) require **no authority at all** — they write
  generated projections, "always safe to regenerate/overwrite"
  (`trust-and-compatibility-boundaries.md` §5), never a reviewed artifact.

## 2. Stage-by-stage: what each CLI command does at each plan.md stage

This replaces a from-scratch numbering with a direct table against
plan.md's own stages. "invocable" means the command can be *run* in that
state without erroring on missing prerequisites; it does not mean running
it is meaningful yet (e.g. `gate g18` is invocable the moment any witness
spec exists, but reports little until interactions declaring
`witness_required` exist too).

| plan.md stage | what happens | CLI command(s) | regime |
|---|---|---|---|
| Stage P0 (bootstrap) | `ligature init --mode port` writes managed files + a **schema-valid but placeholder-content** descriptor (see §4) | `init` | operator |
| — (operator, off-CLI) | operator replaces the placeholder `port_source`, `verifier_policy`, `crates[]`, `review` fields with real project values; `check`/`status` report a `P0` finding while the descriptor still holds the shipped example values (`check next` recommends editing it), though `draft`/`validate` themselves still do not gate on it — see §4 | `check`, `status` | read-only |
| Stage 0 | evidence intake: claim + origin + semantic_disposition + lifecycle | `draft evidence-intake` → `approve draft` | LLM-side → human |
| Stage 1/2 | concepts (L1), intra-contracts (L2) | `draft` (concept templates, if configured) | LLM-side |
| Stage 3 | interactions (I) + reliance (O) + boundary contracts + bridge specs + witness specs + exemptions + protocol-debt records — independent candidate sources | `draft boundary-drafting`/`interaction-drafting` → `approve draft`/`pair`/`exemption-pair` | LLM-side → human |
| Stage 4 | adjudication: G1a/G1b/G2/G2+/R2/G4/G5/G11/G15, **plus G18** (witness coverage) | `validate boundary\|interaction\|exemption\|protocol-debt\|bridge\|evidence\|conflict-resolution\|witness`, `gate g18` | read-only |
| Stage 4.5 | promotion (detached receipt, explicit `artifact_manifest`); **G20** (witness degeneracy, warn); generated projections | `approve promotion`, `validate promotion`, `gate g20`, `report feature-ledger`, `report contact-sheet` | deterministic+human / read-only / projection |
| Stage 5 | emission (`emit_stubs.py`, G8) — not yet implemented in this codebase as of this writing | — | — |
| Stage 6 | attach gate (attachment dimension only) — not yet implemented | — | — |
| Stage 7 | work-package manifest, read-only, hash-pinned incl. gate code | `validate work-package` | read-only |
| — (orchestration) | one issue → one owner → one worktree → one PR; real implementation happens here (`crates/*/src/`, the actual port) | *(not a `ligature` command)* | — |
| Stage 8A | implementation verification: C_static extraction, callsite scope, bridge checks, R1/G16, **G19** (witness determinism, re-dispatches `witness_backend` fresh and compares against the witness spec's stored `determinism.value_hash` — it does not parse `render-witness`'s output file, though `render-witness` is what puts a value there to compare against in the first place) | `extract-c-static`, `validate callsites`, `check-bridges`, `gate g9`, `gate r1-g16`, `render-witness`, `gate g19` | read-only (internal ops write, §3) |
| Stage 8B | acceptance (non-normative): Mode P = differential testing, Mode R = G10R | *(project-specific tooling, not a `ligature` command — plan.md's own G10 is not implemented anywhere in this codebase; nothing in `gate <gate-id>` reads `ci/results/differential_ledger.json` or any equivalent)* | — |
| Stage 8C | release closure: **G14** `satisfies()` over the transitive closure + CG6 well-foundedness discharge, reading a pre-authored closure profile (`specs/_closure/<cluster>.json` — G14 refuses if the directory is absent; it does not create the file) against work-package manifests, callsite reports, and bridge data → confirms `closure_kind` or names a degradation record | `validate closure`, `gate g14` | read-only |
| (cross-cutting) | pilot-cluster ranking, gold-set measurement — not stage-bound, meaningful once concept/verifier/edge structure exists | `report pilot-cluster`, `validate gold-set`, `report gold-set-measurement` | read-only / projection |

Two corrections to a natural first reading of this table:

- **The closure profile is an input to Stage 8C, not its output.**
  `gate g14` reads `specs/_closure/<cluster>.json`'s declared `closure_kind`
  and checks it against the evidence it can actually compute
  (`check_closure_kind`); it does not write that file. Something upstream
  (today: hand-authored, or a future `draft closure`/`approve` path once
  closure profiles are wired into the checkpoint mechanism — not yet the
  case in this codebase) must produce it before Stage 8C can run at all.
- **`report feature-ledger`/`contact-sheet` are Stage 4.5, not Stage 8A.**
  They project over G18/G19/G20 findings, but the ledger/contact-sheet
  generation itself is a Stage 4.5 review artifact (plan.md §16.5's "review
  projections, never promotion inputs" — `cmd_doctor`'s own capability text
  tags both `generate-feature-ledger` and `generate-contact-sheet` "Stage
  4.5"). Producing them *usefully* benefits from Stage 8A data existing
  (G19's determinism result, in particular), but their own stage tag is
  4.5, and running them earlier than that data exists just yields a
  thinner report, not an error.

## 3. Command-by-command placement, with prerequisites

| command | earliest invocable state | prerequisite data | regime | mutates |
|---|---|---|---|---|
| `init --mode port` | Stage P0 (first run); rerun with a **different** binary now reports an `adjudicator pin mismatch` conflict instead of silently re-pinning — use `migrate --upgrade`/`--force` to re-pin deliberately (#65, fixed) | none | operator | managed files, `.codex/skills/`, `.ligature/`, **and** `project-descriptor.json` + `docs/reliance-policy.md` (written once, then marked user-owned) |
| `doctor` | any state, including before `init` | none — reports `installation: not-initialized` | read-only | — |
| `version [--verify]` | any state, including before `init` (inspects the running binary, not the workspace) | none | read-only | — |
| `status [--json]` | any state, including before `init` — reports `descriptor.state: absent` | none | read-only | — |
| `check [--json]` | any state, including before `init` — reports exit 2, `conditions: [invalid_input]`, `next_action: null` | none | read-only | — |
| `draft <kind>` | Stage 0/3 | a valid `<crate>/specs/` layout per the descriptor | LLM-side | writes `<target>.draft` only |
| `approve draft \| pair \| exemption-pair` | Stage 0/3, one draft must exist | a staged `.draft` file | human checkpoint | promotes a draft to its target path |
| `validate boundary\|interaction\|exemption\|protocol-debt\|bridge\|evidence\|conflict-resolution\|witness` | Stage 4 | the relevant `specs/_<kind>/` directory | read-only | — |
| `gate g18` | Stage 4 | witness specs + interactions declaring `witness_required` | read-only | — |
| `approve promotion` | Stage 4.5, post-`validate` | a clean validation pass | human checkpoint (`--reviewer` + `--policy-path` + `--artifact...`) | `specs/_promotions/<cluster>.json` |
| `validate promotion` | Stage 4.5 | a promotion receipt | read-only | — |
| `gate g20` | Stage 4.5 | witness specs with `value_distribution` declared | read-only | — |
| `report feature-ledger` | Stage 4.5 (richer once Stage 8A data exists) | G18/G19/G20 findings, whatever is available | projection, no authority | `ci/results/feature_ledger.json` |
| `report contact-sheet` | Stage 4.5 (same caveat) | same | projection, no authority | `docs/witnesses/_contact_sheet.svg` |
| `validate work-package` | Stage 7 | a work-package manifest | read-only | — |
| `extract-c-static` | Stage 8A | real crate source | internal op, writes | `ci/results/c_static/` |
| `validate callsites` | Stage 8A | C_static reports | read-only | — |
| `check-bridges` | Stage 8A | bridge specs + a configured verifier backend | internal op, writes | `ci/results/bridge_checks/` |
| `gate g9` | Stage 8A | bridge-check results | read-only | — |
| `gate r1-g16` | Stage 8A | C_static reports | read-only | — |
| `render-witness` | Stage 3 (first rendering) through Stage 8A (re-rendering for G19) | a witness spec + configured `witness_backend` | internal op, writes | a witness's declared `output.path`, `determinism.value_hash` |
| `gate g19` | Stage 8A | a witness spec with a stored `determinism.value_hash` | read-only | — |
| `validate closure` | Stage 8C | a closure profile | read-only | — |
| `gate g14` | Stage 8C | closure profile **+** work-package manifests **+** callsite reports **+** bridge data | read-only | — |
| `report pilot-cluster` | any state with authored clusters (Stage 3+) | concept/verifier/edge structure | read-only | stdout only |
| `validate gold-set` | any state with a gold set authored | a gold-set fixture | read-only | — |
| `report gold-set-measurement` | any state with a gold set + candidate predictions | same | projection, no authority | `ci/results/gold_set/` (unless `--no-write`) |
| `migrate [--upgrade\|--prune]` | any state, post-`init`, explicit operator recovery | none | operator, read-only unless a flag given | managed files |
| `migrate --force <path>` | any state, post-`init` | none | operator, flag-gated | the named managed file, **and** re-pins `gate_integrity` (calls the same `apply_plan()` as `--upgrade` — see §5) |
| `migrate --assumptions [--apply --reviewer <name>]` | Stage 7+ (once generated work-package manifests contain legacy composite `assumption_ref`s) | such a manifest | operator, `--apply` requires `--reviewer` | `ci/manifest/*.json` **only** — never a reviewed boundary contract |

## 4. The unenforced Stage P0 → "really described" transition

`init --mode port`'s descriptor isn't blank — it's a **copy of
`schemas/examples/project-descriptor.port.example.json`** with only
`project.name`, `crate_naming_convention`, `crates[]`, and `gate_integrity`
overwritten (`scripts/ligature_install.py:_render_descriptor`). Everything
else survives verbatim from the example fixture, confirmed on disk:

```
port_source.repository:       "<path-to-cpp-source-repository>"
verifier_policy.default:      "creusot"
review.reviewer:               "example-reviewer"
```

This is schema-valid — `mode: port` + a `port_source` object satisfies the
descriptor schema's own `if`/`then` — so before #67 nothing downstream
distinguished "the operator filled this in for real" from "the operator
left the generated example in place." A sequence run against a freshly
init'd, never-edited workspace proceeded silently until something much
later (the oracle build step, off-CLI) failed for an unrelated-looking
reason.

**Fixed (#67), as an explicit finding rather than a schema rule.** A
descriptor still holding the shipped example values is now reported by
`check`/`status` as a `P0` finding (severity `high`, authority
`human-decision-pending`): `check` exits `1`, and `check next` recommends
editing the descriptor instead of a downstream refresh. The comparison
lives in `scripts/ligature_install.py` (`descriptor_placeholder_fields`),
which owns the example fixtures, and is surfaced through
`scripts/project_state.py`'s project-state report — deliberately **not**
through the installation report's managed-file vocabulary, which
classifies product-owned files, not a user-owned file nobody has edited
yet.

The schema-level alternative was rejected: a "reject the literal example
value" rule is more brittle (a legitimate project could coincidentally
want a similar-looking path), and descriptor schemas are a stable public
contract (`docs/trust-and-compatibility-boundaries.md` §1) that such a
change would have to justify more carefully than a read-only finding does.
The finding is deliberately conservative about coincidence:
`review.reviewer` and (for `mode: port`) `port_source.repository` are
flagged on their own — the example's `"example-reviewer"` and example path
are unambiguous placeholders — but `verifier_policy.default: "creusot"` is
a value a real project can legitimately choose, so it is named only when
the descriptor is byte-identical to the example everywhere `init` does not
overwrite it. A real project that happens to pick `creusot` alongside a
real reviewer and repository is not flagged.

## 5. The adjudicator trust-pin sub-state-machine

Orthogonal to §2 — this tracks *which binary is trusted*, not *how far the
project's artifacts have progressed*. It resets to nothing before `init`
and should only move under explicit operator action:

```
UNPINNED  (pre-#57 manifest, or before init)
    │  init (first run)
    ▼
PINNED  ── doctor/status/check with the SAME binary ──▶ PINNED (stays)
    │  init rerun with the SAME binary ──▶ PINNED (stays; pin field no-op)
    │
    │  doctor/status/check with a DIFFERENT binary
    ▼
DRIFTED/MISMATCH  (reported, never silently repaired for a READ command —
    │              confirmed: doctor correctly reports "adjudicator pin:
    │              mismatch" without touching the manifest)
    │  init rerun with a DIFFERENT binary
    ▼
CONFLICT  (init reports "adjudicator pin mismatch", exits non-zero, and
    │      freezes the ENTIRE install, not just the pin: the recorded pin,
    │      installed_product_version/installed_schema_versions, and every
    │      managed-file "upgrade" the refused binary would have written all
    │      stay exactly as the trusted binary left them. A genuinely new
    │      ("create") file still installs; "unchanged"/"user-owned" are
    │      untouched as always — chainlink #65 + #68)
    │  migrate --upgrade | --force <path>   (explicit, operator-gated —
    │                                        both call apply_plan(), which
    │                                        also re-pins gate_integrity
    │                                        as a side effect of the flag
    │                                        the operator explicitly gave)
    ▼
PINNED  (re-pinned, deliberately, under an explicit flag)
```

Both columns of that state machine are now tested. On the read path, a
swapped-in binary is caught by `doctor` without touching the manifest:
`tests/test_zipapp_out_of_checkout.py`'s
`test_source_checkout_is_refused_after_a_packaged_init` reruns `doctor`
from an *unattested source checkout* against a workspace pinned to a
*packaged* build and asserts `"unattested"`. On the `init` path, the same
file's `AdjudicatorRepinAcceptanceTest` builds two genuinely different
packaged artifacts — a `git worktree` at a pre-#65 commit plus the current
tree — and asserts that first init pins normally, a same-binary rerun is a
no-op on the pin, a different-binary rerun exits non-zero with
`adjudicator pin mismatch` and leaves the pin unchanged, and
`migrate --upgrade` re-pins deliberately.

The gap #65 closed: `init` reruns used to bypass this state machine
entirely — `init_workspace()` always called `apply_plan()`, which
unconditionally rewrote `gate_hashes["@adjudicator"]` to whatever binary was
running it, with no comparison and no flag. A rerun under the **same**
binary that was already pinned was a genuine no-op on this field, and still
is; a rerun under a **different** content hash silently re-pinned, exiting
`0` with `installation: current` and no signal. Now `init_workspace()`
compares the running executable's identity against the recorded pin before
applying, reports the `CONFLICT` state above when they differ, and preserves
the existing pin; deliberate re-pinning goes through `migrate
--upgrade`/`--force`, the same explicit path managed-file drift already
uses (chainlink #65).

The gap #68 closed: #65 froze only the trust pin. On that same conflict path
`apply_plan()` still wrote `installed_product_version`/
`installed_schema_versions` from the currently running (refused) binary and
applied every `"upgrade"`-outcome managed file rendered from that binary's
registry — so the pin was preserved while the workspace's content and
version claims could silently move ahead of it, a mixed-provenance install
the manifest did not surface. Now the conflict path passes the *recorded*
versions through (mirroring how `adjudicator_record` is already passed) and
refuses to write any `"upgrade"`-outcome content, while still installing a
genuinely new `"create"` file. `migrate --upgrade` — not a bare `init`
rerun — is the one explicit action that applies the refused binary's content
and version and re-pins the adjudicator (chainlink #68).

## 6. Known gaps, seeded for future misuse-case review

Findings already surfaced by real use of this flow (the #52 pilot, its
independent review, and review of an earlier draft of this document),
kept here so error-case work starts from what's already known:

- **#65** (closed) — `init` rerun no longer silently re-pins
  `gate_integrity[@adjudicator]` to whatever binary invokes it. When the
  running executable's content hash differs from the recorded one,
  `init_workspace()` reports an `adjudicator pin mismatch` conflict, exits
  non-zero, and leaves the pin byte-for-byte untouched; first init and a
  same-binary rerun are unchanged. Deliberate re-pinning goes through the
  explicit `migrate --upgrade`/`--force` path, as with managed-file drift.
  Covered by `tests/test_ligature_install.py` (mocked identities) and
  `tests/test_zipapp_out_of_checkout.py`'s `AdjudicatorRepinAcceptanceTest`
  (two genuinely different packaged builds). See §5.
- **#68** (closed) — an adjudicator conflict freezes the whole install, not
  just the pin. `init_workspace()`'s conflict path passes the previously
  recorded `installed_product_version`/`installed_schema_versions` through
  instead of the refused binary's, and refuses to write any managed-file
  `"upgrade"`-outcome content rendered from that binary; a genuinely new
  `"create"` file still installs. `migrate --upgrade` remains the one
  explicit action that applies the new content/version and re-pins.
  Covered by `tests/test_ligature_install.py`'s
  `AdjudicatorConflictFreezesInstallTest` (mocked renderer and version,
  plus the `create`-still-installs boundary). The pre-#65 vs current
  builds constructed in `AdjudicatorRepinAcceptanceTest` do not differ in
  managed-file content (both render from unchanged templates), so no
  end-to-end case was added there. See §5.
- **#66** (closed) — `gate g14`'s `load_manifests` no longer globs every
  `ci/manifest/*.json` as a work-package manifest: it now discriminates on
  shape (`work_package`/`definition_of_done` top-level keys) before
  work-package schema validation, so `ligature init`'s own
  `ci/manifest/installation.json` is skipped rather than reported
  schema-invalid (severity `high`). A genuinely malformed work-package
  manifest still fails closed, including one that has lost `schema` — the
  key `installation.json` also lacks. Confirmed on the #52 pilot: `gate
  g14` now exits 0 on the closing `semver-core` cluster with
  `installation.json` in place. Originally documented as finding F1 in the
  pilot's `docs/limitations.md`.
- **#64** (closed) — release artifacts now honestly record
  `source_dirty`/`working_tree_diff_hash` rather than mislabeling a dirty
  build clean; relevant here because §5's PINNED state's *meaning*
  depends on `source_commit` being trustworthy provenance, which #64 made
  true — and #65 (closed) made the pin *mechanism* enforce that on the
  `init` path too, not only on read.
- **#67** (closed) — the Stage P0 descriptor-placeholder gap. `check`/
  `status` now report a `P0` finding (severity `high`, authority
  `human-decision-pending`) while the descriptor still holds the shipped
  example values — `review.reviewer`, and `port_source.repository` for
  `mode: port`; `verifier_policy.default` only as part of a fully untouched
  descriptor, so a coincidental `creusot` choice is never flagged alone.
  `check next` recommends editing the descriptor rather than a downstream
  refresh. A genuinely edited descriptor is not flagged. See §4.
- **#69** (closed) — Boundary G2+'s concept resolver did not skip
  `_`-prefixed directories, unlike the witness validator
  (`validate_witness.py`'s `part.startswith("_")` skip), so a witness spec
  and a boundary contract naming the same concept collided ambiguously at
  Stage 4 — **only when such a witness actually exists**; the #52 pilot
  avoided the collision by witnessing a different concept
  (`VersionOrder.compare` rather than `SemVer`), so this was a real but
  conditional gap, not one every Mode P project hits. G2+ now skips those
  directories via `project_descriptor.is_underscore_artifact_path` — the
  same predicate the witness/G18/pilot-cluster resolvers now share, so the
  convention cannot drift apart across validators again. A genuinely
  ambiguous pair of concept specs outside those directories still reports
  the ambiguity: the candidate set was narrowed, not the check disabled.
  Documented in `docs/limitations.md` (F2).
- **Concept-spec schema conflict across tools** — G2+'s
  `constraints[].id` and `gate g18`'s `queries[].witness_required` are
  proposed concept-to-code extensions not applied upstream, while
  `report pilot-cluster` validates against the unmodified vendored
  schema, which rejects both fields. No single concept spec can satisfy
  all three tools at Stage 3/4. This is a genuine cross-tool schema
  disagreement, not read-path noise and not a rubric-scope question —
  it means one of the three tools is always slightly wrong about a
  conforming spec. Documented in `docs/limitations.md` (F3).
- **`select-pilot-cluster`'s rubric excludes any bounded-only (Kani)
  cluster** — `deductive_closure_value > 0` requires a Creusot/Verus
  edge, so a real, honestly-closed Stage 8C cluster can still be
  `eligible: False` for `report pilot-cluster`. This is a scope question
  about what #50's rubric is meant to select for (does it intend to
  exclude bounded closures, or was that an oversight), not a code defect.
  Documented in `docs/limitations.md` (F4).

Eight items above (six closed); none of the open ones blocks a Mode P
project from *genuinely* reaching Stage 8C closure — the remaining
schema-conflict gap is read-path noise a human currently has to route
around, not incorrect promotions, and the remainder are authority or
scope questions rather than wrong gate results. They're listed here
because each is a concrete instance of the question this document exists
to make routine:
*does this command's behavior match where the state machine says it
should sit* — and each one is a case where it didn't, quietly, until
someone actually ran the sequence for real.
