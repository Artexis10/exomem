from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from exomem import embedding_index, embeddings, semantic_index
from exomem import find as find_module


def _write_page(
    root: Path,
    name: str,
    *,
    body: str,
    status: str = "active",
    priority: int = 3,
    updated: str = "2026-07-15",
) -> Path:
    path = root / "Knowledge Base" / "Notes" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    page_id = uuid.uuid5(uuid.NAMESPACE_URL, f"exomem-test:{name}")
    path.write_text(
        "---\n"
        "type: insight\n"
        f"title: {name}\n"
        f"exomem_id: {page_id}\n"
        f"status: {status}\n"
        f"updated: {updated}\n"
        "metadata:\n"
        f"  priority: {priority}\n"
        "---\n\n"
        f"# {name}\n\n{body.rstrip()}\n",
        encoding="utf-8",
    )
    find_module.clear_cache()
    return path


def _rich(*, kind: str, category: str, anchor: str, content: str) -> str:
    return f"## {kind.title()}\n- category: {category}\n- id: {anchor}\n\n{content}\n"


def _vector(first: float, second: float = 0.0) -> np.ndarray:
    value = np.zeros(embedding_index.VECTOR_DIM, dtype=np.float32)
    value[0] = first
    value[1] = second
    value /= np.linalg.norm(value)
    return value


def test_default_auto_page_recall_is_byte_compatible_with_explicit_page(
    tmp_path: Path,
) -> None:
    _write_page(
        tmp_path,
        "default-page",
        body="default compatibility needle\n\n- [config] unit payload ^default",
    )

    default = find_module.find(
        tmp_path,
        query="default compatibility needle",
        scope="kb-only",
        mode="keyword",
        graph=False,
        limit=10,
    )
    explicit = find_module.find(
        tmp_path,
        query="default compatibility needle",
        scope="kb-only",
        mode="keyword",
        graph=False,
        result_level="page",
        limit=10,
    )

    assert [hit.as_dict() for hit in default] == [hit.as_dict() for hit in explicit]
    assert "result_type" not in default[0].as_dict()


def test_auto_unit_recall_intersects_text_category_kind_and_page_filter_axes(
    tmp_path: Path,
) -> None:
    fixtures = (
        ("config-decision", "decision", "config", "Use SQLite WAL mode"),
        ("rule-decision", "decision", "rule", "Use SQLite cache rules"),
        ("config-claim", "claim", "config", "SQLite is mentioned in a claim"),
        ("config-postgres", "decision", "config", "Use PostgreSQL instead"),
    )
    for name, kind, category, content in fixtures:
        _write_page(
            tmp_path,
            name,
            body=_rich(
                kind=kind,
                category=category,
                anchor=name,
                content=content,
            ),
        )

    hits = find_module.find(
        tmp_path,
        query="SQLite",
        scope="kb-only",
        mode="keyword",
        categories=["config", "rule"],
        kinds=["decision"],
        filters={"page.frontmatter:/metadata/priority": {"$eq": 3}},
        limit=20,
    )

    payloads = [hit.as_dict() for hit in hits]
    assert {payload["parent_path"] for payload in payloads} == {
        "Knowledge Base/Notes/config-decision.md",
        "Knowledge Base/Notes/rule-decision.md",
    }
    assert {payload["category"] for payload in payloads} == {"config", "rule"}
    assert {payload["kind"] for payload in payloads} == {"decision"}


def test_empty_query_category_lookup_returns_independent_units(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "filter-only",
        body="- [config] filter-only semantic unit ^filter-only",
    )

    hits = find_module.find(
        tmp_path,
        query="",
        scope="kb-only",
        categories=["config"],
        limit=10,
    )

    assert [hit.as_dict()["source_anchor"] for hit in hits] == ["filter-only"]
    assert hits[0].as_dict()["result_type"] == "semantic_unit"


def test_unit_keyword_mode_requires_every_literal_query_token(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "all-keywords",
        body="- [config] alpha and beta appear together ^all-keywords",
    )
    _write_page(
        tmp_path,
        "partial-keywords",
        body="- [config] alpha appears alone ^partial-keywords",
    )

    hits = find_module.find(
        tmp_path,
        query="alpha beta",
        scope="kb-only",
        mode="keyword",
        graph=False,
        result_level="unit",
        limit=10,
    )

    assert [hit.as_dict()["source_anchor"] for hit in hits] == ["all-keywords"]


def test_unit_vector_lane_filters_exact_refs_before_ranking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _write_page(
        tmp_path,
        "active-vector",
        body="- [config] alpha payload ^active-vector",
    )
    second = _write_page(
        tmp_path,
        "second-vector",
        body="- [config] beta payload ^second-vector",
    )
    draft = _write_page(
        tmp_path,
        "draft-vector",
        status="draft",
        body="- [config] strongest but ineligible ^draft-vector",
    )

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    index = embeddings.get_embedding_index(tmp_path)
    states = [
        semantic_index.build_parent_index_state(tmp_path, path) for path in (active, second, draft)
    ]
    index.upsert_semantic_units(states[0], np.stack([_vector(0.9, 0.1)]), 1.0)
    index.upsert_semantic_units(states[1], np.stack([_vector(0.7, 0.3)]), 1.0)
    index.upsert_semantic_units(states[2], np.stack([_vector(1.0)]), 1.0)
    monkeypatch.setattr(
        embeddings,
        "embed_texts",
        lambda _texts, *, is_query: np.stack([_vector(1.0)]),
    )

    observed: list[set[str]] = []
    original_search = index.search_semantic_units

    def observed_search(*args: Any, **kwargs: Any) -> Any:
        observed.append(set(kwargs["allowed_unit_refs"]))
        return original_search(*args, **kwargs)

    monkeypatch.setattr(index, "search_semantic_units", observed_search)
    hits = find_module.find(
        tmp_path,
        query="vector-only-no-literal",
        scope="kb-only",
        mode="vector",
        graph=False,
        result_level="unit",
        filters={"page.status": {"$eq": "active"}},
        limit=10,
    )

    expected_refs = {
        states[0].document.units[0].unit_ref,
        states[1].document.units[0].unit_ref,
    }
    assert observed == [expected_refs]
    payloads = [hit.as_dict() for hit in hits]
    assert [payload["source_anchor"] for payload in payloads] == [
        "active-vector",
        "second-vector",
    ]
    assert [payload["signals"]["vector_rank"] for payload in payloads] == [1, 2]
    assert all("bm25_rank" not in payload["signals"] for payload in payloads)


def test_unit_vector_lane_rejects_stale_rows_before_top_k(tmp_path: Path) -> None:
    stale = _write_page(
        tmp_path,
        "stale-vector",
        body="- [config] old indexed payload ^stable-anchor",
    )
    current = _write_page(
        tmp_path,
        "current-vector",
        body="- [config] current indexed payload ^current-anchor",
    )
    index = embeddings.get_embedding_index(tmp_path)
    stale_state = semantic_index.build_parent_index_state(tmp_path, stale)
    current_state = semantic_index.build_parent_index_state(tmp_path, current)
    index.upsert_semantic_units(stale_state, np.stack([_vector(1.0)]), 1.0)
    index.upsert_semantic_units(current_state, np.stack([_vector(0.8, 0.2)]), 1.0)

    _write_page(
        tmp_path,
        "stale-vector",
        body="- [config] changed unindexed payload ^stable-anchor",
    )
    allowed_refs = {
        stale_state.document.units[0].unit_ref,
        current_state.document.units[0].unit_ref,
    }

    hits = index.search_semantic_units(
        _vector(1.0),
        k=1,
        allowed_unit_refs={ref for ref in allowed_refs if ref is not None},
    )

    assert [hit.unit_ref for hit in hits] == [current_state.document.units[0].unit_ref]


def test_stale_unit_vector_coverage_marks_degradation_and_falls_back_to_lexical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = _write_page(
        tmp_path,
        "stale-fallback",
        body="- [config] old indexed payload ^stable-fallback",
    )
    unrelated = _write_page(
        tmp_path,
        "unrelated-vector",
        body="- [config] unrelated current vector ^unrelated-vector",
    )
    index = embeddings.get_embedding_index(tmp_path)
    stale_state = semantic_index.build_parent_index_state(tmp_path, stale)
    unrelated_state = semantic_index.build_parent_index_state(tmp_path, unrelated)
    index.upsert_semantic_units(stale_state, np.stack([_vector(1.0)]), 1.0)
    index.upsert_semantic_units(
        unrelated_state,
        np.stack([_vector(0.8, 0.2)]),
        1.0,
    )
    _write_page(
        tmp_path,
        "stale-fallback",
        body="- [config] current lexical fallback needle ^stable-fallback",
    )
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(
        embeddings,
        "embed_texts",
        lambda _texts, *, is_query: np.stack([_vector(1.0)]),
    )

    failed: list[str] = []
    hits = find_module.find(
        tmp_path,
        query="current lexical fallback needle",
        scope="kb-only",
        mode="vector",
        graph=False,
        result_level="unit",
        failed_out=failed,
        limit=1,
    )

    assert [hit.as_dict()["source_anchor"] for hit in hits] == ["stable-fallback"]
    assert failed == ["vector"]


def test_vault_unit_vector_coverage_respects_kb_scoped_embedding_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kb_page = _write_page(
        tmp_path,
        "kb-vector-scope",
        body="- [config] KB vector payload ^kb-vector-scope",
    )
    outside = tmp_path / "Reference" / "outside-vector-scope.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text(
        "---\n"
        "type: insight\n"
        "title: outside-vector-scope\n"
        f"exomem_id: {uuid.uuid5(uuid.NAMESPACE_URL, 'outside-vector-scope')}\n"
        "status: active\n"
        "updated: 2026-07-15\n"
        "---\n\n"
        "- [config] outside lexical payload ^outside-vector-scope\n",
        encoding="utf-8",
    )
    find_module.clear_cache()
    index = embeddings.get_embedding_index(tmp_path)
    kb_state = semantic_index.build_parent_index_state(tmp_path, kb_page)
    index.upsert_semantic_units(kb_state, np.stack([_vector(1.0)]), 1.0)
    monkeypatch.delenv("EXOMEM_INDEX_SCOPE", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(
        embeddings,
        "embed_texts",
        lambda _texts, *, is_query: np.stack([_vector(1.0)]),
    )

    failed: list[str] = []
    hits = find_module.find(
        tmp_path,
        query="vector-only-no-literal",
        scope="vault",
        mode="vector",
        graph=False,
        result_level="unit",
        failed_out=failed,
        limit=10,
    )

    assert [hit.as_dict()["source_anchor"] for hit in hits] == ["kb-vector-scope"]
    assert failed == []


def test_unit_vector_failure_soft_falls_back_to_lexical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_page(
        tmp_path,
        "vector-fallback",
        body="- [config] lexical fallback needle ^vector-fallback",
    )
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(
        embeddings,
        "embed_texts",
        lambda _texts, *, is_query: np.stack([_vector(1.0)]),
    )

    class FailingIndex:
        def search_semantic_units(self, *_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("unit vector backend failed")

    monkeypatch.setattr(embeddings, "get_embedding_index", lambda _root: FailingIndex())
    failed: list[str] = []
    hits = find_module.find(
        tmp_path,
        query="lexical fallback needle",
        scope="kb-only",
        mode="vector",
        graph=False,
        result_level="unit",
        failed_out=failed,
        limit=10,
    )

    assert [hit.as_dict()["source_anchor"] for hit in hits] == ["vector-fallback"]
    assert failed == ["vector"]


def test_embeddings_disabled_unit_recall_never_loads_vector_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_page(
        tmp_path,
        "embeddings-off",
        body="- [config] deterministic lexical unit ^embeddings-off",
    )

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("disabled unit recall must not load embeddings")

    monkeypatch.setattr(embeddings, "get_embedding_index", forbidden)
    monkeypatch.setattr(embeddings, "embed_texts", forbidden)
    hits = find_module.find(
        tmp_path,
        query="deterministic lexical unit",
        scope="kb-only",
        mode="hybrid",
        graph=False,
        result_level="unit",
        limit=10,
    )

    assert [hit.as_dict()["source_anchor"] for hit in hits] == ["embeddings-off"]


def test_mixed_recall_caps_units_per_parent_and_reports_truncation(
    tmp_path: Path,
) -> None:
    _write_page(
        tmp_path,
        "many-units",
        body="\n".join(
            f"- [config] shared mixed needle {index} ^many-{index}" for index in range(7)
        ),
    )
    _write_page(
        tmp_path,
        "one-unit",
        body="- [config] shared mixed needle other ^one-unit",
    )

    hits = find_module.find(
        tmp_path,
        query="shared mixed needle",
        scope="kb-only",
        mode="keyword",
        graph=False,
        result_level="mixed",
        categories=["config"],
        limit=20,
    )
    payloads = [hit.as_dict() for hit in hits]

    page = next(
        payload
        for payload in payloads
        if payload.get("result_type") == "page"
        and payload["path"] == "Knowledge Base/Notes/many-units.md"
    )
    repeated_units = [
        payload
        for payload in payloads
        if payload.get("result_type") == "semantic_unit"
        and payload["parent_path"] == "Knowledge Base/Notes/many-units.md"
    ]
    assert len(repeated_units) == 3
    assert page["mixed_units_truncated"] == 4
    assert any(
        payload.get("result_type") == "page"
        and payload["path"] == "Knowledge Base/Notes/one-unit.md"
        for payload in payloads
    )
    assert any(
        payload.get("result_type") == "semantic_unit"
        and payload["parent_path"] == "Knowledge Base/Notes/one-unit.md"
        for payload in payloads
    )
