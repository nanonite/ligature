from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts.work_package_manifest import (
    AuthoritativeState,
    GateSpec,
    ManifestDerivationError,
    WorkPackageRequest,
    derive_and_render,
    derive_manifest,
    render_manifest,
    validate_derived_manifest,
)


def digest(value: bytes = b"gate") -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def make_request(**updates) -> WorkPackageRequest:
    values = {
        "work_package": "WP-SCHED-001",
        "issue": "chainlink:121",
        "cluster": "scheduling",
        "functions": ("scheduler::TaskQueue::pop_ready",),
        "provided_obligations": ("TaskQueue.C003",),
        "required_obligations": (),
        "bridges": ("BR-SCHED-TQ-001",),
        "assumptions": (),
        "harnesses": {"TaskQueue.C003": "task_queue_pop_ready_C003"},
        "depends_on": ("WP-SCHED-000",),
        "coupling_notes": ("shared queue invariant",),
        "gates": (GateSpec("build", "cargo", ("build", "-p", "scheduler")),),
        "scheduling_rule": "reverse-dependency-order",
        "allowed_write_set": ("crates/scheduler/src/",),
        "protected_write_set": (
            "crates/*/specs/**",
            "ci/manifest/**",
            "scripts/**",
        ),
    }
    values.update(updates)
    return WorkPackageRequest(**values)


def make_state(root: Path | None = None, **updates) -> AuthoritativeState:
    assurance = {
        "required_claims": ["postcondition-holds"],
        "accepted_evidence_kinds": ["creusot-deductive-check"],
        "minimum_scope": {"feature_set": "default"},
        "trust_policy": {"assumptions_allowed": []},
    }
    descriptor = {
        "crates": [{"crate_dir": "crates/scheduler"}],
        "verifier_policy": {"default": "creusot"},
        "write_set": {
            "allowed_roots": ["crates/scheduler/src/"],
            "protected_roots": ["crates/*/specs/**"],
        },
        "gate_integrity": [],
    }
    if root is not None:
        (root / "scripts").mkdir(parents=True, exist_ok=True)
        (root / "scripts/witness_renderer.py").write_bytes(b"renderer")
        (root / "scripts/xml_escape.py").write_bytes(b"escape")
        (root / "docs").mkdir(parents=True, exist_ok=True)
        (root / "docs/reliance-policy.md").write_bytes(b"policy")
    state = AuthoritativeState(
        descriptor=descriptor,
        closure_profiles=(
            {
                "cluster": "scheduling",
                "work_packages": ["WP-SCHED-000", "WP-SCHED-001"],
                "conditions": {"owning_verifier": "creusot"},
            },
        ),
        promotion_receipts=(
            {
                "cluster": "scheduling",
                "promotion_id": "PROM-SCHED-001",
                "artifact_manifest": [
                    {"path": "docs/reliance-policy.md", "hash": digest(b"policy")}
                ],
            },
        ),
        boundary_contracts=(
            {
                "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
                "assumptions": [
                    {
                        "tracking_issue": "chainlink:23",
                        "assumption_hash": digest(b"assumption"),
                    }
                ],
            },
        ),
        interactions=(
            {
                "interaction_id": "I-SCHED-TQ-001",
                "reliances": [
                    {"obligation_id": "TaskQueue.C003", "required_assurance": assurance}
                ],
            },
        ),
        bridge_specs=(
            {
                "bridge_id": "BR-SCHED-TQ-001",
                "callee_requirement": "TaskQueue.C003",
            },
        ),
        base_commit="a1b2c3d",
        toolchain="nightly-2026-05-01",
        target="x86_64-unknown-linux-gnu",
        features=("default",),
        gate_hashes={
            "scripts/witness_renderer.py": digest(b"renderer"),
            "scripts/xml_escape.py": digest(b"escape"),
        },
        workspace_root=root,
    )
    return (
        copy.deepcopy(state)
        if not updates
        else AuthoritativeState(**{**state.__dict__, **updates})
    )


def test_derivation_is_canonical_and_matches_the_repository_schema():
    manifest = derive_manifest(make_request(), make_state())
    encoded = derive_and_render(make_request(), make_state())

    assert json.loads(encoded) == manifest
    assert encoded.endswith(b"\n")
    assert encoded == render_manifest(manifest)
    assert manifest["obligations"] == ["BR-SCHED-TQ-001", "TaskQueue.C003"]
    assert manifest["gate_integrity"] == [
        {"runner": "scripts/witness_renderer.py", "hash": digest(b"renderer")},
        {"runner": "scripts/xml_escape.py", "hash": digest(b"escape")},
    ]


def test_derivation_is_independent_of_input_order():
    first = make_request(
        functions=("scheduler::TaskQueue::pop_ready",),
        coupling_notes=("z note", "a note"),
        gates=(
            GateSpec("prove", "cargo", ("creusot",)),
            GateSpec("build", "cargo", ("build",)),
        ),
    )
    second = make_request(
        functions=("scheduler::TaskQueue::pop_ready",),
        coupling_notes=("a note", "z note"),
        gates=(
            GateSpec("build", "cargo", ("build",)),
            GateSpec("prove", "cargo", ("creusot",)),
        ),
    )
    assert derive_and_render(first, make_state()) == derive_and_render(
        second, make_state()
    )


def test_derived_output_passes_the_repository_structural_validator(tmp_path):
    state = make_state(tmp_path)
    manifest = derive_manifest(make_request(), state)
    findings = validate_derived_manifest(manifest, tmp_path)

    assert not [finding for finding in findings if finding.severity != "info"]


def test_plan_mapping_rejects_unsupported_fields():
    with pytest.raises(ManifestDerivationError, match="unexpected_policy") as error:
        WorkPackageRequest.from_mapping(
            {"work_package": "WP-X", "unexpected_policy": {}}
        )
    assert error.value.code == "unsupported-field"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("functions", "scheduler::TaskQueue::pop_ready"),
        ("gates", "build"),
        ("harnesses", ["TaskQueue.C003", "harness"]),
    ],
)
def test_plan_mapping_rejects_malformed_container_shapes(field, value):
    with pytest.raises(
        ManifestDerivationError, match="must be an array|must be a mapping"
    ):
        WorkPackageRequest.from_mapping({field: value})


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"functions": ("scheduler::TaskQueue::pop_ready",) * 2}, "duplicate function"),
        ({"depends_on": ("WP-SCHED-000",) * 2}, "duplicate dependency"),
        (
            {"provided_obligations": ("TaskQueue.C003",) * 2},
            "duplicate provided obligation",
        ),
        ({"bridges": ("BR-SCHED-TQ-001",) * 2}, "duplicate bridge"),
        ({"gates": (GateSpec("build", "cargo"),) * 2}, "duplicate gate id"),
        (
            {"allowed_write_set": ("crates/scheduler/src/",) * 2},
            "duplicate allowed_write_set",
        ),
        (
            {
                "assumptions": (
                    {
                        "assumption_ref": {
                            "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
                            "tracking_issue": "chainlink:23",
                            "assumption_hash": digest(b"assumption"),
                        },
                        "risk": "high",
                        "mitigations": [
                            {"kind": "test", "reference": "tests/dispatch.rs"}
                        ],
                    },
                )
                * 2
            },
            "duplicate assumption_ref",
        ),
    ],
)
def test_duplicate_plan_identifiers_are_refused(updates, message):
    with pytest.raises(ManifestDerivationError, match=message) as error:
        derive_manifest(make_request(**updates), make_state())
    assert error.value.code == "duplicate-identifier"


@pytest.mark.parametrize(
    ("depends_on", "message"),
    [
        (("WP-OTHER-999",), "resolves to no work package"),
        (("WP-SCHED-001",), "depends on itself"),
    ],
)
def test_dangling_and_self_dependencies_are_refused(depends_on, message):
    with pytest.raises(ManifestDerivationError, match=message):
        derive_manifest(make_request(depends_on=depends_on), make_state())


def test_self_referential_guarantee_is_refused():
    request = make_request(required_obligations=("TaskQueue.C003",))
    with pytest.raises(
        ManifestDerivationError, match="both provides and requires"
    ) as error:
        derive_manifest(request, make_state())
    assert error.value.code == "self-referential-guarantee"


def test_missing_and_ambiguous_authoritative_sources_are_refused():
    with pytest.raises(ManifestDerivationError, match="no closure profile"):
        derive_manifest(make_request(), make_state(closure_profiles=()))
    with pytest.raises(ManifestDerivationError, match="2 closure profiles"):
        derive_manifest(
            make_request(),
            make_state(closure_profiles=make_state().closure_profiles * 2),
        )
    with pytest.raises(ManifestDerivationError, match="no interaction reliance"):
        derive_manifest(make_request(), make_state(interactions=()))
    with pytest.raises(ManifestDerivationError, match="no promotion receipt"):
        derive_manifest(make_request(), make_state(promotion_receipts=()))
    with pytest.raises(ManifestDerivationError, match="no bridge specification"):
        derive_manifest(make_request(), make_state(bridge_specs=()))
    with pytest.raises(ManifestDerivationError, match="2 promotion receipts"):
        derive_manifest(
            make_request(),
            make_state(promotion_receipts=make_state().promotion_receipts * 2),
        )


def test_conflicting_authoritative_assurance_is_refused():
    state = make_state()
    conflicting = copy.deepcopy(state.interactions[0])
    conflicting["interaction_id"] = "I-OTHER"
    conflicting["reliances"][0]["required_assurance"]["minimum_scope"] = {
        "target": "other"
    }
    with pytest.raises(
        ManifestDerivationError, match="conflicting required_assurance"
    ) as error:
        derive_manifest(
            make_request(),
            make_state(interactions=state.interactions + (conflicting,)),
        )
    assert error.value.code == "contradictory-source"


def test_assurance_that_conflicts_with_the_verifier_policy_is_refused():
    state = make_state()
    interaction = copy.deepcopy(state.interactions[0])
    interaction["reliances"][0]["required_assurance"]["accepted_evidence_kinds"] = [
        "kani-bounded-model-check"
    ]
    with pytest.raises(ManifestDerivationError, match="no verifier declared") as error:
        derive_manifest(make_request(), make_state(interactions=(interaction,)))
    assert error.value.code == "contradictory-source"


def test_duplicate_gate_integrity_paths_are_refused():
    descriptor = make_state().descriptor
    descriptor["gate_integrity"] = [
        {"path": "scripts/witness_renderer.py"},
        {"path": "scripts/witness_renderer.py"},
    ]
    with pytest.raises(
        ManifestDerivationError, match="duplicate gate_integrity runner"
    ) as error:
        derive_manifest(make_request(), make_state(descriptor=descriptor))
    assert error.value.code == "duplicate-identifier"


def test_duplicate_features_are_refused():
    with pytest.raises(ManifestDerivationError, match="duplicate feature") as error:
        derive_manifest(make_request(), make_state(features=("default", "default")))
    assert error.value.code == "duplicate-identifier"


def test_malformed_authoritative_container_shapes_are_refused():
    with pytest.raises(
        ManifestDerivationError, match="features must be an array"
    ) as error:
        derive_manifest(make_request(), make_state(features="default"))
    assert error.value.code == "invalid-authoritative-state"


def test_assumption_and_mitigation_shapes_are_not_silently_accepted():
    assumption = {
        "assumption_ref": {
            "boundary_id": "scheduler_dispatch__to__task_queue_pop_ready",
            "tracking_issue": "chainlink:23",
            "assumption_hash": digest(b"assumption"),
        },
        "risk": "high",
        "mitigations": [{"kind": "test", "reference": "tests/dispatch.rs"}],
        "ignored_typo": True,
    }
    with pytest.raises(ManifestDerivationError, match="unsupported ignored_typo"):
        derive_manifest(make_request(assumptions=(assumption,)), make_state())
    assumption.pop("ignored_typo")
    assumption["mitigations"] = [
        {"kind": "test", "reference": "tests/dispatch.rs", "typo": True}
    ]
    with pytest.raises(ManifestDerivationError, match="exactly kind and reference"):
        derive_manifest(make_request(assumptions=(assumption,)), make_state())


def test_descriptor_write_roots_bound_the_plan():
    with pytest.raises(
        ManifestDerivationError, match="widens descriptor allowed_roots"
    ):
        derive_manifest(
            make_request(allowed_write_set=("tests/scheduler/",)),
            make_state(),
        )


def test_stale_gate_hash_and_promotion_artifact_are_refused(tmp_path):
    state = make_state(tmp_path)
    (tmp_path / "docs/reliance-policy.md").write_bytes(b"changed policy")
    receipt = copy.deepcopy(state.promotion_receipts[0])
    receipt["artifact_manifest"][0]["path"] = "docs/reliance-policy.md"
    receipt["artifact_manifest"][0]["hash"] = digest(b"changed policy")
    gate_hashes = dict(state.gate_hashes)
    gate_hashes["scripts/witness_renderer.py"] = digest(b"old renderer")
    state = AuthoritativeState(
        **{
            **state.__dict__,
            "promotion_receipts": (receipt,),
            "gate_hashes": gate_hashes,
        }
    )
    with pytest.raises(
        ManifestDerivationError, match="gate_integrity hash.*stale"
    ) as error:
        derive_manifest(make_request(), state)
    assert error.value.code == "stale-integrity-hash"

    (tmp_path / "docs/reliance-policy.md").write_bytes(b"new policy")
    gate_hashes["scripts/witness_renderer.py"] = digest(b"renderer")
    state = AuthoritativeState(
        **{
            **state.__dict__,
            "promotion_receipts": (receipt,),
            "gate_hashes": gate_hashes,
        }
    )
    with pytest.raises(
        ManifestDerivationError, match="changed since acceptance"
    ) as error:
        derive_manifest(make_request(), state)
    assert error.value.code == "stale-integrity-hash"


def test_promotion_artifact_cannot_escape_the_workspace(tmp_path):
    state = make_state(tmp_path)
    receipt = copy.deepcopy(state.promotion_receipts[0])
    receipt["artifact_manifest"][0]["path"] = "../outside.json"
    with pytest.raises(
        ManifestDerivationError, match="escapes or is not normalized"
    ) as error:
        derive_manifest(
            make_request(), make_state(tmp_path, promotion_receipts=(receipt,))
        )
    assert error.value.code == "invalid-authoritative-state"


def test_derivation_does_not_write_to_the_workspace(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/reliance-policy.md").write_bytes(b"policy")
    state = make_state(tmp_path)
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    derive_manifest(make_request(), state)
    after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    assert after == before
