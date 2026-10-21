"""The parts of a recall that live outside the retrieval stages name their cost.

Measured 2026-09-16 on the personal service after 0.85.5: a keyword recall
took 1.8 s of which the `recall.*` stages explained 0.75 s. The rest ran in
places no span covered -- the served memo's re-checks, the emission decision,
the MCP-layer post-filter, and the lexical publication barrier the first read
after a promotion waited 30 s on. Each of those is a named span now.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import call_spans, commands, due_state, lexstore
from exomem.governance.principal import RequestPrincipal

PAGE = "Knowledge Base/Notes/Insights/one.md"


@pytest.fixture()
def token():
    handle = call_spans.MCP_CALL_TOKEN.set("spans-test")
    due_state.reset_serve_cache()
    try:
        yield "spans-test"
    finally:
        call_spans.pop_call_spans("spans-test")
        call_spans.MCP_CALL_TOKEN.reset(handle)
        due_state.reset_serve_cache()


def _names(token: str) -> dict[str, dict]:
    return {row["name"]: row for row in call_spans.pop_call_spans(token)}


def _persist(vault_root: Path) -> None:
    (vault_root / PAGE).parent.mkdir(parents=True, exist_ok=True)
    (vault_root / PAGE).write_text("# one\n", encoding="utf-8")
    due_state.save(
        vault_root,
        {
            "version": due_state.SCHEMA_VERSION,
            "categories": {"predictions": {PAGE: {"open": [{"path": PAGE}]}}},
        },
    )


def test_a_served_miss_names_the_build_and_a_hit_names_its_rechecks(
    tmp_path: Path, token: str
) -> None:
    _persist(tmp_path)
    who = RequestPrincipal(audience_id="a", authorization_session_id="s", issuer_family="test")
    now = dt.datetime(2026, 9, 18, 9, tzinfo=dt.UTC)
    due_state.served_entries(tmp_path, now=now, principal=who)
    first = _names(token)
    assert "recall.due_state.build" in first
    assert "recall.due_state.verdicts" not in first
    due_state.served_entries(tmp_path, now=now, principal=who)
    second = _names(token)
    assert "recall.due_state.build" not in second
    assert "recall.due_state.verdicts" in second
    assert "recall.due_state.exists" in second


def test_the_emission_decision_has_its_own_span(
    tmp_path: Path, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(due_state, "served", lambda *_a, **_k: {"total": 1, "top": []})
    monkeypatch.setattr(due_state, "should_emit", lambda *_a, **_k: True)
    out = commands._with_due_state(tmp_path, {"hits": []}, purpose=None)
    assert out["due_state"] == {"total": 1, "top": []}
    names = _names(token)
    assert "recall.due_state" in names
    assert "recall.due_state.emit" in names


def test_waiting_on_the_publication_barrier_is_attributed_to_the_call(token: str) -> None:
    with lexstore._timed_barrier(contextlib.nullcontext(), 0.05):
        pass
    row = _names(token)["lexical.publication_wait"]
    assert row["fields"] == {"timeout_ms": 50, "acquired": 1}

    @contextlib.contextmanager
    def refused():
        raise TimeoutError("held")
        yield

    with pytest.raises(TimeoutError):
        with lexstore._timed_barrier(refused(), 30.0):
            pass
    row = _names(token)["lexical.publication_wait"]
    assert row["fields"] == {"timeout_ms": 30000, "acquired": 0}


def test_off_the_mcp_path_the_barrier_span_is_a_no_op() -> None:
    assert call_spans.MCP_CALL_TOKEN.get() is None
    with lexstore._timed_barrier(contextlib.nullcontext(), 0.05):
        pass


# --- the deep pack, the encoder and a direct read name their phases ---------------


def test_a_deep_pack_names_its_phases(vault: Path, token: str) -> None:
    """A slow pack has to say which phase paid: the 2026-09-18 media
    re-segmentation hid inside one opaque `recall.pack` for days."""
    out = commands.op_find(vault, query="metabolism", pack=True)
    assert out["pack"]["packed_paths"]
    names = _names(token)
    assert "recall.pack" in names
    for phase in ("parents", "units", "neighborhood", "tension"):
        assert f"recall.pack.{phase}" in names, phase
    assert names["recall.pack.parents"]["ms"] <= names["recall.pack"]["ms"]


@pytest.mark.parametrize("previous", [False, True])
def test_an_encode_names_the_module_that_asked_and_separates_waiting(
    token: str, monkeypatch: pytest.MonkeyPatch, previous: bool
) -> None:
    import numpy as np

    from exomem import embeddings, recall_space, runtime_resources

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    now_ns = 0

    @contextlib.contextmanager
    def execution():
        nonlocal now_ns
        now_ns += 5000
        try:
            yield
        finally:
            now_ns += 11000  # Releasing the turn is neither waiting nor encoding.

    def encode(texts, **_kwargs):
        nonlocal now_ns
        now_ns += 7000
        return np.asarray([[len(text)] for text in texts], dtype=np.float32)

    model = SimpleNamespace(encode=encode)
    monkeypatch.setattr(embeddings, "time", SimpleNamespace(perf_counter_ns=lambda: now_ns))
    monkeypatch.setattr(embeddings, "get_model", lambda: model)
    monkeypatch.setattr(embeddings, "BGE_GUARD", SimpleNamespace(active=contextlib.nullcontext))
    monkeypatch.setattr(embeddings, "_prefixes", lambda *_args: ("", ""))
    monkeypatch.setattr(embeddings, "encode_batch_size", lambda _model: 1)
    monkeypatch.setattr(runtime_resources, "model_admission", contextlib.nullcontext)
    monkeypatch.setattr(runtime_resources, "model_execution", execution)
    monkeypatch.setattr(recall_space, "previous_resident", lambda _name: model)
    monkeypatch.setattr(recall_space, "_previous_gate", lambda: SimpleNamespace(
        admission=contextlib.nullcontext, execution=execution,
    ))
    # A caller whose frame belongs to an Exomem module, without importing one
    # that would need a model: the attribution reads the frame's module name.
    namespace = {"__name__": "exomem.context_pack", "embeddings": embeddings}
    exec("def ask():\n    return embeddings.embed_texts(['a', 'ccc', 'bb'])\n", namespace)
    with recall_space.selecting("previous-model" if previous else embeddings.MODEL_NAME):
        for _ in range(2):
            actual = namespace["ask"]()
            assert actual[:, 0].tolist() == [1, 3, 2]
    names = _names(token)
    row = names["embeddings.encode"]
    assert row["count"] == 2
    fields = row["fields"]
    assert {key: value for key, value in fields.items() if key not in call_spans.CPU_FIELDS} == {
        "texts": 6, "chars": 12, "execution_wait_us": 30, "model_encode_us": 42,
    }
    assert "encode.by.context_pack" in names


@pytest.mark.parametrize("phase", ["empty", "admission", "execution", "encode"])
def test_failed_encodes_report_only_attempted_intervals(token, monkeypatch, phase) -> None:
    from exomem import embeddings, runtime_resources

    now_ns = 0

    @contextlib.contextmanager
    def admission():
        if phase == "admission":
            raise RuntimeError(phase)
        yield

    @contextlib.contextmanager
    def execution():
        nonlocal now_ns
        now_ns += 5000
        if phase == "execution":
            raise RuntimeError(phase)
        yield

    def encode(_texts, **_kwargs):
        nonlocal now_ns
        now_ns += 7000
        raise RuntimeError(phase)

    monkeypatch.setattr(embeddings, "time", SimpleNamespace(perf_counter_ns=lambda: now_ns))
    monkeypatch.setattr(embeddings, "get_model", lambda: SimpleNamespace(encode=encode))
    monkeypatch.setattr(embeddings, "BGE_GUARD", SimpleNamespace(active=contextlib.nullcontext))
    monkeypatch.setattr(embeddings, "_prefixes", lambda *_args: ("", ""))
    monkeypatch.setattr(embeddings, "encode_batch_size", lambda _model: 1)
    monkeypatch.setattr(runtime_resources, "model_admission", admission)
    monkeypatch.setattr(runtime_resources, "model_execution", execution)
    if phase == "empty":
        assert len(embeddings.embed_texts([])) == 0
    else:
        with pytest.raises(RuntimeError, match=phase):
            embeddings.embed_texts(["one"])
    row = _names(token)["embeddings.encode"]
    assert row["count"] == 1
    measured = {key: value for key, value in row["fields"].items() if key not in call_spans.CPU_FIELDS}
    expected = {"texts": 0, "chars": 0} if phase == "empty" else {"texts": 1, "chars": 3}
    if phase in {"execution", "encode"}:
        expected["execution_wait_us"] = 5
    if phase == "encode":
        expected["model_encode_us"] = 7
    assert measured == expected


@pytest.mark.parametrize("separate", [False, True])
def test_resident_activation_encodes_separate_entry_and_body(token, monkeypatch, separate) -> None:
    import numpy as np

    from exomem import embeddings

    now_ns = 0

    @contextlib.contextmanager
    def execution(*_args):
        nonlocal now_ns
        now_ns += 5000
        try:
            yield
        finally:
            now_ns += 11000

    def encode(_texts, **_kwargs):
        nonlocal now_ns
        now_ns += 7000
        return np.asarray([[1.0]], dtype=np.float32)

    model = SimpleNamespace(encode=encode)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "time", SimpleNamespace(perf_counter_ns=lambda: now_ns))
    monkeypatch.setattr(embeddings, "_MODEL", model)
    monkeypatch.setattr(embeddings, "_ACTIVATION_MODEL", model)
    monkeypatch.setattr(embeddings, "activation_encoder_is_shared", lambda: not separate)
    monkeypatch.setattr(embeddings, "BGE_GUARD", SimpleNamespace(active=contextlib.nullcontext))
    monkeypatch.setattr(embeddings, "ACTIVATION_GUARD", SimpleNamespace(active=contextlib.nullcontext))
    monkeypatch.setattr(embeddings, "_prefixes", lambda *_args: ("", ""))
    monkeypatch.setattr(embeddings, "encode_batch_size", lambda _model: 1)
    monkeypatch.setattr(embeddings, "_interactive_execution", execution)
    actual = embeddings.embed_activation_query_if_loaded("turn")
    assert actual is not None
    assert actual.tolist() == [1.0]
    row = _names(token)["embeddings.encode"]
    assert row["count"] == 1
    assert {key: value for key, value in row["fields"].items() if key not in call_spans.CPU_FIELDS} == {
        "texts": 1, "chars": 4, "execution_wait_us": 5, "model_encode_us": 7,
    }


def test_a_direct_read_names_its_phases(vault: Path, token: str) -> None:
    from exomem import vault as vault_module

    found = commands.op_find(vault, query="metabolism")
    hit = (found["hits"] if isinstance(found, dict) else found)[0]
    _names(token)  # discard the find's spans
    vault_module.evict_inbound_index(vault)
    out = commands.op_read_memory(vault, path=hit["path"], links=True, include_history=True)
    assert "links" in out and "history" in out
    assert out["links"]["inbound"] is None
    names = _names(token)
    assert "read.page" in names
    assert set(names["read.links"]["fields"]) - call_spans.CPU_FIELDS == {
        "outbound",
    }
    assert "entries" in names["read.history"]["fields"]
