from __future__ import annotations

import asyncio
import copy
import hashlib
import pickle
import threading
from pathlib import Path

import pytest

VAULT_BINDING = hashlib.sha256(b"transport-vault-binding").hexdigest()
RUN_ID = "00000000-0000-4000-8000-000000000121"
OPERATION_ID = "00000000-0000-4000-8000-000000000122"
JOURNAL_DIGEST = hashlib.sha256(b"transport-apply-journal").hexdigest()
PLAN_DIGEST = hashlib.sha256(b"transport-cutover-plan").hexdigest()
MANIFEST_DIGEST = hashlib.sha256(b"transport-verification-manifest").hexdigest()
CENSUS_DIGEST = hashlib.sha256(b"transport-post-cutover-census").hexdigest()
RELEASE_BUILD_DIGEST = hashlib.sha256(b"transport-release-build").hexdigest()
DESCRIPTOR_DIGEST = hashlib.sha256(b"transport-surface-descriptor").hexdigest()
CONFIGURATION_DIGEST = hashlib.sha256(b"transport-configuration").hexdigest()
TRUST_DIGEST = hashlib.sha256(b"transport-trust").hexdigest()
PRINCIPAL_MAPPING_DIGEST = hashlib.sha256(b"transport-principal-mapping").hexdigest()
ROUTING_STOP_DIGEST = hashlib.sha256(b"transport-routing-stop").hexdigest()
SUPERVISOR_READINESS_DIGEST = hashlib.sha256(b"transport-supervisor-readiness").hexdigest()
HOSTED_PROFILE_SELECTION_DIGEST = hashlib.sha256(
    b"transport-hosted-profile-selection"
).hexdigest()
HOSTED_OWNER_ENTITLEMENT_READINESS_DIGEST = hashlib.sha256(
    b"transport-hosted-owner-entitlement-readiness"
).hexdigest()
T0 = "2026-08-31T12:00:00.000Z"


def _sealed_at_transport_verifying(vault: Path) -> None:
    from exomem.governance import consolidation_authority, consolidation_seal

    store = consolidation_seal.ConsolidationSealStore(vault)
    current = store.initialize_open(
        vault_binding_digest=VAULT_BINDING,
        recorded_at=T0,
    )
    current = store.begin_consolidation(
        vault_binding_digest=VAULT_BINDING,
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        journal_digest=JOURNAL_DIGEST,
        sealed_at=T0,
        expected_revision=current.revision,
    )
    for target_phase in (
        "sealed",
        "preimage-ready",
        "policy-active",
        "publishing",
        "rebuilding",
        "verifying",
        "verified",
        "transport-stopping",
        "transport-verifying",
    ):
        authority = consolidation_authority.issue_authority(
            vault_binding_digest=VAULT_BINDING,
            run_id=RUN_ID,
            operation_id=OPERATION_ID,
            journal_digest=JOURNAL_DIGEST,
            phase=current.phase,
            action="apply",
        )
        current = store.advance_consolidation(
            authority,
            vault_binding_digest=VAULT_BINDING,
            action="apply",
            target_phase=target_phase,
            recorded_at=T0,
            expected_revision=current.revision,
        )


def _probe_authority():
    from exomem.governance import consolidation_authority

    return consolidation_authority.issue_authority(
        vault_binding_digest=VAULT_BINDING,
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        journal_digest=JOURNAL_DIGEST,
        phase="transport-verifying",
        action="probe",
    )


def _transport_basis(**changes: object) -> dict[str, object]:
    return {
        "schema": "exomem.consolidation-transport-verification-basis/v1",
        "vault_binding_digest": VAULT_BINDING,
        "run_id": RUN_ID,
        "operation_id": OPERATION_ID,
        "plan_digest": PLAN_DIGEST,
        "verification_manifest_digest": MANIFEST_DIGEST,
        "canonical_census_digest": CENSUS_DIGEST,
        "release_build_digest": RELEASE_BUILD_DIGEST,
        "surface_profile": "standalone-v1",
        "surface_descriptor_digest": DESCRIPTOR_DIGEST,
        "configuration_digest": CONFIGURATION_DIGEST,
        "trust_digest": TRUST_DIGEST,
        "principal_mapping_digest": PRINCIPAL_MAPPING_DIGEST,
        "routing_stop_digest": ROUTING_STOP_DIGEST,
        "transport_supervisor_readiness_digest": SUPERVISOR_READINESS_DIGEST,
        "hosted_profile_selection_record_digest": HOSTED_PROFILE_SELECTION_DIGEST,
        "hosted_profile_selection_verifier_generation": 7,
        "hosted_owner_entitlement_verifier_readiness_digest": (
            HOSTED_OWNER_ENTITLEMENT_READINESS_DIGEST
        ),
        **changes,
    }


def _surface_contracts() -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "probe_id": f"{surface}-{kind}",
            "probe_kind": kind,
            "surface": surface,
            "contract_digest": hashlib.sha256(
                f"{surface}-{kind}-contract".encode()
            ).hexdigest(),
            "expected_result_digest": hashlib.sha256(
                f"{surface}-{kind}-result".encode()
            ).hexdigest(),
        }
        for surface in ("mcp", "rest", "hosted", "cli")
        for kind in ("positive", "negative")
    )


def _exact_destination_binding(basis: dict[str, object]):
    from exomem.governance import (
        consolidation_authority,
        consolidation_transport_verification,
    )

    authority = consolidation_authority.issue_authority(
        vault_binding_digest=VAULT_BINDING,
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        journal_digest=JOURNAL_DIGEST,
        phase="transport-stopping",
        action="apply",
    )
    return consolidation_transport_verification.issue_exact_destination_binding(
        authority,
        journal_digest=JOURNAL_DIGEST,
        basis=basis,
    )


def _transport_plan():
    from exomem.governance import consolidation_transport_verification

    basis = _transport_basis()
    return consolidation_transport_verification.build_transport_verification_plan(
        basis=basis,
        contracts=_surface_contracts(),
        exact_destination_binding=_exact_destination_binding(basis),
    )


def test_transport_plan_binds_exact_real_cell_basis_and_every_surface_polarity() -> None:
    plan = _transport_plan()

    assert plan.basis.destination_kind == "real"
    assert plan.basis.canonical_census_digest == CENSUS_DIGEST
    assert plan.basis.routing_stop_digest == ROUTING_STOP_DIGEST
    assert tuple((probe.surface, probe.probe_kind) for probe in plan.probes) == tuple(
        (surface, kind)
        for surface in ("mcp", "rest", "hosted", "cli")
        for kind in ("positive", "negative")
    )
    assert len(plan.digest) == 64


@pytest.mark.parametrize(
    "contracts",
    [
        _surface_contracts()[:-1],
        tuple(
            contract
            for contract in _surface_contracts()
            if not (contract["surface"] == "mcp" and contract["probe_kind"] == "positive")
        ),
    ],
)
def test_transport_plan_refuses_missing_surface_or_polarity(
    contracts: tuple[dict[str, object], ...],
) -> None:
    from exomem.governance import consolidation_transport_verification

    with pytest.raises(
        consolidation_transport_verification.ConsolidationTransportVerificationUnavailable
    ):
        consolidation_transport_verification.build_transport_verification_plan(
            basis=(basis := _transport_basis()),
            contracts=contracts,
            exact_destination_binding=_exact_destination_binding(basis),
        )


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("canonical_census_digest", "0" * 64),
        ("release_build_digest", "1" * 64),
        ("surface_descriptor_digest", "2" * 64),
        ("configuration_digest", "3" * 64),
        ("trust_digest", "4" * 64),
        ("principal_mapping_digest", "5" * 64),
        ("routing_stop_digest", "6" * 64),
        ("transport_supervisor_readiness_digest", "7" * 64),
        ("hosted_profile_selection_record_digest", "8" * 64),
        ("hosted_profile_selection_verifier_generation", 8),
        ("hosted_owner_entitlement_verifier_readiness_digest", "a" * 64),
    ],
)
def test_each_transport_basis_field_changes_the_plan_digest(field: str, changed: object) -> None:
    from exomem.governance import consolidation_transport_verification

    baseline = _transport_plan()
    mutated_basis = _transport_basis(**{field: changed})
    mutated = consolidation_transport_verification.build_transport_verification_plan(
        basis=mutated_basis,
        contracts=_surface_contracts(),
        exact_destination_binding=_exact_destination_binding(mutated_basis),
    )
    assert mutated.digest != baseline.digest


def test_exact_destination_binding_cannot_be_reused_after_basis_drift() -> None:
    from exomem.governance import consolidation_transport_verification

    basis = _transport_basis()
    binding = _exact_destination_binding(basis)
    with pytest.raises(
        consolidation_transport_verification.ConsolidationTransportVerificationUnavailable
    ):
        consolidation_transport_verification.build_transport_verification_plan(
            basis={**basis, "canonical_census_digest": "b" * 64},
            contracts=_surface_contracts(),
            exact_destination_binding=binding,
        )


def test_caller_label_or_forged_binding_cannot_make_clone_evidence_real() -> None:
    from exomem.governance import consolidation_transport_verification

    basis = _transport_basis(destination_kind="real")
    with pytest.raises(
        consolidation_transport_verification.ConsolidationTransportVerificationUnavailable
    ):
        consolidation_transport_verification.build_transport_verification_plan(
            basis=basis,
            contracts=_surface_contracts(),
            exact_destination_binding=object(),
        )


def test_exact_supervised_route_bypasses_only_read_admission_and_never_request_auth(
    tmp_path: Path,
) -> None:
    from exomem.governance import (
        consolidation_admission,
        consolidation_transport_verification,
    )

    _sealed_at_transport_verifying(tmp_path)
    admission = consolidation_admission.ConsolidationAdmission(
        tmp_path,
        vault_binding_digest=VAULT_BINDING,
    )
    with pytest.raises(
        consolidation_admission.ConsolidationAdmissionUnavailable,
        match="^CONSOLIDATION_SEALED$",
    ):
        with admission.admit_read():
            pass

    plan = _transport_plan()
    probe = next(
        candidate
        for candidate in plan.probes
        if candidate.surface == "rest" and candidate.probe_kind == "positive"
    )
    route = consolidation_transport_verification.issue_transport_probe_route(
        _probe_authority(),
        journal_digest=JOURNAL_DIGEST,
        plan=plan,
        probe=probe,
    )
    request = {
        "command": "ask_memory",
        "arguments": {"query": "ordinary normal-auth request"},
        "authorization": "ordinary-session",
    }
    assert "authority" not in repr(request).lower()
    assert "probe_digest" not in repr(request)

    with consolidation_transport_verification.transport_probe_route_scope(
        route,
        plan=plan,
        probe=probe,
    ):
        with admission.admit_read():
            pass
        for enter in (
            admission.admit_mutation,
            admission.admit_transfer,
            admission.admit_background,
        ):
            with pytest.raises(
                consolidation_admission.ConsolidationAdmissionUnavailable,
                match="^CONSOLIDATION_SEALED$",
            ):
                with enter():
                    pass

    with pytest.raises(
        consolidation_admission.ConsolidationAdmissionUnavailable,
        match="^CONSOLIDATION_SEALED$",
    ):
        with admission.admit_read():
            pass


def test_transport_route_is_process_local_unforgeable_and_not_inherited_by_thread(
    tmp_path: Path,
) -> None:
    from exomem.governance import (
        consolidation_admission,
        consolidation_transport_verification,
    )

    _sealed_at_transport_verifying(tmp_path)
    admission = consolidation_admission.ConsolidationAdmission(
        tmp_path,
        vault_binding_digest=VAULT_BINDING,
    )
    plan = _transport_plan()
    probe = plan.probes[0]
    route = consolidation_transport_verification.issue_transport_probe_route(
        _probe_authority(),
        journal_digest=JOURNAL_DIGEST,
        plan=plan,
        probe=probe,
    )
    for serializer in (pickle.dumps, copy.copy, copy.deepcopy):
        with pytest.raises(TypeError):
            serializer(route)

    failures: list[str] = []

    def unrelated_client() -> None:
        try:
            with admission.admit_read():
                pass
        except consolidation_admission.ConsolidationAdmissionUnavailable as error:
            failures.append(error.code)

    with consolidation_transport_verification.transport_probe_route_scope(
        route,
        plan=plan,
        probe=probe,
    ):
        worker = threading.Thread(target=unrelated_client)
        worker.start()
        worker.join()
        with admission.admit_read():
            pass
    assert failures == ["CONSOLIDATION_SEALED"]


def test_transport_route_is_revoked_in_async_child_when_parent_scope_exits(
    tmp_path: Path,
) -> None:
    from exomem.governance import (
        consolidation_admission,
        consolidation_transport_verification,
    )

    _sealed_at_transport_verifying(tmp_path)
    admission = consolidation_admission.ConsolidationAdmission(
        tmp_path,
        vault_binding_digest=VAULT_BINDING,
    )
    plan = _transport_plan()
    probe = plan.probes[0]
    route = consolidation_transport_verification.issue_transport_probe_route(
        _probe_authority(),
        journal_digest=JOURNAL_DIGEST,
        plan=plan,
        probe=probe,
    )

    async def exercise() -> str:
        release = asyncio.Event()

        async def delayed_child() -> str:
            await release.wait()
            try:
                with admission.admit_read():
                    pass
            except consolidation_admission.ConsolidationAdmissionUnavailable as error:
                return error.code
            return "admitted"

        with consolidation_transport_verification.transport_probe_route_scope(
            route,
            plan=plan,
            probe=probe,
        ):
            child = asyncio.create_task(delayed_child())
        release.set()
        return await child

    assert asyncio.run(exercise()) == "CONSOLIDATION_SEALED"


def test_nonmember_probe_cannot_issue_or_enter_a_transport_route() -> None:
    from exomem.governance import consolidation_transport_verification

    plan = _transport_plan()
    foreign_plan = _transport_plan()
    foreign_probe = copy.copy(foreign_plan.probes[0])
    forged_probe = consolidation_transport_verification.TransportProbe(
        schema=foreign_probe.schema,
        ordinal=foreign_probe.ordinal,
        probe_id=foreign_probe.probe_id,
        probe_kind=foreign_probe.probe_kind,
        surface=foreign_probe.surface,
        contract_digest=foreign_probe.contract_digest,
        expected_result_digest=foreign_probe.expected_result_digest,
        probe_digest="9" * 64,
    )
    with pytest.raises(
        consolidation_transport_verification.ConsolidationTransportVerificationUnavailable
    ):
        consolidation_transport_verification.issue_transport_probe_route(
            _probe_authority(),
            journal_digest=JOURNAL_DIGEST,
            plan=plan,
            probe=forged_probe,
        )

    route = consolidation_transport_verification.issue_transport_probe_route(
        _probe_authority(),
        journal_digest=JOURNAL_DIGEST,
        plan=plan,
        probe=plan.probes[0],
    )
    with pytest.raises(
        consolidation_transport_verification.ConsolidationTransportVerificationUnavailable
    ):
        with consolidation_transport_verification.transport_probe_route_scope(
            route,
            plan=plan,
            probe=forged_probe,
        ):
            pass


def test_wrong_transport_authority_or_binding_never_opens_probe_route() -> None:
    from exomem.governance import (
        consolidation_authority,
        consolidation_transport_verification,
    )

    wrong_phase = consolidation_authority.issue_authority(
        vault_binding_digest=VAULT_BINDING,
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        journal_digest=JOURNAL_DIGEST,
        phase="verified",
        action="apply",
    )
    with pytest.raises(
        consolidation_transport_verification.ConsolidationTransportVerificationUnavailable
    ):
        consolidation_transport_verification.issue_transport_probe_route(
            wrong_phase,
            journal_digest=JOURNAL_DIGEST,
            plan=(plan := _transport_plan()),
            probe=plan.probes[0],
        )
    with pytest.raises(
        consolidation_transport_verification.ConsolidationTransportVerificationUnavailable
    ):
        consolidation_transport_verification.issue_transport_probe_route(
            _probe_authority(),
            journal_digest="f" * 64,
            plan=plan,
            probe=plan.probes[0],
        )
