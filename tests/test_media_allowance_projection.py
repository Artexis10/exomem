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
import sqlite3
import threading
from contextlib import closing, contextmanager

import pytest
import test_vault_store_media as store_media_suite
from test_agent_upload_handoff import _prepare
from test_agent_upload_handoff import upload_env as upload_env
from test_vault_store_importer import StoreThread
from test_vault_store_media import PICTURE, PICTURE_BYTES, serve_through
from test_vault_store_projector import NOTES

from exomem import (
    file_watcher,
    media_allowance,
    media_bindings,
    media_jobs,
    media_store,
    mutation_lock,
    preserve,
    server_transfer,
    trash_metadata,
)
from exomem.governance.principal import library_scope
from exomem.vault_store import importer, read, write

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
    with closing(media_jobs.MediaJobStore(root)._connect(readonly=True)) as conn:
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


def test_an_upload_past_the_suffix_bound_is_answered_busy_until_the_projection_follows(
        stored, selected_store, store_thread):
    """Catches an upload admitted on lag: past 2,048 unfollowed rows the total is not known, so the upload must
    wait for the follow, with a retry hint, and then decide on the exact total."""
    allow(selected_store, stored, 100_000)
    assert media_bindings.recover(stored)
    rows = media_bindings.UPLOAD_SUFFIX_ROWS + 4
    bind(store_thread, range(rows), size=40)

    busy = media_store.upload_refusal(stored, 10, "clip.mp4", "source")
    answer = asyncio.run(server_transfer._allowance_refused(stored, 10, "clip.mp4", "source"))

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
        return upload(*args, **kwargs)

    monkeypatch.setattr(media_store.FilesystemStore, "upload", saves_land)

    assert media_store.offload(stored, path) is not None
    assert replayed, "no hold counted the saves that landed before it"
    assert max(replayed) <= media_bindings.REPLAY_ROWS


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


def test_a_note_the_projection_cannot_count_refuses_offload_by_name_until_a_save_fixes_it(
        stored, selected_store, store_thread, monkeypatch):
    """Catches an uncountable note that the total skips (an offload past the allowance) or that only a rebuild
    clears: the refusal names the note, and the save that fixes it makes the total known through the follow."""
    assert media_bindings.recover(stored)
    broken = f"{NOTES}/broken-reference.md"
    put_page(store_thread, broken, b"---\nstored_object:\n  schema: 1\n---\nAn owner's hand edit.\n")
    path = stored_original(stored, store_thread)

    with pytest.raises(media_store.MediaStoreError) as refused:
        media_store.offload(stored, path)

    assert refused.value.code == "MEDIA_ALLOWANCE_UNKNOWN"
    assert broken in json.dumps(getattr(refused.value, "details", None) or str(refused.value)) or broken in str(
        media_store.offload_refusals(stored))
    assert checkpoint_of(stored).complete
    monkeypatch.setattr(media_bindings, "rebuild", lambda _root: pytest.fail("the fix waited for a rebuild"))
    put_page(store_thread, broken, b"---\ntitle: fixed\n---\nAn owner's hand edit, fixed.\n")
    assert media_store.offload(stored, path) is not None


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


def test_a_flagged_page_stops_eviction_and_names_itself_in_the_drain_without_a_rebuild(
        stored, selected_store, store_thread, monkeypatch):
    """Catches a custody fault that eviction trusts past, or that `recover` answers with a rebuild loop."""
    assert media_bindings.recover(stored)
    rel = f"{NOTES}/unparsed.bin.md"
    put_page(store_thread, rel, UNPARSED)
    with monkeypatch.context() as no_rebuild:
        no_rebuild.setattr(media_bindings, "rebuild", lambda _root: pytest.fail("a flagged row started a rebuild"))
        assert not media_bindings.recover(stored)

    with closing(media_jobs.MediaJobStore(stored)._connect(readonly=True)) as conn:
        assert media_bindings.current(stored, conn, kind=media_bindings.ALLOWANCE)
        assert not media_bindings.current(stored, conn, kind=media_bindings.EVICTION)
    with pytest.raises(media_store.MediaStoreError):
        media_store.drain_census(stored)
    assert media_store.drain_record(stored).blocked_by == (rel,)


def test_a_page_moved_away_from_its_original_withdraws_the_projection(stored, selected_store, store_thread):
    """Catches a displaced reference that the follow counts or drops without the ledger's confirmation."""
    assert media_bindings.recover(stored)
    put_page(store_thread, f"{NOTES}/elsewhere.md", counted_page(f"{NOTES}/kept.bin", 5, "v1"))

    followed = media_bindings.follow(stored)

    assert followed.state == media_bindings.REBUILD
    assert not checkpoint_of(stored).complete


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


# Task 3.11: what the batch census pinned, in store mode.

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
