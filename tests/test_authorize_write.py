"""Issue-scoped write grants for sanctioned protected-root writes (chainlink #114).

Enforcement with no exit is enforcement that pushes work out of the tool:
#103 made `protected_roots` blocking, and a protected-root write that is
genuinely required then had only two routes -- widen `allowed_roots`, which
deletes the boundary, or have somebody hand-edit a file, which is exactly
what `write-set-check` cannot tell apart from an intrusion. #114 adds the
third: a tool-mediated, issue-scoped capability record
(`ligature authorize-write`, recorded in `ci/results/protected-writes.jsonl`)
that authorizes one bounded write without widening anything.

These tests are black-box: every case runs the real CLI through
`pipeline.main()` against a real `ligature init` workspace, because the
acceptance criteria are about what the commands *report*, not about a
helper's return value. What they pin, in the order the issue states it:

  * ordinary allowed-root writes stay clean, and a grant never touches them;
  * a protected write with an active exact grant is clean and reports the
    grant id and audit status in the machine-readable output;
  * a protected write without a matching, unexpired grant is still the
    distinct blocking `protected-write` it always was;
  * a grant cannot widen `allowed_roots`, authorize another operation,
    outlive its TTL, or cover another issue or path;
  * a trail that sits inside a declared protected root is not a capability
    record -- neither to write into nor to consume -- so it cannot authorize
    the writes it names, or vouch for its own presence there;
  * the audit view itself is reported whichever way the ledger turned out
    (readable, absent, unreadable, refused) and is deterministic, so a
    consumer diffing two runs sees no churn;
  * expiry and replay are deterministic -- no sleeping, no clock races;
  * the issuer is a named identity, a supervisor only through the
    descriptor's explicit whitelist, and prose is never authority.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import pipeline  # noqa: E402
import write_authorization  # noqa: E402
from schema_utils import make_validator  # noqa: E402

CONSOLIDATED_CHECK_SCHEMA = json.loads(
    (ROOT / "schemas" / "consolidated-check.schema.json").read_text()
)
PROJECT_STATE_SCHEMA = json.loads((ROOT / "schemas" / "project-state.schema.json").read_text())
DESCRIPTOR_SCHEMA = json.loads((ROOT / "schemas" / "project-descriptor.schema.json").read_text())
GRANT_SCHEMA = json.loads((ROOT / "docs" / "write-grant-schema.json").read_text())

# The date-creusot pilot's filled write_set, verbatim from the report.
PILOT_WRITE_SET = {
    "allowed_roots": ["rust/*/src/", "rust/*/tests/"],
    "protected_roots": [
        "rust/*/specs/**",
        "ci/manifest/**",
        "scripts/**",
        "docs/*-schema.json",
        "docs/reliance-policy.md",
        "**/Cargo.toml",
        "**/build.rs",
        "tests/harnesses/**",
        ".github/**",
        "rust-toolchain.toml",
    ],
}

LEDGER_REL = "ci/results/protected-writes.jsonl"
# A fixed date far in the past, so a grant issued at it with any sane TTL is
# expired by arithmetic rather than by sleeping (the test suite never waits).
LONG_AGO = "2020-01-01T00:00:00+00:00"
AN_HOUR_AGO = "2020-01-01T23:00:00+00:00"


class GrantFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name).resolve()
        self.descriptor_path = self.workspace / "project-descriptor.json"

    def tearDown(self):
        self._tmp.cleanup()

    # -- workspace ----------------------------------------------------------
    def init_workspace(self, mode="port", name="date-creusot"):
        """A real `ligature init` (which pins the gate hashes in a manifest),
        the policy marker stamped as chainlink #113 requires, and the pilot's
        filled write_set -- the same fixture shape tests/test_write_set.py
        uses, so a protected write here is the protected write #103 shipped."""
        code, _, _ = self.run_cli("init", "--mode", mode, "--name", name)
        self.assertEqual(code, 0)
        code, _, err = self.run_cli(
            "accept-policy", "--reviewer", "pilot", "--version", "reliance-policy@1.0"
        )
        self.assertEqual(code, 0, err)
        descriptor = json.loads(self.descriptor_path.read_text())
        descriptor["crates"] = [
            {"crate_dir": "rust/date-creusot-core", "contracts_crate": "contracts", "specs_search_root": "rust"}
        ]
        descriptor["review"]["reviewer"] = "pilot-reviewer"
        descriptor["write_set"] = json.loads(json.dumps(PILOT_WRITE_SET))
        if mode == "port":
            descriptor["port_source"]["repository"] = "upstream"
        self.write_descriptor(descriptor)
        return descriptor

    def write_descriptor(self, descriptor: dict) -> None:
        self.descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n")

    def edit_descriptor(self, mutate) -> None:
        descriptor = json.loads(self.descriptor_path.read_text())
        mutate(descriptor)
        self.write_descriptor(descriptor)

    def declare_supervisor(self, *names: str) -> None:
        self.edit_descriptor(
            lambda d: d["write_set"].__setitem__("authorized_supervisors", list(names))
        )

    def protect_results(self) -> None:
        """Declare `ci/results/**` protected -- the workspace `authorize-write`
        refuses to record a grant in, and (chainlink #114) one whose ledger the
        consumer refuses to read."""
        self.edit_descriptor(
            lambda d: d["write_set"]["protected_roots"].append("ci/results/**")
        )

    def write(self, relative: str, data: str = "rogue\n") -> Path:
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
        return path

    # -- commands -----------------------------------------------------------
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = pipeline.main(
                ["--workspace", str(self.workspace), "--descriptor", str(self.descriptor_path), *args]
            )
        return code, out.getvalue(), err.getvalue()

    def write_set_check(self, *args) -> tuple[int, dict]:
        code, out, _ = self.run_cli("write-set-check", "--json", *args)
        return code, json.loads(out)

    def status(self, *args) -> tuple[int, dict]:
        code, out, _ = self.run_cli("status", "--json", *args)
        return code, json.loads(out)

    def check(self, *args) -> tuple[int, dict]:
        code, out, _ = self.run_cli("check", "--json", *args)
        return code, json.loads(out)

    def authorize(self, *args) -> tuple[int, str, str]:
        return self.run_cli("authorize-write", *args)

    def granted(self, *args):
        """Authorize and assert it was recorded; returns the grant id."""
        code, out, err = self.authorize(*args)
        self.assertEqual(code, 0, err)
        for line in out.splitlines():
            if line.startswith("grant: "):
                return line.split(" ", 1)[1]
        self.fail(f"no grant id in authorize-write output:\n{out}\n{err}")

    # -- ledger -------------------------------------------------------------
    @property
    def ledger(self) -> Path:
        return self.workspace / LEDGER_REL

    def ledger_records(self) -> list[dict]:
        return [json.loads(line) for line in self.ledger.read_text().splitlines() if line.strip()]

    def append_ledger(self, record: dict) -> None:
        """A hand-written ledger line -- how a test reaches a state
        `authorize-write` refuses to produce (an expired grant, an edited
        record, a damaged line) without faking a clock."""
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger.open("a") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")

    def append_ledger_raw(self, line: str) -> None:
        """One raw line, for the states `json.dumps` cannot express: text that
        is not JSON at all, or a valid JSON object that is not a grant."""
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger.open("a") as stream:
            stream.write(line + "\n")

    def assertValid(self, schema, document, validator_name="document"):
        validator = make_validator(schema)
        errors = list(validator.iter_errors(document))
        self.assertEqual(errors, [], f"{validator_name} invalid: {[e.message for e in errors]}")

    def _valid_record(self, **overrides) -> dict:
        """A well-formed grant record for this workspace, id recomputed --
        how a test reaches a state `authorize-write` refuses to produce (an
        expired grant, an edited record, a damaged line) without faking a
        clock."""
        record = {
            "event": "grant",
            "schema_version": "1.0",
            "grant_id": "GW-000000000000",
            "issue": 114,
            "path": "ci/manifest/WP-114.json",
            "path_kind": "exact",
            "op": "write",
            "one_shot": False,
            "issuer": "operator",
            "issuer_kind": "human",
            "issued_at": AN_HOUR_AGO,
            "ttl_seconds": 3600,
            "expires_at": "2020-01-02T00:00:00+00:00",
            "note": None,
        }
        record.update(overrides)
        record["grant_id"] = write_authorization.grant_id_for(
            {field: record[field] for field in write_authorization._BINDING_FIELDS}
        )
        return record

    def _unexpired_record(self, **overrides) -> dict:
        """A well-formed grant record that is live for the next hour.

        `_valid_record` is deliberately already expired (its timestamps are
        fixed constants, so no test waits on a clock); this is the counterpart
        for a test whose point is that a grant *does* authorize. The window is
        an hour wide and derived from the run, so it cannot expire mid-test.
        """
        issued = (datetime.now(timezone.utc) - timedelta(seconds=60)).replace(microsecond=0)
        return self._valid_record(
            issued_at=issued.isoformat(),
            expires_at=(issued + timedelta(seconds=3600)).isoformat(),
            **overrides,
        )

    def _future_record(self, **overrides) -> dict:
        """A well-formed record whose window has not opened yet: structurally
        valid and self-consistent (`expires_at == issued_at + ttl_seconds`,
        id recomputed), so the only thing standing between it and an
        authorization is the instant it names -- which is exactly the state
        `authorize-write` refuses to produce and the reader must report."""
        issued = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0)
        return self._valid_record(
            issued_at=issued.isoformat(),
            expires_at=(issued + timedelta(seconds=3600)).isoformat(),
            **overrides,
        )


class AuthorizedWriteIsCleanTest(GrantFixture):
    """The positive case: a protected write the issue is authorized to make
    is clean, and the machine-readable output names the grant that made it
    clean. Reported rather than excused -- a `clean` verdict must never read
    as "nothing was written into that protected root"."""

    def test_a_protected_write_with_an_active_grant_is_clean(self):
        self.init_workspace()
        self.write("ci/manifest/WP-114.json", "{}")
        # without the grant, the issue's own repro: blocking
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 1)
        self.assertEqual(
            [v["path"] for v in doc["write_set"]["violations"]], ["ci/manifest/WP-114.json"]
        )
        grant_id = self.granted(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator", "--ttl", "3600",
        )
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 0)
        self.assertEqual(doc["write_set"]["state"], "clean")
        self.assertEqual(doc["write_set"]["violations"], [])
        self.assertFalse(doc["mutated_workspace"])
        authorized = doc["write_set"]["authorized_writes"]
        self.assertEqual(len(authorized), 1)
        entry = authorized[0]
        self.assertEqual(entry["path"], "ci/manifest/WP-114.json")
        self.assertEqual(entry["grant_id"], grant_id)
        self.assertEqual(entry["issue"], 114)
        self.assertEqual(entry["op"], "write")
        self.assertEqual(entry["issuer"], "operator")
        self.assertEqual(entry["issuer_kind"], "human")
        self.assertFalse(entry["one_shot"])
        self.assertEqual(entry["status"], "active")
        self.assertIn(grant_id, doc["write_set"]["details"])
        self.assertIn("for issue 114", doc["write_set"]["details"])

    def test_the_grant_is_reported_in_the_per_pattern_protected_surface(self):
        self.init_workspace()
        self.write("ci/manifest/WP-114.json", "{}")
        self.granted(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator",
        )
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 0)
        surface = {e["pattern"]: e for e in doc["write_set"]["protected_surface"]}
        entry = surface["ci/manifest/**"]
        # the granted write, plus ci/manifest/installation.json, which the
        # ownership manifest vouches for itself
        self.assertEqual((entry["files"], entry["violations"], entry["authorized"]), (2, 0, 1))

    def test_the_human_readable_report_names_the_grant(self):
        self.init_workspace()
        self.write("rust/date-creusot-core/specs/_boundaries/PROBE.json", "{}")
        grant_id = self.granted(
            "--issue", "114", "--path", "rust/date-creusot-core/specs/_boundaries/PROBE.json",
            "--op", "write", "--issuer", "operator",
        )
        code, out, _ = self.run_cli("write-set-check", "--issue", "114")
        self.assertEqual(code, 0)
        self.assertIn(f"authorized protected write [grant {grant_id}]", out)
        self.assertIn("PROBE.json", out)
        self.assertIn("1 authorized by a write grant", out)
        self.assertIn("issue: 114", out)

    def test_status_and_check_agree_with_write_set_check(self):
        """Four surfaces read the write set, so they cannot disagree about it:
        a grant consumed by one must be consumed by all three."""
        self.init_workspace()
        self.write("ci/manifest/WP-114.json", "{}")
        self.granted(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator",
        )
        self.assertEqual(self.write_set_check("--issue", "114")[0], 0)
        code, state = self.status("--issue", "114")
        self.assertEqual(code, 0)
        self.assertValid(PROJECT_STATE_SCHEMA, state, "project-state")
        self.assertEqual(state["write_set"]["state"], "clean")
        code, consolidated = self.check("--issue", "114")
        self.assertEqual(code, 0)
        self.assertValid(CONSOLIDATED_CHECK_SCHEMA, consolidated, "consolidated-check")
        self.assertEqual([f for f in consolidated["findings"] if f["gate_id"] == "write-set"], [])

    def test_an_ordinary_allowed_root_write_stays_clean_and_needs_no_grant(self):
        self.init_workspace()
        self.write("rust/date-creusot-core/src/lib.rs", "pub fn ok() {}\n")
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 0)
        self.assertEqual(doc["write_set"]["authorized_writes"], [])
        # and a grant for such a path is refused rather than recorded
        code, _, err = self.authorize(
            "--issue", "114", "--path", "rust/date-creusot-core/src/lib.rs",
            "--op", "write", "--issuer", "operator",
        )
        self.assertEqual(code, 2)
        self.assertIn("already under allowed_roots", err)

    def test_a_bounded_pattern_grant_covers_the_files_it_names(self):
        self.init_workspace()
        self.write("ci/manifest/WP-114-a.json", "{}")
        self.write("ci/manifest/WP-115-a.json", "{}")
        self.granted(
            "--issue", "114", "--path", "ci/manifest/WP-114*.json", "--op", "write",
            "--issuer", "operator",
        )
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 1)
        self.assertEqual(
            [v["path"] for v in doc["write_set"]["violations"]], ["ci/manifest/WP-115-a.json"]
        )
        self.assertEqual(
            [e["path"] for e in doc["write_set"]["authorized_writes"]], ["ci/manifest/WP-114-a.json"]
        )

    def test_a_grant_attributes_a_file_in_the_undecidable_half(self):
        """A sanctioned write into a location the pipeline writes nothing
        into (`scripts/`) leaves the non-blocking audit rather than sitting in
        it: a grant answers the same question in both protected halves --
        who vouches for this file -- and the report says which grant."""
        self.init_workspace()
        self.write("scripts/evil.py", "echo rogue\n")
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 0)
        self.assertEqual(doc["write_set"]["protected_unvouched"], ["scripts/evil.py"])
        self.granted(
            "--issue", "114", "--path", "scripts/evil.py", "--op", "write", "--issuer", "operator"
        )
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 0)
        self.assertEqual(doc["write_set"]["protected_unvouched"], [])
        self.assertEqual(
            [e["path"] for e in doc["write_set"]["authorized_writes"]], ["scripts/evil.py"]
        )
        surface = {e["pattern"]: e for e in doc["write_set"]["protected_surface"]}
        self.assertEqual(surface["scripts/**"]["authorized"], 1)
        self.assertEqual(surface["scripts/**"]["unattributed"], 0)


class UngrantedProtectedWriteTest(GrantFixture):
    """The negative case the whole feature must not soften: a protected write
    with no matching, unexpired grant is the same blocking violation #103
    shipped."""

    def test_no_grant_at_all_is_a_blocking_protected_write(self):
        self.init_workspace()
        self.write("ci/manifest/PROBE.json", "{}")
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 1)
        self.assertEqual(
            [(v["path"], v["kind"]) for v in doc["write_set"]["violations"]],
            [("ci/manifest/PROBE.json", "protected-write")],
        )
        self.assertIn("no active write grant", doc["write_set"]["violations"][0]["reason"])

    def test_a_run_naming_no_issue_consumes_no_grant(self):
        """The fail-closed default: the check must be told which issue it is
        about, because a grant authorizes one. Omitting it leaves the
        pre-#114 verdict rather than inventing a permissive one."""
        self.init_workspace()
        self.write("ci/manifest/WP-114.json", "{}")
        self.granted(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator",
        )
        code, doc = self.write_set_check()
        self.assertEqual(code, 1)
        self.assertEqual(
            [v["path"] for v in doc["write_set"]["violations"]], ["ci/manifest/WP-114.json"]
        )
        self.assertIn("no write grant is consulted", doc["write_set"]["violations"][0]["reason"])
        # ...and the ledger is reported, with every record's status, so the
        # condition is visible where it is observed rather than at the write.
        self.assertEqual(doc["write_set"]["grants"]["state"], "readable")
        self.assertEqual(doc["write_set"]["grants"]["issue"], None)
        self.assertEqual(
            [e["status"] for e in doc["write_set"]["grants"]["entries"]], ["no-issue-named"]
        )
        self.assertIn("were not consulted", doc["write_set"]["details"])

    def test_a_grant_for_another_issue_authorizes_nothing(self):
        self.init_workspace()
        self.write("ci/manifest/WP-114.json", "{}")
        self.granted(
            "--issue", "113", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator",
        )
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 1)
        self.assertEqual(
            [v["path"] for v in doc["write_set"]["violations"]], ["ci/manifest/WP-114.json"]
        )
        self.assertEqual(
            [e["status"] for e in doc["write_set"]["grants"]["entries"]], ["other-issue"]
        )

    def test_a_grant_for_another_path_authorizes_nothing(self):
        self.init_workspace()
        self.write("ci/manifest/WP-115.json", "{}")
        self.granted(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator",
        )
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 1)
        self.assertEqual(
            [v["path"] for v in doc["write_set"]["violations"]], ["ci/manifest/WP-115.json"]
        )

    def test_a_grant_cannot_authorize_an_out_of_set_write(self):
        """The widening case, refused at record time and refused again at
        consumption time: a grant naming a path no protected root covers can
        never excuse an out-of-set file, because the ledger is consulted on
        the protected branch only."""
        self.init_workspace()
        self.write("rust/rogue/evil.rs", "pub fn evil() {}\n")
        code, _, err = self.authorize(
            "--issue", "114", "--path", "rust/rogue/evil.rs", "--op", "write",
            "--issuer", "operator",
        )
        self.assertEqual(code, 2)
        self.assertIn("names nothing any declared protected_roots pattern covers", err)
        self.assertFalse(self.ledger.exists())
        # and even a hand-appended line for it authorizes nothing
        self.append_ledger(self._valid_record(path="rust/rogue/evil.rs"))
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 1)
        self.assertEqual(
            [(v["path"], v["kind"]) for v in doc["write_set"]["violations"]],
            [("rust/rogue/evil.rs", "out-of-set")],
        )
        self.assertEqual(doc["write_set"]["authorized_writes"], [])

    def test_a_delete_grant_does_not_authorize_the_write_it_names(self):
        """`op` is a closed vocabulary and a grant authorizes exactly the one
        operation it names -- here the one whose effect (an absent file) is
        not what this check can observe."""
        self.init_workspace()
        self.write("ci/manifest/WP-114.json", "{}")
        self.granted(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "delete",
            "--issuer", "operator",
        )
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 1)
        self.assertEqual(
            [v["path"] for v in doc["write_set"]["violations"]], ["ci/manifest/WP-114.json"]
        )
        self.assertEqual(doc["write_set"]["authorized_writes"], [])
        # the record is still auditable, and reported as active
        self.assertEqual(
            [e["status"] for e in doc["write_set"]["grants"]["entries"]], ["active"]
        )


class GrantCannotWidenTest(GrantFixture):
    """Each way a grant might become a permission, refused at the only place
    it can be recorded, writing nothing."""

    def test_an_absolute_path_is_refused(self):
        self.init_workspace()
        code, _, err = self.authorize(
            "--issue", "114", "--path", "/etc/passwd", "--op", "write", "--issuer", "operator"
        )
        self.assertEqual(code, 2)
        self.assertIn("workspace-relative", err)
        self.assertFalse(self.ledger.exists())

    def test_a_traversing_path_is_refused(self):
        self.init_workspace()
        code, _, err = self.authorize(
            "--issue", "114", "--path", "../outside/PROBE.json", "--op", "write",
            "--issuer", "operator",
        )
        self.assertEqual(code, 2)
        self.assertIn("escapes the workspace", err)

    def test_a_vacuous_pattern_is_refused(self):
        self.init_workspace()
        for pattern in ("**", "*/*", "*/**"):
            with self.subTest(pattern=pattern):
                code, _, err = self.authorize(
                    "--issue", "114", "--path", pattern, "--op", "write", "--issuer", "operator"
                )
                self.assertEqual(code, 2)
                self.assertIn("not a bounded pattern", err)
        self.assertFalse(self.ledger.exists())

    def test_a_directory_path_is_refused(self):
        self.init_workspace()
        code, _, err = self.authorize(
            "--issue", "114", "--path", "ci/manifest/", "--op", "write", "--issuer", "operator"
        )
        self.assertEqual(code, 2)
        self.assertIn("names a directory", err)

    def test_a_grant_cannot_be_recorded_when_the_ledger_is_protected(self):
        """A workspace whose protected roots cover the ledger cannot record a
        grant: every append would itself be a protected write, and a ledger
        whose own writes are boundary breaches is not a capability record."""
        self.init_workspace()
        self.edit_descriptor(
            lambda d: d["write_set"]["protected_roots"].append("ci/results/**")
        )
        code, _, err = self.authorize(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator",
        )
        self.assertEqual(code, 2)
        self.assertIn("is inside declared protected root", err)

    def test_ttl_bounds_are_enforced(self):
        self.init_workspace()
        code, _, err = self.authorize(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator", "--ttl", "0",
        )
        self.assertEqual(code, 2)
        self.assertIn("positive number of seconds", err)
        code, _, err = self.authorize(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator", "--ttl", str(write_authorization.MAX_TTL_SECONDS + 1),
        )
        self.assertEqual(code, 2)
        self.assertIn("exceeds the", err)
        self.assertFalse(self.ledger.exists())

    def test_one_shot_on_a_pattern_is_refused(self):
        self.init_workspace()
        code, _, err = self.authorize(
            "--issue", "114", "--path", "ci/manifest/WP-114*.json", "--op", "write",
            "--issuer", "operator", "--one-shot",
        )
        self.assertEqual(code, 2)
        self.assertIn("one-shot grant binds one exact file", err)

    def test_an_unnamed_issuer_is_refused(self):
        self.init_workspace()
        code, _, err = self.authorize(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write", "--issuer", "  "
        )
        self.assertEqual(code, 2)
        self.assertIn("--issuer is required", err)

    def test_a_non_positive_issue_is_refused(self):
        self.init_workspace()
        code, _, err = self.authorize(
            "--issue", "0", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator",
        )
        self.assertEqual(code, 2)
        self.assertIn("positive integer", err)

    def test_an_unknown_operation_is_refused_by_the_parser(self):
        self.init_workspace()
        with self.assertRaises(SystemExit) as raised:
            self.authorize(
                "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "chmod",
                "--issuer", "operator",
            )
        self.assertEqual(raised.exception.code, 2)
        self.assertFalse(self.ledger.exists())

    def test_an_unknown_issuer_kind_is_refused_by_the_parser(self):
        self.init_workspace()
        with self.assertRaises(SystemExit) as raised:
            self.authorize(
                "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
                "--issuer", "operator", "--issuer-kind", "robot",
            )
        self.assertEqual(raised.exception.code, 2)
        self.assertFalse(self.ledger.exists())

    def test_every_binding_flag_is_required_by_the_parser(self):
        """There is no default for any of them: a grant with no issue, path,
        operation or issuer is not a capability record, so the command must be
        asked for each rather than inventing one."""
        self.init_workspace()
        full = (
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator",
        )
        for flag in ("--issue", "--path", "--op", "--issuer"):
            with self.subTest(flag=flag):
                index = full.index(flag)
                # drop the flag and the value it carries
                args = [a for i, a in enumerate(full) if i not in (index, index + 1)]
                with self.assertRaises(SystemExit) as raised:
                    self.authorize(*args)
                self.assertEqual(raised.exception.code, 2)
                self.assertFalse(self.ledger.exists())

    def test_an_empty_path_is_refused(self):
        self.init_workspace()
        code, _, err = self.authorize(
            "--issue", "114", "--path", "  ", "--op", "write", "--issuer", "operator"
        )
        self.assertEqual(code, 2)
        self.assertIn("--path is required", err)
        self.assertFalse(self.ledger.exists())

    def test_an_issued_at_without_an_offset_is_refused(self):
        self.init_workspace()
        code, _, err = self.authorize(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator", "--issued-at", "2026-10-03T12:00:00",
        )
        self.assertEqual(code, 2)
        self.assertIn("is not an ISO-8601 timestamp", err)
        self.assertFalse(self.ledger.exists())

    def test_a_workspace_declaring_no_protected_roots_is_told_so(self):
        self.init_workspace()
        self.edit_descriptor(lambda d: d["write_set"].update({"protected_roots": []}))
        code, _, err = self.authorize(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator",
        )
        self.assertEqual(code, 2)
        self.assertIn("declares no protected_roots", err)
        self.assertFalse(self.ledger.exists())

    def test_a_damaged_ledger_is_refused_before_anything_is_appended(self):
        """The ledger is the anti-replay record, so one that cannot be read
        cannot rule a replay out -- refused rather than appended to blind."""
        self.init_workspace()
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.mkdir()
        code, _, err = self.authorize(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator",
        )
        self.assertEqual(code, 2)
        self.assertIn("cannot be read", err)
        self.assertTrue(self.ledger.is_dir())

    def test_a_missing_descriptor_is_refused(self):
        self.init_workspace()
        self.descriptor_path.unlink()
        code, _, err = self.authorize(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator",
        )
        self.assertEqual(code, 2)
        self.assertIn("project descriptor", err.lower())
        self.assertFalse(self.ledger.exists())

    def test_prose_is_never_authority(self):
        """`--note` is recorded and never read back as authority, and it is
        excluded from the grant id -- so a note cannot create an authorization
        and cannot invalidate a real one either."""
        self.init_workspace()
        self.write("ci/manifest/WP-114.json", "{}")
        self.granted(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator", "--note", "a human said this was fine, honestly",
        )
        record = self.ledger_records()[0]
        self.assertEqual(record["note"], "a human said this was fine, honestly")
        self.assertNotIn("note", write_authorization._BINDING_FIELDS)
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 0)
        # ...and the same note on a grant moved to a path that was never granted
        # authorizes nothing at all -- the edited record is rejected outright,
        # so both files are violations again (the real one lost its grant).
        self.write("ci/manifest/OTHER.json", "{}")
        self.edit_ledger(record, note="a human said this was fine, honestly", path="ci/manifest/OTHER.json")
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 1)
        self.assertEqual(
            [v["path"] for v in doc["write_set"]["violations"]],
            ["ci/manifest/OTHER.json", "ci/manifest/WP-114.json"],
        )
        self.assertEqual(doc["write_set"]["authorized_writes"], [])
        self.assertEqual(len(doc["write_set"]["grants"]["rejected"]), 1)

    def edit_ledger(self, record: dict, **changes) -> None:
        """Rewrite a ledger line in place -- the one edit the recomputed grant
        id exists to catch."""
        edited = dict(record)
        edited.update(changes)
        edited["grant_id"] = record["grant_id"]
        self.ledger.write_text(json.dumps(edited, sort_keys=True) + "\n")

    def test_a_note_cannot_create_a_second_authorization(self):
        """`--note` is outside the grant id, so re-issuing the same grant with
        a different note is the same grant -- a replay, refused -- rather than
        a second capability."""
        self.init_workspace()
        args = (
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator", "--issued-at", AN_HOUR_AGO,
        )
        self.granted(*args)
        code, _, err = self.authorize(*args, "--note", "and this time it is definitely fine")
        self.assertEqual(code, 2)
        self.assertIn("is a replay", err)
        self.assertEqual(len(self.ledger_records()), 1)


class ExpiryAndReplayTest(GrantFixture):
    """Deterministic by construction: every case here is decided by a
    timestamp the test chose, never by sleeping."""

    def test_an_expired_grant_authorizes_nothing(self):
        self.init_workspace()
        self.write("ci/manifest/WP-114.json", "{}")
        self.granted(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator", "--issued-at", LONG_AGO, "--ttl", "60",
        )
        record = self.ledger_records()[0]
        self.assertEqual(record["expires_at"], "2020-01-01T00:01:00+00:00")
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 1)
        self.assertEqual(
            [v["path"] for v in doc["write_set"]["violations"]], ["ci/manifest/WP-114.json"]
        )
        self.assertEqual(
            [e["status"] for e in doc["write_set"]["grants"]["entries"]], ["expired"]
        )

    def test_an_unexpired_grant_does_authorize(self):
        self.init_workspace()
        self.write("ci/manifest/WP-114.json", "{}")
        self.granted(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator",
        )
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 0)
        self.assertEqual(
            [e["status"] for e in doc["write_set"]["grants"]["entries"]], ["active"]
        )

    def test_expires_at_must_equal_issued_at_plus_ttl(self):
        """Hand-edited to outlive its own TTL: the reader recomputes the
        relation, so a record claiming more time than it was granted is
        rejected rather than honored."""
        self.init_workspace()
        self.write("ci/manifest/WP-114.json", "{}")
        self.granted(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator",
        )
        record = self.ledger_records()[0]
        record["expires_at"] = "2099-01-01T00:00:00+00:00"
        self.ledger.write_text(json.dumps(record, sort_keys=True) + "\n")
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 1)
        self.assertEqual(
            [v["path"] for v in doc["write_set"]["violations"]], ["ci/manifest/WP-114.json"]
        )
        self.assertEqual(len(doc["write_set"]["grants"]["rejected"]), 1)
        self.assertIn("cannot outlive the TTL it names", doc["write_set"]["grants"]["rejected"][0]["reason"])
        self.assertIn("are not usable", doc["write_set"]["details"])

    def test_an_edited_record_is_rejected_rather_than_honored(self):
        """The grant id is a hash of the binding it names, so moving the
        authorization to another path under the same id does not work."""
        self.init_workspace()
        self.write("ci/manifest/WP-115.json", "{}")
        self.granted(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator",
        )
        record = self.ledger_records()[0]
        record["path"] = "ci/manifest/WP-115.json"
        self.ledger.write_text(json.dumps(record, sort_keys=True) + "\n")
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 1)
        self.assertEqual(
            [v["path"] for v in doc["write_set"]["violations"]], ["ci/manifest/WP-115.json"]
        )
        self.assertIn("edited in place", doc["write_set"]["grants"]["rejected"][0]["reason"])

    def test_a_ttl_above_the_ceiling_in_a_hand_written_record_authorizes_nothing(self):
        """The ceiling is the reader's rule too, not only the writer's: a line
        appended by something other than `authorize-write` must not buy a year
        of standing authority, however self-consistent its expiry is."""
        self.init_workspace()
        self.write("ci/manifest/WP-114.json", "{}")
        record = self._valid_record(ttl_seconds=write_authorization.MAX_TTL_SECONDS + 1)
        record["issued_at"] = "2026-10-03T00:00:00+00:00"
        record["expires_at"] = "2027-10-03T00:00:01+00:00"
        record["grant_id"] = write_authorization.grant_id_for(
            {f: record[f] for f in write_authorization._BINDING_FIELDS}
        )
        self.append_ledger(record)
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 1)
        self.assertEqual(
            [v["path"] for v in doc["write_set"]["violations"]], ["ci/manifest/WP-114.json"]
        )
        self.assertEqual(len(doc["write_set"]["grants"]["rejected"]), 1)
        self.assertIn("exceeds the", doc["write_set"]["grants"]["rejected"][0]["reason"])
        self.assertEqual(doc["write_set"]["authorized_writes"], [])

    def test_a_damaged_ledger_line_is_reported_and_authorizes_nothing(self):
        self.init_workspace()
        self.write("ci/manifest/WP-114.json", "{}")
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text("{not json\n")
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 1)
        self.assertEqual(len(doc["write_set"]["grants"]["rejected"]), 1)
        self.assertIn("not valid JSON", doc["write_set"]["grants"]["rejected"][0]["reason"])

    def test_a_replayed_grant_records_nothing(self):
        """`grant_id` is a hash of the binding, so re-running the issuing
        command with the same `--issued-at` produces the same id and is
        refused: a retry is idempotent, not a way to stack grants."""
        self.init_workspace()
        args = (
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator", "--issued-at", AN_HOUR_AGO,
        )
        self.granted(*args)
        self.assertEqual(len(self.ledger_records()), 1)
        code, _, err = self.authorize(*args)
        self.assertEqual(code, 2)
        self.assertIn("is a replay", err)
        self.assertEqual(len(self.ledger_records()), 1)

    def test_a_repeated_line_in_a_hand_edited_ledger_is_a_duplicate(self):
        """Appended rather than issued: the reader reports the earlier copy as
        a duplicate (last entry wins, so re-issuing supersedes) and neither
        copy becomes two authorizations."""
        self.init_workspace()
        self.write("ci/manifest/WP-114.json", "{}")
        self.granted(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator",
        )
        record = self.ledger_records()[0]
        self.append_ledger(record)
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 0)
        self.assertEqual(
            [e["status"] for e in doc["write_set"]["grants"]["entries"]],
            ["duplicate", "active"],
        )

    def test_a_one_shot_grant_is_reported_spent_once_the_file_is_there(self):
        self.init_workspace()
        self.write("ci/manifest/WP-114.json", "{}")
        self.granted(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator", "--one-shot",
        )
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 0)
        self.assertEqual(doc["write_set"]["authorized_writes"][0]["status"], "spent")
        self.assertTrue(doc["write_set"]["authorized_writes"][0]["one_shot"])
        # a second file the one-shot grant did not name is still a violation:
        # one exact file, not the directory it sits in
        self.write("ci/manifest/WP-114-again.json", "{}")
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 1)
        self.assertEqual(
            [v["path"] for v in doc["write_set"]["violations"]], ["ci/manifest/WP-114-again.json"]
        )

    def test_a_reissued_grant_supersedes_the_earlier_one(self):
        self.init_workspace()
        self.write("ci/manifest/WP-114.json", "{}")
        self.declare_supervisor("supervisor-bot")
        first = self.granted(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator",
        )
        second = self.granted(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "supervisor-bot", "--issuer-kind", "supervisor",
        )
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 0)
        self.assertEqual(doc["write_set"]["authorized_writes"][0]["grant_id"], second)
        self.assertNotEqual(first, second)


class SupervisorWhitelistTest(GrantFixture):
    """The supervisor lane is the only one an automation may use, and it is
    reachable only through an explicit machine-checkable whitelist the
    consumer re-verifies -- never through prose."""

    def test_an_undeclared_supervisor_is_refused(self):
        self.init_workspace()
        code, _, err = self.authorize(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "ci-bot", "--issuer-kind", "supervisor",
        )
        self.assertEqual(code, 2)
        self.assertIn("not a declared supervisor", err)
        self.assertIn("authorized_supervisors", err)
        self.assertFalse(self.ledger.exists())

    def test_a_declared_supervisor_may_issue_and_is_honored(self):
        self.init_workspace()
        self.declare_supervisor("ci-bot")
        self.write("ci/manifest/WP-114.json", "{}")
        self.granted(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "ci-bot", "--issuer-kind", "supervisor",
        )
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 0)
        entry = doc["write_set"]["authorized_writes"][0]
        self.assertEqual(entry["issuer"], "ci-bot")
        self.assertEqual(entry["issuer_kind"], "supervisor")

    def test_removing_the_supervisor_from_the_descriptor_stops_the_grant(self):
        """The whitelist is re-read on every consumption, not baked into the
        record: withdrawing the authority stops the grant immediately."""
        self.init_workspace()
        self.declare_supervisor("ci-bot")
        self.write("ci/manifest/WP-114.json", "{}")
        self.granted(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "ci-bot", "--issuer-kind", "supervisor",
        )
        self.assertEqual(self.write_set_check("--issue", "114")[0], 0)
        self.edit_descriptor(lambda d: d["write_set"].pop("authorized_supervisors"))
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 1)
        self.assertEqual(
            [v["path"] for v in doc["write_set"]["violations"]], ["ci/manifest/WP-114.json"]
        )
        self.assertEqual(
            [e["status"] for e in doc["write_set"]["grants"]["entries"]], ["unverified-issuer"]
        )

    def test_the_human_lane_needs_no_whitelist_entry(self):
        self.init_workspace()
        self.write("ci/manifest/WP-114.json", "{}")
        self.granted(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator", "--issuer-kind", "human",
        )
        self.assertEqual(self.write_set_check("--issue", "114")[0], 0)

    def test_the_descriptor_field_validates_and_is_closed(self):
        self.init_workspace()
        self.declare_supervisor("ci-bot")
        document = json.loads(self.descriptor_path.read_text())
        self.assertValid(DESCRIPTOR_SCHEMA, document, "descriptor")
        # a non-string entry is refused at load rather than authorizing nobody
        document["write_set"]["authorized_supervisors"] = [""]
        validator = make_validator(DESCRIPTOR_SCHEMA)
        self.assertTrue(list(validator.iter_errors(document)))


class OneShotLifecycleTest(GrantFixture):
    """What a `--one-shot` grant does, what it cannot do, and the two ends of
    the window it lives in -- the integration half of chainlink #114.

    The distinction this class pins is the one a reader of the report cannot
    check for himself: `spent` is an *attribution* -- the write a grant names
    was attributed to it -- and not a consumed count. A workspace snapshot
    sees which files exist, not how often each was written, so the check
    cannot tell that write from a later edit to the same file. It says so
    instead of letting the word imply a counter it never kept, and what does
    bound the attribution is the grant's TTL.

    The window's other end is closed too. `expires_at == issued_at +
    ttl_seconds` alone would call a future-dated line unexpired, so a grant
    nobody has issued yet would authorize a write; both halves hold -- the
    producer refuses to record such a grant, and the consumer reports one it
    reads as `not-yet-issued` and honors as nothing.
    """

    def one_shot_grant(self, path: str = "ci/manifest/WP-114.json", *extra: str) -> str:
        return self.granted(
            "--issue", "114", "--path", path, "--op", "write",
            "--issuer", "operator", "--one-shot", *extra,
        )

    def test_a_one_shot_write_is_clean_reported_spent_and_states_its_limit(self):
        self.init_workspace()
        self.write("ci/manifest/WP-114.json", "{}")
        grant_id = self.one_shot_grant()
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 0)
        entry = doc["write_set"]["authorized_writes"][0]
        self.assertEqual((entry["grant_id"], entry["status"], entry["one_shot"]),
                         (grant_id, "spent", True))
        # the entry carries the bound that DOES apply, so a consumer can act on
        # it without parsing prose
        self.assertEqual(entry["expires_at"], self.ledger_records()[0]["expires_at"])
        details = doc["write_set"]["details"]
        self.assertIn("one-shot", details)
        self.assertIn("counts no edits", details)
        self.assertIn("until the grant expires", details)
        # and the same sentence is in the human-readable report, since the text
        # surface prints the same `details`
        _, out, _ = self.run_cli("write-set-check", "--issue", "114")
        self.assertIn("counts no edits", out)

    def test_a_later_edit_to_the_same_file_is_the_stated_residual(self):
        """Pinned deliberately, because it is the limit rather than an
        oversight: the second write is still attributed to the one-shot grant,
        the report still calls the grant `spent`, and the report still states
        that it counts no edits. A future change to this behavior has to
        change what the report says in the same commit -- which is the point of
        stating it where the verdict is observed."""
        self.init_workspace()
        self.write("ci/manifest/WP-114.json", "{}")
        self.one_shot_grant()
        self.assertEqual(self.write_set_check("--issue", "114")[0], 0)
        self.write("ci/manifest/WP-114.json", '{"edited": true}')
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 0)
        self.assertEqual(doc["write_set"]["authorized_writes"][0]["status"], "spent")
        self.assertIn("counts no edits", doc["write_set"]["details"])
        # the second file is still refused: one exact file, not its directory
        self.write("ci/manifest/WP-114b.json", "{}")
        self.assertEqual(self.write_set_check("--issue", "114")[0], 1)

    def test_a_one_shot_grant_cannot_be_re_issued_as_a_second_write(self):
        """Replay, from the writer's side. The id is a hash of the binding, so
        an identical re-issue -- same request at the same instant -- is refused
        and records nothing: a retry of the issuing command is idempotent
        rather than a way to stack one-shot grants. Both calls name the same
        `--issued-at`, so the case does not depend on which second the suite
        happens to run in."""
        self.init_workspace()
        self.write("ci/manifest/WP-114.json", "{}")
        issued = (datetime.now(timezone.utc) - timedelta(seconds=60)).replace(
            microsecond=0
        ).isoformat()
        self.one_shot_grant("ci/manifest/WP-114.json", "--issued-at", issued)
        code, _, err = self.authorize(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator", "--one-shot", "--issued-at", issued,
        )
        self.assertEqual(code, 2)
        self.assertIn("is a replay", err)
        self.assertEqual(len(self.ledger_records()), 1)

    def test_a_repeated_one_shot_line_is_a_duplicate_and_honors_one_path(self):
        """Replay, from the reader's side: append-only, so a repeated id is
        `duplicate` with the last line live -- and the pair authorizes one
        write at the path it names, never two."""
        self.init_workspace()
        self.write("ci/manifest/WP-114.json", "{}")
        record = self._unexpired_record(path="ci/manifest/WP-114.json", one_shot=True)
        self.append_ledger(record)
        self.append_ledger(record)
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 0)
        self.assertEqual(
            [e["status"] for e in doc["write_set"]["grants"]["entries"]], ["duplicate", "active"]
        )
        self.assertEqual(
            [e["path"] for e in doc["write_set"]["authorized_writes"]], ["ci/manifest/WP-114.json"]
        )
        # two copies of the record are still one authorization, at one path
        self.write("ci/manifest/WP-114b.json", "{}")
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 1)
        self.assertEqual(
            [v["path"] for v in doc["write_set"]["violations"]], ["ci/manifest/WP-114b.json"]
        )

    def test_an_expired_one_shot_grant_authorizes_nothing(self):
        self.init_workspace()
        self.write("ci/manifest/WP-114.json", "{}")
        self.append_ledger(self._valid_record(path="ci/manifest/WP-114.json", one_shot=True))
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 1)
        self.assertEqual(
            [v["path"] for v in doc["write_set"]["violations"]], ["ci/manifest/WP-114.json"]
        )
        self.assertEqual(
            [e["status"] for e in doc["write_set"]["grants"]["entries"]],
            [write_authorization.STATUS_EXPIRED],
        )

    def test_a_window_that_has_not_opened_yet_authorizes_nothing(self):
        self.init_workspace()
        self.write("ci/manifest/WP-114.json", "{}")
        self.append_ledger(self._future_record(path="ci/manifest/WP-114.json"))
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 1)
        self.assertEqual(
            [v["path"] for v in doc["write_set"]["violations"]], ["ci/manifest/WP-114.json"]
        )
        self.assertEqual(
            [e["status"] for e in doc["write_set"]["grants"]["entries"]],
            [write_authorization.STATUS_NOT_YET_ISSUED],
        )
        # reported, not dropped: the record is in the audit with the status
        # that says why it did not authorize
        self.assertEqual(len(doc["write_set"]["grants"]["entries"]), 1)
        self.assertEqual(doc["write_set"]["grants"]["rejected"], [])
        self.assertEqual(doc["write_set"]["authorized_writes"], [])

    def test_a_future_issued_at_is_refused_by_the_producer_and_writes_nothing(self):
        self.init_workspace()
        future = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0)
        code, _, err = self.authorize(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator", "--issued-at", future.isoformat(),
        )
        self.assertEqual(code, 2)
        self.assertIn("is in the future", err)
        self.assertFalse(self.ledger.exists(), "a refusal must not create the ledger")

    def test_the_window_verdicts_are_deterministic_across_repeated_runs(self):
        """No sleeping, no clock race: a grant inside its window, one past it
        and one short of it all reach the same verdict on a re-run, and the
        report is byte-identical, because expiry is arithmetic against the
        run's own clock rather than an observation over time."""
        self.init_workspace()
        self.write("ci/manifest/WP-114.json", "{}")
        self.write("ci/manifest/WP-115.json", "{}")
        self.append_ledger(self._unexpired_record(path="ci/manifest/WP-114.json"))
        self.append_ledger(self._valid_record(path="ci/manifest/WP-115.json"))
        self.append_ledger(self._future_record(path="ci/manifest/WP-116.json"))
        first = None
        for _ in range(3):
            code, out, _ = self.run_cli("write-set-check", "--json", "--issue", "114")
            self.assertEqual(code, 1)
            if first is None:
                first = out
            self.assertEqual(out, first)
        self.assertEqual(
            [e["status"] for e in json.loads(first)["write_set"]["grants"]["entries"]],
            ["active", "expired", "not-yet-issued"],
        )


class GrantRecordTest(GrantFixture):
    """The record itself: the audit line, its versioned shape, and the help
    text that documents the rules it enforces."""

    def test_the_record_binds_every_field_its_authority_is_of(self):
        self.init_workspace()
        code, out, _ = self.run_cli(
            "authorize-write", "--json", "--issue", "114", "--path", "ci/manifest/WP-114.json",
            "--op", "write", "--issuer", "operator", "--issuer-kind", "human", "--ttl", "120",
            "--one-shot", "--note", "sanctioned by the operator for the 114 repro",
            "--issued-at", AN_HOUR_AGO,
        )
        self.assertEqual(code, 0)
        record = json.loads(out)
        self.assertEqual(record["event"], "grant")
        self.assertEqual(record["schema_version"], "1.0")
        self.assertEqual(record["issue"], 114)
        self.assertEqual(record["path"], "ci/manifest/WP-114.json")
        self.assertEqual(record["path_kind"], "exact")
        self.assertEqual(record["op"], "write")
        self.assertTrue(record["one_shot"])
        self.assertEqual(record["issuer"], "operator")
        self.assertEqual(record["issuer_kind"], "human")
        self.assertEqual(record["issued_at"], AN_HOUR_AGO)
        self.assertEqual(record["ttl_seconds"], 120)
        self.assertEqual(record["expires_at"], "2020-01-01T23:02:00+00:00")
        self.assertEqual(record["note"], "sanctioned by the operator for the 114 repro")
        self.assertTrue(record["grant_id"].startswith("GW-"))
        # exactly the line that was appended, and nothing else was written
        self.assertEqual(self.ledger_records(), [record])

    def test_the_record_validates_against_its_published_schema(self):
        """`docs/write-grant-schema.json` is the record's versioned contract,
        so a shape the command emits is checked against it here: two
        declarations of one fact that cannot drift silently. What the schema
        cannot express (the expiry relation and the id hash) is pinned by the
        cases around it."""
        self.init_workspace()
        variants = (
            ("ci/manifest/WP-114.json", ("--op", "write")),
            ("ci/manifest/WP-114b.json", ("--op", "write", "--one-shot")),
            ("ci/manifest/WP-114c.json", ("--op", "delete")),
            ("ci/manifest/WP-114d.json", ("--op", "write", "--note", "an audit note")),
        )
        for path, flags in variants:
            with self.subTest(path=path):
                code, out, err = self.run_cli(
                    "authorize-write", "--json", "--issue", "114", "--path", path,
                    "--issuer", "operator", "--ttl", "120", *flags,
                )
                self.assertEqual(code, 0, err)
                self.assertValid(GRANT_SCHEMA, json.loads(out), f"grant for {path}")
        for record in self.ledger_records():
            self.assertValid(GRANT_SCHEMA, record, f"ledger line {record['grant_id']}")

    def test_the_schema_is_closed_so_an_unknown_key_cannot_authorize(self):
        record = dict(self.ledger_records()[0]) if self.ledger.exists() else {
            "event": "grant", "schema_version": "1.0", "grant_id": "GW-000000000000",
            "issue": 114, "path": "ci/manifest/WP-114.json", "path_kind": "exact",
            "op": "write", "one_shot": False, "issuer": "operator", "issuer_kind": "human",
            "issued_at": AN_HOUR_AGO, "ttl_seconds": 120,
            "expires_at": "2020-01-01T23:02:00+00:00", "note": None,
        }
        record["allow_any_path"] = True
        validator = make_validator(GRANT_SCHEMA)
        errors = list(validator.iter_errors(record))
        self.assertTrue(errors, "a key the schema never had must not validate")

    def test_the_grant_id_is_the_hash_of_the_binding(self):
        self.init_workspace()
        grant_id = self.granted(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator", "--issued-at", AN_HOUR_AGO,
        )
        record = self.ledger_records()[0]
        self.assertEqual(
            grant_id,
            write_authorization.grant_id_for(
                {f: record[f] for f in write_authorization._BINDING_FIELDS}
            ),
        )

    def test_the_record_is_auditable_on_disk_and_reported_even_when_unused(self):
        """A ledger nobody looked at is how an authorization surface goes
        stale unnoticed: the report carries it either way."""
        self.init_workspace()
        self.granted(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator",
        )
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 0)
        self.assertEqual(doc["write_set"]["grants"]["ledger"], LEDGER_REL)
        self.assertEqual(doc["write_set"]["grants"]["state"], "readable")
        code, out, _ = self.run_cli("write-set-check", "--issue", "114")
        self.assertIn(f"grant ledger: readable ({LEDGER_REL})", out)

    def test_the_audit_view_carries_the_records_own_metadata(self):
        """The issuer's note and the TTL reach the report, so "who authorized
        this, for what, and until when" is answerable without opening the
        ledger file -- on the machine-readable surface and the text one. The
        note is still metadata only: it authorizes nothing, and it cannot make
        a grant invalid either (that is the `grant_id` half, pinned above)."""
        self.init_workspace()
        self.write("ci/manifest/WP-114.json", "{}")
        self.granted(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator", "--ttl", "120",
            "--note", "sanctioned for the 114 repro",
        )
        _, doc = self.write_set_check("--issue", "114")
        entry = doc["write_set"]["grants"]["entries"][0]
        self.assertEqual(entry["ttl_seconds"], 120)
        self.assertEqual(entry["note"], "sanctioned for the 114 repro")
        _, out, _ = self.run_cli("write-set-check", "--issue", "114")
        self.assertIn("ttl 120s", out)
        self.assertIn("-- sanctioned for the 114 repro", out)
        # a grant recorded without one reports null rather than inventing text
        self.granted(
            "--issue", "114", "--path", "ci/manifest/WP-115.json", "--op", "write",
            "--issuer", "operator",
        )
        _, doc = self.write_set_check("--issue", "114")
        self.assertEqual(
            [e["note"] for e in doc["write_set"]["grants"]["entries"]],
            ["sanctioned for the 114 repro", None],
        )

    def test_the_text_output_names_every_binding(self):
        self.init_workspace()
        code, out, _ = self.run_cli(
            "authorize-write", "--issue", "114", "--path", "ci/manifest/WP-114.json",
            "--op", "write", "--issuer", "operator", "--ttl", "120",
        )
        self.assertEqual(code, 0)
        for expected in (
            "issue: 114",
            "path: ci/manifest/WP-114.json (exact)",
            "op: write",
            "one_shot: no",
            "issuer: operator (human)",
            "ttl 120s",
            f"ledger: {LEDGER_REL}",
            "write-set-check --issue 114",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, out)

    def test_the_text_output_states_what_a_one_shot_grant_does_not_bound(self):
        """Said at the moment the grant is recorded, which is the only moment a
        reader can act on it -- and with the actionable half, because the limit
        is a fact an operator needs before issuing the grant rather than after
        the check reports it."""
        self.init_workspace()
        code, out, _ = self.run_cli(
            "authorize-write", "--issue", "114", "--path", "ci/manifest/WP-114.json",
            "--op", "write", "--issuer", "operator", "--one-shot",
        )
        self.assertEqual(code, 0)
        for expected in (
            "one_shot: yes",
            "attribution and not a count",
            "counts no edits",
            "what bounds this grant is its TTL",
            "Issue a further grant",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, out)

    def test_help_documents_the_rules_the_command_enforces(self):
        parser = pipeline.build_parser()
        subparsers = [
            action for action in parser._actions if hasattr(action, "choices") and action.choices
        ]
        authorize = next(
            action.choices["authorize-write"] for action in subparsers if action.choices
        )
        # argparse hard-wraps help text, so compare on collapsed whitespace
        text = " ".join(authorize.format_help().split())
        for expected in (
            "cannot widen allowed_roots",
            "authorized_supervisors",
            "outlive its TTL",
            "a timestamp in the future is refused",
            "that is an attribution, not a counter",
            "no prose",
            "bounded pattern",
            "only operation write-set-check consumes",
            "human checkpoint",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, text)


class GrantsAcrossIssuesTest(GrantFixture):
    """Grants are issue-scoped, and the issue is the unit the run must name
    -- so two issues' work in one workspace cannot borrow each other's
    authorization."""

    def test_two_issues_each_hold_their_own_grant(self):
        self.init_workspace()
        self.write("ci/manifest/WP-113.json", "{}")
        self.write("ci/manifest/WP-114.json", "{}")
        self.granted(
            "--issue", "113", "--path", "ci/manifest/WP-113.json", "--op", "write",
            "--issuer", "operator",
        )
        self.granted(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator",
        )
        self.assertEqual(self.write_set_check("--issue", "113")[0], 1)
        code, doc = self.write_set_check("--issue", "113")
        self.assertEqual(
            [v["path"] for v in doc["write_set"]["violations"]], ["ci/manifest/WP-114.json"]
        )
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 1)
        self.assertEqual(
            [v["path"] for v in doc["write_set"]["violations"]], ["ci/manifest/WP-113.json"]
        )
        # both grants are reported for both runs, each with its own status
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(
            sorted(e["status"] for e in doc["write_set"]["grants"]["entries"]),
            ["active", "other-issue"],
        )

    def test_an_uninitialized_workspace_refuses_the_grant_and_reports_unknown(self):
        code, _, err = self.authorize(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator",
        )
        self.assertEqual(code, 2)
        self.assertIn("project descriptor", err.lower())
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 2)
        self.assertEqual(doc["write_set"]["state"], "unknown")


class LedgerInsideAProtectedRootTest(GrantFixture):
    """The writer's refusal, held by the reader too (chainlink #114).

    `authorize-write` refuses to record a grant in a workspace whose declared
    `protected_roots` cover `ci/results/`, because every append to its own
    capability trail would be a protected write. A refusal only the writer
    keeps is a refusal a hand-appended line walks straight past -- and the
    line it walks past with is the one that matters most here: a grant naming
    the ledger's own path would make a trail inside a protected root vouch for
    its own presence there, and `write_set`'s own #103 rule is that a
    canonical pipeline location never excuses a declared protected root.
    """

    def test_a_hand_written_grant_in_a_protected_ledger_authorizes_nothing(self):
        self.init_workspace()
        self.protect_results()
        self.write("ci/manifest/WP-114.json", "{}")
        self.append_ledger(self._valid_record(path="ci/manifest/WP-114.json"))
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 1)
        self.assertEqual(
            [(v["path"], v["kind"]) for v in doc["write_set"]["violations"]],
            [("ci/manifest/WP-114.json", "protected-write")],
        )
        self.assertEqual(doc["write_set"]["authorized_writes"], [])
        self.assertEqual(
            [e["status"] for e in doc["write_set"]["grants"]["entries"]],
            [write_authorization.STATUS_LEDGER_PROTECTED],
        )
        self.assertEqual(doc["write_set"]["grants"]["ledger_protected"], "ci/results/**")

    def test_a_grant_cannot_vouch_for_the_ledgers_own_presence_in_a_protected_root(self):
        """The sharpest form: the record names the ledger's own path, so
        honoring it would move that file out of the protected-surface audit and
        report a declared protected root over `ci/results/` as one that was
        checked and found vouched."""
        self.init_workspace()
        self.protect_results()
        self.append_ledger(self._unexpired_record(path=LEDGER_REL))
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 0)
        self.assertEqual(doc["write_set"]["authorized_writes"], [])
        self.assertIn(LEDGER_REL, doc["write_set"]["protected_unvouched"])
        surface = {e["pattern"]: e for e in doc["write_set"]["protected_surface"]}
        self.assertEqual(surface["ci/results/**"]["authorized"], 0)
        self.assertEqual(surface["ci/results/**"]["unattributed"], 1)
        # ...and the audit says why, so the clean verdict cannot be read as
        # "nothing happened in that protected root"
        self.assertIn("inside declared protected root", doc["write_set"]["details"])

    def test_the_ledger_protection_is_read_from_the_descriptor_not_hardcoded(self):
        """The pattern is whatever the descriptor declares: removing the
        declaration restores consumption, so the reader is holding the same
        rule rather than a constant of its own."""
        self.init_workspace()
        self.protect_results()
        self.write("ci/manifest/WP-114.json", "{}")
        self.append_ledger(self._unexpired_record(path="ci/manifest/WP-114.json"))
        self.assertEqual(self.write_set_check("--issue", "114")[0], 1)
        self.edit_descriptor(
            lambda d: d["write_set"].__setitem__(
                "protected_roots",
                [p for p in d["write_set"]["protected_roots"] if p != "ci/results/**"],
            )
        )
        # the same ledger, the same record, no declaration covering it now --
        # which is also the state `authorize-write` would have recorded it in
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 0)
        self.assertEqual(
            [e["path"] for e in doc["write_set"]["authorized_writes"]], ["ci/manifest/WP-114.json"]
        )
        self.assertEqual(doc["write_set"]["grants"]["ledger_protected"], None)

    def test_the_text_report_names_the_protected_ledger_and_its_records(self):
        self.init_workspace()
        self.protect_results()
        self.append_ledger(self._valid_record(path="ci/manifest/WP-114.json"))
        code, out, _ = self.run_cli("write-set-check", "--issue", "114")
        self.assertEqual(code, 0)
        self.assertIn("ci/results/**", out)
        self.assertIn("authorize-write refuses", out)
        self.assertIn(write_authorization.STATUS_LEDGER_PROTECTED, out)


class GrantAuditReportTest(GrantFixture):
    """The audit view itself (chainlink #114): what the three read-only
    surfaces report about the ledger, deterministically and machine-readably,
    whichever way the ledger turned out."""

    def test_every_reported_status_is_in_the_closed_vocabulary(self):
        """`grants[].entries[].status` is a machine-readable field, so its
        vocabulary is a contract rather than whatever string a given branch
        happened to return. One workspace is walked through every rung --
        active, expired, not-yet-issued, other-issue, no-issue-named,
        duplicate, unverified-issuer, ledger-protected -- and the set it
        produced is asserted equal to the declared one: a rung nobody can
        reach is a promise the vocabulary cannot keep, and a rung outside it
        is a value no consumer was told to expect."""
        self.init_workspace()

        def statuses(*args) -> set[str]:
            _, doc = self.write_set_check(*args)
            return {e["status"] for e in doc["write_set"]["grants"]["entries"]}

        seen: set[str] = set()
        duplicate = self._unexpired_record(path="ci/manifest/WP-114.json")
        self.append_ledger(duplicate)
        seen |= statuses("--issue", "114")                      # active
        seen |= statuses("--issue", "113")                      # other-issue
        seen |= statuses()                                     # no-issue-named
        # Reuse the exact binding: fresh records receive a new `issued_at`
        # and grant id, so appending a newly rendered record would not be a
        # duplicate.
        self.append_ledger(duplicate)
        seen |= statuses("--issue", "114")                      # duplicate
        self.append_ledger(self._valid_record(path="ci/manifest/WP-115.json"))
        seen |= statuses("--issue", "114")                      # expired
        self.append_ledger(self._future_record(path="ci/manifest/WP-117.json"))
        seen |= statuses("--issue", "114")                      # not-yet-issued
        self.declare_supervisor("declared-bot")
        self.append_ledger(
            self._unexpired_record(
                path="ci/manifest/WP-116.json",
                issuer="undeclared-bot",
                issuer_kind="supervisor",
            )
        )
        seen |= statuses("--issue", "114")                      # unverified-issuer
        self.protect_results()
        seen |= statuses("--issue", "114")                      # ledger-protected

        self.assertEqual(
            seen,
            set(write_authorization.GRANT_STATUSES),
            f"the report produced {sorted(seen)} against the declared vocabulary "
            f"{sorted(write_authorization.GRANT_STATUSES)}",
        )

    def test_an_unreadable_ledger_yields_no_grant_and_says_why(self):
        """Fail-closed on an unreadable trail -- a trail that cannot be read
        proves no authorization -- and the *reason* is reported rather than
        dropped, because `unreadable` alone names nothing an operator can act
        on."""
        self.init_workspace()
        self.write("ci/manifest/WP-114.json", "{}")
        self.granted(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator",
        )
        self.ledger.unlink()
        self.ledger.mkdir()  # a path that cannot be read as a text file
        code, doc = self.write_set_check("--issue", "114")
        self.assertEqual(code, 1)
        grants = doc["write_set"]["grants"]
        self.assertEqual(grants["state"], "unreadable")
        self.assertIn(LEDGER_REL, grants["error"])
        self.assertEqual(grants["entries"], [])
        self.assertEqual(doc["write_set"]["authorized_writes"], [])
        self.assertEqual(
            [v["path"] for v in doc["write_set"]["violations"]], ["ci/manifest/WP-114.json"]
        )
        self.assertIn("could not be read", doc["write_set"]["details"])
        # the cause, not just the verdict -- and in `details` too, so the one-
        # line summary a reader sees names it rather than only "unreadable"
        self.assertIn(str(self.ledger), grants["error"])
        self.assertIn(str(self.ledger), doc["write_set"]["details"])

    def test_an_unreadable_ledger_is_reported_by_status_and_check_too(self):
        """All three surfaces call the same function, so the condition is
        visible on all three: a `check --json` consumer is not left with a
        blocking finding and no way to learn that an authorization trail went
        unreadable."""
        self.init_workspace()
        self.write("ci/manifest/WP-114.json", "{}")
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.mkdir()
        code, state = self.status("--issue", "114")
        self.assertEqual(code, 0)
        self.assertIn("could not be read", state["write_set"]["details"])
        code, consolidated = self.check("--issue", "114")
        self.assertEqual(code, 1)
        write_set_findings = [f for f in consolidated["findings"] if f["gate_id"] == "write-set"]
        # `check --json` is the consolidated findings document, so what it owes
        # a consumer here is the verdict and the grant half of the reason; the
        # ledger's own audit is reachable on `status --json` and
        # `write-set-check --json`, and the two are stated not to disagree.
        self.assertEqual(
            [(f["severity"], f["subject"], "no active write grant" in f["reason"]) for f in write_set_findings],
            [("high", "ci/manifest/WP-114.json", True)],
        )

    def test_the_text_report_names_the_ledger_failure(self):
        self.init_workspace()
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.mkdir()
        code, out, _ = self.run_cli("write-set-check", "--issue", "114")
        self.assertEqual(code, 0)
        self.assertIn("grant ledger: unreadable", out)
        self.assertIn("ledger unreadable:", out)

    def test_status_and_check_report_the_same_verdict_without_an_issue_and_for_another_issue(self):
        """The negative half of "the three surfaces cannot disagree". Only the
        positive direction was pinned before: a grant consumed by all three,
        or nothing. The other two ways to fail to consume it -- naming no
        issue, and naming a different one -- must reach the same verdict, or
        `status` and `check` are only as trustworthy as the positive test."""
        self.init_workspace()
        self.write("ci/manifest/WP-114.json", "{}")
        self.granted(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator",
        )
        for scope in ([], ["--issue", "113"]):
            with self.subTest(scope=scope):
                self.assertEqual(self.write_set_check(*scope)[0], 1)
                code, state = self.status(*scope)
                self.assertEqual(code, 0)
                self.assertValid(PROJECT_STATE_SCHEMA, state, "project-state")
                self.assertEqual(state["write_set"]["state"], "violations")
                code, consolidated = self.check(*scope)
                self.assertEqual(code, 1)
                self.assertValid(CONSOLIDATED_CHECK_SCHEMA, consolidated, "consolidated-check")
                self.assertEqual(
                    [
                        (f["severity"], f["subject"])
                        for f in consolidated["findings"]
                        if f["gate_id"] == "write-set"
                    ],
                    [("high", "ci/manifest/WP-114.json")],
                )

    def test_an_issue_no_grant_can_name_consumes_nothing(self):
        """`authorize-write` refuses a non-positive `--issue`, so no record can
        ever name one; a run that passes one anyway matches no record and must
        say so as the pre-#114 verdict rather than inventing a permissive
        reading."""
        self.init_workspace()
        self.write("ci/manifest/WP-114.json", "{}")
        self.granted(
            "--issue", "114", "--path", "ci/manifest/WP-114.json", "--op", "write",
            "--issuer", "operator",
        )
        code, doc = self.write_set_check("--issue", "0")
        self.assertEqual(code, 1)
        self.assertEqual(
            [v["path"] for v in doc["write_set"]["violations"]], ["ci/manifest/WP-114.json"]
        )
        self.assertEqual(
            [e["status"] for e in doc["write_set"]["grants"]["entries"]],
            [write_authorization.STATUS_OTHER_ISSUE],
        )

    def test_the_grant_report_is_byte_identical_across_repeated_runs(self):
        """Determinism, pinned: the audit is a machine-readable surface, and a
        consumer diffing two runs must not see churn from dict ordering, the
        clock, or the order grants happen to appear in."""
        self.init_workspace()
        self.write("ci/manifest/WP-114.json", "{}")
        self.write("scripts/rogue.py", "echo rogue\n")
        self.append_ledger(self._unexpired_record(path="ci/manifest/WP-114.json"))
        self.append_ledger(self._unexpired_record(path="scripts/rogue.py"))
        self.append_ledger_raw("{damaged")
        self.append_ledger_raw(json.dumps({"event": "something-else", "line": 4}))
        for _ in range(3):
            _, out, _ = self.run_cli("write-set-check", "--json", "--issue", "114")
            if not hasattr(self, "_first"):
                self._first = out
            self.assertEqual(out, self._first)
            _, status_out, _ = self.run_cli("status", "--json", "--issue", "114")
            if not hasattr(self, "_first_status"):
                self._first_status = status_out
            self.assertEqual(status_out, self._first_status)
        report = json.loads(self._first)["write_set"]
        # and the ordering it settles on is the documented one: authorized
        # writes in walk order, grant entries in ledger order, rejected lines
        # in line order -- a line that claims to be a grant and is not usable is
        # reported with its reason, and a line that is simply some other event
        # is counted rather than read as an authorization.
        self.assertEqual(
            [e["path"] for e in report["authorized_writes"]],
            ["ci/manifest/WP-114.json", "scripts/rogue.py"],
        )
        self.assertEqual([e["line"] for e in report["grants"]["entries"]], [1, 2])
        self.assertEqual([r["line"] for r in report["grants"]["rejected"]], [3])
        self.assertEqual(report["grants"]["ignored"], 1)
        self.assertIsNone(report["grants"]["error"])
        self.assertIsNone(report["grants"]["ledger_protected"])


if __name__ == "__main__":
    unittest.main()
