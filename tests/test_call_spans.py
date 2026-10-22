"""Phase timing for one MCP call.

The defect these exist to prevent is not a wrong number, it is a *quiet* one:
instrumentation that silently records nothing looks identical to a call that
spent no time anywhere. Several of these assert on absence for that reason.
"""

from __future__ import annotations

import time

import pytest

from exomem import call_ledger, call_spans, command_surface

pytestmark = pytest.mark.reads_repository_docs


@pytest.fixture(autouse=True)
def _isolate_spans():
    call_spans.reset()
    yield
    call_spans.reset()


@pytest.fixture
def token():
    handle = call_spans.MCP_CALL_TOKEN.set("test-token")
    try:
        yield "test-token"
    finally:
        call_spans.MCP_CALL_TOKEN.reset(handle)


def test_the_failure_breadcrumb_and_the_timers_share_one_token(token) -> None:
    """Two definitions that must agree are one definition that cannot disagree."""
    assert command_surface._MCP_CALL_TOKEN is call_spans.MCP_CALL_TOKEN


def test_repeated_phases_aggregate_into_one_row_with_a_count(token) -> None:
    """A phase entered per changed path must not put a row per path in the ledger."""
    for _ in range(3):
        call_spans.record_span("graph.refresh_paths", 10.0)

    spans = call_spans.pop_call_spans(token)

    assert spans == [{"name": "graph.refresh_paths", "count": 3, "ms": 30.0}]


def test_spans_come_back_slowest_first(token) -> None:
    """Truncation drops the least interesting phase, so order is load-bearing."""
    call_spans.record_span("fast", 1.0)
    call_spans.record_span("slow", 100.0)
    call_spans.record_span("middling", 10.0)

    assert [s["name"] for s in call_spans.pop_call_spans(token)] == [
        "slow",
        "middling",
        "fast",
    ]


def test_popping_twice_yields_nothing_the_second_time(token) -> None:
    """One call, one ledger row: a second pop must not re-attribute the same work."""
    call_spans.record_span("phase", 5.0)

    assert call_spans.pop_call_spans(token)
    assert call_spans.pop_call_spans(token) == []


def test_recording_outside_an_mcp_call_is_a_no_op() -> None:
    """The same instrumentation runs on CLI, watcher and test paths.

    Those never mint a token. Recording anyway would accumulate under a `None`
    key and leak for the life of the process.
    """
    with call_spans.span("phase.outside"):
        pass

    assert call_spans.pop_call_spans(None) == []
    assert not call_spans._SPANS


def test_a_phase_that_raised_is_still_recorded(token) -> None:
    """The phase that blew up after eighteen seconds is the one worth seeing."""
    with pytest.raises(ValueError):
        with call_spans.span("phase.explodes"):
            raise ValueError("boom")

    assert [s["name"] for s in call_spans.pop_call_spans(token)] == ["phase.explodes"]


def test_span_distinguishes_thread_cpu_from_elapsed_wait(token, monkeypatch) -> None:
    """A long wall interval must not be reported as CPU consumed by its thread."""
    ticks = iter((1_000_000, 3_500_000))
    monkeypatch.setattr(time, "thread_time_ns", lambda: next(ticks))
    with call_spans.span("phase.waits"):
        time.sleep(0.01)

    row = call_spans.pop_call_spans(token)[0]
    assert row["fields"]["thread_cpu_us"] == 2500
    assert row["ms"] >= 10



def test_span_separates_other_threads_cpu_from_its_own(token, monkeypatch) -> None:
    """Wall far above thread CPU is contention when other threads burned the CPU."""
    thread_ticks = iter((1_000_000, 1_500_000))
    process_ticks = iter((10_000_000, 18_000_000))
    monkeypatch.setattr(time, "thread_time_ns", lambda: next(thread_ticks))
    monkeypatch.setattr(time, "process_time_ns", lambda: next(process_ticks))
    with call_spans.span("phase.contended"):
        pass

    row = call_spans.pop_call_spans(token)[0]
    assert (row["fields"]["thread_cpu_us"], row["fields"]["process_cpu_us"]) == (500, 8000)


def test_recall_stages_record_cpu_and_counts_when_a_stage_refuses(token, monkeypatch) -> None:
    """Slow or refused recall keeps CPU separate from wall time and caller counts."""
    from exomem.find_types import FindTimings

    ticks = iter((1_000_000, 3_500_000, 5_000_000, 6_500_000))
    monkeypatch.setattr(time, "thread_time_ns", lambda: next(ticks))
    timings = FindTimings()
    measured = {"candidates": 3}
    for _ in range(2):
        with pytest.raises(ValueError), timings.span(
            "semantic.search", measured=measured, elapsed_ms_field="entry_ms"
        ):
            time.sleep(0.01)
            raise ValueError("refused")

    row = call_spans.pop_call_spans(token)[0]
    assert row["name"] == "recall.semantic.search"
    assert row["count"] == 2
    fields = row["fields"]
    assert fields["entry_ms"] >= 18
    assert fields["entry_ms"] <= row["ms"] <= fields["entry_ms"] + 2
    counts_and_thread_cpu = {
        key: value for key, value in fields.items()
        if key not in {call_spans.PROCESS_CPU_US, "entry_ms"}
    }
    assert counts_and_thread_cpu == {
        "candidates": 6, "thread_cpu_us": 4000,
    }
    assert measured == {"candidates": 3}
    # CPU is private ledger metadata; public wall-time attribution keeps its shape.
    assert set(timings.as_dict()["stages"]["semantic.search"]) == {"ms", "source", "calls"}


def test_recall_stage_outside_mcp_never_samples_thread_cpu(monkeypatch) -> None:
    """Offline recall keeps its timing budget and does not create MCP measurements."""
    from exomem.find_types import FindTimings

    def unexpected_cpu_clock():
        raise AssertionError("offline recall sampled an MCP CPU clock")

    monkeypatch.setattr(time, "thread_time_ns", unexpected_cpu_clock)
    monkeypatch.setattr(time, "process_time_ns", unexpected_cpu_clock)
    with FindTimings().span("semantic.search", elapsed_ms_field="entry_ms"):
        pass

    assert not call_spans._SPANS


def test_cpu_attribution_preserves_the_carry_rebuild_sentinel(
    token, tmp_path, monkeypatch
) -> None:
    import json

    from exomem import working_set

    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(tmp_path))
    call_ledger.reset_chain_cache()
    with call_spans.meter(*working_set.CARRY_SPAN_FIELDS) as measured:
        with call_spans.span("recall.working_set.carry.phrase", measured):
            call_spans.measure(call_spans.STATS_REBUILT, 1)

    call_ledger.record_call(
        request_id="carry-cpu", tool="activate_context", outcome="ok", duration_ms=1.0,
        spans=call_spans.pop_call_spans(token),
    )
    stored = json.loads((tmp_path / "ledger.jsonl").read_text().splitlines()[-1])
    row = stored["spans"][0]
    assert row["fields"][call_spans.STATS_REBUILT] == 1
    assert set(working_set.CARRY_SPAN_FIELDS) <= row["fields"].keys()
    assert row["fields"][call_spans.THREAD_CPU_US] >= 0
    assert call_ledger.verify() == []


def test_direct_read_exposes_dispatcher_stages_without_changing_the_page(vault, token) -> None:
    """A fast page timer must not hide preparation, filtering or receipt cost."""
    from exomem import commands, writer_lease
    from exomem.governance.principal import library_scope

    (vault / "timed-read.md").write_text("# Example\n\nVisible example text.\n")
    command = next(item for item in commands.PRODUCT_COMMANDS if item.name == "read_memory")
    with library_scope():
        result = writer_lease.invoke_command(command, vault, path="timed-read.md")

    assert "Visible example text." in result["body"]
    stages = {item["name"]: item for item in call_spans.pop_call_spans(token)}
    for name in (
        "command.body", "command.terminal_filter", "command.disclosure_receipt",
        "read.prepare.resolve_path", "read.prepare.resolve_physical",
        "read.prepare.inspect", "read.prepare.held_read",
    ):
        assert stages[name]["count"] == 1 and stages[name]["ms"] >= 0
        assert stages[name]["fields"]["thread_cpu_us"] >= 0


def test_missing_read_keeps_its_refusal_and_failed_stage_timings(vault, token) -> None:
    """A failed read must retain timings and the terminal error filter."""
    from exomem import commands, writer_lease
    from exomem.governance.principal import library_scope

    command = next(item for item in commands.PRODUCT_COMMANDS if item.name == "read_memory")
    with library_scope(), pytest.raises(ValueError, match="^NOT_FOUND:"):
        writer_lease.invoke_command(command, vault, path="absent-example.md")

    stages = {item["name"]: item for item in call_spans.pop_call_spans(token)}
    for name in ("command.body", "command.terminal_filter", "command.disclosure_receipt"):
        assert stages[name]["count"] == 1 and stages[name]["ms"] >= 0
        assert stages[name]["fields"]["thread_cpu_us"] >= 0


def test_a_public_move_attributes_preflight_to_its_existing_command_span(vault, token) -> None:
    """A successful rename must distinguish semantic preparation from other held work."""
    from exomem import commands, writer_lease
    from exomem.governance.principal import library_scope

    old_path = "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md"
    new_path = "Knowledge Base/Notes/Insights/renamed-example.md"
    command = next(item for item in commands.PRODUCT_COMMANDS if item.name == "manage_memory_file")
    with library_scope():
        result = writer_lease.invoke_command(
            command, vault, operation="move", old_path=old_path, new_path=new_path,
        )

    assert result["status"] == "committed"
    assert not (vault / old_path).exists()
    assert (vault / new_path).exists()
    stages = {item["name"]: item for item in call_spans.pop_call_spans(token)}
    body = stages["command.body"]
    fields = body["fields"]
    assert fields[call_spans.MOVE_PREFLIGHTS] == 1
    assert 0 < fields[call_spans.MOVE_PREFLIGHT_MS] <= body["ms"]
    assert set(fields) - call_spans.CPU_FIELDS == {
        call_spans.MOVE_PREFLIGHT_MS, call_spans.MOVE_PREFLIGHTS,
    }


def test_idempotency_claim_is_visible_without_repeating_the_write(tmp_path, token) -> None:
    """A slow claim must not disappear into the command body or replay its write."""
    from exomem.writer_lease import IdempotencyStore

    store = IdempotencyStore(tmp_path / "claim-state" / "idempotency.sqlite")
    result = {"saved": True}

    assert store.run("capture", "digest", lambda: result) == result
    assert store.run(
        "capture", "digest", lambda: pytest.fail("a replay repeated the write")
    ) == result

    stages = {item["name"]: item for item in call_spans.pop_call_spans(token)}
    claim = stages["mutation.idempotency_claim"]
    assert claim["count"] == 2 and claim["ms"] >= 0
    assert claim["fields"]["thread_cpu_us"] >= 0


def test_span_measures_real_elapsed_time(token) -> None:
    with call_spans.span("phase.sleeps"):
        time.sleep(0.02)

    (recorded,) = call_spans.pop_call_spans(token)
    assert recorded["ms"] >= 15.0, recorded


def test_a_call_generating_names_is_truncated_not_unbounded(token) -> None:
    """Exceeding the name budget is a caller bug; it must not grow the row,
    and the spans it could not name are summed rather than dropped unseen."""
    for index in range(call_spans.MAX_NAMES_PER_CALL + 50):
        call_spans.record_span(f"generated.{index}", 1.0)

    spans = {item["name"]: item for item in call_spans.pop_call_spans(token)}
    assert len(spans) == call_spans.MAX_NAMES_PER_CALL
    assert spans[call_spans.OVERFLOW_SPAN]["count"] == 51
    assert spans[call_spans.OVERFLOW_SPAN]["ms"] == 51.0


def test_a_missed_pop_cannot_leak_indefinitely() -> None:
    """The middleware pops unconditionally; this is the guard for paths that don't."""
    for index in range(call_spans.MAX_CALLS + 20):
        handle = call_spans.MCP_CALL_TOKEN.set(f"token-{index}")
        call_spans.record_span("phase", 1.0)
        call_spans.MCP_CALL_TOKEN.reset(handle)

    assert len(call_spans._SPANS) <= call_spans.MAX_CALLS


def test_instrumentation_never_raises_into_the_call(token) -> None:
    """A timer that can fail the call it measures is worse than no timer."""
    call_spans.record_span("phase", float("nan"))
    call_spans.record_span(None, 1.0)  # type: ignore[arg-type]

    call_spans.pop_call_spans(token)  # must not raise


def test_the_decorator_preserves_the_function_and_its_result(token) -> None:
    @call_spans.timed("phase.decorated")
    def add(left: int, right: int) -> int:
        """Docstring survives."""
        return left + right

    assert add(2, 3) == 5
    assert add.__name__ == "add"
    assert add.__doc__ == "Docstring survives."
    assert [s["name"] for s in call_spans.pop_call_spans(token)] == ["phase.decorated"]


# ------------------------------------------------------------------ ledger row


def test_spans_reach_the_ledger_row_and_the_chain_still_verifies(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(tmp_path))
    call_ledger.reset_chain_cache()

    row = call_ledger.record_call(
        request_id="r1",
        tool="edit_memory",
        outcome="ok",
        duration_ms=80.0,
        total_ms=81.0,
        arguments={"path": "a.md"},
        spans=[{"name": "corpus_context.build", "count": 1, "ms": 50.0}],
    )

    assert row is not None
    assert row["spans"] == [{"name": "corpus_context.build", "count": 1, "ms": 50.0}]
    assert call_ledger.verify() == []


def test_an_uninstrumented_call_records_an_empty_span_list(tmp_path, monkeypatch) -> None:
    """Absence must read as "nothing reported", never as a missing field."""
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(tmp_path))
    call_ledger.reset_chain_cache()

    row = call_ledger.record_call(
        request_id="r1", tool="read_memory", outcome="ok", duration_ms=5.0
    )

    assert row is not None
    assert row["spans"] == []


def test_the_row_bounds_spans_independently_of_the_producer() -> None:
    """The row is hash-chained, so it cannot trust a caller to have bounded
    itself, and a row that dropped spans says how many it dropped."""
    def row(count: int) -> dict:
        return call_ledger.build_row(
            sequence=1, prev_hash=call_ledger.GENESIS_HASH, request_id="request",
            tool="read_memory", outcome="ok", duration_ms=1.0,
            spans=[
                {"name": f"phase.{i}", "count": 1, "ms": float(i)} for i in range(count - 1)
            ] + [{
                "name": "embeddings.encode", "count": 2, "ms": float(count),
                "fields": {"texts": 6, "chars": 12, "execution_wait_us": 30, "model_encode_us": 42,
                           "thread_cpu_us": 2, "process_cpu_us": 8},
            }],
        )

    clipped = row(200)
    assert len(clipped["spans"]) == call_ledger._MAX_SPANS
    assert clipped["spans"][0]["ms"] > clipped["spans"][-1]["ms"], "kept the slowest"
    assert clipped["spans"][0]["fields"] == {
        "texts": 6, "chars": 12, "execution_wait_us": 30, "model_encode_us": 42,
        "thread_cpu_us": 2, "process_cpu_us": 8,
    }
    assert clipped["spans_truncated"] == 200 - call_ledger._MAX_SPANS
    assert "spans_truncated" not in row(call_ledger._MAX_SPANS)


def test_the_row_rebuilds_span_fields_rather_than_passing_them_through() -> None:
    """An unexpected key would change a hashed row's identity meaninglessly."""
    shaped = call_ledger._clip_spans(
        [{"name": "phase", "count": 2, "ms": 3.0, "surprise": "payload"}]
    )

    assert shaped == [{"name": "phase", "count": 2, "ms": 3.0}]


def test_malformed_span_entries_are_dropped_not_fatal() -> None:
    shaped = call_ledger._clip_spans(
        [
            "not a dict",  # type: ignore[list-item]
            {"count": 1, "ms": 1.0},
            {"name": "", "ms": 1.0},
            {"name": "ok", "ms": "not a number"},
            {"name": "kept", "count": 1, "ms": 4.0},
        ]
    )

    assert shaped == [{"name": "kept", "count": 1, "ms": 4.0}]


def test_a_mark_closes_a_span_between_two_distant_sites(token) -> None:
    """Some intervals have no single frame holding both ends."""
    call_spans.mark("canonical_files_committed")
    time.sleep(0.02)
    call_spans.record_span_since("derived.canonical_to_committed", "canonical_files_committed")
    spans = {row["name"]: row for row in call_spans.pop_call_spans(token)}
    assert "derived.canonical_to_committed" in spans
    assert spans["derived.canonical_to_committed"]["ms"] >= 15


def test_a_span_since_a_mark_that_was_never_stamped_records_nothing(token) -> None:
    """A missing mark means that path did not run, not that this one should guess."""
    call_spans.record_span_since("derived.canonical_to_committed", "never_stamped")
    assert call_spans.pop_call_spans(token) == []


def test_a_mark_outside_an_mcp_call_is_a_no_op() -> None:
    call_spans.mark("canonical_files_committed")
    call_spans.record_span_since("derived.canonical_to_committed", "canonical_files_committed")
    assert call_spans.pop_call_spans(None) == []


def test_marks_cannot_grow_without_bound(token) -> None:
    for i in range(call_spans.MAX_NAMES_PER_CALL + 10):
        call_spans.mark(f"mark-{i}")
    call_spans.record_span_since("late", f"mark-{call_spans.MAX_NAMES_PER_CALL + 5}")
    assert call_spans.pop_call_spans(token) == []


#: Every span name the write and retrieval paths record, and the documentation
#: that has to name them. A span whose name drifts is a diagnosis that silently
#: stops finding its number, which is exactly the failure `spans` exists to
#: prevent -- so the source and `docs/observability.md` are pinned to each other
#: rather than to a hand list that can rot.
_DOCUMENTED_SPAN_NAMES = frozenset(
    {
        "corpus_context.build",
        "derived.canonical_commit",
        "derived.canonical_to_committed",
        "derived.receipt_prepare",
        "derived.receipt_proof",
        "derived.acknowledgement",
        "derived.pending_visibility",
        "derived.advisory_execute",
        "derived.component_dispatch",
        "derived.component_completion",
        "index.upsert_after_write",
        "index.memory_refs",
        "index.resolver",
        "index.lexstore",
        "index.epistemic_graph",
        "index.embeddings",
        "graph.refresh_paths",
        "lexical.rebuild_atomic",
        "lexical.connect",
        "lexical.connect.guard",
        "lexical.connect.sqlite",
        "lexical.connect.owner_family",
        "lexical.readiness",
        "lexical.proof",
        "lexical.query",
        "lexical.resolver_entries",
        "recall.projection.checkpoint",
        "recall.projection.snapshot",
        "recall.projection.scope_snapshot",
        "recall.projection.relativize",
        "embeddings.model_load",
        "embeddings.encode",
        "embeddings.matrix_load",
        "embeddings.matrix_catch_up",
        "embeddings.unit_matrix_load",
        "delivery.vocabulary_after_commit",
        "derived.fanout",
        "derived.terminal_persist",
        "derived.deferred_index_store",
        "index.path_partition",
        "index.semantic_states",
        "index.policy_revalidate",
        "index.corpus_publish",
        "index.semantic_purge",
        "index.path_custody",
        "index.self_write_registration",
        "index.graph_epoch_handoff",
        "index.completion_check",
        "index.full_refresh_store",
        "advisory.best_cosine",
        "advisory.overlap_groups",
        "command.refusal_floor",
        # The write-stage collector's own names, emitted into the ledger
        # unconditionally by `MutationTimings.emit_call_spans`. Recorded
        # through a variable at that seam, so the source pin below finds
        # them at the `mutation_timing_span` call sites that name each one.
        "commit.boundary_acquire",
        "commit.creation_lock",
        "commit.embedding_prewarm",
        "commit.locked_commit",
        "commit.manifest",
        "commit.resolver_prime",
        "commit.revalidate",
        "commit.stamp_check",
        "preflight.contract_eval",
        "preflight.corpus_context",
        "preflight.page_states",
        "preflight.read_guarded",
        "preflight.registries",
        "preflight.relation_review",
        "preflight.validity_token",
        # A page creation's own stages, and the blocks every write attaches
        # after its commit, recorded directly rather than by the collector.
        "writer.resolver_snapshot",
        "preflight.creation",
        "commit.source_closure",
        "commit.relation_review_prepare",
        "commit.catalog_prepare",
        "commit.relation_review",
        "commit.relation_review_attempt",
        "post_commit.structure_suggestion",
        "post_commit.records_routing",
        "post_commit.observation_delta",
        "post_commit.records_delivery",
        "post_commit.due_state",
        "post_commit.capture_sweep",
        "post_commit.entity_candidate",
        "advisory.default_sweep",
        "command.body",
        "command.terminal_filter",
        "egress.cross_check",
        "egress.scrub",
        "spans.overflow",
        "recall.working_set.carry.phrase",
        "recall.working_set.carry.title",
        "runtime.gc",
    }
)


def test_every_declared_span_name_is_documented() -> None:
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[1] / "docs" / "observability.md").read_text(
        encoding="utf-8"
    )
    missing = sorted(name for name in _DOCUMENTED_SPAN_NAMES if f"`{name}`" not in doc)
    assert not missing, (
        "a span name the write path records is absent from docs/observability.md: "
        f"{missing}"
    )


def test_every_declared_span_name_is_recorded_somewhere_in_the_source() -> None:
    """A documented name nothing emits is a diagnosis that will never find it."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "exomem"
    body = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(src.rglob("*.py"))
    )
    missing = sorted(
        name
        for name in _DOCUMENTED_SPAN_NAMES
        if f'"{name}"' not in body
        # `index.<component>` is recorded from one f-string seam with the
        # component name supplied by the caller, so pin the component instead.
        and f'"{name.removeprefix("index.")}"' not in body
    )
    assert not missing, f"documented span names nothing records: {missing}"


def test_eviction_warns_once_per_window_rather_than_per_drop(caplog) -> None:
    """Losing a live call's measurements is a warning; saying so 200 times is noise."""
    caplog.set_level("WARNING", logger="exomem.call_spans")
    for index in range(call_spans.MAX_CALLS + 30):
        handle = call_spans.MCP_CALL_TOKEN.set(f"evict-{index}")
        try:
            call_spans.record_span("phase", 1.0)
        finally:
            call_spans.MCP_CALL_TOKEN.reset(handle)

    lines = [
        record for record in caplog.records if "call span eviction dropped" in record.getMessage()
    ]
    assert lines, "an eviction that reports nothing is indistinguishable from no instrumentation"
    assert len(lines) == 1, f"one line per window, not per drop: {len(lines)}"
    assert lines[0].levelname == "WARNING"


def test_a_path_shaped_field_key_never_reaches_a_row(token, caplog) -> None:
    """Field keys are measurement names, so a key cannot smuggle content.

    The values were bounded from the start -- coerced to int, so a string
    cannot land -- but the KEYS were only clipped to 64 characters, which a
    vault path fits inside comfortably. A ledger row is hash-chained and
    operator-readable outside the vault, so a key like
    `Knowledge Base/Notes/<title>` would put a note's identity in it.
    """
    caplog.set_level("WARNING", logger="exomem.call_spans")

    call_spans.record_span(
        "index.memory_refs",
        1.0,
        {
            "paths": 3,
            "Knowledge Base/Notes/Insights/secret-title.md": 1,
            "Title With Spaces": 1,
            "camelCase": 1,
            "has.dot": 1,
            "has-dash": 1,
            "digits9": 1,
        },
    )

    spans = {span["name"]: span for span in call_spans.pop_call_spans(token)}
    fields = spans["index.memory_refs"].get("fields") or {}
    assert fields == {"paths": 3}, (
        f"a key that is not a bare measurement name reached the row: {fields}"
    )
    assert "secret-title" not in caplog.text, (
        "the warning must report the rejected key's shape, never its text"
    )
    assert "span field key rejected" in caplog.text


def test_the_ledger_refuses_a_path_shaped_field_key_too(token) -> None:
    """The last seam before the hash chain enforces it independently."""
    shaped = call_ledger._clip_spans(
        [
            {
                "name": "index.memory_refs",
                "count": 1,
                "ms": 1.0,
                "fields": {
                    "paths": 2,
                    "Knowledge Base/Notes/Insights/secret-title.md": 1,
                },
            }
        ]
    )

    assert shaped == [
        {"name": "index.memory_refs", "count": 1, "ms": 1.0, "fields": {"paths": 2}}
    ], shaped
