from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event, Thread

import pytest

from exomem import vocabulary_authority
from exomem.governance.authorization_session_lifecycle import AuthorizationSessionContext
from exomem.governance.principal import RequestPrincipal
from exomem.vocabulary_effects import Effect

NOW = 1_700_000_000
_TEST_VOCABULARY_FLOORS: dict[Path, int] = {}


@dataclass(frozen=True)
class _Keyring:
    keyring_id: str = "keyring-1"


@dataclass(frozen=True)
class _Control:
    cell_id: str = "cell-1"
    logical_vault_id: str = "vault-1"
    activation_epoch: int = 7
    vocabulary_authority_floor: int = 1


@dataclass(frozen=True)
class _Custody:
    keyring: _Keyring = _Keyring()
    control: _Control = _Control()
    control_path: Path = Path("/tmp/exomem-authority-test-control.json")


def _principal(audience: str = "agent-a") -> RequestPrincipal:
    context = AuthorizationSessionContext(
        session_id=f"session-{audience}",
        principal_id=audience,
        issuer_family="test-issuer",
        cell_id="cell-1",
        logical_vault_id="vault-1",
        keyring_id="keyring-1",
        credential_generation=7,
        expires_at=NOW + 600,
    )
    return RequestPrincipal(
        audience_id=audience,
        surface="test",
        authorization_session_id=context.session_id,
        issuer_family=context.issuer_family,
        verified_authorization_session=context,
    )


def _operation(
    *,
    operation_id: str = "operation-1",
    effects: tuple[Effect, ...] = (Effect("entity.create", "Knowledge Base/Entities/Organizations/a.md", "memory:entity-a"),),
) -> vocabulary_authority.CanonicalOperation:
    return vocabulary_authority.CanonicalOperation.from_effects(
        operation_id=operation_id,
        command_digest="a" * 64,
        effects=effects,
        image_digest="b" * 64,
        registry_digests={"entity_types": "c" * 64},
        target_digests={"memory:entity-a": "d" * 64},
    )


def _owner_decision() -> vocabulary_authority.TrustedOwnerDecision:
    return vocabulary_authority._trusted_owner_decision_for_adapter(  # noqa: SLF001
        owner_id="owner-1", ceremony_id="ceremony-1"
    )


def _bound_owner_decision(
    action: str,
    principal: RequestPrincipal,
    *,
    request_id: str | None = None,
    operation: vocabulary_authority.CanonicalOperation | None = None,
    grant: dict[str, object] | None = None,
    authority_id: str | None = None,
    ceremony_id: str | None = None,
) -> vocabulary_authority.TrustedOwnerDecision:
    binding = vocabulary_authority._owner_binding(  # noqa: SLF001
        action,
        principal,
        request_id=request_id,
        operation=None if operation is None else operation.as_dict(),
        grant=grant,
        authority_id=authority_id,
    )
    return vocabulary_authority._trusted_owner_decision_for_adapter(  # noqa: SLF001
        owner_id="owner-1",
        ceremony_id=ceremony_id or f"{action}-{binding[:12]}",
        binding_digest=binding,
        expires_at=NOW + 300,
    )


def _floor() -> vocabulary_authority.DeploymentFloorProof:
    return vocabulary_authority._deployment_floor_for_adapter(  # noqa: SLF001
        runtime="vocabulary-authority/v2", generation=7
    )


def _activate(authority: vocabulary_authority.VocabularyAuthority, principal: RequestPrincipal) -> None:
    floor = getattr(authority, "_test_vocabulary_floor", None)
    if floor is not None:
        floor["value"] = 2
        _TEST_VOCABULARY_FLOORS[floor["path"]] = 2
    binding = vocabulary_authority._owner_binding(  # noqa: SLF001
        "activate",
        principal,
        operation={"runtime": "vocabulary-authority/v2", "generation": 7},
    )
    authority.activate(
        principal=principal,
        decision=vocabulary_authority._trusted_owner_decision_for_adapter(  # noqa: SLF001
            owner_id="owner-1",
            ceremony_id="activation-ceremony",
            binding_digest=binding,
            expires_at=NOW + 300,
        ),
        deployment_floor=_floor(),
        binding=binding,
    )


def _grant(
    authority: vocabulary_authority.VocabularyAuthority,
    principal: RequestPrincipal,
    *,
    actions: tuple[str, ...],
    scope: vocabulary_authority.AuthorityScope,
    expires_at: int,
) -> str:
    manifest = {"actions": list(tuple(sorted(set(actions)))), "scope": scope.as_dict(), "expires_at": expires_at}
    return authority.grant(
        principal=principal,
        decision=_bound_owner_decision("grant", principal, grant=manifest),
        audience=principal,
        actions=actions,
        scope=scope,
        expires_at=expires_at,
        binding=vocabulary_authority._owner_binding("grant", principal, grant=manifest),  # noqa: SLF001
    )


def _approve_exact(
    authority: vocabulary_authority.VocabularyAuthority,
    principal: RequestPrincipal,
    operation: vocabulary_authority.CanonicalOperation,
    *,
    expires_at: int,
) -> str:
    return authority.approve_exact(
        principal=principal,
        decision=_bound_owner_decision("approve-exact", principal, operation=operation),
        audience=principal,
        operation=operation,
        expires_at=expires_at,
        binding=vocabulary_authority._owner_binding(  # noqa: SLF001
            "approve-exact", principal, operation=operation.as_dict()
        ),
    )


def _approve_request(
    authority: vocabulary_authority.VocabularyAuthority,
    principal: RequestPrincipal,
    request_id: str,
) -> str:
    operation = authority.inspect_request_for_owner(request_id, principal=principal)
    binding = vocabulary_authority._owner_binding(  # noqa: SLF001
        "approve-request", principal, request_id=request_id, operation=operation.as_dict()
    )
    return authority.approve_request(
        request_id,
        principal=principal,
        decision=_bound_owner_decision(
            "approve-request", principal, request_id=request_id, operation=operation
        ),
        binding=binding,
    )


def _revoke(
    authority: vocabulary_authority.VocabularyAuthority,
    principal: RequestPrincipal,
    authority_id: str,
) -> None:
    binding = vocabulary_authority._owner_binding(  # noqa: SLF001
        "revoke", principal, authority_id=authority_id
    )
    authority.revoke(
        principal=principal,
        decision=_bound_owner_decision("revoke", principal, authority_id=authority_id),
        authority_id=authority_id,
        binding=binding,
    )


def _store(root: Path) -> vocabulary_authority.VocabularyAuthority:
    control_path = root.parent / "custody" / "control.json"
    control_path.parent.mkdir(parents=True, exist_ok=True)
    control_path.parent.chmod(0o700)
    control_path.write_text("control")
    floor = {
        "value": _TEST_VOCABULARY_FLOORS.setdefault(control_path, 1),
        "path": control_path,
    }
    authority = vocabulary_authority.VocabularyAuthority(
        root,
        custody_loader=lambda _root, *, now: _Custody(
            control=replace(_Control(), vocabulary_authority_floor=_TEST_VOCABULARY_FLOORS[control_path]),
            control_path=control_path,
        ),
        clock=lambda: NOW,
        session_status_verifier=lambda _connection, *, custody, context, now: context,
    )
    authority._test_vocabulary_floor = floor  # type: ignore[attr-defined]  # noqa: SLF001
    return authority


def test_absent_authority_store_preserves_v1_without_creating_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))

    status = _store(tmp_path / "vault").status(_principal())

    assert status.mode == "v1"
    assert not list((tmp_path / "custody").glob("*.vocabulary-authority.*"))


def test_transition_helpers_derive_custody_artifacts_and_report_authenticated_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    root = tmp_path / "vault"
    authority = _store(root)
    control_path = root.parent / "custody" / "control.json"

    marker, database = vocabulary_authority.authority_artifact_paths(control_path, "vault-1")

    assert marker == authority._marker_path(_Custody(control_path=control_path))  # noqa: SLF001
    assert database == authority._database_path(_Custody(control_path=control_path))  # noqa: SLF001
    assert authority.transition_status() == "v1"
    _activate(authority, _principal())
    assert authority.transition_status() == "v2"

    monkeypatch.delenv("EXOMEM_AUTH_SESSION_KEYRING_FILE", raising=False)
    monkeypatch.delenv("EXOMEM_AUTH_SESSION_CONTROL_FILE", raising=False)
    assert vocabulary_authority.transition_status(root, now=NOW) == "v1"


def test_default_runtime_without_custody_configuration_preserves_local_v1(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("EXOMEM_AUTH_SESSION_KEYRING_FILE", raising=False)
    monkeypatch.delenv("EXOMEM_AUTH_SESSION_CONTROL_FILE", raising=False)
    monkeypatch.delenv("EXOMEM_AUTH_SESSION_MEMBERSHIP_FILE", raising=False)

    status = vocabulary_authority.VocabularyAuthority(tmp_path / "vault").runtime_status()

    assert status == vocabulary_authority.AuthorityStatus("v1", None, 0)
    assert not (tmp_path / "vault").exists()


def test_configured_but_missing_custody_files_are_unavailable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_AUTH_SESSION_KEYRING_FILE", str(tmp_path / "missing-keyring.json"))
    monkeypatch.setenv("EXOMEM_AUTH_SESSION_CONTROL_FILE", str(tmp_path / "missing-control.json"))

    status = vocabulary_authority.VocabularyAuthority(tmp_path / "vault").runtime_status()

    assert status == vocabulary_authority.AuthorityStatus("unavailable", None, 0)


def test_configured_but_corrupt_custody_files_are_unavailable(tmp_path: Path, monkeypatch) -> None:
    keyring = tmp_path / "keyring.json"
    control = tmp_path / "control.json"
    keyring.write_text("{}")
    control.write_text("{}")
    keyring.chmod(0o600)
    control.chmod(0o600)
    monkeypatch.setenv("EXOMEM_AUTH_SESSION_KEYRING_FILE", str(keyring))
    monkeypatch.setenv("EXOMEM_AUTH_SESSION_CONTROL_FILE", str(control))

    status = vocabulary_authority.VocabularyAuthority(tmp_path / "vault").runtime_status()

    assert status == vocabulary_authority.AuthorityStatus("unavailable", None, 0)


def test_registry_only_operation_allows_an_empty_target_digest_set() -> None:
    operation = vocabulary_authority.CanonicalOperation.from_effects(
        operation_id="registry-only",
        command_digest="a" * 64,
        effects=(
            Effect(
                "entity_type.add",
                "Knowledge Base/_Schema/entity-types.yaml",
                "guild",
            ),
        ),
        image_digest="b" * 64,
        registry_digests={"entity_types": "c" * 64},
        target_digests={},
    )

    assert operation.target_digests == ()


def test_activation_has_no_default_grants_and_is_bound_to_current_custody(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    authority = _store(tmp_path / "vault")
    authority._test_vocabulary_floor["value"] = 2  # type: ignore[attr-defined]  # noqa: SLF001
    _TEST_VOCABULARY_FLOORS[authority._test_vocabulary_floor["path"]] = 2  # type: ignore[attr-defined]  # noqa: SLF001
    agent = _principal()

    activated = authority.activate(
        principal=agent,
        decision=vocabulary_authority._trusted_owner_decision_for_adapter(  # noqa: SLF001
            owner_id="owner-1",
            ceremony_id="activation-status",
            binding_digest=vocabulary_authority._owner_binding(  # noqa: SLF001
                "activate", agent, operation={"runtime": "vocabulary-authority/v2", "generation": 7}
            ),
            expires_at=NOW + 300,
        ),
        deployment_floor=_floor(),
        binding=vocabulary_authority._owner_binding(  # noqa: SLF001
            "activate", agent, operation={"runtime": "vocabulary-authority/v2", "generation": 7}
        ),
    )

    assert activated.mode == "v2"
    assert activated.generation == 7
    assert activated.active_grants == 0
    with pytest.raises(vocabulary_authority.VocabularyAuthorityDenied):
        authority.reserve(_operation(), principal=agent)


def test_vault_wide_entity_grant_cannot_authorize_edge_or_narrow_new_entity(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    authority = _store(tmp_path / "vault")
    agent = _principal()
    _activate(authority, agent)
    _grant(
        authority,
        agent,
        actions=("entity.create",),
        scope=vocabulary_authority.AuthorityScope.vault_wide(),
        expires_at=NOW + 100,
    )

    reservation = authority.reserve(_operation(), principal=agent)
    assert reservation.authority_ids
    with pytest.raises(vocabulary_authority.VocabularyAuthorityDenied):
        authority.reserve(
            _operation(
                operation_id="edge",
                effects=(
                    Effect(
                        "edge.add",
                        "Knowledge Base/Notes/a.md",
                        "edge-a",
                        {"source": "memory:a", "target": "memory:b", "relation": "relates_to"},
                    ),
                ),
            ),
            principal=agent,
        )


def test_mixed_effect_set_refuses_atomically_when_one_effect_lacks_authority(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    authority = _store(tmp_path / "vault")
    agent = _principal()
    _activate(authority, agent)
    _grant(
        authority,
        agent,
        actions=("entity.create",),
        scope=vocabulary_authority.AuthorityScope.vault_wide(),
        expires_at=NOW + 100,
    )
    mixed = _operation(
        operation_id="mixed",
        effects=(
            Effect("entity.create", "Knowledge Base/Entities/Organizations/a.md", "memory:entity-a"),
            Effect(
                "edge.add",
                "Knowledge Base/Notes/a.md",
                "edge-a",
                {"source": "memory:a", "target": "memory:b", "relation": "relates_to"},
            ),
        ),
    )

    with pytest.raises(vocabulary_authority.VocabularyAuthorityDenied):
        authority.reserve(mixed, principal=agent)
    assert authority.status(agent).active_grants == 1


def test_project_edge_grant_requires_current_endpoint_proof_but_not_its_old_digest(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    authority = _store(tmp_path / "vault")
    agent = _principal()
    _activate(authority, agent)
    scope = vocabulary_authority.AuthorityScope.project_edge(
        project_ref="memory:project-a",
        source_ref="memory:source",
        target_ref="memory:target",
        membership_digest="a" * 64,
    )
    _grant(
        authority,
        agent,
        actions=("edge.add",),
        scope=scope,
        expires_at=NOW + 100,
    )

    def edge_operation(target: str, membership_digest: str):
        return vocabulary_authority.CanonicalOperation.from_effects(
            operation_id=f"edge-{target}",
            command_digest="e" * 64,
            effects=(
                Effect(
                    "edge.add",
                    "Knowledge Base/Notes/source.md",
                    "edge-a",
                    {"source": "memory:source", "target": target, "relation": "supplies"},
                ),
            ),
            image_digest="f" * 64,
            registry_digests={"relation_types": "d" * 64},
            target_digests={"memory:source": "1" * 64, target: "2" * 64},
            scope_proofs=(
                vocabulary_authority.AuthorityScope.project_edge(
                    project_ref="memory:project-a",
                    source_ref="memory:source",
                    target_ref=target,
                    membership_digest=membership_digest,
                ),
            ),
        )

    assert authority.reserve(edge_operation("memory:target", "b" * 64), principal=agent).authority_ids
    with pytest.raises(vocabulary_authority.VocabularyAuthorityDenied):
        authority.reserve(edge_operation("memory:other", "c" * 64), principal=agent)


def test_exact_approval_is_reserved_to_one_identity_and_survives_restart(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    root = tmp_path / "vault"
    authority = _store(root)
    agent = _principal()
    operation = _operation()
    _activate(authority, agent)
    _approve_exact(authority, agent, operation, expires_at=NOW + 100)

    first = authority.reserve(operation, principal=agent)
    restarted = _store(root)
    replay = restarted.reserve(operation, principal=agent)
    assert replay.reservation_id == first.reservation_id
    with pytest.raises(vocabulary_authority.VocabularyAuthorityDenied):
        restarted.reserve(_operation(operation_id="different-operation"), principal=agent)


def test_exact_approval_spends_with_the_receipt_and_never_reexecutes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    authority = _store(tmp_path / "vault")
    agent = _principal()
    operation = _operation()
    _activate(authority, agent)
    _approve_exact(authority, agent, operation, expires_at=NOW + 100)
    reservation = authority.reserve(operation, principal=agent)

    with authority.commit_guard(reservation, principal=agent) as guard:
        guard.mark_committed(
            vocabulary_authority._canonical_receipt_for_writer(  # noqa: SLF001
                operation=operation, receipt_id="receipt-1"
            )
        )

    replay = authority.reserve(operation, principal=agent)
    assert replay.reservation_id == reservation.reservation_id
    assert replay.state == "committed"
    with pytest.raises(vocabulary_authority.VocabularyAuthorityDenied):
        authority.reserve(_operation(operation_id="new-operation"), principal=agent)


def test_pending_request_binds_the_stored_full_operation_before_owner_approval(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    authority = _store(tmp_path / "vault")
    agent = _principal()
    operation = _operation()
    _activate(authority, agent)

    request = authority.request(operation, principal=agent, expires_at=NOW + 100)
    repeated = authority.request(operation, principal=agent, expires_at=NOW + 100)
    assert repeated.request_id == request.request_id
    assert request.display_effects == (
        {
            "action": "entity.create",
            "path": "Knowledge Base/Entities/Organizations/a.md",
            "key": "memory:entity-a",
        },
    )
    with pytest.raises(vocabulary_authority.VocabularyAuthorityDenied):
        authority.request_status(request.request_id, principal=_principal("other"))

    authority_id = _approve_request(authority, agent, request.request_id)
    assert authority_id.startswith("vocab-auth-")
    assert authority.request_status(request.request_id, principal=agent).state == "approved"
    assert authority.reserve(operation, principal=agent).authority_ids == (authority_id,)


def test_owner_decision_cannot_approve_a_different_pending_operation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    authority = _store(tmp_path / "vault")
    agent = _principal()
    _activate(authority, agent)
    first = authority.request(_operation(), principal=agent, expires_at=NOW + 100)
    second = authority.request(
        _operation(operation_id="operation-2"), principal=agent, expires_at=NOW + 100
    )
    decision = _bound_owner_decision(
        "approve-request",
        agent,
        request_id=first.request_id,
        operation=authority.inspect_request_for_owner(first.request_id, principal=agent),
    )

    binding = vocabulary_authority._owner_binding(  # noqa: SLF001
        "approve-request",
        agent,
        request_id=first.request_id,
        operation=authority.inspect_request_for_owner(first.request_id, principal=agent).as_dict(),
    )
    authority.approve_request(
        first.request_id, principal=agent, decision=decision, binding=binding
    )

    with pytest.raises(vocabulary_authority.VocabularyAuthorityDenied):
        authority.approve_request(
            second.request_id, principal=agent, decision=decision, binding=binding
        )


def test_runtime_status_requires_the_current_supported_floor_without_a_session(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    authority = _store(tmp_path / "vault")
    assert authority.runtime_status().mode == "v1"
    _activate(authority, _principal())

    assert authority.runtime_status() == vocabulary_authority.AuthorityStatus("v2", 7, 0)


def test_session_generation_alone_does_not_replace_session_status_revalidation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    authority = _store(tmp_path / "vault")
    agent = _principal()
    _activate(authority, agent)
    stale_context = AuthorizationSessionContext(
        session_id="session-agent-a",
        principal_id="agent-a",
        issuer_family="test-issuer",
        cell_id="cell-1",
        logical_vault_id="vault-1",
        keyring_id="keyring-1",
        credential_generation=6,
        expires_at=NOW + 600,
    )
    stale = RequestPrincipal(
        audience_id="agent-a",
        surface="test",
        authorization_session_id=stale_context.session_id,
        issuer_family=stale_context.issuer_family,
        verified_authorization_session=stale_context,
    )

    assert authority.status(stale).mode == "v2"


def test_reconcile_accepts_only_writer_sealed_evidence_after_an_uncertain_outcome(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    authority = _store(tmp_path / "vault")
    agent = _principal()
    operation = _operation()
    _activate(authority, agent)
    _approve_exact(authority, agent, operation, expires_at=NOW + 100)
    reservation = authority.reserve(operation, principal=agent)
    forged = vocabulary_authority.CanonicalReceiptEvidence(
        operation.operation_id,
        operation.command_digest,
        operation.effect_digest,
        "receipt-forged",
        object(),
    )

    with pytest.raises(vocabulary_authority.VocabularyAuthorityDenied):
        authority.reconcile_committed(reservation, forged)
    reconciled = authority.reconcile_committed(
        reservation,
        vocabulary_authority._canonical_receipt_for_writer(  # noqa: SLF001
            operation=operation, receipt_id="receipt-recovered"
        ),
    )
    assert reconciled.state == "committed"


def test_expired_authority_refuses_a_reserved_remaining_step(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    authority = _store(tmp_path / "vault")
    agent = _principal()
    _activate(authority, agent)
    _grant(
        authority,
        agent,
        actions=("entity.create",),
        scope=vocabulary_authority.AuthorityScope.vault_wide(),
        expires_at=NOW + 1,
    )
    reservation = authority.reserve(_operation(), principal=agent)

    with pytest.raises(vocabulary_authority.VocabularyAuthorityDenied):
        with authority.commit_guard(reservation, principal=agent, now=NOW + 2):
            pass


def test_grant_rechecks_owner_and_session_expiry_after_waiting_for_sqlite(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    root = tmp_path / "vault"
    control_path = root.parent / "custody" / "control.json"
    control_path.parent.mkdir(parents=True)
    control_path.parent.chmod(0o700)
    control_path.write_text("control")
    moment = [NOW]
    authority = vocabulary_authority.VocabularyAuthority(
        root,
        custody_loader=lambda _root, *, now: _Custody(
            control=replace(_Control(), vocabulary_authority_floor=2),
            control_path=control_path,
        ),
        clock=lambda: moment[0],
        session_status_verifier=lambda _connection, *, custody, context, now: context,
    )
    agent = _principal()
    _activate(authority, agent)
    custody = _Custody(
        control=replace(_Control(), vocabulary_authority_floor=2),
        control_path=control_path,
    )
    lock = authority._connect(custody, create=False)  # noqa: SLF001
    assert lock is not None
    lock.execute("BEGIN IMMEDIATE")
    manifest = {
        "actions": ["entity.create"],
        "scope": vocabulary_authority.AuthorityScope.vault_wide().as_dict(),
        "expires_at": NOW + 100,
    }
    binding = vocabulary_authority._owner_binding("grant", agent, grant=manifest)  # noqa: SLF001
    decision = vocabulary_authority._trusted_owner_decision_for_adapter(  # noqa: SLF001
        owner_id="owner-1", ceremony_id="expiring-grant", binding_digest=binding, expires_at=NOW + 1
    )
    finished, started = Event(), Event()
    errors: list[BaseException] = []

    def grant() -> None:
        started.set()
        try:
            authority.grant(
                principal=agent,
                decision=decision,
                audience=agent,
                actions=("entity.create",),
                scope=vocabulary_authority.AuthorityScope.vault_wide(),
                expires_at=NOW + 100,
                binding=binding,
            )
        except Exception as error:  # noqa: BLE001 - asserted after the worker joins
            errors.append(error)
        finally:
            finished.set()

    worker = Thread(target=grant)
    worker.start()
    assert started.wait(1)
    assert not finished.wait(0.05)
    moment[0] = NOW + 2
    lock.commit()
    worker.join(timeout=2)

    assert finished.is_set()
    assert len(errors) == 1
    assert isinstance(errors[0], vocabulary_authority.VocabularyAuthorityDenied)


def test_unsealed_caller_supplied_owner_shape_cannot_activate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    authority = _store(tmp_path / "vault")
    forged = vocabulary_authority.TrustedOwnerDecision("owner-1", "ceremony-1", object())

    with pytest.raises(vocabulary_authority.VocabularyAuthorityDenied):
        authority.activate(
            principal=_principal(),
            decision=forged,
            deployment_floor=_floor(),
            binding=vocabulary_authority._owner_binding(  # noqa: SLF001
                "activate",
                _principal(),
                operation={"runtime": "vocabulary-authority/v2", "generation": 7},
            ),
        )


def test_revoke_before_guard_refuses_but_committed_receipt_is_retained(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    authority = _store(tmp_path / "vault")
    agent = _principal()
    _activate(authority, agent)
    grant_id = _grant(
        authority,
        agent,
        actions=("entity.create",),
        scope=vocabulary_authority.AuthorityScope.vault_wide(),
        expires_at=NOW + 100,
    )
    reservation = authority.reserve(_operation(), principal=agent)
    _revoke(authority, agent, grant_id)
    with pytest.raises(vocabulary_authority.VocabularyAuthorityDenied):
        with authority.commit_guard(reservation, principal=agent):
            pass


def test_authority_use_manifest_binds_each_effect_to_the_reserved_authority(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    authority = _store(tmp_path / "vault")
    agent = _principal()
    _activate(authority, agent)
    grant_id = _grant(
        authority,
        agent,
        actions=("entity.create",),
        scope=vocabulary_authority.AuthorityScope.vault_wide(),
        expires_at=NOW + 100,
    )
    reservation = authority.reserve(_operation(), principal=agent)

    assert authority.authority_use_manifest(reservation) == (
        {
            "effect_index": 0,
            "authority_id": grant_id,
            "action": "entity.create",
            "generation": 7,
        },
    )


def test_commit_guard_serializes_a_concurrent_revoke_after_a_committed_receipt(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    authority = _store(tmp_path / "vault")
    agent = _principal()
    _activate(authority, agent)
    grant_id = _grant(
        authority,
        agent,
        actions=("entity.create",),
        scope=vocabulary_authority.AuthorityScope.vault_wide(),
        expires_at=NOW + 100,
    )
    reservation = authority.reserve(_operation(), principal=agent)
    started, finished = Event(), Event()
    errors: list[BaseException] = []

    def revoke() -> None:
        started.set()
        try:
            _revoke(_store(tmp_path / "vault"), agent, grant_id)
        except Exception as error:  # noqa: BLE001 - asserted after the worker joins
            errors.append(error)
        finally:
            finished.set()

    with authority.commit_guard(reservation, principal=agent) as guard:
        worker = Thread(target=revoke)
        worker.start()
        assert started.wait(1)
        assert not finished.wait(0.05)
        guard.mark_committed(
            vocabulary_authority._canonical_receipt_for_writer(  # noqa: SLF001
                operation=_operation(), receipt_id="receipt-race"
            )
        )
    worker.join(timeout=2)

    assert finished.is_set()
    assert errors == []
    assert authority.reserve(_operation(), principal=agent).state == "committed"


def test_corrupt_existing_store_fails_closed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    root = tmp_path / "vault"
    authority = _store(root)
    _activate(authority, _principal())
    path = authority._database_path(_Custody(control_path=root.parent / "custody" / "control.json"))  # noqa: SLF001
    path.write_bytes(b"not sqlite")

    with pytest.raises(vocabulary_authority.VocabularyAuthorityUnavailable):
        authority.status(_principal())


def test_marker_without_database_refuses_instead_of_downgrading_to_v1(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    root = tmp_path / "vault"
    authority = _store(root)
    _activate(authority, _principal())
    path = authority._database_path(_Custody(control_path=root.parent / "custody" / "control.json"))  # noqa: SLF001
    path.unlink()

    assert authority.runtime_status().mode == "unavailable"
    with pytest.raises(vocabulary_authority.VocabularyAuthorityUnavailable):
        authority.status(_principal())


def test_published_v2_floor_refuses_erased_authority_artifacts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    root = tmp_path / "vault"
    authority = _store(root)
    _activate(authority, _principal())
    custody = _Custody(
        control=replace(_Control(), vocabulary_authority_floor=2),
        control_path=root.parent / "custody" / "control.json",
    )
    marker = authority._marker_path(custody)  # noqa: SLF001
    database = authority._database_path(custody)  # noqa: SLF001
    marker.unlink()
    database.unlink()
    for suffix in ("-journal", "-wal", "-shm"):
        database.with_name(f"{database.name}{suffix}").unlink(missing_ok=True)
    marker.parent.chmod(0o775)

    assert authority.runtime_status().mode == "unavailable"
    with pytest.raises(vocabulary_authority.VocabularyAuthorityUnavailable):
        authority.status(_principal())


def test_marker_and_initialized_database_without_activation_refuse_instead_of_v1(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    root = tmp_path / "vault"
    authority = _store(root)
    custody = _Custody(
        control=replace(_Control(), vocabulary_authority_floor=2),
        control_path=root.parent / "custody" / "control.json",
    )
    marker = authority._create_marker(  # noqa: SLF001
        custody, floor=_floor(), owner=_owner_decision(), now=NOW
    )
    connection = authority._connect(custody, create=True, marker=marker)  # noqa: SLF001
    assert connection is not None
    connection.close()

    assert authority.runtime_status().mode == "unavailable"


def test_activation_retry_finishes_the_same_marker_after_a_pre_database_crash(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    root = tmp_path / "vault"
    authority = _store(root)
    custody = _Custody(control_path=root.parent / "custody" / "control.json")
    marker = authority._create_marker(  # noqa: SLF001
        custody, floor=_floor(), owner=_owner_decision(), now=NOW
    )

    assert authority.runtime_status().mode == "unavailable"
    _activate(authority, _principal())
    assert authority._marker(custody)["store_id"] == marker["store_id"]  # noqa: SLF001
    assert authority.runtime_status().mode == "v2"


def test_malformed_marker_refuses_without_falling_back_to_v1(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    root = tmp_path / "vault"
    authority = _store(root)
    _activate(authority, _principal())
    marker = authority._marker_path(_Custody(control_path=root.parent / "custody" / "control.json"))  # noqa: SLF001
    marker.write_text("{}")

    assert authority.runtime_status().mode == "unavailable"


def test_marker_binds_the_store_to_authenticated_custody_not_the_vault_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    root = tmp_path / "vault"
    authority = _store(root)
    _activate(authority, _principal())
    control_path = root.parent / "custody" / "control.json"
    moved = vocabulary_authority.VocabularyAuthority(
        tmp_path / "moved-vault",
        custody_loader=lambda _root, *, now: _Custody(
            control=replace(_Control(), vocabulary_authority_floor=2),
            control_path=control_path,
        ),
        clock=lambda: NOW,
    )

    assert moved.runtime_status().mode == "v2"


def test_changed_custody_keyring_refuses_the_marker_bound_store(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    root = tmp_path / "vault"
    authority = _store(root)
    _activate(authority, _principal())
    control_path = root.parent / "custody" / "control.json"
    changed = vocabulary_authority.VocabularyAuthority(
        root,
        custody_loader=lambda _root, *, now: _Custody(
            keyring=_Keyring("keyring-2"), control_path=control_path
        ),
        clock=lambda: NOW,
    )

    assert changed.runtime_status().mode == "unavailable"


def test_unsafe_custody_parent_refuses_authority_state_before_opening_sqlite(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    root = tmp_path / "vault"
    authority = _store(root)
    control_parent = root.parent / "custody"
    _activate(authority, _principal())
    control_parent.chmod(0o777)

    assert authority.runtime_status().mode == "unavailable"


def test_absent_authority_artifacts_preserve_v1_with_v4_custody_parent(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    root = tmp_path / "vault"
    control_path = root.parent / "custody" / "control.json"
    control_path.parent.mkdir(parents=True)
    control_path.parent.chmod(0o775)
    control_path.write_text("control")
    control_path.chmod(0o600)
    authority = vocabulary_authority.VocabularyAuthority(
        root,
        custody_loader=lambda _root, *, now: _Custody(control_path=control_path),
        clock=lambda: NOW,
    )

    assert authority.runtime_status().mode == "v1"

    marker = authority._marker_path(_Custody(control_path=control_path))  # noqa: SLF001
    marker.write_text("present")
    marker.chmod(0o600)

    assert authority.runtime_status().mode == "unavailable"


def test_untrusted_sqlite_sidecar_refuses_before_opening_authority_database(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    root = tmp_path / "vault"
    authority = _store(root)
    _activate(authority, _principal())
    custody = _Custody(control_path=root.parent / "custody" / "control.json")
    database = authority._database_path(custody)  # noqa: SLF001
    sidecar = database.with_name(f"{database.name}-wal")
    sidecar.write_text("untrusted")
    sidecar.chmod(0o644)

    assert authority.runtime_status().mode == "unavailable"
