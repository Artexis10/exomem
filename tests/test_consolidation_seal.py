from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import pickle

import pytest


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _authority():
    from exomem.governance import consolidation_authority

    return consolidation_authority.issue_authority(
        vault_binding_digest=_digest("vault-binding"),
        run_id="00000000-0000-4000-8000-000000000001",
        operation_id="00000000-0000-4000-8000-000000000042",
        journal_digest=_digest("apply-journal"),
        phase="transport-verifying",
        action="probe",
    )


def test_consolidation_authority_is_exactly_vault_run_journal_phase_action_bound() -> None:
    from exomem.governance import consolidation_authority

    authority = _authority()
    consolidation_authority.require_authority(
        authority,
        vault_binding_digest=_digest("vault-binding"),
        run_id="00000000-0000-4000-8000-000000000001",
        operation_id="00000000-0000-4000-8000-000000000042",
        journal_digest=_digest("apply-journal"),
        phase="transport-verifying",
        action="probe",
    )
    assert repr(authority) == "<ConsolidationAuthority process-local>"

    substitutions = {
        "vault_binding_digest": _digest("other-vault"),
        "run_id": "00000000-0000-4000-8000-000000000002",
        "operation_id": "00000000-0000-4000-8000-000000000043",
        "journal_digest": _digest("other-journal"),
        "phase": "verifying",
        "action": "verify",
    }
    baseline = {
        "vault_binding_digest": _digest("vault-binding"),
        "run_id": "00000000-0000-4000-8000-000000000001",
        "operation_id": "00000000-0000-4000-8000-000000000042",
        "journal_digest": _digest("apply-journal"),
        "phase": "transport-verifying",
        "action": "probe",
    }
    for field, changed in substitutions.items():
        with pytest.raises(consolidation_authority.ConsolidationAuthorityUnavailable):
            consolidation_authority.require_authority(
                authority,
                **{**baseline, field: changed},
            )


def test_consolidation_authority_cannot_serialize_copy_or_round_trip_request_data() -> None:
    from exomem.governance import consolidation_authority

    authority = _authority()
    for serializer in (
        pickle.dumps,
        copy.copy,
        copy.deepcopy,
        json.dumps,
        vars,
        dataclasses.asdict,
    ):
        with pytest.raises(TypeError):
            serializer(authority)

    request_value = {
        "vault_binding_digest": _digest("vault-binding"),
        "run_id": "00000000-0000-4000-8000-000000000001",
        "operation_id": "00000000-0000-4000-8000-000000000042",
        "journal_digest": _digest("apply-journal"),
        "phase": "transport-verifying",
        "action": "probe",
    }
    with pytest.raises(consolidation_authority.ConsolidationAuthorityUnavailable):
        consolidation_authority.require_authority(authority=request_value, **request_value)


def test_probe_authority_exists_only_for_the_transport_verifying_phase() -> None:
    from exomem.governance import consolidation_authority

    with pytest.raises(consolidation_authority.ConsolidationAuthorityUnavailable):
        consolidation_authority.issue_authority(
            vault_binding_digest=_digest("vault-binding"),
            run_id="00000000-0000-4000-8000-000000000001",
            operation_id="00000000-0000-4000-8000-000000000042",
            journal_digest=_digest("apply-journal"),
            phase="verifying",
            action="probe",
        )
    with pytest.raises(consolidation_authority.ConsolidationAuthorityUnavailable):
        consolidation_authority.issue_authority(
            vault_binding_digest=_digest("vault-binding"),
            run_id="00000000-0000-4000-8000-000000000001",
            operation_id="00000000-0000-4000-8000-000000000042",
            journal_digest=_digest("apply-journal"),
            phase="transport-verifying",
            action="read",
        )


def test_forged_constructor_seal_never_authorizes() -> None:
    from exomem.governance import consolidation_authority

    forged = consolidation_authority.ConsolidationAuthority(
        _digest("vault-binding"),
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000042",
        _digest("apply-journal"),
        "transport-verifying",
        "probe",
        object(),
    )
    with pytest.raises(consolidation_authority.ConsolidationAuthorityUnavailable):
        consolidation_authority.require_authority(
            forged,
            vault_binding_digest=_digest("vault-binding"),
            run_id="00000000-0000-4000-8000-000000000001",
            operation_id="00000000-0000-4000-8000-000000000042",
            journal_digest=_digest("apply-journal"),
            phase="transport-verifying",
            action="probe",
        )
