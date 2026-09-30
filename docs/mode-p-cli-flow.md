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
| Stage 0 | evidence intake: claim + origin + semantic_disposition + lifecycle | `draft evidence-intake` → `promote-evidence` | LLM-side → mechanical |
| Stage 1/2 | concepts (L1), intra-contracts (L2) | `draft concept-to-code` (concept spec + constraint-id production) | LLM-side |
| Stage 3 | interactions (I) + reliance (O) + boundary contracts + bridge specs + witness specs + exemptions + protocol-debt records + conflict resolutions — independent candidate sources | `draft boundary-drafting`/`interaction-drafting`/`bridge-drafting`/`witness-drafting`/`exemption-drafting`/`protocol-debt-drafting`/`conflict-resolution-drafting` → `approve draft`/`pair`/`exemption-pair` | LLM-side → human |
| Stage 4 | adjudication: G1a/G1b/G2/G2+/R2/G4/G5/G11/G15, **plus G18** (witness coverage) | `validate boundary\|interaction\|exemption\|protocol-debt\|bridge\|evidence\|conflict-resolution\|witness`, `gate g18` | read-only |
| Stage 4.5 | promotion (detached receipt, explicit `artifact_manifest`); **G20** (witness degeneracy, warn); generated projections | `approve promotion`, `validate promotion`, `gate g20`, `report feature-ledger`, `report contact-sheet` | deterministic+human / read-only / projection |
| Stage 5 | emission (`emit_stubs.py`, G8) — not yet implemented in this codebase as of this writing | — | — |
| Stage 6 | attach gate (attachment dimension only) — not yet implemented | — | — |
| Stage 7 | work-package manifest, read-only, hash-pinned incl. gate code | `validate work-package` | read-only |
| — (orchestration) | one issue → one owner → one worktree → one PR; real implementation happens here (`crates/*/src/`, the actual port) | *(not a `ligature` command)* | — |
| Stage 8A | implementation verification: C_static extraction, callsite scope, bridge checks, R1/G16, **G19** (witness determinism, re-dispatches `witness_backend` fresh and compares against the witness spec's stored `determinism.value_hash` — it does not parse `render-witness`'s output file, though `render-witness` is what puts a value there to compare against in the first place) | `extract-c-static`, `validate callsites`, `check-bridges`, `gate g9`, `gate r1-g16`, `render-witness`, `gate g19` | read-only (internal ops write, §3) |
| Stage 8B | acceptance (non-normative): Mode P = differential testing, Mode R = G10R | *(project-specific tooling, not a `ligature` command — plan.md's own G10 is not implemented anywhere in this codebase; nothing in `gate <gate-id>` reads `ci/results/differential_ledger.json` or any equivalent)* | — |
| Stage 8C | release closure: **G14** `satisfies()` over the transitive closure + CG6 well-foundedness discharge, reading a pre-authored closure profile (`specs/_closure/<cluster>.json` — G14 refuses if the directory is absent; it does not create the file) against work-package manifests, callsite reports, and bridge data → confirms `closure_kind` or names a degradation record. The ACHIEVED side G14 evaluates against is produced by **`record-assurance`** (#87) from the verifier's own proof certificates — nothing else can write it | `validate closure`, `record-assurance`, `gate g14` | read-only (`record-assurance` writes the report, §3) |
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
| `init --mode port` | Stage P0 (first run); rerun with a **different** binary now reports an `adjudicator pin mismatch` conflict instead of silently re-pinning — use `migrate --upgrade`/`--force` to re-pin deliberately (#65, fixed) | none | operator | managed files, `.codex/skills/`, `.ligature/`, `scripts/` (the two witness-renderer scripts G13 hash-pins — #84), **and** `project-descriptor.json` + `docs/reliance-policy.md` (written once, then marked user-owned) |
| `doctor` | any state, including before `init` | none — reports `installation: not-initialized` | read-only | — |
| `version [--verify]` | any state, including before `init` (inspects the running binary, not the workspace) | none | read-only | — |
| `status [--json]` | any state, including before `init` — reports `descriptor.state: absent` | none | read-only | — |
| `check [--json]` | any state, including before `init` — reports exit 2, `conditions: [invalid_input]`, `next_action: null` | none | read-only | — |
| `write-set-check [--json]` | any state with a schema-valid descriptor (before `init`: `unknown`, exit 2) | a `project-descriptor.json` with a `write_set` | read-only | — |
| `draft <kind>` | Stage 0/3 | a valid `<crate>/specs/` layout per the descriptor | LLM-side | writes `<target>.draft` only |
| `approve draft \| pair \| exemption-pair` | Stage 3, one draft must exist (evidence excluded -- it carries no `review` block) | a staged `.draft` file | human checkpoint | promotes a draft to its target path |
| `promote-evidence` | Stage 0, one evidence draft must exist | a staged `evidence/<id>.json.draft` | mechanical (no `--reviewer`; evidence is non-normative) | renames the draft to its target + an audit entry (chainlink #79) |
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
| `record-assurance <work-package> --proof <obligation>=<path>` | Stage 8C, once a verifier run has left proof certificates (e.g. `cargo creusot`'s `verif/**/proof.json`) | a schema-valid work-package manifest **+** a why3find proof certificate per obligation it is asked to record | mechanical (every field derived from evidence + manifest; no `--reviewer`) | the manifest's own `report.emit` path — an assurance report (chainlink #87); refuses instead of writing when a certificate has a stuck subgoal, is older than its Coma program, or cannot be shown to be about the obligation |
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

### Declaring the intended closure_kind (chainlink #73)

The per-pilot bootstrap step is: `init --mode port`, then fill in
`project-descriptor.json` — set `verifier_policy.default` **and the
intended `closure_kind`** — before any code exists. `verifier_policy.default`
was expressible; the intended `closure_kind` was not, in any shape: the
date-creusot pilot's probe matrix (top-level `closure_kind`,
`verifier_policy.closure_kind`, a top-level `closure` object,
`verifier_policy.closure_kinds`) was rejected by the schema 1.0 on every
row, and `init` exposed no switch for it either. The pilot could only
record the intent outside the tool (its `analysis/ligature/closure-intent.json`),
which the tool cannot read back at G14 time.

The descriptor schema now has the field the pilot was looking for: a
top-level, optional `closure_kind` (`deductive` | `bounded` | `partial`,
plan.md §4's vocabulary — the third value added by chainlink #85, see
below). It records **intent only** — each cluster's closure profile
(`specs/_closure/<cluster>.json`) remains the authoritative per-cluster
declaration at Stage 8C, and gate g14 recomputes `closure_kind` from the
evidence actually present, so a declared intent can never launder a
bounded cluster into a deductive one. The field is deliberately absent
from the init template: a project that has not chosen an intent yet must
not be silently defaulted to `deductive`. `status --json` reads the
declaration back as `descriptor.closure_kind` (null when absent, invalid,
or undeclared).

### Declaring a partially-verified closure (chainlink #85)

The same vocabulary gap existed one layer down, at the declaration that
is actually authoritative: `docs/closure-profile-schema.json`'s
`closure_kind` accepted only `deductive` and `bounded`, so a pilot that
cannot achieve full deductive closure had no way to state its actual
state — `validate-closure` rejected `partial` (and `mixed`, `degraded`)
at G1a with `'…' is not one of ['deductive', 'bounded']`, leaving only
rounding the claim up to `deductive` (over-claiming) or down to
`bounded` (claiming a uniform guarantee over a closure that does not
deliver one uniformly).

The enum gains exactly one value, `partial` (plan.md §4): partially
verified — part of the closure carries the guarantee its evidence
supports and part does not. `deductive` and `bounded` are uniform claims
over the whole closure; `partial` claims strictly less than either, so
gate g14's evidence-kind check never refutes it (that check polices
`deductive` over a Kani result, and `bounded` under an all-deductive
closure), while everything `partial` describes is still policed by the
findings that own it: a missing achieved record blocks whatever the kind
says, and a failing closure condition still needs its own degradation
record. The value is accepted everywhere the vocabulary appears — the
descriptor's intent field, `status --json`'s descriptor and per-cluster
echoes (`schemas/project-state.schema.json`), and the feature ledger.
`mixed` and `degraded` are deliberately not kinds: `degraded` already
names g14's own outcome for a cluster released under a degradation
record (orthogonal to whichever kind it declares), and the
evidence-composition fact `mixed` would name is already carried by
`conditions.single_verifier_system` and the closure's own evidence
kinds.

### An invalid descriptor names its offender (chainlink #73)

A descriptor the schema rejects used to be reported as `conditions:
[invalid_input]` next to an **empty findings list**, a `null`
`next_action`, and — in `status` — a gate-integrity detail of "no project
descriptor present" while `descriptor.path` named the file that existed.
A descriptor typo was indistinguishable from a missing descriptor, and the
only recovery path was trial and error against an undocumented schema.

`check` now emits a `P0` finding (severity `high`, authority
`mechanized-gate`) that names the offending property with its JSON path
and the permitted alternatives — `$.port_source: unexpected property
'commit'; permitted: language, oracle_build_command, repository` for the
pilot's `port_source.commit` probe — and `check next` recommends fixing
`project-descriptor.json` as a `human-decision` action (no CLI command
can repair a descriptor). `status` carries the same diagnostic as an open
finding, and its gate-integrity detail reads "project descriptor is
present but invalid: …" so the two states stay distinguishable. The same
diagnostic is in the `ProjectDescriptorError` message every
`load_project_descriptor` caller already prints.

### `verifier_policy`: the open object, the undisclosed enum, the unexpressible second verifier (chainlink #76)

`verifier_policy` was the only object in the descriptor that permitted
unknown keys — every other object is `additionalProperties: false`. The
rule was "arbitrary keys, every value a member of an undocumented
three-value enum", which produced three defects in the one field a
`creusot`/`verus`/`kani` pilot is named after:

1. **Silent acceptance.** A misspelled key (`defualt: "kani"`) validated,
   `check` exited 0 with `findings: []`, and the typo was
   indistinguishable from a real key.
2. **An undiscoverable, unreported value domain.** Exactly `creusot` |
   `verus` | `kani` were accepted (case-sensitive, exact, no composition,
   no whitespace tolerance) — and no command reported the effective
   policy: `status --json` and `check --json` contained zero occurrences
   of the string "verifier" in either the valid or the invalid case.
3. **An unexpressible — but fake-writable — second verifier.** Every
   composition shape (`verifiers: [...]`, `additional: [...]`,
   `per_kind: {...}`, `default: "verus+kani"`) was rejected, while an
   arbitrary extra key holding a second enum member validated and was
   then silently discarded by every reader.

The fix documents rather than closes the object, because per-cluster
overrides are load-bearing: gate g9 resolves `policy.get(cluster,
policy["default"])`, and the shipped greenfield example itself declares
`"verifier_policy": { "default": "creusot", "scheduling": "kani" }`. The
schema's `verifier_policy` description now states the key set —
`default` (required), any other key naming a cluster and holding that
cluster's verifier, and `supporting` — and the `$defs/verifier`
description names the three permitted values and the exact-match rule,
so the value domain is discoverable from the schema itself rather than
from a rejection message. `supporting` (an array of enum members) is the
declared shape for multi-verifier composition — the P3 crypto-mixed
pilot's `verus` + `kani` in one workspace, and the epic's "Kani
supporting evidence" probe, get a real descriptor-level home instead of
an accident of `additionalProperties`. The Defect-1 hazard is mitigated
by observability: `status --json` echoes the effective policy as
`descriptor.verifier_policy` (`default`, `clusters`, `supporting` — the
shape gate g9 consumes), so a typo shows up under `clusters` as an
override for a cluster literally named `defualt`, visibly not the default
the user meant. `init` also ships `schemas/project-descriptor.schema.json`
into the project root itself (managed, attested), not only into
`.ligature/schemas/`, so the schema governing the user-owned descriptor
is present on disk where a black-box probe can find it.

### `write_set`: the enforcement boundary that nothing enforced (chainlink #77)

`project-descriptor.json`'s `write_set` (`allowed_roots` /
`protected_roots`) is the only thing the descriptor says keeps an
implementation inside its crate's `src/` and `tests/` and away from the
pinned upstream checkout, `ci/manifest/**` and the specs. In v1.0 it was
consumed by nothing and mentioned in no file `init` generates — not the
hash-pinned skill authority region, not any of the three prompts — and the
descriptor accepted every shape of the object, including
`allowed_roots: ["**"]` and `protected_roots: []`, rejecting only the
object's total removal. The date-creusot pilot's repro (`rust/rogue/evil.rs`,
`rust/rogue/notes.txt`, `docs/evil.md` next to a filled descriptor) was
reported by every command as nothing: `check --json` exited 0 with
`findings: []`, `status --json` carried no write-set state, and `doctor`'s
command list had no write-set verb.

The fix makes the boundary machine-checked rather than merely declared,
in the report's option (a) shape. `scripts/write_set.py` evaluates the
write set against the workspace's actual files: a file is accounted for by
an `allowed_roots` pattern, a `protected_roots` pattern, the ownership
manifest (`ci/manifest/installation.json`), a declared crate's `specs/`
tree, a canonical pipeline location (`ci/`, `evidence/`, workspace-level
`specs/_<kind>/`, `docs/witnesses/` — places the pipeline writes, not the
agent), or the pinned upstream checkout (`port_source.repository` when it
names a directory inside the workspace). Everything else is an out-of-set
write. `ligature write-set-check [--json]` reports the violations and
exits 0/1/2 per the exit-code contract; `status --json` carries the
verdict as `write_set.state` (`clean` / `violations` / `unknown`) — the
`status.gate_integrity`-style state, so #74's missing-check-gate problem
does not repeat; `check --json` carries one high-severity `write-set`
finding per violation, so `check` exits 1 on the pilot's repro. The
vacuous declaration shapes the schema accepts (`allowed_roots: ["**"]`,
`protected_roots: []`) are flagged as violations of the write set's own
purpose rather than rejected by the schema — descriptor schemas are a
stable public contract (§1 of trust-and-compatibility-boundaries.md), and
the codebase's own precedent for exactly this situation (#67's Stage-P0
placeholder) is a conservative finding, not a schema rule. Files inside
`protected_roots` that nothing vouches for are reported separately as a
non-blocking protected-surface audit: the check cannot distinguish a
project's own protected files from an agent's intrusion into a protected
area and must not fail closed on the former. The same change closes the
descriptor-level `gate_integrity` path-escape gap (companion evidence on
#74): an entry resolving outside the workspace (absolute, or a `../`
traversal) is refused rather than hashed against someone else's file. The
installed skill's authority region gains a binding write-set rule and the
command table gains the verb; all three prompts gain a write-set note.

### The normative user-owned document that nothing drift-checked (chainlink #78)

`docs/reliance-policy.md` is user-owned — `init` installs the template and
never overwrites it — but it is also a **normative input**: the standing
governance document Stage 3 boundary drafting applies its resolution rule
from, and whose `Policy version: <name>@<major>.<minor>` line
`accept-promotion` reads to compute a promotion receipt's
`policy_version`. The ownership manifest recorded `base_hash` and
`expected_hash` for it, implying a guarantee nothing made: neither value
was ever compared, so the document could be rewritten — including to
contradict its own fixed resolution table — while `doctor`, `check` and
`status` all reported healthy. The only detectable state was a deleted
file, and only `doctor` noticed that, as one `MISSING` line. The
descriptor's `compatibility_policy.reliance_policy_path` pointer was
equally inert: read by nothing, so a workspace could declare a nonexistent
policy document (or one outside the project root) and still validate as
`present-valid` with `check` exit 0, and nothing checked that
`accept-promotion --policy-path` named the file the descriptor declares.

The fix compares what the manifest already records. A normative
user-owned document's on-disk content is checked against the manifest's
reviewed `base_hash`: a mismatch (or a missing file) makes `doctor`
report `DRIFTED`/`MISSING` and exit 1, makes `check` emit a high-severity
`policy-drift` finding and exit 1, and drops `status --json`'s
`installation_manifest.state` from `current` to `drifted` with the drift
carried as an open finding. The recorded base moves only through the
explicit accept path `ligature accept-policy --reviewer <name>` —
`init`/`migrate` never re-base a normative user-owned file to whatever is
on disk, so a reviewed edit is recorded rather than silent — and the
document must carry exactly one `Policy version:` marker line, the same
convention `accept-promotion` reads, so the recorded hash always
corresponds to a policy that can yield a policy_version. The descriptor's
`reliance_policy_path` is now validated (`check` fails closed when it
names a nonexistent file or a path outside the project root) and consumed
as the default for both accept commands, with a disagreeing explicit
`--policy-path` refused. A legacy manifest that records no `base_hash`
adopts the on-disk content once, so a pre-#78 workspace becomes
drift-checkable rather than permanently unverifiable.

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
- **#72** (closed) — `status --json` applied the same misreading #66 fixed
  in `gate g14`: `project_state._discover_artifact_files` globbed every
  `ci/manifest/*.json` as a work-package manifest, so `ligature init`'s own
  `ci/manifest/installation.json` surfaced as a `work-package` artifact with
  lifecycle `invalid`, contradicting the same document's
  `installation_manifest.state: current` (and `doctor`'s
  `installation: current`) after every successful `init --mode port`.
  `status` now reuses `gate_g14.looks_like_work_package_manifest` so the two
  walks cannot drift apart on what a work-package manifest is. Unparseable
  files stay in the discovery set and are still reported honestly as invalid
  work-packages; a schema-invalid work-package manifest is still reported as
  `work-package`/`invalid`. Covered by regression tests in
  `tests/test_project_state.py` and an end-to-end assertion in
  `tests/test_zipapp_out_of_checkout.py`.
- **#73** (closed) — the project-descriptor schema 1.0 had no
  `closure_kind` field, so the intended closure_kind could not be declared
  in the tool's own input before any code exists (the date-creusot pilot's
  four probe shapes were all rejected, and `init` had no switch), and an
  invalid descriptor was rejected with no actionable diagnostic: `check`
  reported `conditions: [invalid_input]` next to an empty findings list
  and a null `next_action`, and `status`'s gate integrity claimed "no
  project descriptor present" while the descriptor file sat right there.
  The descriptor schema gains an optional top-level `closure_kind`
  (`deductive` | `bounded` — the vocabulary #85 later extended with
  `partial`) recording intent only — per-cluster closure
  profiles remain authoritative at Stage 8C — which `status --json` reads
  back as `descriptor.closure_kind`. An invalid descriptor now produces a
  `P0` finding naming the offending property with its JSON path and the
  permitted alternatives, a `human-decision` `next_action` recommending
  the fix, and a gate-integrity detail of "project descriptor is present
  but invalid: …". Covered by regression tests in
  `tests/test_project_state.py`, `tests/test_project_descriptor_schema.py`,
  and `tests/test_pipeline.py`, plus end-to-end assertions in
  `tests/test_zipapp_out_of_checkout.py`. See §4.
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
- **#70** (closed) — concept-spec schema conflict across tools. G2+'s
  `constraints[].id` and `gate g18`'s `queries[].witness_required` are
  proposed concept-to-code extensions, already decided
  (`docs/concept-to-code-modifications.md` gaps #5/#6, chainlink #33/#40)
  but not yet applied upstream, while `report pilot-cluster` validated
  against the unmodified vendored schema, which rejected both fields —
  no single concept spec could satisfy all three tools at Stage 3/4.
  `select_pilot_cluster.py` now validates against a copy of the live
  vendored schema extended in memory with both already-decided fields
  (`load_extended_concept_spec_schema()`), each pulled from its own
  regression-tested proposed-patch artifact so the local copy can't
  drift from what's actually proposed. `constraint.id` is accepted as
  optional there, deliberately diverging from #40's own "required"
  decision, since requiring it today would reject every spec that
  hasn't done the backfill #40 describes — a regression, not the fix
  this asked for. `vendor/concept-to-code` remains untouched either
  way. Originally documented as finding F3 in `docs/limitations.md`.
- **Not a gap: `select-pilot-cluster`'s rubric correctly excludes any
  bounded-only (Kani) cluster.** `plan.md` §14 states this explicitly —
  "deductive-closure value... Must be **> 0** to be eligible at all...
  It is 0 whenever... its sole verifier is `kani`" — this is the
  original, deliberate spec for the pilot-selection rubric (chainlink
  #4/#50), not an implementation artifact. A real, honestly-closed
  Stage 8C cluster with only bounded (Kani) closure — like the #52
  pilot's own `semver-core` — is genuinely, correctly `eligible: False`
  for `report pilot-cluster`: the pilot-selection role was always
  designed to pick the pipeline's *strongest* demonstrable claim
  (deductive closure) first, not any honest closure. `#50` stays
  correctly blocked on this specifically — it needs a real project with
  a genuinely deductive-closure (Creusot/Verus) cluster, separate,
  unstarted work, not a rubric fix. Originally documented as finding F4
  in `docs/limitations.md`; no chainlink issue filed, since nothing
  here is a defect to track.

Eight items above (seven closed, one resolved as "working as designed");
none of the open ones blocks a Mode P project from *genuinely* reaching
Stage 8C closure — they're either fixed, or (F4) correctly-behaving code
against a real, separate scope gap (`#50` needing a deductive-closure
project) that isn't this rubric's own fault. They're listed here
because each is a concrete instance of the question this document exists
to make routine:
*does this command's behavior match where the state machine says it
should sit* — and each one is a case where it didn't, quietly, until
someone actually ran the sequence for real.
