"""Neargye Mode-P release-candidate acceptance (chainlink #60).

The acceptance boundary between #53 (the metaharness epic) and #52 (the real
Mode-P port): the released artifact, run with the ligature-workspace source
checkout unavailable to it, must initialize a real Neargye workspace
target and prove the loop driver
surfaces a real problem there -- not just in a synthetic temp fixture.

Like tests/test_zipapp_out_of_checkout.py, this builds the real
`scripts/build_zipapp.py --out dist` artifact, copies ONLY the `.pyz` into a
directory outside the repository, and runs it with `python3 -I` from a cwd
outside the checkout. Unlike that #57 smoke test, this goes further: the
eleven-verb grammar, the bundled-resource inventory, a not-yet-initialized
`status`/`check`, the real port init on Neargye (idempotence, ownership/base
hashes, skill authority pin, `@adjudicator` binary pin), a deliberate G21
failure surfaced through `check`, a conflict/recovery cycle, and independent
inspection of the release payload against the inventory's `never_release`
list.

Set `LIGATURE_NEARGYE_WORKSPACE` to the (empty or already
Ligature-initialized) target; the class skips if it is unset, if the
directory is absent, or if it contains unrecognized content. The real port itself (SemVer source pinning,
oracles, differential tests) is #52, explicitly out of scope here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import adjudicator  # noqa: E402
import assumption_registry as ar  # noqa: E402
import ligature_install  # noqa: E402

_NEARGYE_ENV = os.environ.get("LIGATURE_NEARGYE_WORKSPACE")
NEARGYE_WORKSPACE = Path(_NEARGYE_ENV).expanduser() if _NEARGYE_ENV else None
CONTRACT_PATH = ROOT / "docs" / "cli-contract.md"
INVENTORY_PATH = ROOT / "docs" / "implementation-inventory.json"
DIST = ROOT / "dist"
BUNDLE_DISPOSITIONS = frozenset({"runtime-required", "installed-template"})

_CONTRACT_VERBS = {
    "init", "doctor", "version", "status", "check",
    "validate", "gate", "draft", "approve", "migrate", "report",
}
_MANAGED = (
    ".codex/skills/ligature/SKILL.md",
    ".ligature/schemas/project-state.schema.json",
    ".ligature/schemas/consolidated-check.schema.json",
    ".ligature/prompts/stage-0-evidence-intake.md",
    ".ligature/prompts/stage-3-boundary-drafting.md",
    ".ligature/prompts/stage-3-interaction-drafting.md",
)
_USER_OWNED = ("project-descriptor.json", "docs/reliance-policy.md")


def _run(artifact: Path, cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", str(artifact), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _archive_members(pyz: Path) -> dict[str, str]:
    with zipfile.ZipFile(pyz) as zf:
        return {
            name: "sha256:" + hashlib.sha256(zf.read(name)).hexdigest()
            for name in zf.namelist()
            if not name.endswith("/")
        }


def _expected_bundle_members(inventory: dict) -> set[str]:
    """Re-derive the bundle independently of build_zipapp.py: read the
    inventory's dispositions here, in the test, and expect exactly this set
    in the archive (plus the attestation, which is checked separately)."""
    members: set[str] = set()
    for module in inventory["python_modules"]:
        if module["disposition"] in BUNDLE_DISPOSITIONS:
            members.add(Path(module["path"]).name)
    for key in ("schemas", "schema_examples", "prompts", "documentation_and_templates"):
        for entry in inventory[key]:
            if entry["disposition"] in BUNDLE_DISPOSITIONS:
                members.add(f"ligature_data/{entry['path']}")
    for asset in inventory["vendored_runtime_assets"]:
        if asset.get("required_at_runtime"):
            members.add(f"ligature_data/{asset['path']}")
    members |= {"__main__.py", "ligature_data/__init__.py"}
    return members


def _contract_verbs() -> list[str]:
    text = CONTRACT_PATH.read_text()
    section = text.split("## 1. Grammar", 1)[1].split("\n## 2.", 1)[0]
    block = section.split("```", 2)[1]
    return list(dict.fromkeys(re.findall(r"^ligature\s+([a-z][a-z0-9-]*)", block, re.M)))


def _forbidden_release_tokens(inventory: dict) -> set[str]:
    """Concrete substrings from never_release patterns; prose patterns
    (ones containing spaces, e.g. the 'any user-authored descriptor' entry)
    are not machine-checkable and are skipped."""
    tokens: set[str] = set()
    for entry in inventory["never_release"]:
        pattern = entry["pattern"]
        for token in pattern.split(","):
            token = token.strip().replace("*", "").rstrip("/")
            if not token or " " in token:
                continue
            tokens.add(token)
    return tokens


@unittest.skipUnless(
    NEARGYE_WORKSPACE is not None and NEARGYE_WORKSPACE.is_dir(),
    f"Neargye acceptance workspace unset or absent: {NEARGYE_WORKSPACE} (set LIGATURE_NEARGYE_WORKSPACE)",
)
class NeargyePortAcceptanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        build = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "build_zipapp.py"), "--out", str(DIST)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        if build.returncode != 0:
            raise RuntimeError("release build failed:\n" + build.stdout + build.stderr)

        cls.inventory = json.loads(INVENTORY_PATH.read_text())
        cls.provenance = json.loads((DIST / "PROVENANCE.json").read_text())
        cls.content_hash = cls.provenance["content_hash"]
        cls.expected_members = _expected_bundle_members(cls.inventory)

        cls._outside_tmp = tempfile.TemporaryDirectory()
        cls.outside = Path(cls._outside_tmp.name).resolve()
        cls.artifact = cls.outside / "ligature.pyz"
        shutil.copyfile(DIST / "ligature.pyz", cls.artifact)
        cls.members = _archive_members(cls.artifact)

        cls.workspace = NEARGYE_WORKSPACE
        entries = list(cls.workspace.iterdir())
        recognizable = (
            (cls.workspace / ".ligature").is_dir()
            or (cls.workspace / "ci" / "manifest" / "installation.json").is_file()
            or (cls.workspace / "project-descriptor.json").is_file()
        )
        if entries and not recognizable:
            raise unittest.SkipTest(
                f"{cls.workspace} contains unrecognized content; refusing to treat it as the acceptance target"
            )
        cls.init_output = cls._ensure_initialized()

    @classmethod
    def tearDownClass(cls):
        cls._outside_tmp.cleanup()

    # -- helpers -----------------------------------------------------------
    @classmethod
    def _init_port(cls) -> subprocess.CompletedProcess:
        return _run(cls.artifact, cls.outside, "--workspace", str(cls.workspace), "init", "--mode", "port")

    @classmethod
    def _ensure_initialized(cls) -> subprocess.CompletedProcess:
        proc = cls._init_port()
        if proc.returncode != 0 or "CONFLICT" in proc.stdout:
            for path in re.findall(r"CONFLICT\s+(\S+)", proc.stdout):
                _run(cls.artifact, cls.outside, "--workspace", str(cls.workspace), "migrate", "--force", path)
            # This test's freshly built artifact can differ from whichever
            # binary last initialized the workspace. Since chainlink #65
            # that rerun is an explicit `adjudicator pin mismatch` conflict
            # rather than a silent re-pin, so re-pinning this real
            # workspace to the artifact under test is done deliberately.
            _run(cls.artifact, cls.outside, "--workspace", str(cls.workspace), "migrate", "--upgrade")
            proc = cls._init_port()
        doctor = _run(cls.artifact, cls.outside, "--workspace", str(cls.workspace), "doctor")
        if doctor.returncode != 0:
            raise RuntimeError(
                "Neargye workspace is not current after init:\n" + doctor.stdout + doctor.stderr
            )
        return proc

    def _doctor(self) -> subprocess.CompletedProcess:
        return _run(self.artifact, self.outside, "--workspace", str(self.workspace), "doctor")

    def _status_json(self) -> dict:
        proc = _run(self.artifact, self.outside, "--workspace", str(self.workspace), "status", "--json")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def _manifest(self) -> dict:
        return json.loads((self.workspace / "ci" / "manifest" / "installation.json").read_text())

    # -- 1/2: executable identity and stable grammar -----------------------
    def test_01_version_verify_and_doctor_out_of_checkout(self):
        proc = _run(self.artifact, self.outside, "version", "--verify")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("attested: true", proc.stdout)
        self.assertIn(self.content_hash, proc.stdout)
        self.assertIn(str(self.artifact), proc.stdout)
        self.assertNotIn(str(ROOT), proc.stdout)

        doctor = self._doctor()
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        self.assertIn("attested: true", doctor.stdout)
        self.assertIn("adjudicator pin: pinned", doctor.stdout)

    def test_02_help_grammar_matches_the_frozen_contract(self):
        verbs = _contract_verbs()
        self.assertEqual(len(verbs), 11, verbs)
        self.assertEqual(set(verbs), _CONTRACT_VERBS)

        top = _run(self.artifact, self.outside, "--help")
        self.assertEqual(top.returncode, 0, top.stdout + top.stderr)
        for verb in verbs:
            self.assertIn(verb, top.stdout, f"{verb!r} missing from top-level --help")
            help_proc = _run(self.artifact, self.outside, verb, "--help")
            self.assertEqual(help_proc.returncode, 0, f"{verb} --help:\n{help_proc.stdout}{help_proc.stderr}")

    # -- 2: bundled-resource inventory ------------------------------------
    def test_03_bundled_resource_inventory_matches_the_inventory(self):
        self.assertIn(adjudicator.ATTESTATION_ARCNAME, self.members)
        actual = set(self.members) - {adjudicator.ATTESTATION_ARCNAME}
        self.assertEqual(actual - self.expected_members, set(), "extra archive members")
        self.assertEqual(self.expected_members - actual, set(), "missing archive members")

        expected_schemas = {
            member[len("ligature_data/"):]
            for member in self.expected_members
            if member.startswith("ligature_data/") and member.endswith(".json") and "schema" in Path(member).name
        }

        doctor = self._doctor()
        self.assertIn("attested: true", doctor.stdout)
        count_match = re.search(r"^\s*bundle:\s*(\d+)\s+file", doctor.stdout, re.M)
        self.assertIsNotNone(count_match, doctor.stdout[-2000:])
        self.assertEqual(int(count_match.group(1)), len(self.expected_members))
        schema_count = re.search(r"^\s*bundled schemas:\s*(\d+)\b", doctor.stdout, re.M)
        self.assertIsNotNone(schema_count, doctor.stdout[-2000:])
        self.assertEqual(int(schema_count.group(1)), len(expected_schemas))
        after_header = doctor.stdout.split("bundled schemas:", 1)[1]
        reported = set(re.findall(r"^\s+(\S+\.json):\s", after_header, re.M))
        self.assertEqual(reported, expected_schemas)

        self.assertEqual(self.provenance["bundle_file_count"], len(self.expected_members))

    # -- 2: not-yet-initialized status/check -------------------------------
    def test_04_status_and_check_on_an_uninitialized_workspace(self):
        with tempfile.TemporaryDirectory(dir=self.outside) as empty:
            status = _run(self.artifact, self.outside, "--workspace", empty, "status", "--json")
            self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
            document = json.loads(status.stdout)
            self.assertEqual(document["schema_version"], "1.0")
            self.assertEqual(document["descriptor"]["state"], "absent")
            self.assertEqual(document["installation_manifest"]["state"], "not-initialized")
            self.assertEqual(document["binary_identity"]["verified"], "true")

            check = _run(self.artifact, self.outside, "--workspace", empty, "check", "--json")
            self.assertEqual(check.returncode, 2, check.stdout + check.stderr)
            result = json.loads(check.stdout)
            self.assertFalse(result["mutated_workspace"])
            self.assertIn("invalid_input", result["result"]["conditions"])
            self.assertIsNone(result["next_action"])
            self.assertTrue(result["gates"])

            next_only = _run(self.artifact, self.outside, "--workspace", empty, "check", "next")
            self.assertEqual(next_only.returncode, 2, next_only.stdout + next_only.stderr)
            self.assertIn("next action", next_only.stdout)

            human = _run(self.artifact, self.outside, "--workspace", empty, "check")
            self.assertEqual(human.returncode, 2, human.stdout + human.stderr)

    # -- 3: real port init, idempotence, ownership, authority, pin ---------
    def test_05_port_init_is_idempotent_and_records_ownership_and_identity(self):
        proc = self._init_port()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("installation: current", proc.stdout)
        self.assertEqual(proc.stdout.count("\n  unchanged"), len(_MANAGED), proc.stdout)
        self.assertNotIn("\n     create", proc.stdout)
        self.assertIn("skill authority hash: verified", proc.stdout)

        manifest = self._manifest()
        records = {record["path"]: record for record in manifest["files"]}
        for rel in _MANAGED:
            record = records[rel]
            self.assertEqual(record["ownership"], "managed", rel)
            self.assertEqual(record["base_hash"], record["expected_hash"], rel)
            self.assertEqual(record["base_hash"], "sha256:" + _sha256_file(self.workspace / rel), rel)
        for rel in _USER_OWNED:
            self.assertEqual(records[rel]["ownership"], "user", rel)

        skill_text = (self.workspace / ".codex/skills/ligature/SKILL.md").read_text()
        self.assertEqual(
            ligature_install.declared_authority_hash(skill_text),
            ligature_install.computed_authority_hash(skill_text),
        )
        self.assertEqual(
            records[".codex/skills/ligature/SKILL.md"]["authority_hash"],
            ligature_install.computed_authority_hash(skill_text),
        )

        self.assertEqual(manifest["installed_product_version"], self.provenance["product_version"])
        self.assertEqual(manifest["adjudicator"]["kind"], "zipapp")
        self.assertEqual(manifest["adjudicator"]["content_hash"], self.content_hash)
        self.assertEqual(manifest["gate_hashes"]["@adjudicator"], self.content_hash)

        doctor = self._doctor()
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        self.assertIn("skill authority hash: verified", doctor.stdout)
        self.assertIn("adjudicator pin: pinned", doctor.stdout)

        document = self._status_json()
        self.assertEqual(document["gate_integrity"]["state"], "pinned")
        self.assertEqual(document["installation_manifest"]["state"], "current")
        self.assertEqual(document["binary_identity"]["verified"], "true")
        self.assertEqual(document["binary_identity"]["content_hash"], self.content_hash)

    # -- 4: a deliberate failing gate, surfaced by check -------------------
    def test_06_check_surfaces_a_deliberate_g21_failure(self):
        registry_dir = self.workspace / "specs" / "_assumptions"
        registry_path = registry_dir / "registry.json"
        manifest_path = self.workspace / "ci" / "manifest" / "WP-DELIBERATE.json"
        try:
            text = "The acceptance target has exactly one deliberately dangling assumption."
            registry_dir.mkdir(parents=True, exist_ok=True)
            registry_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "entries": [
                            {
                                "assumption_id": "ASM-present",
                                "canonical_text": text,
                                "content_hash": ar.canonical_assumption_hash(text),
                                "tracking_issue": "chainlink:713",
                                "provenance": [{"action": "added", "actor": "acceptance", "at": "2026-09-18"}],
                            }
                        ],
                    },
                    indent=2,
                )
            )
            manifest_path.write_text(
                json.dumps(
                    {
                        "work_package": "WP-DELIBERATE",
                        "definition_of_done": {
                            "trusted_assumptions": [
                                {
                                    "assumption_ref": {"assumption_id": "ASM-does-not-exist"},
                                    "risk": "high",
                                    "mitigations": [{"kind": "human-risk-acceptance", "reference": "X"}],
                                }
                            ]
                        },
                    },
                    indent=2,
                )
            )

            proc = _run(self.artifact, self.outside, "--workspace", str(self.workspace), "check", "--json")
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            document = json.loads(proc.stdout)
            self.assertFalse(document["mutated_workspace"])
            self.assertNotEqual(document["result"]["exit_code"], 0)
            g21 = [finding for finding in document["findings"] if finding["gate_id"] == "G21"]
            self.assertTrue(g21, document["findings"])
            self.assertTrue(any("ASM-does-not-exist" in finding["reason"] for finding in g21))
            self.assertIsNotNone(document["next_action"], "check must recommend a concrete next action")
        finally:
            registry_path.unlink(missing_ok=True)
            manifest_path.unlink(missing_ok=True)
            try:
                registry_dir.rmdir()
            except OSError:
                pass

    # -- 5: conflict detection and recovery --------------------------------
    def test_07_managed_conflict_is_detected_and_recovered(self):
        rel = ".ligature/schemas/project-state.schema.json"
        target = self.workspace / rel
        original = target.read_text()
        try:
            target.write_text(original + "\n// local edit\n")

            doctor = self._doctor()
            self.assertNotEqual(doctor.returncode, 0, doctor.stdout)
            self.assertIn("installation: conflict", doctor.stdout)
            self.assertIn("CONFLICT", doctor.stdout)

            document = self._status_json()
            self.assertEqual(document["gate_integrity"]["state"], "drifted")

            recovery = _run(self.artifact, self.outside, "--workspace", str(self.workspace), "migrate", "--force", rel)
            self.assertEqual(recovery.returncode, 0, recovery.stdout + recovery.stderr)
            self.assertIn("installation: current", recovery.stdout)

            doctor = self._doctor()
            self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
            self.assertIn("installation: current", doctor.stdout)
            self.assertEqual(self._status_json()["gate_integrity"]["state"], "pinned")
        finally:
            if target.read_text() != original:
                target.write_text(original)

    # -- 6: release payload inspection -------------------------------------
    def test_08_release_payload_excludes_never_release_and_is_consistent(self):
        for name in ("ligature.pyz", "SHA256SUMS", "PROVENANCE.json", "NOTICE", "THIRD_PARTY_NOTICES.txt"):
            self.assertTrue((DIST / name).is_file(), name)

        sums = (DIST / "SHA256SUMS").read_text().split()
        self.assertEqual(len(sums), 2)
        self.assertEqual(sums[1], "ligature.pyz")
        self.assertEqual(sums[0], _sha256_file(DIST / "ligature.pyz"))
        self.assertEqual(self.provenance["artifact_sha256"], "sha256:" + sums[0])
        self.assertEqual(self.provenance["content_hash"], self.content_hash)

        attestation = json.loads(
            zipfile.ZipFile(self.artifact).read(adjudicator.ATTESTATION_ARCNAME)
        )
        self.assertEqual(attestation["content_hash"], self.content_hash)
        self.assertEqual(attestation["bundle_manifest"].keys() - self.members.keys(), set())
        self.assertEqual(self.members.keys() - set(attestation["bundle_manifest"]) - {adjudicator.ATTESTATION_ARCNAME}, set())

        notice = (DIST / "NOTICE").read_text().lower()
        self.assertIn("ligature", notice)
        third_party = (DIST / "THIRD_PARTY_NOTICES.txt").read_text()
        self.assertIn("MIT License", third_party)
        self.assertIn("concept-to-code", third_party)

        tokens = _forbidden_release_tokens(self.inventory)
        self.assertTrue(tokens)
        for member in self.members:
            for token in tokens:
                self.assertNotIn(token, member, f"never_release token {token!r} appears in archive member {member!r}")

        self.assertIn("ligature_data/vendor/concept-to-code/schemas/spec.schema.json", self.members)
        # no test-only, cache, or local issue-tracker content anywhere
        self.assertFalse([m for m in self.members if m.startswith("tests/") or "__pycache__" in m or m.endswith(".pyc")])


if __name__ == "__main__":
    unittest.main()
