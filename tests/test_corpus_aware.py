"""Tests for corpus-aware writes (suggest_related + detect_duplicates).

Logic tests (path canon, self/already-linked exclusion, hub re-rank) monkeypatch
find() and run torch-free. Semantic tests build the real sidecar over the fixture
vault and exercise dedup + note()'s suggestion block; they import-skip without
torch and lift the suite-wide embeddings gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import (
    add as add_module,
    corpus_aware,
    edit as edit_module,
    embeddings,
    find as find_module,
    note as note_module,
)


# ---------------- pure / torch-free logic ----------------


def test_canon_normalizes_equivalent_forms() -> None:
    a = corpus_aware._canon("Knowledge Base/Notes/Insights/x.md")
    b = corpus_aware._canon("Notes/Insights/x")
    c = corpus_aware._canon("Knowledge Base/Notes/Insights/x.md#a-heading")
    assert a == b == c == "notes/insights/x"


def _hit(path: str, *, gid: int = 0, vr: int | None = None, br: int | None = None):
    return find_module.Hit(
        path=path, type="insight", scope=None, title=path.rsplit("/", 1)[-1],
        updated="", excerpt="ex", bm25_rank=br, vector_rank=vr, graph_in_degree=gid,
    )


def test_suggest_related_excludes_self_and_already_linked(monkeypatch) -> None:
    fake = [
        _hit("Knowledge Base/Notes/Insights/self.md"),
        _hit("Knowledge Base/Notes/Insights/linked.md"),
        _hit("Knowledge Base/Notes/Insights/fresh1.md"),
        _hit("Knowledge Base/Notes/Insights/fresh2.md"),
    ]
    monkeypatch.setattr(find_module, "find", lambda *a, **k: fake)
    out = corpus_aware.suggest_related(
        Path("/unused"), title="t", body="b",
        self_path="Knowledge Base/Notes/Insights/self",
        existing_links={"Knowledge Base/Notes/Insights/linked"},
        limit=8,
    )
    paths = {corpus_aware._canon(s.path) for s in out}
    assert "notes/insights/self" not in paths      # never suggest itself
    assert "notes/insights/linked" not in paths     # already linked
    assert {"notes/insights/fresh1", "notes/insights/fresh2"} <= paths


def test_suggest_related_prefers_hubs(monkeypatch) -> None:
    # find ranks the leaf first; a strongly-connected hub sits just below it.
    # The hub bonus must lift the hub to the top.
    fake = [
        _hit("Knowledge Base/Notes/Insights/leaf.md", gid=0),
        _hit("Knowledge Base/Notes/Insights/hub.md", gid=100),
    ]
    monkeypatch.setattr(find_module, "find", lambda *a, **k: fake)
    out = corpus_aware.suggest_related(Path("/unused"), title="t", body="b", limit=8)
    assert corpus_aware._canon(out[0].path) == "notes/insights/hub"


def test_suggest_related_why_mentions_signals(monkeypatch) -> None:
    fake = [_hit("Knowledge Base/Notes/Insights/x.md", gid=5, vr=2)]
    monkeypatch.setattr(find_module, "find", lambda *a, **k: fake)
    out = corpus_aware.suggest_related(Path("/unused"), title="t", body="b")
    assert "semantic #2" in out[0].why
    assert "hub" in out[0].why  # gid >= 3


def test_detect_duplicates_noop_when_embeddings_disabled(vault: Path) -> None:
    # Runs under the suite-wide EXOMEM_DISABLE_EMBEDDINGS — must short-circuit to
    # [] without loading torch or touching a sidecar.
    assert corpus_aware.detect_duplicates(
        vault, title="anything", body="some body", types_filter=["insight"]
    ) == []


def test_dup_threshold_defaults_when_env_unset(monkeypatch) -> None:
    monkeypatch.delenv("EXOMEM_DUP_THRESHOLD", raising=False)
    assert corpus_aware._dup_threshold() == corpus_aware.DUP_THRESHOLD


def test_dup_threshold_honors_env_override(monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_DUP_THRESHOLD", "0.93")
    assert corpus_aware._dup_threshold() == 0.93


def test_dup_threshold_falls_back_on_garbage(monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_DUP_THRESHOLD", "loose")
    assert corpus_aware._dup_threshold() == corpus_aware.DUP_THRESHOLD


# ---------------- conflict (contradiction band) — torch-free ----------------
#
# The band partition + candidate restriction are exercised with a `precomputed`
# cosine map over seeded pages, so the LOGIC is tested deterministically without
# depending on actual embedding values (which can't be pinned to a band).


def _seed_md(
    vault: Path, rel: str, *, type_: str, status: str = "active", body: str = "Body."
) -> str:
    """Write a minimal parseable page at KB-relative `rel` (with .md). Returns the
    sidecar-form key: vault-relative WITH 'Knowledge Base/' prefix and .md."""
    p = vault / "Knowledge Base" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\ntype: {type_}\nstatus: {status}\n"
        f"created: 2026-01-01\nupdated: 2026-01-01\n---\n\n# {rel}\n\n{body}\n",
        encoding="utf-8",
    )
    return f"Knowledge Base/{rel}"


@pytest.fixture
def _no_embed_writes(monkeypatch):
    """Run a write's corpus block torch-free: stub the embedding machinery so
    note/add/edit exercise the contradiction WIRING without loading a model."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(corpus_aware, "_best_cosine_per_file", lambda *a, **k: {})
    monkeypatch.setattr(corpus_aware, "suggest_related", lambda *a, **k: [])
    monkeypatch.setattr(embeddings, "upsert_after_write", lambda *a, **k: None)


def test_detect_contradictions_noop_when_embeddings_disabled(vault: Path) -> None:
    # Suite-wide EXOMEM_DISABLE_EMBEDDINGS — must short-circuit to [].
    assert corpus_aware.detect_contradictions(vault, title="x", body="y") == []


def test_contradiction_floor_defaults_when_env_unset(monkeypatch) -> None:
    monkeypatch.delenv("EXOMEM_CONTRADICTION_FLOOR", raising=False)
    assert corpus_aware._contradiction_floor() == corpus_aware.CONTRADICTION_FLOOR


def test_contradiction_floor_honors_env_override(monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_CONTRADICTION_FLOOR", "0.80")
    assert corpus_aware._contradiction_floor() == 0.80


def test_contradiction_floor_falls_back_on_garbage(monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_CONTRADICTION_FLOOR", "close")
    assert corpus_aware._contradiction_floor() == corpus_aware.CONTRADICTION_FLOOR


def test_overlap_warning_is_honest() -> None:
    c = corpus_aware.DupCandidate(path="Notes/Insights/x", title="X", cosine=0.86)
    w = corpus_aware.overlap_warning(c)
    assert "[[Notes/Insights/x]]" in w
    assert "0.86" in w
    assert "review" in w.lower()
    # Honest: names contradiction as a QUESTION, never asserts duplicate/verdict.
    assert "near-duplicate" not in w.lower()
    assert "contradict" in w.lower()


def test_detect_contradictions_band_and_candidate_restriction(vault, monkeypatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    active = _seed_md(vault, "Notes/Insights/active-twin.md", type_="insight")
    superseded = _seed_md(
        vault, "Notes/Insights/old-twin.md", type_="insight", status="superseded"
    )
    source = _seed_md(vault, "Sources/Other/raw-twin.md", type_="source")
    (vault / "Knowledge Base" / "_access.yaml").write_text(
        "readonly:\n  - Products\nexcluded: []\n", encoding="utf-8"
    )
    readonly = _seed_md(vault, "Products/ro-twin.md", type_="insight")
    dup = _seed_md(vault, "Notes/Insights/dup-twin.md", type_="insight")
    below = _seed_md(vault, "Notes/Insights/far.md", type_="insight")
    find_module.clear_cache()

    precomputed = {
        active: 0.85,      # in band, active compiled, read-write → FLAGGED
        superseded: 0.86,  # in band but superseded → excluded
        source: 0.87,      # in band but not a compiled type → excluded
        readonly: 0.88,    # in band but readonly tier → excluded
        dup: 0.95,         # >= ceiling → a duplicate, not a contradiction
        below: 0.50,       # < floor → not in band
    }
    out = corpus_aware.detect_contradictions(
        vault, title="t", body="b", precomputed=precomputed, top_n=10
    )
    assert {corpus_aware._canon(c.path) for c in out} == {"notes/insights/active-twin"}, [
        c.as_dict() for c in out
    ]


def test_detect_contradictions_excludes_self(vault, monkeypatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    p = _seed_md(vault, "Notes/Insights/selfie.md", type_="insight")
    find_module.clear_cache()
    out = corpus_aware.detect_contradictions(
        vault, title="t", body="b",
        self_path="Knowledge Base/Notes/Insights/selfie",
        precomputed={p: 0.85}, top_n=10,
    )
    assert out == []


def test_detect_contradictions_inverted_band_disabled(vault, monkeypatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv("EXOMEM_CONTRADICTION_FLOOR", "0.95")  # >= dup ceiling 0.90
    p = _seed_md(vault, "Notes/Insights/twin.md", type_="insight")
    find_module.clear_cache()
    out = corpus_aware.detect_contradictions(
        vault, title="t", body="b", precomputed={p: 0.93}, top_n=10
    )
    assert out == []  # inverted band → disabled, nothing returned


def test_note_surfaces_overlap_warning(vault, _no_embed_writes, monkeypatch) -> None:
    cand = corpus_aware.DupCandidate(
        path="Knowledge Base/Notes/Insights/x", title="X", cosine=0.86
    )
    monkeypatch.setattr(corpus_aware, "detect_duplicates", lambda *a, **k: [])
    monkeypatch.setattr(corpus_aware, "detect_contradictions", lambda *a, **k: [cand])
    res = note_module.note(
        vault, content="Body.", note_type="insight", title="New One", tags=["t"]
    )
    assert any("overlaps active note" in w for w in res.warnings), res.warnings


def test_add_surfaces_overlap_warning(
    vault, source_schema, _no_embed_writes, monkeypatch
) -> None:
    cand = corpus_aware.DupCandidate(
        path="Knowledge Base/Notes/Insights/x", title="X", cosine=0.86
    )
    monkeypatch.setattr(corpus_aware, "detect_duplicates", lambda *a, **k: [])
    monkeypatch.setattr(corpus_aware, "detect_contradictions", lambda *a, **k: [cand])
    res = add_module.add(
        vault, source_schema, content="Body.", source_type="other", title="New Capture"
    )
    assert any("overlaps active note" in w for w in res.warnings), res.warnings


def test_edit_body_change_surfaces_overlap(vault, _no_embed_writes, monkeypatch) -> None:
    target = _seed_md(vault, "Notes/Insights/editable.md", type_="insight")
    find_module.clear_cache()
    cand = corpus_aware.DupCandidate(
        path="Knowledge Base/Notes/Insights/x", title="X", cosine=0.86
    )
    monkeypatch.setattr(corpus_aware, "detect_contradictions", lambda *a, **k: [cand])
    res = edit_module.edit(vault, path=target, why="refine", new_body="Rewritten claim.")
    assert any("overlaps active note" in w for w in res.warnings), res.warnings


def test_edit_tags_only_skips_contradiction(vault, _no_embed_writes, monkeypatch) -> None:
    target = _seed_md(vault, "Notes/Insights/retag-me.md", type_="insight")
    find_module.clear_cache()
    calls = {"n": 0}

    def _spy(*a, **k):
        calls["n"] += 1
        return [corpus_aware.DupCandidate(path="x", title="X", cosine=0.86)]

    monkeypatch.setattr(corpus_aware, "detect_contradictions", _spy)
    res = edit_module.edit(vault, path=target, why="retag", tags=["a", "b"])
    assert calls["n"] == 0  # body unchanged → check skipped, no embed
    assert not any("overlaps active note" in w for w in res.warnings)


def test_note_shares_one_embedding_pass(vault, monkeypatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(corpus_aware, "suggest_related", lambda *a, **k: [])
    monkeypatch.setattr(embeddings, "upsert_after_write", lambda *a, **k: None)
    calls = {"n": 0}

    def _spy(*a, **k):
        calls["n"] += 1
        return {}

    monkeypatch.setattr(corpus_aware, "_best_cosine_per_file", _spy)
    note_module.note(
        vault, content="Body.", note_type="insight", title="Shared Pass One", tags=["t"]
    )
    assert calls["n"] == 1  # dup + contradiction partitions share ONE encode


# ---------------- semantic (model-loading) ----------------

pytest.importorskip("sentence_transformers")
pytest.importorskip("torch")


_INSIGHT = "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md"


@pytest.fixture
def embeddings_enabled(monkeypatch):
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    embeddings._IMPORT_FAILED = False


def test_detect_duplicates_flags_near_identical(vault: Path, embeddings_enabled) -> None:
    embeddings.EmbeddingIndex(vault).rebuild_all()
    page = find_module._CACHE.get(vault / _INSIGHT, vault)
    assert page is not None
    # Feed an existing page's own content back as a "draft" → near-perfect cosine.
    dups = corpus_aware.detect_duplicates(
        vault, title=page.title, body=page.body, types_filter=["insight"]
    )
    match = next((d for d in dups if "progressive-disclosure" in d.path), None)
    assert match is not None, f"expected the twin insight flagged; got {dups}"
    assert match.cosine >= corpus_aware.DUP_THRESHOLD


def test_detect_duplicates_respects_type_filter(vault: Path, embeddings_enabled) -> None:
    embeddings.EmbeddingIndex(vault).rebuild_all()
    page = find_module._CACHE.get(vault / _INSIGHT, vault)
    # Filtering to a type the twin isn't → it must not be returned.
    dups = corpus_aware.detect_duplicates(
        vault, title=page.title, body=page.body, types_filter=["pattern"]
    )
    assert not any("progressive-disclosure" in d.path for d in dups)


def test_note_attaches_suggestions_for_near_twin(vault: Path, embeddings_enabled) -> None:
    embeddings.EmbeddingIndex(vault).rebuild_all()
    twin = find_module._CACHE.get(vault / _INSIGHT, vault)
    res = note_module.note(
        vault,
        content=twin.body,
        note_type="insight",
        title="Near twin of progressive disclosure",
        tags=["ux"],
    )
    d = res.as_dict()
    assert d["path"]
    # The original insight should surface as a related-link suggestion.
    assert d.get("suggestions"), "expected suggestions for a near-twin insight"
    sugg = {corpus_aware._canon(s["path"]) for s in d["suggestions"]}
    assert "notes/insights/progressive-disclosure-without-mode-fragmentation" in sugg


def test_detect_contradictions_excludes_real_near_identical(
    vault: Path, embeddings_enabled
) -> None:
    # With REAL embeddings: a page's own content scores >= the dup ceiling, so it
    # is a duplicate, NOT a contradiction — the band must exclude it.
    embeddings.EmbeddingIndex(vault).rebuild_all()
    page = find_module._CACHE.get(vault / _INSIGHT, vault)
    assert page is not None
    out = corpus_aware.detect_contradictions(vault, title=page.title, body=page.body)
    assert not any("progressive-disclosure" in c.path for c in out), [
        c.as_dict() for c in out
    ]
