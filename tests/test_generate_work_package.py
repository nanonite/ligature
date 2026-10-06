from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import gate_g14
import generate_work_package
import pipeline
import project_state
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


class IntegratedWorkspace:
    """Small real project assembled from the repository's validated fixtures.

    Unlike Workspace above, this deliberately leaves the generator's
    authoritative loader in place, so a successful manifest has crossed
    the real source validators before the downstream gates see it.
    """

    def __init__(self, root: Path):
        self.root = root
        fixture = ROOT / "tests/fixtures/work_packages/valid"
        shutil.copytree(
            fixture,
            root,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        self.descriptor_path = root / "project-descriptor.json"
        descriptor = json.loads(self.descriptor_path.read_text())
        descriptor["write_set"]["allowed_roots"].append("scripts/**")
        descriptor["write_set"]["protected_roots"].append("ci/manifest/**")
        self.descriptor_path.write_text(json.dumps(descriptor, indent=2) + "\n")

        for path in (root / "ci/manifest").glob("WP-SCHED-001.*"):
            path.unlink()

        crate_specs = root / "crates/scheduler/specs"
        (crate_specs / "_interactions").mkdir(parents=True, exist_ok=True)
        (crate_specs / "_bridges").mkdir(parents=True, exist_ok=True)
        shutil.copy2(
            ROOT / "tests/fixtures/promotions/valid/crates/scheduler/specs/_boundaries/scheduler_dispatch__to__task_queue_pop_ready.json",
            crate_specs / "_boundaries/scheduler_dispatch__to__task_queue_pop_ready.json",
        )
        shutil.copy2(
            ROOT / "tests/fixtures/interactions/valid/specs/_interactions/I-SCHED-TQ-001.json",
            crate_specs / "_interactions/I-SCHED-TQ-001.json",
        )
        shutil.copy2(
            ROOT / "tests/fixtures/bridges/valid/specs/_bridges/BR-SCHED-TQ-001.json",
            crate_specs / "_bridges/BR-SCHED-TQ-001.json",
        )

        closure_dir = root / "specs/_closure"
        closure_dir.mkdir(parents=True, exist_ok=True)
        profile = json.loads(
            (ROOT / "tests/fixtures/closure/stale/specs/_closure/scheduler-core.json").read_text()
        )
        profile["cluster"] = "scheduling"
        profile["work_packages"] = ["WP-SCHED-001"]
        (closure_dir / "scheduling.json").write_text(
            json.dumps(profile, indent=2) + "\n"
        )

        promotion_source = ROOT / "tests/fixtures/promotions/valid"
        (root / "specs/_promotions").mkdir(parents=True, exist_ok=True)
        shutil.copy2(
            promotion_source / "specs/_promotions/scheduling.json",
            root / "specs/_promotions/scheduling.json",
        )
        (root / "docs").mkdir(parents=True, exist_ok=True)
        shutil.copy2(
            promotion_source / "docs/reliance-policy.md",
            root / "docs/reliance-policy.md",
        )
        self.plan_path = root.parent / "wp-plan.json"
        request = make_request(issue="chainlink:123", depends_on=())
        self.plan_path.write_text(json.dumps(dataclasses.asdict(request), indent=2) + "\n")

        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "ligature-tests@example.invalid"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Ligature integration test"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture baseline"], cwd=root, check=True)

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

    def authorize(self, path: str) -> None:
        code, _output, error = self.run(
            "authorize-write",
            "--issue",
            "123",
            "--path",
            path,
            "--op",
            "write",
            "--issuer",
            "integration-test",
            "--one-shot",
        )
        assert code == 0, error

    def generate(self) -> tuple[int, str, str]:
        return self.run(
            "generate-work-package",
            "--plan",
            str(self.plan_path),
            "--issue",
            "123",
            "--toolchain",
            "nightly-2026-05-01",
            "--target",
            "x86_64-unknown-linux-gnu",
            "--feature",
            "default",
            "--json",
        )

    def authorize_existing_protected_artifacts(self) -> None:
        for path in sorted(
            file.relative_to(self.root).as_posix()
            for file in (self.root / "crates/scheduler/specs").rglob("*.json")
        ):
            self.authorize(path)


@pytest.fixture
def integrated_workspace(tmp_path):
    project = IntegratedWorkspace(tmp_path / "project")
    project.authorize_existing_protected_artifacts()
    project.authorize(MANIFEST_REL)
    return project


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


def test_real_generation_passes_validator_and_is_consumed_by_project_gates(
    integrated_workspace,
):
    project = integrated_workspace
    code, output, error = project.generate()
    assert code == 0, error
    result = json.loads(output)
    assert result["event"] == "work-package-generation"
    assert project.manifest.is_file()

    code, validation, error = project.run(
        "validate-work-package", str(project.manifest)
    )
    assert code == 0, error + validation
    assert "passes G1a and §10.1" in validation

    manifests, findings = gate_g14.load_manifests(project.root)
    assert "WP-SCHED-001" in manifests
    assert not [finding for finding in findings if "WP-SCHED-001" in str(finding)]

    state = project_state.build_project_state(
        project.root, project.descriptor_path, issue=123
    )
    artifact = next(
        artifact
        for artifact in state["artifacts"]
        if artifact["path"] == MANIFEST_REL
    )
    assert artifact["lifecycle"] == "validated"

    doctor_code, doctor, error = project.run("doctor")
    assert doctor_code == 0, error + doctor
    assert "Installation:" in doctor

    check_code, check_output, error = project.run("check", "--json", "--issue", "123")
    check = json.loads(check_output)
    assert check_code == check["result"]["exit_code"], error
    assert check["result"]["conditions"]
    assert any(
        run["gate_id"] == "g14" and run["outcome"] == "executed"
        for run in check["gates"]
    )
    assert not any(
        finding["gate_id"] == "g14" and "not schema-valid" in finding["reason"]
        for finding in check["findings"]
    )
    # The fixture has no proof report, callsite extraction, or human review;
    # check must preserve its usual fail-closed result even though it found
    # and loaded this structurally valid generated manifest.
    assert check_code != 0

    write_code, write_output, error = project.run(
        "write-set-check", "--json", "--issue", "123"
    )
    write_report = json.loads(write_output)
    assert write_code == 0, error + json.dumps(write_report, sort_keys=True)
    assert write_report["write_set"]["state"] == "clean"
    assert write_report["write_set"]["grants"]["ignored"] == 1
    assert not write_report["write_set"]["violations"]


@pytest.mark.parametrize(
    "failure",
    ["stale-policy", "invalid-plan", "missing-interaction"],
)
def test_invalid_authoritative_or_plan_input_never_produces_a_manifest(
    integrated_workspace, failure
):
    project = integrated_workspace
    if failure == "stale-policy":
        (project.root / "docs/reliance-policy.md").write_text("changed after approval\n")
    elif failure == "invalid-plan":
        plan = json.loads(project.plan_path.read_text())
        plan["unexpected_policy"] = True
        project.plan_path.write_text(json.dumps(plan) + "\n")
    else:
        (project.root / "crates/scheduler/specs/_interactions/I-SCHED-TQ-001.json").unlink()

    code, output, error = project.generate()
    refusal = json.loads(output)
    assert code == 2, error
    assert refusal["error"]["code"] in {
        "invalid-promotion-input",
        "unsupported-field",
        "missing-source",
        "invalid-authoritative-state",
    }
    assert not project.manifest.exists()


def test_downstream_validation_and_check_reject_tampered_generated_fields(
    integrated_workspace,
):
    project = integrated_workspace
    code, _output, error = project.generate()
    assert code == 0, error
    generated = json.loads(project.manifest.read_text())
    generated["unrecognized"] = "not valid in schema 1.0"
    project.manifest.write_text(json.dumps(generated, indent=2) + "\n")

    validation_code, validation, error = project.run(
        "validate-work-package", str(project.manifest)
    )
    assert validation_code == 1, error
    assert "FAIL:" in validation
    assert "unrecognized" in validation

    manifests, findings = gate_g14.load_manifests(project.root)
    assert "WP-SCHED-001" not in manifests
    assert any("not schema-valid" in finding.reason for finding in findings)

    check_code, check_output, error = project.run("check", "--json", "--issue", "123")
    check = json.loads(check_output)
    assert check_code != 0, error
    assert check["result"]["exit_code"] != 0


def test_gate_integrity_is_derived_from_live_files_and_stale_runners_block(
    integrated_workspace,
):
    project = integrated_workspace
    code, _output, error = project.generate()
    assert code == 0, error
    original = project.manifest.read_bytes()
    manifest = json.loads(original)
    assert manifest["gate_integrity"]
    for entry in manifest["gate_integrity"]:
        actual = hashlib.sha256((project.root / entry["runner"]).read_bytes()).hexdigest()
        assert entry["hash"] == f"sha256:{actual}"

    runner = project.root / manifest["gate_integrity"][0]["runner"]
    runner.write_bytes(runner.read_bytes() + b"# changed after generation\n")

    validation_code, validation, error = project.run(
        "validate-work-package", str(project.manifest)
    )
    assert validation_code == 1, error
    assert "gate_integrity hash mismatch" in validation

    # Regeneration cannot replace the stale canonical output without an
    # operator choosing a new package or reviewing the conflict.
    code, output, error = project.generate()
    refusal = json.loads(output)
    assert code == 2, error
    assert refusal["error"]["code"] == "target-conflict"
    assert project.manifest.read_bytes() == original

    check_code, check_output, error = project.run("check", "--json", "--issue", "123")
    check = json.loads(check_output)
    assert check_code != 0, error
    assert any(run["gate_id"] == "g14" for run in check["gates"])


def test_candidate_validation_refusal_leaves_no_manifest_or_success_audit(
    workspace, monkeypatch
):
    grant_id = workspace.authorize()
    monkeypatch.setattr(
        generate_work_package.work_package_manifest,
        "validate_derived_manifest",
        lambda *_args, **_kwargs: [
            type(
                "Finding",
                (),
                {"severity": "error", "__str__": lambda _self: "G1a refused"},
            )()
        ],
    )

    code, output, error = workspace.generate("--grant-id", grant_id)
    refusal = output
    assert code == 2, error
    assert refusal["error"]["code"] == "derived-manifest-invalid"
    assert not workspace.manifest.exists()
    assert not [
        row
        for row in (json.loads(line) for line in workspace.ledger.read_text().splitlines())
        if row.get("event") == "work-package-generation"
    ]
