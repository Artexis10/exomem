"""Counterfactual wire-envelope closure for governed projected retrieval."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from governance_projection_support import verified_namespace
from starlette.testclient import TestClient

from exomem import server, writer_lease
from exomem.governance import (
    decisions,
    principal,
    projection_authorization,
    projection_runtime,
    projection_store,
    projections,
    schema_v4,
)
from exomem.governance.policy import Policy, Scope
from exomem.server_runtime import ServerRuntime

_REST_KEY = "governance-oracle-key"
_TRANSPORT_ONLY_HEADERS = frozenset({"date", "traceparent", "tracestate"})


@dataclass(frozen=True, slots=True)
class _CanonicalTransportEnvelope:
    status_code: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


def _canonical_transport_envelope(
    value,  # noqa: ANN001
    *,
    transport: str,
) -> _CanonicalTransportEnvelope:
    """Remove only the closed metadata set for the named transport."""

    if transport == "http":
        headers = tuple(
            sorted(
                (name.lower(), header_value)
                for name, header_value in value.headers.multi_items()
                if name.lower() not in _TRANSPORT_ONLY_HEADERS
            )
        )
        return _CanonicalTransportEnvelope(
            status_code=int(value.status_code),
            headers=headers,
            body=bytes(value.content),
        )
    if transport == "jsonrpc":
        decoded = json.loads(value)
        if (
            not isinstance(decoded, dict)
            or decoded.get("jsonrpc") != "2.0"
            or "id" not in decoded
        ):
            raise ValueError("normalizer requires one JSON-RPC response envelope")
        normalized = dict(decoded)
        normalized.pop("id")
        return _CanonicalTransportEnvelope(
            status_code=200,
            headers=(),
            body=json.dumps(
                normalized,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )
    raise ValueError("normalizer supports only HTTP and JSON-RPC")


def test_transport_normalizer_removes_only_closed_outer_metadata() -> None:
    first = httpx.Response(
        200,
        headers={
            "Content-Type": "application/json",
            "Date": "Wed, 26 Aug 2026 10:00:00 GMT",
            "Traceparent": "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01",
            "X-Request-ID": "application-request-1",
        },
        content=b'{"data":{"request_id":"application-request-1"}}',
    )
    transport_changed = httpx.Response(
        200,
        headers={
            "Content-Type": "application/json",
            "Date": "Wed, 26 Aug 2026 10:00:01 GMT",
            "Traceparent": "00-cccccccccccccccccccccccccccccccc-dddddddddddddddd-01",
            "X-Request-ID": "application-request-1",
        },
        content=first.content,
    )
    application_changed = httpx.Response(
        200,
        headers={
            "Content-Type": "application/json",
            "Date": "Wed, 26 Aug 2026 10:00:01 GMT",
            "Traceparent": "00-cccccccccccccccccccccccccccccccc-dddddddddddddddd-01",
            "X-Request-ID": "application-request-2",
        },
        content=b'{"data":{"request_id":"application-request-2"}}',
    )

    assert _canonical_transport_envelope(
        first, transport="http"
    ) == _canonical_transport_envelope(transport_changed, transport="http")
    assert _canonical_transport_envelope(
        first, transport="http"
    ) != _canonical_transport_envelope(application_changed, transport="http")

    jsonrpc_one = b'{"jsonrpc":"2.0","id":1,"result":{"request_id":"stable"}}'
    jsonrpc_two = b'{"result":{"request_id":"stable"},"id":"two","jsonrpc":"2.0"}'
    jsonrpc_application_change = (
        b'{"jsonrpc":"2.0","id":1,"result":{"request_id":"changed"}}'
    )
    assert _canonical_transport_envelope(
        jsonrpc_one, transport="jsonrpc"
    ) == _canonical_transport_envelope(jsonrpc_two, transport="jsonrpc")
    assert _canonical_transport_envelope(
        jsonrpc_one, transport="jsonrpc"
    ) != _canonical_transport_envelope(jsonrpc_application_change, transport="jsonrpc")


def _projected_item(
    policy: Policy,
    *,
    path: str,
    scope_id: str,
    body: str,
) -> projection_store.ProjectionItemVariants:
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return projection_store.ProjectionItemVariants(
        item_identity=path,
        content_hash=content_hash,
        scope_ids=(scope_id,),
        variants=projections.enumerate_projection_variants(
            item_identity=path,
            content_hash=content_hash,
            scope_ids=(scope_id,),
            policy=policy,
            projector_schema_version=projections.PROJECTOR_SCHEMA_VERSION,
            full_search_fields={
                "title": Path(path).stem,
                "body": body,
                "type": "insight",
                "status": "active",
                "updated": "2026-08-26",
            },
        ),
    )


def _counterfactual_runtime(*, hidden_present: bool) -> projection_runtime.ActiveProjectionRuntime:
    visible = Scope(id="oracle-visible", source="scopes/oracle-visible.yaml")
    hidden = Scope(
        id="oracle-hidden",
        source="scopes/oracle-hidden.yaml",
        default_deny=True,
    )
    policy = Policy(
        fingerprint="f" * 64,
        scopes={visible.id: visible, hidden.id: hidden},
    )
    items = [
        _projected_item(
            policy,
            path="Knowledge Base/oracle-visible-a.md",
            scope_id=visible.id,
            body="oracle-visible alpha projected result",
        ),
        _projected_item(
            policy,
            path="Knowledge Base/oracle-visible-b.md",
            scope_id=visible.id,
            body="oracle-visible beta projected result",
        ),
    ]
    if hidden_present:
        items.append(
            _projected_item(
                policy,
                path="Knowledge Base/private/oracle-hidden.md",
                scope_id=hidden.id,
                body="oracle-visible " * 32 + "counterfactual hidden source",
            )
        )
    item_tuple = tuple(items)
    key = projections.ProjectionNamespaceKey(
        policy_fingerprint=policy.fingerprint,
        projector_schema_version=projections.PROJECTOR_SCHEMA_VERSION,
        catalog_generation=len(item_tuple),
    )
    namespace = verified_namespace(key, item_tuple)
    snapshot = schema_v4.ActivePolicySnapshot(
        active=schema_v4.VerifiedActiveGovernanceState(
            logical_vault_id="oracle-vault",
            activation_store_id="oracle-store",
            activation_epoch=len(item_tuple),
            activation_state_digest=namespace.active_state_digest,
            policy_generation_id="oracle-policy",
            policy_fingerprint=key.policy_fingerprint,
            projector_schema_version=key.projector_schema_version,
            catalog_generation=key.catalog_generation,
            projection_namespace_id=key.namespace_id,
        ),
        policy=policy,
        source_documents=(),
        catalog_descriptor=projection_store.catalog_descriptor_bytes(key, item_tuple),
        projection_namespace_evidence=(
            projection_store.projection_namespace_evidence_bytes(namespace.manifest)
        ),
    )
    return projection_runtime.ActiveProjectionRuntime(snapshot, namespace)


def _client(monkeypatch: pytest.MonkeyPatch, root: Path) -> TestClient:
    runtime = ServerRuntime(
        vault_root=root,
        source_schema=SimpleNamespace(source_types={}),
        project_keys_hint="",
        base_url="",
    )
    monkeypatch.setattr(server, "initialize_runtime", lambda **_kwargs: runtime)
    return TestClient(server.build_server(require_auth=False).http_app())


def test_l0_present_and_physically_absent_have_the_same_complete_wire_envelope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A default-denied row cannot alter any serialized application field."""

    monkeypatch.setenv("EXOMEM_REST_API_KEY", _REST_KEY)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_RANKING", "1")
    writer_state = tmp_path / "writer-state"
    writer_state.mkdir()
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(writer_state))
    monkeypatch.setattr(writer_lease, "start_server_lifecycle", lambda: None)
    writer_lease.reset_managers_for_tests()
    monkeypatch.setattr(
        principal,
        "resolve_rest_principal",
        lambda _scope: principal.RequestPrincipal(
            audience_id="oracle-external",
            surface="rest",
            resolved=True,
            issuer_family="oracle-test",
        ),
    )

    roots = {
        "absent": tmp_path / "absent",
        "present": tmp_path / "present",
    }
    for root in roots.values():
        (root / "Knowledge Base").mkdir(parents=True)
    runtimes = {
        roots["absent"]: _counterfactual_runtime(hidden_present=False),
        roots["present"]: _counterfactual_runtime(hidden_present=True),
    }
    present_runtime = runtimes[roots["present"]]
    authorization = projection_authorization.build_authorization_map(
        present_runtime.namespace,
        policy=present_runtime.snapshot.policy,
        audience="oracle-external",
        purpose=None,
        verified_session_grants=(),
        catalog=present_runtime.catalog,
    )
    assert "Knowledge Base/private/oracle-hidden.md" in authorization.withheld_identities
    assert decisions.decide(
        ["oracle-hidden"],
        audience="oracle-external",
        policy=present_runtime.snapshot.policy,
    ).level == 0
    monkeypatch.setattr(
        projection_runtime,
        "has_preactivated_projection_runtime",
        lambda root: Path(root) in runtimes,
    )
    monkeypatch.setattr(
        projection_runtime,
        "load_active_projection_runtime",
        lambda root: runtimes[Path(root)],
    )
    clients = {name: _client(monkeypatch, root) for name, root in roots.items()}
    body = {
        "query": "oracle-visible",
        "scope": "vault",
        "mode": "keyword",
        "graph": False,
        "rerank": False,
        "limit": 2,
        "detail": "full",
        "include_timings": True,
    }
    try:
        responses = {
            name: client.post(
                "/api/ask_memory",
                json=body,
                headers={"Authorization": f"Bearer {_REST_KEY}"},
            )
            for name, client in clients.items()
        }
    finally:
        for client in clients.values():
            client.close()
        writer_lease.reset_managers_for_tests()

    assert responses["absent"].status_code == 200, responses["absent"].text
    assert responses["present"].status_code == 200, responses["present"].text
    assert responses["absent"].json()["data"]["timings_suppressed"] == {
        "status": "governed_projection"
    }
    assert b"oracle-hidden" not in responses["present"].content
    assert _canonical_transport_envelope(
        responses["present"], transport="http"
    ) == _canonical_transport_envelope(responses["absent"], transport="http")
