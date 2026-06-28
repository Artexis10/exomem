"""The adopted-RankingConfig disk-load seam in find.py.

find() consults disk for a tuned config ONLY when called without an explicit
`config` (the live server's path); the eval harnesses pass config= and stay
hermetic. These tests exercise the loader directly: resolution order, coercion,
fail-loud-then-DEFAULT, and the per-process memo. They drop the suite-wide
KB_MCP_DISABLE_RANKING_CONFIG so the real seam runs, and pin the resolved path
to a tmp file so a committed repo-root config can't leak in.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from kb_mcp import find as find_module


@pytest.fixture(autouse=True)
def _exercise_real_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo the suite-wide disable flag and reset the memo around each test."""
    monkeypatch.delenv("KB_MCP_DISABLE_RANKING_CONFIG", raising=False)
    monkeypatch.delenv("KB_MCP_RANKING_CONFIG", raising=False)
    find_module.reset_active_ranking_cache()
    yield
    find_module.reset_active_ranking_cache()


def _write(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_absent_file_returns_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_MCP_RANKING_CONFIG", str(tmp_path / "nope.json"))
    assert find_module._active_ranking() == find_module.DEFAULT_RANKING


def test_valid_file_is_loaded_with_tuple_coercion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write(
        tmp_path / "ranking_config.json",
        {
            "rrf_k": 30,
            "compiled_boost": 1.3,
            "intent_weights_exact": [0.7, 1.5, 1.5, 1.0, 0.7, 1.0],
        },
    )
    monkeypatch.setenv("KB_MCP_RANKING_CONFIG", str(cfg))
    loaded = find_module._active_ranking()
    assert loaded.rrf_k == 30
    assert loaded.compiled_boost == 1.3
    # JSON arrays must come back as tuples (frozen dataclass requires hashable).
    assert isinstance(loaded.intent_weights_exact, tuple)
    assert loaded.intent_weights_exact == (0.7, 1.5, 1.5, 1.0, 0.7, 1.0)
    # Unspecified knobs keep their defaults.
    assert loaded.source_penalty == find_module.DEFAULT_RANKING.source_penalty


def test_malformed_json_fails_loud_then_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bad = tmp_path / "ranking_config.json"
    bad.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("KB_MCP_RANKING_CONFIG", str(bad))
    with caplog.at_level(logging.ERROR, logger="kb_mcp.find"):
        assert find_module._active_ranking() == find_module.DEFAULT_RANKING
    assert any("invalid" in r.message for r in caplog.records)


def test_unknown_knob_ignored_missing_defaulted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = _write(tmp_path / "ranking_config.json", {"rrf_k": 42, "bogus_knob": 5})
    monkeypatch.setenv("KB_MCP_RANKING_CONFIG", str(cfg))
    with caplog.at_level(logging.WARNING, logger="kb_mcp.find"):
        loaded = find_module._active_ranking()
    assert loaded.rrf_k == 42  # known knob applied
    assert loaded.compiled_boost == find_module.DEFAULT_RANKING.compiled_boost
    assert any("bogus_knob" in r.message for r in caplog.records)


def test_bad_lane_tuple_length_fails_loud_then_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = _write(tmp_path / "ranking_config.json", {"intent_weights_exact": [1.0, 2.0, 3.0]})
    monkeypatch.setenv("KB_MCP_RANKING_CONFIG", str(cfg))
    with caplog.at_level(logging.ERROR, logger="kb_mcp.find"):
        assert find_module._active_ranking() == find_module.DEFAULT_RANKING
    assert any("lane weights" in r.message or "invalid" in r.message for r in caplog.records)


def test_disable_flag_forces_default_even_with_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write(tmp_path / "ranking_config.json", {"rrf_k": 30})
    monkeypatch.setenv("KB_MCP_RANKING_CONFIG", str(cfg))
    monkeypatch.setenv("KB_MCP_DISABLE_RANKING_CONFIG", "1")
    assert find_module._active_ranking() == find_module.DEFAULT_RANKING


def test_repo_root_fallback_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no env override, the loader reads <repo_root>/ranking_config.json."""
    monkeypatch.setattr(find_module, "_REPO_ROOT", tmp_path)
    _write(tmp_path / "ranking_config.json", {"rrf_k": 99})
    assert find_module._active_ranking().rrf_k == 99


def test_memo_loads_once_until_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "ranking_config.json"
    _write(cfg, {"rrf_k": 30})
    monkeypatch.setenv("KB_MCP_RANKING_CONFIG", str(cfg))
    assert find_module._active_ranking().rrf_k == 30
    _write(cfg, {"rrf_k": 77})  # change on disk
    assert find_module._active_ranking().rrf_k == 30  # memo still serves the old value
    find_module.reset_active_ranking_cache()
    assert find_module._active_ranking().rrf_k == 77  # reloaded after reset
