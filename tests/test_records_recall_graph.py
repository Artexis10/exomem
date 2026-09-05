"""Records stay out of the derived ordinary-recall graph."""

from __future__ import annotations

from pathlib import Path

from exomem import context_pack, epistemic_graph, find_candidates, freshness
from exomem import find as find_module
from exomem import vault as vault_module


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _manifest() -> str:
    return """---
type: collection
exomem_id: 12345678-1234-4abc-8def-123456789abc
title: Health sessions
semantic_profile: records
collection_version: 1
lifecycle: active
schema_version: 1
storage:
  strategy: markdown-items
  format_version: 1
  source: items
item_schema:
  natural_key: [observed]
  fields:
    observed:
      type: string
---
# Health sessions
"""


def test_graph_rebuild_indexes_only_record_collection_manifest(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    manifest = _write(
        vault, "Knowledge Base/Records/Health/_collection.md", _manifest()
    )
    raw = _write(
        vault,
        "Knowledge Base/Records/Health/sessions/2026-08-02.md",
        "# Private workout\n\nSecret 100kg progression.",
    )
    _write(
        vault,
        "Knowledge Base/Notes/Insight.md",
        "# Ordinary insight\n\n[[Knowledge Base/Records/Health/sessions/2026-08-02]]",
    )

    index = epistemic_graph.EpistemicGraphIndex(vault)
    index.rebuild_all()

    paths = {node["path"] for node in index.nodes()}
    assert manifest.relative_to(vault).as_posix() in paths
    assert raw.relative_to(vault).as_posix() not in paths
    assert all(raw.relative_to(vault).as_posix() not in str(edge) for edge in index.edges())


def test_suppressed_refresh_purges_existing_graph_rows_when_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    raw = _write(
        vault,
        "Knowledge Base/Records/Health/sessions/2026-08-02.md",
        "# Private workout\n\nSecret progression.",
    )
    index = epistemic_graph.EpistemicGraphIndex(vault)
    conn = index._connect()
    try:
        with conn:
            epistemic_graph._insert_node(
                conn,
                epistemic_graph.GraphNode(
                    node_key=epistemic_graph._file_key(raw.relative_to(vault).as_posix()),
                    kind="file",
                    path=raw.relative_to(vault).as_posix(),
                    anchor="page",
                    title="Private workout",
                    text="Secret",
                    source_hash="hash",
                ),
            )
            epistemic_graph._insert_edge(
                conn,
                epistemic_graph.GraphEdge(
                    edge_key="edge",
                    src_key="file:Knowledge Base/Notes/Insight.md",
                    dst_key=epistemic_graph._file_key(raw.relative_to(vault).as_posix()),
                    relation_type="links_to",
                    raw_relation="links_to",
                    parent_relation=None,
                    registry_status="registered",
                    registry_version=1,
                    registry_hash="hash",
                    origin="test",
                    source_path="Knowledge Base/Notes/Insight.md",
                ),
            )
    finally:
        conn.close()

    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_INDEX", "1")
    index.refresh_paths([raw])

    conn = index._connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0] == 0
    finally:
        conn.close()


def test_recall_resolver_excludes_raw_record_stem_and_title_collision(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write(
        vault,
        "Knowledge Base/Notes/Progress.md",
        "---\ntitle: Visible progress\n---\n# Visible progress\n",
    )
    _write(
        vault,
        "Knowledge Base/Records/Health/Progress.md",
        "---\ntitle: Visible progress\n---\n# Private progress\n",
    )

    resolver = find_module.recall_resolver_snapshot(vault)
    resolved, warning = vault_module.normalize_wikilink(
        "Visible progress", vault, resolver=resolver, strict=False
    )

    assert warning is None
    assert resolved == "Knowledge Base/Notes/Progress"
    private, private_warning = vault_module.normalize_wikilink(
        "Knowledge Base/Records/Health/Progress", vault, resolver=resolver, strict=False
    )
    assert private_warning is not None
    assert private == "Knowledge Base/Records/Health/Progress"


def test_default_recall_resolver_cache_ignores_raw_record_edits(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write(vault, "Knowledge Base/Notes/Visible.md", "# Visible\n")
    raw = _write(
        vault,
        "Knowledge Base/Records/Health/session.md",
        "# Private session\n",
    )
    find_module._RECALL_RESOLVER_CACHE.clear()

    find_module.recall_resolver_snapshot(vault)
    first_identity = find_module._RECALL_RESOLVER_CACHE[vault][0]
    raw.write_text("# Private session after a much longer manual edit\n", encoding="utf-8")
    find_module.recall_resolver_snapshot(vault)

    assert find_module._RECALL_RESOLVER_CACHE[vault][0] == first_identity


def test_writer_resolver_coalesces_renamed_target_before_delayed_callback(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    source = _write(vault, "Knowledge Base/Notes/A.md", "# A\n\n[[Old B]]\n")
    target = _write(vault, "Knowledge Base/Notes/B.md", "# Old B\n")
    freshness.seed(
        vault,
        "vault",
        (
            (str(path), freshness.stat_signature(path))
            for path in vault_module.walk_vault_md(vault)
        ),
    )
    kb = vault / "Knowledge Base"
    freshness.seed(
        vault,
        "kb",
        (
            (str(path), freshness.stat_signature(path))
            for path in find_module._walk_md(kb)
        ),
    )
    find_module._RESOLVER_CACHE.clear()
    find_module.shared_resolver(vault)

    source.write_text("# A\n\n[[New B]]\n\nChanged.\n", encoding="utf-8")
    target.write_text("# New B\n\nChanged.\n", encoding="utf-8")
    freshness.on_files_changed(vault, changed=[source])
    freshness.on_files_changed(vault, changed=[target])
    find_module.on_resolver_files_changed(vault, ["Knowledge Base/Notes/A.md"], [])

    resolver = find_module.writer_resolver_snapshot(vault)
    resolved, warning = vault_module.normalize_wikilink(
        "New B", vault, resolver=resolver, strict=False
    )
    assert warning is None
    assert resolved == "Knowledge Base/Notes/B"
    old, old_warning = vault_module.normalize_wikilink(
        "Old B", vault, resolver=resolver, strict=False
    )
    assert old == "Old B"
    assert old_warning is not None

    find_module.on_resolver_files_changed(vault, ["Knowledge Base/Notes/B.md"], [])
    delayed = find_module.writer_resolver_snapshot(vault)
    delayed_resolved, delayed_warning = vault_module.normalize_wikilink(
        "New B", vault, resolver=delayed, strict=False
    )
    assert delayed_warning is None
    assert delayed_resolved == "Knowledge Base/Notes/B"


def test_live_recall_resolver_cache_is_patched_without_admitting_raw_records(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    visible = _write(
        vault,
        "Knowledge Base/Notes/Visible.md",
        "---\ntitle: Old visible title\n---\n# Visible\n",
    )
    raw = _write(
        vault,
        "Knowledge Base/Records/Health/private.md",
        "---\ntitle: Private title\n---\n# Private\n",
    )
    freshness.seed(
        vault,
        "vault",
        (
            (str(path), freshness.stat_signature(path))
            for path in vault_module.walk_vault_md(vault)
        ),
    )
    kb = vault / "Knowledge Base"
    freshness.seed(
        vault,
        "kb",
        (
            (str(path), freshness.stat_signature(path))
            for path in find_module._walk_md(kb)
        ),
    )
    find_module._RECALL_RESOLVER_CACHE.clear()
    find_module.recall_resolver_snapshot(vault)
    cached = find_module._RECALL_RESOLVER_CACHE[vault][1]

    visible.write_text(
        "---\ntitle: Replacement visible title\n---\n# Visible\n", encoding="utf-8"
    )
    raw.write_text(
        "---\ntitle: Leaking private title\n---\n# Private\n", encoding="utf-8"
    )
    freshness.on_files_changed(vault, changed=[visible, raw])
    find_module.on_resolver_files_changed(
        vault,
        [
            visible.relative_to(vault).as_posix(),
            raw.relative_to(vault).as_posix(),
        ],
        [],
    )

    assert find_module._RECALL_RESOLVER_CACHE[vault][1] is cached
    assert find_module._RECALL_RESOLVER_CACHE[vault][0] == find_module.FreshnessSnapshot(
        vault
    ).projection_key("vault")
    assert "replacement visible title" in cached.titles
    resolver = find_module.recall_resolver_snapshot(vault)
    resolved, warning = vault_module.normalize_wikilink(
        "Replacement visible title", vault, resolver=resolver, strict=False
    )
    assert warning is None
    assert resolved == "Knowledge Base/Notes/Visible"
    private, private_warning = vault_module.normalize_wikilink(
        "Leaking private title", vault, resolver=resolver, strict=False
    )
    assert private_warning is not None
    assert private == "Leaking private title"


def test_live_recall_resolver_coalesces_an_absolute_deleted_path(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write(vault, "Knowledge Base/Notes/Visible.md", "# Visible\n")
    target = _write(vault, "Knowledge Base/Notes/Target.md", "# Target\n")
    freshness.seed(
        vault,
        "vault",
        (
            (str(path), freshness.stat_signature(path))
            for path in vault_module.walk_vault_md(vault)
        ),
    )
    kb = vault / "Knowledge Base"
    freshness.seed(
        vault,
        "kb",
        (
            (str(path), freshness.stat_signature(path))
            for path in find_module._walk_md(kb)
        ),
    )
    find_module._RECALL_RESOLVER_CACHE.clear()
    find_module.recall_resolver_snapshot(vault)

    target.unlink()
    freshness.on_files_changed(vault, deleted=[target])
    # A delayed/unrelated callback must still consume the projected delete.
    find_module.on_resolver_files_changed(vault, ["Knowledge Base/Notes/Visible.md"], [])

    resolver = find_module.recall_resolver_snapshot(vault)
    resolved, warning = vault_module.normalize_wikilink(
        "Target", vault, resolver=resolver, strict=False
    )
    assert resolved == "Target"
    assert warning is not None


def test_recall_resolver_policy_change_evicts_instead_of_path_patching(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _write(vault, "Knowledge Base/Notes/Visible.md", "# Visible\n")
    freshness.seed(
        vault,
        "vault",
        (
            (str(path), freshness.stat_signature(path))
            for path in vault_module.walk_vault_md(vault)
        ),
    )
    kb = vault / "Knowledge Base"
    freshness.seed(
        vault,
        "kb",
        (
            (str(path), freshness.stat_signature(path))
            for path in find_module._walk_md(kb)
        ),
    )
    find_module._RECALL_RESOLVER_CACHE.clear()
    find_module.recall_resolver_snapshot(vault)

    (kb / "_access.yaml").write_text("excluded:\n  - Notes\n", encoding="utf-8")
    find_module.on_resolver_files_changed(
        vault, ["Knowledge Base/_access.yaml"], []
    )

    assert vault not in find_module._RECALL_RESOLVER_CACHE
    assert vault not in find_module._RECALL_RESOLVER_CHECKPOINTS
    rebuilt = find_module.recall_resolver_snapshot(vault)
    resolved, warning = vault_module.normalize_wikilink(
        "Visible", vault, resolver=rebuilt, strict=False
    )
    assert warning is not None
    assert resolved == "Visible"


def test_projected_resolver_participates_in_ram_cache_unload(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write(vault, "Knowledge Base/Notes/Visible.md", "# Visible\n")
    find_module._RECALL_RESOLVER_CACHE.clear()
    find_module.recall_resolver_snapshot(vault)

    unloaded = find_module.unload_ram_caches()

    assert unloaded["resolvers"] >= 1
    assert find_module._RECALL_RESOLVER_CACHE == {}
    assert find_module._RECALL_RESOLVER_CHECKPOINTS == {}


def test_context_neighborhood_drops_raw_record_inbound_link(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    note = _write(vault, "Knowledge Base/Notes/Visible.md", "# Visible\n")
    _write(
        vault,
        "Knowledge Base/Records/Health/hidden.md",
        "# Hidden\n\n[[Knowledge Base/Notes/Visible]]\n",
    )
    page = find_module._CACHE.get(note, vault)
    assert page is not None

    neighbors, dropped = context_pack._neighborhood(vault, [page], 10)

    assert neighbors == []
    assert dropped == 0


def test_index_path_rechecks_manifest_before_graph_publication(
    tmp_path: Path, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    manifest = _write(
        vault, "Knowledge Base/Records/Health/_collection.md", _manifest()
    )
    index = epistemic_graph.EpistemicGraphIndex(vault)
    resolver = find_module.recall_resolver_snapshot(
        vault, freshness=epistemic_graph._disk_vault_freshness(vault)
    )
    real_edges = epistemic_graph._edges_for_page

    def replace_with_raw(*args, **kwargs):
        result = real_edges(*args, **kwargs)
        manifest.write_text("# raw private workout\n", encoding="utf-8")
        return result

    monkeypatch.setattr(epistemic_graph, "_edges_for_page", replace_with_raw)
    conn = index._connect()
    try:
        assert index._index_path(conn, manifest, resolver=resolver) is False
        assert conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0] == 0
    finally:
        conn.close()


def test_fused_raw_record_path_is_refused_before_page_cache_hydration(
    tmp_path: Path, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    raw = _write(vault, "Knowledge Base/Records/Health/raw.md", "# raw\n")
    bundle = find_candidates.empty_bundle()
    bundle.fused = [(raw.relative_to(vault).as_posix(), 1.0)]
    bundle.had_rankings = True
    monkeypatch.setattr(find_candidates, "collect_candidates", lambda *_a, **_k: bundle)
    monkeypatch.setattr(
        find_module._CACHE,
        "get",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("raw path hydrated")),
    )

    assert find_module._find_semantic(
        vault,
        query="raw",
        query_norm="raw",
        types=None,
        projects=None,
        tags=None,
        speakers=None,
        file_types=None,
        exclude_file_types=None,
        limit=5,
        scope="kb",
        mode="hybrid",
    ) == []


def test_direct_graph_projection_ignores_raw_records_but_tracks_admitted_edits(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    visible = _write(vault, "Knowledge Base/Notes/Visible.md", "# visible\n")
    raw = _write(vault, "Knowledge Base/Records/Health/raw.md", "# raw one\n")

    before = epistemic_graph._disk_vault_freshness(vault)
    raw.write_text("# raw two\n", encoding="utf-8")
    assert epistemic_graph._disk_vault_freshness(vault) == before

    visible.write_text("# visible two\n", encoding="utf-8")
    assert epistemic_graph._disk_vault_freshness(vault) != before


def test_suppressed_path_purge_keeps_other_source_unit_collision_proof(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    raw_rel = "Knowledge Base/Records/Health/raw.md"
    visible_rel = "Knowledge Base/Notes/Visible.md"
    _write(vault, raw_rel, "# raw\n")
    _write(vault, visible_rel, "# visible\n")
    index = epistemic_graph.EpistemicGraphIndex(vault)
    shared_unit = "unit:shared-unit-ref"
    conn = index._connect()
    try:
        with conn:
            epistemic_graph._insert_node(
                conn,
                epistemic_graph.GraphNode(
                    node_key=shared_unit, kind="finding", path=raw_rel,
                    anchor="unit", title="raw", text="raw", source_hash="hash",
                ),
            )
            epistemic_graph._insert_edge(
                conn,
                epistemic_graph.GraphEdge(
                    edge_key="visible-proof",
                    src_key=shared_unit,
                    dst_key=epistemic_graph._file_key(visible_rel),
                    relation_type="derived_from",
                    raw_relation="derived_from",
                    parent_relation=None,
                    registry_status="registered",
                    registry_version=1,
                    registry_hash="hash",
                    origin="semantic_unit",
                    source_path=visible_rel,
                ),
            )
        index._delete_path(conn, raw_rel)
        remaining = conn.execute(
            "SELECT edge_key FROM graph_edges WHERE edge_key = 'visible-proof'"
        ).fetchone()
        assert remaining == ("visible-proof",)
    finally:
        conn.close()


def test_collision_override_allows_only_proven_current_endpoint(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    raw_rel = "Knowledge Base/Records/Health/raw.md"
    visible_rel = "Knowledge Base/Notes/Visible.md"
    _write(vault, raw_rel, "# raw\n")
    _write(vault, visible_rel, "# visible\n")
    index = epistemic_graph.EpistemicGraphIndex(vault)
    shared_unit = "unit:shared-unit-ref"
    edge = {
        "source_path": visible_rel,
        "src_key": shared_unit,
        "dst_key": epistemic_graph._file_key(visible_rel),
    }
    conn = index._connect()
    try:
        with conn:
            epistemic_graph._insert_node(
                conn,
                epistemic_graph.GraphNode(
                    node_key=shared_unit, kind="finding", path=raw_rel,
                    anchor="unit", title="raw", text="raw", source_hash="hash",
                ),
            )
        assert not epistemic_graph._edge_recall_allowed(conn, vault, edge)
        assert epistemic_graph._edge_recall_allowed(
            conn,
            vault,
            edge,
            endpoint_overrides={
                shared_unit: {
                    "node_key": shared_unit,
                    "path": visible_rel,
                }
            },
        )
    finally:
        conn.close()
