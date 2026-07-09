"""Compute-mode resolution + per-machine config persistence (`exomem.mode`).

Torch-free policy layer: taxonomy/aliases, the env → config-file → legacy-alias →
default precedence, the derived booleans (preload / release-when-idle / bulk-gpu),
and the atomic config writer. No hardware needed.
"""

from __future__ import annotations

import builtins
import json
import os
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
    assert mode.normalize("resource-saver") == "quiet"
    assert mode.normalize("low-resource") == "quiet"
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


def test_env_beats_config_file_and_auto_quiet_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "mode": "quiet",
                "auto_quiet": {"active": True, "previous_mode": "normal"},
            }
        ),
        "utf-8",
    )
    monkeypatch.setenv("EXOMEM_MODE", "performance")
    assert mode.resolve_mode() == "performance"


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


@pytest.mark.parametrize(
    "m, expected",
    [
        (
            "quiet",
            {
                "preload_cpu_caches": False,
                "retain_cpu_caches": False,
                "defer_expensive_indexes": True,
                "watcher_policy": {
                    "debounce_seconds": 2.0,
                    "reconcile_interval_seconds": 900.0,
                    "max_embed_files_per_batch": 0,
                    "max_reconcile_embed_files": 0,
                    "defer_expensive_indexes": True,
                },
                "release_when_idle": True,
                "bulk_gpu": False,
            },
        ),
        (
            "normal",
            {
                "preload_cpu_caches": True,
                "retain_cpu_caches": True,
                "defer_expensive_indexes": False,
                "watcher_policy": {
                    "debounce_seconds": 0.5,
                    "reconcile_interval_seconds": 300.0,
                    "max_embed_files_per_batch": 32,
                    "max_reconcile_embed_files": 500,
                    "defer_expensive_indexes": False,
                },
                "release_when_idle": False,
                "bulk_gpu": False,
            },
        ),
        (
            "performance",
            {
                "preload_cpu_caches": True,
                "retain_cpu_caches": True,
                "defer_expensive_indexes": False,
                "watcher_policy": {
                    "debounce_seconds": 0.5,
                    "reconcile_interval_seconds": 300.0,
                    "max_embed_files_per_batch": 32,
                    "max_reconcile_embed_files": 500,
                    "defer_expensive_indexes": False,
                },
                "release_when_idle": True,
                "bulk_gpu": True,
            },
        ),
    ],
)
def test_resolved_resource_policy_fields(
    monkeypatch: pytest.MonkeyPatch, m: str, expected: dict
) -> None:
    monkeypatch.setenv("EXOMEM_MODE", m)
    snap = mode.resolved()
    for key, value in expected.items():
        assert snap[key] == value
    assert mode.preload_cpu_caches() is expected["preload_cpu_caches"]
    assert mode.retain_cpu_caches() is expected["retain_cpu_caches"]
    assert mode.defer_expensive_indexes() is expected["defer_expensive_indexes"]
    assert mode.watcher_policy().as_dict() == expected["watcher_policy"]


def test_watcher_live_embed_cap_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "normal")
    monkeypatch.setenv("EXOMEM_WATCHER_MAX_EMBED_FILES", "7")

    assert mode.watcher_policy().max_embed_files_per_batch == 7


def test_policy_helpers_do_not_import_torch(monkeypatch: pytest.MonkeyPatch) -> None:
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):  # noqa: ANN001
        if name == "torch" or name.startswith("torch."):
            raise AssertionError("mode policy helpers must not import torch")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    mode.normalize("resource-saver")
    mode.resolve_mode()
    mode.preload_models()
    mode.preload_cpu_caches()
    mode.retain_cpu_caches()
    mode.defer_expensive_indexes()
    mode.watcher_policy()
    mode.release_when_idle()
    mode.bulk_gpu_opted()
    mode.resolved()


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
        "preload_cpu_caches": False,
        "retain_cpu_caches": False,
        "defer_expensive_indexes": True,
        "watcher_policy": {
            "debounce_seconds": 2.0,
            "reconcile_interval_seconds": 900.0,
            "max_embed_files_per_batch": 0,
            "max_reconcile_embed_files": 0,
            "defer_expensive_indexes": True,
        },
        "release_when_idle": True,
        "bulk_gpu": False,
    }


# ---- apply_live: reconcile the running process to a mode ----

def _patch_runtime(monkeypatch):
    """Stub runtime unload/start hooks; return a call recorder."""
    from exomem import bm25, embeddings, find, model_reaper

    calls = {"unload": 0, "cache": 0, "start": 0, "stop": 0}
    monkeypatch.setattr(embeddings, "unload_model", lambda: calls.__setitem__("unload", calls["unload"] + 1) or True)
    monkeypatch.setattr(embeddings, "unload_reranker", lambda: True)
    monkeypatch.setattr(embeddings, "unload_clip_model", lambda: True)
    monkeypatch.setattr(embeddings, "unload_index_caches", lambda: calls.__setitem__("cache", calls["cache"] + 1) or {"embedding": 0, "clip": 0})
    monkeypatch.setattr(bm25, "unload_cache", lambda: calls.__setitem__("cache", calls["cache"] + 1) or False)
    monkeypatch.setattr(find, "unload_ram_caches", lambda: calls.__setitem__("cache", calls["cache"] + 1) or {})
    monkeypatch.setattr(model_reaper, "start", lambda: calls.__setitem__("start", calls["start"] + 1))
    monkeypatch.setattr(model_reaper, "stop", lambda: calls.__setitem__("stop", calls["stop"] + 1))
    return calls


def test_apply_live_normal_unloads_and_stops_reaper(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_runtime(monkeypatch)
    monkeypatch.setenv("EXOMEM_MODE", "normal")
    policy = mode.apply_live()
    assert calls["unload"] == 1  # models unloaded so they reload on the new device
    assert calls["stop"] == 1 and calls["start"] == 0  # normal → reaper off
    assert policy["mode"] == "normal"


def test_apply_live_performance_starts_reaper(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_runtime(monkeypatch)
    monkeypatch.setenv("EXOMEM_MODE", "performance")
    mode.apply_live()
    assert calls["start"] == 1 and calls["stop"] == 0


def test_apply_live_quiet_unloads_heavy_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_runtime(monkeypatch)
    monkeypatch.setenv("EXOMEM_MODE", "quiet")
    policy = mode.apply_live()
    assert policy["mode"] == "quiet"
    assert calls["cache"] == 3
    assert calls["start"] == 1 and calls["stop"] == 0


# ---- config watch: apply a file-driven mode change live ----

def test_config_watch_applies_on_change(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    monkeypatch.delenv("EXOMEM_DISABLE_MODE_WATCH", raising=False)
    monkeypatch.delenv("EXOMEM_MODE", raising=False)  # let the config file drive resolution
    applied: list[str] = []
    monkeypatch.setattr(mode, "apply_live", lambda: applied.append(mode.resolve_mode()))
    try:
        mode.start_config_watch(interval=0.02)
        time.sleep(0.06)
        assert applied == []  # baseline normal, no change yet
        mode.write_mode("quiet")
        time.sleep(0.12)
    finally:
        mode.stop_config_watch()
    assert "quiet" in applied


def test_config_watch_disabled_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_MODE_WATCH", "1")
    assert mode.start_config_watch(interval=0.01) is None


# ---- exomem mode CLI ----

def test_mode_cli_status_human(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    from exomem.__main__ import main

    monkeypatch.setenv("EXOMEM_MODE", "quiet")
    assert main(["mode"]) == 0
    out = capsys.readouterr().out
    assert "quiet" in out and "source: env" in out


def test_mode_cli_status_json(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    from exomem.__main__ import main

    monkeypatch.setenv("EXOMEM_MODE", "performance")
    assert main(["mode", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "performance"
    assert data["source"] == "env"
    assert data["bulk_gpu"] is True


def test_mode_cli_set_writes_config(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    from exomem.__main__ import main

    monkeypatch.delenv("EXOMEM_MODE", raising=False)
    assert main(["mode", "gpu"]) == 0  # alias → performance
    assert mode.read_config().get("mode") == "performance"
    assert "performance" in capsys.readouterr().out


def test_mode_cli_low_resource_alias_writes_quiet(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from exomem.__main__ import main

    monkeypatch.delenv("EXOMEM_MODE", raising=False)
    assert main(["mode", "low-resource"]) == 0
    assert mode.read_config().get("mode") == "quiet"
    assert "quiet" in capsys.readouterr().out


# ---- config_path: cross-user (service vs CLI) resolution ----

def test_config_path_env_override_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(tmp_path / "c.json"))
    assert mode.config_path() == tmp_path / "c.json"


def test_config_path_default_shared_and_machine_wide(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default resolves to a machine-wide file (NATIVE platform — patching os.name would
    break pathlib): ProgramData\\exomem on Windows so a LocalSystem service and the user
    CLI share it (the live-switch fix), ~/.exomem on POSIX."""
    monkeypatch.delenv("EXOMEM_CONFIG_PATH", raising=False)
    p = mode.config_path()
    assert p.name == "config.json"
    if os.name == "nt":
        assert "exomem" in p.parts       # %PROGRAMDATA%\exomem\config.json
        assert ".exomem" not in p.parts  # NOT the per-user home dotdir
    else:
        assert ".exomem" in p.parts      # ~/.exomem/config.json


def test_config_path_windows_programdata_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """On Windows, PROGRAMDATA drives the base (native os.name only — no cross-platform patch)."""
    if os.name != "nt":
        pytest.skip("Windows-only path branch")
    monkeypatch.delenv("EXOMEM_CONFIG_PATH", raising=False)
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "PD"))
    assert mode.config_path() == tmp_path / "PD" / "exomem" / "config.json"
