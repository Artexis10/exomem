"""The Cloud export in three steps: pause, pack and upload (add-cloud-media-store D8, 2026-11-08).

Each case runs the real portability writer and verifier and the real age decryption. The workflow case
also runs the real media edge over HTTP and the presigned parts over TLS.
"""
from __future__ import annotations

import contextlib
import dataclasses
import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path

import pytest

from exomem import cloud_export, cloud_export_steps, media_store, originals, vault_portability
from exomem.governance.principal import OWNER_AUDIENCE, RequestPrincipal, request_scope
from exomem.kbdir import kb_dirname
from exomem.trash_metadata import METADATA_SUFFIX, TRASH_SUBPATH
from tests.test_cloud_export import CELL, PartStore, storage  # noqa: F401
from tests.test_media_collection import Cell, cell  # noqa: F401
from tests.test_media_store import _offload, _stored, selected_store  # noqa: F401

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the cell export Job runs only on Linux cells")

PLENTY = 1024**3


def _owner():
    return request_scope(RequestPrincipal(audience_id=OWNER_AUDIENCE, surface="cloud-export",
                                          issuer_family=f"cloud-cell:{CELL}"))


def _evicted(vault: Path, category: str, size: int) -> tuple[Path, originals.StoredObject, bytes]:
    path = _stored(vault, category, size)
    data = path.read_bytes()
    reference = originals.original_state(vault, path).reference
    assert media_store.evict(vault, path)
    return path, reference, data


def _trash_companion(vault: Path, path: Path) -> Path:
    """Move an evicted original's companion into a Trash bundle, as a folder deletion leaves it: no payload beside it."""
    bundle = vault / kb_dirname() / TRASH_SUBPATH / "2026-11-08" / "bundle"
    bundle.mkdir(parents=True)
    originals.sidecar_path(path).rename(bundle / originals.sidecar_path(path).name)
    (bundle.parent / ("bundle" + METADATA_SUFFIX)).write_text(
        json.dumps({"original_path": path.parent.relative_to(vault).as_posix()}))
    return bundle / path.name


def _start_phase(log: Path, *args: str) -> subprocess.Popen:
    # Each step is its own process, as each is its own Job step on a cell; one shared process would let a step
    # inherit another's state.
    return subprocess.Popen([sys.executable, "-m", "exomem", "cell-export", *args, "--termination-log", str(log)],
                            env=dict(os.environ), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _finish_phase(process: subprocess.Popen, log: Path) -> dict:
    _, stderr = process.communicate(timeout=300)
    report = json.loads(log.read_text(encoding="ascii"))
    assert process.returncode == (1 if "error" in report else 0), stderr.decode(errors="replace")[-2000:]
    return report


def _phase(log: Path, *args: str) -> dict:
    return _finish_phase(_start_phase(log, *args), log)


def _verify_remote(url: str, *, archive_bytes: int, archive_sha256: str, max_bytes: int = PLENTY) -> dict:
    # A refusal's detail names the cause; a bare `match=` on the code would drop it.
    try:
        return cloud_export.verify_remote_export(url, cell_id=CELL, archive_bytes=archive_bytes,
                                                 archive_sha256=archive_sha256, max_bytes=max_bytes)
    except cloud_export.ExportRefused as error:
        pytest.fail(f"{error.code} {error.detail}")


def test_an_export_carries_every_member_and_each_evicted_original_byte_identical(
        cell: Cell, storage: dict, tmp_path: Path, tmp_path_factory, monkeypatch):  # noqa: F811
    """Defect: the archive omits an evicted original, live or in Trash, or a note whose companion header cannot be
    read, or carries what the runtime wrote after the pause."""
    vault = cell.vault
    cached = cell.original("cached.txt", b"cached bytes")
    _offload(vault, cached)
    evicted = cell.original("evicted.txt", b"evicted bytes")
    _offload(vault, evicted)
    assert media_store.evict(vault, evicted)
    discarded = cell.original("discarded.txt", b"discarded bytes")
    _offload(vault, discarded)
    assert media_store.evict(vault, discarded)
    trashed = _trash_companion(vault, discarded)
    pending = cell.original("pending.txt", b"pending bytes")
    damaged = cell.original("damaged.txt", b"bytes beside a damaged companion")
    companion = originals.sidecar_path(damaged)
    companion.write_text(companion.read_text().replace("evidence_file:", "evidence_file: [", 1))
    note = vault / kb_dirname() / "Notes" / "Before the pause.md"
    note.write_text("written before the pause")

    staging = tmp_path_factory.mktemp("staging")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", CELL)
    monkeypatch.setenv("SSL_CERT_FILE", str(storage["ca"]))
    common = ["--export-id", str(uuid.uuid4()), "--max-bytes", str(PLENTY), "--vault", str(vault),
              "--staging", str(staging)]
    paused = _phase(tmp_path / "pause.json", "--phase", "pause", "--media-mode", "served", *common)
    assert "error" not in paused, paused
    assert (paused["evicted"], paused["evicted_bytes"]) == (2, 28), paused

    # The hold ends with the pause: the runtime serves, and what it writes now is not the export's.
    note.write_text("written while the pack runs")
    cached.unlink()
    shutil.rmtree(vault / kb_dirname() / TRASH_SUBPATH)

    urls = [f"{storage['base']}/cells/{CELL}/exports/archive.zip?partNumber={number}" for number in range(1, 201)]
    pack = _start_phase(tmp_path / "pack.json", "--phase", "pack", "--part-bytes", str(64 * 1024),
                        "--max-parts", "200", *common)
    uploaded = _phase(tmp_path / "upload.json", "--phase", "upload", *common,
                      *(argument for url in urls for argument in ("--part-url", url)))
    packed = _finish_phase(pack, tmp_path / "pack.json")
    assert "error" not in packed and "error" not in uploaded, (packed, uploaded)
    assert packed["evicted"] == 2

    parts = [storage["parts"][f"/cells/{CELL}/exports/archive.zip?partNumber={number}"]
             for number in range(1, uploaded["parts"] + 1)]
    assert uploaded["parts"] > 2 and len(storage["parts"]) == uploaded["parts"]
    assert uploaded["etag_digest"] == cloud_export.etag_digest(
        ['"' + hashlib.md5(part).hexdigest() + '"' for part in parts])  # noqa: S324 - S3's part ETag
    archive = tmp_path / "archive.zip"
    archive.write_bytes(b"".join(parts))
    verified = vault_portability.verify_export_archive(archive, expected_cell_id=CELL)
    assert (verified.archive_sha256, verified.archive_size) == (uploaded["sha256"], uploaded["bytes"])
    with zipfile.ZipFile(archive) as opened:
        files = {record["path"]: record for record in
                 json.loads(opened.read(vault_portability.MANIFEST_NAME))["files"]}
        expected = {evicted: b"evicted bytes", trashed: b"discarded bytes", cached: b"cached bytes",
                    pending: b"pending bytes", damaged: b"bytes beside a damaged companion",
                    note: b"written before the pause"}
        for path, data in expected.items():
            relative = path.relative_to(vault).as_posix()
            assert opened.read(relative) == data, relative
            assert files[relative]["sha256"] == hashlib.sha256(data).hexdigest()
        assert opened.read(companion.relative_to(vault).as_posix()) == companion.read_bytes()
    plan = json.loads((staging / cloud_export_steps.EXPORT_DIR / cloud_export_steps.PLAN_NAME).read_text())
    assert sorted(entry["path"] for entry in plan["evicted"]) == sorted(
        path.relative_to(vault).as_posix() for path in (evicted, trashed))
    reports = json.dumps([paused, packed, uploaded])
    assert str(vault) not in reports and str(staging) not in reports and storage["base"] not in reports
    assert not list((staging / cloud_export_steps.EXPORT_DIR / cloud_export_steps.SPOOL_NAME).glob("part-*"))


@pytest.mark.parametrize("damage", ["corrupt", "absent", "staged"])
def test_a_member_that_the_pack_cannot_reproduce_fails_the_export(vault: Path, selected_store, tmp_path_factory,  # noqa: F811
                                                                  damage):
    """Defect: a corrupt or missing stored version, or a staged copy cut short, is archived as an absence or as
    other bytes, or a missing version waits out every retry first."""
    _path, reference, _data = _evicted(vault, "evicted", 300)
    staging = tmp_path_factory.mktemp("staging")
    export_id = str(uuid.uuid4())
    with _owner():
        cloud_export_steps.pause(vault, staging, export_id=export_id, cell_id=CELL, media_mode="served",
                                 max_bytes=PLENTY)
        stored = Path(str(selected_store[0]["root"])) / reference.object / reference.version
        stored.chmod(0o600)
        if damage == "corrupt":
            stored.write_bytes(bytes(byte ^ 1 for byte in stored.read_bytes()))
        elif damage == "absent":
            stored.unlink()
        else:
            # A node that crashed before the pause's sync can leave a staged copy without its tail.
            plan = json.loads((staging / cloud_export_steps.EXPORT_DIR / cloud_export_steps.PLAN_NAME).read_text())
            path, size, _classification = next(member for member in plan["members"] if member[1])
            os.truncate(staging / cloud_export_steps.EXPORT_DIR / cloud_export_steps.MEMBERS_NAME / path, size - 1)
        waits: list[float] = []
        with pytest.raises((media_store.MediaStoreError, vault_portability.PortabilityError)) as failed:
            cloud_export_steps.pack(vault, staging, export_id=export_id, cell_id=CELL, part_bytes=64 * 1024**2,
                                    max_parts=10, max_bytes=PLENTY, sleep=waits.append)
    assert failed.value.code == {"corrupt": "MEDIA_ORIGINAL_UNVERIFIED", "absent": "STORED_MEMBER_ABSENT",
                                 "staged": "STAGED_MEMBER_CHANGED"}[damage]
    assert waits == []
    spool = staging / cloud_export_steps.EXPORT_DIR / cloud_export_steps.SPOOL_NAME
    assert not (spool / cloud_export_steps.END_NAME).exists()
    with pytest.raises(cloud_export.ExportRefused, match="^EXPORT_FAILED$"):
        cloud_export_steps.upload(staging, ["https://storage.invalid/part-1"], sleep=pytest.fail)


def test_an_evicted_original_behind_a_companion_that_cannot_be_read_is_archived(vault: Path, selected_store,  # noqa: F811
                                                                               tmp_path_factory):
    """Defect: a damaged companion hides an evicted original from the walk, and the export drops it without a trace."""
    path, _reference, data = _evicted(vault, "evicted", 300)
    companion = originals.sidecar_path(path)
    damaged = companion.read_text().replace("evidence_file:", "evidence_file: [", 1)
    assert damaged != companion.read_text()
    companion.write_text(damaged)
    staging = tmp_path_factory.mktemp("staging")
    export_id = str(uuid.uuid4())
    with _owner():
        paused = cloud_export_steps.pause(vault, staging, export_id=export_id, cell_id=CELL, media_mode="served",
                                          max_bytes=PLENTY)
        cloud_export_steps.pack(vault, staging, export_id=export_id, cell_id=CELL, part_bytes=64 * 1024**2,
                                max_parts=10, max_bytes=PLENTY)
    assert paused.evicted == 1
    archive = staging / cloud_export_steps.EXPORT_DIR / cloud_export_steps.SPOOL_NAME / "part-00001"
    with zipfile.ZipFile(archive) as written:
        assert written.read(path.relative_to(vault).as_posix()) == data
        assert written.read(companion.relative_to(vault).as_posix()) == damaged.encode()


@pytest.mark.parametrize("tamper", ["absolute", "link"])
def test_a_planted_plan_never_puts_a_file_outside_staging_into_the_archive(vault: Path, tmp_path_factory, tamper):
    """Defect: a pod that can write staging plants a plan member that names the pack's media key, by an absolute
    path or through a link, and the pack archives the key for the upload to send."""
    secret = b"stand-in for the media identity"
    # The layout that Kubernetes gives a mounted Secret: the files in a dated directory behind a link.
    keys = tmp_path_factory.mktemp("media")
    (keys / "..2026_11_08").mkdir()
    (keys / "..2026_11_08" / "age-identity").write_bytes(secret)
    os.symlink("..2026_11_08", keys / "..data")
    staging = tmp_path_factory.mktemp("staging")
    export_dir = staging / cloud_export_steps.EXPORT_DIR
    export_id = str(uuid.uuid4())
    with _owner():
        cloud_export_steps.pause(vault, staging, export_id=export_id, cell_id=CELL, media_mode=None,
                                 max_bytes=PLENTY)
        plan = json.loads((export_dir / cloud_export_steps.PLAN_NAME).read_text())
        if tamper == "absolute":
            plan["members"].append([str(keys / "..data" / "age-identity"), len(secret), "user_content"])
        else:
            os.symlink(keys / "..data", export_dir / cloud_export_steps.MEMBERS_NAME / "Loot")
            plan["members"].append(["Loot/age-identity", len(secret), "user_content"])
        (export_dir / cloud_export_steps.PLAN_NAME).write_text(json.dumps(plan))
        with pytest.raises(vault_portability.PortabilityError) as failed:
            cloud_export_steps.pack(vault, staging, export_id=export_id, cell_id=CELL, part_bytes=64 * 1024**2,
                                    max_parts=10, max_bytes=PLENTY)
    assert failed.value.code == "UNSAFE_STAGED_MEMBER"
    parts = (export_dir / cloud_export_steps.SPOOL_NAME).glob("part-*")
    assert not any(secret in part.read_bytes() for part in parts)


def test_a_read_that_drops_mid_object_is_read_again_from_its_start(vault: Path, selected_store,  # noqa: F811
                                                                   tmp_path_factory, monkeypatch):
    """Defect: a media edge that restarts during the pack fails the export, or leaves a member with a doubled prefix."""
    path, reference, data = _evicted(vault, "evicted", 300_000)
    staging = tmp_path_factory.mktemp("staging")
    export_id = str(uuid.uuid4())
    opened = media_store.FilesystemStore.open
    drops = [200_000]

    class Dropping:
        def __init__(self, stream):
            self.stream, self.left = stream, drops.pop()

        def read(self, size=-1):
            if self.left <= 0:
                raise ConnectionResetError(errno.ECONNRESET, "the edge restarted")
            block = self.stream.read(min(size, self.left) if size >= 0 else self.left)
            self.left -= len(block)
            return block

    def dropping_open(store, name, version):
        context = opened(store, name, version)
        stream = context.__enter__()
        try:
            yield Dropping(stream) if drops else stream
        finally:
            context.__exit__(None, None, None)

    monkeypatch.setattr(media_store.FilesystemStore, "open", contextlib.contextmanager(dropping_open))
    with _owner():
        cloud_export_steps.pause(vault, staging, export_id=export_id, cell_id=CELL, media_mode="served",
                                 max_bytes=PLENTY)
        waits: list[float] = []
        end = cloud_export_steps.pack(vault, staging, export_id=export_id, cell_id=CELL, part_bytes=64 * 1024**2,
                                      max_parts=10, max_bytes=PLENTY, sleep=waits.append)
    assert waits == [cloud_export_steps.FETCH_RETRY_SECONDS[0]] and end["parts"] == 1
    archive = staging / cloud_export_steps.EXPORT_DIR / cloud_export_steps.SPOOL_NAME / "part-00001"
    with zipfile.ZipFile(archive) as written:
        assert written.read(path.relative_to(vault).as_posix()) == data
    assert reference.ciphertext_size > 200_000


@pytest.mark.parametrize("binding", ["retained", "unbound"])
def test_a_pause_that_cannot_reach_an_evicted_original_refuses_the_export(
        vault: Path, selected_store, tmp_path_factory, monkeypatch, binding):  # noqa: F811
    """Defect: a cell without an edge, or whose store binding is not mounted, exports its evicted originals as absences."""
    _evicted(vault, "evicted", 300)
    if binding == "unbound":
        monkeypatch.delenv(originals.CONFIG_ENV)
    staging = tmp_path_factory.mktemp("staging")
    with _owner(), pytest.raises((cloud_export.ExportRefused, vault_portability.PortabilityError)) as refused:
        cloud_export_steps.pause(vault, staging, export_id=str(uuid.uuid4()), cell_id=CELL,
                                 media_mode="retained" if binding == "retained" else "served", max_bytes=PLENTY)
    assert refused.value.code == {"retained": "EXPORT_MEDIA_UNREACHABLE", "unbound": "MEDIA_STORE_UNBOUND"}[binding]
    assert not (staging / cloud_export_steps.EXPORT_DIR / cloud_export_steps.PLAN_NAME).exists()


def test_a_plan_above_the_export_bound_refuses_as_too_large(vault: Path, tmp_path_factory):
    """Defect: an export above the parts that cellctl can sign runs its pack for hours, then fails as a fault."""
    staging = tmp_path_factory.mktemp("staging")
    with _owner(), pytest.raises(cloud_export.ExportRefused, match="^EXPORT_TOO_LARGE$"):
        cloud_export_steps.pause(vault, staging, export_id=str(uuid.uuid4()), cell_id=CELL, media_mode=None,
                                 max_bytes=1000)
    assert not (staging / cloud_export_steps.EXPORT_DIR / cloud_export_steps.PLAN_NAME).exists()


def test_an_evicted_original_above_the_librarys_per_file_default_passes_the_planned_bound(
        vault: Path, selected_store, storage, tmp_path_factory, monkeypatch):  # noqa: F811
    """Defect: the pause, the pack or the Cloud verifier keeps the library's 2 GiB per-file default, so an evicted
    recording above it fails every export that the planned bound covers. The default is scaled down to 1 KiB, so
    a 4 KiB original stands for one above 2 GiB; the archive layout of a member above 2 GiB is not covered."""

    @dataclasses.dataclass(frozen=True)
    class ScaledDefaults(vault_portability.PortabilityLimits):
        max_file_bytes: int = 1024

    monkeypatch.setattr(vault_portability, "PortabilityLimits", ScaledDefaults)
    monkeypatch.setenv("SSL_CERT_FILE", str(storage["ca"]))
    _path, _reference, data = _evicted(vault, "recording", 4096)
    assert len(data) > ScaledDefaults().max_file_bytes
    staging = tmp_path_factory.mktemp("staging")
    export_id = str(uuid.uuid4())

    with _owner():
        cloud_export_steps.pause(vault, staging, export_id=export_id, cell_id=CELL, media_mode="served",
                                 max_bytes=PLENTY)
        packed = cloud_export_steps.pack(vault, staging, export_id=export_id, cell_id=CELL, part_bytes=64 * 1024**2,
                                         max_parts=10, max_bytes=PLENTY)
    PartStore.objects["/archive?versionId=exact"] = (
        staging / cloud_export_steps.EXPORT_DIR / cloud_export_steps.SPOOL_NAME / "part-00001").read_bytes()
    verified = _verify_remote(f"{storage['base']}/archive?versionId=exact",
                              archive_bytes=packed["bytes"], archive_sha256=packed["sha256"])

    assert (packed["parts"], packed["evicted"], verified["verified"]) == (1, 1, True)


def test_the_pause_copies_between_two_volumes(vault: Path, tmp_path_factory, monkeypatch):
    """Defect: Linux refuses copy_file_range between the cell volume and its staging claim, and every pause fails."""
    def refused(*_args):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "copy_file_range", refused, raising=False)
    staging = tmp_path_factory.mktemp("staging")
    with _owner():
        result = cloud_export_steps.pause(vault, staging, export_id=str(uuid.uuid4()), cell_id=CELL, media_mode=None,
                                          max_bytes=PLENTY)
        members = list(vault_portability.iter_archive_members(vault))
    assert result.members == len(members) > 0
    for member in members:
        staged = staging / cloud_export_steps.EXPORT_DIR / cloud_export_steps.MEMBERS_NAME / member.path
        assert staged.read_bytes() == member.source_path.read_bytes(), member.path


def test_the_spool_never_holds_more_than_two_parts(tmp_path: Path):
    """Defect: a pack that outruns its upload fills the staging claim, which has room for two parts beside the members."""
    spool = tmp_path / "spool"
    sent: list[bytes] = []

    def upload_oldest(_seconds: float) -> None:
        waiting = sorted(path for path in spool.iterdir() if path.name.startswith("part-"))
        assert len(waiting) <= cloud_export_steps.SPOOL_PARTS
        sent.append(waiting[0].read_bytes())
        waiting[0].unlink()

    sink = cloud_export_steps.SpoolSink(spool, 1000, 50, sleep=upload_oldest)
    data = os.urandom(10_500)
    for offset in range(0, len(data), 700):
        sink.write(data[offset:offset + 700])
    end = sink.finish()
    sent += [path.read_bytes() for path in sorted(spool.glob("part-*"))]
    assert b"".join(sent) == data
    assert end == {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "parts": 11}
