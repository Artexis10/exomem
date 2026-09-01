from __future__ import annotations

from pathlib import Path
from uuid import UUID

from test_planning_mutation import _manifest

from exomem import bm25, freshness, index_sync


def test_planning_manifest_is_discoverable_but_raw_items_are_structured_only(
    tmp_path: Path,
) -> None:
    from exomem.recall_policy import is_recall_candidate, is_structured_only_path

    directory = tmp_path / "Knowledge Base" / "Planning" / "Work"
    items = directory / "Items"
    items.mkdir(parents=True)
    manifest = directory / "_collection.md"
    manifest.write_text(
        """---
type: collection
exomem_id: 2db90f18-70df-4e41-986e-2d7d7db1caca
title: Planning work
semantic_profile: planning
collection_version: 1
schema_version: 1
lifecycle: active
storage: {strategy: markdown-items, source: Items, format_version: 1}
item_schema:
  natural_key: [title]
  fields: {title: {type: string, required: true}}
---
""",
        encoding="utf-8",
    )
    item = items / "one.md"
    item.write_text("# raw work\n", encoding="utf-8")

    assert is_recall_candidate(tmp_path, manifest)
    assert not is_recall_candidate(tmp_path, item)
    assert is_structured_only_path(tmp_path, item)


def _seed_thousand_planning_items(tmp_path: Path) -> tuple[Path, Path]:
    from exomem.planning import add, create_collection

    manifest_path = "Knowledge Base/Planning/Scale/_collection.md"
    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Activity\n", encoding="utf-8")
    create_collection(
        tmp_path,
        manifest_path,
        _manifest() + "\nplanning scale needle\n",
        why="create scale collection",
    )
    first_id = str(UUID(int=1))
    add(
        tmp_path,
        manifest_path,
        plan_id=first_id,
        item={"title": "raw planning 0"},
        why="seed scale item",
    )
    manifest = tmp_path / manifest_path
    first_item = manifest.parent / "Items" / "raw planning 0.md"
    template = first_item.read_text(encoding="utf-8")
    for index in range(1, 1_000):
        item_id = str(UUID(int=index + 1))
        (manifest.parent / "Items" / f"{item_id}.md").write_text(
            template.replace(first_id, item_id).replace("raw planning 0", f"raw planning {index}"),
            encoding="utf-8",
        )
    return manifest, first_item


def test_thousand_planning_items_stay_out_of_recall_but_queries_remain_complete(
    tmp_path: Path, monkeypatch
) -> None:
    from exomem.planning import query

    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    manifest, first_item = _seed_thousand_planning_items(tmp_path)

    bm25.clear_cache()
    recalled = [path for path, _score in bm25.search(tmp_path, "planning scale needle", k=5)]
    assert manifest.relative_to(tmp_path).as_posix() in recalled
    assert first_item.relative_to(tmp_path).as_posix() not in recalled
    continuation = None
    returned = 0
    while True:
        page = query(
            tmp_path,
            manifest.relative_to(tmp_path).as_posix(),
            lifecycle="all",
            limit=50,
            continuation=continuation,
        )
        assert page["total_matched"] == 1_000
        returned += len(page["rows"])
        continuation = page["continuation"]
        if continuation is None:
            break
    assert returned == 1_000
    assert first_item.is_file()


def test_planning_raw_item_edits_do_not_churn_recall_freshness(tmp_path: Path) -> None:
    manifest, first_item = _seed_thousand_planning_items(tmp_path)

    before = freshness.recall_triple(tmp_path, "kb")
    first_item.write_text(
        first_item.read_text(encoding="utf-8") + "\nHuman note.\n", encoding="utf-8"
    )
    assert freshness.recall_triple(tmp_path, "kb") == before

    manifest.write_text(
        manifest.read_text(encoding="utf-8") + "\nManifest edit.\n", encoding="utf-8"
    )
    assert freshness.recall_triple(tmp_path, "kb") != before


def test_index_sync_keeps_planning_identity_but_purges_raw_items_semantically(
    tmp_path: Path, monkeypatch
) -> None:
    manifest, first_item = _seed_thousand_planning_items(tmp_path)
    seen: dict[str, list[list[str]]] = {}

    def capture(name: str):
        def _capture(_root, values, *_args, **_kwargs):
            seen[name] = [[str(value) for value in values]]

        return _capture

    from exomem import embeddings, epistemic_graph, find, lexstore, memory_refs

    monkeypatch.setattr(memory_refs, "upsert_after_write", capture("identity"))
    monkeypatch.setattr(lexstore, "upsert_after_write", capture("lexical"))
    monkeypatch.setattr(epistemic_graph, "upsert_after_write", capture("graph"))
    monkeypatch.setattr(
        embeddings,
        "upsert_after_write_status",
        lambda _root, paths: (
            capture("vector")(_root, paths)
            or embeddings.EmbeddingSyncStatus("completed", "embedding_upsert_completed", len(paths))
        ),
    )
    monkeypatch.setattr(find, "on_resolver_files_changed", lambda *_args: None)
    monkeypatch.setattr(index_sync, "purge_semantic_only", capture("purge"))

    index_sync.upsert_after_write(tmp_path, [first_item, manifest])

    raw = first_item.relative_to(tmp_path).as_posix()
    collection = manifest.relative_to(tmp_path).as_posix()
    assert seen["identity"] == [[str(first_item), str(manifest)]]
    assert seen["lexical"] == [[str(manifest)]]
    assert seen["graph"] == [[str(manifest)]]
    assert seen["vector"] == [[str(manifest)]]
    assert seen["purge"] == [[raw]]
    assert collection.endswith("_collection.md")


def test_reconcile_purges_a_stale_planning_semantic_receipt(tmp_path: Path, monkeypatch) -> None:
    from exomem import deferred_index, reconcile

    _manifest, first_item = _seed_thousand_planning_items(tmp_path)
    raw = first_item.relative_to(tmp_path).as_posix()
    with deferred_index._connect(tmp_path, create=True) as conn:
        conn.execute(
            "INSERT INTO semantic_upserts(rel_path, created_at, updated_at, revision) "
            "VALUES (?, 0, 0, 1)",
            (raw,),
        )
    monkeypatch.setattr("exomem.governance.receipts.reconcile", lambda *_args, **_kwargs: {})

    dry = reconcile.reconcile(tmp_path, dry_run=True)
    assert {item["component"] for item in dry.semantic_suppressed_drift} >= {"deferred_semantic"}
    applied = reconcile.reconcile(tmp_path)
    assert raw in applied.semantic_suppressed_purged
    assert reconcile.reconcile(tmp_path).semantic_suppressed_drift == []
