"""The vocabulary registry contract, end to end (`add-vocabulary-registries`).

An agent promotes an entity type, sees it in bootstrap with a count, uses it,
and the owner reverts the promotion. The revert restores the registry bytes and
touches no page: the page that used the type is left as unregistered debt.
The cases after that one each pin a rule the end-to-end path cannot reach: a
stale hash, a restricted principal's save, and a hand-edited overlay.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from exomem import commands, entity_types, graph_sync, registry_history

_VENUE = {
    "label": "Venue",
    "guidance": "A stable place where recurring events happen.",
    "parent": "concept",
    "attributes": {"folder": "Venues"},
}


def _served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An initialized vault and an in-process MCP server over it."""
    from conftest import initialize_vault_state_offline

    from exomem import server as server_module
    from exomem.init import init_vault

    vault = tmp_path / "vault"
    init_vault(vault)
    initialize_vault_state_offline(vault, source="vocabulary registries")
    monkeypatch.setattr(server_module, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_RELEVANCE_CHECK", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    monkeypatch.setenv("EXOMEM_DISABLE_FILE_WATCHER", "1")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-lease"))
    mcp = server_module.build_server(require_auth=False)

    def call(tool: str, arguments: dict) -> dict:
        content = asyncio.run(
            mcp.call_tool(tool, arguments, run_middleware=True)
        ).structured_content
        # A tool whose output schema is not an object wraps its result.
        return content["result"] if set(content) == {"result"} else content

    return vault, call


def _entity_types_in_bootstrap(call) -> dict:
    served = call("bootstrap", {"section": "vocabulary"})
    return served["vocabulary"]["entity-types"]


def test_a_reverted_promotion_leaves_its_page_as_debt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call = _served(tmp_path, monkeypatch)

    # Promote a type through the generic contract.
    before = call("schema_memory", {"subject": "entity-types", "operation": "inspect"})
    assert "venue" not in {entry["key"] for entry in before["entries"]}
    promoted = call(
        "schema_memory",
        {
            "subject": "entity-types",
            "operation": "save",
            "proposal": {"upsert": {"venue": _VENUE}},
            "expected_hash": before["content_hash"],
            "why": "recurring event places need their own identity",
        },
    )
    # The write says what it promoted, under which parent, and how to revert it.
    [receipt] = promoted["vocabulary_receipt"]
    assert "entity-types: registered venue (parent concept)" in receipt
    assert 'operation="restore"' in receipt

    # Bootstrap lists it with a counted zero once the server's own graph
    # maintenance has converged on the new registry.
    assert graph_sync.drain_active_rebuilds(timeout=60)
    listed = _entity_types_in_bootstrap(call)
    assert listed["top"]["venue"] == 0

    # Use it on a page; the count follows the projection.
    created = call(
        "connect_memory",
        {
            "operation": "create-entity",
            "entity_type": "venue",
            "name": "Harbour Hall",
            "summary": "A concert hall by the harbour that hosts the spring series.",
        },
    )
    page = vault / created["path"]
    page_bytes = page.read_bytes()
    assert graph_sync.drain_active_rebuilds(timeout=60)
    assert _entity_types_in_bootstrap(call)["top"]["venue"] == 1

    # The owner restores the version the promotion replaced.
    history = call("schema_memory", {"subject": "entity-types", "operation": "history"})
    newest = history["versions"][0]
    assert newest["operation"] == "save"
    assert newest["why"] == "recurring event places need their own identity"
    restored = call(
        "schema_memory",
        {
            "subject": "entity-types",
            "operation": "restore",
            "version": newest["version"],
            "expected_hash": history["content_hash"],
            "why": "the owner keeps places as concepts",
        },
    )
    assert "removed venue" in restored["vocabulary_receipt"][0]
    assert entity_types.load_entity_types(vault).resolve("venue") is None

    # The page keeps its bytes and is reported as unregistered debt.
    assert page.read_bytes() == page_bytes
    audit = call("maintain_memory", {"mode": "audit", "categories": ["entity_type_unregistered"]})
    debt = [
        finding
        for finding in audit["findings"]
        if finding["category"] == "entity_type_unregistered"
    ]
    assert [finding["path"] for finding in debt] == [created["path"]]


def test_a_first_use_registration_is_announced_and_marked_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _vault, call = _served(tmp_path, monkeypatch)

    # A capture that names an unfamiliar kind registers it and says so once.
    captured = call(
        "capture_source",
        {
            "title": "Spring survey notes",
            "content": "Hand-written notes from the spring bird survey.",
            "source_kind": "field-notebook",
        },
    )
    [receipt] = captured["vocabulary_receipt"]
    assert "source-kinds: registered field-notebook on first use" in receipt
    assert 'operation="restore"' in receipt

    # Another agent's next bootstrap marks it new.
    served = call("bootstrap", {"section": "vocabulary"})
    assert "field-notebook" in served["vocabulary"]["source-kinds"]["new"]
    assert served["vocabulary"]["new_since"]


@pytest.fixture
def owner_scope():
    from exomem.governance.principal import library_scope

    with library_scope():
        yield


@pytest.mark.usefixtures("owner_scope")
def test_a_stale_hash_refuses_the_second_save(vault: Path) -> None:
    current = commands.op_schema_memory(vault, subject="entity-types", operation="inspect")
    commands.op_schema_memory(
        vault,
        subject="entity-types",
        operation="save",
        proposal={"upsert": {"venue": _VENUE}},
        expected_hash=current["content_hash"],
        why="first agent",
    )
    overlay = entity_types.extension_registry_path(vault)
    first = overlay.read_bytes()

    with pytest.raises(ValueError, match="STALE"):
        commands.op_schema_memory(
            vault,
            subject="entity-types",
            operation="save",
            proposal={
                "upsert": {
                    "arena": {**_VENUE, "label": "Arena", "attributes": {"folder": "Arenas"}}
                }
            },
            expected_hash=current["content_hash"],
            why="second agent, same read",
        )
    assert overlay.read_bytes() == first

    # A fresh proposal for the same idea sees the first agent's entry.
    proposed = commands.op_schema_memory(
        vault,
        subject="entity-types",
        operation="propose",
        proposal={"upsert": {"venues": {**_VENUE, "attributes": {"folder": "Venue Places"}}}},
    )
    assert "venue" in {item["key"] for item in proposed["collisions"]}


def _govern(vault: Path, *, scope_path: str = "Notes/Withheld/**") -> None:
    """A governed policy: one withheld folder for the `external` audience."""
    from exomem.governance import egress

    governance = vault / "Knowledge Base" / "_Governance"
    (governance / "scopes").mkdir(parents=True, exist_ok=True)
    (governance / "rules").mkdir(parents=True, exist_ok=True)
    (governance / "scopes" / "withheld.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\nname: Withheld\n"
        f'paths: ["{scope_path}"]\n',
        encoding="utf-8",
    )
    (governance / "rules" / "withheld-external.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\n'
        f"audience: external\nceiling: {egress.LEVEL_NONE}\n",
        encoding="utf-8",
    )


def _reset_governance() -> None:
    from exomem import find as find_module
    from exomem.governance import egress, membership, policy

    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()
    find_module.clear_cache()


@pytest.mark.parametrize("governed", [False, True])
def test_a_restricted_save_becomes_a_pending_proposal(vault: Path, governed: bool) -> None:
    from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope

    if governed:
        _govern(vault)
    _reset_governance()
    before = entity_types.load_entity_types(vault).extension_hash
    with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
        pending = commands.op_schema_memory(
            vault,
            subject="entity-types",
            operation="save",
            proposal={"upsert": {"venue": _VENUE}},
            expected_hash=before,
            why="the delegate wants a place type",
        )
        # Counts and reasons are the owner's.
        inspected = commands.op_schema_memory(vault, subject="entity-types", operation="inspect")
    assert pending["state"] == "pending_review"
    assert entity_types.load_entity_types(vault).extension_hash == before
    assert entity_types.load_entity_types(vault).resolve("venue") is None
    assert inspected["counts"] == "unavailable"
    assert inspected["reason"] == "audience_restricted"

    _reset_governance()
    with request_scope(owner_principal(surface="mcp")):
        queue = commands.op_review_memory(vault, mode="vocabulary")
    items = [item for item in queue["items"] if item["ref"] == pending["item_ref"]]
    assert len(items) == 1
    assert "the delegate wants a place type" in items[0]["question"]
    assert '"venue"' in items[0]["question"]


@pytest.mark.usefixtures("owner_scope")
def test_a_hand_edited_overlay_is_read_with_findings_and_kept_on_the_next_save(
    vault: Path,
) -> None:
    overlay = entity_types.extension_registry_path(vault)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "entity_types": {
                    "site": {
                        "folder": "Sites",
                        "label": "Site",
                        "aliases": [],
                        "capture_guidance": "A stable physical site identity.",
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    assert entity_types.load_entity_types(vault).resolve("site") is not None

    # The owner adds an invalid entry by hand; the loaded registry follows the
    # edit without a restart, keeps the valid entry and reports the other.
    hand_edited = overlay.read_text(encoding="utf-8") + (
        "  person-two:\n    folder: People\n    label: Person\n"
    )
    overlay.write_text(hand_edited, encoding="utf-8")
    inspected = commands.op_schema_memory(vault, subject="entity-types", operation="inspect")
    assert "site" in {entry["key"] for entry in inspected["entries"]}
    assert "person-two" not in {entry["key"] for entry in inspected["entries"]}
    assert any(finding.get("entity_type") == "person-two" for finding in inspected["findings"])

    # The next governed save snapshots the hand-edited bytes exactly.
    commands.op_schema_memory(
        vault,
        subject="entity-types",
        operation="save",
        proposal={"upsert": {"venue": _VENUE}},
        expected_hash=inspected["content_hash"],
        why="add a place type",
    )
    newest = registry_history.versions(vault, stem="entity-types")[0]
    assert (
        registry_history.read_version(vault, stem="entity-types", version=newest["version"])
        == hand_edited
    )


def test_effective_digest_ignores_entry_origin_but_tracks_meaning() -> None:
    """Moving an unchanged entry into an overlay must not invalidate vocabulary consumers."""
    from dataclasses import replace

    from exomem.vocabulary.registry import Entry, effective_digest

    shipped = Entry(key="venue", label="Venue", origin="pack")
    overlaid = replace(shipped, origin="vault")
    assert effective_digest("entity-types", {"venue": shipped}) == effective_digest(
        "entity-types", {"venue": overlaid}
    )
    assert effective_digest("entity-types", {"venue": shipped}) != effective_digest(
        "entity-types", {"venue": replace(overlaid, label="Event venue")}
    )


def test_schema_watcher_invalidates_a_same_fingerprint_hand_edit(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An overlay edit hidden by file metadata must reach the next registry reader."""
    from exomem import file_watcher
    from exomem.vocabulary import registry

    overlay = entity_types.extension_registry_path(vault)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(
        {
            "schema_version": 1,
            "entity_types": {
                "site": {
                    "folder": "Sites",
                    "label": "Site",
                    "aliases": [],
                    "capture_guidance": "A stable physical site identity.",
                }
            },
        }
    )
    overlay.write_text(text, encoding="utf-8")
    # Force the fingerprint collision that the event boundary must recover from.
    fingerprint = registry._stat_key(overlay)
    monkeypatch.setattr(registry, "_stat_key", lambda path: fingerprint)
    assert entity_types.load_entity_types(vault).resolve("site").label == "Site"
    overlay.write_text(text.replace("label: Site", "label: Spot"), encoding="utf-8")
    assert entity_types.load_entity_types(vault).resolve("site").label == "Site"

    file_watcher.FileWatcher(vault)._record(overlay, deleted=False)

    assert entity_types.load_entity_types(vault).resolve("site").label == "Spot"


@pytest.mark.parametrize("bound", [False, True])
def test_absent_or_unresolved_principal_cannot_save(vault: Path, bound: bool) -> None:
    from contextlib import nullcontext

    from exomem.governance.principal import most_restrictive_principal, request_scope

    scope = request_scope(most_restrictive_principal(surface="mcp")) if bound else nullcontext()
    with scope, pytest.raises(ValueError, match="UNRESOLVED_PRINCIPAL"):
        commands.op_schema_memory(
            vault,
            subject="entity-types",
            operation="save",
            proposal={"upsert": {"venue": _VENUE}},
            expected_hash="none",
            why="no authority",
        )
    assert not entity_types.extension_registry_path(vault).exists()


def test_hosted_raw_exemption_does_not_grant_owner_writes(vault: Path) -> None:
    from exomem.governance import raw_protection
    from exomem.governance.principal import (
        HOSTED_GATEWAY_ISSUER_FAMILY,
        RequestPrincipal,
        library_scope,
        request_scope,
    )

    who = RequestPrincipal(
        audience_id="tenant",
        surface="hosted",
        issuer_family=HOSTED_GATEWAY_ISSUER_FAMILY,
    )
    assert not raw_protection.applies_to(who)
    with request_scope(who), library_scope():
        saved = commands.op_schema_memory(
            vault,
            subject="entity-types",
            operation="save",
            proposal={"upsert": {"venue": _VENUE}},
            expected_hash="none",
            why="tenant proposal",
        )
        review = commands.op_review_memory(vault, mode="vocabulary")
        assert saved["item_ref"] not in {item["ref"] for item in review["items"]}
    assert saved["state"] == "pending_review"
    assert entity_types.load_entity_types(vault).resolve("venue") is None


def test_withheld_registry_is_not_loaded_for_private_dependent_operations(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import egress
    from exomem.governance.principal import RequestPrincipal, request_scope
    from exomem.vocabulary import contract, registry

    monkeypatch.setattr(egress, "release_level_for_path_only", lambda *a, **kw: egress.LEVEL_NONE)

    def private_read(*args, **kwargs):
        pytest.fail("private registry producer ran before admission")

    monkeypatch.setattr(registry, "load", private_read)
    with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
        for operation in ("inspect", "propose", "save", "history"):
            result = commands.op_schema_memory(
                vault,
                subject="entity-types",
                operation=operation,
                proposal={"upsert": {"venue": _VENUE}}
                if operation in {"save", "propose"}
                else None,
                expected_hash="none" if operation == "save" else None,
                why="private-dependent" if operation == "save" else None,
            )
            assert result == {
                "subject": "entity-types",
                "available": False,
                "reason": "audience_restricted",
            }
        assert contract.inspect(vault, entity_types.SPEC)["available"] is False


def test_limited_owner_save_uses_admitted_overlay_without_approval(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from exomem.governance import egress
    from exomem.governance.principal import owner_principal, request_scope

    monkeypatch.setattr(
        egress,
        "owner_only_aggregate",
        lambda *a, **kw: {
            "available": False,
            "reason": "audience_restricted",
        },
    )
    monkeypatch.setattr(egress, "release_level_for_path_only", lambda *a, **kw: egress.LEVEL_FULL)
    with request_scope(owner_principal(surface="mcp")):
        result = commands.op_schema_memory(
            vault,
            subject="entity-types",
            operation="save",
            proposal={"upsert": {"venue": _VENUE}},
            expected_hash="none",
            why="admitted owner",
        )
        inspected = commands.op_schema_memory(vault, subject="entity-types", operation="inspect")
    assert result["saved"]["content_hash"] != "none"
    assert inspected["counts"] == "unavailable"
    assert entity_types.load_entity_types(vault).resolve("venue") is not None


@pytest.mark.usefixtures("owner_scope")
@pytest.mark.parametrize(
    ("subject", "key", "entry"),
    [
        ("relations", "vault.guides", {"description": "Guides application", "parent": "supports"}),
        ("source-kinds", "field-notebook", {"label": "Field notebook"}),
        ("domains", "ornithology", {"label": "Ornithology"}),
        ("categories", "field_observation", {"description": "An observation made in the field"}),
    ],
)
def test_each_overlay_adapter_restores_its_legacy_grammar(vault, subject, key, entry):
    """Every adapter must round-trip through history without changing its legacy file grammar."""
    before = commands.op_schema_memory(vault, subject=subject, operation="inspect")
    proposed = commands.op_schema_memory(
        vault,
        subject=subject,
        operation="propose",
        proposal={"upsert": {key: entry}},
    )
    assert proposed["valid"], proposed
    result = commands.op_schema_memory(
        vault,
        subject=subject,
        operation="save",
        proposal={"upsert": {key: entry}},
        expected_hash=before["content_hash"],
        why="adapter round trip",
    )
    saved = result["saved"]
    promoted_bytes = (vault / saved["path"]).read_bytes()
    history = commands.op_schema_memory(vault, subject=subject, operation="history")
    restored = commands.op_schema_memory(
        vault,
        subject=subject,
        operation="restore",
        version=history["versions"][0]["version"],
        expected_hash=saved["content_hash"],
        why="restore the previous vocabulary",
    )
    assert restored["removed_keys"] == [key]
    undone = commands.op_schema_memory(
        vault,
        subject=subject,
        operation="restore",
        version=restored["saved"]["history"]["version"],
        expected_hash=restored["saved"]["content_hash"],
        why="undo the restore",
    )
    assert (vault / undone["saved"]["path"]).read_bytes() == promoted_bytes


def test_activated_v2_classifies_registry_history_as_derived_auxiliaries(tmp_path, monkeypatch):
    """A registry save with history must retain the existing v2 grant requirement."""
    from test_vocabulary_authority import (
        NOW,
        _activate,
        _grant,
        _principal,
        _store,
        install_unit_session_boundary,
    )

    from exomem import reserved_paths, vocabulary_authority, vocabulary_gate
    from exomem.governance.principal import request_scope

    install_unit_session_boundary(monkeypatch)
    # The existing authority fixture supplies session validity without a SQL session store.
    monkeypatch.setattr(
        "exomem.governance.authorization_session_authority.active_session_purpose",
        lambda *args, **kwargs: None,
    )
    root = tmp_path / "vault"
    root.mkdir()
    authority = _store(root)
    principal = _principal()
    _activate(authority, principal)
    _grant(
        authority,
        principal,
        actions=("entity_type.add",),
        scope=vocabulary_authority.AuthorityScope.vault_wide(),
        expires_at=NOW + 300,
    )
    monkeypatch.setattr(vocabulary_authority, "VocabularyAuthority", lambda _: authority)
    with request_scope(principal), reserved_paths._owner_authority_scope("schema_memory"):
        with vocabulary_gate.operation_context(
            root,
            idempotency_key="registry-history",
            command_digest="a" * 64,
            receipt_id="registry-history",
            principal=principal,
        ) as context:
            saved = commands.op_schema_memory(
                root,
                subject="entity-types",
                operation="save",
                proposal={"upsert": {"venue": _VENUE}},
                expected_hash="none",
                why="v2 grant",
            )
            receipt = vocabulary_gate.attach_evidence({"state": "committed", **saved}, context)
    assert saved["saved"]["history"]["version"]
    assert [
        item["action"]
        for use in receipt["additive_authority"]["uses"]
        for item in use["authorities"]
    ] == ["entity_type.add"]
    assert registry_history.versions(root, stem="entity-types")[0]["why"] == "v2 grant"


@pytest.mark.usefixtures("owner_scope")
def test_taxonomy_restore_reports_both_axes_it_removes(vault):
    kind = commands.op_schema_memory(
        vault,
        subject="source-kinds",
        operation="save",
        proposal={"upsert": {"field-notebook": {"label": "Field notebook"}}},
        expected_hash="none",
        why="new kind",
    )["saved"]
    domain = commands.op_schema_memory(
        vault,
        subject="domains",
        operation="save",
        proposal={"upsert": {"ornithology": {"label": "Ornithology"}}},
        expected_hash=kind["content_hash"],
        why="new domain",
    )["saved"]
    restored = commands.op_schema_memory(
        vault,
        subject="source-kinds",
        operation="restore",
        version=kind["history"]["version"],
        expected_hash=domain["content_hash"],
        why="restore the shared overlay",
    )
    assert restored["removed_by_registry"] == {
        "source-kinds": ["field-notebook"],
        "domains": ["ornithology"],
    }


def test_legacy_relation_inference_save_queues_a_hosted_nonowner(vault):
    """The legacy save flag must not bypass the owner rule on an exempt hosted cell."""
    from exomem import relation_registry
    from exomem.governance.principal import (
        HOSTED_GATEWAY_ISSUER_FAMILY,
        RequestPrincipal,
        request_scope,
    )

    who = RequestPrincipal(
        audience_id="tenant",
        surface="hosted",
        issuer_family=HOSTED_GATEWAY_ISSUER_FAMILY,
    )
    with request_scope(who):
        result = commands.op_schema_memory(
            vault,
            subject="relations",
            operation="infer",
            save=True,
            proposal={
                "schema_version": relation_registry.EXTENSION_SCHEMA_VERSION,
                "extensions": {
                    "vault.guides": {
                        "parent": "supports",
                        "description": "Guides application",
                    }
                },
            },
            expected_hash="none",
            why="legacy nonowner proposal",
        )
    assert result["state"] == "pending_review"
    assert "vault.guides" not in relation_registry.load_registry(vault).extensions


def test_file_policy_withholds_private_registry_definitions(vault):
    """A real path policy must withhold keys, hashes and collisions together."""
    from exomem.governance.principal import RequestPrincipal, request_scope

    overlay = entity_types.extension_registry_path(vault)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "entity_types": {
                    "venue": {
                        "folder": "Venues",
                        "label": "Private Venue",
                        "aliases": [],
                        "capture_guidance": "A private registry fixture",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    _govern(vault, scope_path="_Schema/entity-types.yaml")
    _reset_governance()
    with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
        inspected = commands.op_schema_memory(vault, subject="entity-types", operation="inspect")
        proposed = commands.op_schema_memory(
            vault,
            subject="entity-types",
            operation="propose",
            proposal={"upsert": {"venue": _VENUE}},
        )
    assert (
        inspected
        == proposed
        == {
            "subject": "entity-types",
            "available": False,
            "reason": "audience_restricted",
        }
    )
