import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from validate_work_package import validate_data, validate_file, load_validator  # noqa: E402

FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "work_packages" / "valid"
MANIFEST_PATH = FIXTURE_ROOT / "ci" / "manifest" / "WP-SCHED-001.json"
YAML_MANIFEST_PATH = FIXTURE_ROOT / "ci" / "manifest" / "WP-SCHED-001.yaml"
SPECS_SEARCH_ROOT = FIXTURE_ROOT / "crates" / "scheduler" / "specs"


def load_valid() -> dict:
    return json.loads(MANIFEST_PATH.read_text())


def run(
    data: dict,
    workspace_root: Path = FIXTURE_ROOT,
    specs_search_root: Path | None = SPECS_SEARCH_ROOT,
    allowed_boundary_dirs: list[Path] | None = None,
):
    validator = load_validator()
    return validate_data(MANIFEST_PATH, data, validator, workspace_root, specs_search_root, allowed_boundary_dirs)


def errors_of(findings):
    return [f for f in findings if f.severity == "error"]


def infos_of(findings):
    return [f for f in findings if f.severity == "info"]


class ValidWorkPackageTest(unittest.TestCase):
    def test_valid_manifest_has_no_errors(self):
        findings = run(load_valid())
        self.assertEqual(errors_of(findings), [], [str(f) for f in errors_of(findings)])

    def test_valid_manifest_reports_the_two_known_gaps_as_info(self):
        findings = run(load_valid())
        infos = infos_of(findings)
        self.assertEqual(len(infos), 2)
        reasons = " ".join(f.reason for f in infos)
        self.assertIn("promotion_id", reasons)
        self.assertIn("allowed_write_set", reasons)

    def test_canonical_yaml_manifest_validates_too(self):
        """plan.md §10's own worked example is ci/manifest/WP-MCMC-004.yaml
        -- an external review found only .json was ever accepted, so the
        plan's own canonical format failed G1a outright."""
        validator = load_validator()
        findings = validate_file(YAML_MANIFEST_PATH, validator, FIXTURE_ROOT, SPECS_SEARCH_ROOT)
        self.assertEqual(errors_of(findings), [], [str(f) for f in errors_of(findings)])


class G1aFailureTest(unittest.TestCase):
    def test_missing_required_field_is_rejected(self):
        data = load_valid()
        del data["gates"]
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_wildcard_harness_name_is_rejected(self):
        data = load_valid()
        data["definition_of_done"]["provided_guarantees"][0]["harness"] = "task_queue_*"
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_forbidden_action_outside_enum_is_rejected(self):
        data = load_valid()
        data["failure_policy"]["forbidden"].append("do_whatever_you_want")
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))


class GateIntegrityTest(unittest.TestCase):
    def test_hash_mismatch_is_rejected(self):
        data = load_valid()
        data["gate_integrity"][0]["hash"] = "sha256:" + "0" * 64
        findings = run(data)
        errors = errors_of(findings)
        self.assertTrue(any(f.gate == "G13" and "mismatch" in f.reason for f in errors))

    def test_missing_runner_file_is_rejected(self):
        data = load_valid()
        data["gate_integrity"][0]["runner"] = "scripts/does_not_exist.py"
        findings = run(data)
        errors = errors_of(findings)
        self.assertTrue(any(f.gate == "G13" and "not found" in f.reason for f in errors))

    def test_correct_hash_passes(self):
        findings = run(load_valid())
        self.assertFalse(any(f.gate == "G13" for f in errors_of(findings)))

    def test_absolute_path_escaping_the_workspace_is_rejected_even_with_the_correct_hash(self):
        """External review, medium severity: an absolute path silently
        discards the workspace_root join entirely (Path("/a") / "/etc/hosts"
        == Path("/etc/hosts") in pathlib), so a gate_integrity entry could
        point anywhere on disk and still pass if its hash happened to
        match. Reproduced with a real file outside the fixture root."""
        import hashlib
        outside = FIXTURE_ROOT.parent / "outside_workspace_secret.py"
        real_hash = "sha256:" + hashlib.sha256(outside.read_bytes()).hexdigest()

        data = load_valid()
        data["gate_integrity"][0] = {"runner": str(outside), "hash": real_hash}
        findings = run(data)
        errors = errors_of(findings)
        self.assertTrue(any("escapes the workspace" in f.reason for f in errors))

    def test_traversal_escaping_the_workspace_is_rejected(self):
        import hashlib
        outside = FIXTURE_ROOT.parent / "outside_workspace_secret.py"
        real_hash = "sha256:" + hashlib.sha256(outside.read_bytes()).hexdigest()

        data = load_valid()
        data["gate_integrity"][0] = {"runner": "../outside_workspace_secret.py", "hash": real_hash}
        findings = run(data)
        errors = errors_of(findings)
        self.assertTrue(any("escapes the workspace" in f.reason for f in errors))


class GlobOverlapUnitTest(unittest.TestCase):
    """Unit tests on the segment-based intersection check itself, since a
    naive 'shared prefix' heuristic would falsely flag genuinely disjoint
    directory trees -- this must be exact for the grammar this schema
    actually uses (literal segments, *, **)."""

    def test_literal_allowed_inside_wildcarded_protected_overlaps(self):
        from validate_work_package import _patterns_can_overlap
        self.assertTrue(_patterns_can_overlap("scripts/dummy_gate.py", "scripts/**"))

    def test_disjoint_subtrees_under_the_same_wildcarded_parent_do_not_overlap(self):
        """The false-positive case a naive prefix comparison gets wrong:
        same static prefix ("crates/"), genuinely different subtrees."""
        from validate_work_package import _patterns_can_overlap
        self.assertFalse(_patterns_can_overlap("crates/*/src/**", "crates/*/specs/**"))

    def test_directory_prefix_without_explicit_star_star_still_overlaps(self):
        from validate_work_package import _patterns_can_overlap
        self.assertTrue(_patterns_can_overlap("crates/scheduler/src/", "crates/*/**"))

    def test_completely_unrelated_paths_do_not_overlap(self):
        from validate_work_package import _patterns_can_overlap
        self.assertFalse(_patterns_can_overlap("crates/scheduler/src/", "docs/*.md"))

    def test_identical_literal_paths_overlap(self):
        from validate_work_package import _patterns_can_overlap
        self.assertTrue(_patterns_can_overlap("Cargo.toml", "Cargo.toml"))


class WriteSetAnchoringTest(unittest.TestCase):
    """External review, high severity: write-set patterns were never
    checked for escaping the workspace -- the write-policy equivalent of
    the gate_integrity path escape fixed in the previous round."""

    def test_traversal_in_allowed_write_set_is_rejected(self):
        data = load_valid()
        data["write_policy"]["allowed_write_set"] = ["../outside-worktree/**"]
        findings = run(data)
        errors = errors_of(findings)
        self.assertTrue(any("not workspace-relative" in f.reason for f in errors), [str(f) for f in errors])

    def test_absolute_path_in_allowed_write_set_is_rejected(self):
        data = load_valid()
        data["write_policy"]["allowed_write_set"] = ["/etc/**"]
        findings = run(data)
        errors = errors_of(findings)
        self.assertTrue(any("not workspace-relative" in f.reason for f in errors), [str(f) for f in errors])

    def test_traversal_in_protected_write_set_is_also_rejected(self):
        data = load_valid()
        data["write_policy"]["protected_write_set"].append("../elsewhere/**")
        findings = run(data)
        errors = errors_of(findings)
        self.assertTrue(any("not workspace-relative" in f.reason for f in errors), [str(f) for f in errors])

    def test_traversal_deeper_in_the_pattern_is_still_rejected(self):
        data = load_valid()
        data["write_policy"]["allowed_write_set"] = ["crates/../../outside/**"]
        findings = run(data)
        errors = errors_of(findings)
        self.assertTrue(any("not workspace-relative" in f.reason for f in errors), [str(f) for f in errors])

    def test_ordinary_workspace_relative_patterns_pass(self):
        findings = run(load_valid())
        self.assertFalse(any("not workspace-relative" in f.reason for f in errors_of(findings)))


class WriteSetDisjointnessTest(unittest.TestCase):
    def test_the_exact_reported_bypass_is_now_rejected(self):
        """External review: allowed=scripts/dummy_gate.py, protected=scripts/**
        reported zero findings under the old literal-string-equality check."""
        data = load_valid()
        data["write_policy"]["allowed_write_set"] = ["scripts/dummy_gate.py"]
        data["write_policy"]["protected_write_set"] = ["scripts/**"]
        findings = run(data)
        errors = errors_of(findings)
        self.assertTrue(any("overlaps protected" in f.reason for f in errors))

    def test_literal_overlap_is_still_rejected(self):
        data = load_valid()
        data["write_policy"]["protected_write_set"].append("crates/scheduler/src/")
        findings = run(data)
        errors = errors_of(findings)
        self.assertTrue(any("overlaps protected" in f.reason for f in errors))

    def test_genuinely_disjoint_write_sets_pass(self):
        findings = run(load_valid())
        self.assertFalse(any(f.gate == "G13" and "overlaps" in f.reason for f in errors_of(findings)))


class TrustedAssumptionResolutionTest(unittest.TestCase):
    def test_dangling_boundary_reference_is_rejected(self):
        data = load_valid()
        data["definition_of_done"]["trusted_assumptions"][0]["assumption_ref"]["boundary_id"] = "no_such_boundary"
        findings = run(data)
        errors = errors_of(findings)
        self.assertTrue(any("does not resolve" in f.reason for f in errors))

    def test_glob_metacharacters_in_boundary_id_are_not_a_wildcard_bypass(self):
        """External review: changing boundary_id to '*' used to resolve
        successfully because it was interpolated straight into
        Path.glob(). Resolution must be by the boundary contract's own
        declared boundary_id field, matched exactly."""
        data = load_valid()
        data["definition_of_done"]["trusted_assumptions"][0]["assumption_ref"]["boundary_id"] = "*"
        findings = run(data)
        errors = errors_of(findings)
        self.assertTrue(any("does not resolve" in f.reason for f in errors))

    def test_wrong_tracking_issue_is_rejected(self):
        data = load_valid()
        data["definition_of_done"]["trusted_assumptions"][0]["assumption_ref"]["tracking_issue"] = "chainlink:999"
        findings = run(data)
        errors = errors_of(findings)
        self.assertTrue(any("no assumption in it matches" in f.reason for f in errors))

    def test_wrong_hash_is_rejected(self):
        data = load_valid()
        data["definition_of_done"]["trusted_assumptions"][0]["assumption_ref"]["assumption_hash"] = "sha256:" + "9" * 64
        findings = run(data)
        errors = errors_of(findings)
        self.assertTrue(any("no assumption in it matches" in f.reason for f in errors))

    def test_ambiguous_boundary_id_across_two_files_is_rejected(self):
        """Both candidates must be genuinely canonical (correct filename,
        flat layout, full G1a validity) to reach the ambiguity check at
        all -- otherwise this would test 'two garbage files' rejection,
        not 'two real, competing boundary contracts' rejection."""
        with tempfile.TemporaryDirectory() as tmp:
            search_root = Path(tmp)
            for sub in ("crate_a", "crate_b"):
                bdir = search_root / sub / "specs" / "_boundaries"
                bdir.mkdir(parents=True)
                (bdir / "scheduler_dispatch__to__task_queue_pop_ready.json").write_text(json.dumps({
                    "schema_version": "1.0",
                    "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
                    "caller": {"concept": "Scheduler", "method": "dispatch"},
                    "callee": {"concept": "TaskQueue", "method": "pop_ready"},
                    "callee_guarantees": ["TaskQueue.C003"],
                    "assumptions": [{
                        "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
                        "tracking_issue": "chainlink:713",
                        "assumption_hash": "sha256:" + "1" * 64,
                    }],
                    "review": {"reviewer": "example-reviewer", "reviewed_at": "2026-08-30"},
                }))
            findings = run(load_valid(), specs_search_root=search_root)
            errors = errors_of(findings)
            self.assertTrue(any("ambiguously" in f.reason for f in errors), [str(f) for f in errors])

    def test_noncanonical_boundary_placement_is_not_trusted(self):
        """External review, medium severity: a schema-shaped-enough JSON
        file dropped anywhere under a directory literally named
        _boundaries used to resolve successfully -- reproduced with
        junk/not-a-crate/_boundaries/anything.json (wrong filename, and
        missing every other schema-required field)."""
        with tempfile.TemporaryDirectory() as tmp:
            search_root = Path(tmp)
            bdir = search_root / "junk" / "not-a-crate" / "_boundaries"
            bdir.mkdir(parents=True)
            (bdir / "anything.json").write_text(json.dumps({
                "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
                "assumptions": [{
                    "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
                    "tracking_issue": "chainlink:713",
                    "assumption_hash": "sha256:" + "1" * 64,
                }],
            }))
            findings = run(load_valid(), specs_search_root=search_root)
            errors = errors_of(findings)
            self.assertTrue(any("does not resolve" in f.reason for f in errors), [str(f) for f in errors])

    def test_allowed_boundary_dirs_restricts_resolution_to_declared_crates(self):
        """The canonical fixture boundary is genuinely valid and correctly
        placed, but if it's not one of the *declared* crate boundary
        directories, it must not be trusted -- same anchoring discipline
        as pipeline.py's own dispatch fix."""
        elsewhere = SPECS_SEARCH_ROOT.parent.parent.parent / "not_the_declared_crate" / "specs" / "_boundaries"
        findings = run(load_valid(), allowed_boundary_dirs=[elsewhere])
        errors = errors_of(findings)
        self.assertTrue(any("does not resolve" in f.reason for f in errors), [str(f) for f in errors])

    def test_allowed_boundary_dirs_including_the_real_one_still_resolves(self):
        findings = run(load_valid(), allowed_boundary_dirs=[SPECS_SEARCH_ROOT / "_boundaries"])
        self.assertEqual(errors_of(findings), [])

    def test_no_search_root_is_a_hard_error_not_a_silent_pass(self):
        """External review, high severity: this used to degrade to a
        non-blocking info finding, which let a manifest with real
        trusted_assumptions print OK without the check ever running --
        one of this validator's three claimed mechanical guarantees."""
        findings = run(load_valid(), specs_search_root=None)
        errors = errors_of(findings)
        self.assertTrue(any("no specs_search_root given" in f.reason for f in errors))

    def test_empty_trusted_assumptions_needs_no_search_root(self):
        data = load_valid()
        data["definition_of_done"]["trusted_assumptions"] = []
        findings = run(data, specs_search_root=None)
        self.assertEqual(errors_of(findings), [])


class StandaloneCliBoundaryDirChoiceTest(unittest.TestCase):
    """External review, medium severity: pipeline.py correctly supplies
    descriptor-derived boundary directories, but validate_work_package.py's
    own standalone main() always passed allowed_boundary_dirs=None,
    reintroducing the 'trusts any schema-valid boundary anywhere' gap
    outside pipeline.py. No prior test exercised main() at all -- these do.

    A first fix added a third, named opt-out flag, --allow-any-crate-boundary.
    A second review round found that flag was the identical trust gap
    behind an explicit switch and it was removed rather than kept as a
    documented escape hatch -- there is no way to skip canonical-crate
    restriction from this CLI, only --descriptor or --allowed-boundary-dir
    to supply it."""

    def _args(self, *extra):
        return [
            str(MANIFEST_PATH),
            "--workspace-root", str(FIXTURE_ROOT),
            "--specs-search-root", str(SPECS_SEARCH_ROOT),
            *extra,
        ]

    def test_neither_flag_is_refused(self):
        import validate_work_package
        rc = validate_work_package.main(self._args())
        self.assertEqual(rc, 2)

    def test_allow_any_crate_boundary_flag_no_longer_exists(self):
        """Confirms the removal, not just that it's undocumented --
        argparse itself must reject it."""
        import validate_work_package
        with self.assertRaises(SystemExit):
            validate_work_package.main(self._args("--allow-any-crate-boundary"))

    def test_junk_boundary_is_rejected_no_matter_what_flags_are_passed(self):
        """The actual end-to-end reproduction: a fully valid, correctly-
        named boundary under a non-crate path must be refused regardless
        of which of the two remaining (legitimate) flag choices is used --
        there is no longer a third choice that would let it through."""
        import validate_work_package
        with tempfile.TemporaryDirectory() as tmp:
            junk_dir = Path(tmp) / "junk" / "not-a-crate" / "_boundaries"
            junk_dir.mkdir(parents=True)
            (junk_dir / "scheduler_dispatch__to__task_queue_pop_ready.json").write_text(json.dumps({
                "schema_version": "1.0",
                "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
                "caller": {"concept": "Scheduler", "method": "dispatch"},
                "callee": {"concept": "TaskQueue", "method": "pop_ready"},
                "callee_guarantees": ["TaskQueue.C003"],
                "assumptions": [{
                    "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
                    "tracking_issue": "chainlink:713",
                    "assumption_hash": "sha256:" + "1" * 64,
                }],
                "review": {"reviewer": "x", "reviewed_at": "2026-08-31"},
            }))
            args = [
                str(MANIFEST_PATH),
                "--workspace-root", str(FIXTURE_ROOT),
                "--specs-search-root", str(tmp),
                "--allowed-boundary-dir", str(SPECS_SEARCH_ROOT / "_boundaries"),
            ]
            rc = validate_work_package.main(args)
            self.assertEqual(rc, 1)

    def test_explicit_allowed_boundary_dir_passes(self):
        import validate_work_package
        rc = validate_work_package.main(
            self._args("--allowed-boundary-dir", str(SPECS_SEARCH_ROOT / "_boundaries"))
        )
        self.assertEqual(rc, 0)

    def test_explicit_allowed_boundary_dir_excluding_the_real_one_fails(self):
        import validate_work_package
        with tempfile.TemporaryDirectory() as elsewhere:
            rc = validate_work_package.main(self._args("--allowed-boundary-dir", elsewhere))
            self.assertEqual(rc, 1)

    def test_descriptor_flag_derives_dirs_and_passes(self):
        import validate_work_package
        rc = validate_work_package.main(
            self._args("--descriptor", str(FIXTURE_ROOT / "project-descriptor.json"))
        )
        self.assertEqual(rc, 0)

    def test_descriptor_and_allowed_boundary_dir_together_is_refused(self):
        import validate_work_package
        rc = validate_work_package.main(
            self._args(
                "--descriptor", str(FIXTURE_ROOT / "project-descriptor.json"),
                "--allowed-boundary-dir", str(SPECS_SEARCH_ROOT / "_boundaries"),
            )
        )
        self.assertEqual(rc, 2)


class WitnessRendererIntegrityTest(unittest.TestCase):
    """chainlink #35: the witness renderer implementation
    (scripts/witness_renderer.py, scripts/xml_escape.py) must have its
    own gate_integrity entry, be covered by protected_write_set, and
    never be covered by allowed_write_set -- all G13 pre-flight,
    unconditional for every manifest."""

    def test_valid_manifest_has_both_renderer_paths_pinned_and_protected(self):
        findings = run(load_valid())
        self.assertFalse(any("witness renderer" in f.reason for f in errors_of(findings)), [str(f) for f in findings])

    def test_missing_renderer_gate_integrity_entry_is_rejected(self):
        data = load_valid()
        data["gate_integrity"] = [
            e for e in data["gate_integrity"] if e["runner"] != "scripts/witness_renderer.py"
        ]
        findings = run(data)
        errors = errors_of(findings)
        self.assertTrue(
            any(
                f.gate == "G13" and "missing a required entry" in f.reason and "witness_renderer.py" in f.reason
                for f in errors
            ),
            [str(f) for f in errors],
        )

    def test_missing_xml_escape_helper_entry_is_rejected(self):
        """The transitive helper must be pinned too -- witness_renderer.py
        imports it directly for its own text escaping, so a
        modification there can change rendered evidence without
        touching witness_renderer.py's own hash."""
        data = load_valid()
        data["gate_integrity"] = [e for e in data["gate_integrity"] if e["runner"] != "scripts/xml_escape.py"]
        findings = run(data)
        errors = errors_of(findings)
        self.assertTrue(
            any(
                f.gate == "G13" and "missing a required entry" in f.reason and "xml_escape.py" in f.reason
                for f in errors
            ),
            [str(f) for f in errors],
        )

    def test_renderer_hash_drift_is_rejected(self):
        data = load_valid()
        for entry in data["gate_integrity"]:
            if entry["runner"] == "scripts/witness_renderer.py":
                entry["hash"] = "sha256:" + "0" * 64
        findings = run(data)
        errors = errors_of(findings)
        self.assertTrue(
            any(f.gate == "G13" and "hash mismatch" in f.reason and "witness_renderer.py" in f.reason for f in errors),
            [str(f) for f in errors],
        )

    def test_renderer_path_escaping_the_workspace_is_rejected(self):
        data = load_valid()
        for entry in data["gate_integrity"]:
            if entry["runner"] == "scripts/witness_renderer.py":
                entry["runner"] = "../outside_workspace_secret.py"
        findings = run(data)
        errors = errors_of(findings)
        self.assertTrue(any("escapes the workspace" in f.reason for f in errors), [str(f) for f in errors])

    def test_renderer_not_covered_by_protected_write_set_is_rejected(self):
        data = load_valid()
        data["write_policy"]["protected_write_set"] = [
            p for p in data["write_policy"]["protected_write_set"] if p != "scripts/**"
        ]
        findings = run(data)
        errors = errors_of(findings)
        self.assertTrue(
            any(
                f.gate == "G13" and "not covered by protected_write_set" in f.reason
                and "witness_renderer.py" in f.reason
                for f in errors
            ),
            [str(f) for f in errors],
        )

    def test_renderer_covered_by_allowed_write_set_is_rejected(self):
        data = load_valid()
        data["write_policy"]["allowed_write_set"] = list(data["write_policy"]["allowed_write_set"]) + [
            "scripts/witness_renderer.py"
        ]
        findings = run(data)
        errors = errors_of(findings)
        self.assertTrue(
            any(
                f.gate == "G13" and "covered by allowed_write_set" in f.reason
                and "witness_renderer.py" in f.reason
                for f in errors
            ),
            [str(f) for f in errors],
        )

    def test_a_broad_protected_pattern_genuinely_proven_to_cover_the_renderer_passes(self):
        """'scripts/**' must satisfy coverage because the real
        glob-overlap engine proves it does -- not because the pattern
        merely looks broad. The canonical fixture already relies on
        this; this test names the guarantee directly."""
        from validate_work_package import _patterns_can_overlap
        self.assertTrue(_patterns_can_overlap("scripts/**", "scripts/witness_renderer.py"))
        self.assertTrue(_patterns_can_overlap("scripts/**", "scripts/xml_escape.py"))


if __name__ == "__main__":
    unittest.main()
