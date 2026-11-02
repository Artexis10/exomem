"""The applier of the write-intent journal: an accepted write commits once, in acceptance order, as the
caller who sent it, through the command path a synchronous write takes.

Real threads and real second processes. Each case names the failure only it catches.
"""

from __future__ import annotations

import dataclasses
import multiprocessing
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from held_boundary import held_boundary
from test_connector_boundary import PRIVATE, configured_boundary  # noqa: F401 - the fixture
from test_write_intent_accept import (
    _draft_kwargs,
    _journal,
    _log_rows,
    _manager,
    _remember,
    held_boundary_in_thread,
)

from exomem import (
    commands,
    intent_accept,
    intent_applier,
    server_auth,
    state_paths,
    write_intents,
    writer_lease,
)
from exomem.cli_ops import OpError, error_dict
from exomem.governance import principal as principal_module
from exomem.governance.principal import owner_principal, request_scope
from exomem.mutation_terminal import split_response_detail


@pytest.fixture(autouse=True)
def _journal_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_WRITE_INTENTS", "on")
    yield
    writer_lease.reset_managers_for_tests()


@contextmanager
def _no_applier(monkeypatch: pytest.MonkeyPatch, **env: str):
    """Accept writes without starting an applier, so a case decides when intents are applied."""
    with monkeypatch.context() as patch:
        patch.setattr(intent_applier, "ensure_running", lambda *args, **kwargs: None)
        for name, value in env.items():
            patch.setenv(name, value)
        yield


def _command(name: str):
    """A command as a caller reaches it; `create_file` is on no product surface, so the full registry serves second."""
    for registry in (commands.product_commands_for("mcp"), commands.COMMANDS):
        found = next((c for c in registry if c.name == name), None)
        if found is not None:
            return found
    raise LookupError(name)


def _wait_for(journal: write_intents.WriteIntentJournal, intent_id: str, states: tuple[str, ...], seconds: float = 30.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        intent = journal.get(intent_id)
        if intent is not None and intent.state in states:
            return intent
        time.sleep(0.05)
    raise AssertionError(f"intent {intent_id} never reached {states}; it is {journal.get(intent_id).state}")


def _seed(manager, vault: Path, name: str, kwargs: dict, who, *, key: str, validation: str = "complete"):
    """Record an intent as accept would, for a case that needs a row accept itself would not make."""
    command = _command(name)
    canonical, _ = split_response_detail(writer_lease._canonicalize_command_kwargs(kwargs))
    intent, created = _journal(manager, vault).accept(
        audience=who.audience_id,
        command=name,
        arguments=canonical,
        surface="mcp",
        principal=intent_accept.principal_snapshot(who),
        digest=writer_lease._command_digest(command, canonical),
        retry_key=key,
        window_seconds=600.0,
        validation=validation,
    )
    assert created
    return intent


def _applier_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name.startswith("write-intent-applier")]


def test_an_accepted_write_commits_once_when_the_boundary_frees_and_a_resend_gets_its_terminal(vault: Path) -> None:
    """Without an applier an accepted write is never applied; a second run would write the page twice."""
    manager = _manager()
    kwargs = _draft_kwargs(vault, 20)
    with held_boundary_in_thread(vault, manager):
        accepted = _remember(manager, vault, kwargs, idempotency_key="applies-once")
        assert accepted["state"] == "accepted"
        assert not list(vault.rglob("accepted-save-20*.md"))
    journal = _journal(manager, vault)
    done = _wait_for(journal, accepted["intent_id"], ("committed",))
    assert done.terminal["state"] == "committed"
    assert len(list(vault.rglob("accepted-save-20*.md"))) == 1
    assert done.arguments is None, "a committed intent keeps no arguments"

    again = _remember(manager, vault, kwargs, idempotency_key="applies-once")
    assert again == done.terminal
    assert len(list(vault.rglob("accepted-save-20*.md"))) == 1


def test_an_accepting_caller_inside_its_window_gets_the_terminal_the_moment_the_intent_ends(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caller waits on the applier's signal; without it a commit inside the window would still answer `accepted`.

    `always` mode journals the write. The test ends the intent itself, as the applier does, so the
    answer's timing depends on the wake-up and not on how long this machine takes to commit a page.
    """
    manager = _manager()
    terminal = {"ok": True, "state": "committed", "status": "committed", "terminal": True, "path": "x.md"}

    def end_it() -> None:
        journal = _journal(manager, vault)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not journal.non_terminal():
            time.sleep(0.02)
        time.sleep(0.4)
        journal.finish(journal.non_terminal()[0].intent_id, "committed", terminal=terminal)
        intent_accept.notify_applier_progress()

    with _no_applier(monkeypatch, EXOMEM_WRITE_INTENTS="always"):
        ender = threading.Thread(target=end_it)
        ender.start()
        started = time.monotonic()
        answer = _remember(manager, vault, _draft_kwargs(vault, 21), idempotency_key="in-window")
        elapsed = time.monotonic() - started
        ender.join(30)
    assert answer == terminal
    assert elapsed < intent_accept.ACCEPT_DEADLINE_SECONDS + 1.0


def test_a_write_that_arrives_behind_a_queued_intent_commits_after_it(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordering check only holds the later write back; the applier must then commit both, earlier first."""
    manager = _manager()
    with _no_applier(monkeypatch):
        with held_boundary_in_thread(vault, manager):
            first = _remember(manager, vault, _draft_kwargs(vault, 22), implicit_idempotency_scope="principal:a")
        with request_scope(principal_module.RequestPrincipal("principal:other", surface="mcp")):
            later = manager.invoke(
                _command("remember"), (vault,), _draft_kwargs(vault, 23), implicit_idempotency_scope="principal:b"
            )
        assert first["state"] == later["state"] == "accepted"
        assert not list(vault.rglob("accepted-save-2[23]*.md"))

    intent_applier.ensure_running(manager, vault)
    journal = _journal(manager, vault)
    one = _wait_for(journal, first["intent_id"], ("committed", "conflicted"))
    two = _wait_for(journal, later["intent_id"], ("committed", "conflicted"))
    assert (one.state, two.state) == ("committed", "committed")
    assert one.terminal_at <= two.terminal_at
    assert len(list(vault.rglob("accepted-save-22*.md"))) == len(list(vault.rglob("accepted-save-23*.md"))) == 1


def test_a_dependent_write_accepted_deferred_commits_after_the_write_it_links_to(vault: Path) -> None:
    """B links to A, which only commits later; applying B first would refuse it for an unresolved link."""
    manager = _manager()
    dependent = {
        "content": (
            "# Follow-up\n\nBuilds on [[accepted-save-24]].\n\n## Observations\n\n"
            "- [operating constraint] Keep the follow-up behind its source #reliability\n"
        ),
        "title": "Follow-up",
        "slug": "accepted-follow-up-24",
        "suggestions": False,
    }
    with held_boundary_in_thread(vault, manager):
        first = _remember(manager, vault, _draft_kwargs(vault, 24), implicit_idempotency_scope="principal:a")
        second = _remember(manager, vault, dependent, implicit_idempotency_scope="principal:a2")
    assert second["validation"] == "deferred"
    journal = _journal(manager, vault)
    one = _wait_for(journal, first["intent_id"], ("committed", "conflicted", "rejected"))
    two = _wait_for(journal, second["intent_id"], ("committed", "conflicted", "rejected"))
    assert (one.state, two.state) == ("committed", "committed"), (one.conflict, two.terminal)
    assert one.terminal_at <= two.terminal_at
    assert len(list(vault.rglob("accepted-follow-up-24*.md"))) == 1


def test_a_resend_with_the_original_arguments_after_a_rewrite_gets_the_intent_not_a_key_clash(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The leaf may receive rewritten arguments, but K stays claimed with the caller's original digest."""
    manager = _manager()
    arguments = {"path": "Knowledge Base/Notes/rewrite-original.md", "content": "# Rewrite\n\nbody\n"}
    create = _command("create_file")

    def send():
        with request_scope(owner_principal(surface="mcp")):
            return manager.invoke(create, (vault,), dict(arguments), idempotency_key="rewrite-key")

    with _no_applier(monkeypatch):
        with held_boundary_in_thread(vault, manager):
            accepted = send()
        assert accepted["state"] == "accepted"
        journal = _journal(manager, vault)
        intent = journal.get(accepted["intent_id"])
        rewritten = {**intent.arguments, "path": "Knowledge Base/Notes/rewrite-moved.md"}
        # What a later reconciliation writes (design D5); the applier passes it to the leaf and nowhere else.
        with sqlite3.connect(journal.path) as conn:
            conn.execute(
                "UPDATE intents SET leaf_arguments = ? WHERE intent_id = ?",
                (write_intents._dump(rewritten), intent.intent_id),
            )

    intent_applier.ensure_running(manager, vault)
    done = _wait_for(journal, intent.intent_id, ("committed", "conflicted", "rejected"))
    assert done.state == "committed", (done.conflict, done.terminal)
    assert (vault / "Knowledge Base/Notes/rewrite-moved.md").is_file()
    assert not (vault / "Knowledge Base/Notes/rewrite-original.md").exists()

    assert send() == done.terminal


def test_an_applied_intent_whose_target_is_reserved_is_refused_as_the_synchronous_path_refuses(vault: Path) -> None:
    """The applier must run admission against the stored arguments, not trust that accept already did."""
    manager = _manager()
    arguments = {"path": "Knowledge Base/_Consolidation/run.json", "content": "secret"}
    with request_scope(owner_principal(surface="mcp")):
        with pytest.raises(OpError) as synchronous:
            writer_lease.invoke_command(_command("create_file"), vault, **arguments)
    intent = _seed(manager, vault, "create_file", arguments, owner_principal(surface="mcp"), key="reserved", validation="deferred")
    intent_applier.ensure_running(manager, vault)
    done = _wait_for(_journal(manager, vault), intent.intent_id, ("rejected", "conflicted", "committed"))
    assert done.state == "rejected"
    assert done.terminal["error"]["code"] == synchronous.value.code == "RESERVED_PATH"
    assert done.terminal["error"] == error_dict(synchronous.value)
    assert not (vault / arguments["path"]).exists()


def test_a_cell_that_turned_read_only_keeps_the_intent_queued_and_commits_it_when_it_lifts(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only cell refuses a write for a reason that passes; the intent must wait, not end."""
    from exomem import cloud_cell

    read_only = {"on": True}
    monkeypatch.setattr(cloud_cell, "cloud_read_only_enabled", lambda: read_only["on"])
    manager = _manager()
    kwargs = _draft_kwargs(vault, 26)
    intent = _seed(manager, vault, "remember", kwargs, owner_principal(surface="mcp"), key="read-only")
    with pytest.raises(OpError) as synchronous:
        with request_scope(owner_principal(surface="mcp")):
            writer_lease.invoke_command(_command("remember"), vault, **kwargs)
    intent_applier.ensure_running(manager, vault)
    journal = _journal(manager, vault)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and journal.get(intent.intent_id).wait_cause != synchronous.value.code:
        time.sleep(0.05)
    waiting = journal.get(intent.intent_id)
    assert waiting.state in ("accepted", "applying") and waiting.wait_cause == "CLOUD_CELL_READ_ONLY"
    assert not list(vault.rglob("accepted-save-26*.md"))

    read_only["on"] = False
    assert _wait_for(journal, intent.intent_id, ("committed", "conflicted")).state == "committed"


ISSUER = "https://memory.example.test"


def _session_principal():
    """A principal as an MCP login makes it: client binding, origin login and a verified authorization session."""
    from exomem.governance.authorization_session_lifecycle import AuthorizationSessionContext

    return principal_module.RequestPrincipal(
        "principal:session",
        surface="mcp",
        authorization_session_id="auth-1",
        purpose="notes",
        issuer_family="mcp-oauth:abc",
        verified_authorization_session=AuthorizationSessionContext(
            session_id="auth-1", principal_id="principal:session", issuer_family="mcp-oauth:abc", cell_id="c",
            logical_vault_id="v", keyring_id="k", credential_generation=1, expires_at=int(time.time()) + 3600,
        ),
        client_binding=principal_module.ClientBinding(ISSUER, "client-1"),
        origin_session=principal_module.OriginSessionBinding("login-1", "gen-1", f"{ISSUER}/mcp"),
    )


def test_a_snapshot_of_a_session_principal_rebuilds_to_the_same_principal(vault: Path) -> None:
    """The row's JSON snapshot must rebuild every nested field, or the applier would run as someone else."""
    from exomem import upload_tokens

    who = _session_principal()
    manager = _manager()
    intent = _seed(manager, vault, "create_file", {"path": "Knowledge Base/Notes/x.md", "content": "x"}, who, key="rebuild")
    stored = _journal(manager, vault).get(intent.intent_id).principal
    assert "administrative_ingress" not in stored
    assert upload_tokens._principal_from_values(stored) == dataclasses.replace(who, administrative_ingress=False)


def test_an_intent_whose_login_was_revoked_after_accept_is_conflicted_and_the_page_is_unchanged(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live login applies; once revoked, its queued write must not apply with its old authority."""
    from types import SimpleNamespace

    login = {"revoked": False, "bindings": []}

    async def validate_binding(*, session_id, generation, client_id):
        login["bindings"].append((session_id, generation, client_id))
        return None if login["revoked"] else object()

    authority = SimpleNamespace(issuer=ISSUER, audience=f"{ISSUER}/mcp", validate_binding=validate_binding)
    monkeypatch.setenv("EXOMEM_BASE_URL", ISSUER)
    monkeypatch.setattr(server_auth, "build_session_authority", lambda **kwargs: authority)
    who = _session_principal()
    manager = _manager()
    journal = _journal(manager, vault)

    live_args = {"path": "Knowledge Base/Notes/login-live.md", "content": "# Live\n\nbody\n"}
    live = _seed(manager, vault, "create_file", live_args, who, key="login-live")
    intent_applier.ensure_running(manager, vault)
    assert _wait_for(journal, live.intent_id, ("committed", "conflicted", "rejected")).state == "committed"
    assert login["bindings"] == [("login-1", "gen-1", "client-1")]

    login["revoked"] = True
    revoked_args = {"path": "Knowledge Base/Notes/login-revoked.md", "content": "# Revoked\n\nbody\n"}
    revoked = _seed(manager, vault, "create_file", revoked_args, who, key="login-revoked")
    intent_applier.ensure_running(manager, vault)
    done = _wait_for(journal, revoked.intent_id, ("conflicted", "committed", "rejected"))
    assert done.state == "conflicted" and done.conflict["cause"] == "authorization_revoked"
    assert not (vault / revoked_args["path"]).exists()


def test_an_intent_whose_authorization_session_expired_is_conflicted_as_expired(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An expired session is a different remedy from a revoked login, so the cause must say which."""
    from exomem.governance.authorization_session_lifecycle import AuthorizationSessionContext

    expired = AuthorizationSessionContext(
        session_id="s1", principal_id="p1", issuer_family="local", cell_id="c", logical_vault_id="v",
        keyring_id="k", credential_generation=1, expires_at=int(time.time()) - 60,
    )
    who = principal_module.RequestPrincipal(
        "principal:expiring", surface="mcp", verified_authorization_session=expired, issuer_family="local"
    )
    manager = _manager()
    arguments = {"path": "Knowledge Base/Notes/expired.md", "content": "# Expired\n\nbody\n"}
    intent = _seed(manager, vault, "create_file", arguments, who, key="expired")
    intent_applier.ensure_running(manager, vault)
    done = _wait_for(_journal(manager, vault), intent.intent_id, ("conflicted", "committed", "rejected"))
    assert done.state == "conflicted" and done.conflict["cause"] == "authorization_expired"
    assert not (vault / arguments["path"]).exists()


@pytest.mark.parametrize("target", [PRIVATE, "Knowledge Base/Notes/not-there.md"])
def test_a_restricted_principals_intent_outside_its_scope_ends_with_the_synchronous_error(
    vault: Path, configured_boundary, target: str  # noqa: F811
) -> None:
    """The applier re-derives nothing: the same boundary check runs for the recorded principal, and answers
    the same for a hidden page that exists and for a path that does not, so the terminal is no oracle."""
    _, authenticate = configured_boundary
    who = authenticate("limited")
    manager = _manager()
    arguments = {"path": target, "content": "# Overwritten\n\nbody\n"}
    before = (vault / PRIVATE).read_bytes()
    with request_scope(who):
        with pytest.raises(ValueError) as synchronous:
            writer_lease.invoke_command(_command("create_file"), vault, **arguments)
    intent = _seed(manager, vault, "create_file", arguments, who, key=f"restricted:{target}", validation="deferred")
    intent_applier.ensure_running(manager, vault)
    done = _wait_for(_journal(manager, vault), intent.intent_id, ("rejected", "conflicted", "committed"))
    assert done.state == "rejected"
    assert done.terminal["error"] == error_dict(synchronous.value)
    assert (vault / PRIVATE).read_bytes() == before
    assert not (vault / "Knowledge Base/Notes/not-there.md").exists()


def test_a_service_stop_during_an_apply_that_waits_for_the_boundary_returns_the_intent_to_accepted(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop must not strand an intent `applying`, which only a review item could then resolve."""
    manager = _manager()
    kwargs = _draft_kwargs(vault, 27)
    with held_boundary_in_thread(vault, manager) as held:
        accepted = _remember(manager, vault, kwargs, idempotency_key="shutdown-key")
        journal = _journal(manager, vault)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and journal.get(accepted["intent_id"]).attempts == 0:
            time.sleep(0.05)
        assert journal.get(accepted["intent_id"]).attempts >= 1, "the applier never reached the boundary"
        started = time.monotonic()
        intent_applier.stop_all(timeout=intent_applier.STOP_JOIN_SECONDS)
        assert time.monotonic() - started < intent_applier.STOP_JOIN_SECONDS
        stopped = journal.get(accepted["intent_id"])
        assert stopped.state == "accepted", "the stopped applier left the intent applying"
        assert not _applier_threads()
        held.release.set()

    # The next applier commits it once, with no review item.
    intent_applier.ensure_running(manager, vault)
    done = _wait_for(journal, accepted["intent_id"], ("committed", "conflicted"))
    assert done.state == "committed", done.conflict
    assert len(list(vault.rglob("accepted-save-27*.md"))) == 1


def test_with_the_journal_off_and_no_journal_file_nothing_starts_and_no_file_is_made(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off is the rollback: with no journal there is nothing to drain, so no thread starts and no file appears."""
    monkeypatch.setenv("EXOMEM_WRITE_INTENTS", "off")
    manager = _manager()
    assert intent_applier.ensure_running(manager, vault) is None
    assert intent_applier.ensure_running(manager, vault, kind=intent_applier.KIND_SERVICE) is None
    _remember(manager, vault, _draft_kwargs(vault, 28), implicit_idempotency_scope="principal:t")
    writer_lease.start_server_lifecycle()
    try:
        assert not _applier_threads()
    finally:
        writer_lease.stop_server_lifecycle()
    assert not (state_paths.vault_state_dir(vault) / write_intents.JOURNAL_FILENAME).exists()


def test_with_the_journal_off_accepted_writes_still_drain(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rolling the switch back must not strand intents already accepted: off accepts nothing new and drains the rest."""
    manager = _manager()
    who = owner_principal(surface="mcp")
    with _no_applier(monkeypatch, EXOMEM_WRITE_INTENTS="on"):
        intent = _seed(manager, vault, "remember", _draft_kwargs(vault, 29), who, key="stranded")
    monkeypatch.setenv("EXOMEM_WRITE_INTENTS", "off")
    assert intent_applier.ensure_running(manager, vault) is not None
    assert _wait_for(_journal(manager, vault), intent.intent_id, ("committed",)).state == "committed"


def test_the_service_lifecycle_starts_and_stops_the_applier_for_its_vault(vault: Path) -> None:
    """Accepted writes of a restarted service need an applier that no request has to start."""
    writer_lease.start_server_lifecycle()
    try:
        assert [thread.name for thread in _applier_threads()] == ["write-intent-applier-service"]
    finally:
        writer_lease.stop_server_lifecycle()
    assert not _applier_threads()


def _cli_applier_process(vault_root: str, started, report) -> None:
    """A journaling CLI process: it starts an applier, and reports how many intents it had committed when it ended."""
    manager = writer_lease.LeaseManager(writer_lease.LeaseConfig.from_env())
    applier = intent_applier.ensure_running(manager, Path(vault_root))
    started.set()
    assert applier is not None
    applier._thread.join(120)
    with sqlite3.connect(manager._intent_journal(Path(vault_root)).path) as conn:
        report.put(conn.execute("SELECT COUNT(*) FROM intents WHERE state = 'committed'").fetchone()[0])


def test_a_service_that_starts_while_a_cli_applies_takes_over_after_the_current_intent(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two appliers would apply out of order; a CLI that never yields would keep another process's intents on its own code."""
    manager = _manager()
    with _no_applier(monkeypatch, EXOMEM_WRITE_INTENTS="always"):
        ids = [
            _remember(manager, vault, _draft_kwargs(vault, number), implicit_idempotency_scope=f"principal:{number}")[
                "intent_id"
            ]
            for number in (30, 31, 32)
        ]
    journal = _journal(manager, vault)

    context = multiprocessing.get_context("spawn")
    started, report = context.Event(), context.Queue()
    with held_boundary_in_thread(vault, manager) as held:
        child = context.Process(target=_cli_applier_process, args=(str(vault), started, report))
        child.start()
        try:
            assert started.wait(60)
            # The CLI applier holds the lock and waits for the boundary; the service starts behind it.
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and journal.get(ids[0]).attempts == 0:
                time.sleep(0.05)
            assert journal.get(ids[0]).attempts >= 1, "the CLI applier never reached the boundary"
            service = intent_applier.ensure_running(manager, vault, kind=intent_applier.KIND_SERVICE)
            assert service is not None
            time.sleep(0.6)
            held.release.set()
            handed = report.get(timeout=120)
        finally:
            child.join(30)
            if child.is_alive():
                child.terminate()

    done = [_wait_for(journal, intent_id, ("committed", "conflicted")) for intent_id in ids]
    assert [intent.state for intent in done] == ["committed"] * 3, [intent.conflict for intent in done]
    assert [intent.terminal_at for intent in done] == sorted(intent.terminal_at for intent in done)
    assert 1 <= handed < 3, "the CLI applier should apply the intent in hand and hand the rest to the service"
    assert all(len(list(vault.rglob(f"accepted-save-{n}*.md"))) == 1 for n in (30, 31, 32))


def test_the_applier_dispatch_takes_no_accept_window_and_counts_no_busy_row(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A window on the applier would journal its own intent again; its waits must not read as client-visible busy."""
    logged = len(_log_rows())
    manager = _manager()
    seen = []
    real = writer_lease.LeaseManager._mutation_boundary

    def spy(self, *args, **kwargs):
        from exomem import mutation_lock

        seen.append((intent_accept.current_applier_dispatch() is not None, mutation_lock.current_accept_window()))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(writer_lease.LeaseManager, "_mutation_boundary", spy)
    with held_boundary(vault, monkeypatch, release_when_refused=False) as held:
        accepted = _remember(manager, vault, _draft_kwargs(vault, 29), idempotency_key="no-window")
        journal = _journal(manager, vault)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and journal.get(accepted["intent_id"]).attempts < 2:
            time.sleep(0.05)
        held.release.set()
    done = _wait_for(journal, accepted["intent_id"], ("committed", "conflicted"))
    assert done.state == "committed"
    applier_calls = [window for dispatching, window in seen if dispatching]
    assert applier_calls and all(window is None for window in applier_calls)
    rows = _log_rows(logged)
    busy = [row for row in rows if row.get("error_code") == "MUTATION_BUSY"]
    assert busy == [], (busy, accepted["request_id"])
