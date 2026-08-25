"""Actual-wire hidden-present/absent release gate for projected retrieval.

The lean suite collects this module but does not execute the 200-pair timing run.
CI selects exactly one mandatory route per matrix job through the closed route
name.  Each job uses the checked manifest, fixed completion class, and all
three predeclared corpus capacities; there is no sample-count or capacity knob.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from governance_projection_support import verified_namespace
from starlette.testclient import TestClient

from exomem import embeddings, server, writer_lease
from exomem.governance import (
    principal,
    projected_graph,
    projected_retrieval,
    projection_runtime,
    projection_store,
    projection_timing,
    projections,
    schema_v4,
)
from exomem.governance.policy import Policy, Scope
from exomem.server_runtime import ServerRuntime

_MANIFEST_PATH = (
    Path(__file__).parents[1]
    / "benchmarks"
    / "governance"
    / "projected-wire-release-v1.json"
)
_VECTOR_MANIFEST_PATH = (
    Path(__file__).parents[1]
    / "benchmarks"
    / "governance"
    / "projected-wire-vector-cpu-release-v1.json"
)
_ROUTE_ENV = "EXOMEM_GOVERNANCE_TIMING_ROUTE"
_PROFILE_ENV = "EXOMEM_GOVERNANCE_TIMING_PROFILE"
_REST_KEY = "governance-wire-release-key"
_VECTOR_EXTRACTOR = "projected-text-v1"
_GRAPH_EXTRACTOR = "projected-graph-v1"
_GRAPH_MODEL = "projected-graph-v1"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _maximum_query() -> str:
    prefix = "wire-visible "
    dense_unique_terms = "".join(f"q{index:04x} " for index in range(1_000))
    query = (prefix + dense_unique_terms)[:4_096]
    return query.ljust(4_096, " ")


def _item(
    policy: Policy,
    *,
    path: str,
    scope_id: str,
    body: str,
    media_type: str | None = None,
) -> projection_store.ProjectionItemVariants:
    fields = {"body": body}
    if media_type is not None:
        fields["media_type"] = media_type
    content_hash = _digest(body)
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
            full_search_fields=fields,
        ),
    )


def _l6_variant(
    item: projection_store.ProjectionItemVariants,
) -> projections.ProjectionVariant:
    return next(variant for variant in item.variants if variant.decision_level == 6)


def _graph_measurements(
    items: tuple[projection_store.ProjectionItemVariants, ...],
    *,
    visible_count: int,
    graph_edge_count: int,
    omit_item_identity: str | None = None,
) -> tuple[projected_graph.ProjectionGraphMeasurement, ...]:
    variants = tuple(_l6_variant(item) for item in items)
    visible_source, visible_target = variants[:2]
    edges_by_source: dict[str, tuple[projected_graph.ProjectionGraphEdge, ...]] = {
        visible_source.item_identity: (
            projected_graph.ProjectionGraphEdge(
                visible_source.item_identity,
                visible_target.item_identity,
                "supports",
            ),
        )
    }
    remaining = graph_edge_count - 1
    if remaining > 0:
        hidden_source = variants[visible_count]
        targets = variants[visible_count:]
        hidden_edges = tuple(
            projected_graph.ProjectionGraphEdge(
                hidden_source.item_identity,
                targets[index % len(targets)].item_identity,
                f"private-relation-{index // len(targets):05d}",
            )
            for index in range(remaining)
        )
        edges_by_source[hidden_source.item_identity] = hidden_edges
    return tuple(
        projected_graph.ProjectionGraphMeasurement(
            measurement_key=projections.MeasurementKey(
                projection_variant_id=variant.projection_variant_id,
                lane="graph",
                extractor_version=_GRAPH_EXTRACTOR,
                model_version=_GRAPH_MODEL,
            ),
            edges=edges_by_source.get(variant.item_identity, ()),
        )
        for variant in variants
        if variant.item_identity != omit_item_identity
    )


def _vector_measurements(
    items: tuple[projection_store.ProjectionItemVariants, ...],
    *,
    visible_count: int,
) -> tuple[projected_retrieval.ProjectionVectorMeasurement, ...]:
    measurements: list[projected_retrieval.ProjectionVectorMeasurement] = []
    for index, item in enumerate(items[:visible_count]):
        variant = _l6_variant(item)
        vector = [0.0] * 768
        vector[index % len(vector)] = 1.0
        measurements.append(
            projected_retrieval.ProjectionVectorMeasurement(
                measurement_key=projections.MeasurementKey(
                    projection_variant_id=variant.projection_variant_id,
                    lane="vector",
                    extractor_version=_VECTOR_EXTRACTOR,
                    model_version=embeddings.MODEL_NAME,
                ),
                vector=tuple(vector),
            )
        )
    return tuple(measurements)


def _active_runtime(
    *,
    visible_count: int,
    visible_body: str,
    hidden_count: int,
    graph_edge_count: int,
    omit_last_hidden_graph_measurement: bool = False,
    vector_enabled: bool = False,
    policy_fingerprint: str = "f" * 64,
) -> projection_runtime.ActiveProjectionRuntime:
    visible = Scope(id="wire-visible", source="scopes/wire-visible.yaml")
    hidden = Scope(
        id="wire-hidden",
        source="scopes/wire-hidden.yaml",
        default_deny=True,
    )
    policy = Policy(
        fingerprint=policy_fingerprint,
        scopes={visible.id: visible, hidden.id: hidden},
    )
    if visible_count < 2:
        raise ValueError("wire fixture requires at least two visible items")
    items: list[projection_store.ProjectionItemVariants] = []
    for index in range(visible_count):
        items.append(
            _item(
                policy,
                path=f"Knowledge Base/wire-visible-{index:03d}.md",
                scope_id=visible.id,
                body=f"{visible_body} item {index:03d}",
                media_type="image" if index == 1 else None,
            )
        )
    for index in range(hidden_count):
        body = f"wire-visible private wire-hidden-body-{index:05d}"
        if (
            hidden_count + visible_count == projections.MAX_GOVERNED_CATALOG_ITEMS
            and index == 0
        ):
            fragment = "wire-visible wire-hidden-body-maximum "
            repeated = fragment * (
                projections.MAX_GOVERNED_SEARCH_BYTES_PER_ITEM // len(fragment) + 1
            )
            body = repeated[: projections.MAX_GOVERNED_SEARCH_BYTES_PER_ITEM]
            assert len(body.encode("utf-8")) == (
                projections.MAX_GOVERNED_SEARCH_BYTES_PER_ITEM
            )
        items.append(
            _item(
                policy,
                path=f"Knowledge Base/private/wire-hidden-{index:05d}.md",
                scope_id=hidden.id,
                body=body,
            )
        )
    item_tuple = tuple(items)
    projections.require_supported_capacity(catalog_items=len(item_tuple))
    key = projections.ProjectionNamespaceKey(
        policy.fingerprint,
        projections.PROJECTOR_SCHEMA_VERSION,
        max(1, hidden_count + visible_count),
    )
    namespace = verified_namespace(key, item_tuple)
    active = schema_v4.VerifiedActiveGovernanceState(
        logical_vault_id="wire-vault",
        activation_store_id="wire-store",
        activation_epoch=1,
        activation_state_digest=namespace.active_state_digest,
        policy_generation_id="wire-policy",
        policy_fingerprint=key.policy_fingerprint,
        projector_schema_version=key.projector_schema_version,
        catalog_generation=key.catalog_generation,
        projection_namespace_id=key.namespace_id,
    )
    measurements = _graph_measurements(
        item_tuple,
        visible_count=visible_count,
        graph_edge_count=graph_edge_count,
        omit_item_identity=(
            item_tuple[-1].item_identity
            if omit_last_hidden_graph_measurement and hidden_count
            else None
        ),
    )
    measured_graph_edge_count = sum(
        len(measurement.edges) for measurement in measurements
    )
    graph_index = projected_graph.ProjectedGraphIndex(
        namespace,
        measurements,
        extractor_version=_GRAPH_EXTRACTOR,
        model_version=_GRAPH_MODEL,
    )
    graph_root = projection_store.ProjectionMeasurementRoot(
        namespace_key=key,
        family_id=projection_store.projection_measurement_family_id(
            key,
            lane="graph",
            extractor_version=_GRAPH_EXTRACTOR,
            model_version=_GRAPH_MODEL,
        ),
        lane="graph",
        extractor_version=_GRAPH_EXTRACTOR,
        model_version=_GRAPH_MODEL,
        measurement_count=len(measurements),
        vector_dimension=None,
        graph_edge_count=measured_graph_edge_count,
        rows_digest=_digest(f"graph:{hidden_count}:{graph_edge_count}"),
    )
    vector_index = None
    vector_root = None
    if vector_enabled:
        vector_measurements = _vector_measurements(
            item_tuple,
            visible_count=visible_count,
        )
        vector_index = projected_retrieval.ProjectedVectorIndex(
            namespace,
            vector_measurements,
            extractor_version=_VECTOR_EXTRACTOR,
            model_version=embeddings.MODEL_NAME,
        )
        vector_root = projection_store.ProjectionMeasurementRoot(
            namespace_key=key,
            family_id=projection_store.projection_measurement_family_id(
                key,
                lane="vector",
                extractor_version=_VECTOR_EXTRACTOR,
                model_version=embeddings.MODEL_NAME,
            ),
            lane="vector",
            extractor_version=_VECTOR_EXTRACTOR,
            model_version=embeddings.MODEL_NAME,
            measurement_count=len(vector_measurements),
            vector_dimension=768,
            graph_edge_count=0,
            rows_digest=_digest(f"vector-visible:{visible_count}"),
        )
    snapshot = schema_v4.ActivePolicySnapshot(
        active=active,
        policy=policy,
        source_documents=(),
        catalog_descriptor=projection_store.catalog_descriptor_bytes(key, item_tuple),
        projection_namespace_evidence=b"wire-release-fixture",
    )
    return projection_runtime.ActiveProjectionRuntime(
        snapshot,
        namespace,
        tuple(root for root in (vector_root, graph_root) if root is not None),
        vector_index=vector_index,
        graph_index=graph_index,
    )


def _route_body(
    route: str,
    *,
    continuation: str | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "query": "wire-visible",
        "scope": "vault",
        "mode": "hybrid",
        "graph": False,
        "rerank": False,
        "include_timings": True,
    }
    if route == "keyword":
        body["mode"] = "keyword"
    elif route in {"vector-hard-off", "vector-live"}:
        body["mode"] = "vector"
    elif route == "rerank-hard-off":
        body["rerank"] = True
    elif route == "clip-hard-off":
        pass
    elif route == "graph":
        body["graph"] = True
    elif route == "graph-rerank-hard-off":
        body["graph"] = True
        body["rerank"] = True
    elif route == "max-query":
        body["query"] = _maximum_query()
    elif route == "max-limit":
        body["limit"] = 100
        body["detail"] = "full"
    elif route == "max-shape":
        body["query"] = _maximum_query()
        body["limit"] = 100
        body["detail"] = "full"
        body["graph"] = True
        body["rerank"] = True
    elif route == "hidden-index-missing":
        body["graph"] = True
    elif route == "pagination":
        body["mode"] = "keyword"
        body["limit"] = 1
        if continuation is not None:
            body["continuation"] = continuation
    return body


def _client_for_root(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
) -> TestClient:
    runtime = ServerRuntime(
        vault_root=root,
        source_schema=SimpleNamespace(source_types={}),
        project_keys_hint="",
        base_url="",
    )
    monkeypatch.setattr(
        server,
        "initialize_runtime",
        lambda **_kwargs: runtime,
    )
    return TestClient(server.build_server(require_auth=False).http_app())


def _canonical_response_digest(response) -> str:  # noqa: ANN001
    value = {"status": response.status_code, "body": response.json()}
    return hashlib.sha256(projections.canonical_jcs(value)).hexdigest()


def _assert_route_response(response, route: str) -> None:  # noqa: ANN001
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["timings_suppressed"] == {"status": "governed_projection"}
    if route in {"max-limit", "max-shape"}:
        assert len(data["hits"]) == 100
    if route == "pagination":
        assert [hit["path"] for hit in data["hits"]] == [
            "Knowledge Base/wire-visible-001.md"
        ]
        assert "continuation" not in data


def test_projected_find_continuation_returns_the_next_authorized_page(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_RANKING", "1")
    runtime = _active_runtime(
        visible_count=3,
        visible_body="wire-visible projected pagination",
        hidden_count=0,
        graph_edge_count=1,
    )
    who = principal.RequestPrincipal(
        audience_id="wire-external-principal",
        surface="rest",
        resolved=True,
        issuer_family="wire-release",
    )
    projection_runtime._clear_projected_continuations_for_tests()

    first = projection_runtime.find_projected_hits(
        tmp_path,
        runtime,
        query="wire-visible",
        limit=1,
        scope="vault",
        mode="keyword",
        graph=False,
        rerank=False,
        principal=who,
        purpose=None,
    )
    assert [hit.path for hit in first.hits] == [
        "Knowledge Base/wire-visible-000.md"
    ]
    assert first.continuation is not None
    assert first.continuation.startswith("pc1.")

    second = projection_runtime.find_projected_hits(
        tmp_path,
        runtime,
        query="wire-visible",
        limit=1,
        scope="vault",
        mode="keyword",
        graph=False,
        rerank=False,
        principal=who,
        purpose=None,
        continuation=first.continuation,
    )
    assert [hit.path for hit in second.hits] == [
        "Knowledge Base/wire-visible-001.md"
    ]
    assert second.continuation is not None
    assert second.continuation != first.continuation


def test_exhausted_first_page_does_not_materialize_continuation_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_RANKING", "1")
    runtime = _active_runtime(
        visible_count=2,
        visible_body="wire-visible projected exhaustive page",
        hidden_count=1,
        graph_edge_count=2,
    )
    who = principal.RequestPrincipal(
        audience_id="wire-external-principal",
        surface="rest",
        resolved=True,
        issuer_family="wire-release",
    )

    def unexpected_digest(*_args, **_kwargs):
        raise AssertionError("exhausted pages must not build continuation state")

    monkeypatch.setattr(
        projection_runtime,
        "_authorization_map_digest",
        unexpected_digest,
    )
    monkeypatch.setattr(
        projection_runtime,
        "_visible_authorization_digest",
        unexpected_digest,
    )
    monkeypatch.setattr(
        projection_runtime,
        "_visible_snapshot_digest",
        unexpected_digest,
    )

    result = projection_runtime.find_projected_hits(
        tmp_path,
        runtime,
        query="wire-visible",
        limit=2,
        scope="vault",
        mode="hybrid",
        graph=True,
        rerank=True,
        principal=who,
        purpose=None,
    )

    assert len(result.hits) == 2
    assert result.continuation is None


def test_projected_continuation_is_hidden_independent_bound_and_replayable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_RANKING", "1")
    absent = _active_runtime(
        visible_count=3,
        visible_body="wire-visible projected pagination",
        hidden_count=0,
        graph_edge_count=1,
    )
    hidden = _active_runtime(
        visible_count=3,
        visible_body="wire-visible projected pagination",
        hidden_count=1,
        graph_edge_count=2,
    )
    who = principal.RequestPrincipal(
        audience_id="wire-external-principal",
        surface="rest",
        resolved=True,
        issuer_family="wire-release",
    )
    call = {
        "query": "wire-visible",
        "limit": 1,
        "scope": "vault",
        "mode": "keyword",
        "graph": False,
        "rerank": False,
        "principal": who,
        "purpose": None,
    }
    projection_runtime._clear_projected_continuations_for_tests()

    first_absent = projection_runtime.find_projected_hits(
        tmp_path / "absent", absent, **call
    )
    first_hidden = projection_runtime.find_projected_hits(
        tmp_path / "hidden", hidden, **call
    )
    assert first_absent.continuation == first_hidden.continuation
    assert first_absent.continuation is not None

    second = projection_runtime.find_projected_hits(
        tmp_path / "absent",
        hidden,
        continuation=first_absent.continuation,
        **call,
    )
    replay = projection_runtime.find_projected_hits(
        tmp_path / "absent",
        hidden,
        continuation=first_absent.continuation,
        **call,
    )
    assert replay == second
    assert [hit.path for hit in second.hits] == [
        "Knowledge Base/wire-visible-001.md"
    ]

    with pytest.raises(
        projection_runtime.ProjectedContinuationUnavailable,
        match="^INVALID_CONTINUATION: continuation is invalid or expired$",
    ):
        projection_runtime.find_projected_hits(
            tmp_path / "absent",
            hidden,
            continuation=first_absent.continuation,
            **{**call, "query": "different-query"},
        )
    with pytest.raises(
        projection_runtime.ProjectedContinuationUnavailable,
        match="^INVALID_CONTINUATION: continuation is invalid or expired$",
    ):
        projection_runtime.find_projected_hits(
            tmp_path / "absent",
            hidden,
            continuation=first_absent.continuation,
            **{
                **call,
                "principal": principal.RequestPrincipal(
                    audience_id="different-principal",
                    surface="rest",
                    resolved=True,
                ),
            },
        )
    for changed in (
        {**call, "purpose": "audit"},
        {
            **call,
            "principal": principal.RequestPrincipal(
                audience_id=who.audience_id,
                surface="mcp",
                resolved=True,
                authorization_session_id="different-session",
            ),
        },
    ):
        with pytest.raises(
            projection_runtime.ProjectedContinuationUnavailable,
            match="^INVALID_CONTINUATION: continuation is invalid or expired$",
        ):
            projection_runtime.find_projected_hits(
                tmp_path / "absent",
                hidden,
                continuation=first_absent.continuation,
                **changed,
            )

    projection_runtime._clear_projected_continuations_for_tests()
    with pytest.raises(
        projection_runtime.ProjectedContinuationUnavailable,
        match="^INVALID_CONTINUATION: continuation is invalid or expired$",
    ):
        projection_runtime.find_projected_hits(
            tmp_path / "absent",
            hidden,
            continuation=first_absent.continuation,
            **call,
        )


def test_projected_continuation_refuses_visible_projection_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_RANKING", "1")
    original = _active_runtime(
        visible_count=3,
        visible_body="wire-visible original",
        hidden_count=0,
        graph_edge_count=1,
    )
    changed = _active_runtime(
        visible_count=3,
        visible_body="wire-visible changed",
        hidden_count=0,
        graph_edge_count=1,
    )
    who = principal.RequestPrincipal(
        audience_id="wire-external-principal",
        surface="rest",
        resolved=True,
    )
    call = {
        "query": "wire-visible",
        "limit": 1,
        "scope": "vault",
        "mode": "keyword",
        "graph": False,
        "rerank": False,
        "principal": who,
        "purpose": None,
    }
    projection_runtime._clear_projected_continuations_for_tests()
    first = projection_runtime.find_projected_hits(tmp_path, original, **call)
    assert first.continuation is not None

    with pytest.raises(
        projection_runtime.ProjectedContinuationUnavailable,
        match="^INVALID_CONTINUATION: continuation is invalid or expired$",
    ):
        projection_runtime.find_projected_hits(
            tmp_path,
            changed,
            continuation=first.continuation,
            **call,
        )


def test_new_first_page_refreshes_a_same_token_after_policy_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_RANKING", "1")
    old = _active_runtime(
        visible_count=3,
        visible_body="wire-visible unchanged",
        hidden_count=0,
        graph_edge_count=1,
        policy_fingerprint="e" * 64,
    )
    current = _active_runtime(
        visible_count=3,
        visible_body="wire-visible unchanged",
        hidden_count=0,
        graph_edge_count=1,
        policy_fingerprint="f" * 64,
    )
    call = {
        "query": "wire-visible",
        "limit": 1,
        "scope": "vault",
        "mode": "keyword",
        "graph": False,
        "rerank": False,
        "principal": principal.RequestPrincipal(
            audience_id="wire-external-principal",
            surface="rest",
            resolved=True,
        ),
        "purpose": None,
    }
    projection_runtime._clear_projected_continuations_for_tests()
    old_first = projection_runtime.find_projected_hits(tmp_path, old, **call)
    current_first = projection_runtime.find_projected_hits(
        tmp_path, current, **call
    )
    assert old_first.continuation == current_first.continuation
    assert current_first.continuation is not None

    second = projection_runtime.find_projected_hits(
        tmp_path,
        current,
        continuation=current_first.continuation,
        **call,
    )
    assert [hit.path for hit in second.hits] == [
        "Knowledge Base/wire-visible-001.md"
    ]
    with pytest.raises(
        projection_runtime.ProjectedContinuationUnavailable,
        match="^INVALID_CONTINUATION: continuation is invalid or expired$",
    ):
        projection_runtime.find_projected_hits(
            tmp_path,
            old,
            continuation=old_first.continuation,
            **call,
        )


def test_projected_continuation_expiry_is_fixed_and_capacity_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_RANKING", "1")
    runtime = _active_runtime(
        visible_count=3,
        visible_body="wire-visible projected pagination",
        hidden_count=0,
        graph_edge_count=1,
    )
    who = principal.RequestPrincipal(
        audience_id="wire-external-principal",
        surface="rest",
        resolved=True,
    )
    call = {
        "limit": 1,
        "scope": "vault",
        "mode": "keyword",
        "graph": False,
        "rerank": False,
        "principal": who,
        "purpose": None,
    }
    now = [100.0]
    monkeypatch.setattr(projection_runtime.time, "monotonic", lambda: now[0])
    projection_runtime._clear_projected_continuations_for_tests()
    first = projection_runtime.find_projected_hits(
        tmp_path, runtime, query="wire-visible", **call
    )
    assert first.continuation is not None

    now[0] = 110.0
    projection_runtime.find_projected_hits(
        tmp_path,
        runtime,
        query="wire-visible",
        continuation=first.continuation,
        **call,
    )
    now[0] = 1_000.0
    with pytest.raises(
        projection_runtime.ProjectedContinuationUnavailable,
        match="^INVALID_CONTINUATION: continuation is invalid or expired$",
    ):
        projection_runtime.find_projected_hits(
            tmp_path,
            runtime,
            query="wire-visible",
            continuation=first.continuation,
            **call,
        )

    projection_runtime._clear_projected_continuations_for_tests()
    now[0] = 2_000.0
    monkeypatch.setattr(projection_runtime, "_MAX_PROJECTED_CONTINUATIONS", 1)
    projection_runtime.find_projected_hits(
        tmp_path, runtime, query="wire-visible", **call
    )
    with pytest.raises(
        projection_runtime.ProjectedContinuationUnavailable,
        match="^INVALID_CONTINUATION: continuation is invalid or expired$",
    ):
        projection_runtime.find_projected_hits(
            tmp_path, runtime, query="projected", **call
        )


def test_projected_hidden_missing_graph_measurement_is_absence_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_RANKING", "1")
    runtime = _active_runtime(
        visible_count=2,
        visible_body="wire-visible projected retrieval",
        hidden_count=1,
        graph_edge_count=2,
        omit_last_hidden_graph_measurement=True,
    )

    external = projection_runtime.find_projected_hits(
        tmp_path,
        runtime,
        query="wire-visible",
        limit=2,
        scope="vault",
        mode="hybrid",
        graph=True,
        rerank=False,
        principal=principal.RequestPrincipal(
            audience_id="wire-external-principal",
            surface="rest",
            resolved=True,
        ),
        purpose=None,
    )
    assert "graph" not in external.warming_components
    assert all("private" not in hit.path for hit in external.hits)

    owner = projection_runtime.find_projected_hits(
        tmp_path,
        runtime,
        query="wire-visible",
        limit=2,
        scope="vault",
        mode="hybrid",
        graph=True,
        rerank=False,
        principal=principal.owner_principal(surface="library"),
        purpose=None,
    )
    assert "graph" in owner.warming_components


@pytest.mark.governance_timing_release
@pytest.mark.timeout(540)
def test_projected_hidden_corpus_actual_wire_characterization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Characterize the closed release routes over the actual REST wire."""
    route = os.environ.get(_ROUTE_ENV)
    if route is None:
        pytest.skip("dedicated governance actual-wire route job only")
    selected_profile = os.environ.get(
        _PROFILE_ENV,
        projection_timing.MODEL_RUNTIME_PROFILE,
    )
    manifest_path = (
        _VECTOR_MANIFEST_PATH
        if selected_profile == projection_timing.VECTOR_CPU_MODEL_RUNTIME_PROFILE
        else _MANIFEST_PATH
    )
    manifest = projection_timing.validate_release_manifest(
        json.loads(manifest_path.read_text(encoding="utf-8"))
    )
    assert manifest.model_runtime_profile == selected_profile
    if route not in manifest.routes:
        pytest.fail(f"unregistered governance timing route: {route!r}")

    monkeypatch.setenv("EXOMEM_REST_API_KEY", _REST_KEY)
    if selected_profile == projection_timing.VECTOR_CPU_MODEL_RUNTIME_PROFILE:
        monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
        monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
        monkeypatch.setenv("EXOMEM_DISABLE_RANKING", "1")
        monkeypatch.setenv("EXOMEM_DEVICE", "cpu")
        monkeypatch.setenv("EXOMEM_EMBED_BACKEND", "torch")
        monkeypatch.delenv("EXOMEM_EMBED_DEVICE", raising=False)
        monkeypatch.delenv("EXOMEM_TORCH_DEVICE", raising=False)
        embeddings.get_model()
    else:
        monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
        monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
        monkeypatch.setenv("EXOMEM_DISABLE_RANKING", "1")
    assert (
        projection_runtime.projected_serving_release_profile()
        == manifest.model_runtime_profile
    )
    writer_state = tmp_path / "writer-state"
    writer_state.mkdir()
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(writer_state))
    for name in (
        "EXOMEM_AUTH_SESSION_KEYRING_FILE",
        "EXOMEM_AUTH_SESSION_CONTROL_FILE",
        "EXOMEM_CF_ACCESS_TEAM_DOMAIN",
        "EXOMEM_CF_ACCESS_AUD",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(writer_lease, "start_server_lifecycle", lambda: None)
    writer_lease.reset_managers_for_tests()
    monkeypatch.setattr(
        principal,
        "resolve_rest_principal",
        lambda _scope: principal.RequestPrincipal(
            audience_id="wire-external-principal",
            surface="rest",
            resolved=True,
            issuer_family="wire-release",
        ),
    )

    roots = {
        "absent": tmp_path / "absent",
        "one": tmp_path / "one-hidden",
        "maximum": tmp_path / "maximum-hidden",
    }
    for root in roots.values():
        (root / "Knowledge Base").mkdir(parents=True)
    visible_count = 100 if route in {"max-limit", "max-shape"} else 2
    visible_body = (
        _maximum_query()
        if route in {"max-query", "max-shape"}
        else "wire-visible projected retrieval"
    )
    runtimes = {
        roots["absent"]: _active_runtime(
            visible_count=visible_count,
            visible_body=visible_body,
            hidden_count=0,
            graph_edge_count=1,
            vector_enabled=(
                selected_profile
                == projection_timing.VECTOR_CPU_MODEL_RUNTIME_PROFILE
            ),
        ),
        roots["one"]: _active_runtime(
            visible_count=visible_count,
            visible_body=visible_body,
            hidden_count=1,
            graph_edge_count=2,
            omit_last_hidden_graph_measurement=(route == "hidden-index-missing"),
            vector_enabled=(
                selected_profile
                == projection_timing.VECTOR_CPU_MODEL_RUNTIME_PROFILE
            ),
        ),
        roots["maximum"]: _active_runtime(
            visible_count=visible_count,
            visible_body=visible_body,
            hidden_count=(
                projections.MAX_GOVERNED_CATALOG_ITEMS - visible_count
            ),
            graph_edge_count=projections.MAX_GOVERNED_GRAPH_EDGES,
            omit_last_hidden_graph_measurement=(route == "hidden-index-missing"),
            vector_enabled=(
                selected_profile
                == projection_timing.VECTOR_CPU_MODEL_RUNTIME_PROFILE
            ),
        ),
    }
    assert all(
        projection_runtime._runtime_supports_release_profile(
            runtime,
            selected_profile,
        )
        for runtime in runtimes.values()
    )
    # Production settles one immutable runtime before publication. This wire
    # fixture holds three replica states in one process, so settle their
    # combined object graph before any client becomes observable.
    projection_runtime._stabilize_projection_runtime(runtimes[roots["maximum"]])
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

    clients = {
        name: _client_for_root(monkeypatch, root)
        for name, root in roots.items()
    }
    schedule = projection_timing.release_sample_schedule(manifest, route=route)
    observations: list[projection_timing.WireObservation] = []
    continuations: dict[str, str] = {}
    caplog.set_level(logging.INFO)
    try:
        for client_name, client in clients.items():
            warmup = client.post(
                "/api/ask_memory",
                json=_route_body(route),
                headers={"Authorization": f"Bearer {_REST_KEY}"},
            )
            if route == "pagination":
                assert warmup.status_code == 200, warmup.text
                continuation = warmup.json()["data"].get("continuation")
                assert isinstance(continuation, str)
                continuations[client_name] = continuation
            else:
                _assert_route_response(warmup, route)
        if route == "pagination":
            assert len(set(continuations.values())) == 1
        for sample in schedule:
            client_name = (
                "absent"
                if not sample.hidden_present or sample.capacity == "zero"
                else sample.capacity
            )
            started = time.perf_counter()
            response = clients[client_name].post(
                "/api/ask_memory",
                json=_route_body(
                    route,
                    continuation=continuations.get(client_name),
                ),
                headers={"Authorization": f"Bearer {_REST_KEY}"},
            )
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            _assert_route_response(response, route)
            observations.append(
                projection_timing.WireObservation(
                    sample=sample,
                    elapsed_ms=elapsed_ms,
                    canonical_envelope_sha256=_canonical_response_digest(response),
                )
            )
    finally:
        for client in clients.values():
            client.close()
        writer_lease.reset_managers_for_tests()

    report = projection_timing.evaluate_wire_route(
        manifest,
        route=route,
        observations=tuple(observations),
    )
    assert report.passed, report
    assert "wire-hidden-" not in caplog.text
    assert _REST_KEY not in caplog.text
