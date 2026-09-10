# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Added
- Authoritative implementation inventory (docs/implementation-inventory.json, schema v1.0) with a filesystem/argparse drift test, replacing the stale "53 tests / M0-M1" summary: 1333 passing tests (154 subtests), 32 registered CLI commands, 39 runtime modules, M0-M4 all complete (#55)
- Stable public CLI grammar v1.0 (docs/cli-contract.md): `init doctor version status check validate gate draft approve migrate report`, with a complete 32/32 legacy-command disposition mapping and the `status`-stub-to-`doctor` retirement plan (#55)
- Versioned `ligature status --json` and `ligature check --json` output contracts (schemas/project-state.schema.json, schemas/consolidated-check.schema.json, both v1.0), specified (not implemented) for #56, with a regression test proving the old capability summary cannot satisfy the new schema (#55)
- Reconciled exit-code contract (docs/exit-code-contract.md) and a pure precedence resolver, scripts/exit_codes.py (#55)
- Trust and compatibility boundaries document (docs/trust-and-compatibility-boundaries.md) (#55)
- Workspace-wide assumption-identity collision check across boundary contracts (#6)

### Fixed
- gate-g14 silently skips invalid closure artifacts instead of saying why (#49)
- Vacuous 'OK' from validators when zero artifacts are discovered (#48)

### Changed
- Reconcile implemented surface and define stable CLI/state contracts (#55)
- Pilot-cluster selection rubric (verifier-agnostic) (#4)
- witness as typed mitigation kind (risk tier low only) (#36)
- Witness artifacts in promotion manifest + protected_write_set + gate_integrity (#35)
- Contact sheet: Stage 4.5 review surface with evidence banner (#32)
- Feature ledger generator: ci/results/feature_ledger.json (#34)
- G20 -- degenerate-witness gate against declared expectation (#31)
- G19 -- witness determinism gate (#30)
- G18 -- witness coverage gate (#29)
- witness_required: add to concept-to-code's query schema (upstream request) (#33)
- Renderer contract: hard-fail on fallback, record actual renderer (#28)
- Witness spec schema: fixture/renderer/determinism contract, incl. canonical numeric-result renderer output (#27)
- Human gold-set prototype: precision, recall, and omission analysis (#26)
- Bridge harness generation: compile typed bridge_logic to a verifier-dispatched check (#47)
- G14 transitive closure + CG6 well-foundedness discharge; closure profiles & degradation records (#25)
- Coarse C_static extractor with honest unresolved classes; R1/G16 risk tiers (#24)
- satisfies() mechanism + governed assurance-profile authorship (#23)
- Typed bridge expression language + call-site fact semantics; harness generation (#22)
- I-schema: closed (#21)
- Deterministic promotion-receipt generator + dedicated acceptance operation (Stage 4.5, no LLM) (#45)
- Stage 3 draft template: interaction (full I-schema: #16-#19) (#41)
- R2: eligible-interaction coverage by boundary or reviewed exemption, fail closed (#46)
- Evidence schema: claim field, semantic_disposition/lifecycle split, conflict resolution (#20)
- Protocol classification + protocol-debt records, fail closed (#19)
- realization.requirement + config_scope on I edges (#18)
- Assurance requirement located in I: reliances[].required_assurance (#17)
- Computed eligibility + separate exemption objects with review blocks (#16)
- Detached promotion receipt with explicit artifact_manifest (#15)
- Work-package manifest schema + validator (orchestrator handoff contract) (#14)
- Prototype A: G2+ role-safety live against boundary v1.0 (#13)
- Pipeline CLI entrypoint: dispatch stages 0-8C from the project descriptor (#37)
- Stage 0/3 one-shot prompt templates: structured-output contract for LLM-side stages (#38)
- Human review/approval checkpoint mechanism (#39)
- Deliberate-failure regression fixture convention (#12)
- G2+ = role safety only for first gate pass (#11)
- G1a/G1b: schema + repo-semantics gate split (#9)
- Stable obligation identifier on constraint: add id (concept-to-code schema change, decided) (#40)
- reliance-policy.md as governed policy doc (#10)
- Boundary artifact ground truth: naming + layout convention (#8)
- Project structure descriptor (Stage P0) (#7)
- concept-to-code: submodule integration + required modifications (#3)
