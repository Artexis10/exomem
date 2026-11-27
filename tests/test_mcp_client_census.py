"""The call ledger and `exomem doctor` say which protocol features each MCP client declares.

Every case drives the real MCP HTTP app, so the revision and capabilities are read from
the request the way a client sends them, not from a faked context.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from exomem import call_ledger, doctor, server

LEGACY_VERSION = "2025-11-25"
MODERN_VERSION = "2026-07-28"
TASKS = "io.modelcontextprotocol/tasks"


@pytest.fixture
def ledger_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "logs"
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(directory))
    monkeypatch.delenv("EXOMEM_DISABLE_CALL_LEDGER", raising=False)
    # Content-private logging reduces what a row may carry; a test opts in to it explicitly.
    monkeypatch.delenv("EXOMEM_HOSTED_CELL", raising=False)
    monkeypatch.delenv("EXOMEM_CLOUD_CELL", raising=False)
    call_ledger.reset_chain_cache()
    yield directory
    call_ledger.reset_chain_cache()


@asynccontextmanager
async def _client(*, stateless: bool) -> AsyncIterator[httpx.AsyncClient]:
    mcp = server.ExomemFastMCP("census")
    mcp.add_middleware(server.CallTraceMiddleware())

    @mcp.tool
    async def probe() -> dict[str, bool]:
        return {"ok": True}

    app = mcp.http_app(stateless_http=stateless, json_response=True)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://census.example"
        ) as client:
            yield client


def _headers(user_agent: str, **extra: str) -> dict[str, str]:
    return {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "user-agent": user_agent,
        **extra,
    }


async def _call_modern(
    client: httpx.AsyncClient, *, capabilities: dict[str, Any], client_info: dict[str, str] | None,
    user_agent: str = "test-agent/1",
) -> None:
    meta: dict[str, Any] = {
        "io.modelcontextprotocol/protocolVersion": MODERN_VERSION,
        "io.modelcontextprotocol/clientCapabilities": capabilities,
    }
    if client_info is not None:
        meta["io.modelcontextprotocol/clientInfo"] = client_info
    response = await client.post(
        "/mcp",
        headers=_headers(
            user_agent, accept="application/json", **{
                "mcp-protocol-version": MODERN_VERSION, "mcp-method": "tools/call", "mcp-name": "probe",
            }
        ),
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"_meta": meta, "name": "probe", "arguments": {}}},
    )
    assert response.status_code == 200, response.text


async def _call_legacy(
    client: httpx.AsyncClient, *, user_agent: str, session: str | None = None
) -> None:
    extra = {"mcp-protocol-version": LEGACY_VERSION}
    if session is not None:
        extra["mcp-session-id"] = session
    response = await client.post(
        "/mcp", headers=_headers(user_agent, **extra),
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "probe", "arguments": {}}},
    )
    assert response.status_code == 200, response.text


def _rows(directory: Path) -> list[dict[str, Any]]:
    path = directory / "ledger.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.anyio
async def test_a_2026_07_28_call_records_its_revision_and_the_extensions_it_declares(ledger_dir: Path) -> None:
    async with _client(stateless=True) as client:
        await _call_modern(
            client,
            capabilities={"extensions": {TASKS: {}}, "elicitation": {"form": {}}},
            client_info={"name": "claude-code", "version": "2.9"},
        )

    [row] = _rows(ledger_dir)
    assert row["client_name"] == "claude-code"
    assert row["protocol_version"] == MODERN_VERSION
    assert row["client_extensions"] == ["elicitation:form", TASKS]
    assert call_ledger.verify() == []


@pytest.mark.anyio
async def test_a_stateless_older_call_records_its_revision_and_leaves_the_extensions_unknown(
    ledger_dir: Path,
) -> None:
    async with _client(stateless=True) as client:
        await _call_legacy(client, user_agent="codex/0.5")

    [row] = _rows(ledger_dir)
    assert row["client_name"] == "codex/0.5"
    assert row["protocol_version"] == LEGACY_VERSION
    # A stateless call keeps no handshake, so "declares nothing" would be a claim the server cannot make.
    assert "client_extensions" not in row


async def _call_after_handshake(capabilities: dict[str, Any], *, name: str = "stdio-like") -> None:
    """One call on a stateful session whose initialize exchange declared `capabilities`."""
    async with _client(stateless=False) as client:
        initialized = await client.post(
            "/mcp", headers=_headers("local-agent/1"),
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": LEGACY_VERSION,
                "capabilities": capabilities,
                "clientInfo": {"name": name, "version": "1"},
            }},
        )
        session = initialized.headers["mcp-session-id"]
        await client.post(
            "/mcp",
            headers=_headers("local-agent/1", **{"mcp-session-id": session, "mcp-protocol-version": LEGACY_VERSION}),
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        await _call_legacy(client, user_agent="local-agent/1", session=session)


@pytest.mark.anyio
async def test_a_stateful_older_call_records_the_extensions_its_handshake_declared(ledger_dir: Path) -> None:
    await _call_after_handshake({"extensions": {TASKS: {}}})

    [row] = _rows(ledger_dir)
    assert row["client_name"] == "stdio-like"
    assert row["protocol_version"] == LEGACY_VERSION
    assert row["client_extensions"] == [TASKS]


@pytest.mark.anyio
async def test_an_empty_elicitation_object_records_form_mode(ledger_dir: Path) -> None:
    # `elicitation: {}` is how a 2025-06-18 client says it supports form mode, and 2025-11-25 keeps
    # that reading; recording nothing would list it as a client without elicitation.
    await _call_after_handshake({"elicitation": {}})

    [row] = _rows(ledger_dir)
    assert row["client_extensions"] == ["elicitation:form"]


@pytest.mark.anyio
async def test_a_2026_07_28_call_without_client_info_is_named_by_its_user_agent_and_declares_nothing(
    ledger_dir: Path,
) -> None:
    async with _client(stateless=True) as client:
        await _call_modern(client, capabilities={}, client_info=None, user_agent="bare-client/4")

    [row] = _rows(ledger_dir)
    assert row["client_name"] == "bare-client/4"
    assert row["client_extensions"] == []


def test_a_client_cannot_fill_a_row_with_extension_ids(ledger_dir: Path) -> None:
    stuffed = [f"vendor.example/{number:03d}-{'x' * 200}" for number in range(40)]

    row = call_ledger.build_row(
        sequence=1, prev_hash=call_ledger.GENESIS_HASH, request_id="r", tool="probe", outcome="ok",
        duration_ms=1.0, client_extensions=stuffed,
    )

    assert len(row["client_extensions"]) == 16
    assert all(len(name) <= 64 for name in row["client_extensions"])
    assert row["client_extensions_truncated"] == 24
    assert row["row_hash"] == call_ledger.row_hash(row)


@pytest.mark.anyio
async def test_the_doctor_lists_each_client_and_revision_of_the_last_day_with_its_extensions(
    ledger_dir: Path,
) -> None:
    async with _client(stateless=True) as client:
        await _call_modern(
            client, capabilities={"extensions": {TASKS: {}}}, client_info={"name": "claude-code", "version": "2"},
        )
        await _call_legacy(client, user_agent="codex/0.5")
    older = dt.datetime.now(dt.UTC) - dt.timedelta(hours=30)
    stale = call_ledger.build_row(
        sequence=1, prev_hash=call_ledger.GENESIS_HASH, request_id="old", tool="probe", outcome="ok",
        duration_ms=1.0, client_name="yesterday-client", protocol_version=LEGACY_VERSION,
        timestamp=older.isoformat(timespec="milliseconds"),
    )
    path = ledger_dir / "ledger.jsonl"
    path.write_text(json.dumps(stale) + "\n" + path.read_text(encoding="utf-8"), encoding="utf-8")

    check = doctor._check_mcp_clients()

    assert check.status == "pass"
    assert check.message == (
        f"2 tool call(s) in the last 24 hours: "
        f"claude-code on {MODERN_VERSION} ({TASKS}); codex/0.5 on {LEGACY_VERSION} (not recorded)"
    )
    assert "yesterday-client" not in check.message


@pytest.mark.anyio
async def test_the_doctor_says_its_census_is_partial_when_the_ledger_starts_inside_the_day(
    ledger_dir: Path,
) -> None:
    # A rotation leaves the live ledger starting a few minutes ago: the census must not
    # present those minutes as the whole day.
    async with _client(stateless=True) as client:
        await _call_legacy(client, user_agent="codex/0.5")

    check = doctor._check_mcp_clients()

    assert check.status == "pass"
    assert check.message.startswith("1 tool call(s) since ")
    assert "UTC (partial: the ledger starts there): codex/0.5 on" in check.message
    assert check.details["complete"] is False
    assert check.details["covered_since"] is not None


def _ledger_row(sequence: int, *, age: dt.timedelta, client: str, **fields: Any) -> dict[str, Any]:
    """A ledger row stamped `age` ago, written directly so the census reads a history a test controls."""
    at = dt.datetime.now(dt.UTC) - age
    return call_ledger.build_row(
        sequence=sequence, prev_hash=call_ledger.GENESIS_HASH, request_id=f"r{sequence}", tool="probe",
        outcome="ok", duration_ms=1.0, client_name=client, protocol_version=LEGACY_VERSION,
        timestamp=at.isoformat(timespec="milliseconds"), **fields,
    )


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


@pytest.mark.anyio
async def test_a_server_side_call_that_no_client_session_carries_still_leaves_its_row(ledger_dir: Path) -> None:
    # `mcp.call_tool` has a request context but no session, and FastMCP raises RuntimeError on `session`.
    mcp = server.ExomemFastMCP("census")
    mcp.add_middleware(server.CallTraceMiddleware())

    @mcp.tool
    async def probe() -> dict[str, bool]:
        return {"ok": True}

    await mcp.call_tool("probe", {})

    [row] = _rows(ledger_dir)
    assert row["tool"] == "probe"
    assert "client_extensions" not in row


@pytest.mark.anyio
async def test_a_census_that_fails_never_costs_the_call_its_row(
    ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> dict[str, Any]:
        raise ValueError("the census could not read this request")

    monkeypatch.setattr("exomem.command_surface.mcp_protocol_features", broken)
    async with _client(stateless=True) as client:
        await _call_legacy(client, user_agent="codex/0.5")

    [row] = _rows(ledger_dir)
    assert row["client_name"] == "codex/0.5"
    assert "protocol_version" not in row


@pytest.mark.anyio
async def test_content_private_logging_keeps_only_the_specifications_extension_ids_and_counts_the_rest(
    ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Rows are hash-chained and cannot be scrubbed later, so a client-chosen id must never reach one.
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    async with _client(stateless=True) as client:
        await _call_modern(
            client,
            capabilities={"extensions": {"com.example/anything": {}, TASKS: {}}, "elicitation": {"form": {}}},
            client_info={"name": "claude-code", "version": "2.9"},
        )

    [row] = _rows(ledger_dir)
    assert row["client_extensions"] == ["elicitation:form", TASKS]
    assert row["client_extensions_other"] == 1
    assert "com.example" not in (ledger_dir / "ledger.jsonl").read_text(encoding="utf-8")
    assert "+1 other" in doctor._check_mcp_clients().message


@pytest.mark.anyio
async def test_the_doctor_message_shows_a_clients_control_bytes_as_escapes(ledger_dir: Path) -> None:
    async with _client(stateless=True) as client:
        await _call_modern(
            client,
            capabilities={"extensions": {"vendor.example/\x1b[31mred": {}}},
            client_info={"name": "evil\x1b[2Jclient", "version": "1"},
        )

    message = doctor._check_mcp_clients().message

    assert message.isprintable()
    assert "evil\\x1b[2Jclient" in message
    assert "vendor.example/\\x1b[31mred" in message


def test_the_doctor_message_is_one_line_and_names_the_ten_busiest_pairs(ledger_dir: Path) -> None:
    rows = [_ledger_row(1, age=dt.timedelta(hours=30), client="before-the-window")]
    for number in range(12):
        rows.append(_ledger_row(number + 2, age=dt.timedelta(minutes=10), client=f"client-{number:02d}"))
    # One more call from client-11 makes it the busiest, so the cut is by calls and not by name.
    rows.append(_ledger_row(14, age=dt.timedelta(minutes=5), client="client-11"))
    _write_rows(ledger_dir / "ledger.jsonl", rows)

    check = doctor._check_mcp_clients()

    assert "\n" not in check.message
    assert check.message.startswith("13 tool call(s) in the last 24 hours: client-11 on 2025-11-25 (not recorded); client-00 on")
    assert check.message.endswith("; +2 more")
    assert len(check.details["clients"]) == 12


def test_the_census_reads_the_archived_ledger_to_reach_the_start_of_the_day(ledger_dir: Path) -> None:
    # A busy install rotates its live ledger within hours; reading only the live file would
    # report a partial day on every run.
    _write_rows(ledger_dir / "ledger-archive" / "ledger-0001.jsonl", [
        _ledger_row(1, age=dt.timedelta(hours=30), client="before-the-window"),
        _ledger_row(2, age=dt.timedelta(hours=10), client="archived-client", client_extensions=[TASKS]),
    ])
    _write_rows(ledger_dir / "ledger.jsonl", [_ledger_row(3, age=dt.timedelta(minutes=5), client="live-client")])

    check = doctor._check_mcp_clients()

    assert check.message == (
        "2 tool call(s) in the last 24 hours: "
        f"archived-client on {LEGACY_VERSION} ({TASKS}); live-client on {LEGACY_VERSION} (not recorded)"
    )
    assert check.details["complete"] is True


def _mark_a_failed_append(ledger_dir: Path, *, age: dt.timedelta) -> None:
    """Leave the marker a failed append leaves beside the ledger, as another process wrote it."""
    marker = call_ledger.gap_marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    at = (dt.datetime.now(dt.UTC) - age).timestamp()
    os.utime(marker, (at, at))


def test_a_failed_append_starts_the_count_after_it(ledger_dir: Path) -> None:
    # Five calls are older than the failure and two are newer. The label says "since the
    # failure", so the count must hold only the two.
    rows = [
        _ledger_row(number, age=dt.timedelta(hours=6 + number * 0.5), client="before-the-gap")
        for number in range(1, 6)
    ]
    rows.sort(key=lambda row: row["ts_utc"])
    rows.append(_ledger_row(6, age=dt.timedelta(hours=1), client="after-the-gap"))
    rows.append(_ledger_row(7, age=dt.timedelta(minutes=20), client="after-the-gap"))
    _write_rows(ledger_dir / "ledger.jsonl", rows)
    _mark_a_failed_append(ledger_dir, age=dt.timedelta(hours=2))

    check = doctor._check_mcp_clients()

    assert check.message.startswith("2 tool call(s) since ")
    assert check.message.endswith(
        f"UTC (partial: a call before then may have left no row): after-the-gap on {LEGACY_VERSION} (not recorded)"
    )
    assert "before-the-gap" not in check.message
    assert check.details["complete"] is False
    assert [client["tool_calls"] for client in check.details["clients"]] == [2]


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="needs POSIX permissions that bind the caller",
)
def test_a_real_failed_append_leaves_no_older_call_in_the_count(ledger_dir: Path) -> None:
    _write_rows(ledger_dir / "ledger.jsonl", [
        _ledger_row(1, age=dt.timedelta(hours=30), client="before-the-window"),
        _ledger_row(2, age=dt.timedelta(hours=2), client="codex/0.5"),
    ])
    path = ledger_dir / "ledger.jsonl"
    path.chmod(0o400)
    try:
        lost = call_ledger.record_call(request_id="lost", tool="probe", outcome="ok", duration_ms=1.0)
        assert lost is None, "sanity: the append failed, so a call has no row"
    finally:
        path.chmod(0o600)

    check = doctor._check_mcp_clients()

    assert check.message.startswith("No tool calls since ")
    assert "(partial: a call before then may have left no row)" in check.message
    assert check.details["complete"] is False


def test_a_cloud_cell_doctor_reports_a_complete_day_for_calls_older_than_its_own_start(
    ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The doctor is a new process on every run. Its own start is not a gap in the ledger.
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    _write_rows(ledger_dir / "ledger.jsonl", [
        _ledger_row(1, age=dt.timedelta(hours=30), client="before-the-window"),
        *[
            _ledger_row(number, age=dt.timedelta(hours=7 - number), client="claude-code")
            for number in range(2, 5)
        ],
    ])

    check = doctor._check_mcp_clients()

    assert check.message.startswith("3 tool call(s) in the last 24 hours: ")
    assert "partial" not in check.message
    assert check.details["complete"] is True


@pytest.mark.anyio
async def test_content_private_logging_counts_an_id_under_the_specifications_prefix_that_is_not_official(
    ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A client can write free text after the prefix, and a chained row cannot be scrubbed.
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    async with _client(stateless=True) as client:
        await _call_modern(
            client,
            capabilities={"extensions": {"io.modelcontextprotocol/patient-has-diagnosis-x": {}}},
            client_info={"name": "claude-code", "version": "2.9"},
        )

    [row] = _rows(ledger_dir)
    assert row["client_extensions"] == []
    assert row["client_extensions_other"] == 1
    assert "patient-has-diagnosis" not in (ledger_dir / "ledger.jsonl").read_text(encoding="utf-8")
