"""Pilot-cluster selection rubric (chainlink #4, plan.md §14).

Built on real workspaces on disk -- concept specs run through the real,
vendored concept-to-code schema, interactions through
validate_interaction.py's own validator, closure profiles through
validate_closure.py's own G1a/G1b/G17 -- for the same reason every other
gate test file in this codebase gives: a test handing the evaluator
pre-loaded dicts would not be testing the evaluator anyone runs.
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import pipeline  # noqa: E402
from select_pilot_cluster import (  # noqa: E402
    EXIT_NO_ELIGIBLE,
    EXIT_OK,
    ClusterCandidate,
    discover_concepts,
    select_pilot,
)
from test_validate_closure import valid_profile  # noqa: E402

REVIEW = {"reviewer": "alice", "reviewed_at": "2026-09-09"}


def concept_spec(name: str, cluster: str, verifier: str = "creusot", kind: str = "struct") -> dict:
    """A genuinely schema-valid concept-to-code spec (struct/trait
    shape). English strings are kept unique per concept only insofar as
    the schema requires minimum length -- content is otherwise
    irrelevant to this rubric."""
    return {
        "schema_version": "1.0",
        "concept": name,
        "cluster": cluster,
        "kind": kind,
        "english_description": f"The {name} concept, used for pilot-selection regression coverage.",
        "queries": [
            {"english": "What is the current observed value?", "rust_sig": "fn value(&self) -> f64", "pure": True}
        ],
        "commands": [],
        "constraints": [
            {"english": "The observed value is always finite and non-negative.", "logic": "self.value() >= 0.0"}
        ],
        "adversary_table": [
            {"scenario": "value becomes negative", "violates": "non-negative invariant", "resolution": "reject"}
        ],
        "verifier": verifier,
    }


def enum_concept_spec(name: str, cluster: str, trait_concept: str, wrapped_concept: str, wrapped_crate: str) -> dict:
    """A genuinely schema-valid kind='enum' concept -- a distinct shape
    (trait_ref/variants, no queries/commands/constraints/adversary_table)
    from the struct/trait shape concept_spec() above."""
    return {
        "schema_version": "1.0",
        "concept": name,
        "cluster": cluster,
        "kind": "enum",
        "english_description": f"The {name} enum concept, used for pilot-selection regression coverage.",
        "trait_ref": {"crate": wrapped_crate, "concept": trait_concept},
        "variants": [{"name": "FastPath", "wraps": {"crate": wrapped_crate, "concept": wrapped_concept}}],
        "verifier": "creusot",
    }


def interaction(interaction_id: str, caller: str, callee: str, protocol_class: str = "pairwise") -> dict:
    """A genuinely schema-valid interaction with `inform` eligibility
    (edge_class: import-only) -- deliberately avoids `boundary-required`
    so no reliances/obligations need fabricating; this rubric's own
    interaction discovery leaves R2/G15 coverage unsupplied regardless
    (see select_pilot_cluster.discover_interactions's own docstring)."""
    return {
        "schema_version": "1.0",
        "interaction_id": interaction_id,
        "caller": {"concept": caller, "method": "read"},
        "callee": {"concept": callee, "method": "read"},
        "edge_class": ["import-only"],
        "eligibility": "inform",
        "rationale": "regression fixture edge",
        "protocol_class": protocol_class,
        "realization": {
            "requirement": "required",
            "config_scope": {"target": "x86_64-unknown-linux-gnu", "features": ["default"], "cfg": []},
        },
        "review": dict(REVIEW),
    }


class Workspace:
    def __init__(self, root: Path, crate_dir: str = "crates/example", specs_search_root: str = "crates"):
        self.root = root
        self.crate_dir = crate_dir
        self.crate = {"crate_dir": crate_dir, "contracts_crate": "contracts", "specs_search_root": specs_search_root}
        self.descriptor = {"crates": [self.crate]}
        (self.root / crate_dir / "specs" / "_interactions").mkdir(parents=True, exist_ok=True)

    def write(self, relative: str, data: dict) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
        return path

    def write_concept(self, name: str, cluster: str, verifier: str = "creusot", kind: str = "struct", filename: str | None = None) -> Path:
        return self.write(f"{self.crate_dir}/specs/{filename or name}.json", concept_spec(name, cluster, verifier, kind))

    def write_raw(self, filename: str, data: dict) -> Path:
        return self.write(f"{self.crate_dir}/specs/{filename}", data)

    def write_interaction(self, interaction_id: str, caller: str, callee: str, protocol_class: str = "pairwise") -> Path:
        return self.write(
            f"{self.crate_dir}/specs/_interactions/{interaction_id}.json",
            interaction(interaction_id, caller, callee, protocol_class),
        )

    def write_closure_profile(self, cluster: str, generic: bool = True) -> Path:
        profile = valid_profile()
        profile["cluster"] = cluster
        profile["work_packages"] = ["WP-X"]
        profile["conditions"]["generic_callees_type_universal_or_creusot_owned"] = generic
        return self.write(f"specs/_closure/{cluster}.json", profile)

    def select(self):
        return select_pilot(self.root, self.descriptor)


class TmpWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def candidate(self, ranked_or_candidates, cluster) -> ClusterCandidate:
        return next(c for c in ranked_or_candidates if c.cluster == cluster)


class EligibleSelectionTest(TmpWorkspaceTest):
    def test_a_fully_qualifying_cluster_is_selected(self):
        self.ws.write_concept("TreeNode", "phylo-tree")
        self.ws.write_concept("Clade", "phylo-tree")
        self.ws.write_interaction("I-TREE-001", "TreeNode", "Clade")
        self.ws.write_closure_profile("phylo-tree", generic=True)

        candidates, ranked, findings = self.ws.select()

        self.assertEqual(findings, [])
        self.assertEqual(len(ranked), 1)
        c = ranked[0]
        self.assertEqual(c.cluster, "phylo-tree")
        self.assertTrue(c.eligible)
        self.assertEqual(c.reasons, ())
        self.assertEqual(c.concept_count, 2)
        self.assertEqual(c.verifiers, ("creusot",))
        self.assertEqual(c.intra_edge_count, 1)
        self.assertEqual(c.deductive_closure_value, 1)
        self.assertEqual(c.generic_status, "non-generic")


class MultipleVerifierExclusionTest(TmpWorkspaceTest):
    def test_two_verifiers_in_one_cluster_excludes_it(self):
        self.ws.write_concept("TreeNode", "phylo-tree", verifier="creusot")
        self.ws.write_concept("Clade", "phylo-tree", verifier="verus")
        self.ws.write_interaction("I-TREE-001", "TreeNode", "Clade")
        self.ws.write_closure_profile("phylo-tree", generic=True)

        candidates, ranked, findings = self.ws.select()

        self.assertEqual(ranked, [])
        c = self.candidate(candidates, "phylo-tree")
        self.assertFalse(c.eligible)
        self.assertEqual(c.verifiers, ("creusot", "verus"))
        self.assertTrue(any("not exactly one verifier" in r for r in c.reasons), c.reasons)
        self.assertEqual(c.deductive_closure_value, 0)

    def test_kani_only_cluster_is_single_verifier_but_not_deductive(self):
        """single_verifier passes (exactly one verifier: kani), but kani
        is bounded model checking, not deductive -- deductive_closure_value
        stays 0 and the cluster is still excluded, for a DIFFERENT
        reason than the multi-verifier case above."""
        self.ws.write_concept("TreeNode", "phylo-tree", verifier="kani")
        self.ws.write_concept("Clade", "phylo-tree", verifier="kani")
        self.ws.write_interaction("I-TREE-001", "TreeNode", "Clade")
        self.ws.write_closure_profile("phylo-tree", generic=True)

        candidates, ranked, findings = self.ws.select()

        self.assertEqual(ranked, [])
        c = self.candidate(candidates, "phylo-tree")
        self.assertEqual(c.verifiers, ("kani",))
        self.assertEqual(c.deductive_closure_value, 0)
        self.assertTrue(any("not a deductive verifier" in r for r in c.reasons), c.reasons)
        self.assertFalse(any("not exactly one verifier" in r for r in c.reasons), c.reasons)


class NonPairwiseExclusionTest(TmpWorkspaceTest):
    def test_a_non_pairwise_intra_cluster_edge_excludes_the_cluster(self):
        self.ws.write_concept("TreeNode", "phylo-tree")
        self.ws.write_concept("Clade", "phylo-tree")
        self.ws.write_interaction("I-TREE-001", "TreeNode", "Clade", protocol_class="non-pairwise")
        self.ws.write_closure_profile("phylo-tree", generic=True)

        candidates, ranked, findings = self.ws.select()

        self.assertEqual(ranked, [])
        c = self.candidate(candidates, "phylo-tree")
        self.assertEqual(c.non_pairwise_edges, ("I-TREE-001",))
        self.assertTrue(any("non-pairwise intra-cluster interaction" in r and "I-TREE-001" in r for r in c.reasons), c.reasons)


class GenericExclusionTest(TmpWorkspaceTest):
    def test_declared_generic_excludes_the_cluster(self):
        self.ws.write_concept("TreeNode", "phylo-tree")
        self.ws.write_concept("Clade", "phylo-tree")
        self.ws.write_interaction("I-TREE-001", "TreeNode", "Clade")
        self.ws.write_closure_profile("phylo-tree", generic=False)

        candidates, ranked, findings = self.ws.select()

        self.assertEqual(ranked, [])
        c = self.candidate(candidates, "phylo-tree")
        self.assertEqual(c.generic_status, "generic")
        self.assertTrue(any("declared generic" in r for r in c.reasons), c.reasons)

    def test_unknown_generic_status_excludes_rather_than_assumes_non_generic(self):
        """The central fail-closed requirement: no closure profile at
        all must NOT be treated as 'non-generic' by default."""
        self.ws.write_concept("TreeNode", "phylo-tree")
        self.ws.write_concept("Clade", "phylo-tree")
        self.ws.write_interaction("I-TREE-001", "TreeNode", "Clade")
        # deliberately: no write_closure_profile() call at all.

        candidates, ranked, findings = self.ws.select()

        self.assertEqual(ranked, [])
        c = self.candidate(candidates, "phylo-tree")
        self.assertEqual(c.generic_status, "unknown")
        self.assertTrue(any("non-generic status unknown" in r for r in c.reasons), c.reasons)


class EnumBearingExclusionTest(TmpWorkspaceTest):
    def test_an_enum_kind_concept_excludes_its_cluster(self):
        self.ws.write_concept("Clade", "phylo-tree")
        self.ws.write_concept("Dispatcher", "phylo-tree")  # the trait this enum wraps a variant of
        self.ws.write_raw(
            "TreeShape.json",
            enum_concept_spec("TreeShape", "phylo-tree", trait_concept="Dispatcher", wrapped_concept="Clade", wrapped_crate="example"),
        )
        self.ws.write_interaction("I-TREE-001", "Clade", "Dispatcher")
        self.ws.write_closure_profile("phylo-tree", generic=True)

        candidates, ranked, findings = self.ws.select()

        self.assertEqual(ranked, [])
        c = self.candidate(candidates, "phylo-tree")
        self.assertEqual(c.enum_concepts, ("TreeShape",))
        self.assertTrue(any("enum-kind concept" in r and "TreeShape" in r for r in c.reasons), c.reasons)


class RankingAndTieBreakTest(TmpWorkspaceTest):
    def test_smaller_deductive_closure_value_ranks_first(self):
        # small: 2 concepts, 1 intra-edge.
        self.ws.write_concept("TreeNode", "small-cluster")
        self.ws.write_concept("Clade", "small-cluster")
        self.ws.write_interaction("I-SMALL-001", "TreeNode", "Clade")
        self.ws.write_closure_profile("small-cluster", generic=True)

        # big: 3 concepts, 2 intra-edges.
        self.ws.write_concept("ChainState", "big-cluster")
        self.ws.write_concept("Proposal", "big-cluster")
        self.ws.write_concept("Acceptance", "big-cluster")
        self.ws.write_interaction("I-BIG-001", "ChainState", "Proposal")
        self.ws.write_interaction("I-BIG-002", "Proposal", "Acceptance")
        self.ws.write_closure_profile("big-cluster", generic=True)

        candidates, ranked, findings = self.ws.select()

        self.assertEqual([c.cluster for c in ranked], ["small-cluster", "big-cluster"])
        self.assertEqual(ranked[0].deductive_closure_value, 1)
        self.assertEqual(ranked[1].deductive_closure_value, 2)

    def test_a_tie_in_deductive_closure_value_breaks_on_concept_count_then_name(self):
        # Both clusters: 1 intra-edge each (tied value). "aaa-cluster"
        # has fewer concepts (2) than "bbb-cluster" (3, with one concept
        # not touching the edge) -- concept_count must be the tie-break,
        # not cluster-name order alone (both would agree here, so a
        # second scenario below isolates name-only tie-breaking).
        self.ws.write_concept("A1", "aaa-cluster")
        self.ws.write_concept("A2", "aaa-cluster")
        self.ws.write_interaction("I-A-001", "A1", "A2")
        self.ws.write_closure_profile("aaa-cluster", generic=True)

        self.ws.write_concept("B1", "bbb-cluster")
        self.ws.write_concept("B2", "bbb-cluster")
        self.ws.write_concept("B3", "bbb-cluster")
        self.ws.write_interaction("I-B-001", "B1", "B2")
        self.ws.write_closure_profile("bbb-cluster", generic=True)

        candidates, ranked, findings = self.ws.select()

        self.assertEqual([c.cluster for c in ranked], ["aaa-cluster", "bbb-cluster"])
        self.assertEqual(ranked[0].deductive_closure_value, ranked[1].deductive_closure_value)
        self.assertLess(ranked[0].concept_count, ranked[1].concept_count)

    def test_a_full_tie_on_value_and_concept_count_breaks_on_cluster_name(self):
        self.ws.write_concept("Z1", "zzz-cluster")
        self.ws.write_concept("Z2", "zzz-cluster")
        self.ws.write_interaction("I-Z-001", "Z1", "Z2")
        self.ws.write_closure_profile("zzz-cluster", generic=True)

        self.ws.write_concept("A1", "aaa-cluster")
        self.ws.write_concept("A2", "aaa-cluster")
        self.ws.write_interaction("I-A-001", "A1", "A2")
        self.ws.write_closure_profile("aaa-cluster", generic=True)

        candidates, ranked, findings = self.ws.select()

        self.assertEqual([c.cluster for c in ranked], ["aaa-cluster", "zzz-cluster"])


class FileOrderIndependenceTest(TmpWorkspaceTest):
    def test_selection_is_independent_of_concept_and_interaction_write_order(self):
        # Same scenario as RankingAndTieBreakTest's first case, but
        # concepts/interactions for the "big" cluster are written FIRST
        # and filenames sort AFTER the small cluster's own -- discovery
        # is sorted by path and grouped by dict key (cluster/concept
        # name), never by write/insertion order.
        self.ws.write_concept("ZChainState", "big-cluster")
        self.ws.write_concept("ZProposal", "big-cluster")
        self.ws.write_concept("ZAcceptance", "big-cluster")
        self.ws.write_interaction("I-BIG-002", "ZProposal", "ZAcceptance")
        self.ws.write_interaction("I-BIG-001", "ZChainState", "ZProposal")
        self.ws.write_closure_profile("big-cluster", generic=True)

        self.ws.write_concept("ATreeNode", "small-cluster")
        self.ws.write_concept("AClade", "small-cluster")
        self.ws.write_interaction("I-SMALL-001", "ATreeNode", "AClade")
        self.ws.write_closure_profile("small-cluster", generic=True)

        candidates, ranked, findings = self.ws.select()

        self.assertEqual([c.cluster for c in ranked], ["small-cluster", "big-cluster"])

    def test_concept_discovery_is_independent_of_glob_order_across_two_crates_sharing_one_root(self):
        crate_a = {"crate_dir": "crates/aaa", "contracts_crate": "contracts", "specs_search_root": "crates"}
        crate_b = {"crate_dir": "crates/zzz", "contracts_crate": "contracts", "specs_search_root": "crates"}
        descriptor = {"crates": [crate_b, crate_a]}  # deliberately reversed from alphabetical
        (self.ws.root / "crates/aaa/specs").mkdir(parents=True, exist_ok=True)
        (self.ws.root / "crates/zzz/specs").mkdir(parents=True, exist_ok=True)
        self.ws.write("crates/aaa/specs/Alpha.json", concept_spec("Alpha", "shared-cluster"))
        self.ws.write("crates/zzz/specs/Beta.json", concept_spec("Beta", "shared-cluster"))

        concepts, findings = discover_concepts(descriptor, self.ws.root)

        self.assertEqual(findings, [])
        self.assertEqual(sorted(concepts), ["Alpha", "Beta"])


class InvalidOrAmbiguousMembershipTest(TmpWorkspaceTest):
    def test_only_the_valid_concept_survives_beside_an_invalid_sibling(self):
        broken = concept_spec("TreeNode", "phylo-tree")
        del broken["verifier"]
        self.ws.write_raw("TreeNode.json", broken)
        self.ws.write_concept("Clade", "phylo-tree")

        candidates, ranked, findings = self.ws.select()

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].cluster, "phylo-tree")
        self.assertEqual(candidates[0].concept_count, 1)
        self.assertTrue(any("schema-invalid" in str(f) for f in findings), findings)

    def test_the_same_concept_declared_by_two_valid_specs_is_ambiguous_not_first_wins(self):
        self.ws.write_concept("TreeNode", "phylo-tree", filename="TreeNode")
        self.ws.write_concept("TreeNode", "other-cluster", filename="TreeNodeDuplicate")
        self.ws.write_concept("Clade", "phylo-tree")
        self.ws.write_interaction("I-TREE-001", "TreeNode", "Clade")
        self.ws.write_closure_profile("phylo-tree", generic=True)

        candidates, ranked, findings = self.ws.select()

        self.assertTrue(
            any("declared by more than one genuinely valid concept spec" in str(f) for f in findings), findings
        )
        # TreeNode is unresolved everywhere -- phylo-tree has only Clade
        # (concept_count 1), and the interaction referencing TreeNode is
        # reported as unresolved rather than silently attributed to
        # either candidate cluster.
        c = self.candidate(candidates, "phylo-tree")
        self.assertEqual(c.concept_count, 1)
        self.assertEqual(c.intra_edge_count, 0)
        self.assertTrue(any("does not resolve to any genuinely valid" in str(f) for f in findings), findings)

    def test_an_interaction_naming_an_unresolvable_concept_is_reported_not_silently_dropped(self):
        self.ws.write_concept("TreeNode", "phylo-tree")
        self.ws.write_interaction("I-TREE-001", "TreeNode", "Ghost")

        candidates, ranked, findings = self.ws.select()

        self.assertTrue(
            any("callee concept 'Ghost' does not resolve" in str(f) for f in findings), findings
        )
        c = self.candidate(candidates, "phylo-tree")
        self.assertEqual(c.intra_edge_count, 0)


class NoEligibleCandidatesTest(TmpWorkspaceTest):
    def test_every_candidate_excluded_reports_reasons_and_empty_ranking(self):
        self.ws.write_concept("TreeNode", "phylo-tree", verifier="creusot")
        self.ws.write_concept("Clade", "phylo-tree", verifier="verus")
        self.ws.write_interaction("I-TREE-001", "TreeNode", "Clade")
        # no closure profile either -- doubly excluded.

        candidates, ranked, findings = self.ws.select()

        self.assertEqual(ranked, [])
        self.assertEqual(len(candidates), 1)
        self.assertFalse(candidates[0].eligible)
        self.assertGreaterEqual(len(candidates[0].reasons), 2)

    def test_cli_exits_no_eligible_with_zero_candidates_at_all(self):
        descriptor_path = self.ws.write("project-descriptor.json", {
            "schema_version": "1.0",
            "project": {"name": "example-empty", "crate_naming_convention": "^example-empty-[a-z]+"},
            "mode": "greenfield",
            "crates": [self.ws.crate],
            "verifier_policy": {"default": "creusot"},
            "compatibility_policy": {"reliance_policy_path": "docs/reliance-policy.md"},
            "write_set": {"allowed_roots": ["crates/*/src/"], "protected_roots": ["scripts/**"]},
            "gate_integrity": [],
            "llm_backend": {"kind": "manual"},
            "review": dict(REVIEW),
        })
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            rc = pipeline.main([
                "--workspace", str(self.ws.root), "--descriptor", str(descriptor_path), "select-pilot-cluster",
            ])
        self.assertEqual(rc, EXIT_NO_ELIGIBLE)
        self.assertIn("No eligible candidate cluster", buffer.getvalue())


class TwoIndependentProjectsTest(TmpWorkspaceTest):
    """chainlink #4's own requirement: the identical evaluator, applied
    fresh and independently to a Mode R (greenfield) project's inputs
    and a Mode P (port) project's inputs, must select each project's OWN
    pilot from its OWN data -- never a shared or hardcoded name.

    This repository holds no real downstream Rust-target project and no
    real C++->Rust port project (confirmed: no `crates/` directory
    exists anywhere outside a test's own temporary workspace) -- the
    same honestly-labeled-stand-in precedent chainlink #26's own Mode P
    gold-set fixture and #24's never-compiled callsite fixtures already
    set. These two scenarios are visibly-synthetic project inputs, not
    a claim that either is a real project."""

    def test_mode_r_and_mode_p_each_select_their_own_pilot_independently(self):
        with tempfile.TemporaryDirectory() as tmp_r, tempfile.TemporaryDirectory() as tmp_p:
            ws_r = Workspace(Path(tmp_r), crate_dir="crates/greenfield-core")
            ws_r.write_concept("Scheduler", "scheduling-core", verifier="creusot")
            ws_r.write_concept("TaskQueue", "scheduling-core", verifier="creusot")
            ws_r.write_interaction("I-R-001", "Scheduler", "TaskQueue")
            ws_r.write_closure_profile("scheduling-core", generic=True)

            ws_p = Workspace(Path(tmp_p), crate_dir="crates/port-core")
            ws_p.write_concept("ParserState", "parsing-oracle", verifier="verus")
            ws_p.write_concept("TokenStream", "parsing-oracle", verifier="verus")
            ws_p.write_concept("Lexeme", "parsing-oracle", verifier="verus")
            ws_p.write_interaction("I-P-001", "ParserState", "TokenStream")
            ws_p.write_interaction("I-P-002", "TokenStream", "Lexeme")
            ws_p.write_closure_profile("parsing-oracle", generic=True)

            _, ranked_r, findings_r = ws_r.select()
            _, ranked_p, findings_p = ws_p.select()

        self.assertEqual(findings_r, [])
        self.assertEqual(findings_p, [])
        self.assertEqual(len(ranked_r), 1)
        self.assertEqual(len(ranked_p), 1)
        self.assertEqual(ranked_r[0].cluster, "scheduling-core")
        self.assertEqual(ranked_p[0].cluster, "parsing-oracle")
        # The two runs must not leak state into one another.
        self.assertNotEqual(ranked_r[0].cluster, ranked_p[0].cluster)


class CmdSelectPilotClusterCliTest(TmpWorkspaceTest):
    def _descriptor(self) -> dict:
        return {
            "schema_version": "1.0",
            "project": {"name": "example-greenfield", "crate_naming_convention": "^example-greenfield-[a-z]+"},
            "mode": "greenfield",
            "crates": [self.ws.crate],
            "verifier_policy": {"default": "creusot"},
            "compatibility_policy": {"reliance_policy_path": "docs/reliance-policy.md"},
            "write_set": {"allowed_roots": ["crates/*/src/"], "protected_roots": ["scripts/**"]},
            "gate_integrity": [],
            "llm_backend": {"kind": "manual"},
            "review": dict(REVIEW),
        }

    def test_a_selected_pilot_is_reported_and_exits_zero_through_the_real_cli(self):
        self.ws.write_concept("TreeNode", "phylo-tree")
        self.ws.write_concept("Clade", "phylo-tree")
        self.ws.write_interaction("I-TREE-001", "TreeNode", "Clade")
        self.ws.write_closure_profile("phylo-tree", generic=True)
        descriptor_path = self.ws.write("project-descriptor.json", self._descriptor())

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            rc = pipeline.main([
                "--workspace", str(self.ws.root), "--descriptor", str(descriptor_path), "select-pilot-cluster",
            ])

        self.assertEqual(rc, EXIT_OK)
        printed = buffer.getvalue()
        self.assertIn("Selected pilot: phylo-tree", printed)

    def test_no_eligible_pilot_exits_nonzero_through_the_real_cli(self):
        self.ws.write_concept("TreeNode", "phylo-tree", verifier="creusot")
        self.ws.write_concept("Clade", "phylo-tree", verifier="verus")
        self.ws.write_interaction("I-TREE-001", "TreeNode", "Clade")
        descriptor_path = self.ws.write("project-descriptor.json", self._descriptor())

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            rc = pipeline.main([
                "--workspace", str(self.ws.root), "--descriptor", str(descriptor_path), "select-pilot-cluster",
            ])

        self.assertEqual(rc, EXIT_NO_ELIGIBLE)
        printed = buffer.getvalue()
        self.assertIn("No eligible candidate cluster", printed)
        self.assertIn("not exactly one verifier", printed)

    def test_select_pilot_cluster_is_listed_by_status(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            pipeline.main(["--workspace", str(self.ws.root), "status"])
        self.assertIn("select-pilot-cluster", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
