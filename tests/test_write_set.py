"""Write-set conformance check (chainlink #77).

The date-creusot pilot's black-box repro, reproduced against the real
CLI: a filled descriptor whose write_set declares `rust/*/src/` and
`rust/*/tests/` as the only writable roots, three files created outside
them (`rust/rogue/evil.rs`, `rust/rogue/notes.txt`, `docs/evil.md`), and
every v1.0 command reporting nothing. These tests pin the fix: the
dedicated `write-set-check` command, the `status --json` write_set state,
the `check --json` findings, and the exit codes.

Also covers the categories the pilot's matrix did not exercise but the
report names: files inside `protected_roots` that nothing vouches for
(the protected-surface audit), the vacuous declaration shapes the schema
accepts (`allowed_roots: ["**"]`, `protected_roots: []`), the pinned
upstream checkout's exclusion, and the canonical pipeline locations the
write set does not govern.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import pipeline  # noqa: E402
import write_set  # noqa: E402
from schema_utils import make_validator  # noqa: E402

PROJECT_STATE_SCHEMA = json.loads((ROOT / "schemas" / "project-state.schema.json").read_text())
CONSOLIDATED_CHECK_SCHEMA = json.loads(
    (ROOT / "schemas" / "consolidated-check.schema.json").read_text()
)

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


class WriteSetFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name).resolve()
        self.descriptor_path = self.workspace / "project-descriptor.json"

    def tearDown(self):
        self._tmp.cleanup()

    def init_workspace(self, mode="port", name="date-creusot"):
        """A real `ligature init` (which records the gate_integrity hashes
        in a manifest, pinning them), then the descriptor edits a real
        project applies on top -- the same fixture shape
        test_project_state.py's WorkspaceFixture uses. The installed policy
        template's `Policy version:` marker is stamped as well
        (chainlink #113), so a write-set test is never also measuring the
        one condition an unfilled policy marker is."""
        code, _ = self.run_cli("init", "--mode", mode, "--name", name)
        self.assertEqual(code, 0)
        code, out = self.run_cli("accept-policy", "--reviewer", "pilot-reviewer", "--version", "reliance-policy@1.0")
        self.assertEqual(code, 0, out)
        descriptor = json.loads(self.descriptor_path.read_text())
        descriptor["crates"] = [
            {"crate_dir": "rust/date-creusot-core", "contracts_crate": "contracts", "specs_search_root": "rust"}
        ]
        descriptor["review"]["reviewer"] = "pilot-reviewer"
        descriptor["write_set"] = json.loads(json.dumps(PILOT_WRITE_SET))
        if mode == "port":
            descriptor["port_source"]["repository"] = "upstream"
        self.descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n")
        return descriptor

    def write(self, relative: str, data: str = "rogue\n") -> Path:
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data)
        return path

    def run_cli(self, *args):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = pipeline.main(
                ["--workspace", str(self.workspace), "--descriptor", str(self.descriptor_path), *args]
            )
        return code, buffer.getvalue()

    def write_set_check(self, *args) -> tuple[int, dict]:
        code, out = self.run_cli("write-set-check", "--json", *args)
        return code, json.loads(out)

    def status(self) -> tuple[int, dict]:
        code, out = self.run_cli("status", "--json")
        return code, json.loads(out)

    def check(self) -> tuple[int, dict]:
        code, out = self.run_cli("check", "--json")
        return code, json.loads(out)

    def assertValid(self, schema, document, validator_name="document"):
        validator = make_validator(schema)
        errors = list(validator.iter_errors(document))
        self.assertEqual(errors, [], f"{validator_name} invalid: {[e.message for e in errors]}")


class WriteSetCleanTest(WriteSetFixture):
    def test_initialized_workspace_is_clean(self):
        self.init_workspace()
        code, doc = self.write_set_check()
        self.assertEqual(code, 0)
        self.assertEqual(doc["write_set"]["state"], "clean")
        self.assertEqual(doc["write_set"]["violations"], [])
        self.assertEqual(doc["write_set"]["protected_unvouched"], [])
        self.assertFalse(doc["mutated_workspace"])

    def test_status_carries_the_write_set_state(self):
        self.init_workspace()
        code, doc = self.status()
        self.assertEqual(code, 0)
        self.assertValid(PROJECT_STATE_SCHEMA, doc, "project-state")
        self.assertEqual(doc["write_set"]["state"], "clean")
        self.assertIn("clean", doc["write_set"]["details"])

    def test_check_is_clean_on_an_initialized_workspace(self):
        self.init_workspace()
        code, doc = self.check()
        self.assertEqual(code, 0)
        self.assertValid(CONSOLIDATED_CHECK_SCHEMA, doc, "consolidated-check")
        self.assertEqual(
            [f for f in doc["findings"] if f["gate_id"] == "write-set"],
            [],
        )


class WriteSetViolationTest(WriteSetFixture):
    """The pilot's exact repro: three files outside allowed_roots, next to
    a filled descriptor, reported by nothing in v1.0."""

    def test_pilot_repro_files_are_reported(self):
        self.init_workspace()
        self.write("rust/rogue/evil.rs", "pub fn evil() {}\n")
        self.write("rust/rogue/notes.txt")
        self.write("docs/evil.md")
        code, doc = self.write_set_check()
        self.assertEqual(code, 1)
        self.assertEqual(doc["write_set"]["state"], "violations")
        paths = sorted(v["path"] for v in doc["write_set"]["violations"])
        self.assertEqual(paths, ["docs/evil.md", "rust/rogue/evil.rs", "rust/rogue/notes.txt"])
        for violation in doc["write_set"]["violations"]:
            self.assertIn("allowed_roots", violation["reason"])

    def test_check_reports_out_of_set_files_as_blocking_findings(self):
        self.init_workspace()
        self.write("rust/rogue/evil.rs", "pub fn evil() {}\n")
        code, doc = self.check()
        self.assertEqual(code, 1)
        self.assertIn("blocking_findings", doc["result"]["conditions"])
        write_set_findings = [f for f in doc["findings"] if f["gate_id"] == "write-set"]
        self.assertEqual(len(write_set_findings), 1)
        self.assertEqual(write_set_findings[0]["severity"], "high")
        self.assertEqual(write_set_findings[0]["subject"], "rust/rogue/evil.rs")
        self.assertEqual(write_set_findings[0]["authority"], "mechanized-gate")

    def test_status_state_is_violations_with_details_naming_the_files(self):
        self.init_workspace()
        self.write("docs/evil.md")
        code, doc = self.status()
        self.assertEqual(code, 0)
        self.assertEqual(doc["write_set"]["state"], "violations")
        self.assertIn("docs/evil.md", doc["write_set"]["details"])

    def test_files_under_allowed_roots_are_not_violations(self):
        self.init_workspace()
        self.write("rust/date-creusot-core/src/lib.rs", "pub fn ok() {}\n")
        self.write("rust/date-creusot-core/tests/it.rs", "#[test]\nfn ok() {}\n")
        code, doc = self.write_set_check()
        self.assertEqual(code, 0)
        self.assertEqual(doc["write_set"]["state"], "clean")

    def test_canonical_pipeline_locations_are_accounted_for(self):
        """The pipeline writes ci/, evidence/, workspace-level specs/_<kind>/,
        and docs/witnesses/ itself -- the write set governs the agent, not
        the pipeline, so files there are not out-of-set writes."""
        self.init_workspace()
        self.write("evidence/E-0001.json", json.dumps({"id": "E-0001"}))
        self.write("ci/results/c_static/target.json", json.dumps({"report_id": "R-1"}))
        self.write("ci/manifest/WP-1.json", json.dumps({"work_package": "WP-1"}))
        self.write("specs/_closure/date-creusot.json", json.dumps({"cluster": "date-creusot"}))
        self.write("docs/witnesses/_contact_sheet.svg", "<svg/>")
        code, doc = self.write_set_check()
        self.assertEqual(code, 0)
        self.assertEqual(doc["write_set"]["state"], "clean")

    def test_managed_files_are_accounted_for(self):
        self.init_workspace()
        # the init-installed managed files are accounted for by the
        # ownership manifest even though they are outside every root
        code, doc = self.write_set_check()
        self.assertEqual(code, 0)
        paths = [v["path"] for v in doc["write_set"]["violations"]]
        self.assertNotIn(".codex/skills/ligature/SKILL.md", paths)
        self.assertNotIn("ci/manifest/installation.json", paths)
        self.assertNotIn("schemas/project-descriptor.schema.json", paths)

    def test_declared_crate_spec_trees_are_accounted_for(self):
        self.init_workspace()
        self.write(
            "rust/date-creusot-core/specs/_boundaries/a__to__b.json",
            json.dumps({"boundary_id": "a__to__b"}),
        )
        code, doc = self.write_set_check()
        self.assertEqual(code, 0)
        self.assertEqual(doc["write_set"]["state"], "clean")


class ProtectedWriteTest(WriteSetFixture):
    """chainlink #103: the issue's exact repro, reproduced against the
    real CLI. Three probe files planted inside the pilot's own
    protected_roots -- one in `ci/manifest/`, two in the declared crate's
    protected specs tree -- reported `write set: clean` with
    `violations: []`, exit 0, and output byte-identical to the no-probe
    baseline in three consecutive releases, because `protected_roots` was
    an "accounted for" category and two location carve-outs (the `ci/`
    canonical prefix, and any file under a declared crate's `specs/`) made
    every probe invisible rather than silent-but-caught.

    A protected root is now enforced: a file inside one is a violation
    unless a declaration vouches for that file."""

    PROBES = (
        "ci/manifest/PROBE.json",
        "rust/date-creusot-core/specs/_interactions/I-PROBE.json",
        "rust/date-creusot-core/specs/_boundaries/PROBE.json",
    )

    def test_the_issue_s_probes_are_violations(self):
        self.init_workspace()
        for probe in self.PROBES:
            self.write(probe, "{}")
        code, doc = self.write_set_check()
        self.assertEqual(code, 1)
        self.assertEqual(doc["write_set"]["state"], "violations")
        self.assertEqual(
            sorted((v["path"], v["kind"]) for v in doc["write_set"]["violations"]),
            [(probe, "protected-write") for probe in sorted(self.PROBES)],
        )

    def test_the_issue_s_probes_block_check_as_high_severity_findings(self):
        """The command is only evidence for authority boundary 5 if `check`
        -- the gate a run is judged on -- fails on it too."""
        self.init_workspace()
        for probe in self.PROBES:
            self.write(probe, "{}")
        code, doc = self.check()
        self.assertEqual(code, 1)
        self.assertIn("blocking_findings", doc["result"]["conditions"])
        findings = [f for f in doc["findings"] if f["gate_id"] == "write-set"]
        self.assertEqual(sorted(f["subject"] for f in findings), sorted(self.PROBES))
        for finding in findings:
            self.assertEqual(finding["severity"], "high")
            self.assertEqual(finding["authority"], "mechanized-gate")

    def test_status_state_is_violations(self):
        self.init_workspace()
        self.write("ci/manifest/PROBE.json", "{}")
        code, doc = self.status()
        self.assertEqual(code, 0)
        self.assertEqual(doc["write_set"]["state"], "violations")
        self.assertIn("ci/manifest/PROBE.json", doc["write_set"]["details"])

    def test_the_two_classes_are_reported_separately(self):
        """The report asks for 'outside the write set' and 'inside a
        protected root' to be distinguishable -- only the second is a
        breach of authority boundary 5."""
        self.init_workspace()
        self.write("rust/rogue/evil.rs", "pub fn evil() {}\n")
        self.write("ci/manifest/PROBE.json", "{}")
        code, doc = self.write_set_check()
        kinds = {v["path"]: v["kind"] for v in doc["write_set"]["violations"]}
        self.assertEqual(kinds["rust/rogue/evil.rs"], "out-of-set")
        self.assertEqual(kinds["ci/manifest/PROBE.json"], "protected-write")

    def test_a_protected_write_is_not_also_an_out_of_set_file(self):
        self.init_workspace()
        self.write("ci/manifest/PROBE.json", "{}")
        code, doc = self.write_set_check()
        self.assertEqual(len(doc["write_set"]["violations"]), 1)

    def test_output_names_the_class_and_the_matched_protected_pattern(self):
        self.init_workspace()
        self.write("rust/date-creusot-core/specs/_boundaries/PROBE.json", "{}")
        code, out = self.run_cli("write-set-check")
        self.assertEqual(code, 1)
        self.assertIn("violation [protected-write]: rust/date-creusot-core/specs/_boundaries/PROBE.json", out)
        self.assertIn("rust/*/specs/**", out)

    def test_a_real_spec_artifact_in_a_protected_specs_tree_is_accounted_for(self):
        """The carve-out the report called out by name is not gone -- it is
        stated: a spec artifact, recognized by the pipeline's own artifact
        discriminators, is pipeline output, not a protected write."""
        self.init_workspace()
        for dirname, body in (
            ("_boundaries", {"boundary_id": "a__to__b"}),
            ("_interactions", {"interaction_id": "I-1"}),
            ("_exemptions", {"interaction_id": "I-1"}),
            ("_protocol_debt", {"interaction_id": "I-1"}),
            ("_bridges", {"bridge_id": "BR-1"}),
            ("_witnesses", {"witness_id": "W-1"}),
        ):
            self.write(
                f"rust/date-creusot-core/specs/{dirname}/x.json", json.dumps(body)
            )
        self.write(
            "ci/manifest/WP-1.json",
            json.dumps({"schema": "work-package-manifest/1.0", "work_package": "WP-1"}),
        )
        code, doc = self.write_set_check()
        self.assertEqual(code, 0)
        self.assertEqual(doc["write_set"]["state"], "clean")
        self.assertEqual(doc["write_set"]["violations"], [])

    def test_a_staged_draft_in_a_protected_specs_tree_is_accounted_for(self):
        """`ligature draft` writes `<target>.json.draft` through
        review_checkpoint.stage_draft -- the pipeline's own command writing
        inside a protected artifact tree, and inert until it is promoted."""
        self.init_workspace()
        self.write(
            "rust/date-creusot-core/specs/_boundaries/a__to__b.json.draft",
            json.dumps({"boundary_id": "a__to__b"}),
        )
        code, doc = self.write_set_check()
        self.assertEqual(code, 0)
        self.assertEqual(doc["write_set"]["state"], "clean")

    def test_a_draft_suffix_outside_an_artifact_directory_is_not_exempt(self):
        """The draft exemption is the pipeline's own staging convention
        inside the locations it writes artifacts into -- it is not a
        filename-shaped hole anywhere else in the protected surface."""
        self.init_workspace()
        self.write("scripts/evil.py.draft", "echo rogue\n")
        code, doc = self.write_set_check()
        self.assertEqual(doc["write_set"]["protected_unvouched"], ["scripts/evil.py.draft"])

    def test_a_protected_file_also_under_an_allowed_root_is_not_a_violation(self):
        """`allowed_roots` wins an overlap: a path the descriptor
        explicitly permits is never also a protected write."""
        self.init_workspace()
        descriptor = json.loads(self.descriptor_path.read_text())
        descriptor["write_set"]["protected_roots"].append("rust/*/src/**")
        self.descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n")
        self.write("rust/date-creusot-core/src/lib.rs", "pub fn ok() {}\n")
        code, doc = self.write_set_check()
        self.assertEqual(code, 0)
        self.assertEqual(doc["write_set"]["state"], "clean")

    def test_a_file_nested_below_a_protected_specs_dir_is_audited_not_violated(self):
        """The one boundary of the decidable half. The artifact
        discriminators disagree about depth -- `_discover_artifact_files`
        reads one level deep, `validate_interaction.find_interaction_files`
        walks any depth deliberately -- so a file nested below `_boundaries/`
        cannot be attributed, and is neither passed in silence nor called a
        breach this check cannot prove."""
        self.init_workspace()
        self.write("rust/date-creusot-core/specs/_boundaries/nested/PROBE.json", "{}")
        code, doc = self.write_set_check()
        self.assertEqual(code, 0)
        self.assertEqual(
            doc["write_set"]["protected_unvouched"],
            ["rust/date-creusot-core/specs/_boundaries/nested/PROBE.json"],
        )

    def test_an_unrecognized_file_under_a_protected_specs_dir_is_a_violation(self):
        """Not only `{}`: a file the artifact system does not recognize as
        any declared kind is a protected write, whichever suffix it
        carries."""
        self.init_workspace()
        self.write("rust/date-creusot-core/specs/_boundaries/NOTES.txt", "notes\n")
        self.write("rust/date-creusot-core/specs/_interactions/yaml.json", "not: a mapping\n")
        code, doc = self.write_set_check()
        self.assertEqual(code, 1)
        self.assertEqual(
            sorted(v["path"] for v in doc["write_set"]["violations"]),
            [
                "rust/date-creusot-core/specs/_boundaries/NOTES.txt",
                "rust/date-creusot-core/specs/_interactions/yaml.json",
            ],
        )

    def test_the_ownership_manifest_vouches_for_itself(self):
        """It records the managed set and so cannot vouch for its own path
        out of it -- an initialized workspace must still be clean."""
        self.init_workspace()
        code, doc = self.write_set_check()
        self.assertEqual(code, 0)
        self.assertNotIn("ci/manifest/installation.json", doc["write_set"]["violations"])


class ProtectedSurfaceReportTest(WriteSetFixture):
    """chainlink #103's third suggestion: a `clean` verdict must be
    distinguishable from one where the check found nothing to look at, so
    the declared protected surface is reported per pattern."""

    def surface(self, doc) -> dict[str, dict]:
        return {entry["pattern"]: entry for entry in doc["write_set"]["protected_surface"]}

    def test_every_declared_pattern_is_reported(self):
        self.init_workspace()
        code, doc = self.write_set_check()
        self.assertEqual(
            sorted(self.surface(doc)),
            sorted(PILOT_WRITE_SET["protected_roots"]),
        )

    def test_a_pattern_matching_no_file_is_named_as_such(self):
        self.init_workspace()
        code, doc = self.write_set_check()
        entry = self.surface(doc)["rust/*/specs/**"]
        self.assertEqual(entry["files"], 0)
        self.assertIn("rust/*/specs/**", doc["write_set"]["details"])
        self.assertIn("matching no file", doc["write_set"]["details"])
        code, out = self.run_cli("write-set-check")
        self.assertIn("protected_root (matched no file): rust/*/specs/**", out)

    def test_a_patterns_counts_name_its_violations_and_unattributed_files(self):
        self.init_workspace()
        self.write("rust/date-creusot-core/specs/_boundaries/PROBE.json", "{}")
        self.write("scripts/evil.py", "echo rogue\n")
        code, doc = self.write_set_check()
        specs = self.surface(doc)["rust/*/specs/**"]
        self.assertEqual((specs["files"], specs["violations"], specs["unattributed"]), (1, 1, 0))
        scripts = self.surface(doc)["scripts/**"]
        # scripts/witness_renderer.py and scripts/xml_escape.py are
        # product-managed; evil.py is the project's-own-or-agent's file the
        # check cannot attribute.
        self.assertEqual((scripts["files"], scripts["violations"], scripts["unattributed"]), (3, 0, 1))

    def test_the_clean_verdict_states_what_it_does_not_cover(self):
        self.init_workspace()
        self.write("scripts/evil.py", "echo rogue\n")
        code, doc = self.write_set_check()
        self.assertEqual(doc["write_set"]["state"], "clean")
        self.assertIn("audit only", doc["write_set"]["details"])
        self.assertIn("does not cover them", doc["write_set"]["details"])


class ProtectedSurfaceAuditTest(WriteSetFixture):
    """Files inside protected_roots that nothing vouches for, in locations
    the pipeline writes nothing into -- the report's 'no command reports a
    file ... inside write_set.protected_roots' half. Reported, deliberately
    non-blocking: the check cannot distinguish a project's own protected
    files from an agent's intrusion into a protected area. chainlink #103
    closes the decidable half (ProtectedWriteTest) and leaves this half
    labelled rather than silent."""

    def test_unvouched_protected_file_is_reported_without_blocking(self):
        self.init_workspace()
        self.write("scripts/evil.py", "echo rogue\n")
        code, doc = self.write_set_check()
        self.assertEqual(code, 0)
        self.assertEqual(doc["write_set"]["state"], "clean")
        self.assertEqual(doc["write_set"]["protected_unvouched"], ["scripts/evil.py"])
        self.assertIn("audit only", doc["write_set"]["details"])

    def test_protected_file_the_product_managed_is_not_unvouched(self):
        self.init_workspace()
        # ci/manifest/installation.json is under protected root
        # ci/manifest/** AND product-managed -- accounted for either way
        code, doc = self.write_set_check()
        self.assertEqual(code, 0)
        self.assertNotIn("ci/manifest/installation.json", doc["write_set"]["protected_unvouched"])

    def test_protected_spec_artifact_is_not_unvouched(self):
        self.init_workspace()
        self.write(
            "rust/date-creusot-core/specs/_boundaries/a__to__b.json",
            json.dumps({"boundary_id": "a__to__b"}),
        )
        code, doc = self.write_set_check()
        self.assertEqual(code, 0)
        self.assertEqual(doc["write_set"]["protected_unvouched"], [])

    def test_unvouched_protected_file_does_not_block_check(self):
        self.init_workspace()
        self.write("scripts/evil.py", "echo rogue\n")
        code, doc = self.check()
        self.assertEqual(code, 0)
        self.assertValid(CONSOLIDATED_CHECK_SCHEMA, doc, "consolidated-check")


class VacuousWriteSetTest(WriteSetFixture):
    """The descriptor-shape matrix from the report: the schema accepts
    shapes that declare no enforcement boundary at all."""

    def test_allowed_roots_matching_every_path_is_a_violation(self):
        self.init_workspace()
        descriptor = json.loads(self.descriptor_path.read_text())
        descriptor["write_set"]["allowed_roots"] = ["**"]
        self.descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n")
        code, doc = self.write_set_check()
        self.assertEqual(code, 1)
        self.assertEqual(doc["write_set"]["state"], "violations")
        self.assertTrue(any("**" in v["reason"] for v in doc["write_set"]["violations"]))

    def test_empty_protected_roots_is_a_violation(self):
        self.init_workspace()
        descriptor = json.loads(self.descriptor_path.read_text())
        descriptor["write_set"]["protected_roots"] = []
        self.descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n")
        code, doc = self.write_set_check()
        self.assertEqual(code, 1)
        self.assertEqual(doc["write_set"]["state"], "violations")
        self.assertTrue(any("protected_roots" in v["reason"] for v in doc["write_set"]["violations"]))

    def test_empty_protected_roots_also_unaccounts_previously_protected_files(self):
        """With protected_roots emptied, the pilot's own protected files are
        outside every allowed root and unaccounted -- out-of-set, not merely
        unvouched."""
        self.init_workspace()
        self.write("scripts/build.sh", "#!/bin/sh\n")
        descriptor = json.loads(self.descriptor_path.read_text())
        descriptor["write_set"]["protected_roots"] = []
        self.descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n")
        code, doc = self.write_set_check()
        self.assertEqual(code, 1)
        paths = [v["path"] for v in doc["write_set"]["violations"]]
        self.assertIn("scripts/build.sh", paths)


class PortSourceExclusionTest(WriteSetFixture):
    """The pinned upstream checkout is a declared input the agent never
    writes -- the write set exists to keep implementation AWAY from it."""

    def test_upstream_checkout_files_are_excluded(self):
        self.init_workspace()
        self.write("upstream/main.c", "int main() { return 0; }\n")
        self.write("upstream/evil.rs", "pub fn evil() {}\n")
        code, doc = self.write_set_check()
        self.assertEqual(code, 0)
        self.assertEqual(doc["write_set"]["state"], "clean")

    def test_upstream_checkout_excluded_even_when_unprotected(self):
        """The exclusion does not depend on protected_roots naming the
        checkout: port_source.repository declares it a pinned input."""
        self.init_workspace()
        descriptor = json.loads(self.descriptor_path.read_text())
        descriptor["write_set"]["protected_roots"] = [
            p for p in descriptor["write_set"]["protected_roots"] if p != "rust/*/specs/**"
        ]
        self.descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n")
        self.write("upstream/main.c", "int main() { return 0; }\n")
        code, doc = self.write_set_check()
        self.assertEqual(code, 0)
        self.assertEqual(doc["write_set"]["state"], "clean")

    def test_url_port_source_excludes_nothing(self):
        self.init_workspace()
        descriptor = json.loads(self.descriptor_path.read_text())
        descriptor["port_source"]["repository"] = "https://github.com/example/date-creusot-cpp.git"
        self.descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n")
        self.write("upstream/main.c", "int main() { return 0; }\n")
        code, doc = self.write_set_check()
        self.assertEqual(code, 1)
        paths = [v["path"] for v in doc["write_set"]["violations"]]
        self.assertIn("upstream/main.c", paths)


class WriteSetDescriptorStateTest(WriteSetFixture):
    def test_absent_descriptor_is_unknown_exit_2(self):
        code, doc = self.write_set_check()
        self.assertEqual(code, 2)
        self.assertEqual(doc["descriptor"]["state"], "absent")
        self.assertEqual(doc["write_set"]["state"], "unknown")

    def test_invalid_descriptor_is_unknown_exit_2(self):
        self.init_workspace()
        descriptor = json.loads(self.descriptor_path.read_text())
        descriptor["mode"] = "bogus-mode"
        self.descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n")
        code, doc = self.write_set_check()
        self.assertEqual(code, 2)
        self.assertEqual(doc["descriptor"]["state"], "present-invalid")
        self.assertEqual(doc["write_set"]["state"], "unknown")

    def test_check_fails_closed_on_absent_descriptor(self):
        # An absent descriptor makes gate_integrity `unknown`, and the
        # exit-code contract's precedence puts gate_integrity_failed (5)
        # above invalid_input (2) -- the check's own basis cannot be
        # trusted, so that outranks everything (chainlink #75).
        code, doc = self.check()
        self.assertEqual(code, 5)
        self.assertIn("invalid_input", doc["result"]["conditions"])
        self.assertIn("gate_integrity_failed", doc["result"]["conditions"])


class WriteSetUnitTest(unittest.TestCase):
    """Direct unit tests of scripts/write_set.py's own helpers."""

    def test_matches_every_path(self):
        self.assertTrue(write_set._matches_every_path("**"))
        self.assertTrue(write_set._matches_every_path("*/**"))
        self.assertTrue(write_set._matches_every_path("*/**/"))
        self.assertFalse(write_set._matches_every_path("crates/*/src/"))
        self.assertFalse(write_set._matches_every_path("*"))
        self.assertFalse(write_set._matches_every_path("rust/*/specs/**"))

    def test_is_canonical(self):
        self.assertTrue(write_set._is_canonical("ci/manifest/installation.json"))
        self.assertTrue(write_set._is_canonical("ci/results/c_static/target.json"))
        self.assertTrue(write_set._is_canonical("evidence/E-0001.json"))
        self.assertTrue(write_set._is_canonical("specs/_closure/date-creusot.json"))
        self.assertTrue(write_set._is_canonical("docs/witnesses/_contact_sheet.svg"))
        self.assertFalse(write_set._is_canonical("docs/reliance-policy.md"))
        self.assertFalse(write_set._is_canonical("rust/date-creusot-core/src/lib.rs"))
        # crate-level underscore dirs are the declared spec tree's, not a
        # workspace-level canonical location
        self.assertFalse(write_set._is_canonical("rust/date-creusot-core/specs/_boundaries/a.json"))

    def test_is_staged_draft_requires_an_artifact_directory(self):
        dirs = {"rust/date-creusot-core/specs/_boundaries", "ci/manifest"}
        self.assertTrue(write_set._is_staged_draft("rust/date-creusot-core/specs/_boundaries/a.json.draft", dirs))
        self.assertTrue(write_set._is_staged_draft("ci/manifest/WP-1.yaml.draft", dirs))
        # wrong directory, wrong staged suffix, wrong base suffix
        self.assertFalse(write_set._is_staged_draft("scripts/evil.py.draft", dirs))
        self.assertFalse(write_set._is_staged_draft("rust/date-creusot-core/specs/_boundaries/a.json", dirs))
        self.assertFalse(write_set._is_staged_draft("rust/date-creusot-core/specs/_boundaries/a.txt.draft", dirs))
        self.assertFalse(write_set._is_staged_draft("a.json.draft", dirs))

    def test_walk_files_prunes_vcs_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".git").mkdir()
            (workspace / ".git" / "config").write_text("[core]\n")
            (workspace / "src").mkdir()
            (workspace / "src" / "lib.rs").write_text("pub fn ok() {}\n")
            (workspace / "outside.txt").write_text("outside\n")
            (workspace / "src" / "linked.txt").symlink_to(workspace / "outside.txt")
            rels = write_set._walk_files(workspace)
            # VCS internals pruned, symlinks skipped (a symlink can point
            # outside the workspace -- hashing it would be checking
            # someone else's file)
            self.assertNotIn(".git/config", rels)
            self.assertNotIn("src/linked.txt", rels)
            self.assertIn("src/lib.rs", rels)


class GateIntegrityEscapeTest(WriteSetFixture):
    """chainlink #77's companion evidence on #74: a descriptor-level
    gate_integrity path resolving outside the workspace must be refused,
    not hashed against someone else's file."""

    def test_absolute_path_outside_the_workspace_is_refused(self):
        self.init_workspace()
        outside = self.workspace.parent / "outside-secret.txt"
        outside.write_text("secret\n")
        try:
            descriptor = json.loads(self.descriptor_path.read_text())
            descriptor["gate_integrity"] = [{"path": str(outside)}]
            self.descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n")
            code, doc = self.status()
            self.assertEqual(code, 0)
            self.assertEqual(doc["gate_integrity"]["state"], "drifted")
            self.assertIn("escapes the workspace", doc["gate_integrity"]["details"])
        finally:
            outside.unlink()

    def test_outside_file_with_matching_recorded_hash_does_not_pass(self):
        """The fail-open this closes: the outside file's hash recorded in
        the manifest used to attest the gate as `pinned` while checking
        someone else's file."""
        self.init_workspace()
        outside = self.workspace.parent / "outside-secret.txt"
        outside.write_text("secret\n")
        try:
            import hashlib

            manifest = json.loads(
                (self.workspace / "ci" / "manifest" / "installation.json").read_text()
            )
            manifest["gate_hashes"][str(outside)] = "sha256:" + hashlib.sha256(b"secret\n").hexdigest()
            (self.workspace / "ci" / "manifest" / "installation.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n"
            )
            descriptor = json.loads(self.descriptor_path.read_text())
            descriptor["gate_integrity"] = [{"path": str(outside)}]
            self.descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n")
            code, doc = self.status()
            self.assertEqual(doc["gate_integrity"]["state"], "drifted")
            self.assertIn("escapes the workspace", doc["gate_integrity"]["details"])
        finally:
            outside.unlink()

    def test_traversal_path_outside_the_workspace_is_refused(self):
        self.init_workspace()
        descriptor = json.loads(self.descriptor_path.read_text())
        descriptor["gate_integrity"] = [{"path": "../outside-gate.py"}]
        self.descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n")
        code, doc = self.status()
        self.assertEqual(doc["gate_integrity"]["state"], "drifted")
        self.assertIn("escapes the workspace", doc["gate_integrity"]["details"])


if __name__ == "__main__":
    unittest.main()
