"""Phase timing for one MCP call.

The defect these exist to prevent is not a wrong number, it is a *quiet* one:
instrumentation that silently records nothing looks identical to a call that
spent no time anywhere. Several of these assert on absence for that reason.
"""

from __future__ import annotations

import time

import pytest

from exomem import call_ledger, call_spans, command_surface


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


def test_span_measures_real_elapsed_time(token) -> None:
    with call_spans.span("phase.sleeps"):
        time.sleep(0.02)

    (recorded,) = call_spans.pop_call_spans(token)
    assert recorded["ms"] >= 15.0, recorded


def test_a_call_generating_names_is_truncated_not_unbounded(token) -> None:
    """Exceeding the name budget is a caller bug; it must not grow the row."""
    for index in range(call_spans.MAX_NAMES_PER_CALL + 50):
        call_spans.record_span(f"generated.{index}", 1.0)

    assert len(call_spans.pop_call_spans(token)) == call_spans.MAX_NAMES_PER_CALL


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
    """The row is hash-chained, so it cannot trust a caller to have bounded itself."""
    shaped = call_ledger._clip_spans(
        [{"name": f"phase.{i}", "count": 1, "ms": float(i)} for i in range(200)]
    )

    assert len(shaped) == call_ledger._MAX_SPANS
    assert shaped[0]["ms"] > shaped[-1]["ms"], "kept the slowest, not the first"


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
        "embeddings.model_load",
        "embeddings.encode",
        "embeddings.matrix_load",
        "embeddings.matrix_catch_up",
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
        "advisory.best_cosine",
        "advisory.overlap_groups",
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
