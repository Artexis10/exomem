"""Received-version derivation rejects content and preserves incomplete evidence."""

from datetime import UTC, datetime

import pytest

from cellctl.access_record import parse_record


def test_metadata_schema_rejects_content_even_when_the_source_is_compromised():
    event = dict(
        schema_version=1,
        kind="kubernetes",
        source_at="2026-10-26T12:00:00Z",
        node="node-a",
        boot_id="boot-a",
        event_id="audit-a",
        identity="operator",
        action="get",
        target="/api/v1/namespaces/cell/secrets/key",
        namespace="cell",
        resource="secrets",
        subresource=None,
    )
    parsed = parse_record(event, {"operator": "routine"})
    assert parsed["identity_tier"] == "routine"
    assert parsed["source_at"] == datetime(2026, 10, 26, 12, tzinfo=UTC)
    with pytest.raises(ValueError):
        parse_record({**event, "requestObject": {"canary": "content"}}, {})
    with pytest.raises(ValueError):
        parse_record({**event, "source_at": "2026-10-26T12:00:00"}, {})


def test_unknown_identity_and_missing_session_end_are_not_invented():
    event = dict(
        schema_version=1,
        kind="node_session",
        source_at="2026-10-26T12:00:00Z",
        node="node-a",
        boot_id="boot-a",
        event_id="open-a",
        identity="root",
        action="open_session",
        session_id="monitor-a",
        authentication_method=None,
        fingerprint=None,
        fingerprint_kind=None,
        source_address="192.0.2.1",
    )
    parsed = parse_record(event, {})
    assert parsed["identity_tier"] == "unknown"
    assert parsed["fingerprint"] is None
    assert "end_at" not in parsed


async def test_exact_version_retries_and_observed_placement_keep_tenants_separate(
    cell_db, tmp_path
):
    """Provider retries cannot duplicate rows or attribute the current node to past access."""
    from types import SimpleNamespace

    import asyncpg

    from cellctl.access_record import derive_receipts, record_placements
    from cellctl.state import PodPlacement

    from .conftest import insert_tenant

    connection = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    try:
        for cell in ("aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"):
            tenant = await insert_tenant(connection, cell)
            await connection.execute(
                "INSERT INTO exomem_cloud_cells(cell_id,tenant_id,desired_state) VALUES($1,$2,'stopped')",
                cell,
                tenant,
            )
        now = datetime(2026, 10, 26, 12, tzinfo=UTC)
        await record_placements(
            connection,
            {
                "aaaaaaaaaaaaaaaa": (PodPlacement("pod-a", "node-a", now, workload_uid="sts-a"),),
                "bbbbbbbbbbbbbbbb": (PodPlacement("pod-b", "node-b", now, workload_uid="sts-b"),),
            },
            now,
            max_gap_seconds=90,
        )
        receipt = await connection.fetchval(
            "INSERT INTO exomem_cloud_access_receipts(bucket_id,object_key,version_id,provider_at) VALUES('bucket','access/node-a/events','version-old',$1) RETURNING id",
            now,
        )
        seen = []

        class Reader:
            config = SimpleNamespace(
                bucket_id="bucket",
                batch_size=2,
                max_records=100,
                platform_namespace="platform",
                platform_workload="cellctl",
            )

            def records(self, key, version):
                seen.append((key, version))
                return [
                    parse_record(
                        dict(
                            schema_version=1,
                            kind="node_session",
                            source_at=now.isoformat(),
                            node="node-a",
                            boot_id="boot-a",
                            event_id="open",
                            action="open_session",
                            session_id="monitor-a",
                        ),
                        {},
                    ),
                    parse_record(dict(
                        schema_version=1, kind="kubernetes", source_at=now.isoformat(),
                        node="node-a", boot_id="boot-a", event_id="proxy", action="get",
                        namespace="exo-cell-aaaaaaaaaaaaaaaa", resource="pods", subresource="proxy",
                    ), {}),
                ]

        assert await derive_receipts(connection, Reader()) == 1
        assert await derive_receipts(connection, Reader()) == 0
        assert seen == [("access/node-a/events", "version-old")]
        assert {row["cell_id"] for row in await connection.fetch(
            "SELECT cell_id FROM exomem_cloud_access_entries")} == {"aaaaaaaaaaaaaaaa"}
        assert await connection.fetchval("SELECT count(*) FROM exomem_cloud_access_entries") == 2
        assert await connection.fetchval(
            "SELECT processed_at IS NOT NULL FROM exomem_cloud_access_receipts WHERE id=$1", receipt
        )
        # A provider outage keeps durable work pending, with no fabricated clean record.
        await connection.execute(
            "INSERT INTO exomem_cloud_access_receipts(bucket_id,object_key,version_id,provider_at) VALUES('bucket','access/node-a/new','unavailable',$1)",
            now,
        )

        class Offline(Reader):
            def records(self, key, version):
                raise OSError("provider unavailable")

        assert await derive_receipts(connection, Offline()) == 0
        assert (
            await connection.fetchval(
                "SELECT error_code FROM exomem_cloud_access_receipts WHERE version_id='unavailable'"
            )
            == "ACCESS_VERSION_UNAVAILABLE"
        )
        assert await connection.fetchval("SELECT count(*) FROM exomem_cloud_access_entries") == 2
        # A malformed retained gzip version is failed evidence, not a provider outage.
        import gzip

        await connection.execute(
            "INSERT INTO exomem_cloud_access_receipts(bucket_id,object_key,version_id,provider_at) VALUES('bucket','access/node-a/invalid','invalid-gzip',$1)",
            now,
        )

        class InvalidCompressed(Reader):
            def records(self, key, version):
                raise gzip.BadGzipFile("invalid compressed metadata")

        assert await derive_receipts(connection, InvalidCompressed()) == 0
        assert await connection.fetchval(
            "SELECT error_code FROM exomem_cloud_access_receipts WHERE version_id='invalid-gzip'"
        ) == "ACCESS_RECORD_INVALID"
    finally:
        await connection.close()


async def test_placement_outage_and_same_node_replacement_preserve_unknown_intervals(cell_db):
    """A collector outage must not fill an unobserved interval or merge different Pod identities."""
    from datetime import timedelta

    import asyncpg

    from cellctl.access_record import record_placements
    from cellctl.state import PodPlacement

    from .conftest import insert_tenant

    connection = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    try:
        tenant = await insert_tenant(owner, "retained-placement")
        cell = "cccccccccccccccc"
        await owner.execute(
            "INSERT INTO exomem_cloud_cells(cell_id,tenant_id,desired_state,observed_state) VALUES($1,$2,'deleted','deleted')",
            cell, tenant,
        )
        now = datetime(2026, 11, 4, 12, tzinfo=UTC)
        original = PodPlacement("original", "node-a", now, workload_uid="sts-a", phase="Running")
        job = PodPlacement("backup", "node-a", now, workload_kind="Job", workload_uid="job-a", phase="Running")
        for seconds in (0, 30, 300):
            await record_placements(connection, {cell: (original, job)}, now+timedelta(seconds=seconds),
                                    max_gap_seconds=90)
        replacement = PodPlacement("replacement", "node-a", now+timedelta(seconds=330),
                                   workload_uid="sts-a", phase="Running")
        await record_placements(connection, {cell: (replacement,)}, now+timedelta(seconds=330),
                                max_gap_seconds=90)
        rows = await owner.fetch("SELECT * FROM exomem_cloud_placements ORDER BY first_observed_at,pod_uid")
        assert len(rows) == 5
        assert {(r["pod_uid"], r["workload_kind"]) for r in rows} == {
            ("original", "StatefulSet"), ("replacement", "StatefulSet"), ("backup", "Job")}
        assert not any(r["first_observed_at"] < now+timedelta(seconds=150) < r["last_observed_at"] for r in rows)
    finally:
        await connection.close()
        await owner.close()


async def test_old_invalid_retries_do_not_starve_an_already_waiting_healthy_version(cell_db):
    """Four bad retained versions must not consume both slots forever after the writer is repaired."""
    from types import SimpleNamespace

    import asyncpg

    from cellctl.access_record import derive_receipts

    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    processor = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        for number in range(4):
            await owner.execute(
                "INSERT INTO exomem_cloud_access_receipts(bucket_id,object_key,version_id,provider_at,received_at,next_attempt_at,attempts,error_code) VALUES('bucket',$1,$1,now(),now()-interval '1 hour',now()-interval '1 second',1,'ACCESS_RECORD_INVALID')",
                f"access/bad-{number}",
            )
        healthy = await owner.fetchval(
            "INSERT INTO exomem_cloud_access_receipts(bucket_id,object_key,version_id,provider_at,next_attempt_at) VALUES('bucket','access/healthy','healthy',now(),now()-interval '10 minutes') RETURNING id"
        )

        class Reader:
            config = SimpleNamespace(bucket_id="bucket", batch_size=2, max_records=100,
                                     platform_namespace="cloud", platform_workload="cellctl")

            def records(self, key, version):
                if version != "healthy":
                    raise ValueError("invalid immutable metadata")
                return [parse_record(dict(schema_version=1, kind="heartbeat",
                    source_at=datetime.now(UTC).isoformat(), node="node-a", boot_id="boot-a",
                    event_id="after-repair", action="heartbeat"), {})]

        assert await derive_receipts(processor, Reader()) == 1
        assert await owner.fetchval(
            "SELECT processed_at IS NOT NULL FROM exomem_cloud_access_receipts WHERE id=$1", healthy
        )
        assert await owner.fetchval(
            "SELECT count(*) FROM exomem_cloud_access_receipts WHERE processed_at IS NULL AND error_code='ACCESS_RECORD_INVALID'"
        ) == 4
    finally:
        await processor.close()
        await owner.close()


async def test_missing_heartbeat_and_buffer_alerts_use_the_existing_receiver_despite_observation_failure(cell_db, monkeypatch):
    """Admission or observation failure must not suppress recording alerts or fabricate a clean heartbeat."""
    import asyncio
    from types import SimpleNamespace

    import asyncpg

    from cellctl import alerts
    from cellctl.access_record import (
        AccessRecordConfig,
        access_alert_states,
        run_access_record_loop,
    )

    connection = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    config = AccessRecordConfig(
        bucket_id="bucket", bucket_name="record", endpoint="http://127.0.0.1:9", region="test",
        access_key_id="test", secret_access_key="test", platform_namespace="cloud", platform_workload="cellctl",
        expected_nodes=["node-a"], heartbeat_max_age_seconds=180, buffer_warn_fraction=0.8,
    )
    try:
        now = datetime.now(UTC)
        assert await access_alert_states(connection, config, now) == {
            "access-record-heartbeat-gap": True,
            "access-record-buffer-pressure": None,
            "access-record-overflow": None,
        }
        receipt = await connection.fetchval(
            "INSERT INTO exomem_cloud_access_receipts(bucket_id,object_key,version_id,provider_at,processed_at) VALUES('bucket','access/events','version',now(),now()) RETURNING id"
        )
        for number, (kind, action, target, result) in enumerate([
            ("heartbeat", "heartbeat", None, None),
            ("shipper_metric", "metric", "buffer_byte_size", "95"),
            ("shipper_metric", "metric", "buffer_max_byte_size", "100"),
            ("shipper_metric", "metric", "component_discarded_events_total", "2"),
        ]):
            await connection.execute(
                "INSERT INTO exomem_cloud_access_records(receipt_id,line_number,kind,source_at,provider_at,node,boot_id,event_id,identity_tier,action,target,result) VALUES($1,$2,$3,now()-interval '1 hour',now(),'node-a','boot-a',$2::integer::text,'service',$4,$5,$6)",
                receipt, number, kind, action, target, result,
            )
        assert all(value is True for value in (await access_alert_states(connection, config, now)).values())
        stop = asyncio.Event()
        delivered = {}

        class Cluster:
            def observe_access_placements(self, cells):
                raise OSError("observation unavailable")

            def read_secret_value(self, *identity):
                assert identity == ("cloud", "alert-delivery", "url")
                return "https://alerts.example.test/test-only"

        def receive(url, *, alert, active, observed_at):
            assert url == "https://alerts.example.test/test-only"
            delivered[alert] = active
            if len(delivered) == 3:
                stop.set()

        monkeypatch.setattr(alerts, "deliver", receive)
        await asyncio.wait_for(run_access_record_loop(
            cell_db.dsn(role="exomem_cellctl"), config, stop, Cluster(),
            SimpleNamespace(alert_delivery_secret=("cloud", "alert-delivery", "url")),
        ), timeout=3)
        assert len(delivered) == 3 and all(delivered.values())
        await connection.execute("UPDATE exomem_cloud_access_records SET source_at=now(),result=CASE target WHEN 'buffer_byte_size' THEN '25' WHEN 'component_discarded_events_total' THEN '0' ELSE result END")
        assert all(value is False for value in (await access_alert_states(connection, config, datetime.now(UTC))).values())
    finally:
        await connection.close()


def test_invalid_recording_configuration_does_not_prevent_cell_reconciliation(monkeypatch, caplog):
    """A malformed optional reader Secret must not turn the access record into a cell availability gate."""
    from types import SimpleNamespace

    from cellctl import main

    for key in ("CELLCTL_DATABASE_DSN", "CELLCTL_CELL_DATA_BACKUP_LIST_KEY_ID",
                "CELLCTL_CELL_DATA_BACKUP_LIST_APPLICATION_KEY", "CELLCTL_CELL_DATA_BACKUP_BUCKET_ID",
                "CELLCTL_HETZNER_READ_TOKEN"):
        monkeypatch.setenv(key, "synthetic")
    monkeypatch.setenv("CELLCTL_ACCESS_RECORD_READER", "{")
    monkeypatch.setattr(main.k8s_config, "load_kube_config", lambda: None)
    monkeypatch.setattr(main.k8s_config, "load_incluster_config", lambda: None)
    monkeypatch.setattr(main.k8s, "ApiClient", lambda: None)
    monkeypatch.setattr(main, "ClusterClient", lambda *args, **kwargs: object())
    monkeypatch.setattr(main, "B2ObjectStorage", lambda config: object())
    monkeypatch.setattr(main, "HetznerVolumeProvider", lambda **kwargs: object())
    monkeypatch.setattr(main, "build_cluster_config", lambda: SimpleNamespace(access_record_enabled=True, storage=None))
    monkeypatch.setattr(main, "build_secrets_config", lambda: object())
    monkeypatch.setattr(main, "build_reconcile_config", lambda: object())
    called = []

    async def reconcile(*args, **kwargs):
        called.append(True)

    monkeypatch.setattr(main, "run_loop", reconcile)
    main.run()
    assert called == [True]
    assert "configure CELLCTL_ACCESS_RECORD_READER" in caplog.text
