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


def run(data: dict, workspace_root: Path = FIXTURE_ROOT, specs_search_root: Path | None = SPECS_SEARCH_ROOT):
    validator = load_validator()
    return validate_data(MANIFEST_PATH, data, validator, workspace_root, specs_search_root)


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
        with tempfile.TemporaryDirectory() as tmp:
            search_root = Path(tmp)
            for sub in ("crate_a", "crate_b"):
                bdir = search_root / sub / "specs" / "_boundaries"
                bdir.mkdir(parents=True)
                (bdir / "x.json").write_text(json.dumps({
                    "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
                    "assumptions": [{
                        "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
                        "tracking_issue": "chainlink:713",
                        "assumption_hash": "sha256:" + "1" * 64,
                    }],
                }))
            findings = run(load_valid(), specs_search_root=search_root)
            errors = errors_of(findings)
            self.assertTrue(any("ambiguously" in f.reason for f in errors))

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

if __name__ == "__main__":
    unittest.main()
