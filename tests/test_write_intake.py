"""A save that meets a busy mutation boundary is queued durably and lands once.

Before the intake, an eligible save that waited out its bounded wait returned
`MUTATION_BUSY` and wrote nothing; it survived only if the agent sent it again.
These tests drive `invoke_command` against a boundary that another manager
holds, with the serving process's drainer running.
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from exomem import commands, write_intake, writer_lease
from exomem.cli_ops import OpError
from exomem.commands import op_remember
from exomem.governance.principal import RequestPrincipal, local_owner_principal, request_scope

_HOLD_SECONDS = 30.0
_LAND_SECONDS = 30.0


@pytest.fixture(autouse=True)
def _short_intake_wait(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_BUSY_INTAKE_AFTER_SECONDS", "0.3")
    yield
    writer_lease.reset_managers_for_tests()


@pytest.fixture
def managers(vault: Path, monkeypatch: pytest.MonkeyPatch):
    """The dispatcher's manager with a short acquire budget, and a separate holder of the boundary."""
    config = writer_lease.LeaseConfig(state_dir=vault.parent / "state")
    writer = writer_lease.LeaseManager(config, mutation_timeout_seconds=0.05)
    holder = writer_lease.LeaseManager(config)
    monkeypatch.setattr(writer_lease, "get_manager", lambda: writer)
    return writer, holder


@pytest.fixture
def drainer(vault: Path, managers):
    drainer = write_intake.start(vault)
    yield drainer
    write_intake.stop(vault)


class _Hold:
    """Hold the vault boundary from another manager until released."""

    def __init__(self, holder: writer_lease.LeaseManager, vault: Path) -> None:
        self.release = threading.Event()
        self.holding = threading.Event()

        def hold() -> None:
            with holder.mutation_guard(vault, request_id="long-writer", operation="semantic edit"):
                self.holding.set()
                assert self.release.wait(_HOLD_SECONDS)

        self.thread = threading.Thread(target=hold)

    def __enter__(self) -> _Hold:
        self.thread.start()
        assert self.holding.wait(_HOLD_SECONDS)
        return self

    def __exit__(self, *exc) -> None:
        self.release.set()
        self.thread.join(timeout=_HOLD_SECONDS)


def _command(name: str):
    return next(c for c in commands.product_commands_for("mcp") if c.name == name)


def _remember_kwargs(vault: Path, number: int) -> dict:
    kwargs = {
        "content": (
            f"# Queued save {number}\n\n## Observations\n\n"
            "- [operating constraint] A busy vault must not lose a save #reliability\n"
        ),
        "title": f"Queued save {number}",
        "slug": f"queued-save-{number}",
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


def _send(command, vault: Path, kwargs: dict, *, scope: str = "principal:session-a",
          principal: RequestPrincipal | None = None):
    with request_scope(principal or local_owner_principal(surface="mcp")):
        return writer_lease.invoke_command(command, vault, implicit_idempotency_scope=scope, **kwargs)


def _wait_for_state(vault: Path, ref: str, state: str) -> dict:
    deadline = time.monotonic() + _LAND_SECONDS
    while time.monotonic() < deadline:
        view = write_intake.read(vault, ref=ref)["queued_write"]
        if view["state"] == state:
            return view
        time.sleep(0.1)
    raise AssertionError(f"{ref} never reached {state}: {write_intake.read(vault, ref=ref)}")


def _pages_titled(vault: Path, title: str) -> list[Path]:
    return [path for path in vault.rglob("*.md") if f"title: {title}" in path.read_text(encoding="utf-8")
            or path.read_text(encoding="utf-8").startswith(f"# {title}")]


def test_a_save_during_a_long_hold_comes_back_queued_and_lands_once_after_it(
    vault: Path, managers, drainer
) -> None:
    _writer, holder = managers
    with _Hold(holder, vault):
        receipt = _send(_command("remember"), vault, _remember_kwargs(vault, 1))
        assert receipt["state"] == "queued"
        assert receipt["committed"] is False
        assert receipt["user_summary"].startswith("Queued, not saved yet")
        assert "title" not in receipt and "id" not in receipt
        ref = receipt["queued_write"]["ref"]
        assert write_intake.read(vault, ref=ref)["queued_write"]["state"] in {"queued", "applying"}
    applied = _wait_for_state(vault, ref, "applied")
    assert applied["outcome"]["user_summary"].startswith("Saved")
    assert "arguments" not in applied
    assert len(_pages_titled(vault, "Queued save 1")) == 1


def test_a_resend_while_queued_returns_the_same_receipt_and_commits_once(
    vault: Path, managers, drainer
) -> None:
    _writer, holder = managers
    remember = _command("remember")
    kwargs = _remember_kwargs(vault, 2)
    with _Hold(holder, vault):
        first = _send(remember, vault, kwargs)
        second = _send(remember, vault, kwargs)
        assert second["queued_write"]["ref"] == first["queued_write"]["ref"]
    _wait_for_state(vault, first["queued_write"]["ref"], "applied")
    # A resend after it landed answers with the queued outcome instead of writing again.
    third = _send(remember, vault, kwargs)
    assert third["queued_write"] == {"ref": first["queued_write"]["ref"], "state": "applied"}
    assert len(_pages_titled(vault, "Queued save 2")) == 1


def test_a_replay_after_a_stop_that_followed_the_commit_writes_nothing_new(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _writer, holder = managers
    real_claim, real_finish = write_intake._claim_head, write_intake._finish_applied
    paused = threading.Event()
    paused.set()
    # Hold the drainer, so the test can stop a replay between its commit and its record.
    monkeypatch.setattr(write_intake, "_claim_head", lambda root: 0.05 if paused.is_set() else real_claim(root))
    with _Hold(holder, vault):
        ref = _send(_command("remember"), vault, _remember_kwargs(vault, 3))["queued_write"]["ref"]

    def stop_after_commit(*_args, **_kwargs):
        raise SystemExit("service stopped after the commit, before the intake row was marked")

    monkeypatch.setattr(write_intake, "_finish_applied", stop_after_commit)
    with pytest.raises(SystemExit):
        write_intake._apply(vault, real_claim(vault))
    assert write_intake.read(vault, ref=ref)["queued_write"]["state"] == "applying"
    assert len(_pages_titled(vault, "Queued save 3")) == 1

    monkeypatch.setattr(write_intake, "_finish_applied", real_finish)
    paused.clear()
    applied = _wait_for_state(vault, ref, "applied")
    assert applied["attempts"] == 2
    assert len(_pages_titled(vault, "Queued save 3")) == 1


def test_a_queued_edit_whose_page_changed_fails_visibly_with_its_arguments(
    vault: Path, managers, drainer
) -> None:
    _writer, holder = managers
    page = "Knowledge Base/Notes/Insights/percentage-based-feature-flag-rollout.md"
    kwargs = {
        "path": page,
        "why": "A source corrected the cohort steps.",
        "operation": {"kind": "replace_string", "old_string": "1%, 5%, 25%, 100%", "new_string": "1%, 10%, 50%, 100%"},
    }
    with _Hold(holder, vault):
        ref = _send(_command("edit_memory"), vault, kwargs)["queued_write"]["ref"]
        # Another writer changes the page while the edit is queued: its anchor text is gone.
        target = vault / page
        target.write_text(target.read_text(encoding="utf-8").replace("1%, 5%, 25%, 100%", "2%, 20%, 100%"),
                          encoding="utf-8")
    failed = _wait_for_state(vault, ref, "failed")
    assert failed["error"]["code"]
    assert failed["arguments"]["operation"] == kwargs["operation"]
    assert "2%, 20%, 100%" in (vault / page).read_text(encoding="utf-8")
    assert [item["ref"] for item in write_intake.read(vault)["items"]] == [ref]
    with pytest.raises(OpError):
        write_intake.dismiss(vault, "0" * 32)
    write_intake.dismiss(vault, ref)
    assert write_intake.read(vault)["items"] == []


def test_a_save_sent_after_a_queued_one_runs_after_it(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _writer, holder = managers
    remember = _command("remember")
    paused = threading.Event()
    real_claim = write_intake._claim_head

    def claim_when_unpaused(root):
        if not paused.is_set():
            return 0.05
        return real_claim(root)

    monkeypatch.setattr(write_intake, "_claim_head", claim_when_unpaused)
    with _Hold(holder, vault):
        first = _send(remember, vault, _remember_kwargs(vault, 4))
    # The boundary is free now, but the first save is still queued: the second must not overtake it.
    second = _send(remember, vault, _remember_kwargs(vault, 5), scope="principal:session-b")
    assert second["state"] == "queued"
    assert second["queued_write"]["position"] == 2
    paused.set()
    one = _wait_for_state(vault, first["queued_write"]["ref"], "applied")
    two = _wait_for_state(vault, second["queued_write"]["ref"], "applied")
    assert one["finished_at"] <= two["finished_at"]


def test_a_write_the_intake_does_not_accept_keeps_the_busy_refusal(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _writer, holder = managers
    monkeypatch.setenv("EXOMEM_CAPTURE_WAIT_SECONDS", "1")
    not_a_save = replace(_command("remember"), busy_intake=False)
    delegated = replace(local_owner_principal(surface="mcp"), authorization_session_id="delegated-session")
    with _Hold(holder, vault):
        with pytest.raises(OpError) as refused:
            _send(not_a_save, vault, _remember_kwargs(vault, 6))
        assert refused.value.code == "MUTATION_BUSY"
        with pytest.raises(OpError) as refused_delegate:
            _send(_command("remember"), vault, _remember_kwargs(vault, 7), principal=delegated)
        assert refused_delegate.value.code != "MUTATION_QUEUED"
    assert write_intake.read(vault)["items"] == []


def test_coordination_status_reports_the_intake_and_unknown_when_unreadable(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _writer, holder = managers
    with _Hold(holder, vault):
        _send(_command("remember"), vault, _remember_kwargs(vault, 8))
        assert write_intake.status(vault)["queued"] + write_intake.status(vault)["applying"] == 1

    def unreadable(*_args, **_kwargs):
        raise OSError("disk read failed")

    monkeypatch.setattr(write_intake, "_store", unreadable)
    assert write_intake.status(vault) == {"state": "unknown"}


def test_only_the_owner_can_read_queued_saves(vault: Path, managers, drainer) -> None:
    _writer, holder = managers
    with _Hold(holder, vault):
        ref = _send(_command("remember"), vault, _remember_kwargs(vault, 9))["queued_write"]["ref"]
    other = RequestPrincipal(audience_id="oauth:someone-else", surface="mcp", resolved=True,
                             issuer_family="mcp-oauth:test")
    with request_scope(other):
        for arguments in ({"mode": "queued-write"}, {"mode": "queued-write", "ref": ref}):
            with pytest.raises(OpError) as refused:
                commands.op_review_memory(vault, **arguments)
            assert refused.value.code == "QUEUED_WRITE_OWNER_ONLY"
    with request_scope(local_owner_principal(surface="mcp")):
        assert commands.op_review_memory(vault, mode="queued-write", ref=ref)["queued_write"]["ref"] == ref
