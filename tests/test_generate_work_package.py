from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import generate_work_package
import pipeline
import write_authorization
from schema_utils import make_validator
from test_work_package_manifest import make_request, make_state

AUDIT_SCHEMA = json.loads(
    (ROOT / "docs/work-package-generation-audit-schema.json").read_text()
)
LEDGER_REL = "ci/results/protected-writes.jsonl"
MANIFEST_REL = "ci/manifest/WP-SCHED-001.json"


class Workspace:
    def __init__(self, path: Path, monkeypatch):
        self.root = path
        fixture = ROOT / "tests/fixtures/work_packages/valid"
        shutil.copytree(fixture, path, dirs_exist_ok=True)
        self.descriptor_path = path / "project-descriptor.json"
        self.descriptor = json.loads(self.descriptor_path.read_text())
        self.descriptor["write_set"]["protected_roots"].append("ci/manifest/**")
        self.descriptor_path.write_text(json.dumps(self.descriptor, indent=2) + "\n")
        request = make_request(issue="chainlink:122")
        self.plan_path = path / "wp-plan.json"
        self.plan_path.write_text(
            json.dumps(dataclasses.asdict(request), indent=2) + "\n"
        )
        if (path / MANIFEST_REL).exists():
            (path / MANIFEST_REL).unlink()
        fixture_state = make_state(path, descriptor=self.descriptor)
        self.state = generate_work_package.work_package_manifest.AuthoritativeState(
            **fixture_state.__dict__
        )
        monkeypatch.setattr(
            generate_work_package,
            "_load_authoritative_state",
            lambda _workspace, _descriptor, **_kwargs: self.state,
        )

    @property
    def ledger(self) -> Path:
        return self.root / LEDGER_REL

    @property
    def manifest(self) -> Path:
        return self.root / MANIFEST_REL

    def run(self, *args: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = pipeline.main(
                [
                    "--workspace",
                    str(self.root),
                    "--descriptor",
                    str(self.descriptor_path),
                    *args,
                ]
            )
        return code, stdout.getvalue(), stderr.getvalue()

    def authorize(
        self,
        *,
        issue: int = 122,
        path: str = MANIFEST_REL,
        op: str = "write",
        extra: tuple[str, ...] = (),
    ) -> str:
        code, output, error = self.run(
            "authorize-write",
            "--issue",
            str(issue),
            "--path",
            path,
            "--op",
            op,
            "--issuer",
            "operator",
            "--one-shot",
            *extra,
        )
        assert code == 0, error
        return next(
            line.split(": ", 1)[1]
            for line in output.splitlines()
            if line.startswith("grant: ")
        )

    def generate(self, *extra: str) -> tuple[int, dict, str]:
        code, output, error = self.run(
            "generate-work-package",
            "--plan",
            str(self.plan_path),
            "--issue",
            "122",
            "--toolchain",
            "nightly-2026-05-01",
            "--target",
            "x86_64-unknown-linux-gnu",
            "--feature",
            "default",
            "--json",
            *extra,
        )
        return code, json.loads(output) if output.strip() else {}, error


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    return Workspace(tmp_path / "project", monkeypatch)


def test_cli_help_and_missing_inputs_name_the_required_values():
    help_text = pipeline.registered_commands()["generate-work-package"].format_help()
    for required in (
        "--plan",
        "--issue",
        "--toolchain",
        "--target",
        "--feature",
        "--grant-id",
        "--json",
    ):
        assert required in help_text

    output, error = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
        code = pipeline.main(["generate-work-package", "--json"])
    refusal = json.loads(output.getvalue())
    assert code == 2
    assert refusal["error"]["code"] == "missing-input"
    assert refusal["error"]["required_inputs"] == [
        "--plan",
        "--issue",
        "--toolchain",
        "--target",
    ]


def test_authoritative_loader_uses_the_repository_validators(workspace, monkeypatch):
    monkeypatch.setattr(
        generate_work_package, "_git_head", lambda _workspace: "a1b2c3d"
    )

    state = generate_work_package._load_authoritative_state(
        workspace.root,
        workspace.descriptor_path,
        toolchain="nightly-2026-05-01",
        target="x86_64-unknown-linux-gnu",
        features=["default"],
    )

    assert state.base_commit == "a1b2c3d"
    assert state.workspace_root == workspace.root.resolve()
    assert state.boundary_contracts
    assert len(state.promotion_receipts) == 1
    assert state.promotion_receipts[0]["cluster"] == "scheduling"


def test_authorized_generation_appends_complete_audit_and_identical_rerun_is_noop(
    workspace,
):
    grant_id = workspace.authorize()

    code, first, error = workspace.generate("--grant-id", grant_id)
    assert code == 0, error
    assert first["event"] == "work-package-generation"
    assert first["issue"] == 122
    assert first["grant_id"] == grant_id
    assert first["path"] == MANIFEST_REL
    assert first["before_hash"] is None
    assert first["result"] == "created"
    assert first["command_result"] == 0
    assert workspace.manifest.is_file()
    make_validator(AUDIT_SCHEMA).validate(first)

    code, second, error = workspace.generate("--grant-id", grant_id)
    assert code == 0, error
    assert second["result"] == "unchanged"
    assert second["before_hash"] == first["after_hash"]
    assert second["after_hash"] == first["after_hash"]
    lines = [json.loads(line) for line in workspace.ledger.read_text().splitlines()]
    audit = [line for line in lines if line.get("event") == "work-package-generation"]
    assert [row["result"] for row in audit] == ["created", "unchanged"]
    assert (
        len(
            [
                entry
                for entry in write_authorization.read_grant_ledger(
                    workspace.root
                ).entries
            ]
        )
        == 1
    )


def test_successful_grant_cannot_be_replayed_after_manifest_deletion(workspace):
    grant_id = workspace.authorize()
    code, _, error = workspace.generate("--grant-id", grant_id)
    assert code == 0, error
    workspace.manifest.unlink()

    code, refusal, _ = workspace.generate("--grant-id", grant_id)
    assert code == 2
    assert refusal["error"]["code"] == "grant-replayed"
    assert not workspace.manifest.exists()


def test_missing_grant_fails_without_creating_output(workspace):
    code, refusal, _ = workspace.generate()
    assert code == 2
    assert refusal["error"]["code"] == "missing-or-invalid-grant"
    assert "authorize-write" in refusal["error"]["required_inputs"][0]
    assert not workspace.manifest.exists()


@pytest.mark.parametrize(
    ("grant_options", "expected_code"),
    [
        ({"issue": 321}, "missing-or-invalid-grant"),
        ({"path": "ci/manifest/WP-OTHER-001.json"}, "missing-or-invalid-grant"),
        ({"op": "delete"}, "missing-or-invalid-grant"),
        (
            {"extra": ("--issued-at", "2020-01-01T00:00:00+00:00")},
            "missing-or-invalid-grant",
        ),
    ],
)
def test_wrong_issue_path_operation_and_expired_grants_fail_closed(
    workspace, grant_options, expected_code
):
    workspace.authorize(**grant_options)

    code, refusal, _ = workspace.generate()
    assert code == 2
    assert refusal["error"]["code"] == expected_code
    assert not workspace.manifest.exists()
    assert not [
        row
        for row in (
            json.loads(line) for line in workspace.ledger.read_text().splitlines()
        )
        if row.get("event") == "work-package-generation"
    ]


def test_conflicting_manifest_is_not_overwritten(workspace):
    workspace.authorize()
    workspace.manifest.parent.mkdir(parents=True, exist_ok=True)
    workspace.manifest.write_bytes(b"operator content\n")

    code, refusal, _ = workspace.generate()
    assert code == 2
    assert refusal["error"]["code"] == "target-conflict"
    assert workspace.manifest.read_bytes() == b"operator content\n"


def test_grant_cannot_authorize_a_target_outside_protected_roots(workspace):
    workspace.authorize()
    descriptor = dict(workspace.state.descriptor)
    descriptor["write_set"] = dict(descriptor["write_set"])
    descriptor["write_set"]["protected_roots"] = ["crates/scheduler/specs/**"]
    workspace.state = generate_work_package.work_package_manifest.AuthoritativeState(
        **{**workspace.state.__dict__, "descriptor": descriptor}
    )

    code, refusal, _ = workspace.generate()
    assert code == 2
    assert refusal["error"]["code"] == "target-not-protected"
    assert not workspace.manifest.exists()


def test_malformed_generation_audit_history_fails_closed(workspace):
    workspace.authorize()
    workspace.ledger.write_text(
        workspace.ledger.read_text()
        + json.dumps({"event": "work-package-generation", "schema_version": "1.0"})
        + "\n"
    )

    code, refusal, _ = workspace.generate()
    assert code == 2
    assert refusal["error"]["code"] == "ledger-invalid"
    assert not workspace.manifest.exists()


def test_plan_issue_must_match_cli_issue(workspace):
    code, refusal, _ = workspace.run(
        "generate-work-package",
        "--plan",
        str(workspace.plan_path),
        "--issue",
        "123",
        "--toolchain",
        "nightly",
        "--target",
        "x86_64-unknown-linux-gnu",
        "--json",
    )
    assert code == 2
    assert json.loads(refusal)["error"]["code"] == "issue-mismatch"
    assert not workspace.manifest.exists()
