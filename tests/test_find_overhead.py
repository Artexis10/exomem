"""reduce-find-per-query-overhead: per-request freshness snapshot, digest
freshness keys (rename/delete safe), derived-text memoization, single-pass
post-RRF multipliers, and startup warm-up. Global invariant: byte-identical
find results."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from exomem import bm25, warmup
from exomem import find as find_module


def _count_walks(vault: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Count TOP-LEVEL kb-tree and vault-tree walks (_walk_md recurses into
    itself per subdirectory; only root-level invocations are walks)."""
    from exomem import vault as vault_module

    kb_root = vault / "Knowledge Base"
    counts = {"kb": 0, "vault": 0}
    orig_kb = find_module._walk_md
    orig_vault = vault_module.walk_vault_md

    def kb_walk(root: Path):
        if root == kb_root:
            counts["kb"] += 1
        return orig_kb(root)

    def vault_walk(root: Path):
        counts["vault"] += 1
        return orig_vault(root)

    monkeypatch.setattr(find_module, "_walk_md", kb_walk)
    monkeypatch.setattr(vault_module, "walk_vault_md", vault_walk)
    return counts


def test_steady_state_walk_budget(vault: Path, monkeypatch) -> None:
    """A warmed repeat hybrid query stat-walks each scope at most once
    (freshness snapshot) plus the keyword lane's single parse walk."""
    monkeypatch.setenv("EXOMEM_FIND_CACHE_SIZE", "0")  # force the lanes to run
    find_module.find(vault, query="metabolism")  # warm BM25/resolver/pages
    counts = _count_walks(vault, monkeypatch)
    find_module.find(vault, query="metabolism")
    # kb walks: snapshot.kb() + keyword lane. vault walks: snapshot.vault()
    # (shared by auto-widen BM25 + resolver freshness check).
    assert counts["kb"] <= 2, counts
    assert counts["vault"] <= 1, counts


def test_kb_only_scope_never_walks_vault(vault: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_FIND_CACHE_SIZE", "0")
    find_module.find(vault, query="metabolism", scope="kb-only", graph=False)
    counts = _count_walks(vault, monkeypatch)
    find_module.find(vault, query="metabolism", scope="kb-only", graph=False)
    assert counts["vault"] == 0, counts


def test_new_file_visible_immediately(vault: Path) -> None:
    """The snapshot is per-request — never cached across requests."""
    new = vault / "Knowledge Base" / "Notes" / "overhead-staleness-probe.md"
    new.parent.mkdir(parents=True, exist_ok=True)
    new.write_text("# Probe\n\nmetabolism overhead probe\n", encoding="utf-8")
    hits = find_module.find(vault, query="metabolism overhead probe")
    assert any(h.path.endswith("overhead-staleness-probe.md") for h in hits)


def test_bm25_sees_rename(vault: Path) -> None:
    """Digest freshness key: a pure rename (mtime preserved) rebuilds the
    corpus — the old max-mtime `>` check served stale paths."""
    old = vault / "Knowledge Base" / "Notes" / "rename-probe-old.md"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text("# Rename probe\n\nzanzibar quixotic marker\n", encoding="utf-8")
    hits = bm25.search(vault, "zanzibar quixotic", k=5)
    assert any(p.endswith("rename-probe-old.md") for p, _ in hits)
    new = old.with_name("rename-probe-new.md")
    os.replace(old, new)  # preserves mtime; count unchanged
    hits = bm25.search(vault, "zanzibar quixotic", k=5)
    paths = [p for p, _ in hits]
    assert any(p.endswith("rename-probe-new.md") for p in paths)
    assert not any(p.endswith("rename-probe-old.md") for p in paths)


def test_bm25_sees_delete(vault: Path) -> None:
    doomed = vault / "Knowledge Base" / "Notes" / "delete-probe.md"
    doomed.parent.mkdir(parents=True, exist_ok=True)
    doomed.write_text("# Delete probe\n\nxylophone gargantuan marker\n", encoding="utf-8")
    hits = bm25.search(vault, "xylophone gargantuan", k=5)
    assert any(p.endswith("delete-probe.md") for p, _ in hits)
    doomed.unlink()
    hits = bm25.search(vault, "xylophone gargantuan", k=5)
    assert not any(p.endswith("delete-probe.md") for p, _ in hits)


def test_bm25_cache_status_and_unload_rebuilds(vault: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    bm25.clear_cache()
    cold = bm25.cache_status()
    assert cold == {
        "loaded": False,
        "corpora": 0,
        "documents": 0,
        "tokenized_documents": 0,
        "tokens": 0,
    }

    first = bm25.search(vault, "insulin", k=5)
    warm = bm25.cache_status()
    assert first
    assert warm["loaded"] is True
    assert warm["corpora"] == 1
    assert warm["documents"] > 0
    assert warm["tokenized_documents"] > 0
    assert warm["tokens"] > 0

    assert bm25.unload_cache() is True
    assert bm25.cache_status()["loaded"] is False
    assert bm25.search(vault, "insulin", k=5) == first


def test_resolver_rebuilds_on_rename(vault: Path) -> None:
    """The resolver's old (count, max-mtime) key missed pure renames."""
    a = vault / "Knowledge Base" / "Notes" / "resolver-rename-a.md"
    a.parent.mkdir(parents=True, exist_ok=True)
    a.write_text("# Resolver rename target\n", encoding="utf-8")
    r1 = find_module._get_query_resolver(vault)
    os.replace(a, a.with_name("resolver-rename-b.md"))
    r2 = find_module._get_query_resolver(vault)
    assert r1 is not r2


def test_derived_text_invalidates_with_page(vault: Path) -> None:
    p = vault / "Knowledge Base" / "Notes" / "derived-text-probe.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# Derived\n\nalpha bravo\n", encoding="utf-8")
    page1 = find_module._CACHE.get(p, vault)
    assert "alpha" in page1.body_norm
    assert bm25.stem_word("bravo") in page1.stem_set
    ns = p.stat().st_mtime_ns
    p.write_text("# Derived\n\ncharlie delta\n", encoding="utf-8")
    os.utime(p, ns=(ns + 2_000_000_000, ns + 2_000_000_000))
    page2 = find_module._CACHE.get(p, vault)
    assert page2 is not page1
    assert "charlie" in page2.body_norm
    assert bm25.stem_word("alpha") not in page2.stem_set


@pytest.mark.parametrize("prefer_compiled", [True, False])
@pytest.mark.parametrize("prefer_active", [True, False])
@pytest.mark.parametrize(
    ("temporal", "query", "boost"),
    [
        (True, "latest metabolism notes", 1.5),  # temporal-active path
        (True, "metabolism", 1.5),               # temporal query gate off
        (True, "latest metabolism notes", 1.0),  # boost gate off
        (False, "latest metabolism notes", 1.5), # temporal param off
    ],
)
def test_single_pass_matches_sequential_reference(
    vault: Path, prefer_compiled, prefer_active, temporal, query, boost
) -> None:
    """The one-pass multiplier helper reproduces the three sequential
    reference passes bit-for-bit across the full gating grid."""
    config = find_module.RankingConfig(temporal_boost=boost)
    # Fused candidates spanning types: compiled notes, sources, a superseded
    # page, media sidecars — whatever the fixture holds.
    paths = []
    kb = vault / "Knowledge Base"
    for p in find_module._walk_md(kb):
        page = find_module._CACHE.get(p, vault)
        if page is not None:
            paths.append(page.rel_path)
    assert len(paths) >= 5
    fused = [(p, 1.0 / (60 + i)) for i, p in enumerate(sorted(paths))]

    reference = list(fused)
    if prefer_compiled:
        reference = find_module._apply_type_boost(reference, vault, config)
    if prefer_active:
        reference = find_module._apply_status_demotion(reference, vault, config)
    if temporal:
        reference = find_module._apply_temporal_boost(reference, vault, query, config)

    memo: dict = {}

    def page_of(rel: str):
        if rel not in memo:
            memo[rel] = find_module._CACHE.get(vault / rel, vault)
        return memo[rel]

    combined = find_module._apply_post_rrf_multipliers(
        list(fused), query, config,
        prefer_compiled=prefer_compiled,
        prefer_active=prefer_active,
        temporal=temporal,
        page_of=page_of,
    )
    assert combined == reference


def test_single_pass_all_off_returns_input_unchanged(vault: Path) -> None:
    fused = [("b.md", 0.5), ("a.md", 0.5)]  # deliberately unsorted tie
    out = find_module._apply_post_rrf_multipliers(
        fused, "metabolism", find_module.RankingConfig(),
        prefer_compiled=False, prefer_active=False, temporal=True,
        page_of=lambda rel: None,
    )
    assert out is fused  # historical all-off path never re-sorted


def test_warm_caches_populates(vault: Path, monkeypatch) -> None:
    # The suite disables warm-up globally (conftest) so build_server never
    # spawns the warm thread; this test exercises the warm path explicitly.
    # Warm builds WHICHEVER backend serves the bm25 lane: under FTS5 the
    # lexical sidecar is synced/populated and the rank-bm25 corpus stays cold
    # on purpose (not holding N token lists resident is the backend's win);
    # on the python rung the corpus cache is built as it always was.
    from exomem import lexstore

    monkeypatch.delenv("EXOMEM_DISABLE_WARMUP", raising=False)
    warmup.warm_caches(vault, preload_cpu_caches=True)
    assert find_module._CACHE.entries  # pages parsed
    if lexstore.backend() != "python" and lexstore.fts5_available():
        assert lexstore.lexical_path(vault).exists()  # sidecar built by warm
    else:
        assert (vault, "kb") in bm25._INDEX._cache
        assert (vault, "vault") in bm25._INDEX._cache
    assert vault in find_module._RESOLVER_CACHE
    # Warmed state means the first query pays no corpus build.
    bm25._INDEX.last_tokenized = 0
    find_module.find(vault, query="metabolism")
    assert bm25._INDEX.last_tokenized == 0


def test_warmup_disabled_by_env(vault: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_WARMUP", "1")
    assert warmup.warm_caches(vault) == {}
    assert not find_module._CACHE.entries


def test_warmup_soft_fails_on_broken_vault(tmp_path: Path) -> None:
    # No Knowledge Base/ dir at all — every step must soft-fail, not raise.
    durations = warmup.warm_caches(tmp_path / "nonexistent")
    assert isinstance(durations, dict)


def test_quiet_cold_find_matches_warm_cache_find(vault: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    monkeypatch.setenv("EXOMEM_MODE", "quiet")
    find_module.clear_cache()
    bm25.clear_cache()
    assert warmup.warm_caches(vault) == {}
    cold = find_module.find(vault, query="metabolism", mode="hybrid", limit=10)

    monkeypatch.setenv("EXOMEM_MODE", "normal")
    find_module.clear_cache()
    bm25.clear_cache()
    warmup.warm_caches(vault)
    warm = find_module.find(vault, query="metabolism", mode="hybrid", limit=10)

    assert [h.path for h in cold] == [h.path for h in warm]
