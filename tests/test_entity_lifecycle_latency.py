"""Model-free cold bounds for recurring identities and family filtering."""

from __future__ import annotations

from pathlib import Path
from time import perf_counter
from types import SimpleNamespace

from exomem import entity_recurrence, entity_types, epistemic_graph
from exomem.vault import WikilinkResolver

ESTABLISHED_CORPUS_SIZE = 2_000
COLD_RUNTIME_CEILING_SECONDS = 2.0


def test_identity_frames_cold_collection_is_bounded_at_established_scale(
    tmp_path: Path,
) -> None:
    pages = [
        SimpleNamespace(
            rel_path=f"Knowledge Base/Notes/note-{index:04d}.md",
            title=f"Note {index}",
            status="active",
            frontmatter={"type": "insight", "status": "active"},
            body=(
                "amber guild is an organization."
                if index % 2 == 0
                else "Membership: amber guild."
            ),
        )
        for index in range(ESTABLISHED_CORPUS_SIZE)
    ]
    registry = entity_types.core_registry()
    entities = entity_recurrence.registry_index(pages, entity_types=registry)
    resolver = WikilinkResolver.from_entries(tmp_path, ())

    started = perf_counter()
    candidates = entity_recurrence.collect(
        pages,
        vault_root=tmp_path,
        resolver=resolver,
        registry=entities,
        entity_types=registry,
        indexable=lambda _path: True,
        attachment_probe=lambda _path: False,
    )
    elapsed = perf_counter() - started

    assert len(candidates) == 1
    assert candidates[0].context_count == ESTABLISHED_CORPUS_SIZE
    assert elapsed < COLD_RUNTIME_CEILING_SECONDS


def test_entity_family_graph_query_and_traversal_are_bounded_at_scale(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "Knowledge Base/_Schema/entity-types.yaml"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(
        "schema_version: 1\n"
        "entity_types:\n"
        "  community:\n"
        "    folder: Communities\n"
        "    label: Community\n"
        "    aliases: []\n"
        "    cue_nouns: [community]\n"
        "    capture_guidance: A stable synthetic community identity.\n"
        "    parent: organization\n"
        "    status: active\n",
        encoding="utf-8",
    )
    seed_rel = "Knowledge Base/Notes/family-seed.md"
    seed_path = tmp_path / seed_rel
    seed_path.parent.mkdir(parents=True)
    seed_path.write_text(
        "---\ntype: insight\ntitle: Family Seed\nstatus: active\n---\n\n# Family Seed\n",
        encoding="utf-8",
    )
    index = epistemic_graph.EpistemicGraphIndex(tmp_path)
    index.rebuild_all()

    leaves = ("organization", "community", "concept", "community", "organization")
    seed_key = f"file:{seed_rel}"
    connected_root = tmp_path / "Knowledge Base/Entities/Synthetic"
    connected_root.mkdir(parents=True)
    for item_index in range(120):
        (connected_root / f"entity-{item_index:04d}.md").write_text(
            "---\ntype: entity\nstatus: active\n---\n",
            encoding="utf-8",
        )
    conn = index._connect()
    try:
        with conn:
            for item_index in range(5_000):
                leaf = leaves[item_index % len(leaves)]
                rel = f"Knowledge Base/Entities/Synthetic/entity-{item_index:04d}.md"
                node_key = f"file:{rel}"
                epistemic_graph._insert_node(
                    conn,
                    epistemic_graph.GraphNode(
                        node_key=node_key,
                        kind="file",
                        path=rel,
                        anchor="page",
                        title=f"Entity {item_index}",
                        text=f"Entity {item_index}",
                        source_hash=f"hash-{item_index}",
                        metadata={
                            "page_type": "entity",
                            "status": "active",
                            "scope": leaf,
                            "entity_type": leaf,
                            "origin": "file",
                        },
                    ),
                )
                if item_index < 120:
                    epistemic_graph._insert_edge(
                        conn,
                        epistemic_graph._edge(
                            seed_key,
                            node_key,
                            "links_to",
                            "wikilink",
                            source_path=seed_rel,
                            source_anchor="page",
                        ),
                    )
            epistemic_graph._bump_generation(conn)
    finally:
        conn.close()

    started = perf_counter()
    result = epistemic_graph.graph_context(
        tmp_path,
        path=seed_rel,
        depth=1,
        max_nodes=200,
        max_edges=200,
        entity_type_families=["organization"],
    )
    elapsed = perf_counter() - started

    matched = [node for node in result["nodes"] if node["path"] != seed_rel]
    assert result["available"] is True
    assert len(matched) == 96, result
    assert all(
        node["metadata"]["entity_family"] == "organization" for node in matched
    )
    assert elapsed < COLD_RUNTIME_CEILING_SECONDS
