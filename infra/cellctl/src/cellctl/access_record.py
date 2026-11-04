"""Derive received access metadata from exact retained object versions."""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime

import boto3
from botocore.config import Config

from . import db
from .state import PodPlacement

logger = logging.getLogger("cellctl")

# Closed fields and values of the versioned access-record wire schema. No content classifier.
FIELDS = (
    "kind",
    "source_at",
    "node",
    "boot_id",
    "event_id",
    "identity",
    "action",
    "target",
    "namespace",
    "resource",
    "subresource",
    "session_id",
    "authentication_method",
    "fingerprint",
    "fingerprint_kind",
    "source_address",
    "result",
)
KINDS = ("kubernetes", "node_session", "sudo", "heartbeat", "shipper_metric")
TIERS = ("routine", "deploy", "admin", "service", "unknown")


@dataclass(frozen=True)
class AccessRecordConfig:
    bucket_id: str
    bucket_name: str
    endpoint: str
    region: str
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    platform_namespace: str
    platform_workload: str
    expected_nodes: list[str]
    heartbeat_max_age_seconds: float
    buffer_warn_fraction: float
    principal_tiers: dict[str, str] = field(default_factory=dict)
    group_tiers: dict[str, str] = field(default_factory=dict)
    batch_size: int = 2
    max_object_bytes: int = 8 * 1024 * 1024
    max_records: int = 20000
    poll_seconds: float = 30
    placement_max_gap_seconds: float = 90

    def __post_init__(self):
        if not all(
            (
                self.bucket_id,
                self.bucket_name,
                self.endpoint,
                self.region,
                self.access_key_id,
                self.secret_access_key,
                self.platform_namespace,
                self.platform_workload,
            )
        ):
            raise ValueError("access record reader requires explicit storage configuration")
        if any(
            tier not in TIERS
            for tier in (*self.principal_tiers.values(), *self.group_tiers.values())
        ):
            raise ValueError("invalid access identity tier")
        if min(self.batch_size, self.max_object_bytes, self.max_records, self.poll_seconds) <= 0:
            raise ValueError("access record bounds must be positive")
        if self.placement_max_gap_seconds < self.poll_seconds:
            raise ValueError("placement observation window must cover the poll interval")
        if (not self.expected_nodes or any(not isinstance(node, str) or not node for node in self.expected_nodes)
                or not math.isfinite(self.heartbeat_max_age_seconds) or self.heartbeat_max_age_seconds <= 0
                or not 0 < self.buffer_warn_fraction <= 1):
            raise ValueError("access record alerts require expected nodes and explicit valid thresholds")


def parse_record(
    value: object, principal_tiers: dict[str, str], group_tiers: dict[str, str] | None = None
) -> dict:
    if (
        not isinstance(value, dict)
        or set(value) - {*FIELDS, "schema_version", "identity_groups"}
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or value.get("kind") not in KINDS
    ):
        raise ValueError("invalid access record schema")
    result = {key: value.get(key) for key in FIELDS}
    for key, item in result.items():
        if item is not None:
            if not isinstance(item, str) or len(item) > 2048 or "\x00" in item:
                raise ValueError("invalid access metadata field")
            # PostgreSQL text requires UTF-8 without NUL; JSON can carry lone surrogates.
            item.encode("utf-8")
        if key in ("kind", "source_at", "node", "boot_id", "event_id", "action") and not item:
            raise ValueError("missing access metadata field")
    timestamp = datetime.fromisoformat(result["source_at"].replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("access timestamp requires timezone")
    result["source_at"] = timestamp.astimezone(UTC)
    groups = value.get("identity_groups") or []
    if (
        not isinstance(groups, list)
        or len(groups) > 64
        or any(not isinstance(group, str) or len(group) > 256 for group in groups)
    ):
        raise ValueError("invalid access identity groups")
    tiers = {(group_tiers or {}).get(group) for group in groups} - {None}
    result["identity_tier"] = principal_tiers.get(
        result["identity"], next(iter(tiers)) if len(tiers) == 1 else "unknown"
    )
    return result


class AccessVersionReader:
    def __init__(self, config: AccessRecordConfig):
        self.config = config
        self.s3 = boto3.client(
            "s3",
            endpoint_url=config.endpoint,
            region_name=config.region,
            aws_access_key_id=config.access_key_id,
            aws_secret_access_key=config.secret_access_key,
            config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1}),
        )

    def records(self, key: str, version_id: str) -> list[dict]:
        response = self.s3.get_object(Bucket=self.config.bucket_name, Key=key, VersionId=version_id)
        body = response["Body"]
        try:
            compressed = body.read(self.config.max_object_bytes + 1)
        finally:
            body.close()
        if len(compressed) > self.config.max_object_bytes:
            raise ValueError("access object exceeds byte bound")
        source = (
            gzip.GzipFile(fileobj=io.BytesIO(compressed))
            if compressed[:2] == b"\x1f\x8b"
            else io.BytesIO(compressed)
        )
        records = []
        total = 0
        with source:
            while line := source.readline(32 * 1024 + 1):
                total += len(line)
                if (
                    len(line) > 32 * 1024
                    or total > self.config.max_object_bytes
                    or len(records) >= self.config.max_records
                ):
                    raise ValueError("access object exceeds decoded bound")
                records.append(
                    parse_record(
                        json.loads(line), self.config.principal_tiers, self.config.group_tiers
                    )
                )
        return records


async def record_placements(
    connection, observations: dict[str, tuple[PodPlacement, ...]], now: datetime, *, max_gap_seconds: float
) -> None:
    for cell_id, pods in observations.items():
        for pod in pods:
            await connection.execute(
                "WITH latest AS (SELECT first_observed_at FROM exomem_cloud_placements "
                " WHERE cell_id=$1 AND pod_uid=$2 ORDER BY last_observed_at DESC LIMIT 1 FOR UPDATE), "
                "extended AS (UPDATE exomem_cloud_placements SET last_observed_at=$6, "
                " source_deletion_requested_at=COALESCE($5,source_deletion_requested_at),phase=$9 "
                " WHERE cell_id=$1 AND pod_uid=$2 AND node=$3 AND workload_kind=$7 AND workload_uid=$8 "
                " AND first_observed_at=(SELECT first_observed_at FROM latest) "
                " AND last_observed_at BETWEEN $6::timestamptz-$10*interval '1 second' AND $6 "
                " RETURNING cell_id) "
                "INSERT INTO exomem_cloud_placements "
                "(cell_id,pod_uid,node,source_created_at,source_deletion_requested_at,first_observed_at,last_observed_at,"
                "workload_kind,workload_uid,phase) SELECT $1,$2,$3,$4,$5,$6,$6,$7,$8,$9 "
                "WHERE NOT EXISTS(SELECT 1 FROM extended) ON CONFLICT DO NOTHING",
                cell_id,
                pod.uid,
                pod.node,
                pod.created_at,
                pod.deletion_requested_at,
                now,
                pod.workload_kind,
                pod.workload_uid,
                pod.phase,
                max_gap_seconds,
            )


async def derive_receipts(connection, reader: AccessVersionReader) -> int:
    processed = 0
    for _ in range(reader.config.batch_size):
        async with connection.transaction():
            # Due retries must not overtake healthy work that has already waited longer.
            receipt = await connection.fetchrow(
                "SELECT * FROM exomem_cloud_access_receipts WHERE processed_at IS NULL "
                "AND next_attempt_at<=now() AND bucket_id=$1 "
                "ORDER BY next_attempt_at,received_at,id FOR UPDATE SKIP LOCKED LIMIT 1",
                reader.config.bucket_id,
            )
            if receipt is None:
                break
            try:
                records = await asyncio.to_thread(
                    reader.records, receipt["object_key"], receipt["version_id"]
                )
            except Exception as error:  # noqa: BLE001 - preserve failed work without raw diagnostics
                code = (
                    "ACCESS_RECORD_INVALID"
                    if isinstance(error, (ValueError, UnicodeError, EOFError, gzip.BadGzipFile))
                    else "ACCESS_VERSION_UNAVAILABLE"
                )
                await connection.execute(
                    "UPDATE exomem_cloud_access_receipts SET attempts=attempts+1, "
                    "next_attempt_at=now()+interval '1 minute',error_code=$2 WHERE id=$1",
                    receipt["id"],
                    code,
                )
                continue
            for line_number, record in enumerate(records):
                columns = (*FIELDS, "identity_tier")
                values = [record[key] for key in columns]
                await connection.execute(
                    "INSERT INTO exomem_cloud_access_records (receipt_id,line_number,provider_at,"
                    + ",".join(columns)
                    + ") VALUES ($1,$2,$3,"
                    + ",".join(f"${index}" for index in range(4, 4 + len(columns)))
                    + ") ON CONFLICT (receipt_id,line_number) DO NOTHING",
                    receipt["id"],
                    line_number,
                    receipt["provider_at"],
                    *values,
                )
            await connection.execute(
                "UPDATE exomem_cloud_access_receipts SET processed_at=now(),error_code=NULL,attempts=attempts+1 WHERE id=$1",
                receipt["id"],
            )
            processed += 1
    # Re-derive bounded received records against newly observed placement too.
    # The exact namespace binds API access. Node evidence uses observed Pod bounds.
    await connection.execute(
        "INSERT INTO exomem_cloud_access_entries (cell_id,record_id) "
        "SELECT cell.cell_id,record.id FROM exomem_cloud_access_records record "
        "JOIN exomem_cloud_cells cell ON "
        "(record.namespace='exo-cell-'||cell.cell_id AND record.kind='kubernetes' "
        " AND (record.resource='secrets' OR record.subresource IN ('exec','attach','portforward','proxy','ephemeralcontainers'))) "
        "OR (record.kind IN ('node_session','sudo') AND EXISTS (SELECT 1 FROM exomem_cloud_placements placement "
        " WHERE placement.cell_id=cell.cell_id AND placement.node=record.node "
        " AND (record.source_at BETWEEN placement.first_observed_at AND placement.last_observed_at "
        " OR (record.kind='node_session' AND record.action='open_session' AND record.session_id IS NOT NULL "
        " AND placement.first_observed_at BETWEEN record.source_at AND (SELECT min(ending.source_at) "
        " FROM exomem_cloud_access_records ending WHERE ending.kind='node_session' AND ending.action='close_session' "
        " AND ending.node=record.node AND ending.boot_id=record.boot_id AND ending.session_id=record.session_id "
        " AND ending.source_at>=record.source_at))))) "
        "OR (record.kind='kubernetes' AND record.resource='certificatesigningrequests' AND record.action='create' "
        " AND EXISTS (SELECT 1 FROM exomem_cloud_placements placement WHERE placement.cell_id=cell.cell_id "
        " AND record.source_at BETWEEN placement.first_observed_at AND placement.last_observed_at)) "
        "OR (record.kind='kubernetes' AND record.namespace=$1 AND record.resource='deployments' "
        " AND record.target=$2 AND record.action IN ('patch','update','create') "
        " AND cell.created_at<=record.source_at) "
        # These schema-defined kinds can become access entries; diagnostic series cannot.
        "WHERE record.kind IN ('kubernetes','node_session','sudo') "
        "AND NOT EXISTS (SELECT 1 FROM exomem_cloud_access_entries entry "
        " WHERE entry.cell_id=cell.cell_id AND entry.record_id=record.id) "
        "ORDER BY record.provider_at,record.id LIMIT $3 "
        "ON CONFLICT (cell_id,record_id) DO NOTHING",
        reader.config.platform_namespace,
        f"/apis/apps/v1/namespaces/{reader.config.platform_namespace}/deployments/{reader.config.platform_workload}",
        reader.config.max_records,
    )
    return processed


async def access_alert_states(connection, config: AccessRecordConfig, now: datetime) -> dict[str, bool | None]:
    """Judge received source times and Vector's fixed buffer metrics; missing metrics stay unknown."""
    beats = {row["node"]: row["source_at"] for row in await connection.fetch(
        "SELECT DISTINCT ON(node) node,source_at FROM exomem_cloud_access_records "
        "WHERE kind='heartbeat' AND node=ANY($1::text[]) ORDER BY node,source_at DESC", config.expected_nodes)}
    metrics = {}
    for row in await connection.fetch(
        "SELECT DISTINCT ON(node,target) node,target,result FROM exomem_cloud_access_records "
        "WHERE kind='shipper_metric' AND node=ANY($1::text[]) ORDER BY node,target,source_at DESC", config.expected_nodes):
        try:
            value = float(row["result"])
            if math.isfinite(value) and value >= 0:
                metrics[row["node"], row["target"]] = value
        except (TypeError, ValueError):
            pass
    pressure = []
    overflow = []
    for node in config.expected_nodes:
        # Vector 0.60 fixes these metric names and their gauge/counter meanings.
        size = metrics.get((node, "buffer_byte_size"))
        maximum = metrics.get((node, "buffer_max_byte_size"))
        discarded = metrics.get((node, "component_discarded_events_total"))
        pressure.append(size / maximum >= config.buffer_warn_fraction if size is not None and maximum else None)
        overflow.append(discarded > 0 if discarded is not None else None)
    # These names are content-free alert receiver protocol labels, not a tenant vocabulary.
    return {
        "access-record-heartbeat-gap": any(node not in beats or (now-beats[node]).total_seconds() > config.heartbeat_max_age_seconds
                                          for node in config.expected_nodes),
        "access-record-buffer-pressure": True if True in pressure else None if None in pressure else False,
        "access-record-overflow": True if True in overflow else None if None in overflow else False,
    }


async def run_access_record_loop(dsn: str, config: AccessRecordConfig, stop: asyncio.Event, cluster, cluster_config) -> None:
    from .reconcile import LoopMemory, _deliver_alert

    reader = None
    memory = LoopMemory()
    while not stop.is_set():
        connection = None
        try:
            if reader is None:
                reader = AccessVersionReader(config)
            connection = await db.connect(dsn)
            try:
                # Retired rows still own retained logs. Reconcile's admission and lifecycle gates do not apply.
                cells = {row["cell_id"] for row in await connection.fetch("SELECT cell_id FROM exomem_cloud_cells")}
                observations = await asyncio.to_thread(cluster.observe_access_placements, cells)
                await record_placements(connection, observations, datetime.now(UTC),
                                        max_gap_seconds=config.placement_max_gap_seconds)
            except Exception:  # noqa: BLE001 - unread placement stays a gap; receipt processing continues
                logger.warning("cellctl: placement recording unavailable")
            await derive_receipts(connection, reader)
            now = datetime.now(UTC)
            for alert, firing in (await access_alert_states(connection, config, now)).items():
                if firing is not None:
                    await _deliver_alert(cluster, cluster_config, memory, alert, firing, now)
        except Exception:  # noqa: BLE001 - record availability never gates cell reconciliation
            logger.warning("cellctl: access record derivation unavailable")
        finally:
            if connection is not None:
                await connection.close()
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.poll_seconds)
        except TimeoutError:
            pass
