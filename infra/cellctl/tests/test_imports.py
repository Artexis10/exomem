"""C5 holds survive restart and cancellation wins before volume cleanup."""
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

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
    assert completed.cleanup_import_transport and completed.import_updates["observed_state"] == "cleanup_discard_stopping"
    assert completed.replicas == 0 and completed.stop_import_jobs == ["discard"]
    finished = replace(item, observed_state="discarded")
    assert decide_import(row, finished, observed, {}, now=NOW).replicas == 1
    expired = decide_import(row, replace(item, requested_state="uploaded"), observed,
                            {"import": ImportJob(active=True)}, now=NOW + timedelta(hours=3))
    assert expired.stop_import_jobs == ["import"]
    assert expired.import_updates["error_code"] == "IMPORT_HOLD_EXPIRED"


def test_prepare_rejects_an_empty_termination_report_without_publishing_a_key():
    """A successful Pod without a recipient must not admit an unusable encrypted upload."""
    row = CellRow(CELL, uuid.uuid4(), 10, 0, "running", None, 1, hold_kind="import", hold_started_at=NOW)
    item = ImportRow(str(uuid.uuid4()), CELL, "adopt", 4, 1, "requested", NOW)
    observed = ClusterObservation(statefulset_exists=True, statefulset_image="example:cell", statefulset_hold_kind="import")
    result = decide_import(row, item, observed, {"prepare": ImportJob(exists=True, succeeded=True)}, now=NOW)
    assert result.import_updates == {"error_code": "IMPORT_KEY_INVALID"}
    assert result.hold_kind == "import" and result.replicas == 0


def test_completed_protected_import_needs_cleanup_before_done():
    row = CellRow(CELL, uuid.uuid4(), 10, 0, "running", None, 1, hold_kind="import", hold_started_at=NOW)
    item = ImportRow(str(uuid.uuid4()), CELL, "archive_members", 12, 2, "uploaded", NOW, recipient="age1" + "a" * 58)
    observed = ClusterObservation(statefulset_exists=True, statefulset_image="example:cell", statefulset_hold_kind="import")
    jobs = {"import": ImportJob(exists=True, succeeded=True)}
    pending = decide_import(row, item, observed, jobs, now=NOW)
    assert pending.import_action == "cleanup" and pending.import_updates["observed_state"] == "cleanup_running"
    jobs["cleanup"] = ImportJob(exists=True, succeeded=True)
    done = decide_import(row, item, observed, jobs, now=NOW)
    assert done.cleanup_import_transport and done.import_updates["observed_state"] == "cleanup_stopping"
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
        assert cluster.jobs[-1]["spec"]["template"]["spec"]["containers"][0]["command"][6] == "prepare"
        jobs["prepare"] = ImportJob(succeeded=True, report={"recipient": "age1" + "a" * 58})
        await reconcile.reconcile_once(controller, cluster, b2, volume, _secrets_config(), _cluster_config(), now=NOW)
        assert (await db.read_import(controller, CELL)).observed_state == "key_ready"
        assert (await db.select_all_rows(controller))[0].hold_kind is None
        jobs.clear()
        cluster.observations[CELL] = _observed_from_applied(cluster, CELL)
        await reconcile.reconcile_once(controller, cluster, b2, volume, _secrets_config(), _cluster_config(), now=NOW)
        current = (await db.select_all_rows(controller))[0]
        assert current.observed_state == "running" and current.ready
        assert (await db.read_import(controller, CELL)).observed_state == "key_ready"
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
        # Restart after deletion must also prove that no upload or version remains.
        stub.add_response("list_multipart_uploads", {"Uploads": []}, {"Bucket": "example-bucket", "Prefix": item.key})
        stub.add_response("list_object_versions", {"Versions": [], "DeleteMarkers": []}, {"Bucket": "example-bucket", "Prefix": item.key})
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
    assert retry.import_updates["observed_state"] == "cleanup_running" and retry.hold_started_at == NOW + timedelta(hours=2)
    held = replace(row, hold_started_at=NOW + timedelta(hours=2))
    overdue = decide_import(held, replace(item, observed_state="cleanup_running"), observed, {"cleanup": ImportJob(exists=True, active=True)}, now=NOW + timedelta(hours=2, minutes=3))
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
    completed = decide_import(retry, replace(item, observed_state="cleanup_running"), observed, {"cleanup": ImportJob(exists=True, succeeded=True)}, now=NOW + timedelta(days=2, seconds=30))
    assert completed.import_updates["observed_state"] == "cleanup_stopping" and completed.replicas == 0
    unfinished = replace(item, observed_state="importing")
    interrupted = decide_import(row, unfinished, observed, {"import": ImportJob(exists=True, active=True)}, now=NOW)
    assert interrupted.import_updates["error_code"] == "IMPORT_INTERRUPTED"
    deleting = replace(row, desired_state="deleted")
    stopping = decide_import(deleting, item, observed, {"import": ImportJob(exists=True)}, now=NOW)
    assert stopping.stop_import_jobs == ["import"]
    assert decide_import(deleting, item, observed, {}, now=NOW) is None


@pytest.mark.parametrize("report,expected", [
    ({"ok": True, "files": 2, "bytes": 12}, (2, 12)),
    ({"ok": True, "files": True, "bytes": 12}, (None, None)),
    ({"ok": True, "files": 2, "bytes": -1}, (None, None)),
    ({"ok": True, "files": 2**63, "bytes": 12}, (None, None)),
    ({"ok": True, "files": 2, "bytes": 2**63}, (None, None)),
    ({}, (None, None)),
])
async def test_cleanup_failure_restart_releases_runtime_then_retries_durably(cell_db, monkeypatch, report, expected, caplog):
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
        jobs.update({"import": ImportJob(exists=True, succeeded=True, report=report),
                     "cleanup": ImportJob(exists=True, failed=True)})
        if expected[0] is not None:
            write_import = db.write_import
            async def report_write_failed(connection, selected_id, updates):
                if "staged_files" in updates:
                    raise RuntimeError("staging report write unavailable")
                await write_import(connection, selected_id, updates)
            with monkeypatch.context() as patch:
                patch.setattr(db, "write_import", report_write_failed)
                patch.setattr(cluster, "delete_job", lambda *args: pytest.fail("removed report before persistence"))
                await step()
            unrecorded = await db.read_import(controller, CELL)
            assert unrecorded.observed_state == "importing" and unrecorded.staged_files is None
            assert "RuntimeError" in caplog.text
        await step()
        pending = await db.read_import(controller, CELL)
        assert pending.observed_state == "cleanup_pending" and pending.error_code is None
        assert (pending.staged_files, pending.staged_bytes) == expected
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
        assert (await db.select_all_rows(controller))[0].hold_kind == "import"
        assert (await db.read_import(controller, CELL)).observed_state == "cleanup_running"
        jobs["cleanup"] = ImportJob(exists=True, succeeded=True)
        await step()
        jobs.clear()
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
        assert cluster.jobs[-1]["spec"]["template"]["spec"]["containers"][0]["command"][6] == "discard"
        jobs["cleanup"] = ImportJob(exists=True, succeeded=True, pods_present=True)
        current += timedelta(seconds=30)
        await step()
        assert (await db.read_import(controller, CELL)).observed_state == "cleanup_stopping"
        assert cluster.observations[CELL].statefulset_replicas == 0
        jobs.clear()
        await step()
        assert (await db.select_all_rows(controller))[0].hold_kind is None
        assert await db.read_import(controller, CELL) is None
        assert await app.fetchval("SELECT observed_state FROM exomem_cloud_imports WHERE import_id=$1", import_id) == "done"
        assert tuple(await app.fetchrow("SELECT staged_files,staged_bytes FROM exomem_cloud_imports WHERE import_id=$1", import_id)) == expected
        assert cluster.observations[CELL].statefulset_replicas == 1
    finally:
        await app.close()
        await controller.close()


@pytest.mark.parametrize("stopping", ["cleanup_stopping", "cleanup_discard_stopping"])
def test_stopping_drains_every_phase_without_replaying_cleanup_after_new_cancel(stopping):
    row = CellRow(CELL, uuid.uuid4(), 10, 0, "running", None, 1, hold_kind="import", hold_started_at=NOW)
    item = ImportRow(str(uuid.uuid4()), CELL, "adopt", 4, 1, "cancelled", NOW,
                     observed_state=stopping, error_code="IMPORT_FAILED")
    observed = ClusterObservation(statefulset_exists=True, statefulset_image="example:cell")
    for phase in ("prepare", "import", "cleanup", "discard"):
        for job in (ImportJob(exists=True, succeeded=True), ImportJob(pods_present=True)):
            waiting = decide_import(row, item, observed, {phase: job}, now=NOW + timedelta(days=2))
            assert waiting.stop_import_jobs == [phase] and waiting.replicas == 0
            assert not waiting.import_action and not waiting.cleanup_import_transport
            assert not waiting.finalize_import and not waiting.import_updates
    finished = decide_import(row, item, observed, {}, now=NOW + timedelta(days=2))
    assert finished.finalize_import and not finished.import_action


def test_running_retains_outcome_after_key_and_ciphertext_cleanup_jobs_disappear():
    row = CellRow(CELL, uuid.uuid4(), 10, 0, "running", None, 1, hold_kind="import", hold_started_at=NOW)
    item = ImportRow(str(uuid.uuid4()), CELL, "archive_members", 4, 1, "uploaded", NOW - timedelta(days=2),
                     observed_state="cleanup_running", error_code="IMPORT_FAILED")
    observed = ClusterObservation(statefulset_exists=True, statefulset_image="example:cell")
    replay = decide_import(row, item, observed, {}, now=NOW)
    assert replay.import_action == "cleanup" and "error_code" not in replay.import_updates
    assert replay.import_updates.get("observed_state") is None


def test_new_request_starts_fresh_deadline_despite_predecessor_statefulset_hold():
    row = CellRow(CELL, uuid.uuid4(), 10, 0, "running", None, 1)
    item = ImportRow(str(uuid.uuid4()), CELL, "adopt", 4, 1, "requested", NOW)
    observed = ClusterObservation(statefulset_exists=True, statefulset_image="example:cell",
                                  statefulset_hold_kind="import", statefulset_hold_started_at=NOW - timedelta(days=2))
    fresh = decide_import(row, item, observed, {}, now=NOW)
    assert fresh.hold_started_at == NOW and fresh.import_action == "prepare"
    assert "error_code" not in fresh.import_updates


async def test_cleanup_crash_journals_survive_removed_keys_jobs_and_late_cancel(cell_db, monkeypatch, caplog, tmp_path):
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

    await _seed_cell(cell_db, CELL, "crash-cleanup-tenant")
    app = await asyncpg.connect(cell_db.dsn(role="substrate_app"))
    controller = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster, b2, volume = FakeClusterGateway(), FakeB2(), FakeHetznerVolumeProvider()
    jobs = {}
    cluster.observe_import_jobs = lambda namespace, import_id: jobs.copy()
    current = datetime.now(UTC)
    config = replace(_cluster_config(), object_storage_endpoint="https://storage.example")
    key_file = tmp_path / "private-key"
    ciphertext = tmp_path / "ciphertext.age"
    key_file.write_bytes(b"disposable key")
    ciphertext.write_bytes(b"disposable ciphertext")

    async def step():
        await reconcile.reconcile_once(controller, cluster, b2, volume, _secrets_config(), config, now=current)
        cluster.observations[CELL] = _observed_from_applied(cluster, CELL)

    try:
        await step()
        await step()
        import_id = await app.fetchval("INSERT INTO exomem_cloud_imports(cell_id,kind,declared_bytes,declared_files) "
                                      "VALUES($1,'archive_members',12,2) RETURNING import_id", CELL)
        jobs["import"] = ImportJob(exists=True, failed=True, report={"error": "IMPORT_FAILED"})
        await step()
        assert (await db.read_import(controller, CELL)).error_code == "IMPORT_FAILED"
        jobs.clear()

        def removed_key_then_crash(manifest):
            assert manifest["spec"]["template"]["spec"]["containers"][0]["command"][6] == "discard"
            key_file.unlink(missing_ok=True)
            raise RuntimeError("cleanup process stopped after private key deletion")

        with monkeypatch.context() as patch:
            patch.setattr(cluster, "run_job", removed_key_then_crash)
            await step()
            assert "RuntimeError" in caplog.text
            caplog.clear()
        running = await db.read_import(controller, CELL)
        assert running.observed_state == "cleanup_running" and running.error_code == "IMPORT_FAILED"
        assert running.staged_files is None and running.staged_bytes is None
        assert not key_file.exists()
        await step()  # A missing completion Job replays only idempotent local cleanup.
        assert cluster.jobs[-1]["spec"]["template"]["spec"]["containers"][0]["command"][6] == "discard"
        jobs["cleanup"] = ImportJob(exists=True, succeeded=True, pods_present=True)
        cleanup_calls = []
        def delete_ciphertext(self, item):
            ciphertext.unlink(missing_ok=True)
            cleanup_calls.append(item.import_id)

        monkeypatch.setattr(ImportStorage, "cleanup", delete_ciphertext)
        write_import = db.write_import

        async def ciphertext_deleted_then_crash(connection, selected_id, updates):
            if updates.get("observed_state") == "cleanup_stopping":
                raise RuntimeError("ciphertext deleted before stopping journal")
            await write_import(connection, selected_id, updates)

        with monkeypatch.context() as patch:
            patch.setattr(db, "write_import", ciphertext_deleted_then_crash)
            await step()
            assert "RuntimeError" in caplog.text
            caplog.clear()
        assert (await db.read_import(controller, CELL)).observed_state == "cleanup_running"
        assert not ciphertext.exists()
        jobs.clear()  # The old completed Job also disappeared during the restart.
        await step()
        assert cluster.jobs[-1]["spec"]["template"]["spec"]["containers"][0]["command"][6] == "discard"
        jobs["cleanup"] = ImportJob(exists=True, succeeded=True)

        def stopping_journal_then_crash(namespace, name):
            raise RuntimeError("controller stopped after stopping journal")

        with monkeypatch.context() as patch:
            patch.setattr(cluster, "delete_job", stopping_journal_then_crash)
            await step()
            assert "RuntimeError" in caplog.text
            caplog.clear()
        assert (await db.read_import(controller, CELL)).observed_state == "cleanup_stopping"
        assert len(cleanup_calls) == 2
        for phase in ("prepare", "import", "cleanup", "discard"):
            jobs.clear()
            jobs[phase] = ImportJob(pods_present=True)
            await step()
            assert (await db.read_import(controller, CELL)).observed_state == "cleanup_stopping"
            with pytest.raises(asyncpg.UniqueViolationError):
                await app.execute("INSERT INTO exomem_cloud_imports(cell_id,kind,declared_bytes,declared_files) "
                                  "VALUES($1,'adopt',1,1)", CELL)
        jobs.clear()
        finalize = db.finalize_import

        async def cancel_after_snapshot(connection, cell_id, selected_id, **options):
            async with app.transaction():
                await app.fetchval("SELECT cell_id FROM exomem_cloud_cells WHERE cell_id=$1 FOR UPDATE", CELL)
                await app.execute("UPDATE exomem_cloud_imports SET requested_state='cancelled' WHERE import_id=$1", import_id)
                await app.execute("UPDATE exomem_cloud_cells SET desired_state='stopped' WHERE cell_id=$1", CELL)
            return await finalize(connection, cell_id, selected_id, **options)

        with monkeypatch.context() as patch:
            patch.setattr(db, "finalize_import", cancel_after_snapshot)
            await step()
        assert (await db.read_import(controller, CELL)).observed_state == "cleanup_running"
        assert (await db.select_all_rows(controller))[0].hold_kind == "import"
        await step()
        jobs["discard"] = ImportJob(exists=True, succeeded=True)
        await step()
        assert (await db.read_import(controller, CELL)).observed_state == "cleanup_discard_stopping"
        jobs.clear()
        await step()
        assert len(cleanup_calls) == 3
        outcome = await app.fetchrow("SELECT observed_state,error_code FROM exomem_cloud_imports WHERE import_id=$1", import_id)
        assert dict(outcome) == {"observed_state": "discarded", "error_code": "IMPORT_FAILED"}
        assert (await db.select_all_rows(controller))[0].hold_kind is None
        assert cluster.observations[CELL].statefulset_replicas == 0
    finally:
        await app.close()
        await controller.close()


@pytest.mark.parametrize(("error_code", "terminal"), [("IMPORT_FAILED", "failed"), ("IMPORT_HOLD_EXPIRED", "expired")])
async def test_terminal_publication_rolls_back_with_hold_release_failure(cell_db, monkeypatch, error_code, terminal):
    import asyncpg

    from cellctl import db
    from tests.test_reconcile import _seed_cell

    await _seed_cell(cell_db, CELL, "atomic-cleanup-tenant")
    app = await asyncpg.connect(cell_db.dsn(role="substrate_app"))
    controller = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        import_id = await app.fetchval("INSERT INTO exomem_cloud_imports(cell_id,kind,declared_bytes,declared_files) "
                                      "VALUES($1,'adopt',1,1) RETURNING import_id", CELL)
        await controller.execute("UPDATE exomem_cloud_imports SET observed_state='cleanup_stopping',error_code=$2 WHERE import_id=$1", import_id, error_code)
        await controller.execute("UPDATE exomem_cloud_cells SET hold_kind='import',hold_started_at=$2 WHERE cell_id=$1", CELL, datetime.now(UTC))

        async def stopped_before_release(*args, **kwargs):
            raise RuntimeError("controller stopped before C1 release")

        with monkeypatch.context() as patch:
            patch.setattr(db, "write_observed", stopped_before_release)
            with pytest.raises(RuntimeError, match="before C1 release"):
                await db.finalize_import(controller, CELL, str(import_id))
        assert (await db.read_import(controller, CELL)).observed_state == "cleanup_stopping"
        assert (await db.select_all_rows(controller))[0].hold_kind == "import"
        with pytest.raises(asyncpg.UniqueViolationError):
            await app.execute("INSERT INTO exomem_cloud_imports(cell_id,kind,declared_bytes,declared_files) VALUES($1,'adopt',1,1)", CELL)
        await db.finalize_import(controller, CELL, str(import_id))
        assert await db.read_import(controller, CELL) is None
        assert await app.fetchval("SELECT observed_state FROM exomem_cloud_imports WHERE import_id=$1", import_id) == terminal
        assert (await db.select_all_rows(controller))[0].hold_kind is None
        assert await app.fetchval("INSERT INTO exomem_cloud_imports(cell_id,kind,declared_bytes,declared_files) VALUES($1,'adopt',1,1) RETURNING import_id", CELL) != import_id
    finally:
        await app.close()
        await controller.close()


@pytest.mark.parametrize(("timing", "intent"), [("before_snapshot", "cancelled"), ("inside_finalizer", "discard_requested")])
async def test_late_intent_removes_adoption_before_discard_publication(cell_db, monkeypatch, tmp_path, timing, intent):
    import asyncio

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

    # This cross-package workflow runs with the runtime package installed beside cellctl.
    cloud_import_job = pytest.importorskip("exomem.cloud_import_job")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", CELL)
    await _seed_cell(cell_db, CELL, "late-adopt-discard")
    app = await asyncpg.connect(cell_db.dsn(role="substrate_app"))
    controller = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    cluster, b2, volume = FakeClusterGateway(), FakeB2(), FakeHetznerVolumeProvider()
    jobs = {}
    cluster.observe_import_jobs = lambda namespace, import_id: jobs.copy()
    current = datetime.now(UTC)
    config = replace(_cluster_config(), object_storage_endpoint="https://storage.example")
    vault = tmp_path / "vault"
    vault.mkdir()

    async def step():
        await reconcile.reconcile_once(controller, cluster, b2, volume, _secrets_config(), config, now=current)
        cluster.observations[CELL] = _observed_from_applied(cluster, CELL)

    try:
        await step()
        await step()
        import_id = await app.fetchval("INSERT INTO exomem_cloud_imports(cell_id,kind,declared_bytes,declared_files) "
                                      "VALUES($1,'adopt',12,1) RETURNING import_id", CELL)
        adopted = vault / "_Imports" / str(import_id)
        adopted.mkdir(parents=True)
        (adopted / "note.md").write_text("Imported note\n")
        identity = vault.parent / ".import-keys" / str(import_id)
        identity.mkdir(parents=True)
        (identity / "identity").write_text("disposable identity")
        cloud_import_job.discard(vault, str(import_id), keep_adoption=True)
        assert adopted.is_dir() and not identity.exists()
        await controller.execute("UPDATE exomem_cloud_imports SET observed_state='cleanup_stopping',error_code='IMPORT_FAILED' WHERE import_id=$1", import_id)
        old_hold = current - timedelta(minutes=5)
        await controller.execute("UPDATE exomem_cloud_cells SET hold_kind='import',hold_started_at=$2 WHERE cell_id=$1", CELL, old_hold)
        cluster.observations[CELL] = replace(cluster.observations[CELL], statefulset_replicas=0,
                                            statefulset_hold_kind="import", statefulset_hold_started_at=old_hold)
        jobs["cleanup"] = ImportJob(exists=True, succeeded=True)
        await step()  # The ordinary cleanup first stops runtime and drains its completion Job.
        jobs.clear()
        if timing == "before_snapshot":
            await app.execute("UPDATE exomem_cloud_imports SET requested_state=$2 WHERE import_id=$1", import_id, intent)
            await step()
        else:
            # Admission owns C1 first. The finalizer must wait, then read the newly committed intent.
            finalize = db.finalize_import
            async with app.transaction():
                await app.fetchval("SELECT cell_id FROM exomem_cloud_cells WHERE cell_id=$1 FOR UPDATE", CELL)
                selected = await db.read_import(controller, CELL)
                assert selected.observed_state == "cleanup_stopping"
                finalizing = asyncio.create_task(finalize(controller, CELL, str(import_id)))
                await app.execute("UPDATE exomem_cloud_imports SET requested_state=$2 WHERE import_id=$1", import_id, intent)
            assert await finalizing is None
        running = await db.read_import(controller, CELL)
        assert running.observed_state == "cleanup_running" and running.error_code == "IMPORT_FAILED"
        assert running.staged_files is None and running.staged_bytes is None
        held = (await db.select_all_rows(controller))[0]
        assert held.hold_kind == "import" and held.hold_started_at > old_hold
        assert adopted.is_dir() and cluster.observations[CELL].statefulset_replicas == 0
        with pytest.raises(asyncpg.UniqueViolationError):
            await app.execute("INSERT INTO exomem_cloud_imports(cell_id,kind,declared_bytes,declared_files) VALUES($1,'adopt',1,1)", CELL)

        def run_discard(manifest):
            command = manifest["spec"]["template"]["spec"]["containers"][0]["command"]
            # Execute the real cell command with the rendered operation and its adoption flag.
            assert cloud_import_job.main([*command[command.index("cell-import") + 1:], "--vault", str(vault),
                                          "--termination-log", str(tmp_path / "result.json")]) == 0
            jobs["discard"] = ImportJob(exists=True, succeeded=True, pods_present=True)

        monkeypatch.setattr(cluster, "run_job", run_discard)
        monkeypatch.setattr(ImportStorage, "cleanup", lambda self, item: None)
        await step()
        assert not adopted.exists() and not identity.exists()
        jobs.clear()  # The process completed before its Job's success observation survived restart.
        await step()
        assert not adopted.exists()  # Full discard repeats safely without the private identity.
        await step()
        assert (await db.read_import(controller, CELL)).observed_state == "cleanup_discard_stopping"
        assert (await db.select_all_rows(controller))[0].hold_kind == "import"
        jobs.clear()
        await step()
        outcome = await app.fetchrow("SELECT observed_state,error_code FROM exomem_cloud_imports WHERE import_id=$1", import_id)
        assert dict(outcome) == {"observed_state": "discarded", "error_code": "IMPORT_FAILED"}
        assert not adopted.exists() and (await db.select_all_rows(controller))[0].hold_kind is None
    finally:
        await app.close()
        await controller.close()


def _failed_discard(report: dict):
    """The decision on the pass that sees the discard Job failed with `report`, and the row it leaves."""
    row = CellRow(CELL, uuid.uuid4(), 10, 0, "running", None, 1, hold_kind="import", hold_started_at=NOW)
    item = ImportRow(str(uuid.uuid4()), CELL, "adopt", 4, 1, "discard_requested", NOW,
                     observed_state="cleanup_running")
    observed = ClusterObservation(statefulset_exists=True, statefulset_image="example:cell", statefulset_hold_kind="import")
    failed_at = NOW + timedelta(minutes=1)
    decision = decide_import(row, item, observed, {"discard": ImportJob(exists=True, failed=True, report=report)},
                             now=failed_at)
    pending = replace(item, observed_state="cleanup_pending", updated_at=failed_at, **{
        name: decision.import_updates[name] for name in ("error_code", "urls_expire_at")
        if name in decision.import_updates})
    return decision, row, pending, failed_at


def _next_attempt(row, pending, failed_at, wait: timedelta):
    released = replace(row, hold_kind=None, hold_started_at=None)
    unheld = ClusterObservation(statefulset_exists=True, statefulset_image="example:cell")
    return decide_import(released, pending, unheld, {}, now=failed_at + wait)


def test_a_failed_cleanup_records_the_code_its_report_names():
    decision, *_ = _failed_discard({"ok": False, "error": "IMPORT_RENAMES_PENDING", "retry": "owner"})
    assert decision.import_updates["error_code"] == "IMPORT_RENAMES_PENDING"
    assert decision.import_updates["observed_state"] == "cleanup_pending"
    unnamed, *_ = _failed_discard({})
    assert unnamed.import_updates["error_code"] == "IMPORT_FAILED"


def test_a_discard_that_waits_on_the_owner_does_not_stop_the_cell_again_for_a_day():
    _, row, pending, failed_at = _failed_discard({"ok": False, "error": "IMPORT_RENAMES_PENDING", "retry": "owner"})
    assert _next_attempt(row, pending, failed_at, timedelta(hours=2)) is None
    assert _next_attempt(row, pending, failed_at, timedelta(hours=23, minutes=59)) is None
    retry = _next_attempt(row, pending, failed_at, timedelta(hours=24))
    assert retry.hold_kind == "import" and retry.replicas == 0
    assert retry.import_updates["observed_state"] == "cleanup_running"


def test_a_discard_that_failed_for_a_reason_a_retry_may_fix_stops_the_cell_again_after_an_hour():
    _, row, pending, failed_at = _failed_discard({"ok": False, "error": "IMPORT_FAILED"})
    assert _next_attempt(row, pending, failed_at, timedelta(minutes=59)) is None
    retry = _next_attempt(row, pending, failed_at, timedelta(hours=1))
    assert retry.hold_kind == "import" and retry.import_updates["observed_state"] == "cleanup_running"
