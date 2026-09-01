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


def test_entity_family_projection_and_traversal_filter_are_bounded_at_scale() -> None:
    registry = entity_types.load_entity_types(
        proposal={
            "schema_version": 1,
            "entity_types": {
                "community": {
                    "folder": "Communities",
                    "label": "Community",
                    "aliases": [],
                    "cue_nouns": ["community"],
                    "capture_guidance": "A stable synthetic community identity.",
                    "parent": "organization",
                    "status": "active",
                }
            },
        }
    )
    leaves = ("organization", "community", "concept", "community", "organization")
    nodes = [
        {
            "node_key": f"file:{index}",
            "kind": "file",
            "metadata": {"page_type": "entity", "scope": leaves[index % len(leaves)]},
        }
        for index in range(5_000)
    ]

    started = perf_counter()
    matched = [
        epistemic_graph._entity_family_metadata(node, registry)
        for node in nodes
        if epistemic_graph._matches_entity_families(
            node, {"organization"}, registry
        )
    ]
    elapsed = perf_counter() - started

    assert len(matched) == 4_000
    assert all(
        node["metadata"]["entity_family"] == "organization" for node in matched
    )
    assert elapsed < COLD_RUNTIME_CEILING_SECONDS
