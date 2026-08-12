from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows idempotency trust boundary")


def test_dpapi_receipt_envelope_round_trips_with_exact_header(tmp_path: Path) -> None:
    from exomem import writer_lease
    from exomem.writer_lease import IdempotencyStore

    store = IdempotencyStore(tmp_path / "state" / "idempotency.sqlite")
    digest = "a" * 64
    assert store._claim_or_inspect("protected", digest, None) == ("owner", None)
    attempt = store._attempts["protected"]
    with sqlite3.connect(store.path) as connection:
        stored = connection.execute(
            "SELECT commit_secret FROM mutations WHERE key = 'protected'"
        ).fetchone()[0]

    assert len(stored) <= 4096
    assert stored[:4] == b"EXID"
    assert stored[4:6] == b"\x01\x01"
    assert int.from_bytes(stored[6:10], "big") == len(stored[10:])
    recovered = writer_lease._ExecutionAttempt(
        attempt.attempt_id, attempt.commit_token, stored, None
    )
    assert store._unprotected_commit_secret(digest, recovered) == attempt.commit_secret


def test_unsafe_database_ace_is_refused_before_sqlite_or_pickle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import mutation_lock, writer_lease
    from exomem.writer_lease import IdempotencyStore

    database = tmp_path / "state" / "idempotency.sqlite"
    IdempotencyStore(database)
    mutation_lock._windows_apply_dacl_sddl(database, "D:P(A;OICI;FA;;;WD)")
    called = False

    def sqlite_open(*_args: object, **_kwargs: object):
        nonlocal called
        called = True
        raise AssertionError("unsafe runtime state reached sqlite")

    monkeypatch.setattr(writer_lease.sqlite3, "connect", sqlite_open)
    blocked = IdempotencyStore(database)

    with pytest.raises(RuntimeError, match="unsafe Windows DACL"):
        blocked._connect()
    assert called is False


def test_reparse_runtime_directory_is_refused_before_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import writer_lease
    from exomem.writer_lease import IdempotencyStore

    outside = tmp_path / "outside"
    outside.mkdir()
    runtime = tmp_path / "runtime"
    try:
        runtime.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Windows symlink creation is unavailable: {error}")
    called = False

    def sqlite_open(*_args: object, **_kwargs: object):
        nonlocal called
        called = True
        raise AssertionError("reparse runtime state reached sqlite")

    monkeypatch.setattr(writer_lease.sqlite3, "connect", sqlite_open)

    with pytest.raises(OSError, match="reparse"):
        IdempotencyStore(runtime / "idempotency.sqlite")
    assert called is False


def test_reparse_ancestor_is_refused_before_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import writer_lease
    from exomem.writer_lease import IdempotencyStore

    outside = tmp_path / "outside"
    outside.mkdir()
    ancestor = tmp_path / "runtime-parent"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(ancestor), str(outside)],
        check=False,
        capture_output=True,
        text=True,
    )
    if created.returncode:
        pytest.skip("Windows junction creation is unavailable")
    called = False

    def sqlite_open(*_args: object, **_kwargs: object):
        nonlocal called
        called = True
        raise AssertionError("reparse ancestor reached sqlite")

    monkeypatch.setattr(writer_lease.sqlite3, "connect", sqlite_open)

    with pytest.raises(OSError, match="reparse"):
        IdempotencyStore(ancestor / "state" / "idempotency.sqlite")
    assert called is False
