import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import review_checkpoint  # noqa: E402
from review_checkpoint import (  # noqa: E402
    SKIP_VALIDATION,
    ApprovalRefused,
    DraftNotStaged,
    approve,
    approve_pair,
    auto_promote_if_mechanical,
    classify,
    draft_path_for,
    is_staged_draft,
    requires_human_checkpoint,
    stage_draft,
)
from validate_boundary_contracts import load_validator, validate_data  # noqa: E402


class ReviewCheckpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = self.root / "specs" / "_boundaries" / "a__to__b.json"
        self.log = self.root / "review_log.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_time_creation_is_never_mechanical(self):
        c = classify(None, {"boundary_id": "a__to__b"})
        self.assertEqual(c, "new")
        self.assertTrue(requires_human_checkpoint(c))

    def test_semantic_change_requires_checkpoint(self):
        old = {"boundary_id": "a__to__b", "callee_guarantees": ["B.C001"], "review": {"reviewer": "alice", "reviewed_at": "2026-08-01"}}
        new = {"boundary_id": "a__to__b", "callee_guarantees": ["B.C002"], "review": {"reviewer": "alice", "reviewed_at": "2026-08-01"}}
        c = classify(old, new)
        self.assertEqual(c, "semantic")
        self.assertTrue(requires_human_checkpoint(c))

    def test_reformatting_only_change_against_approved_prior_is_mechanical(self):
        old = {"boundary_id": "a__to__b", "callee_guarantees": ["B.C001"], "review": {"reviewer": "alice", "reviewed_at": "2026-08-01"}}
        new = {"boundary_id": "a__to__b", "callee_guarantees": ["B.C001"], "review": {"reviewer": "someone-else", "reviewed_at": "2026-09-01"}}
        c = classify(old, new)
        self.assertEqual(c, "mechanical")
        self.assertFalse(requires_human_checkpoint(c))

    def test_change_against_never_approved_prior_is_not_mechanical(self):
        # Prior version exists but was never actually approved (reviewer
        # empty) -- there is no approved baseline to carve out against.
        old = {"boundary_id": "a__to__b", "callee_guarantees": ["B.C001"], "review": {"reviewer": "", "reviewed_at": ""}}
        new = {"boundary_id": "a__to__b", "callee_guarantees": ["B.C001"], "review": {"reviewer": "alice", "reviewed_at": "2026-08-01"}}
        c = classify(old, new)
        self.assertEqual(c, "semantic")

    def test_approve_requires_nonempty_reviewer(self):
        draft = stage_draft({"boundary_id": "a__to__b"}, self.target)
        with self.assertRaises(ValueError):
            approve(draft, self.target, reviewer="", review_log=self.log)

    def test_approve_writes_target_and_removes_draft_and_logs(self):
        draft = stage_draft({"boundary_id": "a__to__b"}, self.target)
        result = approve(
            draft, self.target, reviewer="alice", reviewed_at="2026-08-25",
            review_log=self.log, validate_fn=SKIP_VALIDATION,
        )

        self.assertFalse(draft.exists())
        self.assertTrue(self.target.exists())
        written = json.loads(self.target.read_text())
        self.assertEqual(written["review"], {"reviewer": "alice", "reviewed_at": "2026-08-25"})
        self.assertEqual(result.classification, "new")

        log_lines = self.log.read_text().strip().splitlines()
        self.assertEqual(len(log_lines), 1)
        entry = json.loads(log_lines[0])
        self.assertEqual(entry["reviewer"], "alice")
        self.assertEqual(entry["validation"], "skipped")  # SKIP_VALIDATION was passed above

    def test_audit_log_distinguishes_checked_from_skipped_validation(self):
        """Third review pass: 'a skipped approval is indistinguishable
        from a validated approval' in the persisted record. Both must be
        visible in the log, not just gated in code."""
        target_checked = self.root / "specs" / "_boundaries" / "scheduler_dispatch__to__task_queue_pop_ready.json"
        draft_checked = stage_draft({
            "schema_version": "1.0",
            "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "callee_guarantees": ["TaskQueue.C003"],
        }, target_checked)
        validator = load_validator()
        approve(
            draft_checked, target_checked, reviewer="alice", reviewed_at="2026-08-27",
            review_log=self.log, validate_fn=lambda p, d: validate_data(p, d, validator, specs_search_root=None),
        )

        target_skipped = self.root / "other" / "x.json"
        draft_skipped = stage_draft({"boundary_id": "a__to__b"}, target_skipped)
        approve(draft_skipped, target_skipped, reviewer="bob", reviewed_at="2026-08-27", review_log=self.log, validate_fn=SKIP_VALIDATION)

        entries = [json.loads(line) for line in self.log.read_text().strip().splitlines()]
        self.assertEqual(entries[0]["validation"], "checked")
        self.assertEqual(entries[1]["validation"], "skipped")

    def test_approve_without_validate_fn_raises_instead_of_silently_skipping(self):
        """Review finding D1 (round 2): validate_fn used to default to
        None, which silently meant 'skip validation' -- any caller that
        forgot to wire one in approved with no gate at all. Omitting it
        entirely must now be a hard TypeError, not a silent bypass."""
        draft = stage_draft({"boundary_id": "a__to__b"}, self.target)
        with self.assertRaises(TypeError):
            approve(draft, self.target, reviewer="alice", reviewed_at="2026-08-25", review_log=self.log)
        self.assertFalse(self.target.exists())  # nothing was written

    def test_auto_promote_without_validate_fn_raises(self):
        draft = stage_draft({"boundary_id": "a__to__b"}, self.target)
        approve(draft, self.target, reviewer="alice", reviewed_at="2026-08-01", review_log=self.log, validate_fn=SKIP_VALIDATION)
        draft2 = stage_draft(
            {"boundary_id": "a__to__b", "review": {"reviewer": "bot", "reviewed_at": "2026-08-02"}}, self.target
        )
        with self.assertRaises(TypeError):
            auto_promote_if_mechanical(draft2, self.target, review_log=self.log)

    def test_approve_refuses_a_g2_plus_failing_draft(self):
        """Review finding D1: approve() used to promote a draft with no
        gate at all. Now it must refuse before writing anything."""
        target = self.root / "crate_a" / "specs" / "_boundaries" / "scheduler_dispatch__to__task_queue_pop_ready.json"
        bad_draft = {
            "schema_version": "1.0",
            "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "callee_guarantees": ["TaskQueue.A006"],  # adversary case -- G2+ must reject
        }
        draft = stage_draft(bad_draft, target)

        validator = load_validator()

        def validate_fn(path, data):
            return validate_data(path, data, validator, specs_search_root=None)

        with self.assertRaises(ApprovalRefused) as ctx:
            approve(draft, target, reviewer="alice", reviewed_at="2026-08-25", review_log=self.log, validate_fn=validate_fn)

        self.assertIn("adversary case", str(ctx.exception))
        self.assertFalse(target.exists())  # nothing was promoted
        self.assertTrue(draft.exists())  # draft is untouched, still there to fix
        self.assertFalse(self.log.exists())  # no log entry for a refused approval

    def test_auto_promote_refuses_semantic_change(self):
        # Seed an approved target first.
        draft = stage_draft({"boundary_id": "a__to__b", "callee_guarantees": ["B.C001"]}, self.target)
        approve(draft, self.target, reviewer="alice", reviewed_at="2026-08-01", review_log=self.log, validate_fn=SKIP_VALIDATION)

        # Now stage a semantically different draft -- must not auto-promote.
        draft2 = stage_draft({"boundary_id": "a__to__b", "callee_guarantees": ["B.C002"]}, self.target)
        result = auto_promote_if_mechanical(draft2, self.target, review_log=self.log, validate_fn=SKIP_VALIDATION)
        self.assertIsNone(result)
        self.assertTrue(draft2.exists())  # untouched, still needs a human

    def test_auto_promote_allows_mechanical_change_and_logs_it(self):
        draft = stage_draft({"boundary_id": "a__to__b", "callee_guarantees": ["B.C001"]}, self.target)
        approve(draft, self.target, reviewer="alice", reviewed_at="2026-08-01", review_log=self.log, validate_fn=SKIP_VALIDATION)

        # Identical except the review block -- purely mechanical.
        draft2 = stage_draft(
            {"boundary_id": "a__to__b", "callee_guarantees": ["B.C001"], "review": {"reviewer": "bot", "reviewed_at": "2026-08-02"}},
            self.target,
        )
        result = auto_promote_if_mechanical(draft2, self.target, review_log=self.log, validate_fn=SKIP_VALIDATION)
        self.assertIsNotNone(result)
        self.assertEqual(result.classification, "mechanical")
        self.assertFalse(draft2.exists())

        log_lines = self.log.read_text().strip().splitlines()
        self.assertEqual(len(log_lines), 2)  # original approval + auto-promote
        self.assertIn("auto-promoted", json.loads(log_lines[1])["reviewer"])

    def test_approve_pair_refuses_before_commit_when_audit_log_cannot_be_prepared(self):
        target_a = self.root / "specs" / "_interactions" / "I-A.json"
        target_b = self.root / "specs" / "_protocol_debt" / "I-A.json"
        draft_a = stage_draft({"interaction_id": "I-A"}, target_a)
        draft_b = stage_draft({"interaction_id": "I-A"}, target_b)
        blocked_parent = self.root / "blocked-log-parent"
        blocked_parent.write_text("not a directory")
        review_log = blocked_parent / "review.jsonl"

        with self.assertRaises(FileExistsError):
            approve_pair(
                (draft_a, draft_b),
                (target_a, target_b),
                reviewer="alice",
                review_log=review_log,
                validate_fn=lambda candidates: {target_a: [], target_b: []},
            )

        self.assertFalse(target_a.exists())
        self.assertFalse(target_b.exists())
        self.assertTrue(draft_a.exists())
        self.assertTrue(draft_b.exists())


class MissingStagedDraftPreflightTest(unittest.TestCase):
    """chainlink #102: every promotion path reads `<target>.draft`, and all
    three read it unconditionally -- so a missing draft reached the operator
    as a raw FileNotFoundError traceback at exit 1, through
    `ligature approve`, `approve-pair` and `approve-exemption-pair` alike.

    These are the library-level tests: the preflight is in this module
    rather than in one CLI command, so no caller can reintroduce the
    traceback behind it (pipeline.py's own exact-line assertions are
    `MissingStagedDraftTest` in tests/test_pipeline.py)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target_a = self.root / "specs" / "_interactions" / "I-A.json"
        self.target_b = self.root / "specs" / "_protocol_debt" / "I-A.json"
        self.log = self.root / "review_log.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_draft_path_for_is_the_spelling_stage_draft_writes(self):
        for target in (self.target_a, self.root / "evidence" / "E-1.json"):
            self.assertEqual(draft_path_for(target), stage_draft({}, target))

    def test_approve_refuses_a_missing_draft_with_a_named_remedy(self):
        with self.assertRaises(DraftNotStaged) as ctx:
            approve(
                draft_path_for(self.target_a), self.target_a,
                reviewer="alice", review_log=self.log, validate_fn=SKIP_VALIDATION,
            )
        message = str(ctx.exception)
        self.assertIn(f"no staged draft at {draft_path_for(self.target_a)}", message)
        self.assertIn("ligature draft", message)
        self.assertFalse(self.target_a.exists())
        self.assertFalse(self.log.exists())

    def test_approve_refuses_a_draft_a_prior_promotion_already_consumed(self):
        """The idempotency case: approve(), then approve() again on the same
        target. The first promotion deleted the draft, so the second has
        nothing to read -- and the answer is a sentence, not a traceback."""
        draft = stage_draft({"boundary_id": "a__to__b"}, self.target_a)
        approve(draft, self.target_a, reviewer="alice", review_log=self.log, validate_fn=SKIP_VALIDATION)
        self.assertFalse(draft.exists())

        with self.assertRaises(DraftNotStaged):
            approve(draft, self.target_a, reviewer="alice", review_log=self.log, validate_fn=SKIP_VALIDATION)
        # The already-promoted target is untouched, and the refused re-run
        # appended no second audit entry.
        self.assertEqual(len(self.log.read_text().strip().splitlines()), 1)

    def test_approve_pair_names_every_missing_draft_and_writes_neither(self):
        with self.assertRaises(DraftNotStaged) as ctx:
            approve_pair(
                (draft_path_for(self.target_a), draft_path_for(self.target_b)),
                (self.target_a, self.target_b),
                reviewer="alice",
                review_log=self.log,
                validate_fn=lambda candidates: {self.target_a: [], self.target_b: []},
            )
        message = str(ctx.exception)
        self.assertIn(str(draft_path_for(self.target_a)), message)
        self.assertIn(str(draft_path_for(self.target_b)), message)
        self.assertFalse(self.target_a.exists())
        self.assertFalse(self.target_b.exists())
        self.assertFalse(self.log.exists())

    def test_approve_pair_with_one_draft_staged_names_only_the_missing_one(self):
        """The paired verbs promote all-or-none, so the refusal has to point
        at the one draft to fix and leave the staged one alone -- both to
        keep it correct, and because naming a present draft reads as "restage
        this", which is not what the operator needs to do."""
        draft_a = stage_draft({"interaction_id": "I-A"}, self.target_a)
        with self.assertRaises(DraftNotStaged) as ctx:
            approve_pair(
                (draft_a, draft_path_for(self.target_b)),
                (self.target_a, self.target_b),
                reviewer="alice",
                review_log=self.log,
                validate_fn=lambda candidates: {self.target_a: [], self.target_b: []},
            )
        message = str(ctx.exception)
        self.assertNotIn(str(draft_a), message)
        self.assertIn(str(draft_path_for(self.target_b)), message)
        self.assertTrue(draft_a.exists(), "the staged draft is left intact for a retry")

    def test_auto_promote_refuses_a_missing_draft_too(self):
        """Not reachable from a CLI verb today, but it promotes a draft the
        same way and used to read it the same unguarded."""
        with self.assertRaises(DraftNotStaged):
            auto_promote_if_mechanical(
                draft_path_for(self.target_a), self.target_a,
                review_log=self.log, validate_fn=SKIP_VALIDATION,
            )

    def test_the_preflight_does_not_shadow_a_real_refusal(self):
        """A staged draft that FAILS validation is still ApprovalRefused, not
        DraftNotStaged: one condition is 'there was a draft and it was
        wrong', the other is 'there was never a draft'. Collapsing them
        would tell an author with a broken artifact to go stage one."""
        target = self.root / "crate_a" / "specs" / "_boundaries" / "scheduler_dispatch__to__task_queue_pop_ready.json"
        draft = stage_draft({
            "schema_version": "1.0",
            "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
            "caller": {"concept": "Scheduler", "method": "dispatch"},
            "callee": {"concept": "TaskQueue", "method": "pop_ready"},
            "callee_guarantees": ["TaskQueue.A006"],  # adversary case -- G2+ must reject
        }, target)
        validator = load_validator()

        with self.assertRaises(ApprovalRefused) as ctx:
            approve(
                draft, target, reviewer="alice", review_log=self.log,
                validate_fn=lambda p, d: validate_data(p, d, validator, specs_search_root=None),
            )
        self.assertNotIsInstance(ctx.exception, DraftNotStaged)
        self.assertIn("adversary case", str(ctx.exception))


class StandaloneCliCannotPromoteTest(unittest.TestCase):
    """Third review pass (2026-08-27): --skip-validation on the standalone
    CLI was itself the bypass -- explicit didn't make it safe, it just
    made an unsafe path opt-in instead of the default. The standalone CLI
    no longer offers `approve` at all; promotion only happens through
    `pipeline.py approve`, which always resolves a real validator or
    refuses. classify/diff remain -- they're read-only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = self.root / "specs" / "_boundaries" / "a__to__b.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_approve_is_not_a_recognized_subcommand(self):
        draft = stage_draft({"boundary_id": "a__to__b"}, self.target)
        with self.assertRaises(SystemExit):
            review_checkpoint.main(["approve", str(draft), str(self.target), "--reviewer", "alice"])
        self.assertFalse(self.target.exists())

    def test_classify_still_works(self):
        draft = stage_draft({"boundary_id": "a__to__b"}, self.target)
        rc = review_checkpoint.main(["classify", str(draft), str(self.target)])
        self.assertEqual(rc, 0)


class StagedDraftIsPendingNotBrokenTest(unittest.TestCase):
    """Chainlink #110: `stage_draft()` writes `<target>.draft` and
    `approve()` renames it onto the target, so the two forms are
    distinguished by suffix alone. The predicate that says so is one
    definition, `is_staged_draft`, which every artifact-kind discovery
    function now filters on -- before it, each wrote the rule out for
    itself and four of the nine draft-capable kinds (#90 fixed two) never
    learned it at all."""

    def test_the_predicate_and_the_path_agree(self):
        target = Path("specs/_bridges/BR-X-001.json")
        staged = draft_path_for(target)
        self.assertEqual(staged, Path("specs/_bridges/BR-X-001.json.draft"))
        self.assertTrue(is_staged_draft(staged))
        self.assertFalse(is_staged_draft(target))

    def test_the_path_stage_draft_actually_wrote_is_recognized(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "specs" / "_witnesses" / "task_queue.load_factor.json"
            written = stage_draft({"witness_id": "W-X"}, target)
            self.assertEqual(written, draft_path_for(target))
            self.assertTrue(is_staged_draft(written))

    def test_the_suffix_alone_decides(self):
        """Never "has no review block": an unreviewed artifact at its real
        path is a draft-LIFECYCLE record, which `check` reports as a
        pending human decision -- the opposite disposition."""
        self.assertFalse(is_staged_draft(Path("specs/_bridges/BR-X-001.json")))
        self.assertFalse(is_staged_draft(Path("specs/_bridges/BR-X-001.yaml")))


class EveryDraftCapableKindExcludesItsOwnStagedDraftsTest(unittest.TestCase):
    """The convention as a property of the whole pipeline rather than of
    five separately-fixed modules: for every directory `ligature draft`
    can stage into (derived from scripts/draft_templates.py's own
    registry, so a template added later is covered automatically), no
    validator discovers, validates or counts a `<name>.json.draft`.

    Each per-kind test in tests/test_validate_<kind>.py pins one module's
    own entry points in detail; this one exists because the bug was
    systematically missed -- the identical defect shipped in five modules
    after #90 fixed two, and nothing mechanical said the rule applied
    beyond the two."""

    def _validator_modules(self):
        import project_state

        return [importlib.import_module(name) for name, _ in project_state._VALIDATE_MODULES]

    def test_a_staged_draft_in_every_draft_capable_directory_is_invisible(self):
        import draft_templates

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            staged_dirs = []
            for template in draft_templates.DRAFT_TEMPLATES:
                # targets are spelled `<crate_dir>/specs/<dir>/<name>.json`;
                # materializing `<crate_dir>` as a real crate directory is
                # the only substitution needed.
                directory = template.target.rsplit("/", 1)[0].replace("<crate_dir>", "crates/a")
                directory_path = workspace / directory
                directory_path.mkdir(parents=True, exist_ok=True)
                staged = directory_path / "staged_artifact.json.draft"
                staged.write_text("{}")
                staged_dirs.append(directory)

            for module in self._validator_modules():
                findings = module.validate(workspace)
                self.assertEqual(
                    [str(f) for f in findings],
                    [],
                    f"{module.__name__}.validate() reported a staged draft: {staged_dirs}",
                )
                self.assertEqual(
                    module.count_discovered(workspace),
                    0,
                    f"{module.__name__}.count_discovered() counted a staged draft: {staged_dirs}",
                )


if __name__ == "__main__":
    unittest.main()
