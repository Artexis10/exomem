"""Every `exomem*.log` record names the thread that emitted it.

Request threads and background daemons (`exomem-graph-rebuild`,
`exomem-lexical-repair-*`) interleave in one file, and without a thread name
adjacency reads as causation — issue #576 drew two wrong conclusions that way.
`JsonLinesFormatter` therefore carries the emitting thread's name on every
record.

The field is deliberately additive and taken from `record.threadName`, which is
captured when the record is created, on the emitting thread — not from
`threading.current_thread()` at format time, which names whichever thread the
handler happens to run on. `test_background_thread_record_names_its_own_thread`
is what separates the two.

Safety for readers is a property of *where* the key lands: only the three
`exomem*.log` files go through this formatter. `mutations.jsonl`,
`queries.jsonl`, `writes.jsonl` and `reads.jsonl` are written by separate
hand-rolled appenders (`mutation_journal._append` / `query_log._append`) and are
untouched, which is why the doctor and `/health/ready` journal checks cannot
see this change at all.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

import pytest

from exomem.log_events import JsonLinesFormatter, log_event


@pytest.fixture()
def logger(caplog: pytest.LogCaptureFixture) -> logging.Logger:
    caplog.set_level(logging.DEBUG)
    return logging.getLogger("exomem.test_log_thread_identity")


def _payload(record: logging.LogRecord) -> dict:
    return json.loads(JsonLinesFormatter().format(record))


def test_every_record_carries_a_thread_name(logger: logging.Logger, caplog) -> None:
    """Structured and plain records alike, unconditionally."""
    log_event(logger, logging.INFO, "tool_success", fields={"tool": "remember"})
    structured = _payload(caplog.records[-1])
    assert structured["thread"] == threading.current_thread().name

    logger.info("a perfectly ordinary message")
    plain = _payload(caplog.records[-1])
    assert plain["thread"] == threading.current_thread().name


def test_background_thread_record_names_its_own_thread(
    logger: logging.Logger, caplog
) -> None:
    """A daemon-emitted line must not be attributed to the main thread.

    The record is emitted on `exomem-graph-rebuild` and formatted back on the
    main thread — exactly the handler arrangement in the service — so a
    format-time `threading.current_thread().name` would report `MainThread`
    here and lose the attribution the whole field exists to provide.
    """
    emitted: list[logging.LogRecord] = []

    def _emit() -> None:
        log_event(logger, logging.INFO, "tool_success", fields={"tool": "rebuild"})
        emitted.append(caplog.records[-1])

    worker = threading.Thread(target=_emit, name="exomem-graph-rebuild")
    worker.start()
    worker.join()

    payload = _payload(emitted[-1])
    assert payload["thread"] == "exomem-graph-rebuild"
    assert payload["thread"] != threading.current_thread().name


def test_thread_name_falls_back_when_logging_omits_it(
    logger: logging.Logger, caplog
) -> None:
    """`logging.logThreads = False` leaves `threadName` None; the key stays."""
    logger.info("no thread attribute")
    record = caplog.records[-1]
    record.threadName = None  # type: ignore[assignment]
    assert _payload(record)["thread"] == threading.current_thread().name


def test_thread_key_is_additive_and_reorders_nothing(
    logger: logging.Logger, caplog
) -> None:
    """Existing keys keep their values and their relative order."""
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
    assert payload["logger"] == "exomem.test_log_thread_identity"
    assert payload["fields"] == {"tool": "ask_memory", "duration_ms": 12.5}
    assert payload["content"] == {"message": "boom"}
    assert payload["ts"].endswith("+00:00")

    # The contract is additive: nothing renamed, nothing reordered. Emitted
    # order is `sort_keys=True`, so assert the pre-existing keys still appear
    # in their original relative order — which stays true if a later change
    # adds another key, unlike an exact key-set equality.
    established = ("content", "event", "fields", "level", "logger", "message", "ts")
    seen = [key for key in payload if key in established]
    assert seen == list(established)
    assert "thread" in payload


def test_obs_cli_trace_still_joins_records_carrying_a_thread_name(
    logger: logging.Logger,
    caplog,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`exomem trace` is the only cross-file joiner; unknown keys pass through."""
    from exomem import mutation_journal, obs_cli

    monkeypatch.setenv("EXOMEM_LOG_DIR", str(tmp_path))
    request_id = "req-thread-identity"

    log_event(
        logger,
        logging.INFO,
        "tool_success",
        fields={"tool": "remember", "request_id": request_id},
    )
    line = JsonLinesFormatter().format(caplog.records[-1])
    (tmp_path / "exomem.log").write_text(line + "\n", encoding="utf-8")

    mutation_journal.record_mutation(
        request_id=request_id,
        tool="remember",
        command="note",
        receipt_id=None,
        outcome="committed",
        error_code=None,
        duration_ms=1.0,
    )

    joined = obs_cli.trace(request_id)
    sources = [entry["_source"] for entry in joined]
    assert "server" in sources and "mutations" in sources
    server_entry = next(e for e in joined if e["_source"] == "server")
    assert server_entry["thread"] == threading.current_thread().name
    assert server_entry["fields"]["request_id"] == request_id


def test_jsonl_journals_do_not_gain_a_thread_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The journals the doctor and `/health/ready` parse are a separate writer.

    `doctor._check_observability` and `runtime_readiness` only tail-parse
    `queries/writes/reads/mutations.jsonl`. Those never pass through
    `JsonLinesFormatter`, so the new key cannot reach them — this pins that.
    """
    from exomem import mutation_journal, query_log

    monkeypatch.setenv("EXOMEM_LOG_DIR", str(tmp_path))
    # `query_log._disabled()` is gated on both of these.
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_QUERY_LOG", raising=False)

    mutation_journal.record_mutation(
        request_id="req-journal",
        tool="remember",
        command="note",
        receipt_id=None,
        outcome="committed",
        error_code=None,
        duration_ms=1.0,
    )
    query_log.log_write_call(
        tool="note", written_path="Knowledge Base/x.md", cited_sources=[]
    )

    for name in ("mutations.jsonl", "writes.jsonl"):
        path = tmp_path / name
        assert path.exists(), name
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            assert "thread" not in json.loads(raw), name


def test_tool_surface_line_regex_still_mines_middleware_traces(
    logger: logging.Logger, caplog
) -> None:
    """`scripts/analyze_tool_surface.py` regexes raw `exomem.log` lines.

    It reads the `event=... tool=...` text the call middleware embeds in
    `message`, so a new sibling JSON key must not land between them.
    """
    line_re = re.compile(r"event=tool_(start|success|error)\b.*?\btool=(\S+)")
    logger.info("event=tool_start tool=remember request_id=abc123")
    formatted = JsonLinesFormatter().format(caplog.records[-1])

    assert "thread" in json.loads(formatted)
    match = line_re.search(formatted)
    assert match is not None
    assert match.group(1) == "start"
    assert match.group(2).rstrip('",') == "remember"
