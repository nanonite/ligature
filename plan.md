# Reliance-Graph Pipeline: Plan v5

## Claim block

- **Reliance architecture** — settled. S / I / C / O separation needs no further redesign.
- **Assurance semantics** — open design; the *mechanism* is closable with no tooling gap; **profile authorship is standing human governance**, versioned and owned like `reliance-policy.md`.
- **Bridge semantics** — closable **within** a verifier system (bounded for Kani, deductive for Creusot/Verus); **capability-gapped across** systems, ceiling `harness-tested`.
- **G14 SCC closure** — engineering **plus an explicit non-circularity / induction discharge**. The reachability is free; the cycle rule is not.
- **Closure** — a **per-cluster property with a kind** (`deductive` | `bounded`) under a declared profile. Degraded clusters carry a degradation record. **Never a global pipeline guarantee.**
- **Feature witnesses (§16)** — a deterministic ∃-witness layer for layer-2 pure queries: evidence, never assurance, never in `accepted_evidence_kinds`, **no effect on `closure_kind`**. Independent of the I/O/bridge/closure track; gated on the project descriptor only.

**The value does not depend on closure.** It comes from the manifest freezing the write set, forbidding spec-weakening, and routing every shortfall to a change request. That is realizable at **Prototype A + the work-package manifest**, before any bridge or closure work.

| Track | Ready? | Blocking |
|---|---|---|
| Prototype A — G2+ **role safety only** | **yes**, after §2 | filenames, G1a/G1b split, reliance-policy doc, A006 as expected migration failure |
| Work-package manifest (write-set freeze + change-request routing) | **yes**, after §10 | gate-implementation hashing, valid-YAML examples |
| I-schema | **close** | detached receipt with explicit artifact manifest, computed eligibility, assurance requirement located in I, realization/config scope, protocol classification, evidence lifecycle |
| Bridge implementation | **within-verifier only** | call-site fact semantics, typed bridge expression language |
| Cross-verifier bridges | **capability-gapped** | no soundness theorem exists; `harness-tested` ceiling |
| Release closure (G14) | **conceptually sound** | transitive closure + SCC well-foundedness discharge |
| Feature witnesses (§16) | **yes**, after project descriptor | renderer canonical-numeric-result build-out (§16.1) — confirmed not yet built |

---

## 0. Changes from v4

| v4 | Verdict | v5 |
|---|---|---|
| "No further foundational redesign required" | **Overstated** | Reliance architecture settled; **bridge and assurance semantics remain open design work**. |
| G2+ checks "declared assurance target" at Stage 4 | **No artifact to validate** (§1) | Boundary v1.0 has no assurance field; the manifest is generated at Stage 7, after promotion. **Assurance requirement moves into I.** Prototype A G2+ = **role safety only**. |
| `established_by: [Scheduler.C004]` | **Temporal error** (§2) | A caller *postcondition* holds after the caller returns; the call happens inside. Replaced with **call-site fact semantics**: available contract facts, required local facts, target expression. |
| `accepted_kinds: [bounded-model-check, bridge-checked]` | **Mixes categories** (§3) | Split into independent fields: **claim / evidence-method / result / scope / trust / support**. |
| Promotion hashes "evidence trace relations" | **Incomplete** (§4) | Explicit `artifact_manifest` of paths + hashes, including **evidence records**, conflict resolutions, exemptions, protocol debt, policy docs, schema versions. No implicit globs. |
| Protected write set covers specs, Cargo, CI | **Gates unprotected** (§5) | **Gate implementations, schemas, policy files, harness generators, verifier config, and command registries are hash-pinned.** G13 validates before running any gate. |
| `promotion_id` inside exemptions and conflict records | **Forward reference** (§6) | One-way: normative artifacts carry `review` blocks; the receipt lists them; only generated reports cite `promotion_id`. |
| G14 checks each direct relation | **Misses depth** (§7) | **Transitive closure** over required-guarantee dependencies, cycle detection, trust policy applied across the whole closure, **plus an SCC well-foundedness discharge**. |
| `<boundary_id>::<tracking_issue>` | **OK for MVP** (§8) | Keep, **add `assumption_hash` now**. Registry before broad orchestrator use. |
| 5 dispositions | **Conflates axes** (§9) | Split `semantic_disposition` (required/incidental/bug-compat/unspecified) from `lifecycle` (accepted/aspirational/deferred/rejected/out-of-scope). |
| G16 `unresolved > 0 → block` | **Kills dynamic-dispatch clusters** (§10) | **Risk-based**: critical/high → block; medium → human decision; low → visible accepted limitation. Never a full-coverage claim either way. |
| "topological order" | **Ambiguous** (§11) | O edges point **caller → supplier**; scheduling uses **reverse dependency order** (suppliers first) unless modular assumptions permit parallel work. Record the rule. |
| `- id: build ; runner: cargo ; args: [...]` | **Not valid YAML** (§12) | Block-form YAML. **Canonical examples must pass their schema in CI.** No wildcard harness names. |
| Closure as one bit | **Loses the ceiling** | `closure_kind ∈ {deductive, bounded}`. A Kani-owned cluster satisfies the profile while delivering only bounded assurance. |
| "Kani generic callee has no reachable assurance" | **Over-stated** | **Per-instantiation, not type-universal.** ∀-over-inputs holds; ∀-over-type-parameters does not. |
| "concurrent clusters can never exceed `assumed`" | **Over-stated** | True for Kani and Creusot; **Verus has tracked/ghost-permission concurrency.** Ceiling is verifier- and property-class-specific. |

---

## 1. Thesis

The skill covers layer 1 (concepts) and layer 2 (intra-concept contracts). Architecture lives in layer 3 — **client–supplier reliance edges**.

> **The reliance graph is the semantic-dependency view of architecture. It records what each client relies on from each supplier. Deterministic checks validate its structure and traceability. Verifiers, bridge checks, tests, and human promotion determine which obligations are *established* — and at what closure kind.**

Cross-concept reliance is one major source of contract gaps; the reliance graph makes that class explicit. Local failures — frame conditions, overflow, hidden-state mutation, partial functions, panic paths, nontermination, aliasing — remain layer-2 and Stage-8A concerns.

### 1.1 Project structure descriptor (Stage P0)

The pipeline's one external input, and the concrete answer to "project structure as an input": one per project workspace, read by every later stage instead of any stage hardcoding project shape.

```
schemas/project-descriptor.schema.json   ← normative schema, draft-2020-12
schemas/examples/*.example.json          ← canonical examples, checked against the schema in CI
```

```json
{
  "schema_version": "1.0",
  "project": { "name": "example-greenfield", "crate_naming_convention": "^example-greenfield-[a-z]+" },
  "mode": "greenfield",
  "crates": [
    { "crate_dir": "crates/example-greenfield-core", "contracts_crate": "contracts", "specs_search_root": "crates" }
  ],
  "verifier_policy": { "default": "creusot", "scheduling": "kani" },
  "compatibility_policy": { "reliance_policy_path": "docs/reliance-policy.md" },
  "write_set": { "allowed_roots": ["crates/*/src/"], "protected_roots": ["crates/*/specs/**"] },
  "gate_integrity": [{ "path": "scripts/closure_gate.py" }],
  "llm_backend": { "kind": "claude", "command": "claude -p" },
  "review": { "reviewer": "<human>", "reviewed_at": "2026-08-2x" }
}
```

Fields, and why each exists:

- **`project`** — name and this project's own crate-naming convention. Never hardcoded by the pipeline itself (an earlier draft baked in a `^beast-rs-[a-z]+`-shaped regex; that was a modeling error, not a design requirement — see `docs/concept-to-code-modifications.md` gap #4).
- **`mode`** — `greenfield` (Mode R, §11) or `port` (Mode P, §11, a foreign-language source port validated by differential testing). `mode: port` requires `port_source` (repository, origin language, oracle build command) — enforced by the schema's `if`/`then`, not left to convention. Origin-language provenance is recorded here, one layer above concept-to-code, never inside a concept spec (`docs/concept-to-code-modifications.md` gap #3).
- **`crates[]`** — the concept-to-code binding per crate (`crate_dir` / `contracts_crate` / `specs_search_root`), replacing ad hoc CLI flags re-derived on every invocation (`docs/concept-to-code-modifications.md` gap #1).
- **`verifier_policy`** — default verifier plus per-cluster overrides, feeding `closure_kind` (§4) and CG1 cross-verifier detection directly from `crates[].verifier` values concept-to-code already exposes.
- **`compatibility_policy`** — where the governed policy docs live (`reliance-policy.md`, and `witness-policy.md` if §16 is in use).
- **`write_set`** — default `allowed_roots` / `protected_roots` (§10); a starting point each work package narrows, never widens.
- **`gate_integrity`** — gate-implementation paths to hash-pin; G13 validates every one before any gate runs (§10, §12).
- **`llm_backend`** — §6.1's pluggable one-shot backend selection (`claude` / `codex` / `opencode` / `manual`) for stages 0, 3, and findings-driven re-entry.
- **`review`** — same discipline as every other normative artifact in this plan: a human reviewer and date, required, `additionalProperties: false` throughout.

---

## 2. Local ground truth — fix before Prototype A

```
crates/*/specs/_boundaries/<caller_concept>_<caller_method>__to__<callee_concept>_<callee_method>.json
```

- **Flat files.** An earlier tool this rule is inherited from used `glob("*.json")` — no recursion, so nested layouts were silently ignored rather than rejected. `scripts/validate_boundary_naming.py` (chainlink #8) fixes this the other way: it scans recursively and treats anything not directly inside a `_boundaries/` directory as a hard violation, so a nested file surfaces instead of vanishing.
- **Doubled underscores on both sides of `to`.** Single underscores will not match.
- **`boundary_id` == filename stem**, exactly.
- Validate any generator against a live artifact byte-for-byte. Do not re-derive the rule.

`scripts/validate_boundary_contracts.py` implements the full G1a/G1b/G2+ chain against `docs/boundary-contract-schema.json` (chainlink #9/#11); fixtures and the deliberate-failure regression case (chainlink #12) live under `tests/fixtures/boundary_contracts/`.

concept-to-code's current concept schema `$defs` (6, checked directly against `vendor/concept-to-code/schemas/spec.schema.json`): `kani_f64_check`, `query`, `command`, `constraint`, `adversary_case`, `source_reference`. **Neither `command` nor `constraint` carries a stable id** — `query`/`command` have a de facto one via the function name embedded in `rust_sig`, but `constraint` has nothing beyond `english` text and array position, which the whole obligation-reference scheme (`callee_guarantees: [TaskQueue.C003]`, work-package `obligations`, `reliances[].obligation_id`) depends on. See `docs/concept-to-code-modifications.md` gap #6 — decided: add `id: string` to `constraint` upstream, not yet applied. The PascalCase→snake_case conversion this pipeline's own tooling needs (for filename↔body cross-checks) is copied verbatim from `vendor/concept-to-code/emit_stubs.py`'s `snake_case()` — an external review after #37 landed (2026-08-26) found the first version re-derived it via regex instead and broke on acronym concepts (`HTTPClient` → `h_t_t_p_client`), which this plan's own §2 already warned against ("copy the rule, do not re-derive it"). Crate naming is never hardcoded by the pipeline; it's a per-project declaration in the project structure descriptor (§1.1). Every canonical example is copied from a passing live artifact and generalized — hand-written examples are untrustworthy against `additionalProperties: false`.

**Semantic policy** (`docs/reliance-policy.md`), resolving the schema-prose vs. G2+ conflict:

```
callee PRECONDITION                        → bridge specification
callee POSTCONDITION / INVARIANT relied on → callee_guarantees
adversary case (A*)                        → evidence / test seed, never a guarantee
```

The live `PartitionedTreeLikelihood.A006` inside `callee_guarantees` is an **expected G2+ failure** and the first regression fixture.

**Prototype A scope: G2+ is role safety only.** The declared-assurance-target check is removed until §3.1 lands.

---

## 3. Capability-gap register

The honest boundary of what this pipeline can achieve in 2026. Verifier population in the repo: **Creusot 53, Kani 23, Verus 23**.

| ID | Gap | Nature | Ceiling | Response |
|---|---|---|---|---|
| **CG1** | **Cross-verifier composition has no soundness theorem.** Kani (CBMC bounded), Creusot (Why3/SMT deductive), Verus (SMT + ghost) differ in memory model, numeric abstraction, panic/termination semantics, trusted-assumption sets. | permanent, tooling | `harness-tested` | degradation record + tracking issue |
| **CG2** | **Call-graph completeness is undecidable** — dynamic dispatch, fn pointers, closures, FFI. | permanent, theoretical | honest `unresolved` classes | report, never claim closure |
| **CG3** | **Generics under Kani** verify per monomorphization: ∀-over-inputs holds, **∀-over-type-parameters does not**. | tooling | per-instantiation | **placement rule**: assign generic callees to Creusot when type-universality matters |
| **CG4** | **Concurrency** — no coverage in Kani or Creusot. **Verus has tracked/ghost-permission concurrency.** | verifier-specific | `assumed` for Kani/Creusot; **can exceed for Verus** | property-class-specific ceiling, never a blanket rule |
| **CG5** | **Non-falsifiable assumptions** — wait-freedom, complexity bounds, harness completeness. | permanent | `human-risk-acceptance` | typed mitigation, risk-tiered |
| **CG6** | **Circular assume-guarantee is not automatically sound.** If A's proof assumes B and B's assumes A, the pair can be mutually satisfied yet model-inconsistent without a well-foundedness argument — step index, decreasing measure, or temporal stratification. | semantic obligation | requires explicit discharge | SCC closure needs a stated discharge rule (§8.5) |

**CG1 is already latent beyond the two hand-authored boundaries.** The analysis-configuration cluster contains Creusot↔Kani intra-cluster structural edges. Cross-verifier reliance is a property of the current decomposition, not a two-edge special case — the `harness-tested` ceiling and degradation record will fire more often than a two-edge estimate implies.

**Not a capability gap:** `satisfies()` *mechanism* is engineering. But **which** achieved assurance is sufficient for **which** obligation — e.g. accepting Kani bounded at `unwind: 8`, `queue_len_le_8` for a release obligation — is an irreducible risk decision. Profile authorship is versioned, owned human governance. It closes, but never stops needing a human.

---

## 4. Closure profile

A **per-cluster** declared property, with a kind.

```yaml
# specs/_closure/mcmc-chain.yaml
cluster: mcmc-chain
closure_kind: deductive        # deductive | bounded
conditions:
  single_verifier_system: true
  owning_verifier: creusot
  protocol_class_all_pairwise: true
  unresolved_indirect_calls_at_or_above_medium: 0
  generic_callees_type_universal_or_creusot_owned: true
  transitive_assumptions_within_policy: true
  scc_wellfoundedness_discharged: true    # CG6; n/a if acyclic
review:
  reviewer: <human>
  reviewed_at: 2026-08-2x
```

| Owning verifier | `closure_kind` | Meaning |
|---|---|---|
| Creusot / Verus | **deductive** | universal over inputs, modulo SMT completeness and trusted specs |
| Kani | **bounded** | holds only within unwind/harness bounds and enumerated monomorphizations |

Two clusters with identical profile bits carry different guarantees; `closure_kind` is what prevents a Kani-owned cluster from silently reading as full closure.

**Degradation record** — a declared state, not a failure, on the same footing as protocol debt:

```yaml
# specs/_closure/analysis-configuration.degradation.yaml
cluster: analysis-configuration
failed_conditions:
  - single_verifier_system
affected_edges:
  - <boundary_id of the Creusot→Kani edge>
ceiling: harness-tested
tracking_issue: chainlink:...
review:
  reviewer: <human>
  reviewed_at: 2026-08-2x
```

Implemented (chainlink #25) as `docs/closure-profile-schema.json` + `docs/degradation-record-schema.json` + `scripts/validate_closure.py` (G1a/G1b/G17), JSON rather than YAML like every other artifact type here, at `specs/_closure/<cluster>.json` and `specs/_closure/<cluster>.degradation.json` — the same directory and the same filename distinction this section's two examples already draw, workspace-level because a cluster spans crates by construction (`analysis-configuration` in §3 is the worked example).

Three additions to the shape above, each because the gate could not otherwise do its job:

- **`work_packages[]`** — this section names a cluster without saying what is in one, and G14's closure has to start somewhere. The cluster declares its own entry work packages; the closure then follows dependencies *out* of them, including into work packages the cluster never named.
- **`scc_discharges[]`** — §8.5 requires an explicit well-foundedness discharge, and `scc_wellfoundedness_discharged: true` is a bit, not an argument. Each entry names its SCC's members exactly, a `kind` from §8.5's own closed vocabulary (`step-index` / `decreasing-measure` / `temporal-stratification` — free text would let "we reviewed it" pass as a discharge), and the argument itself. The gate checks that one exists, covers the SCC actually computed, and was reviewed; it cannot check that the argument is *correct*, and says so rather than implying otherwise.
- **`scc_wellfoundedness_discharged: "not-applicable"`** — this section's own "n/a if acyclic", made a value instead of a comment. An acyclic closure must declare `not-applicable`, not `true`: claiming a discharge that was never needed is a vacuous truth, the same objection #48 raised against a vacuous OK.

`generic_callees_type_universal_or_creusot_owned` is **the one condition the gate cannot recompute** — no artifact in this pipeline carries the type information CG3 needs. It stays a human declaration, and `gate-g14` reports it as declared-not-verified on every run rather than letting it pass as checked. Every other condition is recomputed from the closure and rejected on disagreement in both directions, the discipline G1b already applies to computed eligibility in I. `unresolved_indirect_calls_at_or_above_medium` is not recomputed either — it is *read* from `scripts/gate_r1_g16.py`'s `unresolved_at_or_above_medium()` (#24), so the closure profile and the R1/G16 gate can never disagree about what "unresolved" means.

What a degradation record can excuse is bounded by its own vocabulary, and that bound is the point: `failed_conditions` is an enum of **condition keys only**, so a missing achieved record, an unsatisfied requirement, an unsupported dependency, a failed bridge, or a mis-declared condition bit keeps blocking with a record present. Those are absences of evidence; a ceiling is a limit on a claim, and there is no vocabulary here in which a human accepts "the proof is missing". G17 checks profile and record against each other in both directions — a record naming a condition the profile declares as holding is a stale excuse (the cluster reads as degraded after the gap closed, and nobody shuts the tracking issue), and a profile declaring a condition false with nothing covering it is an undeclared degradation.

---

## 5. Four graphs

| Graph | Meaning | Source | Normative? |
|---|---|---|---|
| **S** | declaration / type dependency | `gen_concept_graph.py` | **no** — candidate suggestion only |
| **I** | **intended** method interaction **+ assurance requirement** | `docs/interaction-schema.json` | yes, after promotion |
| **C** | **realized** call relation — `C_static` / `C_dynamic` | config-pinned extractors | observed, configuration-relative |
| **O** | contractual reliance | `_boundaries/*.json` v1.0 | yes, after promotion |

- **R2 (pre-impl):** every *eligible* I edge covered by an O artifact or a reviewed exemption.
- **R1 (post-impl):** C reconciled with I — eligible calls, compatible configurations, risk-tiered on extraction confidence.
- **S:** proposes candidate I edges only.

R2 proves coverage **relative to accepted I**; it does not prove I complete. Since Stage 3 generates both I and O, independent candidate sources are mandatory: Mode P source call extraction, tests and runtime traces, S candidates, data-flow analysis, requirements, a separate critic pass, human promotion.

Measured (chainlink #26) by `docs/gold-set-schema.json` + `scripts/validate_gold_set.py` (the human end) and `docs/gold-set-measurement-schema.json` + `scripts/measure_gold_set.py` (the arithmetic), wired as `validate-gold-set` / `measure-gold-set`. A gold set is a **reviewed** artifact (`specs/_gold_sets/<cluster>.json`, review block, human curator) rather than a machine observation, because it is a judgement about which edges exist and a judgement with no author is not evidence. Three numbers per cluster, and the third is why the issue exists:

| | |
|---|---|
| **precision** | I edges, within the curated scope, that the gold set contains |
| **recall** | gold edges that I proposed |
| **omission** | gold edges **no** candidate source proposed at all |

A fourth is reported beside omission because the two failures are different and differently fixable: **`missed_but_proposed`** — a gold edge absent from I that an independent source *did* propose. The signal existed and the pipeline did not use it.

**Direction discipline.** An edge C_static proposes that I lacks is **R1's** territory (§9.1, `gate-r1-g16`), and it is deliberately not counted here; #26 runs the other way — gold edges nobody proposed. The two are kept apart in the schema, the code, and the report, because letting a good R1 score read as evidence about completeness is exactly the confusion this measurement exists to prevent.

**Anti-circularity is structural, not exhortation.** A gold set copied out of the I set it audits scores recall 1.0 vacuously. Provenance is therefore per edge (`derived_from`), and its enum contains no value meaning "read it in the interaction set" — a curator who worked from I has to write something false rather than merely omit something. G1b additionally requires each edge's method to be one the curator declared, each caller to be inside `examined_concepts` (precision must not count an edge nobody was asked to look at), and refuses intra-concept edges, which `gate-r1-g16` is right to treat as internal helpers.

**Which sources are honestly covered, and which are recorded as absent.** Of the seven independent sources named above, exactly **one** exists as tooling in this codebase: C_static extraction (#24). S-graph, tests/traces, data-flow, requirements, a critic pass, and Mode P source extraction (#5) are **not built** — none of the six has tooling behind it in this codebase, on *either* test track. On a Mode R workspace, Mode P source extraction carries the further, permanent status **not applicable to the track**: it is Mode P's own source and has no place in a Mode R measurement even once #5 exists. The two statuses are kept apart deliberately — "not built" is a fixable gap; "not applicable to track" never is, for a workspace running the other track. Every measurement lists all seven sources with their real status, and the printed summary says "measured against 1 of 7 independent candidate sources" every run, because an omission count is only interpretable next to how many sources were consulted — three omissions against one source is a different statement from three against eight. The gold set is itself a declared `human-lower-bound`, so an omission count from this prototype is a lower bound computed against a lower bound, and says so.

**Not a gate.** A non-zero omission count blocks nothing; it is a finding a human reads. What fails is being *unable* to measure — no gold set, a gold set for a track this workspace is not running, or a scope in which I proposed nothing (precision `undefined`, never `1.0`; #48's discipline applied to an audit rather than a validator).

An external review found two gaps. **High:** the issue's own acceptance criterion is "at least one pilot cluster per test track," and the repository carried only the Mode R pilot — no Mode P gold set, no successful Mode P measurement. Fixed with `tests/fixtures/gold_sets/valid/specs/_gold_sets/scheduler-core-port.json` (`track: mode-p`) and a `mode: port` project descriptor fixture, proving `measure_gold_set.py` measures a Mode P cluster correctly end to end — track derivation from `descriptor["mode"]`, the track-match rejection in both directions, and the source register's Mode-P-specific status all included. This does not implement Mode P itself (chainlink #5 remains unbuilt); it is a fixture pilot cluster, the same precedent as `tests/fixtures/callsites/`'s never-compiled Rust (#24) and `tests/fixtures/bridges/verifier/fake_verifier.py` (#47) — real machinery, exercised against a stand-in, the stand-in visible. **Medium:** `mode-p-source-extraction` reported `status: available` on a Mode P workspace while its own `detail` field said the same source was "not built" and its edge set was empty — overstating how many independent sources were actually consulted. Fixed: since chainlink #5 does not exist in this codebase on *either* track, the status is now `not-built` on a Mode P workspace (matching the other five unbuilt sources) and stays `not-applicable-to-track` only on Mode R, where the distinction above applies.

### 5.1 Interaction schema (I) — now carries the assurance requirement

```yaml
# docs/interaction-schema.json → specs/_interactions/*.json
interaction_id: I-SCHED-TQ-001
caller:
  concept: Scheduler
  method: dispatch
callee:
  concept: TaskQueue
  method: pop_ready
edge_class:
  - stateful
  - cross-verifier
eligibility: boundary-required      # COMPUTED from edge_class; stored must match (G1b)
rationale: "dispatch's postcondition depends on pop_ready's return discipline"
evidence_links:
  - E-0143
protocol_class: pairwise
realization:
  requirement: required             # required|optional|feature-gated|platform-gated|test-only|fallback-only
  config_scope:
    target: x86_64-unknown-linux-gnu
    features: [default]
    cfg: []
reliances:                          # §3.1 of review, Option A
  - obligation_id: TaskQueue.C003
    required_assurance:
      required_claims:
        - postcondition-holds
      accepted_evidence_kinds:
        - creusot-deductive-check
      minimum_scope:
        input_domain: queue_len_le_8
        feature_set: default
      trust_policy:
        assumptions_allowed: []
review:
  reviewer: <human>
  reviewed_at: 2026-08-2x
# NO promotion_id here — one-way references (§7.1)
```

### 5.2 Eligibility is computed

```
cross-verifier | cross-crate-public-api | stateful | error-panic-boundary
  | ownership-transfer | numeric-domain-boundary   → boundary-required
pure-data-type-reference | import-only             → inform
marker-type | phantom-type                         → ignore
```

G1b recomputes and rejects disagreement. A proposing model cannot mark a stateful cross-verifier edge `ignore`. Exemptions are separate reviewed objects carrying a `review` block (not a `promotion_id`).

Implemented in `docs/interaction-schema.json` (G1a; chainlink #16 scope only — `interaction_id`, `caller`/`callee`, `edge_class`, `eligibility`, `rationale`, `evidence_links`, `review`; deliberately without `reliances[].required_assurance` (#17), `realization`/`config_scope` (#18), or `protocol_class` (#19), which extend this same schema when their milestones land) and `scripts/validate_interaction.py`: `compute_eligibility()` implements the table above exactly (boundary-required wins over inform, which wins over ignore, when an edge carries more than one class — every schema-valid `edge_class` array resolves to exactly one bucket), and G1b rejects disagreement with the stored `eligibility` in *both* directions, not just under-claiming — an edge overstated as `boundary-required` for a `marker-type`-only class is rejected exactly like a stateful cross-verifier edge understated as `ignore`. Filename/layout discipline mirrors boundary contracts: flat inside a `_interactions/` directory, `interaction_id` == filename stem.

Exemptions are implemented in `docs/exemption-schema.json` + `scripts/validate_exemption.py`: a separate object at `crates/*/specs/_exemptions/<interaction_id>.json`, `interaction_id` == filename stem (one exemption per interaction), carrying its own `review` block and never a `promotion_id` (`additionalProperties: false` rejects one outright — references are one-way, §7.1). Chainlink #16 scope is schema + naming (G1a/G1b) only; cross-referencing that the named interaction is real and actually eligible, and that R2's coverage requirement (boundary OR reviewed exemption) is satisfied, is R2's own job (#21), not this validator's. Both validators are wired into `pipeline.py` as `validate-interaction`/`validate-exemption` (Stage 4, descriptor-driven crate iteration like `validate`) and into the `draft`/`approve` dispatcher (`_select_validate_fn`) alongside boundary contracts, so `pipeline.py approve` gates all three artifact types through the same reviewed-checkpoint mechanism.

A further external review (2026-09-01) found two residual gaps, both reproduced directly before being fixed: (1) high — neither `check_naming` checked the file suffix, so a schema-valid interaction or exemption saved as `*.yaml` matched G1b's other checks with zero findings, and separately, both scan-side `validate()` functions silently `continue`d past any non-`.json` file instead of reporting it, so a malformed or wrong-extension artifact under a real `_interactions`/`_exemptions` directory produced an overall OK — fixed by adding an explicit `.json`-suffix check to `check_naming` (G1b) in both validators, no longer skipping non-`.json` files in the scan loop (so unparseable content now surfaces as a G1a finding instead of vanishing), and adding the same suffix refusal to `pipeline.py`'s `_select_validate_fn` dispatcher up front, before its directory match runs — reproduced end to end through `approve`, which previously wrote a `.yaml`-named artifact straight through with a real validator's blessing; (2) medium — the Stage 4 scan commands (`cmd_validate_interaction`/`cmd_validate_exemption`) searched recursively for any directory named `_interactions`/`_exemptions` anywhere under the crate, not anchored to the crate's declared `specs/_interactions`/`specs/_exemptions` layout, so a schema-valid artifact under `<crate>/not_specs/_interactions/` passed with zero findings — the same class of gap the approve dispatcher was already anchored against (§7.1's `#15` review chain, and the boundary-contract fourth review pass), just not yet applied to these two scan commands. Fixed with a new `validate_dir()` in each validator module that scans only an already-known, exact directory (no `_interactions`/`_exemptions`-anywhere search), called by `pipeline.py` with `project_descriptor.interaction_dir_for()`/`exemption_dir_for()`'s result; the standalone CLIs' own recursive `validate()` is deliberately left unanchored (mirrors the boundary-contract standalone CLI, which has no crate-descriptor concept to anchor to) but now reports rather than skips non-`.json` files. A crate with no interactions/exemptions declared yet is not an error (`validate_dir` returns `[]` for a missing canonical directory); only a missing *crate* root is a config mistake worth failing loud on.

A THIRD review pass (2026-09-01) found the medium-severity fix above was itself wrong: anchoring the scan to *only* the canonical directory stopped a mislocated artifact from being wrongly validated, but it also stopped that artifact from being examined at all — a crate with no `specs/_interactions` and a fully schema-valid artifact under `<crate>/not_specs/_interactions/` still reported OK, reproducing the exact zero-findings outcome the original review objected to, just by omission instead of false acceptance; reproduced directly with otherwise-valid fixture content before fixing. `validate_dir()` is replaced by `validate_crate(crate_root, canonical_dir)` in both validator modules: it discovers candidates crate-wide (the same recursive `find_interaction_files`/`find_exemption_files` discovery the standalone CLI already used), then rejects — with an explicit G1b finding naming the expected and actual directory — any candidate whose resolved parent isn't exactly `canonical_dir`, regardless of whether the artifact's own content would otherwise be schema-valid. `pipeline.py`'s `cmd_validate_interaction`/`cmd_validate_exemption` now call `validate_crate(crate_root, interaction_dir_for(...))` instead. Regression fixtures assert this end to end: a fully valid artifact placed under `not_specs/_interactions/` (or `_exemptions/`) now fails with exit code 1, proving location alone causes rejection, not merely that a differently-broken payload happens to fail for an unrelated reason.

`reliances[].required_assurance` (chainlink #17) extends `docs/interaction-schema.json` with the worked example above field-for-field. §8.1's type split is enforced structurally, not by custom code: `required_claims` and `accepted_evidence_kinds` are two disjoint JSON Schema enums (claim values only in one, verification-method values only in the other), so a `bridge-checked`/`bounded-model-check`-style value can never land in the wrong field — the schema itself makes the mixing this section warns against impossible to write, in either direction. `minimum_scope` is deliberately left open (`additionalProperties: {"type": "string"}`, mirroring `promotion-receipt-schema.json`'s `schema_versions`) since its vocabulary is verifier-specific; `trust_policy` is closed, matching the worked example's single `assumptions_allowed` field exactly. `reliances` itself is optional at the schema level — an `inform`/`ignore`-eligible edge has nothing to declare a reliance on, so it's never forced into an empty array. `scripts/validate_interaction.py` adds one new G1b semantic check, `check_reliance_obligation_uniqueness`: two reliance entries for the same `obligation_id` within one interaction are rejected (mirrors `validate_boundary_contracts.py`'s tracking-issue-uniqueness check) — otherwise there would be no way to tell which of two conflicting `required_assurance` blocks governs. Cross-referencing that a declared `obligation_id` actually names a real obligation on the callee (the same kind of resolution boundary contracts' G2+ already does for `callee_guarantees`) is deliberately out of scope here, for the same reason #16 deferred it — a future gate's job, not this schema's.

An external review (2026-09-01) found this initial delivery left **G2++** unimplemented — the gate table's own row (§12: "Declared assurance requirement present in I *(after §5.1 lands, not Prototype A)*") that chainlink #11 explicitly deferred until #17 landed. `reliances` being schema-optional with no `minItems` meant a `boundary-required` interaction with `reliances` omitted, or `reliances: []`, passed with zero findings — reproduced directly before fixing. This is not R2's job (#21): R2 checks whether an *eligible* interaction is *covered* by a boundary artifact or reviewed exemption; G2++ checks, independently, whether the interaction *declares* an assurance requirement at all — a `boundary-required` edge with no boundary contract yet **and** no declared reliance would otherwise be invisible to both checks. Fixed with `check_g2_plus_plus` in `scripts/validate_interaction.py`: any interaction with stored `eligibility == "boundary-required"` must have a non-empty `reliances`; `inform`/`ignore` edges remain exempt, since `reliances` is optional precisely for their case. An empty array is treated identically to an omitted field — both declare nothing. Independently reproduced via raw script calls confirming both the omitted-field and empty-array cases are now rejected, and that `inform`/`ignore` edges remain unaffected either way.

`realization.requirement` + `config_scope` (chainlink #18) extends `docs/interaction-schema.json` with the worked example's `realization` block: `requirement` is a closed enum of the six documented values (`required`/`optional`/`feature-gated`/`platform-gated`/`test-only`/`fallback-only`); `config_scope` requires `target`/`features`/`cfg` all present, matching the worked example field-for-field. Unlike `reliances` (§17, conditionally optional via G2++), `realization` is **required on every interaction regardless of eligibility** — the issue text is "each interaction edge declares," not conditioned on `boundary-required`, so an `inform`/`ignore` edge must declare it too. `target` is checked against a loose target-triple shape (2–4 lowercase hyphen-separated segments) rather than an exhaustive enumeration of real Rust targets, which changes over time and isn't the kind of fixed vocabulary a schema enum suits. `features` and `cfg` carry no `minItems`, unlike `edge_class`/`required_claims` — an edge can legitimately be gated by nothing (always compiled, no extra `cfg` predicate), matching the worked example's own `cfg: []`. No new G1b/G2-style semantic check was added for `realization` itself: unlike `eligibility` (derived from `edge_class` by a documented formula) or `reliances` (gated by G2++, a documented table row), plan.md states no derivable or cross-field rule for `requirement`/`config_scope` to check against, so none was invented.

An external review (2026-09-01) found the `target` pattern itself was wrong: `^[a-z0-9_]+(-[a-z0-9_]+){1,3}$` rejected real Rust target triples that use a dot within a segment, e.g. `thumbv8m.main-none-eabi` and `thumbv8m.base-none-eabi` (Cortex-M33/M23, with/without the Main/Base architecture profile) — reproduced directly before fixing. Fixed by allowing `.` inside each segment's character class: `^[a-z0-9_.]+(-[a-z0-9_.]+){1,3}$`. Both dotted targets, and the existing garbage/no-hyphen negative cases, were independently re-verified via raw script calls after the fix.

A second pass (2026-09-01) found that fix was itself too loose: `[a-z0-9_.]+` allows a dot anywhere, including a leading dot (`.-none-eabi`), a trailing one (`thumbv8m.-none-eabi`), consecutive dots (`thumbv8m..main-none-eabi`), a dot immediately before a hyphen (`thumbv8m.main.-none-eabi`), or a segment that's nothing but a dot (`foo-.-bar`) — all five reproduced as accepted before fixing. Fixed by requiring dots to sit strictly between non-empty components: `^[a-z0-9_]+(?:\.[a-z0-9_]+)*(?:-[a-z0-9_]+(?:\.[a-z0-9_]+)*){1,3}$` — each hyphen-separated segment is itself a non-empty, dot-joined sequence of non-empty sub-components, so a dot can never be adjacent to a hyphen, another dot, or a string boundary. Verified against all 320 built-in `rustc --print target-list` targets (all match), both `thumbv8m.*` targets, all five malformed-dot cases (all rejected), and the pre-existing `garbage`/`not_a_target_triple` negatives (still rejected) — independently, via raw script calls, before signing off.

### 5.3 Protocol classification — fail closed

Non-pairwise classes require a protocol artifact or a protocol-debt record. An out-of-scope declaration suffices only when **all five** hold: no promoted obligation depends on the protocol; no work package touches its path; no release claim includes it; a human signed the scope cut; a tracking issue records the missing support. A normal boundary exemption is never sufficient for a temporal obligation.

Implemented (chainlink #19): `protocol_class` is added to `docs/interaction-schema.json` as a required, closed two-value enum (`pairwise`/`non-pairwise`) — required on every interaction like `realization` (§18), since "non-pairwise classes require..." presupposes every interaction always has a classification to check. A "protocol artifact" itself is deliberately not built here: plan.md gives it no worked example or shape anywhere (unlike every other artifact type this session has delivered), and it most plausibly refers to the not-yet-built bridge spec (§8.2, `specs/_bridges/*.yaml`, which already carries its own `protocol_class: pairwise` field) — future M4/M6 work, out of scope for #19.

The protocol-debt record IS fully specified in prose and is built as its own artifact type: `docs/protocol-debt-schema.json` + `scripts/validate_protocol_debt.py`, one per `crates/*/specs/_protocol_debt/<interaction_id>.json`, `interaction_id` == filename stem (mirrors `docs/exemption-schema.json`'s discipline exactly — same naming/layout, same G1a/G1b/`validate_crate` shape — but a distinct artifact type, since "a normal boundary exemption is never sufficient for a temporal obligation"). Of the "all five" conditions: the two that are checkable from the record's own fields are directly enforced (`tracking_issue`, non-empty; `review`, required); the three that are facts about promoted/external state (no promoted obligation depends on it, no work package touches its path, no release claim includes it) cannot be mechanically re-verified by a standalone schema — they are represented as `const: true` attestation fields instead, so a record can never be filed admitting one is false, even though the schema can't independently confirm it's true. This is the same cross-referencing boundary #16 drew for R2/exemptions, applied here to G15.

G15 itself (does a non-pairwise interaction actually have a corresponding protocol artifact or protocol-debt record) is a cross-file coverage check, structurally identical to R2's own.

An external review (2026-09-01) found this deferral was wrong: #21 is only the I-schema milestone gate ("all of #15-#20 landed"), not an issue that itself implements gates — #19's own title says "fail closed," so G15 belongs to #19, not to whatever eventually implements R2. The review reproduced a non-pairwise interaction with zero coverage artifacts passing with zero findings, plus two gaps in the debt-record validator itself: a debt record naming a nonexistent interaction, and one filed for what is actually a *pairwise* interaction, both also passed with zero findings. All three reproduced directly before being fixed.

**G15 is now implemented**, fail-closed, in `scripts/validate_interaction.py` (`check_g15_protocol_coverage`) and `scripts/validate_protocol_debt.py` (`check_interaction_cross_reference`, labeled `G2` — a dangling-reference check, mirroring boundary contracts' own `G2`). Since a "protocol artifact" has no schema, the only checkable coverage mechanism today is a *valid* protocol-debt record naming the interaction — "valid" meaning it itself passes G1a/G1b and its own `G2` cross-reference (real interaction, actually `non-pairwise`). `pipeline.py`'s `cmd_validate_interaction`/`cmd_validate_protocol_debt` and the `approve` dispatcher all wire the two validator modules together via `project_descriptor`-style composition at the orchestration layer — neither validator module imports the other, avoiding a circular import, the same reason `project_descriptor.py` itself exists. Lower-level cross-reference checks accept an optional lookup defaulting to `None` and may emit a visible **info**-severity note ("not checked in this context") for callers that have not supplied a workspace/crate root; the descriptor-driven `validate_crate()` used by `pipeline.py` and the standalone interaction CLI both supply a real coverage context, making them genuinely fail-closed. `main()` in both standalone scripts filters findings by severity (mirroring `validate_boundary_contracts.py`'s own established info/error split).

An end-to-end review found three residual workflow gaps. First, the two ordinary approval orders for a brand-new non-pairwise interaction and its debt record formed a cycle: each side required the other to be already approved. `review_checkpoint.approve_pair()` and `pipeline.py approve-pair` provide the explicit bootstrap transaction: both drafts receive one human review, their body IDs and filename stems must match, both are validated against a combined in-memory view, and both targets are committed together; ordinary single-artifact approval remains fail-closed. The pair checkpoint prepares both audit entries before committing and rolls back targets, drafts, and the prepared log append on later write failures. Second, `load_interactions_by_id()` now admits only schema-valid, canonically named, reviewed interactions and omits duplicate IDs, so malformed or independently unapproved JSON cannot satisfy a debt reference. Third, the standalone interaction validator derives G15 coverage recursively from sibling `_protocol_debt/` directories under its workspace/crate root, keeps that coverage keyed by its corresponding `_interactions` directory, and treats missing coverage as empty, so an uncovered non-pairwise interaction exits nonzero rather than producing an informational pass or borrowing coverage from another crate.

A further finding (medium) questioned the three externally-facing attestation fields (`no_promoted_obligation_depends_on_protocol`, `no_work_package_touches_its_path`, `no_release_claim_includes_it`): `const: true` prevents recording `false`, but doesn't establish the conditions actually hold, and offered a choice — mechanically verify what's checkable and block on what isn't, or explicitly declare attestation authoritative. Given no artifact in this codebase captures *which code path a protocol covers* (a prerequisite for checking "no work package touches its path" at all), and given "release claim" has no schema yet, partially mechanizing just one of the three would imply more rigor than actually exists. `docs/protocol-debt-schema.json`'s description now states this explicitly as a normative authority model, not a silent gap: a human reviewer's signed attestation is authoritative for all three, exactly as plan.md §7.2 already establishes review as the terminal authority for every other checkpoint in this system (boundary contracts, exemptions, promotions — none of those are mechanically re-derived by their validators either).

---

## 6. Scope of the workflow

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                     LLM SIDE — proposes, NON-NORMATIVE                         │
│  Stage 0  EVIDENCE INTAKE  → claim + origin + semantic_disposition + lifecycle │
│  Stage 1  CONCEPTS     L1                                                      │
│  Stage 2  INTRA-CONTRACTS L2                                                   │
│  Stage 3  INTERACTION (I, incl. assurance requirement) + RELIANCE (O)          │
│           + BRIDGE SPECS (call-site fact semantics)                            │
│           + independent candidate sources                                      │
└──────────────────────────────┬─────────────────────────────────────────────────┘
════════════════════════════ DETERMINISM BOUNDARY (≠ AUTHORITY) ═════════════════
                               ▼
│  Stage 4    ADJUDICATION                                                       │
│             G1a JSON Schema · G1b repo semantics + COMPUTED eligibility        │
│             G2 ref-integrity · G2+ ROLE SAFETY · R2 I↔O                        │
│             G4 evidence · G5 grounding · G11 unresolved conflicts · G15 protocol│
                               ▼
│  Stage 4.5  PROMOTION → detached receipt w/ EXPLICIT artifact_manifest          │
                               ▼
│  Stage 5    EMISSION      emit_stubs.py · G8 emitter fidelity · NO injection    │
│  Stage 6    ATTACH GATE   attachment dimension only                             │
│  Stage 7    WORK-PACKAGE MANIFEST  read-only · hash-pinned incl. GATE CODE      │
                               ▼
             ORCHESTRATION  (one issue → one owner → one worktree → one PR)
             scheduling: REVERSE dependency order — suppliers before callers
                               ▼
│  Stage 8A   IMPLEMENTATION VERIFICATION                                         │
│             per-obligation claim/evidence/scope/trust records ·                 │
│             scoped call-site coverage · bridge checks · R1 (risk-tiered)        │
│  Stage 8B   ACCEPTANCE (non-normative)  P: differential · R: G10R               │
│  Stage 8C   RELEASE CLOSURE  G14 satisfies() over TRANSITIVE closure            │
│             + SCC well-foundedness discharge (CG6)                              │
│             → closure profile w/ closure_kind, or degradation record            │
        │                         │                          │
        └── change requests ──────┴── drift findings ─────────┘
                    ▲                                         │
                    └──── targeted evidence backfill ─────────┘
```

```mermaid
flowchart TD
  subgraph LLM["LLM — proposes, non-normative"]
    S0["Stage 0 Evidence<br/>claim + semantic_disposition + lifecycle"] --> S1["Stage 1 Concepts L1"] --> S2["Stage 2 Intra-contracts L2"]
    S2 --> S3["Stage 3 I (+assurance req) + O + bridge specs<br/>independent candidate sources"]
  end
  S3 --> S4["Stage 4 Adjudication<br/>G1a G1b G2 G2+role-safety R2 G4 G5 G11 G15"]
  S4 -->|fail| FB["Findings + change requests"]
  S4 -->|pass| P45["Stage 4.5 Promotion<br/>detached receipt, explicit artifact_manifest"]
  P45 --> S5["Stage 5 Emission — G8"] --> S6["Stage 6 Attach gate"]
  S6 --> S7["Stage 7 Work-package manifest<br/>gate code hash-pinned"]
  S7 --> ORCH["Orchestration — suppliers before callers"]
  ORCH --> S8A["Stage 8A Verification<br/>claim/evidence/scope/trust · call-site scope · bridges · R1"]
  S8A --> S8B["Stage 8B Acceptance"] --> S8C["Stage 8C Closure<br/>G14 transitive + CG6 discharge<br/>→ closure_kind or degradation record"]
  S8A -.unprovable.-> FB
  S8C -.shortfall.-> FB
  FB -.targeted backfill.-> S0
```

### 6.1 One CLI, pluggable LLM backend, human checkpoints from §7.2

The diagram's two halves are not two separately-invoked tools. **One CLI spans the whole pipeline**, stages 0 through 8C, reading the project structure descriptor for all configuration. The determinism boundary is enforced in *how* it drives each half, not in *who* invokes it. Implemented in `scripts/pipeline.py` (chainlink #37) — `draft` (Stage 0/3, dispatches the configured `llm_backend`), `approve` (the checkpoint from below), `validate` (Stage 4 G1a/G1b/G2+, fully deterministic) are real today; `pipeline status` reports the rest (promotion, emission, manifest, Stage 8A–8C) as not yet implemented rather than silently no-op'ing, since their schemas don't exist until M3/M4 land:

- **LLM-side stages (0 evidence intake, 3 interaction/reliance/bridge drafting, and any findings-driven re-entry into Stage 0)** — the CLI invokes a configurable **one-shot LLM backend** against a structured-output prompt template: the LLM must emit only the target JSON/YAML matching the relevant schema, nothing else, so the CLI can parse stdout directly with no human transcription step. Backend is pluggable — `claude -p`, `codex exec`, `opencode run`, or a manual mode that prints the constructed prompt and waits for the user to run it themselves and hand back the result. Whichever backend produced it, the output lands in the same place a human-drafted artifact would and is immediately run through G1a/G1b for fast local feedback (cheap syntax/eligibility checks — full Stage 4 adjudication still happens later, unchanged).
- **Every human-authority checkpoint already listed in §7.2** — first cluster acceptance, new concepts, new/weakened contracts, new high/critical assumptions, compatibility-policy changes, evidence-conflict resolution, protocol exemptions, cross-verifier trust decisions, assurance-profile authorship, closure-profile/degradation-record acceptance — is a **pause point**, not a suggestion. The CLI surfaces the proposed artifact (diff against the prior version, or full content if new) and waits for an explicit approval before continuing into Stage 4. An LLM backend may recommend; per §7.2 it is never the sole authority, and this mechanism must not blur that regardless of which backend produced the draft. Only the versioned-policy auto-promote carve-out in §7.2 (mechanical, no semantic text change, no assurance-target decrease, no new assumption, all hashes still bound) skips the pause. Implemented in `scripts/review_checkpoint.py` (chainlink #39): a candidate artifact is staged as `<target>.draft`, never written to its target path directly; `classify()` operationalizes the carve-out as "identical to an *already-approved* prior version except the `review` block itself" — first-time creation is never mechanical, since there is no approved baseline to diff against; `approve()` is the only path that requires an explicit, non-empty reviewer and appends to an audit log (`ci/results/review_log.jsonl`) that survives even if the artifact changes again later. `approve()` also runs the artifact's mechanical gate (a caller-supplied `validate_fn`, e.g. G1a/G1b/G2+ for a boundary contract) *before* writing anything and refuses to promote on any error-severity finding — an external review after #37 landed (2026-08-26) found the first version let a human approve a schema-invalid or role-unsafe draft straight through, which broke this section's own ordering: mechanically-valid → semantically-reviewed → accepted. A second review pass (2026-08-26) found the fix itself was incomplete: `validate_fn` still defaulted to `None`, which silently meant "skip," so any caller that simply forgot to wire one in — including the standalone `review_checkpoint.py approve` CLI, which has no way to know which validator applies to an arbitrary target — still approved with no gate. Fixed by removing the default entirely: `approve()`/`auto_promote_if_mechanical()` now require either a real validator or the explicit `SKIP_VALIDATION` sentinel, and the standalone CLI requires an explicit `--skip-validation` flag to proceed at all (use `pipeline.py approve` instead, which wires in the right validator by the `specs/_<kind>/` directory convention). The same pass also found and fixed: a missing or typo'd scan root produced "OK, 0 findings" indistinguishable from a genuinely clean project (`find_boundary_files` now raises `FileNotFoundError` on a nonexistent root); `review.reviewer`/`reviewed_at` had no `minLength`/format enforcement, so `reviewer: ""` and `reviewed_at: "not-a-date"` both validated (fixed with `minLength: 1`, a defense-in-depth date pattern, and centralizing validator construction in `scripts/schema_utils.py` so `format: date` is actually enforced everywhere, not just where a caller happened to pass a format checker); and `pipeline.py draft`/`approve` accepted a target path verbatim with no check it belonged to the workspace or any declared crate (`_require_target_in_workspace` now refuses both). A third review pass (2026-08-27) found the second pass's fix was still incomplete at a different layer: `pipeline.py`'s own `_select_validate_fn` dispatcher returned `SKIP_VALIDATION` for *any* target it didn't recognize — a boundary artifact placed under a typo'd `specs/_boundary/` (missing the trailing s) matched nothing, silently got `SKIP_VALIDATION`, and was approved with zero gating; reproduced end to end. The `--skip-validation` escape hatch on the standalone `review_checkpoint.py approve` CLI was the same failure one level down — explicit didn't make it safe, it just made an unsafe default opt-in. Fixed by inverting the dispatcher's default: it now raises for anything it doesn't recognize as a known artifact type, rather than falling back to no validation, and the standalone CLI no longer offers `approve` at all (only the read-only `classify`/`diff`) — promotion only ever happens through `pipeline.py approve`, which always resolves a real validator or refuses outright. The audit log also now records `validation: "checked" | "skipped"` per entry, since a skipped approval used to be indistinguishable from a validated one in the record itself, not just in code. A fourth review pass (2026-08-30) found that "recognized" was still too loose even after the third pass's fix: the dispatcher's match condition, `"_boundaries" in target.parts`, matches a `_boundaries` path component *anywhere*, not the crate's actual declared layout — a structurally valid boundary at `crate_a/not_specs/_boundaries/x.json` (wrong parent entirely) and one at `crate_a/specs/nested/_boundaries/x.json` (nested) both matched and promoted successfully; neither is caught by G1b's own "flat" check either, since that only confirms the file sits directly inside *a* directory named `_boundaries`, with no opinion on where that directory sits. Fixed by anchoring the check to the exact expected path — `target.parent` must equal `<crate_dir>/specs/_boundaries` for some declared crate, not merely contain that name somewhere upstream.
- **Stage 4 → Stage 7 (adjudication → promotion → emission → attach → manifest)** run fully deterministically once past that checkpoint — no LLM calls, by construction, for the reason CG1–CG6 exist: a gate that could ask an LLM "does this look right" would make the LLM's own Stage 0–3 proposal self-certifying.
- **Stage 8A → 8C** are the same CLI, invoked again later (post-orchestration, per-PR or at release time) — deterministic verifier dispatch and closure computation, no LLM calls there either.

---

## 7. Promotion

### 7.1 Detached receipt with an explicit manifest

```yaml
# specs/_promotions/mcmc-chain.yaml
promotion_id: PROM-MCMC-001
cluster: mcmc-chain
reviewer: <human>
policy_version: reliance-policy@1.2
schema_versions:
  boundary: 1.0
  interaction: 1.0
accepted_at: 2026-08-2x
artifact_manifest:
  - path: crates/beast-rs-mcmc/specs/mcmc_chain.json
    hash: sha256:...
  - path: crates/beast-rs-mcmc/specs/_interactions/I-MCMC-001.json
    hash: sha256:...
  - path: evidence/E-0143.json
    hash: sha256:...
  - path: specs/_conflicts/EC-004.json
    hash: sha256:...
  - path: docs/reliance-policy.md
    hash: sha256:...
```

The manifest **states the exact set**; no implicit globs. It covers concept specs, interaction specs (incl. assurance requirements), boundary specs, bridge specs, **evidence records**, evidence links, conflict resolutions, exemptions, protocol-debt records, design decisions, compatibility policy, `reliance-policy.md`, and applicable schema/policy versions. The receipt is **not** in its own manifest.

Any listed file changing invalidates the receipt → **acceptance revoked**.

Implemented in `docs/promotion-receipt-schema.json` (G1a, matches the worked example above field-for-field — `reviewer`/`accepted_at` top-level, not nested under a `review:` block, since promotion *is* the human-sign-off event §7.2 describes, not a separate artifact being reviewed) and `scripts/validate_promotion_receipt.py` (chainlink #15), plus `pipeline.py validate-promotion` — standalone, not routed through the draft/approve checkpoint, for the same field-shape reason `approve()`'s nested `review: {}` write can't be reused here, and because receipt *generation* (reading an accepted artifact set and computing the manifest) is separate, not-yet-built work needing the rest of the I-schema machinery, the same boundary already drawn for the work-package manifest's own generator. Every rule above is a real mechanical check, not just schema shape: every `artifact_manifest` path is checked for workspace escape both syntactically (no absolute path, no `..` segment) and after resolution (guards a symlink pointing outside the workspace — verified against a real symlink, not just asserted); every declared hash is recomputed from the real file and compared, with a mismatch reported in exactly the plan's own language ("acceptance is revoked"); the receipt's own path is checked against its own `artifact_manifest` (self-reference is a hard error); and every listed JSON artifact is checked for a `promotion_id` field of its own, since references are one-way. Built with every class of gap the `#14` review chain found already closed from the start, rather than found by review after the fact.

A further external review (2026-08-31) found four residual gaps in the initial delivery, all reproduced directly before being fixed: (1) high — the one-way-reference check only ever inspected `.json` artifacts, so a correctly hashed YAML artifact carrying its own `promotion_id` passed with zero findings, even though §7.1's rule is about an artifact being normative, not which serialization it uses — fixed by loading `.yaml`/`.yml` artifacts the same as `.json` before checking for the field; (2) medium — the receipt itself was never anchored to the workspace or checked against its canonical location, so a receipt copied to an arbitrary path like `/tmp/scheduling.json` validated cleanly against a fixture workspace it wasn't even part of — fixed by resolving the receipt path for workspace containment and requiring it sit directly under `specs/_promotions/`; (3) medium — `artifact_manifest` enforced no set uniqueness, so appending an identical copy of an existing entry produced zero findings — fixed by tracking every entry's *resolved* path (not its string) during validation, catching both literal duplicates and different path strings that alias the same real file; (4) low — the standalone `validate_promotion_receipt.py main()` entrypoint was still untested, since the three CLI integration tests only ever invoked `pipeline.main()` — fixed by adding tests that call `validate_promotion_receipt.main()` directly with real argv.

**References are one-way:** normative artifacts carry `review` blocks and never a `promotion_id`; the receipt lists artifacts; only generated reports cite `promotion_id`.

### 7.2 Authority

Human sign-off required for: first cluster acceptance; new concepts; new or weakened contracts; new high/critical assumptions; compatibility-policy changes; evidence-conflict resolution; protocol exemptions and scope cuts; cross-verifier trust decisions; **assurance-profile authorship**; **closure-profile and degradation-record acceptance**.

Versioned policy may auto-promote only mechanical changes with no semantic text change, no assurance-target decrease, no new assumption, and all hashes still bound. **An LLM critic may recommend; it is never the sole authority.**

---

## 8. Assurance and bridges

> **Normative files declare *required* assurance. CI files record *achieved* assurance.**

### 8.1 Type split — claim / evidence / result / scope / trust / support

`bridge-checked` and `bounded-model-check` are different categories and must never share a field.

| Value | Category |
|---|---|
| `documented` | specification state |
| `tested`, `harness-tested` | evidence method / result |
| `kani-bounded-model-check`, `creusot-deductive-check`, `verus-deductive-check` | verification method |
| `callee-precondition-established`, `postcondition-holds` | claim |
| `assumed` | trust state |
| `unsupported` | support state |

Required:

```yaml
required_assurance:
  required_claims:
    - callee-precondition-established
  accepted_evidence_kinds:
    - kani-bounded-model-check
    - creusot-deductive-check
    - verus-deductive-check
  minimum_scope:
    input_domain: queue_len_le_8
    feature_set: default
  trust_policy:
    assumptions_allowed: []
```

Achieved:

```yaml
claim:
  kind: callee-precondition-established
  result: pass
evidence:
  kind: kani-bounded-model-check
  verifier: kani
  harness: dispatch_C002_bridge
  scope:
    input_domain: queue_len_le_8
    unwind: 8
    harness_hash: sha256:...
trust:
  assumptions: []
support:
  status: supported
config:
  toolchain: ...
  target: ...
  features: [default]
```

`proved` is retired. **`same-system-composed` stays out of the schema.** Reintroduce only when one live edge demonstrates: one common verifier model, formal caller and callee contracts, a checked call-site implication, explicit assumption handling, and a stored assurance result. Both current boundary artifacts cross systems (Creusot→Kani, Creusot→Verus); no demonstration path exists today.

### 8.2 Bridge specification — call-site fact semantics

A caller **postcondition** holds after the caller returns; the call to the callee happens *inside*. It cannot establish a callee precondition at an earlier call site. The real implication is:

```
caller preconditions ∧ caller invariants ∧ path condition ∧ prior call results
   ⟹ callee precondition
```

Available facts at a call site: caller preconditions, type invariants, local assertions, branch conditions, loop invariants, previous callee postconditions, argument predicates, trusted environment assumptions. **The enclosing caller postcondition is normally not among them.**

```yaml
# specs/_bridges/BR-SCHED-TQ-001.yaml
bridge_id: BR-SCHED-TQ-001
boundary_id: scheduler_dispatch__to__task_queue_pop_ready
callee_requirement: TaskQueue.C001
available_contract_facts:
  - obligation_id: Scheduler.C001
    role: caller-precondition
  - obligation_id: Scheduler.C003
    role: invariant
required_local_facts:
  - fact_id: queue_ready_at_call
    expression: "queue.has_ready_at_or_before(now)"
target_expression: "TaskQueue.C001(args, callee_state)"
protocol_class: pairwise
```

The assurance result records whether the verifier established the implication **at each call site**.

### 8.3 Bridge logic — typed expression language (preferred)

Prose `bridge_logic` cannot support machine assurance: the harness may assert a weaker property, G8 cannot check fidelity, and the result cannot show the declared bridge was checked.

```yaml
bindings:
  caller_self: Scheduler
  callee_self: TaskQueue
  args:
    now: Time
premises:
  - "caller_self.queue_ready(now)"
  - "callee_self.invariant()"
conclusion:
  obligation_id: TaskQueue.C001
```

Generate the harness from this representation.

**Temporary alternative:** make the harness itself normative and hash-pinned, and report `harness-tested` — **never `bridge-checked`**, because no machine relation exists between prose and harness.

**Superseded (chainlink #47) within a single verifier system.** The machine relation now exists: `scripts/bridge_harness.py` compiles a promoted `bridge_logic` to a harness deterministically, so the harness hash is a property *of the bridge* rather than of whoever last edited a file. A hash-pinned hand-written harness pinned *a* harness; a derived hash pins *this bridge's* harness, and `scripts/gate_g9.py` re-derives it on every run rather than comparing stored values. The `harness-tested` ceiling still applies **across** verifier systems (CG1) — enforced at the cluster level by the closure profile's `single_verifier_system` (#25), not by this compiler.

`bindings` become the harness's typed parameters (a nested group `args: {now: Time}` flattens to one parameter `args_now`, so a premise and a signature cannot disagree); `premises` become the assumed facts; `conclusion` becomes what the verifier must establish. Three renderers, one per system: Creusot `#[requires]`/`#[ensures]` over an empty body (with no code to execute, `ensures` can only be discharged from `requires`, which is exactly §8.2's call-site implication), Verus `proof fn ... requires ... ensures`, Kani `assume`/`assert` under `#[kani::proof]` — bounded, which is why a Kani result makes its cluster's `closure_kind` `bounded`.

Two checks fall out of compilation rather than being bolted on: a premise may only apply an obligation the bridge declared in `available_contract_facts` (§8.2's central error class, mechanically refused), and every root identifier must be a declared binding — a harness cannot pass a value it was never given. `target_expression` is deliberately **not** compiled: this repo's own artifacts read it two ways (an obligation application in §8.2, a callee call in #22's fixture), and inventing a semantics for an ambiguous field is the kind of guess the closed fragment exists to prevent; #22 already ties `callee_requirement` to `conclusion.obligation_id`.

Dispatch is a pluggable command per verifier (`verifier_backends` in the project descriptor, exactly as `llm_backend` is for Stage 0/3), resolved to the verifier that **owns** the bridge's cluster — through the closure profile that names the work package requiring the bridge, then `verifier_policy`. This repository has no Rust crates, so nothing here runs a real verifier, and the response is to build the dispatch for real and refuse to fake the verdict: with no backend configured, the harness is still generated and **no record is written**, which blocks at G9. A crashed or unparsable runner records nothing either — a verifier that did not run has not refuted anything, and writing `result: fail` would turn an infrastructure failure into a claim about the bridge. Tests drive it through a visible stand-in (`tests/fixtures/bridges/verifier/fake_verifier.py`), the same precedent as `tests/fixtures/callsites/`'s never-compiled Rust.

G9 then checks that a recorded result is about *this* bridge: the harness on disk still matches a recompilation, the recorded hash equals the recomputed one, the verifier owns the cluster, `claim.kind` is `callee-precondition-established` and passed, and — the relation #25's G14 was missing — no work package's assurance report asserts a `bridge_record` that disagrees with the check. G14 consumes those records as evidence a bridge passed; until now nothing stopped one from being written by hand. `gate_g9.bridge_records_for()` lets a work package's CI assemble its report *from* the checks that ran, which is the honest direction.

### 8.4 Assumption identity

```yaml
assumption_ref:
  boundary_id: scheduler_dispatch__to__task_queue_pop_ready
  tracking_issue: chainlink:713
  assumption_hash: sha256:...
```

G1b enforces one tracking issue per assumption within a boundary. Sufficient for Prototype A and early bridge work. **Move to a registry before broad orchestrator use** — i.e. when an assumption spans boundaries, a method or boundary is renamed, one issue tracks several assumptions, history must survive text changes, or assumptions become shared project concepts.

### 8.5 G14 — transitive closure plus well-foundedness

Direct A→B checking passes while a C-level assumption sits below policy. G14 must:

1. Load all required-guarantee dependencies.
2. Compute the **transitive** dependency closure.
3. Detect cycles.
4. Evaluate each requirement with `satisfies(required_profile, achieved_record, context)`.
5. Apply trust policy to **every assumption in the closure**.
6. Fail if any required dependency is unsupported or below policy.

**For cycles (CG6):** mutual satisfaction is not soundness. An O-SCC closes only with all of — every body meets its provided contracts; every bridge requirement passes; every assumption satisfies policy; **and an explicit well-foundedness discharge** (step index, decreasing measure, or temporal stratification showing no instantaneous circular dependence). An accepted interface contract alone does not close the release gate.

Implemented (chainlink #25) as `scripts/gate_g14.py`, wired as `pipeline.py gate-g14`, with all six steps above in that order. The graph comes from the artifacts that already existed: a work-package manifest's `definition_of_done` is both sides of an edge — `provided_guarantees` is what a work package supplies, `required_guarantees` what it needs — so the provider index is `obligation_id → work package`, and the closure is a walk over it from the cluster's own entry manifests. The achieved side needed one new container, `docs/assurance-report-schema.json`, at each manifest's own `report.emit` path: #23 defined the per-obligation record without defining where a gate finds one. Two work packages providing the same obligation is a hard error rather than a first-wins pick — the closure would otherwise be over a graph that depends on directory order.

**Step 5 is not step 4 repeated.** Running `satisfies()` on every edge checks each edge against *its own* `trust_policy`; this section's opening case is an assumption that every such check passes and that the cluster's own entry policy would never have allowed. So the gate additionally collects every assumption appearing in any achieved record anywhere in the closure and holds it against the cluster's entry requirements' allow-lists. That check is what fires on the depth-2 case, and it is a distinct finding from the edge-level one, tagged with the `transitive_assumptions_within_policy` condition so a degradation record can name it.

`satisfies(required_profile, achieved_record, context)`'s third parameter is **defined here** (#23 accepted it unused and named #25 as the issue that would define it): `gate_g14.closure_context()` carries what a cluster-level policy needs and a single edge cannot know — which cluster, its declared kind and owning verifier, which work package required this of which other, and the closure depth. `satisfies()` still ignores it, deliberately: a `required_profile` is authored governance, and silently strengthening or weakening it from ambient cluster facts would move a risk decision out of the reviewed artifact that owns it. Policy is applied in the gate, with each edge's provenance attached to the finding.

Cycle detection is iterative Tarjan (a long dependency chain must not blow the stack in a release gate); a non-trivial SCC is size > 1 or a genuine self-dependency. A discharge must match the computed SCC's member set exactly, and a discharge naming a component that no longer exists is rejected as stale rather than ignored.

The outcome is **per cluster and never global**: `closes` (with its `closure_kind` reported alongside, since two clusters with identical bits carry different guarantees), `degraded` (a reviewed record covers exactly the conditions that failed — exit 0, and the wording never says closed), or `blocked`. There is deliberately no aggregate "everything closed" line, and the gate prints §4's per-cluster caveat every run. Nothing to close is a failure, not a pass (#48's discipline in its most dangerous position): a release gate reporting OK over zero clusters would be claiming exactly what it never computed.

An external review found `load_bridges()` validated every bridge with no boundary context at all — `validate_bridge_data(path, data, validator)` called with no fourth argument defaults `boundaries_by_id` to `None`, and `check_boundary_cross_reference`'s own documented behavior for `None` is "not checked," a non-blocking info note, never an error. A cluster was reproduced closing successfully even though its bridge's `boundary_id` resolved nowhere — "every bridge must pass before closure" was meaningless while the boundary half of a bridge's own G2 check never ran. Fixed by scanning **crate by crate**, the same way `pipeline.py`'s `cmd_validate_bridge` already does: each crate's boundaries are loaded once via `load_boundaries_by_id`, and only bridges directly under that crate are checked against them. Merging every crate's boundaries into one dict was deliberately rejected as the fix — it would let a bridge's `boundary_id` resolve against a *different* crate's identically-named boundary, silently defeating `validate_bridge.py`'s own "must be in the same crate" scoping instead of closing the gap. `load_bridges` and `gate_workspace` now both take the project descriptor; `pipeline.py`'s `cmd_gate_g14` loads and threads it through, and the standalone `gate_g14.py` CLI gained the same `--descriptor` flag its sibling gates already carry.

A second external review found that fix was still incomplete one step later: per-crate *validation* was correctly scoped, but the *result* was merged into one flat `bridges: dict[str, dict]` keyed only by `bridge_id`, via `setdefault` — first-crate-wins. Reproduced: a work package's own crate's bridge is invalidated (its boundary contract removed) while a *different* crate happens to supply an independently valid bridge sharing the same `bridge_id`; the cluster closed by resolving the requirement against the other crate's substitute instead of failing on its own crate's broken bridge. `bridge_id`, like `boundary_id`/`interaction_id`, is a workspace-wide identifier meant to name exactly one bridge — discovering it under more than one crate is not a choice between candidates to resolve from whichever validated, it is an identity collision. Work packages carry no explicit crate association today, so true crate-qualified resolution through a requirement is not available without a larger schema change; what's fixed instead is the weaker, always-correct half of the requirement: any `bridge_id` discovered (a real bridge file naming it, independent of that copy's own validity) under more than one crate is now excluded entirely from the resolvable set, with an explicit `Finding` — the same "ambiguous, never first-wins" choice already made for a work package providing an obligation twice. `load_bridges` now returns `tuple[dict[str, dict], list[Finding]]`; `gate_g14.py`'s own `gate_workspace` and `gate_g9.py`'s `check_bridges`/`gate_workspace` all thread the new findings through rather than silently discarding them.

A third external review found a separate gap in the same function: `find_bridge_files` discovers `**/_bridges/**/*` recursively, with no anchor to a crate's actual declared layout, and `load_bridges` never checked one — an otherwise well-formed bridge stored at `crates/scheduler/not_specs/_bridges/BR-SCHED-TQ-001.json` still resolved and closed a cluster with zero findings. `validate_bridge.py`'s own `validate_crate` already draws this exact line for the standalone CLI (discover crate-wide so a mislocated artifact is actually found, then reject it by location alone); `load_bridges` just never applied the same check. Fixed by requiring each discovered file's parent directory equal the crate's `bridge_dir_for()` exactly before it is treated as a candidate at all — a mislocated file is reported (G1b, mirroring `validate_bridge.py`'s own message) and excluded from both bridge_id resolution and the cross-crate ambiguity tracking above, the same as `validate_crate` already does. A new regression test moves a fixture's bridge to a noncanonical path and asserts the cluster now blocks with an explicit finding, where it previously closed silently.

An external review of #25 (chainlink #49, non-blocking observation, diagnosability not soundness) found `validate_closure.load_cluster_artifacts()` correctly excludes any closure profile or degradation record carrying an error-severity validation finding — the same fail-closed "genuinely valid, not just present" bar every cross-reference in this pipeline applies — but discarded the reason: `gate_g14.gate_workspace()` reported the "degradation record but no valid closure profile" shape, and nothing else, leaving an invalid profile or an invalid degradation record dropped without a word. Fail-closed was correct; silent was not, the same "loud failure over silent nonsense" discipline #48 and `gate_r1_g16`'s empty-reconciliation message already apply. Fixed with `validate_closure.load_cluster_artifacts_with_invalid()`, which shares `load_cluster_artifacts()`'s own single validation pass (`_scan_cluster_artifacts()`) rather than validating each candidate a second time to recover the diagnostic, and additionally returns the sorted paths of every canonical artifact it excluded. `gate_g14.gate_workspace()` now surfaces one deterministic G14 workspace finding per excluded path — file path, "ignored as invalid," and a pointer to `pipeline.py validate-closure` for the detailed reason — symmetric for invalid profiles and invalid degradation records alike, and additive: the existing "degradation record but no valid closure profile" diagnostic, and a valid profile/degradation pair's freedom from any invalid-artifact finding, are both unchanged. `load_cluster_artifacts()` itself keeps its exact prior signature and behavior, so `gate_g9.py` and `generate_feature_ledger.py` (its other two callers) are untouched.

---

## 9. Call sites and C

```yaml
callsite_id: CS-SCHED-DISPATCH-004
caller: Scheduler.dispatch
callee: TaskQueue.pop_ready
source:
  path: crates/beast-rs-scheduler/src/dispatch.rs
  symbol: "Scheduler::dispatch"
  syntax_hash: sha256:...
required_bridges:
  - BR-SCHED-TQ-001
```

```yaml
callsite_coverage:
  discovered: 4
  checked: 3
  unresolved: 1
coverage_scope:
  extractor: coarse-rustc-callgraph
  extractor_version: 0.1.0
  supported_call_forms: [direct, monomorphized-generic]
  unsupported_call_forms: [dynamic-trait, function-pointer]
  completeness_claim: sound-for-supported-forms
```

Report **"all discovered call sites resolved."** Never "all call sites resolved."

### 9.1 R1 / G16 risk policy

| Call class | Missing from I |
|---|---|
| definite eligible direct call | **block** |
| possible eligible dispatch | warn, or block promotion by risk |
| unresolved indirect call | keep unresolved; never claim closure |
| dynamic observation only | evidence, not completeness |
| internal helper | ignore unless architecturally eligible |

Unresolved calls are risk-tiered: **critical/high → block; medium → human decision; low → visible accepted limitation.** A blanket `unresolved > 0 → block` makes every dynamic-dispatch cluster unusable; risk tiering keeps the honest limitation visible without paralysis. Neither path permits a full-coverage claim.

**Minimum viable `C_static`:** coarse compiler-backed extractor, **not MIR**. Classes: `definite-direct-call`, `possible-dispatch`, `unresolved-indirect-call`. Requirements: source-level method identity, target/feature provenance, stable call-site location, honest unresolved output, no regex-only completeness claim. MIR later for release-critical clusters or disputed edges.

Implemented (chainlink #24) as three separated pieces, the same generator/schema/gate split #45 drew for promotion receipts: `scripts/extract_c_static.py` (extraction), `docs/callsite-schema.json` + `scripts/validate_callsites.py` (G1a/G1b), `scripts/gate_r1_g16.py` (R1 + G16), all three wired into `pipeline.py` as `extract-c-static` / `validate-callsites` / `gate-r1-g16`.

The extractor is **syntactic, not compiler-backed** — a Rust lexer (comments, every string/char/raw/byte literal form, lifetimes) plus a brace-depth `impl`/`fn` structural pass, then token-level classification. That is short of this section's "coarse compiler-backed", and the honesty requirement is what makes the gap safe rather than hidden: `coverage_scope` carries an `extractor_backing` field, and the schema itself binds `syntactic` to `completeness_claim: discovered-lower-bound` — only a compiler-backed extractor may assert `sound-for-supported-forms`. A future rustc-driven extractor writes the same schema with a stronger pair; nothing else in the chain changes. This is the structural form of "no regex-only completeness claim": the claim is not prose an implementer may quietly upgrade.

Three scoping decisions, each one made to keep the honest signal from drowning in noise, and each recorded in the artifact rather than left implicit:

- **Concept universe.** C relates *local concept methods*, so the universe is every type with an `impl` block in the scanned sources, recorded in `attribution_scope.local_concepts`. `HashMap::new()` is a resolved call that is simply not an edge — it is counted in `out_of_scope_call_sites`, not tiered `medium`. Tiering every std call would produce exactly the paralysis §9.1 rejects for a blanket `unresolved > 0 → block`.
- **Attribution.** Only methods inside a concept-shaped `impl` block get a caller identity. Free functions, trait default bodies and nested `fn`s are counted in `unattributed_call_sites`; macro invocations (which can expand to calls absent from the token stream entirely) in `macro_invocations`. The blind spots are counted, never silently absent.
- **Unresolved vs out-of-scope.** A bare `f(..)` is classified `unresolved-indirect-call` when `f` is a parameter or `let` binding (a call through a value — fn pointer, closure, boxed trait object, CG2's own territory) or collides with a local method name; an ordinary free-function call is out of scope, not unresolved.

`callsite_coverage` renames this section's `checked` to `resolved` in the extraction artifact, and the gate reports `checked` itself: `checked` is a *reconciliation* outcome (was this call site checked against I?) that extraction cannot know, and a generator emitting a field it cannot compute is how a stored count stops meaning anything. Both are recomputed and rejected on disagreement in **both** directions (G1b), the same way computed eligibility is treated in I — an understated `unresolved: 0` would otherwise buy §4's `unresolved_indirect_calls_at_or_above_medium: 0` closure condition for free.

Risk tiers are **generated at `medium`/`extractor-default` only**. The extractor has no basis for `critical`/`high`, and emitting `low` would silently accept a limitation nobody looked at; moving a tier is a human decision carrying its own `risk_review` block, and `extract-c-static` carries such human tiers across a re-extraction (by `callsite_id`, deterministic for unchanged source) while re-deriving its own defaults. G16's exit code is three-valued to match this section's three dispositions — 0 pass, 1 block, 3 human decision outstanding — so a `medium` collapses into neither neighbour. R1 treats an intra-concept call as an internal helper (info), a cross-concept `definite-direct-call` absent from I as a block (its eligibility cannot even be computed while it has no `edge_class`), and a target-triple mismatch against the matched interaction's `realization.config_scope` as a configuration incompatibility, not a match. The only coverage sentence either tool emits is **"all discovered call sites resolved"**; a test asserts the unqualified phrase never appears in any printed output.

---

## 10. Work packages

Orchestrator unit: **one issue → one owner → one worktree → one PR**.

**SCC grouping is advisory.** Modular verification handles many cycles; an O-SCC is a coupling warning and scheduling input. Group only when implementations must change together, a shared invariant spans both sides, a bridge cannot be checked independently, or intermediate states cannot pass CI.

**Scheduling rule (recorded, not implied):** O edges point **caller → supplier**; implementation proceeds in **reverse dependency order — suppliers before callers** — unless modular assumptions permit parallel work.

```yaml
# ci/manifest/WP-MCMC-004.yaml — GENERATED, READ-ONLY
schema: work-package-manifest/1.0
work_package: WP-MCMC-004
issue: chainlink:900
depends_on:
  - WP-MCMC-002
coupling_notes:
  - "O-SCC with proposal_kernel; separable — G14 closes at release"
scheduling_rule: reverse-dependency-order

functions:
  - mcmc::Chain::step          # write-set consistent (§10.1)
obligations:
  - Chain.C002
  - BR-CHAIN-PK-001

provenance:
  base_commit: a1b2c3d
  promotion_id: PROM-MCMC-001
  artifact_set_hash: sha256:...
  toolchain: nightly-2026-05-01
  target: x86_64-unknown-linux-gnu
  features: [default]

gate_integrity:                 # §5 of review — G13 validates BEFORE running any gate
  - runner: scripts/closure_gate.py
    hash: sha256:...
  - runner: scripts/callsite_coverage.py
    hash: sha256:...
  - runner: scripts/validate_boundary_contracts.py
    hash: sha256:...
  - runner: docs/boundary-contract-schema.json
    hash: sha256:...
  - runner: docs/reliance-policy.md
    hash: sha256:...

write_policy:
  allowed_write_set:
    - crates/beast-rs-mcmc/src/
    - crates/beast-rs-mcmc/tests/
  protected_write_set:
    - "crates/*/specs/**"
    - "ci/manifest/**"
    - "scripts/**"
    - "docs/*-schema.json"
    - "docs/reliance-policy.md"
    - "**/Cargo.toml"
    - "**/build.rs"
    - "tests/harnesses/**"
    - ".github/**"
    - "rust-toolchain.toml"
    - "**/kani.toml"
    - "Justfile"
  on_conflict: raise_change_request

definition_of_done:
  provided_guarantees:
    - obligation_id: Chain.C002
      required_assurance:
        required_claims:
          - postcondition-holds
        accepted_evidence_kinds:
          - creusot-deductive-check
        minimum_scope:
          feature_set: default
        trust_policy:
          assumptions_allowed: []
      harness: chain_step_C002        # exact name, no wildcards
  required_preconditions_to_establish:
    - bridge_id: BR-CHAIN-PK-001
      required_assurance:
        required_claims:
          - callee-precondition-established
        accepted_evidence_kinds:
          - creusot-deductive-check
      callsite_requirement: all-discovered-resolved
  required_guarantees:
    - obligation_id: ProposalKernel.C003
      assume_during_check: true
      required_assurance:            # enforced by G14 at 8C, not here
        required_claims:
          - postcondition-holds
        accepted_evidence_kinds:
          - creusot-deductive-check
  trusted_assumptions:
    - assumption_ref:
        boundary_id: mcmc_chain_step__to__proposal_kernel_propose
        tracking_issue: chainlink:713
        assumption_hash: sha256:...
      risk: high
      mitigations:
        - kind: test
          reference: tests/chain_detailed_balance.rs
        - kind: human-risk-acceptance
          reference: PROM-MCMC-001

gates:
  - id: build
    runner: cargo
    args:
      - build
      - -p
      - beast-rs-mcmc
  - id: prove
    runner: cargo
    args:
      - creusot
      - -p
      - beast-rs-mcmc
  - id: bridges
    runner: cargo
    args:
      - test
      - -p
      - beast-rs-mcmc
      - --test
      - bridges
  - id: callsites
    runner: python
    args:
      - scripts/callsite_coverage.py
      - --wp
      - WP-MCMC-004
  - id: realized
    runner: python
    args:
      - scripts/extract_call_graph.py
      - --check-against
      - I

failure_policy:
  verifier_timeout: raise_change_request(kind=budget_or_assumption)
  obligation_unprovable: raise_change_request(kind=contract_revision)
  missing_callee_guarantee: raise_change_request(kind=missing_contract)
  unintended_call_edge: raise_change_request(kind=interaction_revision)
  unresolved_callsite: raise_change_request(kind=callsite_unresolved)
  forbidden:
    - weaken_or_delete_contract
    - delete_or_stub_harness
    - add_trusted_assumption
    - edit_protected_paths
    - modify_gate_implementation
    - change_features_or_toolchain
    - disable_test_registration

report:
  emit: ci/results/WP-MCMC-004.json
  per_obligation_assurance_record: true
```

### 10.1 Validator rules

- **Every owned function's source path is covered by `allowed_write_set`.** A package listing a callee in another crate without that crate in its write set is invalid.
- `promotion_id` resolves; `artifact_set_hash` matches the current normative set.
- Every `assumption_ref` resolves to a real boundary + tracking issue + hash.
- **Every gate implementation hash matches before any gate runs** (G13 pre-flight).
- Harness names are exact; no wildcards unless the runner explicitly supports them.
- **Canonical manifest examples must pass their schema in CI.**

Implemented in `docs/work-package-manifest-schema.json` (G1a, draft-2020-12, matching the worked example above field-for-field) and `scripts/validate_work_package.py` (chainlink #14), plus `pipeline.py validate-work-package` — standalone, not routed through the draft/approve checkpoint, since a manifest is machine-generated at Stage 7 from already-promoted content rather than LLM-drafted and human-reviewed the way a boundary contract is. Of the five rules above, three are fully mechanical today and actually enforced (not just schema-shaped): gate-implementation hashes are computed from the real files on disk and compared (G13); `allowed_write_set`/`protected_write_set` are checked for literal overlap; every `assumption_ref` is resolved against a real boundary contract's own `assumptions[]` entry, not just pattern-matched. Two are honestly unimplemented and say so as a visible, non-blocking finding rather than silently passing or failing: `promotion_id` resolution (nothing to resolve against until #15 lands) and "every owned function's source path is covered by `allowed_write_set`" (no schema-computable mapping from a Rust module path to a file path without real crate source, which this workspace doesn't have).

An external review (2026-08-30) found the first version of all three "fully mechanical" checks still failed open in real ways: write-set overlap only compared literal strings, so `allowed: scripts/dummy_gate.py` against `protected: scripts/**` reported clean; assumption-ref resolution silently degraded to a non-blocking note whenever `--specs-search-root` was omitted, so a manifest with real `trusted_assumptions` could print `OK` without the check ever running; and `boundary_id` was interpolated directly into a glob pattern, so a `boundary_id` of `"*"` resolved successfully against any boundary file in the tree. A fourth finding: `gate_integrity` accepted an absolute path or a `../` traversal that resolved outside the workspace entirely (`Path("/a") / "/etc/hosts"` silently discards the left side in `pathlib`) and still passed with the correct hash. A fifth: only `.json` was ever accepted, though this section's own worked example is `ci/manifest/WP-MCMC-004.yaml`.

All five fixed: write-set overlap is now a real segment-based glob intersection (exact for the literal/`*`/`**` grammar this schema uses — a naive shared-prefix heuristic would have falsely flagged `crates/*/src/**` against `crates/*/specs/**` as conflicting, which the fix specifically avoids); assumption-ref resolution is a hard error, not an info finding, whenever `trusted_assumptions` is non-empty and no search root is available (both CLIs now default the search root to the workspace root rather than leaving it unset); boundary resolution matches by scanning real boundary files and comparing each one's own declared `boundary_id` field exactly, never by building a glob from untrusted input, and requires exactly one match (ambiguous matches are a hard error, same discipline as the boundary-contract validator's own duplicate-concept check); `gate_integrity` now resolves and verifies containment under `workspace_root` before checking any hash; and both `.json` and `.yaml`/`.yml` manifests validate.

A follow-up review pass on the same day found two of those five fixes were themselves incomplete. First, the glob-intersection engine's recursion advanced past a `**` after consuming at most one token — it modeled the Kleene star's epsilon exit (matching zero tokens) but not its self-loop (consuming one token while remaining on the star), so `scripts/**` never actually matched anything nested more than one segment deep, and single-segment inline wildcards like `docs/*-schema.json` weren't recognized as wildcards at all. Rewritten as `_glob_language_overlap`, a proper BFS reachability check over the product automaton (both transitions modeled), applied uniformly at the path-segment level and, within one segment, the character level, so both gaps close with one mechanism rather than two special cases. Second, assumption resolution trusted any JSON file with a matching `boundary_id` field found anywhere under a directory literally named `_boundaries`, including a garbage file at an arbitrary, non-crate path — fixed by requiring a candidate to pass both naming/layout (§2's `__to__` convention) and full G1a schema validation before its content is trusted, and, when the project descriptor is available (`pipeline.py`'s production path), restricting candidates to exactly the descriptor's declared `<crate_dir>/specs/_boundaries` directories. A third, separate finding from the same pass: `pyyaml` (added for YAML manifest support) was an undeclared dependency with no manifest anywhere in the project — fixed with `requirements.txt`, verified against a genuinely clean virtualenv rather than assumed.

A further review pass (2026-08-31) found two more fail-open paths in what remained. First: `allowed_write_set`/`protected_write_set` patterns were never checked for escaping the workspace itself — `allowed_write_set: ["../outside-worktree/**"]` passed with zero errors, the write-policy equivalent of the `gate_integrity` path escape fixed above. Fixed with a syntactic check (`check_write_set_anchoring`): an absolute pattern or a literal `..` segment anywhere is refused outright, never resolved against the filesystem (these are glob patterns, not concrete paths — `Path.resolve()` doesn't mean anything useful for one). Second: `pipeline.py`'s production path correctly derives canonical crate boundary directories from the project descriptor, but `scripts/validate_work_package.py`'s own standalone `main()` always passed `allowed_boundary_dirs=None`, silently reintroducing the "trust any schema-valid boundary anywhere" gap outside `pipeline.py` even after the library-level fix landed — reproduced with a fully valid, correctly-named boundary placed outside any declared crate. Fixed by requiring the standalone CLI to make an explicit choice every invocation: `--descriptor` (derives directories the same way `pipeline.py` does) or `--allowed-boundary-dir` (repeatable, explicit list) — omitting both is refused rather than silently defaulting to the weaker mode. The descriptor-loading logic itself was factored out of `pipeline.py` into a new shared `scripts/project_descriptor.py` so both files could use it without a circular import (`pipeline.py` already imports from `validate_work_package.py`).

A sixth review pass, on the same fix, found the first version of that requirement still had an escape hatch: a third, named opt-out flag, `--allow-any-crate-boundary`, restored `allowed_boundary_dirs: None` — the exact same trust gap the rest of the fix closed, just behind an explicit switch, and (a separate, smaller finding) not checked for combination with the other two flags either. Reproduced end to end: with `--allow-any-crate-boundary`, a fully valid boundary placed under a non-crate path resolved successfully again. Removed the flag entirely rather than hardening it, the same call already made for `review_checkpoint.py`'s standalone `approve` (§6.1: removed outright, never offered behind `--skip-validation`) — an explicit bypass is still a bypass. There is now no way to skip canonical-crate restriction from the standalone CLI, only two ways to supply it.

---

## 11. Evidence

```json
{ "id": "E-0143",
  "kind": "requirement | source-artifact | observed-behavior | test | comment",
  "claim": "pop_ready returns None only when no task has deadline <= now",
  "origin": { "repository": "...", "commit": "a1b2c3d", "symbol": "TaskQueue::pop_ready",
              "path": "src/queue.cpp", "content_hash": "sha256:...", "line_hint": "118-160" },
  "semantic_disposition": "required | incidental | bug-compat | unspecified",
  "lifecycle": "accepted | aspirational | deferred | rejected | out-of-scope",
  "confidence": "high | medium | low", "mode": "P | R" }
```

The **`claim` field is required** — a hash and location identify bytes, not the proposition the analysis relies on. Splitting `semantic_disposition` from `lifecycle` stops `aspirational` acting as both authority and lifecycle state, and gives Mode R the `rejected` / `deferred` / `out-of-scope` states it needs.

Conflict resolution:

```yaml
conflict_id: EC-004
evidence: [E-0143, E-0201]
status: resolved
resolution:
  selected_authority: E-0201
  disposition_of_other: incidental
  rationale: "compatibility policy: do not preserve the legacy defect"
review:
  reviewer: <human>
  reviewed_at: 2026-08-2x
```

**G11 blocks unresolved conflicts only.**

**Mode P authority:** compatibility policy → accepted dispositions → promoted contract model → C++ code/tests/oracle (evidence only). Differential testing finds counterexamples, never equivalence. **Mode P has a stronger falsifier than Mode R; its ledger does not close.** Governance profiles, not subtraction: Mode P adds a compatibility profile, Mode R a requirements-governance profile.

Implemented (chainlink #20): `docs/evidence-schema.json` + `scripts/validate_evidence.py` match the worked example field-for-field, plus `schema_version` (not shown in this section's terser example, but added for consistency with every other schema in this codebase). `claim` is required and non-empty, matching this section's own words. `semantic_disposition` and `lifecycle` are two independent enums, not one conflated field — every combination of the two validates cleanly, proving `aspirational` can no longer act as both authority and lifecycle state. Evidence deliberately has **no `review` block**: plan.md §7.2's human-checkpoint list names "evidence-conflict resolution," not evidence itself, and Stage 0 is LLM-proposed, non-normative. Consequently evidence is **not** wired into `pipeline.py`'s `approve` dispatcher — `review_checkpoint.approve()` unconditionally injects a `review` block into whatever it promotes, which this schema's `additionalProperties: false` would reject outright.

`docs/conflict-resolution-schema.json` + `scripts/validate_conflict_resolution.py` implement the conflict-resolution shape, with `status == "resolved"` requiring `resolution` + `review` via the schema's own `if`/`then` (the same conditional-requirement pattern §1.1 already uses for `mode: port` + `port_source`). `resolution.selected_authority` must be one of the record's own `evidence` ids (G1b — not expressible as a plain JSON Schema cross-field constraint) and, when the caller supplies a real evidence-id lookup, every id in `evidence` must resolve to a real evidence record (a live dangling-reference check). **G11 is implemented directly, not deferred**: `status != "resolved"` is a hard error at Stage 4 — this section's own words, "G11 blocks unresolved conflicts only," are the literal check, not a policy statement left for a later gate to enforce (a lesson carried forward from #19's own review chain, which found "fail closed" language in an issue's own title/description had been wrongly assigned to a milestone-gate issue that implements nothing itself).

Both artifact types are **workspace-level, not crate-scoped** — plan.md §7's own `artifact_manifest` worked example places `evidence/E-0143.json` and `specs/_conflicts/EC-004.json` with no crate prefix, siblings of `docs/reliance-policy.md`. This required two real fixes beyond just "call `validate_workspace()` once instead of looping over crates": `pipeline.py`'s `_require_target_in_workspace` and `_select_validate_fn` were both structured around crate membership (`raise` if a target isn't under any declared crate's directory), which would have incorrectly refused a legitimate conflict-resolution target in any project whose `crate_dir` isn't literally `"."` — every prior M3 fixture happened to use exactly that convention, which would have hidden the gap. Fixed by recognizing the workspace-level canonical directory as a second, independent form of in-bounds target, checked before (not nested inside) the crate-anchored logic. Verified directly against a `crate_dir: "crate_a"` descriptor (not `"."`), approving a conflict-resolution target that sits outside that crate's directory entirely.

Deliberately out of scope: G4/G5 (evidence tracing to obligations / ungrounded obligations) — neither is named in chainlink #20's own description, and both would need cross-referencing interaction `evidence_links` (#16) to evidence ids, a deeper cross-artifact-type concern like R2's own, not this schema's; tracked in `pipeline.py`'s `NOT_YET_IMPLEMENTED`.

An external review (2026-09-02) found four further gaps, all reproduced directly before being fixed:

1. **High** — `scripts/validate_evidence.py`'s `load_evidence_ids` (used by the conflict-resolution cross-reference) trusted any parseable JSON object with a string `id` field, so a file containing only `{"id": "E-0143"}` — missing every other required field, never checked for correct naming or placement — satisfied a conflict-resolution's cross-reference, and `approve` committed a resolved conflict referencing it. Fixed: renamed to `valid_evidence_ids`, and an id is now trusted only when `validate_file()` returns zero findings for it — the same "genuinely valid, not just present" bar #19's `valid_interaction_ids_from_crate` already applies to protocol-debt coverage. Duplicate ids across different files are excluded rather than trusted ambiguously (defense in depth; `check_naming`'s own flat+id==stem requirement already makes this structurally rare within one directory listing).
2. **Medium** — the standalone `validate_conflict_resolution.py`'s own `validate()`/`main()` left `evidence_ids` as `None` unconditionally, so a resolved conflict in a workspace with no `evidence/` directory at all printed `OK` with only a non-blocking info note. Unlike `scripts/validate_interaction.py`'s standalone CLI (which genuinely has no crate-boundary concept to resolve protocol-debt coverage against), evidence and conflict-resolution are *always* direct workspace-level siblings of the exact same `root` argument — `root / "evidence"` is unambiguous. Fixed: `validate()` now derives `valid_evidence_ids(root / "evidence")` by default whenever the caller doesn't supply an explicit set, making the standalone CLI genuinely fail-closed too, not just the workspace-aware `validate_workspace()`.
3. **Medium** — `pipeline.py`'s `_require_target_in_workspace` recognized workspace-level conflict-resolution targets but not workspace-level evidence targets: with a real `crate_dir` like `"crate_a"`, `pipeline.py draft` refused the canonical `evidence/E-0001.json` target outright, contradicting the claim that evidence could be staged through `draft`. Fixed by widening the same workspace-level check to also accept `project_descriptor.evidence_dir_for()`'s directory (evidence still correctly never reaches `_select_validate_fn`, since `cmd_draft` never calls it and evidence is never approved). The Stage-0 prompt (`prompts/stage-0-evidence-intake.md`) was also stale: it claimed no schema existed, its output contract omitted the now-required `schema_version`, and it called `aspirational` a `semantic_disposition` value in its own prose example even though the schema (and the prompt's own output-contract JSON, correctly) places it under `lifecycle`. All three fixed; the prompt now points at the real schema as authoritative and uses a correct `bug-compat`/`deferred` example pairing.
4. **Medium** — `docs/conflict-resolution-schema.json`'s `evidence` array permitted three or more ids with only one undifferentiated `resolution.disposition_of_other`, so a three-evidence conflict passed with zero findings despite no way to tell which loser the disposition described. plan.md's own worked example and `disposition_of_other`'s singular naming both model a pairwise conflict; fixed with `maxItems: 2` rather than inventing a keyed-by-id structure the text never described.

All four independently reproduced as fixed via raw CLI/script calls outside the test suite, including the workspace-level `draft` fix run end to end against a real `crate_dir: "crate_a"` descriptor. Test count: 346 → 354, full suite green, zero regressions.

A further review pass (2026-09-02, medium) found one remaining layout gap: both standalone CLIs' own `validate()` — unlike their `validate_workspace()` counterparts pipeline.py uses — still only checked a found file's *immediate parent name* (`evidence`/`_conflicts`), never where that directory itself sat relative to the given `root`. A schema-valid evidence record under `<root>/docs/evidence/E-0143.json`, and a schema-valid conflict resolution under `<root>/not_specs/_conflicts/EC-004.json`, both matched and passed with zero findings — reproduced directly before fixing. Since `root` is always the workspace root itself for both of these standalone CLIs (unlike `scripts/validate_interaction.py`'s own standalone CLI, which genuinely has no crate-boundary concept and can't derive one canonical directory), `validate()` in both modules now simply delegates to `validate_workspace(root, root / "evidence" | "specs" / "_conflicts", ...)` — the exact same discover-then-reject-by-location behavior, rather than a separate, weaker unanchored scan. Test count: 354 → 358, full suite green, zero regressions.

---

## 12. Feedback points — drift tabs

| ID | Check | Stage | Severity |
|---|---|---|---|
| **G1a** | Full JSON Schema (draft-2020-12), incl. `additionalProperties: false` | 4 | hard error |
| **G1b** | filename ↔ `boundary_id`; **computed eligibility mismatch**; tracking-issue uniqueness | 4 | hard error |
| **G2** | Reference integrity — dangling `obligation_id` | 4 | hard error |
| **G2+** | **Role safety only** — `A*` as guarantee, sentinel as guarantee, `applies_to` mismatch | 4 | hard error |
| **G2++** | Declared assurance requirement present in I *(after §5.1 lands, not Prototype A)* | 4 | hard error |
| **R2** | eligible I edge with no boundary and no reviewed exemption | 4 | block by edge class |
| **R1** | realized call absent from I — risk-tiered (§9.1) | 8A | block / warn |
| **G15** | non-pairwise protocol without artifact or valid debt record | 4 | block promotion |
| **G16** | call-site coverage; unresolved risk-tiered | 8A | block / decide / accept |
| **G4** | `required`/`bug-compat` evidence tracing to nothing | 4 | block |
| **G5** | ungrounded obligation | 4 | warn on draft; **block at promotion attempt** |
| **G11** | **unresolved** evidence conflict | 4 | block promotion |
| **G6** | assumption lacking risk-tier-appropriate typed mitigations | 4, 8A | always visible |
| **G12** | artifact consumed while unpromoted; manifest hash drift | 4.5→5 | hard error |
| **G7** | achieved assurance written into a normative file; category mixing in an assurance field | 6, 8A | hard error |
| **G8** | emitter fidelity — referenced guarantee not produced on that method (**no injection**) | 5 | hard error |
| **G9** | obligation or mitigation regresses; bridge check fails | 8A / CI | block merge |
| **G13** | protected path edited; **gate-implementation hash mismatch**; write-set/function mismatch; toolchain/feature drift | pre-gate, 8A/CI | hard error |
| **G14** | `satisfies()` false anywhere in the **transitive** closure; **SCC without well-foundedness discharge** | 8C | block release |
| **G17** | closure profile asserts `deductive` while owning verifier is Kani; profile bits inconsistent with degradation record | 8C | hard error |
| **G10** | differential divergence unexplained by disposition *(Mode P)* | 8B | block |
| **G10R** | property / metamorphic / model-based / mutation failures; stakeholder rejection *(Mode R)* | 8B | block |
| **G18** | witness coverage — every `witness_required` query has a witness spec and a generated rendering | 4 | hard error |
| **G19** | witness determinism — regenerate, compare `value_hash` (not the rendering) | 8A / CI | hard error |
| **G20** | degenerate witness — renderer actual ≠ declared (**hard error at generation**, not this gate); declared-expectation mismatch (distribution/fixture/coverage_region) | 4.5 | warn; block at promotion if unresolved |

**Typed mitigations** (`test | monitor | proof | static-analysis | environment-control | human-risk-acceptance | witness`), risk-tiered: critical → human approval + ≥1 technical mitigation; high → human approval + test/analysis/environment control; medium → issue + plan; low → issue + explicit acceptance. **Never force a test for an untestable assumption.** `witness` is acceptable at **`low` only** — it is an example, one fixture wide; order of strength is witness < example-test < property-test < proof.

**Reporting rule (chainlink #48):** a validator that discovered nothing must say so. Every scanning validator's pass line is written by one helper, `scripts/scan_summary.py`'s `pass_line()` — `OK (nothing to check: no <artifacts> discovered under <root>)` for an empty scan, `OK: all <artifacts> pass <checks> (N discovered)` otherwise — because the old shared shape ("OK: all evidence records pass G1a/G1b", printed just as confidently over an empty workspace) existed at 17 call sites, which is how it stayed wrong at all of them at once. Each module owns a `count_discovered()` wrapping its **own** discovery function, so the reported count can never drift from the set actually validated. The exit code is unchanged (0): an empty workspace, or a crate with none of an artifact kind, is a legitimate state — this is a reporting fix, not a gate. **Gates are the opposite:** `gate_r1_g16` fails closed when it reconciles zero reports, and its coverage line for a report with no call sites claims no coverage rather than vacuously "all resolved" — a gate reporting a pass over nothing checked is the same false fixed point in its most dangerous position.

**Fixpoint:** zero mechanical findings is a false fixed point. Convergence needs schema clean, no dangling refs, no *unresolved* conflicts, eligible edges covered or exempted, roles valid, obligations grounded, **promotion receipt matching an explicit manifest**, realized calls matching within compatible configurations, coverage reported with scope, assurance records present, acceptance passed, **G14 transitive closure satisfied**, **a closure profile or degradation record accepted**, and — where a project declares feature witnesses — **witness coverage and determinism clean, with no witness ever read as a substitute for the assurance record beside it**. Five iterations is a safety limit, not evidence of correctness.

---

## 13. Build order

**Before Prototype A**
1. Fix filenames (`__to__`, `boundary_id` == stem, flat).
2. G1a/G1b split with a real draft-2020-12 validator.
3. **G2+ = role safety only** (no assurance-target check yet).
4. Publish `docs/reliance-policy.md`.
5. Treat `PartitionedTreeLikelihood.A006` as an expected migration failure.

**Prototype A** — G2+ against boundary v1.0.

**Ship-early value track** (independent of closure)
6. Work-package manifest + validator, **including gate-integrity hashing** and change-request routing. This is the write-set freeze and spec-weakening prohibition — the value that does not depend on bridges or closure.

**Before I-schema**
7. Detached receipt with explicit `artifact_manifest`.
8. Computed eligibility + separate exemption objects with `review` blocks.
9. Assurance requirement located in I (`reliances[].required_assurance`).
10. `realization.requirement` + `config_scope`.
11. Protocol classification and debt records.
12. Evidence `claim`, `semantic_disposition` / `lifecycle` split, conflict resolution.

**Then**
13. Human gold-set prototype — precision, recall, **and omission analysis**.
14. Typed bridge expression language + call-site fact semantics; harness generation from the typed form.
15. Assurance type split in schemas; `satisfies()` mechanism; profile authorship as governed policy.
16. Coarse `C_static` extractor with honest unresolved classes; R1/G16 risk tiers.
17. Stage 8A; then G14 transitive closure + CG6 discharge; then closure profiles and degradation records.

---

## 14. First end-to-end target

A **verifier-agnostic, mechanized rubric** picks each project's own first end-to-end pilot cluster — never a fixed name, and never a hand-curated table. Applied fresh, independently, to any project's own descriptor and workspace, it selects a **single-verifier, pairwise, non-generic, enum-free** cluster, ranked by deductive-closure value.

**The five criteria, derived from validated project artifacts, never invented or hand-applied:**

- **exactly one verifier** — every concept in the cluster declares the same `verifier` (concept-to-code's own schema-required field: `kani` | `creusot` | `verus`).
- **pairwise** — every intra-cluster interaction (both `caller` and `callee` concepts resolve unambiguously to the cluster) declares `protocol_class: pairwise` (§5.3).
- **non-generic** — the cluster's genuinely valid, reviewed closure profile (§4) declares `conditions.generic_callees_type_universal_or_creusot_owned: true`.
- **enum-free** — no concept in the cluster has `kind: "enum"`.
- **deductive-closure value** — see below. Must be **> 0** to be eligible at all.

**Deductive-closure value, precisely:** for a cluster with exactly one declared verifier V, this is the count of intra-cluster interaction edges when V is a *deductive* verifier (`creusot` or `verus` — concept-to-code's `verifier` enum has exactly three values; `kani` is bounded model checking, not a deductive proof, the identical distinction CG1/CG3 already draw throughout this plan). It is 0 whenever the cluster doesn't have exactly one verifier, or its sole verifier is `kani`. Ranking is **ascending** by this value among eligible clusters only — the pilot role wants the smallest closable unit, "fastest first closure," not the largest (a *different*, not-yet-mechanized role this issue does not ask for). Ties break on concept count ascending, then cluster name — a total order, since cluster name is unique per candidate.

**Architectural limitation: non-generic is rarely mechanically knowable before a pilot exists.** `generic_callees_type_universal_or_creusot_owned` is, by this plan's own design (§8.5's CG3 discussion), never computed anywhere in this codebase — the only place it is captured at all is a closure profile's own declared condition, an artifact that by construction does not normally exist yet for a cluster nobody has picked. Per this issue's own explicit requirement, an absent or silent closure profile is treated as **unknown** and excludes the cluster, rather than defaulting to "assumed non-generic." In practice this means most real candidate clusters are excluded here until a human authors and reviews a minimal closure profile declaring at least this one condition honestly — a genuine bootstrapping cost, reported plainly rather than routed around with an unreliable heuristic (parsing `rust_sig` text for `<...>` was considered and rejected: not a validated field's own declared meaning, just a guess at Rust syntax).

**Implemented** (chainlink #4) as `scripts/select_pilot_cluster.py`, wired as `pipeline.py select-pilot-cluster`. Concept specs are validated against the real, vendored `vendor/concept-to-code/schemas/spec.schema.json` — the first place in this codebase that actually runs that schema against a concept spec document, since `cluster` and `verifier` need to be schema-guaranteed present for this rubric in a way nothing else here required. Interactions reuse `validate_interaction.py`'s own discovery and `validate_data` (R2/G15 coverage left at its default `None`, downgrading to non-blocking info: this rubric asks whether a cluster's *shape* is closable, not whether the boundary contracts a pilot would go on to author already exist — that is the pilot's own future work, not a precondition for picking it). Closure profiles are read via `validate_closure.load_cluster_artifacts_with_invalid` (chainlink #49), the same "genuinely valid, not just present" bar every cross-reference in this pipeline applies. The same concept-identity and cross-reference discipline established elsewhere — "ambiguous, never first-wins" — applies to concept membership: the same concept name declared by more than one genuinely valid spec (agreeing or disagreeing on `cluster`) is excluded from every cluster's membership, reported, never guessed; an interaction naming a concept that resolves to none, or references it from more than one valid file, is excluded from every cluster's edge count and reported the same way. Every candidate cluster, its full derived metrics, its eligibility decision and every reason it failed (not just the first), the final ranking, and the selected pilot are all in one deterministic report; when no candidate is eligible, every exclusion reason is still printed and the command exits nonzero (`EXIT_NO_ELIGIBLE`) — the same "loud failure over silent nonsense" discipline #48/#49 already established, applied here to a selection tool rather than a gate.

An external review found the exit code ignored input findings entirely: a schema-invalid concept spec, an ambiguous concept identity, an invalid interaction, an unresolved edge endpoint, or an invalid closure artifact were all collected and printed, but `main()` returned `EXIT_OK` whenever an eligible cluster still happened to survive elsewhere in the workspace — reproduced directly (an invalid concept spec beside an otherwise-eligible cluster printed the error, selected the cluster, and returned 0). Invalid closure artifacts were also silently discarded rather than reported at all. Fixed with `exit_code_for(ranked, findings)`: any error-severity finding now forces `EXIT_INPUT_ERROR` regardless of whether a ranking exists — the same "genuinely valid, not just present" bar this codebase applies everywhere else, applied here to the report as a whole rather than to one artifact. A provisional ranking is still printed, never withheld (labeled `PROVISIONAL pilot (NOT authoritative ...)` rather than `Selected pilot:`, so a caller reading only the last line can't mistake it for a clean result), and `load_cluster_artifacts_with_invalid`'s own excluded-path list is now threaded into the finding set instead of discarded.

**Applied independently to both test tracks, honestly labeled — and not yet to a real project.** This repository holds no real downstream Rust-target (Mode R) project and no real C++→Rust port (Mode P) project — confirmed: no `crates/` directory exists anywhere in this repository outside a test's own temporary workspace, and `schemas/examples/project-descriptor.*.example.json` are templates, not a real project's own descriptor. A second external review, high severity, held this to the issue's own explicit text ("apply it fresh to both the primary Rust-target project and the C++->Rust port project"): a fixture demonstration proves the evaluator works, but does not satisfy that requirement, and closing #4 without either applying it for real or tracking the gap explicitly would let the requirement quietly disappear. Chainlink #4 is reopened, blocked by chainlink #50 ("Apply the pilot-cluster selection rubric to the real Mode R and Mode P projects"), which exists specifically to carry this remaining work until real project inputs are available — the same "real machinery, exercised against a stand-in, the stand-in visible" precedent chainlink #24's never-compiled callsite fixtures and #26's Mode P gold-set fixture already set, now with the gap itself tracked rather than only mentioned in prose. With that limitation stated plainly, the evaluator was still run fresh, twice, independently, against two visibly-synthetic stand-in project inputs (`tests/test_select_pilot_cluster.py::TwoIndependentProjectsTest`) to demonstrate it produces a *different* answer for a *different* project's own data, never a shared or hardcoded name:

| Stand-in project | Selected pilot | Verifier(s) | Concepts | Intra-edges | Deductive-closure value |
|---|---|---|---|---|---|
| Mode R (greenfield) | `scheduling-core` | creusot | 2 | 1 | 1 |
| Mode P (port) | `parsing-oracle` | verus | 3 | 2 | 2 |

Both runs report zero input findings and select their own single eligible candidate; a real project's own descriptor and workspace, run through the identical `pipeline.py select-pilot-cluster`, is chainlink #50's own remaining work before this section can honestly name a live pilot for either track.

---

## 15. Open items

- **Resolved (chainlink #47):** within a single verifier system, "compiles to a harness the owning verifier checks" **is** a sufficient definition — but only because the compiler is **total or rejecting**. "The harness defines the meaning" is a vacuous answer for a language that accepts arbitrary strings: the meaning of an expression would then be whatever the emitter happened to produce for it, and two readers could disagree about a document that compiled. `scripts/bridge_harness.py` therefore accepts a closed fragment (predicate application over declared bindings — no operators, literals, quantifiers or negation; `premises` is a list, and the list is the conjunction) and refuses everything else with a reason naming the offending text. No formal semantics document is needed for that; what was needed was a compiler that cannot shrug. **Across** systems the answer remains no — CG1, no soundness theorem — so the `harness-tested` ceiling stands there and is enforced by the closure profile's `single_verifier_system` (§4). **Re-confirmed (chainlink #6 audit)** against the actual evidence rather than re-asserted: `docs/bridge-schema.json`'s `bridge_logic` fields, `scripts/bridge_harness.py`'s closed grammar (`expression := obligation | call | path`, no operators/literals/quantifiers/negation, `premises` a list whose conjunction needs no `&&`) and its own `CompileError`-on-anything-else discipline, `pipeline.py check-bridges` dispatching the compiled harness to the owning verifier, and `tests/test_bridge_harness.py::RejectionTest`'s eight explicit refusal cases (operators, in-premise conjunction, negation, literals, quantifiers, unbalanced parens, trailing junk, empty input) are the total-or-rejecting proof, not a claim about them. Nothing further to implement; this bullet documents the audit, not a new resolution.
- **Resolved (chainlink #6 audit):** the concrete well-foundedness discharge format for an O-SCC (CG6) is the temporal-stratification-style argument reviewed by a human, not a machine-checked measure with its own proof obligation — and the current `scc_discharges[]` schema (closed `kind` vocabulary: `step-index` / `decreasing-measure` / `temporal-stratification`; a required `argument` string, `minLength: 40`; exact member-set matching against the computed SCC; stale discharges rejected) is the intended concrete format, not an interim placeholder. **Authority boundary, stated honestly in the schema's own field description and unchanged by this audit:** mechanically checked — a discharge exists, names exactly the SCC `gate_g14.check_cycles` actually computed, uses one of the three closed kinds, and sits inside a genuinely valid, human-reviewed closure profile (`load_cluster_artifacts_with_invalid`'s own "genuinely valid, not just present" bar); never mechanically checked — whether the argument's own reasoning is *correct*. A machine-checked "declared measure with a proof obligation" (e.g. a verifier harness proving a measure strictly decreases across every SCC edge, the same "total or rejecting" discipline `bridge_harness.py` applies to bridge premises) remains a possible future strengthening, but building it now — with no real cross-verifier SCC in this repository to design the measure-expression language against — would be inventing a mechanism to make an issue closable rather than answering a real need; not attempted.
- **Resolved (chainlink #6 audit):** composite assumption identity's real mechanical failure condition is not a cluster-count threshold at all (the issue's own original framing) but a workspace-wide **identity collision**: the same `tracking_issue` naming *different* `assumption_hash` values across boundary contracts — plan.md §8.4's own "one issue tracks several assumptions" trigger, made mechanically checkable. The same `tracking_issue` naming the *identical* hash everywhere remains legitimate (§8.4's "an assumption spans boundaries" case) and is not flagged. **Implemented** as `validate_boundary_contracts.check_assumption_identity_collisions()`, wired into `pipeline.py validate` (Stage 4) over every genuinely valid boundary contract discovered workspace-wide (`load_boundaries_by_id`, one call per crate, merged) — the cross-crate half of the tracking-issue-uniqueness check G1b already enforces within one boundary file. **Authority boundary:** fully mechanically checked, hard error (`G1b`) — no human-review step is needed to detect a hash mismatch. This is deliberately the *smallest* registry-shaped mechanism that fails closed on the one condition actually observable today; a full assumption registry (stable IDs surviving rename/text-edit, cross-boundary linkage as a first-class artifact) remains future work if this collision check starts firing in practice, not built speculatively ahead of evidence that it's needed.
- **General rule documented, project-agnostic (chainlink #6 audit); the one instance named in this plan remains externally blocked.** Whether a cluster with latent cross-verifier edges should be re-decomposed or carry a permanent degradation record is answerable in general, without naming any project's own cluster: **re-decompose** when the cross-verifier edges are a narrow boundary relative to the cluster's own intra-verifier edges (splitting along the verifier line does not fragment mostly-single-verifier work into disconnected pieces), each resulting half stays internally cohesive on its own (its own concepts remain connected via intra-cluster edges, not isolated singletons), and no single CG6 cycle spans both halves (a split that just relocates one cycle across a new crate boundary, still needing cross-verifier trust, has not reduced risk — decomposition must reduce risk, not relabel it); **keep a permanent degradation record** when the cross-verifier dependency is intrinsic to one conceptual unit (no split boundary exists that does not cut through an essential dependency) or when a hypothetical split would only recreate the identical cross-verifier boundary contract one level up. The mechanical inputs this rule needs already exist and require no new tooling: `gate_g14.check_closure_kind`'s own `_evidence_kinds` finding already names which verifiers are actually observed whenever `single_verifier_system` is `False`, and `select_pilot_cluster.py`'s per-cluster verifier/intra-edge extraction (chainlink #4) — pointed at any cluster, not only a pilot candidate — gives the concrete concept and edge counts the rule above weighs. plan.md §3's own `analysis-configuration` is a worked example from a hypothetical downstream project, not a real cluster this repository has evidence for (this repository holds no real Mode R or Mode P project at all — the identical blocker chainlink #50 already names for pilot selection); applying the rule to it, or to any other specific cluster, is chainlink #51's own scoped, linked-blocker work, not decided speculatively here.
- **Resolved:** the witness renderer does not yet emit a canonical numeric result artifact separate from the rendering. Building that artifact is the first build step under W1 (§16.1), a prerequisite for `value_hash` — not open, just sequenced first.
- **Resolved (chainlink #33):** `witness_required` (§16.1) is added to `concept-to-code`'s `query` `$def` as an optional `boolean` (default `false`), co-located with `pure: true`. Reported upstream as gap #5 in `docs/concept-to-code-modifications.md`; the vendored submodule stays an unmodified read-only pin (`deac5cd`) — nothing here edits it. The literal proposed patch is recorded at `docs/concept-to-code-witness-required-schema.json`, kept honest by a drift guard (`tests/test_concept_to_code_witness_required_schema.py`) that pulls every field but `witness_required` verbatim from the live vendored `query` `$def` at test time, plus regression tests for validity when true/false/omitted, rejection of non-boolean values, and compatibility with every real query document across the vendored fixtures. `emit_stubs.py` confirmed to need no change — its query handling is `.get()`-based and reads only `english`/`rust_sig`/`pure`.
- Should the feature ledger's `implementation_observed` feed the orchestrator's work-package readiness check directly, or stay advisory? Advisory first; promote to a scheduling input only after the witness renderer itself is under `gate_integrity`.

---

## 16. Feature witnesses

An ∃-witness layer for layer-2 **pure queries**: evaluate the query on a fixed fixture, render the result deterministically. Same epistemic status as a Gherkin scenario — it can *falsify* (a witness that renders all-constant or crashes is wrong) but never *establish* the ∀ over all inputs. **Evidence, never assurance; never in `accepted_evidence_kinds`; no effect on `closure_kind`.** A cluster with full witness coverage and zero verifier results is still `unsupported` — the feature ledger (§16.4) must show that plainly, never collapse it.

It is stronger than an ordinary scenario in two ways that matter here: **deterministic** (hashable, pinnable in the promotion manifest) and **total over a declared feature set** (coverage becomes a mechanical check, not a by-eye read of a contact sheet).

### 16.1 Witness spec

```
specs/_witnesses/<concept>.<query>.json     ← normative: fixture + determinism claim + expectation
docs/witnesses/<concept>.<query>.svg        ← generated: the rendering
docs/witnesses/_contact_sheet.svg            ← generated: the Stage 4.5 review projection
```

```yaml
# specs/_witnesses/task_queue.load_factor.json
witness_id: W-TQ-LOAD-FACTOR
concept: TaskQueue
query: load_factor                 # must resolve to a query with pure: true (G2-style ref check)
fixture:
  fixture_id: FX-QUEUE-BOTTOM-ROW
  seed: 0
  description: "8-slot queue, 3 ready tasks front-loaded"
renderer: scalar_field_svg
expectation:                       # §16's A2 correction: declare intent, don't infer it
  renderer: scalar_field_svg
  coverage_region: bottom-row      # full-grid | bottom-row | corridor | single-cell | project-defined
  value_distribution: must-vary    # must-vary | constant-allowed
  fixture_family: FX-BOTTOM-ROW
determinism:                       # normative contract: the FEATURE VALUE is stable, not the rendering
  value_hash: sha256:...           # hash of the canonical scalar/data-model result, renderer-independent
  claim: byte-identical-across-runs
  platforms: [x86_64-unknown-linux-gnu]
output:                            # generated rendering; hashed for change-tracking only, never gates promotion
  path: docs/witnesses/task_queue.load_factor.svg
  render_hash: sha256:...
  renderer_actual: scalar_field_svg   # what actually ran — G20 compares against `renderer` above
review:
  reviewer: <human>
  reviewed_at: 2026-08-2x
```

**Why split `value_hash` from `render_hash`:** SVG serialization determinism is fragile — float-to-string formatting, locale, path-coordinate rounding, attribute ordering, font metrics, and renderer-library version all perturb the bytes with no change to the computed value. Pinning the picture would couple the promotion receipt to rendering incidentals; a contributor regenerating docs with a newer renderer would invalidate an unrelated receipt. `value_hash` is the normative determinism contract (G19); `render_hash` is change-tracking only and never gates anything. Same discipline as elsewhere in this plan: pin the data, not the projection.

**`witness_required`** marks a query as a declared feature (open item, §15) — G18 checks coverage *relative to* the declared set, exactly as R2 proves coverage relative to accepted I and no further. A query nobody declared is invisible to G18; completeness of the feature set stays a human/promotion concern, same boundary as everywhere else in this plan.

Implemented (chainlink #27) as `docs/witness-spec-schema.json` + `docs/witness-result-schema.json` + `scripts/validate_witness.py` + `scripts/witness_result.py`, wired as `validate-witness`. Witness specs are **crate-scoped** (`<crate_dir>/specs/_witnesses/<snake_case(concept)>.<query>.json`) because a witness is over one concept's query and a concept lives in a crate — unlike closure profiles and gold sets, which span crates. The stem uses concept-to-code's **own** `snake_case` (copied verbatim from `vendor/concept-to-code/emit_stubs.py`, with a test asserting the two agree), per §2's warning about what re-deriving it cost. `witness_id` is deliberately *not* derived from the filename: §16.1's own example abbreviates TaskQueue to `W-TQ-…`, so a derivation rule would either reject the plan's example or invent an abbreviation scheme nobody declared; it is required to be unique instead.

**What the canonical result IS.** The artifact §16.1 says does not exist yet: `ci/results/witnesses/<witness_id>.json`, holding the numbers the query produced on the fixture and no rendering at all. Three closed result kinds — `scalar`, `series`, and `grid` (a sparse 2-D field, which is what `scalar_field_svg` draws and what `coverage_region` is a statement about). A rendered field's value domain is captured as `value_domain{distinct_values, minimum, maximum}`, **recomputed on every validation and rejected on disagreement**, so a degeneracy check (#31) reading `distinct_values` reads a fact rather than a claim; minimum/maximum are compared as decimals, never lexically ("10" < "9" as strings).

**The float rule — how the hash is computed, and the one decision everything rests on.** A canonical result contains **no JSON numbers for measured values**: every value is a decimal *string* in a constrained form. The consequence is that hashing never formats a float. This section names float-to-string formatting as a reason `render_hash` cannot be normative — and a canonicalizer that formatted floats would drag that identical hazard into `value_hash`, one layer down and much harder to see. Formatting therefore stays with the producer, where G19 catches drift by regenerating: a producer whose formatting moves fails a determinism gate (a true statement about that producer) instead of silently changing a normative hash. `scripts/witness_result.py`'s `canonical_number()` is the reference encoder — shortest round-trip, one spelling of zero, normalized exponents, no trailing fractional zeros, and NaN/±Inf refused outright (a witness whose value is not a number has no stable value to pin). Serialization is `canonical-json-v1`: sorted keys, `(',',':')` separators, no trailing newline, over exactly `witness_id · concept · query · fixture_id · seed · result` — identity of the fact plus the fact. `renderer_actual`, `value_domain` and every path are excluded: the same numbers from a different renderer are the *same fact* and must hash the same, while the same numbers from a different fixture are a *different fact* and must not. The rule and its field list are recorded **in** each artifact, so a future change to the derivation is a visible version bump rather than a silent re-hash.

**How `renderer_actual` is recorded, and why the comparison exists as data.** Both the witness spec's `output` and the canonical result carry it, and both are `required`. That is what makes the declared/actual comparison a *fact on disk* rather than a convention someone remembers: a generator that fell back to a degraded renderer (a text strip standing in for a field plot) has to write down that it did, and a result found on its own can still say what made it. `validate-witness` fails on `renderer_actual ≠ renderer` because the check is self-contained in one document — the same reasoning that put #22's `callee_requirement ↔ conclusion.obligation_id` check in its validator. **Hard-failing at generation** is the renderer contract (#28), and G20 (#31) is the gate; this issue supplies the field they compare.

**Where fixture families live.** Nowhere new: a family is *defined by* the witnesses that declare it. The property that makes the declaration meaningful is checked instead — every witness naming one `fixture_family` must declare the same `coverage_region` — which is exactly §16.2's "coverage_region inconsistent with fixture_family", made mechanical without inventing an artifact type nobody writes. A standalone family registry becomes worth building only when a family needs to exist with no witness in it.

**Boundary, drawn the way `validate_closure.py` drew its line against `gate_g14.py`.** This validator checks a witness against its schema, its filename, its own two renderer fields, and the concept spec (G2: the query must resolve to a `pure: true` query, matched on the spec's own `concept` field the way `validate_boundary_contracts.py` matches a constraint — and skipping underscore-prefixed artifact directories, since a witness carries its own top-level `concept` and an unfiltered scan finds *itself*). It checks a result against *itself* — canonical values, sorted unique grid cells, recomputed domain and hash. It does **not** do G18 coverage (#29; and `witness_required` is not in the vendored schema yet — #33, reported upstream, never applied to the submodule), G19 determinism across a regeneration (#30), or G20 degeneracy (#31). And the epistemic rule holds throughout: a witness is **evidence, never assurance**. No field in either schema is read by `satisfies()`, `accepted_evidence_kinds`, or any closure computation, and a test asserts that vocabulary is absent from the spec schema outright.

There is no Rust renderer in this repository, and the implementation does not pretend otherwise: `tests/fixtures/witnesses/stand_in_producer.py` is a visible stand-in that produces values, while the canonicalization, domain and hash around it are real — the same precedent as #24's never-compiled Rust and #47's `fake_verifier.py`.

An external review found three gaps, all in how strictly "canonical" was actually enforced. **High:** `is_canonical_decimal` was a shape-only pattern check — "10" and "1e1" both matched it, even though `canonical_number()` would only ever produce "10" for the value ten. Two producers computing the identical float could therefore emit two different, individually valid strings for one number, and `distinct_values` counted raw strings rather than the numbers they denoted — reproduced with a constant grid, half its cells spelled the shortest way and half with a redundant exponent, `distinct_values: 2`. Fixed with `is_canonically_encoded()`: not a looser pattern but a round-trip check — parse the string back to a float, re-encode it, and require the result to match the original verbatim, which is exactly what "shortest string that round-trips exactly" already guarantees for genuine `canonical_number()` output and refuses for anything else. `compute_value_domain` now requires every value to pass this stronger check (raising, not merely miscounting) and computes `distinct_values` over the parsed `Decimal`s rather than the raw strings, as defense in depth once the check above already guarantees a one-to-one spelling. The same review named a second, narrower precision hazard in `canonical_number()`'s own `Decimal` input path: `float(Decimal('1e-400'))` is exactly `0.0` with no exception — a genuinely nonzero value silently becoming a different value (zero), not merely a less precise spelling of the same one. Ordinary precision loss beyond float64's ~17 significant figures is that downcast working as designed (this scheme is scoped to f64-precision measured values) and is deliberately left alone; only the zero-underflow case, where the encoded value no longer denotes the same *fact* at all, now raises.

A second review pass found `is_canonically_encoded()` itself broke the one case it most needed to protect: `canonical_number()`'s `int` branch preserves an integer *exactly* past float64's ~2**53 ceiling (`canonical_number(2**53 + 1) == "9007199254740993"`), but the round-trip check reparsed every string through `float()` unconditionally — `float("9007199254740993")` rounds to `9007199254740992.0`, so the function rejected output `canonical_number()` had only just produced, and `build_result(..., encode_scalar(2**53 + 1))` raised. Fixed by matching the dispatch it is meant to verify: a plain integer spelling (no `.`, no `e`) is reparsed as an `int`, the same branch `canonical_number()` itself takes, never touching `float` for a value that branch exists specifically to keep exact.

**Medium:** `canonicalization.hashed_fields` was schema-shaped as "any subset of six field names, at least one" but `canonical_payload`/`compute_value_hash` always hashed the full hardcoded set regardless of what a document declared — so a document could claim `hashed_fields: ["result"]` and still pass validation, because its `value_hash` (computed by every real producer over all six fields) matched the recomputation anyway. The declared list was decorative, not enforced — the opposite of "self-describing." Fixed: the field list is not read as a per-document instruction (the module's own design is one fixed rule, `canonical-json-v1`, versioned as a whole); `canonical_payload` now checks a declared list against the rule's actual field set and raises on disagreement, with `validate_witness.py` carrying the same check ahead of it so a mismatch produces a clean `Finding` rather than an uncaught exception — the identical two-layer pattern `compute_value_domain`'s own encoding check already used.

**Medium:** grid cells were checked for uniqueness and sort order but never against the grid's own declared `rows`/`columns` — a schema-valid 1×1 result could carry a cell at `(9, 0)` with zero findings. `encode_grid()` (the Python producer helper) already refused this for its own callers, but nothing enforced it against an arbitrary on-disk result file, which is exactly the surface a validator exists to guard. `validate_result_data` now rejects any cell whose `(row, column)` falls outside `[0, rows) × [0, columns)`.

### 16.2 Gates — G18 / G19 / G20

- **G18 — witness coverage.** Every query marked `witness_required` has a witness spec and a generated rendering. Stage 4, hard error.
- **G19 — witness determinism.** Regenerate, compare `value_hash` — never `render_hash`. Stage 8A / CI, hard error. Cheap and worth its own gate: determinism is a real contract on the renderer, not a heuristic.
- **G20 — degenerate witness, against the declared `expectation`, not a global heuristic.** "All-constant output" is correct for a single-cell fixture and a defect for a full-field one — a global anomaly detector both false-positives and false-negatives. Check instead: `renderer_actual` ≠ declared `renderer` → **hard error at generation time**, not this gate — a renderer must never silently fall back to a degraded rendering (e.g. a text/table strip standing in for a field plot); it must fail loudly the moment it cannot handle the declared shape. `value_distribution: must-vary` while output is constant → fail. `coverage_region` inconsistent with `fixture_family` → fail. Everything else here is Stage 4.5, warn, block at promotion if unresolved — this is what turns a by-eye contact-sheet catch into a mechanical one, and it is the only thing that distinguishes intentional partial coverage (a feature only defined along a path) from fixture drift.

The "hard error at generation time" half is implemented (chainlink #28) as `scripts/witness_renderer.py` (the contract) + `scripts/generate_witness.py` (the entrypoint), wired as `pipeline.py render-witness <witness_id> --renderer <name>`.

**G18 itself is implemented** (chainlink #29) as `scripts/gate_g18.py`, wired as `pipeline.py gate-g18`. `witness_required` is read directly off a query's own JSON field, requiring the literal value `true` — the same untyped, direct-field-access discipline `validate_witness.py`'s own G2 already applies to `pure`, and the reason chainlink #33's still-pending upstream schema change (`witness_required` is not yet in the vendored `concept-to-code` schema) is not a blocker here: nothing in this codebase, including `emit_stubs.py` itself, ever runs concept-to-code's JSON Schema validator against a concept spec document at all. Discovery mirrors `validate_witness.py`'s own `resolve_query` scan (same underscore-directory skip, same sorted walk) rather than re-deriving it, factoring the shared `rust_sig` → name extraction (`query_name_from_sig`) out of `resolve_query` so the two can never silently disagree about what a query is even called. Coverage is checked against the "genuinely valid, not just present" bar everywhere else in this plan: a witness spec that exists but fails G1a/G1b/G2 does not count, and a valid spec whose declared `output.path` names a file never actually written (checked by plain existence, trustworthy only because `generate_witness.py`'s own atomic-write contract from #28 guarantees that path is either the complete rendering or absent, never partial) does not count either. Two ambiguity cases are refused rather than resolved from whichever sorted first, the identical "ambiguous, never first-wins" choice chainlink #25 already made for a cross-crate `bridge_id` collision: the same `(concept, query)` declared `witness_required: true` in more than one concept spec, and a genuinely valid witness resolving to the same `(concept, query)` found under more than one crate's `_witnesses/` directory.

An external review found the second ambiguity case was computed too early: `collect_valid_witnesses` raised it for every genuinely valid witness in the workspace, before its result was ever narrowed to the declared feature set — reproduced with zero declared features (`witness_required` false or absent everywhere) still returning a "more than one crate" error for two crates that each happened to hold an ordinary, unrelated duplicate witness. That contradicts this gate's own stated scope and the coverage sentence above: an undeclared query must stay invisible to G18, ambiguity findings included, exactly as R2 stays invisible to an undeclared interaction. Fixed by threading the declared key set into `collect_valid_witnesses` and only raising the cross-crate finding for a `(concept, query)` pair that is actually declared.

**G19 is implemented** (chainlink #30) as `scripts/gate_g19.py`, wired as `pipeline.py gate-g19`. Unlike G18, it is not scoped to the declared `witness_required` set — every genuinely valid, promoted witness spec carries its own `determinism.value_hash` claim and every one of them is checked, the same way G9 checks every promoted bridge rather than only the ones a manifest references. `render_hash` is never read anywhere in this module; a regression test proves a changed `render_hash` alone never blocks.

An external review found the first version false-passed two ways, both high severity. **First:** that version compared a witness spec's declared hash against whatever canonical result already sat at `ci/results/witnesses/<witness_id>.json`, on the premise that regeneration happened "upstream" the way verifier dispatch is external to G9. Nothing enforced that premise — if a producer's real behavior drifted but CI never re-ran it, the stale file still matched the promoted hash and the gate passed regardless; an unenforced upstream step establishes nothing. **Second:** the join was on `witness_id` alone, so a spec's `fixture_id`, `seed`, `concept` or `query` could change while its declared `value_hash` stayed untouched, and a stale result carrying the OLD identity would still be accepted as the current one. Fixed by making regeneration unconditional and internal to the gate rather than an assumed precondition of it: `witness_backend` (project descriptor, mirroring `verifier_backends`'s pluggable-command shape exactly) names one command, and `gate_workspace()` now dispatches it FRESH on every run, for every genuinely valid witness spec, passing that spec's own current `witness_id`/`concept`/`query`/`fixture_id`/`seed`/`renderer` as the invocation's arguments — so a regenerated result's identity is constructed from the current spec, not read back from a possibly stale one. With no backend configured, nothing was actually re-evaluated this run and the gate blocks, the identical posture G9 already takes when no `verifier_backends` entry exists. As defense in depth against a backend that ignores its own arguments, the regenerated document's identity fields are compared against the spec explicitly before its hash is trusted at all — dispatch alone cannot rule out a misbehaving backend, which is why this is a separate, explicit check with its own regression test. This gate deliberately does not persist the regenerated result to `ci/results/witnesses/` as a side effect: gates in this codebase check, they do not mutate artifacts, the same line `gate_g9.py`'s own `gate_workspace()` draws against `check_bridges()`. Nothing here re-hashes anything either: `validate_witness.py`'s own `validate_result_data` still applies the same G1a/G1b bar to the freshly regenerated document that `load_results_by_witness` applies to a stored one.

A third, medium-severity finding in the same review: the cross-file ambiguity check compared only the set of owning CRATE NAMES, so two independently valid witness files declaring the same `witness_id` in the SAME crate (reachable when they cover different queries, each with its own correctly-matching filename, since the filename is derived from `(concept, query)` rather than `witness_id`) fell through to whichever sorted first instead of being flagged. Fixed by comparing the count of valid FILES sharing a `witness_id` rather than the count of crates they happen to span — the same choice #29 and #25 already make for their own cross-crate collisions, just no longer blind to a same-crate one.

A second review pass found two further gaps, both medium severity. The backend subprocess ran in the CALLER's own directory rather than the selected workspace, so invoking `pipeline.py --workspace /elsewhere gate-g19` from a different cwd left a real producer unable to reliably resolve fixture/source files relative to the workspace it was meant to inspect, or resolving them against whatever checkout happened to be underfoot instead — fixed by threading `workspace` into `regenerate_witness()` and invoking with `cwd=workspace`. Separately, the identity check compared `witness_id`/`concept`/`query`/`fixture_id`/`seed` but not `renderer_actual` against the requested `renderer` — a real gap, not an oversight of a minor field: `renderer_actual` is deliberately EXCLUDED from `value_hash` by §16.1's own rule ("the same numbers from a different renderer are the same fact"), so nothing in the hash comparison alone could ever have caught a backend that silently ignored `--renderer` and reported a different, still schema-valid renderer instead — exactly the declared/actual mismatch §16.2 makes a hard error at generation time for chainlink #28's `generate_witness.py`. Fixed by adding `renderer_actual` to the same explicit identity comparison the other fields already go through.

**G20 is implemented** (chainlink #31) as `scripts/gate_g20.py`, wired as `pipeline.py gate-g20`. Two checks, both WARN severity per §12's gate table (not the "→ fail" wording §16.2's own prose uses for the underlying defect — the table is authoritative on the gate's own reporting tier): `expectation.value_distribution: must-vary` but the current genuinely valid canonical result measures only one distinct value, and `coverage_region` inconsistent with `fixture_family` workspace-wide. The gate table's own "block at promotion if unresolved" names a promotion-time enforcement point that does not exist yet for witness specs — `specs/_witnesses/` is not a recognized artifact type in `pipeline.py`'s own `_select_validate_fn`/`approve()` dispatcher — so this gate's job today is to make the finding impossible to miss rather than to actually gate a promotion step: it exits with a THIRD, distinct code, mirroring `gate_r1_g16.py`'s own `EXIT_DECISION_REQUIRED` tier for the identical "not blocked, but never silently passed either" shape, rather than collapsing a real defect into a clean 0 or the same code a genuine block uses. The fixture-family check reuses `validate_witness.py`'s own `check_family_consistency` rather than reimplementing it — that function already existed, described in §16.1 as implementing exactly this requirement, but was only ever exercised by `validate_crate()`'s own crate-scoped hard-error G1b pass, which neither G18's nor G19's own witness collection calls (they validate one document at a time); G20 is the first gate to actually run it, workspace-wide rather than per-crate since a `fixture_family` name carries no crate namespace. `collect_valid_witness_specs` (ambiguity handling included) is imported directly from `gate_g19.py` rather than re-implemented a third time, the same cross-gate-module reuse precedent `gate_g9.py` already set by importing `load_bridges`/`load_manifests` from `gate_g14.py`; an ambiguous `witness_id` still blocks outright at ERROR severity here, since it is a structural identity problem rather than a degeneracy one.

An external review found three further gaps, two high severity, one medium. **First:** the must-vary check joined a spec to its canonical result solely by `witness_id`. `load_results_by_witness()` proves a result is self-consistent, never that it is ABOUT the spec it is being compared against — a stale result left over from an earlier `fixture_id`/`seed`/`concept`/`query` revision, still self-consistent and still keyed under the same `witness_id`, could make a currently-constant witness pass by borrowing an old, unrelated result that happens to vary. Fixed by factoring `gate_g19.py`'s own spec/result identity comparison into a standalone `identity_mismatches()` (witness_id/concept/query/fixture_id/seed/renderer_actual), reused by both gates rather than re-derived: G19 applies it to a freshly regenerated document, G20 now applies the identical check to a stored one before trusting its `value_domain` at all. **Second:** "block at promotion if unresolved" was asserted but not built — `specs/_witnesses/` was not a recognized artifact type in `pipeline.py`'s `_select_validate_fn`/`approve()` dispatcher at all, so no promotion step existed for G20's findings to plug into, and `EXIT_WARN` alone cannot BE that lifecycle (ordinary CI treats any non-zero exit as a failure, or a caller that tolerates it never blocks anything). Fixed by wiring witnesses into `approve()`: a new `gate_g20.validate_witness_for_approval()` is the `validate_fn` `_select_validate_fn` now returns for `<crate_dir>/specs/_witnesses/*.json` (the first branch there that needs the WHOLE workspace, not just the one document, since G20's checks are inherently cross-witness the same way G9/G14/G18/G19 are — `_select_validate_fn` already has `workspace`/`descriptor` in scope and the returned closure simply captures them). It runs G1a/G1b/G2 first (a degeneracy check on a schema-invalid or dangling witness isn't meaningful), then G20's own checks, and promotes from WARN to ERROR the findings naming the witness actually being promoted — `approve()`'s existing "refuse on any error" rule then does the real blocking, unmodified. "Warn early, block at promotion" is the identical finding at two severities depending on when it is checked, not two mechanisms that could silently drift apart. **Third:** `validate_witness.py`'s own `validate_crate()`/`validate()` still called `check_family_consistency()` directly, hard-failing a same-crate fixture-family disagreement as G1b/error at ordinary Stage 4 validation — before G20 could ever report the identical defect as its own Stage 4.5 warning. One defect cannot carry two disagreeing dispositions. Fixed by removing both call sites; `check_family_consistency()` itself stays (G20 is its only caller now, workspace-wide).

A further review pass found the promotion wiring above was itself not candidate-aware, high severity, reproduced end to end: `validate_witness_for_approval()` ran `gate_workspace()`, which scans whatever is already on disk at `path` — for a FIRST-TIME promotion there is nothing there for this `witness_id` yet, so the candidate was invisible to the scan and a must-vary-over-constant witness was approved with exit code 0 (the one existing integration test only exercised RE-approving an already-promoted witness, so it never caught this). For an update, the scan checked the STALE on-disk version rather than the candidate actually being promoted. Fixed by constructing the PROSPECTIVE post-promotion witness set explicitly — every other genuinely valid witness currently on disk, with the candidate replacing (update) or inserting as (first-time) its own `witness_id` entry — and checking the candidate specifically against that set: must-vary against its own (unaffected) canonical result, and a new `check_candidate_fixture_family_consistency()` for the family check, which compares the candidate directly against every peer rather than reusing the pairwise `check_family_consistency()`/`check_fixture_family_consistency()` scan, whose own shape only ever flags whichever witness sorts SECOND in a disagreeing pair — a candidate that happened to sort first would otherwise escape it silently regardless of the fix above.

A second review pass on that same fix found it still built the prospective set by collapsing to a `witness_id`-keyed dict BEFORE substituting the candidate — `specs[witness_id] = data` silently overwrote whatever OTHER file already occupied that dict key, so a first-time candidate reusing an unrelated peer's `witness_id` erased that peer from the prospective model instead of colliding with it (reproduced end to end: a new `task_queue.depth.json` declaring the SAME `witness_id` as the already-promoted `task_queue.load_factor.json`, with `constant-allowed` so the must-vary check itself never tripped, approved with exit code 0, leaving two promoted files sharing one supposedly workspace-unique id). The inverse also caused false rejection: an existing file changing its own `witness_id` on re-approval left its OLD on-disk identity as a "ghost" peer, since nothing removed the STALE entry that substitution-by-witness_id never touched. Fixed by building the prospective set BY PATH instead: `gate_g19.py`'s `collect_valid_witness_specs()` is now built on top of a new, lower-level `collect_valid_witness_entries()` returning `(path, data)` pairs with no witness_id collapsing at all; `validate_witness_for_approval()` excludes whatever entry's path equals the promotion target (correctly dropping an update's stale content regardless of what witness_id it declared), appends the candidate at that path, and only THEN checks uniqueness directly against the path-substituted set (`check_candidate_uniqueness()`) — before any witness_id-keyed collapsing happens, since collapsing first is exactly what made the collision invisible.

**The contract is written as code, not policy.** Every renderer function either produces an SVG matching the result's `kind` or raises `RendererError` naming exactly why — there is no third branch anywhere in `witness_renderer.py` that says "if I can't do a field plot, draw a table instead." That branch is the exact failure this section forbids, and it is not merely disallowed by convention; it does not exist to be reached, the same total-or-rejecting shape chainlink #47 gave the bridge-logic compiler. Three renderers cover the three canonical result kinds `docs/witness-result-schema.json` closes over: `scalar_svg`, `series_svg`, `scalar_field_svg` (the one plan.md's own worked example names — a grid, since `load_factor` is witnessed across a fixture, not called once).

**Two structural defenses stop a mismatch from ever reaching disk, which is what makes `renderer_actual` worth checking later.** First, dispatch calls *exactly* the function registered under the declared name — no "closest match," no default. An unregistered name is refused before any rendering is attempted. Second, every renderer reports its own identity as a literal constant in its own body, not by echoing the name it was called under; the dispatcher cross-checks the two and raises on disagreement. This catches the one bug class kind-checking cannot: a registry wired to the wrong function. `renderer_actual` in a written artifact is therefore an independent observation of what ran, not an echo of the request — which is what makes G20's later declared/actual comparison (#31) a real check rather than a tautology.

**Generation writes nothing on failure — not a partial file, not a degraded picture under an honest label.** `generate_witness.py` reads the already-validated canonical result at `ci/results/witnesses/<witness_id>.json` (chainlink #27's own "genuinely valid, not just present" bar), dispatches it through the renderer contract, and only touches disk after the renderer has already succeeded. It never mutates a witness spec: a spec is normative and reviewed, and a generator quietly rewriting `output.render_hash` on a promoted spec would edit around review the same way a model authoring its own `review` block would. Chronology follows from this: the canonical result and its rendering are produced FIRST, then a witness spec draft is authored quoting both `output.render_hash` and `output.renderer_actual` — the schema's own `required` on that block presupposes generation already happened.

No graphics library: this codebase's only runtime dependency is `jsonschema` (`requirements.txt`), and `render_hash` is deliberately non-normative (never gates promotion), so nothing here needs to survive a renderer-library version the way `value_hash` must not. SVG is fixed string templates over the canonical result's own decimal strings; any additional numbers a template needs (bar heights, cell shading) are computed with `Decimal` and formatted to a fixed width, never with `canonical_number()`'s round-trip encoding — that guarantee belongs to the normative hash alone. A sparse grid (plan.md §16.1's "a feature defined only along a corridor") renders its missing cells with an explicit not-covered hatch, never as a blank that could be mistaken for a zero value.

An external review of #28 found two gaps in the fail-loud contract as first landed, both fixed. **High:** `generate_witness.py` dispatched on the CLI's requested renderer alone, never checking it against the canonical result's *own* `renderer_actual` (`docs/witness-result-schema.json` requires that field precisely so "a result found on its own must still be able to say what made it"). A schema-valid result self-recorded as `series_svg` could be rendered successfully by a requested `scalar_field_svg` whenever the two happened to share a compatible shape, leaving two on-disk records of "what actually ran" that contradict each other — exactly the self-describing guarantee that field exists to prevent losing. `generate()` now rejects any disagreement between the requested renderer and the result's own claim *before* attempting to render, with its own `GenerationError`, distinct from a `RendererError` shape mismatch. **Medium:** the SVG was written directly to its final path (`destination.write_text(...)`); an I/O failure partway through (disk full, process killed) could leave a truncated file sitting where a previously good rendering used to be, breaking "failure means no mutation" at the I/O layer even though the logic layer already honored it. `_write_atomically()` now writes to a temporary sibling in the same directory (same-filesystem, so the final step is a rename, not a copy), flushes and fsyncs it, then replaces the destination with `os.replace` — a reader observes only the old complete file or the new complete file, never a mixture, and a failure at any point leaves neither a stray temp file nor a mutated destination.

### 16.3 Contact sheet — Stage 4.5 review surface

Same role as any other generated diagram: read-only, never editable, derived from normative inputs. It is what a human actually looks at at the moment of promotion — which is exactly why it needs two fixed controls, not because it is unreliable but because a grid of uniformly green panels reads as proof whether or not it is one:

1. A fixed banner: **∃-witness evidence — not verification.**
2. Each panel carries the owning obligation's `assurance_status` and the cluster's `closure_kind` beside it. A reviewer must see "witness green / assurance `unsupported`" side by side — coverage must never be readable as proof by omission.

**Implemented** (chainlink #32) as `scripts/generate_contact_sheet.py`, wired as `pipeline.py generate-contact-sheet`, writing `docs/witnesses/_contact_sheet.svg`. One panel per DECLARED feature, in the identical order `generate_feature_ledger.generate_ledger()` itself produces (sorted by `(concept, query)`) — this module computes no witness/determinism/degeneracy/assurance/closure fact of its own; every one of those six values is read verbatim off a fresh `generate_ledger()` call (the same function `write_ledger()` uses, never a stale `ci/results/feature_ledger.json`), and the generated ledger is schema-validated (`generate_feature_ledger.load_validator()`, reused directly) before a panel is ever drawn over it — the same "genuinely valid, not just present" bar `write_ledger()` already applies before writing one to disk.

**The two-axis discipline becomes a rendering rule, not a new computation.** `witness_present`/`implementation_observed`/`determinism`/`degeneracy` are drawn as colored traffic-light pills (green/amber/red/grey). `assurance_status` and `closure_kind` are drawn in a completely disjoint, fixed, non-traffic-light palette (a bordered neutral box, `#eef1f7` fill, `#1a237e` stroke/text) with an explicit `assurance:`/`closure:` text prefix — **never colored green, regardless of the value shown.** This is the concrete visual mechanism behind §16.3's own rule: a fully green witness panel cannot be scanned as "assurance established" by someone reading colors rather than words, because the assurance/closure column is structurally incapable of ever rendering in the traffic-light palette. `implementation_observed: true` beside `assurance_status: unsupported` (today's only value — §16.4) is therefore the everyday case, not a special one, and a regression test asserts the axis box's own `fill` never matches the green/amber/red pill colors in the same panel.

**Each feature's witness rendering is embedded, not linked — and rendered from the same fresh evaluation the status columns come from, never a blind read of whatever sits on disk.** An external review found this was not true of the first version: it read and embedded `docs/witnesses/<concept>.<query>.svg` directly off disk whenever `witness_present` was true, high severity, reproduced two ways — a currently-passing witness whose on-disk SVG was stale or entirely unrelated to what the fresh regeneration actually measured, embedded as-is beside an honestly green status row with nothing to say otherwise; and a currently-passing witness whose `output.path` did not even contain valid SVG, embedded as-is with no placeholder. Fixed by sharing ONE evaluation pass between the ledger and the contact sheet rather than each computing (or worse, re-dispatching) its own: `generate_feature_ledger.py` now exposes `evaluate_ledger()`, returning a `FeatureEvaluation` per declared feature that carries the witness spec AND the exact freshly regenerated canonical result (`document`) `check_witness_determinism` produced — the SAME document determinism/degeneracy are computed from — with `generate_ledger()` itself now a thin wrapper unpacking just the `entry` half. The contact sheet's thumbnail is then, in order: (1) when a fresh `document` exists, RENDERED FRESH from it via `witness_renderer.render(witness["renderer"], document["result"])` — the identical dispatch chainlink #28's own `generate_witness.py` makes, over data already in memory, so this never re-invokes the external `witness_backend` a second time; `identity_mismatches()` already guarantees `document["renderer_actual"] == witness["renderer"]` before `document` is ever non-`None`, so this is never a declared/actual mismatch by construction, and a `RendererError` regardless falls back to the placeholder; (2) only when NOTHING was checked at all this run (`document is None` AND `determinism: not-checked` — i.e. no `witness_backend` configured) does it fall back to the on-disk file; (3) `witness_present: false`, no witness, or every case above fails: the explicit dashed-border "rendering unavailable" placeholder, panel never dropped.

A second review round found two further gaps in step (2), both fixed. **Medium:** the on-disk fallback's own validity check was `root.tag.endswith("svg")` — well-formed XML alone is not "valid SVG," and `endswith` is not even a correct namespace check: `<notsvg/>` and `<x:foosvg xmlns:x="urn:not-svg"/>` (a foreign namespace whose local name merely happens to end in "svg") both parsed cleanly and both matched. Fixed by requiring the parsed root's tag to be an EXACT match — either the real, namespaced `{http://www.w3.org/2000/svg}svg`, or an unprefixed, no-namespace `svg` (the latter exists only for foreign input; every SVG this pipeline's own `witness_renderer.py` produces already declares the namespace). **Low:** the fallback actually fired for EVERY `document is None` case — including a checked-and-FAILED regeneration (a real dispatch failure, an invalid result, or a mismatched identity, all `determinism: fail`) — contradicting the "only when no backend is configured" contract stated above, which an earlier version of this very paragraph asserted without the code actually enforcing it. Fixed by gating the fallback on `determinism == "not-checked"` specifically (not merely `document is None`): a backend that was actually invoked and could not be trusted this run now gets the SAME placeholder a missing rendering gets, rather than silently reusing whatever rendering happens to already sit on disk from some earlier, possibly different, run. A missing/failing regeneration still leaves `determinism`/`degeneracy` honestly `not-checked`/`fail`; only a genuine no-backend workspace's EXISTING, exactly-validated on-disk rendering still displays.

**Never a promotion input**, identically to the feature ledger (§16.6): nothing in `pipeline.py`'s `approve()`/`_select_validate_fn` dispatcher recognizes `docs/witnesses/_contact_sheet.svg` as a normative artifact type, and this module writes to no path that dispatcher does recognize.

**Deterministic bytes.** Panel order follows `generate_ledger()`'s own sorted order, never filesystem discovery order; every layout constant is a fixed integer; the only variable-length inputs (escaped identity/status text, and each embedded witness SVG's own bytes) are concatenated verbatim, never reformatted — base64 is a pure function of bytes, and the embedded witness SVGs are themselves byte-deterministic (`witness_renderer.py`'s fixed templates). No timestamp or other environment-dependent value appears anywhere in the output; two calls over identical workspace state produce byte-identical SVG text. Atomic write (`atomic_write.write_atomically`, chainlink #28's own guarantee) — a failed generation leaves the previous complete contact sheet untouched and no temporary file behind. XML/SVG text escaping is factored into a new shared `scripts/xml_escape.py` (chainlink #32), reused by both this module and `witness_renderer.py` (which previously carried its own private, now-superseded copy) — the same "one module imports another's helper rather than re-deriving it" precedent `atomic_write.py` already set.

### 16.4 Feature ledger — the tracking artifact

A **generated projection**, same tier as the contact sheet: read-only, never editable, derived from normative inputs. Not a new normative file — the normative requirement lives in policy (16.5), enforcement lives in G18–G20, this is visibility only.

```yaml
# ci/results/feature_ledger.json — GENERATED, READ-ONLY
generated_from:
  concept_specs_hash: sha256:...
  witness_specs_hash: sha256:...
  assurance_results_hash: sha256:...
features:
  - feature: TaskQueue.load_factor
    witness_required: true
    witness_present: true          # G18
    implementation_observed: true  # a witness rendered without panic -> body executes on the fixture
    determinism: pass              # G19 (value_hash)
    degeneracy: ok                 # G20 disposition
    assurance_status: unsupported  # from the obligation's assurance record
    owning_cluster: scheduling
    closure_kind: n/a
```

**The rule that keeps it honest — two columns, never merged:**

- `implementation_observed` = the body **executes** on the fixture and yields a stable value. A weak liveness fact, genuinely useful: it distinguishes an implemented feature from an `unimplemented!()` stub, cheaply, because a stub cannot render a stable witness. Usable as an *orchestration signal* — a work package whose features lack a green witness are not yet implemented (§15's advisory-first open item).
- `assurance_status` = the **contract** is established (`unsupported | tested | bounded | deductive`). A green witness proves the body runs on one fixture; it proves nothing about correctness, completeness over all inputs, or contract conformance.

Never write `implementation_observed: true` as `implementation-proved` in an assurance record. Exactly the same discipline as `closure_kind` vs. coverage elsewhere in this plan: two dimensions, shown side by side, never collapsed into one bit.

**Implemented** (chainlink #34) as `docs/feature-ledger-schema.json` + `scripts/generate_feature_ledger.py`, wired as `pipeline.py generate-feature-ledger`. One entry per DECLARED feature (`witness_required: true`, chainlink #29's own scoping, reused not re-derived: `gate_g18.collect_declared_features`) — an undeclared query stays invisible to the ledger exactly as it stays invisible to G18.

**Every column is read from the gate that already owns it, computed fresh every run — never a cache of a stale one.** `witness_present` comes from `gate_g18.gate_workspace()`'s own per-feature disposition (genuinely valid witness, rendering exists on disk, unambiguous). `determinism` and `degeneracy` are NOT read from `gate_g19.gate_workspace()`/`gate_g20.gate_workspace()`'s own aggregate, on-disk-result-based scans (see the external-review fix below for why) — instead, per declared feature, the ledger calls `gate_g19.check_witness_determinism()` directly, which dispatches the SAME real regeneration through the configured `witness_backend` (chainlink #30) that G19 itself trusts, and feeds the resulting in-memory document straight into `gate_g20.check_must_vary()` and `gate_g20.check_candidate_fixture_family_consistency()` — no gate ever re-reads a stored result off disk for the ledger's purposes. `determinism`/`degeneracy` are `not-checked` (never `ok`/`pass` by default) when there is no witness to check at all, and both are specifically `not-checked` rather than `fail`/`ok` when no `witness_backend` is configured (or regeneration failed to produce a trustworthy document at all) — nothing was actually re-evaluated this run, a different fact from a real mismatch, distinguished by matching G19's own "no witness_backend configured" finding text. `implementation_observed` requires ALL three clean (`witness_present` and `determinism: pass` and `degeneracy: ok`) — the strict reading this issue's own requirements name, and the reason a stored result is never trusted merely because its `witness_id` matches (chainlink #30/#31's own hard-won lesson, inherited here by construction since it reuses the same gates rather than re-deriving their identity checks).

**An external review of the first implementation found three reporting gaps, one high severity, two medium, all fixed in the same round.** *First (high):* the original ledger fed `gate_g20.check_must_vary()` an on-disk `load_results_by_witness(workspace)` scan, independent of G19's own live regeneration — a witness with no stored result on disk (or a STALE stored result left over from an earlier, still-varying run) made `check_must_vary` silently skip with no finding, which the ledger then read as `degeneracy: ok`, even though nothing about the current implementation was ever actually checked. Fixed by factoring `gate_g19.gate_workspace()`'s own per-witness loop body out into `gate_g19.check_witness_determinism()` (returns the regenerated document itself, not just findings, whenever that document is genuinely valid and its identity matches the current spec), and feeding that SAME fresh in-memory document into `gate_g20.check_must_vary()` from the ledger — a stored result on disk is no longer consulted for this purpose at all, closing the hole by construction rather than special-casing "no stored result." *Second (medium):* `generate_ledger()` discarded `collect_declared_features()`'s own ambiguity findings, iterating only its collapsed map — two concept specs declaring the same required feature therefore produced an empty ledger silently, with the CLI reporting "nothing to check" rather than a declaration conflict. Fixed by raising `GenerationError` immediately whenever any ambiguous-declaration finding is present, before any feature is examined — generation now fails loudly rather than ever silently omitting a declared feature. *Third (medium):* the fixture-family/coverage_region consistency check used `gate_g20.check_fixture_family_consistency()`, a pairwise workspace-wide scan that (by construction, see its own docstring) only ever flags whichever witness_id sorts lexicographically SECOND within a disagreeing family — the other member of the same conflicting family read as fully clean, and renaming a witness_id could change which one. Fixed by switching to `gate_g20.check_candidate_fixture_family_consistency()` (already factored out during chainlink #31's promotion-wiring work for exactly this reason), called once per declared feature against the full genuinely-valid witness set — every member of a disagreeing family now gets its own independent, sort-order-independent, non-clean disposition.

**`owning_cluster` and `closure_kind` are read directly from already-existing artifacts, not re-derived or recomputed.** `owning_cluster` is the concept spec's own top-level `cluster` field — confirmed already domain-agnostic and required by `vendor/concept-to-code/schemas/spec.schema.json` (`docs/concept-to-code-modifications.md`'s own "already compatible, no change needed" finding for gap-adjacent fields). `closure_kind` is the cluster's genuinely valid closure profile's own declared value (`validate_closure.load_cluster_artifacts`, the same "genuinely valid, not just present" bar every cross-reference in this pipeline applies), or `n/a` when none exists — a DECLARATION read, not a live G14 closure recomputation, which would be a much heavier dependency (the full manifest/provider/bridge graph) this generated projection does not need and §16.6 does not ask for.

**`assurance_status` is always `unsupported` today, and this is the honest answer, not a placeholder.** A witnessed feature is a concept-to-code QUERY (`<Concept>.<query>`, a snake_case function name); an obligation this pipeline's own layer 3 tracks is a CONSTRAINT (`<Concept>.C\d+`, `docs/achieved-assurance-schema.json`'s own `obligation_id` pattern) — a different concept-to-code `$def` entirely, and nothing in either schema, or anywhere else in this codebase, names a field linking one to the other. There is currently no mechanical way to resolve "the obligation this query's feature is establishing assurance for." `resolve_assurance_status()` is still written as a real resolution function, not a hardcoded constant, so a future linking convention has exactly one place to plug in; `tested`/`bounded`/`deductive` are already valid per the schema (a behavior change later is not a schema change).

**`generated_from`'s three hashes** are sha256 over a canonical-json-v1-style serialization (sorted keys, compact separators, matching `scripts/witness_result.py`'s own discipline) of every input artifact actually consulted, sorted by workspace-relative path so the hash never depends on filesystem discovery order: `concept_specs_hash` over every concept spec owning at least one declared feature; `witness_specs_hash` over every genuinely valid witness spec workspace-wide (`gate_g19.collect_valid_witness_entries`, reused); `assurance_results_hash` over every genuinely schema-valid work-package assurance report reachable from a schema-valid manifest's own `report.emit` path (`gate_g14.load_manifests` + its own report validator).

**Atomic write, refuse-on-invalid.** `write_ledger()` validates the generated ledger against `docs/feature-ledger-schema.json` before writing anything — a generated artifact this codebase's own schema rejects must never reach disk — and writes via `atomic_write.write_atomically()` (factored out of `generate_witness.py`'s own `_write_atomically`, chainlink #28, rather than re-derived a second time): a failure partway through leaves the previous ledger byte-identical and no temporary file behind.

### 16.5 Consequences

- **Promotion manifest.** Witness specs and `value_hash` join `artifact_manifest` (§7.1) — a fixture change or a value change invalidates the receipt, correctly, since the witness was part of what was reviewed. `render_hash` does **not** gate promotion.
- **`protected_write_set` + `gate_integrity`.** The renderer is gate-adjacent code — hash-pin it (§10, §13). An implementing agent that can edit the renderer can make a wrong feature look right.
- **Mitigation kind.** `witness` is a typed mitigation (§12) acceptable at risk tier **`low` only** — it is an example, one fixture wide. Prefer a property test where the feature is a pure scalar query; it is nearly as cheap and strictly stronger. Order: witness < example-test < property-test < proof.

**Implemented** (chainlink #35), in two independent modules over two independent normative artifacts:

**Promotion receipt.** `scripts/generate_promotion_receipt.py` and `scripts/validate_promotion_receipt.py` (§7.1) recognize a canonical crate-scoped witness spec (`<crate_dir>/specs/_witnesses/*.json`) as a real `artifact_manifest` kind, discovered the identical way G18/G19 already do — `gate_g18.collect_declared_features`/`collect_valid_witnesses` for the declared, ambiguity-resolved feature set, `gate_g19.collect_valid_witness_entries` to recover each one's on-disk path — never a new glob or a first-wins resolution. `validate_promotion_receipt.required_witness_paths()` computes the exact set a given promotion must include: every declared (`witness_required: true`) feature belonging to the promotion's own `cluster` and having its own genuinely valid, unambiguous witness spec. Both `accept_promotion()` and the validator's own `check_required_witnesses()` call this ONE shared function — an external review, high severity, found an earlier version existed only in the generator, so a receipt that started complete but was later hand-edited to drop a required witness entry produced zero findings on validation, even with a descriptor supplied; sharing the computation closes that gap by construction rather than by keeping two copies in sync. Fails closed (`PromotionReceiptError` from generation, a `Finding` from validation) when any required witness is missing from the accepted set, when a declared feature has no valid witness at all, or when declaration/witness resolution is itself ambiguous — the identical fail-closed set plan.md's own "missing / invalid / ambiguous / outside its canonical directory" names.

Scoped to the promotion's own `cluster` (external review, medium severity: an earlier version was workspace-wide, so a promotion for one cluster wrongly required witnesses belonging to unrelated clusters). A concept spec's own top-level `cluster` field is required by vendor/concept-to-code's schema. Cluster attribution itself fails closed (`RequiredWitnessError`) rather than reusing `generate_feature_ledger.owning_cluster_for()` directly — a second review pass, high severity: that function's own honest `"unknown"` fallback for a missing `cluster` field is the right answer for a display-only reader with nothing to gate on it, but the wrong answer here, since it would silently exclude a declared feature from every cluster's required set (this pipeline never runs concept-to-code's own JSON Schema validator against a concept spec, so a missing `cluster` — or an unreadable concept spec — is a real, reachable state: the reviewer's own repro removed `cluster`, then removed the witness from the accepted set, and got zero findings).

A third review pass, high severity, found the fix itself still accepted any non-empty string: a concept spec declaring `"cluster": "SCHEDULING"` (out of grammar, but non-empty) passed, and since `docs/promotion-receipt-schema.json`'s own `cluster` field is pattern-constrained to `^[a-z][a-z0-9-]*$`, no schema-valid receipt could ever equal it — the feature was silently excluded from every promotion's required set forever, the identical bug reached through a different input shape. Fixed with `CLUSTER_PATTERN` (the same grammar, defined once in `validate_promotion_receipt.py`): a declared `cluster` must match it, not merely be a non-empty string. The same pass, low severity, found the defensive second-read branch only guarded `json.loads()` itself raising — valid JSON parsing to something other than an object (e.g. a bare array) reached `concept_data.get("cluster")` and raised `AttributeError` instead of `RequiredWitnessError`; fixed with an explicit `isinstance(concept_data, dict)` check ahead of it.

A witness's `artifact_manifest` entry is hashed with `validate_witness.witness_promotion_digest()` — canonical JSON (sorted keys, compact separators, the same canonical-json-v1 style `witness_result.py` and `generate_feature_ledger.py` already use) over the ENTIRE witness spec with only `output.render_hash` excluded — never a general weakening of the artifact's own hash. Fixture identity, `renderer`/`expectation`, `output.path`, `output.renderer_actual`, and the normative `determinism.value_hash` all stay covered: any of them changing does invalidate the receipt, exactly as this section's own first bullet requires. This ONE rule is defined once (`validate_witness.py`) and reused verbatim by both the generator and the validator, so the two can never silently disagree about what invalidates a witness's entry. A witness spec is validated with `validate_witness.py`'s own G1a/G1b/G2 (real concept/query context from its owning crate's `specs_search_root`) before its digest is ever trusted or its `schema_version` recorded — the identical "genuinely valid, not just present" bar every other recognized artifact kind here already gets.

Generated witness SVGs, `docs/witnesses/_contact_sheet.svg`, and `ci/results/feature_ledger.json` are refused outright (`is_generated_review_projection()`, checked in both `compute_artifact_manifest()` and the validator's own `check_artifact_manifest()`) if a caller lists one in `artifact_manifest` at all — external review, medium severity: an earlier version accepted these as ordinary, plain-byte-hashed artifacts with nothing stopping a caller from listing one, so including the contact sheet silently made promotion acceptance gate on a picture regenerating, contradicting §16.6's own "review projections, never promotion inputs" boundary. They are never required either way. A second review pass, medium severity, found this classification was itself bypassable: it compared only the entry's RESOLVED (symlink-followed) identity, so a symlink planted AT the canonical `docs/witnesses/*.svg` location but pointing OUTSIDE it — the resolved target's own parent directory is not `docs/witnesses` — was accepted and byte-hashed as an ordinary artifact, with its later target change then revoking an unrelated promotion. `is_generated_review_projection()` now also checks the manifest's own STATED path, purely lexically (`_looks_like_witness_svg_or_ledger_path()`, never filesystem-resolved) — either signal alone is sufficient to refuse. Backward compatible throughout: `descriptor` is an optional parameter everywhere it was added, and a workspace with no witnesses reproduces every function's original behavior exactly.

**Renderer integrity.** `scripts/validate_work_package.py` (§10/§13) adds `check_witness_renderer_integrity()`, requiring `scripts/witness_renderer.py` AND its own direct helper `scripts/xml_escape.py` (imported by the renderer for its own text escaping — a modification there can change rendered evidence just as surely as one to the renderer itself, closing the transitive gap rather than pinning only the entrypoint) each have their own `gate_integrity` entry, are covered by `protected_write_set`, and are NOT covered by `allowed_write_set` — unconditional for every work-package manifest, the same treatment every other pinned gate implementation in this section's own worked example already gets, and project-agnostic: these are THIS PIPELINE's own fixed implementation paths, never a downstream project's concept/cluster/crate/witness_id. Reuses `check_gate_integrity()` for hash correctness and workspace/symlink containment (already runs against every present entry, regardless of name) and `_patterns_can_overlap()` — the exact same product-automaton glob-intersection engine chainlink #14's own review chain hardened — for write-set coverage, proving `scripts/**`'s claimed coverage rather than assuming a broad pattern is enough. No second integrity checker; only two new, narrow requirements layered onto the existing machinery.

**Implemented** (chainlink #36). `witness` was already present in `docs/work-package-manifest-schema.json`'s `mitigations[].kind` enum and in this section's own prose ahead of this issue — the issue's own description called the remaining work "doc-only," which undersold what was actually missing: nothing enforced the risk-tier restriction mechanically, so a `witness` mitigation was schema-legal at every risk tier. `scripts/validate_work_package.py` adds `check_witness_mitigation_risk_tier()` (gate G6, wired into `validate_data()` alongside the other §10.1 checks), a pure structural check over each `trusted_assumptions[]` entry already parsed and schema-valid — no filesystem or cross-file resolution needed, unlike `check_trusted_assumptions()`. For any entry whose `mitigations[]` includes a `witness`-kind entry: reject outright (`severity="error"`) if `risk` is not `low`, even when a stronger mitigation (`test`, `proof`, `human-risk-acceptance`, ...) is also present on the same entry — an inappropriate-tier witness is the defect regardless of its siblings. At `risk: "low"`, this section's own requirement is "issue + explicit acceptance": the issue half is already structurally guaranteed by the required `assumption_ref.tracking_issue` field, and the explicit-acceptance half is the existing `human-risk-acceptance` mitigation kind — reused, not reinvented, per the issue's own "do not invent a new authority mechanism" constraint. A low-risk witness therefore still requires its own `human-risk-acceptance` mitigation alongside it on the same entry; a witness augments that acceptance, it never stands in for it. Assumptions with no `witness`-kind mitigation are untouched by this check, so every entry composition that validated before #36 still validates identically. Known limitation, reported rather than papered over: this closes only the `witness`-specific slice of G6 (plan.md §12's gate table row) — the full risk-tier mitigation-composition matrix for the other six typed-mitigation kinds (e.g., whether `critical` genuinely has ≥1 technical mitigation plus human approval, or `medium` genuinely has "issue + plan") remains unimplemented; G6 is only partially mechanized by this issue, not closed as a gate.

### 16.6 Boundary

Not layer 3, not a bridge, no effect on `closure_kind`. This section adds an evidence source and two generated reports; it does not touch S/I/C/O, promotion authority, or the G14 closure mechanism. Keep the separation sharp, or the contact sheet starts reading as proof — the exact failure mode the rest of this plan is built to avoid.
