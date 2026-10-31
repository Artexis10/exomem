"""A relation reads as one display label wherever its key is served.

The key stays the machine identity. `relation_registry.display_label` is the one
formatter, and the producers add `relation_label` beside the key they already emit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import commands, epistemic_graph, find, relation_registry
from exomem.governance.principal import library_scope


def _registry(**extensions: dict) -> relation_registry.RelationRegistry:
    """The core relations plus these extensions, parsed as a reviewed proposal."""
    return relation_registry.load_registry(
        proposal={"schema_version": 1, "extensions": extensions}
    )


def _extension(parent: str = "owns", **fields: object) -> dict:
    return {"parent": parent, "description": "A reviewed meaning.", **fields}


def test_a_core_relation_reads_as_its_key() -> None:
    assert relation_registry.display_label(relation_registry.core_registry(), "owns") == "owns"


def test_an_extension_reads_as_a_kind_of_its_parent() -> None:
    registry = _registry(**{"vault.has_pet": _extension(aliases=["has_pet"])})

    assert relation_registry.display_label(registry, "vault.has_pet") == "has_pet (a kind of owns)"


def test_a_declared_label_replaces_the_name_in_the_key() -> None:
    registry = _registry(
        **{"vault.has_pet": _extension(label="keeps_pet", aliases=["keeps_pet"])}
    )

    assert (
        relation_registry.display_label(registry, "vault.has_pet") == "keeps_pet (a kind of owns)"
    )


def test_a_label_the_caller_could_not_pass_back_is_refused() -> None:
    registry = _registry(**{"vault.has_pet": _extension(label="keeps_pet")})

    assert [item["code"] for item in registry.findings if item["severity"] == "error"] == [
        "unresolved_label"
    ]


def test_a_shared_label_carries_its_namespace_and_a_core_relation_does_not() -> None:
    registry = _registry(
        **{
            "vault.applies": _extension(aliases=["vault_applies"]),
            "team.applies": _extension(aliases=["team_applies"]),
            "vault.has_pet": _extension(aliases=["has_pet"]),
        }
    )

    assert relation_registry.display_label(registry, "vault.applies") == (
        "vault.applies (a kind of owns)"
    )
    assert relation_registry.display_label(registry, "team.applies") == (
        "team.applies (a kind of owns)"
    )
    assert relation_registry.display_label(registry, "vault.has_pet") == "has_pet (a kind of owns)"
    assert relation_registry.display_label(registry, "owns") == "owns"


def test_a_deprecated_extension_is_marked() -> None:
    registry = _registry(
        **{
            "vault.has_pet": _extension(aliases=["has_pet"]),
            "vault.keeps_pet": _extension(
                status="deprecated", replaced_by="vault.has_pet", aliases=["keeps_pet"]
            ),
        }
    )

    assert relation_registry.display_label(registry, "vault.keeps_pet") == (
        "keeps_pet (a kind of owns) (deprecated)"
    )


def test_a_key_the_registry_does_not_hold_reads_as_itself() -> None:
    assert (
        relation_registry.display_label(relation_registry.core_registry(), "vault.ghost")
        == "vault.ghost"
    )


@pytest.mark.usefixtures("owner_scope")
def test_a_label_survives_save_restore_and_the_save_of_another_key(vault: Path) -> None:
    entry = _extension(parent="supports", label="guides_work", aliases=["guides_work"])
    before = commands.op_schema_memory(vault, subject="relations", operation="inspect")
    saved = commands.op_schema_memory(
        vault,
        subject="relations",
        operation="save",
        proposal={"upsert": {"vault.guides": entry}},
        expected_hash=before["content_hash"],
        why="register a labelled relation",
    )["saved"]
    loaded = relation_registry.load_registry(vault).definition("vault.guides")
    assert loaded is not None and loaded.label == "guides_work"

    # A reviewed save of a different key rewrites the whole overlay from the loaded
    # registry; the first key keeps its label.
    commands.op_schema_memory(
        vault,
        subject="relations",
        operation="save-relations",
        proposal={"upsert": {"vault.backs": _extension(parent="supports", aliases=["backs"])}},
        expected_hash=saved["content_hash"],
        why="register another relation",
    )
    assert relation_registry.load_registry(vault).definition("vault.guides").label == "guides_work"

    history = commands.op_schema_memory(vault, subject="relations", operation="history")
    restored = commands.op_schema_memory(
        vault,
        subject="relations",
        operation="restore",
        version=history["versions"][0]["version"],
        expected_hash=history["content_hash"],
        why="drop the second relation",
    )
    assert relation_registry.load_registry(vault).definition("vault.guides").label == "guides_work"
    assert restored["removed_keys"] == ["vault.backs"]


@pytest.fixture
def owner_scope():  # noqa: ANN201
    with library_scope():
        yield


def _register_has_pet(vault: Path) -> None:
    before = commands.op_schema_memory(vault, subject="relations", operation="inspect")
    commands.op_schema_memory(
        vault,
        subject="relations",
        operation="save",
        proposal={
            "upsert": {
                "vault.has_pet": {
                    "parent": "owns",
                    "description": "Keeps an animal.",
                    "aliases": ["has_pet"],
                }
            }
        },
        expected_hash=before["content_hash"],
        why="register has_pet",
    )


@pytest.mark.usefixtures("owner_scope")
def test_resolve_relation_labels_every_row_beside_its_unchanged_key(vault: Path) -> None:
    _register_has_pet(vault)

    result = commands.op_connect_memory(
        vault, operation="resolve-relation", requested_relation="has_pet"
    )

    match = result["exact_matches"][0]
    assert (match["canonical"], match["relation_label"]) == (
        "vault.has_pet",
        "has_pet (a kind of owns)",
    )
    core = {row["key"]: row for row in result["core_vocabulary"]}
    assert core["owns"]["relation_label"] == "owns"
    candidate = result["candidates"][0]
    assert (candidate["key"], candidate["canonical"]) == ("vault.has_pet", "vault.has_pet")
    assert candidate["relation_label"] == "has_pet (a kind of owns)"


# --- The payloads Studio renders ------------------------------------------- #
# Studio shows `relation_label` and falls back to the key without one, so a
# producer that drops the label degrades silently to the key.

INSIGHTS = "Knowledge Base/Notes/Insights"


def _seed_settles(vault: Path) -> None:
    """`hive` settles `swarm` through a unit relation spelled with a vault extension's alias."""
    schema = vault / "Knowledge Base" / "_Schema" / "relation-registry.yaml"
    schema.parent.mkdir(parents=True, exist_ok=True)
    schema.write_text(
        "schema_version: 1\nextensions:\n  vault.settles:\n    parent: answers\n"
        "    description: Settles an open question.\n    aliases: [settles]\n",
        encoding="utf-8",
    )
    pages = {
        "swarm": "Do hot hives swarm earlier?",
        "hive": f"## Claim\n- id: c-1\n- relations: settles: [[{INSIGHTS}/swarm]]\n\nShade settles it.",
    }
    for name, body in pages.items():
        path = vault / INSIGHTS / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\ntype: insight\nstatus: active\n---\n# {name} note\n\n{body}\n", encoding="utf-8"
        )
    find.clear_cache()
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()


def test_a_relation_queue_item_reads_as_the_meaning_its_spelling_authors(tmp_path: Path) -> None:
    _seed_settles(tmp_path)

    queue = commands.op_review_memory(tmp_path, mode="relation-queue")

    lifted = next(
        item
        for group in queue["groups"]
        for item in group["items"]
        if item["method"] == "unit_relation_lift"
    )
    assert (lifted["relation_type"], lifted["relation_label"]) == (
        "settles",
        "settles (a kind of answers)",
    )


def test_a_suggested_relation_reads_as_the_meaning_its_spelling_authors(tmp_path: Path) -> None:
    _seed_settles(tmp_path)

    candidates = commands.op_connect_memory(
        tmp_path, operation="suggest-relations", path=f"{INSIGHTS}/hive.md"
    )["candidates"]

    lifted = next(item for item in candidates if item["method"] == "unit_relation_lift")
    assert (lifted["relation_type"], lifted["relation_label"]) == (
        "settles",
        "settles (a kind of answers)",
    )


def test_a_review_context_graph_edge_keeps_its_label_beside_the_key(tmp_path: Path) -> None:
    _seed_settles(tmp_path)
    item = next(
        item
        for item in commands.op_review_memory(tmp_path, mode="attention")["items"]
        if item["path"].endswith("swarm.md")
    )

    context = commands.op_review_item_context(
        tmp_path, ref=item["ref"], expected_fingerprint=item["fingerprint"]
    )

    edge = next(edge for edge in context["graph"]["edges"] if edge["src_key"].startswith("block:"))
    assert (edge["relation_type"], edge["relation_label"]) == (
        "vault.settles",
        "settles (a kind of answers)",
    )
