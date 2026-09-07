"""Honest empty-scan reporting across every scanning validator (#48).

A validator that discovers zero artifacts used to print the same
confident "OK: all X pass ..." line as one that checked real artifacts
and found them clean. rc stayed 0 either way -- correct, since an empty
workspace is a legitimate state -- so the wording was the only thing a
reader or a CI log had to go on, and it said the wrong thing.

One test per validator here, through the real CLI entrypoints (both the
standalone `main()` and the `pipeline.py` subcommand), because that is
the surface the issue was reported against.
"""
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
import validate_boundary_contracts  # noqa: E402
import validate_boundary_naming  # noqa: E402
import validate_bridge  # noqa: E402
import validate_callsites  # noqa: E402
import validate_closure  # noqa: E402
import validate_conflict_resolution  # noqa: E402
import validate_evidence  # noqa: E402
import validate_gold_set  # noqa: E402
import validate_exemption  # noqa: E402
import validate_interaction  # noqa: E402
import validate_protocol_debt  # noqa: E402
import validate_witness  # noqa: E402
from scan_summary import pass_line  # noqa: E402

EMPTY_PREFIX = "OK (nothing to check: no "

# module, the standalone CLI's own argv, the artifact noun it reports
SCANNING_VALIDATORS = [
    (validate_boundary_contracts, "boundary contracts"),
    (validate_boundary_naming, "boundary artifacts"),
    (validate_bridge, "bridges"),
    (validate_callsites, "C_static reports"),
    (validate_closure, "closure artifacts"),
    (validate_conflict_resolution, "conflict-resolution records"),
    (validate_evidence, "evidence records"),
    (validate_gold_set, "gold sets"),
    (validate_exemption, "exemptions"),
    (validate_interaction, "interactions"),
    (validate_protocol_debt, "protocol-debt records"),
    (validate_witness, "witness artifacts"),
]

# pipeline subcommand -> the artifact noun its pass line reports
PIPELINE_SCANS = {
    "validate": "boundary contracts",
    "validate-interaction": "interactions",
    "validate-exemption": "exemptions",
    "validate-protocol-debt": "protocol-debt records",
    "validate-bridge": "bridges",
    "validate-callsites": "C_static reports",
    "validate-evidence": "evidence records",
    "validate-conflict-resolution": "conflict-resolution records",
    "validate-closure": "closure artifacts",
    "validate-gold-set": "gold sets",
    "validate-witness": "witness artifacts",
}


class PassLineTest(unittest.TestCase):
    def test_zero_discovered_says_nothing_was_checked(self):
        line = pass_line(0, "evidence records", "G1a/G1b", Path("/ws"))
        self.assertEqual(line, "OK (nothing to check: no evidence records discovered under /ws)")

    def test_a_real_pass_carries_the_count_it_was_computed_from(self):
        line = pass_line(3, "evidence records", "G1a/G1b", Path("/ws"))
        self.assertEqual(line, "OK: all evidence records pass G1a/G1b (3 discovered)")

    def test_the_two_outcomes_are_never_the_same_sentence(self):
        self.assertNotEqual(
            pass_line(0, "bridges", "G1a/G1b", "/ws"),
            pass_line(1, "bridges", "G1a/G1b", "/ws"),
        )


class StandaloneCliEmptyScanTest(unittest.TestCase):
    """Every scanning module's own `main()`, over a workspace holding none
    of its artifact kind."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, module, *extra) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = module.main([str(self.workspace), *extra])
        return code, buffer.getvalue()

    def test_every_scanning_validator_reports_an_empty_scan_honestly(self):
        for module, artifacts in SCANNING_VALIDATORS:
            with self.subTest(module=module.__name__):
                code, printed = self._run(module)
                self.assertEqual(code, 0, printed)
                self.assertIn(f"{EMPTY_PREFIX}{artifacts} discovered under", printed)
                self.assertNotIn("OK: all", printed)

    def test_count_discovered_mirrors_each_modules_own_scan(self):
        for module, _ in SCANNING_VALIDATORS:
            with self.subTest(module=module.__name__):
                self.assertEqual(module.count_discovered(self.workspace), 0)


class StandaloneCliNonEmptyScanTest(unittest.TestCase):
    """The other half of the distinction: a scan that did check something
    says how much."""

    def test_a_real_evidence_scan_reports_its_count(self):
        workspace = ROOT / "tests" / "fixtures" / "evidence" / "valid"
        discovered = validate_evidence.count_discovered(workspace)
        self.assertGreater(discovered, 0)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = validate_evidence.main([str(workspace)])
        printed = buffer.getvalue()
        self.assertEqual(code, 0, printed)
        self.assertIn(f"({discovered} discovered)", printed)
        self.assertNotIn(EMPTY_PREFIX, printed)


class PipelineEmptyScanTest(unittest.TestCase):
    """The same distinction through pipeline.py, the surface #48 was
    actually reported against."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        descriptor = json.loads(
            (ROOT / "schemas" / "examples" / "project-descriptor.greenfield.example.json").read_text()
        )
        descriptor["crates"] = [
            {"crate_dir": ".", "contracts_crate": "contracts", "specs_search_root": "specs"}
        ]
        self.descriptor_path = self.workspace / "project-descriptor.json"
        self.descriptor_path.write_text(json.dumps(descriptor))

    def tearDown(self):
        self._tmp.cleanup()

    def test_every_scanning_subcommand_reports_an_empty_workspace_honestly(self):
        for command, artifacts in PIPELINE_SCANS.items():
            with self.subTest(command=command):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    code = pipeline.main([
                        "--workspace", str(self.workspace),
                        "--descriptor", str(self.descriptor_path),
                        command,
                    ])
                printed = buffer.getvalue()
                self.assertEqual(code, 0, printed)
                self.assertIn(f"{EMPTY_PREFIX}{artifacts} discovered under", printed)
                self.assertNotIn("OK: all", printed)


if __name__ == "__main__":
    unittest.main()
