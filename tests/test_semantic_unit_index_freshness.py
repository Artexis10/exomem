"""One-pass semantic refresh and query-time parent generation validation."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from exomem import (
    call_spans,
    epistemic_graph,
    freshness,
    index_sync,
    lexstore,
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


def _write(root: Path, content: str, *, clear_freshness: bool = True) -> Path:
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
    if clear_freshness:
        find_module.clear_cache()
    else:
        find_module.reset_page_and_result_caches()
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


def test_parent_validation_accepts_a_symlink_to_the_vault_root(tmp_path: Path) -> None:
    from exomem import activation_manifest, deferred_write_advisory

    root = tmp_path / "vault"
    path = _write(root, "- [fact] current token ^current\n")
    alias = tmp_path / "vault-alias"
    try:
        alias.symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    state = semantic_index.build_parent_index_state(root, _REL)
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    assert semantic_index.current_parent_index_state(alias, _REL).parent_source_hash == expected
    assert activation_manifest.build_census(alias).by_path[_REL].source_hash == expected
    assert deferred_write_advisory._observe_fingerprint(alias, _REL) == expected

    validated = semantic_index.validate_parent_record(
        alias,
        parent_path=_REL,
        parent_generation_value=state.parent_generation,
        parent_source_hash=expected,
        parser_version=state.parser_version,
    )

    assert validated.current, validated.code
    assert validated.current_parent_source_hash == expected


def test_restricted_unit_statistics_retire_after_unit_only_repairs(tmp_path: Path) -> None:
    """Warm ranking must observe unit repairs even when page rows and checkpoints stay fixed."""
    _write(tmp_path, "- [fact] harbor gauges ^gauges\n- [fact] harbor tides ^tides\n")
    lexstore.ensure_fresh(tmp_path)

    def ranked():
        with call_spans.meter(call_spans.STATS_REBUILT) as measured:
            hits = lexstore.search_semantic_units(
                tmp_path, "harbor", 5, scope="kb", admitted_parent_paths={_REL},
                _validate_current=False, repair=False,
            )
        assert hits is not None
        return hits, int(measured[call_spans.STATS_REBUILT])

    before, rebuilt = ranked()
    assert len(before) == 2 and rebuilt == 1
    assert ranked() == (before, 0)
    connection = lexstore.get_store(tmp_path)._connect()
    try:
        with connection:
            rowid = connection.execute(
                "SELECT rowid FROM semantic_units WHERE parent_path = ? AND anchor = ?",
                (_REL, "gauges"),
            ).fetchone()[0]
            connection.execute("UPDATE unit_fts SET stemmed = 'unrelated' WHERE rowid = ?", (rowid,))
        corrected, rebuilt = ranked()
        assert [hit.anchor for hit in corrected] == ["tides"] and rebuilt == 1
        assert ranked() == (corrected, 0)
        with connection:
            connection.execute(
                "DELETE FROM semantic_units WHERE parent_path = ? AND anchor = ?", (_REL, "tides"),
            )
        assert ranked() == ([], 1)
        assert ranked() == ([], 0)
    finally:
        connection.close()


def test_warm_restricted_duplicates_keep_baseline_tokens_and_last_eligible_occurrence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Duplicate selection stays exact; unrelated growth bounds only unique-reference text reads."""
    import json
    import uuid

    from exomem import bm25, working_set_runtime

    def write(name, content, page_id):
        path = tmp_path / "Knowledge Base/Notes" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nexomem_id: {page_id}\n---\n# Units\n\n{content}\n", encoding="utf-8")
        return path.relative_to(tmp_path).as_posix()

    duplicates = [
        write("a-first", "- [observation] lighthouse first tokens ^shared", _PAGE_ID),
        write("m-first-eligible", "- [config] channel earlier alias ^shared", _PAGE_ID),
        write("m-last-eligible", "- [config] channel later alias ^shared", _PAGE_ID),
        write("z-ineligible", "- [observation] estuary ignored alias ^shared", _PAGE_ID),
    ]
    parents = set(duplicates)
    unique = write("unique-beacon", "- [config] beacon unique signal ^beacon",
                   uuid.uuid5(uuid.NAMESPACE_URL, "unit-reuse:unique"))
    parents.add(unique)

    def frozen_baseline(query):
        # Freeze the former unordered corpus/candidate traversals together:
        # duplicate precedence is not a rowid maximum or the MATCH order.
        connection = lexstore.get_store(tmp_path)._connect()
        try:
            connection.execute("BEGIN")
            view = lexstore._SelectedUnits(tmp_path, connection, admitted=parents)
            corpus = {}
            for row, unit in view.served(connection.execute(
                "SELECT u.unit_ref, u.parent_path, unit_fts.stemmed FROM semantic_units u "
                "JOIN unit_fts ON unit_fts.rowid = u.rowid "
                "WHERE u.in_kb = 1 AND u.parent_path IN (SELECT value FROM json_each(?))",
                (json.dumps(sorted(parents)),),
            )):
                corpus.setdefault(str(unit.unit_ref), str(row[2]).split())
            plans = view.plans(categories=("config",), kinds=(), clauses=None, registry_scope=None)
            predicate, params = view.predicate(plans)
            candidates = {
                str(unit.unit_ref): (row, unit)
                for row, unit in view.served(connection.execute(
                    f"SELECT {lexstore._OCCURRENCE_COLUMNS} FROM semantic_units u "
                    f"WHERE u.in_kb = 1 AND {predicate} "
                    "AND u.parent_path IN (SELECT value FROM json_each(?))",
                    [*params, json.dumps(sorted(parents))],
                )) if view.matches(plans, str(row[1]), unit) and str(unit.unit_ref) in corpus
            }
            view.require_complete()
            return [lexstore._unit_hit(*candidates[ref], score) for ref, score in bm25.score_token_corpus(
                corpus, bm25.tokenize(query, query=True), 5, allowed_paths=set(candidates),
            )]
        finally:
            connection.close()

    def ranked(query):
        result = lexstore.get_store(tmp_path).search_semantic_units_result(
            [], 5, ("config",), (), "kb", None, admitted_parent_paths=parents,
            query_units=bm25.token_units(query, query=True),
            term_budget=working_set_runtime.material_term_budget(),
        )
        assert result.readiness.complete
        return list(result.value or ())

    for filler_count in (8, 32):
        for index in range(filler_count):
            parents.add(write(
                f"filler-{index:02d}", f"- [config] unrelated gauges {index} ^gauge",
                uuid.uuid5(uuid.NAMESPACE_URL, f"unit-reuse:{index}"),
            ))
        find_module.clear_cache()
        lexstore.ensure_fresh(tmp_path)
        expected = {query: frozen_baseline(query) for query in ("lighthouse", "channel", "beacon")}
        assert len(expected["lighthouse"]) == 1
        assert expected["channel"] == []
        assert len(expected["beacon"]) == 1 and expected["beacon"][0].parent_path == unique
        # Ambiguous refs preserve the old SELECT's full traversal, which remains unbounded.
        for query, baseline in expected.items():
            assert ranked(query) == baseline
        store = lexstore.get_store(tmp_path)
        original_connect = store._connect
        fetched_text = []
        interpreted_parents = []

        def counted_connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)

            def row_factory(cursor, row):
                names = {column[0] for column in cursor.description}
                if names & {"content", "stemmed"}:
                    fetched_text.append(row)
                if "structure_json" in names:
                    interpreted_parents.append(row)
                return row

            connection.row_factory = row_factory
            return connection

        with monkeypatch.context() as counted:
            counted.setattr(store, "_connect", counted_connect)
            with call_spans.meter(call_spans.STATS_REBUILT) as measured:
                assert ranked("beacon") == expected["beacon"]
            assert measured[call_spans.STATS_REBUILT] == 0
        assert len(fetched_text) == 2  # One representative and one winning alias, at either corpus size.
        assert len(interpreted_parents) <= 2


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
    assert all(freshness.rebaseline(tmp_path).values())
    graph.rebuild_all()

    path = _write(
        tmp_path,
        "- [rule] current token ^current\n",
        clear_freshness=False,
    )

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


def test_registry_change_updates_selected_units_without_rebuilding_structural_rows(
    tmp_path: Path, monkeypatch
) -> None:
    _write_language_registry(tmp_path, alias="local_setting", canonical="config")
    page = _write(tmp_path, "- [local_setting] registry-bound token ^registry\n")
    entries = [(str(page), freshness.stat_signature(page))]
    freshness.seed(tmp_path, "kb", entries)
    freshness.seed(tmp_path, "vault", entries)
    lexstore.ensure_fresh(tmp_path)
    current_triple = freshness.triple(tmp_path, "kb")
    initial = lexstore.search_semantic_units(
        tmp_path,
        "registry-bound",
        k=5,
        categories=["config"],
        scope="kb",
        freshness=current_triple,
    )
    assert initial and [hit.category for hit in initial] == ["config"]

    _write_language_registry(tmp_path, alias="local_setting", canonical="setting")
    scheduled: list[Path] = []
    monkeypatch.setattr(lexstore, "_schedule_repair", lambda root, **_kwargs: scheduled.append(root))

    # Definition changes reinterpret the unchanged structural projection immediately.
    assert lexstore.search_semantic_units(
        tmp_path,
        "registry-bound",
        k=5,
        categories=["config"],
        scope="kb",
        freshness=current_triple,
    ) == []
    assert scheduled == []

    repaired = lexstore.search_semantic_units(
        tmp_path,
        "registry-bound",
        k=5,
        categories=["setting"],
        scope="kb",
        freshness=freshness.triple(tmp_path, "kb"),
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
    rebuilt = semantic_index.selected_parent_index_state(tmp_path, path)

    assert [unit.category for unit in preflight.document.units] == ["config"]
    assert rebuilt.document == preflight.document
