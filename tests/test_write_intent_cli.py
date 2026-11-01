"""The CLI's side of accepted writes (design D1, D8): the unbounded wait after the row, Ctrl-C, `exomem writes`.

Drives `exomem.__main__.main` in-process against a vault copy, with the boundary held by another manager.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from pathlib import Path

import pytest
from test_write_intent_accept import _journal, _manager, held_boundary_in_thread
from test_write_intent_applier import _no_applier
from test_write_intent_decisions import _accept_edit, _edit_kwargs, _new_page

from exomem import writer_lease
from exomem.__main__ import main


@pytest.fixture(autouse=True)
def _journal_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_WRITE_INTENTS", "on")
    writer_lease.reset_managers_for_tests()
    yield
    writer_lease.reset_managers_for_tests()


def _run(argv: list[str], capsys) -> tuple[int, str, str]:
    try:
        code = main(argv)
    except SystemExit as error:
        code = error.code if isinstance(error.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _remember_argv(number: int) -> list[str]:
    content = (
        f"# Cli save {number}\n\n## Observations\n\n"
        "- [operating constraint] Keep capture off the client #reliability\n\n"
        "## Relations\n\n"
        "- mitigates [[Knowledge Base/Notes/Failures/cache-stampede-on-cold-start]]\n"
    )
    return [
        "remember",
        "--content",
        content,
        "--title",
        f"Cli save {number}",
        "--slug",
        f"cli-save-{number}",
    ]


def test_a_cli_write_waits_past_the_accept_deadline_and_prints_the_committed_terminal(
    vault: Path, capsys
) -> None:
    """The accept deadline protects a client that loses its reply; the CLI loses none, so stopping at 2 s would hand a script `accepted`."""
    manager = _manager()
    started = time.monotonic()
    with held_boundary_in_thread(vault, manager) as held:
        threading.Timer(5.0, held.release.set).start()
        code, out, _ = _run([*_remember_argv(90), "--json"], capsys)
    payload = json.loads(out.strip().splitlines()[-1])
    assert code == 0 and payload["data"]["status"] == "committed"
    assert time.monotonic() - started > 4.5, "it answered before the boundary freed"
    assert len(list(vault.rglob("cli-save-90*.md"))) == 1


def test_ctrl_c_during_the_wait_leaves_the_write_queued_and_prints_its_reference(
    vault: Path, capsys
) -> None:
    """An interrupted CLI must not lose the write or leave the user without a way to find it."""
    manager = _manager()
    with held_boundary_in_thread(vault, manager) as held:
        threading.Timer(3.5, lambda: os.kill(os.getpid(), signal.SIGINT)).start()
        code, _, err = _run(_remember_argv(91), capsys)
        queued = _journal(manager, vault).non_terminal()
        held.release.set()
    assert code == 130
    assert len(queued) == 1 and queued[0].state in ("accepted", "applying")
    assert f"stays queued as exomem://write/{queued[0].intent_id}" in err
    assert f"exomem writes show {queued[0].intent_id}" in err
    assert not list(vault.rglob("cli-save-91*.md"))


def test_writes_list_and_show_report_an_accepted_write_and_discard_refuses_it(
    vault: Path, capsys, monkeypatch
) -> None:
    """A user who lost the reference must find the write again; deciding a write that still waits would race its applier."""
    manager = _manager()
    path = _new_page(manager, vault, 92)
    with _no_applier(monkeypatch):
        intent = _accept_edit(manager, vault, _edit_kwargs(path, "waiting"), "cli-list")
    code, out, _ = _run(["writes", "list"], capsys)
    assert code == 0 and f"exomem://write/{intent.intent_id}" in out and "accepted" in out
    code, out, _ = _run(["writes", "show", intent.intent_id, "--json"], capsys)
    shown = json.loads(out.strip().splitlines()[-1])["data"]
    assert code == 0 and shown["state"] == "accepted" and shown["command"] == "edit_memory"
    code, _, err = _run(["writes", "discard", intent.intent_id], capsys)
    assert code == 1 and "WRITE_DECISION_REFUSED" in err, (code, err)
    assert _journal(manager, vault).get(intent.intent_id).state == "accepted"
