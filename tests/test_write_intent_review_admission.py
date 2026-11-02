"""A restricted client of the owner's audience must not see or decide another client's write on a page it cannot read."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from test_connector_boundary import PRIVATE, PUBLIC
from test_connector_boundary import configured_boundary as configured_boundary  # noqa: F401
from test_write_intent_accept import _journal, _manager

from exomem import commands, intent_applier, writer_lease
from exomem.governance.principal import request_scope


@pytest.fixture(autouse=True)
def _journal_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_WRITE_INTENTS", "on")
    monkeypatch.setattr(intent_applier, "ensure_running", lambda *args, **kwargs: None)
    yield
    writer_lease.reset_managers_for_tests()


def _conflicted_on(manager, vault: Path, who, path: str, key: str, cause: str = "basis_changed"):
    journal = _journal(manager, vault)
    intent, _ = journal.accept(
        audience=who.audience_id,
        command="edit_memory",
        arguments={"path": path, "operation": {"expected_hash": "0" * 64}},
        surface="mcp",
        principal={"audience_id": who.audience_id},
        digest=key,
        retry_key=key,
        window_seconds=600.0,
        validation="complete",
        planned_paths=[path],
        target_hashes={},
    )
    assert journal.begin_apply(intent.intent_id)
    journal.finish(intent.intent_id, "conflicted", conflict={"cause": cause})
    return intent.intent_id


def test_a_limited_client_cannot_see_retry_or_discard_the_full_clients_intent_on_a_private_page(
    configured_boundary, vault: Path  # noqa: F811
) -> None:
    """Both clients share the owner audience; only the client binding keeps the limited one off the private page."""
    _, authenticate = configured_boundary
    limited, full = authenticate("limited"), authenticate("full")
    manager = _manager()
    private_id = _conflicted_on(manager, vault, full, PRIVATE, "private-edit")
    public_id = _conflicted_on(manager, vault, full, PUBLIC, "public-edit")
    before = hashlib.sha256((vault / PRIVATE).read_bytes()).hexdigest()
    private_ref, public_ref = f"exomem://write/{private_id}", f"exomem://write/{public_id}"
    with request_scope(limited):
        with pytest.raises(ValueError, match="REVIEW_ITEM_NOT_FOUND"):
            commands.op_review_memory(vault, mode="item", ref=private_ref)
        with pytest.raises(ValueError, match="REVIEW_ITEM_NOT_FOUND"):
            commands.op_review_memory(vault, mode="write-intent", ref=private_ref)
        for action in ("retry-write", "discard-write"):
            with pytest.raises(ValueError, match="REVIEW_ITEM_NOT_FOUND"):
                commands.op_triage_memory(vault, private_ref, action, rebase=action == "retry-write")
        listed = commands.op_review_memory(vault, mode="write-intent")
        shown_public = commands.op_review_memory(vault, mode="item", ref=public_ref)
        attention = commands.op_review_memory(vault, mode="attention")
    assert [row["ref"] for row in listed["writes"]] == [public_ref]
    assert shown_public["state"] == "conflicted"
    assert attention["write_conflicts"] == {"count": 1}
    assert _journal(manager, vault).get(private_id).state == "conflicted"
    assert hashlib.sha256((vault / PRIVATE).read_bytes()).hexdigest() == before
    with request_scope(full):
        assert commands.op_review_memory(vault, mode="item", ref=private_ref)["state"] == "conflicted"
        assert len(commands.op_review_memory(vault, mode="write-intent")["writes"]) == 2


def test_later_intents_list_only_what_the_reader_may_see(
    configured_boundary, vault: Path  # noqa: F811
) -> None:
    """The overtaking list must not name a later write on a page the reader's client binding cannot admit."""
    _, authenticate = configured_boundary
    limited, full = authenticate("limited"), authenticate("full")
    manager = _manager()
    first = _conflicted_on(manager, vault, limited, PUBLIC, "first")
    # A later write on the same page that the limited client may see, and one that also plans the private page.
    visible = _conflicted_on(manager, vault, full, PUBLIC, "visible")
    journal = _journal(manager, vault)
    mixed, _ = journal.accept(
        audience=full.audience_id, command="edit_memory", arguments={"path": PUBLIC}, surface="mcp",
        principal={"audience_id": full.audience_id}, digest="mixed", retry_key="mixed",
        window_seconds=600.0, validation="complete", planned_paths=[PUBLIC, PRIVATE], target_hashes={},
    )
    with request_scope(limited):
        shown = commands.op_review_memory(vault, mode="item", ref=f"exomem://write/{first}")
    assert [row["ref"] for row in shown["later_intents"]] == [f"exomem://write/{visible}"]
    with request_scope(full):
        later = commands.op_review_memory(vault, mode="item", ref=f"exomem://write/{first}")["later_intents"]
    assert [row["ref"] for row in later] == [f"exomem://write/{visible}", f"exomem://write/{mixed.intent_id}"]


def test_retrying_an_unknown_outcome_is_refused_once_a_planned_page_changed_since_accept(vault: Path) -> None:
    """A changed page means the write probably landed; a second run would write it twice."""
    from test_write_intent_decisions import _new_page

    from exomem import intent_accept
    from exomem.governance.principal import owner_principal

    manager = _manager()
    owner = owner_principal(surface="mcp")
    unchanged_page, changed_page = _new_page(manager, vault, 81), _new_page(manager, vault, 82)
    journal = _journal(manager, vault)
    ids = {}
    for name, page in (("unchanged", unchanged_page), ("changed", changed_page)):
        intent, _ = journal.accept(
            audience=owner.audience_id, command="edit_memory", arguments={"path": page}, surface="mcp",
            principal=intent_accept.principal_snapshot(owner), digest=name, retry_key=name,
            window_seconds=600.0, validation="complete", planned_paths=[page],
            target_hashes=intent_accept.planned_hashes(vault, [page]),
        )
        assert journal.begin_apply(intent.intent_id)
        journal.finish(intent.intent_id, "conflicted", conflict={"cause": "outcome_unknown"})
        ids[name] = intent.intent_id
    (vault / changed_page).write_bytes((vault / changed_page).read_bytes() + b"\nLanded.\n")
    with request_scope(owner):
        with pytest.raises(ValueError, match="probably landed"):
            commands.op_triage_memory(vault, f"exomem://write/{ids['changed']}", "retry-write")
        allowed = commands.op_triage_memory(vault, f"exomem://write/{ids['unchanged']}", "retry-write")
    assert allowed["retried_as"]["state"] == "accepted"
    assert journal.get(ids["changed"]).state == "conflicted"
