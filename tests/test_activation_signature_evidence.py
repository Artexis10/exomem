"""Activation must corroborate anchors without acquiring the recall corpus."""

from pathlib import Path

import numpy as np
import pytest
from test_working_set_index import _seed_structure, _write

from exomem import commands, embeddings, lexstore, readiness, working_set_index, working_set_runtime
from exomem.runtime_resources import ModelBusyError


#: Planted signature space: the two outlier anchors sit on axis 0, which is the
#: turn's own direction; every other signature points elsewhere.
DIM = 64
QUERY = np.eye(DIM, dtype=np.float32)[0]


def _plant_signatures(conn, anchors, outliers: set[str], n: int = 60, seed: int = 5) -> None:
    """Plant a population the corpus-relative band can calibrate against.

    The band measures a turn against the median and spread of the whole
    catalogue and needs at least 50 vectors, so a handful of fixture anchors
    alone would be `uncalibrated`. `n` background signatures, random in the
    same space, stand in for the rest of a catalogue; the named `outliers` are
    the turn's own direction and the fixture's other anchors are orthogonal.
    """
    rng = np.random.default_rng(seed)
    for row in anchors:
        vector = QUERY if row.title in outliers else np.eye(DIM, dtype=np.float32)[1]
        conn.execute(
            "INSERT INTO anchor_vectors(anchor_id, vector) VALUES (?, ?)",
            (row.anchor_id, np.asarray(vector, dtype=np.float32).tobytes()),
        )
    for i in range(n):
        vector = rng.standard_normal(DIM).astype(np.float32)
        conn.execute(
            "INSERT INTO anchor_vectors(anchor_id, vector) VALUES (?, ?)",
            (f"planted:background-{i}", (vector / np.linalg.norm(vector)).tobytes()),
        )


def test_activation_latency_gate_counts_the_entire_request():
    from test_latency_gate import _compiler_ms

    assert (
        _compiler_ms(
            {
                "total_ms": 38_000.0,
                "stages": {
                    "working_set.resolve": {"ms": 10.0},
                    "working_set.retrieval": {"ms": 36_000.0},
                },
            }
        )
        == 38_000.0
    )


@pytest.fixture
def signatures(vault: Path, monkeypatch: pytest.MonkeyPatch):
    _seed_structure(vault)
    _write(
        vault / "Knowledge Base/Products/Cedar Carrier.md",
        "---\ntype: note\nstatus: active\n---\n# Cedar Carrier\n\nA cargo trailer.\n",
    )
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    # Fixed scorer outputs isolate admission/resolution from model availability;
    # the operation, catalogue, resolver, role lanes and egress are real.
    conn = index._connect()
    assert conn is not None
    _plant_signatures(conn, index.anchors(), {"Cargo Sled", "Cedar Carrier"})
    conn.commit()
    index.close()
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS")
    monkeypatch.setattr(readiness, "should_defer", lambda component: False)
    monkeypatch.setattr(
        embeddings, "embed_query_if_loaded", lambda text: QUERY, raising=False
    )
    calls = []
    monkeypatch.setattr(commands.find_module, "find", lambda *a, **k: calls.append(k) or [])
    working_set_runtime.reset_caches_for_tests()
    yield vault, calls
    working_set_runtime.reset_caches_for_tests()


def test_rare_word_gets_real_signature_corroboration_without_full_recall(signatures):
    vault, calls = signatures
    packet = commands.op_activate_context(vault, turn="Could the sled cope?", include_timings=True)
    assert calls == [], "activation must not acquire ordinary hybrid recall resources"
    sled = next(a for a in packet["anchors"] if a["title"] == "Cargo Sled")
    assert sled["status"] == "resolved"
    assert {"rare_term", "vector_band"} <= set(sled["evidence"])
    assert "retrieval" not in sled["evidence"]
    assert packet["generation"]["semantic_evidence"] == "ready"
    assert packet["units"] or packet["pointers"]
    assert "working_set.semantic" in packet["timings"]["stages"]
    assert (
        sum(s["ms"] for s in packet["timings"]["stages"].values()) <= packet["timings"]["total_ms"]
    )


def test_exact_alias_does_not_hide_a_semantic_competitor(signatures):
    vault, calls = signatures
    packet = commands.op_activate_context(vault, turn="Cargo Sled or cedar?")
    assert packet["abstention"]["reason"] == "ambiguous"
    assert {a["title"] for a in packet["anchors"] if a["status"] == "resolved"} == {
        "Cargo Sled",
        "Cedar Carrier",
    }
    assert calls == []
    assert packet["units"] == []


def test_vectors_alone_never_create_resolved_context(signatures):
    vault, calls = signatures
    packet = commands.op_activate_context(vault, turn="violet distant drums")
    assert packet["abstained"] is True
    assert all(a["status"] == "partial" for a in packet["anchors"])
    assert packet["units"] == packet["pointers"] == []
    assert calls == []


@pytest.mark.parametrize("state", ["busy", "unavailable", "warming"])
def test_transient_semantic_failure_does_not_poison_packet_cache(signatures, monkeypatch, state):
    vault, _ = signatures

    def unavailable(text):
        if state == "busy":
            raise ModelBusyError("busy")
        return None

    monkeypatch.setattr(embeddings, "embed_query_if_loaded", unavailable)
    monkeypatch.setattr(readiness, "should_defer", lambda component: state == "warming")
    first = working_set_runtime.serve(vault, turn="Could the sled cope?", max_chars=4000)
    assert first["generation"]["semantic_evidence"] == state
    assert first["abstained"] is True
    monkeypatch.setattr(readiness, "should_defer", lambda component: False)
    monkeypatch.setattr(embeddings, "embed_query_if_loaded", lambda text: QUERY)
    second = working_set_runtime.serve(vault, turn="Could the sled cope?", max_chars=4000)
    assert second["generation"]["semantic_evidence"] == "ready"
    assert second["abstained"] is False


def test_disabled_semantics_cannot_reuse_enabled_packet(signatures, monkeypatch):
    vault, _ = signatures
    first = working_set_runtime.serve(vault, turn="Could the sled cope?", max_chars=4000)
    assert first["abstained"] is False
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    second = working_set_runtime.serve(vault, turn="Could the sled cope?", max_chars=4000)
    assert second["generation"]["semantic_evidence"] == "disabled"
    assert second["abstained"] is True


def test_explicit_agent_choice_does_not_request_an_encode(signatures, monkeypatch):
    vault, _ = signatures
    index = working_set_index.WorkingSetIndex(vault)
    chosen = next(row for row in index.anchors() if row.title == "Cargo Sled")
    index.close()
    encodes = []
    monkeypatch.setattr(embeddings, "embed_query_if_loaded", lambda text: encodes.append(text))
    packet = commands.op_activate_context(
        vault, turn="violet distant drums", anchor=chosen.ref or chosen.path
    )
    assert packet["abstained"] is False
    assert packet["generation"]["semantic_evidence"] == "agent_choice"
    assert encodes == []


@pytest.mark.parametrize("turn", ["sled rated", "cargo 400 kg"])
def test_lean_activation_preserves_page_content_corroboration(signatures, monkeypatch, turn):
    from test_latency_gate import _seed_freshness_live

    vault, calls = signatures
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    _seed_freshness_live(vault)
    lexstore.ensure_fresh(vault)
    packet = commands.op_activate_context(vault, turn=turn)
    sled = next(a for a in packet["anchors"] if a["title"] == "Cargo Sled")
    assert sled["status"] == "resolved"
    assert {"rare_term", "retrieval"} <= set(sled["evidence"])
    assert packet["generation"]["lexical_evidence"] == "available"
    assert calls == []


def test_activation_catalog_query_forbids_foreground_repairs(signatures, monkeypatch):
    vault, _ = signatures
    requested = []

    def catalog(*args, **kwargs):
        requested.append(kwargs)
        return lexstore.CatalogQueryResult(None, lexstore.CatalogReadiness("stale", False, "fts5"))

    monkeypatch.setattr(lexstore, "search_bm25_result", catalog)
    first = commands.op_activate_context(vault, turn="Cargo Sled")
    assert requested and requested[0]["allow_delta"] is False
    assert 0 < len(requested[0]["allowed_paths"]) < 20
    assert first["generation"]["lexical_evidence"] == "stale"
    assert working_set_runtime._PACKET_CACHE == {}


@pytest.mark.parametrize("turn", ["Give me a poem about a sled.", "sled sleds", "the sled"])
def test_one_shared_word_is_not_its_own_lexical_corroboration(signatures, monkeypatch, turn):
    from test_latency_gate import _seed_freshness_live

    vault, _ = signatures
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    _seed_freshness_live(vault)
    lexstore.ensure_fresh(vault)
    packet = commands.op_activate_context(vault, turn=turn)

    sled = next(a for a in packet["anchors"] if a["title"] == "Cargo Sled")
    assert sled["status"] == "partial"
    assert "retrieval" not in sled["evidence"]
    assert packet["abstained"] is True
    assert packet["units"] == []


def test_single_word_exact_alias_still_resolves_without_lexical_corroboration(
    signatures, monkeypatch
):
    vault, _ = signatures
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    _write(
        vault / "Knowledge Base/Products/Kestrel.md",
        "---\ntype: note\nstatus: active\n---\n# Kestrel\n\nA bicycle.\n",
    )
    packet = commands.op_activate_context(vault, turn="Kestrel")
    kestrel = next(a for a in packet["anchors"] if a["title"] == "Kestrel")
    assert kestrel["status"] == "resolved"
    assert "exact_alias" in kestrel["evidence"]


def test_collection_override_keeps_outcome_and_next_action(signatures, monkeypatch):
    from uuid import UUID

    from test_working_set_index import _seed_planning

    from exomem import planning

    vault, _ = signatures
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    path = _seed_planning(vault)
    action = "Pack the northern corridor supplies"
    planning.add(
        vault, path, plan_id=str(UUID(int=8)),
        item={"title": action, "kind": "work-item"}, why="seed complementary planning item",
    )
    packet = commands.op_activate_context(vault, turn="What remains to do?", anchor=path)
    assert packet["abstained"] is False
    titles = {anchor["title"] for anchor in packet["anchors"]}
    assert titles == {"Winter schedule for the northern corridor", action}
    assert titles <= {
        item.get("title") or item.get("text") for item in packet["pointers"] + packet["units"]
    }


def test_new_anchor_gets_lexical_evidence_on_first_activation(signatures, monkeypatch):
    from test_latency_gate import _seed_freshness_live

    vault, _ = signatures
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    _write(
        vault / "Knowledge Base/Products/Polar Hauler.md",
        "---\ntype: note\nstatus: active\n---\n# Polar Hauler\n\nReady for winter.\n",
    )
    _seed_freshness_live(vault)
    lexstore.ensure_fresh(vault)
    packet = commands.op_activate_context(vault, turn="hauler winter")
    hauler = next(a for a in packet["anchors"] if a["title"] == "Polar Hauler")
    assert hauler["status"] == "resolved"
    assert "retrieval" in hauler["evidence"]


def test_publication_between_evidence_and_compilation_cannot_mix_generations(
    signatures, monkeypatch
):
    vault, _ = signatures
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    original = working_set_runtime.lexical_evidence

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        index = working_set_index.WorkingSetIndex(vault)
        index.rebuild()
        index.close()
        return result

    monkeypatch.setattr(working_set_runtime, "lexical_evidence", changed)
    packet = commands.op_activate_context(vault, turn="Cargo Sled")
    assert packet["abstention"]["reason"] == "unavailable"
    assert packet["generation"]["lexical_evidence"] == "stale"
    assert packet["units"] == []
    assert working_set_runtime._PACKET_CACHE == {}


def test_a_catalogue_too_small_to_calibrate_bands_nothing(signatures, monkeypatch):
    """Below 50 signatures there is no chance level to measure a turn against:
    the state is `uncalibrated`, no anchor earns `vector_band`, and the turn is
    not even encoded."""
    vault, _ = signatures
    index = working_set_index.WorkingSetIndex(vault)
    conn = index._connect()
    conn.execute("DELETE FROM anchor_vectors WHERE anchor_id LIKE 'planted:%'")
    conn.commit()
    index.close()
    encodes = []
    monkeypatch.setattr(embeddings, "embed_query_if_loaded", lambda text, **_kw: encodes.append(text) or QUERY)

    packet = commands.op_activate_context(vault, turn="Could the sled cope?")

    assert packet["generation"]["semantic_evidence"] == "uncalibrated"
    sled = next(a for a in packet["anchors"] if a["title"] == "Cargo Sled")
    assert "vector_band" not in sled["evidence"]
    assert sled["status"] == "partial"
    assert encodes == []
