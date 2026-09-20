"""Review round 4 (A2 follow-up) -- bounded work end-to-end through activation.

The A2 fix (`late_link_projection`) was proven directly against
`working_set_state._from_records`. Nobody had yet shown that a REAL
`commands.op_activate_context` request actually takes that path, or that
some OTHER reach from activation into Records/Planning governance still
projects every record eagerly with the cold candidate index allowed --
which would reproduce the same class of vault-wide-walk stall through a
different door.

This file drives `commands.op_activate_context` end to end (never
`_from_records` directly) and spies on `vault.walk_vault_md` and
`record_formats._late_projected_result` around the call, for every reach
the accompanying audit found:

  1. `working_set_state.current_state_for` -> `_from_records` (Records,
     already fixed): a stateful Records-anchored referent, many records,
     an unrelated bare-title link on the newest one.
  2. `working_set_index._collection_candidates` -> `_planning_candidates`
     -> `planning.query` (Planning, EAGER, `late_link_projection` never
     set): reached from `ensure_index`'s synchronous index refresh inside
     `op_activate_context`, not from any lane. Planning's only real
     link-typed field convention (`parent: {type: link, link_kind: plan}`)
     is gated by `planning.normalize_item`'s format+existence check at
     BOTH write time (`planning.add` refuses a bare title outright -- see
     the first assertion below) and read time
     (`planning.query`'s `_validate_authorized_snapshot` re-validates
     every record's `parent` before `record_governance.query_collection`
     is ever called), so a malformed reference fails the whole collection
     closed before link governance -- and therefore before
     `_candidate_index_available()` -- ever runs. The fixture below hand-
     plants the one shape the real writer cannot produce, to exercise
     that read-time gate directly; every other value in this file goes
     through the real writers.

Every other candidate the audit considered (`_records_lane`,
`_planning_lane` via `working_set._lane`, `working_set_state._from_profile`
and `_from_neighbourhood`, `record_governance.effective_claims`) does not
call `record_governance.query_collection`, `record_formats.query_collection`
or `_LinkProjector.create` at all -- confirmed by reading each function
body and by grepping the whole `src/exomem` tree for those three names.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from uuid import UUID

import pytest

from exomem import (
    commands,
    freshness,
    lexstore,
    record_formats,
    record_governance,
    working_set_index,
    working_set_runtime,
)
from exomem import find as find_module
from exomem import vault as vault_module
from exomem.vault import walk_vault_md


def _seed_freshness_live(vault: Path) -> None:
    """Seed the event-maintained freshness registry the way the watcher
    does (see test_latency_gate.py's `_seed_freshness_live`), so the
    ordinary lexical/BM25 sidecar build the activation call ALSO triggers
    is warm rather than walking the vault for an unrelated reason --
    otherwise that walk would be indistinguishable, in a raw call count,
    from the one this file is checking for.
    """
    freshness.seed(
        vault,
        "vault",
        ((str(p), freshness.stat_signature(p)) for p in walk_vault_md(vault)),
    )
    kb = vault / "Knowledge Base"
    freshness.seed(
        vault,
        "kb",
        ((str(p), freshness.stat_signature(p)) for p in find_module._walk_md(kb)),
    )


def _warm(vault: Path) -> None:
    """Warm every piece of index/sidecar machinery an activation request
    touches, BEFORE the spies below are installed -- so only a walk
    genuinely triggered by Records/Planning link governance, during the
    measured `op_activate_context` call, is attributed to it.
    """
    _seed_freshness_live(vault)
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()


class _WalkCounter:
    def __init__(self) -> None:
        self.count = 0
        self._real = vault_module.walk_vault_md

    def __enter__(self) -> _WalkCounter:
        def counting(root: Path) -> object:
            self.count += 1
            return self._real(root)

        vault_module.walk_vault_md = counting
        return self

    def __exit__(self, *exc: object) -> None:
        vault_module.walk_vault_md = self._real


def test_activation_bounds_records_current_state_end_to_end(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) A turn resolves a stateful Records-anchored referent whose
    claiming collection holds many records, the newest carrying an
    unrelated bare-title link. Driven through `commands.op_activate_context`
    (not `_from_records` directly): the vault must never be scanned, the
    bounded late-projection path must actually run, and the current-state
    statement must still be truthful.
    """
    kb = vault / "Knowledge Base"
    (kb / "log.md").write_text("# Activity\n", encoding="utf-8")
    (kb / "Products").mkdir(parents=True, exist_ok=True)
    (kb / "Products" / "Fleet Van.md").write_text(
        "---\ntype: note\nstatus: active\nupdated: 2026-09-02\n---\n\n"
        "# Fleet Van\n\n## Summary\n\nThe fleet delivery van.\n",
        encoding="utf-8",
    )
    collection_id = "77777777-8888-4999-8aaa-bbbbbbbbbbbb"
    coll_dir = kb / "Records" / "Fleet Log"
    coll_dir.mkdir(parents=True)
    (coll_dir / "_collection.md").write_text(
        f"---\ntype: collection\nexomem_id: {collection_id}\n"
        "title: Fleet log\nsemantic_profile: records\ncollection_version: 1\nschema_version: 1\n"
        "lifecycle: active\nstorage:\n  strategy: markdown-items\n  source: Items\n  format_version: 1\n"
        "claims:\n  terms: [fleet, van, log]\n"
        "item_schema:\n  natural_key: [observed_on, asset]\n  fields:\n"
        "    observed_on: {type: date, required: true}\n    asset: {type: link, required: true}\n"
        "    status: {type: string}\n---\n\nFleet log fixture.\n",
        encoding="utf-8",
    )
    items = coll_dir / "Items"
    items.mkdir()
    start = datetime.date(2026, 1, 1)
    for index in range(40):
        occurred_on = (start + datetime.timedelta(days=index)).isoformat()
        (items / f"{occurred_on}.md").write_text(
            f"---\ntype: record\ncollection_id: {collection_id}\n"
            f"record_id: aaaaaaaa-aaaa-4aaa-8aaa-{index:012d}\nschema_version: 1\n"
            f"observed_on: {occurred_on}\n"
            'asset: "[[Assets/Fleet Van]]"\nstatus: completed\n---\n',
            encoding="utf-8",
        )
    # The newest record carries an unrelated link field holding a bare
    # title -- the exact shape R1 already proved current_state_for skips.
    (items / "2026-07-15-bare.md").write_text(
        f"---\ntype: record\ncollection_id: {collection_id}\n"
        "record_id: 99999999-9999-4999-8999-999999999999\nschema_version: 1\n"
        "observed_on: 2026-07-15\n"
        'asset: "[[Some Bare Vehicle Title]]"\nstatus: completed\n---\n',
        encoding="utf-8",
    )

    _warm(vault)

    late_calls = 0
    real_late = record_formats._late_projected_result

    def counting_late(*args: object, **kwargs: object) -> object:
        nonlocal late_calls
        late_calls += 1
        return real_late(*args, **kwargs)

    monkeypatch.setattr(record_formats, "_late_projected_result", counting_late)

    with _WalkCounter() as walks:
        packet = commands.op_activate_context(vault, turn="fleet van")

    assert packet["abstained"] is False, packet.get("abstention")
    assert walks.count == 0, (
        "activation must not scan the vault for a Records link field the "
        "current-state lookup never asked for"
    )
    assert late_calls >= 1, (
        "the bounded late-projection path must actually run for this shape, "
        "not silently fall back to the eager path"
    )
    records_entries = [
        entry for entry in packet["current_state"] if entry.get("source") == "records"
    ]
    assert records_entries, packet["current_state"]
    for entry in records_entries:
        assert entry["as_of"] == "2026-07-15"
        assert entry["statement"] == "status: completed"
        assert "Bare Vehicle" not in entry["statement"]


def test_activation_planning_reach_does_not_walk_the_vault_for_a_malformed_parent_link(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b) The Planning reach the audit found: `ensure_index`'s synchronous
    index refresh (not any role lane) calls `working_set_index
    ._planning_candidates` -> `planning.query` -> `record_governance
    .query_collection` EAGERLY (no `late_link_projection`) for every
    discovered Planning collection with an `active` lifecycle filter.

    First: the real writer refuses to create the dangerous shape at all --
    `planning.add` raises for a bare-title `parent`, so that value can
    never reach a Planning file through the product's own write surface.
    Second (hand-planted, since the real writer just proved it cannot
    produce this file): even when such a file exists on disk,
    `planning.query`'s own read-time snapshot validation
    (`_validate_authorized_snapshot` -> `normalize_item` ->
    `_validate_optional`) rejects the malformed `parent` value and fails
    the WHOLE collection's query closed BEFORE `record_governance
    .query_collection` -- and therefore before link governance's
    `_candidate_index_available()` -- is ever reached. Driven end to end
    through `commands.op_activate_context`: no vault-wide scan, and the
    request still serves a packet rather than crashing.
    """
    kb = vault / "Knowledge Base"
    (kb / "log.md").write_text("# Activity\n", encoding="utf-8")
    collection_id = "55555555-6666-4777-8888-999999999999"
    manifest_path = "Knowledge Base/Planning/Fleet/_collection.md"
    coll_dir = kb / "Planning" / "Fleet"
    coll_dir.mkdir(parents=True)
    (coll_dir / "_collection.md").write_text(
        f"---\ntype: collection\nexomem_id: {collection_id}\n"
        "title: Fleet planning\nsemantic_profile: planning\ncollection_version: 1\nschema_version: 1\n"
        "lifecycle: active\nstorage:\n  strategy: markdown-items\n  source: Items\n  format_version: 1\n"
        "item_schema:\n  natural_key: [title]\n  fields:\n"
        "    title: {type: string, required: true}\n    kind: {type: string}\n    status: {type: string}\n"
        "    lifecycle: {type: string}\n    priority: {type: string}\n    commitment: {type: string}\n"
        "    horizon: {type: string}\n    health: {type: string}\n    area: {type: string}\n"
        "    parent: {type: link, link_kind: plan}\n---\n\nFleet planning items.\n",
        encoding="utf-8",
    )
    (coll_dir / "Items").mkdir()

    from exomem import planning
    from exomem import structured_collections as collections

    # The real writer refuses the dangerous shape outright.
    with pytest.raises(collections.CollectionError, match="parent must be a Planning reference"):
        planning.add(
            vault,
            manifest_path,
            plan_id=str(UUID(int=1)),
            item={
                "title": "Replace the fleet van",
                "kind": "work-item",
                "status": "active",
                "lifecycle": "active",
                "commitment": "committed",
                "horizon": "month",
                "parent": "[[Some Bare Title Plan]]",
            },
            why="attempt a bare-title parent reference",
        )
    # Confirm nothing was written by the refused attempt.
    assert list((coll_dir / "Items").iterdir()) == []

    # Hand-plant the file the writer just proved it cannot produce, to
    # exercise the READ path directly.
    (coll_dir / "Items" / "a.md").write_text(
        f"---\ntype: plan\ncollection_id: {collection_id}\n"
        "plan_id: 11111111-1111-4111-8111-111111111111\nschema_version: 1\n"
        "title: Replace the fleet van\nkind: work-item\nstatus: active\nlifecycle: active\n"
        "commitment: committed\nhorizon: month\nhealth: unknown\npriority: none\n"
        'parent: "[[Some Bare Title Plan]]"\n---\n\nBody.\n',
        encoding="utf-8",
    )
    (kb / "Products").mkdir(parents=True, exist_ok=True)
    (kb / "Products" / "Fleet Van.md").write_text(
        "---\ntype: note\nstatus: active\nupdated: 2026-09-02\n---\n\n"
        "# Fleet Van\n\n## Summary\n\nThe fleet delivery van.\n",
        encoding="utf-8",
    )

    _warm(vault)

    with _WalkCounter() as walks:
        packet = commands.op_activate_context(vault, turn="fleet van")

    assert walks.count == 0, (
        "a malformed Planning parent link must not scan the vault, even "
        "though this reach never opts into late_link_projection"
    )
    assert packet["abstained"] is False, packet.get("abstention")


def test_activation_planning_reach_is_genuinely_eager_for_a_valid_collection(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Companion to (b): proves the reach identified in the audit is real
    and exercised (not accidentally dead code) for an ordinary, valid
    Planning collection with no link field at all -- `record_governance
    .query_collection(semantic_profile="planning", ...)` is called without
    `late_link_projection` during the same `op_activate_context` request.
    """
    from test_working_set_index import _seed_planning, _seed_structure

    _seed_structure(vault)
    _seed_planning(vault)
    _warm(vault)

    calls: list[bool | None] = []
    real_query_collection = record_governance.query_collection

    def spy(*args: object, **kwargs: object) -> object:
        if kwargs.get("semantic_profile") == "planning":
            calls.append(kwargs.get("late_link_projection"))
        return real_query_collection(*args, **kwargs)

    monkeypatch.setattr(record_governance, "query_collection", spy)

    packet = commands.op_activate_context(vault, turn="the cargo sled north")

    assert packet["abstained"] is False, packet.get("abstention")
    assert calls, "the Planning reach from index refresh must actually run for this fixture"
    assert all(value in (None, False) for value in calls), (
        f"expected the eager (no late_link_projection) path for every planning "
        f"query_collection call, got {calls}"
    )
