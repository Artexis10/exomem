"""Fixed, repository-owned timing classes for governed projected requests."""

from __future__ import annotations

import importlib
import json
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import writer_lease
from exomem.governance import egress, projection_runtime, projections

_CHECKED_RELEASE_MANIFEST = (
    Path(__file__).parents[1]
    / "benchmarks"
    / "governance"
    / "projected-wire-release-v1.json"
)


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
        "model_runtime_profile": "models-hard-off-v1",
        "request_classes": [
            {
                "name": "projected-find-v1",
                "padding_ms": 250,
                "deadline_ms": 300,
                "max_query_chars": 4_096,
                "max_limit": 100,
            }
        ],
        "routes": [
            "keyword",
            "bm25",
            "vector-hard-off",
            "rerank-hard-off",
            "clip-hard-off",
            "graph",
            "graph-rerank-hard-off",
            "max-query",
            "max-limit",
            "max-shape",
            "hidden-index-missing",
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


def test_checked_release_manifest_is_the_validated_nonwaivable_contract() -> None:
    timing = _timing_module()

    checked = json.loads(_CHECKED_RELEASE_MANIFEST.read_text(encoding="utf-8"))

    assert checked == _release_manifest()
    assert timing.validate_release_manifest(checked).routes == frozenset(
        {
            "keyword",
            "bm25",
            "vector-hard-off",
            "rerank-hard-off",
            "clip-hard-off",
            "graph",
            "graph-rerank-hard-off",
            "max-query",
            "max-limit",
            "max-shape",
            "hidden-index-missing",
            "pagination",
        }
    )


def test_keyword_request_class_is_closed_and_repository_owned() -> None:
    timing = _timing_module()

    request_class = timing.request_class_for_find(
        mode="keyword",
        scope="vault",
        graph=False,
        rerank=False,
    )

    assert request_class.name == "projected-find-v1"
    assert request_class.padding_ms == 250
    assert request_class.deadline_ms == 300
    assert request_class.max_query_chars == 4_096
    assert request_class.max_limit == 100
    assert request_class.max_hidden_delta_ms == projections.MAX_HIDDEN_CORPUS_WIRE_DELTA_MS
    assert (
        request_class.max_hidden_delta_ratio
        == projections.MAX_HIDDEN_CORPUS_WIRE_DELTA_RATIO
    )
    with pytest.raises(TypeError):
        timing.PUBLIC_REQUEST_CLASSES["caller-selected"] = request_class


def test_public_default_kb_scope_uses_the_registered_completion_class() -> None:
    timing = _timing_module()

    assert timing.request_class_for_find(
        mode="hybrid",
        scope="kb",
        graph=True,
        rerank=None,
    ) is timing.PUBLIC_REQUEST_CLASSES["projected-find-v1"]


@pytest.mark.parametrize(
    ("mode", "graph", "rerank"),
    [
        ("keyword", False, False),
        ("hybrid", False, False),
        ("hybrid", True, False),
        ("hybrid", False, True),
        ("vector", False, False),
    ],
)
def test_every_projected_find_lane_uses_the_same_fixed_completion_class(
    mode: str,
    graph: bool,
    rerank: bool,
) -> None:
    timing = _timing_module()

    request_class = timing.request_class_for_find(
        mode=mode,
        scope="vault",
        graph=graph,
        rerank=rerank,
    )

    assert request_class is timing.PUBLIC_REQUEST_CLASSES["projected-find-v1"]


def test_completion_uses_the_fixed_target_without_observation_adaptation() -> None:
    timing = _timing_module()
    request_class = timing.PUBLIC_REQUEST_CLASSES["projected-find-v1"]
    readings = iter((10.0, 10.1, 10.25))
    sleeps: list[float] = []

    timing.complete_public_request(
        request_class,
        started_at=next(readings),
        clock=lambda: next(readings),
        sleeper=sleeps.append,
    )

    assert sleeps == [pytest.approx(0.15)]


def test_completion_retries_an_early_padding_wake() -> None:
    timing = _timing_module()
    request_class = timing.PUBLIC_REQUEST_CLASSES["projected-find-v1"]
    readings = iter((10.1, 10.2, 10.25))
    sleeps: list[float] = []

    timing.complete_public_request(
        request_class,
        started_at=10.0,
        clock=lambda: next(readings),
        sleeper=sleeps.append,
    )

    assert sleeps == [pytest.approx(0.15), pytest.approx(0.05)]


def test_completion_refuses_a_missed_fixed_deadline() -> None:
    timing = _timing_module()
    request_class = timing.PUBLIC_REQUEST_CLASSES["projected-find-v1"]

    with pytest.raises(timing.ProjectedRequestDeadlineExceeded):
        timing.complete_public_request(
            request_class,
            started_at=20.0,
            clock=lambda: 20.301,
            sleeper=lambda _seconds: pytest.fail("deadline overrun slept"),
        )


def test_completion_refuses_padding_sleep_that_misses_deadline() -> None:
    timing = _timing_module()
    request_class = timing.PUBLIC_REQUEST_CLASSES["projected-find-v1"]
    readings = iter((30.240, 30.301))

    with pytest.raises(timing.ProjectedRequestDeadlineExceeded):
        timing.complete_public_request(
            request_class,
            started_at=30.0,
            clock=lambda: next(readings),
            sleeper=lambda _seconds: None,
        )


@pytest.mark.parametrize("reading", [float("nan"), float("inf"), float("-inf")])
def test_completion_refuses_nonfinite_clock(reading: float) -> None:
    timing = _timing_module()
    request_class = timing.PUBLIC_REQUEST_CLASSES["projected-find-v1"]

    with pytest.raises(timing.ProjectedRequestTimingUnavailable):
        timing.complete_public_request(
            request_class,
            started_at=10.0,
            clock=lambda: reading,
            sleeper=lambda _seconds: pytest.fail("invalid clock slept"),
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
        assert request_class.name == "projected-find-v1"
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


def test_projected_completion_wraps_the_default_shape_and_its_error_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    timing = _timing_module()
    events: list[str] = []

    def leaf(
        vault_root,
        query: str = "",
        *,
        mode: str = "hybrid",
        scope: str = "kb",
        graph: bool = True,
        rerank: bool | None = None,
    ):
        del vault_root, query, mode, scope, graph, rerank
        events.append("leaf")
        raise RuntimeError("projected failure")

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
        assert request_class is timing.PUBLIC_REQUEST_CLASSES[
            "projected-find-v1"
        ]
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

    with pytest.raises(RuntimeError, match="projected failure"):
        writer_lease.invoke_command(command, tmp_path, query="projection-only term")
    assert events == ["enter", "leaf", "complete"]


def test_unavailable_projected_boundary_still_uses_fixed_completion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    timing = _timing_module()
    events: list[str] = []

    def leaf(vault_root, query: str = ""):
        assert vault_root == tmp_path
        assert query == "projection-only term"
        events.append("leaf")
        raise projection_runtime.ProjectionRuntimeUnavailable(
            "governed projected retrieval is unavailable"
        )

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
    def fixed_completion(request_class, *, started_at=None):
        assert request_class.name == "projected-find-v1"
        assert started_at is not None
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
        lambda _root: False,
    )
    monkeypatch.setattr(
        projection_runtime,
        "requires_fixed_projected_completion",
        lambda _root: False,
    )
    monkeypatch.setattr(
        projection_runtime,
        "classify_projected_completion_boundary",
        lambda _root: (events.append("classify"), True)[1],
    )
    monkeypatch.setattr(timing, "fixed_public_completion", fixed_completion)

    with pytest.raises(
        projection_runtime.ProjectionRuntimeUnavailable,
        match="governed projected retrieval is unavailable",
    ):
        writer_lease.invoke_command(
            command,
            tmp_path,
            query="projection-only term",
        )
    assert events == ["classify", "enter", "leaf", "complete"]


def test_hidden_error_and_pagination_evidence_opens_the_exact_release_profile() -> None:
    assert projection_runtime._PROJECTED_SERVING_RELEASE_ACCEPTED is True


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
    monkeypatch.setattr(
        projection_runtime,
        "requires_fixed_projected_completion",
        lambda _root: False,
    )
    monkeypatch.setattr(
        projection_runtime,
        "classify_projected_completion_boundary",
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
        lambda manifest: manifest.__setitem__(
            "model_runtime_profile", "models-enabled"
        ),
        lambda manifest: manifest["request_classes"][0].__setitem__(
            "max_query_chars", 4_097
        ),
        lambda manifest: manifest["request_classes"][0].__setitem__(
            "max_limit", 101
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
        request_class_name="projected-find-v1",
        hidden_present_ms=equivalent,
        physically_absent_ms=absent,
    )
    refused = timing.evaluate_wire_differential(
        manifest,
        request_class_name="projected-find-v1",
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
        request_class_name="projected-find-v1",
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
            model_runtime_profile="models-enabled",
            request_class_names=("projected-find-v1",),
            routes=frozenset(),
            catalog_items=0,
            searchable_bytes_per_item=0,
            graph_edges=0,
        )
        timing.evaluate_wire_differential(
            manifest,
            request_class_name="projected-find-v1",
            hidden_present_ms=(1.0,),
            physically_absent_ms=(1.0,),
        )


def test_release_schedule_is_predeclared_paired_and_covers_every_capacity() -> None:
    timing = _timing_module()
    manifest = timing.validate_release_manifest(_release_manifest())

    schedule = timing.release_sample_schedule(manifest, route="graph")

    assert len(schedule) == 400
    assert {sample.route for sample in schedule} == {"graph"}
    assert {sample.capacity for sample in schedule} == {"zero", "one", "maximum"}
    assert sum(sample.hidden_present for sample in schedule) == 200
    assert sum(not sample.hidden_present for sample in schedule) == 200
    assert sum(
        sample.hidden_present and sample.capacity == "one" for sample in schedule
    ) == 100
    assert sum(
        sample.hidden_present and sample.capacity == "maximum" for sample in schedule
    ) == 100
    for offset in range(0, len(schedule), 2):
        left, right = schedule[offset : offset + 2]
        assert left.pair_id == right.pair_id
        assert left.hidden_present is not right.hidden_present
        present = left if left.hidden_present else right
        absent = right if left.hidden_present else left
        assert present.capacity in {"one", "maximum"}
        assert absent.capacity == "zero"


def test_route_release_gate_requires_exact_schedule_and_byte_equal_pairs() -> None:
    timing = _timing_module()
    manifest = timing.validate_release_manifest(_release_manifest())
    schedule = timing.release_sample_schedule(manifest, route="keyword")
    observations = tuple(
        timing.WireObservation(
            sample=sample,
            elapsed_ms=250.0,
            canonical_envelope_sha256="a" * 64,
        )
        for sample in schedule
    )

    report = timing.evaluate_wire_route(
        manifest,
        route="keyword",
        observations=observations,
    )

    assert report.route == "keyword"
    assert report.envelope_pairs_equal is True
    assert report.timing.passed is True

    with pytest.raises(timing.ProjectedRequestTimingUnavailable):
        timing.evaluate_wire_route(
            manifest,
            route="keyword",
            observations=observations[:-1],
        )

    mismatched = list(observations)
    mismatched[1] = timing.WireObservation(
        sample=mismatched[1].sample,
        elapsed_ms=mismatched[1].elapsed_ms,
        canonical_envelope_sha256="b" * 64,
    )
    refused = timing.evaluate_wire_route(
        manifest,
        route="keyword",
        observations=tuple(mismatched),
    )
    assert refused.envelope_pairs_equal is False
    assert refused.passed is False


def test_route_release_gate_derives_deadline_failure_from_wire_observation() -> None:
    timing = _timing_module()
    manifest = timing.validate_release_manifest(_release_manifest())
    schedule = timing.release_sample_schedule(manifest, route="pagination")
    observations = [
        timing.WireObservation(
            sample=sample,
            elapsed_ms=250.0,
            canonical_envelope_sha256="c" * 64,
        )
        for sample in schedule
    ]
    observations[0] = timing.WireObservation(
        sample=observations[0].sample,
        elapsed_ms=301.0,
        canonical_envelope_sha256="c" * 64,
    )

    report = timing.evaluate_wire_route(
        manifest,
        route="pagination",
        observations=tuple(observations),
    )

    assert report.timing.deadline_misses == 1
    assert report.passed is False
