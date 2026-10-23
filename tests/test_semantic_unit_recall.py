from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from exomem import (
    commands,
    context_pack,
    embedding_index,
    embeddings,
    freshness,
    lexstore,
    semantic_index,
)
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


def _rich(
    *,
    kind: str,
    category: str,
    anchor: str,
    content: str,
    tags: str | None = None,
    context: str | None = None,
    relations: str | None = None,
) -> str:
    metadata = [f"- category: {category}", f"- id: {anchor}"]
    if tags is not None:
        metadata.append(f"- tags: {tags}")
    if context is not None:
        metadata.append(f"- context: {context}")
    if relations is not None:
        metadata.append(f"- relations: {relations}")
    metadata_text = "\n".join(metadata)
    return f"## {kind.title()}\n{metadata_text}\n\n{content}\n"


def _vector(first: float, second: float = 0.0) -> np.ndarray:
    value = np.zeros(embedding_index.VECTOR_DIM, dtype=np.float32)
    value[0] = first
    value[1] = second
    value /= np.linalg.norm(value)
    return value


def _prepare_semantic_catalog(root: Path, paths: list[Path]) -> None:
    entries = [(str(path), freshness.stat_signature(path)) for path in paths]
    freshness.seed(root, "kb", entries)
    freshness.seed(root, "vault", entries)
    lexstore.ensure_fresh(root)


@pytest.mark.parametrize("result_level", ["unit", "mixed"])
def test_managed_cold_and_warm_recall_keeps_unit_identity_and_bounds_parent_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result_level: str, warm_managed_cell,
) -> None:
    """Managed unit and mixed calls preserve identity and bound canonical parent reads."""
    from test_governance_egress import _reset_caches, write_rule, write_scope

    from exomem import bm25, memory_refs, semantic_units
    from exomem.governance import egress
    from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope

    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setenv("EXOMEM_FIND_CACHE_SIZE", "0")
    paths = [
        _write_page(tmp_path, f"harbor-{index:02d}", body=_rich(
            kind="decision", category="config", anchor="gauges",
            content=f"Harbor gauges follow the tide {index}.", tags="harbor", context="winter",
        )) for index in range(24)
    ]
    hidden = _write_page(tmp_path, "hidden-gauges", body="- [config] Harbor gauges stay sealed. ^sealed")
    paths.append(hidden)
    write_scope(tmp_path, paths="Notes/hidden-gauges.md", name="Restricted gauges")
    write_rule(tmp_path, ceiling=0)
    _reset_caches()
    warm_managed_cell(tmp_path)
    canonical = {
        unit.unit_ref: unit for path in paths
        for unit in semantic_index.build_parent_index_state(tmp_path, path).document.units
    }
    raw_get = find_module._CACHE.get
    statistics_of = bm25.CorpusStatistics.of.__func__
    reads: list[str] = []
    unit_builds = 0

    def counted_get(path, root):
        reads.append(path.relative_to(root).as_posix())
        return raw_get(path, root)

    def counted_statistics(cls, corpus):
        nonlocal unit_builds
        if corpus and all(ref.startswith(memory_refs.REF_PREFIX) for ref in corpus):
            unit_builds += 1
        return statistics_of(cls, corpus)

    def unexpected_parse(*_args, **_kwargs):
        raise AssertionError("unit selection reparsed canonical Markdown")

    monkeypatch.setattr(find_module._CACHE, "get", counted_get)
    monkeypatch.setattr(bm25.CorpusStatistics, "of", classmethod(counted_statistics))
    monkeypatch.setattr(semantic_units, "parse_semantic_units", unexpected_parse)
    for who in (owner_principal(), RequestPrincipal(audience_id="external", surface="mcp")):
        responses = []
        builds = []
        for _ in range(2):
            find_module.reset_page_and_result_caches()
            reads.clear()
            unit_builds = 0
            with request_scope(who):
                response = commands.op_find(
                    tmp_path, query="harbor gauges", mode="hybrid", scope="kb-only",
                    graph=False, result_level=result_level, limit=3,
                )
            hits = response["hits"] if isinstance(response, dict) else response
            assert hits
            parents = {hit.get("parent_path", hit.get("path")) for hit in hits}
            # The hit pool stays fixed when several hits share one parent.
            assert len(reads) <= egress.pool_limit(3)
            for hit in hits:
                if hit.get("result_type") == "semantic_unit":
                    expected = canonical[hit["unit_ref"]]
                    assert hit["content"] == expected.content
                    assert hit["source_span"] == find_module._unit_span(expected)
                    assert hit["source_hash"] == expected.source_hash
            responses.append(response)
            builds.append(unit_builds)
        assert responses[0] == responses[1]
        assert builds[1] == 0
        if who.audience_id == "external":
            assert builds[0] == 1
            assert hidden.relative_to(tmp_path).as_posix() not in parents


@pytest.mark.parametrize("mode", ["keyword", "hybrid"])
@pytest.mark.parametrize("drop_first", [False, True])
def test_managed_mixed_recall_bounds_reads_for_disjoint_page_and_unit_parents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warm_managed_cell, mode: str, drop_first: bool,
) -> None:
    """Older matching units must not add reads beyond the frozen mixed hit pool."""
    from test_governance_egress import _reset_caches, write_rule, write_scope

    from exomem.governance import egress
    from exomem.governance.principal import owner_principal, request_scope

    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setenv("EXOMEM_FIND_CACHE_SIZE", "0")
    pages = []
    for number in range(6):
        pages.append(_write_page(
            tmp_path, f"emberlamp-{number}", body="Routine maintenance stays documented.",
        ))
    older = _write_page(
        tmp_path, "older-decisions", updated="2026-06-01",
        body="\n".join(_rich(
            kind="decision", category="config", anchor=f"calibration-{number}",
            content=f"Emberlamp calibration keeps the reading stable {number}.",
        ) for number in range(3)),
    )
    write_scope(tmp_path)
    write_rule(tmp_path, ceiling=0)
    _reset_caches()
    warm_managed_cell(tmp_path)
    find_module.reset_page_and_result_caches()
    raw_get = find_module._CACHE.get
    reads: list[str] = []
    dropped: list[str] = []

    def counted_get(path, root):
        reads.append(path.relative_to(root).as_posix())
        if drop_first and not dropped and path in pages:
            path.unlink()
            dropped.append(path.relative_to(root).as_posix())
        return raw_get(path, root)

    monkeypatch.setattr(find_module._CACHE, "get", counted_get)
    with request_scope(owner_principal()):
        response = commands.op_find(
            tmp_path, query="emberlamp", mode=mode, scope="kb-only",
            graph=False, rerank=False, result_level="mixed", limit=3, explain=True,
        )
    hits = response["hits"] if isinstance(response, dict) else response
    assert any(
        hit.get("result_type") == "semantic_unit"
        and hit["parent_path"] == older.relative_to(tmp_path).as_posix()
        for hit in hits
    ), (response, reads)
    assert len(reads) <= egress.pool_limit(3), reads
    if mode == "keyword":
        # Three page slots and three units share four canonical parents. A
        # missing selected page must not bring a fourth page into those slots.
        assert set(reads) == {
            page.relative_to(tmp_path).as_posix() for page in pages[-3:] + [older]
        }
        ranks = {page.relative_to(tmp_path).as_posix(): rank
                 for rank, page in enumerate(reversed(pages), start=1)}
        for hit in hits:
            if hit.get("result_type") == "page":
                explanation = hit["ranking_explanation"]
                assert explanation["lanes"]["keyword"]["rank"] == ranks[hit["path"]]
                assert {"stage": "keyword", "rank": ranks[hit["path"]]} in explanation["ordering_path"]
    if drop_first:
        assert len(dropped) == 1
        assert all(hit.get("path") != dropped[0] for hit in hits)


@pytest.mark.parametrize("coverage", [
    None,
    {"format": 99, "body_length": 1, "owned": [], "withheld": []},
    {"format": 1, "body_length": 1, "owned": [[0, 1000]], "withheld": [[0, 1000]]},
])
def test_managed_unit_selection_declines_unknown_carrier_coverage_without_reading_bodies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, coverage,
) -> None:
    """Missing coverage cannot authorize a carrier as prose or trigger a candidate scan."""
    import json

    from exomem import ranking_config, readiness, structured_filters

    path = _write_page(tmp_path, "carrier-coverage", body="- [config] Harbor gauges stay bounded. ^gauges")
    _prepare_semantic_catalog(tmp_path, [path])
    rel = path.relative_to(tmp_path).as_posix()
    connection = lexstore.get_store(tmp_path)._connect()
    try:
        metadata = json.loads(connection.execute("SELECT structure_json FROM pages WHERE path = ?", (rel,)).fetchone()[0])
        if coverage is None:
            metadata.pop("prose_carriers")
        else:
            metadata["prose_carriers"] = coverage
        with connection:
            connection.execute("UPDATE pages SET structure_json = ? WHERE path = ?", (json.dumps(metadata), rel))
    finally:
        connection.close()

    def unexpected_read(*_args, **_kwargs):
        raise AssertionError("managed unit selection read a canonical candidate body")

    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
    monkeypatch.setattr(find_module._CACHE, "get", unexpected_read)
    with pytest.raises(find_module.RetrievalIndexWarming):
        find_module._find_semantic_units(
            tmp_path, query="", limit=3, scope="kb", mode="keyword",
            plan=structured_filters.compile_filter({"unit.category": {"$eq": "config"}}),
            snapshot=find_module.FreshnessSnapshot(tmp_path), prefer_active=False,
            config=ranking_config.DEFAULT_RANKING, degraded_out=[], failed_out=[],
            allowed_parent_paths={rel},
        )


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


@pytest.mark.parametrize("query", ["", "Visible"])
def test_origin_carriers_do_not_project_fake_or_overlapping_unit_fields(
    tmp_path: Path, query: str,
) -> None:
    path = _write_page(
        tmp_path,
        "origin-units",
        body=(
            "<!-- exomem-origin:v9\n"
            "## Observations\n- [config] private-attribution ^private-compact\n"
            "## Decision\n- category: config\n- id: private-rich\n"
            "- context: private-attribution\n- tags: private-attribution\n"
            "- confidential: private-attribution\n\nprivate-attribution\n-->\n\n"
            "## Observations\n- [config] Visible compact conclusion ^public-compact\n\n"
            "## Decision\n- category: config\n- id: overlapping-rich\n\n"
            "Visible opening.\n<!-- exomem-origin:v9 private-attribution -->\n\n"
            "## Decision\n- category: config\n- id: public-rich\n"
            "- context: Visible context\n- tags: visible\n\nVisible rich conclusion.\n"
        ),
    )
    _prepare_semantic_catalog(tmp_path, [path])
    canonical = semantic_index.current_parent_index_state(tmp_path, path)
    expected = {
        unit.unit_ref: unit
        for unit in canonical.document.units
        if unit.anchor in {"public-compact", "public-rich"}
    }
    original = path.read_bytes()

    result = commands.op_find(
        tmp_path, query=query, scope="kb-only", mode="keyword", graph=False,
        result_level="unit", pack=True,
    )

    assert {hit["unit_ref"] for hit in result["hits"]} == set(expected)
    for hit in result["hits"]:
        unit = expected[hit["unit_ref"]]
        assert hit["content"] == unit.content
        assert hit["source_hash"] == unit.source_hash
        assert hit["source_span"] == find_module._unit_span(unit)
    rel_path = path.relative_to(tmp_path).as_posix()
    packed = result["pack"]["semantic_units"][rel_path]["units"]
    assert {unit["unit_ref"] for unit in packed} == set(expected)
    assert all(unit["fingerprint"] == expected[unit["unit_ref"]].fingerprint for unit in packed)
    assert "private-attribution" not in str(result)
    page_result = commands.op_find(
        tmp_path, query=query, scope="kb-only", mode="keyword", graph=False,
        result_level="page", categories=["config"],
    )
    assert {unit["unit_ref"] for unit in page_result[0]["matched_units"]} == set(expected)
    assert path.read_bytes() == original


def test_raw_unit_capture_preserves_origin_and_literal_unit_uses_owning_span(tmp_path: Path) -> None:
    carrier = "<!-- exomem-origin:v9 literal-attribution -->"
    literal_path = _write_page(
        tmp_path, "origin-unit-literal",
        body="## Observations\n- [config] `" + carrier + "` ^literal",
    )
    raw_note = _write_page(
        tmp_path, "origin-unit-raw",
        body=_rich(kind="decision", category="config", anchor="raw", content=carrier),
    )
    raw_path = tmp_path / "Knowledge Base/Sources/origin-unit-raw.md"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_note.rename(raw_path)
    _prepare_semantic_catalog(tmp_path, [literal_path, raw_path])

    hits = find_module.find(
        tmp_path, query="", scope="kb-only", mode="keyword", graph=False, result_level="unit"
    )

    assert {hit.parent_path for hit in hits} == {
        literal_path.relative_to(tmp_path).as_posix(), raw_path.relative_to(tmp_path).as_posix()
    }
    assert all(carrier in hit.content and carrier in hit.excerpt for hit in hits)


def test_auto_unit_recall_intersects_text_category_kind_and_page_filter_axes(
    tmp_path: Path,
) -> None:
    fixtures = (
        ("config-decision", "decision", "config", "Use SQLite WAL mode"),
        ("rule-decision", "decision", "rule", "Use SQLite cache rules"),
        ("config-claim", "claim", "config", "SQLite is mentioned in a claim"),
        ("config-postgres", "decision", "config", "Use PostgreSQL instead"),
    )
    pages = [
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
        for name, kind, category, content in fixtures
    ]
    _prepare_semantic_catalog(tmp_path, pages)

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


def test_rich_tags_and_context_survive_filters_hits_reads_lexical_and_packs(
    tmp_path: Path,
) -> None:
    path = _write_page(
        tmp_path,
        "rich-projection",
        body=_rich(
            kind="decision",
            category="runtime reliability",
            anchor="rich-projection",
            content="Keep the retry window bounded",
            tags="Reliability, runtime/retry, reliability",
            context="Edge path",
        ),
    )

    hits = find_module.find(
        tmp_path,
        query="",
        scope="kb-only",
        mode="keyword",
        result_level="unit",
        filters={
            "$and": [
                {"unit.tags": {"$contains": "runtime/retry"}},
                {"unit.context": {"$eq": "Edge path"}},
            ]
        },
        limit=10,
    )

    assert len(hits) == 1
    hit = hits[0]
    payload = hit.as_dict()
    assert payload["tags"] == ["reliability", "runtime/retry"]
    assert payload["context"] == "Edge path"

    exact = commands.op_read_memory(
        tmp_path,
        path.relative_to(tmp_path).as_posix(),
        unit_ref=payload["unit_ref"],
    )
    assert exact["status"] == "found"
    assert exact["unit"]["tags"] == ["reliability", "runtime/retry"]
    assert exact["unit"]["context"] == "Edge path"

    lexical = lexstore.search_semantic_units(
        tmp_path,
        "retry window",
        k=10,
        scope="kb",
    )
    assert lexical is not None
    record = next(item for item in lexical if item.unit_ref == payload["unit_ref"])
    assert record.tags == ("reliability", "runtime/retry")
    assert record.context == "Edge path"

    pack = context_pack.assemble_pack(tmp_path, [hit])
    packed = pack["semantic_units"][payload["parent_path"]]["units"][0]
    assert packed["tags"] == ["reliability", "runtime/retry"]
    assert packed["context"] == "Edge path"


def test_empty_query_category_lookup_returns_independent_units(tmp_path: Path) -> None:
    page = _write_page(
        tmp_path,
        "filter-only",
        body="- [config] filter-only semantic unit ^filter-only",
    )
    _prepare_semantic_catalog(tmp_path, [page])

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


def test_unit_vector_lane_filters_exact_parents_after_bounded_ranking(
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

    observed: list[tuple[set[str] | None, bool]] = []
    original_search = index.search_semantic_units

    def observed_search(*args: Any, **kwargs: Any) -> Any:
        allowed = kwargs["allowed_unit_refs"]
        observed.append((set(allowed) if allowed is not None else None, kwargs["validate"]))
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

    assert observed == [(None, False)]
    payloads = [hit.as_dict() for hit in hits]
    assert [payload["source_anchor"] for payload in payloads] == [
        "active-vector",
        "second-vector",
    ]
    assert [payload["signals"]["vector_rank"] for payload in payloads] == [1, 2]
    assert all("bm25_rank" not in payload["signals"] for payload in payloads)


def test_unit_vector_category_filter_is_pushed_down_before_candidate_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = embeddings.get_embedding_index(tmp_path)
    pages: list[Path] = []
    for rank in range(20):
        page = _write_page(
            tmp_path,
            f"architecture-{rank:02d}",
            body=f"- [architecture] higher vector candidate {rank} ^architecture-{rank}",
        )
        pages.append(page)
        state = semantic_index.build_parent_index_state(tmp_path, page)
        index.upsert_semantic_units(
            state,
            np.stack([_vector(1.0, rank / 100.0)]),
            1.0,
        )
    target = _write_page(
        tmp_path,
        "reliability-target",
        body="- [reliability] exact category target ^reliability-target",
    )
    pages.append(target)
    target_state = semantic_index.build_parent_index_state(tmp_path, target)
    index.upsert_semantic_units(target_state, np.stack([_vector(0.1, 1.0)]), 1.0)
    _prepare_semantic_catalog(tmp_path, pages)
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
        scope="kb-only",
        mode="vector",
        graph=False,
        result_level="unit",
        categories=["reliability"],
        failed_out=failed,
        limit=1,
    )

    assert [hit.as_dict()["source_anchor"] for hit in hits] == ["reliability-target"]
    assert failed == []


def test_rich_unit_recall_preserves_typed_relations(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "rich-relations",
        body=_rich(
            kind="decision",
            category="architecture",
            anchor="bounded-sidecar",
            content="Use the bounded lexical sidecar for foreground recall.",
            relations="evidenced_by: [[Knowledge Base/Sources/Latency benchmark]]",
        ),
    )

    hits = find_module.find(
        tmp_path,
        query="bounded lexical sidecar",
        scope="kb-only",
        mode="keyword",
        graph=False,
        result_level="unit",
        limit=5,
    )

    assert hits[0].as_dict()["relations"] == [
        {
            "kind": "evidenced_by",
            "target": "[[Knowledge Base/Sources/Latency benchmark]]",
            "raw": "evidenced_by: [[Knowledge Base/Sources/Latency benchmark]]",
            "line": 6,
        }
    ]


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
    many_units = _write_page(
        tmp_path,
        "many-units",
        body="\n".join(
            f"- [config] shared mixed needle {index} ^many-{index}" for index in range(7)
        ),
    )
    one_unit = _write_page(
        tmp_path,
        "one-unit",
        body="- [config] shared mixed needle other ^one-unit",
    )
    _prepare_semantic_catalog(tmp_path, [many_units, one_unit])

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
