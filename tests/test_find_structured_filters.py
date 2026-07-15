from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from exomem import embeddings
from exomem import find as find_module
from exomem.structured_filters import FilterError, compile_filter


def _write_page(
    vault: Path,
    name: str,
    *,
    status: str,
    updated: str,
    priority: int,
    observations: str,
    projects: str = "[alpha]",
) -> Path:
    path = vault / "Knowledge Base" / "Notes" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "type: insight\n"
        f"status: {status}\n"
        f"updated: {updated}\n"
        f"projects: {projects}\n"
        "tags: [auth]\n"
        "metadata:\n"
        f"  priority: {priority}\n"
        "---\n\n"
        f"# {name}\n\nstructured-filter-marker\n\n{observations}\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def filter_vault(vault: Path) -> Path:
    _write_page(
        vault,
        "matching",
        status="active",
        updated="2026-01-03",
        priority=3,
        observations="- [config] Matching unit #auth ^matching",
    )
    _write_page(
        vault,
        "split-units",
        status="active",
        updated="2026-01-02",
        priority=3,
        observations=(
            "- [config] Category on one unit #sqlite ^category\n"
            "- [rule] Tag on another unit #auth ^tag"
        ),
    )
    _write_page(
        vault,
        "draft",
        status="draft",
        updated="2026-01-04",
        priority=4,
        observations="- [config] Draft unit #auth ^draft",
    )
    find_module.clear_cache()
    return vault


@pytest.mark.parametrize("mode", ["keyword", "hybrid", "vector"])
def test_every_lane_consumes_the_same_page_filter_eligibility(
    filter_vault: Path, mode: str
) -> None:
    hits = find_module.find(
        filter_vault,
        query="structured-filter-marker",
        scope="kb-only",
        mode=mode,
        graph=True,
        filters={
            "page.status": {"$eq": "active"},
            "page.frontmatter:/metadata/priority": {"$between": [3, 3]},
        },
        limit=20,
    )
    paths = {hit.path for hit in hits}
    assert paths == {
        "Knowledge Base/Notes/matching.md",
        "Knowledge Base/Notes/split-units.md",
    }


def test_unit_predicates_are_grouped_against_one_child_for_page_results(
    filter_vault: Path,
) -> None:
    hits = find_module.find(
        filter_vault,
        query="",
        scope="kb-only",
        mode="keyword",
        result_level="page",
        filters={
            "$and": [
                {"page.status": {"$eq": "active"}},
                {"unit.category": {"$eq": "config"}},
                {"unit.tags": {"$contains": "auth"}},
            ]
        },
        limit=20,
    )
    assert [hit.path for hit in hits] == ["Knowledge Base/Notes/matching.md"]


def test_generic_filter_and_shortcuts_intersect_in_filter_only_order(
    filter_vault: Path,
) -> None:
    hits = find_module.find(
        filter_vault,
        query="",
        scope="kb-only",
        result_level="page",
        types=["insight"],
        projects=["alpha"],
        tags=["AUTH"],
        categories=["config"],
        filters={"page.status": {"$eq": "active"}},
        limit=20,
    )
    assert [hit.path for hit in hits] == [
        "Knowledge Base/Notes/matching.md",
        "Knowledge Base/Notes/split-units.md",
    ]


def test_category_alias_is_resolved_before_candidate_work(filter_vault: Path) -> None:
    registry = (
        filter_vault
        / "Knowledge Base"
        / "_Schema"
        / "semantic-language-registry.yaml"
    )
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        "schema_version: 1\n"
        "categories:\n"
        "  config:\n"
        "    description: Configuration facts\n"
        "    aliases: [configuration]\n"
        "kinds: {}\n",
        encoding="utf-8",
    )
    find_module.clear_cache()
    hits = find_module.find(
        filter_vault,
        query="",
        scope="kb-only",
        result_level="page",
        categories=["configuration"],
        filters={"page.status": {"$eq": "active"}},
        limit=20,
    )
    assert {hit.path for hit in hits} == {
        "Knowledge Base/Notes/matching.md",
        "Knowledge Base/Notes/split-units.md",
    }


def test_hot_cache_tracks_registry_changes_for_unit_filters(
    filter_vault: Path,
) -> None:
    _write_page(
        filter_vault,
        "registry-cache-target",
        status="active",
        updated="2026-01-05",
        priority=99,
        observations="- [configuration] Registry-sensitive unit ^registry-cache",
    )
    registry = (
        filter_vault
        / "Knowledge Base"
        / "_Schema"
        / "semantic-language-registry.yaml"
    )

    def write_registry(*, configuration_target: str) -> None:
        other = "rule" if configuration_target == "config" else "config"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(
            "schema_version: 1\n"
            "categories:\n"
            f"  {configuration_target}:\n"
            f"    description: {configuration_target} facts\n"
            "    aliases: [configuration]\n"
            f"  {other}:\n"
            f"    description: {other} facts\n"
            "kinds: {}\n",
            encoding="utf-8",
        )

    write_registry(configuration_target="config")
    find_module.clear_cache()
    first = find_module.find(
        filter_vault,
        query="",
        scope="kb-only",
        result_level="page",
        categories=["config"],
        filters={"page.frontmatter:/metadata/priority": {"$eq": 99}},
        limit=20,
    )
    assert [hit.path for hit in first] == [
        "Knowledge Base/Notes/registry-cache-target.md"
    ]

    # Same request, no explicit cache clear: registry content is answer
    # freshness whenever unit predicates participate in eligibility.
    write_registry(configuration_target="rule")
    second = find_module.find(
        filter_vault,
        query="",
        scope="kb-only",
        result_level="page",
        categories=["config"],
        filters={"page.frontmatter:/metadata/priority": {"$eq": 99}},
        limit=20,
    )
    assert second == []


def test_unit_filter_auto_returns_independently_citable_units(
    filter_vault: Path,
) -> None:
    hits = find_module.find(
        filter_vault,
        query="",
        scope="kb-only",
        mode="keyword",
        filters={
            "$and": [
                {"page.status": {"$eq": "active"}},
                {"unit.category": {"$eq": "config"}},
            ]
        },
        limit=20,
    )

    payloads = [hit.as_dict() for hit in hits]
    assert [payload["parent_path"] for payload in payloads] == [
        "Knowledge Base/Notes/matching.md",
        "Knowledge Base/Notes/split-units.md",
    ]
    assert all(payload["result_type"] == "semantic_unit" for payload in payloads)
    assert all(payload["unit_ref"] for payload in payloads)
    assert all(payload["source_anchor"] for payload in payloads)
    assert all("text" not in payload["source_span"] for payload in payloads)
    assert [payload["category"] for payload in payloads] == ["config", "config"]


def test_explicit_page_mode_annotates_the_same_matching_unit(
    filter_vault: Path,
) -> None:
    hits = find_module.find(
        filter_vault,
        query="",
        scope="kb-only",
        mode="keyword",
        result_level="page",
        filters={
            "$and": [
                {"page.status": {"$eq": "active"}},
                {"unit.category": {"$eq": "config"}},
                {"unit.tags": {"$contains": "auth"}},
            ]
        },
        limit=20,
    )

    assert [hit.path for hit in hits] == ["Knowledge Base/Notes/matching.md"]
    payload = hits[0].as_dict()
    assert len(payload["matched_units"]) == 1
    assert payload["matched_units"][0]["category"] == "config"
    assert payload["matched_units"][0]["anchor"] == "matching"
    assert "text" not in payload["matched_units"][0]["span"]


def test_python_lexical_backend_preserves_unit_filter_eligibility(
    filter_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    hits = find_module.find(
        filter_vault,
        query="Category",
        scope="kb-only",
        result_level="unit",
        categories=["config"],
        filters={"page.status": {"$eq": "active"}},
        limit=20,
    )

    assert [hit.as_dict()["parent_path"] for hit in hits] == [
        "Knowledge Base/Notes/split-units.md"
    ]


def test_page_matched_units_are_bounded_and_report_truncation(
    filter_vault: Path,
) -> None:
    _write_page(
        filter_vault,
        "many-units",
        status="active",
        updated="2026-01-06",
        priority=77,
        observations="\n".join(
            f"- [config] Matching unit {index} #auth ^cap-{index}"
            for index in range(7)
        ),
    )
    hits = find_module.find(
        filter_vault,
        query="",
        scope="kb-only",
        result_level="page",
        filters={
            "$and": [
                {"page.frontmatter:/metadata/priority": {"$eq": 77}},
                {"unit.category": {"$eq": "config"}},
            ]
        },
        limit=20,
    )

    payload = hits[0].as_dict()
    assert len(payload["matched_units"]) == 5
    assert payload["matched_units_truncated"] == 2


def test_category_word_in_content_cannot_spoof_exact_unit_metadata(
    filter_vault: Path,
) -> None:
    _write_page(
        filter_vault,
        "category-spoof",
        status="active",
        updated="2026-01-06",
        priority=88,
        observations="- [decision] This mentions requirement repeatedly ^spoof",
    )
    hits = find_module.find(
        filter_vault,
        query="requirement",
        scope="kb-only",
        categories=["requirement"],
        filters={"page.frontmatter:/metadata/priority": {"$eq": 88}},
        limit=20,
    )
    assert hits == []


def test_identical_unit_content_keeps_distinct_category_identities(
    filter_vault: Path,
) -> None:
    _write_page(
        filter_vault,
        "same-content",
        status="active",
        updated="2026-01-06",
        priority=89,
        observations=(
            "- [config] Identical semantic payload ^same-config\n"
            "- [rule] Identical semantic payload ^same-rule"
        ),
    )
    hits = find_module.find(
        filter_vault,
        query="Identical semantic payload",
        scope="kb-only",
        categories=["config", "rule"],
        filters={"page.frontmatter:/metadata/priority": {"$eq": 89}},
        limit=20,
    )

    payloads = [hit.as_dict() for hit in hits]
    assert [payload["category"] for payload in payloads] == ["config", "rule"]
    assert len({payload["unit_ref"] for payload in payloads}) == 2


def test_text_unit_recall_falls_back_when_registry_makes_fts_rows_stale(
    filter_vault: Path,
) -> None:
    _write_page(
        filter_vault,
        "registry-text-freshness",
        status="active",
        updated="2026-01-06",
        priority=90,
        observations="- [configuration] registry needle ^registry-text",
    )
    registry = (
        filter_vault
        / "Knowledge Base"
        / "_Schema"
        / "semantic-language-registry.yaml"
    )

    def write_registry(description: str) -> None:
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(
            "schema_version: 1\n"
            "categories:\n"
            "  config:\n"
            f"    description: {description}\n"
            "    aliases: [configuration]\n"
            "kinds: {}\n",
            encoding="utf-8",
        )

    write_registry("Initial configuration facts")
    first = find_module.find(
        filter_vault,
        query="registry needle",
        scope="kb-only",
        result_level="unit",
        filters={"page.frontmatter:/metadata/priority": {"$eq": 90}},
        limit=20,
    )
    assert len(first) == 1

    write_registry("Revised configuration facts")
    second = find_module.find(
        filter_vault,
        query="registry needle",
        scope="kb-only",
        result_level="unit",
        filters={"page.frontmatter:/metadata/priority": {"$eq": 90}},
        limit=20,
    )
    assert [hit.as_dict()["unit_ref"] for hit in second] == [
        first[0].as_dict()["unit_ref"]
    ]


def test_python_unit_ranking_breaks_zero_score_ties_toward_active_parent(
    filter_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    _write_page(
        filter_vault,
        "a-superseded",
        status="superseded",
        updated="2026-01-06",
        priority=91,
        observations="- [config] needle ^superseded-needle",
    )
    _write_page(
        filter_vault,
        "z-active",
        status="active",
        updated="2026-01-06",
        priority=91,
        observations="- [config] needle ^active-needle",
    )
    for index in range(2):
        _write_page(
            filter_vault,
            f"filler-{index}",
            status="active",
            updated="2026-01-06",
            priority=91,
            observations=f"- [config] unrelated-{index} ^filler-{index}",
        )

    active_first = find_module.find(
        filter_vault,
        query="needle",
        scope="kb-only",
        result_level="unit",
        filters={"page.frontmatter:/metadata/priority": {"$eq": 91}},
        prefer_active=True,
        limit=20,
    )
    assert [hit.as_dict()["parent_path"] for hit in active_first] == [
        "Knowledge Base/Notes/z-active.md",
        "Knowledge Base/Notes/a-superseded.md",
    ]

    path_order = find_module.find(
        filter_vault,
        query="needle",
        scope="kb-only",
        result_level="unit",
        filters={"page.frontmatter:/metadata/priority": {"$eq": 91}},
        prefer_active=False,
        limit=20,
    )
    assert path_order[0].as_dict()["parent_path"] == (
        "Knowledge Base/Notes/a-superseded.md"
    )


def test_invalid_filter_fails_before_candidate_search(
    filter_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    searched = False

    def _candidate_search(*_args: object, **_kwargs: object) -> list[object]:
        nonlocal searched
        searched = True
        return []

    monkeypatch.setattr(find_module, "_find_keyword", _candidate_search)
    with pytest.raises(FilterError) as caught:
        find_module.find(
            filter_vault,
            query="",
            filters={"unit.categry": {"$eq": "config"}},
        )
    assert caught.value.code == "INVALID_FILTER_FIELD"
    assert not searched


def test_real_vector_lane_ranks_only_filter_eligible_parents(
    filter_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    index = embeddings.get_embedding_index(filter_vault)

    def vector(first: float, second: float = 0.0) -> np.ndarray:
        value = np.zeros(embeddings.VECTOR_DIM, dtype=np.float32)
        value[0] = first
        value[1] = second
        value /= np.linalg.norm(value)
        return value

    index.upsert_file(
        "Knowledge Base/Notes/draft.md", ["draft"], np.stack([vector(1.0)]), 1.0
    )
    index.upsert_file(
        "Knowledge Base/Notes/matching.md",
        ["matching"],
        np.stack([vector(0.9, 0.1)]),
        1.0,
    )
    index.upsert_file(
        "Knowledge Base/Notes/split-units.md",
        ["split"],
        np.stack([vector(0.8, 0.2)]),
        1.0,
    )
    monkeypatch.setattr(
        embeddings,
        "embed_texts",
        lambda _texts, *, is_query: np.stack([vector(1.0)]),
    )

    hits = find_module.find(
        filter_vault,
        query="no-literal-vector-query",
        scope="kb-only",
        mode="vector",
        graph=False,
        filters={
            "page.status": {"$eq": "active"},
            "page.frontmatter:/metadata/priority": {"$eq": 3},
        },
        limit=10,
    )

    assert [hit.path for hit in hits] == [
        "Knowledge Base/Notes/matching.md",
        "Knowledge Base/Notes/split-units.md",
    ]


def test_scene_frame_candidate_inherits_its_emitted_parent_eligibility(
    filter_vault: Path,
) -> None:
    parent = filter_vault / "Knowledge Base" / "Evidence" / "video.mp4.md"
    child = (
        filter_vault
        / "Knowledge Base"
        / "Evidence"
        / "video.mp4.frames"
        / "frame.jpg.md"
    )
    parent.parent.mkdir(parents=True, exist_ok=True)
    child.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text(
        "---\ntype: source\nstatus: active\nmedia_type: video\n---\n# Video\n",
        encoding="utf-8",
    )
    child.write_text(
        "---\ntype: source\nparent_media: Knowledge Base/Evidence/video.mp4\n"
        "media_type: image\n---\n# Frame\n",
        encoding="utf-8",
    )
    find_module.clear_cache()

    eligible = find_module._eligible_filter_paths(
        filter_vault,
        scope="kb",
        plan=compile_filter({"page.status": {"$eq": "active"}}),
    )

    assert "Knowledge Base/Evidence/video.mp4.md" in eligible
    assert "Knowledge Base/Evidence/video.mp4.frames/frame.jpg.md" in eligible


def test_auto_widen_pushes_exact_outside_eligibility_into_bm25(
    filter_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = filter_vault / "Projects" / "active.md"
    draft = filter_vault / "Projects" / "draft.md"
    active.parent.mkdir(parents=True, exist_ok=True)
    active.write_text(
        "---\ntype: insight\nstatus: active\n---\n# Active\n\noutside-marker\n",
        encoding="utf-8",
    )
    draft.write_text(
        "---\ntype: insight\nstatus: draft\n---\n# Draft\n\noutside-marker\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_bm25_search(
        _root: Path,
        _query: str,
        k: int,
        **kwargs: object,
    ) -> list[tuple[str, float]]:
        captured["k"] = k
        captured["allowed_paths"] = kwargs.get("allowed_paths")
        return [("Projects/active.md", 1.0)]

    from exomem import bm25

    monkeypatch.setattr(bm25, "search", fake_bm25_search)
    hits = find_module._find_outside_kb(
        filter_vault,
        query="outside-marker",
        query_norm="outside-marker",
        types=None,
        projects=None,
        tags=None,
        limit=5,
        filter_plan=compile_filter({"page.status": {"$eq": "active"}}),
    )

    assert captured["allowed_paths"] == {"Projects/active.md"}
    assert captured["k"] == 5
    assert [hit.path for hit in hits] == ["Projects/active.md"]
