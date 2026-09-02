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
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import call_ledger, command_surface, guards
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

    context = SimpleNamespace(
        session=SimpleNamespace(
            client_params=SimpleNamespace(
                clientInfo=SimpleNamespace(name=name, version=version)
            )
        ),
        session_id="sess-1234",
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
    assert identity["session_id"] == "sess-1234"
    assert identity["transport"] == "stdio"


def test_the_row_carries_the_calling_client(ledger_dir: Path, monkeypatch) -> None:
    _fake_mcp_client(monkeypatch, name="chatgpt", version="9.9")

    _drive("browse_memory", {}, _ok)

    row = _rows(ledger_dir)[0]
    assert row["client_name"] == "chatgpt"
    assert row["client_version"] == "9.9"
    assert row["session_id"] == "sess-1234"


def test_identity_is_absent_rather_than_fatal_outside_a_call(ledger_dir: Path) -> None:
    """No MCP context (a CLI call, a test, a background drain) must degrade to
    null fields, never to a raise on the call path."""
    _drive("browse_memory", {}, _ok)

    row = _rows(ledger_dir)[0]
    assert row["client_name"] is None
    assert row["session_id"] is None


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
        "derived.post_canonical",
        "derived.receipt_prepare",
        "derived.receipt_proof",
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
