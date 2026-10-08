"""Black-box coverage for typed, authorized Cargo profile configuration."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import tomllib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import pipeline  # noqa: E402
from schema_utils import make_validator  # noqa: E402

LEDGER_REL = "ci/results/protected-writes.jsonl"
ISSUE = 121


class ProfileFixture:
    def setup(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name).resolve()
        self.descriptor_path = self.workspace / "project-descriptor.json"
        self.manifest = self.workspace / "Cargo.toml"
        self.ledger = self.workspace / LEDGER_REL
        code, _, err = self.run("init", "--mode", "port", "--name", "profile-pilot")
        assert code == 0, err
        descriptor = json.loads(self.descriptor_path.read_text())
        descriptor["write_set"] = {
            "allowed_roots": ["rust/*/src/", "rust/*/tests/"],
            "protected_roots": ["Cargo.toml", "**/Cargo.toml", "ci/manifest/**"],
        }
        self.descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n")
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "lib.rs").write_text("pub fn marker() {}\n")

    def teardown(self):
        self._tmp.cleanup()

    def run(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = pipeline.main(
                ["--workspace", str(self.workspace), *args]
            )
        return code, out.getvalue(), err.getvalue()

    def cargo_manifest(self, text: str = ""):
        if not text:
            text = (
                "[package]\n"
                'name = "profile-pilot"\n'
                'version = "0.1.0"\n'
                'edition = "2021"\n'
                "\n"
                "[features]\n"
                "default = [\n"
                '  "alpha",\n'
                "]\n"
                "alpha = []\n"
                "\n"
                "[profile.release] # profile comments stay\n"
                "opt-level = 1 # retain this note\n"
                "debug = false\n"
                "incremental = true\n"
                "\n"
                "[profile.release.package.profile-pilot]\n"
                "opt-level = 2\n"
            )
        self.manifest.write_text(text)
        return text

    def grant(self, *, issue=ISSUE, path="Cargo.toml", one_shot=False, ttl=3600):
        args = [
            "authorize-write",
            "--issue", str(issue),
            "--path", path,
            "--op", "write",
            "--issuer", "reviewer",
            "--ttl", str(ttl),
            "--json",
        ]
        if one_shot:
            args.append("--one-shot")
        code, out, err = self.run(*args)
        assert code == 0, err
        return json.loads(out)["grant_id"]

    def configure(self, *args, issue=ISSUE, json_output=True):
        command = [
            "configure-cargo-profile",
            "--issue", str(issue),
            "--profile", "release",
            *args,
        ]
        if json_output:
            command.append("--json")
        code, out, err = self.run(*command)
        return code, json.loads(out) if out.strip() and json_output else out, err


@pytest.fixture
def workspace():
    fixture = ProfileFixture()
    fixture.setup()
    try:
        yield fixture
    finally:
        fixture.teardown()


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_merges_typed_values_and_preserves_other_cargo_toml_bytes(workspace):
    original = workspace.cargo_manifest()
    grant = workspace.grant(one_shot=True)

    code, record, err = workspace.configure(
        "--grant-id", grant,
        "--opt-level", "3",
        "--lto", "thin",
        "--no-incremental",
        "--codegen-units", "1",
    )

    assert code == 0, err
    expected = original.replace("opt-level = 1 # retain this note", "opt-level = 3 # retain this note")
    expected = expected.replace("[profile.release] # profile comments stay\n", "[profile.release] # profile comments stay\nlto = \"thin\"\ncodegen-units = 1\n")
    expected = expected.replace("incremental = true", "incremental = false")
    assert workspace.manifest.read_text() == expected
    parsed = tomllib.loads(expected)
    assert parsed["profile"]["release"]["opt-level"] == 3
    assert parsed["profile"]["release"]["lto"] == "thin"
    assert parsed["profile"]["release"]["incremental"] is False
    assert parsed["profile"]["release"]["debug"] is False
    assert parsed["profile"]["release"]["package"]["profile-pilot"]["opt-level"] == 2
    assert parsed["features"] == {"default": ["alpha"], "alpha": []}
    assert not (workspace.workspace / "Cargo.lock").exists()

    schema = json.loads((ROOT / "docs/cargo-profile-audit-schema.json").read_text())
    assert not list(make_validator(schema).iter_errors(record))
    assert record["event"] == "cargo-profile-configuration"
    assert record["issue"] == ISSUE
    assert record["grant_id"] == grant
    assert record["profile"] == "release"
    assert record["changes"] == {
        "opt-level": 3,
        "lto": "thin",
        "incremental": False,
        "codegen-units": 1,
    }
    assert set(record["validation"].values()) == {
        "passed", "cargo metadata --no-deps --format-version 1 --offline"
    }
    assert "git_commit" not in record and "diff" not in record
    assert "before_hash" not in record and "after_hash" not in record
    assert len(_events(workspace.ledger)) == 2


def test_new_profile_is_created_without_reformatting_existing_manifest(workspace):
    original = workspace.cargo_manifest().replace("\n[profile.release] # profile comments stay", "")
    original = original.replace("opt-level = 1 # retain this note\n", "")
    original = original.replace("debug = false\n", "")
    original = original.replace("incremental = true\n", "")
    original = original.replace("\n[profile.release.package.profile-pilot]\nopt-level = 2\n", "\n")
    workspace.manifest.write_text(original)
    workspace.grant()

    code, record, err = workspace.configure("--panic", "abort")

    assert code == 0, err
    assert workspace.manifest.read_text() == original + "[profile.release]\npanic = \"abort\"\n"
    assert tomllib.loads(workspace.manifest.read_text())["profile"]["release"] == {"panic": "abort"}
    assert record["result"] == "updated"


def test_unchanged_settings_are_audited_without_rewriting_the_manifest(workspace):
    original = workspace.cargo_manifest()
    grant = workspace.grant(one_shot=True)

    code, first, err = workspace.configure("--grant-id", grant, "--opt-level", "1")
    assert code == 0, err
    after_first = workspace.manifest.read_bytes()
    assert after_first == original.encode()
    assert first["result"] == "unchanged"

    code, second, err = workspace.configure("--grant-id", grant, "--opt-level", "1")
    assert code == 0, err
    assert second["result"] == "unchanged"
    assert workspace.manifest.read_bytes() == after_first
    assert len(_events(workspace.ledger)) == 3


def test_one_shot_grant_cannot_authorize_a_second_manifest_update(workspace):
    workspace.cargo_manifest()
    grant = workspace.grant(one_shot=True)
    code, _, err = workspace.configure("--grant-id", grant, "--opt-level", "3")
    assert code == 0, err
    after_first = workspace.manifest.read_bytes()

    code, refusal, _ = workspace.configure("--grant-id", grant, "--panic", "abort")

    assert code == 2
    assert refusal["error"]["code"] == "grant-replayed"
    assert workspace.manifest.read_bytes() == after_first
    assert len(_events(workspace.ledger)) == 2


@pytest.mark.parametrize(
    "arguments",
    [
        ("--opt-level", "4"),
        ("--codegen-units", "0"),
        ("--panic", "continue"),
        ("--lto", "fast"),
        ("--strip", "everything"),
    ],
)
def test_invalid_typed_values_are_refused_without_mutation(workspace, arguments):
    original = workspace.cargo_manifest()
    workspace.grant()

    code, refusal, _ = workspace.configure(*arguments)

    assert code == 2
    assert refusal["state"] == "refused"
    assert workspace.manifest.read_text() == original
    assert len(_events(workspace.ledger)) == 1


def test_grant_must_be_for_this_issue_and_exact_cargo_manifest(workspace):
    original = workspace.cargo_manifest()
    workspace.grant(issue=ISSUE)

    code, refusal, _ = workspace.configure("--opt-level", "3", issue=ISSUE + 1)
    assert code == 2
    assert refusal["error"]["code"] == "missing-or-invalid-grant"
    assert workspace.manifest.read_text() == original

    workspace.ledger.unlink()
    descriptor = json.loads(workspace.descriptor_path.read_text())
    descriptor["write_set"] = {"allowed_roots": [], "protected_roots": ["Cargo.toml*"]}
    workspace.descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n")
    workspace.grant(path="Cargo.toml*")
    code, refusal, _ = workspace.configure("--opt-level", "3")
    assert code == 2
    assert refusal["error"]["code"] == "missing-or-invalid-grant"
    assert workspace.manifest.read_text() == original


def test_cargo_validation_failure_rolls_back_and_does_not_audit_a_success(workspace):
    original = "[package]\nversion = \"0.1.0\"\nedition = \"2021\"\n"
    workspace.cargo_manifest(original)
    workspace.grant()

    code, refusal, _ = workspace.configure("--opt-level", "3")

    assert code == 2
    assert refusal["error"]["code"] == "cargo-validation-failed"
    assert workspace.manifest.read_text() == original
    assert len(_events(workspace.ledger)) == 1


def test_invalid_existing_multiline_profile_field_is_left_untouched(workspace):
    original = workspace.cargo_manifest().replace(
        "opt-level = 1 # retain this note", "opt-level = [\n  1,\n]"
    )
    workspace.manifest.write_text(original)
    workspace.grant()

    code, refusal, _ = workspace.configure("--opt-level", "3")

    assert code == 2
    assert refusal["error"]["code"] == "unsupported-profile-setting-source"
    assert workspace.manifest.read_text() == original
    assert len(_events(workspace.ledger)) == 1
