#!/usr/bin/env python3
"""The pipeline CLI entrypoint (plan.md §6.1, chainlink #37).

One command, invoked against a project workspace, spanning the whole
pipeline -- not a two-tool split between an LLM-run authoring session and a
separately-invoked deterministic tool. Reads the project structure
descriptor for all configuration.

Implemented now, against schemas that actually exist:
  draft     Stage 0/3 -- render a prompt template, dispatch to the
            configured one-shot LLM backend, stage the result as a draft.
            Never calls a backend for anything past this command.
  approve   The review/approval checkpoint (#39) -- the path that writes a
            single draft to its target path.
  approve-pair  The explicit transactional checkpoint for a new non-pairwise
            interaction and its protocol-debt record, whose cross-references
            require both artifacts to be accepted together.
  approve-exemption-pair  The same transactional bootstrap for a new
            interaction and a reviewed exemption covering it (chainlink #46,
            R2's exemption route) -- neither can be approved alone, since
            R2 requires an already-promoted exemption and the exemption's
            own cross-reference requires an already-promoted interaction.
  validate  Stage 4 (G1a/G1b/G2+) over boundary contracts (#9/#11) --
            fully deterministic, no LLM calls, by construction.
  validate-work-package  Stage 7's own schema + §10.1 checks (#14) over a
            work-package manifest. Standalone, not routed through
            draft/approve -- a manifest is machine-generated at Stage 7
            from already-promoted content, not LLM-drafted and
            human-reviewed the way a boundary contract is.
  validate-promotion  Stage 4.5's schema + §7.1 checks (#15) over a
            promotion receipt: every artifact_manifest path is workspace-
            anchored, every declared hash is verified against the real
            file, the receipt is never in its own manifest, and no listed
            artifact carries a promotion_id back (references are
            one-way). Standalone, not routed through draft/approve --
            the receipt's reviewer/accepted_at fields are top-level, not
            the nested review: {} shape approve() writes.
  accept-promotion  Stage 4.5's own generator (#45): loads the real
            project descriptor (--descriptor, same global flag every
            other command uses) and deterministically computes
            artifact_manifest (real file hashes), promotion_id (from
            `cluster` alone), and schema_versions -- the latter only
            after validating each accepted artifact against its own
            real per-kind validator WITH real cross-file context
            (interactions_by_id, R2's boundary/exemption coverage, G15's
            protocol-debt coverage, evidence ids -- the same context
            cmd_validate_interaction/cmd_validate_exemption/
            cmd_validate_protocol_debt build), so G2/G15/R2/G11/etc. run
            for real, fail-closed, not degraded to non-blocking info
            findings. Artifact kind is matched against the descriptor's
            own canonical directories, not merely a directory sharing a
            kind's basename. policy_version is read from the accepted
            policy document's own content (its exactly-one "Policy
            version: <name>@<major>.<minor>" marker line,
            docs/reliance-policy.template.md), not supplied as a
            free-form argument -- --policy-path names which accepted
            artifact to read it from. Validates the assembled receipt
            against validate-promotion's own real checks before writing
            anything, then appends the audit entry and swaps the receipt
            into place as one transaction (a failure on either side
            leaves neither behind) -- the dedicated acceptance operation
            this schema needs, since approve()'s nested review: {} write
            is incompatible with this schema's top-level reviewer/
            accepted_at and additionalProperties: false. See
            scripts/generate_promotion_receipt.py.
  validate-interaction  Stage 4's G1a/G1b/G2++/G15 over interaction (I)
            specs (#16/#17/#19): schema plus COMPUTED eligibility (plan.md
            §5.2) -- eligibility is derived from edge_class, never
            hand-set, and disagreement between the derived and stored
            value is rejected -- plus G15 (fail closed): a non-pairwise
            interaction with no valid protocol-debt record covering it is
            rejected, cross-referenced live against the crate's
            _protocol_debt/ directory.
  validate-exemption  Stage 4's G1a/G1b over boundary-required exemption
            objects (#16): schema plus naming (interaction_id == filename
            stem), plus G2 (chainlink #46): the named interaction must
            resolve to a real, promoted, boundary-required interaction --
            a dangling or ineligible reference is rejected outright, and
            never contributes to validate-interaction's own R2 coverage
            check either (same mechanism, not two).
  validate-protocol-debt  Stage 4's G1a/G1b over protocol-debt records
            (#19): schema plus naming plus interaction cross-reference --
            a debt record naming a nonexistent interaction, or one whose
            protocol_class is actually pairwise, is rejected, cross-
            referenced live against the crate's _interactions/ directory.
  validate-evidence  Stage 4's G1a/G1b over evidence records (#20):
            schema plus naming. Workspace-level, not crate-scoped (one
            evidence/ directory per workspace, not per crate). Never
            routed through draft/approve -- evidence carries no review
            block (plan.md §7.2's human-checkpoint list names
            "evidence-conflict resolution", not evidence itself).
  validate-conflict-resolution  Stage 4's G1a/G1b/G11 over evidence
            conflict-resolution records (#20): schema (incl. the
            status == resolved => resolution + review requirement),
            selected-authority membership in the record's own evidence
            list, a live dangling-evidence-reference check, and G11 --
            "only unresolved conflicts block" (plan.md §11's own words) --
            enforced directly, not deferred. Also workspace-level; IS
            routed through draft/approve, since it does carry a review
            block and is in §7.2's checkpoint list.
  validate-bridge  Stage 4's G1a/G1b/G2 over bridge specifications
            (plan.md §8.2/§8.3, chainlink #22): schema (incl. the typed
            bindings/premises/conclusion bridge_logic form, and the
            structural exclusion of "caller-postcondition" from
            available_contract_facts' role enum -- a caller postcondition
            only holds after the caller returns, so it can never be an
            available call-site fact) plus naming (bridge_id == filename
            stem, flat inside a crate's _bridges/ directory) plus
            self-contained consistency (callee_requirement must equal
            bridge_logic.conclusion.obligation_id -- the human-readable
            summary and the machine-checkable conclusion can never
            silently diverge) plus G2: boundary_id must resolve to a
            real, promoted boundary contract in the same crate, and
            callee_requirement must be one of that boundary's own
            callee_guarantees. This is the schema+validator half of #22
            only -- generating a verifier harness from bridge_logic is
            explicitly out of scope here (plan.md §15's own open items
            list harness-generation semantics as unresolved design
            territory), deferred to a follow-up chainlink issue.
  extract-c-static  Stage 8A's coarse C_static extractor (plan.md §9,
            chainlink #24): one report per descriptor crate into
            ci/results/c_static/<crate>.json -- a generated observation,
            never a spec under the protected crates/*/specs/**. Requires
            an explicit --target: C is configuration-relative, and an
            invented target makes R1's configuration comparison
            meaningless. Syntactic, so it only ever claims
            `discovered-lower-bound` -- a claim the schema itself binds
            to the extractor's backing.
  validate-callsites  Stage 8A's G1a/G1b over those reports:
            schema, naming (report_id == filename stem, flat inside
            c_static/), and RECOMPUTED callsite coverage -- the stored
            counts are never trusted, the same discipline G1b applies to
            computed eligibility in I. Workspace-level; carries no
            review block, so never routed through draft/approve.
  gate-r1-g16  Stage 8A's R1 + G16 (plan.md §9.1): realized calls
            reconciled against I within compatible configurations, and
            unresolved call sites risk-tiered -- critical/high block,
            medium is a human decision (exit 3, never a silent pass),
            low is a visible accepted limitation. Reports "all
            discovered call sites resolved", never "all call sites
            resolved".
  render-witness  Dispatch a witness's already-validated canonical
            result through the renderer contract (plan.md §16.1/§16.2,
            chainlink #28): scripts/witness_renderer.py either produces
            an SVG appropriate to the result's kind or raises -- there is
            no third path, no "degraded fallback" branch anywhere in the
            renderer code. Writes NOTHING on failure: no partial SVG, no
            placeholder. renderer_actual is self-reported by the
            renderer function itself, not echoed from the request, and
            cross-checked against the registry key it was dispatched
            under -- an independent observation of what ran, which is
            what makes G20's later declared/actual comparison (#31)
            meaningful. Never mutates a witness spec on disk (a reviewed,
            normative artifact); prints the output block a draft would
            quote.
  validate-witness  G1a/G1b over witness specs (plan.md §16.1, chainlink
            #27) plus G2: the witnessed `query` must resolve to a query
            declared `pure: true` in the concept's own spec, matched the
            way validate_boundary_contracts.py matches a constraint (on
            the spec's own `concept` field, never a filename). Also
            validates the canonical results under ci/results/witnesses/,
            recomputing value_domain and value_hash -- the normative
            determinism artifact `render_hash` deliberately is not.
            Witness coverage (G18, #29), determinism across a
            regeneration (G19, #30) and degeneracy against the declared
            expectation (G20, #31) are separate gates, not this
            validator. A witness is EVIDENCE, never assurance: nothing
            here reaches satisfies(), accepted_evidence_kinds or
            closure_kind.
  gate-g18  Stage 4's witness coverage gate (plan.md §16.2, chainlink
            #29): every query marked `witness_required: true` in a
            concept spec must resolve to a genuinely valid witness spec
            (validate-witness's own G1a/G1b/G2 bar) whose declared
            rendering actually exists on disk. Coverage is relative to
            the DECLARED feature set only -- an undeclared query is
            invisible to this gate, the same boundary R2 draws against
            accepted I. The same `(concept, query)` declared true in more
            than one concept spec, or covered by a genuinely valid
            witness in more than one crate, is ambiguous and excluded
            rather than resolved from whichever sorted first.
  gate-g19  Stage 8A/CI's witness determinism gate (plan.md §16.2,
            chainlink #30): every genuinely valid witness spec's
            declared `determinism.value_hash` must match a FRESH
            regeneration's `value_hash` -- regeneration is unconditional
            and internal to this gate, dispatched to the project
            descriptor's `witness_backend` command every run, passing
            that spec's own current witness_id/concept/query/fixture_id
            /seed/renderer as arguments so a regenerated result's
            identity is constructed from the current spec and checked
            against it before its hash is ever trusted. With no backend
            configured, nothing was actually re-evaluated this run and
            the gate blocks -- an earlier version instead trusted
            whatever canonical result already sat on disk, which an
            external review found let a stale file outlive a producer's
            real behavior change and pass regardless. `render_hash` is
            never read here: a determinism claim is about the measured
            VALUE, not the rendering, and a changed `render_hash` alone
            must never block. Reuses validate_witness.py's own G1a/G1b
            result validation rather than re-hashing anything.
  gate-g20  Stage 4.5's degenerate-witness gate (plan.md §12's gate
            table, chainlink #31): `expectation.value_distribution:
            must-vary` but the current genuinely valid canonical result
            measures only one distinct value, and `coverage_region`
            inconsistent with `fixture_family` workspace-wide (reuses
            validate_witness.py's own check_family_consistency rather
            than reimplementing it). Both WARN as this gate's own
            report: not a hard Stage-4/CI block, since `gate-g20` alone
            is a fresh recomputation with no promotion attached to it;
            it exits with a THIRD, distinct code, mirroring
            gate-r1-g16's own EXIT_DECISION_REQUIRED tier, never
            collapsing into a clean 0 or the same exit code a real
            block uses. The SAME findings are promoted from WARN to
            ERROR at `approve()` time for a witness spec promotion --
            `specs/_witnesses/*.json` is a recognized artifact type
            there (chainlink #31's own promotion wiring), so "warn
            early, block at promotion if unresolved" is one mechanism,
            not two. An ambiguous witness_id (imported from gate-g19's
            own collection) still blocks outright here regardless --
            a structural identity problem, not a degeneracy one.
  generate-feature-ledger  Stage 4.5's feature ledger generator
            (plan.md §16.4, chainlink #34): a GENERATED, READ-ONLY
            projection at ci/results/feature_ledger.json, one entry per
            DECLARED (witness_required: true) feature. Reuses G18/G19/G20
            for real (including G19's own live regeneration dispatch)
            rather than re-deriving their dispositions, so this is a
            fresh recomputation every run, never a cache of a stale one.
            The two-column discipline plan.md §16.4 names is the whole
            reason this exists: `implementation_observed` (a weak
            liveness fact -- the body executed and produced a stable,
            non-degenerate value) and `assurance_status` (the contract
            is established) are never inferred from each other.
            `assurance_status` is currently always `unsupported` -- see
            scripts/generate_feature_ledger.py's own module docstring
            for why no mechanical link between a witnessed query and a
            pipeline obligation exists yet. Refuses to write a
            schema-invalid result; atomic write, same guarantee
            chainlink #28 established for a witness rendering.
  generate-contact-sheet  Stage 4.5's review surface (plan.md §16.3,
            chainlink #32): a GENERATED, READ-ONLY SVG at
            docs/witnesses/_contact_sheet.svg, one panel per DECLARED
            feature, built over a fresh generate-feature-ledger call --
            never a stale ci/results/feature_ledger.json. Fixed banner
            ("∃-witness evidence -- not verification.") plus, per
            panel, the SAME two-axis discipline #34 established: witness
            /implementation/determinism/degeneracy are drawn as colored
            traffic-light badges, while assurance_status and
            closure_kind are drawn in a completely disjoint, never-green
            palette with an explicit text prefix -- a fully green panel
            can never be scanned as "assurance established." Each
            feature's own generated witness SVG is embedded inline as a
            base64 data URI when its witness is present (never a
            relative file link, so the sheet stays one self-contained
            artifact); missing/unavailable renders an explicit dashed
            placeholder without dropping the panel. Never a promotion
            input -- not recognized by approve()'s dispatcher. Refuses
            to build over an ambiguous declaration or a schema-invalid
            ledger; atomic write, chainlink #28's own guarantee.
  validate-gold-set  G1a/G1b over human gold sets (plan.md §5.2's
            independence argument, chainlink #26): schema, naming, and
            the scope/provenance discipline -- every edge's caller must
            be in the curated scope, every derived_from must be a method
            the curator claimed, and no intra-concept edges (which
            gate-r1-g16 correctly treats as internal helpers). The
            anti-circularity guard is in the schema: `derived_from` has
            no value for "read it in the interaction set", because a
            gold set derived from the artifact it audits makes recall
            vacuous.
  measure-gold-set  Precision, recall and OMISSION against a human gold
            set (chainlink #26). Omission -- gold edges no candidate
            source proposed at all -- is the number that makes "R2
            passed, therefore I is complete" unreadable. Every report
            lists all seven of plan.md §5.2's independent candidate
            sources with their real status, because an omission count is
            only interpretable next to how many sources were actually
            consulted (two exist here; five are not built). Not a gate:
            omission is a finding to read; what fails is being unable to
            measure.
  check-bridges  Stage 8A's bridge harness generation (plan.md §8.3,
            chainlink #47): compiles a promoted bridge's typed
            bridge_logic to a harness for the verifier that OWNS its
            cluster (resolved through the closure profile that names the
            work package requiring the bridge, then verifier_policy),
            writes it under ci/harness/, and dispatches it to the
            configured verifier_backends command, recording the verdict.
            The compiler is total-or-rejecting over a closed expression
            fragment -- "compiles to a harness the owning verifier
            checks" is only a definition if what it cannot compile is
            refused with a reason. With no backend configured nothing is
            dispatched and nothing is recorded: a verifier that never ran
            must never produce a pass.
  gate-g9  Stage 8A's G9 ("bridge check fails", plan.md §12): recompiles
            each promoted bridge and checks that the recorded result is
            about THAT bridge -- the harness on disk matches the
            recompilation, the recorded harness hash equals the
            recomputed one, the verifier owns the cluster, the claim
            passed, and no work package's assurance report asserts a
            bridge_record that disagrees with the check. This is the
            machine relation §8.3's temporary "hash-pin the harness and
            report harness-tested" fallback said did not exist.
  validate-closure  Stage 8C's G1a/G1b over closure profiles and
            degradation records (plan.md §4, chainlink #25), plus G17:
            `deductive` is refused for a Kani-owned cluster, and a
            cluster's profile and degradation record must agree about
            which conditions failed -- in both directions, since a stale
            excuse and an undeclared degradation each hide something.
  gate-g14  Stage 8C's release closure (plan.md §8.5): loads every
            required-guarantee dependency, computes the TRANSITIVE
            closure, detects cycles, evaluates satisfies() on every
            requirement in it, and holds every assumption anywhere in
            the closure against the cluster's own entry trust policy --
            §8.5's motivating case is a depth-2 assumption that every
            direct check passes over. A cycle closes only with an
            explicit well-foundedness discharge naming exactly its
            members (CG6). Outcome is per cluster and never global:
            closes / degraded-under-an-accepted-record / blocked.

Not yet implemented -- the schemas these stages need don't exist yet
(tracked as the named chainlink issues, not guessed at here):
  emission, attach, manifest and promotion-receipt *generation* (the
  schemas/validators exist as of #14/#15; the generators that read
  promoted I/O and emit these don't, since they need the rest of the
  I-schema machinery M3 builds). Stage 8A is now partly real (#24's
  three commands above, plus #23's satisfies() mechanism), and Stage 8C
  is real (#25's validate-closure/gate-g14); bridge harness
  generation/dispatch (#47) and Stage 8B acceptance (#26) remain open.
  `pipeline status` reports this honestly instead of a stage silently
  no-op'ing.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract_c_static import ExtractionError  # noqa: E402
from extract_c_static import extract_crate as extract_c_static_crate  # noqa: E402
from gate_g14 import gate_workspace as gate_g14_workspace  # noqa: E402
from gate_g18 import gate_workspace as gate_g18_workspace  # noqa: E402
from gate_g18 import report_findings as report_g18_findings  # noqa: E402
from gate_g19 import gate_workspace as gate_g19_workspace  # noqa: E402
from gate_g19 import report_findings as report_g19_findings  # noqa: E402
from gate_g20 import gate_workspace as gate_g20_workspace  # noqa: E402
from gate_g20 import report_findings as report_g20_findings  # noqa: E402
from generate_feature_ledger import GenerationError as FeatureLedgerGenerationError  # noqa: E402
from generate_feature_ledger import write_ledger as write_feature_ledger  # noqa: E402
from generate_contact_sheet import write_contact_sheet  # noqa: E402
from gate_g9 import check_bridges as check_bridges_g9  # noqa: E402
from gate_g9 import gate_workspace as gate_g9_workspace  # noqa: E402
from gate_g9 import report_findings as report_g9_findings  # noqa: E402
from measure_gold_set import measure_workspace as measure_gold_set_workspace  # noqa: E402
from measure_gold_set import report as report_gold_set_measurements  # noqa: E402
from gate_g14 import report_outcomes as report_g14_outcomes  # noqa: E402
from gate_r1_g16 import gate_workspace as gate_r1_g16_workspace  # noqa: E402
from gate_r1_g16 import report_findings as report_r1_g16_findings  # noqa: E402
from generate_promotion_receipt import PromotionReceiptError  # noqa: E402
from generate_promotion_receipt import accept_promotion  # noqa: E402
from project_descriptor import ProjectDescriptorError  # noqa: E402
from project_descriptor import boundary_dir_for as _boundary_dir_for  # noqa: E402
from project_descriptor import boundary_dirs_for_descriptor  # noqa: E402
from project_descriptor import bridge_dir_for as _bridge_dir_for  # noqa: E402
from project_descriptor import conflict_dir_for as _conflict_dir_for  # noqa: E402
from project_descriptor import evidence_dir_for as _evidence_dir_for  # noqa: E402
from project_descriptor import exemption_dir_for as _exemption_dir_for  # noqa: E402
from project_descriptor import interaction_dir_for as _interaction_dir_for  # noqa: E402
from project_descriptor import load_project_descriptor as _load_project_descriptor  # noqa: E402
from project_descriptor import protocol_debt_dir_for as _protocol_debt_dir_for  # noqa: E402
from review_checkpoint import ApprovalRefused  # noqa: E402
from review_checkpoint import approve as checkpoint_approve  # noqa: E402
from review_checkpoint import approve_pair as checkpoint_approve_pair  # noqa: E402
from review_checkpoint import stage_draft  # noqa: E402
from validate_boundary_contracts import load_boundaries_by_id  # noqa: E402
from validate_boundary_contracts import load_draft_validator as load_boundary_draft_validator  # noqa: E402
from validate_boundary_contracts import load_validator as load_boundary_validator  # noqa: E402
from validate_boundary_contracts import validate as validate_boundaries  # noqa: E402
from validate_boundary_contracts import validate_data as validate_boundary_data  # noqa: E402
from validate_boundary_contracts import validate_draft_data as validate_boundary_draft_data  # noqa: E402
from validate_boundary_contracts import valid_boundary_edges_from_crate  # noqa: E402
from validate_boundary_contracts import count_discovered as _count_boundaries  # noqa: E402
from validate_bridge import load_draft_validator as load_bridge_draft_validator  # noqa: E402
from validate_bridge import load_validator as load_bridge_validator  # noqa: E402
from validate_bridge import validate_crate as validate_bridge_crate  # noqa: E402
from validate_bridge import validate_data as validate_bridge_data  # noqa: E402
from validate_bridge import validate_draft_data as validate_bridge_draft_data  # noqa: E402
from validate_bridge import count_discovered as _count_bridges  # noqa: E402
from validate_callsites import callsite_report_dir_for as _callsite_report_dir_for  # noqa: E402
from validate_callsites import validate_workspace as validate_callsites_workspace  # noqa: E402
from validate_callsites import count_discovered as _count_callsite_reports  # noqa: E402
from validate_closure import closure_dir_for as _closure_dir_for  # noqa: E402
from validate_closure import count_discovered as _count_closure_artifacts  # noqa: E402
from validate_closure import load_validators as load_closure_validators  # noqa: E402
from validate_closure import validate_draft_data as validate_closure_draft_data  # noqa: E402
from validate_closure import validate_workspace as validate_closure_workspace  # noqa: E402
from validate_gold_set import count_discovered as _count_gold_sets  # noqa: E402
from validate_gold_set import gold_set_dir_for as _gold_set_dir_for  # noqa: E402
from validate_gold_set import validate_workspace as validate_gold_set_workspace  # noqa: E402
from validate_witness import count_discovered as _count_witnesses  # noqa: E402
from validate_witness import count_results as _count_witness_results  # noqa: E402
from validate_witness import load_validator as load_witness_validator  # noqa: E402
from validate_witness import validate_crate as validate_witness_crate  # noqa: E402
from validate_witness import validate_results as validate_witness_results  # noqa: E402
from validate_witness import witness_dir_for as _witness_dir_for  # noqa: E402
from gate_g20 import validate_witness_for_approval  # noqa: E402
from generate_witness import GenerationError  # noqa: E402
from generate_witness import generate as generate_witness  # noqa: E402
from validate_conflict_resolution import load_draft_validator as load_conflict_resolution_draft_validator  # noqa: E402
from validate_conflict_resolution import load_validator as load_conflict_resolution_validator  # noqa: E402
from validate_conflict_resolution import validate_data as validate_conflict_resolution_data  # noqa: E402
from validate_conflict_resolution import validate_draft_data as validate_conflict_resolution_draft_data  # noqa: E402
from validate_conflict_resolution import validate_workspace as validate_conflict_resolution_workspace  # noqa: E402
from validate_conflict_resolution import count_discovered as _count_conflicts  # noqa: E402
from validate_evidence import load_validator as load_evidence_validator  # noqa: E402
from validate_evidence import valid_evidence_ids  # noqa: E402
from validate_evidence import validate_data as validate_evidence_data  # noqa: E402
from validate_evidence import validate_workspace as validate_evidence_workspace  # noqa: E402
from validate_evidence import count_discovered as _count_evidence  # noqa: E402
from validate_exemption import load_draft_validator as load_exemption_draft_validator  # noqa: E402
from validate_exemption import load_validator as load_exemption_validator  # noqa: E402
from validate_exemption import validate_crate as validate_exemption_crate  # noqa: E402
from validate_exemption import validate_data as validate_exemption_data  # noqa: E402
from validate_exemption import validate_draft_data as validate_exemption_draft_data  # noqa: E402
from validate_exemption import valid_exemption_interaction_ids_from_crate  # noqa: E402
from validate_exemption import count_discovered as _count_exemptions  # noqa: E402
from validate_interaction import load_draft_validator as load_interaction_draft_validator  # noqa: E402
from validate_interaction import load_interactions_by_id  # noqa: E402
from validate_interaction import load_validator as load_interaction_validator  # noqa: E402
from validate_interaction import validate_crate as validate_interaction_crate  # noqa: E402
from validate_interaction import validate_data as validate_interaction_data  # noqa: E402
from validate_interaction import validate_draft_data as validate_interaction_draft_data  # noqa: E402
from validate_interaction import count_discovered as _count_interactions  # noqa: E402
from validate_promotion_receipt import load_validator as load_promotion_validator  # noqa: E402
from validate_protocol_debt import load_draft_validator as load_protocol_debt_draft_validator  # noqa: E402
from validate_protocol_debt import load_validator as load_protocol_debt_validator  # noqa: E402
from validate_protocol_debt import valid_interaction_ids_from_crate  # noqa: E402
from validate_protocol_debt import validate_crate as validate_protocol_debt_crate  # noqa: E402
from validate_protocol_debt import validate_data as validate_protocol_debt_data  # noqa: E402
from validate_protocol_debt import validate_draft_data as validate_protocol_debt_draft_data  # noqa: E402
from validate_protocol_debt import count_discovered as _count_protocol_debt  # noqa: E402
from validate_promotion_receipt import validate_file as validate_promotion_file  # noqa: E402
from validate_work_package import load_validator as load_work_package_validator  # noqa: E402
from validate_work_package import validate_file as validate_work_package_file  # noqa: E402
from scan_summary import pass_line  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "prompts"

NOT_YET_IMPLEMENTED = {
    "G4/G5 evidence tracing/grounding": "not yet implemented -- required/bug-compat evidence tracing to "
    "nothing (G4) and ungrounded obligations (G5) both need cross-referencing interaction evidence_links "
    "(#16) to evidence ids (#20), which exist individually but aren't cross-referenced against each other yet",
    "emission": "M3's I-schema is now complete (#16-#20); emission (Stage 5) itself is still not built",
    "attach": "M3's I-schema is now complete (#16-#20); attach (Stage 6) itself is still not built",
    "manifest generation": "#14's schema+validator exist (`validate-work-package`); M3's I-schema is now "
    "complete (#16-#20), but the generator that reads promoted I/O and emits a manifest from it is still not built",
    "8A": "partly implemented -- C_static extraction and R1/G16 are real (#24), bridge harness "
    "generation, verifier dispatch and G9 are real (#47: check-bridges, gate-g9), and per-obligation "
    "assurance records have a record schema (#23) plus the report container G14 reads (#25). What is "
    "missing is the runner that emits a work package's OBLIGATION records from a real verifier run; "
    "bridge records now have one (ci/results/bridge_checks/), and no verifier backend is configured "
    "in this repository, so nothing here dispatches for real",
    "8B": "#26 (M4)",
}

_TEMPLATE_VAR = re.compile(r"\{\{(\w+)\}\}")


class PipelineError(Exception):
    pass


def load_project_descriptor(path: Path) -> dict:
    """Thin wrapper: the real logic lives in project_descriptor.py, shared
    with validate_work_package.py (which can't import this module back --
    pipeline.py already imports from validate_work_package.py, so the
    reverse would be circular). Re-raises as PipelineError so main()'s
    existing exception handling doesn't need to know about a second
    exception type."""
    try:
        return _load_project_descriptor(path)
    except ProjectDescriptorError as e:
        raise PipelineError(str(e))


def render_prompt(template_path: Path, variables: dict[str, str]) -> str:
    """{{var}} substitution -- deliberately not str.format(), since the
    templates embed literal JSON with single braces."""
    text = template_path.read_text()

    def _sub(match: re.Match) -> str:
        key = match.group(1)
        if key not in variables:
            raise PipelineError(f"template {template_path} references undefined variable {{{{{key}}}}}")
        return variables[key]

    rendered = _TEMPLATE_VAR.sub(_sub, text)

    # A malformed placeholder (e.g. containing a paren or space) won't
    # match _TEMPLATE_VAR's \w+ and would otherwise pass through silently
    # instead of raising -- catch that class of template-authoring bug
    # here rather than shipping a prompt with a literal {{...}} in it.
    leftover = re.search(r"\{\{[^}]*\}\}", rendered)
    if leftover:
        raise PipelineError(
            f"template {template_path} has an unresolved placeholder "
            f"{leftover.group(0)!r} that didn't match \\w+ -- fix the "
            "template, don't add ad hoc syntax to render_prompt"
        )

    return rendered


def invoke_llm_backend(backend: dict, prompt: str, runner=subprocess.run) -> str:
    """Pluggable one-shot backend (plan.md §6.1). Prompt is always piped
    via stdin regardless of backend, to avoid fighting each tool's own
    argument-length/quoting conventions for a long structured prompt.
    `runner` is injectable for testing -- never actually shells out in a
    unit test."""
    kind = backend["kind"]
    if kind == "manual":
        raise PipelineError(
            "llm_backend.kind is 'manual' -- print the prompt yourself and "
            "call stage_draft() with the result; invoke_llm_backend() does "
            "not run anything for manual mode."
        )

    default_commands = {
        "claude": ["claude", "-p"],
        "codex": ["codex", "exec"],
        "opencode": ["opencode", "run"],
    }
    if kind not in default_commands:
        raise PipelineError(f"unknown llm_backend.kind {kind!r}")

    command = backend.get("command")
    argv = command.split() if command else default_commands[kind]

    result = runner(argv, input=prompt, capture_output=True, text=True)
    if result.returncode != 0:
        raise PipelineError(
            f"LLM backend {argv} exited {result.returncode}: {result.stderr}"
        )
    return result.stdout


def parse_llm_json_output(raw: str) -> dict:
    """The templates require JSON-only output. Tolerates exactly one
    deviation: the *entire* response wrapped in a single markdown fence
    (```json ... ``` with nothing else before or after) -- the single most
    common way models violate "JSON only" while still being unambiguous
    about what to extract. Anything looser (prose before/after the fence,
    multiple fences) is deliberately NOT unwrapped and hard-fails instead --
    review finding D8: an earlier version of this docstring over-claimed
    tolerance here. That failure is acceptable-by-design (retry/escalate,
    plan.md §16.1's "no partial output if you are unsure" principle), not a
    bug to paper over by guessing which fenced block was the real answer."""
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*\n(.*)\n```$", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise PipelineError(
            f"LLM backend output was not valid JSON (after fence-stripping): {e}\n"
            f"raw output was:\n{raw}"
        )


def cmd_validate(args: argparse.Namespace) -> int:
    descriptor = load_project_descriptor(args.descriptor)
    findings_total = []
    discovered = 0
    for crate in descriptor["crates"]:
        crate_root = args.workspace / crate["crate_dir"]
        specs_search_root = args.workspace / crate["specs_search_root"]
        try:
            findings_total.extend(validate_boundaries(crate_root, specs_search_root))
            discovered += _count_boundaries(crate_root)
        except FileNotFoundError as e:
            # A crate_dir typo in the descriptor must be a loud failure,
            # not a silent "0 boundaries found, all clean" (external
            # review finding, high severity).
            raise PipelineError(f"crate {crate['crate_dir']!r} in the project descriptor: {e}")

    errors = [f for f in findings_total if f.severity == "error"]
    infos = [f for f in findings_total if f.severity == "info"]

    if infos:
        print(f"INFO: {len(infos)} non-blocking finding(s)")
        for f in infos:
            print(f"  - {f}")

    if not errors:
        print(pass_line(discovered, "boundary contracts", "G1a/G1b/G2+", args.workspace))
        return 0

    print(f"FAIL: {len(errors)} finding(s)")
    for f in errors:
        print(f"  - {f}")
    return 1


def cmd_validate_work_package(args: argparse.Namespace) -> int:
    # Default to the workspace root, never None -- omitting --specs-search-root
    # must not silently disable assumption-ref resolution (external review,
    # high severity: a real manifest with trusted_assumptions printed OK
    # via this exact code path without the check ever running).
    specs_search_root = args.specs_search_root if args.specs_search_root is not None else args.workspace

    # Restrict trusted-assumption resolution to the project descriptor's
    # own declared <crate_dir>/specs/_boundaries directories -- otherwise
    # a schema-shaped JSON file dropped anywhere under a directory named
    # _boundaries counts as "a real boundary contract" (external review:
    # reproduced with junk/not-a-crate/_boundaries/anything.json).
    descriptor = load_project_descriptor(args.descriptor)
    allowed_boundary_dirs = boundary_dirs_for_descriptor(descriptor, args.workspace)

    validator = load_work_package_validator()
    findings = validate_work_package_file(
        args.manifest, validator, args.workspace, specs_search_root, allowed_boundary_dirs
    )
    errors = [f for f in findings if f.severity == "error"]
    infos = [f for f in findings if f.severity == "info"]

    if infos:
        print(f"INFO: {len(infos)} non-blocking finding(s)")
        for f in infos:
            print(f"  - {f}")

    if not errors:
        print("OK: work package manifest passes G1a and §10.1 checks")
        return 0

    print(f"FAIL: {len(errors)} finding(s)")
    for f in errors:
        print(f"  - {f}")
    return 1


def cmd_validate_promotion(args: argparse.Namespace) -> int:
    validator = load_promotion_validator()
    findings = validate_promotion_file(args.receipt, validator, args.workspace)

    if not findings:
        print("OK: promotion receipt passes G1a and §7.1 checks")
        return 0

    print(f"FAIL: {len(findings)} finding(s)")
    for f in findings:
        print(f"  - {f}")
    return 1


def cmd_accept_promotion(args: argparse.Namespace) -> int:
    try:
        target_path = accept_promotion(
            workspace_root=args.workspace,
            cluster=args.cluster,
            reviewer=args.reviewer,
            policy_path=args.policy_path,
            artifact_paths=args.artifact,
            descriptor_path=args.descriptor,
            accepted_at=args.accepted_at,
        )
    except (PromotionReceiptError, ApprovalRefused, ValueError, ProjectDescriptorError) as e:
        raise PipelineError(str(e))
    print(f"accepted: {target_path}")
    return 0


def _require_crate_root_exists(crate: dict, workspace: Path) -> Path:
    crate_root = workspace / crate["crate_dir"]
    if not crate_root.is_dir():
        raise PipelineError(
            f"crate {crate['crate_dir']!r} in the project descriptor: crate root does not "
            f"exist or is not a directory: {crate_root}"
        )
    return crate_root


def cmd_validate_interaction(args: argparse.Namespace) -> int:
    # Discovers candidates crate-wide (any directory literally named
    # _interactions, at any depth) and rejects any that don't sit
    # directly under the crate's exact <crate_dir>/specs/_interactions
    # directory (project_descriptor.interaction_dir_for) -- external
    # review, medium severity, SECOND pass: an earlier anchored-only-the-
    # canonical-directory fix stopped a mislocated artifact from being
    # wrongly validated, but also stopped it from ever being looked at,
    # reproducing the same "zero findings" outcome by omission instead of
    # false acceptance. validate_interaction_crate() discovers first, then
    # rejects by location, so a mislocated artifact is neither accepted
    # nor invisible.
    # G15 (non-pairwise protocol coverage) is fail-closed here, not
    # deferred: for each crate, load its real interactions, use them to
    # find which protocol-debt records are themselves fully valid (their
    # own cross-reference to a real, non-pairwise interaction checked),
    # and pass that coverage set into the interaction scan so a
    # non-pairwise interaction with no valid debt record is rejected --
    # external review, high severity: this was previously assigned to
    # chainlink #21 in NOT_YET_IMPLEMENTED, but #21 is only the I-schema
    # milestone gate (all of #15-#20 landed), not an issue that itself
    # implements gates; #19's own title says "fail closed."
    # R2 (eligible-interaction coverage by boundary or reviewed exemption)
    # is the same shape, fail-closed the same way -- chainlink #46: load
    # the crate's own boundary contracts and exemptions, keep only the
    # ones that are themselves fully valid, and pass both coverage sets
    # into the scan so an eligible interaction with neither a covering
    # boundary nor a reviewed exemption is rejected.
    descriptor = load_project_descriptor(args.descriptor)
    findings_total = []
    discovered = 0
    for crate in descriptor["crates"]:
        crate_root = _require_crate_root_exists(crate, args.workspace)
        specs_search_root = args.workspace / crate["specs_search_root"]
        interactions_by_id = load_interactions_by_id(_interaction_dir_for(crate, args.workspace))
        valid_debt_interaction_ids = valid_interaction_ids_from_crate(
            crate_root, _protocol_debt_dir_for(crate, args.workspace), interactions_by_id
        )
        covering_boundary_edges = valid_boundary_edges_from_crate(
            crate_root, _boundary_dir_for(crate, args.workspace), specs_search_root
        )
        valid_exemption_interaction_ids = valid_exemption_interaction_ids_from_crate(
            crate_root, _exemption_dir_for(crate, args.workspace), interactions_by_id
        )
        discovered += _count_interactions(crate_root)
        findings_total.extend(
            validate_interaction_crate(
                crate_root, _interaction_dir_for(crate, args.workspace), valid_debt_interaction_ids,
                covering_boundary_edges, valid_exemption_interaction_ids,
            )
        )

    if not findings_total:
        print(pass_line(
            discovered, "interactions",
            "G1a/G1b (incl. computed eligibility), G15 protocol coverage, and R2 coverage",
            args.workspace,
        ))
        return 0

    print(f"FAIL: {len(findings_total)} finding(s)")
    for f in findings_total:
        print(f"  - {f}")
    return 1


def cmd_validate_exemption(args: argparse.Namespace) -> int:
    # Same discover-then-reject-by-location scan as
    # cmd_validate_interaction, for <crate_dir>/specs/_exemptions.
    # R2's reference-integrity half (chainlink #46): each exemption's own
    # interaction_id must resolve to a real, boundary-required interaction
    # -- fail-closed the same way cmd_validate_protocol_debt's own
    # interaction cross-reference already is.
    descriptor = load_project_descriptor(args.descriptor)
    findings_total = []
    discovered = 0
    for crate in descriptor["crates"]:
        crate_root = _require_crate_root_exists(crate, args.workspace)
        interactions_by_id = load_interactions_by_id(_interaction_dir_for(crate, args.workspace))
        discovered += _count_exemptions(crate_root)
        findings_total.extend(
            validate_exemption_crate(crate_root, _exemption_dir_for(crate, args.workspace), interactions_by_id)
        )

    if not findings_total:
        print(pass_line(
            discovered, "exemptions", "G1a/G1b (incl. interaction cross-reference)", args.workspace
        ))
        return 0

    print(f"FAIL: {len(findings_total)} finding(s)")
    for f in findings_total:
        print(f"  - {f}")
    return 1


def cmd_validate_protocol_debt(args: argparse.Namespace) -> int:
    # Same discover-then-reject-by-location scan as
    # cmd_validate_interaction/cmd_validate_exemption, for
    # <crate_dir>/specs/_protocol_debt, plus the same interaction
    # cross-reference cmd_validate_interaction's G15 check relies on.
    descriptor = load_project_descriptor(args.descriptor)
    findings_total = []
    discovered = 0
    for crate in descriptor["crates"]:
        crate_root = _require_crate_root_exists(crate, args.workspace)
        interactions_by_id = load_interactions_by_id(_interaction_dir_for(crate, args.workspace))
        discovered += _count_protocol_debt(crate_root)
        findings_total.extend(
            validate_protocol_debt_crate(
                crate_root, _protocol_debt_dir_for(crate, args.workspace), interactions_by_id
            )
        )

    if not findings_total:
        print(pass_line(
            discovered, "protocol-debt records", "G1a/G1b (incl. interaction cross-reference)", args.workspace
        ))
        return 0

    print(f"FAIL: {len(findings_total)} finding(s)")
    for f in findings_total:
        print(f"  - {f}")
    return 1


def cmd_validate_bridge(args: argparse.Namespace) -> int:
    # Same discover-then-reject-by-location scan as
    # cmd_validate_interaction/cmd_validate_exemption/
    # cmd_validate_protocol_debt, for <crate_dir>/specs/_bridges, plus
    # the G2 boundary cross-reference validate_bridge.py's own
    # check_boundary_cross_reference needs: only boundaries that are
    # themselves fully valid (schema + naming) are trusted, the same
    # "must be genuinely valid, not just present" bar every other
    # cross-reference in this pipeline applies.
    descriptor = load_project_descriptor(args.descriptor)
    findings_total = []
    discovered = 0
    for crate in descriptor["crates"]:
        crate_root = _require_crate_root_exists(crate, args.workspace)
        specs_search_root = args.workspace / crate["specs_search_root"]
        boundaries_by_id = load_boundaries_by_id(_boundary_dir_for(crate, args.workspace), specs_search_root)
        discovered += _count_bridges(crate_root)
        findings_total.extend(
            validate_bridge_crate(crate_root, _bridge_dir_for(crate, args.workspace), boundaries_by_id)
        )

    if not findings_total:
        print(pass_line(
            discovered, "bridges",
            "G1a/G1b (incl. conclusion consistency) and G2 boundary cross-reference",
            args.workspace,
        ))
        return 0

    print(f"FAIL: {len(findings_total)} finding(s)")
    for f in findings_total:
        print(f"  - {f}")
    return 1


def cmd_extract_c_static(args: argparse.Namespace) -> int:
    """Stage 8A: generate one C_static report per descriptor crate
    (chainlink #24). Output goes to ci/results/c_static/<crate>.json --
    a generated CI observation, never under crates/*/specs/**, which the
    descriptor's write_set marks protected precisely because specs are
    hand-authored and promoted.

    The target triple is a required argument, never defaulted: C is
    configuration-relative (plan.md §5), and an invented target would
    make R1's own configuration comparison meaningless. An existing
    report at the output path is passed back in as `--previous` so a
    human-set risk tier survives re-extraction (extractor defaults are
    re-derived, as they should be)."""
    descriptor = load_project_descriptor(args.descriptor)
    workspace = _require_workspace_root_exists(args.workspace)
    out_dir = _callsite_report_dir_for(workspace)
    config_scope = {
        "target": args.target,
        "features": sorted(set(args.feature)),
        "cfg": sorted(set(args.cfg)),
    }

    for crate in descriptor["crates"]:
        crate_root = _require_crate_root_exists(crate, workspace)
        report_id = crate["crate_dir"].replace("/", "_")
        out_path = out_dir / f"{report_id}.json"
        previous = None
        if out_path.exists():
            try:
                previous = json.loads(out_path.read_text())
            except json.JSONDecodeError:
                previous = None
        try:
            report = extract_c_static_crate(
                crate_root, workspace, report_id, crate["crate_dir"], config_scope, previous
            )
        except ExtractionError as e:
            raise PipelineError(str(e))
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2) + "\n")
        coverage = report["callsite_coverage"]
        print(
            f"wrote {out_path}: {coverage['discovered']} discovered, {coverage['resolved']} resolved, "
            f"{coverage['unresolved']} unresolved "
            f"({report['coverage_scope']['completeness_claim']})"
        )
    return 0


def cmd_validate_callsites(args: argparse.Namespace) -> int:
    # Workspace-level like validate-evidence: one ci/results/c_static
    # directory per workspace, not one per crate (each report names its
    # own crate_dir). No descriptor is needed to resolve anything.
    workspace_root = _require_workspace_root_exists(args.workspace)
    findings = validate_callsites_workspace(workspace_root, _callsite_report_dir_for(workspace_root))

    if not findings:
        print(pass_line(
            _count_callsite_reports(workspace_root), "C_static reports",
            "G1a/G1b (incl. recomputed callsite coverage)", workspace_root,
        ))
        return 0

    print(f"FAIL: {len(findings)} finding(s)")
    for f in findings:
        print(f"  - {f}")
    return 1


def cmd_render_witness(args: argparse.Namespace) -> int:
    """chainlink #28: dispatch a witness's already-validated canonical
    result through the renderer contract (scripts/witness_renderer.py)
    and write its SVG. Fails loudly and writes nothing on any of: no
    genuinely valid canonical result for this witness_id, an unregistered
    renderer, or a renderer that cannot honestly handle the result's
    shape (never a degraded substitute). Prints the `output` block
    (path/render_hash/renderer_actual) a witness spec draft would quote."""
    workspace = _require_workspace_root_exists(args.workspace)
    try:
        output = generate_witness(workspace, args.witness_id, args.renderer)
    except GenerationError as e:
        raise PipelineError(str(e))
    print(json.dumps(output, indent=2))
    return 0


def cmd_validate_witness(args: argparse.Namespace) -> int:
    """chainlink #27: G1a/G1b over witness specs, plus G2 -- the query
    must resolve to a `pure: true` query in the concept's own spec --
    and the canonical results under ci/results/witnesses/, whose
    value_domain and value_hash are recomputed.

    Crate-scoped like validate-interaction: a witness is over one
    concept's query, and a concept lives in a crate. G18 coverage (#29),
    G19 determinism (#30) and G20 degeneracy (#31) are separate gates and
    are deliberately not here."""
    descriptor = load_project_descriptor(args.descriptor)
    workspace = _require_workspace_root_exists(args.workspace)
    findings_total = []
    discovered = 0
    for crate in descriptor["crates"]:
        crate_root = _require_crate_root_exists(crate, workspace)
        specs_search_root = workspace / crate["specs_search_root"]
        discovered += _count_witnesses(crate_root)
        findings_total.extend(
            validate_witness_crate(crate_root, _witness_dir_for(crate, workspace), specs_search_root)
        )

    findings_total.extend(validate_witness_results(workspace))
    discovered += _count_witness_results(workspace)

    errors = [f for f in findings_total if f.severity == "error"]
    infos = [f for f in findings_total if f.severity == "info"]

    if infos:
        print(f"INFO: {len(infos)} non-blocking finding(s)")
        for f in infos:
            print(f"  - {f}")

    if not errors:
        print(pass_line(
            discovered, "witness artifacts", "G1a/G1b and G2 pure-query resolution", workspace
        ))
        return 0

    print(f"FAIL: {len(errors)} finding(s)")
    for f in errors:
        print(f"  - {f}")
    return 1


def cmd_validate_gold_set(args: argparse.Namespace) -> int:
    # Workspace-level like validate-closure: one gold set per cluster,
    # and a cluster spans crates. No descriptor is needed to resolve
    # anything here -- the track check belongs to measurement, which is
    # where a descriptor is actually read.
    workspace_root = _require_workspace_root_exists(args.workspace)
    findings = validate_gold_set_workspace(workspace_root, _gold_set_dir_for(workspace_root))

    if not findings:
        print(pass_line(
            _count_gold_sets(workspace_root), "gold sets",
            "G1a/G1b (scope, provenance and edge discipline)", workspace_root,
        ))
        return 0

    print(f"FAIL: {len(findings)} finding(s)")
    for f in findings:
        print(f"  - {f}")
    return 1


def cmd_measure_gold_set(args: argparse.Namespace) -> int:
    """chainlink #26: measure the accepted interaction set against a
    human gold set -- precision, recall, and the omission count that
    keeps the other two honest.

    Not a gate: a non-zero omission count is a finding a human reads, not
    a block. What fails is being unable to measure at all."""
    descriptor = load_project_descriptor(args.descriptor)
    workspace = _require_workspace_root_exists(args.workspace)
    measurements, findings = measure_gold_set_workspace(workspace, descriptor, write=not args.no_write)
    return report_gold_set_measurements(measurements, findings, workspace)


def cmd_check_bridges(args: argparse.Namespace) -> int:
    """Stage 8A (chainlink #47): compile every promoted bridge's
    bridge_logic to a harness for the verifier that owns its cluster,
    write it under ci/harness/, dispatch it to the configured
    verifier_backends command, and record the verdict at
    ci/results/bridge_checks/<bridge_id>.json.

    Generation always happens; dispatch happens only where a backend is
    configured. Anything that could not be checked is a finding and no
    record -- a bridge with no record blocks at gate-g9, which is the
    honest outcome for a bridge nothing verified."""
    descriptor = load_project_descriptor(args.descriptor)
    workspace = _require_workspace_root_exists(args.workspace)
    written, findings = check_bridges_g9(workspace, descriptor)

    for path in written:
        print(f"wrote {path}")
    if not findings:
        print(f"OK: {len(written)} artifact(s) written; every promoted bridge compiled and was dispatched")
        return 0

    print(f"FAIL: {len(findings)} finding(s)")
    for finding in findings:
        print(f"  - {finding}")
    return 1


def cmd_gate_g9(args: argparse.Namespace) -> int:
    """Stage 8A's G9 (chainlink #47): recompile each promoted bridge and
    check that the recorded verifier result is demonstrably about THAT
    bridge -- harness on disk unchanged from the recompilation, recorded
    harness hash equal to the recomputed one, owning verifier, passing
    claim, and no work-package assurance report asserting a bridge_record
    that disagrees with the check."""
    descriptor = load_project_descriptor(args.descriptor)
    workspace = _require_workspace_root_exists(args.workspace)
    findings, discovered = gate_g9_workspace(workspace, descriptor)
    return report_g9_findings(findings, discovered, workspace)


def cmd_validate_closure(args: argparse.Namespace) -> int:
    # Workspace-level like validate-evidence: specs/_closure/ holds one
    # profile (and at most one degradation record) per cluster, and a
    # cluster spans crates by construction. No descriptor is needed.
    workspace_root = _require_workspace_root_exists(args.workspace)
    findings = validate_closure_workspace(workspace_root, _closure_dir_for(workspace_root))

    if not findings:
        print(pass_line(
            _count_closure_artifacts(workspace_root), "closure artifacts",
            "G1a/G1b and G17 profile/record consistency", workspace_root,
        ))
        return 0

    print(f"FAIL: {len(findings)} finding(s)")
    for f in findings:
        print(f"  - {f}")
    return 1


def cmd_gate_g14(args: argparse.Namespace) -> int:
    """Stage 8C's release closure (chainlink #25). Fails closed on
    nothing to close: a release gate that reports OK over zero clusters
    would be the #48 vacuity in its most dangerous position.

    Loads the project descriptor so gate_g14_workspace can resolve each
    bridge's own crate's boundary contracts for its G2 check (external
    review, high severity: an earlier version validated bridges with no
    boundary context at all, which degrades G2 to a non-blocking info
    note and let a cluster close over a bridge whose boundary_id resolved
    nowhere)."""
    descriptor = load_project_descriptor(args.descriptor)
    workspace = _require_workspace_root_exists(args.workspace)
    if not _closure_dir_for(workspace).is_dir():
        raise PipelineError(
            f"no closure directory at {_closure_dir_for(workspace)} -- a release gate with "
            "nothing to close is not a pass; author a closure profile per cluster "
            "(docs/closure-profile-schema.json)"
        )
    outcomes, workspace_findings = gate_g14_workspace(workspace, descriptor)
    return report_g14_outcomes(outcomes, workspace_findings)


def cmd_gate_g18(args: argparse.Namespace) -> int:
    """Stage 4's G18 (chainlink #29): every query marked
    witness_required has a witness spec and a generated rendering,
    checked against the declared feature set only (plan.md §16.2)."""
    descriptor = load_project_descriptor(args.descriptor)
    workspace = _require_workspace_root_exists(args.workspace)
    findings, discovered = gate_g18_workspace(workspace, descriptor)
    return report_g18_findings(findings, discovered, workspace)


def cmd_gate_g19(args: argparse.Namespace) -> int:
    """Stage 8A/CI's G19 (chainlink #30): dispatch the descriptor's
    witness_backend fresh for every genuinely valid witness spec and
    compare the regenerated value_hash against determinism.value_hash
    -- never render_hash (plan.md §16.2)."""
    descriptor = load_project_descriptor(args.descriptor)
    workspace = _require_workspace_root_exists(args.workspace)
    findings, discovered = gate_g19_workspace(workspace, descriptor)
    return report_g19_findings(findings, discovered, workspace)


def cmd_gate_g20(args: argparse.Namespace) -> int:
    """Stage 4.5's G20 (chainlink #31): every genuinely valid witness
    declaring value_distribution: must-vary must not measure a constant
    result, and fixture_family/coverage_region must agree workspace-wide
    -- both WARN severity (plan.md §12's gate table), exit code
    distinguishing pass/warn/blocked three ways like gate-r1-g16's own
    EXIT_DECISION_REQUIRED tier."""
    descriptor = load_project_descriptor(args.descriptor)
    workspace = _require_workspace_root_exists(args.workspace)
    findings, discovered = gate_g20_workspace(workspace, descriptor)
    return report_g20_findings(findings, discovered, workspace)


def cmd_generate_feature_ledger(args: argparse.Namespace) -> int:
    """Stage 4.5's feature ledger generator (chainlink #34, plan.md
    §16.4): a GENERATED, READ-ONLY projection at
    ci/results/feature_ledger.json, one entry per declared
    (witness_required: true) feature, reusing G18/G19/G20 for real
    rather than re-deriving their dispositions. Never consulted by
    satisfies()/assurance/closure computation; refuses to write a
    schema-invalid result rather than ever producing one."""
    descriptor = load_project_descriptor(args.descriptor)
    workspace = _require_workspace_root_exists(args.workspace)
    try:
        destination = write_feature_ledger(workspace, descriptor)
    except FeatureLedgerGenerationError as e:
        raise PipelineError(str(e))
    count = len(json.loads(destination.read_text())["features"])
    if count == 0:
        print(f"wrote {destination} (0 declared features -- nothing to check)")
    else:
        print(f"wrote {destination} ({count} declared feature(s))")
    return 0


def cmd_generate_contact_sheet(args: argparse.Namespace) -> int:
    """Stage 4.5's contact sheet generator (chainlink #32, plan.md
    §16.3): a GENERATED, READ-ONLY review surface at
    docs/witnesses/_contact_sheet.svg, one panel per declared
    (witness_required: true) feature, built over a fresh
    generate_feature_ledger.generate_ledger() call -- never a stale
    ci/results/feature_ledger.json. Same reuse discipline as
    generate-feature-ledger: refuses to build over an ambiguous
    declaration or a ledger that fails its own schema, and writes
    atomically so a failed generation leaves the previous complete
    contact sheet untouched."""
    descriptor = load_project_descriptor(args.descriptor)
    workspace = _require_workspace_root_exists(args.workspace)
    try:
        destination, count = write_contact_sheet(workspace, descriptor)
    except FeatureLedgerGenerationError as e:
        raise PipelineError(str(e))
    if count == 0:
        print(f"wrote {destination} (0 declared features -- nothing to check)")
    else:
        print(f"wrote {destination} ({count} declared feature(s))")
    return 0


def cmd_gate_r1_g16(args: argparse.Namespace) -> int:
    """Stage 8A's R1 + G16 (chainlink #24). Exit codes are three-valued,
    matching plan.md §9.1's own three dispositions: 0 pass, 1 blocked
    (critical/high unresolved, or a definite cross-concept call absent
    from I), 3 a human risk decision is outstanding. A medium tier is
    never collapsed into either neighbour -- that collapse is exactly
    what §9.1 rejects in both directions."""
    descriptor = load_project_descriptor(args.descriptor)
    workspace = _require_workspace_root_exists(args.workspace)
    if not _callsite_report_dir_for(workspace).is_dir():
        raise PipelineError(
            f"no C_static reports at {_callsite_report_dir_for(workspace)} -- run "
            "`pipeline.py extract-c-static` first; an empty reconciliation is not a pass"
        )
    interaction_dirs = {
        crate["crate_dir"]: _interaction_dir_for(crate, workspace) for crate in descriptor["crates"]
    }
    findings, counts = gate_r1_g16_workspace(workspace, interaction_dirs)
    return report_r1_g16_findings(findings, counts)


def _require_workspace_root_exists(workspace: Path) -> Path:
    if not workspace.is_dir():
        raise PipelineError(f"workspace root does not exist or is not a directory: {workspace}")
    return workspace


def cmd_validate_evidence(args: argparse.Namespace) -> int:
    # Evidence is workspace-level, not crate-scoped (see
    # project_descriptor.evidence_dir_for's own docstring) -- one scan,
    # not a per-crate loop the way validate-interaction/-exemption/
    # -protocol-debt work. No project descriptor is needed at all: there
    # is nothing crate-specific to resolve.
    workspace_root = _require_workspace_root_exists(args.workspace)
    findings = validate_evidence_workspace(workspace_root, _evidence_dir_for(workspace_root))

    if not findings:
        print(pass_line(_count_evidence(workspace_root), "evidence records", "G1a/G1b", workspace_root))
        return 0

    print(f"FAIL: {len(findings)} finding(s)")
    for f in findings:
        print(f"  - {f}")
    return 1


def cmd_validate_conflict_resolution(args: argparse.Namespace) -> int:
    # Same workspace-level scope as cmd_validate_evidence. G11 (unresolved
    # conflicts block) and the evidence cross-reference are both real,
    # fail-closed checks here -- not deferred the way #19's G15 initially
    # (and wrongly) was.
    workspace_root = _require_workspace_root_exists(args.workspace)
    evidence_ids = valid_evidence_ids(_evidence_dir_for(workspace_root))
    findings = validate_conflict_resolution_workspace(
        workspace_root, _conflict_dir_for(workspace_root), evidence_ids
    )

    errors = [f for f in findings if f.severity == "error"]
    infos = [f for f in findings if f.severity == "info"]

    if infos:
        print(f"INFO: {len(infos)} non-blocking finding(s)")
        for f in infos:
            print(f"  - {f}")

    if not errors:
        print(pass_line(
            _count_conflicts(workspace_root), "conflict-resolution records", "G1a/G1b/G11", workspace_root
        ))
        return 0

    print(f"FAIL: {len(errors)} finding(s)")
    for f in errors:
        print(f"  - {f}")
    return 1


def cmd_draft(args: argparse.Namespace) -> int:
    descriptor = load_project_descriptor(args.descriptor)
    _require_target_in_workspace(args.target, args.workspace, descriptor)
    backend = descriptor.get("llm_backend", {"kind": "manual"})

    template_path = PROMPTS / f"stage-{args.stage}-{args.template_name}.md"
    if not template_path.exists():
        raise PipelineError(f"no template at {template_path}")

    variables = dict(pair.split("=", 1) for pair in args.var)
    prompt = render_prompt(template_path, variables)

    if backend["kind"] == "manual":
        print(prompt)
        print("\n--- paste the model's JSON-only response, then EOF (ctrl-D) ---", file=sys.stderr)
        raw = sys.stdin.read()
    else:
        raw = invoke_llm_backend(backend, prompt)

    data = parse_llm_json_output(raw)
    draft_path = stage_draft(data, args.target)
    print(f"staged draft: {draft_path}")

    # plan.md §6.1: generated output is immediately run through G1a/G1b
    # for fast local feedback, full Stage 4 adjudication unchanged. The
    # draft stays on disk either way -- this is feedback for correction,
    # not a promotion decision (only approve() writes the target path).
    validate_fn = _select_draft_validate_fn(args.target, args.workspace, descriptor)
    findings = validate_fn(args.target, data)
    errors = [f for f in findings if getattr(f, "severity", "error") == "error"]
    infos = [f for f in findings if getattr(f, "severity", "error") == "info"]

    if infos:
        print(f"INFO: {len(infos)} non-blocking finding(s)")
        for f in infos:
            print(f"  - {f}")

    if errors:
        print(f"FAIL: generated draft has {len(errors)} G1a/G1b finding(s) -- fix before approve()")
        for f in errors:
            print(f"  - {f}")
        return 1

    print("OK: generated draft passes G1a/G1b immediate checks")
    return 0


def _crate_for(target: Path, workspace: Path, descriptor: dict) -> dict | None:
    for crate in descriptor["crates"]:
        crate_root = (workspace / crate["crate_dir"]).resolve()
        try:
            target.resolve().relative_to(crate_root)
        except ValueError:
            continue
        return crate
    return None


def _specs_search_root_for(target: Path, workspace: Path, descriptor: dict) -> Path | None:
    crate = _crate_for(target, workspace, descriptor)
    return workspace / crate["specs_search_root"] if crate else None


def _require_target_in_workspace(target: Path, workspace: Path, descriptor: dict) -> None:
    """Draft/approve targets used to be accepted verbatim -- args.target
    with no check it belonged to the workspace or any declared crate at
    all (external review finding, medium severity). A path outside the
    project's own declared scope is refused outright, not just silently
    processed.

    Chainlink #20: conflict-resolution records are workspace-level, not
    crate-scoped (project_descriptor.conflict_dir_for's own docstring --
    plan.md §7's artifact_manifest worked example places
    specs/_conflicts/EC-004.json with no crate prefix). The crate-only
    check below would incorrectly refuse a legitimate conflict-resolution
    target in any project whose crate_dir isn't literally "." -- fixed by
    also accepting the one recognized workspace-level artifact location,
    not just crate membership.

    External review, medium severity: evidence/ (project_descriptor.
    evidence_dir_for) is the *other* workspace-level artifact type from
    #20 and was missing from this same fix -- with a real crate_dir like
    "crate_a", `pipeline.py draft` refused the canonical
    evidence/E-0001.json target outright, even though evidence records
    are meant to be staged via `draft` (Stage 0). Reproduced directly
    before fixing. Evidence is still never routed through `approve()`
    (see scripts/validate_evidence.py's own docstring for why), so this
    only needs to widen the *draft*-time in-bounds check, not
    `_select_validate_fn`'s dispatcher, which `cmd_draft` never calls."""
    try:
        target.resolve().relative_to(workspace.resolve())
    except ValueError:
        raise PipelineError(f"target {target} is outside the workspace {workspace} -- refusing")
    if _crate_for(target, workspace, descriptor) is not None:
        return
    if target.resolve().parent == _conflict_dir_for(workspace):
        return
    if target.resolve().parent == _evidence_dir_for(workspace):
        return
    raise PipelineError(
        f"target {target} does not belong to any crate declared in the project "
        "descriptor, and is not a recognized workspace-level artifact location "
        "either -- refusing to draft/approve outside a declared scope"
    )


def _select_validate_fn(target: Path, workspace: Path, descriptor: dict):
    """Dispatch by the specs/_<kind>/ directory convention used throughout
    plan.md -- extensible to interaction/witness/etc. validators once M3/M4
    give them schemas; only boundary contracts exist to validate today.

    Raises for anything this dispatcher doesn't recognize -- a third review
    pass (2026-08-27) found the previous version returned SKIP_VALIDATION
    for *any* unmatched path, which converted "unknown artifact type" into
    a silent bypass: a boundary placed under a typo'd `_boundary/` (missing
    the trailing s) matched nothing, got SKIP_VALIDATION, and was approved
    with zero gating. Reproduced end to end. This dispatcher's job is to
    recognize known artifact types and refuse everything else outright --
    "no validator exists yet" must be a hard stop on the approval path, not
    a reason to let it through. When M3/M4 add real validators for other
    artifact types, they extend this if/elif chain; until then, this
    pipeline simply cannot approve those artifact types, which is correct.

    A FOURTH review pass (2026-08-30) found that "recognized" was still too
    loose: `"_boundaries" in target.parts` matches a `_boundaries` component
    ANYWHERE in the path, not the crate's actual declared layout --
    `crate_a/not_specs/_boundaries/x.json` and `crate_a/specs/nested/_boundaries/x.json`
    both matched and promoted successfully. Neither is flagged by G1b's own
    "flat" check either, since that only checks the file sits directly
    inside a directory literally named `_boundaries` -- it has no opinion
    on where `_boundaries` itself sits. Fixed by anchoring to the exact
    expected path: target.parent must equal <crate_dir>/specs/_boundaries,
    not merely contain that name somewhere upstream.

    A FIFTH review pass (2026-09-01) found this directory-anchoring check
    never looked at the target's own suffix: a schema-valid interaction or
    exemption approved as e.g. `_interactions/I-X-001.yaml` matched by
    directory alone, got a real validator, and was written straight
    through to a `.yaml` path -- despite every schema/docstring in this
    codebase documenting `*.json` as the canonical extension for all three
    artifact types. Reproduced end to end via `approve`. Fixed by refusing
    any non-.json target up front, before the directory match even runs.

    A SIXTH review pass (chainlink #31) added witness specs at
    <crate_dir>/specs/_witnesses/*.json: `specs/_witnesses/` was not a
    recognized artifact type at all, so a witness could never be
    promoted through `approve()`, and G20's own "warn now, block at
    promotion if unresolved" gate-table disposition had nothing on the
    promotion side to plug into. Unlike every other branch here, this
    one's validate_fn (gate_g20.validate_witness_for_approval) needs the
    WHOLE workspace, not just the one document being promoted -- G20's
    checks are inherently cross-witness the same way G9/G14/G18/G19 are,
    which none of this dispatcher's other validators have needed before."""
    if target.suffix != ".json":
        raise PipelineError(
            f"target {target} is not a .json file -- this pipeline only "
            "recognizes .json artifacts for boundary contracts, interactions, "
            "exemptions, protocol-debt records, bridges, and conflict-resolution records"
        )
    # Chainlink #20: conflict-resolution records are workspace-level, not
    # crate-scoped -- checked before the crate-anchored block below, not
    # nested inside it, since a target here need not belong to any crate
    # at all (see _require_target_in_workspace's own note on the same gap).
    if target.resolve().parent == _conflict_dir_for(workspace):
        validator = load_conflict_resolution_validator()
        evidence_ids = valid_evidence_ids(_evidence_dir_for(workspace))
        return lambda path, data: validate_conflict_resolution_data(path, data, validator, evidence_ids)
    crate = _crate_for(target, workspace, descriptor)
    if crate is not None:
        resolved_parent = target.resolve().parent
        if resolved_parent == _boundary_dir_for(crate, workspace):
            validator = load_boundary_validator()
            specs_search_root = _specs_search_root_for(target, workspace, descriptor)
            return lambda path, data: validate_boundary_data(path, data, validator, specs_search_root)
        if resolved_parent == _interaction_dir_for(crate, workspace):
            validator = load_interaction_validator()
            crate_root = (workspace / crate["crate_dir"]).resolve()
            specs_search_root = _specs_search_root_for(target, workspace, descriptor)
            interactions_by_id = load_interactions_by_id(_interaction_dir_for(crate, workspace))
            valid_debt_interaction_ids = valid_interaction_ids_from_crate(
                crate_root, _protocol_debt_dir_for(crate, workspace), interactions_by_id
            )
            covering_boundary_edges = valid_boundary_edges_from_crate(
                crate_root, _boundary_dir_for(crate, workspace), specs_search_root
            )
            valid_exemption_interaction_ids = valid_exemption_interaction_ids_from_crate(
                crate_root, _exemption_dir_for(crate, workspace), interactions_by_id
            )
            return lambda path, data: validate_interaction_data(
                path, data, validator, valid_debt_interaction_ids, covering_boundary_edges,
                valid_exemption_interaction_ids,
            )
        if resolved_parent == _exemption_dir_for(crate, workspace):
            validator = load_exemption_validator()
            interactions_by_id = load_interactions_by_id(_interaction_dir_for(crate, workspace))
            return lambda path, data: validate_exemption_data(path, data, validator, interactions_by_id)
        if resolved_parent == _protocol_debt_dir_for(crate, workspace):
            validator = load_protocol_debt_validator()
            interactions_by_id = load_interactions_by_id(_interaction_dir_for(crate, workspace))
            return lambda path, data: validate_protocol_debt_data(path, data, validator, interactions_by_id)
        if resolved_parent == _bridge_dir_for(crate, workspace):
            validator = load_bridge_validator()
            boundary_specs_search_root = _specs_search_root_for(target, workspace, descriptor)
            boundaries_by_id = load_boundaries_by_id(
                _boundary_dir_for(crate, workspace), boundary_specs_search_root
            )
            return lambda path, data: validate_bridge_data(path, data, validator, boundaries_by_id)
        if resolved_parent == _witness_dir_for(crate, workspace):
            # chainlink #31: unlike every branch above, this validate_fn
            # needs the WHOLE workspace, not just this one document --
            # G20's degeneracy checks (must-vary vs constant, cross-witness
            # fixture-family consistency) are inherently workspace-scoped,
            # the same way G9/G14/G18/G19 are and no other approve()-time
            # validator here has needed to be. workspace/descriptor are
            # already in scope in this function and simply close over them.
            validator = load_witness_validator()
            specs_search_root = _specs_search_root_for(target, workspace, descriptor)
            return lambda path, data: validate_witness_for_approval(
                path, data, validator, specs_search_root, workspace, descriptor
            )
    raise PipelineError(
        f"no validator recognizes target {target} -- this pipeline only "
        "validates boundary contracts at <crate_dir>/specs/_boundaries/*.json, "
        "interactions at <crate_dir>/specs/_interactions/*.json, exemptions "
        "at <crate_dir>/specs/_exemptions/*.json, protocol-debt records at "
        "<crate_dir>/specs/_protocol_debt/*.json, bridges at "
        "<crate_dir>/specs/_bridges/*.json, witnesses at "
        "<crate_dir>/specs/_witnesses/*.json, and conflict-resolution records "
        "at specs/_conflicts/*.json (workspace-level) today. Evidence records at "
        "evidence/*.json (workspace-level) are a valid draft target but are never "
        "approved -- they carry no review block, so approve() cannot promote them. "
        "Refusing to draft/approve an artifact type or location it cannot "
        "mechanically gate, rather than silently skipping validation for it."
    )


def _select_draft_validate_fn(target: Path, workspace: Path, descriptor: dict):
    """Immediate Stage 0/3 feedback validator for a freshly generated
    draft -- plan.md §6.1: "the output ... is immediately run through
    G1a/G1b for fast local feedback." cmd_draft used to skip this
    entirely (external review, high severity: a generated evidence
    record missing the required `origin` block staged with return code
    0). A second review pass, also high severity, found the first fix's
    approach -- delegating to _select_validate_fn, the same dispatcher
    approve() uses -- was itself wrong: boundary/interaction/exemption/
    protocol-debt/conflict-resolution all require `review` at the schema's
    own top level (docs/*-schema.json), but a Stage 0/3 draft never has
    one yet (review_checkpoint.stage_draft() writes the model's raw
    output; review_checkpoint.approve() is the only thing that attaches
    `review`, after a human reviewer signs off). Reusing the approve-time
    validator meant *every* non-evidence draft failed immediate
    validation unconditionally, and also ran Stage-4-only cross-file
    gates (G2+, G15, G11, evidence/interaction cross-references) that
    plan.md §6.1 never asked for at draft time. Reproduced directly for
    boundary, interaction, and a resolved conflict-resolution draft
    before this fix.

    Each validate_<type>.py module now owns a dedicated
    validate_draft_data()/load_draft_validator() pair: schema validation
    with `review` treated as not-yet-required (schema_utils.
    make_validator_without_required, applied recursively so a nested
    if/then like conflict-resolution's status=="resolved" conditional is
    covered too), an explicit rejection of a model-supplied `review`
    block, and only the naming/shape (G1b) checks that need no cross-file
    context -- see each module's own validate_draft_data() docstring for
    exactly what it excludes and why. This dispatcher's location-matching
    logic (which directory belongs to which artifact type) intentionally
    mirrors _select_validate_fn's -- the two must never silently diverge
    on that -- but calls the draft-time validator, not the approve-time
    one."""
    if target.suffix != ".json":
        raise PipelineError(
            f"target {target} is not a .json file -- this pipeline only "
            "recognizes .json artifacts for boundary contracts, interactions, "
            "exemptions, protocol-debt records, bridges, evidence, and conflict-resolution records"
        )
    if target.resolve().parent == _evidence_dir_for(workspace):
        validator = load_evidence_validator()
        return lambda path, data: validate_evidence_data(path, data, validator)
    if target.resolve().parent == _conflict_dir_for(workspace):
        validator = load_conflict_resolution_draft_validator()
        return lambda path, data: validate_conflict_resolution_draft_data(path, data, validator)
    crate = _crate_for(target, workspace, descriptor)
    if crate is not None:
        resolved_parent = target.resolve().parent
        if resolved_parent == _boundary_dir_for(crate, workspace):
            validator = load_boundary_draft_validator()
            return lambda path, data: validate_boundary_draft_data(path, data, validator)
        if resolved_parent == _interaction_dir_for(crate, workspace):
            validator = load_interaction_draft_validator()
            return lambda path, data: validate_interaction_draft_data(path, data, validator)
        if resolved_parent == _exemption_dir_for(crate, workspace):
            validator = load_exemption_draft_validator()
            return lambda path, data: validate_exemption_draft_data(path, data, validator)
        if resolved_parent == _protocol_debt_dir_for(crate, workspace):
            validator = load_protocol_debt_draft_validator()
            return lambda path, data: validate_protocol_debt_draft_data(path, data, validator)
        if resolved_parent == _bridge_dir_for(crate, workspace):
            validator = load_bridge_draft_validator()
            return lambda path, data: validate_bridge_draft_data(path, data, validator)
    raise PipelineError(
        f"no draft validator recognizes target {target} -- this pipeline only "
        "validates drafts for boundary contracts at <crate_dir>/specs/_boundaries/*.json, "
        "interactions at <crate_dir>/specs/_interactions/*.json, exemptions "
        "at <crate_dir>/specs/_exemptions/*.json, protocol-debt records at "
        "<crate_dir>/specs/_protocol_debt/*.json, bridges at "
        "<crate_dir>/specs/_bridges/*.json, evidence at evidence/*.json "
        "(workspace-level), and conflict-resolution records at specs/_conflicts/*.json "
        "(workspace-level) today."
    )


def _select_pair_validate_fn(
    interaction_target: Path,
    protocol_debt_target: Path,
    workspace: Path,
    descriptor: dict,
):
    """Build the transaction validator for a new non-pairwise I + debt pair.

    Single-artifact validators intentionally consult only promoted sibling
    artifacts.  This callback is the explicit bootstrap path: it validates
    both drafts against a combined view containing the candidate interaction
    and candidate debt record, while still requiring each side's own full
    schema/naming/review gates.
    """
    interaction_crate = _crate_for(interaction_target, workspace, descriptor)
    debt_crate = _crate_for(protocol_debt_target, workspace, descriptor)
    if interaction_crate is None or debt_crate is None:
        raise PipelineError("paired approval targets must belong to declared crates")
    if interaction_crate["crate_dir"] != debt_crate["crate_dir"]:
        raise PipelineError("paired interaction and protocol-debt targets must belong to the same crate")

    interaction_dir = _interaction_dir_for(interaction_crate, workspace)
    debt_dir = _protocol_debt_dir_for(interaction_crate, workspace)
    if interaction_target.suffix != ".json" or interaction_target.resolve().parent != interaction_dir:
        raise PipelineError(
            f"paired approval interaction target must be exactly under {interaction_dir} as a .json file"
        )
    if protocol_debt_target.suffix != ".json" or protocol_debt_target.resolve().parent != debt_dir:
        raise PipelineError(
            f"paired approval protocol-debt target must be exactly under {debt_dir} as a .json file"
        )

    crate_root = (workspace / interaction_crate["crate_dir"]).resolve()
    specs_search_root = workspace / interaction_crate["specs_search_root"]
    interaction_validator = load_interaction_validator()
    debt_validator = load_protocol_debt_validator()

    def validate_pair(candidates: dict[Path, dict]) -> dict[Path, list]:
        candidate_interaction = candidates[interaction_target]
        candidate_debt = candidates[protocol_debt_target]

        interaction_id = candidate_interaction.get("interaction_id")
        debt_interaction_id = candidate_debt.get("interaction_id")
        if not (
            isinstance(interaction_id, str)
            and interaction_id == debt_interaction_id
            and interaction_target.stem == interaction_id
            and protocol_debt_target.stem == interaction_id
        ):
            raise ApprovalRefused(
                "paired approval requires the interaction and protocol-debt body IDs "
                "and filename stems to match; refusing to grant candidate G15 coverage"
            )

        interactions_by_id = load_interactions_by_id(interaction_dir)

        candidate_id = interaction_id
        if isinstance(candidate_id, str) and candidate_id in interactions_by_id.duplicate_ids:
            raise ApprovalRefused(
                f"interaction_id {candidate_id!r} has duplicate canonical candidates; refusing paired approval"
            )

        # If this is an update, replace the old promoted version in the
        # transaction view.  A new pair has no entry to replace.
        transaction_interactions = dict(interactions_by_id)
        if isinstance(candidate_id, str):
            transaction_interactions.pop(candidate_id, None)

        existing_coverage = valid_interaction_ids_from_crate(crate_root, debt_dir, interactions_by_id)
        interaction_coverage = set(existing_coverage)
        if isinstance(candidate_id, str):
            # The debt draft is validated below in the same transaction.  It
            # is safe to let G15 see this candidate ID here because the pair
            # is not committed unless the debt draft also passes.
            interaction_coverage.add(candidate_id)

        # R2 (chainlink #46): a non-pairwise interaction can independently
        # be boundary-required, so it needs the same coverage check any
        # other interaction does -- from the crate's EXISTING promoted
        # boundaries/exemptions, not from anything in this transaction.
        covering_boundary_edges = valid_boundary_edges_from_crate(
            crate_root, _boundary_dir_for(interaction_crate, workspace), specs_search_root
        )
        valid_exemption_interaction_ids = valid_exemption_interaction_ids_from_crate(
            crate_root, _exemption_dir_for(interaction_crate, workspace), interactions_by_id
        )

        interaction_findings = validate_interaction_data(
            interaction_target, candidate_interaction, interaction_validator, interaction_coverage,
            covering_boundary_edges, valid_exemption_interaction_ids,
        )
        interaction_errors = [
            finding for finding in interaction_findings if getattr(finding, "severity", "error") == "error"
        ]

        debt_findings: list = []
        if not interaction_errors and isinstance(candidate_id, str):
            transaction_interactions[candidate_id] = candidate_interaction
            debt_findings = validate_protocol_debt_data(
                protocol_debt_target, candidate_debt, debt_validator, transaction_interactions
            )

        return {
            interaction_target: interaction_findings,
            protocol_debt_target: debt_findings,
        }

    return validate_pair


def _select_interaction_exemption_pair_validate_fn(
    interaction_target: Path,
    exemption_target: Path,
    workspace: Path,
    descriptor: dict,
):
    """Build the transaction validator for a new interaction + exemption
    pair -- chainlink #46's own bootstrap gap, mirrors
    _select_pair_validate_fn exactly (interaction + protocol-debt) but for
    R2's OTHER coverage source.

    External review, high severity: single-artifact approve() makes R2
    genuinely uncrossable for the exemption route -- an interaction can't
    be approved without an ALREADY-PROMOTED reviewed exemption covering
    it (R2), and an exemption can't be approved without the interaction
    it names ALREADY being a promoted, boundary-required interaction
    (its own G2 cross-reference, which only ever consults
    load_interactions_by_id's promoted lookup, never a draft). Neither
    can go first through approve() alone -- reproduced directly both
    orders before this fix. This callback validates both drafts against
    a combined view containing the candidate interaction and candidate
    exemption, the same bootstrap discipline _select_pair_validate_fn
    already established for non-pairwise interaction + protocol-debt."""
    interaction_crate = _crate_for(interaction_target, workspace, descriptor)
    exemption_crate = _crate_for(exemption_target, workspace, descriptor)
    if interaction_crate is None or exemption_crate is None:
        raise PipelineError("paired approval targets must belong to declared crates")
    if interaction_crate["crate_dir"] != exemption_crate["crate_dir"]:
        raise PipelineError("paired interaction and exemption targets must belong to the same crate")

    interaction_dir = _interaction_dir_for(interaction_crate, workspace)
    exemption_dir = _exemption_dir_for(interaction_crate, workspace)
    if interaction_target.suffix != ".json" or interaction_target.resolve().parent != interaction_dir:
        raise PipelineError(
            f"paired approval interaction target must be exactly under {interaction_dir} as a .json file"
        )
    if exemption_target.suffix != ".json" or exemption_target.resolve().parent != exemption_dir:
        raise PipelineError(
            f"paired approval exemption target must be exactly under {exemption_dir} as a .json file"
        )

    crate_root = (workspace / interaction_crate["crate_dir"]).resolve()
    specs_search_root = workspace / interaction_crate["specs_search_root"]
    debt_dir = _protocol_debt_dir_for(interaction_crate, workspace)
    interaction_validator = load_interaction_validator()
    exemption_validator = load_exemption_validator()

    def validate_pair(candidates: dict[Path, dict]) -> dict[Path, list]:
        candidate_interaction = candidates[interaction_target]
        candidate_exemption = candidates[exemption_target]

        interaction_id = candidate_interaction.get("interaction_id")
        exemption_interaction_id = candidate_exemption.get("interaction_id")
        if not (
            isinstance(interaction_id, str)
            and interaction_id == exemption_interaction_id
            and interaction_target.stem == interaction_id
            and exemption_target.stem == interaction_id
        ):
            raise ApprovalRefused(
                "paired approval requires the interaction and exemption body IDs "
                "and filename stems to match; refusing to grant candidate R2 coverage"
            )

        interactions_by_id = load_interactions_by_id(interaction_dir)

        candidate_id = interaction_id
        if isinstance(candidate_id, str) and candidate_id in interactions_by_id.duplicate_ids:
            raise ApprovalRefused(
                f"interaction_id {candidate_id!r} has duplicate canonical candidates; refusing paired approval"
            )

        # If this is an update, replace the old promoted version in the
        # transaction view.  A new pair has no entry to replace.
        transaction_interactions = dict(interactions_by_id)
        if isinstance(candidate_id, str):
            transaction_interactions.pop(candidate_id, None)

        # G15 coverage is unaffected by this transaction (no protocol-debt
        # record involved here) -- from the crate's existing promoted state.
        valid_debt_interaction_ids = valid_interaction_ids_from_crate(crate_root, debt_dir, interactions_by_id)
        # Boundary coverage is likewise unaffected -- a boundary contract
        # isn't part of this pairing.
        covering_boundary_edges = valid_boundary_edges_from_crate(
            crate_root, _boundary_dir_for(interaction_crate, workspace), specs_search_root
        )

        existing_exemption_coverage = valid_exemption_interaction_ids_from_crate(
            crate_root, exemption_dir, interactions_by_id
        )
        exemption_coverage = set(existing_exemption_coverage)
        if isinstance(candidate_id, str):
            # The exemption draft is validated below in the same
            # transaction.  It is safe to let R2 see this candidate ID
            # here because the pair is not committed unless the exemption
            # draft also passes.
            exemption_coverage.add(candidate_id)

        interaction_findings = validate_interaction_data(
            interaction_target, candidate_interaction, interaction_validator, valid_debt_interaction_ids,
            covering_boundary_edges, exemption_coverage,
        )
        interaction_errors = [
            finding for finding in interaction_findings if getattr(finding, "severity", "error") == "error"
        ]

        exemption_findings: list = []
        if not interaction_errors and isinstance(candidate_id, str):
            transaction_interactions[candidate_id] = candidate_interaction
            exemption_findings = validate_exemption_data(
                exemption_target, candidate_exemption, exemption_validator, transaction_interactions
            )

        return {
            interaction_target: interaction_findings,
            exemption_target: exemption_findings,
        }

    return validate_pair


def cmd_approve(args: argparse.Namespace) -> int:
    descriptor = load_project_descriptor(args.descriptor)
    _require_target_in_workspace(args.target, args.workspace, descriptor)
    validate_fn = _select_validate_fn(args.target, args.workspace, descriptor)

    draft_path = args.target.with_suffix(args.target.suffix + ".draft")
    try:
        result = checkpoint_approve(
            draft_path, args.target, reviewer=args.reviewer, reviewed_at=args.reviewed_at, validate_fn=validate_fn
        )
    except ApprovalRefused as e:
        raise PipelineError(str(e))
    print(f"approved: {result.target_path} ({result.classification}) by {result.reviewer} at {result.reviewed_at}")
    return 0


def cmd_approve_pair(args: argparse.Namespace) -> int:
    descriptor = load_project_descriptor(args.descriptor)
    _require_target_in_workspace(args.interaction_target, args.workspace, descriptor)
    _require_target_in_workspace(args.protocol_debt_target, args.workspace, descriptor)
    validate_fn = _select_pair_validate_fn(
        args.interaction_target, args.protocol_debt_target, args.workspace, descriptor
    )

    targets = (args.interaction_target, args.protocol_debt_target)
    drafts = tuple(target.with_suffix(target.suffix + ".draft") for target in targets)
    try:
        results = checkpoint_approve_pair(
            drafts,
            targets,
            reviewer=args.reviewer,
            reviewed_at=args.reviewed_at,
            validate_fn=validate_fn,
        )
    except ApprovalRefused as e:
        raise PipelineError(str(e))
    for result in results:
        print(f"approved: {result.target_path} ({result.classification}) by {result.reviewer} at {result.reviewed_at}")
    return 0


def cmd_approve_exemption_pair(args: argparse.Namespace) -> int:
    descriptor = load_project_descriptor(args.descriptor)
    _require_target_in_workspace(args.interaction_target, args.workspace, descriptor)
    _require_target_in_workspace(args.exemption_target, args.workspace, descriptor)
    validate_fn = _select_interaction_exemption_pair_validate_fn(
        args.interaction_target, args.exemption_target, args.workspace, descriptor
    )

    targets = (args.interaction_target, args.exemption_target)
    drafts = tuple(target.with_suffix(target.suffix + ".draft") for target in targets)
    try:
        results = checkpoint_approve_pair(
            drafts,
            targets,
            reviewer=args.reviewer,
            reviewed_at=args.reviewed_at,
            validate_fn=validate_fn,
        )
    except ApprovalRefused as e:
        raise PipelineError(str(e))
    for result in results:
        print(f"approved: {result.target_path} ({result.classification}) by {result.reviewer} at {result.reviewed_at}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    print(
        "Implemented: draft (Stage 0/3), approve (checkpoint), "
        "approve-pair (transactional interaction + protocol-debt checkpoint), "
        "approve-exemption-pair (transactional interaction + exemption checkpoint, R2's exemption bootstrap), "
        "validate (Stage 4 G1a/G1b/G2+), "
        "validate-interaction (Stage 4 G1a/G1b + computed eligibility + G15 + R2), "
        "validate-exemption (Stage 4 G1a/G1b naming + G2 interaction cross-reference), "
        "validate-protocol-debt (Stage 4 G1a/G1b + interaction cross-reference), "
        "validate-bridge (Stage 4 G1a/G1b/G2 over bridge specifications, chainlink #22), "
        "validate-evidence (Stage 4 G1a/G1b, workspace-level), "
        "validate-conflict-resolution (Stage 4 G1a/G1b/G11, workspace-level), "
        "validate-work-package (Stage 7 schema + §10.1, standalone), "
        "validate-promotion (Stage 4.5 schema + §7.1, standalone), "
        "accept-promotion (Stage 4.5's own generator, chainlink #45), "
        "extract-c-static (Stage 8A's coarse syntactic C_static extractor, chainlink #24), "
        "validate-callsites (Stage 8A G1a/G1b over C_static reports, workspace-level), "
        "gate-r1-g16 (Stage 8A R1 reconciliation + G16 risk-tiered unresolved policy; "
        "exit 3 means a human risk decision is outstanding), "
        "render-witness (dispatch a canonical result through the renderer contract, chainlink #28), "
        "validate-witness (G1a/G1b/G2 over witness specs + canonical results, chainlink #27), "
        "validate-gold-set (G1a/G1b over human gold sets, chainlink #26), "
        "measure-gold-set (precision/recall/omission against a human gold set, chainlink #26), "
        "check-bridges (Stage 8A bridge harness generation + verifier dispatch, chainlink #47), "
        "gate-g9 (Stage 8A bridge-check verification against a recompilation of the promoted bridge), "
        "validate-closure (Stage 8C G1a/G1b + G17 over closure profiles and degradation records), "
        "gate-g14 (Stage 8C release closure over the transitive assurance graph, per cluster, "
        "with the CG6 well-foundedness discharge, chainlink #25), "
        "gate-g18 (Stage 4 witness coverage over the declared witness_required feature set, "
        "chainlink #29), "
        "gate-g19 (Stage 8A/CI witness determinism: regenerate, compare value_hash, never "
        "render_hash, chainlink #30), "
        "gate-g20 (Stage 4.5 degeneracy: must-vary vs constant, fixture-family/coverage_region "
        "consistency, warn -- block at promotion if unresolved, chainlink #31), "
        "generate-feature-ledger (Stage 4.5 generated projection over G18/G19/G20, "
        "implementation_observed vs assurance_status never merged, chainlink #34), "
        "generate-contact-sheet (Stage 4.5 review surface with the ∃-witness evidence banner, "
        "traffic-light witness/determinism/degeneracy columns kept visually distinct from a "
        "never-green assurance_status/closure_kind column, chainlink #32)"
    )
    print("Not yet implemented:")
    for stage, ref in NOT_YET_IMPLEMENTED.items():
        print(f"  - {stage}: {ref}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", type=Path, default=Path("."))
    parser.add_argument(
        "--descriptor",
        type=Path,
        default=None,
        help="Defaults to <workspace>/project-descriptor.json",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    validate_p = sub.add_parser("validate", help="Stage 4: G1a/G1b/G2+ over boundary contracts")
    validate_p.set_defaults(func=cmd_validate)

    validate_interaction_p = sub.add_parser(
        "validate-interaction", help="Stage 4: G1a/G1b + computed eligibility over interaction (I) specs"
    )
    validate_interaction_p.set_defaults(func=cmd_validate_interaction)

    validate_exemption_p = sub.add_parser(
        "validate-exemption", help="Stage 4: G1a/G1b over boundary-required exemption objects"
    )
    validate_exemption_p.set_defaults(func=cmd_validate_exemption)

    validate_protocol_debt_p = sub.add_parser(
        "validate-protocol-debt", help="Stage 4: G1a/G1b over protocol-debt records"
    )
    validate_protocol_debt_p.set_defaults(func=cmd_validate_protocol_debt)

    validate_bridge_p = sub.add_parser(
        "validate-bridge", help="Stage 4: G1a/G1b/G2 over bridge specifications (chainlink #22)"
    )
    validate_bridge_p.set_defaults(func=cmd_validate_bridge)

    validate_evidence_p = sub.add_parser(
        "validate-evidence", help="Stage 4: G1a/G1b over evidence records (workspace-level)"
    )
    validate_evidence_p.set_defaults(func=cmd_validate_evidence)

    validate_conflict_resolution_p = sub.add_parser(
        "validate-conflict-resolution",
        help="Stage 4: G1a/G1b/G11 over evidence conflict-resolution records (workspace-level)",
    )
    validate_conflict_resolution_p.set_defaults(func=cmd_validate_conflict_resolution)

    validate_wp_p = sub.add_parser(
        "validate-work-package", help="Stage 7: schema + §10.1 checks over a work-package manifest"
    )
    validate_wp_p.add_argument("manifest", type=Path)
    validate_wp_p.add_argument(
        "--specs-search-root",
        type=Path,
        default=None,
        help="Root to resolve trusted_assumptions[].assumption_ref against (optional).",
    )
    validate_wp_p.set_defaults(func=cmd_validate_work_package)

    validate_promo_p = sub.add_parser(
        "validate-promotion", help="Stage 4.5: schema + §7.1 checks over a promotion receipt"
    )
    validate_promo_p.add_argument("receipt", type=Path)
    validate_promo_p.set_defaults(func=cmd_validate_promotion)

    accept_promo_p = sub.add_parser(
        "accept-promotion",
        help="Stage 4.5's own generator: deterministically compute and write a promotion receipt (chainlink #45)",
    )
    accept_promo_p.add_argument("cluster")
    accept_promo_p.add_argument("--reviewer", required=True)
    accept_promo_p.add_argument("--accepted-at", default=None)
    accept_promo_p.add_argument(
        "--policy-path", required=True,
        help=(
            "workspace-relative path to the accepted policy document (e.g. docs/reliance-policy.md) "
            "-- must itself be one of --artifact; its own 'Policy version: <name>@<major>.<minor>' "
            "line is read to compute policy_version"
        ),
    )
    accept_promo_p.add_argument(
        "--artifact", action="append", default=[], required=True,
        help="workspace-relative path being accepted into this promotion, repeatable",
    )
    accept_promo_p.set_defaults(func=cmd_accept_promotion)

    draft_p = sub.add_parser("draft", help="Stage 0/3: one-shot LLM draft")
    draft_p.add_argument("stage", choices=["0", "3"])
    draft_p.add_argument("template_name", help="e.g. evidence-intake, boundary-drafting")
    draft_p.add_argument("target", type=Path, help="Target artifact path the draft will eventually promote to")
    draft_p.add_argument("--var", action="append", default=[], help="key=value, repeatable")
    draft_p.set_defaults(func=cmd_draft)

    approve_p = sub.add_parser("approve", help="Review checkpoint: promote a draft")
    approve_p.add_argument("target", type=Path)
    approve_p.add_argument("--reviewer", required=True)
    approve_p.add_argument("--reviewed-at", default=None)
    approve_p.set_defaults(func=cmd_approve)

    approve_pair_p = sub.add_parser(
        "approve-pair",
        help="Review and atomically promote a new non-pairwise interaction plus its protocol-debt record",
    )
    approve_pair_p.add_argument("interaction_target", type=Path)
    approve_pair_p.add_argument("protocol_debt_target", type=Path)
    approve_pair_p.add_argument("--reviewer", required=True)
    approve_pair_p.add_argument("--reviewed-at", default=None)
    approve_pair_p.set_defaults(func=cmd_approve_pair)

    approve_exemption_pair_p = sub.add_parser(
        "approve-exemption-pair",
        help=(
            "Review and atomically promote a new interaction plus a reviewed exemption covering it "
            "(R2's exemption bootstrap, chainlink #46)"
        ),
    )
    approve_exemption_pair_p.add_argument("interaction_target", type=Path)
    approve_exemption_pair_p.add_argument("exemption_target", type=Path)
    approve_exemption_pair_p.add_argument("--reviewer", required=True)
    approve_exemption_pair_p.add_argument("--reviewed-at", default=None)
    approve_exemption_pair_p.set_defaults(func=cmd_approve_exemption_pair)

    extract_c_static_p = sub.add_parser(
        "extract-c-static",
        help="Stage 8A: extract C_static call sites per crate into ci/results/c_static (chainlink #24)",
    )
    extract_c_static_p.add_argument(
        "--target", required=True,
        help="Rust target triple this observation is relative to -- required, never guessed",
    )
    extract_c_static_p.add_argument("--feature", action="append", default=[])
    extract_c_static_p.add_argument("--cfg", action="append", default=[])
    extract_c_static_p.set_defaults(func=cmd_extract_c_static)

    validate_callsites_p = sub.add_parser(
        "validate-callsites",
        help="Stage 8A: G1a/G1b over C_static reports, incl. recomputed coverage (workspace-level)",
    )
    validate_callsites_p.set_defaults(func=cmd_validate_callsites)

    gate_r1_g16_p = sub.add_parser(
        "gate-r1-g16",
        help="Stage 8A: reconcile C_static against I (R1) and apply the unresolved risk policy (G16)",
    )
    gate_r1_g16_p.set_defaults(func=cmd_gate_r1_g16)

    render_witness_p = sub.add_parser(
        "render-witness",
        help="Dispatch a witness's canonical result through the renderer contract (chainlink #28)",
    )
    render_witness_p.add_argument("witness_id", help="e.g. W-TQ-LOAD-FACTOR")
    render_witness_p.add_argument("--renderer", required=True, help="the DECLARED renderer to dispatch to")
    render_witness_p.set_defaults(func=cmd_render_witness)

    validate_witness_p = sub.add_parser(
        "validate-witness",
        help="G1a/G1b/G2 over witness specs and canonical results (chainlink #27)",
    )
    validate_witness_p.set_defaults(func=cmd_validate_witness)

    validate_gold_set_p = sub.add_parser(
        "validate-gold-set",
        help="G1a/G1b over human gold sets (chainlink #26)",
    )
    validate_gold_set_p.set_defaults(func=cmd_validate_gold_set)

    measure_gold_set_p = sub.add_parser(
        "measure-gold-set",
        help="Measure I against a human gold set: precision, recall, omission (chainlink #26)",
    )
    measure_gold_set_p.add_argument("--no-write", action="store_true")
    measure_gold_set_p.set_defaults(func=cmd_measure_gold_set)

    check_bridges_p = sub.add_parser(
        "check-bridges",
        help="Stage 8A: compile bridge_logic to a harness, dispatch it to the owning verifier (#47)",
    )
    check_bridges_p.set_defaults(func=cmd_check_bridges)

    gate_g9_p = sub.add_parser(
        "gate-g9",
        help="Stage 8A: G9 -- the recorded bridge check must be about the bridge it names (#47)",
    )
    gate_g9_p.set_defaults(func=cmd_gate_g9)

    validate_closure_p = sub.add_parser(
        "validate-closure",
        help="Stage 8C: G1a/G1b + G17 over closure profiles and degradation records (chainlink #25)",
    )
    validate_closure_p.set_defaults(func=cmd_validate_closure)

    gate_g14_p = sub.add_parser(
        "gate-g14",
        help="Stage 8C: G14 transitive closure + CG6 discharge, per cluster (chainlink #25)",
    )
    gate_g14_p.set_defaults(func=cmd_gate_g14)

    gate_g18_p = sub.add_parser(
        "gate-g18",
        help="Stage 4: G18 -- every witness_required query has a witness spec and rendering (#29)",
    )
    gate_g18_p.set_defaults(func=cmd_gate_g18)

    gate_g19_p = sub.add_parser(
        "gate-g19",
        help="Stage 8A/CI: G19 -- regenerate and compare a witness's value_hash, never render_hash (#30)",
    )
    gate_g19_p.set_defaults(func=cmd_gate_g19)

    gate_g20_p = sub.add_parser(
        "gate-g20",
        help="Stage 4.5: G20 -- must-vary vs constant, fixture-family consistency, warn (#31)",
    )
    gate_g20_p.set_defaults(func=cmd_gate_g20)

    generate_feature_ledger_p = sub.add_parser(
        "generate-feature-ledger",
        help="Stage 4.5: generate ci/results/feature_ledger.json from G18/G19/G20 (#34)",
    )
    generate_feature_ledger_p.set_defaults(func=cmd_generate_feature_ledger)

    generate_contact_sheet_p = sub.add_parser(
        "generate-contact-sheet",
        help="Stage 4.5: generate docs/witnesses/_contact_sheet.svg, the review surface (#32)",
    )
    generate_contact_sheet_p.set_defaults(func=cmd_generate_contact_sheet)

    status_p = sub.add_parser("status", help="What this CLI can and can't do yet")
    status_p.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    if args.descriptor is None:
        args.descriptor = args.workspace / "project-descriptor.json"

    try:
        return args.func(args)
    except PipelineError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
