"""Privacy-safe typed semantic-catalog outcomes and exact-lane timings."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from exomem import commands, freshness, lexstore
from exomem import find as find_module

_PROFILE_KEYS = {
    "capability",
    "backend",
    "outcome",
    "complete",
    "repair_state",
    "retry_after_ms",
}


@pytest.fixture(autouse=True)
def _fresh_state() -> Any:
    lexstore.reset_memo()
    lexstore.clear_stores()
    find_module.clear_cache()
    freshness.clear()
    yield
    lexstore.reset_memo()
    lexstore.clear_stores()
    find_module.clear_cache()
    freshness.clear()


def _write_note(root: Path, name: str) -> Path:
    path = root / "Knowledge Base" / "Notes" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    page_id = uuid.uuid5(uuid.NAMESPACE_URL, f"catalog-diagnostics:{name}")
    path.write_text(
        "---\n"
        "type: insight\n"
        f"title: {name}\n"
        f"exomem_id: {page_id}\n"
        "status: active\n"
        "updated: 2026-07-23\n"
        "---\n\n"
        f"# {name}\n\n- [constraint] Keep {name} bounded #code ^{name}\n",
        encoding="utf-8",
    )
    return path


def _seed(root: Path, paths: list[Path]) -> None:
    entries = [(str(path), freshness.stat_signature(path)) for path in paths]
    freshness.seed(root, "kb", entries)
    freshness.seed(root, "vault", entries)


@pytest.mark.parametrize(
    ("status", "complete", "repair_state"),
    [
        ("available", True, "none"),
        ("stale", False, "requested"),
        ("unsupported", False, "not_applicable"),
        ("transient_failure", False, "not_applicable"),
        ("fatal_failure", False, "replacement_needed"),
    ],
)
def test_catalog_profile_has_fixed_private_shape(
    status: str, complete: bool, repair_state: str
) -> None:
    profile = lexstore.catalog_timing_profile(
        lexstore.CatalogReadiness(status, complete, "auto")
    )
    assert set(profile) == _PROFILE_KEYS
    assert profile["capability"] == "semantic_catalog"
    assert profile["backend"] in {"metadata_only", "not_used"}
    assert profile["outcome"] == status
    assert profile["complete"] is complete
    assert profile["repair_state"] == repair_state
    assert profile["retry_after_ms"] is None if complete else profile["retry_after_ms"] > 0


@pytest.mark.parametrize("result_level", ["page", "unit"])
@pytest.mark.parametrize(
    ("status", "public_status"),
    [
        ("stale", "warming"),
        ("fatal_failure", "warming"),
        ("transient_failure", "temporarily_unavailable"),
        ("unsupported", "temporarily_unavailable"),
    ],
)
def test_incomplete_catalog_outcomes_are_typed_and_not_cached(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result_level: str,
    status: str,
    public_status: str,
) -> None:
    _seed(tmp_path, [])
    calls = 0

    def result(*_args: Any, **_kwargs: Any) -> lexstore.CatalogQueryResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            return lexstore.CatalogQueryResult(
                None, lexstore.CatalogReadiness(status, False, "metadata_only")
            )
        return lexstore.CatalogQueryResult(
            [], lexstore.CatalogReadiness("available", True, "metadata_only")
        )

    seam = (
        "search_semantic_parent_paths_result"
        if result_level == "page"
        else "search_semantic_units_result"
    )
    monkeypatch.setattr(lexstore, seam, result)
    request = {
        "query": "",
        "categories": ["constraint"],
        "result_level": result_level,
        "scope": "kb-only",
        "mode": "keyword",
        "graph": False,
        "rerank": False,
        "include_timings": True,
    }

    with pytest.raises(find_module.RetrievalIndexWarming) as caught:
        commands.op_find(tmp_path, **request)
    assert caught.value.details == {
        "complete": False,
        "status": public_status,
        "retry_after_ms": 250,
    }

    recovered = commands.op_find(tmp_path, **request)
    hot = commands.op_find(tmp_path, **request)
    assert recovered["hits"] == []
    assert hot["hits"] == []
    assert hot["timings"]["cache"]["hit"] is True
    assert calls == 2


@pytest.mark.parametrize("result_level", ["page", "unit"])
def test_exact_catalog_cold_and_hot_timings_are_measurable(
    tmp_path: Path, result_level: str
) -> None:
    paths = [_write_note(tmp_path, "alpha"), _write_note(tmp_path, "beta")]
    _seed(tmp_path, paths)
    lexstore.ensure_fresh(tmp_path)
    find_module.reset_page_and_result_caches()

    request = {
        "query": "",
        "categories": ["constraint"],
        "result_level": result_level,
        "scope": "kb-only",
        "mode": "keyword",
        "graph": False,
        "rerank": False,
        "pack": False,
        "limit": 10,
        "include_timings": True,
    }
    cold = commands.op_find(tmp_path, **request)
    hot = commands.op_find(tmp_path, **request)

    assert len(cold["hits"]) == 2
    assert len(hot["hits"]) == 2
    cold_timing = cold["timings"]
    hot_timing = hot["timings"]
    assert "ms" in cold_timing["stages"]["filter_eligibility"]
    assert set(cold_timing["profile"]["catalog"]) == _PROFILE_KEYS
    assert cold_timing["profile"]["catalog"] == {
        "capability": "semantic_catalog",
        "backend": "metadata_only",
        "outcome": "available",
        "complete": True,
        "repair_state": "none",
        "retry_after_ms": None,
    }
    assert hot_timing["cache"]["hit"] is True
    assert hot_timing["stages"]["filter_eligibility"] == {
        "ms": 0.0,
        "cache_hit": True,
    }
    assert hot_timing["profile"]["catalog"] == {
        "capability": "semantic_catalog",
        "backend": "not_used",
        "outcome": "available",
        "complete": True,
        "repair_state": "not_applicable",
        "retry_after_ms": None,
    }


def test_planner_unsupported_uses_scan_oracle_without_catalog_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_note(tmp_path, "oracle")
    _seed(tmp_path, [path])

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("planner-unsupported recall touched the exact catalog")

    monkeypatch.setattr(lexstore, "search_semantic_parent_paths_result", forbidden)
    envelope = commands.op_find(
        tmp_path,
        query="",
        filters={
            "$or": [
                {"unit.category": {"$eq": "constraint"}},
                {"page.status": {"$eq": "active"}},
            ]
        },
        result_level="page",
        scope="kb-only",
        mode="keyword",
        graph=False,
        rerank=False,
        include_timings=True,
    )
    assert len(envelope["hits"]) == 1
    assert "catalog" not in envelope["timings"]["profile"]


@pytest.mark.parametrize("result_level", ["page", "unit"])
def test_exact_metadata_recall_never_probes_fts_after_catalog_is_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result_level: str
) -> None:
    path = _write_note(tmp_path, "metadata")
    _seed(tmp_path, [path])
    lexstore.ensure_fresh(tmp_path)
    find_module.reset_page_and_result_caches()

    monkeypatch.setattr(
        lexstore,
        "fts5_available",
        lambda: (_ for _ in ()).throw(AssertionError("FTS capability probed")),
    )
    envelope = commands.op_find(
        tmp_path,
        query="",
        categories=["constraint"],
        result_level=result_level,
        scope="kb-only",
        mode="keyword",
        graph=False,
        rerank=False,
        include_timings=True,
    )
    assert len(envelope["hits"]) == 1
    assert envelope["timings"]["profile"]["catalog"]["backend"] == "metadata_only"
