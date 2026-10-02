"""Regression tests for schemas/project-descriptor.schema.json (plan.md §1.1).

Per plan.md §12 (G1a) and the epic's own hard rule: canonical examples must
pass their schema in CI, and hand-written examples are untrustworthy against
additionalProperties: false unless actually checked.
"""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from project_descriptor import ProjectDescriptorError  # noqa: E402
from project_descriptor import _schema_property_paths  # noqa: E402
from project_descriptor import load_project_descriptor  # noqa: E402
from project_descriptor import read_descriptor_text  # noqa: E402
from project_descriptor import schema_diagnostics  # noqa: E402
from schema_utils import make_validator  # noqa: E402

SCHEMA_PATH = ROOT / "schemas" / "project-descriptor.schema.json"
EXAMPLES_DIR = ROOT / "schemas" / "examples"


class ProjectDescriptorSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA_PATH.read_text())
        cls.validator = make_validator(cls.schema)

    def test_greenfield_example_is_valid(self):
        instance = json.loads(
            (EXAMPLES_DIR / "project-descriptor.greenfield.example.json").read_text()
        )
        errors = list(self.validator.iter_errors(instance))
        self.assertEqual(errors, [], [e.message for e in errors])

    def test_port_example_is_valid(self):
        instance = json.loads(
            (EXAMPLES_DIR / "project-descriptor.port.example.json").read_text()
        )
        errors = list(self.validator.iter_errors(instance))
        self.assertEqual(errors, [], [e.message for e in errors])

    def test_rejects_unknown_top_level_field(self):
        instance = json.loads(
            (EXAMPLES_DIR / "project-descriptor.greenfield.example.json").read_text()
        )
        instance["unexpected_field"] = "should be rejected"
        errors = list(self.validator.iter_errors(instance))
        self.assertTrue(errors, "schema accepted an undeclared top-level field")

    def test_rejects_missing_required_field(self):
        instance = json.loads(
            (EXAMPLES_DIR / "project-descriptor.greenfield.example.json").read_text()
        )
        del instance["write_set"]
        errors = list(self.validator.iter_errors(instance))
        self.assertTrue(errors, "schema accepted a missing required field")

    def test_port_mode_requires_port_source(self):
        instance = json.loads(
            (EXAMPLES_DIR / "project-descriptor.greenfield.example.json").read_text()
        )
        instance["mode"] = "port"
        errors = list(self.validator.iter_errors(instance))
        self.assertTrue(
            errors, "mode: port validated without port_source (if/then not enforced)"
        )

    def test_greenfield_mode_does_not_require_port_source(self):
        instance = json.loads(
            (EXAMPLES_DIR / "project-descriptor.greenfield.example.json").read_text()
        )
        self.assertEqual(instance["mode"], "greenfield")
        self.assertNotIn("port_source", instance)
        errors = list(self.validator.iter_errors(instance))
        self.assertEqual(errors, [])

    def test_empty_reviewer_is_rejected(self):
        """Review finding (round 2, medium severity): reviewer had no
        minLength, so review: {reviewer: "", reviewed_at: ...} validated
        -- an empty string is not a reviewer."""
        instance = json.loads(
            (EXAMPLES_DIR / "project-descriptor.greenfield.example.json").read_text()
        )
        instance["review"]["reviewer"] = ""
        errors = list(self.validator.iter_errors(instance))
        self.assertTrue(errors, "schema accepted an empty reviewer")

    def test_malformed_reviewed_at_is_rejected(self):
        """Review finding (round 2, medium severity): format: date is
        annotation-only unless a format_checker is wired in, which no call
        site did -- reviewed_at: "not-a-date" validated. Now enforced both
        by an actual format checker (schema_utils.make_validator) and a
        structural pattern, so this holds even if a future validator is
        built by hand without the shared helper."""
        instance = json.loads(
            (EXAMPLES_DIR / "project-descriptor.greenfield.example.json").read_text()
        )
        instance["review"]["reviewed_at"] = "not-a-date"
        errors = list(self.validator.iter_errors(instance))
        self.assertTrue(errors, "schema accepted reviewed_at: 'not-a-date'")

    def test_greenfield_mode_forbids_port_source(self):
        """Review finding D6: the if/then required port_source in port
        mode but nothing forbade a stray port_source under greenfield."""
        instance = json.loads(
            (EXAMPLES_DIR / "project-descriptor.greenfield.example.json").read_text()
        )
        instance["port_source"] = {
            "repository": "should not be here",
            "language": "cpp",
            "oracle_build_command": "make",
        }
        errors = list(self.validator.iter_errors(instance))
        self.assertTrue(errors, "schema accepted port_source under mode: greenfield")

    def test_unknown_verifier_is_rejected(self):
        instance = json.loads(
            (EXAMPLES_DIR / "project-descriptor.greenfield.example.json").read_text()
        )
        instance["verifier_policy"]["default"] = "not-a-real-verifier"
        errors = list(self.validator.iter_errors(instance))
        self.assertTrue(errors, "schema accepted an unknown verifier name")

    def test_closure_kind_is_accepted(self):
        """chainlink #73: the date-creusot pilot had no way to declare the
        intended closure_kind in the descriptor -- every shape it tried
        (top-level closure_kind, verifier_policy.closure_kind, a closure
        object, verifier_policy.closure_kinds) was rejected, so the intent
        could only be recorded outside the tool. The top-level field is
        the documented place. #85 extends the vocabulary with `partial`
        so a pilot that cannot achieve full deductive closure can declare
        that intent too."""
        for kind in ("deductive", "bounded", "partial"):
            with self.subTest(closure_kind=kind):
                instance = json.loads(
                    (EXAMPLES_DIR / "project-descriptor.greenfield.example.json").read_text()
                )
                instance["closure_kind"] = kind
                errors = list(self.validator.iter_errors(instance))
                self.assertEqual(errors, [], [e.message for e in errors])

    def test_closure_kind_rejects_value_outside_the_enum(self):
        instance = json.loads(
            (EXAMPLES_DIR / "project-descriptor.greenfield.example.json").read_text()
        )
        instance["closure_kind"] = "speculative"
        errors = list(self.validator.iter_errors(instance))
        self.assertTrue(
            errors, "schema accepted a closure_kind outside deductive/bounded/partial"
        )

    def test_closure_kind_rejects_non_string(self):
        instance = json.loads(
            (EXAMPLES_DIR / "project-descriptor.greenfield.example.json").read_text()
        )
        instance["closure_kind"] = 1
        errors = list(self.validator.iter_errors(instance))
        self.assertTrue(errors, "schema accepted a non-string closure_kind")


class VerifierPolicyTest(unittest.TestCase):
    """chainlink #76: `verifier_policy` was the only open object in the
    descriptor, its value domain (creusot | verus | kani) was an accident of
    `additionalProperties` -- undiscoverable and unreported by any command --
    and a second or supporting verifier could not be expressed at all,
    while an arbitrary extra key holding a second enum member validated
    silently and was then discarded by every reader."""

    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA_PATH.read_text())
        cls.validator = make_validator(cls.schema)

    def _port_descriptor(self) -> dict:
        return json.loads(
            (EXAMPLES_DIR / "project-descriptor.port.example.json").read_text()
        )

    def test_verifier_policy_key_set_is_documented(self):
        """The open object's key set is documented in the schema itself:
        'default' (required), per-cluster override keys, and 'supporting'
        as the declared multi-verifier shape (chainlink #76)."""
        description = self.schema["properties"]["verifier_policy"]["description"]
        self.assertIn("default", description)
        self.assertIn("cluster", description)
        self.assertIn("supporting", description)

    def test_verifier_enum_domain_is_documented(self):
        """The value domain is discoverable from the schema, not only from
        a rejection message: the $defs/verifier description names the three
        permitted values and the exact-match rule (chainlink #76)."""
        description = self.schema["$defs"]["verifier"]["description"]
        for verifier in ("creusot", "verus", "kani"):
            self.assertIn(verifier, description)

    def test_supporting_array_is_the_real_multi_verifier_shape(self):
        """The P3 crypto-mixed pilot's verus + kani in one workspace, and
        the epic's 'Kani supporting evidence' probe, have a descriptor-level
        home: a documented list, not an arbitrary enum-valued key
        (chainlink #76)."""
        instance = self._port_descriptor()
        instance["verifier_policy"] = {"default": "verus", "supporting": ["kani"]}
        errors = list(self.validator.iter_errors(instance))
        self.assertEqual(errors, [], [e.message for e in errors])

    def test_supporting_rejects_a_bare_string(self):
        """The fake-write from chainlink #76 Defect 3 --
        `verifier_policy.supporting: "kani"` (a string, not a list) -- is
        now a schema violation, not a silently discarded declaration."""
        instance = self._port_descriptor()
        instance["verifier_policy"] = {"default": "verus", "supporting": "kani"}
        errors = list(self.validator.iter_errors(instance))
        self.assertTrue(errors, "schema accepted supporting as a bare string")

    def test_supporting_rejects_a_non_member(self):
        instance = self._port_descriptor()
        instance["verifier_policy"] = {"default": "verus", "supporting": ["dafny"]}
        errors = list(self.validator.iter_errors(instance))
        self.assertTrue(errors, "schema accepted a non-member in supporting")

    def test_per_cluster_override_is_still_accepted(self):
        """gate_g9.py resolves `policy.get(cluster, policy["default"])`,
        so per-cluster override keys must stay expressible -- the fix
        documents the key set rather than closing the object (chainlink
        #76)."""
        instance = self._port_descriptor()
        instance["verifier_policy"] = {"default": "creusot", "crypto-mixed": "kani"}
        errors = list(self.validator.iter_errors(instance))
        self.assertEqual(errors, [], [e.message for e in errors])

    def test_misspelled_key_is_accepted_as_a_cluster_override(self):
        """The documented behavior for the Defect 1 hazard: an arbitrary
        key is a per-cluster override, so `defualt: "kani"` validates as an
        override for a cluster literally named 'defualt'. Closing the
        object would break per-cluster overrides, so the hazard is
        mitigated instead: the key set is documented on the schema, and
        `status --json` echoes the policy so the typo is visible under
        `clusters` rather than silent (chainlink #76)."""
        instance = self._port_descriptor()
        instance["verifier_policy"]["defualt"] = "kani"
        errors = list(self.validator.iter_errors(instance))
        self.assertEqual(errors, [], [e.message for e in errors])


class SchemaDiagnosticsTest(unittest.TestCase):
    """chainlink #73: the load-time diagnostic for an invalid descriptor
    names the offending property -- with its JSON path and the permitted
    alternatives -- instead of jsonschema's raw messages, which bury the
    rejected key in prose ("Additional properties are not allowed
    ('commit' was unexpected)") or print the whole descriptor as the
    failing instance."""

    def setUp(self):
        self.example = json.loads(
            (EXAMPLES_DIR / "project-descriptor.port.example.json").read_text()
        )

    def test_valid_descriptor_has_no_diagnostics(self):
        self.assertEqual(schema_diagnostics(self.example), [])

    def test_unknown_property_names_json_path_and_permitted_alternatives(self):
        data = json.loads(json.dumps(self.example))
        data["port_source"]["commit"] = "abc123"
        self.assertEqual(
            schema_diagnostics(data),
            [
                "$.port_source: unexpected property 'commit'; "
                "permitted: language, oracle_build_command, repository"
            ],
        )

    def test_unknown_top_level_property_names_the_permitted_top_level_keys(self):
        data = json.loads(json.dumps(self.example))
        data["closure_kinds"] = ["deductive"]
        (line,) = schema_diagnostics(data)
        self.assertTrue(line.startswith("$:"))  # the top-level object's JSON path
        self.assertIn("unexpected property 'closure_kinds'", line)
        self.assertIn("closure_kind", line)  # the near-miss the pilot wanted

    def test_enum_violation_names_the_permitted_values(self):
        data = json.loads(json.dumps(self.example))
        data["mode"] = "hybrid"
        lines = schema_diagnostics(data)
        self.assertIn("$.mode: 'hybrid' is not one of ['greenfield', 'port']", lines)

    def test_missing_required_property_is_named(self):
        data = json.loads(json.dumps(self.example))
        del data["write_set"]
        self.assertEqual(
            schema_diagnostics(data),
            ["$: missing required property 'write_set'"],
        )

    def test_multiple_problems_each_get_a_line(self):
        data = json.loads(json.dumps(self.example))
        data["port_source"]["commit"] = "abc123"
        data["verifier_policy"]["default"] = "dafny"
        lines = schema_diagnostics(data)
        self.assertEqual(len(lines), 2)
        self.assertIn("$.port_source", lines[0])
        self.assertIn("$.verifier_policy.default", lines[1])

    def test_unknown_key_and_invalid_value_are_distinguished(self):
        """chainlink #76 Defect 3: 'unknown key' and 'invalid value' were
        the same undifferentiated present-invalid / invalid_input. The
        diagnostic text must say which happened: an unexpected property
        names the permitted alternatives; an enum violation names the
        permitted values (chainlink #73's distinction, pinned for the
        verifier_policy field the pilot is named after)."""
        data = json.loads(json.dumps(self.example))
        data["verifier_policy"]["zzz_not_a_real_key"] = "nope"
        (line,) = schema_diagnostics(data)
        self.assertIn("$.verifier_policy.zzz_not_a_real_key", line)
        self.assertIn("is not one of", line)
        for verifier in ("kani", "creusot", "verus"):
            self.assertIn(verifier, line)

    def test_unknown_key_in_a_closed_object_names_permitted_alternatives(self):
        """The other half of the distinction: a key the schema does not
        know in a CLOSED object is an 'unexpected property', not a bad
        value -- the two failure kinds never collapse into one message
        (chainlink #76)."""
        data = json.loads(json.dumps(self.example))
        data["compatibility_policy"]["zzz"] = "kani"
        (line,) = schema_diagnostics(data)
        self.assertIn("unexpected property 'zzz'", line)
        self.assertIn("permitted:", line)


class DidYouMeanTest(unittest.TestCase):
    """chainlink #106: `zzz_not_a_real_field` (a key the schema never had)
    and `defualt` (a near-miss of a key it does have) produced a
    byte-identical `unexpected property '<key>'` line, so the only way to
    recover the intended key was to eyeball a flat alphabetical dump of
    every permitted name -- which is the trial-and-error against an
    undocumented schema chainlink #73 was supposed to have removed.

    Three failure kinds, three distinguishable diagnostics: an unknown
    key (no suggestion), a mistyped permitted key (the one it looks
    like), and a real key written in the wrong object (where that key
    actually lives)."""

    def setUp(self):
        self.example = json.loads(
            (EXAMPLES_DIR / "project-descriptor.port.example.json").read_text()
        )

    def diagnose(self, mutate) -> list[str]:
        data = json.loads(json.dumps(self.example))
        mutate(data)
        return schema_diagnostics(data)

    def test_the_issue_repro_no_longer_gives_two_keys_the_same_message(self):
        """The defect, verbatim: these two probes produced the same
        message shape, so a user could not tell a typo from a key that
        does not exist."""
        unknown = self.diagnose(lambda d: d.update(zzz_not_a_real_field="x"))
        typo = self.diagnose(lambda d: d.update(defualt="x"))
        self.assertNotEqual(unknown, typo)
        # ...and neither collapsed into the other: the unknown key gets no
        # suggestion at all, the typo gets exactly one.
        self.assertNotIn("did you mean", unknown[0])
        self.assertIn("did you mean", typo[0])

    def test_unknown_key_gets_no_suggestion_at_all(self):
        """A guess the user cannot act on costs them the thing this
        diagnostic exists to give back, so a key that is genuinely
        unknown gets chainlink #73's message unchanged -- including the
        exact line quoted in docs/mode-p-cli-flow.md."""
        data = json.loads(json.dumps(self.example))
        data["port_source"]["commit"] = "abc123"
        self.assertEqual(
            schema_diagnostics(data),
            [
                (
                    "$.port_source: unexpected property 'commit'; "
                    "permitted: language, oracle_build_command, repository"
                )
            ],
        )

    def test_a_misspelled_permitted_key_names_the_key_it_looks_like(self):
        (line,) = self.diagnose(lambda d: d.update(closure_kinds="x"))
        self.assertIn("unexpected property 'closure_kinds'", line)
        self.assertTrue(line.endswith("did you mean 'closure_kind'?"), line)

    def test_a_transposed_key_is_a_near_miss(self):
        """`defualt` is one ADJACENT TRANSPOSITION from `default`. Under
        plain Levenshtein that is two edits, indistinguishable from an
        unrelated key -- which is why the gap is measured with
        Damerau-Levenshtein. Pointed at here from the top level, where
        `default` is not permitted, so the message also has to say where
        the key it was reaching for lives."""
        (line,) = self.diagnose(lambda d: d.update(defualt="x"))
        self.assertIn("unexpected property 'defualt'", line)
        self.assertIn("'$.verifier_policy.default'", line)
        self.assertIn("a property of $.verifier_policy, not of $", line)

    def test_a_misplaced_key_is_not_reported_as_a_permitted_sibling(self):
        """The hint must name the key's real home, never one of the
        top-level keys it is merely near -- sending the user after
        `crates` when they meant `verifier_policy.default` would be a
        confident wrong answer."""
        (line,) = self.diagnose(lambda d: d.update(defualt="x"))
        self.assertNotIn("did you mean 'crates'", line)
        self.assertNotIn("did you mean 'project'", line)

    def test_a_prefix_is_a_near_miss_however_long_the_omitted_tail(self):
        """`closure` -> `closure_kind` is one intent, not five edits: it
        is the shape the date-creusot pilot actually probed with."""
        (line,) = self.diagnose(lambda d: d.update(closure={"kind": "deductive"}))
        self.assertIn("unexpected property 'closure'", line)
        self.assertTrue(line.endswith("did you mean 'closure_kind'?"), line)

    def test_case_and_separator_slips_are_near_misses(self):
        """The descriptor's keys are lowercase snake_case, so `Mode` is a
        slip of `mode` rather than a new key, and
        `oraclebuildcommand` is a slip of `oracle_build_command`."""
        (line,) = self.diagnose(lambda d: d.update(Mode="port"))
        self.assertTrue(line.endswith("did you mean 'mode'?"), line)
        (line,) = self.diagnose(
            lambda d: d["port_source"].update(oraclebuildcommand="make oracle")
        )
        self.assertTrue(line.endswith("did you mean 'oracle_build_command'?"), line)

    def test_a_near_miss_inside_a_nested_object_is_suggested(self):
        (line,) = self.diagnose(lambda d: d["crates"][0].update(specs_search_rooot="rust"))
        self.assertIn("$.crates[0]", line)
        self.assertTrue(line.endswith("did you mean 'specs_search_root'?"), line)

    def test_a_key_the_schema_reached_through_a_ref_is_suggested(self):
        """`review` is a `$ref` to `#/$defs/review`, so a walk that did
        not resolve local pointers could not suggest its own keys."""
        (line,) = self.diagnose(lambda d: d["review"].update(reviewred_at="2026-10-01"))
        self.assertTrue(line.endswith("did you mean 'reviewed_at'?"), line)

    def test_each_rejected_key_gets_its_own_clause(self):
        """Two rejected keys, two suggestions, each tied to the key it
        belongs to -- and an unknown key alongside a typo does not
        dilute the typo's suggestion."""
        (line,) = self.diagnose(
            lambda d: d.update(closure_kinds="x", comptability_policy="y")
        )
        self.assertIn("'closure_kind' (for 'closure_kinds')", line)
        self.assertIn("'compatibility_policy' (for 'comptability_policy')", line)
        (line,) = self.diagnose(lambda d: d.update(closure_kinds="x", zzzzzz="y"))
        self.assertIn("'closure_kind' (for 'closure_kinds')", line)
        self.assertNotIn("zzzzzz)", line)

    def test_only_unknown_keys_stay_suggestion_free(self):
        for mutate in (
            lambda d: d.update(aaa="x", bbb="y"),
            lambda d: d["compatibility_policy"].update(zzz="x"),
            lambda d: d.update(zzz_not_a_real_field="x"),
            lambda d: d["project"].update(nickname="x"),
        ):
            for line in self.diagnose(mutate):
                self.assertNotIn("did you mean", line)

    def test_a_suggestion_never_names_a_key_the_schema_does_not_accept(self):
        """Every suggestion the did-you-mean can emit -- at this object or
        somewhere else in the descriptor -- must resolve to a property the
        schema actually declares. A hand-kept list of names could drift;
        the schema cannot."""
        schema = json.loads(SCHEMA_PATH.read_text())
        declared = {path for path, _name in _schema_property_paths(schema, schema)}
        # One rejected key per probe, drawn from the shapes people actually
        # mistype: a transposition, a dropped letter, a plural, a prefix, a
        # case slip, a separator slip, and a pluralized object name.
        for key in (
            "defualt",
            "comptability_policy",
            "closure_kinds",
            "closure",
            "Mode",
            "crate",
            "write_sets",
            "schemas_version",
            "crates_list",
        ):
            data = json.loads(json.dumps(self.example))
            data[key] = "x"
            for line in schema_diagnostics(data):
                if "did you mean" not in line:
                    continue
                # Everything after `did you mean`, minus the per-key
                # parentheticals (which name the rejected key, not the
                # suggestion) and the question mark.
                hint = line.split("did you mean ", 1)[1]
                hint = re.sub(r"\(for '[^']*'(;[^)]*)?\)", "", hint).rstrip("?")
                suggestions = re.findall(r"'([^']+)'", hint)
                self.assertTrue(suggestions, line)
                for suggestion in suggestions:
                    # A local suggestion is a bare key name; a misplaced
                    # key's is the JSON path of the key's real home.
                    self.assertIn(
                        suggestion if suggestion.startswith("$") else f"$.{suggestion}",
                        declared,
                        line,
                    )

    def test_the_suggestion_is_deterministic(self):
        """Ties break on the name itself, so the same descriptor reports
        the same line on every machine and every run -- the determinism
        every other fact this module reports already has."""
        def mutate(data):
            data.update(closure_kinds="x")

        self.assertEqual(self.diagnose(mutate), self.diagnose(mutate))

    def test_a_rejected_key_in_a_closed_object_with_no_suggestion_is_unchanged(self):
        """An object whose whole permitted list is far from the rejected
        key must not be padded with a guess -- `zzz` against
        `reliance_policy_path`/`witness_policy_path` is the #76 case."""
        (line,) = self.diagnose(lambda d: d["compatibility_policy"].update(zzz="x"))
        self.assertEqual(
            line,
            "$.compatibility_policy: unexpected property 'zzz'; "
            "permitted: reliance_policy_path, witness_policy_path",
        )

    def test_an_open_object_still_accepts_a_per_cluster_override(self):
        """chainlink #76's documented decision is unchanged by #106:
        `verifier_policy` stays open, because gate g9 resolves a
        per-cluster override -- the did-you-mean adds a diagnostic for a
        CLOSED object, it does not close one."""
        data = json.loads(json.dumps(self.example))
        data["verifier_policy"]["defualt"] = "kani"
        self.assertEqual(schema_diagnostics(data), [])

    def test_the_diagnostic_surfaces_on_the_load_error_every_command_prints(self):
        """Every CLI boundary prints `error: {e}` for a
        `ProjectDescriptorError`, so the suggestion has to be in the
        exception message, not only in the `check` finding."""
        data = json.loads(json.dumps(self.example))
        data["closure_kinds"] = ["deductive"]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(data, f)
        path = Path(f.name)
        try:
            with self.assertRaises(ProjectDescriptorError) as ctx:
                load_project_descriptor(path)
            message = str(ctx.exception)
            self.assertIn("unexpected property 'closure_kinds'", message)
            self.assertIn("did you mean 'closure_kind'?", message)
        finally:
            path.unlink()


class MissingDescriptorTest(unittest.TestCase):
    """chainlink #108: the loader read `project-descriptor.json` without
    checking that it exists, so an uninitialized workspace reached the
    user as a raw `FileNotFoundError` traceback out of `extract-c-static`
    and `validate` (the swisstable-verus pilot, `swisstable-verus-mcp-007`)
    instead of the one-line diagnostic the rest of the surface already
    gave. Fifteen of the commands that call this loader crashed that way;
    `status`, `check` and `doctor` never did, which is what made it a
    defect rather than a design choice. The guard therefore lives here, in
    the one function every one of them calls."""

    def test_a_missing_descriptor_names_the_file_and_the_command_that_creates_it(self):
        """The pilot's expectation, verbatim: name the missing file, name
        `ligature init` (the same command `migrate --upgrade` tells an
        uninitialized workspace to run), one line."""
        missing = Path("/tmp/nonexistent-workspace/project-descriptor.json")
        with self.assertRaises(ProjectDescriptorError) as ctx:
            read_descriptor_text(missing)
        message = str(ctx.exception)
        self.assertIn(str(missing), message)
        self.assertIn("ligature init", message)
        self.assertEqual(len(message.splitlines()), 1, message)

    def test_the_missing_file_is_not_reported_as_a_bare_file_not_found(self):
        """A bare `FileNotFoundError` is the defect (#108). The guard's
        exception must not be one, or every caller catching OSError keeps
        printing a raw errno string and the caller that translates this
        error type (pipeline's wrapper) never sees it at all."""
        with self.assertRaises(ProjectDescriptorError) as ctx:
            load_project_descriptor(Path("/tmp/nonexistent-workspace/project-descriptor.json"))
        self.assertNotIsInstance(ctx.exception, OSError)
        self.assertNotIsInstance(ctx.exception, ValueError)
        self.assertNotIn("Errno", str(ctx.exception))
        self.assertNotIn("Traceback", str(ctx.exception))

    def test_the_message_carries_no_error_prefix_the_cli_adds_itself(self):
        """Every CLI surface prints `error: {e}`; a guard that prefixed
        its own message would print `error: error: ...`."""
        with self.assertRaises(ProjectDescriptorError) as ctx:
            read_descriptor_text(Path("/tmp/nonexistent-workspace/project-descriptor.json"))
        self.assertFalse(str(ctx.exception).startswith("error:"), str(ctx.exception))

    def test_the_diagnostics_name_the_missing_file_too(self):
        """`diagnostics` is what `status`/`check` render for a
        present-but-invalid descriptor. An absent one has no property to
        blame, but it still owes the caller a one-line statement rather
        than an empty list it cannot tell apart from 'no problem'."""
        with self.assertRaises(ProjectDescriptorError) as ctx:
            load_project_descriptor(Path("/tmp/nonexistent-workspace/project-descriptor.json"))
        (line,) = ctx.exception.diagnostics
        self.assertIn("project-descriptor.json", line)

    def test_a_directory_is_reported_as_a_directory_not_as_a_missing_file(self):
        """Same guard, different condition: `--descriptor <dir>` used to
        raise `IsADirectoryError` out of pathlib with no idea which file
        was wanted. `manifest_input.ensure_manifest_path` (chainlink #83)
        makes the same distinction, so the two guards cannot be read as
        disagreeing about what is wrong."""
        with self.assertRaises(ProjectDescriptorError) as ctx:
            read_descriptor_text(Path(tempfile.gettempdir()))
        message = str(ctx.exception)
        self.assertIn("is a directory", message)
        self.assertNotIn("no such file", message)
        self.assertNotIn("ligature init", message)

    def test_an_undecodable_descriptor_is_not_a_traceback_either(self):
        """The remaining way `read_text()` can fail loudly: bytes that are
        not text. Same class of defect (an unhandled exception where a
        one-line diagnostic belongs), same guard."""
        with tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False) as handle:
            handle.write(b"\xff\xfe not utf-8")
            path = Path(handle.name)
        try:
            with self.assertRaises(ProjectDescriptorError) as ctx:
                load_project_descriptor(path)
            self.assertIn("not valid UTF-8", str(ctx.exception))
        finally:
            path.unlink()

    def test_a_valid_descriptor_still_loads_through_the_guard(self):
        """The guard is a pre-flight, not a replacement: the happy path
        and the schema diagnostics are untouched by it."""
        data = load_project_descriptor(EXAMPLES_DIR / "project-descriptor.greenfield.example.json")
        self.assertEqual(data["mode"], "greenfield")


if __name__ == "__main__":
    unittest.main()
