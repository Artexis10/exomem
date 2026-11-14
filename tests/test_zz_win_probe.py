"""TEMPORARY Windows probe, deleted before the change ends: SQLite beside a held handle."""

from __future__ import annotations

import json
import sqlite3
import sys

import pytest

from exomem import held_fs

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows probe")


def _sqlite(path, wal=False):
    try:
        conn = sqlite3.connect(str(path), timeout=0, isolation_level=None)
        try:
            if wal:
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS t(x)")
            conn.execute("INSERT INTO t VALUES (1)")
        finally:
            conn.close()
        return "ok"
    except Exception as error:  # noqa: BLE001 - probe
        return f"{type(error).__name__}: {error}"


def test_probe(tmp_path):
    out = {"sqlite": sqlite3.sqlite_version}
    filesystem = held_fs.acquire(tmp_path).require()
    (tmp_path / "work").mkdir()
    with filesystem.parent("work", access="mutate").require() as parent:
        for access in ("read", "identity", "write", "mutate"):
            for wal in (False, True):
                leaf = f"{access}-{int(wal)}.sqlite"
                (tmp_path / "work" / leaf).write_bytes(b"")
                held = filesystem.file(parent, leaf, access=access)
                if not held.ok:
                    out[f"{access}/wal={wal}"] = f"hold failed {held.error!r}"
                    continue
                with held.value:
                    out[f"{access}/wal={wal}"] = _sqlite(tmp_path / "work" / leaf, wal)
        # The migration's own shape: the held handle creates the file, with delete authority.
        created = filesystem.file(parent, "created.sqlite", create=True, exclusive=True, access="mutate")
        with created.require():
            out["created-mutate"] = _sqlite(tmp_path / "work" / "created.sqlite")
        created = filesystem.file(parent, "created-read.sqlite", create=True, exclusive=True)
        with created.require() as read_handle:
            out["created-read"] = _sqlite(tmp_path / "work" / "created-read.sqlite")
            reopened = filesystem.file(parent, "created-read.sqlite", access="mutate")
            out["mutate-reopen-while-read-held"] = "ok" if reopened.ok else repr(reopened.error)
            if reopened.ok:
                with reopened.value as mutable:
                    out["same-identity"] = held_fs._same_file_identity(read_handle.identity, mutable.identity)
        # The other direction: SQLite holds the file first, then a delete-capable handle asks for it.
        (tmp_path / "work" / "first.sqlite").write_bytes(b"")
        conn = sqlite3.connect(str(tmp_path / "work" / "first.sqlite"), isolation_level=None)
        conn.execute("CREATE TABLE t(x)")
        try:
            late = filesystem.file(parent, "first.sqlite", access="mutate")
            out["mutate-while-sqlite-open"] = "ok" if late.ok else repr(late.error)
            if late.ok:
                late.value.close()
        finally:
            conn.close()
        after = filesystem.file(parent, "first.sqlite", access="mutate")
        out["mutate-after-sqlite-closed"] = "ok" if after.ok else repr(after.error)
        if after.ok:
            with after.value as mutable:
                renamed = filesystem.rename(mutable, parent, "first-renamed.sqlite", replace=False)
                out["rename-by-handle"] = "ok" if renamed.ok else repr(renamed.error)
    pytest.fail("PROBE " + json.dumps(out, indent=1, sort_keys=True))
