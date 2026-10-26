"""C6 owner exports: the hold, the verified upload, links, expiry and isolation."""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import asyncpg
import pytest
from botocore.exceptions import ClientError

from cellctl import db, reconcile
from cellctl.capacity import GIB
from cellctl.exports import (
    ExportJob,
    ExportRow,
    ExportSize,
    decide_export,
    etag_digest,
    export_deadline,
    export_size,
)
from cellctl.manifests import CellManifestSpec, namespace_name, render_export_job
from cellctl.state import CellRow, ClusterObservation
from cellctl.storage.fake_b2 import FakeB2
from cellctl.storage.fake_hetzner import FakeHetznerVolumeProvider

from .test_reconcile import (
    FakeClusterGateway,
    _cluster_config,
    _observed_from_applied,
    _secrets_config,
    _seed_cell,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
CELL = "aaaaaaaaaaaaaaaa"
OTHER_CELL = "bbbbbbbbbbbbbbbb"
IMAGE = "registry.example/cell@sha256:" + "a" * 64
EXPORT_ID = "00000000-0000-4000-8000-000000000001"
MIB = 1024 * 1024
REPORT = {"bytes": 5, "sha256": "b" * 64, "parts": 1, "etag_digest": "c" * 64, "used": 4096, "total": 8192}
# Shared with the cell command's own test (tests/test_cloud_export.py).
DIGEST_VECTOR = "91124627b9b8d6bffcbbfb35d09b5fb3cea67f87e4e84d341fac87ba0d0586da"


def _row(**fields) -> CellRow:
    values = {"observed_image": IMAGE, "observed_state": "running", "ready": True, **fields}
    return CellRow(CELL, uuid.uuid4(), 10, 0, values.pop("desired_state", "running"), None, 1, **values)


def _export(**fields) -> ExportRow:
    return ExportRow(EXPORT_ID, CELL, fields.pop("requested_state", "requested"), NOW, **fields)


SERVING = ClusterObservation(statefulset_exists=True, statefulset_image=IMAGE, statefulset_replicas=1,
                             pod_exists=True, pod_uses_volume=True, pod_ready=True)
HELD = replace(SERVING, statefulset_hold_kind="export", statefulset_hold_started_at=NOW, statefulset_replicas=0)
DRAINED = replace(HELD, pod_exists=False, pod_uses_volume=False, pod_ready=False)
PACKING = _export(observed_state="packing", upload_id="upload-one")
READY = replace(PACKING, observed_state="ready", upload_id=None, archive_bytes=5, archive_sha256="b" * 64,
                ready_at=NOW, expires_at=NOW + timedelta(hours=24))


def test_a_stopped_cells_export_never_asks_for_a_replica():
    """Defect: exporting a suspended account starts its runtime."""
    row = _row(desired_state="stopped", observed_state="stopped", ready=False)
    held_row = replace(row, hold_kind="export", hold_started_at=NOW)
    stopped = replace(DRAINED, statefulset_hold_kind=None)
    passes = [
        decide_export(row, _export(), stopped, ExportJob(), now=NOW),
        decide_export(held_row, PACKING, DRAINED, ExportJob(), now=NOW),
        decide_export(held_row, PACKING, DRAINED, ExportJob(exists=True, succeeded=True, report=REPORT), now=NOW),
        decide_export(held_row, READY, DRAINED, ExportJob(exists=True, succeeded=True, report=REPORT), now=NOW),
        decide_export(held_row, READY, DRAINED, ExportJob(), now=NOW),
    ]
    assert [decision.replicas for decision in passes] == [0, 0, 0, 0, 0]
    assert passes[0].export_transport == "create" and passes[0].hold_kind == "export"
    assert passes[1].run_export_job
    assert passes[2].export_transport == "complete"
    assert passes[2].export_updates["observed_state"] == "packing"
    assert "expires_at" not in passes[2].export_updates
    assert passes[3].stop_export_job and passes[3].hold_kind == "export"
    assert passes[4].hold_kind is None and passes[4].row_updates["hold_kind"] is None


def test_an_export_waits_while_its_volume_is_unavailable_but_a_packing_one_keeps_its_hold():
    """Defect: an export starts on a volume that is due to move, or a predecessor's
    leftover hold keeps the cell stopped while the move waits."""
    row = _row()
    held_row = replace(row, hold_kind="export", hold_started_at=NOW)
    assert decide_export(row, _export(), SERVING, ExportJob(), now=NOW, volume_unavailable=True) is None
    leftover = decide_export(held_row, _export(), DRAINED, ExportJob(), now=NOW, volume_unavailable=True)
    assert leftover.hold_kind is None and leftover.replicas == 1
    packing = decide_export(held_row, PACKING, DRAINED, ExportJob(), now=NOW, volume_unavailable=True)
    assert packing is not None and packing.run_export_job


def test_a_running_cells_export_waits_for_the_volume_and_restarts_the_runtime_after():
    """Defect: the export Job mounts the volume beside the runtime, or the cell stays stopped after."""
    row = _row()
    held_row = replace(row, hold_kind="export", hold_started_at=NOW)
    start = decide_export(row, _export(), SERVING, ExportJob(), now=NOW)
    assert start.replicas == 0 and start.export_updates == {"observed_state": "packing"}
    draining = decide_export(held_row, PACKING, HELD, ExportJob(), now=NOW)
    assert draining.replicas == 0 and not draining.run_export_job
    assert decide_export(held_row, PACKING, DRAINED, ExportJob(), now=NOW).run_export_job
    released = decide_export(held_row, READY, DRAINED, ExportJob(), now=NOW)
    assert released.replicas == 1 and released.hold_kind is None


def test_an_export_past_its_deadline_fails_and_frees_the_cell():
    """Defect: a hung export keeps the account stopped."""
    held_row = _row(hold_kind="export", hold_started_at=NOW)
    late = NOW + timedelta(minutes=30)
    running_job = ExportJob(exists=True, active=True, pods_present=True)
    expired = decide_export(held_row, PACKING, DRAINED, running_job, now=late)
    assert expired.export_updates == {"observed_state": "failed", "error_code": "EXPORT_HOLD_EXPIRED"}
    assert expired.export_transport == "remove" and expired.stop_export_job
    failed = replace(PACKING, observed_state="failed", error_code="EXPORT_HOLD_EXPIRED")
    assert decide_export(held_row, failed, DRAINED, running_job, now=late).hold_kind == "export"
    assert decide_export(held_row, failed, DRAINED, ExportJob(), now=late).replicas == 1


def test_a_larger_cell_gets_the_deadline_once_per_started_10_gib():
    """Defect: a flat deadline fails every export of a cell grown past 10 GiB."""
    assert export_deadline(_row()) == timedelta(minutes=30)
    assert export_deadline(_row(grown_storage_gib=11)) == timedelta(minutes=60)
    large = _row(grown_storage_gib=110, hold_kind="export", hold_started_at=NOW)
    running_job = ExportJob(exists=True, active=True, pods_present=True)
    assert decide_export(large, PACKING, DRAINED, running_job, now=NOW + timedelta(minutes=329)).export_updates == {}
    expired = decide_export(large, PACKING, DRAINED, running_job, now=NOW + timedelta(minutes=330))
    assert expired.export_updates == {"observed_state": "failed", "error_code": "EXPORT_HOLD_EXPIRED"}


# The controller branches only on whether the Job's token is a C6 code.
@pytest.mark.parametrize(("reported", "recorded"), [
    ("SOURCE_CHANGED_DURING_EXPORT", "SOURCE_CHANGED_DURING_EXPORT"),
    ("Traceback: /data/vault/Example note.md", "EXPORT_FAILED"),
])
def test_a_failed_job_reaches_the_row_only_as_a_c6_code(reported, recorded):
    """Defect: a Job's free text, such as a note path, reaches the control plane."""
    held_row = _row(hold_kind="export", hold_started_at=NOW)
    job = ExportJob(exists=True, failed=True, report={"error": reported})
    decision = decide_export(held_row, PACKING, DRAINED, job, now=NOW)
    assert decision.export_updates == {"observed_state": "failed", "error_code": recorded}


def test_deleting_the_cell_interrupts_its_export_without_holding_up_the_deletion():
    """Defect: an export in flight keeps a deleted tenant's cell from being torn down."""
    deleting = _row(desired_state="deleted", hold_kind="export", hold_started_at=NOW)
    interrupted = decide_export(deleting, PACKING, DRAINED, ExportJob(exists=True, active=True), now=NOW)
    assert not interrupted.apply_manifests and interrupted.export_transport == "remove"
    assert interrupted.export_updates == {"observed_state": "failed", "error_code": "EXPORT_INTERRUPTED"}
    failed = replace(PACKING, observed_state="failed", error_code="EXPORT_INTERRUPTED")
    assert decide_export(deleting, failed, DRAINED, ExportJob(), now=NOW) is None


def test_an_export_waits_while_another_hold_owns_the_volume():
    """Defect: an export stops a cell in the middle of its backup."""
    backing_up = _row(hold_kind="backup", hold_started_at=NOW)
    observed = replace(SERVING, statefulset_hold_kind="snapshot-backup", statefulset_hold_started_at=NOW)
    assert decide_export(backing_up, _export(), observed, ExportJob(), now=NOW) is None


def test_an_export_covers_everything_the_cell_can_hold_in_at_most_1000_parts():
    """Defect: a cell grown past 10 GiB, or past its last measurement, can never be exported."""
    assert export_size(_row()) == ExportSize(max_bytes=10 * GIB, part_bytes=64 * MIB, parts=164)
    grown = export_size(_row(grown_storage_gib=110))
    assert grown.max_bytes == 110 * GIB and grown.parts <= 1000 and grown.part_bytes % MIB == 0
    assert grown.parts * grown.part_bytes >= grown.max_bytes * 1.02  # the ZIP's own headers
    measured = export_size(_row(used_bytes=GIB, filesystem_bytes=12 * GIB, usage_observed_at=NOW))
    assert measured.max_bytes == 12 * GIB


def test_the_part_digest_matches_the_cell_commands_vector():
    """Defect: cellctl and the cell disagree on the digest, so every export fails."""
    assert etag_digest(['"0cc175b9c0f1b6a831c399e269772661"', '"92eb5ffee6ae2fec3ad71c777531578f"']) == DIGEST_VECTOR


def test_the_export_job_carries_part_urls_but_no_storage_credential():
    """Defect: the Job could reach other objects, or stages an archive without reserving node storage."""
    spec = CellManifestSpec(cell_id=CELL, image=IMAGE, replicas=0, read_only=False, storage_gib=20,
                            hold_kind="export", hold_started_at=NOW.isoformat())
    size = ExportSize(max_bytes=20 * GIB, part_bytes=64 * MIB, parts=327)
    job = render_export_job(spec, _export(), size, part_urls=["https://storage.test/one", "https://storage.test/two"],
                            deadline_seconds=1800)
    pod = job["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["command"][-4:] == ["--part-url", "https://storage.test/one", "--part-url", "https://storage.test/two"]
    assert container["command"][container["command"].index("--max-bytes") + 1] == str(20 * GIB)
    assert {env["name"]: set(env) for env in container["env"]} == {
        name: {"name", "value"} for name in ("EXOMEM_CLOUD_CELL", "EXOMEM_CLOUD_CELL_ID", "TMPDIR", "EXOMEM_LOG_DIR")}
    assert "envFrom" not in container
    assert "ephemeral-storage" not in container["resources"]["requests"]
    assert all(volume.get("emptyDir", {}).get("medium") == "Memory"
               for volume in pod["volumes"] if "emptyDir" in volume)
    assert job["spec"]["template"]["metadata"]["labels"]["exomem.io/cell-job"] == "export"


def test_a_local_export_job_tolerates_its_cordoned_node():
    """Defect: the removal playbook cordons a local cell's node, so its export Job never schedules."""
    spec = CellManifestSpec(cell_id=CELL, image=IMAGE, replicas=0, read_only=False, storage_gib=20,
                            local_volume=True, hold_kind="export", hold_started_at=NOW.isoformat())
    size = ExportSize(max_bytes=20 * GIB, part_bytes=64 * MIB, parts=327)
    job = render_export_job(spec, _export(), size, part_urls=["https://storage.test/one"], deadline_seconds=1800)
    assert job["spec"]["template"]["spec"]["tolerations"] == [
        {"key": "node.kubernetes.io/unschedulable", "operator": "Exists", "effect": "NoSchedule"}]


# --- C6 against real Postgres roles, with object storage at its SDK boundary ---------


class FakeS3:
    """S3 at the SDK boundary. One store; each client is one B2 key, refused
    outside its cell's prefix exactly as FakeB2 refuses that key."""

    def __init__(self, b2: FakeB2) -> None:
        self.b2 = b2
        self.uploads: dict[str, dict] = {}
        self.versions: dict[str, list[tuple[str, bytes]]] = {}
        self.calls: list[tuple[str, str]] = []  # (key id, object name)
        self.abort_error: Exception | None = None
        self._ids = itertools.count(1)

    def client(self, cluster_config, key_id: str, key_secret: str) -> FakeS3Client:
        return FakeS3Client(self, key_id)

    def put_part(self, url: str, data: bytes) -> str:
        """What the export Job's PUT through a presigned part URL does."""
        query = {name: values[0] for name, values in parse_qs(urlsplit(url).query).items()}
        etag = '"' + hashlib.md5(data).hexdigest() + '"'  # noqa: S324 - S3's part ETag
        self.uploads[query["upload_id"]]["parts"][int(query["part"])] = (etag, data)
        return etag


class FakeS3Client:
    def __init__(self, store: FakeS3, key_id: str) -> None:
        self.store, self.key_id = store, key_id

    def _use(self, name: str) -> None:
        self.store.b2.authorize(self.key_id, name)
        self.store.calls.append((self.key_id, name))

    def get_paginator(self, operation: str):
        return type("Paginator", (), {"paginate": lambda _self, **kwargs: [getattr(self, operation)(**kwargs)]})()

    def list_multipart_uploads(self, Bucket, Prefix):
        self._use(Prefix)
        return {"Uploads": [{"Key": upload["key"], "UploadId": upload_id}
                            for upload_id, upload in self.store.uploads.items() if upload["key"].startswith(Prefix)]}

    def list_object_versions(self, Bucket, Prefix):
        self._use(Prefix)
        return {"Versions": [{"Key": key, "VersionId": version} for key, versions in self.store.versions.items()
                             if key.startswith(Prefix) for version, _data in versions]}

    def list_parts(self, Bucket, Key, UploadId):
        self._use(Key)
        if UploadId not in self.store.uploads:
            raise ClientError({"Error": {"Code": "NoSuchUpload"}}, "ListParts")
        parts = self.store.uploads[UploadId]["parts"]
        return {"Parts": [{"PartNumber": number, "ETag": parts[number][0], "Size": len(parts[number][1])}
                          for number in sorted(parts)]}

    def create_multipart_upload(self, Bucket, Key, **_headers):
        self._use(Key)
        upload_id = f"upload-{next(self.store._ids)}"
        self.store.uploads[upload_id] = {"key": Key, "parts": {}}
        return {"UploadId": upload_id}

    def generate_presigned_url(self, operation, Params, ExpiresIn, HttpMethod=None):
        self._use(Params["Key"])
        query = {"op": operation, "key_id": self.key_id, "expires": ExpiresIn, "upload_id": Params.get("UploadId"),
                 "part": Params.get("PartNumber"), "version": Params.get("VersionId")}
        return f"https://storage.test/{Params['Bucket']}/{Params['Key']}?" + "&".join(
            f"{name}={value}" for name, value in query.items() if value is not None)

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):
        self._use(Key)
        upload = self.store.uploads.pop(UploadId)
        data = b"".join(upload["parts"][part["PartNumber"]][1] for part in MultipartUpload["Parts"])
        version = f"version-{next(self.store._ids)}"
        self.store.versions.setdefault(Key, []).append((version, data))
        return {"VersionId": version}

    def head_object(self, Bucket, Key, VersionId=None):
        self._use(Key)
        return {"ContentLength": len(dict(self.store.versions[Key])[VersionId]), "VersionId": VersionId}

    def abort_multipart_upload(self, Bucket, Key, UploadId):
        self._use(Key)
        if self.store.abort_error is not None:
            raise self.store.abort_error
        self.store.uploads.pop(UploadId, None)

    def delete_object(self, Bucket, Key, VersionId):
        self._use(Key)
        self.store.versions[Key] = [entry for entry in self.store.versions[Key] if entry[0] != VersionId]
        if not self.store.versions[Key]:
            del self.store.versions[Key]


class Harness:
    """One cellctl process against the real C6 schema and grants."""

    def __init__(self, cell_db, monkeypatch) -> None:
        self.cell_db = cell_db
        self.cluster = FakeClusterGateway()
        self.b2 = FakeB2()
        self.s3 = FakeS3(self.b2)
        self.jobs: dict[str, ExportJob] = {}
        self.cluster.observe_export_job = lambda namespace, export_id, verify=False: self.jobs.get(
            export_id + ("-verify" if verify else ""), ExportJob())
        monkeypatch.setattr(reconcile, "_cell_s3_client", self.s3.client)

    async def __aenter__(self) -> Harness:
        self.app = await asyncpg.connect(self.cell_db.dsn(role="substrate_app"))
        self.cellctl = await asyncpg.connect(self.cell_db.dsn(role="exomem_cellctl"))
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.app.close()
        await self.cellctl.close()

    async def run(self, now: datetime = NOW) -> None:
        await reconcile.reconcile_once(self.cellctl, self.cluster, self.b2, FakeHetznerVolumeProvider(),
                                       _secrets_config(), _cluster_config(), now=now)

    async def serve(self, cell_id: str, desired_state: str = "running") -> None:
        await _seed_cell(self.cell_db, cell_id, f"tenant-{cell_id}")
        await self.run()
        self.cluster.observations[cell_id] = _observed_from_applied(self.cluster, cell_id)
        await self.run()
        if desired_state != "running":
            await self.app.execute("UPDATE exomem_cloud_cells SET desired_state=$2 WHERE cell_id=$1", cell_id,
                                   desired_state)
            await self.run()
            self.cluster.observations[cell_id] = _observed_from_applied(self.cluster, cell_id)
            await self.run()

    async def request(self, cell_id: str) -> str:
        return str(await self.app.fetchval(
            "INSERT INTO exomem_cloud_exports(cell_id) VALUES($1) RETURNING export_id", cell_id))

    async def export(self, export_id: str) -> asyncpg.Record:
        return await self.app.fetchrow("SELECT * FROM exomem_cloud_exports WHERE export_id=$1", uuid.UUID(export_id))

    async def cell(self, cell_id: str) -> CellRow:
        return next(row for row in await db.select_all_rows(self.cellctl) if row.cell_id == cell_id)

    async def verify(self, export_id: str) -> None:
        item = await self.export(export_id)
        assert item["observed_state"] == "packing" and item["archive_version_id"]
        assert item["ready_at"] is None and item["download_url"] is None
        self.jobs[export_id + "-verify"] = ExportJob(exists=True, succeeded=True,
            report={"verified": True, "bytes": item["archive_bytes"], "sha256": item["archive_sha256"],
                    "version": item["archive_version_id"]})
        await self.run()

    async def pack(self, cell_id: str, export_id: str, archive: bytes) -> dict:
        """Hold the cell, release its volume, then act as the export Job: upload
        the archive to the rendered part URLs and report it."""
        await self.run()
        self.cluster.observations[cell_id] = _observed_from_applied(self.cluster, cell_id)
        await self.run()
        name = f"export-{uuid.UUID(export_id).hex}"
        job = next(job for job in reversed(self.cluster.jobs) if job["metadata"]["name"] == name)
        command = job["spec"]["template"]["spec"]["containers"][0]["command"]
        urls = [command[index + 1] for index, arg in enumerate(command) if arg == "--part-url"]
        part_bytes = int(command[command.index("--part-bytes") + 1])
        chunks = [archive[offset:offset + part_bytes] for offset in range(0, len(archive), part_bytes)]
        etags = [self.s3.put_part(url, chunk) for url, chunk in zip(urls, chunks, strict=False)]
        report = {"bytes": len(archive), "sha256": hashlib.sha256(archive).hexdigest(), "parts": len(etags),
                  "etag_digest": etag_digest(etags), "used": 3 * MIB, "total": 10 * 1024 * MIB}
        self.jobs[export_id] = ExportJob(exists=True, succeeded=True, report=report, pods_present=True,
                                         finished_at=NOW)
        return {"urls": urls, "job": job}


async def test_a_running_cells_export_is_verified_kept_linked_and_deleted(cell_db, monkeypatch):
    """Defect: an export never becomes downloadable, outlives its TTL, or links an unverified object."""
    archive = b"PK\x03\x04 canonical archive bytes"
    async with Harness(cell_db, monkeypatch) as harness:
        await harness.serve(CELL)
        export_id = await harness.request(CELL)
        await harness.run()
        held = await harness.cell(CELL)
        assert held.hold_kind == "export"
        assert (await harness.export(export_id))["observed_state"] == "packing"
        assert harness.cluster.events[-1] == ("apply_statefulset", namespace_name(CELL), 0)
        assert harness.cluster.jobs == []  # the runtime's pod still holds the volume

        packed = await harness.pack(CELL, export_id, archive)
        assert packed["job"]["metadata"]["name"] == f"export-{uuid.UUID(export_id).hex}"
        await harness.run()
        await harness.verify(export_id)
        ready = await harness.export(export_id)
        assert ready["observed_state"] == "ready" and ready["archive_bytes"] == len(archive)
        assert ready["expires_at"] == NOW + timedelta(hours=24) and ready["archive_version_id"]
        usage = await harness.cell(CELL)
        assert (usage.used_bytes, usage.filesystem_bytes, usage.usage_observed_at) == (3 * MIB, 10 * 1024 * MIB, NOW)

        harness.jobs[export_id] = ExportJob()  # the deleted Job is gone
        harness.jobs[export_id + "-verify"] = ExportJob()
        await harness.run()
        assert (await harness.cell(CELL)).hold_kind is None
        assert harness.cluster.events[-1] == ("apply_statefulset", namespace_name(CELL), 1)

        await harness.app.execute("UPDATE exomem_cloud_exports SET link_requested_at=now() WHERE export_id=$1",
                                  uuid.UUID(export_id))
        await harness.run(NOW + timedelta(minutes=1))
        linked = await harness.export(export_id)
        link = parse_qs(urlsplit(linked["download_url"]).query)
        assert link["version"] == [ready["archive_version_id"]] and link["expires"] == ["300"]
        assert linked["download_url_expires_at"] == NOW + timedelta(minutes=1, seconds=300)
        assert linked["link_issued_at"] >= linked["link_requested_at"]
        await harness.run(NOW + timedelta(minutes=2))
        again = await harness.export(export_id)
        assert again["download_url_expires_at"] == linked["download_url_expires_at"]  # each request is answered once

        await harness.run(NOW + timedelta(hours=24))
        expired = await harness.export(export_id)
        assert expired["observed_state"] == "expired" and expired["download_url"] is None
        assert harness.s3.versions == {} and harness.s3.uploads == {}


async def test_a_stopped_cell_is_exported_without_its_runtime_starting(cell_db, monkeypatch):
    """Defect: exporting a suspended account starts its runtime."""
    async with Harness(cell_db, monkeypatch) as harness:
        await harness.serve(CELL, desired_state="stopped")
        applied_before = len(harness.cluster.events)
        export_id = await harness.request(CELL)
        await harness.pack(CELL, export_id, b"PK archive of a stopped cell")
        await harness.run()
        await harness.verify(export_id)
        harness.jobs[export_id] = ExportJob()
        harness.jobs[export_id + "-verify"] = ExportJob()
        await harness.run()
        assert (await harness.export(export_id))["observed_state"] == "ready"
        assert (await harness.cell(CELL)).hold_kind is None
        statefulset_replicas = [event[2] for event in harness.cluster.events[applied_before:]
                                if event[0] == "apply_statefulset"]
        assert statefulset_replicas and set(statefulset_replicas) == {0}


async def test_an_export_never_recreates_a_lost_claim(cell_db, monkeypatch):
    """Defect: a packing export's hold re-applies the cell's bundle after its claim
    is gone, and the apply creates an empty claim in place of the tenant's volume."""
    async with Harness(cell_db, monkeypatch) as harness:
        await harness.serve(CELL)
        await db.write_observed(harness.cellctl, CELL, {"volume_id": "vol-1"})
        harness.cluster.observations[CELL] = _observed_from_applied(harness.cluster, CELL, pvc_volume_id="vol-1")
        export_id = await harness.request(CELL)
        await harness.run()
        assert (await harness.export(export_id))["observed_state"] == "packing"
        harness.cluster.observations[CELL] = _observed_from_applied(
            harness.cluster, CELL, pvc_exists=False, pvc_bound=False, pvc_volume_id=None,
            pod_exists=False, pod_uses_volume=False, pod_ready=False, ready_pod_image=None)
        events_before = len(harness.cluster.events)

        await harness.run(NOW + timedelta(seconds=10))

        assert not [event for event in harness.cluster.events[events_before:] if event[0] == "apply_statefulset"]
        assert (await harness.cell(CELL)).last_error_code == "VOLUME_MISSING"

        # Deleting the cell still ends the export and removes its open upload.
        await harness.app.execute("UPDATE exomem_cloud_cells SET desired_state='deleted' WHERE cell_id=$1", CELL)
        await harness.run(NOW + timedelta(seconds=20))
        ended = await harness.export(export_id)
        assert (ended["observed_state"], ended["error_code"]) == ("failed", "EXPORT_INTERRUPTED")
        assert harness.s3.uploads == {}


async def test_a_substituted_part_never_completes(cell_db, monkeypatch):
    """Defect: someone holding a part URL replaces the owner's archive before completion."""
    async with Harness(cell_db, monkeypatch) as harness:
        await harness.serve(CELL)
        export_id = await harness.request(CELL)
        packed = await harness.pack(CELL, export_id, b"PK genuine archive")
        harness.s3.put_part(packed["urls"][0], b"PK forged!!archive")  # same length, other bytes
        await harness.run()
        refused = await harness.export(export_id)
        assert (refused["observed_state"], refused["error_code"]) == ("failed", "EXPORT_UPLOAD_FAILED")
        assert refused["upload_id"] is None and harness.s3.uploads == {} and harness.s3.versions == {}
        assert ("delete_job", namespace_name(CELL), f"export-{uuid.UUID(export_id).hex}") in harness.cluster.events


async def test_deleting_the_cell_aborts_its_export_upload_until_the_abort_succeeds(cell_db, monkeypatch):
    """Defect: one failed abort leaves a deleted tenant's half-uploaded export in object storage, unretried."""
    async with Harness(cell_db, monkeypatch) as harness:
        await harness.serve(CELL)
        export_id = await harness.request(CELL)
        await harness.pack(CELL, export_id, b"PK archive")
        harness.s3.abort_error = ClientError({"Error": {"Code": "InternalError"}}, "AbortMultipartUpload")
        await harness.app.execute("UPDATE exomem_cloud_cells SET desired_state='deleted' WHERE cell_id=$1", CELL)
        await harness.run()
        await harness.run()
        interrupted = await harness.export(export_id)
        assert (interrupted["observed_state"], interrupted["error_code"]) == ("failed", "EXPORT_INTERRUPTED")
        assert interrupted["upload_id"] is not None and harness.s3.uploads

        harness.s3.abort_error = None
        await harness.run()
        assert (await harness.export(export_id))["upload_id"] is None and harness.s3.uploads == {}


async def test_a_large_cells_job_and_part_urls_last_as_long_as_its_hold(cell_db, monkeypatch):
    """Defect: a large cell's export Job or part URLs expire long before its hold deadline."""
    async with Harness(cell_db, monkeypatch) as harness:
        await harness.serve(CELL)
        await harness.cellctl.execute("UPDATE exomem_cloud_cells SET grown_storage_gib=110 WHERE cell_id=$1", CELL)
        export_id = await harness.request(CELL)
        packed = await harness.pack(CELL, export_id, b"PK archive")
        assert packed["job"]["spec"]["activeDeadlineSeconds"] == 330 * 60
        assert {parse_qs(urlsplit(url).query)["expires"][0] for url in packed["urls"]} == {str(330 * 60)}


async def test_each_export_is_signed_only_with_its_own_cells_key(cell_db, monkeypatch):
    """Defect: one tenant's export is created, completed or linked with another cell's key or prefix."""
    async with Harness(cell_db, monkeypatch) as harness:
        await harness.serve(CELL)
        await harness.serve(OTHER_CELL)
        exports = {cell_id: await harness.request(cell_id) for cell_id in (CELL, OTHER_CELL)}
        for cell_id, export_id in exports.items():
            await harness.pack(cell_id, export_id, f"PK archive of {cell_id}".encode())
        await harness.run()
        for export_id in exports.values():
            await harness.verify(export_id)
            await harness.app.execute("UPDATE exomem_cloud_exports SET link_requested_at=now() WHERE export_id=$1",
                                      uuid.UUID(export_id))
        await harness.run()
        for cell_id, export_id in exports.items():
            key_id = (await harness.cell(cell_id)).b2_key_id
            url = urlsplit((await harness.export(export_id))["download_url"])
            assert url.path == f"/exomem-cloud-backups/cells/{cell_id}/exports/{export_id}.zip"
            assert parse_qs(url.query)["key_id"] == [key_id]
            assert {name for used, name in harness.s3.calls if used == key_id} <= {
                f"cells/{cell_id}/exports/{export_id}.zip"}


async def test_each_role_holds_exactly_its_c6_columns(cell_db):
    """Defect: the web app can forge an export's outcome, cellctl can forge a request, or the gateway reads exports."""
    owner = await asyncpg.connect(cell_db.dsn(role="substrate_owner"))
    try:
        rows = await owner.fetch(
            "SELECT grantee, privilege_type, column_name FROM information_schema.column_privileges "
            "WHERE table_name='exomem_cloud_exports' AND grantee IN ('substrate_app','exomem_cellctl','exomem_gateway')")
        usage = await owner.fetch(
            "SELECT grantee FROM information_schema.column_privileges WHERE table_name='exomem_cloud_cells' "
            "AND privilege_type='UPDATE' AND column_name='used_bytes' "
            "AND grantee IN ('substrate_app','exomem_cellctl','exomem_gateway')")
        app_deletes = await owner.fetchval("SELECT has_table_privilege('substrate_app','exomem_cloud_exports','DELETE')")
    finally:
        await owner.close()
    granted = {(row["grantee"], row["privilege_type"]): set() for row in rows}
    for row in rows:
        granted[(row["grantee"], row["privilege_type"])].add(row["column_name"])
    observation = {"observed_state", "error_code", "archive_bytes", "archive_sha256", "archive_version_id", "upload_id",
                   "ready_at", "expires_at", "download_url", "download_url_expires_at", "link_issued_at", "packing_report"}
    assert granted[("substrate_app", "INSERT")] == {"export_id", "cell_id", "requested_state", "link_requested_at"}
    assert granted[("substrate_app", "UPDATE")] == {"requested_state", "link_requested_at"}
    assert granted[("exomem_cellctl", "UPDATE")] == observation
    assert ("exomem_cellctl", "INSERT") not in granted
    assert not [key for key in granted if key[0] == "exomem_gateway"]
    assert {row["grantee"] for row in usage} == {"exomem_cellctl"}
    assert app_deletes is False  # C6 rows are the content-free record; nothing deletes them


async def test_a_request_and_a_link_request_wake_cellctl_but_its_own_writes_do_not(cell_db):
    """Defect: a link request waits for the idle poll, or cellctl's writes wake it in a loop."""
    await _seed_cell(cell_db, CELL, "tenant-notify")
    app = await asyncpg.connect(cell_db.dsn(role="substrate_app"))
    controller = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    woken: list[str] = []
    await controller.add_listener("exomem_cloud_cells", lambda *args: woken.append(args[-1]))
    try:
        export_id = await app.fetchval("INSERT INTO exomem_cloud_exports(cell_id) VALUES($1) RETURNING export_id", CELL)
        await db.write_export(controller, str(export_id), {"observed_state": "packing", "upload_id": "upload-one"})
        await app.execute("UPDATE exomem_cloud_exports SET link_requested_at=now() WHERE export_id=$1", export_id)
        for _ in range(50):  # notifications from another session arrive asynchronously
            await controller.execute("SELECT 1")
            if len(woken) >= 2:
                break
            await asyncio.sleep(0.05)
        assert woken == [CELL, CELL]
    finally:
        await app.close()
        await controller.close()


async def test_verification_get_capability_stays_in_its_secret_and_failure_removes_only_that_export(cell_db, monkeypatch):
    """Routine Pod readers must never receive the completed archive's GET capability."""
    import base64
    import json

    from cellctl.exports import verification_secret_name

    async with Harness(cell_db, monkeypatch) as harness:
        await harness.serve(CELL)
        export_id = await harness.request(CELL)
        await harness.pack(CELL, export_id, b"PK private completed archive")
        await harness.run()
        item = await harness.export(export_id)
        assert item["observed_state"] == "packing"
        await harness.run()
        secret_name = verification_secret_name(export_id)
        secret_key = (namespace_name(CELL), "Secret", secret_name)
        secret = harness.cluster.applied[secret_key]
        capability = base64.b64decode(secret["data"]["get-url"]).decode()
        job = harness.cluster.jobs[-1]
        encoded_job = json.dumps(job)
        assert capability not in encoded_job and secret["data"]["get-url"] not in encoded_job
        container = job["spec"]["template"]["spec"]["containers"][0]
        reference = next(entry for entry in container["env"] if entry["name"] == "EXOMEM_EXPORT_VERIFY_URL")
        assert reference["valueFrom"]["secretKeyRef"] == {"name": secret_name, "key": "get-url"}
        assert all("persistentVolumeClaim" not in volume for volume in job["spec"]["template"]["spec"]["volumes"])
        unrelated = (namespace_name(CELL), "Secret", verification_secret_name(EXPORT_ID))
        harness.cluster.applied[unrelated] = {"unrelated": True}
        harness.jobs[export_id + "-verify"] = ExportJob(exists=True, succeeded=True,
            report={"verified": True, "bytes": item["archive_bytes"], "sha256": item["archive_sha256"],
                    "version": "wrong-version"})
        await harness.run()
        failed = await harness.export(export_id)
        assert failed["observed_state"] == "failed" and failed["download_url"] is None
        assert secret_key not in harness.cluster.applied and unrelated in harness.cluster.applied
        assert harness.s3.versions == {}
