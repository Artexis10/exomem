"""Recent promotions: who is told about a promotion, and when it stops being news.

An entry is source fact in the review-state ledger; these tests drive the real
writers and the real due-state carrier and change only the caller identity.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import command_surface, commands, promotions
from exomem import due_state as due_state_module

CATEGORY = promotions.FAMILY


@pytest.fixture(autouse=True)
def _clean_emission_state():
    due_state_module.reset_emission_state()
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
    assert [row["subjects"] for row in edges] == [
        [{"subject": second, "predicate": "supports", "target": first}]
    ]


def test_a_registry_save_adds_one_entry_for_all_the_keys_it_adds(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as(monkeypatch, "session-a")
    _save_relation(vault, "vault.hosts", parent="relates_to")

    rows = [row for row in _ledger(vault) if row["kind"] == "registry"]
    assert len(rows) == 1
    assert rows[0]["subjects"] == [{"registry": "relations", "key": "vault.hosts"}]
    assert rows[0]["new_family"] is False, "a relation under a parent is not a new family"

    _as(monkeypatch, "session-b")
    assert _count(vault) == 1


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
