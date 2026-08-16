"""Hosted redaction upgrade: a structured, cataloged `log_event()` record
drops only its `content` payload in a hosted cell, keeping the content-free
`event`/`fields` skeleton. Any record NOT produced through `log_event()` for a
cataloged event keeps today's full-blanking, fail-closed behavior unchanged.
`exc_info` is always stripped in a hosted cell, structured or not.
"""

from __future__ import annotations

import json
import logging
import threading

import pytest

from exomem import hosted_runtime, privacy_log
from exomem.log_events import JsonLinesFormatter, log_event


@pytest.fixture()
def hosted_logger(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    monkeypatch.setenv(hosted_runtime.HOSTED_MODE_ENV, "true")
    privacy_log.install_hosted_log_redaction()
    caplog.set_level(logging.DEBUG)
    return logging.getLogger("exomem.test_privacy_structured_redaction")


def test_hosted_structured_record_drops_content_keeps_skeleton(hosted_logger, caplog) -> None:
    log_event(
        hosted_logger,
        logging.WARNING,
        "tool_failure",
        fields={"tool": "ask_memory", "code": "MUTATION_BUSY"},
        content={"message": "sensitive vault content excerpt"},
    )
    record = caplog.records[-1]
    assert record.event == "tool_failure"
    assert record.fields == {"tool": "ask_memory", "code": "MUTATION_BUSY"}
    assert not getattr(record, "content", None)
    assert "sensitive vault content excerpt" not in caplog.text


def test_hosted_unstructured_record_stays_fully_blanked(hosted_logger, caplog) -> None:
    hosted_logger.warning("plain message with /secret/vault/path.md inside")
    record = caplog.records[-1]
    assert record.getMessage() == "event=hosted_log_redacted code=HOSTED_CONTENT_REDACTED"
    assert "/secret/vault/path.md" not in caplog.text


def test_hosted_structured_record_still_strips_exc_info(hosted_logger, caplog) -> None:
    try:
        raise ValueError("private-sentinel-detail")
    except ValueError:
        log_event(
            hosted_logger,
            logging.ERROR,
            "tool_failure",
            fields={"tool": "ask_memory"},
            content={"message": "boom"},
            exc_info=True,
        )
    record = caplog.records[-1]
    assert record.exc_info is None
    assert record.exc_text is None
    assert "private-sentinel-detail" not in caplog.text


def test_hosted_call_trace_line_passes_through_but_strips_exc_info(hosted_logger, caplog) -> None:
    call_log = logging.getLogger("exomem.calls")
    try:
        raise ValueError("trace-exc-sentinel")
    except ValueError:
        call_log.error(
            "event=hosted_call kind=tool_error tool=ask_memory request_id=req-1",
            exc_info=True,
        )
    record = caplog.records[-1]
    assert record.getMessage().startswith("event=hosted_call ")
    assert record.exc_info is None
    assert "trace-exc-sentinel" not in caplog.text


def test_non_hosted_structured_record_is_untouched(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv(hosted_runtime.HOSTED_MODE_ENV, raising=False)
    privacy_log.install_hosted_log_redaction()
    caplog.set_level(logging.DEBUG)
    logger = logging.getLogger("exomem.test_privacy_structured_redaction.nonhosted")
    log_event(
        logger,
        logging.INFO,
        "tool_failure",
        fields={"tool": "ask_memory"},
        content={"message": "not hosted, stays as-is"},
    )
    record = caplog.records[-1]
    assert record.content == {"message": "not hosted, stays as-is"}

def test_hosted_blanked_record_emits_only_allowed_keys(hosted_logger, caplog) -> None:
    """Pin what a fully-blanked record actually PUTS ON DISK, not what the
    LogRecord looks like.

    The boundary blanks a record in place (`msg`/`args`/`fields`/`content`/
    `exc_*`); it does not rebuild the payload from an allowlist. So every key
    `JsonLinesFormatter` chooses to emit is one the boundary must have
    explicitly considered, and a new formatter key silently rides through
    redaction by default. Every other test in this file asserts on record
    attributes, which is precisely how `thread` was first added without anyone
    noticing it crossed the boundary.

    If this fails because you added a formatter key: decide whether its value
    can ever carry tenant- or content-derived text. If it can, either make the
    source static or drop the key in the redacted branch. Only then widen the
    set below.
    """
    hosted_logger.warning("plain message with /secret/vault/path.md inside")
    payload = json.loads(JsonLinesFormatter().format(caplog.records[-1]))

    assert set(payload) == {"event", "level", "logger", "message", "thread", "ts"}
    assert payload["message"] == "event=hosted_log_redacted code=HOSTED_CONTENT_REDACTED"
    assert "/secret/vault/path.md" not in json.dumps(payload)
    # Every exomem thread name is a static literal, so `thread` is content-free
    # by construction — `tests/test_log_thread_identity.py` pins the one site
    # that used to interpolate a vault path.
    assert payload["thread"] == threading.current_thread().name


def test_hosted_uncataloged_extra_record_is_fully_blanked(hosted_logger, caplog) -> None:
    """The boundary itself must blank `content`/`fields` for any record whose
    event is not in the catalog — even one injected through raw `extra=`,
    bypassing `log_event()`'s own classification guard. Classification is
    earned at the boundary, never assumed from the caller."""
    hosted_logger.warning(
        "raw message",
        extra={
            "event": "not_in_catalog",
            "fields": {"note": "field-sentinel"},
            "content": {"secret": "content-sentinel"},
        },
    )
    record = caplog.records[-1]
    assert record.getMessage() == "event=hosted_log_redacted code=HOSTED_CONTENT_REDACTED"
    assert not getattr(record, "content", None)
    assert not getattr(record, "fields", None)
