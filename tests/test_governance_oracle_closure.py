"""Counterfactual wire-envelope closure for governed projected retrieval."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from governance_projection_support import verified_namespace
from starlette.testclient import TestClient

from exomem import embeddings, readiness, server, writer_lease
from exomem.governance import (
    decisions,
    principal,
    projected_graph,
    projected_retrieval,
    projection_authorization,
    projection_measurement_store,
    projection_runtime,
    projection_store,
    projection_timing,
    projections,
    schema_v4,
)
from exomem.governance.policy import Policy, Rule, Scope
from exomem.server_runtime import ServerRuntime

_REST_KEY = "governance-oracle-key"
_TRANSPORT_ONLY_HEADERS = frozenset({"date", "traceparent", "tracestate"})
_VECTOR_EXTRACTOR = "projected-text-v1"
_CLIP_EXTRACTOR = "pixels-v1"
_GRAPH_EXTRACTOR = "projected-graph-v1"
_GRAPH_MODEL = "graph-schema-v1"


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
    media_type: str | None = None,
) -> projection_store.ProjectionItemVariants:
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    search_fields = {
        "title": Path(path).stem,
        "body": body,
        "type": "media" if media_type is not None else "insight",
        "status": "active",
        "updated": "2026-08-26",
    }
    if media_type is not None:
        search_fields["media_type"] = media_type
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
            full_search_fields=search_fields,
        ),
    )


_DEFAULT_VISIBLE_BODIES = (
    "oracle-visible alpha projected result",
    "oracle-visible beta projected result",
)


def _runtime_from_items(
    policy: Policy,
    items: tuple[projection_store.ProjectionItemVariants, ...],
    *,
    vector_for_variant: Callable[
        [projections.ProjectionVariant], tuple[float, ...]
    ]
    | None = None,
    clip_samples_for_variant: Callable[
        [projections.ProjectionVariant],
        tuple[projected_retrieval.ProjectionClipSample, ...] | None,
    ]
    | None = None,
    graph_edges_for_variant: Callable[
        [projections.ProjectionVariant],
        tuple[projected_graph.ProjectionGraphEdge, ...],
    ]
    | None = None,
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
    roots: list[projection_store.ProjectionMeasurementRoot] = []
    vector_index: projected_retrieval.ProjectedVectorIndex | None = None
    clip_index: projected_retrieval.ProjectedClipIndex | None = None
    graph_index: projected_graph.ProjectedGraphIndex | None = None
    if vector_for_variant is not None:
        vector_measurements = tuple(
            projected_retrieval.ProjectionVectorMeasurement(
                projections.MeasurementKey(
                    projection_variant_id=variant.projection_variant_id,
                    lane="vector",
                    extractor_version=_VECTOR_EXTRACTOR,
                    model_version=embeddings.MODEL_NAME,
                ),
                vector_for_variant(variant),
            )
            for item in items
            for variant in item.variants
        )
        vector_index = projected_retrieval.ProjectedVectorIndex(
            namespace,
            vector_measurements,
            extractor_version=_VECTOR_EXTRACTOR,
            model_version=embeddings.MODEL_NAME,
        )
        vector_family = projection_measurement_store.MeasurementFamilyKey(
            namespace_key=key,
            lane="vector",
            extractor_version=_VECTOR_EXTRACTOR,
            model_version=embeddings.MODEL_NAME,
        )
        roots.append(
            projection_store.ProjectionMeasurementRoot(
                namespace_key=key,
                family_id=vector_family.family_id,
                lane="vector",
                extractor_version=_VECTOR_EXTRACTOR,
                model_version=embeddings.MODEL_NAME,
                measurement_count=len(vector_measurements),
                vector_dimension=len(vector_measurements[0].vector),
                graph_edge_count=0,
                rows_digest="a" * 64,
            )
        )
    if clip_samples_for_variant is not None:
        clip_measurements: list[
            projected_retrieval.ProjectionClipMeasurement
        ] = []
        for item in items:
            for variant in item.variants:
                samples = clip_samples_for_variant(variant)
                if samples is None:
                    continue
                clip_measurements.append(
                    projected_retrieval.ProjectionClipMeasurement(
                        projections.MeasurementKey(
                            projection_variant_id=variant.projection_variant_id,
                            lane="clip",
                            extractor_version=_CLIP_EXTRACTOR,
                            model_version=embeddings.CLIP_MODEL_NAME,
                        ),
                        samples=samples,
                    )
                )
        if clip_measurements:
            clip_index = projected_retrieval.ProjectedClipIndex(
                namespace,
                clip_measurements,
                extractor_version=_CLIP_EXTRACTOR,
                model_version=embeddings.CLIP_MODEL_NAME,
            )
            clip_family = projection_measurement_store.MeasurementFamilyKey(
                namespace_key=key,
                lane="clip",
                extractor_version=_CLIP_EXTRACTOR,
                model_version=embeddings.CLIP_MODEL_NAME,
            )
            roots.append(
                projection_store.ProjectionMeasurementRoot(
                    namespace_key=key,
                    family_id=clip_family.family_id,
                    lane="clip",
                    extractor_version=_CLIP_EXTRACTOR,
                    model_version=embeddings.CLIP_MODEL_NAME,
                    measurement_count=len(clip_measurements),
                    vector_dimension=len(clip_measurements[0].samples[0].vector),
                    graph_edge_count=0,
                    rows_digest="c" * 64,
                )
            )
    if graph_edges_for_variant is not None:
        graph_measurements = tuple(
            projected_graph.ProjectionGraphMeasurement(
                projections.MeasurementKey(
                    projection_variant_id=variant.projection_variant_id,
                    lane="graph",
                    extractor_version=_GRAPH_EXTRACTOR,
                    model_version=_GRAPH_MODEL,
                ),
                graph_edges_for_variant(variant),
            )
            for item in items
            for variant in item.variants
        )
        graph_index = projected_graph.ProjectedGraphIndex(
            namespace,
            graph_measurements,
            extractor_version=_GRAPH_EXTRACTOR,
            model_version=_GRAPH_MODEL,
        )
        graph_family = projection_measurement_store.MeasurementFamilyKey(
            namespace_key=key,
            lane="graph",
            extractor_version=_GRAPH_EXTRACTOR,
            model_version=_GRAPH_MODEL,
        )
        roots.append(
            projection_store.ProjectionMeasurementRoot(
                namespace_key=key,
                family_id=graph_family.family_id,
                lane="graph",
                extractor_version=_GRAPH_EXTRACTOR,
                model_version=_GRAPH_MODEL,
                measurement_count=len(graph_measurements),
                vector_dimension=None,
                graph_edge_count=sum(
                    len(measurement.edges) for measurement in graph_measurements
                ),
                rows_digest="b" * 64,
            )
        )
    return projection_runtime.ActiveProjectionRuntime(
        snapshot,
        namespace,
        tuple(roots),
        vector_index=vector_index,
        clip_index=clip_index,
        graph_index=graph_index,
    )


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


def _wire_pages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    hidden_bodies: tuple[str, ...],
    request: dict[str, object],
    max_pages: int,
    query_vector: tuple[float, ...] | None = None,
    clip_query_vector: tuple[float, ...] | None = None,
    rerank_scorer: Callable[[str, list[str]], list[float]] | None = None,
    visible_bodies: tuple[str, ...] = _DEFAULT_VISIBLE_BODIES,
    runtime_factory: Callable[
        [tuple[str, ...]], projection_runtime.ActiveProjectionRuntime
    ]
    | None = None,
) -> tuple[
    dict[str, projection_runtime.ActiveProjectionRuntime],
    dict[str, tuple[httpx.Response, ...]],
]:
    # This suite proves canonical application bytes, not wall-clock release timing.
    # The dedicated actual-wire gate owns the fixed completion-class assertions.
    monkeypatch.setattr(
        projection_timing,
        "fixed_public_completion",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setenv("EXOMEM_REST_API_KEY", _REST_KEY)
    if query_vector is None:
        monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    else:
        monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)

        def embed_query(_texts: list[str], *, is_query: bool) -> list[list[float]]:
            assert is_query is True
            return [list(query_vector)]

        monkeypatch.setattr(
            embeddings,
            "embed_texts",
            embed_query,
        )
    if (
        query_vector is not None
        or clip_query_vector is not None
        or rerank_scorer is not None
    ):
        monkeypatch.setattr(readiness, "should_defer", lambda _lane: False)
    if clip_query_vector is None:
        monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    else:
        monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
        monkeypatch.setattr(embeddings, "clip_enabled", lambda: True)
        monkeypatch.setattr(
            embeddings,
            "embed_clip_text",
            lambda _query: list(clip_query_vector),
        )
    if rerank_scorer is None:
        monkeypatch.setenv("EXOMEM_DISABLE_RANKING", "1")
    else:
        monkeypatch.delenv("EXOMEM_DISABLE_RANKING", raising=False)
        monkeypatch.setattr(embeddings, "ranking_enabled", lambda: True)
        monkeypatch.setattr(embeddings, "rerank_pairs", rerank_scorer)
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
        responses: dict[str, tuple[httpx.Response, ...]] = {}
        for name, client in clients.items():
            pages: list[httpx.Response] = []
            continuation: str | None = None
            for _page_number in range(max_pages):
                page_request = dict(request)
                if continuation is not None:
                    page_request["continuation"] = continuation
                response = client.post(
                    "/api/ask_memory",
                    json=page_request,
                    headers={"Authorization": f"Bearer {_REST_KEY}"},
                )
                pages.append(response)
                if response.status_code != 200:
                    break
                continuation = response.json()["data"].get("continuation")
                if continuation is None:
                    break
            responses[name] = tuple(pages)
    finally:
        for client in clients.values():
            client.close()
        projection_runtime._clear_projected_continuations_for_tests()
        writer_lease.reset_managers_for_tests()
    return (
        {name: runtimes_by_root[root] for name, root in roots.items()},
        responses,
    )


def _wire_pair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    hidden_bodies: tuple[str, ...],
    request: dict[str, object],
    query_vector: tuple[float, ...] | None = None,
    clip_query_vector: tuple[float, ...] | None = None,
    rerank_scorer: Callable[[str, list[str]], list[float]] | None = None,
    visible_bodies: tuple[str, ...] = _DEFAULT_VISIBLE_BODIES,
    runtime_factory: Callable[
        [tuple[str, ...]], projection_runtime.ActiveProjectionRuntime
    ]
    | None = None,
) -> tuple[
    dict[str, projection_runtime.ActiveProjectionRuntime],
    dict[str, httpx.Response],
]:
    runtimes, pages = _wire_pages(
        monkeypatch,
        tmp_path,
        hidden_bodies=hidden_bodies,
        request=request,
        max_pages=1,
        query_vector=query_vector,
        clip_query_vector=clip_query_vector,
        rerank_scorer=rerank_scorer,
        visible_bodies=visible_bodies,
        runtime_factory=runtime_factory,
    )
    return runtimes, {name: responses[0] for name, responses in pages.items()}


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


def test_hidden_rows_cannot_change_any_pagination_boundary_cursor_or_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    visible_bodies = tuple("paginationquartz visible page row" for _ in range(5))
    _runtimes, pages = _wire_pages(
        monkeypatch,
        tmp_path,
        hidden_bodies=tuple(
            "paginationquartz " * 32 + f"hidden row {index}" for index in range(80)
        ),
        visible_bodies=visible_bodies,
        request=_request(query="paginationquartz", limit=2),
        max_pages=4,
    )

    assert len(pages["absent"]) == 3
    assert len(pages["present"]) == 3
    for absent, present in zip(pages["absent"], pages["present"], strict=True):
        assert absent.status_code == 200, absent.text
        assert present.status_code == 200, present.text
        assert _canonical_transport_envelope(
            present, transport="http"
        ) == _canonical_transport_envelope(absent, transport="http")

    absent_data = [response.json()["data"] for response in pages["absent"]]
    present_data = [response.json()["data"] for response in pages["present"]]
    assert [
        [hit["path"] for hit in data["hits"]] for data in present_data
    ] == [
        [
            "Knowledge Base/oracle-visible-000.md",
            "Knowledge Base/oracle-visible-001.md",
        ],
        [
            "Knowledge Base/oracle-visible-002.md",
            "Knowledge Base/oracle-visible-003.md",
        ],
        ["Knowledge Base/oracle-visible-004.md"],
    ]
    assert [data.get("continuation") is not None for data in present_data] == [
        True,
        True,
        False,
    ]
    assert [data.get("continuation") for data in present_data] == [
        data.get("continuation") for data in absent_data
    ]


def _projection_vector_runtime(
    hidden_bodies: tuple[str, ...],
) -> projection_runtime.ActiveProjectionRuntime:
    projected = Scope(id="oracle-projected", source="scopes/oracle-projected.yaml")
    hidden = Scope(
        id="oracle-hidden",
        source="scopes/oracle-hidden.yaml",
        default_deny=True,
    )
    policy = Policy(
        fingerprint="d" * 64,
        scopes={projected.id: projected, hidden.id: hidden},
        rules=(
            Rule(
                id="oracle-vector-external",
                source="rules/oracle-vector-external.yaml",
                scope_ids=(projected.id,),
                audience="oracle-external",
                ceiling=3,
                options={"abstract": "vectorprojectionquartz safe abstract"},
            ),
        ),
    )
    items = [
        _projected_item(
            policy,
            path=f"Knowledge Base/oracle-visible-{index:03d}.md",
            scope_id=projected.id,
            body=f"rawvectorquartz visible item {index}",
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

    def vector_for_variant(
        variant: projections.ProjectionVariant,
    ) -> tuple[float, ...]:
        if variant.decision_level == 3:
            return (1.0, 0.0)
        if "/private/" in variant.item_identity:
            return (0.0, 1.0)
        return (-1.0, 0.0)

    return _runtime_from_items(
        policy,
        tuple(items),
        vector_for_variant=vector_for_variant,
    )


@pytest.mark.parametrize(
    ("query_vector", "expected_external_score"),
    (
        ((1.0, 0.0), 1.0),
        ((0.0, 1.0), 0.0),
    ),
)
def test_vector_ranking_uses_only_the_selected_projection_before_top_k(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    query_vector: tuple[float, ...],
    expected_external_score: float,
) -> None:
    runtimes, responses = _wire_pair(
        monkeypatch,
        tmp_path,
        hidden_bodies=("rawvectorquartz hidden source",),
        runtime_factory=_projection_vector_runtime,
        query_vector=query_vector,
        request=_request(query="vector oracle", limit=2, mode="vector"),
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
    vector_index = present_runtime.vector_index
    assert vector_index is not None
    external_hits = vector_index.search_vector(
        external,
        query_vector,
        k=2,
    )
    owner_hits = vector_index.search_vector(
        owner,
        query_vector,
        k=3,
    )
    assert [hit.decision_level for hit in external_hits] == [3, 3]
    assert [hit.score for hit in external_hits] == [
        expected_external_score,
        expected_external_score,
    ]
    assert owner_hits[0].item_identity == (
        "Knowledge Base/private/oracle-hidden-000.md"
    )

    _assert_same_success_envelope(responses)
    content = responses["present"].content
    assert b"vectorprojectionquartz" in content
    assert b"rawvectorquartz" not in content
    assert b"oracle-hidden" not in content


def _rerank_runtime(
    hidden_bodies: tuple[str, ...],
) -> projection_runtime.ActiveProjectionRuntime:
    alpha = Scope(id="oracle-alpha", source="scopes/oracle-alpha.yaml")
    beta = Scope(id="oracle-beta", source="scopes/oracle-beta.yaml")
    hidden = Scope(
        id="oracle-hidden",
        source="scopes/oracle-hidden.yaml",
        default_deny=True,
    )
    policy = Policy(
        fingerprint="c" * 64,
        scopes={alpha.id: alpha, beta.id: beta, hidden.id: hidden},
        rules=tuple(
            Rule(
                id=f"oracle-rerank-{scope.id}",
                source=f"rules/oracle-rerank-{scope.id}.yaml",
                scope_ids=(scope.id,),
                audience="oracle-external",
                ceiling=3,
                options={
                    "abstract": f"rerankprojectionquartz {label} safe abstract"
                },
            )
            for scope, label in ((alpha, "alpha"), (beta, "beta"))
        ),
    )
    items = [
        _projected_item(
            policy,
            path=f"Knowledge Base/oracle-visible-{index:03d}.md",
            scope_id=scope.id,
            body=f"rawrerankquartz visible item {index}",
        )
        for index, scope in enumerate((alpha, beta))
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


def test_reranker_receives_only_selected_projection_text_before_final_top_k(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: list[tuple[str, tuple[str, ...]]] = []

    def rerank_scorer(query: str, passages: list[str]) -> list[float]:
        observed.append((query, tuple(passages)))
        return [
            100.0
            if "oracle-hidden-000" in passage
            else 10.0
            if "beta safe abstract" in passage
            else 1.0
            for passage in passages
        ]

    request = _request(query="rerankprojectionquartz", limit=2)
    request["rerank"] = True
    runtimes, responses = _wire_pair(
        monkeypatch,
        tmp_path,
        hidden_bodies=("rerankprojectionquartz hidden raw source",),
        runtime_factory=_rerank_runtime,
        rerank_scorer=rerank_scorer,
        request=request,
    )

    assert len(observed) == 2
    for query, passages in observed:
        assert query == "rerankprojectionquartz"
        assert len(passages) == 2
        assert all("rerankprojectionquartz" in value for value in passages)
        assert all("rawrerankquartz" not in value for value in passages)
        assert all("oracle-hidden" not in value for value in passages)

    present_runtime = runtimes["present"]
    owner = projection_authorization.build_authorization_map(
        present_runtime.namespace,
        policy=present_runtime.snapshot.policy,
        audience=principal.OWNER_AUDIENCE,
        purpose=None,
        verified_session_grants=(),
        catalog=present_runtime.catalog,
    )
    owner_reranked = present_runtime.reranker.rerank_batch(
        owner,
        "rerankprojectionquartz",
        (
            "Knowledge Base/oracle-visible-000.md",
            "Knowledge Base/oracle-visible-001.md",
            "Knowledge Base/private/oracle-hidden-000.md",
        ),
        scorer=rerank_scorer,
        k=3,
    )
    assert owner_reranked[0].item_identity == (
        "Knowledge Base/private/oracle-hidden-000.md"
    )

    _assert_same_success_envelope(responses)
    data = responses["present"].json()["data"]
    assert [hit["abstract"] for hit in data["hits"]] == [
        "rerankprojectionquartz beta safe abstract",
        "rerankprojectionquartz alpha safe abstract",
    ]
    assert b"rawrerankquartz" not in responses["present"].content
    assert b"oracle-hidden" not in responses["present"].content


def _graph_runtime(
    hidden_bodies: tuple[str, ...],
) -> projection_runtime.ActiveProjectionRuntime:
    visible = Scope(id="oracle-visible", source="scopes/oracle-visible.yaml")
    hidden = Scope(
        id="oracle-hidden",
        source="scopes/oracle-hidden.yaml",
        default_deny=True,
    )
    policy = Policy(
        fingerprint="b" * 64,
        scopes={visible.id: visible, hidden.id: hidden},
    )
    seed = "Knowledge Base/oracle-visible-000.md"
    target = "Knowledge Base/oracle-visible-001.md"
    hidden_identity = "Knowledge Base/private/oracle-hidden-000.md"
    items = [
        _projected_item(
            policy,
            path=seed,
            scope_id=visible.id,
            body="graphoraclequartz visible seed",
        ),
        _projected_item(
            policy,
            path=target,
            scope_id=visible.id,
            body="visible graph target",
        ),
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
    hidden_present = bool(hidden_bodies)

    def graph_edges_for_variant(
        variant: projections.ProjectionVariant,
    ) -> tuple[projected_graph.ProjectionGraphEdge, ...]:
        if variant.decision_level < 6:
            return ()
        targets: tuple[str, ...]
        if variant.item_identity == seed:
            targets = (target, hidden_identity) if hidden_present else (target,)
        elif variant.item_identity == hidden_identity:
            targets = (target,)
        else:
            targets = ()
        return tuple(
            projected_graph.ProjectionGraphEdge(
                source_item_identity=variant.item_identity,
                target_item_identity=edge_target,
                relation_type="supports",
            )
            for edge_target in targets
        )

    return _runtime_from_items(
        policy,
        tuple(items),
        graph_edges_for_variant=graph_edges_for_variant,
    )


def test_hidden_vertices_and_edges_cannot_change_public_graph_fusion_or_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request = _request(query="graphoraclequartz", limit=2)
    request["graph"] = True
    runtimes, responses = _wire_pair(
        monkeypatch,
        tmp_path,
        hidden_bodies=("graphoraclequartz hidden graph source",),
        runtime_factory=_graph_runtime,
        request=request,
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
    graph_index = present_runtime.graph_index
    assert graph_index is not None
    external_graph = graph_index.authorize(external)
    owner_graph = graph_index.authorize(owner)
    seed = "Knowledge Base/oracle-visible-000.md"
    target = "Knowledge Base/oracle-visible-001.md"
    hidden = "Knowledge Base/private/oracle-hidden-000.md"
    assert external_graph.vertices == (seed, target)
    assert external_graph.neighbors(seed) == (target,)
    assert external_graph.in_degree(target) == 1
    assert hidden not in external_graph.vertices
    assert owner_graph.neighbors(seed) == (target, hidden)
    assert owner_graph.in_degree(target) == 2
    assert owner_graph.reachable(seed, hidden) is True

    _assert_same_success_envelope(responses)
    data = responses["present"].json()["data"]
    assert [hit["path"] for hit in data["hits"]] == [seed, target]
    assert data["hits"][1]["signals"] == {
        "graph_hop": True,
        "graph_in_degree": 1,
    }
    assert data["hits"][1]["graph"] == {
        "relation_type": "supports",
        "direction": "outbound",
        "seed": seed,
    }
    assert b"oracle-hidden" not in responses["present"].content


def _image_clip_runtime(
    hidden_bodies: tuple[str, ...],
) -> projection_runtime.ActiveProjectionRuntime:
    visible = Scope(id="oracle-visible", source="scopes/oracle-visible.yaml")
    hidden = Scope(
        id="oracle-hidden",
        source="scopes/oracle-hidden.yaml",
        default_deny=True,
    )
    policy = Policy(
        fingerprint="a" * 64,
        scopes={visible.id: visible, hidden.id: hidden},
    )
    items = [
        _projected_item(
            policy,
            path=f"Knowledge Base/media/oracle-visible-{index:03d}.jpg",
            scope_id=visible.id,
            body=f"visible image {index}",
            media_type="image",
        )
        for index in range(2)
    ]
    items.extend(
        _projected_item(
            policy,
            path=f"Knowledge Base/private/oracle-hidden-{index:03d}.jpg",
            scope_id=hidden.id,
            body=body,
            media_type="image",
        )
        for index, body in enumerate(hidden_bodies)
    )

    def samples_for_variant(
        variant: projections.ProjectionVariant,
    ) -> tuple[projected_retrieval.ProjectionClipSample, ...] | None:
        if not projected_retrieval.clip_variant_applicable(variant):
            return None
        if "/private/" in variant.item_identity:
            vector = (1.0, 0.0)
        elif variant.item_identity.endswith("000.jpg"):
            vector = (0.8, 0.6)
        else:
            vector = (0.6, 0.8)
        return (
            projected_retrieval.ProjectionClipSample(
                frame_timestamp_ms=None,
                vector=vector,
            ),
        )

    return _runtime_from_items(
        policy,
        tuple(items),
        clip_samples_for_variant=samples_for_variant,
    )


def test_hidden_best_image_match_is_authorized_before_the_clip_cap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtimes, responses = _wire_pair(
        monkeypatch,
        tmp_path,
        hidden_bodies=("hidden image pixels",),
        runtime_factory=_image_clip_runtime,
        clip_query_vector=(1.0, 0.0),
        request=_request(query="visual oracle", limit=1, mode="vector"),
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
    clip_index = present_runtime.clip_index
    assert clip_index is not None
    assert clip_index.search_clip(external, (1.0, 0.0), k=1)[0].item_identity == (
        "Knowledge Base/media/oracle-visible-000.jpg"
    )
    assert clip_index.search_clip(owner, (1.0, 0.0), k=1)[0].item_identity == (
        "Knowledge Base/private/oracle-hidden-000.jpg"
    )

    _assert_same_success_envelope(responses)
    data = responses["present"].json()["data"]
    assert [hit["path"] for hit in data["hits"]] == [
        "Knowledge Base/media/oracle-visible-000.jpg"
    ]
    assert data["hits"][0]["signals"] == {"clip_rank": 1, "clip_score": 0.8}
    assert b"oracle-hidden" not in responses["present"].content


def _video_clip_runtime(
    hidden_bodies: tuple[str, ...],
) -> projection_runtime.ActiveProjectionRuntime:
    visible = Scope(id="oracle-visible", source="scopes/oracle-visible.yaml")
    hidden = Scope(
        id="oracle-hidden",
        source="scopes/oracle-hidden.yaml",
        default_deny=True,
    )
    policy = Policy(
        fingerprint="9" * 64,
        scopes={visible.id: visible, hidden.id: hidden},
    )
    items = [
        _projected_item(
            policy,
            path="Knowledge Base/media/oracle-visible-video.mp4",
            scope_id=visible.id,
            body="visible video",
            media_type="video",
        )
    ]
    items.extend(
        _projected_item(
            policy,
            path=f"Knowledge Base/private/oracle-hidden-{index:03d}.mp4",
            scope_id=hidden.id,
            body=body,
            media_type="video",
        )
        for index, body in enumerate(hidden_bodies)
    )

    def samples_for_variant(
        variant: projections.ProjectionVariant,
    ) -> tuple[projected_retrieval.ProjectionClipSample, ...] | None:
        if not projected_retrieval.clip_variant_applicable(variant):
            return None
        if "/private/" in variant.item_identity:
            timestamp_vectors = ((2_000, (1.0, 0.0)),)
        else:
            timestamp_vectors = (
                (1_000, (0.8, 0.6)),
                (4_500, (0.95, 0.3122498999)),
                (8_500, (0.95, 0.3122498999)),
            )
        return tuple(
            projected_retrieval.ProjectionClipSample(
                frame_timestamp_ms=timestamp_ms,
                vector=vector,
            )
            for timestamp_ms, vector in timestamp_vectors
        )

    return _runtime_from_items(
        policy,
        tuple(items),
        clip_samples_for_variant=samples_for_variant,
    )


def test_hidden_video_match_cannot_change_the_earliest_best_visible_frame(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtimes, responses = _wire_pair(
        monkeypatch,
        tmp_path,
        hidden_bodies=("hidden video pixels",),
        runtime_factory=_video_clip_runtime,
        clip_query_vector=(1.0, 0.0),
        request=_request(query="video scene", limit=1, mode="vector"),
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
    clip_index = present_runtime.clip_index
    assert clip_index is not None
    external_hit = clip_index.search_clip(external, (1.0, 0.0), k=1)[0]
    owner_hit = clip_index.search_clip(owner, (1.0, 0.0), k=1)[0]
    assert external_hit.item_identity == (
        "Knowledge Base/media/oracle-visible-video.mp4"
    )
    assert external_hit.clip_frame_timestamp_ms == 4_500
    assert owner_hit.item_identity == "Knowledge Base/private/oracle-hidden-000.mp4"

    _assert_same_success_envelope(responses)
    data = responses["present"].json()["data"]
    assert data["hits"][0]["clip_match_at"] == "0:04"
    assert b"oracle-hidden" not in responses["present"].content


def _companion_only_clip_runtime(
    hidden_bodies: tuple[str, ...],
) -> projection_runtime.ActiveProjectionRuntime:
    companion = Scope(id="oracle-companion", source="scopes/oracle-companion.yaml")
    hidden = Scope(
        id="oracle-hidden",
        source="scopes/oracle-hidden.yaml",
        default_deny=True,
    )
    policy = Policy(
        fingerprint="8" * 64,
        scopes={companion.id: companion, hidden.id: hidden},
        rules=(
            Rule(
                id="oracle-companion-external",
                source="rules/oracle-companion-external.yaml",
                scope_ids=(companion.id,),
                audience="oracle-external",
                ceiling=3,
                options={
                    "abstract": "companionvisualquartz safe image description"
                },
            ),
        ),
    )
    items = [
        _projected_item(
            policy,
            path="Knowledge Base/media/lower-image.jpg.md",
            scope_id=companion.id,
            body="raw companion metadata",
        )
    ]
    items.extend(
        _projected_item(
            policy,
            path=f"Knowledge Base/private/oracle-hidden-{index:03d}.jpg",
            scope_id=hidden.id,
            body=body,
            media_type="image",
        )
        for index, body in enumerate(hidden_bodies)
    )

    def samples_for_variant(
        variant: projections.ProjectionVariant,
    ) -> tuple[projected_retrieval.ProjectionClipSample, ...] | None:
        if not projected_retrieval.clip_variant_applicable(variant):
            return None
        return (
            projected_retrieval.ProjectionClipSample(
                frame_timestamp_ms=None,
                vector=(1.0, 0.0),
            ),
        )

    return _runtime_from_items(
        policy,
        tuple(items),
        clip_samples_for_variant=samples_for_variant,
    )


def test_lower_media_uses_only_its_authorized_textual_companion_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtimes, responses = _wire_pair(
        monkeypatch,
        tmp_path,
        hidden_bodies=("hidden image pixels",),
        runtime_factory=_companion_only_clip_runtime,
        clip_query_vector=(1.0, 0.0),
        request=_request(query="companionvisualquartz", limit=1),
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
    selected = present_runtime.catalog.select(external)
    assert len(selected) == 1
    assert selected[0].decision_level == 3
    assert not projected_retrieval.clip_variant_applicable(selected[0])

    _assert_same_success_envelope(responses)
    data = responses["present"].json()["data"]
    assert data["hits"][0]["abstract"] == (
        "companionvisualquartz safe image description"
    )
    assert data.get("warming") is None
    assert b"raw companion metadata" not in responses["present"].content
    assert b"oracle-hidden" not in responses["present"].content
