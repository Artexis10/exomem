"""TDD-red tests for the `EXOMEM_LOG_DIR` override contract (OpenSpec change
`add-docker-distribution`; see design.md D8 and specs/distribution-surfaces/spec.md's
"Configurable Log Directory" requirement).

The implementation lands in a parallel task
(`logging_config.resolve_log_dir()`, `server.run()`, `query_log.py`). Until
`resolve_log_dir` exists, several of these fail on a clean `AttributeError`
rather than a silent wrong-path assertion; the rest fail on an assertion
mismatch because today's code never consults `EXOMEM_LOG_DIR` at all.

Contract:
- `logging_config.resolve_log_dir()` returns `Path($EXOMEM_LOG_DIR)` when the
  env var is set, else today's `parents[2] / "logs"` default — byte-identical
  to current behavior when unset.
- `server.run()`'s `log_dir` resolution: a passed `log_dir=` argument
  (unchanged, still wins) -> `EXOMEM_LOG_DIR` env -> the same default.
- `query_log`'s JSONL paths (`QUERIES_PATH`/`WRITES_PATH`/`READS_PATH`) resolve
  through the same helper at each write, not a value frozen at import time.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from exomem import logging_config, query_log, server


class _StopRun(Exception):
    """Raised by the fake `configure_logging` to abort `server.run()` right
    after logging would be configured, before it reaches `build_server()` /
    `mcp.run()` — stdio would block reading stdin forever, and HTTP transport
    requires a full OAuth environment; this test needs neither."""


def _default_log_dir() -> Path:
    """Mirrors today's hardcoded default independently of the implementation
    under test, so a bug in the new resolution logic can't accidentally make
    the test agree with itself."""
    return Path(server.__file__).resolve().parents[2] / "logs"


def _capturing_configure_logging(captured: list[Path]):
    def _fake(log_dir: Path, *args, **kwargs) -> None:
        captured.append(log_dir)
        raise _StopRun

    return _fake


# --- logging_config.resolve_log_dir() --------------------------------------


def test_resolve_log_dir_honors_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "custom-logs"
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(override))
    assert logging_config.resolve_log_dir() == override


def test_resolve_log_dir_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_LOG_DIR", raising=False)
    assert logging_config.resolve_log_dir() == _default_log_dir()


# --- server.run() -----------------------------------------------------------


def test_server_run_uses_env_log_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "env-logs"
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(override))

    captured: list[Path] = []
    monkeypatch.setattr(
        logging_config, "configure_logging", _capturing_configure_logging(captured)
    )

    with pytest.raises(_StopRun):
        server.run(transport="stdio")
    assert captured == [override]


def test_server_run_default_log_dir_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXOMEM_LOG_DIR", raising=False)

    captured: list[Path] = []
    monkeypatch.setattr(
        logging_config, "configure_logging", _capturing_configure_logging(captured)
    )

    with pytest.raises(_StopRun):
        server.run(transport="stdio")
    assert captured == [_default_log_dir()]


def test_server_run_explicit_log_dir_wins_over_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The existing explicit-override precedent is preserved: a caller-passed
    `log_dir=` still wins over `EXOMEM_LOG_DIR`."""
    env_dir = tmp_path / "env-logs"
    explicit_dir = tmp_path / "explicit-logs"
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(env_dir))

    captured: list[Path] = []
    monkeypatch.setattr(
        logging_config, "configure_logging", _capturing_configure_logging(captured)
    )

    with pytest.raises(_StopRun):
        server.run(transport="stdio", log_dir=explicit_dir)
    assert captured == [explicit_dir]


# --- query_log ----------------------------------------------------------------


def test_query_log_writes_under_env_override_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_QUERY_LOG", raising=False)
    override = tmp_path / "override-logs"
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(override))

    query_log.log_write_call(tool="note", written_path="x", cited_sources=[])

    written = override / "writes.jsonl"
    assert written.exists(), (
        "log_write_call should resolve its path through the same "
        "EXOMEM_LOG_DIR-aware accessor as logging_config.resolve_log_dir(), "
        "not a path frozen at import time"
    )
    rec = json.loads(written.read_text(encoding="utf-8").splitlines()[0])
    assert rec["tool"] == "note"
    assert rec["written_path"] == "x"


def test_query_log_uses_module_default_when_env_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With EXOMEM_LOG_DIR unset, writes land at the module-level default
    path — the seam the existing query-log tests monkeypatch. The env var is
    consulted PER CALL (previous test); the unset case must keep the
    patchable-constant contract the rest of the suite depends on."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_QUERY_LOG", raising=False)
    monkeypatch.delenv("EXOMEM_LOG_DIR", raising=False)
    default_path = tmp_path / "patched-writes.jsonl"
    monkeypatch.setattr(query_log, "WRITES_PATH", default_path)

    query_log.log_write_call(tool="note", written_path="y", cited_sources=[])

    assert default_path.exists()
