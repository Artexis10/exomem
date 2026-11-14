"""TEMPORARY Windows diagnostic, deleted before the change ends: where the offline migrate route stops."""

from __future__ import annotations

import ctypes
import gc
import os
import sqlite3
import sys
import traceback
from ctypes import wintypes

import pytest
from test_collection_store_migration import (
    OPERATOR,
    env,  # noqa: F401
    legacy_vault,
    stop_writer,
)
from test_collection_store_migration import (
    test_offline_migrate_routes_every_records_and_planning_collection_without_changing_a_vault_byte as _real,
)
from test_vault_store_migration import (
    ALPHA,
    BETA,
    OPERATOR_SECRET,
    RELEASED,
    _lease_environment,
    coordinator_url,  # noqa: F401
)

from exomem import held_fs
from exomem.collection_store import admission, capability, custody, legacy
from exomem.governance.principal import library_scope

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" and not os.environ.get("DIAG_ANY"), reason="Windows diagnostic"
)


def _route(tmp_path, monkeypatch, url, step):
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv(OPERATOR, raising=False)
    monkeypatch.delenv(custody.SYNC_ROOTS_ENV, raising=False)
    monkeypatch.setattr("exomem.records._capture_sweep_carrier", lambda *a, **kw: None)
    monkeypatch.setattr("exomem.records._due_state_carrier", lambda *a, **kw: None)
    for name, value in _lease_environment(tmp_path, url).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(capability, "RELEASED", RELEASED)
    outcome = None
    with library_scope():
        root = legacy_vault(tmp_path / "vault")
        (root / ALPHA).write_text("---\ntitle: Alpha\ntype: note\n---\n# Alpha\n\nIn [[Beta]].\n")
        (root / BETA).write_text("---\ntitle: Beta\ntype: note\n---\n# Beta\n\nThe table.\n")
        with pytest.MonkeyPatch.context() as shell:
            shell.setenv(OPERATOR, OPERATOR_SECRET)
            try:
                outcome = "OK " + repr(admission.migrate_route(root, step=step))[:1500]
            except BaseException:  # noqa: BLE001 - diagnostic
                outcome = "RAISED\n" + traceback.format_exc()
    stop_writer()
    pytest.fail(f"DIAG {step}: {outcome}")


@pytest.mark.parametrize("step", ["apply"])
def test_route(tmp_path, monkeypatch, coordinator_url, step):  # noqa: F811
    _route(tmp_path, monkeypatch, coordinator_url, step)


class _Process(ctypes.Structure):
    _fields_ = [("pid", wintypes.DWORD), ("start", wintypes.FILETIME)]


class _Info(ctypes.Structure):
    _fields_ = [
        ("process", _Process),
        ("app", wintypes.WCHAR * 256),
        ("service", wintypes.WCHAR * 64),
        ("kind", wintypes.DWORD),
        ("status", wintypes.ULONG),
        ("session", wintypes.DWORD),
        ("restartable", wintypes.BOOL),
    ]


def _holders(path):
    """The processes that the Restart Manager says hold ``path`` open."""
    manager = ctypes.WinDLL("Rstrtmgr")
    session = wintypes.DWORD(0)
    key = (wintypes.WCHAR * 33)()
    if manager.RmStartSession(ctypes.byref(session), 0, key):
        return "RmStartSession failed"
    try:
        files = (wintypes.LPCWSTR * 1)(str(path))
        if manager.RmRegisterResources(session, 1, files, 0, None, 0, None):
            return "RmRegisterResources failed"
        needed, count, reasons = wintypes.UINT(0), wintypes.UINT(0), wintypes.DWORD(0)
        code = manager.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), None, ctypes.byref(reasons))
        if code == 0 and needed.value == 0:
            return "no holder"
        if code != 234:
            return f"RmGetList {code}"
        infos = (_Info * needed.value)()
        count = wintypes.UINT(needed.value)
        code = manager.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), infos, ctypes.byref(reasons))
        return f"{code}: " + ", ".join(f"{i.process.pid} {i.app}" for i in infos[: count.value]) + f" (self {os.getpid()})"
    finally:
        manager.RmEndSession(session)


DIAGS = []


def _live_connections():
    found = []
    for candidate in gc.get_objects():
        if isinstance(candidate, sqlite3.Connection):
            try:
                found.append(str(candidate.execute("PRAGMA database_list").fetchall()))
            except sqlite3.Error:
                pass
    return found


def test_instrumented_preflight(env, capsys, monkeypatch):  # noqa: F811
    original = legacy.LegacyAuditSpool.__exit__

    def wrapped(self, *exc):
        path, directory = self._path, self._staging_directory
        try:
            return original(self, *exc)
        except BaseException as error:
            DIAGS.append(f"spool exit failed: {error!r}")
            DIAGS.append(f"holders: {_holders(path)}")
            DIAGS.append(f"live connections: {_live_connections()}")
            gc.collect()
            DIAGS.append(f"after gc: holders {_holders(path)}")
            with held_fs.acquire(directory).require() as filesystem, filesystem.parent(".").require() as parent:
                again = filesystem.file(parent, path.name, access="mutate")
                DIAGS.append(f"after gc: mutate open ok={again.ok}")
                if again.ok:
                    again.value.close()
            raise

    monkeypatch.setattr(legacy.LegacyAuditSpool, "__exit__", wrapped)
    try:
        _real(env, capsys)
        outcome = "the real test passed"
    except BaseException:  # noqa: BLE001 - diagnostic
        outcome = "RAISED " + traceback.format_exc()[-2500:]
    pytest.fail("DIAG " + "\n".join(DIAGS) + "\n" + outcome)
