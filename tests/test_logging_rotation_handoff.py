"""The server log keeps reaching its live path when another process rotates it."""

from __future__ import annotations

import json
import logging

from exomem import logging_config


def test_a_record_after_a_peer_rotates_the_server_log_lands_in_the_live_file(tmp_path):
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        logging_config.configure_logging(tmp_path)
        logging.getLogger("exomem.test").info("before the handoff")

        # The outgoing server rotates the shared file, as its RotatingFileHandler does.
        (tmp_path / "exomem.log").rename(tmp_path / "exomem.log.1")
        (tmp_path / "exomem.log").write_text("", encoding="utf-8")

        logging.getLogger("exomem.test").info("after the handoff")
        for handler in root.handlers:
            handler.flush()

        live = [
            json.loads(line)["message"]
            for line in (tmp_path / "exomem.log").read_text().splitlines()
        ]
        backup = [
            json.loads(line)["message"]
            for line in (tmp_path / "exomem.log.1").read_text().splitlines()
        ]
        assert live == ["after the handoff"]
        assert backup == ["before the handoff"]
    finally:
        for handler in list(root.handlers):
            if handler not in saved_handlers:
                root.removeHandler(handler)
                handler.close()
        for handler in saved_handlers:
            if handler not in root.handlers:
                root.addHandler(handler)
        root.setLevel(saved_level)
