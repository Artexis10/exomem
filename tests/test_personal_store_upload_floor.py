"""A stored Evidence original must leave capacity for its canonical and projected copies."""

import json
from contextlib import closing
from types import SimpleNamespace

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import vault  # noqa: F401
from test_vault_store_projector import projecting  # noqa: F401
from test_vault_store_write_path import served, stored, writer  # noqa: F401

from exomem import media_capacity, preserve, writer_lease
from exomem.cli_ops import OpError
from exomem.collection_store import connection
from exomem.governance.principal import library_scope
from exomem.vault_store import read


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

    free[0] = floor + 16 * floor
    assert target not in projecting.run_pass().deferred
    assert (served / target).read_bytes() == data.encode()
