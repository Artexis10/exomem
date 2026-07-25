"""Structured event logging: log_event() attaches {event, fields, content} to
every record via `extra=`, and never raises regardless of what it is handed.
"""

from __future__ import annotations

import json
import logging

import pytest

from exomem import log_events
from exomem.log_events import (
    EVENT_CATALOG,
    JsonLinesFormatter,
    KeyValueFormatter,
    bounded_traceback,
    log_event,
)


@pytest.fixture()
def logger(caplog: pytest.LogCaptureFixture) -> logging.Logger:
    caplog.set_level(logging.DEBUG)
    return logging.getLogger("exomem.test_log_events")


def test_log_event_attaches_extra_fields(logger: logging.Logger, caplog) -> None:
    log_event(
        logger,
        logging.INFO,
        "tool_failure",
        fields={"tool": "ask_memory", "code": "MUTATION_BUSY"},
        content={"message": "boom"},
    )
    record = caplog.records[-1]
    assert record.event == "tool_failure"
    assert record.fields == {"tool": "ask_memory", "code": "MUTATION_BUSY"}
    assert record.content == {"message": "boom"}


def test_log_event_drops_undeclared_content_field(logger: logging.Logger, caplog) -> None:
    """`tool_success` declares no content fields, so a smuggled key is dropped."""
    log_event(
        logger,
        logging.INFO,
        "tool_success",
        fields={"tool": "ask_memory"},
        content={"leaked_query_text": "sensitive"},
    )
    record = caplog.records[-1]
    assert record.content == {}


def test_log_event_drops_all_content_for_unregistered_event(
    logger: logging.Logger, caplog
) -> None:
    log_event(
        logger,
        logging.INFO,
        "not_a_real_event",
        content={"message": "should not survive"},
    )
    record = caplog.records[-1]
    assert record.content == {}


def test_log_event_never_raises_on_bad_input(logger: logging.Logger) -> None:
    # fields/content that aren't mapping-like must not crash the caller.
    log_event(logger, logging.INFO, "tool_success", fields="not-a-mapping")  # type: ignore[arg-type]


def test_event_catalog_declares_every_content_bearing_event_explicitly() -> None:
    assert "tool_failure" in EVENT_CATALOG
    assert "message" in EVENT_CATALOG["tool_failure"].content_fields
    assert "rest_failure" in EVENT_CATALOG
    assert "message" in EVENT_CATALOG["rest_failure"].content_fields
    # A content-free event declares no content fields at all.
    assert EVENT_CATALOG["tool_success"].content_fields == frozenset()


def test_json_lines_formatter_emits_parseable_utc_record(logger: logging.Logger, caplog) -> None:
    log_event(
        logger,
        logging.WARNING,
        "tool_failure",
        fields={"tool": "ask_memory", "duration_ms": 12.5},
        content={"message": "boom"},
    )
    record = caplog.records[-1]
    formatted = JsonLinesFormatter().format(record)
    payload = json.loads(formatted)
    assert payload["event"] == "tool_failure"
    assert payload["level"] == "WARNING"
    assert payload["fields"] == {"tool": "ask_memory", "duration_ms": 12.5}
    assert payload["content"] == {"message": "boom"}
    assert payload["ts"].endswith("+00:00") or payload["ts"].endswith("Z")


def test_json_lines_formatter_handles_plain_unstructured_record(caplog) -> None:
    plain_logger = logging.getLogger("exomem.test_log_events.plain")
    caplog.set_level(logging.DEBUG)
    plain_logger.info("a perfectly ordinary message")
    record = caplog.records[-1]
    payload = json.loads(JsonLinesFormatter().format(record))
    assert payload["message"] == "a perfectly ordinary message"
    assert payload.get("event") is None
    assert "fields" not in payload
    assert "content" not in payload


def test_key_value_formatter_renders_event_and_fields(logger: logging.Logger, caplog) -> None:
    log_event(
        logger,
        logging.INFO,
        "tool_success",
        fields={"tool": "ask_memory", "duration_ms": 3},
    )
    record = caplog.records[-1]
    formatted = KeyValueFormatter().format(record)
    assert "event=tool_success" in formatted
    assert "tool=ask_memory" in formatted
    assert "duration_ms=3" in formatted


def test_bounded_traceback_truncates_long_output() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        text = bounded_traceback(True, max_chars=200)
    assert "ValueError: boom" in text
    assert len(text) <= 220


def test_bounded_traceback_empty_without_exception() -> None:
    assert bounded_traceback(None) == ""


def test_module_has_no_wildcard_surprises() -> None:
    # Sanity: the module exposes exactly what other O1/O3 code depends on.
    for name in ("log_event", "EVENT_CATALOG", "JsonLinesFormatter", "KeyValueFormatter", "bounded_traceback"):
        assert hasattr(log_events, name)
