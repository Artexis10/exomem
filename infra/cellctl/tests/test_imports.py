"""C5 holds survive restart and cancellation wins before volume cleanup."""
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from cellctl.imports import ImportJob, ImportRow, decide_import
from cellctl.state import CellRow, ClusterObservation

NOW = datetime(2026, 1, 1, tzinfo=UTC)
CELL = "aaaaaaaaaaaaaaaa"


def test_prepare_waits_for_runtime_and_releases_hold_with_recipient():
    row = CellRow(CELL, uuid.uuid4(), 10, 0, "running", None, 1)
    item = ImportRow(str(uuid.uuid4()), CELL, "adopt", 4, 1, "requested", NOW)
    observed = ClusterObservation(statefulset_exists=True, statefulset_image="example:cell", pod_uses_volume=True)
    waiting = decide_import(row, item, observed, {}, now=NOW)
    assert waiting.replicas == 0 and waiting.hold_kind == "import" and waiting.import_action is None
    held = replace(row, hold_kind="import", hold_started_at=NOW)
    stopped = replace(observed, statefulset_hold_kind="import", pod_uses_volume=False)
    assert decide_import(held, item, stopped, {}, now=NOW).import_action == "prepare"
    ready = decide_import(held, item, stopped, {"prepare": ImportJob(succeeded=True, report={"recipient": "age1" + "a" * 58})}, now=NOW)
    assert ready.replicas == 1 and ready.hold_kind is None
    assert ready.import_updates["observed_state"] == "key_ready"


def test_cancel_and_expiry_stop_active_import_before_cleanup():
    row = CellRow(CELL, uuid.uuid4(), 10, 0, "running", None, 1, hold_kind="import", hold_started_at=NOW)
    item = ImportRow(str(uuid.uuid4()), CELL, "adopt", 4, 1, "cancelled", NOW, recipient="age1" + "a" * 58)
    observed = ClusterObservation(statefulset_exists=True, statefulset_image="example:cell", statefulset_hold_kind="import")
    stopping = decide_import(row, item, observed, {"import": ImportJob(exists=True, active=True)}, now=NOW)
    assert stopping.stop_import_jobs == ["import"] and stopping.import_action is None
    after_release = decide_import(row, item, observed, {}, now=NOW)
    assert after_release.import_action == "discard" and after_release.replicas == 0
    completed = decide_import(row, item, observed, {"discard": ImportJob(exists=True, succeeded=True)}, now=NOW)
    assert completed.cleanup_import_transport and completed.import_updates["observed_state"] == "discarded"
    assert completed.replicas == 0 and completed.stop_import_jobs == ["discard"]
    finished = replace(item, observed_state="discarded")
    assert decide_import(row, finished, observed, {}, now=NOW).replicas == 1
    expired = decide_import(row, replace(item, requested_state="uploaded"), observed,
                            {"import": ImportJob(active=True)}, now=NOW + timedelta(hours=3))
    assert expired.stop_import_jobs == ["import"]
    assert expired.import_updates["error_code"] == "IMPORT_HOLD_EXPIRED"


def test_completed_protected_import_needs_cleanup_before_done():
    row = CellRow(CELL, uuid.uuid4(), 10, 0, "running", None, 1, hold_kind="import", hold_started_at=NOW)
    item = ImportRow(str(uuid.uuid4()), CELL, "archive_members", 12, 2, "uploaded", NOW, recipient="age1" + "a" * 58)
    observed = ClusterObservation(statefulset_exists=True, statefulset_image="example:cell", statefulset_hold_kind="import")
    jobs = {"import": ImportJob(exists=True, succeeded=True)}
    pending = decide_import(row, item, observed, jobs, now=NOW)
    assert pending.import_action == "cleanup" and "observed_state" not in pending.import_updates
    jobs["cleanup"] = ImportJob(exists=True, succeeded=True)
    done = decide_import(row, item, observed, jobs, now=NOW)
    assert done.cleanup_import_transport and done.import_updates["observed_state"] == "done"
    assert done.import_updates["part_urls"] is None and done.import_updates["recipient"] is None


async def test_reconcile_prepare_survives_restart_with_real_c5(cell_db):
    import asyncpg

    from cellctl import db, reconcile
    from cellctl.storage.fake_b2 import FakeB2
    from cellctl.storage.fake_hetzner import FakeHetznerVolumeProvider
    from tests.test_reconcile import (
        FakeClusterGateway,
        _cluster_config,
        _observed_from_applied,
        _secrets_config,
        _seed_cell,
    )

    await _seed_cell(cell_db, CELL, "import-tenant")
    app = await asyncpg.connect(cell_db.dsn(role="substrate_app"))
    controller = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster = FakeClusterGateway()
    jobs = {}
    cluster.observe_import_jobs = lambda namespace, import_id: jobs
    b2 = FakeB2()
    volume = FakeHetznerVolumeProvider()
    try:
        await reconcile.reconcile_once(controller, cluster, b2, volume, _secrets_config(), _cluster_config(), now=NOW)
        cluster.observations[CELL] = _observed_from_applied(cluster, CELL)
        await reconcile.reconcile_once(controller, cluster, b2, volume, _secrets_config(), _cluster_config(), now=NOW)
        await app.execute("INSERT INTO exomem_cloud_imports(cell_id,kind,declared_bytes,declared_files) VALUES($1,'adopt',4,1)", CELL)
        cluster.observations[CELL] = replace(cluster.observations[CELL], pod_uses_volume=True)
        await reconcile.reconcile_once(controller, cluster, b2, volume, _secrets_config(), _cluster_config(), now=NOW)
        assert not cluster.jobs
        assert (await db.select_all_rows(controller))[0].hold_kind == "import"
        # Each call uses fresh LoopMemory, as after process restart.
        cluster.observations[CELL] = replace(_observed_from_applied(cluster, CELL), pod_uses_volume=False)
        await reconcile.reconcile_once(controller, cluster, b2, volume, _secrets_config(), _cluster_config(), now=NOW)
        assert cluster.jobs[-1]["spec"]["template"]["spec"]["containers"][0]["command"][2] == "prepare"
        jobs["prepare"] = ImportJob(succeeded=True, report={"recipient": "age1" + "a" * 58})
        await reconcile.reconcile_once(controller, cluster, b2, volume, _secrets_config(), _cluster_config(), now=NOW)
        assert (await db.read_import(controller, CELL)).observed_state == "key_ready"
        assert (await db.select_all_rows(controller))[0].hold_kind is None
    finally:
        await app.close()
        await controller.close()


def test_multipart_sdk_signs_exact_parts_completes_report_and_deletes_all_versions():
    from urllib.parse import parse_qs, urlsplit

    import boto3
    from botocore.stub import Stubber

    from cellctl.imports import PART_BYTES, ImportStorage

    client = boto3.client("s3", endpoint_url="https://storage.example", region_name="us-east-1",
                          aws_access_key_id="example-key", aws_secret_access_key="example-secret")
    item = ImportRow(str(uuid.uuid4()), CELL, "adopt", 4, 1, "requested", NOW,
                     ciphertext_sha256="a" * 64, ciphertext_bytes=PART_BYTES + 1)
    storage = ImportStorage(client, "example-bucket")
    key = {"Bucket": "example-bucket", "Key": item.key}
    with Stubber(client) as stub:
        stub.add_response("list_multipart_uploads", {"Uploads": []}, {"Bucket": "example-bucket", "Prefix": item.key})
        stub.add_response("create_multipart_upload", {"UploadId": "upload-one"}, key)
        upload_id, parts = storage.prepare(item)
        assert [p["part_size_bytes"] for p in parts] == [PART_BYTES, 1]
        assert [parse_qs(urlsplit(p["url"]).query)["partNumber"] for p in parts] == [["1"], ["2"]]
        accepted = replace(item, upload_id=upload_id, part_urls=parts,
                           part_etags=[{"part_number": 1, "etag": '"one"'}, {"part_number": 2, "etag": '"two"'}])
        stub.add_response("complete_multipart_upload", {}, {**key, "UploadId": upload_id,
            "MultipartUpload": {"Parts": [{"PartNumber": 1, "ETag": '"one"'}, {"PartNumber": 2, "ETag": '"two"'}]}})
        stub.add_response("head_object", {"ContentLength": PART_BYTES + 1, "VersionId": "immutable-version"}, key)
        assert parse_qs(urlsplit(storage.complete(accepted)).query)["versionId"] == ["immutable-version"]
        stub.add_response("list_multipart_uploads", {"Uploads": [{"Key": item.key, "UploadId": "orphan"}]}, {"Bucket": "example-bucket", "Prefix": item.key})
        stub.add_response("abort_multipart_upload", {}, {**key, "UploadId": "orphan"})
        stub.add_response("list_object_versions", {"Versions": [{"Key": item.key, "VersionId": "old"}, {"Key": item.key, "VersionId": "new"}], "DeleteMarkers": [{"Key": item.key, "VersionId": "hidden"}]}, {"Bucket": "example-bucket", "Prefix": item.key})
        for version in ("old", "new", "hidden"):
            stub.add_response("delete_object", {}, {**key, "VersionId": version})
        stub.add_response("list_multipart_uploads", {"Uploads": []}, {"Bucket": "example-bucket", "Prefix": item.key})
        stub.add_response("list_object_versions", {"Versions": [], "DeleteMarkers": []}, {"Bucket": "example-bucket", "Prefix": item.key})
        storage.cleanup(accepted)
        stub.assert_no_pending_responses()


def test_failed_cleanup_releases_only_after_controllers_and_terminating_pods_disappear():
    row = CellRow(CELL, uuid.uuid4(), 10, 0, "read_only", None, 1, hold_kind="import", hold_started_at=NOW)
    item = ImportRow(str(uuid.uuid4()), CELL, "archive_members", 12, 2, "uploaded", NOW,
                     observed_state="cleanup_pending", updated_at=NOW + timedelta(minutes=3))
    observed = ClusterObservation(statefulset_exists=True, statefulset_image="example:cell", statefulset_hold_kind="import")
    for job in (ImportJob(exists=True, failed=True), ImportJob(pods_present=True)):
        waiting = decide_import(row, item, observed, {"cleanup": job}, now=NOW + timedelta(minutes=4))
        assert waiting.stop_import_jobs == ["cleanup"] and waiting.replicas == 0
    released = decide_import(row, item, observed, {}, now=NOW + timedelta(minutes=4))
    assert released.replicas == 1 and released.read_only and released.hold_kind is None
    assert released.import_updates == {}  # Polling cannot extend the retry timestamp.
    converged = replace(row, hold_kind=None, hold_started_at=None)
    unheld = replace(observed, statefulset_hold_kind=None)
    assert decide_import(converged, item, unheld, {}, now=NOW + timedelta(minutes=20)) is None
    retry = decide_import(converged, item, unheld, {}, now=NOW + timedelta(hours=2))
    assert retry.cleanup_import_transport and retry.retry_import_cleanup and not retry.apply_manifests
    held = replace(row, hold_started_at=NOW + timedelta(hours=2))
    overdue = decide_import(held, item, observed, {"cleanup": ImportJob(exists=True, active=True)}, now=NOW + timedelta(hours=2, minutes=3))
    assert overdue.import_updates["observed_state"] == "cleanup_pending"
    assert "error_code" not in overdue.import_updates
    assert overdue.stop_import_jobs == ["cleanup"]


def test_cleanup_keeps_success_after_upload_expiry_and_honors_suspension_and_deletion():
    row = CellRow(CELL, uuid.uuid4(), 10, 0, "stopped", None, 1, hold_kind="import", hold_started_at=NOW)
    item = ImportRow(str(uuid.uuid4()), CELL, "archive_members", 12, 2, "uploaded", NOW,
                     observed_state="cleanup_pending", updated_at=NOW + timedelta(minutes=3))
    observed = ClusterObservation(statefulset_exists=True, statefulset_image="example:cell", statefulset_hold_kind="import")
    assert decide_import(row, item, observed, {}, now=NOW + timedelta(days=2)).replicas == 0
    retry = replace(row, hold_started_at=NOW + timedelta(days=2))
    completed = decide_import(retry, item, observed, {"cleanup": ImportJob(exists=True, succeeded=True)}, now=NOW + timedelta(days=2, seconds=30))
    assert completed.import_updates["observed_state"] == "done" and completed.replicas == 0
    unfinished = replace(item, observed_state="importing")
    interrupted = decide_import(row, unfinished, observed, {"import": ImportJob(exists=True, active=True)}, now=NOW)
    assert interrupted.import_updates["error_code"] == "IMPORT_INTERRUPTED"
    deleting = replace(row, desired_state="deleted")
    stopping = decide_import(deleting, item, observed, {"import": ImportJob(exists=True)}, now=NOW)
    assert stopping.stop_import_jobs == ["import"]
    assert decide_import(deleting, item, observed, {}, now=NOW) is None


async def test_cleanup_failure_restart_releases_runtime_then_retries_durably(cell_db, monkeypatch):
    import asyncpg

    from cellctl import db, reconcile
    from cellctl.imports import ImportStorage
    from cellctl.storage.fake_b2 import FakeB2
    from cellctl.storage.fake_hetzner import FakeHetznerVolumeProvider
    from tests.test_reconcile import (
        FakeClusterGateway,
        _cluster_config,
        _observed_from_applied,
        _secrets_config,
        _seed_cell,
    )

    await _seed_cell(cell_db, CELL, "cleanup-tenant")
    app = await asyncpg.connect(cell_db.dsn(role="substrate_app"))
    controller = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster, b2, volume = FakeClusterGateway(), FakeB2(), FakeHetznerVolumeProvider()
    jobs = {}
    cluster.observe_import_jobs = lambda namespace, import_id: jobs.copy()
    current = datetime.now(UTC)

    async def step():
        # Fresh loop memory deliberately discards every process-local retry hint.
        await reconcile.reconcile_once(controller, cluster, b2, volume, _secrets_config(), replace(_cluster_config(), object_storage_endpoint="https://storage.example"), now=current)
        cluster.observations[CELL] = _observed_from_applied(cluster, CELL)

    try:
        await step()
        await step()
        import_id = await app.fetchval("INSERT INTO exomem_cloud_imports(cell_id,kind,declared_bytes,declared_files) "
                                      "VALUES($1,'archive_members',12,2) RETURNING import_id", CELL)
        await controller.execute("UPDATE exomem_cloud_imports SET observed_state='importing' WHERE import_id=$1", import_id)
        await controller.execute("UPDATE exomem_cloud_cells SET hold_kind='import',hold_started_at=$2 WHERE cell_id=$1", CELL, current)
        cluster.observations[CELL] = replace(cluster.observations[CELL], statefulset_hold_kind="import",
                                            statefulset_hold_started_at=current, statefulset_replicas=0)
        jobs.update({"import": ImportJob(exists=True, succeeded=True), "cleanup": ImportJob(exists=True, failed=True)})
        await step()
        pending = await db.read_import(controller, CELL)
        assert pending.observed_state == "cleanup_pending" and pending.error_code is None
        assert (await db.select_all_rows(controller))[0].hold_kind == "import"
        jobs.clear()  # Kubernetes confirms both Job controllers and all pods are gone.
        current = pending.updated_at + timedelta(seconds=1)
        await step()
        assert (await db.select_all_rows(controller))[0].hold_kind is None
        assert cluster.observations[CELL].statefulset_replicas == 1
        stamp = (await db.read_import(controller, CELL)).updated_at
        current += timedelta(minutes=20)
        await step()
        assert (await db.read_import(controller, CELL)).updated_at == stamp

        def storage_unavailable(self, item):
            raise OSError("disposable object service unavailable")

        monkeypatch.setattr(ImportStorage, "cleanup", storage_unavailable)
        current = stamp + timedelta(hours=2)
        await step()
        assert (await db.select_all_rows(controller))[0].hold_kind is None
        failed_retry = await db.read_import(controller, CELL)
        assert failed_retry.observed_state == "cleanup_pending" and failed_retry.error_code is None
        assert failed_retry.updated_at > stamp
        monkeypatch.setattr(ImportStorage, "cleanup", lambda self, item: None)
        current = failed_retry.updated_at + timedelta(hours=2)
        await step()
        assert (await db.select_all_rows(controller))[0].hold_kind == "import"
        cluster.observations[CELL] = replace(cluster.observations[CELL], pod_uses_volume=False)
        await step()
        assert cluster.jobs[-1]["spec"]["template"]["spec"]["containers"][0]["command"][2] == "discard"
        jobs["cleanup"] = ImportJob(exists=True, succeeded=True, pods_present=True)
        current += timedelta(seconds=30)
        await step()
        assert (await db.read_import(controller, CELL)).observed_state == "done"
        assert cluster.observations[CELL].statefulset_replicas == 0
        jobs.clear()
        await step()
        assert (await db.select_all_rows(controller))[0].hold_kind is None
        assert cluster.observations[CELL].statefulset_replicas == 1
    finally:
        await app.close()
        await controller.close()
