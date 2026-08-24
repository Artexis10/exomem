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

from exomem import server, writer_lease
from exomem.governance import (
    principal,
    projected_graph,
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
_ROUTE_ENV = "EXOMEM_GOVERNANCE_TIMING_ROUTE"
_REST_KEY = "governance-wire-release-key"
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
    )


def _active_runtime(
    *,
    visible_count: int,
    visible_body: str,
    hidden_count: int,
    graph_edge_count: int,
) -> projection_runtime.ActiveProjectionRuntime:
    visible = Scope(id="wire-visible", source="scopes/wire-visible.yaml")
    hidden = Scope(
        id="wire-hidden",
        source="scopes/wire-hidden.yaml",
        default_deny=True,
    )
    policy = Policy(
        fingerprint="f" * 64,
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
        graph_edge_count=graph_edge_count,
        rows_digest=_digest(f"graph:{hidden_count}:{graph_edge_count}"),
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
        (graph_root,),
        graph_index=graph_index,
    )


def _route_body(route: str) -> dict[str, object]:
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
    elif route == "vector-hard-off":
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
    elif route == "invalid-limit":
        body["limit"] = 101
    elif route == "minimum-limit":
        body["limit"] = 1
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
    expected_status = 400 if route == "invalid-limit" else 200
    assert response.status_code == expected_status, response.text
    if response.status_code == 200:
        data = response.json()["data"]
        assert data["timings_suppressed"] == {
            "status": "governed_projection"
        }
        if route in {"max-limit", "max-shape"}:
            assert len(data["hits"]) == 100


@pytest.mark.governance_timing_release
@pytest.mark.timeout(240)
def test_projected_hidden_corpus_actual_wire_characterization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Characterize implemented routes; release acceptance stays closed.

    Invalid input and a minimum limit do not certify hidden-state failures or
    real cursor pagination, so this harness must not be treated as that proof.
    """
    route = os.environ.get(_ROUTE_ENV)
    if route is None:
        pytest.skip("dedicated governance actual-wire route job only")
    manifest = projection_timing.validate_release_manifest(
        json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    )
    assert manifest.model_runtime_profile == "models-hard-off-v1"
    if route not in manifest.routes:
        pytest.fail(f"unregistered governance timing route: {route!r}")

    monkeypatch.setenv("EXOMEM_REST_API_KEY", _REST_KEY)
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
        ),
        roots["one"]: _active_runtime(
            visible_count=visible_count,
            visible_body=visible_body,
            hidden_count=1,
            graph_edge_count=2,
        ),
        roots["maximum"]: _active_runtime(
            visible_count=visible_count,
            visible_body=visible_body,
            hidden_count=(
                projections.MAX_GOVERNED_CATALOG_ITEMS - visible_count
            ),
            graph_edge_count=projections.MAX_GOVERNED_GRAPH_EDGES,
        ),
    }
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
    caplog.set_level(logging.INFO)
    try:
        for client in clients.values():
            warmup = client.post(
                "/api/ask_memory",
                json=_route_body(route),
                headers={"Authorization": f"Bearer {_REST_KEY}"},
            )
            _assert_route_response(warmup, route)
        for sample in schedule:
            client_name = (
                "absent"
                if not sample.hidden_present or sample.capacity == "zero"
                else sample.capacity
            )
            started = time.perf_counter()
            response = clients[client_name].post(
                "/api/ask_memory",
                json=_route_body(route),
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
