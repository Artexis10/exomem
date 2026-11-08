"""The backup Job's collector condemns only what no retained snapshot reaches (collect-released-media D5).

Each case runs the real cell, the real media edge over HTTP and the test provider with its bucket rule. Snapshots
live in a small stand-in for restic, behind the same two commands that the collector runs: `snapshots --json` and
`dump <id> <path>`.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import httpx
import pytest

from exomem import (
    delete_file,
    media_collect,
    media_edge_store,
    media_jobs,
    media_manifest,
    media_processing,
    media_store,
    originals,
    preserve,
)
from exomem.governance.principal import library_scope
from exomem.kbdir import kb_dirname
from exomem.media_edge_b2 import B2
from exomem.media_edge_protocol import COLLECTOR_AUDIENCE
from exomem.media_edge_store import CREDENTIAL_FILE_ENV, URL_ENV
from exomem.trash_metadata import TRASH_SUBPATH
from tests.fake_b2 import FakeB2
from tests.test_media_edge import CELL_A, Harness
from tests.test_media_store import _offload, selected_store  # noqa: F401
from tests.test_upload_sessions import _listening

MANIFEST_PATH = "/tmp/media-manifest.json"
CELLCTL = Path(__file__).resolve().parents[1] / "infra" / "cellctl" / "src" / "cellctl"
RESTIC = """#!{python}
import json, os, sys
from pathlib import Path
repo = Path(os.environ["FAKE_RESTIC_REPO"])
args = sys.argv[1:]
if args == ["snapshots", "--json"]:
    print(json.dumps([{{"id": entry.name}} for entry in sorted(repo.iterdir())]))
elif args[:1] == ["dump"] and len(args) == 3 and (repo / args[1] / args[2].lstrip("/")).is_file():
    sys.stdout.buffer.write((repo / args[1] / args[2].lstrip("/")).read_bytes())
else:
    sys.exit("path not found in snapshot")
"""


class Cell:
    def __init__(self, vault: Path, harness: Harness, repo: Path):
        self.vault, self.harness, self.repo = vault, harness, repo
        self._snapshots = 0

    def original(self, name: str, data: bytes = b"the same recorded bytes") -> Path:
        path = self.vault / kb_dirname() / "Notes" / "Media" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        media_processing.reconcile_media(self.vault, path)
        preserve.update_sidecar_extraction(self.vault, originals.sidecar_path(path), text="searchable text",
                                           engine="text")
        media_jobs.MediaJobStore(self.vault).retire_ocr(path, "text")
        return path

    def discard(self, path: Path) -> None:
        """The owner deletes the original, then empties the Trash."""
        with library_scope():
            delete_file.delete_file(self.vault, path=path.relative_to(self.vault).as_posix(), confirm=True)
        shutil.rmtree(self.vault / kb_dirname() / TRASH_SUBPATH)

    def snapshot(self, manifest: bytes | None = None) -> str:
        """A backup of the cell as it is now, holding the manifest that the Job writes (or the one given)."""
        self._snapshots += 1
        identifier = hashlib.sha256(f"snapshot {self._snapshots}".encode()).hexdigest()
        if manifest is None:
            try:
                manifest = media_manifest.render(media_manifest.build(self.vault))
            except media_manifest.ManifestUnknown:
                manifest = media_manifest.render(None)
        stored = self.repo / identifier / MANIFEST_PATH.lstrip("/")
        stored.parent.mkdir(parents=True)
        stored.write_bytes(manifest)
        return identifier

    def prune(self, *identifiers: str) -> None:
        for identifier in identifiers:
            shutil.rmtree(self.repo / identifier)

    def collect(self, identifier: str) -> int:
        return media_collect.main(["--snapshot", identifier, "--manifest-path", MANIFEST_PATH])

    def hold(self, *, prune: tuple[str, ...] = ()) -> str:
        identifier = self.snapshot()
        self.prune(*prune)
        assert self.collect(identifier) == 0
        return identifier

    def head(self, reference: originals.StoredObject):
        return media_store.store_for(self.vault).head(reference.object, reference.version)

    def hides(self) -> int:
        return self.harness.fake.calls.count("b2_hide_file")


@pytest.fixture
def cell(vault: Path, selected_store, tmp_path: Path, monkeypatch):  # noqa: F811
    values, config = selected_store
    harness = Harness(tmp_path / "edge", max_object_bytes=10_000_000)
    rewritten = {key: value for key, value in values.items() if key != "root"} | {"adapter": "media-edge"}
    config.write_text(json.dumps({"vaults": {str(vault): rewritten}}))
    runtime, collector = tmp_path / "runtime-credential", tmp_path / "collector-credential"
    runtime.write_text(harness.credential(CELL_A))
    collector.write_text(harness.credential(CELL_A, aud=COLLECTOR_AUDIENCE))
    monkeypatch.setenv(CREDENTIAL_FILE_ENV, str(runtime))
    monkeypatch.setenv(media_collect.COLLECTOR_CREDENTIAL_FILE_ENV, str(collector))
    tools, repo = tmp_path / "bin", tmp_path / "repo"
    tools.mkdir()
    repo.mkdir()
    (tools / "restic").write_text(RESTIC.format(python=sys.executable))
    (tools / "restic").chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_RESTIC_REPO", str(repo))
    with _listening(harness.edge) as (host, port):
        monkeypatch.setenv(URL_ENV, f"http://{host}:{port}")
        yield Cell(vault, harness, repo)


def test_the_manifest_lists_live_and_trash_references_protected_and_released_names(cell: Cell, monkeypatch):
    """A manifest that missed Trash references or protection records would let a hold condemn a restorable name."""
    live = _offload(cell.vault, cell.original("live.txt", b"live bytes"))
    trashed_path = cell.original("trashed.txt", b"trashed bytes")
    trashed = _offload(cell.vault, trashed_path)
    with library_scope():
        delete_file.delete_file(cell.vault, path=trashed_path.relative_to(cell.vault).as_posix(), confirm=True)
    pending_path = cell.original("pending.txt", b"pending bytes")

    def lost(self, name, stream, *, size, sha256):
        raise media_store.MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the upload did not finish")

    monkeypatch.setattr(media_edge_store.EdgeStore, "upload", lost)
    with pytest.raises(media_store.MediaStoreError):
        media_store.offload(cell.vault, pending_path)

    manifest = media_manifest.parse(media_manifest.render(media_manifest.build(cell.vault)))
    assert manifest is not None
    assert manifest.references == {(live.object, live.version), (trashed.object, trashed.version)}
    assert manifest.released == {trashed.object}
    assert len(manifest.protected) == 1 and not manifest.protected & {live.object, trashed.object}


def test_a_companion_that_does_not_parse_makes_the_manifest_unknown(cell: Cell):
    """A census that skipped an unreadable companion would leave its name out of reach and open to a hide."""
    path = cell.original("damaged.txt")
    _offload(cell.vault, path)
    companion = originals.sidecar_path(path)
    companion.write_text(companion.read_text().replace("ciphertext_sha256", "ciphertext_digest"))

    with pytest.raises(media_manifest.ManifestUnknown):
        media_manifest.build(cell.vault)
    assert media_manifest.parse(media_manifest.render(None)) is None


def test_a_name_that_a_retained_snapshot_references_is_hidden_only_after_that_snapshot_is_pruned(cell: Cell):
    """(a) A collector that read only the newest manifest would hide a version that an older backup restores."""
    path = cell.original("recording.txt")
    reference = _offload(cell.vault, path)
    first = cell.hold()
    cell.discard(path)
    second = cell.hold()
    assert cell.hides() == 0 and not cell.head(reference).condemned

    cell.hold(prune=(first,))
    assert cell.head(reference).condemned
    cell.harness.fake.lifecycle_tick(cell.harness.fake.hiding_days)
    assert cell.head(reference) is None
    assert second  # the snapshot that released the name stays retained throughout


def test_a_deletion_after_the_clone_condemns_nothing_in_that_hold(cell: Cell):
    """(b) A hold that read its own reach from the live cell, not from the snapshot that it just wrote, would hide a
    name that this snapshot still restores."""
    path = cell.original("recording.txt")
    reference = _offload(cell.vault, path)
    captured = cell.snapshot()
    cell.discard(path)

    assert cell.collect(captured) == 0
    assert cell.hides() == 0 and not cell.head(reference).condemned


def test_a_clone_taken_mid_reuse_keeps_the_name_through_the_reusers_synced_protection_record(cell: Cell, monkeypatch):
    """(c) A clone taken while a reuse is in flight holds the name only in the reuser's protection record. That record
    can sit in the ledger's write-ahead log after an older release reached the main file. A manifest that ignored
    protection records, or read the ledger without its log, would hide the version that the reuse then publishes."""
    source, holder = cell.original("source.txt"), cell.original("holder.txt")
    reference = _offload(cell.vault, source)
    assert _offload(cell.vault, holder) == reference
    # The running service holds the ledger open, so committed rows stay in its log until a checkpoint.
    with closing(sqlite3.connect(media_jobs.MediaJobStore(cell.vault).path)) as service:
        cell.discard(source)
        service.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        publish = media_store._publish_reference

        def stopped(vault, snapshot, value):
            if value is not None:
                raise RuntimeError("the clone was taken before the reuse published")
            return publish(vault, snapshot, value)

        with monkeypatch.context() as in_flight:
            in_flight.setattr(media_store, "_publish_reference", stopped)
            with pytest.raises(RuntimeError, match="before the reuse published"):
                media_store.offload(cell.vault, cell.original("reuser.txt"))
        cell.discard(holder)

        cell.hold()
    assert cell.hides() == 0 and not cell.head(reference).condemned


def test_a_released_reuse_source_stays_while_the_reuser_references_it(cell: Cell):
    """(d) A collector that subtracted only the older manifests' names would hide a version that this hold's own
    snapshot still references through the reuser."""
    source, reuser = cell.original("source.txt"), cell.original("reuser.txt")
    reference = _offload(cell.vault, source)
    assert _offload(cell.vault, reuser) == reference
    cell.discard(source)

    cell.hold()
    assert cell.hides() == 0 and not cell.head(reference).condemned


def test_after_holds_and_lifecycle_ticks_every_retained_reference_heads_current(cell: Cell):
    """(e) Any hide that reached a name that a retained backup references would, after the grace, leave that
    backup's restore pointing at a deleted version."""
    fake = cell.harness.fake
    paths = {name: cell.original(f"{name}.txt", name.encode() * 8) for name in ("first", "second", "kept")}
    references = {name: _offload(cell.vault, path) for name, path in paths.items()}

    def tick_and_restore_every_retained_snapshot() -> None:
        fake.lifecycle_tick(fake.hiding_days)
        for retained in cell.repo.iterdir():
            manifest = media_manifest.parse((retained / MANIFEST_PATH.lstrip("/")).read_bytes())
            assert manifest is not None and manifest.references
            for name, version in manifest.references:
                head = media_store.store_for(cell.vault).head(name, version)
                assert head is not None and not head.condemned

    oldest = cell.hold()
    cell.discard(paths["first"])
    older = cell.hold()
    tick_and_restore_every_retained_snapshot()
    cell.discard(paths["second"])
    cell.hold(prune=(oldest,))
    tick_and_restore_every_retained_snapshot()
    cell.hold(prune=(older,))
    tick_and_restore_every_retained_snapshot()

    assert cell.head(references["first"]) is None and cell.head(references["second"]) is None
    assert not cell.head(references["kept"]).condemned


@pytest.mark.parametrize("manifest", [None, media_manifest.render(None), b'{"v":1,"census":"complete"'],
                         ids=["missing", "unknown", "truncated"])
def test_a_retained_snapshot_without_a_complete_manifest_stops_every_hide(cell: Cell, manifest):
    """(f) A collector that skipped a snapshot it cannot read would hide names that that snapshot restores."""
    path = cell.original("recording.txt")
    reference = _offload(cell.vault, path)
    unreadable = cell.snapshot(b"placeholder")
    target = cell.repo / unreadable / MANIFEST_PATH.lstrip("/")
    if manifest is None:
        target.unlink()
    else:
        target.write_bytes(manifest)
    cell.discard(path)

    stopped = cell.collect(cell.snapshot())
    assert cell.hides() == 0 and not cell.head(reference).condemned
    assert stopped == 1


def test_a_name_that_the_edge_holds_busy_is_skipped_and_the_pass_goes_on(cell: Cell, monkeypatch):
    """A pass that stopped at one busy name would, hold after hold, never reach the names after it in name order."""
    paths = [cell.original(f"{name}.txt", name.encode() * 8) for name in ("first", "second")]
    references = sorted((_offload(cell.vault, path) for path in paths), key=lambda reference: reference.object)
    for path in paths:
        cell.discard(path)
    post = httpx.post

    def edge(url: str, **kwargs):
        if references[0].object in url:  # the edge's serial-write hold on this name
            return httpx.Response(409, request=httpx.Request("POST", url))
        return post(url, **kwargs)

    monkeypatch.setattr(httpx, "post", edge)
    cell.hold()
    assert cell.hides() == 1
    assert [cell.head(reference).condemned for reference in references] == [False, True]


def test_the_bucket_rule_deletes_only_hidden_or_superseded_versions():
    """The collection cases trust this rule: deleting a current version would make them fail for the wrong
    reason, and deleting nothing would make them pass for none."""
    fake = FakeB2()

    async def scenario() -> dict[str, str]:
        async with httpx.AsyncClient(transport=fake.transport()) as client:
            b2 = B2(client, key_id="key", application_key="secret", bucket=fake.bucket)

            async def upload(name: str, data: bytes) -> str:
                return await b2.upload_file(await b2.upload_target(), name, data, hashlib.sha1(data).hexdigest(), {})

            versions = {"superseded": await upload("one", b"first"), "newest": await upload("one", b"second"),
                        "hidden": await upload("two", b"hidden"), "current": await upload("three", b"current")}
            await b2.hide_file("two")
            return versions

    versions = asyncio.run(scenario())
    assert fake.lifecycle_tick(fake.hiding_days - 1) == []
    assert sorted(fake.lifecycle_tick(1)) == sorted([versions["superseded"], versions["hidden"]])
    assert fake.lifecycle_tick(10 * fake.hiding_days) == []
    assert {versions["newest"], versions["current"]} <= set(fake.files)


def _cellctl_constant(module: str, name: str) -> object:
    # cellctl cannot import the cell and the cell cannot import cellctl, so this reads cellctl's constant from its source.
    values = [node.value for node in ast.parse((CELLCTL / module).read_text(encoding="utf-8")).body
              if isinstance(node, ast.Assign) and [t.id for t in node.targets if isinstance(t, ast.Name)] == [name]]
    assert len(values) == 1
    return ast.literal_eval(values[0])


def test_cellctl_signs_and_places_the_collector_credential_where_the_cell_reads_it():
    """A difference refuses every condemn or finds no credential, and only the Job's log would say so."""
    assert _cellctl_constant("media_edge.py", "COLLECTOR_AUDIENCE") == COLLECTOR_AUDIENCE
    assert _cellctl_constant("manifests.py", "MEDIA_COLLECTOR_CREDENTIAL_ENV") == media_collect.COLLECTOR_CREDENTIAL_FILE_ENV
    assert _cellctl_constant("manifests.py", "MEDIA_EDGE_URL_ENV") == URL_ENV


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="the read-only mount needs bubblewrap, which Linux CI installs")
def test_the_backup_jobs_own_commands_reach_the_cells_manifest_and_collector(cell: Cell, tmp_path: Path):
    """The Job runs both commands in shell that keeps the backup going whatever they do, so a command or a flag that
    cellctl and the cell spell differently would leave every hold collecting nothing, without a word. So would a
    census that writes anywhere on the clone: the Job mounts the vault and the state root read-only. A result line
    that cellctl reads differently would leave every hold's outcome out of cellctl's log."""
    loader = importlib.util.spec_from_file_location("cellctl_backup_source", CELLCTL / "backup_source.py")
    job = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(job)
    path = cell.original("recording.txt")
    reference = _offload(cell.vault, path)
    cell.discard(path)
    tools, written = tmp_path / "image", tmp_path / "written-manifest.json"
    tools.mkdir()
    (tools / "exomem").write_text(f'#!/bin/sh\nexec "{sys.executable}" -m exomem "$@"\n', encoding="utf-8")
    (tools / "exomem").chmod(0o755)
    shell = {**os.environ, "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}", "TMPDIR": str(tmp_path)}

    manifest = job.media_manifest_command().replace("/data/vault", str(cell.vault))
    state = os.environ["EXOMEM_STATE_ROOT"]
    read_only = ["bwrap", "--dev-bind", "/", "/", "--ro-bind", str(cell.vault), str(cell.vault), "--ro-bind", state, state]
    subprocess.run([*read_only, "sh", "-c", manifest.replace(job.MEDIA_MANIFEST_PATH, str(written))],
                   check=True, env=shell)
    snapshot = cell.snapshot(written.read_bytes())
    report = tmp_path / "termination-log"
    collect = f"{job.MEDIA_COLLECT_START} && {job.media_collect_command()}".replace("/dev/termination-log", str(report))
    subprocess.run(["sh", "-c", collect], check=True, env={**shell, "SNAPSHOT_ID": snapshot})

    assert job.MEDIA_MANIFEST_PATH == MANIFEST_PATH
    assert cell.hides() == 1 and cell.head(reference).condemned
    assert job.parse_media_collection(report.read_text(encoding="utf-8").removeprefix("\n")) == ("complete", "-", 1, 1, 0)
