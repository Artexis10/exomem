"""Repository-owned completion classes for governed projected requests."""

from __future__ import annotations

import math
import os
import random
import statistics
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
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
    max_query_chars: int
    max_limit: int
    max_hidden_delta_ms: int
    max_hidden_delta_ratio: float

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or not self.name
            or type(self.padding_ms) is not int
            or type(self.deadline_ms) is not int
            or self.padding_ms <= 0
            or self.deadline_ms < self.padding_ms
            or self.max_query_chars != 4_096
            or self.max_limit != 100
            or self.max_hidden_delta_ms
            != projections.MAX_HIDDEN_CORPUS_WIRE_DELTA_MS
            or self.max_hidden_delta_ratio
            != projections.MAX_HIDDEN_CORPUS_WIRE_DELTA_RATIO
        ):
            raise ProjectedRequestTimingUnavailable(
                "governed projected request timing is unavailable"
            )


_PROJECTED_FIND_V1 = PublicRequestClass(
    name="projected-find-v1",
    padding_ms=250,
    deadline_ms=300,
    max_query_chars=4_096,
    max_limit=100,
    max_hidden_delta_ms=projections.MAX_HIDDEN_CORPUS_WIRE_DELTA_MS,
    max_hidden_delta_ratio=projections.MAX_HIDDEN_CORPUS_WIRE_DELTA_RATIO,
)
_PROJECTED_FIND_VECTOR_CPU_V1 = PublicRequestClass(
    name="projected-find-vector-cpu-v1",
    padding_ms=1_000,
    deadline_ms=1_500,
    max_query_chars=4_096,
    max_limit=100,
    max_hidden_delta_ms=projections.MAX_HIDDEN_CORPUS_WIRE_DELTA_MS,
    max_hidden_delta_ratio=projections.MAX_HIDDEN_CORPUS_WIRE_DELTA_RATIO,
)

PUBLIC_REQUEST_CLASSES: Mapping[str, PublicRequestClass] = MappingProxyType(
    {
        _PROJECTED_FIND_V1.name: _PROJECTED_FIND_V1,
        _PROJECTED_FIND_VECTOR_CPU_V1.name: _PROJECTED_FIND_VECTOR_CPU_V1,
    }
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
        "model_runtime_profile",
        "request_classes",
        "routes",
        "capacity",
        "scheduler_tolerance_subtracted",
        "padding_selected_before_sampling",
    }
)
_REQUEST_CLASS_KEYS = frozenset(
    {"name", "padding_ms", "deadline_ms", "max_query_chars", "max_limit"}
)
_CAPACITY_KEYS = frozenset(
    {"catalog_items", "searchable_bytes_per_item", "graph_edges"}
)
_HARD_OFF_REQUIRED_ROUTES = frozenset(
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
_VECTOR_CPU_REQUIRED_ROUTES = frozenset(
    {
        "keyword",
        "bm25",
        "vector-live",
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
_ALL_REQUIRED_ROUTES = _HARD_OFF_REQUIRED_ROUTES | _VECTOR_CPU_REQUIRED_ROUTES
_HARDWARE_RUNTIME_PROFILE = "github-hosted-ubuntu-latest-x64-python3.13"
MODEL_RUNTIME_PROFILE = "models-hard-off-v1"
VECTOR_CPU_MODEL_RUNTIME_PROFILE = "vectors-cpu-torch-v1"
_PROFILE_REQUEST_CLASSES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        MODEL_RUNTIME_PROFILE: (_PROJECTED_FIND_V1.name,),
        VECTOR_CPU_MODEL_RUNTIME_PROFILE: (_PROJECTED_FIND_VECTOR_CPU_V1.name,),
    }
)
_PROFILE_REQUIRED_ROUTES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        MODEL_RUNTIME_PROFILE: _HARD_OFF_REQUIRED_ROUTES,
        VECTOR_CPU_MODEL_RUNTIME_PROFILE: _VECTOR_CPU_REQUIRED_ROUTES,
    }
)
_MIN_SAMPLE_COUNT_PER_CONDITION = 200
_MIN_BOOTSTRAP_RESAMPLES = 2_000
_TIMING_RELEASE_MANIFEST_PROOF = object()


@dataclass(frozen=True, slots=True, init=False)
class TimingReleaseManifest:
    """Validated, non-waivable inputs for one actual-wire release gate."""

    hidden_corpus_wire_delta_ms: int
    sample_count_per_condition: int
    bootstrap_resamples: int
    bootstrap_seed: int
    hardware_runtime_profile: str
    model_runtime_profile: str
    request_class_names: tuple[str, ...]
    routes: frozenset[str]
    catalog_items: int
    searchable_bytes_per_item: int
    graph_edges: int

    def __init__(
        self,
        *,
        hidden_corpus_wire_delta_ms: int,
        sample_count_per_condition: int,
        bootstrap_resamples: int,
        bootstrap_seed: int,
        hardware_runtime_profile: str,
        model_runtime_profile: str,
        request_class_names: tuple[str, ...],
        routes: frozenset[str],
        catalog_items: int,
        searchable_bytes_per_item: int,
        graph_edges: int,
        _proof: object | None = None,
    ) -> None:
        if _proof is not _TIMING_RELEASE_MANIFEST_PROOF:
            raise ProjectedRequestTimingUnavailable(
                "governed projected request timing is unavailable"
            )
        values = {
            "hidden_corpus_wire_delta_ms": hidden_corpus_wire_delta_ms,
            "sample_count_per_condition": sample_count_per_condition,
            "bootstrap_resamples": bootstrap_resamples,
            "bootstrap_seed": bootstrap_seed,
            "hardware_runtime_profile": hardware_runtime_profile,
            "model_runtime_profile": model_runtime_profile,
            "request_class_names": request_class_names,
            "routes": routes,
            "catalog_items": catalog_items,
            "searchable_bytes_per_item": searchable_bytes_per_item,
            "graph_edges": graph_edges,
        }
        for name, value in values.items():
            object.__setattr__(self, name, value)


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


@dataclass(frozen=True, slots=True)
class WireSample:
    """One predeclared member of a hidden-present/absent wire pair.

    ``capacity`` is the actual hidden-corpus condition: absent samples are
    always ``zero``; present samples alternate between ``one`` and ``maximum``.
    """

    pair_id: int
    route: str
    capacity: str
    hidden_present: bool

    def __post_init__(self) -> None:
        if (
            type(self.pair_id) is not int
            or self.pair_id < 0
            or self.route not in _ALL_REQUIRED_ROUTES
            or self.capacity not in {"zero", "one", "maximum"}
            or type(self.hidden_present) is not bool
        ):
            raise ProjectedRequestTimingUnavailable(
                "governed projected request timing is unavailable"
            )


@dataclass(frozen=True, slots=True)
class WireObservation:
    """Actual completion and canonical-envelope digest for one sample."""

    sample: WireSample
    elapsed_ms: float
    canonical_envelope_sha256: str

    def __post_init__(self) -> None:
        try:
            digest = bytes.fromhex(self.canonical_envelope_sha256)
        except (TypeError, ValueError) as error:
            raise ProjectedRequestTimingUnavailable(
                "governed projected request timing is unavailable"
            ) from error
        if (
            not isinstance(self.sample, WireSample)
            or isinstance(self.elapsed_ms, bool)
            or not isinstance(self.elapsed_ms, (int, float))
            or not math.isfinite(float(self.elapsed_ms))
            or float(self.elapsed_ms) < 0
            or len(digest) != 32
            or self.canonical_envelope_sha256 != digest.hex()
        ):
            raise ProjectedRequestTimingUnavailable(
                "governed projected request timing is unavailable"
            )


@dataclass(frozen=True, slots=True)
class WireRouteReport:
    """Content and timing verdict for one mandatory actual-wire route."""

    route: str
    envelope_pairs_equal: bool
    timing: WireDifferentialReport
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
    model_runtime_profile = manifest["model_runtime_profile"]
    if not isinstance(model_runtime_profile, str):
        raise ProjectedRequestTimingUnavailable(
            "governed projected request timing is unavailable"
        )
    expected_class_names = _PROFILE_REQUEST_CLASSES.get(model_runtime_profile)
    expected_routes = _PROFILE_REQUIRED_ROUTES.get(model_runtime_profile)
    if expected_class_names is None or expected_routes is None:
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
            or type(declared["max_query_chars"]) is not int
            or declared["max_query_chars"] != registered.max_query_chars
            or type(declared["max_limit"]) is not int
            or declared["max_limit"] != registered.max_limit
        ):
            raise ProjectedRequestTimingUnavailable(
                "governed projected request timing is unavailable"
            )
        names.append(name)
    if len(names) != len(set(names)) or tuple(names) != expected_class_names:
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
        or frozenset(routes) != expected_routes
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
        model_runtime_profile=model_runtime_profile,
        request_class_names=tuple(names),
        routes=frozenset(routes),
        catalog_items=expected_capacity["catalog_items"],
        searchable_bytes_per_item=expected_capacity["searchable_bytes_per_item"],
        graph_edges=expected_capacity["graph_edges"],
        _proof=_TIMING_RELEASE_MANIFEST_PROOF,
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
    present = _wire_samples(hidden_present_ms, manifest.sample_count_per_condition)
    absent = _wire_samples(physically_absent_ms, manifest.sample_count_per_condition)
    deadline_misses = sum(
        sample > request_class.deadline_ms for sample in (*present, *absent)
    )

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


def release_sample_schedule(
    manifest: TimingReleaseManifest,
    *,
    route: str,
) -> tuple[WireSample, ...]:
    """Return the immutable pre-observation paired schedule for one route."""

    if (
        not isinstance(manifest, TimingReleaseManifest)
        or route not in manifest.routes
        or route not in _ALL_REQUIRED_ROUTES
    ):
        raise ProjectedRequestTimingUnavailable(
            "governed projected request timing is unavailable"
        )
    seed_material = f"{manifest.bootstrap_seed}\0{route}".encode("ascii")
    rng = random.Random(int.from_bytes(sha256(seed_material).digest()[:8], "big"))
    present_capacities = [
        ("one", "maximum")[index % 2]
        for index in range(manifest.sample_count_per_condition)
    ]
    rng.shuffle(present_capacities)
    schedule: list[WireSample] = []
    for pair_id, present_capacity in enumerate(present_capacities):
        first_present = bool(rng.getrandbits(1))
        for hidden_present in (first_present, not first_present):
            schedule.append(
                WireSample(
                    pair_id=pair_id,
                    route=route,
                    capacity=present_capacity if hidden_present else "zero",
                    hidden_present=hidden_present,
                )
            )
    return tuple(schedule)


def evaluate_wire_route(
    manifest: TimingReleaseManifest,
    *,
    route: str,
    observations: tuple[WireObservation, ...],
) -> WireRouteReport:
    """Evaluate one route only when it follows the predeclared paired schedule."""

    if not isinstance(observations, tuple):
        raise ProjectedRequestTimingUnavailable(
            "governed projected request timing is unavailable"
        )
    schedule = release_sample_schedule(manifest, route=route)
    if len(observations) != len(schedule) or any(
        not isinstance(observation, WireObservation)
        or observation.sample != expected
        for observation, expected in zip(observations, schedule, strict=True)
    ):
        raise ProjectedRequestTimingUnavailable(
            "governed projected request timing is unavailable"
        )
    present: list[float] = []
    absent: list[float] = []
    envelope_pairs_equal = True
    for offset in range(0, len(observations), 2):
        left, right = observations[offset : offset + 2]
        envelope_pairs_equal = envelope_pairs_equal and (
            left.canonical_envelope_sha256 == right.canonical_envelope_sha256
        )
    for observation in observations:
        target = present if observation.sample.hidden_present else absent
        target.append(float(observation.elapsed_ms))
    if len(manifest.request_class_names) != 1:
        raise ProjectedRequestTimingUnavailable(
            "governed projected request timing is unavailable"
        )
    timing = evaluate_wire_differential(
        manifest,
        request_class_name=manifest.request_class_names[0],
        hidden_present_ms=present,
        physically_absent_ms=absent,
    )
    return WireRouteReport(
        route=route,
        envelope_pairs_equal=envelope_pairs_equal,
        timing=timing,
        passed=envelope_pairs_equal and timing.passed,
    )


def request_class_for_find(
    *,
    mode: str,
    scope: str,
    graph: bool,
    rerank: bool | None,
) -> PublicRequestClass:
    """Select the fixed class shared by every implemented projected find lane."""

    if (
        mode not in {"keyword", "hybrid", "vector"}
        or scope not in {"kb", "vault"}
        or type(graph) is not bool
        or rerank not in {None, False, True}
        or (mode == "keyword" and graph)
    ):
        raise ProjectedRequestTimingUnavailable(
            "governed projected request timing is unavailable"
        )
    if model_runtime_profile_from_environment() == VECTOR_CPU_MODEL_RUNTIME_PROFILE:
        return _PROJECTED_FIND_VECTOR_CPU_V1
    return _PROJECTED_FIND_V1


def model_runtime_profile_from_environment() -> str | None:
    """Resolve only repository-certified, exact model execution profiles."""

    if all(
        os.environ.get(name) == "1"
        for name in (
            "EXOMEM_DISABLE_EMBEDDINGS",
            "EXOMEM_DISABLE_CLIP",
            "EXOMEM_DISABLE_RANKING",
        )
    ):
        return MODEL_RUNTIME_PROFILE
    if (
        "EXOMEM_DISABLE_EMBEDDINGS" not in os.environ
        and os.environ.get("EXOMEM_DISABLE_CLIP") == "1"
        and os.environ.get("EXOMEM_DISABLE_RANKING") == "1"
        and os.environ.get("EXOMEM_DEVICE") == "cpu"
        and os.environ.get("EXOMEM_EMBED_BACKEND") == "torch"
        and "EXOMEM_EMBED_DEVICE" not in os.environ
        and "EXOMEM_TORCH_DEVICE" not in os.environ
    ):
        return VECTOR_CPU_MODEL_RUNTIME_PROFILE
    return None


def request_class_for_command(
    command: object,
    injected: tuple[object, ...],
    kwargs: Mapping[str, object],
) -> PublicRequestClass | None:
    """Resolve the class before semantic validation so errors are padded too."""

    del injected, kwargs
    if getattr(command, "name", None) not in {"ask_memory", "find"}:
        return None
    leaf = getattr(command, "leaf", None)
    if not callable(leaf):
        raise ProjectedRequestTimingUnavailable(
            "governed projected request timing is unavailable"
        )
    # The one class covers every registered shape up to its declared maxima,
    # plus normalized refusals. Shape-specific actual-wire routes enforce that
    # breadth; callers cannot select another padding or deadline.
    if model_runtime_profile_from_environment() == VECTOR_CPU_MODEL_RUNTIME_PROFILE:
        return _PROJECTED_FIND_VECTOR_CPU_V1
    return _PROJECTED_FIND_V1


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
    if (
        isinstance(started_at, bool)
        or not isinstance(started_at, (int, float))
        or not math.isfinite(float(started_at))
    ):
        raise ProjectedRequestTimingUnavailable(
            "governed projected request timing is unavailable"
        )
    started = float(started_at)
    previous = started
    while True:
        reading = clock()
        if (
            isinstance(reading, bool)
            or not isinstance(reading, (int, float))
            or not math.isfinite(float(reading))
            or float(reading) < previous
        ):
            raise ProjectedRequestTimingUnavailable(
                "governed projected request timing is unavailable"
            )
        completed = float(reading)
        elapsed_ms = (completed - started) * 1000.0
        if elapsed_ms > request_class.deadline_ms:
            raise ProjectedRequestDeadlineExceeded(
                "governed projected request deadline was exceeded"
            )
        if elapsed_ms >= request_class.padding_ms:
            return
        sleeper((request_class.padding_ms - elapsed_ms) / 1000.0)
        previous = completed


@contextmanager
def fixed_public_completion(
    request_class: PublicRequestClass,
    *,
    started_at: float | None = None,
) -> Iterator[None]:
    """Apply one class to success and error completion paths alike."""

    started = time.perf_counter() if started_at is None else started_at
    try:
        yield
    finally:
        complete_public_request(request_class, started_at=started)


__all__ = [
    "PUBLIC_REQUEST_CLASSES",
    "ProjectedRequestDeadlineExceeded",
    "ProjectedRequestTimingUnavailable",
    "PublicRequestClass",
    "TimingReleaseManifest",
    "WireObservation",
    "WireDifferentialReport",
    "WireRouteReport",
    "WireSample",
    "complete_public_request",
    "evaluate_wire_differential",
    "evaluate_wire_route",
    "fixed_public_completion",
    "model_runtime_profile_from_environment",
    "request_class_for_command",
    "request_class_for_find",
    "release_sample_schedule",
    "validate_release_manifest",
    "VECTOR_CPU_MODEL_RUNTIME_PROFILE",
]
