"""The promotion classes hold at their writers (`promote-referents-without-nudges` 1.2).

The served `promotion_classes` map tells an agent what it may do without asking.
These cases pin the writers to the same rule where no other test does:

- a resolved non-owner's relation save through `save-relations` waits as a pending
  item for the owner, so it is only a structural suggestion;
- a limited owner's save that needs withheld registry definitions reports
  `unavailable`, never a success;
- a v2-activated vault refuses an entity creation that no grant covers;
- an upgraded v1 vault stays v1, and the owner's registry saves commit at once.

Other tests already pin the owner's leaves: the relation-extension save, the
entity creation and the typed edge through `edit_memory` commit with no
confirmation argument in
`test_operator_site_cohort.py::test_a_missing_meaning_goes_through_the_governed_relation_extension`.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from exomem import commands, entity_types, relation_registry, vocabulary_authority, writer_lease
from exomem.governance.principal import (
    RequestPrincipal,
    library_scope,
    owner_principal,
    request_scope,
)

_PLACE_TYPE = {
    "label": "Venue",
    "guidance": "A stable place where recurring events happen.",
    "parent": "concept",
    "attributes": {"folder": "Venues"},
}
_GUIDES = {"parent": "supports", "description": "Guides application", "direction": "directed"}


def _invoke(root: Path, tool: str, **arguments) -> dict:
    command = next(item for item in commands.PRODUCT_COMMANDS if item.name == tool)
    return writer_lease.invoke_command(command, root, **arguments)


def test_a_resolved_non_owner_relation_save_stays_a_pending_item(vault: Path) -> None:
    before = relation_registry.load_registry(vault).extension_hash
    with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
        result = commands.op_schema_memory(
            vault,
            subject="relations",
            operation="save-relations",
            proposal={"upsert": {"vault.guides": _GUIDES}},
            expected_hash=before,
            why="a delegate proposes a durable meaning",
        )

    assert result["state"] == "pending_review"
    assert relation_registry.load_registry(vault).extension_hash == before
    with request_scope(owner_principal(surface="mcp")):
        queue = commands.op_review_memory(vault, mode="vocabulary")
    assert result["item_ref"] in {item["ref"] for item in queue["items"]}


@pytest.mark.parametrize(
    ("subject", "operation", "proposal"),
    [
        # The generic registry contract and the relation writer's own handler gate
        # separately, so each is pinned.
        ("entity-types", "save", {"upsert": {"venue": _PLACE_TYPE}}),
        ("relations", "save-relations", {"upsert": {"vault.guides": _GUIDES}}),
    ],
)
def test_a_limited_owner_save_that_needs_withheld_definitions_is_unavailable(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    subject: str,
    operation: str,
    proposal: dict,
) -> None:
    from exomem.governance import egress

    restricted = {"available": False, "reason": "audience_restricted"}
    monkeypatch.setattr(egress, "owner_only_aggregate", lambda *a, **kw: restricted)
    monkeypatch.setattr(egress, "unrestricted_content_access", lambda *a, **kw: False)
    monkeypatch.setattr(egress, "release_level_for_path_only", lambda *a, **kw: egress.LEVEL_NONE)
    types_before = entity_types.extension_registry_path(vault).exists()
    relations_before = relation_registry.extension_registry_path(vault).exists()

    with request_scope(owner_principal(surface="mcp")):
        result = commands.op_schema_memory(
            vault,
            subject=subject,
            operation=operation,
            proposal=proposal,
            expected_hash="none",
            why="the owner promotes a meaning",
        )

    assert result == {"subject": subject, "available": False, "reason": "audience_restricted"}
    assert entity_types.extension_registry_path(vault).exists() is types_before
    assert relation_registry.extension_registry_path(vault).exists() is relations_before


def test_a_v2_vault_without_a_grant_refuses_an_entity_creation(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_vocabulary_authority import (
        _activate,
        _principal,
        _store,
        install_unit_session_boundary,
    )

    from exomem import (
        activation_manifest,
        epistemic_graph,
        link,
        vocabulary_admission,
        vocabulary_gate,
    )

    install_unit_session_boundary(monkeypatch)
    with library_scope():
        activation_manifest.ensure_manifest(vault)
    warm_managed_cell(vault)
    with library_scope():
        epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    assert vocabulary_admission.runtime_admission(vault).status.mode == "v1"
    authority = _store(vault)
    principal = _principal()
    _activate(authority, principal)
    monkeypatch.setattr(vocabulary_authority, "VocabularyAuthority", lambda _: authority)
    pages_before = sorted((vault / "Knowledge Base").rglob("*.md"))

    with (
        request_scope(principal),
        library_scope(),
        vocabulary_gate.operation_context(
            vault,
            idempotency_key="entity-without-grant",
            command_digest="c" * 64,
            receipt_id="entity-receipt",
            principal=principal,
        ),
        pytest.raises(vocabulary_authority.VocabularyAuthorityDenied) as denied,
    ):
        link.link(
            vault,
            entity_type="concept",
            name="Bounded change",
            summary="A concept no grant covers.",
            today=dt.date(2026, 9, 7),
        )

    request_id = denied.value.details["vocabulary_request_id"]
    operation = authority.inspect_request_for_owner(request_id, principal=principal)
    assert {effect.action for effect in operation.effects} == {"entity.create"}
    assert sorted((vault / "Knowledge Base").rglob("*.md")) == pages_before


def test_an_upgraded_v1_vault_stays_v1_and_owner_registry_saves_commit(vault: Path) -> None:
    """The `vault` fixture is the upgrade path: a v1 vault through the offline state
    migration. No upgrade, migration or default activates v2 or adds an approval."""
    assert vocabulary_authority.VocabularyAuthority(vault).runtime_status().mode == "v1"

    with library_scope():
        saved_type = _invoke(
            vault,
            "schema_memory",
            subject="entity-types",
            operation="save",
            proposal={"upsert": {"venue": _PLACE_TYPE}},
            expected_hash="none",
            why="no registered type fits a recurring place",
        )
        saved_relation = _invoke(
            vault,
            "schema_memory",
            subject="relations",
            operation="save-relations",
            proposal={"upsert": {"vault.guides": _GUIDES}},
            expected_hash=relation_registry.load_registry(vault).extension_hash,
            why="no core relation says that one note guides another",
        )

    assert saved_type["state"] == "committed"
    assert entity_types.load_entity_types(vault).resolve("venue") is not None
    assert saved_relation["state"] == "committed"
    assert relation_registry.load_registry(vault).definition("vault.guides") is not None
    assert vocabulary_authority.VocabularyAuthority(vault).runtime_status().mode == "v1"
