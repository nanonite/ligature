#!/usr/bin/env python3
"""Issue-scoped write grants: the sanctioned way to write into a protected
root (chainlink #114).

`protected_roots` is enforced (#103): a file inside one that no declaration
vouches for is a blocking `protected-write` violation. That is the right
default and it left one real dead end, which #113 hit from the policy
document's side and #107 hit from the interaction-spec's side: some
protected-root writes are *sanctioned* -- a recurring edit a work package
genuinely needs, to a path nobody can hand-edit -- and until now the only
supported exit was to widen `allowed_roots` (which deletes the boundary the
grant is supposed to live inside) or to have a human edit the file outside
every tool (which is exactly what the audit trail then cannot distinguish
from an agent's intrusion).

So this module owns the *record* half of the remedy, and
`scripts/write_set.py` owns the *enforcement* half:

  * `ligature authorize-write --issue N --path P --op K [--ttl S]
    [--one-shot] --issuer <name> [--issuer-kind human|supervisor]` appends
    one capability record to `ci/results/protected-writes.jsonl`, and
  * `ligature write-set-check --issue N` (and `status`/`check --issue N`)
    consumes the records that are active for that issue, so a protected
    write covered by one is clean and reports its grant id, while a
    protected write with no matching, unexpired record is still the
    blocking `protected-write` it always was.

**What a grant may never do**, each enforced by recomputation rather than
by trusting the record's own fields (the discipline chainlink #112 applied
to a bridge's `callee_shape`):

  * it cannot widen `allowed_roots` -- `authorize-write` refuses a path no
    declared `protected_roots` pattern covers, and `write_set` consults the
    ledger only on the protected branch, so a grant can never excuse an
    out-of-set write even if a line were appended by hand;
  * it cannot authorize another operation -- `op` is a closed vocabulary
    (`write`, `delete`) and a grant authorizes exactly the one it names;
    only `write` is consumable by the conformance check, because a `delete`
    grant's effect (an absent file) is not something a file walk can see;
  * it cannot outlive its TTL -- `expires_at` is `issued_at + ttl_seconds`,
    `ttl_seconds` is bounded at both ends (positive, and no more than
    `MAX_TTL_SECONDS`), the reader recomputes both, and an expired record
    authorizes nothing (it is reported as `expired` rather than dropped); nor
    can it start early -- a record whose window has not opened yet (`issued_at`
    after the clock the checking run decides against) is reported
    `not-yet-issued` and authorizes nothing, and `authorize-write` refuses to
    record one at all;
  * it cannot authorize another issue or path -- the issue must equal the
    one the checking run names, and the path is bound exactly (or to an
    explicitly bounded pattern);
  * it cannot be edited into something else in place -- `grant_id` is a hash
    of the binding fields, and the reader recomputes it, so a hand-edited
    `path`, `issue`, `op`, `ttl`, `one_shot`, `issuer` or `issued_at` under a
    preserved id is rejected rather than honored.

**The issuer is a named identity, never prose.** `--issuer` is required and
recorded; `--issuer-kind human` is a human checkpoint on the same terms as
`approve`/`accept-policy`/`record-ruling` (the installed skill says an agent
must never run it), and `--issuer-kind supervisor` additionally requires the
identity to appear in the descriptor's explicit
`write_set.authorized_supervisors` whitelist -- so the automation lane is
machine-checkable end to end: `write_set` re-verifies the issuer against the
descriptor every time it consumes a record, and a hand-written line naming a
supervisor nobody declared authorizes nothing. `--note` is recorded as audit
metadata and is deliberately excluded from the grant id: no prose field is
ever read when deciding whether a grant authorizes a write.

**Trust model, stated rather than implied.** The ledger is an audit trail,
not a signature -- the same model `record-ruling`/`human_rulings.jsonl` and
`approve`/`review_log.jsonl` already use, and the same residual they accept:
an entry *appended* by someone with write access to the ledger is
indistinguishable from one `authorize-write` wrote (that access is itself
outside the agent's write set, and the skill forbids the agent from using
it). What this module does add over that model is the recomputation above:
an existing entry cannot be edited into a different authorization in place.
Deletion of a whole line cannot be detected either, and both halves hold the
one rule that keeps a capability trail from becoming its own permission: a
ledger that sits inside a declared protected root is not a capability record.
`authorize-write` refuses to record one there -- a workspace in which
recording a grant would itself be a protected write is told so, rather than
handed a ledger whose every write is a boundary breach -- and the reader
refuses to consume one either, because a writer-side refusal a hand-appended
line walks straight past is not a refusal at all. That is the discipline
this module already states about the TTL ceiling and the recomputed id,
applied to the one precondition that had no reader half: the half that has to
hold is always the half that reads a line somebody else appended.

`authorize_write()` is the capability writer: it appends one `event: grant`
line and writes nothing else. The same ledger may carry narrowly defined
use-audit events from capability consumers, but those rows are never parsed
as grants and never authorize anything.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from project_descriptor import load_project_descriptor  # noqa: E402
from validate_work_package import _pattern_covers_path  # noqa: E402

# The ledger. `ci/results/` is the pipeline's own output location, the same
# place the review log, the human-ruling log and the evidence-promotion log
# live; `write_set._is_canonical` accounts for it as a pipeline location, so
# recording a grant is not itself an out-of-set write. A project whose
# `protected_roots` cover it (a `ci/**` declaration is a perfectly reasonable
# one) is refused the grant rather than handed a ledger whose every append is
# a boundary breach -- `authorize_write()` refuses to record, and
# `GrantLedger.statuses()` refuses to consume, because both halves of that rule
# are load-bearing: a trail that sits inside a declared protected root is not a
# capability record, whether `authorize-write` wrote it or something else did.
GRANT_LEDGER_REL = "ci/results/protected-writes.jsonl"

GRANT_SCHEMA_VERSION = "1.0"

OP_WRITE = "write"
OP_DELETE = "delete"
#: The closed operation vocabulary. `write` covers creating or replacing a
#: file's content -- the conformance check cannot tell those apart from a
#: file walk, and says so in the record's own help text -- and is the only
#: operation `write_set` consumes. `delete` authorizes removing the granted
#: path and is recorded so a sanctioned deletion is auditable; it can never
#: make an existing file clean, because a file's absence is not an
#: observable this check can report on.
WRITE_OPERATIONS = (OP_WRITE, OP_DELETE)

ISSUER_KIND_HUMAN = "human"
ISSUER_KIND_SUPERVISOR = "supervisor"
ISSUER_KINDS = (ISSUER_KIND_HUMAN, ISSUER_KIND_SUPERVISOR)

PATH_KIND_EXACT = "exact"
PATH_KIND_PATTERN = "pattern"

#: The closed status vocabulary `GrantLedger.statuses()` reports -- the
#: machine-readable half of the audit, and the list docs/cli-contract.md §8
#: names. Exactly one applies to a record per run, and they are decided in
#: this order, so the reason a record did not authorize is never ambiguous:
#:
#:   * `ledger-protected` -- the ledger itself sits inside a declared
#:     `protected_roots` pattern. `authorize-write` refuses to record there,
#:     and the reader holds the same rule: a capability trail that lives
#:     inside a protected root is not a capability record, so nothing in it
#:     authorizes anything -- including a record naming the ledger's own
#:     path, which would otherwise let a trail vouch for its own presence in
#:     the protected surface it is supposed to sit outside of. First, because
#:     it is a property of where the record lives, not of what it names:
#:     every other rung would be reporting on an authority that does not
#:     exist.
#:   * `duplicate` -- a repeated `grant_id`; the last line is the live one.
#:   * `no-issue-named` -- the run named no `--issue`, and a grant authorizes
#:     its own issue only.
#:   * `other-issue` -- a record issued for a different issue.
#:   * `not-yet-issued` -- `issued_at` is after the clock this run decides
#:     against, so the window a grant authorizes has not opened yet. The
#:     other end of expiry, and reported rather than assumed away: without it
#:     a future-dated line is "unexpired" by the only relation the reader
#:     recomputes (`now < expires_at`), and a grant nobody has issued yet
#:     would authorize a write. `authorize-write` refuses to record one (a
#:     grant cannot be issued for a time that has not happened) and the reader
#:     holds the same rule, because the half that has to hold is the half that
#:     reads a line somebody else appended. Ahead of `expired` because a
#:     record in this state is not yet, rather than no longer, in its window.
#:   * `expired` -- `now >= expires_at`, recomputed from the binding.
#:   * `unverified-issuer` -- a supervisor the descriptor does not declare.
#:   * `active` -- authorizes.
STATUS_LEDGER_PROTECTED = "ledger-protected"
STATUS_DUPLICATE = "duplicate"
STATUS_NO_ISSUE = "no-issue-named"
STATUS_OTHER_ISSUE = "other-issue"
STATUS_NOT_YET_ISSUED = "not-yet-issued"
STATUS_EXPIRED = "expired"
STATUS_UNVERIFIED_ISSUER = "unverified-issuer"
STATUS_ACTIVE = "active"
GRANT_STATUSES = (
    STATUS_LEDGER_PROTECTED,
    STATUS_DUPLICATE,
    STATUS_NO_ISSUE,
    STATUS_OTHER_ISSUE,
    STATUS_NOT_YET_ISSUED,
    STATUS_EXPIRED,
    STATUS_UNVERIFIED_ISSUER,
    STATUS_ACTIVE,
)

#: A grant is never standing by default, and never standing for long on
#: request: `issued_at + ttl_seconds` is `expires_at`, recomputed on read.
DEFAULT_TTL_SECONDS = 900
MAX_TTL_SECONDS = 86_400

GRANT_ID_PREFIX = "GW-"
_GRANT_ID_HEX_DIGITS = 12

#: The fields a grant's authority is *of*. Every one is recomputed on read
#: (types, vocabulary, the expiry relation, and the id), so none of them is
#: stored-and-trusted. `note` is the one field outside this list, and it is
#: audit metadata only: prose that never authorizes anything must not be able
#: to invalidate a real record either. The record carries nothing else -- no
#: `logged_at`, no separate append timestamp -- because `issued_at` already
#: says when the grant was issued, and a second clock reading would be one
#: more value a hand-written line could disagree with for no gain.
_BINDING_FIELDS = (
    "issue",
    "path",
    "path_kind",
    "op",
    "one_shot",
    "issuer",
    "issuer_kind",
    "issued_at",
    "ttl_seconds",
)
_REQUIRED_FIELDS = _BINDING_FIELDS + ("grant_id", "expires_at")

_GLOB_META = frozenset("*?[]{}")


class WriteAuthorizationError(Exception):
    """A refusal from `authorize-write`, carrying the one line to print.

    Every refusal is about the *request* -- an unbound path, a vacuous
    pattern, an undeclared supervisor, a TTL above the ceiling, a replayed
    grant -- so the CLI answers it with the exit-code contract's
    invalid-input code 2 rather than the blocking-findings code.
    """


# ---------------------------------------------------------------------------
# Path binding
# ---------------------------------------------------------------------------
def _looks_like_pattern(raw: str) -> bool:
    return any(ch in _GLOB_META for ch in raw)


def _pattern_is_unbounded(pattern: str) -> bool:
    """Whether a grant pattern names so much that it is not a capability.

    Two tests, one stricter than the other:

    * the vacuous shape #77 already reports for an `allowed_roots` entry
      (`"**"`, `"*/**"`) -- reused rather than re-derived, through a lazy
      import because `write_set` imports *this* module at module scope and a
      module-level import back would be circular; and
    * one a root declaration does not need and a capability does: at least
      one segment must carry a literal character. `*/*` covers every
      two-segment path, so a grant naming it would be a standing permission
      over the workspace rather than a bounded authorization.
    """
    from write_set import _matches_every_path

    if _matches_every_path(pattern):
        return True
    segments = [s for s in pattern.split("/") if s not in ("", ".")]
    return not any(any(ch not in _GLOB_META for ch in s) for s in segments)


def classify_granted_path(raw: str) -> tuple[str, str]:
    """`raw` as (workspace-relative posix path, `exact`|`pattern`).

    Syntactic, never resolved against the filesystem: a grant is issued
    *before* the write exists, so there is nothing to resolve, and resolving
    would make a grant depend on a symlink an attacker could retarget.
    Refusals are the same escape rules `validate_work_package.
    check_write_set_anchoring` applies to a declared write-set pattern: no
    absolute path, no `..` segment.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise WriteAuthorizationError(
            "--path is required: name the exact file (e.g. ci/manifest/WP-114.json) or an "
            "explicitly bounded pattern (e.g. ci/manifest/WP-114.*.json)"
        )
    candidate = raw.strip()
    if candidate.startswith(("/", "~")):
        raise WriteAuthorizationError(
            f"--path {raw!r} must be workspace-relative: a grant binds a path inside this "
            "workspace, and an absolute path names a file outside it"
        )
    if candidate.endswith("/"):
        raise WriteAuthorizationError(
            f"--path {raw!r} names a directory, and a grant binds a file -- pass "
            f"{candidate}** if you mean every file under it"
        )
    segments = candidate.split("/")
    if any(segment == ".." for segment in segments):
        raise WriteAuthorizationError(
            f"--path {raw!r} escapes the workspace: a '..' segment is refused, not resolved "
            "(the same rule validate-work-package applies to a declared write-set pattern)"
        )
    normalized = "/".join(segment for segment in segments if segment not in ("", "."))
    if not normalized:
        raise WriteAuthorizationError(f"--path {raw!r} names no path")
    if _looks_like_pattern(normalized):
        if _pattern_is_unbounded(normalized):
            raise WriteAuthorizationError(
                f"--path {raw!r} is not a bounded pattern: it names no fixed location (no "
                "literal segment to anchor it, or it matches every path), so it would be a "
                "standing permission over the workspace rather than a capability for one "
                "write. Narrow it to a literal anchor (e.g. 'ci/manifest/WP-114*.json'); a "
                "pattern matching every path ('**', '*/*') is refused"
            )
        return normalized, PATH_KIND_PATTERN
    return normalized, PATH_KIND_EXACT


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------
def _parse_timestamp(value: object) -> datetime | None:
    """An aware `datetime`, or None. A naive timestamp is refused: "expired"
    is not decidable against a clock with no offset."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def grant_id_for(binding: dict) -> str:
    """The grant id of a binding -- a hash of the fields its authority is
    of, so a record cannot be edited into a different authorization while
    keeping its id (the reader recomputes this and rejects a disagreement).

    Deliberately excludes `note`, the only field outside the binding:
    audit metadata that authorizes nothing should not be able to change an id
    either.
    """
    canonical = json.dumps(
        {field: binding[field] for field in _BINDING_FIELDS},
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return GRANT_ID_PREFIX + digest[:_GRANT_ID_HEX_DIGITS]


@dataclass(frozen=True)
class Grant:
    """One recorded capability: write (or delete) at one bounded path, for
    one issue, until one instant, issued by one named identity."""

    grant_id: str
    issue: int
    path: str
    path_kind: str
    op: str
    one_shot: bool
    issuer: str
    issuer_kind: str
    issued_at: str
    ttl_seconds: int
    expires_at: str
    note: str | None
    line: int

    def covers(self, rel: str) -> bool:
        """Whether this grant's path covers the workspace-relative `rel`."""
        if self.path_kind == PATH_KIND_EXACT:
            return rel == self.path
        return _pattern_covers_path(self.path, rel)

    def as_record(self) -> dict:
        """The record as `authorize-write` appends it -- the audit line, and
        the shape `protected-writes.jsonl` is expected to carry."""
        record = {
            "event": "grant",
            "schema_version": GRANT_SCHEMA_VERSION,
            "grant_id": self.grant_id,
            "issue": self.issue,
            "path": self.path,
            "path_kind": self.path_kind,
            "op": self.op,
            "one_shot": self.one_shot,
            "issuer": self.issuer,
            "issuer_kind": self.issuer_kind,
            "issued_at": self.issued_at,
            "ttl_seconds": self.ttl_seconds,
            "expires_at": self.expires_at,
            "note": self.note,
        }
        return record


def authorizing_grant(grants: list[Grant], rel: str, op: str) -> Grant | None:
    """The record authorizing `op` at `rel`, or None.

    The LAST match wins, so re-issuing supersedes an earlier grant in the
    same append-only ledger the way the last human ruling supersedes an
    earlier one -- without either being editable. Takes the already-filtered
    active records so a consumer that has computed them once (as
    `write_set` does, per workspace rather than per file) does not re-derive
    the statuses per file.
    """
    match: Grant | None = None
    for grant in grants:
        if grant.op == op and grant.covers(rel):
            match = grant
    return match


def _binding_of(record: dict) -> dict:
    return {field: record[field] for field in _BINDING_FIELDS if field in record}


def parse_grant(record: object, line: int) -> tuple[Grant | None, str | None]:
    """One ledger line as (grant, None) or (None, why-not).

    Every constraint the record claims is re-derived here: the vocabulary,
    the path's own classification, the boundedness of a pattern, the TTL's
    own bounds, the `expires_at == issued_at + ttl_seconds` relation, and
    the id. A line that fails any of them authorizes nothing and is
    reported, rather than dropped.
    """
    if not isinstance(record, dict):
        return None, "not a JSON object"
    if record.get("event") != "grant":
        return None, f"not a grant record (event={record.get('event')!r})"
    missing = [field for field in _REQUIRED_FIELDS if field not in record]
    if missing:
        return None, f"missing {', '.join(missing)}"
    if record.get("schema_version") != GRANT_SCHEMA_VERSION:
        return None, (
            f"schema_version {record.get('schema_version')!r} is not {GRANT_SCHEMA_VERSION!r}"
        )
    issue = record["issue"]
    if isinstance(issue, bool) or not isinstance(issue, int) or issue < 1:
        return None, f"issue {issue!r} is not a positive integer"
    if record["op"] not in WRITE_OPERATIONS:
        return None, f"op {record['op']!r} is not one of {', '.join(WRITE_OPERATIONS)}"
    if record["issuer_kind"] not in ISSUER_KINDS:
        return None, f"issuer_kind {record['issuer_kind']!r} is not one of {', '.join(ISSUER_KINDS)}"
    if not isinstance(record["issuer"], str) or not record["issuer"].strip():
        return None, "issuer is empty -- a grant with no named issuer is not a grant"
    if not isinstance(record["one_shot"], bool):
        return None, f"one_shot {record['one_shot']!r} is not a boolean"
    ttl = record["ttl_seconds"]
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 1:
        return None, f"ttl_seconds {ttl!r} is not a positive integer"
    # The ceiling is recomputed here, not only in `authorize_write()`: the
    # writer refuses an over-long TTL, but the reader is the half that has to
    # hold when the line was appended by something else, and an unbounded
    # expiry relation alone would let a hand-written record name a year of
    # authority with a self-consistent `expires_at` and be honored for all
    # of it. Same reasoning as the expiry relation below: a grant is never
    # standing by default, and never standing for longer than a day.
    if ttl > MAX_TTL_SECONDS:
        return None, (
            f"ttl_seconds {ttl} exceeds the {MAX_TTL_SECONDS}s ceiling: a write grant is issued "
            "for the work at hand, not as a standing permission, so a record naming more time "
            "than any issuer could have granted authorizes nothing"
        )
    if record.get("note") is not None and not isinstance(record["note"], str):
        return None, "note is not a string"
    try:
        normalized, kind = classify_granted_path(record["path"])
    except WriteAuthorizationError as exc:
        return None, str(exc)
    if normalized != record["path"]:
        return None, f"path {record['path']!r} is not normalized (expected {normalized!r})"
    if record["path_kind"] != kind:
        return None, (
            f"path_kind {record['path_kind']!r} disagrees with the path itself "
            f"({kind!r} -- a grant cannot declare a pattern to be an exact file)"
        )
    if kind == PATH_KIND_PATTERN and record["one_shot"]:
        return None, (
            "one_shot with path_kind 'pattern' -- a one-shot grant binds one exact file, "
            "because a pattern grant authorizes as many writes as its TTL allows"
        )
    issued = _parse_timestamp(record["issued_at"])
    if issued is None:
        return None, f"issued_at {record['issued_at']!r} is not an ISO-8601 timestamp with a UTC offset"
    expires = _parse_timestamp(record["expires_at"])
    if expires is None:
        return None, f"expires_at {record['expires_at']!r} is not an ISO-8601 timestamp with a UTC offset"
    if expires != issued + timedelta(seconds=ttl):
        return None, (
            f"expires_at {record['expires_at']!r} is not issued_at + ttl_seconds "
            f"({ttl}) -- a grant cannot outlive the TTL it names"
        )
    recomputed = grant_id_for(_binding_of(record))
    if record["grant_id"] != recomputed:
        return None, (
            f"grant_id {record['grant_id']!r} does not match the binding it names "
            f"(recomputed {recomputed!r} -- an entry edited in place authorizes nothing)"
        )
    return (
        Grant(
            grant_id=recomputed,
            issue=issue,
            path=normalized,
            path_kind=kind,
            op=record["op"],
            one_shot=record["one_shot"],
            issuer=record["issuer"],
            issuer_kind=record["issuer_kind"],
            issued_at=record["issued_at"],
            ttl_seconds=ttl,
            expires_at=record["expires_at"],
            note=record.get("note"),
            line=line,
        ),
        None,
    )


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RejectedGrant:
    """A ledger line that claims to be a grant and cannot be used as one."""

    line: int
    reason: str

    def as_dict(self) -> dict:
        return {"line": self.line, "reason": self.reason}


@dataclass
class GrantLedger:
    """The parsed ledger: usable records, the lines that are not usable (with
    why), and whether it could be read at all.

    An unreadable ledger yields no grants at all -- the same fail-closed
    reading `ruling_gaps()` gives a damaged human-ruling log, for the same
    reason: a trail that cannot be read proves no authorization.
    """

    path: Path | None = None
    entries: list[Grant] = field(default_factory=list)
    rejected: list[RejectedGrant] = field(default_factory=list)
    ignored: int = 0
    error: str | None = None

    @property
    def state(self) -> str:
        if self.error is not None:
            return "unreadable"
        if self.path is None:
            return "absent"
        return "readable"

    # -- status -------------------------------------------------------------
    # Fixed order, so one entry has exactly one reported reason: an entry in a
    # ledger that sits inside a declared protected root is not an authorization
    # at all whatever else is true of it, a replayed entry is a duplicate
    # whatever else is true of it, an entry for another issue is not this run's
    # business, an entry whose window has not opened yet is not authority yet,
    # an expired entry is expired whether or not it names this issue, and an
    # unverified issuer is last because it is the property only the
    # descriptor's whitelist can settle.
    @staticmethod
    def _status(
        grant: Grant,
        last_line: dict[str, int],
        issue: int | None,
        supervisors: frozenset[str],
        now: datetime,
        ledger_protected: str | None = None,
    ) -> str:
        if ledger_protected is not None:
            return STATUS_LEDGER_PROTECTED
        if last_line[grant.grant_id] != grant.line:
            return STATUS_DUPLICATE
        if issue is None:
            return STATUS_NO_ISSUE
        if grant.issue != issue:
            return STATUS_OTHER_ISSUE
        issued = _parse_timestamp(grant.issued_at)
        if issued is not None and now < issued:
            return STATUS_NOT_YET_ISSUED
        expires = _parse_timestamp(grant.expires_at)
        if expires is not None and now >= expires:
            return STATUS_EXPIRED
        if grant.issuer_kind == ISSUER_KIND_SUPERVISOR and grant.issuer not in supervisors:
            return STATUS_UNVERIFIED_ISSUER
        return STATUS_ACTIVE

    def statuses(
        self,
        *,
        issue: int | None,
        supervisors: frozenset[str] = frozenset(),
        now: datetime | None = None,
        ledger_protected: str | None = None,
    ) -> list[tuple[Grant, str]]:
        """Every usable record with its status, in ledger order -- the audit
        view, so a report can say why a grant did not authorize as well as
        which one did.

        `ledger_protected` is the declared `protected_roots` pattern covering
        this ledger (see `ledger_protected_by`), or None. When it is set the
        reader holds the writer's own refusal -- a trail inside a protected
        root is not a capability record -- so every record is reported
        `ledger-protected` and none of them authorizes anything.
        """
        moment = now or datetime.now(timezone.utc)
        last_line: dict[str, int] = {}
        for grant in self.entries:
            last_line[grant.grant_id] = max(last_line.get(grant.grant_id, grant.line), grant.line)
        return [
            (grant, self._status(grant, last_line, issue, supervisors, moment, ledger_protected))
            for grant in self.entries
        ]

    def active_grants(
        self,
        *,
        issue: int | None,
        supervisors: frozenset[str] = frozenset(),
        now: datetime | None = None,
        ledger_protected: str | None = None,
    ) -> list[Grant]:
        """The records that authorize something right now, in ledger order."""
        return [
            grant
            for grant, status in self.statuses(
                issue=issue,
                supervisors=supervisors,
                now=now,
                ledger_protected=ledger_protected,
            )
            if status == STATUS_ACTIVE
        ]

    def authorizing(
        self,
        rel: str,
        op: str,
        *,
        issue: int | None,
        supervisors: frozenset[str] = frozenset(),
        now: datetime | None = None,
        ledger_protected: str | None = None,
    ) -> Grant | None:
        """The record authorizing `op` at `rel`, or None."""
        return authorizing_grant(
            self.active_grants(
                issue=issue,
                supervisors=supervisors,
                now=now,
                ledger_protected=ledger_protected,
            ),
            rel,
            op,
        )


def grant_ledger_path(workspace_root: Path) -> Path:
    """Where grants live. Workspace-scoped from the start, the
    cwd-relative lesson chainlink #45 taught `accept_promotion()`'s audit
    log rather than re-learning it here."""
    return workspace_root / GRANT_LEDGER_REL


def protecting_pattern(protected_roots: list[str], rel: str) -> str | None:
    """The first declared `protected_roots` pattern covering `rel`, or None.

    The declared-pattern question in one place, because #114 needs it from
    both ends -- `authorize_write()` asks whether the path it is about to bind
    is inside a protected root, and `write_set` asks the same of the ledger
    itself. Two hand-rolled loops would be free to disagree about which
    pattern matched, and the disagreement would only ever show up as one half
    of the boundary quietly not applying.
    """
    return next(
        (pattern for pattern in protected_roots if _pattern_covers_path(pattern, rel)),
        None,
    )


def ledger_protected_by(protected_roots: list[str], ledger_rel: str) -> str | None:
    """The declared `protected_roots` pattern covering the grant ledger, or
    None.

    Both halves of one rule, and both are load-bearing. The writer's half is
    `authorize_write()`'s refusal: a workspace whose protected roots cover
    `ci/results/` cannot record a grant, because every append to its own
    capability trail would be a boundary breach. The reader's half is
    `GrantLedger.statuses()`, which reports every record in such a ledger as
    `ledger-protected` rather than honoring it -- without it the writer's
    refusal is defeatable by hand: a line appended to a ledger that sits
    inside a protected root would authorize the writes it names, and a grant
    naming the ledger's own path would make that trail vouch for its own
    presence in the very protected surface it must stay outside of.
    """
    return protecting_pattern(protected_roots, ledger_rel)


def read_grant_ledger(workspace_root: Path, ledger: Path | None = None) -> GrantLedger:
    """Parse the ledger, or report why it cannot be read.

    A missing ledger is the ordinary state of a workspace that has issued no
    grant (`absent`), not an error. A line that is valid JSON but is not a
    grant record is counted in `ignored` rather than rejected -- the ledger
    is a trail of events, and a future event kind must not be read as a
    grant -- while a line that *claims* to be a grant and cannot be used as
    one is rejected with its reason, because that is a damaged authorization
    record and dropping it silently would read as "no grant here".
    """
    path = grant_ledger_path(workspace_root) if ledger is None else ledger
    result = GrantLedger()
    try:
        text = path.read_text()
    except FileNotFoundError:
        return result
    except (OSError, UnicodeDecodeError) as exc:
        result.path = path
        result.error = f"{path} cannot be read, so no write grant can be proven: {exc}"
        return result

    result.path = path
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            result.rejected.append(
                RejectedGrant(lineno, "not valid JSON, so the line cannot be an authorization record")
            )
            continue
        if not isinstance(record, dict) or record.get("event") != "grant":
            result.ignored += 1
            continue
        grant, reason = parse_grant(record, lineno)
        if grant is None:
            result.rejected.append(RejectedGrant(lineno, reason or "unusable grant record"))
            continue
        result.entries.append(grant)
    return result


def supervisor_authorities(descriptor: dict | None) -> frozenset[str]:
    """The identities the descriptor declares able to issue a grant in the
    supervisor lane -- `write_set.authorized_supervisors`. Absent means none,
    so a workspace that declares no supervisor cannot record one at all."""
    if not isinstance(descriptor, dict):
        return frozenset()
    write_set = descriptor.get("write_set")
    if not isinstance(write_set, dict):
        return frozenset()
    declared = write_set.get("authorized_supervisors")
    if not isinstance(declared, list):
        return frozenset()
    return frozenset(name for name in declared if isinstance(name, str) and name)


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------
def authorize_write(
    workspace_root: Path,
    *,
    issue: int,
    path: str,
    op: str,
    issuer: str,
    issuer_kind: str = ISSUER_KIND_HUMAN,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    one_shot: bool = False,
    note: str | None = None,
    issued_at: str | None = None,
    descriptor: dict | None = None,
    descriptor_path: Path | None = None,
    ledger: Path | None = None,
) -> Grant:
    """Record one capability and return it.

    Fail-closed on every precondition, because each of these is the thing
    that would otherwise turn a capability record into a permission:

    * the descriptor must load -- its `protected_roots` are what "a
      protected-root write" means here, and its
      `authorized_supervisors` is the whitelist the supervisor lane is
      checked against (the same reason `record_ruling()` requires a
      descriptor);
    * a supervisor issuer must be a declared supervisor;
    * the path must be workspace-relative, bounded, and covered by a declared
      `protected_roots` pattern -- otherwise the grant would be authorizing
      something no root protects, which is the widening case;
    * the path must NOT already be under an `allowed_roots` pattern: such a
      write is clean without any grant, and a record that exists only to
      name a permitted path is a permission-shaped object with no work to do;
    * `--one-shot` binds one exact file, never a pattern;
    * `ttl_seconds` is a positive integer no larger than
      `MAX_TTL_SECONDS` -- a grant is never standing by default and never
      standing for longer than a day on request;
    * an explicit `issued_at` must not be in the future -- the window a grant
      authorizes has to have opened before the record means anything, and the
      reader reports one that has not as `not-yet-issued` rather than
      honoring it;
    * the ledger must not itself sit inside a declared protected root;
    * an identical grant (`grant_id` is a hash of the binding, so an exact
      re-run with the same `--issued-at` produces the same id) must not
      already be recorded -- a replay records nothing, which is what makes a
      retry of the issuing command idempotent instead of a way to stack
      grants.

    Appends exactly one line to the ledger (`ci/results/protected-writes.jsonl`
    by default) and writes nothing else. A refusal writes nothing at all.
    """
    if isinstance(issue, bool) or not isinstance(issue, int) or issue < 1:
        raise WriteAuthorizationError(
            f"--issue {issue!r} is not a positive integer: a grant is scoped to one issue, "
            "because that is the unit a human authorizes work in"
        )
    if op not in WRITE_OPERATIONS:
        raise WriteAuthorizationError(
            f"--op {op!r} must be one of: {', '.join(WRITE_OPERATIONS)}"
        )
    if issuer_kind not in ISSUER_KINDS:
        raise WriteAuthorizationError(
            f"--issuer-kind {issuer_kind!r} must be one of: {', '.join(ISSUER_KINDS)}"
        )
    if not isinstance(issuer, str) or not issuer.strip():
        raise WriteAuthorizationError(
            "--issuer is required: a grant with no named issuer is not a capability record, and "
            "prose about who approved it is not an issuer"
        )
    issuer = issuer.strip()
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds < 1:
        raise WriteAuthorizationError(
            f"--ttl {ttl_seconds!r} must be a positive number of seconds"
        )
    if ttl_seconds > MAX_TTL_SECONDS:
        raise WriteAuthorizationError(
            f"--ttl {ttl_seconds}s exceeds the {MAX_TTL_SECONDS}s ceiling: a write grant is "
            "issued for the work at hand, not for a standing permission -- re-issue it if the "
            "work is still going"
        )

    normalized, path_kind = classify_granted_path(path)
    if path_kind == PATH_KIND_PATTERN and one_shot:
        raise WriteAuthorizationError(
            f"--one-shot with the pattern {normalized!r}: a one-shot grant binds one exact file "
            f"({normalized.rstrip('*')}...) because a pattern authorizes as many writes as its "
            "TTL allows"
        )

    if descriptor is None:
        if descriptor_path is None:
            descriptor_path = workspace_root / "project-descriptor.json"
        descriptor = load_project_descriptor(descriptor_path)

    write_set = descriptor.get("write_set") if isinstance(descriptor, dict) else None
    write_set = write_set if isinstance(write_set, dict) else {}
    protected_roots = [p for p in (write_set.get("protected_roots") or []) if isinstance(p, str)]
    allowed_roots = [p for p in (write_set.get("allowed_roots") or []) if isinstance(p, str)]
    if not protected_roots:
        raise WriteAuthorizationError(
            "this project descriptor declares no protected_roots, so there is no protected-root "
            "write to authorize: a grant cannot widen allowed_roots"
        )

    if issuer_kind == ISSUER_KIND_SUPERVISOR and issuer not in supervisor_authorities(descriptor):
        raise WriteAuthorizationError(
            f"--issuer {issuer!r} is not a declared supervisor: the supervisor lane is an "
            "explicit machine-checkable whitelist, so list the identity in project-descriptor.json's "
            "write_set.authorized_supervisors (or have a human issue the grant with "
            "--issuer-kind human). No prose permission is accepted in its place"
        )

    # A path `allowed_roots` already permits is refused FIRST, and with the
    # more specific reason: such a write is clean with no grant at all, so a
    # record naming it is a permission-shaped object with no work to do --
    # and the "it cannot widen allowed_roots" half of the guarantee is worth
    # stating in that case rather than the vaguer "no protected root covers
    # it" one.
    allowed_overlap = protecting_pattern(allowed_roots, normalized)
    if allowed_overlap is not None:
        raise WriteAuthorizationError(
            f"--path {normalized!r} is already under allowed_roots pattern {allowed_overlap!r}, so a "
            "write there is clean without any grant: record a grant only for a protected write that "
            "would otherwise be reported as protected-write"
        )
    if protecting_pattern(protected_roots, normalized) is None:
        raise WriteAuthorizationError(
            f"--path {normalized!r} names nothing any declared protected_roots pattern covers: a "
            "grant authorizes a sanctioned protected-root write and nothing else. It cannot widen "
            "allowed_roots and it cannot excuse a write that no root protects"
        )

    ledger_path = grant_ledger_path(workspace_root) if ledger is None else ledger
    try:
        ledger_rel = ledger_path.resolve().relative_to(workspace_root.resolve()).as_posix()
    except (ValueError, OSError) as exc:
        raise WriteAuthorizationError(
            f"the grant ledger {ledger_path} is outside the workspace, so a grant recorded in it "
            "could not be consumed by this workspace's own write-set check"
        ) from exc
    covering_ledger = ledger_protected_by(protected_roots, ledger_rel)
    if covering_ledger is not None:
        raise WriteAuthorizationError(
            f"the grant ledger {ledger_rel} is inside declared protected root {covering_ledger!r}, "
            "so recording a grant would itself be a protected write: a workspace whose protected "
            "roots cover ci/results/ cannot record write grants, and the check is right to report "
            "that ledger as one. A ledger in that position is refused by the reader for the same "
            "reason -- a capability trail that lives inside a protected root is not a capability "
            "record, whoever appended its lines"
        )

    now = datetime.now(timezone.utc)
    if issued_at is None:
        moment = now
    else:
        parsed = _parse_timestamp(issued_at)
        if parsed is None:
            raise WriteAuthorizationError(
                f"--issued-at {issued_at!r} is not an ISO-8601 timestamp with a UTC offset "
                "(e.g. 2026-10-03T12:00:00+00:00)"
            )
        moment = parsed.astimezone(timezone.utc)
        # `--issued-at` exists so a re-run is idempotent and an expiring grant
        # is reproducible without waiting; a timestamp in the future serves
        # neither, and would record a window that has not opened yet -- which
        # the reader reports as `not-yet-issued` and honors as nothing, because
        # a grant nobody has issued yet cannot authorize a write. One clock
        # reading, taken once and used for both halves of that decision, so
        # the refusal cannot disagree with the status it exists to prevent.
        if moment > now:
            raise WriteAuthorizationError(
                f"--issued-at {issued_at!r} is in the future (now {now.replace(microsecond=0).isoformat()}): "
                "a grant cannot be issued for a time that has not happened, and a window that has not "
                "opened yet authorizes nothing. Re-run without --issued-at to record it now"
            )
    issued_text = moment.replace(microsecond=0).isoformat()
    expires_text = (moment + timedelta(seconds=ttl_seconds)).replace(microsecond=0).isoformat()

    binding = {
        "issue": issue,
        "path": normalized,
        "path_kind": path_kind,
        "op": op,
        "one_shot": bool(one_shot),
        "issuer": issuer,
        "issuer_kind": issuer_kind,
        "issued_at": issued_text,
        "ttl_seconds": ttl_seconds,
    }
    record = dict(binding)
    record["event"] = "grant"
    record["schema_version"] = GRANT_SCHEMA_VERSION
    record["grant_id"] = grant_id_for(binding)
    record["expires_at"] = expires_text
    record["note"] = note

    existing = read_grant_ledger(workspace_root, ledger=ledger_path)
    if existing.error is not None:
        raise WriteAuthorizationError(
            f"{existing.error}; fix or remove the damaged ledger before recording a grant"
        )
    replayed = next(
        (grant for grant in existing.entries if grant.grant_id == record["grant_id"]),
        None,
    )
    if replayed is not None:
        raise WriteAuthorizationError(
            f"grant {record['grant_id']} is already recorded at line {replayed.line} "
            f"(issued at {replayed.issued_at} by {replayed.issuer!r} for issue {replayed.issue}) -- "
            "a grant is recorded once, and an identical re-issue is a replay: it records nothing"
        )

    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    line_number = sum(1 for _ in ledger_path.read_text().splitlines()) + 1 if ledger_path.exists() else 1
    grant, reason = parse_grant(record, line_number)
    if grant is None:  # pragma: no cover -- the record was just built here
        raise WriteAuthorizationError(f"the grant record just written is not usable: {reason}")
    # The appended line comes from Grant.as_record(), the one definition of the
    # record's shape -- not from the dict assembled above -- so the audit line
    # and the `--json` form cannot drift apart.
    with ledger_path.open("a") as stream:
        stream.write(json.dumps(grant.as_record(), sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return grant
