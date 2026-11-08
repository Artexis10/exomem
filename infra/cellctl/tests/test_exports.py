"""C6 owner exports: the hold, the verified upload, links, expiry and isolation."""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import asyncpg
import pytest
from botocore.exceptions import ClientError
from kubernetes.client.exceptions import ApiException

from cellctl import db, reconcile
from cellctl.capacity import GIB
from cellctl.export_layout import MAX_PART_BYTES, job_name, staging_claim_name
from cellctl.exports import (
    ARCHIVE_BOUND,
    PACK,
    PAUSE,
    UPLOAD,
    VERIFY,
    ExportJob,
    ExportRow,
    ExportSize,
    decide_export,
    etag_digest,
    export_size,
    pause_window,
)
from cellctl.manifests import JOB_EGRESS_POLICY, CellManifestSpec, namespace_name, render_export_job
from cellctl.state import CellRow, ClusterObservation
from cellctl.storage.fake_hetzner import FakeHetznerVolumeProvider

from .fake_s3 import FakeS3
from .test_reconcile import (
    STORAGE_CLASS,
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
# The pause's numbers-only plan, with the statvfs measurement every cell Job reports.
PAUSE_REPORT = {"phase": "pause", "members": 3, "member_bytes": 4096, "evicted": 1, "evicted_bytes": 1024,
                "planned_bytes": 8192, "used": 3 * 1024 * 1024, "total": 10 * 1024 ** 3}
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
PACKING = _export(observed_state="packing", upload_id="upload-one",
                  packing_report=json.dumps({"step": "pause", "image": IMAGE}))
TRANSFERRING = replace(PACKING, packing_report=json.dumps({"step": "transfer", "image": IMAGE,
                                                           "paused_at": NOW.isoformat(), "planned_bytes": 8192}))


def test_a_stopped_cells_export_never_asks_for_a_replica():
    """Defect: exporting a suspended account starts its runtime."""
    row = _row(desired_state="stopped", observed_state="stopped", ready=False)
    held_row = replace(row, hold_kind="export", hold_started_at=NOW)
    stopped = replace(DRAINED, statefulset_hold_kind=None)
    paused = ExportJob(exists=True, succeeded=True, report=PAUSE_REPORT)
    passes = [
        decide_export(row, _export(), stopped, ExportJob(), now=NOW),
        decide_export(held_row, PACKING, DRAINED, ExportJob(), now=NOW),
        decide_export(held_row, PACKING, DRAINED, paused, now=NOW),
        decide_export(held_row, TRANSFERRING, DRAINED, paused, now=NOW),
        decide_export(held_row, TRANSFERRING, DRAINED, ExportJob(), now=NOW),
    ]
    assert [decision.replicas for decision in passes] == [0, 0, 0, 0, 0]
    assert passes[0].export_transport == "create" and passes[0].hold_kind == "export"
    assert passes[1].run_export_job
    assert json.loads(passes[2].export_updates["packing_report"])["step"] == "transfer"
    assert passes[2].stop_export_job and passes[2].hold_kind == "export"
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


def test_a_running_cells_pause_waits_for_the_volume_and_restarts_the_runtime_at_its_report():
    """Defect: the pause mounts the volume beside the runtime, the cell stays stopped for the
    whole upload, or the upload runs another image than the pause."""
    row = _row()
    held_row = replace(row, hold_kind="export", hold_started_at=NOW)
    start = decide_export(row, _export(), SERVING, ExportJob(), now=NOW)
    assert start.replicas == 0 and start.export_updates["observed_state"] == "packing"
    assert json.loads(start.export_updates["packing_report"]) == {"step": "pause", "image": IMAGE}
    draining = decide_export(held_row, PACKING, HELD, ExportJob(), now=NOW)
    assert draining.replicas == 0 and not draining.run_export_job
    assert decide_export(held_row, PACKING, DRAINED, ExportJob(), now=NOW).run_export_job
    released = decide_export(held_row, TRANSFERRING, DRAINED, ExportJob(), now=NOW)
    assert released.replicas == 1 and released.hold_kind is None
    assert decide_export(row, TRANSFERRING, SERVING, ExportJob(), now=NOW) is None  # the transfer needs no hold


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


def test_the_pause_lasts_as_long_as_copying_the_used_bytes_at_the_floor_rate():
    """Defect: a large but nearly empty cell stays stopped as long as its size allows, or a
    cell never measured fails while it still copies."""
    nearly_empty = _row(grown_storage_gib=110, used_bytes=GIB, usage_observed_at=NOW)
    # Two 2-minute start allowances, a third for the plan, and 1 GiB at 25 MiB/s (41 s).
    assert pause_window(nearly_empty) == timedelta(minutes=6, seconds=41)
    assert pause_window(_row(grown_storage_gib=110)) == timedelta(minutes=6, seconds=4506)  # all 110 GiB
    held = replace(nearly_empty, hold_kind="export", hold_started_at=NOW)
    running_job = ExportJob(exists=True, active=True, pods_present=True)
    assert decide_export(held, PACKING, DRAINED, running_job, now=NOW + timedelta(seconds=400)).export_updates == {}
    expired = decide_export(held, PACKING, DRAINED, running_job, now=NOW + timedelta(seconds=401))
    assert expired.export_updates == {"observed_state": "failed", "error_code": "EXPORT_HOLD_EXPIRED"}


# Each case is its own branch of the pause deadline.
@pytest.mark.parametrize(("job", "now", "diagnostic"), [
    (ExportJob(exists=True, active=True, created_at=NOW), NOW + timedelta(minutes=2), "staging_not_running"),
    (ExportJob(exists=True, active=True, pods_present=True, created_at=NOW, running_at=NOW),
     NOW + timedelta(seconds=161), "pause_below_copy_floor"),
])
def test_a_pause_that_misses_its_start_or_its_copy_rate_fails_and_frees_the_cell(job, now, diagnostic):
    """Defect: a staging volume that never attaches, or a copy far below the floor, keeps
    the account stopped until the whole window ends, with nothing to say why."""
    held = _row(used_bytes=GIB, usage_observed_at=NOW, hold_kind="export", hold_started_at=NOW)
    before = decide_export(held, PACKING, DRAINED, job, now=now - timedelta(seconds=1))
    assert before.export_updates == {} and before.hold_kind == "export"
    missed = decide_export(held, PACKING, DRAINED, job, now=now)
    assert (missed.export_updates["observed_state"], missed.export_updates["error_code"]) == ("failed", "EXPORT_FAILED")
    assert json.loads(missed.export_updates["packing_report"])["diagnostic"] == diagnostic
    assert missed.export_transport == "remove" and missed.stop_export_job
    failed = replace(PACKING, observed_state="failed", error_code="EXPORT_FAILED")
    assert decide_export(held, failed, DRAINED, ExportJob(), now=now).replicas == 1


# The controller branches only on whether the Job's token is a C6 code.
@pytest.mark.parametrize(("reported", "recorded"), [
    ("SOURCE_CHANGED_DURING_EXPORT", "SOURCE_CHANGED_DURING_EXPORT"),
    ("CASE_COLLISION", "CASE_COLLISION"),
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


def test_an_archive_up_to_the_bound_fits_1000_parts_of_at_most_1_gib():
    """Defect: a large plan needs more part URLs than a Job carries, or a part larger than
    the pack's two-part spool on staging."""
    assert export_size(10 * GIB) == ExportSize(max_bytes=10 * GIB, part_bytes=64 * MIB, parts=164)
    largest = export_size(ARCHIVE_BOUND)
    assert largest.parts <= 1000 and largest.part_bytes <= MAX_PART_BYTES and largest.part_bytes % MIB == 0
    assert largest.parts * largest.part_bytes >= ARCHIVE_BOUND * 1.02  # the ZIP's own headers


def test_the_part_digest_matches_the_cell_commands_vector():
    """Defect: cellctl and the cell disagree on the digest, so every export fails."""
    assert etag_digest(['"0cc175b9c0f1b6a831c399e269772661"', '"92eb5ffee6ae2fec3ad71c777531578f"']) == DIGEST_VECTOR


def test_the_upload_carries_part_urls_and_staging_but_no_cell_volume_or_credential():
    """Defect: the upload could reach other objects or the live vault, or stages the
    archive on node storage instead of the export's own claim."""
    spec = CellManifestSpec(cell_id=CELL, image=IMAGE, replicas=1, read_only=False, storage_gib=20)
    size = ExportSize(max_bytes=20 * GIB, part_bytes=64 * MIB, parts=327)
    job = render_export_job(spec, _export(), UPLOAD, size=size, deadline_seconds=1800,
                            part_urls=["https://storage.test/one", "https://storage.test/two"])
    pod = job["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["command"][container["command"].index("--part-url"):][:4] == [
        "--part-url", "https://storage.test/one", "--part-url", "https://storage.test/two"]
    assert container["command"][container["command"].index("--max-bytes") + 1] == str(20 * GIB)
    assert {env["name"]: set(env) for env in container["env"]} == {
        name: {"name", "value"} for name in ("EXOMEM_CLOUD_CELL", "EXOMEM_CLOUD_CELL_ID", "TMPDIR", "EXOMEM_LOG_DIR")}
    assert "envFrom" not in container
    claims = {volume["persistentVolumeClaim"]["claimName"] for volume in pod["volumes"] if "persistentVolumeClaim" in volume}
    assert claims == {staging_claim_name(EXPORT_ID)}
    assert not [volume for volume in pod["volumes"] if "secret" in volume or "projected" in volume]
    assert all(volume.get("emptyDir", {}).get("medium") == "Memory"
               for volume in pod["volumes"] if "emptyDir" in volume)
    assert job["spec"]["template"]["metadata"]["labels"]["exomem.io/cell-job"] == "export"


# Each step that mounts a volume carries the toleration; the verification mounts none.
@pytest.mark.parametrize("step", [PAUSE, PACK, UPLOAD])
def test_a_local_export_step_tolerates_its_cordoned_node(step):
    """Defect: the removal playbook cordons a local cell's node, so its export never schedules."""
    spec = CellManifestSpec(cell_id=CELL, image=IMAGE, replicas=0, read_only=False, storage_gib=20,
                            local_volume=True, hold_kind="export", hold_started_at=NOW.isoformat())
    size = ExportSize(max_bytes=20 * GIB, part_bytes=64 * MIB, parts=327)
    job = render_export_job(spec, _export(), step, size=size, part_urls=["https://storage.test/one"],
                            deadline_seconds=1800)
    assert job["spec"]["template"]["spec"]["tolerations"] == [
        {"key": "node.kubernetes.io/unschedulable", "operator": "Exists", "effect": "NoSchedule"}]


# --- C6 against real Postgres roles, with object storage at its SDK boundary ---------


class Harness:
    """One cellctl process against the real C6 schema and grants."""

    def __init__(self, cell_db, monkeypatch) -> None:
        self.cell_db = cell_db
        self.cluster = FakeClusterGateway()
        self.s3 = FakeS3()
        self.jobs: dict[tuple[str, str], ExportJob] = {}
        self.cluster.observe_export_job = lambda namespace, export_id, *, step: self.jobs.get(
            (export_id, step), ExportJob())
        monkeypatch.setattr(reconcile, "_staging_s3_client", self.s3.client)

    async def __aenter__(self) -> Harness:
        self.app = await asyncpg.connect(self.cell_db.dsn(role="substrate_app"))
        self.cellctl = await asyncpg.connect(self.cell_db.dsn(role="exomem_cellctl"))
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.app.close()
        await self.cellctl.close()

    async def run(self, now: datetime = NOW) -> None:
        await reconcile.reconcile_once(self.cellctl, self.cluster, FakeHetznerVolumeProvider(),
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

    def created(self, export_id: str, step: str) -> dict:
        """The last manifest cellctl created under the step's name."""
        name = staging_claim_name(export_id) if step == "staging" else job_name(export_id, step)
        return next(job for job in reversed(self.cluster.jobs) if job["metadata"]["name"] == name)

    async def verify(self, export_id: str) -> None:
        item = await self.export(export_id)
        assert item["observed_state"] == "packing" and item["archive_version_id"]
        assert item["ready_at"] is None and item["download_url"] is None
        await self.run()
        assert self.created(export_id, VERIFY)
        self.jobs[(export_id, VERIFY)] = ExportJob(exists=True, succeeded=True,
            report={"verified": True, "bytes": item["archive_bytes"], "sha256": item["archive_sha256"],
                    "version": item["archive_version_id"]})
        await self.run()

    async def pause(self, cell_id: str, export_id: str, *, planned_bytes: int = 8192) -> dict:
        """Hold the cell, release its volume, act as the pause Job, then let the
        runtime take its volume back."""
        await self.run()
        self.cluster.observations[cell_id] = _observed_from_applied(self.cluster, cell_id)
        await self.run()
        job = self.created(export_id, PAUSE)
        self.jobs[(export_id, PAUSE)] = ExportJob(exists=True, succeeded=True, pods_present=True, finished_at=NOW,
                                                  report={**PAUSE_REPORT, "planned_bytes": planned_bytes})
        await self.run()  # records the plan and stops the pause Job
        self.jobs[(export_id, PAUSE)] = ExportJob()
        await self.run()  # the pause Job is gone, so the hold ends
        self.cluster.observations[cell_id] = _observed_from_applied(self.cluster, cell_id)
        return job

    async def pack(self, cell_id: str, export_id: str, archive: bytes, *, planned_bytes: int = 8192) -> dict:
        """Pause, then act as the upload and the pack: send the archive to the
        upload's part URLs, in the pack's part size, and report it."""
        await self.pause(cell_id, export_id, planned_bytes=planned_bytes)
        await self.run()  # starts the upload
        upload = self.created(export_id, UPLOAD)
        self.jobs[(export_id, UPLOAD)] = ExportJob(exists=True, active=True, pods_present=True)
        await self.run()  # starts the pack beside it
        pack = self.created(export_id, PACK)
        upload_command = upload["spec"]["template"]["spec"]["containers"][0]["command"]
        pack_command = pack["spec"]["template"]["spec"]["containers"][0]["command"]
        urls = [upload_command[index + 1] for index, arg in enumerate(upload_command) if arg == "--part-url"]
        part_bytes = int(pack_command[pack_command.index("--part-bytes") + 1])
        chunks = [archive[offset:offset + part_bytes] for offset in range(0, len(archive), part_bytes)]
        etags = [self.s3.put_part(url, chunk) for url, chunk in zip(urls, chunks, strict=False)]
        report = {"bytes": len(archive), "sha256": hashlib.sha256(archive).hexdigest(), "parts": len(etags),
                  "etag_digest": etag_digest(etags)}
        self.jobs[(export_id, PACK)] = ExportJob(exists=True, succeeded=True, pods_present=True, finished_at=NOW)
        self.jobs[(export_id, UPLOAD)] = ExportJob(exists=True, succeeded=True, report=report, pods_present=True,
                                                   finished_at=NOW)
        return {"urls": urls, "upload": upload, "pack": pack}


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
        staging = harness.created(export_id, "staging")
        assert staging["spec"]["storageClassName"] == STORAGE_CLASS
        # A cell never measured stages its whole 10 GiB, plus 10% and the pack's two-part spool,
        # in the 95% of the claim that ext4 leaves to a user that is not root: 13 GiB / 0.95.
        assert staging["spec"]["resources"]["requests"]["storage"] == "14Gi"
        # The hold ended at the pause report, before the upload started.
        assert (await harness.cell(CELL)).hold_kind is None
        statefulset_replicas = [event[2] for event in harness.cluster.events if event[0] == "apply_statefulset"]
        assert statefulset_replicas[-1] == 1
        assert packed["upload"]["spec"]["template"]["spec"]["containers"][0]["image"] == IMAGE
        await harness.run()
        assert ("delete_export_staging", namespace_name(CELL), export_id) in harness.cluster.events
        await harness.verify(export_id)
        ready = await harness.export(export_id)
        assert ready["observed_state"] == "ready" and ready["archive_bytes"] == len(archive)
        assert ready["expires_at"] == NOW + timedelta(hours=24) and ready["archive_version_id"]
        usage = await harness.cell(CELL)
        assert (usage.used_bytes, usage.filesystem_bytes, usage.usage_observed_at) == (3 * MIB, 10 * 1024 * MIB, NOW)

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
        removed = {event for event in harness.cluster.events if event[0] in ("delete_job", "delete_export_staging")}
        assert {("delete_job", namespace_name(CELL), job_name(export_id, step)) for step in (UPLOAD, PACK)} <= removed
        assert ("delete_export_staging", namespace_name(CELL), export_id) in removed


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


async def test_a_large_archives_steps_and_part_urls_last_its_whole_transfer(cell_db, monkeypatch):
    """Defect: a large archive's upload, pack or part URLs expire long before its transfer
    deadline, or a deadline from the pause's own time cuts the transfer short."""
    async with Harness(cell_db, monkeypatch) as harness:
        await harness.serve(CELL)
        export_id = await harness.request(CELL)
        packed = await harness.pack(CELL, export_id, b"PK archive", planned_bytes=110 * GIB)
        # The configured 30 minutes for each started 10 GiB of the plan.
        assert packed["upload"]["spec"]["activeDeadlineSeconds"] == 330 * 60
        assert packed["pack"]["spec"]["activeDeadlineSeconds"] == 330 * 60
        assert {parse_qs(urlsplit(url).query)["expires"][0] for url in packed["urls"]} == {str(330 * 60)}
        assert harness.created(export_id, PAUSE)["spec"]["activeDeadlineSeconds"] < 30 * 60


async def test_a_second_export_on_one_node_waits_until_the_first_one_ends(cell_db, monkeypatch):
    """Defect: two exports pause two cells on one node and compete for its disk, or a
    waiting export stops its cell before it can run."""
    async with Harness(cell_db, monkeypatch) as harness:
        await harness.serve(CELL)
        await harness.serve(OTHER_CELL)
        for cell_id in (CELL, OTHER_CELL):
            await db.write_observed(harness.cellctl, cell_id, {"node": "node-a"})
        exports = {cell_id: await harness.request(cell_id) for cell_id in (CELL, OTHER_CELL)}
        await harness.run()
        states = {cell_id: (await harness.export(export_id))["observed_state"] for cell_id, export_id in exports.items()}
        assert sorted(states.values(), key=str) == [None, "packing"]  # either one may go first
        first = next(cell_id for cell_id, state in states.items() if state == "packing")
        second = next(cell_id for cell_id in exports if cell_id != first)
        waiting = await harness.export(exports[second])
        assert json.loads(waiting["packing_report"]) == {"waiting": "node_export_slot"}
        assert (await harness.cell(second)).hold_kind is None

        await harness.app.execute("UPDATE exomem_cloud_exports SET requested_state='superseded' WHERE export_id=$1",
                                  uuid.UUID(exports[first]))
        await harness.run()
        assert (await harness.export(exports[first]))["observed_state"] == "expired"
        await harness.run()
        assert (await harness.export(exports[second]))["observed_state"] == "packing"
        assert (await harness.cell(second)).hold_kind == "export"


async def test_the_pack_waits_until_the_job_egress_that_keeps_it_off_public_egress_lands(cell_db, monkeypatch):
    """Defect: a cell namespace keeps a job-egress from before the split, which gives every Job
    public egress, and cellctl starts the pack, which holds the media key, under it."""
    async with Harness(cell_db, monkeypatch) as harness:
        await harness.serve(CELL)
        export_id = await harness.request(CELL)
        await harness.pause(CELL, export_id)
        await harness.run()  # starts the upload
        harness.jobs[(export_id, UPLOAD)] = ExportJob(exists=True, active=True, pods_present=True)
        apply_all = harness.cluster.apply_all

        def refuse_job_egress(manifests: list[dict]) -> None:
            if any(m["kind"] == "NetworkPolicy" and m["metadata"]["name"] == JOB_EGRESS_POLICY for m in manifests):
                raise ApiException(status=403, reason="Forbidden")
            apply_all(manifests)

        harness.cluster.apply_all = refuse_job_egress
        await harness.run()
        assert job_name(export_id, PACK) not in {job["metadata"]["name"] for job in harness.cluster.jobs}

        harness.cluster.apply_all = apply_all
        await harness.run(NOW + timedelta(minutes=3))  # past the refusal's backoff
        assert harness.created(export_id, PACK)


async def test_every_export_goes_through_the_staging_key_under_its_own_cells_prefix(cell_db, monkeypatch):
    """Defect: an export is created, completed or linked outside its cell's prefix, or in the backup bucket."""
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
        staging_key_id = _secrets_config().staging_key_id
        for cell_id, export_id in exports.items():
            url = urlsplit((await harness.export(export_id))["download_url"])
            staging_bucket = _cluster_config().cell_data_staging_bucket
            assert url.path == f"/{staging_bucket}/cells/{cell_id}/exports/{export_id}.zip"
            assert parse_qs(url.query)["key_id"] == [staging_key_id]
        assert {used for used, _name in harness.s3.calls} == {staging_key_id}
        assert {name.split("/")[1] for _used, name in harness.s3.calls} == {CELL, OTHER_CELL}


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
    assert granted[("substrate_app", "INSERT")] == {"export_id", "cell_id", "requested_state", "link_requested_at",
                                                    "operation_grant"}
    assert granted[("substrate_app", "UPDATE")] == {"requested_state", "link_requested_at"}
    assert granted[("exomem_cellctl", "UPDATE")] == observation
    assert ("exomem_cellctl", "INSERT") not in granted
    assert not [key for key in granted if key[0] == "exomem_gateway"]
    assert {row["grantee"] for row in usage} == {"exomem_cellctl"}
    assert app_deletes is False  # C6 rows are the content-free record; nothing deletes them


async def test_cellctl_reads_the_grant_substrate_signs_and_cannot_write_one(cell_db):
    """Defect: the grant never reaches the Job, or a compromised cellctl forges the grant it then relays."""
    await _seed_cell(cell_db, CELL, "tenant-grant")
    app = await asyncpg.connect(cell_db.dsn(role="substrate_app"))
    controller = await asyncpg.connect(cell_db.dsn(role="exomem_cellctl"))
    try:
        export_id = await app.fetchval(
            "INSERT INTO exomem_cloud_exports(cell_id, operation_grant) VALUES($1, 'signed-export-grant') RETURNING export_id",
            CELL)
        import_id = await app.fetchval(
            "INSERT INTO exomem_cloud_imports(cell_id, kind, declared_bytes, declared_files) "
            "VALUES($1, 'adopt', 0, 0) RETURNING import_id", CELL)
        await app.execute("UPDATE exomem_cloud_imports SET operation_grant='signed-import-grant' WHERE import_id=$1",
                          import_id)

        assert [item.operation_grant for item in await db.read_exports(controller, CELL)] == ["signed-export-grant"]
        assert (await db.read_import(controller, CELL)).operation_grant == "signed-import-grant"
        for table, key, identifier in (("exports", "export_id", export_id), ("imports", "import_id", import_id)):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await controller.execute(f"UPDATE exomem_cloud_{table} SET operation_grant='forged' WHERE {key}=$1",
                                         identifier)
    finally:
        await app.close()
        await controller.close()


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

    from cellctl.export_layout import verification_secret_name

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
        assert {entry["name"]: entry.get("value") for entry in container["env"]}["EXOMEM_CLOUD_CELL"] == "1"
        reference = next(entry for entry in container["env"] if entry["name"] == "EXOMEM_EXPORT_VERIFY_URL")
        assert reference["valueFrom"]["secretKeyRef"] == {"name": secret_name, "key": "get-url"}
        assert all("persistentVolumeClaim" not in volume for volume in job["spec"]["template"]["spec"]["volumes"])
        unrelated = (namespace_name(CELL), "Secret", verification_secret_name(EXPORT_ID))
        harness.cluster.applied[unrelated] = {"unrelated": True}
        harness.jobs[(export_id, VERIFY)] = ExportJob(exists=True, succeeded=True,
            report={"verified": True, "bytes": item["archive_bytes"], "sha256": item["archive_sha256"],
                    "version": "wrong-version"})
        await harness.run()
        failed = await harness.export(export_id)
        assert failed["observed_state"] == "failed" and failed["download_url"] is None
        assert secret_key not in harness.cluster.applied and unrelated in harness.cluster.applied
        assert harness.s3.versions == {}
