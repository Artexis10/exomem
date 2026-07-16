from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml

from exomem import bm25, embeddings, lexstore, readiness, semantic_language_registry
from exomem import find as find_module
from exomem.structured_filters import FilterError

PROBE_METADATA: dict[str, dict[str, Any]] = {
    "equal": {
        "conformance": True,
        "probe": "café",
        "nested": {"0": {"leaf": "yes"}},
    },
    "other": {"conformance": True, "probe": "other"},
    "array": {
        "conformance": True,
        "probe": ["café", "βeta"],
        "nested": [{"leaf": "yes"}],
    },
    "null": {"conformance": True, "probe": None},
    "missing": {"conformance": True, "unrelated": "value"},
    "mapping": {"conformance": True, "probe": {"nested": "café"}},
    "date-string": {"conformance": True, "probe": "2026-01-02"},
    "number-string": {"conformance": True, "probe": "7"},
    "number": {"conformance": True, "probe": 7},
    "boolean": {"conformance": True, "probe": True},
}


FILTER_CASES: tuple[tuple[str, dict[str, Any], set[str]], ...] = (
    (
        "exact string equality",
        {"page.frontmatter:/metadata/probe": {"$eq": "café"}},
        {"equal"},
    ),
    (
        "exact string inequality",
        {"page.frontmatter:/metadata/probe": {"$ne": "café"}},
        {"other", "date-string", "number-string"},
    ),
    (
        "scalar or array membership",
        {"page.frontmatter:/metadata/probe": {"$in": ["café"]}},
        {"equal", "array"},
    ),
    (
        "string substring or array membership",
        {"page.frontmatter:/metadata/probe": {"$contains": "café"}},
        {"equal", "array"},
    ),
    (
        "terminal array all",
        {"page.frontmatter:/metadata/probe": {"$all": ["βeta", "café"]}},
        {"array"},
    ),
    (
        "explicit null",
        {"page.frontmatter:/metadata/probe": {"$eq": None}},
        {"null"},
    ),
    (
        "missing field",
        {"page.frontmatter:/metadata/probe": {"$exists": False}},
        {"missing"},
    ),
    (
        "numeric mapping key but not array traversal",
        {"page.frontmatter:/metadata/nested/0/leaf": {"$eq": "yes"}},
        {"equal"},
    ),
    (
        "date-looking string remains a string",
        {"page.frontmatter:/metadata/probe": {"$eq": "2026-01-02"}},
        {"date-string"},
    ),
    (
        "number-looking string remains a string",
        {"page.frontmatter:/metadata/probe": {"$eq": "7"}},
        {"number-string"},
    ),
    (
        "number does not equal boolean",
        {"page.frontmatter:/metadata/probe": {"$eq": 7}},
        {"number"},
    ),
    (
        "boolean does not equal number",
        {"page.frontmatter:/metadata/probe": {"$eq": True}},
        {"boolean"},
    ),
)


def _rel(name: str) -> str:
    return f"Knowledge Base/Notes/{name}.md"


def _conformance_filter(filters: dict[str, Any]) -> dict[str, Any]:
    return {
        "$and": [
            {
                "page.frontmatter:/metadata/conformance": {
                    "$eq": True,
                }
            },
            filters,
        ]
    }


def _write_probe_page(vault: Path, name: str, metadata: dict[str, Any]) -> None:
    path = vault / _rel(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = yaml.safe_dump(
        {
            "type": "insight",
            "status": "active",
            "updated": "2026-01-02",
            "projects": ["alpha"],
            "tags": ["auth"],
            "metadata": metadata,
        },
        allow_unicode=True,
        sort_keys=False,
    )
    graph_seed = (
        "graph-seed-only\n\n[[Knowledge Base/Notes/mapping]]\n\n" if name == "equal" else ""
    )
    path.write_text(
        f"---\n{frontmatter}---\n\n# {name}\n\n"
        f"parity-needle\n\n{graph_seed}"
        f"- [config] Unit for {name} #auth ^{name}\n",
        encoding="utf-8",
    )


@pytest.fixture
def conformance_vault(vault: Path) -> Path:
    for name, metadata in PROBE_METADATA.items():
        _write_probe_page(vault, name, metadata)
    find_module.clear_cache()
    return vault


def _assert_filter_cases(
    vault: Path,
    *,
    mode: str,
    graph: bool,
) -> None:
    for label, filters, expected_names in FILTER_CASES:
        hits = find_module.find(
            vault,
            query="parity-needle",
            scope="kb-only",
            mode=mode,
            graph=graph,
            rerank=False,
            temporal=False,
            filters=_conformance_filter(filters),
            limit=20,
        )
        assert {hit.path for hit in hits} == {_rel(name) for name in expected_names}, label


@pytest.mark.parametrize(
    ("mode", "graph", "lexical_backend"),
    [
        ("keyword", False, "python"),
        ("hybrid", False, "python"),
        ("hybrid", False, "fts5"),
        ("hybrid", True, "fts5"),
    ],
    ids=("keyword", "bm25-python-hybrid", "sqlite-fts5-hybrid", "graph-enriched"),
)
def test_heterogeneous_filter_eligibility_is_identical_across_lexical_lanes(
    conformance_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    graph: bool,
    lexical_backend: str,
) -> None:
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", lexical_backend)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    observed_eligible: list[set[str]] = []
    observed_lane_rankings: list[tuple[set[str], ...]] = []
    observed_bm25: list[tuple[set[str], set[str]]] = []

    if mode == "keyword":
        original_keyword = find_module._find_keyword

        def observed_keyword(*args: Any, **kwargs: Any) -> Any:
            observed_eligible.append(set(kwargs["eligible_paths"]))
            return original_keyword(*args, **kwargs)

        monkeypatch.setattr(find_module, "_find_keyword", observed_keyword)
    else:
        original_bm25 = bm25.search

        def observed_bm25_search(*args: Any, **kwargs: Any) -> Any:
            allowed_paths = set(kwargs["allowed_paths"])
            result = original_bm25(*args, **kwargs)
            observed_bm25.append((allowed_paths, {path for path, _score in result}))
            return result

        monkeypatch.setattr(bm25, "search", observed_bm25_search)
        original_collect = find_module.find_candidates.collect_candidates

        def observed_collect(*args: Any, **kwargs: Any) -> Any:
            observed_eligible.append(set(kwargs["eligible_paths"]))
            bundle = original_collect(*args, **kwargs)
            observed_lane_rankings.append(
                tuple(
                    set(ranking)
                    for ranking in (
                        bundle.bm25_ranking,
                        bundle.keyword_ranking,
                        bundle.vector_ranking,
                        bundle.clip_ranking,
                        bundle.graph_ranking,
                    )
                )
            )
            return bundle

        monkeypatch.setattr(
            find_module.find_candidates,
            "collect_candidates",
            observed_collect,
        )

    _assert_filter_cases(conformance_vault, mode=mode, graph=graph)
    expected_sets = [
        {_rel(name) for name in expected_names} for _label, _filters, expected_names in FILTER_CASES
    ]
    assert observed_eligible == expected_sets
    if mode != "keyword":
        assert observed_bm25 == [(expected, expected) for expected in expected_sets]
        assert len(observed_lane_rankings) == len(expected_sets)
        for expected, rankings in zip(
            expected_sets,
            observed_lane_rankings,
            strict=True,
        ):
            bm25_ranking, keyword_ranking, *optional_rankings = rankings
            assert bm25_ranking == expected
            assert keyword_ranking == expected
            assert all(ranking <= expected for ranking in optional_rankings)
    if lexical_backend == "fts5":
        assert lexstore.cache_token(conformance_vault) == "fts5"


def _vector(first: float, second: float = 0.0) -> np.ndarray:
    value = np.zeros(embeddings.VECTOR_DIM, dtype=np.float32)
    value[0] = first
    value[1] = second
    value /= np.linalg.norm(value)
    return value


@pytest.mark.parametrize("requested_backend", ["numpy", "sqlite-vec"])
def test_heterogeneous_filter_eligibility_is_identical_for_vector_backends(
    conformance_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    requested_backend: str,
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "numpy")
    index = embeddings.get_embedding_index(conformance_vault)
    for position, name in enumerate(PROBE_METADATA):
        index.upsert_file(
            _rel(name),
            [f"vector content {name}"],
            np.stack([_vector(1.0, position / 100.0)]),
            float(position + 1),
        )

    # Filtered search has a proven exact numpy implementation even when the
    # optional sqlite-vec backend is requested. It must not run an unfiltered
    # vec0 query and post-hoc trim a bounded top-k result.
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", requested_backend)
    observed_searches: list[tuple[set[str], set[str]]] = []
    original_search = index.search

    def observed_search(
        query_vector: np.ndarray,
        *,
        k: int,
        allowed_paths: set[str] | None = None,
    ) -> list[tuple[str, int, str, float]]:
        assert allowed_paths is not None
        result = original_search(
            query_vector,
            k=k,
            allowed_paths=allowed_paths,
        )
        observed_searches.append(
            (set(allowed_paths), {path for path, _index, _text, _score in result})
        )
        return result

    monkeypatch.setattr(index, "search", observed_search)

    def forbidden_unfiltered_vec_query(*_args: object, **_kwargs: object) -> None:
        pytest.fail("filtered vector search must not call unfiltered vec0 KNN")

    monkeypatch.setattr(index, "_vec_search", forbidden_unfiltered_vec_query)
    monkeypatch.setattr(
        embeddings,
        "embed_texts",
        lambda _texts, *, is_query: np.stack([_vector(1.0)]),
    )
    find_module.clear_cache()

    for label, filters, expected_names in FILTER_CASES:
        hits = find_module.find(
            conformance_vault,
            query="vector-only-no-literal",
            scope="kb-only",
            mode="vector",
            graph=False,
            rerank=False,
            temporal=False,
            filters=_conformance_filter(filters),
            limit=20,
        )
        assert {hit.path for hit in hits} == {_rel(name) for name in expected_names}, label

    expected_sets = [
        {_rel(name) for name in expected_names} for _label, _filters, expected_names in FILTER_CASES
    ]
    assert observed_searches == [(expected, expected) for expected in expected_sets]


def test_graph_neighbor_cannot_escape_the_shared_eligible_identity_set(
    conformance_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    positive = find_module.find(
        conformance_vault,
        query="graph-seed-only",
        scope="kb-only",
        mode="hybrid",
        graph=True,
        rerank=False,
        temporal=False,
        filters={"page.frontmatter:/metadata/probe": {"$exists": True}},
        limit=20,
    )
    positive_by_path = {hit.path: hit for hit in positive}
    assert positive_by_path[_rel("mapping")].graph_hop is True

    hits = find_module.find(
        conformance_vault,
        query="graph-seed-only",
        scope="kb-only",
        mode="hybrid",
        graph=True,
        rerank=False,
        temporal=False,
        filters={"page.frontmatter:/metadata/probe": {"$eq": "café"}},
        limit=20,
    )
    assert [hit.path for hit in hits] == [_rel("equal")]


def test_optional_backend_failure_degrades_without_broadening_eligibility(
    conformance_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)

    class RefusingIndex:
        def search(
            self,
            _query_vec: np.ndarray,
            *,
            k: int,
            allowed_paths: set[str] | None = None,
        ) -> list[tuple[str, int, str, float]]:
            assert k > 0
            assert allowed_paths == {_rel("equal")}
            raise RuntimeError("backend cannot enforce allowed_paths")

    monkeypatch.setattr(
        embeddings,
        "get_embedding_index",
        lambda _root: RefusingIndex(),
    )
    monkeypatch.setattr(
        embeddings,
        "embed_texts",
        lambda _texts, *, is_query: np.stack([_vector(1.0)]),
    )
    failed: list[str] = []
    hits = find_module.find(
        conformance_vault,
        query="parity-needle",
        scope="kb-only",
        mode="vector",
        graph=False,
        filters={"page.frontmatter:/metadata/probe": {"$eq": "café"}},
        failed_out=failed,
        limit=20,
    )
    assert [hit.path for hit in hits] == [_rel("equal")]
    assert "vector" in failed


def test_optional_clip_backend_receives_and_cannot_escape_exact_eligibility(
    conformance_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = conformance_vault / "Knowledge Base" / "Evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    for name, probe in (("eligible", "café"), ("ineligible", "other")):
        frontmatter = yaml.safe_dump(
            {
                "type": "source",
                "status": "active",
                "media_type": "image",
                "media_file": f"Knowledge Base/Evidence/{name}.jpg",
                "metadata": {"probe": probe},
            },
            allow_unicode=True,
            sort_keys=False,
        )
        (evidence / f"{name}.jpg.md").write_text(
            f"---\n{frontmatter}---\n\n# {name}\n",
            encoding="utf-8",
        )

    expected_allowed = {"Knowledge Base/Evidence/eligible.jpg"}

    class ClipIndex:
        def search(
            self,
            _query_vec: np.ndarray,
            *,
            k: int,
            allowed_paths: set[str] | None = None,
        ) -> list[tuple[str, float | None, float]]:
            assert k > 0
            assert allowed_paths == expected_allowed
            # A defensive shared post-filter must still reject a backend that
            # violates the allowlist it was given.
            return [
                ("Knowledge Base/Evidence/ineligible.jpg", None, 1.0),
                ("Knowledge Base/Evidence/eligible.jpg", None, 0.9),
            ]

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(readiness, "should_defer", lambda _component: False)
    monkeypatch.setattr(embeddings, "clip_enabled", lambda: True)
    monkeypatch.setattr(
        embeddings,
        "get_embedding_index",
        lambda _root: (_ for _ in ()).throw(ImportError("vector unavailable")),
    )
    monkeypatch.setattr(
        embeddings, "embed_texts", lambda *_args, **_kwargs: np.stack([_vector(1.0)])
    )
    monkeypatch.setattr(embeddings, "embed_clip_text", lambda _query: _vector(1.0))
    monkeypatch.setattr(embeddings, "get_clip_index", lambda _root: ClipIndex())
    find_module.clear_cache()

    hits = find_module.find(
        conformance_vault,
        query="visual-only-no-literal",
        scope="kb-only",
        mode="vector",
        graph=False,
        rerank=False,
        temporal=False,
        file_types=["image"],
        filters={"page.frontmatter:/metadata/probe": {"$eq": "café"}},
        limit=20,
    )
    assert [hit.path for hit in hits] == ["Knowledge Base/Evidence/eligible.jpg.md"]


def _candidate_work_must_not_start(*_args: object, **_kwargs: object) -> None:
    pytest.fail("invalid filter reached candidate generation")


def _forbid_candidate_work(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("_eligible_filter_paths", "_find_keyword", "_find_semantic_units"):
        monkeypatch.setattr(find_module, name, _candidate_work_must_not_start)
    monkeypatch.setattr(
        find_module.find_candidates,
        "collect_candidates",
        _candidate_work_must_not_start,
    )


def test_generic_resource_and_injection_failures_precede_candidate_work(
    conformance_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_candidate_work(monkeypatch)
    large_raw_plan = {
        "$and": [
            {f"page.frontmatter:/field-{index}": {"$eq": (f"value-{index}-" + ("x" * 590))}}
            for index in range(32)
        ]
    }
    combined_values = {
        "$and": [
            {f"page.frontmatter:/values-{index}": {"$in": list(range(64))}} for index in range(4)
        ]
    }
    five_deep = {"$not": {"$not": {"$not": {"$not": {"$not": {"page.status": {"$eq": "active"}}}}}}}
    too_many_leaves = {
        "$and": [{f"page.frontmatter:/leaf-{index}": {"$exists": True}} for index in range(33)]
    }
    cases = (
        (
            {"filters": {"page.status": {"$regex": "active"}}},
            "INVALID_FILTER_OPERATOR",
        ),
        (
            {"filters": {"$where": "this.metadata.probe"}},
            "INVALID_FILTER_FIELD",
        ),
        (
            {"filters": {"page.frontmatter:/probe": {"$in": ["same"] * 65}}},
            "FILTER_TOO_COMPLEX",
        ),
        (
            {"filters": {"page.frontmatter:/probe": {"$all": ["same"] * 65}}},
            "FILTER_TOO_COMPLEX",
        ),
        (
            {"filters": {"page.frontmatter:/probe": {"$eq": "x" * 1025}}},
            "FILTER_TOO_LARGE",
        ),
        (
            {"filters": {"page.frontmatter:/" + "/".join(["x"] * 17): {"$exists": True}}},
            "FILTER_TOO_COMPLEX",
        ),
        (
            {"filters": {"page.frontmatter:/" + ("x" * 513): {"$exists": True}}},
            "FILTER_TOO_LARGE",
        ),
        (
            {"filters": {"page.frontmatter:/number": {"$eq": 10**64}}},
            "FILTER_TOO_LARGE",
        ),
        ({"filters": five_deep}, "FILTER_TOO_COMPLEX"),
        ({"filters": too_many_leaves}, "FILTER_TOO_COMPLEX"),
        ({"filters": large_raw_plan}, "FILTER_TOO_LARGE"),
        (
            {"filters": combined_values, "tags": ["one-more"]},
            "FILTER_TOO_COMPLEX",
        ),
        (
            {"projects": ["x" * 1025]},
            "FILTER_TOO_LARGE",
        ),
    )
    for kwargs, expected_code in cases:
        with pytest.raises(FilterError) as caught:
            find_module.find(
                conformance_vault,
                query="parity-needle",
                scope="kb-only",
                mode="hybrid",
                **kwargs,
            )
        assert caught.value.code == expected_code


def test_structural_and_alias_resolved_plan_limits_precede_candidate_work(
    conformance_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_candidate_work(monkeypatch)
    values = [f"{index:02d}-" + ("x" * 97) for index in range(64)]
    structural = {
        "$and": [
            {"page.frontmatter:/generic-a": {"$in": values}},
            {"page.frontmatter:/generic-b": {"$in": values}},
        ]
    }
    with pytest.raises(FilterError) as structural_error:
        find_module.find(
            conformance_vault,
            query="parity-needle",
            scope="kb-only",
            filters=structural,
            projects=values,
            tags=values,
        )
    assert structural_error.value.code == "FILTER_TOO_LARGE"

    class ExpandingRegistry:
        @staticmethod
        def resolve_category(value: str) -> semantic_language_registry.LabelResolution:
            resolved = (value + "_" + ("x" * 64))[:64]
            return semantic_language_registry.LabelResolution(
                raw=value,
                key=value,
                resolved=resolved,
                status="active",
                definition=None,
            )

        @staticmethod
        def resolve_kind(value: str) -> semantic_language_registry.LabelResolution:
            raise AssertionError(f"unexpected kind resolution: {value}")

    monkeypatch.setattr(
        semantic_language_registry,
        "load_registry",
        lambda _root: ExpandingRegistry(),
    )
    alias_expression = {
        "$and": [
            {"unit.category": {"$in": [f"a{group}_{index}" for index in range(64)]}}
            for group in range(4)
        ]
    }
    with pytest.raises(FilterError) as resolved_error:
        find_module.find(
            conformance_vault,
            query="parity-needle",
            scope="kb-only",
            filters=alias_expression,
        )
    assert resolved_error.value.code == "FILTER_TOO_LARGE"


@pytest.mark.parametrize(
    "shortcut",
    [
        "types",
        "projects",
        "tags",
        "speakers",
        "file_types",
        "exclude_file_types",
        "categories",
        "kinds",
    ],
)
def test_every_shortcut_raw_limit_fails_before_candidate_work(
    conformance_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    shortcut: str,
) -> None:
    _forbid_candidate_work(monkeypatch)
    with pytest.raises(FilterError) as caught:
        find_module.find(
            conformance_vault,
            query="parity-needle",
            scope="kb-only",
            **{shortcut: ["duplicate"] * 65},
        )
    assert caught.value.code == "FILTER_TOO_COMPLEX"
    assert caught.value.path == f"$.shortcuts.{shortcut}"


@pytest.mark.parametrize(
    ("shortcut", "value"),
    [
        *(
            (shortcut, ["x" * 1025])
            for shortcut in (
                "types",
                "projects",
                "tags",
                "speakers",
                "file_types",
                "exclude_file_types",
                "categories",
                "kinds",
            )
        ),
        ("updated_after", "x" * 1025),
        ("updated_before", "x" * 1025),
    ],
)
def test_every_shortcut_value_limit_fails_before_candidate_work(
    conformance_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    shortcut: str,
    value: object,
) -> None:
    _forbid_candidate_work(monkeypatch)
    with pytest.raises(FilterError) as caught:
        find_module.find(
            conformance_vault,
            query="parity-needle",
            scope="kb-only",
            **{shortcut: value},
        )
    assert caught.value.code == "FILTER_TOO_LARGE"
    assert caught.value.path.startswith(f"$.shortcuts.{shortcut}")


def test_multibyte_string_boundary_is_measured_without_coercion(
    conformance_vault: Path,
) -> None:
    hits = find_module.find(
        conformance_vault,
        query="",
        scope="kb-only",
        mode="keyword",
        filters={"page.frontmatter:/metadata/probe": {"$eq": "💾" * 1024}},
        limit=20,
    )
    assert hits == []


def test_sql_injection_text_is_an_inert_exact_value(
    conformance_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "fts5")
    payload = '"; DROP TABLE pages; --'
    injected = find_module.find(
        conformance_vault,
        query="parity-needle",
        scope="kb-only",
        mode="hybrid",
        graph=False,
        temporal=False,
        filters={"page.frontmatter:/metadata/probe": {"$eq": payload}},
        limit=20,
    )
    assert injected == []
    assert lexstore.cache_token(conformance_vault) == "fts5"

    healthy = find_module.find(
        conformance_vault,
        query="parity-needle",
        scope="kb-only",
        mode="hybrid",
        graph=False,
        temporal=False,
        filters={"page.frontmatter:/metadata/probe": {"$eq": "café"}},
        limit=20,
    )
    assert [hit.path for hit in healthy] == [_rel("equal")]


def test_category_alias_conflict_fails_before_candidate_work(
    conformance_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = conformance_vault / "Knowledge Base" / "_Schema" / "semantic-language-registry.yaml"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        "schema_version: 1\n"
        "categories:\n"
        "  config:\n"
        "    description: Configuration facts\n"
        "    aliases: [shared]\n"
        "  rule:\n"
        "    description: Rule facts\n"
        "    aliases: [shared]\n"
        "kinds: {}\n",
        encoding="utf-8",
    )
    find_module.clear_cache()
    _forbid_candidate_work(monkeypatch)
    with pytest.raises(FilterError) as caught:
        find_module.find(
            conformance_vault,
            query="",
            scope="kb-only",
            categories=["shared"],
        )
    assert caught.value.code == "INVALID_FILTER_VALUE"
    assert caught.value.path == "$.shortcuts.categories"
