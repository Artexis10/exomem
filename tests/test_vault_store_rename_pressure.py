"""An outside rename keeps its exact custody when the store's media floor refuses it."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import vault  # noqa: F401
from test_vault_store_projector import projecting  # noqa: F401
from test_vault_store_write_path import served, stored, writer  # noqa: F401

from exomem import file_watcher, media_capacity, preserve, writer_lease
from exomem.cli_ops import OpError
from exomem.governance.principal import library_scope
from exomem.vault_store import importer, read, write

_SOURCE = "Knowledge Base/Evidence/Tests/Minutes/original.md"
_DUPLICATE = "Knowledge Base/Evidence/Tests/Minutes/same-bytes.md"
_DESTINATION = "Knowledge Base/Evidence/Tests/Minutes/renamed.md"
_OTHER = "Knowledge Base/Notes/Independent.md"
_CONTENT = "# Observations\n\n" + "One shared observation.\n" * 800
_FLOOR = 1024 * 1024


@pytest.fixture
def outside_rename(served, writer, projecting, tmp_path, monkeypatch):  # noqa: F811
    config = tmp_path / "media.json"
    config.write_text(json.dumps({"vaults": {str(served): {"store": "test", "upload_floor_bytes": _FLOOR}}}))
    monkeypatch.setenv("EXOMEM_MEDIA_STORE_CONFIG", str(config))
    free = [_FLOOR + 32 * _FLOOR]
    monkeypatch.setattr(
        media_capacity.shutil, "disk_usage",
        lambda _path: SimpleNamespace(total=100 * _FLOOR, free=free[0]),
    )
    with library_scope(), writer_lease.active_manager().mutation_guard(served, operation="test_preserve"):
        assert preserve.preserve(served, scope="Tests", category="Minutes",
                                 filename="original.md", content=_CONTENT).path == _SOURCE
        assert preserve.preserve(served, scope="Tests", category="Minutes",
                                 filename="same-bytes.md", content=_CONTENT).path == _DUPLICATE
    assert not projecting.run_pass().deferred
    assert (served / _SOURCE).read_bytes() == (served / _DUPLICATE).read_bytes() == _CONTENT.encode()
    (served / _SOURCE).rename(served / _DESTINATION)
    (served / _DUPLICATE).unlink()
    (served / _OTHER).write_bytes(b"# Independent edit\n")

    running = importer.Importer(served, run=lambda work: work(writer))
    watcher = file_watcher.FileWatcher(served, importer=running)
    watcher._note_move(served / _SOURCE, served / _DESTINATION)
    for rel in (_SOURCE, _DUPLICATE, _DESTINATION, _OTHER):
        watcher._pend_import(rel)
    free[0] = _FLOOR
    return served, free, watcher


@pytest.fixture
def pressured_rename(outside_rename):
    root, free, watcher = outside_rename
    with library_scope():
        watcher._import_pending()

    assert read.owned_bytes(root, _OTHER) == b"# Independent edit\n"
    assert read.owned_bytes(root, _SOURCE) == _CONTENT.encode()
    assert not read.page_exists(root, _DESTINATION)
    return root, free


def test_the_exact_observed_rename_survives_a_fresh_importer_at_the_floor(pressured_rename, writer):  # noqa: F811
    root, free = pressured_rename
    # A new identical destination makes hash inference ambiguous after the restart.
    another = "Knowledge Base/Evidence/Tests/Minutes/independent-copy.md"
    (root / another).write_bytes(_CONTENT.encode())
    restarted = importer.Importer(root, run=lambda work: work(writer))
    with library_scope():
        source_only = restarted.import_paths([_SOURCE])
        refused = restarted.sweep()
    assert _SOURCE in source_only.unjudged and _SOURCE not in source_only.held
    assert refused.unjudged[_DESTINATION] == media_capacity.UPLOAD_SPACE_BUSY
    assert not read.page_exists(root, _DESTINATION)
    assert read.owned_bytes(root, another) == _CONTENT.encode()

    free[0] = _FLOOR + 32 * _FLOOR
    with library_scope():
        moved = restarted.sweep()
    assert moved.moved == {_DESTINATION: _SOURCE}
    assert not read.page_exists(root, _SOURCE)
    assert read.owned_bytes(root, _DESTINATION) == _CONTENT.encode()
    with read.snapshot(root) as view:
        audit = view.connection.execute("SELECT operation, reason FROM store_audit WHERE path = ?", (_DESTINATION,))
        assert audit.fetchall() == [("move", write.MEDIA_PAYLOAD_WRITE_REASON)]


@pytest.mark.parametrize("changed", ["source", "destination"])
def test_a_deferred_rename_does_not_transfer_custody_after_the_stored_revision_or_bytes_change(
    pressured_rename, writer, changed,  # noqa: F811
):
    root, free = pressured_rename
    free[0] = _FLOOR + 32 * _FLOOR
    if changed == "source":
        write.commit(writer, write.StoreChange(
            write.Origin.GOVERNED, "a new canonical revision",
            puts=(write.PagePut(_SOURCE, b"# New original revision\n"),),
        ))
    else:
        (root / _DESTINATION).write_bytes(b"# Different original\n")

    restarted = importer.Importer(root, run=lambda work: work(writer))
    with library_scope():
        stale = restarted.sweep()
    assert not stale.moved
    assert stale.unjudged[_DESTINATION] == importer.CHANGED_WHILE_READ
    assert not read.page_exists(root, _DESTINATION)
    assert read.page_exists(root, _SOURCE)


def test_a_same_bytes_save_keeps_the_destination_bound_to_admission(pressured_rename, writer):  # noqa: F811
    root, free = pressured_rename
    replacement = (root / _DESTINATION).with_suffix(".replacement")
    replacement.write_bytes(_CONTENT.encode())
    replacement.replace(root / _DESTINATION)

    restarted = importer.Importer(root, run=lambda work: work(writer))
    with library_scope():
        at_floor = restarted.sweep()
        destination_only = restarted.import_paths([_DESTINATION])
    assert not at_floor.moved
    assert _DESTINATION in destination_only.unjudged
    assert not read.page_exists(root, _DESTINATION)
    assert at_floor.unjudged[_DESTINATION] == media_capacity.UPLOAD_SPACE_BUSY

    free[0] = _FLOOR + 32 * _FLOOR
    with library_scope():
        settled = restarted.sweep()
    assert settled.moved == {_DESTINATION: _SOURCE}
    with read.snapshot(root) as view:
        audit = view.connection.execute("SELECT operation, reason FROM store_audit WHERE path = ?", (_DESTINATION,))
        assert audit.fetchall() == [("move", write.MEDIA_PAYLOAD_WRITE_REASON)]


@pytest.mark.parametrize("changed", ["destination_during_admission", "source_after_cache_prepare"])
def test_a_move_rolls_back_when_a_path_changes_after_initial_validation(
    pressured_rename, writer, monkeypatch, changed,  # noqa: F811
):
    root, free = pressured_rename
    free[0] = _FLOOR + 32 * _FLOOR
    sample = media_capacity.shutil.disk_usage
    cache = writer.handle.release_cache
    prepare = cache.prepare
    changes = []

    def replace_destination(path):
        if not changes:
            (root / _DESTINATION).write_bytes(b"# Editor saved after validation\n")
            changes.append(_DESTINATION)
        return sample(path)

    def restore_source():
        prepare()
        if not changes and writer.connection.execute(
            "SELECT 1 FROM store_pages WHERE path = ?", (_DESTINATION,),
        ).fetchone():
            (root / _SOURCE).write_bytes(_CONTENT.encode())
            changes.append(_SOURCE)

    restarted = importer.Importer(root, run=lambda work: work(writer))
    with monkeypatch.context() as race, library_scope():
        if changed == "destination_during_admission":
            race.setattr(media_capacity.shutil, "disk_usage", replace_destination)
        else:
            race.setattr(cache, "prepare", restore_source)
        result = restarted.sweep()
    assert len(changes) == 1
    assert not result.moved
    assert result.unjudged[_SOURCE] == result.unjudged[_DESTINATION] == importer.CHANGED_WHILE_READ
    assert read.owned_bytes(root, _SOURCE) == _CONTENT.encode()
    assert not read.page_exists(root, _DESTINATION)
    with read.snapshot(root) as view:
        assert not view.connection.execute(
            "SELECT 1 FROM store_manifest WHERE path = ?", (_DESTINATION,),
        ).fetchone()
    with library_scope():
        assert _DESTINATION in restarted.import_paths([_DESTINATION]).unjudged


def test_retry_lookup_cost_does_not_grow_with_the_unrelated_projection_queue(
    pressured_rename, writer, record_property,  # noqa: F811
):
    root, _free = pressured_rename

    def retry_work() -> int:
        steps = 0

        def count():
            nonlocal steps
            steps += 1
            return 0

        restarted = importer.Importer(root, run=lambda work: work(writer))
        report = importer.ImportReport()
        with library_scope(), read.request_snapshot(root), read.snapshot(root) as view, restarted._filesystem() as fs:
            view.connection.set_progress_handler(count, 1)
            try:
                restarted._retry_renames(fs, report)
            finally:
                view.connection.set_progress_handler(None, 0)
        assert report.unjudged[_DESTINATION] == media_capacity.UPLOAD_SPACE_BUSY
        assert not report.moved
        return steps

    small = retry_work()
    with writer.handle.transaction() as tx:
        tx.executemany(
            "INSERT INTO store_projection_queue(path, revision, operation, from_path, queued_at) "
            "VALUES (?, 1, 'move', ?, '2026-01-01')",
            ((f"queued/{n}.md", f"retired/{n}.md") for n in range(50_000)),
        )
    large = retry_work()
    record_property("retry_sqlite_steps", f"{small} steps before and {large} after inserting 50,000 queued moves")
    assert large <= small + 10_000, (small, large)


def test_the_pair_is_durable_before_the_floor_refusal_reaches_the_importer(
    outside_rename, writer, monkeypatch,  # noqa: F811
):
    root, _free, watcher = outside_rename

    class Stopped(BaseException):
        pass

    commit = write.commit

    def stop_at_refusal(active_writer, change, **options):
        try:
            return commit(active_writer, change, **options)
        except OpError as refusal:
            assert refusal.code == media_capacity.UPLOAD_SPACE_BUSY
            raise Stopped() from refusal

    with monkeypatch.context() as cut:
        cut.setattr(write, "commit", stop_at_refusal)
        with pytest.raises(Stopped), library_scope():
            watcher._import_pending()

    restarted = importer.Importer(root, run=lambda work: work(writer))
    with library_scope():
        report = restarted.sweep()
    assert report.unjudged[_DESTINATION] == media_capacity.UPLOAD_SPACE_BUSY
    assert not read.page_exists(root, _DESTINATION)
    assert read.owned_bytes(root, _SOURCE) == _CONTENT.encode()
    assert read.owned_bytes(root, _OTHER) == b"# Independent edit\n"
