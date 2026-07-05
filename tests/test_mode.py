"""Compute-mode resolution + per-machine config persistence (`exomem.mode`).

Torch-free policy layer: taxonomy/aliases, the env → config-file → legacy-alias →
default precedence, the derived booleans (preload / release-when-idle / bulk-gpu),
and the atomic config writer. No hardware needed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import mode


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the config file at a controllable tmp path and clear ambient mode env."""
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(tmp_path / "config.json"))
    for var in ("EXOMEM_MODE", "EXOMEM_QUIET_MODE", "EXOMEM_RELEASE_GPU_WHEN_IDLE"):
        monkeypatch.delenv(var, raising=False)


# ---- normalize / aliases ----

def test_normalize_canonical_and_aliases() -> None:
    assert mode.normalize("quiet") == "quiet"
    assert mode.normalize("normal") == "normal"
    assert mode.normalize("performance") == "performance"
    assert mode.normalize("gpu") == "performance"  # alias
    assert mode.normalize("turbo") == "performance"  # alias
    assert mode.normalize(" GPU ") == "performance"  # case/space-insensitive


def test_normalize_unknown_is_none() -> None:
    assert mode.normalize("nonsense") is None
    assert mode.normalize("") is None
    assert mode.normalize(None) is None


# ---- resolve_mode precedence ----

def test_default_is_normal() -> None:
    assert mode.resolve_mode() == "normal"


def test_env_mode_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "quiet")
    assert mode.resolve_mode() == "quiet"


def test_env_mode_accepts_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "turbo")
    assert mode.resolve_mode() == "performance"


def test_config_file_used_when_no_env() -> None:
    mode.write_mode("performance")
    assert mode.resolve_mode() == "performance"


def test_env_beats_config_file(monkeypatch: pytest.MonkeyPatch) -> None:
    mode.write_mode("performance")
    monkeypatch.setenv("EXOMEM_MODE", "quiet")
    assert mode.resolve_mode() == "quiet"


def test_legacy_quiet_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_QUIET_MODE", "1")
    assert mode.resolve_mode() == "quiet"


def test_invalid_env_mode_falls_through_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "bogus")
    assert mode.resolve_mode() == "normal"


# ---- derived policy booleans ----

@pytest.mark.parametrize(
    "m, preload", [("quiet", False), ("normal", True), ("performance", True)]
)
def test_preload_models(monkeypatch: pytest.MonkeyPatch, m: str, preload: bool) -> None:
    monkeypatch.setenv("EXOMEM_MODE", m)
    assert mode.preload_models() is preload


@pytest.mark.parametrize(
    "m, release", [("quiet", True), ("normal", False), ("performance", True)]
)
def test_release_when_idle_by_mode(monkeypatch: pytest.MonkeyPatch, m: str, release: bool) -> None:
    monkeypatch.setenv("EXOMEM_MODE", m)
    assert mode.release_when_idle() is release


def test_release_when_idle_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "normal")  # default off
    monkeypatch.setenv("EXOMEM_RELEASE_GPU_WHEN_IDLE", "on")
    assert mode.release_when_idle() is True
    monkeypatch.setenv("EXOMEM_MODE", "performance")  # default on
    monkeypatch.setenv("EXOMEM_RELEASE_GPU_WHEN_IDLE", "off")
    assert mode.release_when_idle() is False


@pytest.mark.parametrize(
    "m, bulk", [("quiet", False), ("normal", False), ("performance", True)]
)
def test_bulk_gpu_opted(monkeypatch: pytest.MonkeyPatch, m: str, bulk: bool) -> None:
    monkeypatch.setenv("EXOMEM_MODE", m)
    assert mode.bulk_gpu_opted() is bulk


# ---- config file read/write ----

def test_read_config_missing_is_empty() -> None:
    assert mode.read_config() == {}


def test_read_config_corrupt_is_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text("{not json", "utf-8")
    assert mode.read_config() == {}


def test_write_mode_roundtrip_and_atomic(tmp_path: Path) -> None:
    path = mode.write_mode("gpu")  # alias normalized to canonical
    assert path == tmp_path / "config.json"
    data = json.loads(path.read_text("utf-8"))
    assert data["mode"] == "performance"
    assert data["schema"] == 1
    assert not list(tmp_path.glob("*.tmp"))  # temp file cleaned up by os.replace


def test_write_mode_preserves_other_keys(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text(json.dumps({"schema": 1, "other": "keep"}), "utf-8")
    mode.write_mode("quiet")
    data = json.loads((tmp_path / "config.json").read_text("utf-8"))
    assert data["mode"] == "quiet"
    assert data["other"] == "keep"


def test_write_mode_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        mode.write_mode("bogus")


def test_resolved_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "quiet")
    snap = mode.resolved()
    assert snap == {
        "mode": "quiet",
        "preload_models": False,
        "release_when_idle": True,
        "bulk_gpu": False,
    }
