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
        "harnesses": {"TaskQueue.C003": "creusot"},
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
                "realization": {
                    "config_scope": {
                        "target": "x86_64-unknown-linux-gnu",
                        "features": ["default"],
                    }
                },
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
    # Manifest-first is a supported bootstrap: the promotion receipt and
    # descriptor still anchor this package; profile membership is deferred
    # until the approved profile and generated manifests can be checked by G14.
    manifest = derive_manifest(make_request(), make_state(closure_profiles=()))
    assert manifest["work_package"] == "WP-SCHED-001"
    assert manifest["depends_on"] == ["WP-SCHED-000"]

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


def test_manifest_first_defers_dependency_membership_until_g14():
    request = make_request(depends_on=("WP-SCHED-FUTURE-001",))
    manifest = derive_manifest(request, make_state(closure_profiles=()))
    assert manifest["depends_on"] == ["WP-SCHED-FUTURE-001"]

    with pytest.raises(ManifestDerivationError, match="resolves to no work package"):
        derive_manifest(request, make_state())


def test_profile_first_keeps_membership_as_a_hard_generator_check():
    request = make_request(work_package="WP-SCHED-OTHER-001", depends_on=())
    with pytest.raises(ManifestDerivationError, match="not listed in cluster"):
        derive_manifest(request, make_state())


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


def _standalone_concept_spec(**overrides):
    spec = {
        "concept": "Scheduler",
        "cluster": "scheduling",
        "verifier": "creusot",
        "constraints": [
            {
                "english": "advance returns only valid positions.",
                "logic": "result <= self.capacity",
                "kind": "postcondition",
                "applies_to": ["advance"],
                "id": "C007",
            }
        ],
        "review": {"reviewer": "reviewer", "reviewed_at": "2026-10-16"},
    }
    spec.update(overrides)
    return spec


def _standalone_request(**overrides):
    values = {
        "provided_obligations": ("Scheduler.C007",),
        "bridges": (),
        "harnesses": {"Scheduler.C007": "creusot"},
        "functions": ("scheduler::Scheduler::advance",),
    }
    values.update(overrides)
    return make_request(**values)


def _standalone_state(**overrides):
    values = {"concept_specs": (_standalone_concept_spec(),)}
    values.update(overrides)
    return make_state(**values)


def test_standalone_obligation_derives_assurance_from_reviewed_constraint():
    """chainlink #162: an obligation no interaction reliance declares gets
    its assurance from the reviewed concept spec's own constraint plus the
    owning verifier policy -- not from an invented interaction."""
    manifest = derive_manifest(_standalone_request(), _standalone_state())
    guarantee = next(
        entry
        for entry in manifest["definition_of_done"]["provided_guarantees"]
        if entry["obligation_id"] == "Scheduler.C007"
    )
    assert guarantee["required_assurance"] == {
        "required_claims": ["postcondition-holds"],
        "accepted_evidence_kinds": ["creusot-deductive-check"],
        "trust_policy": {"assumptions_allowed": []},
    }
    assert "Scheduler.C007" in manifest["obligations"]


def test_work_package_plan_rejects_a_function_name_as_verifier_classification():
    with pytest.raises(ManifestDerivationError, match="use 'creusot' or 'verus'"):
        derive_manifest(
            make_request(harnesses={"TaskQueue.C003": "counter_capacity"}),
            make_state(),
        )


def test_standalone_obligation_refuses_without_a_reviewed_source():
    with pytest.raises(ManifestDerivationError, match="no interaction reliance") as error:
        derive_manifest(_standalone_request(), _standalone_state(concept_specs=()))
    assert error.value.code == "missing-source"

    with pytest.raises(ManifestDerivationError, match="declares no constraint") as error:
        derive_manifest(
            _standalone_request(),
            _standalone_state(
                concept_specs=(_standalone_concept_spec(constraints=[]),)
            ),
        )
    assert error.value.code == "missing-source"


def test_standalone_obligation_refuses_unreviewed_spec_and_wrong_verifier():
    unreviewed = _standalone_concept_spec()
    del unreviewed["review"]
    with pytest.raises(ManifestDerivationError, match="without a `review` block") as error:
        derive_manifest(
            _standalone_request(), _standalone_state(concept_specs=(unreviewed,))
        )
    assert error.value.code == "unreviewed-source"

    with pytest.raises(ManifestDerivationError, match="owning verifier") as error:
        derive_manifest(
            _standalone_request(),
            _standalone_state(concept_specs=(_standalone_concept_spec(verifier="kani"),)),
        )
    assert error.value.code == "contradictory-source"


def test_standalone_obligation_refuses_method_mismatch_and_precondition():
    with pytest.raises(ManifestDerivationError, match="names no function this work package owns") as error:
        derive_manifest(
            _standalone_request(functions=("scheduler::Scheduler::other",)),
            _standalone_state(),
        )
    assert error.value.code == "dangling-reference"

    with pytest.raises(ManifestDerivationError, match="kind 'precondition'") as error:
        derive_manifest(
            _standalone_request(),
            _standalone_state(
                concept_specs=(
                    _standalone_concept_spec(
                        constraints=[
                            {
                                "english": "callers must pass a bounded position.",
                                "logic": "self.position <= self.capacity",
                                "kind": "precondition",
                                "applies_to": ["advance"],
                                "id": "C007",
                            }
                        ]
                    ),
                )
            ),
        )
    assert error.value.code == "unsupported-source"


def _cross_verifier_state(**updates):
    """A Verus-owned package that requires a Creusot-owned guarantee.

    The two packages are members of their own clusters/profiles, exactly the
    per-verifier split a mixed project uses; only the dependency edge crosses
    the cluster boundary."""
    state = make_state(
        concept_specs=(
            _standalone_concept_spec(
                concept="Codomain",
                cluster="creusot-core",
                verifier="creusot",
                constraints=[
                    {
                        "english": "the codomain mapping preserves the field.",
                        "logic": "result == input",
                        "kind": "postcondition",
                        "applies_to": ["compute"],
                        "id": "C001",
                    }
                ],
            ),
        ),
    )
    descriptor = copy.deepcopy(state.descriptor)
    descriptor["verifier_policy"] = {
        "default": "creusot",
        "clusters": {"verus-core": "verus", "creusot-core": "creusot"},
    }
    values = {
        "descriptor": descriptor,
        "closure_profiles": (
            {
                "cluster": "verus-core",
                "work_packages": ["WP-VERUS-001"],
                "conditions": {"owning_verifier": "verus"},
            },
            {
                "cluster": "creusot-core",
                "work_packages": ["WP-CREUSOT-001"],
                "conditions": {"owning_verifier": "creusot"},
            },
        ),
        "promotion_receipts": (
            {
                "cluster": "verus-core",
                "promotion_id": "PROM-VERUS-001",
                "artifact_manifest": [
                    {"path": "docs/reliance-policy.md", "hash": digest(b"policy")}
                ],
            },
        ),
        "interactions": (),
        "bridge_specs": (),
    }
    values.update(updates)
    return AuthoritativeState(**{**state.__dict__, **values})


def _cross_verifier_request(**updates):
    values = {
        "work_package": "WP-VERUS-001",
        "cluster": "verus-core",
        "provided_obligations": (),
        "required_obligations": ("Codomain.C001",),
        "bridges": (),
        "harnesses": {},
        "depends_on": ("WP-CREUSOT-001",),
    }
    values.update(updates)
    return make_request(**values)


def test_required_guarantee_uses_the_providers_verifier_policy():
    """chainlink #209: a Verus package may require a Creusot-provided
    guarantee. The required assurance is read against the provider's
    cluster policy, not the requiring package's owner, and the cross-cluster
    dependency is a declared package in the other cluster's profile."""
    manifest = derive_manifest(_cross_verifier_request(), _cross_verifier_state())

    required = manifest["definition_of_done"]["required_guarantees"]
    assert [entry["obligation_id"] for entry in required] == ["Codomain.C001"]
    assert required[0]["required_assurance"]["accepted_evidence_kinds"] == [
        "creusot-deductive-check"
    ]
    assert manifest["depends_on"] == ["WP-CREUSOT-001"]


def test_required_guarantee_from_an_interaction_may_cross_verifiers():
    """The same edge declared by a reviewed interaction reliance is accepted
    when the callee concept's own cluster declares the producing verifier."""
    provider = _standalone_concept_spec(
        concept="TaskQueue",
        cluster="creusot-core",
        verifier="creusot",
    )
    interaction = {
        "interaction_id": "I-VERUS-CREUSOT-001",
        "callee": {"concept": "TaskQueue", "method": "pop_ready"},
        "realization": {
            "config_scope": {
                "target": "x86_64-unknown-linux-gnu",
                "features": ["default"],
            }
        },
        "reliances": [
            {
                "obligation_id": "TaskQueue.C003",
                "required_assurance": {
                    "required_claims": ["postcondition-holds"],
                    "accepted_evidence_kinds": ["creusot-deductive-check"],
                },
            }
        ],
    }
    state = _cross_verifier_state(
        concept_specs=(provider,),
        interactions=(interaction,),
    )

    manifest = derive_manifest(
        _cross_verifier_request(
            required_obligations=("TaskQueue.C003",),
        ),
        state,
    )

    required = manifest["definition_of_done"]["required_guarantees"][0]
    assert required["required_assurance"]["accepted_evidence_kinds"] == [
        "creusot-deductive-check"
    ]


def test_providing_a_foreign_verifiers_obligation_is_still_refused():
    """The per-verifier package model is deliberate: a required guarantee may
    cross clusters, but a package cannot *provide* an obligation its own
    cluster/verifier does not own (chainlink #209)."""
    with pytest.raises(ManifestDerivationError, match="owning verifier") as error:
        derive_manifest(
            _cross_verifier_request(
                provided_obligations=("Codomain.C001",),
                required_obligations=(),
                harnesses={"Codomain.C001": "creusot"},
                depends_on=(),
            ),
            _cross_verifier_state(
                concept_specs=(
                    _standalone_concept_spec(
                        concept="Codomain",
                        cluster="creusot-core",
                        verifier="creusot",
                    ),
                )
            ),
        )
    assert error.value.code in {"missing-source", "contradictory-source"}


def test_cross_cluster_dependency_must_resolve_in_some_profile():
    with pytest.raises(ManifestDerivationError, match="another cluster") as error:
        derive_manifest(
            _cross_verifier_request(depends_on=("WP-NOWHERE-001",)),
            _cross_verifier_state(),
        )
    assert error.value.code == "dangling-dependency"


def test_provenance_configuration_must_match_selected_interactions():
    with pytest.raises(
        ManifestDerivationError, match="do not match selected interaction"
    ) as error:
        derive_manifest(make_request(), make_state(target="aarch64-unknown-linux-gnu"))
    assert error.value.code == "contradictory-source"


def test_function_paths_use_the_rust_identifier_for_hyphenated_cargo_crates():
    descriptor = make_state().descriptor
    descriptor["crates"] = [{"crate_dir": "crates/scheduler-core"}]

    manifest = derive_manifest(
        make_request(functions=("scheduler_core::TaskQueue::pop_ready",)),
        make_state(descriptor=descriptor),
    )

    assert manifest["functions"] == ["scheduler_core::TaskQueue::pop_ready"]


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


def test_virtual_adjudicator_pin_is_not_treated_as_a_workspace_gate_file():
    descriptor = make_state().descriptor
    descriptor["gate_integrity"] = [{"path": "@adjudicator"}]

    manifest = derive_manifest(make_request(), make_state(descriptor=descriptor))

    assert "@adjudicator" not in {
        entry["runner"] for entry in manifest["gate_integrity"]
    }


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


def test_descriptor_protected_root_is_preserved():
    state = make_state()
    descriptor = copy.deepcopy(state.descriptor)
    descriptor["write_set"]["protected_roots"].append("crates/*/Cargo.toml")
    request = make_request(
        allowed_write_set=("crates/scheduler/src/lib.rs",),
        protected_write_set=(
            "crates/*/specs/**",
            "ci/manifest/**",
            "scripts/**",
            "crates/*/Cargo.toml",
        )
    )

    manifest = derive_manifest(request, make_state(descriptor=descriptor))

    assert "crates/*/Cargo.toml" in manifest["write_policy"]["protected_write_set"]


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
