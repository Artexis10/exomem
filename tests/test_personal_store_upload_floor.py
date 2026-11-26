"""A stored Evidence original must leave capacity for its canonical and projected copies.

In store mode the commit meters the page; the rows derived from it wait in the derivation queue,
with the reason ``no_room``, until the vault has room for them.
"""

import json
import string
from contextlib import closing
from itertools import islice, product
from types import SimpleNamespace

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import vault  # noqa: F401
from test_vault_store_projector import projecting  # noqa: F401
from test_vault_store_write_path import served, stored, writer  # noqa: F401

from exomem import lexstore, media_capacity, move_file, preserve, writer_lease
from exomem.cli_ops import OpError
from exomem.collection_store import connection
from exomem.governance.principal import library_scope
from exomem.vault_store import derivation, read, write


def queued(writer, rel):  # noqa: F811
    """The reason codes that keep ``rel``'s derived rows waiting: empty once they are derived."""
    return writer.connection.execute(
        "SELECT failed_code FROM store_derivation_queue WHERE path = ?", (rel,)
    ).fetchall()


def derive_again(root, writer):  # noqa: F811
    """Retry every waiting derivation now, as the store thread does when it starts."""
    derivation.started(root.resolve(), writer)
    derivation.drain(root.resolve(), writer)


def test_store_original_and_later_projection_each_obey_the_vault_floor(
    served, writer, projecting, tmp_path, monkeypatch,  # noqa: F811
):
    config = tmp_path / "media.json"
    floor = 1024**2
    config.write_text(json.dumps({"vaults": {str(served): {"store": "test", "upload_floor_bytes": floor}}}))
    monkeypatch.setenv("EXOMEM_MEDIA_STORE_CONFIG", str(config))
    database = connection.store_path(served)
    assert database.stat().st_dev == served.stat().st_dev
    unit = media_capacity._allocation_unit(served)
    free = [floor + unit - 1]
    monkeypatch.setattr(media_capacity.shutil, "disk_usage",
                        lambda _path: SimpleNamespace(total=100 * floor, free=free[0]))
    data = "# Original meeting notes\n\n" + "One recorded item.\n" * 7000
    target = "Knowledge Base/Evidence/Tests/Minutes/minutes.md"

    def file_original():
        with library_scope(), writer_lease.active_manager().mutation_guard(served, operation="test_preserve"):
            return preserve.preserve(served, scope="Tests", category="Minutes", filename="minutes.md", content=data)

    previous_revision = read.current_revision(served)
    with pytest.raises(OpError) as busy:
        file_original()
    assert busy.value.code == media_capacity.UPLOAD_SPACE_BUSY
    assert read.current_revision(served) == previous_revision
    assert not read.page_exists(served, target) and not (served / target).exists()

    with closing(connection.open_reader(database)) as reader:
        reader.execute("BEGIN")
        assert reader.execute("SELECT revision FROM store_mode").fetchone() == (previous_revision,)
        free[0] = floor + 16 * floor
        saved = file_original()
        assert saved.path == target and read.owned_bytes(served, target) == data.encode()

        page_size = writer.connection.execute("PRAGMA page_size").fetchone()[0]
        page_count = writer.connection.execute("PRAGMA page_count").fetchone()[0]
        checkpoint_growth = page_count * page_size - database.stat().st_size
        assert checkpoint_growth > 0  # The earlier real reader pins all of this commit's new pages in WAL.
        free[0] = floor + checkpoint_growth + unit - 1
        with media_capacity.upload_room(served, device=served.stat().st_dev) as room:
            assert room is not None
            with pytest.raises(OpError) as held:
                room.require(1)
            assert held.value.code == media_capacity.UPLOAD_SPACE_BUSY

        free[0] = floor + checkpoint_growth + len(data.encode())
        report = projecting.run_pass()
        assert report.deferred[target] == media_capacity.UPLOAD_SPACE_BUSY
        assert not (served / target).exists()
        assert read.owned_bytes(served, target) == data.encode()

        renamed = "Knowledge Base/Evidence/Tests/Minutes/archived.md"
        writer.connection.execute("PRAGMA secure_delete = ON")
        note = "Knowledge Base/Notes/Rename record.md"
        change = write.StoreChange(
            write.Origin.GOVERNED, "rename evidence", moves=(write.PageMove(target, renamed),),
            puts=(write.PagePut(note, b"# Rename record\n"),),
        )
        revision = read.current_revision(served)
        wal = database.with_name(database.name + "-wal")
        wal_size = wal.stat().st_size
        with pytest.raises(OpError) as moving:
            write.commit(writer, change)
        assert moving.value.code == media_capacity.UPLOAD_SPACE_BUSY
        assert read.current_revision(served) == revision and wal.stat().st_size == wal_size
        assert read.owned_bytes(served, target) == data.encode()
        assert not read.page_exists(served, renamed) and not read.page_exists(served, note)

        free[0] = floor + 32 * floor
        write.commit(writer, change)
        assert wal.stat().st_size > wal_size
        assert not read.page_exists(served, target) and not (served / target).exists()
        assert read.owned_bytes(served, renamed) == data.encode()
        assert read.owned_bytes(served, note) == b"# Rename record\n"
        page_count = writer.connection.execute("PRAGMA page_count").fetchone()[0]
        free[0] = floor + page_count * page_size - database.stat().st_size + len(data.encode())
        assert projecting.run_pass().deferred[renamed] == media_capacity.UPLOAD_SPACE_BUSY
        assert not (served / renamed).exists()

    free[0] = floor + 16 * floor
    with media_capacity.commit_credit("other-original"):
        with media_capacity.upload_room(served, device=served.stat().st_dev, reserve=True) as room:
            assert room is not None
            room.reserve_commit(floor, 2, incoming=0, offset=0)
    config.write_text(json.dumps({"vaults": {str(served): {"store": "test", "upload_floor_bytes": 0}}}))
    free[0] = 2 * floor + len(data.encode())
    assert projecting.run_pass().deferred[renamed] == media_capacity.UPLOAD_SPACE_BUSY
    assert not (served / renamed).exists()
    media_capacity.release_commit_credit(served, "other-original")
    free[0] = 16 * floor
    assert renamed not in projecting.run_pass().deferred
    assert (served / renamed).read_bytes() == data.encode()


def test_indexable_original_needs_room_for_its_search_rows(
    served, writer, tmp_path, monkeypatch,  # noqa: F811
):
    if not lexstore.fts5_available():
        pytest.skip("this SQLite build omits the full-text catalogue")
    config = tmp_path / "media.json"
    floor = 1024**2
    config.write_text(json.dumps({"vaults": {str(served): {"store": "test", "upload_floor_bytes": floor}}}))
    monkeypatch.setenv("EXOMEM_MEDIA_STORE_CONFIG", str(config))
    # This page's first batch takes seconds to fail; the request must try each page alone before it
    # acknowledges, which a one-second slice would cut short on a slow host.
    monkeypatch.setattr(derivation, "SLICE_SECONDS", 60.0)
    free = [floor + 6 * floor]
    monkeypatch.setattr(media_capacity.shutil, "disk_usage",
                        lambda _path: SimpleNamespace(total=128 * floor, free=free[0]))

    # The header is stored in both the page and its covering index. The body has
    # a large set of distinct trigrams and words for the two real FTS indexes.
    alphabet = string.ascii_lowercase + string.digits
    words = " ".join("".join(chars) for chars in islice(product(alphabet, repeat=4), 90_000))
    data = f"---\ntitle: Catalogue sample\nx-note: {words}\n---\n\n{words}\n"
    target = "Knowledge Base/Evidence/Tests/Minutes/catalogue.md"

    def catalogued():
        with closing(connection.open_reader(connection.store_path(served))) as reader:
            return reader.execute("SELECT pages.path FROM tri JOIN pages ON pages.rowid = tri.rowid "
                                  "WHERE tri MATCH ? AND pages.path = ?", ("a123", target)).fetchall()

    # The page fits above the floor and is stored; its search rows do not, so they wait for room.
    with library_scope(), writer_lease.active_manager().mutation_guard(served, operation="test_preserve"):
        assert preserve.preserve(served, scope="Tests", category="Minutes", filename="catalogue.md",
                                 content=data).path == target
    assert read.owned_bytes(served, target) == data.encode()
    assert queued(writer, target) == [(derivation.NO_ROOM,)] and catalogued() == []

    free[0] = 96 * floor
    with media_capacity.commit_credit("other-catalogue-original"):
        with media_capacity.upload_room(served, device=served.stat().st_dev, reserve=True) as room:
            assert room is not None
            room.reserve_commit(90 * floor, 1, incoming=0, offset=0)
    config.write_text(json.dumps({"vaults": {str(served): {"store": "test", "upload_floor_bytes": 0}}}))
    derive_again(served, writer)
    assert queued(writer, target) == [(derivation.NO_ROOM,)] and catalogued() == []

    media_capacity.release_commit_credit(served, "other-catalogue-original")
    derive_again(served, writer)
    assert queued(writer, target) == [] and catalogued() == [(target,)]


def test_public_move_of_a_pending_original_meters_its_lineage_rewrite(
    served, writer, projecting, tmp_path, monkeypatch,  # noqa: F811
):
    config = tmp_path / "media.json"
    floor = 1024**2
    config.write_text(json.dumps({"vaults": {str(served): {"store": "test", "upload_floor_bytes": floor}}}))
    monkeypatch.setenv("EXOMEM_MEDIA_STORE_CONFIG", str(config))
    free = [floor + 32 * floor]
    monkeypatch.setattr(media_capacity.shutil, "disk_usage",
                        lambda _path: SimpleNamespace(total=100 * floor, free=free[0]))
    database = connection.store_path(served)
    original = "---\ntitle: Survey notes\n---\n\n" + "One survey observation.\n" * 800
    source = "Knowledge Base/Evidence/Tests/Minutes/survey.md"
    destination = "Knowledge Base/Evidence/Tests/Minutes/archived-survey.md"

    def rename():
        with library_scope():
            return move_file.move_file(served, old_path=source, new_path=destination, update_wikilinks=False)

    with closing(connection.open_reader(database)) as reader:
        reader.execute("BEGIN")
        assert reader.execute("SELECT revision FROM store_mode").fetchone() is not None
        with library_scope(), writer_lease.active_manager().mutation_guard(served, operation="test_preserve"):
            saved = preserve.preserve(served, scope="Tests", category="Minutes", filename="survey.md", content=original)
        assert saved.path == source and not (served / source).exists()
        revision = read.current_revision(served)
        page_size = writer.connection.execute("PRAGMA page_size").fetchone()[0]
        page_count = writer.connection.execute("PRAGMA page_count").fetchone()[0]
        free[0] = floor + page_count * page_size - database.stat().st_size
        with pytest.raises(OpError) as busy:
            rename()
        assert busy.value.code == media_capacity.UPLOAD_SPACE_BUSY
        assert read.current_revision(served) == revision and not read.page_exists(served, destination)
        assert read.owned_bytes(served, source) == original.encode()

        free[0] = floor + 32 * floor
        assert rename().new_path == destination
        moved = read.owned_bytes(served, destination)
        assert moved != original.encode() and source.encode() in moved
        assert b"One survey observation." in moved and not read.page_exists(served, source)
        page_count = writer.connection.execute("PRAGMA page_count").fetchone()[0]
        free[0] = floor + page_count * page_size - database.stat().st_size
        assert projecting.run_pass().deferred[destination] == media_capacity.UPLOAD_SPACE_BUSY
        assert not (served / source).exists() and not (served / destination).exists()
        assert read.owned_bytes(served, destination) == moved

    free[0] = floor + 32 * floor
    assert destination not in projecting.run_pass().deferred
    assert (served / destination).read_bytes() == moved


def test_unindexed_move_reserves_wal_for_retiring_the_source_catalogue(
    served, writer, tmp_path, monkeypatch,  # noqa: F811
):
    config = tmp_path / "media.json"
    floor = 1024**2
    config.write_text(json.dumps({"vaults": {str(served): {"store": "test", "upload_floor_bytes": floor}}}))
    monkeypatch.setenv("EXOMEM_MEDIA_STORE_CONFIG", str(config))
    monkeypatch.setattr(lexstore, "fts5_available", lambda: False)
    free = [floor + 40 * floor]
    monkeypatch.setattr(media_capacity.shutil, "disk_usage",
                        lambda _path: SimpleNamespace(total=256 * floor, free=free[0]))
    original = b"# Field notes\n\n" + b"- [rule] Confirm the valve.\n" * 610
    source = "Knowledge Base/Evidence/Tests/Minutes/field.md"
    destination = "Knowledge Base/_archive/field.md"
    assert not any(lexstore.page_membership(destination))
    write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "file original",
                                           puts=(write.PagePut(source, original, media_allocation=True),)))
    assert read.owned_bytes(served, source) == original
    assert queued(writer, source) == [(derivation.NO_ROOM,)]

    free[0] = floor + 128 * floor
    derive_again(served, writer)
    assert queued(writer, source) == []
    database = connection.store_path(served)
    wal = database.with_name(database.name + "-wal")
    writer.connection.execute("PRAGMA secure_delete = ON")
    assert writer.handle.checkpoint()
    change = write.StoreChange(write.Origin.GOVERNED, "archive original",
                               moves=(write.PageMove(source, destination),))
    headroom = 24 * floor
    with closing(connection.open_reader(database)) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT revision FROM store_mode").fetchone()
        (structure_size,) = reader.execute(
            "SELECT length(CAST(structure_json AS BLOB)) FROM pages WHERE path = ?", (source,),
        ).fetchone()
        (graph_size,) = reader.execute(
            "SELECT MAX(length(CAST(metadata AS BLOB))) FROM graph_nodes WHERE path = ?", (source,),
        ).fetchone()
        assert structure_size + graph_size > headroom
        assert reader.execute("SELECT COUNT(*) FROM semantic_units WHERE parent_path = ?", (source,)).fetchone() == (610,)
        wal_size = wal.stat().st_size
        free[0] = floor + headroom
        write.commit(writer, change)
        assert read.owned_bytes(served, destination) == original and not read.page_exists(served, source)
        assert queued(writer, source) == [(derivation.NO_ROOM,)]
        assert writer.connection.execute("SELECT COUNT(*) FROM pages WHERE path = ?", (source,)).fetchone() == (1,)

        free[0] = floor + 128 * floor
        derive_again(served, writer)
        assert wal.stat().st_size - wal_size > headroom
        with closing(connection.open_reader(database)) as current:
            assert current.execute("SELECT path FROM pages WHERE path IN (?, ?)", (source, destination)).fetchall() == []


def test_original_reserves_resolved_graph_records_before_writing_them(
    served, writer, tmp_path, monkeypatch,  # noqa: F811
):
    config = tmp_path / "media.json"
    floor = 1024**2
    config.write_text(json.dumps({"vaults": {str(served): {"store": "test", "upload_floor_bytes": floor}}}))
    monkeypatch.setenv("EXOMEM_MEDIA_STORE_CONFIG", str(config))
    monkeypatch.setattr(lexstore, "fts5_available", lambda: False)
    free = [floor + 4 * floor]
    monkeypatch.setattr(media_capacity.shutil, "disk_usage",
                        lambda _path: SimpleNamespace(free=free[0]))
    folder = "Knowledge Base/Notes/" + "/".join(
        f"catalogue-section-{index:02}-" + "region-" * 9 for index in range(31)
    )
    targets = tuple(f"Station-{index:03}" for index in range(420))
    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "prepare catalogue targets",
        puts=tuple(write.PagePut(f"{folder}/{name}.md", f"# {name}\n".encode()) for name in targets),
    ))
    original = ("# Field directory\n\n" + "\n".join(f"- [[{name}]]" for name in targets)).encode()
    source = "Knowledge Base/Evidence/Tests/Minutes/directory.md"
    change = write.StoreChange(write.Origin.GOVERNED, "file directory",
                               puts=(write.PagePut(source, original, media_allocation=True),))
    database = connection.store_path(served)
    wal = database.with_name(database.name + "-wal")
    assert writer.handle.checkpoint()
    with closing(connection.open_reader(database)) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT revision FROM store_mode").fetchone()
        original_pages, = reader.execute("PRAGMA page_count").fetchone()
        page_size, = reader.execute("PRAGMA page_size").fetchone()
        wal_size = wal.stat().st_size
        # The page fits above the floor; its resolved graph rows do not, and none is written.
        write.commit(writer, change)
        assert read.owned_bytes(served, source) == original
        assert queued(writer, source) == [(derivation.NO_ROOM,)]
        assert writer.connection.execute(
            "SELECT COUNT(*) FROM graph_edges WHERE source_path = ?", (source,),
        ).fetchone() == (0,)

        free[0] = floor + 256 * floor
        derive_again(served, writer)
        count, longest = writer.connection.execute(
            "SELECT COUNT(*), MAX(length(dst_key)) FROM graph_edges WHERE source_path = ?", (source,),
        ).fetchone()
        assert count == len(targets) and longest > 2000
        growth = (writer.connection.execute("PRAGMA page_count").fetchone()[0] - original_pages) * page_size
        assert growth + wal.stat().st_size - wal_size > 4 * floor


def test_a_small_note_needs_room_for_itself_not_for_the_whole_store(
    served, writer, tmp_path, monkeypatch,  # noqa: F811
):
    """Catches a graph reservation that grows with the store database instead of with the note."""
    config = tmp_path / "media.json"
    floor = 1024**2
    config.write_text(json.dumps({"vaults": {str(served): {"store": "test", "upload_floor_bytes": floor}}}))
    monkeypatch.setenv("EXOMEM_MEDIA_STORE_CONFIG", str(config))
    # A large store database: only its page count matters to the reservation.
    writer.connection.execute("CREATE TABLE padding(x BLOB)")
    for _ in range(4):
        writer.connection.execute("INSERT INTO padding VALUES (zeroblob(16 * 1024 * 1024))")
    writer.connection.commit()
    assert writer.handle.checkpoint()
    free = [floor + 32 * floor]
    monkeypatch.setattr(media_capacity.shutil, "disk_usage", lambda _path: SimpleNamespace(free=free[0]))
    note = "# Field notes\n\n" + "".join(f"- [rule] Confirm valve {index} before the shift.\n" for index in range(100))
    target = "Knowledge Base/Evidence/Tests/Minutes/valves.md"

    write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "file note",
                                           puts=(write.PagePut(target, note.encode(), media_allocation=True),)))

    assert read.owned_bytes(served, target) == note.encode()


def test_a_move_needs_room_for_the_leaves_its_source_records_leave(
    served, writer, tmp_path, monkeypatch,  # noqa: F811
):
    """Catches a move that reserves the source's overflow pages but not the leaves its deleted rows rewrite."""
    config = tmp_path / "media.json"
    floor = 1024**2
    config.write_text(json.dumps({"vaults": {str(served): {"store": "test", "upload_floor_bytes": floor}}}))
    monkeypatch.setenv("EXOMEM_MEDIA_STORE_CONFIG", str(config))
    monkeypatch.setattr(lexstore, "fts5_available", lambda: False)
    free = [floor + 1024 * floor]
    monkeypatch.setattr(media_capacity.shutil, "disk_usage", lambda _path: SimpleNamespace(free=free[0]))
    note = ("# Field notes\n\n" + "".join(f"- [rule] Confirm valve {index} before the shift.\n"
                                           for index in range(3000))).encode()
    first, second = "Knowledge Base/Evidence/Tests/Minutes/first.md", "Knowledge Base/Evidence/Tests/Minutes/second.md"
    for path in (first, second):
        write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "file note",
                                               puts=(write.PagePut(path, note, media_allocation=True),)))
    database = connection.store_path(served)
    wal = database.with_name(database.name + "-wal")
    assert writer.handle.checkpoint()
    with closing(connection.open_reader(database)) as reader:
        reader.execute("BEGIN")  # A pinned reader keeps every frame of both moves in the WAL.
        reader.execute("SELECT revision FROM store_mode").fetchone()
        page_size, = reader.execute("PRAGMA page_size").fetchone()
        with monkeypatch.context() as patch:
            patch.setattr(derivation, "drain", lambda *_arguments, **_keywords: None)
            write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "archive first",
                                                   moves=(write.PageMove(first, "Knowledge Base/_archive/first.md"),)))
        pages = writer.connection.execute("PRAGMA page_count").fetchone()[0]
        wal_size = wal.stat().st_size
        derivation.drain(served.resolve(), writer)
        # What the drain wrote for the first move's rows: its source's deleted rows and their leaves.
        written = ((writer.connection.execute("PRAGMA page_count").fetchone()[0] - pages) * page_size
                   + wal.stat().st_size - wal_size)

        free[0] = floor + written - media_capacity._allocation_unit(served)
        write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "archive second",
                                               moves=(write.PageMove(second, "Knowledge Base/_archive/second.md"),)))
        assert queued(writer, second) == [(derivation.NO_ROOM,)]
        assert writer.connection.execute("SELECT COUNT(*) FROM pages WHERE path = ?", (second,)).fetchone() == (1,)


def test_an_original_needs_room_for_the_pages_that_link_to_it(
    served, writer, tmp_path, monkeypatch,  # noqa: F811
):
    """Catches a filing that reserves its own graph rows but not the linking pages it re-derives."""
    config = tmp_path / "media.json"
    floor = 1024**2
    config.write_text(json.dumps({"vaults": {str(served): {"store": "test", "upload_floor_bytes": floor}}}))
    monkeypatch.setenv("EXOMEM_MEDIA_STORE_CONFIG", str(config))
    free = [floor + 1024 * floor]
    monkeypatch.setattr(media_capacity.shutil, "disk_usage", lambda _path: SimpleNamespace(free=free[0]))
    for target in ("north-survey", "south-survey"):
        body = "".join(f"- [rule] Check gauge {index} against [[{target}]].\n" for index in range(30))
        write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "linking notes", puts=tuple(
            write.PagePut(f"Knowledge Base/Notes/Surveys/{target}-{note:03}.md", f"# Note {note}\n\n{body}".encode())
            for note in range(200))))
    database = connection.store_path(served)
    wal = database.with_name(database.name + "-wal")
    assert writer.handle.checkpoint()

    def file_survey(name: str) -> str:
        path = f"Knowledge Base/Evidence/Tests/Surveys/{name}.md"
        write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "file survey", puts=(write.PagePut(
            path, f"# {name}\n\nRaw readings.\n".encode(), media_allocation=True),)))
        return path

    with closing(connection.open_reader(database)) as reader:
        reader.execute("BEGIN")  # A pinned reader keeps every frame of both filings in the WAL.
        reader.execute("SELECT revision FROM store_mode").fetchone()
        page_size, = reader.execute("PRAGMA page_size").fetchone()
        with monkeypatch.context() as patch:
            patch.setattr(derivation, "drain", lambda *_arguments, **_keywords: None)
            file_survey("north-survey")
        pages = writer.connection.execute("PRAGMA page_count").fetchone()[0]
        wal_size = wal.stat().st_size
        derivation.drain(served.resolve(), writer)
        written = ((writer.connection.execute("PRAGMA page_count").fetchone()[0] - pages) * page_size
                   + wal.stat().st_size - wal_size)
        assert written > 8 * floor  # the 200 linking pages re-derive their graph rows

        free[0] = floor + written - media_capacity._allocation_unit(served)
        south = file_survey("south-survey")
        assert read.page_exists(served, south)
        assert queued(writer, south) == [(derivation.NO_ROOM,)]


def test_an_original_draws_no_room_for_the_other_pages_its_commit_writes(
    served, writer, tmp_path, monkeypatch,  # noqa: F811
):
    """Catches a drain that charges a commit's unrelated notes to its original's room.

    The charge fails the commit's batch, and its 300 notes then derive one transaction at a time.
    """
    config = tmp_path / "media.json"
    floor = 1024**2
    config.write_text(json.dumps({"vaults": {str(served): {"store": "test", "upload_floor_bytes": floor}}}))
    monkeypatch.setenv("EXOMEM_MEDIA_STORE_CONFIG", str(config))
    # Room for the original's own commit and derived rows, not for the graph rows of the notes.
    free = [floor + 6 * floor]
    monkeypatch.setattr(media_capacity.shutil, "disk_usage", lambda _path: SimpleNamespace(free=free[0]))
    notes = tuple(
        write.PagePut(
            f"Knowledge Base/Notes/Gauges/gauge-{note:03}.md",
            (f"# Gauge {note}\n\n" + "".join(
                f"- [rule] Check gauge {note} against [[station-{note:03}-{index:02}-{'calibration-' * 20}]].\n"
                for index in range(30)
            )).encode(),
        )
        for note in range(300)
    )
    original = "Knowledge Base/Evidence/Tests/Gauges/calibration.md"
    with monkeypatch.context() as patch:
        patch.setattr(derivation, "drain", lambda *_arguments, **_keywords: None)
        write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "file calibration with notes", puts=(
            write.PagePut(original, b"# Calibration\n\nRaw readings.\n", media_allocation=True), *notes,
        )))

    drained = derivation.drain(served.resolve(), writer)

    assert (drained.batches, drained.failed) == (1, 0)
    assert queued(writer, original) == []
    assert writer.connection.execute(
        "SELECT COUNT(*) FROM store_derivation_queue WHERE path LIKE 'Knowledge Base/Notes/Gauges/%'"
    ).fetchone() == (0,)


def test_a_second_move_needs_room_for_the_pages_that_link_to_its_first_path(
    served, writer, projecting, tmp_path, monkeypatch,  # noqa: F811
):
    """Catches a move that charges the pages linking to the path it leaves, but not the ones that
    reach the original through its lineage and re-derive all the same."""
    config = tmp_path / "media.json"
    floor = 1024**2
    config.write_text(json.dumps({"vaults": {str(served): {"store": "test", "upload_floor_bytes": floor}}}))
    monkeypatch.setenv("EXOMEM_MEDIA_STORE_CONFIG", str(config))
    free = [floor + 1024 * floor]
    monkeypatch.setattr(media_capacity.shutil, "disk_usage",
                        lambda _path: SimpleNamespace(total=4096 * floor, free=free[0]))
    folder = "Knowledge Base/Evidence/Tests/Minutes"
    for name in ("north-readings", "south-readings"):
        with library_scope(), writer_lease.active_manager().mutation_guard(served, operation="test_preserve"):
            preserve.preserve(served, scope="Tests", category="Minutes", filename=f"{name}.md",
                              content="---\ntitle: Gauge log\n---\n\nRaw readings.\n")
        # The move records lineage in the frontmatter, and the title differs from the first stem, so the
        # notes reach the original through its lineage alone.
        body = "".join(f"- [rule] Check gauge {index} against [[{name}]].\n" for index in range(30))
        write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "linking notes", puts=tuple(
            write.PagePut(f"Knowledge Base/Notes/Readings/{name}-{note:03}.md", f"# Note {note}\n\n{body}".encode())
            for note in range(200))))

    def move(old: str, new: str) -> None:
        with library_scope():
            move_file.move_file(served, old_path=f"{folder}/{old}.md", new_path=f"{folder}/{new}.md",
                                update_wikilinks=False)

    move("north-readings", "kept-north-readings")
    move("south-readings", "kept-south-readings")
    database = connection.store_path(served)
    wal = database.with_name(database.name + "-wal")
    assert writer.handle.checkpoint()
    with closing(connection.open_reader(database)) as reader:
        reader.execute("BEGIN")  # A pinned reader keeps every frame of both moves in the WAL.
        reader.execute("SELECT revision FROM store_mode").fetchone()
        page_size, = reader.execute("PRAGMA page_size").fetchone()
        with monkeypatch.context() as patch:
            patch.setattr(derivation, "drain", lambda *_arguments, **_keywords: None)
            move("kept-north-readings", "archived-north-readings")
        pages = writer.connection.execute("PRAGMA page_count").fetchone()[0]
        wal_size = wal.stat().st_size
        derivation.drain(served.resolve(), writer)
        written = ((writer.connection.execute("PRAGMA page_count").fetchone()[0] - pages) * page_size
                   + wal.stat().st_size - wal_size)
        assert written > 8 * floor  # the 200 notes that name the first path re-derive their graph rows

        free[0] = floor + written - media_capacity._allocation_unit(served)
        move("kept-south-readings", "archived-south-readings")
        south = f"{folder}/archived-south-readings.md"
        assert read.page_exists(served, south)
        assert queued(writer, south) == [(derivation.NO_ROOM,)]
