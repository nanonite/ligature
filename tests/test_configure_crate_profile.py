"""Black-box coverage for the authorized crate-scoped Cargo-Verus profile."""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import tomllib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import pipeline  # noqa: E402
from schema_utils import make_validator  # noqa: E402

ISSUE = 125
CRATE_DIR = "rust/demo-core"
MANIFEST_REL = f"{CRATE_DIR}/Cargo.toml"
LEDGER_REL = "ci/results/protected-writes.jsonl"
VERUS_VERSION = "0.2025.10.0"
TOOLCHAIN = "1.88.0-x86_64-unknown-linux-gnu"
VSTD_VERSION = "0.2025.10.0"


class CrateProfileFixture:
    def setup(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name).resolve()
        self.descriptor_path = self.workspace / "project-descriptor.json"
        self.crate = self.workspace / CRATE_DIR
        self.manifest = self.crate / "Cargo.toml"
        self.ledger = self.workspace / LEDGER_REL
        self.bin_dir = self.workspace / "fake-bin"

        code, _, err = self.run("init", "--mode", "port", "--name", "profile-pilot")
        assert code == 0, err
        descriptor = json.loads(self.descriptor_path.read_text())
        descriptor["crates"] = [
            {"crate_dir": CRATE_DIR, "contracts_crate": "contracts", "specs_search_root": "rust"},
            {"crate_dir": "rust/sibling", "contracts_crate": "contracts", "specs_search_root": "rust"},
        ]
        descriptor["write_set"] = {
            "allowed_roots": ["rust/*/src/", "rust/*/tests/"],
            "protected_roots": ["**/Cargo.toml"],
        }
        self.descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n")
        self.crate.mkdir(parents=True)
        self.manifest.write_text(
            "[package]\n"
            'name = "demo-core"\n'
            'version = "0.1.0"\n'
            'edition = "2021"\n'
            "\n"
            "[features]\n"
            'default = ["std"]\n'
            'std = ["dep:serde"]\n'
            "\n"
            "[dependencies]\n"
            'serde = { version = "1", optional = true }\n'
        )
        self.sibling = self.workspace / "rust/sibling/Cargo.toml"
        self.sibling.parent.mkdir(parents=True)
        self.sibling.write_text(
            '[package]\nname = "sibling"\nversion = "0.1.0"\nedition = "2021"\n'
        )
        (self.workspace / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["rust/demo-core", "rust/sibling"]\nresolver = "2"\n'
        )
        self.lock = self.workspace / "Cargo.lock"
        self.lock.write_bytes(b"# preserved lockfile\nversion = 4\n")
        self.install_fake_toolchain()

    def teardown(self):
        self._tmp.cleanup()

    def run(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = pipeline.main(["--workspace", str(self.workspace), "--descriptor", str(self.descriptor_path), *args])
        return code, out.getvalue(), err.getvalue()

    def install_fake_toolchain(self, *, vstd_version=VSTD_VERSION, rust_release="1.88.0"):
        self.bin_dir.mkdir(exist_ok=True)
        cargo = self.bin_dir / "cargo"
        cargo.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys, tomllib\n"
            "args = sys.argv[1:]\n"
            "if args and args[0] == 'metadata':\n"
            "    manifest = pathlib.Path(args[args.index('--manifest-path') + 1]).resolve()\n"
            "    data = tomllib.loads(manifest.read_text())\n"
            "    package = data['package']\n"
            "    print(json.dumps({'workspace_root': str(pathlib.Path.cwd()), 'packages': [{\n"
            "        'name': package['name'], 'version': package['version'],\n"
            "        'id': package['name'] + '@' + package['version'],\n"
              "        'manifest_path': str(manifest), 'metadata': package.get('metadata', {})\n"
              "    }]}))\n"
              "else:\n"
              "    pathlib.Path.cwd().joinpath('Cargo.lock').write_text('# changed by fake cargo\\n')\n"
              "    if args and args[0] == 'check' and pathlib.Path.cwd().joinpath('FAIL_CHECK').exists():\n"
              "        print('injected cargo check failure', file=sys.stderr)\n"
              "        sys.exit(1)\n"
        )
        cargo.chmod(0o755)

        rustc = self.bin_dir / "rustc"
        rustc.write_text(
            "#!/usr/bin/env python3\n"
            "print('rustc 1.88.0 (fake)')\n"
            "print('binary: rustc')\n"
            "print('host: x86_64-unknown-linux-gnu')\n"
            f"print('release: {rust_release}')\n"
            "print('LLVM version: 19.1.0')\n"
        )
        rustc.chmod(0o755)

        verus = self.bin_dir / "verus"
        version_json = json.dumps({"version": VERUS_VERSION, "toolchain": TOOLCHAIN})
        verus.write_text(
            "#!/usr/bin/env python3\n"
            f"print({version_json!r})\n"
        )
        verus.chmod(0o755)
        vstd = self.bin_dir / "vstd"
        vstd.mkdir(exist_ok=True)
        (vstd / "Cargo.toml").write_text(
            f'[package]\nname = "vstd"\nversion = "{vstd_version}"\nedition = "2021"\n'
        )

    def grant(self, *, issue=ISSUE, path=MANIFEST_REL, ttl=3600):
        code, out, err = self.run(
            "authorize-write", "--issue", str(issue), "--path", path,
            "--op", "write", "--issuer", "reviewer", "--ttl", str(ttl), "--json",
        )
        assert code == 0, err
        return json.loads(out)["grant_id"]

    def configure(self, *args, issue=ISSUE, crate="demo-core"):
        with patch.dict(os.environ, {"PATH": f"{self.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}):
            return self.run(
                "configure-crate", "--issue", str(issue), "--crate", crate,
                "--profile", "cargo-verus", *args, "--json",
            )


@pytest.fixture
def workspace():
    fixture = CrateProfileFixture()
    fixture.setup()
    try:
        yield fixture
    finally:
        fixture.teardown()


def events(path: Path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_initial_profile_hashes_only_the_selected_crate_and_preserves_other_manifests(workspace):
    before = workspace.manifest.read_bytes()
    sibling_before = workspace.sibling.read_bytes()
    lock_before = workspace.lock.read_bytes()
    grant = workspace.grant()

    code, output, err = workspace.configure("--grant-id", grant)

    assert code == 0, output + err
    record = json.loads(output)
    schema = json.loads((ROOT / "docs/cargo-verus-profile-audit-schema.json").read_text())
    assert not list(make_validator(schema).iter_errors(record))
    updated = workspace.manifest.read_bytes()
    parsed = tomllib.loads(updated.decode())
    assert parsed["dependencies"]["serde"] == {"version": "1", "optional": True}
    assert parsed["dependencies"]["vstd"] == {"version": f"={VSTD_VERSION}"}
    assert parsed["features"] == {"default": ["std"], "std": ["dep:serde"]}
    assert parsed["package"]["metadata"]["verus"]["verify"] is True
    assert parsed["lints"]["rust"]["unexpected_cfgs"] == {
        "level": "warn", "check-cfg": ["cfg(verus_only)"]
    }
    assert record["path"] == MANIFEST_REL
    assert record["crate"] == "demo-core"
    assert record["grant_id"] == grant
    assert record["before_hash"] == "sha256:" + hashlib.sha256(before).hexdigest()
    assert record["after_hash"] == "sha256:" + hashlib.sha256(updated).hexdigest()
    manifest_hashes = {row["path"]: row for row in record["crate_manifest_hashes"]}
    assert manifest_hashes[MANIFEST_REL]["before_hash"] == record["before_hash"]
    assert manifest_hashes[MANIFEST_REL]["after_hash"] == record["after_hash"]
    assert manifest_hashes[MANIFEST_REL]["result"] == "updated"
    for path, raw in (("Cargo.toml", (workspace.workspace / "Cargo.toml").read_bytes()),
                      ("rust/sibling/Cargo.toml", sibling_before)):
        assert manifest_hashes[path]["before_hash"] == manifest_hashes[path]["after_hash"]
        assert manifest_hashes[path]["before_hash"] == "sha256:" + hashlib.sha256(raw).hexdigest()
        assert manifest_hashes[path]["result"] == "unchanged"
    assert record["validation"]["crate_manifests_preserved"] == "passed"
    assert record["validation"]["cargo_lock_preserved"] == "passed"
    assert workspace.sibling.read_bytes() == sibling_before
    assert workspace.lock.read_bytes() == lock_before
    assert record["cargo_lock_hashes"] == {
        "before_hash": "sha256:" + hashlib.sha256(lock_before).hexdigest(),
        "after_hash": "sha256:" + hashlib.sha256(lock_before).hexdigest(),
        "result": "preserved",
    }
    assert f"--- {MANIFEST_REL}" in err
    assert len(events(workspace.ledger)) == 2


def test_idempotent_rerun_records_identical_hashes_and_no_diff(workspace):
    grant = workspace.grant()
    code, first, err = workspace.configure("--grant-id", grant)
    assert code == 0, err
    first_record = json.loads(first)
    configured_bytes = workspace.manifest.read_bytes()

    code, second, err = workspace.configure("--grant-id", grant)

    assert code == 0, err
    second_record = json.loads(second)
    assert second_record["result"] == "unchanged"
    assert second_record["transition"] == "unchanged"
    assert second_record["before_hash"] == second_record["after_hash"]
    assert second_record["before_hash"] == first_record["after_hash"]
    assert workspace.manifest.read_bytes() == configured_bytes
    assert "diff" not in err
    assert len(events(workspace.ledger)) == 3


def test_cargo_created_lockfile_is_removed_when_none_existed_before(workspace):
    workspace.lock.unlink()
    workspace.grant()

    code, output, err = workspace.configure()

    assert code == 0, output + err
    record = json.loads(output)
    assert not workspace.lock.exists()
    assert record["cargo_lock_hashes"] == {
        "before_hash": None,
        "after_hash": None,
        "result": "preserved",
    }


def test_missing_wrong_issue_and_mismatched_toolchain_refuse_without_manifest_write(workspace):
    original = workspace.manifest.read_bytes()
    workspace.grant(issue=ISSUE - 1)
    code, output, _ = workspace.configure()
    assert code == 2
    assert json.loads(output)["error"]["code"] == "missing-or-invalid-grant"
    assert workspace.manifest.read_bytes() == original

    workspace.grant()
    code, output, _ = workspace.configure(issue=ISSUE + 1)
    assert code == 2
    assert json.loads(output)["error"]["code"] == "missing-or-invalid-grant"
    assert workspace.manifest.read_bytes() == original

    workspace.install_fake_toolchain(rust_release="1.89.0")
    code, output, _ = workspace.configure()
    assert code == 2
    assert json.loads(output)["error"]["code"] == "verifier-version-mismatch"
    assert workspace.manifest.read_bytes() == original


def test_foreign_vstd_source_is_refused_without_mutating_dependencies(workspace):
    original = workspace.manifest.read_text() + '\n[dependencies.vstd]\npath = "../vstd"\n'
    workspace.manifest.write_text(original)
    workspace.grant()

    code, output, _ = workspace.configure()

    assert code == 2
    assert json.loads(output)["error"]["code"] == "conflicting-vstd-source"
    assert workspace.manifest.read_text() == original


def test_validation_failure_restores_manifest_and_lockfile_without_a_success_event(workspace):
    original_manifest = workspace.manifest.read_bytes()
    original_lock = workspace.lock.read_bytes()
    workspace.grant()
    (workspace.workspace / "FAIL_CHECK").touch()

    code, output, _ = workspace.configure()

    assert code == 2
    assert json.loads(output)["error"]["code"] == "validation-failed"
    assert workspace.manifest.read_bytes() == original_manifest
    assert workspace.lock.read_bytes() == original_lock
    assert len(events(workspace.ledger)) == 1


def test_existing_vstd_features_and_cfg_lints_are_preserved(workspace):
    original = (
        "[package]\n"
        'name = "demo-core"\n'
        'version = "0.1.0"\n'
        'edition = "2021"\n'
        "\n"
        "[dependencies]\n"
        f'vstd = {{ version = "={VSTD_VERSION}", features = ["alloc"], default-features = false }}\n'
        "serde = { version = \"1\", optional = true }\n"
        "\n"
        "[lints.rust]\n"
        'unexpected_cfgs = { level = "deny", check-cfg = ["cfg(project)",] }\n'
    )
    workspace.manifest.write_text(original)
    grant = workspace.grant()

    code, output, err = workspace.configure("--grant-id", grant)

    assert code == 0, output + err
    record = json.loads(output)
    parsed = tomllib.loads(workspace.manifest.read_text())
    assert record["transition"] == "initial"
    assert parsed["dependencies"]["vstd"] == {
        "version": f"={VSTD_VERSION}", "features": ["alloc"], "default-features": False
    }
    assert parsed["lints"]["rust"]["unexpected_cfgs"] == {
        "level": "deny", "check-cfg": ["cfg(project)", "cfg(verus_only)"]
    }
    assert parsed["dependencies"]["serde"] == {"version": "1", "optional": True}


def test_verus_toolchain_repin_is_explicit_and_hash_audited(workspace):
    grant = workspace.grant()
    code, initial, err = workspace.configure("--grant-id", grant)
    assert code == 0, err
    first = json.loads(initial)
    original = workspace.manifest.read_bytes()
    new_version = "0.2025.11.0"
    workspace.install_fake_toolchain(vstd_version=new_version)
    next_grant = workspace.grant(ttl=1800)

    code, output, err = workspace.configure("--grant-id", next_grant)

    assert code == 0, output + err
    record = json.loads(output)
    assert record["transition"] == "verus-toolchain-repin"
    assert record["before_hash"] == "sha256:" + hashlib.sha256(original).hexdigest()
    assert record["after_hash"] == "sha256:" + hashlib.sha256(workspace.manifest.read_bytes()).hexdigest()
    assert tomllib.loads(workspace.manifest.read_text())["dependencies"]["vstd"]["version"] == f"={new_version}"
