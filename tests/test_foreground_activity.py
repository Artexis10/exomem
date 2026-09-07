"""Process-local foreground priority for explicitly scoped background scans."""

from __future__ import annotations

import json
import os
import select
import signal
import threading
from pathlib import Path

import pytest

from exomem import foreground_activity

_FORK_OBSERVE_SECONDS = 5.0


def _fork_foreground_state(vault: Path, *, checkpoint: bool) -> tuple[int, int]:
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no cover - parent receives the child assertion result
        os.close(read_fd)
        try:
            if checkpoint:
                foreground_activity.checkpoint(vault)
            payload = json.dumps(
                (
                    foreground_activity.foreground_active(vault),
                    foreground_activity.background_active(vault),
                )
            ).encode()
            os.write(write_fd, payload)
        finally:
            os.close(write_fd)
            os._exit(0)
    os.close(write_fd)
    return child, read_fd


def _read_forked_foreground_state(child: int, read_fd: int) -> tuple[bool, bool]:
    try:
        readable, _, _ = select.select([read_fd], [], [], _FORK_OBSERVE_SECONDS)
        assert readable, "forked child retained a foreground activity lock"
        payload = tuple(json.loads(os.read(read_fd, 1024).decode()))
        _pid, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        assert len(payload) == 2
        return bool(payload[0]), bool(payload[1])
    finally:
        os.close(read_fd)
        try:
            reaped, _status = os.waitpid(child, os.WNOHANG)
        except ChildProcessError:
            reaped = child
        if not reaped:
            os.kill(child, signal.SIGKILL)
            os.waitpid(child, 0)


def test_checkpoint_yields_only_to_another_thread_for_the_same_vault(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    other = tmp_path / "other"
    vault.mkdir()
    other.mkdir()
    slept: list[float] = []
    clock = [0.0]
    monkeypatch.setattr(foreground_activity.time, "monotonic", lambda: clock[0])

    def sleep(requested: float) -> None:
        slept.append(requested)
        clock[0] += requested

    monkeypatch.setattr(foreground_activity.time, "sleep", sleep)
    with foreground_activity.foreground_scope(other), foreground_activity.background_scope(vault):
        foreground_activity.checkpoint(vault)
    assert slept == []

    entered = threading.Event()
    release = threading.Event()

    def foreground() -> None:
        with foreground_activity.foreground_scope(vault):
            entered.set()
            release.wait()

    thread = threading.Thread(target=foreground)
    thread.start()
    assert entered.wait(1)
    with foreground_activity.background_scope(vault):
        foreground_activity.checkpoint(vault)
    release.set()
    thread.join(1)
    assert slept and max(slept) <= 0.005
    assert sum(slept) <= 0.050001


def test_foreground_nesting_suppresses_its_background_scope_and_unwinds(tmp_path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    slept: list[float] = []
    entered = threading.Event()
    release = threading.Event()
    holder = threading.Thread(target=lambda: _hold_foreground(vault, entered, release))
    holder.start()
    assert entered.wait(5)
    monkeypatch.setattr(foreground_activity.time, "sleep", slept.append)
    try:
        with foreground_activity.background_scope(vault):
            with foreground_activity.foreground_scope(vault):
                foreground_activity.checkpoint(vault)
            assert foreground_activity.background_active(vault)
    finally:
        release.set()
        holder.join(5)
    assert slept == []
    assert not holder.is_alive()
    assert not foreground_activity.background_active(vault)
    assert not foreground_activity.foreground_active(vault)


def test_foreground_cleanup_covers_base_exception(tmp_path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(KeyboardInterrupt):
        with foreground_activity.foreground_scope(vault):
            assert foreground_activity.foreground_active(vault)
            raise KeyboardInterrupt
    assert not foreground_activity.foreground_active(vault)


def test_waiter_bypass_is_checked_outside_activity_lock(tmp_path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    called = []
    entered = threading.Event()
    release = threading.Event()

    def bypass() -> bool:
        acquired = threading.Event()
        contender = threading.Thread(
            target=lambda: _acquire_activity_lock(acquired),
        )
        contender.start()
        try:
            assert acquired.wait(5)
        finally:
            contender.join(5)
        assert not contender.is_alive()
        called.append(True)
        return True

    def foreground() -> None:
        with foreground_activity.foreground_scope(vault):
            entered.set()
            release.wait()

    thread = threading.Thread(target=foreground)
    thread.start()
    assert entered.wait(1)
    try:
        with foreground_activity.background_scope(vault, waiter_bypass=bypass):
            foreground_activity.checkpoint(vault)
    finally:
        release.set()
        thread.join(5)
    assert called
    assert not thread.is_alive()


def test_checkpoint_is_inert_before_inspecting_a_vault_spelling() -> None:
    class UnusablePath:
        def __fspath__(self) -> str:
            raise AssertionError("inactive checkpoint must not inspect its vault")

    foreground_activity.checkpoint(UnusablePath())


def test_checkpoint_reuses_the_background_scope_identity(tmp_path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    slept: list[float] = []
    clock = [0.0]
    entered = threading.Event()
    release = threading.Event()
    thread = threading.Thread(target=lambda: _hold_foreground(vault, entered, release))
    thread.start()
    assert entered.wait(1)
    monkeypatch.setattr(foreground_activity.time, "monotonic", lambda: clock[0])

    def sleep(requested: float) -> None:
        slept.append(requested)
        clock[0] += requested

    monkeypatch.setattr(foreground_activity.time, "sleep", sleep)

    with foreground_activity.background_scope(vault):
        monkeypatch.setattr(
            foreground_activity,
            "_canonical",
            lambda _root: (_ for _ in ()).throw(AssertionError("checkpoint resolved vault")),
        )
        foreground_activity.checkpoint(vault)

    release.set()
    thread.join(1)
    assert slept and max(slept) <= 0.005
    assert sum(slept) <= 0.050001


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_fork_child_replaces_an_inherited_held_activity_lock(tmp_path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    acquired = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with foreground_activity._LOCK:
            acquired.set()
            assert release.wait(5)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert acquired.wait(1)
    child, read_fd = _fork_foreground_state(vault, checkpoint=False)
    try:
        assert _read_forked_foreground_state(child, read_fd) == (False, False)
    finally:
        release.set()
        holder.join(1)
    assert not holder.is_alive()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_fork_child_drops_vanished_foreground_holders_and_background_scope(tmp_path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    entered = threading.Event()
    release = threading.Event()
    holder = threading.Thread(target=lambda: _hold_foreground(vault, entered, release))
    holder.start()
    assert entered.wait(1)
    try:
        with foreground_activity.background_scope(vault):
            child, read_fd = _fork_foreground_state(vault, checkpoint=True)
        assert _read_forked_foreground_state(child, read_fd) == (False, False)
    finally:
        release.set()
        holder.join(1)
    assert not holder.is_alive()


def test_checkpoint_stops_after_one_scheduling_overshoot(tmp_path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    clock = [0.0]
    sleeps: list[float] = []
    monkeypatch.setattr(foreground_activity.time, "monotonic", lambda: clock[0])

    def sleep(requested: float) -> None:
        sleeps.append(requested)
        clock[0] += 1.0

    monkeypatch.setattr(foreground_activity.time, "sleep", sleep)
    entered = threading.Event()
    release = threading.Event()
    thread = threading.Thread(
        target=lambda: _hold_foreground(vault, entered, release), daemon=True
    )
    thread.start()
    assert entered.wait(1)
    with foreground_activity.background_scope(vault):
        foreground_activity.checkpoint(vault)
    release.set()
    thread.join(1)
    assert sleeps == [0.005]


def test_checkpoint_resamples_waiter_after_a_pause(tmp_path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    waiter = [False]
    sleeps: list[float] = []
    monkeypatch.setattr(foreground_activity.time, "sleep", lambda delay: (sleeps.append(delay), waiter.__setitem__(0, True)))
    entered = threading.Event()
    release = threading.Event()
    thread = threading.Thread(target=lambda: _hold_foreground(vault, entered, release), daemon=True)
    thread.start()
    assert entered.wait(1)
    with foreground_activity.background_scope(vault, waiter_bypass=lambda: waiter[0]):
        foreground_activity.checkpoint(vault)
    release.set()
    thread.join(1)
    assert sleeps == [0.005]


def _hold_foreground(vault, entered: threading.Event, release: threading.Event) -> None:
    with foreground_activity.foreground_scope(vault):
        entered.set()
        release.wait()


def _acquire_activity_lock(acquired: threading.Event) -> None:
    with foreground_activity._LOCK:
        acquired.set()
