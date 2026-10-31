"""`logs/ledger.jsonl`: one hash-chained row per MCP tool call, from every client.

The properties worth pinning are the ones whose absence made a live incident
hard to explain: a refusal that looked like a success, a call from an
unidentifiable client, note content leaking into an operator-readable file, and
a record whose gaps were silent.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import call_ledger, cloud_cell, command_surface, guards
from exomem import server as server_module


@pytest.fixture(autouse=True)
def ledger_dir(tmp_path, monkeypatch):
    directory = tmp_path / "logs"
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(directory))
    monkeypatch.delenv("EXOMEM_DISABLE_CALL_LEDGER", raising=False)
    call_ledger.reset_chain_cache()
    yield directory
    call_ledger.reset_chain_cache()


def _rows(directory: Path) -> list[dict]:
    path = directory / "ledger.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _drive(tool: str, arguments: dict, call_next):
    context = SimpleNamespace(message={"params": {"name": tool, "arguments": arguments}})
    return asyncio.run(
        server_module.CallTraceMiddleware().on_call_tool(context, call_next)
    )


async def _ok(_context):
    return {"ok": True}


# ---------------------------------------------------------------- one per call


def test_a_successful_call_appends_exactly_one_row(ledger_dir: Path) -> None:
    _drive("remember", {"title": "x"}, _ok)

    rows = _rows(ledger_dir)
    assert len(rows) == 1
    assert rows[0]["tool"] == "remember"
    assert rows[0]["outcome"] == "ok"
    assert rows[0]["error_code"] is None
    assert rows[0]["duration_ms"] is not None
    assert rows[0]["request_id"]


def test_a_read_call_is_recorded_too(ledger_dir: Path) -> None:
    """Reads are the bulk of call volume and the least journalled today.

    `mutations.jsonl` covers writes only, so a client whose *reads* are failing
    leaves no structured trace at all.
    """
    _drive("ask_memory", {"query": "anything"}, _ok)

    rows = _rows(ledger_dir)
    assert [row["tool"] for row in rows] == ["ask_memory"]
    assert rows[0]["outcome"] == "ok"


def test_one_call_is_never_double_counted(ledger_dir: Path) -> None:
    for _ in range(3):
        _drive("browse_memory", {}, _ok)

    assert len(_rows(ledger_dir)) == 3


# ------------------------------------------------------------------- outcomes


def test_a_refusal_is_recorded_as_refused_with_its_code(ledger_dir: Path) -> None:
    """The defect this ledger exists for: refusals return, they do not raise.

    The tool wrapper converts a governance refusal into an error *envelope* and
    returns it, so control flow at this seam is indistinguishable from success.
    Only the wrapper's breadcrumb tells them apart, and without consulting it
    every `WRITER_LEASE_REQUIRED` and `MUTATION_BUSY` would be durably recorded
    as `ok` -- leaving "the write silently did not happen" unreconstructable.
    """

    async def call_next(_context):
        # The exact seam the synchronous tool wrapper uses on a refusal.
        command_surface._log_tool_failure(
            tool="remember",
            request_id=command_surface.mcp_request_id(),
            code="MUTATION_BUSY",
            duration_ms=1.0,
            message="another mutation holds the lock",
        )
        return {"ok": False, "error": {"code": "MUTATION_BUSY"}}

    _drive("remember", {"title": "x"}, call_next)

    rows = _rows(ledger_dir)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "refused"
    assert rows[0]["error_code"] == "MUTATION_BUSY"


def test_an_uncaught_exception_is_recorded_as_an_error(ledger_dir: Path) -> None:
    async def call_next(_context):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        _drive("remember", {"title": "x"}, call_next)

    rows = _rows(ledger_dir)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "error"
    assert rows[0]["error_code"] == "RuntimeError"


def test_a_guard_rejection_is_still_recorded(ledger_dir: Path) -> None:
    """The content guard rejects *before* `call_next`, so it is the one call
    shape that would otherwise leave no row at all. It is a governance refusal,
    not a crash, so it is `refused` carrying the guard's own code."""

    async def call_next(_context):
        raise AssertionError("the guard should have rejected before the leaf")

    with pytest.raises(ValueError):
        _drive("remember", {"content": "A" * 2_000_000}, call_next)

    rows = _rows(ledger_dir)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "refused"
    assert rows[0]["error_code"] == "BINARY_BLOB_REJECTED"


def test_an_unstructured_error_keeps_its_class_name() -> None:
    """A genuinely unexpected exception must stay visible as a bug rather than
    be laundered into a plausible-looking refusal code."""
    assert (
        server_module._leading_error_code(ValueError("MUTATION_BUSY: held"))
        == "MUTATION_BUSY"
    )
    assert (
        server_module._leading_error_code(ValueError("something: broke")) == "ValueError"
    )
    assert (
        server_module._leading_error_code(RuntimeError("no colon here")) == "RuntimeError"
    )


# -------------------------------------------------------------------- latency


def test_a_row_carries_both_the_leaf_and_the_caller_s_wall_clock(
    ledger_dir: Path,
) -> None:
    """`duration_ms` is the leaf; `total_ms` is what the caller waited.

    Keeping both is the point: `duration_ms` is what the prose trace and
    `exomem_tool_duration_ms` have always reported, so redefining it would
    silently change a live metric -- but on its own it cannot explain a call
    that was slow *before* the leaf.
    """

    async def call_next(_context):
        time.sleep(0.05)
        return {"ok": True}

    _drive("ask_memory", {"query": "q"}, call_next)

    row = _rows(ledger_dir)[0]
    assert row["duration_ms"] >= 50
    assert row["total_ms"] >= row["duration_ms"]


def test_time_spent_before_the_leaf_is_visible(ledger_dir: Path, monkeypatch) -> None:
    """The gap between the two clocks is the diagnostic: it says the cost was
    in admission, not in the work. A guard that got slow would otherwise be
    invisible, because the leaf clock does not start until the guard is done."""
    real_guard = guards.guard_text_content

    def slow_guard(content, **kwargs):
        time.sleep(0.05)
        return real_guard(content, **kwargs)

    monkeypatch.setattr(guards, "guard_text_content", slow_guard)

    _drive("remember", {"content": "a short note"}, _ok)

    row = _rows(ledger_dir)[0]
    assert row["outcome"] == "ok"
    assert row["duration_ms"] < 25, "the leaf itself was fast"
    assert row["total_ms"] >= 50, "the pre-leaf cost has to show up somewhere"


def test_a_refusal_still_reports_how_long_the_caller_waited(ledger_dir: Path) -> None:
    """A refused call is exactly the one whose latency you want: "it was locked
    for 41 seconds and then refused" is the sentence the ledger has to support."""

    async def call_next(_context):
        time.sleep(0.05)
        command_surface._log_tool_failure(
            tool="remember",
            request_id=command_surface.mcp_request_id(),
            code="MUTATION_BUSY",
            duration_ms=50.0,
            message="another mutation holds the lock",
        )
        return {"ok": False, "error": {"code": "MUTATION_BUSY"}}

    _drive("remember", {"title": "x"}, call_next)

    row = _rows(ledger_dir)[0]
    assert row["outcome"] == "refused"
    assert row["error_code"] == "MUTATION_BUSY"
    assert row["total_ms"] >= 50


def test_the_request_size_is_recorded_so_a_slow_call_is_interpretable(
    ledger_dir: Path,
) -> None:
    _drive("remember", {"content": "x" * 5_000}, _ok)
    big = _rows(ledger_dir)[0]["request_bytes"]

    call_ledger.reset_chain_cache()
    (ledger_dir / "ledger.jsonl").unlink()
    _drive("remember", {"content": "x"}, _ok)
    small = _rows(ledger_dir)[0]["request_bytes"]

    assert big > 5_000 > small


# ------------------------------------------------------------- who is calling


def _fake_mcp_client(monkeypatch, *, name: str, version: str) -> None:
    """Stand in for the MCP initialize handshake's `clientInfo`."""
    import fastmcp.server.dependencies as dependencies
    from mcp.types import ClientCapabilities, Implementation, InitializeRequestParams

    context = SimpleNamespace(
        session=SimpleNamespace(
            client_params=InitializeRequestParams(
                protocol_version="2025-11-25",
                capabilities=ClientCapabilities(),
                client_info=Implementation(name=name, version=version),
            )
        ),
        session_id="sess-1234",
        transport="stdio",
    )
    monkeypatch.setattr(dependencies, "get_context", lambda: context)
    monkeypatch.setattr(dependencies, "get_http_headers", lambda **_kw: {})


def test_caller_identity_comes_from_the_handshake(monkeypatch) -> None:
    """The server already learns which client is calling, then discards it.

    Recovering it is the whole point: a vault served to claude.ai, ChatGPT,
    Codex and a local CLI at once otherwise produces one undifferentiated
    stream, and "which client's calls are failing?" is unanswerable.
    """
    _fake_mcp_client(monkeypatch, name="claude-ai", version="1.2.3")

    identity = command_surface.mcp_caller_identity()
    assert identity["client_name"] == "claude-ai"
    assert identity["client_version"] == "1.2.3"
    # stdio: the connection's own session, not the id the framework minted for this call.
    assert identity["session_id"] == command_surface.mcp_session_id() != "sess-1234"
    assert identity["transport"] == "stdio"
    assert identity["app_id"] is None

    import fastmcp.server.dependencies as dependencies

    monkeypatch.setattr(dependencies, "get_http_headers", lambda **_kw: {"user-agent": "client/1"})
    over_http = command_surface.mcp_caller_identity()
    assert over_http["transport"] == "http"
    assert over_http["session_id"] == "sess-1234"


def test_the_row_carries_the_calling_client(ledger_dir: Path, monkeypatch) -> None:
    _fake_mcp_client(monkeypatch, name="chatgpt", version="9.9")

    _drive("browse_memory", {}, _ok)

    row = _rows(ledger_dir)[0]
    assert row["client_name"] == "chatgpt"
    assert row["client_version"] == "9.9"
    assert row["session_id"] == command_surface.mcp_session_id()


def test_identity_is_absent_rather_than_fatal_outside_a_call(ledger_dir: Path) -> None:
    """No MCP context (a CLI call, a test, a background drain) must degrade to
    null fields, never to a raise on the call path."""
    _drive("browse_memory", {}, _ok)

    row = _rows(ledger_dir)[0]
    assert row["client_name"] is None
    assert row["session_id"] is None
    assert row["app_id"] is None


# -------------------------------------------------------------------- privacy


def test_no_argument_value_ever_reaches_the_ledger(ledger_dir: Path) -> None:
    """Redaction is a property of the row builder, not of a downstream filter.

    `privacy_log`'s process-wide redactor is gated on `EXOMEM_HOSTED_CELL` and
    is simply off for local installs, so a ledger that relied on it would write
    note bodies and query text verbatim on every self-hosted machine.
    """
    sentinel = "PINEAPPLE-SENTINEL-8f3a2c-do-not-log"

    _drive(
        "remember",
        {
            "title": sentinel,
            "content": f"a body containing {sentinel}",
            "path": "Notes/x.md",
        },
        _ok,
    )

    written = "\n".join(
        p.read_text(encoding="utf-8") for p in ledger_dir.rglob("*") if p.is_file()
    )
    assert sentinel not in written

    row = _rows(ledger_dir)[0]
    assert sorted(row["arg_names"]) == ["content", "path", "title"]
    for name in ("title", "content"):
        assert row["args"][name]["len"] > 0
        assert len(row["args"][name]["sha256"]) == 64
    # The addressed page is structural, not content, and is the first thing a
    # forensic pass needs.
    assert row["target_paths"] == ["Notes/x.md"]


def test_content_private_logging_redacts_names_paths_and_hashes(
    ledger_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under content-private logging (design D1.2), a target path and a
    caller-chosen argument name are both content, and a sha256 of a short
    guessed value is an offline confirmation oracle for it -- a hosted or
    cloud row keeps none of the three, only positional names and lengths."""
    import hashlib

    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    sentinel = "blue-port-bookkeeping-record"

    _drive(
        "remember",
        {"title": "T", "content": sentinel, "path": "Notes/x.md"},
        _ok,
    )

    written = "\n".join(
        p.read_text(encoding="utf-8") for p in ledger_dir.rglob("*") if p.is_file()
    )
    assert sentinel not in written

    row = _rows(ledger_dir)[0]
    assert row["target_paths"] == []
    assert sorted(row["arg_names"]) == ["arg0", "arg1", "arg2"]
    for name in row["arg_names"]:
        assert "sha256" not in row["args"][name]
        assert row["args"][name]["len"] > 0
    # Not merely that the sentinel is absent -- an offline hash of a *guessed*
    # value would otherwise confirm it, which is exactly what a bare "the
    # value itself is redacted" check would miss.
    guessed = hashlib.sha256(call_ledger.canonical_json({"v": sentinel})).hexdigest()
    assert guessed not in written


def test_a_credential_in_an_argument_never_reaches_the_ledger(ledger_dir: Path) -> None:
    """Secrets arrive as ordinary argument values -- a token pasted into a note,
    a connection string in an edit. Hashing every value by construction is what
    makes that safe; a denylist of secret-looking argument *names* would not be."""
    secret = "sk-live-51H9zzzQQQ-REAL-LOOKING-CREDENTIAL"  # noqa: S105 - test sentinel

    _drive(
        "edit_memory",
        {
            "path": "Notes/x.md",
            "operation": {
                "kind": "replace_body",
                "new_body": f"export EXOMEM_REST_API_KEY={secret}",
            },
        },
        _ok,
    )

    written = "\n".join(
        p.read_text(encoding="utf-8") for p in ledger_dir.rglob("*") if p.is_file()
    )
    assert secret not in written
    # Nested structures are hashed whole, so a value buried inside an argument
    # object is covered by the same construction as a top-level one.
    assert "new_body" not in written


def test_a_malformed_edit_is_recorded_rather_than_lost(ledger_dir: Path) -> None:
    """`edit_memory` normalizes its operation ahead of both the guard and the
    leaf, so a rejection there is the earliest way a call can end -- and the one
    that would otherwise leave no durable record of having been attempted."""

    async def call_next(_context):
        raise AssertionError("normalization should have rejected before the leaf")

    with pytest.raises(ValueError):
        _drive("edit_memory", {"path": "Notes/x.md", "operation": {}}, call_next)

    rows = _rows(ledger_dir)
    assert len(rows) == 1
    assert rows[0]["tool"] == "edit_memory"
    assert rows[0]["outcome"] == "refused"
    assert rows[0]["error_code"] == "INVALID_EDIT"
    assert rows[0]["target_paths"] == ["Notes/x.md"]


def test_identical_arguments_hash_identically(ledger_dir: Path) -> None:
    """Shape-matching across calls is the point: it answers "is this client
    retrying the same call?" without recording what the call said."""
    _drive("ask_memory", {"query": "same"}, _ok)
    _drive("ask_memory", {"query": "same"}, _ok)
    _drive("ask_memory", {"query": "different"}, _ok)

    rows = _rows(ledger_dir)
    assert rows[0]["args"]["query"]["sha256"] == rows[1]["args"]["query"]["sha256"]
    assert rows[0]["args"]["query"]["sha256"] != rows[2]["args"]["query"]["sha256"]


def test_the_caller_principal_is_a_hash_never_a_credential(
    ledger_dir: Path, monkeypatch
) -> None:
    """The per-principal identity stays hashed and separate from the client
    identity: one names software, the other could name a person."""
    import fastmcp.server.dependencies as dependencies

    token = "raw-bearer-credential-do-not-log"  # noqa: S105 - test sentinel
    monkeypatch.setattr(dependencies, "get_access_token", lambda: None)
    monkeypatch.setattr(
        dependencies, "get_http_headers", lambda **_kw: {"authorization": f"Bearer {token}"}
    )

    _drive("browse_memory", {}, _ok)

    row = _rows(ledger_dir)[0]
    assert row["caller_principal_hash"].startswith("bearer:")
    assert token not in (ledger_dir / "ledger.jsonl").read_text(encoding="utf-8")


def test_the_file_mode_is_restrictive_where_the_platform_has_one(
    ledger_dir: Path,
) -> None:
    _drive("browse_memory", {}, _ok)

    mode = (ledger_dir / "ledger.jsonl").stat().st_mode & 0o777
    if os.name == "nt":
        pytest.skip("Windows has no meaningful POSIX mode")
    assert mode == 0o600


def test_the_append_does_not_fsync_on_the_call_path(ledger_dir: Path, monkeypatch) -> None:
    """The append is diagnostic and sits in every call's critical section, so
    its budget is microseconds. Rows lost to a hard crash stay detectable as a
    later `sequence` gap -- which is why durability here is not worth a sync."""
    calls: list[int] = []
    monkeypatch.setattr(os, "fsync", lambda fd: calls.append(fd))

    _drive("browse_memory", {}, _ok)

    assert calls == []
    assert len(_rows(ledger_dir)) == 1


def test_an_oversized_argument_cannot_produce_an_unbounded_row(ledger_dir: Path) -> None:
    _drive("ask_memory", {"query": "q" * 500_000}, _ok)

    assert (ledger_dir / "ledger.jsonl").stat().st_size < 4_096


# ---------------------------------------------------------------------- chain


def test_rows_chain_on_the_previous_row_hash(ledger_dir: Path) -> None:
    for _ in range(3):
        _drive("browse_memory", {}, _ok)

    rows = _rows(ledger_dir)
    assert [row["sequence"] for row in rows] == [1, 2, 3]
    assert rows[0]["prev_hash"] == call_ledger.GENESIS_HASH
    assert rows[1]["prev_hash"] == rows[0]["row_hash"]
    assert rows[2]["prev_hash"] == rows[1]["row_hash"]
    assert call_ledger.verify(ledger_dir / "ledger.jsonl") == []


def test_a_dropped_row_is_detectable(ledger_dir: Path) -> None:
    """Eviction has to be visible. The prose call log's failure mode is that a
    burst scrolls the lines you need out of the retained window, silently."""
    for _ in range(3):
        _drive("browse_memory", {}, _ok)

    path = ledger_dir / "ledger.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")

    problems = call_ledger.verify(path)
    assert problems, "removing a row must not verify clean"
    assert any("sequence" in problem or "prev_hash" in problem for problem in problems)


def test_an_edited_row_is_detectable(ledger_dir: Path) -> None:
    _drive("remember", {"title": "x"}, _ok)

    path = ledger_dir / "ledger.jsonl"
    record = json.loads(path.read_text(encoding="utf-8").strip())
    record["tool"] = "something_else"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    assert any("row_hash" in problem for problem in call_ledger.verify(path))


def test_a_restart_resumes_the_chain_rather_than_restarting_it(ledger_dir: Path) -> None:
    """A fresh process must not reset `sequence`, or every restart would look
    exactly like the tampering the sequence exists to detect."""
    _drive("browse_memory", {}, _ok)
    call_ledger.reset_chain_cache()  # what a process restart looks like
    _drive("browse_memory", {}, _ok)

    rows = _rows(ledger_dir)
    assert [row["sequence"] for row in rows] == [1, 2]
    assert rows[1]["prev_hash"] == rows[0]["row_hash"]
    assert call_ledger.verify(ledger_dir / "ledger.jsonl") == []


# ------------------------------------------------------------------- rotation


def test_rotation_bounds_the_live_file_without_breaking_the_chain(
    ledger_dir: Path, monkeypatch
) -> None:
    """`queries.jsonl` and its siblings have no rotation and are already
    multi-MB. Bounding the live file must not cost the continuity that makes a
    gap detectable, so archived rows stay byte-exact and `sequence` runs on."""
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_ROTATE_BYTES", "1")
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_KEEP_ROWS", "2")

    for _ in range(6):
        _drive("browse_memory", {}, _ok)

    live = _rows(ledger_dir)
    archives = list((ledger_dir / "ledger-archive").glob("ledger-*.jsonl"))
    assert archives, "an oversized live file must produce an archive"

    archived: list[dict] = []
    for archive in archives:
        archived.extend(
            json.loads(line)
            for line in archive.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    # Archive filenames are content-addressed, not ordered; the rows carry the
    # order.
    archived.sort(key=lambda row: row["sequence"])
    everything = archived + live
    assert [row["sequence"] for row in everything] == list(
        range(1, len(everything) + 1)
    ), "sequence must not reset"
    assert len(live) <= 3, "the live file must actually be bounded"
    # The chain spans the boundary: the last archived row is the first retained
    # row's predecessor, so a verifier walks archive-then-live continuously.
    assert live[0]["prev_hash"] == archived[-1]["row_hash"]


# ---------------------------------------------------------------- containment


def test_a_ledger_failure_never_breaks_the_call(ledger_dir: Path, monkeypatch) -> None:
    def explode(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(call_ledger, "append_row", explode)

    async def call_next(_context):
        return {"ok": True, "value": 42}

    assert _drive("remember", {}, call_next) == {"ok": True, "value": 42}
    assert _rows(ledger_dir) == []


def test_the_kill_switch_silences_the_ledger(ledger_dir: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_CALL_LEDGER", "1")

    _drive("remember", {"title": "x"}, _ok)
    assert _rows(ledger_dir) == []


def test_the_ledger_writes_outside_the_vault(tmp_path: Path, monkeypatch) -> None:
    """A ledger that cannot write during a read-only-vault incident cannot
    explain that incident, so its location is independent of the vault and sits
    beside the journals an operator is already reading."""
    monkeypatch.delenv("EXOMEM_CALL_LEDGER_DIR", raising=False)
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(tmp_path / "hostlogs"))
    call_ledger.reset_chain_cache()

    resolved = call_ledger.ledger_path()
    assert resolved.parent == (tmp_path / "hostlogs")
    assert resolved.name == "ledger.jsonl"


# ------------------------------------------------- derived-work phases (Lane 5)
#
# The change moves the expensive half of a governed write behind durable
# custody. Two boundary clocks can prove the call got faster and cannot say
# whether the work converged, so the ledger has to carry the phases and the
# diagnostics that answer that -- and carry them content-free, because this
# file is operator-readable and lives outside the vault.


def test_derived_phase_vocabulary_is_closed_and_content_free() -> None:
    """Phase names are a closed set, so no producer can name a path."""
    names = call_ledger.DERIVED_PHASES
    assert isinstance(names, frozenset)
    assert names == {
        "derived.acknowledgement",
        "derived.advisory_execute",
        "derived.canonical_commit",
        "derived.component_completion",
        "derived.component_dispatch",
        "derived.pending_visibility",
        "derived.deferred_index_store",
        "derived.fanout",
        "derived.post_canonical",
        "derived.receipt_prepare",
        "derived.receipt_proof",
        "derived.terminal_persist",
    }
    for name in names:
        assert name.startswith("derived.")
        assert "/" not in name and " " not in name


def test_a_derived_phase_is_recorded_on_the_calls_ledger_row(
    ledger_dir: Path,
) -> None:
    """A phase reported during a call reaches that call's row as a span."""
    from exomem import call_spans

    async def leaf(_context):
        with call_spans.span("derived.acknowledgement"):
            pass
        return {"ok": True}

    _drive("remember", {"content": "x"}, leaf)
    row = _rows(ledger_dir)[-1]
    spans = {span["name"]: span for span in row["spans"]}
    assert "derived.acknowledgement" in spans, row["spans"]
    assert spans["derived.acknowledgement"]["count"] == 1
    assert isinstance(spans["derived.acknowledgement"]["ms"], float)


def test_a_recall_row_attributes_its_time_by_stage(
    ledger_dir: Path, vault: Path
) -> None:
    """A slow recall row must say which stage was slow, without a second call."""
    from exomem import commands

    marker = "zzledgerprobetokenzz"

    async def leaf(_context):
        return commands.op_ask_memory(vault, query=marker, limit=5)

    result = _drive("ask_memory", {"query": marker, "limit": 5}, leaf)

    row = _rows(ledger_dir)[-1]
    recall = [span for span in row["spans"] if span["name"].startswith("recall.")]
    assert recall, row["spans"]
    for span in recall:
        assert {"name", "count", "ms"} <= span.keys()
        if "thread_cpu_us" in span.get("fields", {}):
            assert span["fields"]["thread_cpu_us"] >= 0
        assert isinstance(span["ms"], float)
        # Stage names only: no path, no query, no excerpt can ride in one.
        assert "/" not in span["name"] and " " not in span["name"]
    written = json.dumps(row)
    assert marker not in written
    assert ".md" not in written.replace('"target_paths": []', "")
    # Collected, but not returned: response inclusion stays opt-in.
    assert "timings" not in (result if isinstance(result, dict) else {})


def test_a_cli_find_records_no_spans(vault: Path) -> None:
    """Outside an MCP call the instrumentation reports nothing at all."""
    from exomem import call_spans, commands

    call_spans.reset()
    commands.op_find(vault, query="metabolism", limit=5)

    # No token was minted, so nothing may have been keyed under any token.
    assert call_spans._SPANS == {}


def test_nested_recall_stages_reach_the_ledger(
    ledger_dir: Path, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import commands, embeddings

    monkeypatch.setattr(
        embeddings, "rerank_pairs", lambda _query, passages: [0.0] * len(passages)
    )
    # The row keeps its slowest spans; in a fixture vault every span is fast,
    # so which ones survive the cap is timing noise, not what this checks.
    monkeypatch.setattr(call_ledger, "_MAX_SPANS", 256)

    async def leaf(_context):
        return commands.op_ask_memory(
            vault, query="metabolism", rerank=True, deep=True, graph_enrich=True, limit=5
        )

    result = _drive("ask_memory", {"query": "metabolism", "rerank": True}, leaf)
    spans = {span["name"] for span in _rows(ledger_dir)[-1]["spans"]}
    assert {
        "recall.semantic.search", "recall.bm25", "recall.rerank", "recall.graph_enrich"
    } <= spans
    assert "timings" not in result


def test_a_budgeted_row_records_what_the_budget_did(
    ledger_dir: Path, vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import commands, request_budget

    monkeypatch.setenv(request_budget.BUDGET_ENV_VAR, "0.001")
    request_budget.reset_resolution_cache()

    async def leaf(_context):
        return commands.op_ask_memory(vault, query="metabolism", rerank=True, limit=5)

    _drive("ask_memory", {"query": "metabolism", "rerank": True}, leaf)

    row = _rows(ledger_dir)[-1]
    assert row["budget"] == {
        "seconds": 0.001,
        "remaining_ms": 0,
        "skipped": ["rerank"],
    }


def test_every_budgeted_row_records_the_budget_even_when_nothing_was_skipped(
    ledger_dir: Path,
) -> None:
    """The row is an operator instrument, not a message to a client.

    "50 seconds, 49 left, nothing skipped" is the reading that says a reserve
    is not yet costing calls — which is exactly what the PROVISIONAL reserves
    have to be tuned against.
    """
    from exomem import request_budget

    async def leaf(_context):
        return {"ok": True}

    _drive("remember", {"content": "x"}, leaf)

    budget = _rows(ledger_dir)[-1]["budget"]
    assert budget["seconds"] == request_budget.MCP_REQUEST_BUDGET_SECONDS
    assert budget["skipped"] == []
    assert budget["remaining_ms"] > 0


def test_a_reconcile_class_row_carries_no_budget_block(ledger_dir: Path) -> None:
    """No budget applied, so there is nothing for the row to report."""

    async def leaf(_context):
        return {"ok": True}

    _drive("maintain_memory", {"mode": "reconcile"}, leaf)

    assert _rows(ledger_dir)[-1]["budget"] is None


def test_derived_counters_are_closed_named_and_reset_able() -> None:
    call_ledger.reset_derived_counters()
    assert call_ledger.derived_counters() == dict.fromkeys(
        call_ledger.DERIVED_COUNTERS, 0
    )
    call_ledger.note_derived_event("advisory_vectors_reused")
    call_ledger.note_derived_event("advisory_vectors_reused")
    call_ledger.note_derived_event("component_completed")
    counters = call_ledger.derived_counters()
    assert counters["advisory_vectors_reused"] == 2
    assert counters["component_completed"] == 1

    # An unknown counter creates no field and counts nothing. It is dropped
    # rather than raised: every caller is a worker holding exact custody, and
    # an exception there would cost a claim rotation for a miscounted event.
    before = call_ledger.derived_counters()
    call_ledger.note_derived_event("Knowledge Base/Notes/leak.md")
    after = call_ledger.derived_counters()
    assert after == before
    assert set(after) == set(call_ledger.DERIVED_COUNTERS)
    assert "Knowledge Base/Notes/leak.md" not in after
    call_ledger.reset_derived_counters()


def test_derived_diagnostics_are_content_free(tmp_path: Path, monkeypatch) -> None:
    """Counts, ages, depths and closed codes only -- never a path or a title."""
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    vault_root = tmp_path / "vault"
    (vault_root / "Knowledge Base" / "Notes").mkdir(parents=True)
    secret = "Knowledge Base/Notes/confidential-title.md"
    (vault_root / secret).write_text("---\ntitle: Secret\n---\n", encoding="utf-8")

    diagnostics = call_ledger.derived_diagnostics(vault_root)
    rendered = json.dumps(diagnostics, sort_keys=True, default=str)
    for token in ("confidential-title", "Knowledge Base", "Secret", str(vault_root)):
        assert token not in rendered, (token, rendered)

    assert diagnostics["fast_durable_ack"] in {"active", "inactive"}
    assert diagnostics["due_components"] == 0
    assert diagnostics["recoverable_batches"] == 0
    assert isinstance(diagnostics["counters"], dict)
    assert isinstance(diagnostics["pending_visibility"], dict)
    assert set(diagnostics["last_drain_pass"]) == {
        "at_age_seconds",
        "claimed",
        "completed",
        "max_attempt_count",
        "oldest_due_age_seconds",
    }


def test_derived_diagnostics_report_the_capability_flag_both_ways(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "state"))
    vault_root = tmp_path / "vault"
    (vault_root / "Knowledge Base").mkdir(parents=True)

    monkeypatch.setenv("EXOMEM_FAST_DURABLE_ACK", "1")
    assert call_ledger.derived_diagnostics(vault_root)["fast_durable_ack"] == "active"
    monkeypatch.setenv("EXOMEM_FAST_DURABLE_ACK", "0")
    assert (
        call_ledger.derived_diagnostics(vault_root)["fast_durable_ack"] == "inactive"
    )


def test_an_artifact_write_records_what_landed_and_no_handle(
    ledger_dir: Path, vault: Path, tmp_path: Path, monkeypatch
) -> None:
    """The ledger has to be able to say what an artifact write put in the vault.

    Its targets come from path-shaped *arguments*, and `preserve_artifacts` has
    none: the live rows all carried `target_paths: []`. The outcome knows, and
    handing it over stays within the never-values rule -- a vault-relative path
    is exactly what a note write already records.
    """
    import hashlib
    import uuid

    from exomem import client_artifacts, commands

    signed_url = "https://files.example/SIGNED-HANDLE-do-not-log?sig=abc123"
    payload = b"artifact bytes"

    def stage_artifact(file, _budget, **_kwargs):
        if file["file_id"] == "file-two":
            raise client_artifacts.SafeFetchError(
                "SAFE_FETCH_FAILED", "download could not be retrieved"
            )
        staged = tmp_path / f"{file['file_id']}-{uuid.uuid4().hex}.bin"
        staged.write_bytes(payload + file["file_id"].encode())
        return client_artifacts.StagedArtifact(
            file_id=file["file_id"],
            path=staged,
            size=staged.stat().st_size,
            sha256=hashlib.sha256(staged.read_bytes()).hexdigest(),
            content_type="application/octet-stream",
            filename=f"{file['file_id']}.bin",
        )

    monkeypatch.setattr(client_artifacts, "stage_artifact", stage_artifact)
    monkeypatch.setattr(client_artifacts, "mark_active_mutation_committed", lambda: None)
    monkeypatch.setattr(
        client_artifacts,
        "active_manager",
        lambda: SimpleNamespace(mutation_guard=lambda *_a, **_k: nullcontext()),
    )

    arguments = {
        "scope": "case",
        "category": "raw",
        "files": [
            {"download_url": signed_url, "file_id": "file-one"},
            {"download_url": signed_url, "file_id": "file-two"},
            {"download_url": signed_url, "file_id": "file-three"},
        ],
    }

    async def call_next(_context):
        return commands.op_preserve_artifacts(
            vault,
            scope=arguments["scope"],
            category=arguments["category"],
            files=arguments["files"],
        )

    result = _drive("preserve_artifacts", arguments, call_next)

    assert result["summary"] == {"stored": 2, "already_stored": 0, "failed": 1}
    row = _rows(ledger_dir)[0]
    assert row["target_paths"] == [
        "Knowledge Base/Evidence/case/raw/file-one.bin",
        "Knowledge Base/Evidence/case/raw/file-three.bin",
    ]

    written = "\n".join(
        p.read_text(encoding="utf-8") for p in ledger_dir.rglob("*") if p.is_file()
    )
    assert "SIGNED-HANDLE-do-not-log" not in written
    assert "sig=abc123" not in written
    assert "file-one" not in row["args"]
    for handle_field in ("download_url", "file_id"):
        assert handle_field not in written
    assert "artifact bytes" not in written


# ------------------------------------------------------------- principal kind

_REMOTE_BASE = "https://memory.example.test"
_REMOTE_ID = 4242


def _remote_session(monkeypatch, *, bound: bool) -> str:
    """Present a synthetic durable-session token; return its remote scope."""
    import hashlib

    import fastmcp.server.dependencies as dependencies

    from exomem.session_oauth import ExomemSessionAccessToken

    monkeypatch.setenv("EXOMEM_BASE_URL", _REMOTE_BASE)
    monkeypatch.setenv("EXOMEM_GITHUB_USER_ID", str(_REMOTE_ID))
    if bound:
        monkeypatch.setenv("EXOMEM_OWNER_OAUTH_SUBJECT", f"github:{_REMOTE_ID}")
    else:
        monkeypatch.delenv("EXOMEM_OWNER_OAUTH_SUBJECT", raising=False)
    token = ExomemSessionAccessToken(
        token="synthetic-session-token",
        client_id="synthetic-client",
        scopes=["exomem:read"],
        claims={
            "sub": str(_REMOTE_ID),
            "github_user_id": _REMOTE_ID,
            "github_login": "example-owner",
            "iss": _REMOTE_BASE,
            "aud": f"{_REMOTE_BASE}/mcp",
        },
    )
    monkeypatch.setattr(dependencies, "get_access_token", lambda: token)
    digest = hashlib.sha256(f"{_REMOTE_BASE}\0{_REMOTE_ID}".encode()).hexdigest()
    return f"principal:{digest}"


def test_a_local_owner_call_is_labelled_owner(ledger_dir: Path) -> None:
    _drive("browse_memory", {}, _ok)
    assert _rows(ledger_dir)[0]["principal_kind"] == "owner"


def test_a_bound_remote_owner_call_is_labelled_owner_oauth_with_the_remote_hash(
    ledger_dir: Path, monkeypatch
) -> None:
    remote_scope = _remote_session(monkeypatch, bound=True)
    _drive("browse_memory", {}, _ok)
    row = _rows(ledger_dir)[0]
    assert row["principal_kind"] == "owner-oauth"
    # Labelled remote: the caller hash is the remote identity's, never "owner".
    assert row["caller_principal_hash"] == remote_scope
    assert "example-owner" not in (ledger_dir / "ledger.jsonl").read_text(encoding="utf-8")


def test_an_unbound_remote_call_is_labelled_principal(ledger_dir: Path, monkeypatch) -> None:
    remote_scope = _remote_session(monkeypatch, bound=False)
    _drive("browse_memory", {}, _ok)
    row = _rows(ledger_dir)[0]
    assert row["principal_kind"] == "principal"
    assert row["caller_principal_hash"] == remote_scope


def test_an_unresolved_remote_call_is_labelled_unresolved(ledger_dir: Path, monkeypatch) -> None:
    import fastmcp.server.dependencies as dependencies

    monkeypatch.setattr(dependencies, "get_access_token", lambda: None)
    monkeypatch.setattr(dependencies, "get_http_headers", lambda **_kw: {"host": "memory"})
    _drive("browse_memory", {}, _ok)
    assert _rows(ledger_dir)[0]["principal_kind"] == "unresolved"


def test_principal_kind_is_part_of_the_hashed_row(ledger_dir: Path, monkeypatch) -> None:
    _remote_session(monkeypatch, bound=True)
    _drive("browse_memory", {}, _ok)
    assert call_ledger.verify(ledger_dir / "ledger.jsonl") == []
    row = _rows(ledger_dir)[0]
    tampered = dict(row, principal_kind="owner")
    assert call_ledger.row_hash(tampered) != row["row_hash"]


def test_a_local_ingress_call_is_labelled_owner_local(ledger_dir: Path, monkeypatch) -> None:
    import fastmcp.server.dependencies as dependencies

    from exomem import local_ingress

    token = local_ingress.LocalIngressAccessToken(
        token="synthetic-local-token",
        client_id="home",
        scopes=list(local_ingress.LOCAL_SCOPES),
        claims={"sub": "local:synthetic", "iss": local_ingress.LOCAL_ISSUER},
    )
    grant = local_ingress.LocalGrant(
        bearer="synthetic-local-token", access_token=token, client_id="home", session_id="s"
    )
    monkeypatch.setattr(dependencies, "get_access_token", lambda: token)
    bound = local_ingress._GRANT.set(grant)
    try:
        _drive("browse_memory", {}, _ok)
    finally:
        local_ingress._GRANT.reset(bound)
    row = _rows(ledger_dir)[0]
    assert row["principal_kind"] == "owner-local"
    assert "synthetic-local-token" not in (ledger_dir / "ledger.jsonl").read_text(encoding="utf-8")


@pytest.fixture
def activation_owner():
    from exomem.governance.principal import owner_principal, request_scope

    with request_scope(owner_principal(surface="mcp")):
        yield


@pytest.mark.parametrize("packet,outcome,observation", [
    ({"abstained": True, "abstention": {"reason": "unresolved"}}, "abstained",
     {"abstained": True, "reason": "unresolved", "budget_limited": False}),
    ({"abstained": True, "abstention": {"reason": "private text"}}, "abstained",
     {"abstained": True, "reason": None, "budget_limited": False}),
    ({"abstained": True, "request_budget": {"applied": "true"}}, "abstained", None),
    ({"abstained": "true"}, "ok", None),
    ({"abstained": False, "request_budget": {"applied": True}}, "ok",
     {"abstained": False, "reason": None, "budget_limited": True}),
])
def test_activation_wrapper_thread_stores_strict_final_flags(
    vault, ledger_dir, activation_owner, packet, outcome, observation
):
    from dataclasses import replace

    from exomem import commands

    command = next(item for item in commands.COMMANDS if item.name == "activate_context")
    command = replace(command, leaf=lambda _vault, **_kwargs: packet)
    wrapper = command_surface.bind_vault(command.leaf, vault, command=command)

    async def dispatch(_context):
        return await asyncio.to_thread(wrapper, turn="fixture")

    served = _drive("activate_context", {"turn": "fixture"}, dispatch)
    assert served == packet
    row = _rows(ledger_dir)[0]
    assert row["outcome"] == outcome
    assert row["error_code"] is None
    assert row.get("activation_result") == observation
    assert ("activation_result" in row) == (observation is not None)


def test_activation_observation_uses_the_final_postfilter(
    vault, ledger_dir, activation_owner, monkeypatch
):
    from dataclasses import replace

    from exomem import commands, writer_lease
    from exomem.governance import egress

    command = next(item for item in commands.COMMANDS if item.name == "activate_context")
    command = replace(command, leaf=lambda _vault, **_kwargs: {"abstained": False})
    monkeypatch.setattr(writer_lease, "invoke_command", lambda *_a, **_k: {"abstained": False})
    final = {"abstained": True, "abstention": {"reason": "withheld"}}
    monkeypatch.setattr(egress, "postfilter", lambda *_a, **_k: final)
    wrapper = command_surface.bind_vault(command.leaf, vault, command=command)

    async def dispatch(_context):
        return await asyncio.to_thread(wrapper, turn="fixture")

    assert _drive("activate_context", {}, dispatch) == final
    row = _rows(ledger_dir)[0]
    assert row["outcome"] == "abstained"
    assert row["activation_result"]["reason"] == "withheld"


@pytest.mark.usefixtures("anchors_packet")  # tests the rollback packet; maps is the default
def test_real_activation_exhaustion_and_useful_partial_have_distinct_rows(
    vault, ledger_dir, activation_owner, monkeypatch
):
    from copy import deepcopy
    from dataclasses import replace

    from exomem import (
        commands,
        lexstore,
        request_budget,
        working_set,
        working_set_index,
        working_set_runtime,
    )

    page = vault / "Knowledge Base" / "Products" / "Harbour Cart.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\ntype: note\nstatus: active\nupdated: 2026-10-10\n---\n\n# Harbour Cart\n\n"
        "## Summary\n\nA cart that carries supplies.\n\n## Constraints\n\n"
        "The cart carries at most 100 kg.\n", encoding="utf-8"
    )
    monkeypatch.setenv("EXOMEM_DISABLE_RELEVANCE_CHECK", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_WORKING_SET_BAND", "1")
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    command = next(item for item in commands.COMMANDS if item.name == "activate_context")
    wrapper = command_surface.bind_vault(command.leaf, vault, command=command)
    turn = "What are the Harbour Cart constraints?"

    async def dispatch(_context):
        return await asyncio.to_thread(wrapper, turn=turn)

    useful = _drive("activate_context", {"turn": turn}, dispatch)
    assert useful["abstained"] is False and useful["budget"]["used_chars"] > 0

    async def expired_dispatch(context):
        request_budget.current().deadline = time.monotonic() - 1
        return await dispatch(context)

    entry = _drive("activate_context", {"turn": turn}, expired_dispatch)
    assert entry["abstained"] is True
    assert entry["request_budget"]["skipped"] == ["working_set.freshness"]
    real_compile = working_set.compile_packet

    def expired_compile(*args, **kwargs):
        request_budget.current().deadline = time.monotonic() - 1
        return real_compile(*args, **kwargs)

    working_set_runtime.reset_caches_for_tests()
    with monkeypatch.context() as patch:
        patch.setattr(working_set, "compile_packet", expired_compile)
        compiler = _drive("activate_context", {"turn": turn}, dispatch)
    assert compiler["abstained"] is True
    assert compiler["request_budget"]["skipped"] == ["working_set.semantic"]

    def partial_leaf(_vault, **_kwargs):
        request_budget.current().note_skipped("working_set.conversation")
        packet = deepcopy(useful)
        packet["request_budget"] = request_budget.current().as_response_block()
        return packet

    partial_command = replace(command, leaf=partial_leaf)
    wrapper = command_surface.bind_vault(partial_leaf, vault, command=partial_command)
    partial = _drive("activate_context", {"turn": turn}, dispatch)
    assert partial["abstained"] is False and partial["budget"]["used_chars"] > 0
    rows = _rows(ledger_dir)
    assert [row["outcome"] for row in rows] == ["ok", "abstained", "abstained", "ok"]
    assert [row["activation_result"]["budget_limited"] for row in rows] == [False, True, True, True]
    assert all(row["error_code"] is None for row in rows)
    assert call_ledger.verify(ledger_dir / "ledger.jsonl") == []


@pytest.mark.parametrize("failure", ["refusal", "dispatch", "serialization"])
def test_activation_failure_takes_precedence_over_the_observation(
    vault, ledger_dir, activation_owner, failure
):
    from dataclasses import replace

    from exomem import commands
    from exomem.cli_ops import OpError

    command = next(item for item in commands.COMMANDS if item.name == "activate_context")

    def leaf(_vault, **_kwargs):
        command_surface.current_activation_result().capture({"abstained": True})
        if failure == "refusal":
            raise OpError("INVALID_ANCHOR", "fixture refusal")
        if failure == "dispatch":
            raise RuntimeError("fixture dispatch error")
        return {"abstained": True, "material": object()}

    command = replace(command, leaf=leaf)
    wrapper = command_surface.bind_vault(leaf, vault, command=command)

    async def dispatch(_context):
        packet = await asyncio.to_thread(wrapper, turn="fixture")
        if failure == "serialization":
            json.dumps(packet)
        return packet

    if failure == "refusal":
        assert _drive("activate_context", {}, dispatch)["success"] is False
    else:
        with pytest.raises((RuntimeError, TypeError)):
            _drive("activate_context", {}, dispatch)
    row = _rows(ledger_dir)[0]
    assert row["outcome"] == ("refused" if failure == "refusal" else "error")
    if failure != "refusal":
        assert "activation_result" not in row


def test_observation_capture_and_ledger_failure_do_not_change_the_response(
    vault, ledger_dir, activation_owner, monkeypatch
):
    from dataclasses import replace

    from exomem import activation_result, commands

    def broken(*_a, **_k):
        raise RuntimeError("fixture telemetry failure")

    packet = {"abstained": True}
    command = next(item for item in commands.COMMANDS if item.name == "activate_context")
    command = replace(command, leaf=lambda _vault, **_k: packet)
    wrapper = command_surface.bind_vault(command.leaf, vault, command=command)
    monkeypatch.setattr(activation_result.Observation, "capture", broken)
    monkeypatch.setattr(call_ledger, "canonical_json", broken)

    async def dispatch(_context):
        return await asyncio.to_thread(wrapper, turn="fixture")

    assert _drive("activate_context", {}, dispatch) == packet
    assert _rows(ledger_dir) == []


def test_mixed_legacy_and_observed_activation_chain_keeps_historical_hashes(ledger_dir):
    legacy = call_ledger.record_call(
        request_id="fixture", tool="activate_context", outcome="ok", duration_ms=1
    )
    legacy_bytes = (ledger_dir / "ledger.jsonl").read_bytes()
    observed = call_ledger.record_call(
        request_id="fixture", tool="activate_context", outcome="abstained", duration_ms=1,
        activation_result={"abstained": True, "reason": "unresolved", "budget_limited": False}
    )
    assert "activation_result" not in legacy
    assert observed["prev_hash"] == legacy["row_hash"]
    assert (ledger_dir / "ledger.jsonl").read_bytes().startswith(legacy_bytes)
    assert call_ledger.verify(ledger_dir / "ledger.jsonl") == []


@pytest.mark.parametrize("cancel_first", [False, True])
def test_equal_request_ids_and_late_workers_cannot_exchange_activation_results(
    vault, ledger_dir, activation_owner, monkeypatch, cancel_first
):
    import threading
    from dataclasses import replace

    from exomem import commands

    monkeypatch.setattr(command_surface, "mcp_request_id", lambda: "same-client-id")
    release = threading.Event()
    command = next(item for item in commands.COMMANDS if item.name == "activate_context")

    async def run():
        entered, finished = asyncio.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()

        def leaf(_vault, turn="", **_kwargs):
            if turn == "first":
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(timeout=10), "the test must release its worker"
                # Observe the abandoned cell before a cancelled worker finishes.
                command_surface.current_activation_result().capture({"abstained": True})
                loop.call_soon_threadsafe(finished.set)
            return {"abstained": turn == "first"}

        wrapper = command_surface.bind_vault(leaf, vault, command=replace(command, leaf=leaf))

        async def dispatch(context):
            return await asyncio.to_thread(wrapper, **context.message["params"]["arguments"])

        def context(turn):
            return SimpleNamespace(message={"params": {
                "name": "activate_context", "arguments": {"turn": turn}
            }})

        middleware = server_module.CallTraceMiddleware()
        first = asyncio.create_task(middleware.on_call_tool(context("first"), dispatch))
        try:
            await asyncio.wait_for(entered.wait(), timeout=10)
            if cancel_first:
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
            second = await middleware.on_call_tool(context("second"), dispatch)
            assert second["abstained"] is False
            release.set()
            if not cancel_first:
                assert (await first)["abstained"] is True
            await asyncio.wait_for(finished.wait(), timeout=10)
            assert command_surface.current_activation_result() is None
        finally:
            release.set()

    asyncio.run(run())
    rows = _rows(ledger_dir)
    assert [row["request_id"] for row in rows] == ["same-client-id"] * len(rows)
    assert [row["outcome"] for row in rows] == (["ok"] if cancel_first else ["ok", "abstained"])
    assert [row["activation_result"]["abstained"] for row in rows] == (
        [False] if cancel_first else [False, True]
    )


# ------------------------------------------------------------------ schema 2


def version_1_row(*, sequence: int, prev_hash: str, tool: str, ts_utc: str) -> dict:
    """A row exactly as the version 1 builder wrote it: no `access`, no `item_count`."""
    row = {
        "schema_version": 1,
        "sequence": sequence,
        "prev_hash": prev_hash,
        "ts_utc": ts_utc,
        "request_id": "request",
        "session_id": None,
        "client_name": "claude.ai",
        "client_version": None,
        "transport": "http",
        "caller_principal_hash": None,
        "principal_kind": "owner",
        "tool": tool,
        "arg_names": [],
        "args": {},
        "target_paths": [],
        "outcome": "ok",
        "error_code": None,
        "duration_ms": 1.0,
        "total_ms": 1.0,
        "request_bytes": 0,
        "spans": [],
        "budget": None,
        "truncated": False,
    }
    row["row_hash"] = call_ledger.row_hash(row)
    return row


def test_an_upgraded_ledger_verifies_across_its_version_1_rows(ledger_dir: Path) -> None:
    """The first image that writes version 2 rows starts on a ledger an older image
    filled. Rejecting those rows, or breaking the chain where the versions meet,
    would make `exomem ledger verify` report tampering after every upgrade."""
    from exomem import obs_cli

    first = version_1_row(
        sequence=1, prev_hash=call_ledger.GENESIS_HASH, tool="ask_memory",
        ts_utc="2026-03-01T09:00:00.000+00:00",
    )
    second = version_1_row(
        sequence=2, prev_hash=first["row_hash"], tool="remember",
        ts_utc="2026-03-01T09:01:00.000+00:00",
    )
    ledger_dir.mkdir()
    for row in (first, second):
        call_ledger.append_row(row, path=ledger_dir / "ledger.jsonl")
    call_ledger.reset_chain_cache()  # the upgraded process starts on that file

    _drive("ask_memory", {"query": "anything"}, _ok)

    upgraded = _rows(ledger_dir)[-1]
    assert upgraded["schema_version"] == 2
    assert upgraded["prev_hash"] == second["row_hash"]
    assert obs_cli.verify_ledger() == []


def test_two_long_paths_that_share_their_start_are_two_pages(ledger_dir: Path) -> None:
    """A row bounds each path it shows. A move of two pages deep in one long folder
    must still count two pages, not one."""
    folder = "Notes/" + "a-very-long-folder-name/" * 30
    row = call_ledger.build_row(
        sequence=1, prev_hash=call_ledger.GENESIS_HASH, request_id="request",
        tool="move_memory", outcome="ok", duration_ms=1.0, access="write",
        arguments={"paths": [folder + "first.md", folder + "second.md"]},
    )

    assert row["item_count"] == 2


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="needs POSIX permissions that bind the caller",
)
def test_the_gap_marker_is_never_earlier_than_the_failure_it_marks(ledger_dir: Path) -> None:
    """A reader in another process starts its activity interval after the marker's
    time. A marker stamped before the failure, as a coarse file system clock does,
    would put the lost call back inside that interval."""
    _drive("ask_memory", {"query": "anything"}, _ok)
    path = ledger_dir / "ledger.jsonl"
    path.chmod(0o400)
    try:
        for _ in range(100):
            before = time.time_ns() // 1000 * 1000
            lost = call_ledger.record_call(
                request_id="request", tool="ask_memory", outcome="ok", duration_ms=1.0
            )
            assert lost is None, "sanity: the append failed"
            assert call_ledger.gap_marker_path().stat().st_mtime_ns >= before
    finally:
        path.chmod(0o600)


def test_a_gateway_actor_is_independent_of_the_handshake_and_absent_without_headers(monkeypatch):
    """Caller names cannot select a Home actor, even when they look like its UUID."""
    import fastmcp.server.dependencies as dependencies

    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    actor = "12345678-1234-4234-8234-123456789abc"
    _fake_mcp_client(monkeypatch, name=actor, version="1")
    assert command_surface.mcp_caller_identity()["app_id"] is None

    monkeypatch.setattr(
        dependencies, "get_http_headers", lambda **_kw: {cloud_cell.GATEWAY_APP_ID_HEADER: actor}
    )
    assert command_surface.mcp_caller_identity()["app_id"] == actor

    def unavailable(**_kw):
        raise LookupError("no HTTP request")

    monkeypatch.setattr(dependencies, "get_http_headers", unavailable)
    assert command_surface.mcp_caller_identity()["app_id"] is None



def test_a_recall_row_splits_its_catalogue_waits(
    ledger_dir: Path, vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A catalogue read that waits seconds on little thread CPU has to say where.

    On 2026-10-19 a three-row catalogue read took 2-5 s at under 0.7 s of
    thread CPU; the connect, the proof and the query were one number. Process
    CPU beside thread CPU tells contention from a blocking wait.
    """
    from exomem import commands

    warm_managed_cell(vault)
    # A slow child outranks fast spans under the row cap; here all are fast.
    monkeypatch.setattr(call_ledger, "_MAX_SPANS", 256)

    async def leaf(_context):
        return commands.op_ask_memory(vault, query="metabolism", limit=5)

    _drive("ask_memory", {"query": "metabolism", "limit": 5}, leaf)

    spans = {span["name"]: span for span in _rows(ledger_dir)[-1]["spans"]}
    for name in (
        "lexical.connect",
        "lexical.connect.guard",
        "lexical.connect.sqlite",
        "lexical.connect.owner_family",
        "lexical.readiness",
        "lexical.proof",
        "lexical.query",
        "lexical.resolver_entries",
    ):
        assert name in spans, (name, sorted(spans))
        assert isinstance(spans[name]["fields"]["process_cpu_us"], int), name
    assert spans["lexical.resolver_entries"]["fields"]["rows"] > 0


def test_a_span_with_every_field_slot_used_keeps_both_cpu_readings(
    ledger_dir: Path,
) -> None:
    """The working-set carry rungs fill all five field slots; CPU must not be clipped."""
    from exomem import call_spans

    async def leaf(_context):
        with call_spans.span("derived.full", {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5}):
            pass
        return {"ok": True}

    _drive("remember", {"content": "x"}, leaf)
    spans = {span["name"]: span for span in _rows(ledger_dir)[-1]["spans"]}
    fields = spans["derived.full"]["fields"]
    assert set(fields) == {"a", "b", "c", "d", "e", "thread_cpu_us", "process_cpu_us"}
