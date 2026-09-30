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
    who = SimpleNamespace(audience_id="a", authorization_session_id="s", resolved=True)
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


def test_an_encode_names_the_module_that_asked(token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import numpy as np

    from exomem import embeddings

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(
        embeddings, "_embed_texts",
        lambda texts, is_query=False: np.zeros((len(texts), embeddings.VECTOR_DIM), dtype=np.float32),
    )
    # A caller whose frame belongs to an Exomem module, without importing one
    # that would need a model: the attribution reads the frame's module name.
    namespace = {"__name__": "exomem.context_pack", "embeddings": embeddings}
    exec("def ask():\n    return embeddings.embed_texts(['one', 'two'])\n", namespace)
    namespace["ask"]()
    names = _names(token)
    assert names["embeddings.encode"]["fields"] == {"texts": 2, "chars": 6}
    assert "encode.by.context_pack" in names
    assert embeddings._encode_caller.__doc__  # the attribution is documented at the seam


def test_a_direct_read_names_its_phases(vault: Path, token: str) -> None:
    found = commands.op_find(vault, query="metabolism")
    hit = (found["hits"] if isinstance(found, dict) else found)[0]
    _names(token)  # discard the find's spans
    out = commands.op_read_memory(vault, path=hit["path"], links=True, include_history=True)
    assert "links" in out and "history" in out
    names = _names(token)
    assert "read.page" in names
    assert set(names["read.links"]["fields"]) == {"inbound", "outbound"}
    assert "entries" in names["read.history"]["fields"]
