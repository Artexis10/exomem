"""Process-local foreground priority for explicitly scoped background scans."""

from __future__ import annotations

import threading

import pytest

from exomem import foreground_activity


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
    monkeypatch.setattr(foreground_activity.time, "sleep", slept.append)
    with foreground_activity.background_scope(vault):
        with foreground_activity.foreground_scope(vault):
            foreground_activity.checkpoint(vault)
        assert foreground_activity.background_active(vault)
    assert slept == []
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
        called.append(True)
        return True

    def foreground() -> None:
        with foreground_activity.foreground_scope(vault):
            entered.set()
            release.wait()

    thread = threading.Thread(target=foreground)
    thread.start()
    assert entered.wait(1)
    with foreground_activity.background_scope(vault, waiter_bypass=bypass):
        foreground_activity.checkpoint(vault)
    release.set()
    thread.join(1)
    assert called


def test_checkpoint_is_inert_before_inspecting_a_vault_spelling() -> None:
    class UnusablePath:
        def __fspath__(self) -> str:
            raise AssertionError("inactive checkpoint must not inspect its vault")

    foreground_activity.checkpoint(UnusablePath())


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
