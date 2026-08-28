"""Load one request-pinned active authorization-projection namespace."""

from __future__ import annotations

import gc
import hashlib
import math
import os
import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from numbers import Real
from pathlib import Path, PurePosixPath

from .. import bm25, find_policy, fusion, ranking_config
from ..find_types import GraphProvenance, Hit
from ..kbdir import kb_dirname
from . import (
    authorization_custody,
    authorization_session_authority,
    authorization_session_lifecycle,
    projected_graph,
    projected_retrieval,
    projection_authorization,
    projection_measurement_store,
    projection_store,
    projection_timing,
    projections,
    schema_v4,
    store,
)
from .principal import RequestPrincipal


class ProjectionRuntimeUnavailable(RuntimeError):
    """The enrolled projected corpus cannot be verified for serving."""


@dataclass(frozen=True, slots=True)
class _PreactivatedProjectionRuntime:
    root_key: str
    cell_id: str
    logical_vault_id: str
    activation_store_id: str
    activation_epoch: int
    activation_state_digest: str
    runtime: ActiveProjectionRuntime


_PREACTIVATED_RUNTIMES: dict[str, _PreactivatedProjectionRuntime] = {}
_PROJECTED_COMPLETION_ROOTS: set[str] = set()
_PREACTIVATED_RUNTIME_LOCK = threading.RLock()
_PROJECTED_CONTINUATION_TTL_S = 15 * 60.0
_MAX_PROJECTED_CONTINUATIONS = 4_096
_PROJECTED_CONTINUATION_PREFIX = "pc1."
_PROJECTED_CONTINUATION_DOMAIN = b"exomem.projected-find-continuation.v1\0"
_VISIBLE_SNAPSHOT_DOMAIN = b"exomem.projected-visible-snapshot.v1\0"
_AUTHORIZATION_MAP_DOMAIN = b"exomem.projected-authorization-map.v1\0"
_VISIBLE_AUTHORIZATION_DOMAIN = b"exomem.projected-visible-authorization.v1\0"
_PROJECTED_VECTOR_QUERY_MAX_CODEPOINTS = 600
# Repository-owned release fence. Checked manifests and required actual-wire CI
# matrices certify the exact hard-off and CPU/Torch-vector profiles. Environment
# cannot opt any other model configuration into serving; reranker, CLIP, GPU,
# ONNX, mixed, and device-override profiles remain closed until their own evidence
# lands. Both accepted profiles include hidden-only missing-index reduction and a
# real second-page continuation.
_PROJECTED_SERVING_RELEASE_ACCEPTED = True


def projected_serving_release_profile() -> str | None:
    """Return a repository-certified runtime profile only when exact."""

    return projection_timing.model_runtime_profile_from_environment()


def _runtime_supports_release_profile(
    runtime: ActiveProjectionRuntime,
    profile: str,
) -> bool:
    if profile == projection_timing.MODEL_RUNTIME_PROFILE:
        return True
    if profile != projection_timing.VECTOR_CPU_MODEL_RUNTIME_PROFILE:
        return False
    from .. import embeddings

    return (
        runtime.vector_index is not None
        and runtime.vector_index.extractor_version == "projected-text-v1"
        and runtime.vector_index.model_version == embeddings.MODEL_NAME
    )


def _projected_vector_query(query: str) -> str:
    """Return the fixed, model-only query projection certified by release CI."""

    normalized = " ".join(query.split())
    if len(normalized) <= _PROJECTED_VECTOR_QUERY_MAX_CODEPOINTS:
        return normalized
    prefix = normalized[:_PROJECTED_VECTOR_QUERY_MAX_CODEPOINTS]
    if " " in prefix:
        prefix = prefix.rsplit(" ", 1)[0]
    return prefix


@dataclass(frozen=True, slots=True)
class ActiveProjectionRuntime:
    snapshot: schema_v4.ActivePolicySnapshot
    namespace: projection_store.VerifiedProjectionNamespace
    measurement_roots: tuple[projection_store.ProjectionMeasurementRoot, ...] = ()
    vector_index: projected_retrieval.ProjectedVectorIndex | None = field(
        default=None,
        repr=False,
    )
    clip_index: projected_retrieval.ProjectedClipIndex | None = field(
        default=None,
        repr=False,
    )
    graph_index: projected_graph.ProjectedGraphIndex | None = field(
        default=None,
        repr=False,
    )
    catalog: projected_retrieval.ProjectionCatalog = field(
        init=False,
        repr=False,
    )
    lexical_index: projected_retrieval.ProjectedLexicalIndex = field(
        init=False,
        repr=False,
    )
    reranker: projected_retrieval.ProjectedReranker = field(
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.snapshot, schema_v4.ActivePolicySnapshot)
            or not isinstance(
                self.namespace,
                projection_store.VerifiedProjectionNamespace,
            )
            or self.namespace.active_state_digest
            != self.snapshot.active.activation_state_digest
            or self.namespace.namespace_key.policy_fingerprint
            != self.snapshot.active.policy_fingerprint
            or self.namespace.namespace_key.projector_schema_version
            != self.snapshot.active.projector_schema_version
            or self.namespace.namespace_key.catalog_generation
            != self.snapshot.active.catalog_generation
        ):
            raise ProjectionRuntimeUnavailable(
                "governed projected retrieval is unavailable"
            )
        if not isinstance(self.measurement_roots, tuple):
            raise ProjectionRuntimeUnavailable(
                "governed projected retrieval is unavailable"
            )
        by_lane: dict[str, projection_store.ProjectionMeasurementRoot] = {}
        for root_commitment in self.measurement_roots:
            if (
                not isinstance(
                    root_commitment,
                    projection_store.ProjectionMeasurementRoot,
                )
                or root_commitment.namespace_key != self.namespace.namespace_key
                or root_commitment.lane in by_lane
            ):
                raise ProjectionRuntimeUnavailable(
                    "governed projected retrieval is unavailable"
                )
            by_lane[root_commitment.lane] = root_commitment
        ordered_roots = tuple(
            by_lane[lane]
            for lane in ("vector", "clip", "graph")
            if lane in by_lane
        )
        if ordered_roots != self.measurement_roots:
            raise ProjectionRuntimeUnavailable(
                "governed projected retrieval is unavailable"
            )
        indexes = {
            "vector": self.vector_index,
            "clip": self.clip_index,
            "graph": self.graph_index,
        }
        for lane, index in indexes.items():
            root = by_lane.get(lane)
            if (root is None) != (index is None):
                raise ProjectionRuntimeUnavailable(
                    "governed projected retrieval is unavailable"
                )
            if root is None or index is None:
                continue
            if lane == "vector":
                if not isinstance(index, projected_retrieval.ProjectedVectorIndex):
                    raise ProjectionRuntimeUnavailable(
                        "governed projected retrieval is unavailable"
                    )
                index_key = index.namespace_key
            elif lane == "clip":
                if not isinstance(index, projected_retrieval.ProjectedClipIndex):
                    raise ProjectionRuntimeUnavailable(
                        "governed projected retrieval is unavailable"
                    )
                index_key = index.namespace_key
            else:
                if not isinstance(index, projected_graph.ProjectedGraphIndex):
                    raise ProjectionRuntimeUnavailable(
                        "governed projected retrieval is unavailable"
                    )
                index_key = index.catalog.namespace_key
            if (
                index_key != self.namespace.namespace_key
                or index.extractor_version != root.extractor_version
                or index.model_version != root.model_version
            ):
                raise ProjectionRuntimeUnavailable(
                    "governed projected retrieval is unavailable"
                )
        catalog = projected_retrieval.ProjectionCatalog(self.namespace)
        object.__setattr__(self, "catalog", catalog)
        object.__setattr__(
            self,
            "lexical_index",
            projected_retrieval.ProjectedLexicalIndex(catalog),
        )
        object.__setattr__(
            self,
            "reranker",
            projected_retrieval.ProjectedReranker(self.namespace),
        )

    @property
    def warming_components(self) -> tuple[str, ...]:
        """Return model lanes absent from this immutable active tuple."""

        ready = frozenset(root.lane for root in self.measurement_roots)
        return tuple(lane for lane in ("vector", "clip", "graph") if lane not in ready)


@dataclass(frozen=True, slots=True)
class _ProjectedContinuationRecord:
    root_key: str
    token: str
    runtime: ActiveProjectionRuntime
    principal_binding: tuple[str, str | None]
    declared_purpose: str | None
    request_digest: str
    authorization_digest: str
    visible_authorization_digest: str
    visible_snapshot_digest: str
    next_offset: int
    candidate_depth: int
    expires_at: float


_PROJECTED_CONTINUATIONS: dict[
    tuple[str, str],
    _ProjectedContinuationRecord,
] = {}
_ACTIVE_PROJECTED_REQUESTS: dict[tuple[str, str], int] = {}
_PROJECTED_CONTINUATION_LOCK = threading.RLock()


@dataclass(frozen=True, slots=True)
class ProjectedFindResult:
    """One public acquisition result derived only from the pinned namespace."""

    hits: tuple[Hit, ...]
    withheld_paths: frozenset[str]
    warming_components: tuple[str, ...] = ()
    declared_purpose: str | None = None
    continuation: str | None = None


class ProjectedContinuationUnavailable(ValueError):
    """A governed continuation is malformed, stale, expired, or unavailable."""


def _invalid_continuation() -> ProjectedContinuationUnavailable:
    return ProjectedContinuationUnavailable(
        "INVALID_CONTINUATION: continuation is invalid or expired"
    )


def _framed_pairs_digest(
    domain: bytes,
    pairs: tuple[tuple[str, str | None], ...],
) -> str:
    digest = hashlib.sha256()
    digest.update(domain)
    digest.update(len(pairs).to_bytes(4, "big"))
    for left, right in pairs:
        for value in (left, right):
            encoded = b"" if value is None else value.encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def _principal_binding(principal: RequestPrincipal) -> tuple[str, str | None]:
    return (principal.audience_id, principal.authorization_session_id)


def _projected_request_digest(
    *,
    query: str,
    limit: int,
    scope: str,
    mode: str,
    graph: bool,
    rerank: bool | None,
    auto_rerank: bool,
    prefer_compiled: bool,
    prefer_active: bool,
) -> str:
    value = {
        "auto_rerank": auto_rerank,
        "graph": graph,
        "limit": limit,
        "mode": mode,
        "prefer_active": prefer_active,
        "prefer_compiled": prefer_compiled,
        "query": query,
        "rerank": rerank,
        "scope": scope,
    }
    return hashlib.sha256(projections.canonical_jcs(value)).hexdigest()


def _authorization_map_digest(
    authorization: projected_retrieval.AuthorizationProjectionMap,
) -> str:
    selected = {
        selection.item_identity: selection.projection_variant_id
        for selection in authorization.selections
    }
    pairs = tuple(
        (identity, selected.get(identity))
        for identity in sorted(
            set(selected).union(authorization.withheld_identities),
            key=lambda value: value.encode("utf-16-be"),
        )
    )
    return _framed_pairs_digest(_AUTHORIZATION_MAP_DOMAIN, pairs)


def _visible_authorization_digest(
    authorization: projected_retrieval.AuthorizationProjectionMap,
) -> str:
    return _framed_pairs_digest(
        _VISIBLE_AUTHORIZATION_DOMAIN,
        tuple(
            sorted(
                (
                    (selection.item_identity, selection.projection_variant_id)
                    for selection in authorization.selections
                    if selection.projection_variant_id is not None
                ),
                key=lambda pair: pair[0].encode("utf-16-be"),
            )
        ),
    )


def _visible_snapshot_digest(
    ordered_identities: tuple[str, ...],
    selected_variants: Mapping[str, projections.ProjectionVariant],
) -> str:
    return _framed_pairs_digest(
        _VISIBLE_SNAPSHOT_DOMAIN,
        tuple(
            (
                identity,
                selected_variants[identity].projection_variant_id,
            )
            for identity in ordered_identities
        ),
    )


def _continuation_token(
    *,
    principal_binding: tuple[str, str | None],
    declared_purpose: str | None,
    request_digest: str,
    visible_snapshot_digest: str,
    next_offset: int,
) -> str:
    value = {
        "authorization_session_id": principal_binding[1],
        "principal_id": principal_binding[0],
        "purpose": declared_purpose,
        "request_digest": request_digest,
        "next_offset": next_offset,
        "visible_snapshot_digest": visible_snapshot_digest,
    }
    return _PROJECTED_CONTINUATION_PREFIX + hashlib.sha256(
        _PROJECTED_CONTINUATION_DOMAIN + projections.canonical_jcs(value)
    ).hexdigest()


def _purge_projected_continuations(now: float) -> None:
    expired = [
        key
        for key, record in _PROJECTED_CONTINUATIONS.items()
        if record.expires_at <= now
    ]
    for key in expired:
        del _PROJECTED_CONTINUATIONS[key]


def _clear_projected_continuations_for_tests() -> None:
    with _PROJECTED_CONTINUATION_LOCK:
        _PROJECTED_CONTINUATIONS.clear()


def _clear_projection_request_pins_for_tests() -> None:
    with _PROJECTED_CONTINUATION_LOCK:
        _ACTIVE_PROJECTED_REQUESTS.clear()


def _lookup_projected_continuation(
    vault_root: Path,
    continuation: object,
) -> _ProjectedContinuationRecord:
    if (
        not isinstance(continuation, str)
        or len(continuation) != len(_PROJECTED_CONTINUATION_PREFIX) + 64
        or not continuation.startswith(_PROJECTED_CONTINUATION_PREFIX)
        or any(
            character not in "0123456789abcdef"
            for character in continuation[len(_PROJECTED_CONTINUATION_PREFIX) :]
        )
    ):
        raise _invalid_continuation()
    now = time.monotonic()
    key = (_root_key(Path(vault_root)), continuation)
    with _PROJECTED_CONTINUATION_LOCK:
        _purge_projected_continuations(now)
        record = _PROJECTED_CONTINUATIONS.get(key)
    if record is None:
        raise _invalid_continuation()
    return record


@contextmanager
def _projection_runtime_request_scope(
    vault_root: Path,
    runtime: ActiveProjectionRuntime,
    *,
    continuation: object,
) -> Iterator[tuple[ActiveProjectionRuntime, _ProjectedContinuationRecord | None]]:
    """Pin the exact current or continuation runtime for one complete request."""

    if not isinstance(runtime, ActiveProjectionRuntime):
        raise ProjectionRuntimeUnavailable(
            "governed projected retrieval is unavailable"
        )
    root_key = _root_key(Path(vault_root))
    with _PROJECTED_CONTINUATION_LOCK:
        record = (
            None
            if continuation is None
            else _lookup_projected_continuation(Path(vault_root), continuation)
        )
        selected = runtime if record is None else record.runtime
        pin = (root_key, selected.namespace.namespace_key.namespace_id)
        _ACTIVE_PROJECTED_REQUESTS[pin] = _ACTIVE_PROJECTED_REQUESTS.get(pin, 0) + 1
    try:
        yield selected, record
    finally:
        with _PROJECTED_CONTINUATION_LOCK:
            remaining = _ACTIVE_PROJECTED_REQUESTS.get(pin, 0) - 1
            if remaining > 0:
                _ACTIVE_PROJECTED_REQUESTS[pin] = remaining
            else:
                _ACTIVE_PROJECTED_REQUESTS.pop(pin, None)


def projection_namespace_runtime_pins(vault_root: Path) -> frozenset[str]:
    """Snapshot exact process-local serving, request, and continuation pins."""

    root_key = _root_key(Path(vault_root))
    pins: set[str] = set()
    with _PREACTIVATED_RUNTIME_LOCK:
        current = _PREACTIVATED_RUNTIMES.get(root_key)
        if current is not None:
            pins.add(current.runtime.namespace.namespace_key.namespace_id)
    with _PROJECTED_CONTINUATION_LOCK:
        _purge_projected_continuations(time.monotonic())
        pins.update(
            record.runtime.namespace.namespace_key.namespace_id
            for (record_root, _token), record in _PROJECTED_CONTINUATIONS.items()
            if record_root == root_key
        )
        pins.update(
            namespace_id
            for (request_root, namespace_id), count in _ACTIVE_PROJECTED_REQUESTS.items()
            if request_root == root_key and count > 0
        )
    return frozenset(pins)


def _register_projected_continuation(
    vault_root: Path,
    *,
    runtime: ActiveProjectionRuntime,
    principal_binding: tuple[str, str | None],
    declared_purpose: str | None,
    request_digest: str,
    authorization_digest: str,
    visible_authorization_digest: str,
    visible_snapshot_digest: str,
    next_offset: int,
    candidate_depth: int,
    replace_existing: bool,
) -> str:
    token = _continuation_token(
        principal_binding=principal_binding,
        declared_purpose=declared_purpose,
        request_digest=request_digest,
        visible_snapshot_digest=visible_snapshot_digest,
        next_offset=next_offset,
    )
    now = time.monotonic()
    root_key = _root_key(Path(vault_root))
    key = (root_key, token)
    with _PROJECTED_CONTINUATION_LOCK:
        _purge_projected_continuations(now)
        existing = _PROJECTED_CONTINUATIONS.get(key)
        if existing is not None:
            public_binding_mismatch = (
                existing.principal_binding != principal_binding
                or existing.declared_purpose != declared_purpose
                or existing.request_digest != request_digest
                or existing.visible_authorization_digest
                != visible_authorization_digest
                or existing.visible_snapshot_digest != visible_snapshot_digest
                or existing.next_offset != next_offset
                or existing.candidate_depth != candidate_depth
            )
            if public_binding_mismatch:
                raise _invalid_continuation()
            if not replace_existing:
                if (
                    existing.runtime is not runtime
                    or existing.authorization_digest != authorization_digest
                ):
                    raise _invalid_continuation()
                return token
        if len(_PROJECTED_CONTINUATIONS) >= _MAX_PROJECTED_CONTINUATIONS:
            if existing is None:
                raise _invalid_continuation()
        _PROJECTED_CONTINUATIONS[key] = _ProjectedContinuationRecord(
            root_key=root_key,
            token=token,
            runtime=runtime,
            principal_binding=principal_binding,
            declared_purpose=declared_purpose,
            request_digest=request_digest,
            authorization_digest=authorization_digest,
            visible_authorization_digest=visible_authorization_digest,
            visible_snapshot_digest=visible_snapshot_digest,
            next_offset=next_offset,
            candidate_depth=candidate_depth,
            expires_at=now + _PROJECTED_CONTINUATION_TTL_S,
        )
    return token


def _stabilize_projection_runtime(
    runtime: ActiveProjectionRuntime,
) -> ActiveProjectionRuntime:
    """Finish tracing the immutable runtime before it becomes request-visible.

    Exact-capacity catalogs and graph families contain hundreds of thousands of
    long-lived immutable objects. Leaving their first full GC traversal to a
    public request makes that request's completion depend on hidden corpus
    size. Collect once at the startup publication boundary, before routing can
    resolve this runtime; request-local allocations then stay in the young
    generations covered by the fixed completion class.
    """

    if not isinstance(runtime, ActiveProjectionRuntime):
        raise ProjectionRuntimeUnavailable(
            "governed projected retrieval is unavailable"
        )
    gc.collect()
    return runtime


def _root_key(vault_root: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(Path(vault_root))))


def _control_activation(
    custody: authorization_custody.AuthorizationCustody,
) -> tuple[str, str, str, int, str] | None:
    control = custody.control
    if not control.governance_enrolled:
        return None
    if (
        control.activation_store_id is None
        or control.activation_epoch is None
        or control.activation_state_digest is None
    ):
        raise ProjectionRuntimeUnavailable(
            "governed projected retrieval is unavailable"
        )
    return (
        control.cell_id,
        control.logical_vault_id,
        control.activation_store_id,
        control.activation_epoch,
        control.activation_state_digest,
    )


def _configured_external_custody() -> bool:
    return any(
        variable in os.environ
        for variable in (
            authorization_custody.KEYRING_FILE_ENV,
            authorization_custody.CONTROL_FILE_ENV,
        )
    )


def _clear_preactivated_runtimes_for_tests() -> None:
    """Clear process-local serving state between isolated unit tests."""

    with _PREACTIVATED_RUNTIME_LOCK:
        _PREACTIVATED_RUNTIMES.clear()
        _PROJECTED_COMPLETION_ROOTS.clear()


def has_preactivated_projection_runtime(vault_root: Path) -> bool:
    """Return process-local readiness without opening custody or catalog state."""

    with _PREACTIVATED_RUNTIME_LOCK:
        return _root_key(Path(vault_root)) in _PREACTIVATED_RUNTIMES


def requires_fixed_projected_completion(vault_root: Path) -> bool:
    """Return the process-classified timing boundary without request IO."""

    with _PREACTIVATED_RUNTIME_LOCK:
        return _root_key(Path(vault_root)) in _PROJECTED_COMPLETION_ROOTS


def classify_projected_completion_boundary(vault_root: Path) -> bool:
    """Observe and retain the irreversible projected timing boundary."""

    root = Path(vault_root)
    root_key = _root_key(root)
    with _PREACTIVATED_RUNTIME_LOCK:
        if root_key in _PROJECTED_COMPLETION_ROOTS:
            return True
    completion_required = requires_projected_read_boundary(root)
    if completion_required:
        with _PREACTIVATED_RUNTIME_LOCK:
            _PROJECTED_COMPLETION_ROOTS.add(root_key)
    return completion_required


def preactivate_projection_runtime(
    vault_root: Path,
) -> ActiveProjectionRuntime | None:
    """Build one exact v4 runtime before a transport accepts requests.

    An unconfigured or authenticated never-enrolled vault has no projected
    runtime.  Configured but unverifiable custody and every incomplete v4
    namespace refuse content-free; startup never relabels them as legacy.
    """

    root = Path(vault_root)
    root_key = _root_key(root)
    classify_projected_completion_boundary(root)
    if not _configured_external_custody():
        return None
    connection: sqlite3.Connection | None = None
    try:
        custody = authorization_custody.load_authorization_custody(
            root,
            now=int(time.time()),
        )
        activation = _control_activation(custody)
        if activation is None:
            with _PREACTIVATED_RUNTIME_LOCK:
                _PROJECTED_COMPLETION_ROOTS.discard(root_key)
            return None
        cell_id, logical_vault_id, store_id, epoch, digest = activation
        connection = store.open_active_governance_read_connection(root)
        connection.execute("BEGIN")
        snapshot = schema_v4.load_active_policy(
            connection,
            expected_logical_vault_id=logical_vault_id,
            expected_activation_store_id=store_id,
            expected_activation_epoch=epoch,
            expected_activation_state_digest=digest,
        )
        evidence = projection_store.namespace_evidence_from_snapshot(snapshot)
        expected_manifest = evidence.manifest
        manifest, items = projection_store.load_projection_catalog(
            root,
            key=expected_manifest.namespace_key,
            expected_rows_digest=expected_manifest.rows_digest,
        )
        if manifest != expected_manifest:
            raise ProjectionRuntimeUnavailable(
                "governed projected retrieval is unavailable"
            )
        namespace = projection_store.bind_active_projection_namespace(
            snapshot,
            manifest=manifest,
            items=items,
        )
        vector_index: projected_retrieval.ProjectedVectorIndex | None = None
        clip_index: projected_retrieval.ProjectedClipIndex | None = None
        graph_index: projected_graph.ProjectedGraphIndex | None = None
        for root_commitment in evidence.required_measurement_roots:
            family = projection_measurement_store.MeasurementFamilyKey(
                namespace_key=namespace.namespace_key,
                lane=root_commitment.lane,
                extractor_version=root_commitment.extractor_version,
                model_version=root_commitment.model_version,
            )
            if family.family_id != root_commitment.family_id:
                raise ProjectionRuntimeUnavailable(
                    "governed projected retrieval is unavailable"
                )
            if root_commitment.lane == "vector":
                loaded_manifest, vector_index = (
                    projection_measurement_store.load_vector_index(
                        root,
                        namespace=namespace,
                        family=family,
                        expected_rows_digest=root_commitment.rows_digest,
                    )
                )
            elif root_commitment.lane == "clip":
                loaded_manifest, clip_index = (
                    projection_measurement_store.load_clip_index(
                        root,
                        namespace=namespace,
                        family=family,
                        expected_rows_digest=root_commitment.rows_digest,
                    )
                )
            else:
                loaded_manifest, graph_index = (
                    projection_measurement_store.load_graph_index(
                        root,
                        namespace=namespace,
                        family=family,
                        expected_rows_digest=root_commitment.rows_digest,
                    )
                )
            if (
                projection_measurement_store.measurement_root(loaded_manifest)
                != root_commitment
            ):
                raise ProjectionRuntimeUnavailable(
                    "governed projected retrieval is unavailable"
                )
        runtime = _stabilize_projection_runtime(
            ActiveProjectionRuntime(
                snapshot,
                namespace,
                evidence.required_measurement_roots,
                vector_index=vector_index,
                clip_index=clip_index,
                graph_index=graph_index,
            )
        )
        record = _PreactivatedProjectionRuntime(
            root_key=_root_key(root),
            cell_id=cell_id,
            logical_vault_id=logical_vault_id,
            activation_store_id=store_id,
            activation_epoch=epoch,
            activation_state_digest=digest,
            runtime=runtime,
        )
        with _PREACTIVATED_RUNTIME_LOCK:
            _PREACTIVATED_RUNTIMES[record.root_key] = record
        return runtime
    except ProjectionRuntimeUnavailable:
        raise
    except (
        authorization_custody.AuthorizationCustodyUnavailable,
        projection_store.ProjectionStoreError,
        schema_v4.SchemaV4Error,
        store.UnsupportedGovernanceSchema,
        FileNotFoundError,
        OSError,
        RuntimeError,
        sqlite3.Error,
        TypeError,
        ValueError,
    ) as error:
        raise ProjectionRuntimeUnavailable(
            "governed projected retrieval is unavailable"
        ) from error
    finally:
        if connection is not None:
            connection.close()


def _preactivated_runtime(vault_root: Path) -> ActiveProjectionRuntime | None:
    root = Path(vault_root)
    key = _root_key(root)
    with _PREACTIVATED_RUNTIME_LOCK:
        record = _PREACTIVATED_RUNTIMES.get(key)
    if record is None:
        return None
    try:
        custody = authorization_custody.load_authorization_custody(
            root,
            now=int(time.time()),
        )
        activation = _control_activation(custody)
    except (
        authorization_custody.AuthorizationCustodyUnavailable,
        ProjectionRuntimeUnavailable,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as error:
        raise ProjectionRuntimeUnavailable(
            "governed projected retrieval is unavailable"
        ) from error
    expected = (
        record.cell_id,
        record.logical_vault_id,
        record.activation_store_id,
        record.activation_epoch,
        record.activation_state_digest,
    )
    if activation != expected:
        raise ProjectionRuntimeUnavailable(
            "governed projected retrieval is unavailable"
        )
    connection: sqlite3.Connection | None = None
    try:
        connection = store.open_active_governance_read_connection(root)
        connection.execute("BEGIN")
        current = schema_v4.load_active_tuple_pointer(connection)
    except (
        schema_v4.SchemaV4Error,
        store.UnsupportedGovernanceSchema,
        FileNotFoundError,
        OSError,
        RuntimeError,
        sqlite3.Error,
        TypeError,
        ValueError,
    ) as error:
        raise ProjectionRuntimeUnavailable(
            "governed projected retrieval is unavailable"
        ) from error
    finally:
        if connection is not None:
            connection.close()
    if current != record.runtime.snapshot.active:
        raise ProjectionRuntimeUnavailable(
            "governed projected retrieval is unavailable"
        )
    return record.runtime


def _external_custody_requires_projected_boundary(root: Path) -> bool:
    configured = any(
        variable in os.environ
        for variable in (
            authorization_custody.KEYRING_FILE_ENV,
            authorization_custody.CONTROL_FILE_ENV,
        )
    )
    if not configured:
        return False
    try:
        custody = authorization_custody.load_authorization_custody(
            root,
            now=int(time.time()),
        )
    except authorization_custody.AuthorizationCustodyUnavailable:
        # A configured but unreadable external trust root is not proof that the
        # vault was never enrolled.  Refuse instead of falling into legacy raw
        # acquisition.
        return True
    return custody.control.governance_enrolled


def _unsupported_schema_requires_projected_boundary(root: Path) -> bool:
    if _external_custody_requires_projected_boundary(root):
        return True
    legacy: sqlite3.Connection | None = None
    try:
        legacy = store.open_readonly_connection(root)
        if legacy is not None:
            return False
    except (OSError, RuntimeError, sqlite3.Error):
        return True
    finally:
        if legacy is not None:
            legacy.close()
    # The exact v3 opener intentionally maps corrupt and future schemas to
    # ``None``.  Their physical presence therefore means "unknown authority",
    # not "ungoverned".
    return os.path.lexists(store.sidecar_path(root))


def requires_projected_read_boundary(vault_root: Path) -> bool:
    """Conservatively identify reads that must never reopen a legacy raw lane."""

    root = Path(vault_root)
    connection: sqlite3.Connection | None = None
    try:
        connection = store.open_active_governance_read_connection(root)
    except store.UnsupportedGovernanceSchema:
        return _unsupported_schema_requires_projected_boundary(root)
    except (FileNotFoundError, OSError, RuntimeError, sqlite3.Error):
        return True
    else:
        return True
    finally:
        if connection is not None:
            connection.close()


def load_active_projection_runtime(
    vault_root: Path,
) -> ActiveProjectionRuntime | None:
    """Return ``None`` only for proven legacy/unenrolled reads.

    Exact-v4 serving remains closed until activation can install a prebuilt
    runtime and the repository's actual-wire timing gate accepts it.  Public
    requests must never rebuild or tokenize the full namespace themselves.
    """

    root = Path(vault_root)
    preactivated = _preactivated_runtime(root)
    if preactivated is not None:
        release_profile = projected_serving_release_profile()
        if (
            not _PROJECTED_SERVING_RELEASE_ACCEPTED
            or release_profile is None
            or not _runtime_supports_release_profile(
                preactivated,
                release_profile,
            )
        ):
            raise ProjectionRuntimeUnavailable(
                "governed projected retrieval is unavailable"
            )
        return preactivated
    try:
        connection = store.open_active_governance_read_connection(root)
    except store.UnsupportedGovernanceSchema:
        if _unsupported_schema_requires_projected_boundary(root):
            raise ProjectionRuntimeUnavailable(
                "governed projected retrieval is unavailable"
            ) from None
        return None
    except (FileNotFoundError, OSError, RuntimeError, sqlite3.Error) as error:
        raise ProjectionRuntimeUnavailable(
            "governed projected retrieval is unavailable"
        ) from error
    connection.close()
    # This packet intentionally has no serving-runtime activation seam.  The
    # exact-v4 schema fence above is sufficient to choose the closed result;
    # sampling policy/catalog/namespace blobs here would turn the refusal into
    # a corpus-size timing oracle.
    raise ProjectionRuntimeUnavailable(
        "governed projected retrieval is unavailable"
    )


def _verified_session_grants(
    vault_root: Path,
    runtime: ActiveProjectionRuntime,
    *,
    principal: RequestPrincipal,
    purpose: str | None,
) -> tuple[str | None, tuple[projection_authorization.VerifiedProjectionGrant, ...]]:
    context = principal.verified_authorization_session
    if context is None:
        return purpose, ()
    if not isinstance(
        context,
        authorization_session_lifecycle.AuthorizationSessionContext,
    ):
        raise ProjectionRuntimeUnavailable(
            "governed projected retrieval is unavailable"
        )
    connection: sqlite3.Connection | None = None
    now = int(time.time())
    declared_purpose = purpose
    try:
        connection = store.open_authorization_session_connection(vault_root)
        connection.execute("BEGIN")
        if declared_purpose is None:
            declared_purpose = (
                authorization_session_authority.active_session_purpose(
                    connection,
                    context=context,
                    audience=principal.audience_id,
                    now=now,
                )
            )
        pairs = (
            authorization_session_authority.active_session_grants_for_projection_catalog(
                connection=connection,
                context=context,
                audience=principal.audience_id,
                purpose=declared_purpose,
                catalog=tuple(
                    authorization_session_authority.SessionMembership(
                        path=item.item_identity,
                        fingerprint=item.content_hash,
                        scope_ids=item.scope_ids,
                    )
                    for item in runtime.namespace.items
                ),
                policy_fingerprint=runtime.snapshot.policy.fingerprint,
                now=now,
            )
        )
        connection.commit()
    except (
        authorization_session_lifecycle.AuthorizationSessionUnavailable,
        FileNotFoundError,
        OSError,
        RuntimeError,
        sqlite3.Error,
        store.UnsupportedGovernanceSchema,
        ValueError,
    ) as error:
        if connection is not None and connection.in_transaction:
            connection.rollback()
        raise ProjectionRuntimeUnavailable(
            "governed projected retrieval is unavailable"
        ) from error
    finally:
        if connection is not None:
            connection.close()
    catalog = {item.item_identity: item for item in runtime.namespace.items}
    verified: list[projection_authorization.VerifiedProjectionGrant] = []
    for path, grant in pairs:
        item = catalog.get(path)
        if item is None:
            raise ProjectionRuntimeUnavailable(
                "governed projected retrieval is unavailable"
            )
        item_scopes = frozenset(item.scope_ids)
        contributing_scopes = tuple(
            scope_id for scope_id in grant.scope_ids if scope_id in item_scopes
        )
        if not contributing_scopes:
            continue
        verified.append(
            projection_authorization.VerifiedProjectionGrant(
                grant_id=grant.grant_id,
                item_identity=path,
                content_hash=item.content_hash,
                policy_fingerprint=grant.policy_fingerprint,
                scope_ids=contributing_scopes,
                audience=grant.audience,
                purpose=grant.purpose,
                ceiling=grant.ceiling,
            )
        )
    return declared_purpose, tuple(verified)


def _wire_hit(
    hit: projected_retrieval.ProjectedLexicalHit,
    *,
    selection: projected_retrieval.ProjectionSelection,
    rank: int,
    knowledge_base_name: str,
    keyword: bool,
    lane_ranks: dict[str, int] | None = None,
    lane_scores: dict[str, float] | None = None,
    graph_in_degree: int = 0,
    graph_hop: bool = False,
    graph_provenance: GraphProvenance | None = None,
    rerank_score: float | None = None,
    rerank_raw_score: float | None = None,
    rerank_input_rank: int | None = None,
) -> Hit:
    decision = selection.decision
    if decision is None or decision.level != hit.decision_level:
        raise ProjectionRuntimeUnavailable(
            "governed projected retrieval is unavailable"
        )
    fields = dict(hit.search_fields)
    path = hit.item_identity
    path_parts = PurePosixPath(path).parts
    inside_kb = bool(path_parts and path_parts[0] == knowledge_base_name)
    ranks = lane_ranks or {}
    scores = lane_scores or {}
    bm25_rank = ranks.get("bm25")
    keyword_rank = ranks.get("keyword")
    if lane_ranks is None:
        bm25_rank = None if keyword else rank
        keyword_rank = rank if keyword else None
    return Hit(
        path=path,
        type=fields.get("type"),
        scope="kb" if inside_kb else "vault",
        title=fields.get("title") or PurePosixPath(path).stem,
        updated=fields.get("updated", ""),
        excerpt=hit.snippet,
        bm25_rank=bm25_rank,
        keyword_rank=keyword_rank,
        vector_rank=ranks.get("vector"),
        vector_score=scores.get("vector"),
        clip_rank=ranks.get("clip"),
        clip_score=scores.get("clip"),
        clip_frame_ts=(
            None
            if hit.clip_frame_timestamp_ms is None
            else hit.clip_frame_timestamp_ms / 1000.0
        ),
        graph_hop=graph_hop,
        graph_in_degree=graph_in_degree,
        graph_provenance=graph_provenance,
        rerank_score=rerank_score,
        rerank_raw_score=rerank_raw_score,
        rerank_input_rank=rerank_input_rank,
        outside_kb=not inside_kb,
        media_type=fields.get("media_type"),
        status=fields.get("status"),
        snapshot_hash=selection.content_hash,
        decision=decision,
    )


def _projected_rank_multiplier(
    fields: Mapping[str, str],
    config: ranking_config.RankingConfig,
    *,
    prefer_compiled: bool,
    prefer_active: bool,
) -> float:
    """Apply public type/status rank policy using projected fields only."""

    multiplier = 1.0
    if prefer_compiled and not fields.get("media_type"):
        multiplier *= find_policy.type_multiplier(fields.get("type"), config)
    if prefer_active:
        multiplier *= find_policy.status_multiplier(fields.get("status"), config)
    return multiplier


def _order_projected_scope(
    ordered_identities: tuple[str, ...],
    *,
    scope: str,
    page_size: int,
    knowledge_base_name: str,
) -> tuple[str, ...]:
    """Apply the public KB-first reserve as one stable paginatable order."""

    if scope != "kb":
        return ordered_identities
    inside: list[str] = []
    outside: list[str] = []
    for identity in ordered_identities:
        parts = PurePosixPath(identity).parts
        target = inside if parts and parts[0] == knowledge_base_name else outside
        target.append(identity)
    if not inside or not outside:
        return ordered_identities
    reserve = min(max(1, page_size // 5), max(0, page_size - 1))
    kb_keep = page_size - reserve
    ordered: list[str] = []
    inside_offset = 0
    outside_offset = 0
    while inside_offset < len(inside) or outside_offset < len(outside):
        page = inside[inside_offset : inside_offset + kb_keep]
        inside_offset += len(page)
        widened = outside[outside_offset : outside_offset + reserve]
        outside_offset += len(widened)
        if len(page) < kb_keep:
            fill = outside[
                outside_offset : outside_offset + (kb_keep - len(page))
            ]
            widened.extend(fill)
            outside_offset += len(fill)
        if len(widened) < reserve:
            fill = inside[
                inside_offset : inside_offset + (reserve - len(widened))
            ]
            page.extend(fill)
            inside_offset += len(fill)
        ordered.extend(page)
        ordered.extend(widened)
    return tuple(ordered)


def _retain_projected_scope_lane(
    hits: tuple[projected_retrieval.ProjectedLexicalHit, ...],
    *,
    scope: str,
    candidate_depth: int,
    outside_depth: int,
    knowledge_base_name: str,
) -> tuple[projected_retrieval.ProjectedLexicalHit, ...]:
    """Keep a bounded outside-KB pool before per-lane truncation."""

    head = hits[:candidate_depth]
    if scope != "kb":
        return head
    outside = tuple(
        hit
        for hit in hits
        if not (
            (parts := PurePosixPath(hit.item_identity).parts)
            and parts[0] == knowledge_base_name
        )
    )[:outside_depth]
    retained = {hit.item_identity for hit in (*head, *outside)}
    return tuple(hit for hit in hits if hit.item_identity in retained)


def _retain_projected_bm25_hits(
    lane_hits: Mapping[
        str,
        tuple[projected_retrieval.ProjectedLexicalHit, ...],
    ],
    query: str,
) -> tuple[projected_retrieval.ProjectedLexicalHit, ...]:
    """Apply the public BM25-only gate without reopening raw item bytes."""

    bm25_hits = lane_hits.get("bm25", ())
    corroborated = {
        hit.item_identity
        for lane in ("vector", "keyword", "clip")
        for hit in lane_hits.get(lane, ())
    }
    semantic_lanes_absent = not lane_hits.get("vector") and not lane_hits.get("clip")
    query_groups = find_policy.query_word_stem_groups(query)
    retained: list[projected_retrieval.ProjectedLexicalHit] = []
    for hit in bm25_hits:
        if hit.item_identity in corroborated:
            retained.append(hit)
            continue
        text = " ".join(
            hit.search_fields[key]
            for key in sorted(
                hit.search_fields,
                key=lambda value: value.encode("utf-16-be"),
            )
        )
        present, total, content_present = find_policy.stem_word_coverage(
            frozenset(bm25.tokenize(text)),
            query_groups,
        )
        if semantic_lanes_absent:
            keep = 2 * present > total and content_present > 0
        else:
            keep = total > 0 and present == total
        if keep:
            retained.append(hit)
    return tuple(retained)


def _should_auto_rerank(
    lane_hits: Mapping[
        str,
        tuple[projected_retrieval.ProjectedLexicalHit, ...],
    ],
    query: str,
) -> bool:
    """Projected-input form of the public rerank policy."""

    if len((query or "").split()) >= 5:
        return True
    vector = [hit.item_identity for hit in lane_hits.get("vector", ())[:3]]
    lexical = [hit.item_identity for hit in lane_hits.get("bm25", ())[:3]]
    if not vector or not lexical:
        return False
    overlap = len(set(vector) & set(lexical))
    return 1.0 - overlap / max(len(vector), len(lexical)) > 0.5


def _find_projected_hits_pinned(
    vault_root: Path,
    runtime: ActiveProjectionRuntime,
    *,
    current_runtime: ActiveProjectionRuntime,
    continuation_record: _ProjectedContinuationRecord | None,
    query: str,
    limit: int,
    scope: str = "vault",
    mode: str,
    graph: bool,
    rerank: bool | None,
    auto_rerank: bool = False,
    prefer_compiled: bool = True,
    prefer_active: bool = True,
    rank_config: ranking_config.RankingConfig = ranking_config.DEFAULT_RANKING,
    principal: RequestPrincipal,
    purpose: str | None,
    continuation: str | None = None,
) -> ProjectedFindResult:
    """Acquire public candidates without opening a raw corpus/index lane."""

    from .. import readiness

    if not isinstance(runtime, ActiveProjectionRuntime):
        raise ProjectionRuntimeUnavailable(
            "governed projected retrieval is unavailable"
        )
    if continuation_record is not None:
        if (
            continuation_record.runtime.snapshot.policy.fingerprint
            != current_runtime.snapshot.policy.fingerprint
            or continuation_record.runtime.namespace.namespace_key.projector_schema_version
            != current_runtime.namespace.namespace_key.projector_schema_version
        ):
            raise _invalid_continuation()
        if runtime is not continuation_record.runtime:
            raise _invalid_continuation()
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("find: limit must be an integer from 1 through 100")
    if (
        not isinstance(query, str)
        or len(query)
        > projection_timing.PUBLIC_REQUEST_CLASSES[
            "projected-find-v1"
        ].max_query_chars
    ):
        raise ValueError("find: query must be bounded text")
    if (
        type(auto_rerank) is not bool
        or type(prefer_compiled) is not bool
        or type(prefer_active) is not bool
        or not isinstance(rank_config, ranking_config.RankingConfig)
    ):
        raise ValueError("find: projected rank policy is invalid")
    if (
        not query
        or mode not in {"keyword", "hybrid", "vector"}
        or scope not in {"kb", "vault"}
        or (graph and mode == "keyword")
        or rerank not in {None, False, True}
    ):
        raise ProjectionRuntimeUnavailable(
            "governed projected retrieval is unavailable"
        )
    if not isinstance(principal, RequestPrincipal) or not principal.resolved:
        if continuation_record is not None:
            raise _invalid_continuation()
        return ProjectedFindResult(
            hits=(),
            withheld_paths=frozenset(
                item.item_identity for item in runtime.namespace.items
            ),
        )

    declared_purpose, verified_grants = _verified_session_grants(
        Path(vault_root),
        runtime,
        principal=principal,
        purpose=purpose if purpose is not None else principal.purpose,
    )
    principal_binding = _principal_binding(principal)
    request_digest = _projected_request_digest(
        query=query,
        limit=limit,
        scope=scope,
        mode=mode,
        graph=graph,
        rerank=rerank,
        auto_rerank=auto_rerank,
        prefer_compiled=prefer_compiled,
        prefer_active=prefer_active,
    )
    if continuation_record is not None and (
        continuation_record.principal_binding != principal_binding
        or continuation_record.declared_purpose != declared_purpose
        or continuation_record.request_digest != request_digest
    ):
        raise _invalid_continuation()
    authorization = projection_authorization.build_authorization_map(
        runtime.namespace,
        policy=runtime.snapshot.policy,
        audience=principal.audience_id,
        purpose=declared_purpose,
        verified_session_grants=verified_grants,
        catalog=runtime.catalog,
    )
    authorization_digest: str | None = None
    visible_authorization_digest: str | None = None
    if continuation_record is not None:
        authorization_digest = _authorization_map_digest(authorization)
        visible_authorization_digest = _visible_authorization_digest(authorization)
        if (
            continuation_record.authorization_digest != authorization_digest
            or continuation_record.visible_authorization_digest
            != visible_authorization_digest
        ):
            raise _invalid_continuation()
    if continuation_record is not None and current_runtime is not runtime:
        current_purpose, current_grants = _verified_session_grants(
            Path(vault_root),
            current_runtime,
            principal=principal,
            purpose=purpose if purpose is not None else principal.purpose,
        )
        if current_purpose != declared_purpose:
            raise _invalid_continuation()
        current_authorization = projection_authorization.build_authorization_map(
            current_runtime.namespace,
            policy=current_runtime.snapshot.policy,
            audience=principal.audience_id,
            purpose=current_purpose,
            verified_session_grants=current_grants,
            catalog=current_runtime.catalog,
        )
        if (
            _visible_authorization_digest(current_authorization)
            != continuation_record.visible_authorization_digest
        ):
            raise _invalid_continuation()
    inline_withheld = frozenset(
        selection.item_identity
        for selection in authorization.selections
        if selection.projection_variant_id is None
    )
    withheld = (
        authorization.withheld_identities
        if not inline_withheld
        else authorization.withheld_identities.union(inline_withheld)
    )
    by_identity = {
        selection.item_identity: selection
        for selection in authorization.selections
        if selection.projection_variant_id is not None
    }
    selected_count = len(by_identity)
    selected_variants = {
        variant.item_identity: variant
        for variant in runtime.catalog.select(authorization)
    }
    has_selected_l6 = any(
        variant.decision_level == 6 for variant in selected_variants.values()
    )
    has_selected_clip = any(
        projected_retrieval.clip_variant_applicable(variant)
        for variant in selected_variants.values()
    )
    if selected_count == 0:
        return ProjectedFindResult(
            hits=(),
            withheld_paths=withheld,
            declared_purpose=declared_purpose,
        )
    page_offset = (
        0 if continuation_record is None else continuation_record.next_offset
    )
    lane_depth = selected_count
    candidate_depth = (
        min(
            selected_count,
            max(
                limit * rank_config.candidate_multiplier,
                rank_config.candidate_floor,
            ),
        )
        if continuation_record is None
        else continuation_record.candidate_depth
    )
    lane_hits: dict[str, tuple[projected_retrieval.ProjectedLexicalHit, ...]] = {}
    warming: set[str] = set()
    embeddings_module = None

    if mode == "keyword":
        lane_hits["keyword"] = runtime.lexical_index.search_keyword(
            authorization,
            query,
            k=lane_depth,
        )
    elif mode == "hybrid":
        lane_hits["bm25"] = runtime.lexical_index.search_bm25(
            authorization,
            query,
            k=lane_depth,
        )
        lane_hits["keyword"] = runtime.lexical_index.search_keyword(
            authorization,
            query,
            k=lane_depth,
        )

    if mode in {"hybrid", "vector"}:
        vector_index = runtime.vector_index
        if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
            lane_hits["vector"] = ()
        elif readiness.should_defer("embeddings"):
            warming.add("vector")
        else:
            from .. import embeddings as embeddings_module

            if (
                vector_index is None
                or vector_index.extractor_version != "projected-text-v1"
                or vector_index.model_version != embeddings_module.MODEL_NAME
            ):
                warming.add("vector")
            else:
                try:
                    query_vector = tuple(
                        float(value)
                        for value in embeddings_module.embed_texts(
                            [_projected_vector_query(query)],
                            is_query=True,
                        )[0]
                    )
                except Exception:  # noqa: BLE001 - optional query model soft-fails
                    warming.add("vector")
                else:
                    try:
                        lane_hits["vector"] = vector_index.search_vector(
                            authorization,
                            query_vector,
                            k=lane_depth,
                        )
                    except projected_retrieval.ProjectedLaneUnavailable:
                        warming.add("vector")
                    except projected_retrieval.ProjectedRetrievalUnavailable as error:
                        raise ProjectionRuntimeUnavailable(
                            "governed projected retrieval is unavailable"
                        ) from error

        clip_index = runtime.clip_index
        if not has_selected_clip or os.environ.get("EXOMEM_DISABLE_CLIP"):
            lane_hits["clip"] = ()
        else:
            if embeddings_module is None:
                from .. import embeddings as embeddings_module

            if not embeddings_module.clip_enabled():
                lane_hits["clip"] = ()
            elif readiness.should_defer("clip"):
                warming.add("clip")
            elif (
                clip_index is None
                or clip_index.extractor_version != "pixels-v1"
                or clip_index.model_version != embeddings_module.CLIP_MODEL_NAME
            ):
                warming.add("clip")
            else:
                try:
                    clip_query = tuple(
                        float(value)
                        for value in embeddings_module.embed_clip_text(query)
                    )
                except Exception:  # noqa: BLE001 - optional query model soft-fails
                    warming.add("clip")
                else:
                    try:
                        lane_hits["clip"] = clip_index.search_clip(
                            authorization,
                            clip_query,
                            k=lane_depth,
                        )
                    except projected_retrieval.ProjectedLaneUnavailable:
                        warming.add("clip")
                    except projected_retrieval.ProjectedRetrievalUnavailable as error:
                        raise ProjectionRuntimeUnavailable(
                            "governed projected retrieval is unavailable"
                        ) from error

    raw_bm25_hits = lane_hits.get("bm25", ())
    if mode == "hybrid":
        lane_hits["bm25"] = _retain_projected_bm25_hits(lane_hits, query)

    graph_degrees: dict[str, int] = {}
    graph_hops: set[str] = set()
    graph_provenance: dict[str, GraphProvenance] = {}
    if graph:
        if not has_selected_l6:
            lane_hits["graph"] = ()
        elif runtime.graph_index is None:
            warming.add("graph")
        else:
            try:
                admitted = runtime.graph_index.authorize(authorization)
                seeds: list[str] = []
                seen_seeds: set[str] = set()
                graph_seed_cap = min(
                    rank_config.graph_seed_cap,
                    candidate_depth,
                )
                for lane in ("vector", "bm25"):
                    for lane_hit in lane_hits.get(lane, ())[:graph_seed_cap]:
                        if lane_hit.item_identity not in seen_seeds:
                            seen_seeds.add(lane_hit.item_identity)
                            seeds.append(lane_hit.item_identity)
                primary_hits = {
                    "vector": lane_hits.get("vector", ()),
                    "bm25": raw_bm25_hits,
                }
                primary_set = {
                    hit.item_identity
                    for lane in ("vector", "bm25")
                    for hit in primary_hits[lane][:candidate_depth]
                }
                best_tier: dict[str, int] = {}
                first_seen: dict[str, int] = {}
                position = 0
                for seed in seeds:
                    for edge in admitted.outgoing_edges(seed):
                        target = edge.target_item_identity
                        graph_degrees[target] = graph_degrees.get(target, 0) + 1
                        if target in primary_set:
                            position += 1
                            continue
                        tier = 1 if edge.relation_type == "links_to" else 0
                        current_tier = best_tier.get(target)
                        if current_tier is None or tier < current_tier:
                            best_tier[target] = tier
                            first_seen[target] = position
                            graph_provenance[target] = GraphProvenance(
                                relation_type=edge.relation_type,
                                direction="outbound",
                                seed=seed,
                            )
                        graph_hops.add(target)
                        position += 1
                graph_ranking = sorted(
                    best_tier,
                    key=lambda identity: (
                        best_tier[identity],
                        first_seen[identity],
                        identity.encode("utf-16-be"),
                    ),
                )
                lane_hits["graph"] = tuple(
                    projected_retrieval.ProjectedLexicalHit(
                        item_identity=identity,
                        projection_variant_id=(
                            selected_variants[identity].projection_variant_id
                        ),
                        decision_level=selected_variants[identity].decision_level,
                        score=float(graph_degrees[identity]),
                        search_fields=selected_variants[identity].search_fields,
                        snippet=" ".join(
                            selected_variants[identity].search_fields[key]
                            for key in sorted(
                                selected_variants[identity].search_fields,
                                key=lambda value: value.encode("utf-16-be"),
                            )
                        )[:320].rstrip(),
                    )
                    for identity in graph_ranking
                )
            except projected_retrieval.ProjectedLaneUnavailable:
                warming.add("graph")
            except projected_retrieval.ProjectedRetrievalUnavailable as error:
                raise ProjectionRuntimeUnavailable(
                    "governed projected retrieval is unavailable"
                ) from error

    lane_order = ranking_config.LANE_ORDER[:5]
    knowledge_base_name = kb_dirname()
    lane_hits = {
        lane: _retain_projected_scope_lane(
            hits,
            scope=scope,
            candidate_depth=candidate_depth,
            outside_depth=limit,
            knowledge_base_name=knowledge_base_name,
        )
        for lane, hits in lane_hits.items()
    }
    active_lanes = [lane for lane in lane_order if lane_hits.get(lane)]
    rankings = [
        [hit.item_identity for hit in lane_hits[lane]] for lane in active_lanes
    ]
    if not rankings:
        return ProjectedFindResult(
            hits=(),
            withheld_paths=withheld,
            warming_components=tuple(
                lane for lane in ("vector", "clip", "graph", "rerank") if lane in warming
            ),
            declared_purpose=declared_purpose,
        )
    intent_weights = rank_config.intent_weights(find_policy.classify_intent(query))
    weights_by_lane = dict(zip(ranking_config.LANE_ORDER, intent_weights, strict=True))
    fused = fusion.reciprocal_rank_fusion_weighted(
        rankings,
        [weights_by_lane[lane] for lane in active_lanes],
        k=rank_config.rrf_k,
    )
    fused = [
        (
            identity,
            score
            * _projected_rank_multiplier(
                selected_variants[identity].search_fields,
                rank_config,
                prefer_compiled=prefer_compiled,
                prefer_active=prefer_active,
            ),
        )
        for identity, score in fused
    ]
    fused.sort(key=lambda item: (-item[1], item[0]))
    ordered_identities = tuple(identity for identity, _score in fused)
    reranked: tuple[projected_retrieval.ProjectedLexicalHit, ...] | None = None
    rerank_raw_scores: dict[str, float] = {}
    rerank_inputs = {
        identity: rank for rank, identity in enumerate(ordered_identities, 1)
    }
    do_rerank = mode != "keyword" and (
        rerank is True
        or (
            rerank is None
            and auto_rerank
            and _should_auto_rerank(lane_hits, query)
        )
    )
    if do_rerank and os.environ.get("EXOMEM_DISABLE_RANKING"):
        do_rerank = False
    if do_rerank and embeddings_module is None:
        from .. import embeddings as embeddings_module
    if do_rerank and not embeddings_module.ranking_enabled():
        do_rerank = False
    if do_rerank and readiness.should_defer("reranker"):
        warming.add("rerank")
        do_rerank = False
    if do_rerank:
        def score_projected_passages(
            scorer_query: str,
            passages: list[str],
        ) -> list[float]:
            if embeddings_module is None:
                raise ValueError("reranker runtime is unavailable")
            raw_scores = tuple(
                embeddings_module.rerank_pairs(scorer_query, passages)
            )
            if len(raw_scores) != len(ordered_identities):
                raise ValueError("reranker returned an invalid score count")
            pending_raw: dict[str, float] = {}
            adjusted: list[float] = []
            for identity, raw_score in zip(
                ordered_identities,
                raw_scores,
                strict=True,
            ):
                if isinstance(raw_score, bool) or not isinstance(raw_score, Real):
                    raise ValueError("reranker returned a non-finite score")
                score = float(raw_score)
                if not math.isfinite(score):
                    raise ValueError("reranker returned a non-finite score")
                pending_raw[identity] = score
                adjusted.append(
                    score
                    * _projected_rank_multiplier(
                        selected_variants[identity].search_fields,
                        rank_config,
                        prefer_compiled=prefer_compiled,
                        prefer_active=prefer_active,
                    )
                )
            rerank_raw_scores.update(pending_raw)
            return adjusted

        try:
            reranked = runtime.reranker.rerank_batch(
                authorization,
                query,
                ordered_identities,
                scorer=score_projected_passages,
                k=len(ordered_identities),
            )
            ordered_identities = tuple(hit.item_identity for hit in reranked)
        except projected_retrieval.ProjectedLaneUnavailable:
            warming.add("rerank")
        except projected_retrieval.ProjectedRetrievalUnavailable as error:
            raise ProjectionRuntimeUnavailable(
                "governed projected retrieval is unavailable"
            ) from error

    ordered_identities = _order_projected_scope(
        ordered_identities,
        scope=scope,
        page_size=limit,
        knowledge_base_name=knowledge_base_name,
    )
    visible_snapshot_digest: str | None = None
    if continuation_record is not None:
        visible_snapshot_digest = _visible_snapshot_digest(
            ordered_identities,
            selected_variants,
        )
        if (
            continuation_record.visible_snapshot_digest
            != visible_snapshot_digest
            or page_offset >= len(ordered_identities)
        ):
            raise _invalid_continuation()
    page_end = min(page_offset + limit, len(ordered_identities))
    next_continuation = None
    if page_end < len(ordered_identities):
        if authorization_digest is None:
            authorization_digest = _authorization_map_digest(authorization)
        if visible_authorization_digest is None:
            visible_authorization_digest = _visible_authorization_digest(
                authorization
            )
        if visible_snapshot_digest is None:
            visible_snapshot_digest = _visible_snapshot_digest(
                ordered_identities,
                selected_variants,
            )
        next_continuation = _register_projected_continuation(
            Path(vault_root),
            runtime=runtime,
            principal_binding=principal_binding,
            declared_purpose=declared_purpose,
            request_digest=request_digest,
            authorization_digest=authorization_digest,
            visible_authorization_digest=visible_authorization_digest,
            visible_snapshot_digest=visible_snapshot_digest,
            next_offset=page_end,
            candidate_depth=candidate_depth,
            replace_existing=continuation_record is None,
        )

    hits_by_lane = {
        lane: {hit.item_identity: hit for hit in hits}
        for lane, hits in lane_hits.items()
    }
    ranks_by_lane = {
        lane: {
            hit.item_identity: rank for rank, hit in enumerate(lane_values, 1)
        }
        for lane, lane_values in lane_hits.items()
    }
    reranked_by_identity = (
        {} if reranked is None else {hit.item_identity: hit for hit in reranked}
    )
    hits: list[Hit] = []
    for final_rank, identity in enumerate(
        ordered_identities[page_offset:page_end],
        page_offset + 1,
    ):
        source_hit = reranked_by_identity.get(identity)
        if source_hit is None:
            source_hit = next(
                hits_by_lane[lane][identity]
                for lane in lane_order
                if identity in hits_by_lane.get(lane, {})
            )
        lane_ranks = {
            lane: ranks_for_lane[identity]
            for lane, ranks_for_lane in ranks_by_lane.items()
            if identity in ranks_for_lane
        }
        lane_scores = {
            lane: lane_values[identity].score
            for lane, lane_values in hits_by_lane.items()
            if identity in lane_values and lane in {"vector", "clip"}
        }
        hits.append(
            _wire_hit(
                source_hit,
                selection=by_identity[identity],
                rank=final_rank,
                knowledge_base_name=knowledge_base_name,
                keyword=mode == "keyword",
                lane_ranks=lane_ranks,
                lane_scores=lane_scores,
                graph_in_degree=graph_degrees.get(identity, 0),
                graph_hop=identity in graph_hops,
                graph_provenance=graph_provenance.get(identity),
                rerank_score=(
                    reranked_by_identity[identity].score
                    if identity in reranked_by_identity
                    else None
                ),
                rerank_raw_score=rerank_raw_scores.get(identity),
                rerank_input_rank=(
                    rerank_inputs[identity]
                    if identity in reranked_by_identity
                    else None
                ),
            )
        )
    return ProjectedFindResult(
        hits=tuple(hits),
        withheld_paths=withheld,
        warming_components=tuple(
            lane for lane in ("vector", "clip", "graph", "rerank") if lane in warming
        ),
        declared_purpose=declared_purpose,
        continuation=next_continuation,
    )


def find_projected_hits(
    vault_root: Path,
    runtime: ActiveProjectionRuntime,
    *,
    query: str,
    limit: int,
    scope: str = "vault",
    mode: str,
    graph: bool,
    rerank: bool | None,
    auto_rerank: bool = False,
    prefer_compiled: bool = True,
    prefer_active: bool = True,
    rank_config: ranking_config.RankingConfig = ranking_config.DEFAULT_RANKING,
    principal: RequestPrincipal,
    purpose: str | None,
    continuation: str | None = None,
) -> ProjectedFindResult:
    """Acquire one exact runtime pin before any projected request work."""

    with _projection_runtime_request_scope(
        Path(vault_root),
        runtime,
        continuation=continuation,
    ) as (selected, continuation_record):
        return _find_projected_hits_pinned(
            Path(vault_root),
            selected,
            current_runtime=runtime,
            continuation_record=continuation_record,
            query=query,
            limit=limit,
            scope=scope,
            mode=mode,
            graph=graph,
            rerank=rerank,
            auto_rerank=auto_rerank,
            prefer_compiled=prefer_compiled,
            prefer_active=prefer_active,
            rank_config=rank_config,
            principal=principal,
            purpose=purpose,
            continuation=continuation,
        )


__all__ = [
    "ActiveProjectionRuntime",
    "ProjectedContinuationUnavailable",
    "ProjectedFindResult",
    "ProjectionRuntimeUnavailable",
    "classify_projected_completion_boundary",
    "find_projected_hits",
    "has_preactivated_projection_runtime",
    "load_active_projection_runtime",
    "preactivate_projection_runtime",
    "projection_namespace_runtime_pins",
    "requires_fixed_projected_completion",
    "requires_projected_read_boundary",
]
