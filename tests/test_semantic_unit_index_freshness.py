"""One-pass semantic refresh and query-time parent generation validation."""

from __future__ import annotations

from pathlib import Path

from exomem import (
    epistemic_graph,
    index_sync,
    lexstore,
    reconcile,
    semantic_contract,
    semantic_index,
    semantic_language_registry,
    semantic_units,
)
from exomem import (
    find as find_module,
)

_PAGE_ID = "44444444-4444-4444-8444-444444444444"
_REL = "Knowledge Base/Notes/Insights/fresh-units.md"


def _write(root: Path, content: str) -> Path:
    path = root / _REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "type: insight\n"
        "title: Fresh units\n"
        f"exomem_id: {_PAGE_ID}\n"
        "---\n"
        "# Fresh units\n\n"
        f"{content.rstrip()}\n",
        encoding="utf-8",
    )
    find_module.clear_cache()
    return path


def _write_language_registry(root: Path, *, alias: str, canonical: str) -> None:
    path = semantic_language_registry.registry_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "schema_version: 1\n"
        "categories:\n"
        f"  {canonical}:\n"
        f"    description: {canonical} facts\n"
        f"    aliases: [{alias}]\n"
        "kinds: {}\n",
        encoding="utf-8",
    )


def test_index_sync_parses_each_changed_parent_once_for_all_unit_sidecars(
    tmp_path: Path, monkeypatch
) -> None:
    path = _write(tmp_path, "- [config] before ^before\n")
    assert lexstore.search_semantic_units(tmp_path, "before", k=5, scope="kb")
    epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()
    path = _write(tmp_path, "- [rule] after ^after\n")

    calls = 0
    original = semantic_units.parse_semantic_units

    def _counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(semantic_units, "parse_semantic_units", _counted)

    report = index_sync.upsert_after_write(tmp_path, [path], defer_semantic=True)

    assert calls == 1
    assert report.reconcile_required is False
    assert lexstore.search_semantic_units(tmp_path, "before", k=5, scope="kb") == []
    assert lexstore.search_semantic_units(tmp_path, "after", k=5, scope="kb")
    graph_units = [
        node
        for node in epistemic_graph.EpistemicGraphIndex(tmp_path).nodes(path=_REL)
        if node["metadata"].get("record_type") == "semantic_unit"
    ]
    assert [node["text"] for node in graph_units] == ["after"]


def test_index_sync_reuses_preflight_semantic_state_without_reparsing(
    tmp_path: Path, monkeypatch
) -> None:
    path = _write(tmp_path, "- [config] before ^before\n")
    assert lexstore.search_semantic_units(tmp_path, "before", k=5, scope="kb")
    epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()
    path = _write(tmp_path, "- [rule] preflight result ^after\n")
    page_state = semantic_contract.build_page_state(
        tmp_path,
        _REL,
        path.read_text(encoding="utf-8"),
    )
    index_state = semantic_index.from_semantic_page_state(page_state)

    def _unexpected_parse(*_args, **_kwargs):
        raise AssertionError("index fan-out reparsed an already-evaluated parent")

    original_parse = semantic_units.parse_semantic_units
    monkeypatch.setattr(semantic_units, "parse_semantic_units", _unexpected_parse)

    report = index_sync.upsert_after_write(
        tmp_path,
        [path],
        defer_semantic=True,
        semantic_states={_REL: index_state},
    )
    monkeypatch.setattr(semantic_units, "parse_semantic_units", original_parse)

    assert report.reconcile_required is False
    assert lexstore.search_semantic_units(tmp_path, "before", k=5, scope="kb") == []
    assert lexstore.search_semantic_units(
        tmp_path, "preflight", k=5, scope="kb"
    )
    graph_units = [
        node
        for node in epistemic_graph.EpistemicGraphIndex(tmp_path).nodes(path=_REL)
        if node["metadata"].get("record_type") == "semantic_unit"
    ]
    assert [node["text"] for node in graph_units] == ["preflight result"]


def test_committed_parent_hash_rejects_old_lexical_and_graph_units_immediately(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path, "- [config] stale token ^stale\n")
    old_hits = lexstore.search_semantic_units(tmp_path, "stale", k=5, scope="kb")
    assert old_hits
    store = lexstore.get_store(tmp_path)
    old_freshness = store._synced["kb"]
    graph = epistemic_graph.EpistemicGraphIndex(tmp_path)
    graph.rebuild_all()

    path = _write(tmp_path, "- [rule] current token ^current\n")

    # Even a caller handing the lexical sidecar its old trusted corpus token
    # cannot make an old unit pass the independent current-file generation gate.
    assert (
        lexstore.search_semantic_units(
            tmp_path,
            "stale",
            k=5,
            scope="kb",
            freshness=old_freshness,
        )
        == []
    )
    stale_context = epistemic_graph.graph_context(tmp_path, path=_REL, depth=1)
    assert not any(
        node["metadata"].get("record_type") == "semantic_unit"
        for node in stale_context["nodes"]
    )
    assert any(
        warning["code"] == "semantic_unit_index_drift"
        for warning in stale_context["warnings"]
    )

    # A current lexical transaction may land while graph remains old. Current
    # lexical results are usable; stale graph state stays excluded.
    store.upsert_paths([path])
    current = lexstore.search_semantic_units(tmp_path, "current", k=5, scope="kb")
    assert current and [hit.category for hit in current] == ["rule"]
    still_stale_graph = epistemic_graph.graph_context(tmp_path, path=_REL, depth=1)
    assert not any(
        node["metadata"].get("record_type") == "semantic_unit"
        for node in still_stale_graph["nodes"]
    )


def test_registry_change_rejects_semantically_stale_units_without_markdown_change(
    tmp_path: Path, monkeypatch
) -> None:
    _write_language_registry(tmp_path, alias="configuration", canonical="config")
    _write(tmp_path, "- [configuration] registry-bound token ^registry\n")
    initial = lexstore.search_semantic_units(
        tmp_path, "registry-bound", k=5, categories=["config"], scope="kb"
    )
    assert initial and [hit.category for hit in initial] == ["config"]

    _write_language_registry(tmp_path, alias="configuration", canonical="setting")

    assert (
        lexstore.search_semantic_units(
            tmp_path, "registry-bound", k=5, categories=["config"], scope="kb"
        )
        == []
    )

    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    report = reconcile.reconcile(tmp_path)

    assert report.semantic_unit_indexes_status == "repaired"
    repaired = lexstore.search_semantic_units(
        tmp_path, "registry-bound", k=5, categories=["setting"], scope="kb"
    )
    assert repaired and [hit.category for hit in repaired] == ["setting"]


def test_rebuild_and_writer_preflight_share_all_attached_project_scopes(
    tmp_path: Path,
) -> None:
    registry_path = semantic_language_registry.registry_path(tmp_path)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        "schema_version: 1\n"
        "categories:\n"
        "  config:\n"
        "    description: Configuration facts\n"
        "    aliases: [configuration]\n"
        "    scope:\n"
        "      projects: [companion]\n"
        "kinds: {}\n",
        encoding="utf-8",
    )
    path = tmp_path / _REL
    path.parent.mkdir(parents=True, exist_ok=True)
    source = (
        "---\n"
        "type: insight\n"
        "title: Multi-project units\n"
        f"exomem_id: {_PAGE_ID}\n"
        "projects: [atlas, companion]\n"
        "---\n"
        "# Multi-project units\n\n"
        "- [configuration] shared scope ^shared\n"
    )
    path.write_text(source, encoding="utf-8")
    language = semantic_language_registry.load_registry(tmp_path)

    preflight = semantic_contract.build_page_state(
        tmp_path,
        _REL,
        source,
        language_registry=language,
    )
    rebuilt = semantic_index.build_parent_index_state(tmp_path, path)

    assert [unit.category for unit in preflight.document.units] == ["config"]
    assert rebuilt.document == preflight.document
