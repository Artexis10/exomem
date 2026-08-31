from __future__ import annotations

import asyncio
import copy
import dataclasses
import hashlib
import pickle
import sys
import threading
from pathlib import Path
from types import ModuleType

import pytest

_tests_package = ModuleType("tests")
_tests_package.__path__ = [str(Path(__file__).parent)]
sys.modules.setdefault("tests", _tests_package)

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


def _probe_authority(*, journal_digest: str = JOURNAL_DIGEST):
    from exomem.governance import consolidation_authority

    return consolidation_authority.issue_authority(
        vault_binding_digest=VAULT_BINDING,
        run_id=RUN_ID,
        operation_id=OPERATION_ID,
        journal_digest=journal_digest,
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
            plan=plan,
            probe=forged_probe,
        )

    route = consolidation_transport_verification.issue_transport_probe_route(
        _probe_authority(),
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


def test_self_consistent_but_never_admitted_plan_cannot_issue_a_route() -> None:
    from exomem.governance import consolidation_transport_verification as transport

    plan = _transport_plan()
    original = plan.probes[0]
    preliminary = dataclasses.replace(
        original,
        contract_digest="c" * 64,
        probe_digest="0" * 64,
    )
    forged_probe = dataclasses.replace(
        preliminary,
        probe_digest=transport._framed_digest(  # noqa: SLF001 - adversarial fixture
            transport._PROBE_DOMAIN,  # noqa: SLF001 - adversarial fixture
            transport._probe_value(preliminary),  # noqa: SLF001 - adversarial fixture
        ),
    )
    forged_probes = (forged_probe, *plan.probes[1:])
    value = {
        "schema": plan.schema,
        "basis": {
            **transport._basis_value(plan.basis),  # noqa: SLF001 - adversarial fixture
            "basis_digest": plan.basis.digest,
        },
        "probes": tuple(
            {
                **transport._probe_value(candidate),  # noqa: SLF001 - adversarial fixture
                "probe_digest": candidate.probe_digest,
            }
            for candidate in forged_probes
        ),
    }
    forged_plan = dataclasses.replace(
        plan,
        probes=forged_probes,
        digest=transport._framed_digest(  # noqa: SLF001 - adversarial fixture
            transport._PLAN_DOMAIN,  # noqa: SLF001 - adversarial fixture
            value,
        ),
    )
    with pytest.raises(transport.ConsolidationTransportVerificationUnavailable):
        transport.issue_transport_probe_route(
            _probe_authority(),
            plan=forged_plan,
            probe=forged_probe,
        )


def test_exact_destination_binding_journal_cannot_be_rebound_at_route_issuance() -> None:
    from exomem.governance import consolidation_transport_verification

    plan = _transport_plan()
    other_journal = "d" * 64
    with pytest.raises(
        consolidation_transport_verification.ConsolidationTransportVerificationUnavailable
    ):
        consolidation_transport_verification.issue_transport_probe_route(
            _probe_authority(journal_digest=other_journal),
            plan=plan,
            probe=plan.probes[0],
        )


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
            plan=(plan := _transport_plan()),
            probe=plan.probes[0],
        )
    with pytest.raises(
        consolidation_transport_verification.ConsolidationTransportVerificationUnavailable
    ):
        consolidation_transport_verification.issue_transport_probe_route(
            _probe_authority(journal_digest="f" * 64),
            plan=plan,
            probe=plan.probes[0],
        )


def _finalized_verification_journal(vault: Path):
    from exomem.governance import (
        consolidation_verification,
        consolidation_verification_journal,
    )

    def contract(probe_id: str) -> dict[str, str]:
        return {
            "probe_id": probe_id,
            "executor_id": "canonical-governance-surface-v1",
            "contract_digest": hashlib.sha256(
                f"{probe_id}:contract".encode()
            ).hexdigest(),
            "expected_result_digest": hashlib.sha256(
                f"{probe_id}:result".encode()
            ).hexdigest(),
        }

    verification_plan = consolidation_verification.build_verification_plan(
        positive_probes=(contract("transport-parent-positive"),),
        negative_probes=(contract("transport-parent-negative"),),
    )
    store = consolidation_verification_journal.ConsolidationVerificationJournalStore(
        vault,
        run_id=RUN_ID,
    )
    state = store.create(
        operation_id=OPERATION_ID,
        request_digest=hashlib.sha256(b"transport-request").hexdigest(),
        plan_digest=PLAN_DIGEST,
        rebuild_journal_digest=hashlib.sha256(b"transport-rebuild-journal").hexdigest(),
        canonical_census_digest=CENSUS_DIGEST,
        verification_plan=verification_plan,
        last_rebuild_terminal_event_id=f"{'1' * 64}:committed",
        last_rebuild_terminal_payload_digest="2" * 64,
        last_rebuild_effect_ordinal=20,
    )
    for probe in verification_plan.probes:
        state = store.record_probe_result(probe, probe.expected_result_digest)
        state = store.finalize_probe(
            probe,
            probe.expected_result_digest,
            terminal_event_id=f"{probe.ordinal + 3:064x}:committed",
            terminal_payload_digest=f"{probe.ordinal + 5:064x}",
            effect_journal_digest=f"{probe.ordinal + 7:064x}",
        )
    verification_result = hashlib.sha256(b"transport-parent-verified").hexdigest()
    state = store.record_terminal_result(verification_result)
    state = store.finalize_terminal(
        verification_result,
        terminal_event_id=f"{'9' * 64}:committed",
        terminal_payload_digest="a" * 64,
        effect_journal_digest="b" * 64,
    )
    return store, state


def test_transport_progress_chains_the_final_verification_journal(
    tmp_path: Path,
) -> None:
    from exomem.governance import (
        consolidation_transport_journal,
    )

    _verification_store, verified = _finalized_verification_journal(tmp_path)
    plan = _transport_plan()
    store = consolidation_transport_journal.ConsolidationTransportJournalStore(
        tmp_path,
        run_id=RUN_ID,
    )
    stop_effect = consolidation_transport_journal.ConsolidationTransportJournalEffect(
        kind="transport-stop",
        probe_ordinal=None,
        status="final",
        result_digest=plan.basis.routing_stop_digest,
        terminal_event_id=f"{'c' * 64}:committed",
        terminal_payload_digest="d" * 64,
        effect_journal_digest="e" * 64,
    )

    attached = store.create(
        verification_journal=verified,
        transport_plan=plan,
        transport_stop_effect=stop_effect,
    )

    assert attached.in_process_verification_basis_digest == verified.binding_digest
    assert attached.plan_digest == plan.digest
    assert attached.basis_digest == plan.basis.digest
    assert tuple(effect.kind for effect in attached.effects) == (
        "transport-stop",
        *("transport-probe" for _probe in plan.probes),
        "transport-verified",
        "routing-open",
        "complete",
    )
    assert tuple(
        effect.probe_ordinal
        for effect in attached.effects
        if effect.kind == "transport-probe"
    ) == tuple(range(len(plan.probes)))
    assert attached.effects[0] == stop_effect
    assert all(effect.status == "prior" for effect in attached.effects[1:])

    with pytest.raises(
        consolidation_transport_journal.ConsolidationTransportJournalUnavailable
    ):
        store.record_transport_effect_result(
            kind="transport-probe",
            probe_ordinal=1,
            result_digest=plan.probes[1].expected_result_digest,
        )

    probed = store.record_transport_effect_result(
        kind="transport-probe",
        probe_ordinal=0,
        result_digest=plan.probes[0].expected_result_digest,
    )
    probed = store.finalize_transport_effect(
        kind="transport-probe",
        probe_ordinal=0,
        result_digest=plan.probes[0].expected_result_digest,
        terminal_event_id=f"{'f' * 64}:committed",
        terminal_payload_digest="1" * 64,
        effect_journal_digest="2" * 64,
    )
    assert probed.effects[1].status == "final"


def test_transport_journal_is_digest_only_and_rejects_plan_drift(tmp_path: Path) -> None:
    from exomem.governance import (
        consolidation_transport_journal,
        consolidation_transport_verification,
    )

    _verification_store, verified = _finalized_verification_journal(tmp_path)
    plan = _transport_plan()
    store = consolidation_transport_journal.ConsolidationTransportJournalStore(
        tmp_path,
        run_id=RUN_ID,
    )
    stop_effect = consolidation_transport_journal.ConsolidationTransportJournalEffect(
        kind="transport-stop",
        probe_ordinal=None,
        status="final",
        result_digest=plan.basis.routing_stop_digest,
        terminal_event_id=f"{'c' * 64}:committed",
        terminal_payload_digest="d" * 64,
        effect_journal_digest="e" * 64,
    )
    state = store.create(
        verification_journal=verified,
        transport_plan=plan,
        transport_stop_effect=stop_effect,
    )
    assert state.plan_digest == plan.digest

    raw = store.path.read_bytes()
    assert b"transport-parent-positive" not in raw
    assert b"authority" not in raw.lower()
    assert b"exactdestinationbinding" not in raw.lower()

    changed_basis = _transport_basis(canonical_census_digest="f" * 64)
    changed = consolidation_transport_verification.build_transport_verification_plan(
        basis=changed_basis,
        contracts=_surface_contracts(),
        exact_destination_binding=_exact_destination_binding(changed_basis),
    )
    with pytest.raises(
        consolidation_transport_journal.ConsolidationTransportJournalUnavailable
    ):
        store.create(
            verification_journal=verified,
            transport_plan=changed,
            transport_stop_effect=stop_effect,
        )


def _verified_integration_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from exomem import writer_lease
    from exomem.governance import consolidation_verification_coordinator
    from tests.test_consolidation_verification import (  # noqa: PLC0415
        _install_passing_runner,
        _rebuilt_run,
        _verification_manifest,
    )

    monkeypatch.setenv(
        "EXOMEM_WRITER_LEASE_STATE_DIR",
        str(tmp_path / "writer-state"),
    )
    writer_lease.reset_managers_for_tests()
    manifest = _verification_manifest()
    vault, arguments, _plan = _rebuilt_run(
        tmp_path,
        monkeypatch,
        manifest=manifest,
    )
    calls: list[str] = []
    _install_passing_runner(monkeypatch, calls)
    verified = consolidation_verification_coordinator.verify_rebuilt_destination(
        **arguments,
    )
    assert verified.seal_state.phase == "verified"
    return vault, arguments, manifest, verified


def _integration_transport_basis(arguments, manifest, verified):
    return {
        "schema": "exomem.consolidation-transport-verification-basis/v1",
        "vault_binding_digest": arguments["vault_binding_digest"],
        "run_id": arguments["run_id"],
        "operation_id": arguments["operation_id"],
        "plan_digest": arguments["plan_digest"],
        "verification_manifest_digest": manifest.digest,
        "canonical_census_digest": verified.verification_journal.canonical_census_digest,
        "release_build_digest": hashlib.sha256(b"integration-release").hexdigest(),
        "surface_profile": "standalone-v1",
        "surface_descriptor_digest": hashlib.sha256(
            b"integration-descriptor"
        ).hexdigest(),
        "configuration_digest": hashlib.sha256(b"integration-config").hexdigest(),
        "trust_digest": hashlib.sha256(b"integration-trust").hexdigest(),
        "principal_mapping_digest": hashlib.sha256(
            b"integration-principals"
        ).hexdigest(),
        "routing_stop_digest": hashlib.sha256(b"integration-routing-stop").hexdigest(),
        "transport_supervisor_readiness_digest": hashlib.sha256(
            b"integration-supervisor"
        ).hexdigest(),
        "hosted_profile_selection_record_digest": hashlib.sha256(
            b"integration-hosted-selection"
        ).hexdigest(),
        "hosted_profile_selection_verifier_generation": 9,
        "hosted_owner_entitlement_verifier_readiness_digest": hashlib.sha256(
            b"integration-hosted-entitlement"
        ).hexdigest(),
    }


def _pre_stop_basis_digest(basis):
    from exomem.governance import consolidation_transport_verification

    return consolidation_transport_verification.transport_verification_basis_fingerprint(
        basis
    )


def test_transport_coordinator_stops_probes_and_opens_in_receipt_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import (
        consolidation_admission,
        consolidation_receipts,
        consolidation_transport_coordinator,
    )

    vault, arguments, manifest, verified = _verified_integration_run(
        tmp_path,
        monkeypatch,
    )
    basis = _integration_transport_basis(arguments, manifest, verified)
    calls: list[str] = []

    class Supervisor(consolidation_transport_coordinator.TransportSupervisor):
        def __init__(self) -> None:
            self.stopped = False
            self.opened = False

        def revalidate_pre_stop_basis(self, basis) -> str:
            return _pre_stop_basis_digest(basis)

        def classify_stop(self, context):
            return consolidation_transport_coordinator.TransportRoutingObservation(
                state="target" if self.stopped else "prior",
                digest=(
                    context.target_digest if self.stopped else context.prior_digest
                ),
            )

        def stop_and_drain(self, context) -> None:
            calls.append("stop")
            self.stopped = True

        def revalidate_basis(self, plan) -> str:
            calls.append("revalidate")
            return plan.basis.digest

        def run_probe(self, probe, context):
            assert not hasattr(context, "authority")
            assert not hasattr(context, "route")
            assert context.plan_digest
            calls.append(probe.probe_id)
            with context.admission.admit_read():
                pass
            with pytest.raises(
                consolidation_admission.ConsolidationAdmissionUnavailable,
                match="^CONSOLIDATION_SEALED$",
            ):
                with context.admission.admit_mutation():
                    pass
            return consolidation_transport_coordinator.TransportProbeTerminal(
                schema=consolidation_transport_coordinator.TRANSPORT_PROBE_TERMINAL_SCHEMA,
                probe_id=probe.probe_id,
                probe_digest=probe.probe_digest,
                result_digest=probe.expected_result_digest,
                outcome="passed",
            )

        def classify_open(self, context):
            return consolidation_transport_coordinator.TransportRoutingObservation(
                state="target" if self.opened else "prior",
                digest=(
                    context.target_digest if self.opened else context.prior_digest
                ),
            )

        def open_routing(self, context) -> None:
            calls.append("open")
            self.opened = True

    supervisor = Supervisor()
    result = consolidation_transport_coordinator.verify_exact_cell_transport(
        vault_root=vault,
        admission=arguments["admission"],
        journal_digest=arguments["journal_digest"],
        basis=basis,
        contracts=_surface_contracts(),
        supervisor=supervisor,
        recorded_at="2026-08-31T12:00:09.000Z",
    )

    assert supervisor.stopped and supervisor.opened
    assert result.seal_state.phase == "routing-opening"
    assert tuple(effect.status for effect in result.transport_journal.effects) == (
        *("final" for _effect in result.transport_journal.effects[:-1]),
        "prior",
    )
    assert calls == [
        "stop",
        "revalidate",
        *(probe["probe_id"] for probe in _surface_contracts()),
        "revalidate",
        "revalidate",
        "open",
    ]
    records = [
        consolidation_receipts.validate_nested(
            record["consolidation_event"],
            outer_phase=record["phase"],
        )
        for record in consolidation_receipts._active_records(vault)  # noqa: SLF001
        if record.get("event_type") == "consolidation"
        and isinstance(record.get("consolidation_event"), dict)
        and record["consolidation_event"].get("kind")
        in {
            "transport-stop",
            "transport-probe",
            "transport-verified",
            "routing-open",
        }
    ]
    assert [record["kind"] for record in records if record["record_role"] == "intent"] == [
        "transport-stop",
        *("transport-probe" for _probe in _surface_contracts()),
        "transport-verified",
        "routing-open",
    ]
    with pytest.raises(
        consolidation_admission.ConsolidationAdmissionUnavailable,
        match="^CONSOLIDATION_SEALED$",
    ):
        with arguments["admission"].admit_read():
            pass


def test_transport_probe_failure_never_opens_routing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import (
        consolidation_admission,
        consolidation_transport_coordinator,
    )

    vault, arguments, manifest, verified = _verified_integration_run(
        tmp_path,
        monkeypatch,
    )
    basis = _integration_transport_basis(arguments, manifest, verified)

    class FailingSupervisor(consolidation_transport_coordinator.TransportSupervisor):
        stopped = False
        opened = False

        def revalidate_pre_stop_basis(self, basis) -> str:
            return _pre_stop_basis_digest(basis)

        def classify_stop(self, context):
            return consolidation_transport_coordinator.TransportRoutingObservation(
                state="target" if self.stopped else "prior",
                digest=context.target_digest if self.stopped else context.prior_digest,
            )

        def stop_and_drain(self, _context) -> None:
            self.stopped = True

        def revalidate_basis(self, plan) -> str:
            return plan.basis.digest

        def run_probe(self, probe, _context):
            if probe.ordinal == 1:
                raise RuntimeError("private transport detail")
            return consolidation_transport_coordinator.TransportProbeTerminal(
                schema=consolidation_transport_coordinator.TRANSPORT_PROBE_TERMINAL_SCHEMA,
                probe_id=probe.probe_id,
                probe_digest=probe.probe_digest,
                result_digest=probe.expected_result_digest,
                outcome="passed",
            )

        def classify_open(self, context):
            return consolidation_transport_coordinator.TransportRoutingObservation(
                state="prior",
                digest=context.prior_digest,
            )

        def open_routing(self, _context) -> None:
            self.opened = True

    supervisor = FailingSupervisor()
    with pytest.raises(
        consolidation_transport_coordinator.ConsolidationTransportCoordinatorUnavailable
    ):
        consolidation_transport_coordinator.verify_exact_cell_transport(
            vault_root=vault,
            admission=arguments["admission"],
            journal_digest=arguments["journal_digest"],
            basis=basis,
            contracts=_surface_contracts(),
            supervisor=supervisor,
            recorded_at="2026-08-31T12:00:09.000Z",
        )

    assert supervisor.stopped and not supervisor.opened
    assert arguments["admission"].reload().state.phase == "transport-verifying"
    with pytest.raises(
        consolidation_admission.ConsolidationAdmissionUnavailable,
        match="^CONSOLIDATION_SEALED$",
    ):
        with arguments["admission"].admit_read():
            pass


def test_transport_basis_drift_before_open_keeps_routing_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_transport_coordinator

    vault, arguments, manifest, verified = _verified_integration_run(
        tmp_path,
        monkeypatch,
    )
    basis = _integration_transport_basis(arguments, manifest, verified)

    class DriftingSupervisor(consolidation_transport_coordinator.TransportSupervisor):
        stopped = False
        opened = False
        revalidations = 0

        def revalidate_pre_stop_basis(self, basis) -> str:
            return _pre_stop_basis_digest(basis)

        def classify_stop(self, context):
            return consolidation_transport_coordinator.TransportRoutingObservation(
                state="target" if self.stopped else "prior",
                digest=context.target_digest if self.stopped else context.prior_digest,
            )

        def stop_and_drain(self, _context) -> None:
            self.stopped = True

        def revalidate_basis(self, plan) -> str:
            self.revalidations += 1
            return plan.basis.digest if self.revalidations < 3 else "f" * 64

        def run_probe(self, probe, _context):
            return consolidation_transport_coordinator.TransportProbeTerminal(
                schema=consolidation_transport_coordinator.TRANSPORT_PROBE_TERMINAL_SCHEMA,
                probe_id=probe.probe_id,
                probe_digest=probe.probe_digest,
                result_digest=probe.expected_result_digest,
                outcome="passed",
            )

        def classify_open(self, context):
            return consolidation_transport_coordinator.TransportRoutingObservation(
                state="prior",
                digest=context.prior_digest,
            )

        def open_routing(self, _context) -> None:
            self.opened = True

    supervisor = DriftingSupervisor()
    with pytest.raises(
        consolidation_transport_coordinator.ConsolidationTransportCoordinatorUnavailable
    ):
        consolidation_transport_coordinator.verify_exact_cell_transport(
            vault_root=vault,
            admission=arguments["admission"],
            journal_digest=arguments["journal_digest"],
            basis=basis,
            contracts=_surface_contracts(),
            supervisor=supervisor,
            recorded_at="2026-08-31T12:00:09.000Z",
        )

    assert supervisor.stopped and not supervisor.opened
    assert arguments["admission"].reload().state.phase == "transport-verified"


def test_transport_readiness_drift_refuses_before_stopping_routing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import consolidation_transport_coordinator

    vault, arguments, manifest, verified = _verified_integration_run(
        tmp_path,
        monkeypatch,
    )
    basis = _integration_transport_basis(arguments, manifest, verified)

    class RefusingSupervisor(consolidation_transport_coordinator.TransportSupervisor):
        stopped = False

        def revalidate_pre_stop_basis(self, _basis) -> str:
            return "f" * 64

        def classify_stop(self, _context):
            raise AssertionError("routing classification must follow readiness")

        def stop_and_drain(self, _context) -> None:
            self.stopped = True

        def revalidate_basis(self, _plan) -> str:
            raise AssertionError("the exact stopped-cell plan must not exist")

        def run_probe(self, _probe, _context):
            raise AssertionError("probes must not run")

        def classify_open(self, _context):
            raise AssertionError("routing must remain closed")

        def open_routing(self, _context) -> None:
            raise AssertionError("routing must remain closed")

    supervisor = RefusingSupervisor()
    with pytest.raises(
        consolidation_transport_coordinator.ConsolidationTransportCoordinatorUnavailable
    ):
        consolidation_transport_coordinator.verify_exact_cell_transport(
            vault_root=vault,
            admission=arguments["admission"],
            journal_digest=arguments["journal_digest"],
            basis=basis,
            contracts=_surface_contracts(),
            supervisor=supervisor,
            recorded_at="2026-08-31T12:00:09.000Z",
        )

    assert not supervisor.stopped
    assert arguments["admission"].reload().state.phase == "verified"


def test_transport_retry_adopts_final_effects_without_repeating_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import (
        consolidation_receipts,
        consolidation_transport_coordinator,
    )

    vault, arguments, manifest, verified = _verified_integration_run(
        tmp_path,
        monkeypatch,
    )
    basis = _integration_transport_basis(arguments, manifest, verified)

    class StableSupervisor(consolidation_transport_coordinator.TransportSupervisor):
        stopped = False
        opened = False
        stop_calls = 0
        open_calls = 0
        probe_calls = 0

        def revalidate_pre_stop_basis(self, basis) -> str:
            return _pre_stop_basis_digest(basis)

        def classify_stop(self, context):
            return consolidation_transport_coordinator.TransportRoutingObservation(
                state="target" if self.stopped else "prior",
                digest=context.target_digest if self.stopped else context.prior_digest,
            )

        def stop_and_drain(self, _context) -> None:
            self.stop_calls += 1
            self.stopped = True

        def revalidate_basis(self, plan) -> str:
            return plan.basis.digest

        def run_probe(self, probe, _context):
            self.probe_calls += 1
            return consolidation_transport_coordinator.TransportProbeTerminal(
                schema=consolidation_transport_coordinator.TRANSPORT_PROBE_TERMINAL_SCHEMA,
                probe_id=probe.probe_id,
                probe_digest=probe.probe_digest,
                result_digest=probe.expected_result_digest,
                outcome="passed",
            )

        def classify_open(self, context):
            return consolidation_transport_coordinator.TransportRoutingObservation(
                state="target" if self.opened else "prior",
                digest=context.target_digest if self.opened else context.prior_digest,
            )

        def open_routing(self, _context) -> None:
            self.open_calls += 1
            self.opened = True

    supervisor = StableSupervisor()
    call = {
        "vault_root": vault,
        "admission": arguments["admission"],
        "journal_digest": arguments["journal_digest"],
        "basis": basis,
        "contracts": _surface_contracts(),
        "supervisor": supervisor,
        "recorded_at": "2026-08-31T12:00:09.000Z",
    }
    first = consolidation_transport_coordinator.verify_exact_cell_transport(**call)
    first_ids = tuple(
        record["event_id"]
        for record in consolidation_receipts._active_records(vault)  # noqa: SLF001
    )
    second = consolidation_transport_coordinator.verify_exact_cell_transport(**call)
    second_ids = tuple(
        record["event_id"]
        for record in consolidation_receipts._active_records(vault)  # noqa: SLF001
    )

    assert first.transport_journal == second.transport_journal
    assert first_ids == second_ids
    assert supervisor.stop_calls == 1
    assert supervisor.open_calls == 1
    assert supervisor.probe_calls == len(_surface_contracts())


@pytest.mark.parametrize(
    "crash_at",
    (
        "after-stop-effect-final",
        "after-transport-journal",
        "after-transport-verifying",
        "after-probe-route:0",
        "after-probe-result:0",
        "after-probe-effect-final:0",
        "after-probe-aggregate-final:0",
        "after-transport-verified-effect-final",
        "after-transport-verified-aggregate-final",
        "after-transport-verified-phase",
        "after-routing-opening-phase",
        "after-routing-open-side-effect",
        "after-routing-open-effect-final",
        "after-routing-open-aggregate-final",
    ),
)
def test_transport_cross_store_crash_stays_sealed_and_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_at: str,
) -> None:
    from exomem.governance import (
        consolidation_admission,
        consolidation_transport_coordinator,
    )

    vault, arguments, manifest, verified = _verified_integration_run(
        tmp_path,
        monkeypatch,
    )
    basis = _integration_transport_basis(arguments, manifest, verified)

    class CrashSupervisor(consolidation_transport_coordinator.TransportSupervisor):
        stopped = False
        opened = False

        def revalidate_pre_stop_basis(self, current) -> str:
            return _pre_stop_basis_digest(current)

        def classify_stop(self, context):
            return consolidation_transport_coordinator.TransportRoutingObservation(
                state="target" if self.stopped else "prior",
                digest=context.target_digest if self.stopped else context.prior_digest,
            )

        def stop_and_drain(self, _context) -> None:
            self.stopped = True

        def revalidate_basis(self, plan) -> str:
            return plan.basis.digest

        def run_probe(self, probe, _context):
            return consolidation_transport_coordinator.TransportProbeTerminal(
                schema=consolidation_transport_coordinator.TRANSPORT_PROBE_TERMINAL_SCHEMA,
                probe_id=probe.probe_id,
                probe_digest=probe.probe_digest,
                result_digest=probe.expected_result_digest,
                outcome="passed",
            )

        def classify_open(self, context):
            return consolidation_transport_coordinator.TransportRoutingObservation(
                state="target" if self.opened else "prior",
                digest=context.target_digest if self.opened else context.prior_digest,
            )

        def open_routing(self, _context) -> None:
            self.opened = True

    def crash(point: str) -> None:
        if point == crash_at:
            raise RuntimeError("simulated crash")

    supervisor = CrashSupervisor()
    call = {
        "vault_root": vault,
        "admission": arguments["admission"],
        "journal_digest": arguments["journal_digest"],
        "basis": basis,
        "contracts": _surface_contracts(),
        "supervisor": supervisor,
        "recorded_at": "2026-08-31T12:00:09.000Z",
    }
    monkeypatch.setattr(consolidation_transport_coordinator, "_crash_point", crash)
    with pytest.raises(
        consolidation_transport_coordinator.ConsolidationTransportCoordinatorUnavailable
    ):
        consolidation_transport_coordinator.verify_exact_cell_transport(**call)

    with pytest.raises(
        consolidation_admission.ConsolidationAdmissionUnavailable,
        match="^CONSOLIDATION_SEALED$",
    ):
        with arguments["admission"].admit_read():
            pass

    monkeypatch.setattr(
        consolidation_transport_coordinator,
        "_crash_point",
        lambda _point: None,
    )
    resumed = consolidation_transport_coordinator.verify_exact_cell_transport(**call)
    assert resumed.seal_state.phase == "routing-opening"
    assert all(effect.status == "final" for effect in resumed.transport_journal.effects[:-1])
