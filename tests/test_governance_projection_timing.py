"""Fixed, repository-owned timing classes for governed projected requests."""

from __future__ import annotations

import importlib
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from exomem import writer_lease
from exomem.governance import egress, projection_runtime, projections


def _timing_module():
    try:
        return importlib.import_module("exomem.governance.projection_timing")
    except ImportError:
        pytest.fail("governed projection timing registry is unavailable")


def _release_manifest() -> dict[str, object]:
    return {
        "schema": "exomem.governed-projection-timing-release/v1",
        "hidden_corpus_wire_delta_ms": 25,
        "sample_count_per_condition": 200,
        "bootstrap_resamples": 2_000,
        "bootstrap_seed": 20_260_822,
        "hardware_runtime_profile": "github-hosted-ubuntu-latest-x64-python3.13",
        "request_classes": [
            {
                "name": "projected-find-keyword-v1",
                "padding_ms": 250,
                "deadline_ms": 300,
            }
        ],
        "routes": [
            "keyword",
            "bm25",
            "vector",
            "rerank",
            "clip",
            "graph",
            "error",
            "pagination",
        ],
        "capacity": {
            "catalog_items": projections.MAX_GOVERNED_CATALOG_ITEMS,
            "searchable_bytes_per_item": (
                projections.MAX_GOVERNED_SEARCH_BYTES_PER_ITEM
            ),
            "graph_edges": projections.MAX_GOVERNED_GRAPH_EDGES,
        },
        "scheduler_tolerance_subtracted": False,
        "padding_selected_before_sampling": True,
    }


def test_keyword_request_class_is_closed_and_repository_owned() -> None:
    timing = _timing_module()

    request_class = timing.request_class_for_find(
        mode="keyword",
        scope="vault",
        graph=False,
        rerank=False,
    )

    assert request_class.name == "projected-find-keyword-v1"
    assert request_class.padding_ms == 250
    assert request_class.deadline_ms == 300
    assert request_class.max_hidden_delta_ms == projections.MAX_HIDDEN_CORPUS_WIRE_DELTA_MS
    assert (
        request_class.max_hidden_delta_ratio
        == projections.MAX_HIDDEN_CORPUS_WIRE_DELTA_RATIO
    )
    with pytest.raises(TypeError):
        timing.PUBLIC_REQUEST_CLASSES["caller-selected"] = request_class


def test_completion_uses_the_fixed_target_without_observation_adaptation() -> None:
    timing = _timing_module()
    request_class = timing.PUBLIC_REQUEST_CLASSES["projected-find-keyword-v1"]
    readings = iter((10.0, 10.1, 10.25))
    sleeps: list[float] = []

    timing.complete_public_request(
        request_class,
        started_at=next(readings),
        clock=lambda: next(readings),
        sleeper=sleeps.append,
    )

    assert sleeps == [pytest.approx(0.15)]


def test_completion_refuses_a_missed_fixed_deadline() -> None:
    timing = _timing_module()
    request_class = timing.PUBLIC_REQUEST_CLASSES["projected-find-keyword-v1"]

    with pytest.raises(timing.ProjectedRequestDeadlineExceeded):
        timing.complete_public_request(
            request_class,
            started_at=20.0,
            clock=lambda: 20.301,
            sleeper=lambda _seconds: pytest.fail("deadline overrun slept"),
        )


def test_completion_refuses_padding_sleep_that_misses_deadline() -> None:
    timing = _timing_module()
    request_class = timing.PUBLIC_REQUEST_CLASSES["projected-find-keyword-v1"]
    readings = iter((30.240, 30.301))

    with pytest.raises(timing.ProjectedRequestDeadlineExceeded):
        timing.complete_public_request(
            request_class,
            started_at=30.0,
            clock=lambda: next(readings),
            sleeper=lambda _seconds: None,
        )


def test_shared_command_dispatch_owns_projected_completion_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    timing = _timing_module()
    events: list[str] = []

    def leaf(
        vault_root,
        query: str = "",
        *,
        mode: str = "keyword",
        scope: str = "vault",
        graph: bool = False,
        rerank: bool | None = False,
    ):
        assert vault_root == tmp_path
        events.append("leaf")
        if query == "fail":
            raise RuntimeError("projected failure")
        return {"hits": [], "query": query}

    command = SimpleNamespace(
        name="ask_memory",
        leaf=leaf,
        read_only=True,
        response_detail=None,
        path_roles=(),
    )

    class Manager:
        def invoke(self, current, injected, kwargs, **_metadata):
            return current.leaf(*injected, **kwargs)

    @contextmanager
    def fixed_completion(request_class):
        assert request_class.name == "projected-find-keyword-v1"
        events.append("enter")
        try:
            yield
        finally:
            events.append("complete")

    monkeypatch.setattr(writer_lease, "get_manager", lambda: Manager())
    monkeypatch.setattr(egress, "is_vault_root", lambda _value: False)
    monkeypatch.setattr(
        projection_runtime,
        "has_preactivated_projection_runtime",
        lambda _root: True,
    )
    monkeypatch.setattr(timing, "fixed_public_completion", fixed_completion)

    result = writer_lease.invoke_command(
        command,
        tmp_path,
        query="projection-only term",
    )

    assert result == {"hits": [], "query": "projection-only term"}
    assert events == ["enter", "leaf", "complete"]

    events.clear()
    with pytest.raises(RuntimeError, match="projected failure"):
        writer_lease.invoke_command(command, tmp_path, query="fail")
    assert events == ["enter", "leaf", "complete"]


def test_unactivated_command_skips_projected_class_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    def leaf(
        vault_root,
        query: str = "",
        *,
        mode: str = "hybrid",
        scope: str = "kb",
        graph: bool = True,
        rerank: bool | None = None,
    ):
        assert vault_root == tmp_path
        return {"hits": [], "query": query}

    command = SimpleNamespace(
        name="ask_memory",
        leaf=leaf,
        read_only=True,
        response_detail=None,
        path_roles=(),
    )

    class Manager:
        def invoke(self, current, injected, kwargs, **_metadata):
            return current.leaf(*injected, **kwargs)

    monkeypatch.setattr(writer_lease, "get_manager", lambda: Manager())
    monkeypatch.setattr(egress, "is_vault_root", lambda _value: False)
    monkeypatch.setattr(
        projection_runtime,
        "has_preactivated_projection_runtime",
        lambda _root: False,
    )

    assert writer_lease.invoke_command(
        command,
        tmp_path,
        query="ordinary request",
    ) == {"hits": [], "query": "ordinary request"}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda manifest: manifest.__setitem__(
            "hidden_corpus_wire_delta_ms", 26
        ),
        lambda manifest: manifest.__setitem__(
            "sample_count_per_condition", 199
        ),
        lambda manifest: manifest["capacity"].__setitem__(
            "catalog_items", projections.MAX_GOVERNED_CATALOG_ITEMS - 1
        ),
        lambda manifest: manifest.__setitem__(
            "routes",
            [route for route in manifest["routes"] if route != "graph"],
        ),
        lambda manifest: manifest.__setitem__(
            "scheduler_tolerance_subtracted", True
        ),
        lambda manifest: manifest.__setitem__(
            "padding_selected_before_sampling", False
        ),
        lambda manifest: manifest["request_classes"][0].__setitem__(
            "padding_ms", 300
        ),
        lambda manifest: manifest["request_classes"][0].__setitem__(
            "deadline_ms", 301
        ),
    ],
)
def test_release_manifest_cannot_self_waive_timing_contract(mutate) -> None:
    timing = _timing_module()
    manifest = deepcopy(_release_manifest())
    mutate(manifest)

    with pytest.raises(timing.ProjectedRequestTimingUnavailable):
        timing.validate_release_manifest(manifest)


def test_bootstrap_wire_oracle_enforces_absolute_and_relative_bounds() -> None:
    timing = _timing_module()
    manifest = timing.validate_release_manifest(_release_manifest())
    absent = tuple(250.0 for _ in range(200))
    equivalent = tuple(250.0 for _ in range(200))
    displaced = tuple(280.0 for _ in range(200))

    accepted = timing.evaluate_wire_differential(
        manifest,
        request_class_name="projected-find-keyword-v1",
        hidden_present_ms=equivalent,
        physically_absent_ms=absent,
    )
    refused = timing.evaluate_wire_differential(
        manifest,
        request_class_name="projected-find-keyword-v1",
        hidden_present_ms=displaced,
        physically_absent_ms=absent,
    )

    assert accepted.passed is True
    assert accepted.median_delta_upper_99_ms == 0.0
    assert accepted.p95_delta_upper_99_ms == 0.0
    assert refused.passed is False
    assert refused.p95_delta_upper_99_ms > 25.0


def test_wire_oracle_derives_deadline_misses_from_actual_samples() -> None:
    timing = _timing_module()
    manifest = timing.validate_release_manifest(_release_manifest())

    report = timing.evaluate_wire_differential(
        manifest,
        request_class_name="projected-find-keyword-v1",
        hidden_present_ms=tuple(301.0 for _ in range(200)),
        physically_absent_ms=tuple(301.0 for _ in range(200)),
    )

    assert report.deadline_misses == 400
    assert report.passed is False


def test_wire_oracle_rejects_directly_constructed_manifest() -> None:
    timing = _timing_module()

    with pytest.raises(timing.ProjectedRequestTimingUnavailable):
        manifest = timing.TimingReleaseManifest(
            hidden_corpus_wire_delta_ms=25,
            sample_count_per_condition=1,
            bootstrap_resamples=1,
            bootstrap_seed=0,
            hardware_runtime_profile="caller-selected",
            request_class_names=("projected-find-keyword-v1",),
            routes=frozenset(),
            catalog_items=0,
            searchable_bytes_per_item=0,
            graph_edges=0,
        )
        timing.evaluate_wire_differential(
            manifest,
            request_class_name="projected-find-keyword-v1",
            hidden_present_ms=(1.0,),
            physically_absent_ms=(1.0,),
        )
