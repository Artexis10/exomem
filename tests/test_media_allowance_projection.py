"""The media allowance in store mode counts from the binding projection, never by a walk under the boundary.

OpenSpec count-media-allowance-from-bindings, tasks 3.1, 3.2, 3.6, 3.8, 3.11, 3.13, 3.14 and 3.15. The vault is
the migration suite's generated vault in store mode, with the media store suite's real age store and an
allowance. Counted versions are companion pages that a governed commit writes on the store thread, so each page
is one audit row that the projection follows; no original's bytes are needed for the allowance to count them.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import math
import os
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import test_vault_store_media as store_media_suite
from test_agent_upload_handoff import _prepare
from test_agent_upload_handoff import upload_env as upload_env
from test_collection_store_migration import stop_writer
from test_vault_store_importer import StoreThread
from test_vault_store_media import PICTURE, PICTURE_BYTES, on_store_thread, serve_through
from test_vault_store_migration import build_indexes, migrate_apply, rollback_apply
from test_vault_store_projector import NOTES
from test_vault_store_rederive import settle_to_the_end

from exomem import (
    artifact_lifecycle,
    delete_file,
    file_watcher,
    freshness,
    media_allowance,
    media_bindings,
    media_jobs,
    media_protection,
    media_replicas,
    media_storage_record,
    media_store,
    mutation_lock,
    original_pool,
    originals,
    preserve,
    recover_from_trash,
    server_transfer,
    trash_metadata,
)
from exomem import vault as vault_module
from exomem.collection_store import connection as collection_connection
from exomem.governance.principal import library_scope
from exomem.vault_store import derivation, held, importer, projector, read, rederive, write

coordinator_url = store_media_suite.coordinator_url
vault = store_media_suite.vault
stored = store_media_suite.stored
selected_store = store_media_suite.selected_store


@pytest.fixture
def store_thread(stored, monkeypatch):
    thread = StoreThread(stored)
    serve_through(monkeypatch, thread)
    yield thread
    thread.close()


def allow(selected_store, root, allowance_bytes):
    """Set the vault's media allowance; a new allowance is a new policy, so set it before the first `recover`."""
    values, config = selected_store
    config.write_text(json.dumps({"vaults": {str(root): {**values, "allowance_bytes": allowance_bytes}}}))


def counted_page(binary: str, size: int, version: str, *, store: str = "test-store") -> bytes:
    """A companion that binds ``binary`` and records one stored version of ``size`` bytes in ``store``."""
    digest = hashlib.sha256(binary.encode()).hexdigest()
    return (f"---\nevidence_file: {binary}\nbinary_sha256: {digest}\nbinary_size: {size}\nstored_object:\n"
            f"  schema: 1\n  store: {store}\n  key_version: '1'\n  object: {digest}\n  version: {version}\n"
            f"  ciphertext_size: {size + 100}\n  ciphertext_sha256: {hashlib.sha256(version.encode()).hexdigest()}\n"
            "---\n").encode()


def bind(store_thread, numbers, *, size, prefix="counted"):
    """Commit one counted companion per number in one governed change: one audit row each."""
    puts = tuple(write.PagePut(f"{NOTES}/{prefix}-{number}.bin.md",
                               counted_page(f"{NOTES}/{prefix}-{number}.bin", size, f"v-{prefix}-{number}"))
                 for number in numbers)
    store_thread.run(lambda writer: write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "test", puts=puts)))


def put_page(store_thread, rel, body: bytes):
    store_thread.run(lambda writer: write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "test", puts=(write.PagePut(rel, body),))))


def checkpoint_of(root):
    with closing(media_jobs.MediaJobStore(root, create=False)._connect(readonly=True)) as conn:
        return media_bindings.checkpoint(conn)


def side_row(root, rel):
    with closing(media_jobs.MediaJobStore(root)._connect(readonly=True)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT counted, flagged, reason FROM media_reference_rows WHERE companion_rel = ?",
                            (rel,)).fetchone()


def lag_of(root):
    with read.snapshot(root, join_request=False) as view:
        head = view.connection.execute("SELECT COALESCE(MAX(seq), 0) FROM store_audit").fetchone()[0]
    return head - checkpoint_of(root).seq


@contextmanager
def boundary_held_elsewhere(root):
    """Another thread holds the vault's mutation boundary until the block ends, as a long save would."""
    held, release = threading.Event(), threading.Event()

    def hold():
        with media_store._guard(root, "test_long_save"):
            held.set()
            release.wait(60)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert held.wait(30)
    try:
        yield
    finally:
        release.set()
        holder.join(30)


def run_beside(*works, timeout=30):
    """Run each work on its own thread and return the results; a work that blocks past ``timeout`` fails."""
    results, errors = [None] * len(works), []

    def run(index, work):
        try:
            results[index] = work()
        except BaseException as error:  # noqa: BLE001 - reported below
            errors.append(error)

    threads = [threading.Thread(target=run, args=(index, work), daemon=True) for index, work in enumerate(works)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout)
    assert not any(thread.is_alive() for thread in threads), "a work waited on the held boundary"
    assert not errors, errors
    return results


def stored_original(stored, store_thread):
    """A real original that offload can store: a picture the store's importer has recorded."""
    (stored / PICTURE).write_bytes(PICTURE_BYTES)
    importer.Importer(stored, run=store_thread.run).import_paths([PICTURE])
    return stored / PICTURE


def run_media_child(stored, log_dir):
    result = subprocess.run(
        [sys.executable, "-m", "exomem.media_worker_child", "--vault", str(stored),
         "--parent-pid", str(os.getpid()), "--idle-seconds", "0.1"],
        env={**os.environ, "EXOMEM_LOG_DIR": str(log_dir)},
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_the_audit_head_moves_with_page_writers_and_not_with_derived_rows(
        vault, selected_store, tmp_path):
    """A write that changes canonical pages must leave a trail that the allowance can follow."""
    def source():
        with closing(collection_connection.open_reader(collection_connection.store_path(vault))) as conn:
            pages = conn.execute("SELECT path, body FROM store_pages ORDER BY path").fetchall()
            audit = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM store_audit").fetchone()[0]
        return pages, audit

    def changes(label, run):
        before, cursor = source()
        run()
        after, head = source()
        assert before != after and head > cursor, label

    changes("migration", lambda: migrate_apply(vault))
    thread = StoreThread(vault)
    serving = pytest.MonkeyPatch()
    serve_through(serving, thread)
    note, renamed, outside = (f"{NOTES}/{name}.md" for name in ("Audit", "Renamed", "Outside edit"))
    body = b"# Audit\n\nA changed canonical page.\n"

    def commit(**change):
        thread.run(lambda writer: write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "test", **change)))

    try:
        changes("save", lambda: commit(puts=(write.PagePut(note, body),)))
        changes("move", lambda: commit(moves=(write.PageMove(note, renamed),)))
        changes("delete", lambda: commit(deletes=(write.PageDelete(renamed),)))

        (vault / outside).write_bytes(body)
        changes("sweep import", importer.Importer(vault, run=thread.run).sweep)
        changes("save before projection", lambda: commit(puts=(write.PagePut(note, body),)))
        before_project = source()
        projector.Projector(vault, run=thread.run).run_pass()
        assert (vault / note).read_bytes() == body
        assert source() == before_project

        assert media_bindings.rebuild(vault)
        before_rederive = source()
        revision = read.current_revision(vault)

        def refresh(writer):
            with writer.handle.transaction() as conn:
                rederive.record_overlay_change(conn, revision + 1)
            settle_to_the_end(vault, writer)

        thread.run(refresh)
        assert read.current_revision(vault) > revision
        assert source() == before_rederive
        with closing(media_jobs.MediaJobStore(vault, create=False)._connect(readonly=True)) as conn:
            assert media_bindings.current(vault, conn)
    finally:
        serving.undo()
        thread.close()

    stop_writer()
    changes("rollback", lambda: rollback_apply(vault, tmp_path / "export"))


# Task 3.15: upload admission at the store's head.

def test_an_upload_counts_the_versions_that_the_projection_has_not_followed_yet(stored, selected_store, store_thread):
    """Catches upload admission that reads only the published total: versions bound since the last follow would
    let an upload past the allowance. Two uploads decide beside a held boundary, so admission takes none."""
    allow(selected_store, stored, 100_000)
    assert media_bindings.recover(stored)
    bind(store_thread, range(20), size=1_000)
    before = checkpoint_of(stored)
    remaining = 100_000 - 20 * 1_000

    with boundary_held_elsewhere(stored):
        over, fits = run_beside(
            lambda: media_store.upload_refusal(stored, remaining + 1, "clip.mp4", "source"),
            lambda: media_store.upload_refusal(stored, remaining, "clip.mp4", "source"))

    assert isinstance(over, str)
    assert fits is None
    assert checkpoint_of(stored) == before
    assert lag_of(stored) == 20


def test_an_upload_decides_at_the_head_while_a_follow_round_is_publishing(
        stored, selected_store, store_thread, monkeypatch):
    """A reader beside an open publication transaction still sees a complete cursor and replays its suffix."""
    allow(selected_store, stored, 1_000)
    assert media_bindings.recover(stored)
    bind(store_thread, range(2), size=400)
    previous = checkpoint_of(stored)
    publishing, admit_done = threading.Event(), threading.Event()
    publish = media_bindings._publish

    def park_publication(*args, **kwargs):
        publishing.set()
        assert admit_done.wait(30), "admission waited on the follow transaction"
        return publish(*args, **kwargs)

    monkeypatch.setattr(media_bindings, "_publish", park_publication)

    def admit():
        try:
            assert publishing.wait(30)
            assert checkpoint_of(stored) == previous
            return media_store.upload_refusal(stored, 201, "clip.mp4", "source")
        finally:
            admit_done.set()

    followed, refused = run_beside(lambda: media_bindings.follow_store(stored), admit)

    assert followed and lag_of(stored) == 0
    assert isinstance(refused, str)


def test_an_upload_past_the_suffix_bound_is_answered_busy_until_the_projection_follows(
        stored, selected_store, store_thread, monkeypatch, caplog):
    """Catches an upload admitted on lag: past 2,048 unfollowed rows the total is not known, so the upload must
    wait for the follow, with a retry hint, and then decide on the exact total."""
    allow(selected_store, stored, 100_000)
    assert media_bindings.recover(stored)
    rows = media_bindings.UPLOAD_SUFFIX_ROWS + 4
    bind(store_thread, range(rows), size=40)
    previous = checkpoint_of(stored)
    placement = "Knowledge Base/_trash/archived.bin.meta.json"

    def blocked_behind_projection(*_args, **_kwargs):
        raise trash_metadata.PlacementChanged(placement)

    with monkeypatch.context() as blocked, caplog.at_level("INFO", logger="exomem.media_bindings"):
        blocked.setattr(media_bindings, "_derive_round", blocked_behind_projection)
        followed = media_bindings.follow(stored)
        busy = media_store.upload_refusal(stored, 10, "clip.mp4", "source")
        answer = asyncio.run(server_transfer._allowance_refused(stored, 10, "clip.mp4", "source"))

    assert followed.state == media_bindings.LAGGING
    assert placement in caplog.text
    assert checkpoint_of(stored) == previous
    assert isinstance(busy, media_store.UploadBusy) and busy.rows == rows
    # No watcher runs for this vault, so the hint is one reconcile interval.
    assert busy.retry_after == math.ceil(file_watcher.RECONCILE_INTERVAL_SECONDS)
    assert (answer.status, answer.body["code"], answer.headers["Retry-After"]) == (
        503, media_store.STORE_BUSY_CODE, str(busy.retry_after))
    assert answer.body["diagnostics"] == {"audit_rows": rows}
    media_bindings.follow_until(stored)
    remaining = 100_000 - rows * 40
    assert isinstance(media_store.upload_refusal(stored, remaining + 1, "clip.mp4", "source"), str)
    assert media_store.upload_refusal(stored, remaining, "clip.mp4", "source") is None


@pytest.mark.parametrize("cause", ["withdrawn", "no audit position", "side table generation", "store read error",
                                   "placement changed"])
def test_an_upload_whose_total_cannot_be_read_now_is_admitted_as_unknown_without_a_rebuild(
        stored, selected_store, store_thread, monkeypatch, cause):
    """Catches an upload refused, or a rebuild started on a request thread, when the projection cannot count now:
    the upload is admitted and offload's own admission decides later."""
    allow(selected_store, stored, 1_000)
    assert media_bindings.recover(stored)
    bind(store_thread, range(3), size=400)
    if cause == "withdrawn":
        media_bindings.withdraw(stored)
    elif cause == "no audit position":  # as a pooled vault's projection from file mode records none
        with closing(media_jobs.MediaJobStore(stored)._connect()) as conn, conn:
            media_bindings._record(conn, dataclasses.replace(checkpoint_of(stored), seq=None))
    elif cause == "side table generation":
        with closing(media_jobs.MediaJobStore(stored)._connect()) as conn, conn:
            media_bindings._put_meta(conn, media_bindings._SIDE, checkpoint_of(stored).generation + 1)
    else:
        error = (sqlite3.OperationalError("disk I/O error") if cause == "store read error"
                 else trash_metadata.PlacementChanged("a Trash placement changed while the suffix read it"))

        def unreadable(*_args, **_kwargs):
            raise error
        monkeypatch.setattr(media_bindings, "_derive_round", unreadable)
    monkeypatch.setattr(media_bindings, "rebuild", lambda _root: pytest.fail("an upload started a rebuild"))

    assert media_store.upload_refusal(stored, 900, "clip.mp4", "source") is None


def test_a_ticket_answered_busy_before_its_body_keeps_the_ticket(stored, selected_store, store_thread, upload_env):
    """Catches a busy answer that spends the upload link: the uploader is told to retry, so the same link must
    still work once the projection has followed, and then decide on the exact total."""
    import httpx

    allow(selected_store, stored, 100_000)
    assert media_bindings.recover(stored)
    rows = media_bindings.UPLOAD_SUFFIX_ROWS + 4
    bind(store_thread, range(rows), size=40)
    token = _prepare(upload_env)["token"]
    remaining = 100_000 - rows * 40

    async def send():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=upload_env[-1]),
                                     base_url="https://capture.example") as client:
            return await client.post("/upload", content=b"--part\r\n", headers={
                "Authorization": "Bearer " + token, "Content-Type": "multipart/form-data; boundary=part",
                "Content-Length": str(remaining + 1 + server_transfer._FORM_OVERHEAD_BYTES)})

    busy = asyncio.run(send())
    media_bindings.follow_until(stored)
    refused = asyncio.run(send())

    assert busy.status_code == 503, busy.text
    assert busy.json()["code"] == media_store.STORE_BUSY_CODE and int(busy.headers["Retry-After"]) >= 1
    assert "handoff_spent" not in busy.json()
    assert refused.status_code == 413, refused.text
    assert refused.json()["code"] == media_allowance.MEDIA_ALLOWANCE_EXCEEDED


def test_a_ticket_answered_busy_after_its_form_leads_with_a_new_link(stored, selected_store, store_thread, upload_env):
    """Catches a busy answer after the link was spent that tells the uploader to retry with that same link."""
    import httpx

    allow(selected_store, stored, 100_000)
    assert media_bindings.recover(stored)
    rows = media_bindings.UPLOAD_SUFFIX_ROWS + 4
    bind(store_thread, range(rows), size=40)
    data = b"a picture that a person took"
    descriptor = _prepare(upload_env, data, lane="evidence")

    async def send():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=upload_env[-1]),
                                     base_url="https://capture.example") as client:
            return await client.post("/upload", headers={"Authorization": "Bearer " + descriptor["token"]},
                                     files={"file": ("photo.png", data, "image/png")},
                                     data={"hold": "1", "lane": "evidence"})

    busy = asyncio.run(send())

    assert busy.status_code == 503, busy.text
    retry = busy.headers["Retry-After"]
    assert busy.json()["reason"].startswith(
        f"This upload link is used now; request a new link from upload_files after {retry} seconds.")
    assert busy.json()["diagnostics"] == {"audit_rows": rows}
    media_bindings.follow_until(stored)
    descriptor = _prepare(upload_env, data, lane="evidence")
    assert asyncio.run(send()).status_code == 201


# Tasks 3.1 and 3.2: offload in store mode.

@pytest.mark.parametrize("versions", [100, 1_000])
def test_offload_reads_no_note_and_replays_a_bounded_suffix_while_it_holds_the_boundary(
        stored, selected_store, store_thread, monkeypatch, versions):
    """Catches the owner census walking every note under the boundary at each hold, and a hold that replays an
    unbounded audit suffix: 600 saves land while the bytes go to the store, between admission and publication."""
    assert media_bindings.recover(stored)
    bind(store_thread, range(versions), size=10)
    path = stored_original(stored, store_thread)
    monkeypatch.setattr(media_allowance, "reference_usage", lambda *a, **k: pytest.fail("a census walked the notes"))
    monkeypatch.setattr(media_allowance, "bound_references", lambda *a, **k: pytest.fail("a census walked the notes"))
    replayed: list[int] = []
    derive = media_bindings._derive_round

    def counted(root, conn, view, paths, selected):
        paths = tuple(paths)
        if mutation_lock.current_thread_holds_boundary():
            replayed.append(len(paths))
        return derive(root, conn, view, paths, selected)

    monkeypatch.setattr(media_bindings, "_derive_round", counted)
    upload = media_store.FilesystemStore.upload

    def saves_land(*args, **kwargs):
        bind(store_thread, range(600), size=1, prefix="during-upload")
        with read.snapshot(stored, join_request=False) as view:
            last_write = view.connection.execute("SELECT revision FROM store_audit ORDER BY seq DESC LIMIT 1").fetchone()
            assert view.revision > last_write[0], "the derivation drain must run for the writes"
        return upload(*args, **kwargs)

    monkeypatch.setattr(media_store.FilesystemStore, "upload", saves_land)

    assert media_store.offload(stored, path) is not None
    assert replayed, "no hold counted the saves that landed before it"
    assert max(replayed) <= media_bindings.REPLAY_ROWS


def test_the_projection_counts_the_same_versions_as_the_census_after_writes_and_moves(
        stored, selected_store, store_thread):
    """A move must retire the old binding, and two filings of a single version count only once."""
    allow(selected_store, stored, 100)
    assert media_bindings.recover(stored)
    files = projector.Projector(stored, run=store_thread.run)
    original = f"{NOTES}/original.bin"
    shared = f"{NOTES}/shared.bin"
    moved = f"{NOTES}/moved.bin"
    original_body = counted_page(original, 30, "shared-version")
    shared_body = original_body.replace(original.encode(), shared.encode())

    def compare(expected):
        files.run_pass()
        assert media_bindings.follow_store(stored)
        total = media_bindings.head_total(stored)
        assert total.used_bytes == media_allowance.reference_usage(stored).used_bytes == expected

    put_page(store_thread, f"{original}.md", original_body)
    compare(30)
    put_page(store_thread, f"{shared}.md", shared_body)
    put_page(store_thread, f"{NOTES}/foreign.bin.md", counted_page(f"{NOTES}/foreign.bin", 60, "v1", store="foreign"))
    compare(30)

    def move(writer):
        new_body = original_body.replace(original.encode(), moved.encode())
        write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "test", moves=(
            write.PageMove(f"{original}.md", f"{moved}.md", body=new_body),)))

    store_thread.run(move)
    compare(30)

    put_page(store_thread, f"{shared}.md", shared_body.replace(b"binary_size: 30", b"binary_size: 31"))
    compare(None)

    def delete(writer):
        write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "test", deletes=(
            write.PageDelete(f"{shared}.md"), write.PageDelete(f"{moved}.md"),)))

    store_thread.run(delete)
    compare(0)


def test_a_confirmed_old_version_counts_once_on_both_a_moved_page_and_a_sync_copy(
        stored, selected_store, store_thread):
    """Count live, displaced and Trash versions once, without taking an uncountable reference as zero."""
    assert media_bindings.recover(stored)
    path = stored_original(stored, store_thread)
    original = path.relative_to(stored).as_posix()
    companion = f"{original}.md"
    assert media_store.offload(stored, path) is not None
    old_version = read.owned_bytes(stored, companion)
    new_body = counted_page(original, 30, "new-version")
    old_page = f"{NOTES}/Older reference.md"
    outside_copy = stored / f"{NOTES}/Older reference.sync-conflict-20260101-120000-ABCDEFG.md"
    trash_original = path.with_name("other.png")
    trash_content = PICTURE_BYTES + b" distinct original"
    trash_original.write_bytes(trash_content)
    trash_rel = trash_original.relative_to(stored).as_posix()
    importer.Importer(stored, run=store_thread.run).import_paths([trash_rel])
    assert media_store.offload(stored, trash_original) is not None
    on_store_thread(store_thread, lambda: delete_file.delete_file(stored, path=trash_rel, confirm=True))

    put_page(store_thread, companion, new_body)
    put_page(store_thread, old_page, old_version)
    foreign = f"{NOTES}/foreign-reference.bin"
    put_page(store_thread, f"{foreign}.md", counted_page(foreign, 60, "v1", store="another-store"))
    outside_copy.write_bytes(old_version)
    reserved = stored / "Knowledge Base/_Governance/ignored.bin.md"
    reserved.parent.mkdir(exist_ok=True)
    reserved.write_bytes(counted_page(reserved.relative_to(stored).as_posix()[:-3], 99, "reserved"))
    assert media_bindings.rebuild(stored)
    files = projector.Projector(stored, run=store_thread.run)
    files.run_pass()

    total = media_bindings.head_total(stored)
    expected = len(PICTURE_BYTES) + len(trash_content) + 30
    assert total.used_bytes == media_allowance.reference_usage(stored).used_bytes == expected
    assert side_row(stored, old_page)["counted"] == 1
    assert side_row(stored, outside_copy.relative_to(stored).as_posix())["counted"] == 1

    changed = f"{NOTES}/extra.bin"
    conflicting = new_body.replace(original.encode(), changed.encode()).replace(b"binary_size: 30", b"binary_size: 31")
    for uncertain in (
        conflicting,
        b"---\nstored_object:\n  schema: 1\n---\n",
        new_body.replace(f"evidence_file: {original}\n".encode(), b""),
    ):
        put_page(store_thread, f"{changed}.md", uncertain)
        files.run_pass()
        assert media_bindings.follow_store(stored)
        assert media_bindings.head_total(stored).used_bytes is None
        assert media_allowance.reference_usage(stored).used_bytes is None


def test_the_allowance_follows_recopy_release_and_trash_recovery(stored, selected_store, store_thread):
    """Replacing a damaged pointer and retiring it must each leave only the versions the census counts."""
    assert media_bindings.recover(stored)
    path = stored_original(stored, store_thread)
    relative = path.relative_to(stored).as_posix()
    files = projector.Projector(stored, run=store_thread.run)

    def compare(expected):
        files.run_pass()
        assert media_bindings.follow_store(stored)
        assert media_bindings.head_total(stored).used_bytes == media_allowance.reference_usage(stored).used_bytes == expected

    first = media_store.offload(stored, path)
    assert first is not None
    compare(len(PICTURE_BYTES))

    values, _config = selected_store
    (Path(values["root"]) / first.object / first.version).write_bytes(b"damaged ciphertext")
    media_replicas.quarantine(stored, relative, first, level=media_replicas.CONTENT, failure="digest_mismatch")
    replacement = media_store.recopy(stored, path, first.store)
    assert replacement is not None and replacement != first
    compare(len(PICTURE_BYTES))

    media_store.forget_original(stored, path)
    compare(0)

    assert media_store.offload(stored, path) is not None
    trashed = on_store_thread(store_thread, lambda: delete_file.delete_file(stored, path=relative, confirm=True))
    compare(len(PICTURE_BYTES))

    restored = on_store_thread(store_thread, lambda: recover_from_trash.recover_from_trash(
        stored, trash_path=trashed.trash_path))
    assert restored.restored_path == relative
    compare(0)

    assert media_store.offload(stored, path) is not None
    trashed = on_store_thread(store_thread, lambda: delete_file.delete_file(stored, path=relative, confirm=True))
    compare(len(PICTURE_BYTES))
    trash = stored / trashed.trash_path
    trash_page = originals.sidecar_path(trash)
    for removed in (trash, trash_page, Path(f"{trash}{trash_metadata.METADATA_SUFFIX}")):
        removed.unlink(missing_ok=True)
    report = importer.Importer(stored, run=store_thread.run).sweep()
    trash_companion = trash_page.relative_to(stored).as_posix()
    assert trash_companion in report.held
    [item] = [item for item in held.list_held(stored) if item.path == trash_companion]
    on_store_thread(store_thread, lambda: held.resolve_held(
        stored, item.path, held.KEEP_MINE, why="Empty this Trash entry", seen=item.item_version))
    compare(0)


@pytest.mark.parametrize("phase", ["admission", "publication", "scan"])
def test_a_save_commits_while_the_owner_total_is_being_read(stored, selected_store, store_thread, monkeypatch, phase):
    """Catches an owner-total read that holds the mutation boundary: a person's save waits behind it, as it did
    behind the census for the whole walk of the notes."""
    if phase == "scan":
        allow(selected_store, stored, 1)  # an allowance refusal, so that the scan reads the total
    assert media_bindings.recover(stored)
    path = stored_original(stored, store_thread)
    if phase == "scan":
        with pytest.raises(media_store.MediaStoreError):
            media_store.offload(stored, path)
    parked, release, uploaded = threading.Event(), threading.Event(), threading.Event()
    upload = media_store.FilesystemStore.upload

    def mark_upload(*args, **kwargs):
        uploaded.set()
        return upload(*args, **kwargs)

    monkeypatch.setattr(media_store.FilesystemStore, "upload", mark_upload)
    # Admission and the scan read the total at the head; publication first follows, after the bytes are stored.
    target = "follow_until" if phase == "publication" else "head_total"
    real = getattr(media_bindings, target)

    def park(*args, **kwargs):
        if (phase != "publication" or uploaded.is_set()) and not parked.is_set():
            parked.set()
            release.wait(60)
        return real(*args, **kwargs)

    monkeypatch.setattr(media_bindings, target, park)
    work = media_store.enqueue_offloads if phase == "scan" else (lambda root: media_store.offload(root, path))
    worker = threading.Thread(target=work, args=(stored,), daemon=True)
    worker.start()
    assert parked.wait(60), "the owner total was never read"
    saved = []

    def save():
        def governed(_writer):
            # A served store's lease check requires the writing thread to hold the vault boundary.
            with media_store._guard(stored, "test_governed_save"), library_scope():
                saved.append(preserve.preserve(stored, scope="Test", category="landed", filename="landed.txt",
                                               content="A note that a person saved while media moved to the store."))
        store_thread.run(governed)

    try:
        run_beside(save, timeout=10)
    finally:
        release.set()
        worker.join(60)
    assert saved and read.owned_bytes(stored, saved[0].sidecar_path)


# Tasks 2.4, 2.7 and 3.6: a total that is not known now.

def test_an_offload_that_meets_a_projection_needing_a_rebuild_returns_its_job_and_records_the_wait(
        stored, selected_store, store_thread, caplog):
    """Catches a projection that is not current spending the job's attempt or refusing it, and a wait that no log
    shows: the job returns busy, its first wait stays on its row, and publication logs the whole wait."""
    assert media_bindings.recover(stored)
    path = stored_original(stored, store_thread)
    ledger = media_jobs.MediaJobStore(stored)
    media_store.enqueue_offload(stored, path)
    job = ledger.claim_next()
    media_bindings.withdraw(stored)

    with pytest.raises(media_store.MediaStoreError) as returned:
        media_store.offload(stored, path, job=job)

    assert returned.value.code == media_store.STORE_BUSY_CODE
    with closing(ledger._connect(readonly=True)) as conn:
        since, waits = conn.execute("SELECT offload_wait_since, offload_waits FROM jobs WHERE id = ?",
                                    (job.id,)).fetchone()
    assert since is not None and waits == 1
    assert media_bindings.recover(stored)
    with caplog.at_level("INFO"):
        assert media_store.offload(stored, path, job=job) is not None
    with closing(ledger._connect(readonly=True)) as conn:
        assert tuple(conn.execute("SELECT offload_wait_since, offload_waits FROM jobs WHERE id = ?",
                                  (job.id,)).fetchone()) == (None, 0)
    waits_logged = [record for record in caplog.records if "media_offload_wait" in record.getMessage()]
    assert len(waits_logged) == 1


def test_the_media_worker_refunds_the_attempt_while_the_projection_needs_rebuilding(
        stored, selected_store, store_thread, monkeypatch, tmp_path):
    """A waiting offload returns to its queue with all of its attempts available."""
    assert media_bindings.recover(stored)
    path = stored_original(stored, store_thread)
    ledger = media_jobs.MediaJobStore(stored)
    ledger.retire_ocr(path, "image")
    job_id = ledger.enqueue(media_jobs.MediaJob(
        path, path.with_name(f"{path.name}.md"), media_jobs.OFFLOAD_MEDIA_TYPE,
        do_ocr=False, do_offload=True,
    ))
    media_bindings.withdraw(stored)
    stop_writer()
    with monkeypatch.context() as parked:
        parked.setattr(derivation, "drain", lambda *_args, **_kwargs: None)
        put_page(store_thread, f"{NOTES}/owed-derivation.md", b"# A note awaiting derivation\n")
    entered, release = threading.Event(), threading.Event()
    derive = derivation._derive

    def pending(*args):
        entered.set()
        assert release.wait(45)
        return derive(*args)

    monkeypatch.setattr(derivation, "_derive", pending)
    draining = threading.Thread(
        target=store_thread.run, args=(lambda writer: derivation.drain(stored, writer),), daemon=True)
    draining.start()

    try:
        assert entered.wait(30), "the store must have a real derivation to drain"
        run_media_child(stored, tmp_path / "media-logs")
    finally:
        release.set()
        draining.join(40)
    assert not draining.is_alive()

    with closing(ledger._connect(readonly=True)) as conn:
        returned = conn.execute("SELECT state, attempts, returns, offload_waits FROM jobs WHERE id = ?",
                                (job_id,)).fetchone()
    assert tuple(returned) == (media_jobs.PENDING, 0, 1, 1)
    assert not checkpoint_of(stored).complete
    assert path.is_file()


def test_the_media_worker_reads_a_failed_rebuild_from_the_ledger(
        stored, selected_store, store_thread, monkeypatch, tmp_path):
    """The worker sees the named failure even though it runs in another process."""
    assert media_bindings.recover(stored)
    path = stored_original(stored, store_thread)
    ledger = media_jobs.MediaJobStore(stored)
    ledger.retire_ocr(path, "image")
    job_id = ledger.enqueue(media_jobs.MediaJob(
        path, path.with_name(f"{path.name}.md"), media_jobs.OFFLOAD_MEDIA_TYPE,
        do_ocr=False, do_offload=True,
    ))
    note = f"{NOTES}/cannot-read.md"

    def cannot_read(_root, reading):
        reading[:] = [note]
        raise OSError("the note could not be read")

    with monkeypatch.context() as unreadable:
        unreadable.setattr(media_bindings, "_left_out", cannot_read)
        assert media_bindings.rebuild(stored) is False
    put_page(store_thread, f"{NOTES}/after-failure.md", b"# A new note\n")
    monkeypatch.setattr(media_bindings, "rebuild", lambda _root: pytest.fail("an unrelated save triggered a rebuild"))
    assert not media_bindings.recover(stored)
    stop_writer()

    run_media_child(stored, tmp_path / "media-logs")

    with closing(ledger._connect(readonly=True)) as conn:
        state, reason = conn.execute("SELECT state, last_error FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert state == media_jobs.FAILED and media_allowance.MEDIA_ALLOWANCE_UNKNOWN in reason
    entries, _ = media_store.offload_refusals(stored)
    assert any(item.get("blocked_by") == [{"path": note, "reason": "the note could not be read"}] for item in entries)


def test_a_note_the_projection_cannot_count_refuses_offload_by_name_until_a_save_fixes_it(
        stored, selected_store, store_thread, monkeypatch, tmp_path):
    """Catches an uncountable note that the total skips (an offload past the allowance) or that only a rebuild
    clears: the refusal names the note, and the save that fixes it makes the total known through the follow."""
    assert media_bindings.recover(stored)
    broken = f"{NOTES}/broken-reference.md"
    put_page(store_thread, broken, b"---\nstored_object:\n  schema: 1\n---\nAn owner's hand edit.\n")
    path = stored_original(stored, store_thread)
    assert media_bindings.follow_store(stored)
    ledger = media_jobs.MediaJobStore(stored)
    ledger.retire_ocr(path, "image")
    job_id = ledger.enqueue(media_jobs.MediaJob(
        path, originals.sidecar_path(path), media_jobs.OFFLOAD_MEDIA_TYPE, do_ocr=False, do_offload=True,
    ))
    stop_writer()

    run_media_child(stored, tmp_path / "media-logs")

    with closing(ledger._connect(readonly=True)) as conn:
        state, reason = conn.execute("SELECT state, last_error FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert state == media_jobs.FAILED and media_allowance.MEDIA_ALLOWANCE_UNKNOWN in reason
    entries, _ = media_store.offload_refusals(stored)
    assert any(blocker.get("path") == broken for item in entries for blocker in item.get("blocked_by", ()))
    assert checkpoint_of(stored).complete
    monkeypatch.setattr(media_bindings, "rebuild", lambda _root: pytest.fail("the fix waited for a rebuild"))
    put_page(store_thread, f"{NOTES}/unrelated.md", b"# An unrelated note\n")
    with pytest.raises(media_store.MediaStoreError) as still_refused:
        media_store.offload(stored, path)
    assert still_refused.value.code == "MEDIA_ALLOWANCE_UNKNOWN"
    assert side_row(stored, broken)["counted"] == 0
    put_page(store_thread, broken, b"---\ntitle: fixed\n---\nAn owner's hand edit, fixed.\n")
    assert media_store.offload(stored, path) is not None


def test_a_different_custody_seam_can_fail_without_withdrawing_the_media_projection(
        stored, selected_store, store_thread, monkeypatch):
    assert media_bindings.recover(stored)
    before = checkpoint_of(stored)
    monkeypatch.setattr(freshness, "_custody_seams", dict(freshness._custody_seams))
    media_store.register_path_custody()

    def broken(*_args):
        raise OSError("another projection could not apply its receipt")

    freshness.register_custody_seam(freshness.CustodySeam(name="failed-test-projection", apply=broken))
    report = freshness.apply_receipt_paths(stored, changed=[f"{NOTES}/Alpha.md"])

    assert ("failed-test-projection", f"{NOTES}/Alpha.md") in report.mismatches
    assert checkpoint_of(stored) == before
    with closing(media_jobs.MediaJobStore(stored, create=False)._connect(readonly=True)) as conn:
        assert media_bindings.current(stored, conn)


def test_the_first_sweep_counts_a_reference_written_while_the_service_was_stopped(
        stored, selected_store, store_thread, monkeypatch):
    """A disk edit enters the owner total through the store import and its audit row."""
    allow(selected_store, stored, 100)
    assert media_bindings.recover(stored)
    before = media_bindings.source_checkpoint(stored)[1]
    original = f"{NOTES}/offline.bin"
    companion = f"{original}.md"
    old_file = counted_page(original, 30, "offline-version")
    (stored / companion).write_bytes(old_file)

    sweep = importer.Importer(stored, run=store_thread.run).sweep
    result = sweep()
    assert companion in result.imported
    assert media_bindings.source_checkpoint(stored)[1] > before
    assert media_bindings.recover(stored, sweep=True)

    total = media_bindings.head_total(stored)
    assert (total.state, total.used_bytes) == (media_bindings.COUNTED, 30)
    (stored / companion).write_bytes(counted_page(original, 50, "next-version"))
    assert companion in sweep().imported
    assert media_bindings.recover(stored)
    assert media_bindings.head_total(stored).used_bytes == 50

    (stored / companion).write_bytes(old_file)
    with monkeypatch.context() as paused:
        paused.setattr(media_bindings, "follow", lambda *_args, **_kwargs: media_bindings.Followed(media_bindings.LAGGING))
        assert companion in sweep().imported
    with closing(media_jobs.MediaJobStore(stored, create=False)._connect(readonly=True)) as conn:
        assert not media_bindings.current(stored, conn)
    assert media_bindings.recover(stored)
    assert media_bindings.head_total(stored).used_bytes == 30


def test_a_database_restore_can_pass_the_same_audit_position_without_following_the_same_row(
        stored, selected_store, store_thread, tmp_path):
    """Matching sequence numbers after a restore cannot prove that the bindings still match."""
    allow(selected_store, stored, 100)
    assert media_bindings.recover(stored)
    backup_path = tmp_path / "older-store.sqlite"
    with (closing(collection_connection.open_reader(collection_connection.store_path(stored))) as previous,
          closing(sqlite3.connect(backup_path)) as backup):
        previous.backup(backup)
    bind(store_thread, range(1), size=40, prefix="discarded-by-restore")
    assert media_bindings.follow_store(stored)
    before = checkpoint_of(stored)

    def restore(writer):
        with closing(sqlite3.connect(backup_path)) as backup:
            backup.backup(writer.handle.connection)

    store_thread.run(restore)
    with closing(media_jobs.MediaJobStore(stored, create=False)._connect(readonly=True)) as conn:
        assert not media_bindings.current(stored, conn)
    bind(store_thread, range(2), size=15, prefix="written-after-restore")
    assert media_bindings.source_checkpoint(stored)[1] > before.seq

    assert media_bindings.follow(stored).state == media_bindings.REBUILD
    assert not checkpoint_of(stored).complete
    assert media_bindings.recover(stored)
    total = media_bindings.head_total(stored)
    assert (total.state, total.used_bytes) == (media_bindings.COUNTED, 30)


def test_an_outside_page_that_keeps_changing_is_counted_on_the_next_sweep(
        stored, selected_store, store_thread, monkeypatch):
    """A raced outside read keeps the last complete projection until a later sweep can count the new version."""
    allow(selected_store, stored, 100)
    outside = f"{NOTES}/.outside/original.bin"
    companion = stored / f"{outside}.md"
    companion.parent.mkdir()
    companion.write_bytes(counted_page(outside, 20, "first-version"))
    assert media_bindings.recover(stored)
    before = checkpoint_of(stored)
    assert media_bindings.head_total(stored).used_bytes == 20
    companion.write_bytes(counted_page(outside, 40, "next-version"))
    read_header = vault_module.read_frontmatter_header
    changes = []

    def change_while_reading(*args, **kwargs):
        header = read_header(*args, **kwargs)
        if b"next-version" in header:
            with companion.open("ab") as file:
                file.write(b"\nA concurrent edit.\n")
            changes.append(1)
        return header

    with monkeypatch.context() as racing:
        racing.setattr(vault_module, "read_frontmatter_header", change_while_reading)
        assert not media_bindings.check_outside(stored)
    assert len(changes) == 2
    assert checkpoint_of(stored) == before
    assert media_bindings.head_total(stored).used_bytes == 20

    assert media_bindings.check_outside(stored)
    assert media_bindings.head_total(stored).used_bytes == 40


def test_a_save_of_an_owned_page_can_land_during_the_outside_walk(
        stored, selected_store, store_thread, monkeypatch):
    """A write to a page on the audit trail does not block this census of files outside the store."""
    outside = f"{NOTES}/.outside/original.bin"
    companion = stored / f"{outside}.md"
    companion.parent.mkdir()
    companion.write_bytes(counted_page(outside, 20, "outside-version"))
    assert media_bindings.recover(stored)
    edited = f"{NOTES}/Edit while counting.md"
    body = b"# Edit while counting\n"
    read_header = vault_module.read_frontmatter_header
    saved = False

    def save_during_the_walk(*args, **kwargs):
        nonlocal saved
        header = read_header(*args, **kwargs)
        if b"outside-version" in header and not saved:
            saved = True
            put_page(store_thread, edited, body)
            projector.Projector(stored, run=store_thread.run).run_pass()
        return header

    monkeypatch.setattr(vault_module, "read_frontmatter_header", save_during_the_walk)

    assert media_bindings.check_outside(stored)
    assert (stored / edited).read_bytes() == body
    assert checkpoint_of(stored).complete
    assert media_bindings.head_total(stored).used_bytes == 20


def test_a_rebuild_retries_when_a_legacy_cache_row_lands_after_its_diff(
        stored, selected_store, store_thread, monkeypatch):
    """The retry transfers the filing's read age to the physical key and keeps its byte proof."""
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(stored)
    original = "Knowledge Base/Evidence/Archive/pooled.bin"
    put_page(store_thread, f"{original}.md", counted_page(original, 12, "pooled-version"))
    assert media_bindings.recover(stored)
    ledger = media_jobs.MediaJobStore(stored)
    with closing(ledger._connect()) as conn, conn:
        physical, digest, reference = conn.execute(
            "SELECT object_key, sha256, reference FROM media_bindings WHERE binary_rel = ?", (original,)).fetchone()
        assert physical != original
        conn.execute("INSERT INTO media_cache(binary_rel, reference, sha256, identity, last_read, size, owner_read) "
                     "VALUES (?, ?, ?, 'physical-proof', 10, 12, 10)", (physical, reference, digest))
    before = checkpoint_of(stored)
    stage = media_bindings._census_difference

    def add_legacy_after_staging(conn, *, filings):
        ledger_version = stage(conn, filings=filings)
        with closing(ledger._connect()) as other, other:
            other.execute("INSERT INTO media_cache(binary_rel, reference, sha256, identity, last_read, size, owner_read) "
                          "VALUES (?, ?, ?, 'filing-proof', 20, 12, 20)", (original, reference, digest))
        return ledger_version

    with monkeypatch.context() as racing:
        racing.setattr(media_bindings, "_census_difference", add_legacy_after_staging)
        assert media_bindings.rebuild(stored) is None
    assert checkpoint_of(stored) == before

    assert media_bindings.rebuild(stored)
    with closing(ledger._connect(readonly=True)) as conn:
        proofs = [tuple(row) for row in conn.execute("SELECT binary_rel, identity, owner_read FROM media_cache")]
    assert proofs == [(physical, "physical-proof", 20)]


def test_a_rebuild_keeps_its_projection_while_another_ledger_writer_has_the_slot(
        stored, selected_store, monkeypatch):
    """A lock wait inside the vault guard would stall saves and could withdraw a usable projection."""
    assert media_bindings.rebuild(stored)
    previous = checkpoint_of(stored)
    ledger = media_jobs.MediaJobStore(stored)
    stage = media_bindings._census_difference

    with closing(ledger._connect()) as other, monkeypatch.context() as busy:
        def hold_writer_after_staging(conn, *, filings):
            version = stage(conn, filings=filings)
            other.execute("BEGIN IMMEDIATE")
            return version

        busy.setattr(media_bindings, "_census_difference", hold_writer_after_staging)
        assert media_bindings.rebuild(stored) is None
        assert checkpoint_of(stored) == previous
        assert media_bindings.head_total(stored).state == media_bindings.COUNTED

    assert media_bindings.rebuild(stored)


def test_an_existing_pooled_projection_rebuilds_once_when_it_has_no_audit_identity(
        stored, selected_store, store_thread, monkeypatch):
    """An old projection can be complete and at the audit head without proving which row it followed."""
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(stored)
    assert media_bindings.recover(stored)
    legacy = checkpoint_of(stored)
    ledger = media_jobs.MediaJobStore(stored)
    with closing(ledger._connect()) as conn, conn:
        conn.execute("DROP TABLE media_reference_rows")
        conn.execute("DELETE FROM meta WHERE key IN (?, ?)", (media_bindings._IDENTITY, media_bindings._SIDE))
    assert legacy.complete and media_bindings.source_checkpoint(stored)[1] == legacy.seq

    assert media_bindings.recover(stored)
    rebuilt = checkpoint_of(stored)
    assert rebuilt.complete and rebuilt.generation > legacy.generation
    assert rebuilt.seq == legacy.seq
    monkeypatch.setattr(media_bindings, "rebuild", lambda _root: pytest.fail("recovery rebuilt the projection twice"))

    assert media_bindings.recover(stored)
    assert checkpoint_of(stored) == rebuilt


# Tasks 3.8 and 3.13: the page classifier.

UNPARSED = b"---\ntitle: [unclosed\n---\n"


def _bound(fields: str, rel: str) -> bytes:
    return f"---\nevidence_file: {rel[:-3]}\n{fields}---\n".encode()


@pytest.mark.parametrize(("body", "flagged"), [
    pytest.param(lambda rel: UNPARSED, True, id="does not parse"),
    pytest.param(lambda rel: _bound("", rel), True, id="incomplete identity"),
    pytest.param(lambda rel: (f"---\nevidence_file: ../outside.bin\nbinary_sha256: {'a' * 64}\n"
                              "binary_size: 3\n---\n").encode(), True, id="unsafe path"),
    pytest.param(lambda rel: _bound(f"binary_sha256: {'a' * 64}\nbinary_size: 3\nstored_object:\n  schema: 1\n",
                                    rel), True, id="incomplete recovery reference"),
    pytest.param(lambda rel: b"---\nstored_object:\n  schema: 1\n---\n", False, id="malformed reference unbound"),
    pytest.param(lambda rel: counted_page("x", 3, "v1").replace(b"evidence_file: x\n", b""), False,
                 id="valid reference without a binding"),
])
def test_a_page_the_projection_cannot_count_gets_an_unknown_row_flagged_only_for_a_custody_fault(
        stored, selected_store, store_thread, body, flagged):
    """Catches a fault that the follow drops (a silent undercount), and a flag on a page whose fault only the
    allowance cares about, which would stop eviction for nothing."""
    assert media_bindings.recover(stored)
    rel = f"{NOTES}/classified.bin.md"
    put_page(store_thread, rel, body(rel))

    followed = media_bindings.follow_until(stored)

    row = side_row(stored, rel)
    assert followed.state == media_bindings.HEAD and checkpoint_of(stored).complete
    assert (row["counted"], row["flagged"]) == (0, int(flagged)), row["reason"]


def test_a_trash_page_without_a_placement_marks_the_reference_unknown(stored, selected_store, store_thread):
    assert media_bindings.recover(stored)
    rel = "Knowledge Base/_trash/unplaced.bin.md"
    put_page(store_thread, rel, counted_page(f"{NOTES}/original.bin", 30, "stored-version"))

    assert media_bindings.follow_store(stored)

    row = side_row(stored, rel)
    assert (row["counted"], row["flagged"]) == (0, 1)
    assert "trustworthy placement" in row["reason"]


def test_the_follow_waits_for_a_queued_trash_placement_move(stored, selected_store, store_thread):
    assert media_bindings.recover(stored)
    before = checkpoint_of(stored)
    original = f"{NOTES}/parked.bin"
    trash = "Knowledge Base/_trash/2026-01-01/parked.bin"
    placement = trash + trash_metadata.METADATA_SUFFIX
    staged = f"{NOTES}/staged-placement.json"
    data = json.dumps({"original_path": original}).encode()
    (stored / staged).write_bytes(data)
    (stored / trash).parent.mkdir(parents=True, exist_ok=True)
    store_thread.run(lambda writer: write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "test", puts=(write.PagePut(f"{trash}.md", counted_page(original, 30, "v1")),),
        binary_moves=(write.BinaryMove(staged, placement, hashlib.sha256(data).hexdigest(), len(data)),))))

    assert media_bindings.follow_until(stored).state == media_bindings.LAGGING
    assert checkpoint_of(stored) == before
    assert side_row(stored, f"{trash}.md") is None

    projector.Projector(stored, run=store_thread.run).run_pass()

    assert (stored / placement).read_bytes() == data
    assert media_bindings.follow_store(stored)
    assert media_bindings.head_total(stored).used_bytes == 30


@pytest.mark.parametrize("fault", ["broken witness", "deleted Trash companion"])
def test_a_retirement_requires_the_current_authoritative_trash_companion(
        stored, selected_store, store_thread, monkeypatch, fault):
    assert media_bindings.recover(stored)
    path = stored_original(stored, store_thread)
    assert media_store.offload(stored, path) is not None
    relative = path.relative_to(stored).as_posix()
    source_companion = originals.sidecar_path(Path(relative)).as_posix()
    with monkeypatch.context() as parked:
        parked.setattr(media_bindings, "repair_paths", lambda *_args: None)
        trashed = on_store_thread(store_thread, lambda: delete_file.delete_file(stored, path=relative, confirm=True))
        projector.Projector(stored, run=store_thread.run).run_pass()
        trash_companion = originals.sidecar_path(Path(trashed.trash_path)).as_posix()
        assert (stored / trash_companion).is_file()
        if fault == "broken witness":
            body = read.owned_bytes(stored, source_companion)
            put_page(store_thread, source_companion, body + b"\nAn unwitnessed edit.\n")
        else:
            store_thread.run(lambda writer: write.commit(writer, write.StoreChange(
                write.Origin.GOVERNED, "test", deletes=(write.PageDelete(trash_companion),))))

    if fault == "broken witness":
        assert media_bindings.rebuild(stored)
        row = side_row(stored, source_companion)
        assert (row["counted"], row["flagged"]) == (0, 1)
        assert "witnessed companion" in row["reason"]
    else:
        assert (stored / trash_companion).is_file(), "the pending projector delete must leave a stale disk copy"
        assert media_bindings.follow_store(stored)
        with closing(media_jobs.MediaJobStore(stored, create=False)._connect(readonly=True)) as ledger:
            assert ledger.execute("SELECT companion_rel FROM media_bindings WHERE binary_rel = ?",
                                  (relative,)).fetchone()[0] == source_companion
            assert media_bindings.flagged_pages(ledger) == ()


@pytest.mark.parametrize("failure", ["read", "changed", "gone at recheck"])
def test_a_trash_placement_read_failure_withdraws_the_rebuild_without_flagging_a_page(
        stored, selected_store, store_thread, monkeypatch, failure):
    assert media_bindings.recover(stored)
    path = stored_original(stored, store_thread)
    assert media_store.offload(stored, path) is not None
    relative = path.relative_to(stored).as_posix()
    trashed = on_store_thread(store_thread, lambda: delete_file.delete_file(stored, path=relative, confirm=True))
    projector.Projector(stored, run=store_thread.run).run_pass()
    assert media_bindings.follow_store(stored)
    source_page = originals.sidecar_path(Path(relative)).as_posix()
    placement = trashed.trash_path + trash_metadata.METADATA_SUFFIX

    with monkeypatch.context() as failing:
        if failure == "gone at recheck":
            require_current = trash_metadata.PlacementCensus.require_current

            def disappeared(census):
                assert placement in census.versions
                (stored / placement).unlink()
                require_current(census)

            failing.setattr(trash_metadata.PlacementCensus, "require_current", disappeared)
        else:
            document = trash_metadata._document

            def unreadable(root, rel):
                if rel == placement:
                    if failure == "changed":
                        raise trash_metadata.PlacementChanged(rel)
                    raise OSError("could not read the Trash placement")
                return document(root, rel)

            failing.setattr(trash_metadata, "_document", unreadable)
            if failure == "changed":
                before = checkpoint_of(stored)
                original_body = read.owned_bytes(stored, source_page)
                put_page(store_thread, source_page, original_body + b"\n")
                assert media_bindings.follow_until(stored).state == media_bindings.LAGGING
                assert checkpoint_of(stored) == before
        assert media_bindings.rebuild(stored) is False

    assert not checkpoint_of(stored).complete
    with closing(media_jobs.MediaJobStore(stored, create=False)._connect(readonly=True)) as ledger:
        assert ledger.execute("SELECT companion_rel FROM media_reference_rows WHERE flagged = 1").fetchall() == []


def test_two_companions_that_claim_one_original_block_eviction_until_one_is_removed(
        stored, selected_store, store_thread):
    assert media_bindings.recover(stored)
    original = "Knowledge Base/Sources/Archive/original.md"
    current, legacy = originals.companion_paths(original)
    body = counted_page(original, 30, "stored-version")
    put_page(store_thread, current, body)
    assert media_bindings.follow_store(stored)
    put_page(store_thread, legacy, body)

    assert media_bindings.follow_store(stored)

    assert side_row(stored, current)["flagged"] == side_row(stored, legacy)["flagged"] == 1
    assert media_bindings.head_total(stored).state == media_bindings.UNKNOWN
    store_thread.run(lambda writer: write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "test", deletes=(write.PageDelete(legacy),))))
    assert media_bindings.follow_store(stored)
    assert side_row(stored, current) is None and side_row(stored, legacy) is None
    assert media_bindings.head_total(stored).used_bytes == 30


def test_a_companion_that_loses_its_binding_keeps_eviction_blocked_until_it_is_fixed(
        stored, selected_store, store_thread):
    assert media_bindings.recover(stored)
    original = f"{NOTES}/kept.bin"
    rel = f"{original}.md"
    body = counted_page(original, 30, "stored-version")
    put_page(store_thread, rel, body)
    assert media_bindings.follow_store(stored)
    assert media_bindings.head_total(stored).used_bytes == 30

    put_page(store_thread, rel, b"# The preserved note\n")
    assert media_bindings.follow_store(stored)

    assert side_row(stored, rel)["flagged"] == 1
    with closing(media_jobs.MediaJobStore(stored, create=False)._connect(readonly=True)) as conn:
        assert not media_bindings.current(stored, conn)
    assert media_bindings.rebuild(stored)
    assert side_row(stored, rel)["flagged"] == 1

    put_page(store_thread, rel, body)
    assert media_bindings.follow_store(stored)
    assert side_row(stored, rel) is None
    assert media_bindings.head_total(stored).used_bytes == 30


def test_a_flagged_page_stops_eviction_and_names_itself_in_the_drain_without_a_rebuild(
        stored, selected_store, store_thread, monkeypatch):
    """Catches a custody fault that eviction trusts past, or that `recover` answers with a rebuild loop."""
    from test_media_policy_bindings import _policy

    values, config = selected_store
    config.write_text(json.dumps({"vaults": {str(stored): {**values, media_protection.POLICY_FIELD: _policy()}}}))
    assert media_bindings.recover(stored)
    with closing(media_jobs.MediaJobStore(stored)._connect(readonly=True)) as conn:
        assert media_bindings.policy_keys(stored, conn, limit=4) is not None
    rel = f"{NOTES}/unparsed.bin.md"
    put_page(store_thread, rel, UNPARSED)
    with monkeypatch.context() as no_rebuild:
        no_rebuild.setattr(media_bindings, "rebuild", lambda _root: pytest.fail("a flagged row started a rebuild"))
        assert not media_bindings.recover(stored)

    with closing(media_jobs.MediaJobStore(stored)._connect(readonly=True)) as conn:
        assert media_bindings.current(stored, conn, kind=media_bindings.ALLOWANCE)
        assert not media_bindings.current(stored, conn, kind=media_bindings.EVICTION)
        assert media_bindings.policy_keys(stored, conn, limit=4) is None
    with pytest.raises(media_store.MediaStoreError):
        media_store.drain_census(stored)
    assert media_store.drain_record(stored).blocked_by == (rel,)


def test_an_unknown_unbound_reference_does_not_stop_eviction(stored, selected_store, store_thread):
    assert media_bindings.recover(stored)
    path = stored_original(stored, store_thread)
    assert media_store.offload(stored, path) is not None
    note = f"{NOTES}/broken-unbound-reference.md"
    put_page(store_thread, note, b"---\nstored_object:\n  schema: 1\n---\n")

    assert media_bindings.follow_store(stored)
    assert media_bindings.head_total(stored).state == media_bindings.UNKNOWN
    assert media_store.evict(stored, path)
    assert not path.exists()


def test_a_page_moved_away_from_its_original_withdraws_the_projection(stored, selected_store, store_thread):
    """Catches a displaced reference that the follow counts or drops without the ledger's confirmation."""
    assert media_bindings.recover(stored)
    companion = f"{NOTES}/elsewhere.md"
    put_page(store_thread, companion, counted_page(f"{NOTES}/kept.bin", 5, "v1"))

    media_bindings.repair_paths(stored, [companion])

    assert not checkpoint_of(stored).complete
    assert media_bindings.rebuild(stored)
    row = side_row(stored, companion)
    assert (row["counted"], row["flagged"]) == (0, 1)


def test_a_transient_ledger_read_failure_leaves_the_custody_projection_to_follow(
        stored, selected_store, store_thread, monkeypatch):
    """A failed ledger read leaves the old checkpoint complete so the next follow can advance it."""
    allow(selected_store, stored, 100)
    assert media_bindings.recover(stored)
    before = checkpoint_of(stored)
    original = f"{NOTES}/from-the-next-follow.bin"
    companion = f"{original}.md"
    put_page(store_thread, companion, counted_page(original, 30, "followed-version"))

    def unreadable(_conn):
        raise sqlite3.OperationalError("disk I/O error")

    with monkeypatch.context() as failing:
        failing.setattr(media_bindings, "checkpoint", unreadable)
        media_bindings.repair_paths(stored, [companion])
    assert checkpoint_of(stored) == before and lag_of(stored) > 0
    monkeypatch.setattr(media_bindings, "rebuild", lambda _root: pytest.fail("a read failure triggered a rebuild"))

    assert media_bindings.recover(stored)
    assert media_bindings.head_total(stored).used_bytes == 30


# Task 3.14: `recover` follows to the head that it read when its pass started.

def test_recover_follows_past_eight_rounds_to_the_head_it_read_and_leaves_later_saves_to_the_next_pass(
        stored, selected_store, store_thread, monkeypatch):
    """Catches a pass bounded at eight rounds (a backlog never caught up) and one that chases saves forever."""
    assert media_bindings.recover(stored)
    rows = media_bindings.REPLAY_ROWS * (media_bindings.FOLLOW_ROUNDS + 1) + 10
    bind(store_thread, range(rows), size=1)
    with read.snapshot(stored, join_request=False) as view:
        start = media_bindings._head(view)
    follow = media_bindings.follow
    rounds: list[int] = []

    def saves_land(root, **kwargs):
        rounds.append(1)
        if len(rounds) == 1:
            bind(store_thread, range(300), size=1, prefix="during-the-pass")
        return follow(root, **kwargs)

    monkeypatch.setattr(media_bindings, "follow", saves_land)
    monkeypatch.setattr(media_bindings, "rebuild", lambda _root: pytest.fail("lag started a rebuild"))

    media_bindings.recover(stored)

    assert len(rounds) > media_bindings.FOLLOW_ROUNDS
    # The pass ends with the round that reaches the head it read; the saves after that wait for the next pass.
    assert checkpoint_of(stored).seq >= start and lag_of(stored) > 0
    media_bindings.recover(stored)
    assert lag_of(stored) == 0


def test_a_new_allowance_rebuilds_the_projection_at_the_next_recover(stored, selected_store, store_thread):
    """Catches a projection that keeps counting under the policy it was built for after the owner changes it."""
    allow(selected_store, stored, 1_000_000)
    assert media_bindings.recover(stored)
    allow(selected_store, stored, 2_000_000)

    with closing(media_jobs.MediaJobStore(stored)._connect(readonly=True)) as conn:
        assert not media_bindings.current(stored, conn, kind=media_bindings.ALLOWANCE)
    assert media_bindings.recover(stored)
    assert checkpoint_of(stored).policy == media_bindings.policy_key(stored)


def test_a_pooled_vault_rebuilds_its_file_projection_after_migrating_to_store_mode(
        vault, selected_store, monkeypatch):
    """A file projection has no audit position that store-mode recovery can follow."""
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    original_pool.enable(vault)
    assert media_bindings.rebuild(vault)
    before = checkpoint_of(vault)
    assert before.complete and before.seq is None
    stop_writer()
    build_indexes(vault)

    assert migrate_apply(vault) == 0
    assert media_bindings.recover(vault)

    after = checkpoint_of(vault)
    assert after.complete and after.generation > before.generation
    assert after.seq == media_bindings.source_checkpoint(vault)[1]


def test_a_pending_access_edit_does_not_make_the_media_projection_rebuild(
        stored, selected_store, store_thread, monkeypatch):
    """The access reconciliation can stay pending while the media audit trail is current."""
    assert media_bindings.recover(stored)
    before = checkpoint_of(stored)
    watcher = file_watcher.FileWatcher(stored, importer=importer.Importer(stored, run=store_thread.run))
    policy = stored / "Knowledge Base/_access.yaml"
    policy.write_text("excluded: []\n", encoding="utf-8")
    watcher._record(policy, deleted=False)
    monkeypatch.setattr(watcher, "_recover_external_pending", lambda _epoch: None)
    watcher._reconcile_access_policy(watcher._pending_external_epoch)
    assert freshness.external_pending(stored)
    monkeypatch.setattr(media_bindings, "rebuild", lambda _root: pytest.fail("access reconcile triggered a rebuild"))

    assert media_bindings.recover(stored)
    assert checkpoint_of(stored) == before


# Task 3.11: what the batch census pinned, in store mode.

def test_each_offload_member_counts_both_the_previous_member_and_a_save_between_them(
        stored, selected_store, store_thread):
    first = stored / PICTURE
    second = first.with_name("second.png")
    second_body = PICTURE_BYTES + b" different bytes"
    first.write_bytes(PICTURE_BYTES)
    second.write_bytes(second_body)
    importer.Importer(stored, run=store_thread.run).import_paths([PICTURE, second.relative_to(stored).as_posix()])
    allow(selected_store, stored, len(PICTURE_BYTES) + len(second_body) + 10)
    assert media_bindings.recover(stored)
    batch = media_store.OffloadBatch(stored)
    assert media_store.offload(stored, first, batch=batch) is not None
    bind(store_thread, range(1), size=11, prefix="saved-between-members")

    with pytest.raises(media_store.MediaStoreError) as refused:
        media_store.offload(stored, second, batch=batch)

    assert refused.value.code == media_allowance.MEDIA_ALLOWANCE_EXCEEDED
    assert media_bindings.head_total(stored).used_bytes == len(PICTURE_BYTES) + 11


def test_a_status_of_allowance_refusals_reports_a_total_still_being_counted(stored, selected_store, store_thread):
    """Catches a status that shows a stale or zero total while the projection counts: it says it is counting."""
    allow(selected_store, stored, 1)
    assert media_bindings.recover(stored)
    path = stored_original(stored, store_thread)
    with pytest.raises(media_store.MediaStoreError):
        media_store.offload(stored, path)
    media_bindings.withdraw(stored)

    entries, _summary = media_store.offload_refusals(stored)

    allowance_entries = [entry for entry in entries if entry["code"] in media_allowance.ALLOWANCE_REFUSAL_CODES]
    assert allowance_entries and all(entry.get("counting") is True for entry in allowance_entries)
    assert media_store.take_census(stored).counting


def test_window_status_and_the_storage_record_use_the_current_projection(stored, selected_store, store_thread,
                                                                monkeypatch):
    from test_media_store import _window_policy, _window_status

    _window_policy(selected_store, monkeypatch)
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    assert media_bindings.recover(stored)
    bind(store_thread, range(1), size=30)
    assert media_bindings.follow_store(stored)
    stale = "2025-01-01T00:00:00+00:00"
    with closing(media_jobs.MediaJobStore(stored)._connect()) as conn, conn:
        conn.execute("INSERT INTO media_owner_census(singleton, used_bytes, observed_at) VALUES (1, 80, ?)", (stale,))
    monkeypatch.setattr(media_allowance, "reference_usage", lambda *_args: pytest.fail("store mode walked the vault"))
    now = datetime.now(UTC)

    status = _window_status(stored, series_id="test-series", next_window_bytes=1)["media_allowance"]
    assert status["used_bytes"] == 30 and status["observed_at"] > stale
    assert media_storage_record.refresh(stored, now=now)
    assert media_storage_record.read_record(stored).media_used_bytes == 30

    put_page(store_thread, f"{NOTES}/broken-use.md", b"---\nstored_object:\n  schema: 1\n---\n")
    assert media_bindings.follow_store(stored)
    status = _window_status(stored, series_id="test-series", next_window_bytes=1)["media_allowance"]
    assert "used_bytes" not in status and status["unknown"]
    assert media_storage_record.refresh(stored, now=now + timedelta(seconds=media_storage_record.REFRESH_SECONDS))
    written = media_storage_record.read_record(stored)
    assert written.media_used_bytes is None and written.media_observed_at is None
