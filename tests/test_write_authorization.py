"""Focused unit tests for the write-grant PRODUCER (chainlink #114a).

`tests/test_authorize_write.py` drives the whole feature through the real
CLI, which is the right level for "what do the commands report". This module
is the other level: `scripts/write_authorization.py` on its own, with the
descriptor supplied as a dict and the clock supplied as a timestamp, so each
refusal can be attributed to the precondition that causes it rather than to
the surrounding command wiring -- and so a precondition the CLI cannot
easily reach (a record appended by something other than `authorize-write`,
a damaged ledger, a TTL no issuer could have been granted) is still pinned.

What is pinned here, in the order the issue states it:

  * path binding -- exact or explicitly bounded, syntactically, never
    resolved, and refused when absolute, escaping, a directory, or a
    vacuous pattern that names no fixed location;
  * the binding fields themselves -- issue, operation, issuer and issuer
    kind, one-shot, `issued_at`, TTL -- and `grant_id` as a hash of
    exactly those, so an entry cannot be edited into a different
    authorization while keeping its id;
  * the reader, which recomputes every one of those claims rather than
    trusting the record's own field -- vocabulary, normalization,
    boundedness, the TTL's bounds, the `expires_at == issued_at + ttl`
    relation and the id -- with a deterministic reason per refusal;
  * the statuses the reader reports for a record it will not honor
    (expired, other-issue, duplicate, unverified-issuer, no-issue-named);
  * every refusal `authorize_write()` owes the caller, each of which
    writes nothing at all: a widening path, a path `allowed_roots` already
    permits, an undeclared supervisor, a TTL outside its bounds, a
    one-shot on a pattern, a replayed grant, a ledger that sits inside a
    protected root or outside the workspace, and a damaged ledger;
  * the append: one line, field for field the record `as_record()`
    defines, an existing line never rewritten, and a retry of the issuing
    command idempotent rather than a way to stack grants.

Deliberately NOT here: what a grant authorizes at consumption time (which
issue's writes it covers in a `write-set-check` report, the four surfaces
agreeing, one-shot being reported `spent`) -- that is the enforcement half
and it has its own tests in `tests/test_authorize_write.py`.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import write_authorization as wa  # noqa: E402
from project_descriptor import ProjectDescriptorError  # noqa: E402

ISSUED = "2026-10-03T12:00:00+00:00"
HOUR = 3600
#: A grant's own binding, the minimum a unit test has to state. `issued_at`
#: and `ttl_seconds` are the two that make the expiry relation derivable
#: without a clock, which is why every case here can be decided by
#: arithmetic.
BINDING = {
    "issue": 114,
    "path": "ci/manifest/WP-114.json",
    "path_kind": "exact",
    "op": "write",
    "one_shot": False,
    "issuer": "operator",
    "issuer_kind": "human",
    "issued_at": ISSUED,
    "ttl_seconds": HOUR,
}

#: A workspace's declared write set, trimmed to what a grant reasons about:
#: one writable root, and protected roots that are not a directory of
#: everything (the vacuous `**` shape is #77's refusal, tested elsewhere).
DESCRIPTOR = {
    "write_set": {
        "allowed_roots": ["rust/*/src/"],
        "protected_roots": ["ci/manifest/**", "scripts/**"],
    }
}


def expires_after(binding: dict) -> str:
    issued = datetime.fromisoformat(binding["issued_at"])
    return (issued + timedelta(seconds=binding["ttl_seconds"])).isoformat()


def valid_record(**overrides) -> dict:
    """A record the reader must accept: the binding, its recomputed id, and
    `expires_at` derived from `issued_at + ttl_seconds`. `reid=False` keeps a
    stale id on purpose, which is how the edited-in-place cases are stated.
    A malformed override (a non-integer TTL) has no derivable expiry, so the
    relation is left for the reader to complain about instead."""
    binding = dict(BINDING)
    record = dict(BINDING)
    record.update(overrides)
    binding.update({k: v for k, v in overrides.items() if k in wa._BINDING_FIELDS})
    if overrides.pop("reid", True):
        record["grant_id"] = wa.grant_id_for(binding)
    record.setdefault("event", "grant")
    record.setdefault("schema_version", wa.GRANT_SCHEMA_VERSION)
    record.setdefault("grant_id", "GW-000000000000")
    if "expires_at" not in overrides:
        ttl = binding["ttl_seconds"]
        record["expires_at"] = expires_after(binding) if isinstance(ttl, int) else ISSUED
    record.setdefault("note", None)
    return record


class WorkspaceFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name).resolve()
        self.outside = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(shutil.rmtree, self.outside, ignore_errors=True)

    @property
    def ledger(self) -> Path:
        return wa.grant_ledger_path(self.workspace)

    def issue(self, **kwargs) -> wa.Grant:
        """Record one grant with every binding field spelled out."""
        request = {
            "issue": 114,
            "path": BINDING["path"],
            "op": "write",
            "issuer": "operator",
            "issued_at": ISSUED,
            "ttl_seconds": HOUR,
        }
        request.update(kwargs)
        return wa.authorize_write(self.workspace, descriptor=dict(DESCRIPTOR), **request)

    def ledger_lines(self) -> list[str] | None:
        """The ledger's lines, or None when there is no ledger file."""
        return self.ledger.read_text().splitlines() if self.ledger.is_file() else None

    def assertRefused(self, fragment: str, **kwargs):
        """`issue()` refused with a diagnostic naming `fragment`, and nothing
        written -- a refusal that left a line behind would be a partial grant,
        and one that created the ledger would be a write nobody asked for."""
        before = self.ledger_lines()
        with self.assertRaises(wa.WriteAuthorizationError) as raised:
            self.issue(**kwargs)
        self.assertIn(fragment, str(raised.exception))
        self.assertEqual(self.ledger_lines(), before, "a refused grant must write nothing")
        return str(raised.exception)


class PathBindingTest(unittest.TestCase):
    """`classify_granted_path`: the grant's path is classified syntactically,
    and a path with no fixed location in it is refused rather than bound."""

    def test_an_exact_path_is_bound_as_exact(self):
        for raw, expected in (
            ("ci/manifest/WP-114.json", "ci/manifest/WP-114.json"),
            (" scripts/evil.py ", "scripts/evil.py"),
            ("./ci/manifest/WP-114.json", "ci/manifest/WP-114.json"),
            ("ci//manifest/WP-114.json", "ci/manifest/WP-114.json"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(
                    wa.classify_granted_path(raw), (expected, wa.PATH_KIND_EXACT)
                )

    def test_a_bounded_pattern_is_bound_as_a_pattern(self):
        for raw in ("ci/manifest/WP-114*.json", "ci/manifest/WP-11?-x.json", "scripts/[ab].py"):
            with self.subTest(raw=raw):
                self.assertEqual(wa.classify_granted_path(raw)[1], wa.PATH_KIND_PATTERN)

    def test_an_unbound_path_is_refused(self):
        for raw, fragment in (
            ("", "--path is required"),
            ("   ", "--path is required"),
            (None, "--path is required"),
            (42, "--path is required"),
            ("/etc/passwd", "must be workspace-relative"),
            ("~/secrets", "must be workspace-relative"),
            ("ci/manifest/", "names a directory"),
            ("ci/manifest/../manifest/WP-114.json", "escapes the workspace"),
            ("../outside/PROBE.json", "escapes the workspace"),
            (".", "names no path"),
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(wa.WriteAuthorizationError) as raised:
                    wa.classify_granted_path(raw)
                self.assertIn(fragment, str(raised.exception))

    def test_a_pattern_with_no_fixed_location_is_refused(self):
        """A grant is a capability. `*/*` covers every two-segment path and
        `**` covers every path, so a grant naming either would be a standing
        permission over the workspace -- refused even though a
        `protected_roots` pattern would happily have been satisfied."""
        for raw in ("**", "*/*", "*/**", "*/*/*", "**/*"):
            with self.subTest(raw=raw):
                with self.assertRaises(wa.WriteAuthorizationError) as raised:
                    wa.classify_granted_path(raw)
                self.assertIn("not a bounded pattern", str(raised.exception))

    def test_one_literal_segment_is_enough_to_anchor_a_pattern(self):
        self.assertEqual(
            wa.classify_granted_path("ci/manifest/WP-*.json"), ("ci/manifest/WP-*.json", "pattern")
        )


class GrantIdTest(unittest.TestCase):
    """`grant_id` is a hash of the fields the grant's authority is OF, so a
    record cannot be edited into a different authorization and keep its id."""

    def test_the_id_is_the_prefix_and_twelve_hex_digits(self):
        grant_id = wa.grant_id_for(BINDING)
        digest = grant_id[len(wa.GRANT_ID_PREFIX):]
        self.assertTrue(grant_id.startswith(wa.GRANT_ID_PREFIX))
        self.assertEqual(len(digest), wa._GRANT_ID_HEX_DIGITS)
        self.assertTrue(all(ch in "0123456789abcdef" for ch in digest), grant_id)

    def test_every_binding_field_changes_the_id(self):
        base = wa.grant_id_for(BINDING)
        for field, changed in (
            ("issue", 115),
            ("path", "ci/manifest/WP-115.json"),
            ("path_kind", "pattern"),
            ("op", "delete"),
            ("one_shot", True),
            ("issuer", "someone-else"),
            ("issuer_kind", "supervisor"),
            ("issued_at", "2026-10-03T12:00:01+00:00"),
            ("ttl_seconds", HOUR + 1),
        ):
            with self.subTest(field=field):
                self.assertNotEqual(base, wa.grant_id_for({**BINDING, field: changed}))

    def test_audit_metadata_is_outside_the_id(self):
        """`note` authorizes nothing, so it must not be able to change what a
        grant is -- neither to create an authorization nor to invalidate a
        real one. A record carries `logged_at` nowhere; the point is that
        whatever prose arrives with a binding cannot reach the hash."""
        self.assertNotIn("note", wa._BINDING_FIELDS)
        self.assertNotIn("logged_at", wa._BINDING_FIELDS)
        self.assertEqual(wa.grant_id_for({**BINDING, "note": "fine by me"}), wa.grant_id_for(BINDING))


class ParseGrantTest(unittest.TestCase):
    """The reader recomputes every claim the record makes. Each refusal below
    is a claim it refuses to take on the record's word."""

    def assertRejected(self, fragment: str, record, line: int = 7):
        grant, reason = wa.parse_grant(record, line)
        self.assertIsNone(grant, f"{record!r} should not be usable")
        self.assertIn(fragment, reason or "")

    def test_a_well_formed_record_parses_and_round_trips(self):
        record = valid_record()
        grant, reason = wa.parse_grant(record, 7)
        self.assertIsNone(reason)
        self.assertEqual(grant.issue, 114)
        self.assertEqual(grant.path, "ci/manifest/WP-114.json")
        self.assertEqual(grant.path_kind, wa.PATH_KIND_EXACT)
        self.assertEqual(grant.op, "write")
        self.assertFalse(grant.one_shot)
        self.assertEqual(grant.issuer, "operator")
        self.assertEqual(grant.issuer_kind, "human")
        self.assertEqual(grant.ttl_seconds, HOUR)
        self.assertEqual(grant.expires_at, "2026-10-03T13:00:00+00:00")
        self.assertEqual(grant.line, 7)
        # the record the reader accepted is the record the writer appends
        self.assertEqual(grant.as_record(), record)
        # ...and re-parsing what was appended is still the same grant
        self.assertEqual(wa.parse_grant(grant.as_record(), 7)[0], grant)

    def test_a_line_that_is_not_a_grant_record_is_not_one(self):
        self.assertRejected("not a JSON object", ["grant"])
        self.assertRejected("not a grant record", {"event": "revoke"})

    def test_a_missing_binding_field_is_named(self):
        for field in wa._REQUIRED_FIELDS:
            with self.subTest(field=field):
                record = valid_record()
                record.pop(field)
                self.assertRejected(f"missing {field}", record)
        record = valid_record()
        for field in ("grant_id", "expires_at"):
            record.pop(field)
        self.assertRejected("missing grant_id, expires_at", record)

    def test_an_unknown_schema_version_is_refused(self):
        self.assertRejected("schema_version", valid_record(schema_version="2.0"))

    def test_the_issue_must_be_a_positive_integer(self):
        for issue in (0, -1, "114", 11.4, True, None):
            with self.subTest(issue=issue):
                self.assertRejected("is not a positive integer", valid_record(issue=issue))

    def test_the_operation_vocabulary_is_closed(self):
        for op in ("chmod", "WRITE", "", 1):
            with self.subTest(op=op):
                self.assertRejected("is not one of write, delete", valid_record(op=op))

    def test_the_issuer_is_a_named_identity_of_a_known_kind(self):
        for issuer in ("", "   ", 7, None):
            with self.subTest(issuer=issuer):
                self.assertRejected("issuer is empty", valid_record(issuer=issuer))
        for kind in ("robot", "", None):
            with self.subTest(kind=kind):
                self.assertRejected("issuer_kind", valid_record(issuer_kind=kind))

    def test_one_shot_is_a_boolean(self):
        for value in ("true", 1, None):
            with self.subTest(value=value):
                self.assertRejected("one_shot", valid_record(one_shot=value))

    def test_the_ttl_is_bounded_on_both_ends(self):
        for ttl in (0, -1, "3600", 3600.0, True):
            with self.subTest(ttl=ttl):
                self.assertRejected("is not a positive integer", valid_record(ttl_seconds=ttl))
        # the ceiling is the writer's AND the reader's rule: a line appended
        # by something other than `authorize-write` must not buy standing
        # authority no issuer could have been granted, however
        # self-consistent its expiry relation is.
        self.assertRejected(
            "exceeds the 86400s ceiling",
            valid_record(ttl_seconds=wa.MAX_TTL_SECONDS + 1),
        )
        self.assertEqual(
            wa.parse_grant(valid_record(ttl_seconds=wa.MAX_TTL_SECONDS), 1)[0].ttl_seconds,
            wa.MAX_TTL_SECONDS,
        )

    def test_note_must_be_a_string_or_null(self):
        for note in (7, [], {}):
            with self.subTest(note=note):
                self.assertRejected("note is not a string", valid_record(note=note))
        self.assertIsNotNone(wa.parse_grant(valid_record(note="sanctioned"), 1)[0])

    def test_the_path_must_be_the_normal_form_the_classifier_derives(self):
        for raw, fragment in (
            ("/ci/manifest/WP-114.json", "must be workspace-relative"),
            ("ci/manifest/../manifest/WP-114.json", "escapes the workspace"),
            ("ci/manifest/", "names a directory"),
            ("./ci/manifest/WP-114.json", "not normalized"),
            ("ci//manifest/WP-114.json", "not normalized"),
            ("  ci/manifest/WP-114.json  ", "not normalized"),
        ):
            with self.subTest(raw=raw):
                self.assertRejected(fragment, valid_record(path=raw))

    def test_path_kind_must_agree_with_the_path_itself(self):
        self.assertRejected(
            "disagrees with the path itself",
            valid_record(path="ci/manifest/WP-114*.json", path_kind=wa.PATH_KIND_EXACT),
        )
        self.assertRejected(
            "disagrees with the path itself",
            valid_record(path_kind=wa.PATH_KIND_PATTERN),
        )

    def test_a_one_shot_grant_binds_one_exact_file(self):
        self.assertRejected(
            "one_shot with path_kind 'pattern'",
            valid_record(path="ci/manifest/WP-114*.json", path_kind="pattern", one_shot=True),
        )
        self.assertIsNotNone(wa.parse_grant(valid_record(one_shot=True), 1)[0])

    def test_a_timestamp_needs_an_offset(self):
        for field in ("issued_at", "expires_at"):
            with self.subTest(field=field):
                record = valid_record()
                record[field] = "2026-10-03T12:00:00"
                self.assertRejected("ISO-8601 timestamp with a UTC offset", record)
        for value in ("yesterday", "", 1760000000, None):
            with self.subTest(value=value):
                record = valid_record()
                record["issued_at"] = value
                self.assertRejected("ISO-8601 timestamp with a UTC offset", record)

    def test_expires_at_must_equal_issued_at_plus_ttl(self):
        self.assertRejected(
            "cannot outlive the TTL it names",
            valid_record(expires_at="2099-01-01T00:00:00+00:00"),
        )
        self.assertRejected(
            "cannot outlive the TTL it names",
            valid_record(expires_at="2026-10-03T12:59:59+00:00"),
        )

    def test_an_id_that_does_not_match_the_binding_it_names_is_refused(self):
        """The one edit the recomputed id exists to catch: moving the
        authorization to another path, issue, operation, issuer or expiry
        under a preserved id."""
        record = valid_record(reid=False)
        record["path"] = "ci/manifest/OTHER.json"
        self.assertRejected("edited in place", record)

    def test_covers_binds_exactly_and_a_pattern_only_what_it_names(self):
        grant = wa.parse_grant(valid_record(), 1)[0]
        self.assertTrue(grant.covers("ci/manifest/WP-114.json"))
        self.assertFalse(grant.covers("ci/manifest/WP-115.json"))
        self.assertFalse(grant.covers("scripts/evil.py"))
        patterned = wa.parse_grant(
            valid_record(path="ci/manifest/WP-114*.json", path_kind="pattern"), 1
        )[0]
        self.assertTrue(patterned.covers("ci/manifest/WP-114-a.json"))
        self.assertFalse(patterned.covers("ci/manifest/WP-115-a.json"))


class LedgerTest(WorkspaceFixture):
    """`read_grant_ledger`: a trail is read, and a trail that cannot be read
    or cannot be understood proves no authorization at all."""

    def ledger_with(self, *records: object) -> None:
        """Append raw lines. A string is one raw line (already terminated or
        not), so the line numbers under test can be placed exactly."""
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger.open("a") as stream:
            for record in records:
                text = record if isinstance(record, str) else json.dumps(record, sort_keys=True)
                stream.write(text if text.endswith("\n") else text + "\n")

    def test_the_ledger_is_workspace_scoped_at_a_pipeline_location(self):
        """The #45 lesson: the audit trail follows the workspace, not the cwd.
        `ci/results/` is the pipeline's own output location, so recording a
        grant is not itself an out-of-set write."""
        self.assertEqual(wa.grant_ledger_path(self.workspace), self.workspace / wa.GRANT_LEDGER_REL)
        self.assertTrue(wa.GRANT_LEDGER_REL.startswith("ci/results/"))

    def test_a_workspace_with_no_grants_reports_the_ordinary_state(self):
        ledger = wa.read_grant_ledger(self.workspace)
        self.assertEqual(ledger.state, "absent")
        self.assertEqual(ledger.entries, [])
        self.assertEqual(ledger.rejected, [])
        self.assertIsNone(ledger.error)
        # absent is not an error: it is a workspace that has issued no grant
        self.assertEqual(ledger.active_grants(issue=114), [])

    def test_a_recorded_grant_is_read_with_its_line_number(self):
        self.ledger_with("\n", valid_record(), "   \n", valid_record(path="ci/manifest/WP-115.json"))
        ledger = wa.read_grant_ledger(self.workspace)
        self.assertEqual(ledger.state, "readable")
        self.assertEqual([g.line for g in ledger.entries], [2, 4])
        self.assertEqual(ledger.rejected, [])
        self.assertEqual(ledger.ignored, 0)

    def test_a_line_that_is_valid_json_but_not_an_event_is_ignored(self):
        """The ledger is a trail of events, so a future event kind must not be
        read as a grant -- counted, not rejected."""
        self.ledger_with({"event": "revoke", "grant_id": "GW-000000000000"}, valid_record())
        ledger = wa.read_grant_ledger(self.workspace)
        self.assertEqual(ledger.ignored, 1)
        self.assertEqual([g.issue for g in ledger.entries], [114])
        self.assertEqual(ledger.rejected, [])

    def test_a_line_claiming_to_be_a_grant_and_failing_is_reported_not_dropped(self):
        """Dropping it silently would read as 'no grant here', which is the
        one thing a damaged authorization record must not be able to say."""
        self.ledger_with("{not json\n", valid_record(op="chmod"))
        ledger = wa.read_grant_ledger(self.workspace)
        self.assertEqual([r.line for r in ledger.rejected], [1, 2])
        self.assertIn("not valid JSON", ledger.rejected[0].reason)
        self.assertEqual(ledger.entries, [])
        # and a ledger with only damaged lines proves nothing at all
        self.assertEqual(ledger.active_grants(issue=114), [])

    def test_an_unreadable_ledger_yields_no_grants(self):
        """Fail closed, the same reading a damaged human-ruling log gets: a
        trail that cannot be read proves no authorization."""
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.mkdir()  # a directory where the ledger should be
        ledger = wa.read_grant_ledger(self.workspace)
        self.assertEqual(ledger.state, "unreadable")
        self.assertIsNotNone(ledger.error)
        self.assertIn("cannot be read", ledger.error)
        self.assertEqual(ledger.entries, [])
        self.assertEqual(ledger.statuses(issue=114), [])

    def test_undecodable_bytes_are_unreadable_not_empty(self):
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_bytes(b'{"event": "grant", "issue": "\xff\xfe"}\n')
        ledger = wa.read_grant_ledger(self.workspace)
        self.assertEqual(ledger.state, "unreadable")
        self.assertEqual(ledger.entries, [])


class LedgerStatusTest(WorkspaceFixture):
    """A record the reader will not honor is reported with the status that
    says why, rather than quietly vanishing from the audit view."""

    def ledger_with(self, *records: dict) -> wa.GrantLedger:
        """A ledger holding exactly these records -- one case per call, so a
        status never inherits an entry an earlier case left behind."""
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
        )
        return wa.read_grant_ledger(self.workspace)

    def statuses(self, ledger: wa.GrantLedger, **kwargs) -> list[str]:
        return [status for _, status in ledger.statuses(**kwargs)]

    def now(self) -> datetime:
        return datetime.fromisoformat(ISSUED) + timedelta(seconds=60)

    def test_a_matching_unexpired_grant_is_active(self):
        ledger = self.ledger_with(valid_record())
        self.assertEqual(self.statuses(ledger, issue=114, now=self.now()), ["active"])
        self.assertEqual(len(ledger.active_grants(issue=114, now=self.now())), 1)
        self.assertEqual(
            ledger.authorizing("ci/manifest/WP-114.json", "write", issue=114, now=self.now()),
            ledger.entries[0],
        )

    def test_no_issue_named_consults_no_grant(self):
        ledger = self.ledger_with(valid_record())
        self.assertEqual(self.statuses(ledger, issue=None, now=self.now()), ["no-issue-named"])
        self.assertEqual(ledger.active_grants(issue=None, now=self.now()), [])

    def test_a_grant_for_another_issue_is_other_issue(self):
        ledger = self.ledger_with(valid_record(issue=113))
        self.assertEqual(self.statuses(ledger, issue=114, now=self.now()), ["other-issue"])

    def test_an_expired_grant_is_reported_expired_not_dropped(self):
        ledger = self.ledger_with(valid_record())
        later = datetime.fromisoformat(ISSUED) + timedelta(seconds=HOUR)
        # the boundary is inclusive: now == expires_at is expired
        self.assertEqual(self.statuses(ledger, issue=114, now=later), ["expired"])
        self.assertEqual(ledger.active_grants(issue=114, now=later), [])

    def test_a_repeated_id_leaves_the_last_entry_authorizing(self):
        """Append-only, so re-issuing supersedes rather than edits -- and
        neither copy becomes two authorizations."""
        ledger = self.ledger_with(valid_record(), valid_record())
        self.assertEqual(
            self.statuses(ledger, issue=114, now=self.now()), ["duplicate", "active"]
        )
        self.assertEqual(len(ledger.active_grants(issue=114, now=self.now())), 1)

    def test_a_supervisor_grant_needs_the_declared_whitelist(self):
        """Re-verified against the descriptor on every read, so withdrawing the
        identity withdraws the grant -- and a hand-written record naming an
        undeclared supervisor authorizes nothing."""
        ledger = self.ledger_with(valid_record(issuer="ci-bot", issuer_kind="supervisor"))
        declared = frozenset({"ci-bot"})
        self.assertEqual(
            self.statuses(ledger, issue=114, supervisors=declared, now=self.now()), ["active"]
        )
        self.assertEqual(
            self.statuses(ledger, issue=114, supervisors=frozenset(), now=self.now()),
            ["unverified-issuer"],
        )
        self.assertEqual(
            ledger.active_grants(issue=114, supervisors=frozenset(), now=self.now()), []
        )

    def test_the_human_lane_needs_no_whitelist_entry(self):
        ledger = self.ledger_with(valid_record())
        self.assertEqual(
            self.statuses(ledger, issue=114, supervisors=frozenset(), now=self.now()), ["active"]
        )

    def test_status_is_one_reason_per_entry_in_a_fixed_order(self):
        """A record that is several kinds of unusable at once is reported
        once: `duplicate` is a fact about the ledger, `other-issue` is this
        run's business, `expired` is a fact about the record, and
        `unverified-issuer` is last because it is the only one a descriptor
        can settle."""
        far_later = datetime.fromisoformat(ISSUED) + timedelta(days=2)
        expired = valid_record(ttl_seconds=1, issuer="ci-bot", issuer_kind="supervisor")
        # another issue's record, this run's: not this run's business at all
        self.assertEqual(
            self.statuses(
                self.ledger_with(valid_record(**{**expired, "issue": 113})),
                issue=114,
                now=far_later,
            ),
            ["other-issue"],
        )
        # this run's own issue: expired -- not yet the whitelist, which is the
        # one reason only the descriptor can settle
        self.assertEqual(
            self.statuses(self.ledger_with(expired), issue=114, now=far_later), ["expired"]
        )
        # and a repeated id outranks all of them: it is the same grant twice
        self.assertEqual(
            self.statuses(self.ledger_with(expired, expired), issue=114, now=far_later),
            ["duplicate", "expired"],
        )

    def test_authorization_matches_the_operation_it_names(self):
        """A `delete` grant is recorded and auditable, but a file walk cannot
        observe a deletion, so it never authorizes an existing file."""
        ledger = self.ledger_with(valid_record(op="delete"))
        self.assertIsNone(
            ledger.authorizing("ci/manifest/WP-114.json", "write", issue=114, now=self.now())
        )
        self.assertIsNotNone(
            ledger.authorizing("ci/manifest/WP-114.json", "delete", issue=114, now=self.now())
        )

    def test_supervisor_authorities_reads_the_whitelist_and_defaults_to_none(self):
        self.assertEqual(wa.supervisor_authorities(None), frozenset())
        self.assertEqual(wa.supervisor_authorities({}), frozenset())
        self.assertEqual(wa.supervisor_authorities({"write_set": None}), frozenset())
        self.assertEqual(
            wa.supervisor_authorities({"write_set": {"authorized_supervisors": "ci-bot"}}), frozenset()
        )
        self.assertEqual(
            wa.supervisor_authorities(
                {"write_set": {"authorized_supervisors": ["ci-bot", "", 7, "other"]}}
            ),
            frozenset({"ci-bot", "other"}),
        )


class AuthorizeWriteTest(WorkspaceFixture):
    """Every refusal `authorize_write()` owes a caller, and the append it owes
    the ledger when none of them fires."""

    def test_a_sanctioned_protected_write_is_recorded_as_one_appended_line(self):
        grant = self.issue()
        self.assertEqual(grant.line, 1)
        self.assertEqual(grant.expires_at, "2026-10-03T13:00:00+00:00")
        self.assertEqual(grant.grant_id, wa.grant_id_for(BINDING))
        lines = self.ledger.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        # the appended line is exactly the record `as_record()` defines
        self.assertEqual(json.loads(lines[0]), grant.as_record())
        self.assertEqual(wa.read_grant_ledger(self.workspace).entries, [grant])

    def test_a_bounded_pattern_and_a_one_shot_exact_path_are_recorded(self):
        pattern = self.issue(path="ci/manifest/WP-114*.json")
        self.assertEqual(pattern.path_kind, wa.PATH_KIND_PATTERN)
        self.assertFalse(pattern.one_shot)
        one_shot = self.issue(path="ci/manifest/WP-115.json", one_shot=True, ttl_seconds=120)
        self.assertTrue(one_shot.one_shot)
        self.assertEqual(one_shot.expires_at, "2026-10-03T12:02:00+00:00")
        self.assertEqual([g.line for g in wa.read_grant_ledger(self.workspace).entries], [1, 2])

    def test_a_delete_grant_is_recorded_verbatim(self):
        self.assertEqual(self.issue(op="delete").op, "delete")

    def test_the_ttl_defaults_to_a_bounded_non_standing_window(self):
        self.assertEqual(wa.DEFAULT_TTL_SECONDS, 900)
        self.assertEqual(wa.MAX_TTL_SECONDS, 86_400)
        # the default is the producer's, not this fixture's
        grant = wa.authorize_write(
            self.workspace,
            issue=114,
            path=BINDING["path"],
            op="write",
            issuer="operator",
            issued_at=ISSUED,
            descriptor=dict(DESCRIPTOR),
        )
        self.assertEqual(grant.ttl_seconds, wa.DEFAULT_TTL_SECONDS)
        self.assertEqual(grant.expires_at, "2026-10-03T12:15:00+00:00")

    def test_the_record_is_written_nothing_but_the_ledger(self):
        def files() -> list[str]:
            return sorted(
                p.relative_to(self.workspace).as_posix()
                for p in self.workspace.rglob("*")
                if p.is_file()
            )

        self.assertEqual(files(), [])
        self.issue()
        self.assertEqual(files(), [wa.GRANT_LEDGER_REL])

    def test_a_second_grant_appends_and_leaves_the_first_line_untouched(self):
        self.issue()
        first = self.ledger.read_text()
        self.issue(path="ci/manifest/WP-115.json", issue=115)
        lines = self.ledger.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(self.ledger.read_text().startswith(first))
        self.assertEqual([g.path for g in wa.read_grant_ledger(self.workspace).entries],
                         ["ci/manifest/WP-114.json", "ci/manifest/WP-115.json"])

    def test_an_identical_re_issue_is_a_replay_and_records_nothing(self):
        """A retry of the issuing command is idempotent rather than a way to
        stack grants -- the id is a hash of the binding, so the same request
        at the same instant is the same grant."""
        self.issue()
        self.assertRefused("is a replay", note="and this time it is definitely fine")
        self.assertEqual(len(self.ledger_lines()), 1)

    def test_a_different_instant_is_a_different_grant(self):
        self.issue()
        later = self.issue(issued_at="2026-10-03T13:00:00+00:00")
        self.assertNotEqual(wa.grant_id_for(BINDING), later.grant_id)

    def test_the_issue_must_be_a_positive_integer(self):
        for issue in (0, -3, True, "114", None):
            with self.subTest(issue=issue):
                self.assertRefused("is not a positive integer", issue=issue)

    def test_the_operation_and_issuer_kind_vocabularies_are_closed(self):
        self.assertRefused("must be one of", op="chmod")
        self.assertRefused("must be one of", issuer_kind="robot")

    def test_the_issuer_is_required_and_named(self):
        self.assertRefused("--issuer is required", issuer="  ")
        self.assertRefused("--issuer is required", issuer="")
        self.assertRefused("--issuer is required", issuer=None)
        # trimmed, so the id binds the identity and not its padding
        self.assertEqual(self.issue(issuer="  operator  ").issuer, "operator")

    def test_the_ttl_is_bounded_on_both_ends(self):
        self.assertRefused("must be a positive number of seconds", ttl_seconds=0)
        self.assertRefused("must be a positive number of seconds", ttl_seconds=-1)
        self.assertRefused("must be a positive number of seconds", ttl_seconds=HOUR + 0.5)
        self.assertRefused("exceeds the", ttl_seconds=wa.MAX_TTL_SECONDS + 1)
        self.assertEqual(self.issue(ttl_seconds=wa.MAX_TTL_SECONDS).ttl_seconds, wa.MAX_TTL_SECONDS)

    def test_an_unbound_path_is_refused_before_anything_is_written(self):
        for path, fragment in (
            ("", "--path is required"),
            ("/etc/passwd", "must be workspace-relative"),
            ("../outside/PROBE.json", "escapes the workspace"),
            ("ci/manifest/", "names a directory"),
            ("*/*", "not a bounded pattern"),
        ):
            with self.subTest(path=path):
                self.assertRefused(fragment, path=path)

    def test_a_one_shot_grant_binds_one_exact_file(self):
        self.assertRefused(
            "one-shot grant binds one exact file",
            path="ci/manifest/WP-114*.json",
            one_shot=True,
        )

    def test_an_issued_at_without_an_offset_is_refused(self):
        for value in ("2026-10-03T12:00:00", "yesterday", "", "noon", 1760000000):
            with self.subTest(value=value):
                self.assertRefused("is not an ISO-8601 timestamp", issued_at=value)
        # an offset other than UTC is normalized, so the id binds one instant
        self.assertEqual(
            self.issue(issued_at="2026-10-03T14:00:00+02:00").issued_at, ISSUED
        )

    def test_a_grant_cannot_widen_allowed_roots(self):
        """Two refusals, and the more specific one first: a path
        `allowed_roots` already permits is clean without any grant, so a
        record naming it is a permission-shaped object with no work to do."""
        self.assertRefused(
            "is already under allowed_roots pattern",
            path="rust/date-creusot-core/src/lib.rs",
        )
        self.assertRefused(
            "names nothing any declared protected_roots pattern covers",
            path="rust/rogue/evil.rs",
        )
        self.assertRefused(
            "names nothing any declared protected_roots pattern covers",
            path="docs/notes.md",
        )

    def test_a_workspace_declaring_no_protected_roots_is_told_so(self):
        with self.assertRaises(wa.WriteAuthorizationError) as raised:
            wa.authorize_write(
                self.workspace,
                issue=114,
                path="ci/manifest/WP-114.json",
                op="write",
                issuer="operator",
                descriptor={"write_set": {"allowed_roots": ["src/**"]}},
            )
        self.assertIn("declares no protected_roots", str(raised.exception))
        self.assertFalse(self.ledger.exists())

    def test_a_supervisor_issuer_must_be_declared(self):
        self.assertRefused(
            "is not a declared supervisor",
            issuer="ci-bot",
            issuer_kind="supervisor",
        )
        descriptor = {
            "write_set": {
                **DESCRIPTOR["write_set"],
                "authorized_supervisors": ["ci-bot"],
            }
        }
        grant = wa.authorize_write(
            self.workspace,
            issue=114,
            path=BINDING["path"],
            op="write",
            issuer="ci-bot",
            issuer_kind="supervisor",
            issued_at=ISSUED,
            descriptor=descriptor,
        )
        self.assertEqual(grant.issuer_kind, "supervisor")
        self.assertEqual(grant.issuer, "ci-bot")
        # the human lane needs no whitelist entry at all
        self.assertEqual(self.issue(issuer_kind="human").issuer_kind, "human")

    def test_a_missing_descriptor_is_refused(self):
        """The descriptor is what `protected_roots` and the supervisor
        whitelist come from, so a grant cannot be issued without one -- the
        same precondition `record_ruling()` places on a human ruling."""
        with self.assertRaises(ProjectDescriptorError) as raised:
            wa.authorize_write(
                self.workspace,
                issue=114,
                path=BINDING["path"],
                op="write",
                issuer="operator",
                descriptor_path=self.workspace / "project-descriptor.json",
            )
        self.assertIn("descriptor", str(raised.exception).lower())
        self.assertFalse(self.ledger.exists())

    def test_a_ledger_inside_a_protected_root_is_refused(self):
        """Otherwise the workspace reaches a state where recording a grant is
        itself a protected write -- told so, rather than handed a capability
        trail whose every append is a boundary breach."""
        descriptor = {
            "write_set": {**DESCRIPTOR["write_set"], "protected_roots": ["ci/**", "scripts/**"]}
        }
        with self.assertRaises(wa.WriteAuthorizationError) as raised:
            wa.authorize_write(
                self.workspace,
                issue=114,
                path=BINDING["path"],
                op="write",
                issuer="operator",
                issued_at=ISSUED,
                descriptor=descriptor,
            )
        self.assertIn("is inside declared protected root", str(raised.exception))
        self.assertFalse(self.ledger.exists())

    def test_a_ledger_outside_the_workspace_is_refused(self):
        with self.assertRaises(wa.WriteAuthorizationError) as raised:
            wa.authorize_write(
                self.workspace,
                issue=114,
                path=BINDING["path"],
                op="write",
                issuer="operator",
                issued_at=ISSUED,
                descriptor=dict(DESCRIPTOR),
                ledger=self.outside / "grants.jsonl",
            )
        self.assertIn("is outside the workspace", str(raised.exception))
        self.assertFalse((self.outside / "grants.jsonl").exists())

    def test_a_damaged_ledger_is_refused_before_anything_is_appended(self):
        """The ledger is the anti-replay record, so a ledger that cannot be
        read cannot rule a replay out -- and a grant recorded anyway would be
        a second copy of an authority nobody can see."""
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.mkdir()
        self.assertRefused("cannot be read")

    def test_a_recorded_grant_survives_a_re_read_of_the_ledger(self):
        """The append is written in the one shape `parse_grant` accepts, so the
        line just written is readable as the grant it is."""
        grant = self.issue(note="sanctioned for the 114 repro")
        entries = wa.read_grant_ledger(self.workspace).entries
        self.assertEqual(entries, [grant])
        self.assertEqual(entries[0].note, "sanctioned for the 114 repro")


if __name__ == "__main__":
    unittest.main()
