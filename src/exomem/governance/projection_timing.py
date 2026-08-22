"""Repository-owned completion classes for governed projected requests."""

from __future__ import annotations

import inspect
import math
import random
import statistics
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType

from . import projections


class ProjectedRequestTimingUnavailable(RuntimeError):
    """The request does not match a registered public timing class."""


class ProjectedRequestDeadlineExceeded(ProjectedRequestTimingUnavailable):
    """The fixed class could not complete under its repository deadline."""


@dataclass(frozen=True, slots=True)
class PublicRequestClass:
    name: str
    padding_ms: int
    deadline_ms: int
    max_hidden_delta_ms: int
    max_hidden_delta_ratio: float

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or not self.name
            or type(self.padding_ms) is not int
            or type(self.deadline_ms) is not int
            or self.padding_ms <= 0
            or self.deadline_ms != self.padding_ms
            or self.max_hidden_delta_ms
            != projections.MAX_HIDDEN_CORPUS_WIRE_DELTA_MS
            or self.max_hidden_delta_ratio
            != projections.MAX_HIDDEN_CORPUS_WIRE_DELTA_RATIO
        ):
            raise ProjectedRequestTimingUnavailable(
                "governed projected request timing is unavailable"
            )


_PROJECTED_FIND_KEYWORD_V1 = PublicRequestClass(
    name="projected-find-keyword-v1",
    padding_ms=250,
    deadline_ms=250,
    max_hidden_delta_ms=projections.MAX_HIDDEN_CORPUS_WIRE_DELTA_MS,
    max_hidden_delta_ratio=projections.MAX_HIDDEN_CORPUS_WIRE_DELTA_RATIO,
)

PUBLIC_REQUEST_CLASSES: Mapping[str, PublicRequestClass] = MappingProxyType(
    {_PROJECTED_FIND_KEYWORD_V1.name: _PROJECTED_FIND_KEYWORD_V1}
)

_RELEASE_MANIFEST_SCHEMA = "exomem.governed-projection-timing-release/v1"
_RELEASE_MANIFEST_KEYS = frozenset(
    {
        "schema",
        "hidden_corpus_wire_delta_ms",
        "sample_count_per_condition",
        "bootstrap_resamples",
        "bootstrap_seed",
        "hardware_runtime_profile",
        "request_classes",
        "routes",
        "capacity",
        "scheduler_tolerance_subtracted",
        "padding_selected_before_sampling",
    }
)
_REQUEST_CLASS_KEYS = frozenset({"name", "padding_ms", "deadline_ms"})
_CAPACITY_KEYS = frozenset(
    {"catalog_items", "searchable_bytes_per_item", "graph_edges"}
)
_REQUIRED_ROUTES = frozenset(
    {"keyword", "bm25", "vector", "rerank", "clip", "graph", "error", "pagination"}
)
_HARDWARE_RUNTIME_PROFILE = "github-hosted-ubuntu-latest-x64-python3.13"
_MIN_SAMPLE_COUNT_PER_CONDITION = 200
_MIN_BOOTSTRAP_RESAMPLES = 2_000


@dataclass(frozen=True, slots=True)
class TimingReleaseManifest:
    """Validated, non-waivable inputs for one actual-wire release gate."""

    hidden_corpus_wire_delta_ms: int
    sample_count_per_condition: int
    bootstrap_resamples: int
    bootstrap_seed: int
    hardware_runtime_profile: str
    request_class_names: tuple[str, ...]
    routes: frozenset[str]
    catalog_items: int
    searchable_bytes_per_item: int
    graph_edges: int


@dataclass(frozen=True, slots=True)
class WireDifferentialReport:
    """Deterministic bootstrap result for one registered public request class."""

    request_class_name: str
    sample_count_per_condition: int
    physically_absent_median_ms: float
    physically_absent_p95_ms: float
    hidden_present_median_ms: float
    hidden_present_p95_ms: float
    median_delta_upper_99_ms: float
    p95_delta_upper_99_ms: float
    effective_delta_ceiling_ms: float
    deadline_misses: int
    passed: bool


def _closed_mapping(
    value: object,
    keys: frozenset[str],
    name: str,
) -> Mapping[str, object]:
    del name
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ProjectedRequestTimingUnavailable(
            "governed projected request timing is unavailable"
        )
    if any(not isinstance(key, str) for key in value):
        raise ProjectedRequestTimingUnavailable(
            "governed projected request timing is unavailable"
        )
    return value


def _integer_at_least(value: object, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ProjectedRequestTimingUnavailable(
            "governed projected request timing is unavailable"
        )
    return value


def validate_release_manifest(value: object) -> TimingReleaseManifest:
    """Validate the closed repository release gate without caller waivers."""

    manifest = _closed_mapping(value, _RELEASE_MANIFEST_KEYS, "release manifest")
    if manifest["schema"] != _RELEASE_MANIFEST_SCHEMA:
        raise ProjectedRequestTimingUnavailable(
            "governed projected request timing is unavailable"
        )

    delta_ms = _integer_at_least(manifest["hidden_corpus_wire_delta_ms"], 1)
    if delta_ms > projections.MAX_HIDDEN_CORPUS_WIRE_DELTA_MS:
        raise ProjectedRequestTimingUnavailable(
            "governed projected request timing is unavailable"
        )
    sample_count = _integer_at_least(
        manifest["sample_count_per_condition"],
        _MIN_SAMPLE_COUNT_PER_CONDITION,
    )
    bootstrap_resamples = _integer_at_least(
        manifest["bootstrap_resamples"],
        _MIN_BOOTSTRAP_RESAMPLES,
    )
    bootstrap_seed = _integer_at_least(manifest["bootstrap_seed"], 0)
    if manifest["hardware_runtime_profile"] != _HARDWARE_RUNTIME_PROFILE:
        raise ProjectedRequestTimingUnavailable(
            "governed projected request timing is unavailable"
        )
    if manifest["scheduler_tolerance_subtracted"] is not False or manifest[
        "padding_selected_before_sampling"
    ] is not True:
        raise ProjectedRequestTimingUnavailable(
            "governed projected request timing is unavailable"
        )

    request_classes = manifest["request_classes"]
    if not isinstance(request_classes, Sequence) or isinstance(
        request_classes, (str, bytes, bytearray)
    ):
        raise ProjectedRequestTimingUnavailable(
            "governed projected request timing is unavailable"
        )
    names: list[str] = []
    for raw_class in request_classes:
        declared = _closed_mapping(raw_class, _REQUEST_CLASS_KEYS, "request class")
        name = declared["name"]
        registered = PUBLIC_REQUEST_CLASSES.get(name) if isinstance(name, str) else None
        if (
            registered is None
            or type(declared["padding_ms"]) is not int
            or declared["padding_ms"] != registered.padding_ms
            or type(declared["deadline_ms"]) is not int
            or declared["deadline_ms"] != registered.deadline_ms
        ):
            raise ProjectedRequestTimingUnavailable(
                "governed projected request timing is unavailable"
            )
        names.append(name)
    if len(names) != len(set(names)) or set(names) != set(PUBLIC_REQUEST_CLASSES):
        raise ProjectedRequestTimingUnavailable(
            "governed projected request timing is unavailable"
        )

    routes = manifest["routes"]
    if not isinstance(routes, Sequence) or isinstance(routes, (str, bytes, bytearray)):
        raise ProjectedRequestTimingUnavailable(
            "governed projected request timing is unavailable"
        )
    if (
        any(not isinstance(route, str) for route in routes)
        or len(routes) != len(set(routes))
        or frozenset(routes) != _REQUIRED_ROUTES
    ):
        raise ProjectedRequestTimingUnavailable(
            "governed projected request timing is unavailable"
        )

    capacity = _closed_mapping(manifest["capacity"], _CAPACITY_KEYS, "capacity")
    expected_capacity = {
        "catalog_items": projections.MAX_GOVERNED_CATALOG_ITEMS,
        "searchable_bytes_per_item": projections.MAX_GOVERNED_SEARCH_BYTES_PER_ITEM,
        "graph_edges": projections.MAX_GOVERNED_GRAPH_EDGES,
    }
    if any(
        type(capacity[key]) is not int or capacity[key] != expected
        for key, expected in expected_capacity.items()
    ):
        raise ProjectedRequestTimingUnavailable(
            "governed projected request timing is unavailable"
        )

    return TimingReleaseManifest(
        hidden_corpus_wire_delta_ms=delta_ms,
        sample_count_per_condition=sample_count,
        bootstrap_resamples=bootstrap_resamples,
        bootstrap_seed=bootstrap_seed,
        hardware_runtime_profile=_HARDWARE_RUNTIME_PROFILE,
        request_class_names=tuple(names),
        routes=frozenset(routes),
        catalog_items=expected_capacity["catalog_items"],
        searchable_bytes_per_item=expected_capacity["searchable_bytes_per_item"],
        graph_edges=expected_capacity["graph_edges"],
    )


def _wire_samples(value: object, expected_count: int) -> tuple[float, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ProjectedRequestTimingUnavailable(
            "governed projected request timing is unavailable"
        )
    samples: list[float] = []
    for sample in value:
        if isinstance(sample, bool) or not isinstance(sample, (int, float)):
            raise ProjectedRequestTimingUnavailable(
                "governed projected request timing is unavailable"
            )
        measured = float(sample)
        if not math.isfinite(measured) or measured < 0:
            raise ProjectedRequestTimingUnavailable(
                "governed projected request timing is unavailable"
            )
        samples.append(measured)
    if len(samples) != expected_count:
        raise ProjectedRequestTimingUnavailable(
            "governed projected request timing is unavailable"
        )
    return tuple(samples)


def _nearest_rank(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return float(ordered[index])


def evaluate_wire_differential(
    manifest: TimingReleaseManifest,
    *,
    request_class_name: str,
    hidden_present_ms: Sequence[float],
    physically_absent_ms: Sequence[float],
    deadline_misses: int,
) -> WireDifferentialReport:
    """Evaluate hidden-present versus absent actual-wire distributions."""

    if not isinstance(manifest, TimingReleaseManifest):
        raise ProjectedRequestTimingUnavailable(
            "governed projected request timing is unavailable"
        )
    request_class = PUBLIC_REQUEST_CLASSES.get(request_class_name)
    if request_class is None or request_class_name not in manifest.request_class_names:
        raise ProjectedRequestTimingUnavailable(
            "governed projected request timing is unavailable"
        )
    if type(deadline_misses) is not int or deadline_misses < 0:
        raise ProjectedRequestTimingUnavailable(
            "governed projected request timing is unavailable"
        )
    present = _wire_samples(hidden_present_ms, manifest.sample_count_per_condition)
    absent = _wire_samples(physically_absent_ms, manifest.sample_count_per_condition)

    present_median = float(statistics.median(present))
    absent_median = float(statistics.median(absent))
    present_p95 = _nearest_rank(present, 0.95)
    absent_p95 = _nearest_rank(absent, 0.95)
    rng = random.Random(manifest.bootstrap_seed)
    median_deltas: list[float] = []
    p95_deltas: list[float] = []
    count = manifest.sample_count_per_condition
    for _ in range(manifest.bootstrap_resamples):
        present_resample = tuple(present[rng.randrange(count)] for _ in range(count))
        absent_resample = tuple(absent[rng.randrange(count)] for _ in range(count))
        median_deltas.append(
            abs(
                float(statistics.median(present_resample))
                - float(statistics.median(absent_resample))
            )
        )
        p95_deltas.append(
            abs(
                _nearest_rank(present_resample, 0.95)
                - _nearest_rank(absent_resample, 0.95)
            )
        )
    median_upper = _nearest_rank(median_deltas, 0.99)
    p95_upper = _nearest_rank(p95_deltas, 0.99)
    ceiling = min(
        float(manifest.hidden_corpus_wire_delta_ms),
        float(request_class.max_hidden_delta_ms),
        request_class.max_hidden_delta_ratio * absent_p95,
    )
    return WireDifferentialReport(
        request_class_name=request_class_name,
        sample_count_per_condition=count,
        physically_absent_median_ms=absent_median,
        physically_absent_p95_ms=absent_p95,
        hidden_present_median_ms=present_median,
        hidden_present_p95_ms=present_p95,
        median_delta_upper_99_ms=median_upper,
        p95_delta_upper_99_ms=p95_upper,
        effective_delta_ceiling_ms=ceiling,
        deadline_misses=deadline_misses,
        passed=deadline_misses == 0 and median_upper <= ceiling and p95_upper <= ceiling,
    )


def request_class_for_find(
    *,
    mode: str,
    scope: str,
    graph: bool,
    rerank: bool | None,
) -> PublicRequestClass:
    """Select only the one currently implemented public projected shape."""

    if (
        mode != "keyword"
        or scope != "vault"
        or graph is not False
        or rerank is not False
    ):
        raise ProjectedRequestTimingUnavailable(
            "governed projected request timing is unavailable"
        )
    return _PROJECTED_FIND_KEYWORD_V1


def request_class_for_command(
    command: object,
    injected: tuple[object, ...],
    kwargs: Mapping[str, object],
) -> PublicRequestClass | None:
    """Resolve the fixed class from one shared-dispatch command invocation."""

    if getattr(command, "name", None) not in {"ask_memory", "find"}:
        return None
    leaf = getattr(command, "leaf", None)
    if not callable(leaf):
        raise ProjectedRequestTimingUnavailable(
            "governed projected request timing is unavailable"
        )
    try:
        bound = inspect.signature(leaf).bind_partial(*injected, **kwargs)
        bound.apply_defaults()
        return request_class_for_find(
            mode=bound.arguments["mode"],
            scope=bound.arguments["scope"],
            graph=bound.arguments["graph"],
            rerank=bound.arguments["rerank"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ProjectedRequestTimingUnavailable(
            "governed projected request timing is unavailable"
        ) from error


def complete_public_request(
    request_class: PublicRequestClass,
    *,
    started_at: float,
    clock: Callable[[], float] = time.perf_counter,
    sleeper: Callable[[float], object] = time.sleep,
) -> None:
    """Pad to the fixed target or refuse an overrun without adapting it."""

    registered = PUBLIC_REQUEST_CLASSES.get(getattr(request_class, "name", None))
    if registered is not request_class:
        raise ProjectedRequestTimingUnavailable(
            "governed projected request timing is unavailable"
        )
    finished_at = clock()
    if (
        isinstance(started_at, bool)
        or not isinstance(started_at, (int, float))
        or finished_at < started_at
    ):
        raise ProjectedRequestTimingUnavailable(
            "governed projected request timing is unavailable"
        )
    elapsed_ms = (finished_at - float(started_at)) * 1000.0
    if elapsed_ms > request_class.deadline_ms:
        raise ProjectedRequestDeadlineExceeded(
            "governed projected request deadline was exceeded"
        )
    remaining = max(0.0, (request_class.padding_ms - elapsed_ms) / 1000.0)
    if remaining:
        sleeper(remaining)


@contextmanager
def fixed_public_completion(
    request_class: PublicRequestClass,
) -> Iterator[None]:
    """Apply one class to success and error completion paths alike."""

    started_at = time.perf_counter()
    try:
        yield
    finally:
        complete_public_request(request_class, started_at=started_at)


__all__ = [
    "PUBLIC_REQUEST_CLASSES",
    "ProjectedRequestDeadlineExceeded",
    "ProjectedRequestTimingUnavailable",
    "PublicRequestClass",
    "TimingReleaseManifest",
    "WireDifferentialReport",
    "complete_public_request",
    "evaluate_wire_differential",
    "fixed_public_completion",
    "request_class_for_command",
    "request_class_for_find",
    "validate_release_manifest",
]
