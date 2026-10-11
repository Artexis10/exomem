"""Each process role gets its own JSONL log file under the resolved log dir
so Windows never contends for a RotatingFileHandler across processes."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from exomem import logging_config


@pytest.fixture(autouse=True)
def _restore_root_handlers():
    root = logging.getLogger()
    before = list(root.handlers)
    before_level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(before_level)


def test_server_process_writes_exomem_log(tmp_path: Path) -> None:
    logging_config.configure_logging(tmp_path, process="server")
    logging.getLogger("exomem.test").info("hello")
    assert (tmp_path / "exomem.log").exists()


def test_cli_process_writes_separate_file(tmp_path: Path) -> None:
    logging_config.configure_logging(tmp_path, process="cli")
    logging.getLogger("exomem.test").info("hello")
    assert (tmp_path / "exomem-cli.log").exists()
    assert not (tmp_path / "exomem.log").exists()


def test_media_process_writes_separate_file(tmp_path: Path) -> None:
    logging_config.configure_logging(tmp_path, process="media")
    logging.getLogger("exomem.test").info("hello")
    assert (tmp_path / "exomem-media.log").exists()


def test_reconfiguring_for_a_new_process_drops_the_old_handler(tmp_path: Path) -> None:
    logging_config.configure_logging(tmp_path, process="cli")
    logging_config.configure_logging(tmp_path, process="server")
    logging.getLogger("exomem.test").info("after reconfigure")
    assert (tmp_path / "exomem.log").exists()
    root = logging.getLogger()
    from logging.handlers import RotatingFileHandler

    rotating = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
    assert len(rotating) == 1


def test_configured_log_file_is_jsonl(tmp_path: Path) -> None:
    logging_config.configure_logging(tmp_path, process="server")
    logging.getLogger("exomem.test").info("a plain message")
    log_path = tmp_path / "exomem.log"
    line = log_path.read_text(encoding="utf-8").splitlines()[-1]
    payload = json.loads(line)
    assert payload["message"] == "a plain message"
    assert payload["level"] == "INFO"


def test_log_max_mb_and_backups_env_configure_the_rotating_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from logging.handlers import RotatingFileHandler

    monkeypatch.setenv("EXOMEM_LOG_MAX_MB", "2")
    monkeypatch.setenv("EXOMEM_LOG_BACKUPS", "3")
    logging_config.configure_logging(tmp_path, process="server")
    handler = next(h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler))
    assert handler.maxBytes == 2 * 1024 * 1024
    assert handler.backupCount == 3


def test_log_max_mb_env_invalid_falls_back_to_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from logging.handlers import RotatingFileHandler

    monkeypatch.setenv("EXOMEM_LOG_MAX_MB", "not-a-number")
    logging_config.configure_logging(tmp_path, process="server")
    handler = next(h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler))
    assert handler.maxBytes == 5 * 1024 * 1024


def test_exomem_log_level_env_takes_precedence_over_fastmcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "DEBUG")
    logging_config.configure_logging(tmp_path, process="server", level=logging.INFO)
    assert logging.getLogger().level == logging.WARNING


# --- __main__._is_cli_only_invocation --------------------------------------


def test_doctor_is_a_cli_only_invocation() -> None:
    from exomem.__main__ import _is_cli_only_invocation

    assert _is_cli_only_invocation(["doctor"]) is True


def test_a_core_op_is_a_cli_only_invocation() -> None:
    from exomem.__main__ import _is_cli_only_invocation

    assert _is_cli_only_invocation(["ask_memory", "hi"]) is True


def test_bare_serve_is_not_a_cli_only_invocation() -> None:
    from exomem.__main__ import _is_cli_only_invocation

    assert _is_cli_only_invocation([]) is False
    assert _is_cli_only_invocation(["--transport", "stdio"]) is False


def test_main_configures_cli_logging_for_doctor_but_not_for_serve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import exomem.__main__ as main_module

    monkeypatch.setenv("EXOMEM_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(main_module, "_dispatch_main", lambda raw: 0)

    assert main_module.main(["doctor"]) == 0
    assert (tmp_path / "exomem-cli.log").exists()

    for handler in list(logging.getLogger().handlers):
        logging.getLogger().removeHandler(handler)
        handler.close()

    monkeypatch.setenv("EXOMEM_LOG_DIR", str(tmp_path / "unused-by-serve"))
    assert main_module.main([]) == 0
    assert not (tmp_path / "unused-by-serve").exists()


def test_main_dispatches_trace_and_logs_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    import exomem.__main__ as main_module

    monkeypatch.setenv("EXOMEM_LOG_DIR", str(tmp_path))
    assert main_module.main(["trace", "no-such-request-id"]) == 0
    assert "No records found" in capsys.readouterr().out

    (tmp_path / "exomem.log").write_text("hello-from-server-log\n", encoding="utf-8")
    assert main_module.main(["logs", "tail", "--file", "server", "-n", "5"]) == 0
    assert "hello-from-server-log" in capsys.readouterr().out
