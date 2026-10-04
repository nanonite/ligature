import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from validate_exemption import (  # noqa: E402
    count_discovered,
    find_exemption_files,
    load_validator,
    main,
    validate,
    validate_crate,
    validate_data,
)

EXEMPTION_PATH = Path("crates/scheduler/specs/_exemptions/I-SCHED-TQ-001.json")


def load_valid() -> dict:
    return {
        "schema_version": "1.0",
        "interaction_id": "I-SCHED-TQ-001",
        "rationale": "Prototype scaffolding boundary, tracked for removal before release (chainlink:#41)",
        "review": {"reviewer": "alice", "reviewed_at": "2026-08-30"},
    }


_AUTO = "auto"


def run(data: dict, path: Path = EXEMPTION_PATH, interactions_by_id=_AUTO):
    """`interactions_by_id` defaults to a minimal real, boundary-required
    interaction matching `data`'s own interaction_id -- R2's
    reference-integrity check (chainlink #46) is orthogonal to everything
    else this file tests (schema, naming), so satisfying it automatically
    keeps every test that isn't specifically about the cross-reference
    unaffected. Pass an explicit value (including None, or a dict missing
    this id) to override."""
    if interactions_by_id == _AUTO:
        interactions_by_id = {data.get("interaction_id"): {"eligibility": "boundary-required"}}
    return validate_data(path, data, load_validator(), interactions_by_id)


class ValidExemptionTest(unittest.TestCase):
    def test_valid_exemption_has_no_findings(self):
        findings = run(load_valid())
        self.assertEqual(findings, [], [str(f) for f in findings])


class G1aFailureTest(unittest.TestCase):
    def test_missing_required_field_is_rejected(self):
        data = load_valid()
        del data["rationale"]
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_empty_rationale_is_rejected(self):
        data = load_valid()
        data["rationale"] = ""
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_missing_review_is_rejected(self):
        data = load_valid()
        del data["review"]
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_promotion_id_field_is_rejected(self):
        """additionalProperties: false -- an exemption never carries a
        promotion_id of its own (references are one-way, plan.md §7.1)."""
        data = load_valid()
        data["promotion_id"] = "PROM-SCHED-001"
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))


class NamingTest(unittest.TestCase):
    def test_filename_stem_mismatch_is_rejected(self):
        data = load_valid()
        findings = run(data, path=Path("crates/scheduler/specs/_exemptions/wrong-name.json"))
        self.assertTrue(
            any("does not match interaction_id" in f.reason for f in findings), [str(f) for f in findings]
        )

    def test_nested_under_exemptions_dir_is_rejected(self):
        data = load_valid()
        findings = run(data, path=Path("crates/scheduler/specs/_exemptions/nested/I-SCHED-TQ-001.json"))
        self.assertTrue(any("not flat" in f.reason for f in findings), [str(f) for f in findings])

    def test_non_json_suffix_is_rejected(self):
        """External review, high severity: check_naming never checked the
        file suffix, so a schema-valid exemption at I-SCHED-TQ-001.yaml
        produced zero findings."""
        data = load_valid()
        findings = run(data, path=Path("crates/scheduler/specs/_exemptions/I-SCHED-TQ-001.yaml"))
        self.assertTrue(any("must be a .json file" in f.reason for f in findings), [str(f) for f in findings])


class InteractionCrossReferenceTest(unittest.TestCase):
    """R2's reference-integrity half (plan.md gate table §12; chainlink
    #46): an exemption only makes sense if it names a real, eligible
    interaction. Mirrors test_validate_protocol_debt.py's own
    cross-reference tests exactly."""

    def test_no_context_supplied_is_an_info_note_not_a_failure(self):
        findings = run(load_valid(), interactions_by_id=None)
        self.assertFalse(any(f.gate == "G2" and f.severity == "error" for f in findings))
        self.assertTrue(any(f.gate == "G2" and f.severity == "info" for f in findings))

    def test_dangling_interaction_reference_is_rejected(self):
        """The fail-closed case: a real (non-None) lookup that simply
        doesn't contain this interaction_id."""
        findings = run(load_valid(), interactions_by_id={})
        self.assertTrue(
            any(f.gate == "G2" and "dangling reference" in f.reason for f in findings), [str(f) for f in findings]
        )

    def test_ineligible_interaction_reference_is_rejected(self):
        """A real interaction exists, but it isn't boundary-required --
        an exemption for an inform/ignore edge is incoherent, since
        there's nothing to be exempt from."""
        findings = run(load_valid(), interactions_by_id={"I-SCHED-TQ-001": {"eligibility": "inform"}})
        self.assertTrue(
            any(f.gate == "G2" and "only applies to a boundary-required interaction" in f.reason for f in findings),
            [str(f) for f in findings],
        )

    def test_boundary_required_interaction_reference_is_accepted(self):
        findings = run(load_valid(), interactions_by_id={"I-SCHED-TQ-001": {"eligibility": "boundary-required"}})
        self.assertFalse(any(f.gate == "G2" for f in findings), [str(f) for f in findings])


class FindExemptionFilesTest(unittest.TestCase):
    def test_missing_root_raises(self):
        with self.assertRaises(FileNotFoundError):
            find_exemption_files(Path("/nonexistent/does/not/exist"))

    def test_finds_flat_json_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_exemptions"
            d.mkdir(parents=True)
            (d / "I-X-001.json").write_text("{}")
            found = find_exemption_files(Path(tmp))
            self.assertEqual(len(found), 1)


class ValidateReportsNonJsonFilesTest(unittest.TestCase):
    """External review, high severity: validate() used to silently
    `continue` past any non-.json file under a real _exemptions
    directory, producing an overall OK with zero findings."""

    def test_recursive_scan_reports_unparseable_non_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_exemptions"
            d.mkdir(parents=True)
            (d / "I-X-001.yaml").write_text("not: valid: json: [[[")
            findings = validate(Path(tmp))
        self.assertTrue(findings, "a malformed non-.json artifact must be reported, not silently skipped")

    def test_recursive_scan_reports_well_formed_json_with_wrong_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_exemptions"
            d.mkdir(parents=True)
            (d / "I-SCHED-TQ-001.yaml").write_text(json.dumps(load_valid()))
            findings = validate(Path(tmp))
        self.assertTrue(any("must be a .json file" in f.reason for f in findings), [str(f) for f in findings])


class ValidateCrateTest(unittest.TestCase):
    """validate_crate(): the descriptor-driven scan pipeline.py's
    cmd_validate_exemption uses -- discovers candidates crate-wide, then
    rejects any that don't sit directly under the crate's exact canonical
    directory."""

    # A minimal real, boundary-required interaction matching load_valid()'s
    # own interaction_id -- satisfies R2's cross-reference (chainlink #46).
    _REAL_INTERACTIONS = {"I-SCHED-TQ-001": {"eligibility": "boundary-required"}}

    def test_missing_crate_root_raises(self):
        with self.assertRaises(FileNotFoundError):
            validate_crate(Path("/nonexistent"), Path("/nonexistent/specs/_exemptions"), {})

    def test_finds_and_validates_files_in_the_canonical_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            crate_root = Path(tmp)
            canonical = crate_root / "specs" / "_exemptions"
            canonical.mkdir(parents=True)
            (canonical / "I-SCHED-TQ-001.json").write_text(json.dumps(load_valid()))
            findings = validate_crate(crate_root, canonical, self._REAL_INTERACTIONS)
        self.assertEqual(findings, [], [str(f) for f in findings])

    def test_mislocated_but_otherwise_valid_artifact_is_rejected_by_location_alone(self):
        """External review, medium severity, SECOND pass: a first fix
        (validate_dir(), now removed) only scanned the canonical
        directory, so a mislocated artifact was never looked at and the
        scan reported OK -- reproducing the same zero-findings outcome by
        omission instead of false acceptance. This artifact is fully
        schema-valid with a correct filename -- only its directory is
        wrong, proving location alone causes rejection."""
        with tempfile.TemporaryDirectory() as tmp:
            crate_root = Path(tmp)
            stray = crate_root / "not_specs" / "_exemptions"
            stray.mkdir(parents=True)
            (stray / "I-SCHED-TQ-001.json").write_text(json.dumps(load_valid()))
            canonical = crate_root / "specs" / "_exemptions"  # never created
            findings = validate_crate(crate_root, canonical, {})
        self.assertTrue(
            any("not directly under the canonical directory" in f.reason for f in findings),
            [str(f) for f in findings],
        )

    def test_correctly_located_artifact_is_not_flagged_by_a_stray_sibling(self):
        with tempfile.TemporaryDirectory() as tmp:
            crate_root = Path(tmp)
            canonical = crate_root / "specs" / "_exemptions"
            canonical.mkdir(parents=True)
            (canonical / "I-SCHED-TQ-001.json").write_text(json.dumps(load_valid()))
            stray = crate_root / "not_specs" / "_exemptions"
            stray.mkdir(parents=True)
            (stray / "wrong-name.json").write_text(json.dumps(load_valid()))
            findings = validate_crate(crate_root, canonical, self._REAL_INTERACTIONS)
        self.assertTrue(any("not directly under the canonical directory" in f.reason for f in findings))
        self.assertFalse(any(f.path.name == "I-SCHED-TQ-001.json" for f in findings))


class ValidExemptionInteractionIdsFromCrateTest(unittest.TestCase):
    """valid_exemption_interaction_ids_from_crate(): the coverage set
    scripts/validate_interaction.py's check_r2_coverage trusts as
    'covered by a reviewed exemption'. A dangling or ineligible
    exemption reference (chainlink #46) never contributes coverage --
    that's how it's rejected for R2 purposes, not by a separate
    mechanism."""

    def test_valid_exemption_for_a_real_boundary_required_interaction_is_covered(self):
        from validate_exemption import valid_exemption_interaction_ids_from_crate

        with tempfile.TemporaryDirectory() as tmp:
            crate_root = Path(tmp)
            canonical = crate_root / "specs" / "_exemptions"
            canonical.mkdir(parents=True)
            (canonical / "I-SCHED-TQ-001.json").write_text(json.dumps(load_valid()))
            interactions_by_id = {"I-SCHED-TQ-001": {"eligibility": "boundary-required"}}
            covered = valid_exemption_interaction_ids_from_crate(crate_root, canonical, interactions_by_id)
        self.assertEqual(covered, {"I-SCHED-TQ-001"})

    def test_exemption_for_a_dangling_interaction_is_not_covered(self):
        from validate_exemption import valid_exemption_interaction_ids_from_crate

        with tempfile.TemporaryDirectory() as tmp:
            crate_root = Path(tmp)
            canonical = crate_root / "specs" / "_exemptions"
            canonical.mkdir(parents=True)
            (canonical / "I-SCHED-TQ-001.json").write_text(json.dumps(load_valid()))
            covered = valid_exemption_interaction_ids_from_crate(crate_root, canonical, {})
        self.assertEqual(covered, set())

    def test_exemption_for_an_ineligible_interaction_is_not_covered(self):
        from validate_exemption import valid_exemption_interaction_ids_from_crate

        with tempfile.TemporaryDirectory() as tmp:
            crate_root = Path(tmp)
            canonical = crate_root / "specs" / "_exemptions"
            canonical.mkdir(parents=True)
            (canonical / "I-SCHED-TQ-001.json").write_text(json.dumps(load_valid()))
            interactions_by_id = {"I-SCHED-TQ-001": {"eligibility": "inform"}}
            covered = valid_exemption_interaction_ids_from_crate(crate_root, canonical, interactions_by_id)
        self.assertEqual(covered, set())


class StandaloneCliMainTest(unittest.TestCase):
    def _write_real_interaction(self, specs: Path) -> None:
        """A real, fully valid, reviewed, boundary-required interaction
        matching load_valid()'s own interaction_id -- the standalone
        CLI's own validate() derives R2's cross-reference from this
        sibling directory (chainlink #46), the same way it already
        derives G15 coverage from a sibling _protocol_debt/."""
        (specs / "_interactions").mkdir(parents=True, exist_ok=True)
        (specs / "_interactions" / "I-SCHED-TQ-001.json").write_text(json.dumps({
            "schema_version": "1.0",
            "interaction_id": "I-SCHED-TQ-001",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "edge_class": ["stateful"],
            "eligibility": "boundary-required",
            "rationale": "dispatch relies on pop_ready's return discipline",
            "reliances": [{
                "obligation_id": "TaskQueue.C003",
                "required_assurance": {
                    "required_claims": ["postcondition-holds"],
                    "accepted_evidence_kinds": ["creusot-deductive-check"],
                    "minimum_scope": {"input_domain": "queue_len_le_8", "feature_set": "default"},
                    "trust_policy": {"assumptions_allowed": []},
                },
            }],
            "protocol_class": "pairwise",
            "realization": {
                "requirement": "required",
                "config_scope": {"target": "x86_64-unknown-linux-gnu", "features": ["default"], "cfg": []},
            },
            "review": {"reviewer": "alice", "reviewed_at": "2026-08-30"},
        }))

    def test_main_passes_for_valid_fixture_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            specs = Path(tmp) / "specs"
            d = specs / "_exemptions"
            d.mkdir(parents=True)
            (d / "I-SCHED-TQ-001.json").write_text(json.dumps(load_valid()))
            self._write_real_interaction(specs)
            rc = main([tmp])
        self.assertEqual(rc, 0)

    def test_main_fails_for_naming_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_exemptions"
            d.mkdir(parents=True)
            (d / "wrong-name.json").write_text(json.dumps(load_valid()))
            rc = main([tmp])
        self.assertEqual(rc, 1)


class StagedDraftInertTest(unittest.TestCase):
    """Chainlink #110: `draft 3 exemption-drafting` stages
    `<target>.json.draft` WITHOUT a `review` block (only
    `approve --reviewer`, or the bootstrap `approve-exemption-pair`,
    attaches one), and `draft` itself reports "OK: generated draft passes
    G1a/G1b immediate checks" -- yet every exemption scan collected that
    file and failed it on `'review' is a required property` at G1a, so
    `validate-exemption` exited 1 on the output the tool prescribes and
    `check --json` gained one medium finding plus a
    `human_decision_required` condition per staged exemption.
    validate_interaction/validate_boundary_contracts have skipped staged
    drafts since #90; this pins the same convention for exemptions.

    A staged draft is inert (neither reported, counted, nor admitted to
    R2's coverage set) everywhere find_exemption_files() is the discovery
    point -- the standalone validate(), validate_crate() (what
    `validate-exemption` runs), count_discovered() and
    valid_exemption_interaction_ids_from_crate() -- while an unreviewed
    exemption at its REAL `<interaction_id>.json` path is still
    reported."""

    _REAL_INTERACTIONS = {"I-SCHED-TQ-001": {"eligibility": "boundary-required"}}

    def _stage_draft(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        draft = load_valid()
        draft.pop("review")  # exactly what stage-3-exemption-drafting produces
        (directory / "I-SCHED-TQ-001.json.draft").write_text(json.dumps(draft))

    def _crate_with_draft(self, tmp: str, under_canonical: bool = True) -> tuple[Path, Path]:
        crate_root = Path(tmp)
        canonical = crate_root / "specs" / "_exemptions"
        self._stage_draft(canonical if under_canonical else crate_root / "not_specs" / "_exemptions")
        return crate_root, canonical

    def test_find_exemption_files_excludes_staged_drafts(self):
        with tempfile.TemporaryDirectory() as tmp:
            crate_root, canonical = self._crate_with_draft(tmp)
            (canonical / "I-SCHED-TQ-002.json").write_text(json.dumps(load_valid()))
            found = find_exemption_files(crate_root)
        self.assertEqual([p.name for p in found], ["I-SCHED-TQ-002.json"])

    def test_the_recursive_scan_reports_nothing_for_a_staged_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            crate_root, _ = self._crate_with_draft(tmp)
            findings = validate(crate_root, self._REAL_INTERACTIONS)
        self.assertEqual([str(f) for f in findings], [])

    def test_staged_draft_is_not_counted_as_discovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            crate_root, _ = self._crate_with_draft(tmp)
            self.assertEqual(count_discovered(crate_root), 0)

    def test_validate_crate_reports_nothing_for_a_staged_draft(self):
        """cmd_validate_exemption's own path -- the exact command the
        pilot report's EXIT 1 came from."""
        with tempfile.TemporaryDirectory() as tmp:
            crate_root, canonical = self._crate_with_draft(tmp)
            findings = validate_crate(crate_root, canonical, self._REAL_INTERACTIONS)
        self.assertEqual([str(f) for f in findings], [])

    def test_a_staged_draft_never_contributes_r2_coverage(self):
        """Coverage came from the same discovery function, so a pending
        exemption is excluded by not being discovered rather than by
        failing validation as a side effect."""
        from validate_exemption import valid_exemption_interaction_ids_from_crate

        with tempfile.TemporaryDirectory() as tmp:
            crate_root, canonical = self._crate_with_draft(tmp)
            covered = valid_exemption_interaction_ids_from_crate(
                crate_root, canonical, self._REAL_INTERACTIONS
            )
        self.assertEqual(covered, set())

    def test_the_cli_exits_zero_on_a_staged_draft(self):
        """The reported symptom: `validate-exemption` exit 1 with
        [G1a/error] "'review' is a required property" against the draft."""
        with tempfile.TemporaryDirectory() as tmp:
            crate_root, _ = self._crate_with_draft(tmp)
            rc = main([str(crate_root)])
        self.assertEqual(rc, 0)

    def test_staged_draft_in_a_mislocated_directory_is_still_inert(self):
        """Consistency, not just the happy path: a draft under a wrong
        directory is still a pending draft, and stage_draft only accepts
        canonical targets, so no promotion verb could ever consume it."""
        with tempfile.TemporaryDirectory() as tmp:
            crate_root, _ = self._crate_with_draft(tmp, under_canonical=False)
            findings = validate(crate_root, self._REAL_INTERACTIONS)
        self.assertEqual([str(f) for f in findings], [])

    def test_an_unreviewed_real_json_is_still_reported(self):
        """The skip must be exactly `.draft`: an unreviewed artifact at
        its real `<interaction_id>.json` path is a draft-LIFECYCLE record
        (check normalizes it to a pending human decision), not a staging
        file."""
        with tempfile.TemporaryDirectory() as tmp:
            crate_root, canonical = self._crate_with_draft(tmp)
            unreviewed = load_valid()
            unreviewed.pop("review")
            (canonical / "I-SCHED-TQ-001.json").write_text(json.dumps(unreviewed))
            findings = validate(crate_root, self._REAL_INTERACTIONS)
        self.assertTrue(
            any("'review' is a required property" in f.reason for f in findings),
            [str(f) for f in findings],
        )

    def test_wrong_extension_file_is_still_reported(self):
        """Guards the earlier external-review fix (a wrong-extension
        artifact under a real _exemptions directory must surface) from
        being collateral damage of the draft skip."""
        with tempfile.TemporaryDirectory() as tmp:
            crate_root, canonical = self._crate_with_draft(tmp)
            (canonical / "I-SCHED-TQ-001.yaml").write_text(json.dumps(load_valid()))
            findings = validate(crate_root, self._REAL_INTERACTIONS)
        self.assertTrue(
            any("must be a .json file" in f.reason for f in findings),
            [str(f) for f in findings],
        )


if __name__ == "__main__":
    unittest.main()
