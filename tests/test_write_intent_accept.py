"""The accept path of the write-intent journal: a contended eligible write is recorded and answered
within the accept deadline, never refused busy and never committed out of order.

Nothing applies intents in this change set yet, so each case ends at the answer or at the journal row.
"""

from __future__ import annotations

import json
import multiprocessing
import threading
import time
from pathlib import Path

import pytest
from held_boundary import held_boundary
from test_mutation_lock import _process_hold

from exomem import (
    commands,
    intent_accept,
    intent_applier,
    mutation_journal,
    state_paths,
    write_intents,
    writer_lease,
)
from exomem.cli_ops import OpError
from exomem.commands import op_remember
from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope

_DEADLINE = intent_accept.ACCEPT_DEADLINE_SECONDS


@pytest.fixture(autouse=True)
def _journal_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_WRITE_INTENTS", "on")
    # These cases end at the answer or the journal row; the applier has its own file.
    monkeypatch.setattr(intent_applier, "ensure_running", lambda *args, **kwargs: None)
    yield
    writer_lease.reset_managers_for_tests()


def _command(name: str):
    return next(c for c in commands.product_commands_for("mcp") if c.name == name)


def _draft_kwargs(vault: Path, number: int) -> dict:
    """A `remember` call that carries its reviewed draft, so its commit needs no second review."""
    kwargs = {
        "content": (
            f"# Accepted save {number}\n\n## Observations\n\n"
            "- [operating constraint] Keep capture off the client #reliability\n"
        ),
        "title": f"Accepted save {number}",
        "slug": f"accepted-save-{number}",
        "suggestions": False,
    }
    validation = op_remember(vault, validate_only=True, **kwargs)
    kwargs.update(
        draft_id=validation["draft_id"],
        draft_hash=validation["draft_hash"],
        draft_token=validation["draft_token"],
        relation_disposition="reviewed_none",
        relation_review_hash=validation["draft_hash"],
        relation_review_reason="No honest relation exists in the isolated fixture.",
    )
    return kwargs


def _manager() -> writer_lease.LeaseManager:
    return writer_lease.LeaseManager(writer_lease.LeaseConfig.from_env())


def _remember(manager, vault: Path, kwargs: dict, **options):
    with request_scope(owner_principal(surface="mcp")):
        return manager.invoke(_command("remember"), (vault,), kwargs, **options)


def _journal(manager, vault: Path) -> write_intents.WriteIntentJournal:
    journal = manager._intent_journal(vault)
    assert journal is not None
    return journal


def _log_rows(since: int = 0) -> list[dict]:
    """The rows of `logs/mutations.jsonl` after the first `since`, so a shared log dir cannot leak in an earlier run."""
    path = mutation_journal.journal_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()][since:]


def test_a_short_hold_commits_inline_and_a_long_hold_is_accepted_within_the_deadline(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 5 s acquire used to outlast the answer a caller can use; a 120 s holder used to end in busy."""
    manager = _manager()
    logged = len(_log_rows())
    state_root = str(manager.config.state_dir)
    uncontended_started = time.monotonic()
    first = _remember(manager, vault, _draft_kwargs(vault, 0), implicit_idempotency_scope="principal:t")
    uncontended = time.monotonic() - uncontended_started
    assert first["state"] == "committed"

    context = multiprocessing.get_context("spawn")
    holds = []
    try:
        # A second process holds the boundary for one second, then lets go inside the accept deadline.
        short = _spawn_holder(context, state_root, vault, holds)
        threading.Timer(1.0, short["release"].set).start()
        started = time.monotonic()
        committed = _remember(manager, vault, _draft_kwargs(vault, 1), implicit_idempotency_scope="principal:t")
        assert time.monotonic() - started <= _DEADLINE + uncontended + 0.5
        assert committed["state"] == "committed"
        assert (vault / committed["path"]).is_file()

        # A holder that outlasts the deadline: the write is accepted, nothing is written.
        long = _spawn_holder(context, state_root, vault, holds)
        started = time.monotonic()
        accepted = _remember(manager, vault, _draft_kwargs(vault, 2), implicit_idempotency_scope="principal:t")
        elapsed = time.monotonic() - started
        assert elapsed <= _DEADLINE + uncontended + 0.5
        assert accepted["state"] == "accepted"
        assert accepted["committed"] is False and accepted["mutated"] is False
        assert not list(vault.rglob("accepted-save-2*.md"))
        row = next(r for r in _log_rows(logged) if r["outcome"] == "accepted")
        assert row["boundary_wait_ms"] <= _DEADLINE * 1000 + 100
        long["release"].set()
    finally:
        for hold in holds:
            hold["release"].set()
            hold["process"].join(timeout=30)


def _spawn_holder(context, state_root: str, vault: Path, holds: list) -> dict:
    attempting, entered, release = context.Event(), context.Event(), context.Event()
    process = context.Process(
        target=_process_hold, args=(state_root, str(vault), attempting, entered, release)
    )
    process.start()
    hold = {"process": process, "release": release}
    holds.append(hold)
    assert entered.wait(30)
    return hold


def test_a_resend_after_accept_returns_the_same_intent_and_records_no_second_one(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent that resends after a lost reply must not queue its write twice."""
    with held_boundary(vault, monkeypatch, release_when_refused=False):
        manager = _manager()
        kwargs = _draft_kwargs(vault, 3)
        first = _remember(manager, vault, kwargs, idempotency_key="resend-key")
        again = _remember(manager, vault, kwargs, idempotency_key="resend-key")
        assert first["state"] == again["state"] == "accepted"
        assert first["intent_id"] == again["intent_id"]
        assert len(_journal(manager, vault).non_terminal()) == 1


def test_a_resend_after_the_intent_committed_returns_its_terminal_and_no_second_intent(vault: Path) -> None:
    """With the intent finished, the resend answers with the terminal that the applier recorded."""
    manager = _manager()
    kwargs = _draft_kwargs(vault, 4)
    with held_boundary_in_thread(vault, manager):
        first = _remember(manager, vault, kwargs, idempotency_key="done-key")
    assert first["state"] == "accepted"
    journal = _journal(manager, vault)
    terminal = {"ok": True, "state": "committed", "status": "committed", "terminal": True, "path": "x.md"}
    journal.finish(first["intent_id"], "committed", terminal=terminal)

    again = _remember(manager, vault, kwargs, idempotency_key="done-key")
    assert again == {**terminal, "intent_id": first["intent_id"]}
    assert not journal.non_terminal()
    assert not list(vault.rglob("accepted-save-4*.md"))


def test_a_key_reused_for_different_input_is_refused_not_answered_with_the_other_intent(vault: Path) -> None:
    """A resend with different arguments is a different write, as it is without the journal."""
    manager = _manager()
    with held_boundary_in_thread(vault, manager):
        _remember(manager, vault, _draft_kwargs(vault, 5), idempotency_key="reuse-key")
        with pytest.raises(OpError) as refused:
            _remember(manager, vault, _draft_kwargs(vault, 6), idempotency_key="reuse-key")
    assert refused.value.code == "IDEMPOTENCY_KEY_REUSED"


class held_boundary_in_thread:  # noqa: N801 - reads as a context manager at its use sites
    """Hold the boundary from another manager until the block ends; the acquire timeout stays at the default."""

    def __init__(self, vault: Path, manager: writer_lease.LeaseManager):
        self.vault = vault
        self.holder = writer_lease.LeaseManager(manager.config)
        self.release = threading.Event()
        self.holding = threading.Event()

    def __enter__(self):
        def hold() -> None:
            with self.holder.mutation_guard(self.vault, request_id="holder", operation="semantic edit"):
                self.holding.set()
                self.release.wait(60)

        self.thread = threading.Thread(target=hold)
        self.thread.start()
        assert self.holding.wait(30)
        return self

    def __exit__(self, *exc) -> None:
        self.release.set()
        self.thread.join(30)


def test_with_the_journal_off_a_held_boundary_refuses_busy_as_before(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag is the rollback: off must leave every write path as it was."""
    monkeypatch.setenv("EXOMEM_WRITE_INTENTS", "off")
    monkeypatch.setenv("EXOMEM_CAPTURE_WAIT_SECONDS", "0.3")
    manager = _manager()
    with held_boundary_in_thread(vault, manager):
        with pytest.raises(OpError) as refused:
            _remember(manager, vault, _draft_kwargs(vault, 7), implicit_idempotency_scope="principal:t")
    assert refused.value.code == "MUTATION_BUSY"
    assert refused.value.details["cause"] == "capture_wait_exhausted"
    assert not (state_paths.vault_state_dir(vault) / write_intents.JOURNAL_FILENAME).exists()


def test_a_write_behind_a_queued_intent_waits_behind_it_while_other_commands_run(vault: Path) -> None:
    """A free boundary must not let a later write overtake an accepted one; unrelated work still runs."""
    manager = _manager()
    logged = len(_log_rows())
    with held_boundary_in_thread(vault, manager):
        first = _remember(manager, vault, _draft_kwargs(vault, 8), implicit_idempotency_scope="principal:a")
    assert first["state"] == "accepted"

    # The boundary is free now. Another principal has no dependency on the queued write.
    with request_scope(RequestPrincipal("principal:other", surface="mcp")):
        later = manager.invoke(
            _command("remember"), (vault,), _draft_kwargs(vault, 9), implicit_idempotency_scope="principal:b"
        )
    assert later["state"] == "accepted"
    assert not list(vault.rglob("accepted-save-9*.md"))
    assert [i.intent_id for i in _journal(manager, vault).non_terminal()] == [first["intent_id"], later["intent_id"]]

    # An ineligible command does not queue behind the intents.
    moved = next(c for c in commands.COMMANDS if c.name == "move_file")
    with request_scope(owner_principal(surface="mcp")):
        result = manager.invoke(
            moved,
            (vault,),
            {"old_path": "Handbooks/incident-severity-levels.md", "new_path": "Handbooks/moved-levels.md"},
            idempotency_key="move-while-queued",
        )
    assert result["state"] == "committed"
    assert (vault / "Handbooks/moved-levels.md").is_file()

    # The internal refusal that ordered the second write is never a row of the log or a busy count.
    rows = _log_rows(logged)
    assert "MUTATION_BUSY" not in {row["error_code"] for row in rows}
    assert {row["outcome"] for row in rows} <= {"accepted", "committed"}


def test_a_dependent_write_is_accepted_deferred_not_refused_for_its_unresolved_link(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B links to a page that only the waiting write A creates; validating B now would refuse it."""
    manager = _manager()
    with held_boundary_in_thread(vault, manager):
        first = _remember(manager, vault, _draft_kwargs(vault, 10), implicit_idempotency_scope="principal:a")
    assert first["state"] == "accepted" and first["validation"] == "complete"

    dependent = {
        "content": "# Follow-up\n\nBuilds on [[accepted-save-10]].\n",
        "title": "Follow-up",
        "slug": "accepted-follow-up",
        "suggestions": False,
    }
    answer = _remember(manager, vault, dependent, implicit_idempotency_scope="principal:a2")
    assert answer["state"] == "accepted"
    assert answer["validation"] == "deferred"
    assert not list(vault.rglob("accepted-follow-up*.md"))


def test_an_invalid_wide_write_returns_its_validation_error_and_records_no_intent(vault: Path) -> None:
    """A wide command is previewed before accept, so a bad draft is refused as it is without contention."""
    manager = _manager()
    create = next(c for c in commands.COMMANDS if c.name == "create_file")
    with held_boundary_in_thread(vault, manager):
        with request_scope(owner_principal(surface="mcp")):
            with pytest.raises(ValueError, match="INVALID_FRONTMATTER"):
                manager.invoke(
                    create,
                    (vault,),
                    {"path": "Knowledge Base/Notes/bad.md", "content": "---\nfoo: [unclosed\n---\n# bad\n"},
                    idempotency_key="bad-wide",
                )
        assert not _journal(manager, vault).non_terminal()


def test_a_wide_write_with_no_preview_is_accepted_with_validation_deferred(vault: Path) -> None:
    """`capture_source` text meets the boundary before its leaf and has no preview to run."""
    manager = _manager()
    capture = _command("capture_source")
    with held_boundary_in_thread(vault, manager):
        with request_scope(owner_principal(surface="mcp")):
            answer = manager.invoke(
                capture,
                (vault,),
                {"title": "Clip one", "content": "some captured text", "why_captured": "test"},
                idempotency_key="clip-one",
            )
    assert answer["state"] == "accepted"
    assert answer["validation"] == "deferred"


def test_a_capture_that_carries_files_is_not_journaled_until_its_inputs_can_be_staged(vault: Path) -> None:
    """Journaling its arguments alone would strand the fetch handles it needs, so it keeps today's behaviour."""
    capture = _command("capture_source")
    assert intent_accept.eligible(capture, {"title": "t", "content": "c"})
    assert not intent_accept.eligible(capture, {"title": "t", "files": [{"download_url": "https://example.invalid/a"}]})


def test_one_mixed_command_is_journaled_only_in_its_declared_modes() -> None:
    """A move through `manage_memory_file` must keep running synchronously."""
    manage = _command("manage_memory_file")
    assert intent_accept.eligible(manage, {"operation": "create", "path": "a.md"})
    assert intent_accept.eligible(manage, {"operation": "append", "path": "a.md"})
    assert not intent_accept.eligible(manage, {"operation": "move", "old_path": "a.md", "new_path": "b.md"})
    assert not intent_accept.eligible(manage, {"path": "a.md"})


def test_two_attempts_under_one_key_record_one_intent_and_both_callers_get_it(vault: Path) -> None:
    """The second attempt meets the first one's claim on the key; it must end with the first's intent."""
    manager = _manager()
    kwargs = _draft_kwargs(vault, 11)
    answers: list[dict] = []

    def send() -> None:
        answers.append(_remember(manager, vault, kwargs, idempotency_key="twin-key"))

    with held_boundary_in_thread(vault, manager):
        threads = [threading.Thread(target=send) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
    assert [a["state"] for a in answers] == ["accepted", "accepted"]
    assert answers[0]["intent_id"] == answers[1]["intent_id"]
    assert len(_journal(manager, vault).non_terminal()) == 1


def test_every_eligible_write_is_journaled_in_always_mode_even_with_a_free_boundary(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`always` exists so the applier tests can run through the accept path with no contention."""
    monkeypatch.setenv("EXOMEM_WRITE_INTENTS", "always")
    manager = _manager()
    answer = _remember(manager, vault, _draft_kwargs(vault, 12), implicit_idempotency_scope="principal:t")
    assert answer["state"] == "accepted"
    assert answer["validation"] == "deferred"
    assert not list(vault.rglob("accepted-save-12*.md"))


def test_a_full_journal_refuses_busy_with_its_cause_and_writes_nothing(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one refusal accept makes: a caller can tell a full journal from a busy vault."""
    monkeypatch.setenv("EXOMEM_WRITE_INTENT_MAX", "2")
    manager = _manager()
    with held_boundary_in_thread(vault, manager):
        _remember(manager, vault, _draft_kwargs(vault, 13), implicit_idempotency_scope="principal:a")
        with pytest.raises(OpError) as refused:
            _remember(manager, vault, _draft_kwargs(vault, 14), implicit_idempotency_scope="principal:b")
    assert refused.value.code == "MUTATION_BUSY"
    assert refused.value.details["cause"] == write_intents.CAUSE_FULL
    assert refused.value.details["committed"] is False
    assert refused.value.details["retry_after_ms"] > 0
    assert len(_journal(manager, vault).non_terminal()) == 1


def test_the_accepted_envelope_invites_a_poll_not_a_resend(vault: Path) -> None:
    """`retry_after_ms` on a success tells an agent to send the write again, which would queue it twice."""
    manager = _manager()
    with held_boundary_in_thread(vault, manager):
        answer = _remember(manager, vault, _draft_kwargs(vault, 15), implicit_idempotency_scope="principal:t")
    assert {"ok", "state", "terminal", "status", "committed", "mutated", "intent_id", "intent_ref"} <= set(answer)
    assert answer["ok"] is True and answer["terminal"] is False
    assert answer["intent_ref"] == f"exomem://write/{answer['intent_id']}"
    assert answer["poll_after_ms"] > 0
    assert "retry_after_ms" not in answer
    assert answer["validation"] in {"complete", "deferred"}
    assert "wait_seconds" in answer["next_action"]


def test_rest_answers_an_accepted_write_with_202_and_the_same_envelope(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 200 would read as committed; a 5xx would invite the retry the journal exists to avoid."""
    from test_rest_api import _client

    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    manager = _manager()
    with held_boundary_in_thread(vault, manager):
        response = client.post(
            "/api/remember",
            json={
                "note_type": "insight",
                "title": "Accepted over REST",
                "content": "# Accepted over REST\n\n## Claim\n\nA contended write is accepted.\n",
                "status": "draft",
            },
            headers={"Authorization": "Bearer sekret"},
        )
    assert response.status_code == 202, response.text
    data = response.json()["data"]
    assert response.json()["success"] is True
    assert data["state"] == "accepted" and "retry_after_ms" not in data
    assert not list(vault.rglob("accepted-over-rest*.md"))


def test_mcp_returns_an_accepted_write_as_a_normal_tool_result(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The envelope must survive the MCP tool path unchanged: no tool error, no `retry_after_ms`."""
    import asyncio

    from exomem import server

    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    mcp = server.build_server(require_auth=False)
    manager = _manager()
    with held_boundary_in_thread(vault, manager):
        with request_scope(owner_principal(surface="mcp")):
            result = asyncio.run(
                mcp.call_tool(
                    "remember",
                    {
                        "note_type": "insight",
                        "title": "Accepted over MCP",
                        "content": "# Accepted over MCP\n\n## Claim\n\nA contended write is accepted.\n",
                        "status": "draft",
                    },
                    run_middleware=False,
                )
            )
    payload = result.structured_content or json.loads(result.content[0].text)
    payload = payload.get("result", payload)
    assert payload["state"] == "accepted" and payload["terminal"] is False
    assert "retry_after_ms" not in payload
    assert not list(vault.rglob("accepted-over-mcp*.md"))
