"""Task 5.1 — end to end, through the tools, on a vault with no shipped anchors.

A vault whose owner never used `Products/` or `Systems/` at all: an agent
saves a conventions override naming its OWN resource folder and state field,
and a roles override adding a non-English cue with an evidence category --
both through `schema_memory`, never by writing the override file directly --
then a turn resolves through `activate_context`, the packet's current-state
statement is drawn from the custom field, and `generation` names both vault
registries by hash.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import activation_conventions as ac
from exomem import commands, context_roles, working_set_index, working_set_runtime


@pytest.fixture
def bare_vault(tmp_path: Path) -> Path:
    """A vault root with no `Products/`, no `Systems/`, and no override yet."""
    root = tmp_path / "vault"
    (root / "Knowledge Base" / "Equipment").mkdir(parents=True)
    context_roles.clear_cache()
    ac.clear_cache()
    return root


def _write_anchor_page(vault_root: Path) -> None:
    (vault_root / "Knowledge Base" / "Equipment" / "Field Recorder.md").write_text(
        "---\n"
        "type: note\n"
        "status: active\n"
        "stock: 3 rolls\n"
        "updated: 2026-09-05\n"
        "---\n\n"
        "# Field Recorder\n\n"
        "## Summary\n\n"
        "A field recorder used on winter surveys.\n",
        encoding="utf-8",
    )


def test_end_to_end_on_a_vault_with_no_products_or_systems_folder(
    tmp_path: Path, bare_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = bare_vault
    _write_anchor_page(root)
    assert not (root / "Knowledge Base" / "Products").exists()
    assert not (root / "Knowledge Base" / "Systems").exists()

    # An agent saves a conventions override: its own resource folder, its own
    # state field, and an added stopword -- through the tool entry point.
    conventions_before = ac.load_conventions(root).content_hash
    conventions_saved = commands.op_schema_memory(
        root,
        subject="activation-conventions",
        operation="save-conventions",
        proposal={
            "schema_version": 1,
            "anchors": {"resource": {"add_folders": ["Equipment"]}},
            "state": {"prefer_state_fields": ["stock"]},
            "stopwords": {"add": ["depotweit"]},
        },
        why="this vault keeps its resources under Equipment/ and tracks stock, not status",
        expected_hash=conventions_before,
    )
    assert conventions_saved["valid"] is True, conventions_saved
    assert not conventions_saved["findings"]

    # An agent saves a roles override: a non-English cue with an evidence
    # category, on a shipped role -- also through the tool entry point.
    roles_before = context_roles.load_roles(root).roles_hash
    roles_saved = commands.op_schema_memory(
        root,
        subject="context-roles",
        operation="save-roles",
        proposal={
            "schema_version": 1,
            "roles": {
                "active_plans": {
                    "evidence_cues": ["ich plane"],
                    "evidence_categories": ["action"],
                }
            },
        },
        why="the owner's turns are in German",
        expected_hash=roles_before,
    )
    assert roles_saved["valid"] is True, roles_saved
    assert not roles_saved["findings"]

    # Neither save touched anything but its own override file and the
    # history of what it replaced.
    assert context_roles.override_path(root).is_file()
    assert ac.override_path(root).is_file()
    schema = root / "Knowledge Base" / "_Schema"
    assert set(schema.iterdir()) == {
        context_roles.override_path(root),
        ac.override_path(root),
        schema / "history",
    }
    assert {path.parent.name for path in (schema / "history").rglob("*.yaml")} == {
        "context-roles",
        "activation-conventions",
    }

    effective_roles = context_roles.load_roles(root)
    effective_conventions = ac.load_conventions(root)
    assert effective_roles.source == "vault"
    assert effective_conventions.source == "vault"
    assert "ich plane" in effective_roles.roles["active_plans"].evidence_cues
    assert "action" in effective_roles.roles["active_plans"].evidence_categories
    assert "equipment" in effective_conventions.conventions.anchors["resource"].folders
    assert effective_conventions.conventions.state_fields[0] == "stock"

    # A turn resolves through the governed compiler.
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(root))
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(root).rebuild()

    packet = commands.op_activate_context(
        root, turn="Ich plane, den Field Recorder morgen zu benutzen."
    )

    assert not packet.get("abstained"), packet
    anchor_kinds = {row.get("kind") for row in packet.get("anchors") or ()}
    assert "resource" in anchor_kinds

    current_state = packet.get("current_state") or ()
    assert any("stock: 3 rolls" in (entry.get("statement") or "") for entry in current_state)

    generation = packet.get("generation") or {}
    assert generation.get("roles_source") == "vault"
    assert generation.get("roles_hash") == effective_roles.roles_hash
    assert generation.get("conventions_source") == "vault"
    assert generation.get("conventions_hash") == effective_conventions.conventions_hash


# --------------------------------------------------------------------------- #
# Task 5.4: the vault-owned registries change activation only. `find` and
# `ask_memory` answer every input byte for byte the same with a conventions
# override, a roles override, a learned referential cue and a learned name in
# place as without them.
# --------------------------------------------------------------------------- #


_RECALL_INPUTS = (
    "Cargo Sled constraints",
    "who coordinates the northern corridor",
    "continue",
    "weiter",
    "Schlitten",
    "depot ledger winter schedule",
)


def _recall_outputs(root: Path) -> list[str]:
    import json

    from exomem import find as find_module

    find_module.clear_cache()
    outputs = []
    for query in _RECALL_INPUTS:
        for result in (
            commands.op_find(root, query=query),
            commands.op_ask_memory(root, query=query),
        ):
            payload = dict(result) if isinstance(result, dict) else {"result": result}
            payload.pop("timings", None)
            payload.pop("due_state", None)
            outputs.append(json.dumps(payload, sort_keys=True, default=str))
    return outputs


def test_find_and_ask_memory_are_unchanged_by_the_activation_registries(vault: Path) -> None:
    from test_working_set_index import _seed_planning, _seed_structure

    from exomem import lexstore
    from exomem.vault import content_hash

    root = vault
    _seed_structure(root)
    _seed_planning(root)
    lexstore.ensure_fresh(root)
    context_roles.clear_cache()
    ac.clear_cache()
    sled = "Knowledge Base/Products/Cargo Sled.md"
    # The learned name is written first, so both runs read the same page bytes.
    commands.op_edit_memory(
        root,
        path=sled,
        why="the user calls it this",
        operation={
            "kind": "patch_frontmatter",
            "field": "learned_aliases",
            "value": ["Schlitten"],
            "expected_hash": content_hash((root / sled).read_text(encoding="utf-8")),
        },
    )
    lexstore.ensure_fresh(root)
    before = _recall_outputs(root)

    commands.op_schema_memory(
        root,
        subject="activation-conventions",
        operation="save-conventions",
        proposal={
            "schema_version": 1,
            "anchors": {"resource": {"add_folders": ["Notes/Insights"]}},
            "stopwords": {"add": ["northern"]},
            "referential": {"add_cues": ["weiter"]},
        },
        why="parity check",
        expected_hash=ac.load_conventions(root).content_hash,
    )
    commands.op_schema_memory(
        root,
        subject="context-roles",
        operation="save-roles",
        proposal={"schema_version": 1, "roles": {"constraints": {"add_cues": ["ceiling"]}}},
        why="parity check",
        expected_hash=context_roles.load_roles(root).roles_hash,
    )
    lexstore.ensure_fresh(root)

    assert _recall_outputs(root) == before
