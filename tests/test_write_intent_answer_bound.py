"""The accepted answer must reach the caller inside the accept window, even when the held boundary also slows the answer's own filter."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from test_write_intent_accept import _manager, held_boundary_in_thread
from test_write_intent_decisions import _edit_kwargs, _new_page

from exomem import commands, intent_applier, writer_lease
from exomem.governance.principal import owner_principal, request_scope

#: The accept deadline is 2.0 s; the rest of the bound is the leaf's own fixed work.
ANSWER_BOUND_SECONDS = 2.5


@pytest.fixture(autouse=True)
def _journal_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_WRITE_INTENTS", "on")
    monkeypatch.setattr(intent_applier, "ensure_running", lambda *args, **kwargs: None)
    yield
    writer_lease.reset_managers_for_tests()


def test_a_contended_edit_is_answered_accepted_inside_the_bound(vault: Path) -> None:
    """Each filter wait on the held boundary used to add up to 5 s after the accept window had closed."""
    manager = _manager()
    path = _new_page(manager, vault, 80)
    command = next(c for c in commands.product_commands_for("mcp") if c.name == "edit_memory")
    with held_boundary_in_thread(vault, manager):
        started = time.monotonic()
        with request_scope(owner_principal(surface="mcp")):
            answer = writer_lease.invoke_command(
                command, vault, implicit_idempotency_scope="principal:t", **_edit_kwargs(path, "Reworded")
            )
        elapsed = time.monotonic() - started
    print(f"ACCEPTED-ANSWER-SECONDS {elapsed:.3f}")
    assert answer["state"] == "accepted" and answer["intent_id"] and answer["poll_after_ms"]
    assert elapsed < ANSWER_BOUND_SECONDS
