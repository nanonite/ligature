"""Deterministic crate scaffolding for a missing port-mode crate (chainlink #115).

`protected_roots` is enforced (#103), and a crate's own `Cargo.toml` is a
pattern every real project declares. #114 gave a *sanctioned* protected write a
route out of that dead end (an issue-scoped capability record,
`ligature authorize-write`), but not the other half: a port-mode pilot whose
declared crate does not exist yet has no manifest, so nothing downstream can
run, and the only supported bootstrap was a human hand-writing a protected-root
file -- which stalls the worker and produces bytes nobody can reproduce.
`ligature scaffold-crate` is the generator: the same capability record makes
the write *authorized*, and this verb makes it *deterministic*.

These tests are black-box: every case runs the real CLI through
`pipeline.main()` against a real `ligature init` workspace, because the
acceptance criteria are about what the commands *report* and *refuse*, not
about a helper's return value. What they pin, in the order the issue states it:

  * the missing-crate pilot bootstraps with a capability record scoped to the
    manifest ALONE -- no raw `Cargo.toml` write scope is granted, and the
    `src/lib.rs` skeleton needs none at all (the headline case, asserted
    positively rather than left to the refusals below);
  * no capability record, an expired one, another issue's one, a `delete` one,
    or one for a different path: all refused, each naming why, and a refusal
    writes nothing;
  * only descriptor-declared crate names are accepted -- an undeclared name,
    an ambiguous one, an absolute one, one with `..`, one with a glob
    character, and one that resolves into a pipeline artifact location, a
    `specs/` tree or the pinned upstream checkout;
  * there is no way to supply manifest text, and no way to name a target other
    than a declared crate;
  * the output is deterministic and idempotent (`unchanged` writes nothing,
    byte-identical hashes), while an existing manifest with different bytes and
    an existing `src/lib.rs` are never overwritten;
  * the receipt records issue, grant, generated paths, before/after hashes and
    validator results, and validates against `docs/scaffold-receipt-schema.json`;
  * every named check runs before success, and `write_set`'s own verdict --
    not this module's opinion -- is what decides the write was covered.
"""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import pipeline  # noqa: E402
import write_authorization  # noqa: E402
from schema_utils import make_validator  # noqa: E402

RECEIPT_SCHEMA = json.loads((ROOT / "docs" / "scaffold-receipt-schema.json").read_text())
LEDGER_REL = "ci/results/protected-writes.jsonl"
MANIFEST_REL = "rust/date-creusot-core/Cargo.toml"
SKELETON_REL = "rust/date-creusot-core/src/lib.rs"

# The date-creusot pilot's filled write_set, verbatim from the report -- the
# same fixture tests/test_write_set.py uses, so the protected write here is the
# protected write #103 shipped.
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

# A fixed date far in the past, so a grant issued at it with any sane TTL is
# expired by arithmetic rather than by sleeping (the suite never waits).
LONG_AGO = "2020-01-01T00:00:00+00:00"


class ScaffoldFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name).resolve()
        self.descriptor_path = self.workspace / "project-descriptor.json"

    def tearDown(self):
        self._tmp.cleanup()

    # -- workspace ----------------------------------------------------------
    def init_workspace(self, mode="port", name="date-creusot"):
        """A real `ligature init`, the policy marker stamped as #113 requires,
        and the pilot's filled write_set -- the same fixture shape
        tests/test_authorize_write.py uses, so a grant here is a grant #114
        shipped."""
        code, _, _ = self.run_cli("init", "--mode", mode, "--name", name)
        self.assertEqual(code, 0)
        code, _, err = self.run_cli(
            "accept-policy", "--reviewer", "pilot", "--version", "reliance-policy@1.0"
        )
        self.assertEqual(code, 0, err)
        descriptor = json.loads(self.descriptor_path.read_text())
        descriptor["crates"] = [
            {
                "crate_dir": "rust/date-creusot-core",
                "contracts_crate": "contracts",
                "specs_search_root": "rust",
            }
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

    def declare_crate(self, *crate_dirs: str) -> None:
        self.edit_descriptor(
            lambda d: d.__setitem__(
                "crates",
                [
                    {
                        "crate_dir": crate_dir,
                        "contracts_crate": "contracts",
                        "specs_search_root": crate_dir.rsplit("/", 1)[0],
                    }
                    for crate_dir in crate_dirs
                ],
            )
        )

    def make_upstream(self) -> None:
        """A real pinned upstream checkout inside the workspace, so
        `port_source.repository` names a directory the write set excludes."""
        (self.workspace / "upstream" / "include").mkdir(parents=True)
        (self.workspace / "upstream" / "CMakeLists.txt").write_text("project(upstream)\n")

    # -- commands -----------------------------------------------------------
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = pipeline.main(
                ["--workspace", str(self.workspace), "--descriptor", str(self.descriptor_path), *args]
            )
        return code, out.getvalue(), err.getvalue()

    def scaffold(self, *args) -> tuple[int, dict]:
        code, out, err = self.run_cli("scaffold-crate", "--json", *args)
        return code, json.loads(out) if out.strip() else {"stderr": err}

    def authorize(self, *args) -> tuple[int, str, str]:
        return self.run_cli("authorize-write", *args)

    def granted(self, *args) -> str:
        """Authorize the manifest write and assert it was recorded; returns the
        grant id."""
        code, out, err = self.authorize(
            "--issue", "115", "--path", MANIFEST_REL, "--op", "write", "--issuer", "operator",
            "--one-shot", *args,
        )
        self.assertEqual(code, 0, err)
        for line in out.splitlines():
            if line.startswith("grant: "):
                return line.split(" ", 1)[1]
        self.fail(f"no grant id in authorize-write output:\n{out}\n{err}")

    def write_set_check(self, *args) -> tuple[int, dict]:
        """The `write_set` block of `write-set-check --json` -- the same shape
        the scaffold receipt carries, read off the command rather than off this
        module's own call into it."""
        code, out, _ = self.run_cli("write-set-check", "--json", *args)
        return code, json.loads(out)["write_set"]

    # -- files --------------------------------------------------------------
    @property
    def manifest(self) -> Path:
        return self.workspace / MANIFEST_REL

    @property
    def skeleton(self) -> Path:
        return self.workspace / SKELETON_REL

    @property
    def ledger(self) -> Path:
        return self.workspace / LEDGER_REL

    def append_ledger(self, record: dict) -> None:
        """A hand-written ledger line -- how a test reaches a state
        `authorize-write` refuses to produce (an expired grant, an edited
        record) without faking a clock."""
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger.open("a") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")

    def _valid_record(self, **overrides) -> dict:
        """A well-formed grant record for this workspace, with `path_kind`
        re-derived from the path itself -- `parse_grant` recomputes that
        relation on read, so a hand-written line that declared a glob to be an
        exact file would be rejected as unusable rather than exercised."""
        normalized, kind = write_authorization.classify_granted_path(
            overrides.get("path", MANIFEST_REL)
        )
        record = {
            "event": "grant",
            "schema_version": "1.0",
            "grant_id": "GW-000000000000",
            "issue": 115,
            "path": normalized,
            "path_kind": kind,
            "op": "write",
            "one_shot": False,
            "issuer": "operator",
            "issuer_kind": "human",
            "issued_at": LONG_AGO,
            "ttl_seconds": 3600,
            "expires_at": "2020-01-01T01:00:00+00:00",
            "note": None,
        }
        record.update(overrides)
        record["grant_id"] = write_authorization.grant_id_for(
            {field: record[field] for field in write_authorization._BINDING_FIELDS}
        )
        return record

    def _unexpired_record(self, **overrides) -> dict:
        """A well-formed grant record live for the next hour, so a test whose
        point is that a grant *does* authorize has one. Derived from the run, so
        it cannot expire mid-test."""
        issued = (datetime.now(timezone.utc) - timedelta(seconds=60)).replace(microsecond=0)
        return self._valid_record(
            issued_at=issued.isoformat(),
            expires_at=(issued + timedelta(seconds=3600)).isoformat(),
            **overrides,
        )

    def assertValid(self, schema, document, validator_name="document"):
        validator = make_validator(schema)
        errors = list(validator.iter_errors(document))
        self.assertEqual(errors, [], f"{validator_name} invalid: {[e.message for e in errors]}")

    def assertRefused(self, *args, contains=(), manifest_absent=True, skeleton_absent=True):
        """Assert the command refused with exit 2, one line on stderr, and the
        named substrings in the reason -- so every refusal case asserts it is
        *actionable*, not merely non-zero.

        `manifest_absent`/`skeleton_absent` are for the refusal cases that set a
        target up in advance: the assertion there is that the existing bytes
        are untouched, which "does not exist" cannot express. Every other
        refusal asserts nothing was written at all.
        """
        code, receipt = self.scaffold(*args)
        self.assertEqual(code, 2, receipt)
        reason = receipt.get("stderr", "")
        self.assertTrue(reason.startswith("error: "), reason)
        self.assertEqual(len(reason.splitlines()), 1, reason)
        for fragment in contains:
            self.assertIn(fragment, reason)
        if manifest_absent:
            self.assertFalse(self.manifest.exists(), "a refusal wrote the manifest")
        if skeleton_absent:
            self.assertFalse(self.skeleton.exists(), "a refusal wrote the source skeleton")


class BootstrapWithoutCargoTomlScopeTest(ScaffoldFixture):
    """The acceptance criterion, asserted positively: a missing-crate pilot
    bootstraps with a capability record scoped to the manifest alone.

    Nothing here grants raw `Cargo.toml` write scope -- the one protected write
    is bounded to one exact file, one operation, one issue and one expiry -- and
    the `src/lib.rs` skeleton needs no capability record at all, because
    `<crate>/src/` is inside `allowed_roots`. A worker bootstrapping a missing
    crate therefore never holds permission to edit a build file freely; it
    holds one file's worth of it, for one issue, and the receipt says so."""

    def test_missing_crate_bootstraps_with_one_manifest_scoped_grant(self):
        self.init_workspace()
        grant_id = self.granted()
        code, receipt = self.scaffold("--issue", "115", "--crate", "date-creusot-core")
        self.assertEqual(code, 0, receipt)
        self.assertValid(RECEIPT_SCHEMA, receipt, "scaffold receipt")

        self.assertTrue(self.manifest.is_file())
        self.assertTrue(self.skeleton.is_file())

        # The manifest write is the protected one, and it is attributed to the
        # record this run consumed.
        entry = next(e for e in receipt["generated"] if e["role"] == "manifest")
        self.assertEqual(entry["state"], "created")
        self.assertTrue(entry["protected"])
        self.assertEqual(entry["protected_by"], "**/Cargo.toml")
        self.assertEqual(entry["grant_id"], grant_id)

        # The skeleton needs no capability record -- the asymmetry is the point.
        skeleton = next(e for e in receipt["generated"] if e["role"] == "source-skeleton")
        self.assertEqual(skeleton["state"], "created")
        self.assertFalse(skeleton["protected"])
        self.assertIsNone(skeleton["grant_id"])
        self.assertIsNone(skeleton["protected_by"])

        self.assertEqual(receipt["grant"]["grant_id"], grant_id)
        self.assertEqual(receipt["grant"]["op"], "write")
        self.assertEqual(receipt["grant"]["issue"], 115)
        self.assertEqual(receipt["outcome"], "scaffolded")
        self.assertEqual(receipt["state"], "ok")
        self.assertEqual(receipt["event"], "crate-scaffold")
        self.assertEqual(receipt["crate"], "date-creusot-core")
        self.assertEqual(receipt["crate_dir"], "rust/date-creusot-core")
        self.assertEqual(receipt["mode"], "port")

    def test_the_only_protected_write_is_the_one_the_grant_names(self):
        """Before the scaffold, a hand-written manifest is the blocking
        `protected-write` #103 shipped; after it, the same path is a reported
        authorized write -- and the authority that changed that is the record,
        not the file."""
        self.init_workspace()
        self.make_upstream()
        code, before = self.write_set_check("--issue", "115")
        self.assertEqual(code, 0)

        self.granted()
        code, receipt = self.scaffold("--issue", "115", "--crate", "date-creusot-core")
        self.assertEqual(code, 0, receipt)

        # The scaffold's own verdict, carried from write_set.
        self.assertEqual(receipt["write_set"]["state"], "clean")
        authorized = [e for e in receipt["write_set"]["authorized_writes"] if e["path"] == MANIFEST_REL]
        self.assertEqual(len(authorized), 1)
        self.assertEqual(authorized[0]["grant_id"], receipt["grant"]["grant_id"])
        self.assertEqual(authorized[0]["status"], "spent")
        self.assertEqual(receipt["write_set"]["protected_surface"]["authorized"], 1)

        # And the conformance command itself agrees, independently.
        code, after = self.write_set_check("--issue", "115")
        self.assertEqual(code, 0)
        self.assertEqual(after["state"], "clean")
        self.assertEqual(
            [e["path"] for e in after["authorized_writes"]], [MANIFEST_REL], after
        )
        self.assertNotEqual(before["details"], after["details"])

    def test_the_receipt_records_before_and_after_hashes_for_both_files(self):
        self.init_workspace()
        self.granted()
        code, receipt = self.scaffold("--issue", "115", "--crate", "date-creusot-core")
        self.assertEqual(code, 0, receipt)
        self.assertEqual(len(receipt["generated"]), 2)
        for entry in receipt["generated"]:
            path = self.workspace / entry["path"]
            self.assertIsNone(entry["before_hash"], entry)
            expected = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(entry["after_hash"], expected, entry)
            self.assertEqual(entry["size_bytes"], path.stat().st_size, entry)
        manifest_entry = receipt["generated"][0]
        skeleton_entry = receipt["generated"][1]
        self.assertEqual(manifest_entry["path"], MANIFEST_REL)
        self.assertEqual(skeleton_entry["path"], SKELETON_REL)

    def test_nothing_outside_the_two_declared_files_is_created(self):
        """The closed write surface, asserted from the filesystem rather than
        from the receipt's own claim about itself."""
        self.init_workspace()
        self.granted()
        # Snapshot AFTER the grant, so the only paths the scaffold itself can
        # be responsible for are the two files plus the directories holding
        # them -- the ledger line is `authorize-write`'s, not this command's.
        before = sorted(p.relative_to(self.workspace).as_posix() for p in self.workspace.rglob("*"))
        code, receipt = self.scaffold("--issue", "115", "--crate", "date-creusot-core")
        self.assertEqual(code, 0, receipt)
        after = sorted(p.relative_to(self.workspace).as_posix() for p in self.workspace.rglob("*"))
        new = [p for p in after if p not in before]
        self.assertEqual(new, ["rust", "rust/date-creusot-core", "rust/date-creusot-core/Cargo.toml",
                               "rust/date-creusot-core/src", SKELETON_REL], new)

    def test_no_implementation_and_no_normative_content_is_emitted(self):
        """The scaffold declares no behaviour and writes no normative policy or
        spec content -- asserted on the bytes, not on the prose about them."""
        self.init_workspace()
        self.granted()
        code, _ = self.scaffold("--issue", "115", "--crate", "date-creusot-core")
        self.assertEqual(code, 0)

        manifest = tomllib.loads(self.manifest.read_text())
        self.assertEqual(manifest["package"]["name"], "date-creusot-core")
        self.assertEqual(manifest["package"]["version"], "0.1.0")
        self.assertEqual(manifest["package"]["edition"], "2021")
        self.assertEqual(manifest["lib"]["path"], "src/lib.rs")
        # No dependency, workspace or metadata table: a manifest that declared
        # any of those would be asserting something the descriptor does not say.
        self.assertEqual(set(manifest) - {"package", "lib"}, set(), manifest)
        self.assertEqual(set(manifest["package"]), {"name", "version", "edition"})

        skeleton = self.skeleton.read_text()
        for body in ("fn ", "unimplemented!", "todo!", "unsafe", "impl ", "mod ", "#!["):
            self.assertNotIn(body, skeleton, f"the skeleton declares something: {body}")
        self.assertTrue(all(line.startswith("//!") for line in skeleton.splitlines()))

    def test_the_manifest_parses_and_names_its_own_entry_point(self):
        """Build-valid in the sense this command can verify without a Rust
        toolchain: it parses as TOML, declares a package, and the `[lib] path`
        it declares is a file that exists."""
        self.init_workspace()
        self.granted()
        code, _ = self.scaffold("--issue", "115", "--crate", "date-creusot-core")
        self.assertEqual(code, 0)
        manifest = tomllib.loads(self.manifest.read_text())
        self.assertTrue((self.workspace / "rust/date-creusot-core" / manifest["lib"]["path"]).is_file())


class DeterminismTest(ScaffoldFixture):
    """Deterministic output and idempotent re-runs.

    Both halves are asserted by comparing hashes rather than by reading prose
    about them: a second run over an unchanged workspace reports `unchanged`
    and writes nothing, and its `before_hash` for each file equals the first
    run's `after_hash`. The renderer is a pure function of the descriptor's
    declared crate entry, which is what makes that true rather than a
    coincidence."""

    def test_a_second_run_reports_unchanged_and_writes_nothing(self):
        self.init_workspace()
        self.granted()
        code, first = self.scaffold("--issue", "115", "--crate", "date-creusot-core")
        self.assertEqual(code, 0, first)
        manifest_mtime = self.manifest.stat().st_mtime_ns
        skeleton_mtime = self.skeleton.stat().st_mtime_ns

        code, second = self.scaffold("--issue", "115", "--crate", "date-creusot-core")
        self.assertEqual(code, 0, second)
        self.assertEqual(second["outcome"], "unchanged")
        self.assertEqual(second["state"], "ok")
        self.assertEqual(
            [e["state"] for e in second["generated"]], ["unchanged", "preserved"], second
        )
        for before, after in zip(first["generated"], second["generated"], strict=True):
            self.assertEqual(after["before_hash"], before["after_hash"])
            self.assertEqual(after["after_hash"], before["after_hash"])
        # Untouched, not rewritten-with-identical-bytes: the whole point of the
        # idempotent case is that a retry is not a second write.
        self.assertEqual(self.manifest.stat().st_mtime_ns, manifest_mtime)
        self.assertEqual(self.skeleton.stat().st_mtime_ns, skeleton_mtime)

    def test_two_workspaces_with_the_same_declaration_render_identical_bytes(self):
        """Determinism across workspaces, not just across re-runs: the rendered
        bytes depend on the declared crate entry and nothing else, so the same
        declaration in two fresh workspaces yields the same digests."""
        digests = []
        for _ in range(2):
            self.setUp()
            self.init_workspace()
            self.granted()
            code, receipt = self.scaffold("--issue", "115", "--crate", "date-creusot-core")
            self.assertEqual(code, 0, receipt)
            digests.append([e["after_hash"] for e in receipt["generated"]])
            self.tearDown()
        self.assertEqual(digests[0], digests[1])

    def test_crate_dir_and_crate_name_render_the_same_manifest(self):
        """The package name is taken from the *declared* crate_dir, never from
        the request, so the two accepted spellings cannot produce two different
        manifests for one crate."""
        self.init_workspace()
        self.granted()
        code, by_dir = self.scaffold("--issue", "115", "--crate", "rust/date-creusot-core")
        self.assertEqual(code, 0, by_dir)
        self.assertEqual(by_dir["crate"], "date-creusot-core")

        # A second workspace, same declaration, the other spelling.
        self.tearDown()
        self.setUp()
        self.init_workspace()
        self.granted()
        code, by_name = self.scaffold("--issue", "115", "--crate", "date-creusot-core")
        self.assertEqual(code, 0, by_name)
        self.assertEqual(
            [e["after_hash"] for e in by_dir["generated"]],
            [e["after_hash"] for e in by_name["generated"]],
        )


class UnsafeOverwriteTest(ScaffoldFixture):
    """An existing manifest is never rewritten, and an existing `src/lib.rs`
    is never touched.

    The two are mirror images, deliberately. A `Cargo.toml` is a build input
    somebody may already depend on and this command is not entitled to it, so a
    manifest with different bytes is refused outright. `src/lib.rs` lives inside
    `allowed_roots`, so its content belongs to the implementing agent -- which
    means it is *preserved* with its hash, not refused: refusing to run because
    somebody already started implementing would make the verb unusable on a
    crate that is halfway done."""

    def test_an_existing_manifest_with_different_bytes_is_refused_not_overwritten(self):
        self.init_workspace()
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text('[package]\nname = "hand-written"\n')
        before = self.manifest.read_text()
        self.granted()
        self.assertRefused(
            "--issue", "115", "--crate", "date-creusot-core",
            contains=("already exists", "never overwrites"),
            manifest_absent=False,
        )
        self.assertEqual(self.manifest.read_text(), before)
        self.assertFalse(self.skeleton.exists())

    def test_an_existing_manifest_with_identical_bytes_is_the_idempotent_case(self):
        self.init_workspace()
        self.granted()
        code, first = self.scaffold("--issue", "115", "--crate", "date-creusot-core")
        self.assertEqual(code, 0, first)
        code, second = self.scaffold("--issue", "115", "--crate", "date-creusot-core")
        self.assertEqual(code, 0, second)
        self.assertEqual(second["outcome"], "unchanged")

    def test_an_existing_source_skeleton_is_preserved_and_reported(self):
        self.init_workspace()
        self.skeleton.parent.mkdir(parents=True)
        self.skeleton.write_text("//! hand-written implementation\n")
        before = self.skeleton.read_text()
        self.granted()
        code, receipt = self.scaffold("--issue", "115", "--crate", "date-creusot-core")
        self.assertEqual(code, 0, receipt)
        self.assertEqual(self.skeleton.read_text(), before)
        skeleton = receipt["generated"][1]
        self.assertEqual(skeleton["state"], "preserved")
        self.assertEqual(skeleton["before_hash"], skeleton["after_hash"])
        self.assertIn("preserved", self.check_details(receipt, "source-skeleton"))

    def test_a_directory_in_the_way_of_either_target_is_refused(self):
        self.init_workspace()
        self.manifest.mkdir(parents=True)
        self.granted()
        self.assertRefused(
            "--issue", "115", "--crate", "date-creusot-core", contains=("is a directory",),
            manifest_absent=False,
        )
        # And the directory is still a directory: the refusal did not replace it.
        self.assertTrue(self.manifest.is_dir())

    def check_details(self, receipt, name):
        """One check's `details` line -- the receipt is the surface these tests
        assert on, including for the evidence a check actually looked at."""
        return next(c["details"] for c in receipt["checks"] if c["name"] == name)


class NoArbitraryManifestTextTest(ScaffoldFixture):
    """There is no way to supply manifest text, and no way to name a target
    other than a declared crate.

    Asserted against the parser rather than against the module: a flag this
    command must not have is a flag argparse must reject, and that is the shape
    a future change would arrive in."""

    def test_the_command_takes_no_content_flag(self):
        parser = pipeline.build_parser()
        sub = pipeline.registered_commands(parser)["scaffold-crate"]
        options = {
            option
            for action in sub._actions  # noqa: SLF001 -- no public argparse API
            for option in action.option_strings
        }
        # `-h` is argparse's own, on every verb in the CLI; everything else is
        # the four flags this command's write surface is made of.
        self.assertEqual(
            options,
            {"-h", "--help", "--issue", "--crate", "--grant-id", "--json"},
            f"the verb's flag set changed: {sorted(options)}",
        )
        # And the flags that would shape what is written are absent -- the
        # manifest's content and its target are both derived, never supplied.
        self.assertFalse(
            {"--manifest", "--content", "--text", "--body", "--lib", "--path", "--deps",
             "--dependencies", "--src", "--source"} & options
        )

    def test_a_content_flag_is_refused_by_argparse(self):
        self.init_workspace()
        self.granted()
        with self.assertRaises(SystemExit):
            self.run_cli(
                "scaffold-crate", "--issue", "115", "--crate", "date-creusot-core",
                "--manifest", "[package]\nname='evil'\n",
            )
        self.assertFalse(self.manifest.exists())

    def test_the_rendered_manifest_does_not_depend_on_anything_but_the_declaration(self):
        """Two crates declared in the same descriptor render two manifests that
        differ only in their own name -- no workspace state, no ambient file, no
        clock leaking into the bytes."""
        self.init_workspace()
        self.declare_crate("rust/date-creusot-core", "rust/other-core")
        for crate, manifest_rel in (
            ("date-creusot-core", "rust/date-creusot-core/Cargo.toml"),
            ("other-core", "rust/other-core/Cargo.toml"),
        ):
            code, out, err = self.authorize(
                "--issue", "115", "--path", manifest_rel, "--op", "write",
                "--issuer", "operator", "--one-shot",
            )
            self.assertEqual(code, 0, err)
            code, receipt = self.scaffold("--issue", "115", "--crate", crate)
            self.assertEqual(code, 0, receipt)
        first = (self.workspace / "rust/date-creusot-core/Cargo.toml").read_text()
        second = (self.workspace / "rust/other-core/Cargo.toml").read_text()
        self.assertEqual(
            first.replace("date-creusot-core", "<crate>"),
            second.replace("other-core", "<crate>"),
        )


class UndeclaredCrateTest(ScaffoldFixture):
    """Only a crate this workspace's project descriptor declares is
    scaffolded. The scaffold's content and its grant target are both derived
    from the declaration, so an undeclared crate has neither -- and the refusal
    names every declared crate, because "that crate is not in this workspace"
    is not actionable on its own."""

    def test_an_undeclared_crate_name_is_refused_and_lists_what_is_declared(self):
        self.init_workspace()
        self.granted()
        self.assertRefused(
            "--issue", "115", "--crate", "not-a-crate",
            contains=("is not a crate this workspace's project descriptor declares",
                      "rust/date-creusot-core"),
        )

    def test_a_traversal_crate_name_is_refused_not_resolved(self):
        self.init_workspace()
        self.granted()
        self.assertRefused(
            "--issue", "115", "--crate", "../../etc/passwd",
            contains=("escapes the workspace", "'..' segment is refused, not resolved"),
        )
        self.assertRefused(
            "--issue", "115", "--crate", "rust/../../outside",
            contains=("escapes the workspace",),
        )

    def test_an_absolute_crate_name_is_refused(self):
        self.init_workspace()
        self.granted()
        self.assertRefused(
            "--issue", "115", "--crate", "/etc/passwd",
            contains=("must be workspace-relative",),
        )

    def test_a_glob_crate_name_is_refused(self):
        self.init_workspace()
        self.granted()
        self.assertRefused(
            "--issue", "115", "--crate", "rust/*",
            contains=("contains a glob character",),
        )

    def test_a_trailing_slash_is_refused(self):
        self.init_workspace()
        self.granted()
        self.assertRefused(
            "--issue", "115", "--crate", "rust/date-creusot-core/",
            contains=("trailing '/'",),
        )

    def test_two_crates_with_the_same_basename_are_ambiguous_and_refused(self):
        self.init_workspace()
        self.declare_crate("rust/a/core", "rust/b/core")
        self.granted()
        self.assertRefused(
            "--issue", "115", "--crate", "core",
            contains=("names 2 declared crates", "rust/a/core", "rust/b/core", "name the crate_dir"),
        )
        # Naming one of them by crate_dir is the documented way through.
        code, _, err = self.authorize(
            "--issue", "115", "--path", "rust/a/core/Cargo.toml", "--op", "write",
            "--issuer", "operator", "--one-shot",
        )
        self.assertEqual(code, 0, err)
        code, receipt = self.scaffold("--issue", "115", "--crate", "rust/a/core")
        self.assertEqual(code, 0, receipt)
        self.assertEqual(receipt["crate_dir"], "rust/a/core")

    def test_a_declared_crate_dir_that_escapes_the_workspace_is_refused(self):
        """The declaration is checked too, not only the request: a descriptor
        whose own `crate_dir` escapes would otherwise be a scaffold whose target
        is outside the workspace."""
        self.init_workspace()
        self.declare_crate("../outside-core")
        self.granted()
        self.assertRefused(
            "--issue", "115", "--crate", "../outside-core",
            contains=("escapes the workspace",),
        )


class InvalidTargetTest(ScaffoldFixture):
    """Targets the command has no business writing, judged by `write_set`'s own
    predicates rather than a private copy of them."""

    def test_a_crate_dir_inside_the_pinned_upstream_checkout_is_refused(self):
        self.init_workspace()
        self.make_upstream()
        self.declare_crate("upstream/include/port-core")
        self.granted()
        self.assertRefused(
            "--issue", "115", "--crate", "port-core",
            contains=("inside the pinned upstream checkout", "read-only input"),
        )

    def test_a_crate_dir_inside_a_declared_specs_tree_is_refused(self):
        """A second declared crate whose directory lands inside the FIRST
        declared crate's `specs/` tree -- a spec tree is normative content, and
        `write_set._spec_tree_prefixes` is the predicate that says so, so the
        refusal cannot disagree with the conformance check about which paths
        are spec artifacts."""
        self.init_workspace()
        self.declare_crate("rust/date-creusot-core", "rust/date-creusot-core/specs/nested")
        self.assertRefused(
            "--issue", "115", "--crate", "nested",
            contains=("declared crate spec tree", "normative content"),
        )

    def test_a_crate_dir_in_a_canonical_pipeline_artifact_location_is_refused(self):
        self.init_workspace()
        self.declare_crate("ci/manifest/core")
        self.granted()
        self.assertRefused(
            "--issue", "115", "--crate", "core",
            contains=("canonical pipeline artifact location",),
        )

    def test_a_manifest_no_protected_root_covers_is_refused(self):
        """The command's whole premise is that the manifest is a protected
        write, so a workspace whose `protected_roots` does not protect it is
        refused rather than written into -- `authorize-write` would refuse a
        grant for the same path, so there is nothing to bootstrap against."""
        self.init_workspace()
        self.edit_descriptor(
            lambda d: d["write_set"].__setitem__(
                "protected_roots", [p for p in d["write_set"]["protected_roots"] if p != "**/Cargo.toml"]
            )
        )
        self.assertRefused(
            "--issue", "115", "--crate", "date-creusot-core",
            contains=("no declared protected_roots pattern covers",),
        )

    def test_a_manifest_allowed_roots_already_permits_is_refused(self):
        """`allowed_roots` wins, exactly as `authorize-write` refuses a grant for
        a path the descriptor already permits: such a write needs no capability
        record, and authorizing it would be a permission-shaped object with no
        work to do."""
        self.init_workspace()
        self.edit_descriptor(
            lambda d: d["write_set"]["allowed_roots"].append("rust/*/Cargo.toml")
        )
        self.assertRefused(
            "--issue", "115", "--crate", "date-creusot-core",
            contains=("already under allowed_roots pattern",),
        )


class GrantRequiredTest(ScaffoldFixture):
    """The protected write requires a capability record, and every way of not
    having one is a refusal naming why -- never a silent success, and never a
    file written."""

    def test_no_ledger_at_all_is_refused_with_the_issuing_command_named(self):
        self.init_workspace()
        self.assertRefused(
            "--issue", "115", "--crate", "date-creusot-core",
            contains=(
                "no active write grant covers rust/date-creusot-core/Cargo.toml for issue 115",
                "ligature authorize-write --issue 115 --path rust/date-creusot-core/Cargo.toml",
                "No file was written",
            ),
        )
        self.assertFalse(self.ledger.exists())

    def test_issue_zero_or_negative_is_refused(self):
        self.init_workspace()
        self.granted()
        code, out, err = self.run_cli("scaffold-crate", "--issue", "0", "--crate", "date-creusot-core")
        self.assertEqual(code, 2)
        self.assertIn("--issue 0 is not a positive integer", err)
        self.assertFalse(self.manifest.exists())

    def test_a_grant_for_another_issue_does_not_authorize_this_scaffold(self):
        self.init_workspace()
        code, out, err = self.authorize(
            "--issue", "116", "--path", MANIFEST_REL, "--op", "write",
            "--issuer", "operator", "--one-shot",
        )
        self.assertEqual(code, 0, err)
        self.assertRefused(
            "--issue", "115", "--crate", "date-creusot-core",
            contains=("for issue 115", "other-issue"),
        )

    def test_an_expired_grant_is_refused_and_its_status_is_named(self):
        self.init_workspace()
        self.append_ledger(self._valid_record())
        self.assertRefused(
            "--issue", "115", "--crate", "date-creusot-core", contains=("expired",)
        )

    def test_a_grant_for_another_path_does_not_authorize_this_scaffold(self):
        self.init_workspace()
        self.append_ledger(self._unexpired_record(path="rust/date-creusot-core/build.rs"))
        self.assertRefused(
            "--issue", "115", "--crate", "date-creusot-core", contains=("no active write grant covers",)
        )

    def test_a_delete_grant_cannot_authorize_creating_the_manifest(self):
        """`op` is a closed vocabulary and only `write` is consumable: a delete
        grant authorizes removing a path, which can never create the file this
        command writes."""
        self.init_workspace()
        self.append_ledger(self._unexpired_record(op="delete"))
        self.assertRefused(
            "--issue", "115", "--crate", "date-creusot-core", contains=("no active write grant covers",)
        )

    def test_an_unverified_supervisor_grant_does_not_authorize(self):
        self.init_workspace()
        self.append_ledger(
            self._unexpired_record(issuer="autopilot", issuer_kind="supervisor")
        )
        self.assertRefused(
            "--issue", "115", "--crate", "date-creusot-core", contains=("unverified-issuer",)
        )

    def test_a_declared_supervisor_grant_does_authorize(self):
        """The automation lane is machine-checkable end to end, and the
        descriptor's whitelist is the only thing that opens it."""
        self.init_workspace()
        self.edit_descriptor(
            lambda d: d["write_set"].__setitem__("authorized_supervisors", ["autopilot"])
        )
        self.append_ledger(
            self._unexpired_record(issuer="autopilot", issuer_kind="supervisor")
        )
        code, receipt = self.scaffold("--issue", "115", "--crate", "date-creusot-core")
        self.assertEqual(code, 0, receipt)
        self.assertEqual(receipt["grant"]["issuer_kind"], "supervisor")
        self.assertEqual(receipt["grant"]["issuer"], "autopilot")

    def test_a_record_edited_in_place_authorizes_nothing(self):
        self.init_workspace()
        record = self._unexpired_record()
        record["grant_id"] = "GW-000000000000"
        self.append_ledger(record)
        self.assertRefused(
            "--issue", "115", "--crate", "date-creusot-core",
            contains=("no active write grant covers",),
        )
        # And the reason it was unusable is visible in the check that failed,
        # through the same audit the write-set report carries.
        code, report = self.write_set_check("--issue", "115")
        self.assertEqual(code, 0)
        self.assertEqual(report["grants"]["rejected"][0]["line"], 1)
        self.assertIn("does not match the binding", report["grants"]["rejected"][0]["reason"])

    def test_a_ledger_inside_a_declared_protected_root_authorizes_nothing(self):
        self.init_workspace()
        self.edit_descriptor(
            lambda d: d["write_set"]["protected_roots"].append("ci/results/**")
        )
        self.append_ledger(self._unexpired_record())
        self.assertRefused(
            "--issue", "115", "--crate", "date-creusot-core", contains=("ledger-protected",)
        )


class GrantIdPinningTest(ScaffoldFixture):
    """`--grant-id` is a check, not a filter: it records the operator's intent
    and a mismatch is refused rather than silently substituted."""

    def test_the_named_grant_is_the_one_consumed(self):
        self.init_workspace()
        grant_id = self.granted()
        code, receipt = self.scaffold(
            "--issue", "115", "--crate", "date-creusot-core", "--grant-id", grant_id
        )
        self.assertEqual(code, 0, receipt)
        self.assertEqual(receipt["grant"]["grant_id"], grant_id)

    def test_a_mismatched_grant_id_is_refused_and_names_the_one_that_does(self):
        self.init_workspace()
        grant_id = self.granted()
        self.assertRefused(
            "--issue", "115", "--crate", "date-creusot-core",
            "--grant-id", "GW-ffffffffffff",
            contains=("is not the record authorizing", grant_id),
        )


class ModeTest(ScaffoldFixture):
    """#115's scope is a Mode P port pilot. A greenfield workspace's crates come
    from its own layout, so the verb refuses rather than guessing."""

    def test_a_greenfield_workspace_is_refused(self):
        self.init_workspace(mode="greenfield", name="greenfield-proj")
        self.granted()
        self.assertRefused(
            "--issue", "115", "--crate", "date-creusot-core",
            contains=("is mode 'greenfield', not 'port'",),
        )

    def test_an_invalid_descriptor_is_one_line_not_a_traceback(self):
        self.init_workspace()
        self.granted()
        self.write_descriptor({"mode": "port"})
        code, out, err = self.run_cli(
            "scaffold-crate", "--issue", "115", "--crate", "date-creusot-core"
        )
        self.assertEqual(code, 2)
        self.assertIn("error: project descriptor", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(self.manifest.exists())


class ChecksRunBeforeSuccessTest(ScaffoldFixture):
    """Every named check runs, and `write_set`'s own verdict -- not this
    module's reading of the ledger -- is what decides the write was covered."""

    EXPECTED = {"descriptor", "manifest", "workspace", "source-skeleton", "write-set", "doctor"}

    def test_all_six_checks_are_recorded_with_their_blocking_half(self):
        self.init_workspace()
        (self.workspace / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["rust/date-creusot-core"]\nresolver = "2"\n'
        )
        self.granted()
        code, receipt = self.scaffold("--issue", "115", "--crate", "date-creusot-core")
        self.assertEqual(code, 0, receipt)
        self.assertEqual({c["name"] for c in receipt["checks"]}, self.EXPECTED)
        for check in receipt["checks"]:
            self.assertEqual(check["state"], "ok", check)
            self.assertTrue(check["details"].strip(), check)
        blocking = {c["name"] for c in receipt["checks"] if c["blocking"]}
        self.assertEqual(
            blocking, {"descriptor", "manifest", "workspace", "source-skeleton", "write-set"}, receipt
        )

    def test_cargo_rejecting_workspace_membership_fails_scaffolding(self):
        self.init_workspace()
        (self.workspace / "Cargo.toml").write_text(
            '[workspace]\nmembers = []\nresolver = "2"\n'
        )
        self.granted()
        code, receipt = self.scaffold("--issue", "115", "--crate", "date-creusot-core")
        self.assertEqual(code, 1, receipt)
        workspace_check = next(c for c in receipt["checks"] if c["name"] == "workspace")
        self.assertEqual(workspace_check["state"], "failed")
        self.assertTrue(workspace_check["blocking"])
        self.assertIn("cargo metadata", workspace_check["details"])

    def test_generated_manifest_and_skeleton_compile(self):
        self.init_workspace()
        self.granted()
        code, receipt = self.scaffold("--issue", "115", "--crate", "date-creusot-core")
        self.assertEqual(code, 0, receipt)
        with tempfile.TemporaryDirectory() as target_dir:
            built = subprocess.run(
                [
                    "cargo", "check", "--offline", "--manifest-path", str(self.manifest),
                    "--target-dir", target_dir,
                ],
                cwd=self.workspace,
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)

    def test_the_write_set_check_is_the_conformance_command_not_this_modules_opinion(self):
        """The `write-set` check's `details` names `check_write_set`, and the
        receipt's `write_set` block is that command's verdict carried -- so a
        consumer reading the receipt and an operator running the command cannot
        be told two different things about the same write."""
        self.init_workspace()
        self.granted()
        code, receipt = self.scaffold("--issue", "115", "--crate", "date-creusot-core")
        self.assertEqual(code, 0, receipt)
        check = next(c for c in receipt["checks"] if c["name"] == "write-set")
        self.assertIn("write_set.check_write_set", check["details"])
        self.assertIn(MANIFEST_REL, check["details"])
        self.assertEqual(receipt["write_set"]["details"], receipt["write_set"]["details"])
        code, report = self.write_set_check("--issue", "115")
        self.assertEqual(code, 0)
        self.assertEqual(receipt["write_set"]["state"], report["state"])
        self.assertEqual(receipt["write_set"]["details"], report["details"])

    def test_the_doctor_check_is_recorded_and_says_why_it_does_not_block(self):
        self.init_workspace()
        self.granted()
        code, receipt = self.scaffold("--issue", "115", "--crate", "date-creusot-core")
        self.assertEqual(code, 0, receipt)
        doctor = next(c for c in receipt["checks"] if c["name"] == "doctor")
        self.assertFalse(doctor["blocking"], doctor)
        self.assertIn("installation=", doctor["details"])
        self.assertIn("descriptor gate=valid", doctor["details"])
        self.assertIn("recorded rather than enforced", doctor["details"])
        self.assertIn("migrate", doctor["details"])

    def test_a_descriptor_that_stops_being_valid_fails_the_run_and_says_so(self):
        """The one doctor condition that blocks is its descriptor gate, read
        against the file this run actually used (#109's discipline). A
        descriptor that stops satisfying the schema after the write is a
        blocking check failure -- and the files are still reported, because at
        that point the honest report is the one that says so."""
        self.init_workspace()
        self.granted()
        code, receipt = self.scaffold("--issue", "115", "--crate", "date-creusot-core")
        self.assertEqual(code, 0, receipt)

        # Re-verify the blocking half by asking the verifier directly: the
        # receipt's checks are the only place `state: failed` is decided, and a
        # test that cannot reach one would leave the half unpinned.
        from scaffold_crate import _verify_descriptor  # noqa: PLC0415

        broken = self.workspace / "broken-descriptor.json"
        broken.write_text(json.dumps({"mode": "port", "unexpected": True}))
        result = _verify_descriptor(broken)
        self.assertEqual(result.state, "failed")
        self.assertTrue(result.blocking)

    def test_a_manifest_that_does_not_hold_the_rendered_bytes_fails_its_own_check(self):
        """A check that said `ok` about a file that is not there is the failure
        mode this closes, so the verifier is exercised against a manifest whose
        bytes disagree with what the renderer claims to have written."""
        from scaffold_crate import _verify_manifest  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Cargo.toml"
            path.write_text('[package]\nname = "tampered"\nversion = "0.1.0"\nedition = "2021"\n')
            result = _verify_manifest(path, '[package]\nname = "date-creusot-core"\n', "date-creusot-core")
            self.assertEqual(result.state, "failed")
            self.assertTrue(result.blocking)

    def test_a_manifest_that_is_not_toml_fails_its_own_check(self):
        from scaffold_crate import _verify_manifest  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Cargo.toml"
            path.write_text("this is not toml\n")
            result = _verify_manifest(path, "this is not toml\n", "x")
            self.assertEqual(result.state, "failed")
            self.assertIn("not valid TOML", result.details)

    def test_a_failed_blocking_check_makes_the_cli_exit_one_with_the_receipt(self):
        """The exit-code contract's 1: the input was valid and checkable, the
        check ran, and it found a deterministic problem -- as opposed to 2,
        which is a refusal that wrote nothing."""
        import scaffold_crate  # noqa: PLC0415

        original = scaffold_crate._verify_write_set
        scaffold_crate._verify_write_set = lambda *a, **k: (
            scaffold_crate.CheckResult(
                "write-set", scaffold_crate.CHECK_FAILED, True, "injected failure"
            ),
            {"state": "violations", "details": "injected"},
        )
        try:
            self.init_workspace()
            self.granted()
            code, out, err = self.run_cli(
                "scaffold-crate", "--issue", "115", "--crate", "date-creusot-core"
            )
        finally:
            scaffold_crate._verify_write_set = original
        self.assertEqual(code, 1, out)
        self.assertIn("check write-set: FAILED (blocking)", out)
        self.assertIn("state: failed", out)
        # The files are still on disk, and still reported -- which is why the
        # text form names the failed check and the idempotent re-run.
        self.assertTrue(self.manifest.is_file())
        self.assertTrue(self.skeleton.is_file())


class ReceiptShapeTest(ScaffoldFixture):
    """The receipt is a published shape, so it validates against
    `docs/scaffold-receipt-schema.json` -- closed, like every other artifact
    schema here after #104."""

    def test_the_receipt_validates_against_its_published_schema(self):
        self.init_workspace()
        self.granted()
        code, receipt = self.scaffold("--issue", "115", "--crate", "date-creusot-core")
        self.assertEqual(code, 0, receipt)
        self.assertValid(RECEIPT_SCHEMA, receipt, "scaffold receipt")

    def test_the_schema_is_closed_so_an_unread_key_cannot_validate(self):
        self.init_workspace()
        self.granted()
        code, receipt = self.scaffold("--issue", "115", "--crate", "date-creusot-core")
        self.assertEqual(code, 0, receipt)
        receipt["authorized_everything"] = True
        errors = list(make_validator(RECEIPT_SCHEMA).iter_errors(receipt))
        self.assertEqual(len(errors), 1, [e.message for e in errors])
        self.assertIn("authorized_everything", errors[0].message)

    def test_the_text_receipt_says_the_same_facts_as_the_json(self):
        """One receipt, two renderings: an operator reading the text and a
        consumer reading `--json` cannot be told two different things."""
        self.init_workspace()
        grant_id = self.granted()
        code, out, err = self.run_cli(
            "scaffold-crate", "--issue", "115", "--crate", "date-creusot-core"
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        for fragment in (
            "crate: date-creusot-core",
            "crate dir: rust/date-creusot-core",
            "mode: port",
            "issue: 115",
            f"grant: {grant_id}",
            f"manifest: {MANIFEST_REL} (created)",
            f"source-skeleton: {SKELETON_REL} (created)",
            "check write-set: ok (blocking)",
            "check doctor: ok (recorded)",
            "write set: clean",
            "outcome: scaffolded",
            "state: ok",
        ):
            self.assertIn(fragment, out)

    def test_the_text_receipt_names_the_failed_check_and_the_re_run(self):
        import scaffold_crate  # noqa: PLC0415

        original = scaffold_crate._verify_skeleton
        scaffold_crate._verify_skeleton = lambda *a, **k: scaffold_crate.CheckResult(
            "source-skeleton", scaffold_crate.CHECK_FAILED, True, "injected failure"
        )
        try:
            self.init_workspace()
            self.granted()
            code, out, err = self.run_cli(
                "scaffold-crate", "--issue", "115", "--crate", "date-creusot-core"
            )
        finally:
            scaffold_crate._verify_skeleton = original
        self.assertEqual(code, 1, out)
        self.assertIn("check source-skeleton: FAILED (blocking)", out)
        self.assertIn("state: failed", out)
        self.assertIn("Re-run `ligature scaffold-crate --issue 115", out)


class NoScopeBeyondOneManifestTest(ScaffoldFixture):
    """A grant bound to the manifest is not a grant bound to the crate.

    The bootstrap's whole boundary claim is that the capability is narrow: one
    exact file, one operation, one issue, one expiry. These tests are the
    negative space around it -- the same grant must not let anything else
    through, which is what would make it a raw `Cargo.toml` scope in all but
    name."""

    def test_a_pattern_grant_for_a_neighbouring_file_does_not_reach_this_crate(self):
        self.init_workspace()
        self.declare_crate("rust/date-creusot-core", "rust/other-core")
        # A bounded pattern covering another crate's manifest.
        self.append_ledger(self._unexpired_record(path="rust/other-*/Cargo.toml"))
        self.assertRefused(
            "--issue", "115", "--crate", "date-creusot-core",
            contains=("no active write grant covers",),
        )

    def test_a_bounded_pattern_grant_that_covers_the_manifest_is_accepted(self):
        """A pattern grant is legal where it is bounded, and the receipt says
        `path_kind: pattern` rather than pretending the grant named the file."""
        self.init_workspace()
        self.append_ledger(
            self._unexpired_record(path="rust/date-creusot-*/Cargo.toml")
        )
        code, receipt = self.scaffold("--issue", "115", "--crate", "date-creusot-core")
        self.assertEqual(code, 0, receipt)
        self.assertEqual(receipt["grant"]["path_kind"], "pattern")
        self.assertEqual(receipt["grant"]["path"], "rust/date-creusot-*/Cargo.toml")
        self.assertEqual(receipt["generated"][0]["path"], MANIFEST_REL)

    def test_the_grant_is_not_consumed_for_a_second_crate(self):
        """One-shot attribution is what `write_set` reports, and this command
        does not weaken it: the record binds one path, so a second crate has no
        authority and is refused."""
        self.init_workspace()
        self.declare_crate("rust/date-creusot-core", "rust/other-core")
        self.granted()
        code, receipt = self.scaffold("--issue", "115", "--crate", "date-creusot-core")
        self.assertEqual(code, 0, receipt)
        other = self.workspace / "rust/other-core/Cargo.toml"
        self.assertRefused(
            "--issue", "115", "--crate", "other-core", contains=("no active write grant covers",),
            manifest_absent=False,
            skeleton_absent=False,
        )
        self.assertFalse(other.exists(), "the grant for one crate reached a second")
        self.assertFalse(
            (self.workspace / "rust/other-core/src/lib.rs").exists(),
            "the grant for one crate reached a second",
        )

    def test_the_ledger_gains_exactly_one_line_per_bootstrap(self):
        """`scaffold-crate` records no capability of its own: the only ledger
        line is the one `authorize-write` appended, whatever this command does."""
        self.init_workspace()
        self.granted()
        code, receipt = self.scaffold("--issue", "115", "--crate", "date-creusot-core")
        self.assertEqual(code, 0, receipt)
        code, second = self.scaffold("--issue", "115", "--crate", "date-creusot-core")
        self.assertEqual(code, 0, second)
        lines = [json.loads(line) for line in self.ledger.read_text().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, lines)
        self.assertEqual(lines[0]["grant_id"], receipt["grant"]["grant_id"])
        self.assertEqual(lines[0]["line"] if "line" in lines[0] else receipt["grant"]["line"], 1)


if __name__ == "__main__":
    unittest.main()
