"""Counterfactual wire-envelope closure for governed projected retrieval."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
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
from exomem.governance.policy import Policy, Rule, Scope
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


_DEFAULT_VISIBLE_BODIES = (
    "oracle-visible alpha projected result",
    "oracle-visible beta projected result",
)


def _runtime_from_items(
    policy: Policy,
    items: tuple[projection_store.ProjectionItemVariants, ...],
) -> projection_runtime.ActiveProjectionRuntime:
    key = projections.ProjectionNamespaceKey(
        policy_fingerprint=policy.fingerprint,
        projector_schema_version=projections.PROJECTOR_SCHEMA_VERSION,
        catalog_generation=len(items),
    )
    namespace = verified_namespace(key, items)
    snapshot = schema_v4.ActivePolicySnapshot(
        active=schema_v4.VerifiedActiveGovernanceState(
            logical_vault_id="oracle-vault",
            activation_store_id="oracle-store",
            activation_epoch=len(items),
            activation_state_digest=namespace.active_state_digest,
            policy_generation_id="oracle-policy",
            policy_fingerprint=key.policy_fingerprint,
            projector_schema_version=key.projector_schema_version,
            catalog_generation=key.catalog_generation,
            projection_namespace_id=key.namespace_id,
        ),
        policy=policy,
        source_documents=(),
        catalog_descriptor=projection_store.catalog_descriptor_bytes(key, items),
        projection_namespace_evidence=(
            projection_store.projection_namespace_evidence_bytes(namespace.manifest)
        ),
    )
    return projection_runtime.ActiveProjectionRuntime(snapshot, namespace)


def _counterfactual_runtime(
    *,
    hidden_bodies: tuple[str, ...],
    visible_bodies: tuple[str, ...] = _DEFAULT_VISIBLE_BODIES,
) -> projection_runtime.ActiveProjectionRuntime:
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
            path=f"Knowledge Base/oracle-visible-{index:03d}.md",
            scope_id=visible.id,
            body=body,
        )
        for index, body in enumerate(visible_bodies)
    ]
    for index, body in enumerate(hidden_bodies):
        items.append(
            _projected_item(
                policy,
                path=f"Knowledge Base/private/oracle-hidden-{index:03d}.md",
                scope_id=hidden.id,
                body=body,
            )
        )
    return _runtime_from_items(policy, tuple(items))


def _client(monkeypatch: pytest.MonkeyPatch, root: Path) -> TestClient:
    runtime = ServerRuntime(
        vault_root=root,
        source_schema=SimpleNamespace(source_types={}),
        project_keys_hint="",
        base_url="",
    )
    monkeypatch.setattr(server, "initialize_runtime", lambda **_kwargs: runtime)
    return TestClient(server.build_server(require_auth=False).http_app())


def _wire_pair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    hidden_bodies: tuple[str, ...],
    request: dict[str, object],
    visible_bodies: tuple[str, ...] = _DEFAULT_VISIBLE_BODIES,
    runtime_factory: Callable[
        [tuple[str, ...]], projection_runtime.ActiveProjectionRuntime
    ]
    | None = None,
) -> tuple[
    dict[str, projection_runtime.ActiveProjectionRuntime],
    dict[str, httpx.Response],
]:
    monkeypatch.setenv("EXOMEM_REST_API_KEY", _REST_KEY)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_RANKING", "1")
    writer_state = tmp_path / "writer-state"
    writer_state.mkdir()
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(writer_state))
    monkeypatch.setattr(writer_lease, "start_server_lifecycle", lambda: None)
    writer_lease.reset_managers_for_tests()
    projection_runtime._clear_projected_continuations_for_tests()
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
    build_runtime = runtime_factory or (
        lambda hidden: _counterfactual_runtime(
            hidden_bodies=hidden,
            visible_bodies=visible_bodies,
        )
    )
    runtimes_by_root = {
        roots["absent"]: build_runtime(()),
        roots["present"]: build_runtime(hidden_bodies),
    }
    monkeypatch.setattr(
        projection_runtime,
        "has_preactivated_projection_runtime",
        lambda root: Path(root) in runtimes_by_root,
    )
    monkeypatch.setattr(
        projection_runtime,
        "load_active_projection_runtime",
        lambda root: runtimes_by_root[Path(root)],
    )
    clients = {name: _client(monkeypatch, root) for name, root in roots.items()}
    try:
        responses = {
            name: client.post(
                "/api/ask_memory",
                json=request,
                headers={"Authorization": f"Bearer {_REST_KEY}"},
            )
            for name, client in clients.items()
        }
    finally:
        for client in clients.values():
            client.close()
        projection_runtime._clear_projected_continuations_for_tests()
        writer_lease.reset_managers_for_tests()
    return (
        {name: runtimes_by_root[root] for name, root in roots.items()},
        responses,
    )


def _request(*, query: str, limit: int, mode: str = "hybrid") -> dict[str, object]:
    return {
        "query": query,
        "scope": "vault",
        "mode": mode,
        "graph": False,
        "rerank": False,
        "limit": limit,
        "detail": "full",
        "include_timings": True,
    }


def _assert_same_success_envelope(responses: dict[str, httpx.Response]) -> None:
    assert responses["absent"].status_code == 200, responses["absent"].text
    assert responses["present"].status_code == 200, responses["present"].text
    assert _canonical_transport_envelope(
        responses["present"], transport="http"
    ) == _canonical_transport_envelope(responses["absent"], transport="http")


def test_l0_present_and_physically_absent_have_the_same_complete_wire_envelope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A default-denied row cannot alter any serialized application field."""

    runtimes, responses = _wire_pair(
        monkeypatch,
        tmp_path,
        hidden_bodies=(
            "oracle-visible " * 32 + "counterfactual hidden source",
        ),
        request=_request(query="oracle-visible", limit=2, mode="keyword"),
    )
    present_runtime = runtimes["present"]
    authorization = projection_authorization.build_authorization_map(
        present_runtime.namespace,
        policy=present_runtime.snapshot.policy,
        audience="oracle-external",
        purpose=None,
        verified_session_grants=(),
        catalog=present_runtime.catalog,
    )
    assert (
        "Knowledge Base/private/oracle-hidden-000.md"
        in authorization.withheld_identities
    )
    assert decisions.decide(
        ["oracle-hidden"],
        audience="oracle-external",
        policy=present_runtime.snapshot.policy,
    ).level == 0
    _assert_same_success_envelope(responses)
    assert responses["absent"].json()["data"]["timings_suppressed"] == {
        "status": "governed_projection"
    }
    assert b"oracle-hidden" not in responses["present"].content


_RANK_VISIBLE_BODIES = (
    "rank-needle " * 16 + "visible high",
    "rank-needle " * 4 + "visible middle",
    "rank-needle visible low",
)


@pytest.mark.parametrize(
    ("rank_band", "hidden_body", "expected_owner_rank"),
    (
        ("high", "rank-needle " * 32 + "hidden high", 0),
        ("middle", "rank-needle " * 8 + "hidden middle", 1),
        ("low", "rank-needle " + "filler " * 96 + "hidden low", 3),
    ),
)
def test_hidden_raw_rank_cannot_displace_the_public_top_k(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rank_band: str,
    hidden_body: str,
    expected_owner_rank: int,
) -> None:
    runtimes, responses = _wire_pair(
        monkeypatch,
        tmp_path,
        hidden_bodies=(hidden_body,),
        visible_bodies=_RANK_VISIBLE_BODIES,
        request=_request(query="rank-needle", limit=1),
    )
    present_runtime = runtimes["present"]
    owner_authorization = projection_authorization.build_authorization_map(
        present_runtime.namespace,
        policy=present_runtime.snapshot.policy,
        audience=principal.OWNER_AUDIENCE,
        purpose=None,
        verified_session_grants=(),
        catalog=present_runtime.catalog,
    )
    owner_order = [
        hit.item_identity
        for hit in present_runtime.lexical_index.search_bm25(
            owner_authorization,
            "rank-needle",
            k=4,
        )
    ]
    hidden_identity = "Knowledge Base/private/oracle-hidden-000.md"
    assert owner_order.index(hidden_identity) == expected_owner_rank, rank_band

    _assert_same_success_envelope(responses)
    data = responses["present"].json()["data"]
    assert len(data["hits"]) == 1
    assert isinstance(data["continuation"], str)
    assert b"oracle-hidden" not in responses["present"].content


def test_hidden_candidates_beyond_the_visible_top_k_never_consume_the_lane_cap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtimes, responses = _wire_pair(
        monkeypatch,
        tmp_path,
        hidden_bodies=tuple(
            "rank-needle " * 24 + f"hidden {index}" for index in range(80)
        ),
        visible_bodies=_RANK_VISIBLE_BODIES,
        request=_request(query="rank-needle", limit=1),
    )
    external_authorization = projection_authorization.build_authorization_map(
        runtimes["present"].namespace,
        policy=runtimes["present"].snapshot.policy,
        audience="oracle-external",
        purpose=None,
        verified_session_grants=(),
        catalog=runtimes["present"].catalog,
    )
    assert len(external_authorization.withheld_identities) == 80

    _assert_same_success_envelope(responses)
    assert len(responses["present"].json()["data"]["hits"]) == 1


def _projection_only_runtime(
    hidden_bodies: tuple[str, ...],
) -> projection_runtime.ActiveProjectionRuntime:
    projected = Scope(id="oracle-projected", source="scopes/oracle-projected.yaml")
    hidden = Scope(
        id="oracle-hidden",
        source="scopes/oracle-hidden.yaml",
        default_deny=True,
    )
    policy = Policy(
        fingerprint="e" * 64,
        scopes={projected.id: projected, hidden.id: hidden},
        rules=(
            Rule(
                id="oracle-projected-external",
                source="rules/oracle-projected-external.yaml",
                scope_ids=(projected.id,),
                audience="oracle-external",
                ceiling=3,
                options={"abstract": "projectivequartz safe abstract"},
            ),
        ),
    )
    items = [
        _projected_item(
            policy,
            path=f"Knowledge Base/oracle-visible-{index:03d}.md",
            scope_id=projected.id,
            body=f"raw-source-only visible item {index}",
        )
        for index in range(2)
    ]
    items.extend(
        _projected_item(
            policy,
            path=f"Knowledge Base/private/oracle-hidden-{index:03d}.md",
            scope_id=hidden.id,
            body=body,
        )
        for index, body in enumerate(hidden_bodies)
    )
    return _runtime_from_items(policy, tuple(items))


def test_projection_only_term_acquires_visible_rows_without_raw_text_or_l0_influence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtimes, responses = _wire_pair(
        monkeypatch,
        tmp_path,
        hidden_bodies=("projectivequartz " * 32 + "hidden raw source",),
        runtime_factory=_projection_only_runtime,
        request=_request(query="projectivequartz", limit=2),
    )
    present_runtime = runtimes["present"]
    external = projection_authorization.build_authorization_map(
        present_runtime.namespace,
        policy=present_runtime.snapshot.policy,
        audience="oracle-external",
        purpose=None,
        verified_session_grants=(),
        catalog=present_runtime.catalog,
    )
    owner = projection_authorization.build_authorization_map(
        present_runtime.namespace,
        policy=present_runtime.snapshot.policy,
        audience=principal.OWNER_AUDIENCE,
        purpose=None,
        verified_session_grants=(),
        catalog=present_runtime.catalog,
    )
    external_hits = present_runtime.lexical_index.search_bm25(
        external,
        "projectivequartz",
        k=3,
    )
    owner_hits = present_runtime.lexical_index.search_bm25(
        owner,
        "projectivequartz",
        k=3,
    )
    assert [hit.decision_level for hit in external_hits] == [3, 3]
    assert [hit.item_identity for hit in owner_hits] == [
        "Knowledge Base/private/oracle-hidden-000.md"
    ]

    _assert_same_success_envelope(responses)
    content = responses["present"].content
    assert b"projectivequartz" in content
    assert b"raw-source-only" not in content
    assert b"oracle-hidden" not in content


def test_hidden_document_frequency_cannot_change_visible_bm25_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    visible_bodies = (
        "idf-common idf-shared visible-a",
        "idf-rare idf-shared visible-b",
    )
    runtimes, responses = _wire_pair(
        monkeypatch,
        tmp_path,
        hidden_bodies=tuple(
            f"idf-common idf-shared hidden-{index}" for index in range(10)
        ),
        visible_bodies=visible_bodies,
        request=_request(query="idf-common idf-rare idf-shared", limit=2),
    )

    def owner_visible_order(
        runtime: projection_runtime.ActiveProjectionRuntime,
    ) -> list[str]:
        authorization = projection_authorization.build_authorization_map(
            runtime.namespace,
            policy=runtime.snapshot.policy,
            audience=principal.OWNER_AUDIENCE,
            purpose=None,
            verified_session_grants=(),
            catalog=runtime.catalog,
        )
        return [
            hit.item_identity
            for hit in runtime.lexical_index.search_bm25(
                authorization,
                "idf-common idf-rare idf-shared",
                k=12,
            )
            if "/private/" not in hit.item_identity
        ]

    assert owner_visible_order(runtimes["absent"]) == [
        "Knowledge Base/oracle-visible-000.md",
        "Knowledge Base/oracle-visible-001.md",
    ]
    assert owner_visible_order(runtimes["present"]) == [
        "Knowledge Base/oracle-visible-001.md",
        "Knowledge Base/oracle-visible-000.md",
    ]

    _assert_same_success_envelope(responses)
    assert [
        hit["path"] for hit in responses["present"].json()["data"]["hits"]
    ] == [
        "Knowledge Base/oracle-visible-000.md",
        "Knowledge Base/oracle-visible-001.md",
    ]
