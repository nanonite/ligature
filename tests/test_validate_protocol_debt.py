import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from validate_protocol_debt import (  # noqa: E402
    count_discovered,
    find_protocol_debt_files,
    load_validator,
    main,
    validate,
    validate_crate,
    validate_data,
)

PROTOCOL_DEBT_PATH = Path("crates/scheduler/specs/_protocol_debt/I-SCHED-TQ-001.json")


def load_valid() -> dict:
    return {
        "schema_version": "1.0",
        "interaction_id": "I-SCHED-TQ-001",
        "rationale": "Multi-step handshake protocol, not yet modeled; no promoted work depends on it yet",
        "no_promoted_obligation_depends_on_protocol": True,
        "no_work_package_touches_its_path": True,
        "no_release_claim_includes_it": True,
        "tracking_issue": "chainlink:#99",
        "review": {"reviewer": "alice", "reviewed_at": "2026-08-30"},
    }


def default_interactions_by_id() -> dict:
    """The interaction load_valid()'s debt record covers, matching by
    id -- used as run()'s default so G1a/G1b-focused tests aren't
    incidentally polluted by the cross-reference check; tests of the
    cross-reference itself override this explicitly."""
    return {"I-SCHED-TQ-001": {"protocol_class": "non-pairwise"}}


_UNSET = object()


def run(data: dict, path: Path = PROTOCOL_DEBT_PATH, interactions_by_id=_UNSET):
    if interactions_by_id is _UNSET:
        interactions_by_id = default_interactions_by_id()
    return validate_data(path, data, load_validator(), interactions_by_id)


class ValidProtocolDebtTest(unittest.TestCase):
    def test_valid_protocol_debt_has_no_findings(self):
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

    def test_missing_tracking_issue_is_rejected(self):
        data = load_valid()
        del data["tracking_issue"]
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_empty_tracking_issue_is_rejected(self):
        data = load_valid()
        data["tracking_issue"] = ""
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_promotion_id_field_is_rejected(self):
        """additionalProperties: false -- a protocol-debt record never
        carries a promotion_id of its own (references are one-way,
        plan.md §7.1)."""
        data = load_valid()
        data["promotion_id"] = "PROM-SCHED-001"
        findings = run(data)
        self.assertTrue(any(f.gate == "G1a" for f in findings))

    def test_each_attestation_field_must_be_true_not_merely_present(self):
        """plan.md §5.3: the record 'suffices only when all five hold' --
        a record cannot be filed admitting one of the three externally-
        facing conditions is false. const: true rejects False outright."""
        for field in [
            "no_promoted_obligation_depends_on_protocol",
            "no_work_package_touches_its_path",
            "no_release_claim_includes_it",
        ]:
            data = load_valid()
            data[field] = False
            findings = run(data)
            self.assertTrue(any(f.gate == "G1a" for f in findings), f"{field}: expected rejection")

    def test_each_attestation_field_is_required(self):
        for field in [
            "no_promoted_obligation_depends_on_protocol",
            "no_work_package_touches_its_path",
            "no_release_claim_includes_it",
        ]:
            data = load_valid()
            del data[field]
            findings = run(data)
            self.assertTrue(any(f.gate == "G1a" for f in findings), f"{field}: expected rejection")


class NamingTest(unittest.TestCase):
    def test_filename_stem_mismatch_is_rejected(self):
        data = load_valid()
        findings = run(data, path=Path("crates/scheduler/specs/_protocol_debt/wrong-name.json"))
        self.assertTrue(
            any("does not match interaction_id" in f.reason for f in findings), [str(f) for f in findings]
        )

    def test_nested_under_protocol_debt_dir_is_rejected(self):
        data = load_valid()
        findings = run(data, path=Path("crates/scheduler/specs/_protocol_debt/nested/I-SCHED-TQ-001.json"))
        self.assertTrue(any("not flat" in f.reason for f in findings), [str(f) for f in findings])

    def test_non_json_suffix_is_rejected(self):
        data = load_valid()
        findings = run(data, path=Path("crates/scheduler/specs/_protocol_debt/I-SCHED-TQ-001.yaml"))
        self.assertTrue(any("must be a .json file" in f.reason for f in findings), [str(f) for f in findings])


class InteractionCrossReferenceTest(unittest.TestCase):
    """External review, high severity: nothing previously checked that
    interaction_id names a real interaction, or that the interaction it
    names is actually non-pairwise. Both reproduced as passing with zero
    findings before this check was added."""

    def test_no_context_supplied_is_an_info_note_not_a_failure(self):
        findings = run(load_valid(), interactions_by_id=None)
        self.assertFalse(any(f.gate == "G2" and f.severity == "error" for f in findings))
        self.assertTrue(any(f.gate == "G2" and f.severity == "info" for f in findings))

    def test_dangling_interaction_id_is_rejected(self):
        findings = run(load_valid(), interactions_by_id={})
        self.assertTrue(
            any("does not resolve to any real interaction" in f.reason for f in findings),
            [str(f) for f in findings],
        )

    def test_debt_record_for_a_pairwise_interaction_is_rejected(self):
        """A debt record only applies to a non-pairwise interaction --
        one filed for what's actually pairwise is meaningless."""
        findings = run(load_valid(), interactions_by_id={"I-SCHED-TQ-001": {"protocol_class": "pairwise"}})
        self.assertTrue(
            any("only applies to a non-pairwise interaction" in f.reason for f in findings),
            [str(f) for f in findings],
        )

    def test_matching_non_pairwise_interaction_is_accepted(self):
        findings = run(
            load_valid(), interactions_by_id={"I-SCHED-TQ-001": {"protocol_class": "non-pairwise"}}
        )
        self.assertEqual(findings, [], [str(f) for f in findings])


class FindProtocolDebtFilesTest(unittest.TestCase):
    def test_missing_root_raises(self):
        with self.assertRaises(FileNotFoundError):
            find_protocol_debt_files(Path("/nonexistent/does/not/exist"))

    def test_finds_flat_json_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_protocol_debt"
            d.mkdir(parents=True)
            (d / "I-X-001.json").write_text("{}")
            found = find_protocol_debt_files(Path(tmp))
            self.assertEqual(len(found), 1)


class ValidateReportsNonJsonFilesTest(unittest.TestCase):
    def test_recursive_scan_reports_unparseable_non_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_protocol_debt"
            d.mkdir(parents=True)
            (d / "I-X-001.yaml").write_text("not: valid: json: [[[")
            findings = validate(Path(tmp))
        self.assertTrue(findings, "a malformed non-.json artifact must be reported, not silently skipped")

    def test_recursive_scan_reports_well_formed_json_with_wrong_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_protocol_debt"
            d.mkdir(parents=True)
            (d / "I-SCHED-TQ-001.yaml").write_text(json.dumps(load_valid()))
            findings = validate(Path(tmp))
        self.assertTrue(any("must be a .json file" in f.reason for f in findings), [str(f) for f in findings])


class ValidateCrateTest(unittest.TestCase):
    """validate_crate(): the descriptor-driven scan pipeline.py's
    cmd_validate_protocol_debt uses -- discovers candidates crate-wide,
    then rejects any that don't sit directly under the crate's exact
    canonical directory."""

    def test_missing_crate_root_raises(self):
        with self.assertRaises(FileNotFoundError):
            validate_crate(Path("/nonexistent"), Path("/nonexistent/specs/_protocol_debt"), {})

    def test_finds_and_validates_files_in_the_canonical_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            crate_root = Path(tmp)
            canonical = crate_root / "specs" / "_protocol_debt"
            canonical.mkdir(parents=True)
            (canonical / "I-SCHED-TQ-001.json").write_text(json.dumps(load_valid()))
            findings = validate_crate(crate_root, canonical, default_interactions_by_id())
        self.assertEqual(findings, [], [str(f) for f in findings])

    def test_mislocated_but_otherwise_valid_artifact_is_rejected_by_location_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            crate_root = Path(tmp)
            stray = crate_root / "not_specs" / "_protocol_debt"
            stray.mkdir(parents=True)
            (stray / "I-SCHED-TQ-001.json").write_text(json.dumps(load_valid()))
            canonical = crate_root / "specs" / "_protocol_debt"  # never created
            findings = validate_crate(crate_root, canonical, default_interactions_by_id())
        self.assertTrue(
            any("not directly under the canonical directory" in f.reason for f in findings),
            [str(f) for f in findings],
        )

    def test_correctly_located_artifact_is_not_flagged_by_a_stray_sibling(self):
        with tempfile.TemporaryDirectory() as tmp:
            crate_root = Path(tmp)
            canonical = crate_root / "specs" / "_protocol_debt"
            canonical.mkdir(parents=True)
            (canonical / "I-SCHED-TQ-001.json").write_text(json.dumps(load_valid()))
            stray = crate_root / "not_specs" / "_protocol_debt"
            stray.mkdir(parents=True)
            (stray / "wrong-name.json").write_text(json.dumps(load_valid()))
            findings = validate_crate(crate_root, canonical, default_interactions_by_id())
        self.assertTrue(any("not directly under the canonical directory" in f.reason for f in findings))
        self.assertFalse(any(f.path.name == "I-SCHED-TQ-001.json" for f in findings))


class StandaloneCliMainTest(unittest.TestCase):
    def test_main_passes_for_valid_fixture_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_protocol_debt"
            d.mkdir(parents=True)
            (d / "I-SCHED-TQ-001.json").write_text(json.dumps(load_valid()))
            rc = main([tmp])
        self.assertEqual(rc, 0)

    def test_main_fails_for_naming_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "specs" / "_protocol_debt"
            d.mkdir(parents=True)
            (d / "wrong-name.json").write_text(json.dumps(load_valid()))
            rc = main([tmp])
        self.assertEqual(rc, 1)


class StagedDraftInertTest(unittest.TestCase):
    """Chainlink #110: `draft 3 protocol-debt-drafting` stages
    `<target>.json.draft` WITHOUT a `review` block (only `approve --reviewer`,
    in the pairing transaction with the interaction, attaches one), and
    `draft` itself reports "OK: generated draft passes G1a/G1b immediate
    checks" -- yet every protocol-debt scan collected that file and failed
    it on `'review' is a required property` at G1a, so
    `validate-protocol-debt` exited 1 mid-review and `check --json` gained
    one medium finding plus a `human_decision_required` condition per
    staged record. validate_interaction/validate_boundary_contracts have
    skipped staged drafts since #90; this pins the same convention for
    protocol-debt records.

    A staged draft is inert (neither reported, counted, nor admitted to
    G15's coverage set) everywhere find_protocol_debt_files() is the
    discovery point -- the standalone validate(), validate_crate() (what
    `validate-protocol-debt` runs), count_discovered() and
    valid_interaction_ids_from_crate() -- while an unreviewed record at
    its REAL `<interaction_id>.json` path is still reported."""

    def _crate_with_draft(self, tmp: str, under_canonical: bool = True) -> tuple[Path, Path]:
        crate_root = Path(tmp)
        canonical = crate_root / "specs" / "_protocol_debt"
        directory = canonical if under_canonical else crate_root / "not_specs" / "_protocol_debt"
        directory.mkdir(parents=True, exist_ok=True)
        draft = load_valid()
        draft.pop("review")  # exactly what stage-3-protocol-debt-drafting produces
        (directory / "I-SCHED-TQ-001.json.draft").write_text(json.dumps(draft))
        return crate_root, canonical

    def test_find_protocol_debt_files_excludes_staged_drafts(self):
        with tempfile.TemporaryDirectory() as tmp:
            crate_root, canonical = self._crate_with_draft(tmp)
            (canonical / "I-SCHED-TQ-002.json").write_text(json.dumps(load_valid()))
            found = find_protocol_debt_files(crate_root)
        self.assertEqual([p.name for p in found], ["I-SCHED-TQ-002.json"])

    def test_the_recursive_scan_reports_nothing_for_a_staged_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            crate_root, _ = self._crate_with_draft(tmp)
            findings = validate(crate_root, default_interactions_by_id())
        self.assertEqual([str(f) for f in findings], [])

    def test_staged_draft_is_not_counted_as_discovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            crate_root, _ = self._crate_with_draft(tmp)
            self.assertEqual(count_discovered(crate_root), 0)

    def test_validate_crate_reports_nothing_for_a_staged_draft(self):
        """cmd_validate_protocol_debt's own path -- the exact command the
        pilot report's EXIT 1 came from."""
        with tempfile.TemporaryDirectory() as tmp:
            crate_root, canonical = self._crate_with_draft(tmp)
            findings = validate_crate(crate_root, canonical, default_interactions_by_id())
        self.assertEqual([str(f) for f in findings], [])

    def test_a_staged_draft_never_contributes_g15_coverage(self):
        """Coverage came from the same discovery function, so a pending
        debt record is excluded by not being discovered rather than by
        failing validation as a side effect."""
        from validate_protocol_debt import valid_interaction_ids_from_crate

        with tempfile.TemporaryDirectory() as tmp:
            crate_root, canonical = self._crate_with_draft(tmp)
            covered = valid_interaction_ids_from_crate(
                crate_root, canonical, default_interactions_by_id()
            )
        self.assertEqual(covered, set())

    def test_the_cli_exits_zero_on_a_staged_draft(self):
        """The reported symptom: `validate-protocol-debt` exit 1 with
        [G1a/error] "'review' is a required property" against the draft."""
        with tempfile.TemporaryDirectory() as tmp:
            crate_root, _ = self._crate_with_draft(tmp)
            rc = main([str(crate_root)])
        self.assertEqual(rc, 0)

    def test_staged_draft_in_a_mislocated_directory_is_still_inert(self):
        """Consistency, not just the happy path: a draft under a wrong
        directory is still a pending draft, and stage_draft only accepts
        canonical targets, so `approve --pair` could never consume it."""
        with tempfile.TemporaryDirectory() as tmp:
            crate_root, _ = self._crate_with_draft(tmp, under_canonical=False)
            findings = validate(crate_root, default_interactions_by_id())
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
            findings = validate(crate_root, default_interactions_by_id())
        self.assertTrue(
            any("'review' is a required property" in f.reason for f in findings),
            [str(f) for f in findings],
        )

    def test_wrong_extension_file_is_still_reported(self):
        """Guards the over-broad-discovery rule (a wrong-extension
        artifact under a real _protocol_debt directory must surface) from
        being collateral damage of the draft skip."""
        with tempfile.TemporaryDirectory() as tmp:
            crate_root, canonical = self._crate_with_draft(tmp)
            (canonical / "I-SCHED-TQ-001.yaml").write_text(json.dumps(load_valid()))
            findings = validate(crate_root, default_interactions_by_id())
        self.assertTrue(
            any("must be a .json file" in f.reason for f in findings),
            [str(f) for f in findings],
        )


if __name__ == "__main__":
    unittest.main()
