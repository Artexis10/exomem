"""Immutable persistence for projected vector, CLIP, and graph measurements.

Each store is bound to one verified projection namespace and one exact
lane/extractor/model family.  The family is a subkey beneath the namespace: a
model change creates another immutable store without changing or aliasing the
policy/projector/catalog namespace identity.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import struct
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias, cast

from .. import reserved_paths, state_paths
from . import projected_graph, projected_retrieval, projection_store, projections

SCHEMA_USER_VERSION = 1
_DESCRIPTOR_ID = "authorization-projections"
_OWNER = "governance.projections"
_STORE_FILENAME = "rows.sqlite"
_STORE_STATUS = "measurement-rows-ready"
_ROW_DOMAIN = b"exomem.authorization-projection-measurement-row.v1"
_STORE_DOMAIN = b"exomem.authorization-projection-measurements.v1"
_CLIP_PAYLOAD_PREFIX = b"exomem.projection-clip-samples.v1\x00"
_LANES = frozenset({"vector", "clip", "graph"})
_HEX = frozenset("0123456789abcdef")
_TABLES = frozenset({"measurement_meta", "measurement_rows"})
_TRIGGERS = frozenset(
    f"{table}_no_{operation}"
    for table in _TABLES
    for operation in ("insert", "update", "delete")
)


class MeasurementStoreError(RuntimeError):
    """Base class for content-free measurement-store refusals."""


class MeasurementStoreMismatch(MeasurementStoreError):
    """Persisted measurements do not match their immutable commitment."""


ProjectionMeasurement: TypeAlias = (
    projected_retrieval.ProjectionVectorMeasurement
    | projected_retrieval.ProjectionClipMeasurement
    | projected_graph.ProjectionGraphMeasurement
)
ProjectionNamespace: TypeAlias = (
    projection_store.VerifiedProjectionNamespace
    | projection_store.PreparedProjectionNamespace
)


def _bounded_text(value: object, name: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise projections.ProjectionCanonicalizationError(
            f"{name} must be bounded non-empty text"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise projections.ProjectionCanonicalizationError(
            f"{name} contains an invalid Unicode scalar"
        ) from error
    return value


def _digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX for character in value)
    ):
        raise MeasurementStoreMismatch(f"{name} must be one lowercase SHA-256 digest")
    return value


def _stored_integer(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise MeasurementStoreMismatch(f"{name} does not verify")
    return value


def _framed(domain: bytes, fields: Sequence[bytes]) -> bytes:
    output = bytearray(domain)
    output.append(0)
    for field in fields:
        if len(field) > (1 << 32) - 1:
            raise projections.ProjectionCanonicalizationError(
                "measurement identity field is too large"
            )
        output.extend(len(field).to_bytes(4, "big"))
        output.extend(field)
    return bytes(output)


def _framed_digest(domain: bytes, fields: Sequence[bytes]) -> str:
    return hashlib.sha256(_framed(domain, fields)).hexdigest()


@dataclass(frozen=True, slots=True)
class MeasurementFamilyKey:
    """One versioned measurement family beneath an immutable namespace."""

    namespace_key: projections.ProjectionNamespaceKey
    lane: str
    extractor_version: str
    model_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.namespace_key, projections.ProjectionNamespaceKey):
            raise projections.ProjectionCanonicalizationError(
                "measurement family namespace key is invalid"
            )
        if self.lane not in _LANES:
            raise projections.ProjectionCanonicalizationError(
                "measurement family lane is not registered"
            )
        _bounded_text(
            self.extractor_version,
            "measurement family extractor version",
            maximum=256,
        )
        _bounded_text(
            self.model_version,
            "measurement family model version",
            maximum=256,
        )

    @property
    def family_id(self) -> str:
        return projection_store.projection_measurement_family_id(
            self.namespace_key,
            lane=self.lane,
            extractor_version=self.extractor_version,
            model_version=self.model_version,
        )


@dataclass(frozen=True, slots=True)
class MeasurementStoreManifest:
    """Content commitment for one complete immutable measurement family."""

    family: MeasurementFamilyKey
    measurement_count: int
    vector_dimension: int | None
    graph_edge_count: int
    rows_digest: str


@dataclass(frozen=True, slots=True)
class _StoredRow:
    measurement: ProjectionMeasurement
    payload: bytes
    row_digest: str


def measurement_root(
    manifest: MeasurementStoreManifest,
) -> projection_store.ProjectionMeasurementRoot:
    """Return the exact active-tuple commitment for one verified family."""

    if not isinstance(manifest, MeasurementStoreManifest):
        raise MeasurementStoreMismatch("measurement store manifest is unavailable")
    family = manifest.family
    return projection_store.ProjectionMeasurementRoot(
        namespace_key=family.namespace_key,
        family_id=family.family_id,
        lane=family.lane,
        extractor_version=family.extractor_version,
        model_version=family.model_version,
        measurement_count=manifest.measurement_count,
        vector_dimension=manifest.vector_dimension,
        graph_edge_count=manifest.graph_edge_count,
        rows_digest=manifest.rows_digest,
    )


def measurement_store_path(vault_root: Path, family: MeasurementFamilyKey) -> Path:
    """Return the private path derived only from exact family identity."""

    if not isinstance(family, MeasurementFamilyKey):
        raise projections.ProjectionCanonicalizationError(
            "measurement family key is invalid"
        )
    return (
        state_paths.vault_state_dir(vault_root)
        / ".authorization-projections"
        / family.namespace_key.namespace_id
        / "measurements"
        / family.lane
        / family.family_id
        / _STORE_FILENAME
    )


def _measurement_key(measurement: ProjectionMeasurement) -> projections.MeasurementKey:
    key = getattr(measurement, "measurement_key", None)
    if not isinstance(key, projections.MeasurementKey):
        raise projections.ProjectionCanonicalizationError(
            "projection measurement key is invalid"
        )
    return key


def _validate_measurements(
    namespace: ProjectionNamespace,
    family: MeasurementFamilyKey,
    measurements: Iterable[ProjectionMeasurement],
) -> tuple[ProjectionMeasurement, ...]:
    if not isinstance(
        namespace,
        (
            projection_store.VerifiedProjectionNamespace,
            projection_store.PreparedProjectionNamespace,
        ),
    ):
        raise MeasurementStoreMismatch("verified projection namespace is unavailable")
    if family.namespace_key != namespace.namespace_key:
        raise MeasurementStoreMismatch(
            "measurement family does not match the verified namespace"
        )
    rows = tuple(measurements)
    if family.lane == "vector":
        projected_retrieval.ProjectedVectorIndex(
            namespace,
            rows,
            extractor_version=family.extractor_version,
            model_version=family.model_version,
        )
    elif family.lane == "clip":
        projected_retrieval.ProjectedClipIndex(
            namespace,
            rows,
            extractor_version=family.extractor_version,
            model_version=family.model_version,
        )
    else:
        projected_graph.ProjectedGraphIndex(
            namespace,
            rows,
            extractor_version=family.extractor_version,
            model_version=family.model_version,
        )
    return tuple(
        sorted(
            rows,
            key=lambda row: _measurement_key(row).projection_variant_id,
        )
    )


def _vector_payload(vector: tuple[float, ...]) -> bytes:
    # IEEE-754 has two zero encodings with identical retrieval semantics.  Keep
    # one canonical on-disk representation so equivalent measurements cannot
    # produce different family roots.
    canonical = tuple(0.0 if value == 0 else value for value in vector)
    return struct.pack(f">{len(canonical)}d", *canonical)


def _clip_payload(
    measurement: projected_retrieval.ProjectionClipMeasurement,
) -> bytes:
    if (
        len(measurement.samples) == 1
        and measurement.samples[0].frame_timestamp_ms is None
    ):
        return _vector_payload(measurement.samples[0].vector)
    output = bytearray(_CLIP_PAYLOAD_PREFIX)
    output.extend(len(measurement.samples).to_bytes(4, "big"))
    for sample in measurement.samples:
        if sample.frame_timestamp_ms is None:
            output.append(0)
        else:
            output.append(1)
            output.extend(sample.frame_timestamp_ms.to_bytes(4, "big"))
        output.extend(_vector_payload(sample.vector))
    return bytes(output)


def _graph_payload(measurement: projected_graph.ProjectionGraphMeasurement) -> bytes:
    return projections.canonical_jcs(
        {
            "edges": [
                {
                    "source_item_identity": edge.source_item_identity,
                    "target_item_identity": edge.target_item_identity,
                    "relation_type": edge.relation_type,
                }
                for edge in measurement.edges
            ]
        }
    )


def _measurement_payload(measurement: ProjectionMeasurement) -> bytes:
    if isinstance(measurement, projected_retrieval.ProjectionVectorMeasurement):
        return _vector_payload(measurement.vector)
    if isinstance(measurement, projected_retrieval.ProjectionClipMeasurement):
        return _clip_payload(measurement)
    if isinstance(measurement, projected_graph.ProjectionGraphMeasurement):
        return _graph_payload(measurement)
    raise projections.ProjectionCanonicalizationError(
        "projection measurement has an invalid type"
    )


def _stored_row(measurement: ProjectionMeasurement) -> _StoredRow:
    key = _measurement_key(measurement)
    payload = _measurement_payload(measurement)
    return _StoredRow(
        measurement=measurement,
        payload=payload,
        row_digest=_framed_digest(
            _ROW_DOMAIN,
            (
                key.projection_variant_id.encode("ascii"),
                key.lane.encode("ascii"),
                key.extractor_version.encode("utf-8"),
                key.model_version.encode("utf-8"),
                payload,
            ),
        ),
    )


def _materialize(
    namespace: ProjectionNamespace,
    family: MeasurementFamilyKey,
    measurements: Iterable[ProjectionMeasurement],
) -> tuple[tuple[_StoredRow, ...], MeasurementStoreManifest]:
    rows = tuple(
        _stored_row(measurement)
        for measurement in _validate_measurements(namespace, family, measurements)
    )
    vector_dimension: int | None = None
    graph_edge_count = 0
    if family.lane in {"vector", "clip"} and rows:
        first = rows[0].measurement
        assert isinstance(
            first,
            (
                projected_retrieval.ProjectionVectorMeasurement,
                projected_retrieval.ProjectionClipMeasurement,
            ),
        )
        vector_dimension = (
            len(first.samples[0].vector)
            if isinstance(first, projected_retrieval.ProjectionClipMeasurement)
            else len(first.vector)
        )
    if family.lane == "graph":
        graph_edge_count = sum(
            len(row.measurement.edges)
            for row in rows
            if isinstance(row.measurement, projected_graph.ProjectionGraphMeasurement)
        )
        projections.require_supported_capacity(graph_edges=graph_edge_count)
    dimension_field = "-" if vector_dimension is None else str(vector_dimension)
    rows_digest = _framed_digest(
        _STORE_DOMAIN,
        (
            family.namespace_key.policy_fingerprint.encode("ascii"),
            str(family.namespace_key.projector_schema_version).encode("ascii"),
            str(family.namespace_key.catalog_generation).encode("ascii"),
            family.namespace_key.namespace_id.encode("ascii"),
            family.family_id.encode("ascii"),
            family.lane.encode("ascii"),
            family.extractor_version.encode("utf-8"),
            family.model_version.encode("utf-8"),
            str(len(rows)).encode("ascii"),
            dimension_field.encode("ascii"),
            str(graph_edge_count).encode("ascii"),
            *(row.row_digest.encode("ascii") for row in rows),
        ),
    )
    return rows, MeasurementStoreManifest(
        family=family,
        measurement_count=len(rows),
        vector_dimension=vector_dimension,
        graph_edge_count=graph_edge_count,
        rows_digest=rows_digest,
    )


def _crash_point(_point: str) -> None:
    """Test-only crash barrier hook."""


def _schema_names(connection: sqlite3.Connection, kind: str) -> frozenset[str]:
    return frozenset(
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type=? AND name NOT LIKE 'sqlite_%'",
            (kind,),
        )
    )


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE measurement_meta ("
        "singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
        "policy_fingerprint TEXT NOT NULL CHECK(length(policy_fingerprint)=64), "
        "projector_schema_version INTEGER NOT NULL "
        "CHECK(projector_schema_version>0), "
        "catalog_generation INTEGER NOT NULL CHECK(catalog_generation>=0), "
        "namespace_id TEXT NOT NULL CHECK(length(namespace_id)=64), "
        "family_id TEXT NOT NULL CHECK(length(family_id)=64), "
        "lane TEXT NOT NULL CHECK(lane IN ('vector','clip','graph')), "
        "extractor_version TEXT NOT NULL, model_version TEXT NOT NULL, "
        "measurement_count INTEGER NOT NULL CHECK(measurement_count>=0), "
        "vector_dimension INTEGER NOT NULL CHECK(vector_dimension>=0), "
        "graph_edge_count INTEGER NOT NULL CHECK(graph_edge_count>=0), "
        "rows_digest TEXT NOT NULL CHECK(length(rows_digest)=64), "
        "status TEXT NOT NULL CHECK(status='measurement-rows-ready'))"
    )
    connection.execute(
        "CREATE TABLE measurement_rows ("
        "projection_variant_id TEXT PRIMARY KEY "
        "CHECK(length(projection_variant_id)=64), "
        "payload BLOB NOT NULL, row_digest TEXT NOT NULL "
        "CHECK(length(row_digest)=64))"
    )
    connection.execute(f"PRAGMA user_version={SCHEMA_USER_VERSION}")


def _create_immutable_triggers(connection: sqlite3.Connection) -> None:
    for table in sorted(_TABLES):
        for operation in ("insert", "update", "delete"):
            connection.execute(
                f"CREATE TRIGGER {table}_no_{operation} BEFORE {operation.upper()} "
                f"ON {table} BEGIN SELECT RAISE(ABORT, "
                f"'{table} rows are immutable'); END"
            )


def _insert_material(
    connection: sqlite3.Connection,
    rows: tuple[_StoredRow, ...],
    manifest: MeasurementStoreManifest,
) -> None:
    family = manifest.family
    key = family.namespace_key
    connection.execute(
        "INSERT INTO measurement_meta "
        "(singleton, policy_fingerprint, projector_schema_version, "
        "catalog_generation, namespace_id, family_id, lane, extractor_version, "
        "model_version, measurement_count, vector_dimension, graph_edge_count, "
        "rows_digest, status) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            key.policy_fingerprint,
            key.projector_schema_version,
            key.catalog_generation,
            key.namespace_id,
            family.family_id,
            family.lane,
            family.extractor_version,
            family.model_version,
            manifest.measurement_count,
            manifest.vector_dimension or 0,
            manifest.graph_edge_count,
            manifest.rows_digest,
            _STORE_STATUS,
        ),
    )
    connection.executemany(
        "INSERT INTO measurement_rows "
        "(projection_variant_id, payload, row_digest) VALUES (?, ?, ?)",
        (
            (
                _measurement_key(row.measurement).projection_variant_id,
                row.payload,
                row.row_digest,
            )
            for row in rows
        ),
    )


def _metadata_manifest(
    connection: sqlite3.Connection,
    family: MeasurementFamilyKey,
) -> MeasurementStoreManifest:
    user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if _stored_integer(user_version, "measurement store schema") != SCHEMA_USER_VERSION:
        raise MeasurementStoreMismatch("measurement store schema does not verify")
    if _schema_names(connection, "table") != _TABLES:
        raise MeasurementStoreMismatch("measurement store table set does not verify")
    if _schema_names(connection, "trigger") != _TRIGGERS:
        raise MeasurementStoreMismatch("measurement store trigger set does not verify")
    rows = connection.execute("SELECT * FROM measurement_meta").fetchall()
    if len(rows) != 1:
        raise MeasurementStoreMismatch("measurement store metadata does not verify")
    row = rows[0]
    key = family.namespace_key
    if tuple(row[:9]) != (
        1,
        key.policy_fingerprint,
        key.projector_schema_version,
        key.catalog_generation,
        key.namespace_id,
        family.family_id,
        family.lane,
        family.extractor_version,
        family.model_version,
    ) or row[13] != _STORE_STATUS:
        raise MeasurementStoreMismatch("measurement store family does not verify")
    measurement_count = _stored_integer(row[9], "measurement store row count")
    stored_dimension = _stored_integer(row[10], "measurement store dimension")
    graph_edge_count = _stored_integer(row[11], "measurement store edge count")
    if family.lane == "graph":
        if stored_dimension != 0:
            raise MeasurementStoreMismatch("measurement store dimension does not verify")
        vector_dimension: int | None = None
    else:
        if (measurement_count == 0 and stored_dimension != 0) or (
            measurement_count > 0 and stored_dimension <= 0
        ):
            raise MeasurementStoreMismatch("measurement store dimension does not verify")
        if graph_edge_count != 0:
            raise MeasurementStoreMismatch("measurement store edge count does not verify")
        vector_dimension = stored_dimension if measurement_count > 0 else None
    return MeasurementStoreManifest(
        family=family,
        measurement_count=measurement_count,
        vector_dimension=vector_dimension,
        graph_edge_count=graph_edge_count,
        rows_digest=_digest(row[12], "measurement rows digest"),
    )


def _decode_vector(
    payload: bytes,
    *,
    dimension: int,
) -> tuple[float, ...]:
    if dimension <= 0 or len(payload) != dimension * 8:
        raise MeasurementStoreMismatch("measurement vector does not verify")
    try:
        return tuple(value[0] for value in struct.iter_unpack(">d", payload))
    except struct.error as error:
        raise MeasurementStoreMismatch("measurement vector does not verify") from error


def _decode_clip_samples(
    payload: bytes,
    *,
    dimension: int,
) -> tuple[projected_retrieval.ProjectionClipSample, ...]:
    if len(payload) == dimension * 8:
        return (
            projected_retrieval.ProjectionClipSample(
                frame_timestamp_ms=None,
                vector=_decode_vector(payload, dimension=dimension),
            ),
        )
    if not payload.startswith(_CLIP_PAYLOAD_PREFIX):
        raise MeasurementStoreMismatch("CLIP measurement samples do not verify")
    cursor = len(_CLIP_PAYLOAD_PREFIX)
    if len(payload) < cursor + 4:
        raise MeasurementStoreMismatch("CLIP measurement samples do not verify")
    count = int.from_bytes(payload[cursor : cursor + 4], "big")
    cursor += 4
    if not 1 <= count <= projected_retrieval.MAX_PROJECTED_CLIP_SAMPLES_PER_VARIANT:
        raise MeasurementStoreMismatch("CLIP measurement samples do not verify")
    vector_bytes = dimension * 8
    samples: list[projected_retrieval.ProjectionClipSample] = []
    for _index in range(count):
        if cursor >= len(payload):
            raise MeasurementStoreMismatch("CLIP measurement samples do not verify")
        tag = payload[cursor]
        cursor += 1
        if tag == 0:
            timestamp_ms = None
        elif tag == 1 and len(payload) >= cursor + 4:
            timestamp_ms = int.from_bytes(payload[cursor : cursor + 4], "big")
            cursor += 4
        else:
            raise MeasurementStoreMismatch("CLIP measurement samples do not verify")
        if len(payload) < cursor + vector_bytes:
            raise MeasurementStoreMismatch("CLIP measurement samples do not verify")
        samples.append(
            projected_retrieval.ProjectionClipSample(
                frame_timestamp_ms=timestamp_ms,
                vector=_decode_vector(
                    payload[cursor : cursor + vector_bytes],
                    dimension=dimension,
                ),
            )
        )
        cursor += vector_bytes
    if cursor != len(payload):
        raise MeasurementStoreMismatch("CLIP measurement samples do not verify")
    return tuple(samples)


def _decode_graph(payload: bytes) -> tuple[projected_graph.ProjectionGraphEdge, ...]:
    try:
        value = json.loads(payload)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MeasurementStoreMismatch("graph measurement does not verify") from error
    if (
        not isinstance(value, dict)
        or set(value) != {"edges"}
        or not isinstance(value["edges"], list)
        or projections.canonical_jcs(value) != payload
    ):
        raise MeasurementStoreMismatch("graph measurement does not verify")
    try:
        return tuple(
            projected_graph.ProjectionGraphEdge(
                source_item_identity=edge["source_item_identity"],
                target_item_identity=edge["target_item_identity"],
                relation_type=edge["relation_type"],
            )
            for edge in value["edges"]
            if isinstance(edge, dict)
            and set(edge)
            == {
                "source_item_identity",
                "target_item_identity",
                "relation_type",
            }
        )
    except (KeyError, TypeError, projections.ProjectionError) as error:
        raise MeasurementStoreMismatch("graph measurement does not verify") from error


def _decode_measurement(
    family: MeasurementFamilyKey,
    manifest: MeasurementStoreManifest,
    projection_variant_id: str,
    payload: bytes,
) -> ProjectionMeasurement:
    key = projections.MeasurementKey(
        projection_variant_id=_digest(
            projection_variant_id,
            "projection variant id",
        ),
        lane=family.lane,
        extractor_version=family.extractor_version,
        model_version=family.model_version,
    )
    try:
        if family.lane == "vector":
            assert manifest.vector_dimension is not None
            return projected_retrieval.ProjectionVectorMeasurement(
                measurement_key=key,
                vector=_decode_vector(payload, dimension=manifest.vector_dimension),
            )
        if family.lane == "clip":
            assert manifest.vector_dimension is not None
            return projected_retrieval.ProjectionClipMeasurement(
                measurement_key=key,
                samples=_decode_clip_samples(
                    payload,
                    dimension=manifest.vector_dimension,
                ),
            )
        edges = _decode_graph(payload)
        if len(edges) != len(json.loads(payload)["edges"]):
            raise MeasurementStoreMismatch("graph measurement does not verify")
        return projected_graph.ProjectionGraphMeasurement(
            measurement_key=key,
            edges=edges,
        )
    except (AssertionError, projections.ProjectionError) as error:
        raise MeasurementStoreMismatch("projection measurement does not verify") from error


def _verified_measurements(
    connection: sqlite3.Connection,
    manifest: MeasurementStoreManifest,
) -> tuple[ProjectionMeasurement, ...]:
    measurements: list[ProjectionMeasurement] = []
    rows = connection.execute(
        "SELECT projection_variant_id, payload, row_digest FROM measurement_rows "
        "ORDER BY projection_variant_id"
    ).fetchall()
    for row in rows:
        projection_variant_id = _digest(row[0], "projection variant id")
        if not isinstance(row[1], bytes):
            raise MeasurementStoreMismatch("measurement payload does not verify")
        payload = row[1]
        stored_row_digest = _digest(row[2], "measurement row digest")
        measurement = _decode_measurement(
            manifest.family,
            manifest,
            projection_variant_id,
            payload,
        )
        material = _stored_row(measurement)
        if not hmac.compare_digest(material.payload, payload) or not hmac.compare_digest(
            material.row_digest,
            stored_row_digest,
        ):
            raise MeasurementStoreMismatch("measurement row digest does not verify")
        measurements.append(measurement)
    return tuple(measurements)


def _verify_connection(
    connection: sqlite3.Connection,
    *,
    namespace: ProjectionNamespace,
    family: MeasurementFamilyKey,
    expected_rows_digest: str,
) -> tuple[MeasurementStoreManifest, tuple[ProjectionMeasurement, ...]]:
    manifest = _metadata_manifest(connection, family)
    if not hmac.compare_digest(
        manifest.rows_digest,
        _digest(expected_rows_digest, "expected measurement rows digest"),
    ):
        raise MeasurementStoreMismatch(
            "measurement store digest does not match its expected root"
        )
    stored_count = int(
        connection.execute("SELECT COUNT(*) FROM measurement_rows").fetchone()[0]
    )
    if stored_count != manifest.measurement_count:
        raise MeasurementStoreMismatch("measurement store row count does not verify")
    measurements = _verified_measurements(connection, manifest)
    material, recomputed = _materialize(namespace, family, measurements)
    del material
    if recomputed != manifest:
        raise MeasurementStoreMismatch("measurement store rows do not verify")
    return manifest, measurements


def _connect(
    path: Path,
    *,
    readonly: bool,
    vault_root: Path,
    database: Path,
) -> sqlite3.Connection:
    if readonly:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    else:
        connection = sqlite3.connect(path)
    try:
        reserved_paths._publish_sqlite_owner_family(
            vault_root,
            database,
            _DESCRIPTOR_ID,
            connection,
            preserve_existing=True,
        )
        if readonly:
            connection.execute("PRAGMA query_only=ON")
        else:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        if readonly:
            connection.execute("BEGIN")
        return connection
    except BaseException:
        connection.close()
        raise


def stage_measurement_store(
    vault_root: Path,
    *,
    namespace: ProjectionNamespace,
    family: MeasurementFamilyKey,
    measurements: Iterable[ProjectionMeasurement],
) -> MeasurementStoreManifest:
    """Atomically build or exactly replay one immutable measurement family."""

    material, expected = _materialize(namespace, family, measurements)
    root = Path(vault_root)
    database = measurement_store_path(root, family)
    with reserved_paths._subsystem_authority_scope(_OWNER):
        with reserved_paths._identity_coordination_scope(
            root,
            descriptor_ids=(_DESCRIPTOR_ID,),
        ):
            with reserved_paths._sqlite_owner_target_scope(
                root,
                database,
                _DESCRIPTOR_ID,
                create=True,
            ) as retained:
                connection = _connect(
                    retained,
                    readonly=False,
                    vault_root=root,
                    database=database,
                )
                try:
                    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                    if version == SCHEMA_USER_VERSION:
                        connection.execute("BEGIN")
                        manifest, _measurements = _verify_connection(
                            connection,
                            namespace=namespace,
                            family=family,
                            expected_rows_digest=expected.rows_digest,
                        )
                        return manifest
                    if version != 0 or _schema_names(connection, "table"):
                        raise MeasurementStoreMismatch(
                            "measurement store immutable state does not verify"
                        )
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        _create_schema(connection)
                        _insert_material(connection, material, expected)
                        _create_immutable_triggers(connection)
                        _crash_point("before-commit")
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
                    _crash_point("after-commit")
                    connection.execute("BEGIN")
                    manifest, _measurements = _verify_connection(
                        connection,
                        namespace=namespace,
                        family=family,
                        expected_rows_digest=expected.rows_digest,
                    )
                    return manifest
                except sqlite3.Error as error:
                    raise MeasurementStoreMismatch(
                        "measurement store transaction does not verify"
                    ) from error
                finally:
                    reserved_paths._publish_sqlite_owner_family(
                        root,
                        database,
                        _DESCRIPTOR_ID,
                        connection,
                        preserve_existing=True,
                    )
                    connection.close()


def preview_measurement_store(
    *,
    namespace: ProjectionNamespace,
    family: MeasurementFamilyKey,
    measurements: Iterable[ProjectionMeasurement],
) -> MeasurementStoreManifest:
    """Validate and commit to one measurement family without writing its store."""

    _material, manifest = _materialize(namespace, family, measurements)
    return manifest


def verify_measurement_store(
    vault_root: Path,
    *,
    namespace: ProjectionNamespace,
    family: MeasurementFamilyKey,
    expected_rows_digest: str,
) -> MeasurementStoreManifest:
    """Verify every row against one namespace and expected family root."""

    manifest, _measurements = load_measurement_store(
        vault_root,
        namespace=namespace,
        family=family,
        expected_rows_digest=expected_rows_digest,
    )
    return manifest


def load_measurement_store(
    vault_root: Path,
    *,
    namespace: ProjectionNamespace,
    family: MeasurementFamilyKey,
    expected_rows_digest: str,
) -> tuple[MeasurementStoreManifest, tuple[ProjectionMeasurement, ...]]:
    """Load one family only after its namespace and immutable root verify."""

    if family.namespace_key != namespace.namespace_key:
        raise MeasurementStoreMismatch(
            "measurement family does not match the verified namespace"
        )
    root = Path(vault_root)
    database = measurement_store_path(root, family)
    with reserved_paths._subsystem_authority_scope(_OWNER):
        with reserved_paths._identity_coordination_scope(
            root,
            descriptor_ids=(_DESCRIPTOR_ID,),
            identity_may_change=False,
        ):
            with reserved_paths._sqlite_owner_target_scope(
                root,
                database,
                _DESCRIPTOR_ID,
                create=False,
            ) as retained:
                connection = _connect(
                    retained,
                    readonly=True,
                    vault_root=root,
                    database=database,
                )
                try:
                    return _verify_connection(
                        connection,
                        namespace=namespace,
                        family=family,
                        expected_rows_digest=expected_rows_digest,
                    )
                except sqlite3.Error as error:
                    raise MeasurementStoreMismatch(
                        "measurement store read does not verify"
                    ) from error
                finally:
                    reserved_paths._publish_sqlite_owner_family(
                        root,
                        database,
                        _DESCRIPTOR_ID,
                        connection,
                        preserve_existing=True,
                    )
                    connection.close()


def load_vector_index(
    vault_root: Path,
    *,
    namespace: projection_store.VerifiedProjectionNamespace,
    family: MeasurementFamilyKey,
    expected_rows_digest: str,
) -> tuple[MeasurementStoreManifest, projected_retrieval.ProjectedVectorIndex]:
    """Load and rebuild one verified principal-free projected vector index."""

    if family.lane != "vector":
        raise MeasurementStoreMismatch("measurement family is not a vector lane")
    manifest, measurements = load_measurement_store(
        vault_root,
        namespace=namespace,
        family=family,
        expected_rows_digest=expected_rows_digest,
    )
    index = projected_retrieval.ProjectedVectorIndex(
        namespace,
        cast(
            tuple[projected_retrieval.ProjectionVectorMeasurement, ...],
            measurements,
        ),
        extractor_version=family.extractor_version,
        model_version=family.model_version,
    )
    return manifest, index


def load_clip_index(
    vault_root: Path,
    *,
    namespace: projection_store.VerifiedProjectionNamespace,
    family: MeasurementFamilyKey,
    expected_rows_digest: str,
) -> tuple[MeasurementStoreManifest, projected_retrieval.ProjectedClipIndex]:
    """Load and rebuild one verified principal-free projected CLIP index."""

    if family.lane != "clip":
        raise MeasurementStoreMismatch("measurement family is not a CLIP lane")
    manifest, measurements = load_measurement_store(
        vault_root,
        namespace=namespace,
        family=family,
        expected_rows_digest=expected_rows_digest,
    )
    index = projected_retrieval.ProjectedClipIndex(
        namespace,
        cast(
            tuple[projected_retrieval.ProjectionClipMeasurement, ...],
            measurements,
        ),
        extractor_version=family.extractor_version,
        model_version=family.model_version,
    )
    return manifest, index


def load_graph_index(
    vault_root: Path,
    *,
    namespace: projection_store.VerifiedProjectionNamespace,
    family: MeasurementFamilyKey,
    expected_rows_digest: str,
) -> tuple[MeasurementStoreManifest, projected_graph.ProjectedGraphIndex]:
    """Load and rebuild one verified principal-free projected graph index."""

    if family.lane != "graph":
        raise MeasurementStoreMismatch("measurement family is not a graph lane")
    manifest, measurements = load_measurement_store(
        vault_root,
        namespace=namespace,
        family=family,
        expected_rows_digest=expected_rows_digest,
    )
    index = projected_graph.ProjectedGraphIndex(
        namespace,
        cast(
            tuple[projected_graph.ProjectionGraphMeasurement, ...],
            measurements,
        ),
        extractor_version=family.extractor_version,
        model_version=family.model_version,
    )
    return manifest, index


__all__ = [
    "MeasurementFamilyKey",
    "MeasurementStoreError",
    "MeasurementStoreManifest",
    "MeasurementStoreMismatch",
    "load_clip_index",
    "load_graph_index",
    "load_measurement_store",
    "load_vector_index",
    "measurement_root",
    "measurement_store_path",
    "preview_measurement_store",
    "stage_measurement_store",
    "verify_measurement_store",
]
