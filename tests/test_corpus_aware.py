"""Tests for corpus-aware writes (suggest_related + detect_duplicates).

Logic tests (path canon, self/already-linked exclusion, hub re-rank) monkeypatch
find() and run torch-free. Semantic tests build the real sidecar over the fixture
vault and exercise dedup + note()'s suggestion block; they import-skip without
torch and lift the suite-wide embeddings gate.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path

import numpy as np
import pytest

from exomem import add as add_module
from exomem import corpus_aware, embeddings
from exomem import edit as edit_module
from exomem import find as find_module
from exomem import note as note_module

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


def _seed_hit_file(vault: Path, path: str) -> None:
    """Create a minimal in-KB target so eligibility tests exercise access."""
    target = vault / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Candidate\n\nCandidate body.\n", encoding="utf-8")


def test_suggest_related_excludes_self_and_already_linked(
    vault: Path, monkeypatch
) -> None:
    fake = [
        _hit("Knowledge Base/Notes/Insights/self.md"),
        _hit("Knowledge Base/Notes/Insights/linked.md"),
        _hit("Knowledge Base/Notes/Insights/fresh1.md"),
        _hit("Knowledge Base/Notes/Insights/fresh2.md"),
    ]
    for path in (
        "Knowledge Base/Notes/Insights/self.md",
        "Knowledge Base/Notes/Insights/linked.md",
        "Knowledge Base/Notes/Insights/fresh1.md",
        "Knowledge Base/Notes/Insights/fresh2.md",
    ):
        _seed_hit_file(vault, path)
    monkeypatch.setattr(find_module, "find", lambda *a, **k: fake)
    out = corpus_aware.suggest_related(
        vault, title="t", body="b",
        self_path="Knowledge Base/Notes/Insights/self",
        existing_links={"Knowledge Base/Notes/Insights/linked"},
        limit=8,
    )
    paths = {corpus_aware._canon(s.path) for s in out}
    assert "notes/insights/self" not in paths      # never suggest itself
    assert "notes/insights/linked" not in paths     # already linked
    assert {"notes/insights/fresh1", "notes/insights/fresh2"} <= paths


def test_suggest_related_prefers_hubs(vault: Path, monkeypatch) -> None:
    # find ranks the leaf first; a strongly-connected hub sits just below it.
    # The hub bonus must lift the hub to the top.
    fake = [
        _hit("Knowledge Base/Notes/Insights/leaf.md", gid=0),
        _hit("Knowledge Base/Notes/Insights/hub.md", gid=100),
    ]
    for path in (
        "Knowledge Base/Notes/Insights/leaf.md",
        "Knowledge Base/Notes/Insights/hub.md",
    ):
        _seed_hit_file(vault, path)
    monkeypatch.setattr(find_module, "find", lambda *a, **k: fake)
    out = corpus_aware.suggest_related(vault, title="t", body="b", limit=8)
    assert corpus_aware._canon(out[0].path) == "notes/insights/hub"


def test_suggest_related_why_mentions_signals(vault: Path, monkeypatch) -> None:
    fake = [_hit("Knowledge Base/Notes/Insights/x.md", gid=5, vr=2)]
    _seed_hit_file(vault, "Knowledge Base/Notes/Insights/x.md")
    monkeypatch.setattr(find_module, "find", lambda *a, **k: fake)
    out = corpus_aware.suggest_related(vault, title="t", body="b")
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
    candidate_path = _seed_md(vault, "Notes/Insights/x.md", type_="insight")
    cand = corpus_aware.DupCandidate(
        path=candidate_path, title="X", cosine=0.86
    )
    monkeypatch.setattr(corpus_aware, "detect_duplicates", lambda *a, **k: [])
    monkeypatch.setattr(corpus_aware, "detect_contradictions", lambda *a, **k: [cand])
    res = note_module.note(
        vault,
        content="Body.",
        note_type="insight",
        title="New One",
        tags=["t"],
        status="draft",
    )
    assert any("overlaps active note" in w for w in res.warnings), res.warnings


def test_add_surfaces_overlap_warning(
    vault, source_schema, _no_embed_writes, monkeypatch
) -> None:
    candidate_path = _seed_md(vault, "Notes/Insights/x.md", type_="insight")
    cand = corpus_aware.DupCandidate(
        path=candidate_path, title="X", cosine=0.86
    )
    monkeypatch.setattr(corpus_aware, "detect_duplicates", lambda *a, **k: [])
    monkeypatch.setattr(corpus_aware, "detect_contradictions", lambda *a, **k: [cand])
    res = add_module.add(
        vault, source_schema, content="Body.", source_type="other", title="New Capture"
    )
    assert any("overlaps active note" in w for w in res.warnings), res.warnings


def test_add_path_composes_declared_rival_filter(
    vault, source_schema, _no_embed_writes, monkeypatch
) -> None:
    candidate_path = _seed_md(vault, "Sources/Other/rival.md", type_="source")
    candidate = corpus_aware.DupCandidate(
        path=candidate_path, title="Rival", cosine=0.96
    )
    monkeypatch.setattr(corpus_aware, "detect_duplicates", lambda *a, **k: [candidate])
    monkeypatch.setattr(corpus_aware, "detect_contradictions", lambda *a, **k: [])
    monkeypatch.setattr(
        "exomem.contradiction_stance.DeclaredPairFilter",
        lambda *args, **kwargs: lambda _path: True,
    )

    result = add_module.add(
        vault,
        source_schema,
        content="Intentional rival source.",
        source_type="other",
        title="New Rival Capture",
    )

    assert not any("near-duplicate" in warning for warning in result.warnings)


def test_add_commits_when_post_commit_advisory_emission_fails(
    vault, source_schema, _no_embed_writes, monkeypatch
) -> None:
    monkeypatch.setattr(corpus_aware, "detect_duplicates", lambda *a, **k: [])
    monkeypatch.setattr(corpus_aware, "detect_contradictions", lambda *a, **k: [])
    monkeypatch.setattr(
        corpus_aware,
        "emit_write_advisory_groups",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("broken state")),
    )

    result = add_module.add(
        vault,
        source_schema,
        content="Committed even without advisory state.",
        source_type="other",
        title="Fail-open Capture",
    )

    assert (vault / result.path).is_file()


def test_edit_body_change_surfaces_overlap(vault, _no_embed_writes, monkeypatch) -> None:
    target = _seed_md(vault, "Notes/Insights/editable.md", type_="insight")
    find_module.clear_cache()
    candidate_path = _seed_md(vault, "Notes/Insights/x.md", type_="insight")
    cand = corpus_aware.DupCandidate(
        path=candidate_path, title="X", cosine=0.86
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
        vault,
        content="Body.",
        note_type="insight",
        title="Shared Pass One",
        tags=["t"],
        status="draft",
    )
    assert calls["n"] == 1  # dup + contradiction partitions share ONE encode


# ---------------- semantic (model-loading) ----------------


_INSIGHT = "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md"


@pytest.fixture
def embeddings_enabled(monkeypatch):
    pytest.importorskip("sentence_transformers")
    pytest.importorskip("torch")
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
        status="draft",
        suggestions=True,  # default-off since #576; this asserts the pass itself
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


# ---------------- the post-commit sweep reuses the commit's own encode ----------------
#
# A note write encodes the page's chunks once when its commit publishes them to
# the sidecar, then ran the near-dup/contradiction sweep, which encoded the very
# same chunk texts again. On a CPU-only cell that second encode was most of the
# sweep's 9-13 s. These run torch-free with a deterministic encoder, so they pin
# the wiring and the exactness, not model numerics.


@pytest.fixture
def counting_encoder(monkeypatch):
    """A deterministic, torch-free text encoder that records every text it encodes.

    The vector is seeded from the text after its title line, so a draft that
    repeats an existing page's paragraphs scores cosine 1.0 against that page,
    which is what a real near-duplicate looks like to the sweep.
    """
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "_IMPORT_FAILED", False)
    encoded: list[str] = []

    def fake(texts, *, is_query=False):
        encoded.extend(texts)
        rows = []
        for text in texts:
            key = text.split("\n\n", 1)[-1]
            seed = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "little")
            vector = np.random.default_rng(seed).standard_normal(embeddings.VECTOR_DIM)
            rows.append(vector / np.linalg.norm(vector))
        return np.asarray(rows, dtype=np.float32).reshape(len(texts), embeddings.VECTOR_DIM)

    monkeypatch.setattr(embeddings, "embed_texts", fake)
    monkeypatch.setattr(embeddings, "get_model", lambda: object())
    return encoded


_REUSE_PARAGRAPHS = [
    "Reuse probe paragraph one about bounded retry budgets.",
    "Reuse probe paragraph two about idempotent handlers.",
    "Reuse probe paragraph three about jittered backoff.",
    "Reuse probe paragraph four about circuit breakers.",
]


def test_note_sweep_does_not_re_encode_what_its_commit_published(
    vault: Path, counting_encoder: list[str]
) -> None:
    embeddings.get_embedding_index(vault).rebuild_all()
    counting_encoder.clear()
    body = "\n\n".join(_REUSE_PARAGRAPHS)

    note_module.note(
        vault, content=body, note_type="insight", title="Reuse probe", status="draft"
    )

    draft_chunks = embeddings.chunk_text("Reuse probe", body)
    counts = Counter(counting_encoder)
    assert [counts[chunk] for chunk in draft_chunks] == [1] * len(draft_chunks), (
        "each draft chunk must be encoded once per write -- by the commit that "
        "publishes it -- and the advisory sweep must reuse that vector"
    )


def test_reused_vectors_score_exactly_as_a_fresh_encode(
    vault: Path, counting_encoder: list[str]
) -> None:
    body = "\n\n".join(_REUSE_PARAGRAPHS)
    twin = _seed_md(vault, "Notes/Insights/reuse-twin.md", type_="insight", body=body)
    embeddings.get_embedding_index(vault).rebuild_all()

    result = note_module.note(
        vault, content=body, note_type="insight", title="Reuse probe", status="draft"
    ).as_dict()

    # The warning the write returns is the one a fresh encode produces.
    assert any(
        w.startswith(f"possible near-duplicate of [[{twin.removesuffix('.md')}")
        or w.startswith(f"possible near-duplicate of [[{twin}")
        for w in result["warnings"]
    ), result["warnings"]
    fresh = corpus_aware._best_cosine_per_file(vault, title="Reuse probe", body=body)
    counting_encoder.clear()
    reused = corpus_aware._best_cosine_per_file(
        vault, title="Reuse probe", body=body, published_path=result["path"]
    )
    assert counting_encoder == []
    assert reused == fresh
    assert reused[twin] == pytest.approx(1.0, abs=1e-6)


def test_sweep_encodes_when_the_commit_published_nothing(
    vault: Path, counting_encoder: list[str], monkeypatch
) -> None:
    """Reuse is an economy, never a dependency: no stored rows, full encode, same warning."""
    body = "\n\n".join(_REUSE_PARAGRAPHS)
    twin = _seed_md(vault, "Notes/Insights/reuse-twin.md", type_="insight", body=body)
    embeddings.get_embedding_index(vault).rebuild_all()
    monkeypatch.setattr(
        embeddings,
        "upsert_after_write_status",
        lambda _root, paths, **_k: embeddings.EmbeddingSyncStatus(
            "disabled", "embeddings_disabled", len(paths)
        ),
    )
    counting_encoder.clear()

    result = note_module.note(
        vault, content=body, note_type="insight", title="Reuse probe", status="draft"
    ).as_dict()

    draft_chunks = embeddings.chunk_text("Reuse probe", body)
    assert Counter(counting_encoder) == Counter(draft_chunks)
    assert any(twin.removesuffix(".md") in w for w in result["warnings"]), result["warnings"]


def test_sweep_scores_every_draft_chunk_in_one_pass_over_the_matrix(
    vault: Path, counting_encoder: list[str], monkeypatch
) -> None:
    """One matrix read per sweep, not one per chunk, and no chunk text it never uses."""
    embeddings.get_embedding_index(vault).rebuild_all()
    calls = {"all_vectors": 0, "texts": 0}
    real_all_vectors = embeddings.EmbeddingIndex.all_vectors
    real_texts_for = embeddings.EmbeddingIndex._texts_for

    def counted_all_vectors(self):
        calls["all_vectors"] += 1
        return real_all_vectors(self)

    def counted_texts_for(self, pairs):
        calls["texts"] += 1
        return real_texts_for(self, pairs)

    monkeypatch.setattr(embeddings.EmbeddingIndex, "all_vectors", counted_all_vectors)
    monkeypatch.setattr(embeddings.EmbeddingIndex, "_texts_for", counted_texts_for)

    scores = corpus_aware._best_cosine_per_file(
        vault, title="One pass probe", body="\n\n".join(_REUSE_PARAGRAPHS)
    )

    assert scores, "the sweep found nothing, so the pass count proves nothing"
    assert calls == {"all_vectors": 1, "texts": 0}


def test_sweep_does_not_walk_the_corpus(
    vault: Path, counting_encoder: list[str], monkeypatch
) -> None:
    """Eligibility is decided for the pages that reach a chunk's top-k, not the vault.

    The sweep built its allowed-path set by walking every indexed page on every
    write (~0.5 s at ~3,000 pages, growing with the vault) and then consulted it
    for a few dozen candidates.
    """
    from exomem import index_paths

    embeddings.get_embedding_index(vault).rebuild_all()

    def no_walk(_root):
        raise AssertionError("the advisory sweep walked the whole corpus")

    monkeypatch.setattr(index_paths, "iter_index_markdown", no_walk)

    scores = corpus_aware._best_cosine_per_file(
        vault, title="No walk probe", body="\n\n".join(_REUSE_PARAGRAPHS)
    )

    assert scores


def test_sweep_never_scores_a_page_the_index_walk_excludes(
    vault: Path, counting_encoder: list[str]
) -> None:
    """Stray sidecar rows -- a trashed copy, a deleted page -- never reach the sweep."""
    body = "\n\n".join(_REUSE_PARAGRAPHS)
    live = _seed_md(vault, "Notes/Insights/stray-live.md", type_="insight", body=body)
    trashed = _seed_md(vault, "_trash/stray-trashed.md", type_="insight", body=body)
    gone = _seed_md(vault, "Notes/Insights/stray-gone.md", type_="insight", body=body)
    embeddings.get_embedding_index(vault).rebuild_all()
    index = embeddings.get_embedding_index(vault)
    for stray in (trashed, gone):
        page_chunks = embeddings.chunk_text(stray, body)
        index.upsert_file(stray, page_chunks, embeddings.embed_texts(page_chunks), 1.0)
    (vault / gone).unlink()

    scores = corpus_aware._best_cosine_per_file(vault, title="Stray probe", body=body)

    assert live in scores
    assert trashed not in scores
    assert gone not in scores


def test_sweep_span_counts_the_unique_texts_it_actually_encoded(
    vault: Path, counting_encoder: list[str]
) -> None:
    """`texts`/`chars` say what the encoder was handed, not how many chunks missed.

    On partial reuse only the unique missing texts are encoded, so a repeated
    new paragraph is one text, not two.
    """
    from exomem import call_spans

    embeddings.get_embedding_index(vault).rebuild_all()
    body = "\n\n".join(_REUSE_PARAGRAPHS)
    written = note_module.note(
        vault, content=body, note_type="insight", title="Span probe", status="draft"
    ).as_dict()
    fresh = "A fresh paragraph the commit never saw."
    draft = "\n\n".join([*_REUSE_PARAGRAPHS, fresh, fresh])
    counting_encoder.clear()

    handle = call_spans.MCP_CALL_TOKEN.set("u5c-span-count")
    try:
        corpus_aware._best_cosine_per_file(
            vault, title="Span probe", body=draft, published_path=written["path"]
        )
        spans = {span["name"]: span for span in call_spans.pop_call_spans("u5c-span-count")}
    finally:
        call_spans.MCP_CALL_TOKEN.reset(handle)

    fresh_chunk = f"Span probe\n\n{fresh}"
    assert counting_encoder == [fresh_chunk]
    assert spans["advisory.best_cosine"]["fields"] == {
        "texts": 1,
        "chars": len(fresh_chunk),
        "reused": len(_REUSE_PARAGRAPHS),
    }


# ---------------- suggest_relations reads a stored page's vectors back ----------------


def test_suggest_relations_reads_a_stored_page_s_vectors_back(
    vault: Path, counting_encoder: list[str]
) -> None:
    from exomem import epistemic_graph

    body = "\n\n".join(_REUSE_PARAGRAPHS)
    page = _seed_md(vault, "Notes/Insights/relations-reuse.md", type_="insight", body=body)
    twin = _seed_md(vault, "Notes/Insights/relations-twin.md", type_="insight", body=body)
    embeddings.get_embedding_index(vault).rebuild_all()
    find_module.clear_cache()
    counting_encoder.clear()

    result = epistemic_graph.suggest_relations(vault, path=page, limit=50)

    assert counting_encoder == [], "suggest_relations re-encoded a page whose rows are current"
    proximity = [c for c in result["candidates"] if c["method"] == "embedding_proximity"]
    assert any(c["to"] == twin for c in proximity), result["candidates"]


# ---------------- a write encodes each new text once ----------------
#
# `add` sweeps its draft BEFORE committing it (so the capture cannot match
# itself) and its commit then published the same chunk texts by encoding them a
# second time. An edit's sweep scores the page's bare paragraphs, which no
# sidecar row holds, so the page's next edit encoded every unchanged paragraph
# again. A vector is a function of its text, so a text the sweep just encoded is
# handed on instead of being encoded twice.


def test_add_commit_reuses_the_vectors_its_own_advisory_encoded(
    vault: Path, source_schema, counting_encoder: list[str]
) -> None:
    embeddings.get_embedding_index(vault).rebuild_all()
    counting_encoder.clear()
    body = "\n\n".join(_REUSE_PARAGRAPHS)

    result = add_module.add(
        vault, source_schema, content=body, source_type="other", title="Capture reuse probe"
    )

    draft_chunks = embeddings.chunk_text("Capture reuse probe", body)
    counts = Counter(counting_encoder)
    assert [counts[chunk] for chunk in draft_chunks] == [1] * len(draft_chunks), (
        "each capture chunk must be encoded once per add -- by the advisory sweep "
        "that runs before the commit -- and the commit must reuse that vector"
    )
    stored, _units = embeddings.get_embedding_index(vault).stored_text_vectors(result.path)
    assert set(draft_chunks) <= set(stored)
    fresh = embeddings.embed_texts(list(stored))
    assert all(
        np.array_equal(stored[text], vector)
        for text, vector in zip(stored, fresh, strict=True)
    )


def test_a_repeated_edit_encodes_only_the_paragraphs_it_changed(
    vault: Path, counting_encoder: list[str]
) -> None:
    body = "\n\n".join(_REUSE_PARAGRAPHS)
    target = _seed_md(
        vault, "Notes/Insights/edit-reuse.md", type_="insight", status="draft", body=body
    )
    embeddings.get_embedding_index(vault).rebuild_all()
    find_module.clear_cache()
    first = "First appended paragraph about lease renewal."
    second = "Second appended paragraph about fencing tokens."
    edit_module.edit(vault, path=target, why="first", new_body=f"{body}\n\n{first}")
    counting_encoder.clear()

    edit_module.edit(vault, path=target, why="second", new_body=f"{body}\n\n{first}\n\n{second}")

    assert counting_encoder, "the second edit encoded nothing, so the count proves nothing"
    assert all(text.endswith(second) for text in counting_encoder), (
        "the second edit re-encoded paragraphs the first edit had already encoded: "
        f"{[text for text in counting_encoder if not text.endswith(second)]}"
    )
