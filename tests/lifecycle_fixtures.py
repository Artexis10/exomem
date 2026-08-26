"""A generic bound Planning/Records pair, plus one deliberately unbound twin.

Shared by the `unreflected_outcomes` unit tests and the lifecycle-routing
journey. The vocabulary is batch production — queued deliverables and the events
that report them produced — because the binding under test is authored, not
domain-specific: nothing here knows what a deliverable is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

PLANNING_ID = "3f5b6d21-9c4e-4a77-9c2f-6d0b1e2a5c48"
RECORDS_ID = "5a2c7e10-4b8d-4f31-8a90-1c7e4d9b6f22"
UNBOUND_ID = "8e4f1a63-2d57-4c99-b0e1-7a3d5f8c4b16"

PLANNING_PATH = "Knowledge Base/Planning/Delivery/_collection.md"
RECORDS_PATH = "Knowledge Base/Records/Deliveries/_collection.md"
UNBOUND_PATH = "Knowledge Base/Records/Unbound/_collection.md"


def planning_manifest(*, natural_key: str = "[title]", collection_id: str = "") -> str:
    """The Delivery plan manifest. `natural_key` is a parameter for exactly one
    reason: a collection keyed on a field Planning triage CAN reach behaves
    differently from one keyed on `title`, and the docs now say so."""
    exomem_id = collection_id or PLANNING_ID
    return f"""---
type: collection
exomem_id: {exomem_id}
title: Delivery plan
semantic_profile: planning
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Items
  format_version: 1
item_schema:
  natural_key: {natural_key}
  fields:
    title:
      type: string
      required: true
    kind:
      type: string
    status:
      type: string
    lifecycle:
      type: string
    priority:
      type: string
    commitment:
      type: string
    horizon:
      type: string
    health:
      type: string
    area:
      type: string
    parent:
      type: string
---

Intended deliverables.
"""


def records_manifest(*, collection_id: str = RECORDS_ID, join: bool = True) -> str:
    binding = (
        f"""links:
  plans:
    - reference: exomem://memory/{PLANNING_ID}
      query: {{limit: 50}}
      join:
        title: title
"""
        if join
        else ""
    )
    return f"""---
type: collection
exomem_id: {collection_id}
title: Delivery events
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Events
  format_version: 1
item_schema:
  natural_key: [occurred_on, title, event_type]
  fields:
    occurred_on:
      type: date
      required: true
    title:
      type: string
      required: true
    event_type:
      type: string
      required: true
{binding}---

Observed delivery events.
"""


def seed_vault(vault_root: Path, *, unbound: bool = False) -> dict[str, str]:
    """Create the activity log, the Planning collection and the Records twin(s).

    The outcome/initiative pair is not decoration: Planning refuses a committed
    active work-item with no parent initiative, so the chain is what "committed
    work" means in this product.
    """
    from exomem import planning, records

    kb = vault_root / "Knowledge Base"
    kb.mkdir(parents=True, exist_ok=True)
    (kb / "log.md").write_text("# Log\n", encoding="utf-8")
    planning.create_collection(
        vault_root, PLANNING_PATH, planning_manifest(), why="file intended deliverables"
    )
    initiative = _seed_parent_chain(vault_root)
    records.create_collection(
        vault_root, RECORDS_PATH, records_manifest(), why="log delivery events"
    )
    if unbound:
        records.create_collection(
            vault_root,
            UNBOUND_PATH,
            records_manifest(collection_id=UNBOUND_ID, join=False),
            why="log delivery events without a binding",
        )
    return {
        "planning": PLANNING_PATH,
        "records": RECORDS_PATH,
        "unbound": UNBOUND_PATH,
        "initiative": collections_plan_ref(initiative),
    }


def collections_plan_ref(plan_id: str) -> str:
    """The seeded initiative's reference, handed back by `seed_vault` itself."""
    from exomem import structured_collections as collections

    return collections.plan_ref(PLANNING_ID, plan_id)


OUTCOME_TITLE = "Delivery outcome"
INITIATIVE_TITLE = "Delivery initiative"


def _seed_parent_chain(vault_root: Path) -> str:
    from exomem import planning
    from exomem import structured_collections as collections

    outcome = planning.add(
        vault_root,
        PLANNING_PATH,
        item={
            "title": OUTCOME_TITLE,
            "kind": "outcome",
            "status": "planned",
            "commitment": "committed",
            "horizon": "quarter",
        },
        why="state the intended outcome",
    )
    initiative = planning.add(
        vault_root,
        PLANNING_PATH,
        item={
            "title": INITIATIVE_TITLE,
            "kind": "initiative",
            "status": "planned",
            "commitment": "committed",
            "horizon": "quarter",
            "parent": collections.plan_ref(PLANNING_ID, outcome["plan_id"]),
        },
        why="state the initiative under it",
    )
    return str(initiative["plan_id"])


def initiative_ref(vault_root: Path) -> str:
    """The plan reference every seeded work item hangs from, read from the vault.

    Read back rather than re-derived from the title, so a test that disables key
    derivation still builds the same fixture and fails on the behaviour it is
    probing. Read rather than cached in this module, so the answer belongs to the
    vault asked about: a process-global kept the fixture from surviving a vault
    that was copied, restored, or seeded by anything but this call.
    """
    from exomem import record_formats
    from exomem import structured_collections as collections

    manifest = collections.load_manifest(vault_root, vault_root / PLANNING_PATH)
    for record in record_formats.load_adapter(vault_root, manifest).read().records:
        if record.values.get("title") == INITIATIVE_TITLE:
            return collections.plan_ref(PLANNING_ID, record.identity.key)
    raise LookupError(f"{INITIATIVE_TITLE!r} is not in {PLANNING_PATH}")


def queue_item(vault_root: Path, title: str, **fields: Any) -> dict[str, Any]:
    """File one committed, planned deliverable — open work with a real intent.

    Committed rather than the default candidate because Planning's own state
    machine refuses to complete an uncommitted inbox item, and the case under
    test is an outcome landing on work the vault actually intends.
    """
    from exomem import planning

    item = {
        "title": title,
        "status": "planned",
        "commitment": "committed",
        "horizon": "week",
        "parent": initiative_ref(vault_root),
    }
    item.update(fields)
    return planning.add(vault_root, PLANNING_PATH, item=item, why=f"queue {title}")


def settle_item(
    vault_root: Path, added: dict[str, Any], *, status: str = "completed"
) -> dict[str, Any]:
    """Move one item out of the open state through the ordinary triage path.

    The guards are read fresh rather than carried from the `add` receipt: any
    other write moves the container, and a real caller reads before it writes.
    """
    from exomem import planning, record_formats, records
    from exomem import structured_collections as collections

    manifest = collections.load_manifest(vault_root, vault_root / PLANNING_PATH)
    snapshot = record_formats.load_adapter(vault_root, manifest).read()
    guards = records.lifecycle_guards(manifest, snapshot)
    item = next(
        record for record in snapshot.records if record.identity.key == added["plan_id"]
    )
    return planning.triage(
        vault_root,
        PLANNING_PATH,
        plan_id=added["plan_id"],
        transition={"status": status},
        expected_container_hash=guards["expected_container_hash"],
        expected_item_version=item.source.hash,
        why=f"the deliverable is {status}",
    )


def report_event(
    vault_root: Path,
    title: str,
    *,
    occurred_on: str = "2026-08-01",
    event_type: str = "produced",
    collection: str = RECORDS_PATH,
) -> dict[str, Any]:
    """Append one observed event that joins to a queued deliverable by title."""
    from exomem import records

    return records.append_record(
        vault_root,
        collection,
        item={"occurred_on": occurred_on, "title": title, "event_type": event_type},
        why=f"record that {title} was {event_type}",
    )
