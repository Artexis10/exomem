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
def test_a_deferred_rename_does_not_transfer_custody_after_either_witness_changes(
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
        replacement = (root / _DESTINATION).with_suffix(".replacement")
        replacement.write_bytes(_CONTENT.encode())
        replacement.replace(root / _DESTINATION)

    restarted = importer.Importer(root, run=lambda work: work(writer))
    with library_scope():
        stale = restarted.sweep()
    assert not stale.moved
    assert stale.unjudged[_DESTINATION] == importer.CHANGED_WHILE_READ
    assert not read.page_exists(root, _DESTINATION)
    assert read.page_exists(root, _SOURCE)


def test_the_pair_is_durable_before_the_floor_refusal_reaches_the_importer(
    outside_rename, writer, monkeypatch,  # noqa: F811
):
    root, _free, watcher = outside_rename

    class Stopped(BaseException):
        pass

    commit = write.commit

    def stop_at_refusal(active_writer, change):
        try:
            return commit(active_writer, change)
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
