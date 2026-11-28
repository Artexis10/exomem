"""A save that meets a busy mutation boundary is queued durably and lands once.

Before the intake, an eligible save that waited out its bounded wait returned
`MUTATION_BUSY` and wrote nothing; it survived only if the agent sent it again.
These tests drive `invoke_command` against a boundary that another manager
holds, with the serving process's drainer running.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import threading
import time
import tracemalloc
from dataclasses import replace
from pathlib import Path

import pytest
from _public_call import public_call
from conftest import _drain_background_threads
from fastmcp import Client
from record_fixtures import (
    LEDGER_COLLECTION_PATH,
    LEDGER_MANIFEST_TEXT,
    ledger_item,
    setup_ledger_collection,
)
from test_governance_egress import _receipt_records, write_scope

from exomem import (
    auth_sessions,
    call_ledger,
    client_artifacts,
    cloud_cell,
    commands,
    mutation_journal,
    request_budget,
    write_intake,
    writer_lease,
)
from exomem import schema as schema_module
from exomem import server as server_module
from exomem import vault as vault_module
from exomem.cli_ops import OpError
from exomem.commands import op_remember
from exomem.edit_operations import ReplaceStringOperation
from exomem.governance import egress
from exomem.governance import principal as principal_module
from exomem.governance.principal import (
    OriginSessionBinding,
    RequestPrincipal,
    local_owner_principal,
    request_scope,
)

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


def _send(command, vault: Path, kwargs: dict, *, scope: str | None = "principal:session-a",
          principal: RequestPrincipal | None = None, injected: tuple = ()):
    with request_scope(principal or local_owner_principal(surface="mcp")):
        return writer_lease.invoke_command(
            command, vault, *injected, implicit_idempotency_scope=scope, **kwargs
        )


def _wait_for_state(vault: Path, ref: str, state: str) -> dict:
    deadline = time.monotonic() + _LAND_SECONDS
    view: dict = {}
    while time.monotonic() < deadline:
        try:
            view = write_intake.read(vault, ref=ref)["queued_write"]
        except OpError as error:
            # A read that meets the store's busy identity domain is a poll to repeat, not a failure.
            if error.code != "MUTATION_BUSY":
                raise
        else:
            if view["state"] == state:
                return view
        time.sleep(0.1)
    raise AssertionError(f"{ref} never reached {state}: last read {view}")


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


def test_a_stop_during_a_replay_that_waits_on_a_busy_boundary_ends_promptly_and_loses_nothing(
    vault: Path, managers, drainer
) -> None:
    """Defect: a replay waited up to 40 s for the boundary, so a stop spent the supervisor's whole 10 s allowance."""
    _writer, holder = managers
    with _Hold(holder, vault):
        ref = _send(_command("remember"), vault, _remember_kwargs(vault, 30))["queued_write"]["ref"]
        _wait_for_state(vault, ref, "applying")
        started = time.monotonic()
        write_intake.stop(vault)
        elapsed = time.monotonic() - started
        assert not drainer.alive
        assert elapsed < 3.0, elapsed
        assert write_intake.read(vault, ref=ref)["queued_write"]["state"] == "queued"
    write_intake.start(vault)
    _wait_for_state(vault, ref, "applied")
    assert len(_pages_titled(vault, "Queued save 30")) == 1


def test_a_stop_during_a_replay_that_waits_out_a_warm_up_ends_promptly_and_loses_nothing(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect: a replay waited up to 40 s for the warm-up to end, so a stop during start-up overran its allowance."""
    from exomem import readiness

    warming = threading.Event()
    warming.set()
    real_defer = readiness.should_defer
    monkeypatch.setattr(
        readiness, "should_defer",
        lambda component: (component == "graph_handoff" and warming.is_set()) or real_defer(component),
    )
    ref = _send(_command("remember"), vault, _remember_kwargs(vault, 31))["queued_write"]["ref"]
    _wait_for_state(vault, ref, "applying")
    started = time.monotonic()
    write_intake.stop(vault)
    elapsed = time.monotonic() - started
    assert not drainer.alive
    assert elapsed < 3.0, elapsed
    assert write_intake.read(vault, ref=ref)["queued_write"]["state"] == "queued"
    warming.clear()
    write_intake.start(vault)
    _wait_for_state(vault, ref, "applied")
    assert len(_pages_titled(vault, "Queued save 31")) == 1


def test_a_stop_ends_a_store_route_replay_that_waits_for_the_store_thread(
    vault: Path, managers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect: in a store-mode vault a replay waited out a busy store thread, past the stop allowance."""
    from types import SimpleNamespace

    from exomem.collection_store import runtime as store_runtime

    writer, _holder = managers
    monkeypatch.setenv("EXOMEM_CAPTURE_WAIT_SECONDS", "10")
    # Its thread does nothing, so it never takes the request: a store thread busy for the whole wait.
    monkeypatch.setattr(store_runtime.StoreServer, "_run", lambda _server: None)
    server = store_runtime.StoreServer(vault, writer)
    stop = threading.Event()
    token = write_intake._REPLAY.set(write_intake._Replay(vault, SimpleNamespace(), stop=stop))
    threading.Timer(0.3, stop.set).start()
    started = time.monotonic()
    try:
        with pytest.raises(OpError) as refused:
            writer._attempt_store_write(
                lambda *_args, **_kwargs: pytest.fail("the store ran a write it never took"),
                _command("remember"), (vault,), {"mutation_request_id": "replay-store-wait"}, server,
            )
    finally:
        write_intake._REPLAY.reset(token)
    assert refused.value.code in write_intake.ACCEPTED_REFUSALS
    assert time.monotonic() - started < 3.0


def test_a_refusal_that_a_stop_caused_keeps_the_save_queued(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect: a stop that cut episode reference re-planning short failed the queued save for good."""
    _writer, holder = managers
    real_claim, real_invoke = write_intake._claim_head, writer_lease.invoke_command
    paused = threading.Event()
    paused.set()
    monkeypatch.setattr(write_intake, "_claim_head", lambda root: 0.05 if paused.is_set() else real_claim(root))
    with _Hold(holder, vault):
        ref = _send(_command("remember"), vault, _remember_kwargs(vault, 32))["queued_write"]["ref"]
    stop = threading.Event()

    def stopped_while_planning(*_args, **_kwargs):
        stop.set()
        # What episode re-planning raises when its wait ends before the references are proved.
        raise OpError("REFERENCE_PREPARATION_UNAVAILABLE", "Reference ownership could not be validated; nothing was written.",
                      details={"status": "retryable", "committed": False})

    monkeypatch.setattr(writer_lease, "invoke_command", stopped_while_planning)
    write_intake._apply(vault, real_claim(vault), stop=stop)
    assert write_intake.read(vault, ref=ref)["queued_write"]["state"] == "queued"

    monkeypatch.setattr(writer_lease, "invoke_command", real_invoke)
    paused.clear()
    _wait_for_state(vault, ref, "applied")
    assert len(_pages_titled(vault, "Queued save 32")) == 1


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


def test_the_next_save_after_a_queued_save_failed_says_so_once(vault: Path, managers, drainer) -> None:
    # Without the notice, a queued save that fails later is seen only by a caller who asks. The
    # replay queued behind it, a read and a restart must not use the notice up or repeat it.
    _writer, holder = managers
    page = "Knowledge Base/Notes/Insights/percentage-based-feature-flag-rollout.md"
    kwargs = {
        "path": page,
        "why": "A source corrected the cohort steps.",
        "operation": {"kind": "replace_string", "old_string": "1%, 5%, 25%, 100%", "new_string": "1%, 10%, 50%, 100%"},
    }
    remember = _command("remember")
    assert "queued_writes_failed" not in _send(remember, vault, _remember_kwargs(vault, 40))
    with _Hold(holder, vault):
        ref = _send(_command("edit_memory"), vault, kwargs)["queued_write"]["ref"]
        behind = _send(remember, vault, _remember_kwargs(vault, 43), scope="principal:session-c")
        target = vault / page
        target.write_text(target.read_text(encoding="utf-8").replace("1%, 5%, 25%, 100%", "2%, 20%, 100%"),
                          encoding="utf-8")
    _wait_for_state(vault, ref, "failed")
    _wait_for_state(vault, behind["queued_write"]["ref"], "applied")
    read = _send(_command("record_memory"), vault, {"action": "describe"})
    assert "queued_writes_failed" not in read

    noticed = _send(remember, vault, _remember_kwargs(vault, 41), scope="principal:session-b")
    assert noticed["user_summary"].startswith("Saved")
    assert "1 queued save sent earlier failed and was not saved" in noticed["user_summary"]
    assert "queued-write" in noticed["user_summary"]
    assert noticed["queued_writes_failed"] == 1
    later = _send(remember, vault, _remember_kwargs(vault, 42), scope="principal:session-b")
    assert "queued_writes_failed" not in later and "failed" not in later["user_summary"]

    write_intake.stop(vault)
    write_intake.start(vault)
    restarted = _send(remember, vault, _remember_kwargs(vault, 44), scope="principal:session-b")
    assert "queued_writes_failed" not in restarted


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


def test_a_save_behind_queued_saves_is_queued_at_once_without_the_intake_wait(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect: a save behind a backlog waited the whole intake wait for a queue that rarely empties."""
    _writer, holder = managers
    remember = _command("remember")
    paused = threading.Event()
    real_claim = write_intake._claim_head
    monkeypatch.setattr(write_intake, "_claim_head", lambda root: 0.05 if not paused.is_set() else real_claim(root))
    with _Hold(holder, vault):
        first = _send(remember, vault, _remember_kwargs(vault, 6))
    monkeypatch.setenv("EXOMEM_BUSY_INTAKE_AFTER_SECONDS", "30")
    started = time.monotonic()
    second = _send(remember, vault, _remember_kwargs(vault, 7), scope="principal:session-b")
    assert second["state"] == "queued"
    assert time.monotonic() - started < 5.0
    paused.set()
    _wait_for_state(vault, first["queued_write"]["ref"], "applied")
    _wait_for_state(vault, second["queued_write"]["ref"], "applied")


def test_a_compact_save_with_governed_metadata_behind_the_queue_is_refused_at_once_and_never_queued(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect: a save sent behind a backlog was accepted as queued and failed only at the replay."""
    _writer, holder = managers
    observe = _command("observe_memory")
    page = "Knowledge Base/Notes/Insights/percentage-based-feature-flag-rollout.md"
    plain = {
        "path": page, "operation": "add", "category": "operating_constraint",
        "content": "A busy vault must not lose a save",
    }
    paused = threading.Event()
    real_claim = write_intake._claim_head
    monkeypatch.setattr(write_intake, "_claim_head", lambda root: 0.05 if not paused.is_set() else real_claim(root))
    with _Hold(holder, vault):
        first = _send(observe, vault, plain)
    # The boundary is free, but the first save is still queued, so the next one would queue behind it.
    with pytest.raises(ValueError, match="COMPACT_METADATA_REQUIRES_RICH_KIND") as refused:
        _send(observe, vault, {**plain, "content": "Checked constraint", "verdict": "confirmed"},
              scope="principal:session-b")
    assert [item["ref"] for item in write_intake.read(vault)["items"]] == [first["queued_write"]["ref"]]
    paused.set()
    _wait_for_state(vault, first["queued_write"]["ref"], "applied")
    assert "verdict" in str(refused.value) and "`kind`" in str(refused.value)


def test_a_dated_save_behind_the_queue_is_queued_and_applied_as_a_record(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect: the intake refused a dated save before the page's registry could choose the default kind."""
    _writer, holder = managers
    observe = _command("observe_memory")
    page = "Knowledge Base/Notes/Insights/percentage-based-feature-flag-rollout.md"
    plain = {
        "path": page, "operation": "add", "category": "operating_constraint",
        "content": "A busy vault must not lose a save",
    }
    paused = threading.Event()
    real_claim = write_intake._claim_head
    monkeypatch.setattr(write_intake, "_claim_head", lambda root: 0.05 if not paused.is_set() else real_claim(root))
    with _Hold(holder, vault):
        first = _send(observe, vault, plain)
    dated = _send(observe, vault, {**plain, "content": "Dated constraint", "as_of": "2026-11-25"},
                  scope="principal:session-b")
    assert dated["state"] == "queued"
    paused.set()
    _wait_for_state(vault, first["queued_write"]["ref"], "applied")
    applied = _wait_for_state(vault, dated["queued_write"]["ref"], "applied")
    text = (vault / page).read_text(encoding="utf-8")
    assert "- as_of: 2026-11-25" in text and "Dated constraint" in text
    assert "Saved with its date." in json.dumps(applied), applied


def test_an_oversized_recap_behind_the_queue_is_refused_at_once_and_never_queued(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect: a recap whose item broke a size cap came back queued and failed only at the replay."""
    _writer, holder = managers
    observe = _command("observe_memory")
    plain = {
        "path": "Knowledge Base/Notes/Insights/percentage-based-feature-flag-rollout.md",
        "operation": "add", "category": "operating_constraint", "content": "A busy vault must not lose a save",
    }
    paused = threading.Event()
    real_claim = write_intake._claim_head
    monkeypatch.setattr(write_intake, "_claim_head", lambda root: 0.05 if not paused.is_set() else real_claim(root))
    with _Hold(holder, vault):
        first = _send(observe, vault, plain)
    with pytest.raises(ValueError, match="EPISODE_TOO_LARGE"):
        _send(_command("episode_memory"), vault, {
            "action": "record", "subject": "Rollout review", "summary": "Reviewed the rollout steps.",
            "decided": ["Keep the cohort steps " + "x" * 300],
        }, scope="principal:session-b")
    assert [item["ref"] for item in write_intake.read(vault)["items"]] == [first["queued_write"]["ref"]]
    paused.set()
    _wait_for_state(vault, first["queued_write"]["ref"], "applied")


def test_a_record_call_with_arguments_its_action_refuses_behind_the_queue_is_never_queued(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command that owns its argument rules is refused by them before queueing, not at the replay."""
    _writer, holder = managers
    observe = _command("observe_memory")
    plain = {
        "path": "Knowledge Base/Notes/Insights/percentage-based-feature-flag-rollout.md",
        "operation": "add", "category": "operating_constraint", "content": "A busy vault must not lose a save",
    }
    paused = threading.Event()
    real_claim = write_intake._claim_head
    monkeypatch.setattr(write_intake, "_claim_head", lambda root: 0.05 if not paused.is_set() else real_claim(root))
    with _Hold(holder, vault):
        first = _send(observe, vault, plain)
    with pytest.raises(Exception, match="arguments do not match the selected record action"):
        _send(_command("record_memory"), vault, {"action": "append", "collection": "Reading list"},
              scope="principal:session-b")
    assert [item["ref"] for item in write_intake.read(vault)["items"]] == [first["queued_write"]["ref"]]
    paused.set()
    _wait_for_state(vault, first["queued_write"]["ref"], "applied")


def test_a_record_call_its_arguments_refuse_is_still_refused_when_the_egress_filter_faults(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect: a fault in the egress filter dropped the argument refusal, so the save was queued for the replay to
    refuse. That refusal quotes only the caller's own arguments, and was raised unfiltered before the filter ran."""
    _writer, holder = managers
    first, paused = _queue_one_save_and_hold_the_replay(vault, holder, monkeypatch)

    def fault(*args, **kwargs):
        raise RuntimeError("the egress filter faulted")

    monkeypatch.setattr(egress, "postfilter_error", fault)
    with pytest.raises(Exception, match="arguments do not match the selected record action"):
        _send(_command("record_memory"), vault, {"action": "append", "collection": "Reading list"},
              scope="principal:session-b")
    assert [item["ref"] for item in write_intake.read(vault)["items"]] == [first]
    paused.set()
    _wait_for_state(vault, first, "applied")


def _ledger_declaring_a_status(vault: Path, values: str = "draft, published") -> str:
    """The ledger collection with one more field, whose declared values are `values`: `draft` and `published` unless given."""
    manifest = setup_ledger_collection(vault)
    text = manifest.read_text(encoding="utf-8")
    declared = f"    status:\n      type: enum\n      enum: [{values}]\n    exact_text:\n"
    manifest.write_text(text.replace("    exact_text:\n", declared, 1), encoding="utf-8")
    return LEDGER_COLLECTION_PATH


def _status_call(collection: str, action: str, status: str, **target: str) -> dict:
    """An append of a new entry, or an update of the entry that `target` names by its key and guards or its id."""
    if action == "append":
        return {"action": "append", "collection": collection, "item": ledger_item(status=status),
                "why": "record a published entry"}
    return {"action": "update", "collection": collection, **target, "changes": {"status": status},
            "why": "correct an entry"}


def _record_a_draft_entry(vault: Path, collection: str) -> tuple[dict, str]:
    """Save one draft entry directly; return its key and both guards as a query reports them, and its row id."""
    record = _command("record_memory")
    _send(record, vault, {"action": "append", "collection": collection, "why": "record a draft entry",
                          "item": ledger_item(slug="earlier-note", status="draft")})
    view = _send(record, vault, {"action": "query", "collection": collection})
    (row,) = view["rows"]
    guards = {"item_key": row["record_id"], "expected_container_hash": view["snapshot"],
              "expected_item_version": row["item_version"]}
    return guards, row["id"]


def _queue_one_save_and_hold_the_replay(vault: Path, holder, monkeypatch: pytest.MonkeyPatch):
    """Queue an ordinary save under a held boundary and keep the drainer from applying it, so a backlog stands."""
    plain = {
        "path": "Knowledge Base/Notes/Insights/percentage-based-feature-flag-rollout.md",
        "operation": "add", "category": "operating_constraint", "content": "A busy vault must not lose a save",
    }
    paused = threading.Event()
    real_claim = write_intake._claim_head
    monkeypatch.setattr(write_intake, "_claim_head", lambda root: 0.05 if not paused.is_set() else real_claim(root))
    with _Hold(holder, vault):
        first = _send(_command("observe_memory"), vault, plain)
    return first["queued_write"]["ref"], paused


@pytest.mark.parametrize("action", ["append", "update"])
def test_a_record_value_its_collection_never_declares_is_refused_behind_the_queue_and_never_queued(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """Defect: a value outside a declared enum came back "Queued, not saved yet" and failed only at the replay."""
    _writer, holder = managers
    collection = _ledger_declaring_a_status(vault)
    target = _record_a_draft_entry(vault, collection)[0] if action == "update" else {}
    call = _status_call(collection, action, "resolved", **target)
    first, paused = _queue_one_save_and_hold_the_replay(vault, holder, monkeypatch)
    with pytest.raises(OpError) as refused:
        _send(_command("record_memory"), vault, call, scope="principal:session-b")
    assert refused.value.code == "SCHEMA_ENUM"
    assert refused.value.message.endswith('allowed: "draft", "published"')
    assert refused.value.details["field"] == "status"
    assert "held" not in refused.value.details
    assert [item["ref"] for item in write_intake.read(vault)["items"]] == [first]
    paused.set()
    _wait_for_state(vault, first, "applied")
    # Sent directly, the same call reaches the commit's own value check: its key and guards are the row's.
    with pytest.raises(Exception) as direct:
        _send(_command("record_memory"), vault, call, scope="principal:session-b")
    assert direct.value.code == "SCHEMA_ENUM"


def test_a_record_value_its_collection_declares_is_still_queued_behind_the_queue_and_lands(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _writer, holder = managers
    collection = _ledger_declaring_a_status(vault)
    first, paused = _queue_one_save_and_hold_the_replay(vault, holder, monkeypatch)
    receipt = _send(_command("record_memory"), vault, _status_call(collection, "append", "published"),
                    scope="principal:session-b")
    assert receipt["state"] == "queued"
    paused.set()
    _wait_for_state(vault, first, "applied")
    _wait_for_state(vault, receipt["queued_write"]["ref"], "applied")


def test_a_record_value_a_queued_manifest_revision_declares_is_queued_behind_it_and_saved(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The queued revision declares the value before the update replays, so refusing it at once would be wrong.
    The update names its row by id: the container guard covers the manifest, so the revision changes it."""
    _writer, holder = managers
    record = _command("record_memory")
    collection = _ledger_declaring_a_status(vault)
    _guards, item_id = _record_a_draft_entry(vault, collection)
    lifecycle = _send(record, vault, {"action": "inspect", "collection": collection})["lifecycle_guards"]
    manifest_text = (vault / collection).read_text(encoding="utf-8").replace(
        "[draft, published]", "[draft, published, resolved]")
    first, paused = _queue_one_save_and_hold_the_replay(vault, holder, monkeypatch)
    revision = _send(record, vault, {
        "action": "revise", "collection": collection, "manifest_text": manifest_text, **lifecycle,
        "why": "declare a resolved status",
    }, scope="principal:session-b")
    assert revision["state"] == "queued"
    update = _send(record, vault, _status_call(collection, "update", "resolved", item_id=item_id),
                   scope="principal:session-c")
    assert update["state"] == "queued"
    paused.set()
    _wait_for_state(vault, update["queued_write"]["ref"], "applied")
    (row,) = _send(record, vault, {"action": "query", "collection": collection})["rows"]
    assert row["status"] == "resolved"


@pytest.mark.parametrize("named_as", ["Publications ledger", "exomem://memory/5E9D1E2F-8F0A-4B6C-9D21-6F0F4A2B7C31"])
def test_an_undeclared_record_value_behind_a_queued_edit_of_the_manifest_is_queued_not_refused(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch, named_as: str
) -> None:
    """Defect: a queued edit that named the manifest by its title, or by its reference in capitals, left the value
    refused. The edit widens what the collection declares before this save replays, so only the replay can tell."""
    _writer, holder = managers
    collection = _ledger_declaring_a_status(vault)
    widened = vault_module.parse_frontmatter((vault / collection).read_text(encoding="utf-8"))[0]["item_schema"]
    widened["fields"]["status"]["enum"].append("resolved")
    guards, _item_id = _record_a_draft_entry(vault, collection)
    first, paused = _queue_one_save_and_hold_the_replay(vault, holder, monkeypatch)
    with _Hold(holder, vault):
        edit = _send(_command("edit_memory"), vault, {
            "path": named_as,
            "why": "declare a resolved status",
            "operation": {"kind": "patch_frontmatter", "field": "item_schema", "value": widened},
        }, scope="principal:session-b")
    assert edit["state"] == "queued"
    receipt = _send(_command("record_memory"), vault, _status_call(collection, "update", "resolved", **guards),
                    scope="principal:session-c")
    assert receipt["state"] == "queued"
    paused.set()
    _wait_for_state(vault, edit["queued_write"]["ref"], "applied")
    declared = vault_module.parse_frontmatter((vault / collection).read_text(encoding="utf-8"))[0]
    assert declared["item_schema"]["fields"]["status"]["enum"] == ["draft", "published", "resolved"]


def test_a_busy_record_check_by_a_title_two_collections_carry_records_no_release(vault: Path) -> None:
    """Defect: the check's lookup of a title that two collections carry wrote a disclosure receipt releasing
    both manifests, for an ambiguity refusal that reached no caller."""
    setup_ledger_collection(vault)
    archive = vault / "Knowledge Base/Records/Archive/_collection.md"
    archive.parent.mkdir(parents=True)
    archive.write_text(LEDGER_MANIFEST_TEXT.replace(
        "5e9d1e2f-8f0a-4b6c-9d21-6f0f4a2b7c31", "0b6c7d2e-4f1a-4d3b-8e55-1a2b3c4d5e6f"), encoding="utf-8")
    write_scope(vault)  # a policy that is not empty, so a release is decided and recorded
    receipts_before = len(_receipt_records(vault))

    with request_scope(local_owner_principal(surface="mcp")):
        refusal = commands.declared_refusal_before_queueing(
            _command("record_memory"), _status_call("Publications ledger", "append", "resolved"), vault)
    assert refusal is None
    assert _receipt_records(vault)[receipts_before:] == []


def test_the_busy_record_check_holds_none_of_the_queued_arguments_in_memory(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defect: the check loaded and decoded every open Records save on the request thread, up to 4 MiB each,
    and the queue has no cap on how many it holds."""
    _writer, holder = managers
    collection = _ledger_declaring_a_status(vault)
    monkeypatch.setattr(write_intake, "_claim_head", lambda root: 0.05)
    long_text = "x" * (3 << 20)
    with _Hold(holder, vault):
        for number in range(4):
            receipt = _send(_command("record_memory"), vault, {
                "action": "append", "collection": collection, "why": "record a long entry",
                "item": ledger_item(slug=f"long-note-{number}", exact_text=long_text),
            }, scope=f"principal:session-{number}")
            assert receipt["state"] == "queued"
    tracemalloc.start()
    try:
        refusal = commands.declared_refusal_before_queueing(
            _command("record_memory"), _status_call(collection, "append", "resolved"), vault)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert refusal is not None and refusal.code == "SCHEMA_ENUM"
    assert peak < len(long_text), f"the check held {peak} bytes while 4 saves of {len(long_text)} bytes waited"


def _mcp_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The MCP server an agent calls, with no embeddings and the call ledger under `tmp_path`."""
    monkeypatch.setattr(server_module, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.delenv("EXOMEM_MCP_TOOL_SHAPE", raising=False)
    call_ledger.reset_chain_cache()
    return server_module.build_server(require_auth=False)


def _reply_text(result) -> str:
    """Everything an MCP reply shows its caller: its text blocks and its structured content."""
    texts = [getattr(block, "text", "") for block in result.content]
    return "\n".join([*texts, json.dumps(result.structured_content, sort_keys=True)])


_CREDENTIAL = "ghp_" + "A1b2" * 9


def test_a_busy_record_refusal_blocks_a_credential_in_the_declared_values_as_the_save_itself_does(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Defect: the refusal raised before queueing quoted the manifest's enum unfiltered, a credential included,
    while the same refusal from the save itself left through the egress filter, which blocks the credential."""
    _writer, holder = managers
    collection = _ledger_declaring_a_status(vault, f"draft, {_CREDENTIAL}")
    server = _mcp_server(monkeypatch, tmp_path)

    async def add(client: Client, why: str):
        return await client.call_tool(*public_call("add_record", {
            "collection": collection, "why": why, "item": ledger_item(status="resolved"),
        }), raise_on_error=False)

    # One session: the server's shutdown stops the vault's drainer.
    async def run():
        async with Client(server) as client:
            direct = await add(client, "record a published entry")
            first, paused = _queue_one_save_and_hold_the_replay(vault, holder, monkeypatch)
            busy = await add(client, "record a published entry again")
            assert [item["ref"] for item in write_intake.read(vault)["items"]] == [first]
            paused.set()
            _wait_for_state(vault, first, "applied")
        return direct, busy

    direct, busy = asyncio.run(run())
    assert "nothing is held" in busy.structured_content["error"]["message"]
    for reply in (direct, busy):
        assert _CREDENTIAL not in _reply_text(reply)
        error = reply.structured_content["error"]
        assert error["code"] == "SCHEMA_ENUM"
        assert 'allowed: "draft", "[credential blocked by Evermatter egress policy]"' in error["message"]


def test_an_undeclared_record_value_by_title_over_mcp_is_refused_behind_an_ordinary_queued_add_record(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Defect: MCP stores every parameter of a queued `add_record`, an unset one as null, and the check read a
    present `manifest_text` key as a queued revision. One ordinary queued save turned the refusal off, and a wrong
    value came back "Queued, not saved yet"."""
    _writer, holder = managers
    collection = _ledger_declaring_a_status(vault)
    server = _mcp_server(monkeypatch, tmp_path)
    paused = threading.Event()
    real_claim = write_intake._claim_head
    monkeypatch.setattr(write_intake, "_claim_head", lambda root: 0.05 if not paused.is_set() else real_claim(root))

    async def run():
        async with Client(server) as client:
            with _Hold(holder, vault):
                ordinary = await client.call_tool(*public_call("add_record", {
                    "collection": collection, "why": "record a draft", "item": ledger_item(status="draft"),
                }), raise_on_error=False)
            wrong = await client.call_tool(*public_call("add_record", {
                "collection": "Publications ledger", "why": "record a resolved entry",
                "item": ledger_item(slug="second-note", status="resolved"),
            }), raise_on_error=False)
            queued = [item["ref"] for item in write_intake.read(vault)["items"]]
            paused.set()
            _wait_for_state(vault, ordinary.structured_content["queued_write"]["ref"], "applied")
        return ordinary.structured_content, wrong.structured_content, queued

    ordinary, wrong, queued = asyncio.run(run())
    assert queued == [ordinary["queued_write"]["ref"]]
    assert wrong["error"]["code"] == "SCHEMA_ENUM"
    assert wrong["error"]["message"].startswith('Field is outside its enum: status; allowed: "draft", "published"')


def test_a_revisit_save_with_relations_behind_a_backlog_is_told_only_the_exit_that_lands(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dropping the date alone would meet the page's relations refusal, so only a rich kind is offered."""
    _writer, holder = managers
    observe = _command("observe_memory")
    page = "Knowledge Base/Notes/Insights/percentage-based-feature-flag-rollout.md"
    plain = {
        "path": page, "operation": "add", "category": "operating_constraint",
        "content": "A busy vault must not lose a save",
    }
    paused = threading.Event()
    real_claim = write_intake._claim_head
    monkeypatch.setattr(write_intake, "_claim_head", lambda root: 0.05 if not paused.is_set() else real_claim(root))
    with _Hold(holder, vault):
        first = _send(observe, vault, plain)
    with pytest.raises(ValueError, match="COMPACT_METADATA_REQUIRES_RICH_KIND") as refused:
        _send(observe, vault, {
            **plain, "content": "Constraint to revisit", "check_by": "2026-11-25",
            "relations": [{"kind": "supersedes", "target": "applied-export"}],
        }, scope="principal:session-b")
    paused.set()
    _wait_for_state(vault, first["queued_write"]["ref"], "applied")

    remediation = str(refused.value).partition(" Remediation: ")[2]
    assert "omit check_by" not in remediation
    assert "passing a rich kind" in remediation


def _send_after_deadline(command, vault: Path, kwargs: dict, **send) -> object:
    """Send a save whose call has used its whole budget by the time it would start saving."""
    budget = request_budget.RequestBudget(seconds=50)
    budget.deadline = time.monotonic() - 1.0
    token = request_budget.set_current(budget)
    try:
        return _send(command, vault, kwargs, **send)
    finally:
        request_budget.reset_current(token)


def test_a_save_that_used_its_call_time_preparing_is_queued_and_lands_once(
    vault: Path, managers, drainer
) -> None:
    """A starved host once spent 13 minutes preparing a save whose client gave up at 60 s. The save
    must come back queued while the client still listens, then land once, never be lost."""
    receipt = _send_after_deadline(_command("remember"), vault, _remember_kwargs(vault, 31))

    assert receipt["state"] == "queued"
    assert receipt["committed"] is False
    intake_id = receipt["queued_write"]["ref"].removeprefix(write_intake.REF_PREFIX)
    rows = [json.loads(line) for line in mutation_journal.journal_path().read_text(encoding="utf-8").splitlines()]
    # The journal row is how an operator counts saves queued because their call's time ran out.
    (row,) = [r for r in rows if r.get("outcome") == "queued" and r.get("receipt_id") == intake_id[:16]]
    assert row["error_code"] == "request_budget"
    assert _pages_titled(vault, "Queued save 31") == []
    _wait_for_state(vault, receipt["queued_write"]["ref"], "applied")
    assert len(_pages_titled(vault, "Queued save 31")) == 1


def test_a_write_the_intake_cannot_queue_still_lands_when_its_call_time_is_spent(
    vault: Path, managers, drainer
) -> None:
    """A refusal could not queue this write, so refusing it would leave a slow write unable to land."""
    not_a_save = replace(_command("remember"), busy_intake=False)
    _send_after_deadline(not_a_save, vault, _remember_kwargs(vault, 32))

    assert len(_pages_titled(vault, "Queued save 32")) == 1


_FIGURES_CAPTURE = {
    "title": "Fetched figures",
    "source_type": "research-report",
    "files": [{"download_url": "https://files.example/a", "file_id": "file-a"}],
}


def _fetch_figures(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    """Stand in for the download of a caller's file link; returns the file ids fetched."""
    fetched: list[str] = []

    def stage(file, _budget, **_kwargs):  # noqa: ANN001, ANN202
        fetched.append(file["file_id"])
        staged = tmp_path / "staged-figures.bin"
        staged.write_bytes(b"quarterly figures")
        return client_artifacts.StagedArtifact(
            file_id=file["file_id"], path=staged, size=17,
            sha256=hashlib.sha256(b"quarterly figures").hexdigest(),
            content_type="application/octet-stream", filename="figures.bin",
        )

    monkeypatch.setattr(client_artifacts, "stage_artifact", stage)
    return fetched


def test_a_file_capture_past_its_call_time_commits_with_the_file_it_fetched(
    vault: Path, managers, drainer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server cannot fetch a caller's temporary file link again, so queuing the capture would
    fetch it a second time or lose it once the link expired."""
    fetched = _fetch_figures(monkeypatch, tmp_path)
    result = _send_after_deadline(
        _command("capture_source"), vault, _FIGURES_CAPTURE, injected=(schema_module.load_source_schema(vault),)
    )

    assert result.get("state") != "queued", result
    assert fetched == ["file-a"]
    assert [path.read_bytes() for path in vault.rglob("*.bin")] == [b"quarterly figures"]


def test_a_file_capture_that_meets_a_busy_vault_goes_back_to_its_caller(
    vault: Path, managers, drainer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queued replay would fetch the caller's temporary file link again, or find it expired; only
    the caller can send the capture again."""
    _writer, holder = managers
    monkeypatch.setenv("EXOMEM_CAPTURE_WAIT_SECONDS", "1")
    fetched = _fetch_figures(monkeypatch, tmp_path)
    with _Hold(holder, vault), pytest.raises(OpError) as refused:
        _send(_command("capture_source"), vault, _FIGURES_CAPTURE,
              injected=(schema_module.load_source_schema(vault),))

    assert refused.value.code == "MUTATION_BUSY"
    assert refused.value.details["committed"] is False
    assert fetched == ["file-a"]
    assert write_intake.read(vault)["items"] == []


def test_a_file_capture_waits_out_a_short_busy_boundary_as_if_the_intake_were_off(
    vault: Path, managers, drainer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never queued, the capture keeps the full capture wait, not the intake's short one."""
    _writer, holder = managers
    monkeypatch.setenv("EXOMEM_CAPTURE_WAIT_SECONDS", "8")
    fetched = _fetch_figures(monkeypatch, tmp_path)
    with _Hold(holder, vault) as hold:
        threading.Timer(1.5, hold.release.set).start()
        result = _send(_command("capture_source"), vault, _FIGURES_CAPTURE,
                       injected=(schema_module.load_source_schema(vault),))

    assert result.get("state") != "queued", result
    assert fetched == ["file-a"]
    assert [path.read_bytes() for path in vault.rglob("*.bin")] == [b"quarterly figures"]


def test_a_commit_under_way_is_never_refused_when_its_call_time_runs_out(
    vault: Path, managers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a call's first write guard is refused: a later guard belongs to a commit already started."""
    writer, _holder = managers
    monkeypatch.setattr(write_intake, "queues_busy_refusal", lambda: True)
    budget = request_budget.RequestBudget(seconds=50)
    token = request_budget.set_current(budget)
    try:
        with writer.mutation_guard(vault, operation="first step"):
            budget.deadline = time.monotonic() - 1.0
        with writer.mutation_guard(vault, operation="second step"):
            pass
    finally:
        request_budget.reset_current(token)


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
        assert refused_delegate.value.code == "MUTATION_BUSY"
    assert write_intake.read(vault)["items"] == []


def test_a_protocol_step_keeps_the_busy_refusal_while_a_save_of_the_same_command_queues(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A step whose reply the caller needs for its next step (an episode prepare), or a capture under a
    # short reservation, would run against moved state if it were applied later.
    _writer, holder = managers
    monkeypatch.setenv("EXOMEM_CAPTURE_WAIT_SECONDS", "1")
    episode = _command("episode_memory")
    schema = (schema_module.load_source_schema(vault),)
    with _Hold(holder, vault):
        recap = _send(episode, vault, {"action": "record", "subject": "Queued recap",
                                       "summary": "A recap sent while the vault was busy.",
                                       "decided": ["Keep the recap"]}, injected=schema)
        assert recap["state"] == "queued"
        with pytest.raises(OpError) as prepare:
            _send(episode, vault, {"action": "prepare", "episode": "ep-0123456789abcdef0123456789abcdef",
                                   "candidate": "c1", "proposal": {"route": "no_capture", "alternatives": [], "evidence": [],
                                                                  "reason": "Covered by an existing note.", "leaves": []}},
                  injected=schema)
        assert prepare.value.code == "MUTATION_BUSY"
        with pytest.raises(OpError) as reserved:
            identity = {"system": "notes-app", "record_id": "r-1", "revision": "1"}
            _send(_command("capture_source"), vault, {
                "title": "Reserved capture", "content": "Text held under a reservation.",
                "source_identity": identity,
                "capture_reservation": {
                    "protocol_version": 1, "ticket": "t" * 32, "source_identity": identity,
                    "request_sha256": "0" * 64, "admission_expires_at": time.time() + 600,
                    "scope": {"cell_id": None, "replica_id": "r", "ledger_id": "l", "boundary_id": "b"},
                },
            }, injected=schema)
        assert reserved.value.code == "MUTATION_BUSY"
    assert all(item["command"] == "episode_memory" for item in write_intake.read(vault)["items"])


def test_coordination_status_reports_the_intake_and_unknown_when_unreadable(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _writer, holder = managers
    with _Hold(holder, vault):
        _send(_command("remember"), vault, _remember_kwargs(vault, 8))
        status = write_intake.status(vault)
        assert status["queued"] + status["applying"] == 1

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
    # A delegated session of the owner is refused too: its saves never queue, so it reads none.
    delegated = replace(local_owner_principal(surface="mcp"), authorization_session_id="delegated-session")
    for caller in (other, delegated):
        with request_scope(caller):
            for arguments in ({"mode": "queued-write"}, {"mode": "queued-write", "ref": ref}):
                with pytest.raises(OpError) as refused:
                    commands.op_review_memory(vault, **arguments)
                assert refused.value.code == "QUEUED_WRITE_OWNER_ONLY"
            assert "write_intake" not in commands.op_coordination_status(vault)
    with request_scope(local_owner_principal(surface="mcp")):
        assert commands.op_review_memory(vault, mode="queued-write", ref=ref)["queued_write"]["ref"] == ref


def test_a_replay_that_stopped_before_its_commit_gate_lands_once_under_a_fresh_identity(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _writer, holder = managers
    real_claim = write_intake._claim_head
    paused = threading.Event()
    paused.set()
    monkeypatch.setattr(write_intake, "_claim_head", lambda root: 0.05 if paused.is_set() else real_claim(root))
    with _Hold(holder, vault):
        ref = _send(_command("remember"), vault, _remember_kwargs(vault, 10))["queued_write"]["ref"]

        def stopped_while_waiting(*_args, **_kwargs):
            raise SystemExit("service stopped while the replay waited for the boundary")

        # The replay dies in line, before it reaches the commit gate.
        monkeypatch.setattr(writer_lease, "invoke_command", stopped_while_waiting)
        with pytest.raises(SystemExit):
            write_intake._apply(vault, real_claim(vault))
    monkeypatch.undo()
    monkeypatch.setattr(writer_lease, "get_manager", lambda: _writer)
    applied = _wait_for_state(vault, ref, "applied")
    assert applied["attempts"] == 2
    assert len(_pages_titled(vault, "Queued save 10")) == 1


def _hold_and_queue(holder, vault: Path, *sends, monkeypatch=None, then=None) -> list[dict]:
    """Queue each save while another manager holds the boundary; return the receipts.

    With `then`, the drainer claims nothing until the saves are queued and `then` has run,
    so a fault that `then` installs meets each save's first replay.
    """
    resume = threading.Event()
    if then is not None:
        real_claim = write_intake._claim_head
        monkeypatch.setattr(write_intake, "_claim_head", lambda root: real_claim(root) if resume.is_set() else 0.05)
    with _Hold(holder, vault):
        receipts = [send() for send in sends]
    if then is not None:
        then()
        resume.set()
    assert all(receipt["state"] == "queued" for receipt in receipts), receipts
    return receipts


def test_a_typed_mcp_edit_during_a_hold_is_queued_and_applied_after_it(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _writer, holder = managers
    monkeypatch.setattr(server_module, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.delenv("EXOMEM_MCP_TOOL_SHAPE", raising=False)
    call_ledger.reset_chain_cache()
    page = "Knowledge Base/Notes/Insights/percentage-based-feature-flag-rollout.md"
    edit = {"note": page, "action": "replace_text", "old_text": "1%, 5%, 25%, 100%",
            "new_text": "1%, 10%, 50%, 100%", "why": "A source corrected the cohort steps."}

    # The hold covers only the call: the in-process client's teardown waits behind a held
    # boundary, so the hold ends as soon as the receipt is back.
    server = server_module.build_server(require_auth=False)
    hold = _Hold(holder, vault)

    async def send() -> dict:
        async with Client(server) as client:
            result = await client.call_tool(*public_call("edit_note", edit), raise_on_error=False)
            hold.release.set()
            assert result.structured_content is not None
            receipt = result.structured_content
            assert receipt["state"] == "queued", receipt
            # Wait inside the session: the server's shutdown stops the vault's drainer, and a replay
            # that met the hold retries only after its back-off.
            _wait_for_state(vault, receipt["queued_write"]["ref"], "applied")
            return receipt

    with hold:
        asyncio.run(send())
    assert "1%, 10%, 50%, 100%" in (vault / page).read_text(encoding="utf-8")


def test_a_typed_edit_operation_is_stored_as_json_and_replayed(vault: Path, managers, drainer) -> None:
    _writer, holder = managers
    page = "Knowledge Base/Notes/Insights/percentage-based-feature-flag-rollout.md"
    operation = ReplaceStringOperation(kind="replace_string", old_string="1%, 5%, 25%, 100%",
                                       new_string="1%, 10%, 50%, 100%")
    (receipt,) = _hold_and_queue(holder, vault, lambda: _send(
        _command("edit_memory"), vault, {"path": page, "why": "A source corrected it.", "operation": operation}))
    _wait_for_state(vault, receipt["queued_write"]["ref"], "applied")
    assert "1%, 10%, 50%, 100%" in (vault / page).read_text(encoding="utf-8")


def test_the_same_explicit_identity_with_a_different_payload_lands_two_pages(
    vault: Path, managers, drainer
) -> None:
    _writer, holder = managers
    remember = _command("remember")
    first, second = _remember_kwargs(vault, 11), _remember_kwargs(vault, 12)
    receipts = _hold_and_queue(
        holder, vault,
        lambda: _send(remember, vault, {**first, "idempotency_key": "one-key"}, scope=None),
        lambda: _send(remember, vault, {**second, "idempotency_key": "one-key"}, scope=None),
    )
    assert receipts[0]["queued_write"]["ref"] != receipts[1]["queued_write"]["ref"]
    for receipt in receipts:
        _wait_for_state(vault, receipt["queued_write"]["ref"], "applied")
    assert len(_pages_titled(vault, "Queued save 11")) == 1
    assert len(_pages_titled(vault, "Queued save 12")) == 1


def test_a_commit_marker_that_cannot_be_written_commits_nothing_and_the_save_lands_once(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _writer, holder = managers
    real_mark = write_intake._mark_commit_reached
    pages_when_refused: list[int] = []

    def fail_once(replay):
        if not pages_when_refused:
            pages_when_refused.append(len(_pages_titled(vault, "Queued save 13")))
            raise OSError("disk full while marking the commit")
        return real_mark(replay)

    monkeypatch.setattr(write_intake, "_mark_commit_reached", fail_once)
    (receipt,) = _hold_and_queue(holder, vault, lambda: _send(_command("remember"), vault, _remember_kwargs(vault, 13)))
    _wait_for_state(vault, receipt["queued_write"]["ref"], "applied")
    assert pages_when_refused == [0]
    assert len(_pages_titled(vault, "Queued save 13")) == 1


def test_a_replay_whose_outcome_is_unknown_fails_visibly_and_the_queue_moves_on(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _writer, holder = managers
    remember = _command("remember")
    real_invoke = writer_lease.invoke_command

    def unknown_for_the_first(command, *injected, **kwargs):
        if kwargs.get("slug") == "queued-save-14":
            # What the idempotency store answers for an identity a stopped owner left pending.
            raise OpError("MUTATION_OUTCOME_UNKNOWN", "the earlier attempt's outcome is unknown",
                          details={"status": "unknown", "committed": None})
        return real_invoke(command, *injected, **kwargs)

    receipts = _hold_and_queue(
        holder, vault,
        lambda: _send(remember, vault, _remember_kwargs(vault, 14)),
        lambda: _send(remember, vault, _remember_kwargs(vault, 15), scope="principal:session-b"),
        monkeypatch=monkeypatch,
        then=lambda: monkeypatch.setattr(writer_lease, "invoke_command", unknown_for_the_first),
    )
    failed = _wait_for_state(vault, receipts[0]["queued_write"]["ref"], "failed")
    assert failed["error"]["code"] == "MUTATION_OUTCOME_UNKNOWN"
    assert failed["arguments"]["slug"] == "queued-save-14"
    read = write_intake.read(vault, ref=receipts[0]["queued_write"]["ref"])
    assert "may have landed" in read["user_summary"]
    _wait_for_state(vault, receipts[1]["queued_write"]["ref"], "applied")
    assert len(_pages_titled(vault, "Queued save 15")) == 1


def test_a_replay_that_faults_before_the_dispatcher_fails_and_the_save_behind_it_lands(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _writer, holder = managers
    schema = schema_module.load_source_schema(vault)
    episode = {"action": "record", "episode": "ep-" + "b2" * 16, "subject": "Harbor Lamp purchase",
               "summary": "Chose the brass lamp; delivery date still open.", "decided": ["Buy the brass lamp"]}
    real_load = schema_module.load_source_schema
    faults: list[Path] = []

    def schema_fault_once(root):
        # The first load after the saves are queued is the episode replay's own.
        if not faults:
            faults.append(root)
            raise RuntimeError("schema file vanished")
        return real_load(root)

    receipts = _hold_and_queue(
        holder, vault,
        lambda: _send(_command("episode_memory"), vault, episode, injected=(schema,)),
        lambda: _send(_command("remember"), vault, _remember_kwargs(vault, 16), scope="principal:session-b"),
        monkeypatch=monkeypatch,
        then=lambda: monkeypatch.setattr(schema_module, "load_source_schema", schema_fault_once),
    )
    failed = _wait_for_state(vault, receipts[0]["queued_write"]["ref"], "failed")
    assert failed["error"]["code"] == "INTERNAL"
    assert failed["arguments"]["episode"] == episode["episode"]
    _wait_for_state(vault, receipts[1]["queued_write"]["ref"], "applied")
    assert len(_pages_titled(vault, "Queued save 16")) == 1


def test_a_save_that_meets_no_contention_commits_when_the_intake_store_is_unreadable(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable(*_args, **_kwargs):
        raise OSError("disk read failed")

    monkeypatch.setattr(write_intake, "_store", unreadable)
    result = _send(_command("remember"), vault, _remember_kwargs(vault, 17))
    assert result.get("state") != "queued"
    assert result["user_summary"].startswith("Saved")
    assert len(_pages_titled(vault, "Queued save 17")) == 1


def test_an_idle_drainer_leaves_the_vault_identity_generation_alone(vault: Path, managers, drainer) -> None:
    writer, holder = managers
    (receipt,) = _hold_and_queue(holder, vault, lambda: _send(_command("remember"), vault, _remember_kwargs(vault, 18)))
    _wait_for_state(vault, receipt["queued_write"]["ref"], "applied")
    # The save's own background work (a graph rebuild) changes identities; let it finish first.
    _drain_background_threads()
    before = writer_lease._read_reserved_identity_generation(writer.config.state_dir, vault)
    # A restarted drainer reads its existing store and then waits; nothing it does may change an identity.
    write_intake.stop(vault)
    restarted = write_intake.start(vault)
    restarted.notify()
    time.sleep(0.5)
    assert restarted.backlog() == 0
    assert writer_lease._read_reserved_identity_generation(writer.config.state_dir, vault) == before


def test_a_queued_save_whose_login_has_ended_fails_with_its_arguments(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _writer, holder = managers
    signed_in = replace(
        local_owner_principal(surface="mcp"),
        origin_session=OriginSessionBinding(session_id="login-1", generation="1", audience="mcp"),
    )
    (receipt,) = _hold_and_queue(
        holder, vault,
        lambda: _send(_command("remember"), vault, _remember_kwargs(vault, 19), principal=signed_in),
        monkeypatch=monkeypatch,
        then=lambda: monkeypatch.setattr(
            auth_sessions, "origin_login_state_sync", lambda _who: auth_sessions.OriginLogin.NOT_VALID
        ),
    )
    failed = _wait_for_state(vault, receipt["queued_write"]["ref"], "failed")
    assert failed["error"]["code"] == write_intake.LOGIN_REVOKED
    assert failed["arguments"]["slug"] == "queued-save-19"
    assert _pages_titled(vault, "Queued save 19") == []


def test_a_cloud_owners_queued_save_is_applied_without_a_live_request(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A Cloud cell's gateway proves the login per request; the drainer has no request, so only
    # the cell binding is rechecked before the replay.
    _writer, holder = managers
    monkeypatch.setattr(principal_module, "cloud_owner_binding_matches", lambda _cell: True)
    cloud_owner = replace(
        local_owner_principal(surface="mcp"),
        issuer_family=cloud_cell.issuer_family("cell-123"),
        origin_session=OriginSessionBinding(session_id="login-2", generation="1", audience="mcp"),
    )
    (receipt,) = _hold_and_queue(
        holder, vault, lambda: _send(_command("remember"), vault, _remember_kwargs(vault, 24), principal=cloud_owner)
    )
    _wait_for_state(vault, receipt["queued_write"]["ref"], "applied")
    assert len(_pages_titled(vault, "Queued save 24")) == 1


def test_a_resend_of_a_known_save_is_answered_from_the_index_when_its_row_cannot_be_read(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _writer, holder = managers
    remember = _command("remember")
    kwargs = _remember_kwargs(vault, 25)
    (receipt,) = _hold_and_queue(holder, vault, lambda: _send(remember, vault, kwargs))
    ref = receipt["queued_write"]["ref"]
    _wait_for_state(vault, ref, "applied")

    def unreadable(_self, _intake_id):
        raise OSError("intake store unreadable")

    monkeypatch.setattr(write_intake.Admission, "_stored_answer", unreadable)
    resend = _send(remember, vault, kwargs)
    assert resend["queued_write"] == {"ref": ref, "state": "applied"}
    assert len(_pages_titled(vault, "Queued save 25")) == 1


def test_a_save_without_an_idempotency_identity_keeps_the_busy_refusal(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An unkeyed resend cannot be told from a new save, so queueing it would land it once per send.
    monkeypatch.setenv("EXOMEM_CAPTURE_WAIT_SECONDS", "1")
    _writer, holder = managers
    with _Hold(holder, vault), pytest.raises(OpError) as refused:
        _send(_command("remember"), vault, _remember_kwargs(vault, 26), scope=None)
    assert refused.value.code == "MUTATION_BUSY"
    assert write_intake.read(vault)["items"] == []


def test_a_resend_sent_while_the_original_waits_for_the_boundary_lands_once(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The resend arrives while the original still waits for the boundary; the boundary frees
    # just after the original is queued. Run beside it, the resend would commit the save itself
    # and the queued copy would land it again.
    monkeypatch.setenv("EXOMEM_BUSY_INTAKE_AFTER_SECONDS", "2")
    _writer, holder = managers
    remember = _command("remember")
    kwargs = _remember_kwargs(vault, 28)
    replies: dict[str, dict] = {}
    hold = _Hold(holder, vault)
    with hold:
        original = threading.Thread(target=lambda: replies.update(original=_send(remember, vault, kwargs)))
        original.start()
        time.sleep(1.0)
        resend = threading.Thread(target=lambda: replies.update(resend=_send(remember, vault, kwargs)))
        resend.start()
        original.join(_HOLD_SECONDS)
        hold.release.set()
        resend.join(_HOLD_SECONDS)
    ref = replies["original"]["queued_write"]["ref"]
    assert replies["resend"]["queued_write"]["ref"] == ref, replies["resend"]
    _wait_for_state(vault, ref, "applied")
    assert len(_pages_titled(vault, "Queued save 28")) == 1


def test_a_stale_finish_never_overwrites_an_applied_save(vault: Path, managers, drainer) -> None:
    _writer, holder = managers
    (receipt,) = _hold_and_queue(holder, vault, lambda: _send(_command("remember"), vault, _remember_kwargs(vault, 27)))
    ref = receipt["queued_write"]["ref"]
    _wait_for_state(vault, ref, "applied")
    stale = write_intake._Claim(
        id=ref.removeprefix(write_intake.REF_PREFIX), command="remember", arguments={}, principal={},
        surface=None, attempts=1, generation=0, origin=None, origin_ttl=None,
    )
    write_intake._finish_failed(vault, stale, "STALE_ANCHOR", {"code": "STALE_ANCHOR"})
    assert write_intake.read(vault, ref=ref)["queued_write"]["state"] == "applied"


def test_a_save_between_its_insert_and_its_count_holds_later_saves_behind_it(drainer) -> None:
    # The row is durable before the in-memory count rises; a later save that saw an empty queue in
    # that gap would commit ahead of a save already queued.
    assert not drainer.has_backlog()
    drainer.begin_insert()
    assert drainer.has_backlog()
    drainer.end_insert()
    assert not drainer.has_backlog()


def test_an_acceptance_that_meets_a_busy_store_is_retried_and_queues_the_save(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A busy intake store during acceptance must not refuse the save: the acceptance retries.
    real_insert = write_intake.Admission._insert
    calls: list[int] = []

    def busy_once(self, reason, *, create):
        calls.append(1)
        if len(calls) == 1:
            raise OpError("MUTATION_BUSY", "intake store busy", "retry", details={"committed": False})
        return real_insert(self, reason, create=create)

    monkeypatch.setattr(write_intake.Admission, "_insert", busy_once)
    _writer, holder = managers
    (receipt,) = _hold_and_queue(holder, vault, lambda: _send(_command("remember"), vault, _remember_kwargs(vault, 29)))
    assert len(calls) >= 2
    _wait_for_state(vault, receipt["queued_write"]["ref"], "applied")
    assert len(_pages_titled(vault, "Queued save 29")) == 1


def test_a_resend_waits_for_its_original_only_within_its_own_request_budget(drainer) -> None:
    # A resend whose client has stopped waiting must not hold a request thread for the full wait.
    from exomem import request_budget

    assert drainer.enter("origin-2") is None  # the original is in flight
    token = request_budget.set_current(request_budget.RequestBudget(seconds=request_budget.DELIVERY_RESERVE_SECONDS + 0.5))
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            drainer.enter("origin-2")
    finally:
        request_budget.reset_current(token)
        drainer.end_accept("origin-2")
    assert time.monotonic() - started < 5


def test_the_commit_marker_is_written_from_the_store_thread(
    vault: Path, managers, drainer
) -> None:
    # Store-routed saves reach the commit gate on the collection store's thread, not the drainer's.
    _writer, holder = managers
    (receipt,) = _hold_and_queue(holder, vault, lambda: _send(_command("remember"), vault, _remember_kwargs(vault, 30)))
    intake_id = receipt["queued_write"]["ref"].removeprefix(write_intake.REF_PREFIX)
    _wait_for_state(vault, receipt["queued_write"]["ref"], "applied")
    write_intake.stop(vault)  # nothing may reclaim the hand-edited row below
    with write_intake._write(vault) as conn:  # stand the row back up as a claimed replay
        conn.execute("UPDATE intake SET state = 'applying', commit_reached = 0 WHERE id = ?", (intake_id,))
    claim = write_intake._Claim(
        id=intake_id, command="remember", arguments={}, principal={}, surface=None,
        attempts=1, generation=0, origin=None, origin_ttl=None,
    )
    with write_intake._store(vault, mode="write") as conn:
        replay = write_intake._Replay(vault, claim, conn=conn)
        token = write_intake._REPLAY.set(replay)
        try:
            errors: list[BaseException] = []

            def gate() -> None:
                try:
                    write_intake.mark_replay_reached_commit()
                except BaseException as error:  # noqa: BLE001 - the test inspects it
                    errors.append(error)

            other = threading.Thread(target=contextvars.copy_context().run, args=(gate,))
            other.start()
            other.join(10)
            assert errors == []
            with write_intake._store(vault, mode="read") as reader:
                marked = reader.execute("SELECT commit_reached FROM intake WHERE id = ?", (intake_id,)).fetchone()[0]
            assert marked == 1
            # Work that outlives its replay may still commit; its requeue already marked the identity.
            replay.commit_reached = False
            replay.closed = True
            write_intake.mark_replay_reached_commit()
        finally:
            write_intake._REPLAY.reset(token)


def test_a_requeue_whose_work_may_still_commit_keeps_its_identity_marked(vault: Path, managers, drainer) -> None:
    # Store work can outlive its replay and commit later; a reclaim must then reuse the identity,
    # which answers with the stored outcome, instead of replaying the save under a fresh one.
    _writer, holder = managers
    (receipt,) = _hold_and_queue(holder, vault, lambda: _send(_command("remember"), vault, _remember_kwargs(vault, 31)))
    intake_id = receipt["queued_write"]["ref"].removeprefix(write_intake.REF_PREFIX)
    _wait_for_state(vault, receipt["queued_write"]["ref"], "applied")
    write_intake.stop(vault)  # nothing may reclaim the hand-edited row below
    with write_intake._write(vault) as conn:
        conn.execute("UPDATE intake SET state = 'applying', commit_reached = 0 WHERE id = ?", (intake_id,))
    claim = write_intake._Claim(
        id=intake_id, command="remember", arguments={}, principal={}, surface=None,
        attempts=1, generation=0, origin=None, origin_ttl=None,
    )
    write_intake._requeue(vault, claim, "MUTATION_ACKNOWLEDGEMENT_PENDING")
    with write_intake._store(vault, mode="read") as reader:
        state, marked = reader.execute("SELECT state, commit_reached FROM intake WHERE id = ?", (intake_id,)).fetchone()
    assert (state, marked) == ("queued", 1)


def test_an_uncertain_replay_marks_its_identity_before_it_ends(
    vault: Path, managers, drainer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The store may still commit the replay's work after the replay ends; the mark must already be
    # durable then, not only once the requeue lands.
    _writer, holder = managers
    real_invoke, real_requeue = writer_lease.invoke_command, write_intake._requeue
    marked_at_requeue: list[int] = []

    def acknowledgement_pending_once(*args, **kwargs):
        if not marked_at_requeue:
            raise OpError("MUTATION_ACKNOWLEDGEMENT_PENDING", "still running", "retry",
                          details={"committed": None})
        return real_invoke(*args, **kwargs)

    def observing_requeue(root, claim, code):
        with write_intake._store(root, mode="read") as reader:
            marked_at_requeue.append(
                reader.execute("SELECT commit_reached FROM intake WHERE id = ?", (claim.id,)).fetchone()[0]
            )
        return real_requeue(root, claim, code)

    def install() -> None:
        monkeypatch.setattr(writer_lease, "invoke_command", acknowledgement_pending_once)
        monkeypatch.setattr(write_intake, "_requeue", observing_requeue)

    (receipt,) = _hold_and_queue(
        holder, vault, lambda: _send(_command("remember"), vault, _remember_kwargs(vault, 32)),
        monkeypatch=monkeypatch, then=install,
    )
    _wait_for_state(vault, receipt["queued_write"]["ref"], "applied")
    assert marked_at_requeue[0] == 1
    assert len(_pages_titled(vault, "Queued save 32")) == 1
