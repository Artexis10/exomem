"""Owner admission precedes every consolidation request field and effect."""

from __future__ import annotations

import copy
import importlib
import importlib.util
import pickle
from dataclasses import replace
from pathlib import Path

import pytest

from exomem.governance import authorization_session_lifecycle, consolidation_identity
from exomem.governance.principal import RequestPrincipal, owner_principal

NOW = 1_800_000_000
ACTIONS = (
    "start",
    "status",
    "reconcile",
    "plan",
    "approve",
    "apply",
    "verify",
    "recover",
    "abort",
    "rollback",
    "retire-source",
)


def _module():
    assert importlib.util.find_spec("exomem.governance.consolidation_owner"), (
        "consolidation needs explicit owner admission, not a library owner default"
    )
    return importlib.import_module("exomem.governance.consolidation_owner")


def _principal(surface="mcp"):
    who = owner_principal(surface=surface)
    session = authorization_session_lifecycle.AuthorizationSessionContext(
        session_id="authorization-session:0123456789abcdef0123456789abcdef",
        principal_id=who.audience_id,
        issuer_family=who.issuer_family,
        cell_id="cell-a",
        logical_vault_id="vault-a",
        keyring_id="keyring-a",
        credential_generation=1,
        expires_at=NOW + 600,
    )
    return who.with_verified_authorization_session(
        session,
        issuer_family=who.issuer_family,
    )


def _identity():
    return consolidation_identity.ConsolidationCellIdentity(
        schema=consolidation_identity.IDENTITY_SCHEMA,
        cell_id="cell-a",
        vault_id="vault-a",
        installation_id="installation-v1-" + "a" * 64,
        installation_generation=1,
        active_fence_digest="b" * 64,
        root_binding_id="attachment-v1-" + "c" * 64,
        root_binding_digest="d" * 64,
        machine_key_id="host-key-v1-" + "e" * 64,
        adoption_census_digest="f" * 64,
        clone_of_vault_id=None,
        clone_of_installation_id=None,
        clone_of_snapshot_digest=None,
        created_at=NOW - 60,
        authentication_algorithm="HMAC-SHA256",
        record_digest="1" * 64,
        identity_path=Path("private-identity.json"),
    )


class _Secret:
    def __repr__(self):
        pytest.fail("private request value was rendered before owner admission")

    def __str__(self):
        pytest.fail("private request value was coerced before owner admission")

    def __hash__(self):
        pytest.fail("private request value was hashed before owner admission")


@pytest.fixture
def verified_session_stub(monkeypatch):
    """Only facts/adapter unit tests stub durable verification; real tests don't."""
    module = _module()
    monkeypatch.setattr(
        module,
        "_durable_session",
        lambda root, *, principal, now: module._session(principal, now=now),
    )


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize(
    "who",
    [
        None,
        owner_principal(),
        _principal("library"),
        RequestPrincipal("ordinary", surface="mcp", issuer_family="mcp-local-stdio"),
        replace(_principal(), resolved=False),
    ],
)
def test_nonowner_and_unbound_calls_refuse_before_identity_or_body(
    tmp_path,
    monkeypatch,
    action,
    who,
):
    module = _module()
    monkeypatch.setattr(
        consolidation_identity,
        "load_local_identity",
        lambda *a, **k: pytest.fail("unauthorized identity lookup"),
    )
    before = tuple(tmp_path.rglob("*"))
    with pytest.raises(module.ConsolidationOwnerUnavailable) as error:
        module.admit_local_owner(
            tmp_path,
            principal=who,
            arguments={
                "schema": "exomem.consolidate-memory-request/v1",
                "action": action,
                "source_artifact_ref": _Secret(),
            },
            now=NOW,
        )
    assert error.value.as_public_dict() == {
        "code": "CONSOLIDATION_OWNER_UNAVAILABLE",
        "message": "consolidation owner is unavailable",
        "remediation": None,
    }
    assert tuple(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("surface", ["cli", "mcp", "rest"])
def test_explicit_session_owner_is_bound_to_exact_action_and_installation(
    tmp_path,
    monkeypatch,
    action,
    surface,
    verified_session_stub,
):
    module = _module()
    who = _principal(surface)
    identity = _identity()
    monkeypatch.setattr(consolidation_identity, "load_local_identity", lambda *a, **k: identity)
    context = module.admit_local_owner(
        tmp_path,
        principal=who,
        arguments={
            "schema": "exomem.consolidate-memory-request/v1",
            "action": action,
            "source_artifact_ref": _Secret(),
        },
        now=NOW,
    )
    facts = module.require_owner_context(
        context,
        principal=who,
        identity=identity,
        action=action,
        now=NOW,
    )
    assert facts.schema == "ConsolidationOwnerContext/v1"
    assert facts.purpose == "vault-consolidation"
    assert facts.vault_id == identity.vault_id
    assert facts.installation_generation == identity.installation_generation
    assert facts.active_fence_digest == identity.active_fence_digest
    assert facts.authorization_session_id == who.authorization_session_id
    assert facts.action == action
    assert facts.surface == surface
    assert NOW < facts.expires_at <= who.verified_authorization_session.expires_at
    assert facts.nonce and facts.verifier_fingerprint
    assert facts.human_confirmation is False
    assert "vault-a" not in repr(context)
    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            operation(context)


@pytest.mark.parametrize(
    "change",
    [
        "action",
        "vault",
        "installation",
        "generation",
        "fence",
        "session",
        "issuer",
        "surface",
        "expired",
        "future",
        "forged",
    ],
)
def test_owner_capability_cannot_cross_or_outlive_its_binding(
    tmp_path,
    monkeypatch,
    change,
    verified_session_stub,
):
    module = _module()
    who = _principal()
    identity = _identity()
    monkeypatch.setattr(consolidation_identity, "load_local_identity", lambda *a, **k: identity)
    context = module.admit_local_owner(
        tmp_path,
        principal=who,
        arguments={
            "schema": "exomem.consolidate-memory-request/v1",
            "action": "status",
            "run_id": "123e4567-e89b-42d3-a456-426614174000",
        },
        now=NOW,
    )
    action, now = "status", NOW
    if change == "action":
        action = "apply"
    elif change == "vault":
        identity = replace(identity, vault_id="vault-b")
    elif change == "installation":
        identity = replace(identity, installation_id="installation-v1-" + "2" * 64)
    elif change == "generation":
        identity = replace(identity, installation_generation=2)
    elif change == "fence":
        identity = replace(identity, active_fence_digest="3" * 64)
    elif change == "session":
        who = replace(who, authorization_session_id="foreign-session")
    elif change == "issuer":
        who = replace(who, issuer_family="foreign-issuer")
    elif change == "surface":
        who = replace(who, surface="rest")
    elif change == "expired":
        now += 600
    elif change == "future":
        now -= 1
    else:
        context = {"owner": True}
    with pytest.raises(module.ConsolidationOwnerUnavailable):
        module.require_owner_context(
            context,
            principal=who,
            identity=identity,
            action=action,
            now=now,
        )


@pytest.mark.parametrize("change", ["vault", "cell", "alias", "session", "expired"])
def test_cross_bound_identity_never_mints_context(
    tmp_path,
    monkeypatch,
    change,
    verified_session_stub,
):
    module = _module()
    who, identity = _principal(), _identity()
    if change == "vault":
        identity = replace(identity, vault_id="vault-b")
    elif change == "cell":
        identity = replace(identity, cell_id="cell-b")
    elif change == "alias":
        identity = replace(identity, cell_id=identity.vault_id)
    elif change == "session":
        who = replace(who, authorization_session_id="different-session")
    else:
        who = replace(
            who,
            verified_authorization_session=replace(
                who.verified_authorization_session,
                expires_at=NOW,
            ),
        )
    monkeypatch.setattr(consolidation_identity, "load_local_identity", lambda *a, **k: identity)
    with pytest.raises(module.ConsolidationOwnerUnavailable):
        module.admit_local_owner(
            tmp_path,
            principal=who,
            arguments={
                "schema": "exomem.consolidate-memory-request/v1",
                "action": "status",
                "run_id": "123e4567-e89b-42d3-a456-426614174000",
            },
            now=NOW,
        )


def test_adapter_binds_context_but_session_reverification_clears_it(
    tmp_path,
    monkeypatch,
    verified_session_stub,
):
    module = _module()
    who, identity = _principal(), _identity()
    monkeypatch.setattr(consolidation_identity, "load_local_identity", lambda *a, **k: identity)
    bind = getattr(module, "bind_local_owner", None)
    assert callable(bind), "the trusted adapter must carry owner authority to dispatch"
    bound = bind(
        tmp_path,
        principal=who,
        arguments={
            "schema": "exomem.consolidate-memory-request/v1",
            "action": "status",
            "run_id": "123e4567-e89b-42d3-a456-426614174000",
        },
        now=NOW,
    )
    module.require_owner_context(
        bound.consolidation_owner_context,
        principal=bound,
        identity=identity,
        action="status",
        now=NOW,
    )
    rebound = bound.with_verified_authorization_session(
        who.verified_authorization_session,
        issuer_family=who.issuer_family,
    )
    assert rebound.consolidation_owner_context is None


def test_unbound_prepared_consolidation_refuses_before_vault_resolution(monkeypatch):
    from types import SimpleNamespace

    from exomem import product_invoke

    module = _module()
    monkeypatch.setattr(
        product_invoke,
        "resolve_vault_for",
        lambda *a, **k: pytest.fail("unbound request resolved vault"),
    )
    with pytest.raises(module.ConsolidationOwnerUnavailable):
        product_invoke.invoke_prepared(
            SimpleNamespace(name="consolidate_memory"),
            {
                "schema": "exomem.consolidate-memory-request/v1",
                "action": "status",
                "run_id": _Secret(),
            },
            principal=_principal("library"),
        )


def test_local_transport_denies_absent_session_before_presence_and_resolution(monkeypatch):
    from types import SimpleNamespace

    from exomem import product_invoke
    from exomem.governance import consolidation_enrollment

    module = _module()
    monkeypatch.setattr(
        product_invoke,
        "resolve_vault_for",
        lambda *a, **k: pytest.fail("unauthorized vault resolution"),
    )
    monkeypatch.setattr(
        consolidation_enrollment,
        "ensure_cli_runtime_presence",
        lambda *a, **k: pytest.fail("unauthorized presence registration"),
    )
    with pytest.raises(module.ConsolidationOwnerUnavailable):
        product_invoke.verify_local_authorization_transport(
            SimpleNamespace(name="consolidate_memory"),
            raw_for_vault={"run_id": _Secret()},
            surface="cli",
            vault_root=Path("does-not-exist"),
        )


def test_unknown_protected_session_denies_before_presence_and_vault_validation(monkeypatch):
    from types import SimpleNamespace

    from exomem import product_invoke
    from exomem.governance import (
        authorization_request,
        authorization_transport,
        consolidation_enrollment,
    )

    module = _module()
    monkeypatch.setattr(
        product_invoke,
        "resolve_vault_for",
        lambda *a, **k: pytest.fail("pre-auth vault validation"),
    )
    monkeypatch.setattr(
        consolidation_enrollment,
        "ensure_cli_runtime_presence",
        lambda *a, **k: pytest.fail("pre-auth presence registration"),
    )

    def deny(*a, **k):
        raise authorization_request.AuthorizationContextUnavailable

    monkeypatch.setattr(authorization_request, "verify_authorization_context", deny)
    carrier = authorization_transport.CredentialCarrier.from_value(
        "as1.AQEBAQEBAQEBAQEBAQEBAQ.AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI"
    )
    assert not carrier.is_invalid
    with pytest.raises(module.ConsolidationOwnerUnavailable):
        product_invoke.verify_local_authorization_transport(
            SimpleNamespace(name="consolidate_memory"),
            raw_for_vault={"run_id": _Secret()},
            surface="cli",
            vault_root=Path("selected-vault"),
            authorization_carrier=carrier,
        )


def test_local_route_binds_owner_before_reading_action_fields(
    tmp_path,
    monkeypatch,
    verified_session_stub,
):
    from types import SimpleNamespace

    from exomem import product_invoke
    from exomem.governance import authorization_request

    module = _module()
    who, identity = _principal("cli"), _identity()
    monkeypatch.setattr(consolidation_identity, "load_local_identity", lambda *a, **k: identity)
    monkeypatch.setattr("time.time", lambda: NOW)
    bound = product_invoke.enforce_local_authorization_route(
        SimpleNamespace(name="consolidate_memory"),
        {
            "schema": "exomem.consolidate-memory-request/v1",
            "action": "status",
            "run_id": "123e4567-e89b-42d3-a456-426614174000",
        },
        authorization_request.AuthorizationAdmission(who, True),
        vault_root=tmp_path,
    )
    module.require_owner_context(
        bound.consolidation_owner_context,
        principal=bound,
        identity=identity,
        action="status",
        now=NOW,
    )


@pytest.mark.parametrize("invalid", [False, True])
@pytest.mark.parametrize("authorized", [False, True])
def test_mcp_middleware_enforces_owner_before_next_handler(
    tmp_path,
    monkeypatch,
    authorized,
    invalid,
    verified_session_stub,
):
    import asyncio
    from types import SimpleNamespace

    import mcp.types
    from mcp.shared.exceptions import McpError

    from exomem.governance import authorization_request, authorization_transport
    from exomem.governance import principal as principal_module

    module = _module()
    who = _principal() if authorized else owner_principal(surface="mcp")
    monkeypatch.setattr(consolidation_identity, "load_local_identity", lambda *a, **k: _identity())
    monkeypatch.setattr(
        authorization_transport.principal_module, "resolve_mcp_principal", lambda: who
    )
    monkeypatch.setattr(
        authorization_transport,
        "verify_authorization_context",
        lambda *a, **k: authorization_request.AuthorizationAdmission(who, authorized),
    )
    monkeypatch.setattr("time.time", lambda: NOW)
    arguments = {
        "schema": "exomem.consolidate-memory-request/v1",
        "action": "status",
        "run_id": "123e4567-e89b-42d3-a456-426614174000",
    }
    if invalid:
        arguments["expected_run_revision"] = "0"
    message = mcp.types.CallToolRequestParams(name="consolidate_memory", arguments=arguments)

    class Context:
        def __init__(self, msg):
            self.message = msg
            self.fastmcp_context = SimpleNamespace(request_context=None)

        def copy(self, *, message):
            return Context(message)

    async def next_handler(context):
        assert authorized, "unauthorized request reached argument validation"
        assert not invalid, "invalid request reached downstream coercion"
        bound = principal_module.current_principal()
        module.require_owner_context(
            bound.consolidation_owner_context,
            principal=bound,
            identity=_identity(),
            action="status",
            now=NOW,
        )
        assert context.message.arguments == arguments
        return "bound-owner"

    call = authorization_transport.AuthorizationSessionMiddleware(tmp_path).on_call_tool(
        Context(message),
        next_handler,
    )
    if authorized and not invalid:
        assert asyncio.run(call) == "bound-owner"
    else:
        message = (
            "consolidation request is unavailable"
            if authorized
            else "consolidation owner is unavailable"
        )
        with pytest.raises(McpError, match=message):
            asyncio.run(call)


def test_prepared_context_rechecks_exact_selected_destination(
    tmp_path,
    monkeypatch,
    verified_session_stub,
):
    from types import SimpleNamespace

    from exomem import product_invoke

    module = _module()
    identity = _identity()
    monkeypatch.setattr("time.time", lambda: NOW)
    monkeypatch.setattr(consolidation_identity, "load_local_identity", lambda *a, **k: identity)
    bound = module.bind_local_owner(
        tmp_path,
        principal=_principal("cli"),
        arguments={
            "schema": "exomem.consolidate-memory-request/v1",
            "action": "status",
            "run_id": "123e4567-e89b-42d3-a456-426614174000",
        },
        now=NOW,
    )
    monkeypatch.setattr(
        consolidation_identity,
        "load_local_identity",
        lambda *a, **k: replace(identity, installation_generation=2),
    )
    monkeypatch.setattr(
        product_invoke,
        "resolve_vault_for",
        lambda *a, **k: pytest.fail("stale context reached vault validation"),
    )
    with pytest.raises(module.ConsolidationOwnerUnavailable):
        product_invoke.invoke_prepared(
            SimpleNamespace(name="consolidate_memory"),
            {
                "schema": "exomem.consolidate-memory-request/v1",
                "action": "status",
                "run_id": _Secret(),
            },
            principal=bound,
            vault_root=tmp_path,
        )


def test_shared_dispatch_rejects_unbound_owner_before_reserved_preflight(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from exomem import reserved_paths, writer_lease
    from exomem.governance import principal as principal_module

    module = _module()
    monkeypatch.setattr(
        reserved_paths,
        "reserved_preflight",
        lambda *a, **k: pytest.fail("preflight inspected untrusted request"),
    )
    with principal_module.request_scope(owner_principal(surface="mcp")):
        with pytest.raises(module.ConsolidationOwnerUnavailable):
            writer_lease.invoke_command(
                SimpleNamespace(name="consolidate_memory"),
                tmp_path,
                schema="exomem.consolidate-memory-request/v1",
                action="status",
                run_id=_Secret(),
            )


@pytest.mark.parametrize("body_kind", ["valid", "duplicate", "string-revision"])
@pytest.mark.parametrize("authorized", [False, True])
def test_rest_route_checks_owner_before_parsing_and_binds_before_coercion(
    tmp_path,
    monkeypatch,
    authorized,
    body_kind,
    verified_session_stub,
):
    import json

    from fastmcp import FastMCP
    from starlette.testclient import TestClient

    from exomem import commands, server_rest, writer_lease
    from exomem.command_surface import Command
    from exomem.governance import authorization_request, authorization_transport
    from exomem.governance import principal as principal_module
    from exomem.server_transfer import TransferConfig

    module = _module()
    who = _principal("rest") if authorized else owner_principal(surface="rest")
    cmd = Command("consolidate_memory", lambda: None, (), frozenset({"rest"}))
    monkeypatch.setattr(commands, "product_commands_for", lambda *a, **k: (cmd,))
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "test-service-key")
    monkeypatch.setattr(
        authorization_request,
        "verify_authorization_context",
        lambda *a, **k: authorization_request.AuthorizationAdmission(who, authorized),
    )
    monkeypatch.setattr(consolidation_identity, "load_local_identity", lambda *a, **k: _identity())
    monkeypatch.setattr("time.time", lambda: NOW)

    def coerce(*args, **kwargs):
        assert authorized, "unbound REST request reached body coercion"
        assert body_kind == "valid", "invalid request reached body coercion"
        return args[1]

    def invoke(*args, **kwargs):
        bound = principal_module.current_principal()
        module.require_owner_context(
            bound.consolidation_owner_context,
            principal=bound,
            identity=_identity(),
            action="status",
            now=NOW,
        )
        return {"owner_bound": True}

    monkeypatch.setattr(server_rest.cli_ops, "coerce", coerce)
    monkeypatch.setattr(writer_lease, "invoke_command", invoke)
    app = FastMCP("owner-boundary-test")
    server_rest.register_rest_facade(
        app,
        vault_root=tmp_path,
        source_schema=None,
        transfer_config=TransferConfig(None, 1024, None, None, None, None),
    )
    client = TestClient(authorization_transport.AuthorizationCarrierMiddleware(app.http_app()))
    headers = {"Authorization": "Bearer test-service-key"}
    if authorized:
        body = {
            "schema": "exomem.consolidate-memory-request/v1",
            "action": "status",
            "run_id": "123e4567-e89b-42d3-a456-426614174000",
        }
        if body_kind == "string-revision":
            body["expected_run_revision"] = "0"
        raw = json.dumps(body)
        if body_kind == "duplicate":
            raw = raw[:-1] + ', "action":"status"}'
        response = client.post("/api/consolidate_memory", headers=headers, content=raw)
        if body_kind == "valid":
            assert response.status_code == 200
            assert response.json()["data"] == {"owner_bound": True}
        else:
            assert response.status_code == 400
            assert response.json()["success"] is False
    else:
        response = client.post("/api/consolidate_memory", headers=headers, content=b"not-json")
        assert response.json()["error"] == module.ConsolidationOwnerUnavailable().as_public_dict()


@pytest.fixture
def issued_local_session(tmp_path, monkeypatch):
    """Real signed custody, v4 store and bearer, all under isolated test roots."""
    from exomem import sidecar_store
    from exomem.governance import authorization_custody, policy, schema_v4, store

    external = tmp_path / "external"
    external.mkdir(mode=0o700)
    for variable, name in (
        (authorization_custody.KEYRING_FILE_ENV, "keyring.json"),
        (authorization_custody.CONTROL_FILE_ENV, "control.json"),
        (authorization_custody.MEMBERSHIP_FILE_ENV, "membership.json"),
    ):
        monkeypatch.setenv(variable, str(external / name))
    monkeypatch.setenv(authorization_custody.REPLICA_ID_ENV, "standalone")
    root = tmp_path / "vault"
    (root / "Knowledge Base/Notes").mkdir(parents=True)
    who = owner_principal(surface="cli")
    consolidation_identity.adopt_local_identity(root, principal=who, now=NOW)
    registered = authorization_custody.load_authorization_custody(root, now=NOW + 1)
    documents = (
        (
            "scopes/owner.yaml",
            b"governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\npaths:\n  - Notes/**\n",
        ),
    )
    compiled = policy.compile_documents(dict(documents))
    assert not compiled.empty and not compiled.blocked
    seed = schema_v4.MigrationSeed(
        activation_store_id="activation-store-owner-test",
        logical_vault_id=registered.control.logical_vault_id,
        activation_epoch=1,
        policy=schema_v4.PolicyGenerationSeed(
            generation_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            source_documents=documents,
            source_fingerprint=compiled.fingerprint,
            conflict_digest="2" * 64,
            compiled_policy=policy.canonical_compiled_bytes(compiled),
            policy_fingerprint=compiled.fingerprint,
            compiler_schema_version=1,
            projector_schema_version=1,
            predecessor_generation_id=None,
            authoring_event_id="event-owner-test",
            receipt_event_id="receipt-owner-test",
            created_at=NOW,
        ),
        catalog=schema_v4.CatalogGenerationSeed(
            catalog_generation=1,
            descriptor=b'{"artifacts":[]}',
            artifact_count=0,
            created_at=NOW,
        ),
        namespace=schema_v4.ProjectionNamespaceSeed(
            namespace_id="projection-owner-test",
            evidence=b'{"ready":true}',
            ready_at=NOW,
        ),
        migrated_at=NOW,
    )
    authorization_custody.enroll_initial_activation_tuple(
        root,
        expected_control=registered.control,
        target=schema_v4.migration_target(seed),
        now=NOW + 1,
    )
    connection = store.open_connection(root)
    try:
        store._migrate(connection)
        sidecar_store.ensure_meta_table(connection, store.DATA_TABLE, "owner-test")
        connection.commit()
        schema_v4.migrate_v3_connection(connection, seed)
    finally:
        connection.close()
    custody = authorization_custody.load_authorization_custody(root, now=NOW + 2)
    connection = store.open_authorization_session_connection(root)
    try:
        issued = authorization_session_lifecycle.open_session(
            connection,
            custody=custody,
            principal_id=who.audience_id,
            issuer_family=who.issuer_family,
            now=NOW + 3,
            ttl_seconds=120,
        )
    finally:
        connection.close()
    return root, custody, issued


def _verified_local_principal(root, issued):
    from exomem.governance import authorization_request

    return authorization_request.verify_authorization_context(
        root,
        principal=owner_principal(surface="cli"),
        credential=issued.bearer,
        now=NOW + 4,
    ).principal


def test_real_descriptor_session_can_bind_and_recheck_owner(issued_local_session, monkeypatch):
    import os
    from types import SimpleNamespace

    from exomem import product_invoke
    from exomem.governance import authorization_transport

    root, _custody, issued = issued_local_session
    monkeypatch.setattr("time.time", lambda: NOW + 4)
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, issued.bearer.encode())
        os.close(write_fd)
        write_fd = -1
        carrier = authorization_transport.read_cli_authorization_fd(str(read_fd))
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)
    cmd = SimpleNamespace(name="consolidate_memory")
    selected, admission = product_invoke.verify_local_authorization_transport(
        cmd,
        raw_for_vault={},
        surface="cli",
        vault_root=root,
        authorization_carrier=carrier,
    )
    request = {
        "schema": "exomem.consolidate-memory-request/v1",
        "action": "status",
        "run_id": "123e4567-e89b-42d3-a456-426614174000",
    }
    bound = product_invoke.enforce_local_authorization_route(
        cmd,
        request,
        admission,
        vault_root=selected,
    )
    _module().require_bound_request(root, principal=bound, arguments=request, now=NOW + 4)
    assert bound.consolidation_owner_context is not None


@pytest.mark.parametrize("stage", ["admit", "recheck"])
@pytest.mark.parametrize("change", ["fabricated", "revoked", "rotated", "keyring"])
def test_durable_session_is_rechecked_before_private_identity(
    issued_local_session,
    monkeypatch,
    stage,
    change,
):
    from exomem.governance import store

    root, custody, issued = issued_local_session
    module = _module()
    who = _verified_local_principal(root, issued)
    request = {
        "schema": "exomem.consolidate-memory-request/v1",
        "action": "status",
        "run_id": "123e4567-e89b-42d3-a456-426614174000",
    }
    if stage == "recheck":
        who = module.bind_local_owner(root, principal=who, arguments=request, now=NOW + 4)
    session = who.verified_authorization_session
    if change == "fabricated":
        session = replace(session, session_id="authorization-session:" + "0" * 32)
    elif change == "keyring":
        session = replace(session, keyring_id="never-issued-keyring")
    else:
        connection = store.open_authorization_session_connection(root)
        try:
            operation = (
                authorization_session_lifecycle.close_session
                if change == "revoked"
                else authorization_session_lifecycle.rotate_session
            )
            kwargs = {"ttl_seconds": 60} if change == "rotated" else {}
            operation(
                connection,
                custody=custody,
                bearer=issued.bearer,
                principal_id=session.principal_id,
                issuer_family=session.issuer_family,
                now=NOW + 5,
                **kwargs,
            )
        finally:
            connection.close()
    who = replace(
        who, verified_authorization_session=session, authorization_session_id=session.session_id
    )
    monkeypatch.setattr(
        consolidation_identity,
        "load_local_identity",
        lambda *a, **k: pytest.fail("unverified session read private identity"),
    )
    operation = module.bind_local_owner if stage == "admit" else module.require_bound_request
    with pytest.raises(module.ConsolidationOwnerUnavailable):
        operation(root, principal=who, arguments=request, now=NOW + 6)


@pytest.mark.parametrize("extra", [[], ["--unknown", "private-action-value"]])
def test_cli_without_owner_carrier_refuses_before_any_argument_parsing(monkeypatch, capsys, extra):
    from exomem import __main__ as cli_main
    from exomem import commands
    from exomem.command_surface import Command

    cmd = Command("consolidate_memory", lambda: None, (), frozenset({"cli"}))
    monkeypatch.setattr(commands, "product_commands_for", lambda *a, **k: (cmd,))
    monkeypatch.setattr(
        cli_main, "_CLIParser", lambda *a, **k: pytest.fail("absent session reached CLI parsing")
    )
    assert cli_main._core_op_main(["consolidate_memory", *extra]) == 1
    captured = capsys.readouterr()
    assert "CONSOLIDATION_OWNER_UNAVAILABLE" in captured.err
    assert "private-action-value" not in captured.err


@pytest.mark.parametrize("invalid", ["0", 0.0, True, -1])
def test_local_adapter_refuses_semantically_invalid_request_before_coercion(
    tmp_path,
    monkeypatch,
    verified_session_stub,
    invalid,
):
    from types import SimpleNamespace

    from exomem import product_invoke
    from exomem.governance import authorization_request, consolidation_request

    who = _principal("cli")
    monkeypatch.setattr(consolidation_identity, "load_local_identity", lambda *a, **k: _identity())
    monkeypatch.setattr("time.time", lambda: NOW)
    with pytest.raises(consolidation_request.ConsolidationRequestUnavailable):
        product_invoke.enforce_local_authorization_route(
            SimpleNamespace(name="consolidate_memory"),
            {
                "schema": consolidation_request.REQUEST_SCHEMA_NAME,
                "action": "status",
                "run_id": "123e4567-e89b-42d3-a456-426614174000",
                "expected_run_revision": invalid,
            },
            authorization_request.AuthorizationAdmission(who, True),
            vault_root=tmp_path,
        )


def test_mcp_raw_duplicates_cannot_become_an_admissible_request():
    from exomem.governance import authorization_transport

    raw = (
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
        b'"params":{"name":"consolidate_memory","arguments":{'
        b'"action":"status","action":"apply"}}}'
    )
    result = authorization_transport.sanitize_mcp_http_body(raw)
    assert result.carrier.is_invalid


@pytest.mark.parametrize(
    "mode,proof,valid",
    [
        ("real-cutover", False, False),
        ("real-cutover", True, True),
        ("cloned-rehearsal", False, True),
        ("cloned-rehearsal", True, False),
    ],
)
def test_adapter_uses_stored_run_mode_for_cutover_proof(
    tmp_path,
    monkeypatch,
    verified_session_stub,
    mode,
    proof,
    valid,
):
    from types import SimpleNamespace

    from exomem import product_invoke
    from exomem.governance import (
        authorization_request,
        consolidation_request,
        consolidation_run_state,
    )

    identity = _identity()
    who = _principal("cli")
    monkeypatch.setattr(consolidation_identity, "load_local_identity", lambda *a, **k: identity)
    monkeypatch.setattr("time.time", lambda: NOW)
    loads = []

    def load(store, run_id):
        loads.append(run_id)
        return SimpleNamespace(
            identity=SimpleNamespace(
                run_mode=mode,
                destination_vault_id=identity.vault_id,
                destination_installation_id=identity.installation_id,
                destination_generation=identity.installation_generation,
                destination_fence_digest=identity.active_fence_digest,
            )
        )

    monkeypatch.setattr(consolidation_run_state.ConsolidationRunStore, "load", load)
    options = {
        name: "a" * 64
        for name in (
            "expected_reconciliation_digest",
            "expected_policy_bundle_digest",
            "expected_principal_attestation_set_digest",
            "expected_verification_plan_digest",
            "expected_rollback_contingency_digest",
            "expected_source_retention_digest",
            "expected_control_basis_digest",
        )
    }
    if proof:
        options["expected_rehearsal_proof_digest"] = "a" * 64
    request = {
        "schema": consolidation_request.REQUEST_SCHEMA_NAME,
        "action": "plan",
        "operation_id": "123e4567-e89b-42d3-a456-426614174001",
        "run_id": "123e4567-e89b-42d3-a456-426614174000",
        "expected_run_revision": 0,
        "plan_kind": "cutover",
        "operation": "materialize",
        "successor_context_ref": "context",
        "successor_context_digest": "a" * 64,
        "cutover_options": options,
    }

    def invoke():
        return product_invoke.enforce_local_authorization_route(
            SimpleNamespace(name="consolidate_memory"),
            request,
            authorization_request.AuthorizationAdmission(who, True),
            vault_root=tmp_path,
        )

    if valid:
        invoke()
    else:
        with pytest.raises(consolidation_request.ConsolidationRequestUnavailable):
            invoke()
    assert loads == [request["run_id"]]


def test_mcp_sanitizer_preserves_surrogates_for_post_owner_semantic_refusal():
    from exomem.governance import authorization_transport

    raw = (
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
        b'"params":{"name":"consolidate_memory","arguments":{"cursor":"\\ud800"}}}'
    )
    result = authorization_transport.sanitize_mcp_http_body(raw)
    assert result.arguments["cursor"] == "\ud800"
    result.body.decode("utf-8")


def test_mcp_raw_decoder_refuses_non_utf8_json():
    from exomem.governance import authorization_transport

    raw = (
        '{"jsonrpc":"2.0","id":1,"method":"tools/call",'
        '"params":{"name":"consolidate_memory","arguments":{}}}'
    )
    with pytest.raises(authorization_transport.AuthorizationEnvelopeUnavailable):
        authorization_transport.sanitize_mcp_http_body(raw.encode("utf-16"))


def test_stdio_decoder_refuses_invalid_utf8_without_normalizing_it(monkeypatch):
    import asyncio
    import io
    from types import SimpleNamespace

    import anyio
    from mcp.shared.message import SessionMessage

    from exomem.governance import authorization_transport

    invalid = (
        b'{"jsonrpc":"2.0","id":1,"method":"tools/call",'
        b'"params":{"name":"consolidate_memory","arguments":{"cursor":"\xff"}}}\n'
    )
    valid = b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n'
    monkeypatch.setattr(
        authorization_transport.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(invalid + valid))
    )

    async def receive():
        output = anyio.wrap_file(io.StringIO())
        async with authorization_transport.sanitized_stdio_server(stdout=output) as (
            reader,
            writer,
        ):
            first = await reader.receive()
            second = await reader.receive()
            await writer.aclose()
        return first, second

    first, second = asyncio.run(receive())
    assert isinstance(first, authorization_transport.AuthorizationEnvelopeUnavailable)
    assert isinstance(second, SessionMessage)
    assert second.message.root.id == 2
