"""Recent promotions: who is told about a promotion, and when it stops being news.

An entry is source fact in the review-state ledger; these tests drive the real
writers and the real due-state carrier and change only the caller identity.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from test_vocabulary_registries import _served

from exomem import command_surface, commands, promotions
from exomem import due_state as due_state_module

CATEGORY = promotions.FAMILY


@pytest.fixture(autouse=True)
def _clean_emission_state():
    from exomem.governance.principal import library_scope

    due_state_module.reset_emission_state()
    with library_scope():
        yield
    due_state_module.reset_emission_state()


def _as(monkeypatch: pytest.MonkeyPatch, session: str | None) -> None:
    """Run the next calls as one MCP session, or as a process outside any MCP call."""
    monkeypatch.setattr(
        command_surface,
        "mcp_caller_identity",
        lambda: {
            "client_name": None, "client_version": None, "app_id": None,
            "transport": "stdio" if session else None, "session_id": session,
        },
    )


def _create_entity(vault: Path, name: str = "Tidal Ledger", entity_type: str = "concept") -> str:
    created = commands.op_connect_memory(
        vault,
        operation="create-entity",
        entity_type=entity_type,
        name=name,
        summary="A synthetic accounting concept used to exercise promotions.",
    )
    return created["path"]


def _count(vault: Path) -> int:
    block = due_state_module.served(vault)
    return int(((block or {}).get("categories") or {}).get(CATEGORY, 0))


def _deliver(vault: Path) -> int:
    """Serve and deliver the block as the current caller does; the count it carried."""
    block = due_state_module.served(vault)
    due_state_module.mark_emitted(block, vault_root=vault)
    return int(((block or {}).get("categories") or {}).get(CATEGORY, 0))


def test_an_entry_is_hidden_from_its_creator_seen_once_elsewhere_and_then_spent(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as(monkeypatch, "session-a")
    _create_entity(vault)
    assert _count(vault) == 0, "the creating session never counts its own entry"

    _as(monkeypatch, "session-b")
    assert _deliver(vault) == 1, "another conversation counts it once"

    _as(monkeypatch, "session-c")
    assert _count(vault) == 0, "delivery to another conversation settled it for everyone"


def test_a_hook_or_cli_process_never_spends_the_notice(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as(monkeypatch, "session-a")
    _create_entity(vault)

    _as(monkeypatch, None)
    assert _deliver(vault) == 1, "a process outside MCP is shown the entry"

    _as(monkeypatch, "session-b")
    assert _deliver(vault) == 1, "and the owner's next conversation still counts it"


def test_the_creating_session_does_not_spend_its_own_notice(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as(monkeypatch, "session-a")
    _create_entity(vault)
    assert _deliver(vault) == 0

    _as(monkeypatch, "session-b")
    assert _count(vault) == 1


def test_the_compact_bootstrap_hint_for_a_promotion_delivers_its_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `next` hint of the counts summary, followed as written, serves the entry it counts."""
    _vault, call = _served(tmp_path, monkeypatch)
    _as(monkeypatch, "session-a")
    call("change_connections", {"operation": {
        "kind": "create-entity", "entity_type": "concept", "name": "Tidal Ledger",
        "summary": "A synthetic accounting concept used to exercise promotions.",
    }})

    _as(monkeypatch, "session-b")
    hint = call("bootstrap", {})["due_state"]["next"]
    call_node = ast.parse(hint, mode="eval").body
    arguments = {keyword.arg: ast.literal_eval(keyword.value) for keyword in call_node.keywords}
    followed = call(call_node.func.id, arguments)

    assert "error" not in followed, (hint, followed)
    assert [row["category"] for row in followed["due_state"]["top"]] == [CATEGORY], (hint, followed)


@pytest.mark.parametrize("obligation_kind", ["source-backlog", "authored"])
def test_obligations_cannot_starve_the_once_delivered_promotion_notice(
    vault: Path, monkeypatch: pytest.MonkeyPatch, obligation_kind: str,
) -> None:
    from test_due_state_projection import _prediction
    from test_source_backlog_projection import _import_sources

    if obligation_kind == "source-backlog":
        _import_sources(vault, due_state_module.TOP_LIMIT)
    else:
        for ordinal in range(due_state_module.TOP_LIMIT):
            _prediction(vault, f"forecast-{ordinal}", check_by="2026-01-01")
    due_state_module.reconcile(vault)
    _as(monkeypatch, "creator")
    _create_entity(vault)

    _as(monkeypatch, "reader")
    block = due_state_module.served(vault)
    assert block is not None
    assert any(row["category"] == CATEGORY for row in block["top"])
    due_state_module.mark_emitted(block, vault_root=vault)
    _as(monkeypatch, "later-reader")
    assert _count(vault) == 0, "the visible notice was delivered despite the outstanding work"


# --------------------------------------------------------------------------- #
# What counts as a promotion
# --------------------------------------------------------------------------- #

ENTITIES = "Knowledge Base/Entities/Concepts"
NOTE = "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md"


def _add_relation(vault: Path, path: str, bullet: str) -> None:
    """Author one `## Relations` bullet on an existing page through the ordinary editor."""
    from exomem.vault import parse_frontmatter

    _frontmatter, body, _text = parse_frontmatter((vault / path).read_text(encoding="utf-8"))
    section = "" if "## Relations" in body else "\n## Relations\n"
    commands.op_edit_memory(
        vault,
        path,
        "Author a synthetic relation.",
        operation={"kind": "replace_body", "new_body": f"{body.rstrip()}\n{section}{bullet}\n"},
    )


def _save_relation(vault: Path, key: str, *, parent: str) -> dict:
    from exomem.governance.principal import library_scope

    with library_scope():
        inspected = commands.op_schema_memory(vault, subject="relations", operation="inspect")
        return commands.op_schema_memory(
            vault,
            subject="relations",
            operation="save",
            proposal={"upsert": {key: {"parent": parent, "description": "A synthetic relation."}}},
            expected_hash=inspected["content_hash"],
            why="Register a synthetic relation.",
        )


def _ledger(vault: Path) -> list[dict]:
    from exomem import review_state

    return list(review_state.ReviewStateStore(vault).load()["promotions"].values())


def _ledger_ids(vault: Path) -> list[str]:
    from exomem import review_state

    return list(review_state.ReviewStateStore(vault).load()["promotions"])


def test_an_edge_between_two_entities_is_an_entry_and_one_from_a_note_is_not(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as(monkeypatch, "session-a")
    first = _create_entity(vault, "Tidal Ledger")
    second = _create_entity(vault, "Harbour Reconciliation")
    before = len(_ledger(vault))

    _add_relation(vault, NOTE, f"- supports [[{first.removesuffix('.md')}]]")
    assert len(_ledger(vault)) == before, "an edge from a note is not a promotion"

    _add_relation(vault, second, f"- supports [[{first.removesuffix('.md')}]]")
    edges = [row for row in _ledger(vault) if row["kind"] == "edge"]
    assert len(edges) == 1
    subject = edges[0]["subjects"][0]
    assert {key: subject[key] for key in ("subject", "predicate", "target")} == {
        "subject": second, "predicate": "supports", "target": first,
    }
    from exomem import memory_refs

    assert subject["source_ref"] == memory_refs.ref_from_markdown((vault / second).read_text())
    assert subject["target_ref"] == memory_refs.ref_from_markdown((vault / first).read_text())
    assert subject["introduction"]["source_ref"] == subject["source_ref"]


def test_a_registry_save_adds_one_entry_for_all_the_keys_it_adds(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as(monkeypatch, "session-a")
    _save_relation(vault, "vault.hosts", parent="relates_to")

    rows = [row for row in _ledger(vault) if row["kind"] == "registry"]
    assert len(rows) == 1
    assert len(rows[0]["subjects"]) == 1
    subject = rows[0]["subjects"][0]
    assert subject["registry"] == "relations"
    assert subject["key"] == "vault.hosts"
    assert subject["introduced_version"]
    assert subject["after_hash"]
    assert rows[0]["new_family"] is False, "a relation under a parent is not a new family"

    _as(monkeypatch, "session-b")
    assert _count(vault) == 1


def test_a_parentless_type_leads_the_served_notice_and_carries_new_family(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new family root outranks an older ordinary entry in the notice a reader sees."""
    from exomem.governance.principal import library_scope

    _as(monkeypatch, "session-a")
    _create_entity(vault)
    with library_scope():
        inspected = commands.op_schema_memory(vault, subject="entity-types", operation="inspect")
        saved = commands.op_schema_memory(
            vault, subject="entity-types", operation="save",
            proposal={"upsert": {"venue": {
                "label": "Venue", "guidance": "A synthetic meeting place.", "attributes": {"folder": "Venues"},
            }}},
            expected_hash=inspected["content_hash"], why="Register a synthetic root type.",
        )
    assert saved["valid"], saved

    _as(monkeypatch, "session-b")
    top = due_state_module.served(vault)["top"]
    promoted = [row for row in top if row["category"] == CATEGORY]
    assert len(promoted) == 2
    assert promoted[0].get("new_family") is True, "the root type is served ahead of the older entity"
    assert "new_family" not in promoted[1]


def test_a_public_entry_dismissed_through_the_triage_door_is_settled(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as(monkeypatch, "session-a")
    _create_entity(vault)
    (promotion_id,) = [row for row in _ledger_ids(vault)]
    ref = promotions.promotion_ref(promotion_id)

    _as(monkeypatch, "session-b")
    assert _count(vault) == 1
    commands.op_triage_memory(
        vault, ref=ref, action="dismiss", why="Not worth tracking.",
        expected_fingerprint=promotions.identity_fingerprint(promotion_id),
    )

    assert _count(vault) == 0
    assert promotions.served_rows(vault) == []


def test_a_dismissed_entry_stays_dismissed_when_its_page_is_edited_and_linked_again(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review fingerprint is the promotion's identity, not a hash of the page or its dependants."""
    from exomem.vault import parse_frontmatter

    _as(monkeypatch, "session-a")
    entity = _create_entity(vault, "Tidal Ledger")
    (promotion_id,) = _ledger_ids(vault)
    fingerprint = promotions.identity_fingerprint(promotion_id)
    assert re.fullmatch(r"[0-9a-f]{24}", fingerprint)
    _as(monkeypatch, "session-b")
    commands.op_triage_memory(
        vault, ref=promotions.promotion_ref(promotion_id), action="dismiss", why="Not worth tracking.",
        expected_fingerprint=fingerprint,
    )

    _frontmatter, body, _text = parse_frontmatter((vault / entity).read_text(encoding="utf-8"))
    commands.op_edit_memory(
        vault, entity, "Edit the promoted page.",
        operation={"kind": "replace_body", "new_body": f"{body.rstrip()}\nA later synthetic sentence.\n"},
    )
    _add_relation(vault, NOTE, f"- supports [[{entity.removesuffix('.md')}]]")

    assert promotions.identity_fingerprint(promotion_id) == fingerprint
    _as(monkeypatch, "session-c")
    assert _count(vault) == 0, "the edit and the new inbound link did not reopen the entry"


def _record_after_loading(vault: Path, loaded, finish, ready, proceed) -> None:
    from exomem import review_state
    from exomem.governance.principal import library_scope

    original = review_state.ReviewStateStore.load

    def paused_load(store, **kwargs):
        payload = original(store, **kwargs)
        loaded.set()
        assert finish.wait(10)
        return payload

    review_state.ReviewStateStore.load = paused_load
    with library_scope():
        ready.set()
        assert proceed.wait(60)
        assert promotions.record(vault, "entity", [{"path": "second.md"}], write="second")


def _settle_from_another_process(
    vault: Path, promotion_id: str, started, finished, ready, proceed,
) -> None:
    from exomem.governance.principal import library_scope

    with library_scope():
        ready.set()
        assert proceed.wait(60)
        started.set()
        promotions.settle(vault, promotion_id, "settled by delivery")
        finished.set()


def test_another_process_record_cannot_erase_a_persisted_settlement(vault: Path) -> None:
    """Concurrent source recording must preserve the user's existing dismissal."""
    import multiprocessing

    from exomem import review_state

    first = promotions.record(vault, "entity", [{"path": "first.md"}], write="first")
    assert first is not None
    context = multiprocessing.get_context("spawn")
    loaded, finish, started, finished = (context.Event() for _ in range(4))
    recorder_ready, settler_ready, record, settle = (context.Event() for _ in range(4))
    recorder = context.Process(
        target=_record_after_loading, args=(vault, loaded, finish, recorder_ready, record),
    )
    settler = context.Process(
        target=_settle_from_another_process,
        args=(vault, first, started, finished, settler_ready, settle),
    )
    try:
        recorder.start()
        settler.start()
        # Finish process startup before the recorder's bounded critical pause.
        assert recorder_ready.wait(10)
        assert settler_ready.wait(10)
        record.set()
        assert loaded.wait(10)
        settle.set()
        assert started.wait(10)
        # Reproduce the old overwrite if settlement can commit against the paused preimage.
        finished.wait(1)
        finish.set()
        recorder.join(10)
        settler.join(10)
        assert recorder.exitcode == settler.exitcode == 0
        store = review_state.ReviewStateStore(vault)
        assert len(store.load()["promotions"]) == 2
        state, _decision = store.effective_state(first, promotions.identity_fingerprint(first))
        assert state == "dismissed"
    finally:
        finish.set()
        record.set()
        settle.set()
        for process in (recorder, settler):
            if process.pid is not None:
                process.join(10)
                if process.is_alive():
                    process.terminate()
                    process.join()


def test_a_vault_that_withholds_the_pages_counts_nothing_for_a_restricted_audience(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import egress as egress_module
    from exomem.governance import membership, policy
    from exomem.governance.principal import RequestPrincipal, request_scope

    _as(monkeypatch, "session-a")
    _create_entity(vault, "Tidal Ledger")
    _create_entity(vault, "Mira Voss", entity_type="person")
    gov = vault / "Knowledge Base" / "_Governance"
    (gov / "scopes").mkdir(parents=True)
    (gov / "rules").mkdir(parents=True)
    scope_id, rule_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV", "01ARZ3NDEKTSV4RRFFQ69G5FB0"
    (gov / "scopes" / "concepts.yaml").write_text(
        f'governance_version: 1\nid: {scope_id}\nname: Concepts\npaths: ["Entities/Concepts/**"]\n',
        encoding="utf-8",
    )
    (gov / "rules" / "concepts-external.yaml").write_text(
        f'governance_version: 1\nid: {rule_id}\nscope_ids: ["{scope_id}"]\n'
        "audience: external\nceiling: 0\n",
        encoding="utf-8",
    )
    policy._CACHE.clear()
    membership.clear_memo()

    _as(monkeypatch, "session-b")

    def served_for(audience: str | None):
        egress_module.clear_decision_memo()
        if audience is None:
            return due_state_module.served(vault)
        with request_scope(RequestPrincipal(audience_id=audience, surface="mcp")):
            return due_state_module.served(vault)

    external = served_for("external")

    assert external is not None
    assert external["categories"][CATEGORY] == 1, "only the visible promotion is counted"
    assert len([row for row in external["top"] if row["category"] == CATEGORY]) == 1


def test_an_entry_whose_promotion_is_gone_is_spent_and_a_restore_does_not_raise_it_again(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as(monkeypatch, "session-a")
    created = _create_entity(vault)
    page = vault / created
    saved = page.read_text(encoding="utf-8")

    _as(monkeypatch, "session-b")
    page.unlink()
    assert _count(vault) == 0, "the page is gone, so there is nothing to tell"

    page.write_text(saved, encoding="utf-8")
    assert _count(vault) == 0, "a settlement is sticky: restoring the page is not news"

    _as(monkeypatch, "session-a")
    _create_entity(vault, "Harbour Reconciliation")
    _as(monkeypatch, "session-b")
    assert _count(vault) == 1, "a later write earns its own entry"


# --------------------------------------------------------------------------- #
# Where a promotion came from
# --------------------------------------------------------------------------- #


def _log_entry(vault: Path, entity_path: str) -> str:
    """The log entry the entity creation wrote, from its header to the next one."""
    log = (vault / "Knowledge Base" / "log.md").read_text(encoding="utf-8")
    title = entity_path.removeprefix("Knowledge Base/").removesuffix(".md")
    start = log.index(f"link | {title}")
    end = log.find("\n## [", start + 1)
    return log[start : end if end != -1 else len(log)]


def test_a_created_entity_names_the_page_that_led_to_it_in_its_log_entry(vault: Path) -> None:
    created = commands.op_connect_memory(
        vault, operation="create-entity", entity_type="concept", name="Tidal Ledger",
        summary="A synthetic accounting concept used to exercise provenance.", ref=NOTE,
    )["path"]

    assert f"Origin: {NOTE}." in _log_entry(vault, created)


def test_an_origin_the_caller_cannot_read_is_unknown_and_never_refuses(vault: Path) -> None:
    from exomem.governance import egress as egress_module
    from exomem.governance import membership, policy
    from exomem.governance.principal import RequestPrincipal, request_scope

    gov = vault / "Knowledge Base" / "_Governance"
    (gov / "scopes").mkdir(parents=True)
    (gov / "rules").mkdir(parents=True)
    scope_id, rule_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV", "01ARZ3NDEKTSV4RRFFQ69G5FB0"
    (gov / "scopes" / "insights.yaml").write_text(
        f'governance_version: 1\nid: {scope_id}\nname: Insights\npaths: ["Notes/Insights/**"]\n',
        encoding="utf-8",
    )
    (gov / "rules" / "insights-external.yaml").write_text(
        f'governance_version: 1\nid: {rule_id}\nscope_ids: ["{scope_id}"]\n'
        "audience: external\nceiling: 0\n",
        encoding="utf-8",
    )
    policy._CACHE.clear()
    membership.clear_memo()
    egress_module.clear_decision_memo()
    missing = "Knowledge Base/Notes/Insights/a-page-that-was-never-written.md"

    def create_as_external(name: str, ref: str) -> dict:
        with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
            return commands.op_connect_memory(
                vault, operation="create-entity", entity_type="concept", name=name,
                summary="A synthetic accounting concept used to exercise provenance.", ref=ref,
            )

    withheld_result = create_as_external("Tidal Ledger", NOTE)
    missing_result = create_as_external("Harbour Reconciliation", missing)

    for result in (withheld_result, missing_result):
        assert result["path"].startswith("Knowledge Base/Entities/Concepts/"), "the entity was created"
        assert "never-written" not in repr(result) and NOTE not in repr(result)
        assert result.keys() == withheld_result.keys()
        assert result["warnings"] == []
        entry = _log_entry(vault, result["path"])
        assert "Origin: unknown." in entry
        assert NOTE not in entry and missing not in entry
    assert commands._creation_origin(vault, NOTE)["path"] == NOTE, "the owner reads the page"


# --------------------------------------------------------------------------- #
# How entries group and who can silence them
# --------------------------------------------------------------------------- #


def test_one_bulk_call_adds_one_entry_per_kind_and_the_next_call_its_own(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import writer_lease

    _as(monkeypatch, "session-a")
    request = {"id": "request-one"}
    monkeypatch.setattr(writer_lease, "active_mutation_request_id", lambda: request["id"])
    for name in ("Tidal Ledger", "Harbour Reconciliation", "Salt Ledger"):
        _create_entity(vault, name)
    assert [len(row["subjects"]) for row in _ledger(vault) if row["kind"] == "entity"] == [3]

    request["id"] = "request-two"
    _create_entity(vault, "Anchor Ledger")

    sizes = [len(row["subjects"]) for row in _ledger(vault) if row["kind"] == "entity"]
    assert sorted(sizes) == [1, 3]


def test_a_family_the_owner_has_quieted_is_not_counted(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import review_state

    _as(monkeypatch, "session-a")
    _create_entity(vault)
    review_state.ReviewStateStore(vault).set_disposition(
        CATEGORY, "quiet", why="too_frequent: promotions arrive faster than they are read"
    )

    _as(monkeypatch, "session-b")
    assert _count(vault) == 0
