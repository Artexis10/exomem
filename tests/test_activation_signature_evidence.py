"""Activation must corroborate anchors without acquiring the recall corpus."""

import hashlib
from pathlib import Path

import numpy as np
import pytest
from test_working_set_index import _seed_structure, _write

from exomem import (
    commands,
    embeddings,
    lexstore,
    readiness,
    working_set,
    working_set_index,
    working_set_runtime,
)
from exomem.runtime_resources import ModelBusyError

#: Planted signature space: the two outlier anchors sit on axis 0, which is the
#: turn's own direction; every other signature is a seeded random direction.
DIM = 64
QUERY = np.eye(DIM, dtype=np.float32)[0]
OUTLIERS = frozenset({"Cargo Sled", "Cedar Carrier"})
#: Background anchors, so the corpus-relative band has a population of at
#: least 50 to calibrate against; the fixture's own anchors are a dozen.
BACKGROUND = 50
FINGERPRINT = "planted-encoder|cls|l2|0000"


class _PlantedEncoder:
    """The activation encoder the index embeds signatures with: an outlier
    anchor's signature (its title is the first line) points at the turn."""

    def passages(self, texts: list[str]) -> np.ndarray:
        rows = []
        for text in texts:
            if text.split("\n", 1)[0].strip() in OUTLIERS:
                rows.append(QUERY)
                continue
            seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "little")
            vector = np.random.default_rng(seed).standard_normal(DIM).astype(np.float32)
            rows.append(vector / np.linalg.norm(vector))
        return np.vstack(rows)


def _plant_background(vault: Path) -> list[Path]:
    pages = []
    for i in range(BACKGROUND):
        page = vault / f"Knowledge Base/Products/Zorvath {i:02d}.md"
        _write(page, f"---\ntype: note\nstatus: active\n---\n# Zorvath {i:02d}\n\nPlanted background item {i}.\n")
        pages.append(page)
    return pages


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
    background = _plant_background(vault)
    # A resident activation encoder with fixed outputs isolates admission and
    # resolution from model availability; the operation, catalogue, vector
    # pipeline, band, resolver, role lanes and egress are real.
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "embed_activation_passages", _PlantedEncoder().passages)
    monkeypatch.setattr(embeddings, "activation_fingerprint", lambda: FINGERPRINT)
    monkeypatch.setattr(embeddings, "embed_activation_query_if_loaded", lambda text: QUERY)
    monkeypatch.setattr(readiness, "should_defer", lambda component: False)
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    assert len(index.vector_matrix(FINGERPRINT)[0]) >= 50
    index.close()
    calls = []
    monkeypatch.setattr(commands.find_module, "find", lambda *a, **k: calls.append(k) or [])
    working_set_runtime.reset_caches_for_tests()
    yield vault, calls, background
    working_set_runtime.reset_caches_for_tests()


def test_rare_word_gets_real_signature_corroboration_without_full_recall(signatures):
    vault, calls, _background = signatures
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
    vault, calls, _background = signatures
    packet = commands.op_activate_context(vault, turn="Cargo Sled or cedar?")
    assert packet["abstention"]["reason"] == "ambiguous"
    assert {a["title"] for a in packet["anchors"] if a["status"] == "resolved"} == {
        "Cargo Sled",
        "Cedar Carrier",
    }
    assert calls == []
    assert packet["units"] == []


def test_vectors_alone_never_create_resolved_context(signatures):
    vault, calls, _background = signatures
    packet = commands.op_activate_context(vault, turn="violet distant drums")
    assert packet["abstained"] is True
    assert all(a["status"] == "partial" for a in packet["anchors"])
    assert packet["units"] == packet["pointers"] == []
    assert calls == []


@pytest.mark.parametrize("state", ["busy", "unavailable", "warming"])
def test_transient_semantic_failure_does_not_poison_packet_cache(signatures, monkeypatch, state):
    vault, _calls, _background = signatures

    def unavailable(text):
        if state == "busy":
            raise ModelBusyError("busy")
        return None

    monkeypatch.setattr(embeddings, "embed_activation_query_if_loaded", unavailable)
    monkeypatch.setattr(readiness, "should_defer", lambda component: state == "warming")
    first = working_set_runtime.serve(vault, turn="Could the sled cope?", max_chars=4000)
    assert first["generation"]["semantic_evidence"] == state
    assert first["abstained"] is True
    monkeypatch.setattr(readiness, "should_defer", lambda component: False)
    monkeypatch.setattr(embeddings, "embed_activation_query_if_loaded", lambda text: QUERY)
    second = working_set_runtime.serve(vault, turn="Could the sled cope?", max_chars=4000)
    assert second["generation"]["semantic_evidence"] == "ready"
    assert second["abstained"] is False


def test_disabled_semantics_cannot_reuse_enabled_packet(signatures, monkeypatch):
    vault, _calls, _background = signatures
    first = working_set_runtime.serve(vault, turn="Could the sled cope?", max_chars=4000)
    assert first["abstained"] is False
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    second = working_set_runtime.serve(vault, turn="Could the sled cope?", max_chars=4000)
    assert second["generation"]["semantic_evidence"] == "disabled"
    assert second["abstained"] is True


def test_explicit_agent_choice_does_not_request_an_encode(signatures, monkeypatch):
    vault, _calls, _background = signatures
    index = working_set_index.WorkingSetIndex(vault)
    chosen = next(row for row in index.anchors() if row.title == "Cargo Sled")
    index.close()
    encodes = []
    monkeypatch.setattr(embeddings, "embed_activation_query_if_loaded", lambda text: encodes.append(text))
    packet = commands.op_activate_context(
        vault, turn="violet distant drums", anchor=chosen.ref or chosen.path
    )
    assert packet["abstained"] is False
    assert packet["generation"]["semantic_evidence"] == "agent_choice"
    assert encodes == []


@pytest.mark.parametrize("turn", ["sled rated", "cargo 400 kg"])
def test_lean_activation_preserves_page_content_corroboration(signatures, monkeypatch, turn):
    from test_latency_gate import _seed_freshness_live

    vault, calls, _background = signatures
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
    vault, _calls, _background = signatures
    requested = []

    def catalog(*args, **kwargs):
        requested.append(kwargs)
        return lexstore.CatalogQueryResult(None, lexstore.CatalogReadiness("stale", False, "fts5"))

    monkeypatch.setattr(lexstore, "search_bm25_result", catalog)
    first = commands.op_activate_context(vault, turn="Cargo Sled")
    assert requested and requested[0]["allow_delta"] is False
    # Anchor pages only (the fixture's dozen plus the planted background),
    # never the vault's whole page set.
    assert 0 < len(requested[0]["allowed_paths"]) < 20 + BACKGROUND
    assert first["generation"]["lexical_evidence"] == "stale"
    assert working_set_runtime._PACKET_CACHE == {}


@pytest.mark.parametrize("turn", ["Give me a poem about a sled.", "sled sleds", "the sled"])
def test_one_shared_word_is_not_its_own_lexical_corroboration(signatures, monkeypatch, turn):
    from test_latency_gate import _seed_freshness_live

    vault, _calls, _background = signatures
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
    vault, _calls, _background = signatures
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

    vault, _calls, _background = signatures
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

    vault, _calls, _background = signatures
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
    vault, _calls, _background = signatures
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
    vault, _calls, background = signatures
    for page in background:
        page.unlink()
    index = working_set_index.WorkingSetIndex(vault)
    index.update()
    index.close()
    working_set_runtime.reset_caches_for_tests()
    encodes = []
    monkeypatch.setattr(
        embeddings, "embed_activation_query_if_loaded", lambda text: encodes.append(text) or QUERY
    )

    packet = commands.op_activate_context(vault, turn="Could the sled cope?")

    assert packet["generation"]["semantic_evidence"] == "uncalibrated"
    sled = next(a for a in packet["anchors"] if a["title"] == "Cargo Sled")
    assert "vector_band" not in sled["evidence"]
    assert sled["status"] == "partial"
    assert encodes == []


@pytest.mark.parametrize(
    ("turn", "corroborated"),
    [
        ("東京タワーの高さ", False),
        ("東京タワー、会議の議事録", True),
        ("Zölvarn", False),
        ("Zölvarn rollout", True),
        ("zolvarn rollout", True),
    ],
)
def test_one_word_or_one_unspaced_run_is_one_unit_of_lexical_evidence(
    signatures, monkeypatch, turn, corroborated
):
    """An accented word (surface plus folded variant) or a CJK run (many
    bigrams) is one piece of evidence: it never earns `retrieval` alone."""
    from test_latency_gate import _seed_freshness_live

    vault, _calls, _background = signatures
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    _write(
        vault / "Knowledge Base/Products/Tower Wagon.md",
        "---\ntype: note\nstatus: active\n---\n# Tower Wagon\n\n"
        "東京タワーの高さは三百メートル。会議の議事録を共有。Zölvarn plans the rollout.\n",
    )
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    rows = [row for row in index.anchors() if row.title == "Tower Wagon"]
    index.close()
    assert rows
    _seed_freshness_live(vault)
    lexstore.ensure_fresh(vault)

    hits, status = working_set_runtime.lexical_evidence(vault, turn, rows, limit=5)

    assert status == "available"
    assert bool(hits) is corroborated


@pytest.mark.parametrize("turn", ["明日は、散歩です", "昨日は、雨でした", "日曜日は、映画です"])
def test_shared_particles_never_make_an_unrelated_anchor_retrieved(signatures, monkeypatch, turn):
    """A Japanese turn and an anchor page that share only particle bigrams
    (日は, です) are not about the same thing: no anchor earns `retrieval`."""
    from test_latency_gate import _seed_freshness_live

    vault, _calls, _background = signatures
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    _write(
        vault / "Knowledge Base/Products/Weekend Wagon.md",
        "---\ntype: note\nstatus: active\n---\n# Weekend Wagon\n\n"
        "今日は晴れです。週末は家族と過ごします。\n",
    )
    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    rows = index.anchors()
    index.close()
    _seed_freshness_live(vault)
    lexstore.ensure_fresh(vault)

    hits, status = working_set_runtime.lexical_evidence(vault, turn, rows, limit=8)
    assert status == "available"
    assert hits == []
    packet = commands.op_activate_context(vault, turn=turn)
    assert all("retrieval" not in anchor["evidence"] for anchor in packet["anchors"])


def test_an_install_without_an_encoder_serves_the_old_packet_from_the_cache(vault, monkeypatch):
    """Embeddings not disabled, but no encoder ever ran (an install without the
    extra): the state is `absent`, as before step 4, the packet is the one
    embeddings-off serves, and a repeated turn is served from the packet cache."""
    _seed_structure(vault)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(readiness, "should_defer", lambda component: False)
    monkeypatch.setattr(
        embeddings, "embed_activation_query_if_loaded", lambda text: pytest.fail("there is no encoder to ask")
    )
    from test_latency_gate import _seed_freshness_live

    index = working_set_index.WorkingSetIndex(vault)
    index.rebuild()
    index.close()
    _seed_freshness_live(vault)
    lexstore.ensure_fresh(vault)
    turn = "Could the Cargo Sled cope?"

    def shape(packet):
        return {key: packet[key] for key in ("abstained", "anchors", "units", "recent_context", "current_state")}

    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    working_set_runtime.reset_caches_for_tests()
    off = commands.op_activate_context(vault, turn=turn)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS")
    working_set_runtime.reset_caches_for_tests()
    compiles = []
    real = working_set.compile_packet
    monkeypatch.setattr(working_set, "compile_packet", lambda *a, **k: compiles.append(1) or real(*a, **k))

    packets = [commands.op_activate_context(vault, turn=turn) for _ in range(3)]

    assert [packet["generation"]["semantic_evidence"] for packet in packets] == ["absent"] * 3
    assert len(compiles) == 1, "a settled state is cached"
    assert all(shape(packet) == shape(off) for packet in packets)
