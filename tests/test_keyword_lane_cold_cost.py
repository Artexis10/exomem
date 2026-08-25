"""Regression coverage for the keyword lane's watcher-live cold path."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from exomem import find as find_module
from exomem import freshness, lexstore, readiness
from exomem.vault import walk_vault_md

pytestmark = pytest.mark.skipif(
    not lexstore.fts5_available(), reason="this SQLite build lacks FTS5/trigram"
)


def _write_page(
    root: Path,
    rel: str,
    body: str,
    *,
    updated: str = "2026-08-01",
) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    title = path.stem
    path.write_text(
        f"---\ntype: insight\ntitle: {title}\nupdated: {updated}\n---\n"
        f"# {title}\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def _seed_live(root: Path) -> None:
    kb = root / "Knowledge Base"
    freshness.seed(
        root,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in walk_vault_md(root)),
    )
    freshness.seed(
        root,
        "kb",
        (
            (str(path), freshness.stat_signature(path))
            for path in find_module._walk_md(kb)
        ),
    )


def _materialize_live_catalog(root: Path, query: str) -> None:
    _seed_live(root)
    checkpoint = freshness.recall_checkpoint(root, "kb")
    assert (
        lexstore.search_substring(
            root,
            query,
            scope="kb",
            freshness=checkpoint.triple,
            repair=True,
        )
        is not None
    )


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch):
    lexstore.reset_memo()
    lexstore.clear_stores()
    find_module.clear_cache()
    readiness.reset()
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "fts5")
    yield
    lexstore.reset_memo()
    lexstore.clear_stores()
    find_module.clear_cache()
    readiness.reset()


def test_one_write_reconciles_keyword_catalog_in_proportion_to_the_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _write_page(
        tmp_path,
        "Knowledge Base/Notes/target.md",
        "stablemarker oldkeywordpayload",
        updated="2026-08-20",
    )
    for index in range(79):
        _write_page(
            tmp_path,
            f"Knowledge Base/Notes/filler-{index:03d}.md",
            f"stablemarker unrelated payload {index}",
        )
    _materialize_live_catalog(tmp_path, "oldkeywordpayload")

    target.write_text(
        target.read_text(encoding="utf-8").replace(
            "oldkeywordpayload", "newkeywordpayload"
        ),
        encoding="utf-8",
    )
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    freshness.on_files_changed(tmp_path, changed=[target])
    find_module._CACHE.clear()

    walked = 0
    parsed = 0
    delta_calls = 0
    original_walk = find_module._walk_md
    original_get = find_module._CACHE.get
    original_delta = freshness.recall_delta_since

    def counted_walk(root: Path):
        nonlocal walked
        for path in original_walk(root):
            walked += 1
            yield path

    def counted_get(*args, **kwargs):
        nonlocal parsed
        parsed += 1
        return original_get(*args, **kwargs)

    def counted_delta(*args, **kwargs):
        nonlocal delta_calls
        delta_calls += 1
        return original_delta(*args, **kwargs)

    monkeypatch.setattr(find_module, "_walk_md", counted_walk)
    monkeypatch.setattr(find_module._CACHE, "get", counted_get)
    monkeypatch.setattr(freshness, "recall_delta_since", counted_delta)
    monkeypatch.setattr(lexstore, "_schedule_repair", lambda _root: None)

    checkpoint = freshness.recall_checkpoint(tmp_path, "kb")
    paths = find_module._keyword_match_paths(
        tmp_path,
        "newkeywordpayload",
        "kb",
        freshness=checkpoint.triple,
        repair=False,
    )

    assert paths == ["Knowledge Base/Notes/target.md"]
    assert walked == 0
    assert parsed <= 1
    assert delta_calls == 1


def test_small_repairing_sidecar_decline_still_returns_the_reference_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_page(
        tmp_path,
        "Knowledge Base/Notes/newer.md",
        "sharedneedle payload",
        updated="2026-08-20",
    )
    _write_page(
        tmp_path,
        "Knowledge Base/Notes/older.md",
        "sharedneedle payload",
        updated="2026-08-01",
    )
    _materialize_live_catalog(tmp_path, "sharedneedle")
    checkpoint = freshness.recall_checkpoint(tmp_path, "kb")
    healthy = find_module._keyword_match_paths(
        tmp_path,
        "sharedneedle",
        "kb",
        freshness=checkpoint.triple,
        repair=True,
    )

    monkeypatch.setattr(lexstore, "search_substring", lambda *args, **kwargs: None)
    declined = find_module._keyword_match_paths(
        tmp_path,
        "sharedneedle",
        "kb",
        freshness=checkpoint.triple,
        repair=True,
    )

    assert declined == healthy == [
        "Knowledge Base/Notes/newer.md",
        "Knowledge Base/Notes/older.md",
    ]


def test_large_cold_keyword_catalog_returns_typed_warming_without_a_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(80):
        _write_page(
            tmp_path,
            f"Knowledge Base/Notes/page-{index:03d}.md",
            f"coldkeyword payload {index}",
        )
    _seed_live(tmp_path)
    checkpoint = freshness.recall_checkpoint(tmp_path, "kb")
    walked = 0
    parsed = 0

    def forbidden_walk(_root: Path):
        nonlocal walked
        walked += 1
        raise AssertionError("a production-size request must not walk the vault")
        yield  # pragma: no cover - keep this a generator-shaped test double

    def forbidden_parse(*_args, **_kwargs):
        nonlocal parsed
        parsed += 1
        raise AssertionError("a production-size request must not parse pages")

    monkeypatch.setattr(find_module, "_walk_md", forbidden_walk)
    monkeypatch.setattr(find_module._CACHE, "get", forbidden_parse)
    monkeypatch.setattr(lexstore, "_schedule_repair", lambda _root: None)

    with pytest.raises(find_module.RetrievalIndexWarming) as raised:
        find_module._keyword_match_paths(
            tmp_path,
            "coldkeyword",
            "kb",
            freshness=checkpoint.triple,
            repair=False,
        )

    assert raised.value.code == "RETRIEVAL_INDEX_WARMING"
    assert raised.value.status == "warming"
    assert walked == 0
    assert parsed == 0


def test_large_lazy_empty_query_returns_typed_warming_without_a_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(70):
        _write_page(
            tmp_path,
            f"Knowledge Base/Notes/page-{index:03d}.md",
            f"emptyquery payload {index}",
        )
    _seed_live(tmp_path)
    walked = 0
    parsed = 0

    def forbidden_walk(_root: Path):
        nonlocal walked
        walked += 1
        raise AssertionError("an incomplete empty query must not walk the vault")
        yield  # pragma: no cover - keep this a generator-shaped test double

    def forbidden_parse(*_args, **_kwargs):
        nonlocal parsed
        parsed += 1
        raise AssertionError("an incomplete empty query must not parse pages")

    monkeypatch.setattr(find_module, "_walk_md", forbidden_walk)
    monkeypatch.setattr(find_module._CACHE, "get", forbidden_parse)
    monkeypatch.setattr(lexstore, "_schedule_repair", lambda _root: None)

    with pytest.raises(find_module.RetrievalIndexWarming):
        find_module.find(tmp_path, query="", mode="keyword", scope="kb")

    assert walked == 0
    assert parsed == 0


def test_transient_large_catalog_query_returns_typed_unavailable_without_a_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(80):
        _write_page(
            tmp_path,
            f"Knowledge Base/Notes/page-{index:03d}.md",
            f"transientneedle payload {index}",
        )
    _materialize_live_catalog(tmp_path, "transientneedle")
    checkpoint = freshness.recall_checkpoint(tmp_path, "kb")
    walked = 0

    def forbidden_walk(_root: Path):
        nonlocal walked
        walked += 1
        raise AssertionError("a transient query failure must not walk the vault")
        yield  # pragma: no cover - keep this a generator-shaped test double

    monkeypatch.setattr(find_module, "_walk_md", forbidden_walk)
    monkeypatch.setattr(
        lexstore,
        "search_substring_result",
        lambda *_args, **_kwargs: lexstore.CatalogQueryResult(
            None,
            lexstore.CatalogReadiness("transient_failure", False, "fts5"),
        ),
    )

    with pytest.raises(find_module.RetrievalIndexWarming) as raised:
        find_module._keyword_match_paths(
            tmp_path,
            "transientneedle",
            "kb",
            freshness=checkpoint.triple,
            repair=False,
        )

    assert raised.value.status == "temporarily_unavailable"
    assert walked == 0


@pytest.mark.parametrize("search_mode", ["keyword", "hybrid", "vector"])
def test_active_catalog_warm_refuses_every_mode_before_a_cold_freshness_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, search_mode: str
) -> None:
    for index in range(80):
        _write_page(
            tmp_path,
            f"Knowledge Base/Notes/page-{index:03d}.md",
            f"preseedrace payload {index}",
        )
    walked = 0

    def forbidden_walk(_root: Path):
        nonlocal walked
        walked += 1
        raise AssertionError("admission must run before cold freshness walks")
        yield  # pragma: no cover - keep this a generator-shaped test double

    readiness.begin_warm()
    monkeypatch.setattr(find_module, "_walk_md", forbidden_walk)
    monkeypatch.setattr(
        freshness,
        "recall_projection_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("admission must run before projection fallback")
        ),
    )

    with pytest.raises(find_module.RetrievalIndexWarming):
        find_module.find(
            tmp_path,
            query="preseedrace",
            mode=search_mode,
            scope="kb",
            graph=False,
            temporal=False,
        )

    assert walked == 0


def test_catalog_proof_cannot_admit_runtime_without_live_projections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_page(
        tmp_path,
        "Knowledge Base/Notes/target.md",
        "projectionproof payload",
    )
    _seed_live(tmp_path)
    monkeypatch.setattr(
        lexstore,
        "get_store",
        lambda _root: type(
            "CurrentStore",
            (),
            {
                "catalog_readiness": lambda self, *_args, **_kwargs: (
                    lexstore.CatalogReadiness("available", True, "fts5")
                )
            },
        )(),
    )
    freshness.invalidate(tmp_path)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(tmp_path.resolve()))
    readiness.begin_warm()
    readiness.finish_warm()

    lexstore._mark_runtime_retrieval_ready_if_current(tmp_path)

    assert readiness.retrieval_admission() == {
        "state": "unavailable",
        "admitted": False,
    }

    _seed_live(tmp_path)
    lexstore._mark_runtime_retrieval_ready_if_current(tmp_path)

    assert readiness.retrieval_admission() == {
        "state": "ready",
        "admitted": True,
    }


def test_ready_runtime_losing_projection_demotes_without_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_page(
        tmp_path,
        "Knowledge Base/Notes/target.md",
        "projectionloss payload",
    )
    _seed_live(tmp_path)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(tmp_path.resolve()))
    readiness.begin_warm()
    readiness.mark_ready("retrieval_catalog")
    readiness.finish_warm()
    freshness.invalidate(tmp_path)
    scheduled: list[Path] = []
    monkeypatch.setattr(lexstore, "request_repair", lambda root: scheduled.append(root))
    monkeypatch.setattr(
        freshness,
        "recall_projection_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a ready runtime must demote before projection fallback")
        ),
    )

    with pytest.raises(find_module.RetrievalIndexWarming):
        find_module.find(
            tmp_path,
            query="projectionloss",
            mode="vector",
            scope="kb",
            graph=False,
            temporal=False,
        )

    assert scheduled == [tmp_path]
    assert readiness.retrieval_admission() == {
        "state": "unavailable",
        "admitted": False,
    }


def test_declined_projection_is_named_in_find_timings(tmp_path: Path) -> None:
    timings = find_module.FindTimings()
    readiness.begin_warm()

    with pytest.raises(find_module.RetrievalIndexWarming):
        find_module.find(
            tmp_path,
            query="timingprojection",
            mode="vector",
            scope="kb",
            graph=False,
            temporal=False,
            timings=timings,
        )

    stage = timings.as_dict()["stages"]["recall_projection"]
    assert stage["outcome"] == "warming"
    assert stage["ms"] >= 0.0


def test_managed_runtime_refuses_before_warm_begins_without_projection_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readiness.manage_runtime()
    monkeypatch.setattr(
        freshness,
        "recall_projection_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("managed pre-activation request must not walk")
        ),
    )

    with pytest.raises(find_module.RetrievalIndexWarming):
        find_module.find(
            tmp_path,
            query="preactivation",
            mode="vector",
            scope="kb",
            graph=False,
            temporal=False,
        )


def test_failed_catalog_warm_retries_repair_before_refusing_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(80):
        _write_page(
            tmp_path,
            f"Knowledge Base/Notes/page-{index:03d}.md",
            f"retryrepair payload {index}",
        )
    scheduled: list[Path] = []

    def forbidden_walk(_root: Path):
        raise AssertionError("failed-warm retry must precede cold freshness walks")
        yield  # pragma: no cover - keep this a generator-shaped test double

    readiness.begin_warm()
    readiness.finish_warm()
    monkeypatch.setattr(find_module, "_walk_md", forbidden_walk)
    monkeypatch.setattr(
        lexstore,
        "request_repair",
        lambda root: scheduled.append(root),
        raising=False,
    )

    with pytest.raises(find_module.RetrievalIndexWarming):
        find_module.find(tmp_path, query="retryrepair", mode="keyword", scope="kb")

    assert scheduled == [tmp_path]


def test_large_cold_hybrid_catalog_returns_typed_warming_without_a_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(80):
        _write_page(
            tmp_path,
            f"Knowledge Base/Notes/page-{index:03d}.md",
            f"coldhybrid payload {index}",
        )
    _seed_live(tmp_path)
    walked = 0
    parsed = 0

    def forbidden_walk(_root: Path):
        nonlocal walked
        walked += 1
        raise AssertionError("a production-size request must not walk the vault")
        yield  # pragma: no cover - keep this a generator-shaped test double

    def forbidden_parse(*_args, **_kwargs):
        nonlocal parsed
        parsed += 1
        raise AssertionError("a production-size request must not parse pages")

    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setattr(find_module, "_walk_md", forbidden_walk)
    monkeypatch.setattr(find_module._CACHE, "get", forbidden_parse)
    monkeypatch.setattr(lexstore, "_schedule_repair", lambda _root: None)

    with pytest.raises(find_module.RetrievalIndexWarming) as raised:
        find_module.find(
            tmp_path,
            query="coldhybrid",
            mode="hybrid",
            scope="kb",
            graph=False,
            temporal=False,
        )

    assert raised.value.code == "RETRIEVAL_INDEX_WARMING"
    assert raised.value.status == "warming"
    assert walked == 0
    assert parsed == 0


def test_successful_background_repair_marks_configured_catalog_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(80):
        _write_page(
            tmp_path,
            f"Knowledge Base/Notes/page-{index:03d}.md",
            f"backgroundrepair payload {index}",
        )
    _seed_live(tmp_path)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(tmp_path.resolve()))

    lexstore._schedule_repair(tmp_path)

    assert lexstore.await_repairs_idle(tmp_path, timeout=30.0) is True
    assert readiness.retrieval_admission() == {
        "state": "ready",
        "admitted": True,
    }


def test_small_lazy_repair_eventually_admits_configured_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(4):
        _write_page(
            tmp_path,
            f"Knowledge Base/Notes/page-{index:03d}.md",
            f"smalllazy payload {index}",
        )
    _seed_live(tmp_path)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(tmp_path.resolve()))

    hits = find_module.find(
        tmp_path,
        query="smalllazy",
        mode="keyword",
        scope="kb-only",
    )

    assert len(hits) == 4
    assert lexstore.await_repairs_idle(tmp_path, timeout=30.0) is True
    assert readiness.retrieval_admission() == {
        "state": "ready",
        "admitted": True,
    }


def test_discovered_catalog_staleness_revokes_configured_runtime_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(80):
        _write_page(
            tmp_path,
            f"Knowledge Base/Notes/page-{index:03d}.md",
            f"laterstale payload {index}",
        )
    _seed_live(tmp_path)
    lexstore.ensure_fresh(tmp_path)
    store = lexstore.get_store(tmp_path)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(tmp_path.resolve()))
    readiness.begin_warm()
    readiness.mark_ready("retrieval_catalog")
    readiness.finish_warm()
    store.path.unlink()
    monkeypatch.setattr(lexstore, "_schedule_repair", lambda _root: None)

    with pytest.raises(find_module.RetrievalIndexWarming):
        find_module.find(tmp_path, query="laterstale", mode="keyword", scope="kb")

    assert readiness.retrieval_admission() == {
        "state": "unavailable",
        "admitted": False,
    }


@pytest.mark.parametrize("operation", ["upsert", "delete"])
def test_failed_catalog_mutation_revokes_configured_runtime_before_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    page = _write_page(
        tmp_path,
        "Knowledge Base/Notes/target.md",
        "mutationfailure payload",
    )
    store = lexstore.get_store(tmp_path)
    store._failed = True
    scheduled: list[Path] = []
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(tmp_path.resolve()))
    monkeypatch.setattr(
        lexstore,
        "_schedule_repair",
        lambda root, **_kwargs: scheduled.append(root),
    )
    readiness.begin_warm()
    readiness.mark_ready("retrieval_catalog")
    readiness.finish_warm()

    applied = (
        store.upsert_paths([page])
        if operation == "upsert"
        else store.delete_rel_paths(["Knowledge Base/Notes/target.md"])
    )

    assert applied is False
    assert scheduled == [tmp_path]
    assert readiness.retrieval_admission() == {
        "state": "unavailable",
        "admitted": False,
    }


def test_large_cold_vault_widening_returns_typed_warming_without_a_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_page(
        tmp_path,
        "Knowledge Base/Notes/kb-target.md",
        "widenneedle kb payload",
    )
    _materialize_live_catalog(tmp_path, "widenneedle")
    outside: list[Path] = []
    for index in range(80):
        outside.append(
            _write_page(
                tmp_path,
                f"Reference/page-{index:03d}.md",
                f"widenneedle outside payload {index}",
            )
        )
    freshness.on_files_changed(tmp_path, changed=outside)
    walked = 0

    def forbidden_walk(_root: Path):
        nonlocal walked
        walked += 1
        raise AssertionError("auto-widen must not walk a production-size vault")
        yield  # pragma: no cover - keep this a generator-shaped test double

    monkeypatch.setattr(find_module, "_walk_md", forbidden_walk)
    monkeypatch.setattr(lexstore, "_schedule_repair", lambda _root: None)

    with pytest.raises(find_module.RetrievalIndexWarming):
        find_module.find(
            tmp_path,
            query="widenneedle",
            mode="keyword",
            scope="kb",
        )

    assert walked == 0


def test_access_policy_change_is_not_served_from_a_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_page(
        tmp_path,
        "Knowledge Base/Notes/Private/secret.md",
        "policyneedle private",
        updated="2026-08-20",
    )
    _write_page(
        tmp_path,
        "Knowledge Base/Notes/public.md",
        "policyneedle public",
        updated="2026-08-01",
    )
    _materialize_live_catalog(tmp_path, "policyneedle")
    store = lexstore.get_store(tmp_path)

    (tmp_path / "Knowledge Base/_access.yaml").write_text(
        "excluded:\n  - Notes/Private\n", encoding="utf-8"
    )

    def forbidden_delta_apply(*args, **kwargs):
        raise AssertionError("an access-policy transition must take the full path")

    monkeypatch.setattr(store, "_apply_delta_rows", forbidden_delta_apply)
    monkeypatch.setattr(lexstore, "_schedule_repair", lambda _root: None)
    checkpoint = freshness.recall_checkpoint(tmp_path, "kb")

    paths = find_module._keyword_match_paths(
        tmp_path,
        "policyneedle",
        "kb",
        freshness=checkpoint.triple,
        repair=True,
    )

    assert paths == ["Knowledge Base/Notes/public.md"]


def test_nonrepairing_incomplete_delta_returns_warming_without_a_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _write_page(
        tmp_path,
        "Knowledge Base/Notes/target.md",
        "stablemarker beforedelta",
    )
    for index in range(4):
        _write_page(
            tmp_path,
            f"Knowledge Base/Notes/filler-{index}.md",
            f"stablemarker filler {index}",
        )
    _materialize_live_catalog(tmp_path, "beforedelta")

    target.write_text(
        target.read_text(encoding="utf-8").replace("beforedelta", "afterdelta"),
        encoding="utf-8",
    )
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    freshness.on_files_changed(tmp_path, changed=[target])

    original_delta = freshness.recall_delta_since
    original_walk = find_module._walk_md
    walked = 0

    def incomplete(*args, **kwargs):
        delta = original_delta(*args, **kwargs)
        return delta._replace(
            complete=False,
            changed=frozenset(),
            deleted=frozenset(),
            target_signatures=(),
        )

    def counted_walk(root: Path):
        nonlocal walked
        for path in original_walk(root):
            walked += 1
            yield path

    monkeypatch.setattr(freshness, "recall_delta_since", incomplete)
    monkeypatch.setattr(find_module, "_walk_md", counted_walk)
    monkeypatch.setattr(lexstore, "_schedule_repair", lambda _root: None)
    checkpoint = freshness.recall_checkpoint(tmp_path, "kb")

    with pytest.raises(find_module.RetrievalIndexWarming):
        find_module._keyword_match_paths(
            tmp_path,
            "afterdelta",
            "kb",
            freshness=checkpoint.triple,
            repair=False,
        )

    assert walked == 0
