"""The server log keeps reaching its live path when another process rotates it."""

from __future__ import annotations

import json
import logging

import pytest

from exomem import logging_config


@pytest.fixture
def server_log(tmp_path):
    """Configure the server log in `tmp_path`, then restore the root logger."""
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    logging_config.configure_logging(tmp_path)
    try:
        yield tmp_path
    finally:
        for handler in list(root.handlers):
            if handler not in saved_handlers:
                root.removeHandler(handler)
                handler.close()
        for handler in saved_handlers:
            if handler not in root.handlers:
                root.addHandler(handler)
        root.setLevel(saved_level)


def _messages(path):
    for handler in logging.getLogger().handlers:
        handler.flush()
    return [json.loads(line)["message"] for line in path.read_text().splitlines()]


def test_a_record_after_a_peer_rotates_the_server_log_lands_in_the_live_file(server_log):
    logging.getLogger("exomem.test").info("before the handoff")

    # The outgoing server rotates the shared file, as its RotatingFileHandler does.
    (server_log / "exomem.log").rename(server_log / "exomem.log.1")
    (server_log / "exomem.log").write_text("", encoding="utf-8")

    logging.getLogger("exomem.test").info("after the handoff")

    assert _messages(server_log / "exomem.log") == ["after the handoff"]
    assert _messages(server_log / "exomem.log.1") == ["before the handoff"]


def test_a_reopen_that_fails_never_raises_to_the_caller_and_recovers_once_the_path_heals(
    server_log, monkeypatch
):
    monkeypatch.setattr(logging, "raiseExceptions", False)
    log = logging.getLogger("exomem.test")
    log.info("before the handoff")

    # The path is replaced by something the handler cannot open.
    (server_log / "exomem.log").rename(server_log / "exomem.log.1")
    (server_log / "exomem.log").mkdir()
    log.info("lost while the path is unusable")
    log.info("also lost")

    (server_log / "exomem.log").rmdir()
    log.info("after the path heals")

    assert _messages(server_log / "exomem.log") == ["after the path heals"]
