from __future__ import annotations

import os
import sqlite3
import subprocess
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows idempotency trust boundary")


@pytest.mark.parametrize("unsafe_replacement", [False, True])
def test_sidecar_delete_pending_preserves_runtime_trust_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe_replacement: bool
) -> None:
    from exomem import mutation_lock
    from exomem.writer_lease import IdempotencyStore

    store = IdempotencyStore(tmp_path / "state" / "idempotency.sqlite")
    sidecar = store.path.with_name(f"{store.path.name}-shm")
    sidecar.write_bytes(b"\0")
    handle = mutation_lock._windows_open_path(
        sidecar, directory=False, access=0x00010080, share=0x7  # DELETE | FILE_READ_ATTRIBUTES
    )
    release = threading.Event()
    replaced = threading.Event()
    observed: list[int] = []
    real_open = mutation_lock._windows_open_path

    def open_path(path: Path, **kwargs):  # noqa: ANN003, ANN202 - native boundary observer
        try:
            return real_open(path, **kwargs)
        except OSError as error:
            if path == sidecar and error.errno == 5:
                observed.append(error.errno)
                release.set()
                assert replaced.wait(timeout=5), "the pending sidecar was not released"
            raise

    def finish_delete() -> None:
        try:
            # The main thread's finally releases this; a timer must not erase the pending state.
            release.wait()
            mutation_lock._windows_close_handle(handle)
            if unsafe_replacement:
                sidecar.write_bytes(b"\0")
                mutation_lock._windows_apply_dacl_sddl(sidecar, "D:P(A;;FA;;;WD)")
        finally:
            replaced.set()

    # Synchronization and DACL replacement are test overhead, outside the production retry budget.
    monkeypatch.setattr(mutation_lock, "_WINDOWS_SQLITE_PATH_RETRY_SECONDS", 5.0)
    monkeypatch.setattr(mutation_lock, "_windows_open_path", open_path)
    thread = threading.Thread(target=finish_delete)
    thread.start()
    try:
        # Retained-handle disposition keeps deletion pending; path deletion may unlink immediately.
        mutation_lock._windows_delete_handle(handle)
        if unsafe_replacement:
            with pytest.raises(mutation_lock.WindowsRuntimeDaclError):
                store._ensure_private_runtime_state()
        else:
            store._ensure_private_runtime_state()
        assert observed, "the validator did not encounter the native delete-pending error"
    finally:
        release.set()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_persistent_sidecar_access_denial_still_refuses(tmp_path: Path) -> None:
    from exomem import mutation_lock
    from exomem.writer_lease import IdempotencyStore

    store = IdempotencyStore(tmp_path / "state" / "idempotency.sqlite")
    sidecar = store.path.with_name(f"{store.path.name}-shm")
    sidecar.write_bytes(b"\0")
    sid = mutation_lock._windows_current_user_sid()
    mutation_lock._windows_apply_dacl_sddl(
        sidecar, f"D:P(D;;GR;;;{sid})(A;;FA;;;{sid})(A;;FA;;;SY)(A;;FA;;;BA)"
    )
    try:
        with pytest.raises(OSError) as raised:
            store._ensure_private_runtime_state()
        assert raised.value.errno == 5
    finally:
        mutation_lock._windows_apply_private_dacl(sidecar, sid)


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


def test_unsafe_initialization_lock_ace_is_refused_before_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import mutation_lock, writer_lease
    from exomem.writer_lease import IdempotencyStore

    database = tmp_path / "state" / "idempotency.sqlite"
    IdempotencyStore(database)
    lock = database.with_name(f"{database.name}.init.lock")
    mutation_lock._windows_apply_dacl_sddl(lock, "D:P(A;OICI;FA;;;WD)")

    def sqlite_open(*_args: object, **_kwargs: object):
        pytest.fail("unsafe initialization lock reached sqlite")

    monkeypatch.setattr(writer_lease.sqlite3, "connect", sqlite_open)
    blocked = IdempotencyStore(database)

    with pytest.raises(RuntimeError, match="unsafe Windows DACL"):
        blocked._connect()


def test_reparse_initialization_lock_is_refused_before_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import writer_lease
    from exomem.writer_lease import IdempotencyStore

    database = tmp_path / "state" / "idempotency.sqlite"
    IdempotencyStore(database)
    lock = database.with_name(f"{database.name}.init.lock")
    outside = tmp_path / "outside.lock"
    outside.write_bytes(b"\0")
    lock.unlink()
    try:
        lock.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"Windows symlink creation is unavailable: {error}")

    def sqlite_open(*_args: object, **_kwargs: object):
        pytest.fail("reparse initialization lock reached sqlite")

    monkeypatch.setattr(writer_lease.sqlite3, "connect", sqlite_open)

    with pytest.raises(OSError, match="reparse"):
        IdempotencyStore(database)


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
