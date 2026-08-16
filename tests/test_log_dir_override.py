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
  env var is set, else today's `parents[2] / "logs"` default WHEN this
  process is genuinely running against a source checkout (which is what
  pytest does here — see `test_this_repo_is_detected_as_a_source_checkout`
  below) — byte-identical to current behavior in that case, but NOT
  unconditionally: a wheel install takes a different, per-platform fallback
  instead of guessing `parents[2] / "logs"` blind. See
  `tests/test_resolve_log_dir_wheel_fallback.py` for that non-checkout case
  (issue #552).
- `server.run()`'s `log_dir` resolution: a passed `log_dir=` argument
  (unchanged, still wins) -> `EXOMEM_LOG_DIR` env -> the same default.
- `query_log`'s JSONL paths (`QUERIES_PATH`/`WRITES_PATH`/`READS_PATH`) resolve
  through the same helper at each write, not a value frozen at import time.
- `query_log.current_log_dir()` and `audit._RELEVANCE_LOGS_DIR` must agree
  with `logging_config.resolve_log_dir()` with `EXOMEM_LOG_DIR` unset — all
  seven log files (three `exomem*.log`, `mutations.jsonl`, and
  `queries.jsonl`/`writes.jsonl`/`reads.jsonl`) stay co-located rather than
  three of them being computed independently and drifting.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from exomem import audit, logging_config, query_log, server


class _StopRun(Exception):
    """Raised by the fake `configure_logging` to abort `server.run()` right
    after logging would be configured, before it reaches `build_server()` /
    `mcp.run()` — stdio would block reading stdin forever, and HTTP transport
    requires a full OAuth environment; this test needs neither."""


def _default_log_dir() -> Path:
    """Mirrors today's hardcoded checkout-branch default independently of the
    implementation under test, so a bug in the new resolution logic can't
    accidentally make the test agree with itself.

    This is ONLY the correct expectation because pytest itself runs against
    a genuine, editable-installed source checkout of this repo — the
    condition `test_this_repo_is_detected_as_a_source_checkout` asserts
    directly, so this file's assumption is checked rather than merely
    convenient. In a wheel install this formula would NOT match
    `resolve_log_dir()`'s actual return value — that case lives in
    `tests/test_resolve_log_dir_wheel_fallback.py`.
    """
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


def test_this_repo_is_detected_as_a_source_checkout() -> None:
    """Precondition for every `_default_log_dir()`-based assertion in this
    file: they only hold because pytest here runs against a genuine,
    editable-installed source checkout, which takes the checkout branch of
    `resolve_log_dir()`'s fallback. If checkout detection ever regressed for
    a real checkout, those assertions could still incidentally pass by
    coincidence with a *different* wrong fallback — so assert the
    precondition directly rather than relying on that coincidence."""
    checkout_root = Path(logging_config.__file__).resolve().parents[2]
    assert logging_config._is_source_checkout(checkout_root) is True


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


# --- cross-module agreement: nothing computes the log dir independently ----
#
# Issue #552 follow-up: `query_log._LOG_DIR` and `audit._RELEVANCE_LOGS_DIR`
# used to each hardcode their own `<repo>/logs` guess instead of routing
# through `logging_config.resolve_log_dir()`. That drifted silently on a
# wheel install once the checkout-only fallback stopped being assumed
# everywhere: `exomem.log`/`exomem-cli.log`/`exomem-media.log`/
# `mutations.jsonl` moved to the corrected location while
# `queries.jsonl`/`writes.jsonl`/`reads.jsonl` stayed behind — so
# `exomem trace <id>` silently lost those three files' records, and three of
# `doctor`'s four JSONL health checks became permanently unfindable. These
# tests assert agreement directly rather than relying on both sides
# independently reimplementing the same formula and hoping they match.


# `query_log._LOG_DIR` and `audit._RELEVANCE_LOGS_DIR` freeze
# `resolve_log_dir()`'s ENTIRE answer at import — env branch included — so on a
# box where EXOMEM_LOG_DIR is exported before pytest starts (this project's own
# Docker images set it, and the `resolve_log_dir()` docstring tells operators to
# export it) those constants already hold the env value and `delenv` alone
# cannot restore the unset precondition these two assert. Reload each module
# under the cleared env so its constant is re-derived from that precondition,
# then undo the monkeypatch BEFORE the restoring reload so the cleanup reload
# doesn't itself bake the cleared env in (monkeypatch's own teardown runs after
# this function returns, too late for a reload done here) — the same discipline
# as the sentinel tests below.


def test_query_log_current_dir_agrees_with_resolve_log_dir_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXOMEM_LOG_DIR", raising=False)
    try:
        reloaded = importlib.reload(query_log)
        assert reloaded.current_log_dir() == logging_config.resolve_log_dir()
    finally:
        monkeypatch.undo()
        importlib.reload(query_log)


def test_audit_relevance_logs_dir_agrees_with_resolve_log_dir_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXOMEM_LOG_DIR", raising=False)
    try:
        reloaded = importlib.reload(audit)
        assert reloaded._RELEVANCE_LOGS_DIR == logging_config.resolve_log_dir()
    finally:
        monkeypatch.undo()
        importlib.reload(audit)


# The two agreement tests above hold in THIS process's checkout environment
# even under the superseded (pre-#552-followup) code, because a source
# checkout's `resolve_log_dir()` answer happens to equal the old hardcoded
# `<repo>/logs` guess too — a checkout test run can't distinguish "computed
# via resolve_log_dir()" from "coincidentally computed the same value another
# way". These two instead probe DIRECTLY whether each module's constant is
# actually SOURCED from `logging_config.resolve_log_dir()` — by patching that
# function to return a sentinel unrelated to any real filesystem path, then
# reloading the module and checking the sentinel propagated. Environment-
# independent: fails identically whether the test process is a checkout or a
# simulated wheel install.


def _sentinel_dir() -> Path:
    import sys

    return Path("Z:/__sentinel_log_dir__" if sys.platform == "win32" else "/__sentinel_log_dir__")


def test_query_log_module_constant_is_sourced_from_resolve_log_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = _sentinel_dir()
    monkeypatch.setattr(logging_config, "resolve_log_dir", lambda *a, **k: sentinel)
    try:
        reloaded = importlib.reload(query_log)
        assert reloaded._LOG_DIR == sentinel
        assert reloaded.QUERIES_PATH == sentinel / "queries.jsonl"
        assert reloaded.WRITES_PATH == sentinel / "writes.jsonl"
        assert reloaded.READS_PATH == sentinel / "reads.jsonl"
    finally:
        # Restore the REAL resolve_log_dir before reloading again, so the
        # cleanup reload doesn't itself bake the sentinel back in (monkeypatch's
        # own teardown runs AFTER this function returns, which would be too
        # late for a reload done inside this finally).
        monkeypatch.undo()
        importlib.reload(query_log)


def test_audit_module_constant_is_sourced_from_resolve_log_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = _sentinel_dir()
    monkeypatch.setattr(logging_config, "resolve_log_dir", lambda *a, **k: sentinel)
    try:
        reloaded = importlib.reload(audit)
        assert reloaded._RELEVANCE_LOGS_DIR == sentinel
    finally:
        monkeypatch.undo()
        importlib.reload(audit)
