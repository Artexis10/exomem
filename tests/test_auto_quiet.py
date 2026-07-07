from __future__ import annotations

import builtins
import sys

from exomem import auto_quiet


def _forbid_torch_import(monkeypatch):
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "torch" or name.startswith("torch."):
            raise AssertionError("auto-quiet must not import torch")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)


def test_decide_enters_quiet_after_sustained_pressure() -> None:
    state = auto_quiet.AutoQuietState()

    first = auto_quiet.decide(
        state,
        current_mode="normal",
        config_mode="normal",
        pressure=True,
        now=10.0,
        env_pinned=False,
        enter_after=30.0,
    )
    second = auto_quiet.decide(
        state,
        current_mode="normal",
        config_mode="normal",
        pressure=True,
        now=41.0,
        env_pinned=False,
        enter_after=30.0,
    )

    assert first.action == "none"
    assert second == auto_quiet.AutoQuietDecision(
        "enter_quiet", target_mode="quiet", reason="pressure sustained"
    )
    assert state.engaged is True
    assert state.previous_mode == "normal"


def test_decide_restores_after_clear_hysteresis() -> None:
    state = auto_quiet.AutoQuietState(engaged=True, previous_mode="performance")

    first = auto_quiet.decide(
        state,
        current_mode="quiet",
        config_mode="quiet",
        pressure=False,
        now=100.0,
        env_pinned=False,
        restore_after=60.0,
    )
    second = auto_quiet.decide(
        state,
        current_mode="quiet",
        config_mode="quiet",
        pressure=False,
        now=161.0,
        env_pinned=False,
        restore_after=60.0,
    )

    assert first.action == "none"
    assert second == auto_quiet.AutoQuietDecision(
        "restore", target_mode="performance", reason="pressure clear"
    )
    assert state.engaged is False


def test_decide_does_not_restore_over_manual_mode_change() -> None:
    state = auto_quiet.AutoQuietState(engaged=True, previous_mode="normal")

    decision = auto_quiet.decide(
        state,
        current_mode="performance",
        config_mode="performance",
        pressure=False,
        now=200.0,
        env_pinned=False,
    )

    assert decision.action == "none"
    assert decision.reason == "manual mode change detected"
    assert state.engaged is False
    assert state.previous_mode is None


def test_decide_respects_env_pin() -> None:
    state = auto_quiet.AutoQuietState()

    decision = auto_quiet.decide(
        state,
        current_mode="normal",
        config_mode="normal",
        pressure=True,
        now=100.0,
        env_pinned=True,
        enter_after=0.0,
    )

    assert decision.action == "none"
    assert decision.reason == "EXOMEM_MODE pins mode"


def test_pressure_probe_uses_non_torch_headroom(monkeypatch) -> None:
    _forbid_torch_import(monkeypatch)
    monkeypatch.setattr(
        auto_quiet.resource_status,
        "gpu_headroom",
        lambda: {"status": "marginal", "usable": False},
    )

    assert auto_quiet.pressure_active() is True


def test_unavailable_probe_soft_fails_without_mode_change(monkeypatch) -> None:
    monkeypatch.setattr(
        auto_quiet.resource_status,
        "gpu_headroom",
        lambda: {"status": "unknown", "usable": None},
    )
    monkeypatch.setattr(
        auto_quiet.mode,
        "write_mode",
        lambda value: (_ for _ in ()).throw(AssertionError("must not write mode")),
    )

    decision = auto_quiet.tick(auto_quiet.AutoQuietState(), now=100.0)

    assert decision.action == "none"
    assert decision.reason == "pressure probe unavailable"


def test_start_if_enabled_is_default_off_and_env_gated(monkeypatch, tmp_path) -> None:
    auto_quiet.stop()
    monkeypatch.delenv("EXOMEM_AUTO_QUIET", raising=False)
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(tmp_path / "config.json"))

    assert auto_quiet.start_if_enabled() is None

    started: list[str] = []

    class FakeThread:
        def __init__(self, *, target, name, daemon) -> None:
            self.target = target
            self.name = name
            self.daemon = daemon

        def start(self) -> None:
            started.append(self.name)

        def is_alive(self) -> bool:
            return True

        def join(self, timeout=None) -> None:
            return None

    monkeypatch.setenv("EXOMEM_AUTO_QUIET", "1")
    monkeypatch.setattr(auto_quiet.threading, "Thread", FakeThread)

    thread = auto_quiet.start_if_enabled()
    try:
        assert thread is not None
        assert started == ["exomem-auto-quiet"]
    finally:
        auto_quiet.stop()
