"""The `unreflected_outcomes` family: an observed outcome on an open plan item.

Nothing here judges whether the item is done. The family reports that events
joined to an item the vault still holds open, which is the one fact both sides
already contain and neither side states.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from lifecycle_fixtures import (
    PLANNING_PATH,
    RECORDS_PATH,
    UNBOUND_PATH,
    queue_item,
    records_manifest,
    report_event,
    seed_vault,
    settle_item,
)

from exomem import audit as audit_module
from exomem import due_state as due_state_module
from exomem import review_state as review_state_module

FAMILY = "unreflected_outcomes"


def _findings(vault_root: Path) -> list:
    report = audit_module.audit(vault_root, categories=[FAMILY])
    return report.findings


def _titles(findings: list) -> set[str]:
    return {str((finding.meta or {}).get("plan_title") or "") for finding in findings}


# --- 4.1 the family ------------------------------------------------------------


def test_a_recorded_event_on_an_open_item_is_one_finding(tmp_path: Path) -> None:
    """Two open items, one of them reported produced: exactly one candidate."""
    seed_vault(tmp_path)
    queue_item(tmp_path, "Batch 1")
    queue_item(tmp_path, "Batch 2")
    report_event(tmp_path, "Batch 1")

    findings = _findings(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.category == FAMILY
    assert finding.severity == "info"
    assert finding.path.startswith("Knowledge Base/Planning/Delivery/Items/")
    assert (finding.meta or {})["joined_total"] == 1
    assert (finding.meta or {})["binding"] == {"title": "title"}
    assert (finding.meta or {})["records_collection"] == RECORDS_PATH
    assert _titles(findings) == {"Batch 1"}


def test_a_twin_collection_without_a_join_produces_nothing(tmp_path: Path) -> None:
    seed_vault(tmp_path, unbound=True)
    queue_item(tmp_path, "Batch 1")
    report_event(tmp_path, "Batch 1", collection=UNBOUND_PATH)

    assert _findings(tmp_path) == []


def test_a_completed_item_produces_nothing(tmp_path: Path) -> None:
    seed_vault(tmp_path)
    added = queue_item(tmp_path, "Batch 1")
    report_event(tmp_path, "Batch 1")
    settle_item(tmp_path, added)

    assert _findings(tmp_path) == []


def test_an_archived_item_produces_nothing(tmp_path: Path) -> None:
    from exomem import planning

    seed_vault(tmp_path)
    added = queue_item(tmp_path, "Batch 1")
    report_event(tmp_path, "Batch 1")
    cancelled = settle_item(tmp_path, added, status="cancelled")
    planning.update(
        tmp_path,
        PLANNING_PATH,
        plan_id=added["plan_id"],
        changes={"lifecycle": "archived"},
        expected_container_hash=cancelled["after_container_hash"],
        expected_item_version=cancelled["after_item_hash"],
        why="park the cancelled deliverable",
    )

    assert _findings(tmp_path) == []


def test_an_unresolvable_binding_is_reported_as_unevaluated_not_skipped(
    tmp_path: Path,
) -> None:
    """An un-evaluated item is not a pass. Silence here would be a false clean bill."""
    seed_vault(tmp_path)
    queue_item(tmp_path, "Batch 1")
    report_event(tmp_path, "Batch 1")
    manifest = tmp_path / RECORDS_PATH
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "exomem://memory/3f5b6d21-9c4e-4a77-9c2f-6d0b1e2a5c48",
            "exomem://memory/00000000-0000-4000-8000-000000000000",
        ),
        encoding="utf-8",
    )

    report = audit_module.audit(tmp_path, categories=[FAMILY])

    assert report.findings == []
    unevaluated = (report.metadata or {})[FAMILY]["unevaluated"]
    assert [entry["collection"] for entry in unevaluated] == [RECORDS_PATH]
    assert unevaluated[0]["reason"] == "unresolved_planning_reference"


def test_the_family_is_registered_in_every_category_surface() -> None:
    from exomem import attention as attention_module

    assert FAMILY in audit_module.ALL_CATEGORIES
    assert FAMILY in attention_module.DEFAULT_ATTENTION_CATEGORIES
    assert FAMILY in due_state_module.PROJECTION_CATEGORIES
    assert FAMILY in due_state_module.DELTA_CATEGORIES
    # Derived, never restated: the family is triageable because it is default.
    assert FAMILY in review_state_module.registered_families()


# --- 4.2 the fingerprint --------------------------------------------------------


def _fingerprint(vault_root: Path) -> str:
    finding = _findings(vault_root)[0]
    refs = review_state_module.refs_for_paths(
        vault_root, [finding.path, *(finding.paths or [])]
    )
    from exomem import attention as attention_module

    return review_state_module.component_fingerprint(
        target_ref=refs[finding.path],
        reason=attention_module._reason(finding.category, 1, finding),
        related_refs=[
            refs[path] for path in sorted(set(finding.paths or [])) if path != finding.path
        ],
    )


def test_a_second_joined_record_changes_the_fingerprint(tmp_path: Path) -> None:
    seed_vault(tmp_path)
    queue_item(tmp_path, "Batch 1")
    report_event(tmp_path, "Batch 1")
    before = _fingerprint(tmp_path)

    report_event(tmp_path, "Batch 1", occurred_on="2026-08-02")

    after = _fingerprint(tmp_path)
    assert after != before
    assert _findings(tmp_path)[0].meta["joined_total"] == 2


def test_removing_the_second_record_restores_the_first_fingerprint(tmp_path: Path) -> None:
    """A dismissal binds to a fingerprint, so the restored one must be identical."""
    seed_vault(tmp_path)
    queue_item(tmp_path, "Batch 1")
    report_event(tmp_path, "Batch 1")
    before = _fingerprint(tmp_path)
    second = report_event(tmp_path, "Batch 1", occurred_on="2026-08-02")
    assert _fingerprint(tmp_path) != before

    (tmp_path / second["affected_paths"][0]).unlink()

    assert _fingerprint(tmp_path) == before


def test_a_dismissed_item_resurfaces_when_a_new_event_lands(tmp_path: Path) -> None:
    from exomem.commands import op_triage_memory

    seed_vault(tmp_path)
    queue_item(tmp_path, "Batch 1")
    report_event(tmp_path, "Batch 1")
    due_state_module.reconcile(tmp_path)
    first = due_state_module.served_entries(tmp_path)
    assert [row["category"] for row in first] == [FAMILY]
    op_triage_memory(
        tmp_path,
        ref=first[0]["ref"],
        action="dismiss",
        why="handled: already tracked elsewhere",
        expected_fingerprint=first[0]["fingerprint"],
    )
    assert due_state_module.served_entries(tmp_path) == []

    report_event(tmp_path, "Batch 1", occurred_on="2026-08-02")
    due_state_module.reconcile(tmp_path)

    resurfaced = due_state_module.served_entries(tmp_path)
    assert [row["category"] for row in resurfaced] == [FAMILY]
    assert resurfaced[0]["fingerprint"] != first[0]["fingerprint"]
    store = review_state_module.ReviewStateStore(tmp_path)
    assert store.load()["records"], "the earlier dismissal record must stand"


# --- 4.3 the attention surface --------------------------------------------------


def test_the_family_can_be_quieted_through_the_existing_surface(tmp_path: Path) -> None:
    from exomem.commands import op_triage_memory

    seed_vault(tmp_path)
    queue_item(tmp_path, "Batch 1")
    report_event(tmp_path, "Batch 1")
    due_state_module.reconcile(tmp_path)
    assert due_state_module.served_entries(tmp_path)

    op_triage_memory(
        tmp_path,
        ref=review_state_module.family_ref(FAMILY),
        action="quiet",
        why="too_frequent: not this quarter",
    )

    assert due_state_module.served_entries(tmp_path) == []
    # Measured, not clean: the audit still reports it.
    assert len(_findings(tmp_path)) == 1


# --- 4.4 the write-time delta ---------------------------------------------------


def _projection(vault_root: Path) -> dict:
    return (due_state_module.load(vault_root) or {}).get("categories", {}).get(FAMILY, {})


def test_the_delta_after_a_record_append_equals_a_full_recompute(tmp_path: Path) -> None:
    seed_vault(tmp_path)
    queue_item(tmp_path, "Batch 1")
    queue_item(tmp_path, "Batch 2")
    due_state_module.reconcile(tmp_path)
    assert _projection(tmp_path) == {}

    report_event(tmp_path, "Batch 1")

    delta = _projection(tmp_path)
    assert len(delta) == 1
    due_state_module.reconcile(tmp_path)
    assert _projection(tmp_path) == delta


def test_the_delta_after_a_triage_to_completed_clears_it(tmp_path: Path) -> None:
    seed_vault(tmp_path)
    added = queue_item(tmp_path, "Batch 1")
    report_event(tmp_path, "Batch 1")
    due_state_module.reconcile(tmp_path)
    assert len(_projection(tmp_path)) == 1

    settle_item(tmp_path, added)

    assert _projection(tmp_path) == {}


def test_reconcile_heals_an_out_of_band_manifest_edit(tmp_path: Path) -> None:
    seed_vault(tmp_path)
    queue_item(tmp_path, "Batch 1")
    report_event(tmp_path, "Batch 1")
    due_state_module.reconcile(tmp_path)
    assert len(_projection(tmp_path)) == 1

    manifest = tmp_path / RECORDS_PATH
    manifest.write_text(records_manifest(join=False), encoding="utf-8")
    assert len(_projection(tmp_path)) == 1, "the projection is stale until reconcile"

    due_state_module.reconcile(tmp_path)

    assert _projection(tmp_path) == {}


# --- 4.5 the carriers -----------------------------------------------------------


@pytest.fixture(autouse=True)
def _quiet_emission_state() -> None:
    due_state_module.reset_emission_state()


def _command(name: str):
    from exomem import commands

    return next(command for command in commands.PRODUCT_COMMANDS if command.name == name)


def _append_through_the_dispatcher(
    vault_root: Path, title: str, *, occurred_on: str = "2026-08-01", **kwargs
) -> dict:
    """One append over the shared dispatcher -- the path a caller actually takes.

    Driven through `writer_lease.invoke_command` rather than through
    `records.append_record`, because everything under test here (emission
    governance, the batch scope, the `_vault` strip) happens at the mutation
    terminal the dispatcher owns. A test that calls the writer directly asserts
    against a dispatcher it re-implemented, and cannot see any of it.
    """
    from exomem import writer_lease

    return writer_lease.invoke_command(
        _command("record_memory"),
        vault_root,
        action="append",
        collection=RECORDS_PATH,
        item={"occurred_on": occurred_on, "title": title, "event_type": "produced"},
        why=f"record {title} produced",
        **kwargs,
    )


def test_the_append_that_opens_the_gap_carries_the_block(tmp_path: Path) -> None:
    seed_vault(tmp_path)
    queue_item(tmp_path, "Batch 1")
    due_state_module.reconcile(tmp_path)

    receipt = _append_through_the_dispatcher(tmp_path, "Batch 1")

    block = receipt["due_state"]
    assert block["total"] == 1
    assert block["categories"] == {FAMILY: 1}
    assert block["top"][0]["ref"].startswith("exomem://review/")
    assert "_vault" not in block, "the routing hint is server-internal"
    assert "_vault" not in json.dumps(receipt)
    # The projection stores the joined records and the binding component so a
    # withheld one can be dropped at serve. None of it is the caller's business.
    wire = json.dumps(receipt)
    assert "component" not in wire and "joined" not in wire
    assert set(block["top"][0]) == {"category", "ref", "due_since"}


def test_the_legacy_response_detail_carries_no_block(tmp_path: Path) -> None:
    seed_vault(tmp_path)
    queue_item(tmp_path, "Batch 1")
    due_state_module.reconcile(tmp_path)

    receipt = _append_through_the_dispatcher(
        tmp_path, "Batch 1", response_detail="legacy"
    )

    assert "due_state" not in json.dumps(receipt)


def test_twelve_dispatched_appends_in_one_batch_scope_emit_once(tmp_path: Path) -> None:
    """The batch is the unit a caller experiences, so it is the unit governed."""
    seed_vault(tmp_path)
    for index in range(12):
        queue_item(tmp_path, f"Batch {index}")
    due_state_module.reconcile(tmp_path)

    delivered = 0
    with due_state_module.batch_scope(tmp_path):
        for index in range(12):
            if _append_through_the_dispatcher(tmp_path, f"Batch {index}").get("due_state"):
                delivered += 1

    assert delivered == 0, "inside the batch the terminal stays silent"
    assert len(_projection(tmp_path)) == 12, "the counts stay true inside the batch"
    assert due_state_module.would_emit(
        due_state_module.served(tmp_path), vault_root=tmp_path
    ) is True


def test_a_plan_triage_carries_the_cleared_state(tmp_path: Path) -> None:
    from exomem import record_formats, records, writer_lease
    from exomem import structured_collections as collections

    seed_vault(tmp_path)
    added = queue_item(tmp_path, "Batch 1")
    _append_through_the_dispatcher(tmp_path, "Batch 1")
    due_state_module.reconcile(tmp_path)
    manifest = collections.load_manifest(tmp_path, tmp_path / PLANNING_PATH)
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    item = next(r for r in snapshot.records if r.identity.key == added["plan_id"])

    receipt = writer_lease.invoke_command(
        _command("plan_memory"),
        tmp_path,
        action="triage",
        collection=PLANNING_PATH,
        plan_id=added["plan_id"],
        transition={"status": "completed"},
        expected_container_hash=records.lifecycle_guards(manifest, snapshot)[
            "expected_container_hash"
        ],
        expected_item_version=item.source.hash,
        why="the deliverable is completed",
    )

    assert "due_state" not in receipt
    assert _projection(tmp_path) == {}


def test_twelve_appends_in_one_batch_scope_deliver_once(tmp_path: Path) -> None:
    from exomem import mutation_terminal

    seed_vault(tmp_path)
    for index in range(12):
        queue_item(tmp_path, f"Batch {index}")
    due_state_module.reconcile(tmp_path)

    delivered = 0
    with due_state_module.batch_scope(tmp_path):
        for index in range(12):
            receipt = report_event(tmp_path, f"Batch {index}", occurred_on="2026-08-01")
            block, vault_hint = mutation_terminal._due_state_projection(receipt)
            if block is not None and mutation_terminal._admit_due_state(block, vault_hint):
                delivered += 1

    assert delivered == 0, "inside the batch the terminal stays silent"
    assert len(_projection(tmp_path)) == 12, "the counts stay true inside the batch"
    block = due_state_module.served(tmp_path)
    assert due_state_module.would_emit(block, vault_root=tmp_path) is True


def test_a_quiet_family_removes_the_block(tmp_path: Path) -> None:
    from exomem.commands import op_triage_memory

    seed_vault(tmp_path)
    queue_item(tmp_path, "Batch 1")
    due_state_module.reconcile(tmp_path)
    op_triage_memory(
        tmp_path,
        ref=review_state_module.family_ref(FAMILY),
        action="quiet",
        why="too_frequent: not this quarter",
    )

    receipt = report_event(tmp_path, "Batch 1")

    assert "due_state" not in receipt


def test_an_unreadable_review_state_yields_no_block_and_the_write_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_vault(tmp_path)
    queue_item(tmp_path, "Batch 1")
    due_state_module.reconcile(tmp_path)

    def unreadable(*_args: object, **_kwargs: object) -> None:
        raise OSError("review state is unreadable")

    monkeypatch.setattr(due_state_module, "served", unreadable)

    receipt = report_event(tmp_path, "Batch 1")

    assert receipt["outcome"] == "committed"
    assert "due_state" not in receipt


# --- 4.6 disclosure -------------------------------------------------------------


def _write_release_rules(vault: Path, *, hidden: str) -> None:
    root = vault / "Knowledge Base" / "_Governance"
    (root / "scopes").mkdir(parents=True, exist_ok=True)
    (root / "rules").mkdir(parents=True, exist_ok=True)
    (root / "scopes" / "open.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\nname: Open\n"
        'paths: ["**"]\n',
        encoding="utf-8",
    )
    (root / "rules" / "open.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\naudience: external\nceiling: 6\n',
        encoding="utf-8",
    )
    (root / "scopes" / "blocked.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FC5\nname: blocked\n"
        f'paths: ["{hidden}"]\n',
        encoding="utf-8",
    )
    (root / "rules" / "blocked.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FC6\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FC5"]\naudience: external\nceiling: 0\n',
        encoding="utf-8",
    )


def test_a_withheld_record_contributes_nothing_to_the_finding(tmp_path: Path) -> None:
    """The served view must equal the vault with the withheld record absent."""
    from exomem.governance.principal import RequestPrincipal, request_scope

    seed_vault(tmp_path)
    queue_item(tmp_path, "Batch 1")
    queue_item(tmp_path, "Batch 2")
    report_event(tmp_path, "Batch 1")
    withheld = report_event(tmp_path, "Batch 2")
    _write_release_rules(tmp_path, hidden=withheld["affected_paths"][0].split("Knowledge Base/")[-1])

    with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
        findings = _findings(tmp_path)

    assert _titles(findings) == {"Batch 1"}


def test_a_withheld_plan_item_contributes_nothing_to_the_finding(tmp_path: Path) -> None:
    from exomem.governance.principal import RequestPrincipal, request_scope

    seed_vault(tmp_path)
    queue_item(tmp_path, "Batch 1")
    hidden = queue_item(tmp_path, "Batch 2")
    report_event(tmp_path, "Batch 1")
    report_event(tmp_path, "Batch 2")
    _write_release_rules(tmp_path, hidden=hidden["affected_paths"][0].split("Knowledge Base/")[-1])

    with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
        findings = _findings(tmp_path)

    assert _titles(findings) == {"Batch 1"}


def test_the_runtime_never_transitions_the_plan_item(tmp_path: Path) -> None:
    seed_vault(tmp_path)
    added = queue_item(tmp_path, "Batch 1")
    item = tmp_path / added["affected_paths"][0]
    before = item.read_bytes()

    report_event(tmp_path, "Batch 1")
    due_state_module.reconcile(tmp_path, today=dt.date(2027, 1, 1))
    due_state_module.served_entries(tmp_path)

    assert item.read_bytes() == before


# --- round 1 -------------------------------------------------------------------


def test_the_seeded_initiative_survives_a_fresh_import_of_the_fixtures(
    tmp_path: Path,
) -> None:
    """The fixture's parent chain belongs to the vault, not to this process.

    A module-level cache keyed by vault path answered correctly only while the
    same import that seeded the vault was still loaded: a reload, a second
    worker, or a vault copied from elsewhere lost the initiative and the fixture
    stopped building.
    """
    import importlib

    import lifecycle_fixtures

    seeded = seed_vault(tmp_path)
    reloaded = importlib.reload(lifecycle_fixtures)

    assert reloaded.initiative_ref(tmp_path) == seeded["initiative"]
    assert reloaded.queue_item(tmp_path, "Batch 1")["plan_id"]


def test_a_write_into_an_unbound_collection_still_counts_as_a_write(
    tmp_path: Path,
) -> None:
    """m3: `writes` is the denominator, so it counts every governed write.

    Counting only the writes that had something to say makes "0 emissions for N
    writes" divide by the wrong population, and the ratio then flatters the
    governor exactly in the vaults where it did the least work.
    """
    seed_vault(tmp_path, unbound=True)
    queue_item(tmp_path, "Batch 1")
    due_state_module.reconcile(tmp_path)
    before = due_state_module.load(tmp_path)["emission"]

    report_event(tmp_path, "Batch 1", collection=UNBOUND_PATH)

    after = due_state_module.load(tmp_path)["emission"]
    assert after["writes"] == before["writes"] + 1
    assert after["emissions"] == before["emissions"]


def test_a_join_naming_an_undeclared_plan_field_is_reported_as_unevaluated(
    tmp_path: Path,
) -> None:
    """m4: a join onto a field the plan does not declare cannot be evaluated.

    Silence here is indistinguishable from "checked, nothing owed" -- the same
    reason an unresolvable reference is reported rather than skipped.
    """
    seed_vault(tmp_path)
    queue_item(tmp_path, "Batch 1")
    report_event(tmp_path, "Batch 1")
    manifest = tmp_path / RECORDS_PATH
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "        title: title\n", "        title: codename\n"
        ),
        encoding="utf-8",
    )

    report = audit_module.audit(tmp_path, categories=[FAMILY])

    assert report.findings == []
    unevaluated = (report.metadata or {}).get(FAMILY, {}).get("unevaluated") or []
    assert [row.get("reason") for row in unevaluated] == ["undeclared_plan_field"]
    assert unevaluated[0]["fields"] == ["codename"]
    assert unevaluated[0]["collection"] == RECORDS_PATH


def test_an_ordinary_page_write_leaves_the_collection_pair_finding_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M2: the page/structured split is a mechanism, so removing it must bite.

    `unreflected_outcomes` is a property of a bound COLLECTION PAIR. A page write
    can neither produce it nor prove its absence, so a page delta that claims the
    written path's whole delta set silently deletes a live finding -- once for a
    write on the plan item itself, and once for a write anywhere else that
    happens to carry no entry for it.
    """
    seed_vault(tmp_path)
    added = queue_item(tmp_path, "Batch 1")
    report_event(tmp_path, "Batch 1")
    due_state_module.reconcile(tmp_path)
    plan_item = added["affected_paths"][0]
    assert len(_stored(tmp_path)) == 1

    due_state_module.apply_write_delta(tmp_path, plan_item)
    assert len(_stored(tmp_path)) == 1

    due_state_module.apply_write_delta(tmp_path, "Knowledge Base/log.md")
    assert len(_stored(tmp_path)) == 1


def _stored(vault_root: Path) -> dict:
    return ((due_state_module.load(vault_root) or {}).get("categories") or {}).get(
        FAMILY
    ) or {}


def _internal_rules(vault: Path) -> None:
    """The internal audience sees everything the external one is denied."""
    root = vault / "Knowledge Base" / "_Governance" / "rules"
    root.mkdir(parents=True, exist_ok=True)
    (root / "open-internal.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB1\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\naudience: internal\nceiling: 6\n',
        encoding="utf-8",
    )


def _fresh_identities(vault_root: Path) -> set[tuple[str, str]]:
    """(ref, fingerprint) as a full audit composes them for whoever is asking.

    Built through `due_state._entry` -- the module's own composer -- rather than
    re-derived here, because a second derivation of this identity is exactly the
    bug the shared composer exists to prevent.
    """
    identities = set()
    for finding in _findings(vault_root):
        paths = sorted({finding.path, *(finding.paths or [])})
        entry = due_state_module._entry(
            vault_root,
            finding,
            review_state_module.refs_for_paths(vault_root, paths),
        )
        assert entry is not None
        identities.add((str(entry["ref"]), str(entry["fingerprint"])))
    return identities


def test_a_projection_built_internally_serves_the_external_truth(tmp_path: Path) -> None:
    """B1: one projection, two audiences, and disclosure decided at serve.

    The projection is written by whoever happens to write last, so it holds the
    WRITING audience's answer. Serving it unfiltered handed an external reader a
    finding built out of a record that reader may not see -- both the count and a
    review ref pointing at evidence that, for them, does not exist.
    """
    from exomem.governance.principal import RequestPrincipal, request_scope

    internal = RequestPrincipal(audience_id="internal", surface="mcp")
    external = RequestPrincipal(audience_id="external", surface="mcp")
    seed_vault(tmp_path)
    queue_item(tmp_path, "Batch 1")
    queue_item(tmp_path, "Batch 2")
    report_event(tmp_path, "Batch 1")
    withheld = report_event(tmp_path, "Batch 2")
    _write_release_rules(
        tmp_path, hidden=withheld["affected_paths"][0].split("Knowledge Base/")[-1]
    )
    _internal_rules(tmp_path)

    with request_scope(internal):
        internal_titles = _titles(_findings(tmp_path))
        due_state_module.reconcile(tmp_path)
        report_event(tmp_path, "Batch 1", occurred_on="2026-08-05")
        assert len(_stored(tmp_path)) == 2, "the projection holds the writer's truth"

    with request_scope(external):
        external_titles = _titles(_findings(tmp_path))
        external_identities = _fresh_identities(tmp_path)
        served = due_state_module.served_entries(tmp_path)
    assert external_titles == {"Batch 1"}
    assert internal_titles == {"Batch 1", "Batch 2"}
    assert _identities(served) == external_identities
    assert len(served) == 1

    with request_scope(internal):
        assert _identities(due_state_module.served_entries(tmp_path)) == (
            _fresh_identities(tmp_path)
        )


def test_a_partly_withheld_finding_serves_the_surviving_records_fingerprint(
    tmp_path: Path,
) -> None:
    """Survivors-only, recomposed through the audit's own composer.

    Dropping the whole entry would under-report and keeping it whole would
    over-report; the served identity has to be the one a fresh audit under that
    audience produces, or dismissing it there dismisses a different finding.
    """
    from exomem.governance.principal import RequestPrincipal, request_scope

    external = RequestPrincipal(audience_id="external", surface="mcp")
    seed_vault(tmp_path)
    queue_item(tmp_path, "Batch 1")
    report_event(tmp_path, "Batch 1")
    withheld = report_event(tmp_path, "Batch 1", occurred_on="2026-08-05")
    _write_release_rules(
        tmp_path, hidden=withheld["affected_paths"][0].split("Knowledge Base/")[-1]
    )
    _internal_rules(tmp_path)
    with request_scope(RequestPrincipal(audience_id="internal", surface="mcp")):
        due_state_module.reconcile(tmp_path)
    stored = next(iter(_stored(tmp_path).values()))

    with request_scope(external):
        served = due_state_module.served_entries(tmp_path)
        expected = _fresh_identities(tmp_path)

    assert len(served) == 1
    assert _identities(served) == expected
    assert served[0]["fingerprint"] != _stored_fingerprint(stored), (
        "the stored fingerprint was composed from two records, one of them withheld"
    )


def _identities(entries: list) -> set[tuple[str, str]]:
    return {(str(entry["ref"]), str(entry["fingerprint"])) for entry in entries}


def _stored_fingerprint(bucket: dict) -> str:
    rows = due_state_module._unbucket(bucket)
    assert len(rows) == 1
    return str(rows[0]["fingerprint"])


def test_the_advisory_runs_outside_the_mutation_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1: the projection is derived state; the lock is for the canonical write.

    Holding the vault's single writer lease across an audit-shaped read made a
    third of the critical section somebody else's queueing time, for work that
    changes nothing another writer could observe. Page writes already run their
    carrier after the commit returns; the structured ones now match.
    """
    import contextlib

    from exomem import records as records_module
    from exomem import writer_lease

    seed_vault(tmp_path)
    queue_item(tmp_path, "Batch 1")
    due_state_module.reconcile(tmp_path)
    held: list[bool] = []
    inside = {"now": False}
    real_guard = writer_lease.LeaseManager.mutation_guard
    real_carrier = records_module._due_state_carrier

    @contextlib.contextmanager
    def marking_guard(self, *args, **kwargs):
        inside["now"] = True
        try:
            with real_guard(self, *args, **kwargs):
                yield
        finally:
            inside["now"] = False

    def marking_carrier(*args, **kwargs):
        held.append(inside["now"])
        return real_carrier(*args, **kwargs)

    monkeypatch.setattr(writer_lease.LeaseManager, "mutation_guard", marking_guard)
    monkeypatch.setattr(records_module, "_due_state_carrier", marking_carrier)

    receipt = report_event(tmp_path, "Batch 1")

    assert held == [False], "the advisory must not run while the write lock is held"
    assert receipt["due_state"]["total"] == 1, "and it must still reach the receipt"


# --- 4.7 the bounded write-time delta -------------------------------------------


def _update_record(vault_root: Path, key: str, changes: dict) -> dict:
    from exomem import record_formats, records
    from exomem import structured_collections as collections

    manifest = collections.load_manifest(vault_root, vault_root / RECORDS_PATH)
    snapshot = record_formats.load_adapter(vault_root, manifest).read()
    item = next(record for record in snapshot.records if record.identity.key == key)
    return records.update_record(
        vault_root,
        RECORDS_PATH,
        item_key=key,
        changes=changes,
        expected_container_hash=snapshot.snapshot,
        expected_item_version=item.source.hash,
        why="correct the recorded event",
    )


class _DeltaReads:
    """Adapter reads performed by the write-time delta, and by nothing else."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from exomem import record_formats

        self.paths: list[str] = []
        self._inside = False
        real_loader = record_formats.load_adapter
        real_block = due_state_module.block_for_structured_write

        def counting_loader(vault_root, manifest, *args, **kwargs):
            if self._inside:
                self.paths.append(str(manifest.path))
            return real_loader(vault_root, manifest, *args, **kwargs)

        def marking_block(*args, **kwargs):
            self._inside = True
            try:
                return real_block(*args, **kwargs)
            finally:
                self._inside = False

        monkeypatch.setattr(record_formats, "load_adapter", counting_loader)
        monkeypatch.setattr(due_state_module, "block_for_structured_write", marking_block)


def test_an_update_moving_the_join_value_moves_the_finding(tmp_path: Path) -> None:
    """The delta is told the previous values, so it can retract as well as add."""
    seed_vault(tmp_path)
    queue_item(tmp_path, "Batch 1")
    queue_item(tmp_path, "Batch 2")
    appended = report_event(tmp_path, "Batch 1")
    due_state_module.reconcile(tmp_path)
    assert _titles_in(_projection(tmp_path)) == {"Batch 1"}

    _update_record(tmp_path, appended["item_key"], {"title": "Batch 2"})

    delta = _projection(tmp_path)
    assert _titles_in(delta) == {"Batch 2"}
    due_state_module.reconcile(tmp_path)
    assert _projection(tmp_path) == delta


def test_a_triage_to_completed_reads_no_records_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An item that left the open state cannot be a finding, whatever joins it.

    So the entry goes without asking the Records collection anything -- the read
    the round-0 delta paid for on every plan write.
    """
    seed_vault(tmp_path)
    added = queue_item(tmp_path, "Batch 1")
    report_event(tmp_path, "Batch 1")
    due_state_module.reconcile(tmp_path)
    reads = _DeltaReads(monkeypatch)

    settle_item(tmp_path, added)

    assert RECORDS_PATH not in reads.paths
    assert reads.paths == []
    assert _projection(tmp_path) == {}


def test_an_update_of_a_non_join_field_reads_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The joined records it already had are still the joined records it has."""
    seed_vault(tmp_path)
    added = queue_item(tmp_path, "Batch 1")
    report_event(tmp_path, "Batch 1")
    due_state_module.reconcile(tmp_path)
    before = _projection(tmp_path)
    reads = _DeltaReads(monkeypatch)

    from exomem import planning, record_formats, records
    from exomem import structured_collections as collections

    manifest = collections.load_manifest(tmp_path, tmp_path / PLANNING_PATH)
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    item = next(r for r in snapshot.records if r.identity.key == added["plan_id"])
    planning.update(
        tmp_path,
        PLANNING_PATH,
        plan_id=added["plan_id"],
        changes={"priority": "high"},
        expected_container_hash=records.lifecycle_guards(manifest, snapshot)[
            "expected_container_hash"
        ],
        expected_item_version=item.source.hash,
        why="raise the priority",
    )

    assert reads.paths == []
    assert _titles_in(_projection(tmp_path)) == _titles_in(before)


def test_a_plan_item_added_onto_existing_records_gains_its_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one plan-side case that must read: the item is new to the projection."""
    seed_vault(tmp_path)
    report_event(tmp_path, "Batch 1")
    due_state_module.reconcile(tmp_path)
    assert _projection(tmp_path) == {}
    reads = _DeltaReads(monkeypatch)

    queue_item(tmp_path, "Batch 1")

    assert reads.paths == [RECORDS_PATH]
    delta = _projection(tmp_path)
    assert _titles_in(delta) == {"Batch 1"}
    due_state_module.reconcile(tmp_path)
    assert _projection(tmp_path) == delta


def test_a_newly_bound_records_collection_registers_its_binding_on_first_append(
    tmp_path: Path,
) -> None:
    """A binding declared after the last full pass must not wait for the next one.

    The plan side consults the persisted index and never walks, so a collection
    the index has not heard of would be invisible from the plan side until the
    next `reconcile` -- the record side resolving and registering it is what
    keeps that walk-free.
    """
    from exomem import records as records_module

    second = "Knowledge Base/Records/Late/_collection.md"
    seed_vault(tmp_path)
    queue_item(tmp_path, "Batch 1")
    due_state_module.reconcile(tmp_path)
    assert second not in (due_state_module.load(tmp_path) or {}).get("bindings", {})
    records_module.create_collection(
        tmp_path,
        second,
        records_manifest(collection_id="7c1d9e04-5f62-4a88-9b31-2e6a0c4d7f53"),
        why="log delivery events from a second source",
    )

    report_event(tmp_path, "Batch 1", collection=second)

    bindings = (due_state_module.load(tmp_path) or {}).get("bindings") or {}
    assert [row["planning"] for row in bindings[second]] == [PLANNING_PATH]
    assert {row["records"] for row in bindings[PLANNING_PATH]} == {RECORDS_PATH, second}
    assert _titles_in(_projection(tmp_path)) == {"Batch 1"}


def _titles_in(projection: dict) -> set[str]:
    titles = set()
    for bucket in projection.values():
        for row in due_state_module._unbucket(bucket):
            titles.add(str((row.get("component") or {}).get("item_title") or ""))
    return titles
