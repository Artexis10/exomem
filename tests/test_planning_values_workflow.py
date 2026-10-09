"""A vault-added Planning status, from its save to its revert, through the tools.

The owner saves two statuses through `schema_memory`; `plan_memory` accepts
them, due-state and the audit read their planning class, and the archive rule
admits only the settled one. Restoring the registry removes both, yet the
items that use them still read and still take edits that leave the status
alone. Only a write that introduces a removed status refuses. A saved horizon
that YAML would read as a boolean still gets its scaffolded view.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from lifecycle_fixtures import (
    PLANNING_ID,
    PLANNING_PATH,
    initiative_ref,
    planning_manifest,
    report_event,
    seed_vault,
)
from test_vocabulary_registries import _govern, _reset_governance, _served

from exomem import audit as audit_module
from exomem import due_state as due_state_module
from exomem import structured_collections as collections
from exomem.cli_ops import OpError

FAMILY = "unreflected_outcomes"


def _open_titles(vault: Path) -> set[str]:
    report = audit_module.audit(vault, categories=[FAMILY])
    return {str((finding.meta or {}).get("plan_title")) for finding in report.findings}


def _guards(call, title: str) -> dict:
    query = call(
        "plan_memory", {"action": "query", "collection": PLANNING_PATH, "lifecycle": "all"}
    )
    row = next(row for row in query["rows"] if row["title"] == title)
    return {
        "collection": PLANNING_PATH,
        "plan_id": row["plan_id"],
        "expected_container_hash": query["snapshot"],
        "expected_item_version": row["item_version"],
    }


def _add(call, vault: Path, title: str, **fields) -> dict:
    item = {
        "title": title,
        "status": "planned",
        "commitment": "committed",
        "horizon": "week",
        "parent": initiative_ref(vault),
        **fields,
    }
    return call(
        "plan_memory",
        {"action": "add", "collection": PLANNING_PATH, "item": item, "why": f"queue {title}"},
    )


def _triage(call, title: str, status: str) -> dict:
    return call(
        "plan_memory",
        {
            "action": "triage",
            **_guards(call, title),
            "transition": {"status": status},
            "why": status,
        },
    )


def _archive(call, title: str) -> dict:
    return call(
        "plan_memory",
        {
            "action": "update",
            **_guards(call, title),
            "changes": {"lifecycle": "archived"},
            "why": "close it out",
        },
    )


def test_a_vault_status_is_used_classified_archived_and_reverted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    seed_vault(vault)
    due_state_module.reset_emission_state()
    due_state_module.reconcile(vault)

    before = call("schema_memory", {"subject": "planning-values", "operation": "inspect"})
    saved = call(
        "schema_memory",
        {
            "subject": "planning-values",
            "operation": "save",
            "proposal": {
                "upsert": {
                    "status.waiting": {"attributes": {"class": "open"}},
                    "status.parked": {"attributes": {"class": "dropped"}},
                    "horizon.off": {},
                }
            },
            "expected_hash": before["content_hash"],
            "why": "work waits on others, some is shelved, and some is off the plan",
        },
    )
    receipt = " ".join(saved["vocabulary_receipt"])
    assert "registered status.waiting" in receipt
    assert "registered status.parked" in receipt

    # A new scaffolded collection gets a view for the saved horizon.
    later = "Knowledge Base/Planning/Later/_collection.md"
    created = call(
        "plan_memory",
        {
            "action": "create",
            "manifest_path": later,
            "manifest_text": planning_manifest(
                collection_id="7c1d2e3f-4a5b-4c6d-8e7f-90a1b2c3d4e5"
            ),
            "why": "plans for later",
        },
    )
    assert created.get("success") is not False, created
    views = call("plan_memory", {"action": "inspect", "collection": later})["saved_views"]
    assert "off" in {view["name"] for view in views}

    # plan_memory accepts the vault status; due-state and the audit read it as open.
    assert _add(call, vault, "Batch 1").get("success") is not False
    assert _triage(call, "Batch 1", "waiting").get("success") is not False
    reported = report_event(vault, "Batch 1")
    assert reported["due_state"]["categories"] == {FAMILY: 1}
    assert _open_titles(vault) == {"Batch 1"}

    # An open status cannot be archived.
    refused = _archive(call, "Batch 1")
    assert refused["success"] is False
    assert refused["error"]["code"] == "INVALID_PLAN"

    # A dropped status settles the item and admits the archive.
    assert _triage(call, "Batch 1", "parked").get("success") is not False
    assert _open_titles(vault) == set()
    assert due_state_module.served_entries(vault) == []
    assert _archive(call, "Batch 1").get("success") is not False

    assert _add(call, vault, "Batch 2", status="waiting").get("success") is not False

    # The owner reverts the save.
    history = call("schema_memory", {"subject": "planning-values", "operation": "history"})
    restored = call(
        "schema_memory",
        {
            "subject": "planning-values",
            "operation": "restore",
            "version": history["versions"][0]["version"],
            "expected_hash": history["content_hash"],
            "why": "the owner keeps the shipped statuses",
        },
    )
    assert "removed horizon.off, status.parked, status.waiting" in restored["vocabulary_receipt"][0]

    # Stored items keep their removed statuses and still read.
    rows = call("plan_memory", {"action": "query", "collection": PLANNING_PATH, "lifecycle": "all"})
    statuses = {row["title"]: (row["status"], row["lifecycle"]) for row in rows["rows"]}
    assert statuses["Batch 1"] == ("parked", "archived")
    assert statuses["Batch 2"] == ("waiting", "active")
    inspected = call("plan_memory", {"action": "inspect", "collection": PLANNING_PATH})
    assert "UNREGISTERED_PLAN_VALUE" in {item["code"] for item in inspected["diagnostics"]}

    # An edit that leaves the status alone still writes; a new use refuses.
    renamed = call(
        "plan_memory",
        {
            "action": "update",
            **_guards(call, "Batch 2"),
            "changes": {"title": "Batch 2b"},
            "why": "clarify the title",
        },
    )
    assert renamed.get("success") is not False, renamed
    added = _add(call, vault, "Batch 3", status="waiting")
    assert added["success"] is False
    assert added["error"]["code"] == "INVALID_PLAN"


def _save(call, upsert: dict, why: str) -> dict:
    before = call("schema_memory", {"subject": "planning-values", "operation": "inspect"})
    return call(
        "schema_memory",
        {
            "subject": "planning-values",
            "operation": "save",
            "proposal": {"upsert": upsert},
            "expected_hash": before["content_hash"],
            "why": why,
        },
    )


def test_a_kind_saved_again_with_other_parents_keeps_stored_items_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance.principal import RequestPrincipal, request_scope
    from exomem.plan_memory import plan_memory

    vault, call = _served(tmp_path, monkeypatch)
    seed_vault(vault)
    _save(call, {"kind.epic": {"attributes": {"parents": ["outcome"]}}}, "epics sit under outcomes")
    query = call("plan_memory", {"action": "query", "collection": PLANNING_PATH})
    outcome = next(row["plan_id"] for row in query["rows"] if row["kind"] == "outcome")
    added = _add(
        call, vault, "Epic", kind="epic", parent=collections.plan_ref(PLANNING_ID, outcome)
    )
    assert added.get("success") is not False, added

    # The owner removes the kind, then registers it again under other parents.
    history = call("schema_memory", {"subject": "planning-values", "operation": "history"})
    call(
        "schema_memory",
        {
            "subject": "planning-values",
            "operation": "restore",
            "version": history["versions"][0]["version"],
            "expected_hash": history["content_hash"],
            "why": "drop epics",
        },
    )
    _save(call, {"kind.epic": {"attributes": {"parents": ["initiative"]}}}, "epics again")

    # The epic stored under the outcome still reads.
    rows = call("plan_memory", {"action": "query", "collection": PLANNING_PATH, "lifecycle": "all"})
    assert "Epic" in {row["title"] for row in rows["rows"]}
    inspected = call("plan_memory", {"action": "inspect", "collection": PLANNING_PATH})
    assert "INVALID_PLAN_RELATION" not in {item["code"] for item in inspected["diagnostics"]}

    # A caller who cannot read the vault's definitions cannot re-parent the epic.
    guards = _guards(call, "Epic")
    _govern(vault, scope_path="_Schema/planning-values.yaml")
    _reset_governance()
    with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
        with pytest.raises(OpError) as refused:
            plan_memory(
                vault,
                "update",
                **guards,
                changes={"parent": initiative_ref(vault)},
                why="move the epic",
            )
    assert refused.value.code == "PLANNING_VALUES_UNAVAILABLE"
    assert _guards(call, "Epic") == guards


def test_a_restricted_write_does_not_reopen_an_item_a_vault_status_settled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope

    vault, call = _served(tmp_path, monkeypatch)
    seed_vault(vault)
    _save(call, {"status.shipped": {"attributes": {"class": "done"}}}, "shipped work is done")
    assert _add(call, vault, "Batch 1").get("success") is not False
    assert _triage(call, "Batch 1", "shipped").get("success") is not False
    due_state_module.reset_emission_state()
    due_state_module.reconcile(vault)

    # A caller who cannot read the vault's definitions reports an outcome on it.
    _govern(vault, scope_path="_Schema/planning-values.yaml")
    _reset_governance()
    with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
        report_event(vault, "Batch 1")

    # The stored projection still holds the owner's classification: settled.
    with request_scope(owner_principal(surface="mcp")):
        assert due_state_module.served_entries(vault) == []
