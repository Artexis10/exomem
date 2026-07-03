"""Tests for claim-level hygiene (extraction + .claims.sqlite sidecar + polarity).

Three lanes, all torch-free unless noted:
- extraction: claim-bearing sections → a claim string (deterministic).
- sidecar: checksum-keyed upsert / incremental skip / delete, exercised with
  FAKE vectors (monkeypatched `embeddings.embed_texts`) so no model loads.
- polarity: the deterministic heuristic backend + the classify dispatch seam.
- wiring: `detect_contradictions` attaches polarity under the gate, and stays
  byte-identical to baseline when the gate is off.

The one model-loading test (real bge claim vectors) import-skips without torch.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from exomem import claims, corpus_aware, embeddings, find as find_module


# ---------------- extraction (deterministic, torch-free) ----------------


def test_extract_insight_uses_claim_section() -> None:
    body = "# Retrieval needs owned files\n\n## Claim\n\nTools should retrieve from owned files.\n\n## Why\n\nBecause silos.\n"
    out = claims.extract_claim_text("Retrieval needs owned files", body, page_type="insight")
    assert out is not None
    assert out.startswith("Retrieval needs owned files\n\n")
    assert "Tools should retrieve from owned files." in out
    # The claim body is the Claim section, NOT the Why section.
    assert "Because silos." not in out


def test_extract_experiment_uses_conclusion_section() -> None:
    body = "# Batch review\n\n## Hypothesis\n\nBatching helps.\n\n## Conclusion\n\nBatching cut context switches.\n"
    out = claims.extract_claim_text("Batch review", body, page_type="experiment")
    assert "Batching cut context switches." in out
    assert "Batching helps." not in out  # hypothesis is not the conclusion


def test_extract_decision_uses_decision_section() -> None:
    body = "# Adopt bge\n\n## Context\n\nNeeded a model.\n\n## Decision\n\nUse bge-base for retrieval.\n"
    out = claims.extract_claim_text(
        "Adopt bge", body, page_type="entity", entity_type="decision"
    )
    assert "Use bge-base for retrieval." in out
    assert "Needed a model." not in out


def test_extract_type_agnostic_scan_finds_claim_when_type_unknown() -> None:
    # A write-time draft: only title+body in hand (page_type=None) → union scan.
    body = "# X\n\n## Claim\n\nThe asserted thing.\n"
    out = claims.extract_claim_text("X", body, page_type=None)
    assert "The asserted thing." in out


def test_extract_falls_back_to_lead_paragraph_without_sections() -> None:
    # No known claim section → H1 + first paragraph. No format is imposed.
    body = "# Some title\n\nA free-form first paragraph with no sections.\n\nSecond para.\n"
    out = claims.extract_claim_text("Some title", body, page_type="insight")
    assert out == "Some title\n\nA free-form first paragraph with no sections."


def test_extract_title_only_when_body_empty() -> None:
    assert claims.extract_claim_text("Just a title", "", page_type="insight") == "Just a title"


def test_extract_returns_none_when_nothing() -> None:
    assert claims.extract_claim_text("", "", page_type="insight") is None


def test_extract_caps_claim_words() -> None:
    long_claim = " ".join(f"w{i}" for i in range(400))
    body = f"# T\n\n## Claim\n\n{long_claim}\n"
    out = claims.extract_claim_text("T", body, page_type="insight")
    # title line + capped claim body.
    assert len(out.split("\n\n", 1)[1].split()) == claims.CLAIM_MAX_WORDS


def test_split_sections_separates_h1_and_h2() -> None:
    h1, sections = claims._split_sections("# Title\n\n## Claim\n\nBody a\n\n## Why\n\nBody b\n")
    assert h1 == "Title"
    assert sections["claim"] == "Body a"
    assert sections["why"] == "Body b"


def test_checksum_stable_and_changes() -> None:
    a = claims._checksum("X\n\nclaim one")
    assert a == claims._checksum("X\n\nclaim one")   # stable
    assert a != claims._checksum("X\n\nclaim two")   # sensitive to the claim


# ---------------- polarity heuristic (deterministic, torch-free) ----------------


def test_polarity_contradict_via_antonym() -> None:
    r = claims._heuristic_polarity("Caching improves latency", "Caching degrades latency")
    assert r.label == "contradict"
    assert r.method == "heuristic"


def test_polarity_contradict_via_negation() -> None:
    r = claims._heuristic_polarity("Batching helps focus", "Batching does not help focus")
    assert r.label == "contradict"


def test_polarity_duplicate_on_identical() -> None:
    r = claims._heuristic_polarity("Retrieval needs owned files", "Retrieval needs owned files")
    assert r.label == "duplicate"


def test_polarity_refine_same_topic_added_detail() -> None:
    r = claims._heuristic_polarity("Batching helps focus", "Batching helps focus in the morning")
    assert r.label == "refine"


def test_polarity_unrelated_on_disjoint() -> None:
    r = claims._heuristic_polarity("Cats are mammals", "Batching helps focus")
    assert r.label == "unrelated"


def test_classify_polarity_dispatches_to_heuristic_by_default(monkeypatch) -> None:
    monkeypatch.delenv("EXOMEM_CLAIM_POLARITY_NLI", raising=False)
    r = claims.classify_polarity("Caching improves latency", "Caching degrades latency")
    assert r.method == "heuristic"
    assert r.label == "contradict"


def test_classify_polarity_score_bounded() -> None:
    for a, b in [
        ("Caching improves latency", "Caching degrades latency"),
        ("X is true", "Y is unrelated"),
        ("same claim", "same claim"),
    ]:
        r = claims.classify_polarity(a, b)
        assert 0.0 <= r.score <= 1.0


# ---------------- gate ----------------


def test_claim_level_gate_default_off(monkeypatch) -> None:
    monkeypatch.delenv("EXOMEM_CLAIM_LEVEL", raising=False)
    assert claims.claim_level_enabled() is False


def test_claim_level_gate_on(monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_CLAIM_LEVEL", "1")
    assert claims.claim_level_enabled() is True


def test_max_polarity_pairs_default_and_override(monkeypatch) -> None:
    monkeypatch.delenv("EXOMEM_CLAIM_POLARITY_MAX_PAIRS", raising=False)
    assert claims._max_polarity_pairs() == 20
    monkeypatch.setenv("EXOMEM_CLAIM_POLARITY_MAX_PAIRS", "5")
    assert claims._max_polarity_pairs() == 5
    monkeypatch.setenv("EXOMEM_CLAIM_POLARITY_MAX_PAIRS", "junk")
    assert claims._max_polarity_pairs() == 20


# ---------------- sidecar (FAKE vectors — torch-free) ----------------


def _fake_embed(texts, is_query=False):
    """Deterministic unit-ish vectors so the sidecar round-trips without a model."""
    out = np.zeros((len(texts), embeddings.VECTOR_DIM), dtype=np.float32)
    for i, t in enumerate(texts):
        out[i, hash(t) % embeddings.VECTOR_DIM] = 1.0
    return out


def _seed_claim_md(
    vault: Path, rel: str, *, type_: str, h1: str, claim: str,
    section: str = "Claim", status: str = "active", entity_type: str | None = None,
) -> str:
    p = vault / "Knowledge Base" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    fm = [f"type: {type_}", f"status: {status}", "created: 2026-01-01", "updated: 2026-01-01"]
    if entity_type:
        fm.append(f"entity_type: {entity_type}")
    p.write_text(
        "---\n" + "\n".join(fm) + "\n---\n\n"
        f"# {h1}\n\n## {section}\n\n{claim}\n",
        encoding="utf-8",
    )
    return f"Knowledge Base/{rel}"


@pytest.fixture
def _claims_on(monkeypatch):
    monkeypatch.setenv("EXOMEM_CLAIM_LEVEL", "1")
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "embed_texts", _fake_embed)
    claims.clear_claim_indexes()


def test_sidecar_upsert_get_and_all(vault: Path) -> None:
    idx = claims.ClaimIndex(vault)
    vec = np.zeros(embeddings.VECTOR_DIM, dtype=np.float32)
    vec[3] = 1.0
    idx.upsert_many([("Knowledge Base/Notes/Insights/a.md", "T\n\nclaim", "sum1", vec, "insight", "active", 1.0)])
    row = idx.get_row("Knowledge Base/Notes/Insights/a.md")
    assert row is not None
    assert row[0] == "T\n\nclaim"
    assert row[2] == "insight"
    md, mat = idx.all_claims()
    assert len(md) == 1 and mat.shape == (1, embeddings.VECTOR_DIM)
    assert idx.checksums() == {"Knowledge Base/Notes/Insights/a.md": "sum1"}
    idx.delete("Knowledge Base/Notes/Insights/a.md")
    assert idx.get_row("Knowledge Base/Notes/Insights/a.md") is None


def test_upsert_claims_after_write_noop_when_gate_off(vault: Path, monkeypatch) -> None:
    monkeypatch.delenv("EXOMEM_CLAIM_LEVEL", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    rel = _seed_claim_md(vault, "Notes/Insights/g.md", type_="insight", h1="G", claim="A claim.")
    find_module.clear_cache()
    claims.clear_claim_indexes()
    claims.upsert_claims_after_write(vault, [vault / "Knowledge Base" / "Notes/Insights/g.md"])
    # Gate off → no sidecar file is ever created.
    assert not claims.sidecar_path(vault).exists()


def test_upsert_claims_incremental_skips_unchanged(vault: Path, _claims_on) -> None:
    rel = _seed_claim_md(vault, "Notes/Insights/inc.md", type_="insight", h1="Inc", claim="Original claim.")
    find_module.clear_cache()
    path = vault / "Knowledge Base" / "Notes/Insights/inc.md"

    calls = {"n": 0}
    real = _fake_embed

    def _spy(texts, is_query=False):
        calls["n"] += 1
        return real(texts, is_query=is_query)

    import exomem.embeddings as emod
    orig = emod.embed_texts
    emod.embed_texts = _spy
    try:
        claims.upsert_claims_after_write(vault, [path])
        assert calls["n"] == 1  # first index → one encode
        idx = claims.get_claim_index(vault)
        assert "Knowledge Base/Notes/Insights/inc.md" in idx.checksums()

        # Second call, page unchanged → checksum matches → NO re-embed.
        claims.upsert_claims_after_write(vault, [path])
        assert calls["n"] == 1

        # Change the CLAIM → checksum differs → re-embed.
        path.write_text(
            "---\ntype: insight\nstatus: active\ncreated: 2026-01-01\nupdated: 2026-01-01\n---\n\n"
            "# Inc\n\n## Claim\n\nA totally different claim now.\n",
            encoding="utf-8",
        )
        find_module.clear_cache()
        claims.upsert_claims_after_write(vault, [path])
        assert calls["n"] == 2
    finally:
        emod.embed_texts = orig


def test_upsert_claims_drops_non_compiled(vault: Path, _claims_on) -> None:
    # A raw source is not a compiled conclusion → no claim row.
    _seed_claim_md(vault, "Sources/Other/raw.md", type_="source", h1="Raw", claim="Just a capture.")
    find_module.clear_cache()
    claims.upsert_claims_after_write(vault, [vault / "Knowledge Base" / "Sources/Other/raw.md"])
    idx = claims.get_claim_index(vault)
    assert idx.get_row("Knowledge Base/Sources/Other/raw.md") is None


def test_claim_text_for_page_live_extraction_fallback(vault: Path, monkeypatch) -> None:
    # No sidecar built → falls back to live extraction from the parsed page.
    monkeypatch.setenv("EXOMEM_CLAIM_LEVEL", "1")
    rel = _seed_claim_md(vault, "Notes/Insights/live.md", type_="insight", h1="Live", claim="Live extracted claim.")
    find_module.clear_cache()
    claims.clear_claim_indexes()
    txt = claims.claim_text_for_page(vault, rel)
    assert txt is not None and "Live extracted claim." in txt


# ---------------- wiring: proximity → polarity through detect_contradictions ----------------


def test_detect_contradictions_attaches_polarity_when_gated(vault: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_CLAIM_LEVEL", "1")
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    cand_rel = _seed_claim_md(
        vault, "Notes/Insights/caching.md", type_="insight",
        h1="Caching improves latency", claim="Caching improves latency.",
    )
    find_module.clear_cache()
    claims.clear_claim_indexes()
    # precomputed keeps it torch-free (no _best_cosine_per_file / model).
    out = corpus_aware.detect_contradictions(
        vault,
        title="Caching latency note",
        body="# Caching latency note\n\n## Claim\n\nCaching degrades latency.\n",
        precomputed={cand_rel: 0.85},
        top_n=10,
    )
    assert len(out) == 1
    assert out[0].polarity == "contradict"
    assert out[0].polarity_method == "heuristic"
    # The polarity surfaces through the existing overlap_warning surface.
    w = corpus_aware.overlap_warning(out[0])
    assert "CONTRADICTS" in w
    assert out[0].as_dict()["polarity"] == "contradict"


def test_detect_contradictions_polarity_absent_when_gate_off(vault: Path, monkeypatch) -> None:
    monkeypatch.delenv("EXOMEM_CLAIM_LEVEL", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    cand_rel = _seed_claim_md(
        vault, "Notes/Insights/c2.md", type_="insight",
        h1="Caching improves latency", claim="Caching improves latency.",
    )
    find_module.clear_cache()
    claims.clear_claim_indexes()
    out = corpus_aware.detect_contradictions(
        vault, title="t", body="b", precomputed={cand_rel: 0.85}, top_n=10
    )
    assert len(out) == 1
    assert out[0].polarity is None
    # Byte-identical baseline: as_dict omits polarity, warning is the old string.
    assert out[0].as_dict() == {"path": cand_rel, "title": "Caching improves latency", "cosine": 0.85}
    assert "claim-level check" not in corpus_aware.overlap_warning(out[0])


def test_overlap_warning_byte_identical_without_polarity() -> None:
    c = corpus_aware.DupCandidate(path="Notes/Insights/x", title="X", cosine=0.86)
    w = corpus_aware.overlap_warning(c)
    assert w == (
        "overlaps active note [[Notes/Insights/x]] (cosine 0.86) "
        "— review: does this restate, refine, or contradict it? supersede the "
        "stale one if they conflict"
    )


# ---------------- semantic (model-loading) ----------------

pytest.importorskip("sentence_transformers")
pytest.importorskip("torch")


def test_rebuild_all_builds_real_claim_vectors(vault: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_CLAIM_LEVEL", "1")
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    embeddings._IMPORT_FAILED = False
    claims.clear_claim_indexes()
    _seed_claim_md(vault, "Notes/Insights/real.md", type_="insight", h1="Real", claim="A real claim to embed.")
    find_module.clear_cache()
    idx = claims.ClaimIndex(vault)
    n = idx.rebuild_all()
    assert n >= 1
    row = idx.get_row("Knowledge Base/Notes/Insights/real.md")
    assert row is not None
    assert row[1].shape == (embeddings.VECTOR_DIM,)
    assert np.isfinite(row[1]).all()
