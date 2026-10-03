"""`episode_memory`: record a bounded recap, bind it, and inspect the ledger.

One leaf behind MCP, REST and the CLI. `record` writes one canonical recap per
content change through the ordinary Source writer and binds it to the caller's
own episode ledger by the writer's receipt; `inspect` reads that ledger back.
A record accepts no curation leaves, proposals or dispositions: those belong to
the typed candidate actions, pinned in `test_episode_workflow.py`.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from starlette.testclient import TestClient

from exomem import (
    commands,
    curation,
    episode_capture,
    episode_model,
    episode_recovery,
    memory_refs,
    server,
    working_set,
)
from exomem import schema as schema_module
from exomem.__main__ import main as cli_main
from exomem.episode_model import EpisodeError
from exomem.governance import egress
from exomem.governance.principal import (
    RequestPrincipal,
    owner_principal,
    request_scope,
)

KEY = "ep-" + "a1" * 16
RESULT_KEYS = {
    "operation",
    "episode",
    "revision",
    "source",
    "idempotent",
    "recovery",
    "ledger",
    "about_skipped",
}


def _audience(name: str) -> RequestPrincipal:
    return RequestPrincipal(audience_id=name, surface="mcp")


def _record(vault: Path, **overrides: object) -> dict:
    args: dict[str, object] = {
        "action": "record",
        "episode": KEY,
        "subject": "Harbor Lamp purchase",
        "summary": "Chose the brass lamp; delivery date still open.",
        "worked_on": ["Compared two lamps for Project Alpha"],
        "decided": ["Buy the brass Harbor Lamp"],
        "open": ["Confirm the delivery date"],
    }
    args.update(overrides)
    schema = schema_module.load_source_schema(vault)
    return commands.op_episode_memory(vault, schema, **args)


def _episodes(vault: Path) -> list[Path]:
    folder = vault / "Knowledge Base" / "Sources" / "Episodes"
    return sorted(folder.glob("*.md")) if folder.exists() else []


def _frontmatter(path: Path) -> dict:
    head = path.read_text(encoding="utf-8").removeprefix("---\n").partition("\n---\n")[0]
    return yaml.safe_load(head)


def _write_note(vault: Path, name: str, memory_id: str) -> str:
    path = vault / "Knowledge Base" / "Notes" / "Insights" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "type: insight\n"
        f"exomem_id: {memory_id}\n"
        f"title: {name}\n"
        "status: active\n"
        "created: 2026-09-20\n"
        "updated: 2026-09-20\n"
        "sources: []\n"
        "tags: []\n"
        "---\n\n"
        f"# {name}\n\nA note.\n",
        encoding="utf-8",
    )
    return memory_refs.memory_ref(memory_id)


def _withhold_notes_from(vault: Path, audience: str) -> None:
    root = vault / "Knowledge Base" / "_Governance"
    (root / "scopes").mkdir(parents=True, exist_ok=True)
    (root / "rules").mkdir(parents=True, exist_ok=True)
    (root / "scopes" / "withheld-notes.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FAC\n"
        "name: Withheld notes\n"
        'paths: ["Notes/Insights/withheld-*.md"]\n',
        encoding="utf-8",
    )
    (root / "rules" / "withheld-notes.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FAD\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAC"]\n'
        f"audience: {audience}\n"
        "ceiling: 0\n",
        encoding="utf-8",
    )
    egress.clear_decision_memo()
    from exomem.governance import membership, policy

    membership.clear_memo()
    policy._CACHE.clear()


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def test_the_tool_is_registered_on_all_three_surfaces() -> None:
    product = {command.name: command for command in commands.PRODUCT_COMMANDS}
    command = product["episode_memory"]

    assert command.surfaces >= {"mcp", "rest", "cli"}
    assert command.tier == 1
    assert command.cli_writes is True
    assert command.needs_schema is True
    assert command.leaf is commands.op_episode_memory
    assert commands.invocation_is_read_only(command, {"action": "inspect"}) is True
    assert commands.invocation_is_read_only(command, {"action": "record"}) is False


def test_hosted_surfaces_exclude_the_tool() -> None:
    assert "episode_memory" in commands.HOSTED_SURFACE_EXCLUSIONS
    assert "episode_memory" not in commands.hosted_complete_surface_names()
    for profile in commands.PRODUCT_SURFACE_PROFILES.values():
        assert "episode_memory" not in profile.command_names


# --------------------------------------------------------------------------- #
# record
# --------------------------------------------------------------------------- #


def test_record_writes_one_recap_and_binds_revision_one(vault: Path) -> None:
    with request_scope(owner_principal(surface="mcp")):
        result = _record(vault, client="claude-code")

    assert set(result) == RESULT_KEYS
    assert result["episode"] == KEY
    assert result["revision"] == 1
    assert result["idempotent"] is False
    assert result["ledger"] == "bound"
    assert result["recovery"] == "available"
    assert result["about_skipped"] == 0
    assert result["source"]["title"] == "Harbor Lamp purchase"
    assert result["source"]["path"].startswith("Knowledge Base/Sources/Episodes/")
    [page] = _episodes(vault)
    assert page.relative_to(vault).as_posix() == result["source"]["path"]
    frontmatter = _frontmatter(page)
    assert frontmatter["episode"] == KEY
    assert frontmatter["client"] == "claude-code"
    assert memory_refs.memory_ref(str(frontmatter["exomem_id"])) == result["source"]["ref"]


def test_record_mints_a_key_when_the_client_holds_none(vault: Path) -> None:
    with request_scope(owner_principal(surface="mcp")):
        result = _record(vault, episode=None)

    assert episode_capture.EPISODE_KEY_RE.fullmatch(result["episode"])
    assert result["revision"] == 1


def test_a_retry_yields_one_page_and_one_revision(vault: Path) -> None:
    with request_scope(owner_principal(surface="mcp")):
        first = _record(vault)
        again = _record(vault)

    assert again["idempotent"] is True
    assert again["revision"] == first["revision"] == 1
    assert again["source"] == first["source"]
    assert len(_episodes(vault)) == 1


def test_a_changed_record_yields_a_second_page_and_revision_two(vault: Path) -> None:
    with request_scope(owner_principal(surface="mcp")):
        first = _record(vault)
        second = _record(vault, summary="Chose the brass lamp; delivery booked for Friday.")

    assert second["idempotent"] is False
    assert second["revision"] == 2
    assert second["source"]["path"] != first["source"]["path"]
    assert len(_episodes(vault)) == 2
    old = _frontmatter(vault / first["source"]["path"])
    assert old["status"] == "superseded"
    assert "status" not in _frontmatter(vault / second["source"]["path"])


def test_one_key_used_by_two_audiences_keeps_separate_ledgers(vault: Path) -> None:
    with request_scope(_audience("client-a")):
        first = _record(vault)
    with request_scope(_audience("client-b")):
        second = _record(vault, summary="Audience B's own account of the same thread.")
    with request_scope(_audience("client-a")):
        inspected = commands.op_episode_memory(
            vault, schema_module.load_source_schema(vault), action="inspect", episode=KEY
        )

    assert first["revision"] == 1
    assert second["revision"] == 1
    assert [item["revision"] for item in inspected["revisions"]] == [1]
    assert all((vault / r["source"]["path"]).exists() for r in (first, second))


def _withhold_episodes_from(vault: Path, audience: str) -> None:
    root = vault / "Knowledge Base" / "_Governance"
    (root / "scopes").mkdir(parents=True, exist_ok=True)
    (root / "rules").mkdir(parents=True, exist_ok=True)
    (root / "scopes" / "episodes.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FBA\n"
        "name: Recaps\n"
        'paths: ["Sources/Episodes/*.md"]\n',
        encoding="utf-8",
    )
    (root / "rules" / "episodes.yaml").write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FBB\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FBA"]\n'
        f"audience: {audience}\n"
        "ceiling: 0\n",
        encoding="utf-8",
    )
    egress.clear_decision_memo()
    from exomem.governance import membership, policy

    membership.clear_memo()
    policy._CACHE.clear()


@pytest.mark.parametrize("withheld", [True, False], ids=["withheld", "visible"])
def test_another_audience_never_retires_or_replays_the_owners_recap(
    vault: Path, withheld: bool
) -> None:
    """Revisions are grouped per audience, so another audience continuing the
    same key writes its own history: the owner's recap stays live, byte for
    byte, and stays the owner's newest revision in recent context."""
    with request_scope(owner_principal(surface="mcp")):
        owned = _record(vault)
    owner_page = vault / owned["source"]["path"]
    before = owner_page.read_bytes()
    if withheld:
        _withhold_episodes_from(vault, "client-b")

    with request_scope(_audience("client-b")):
        other = _record(vault, summary="Audience B's own account of the same thread.")
        replay = _record(vault)  # the owner's exact content, replayed

    assert owner_page.read_bytes() == before
    assert "status" not in _frontmatter(owner_page)
    assert other["source"]["path"] != owned["source"]["path"]
    assert other["idempotent"] is False and other["revision"] == 1
    assert replay["idempotent"] is False and replay["revision"] == 2
    assert replay["source"]["path"] not in {owned["source"]["path"], other["source"]["path"]}
    assert replay["source"]["ref"] != owned["source"]["ref"]
    mtimes = {
        page.relative_to(vault).as_posix(): page.stat().st_mtime_ns for page in _episodes(vault)
    }
    assert owned["source"]["path"] in working_set._recent_episodes(mtimes, limit=4)

    with request_scope(owner_principal(surface="mcp")):
        inspected = commands.op_episode_memory(
            vault, schema_module.load_source_schema(vault), action="inspect", episode=KEY
        )
        revised = _record(vault, open=["Confirm the delivery date", "Choose a bulb"])
    assert inspected["revisions"] == [{"revision": 1, "recovery": "available"}]
    assert inspected["latest_source_ref"] == owned["source"]["ref"]
    assert revised["revision"] == 2
    assert _frontmatter(owner_page)["status"] == "superseded"
    # Audience B's own live revision (its replay superseded its first) is
    # untouched by the owner's revision.
    assert _frontmatter(vault / other["source"]["path"])["status"] == "superseded"
    assert "status" not in _frontmatter(vault / replay["source"]["path"])


def test_a_clock_step_back_still_orders_the_new_revision_last(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An NTP step, or a second machine running behind, must not make recent
    context serve the retired revision."""
    import datetime as dt

    from exomem import episode_memory

    class _Clock(dt.datetime):
        at = dt.datetime(2026, 9, 23, 12, 0, tzinfo=dt.UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.at if tz is None else cls.at.astimezone(tz)

    class _Dt:
        datetime = _Clock
        UTC = dt.UTC

    monkeypatch.setattr(episode_memory, "dt", _Dt)
    with request_scope(owner_principal(surface="mcp")):
        first = _record(vault)
        _Clock.at -= dt.timedelta(hours=1)
        second = _record(vault, summary="Chose the brass lamp; delivery booked for Friday.")

    order = {
        result["source"]["path"]: episode_capture.filename_parts(
            result["source"]["path"].rsplit("/", 1)[-1]
        )[1]
        for result in (first, second)
    }
    assert order[second["source"]["path"]] > order[first["source"]["path"]]
    assert _frontmatter(vault / first["source"]["path"])["status"] == "superseded"
    mtimes = {
        page.relative_to(vault).as_posix(): page.stat().st_mtime_ns for page in _episodes(vault)
    }
    assert working_set._recent_episodes(mtimes, limit=4) == (second["source"]["path"],)


def test_a_revision_that_changed_after_it_was_read_loses_and_is_re_read(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another record commits between this record's read and its write. The
    compare-and-swap guards the text that was read, so this batch loses, is
    re-read once, and retires the other record's revision instead of marking
    the already-retired one a second time and leaving two live."""
    from exomem import episode_memory

    with request_scope(owner_principal(surface="mcp")):
        first = _record(vault)
    real = episode_memory._revisions
    interleaved: list[dict] = []
    armed = [True]

    def read_then_interleave(vault_root, key, audience):
        observed = real(vault_root, key, audience)
        if armed[0]:
            armed[0] = False
            interleaved.append(_record(vault, summary="A concurrent account."))
        return observed

    monkeypatch.setattr(episode_memory, "_revisions", read_then_interleave)
    with request_scope(owner_principal(surface="mcp")):
        ours = _record(vault, summary="Chose the brass lamp; delivery booked for Friday.")

    [concurrent] = interleaved
    live = [page for page in _episodes(vault) if "status" not in _frontmatter(page)]
    assert [page.relative_to(vault).as_posix() for page in live] == [ours["source"]["path"]]
    retired_once = _frontmatter(vault / first["source"]["path"])["superseded_by"]
    assert isinstance(retired_once, str) or len(retired_once) == 1
    assert concurrent["source"]["path"].removesuffix(".md") in str(retired_once)
    assert _frontmatter(vault / concurrent["source"]["path"])["status"] == "superseded"
    assert ours["revision"] == 3


def test_episode_memory_holds_the_wide_mutation_boundary() -> None:
    """Its read of the live revisions and its write must not interleave with
    another writer's commit; the narrowed boundary would allow exactly that."""
    from exomem import writer_lease

    assert "episode_memory" not in writer_lease._NARROW_BOUNDARY_COMMANDS


def test_blocked_episode_fanout_does_not_hold_the_next_writer(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import vault as vault_module
    from exomem.writer_lease import LeaseConfig, LeaseManager

    monkeypatch.delenv("EXOMEM_FAST_DURABLE_ACK", raising=False)
    fanout_started = threading.Event()
    release_fanout = threading.Event()
    second_committed = threading.Event()
    second_entered = threading.Event()
    second_errors = []
    outcomes = []
    real_fanout = vault_module.post_commit_batch_fanout

    def held_fanout(*args, **kwargs):
        if args[0] is not None and any("/Episodes/" in str(path) for path in args[1]):
            fanout_started.set()
            assert release_fanout.wait(15)
        return real_fanout(*args, **kwargs)

    monkeypatch.setattr(vault_module, "post_commit_batch_fanout", held_fanout)
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "lease"))
    episode_command = next(c for c in commands.PRODUCT_COMMANDS if c.name == "episode_memory")
    schema = schema_module.load_source_schema(vault)

    def record_episode():
        try:
            with request_scope(owner_principal(surface="mcp")):
                outcomes.append(manager.invoke(
                    episode_command,
                    (vault, schema),
                    {
                        "action": "record", "episode": KEY,
                        "subject": "Harbor Lamp purchase",
                        "summary": "Chose the brass lamp; delivery date still open.",
                        "worked_on": ["Compared two lamps for Project Alpha"],
                    },
                ))
        except Exception as error:  # noqa: BLE001 - assert worker failures in the test thread
            outcomes.append(error)

    def write_next():
        path = vault / "Knowledge Base" / "next-writer.md"

        def leaf(_vault):
            second_entered.set()
            vault_module.batch_atomic_write([vault_module.PlannedWrite(path, "next")])
            second_committed.set()
            return "next"

        command = SimpleNamespace(name="next_writer", read_only=False, leaf=leaf)
        try:
            manager.invoke(command, (vault,), {})
        except Exception as error:  # noqa: BLE001 - assert worker failures in the test thread
            second_errors.append(error)

    first = threading.Thread(target=record_episode, daemon=True)
    second = threading.Thread(target=write_next, daemon=True)
    first.start()
    try:
        assert fanout_started.wait(15)
        second.start()
        assert second_committed.wait(3), (second_entered.is_set(), second_errors)
    finally:
        release_fanout.set()
        first.join(timeout=15)
        if second.ident is not None:
            second.join(timeout=15)
    assert not first.is_alive() and not second.is_alive()
    assert len(outcomes) == 1 and not isinstance(outcomes[0], BaseException)


def test_direct_episode_record_runs_fanout_with_durable_exact_paths(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import deferred_index
    from exomem import vault as vault_module

    calls = []

    def observe(_vault, replaced, _reports, _states, **kwargs):
        calls.append((list(replaced), list(kwargs["created_paths"])))
        return True

    monkeypatch.setattr(vault_module, "post_commit_batch_fanout", observe)
    with request_scope(owner_principal(surface="mcp")):
        result = _record(vault)

    assert len(calls) == 1
    replaced, created = calls[0]
    source = vault / result["source"]["path"]
    assert source in replaced and source in created
    assert vault / "Knowledge Base" / "Sources" / "index.md" in replaced
    assert all(path in replaced for path in created)
    queued = set(deferred_index.full_status(vault)["paths"])
    assert {path.relative_to(vault).as_posix() for path in replaced if path.suffix == ".md"} <= queued


def test_episode_fanout_failure_keeps_a_committed_result_and_durable_demand(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import deferred_index
    from exomem import vault as vault_module

    def fail_fanout(*_args, **_kwargs):
        raise OSError("derived fanout unavailable")

    monkeypatch.setattr(vault_module, "post_commit_batch_fanout", fail_fanout)
    with request_scope(owner_principal(surface="mcp")):
        result = _record(vault)
    assert result["ledger"] == "bound"
    assert (vault / result["source"]["path"]).exists()
    assert result["source"]["path"] in deferred_index.full_status(vault)["paths"]


def test_episode_without_durable_demand_or_fanout_reports_committed_uncertainty(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import deferred_index
    from exomem import vault as vault_module
    from exomem.writer_lease import LeaseConfig, LeaseManager

    monkeypatch.setattr(
        deferred_index, "add_full_receipts", lambda *_args: (_ for _ in ()).throw(OSError("queue down"))
    )
    monkeypatch.setattr(
        vault_module, "post_commit_batch_fanout",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("fanout down")),
    )
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "lease"))
    command = next(c for c in commands.PRODUCT_COMMANDS if c.name == "episode_memory")
    schema = schema_module.load_source_schema(vault)
    with request_scope(owner_principal(surface="mcp")):
        with pytest.raises(Exception) as caught:
            manager.invoke(
                command, (vault, schema),
                {
                    "action": "record", "episode": KEY,
                    "subject": "Harbor Lamp purchase",
                    "summary": "Chose the brass lamp; delivery date still open.",
                    "worked_on": ["Compared two lamps for Project Alpha"],
                },
            )
    assert getattr(caught.value, "committed", None) is True
    assert getattr(caught.value, "code", None) == "MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN"
    assert len(_episodes(vault)) == 1


@pytest.mark.parametrize("same_content", [False, True], ids=["changed", "identical"])
def test_concurrent_initial_episode_records_keep_one_live_revision(
    vault: Path, tmp_path: Path, same_content: bool
) -> None:
    from exomem.writer_lease import LeaseConfig, LeaseManager

    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "lease"))
    command = next(c for c in commands.PRODUCT_COMMANDS if c.name == "episode_memory")
    schema = schema_module.load_source_schema(vault)
    start = threading.Barrier(3)
    outcomes = []

    def record(summary: str):
        start.wait(timeout=15)
        try:
            with request_scope(owner_principal(surface="mcp")):
                outcomes.append(manager.invoke(
                    command, (vault, schema),
                    {
                        "action": "record", "episode": KEY,
                        "subject": "Harbor Lamp purchase", "summary": summary,
                        "worked_on": ["Compared two lamps for Project Alpha"],
                    },
                ))
        except Exception as error:  # noqa: BLE001 - assert worker failures in the test thread
            outcomes.append(error)

    first_summary = "Chose the brass lamp; delivery date still open."
    second_summary = first_summary if same_content else "Delivery booked for Friday."
    threads = [
        threading.Thread(target=record, args=(summary,), daemon=True)
        for summary in (first_summary, second_summary)
    ]
    for thread in threads:
        thread.start()
    start.wait(timeout=15)
    for thread in threads:
        thread.join(timeout=15)
    assert all(not thread.is_alive() for thread in threads)
    assert len(outcomes) == 2 and all(not isinstance(item, BaseException) for item in outcomes)
    live = [page for page in _episodes(vault) if "status" not in _frontmatter(page)]
    assert len(live) == 1
    if same_content:
        assert sorted(item["idempotent"] for item in outcomes) == [False, True]
        assert {item["revision"] for item in outcomes} == {1}
        assert len(_episodes(vault)) == 1
    else:
        assert sorted(item["revision"] for item in outcomes) == [1, 2]
        assert all(item["idempotent"] is False for item in outcomes)
        newest = next(item for item in outcomes if item["revision"] == 2)
        assert live[0] == vault / newest["source"]["path"]


@pytest.mark.parametrize("fast_ack", [False, True], ids=["ordinary", "fast-ack"])
def test_episode_derived_work_has_one_owner_in_both_ack_modes(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fast_ack: bool
) -> None:
    from exomem import deferred_index
    from exomem import vault as vault_module
    from exomem.writer_lease import LeaseConfig, LeaseManager

    if fast_ack:
        monkeypatch.setenv("EXOMEM_FAST_DURABLE_ACK", "1")
    else:
        monkeypatch.delenv("EXOMEM_FAST_DURABLE_ACK", raising=False)
    calls = []

    def observe(_vault, replaced, _reports, _states, **_kwargs):
        calls.append(list(replaced))
        return True

    monkeypatch.setattr(vault_module, "post_commit_batch_fanout", observe)
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "lease"))
    command = next(c for c in commands.PRODUCT_COMMANDS if c.name == "episode_memory")
    schema = schema_module.load_source_schema(vault)
    with request_scope(owner_principal(surface="mcp")):
        result = manager.invoke(
            command, (vault, schema),
            {
                "action": "record", "episode": KEY,
                "subject": "Harbor Lamp purchase",
                "summary": "Chose the brass lamp; delivery date still open.",
                "worked_on": ["Compared two lamps for Project Alpha"],
            },
        )
    assert result["ledger"] == "bound"
    assert (vault / result["source"]["path"]).exists()
    if fast_ack:
        assert calls == []
    else:
        assert len(calls) == 1
        assert result["source"]["path"] in deferred_index.full_status(vault)["paths"]


@pytest.mark.parametrize("managed", [False, True], ids=["direct", "terminal"])
def test_episode_cold_graph_repair_respects_the_callers_acknowledgement_contract(
    vault: Path, monkeypatch: pytest.MonkeyPatch, managed: bool
) -> None:
    """A committed recap must not rebuild the vault on its return path."""
    from exomem import deferred_index, epistemic_graph, graph_sync
    from exomem.writer_lease import get_manager

    monkeypatch.delenv("EXOMEM_FAST_DURABLE_ACK", raising=False)
    rebuilds = []
    blocked_return = []
    release = threading.Event()
    real_rebuild = epistemic_graph.EpistemicGraphIndex._rebuild_all_off_boundary

    def observe_rebuild(self, *args, **kwargs):
        rebuilds.append(threading.get_ident())
        if managed and not release.wait(timeout=10):
            blocked_return.append(True)
            raise TimeoutError("capture waited for its derived rebuild")
        return real_rebuild(self, *args, **kwargs)

    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex, "_rebuild_all_off_boundary", observe_rebuild
    )
    manager = get_manager()
    command = next(c for c in commands.PRODUCT_COMMANDS if c.name == "episode_memory")
    schema = schema_module.load_source_schema(vault)
    try:
        with request_scope(owner_principal(surface="mcp")):
            arguments = {
                "action": "record", "episode": KEY,
                "subject": "Harbor Lamp purchase",
                "summary": "Chose the brass lamp; delivery date still open.",
                "worked_on": ["Compared two lamps for Project Alpha"],
            }
            result = (
                manager.invoke(command, (vault, schema), arguments)
                if managed else commands.op_episode_memory(vault, schema, **arguments)
            )
    finally:
        release.set()
        graph_sync.await_active_rebuild(vault, state_root=manager.config.state_dir, timeout=10)
    assert (vault / result["source"]["path"]).exists()
    assert result["source"]["path"] in deferred_index.full_status(vault)["paths"]
    if managed:
        assert result["state"] == "committed"
        assert not blocked_return
        assert threading.get_ident() not in rebuilds
        assert result.get("derived_sync") != "failed"
        assert result["graph_sync"] in {"pending", "completed"}
        checkpoint = graph_sync.read_checkpoint(vault)
        assert checkpoint is not None
        assert graph_sync.repair_is_provisioned(
            vault, checkpoint,
            outcome="deferred" if result["graph_sync"] == "pending" else "completed",
        )
    else:
        assert rebuilds
        assert epistemic_graph.EpistemicGraphIndex(vault).available()


def test_episode_terminal_fanout_starts_repair_on_the_invoking_manager(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-default manager must start the repair its terminal calls pending."""
    from exomem import epistemic_graph, graph_sync
    from exomem.writer_lease import LeaseConfig, LeaseManager

    monkeypatch.delenv("EXOMEM_FAST_DURABLE_ACK", raising=False)
    monkeypatch.setattr(epistemic_graph.EpistemicGraphIndex, "available", lambda self: False)
    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex, "_graph_sync_predecessor_state",
        lambda self, required: "graph_sync_predecessor_mismatch",
    )
    started, release = threading.Event(), threading.Event()

    def rebuild(_index, required):
        started.set()
        assert release.wait(timeout=10)
        return graph_sync.GraphBuildOutcome.covering(required)

    monkeypatch.setattr(epistemic_graph, "_rebuild_outcome", rebuild)
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "lease"))
    command = next(c for c in commands.PRODUCT_COMMANDS if c.name == "episode_memory")
    schema = schema_module.load_source_schema(vault)
    try:
        with request_scope(owner_principal(surface="mcp")):
            result = manager.invoke(
                command, (vault, schema),
                {
                    "action": "record", "episode": KEY,
                    "subject": "Harbor Lamp purchase",
                    "summary": "Chose the brass lamp; delivery date still open.",
                    "worked_on": ["Compared two lamps for Project Alpha"],
                },
            )
        assert result["state"] == "committed"
        assert result["graph_sync"] == "pending"
        assert started.wait(timeout=2)
    finally:
        release.set()
        graph_sync.await_active_rebuild(vault, state_root=manager.config.state_dir, timeout=10)


def test_direct_heat_failure_does_not_turn_committed_episode_into_retryable_error(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import working_set_heat

    def fail_attribution(*_args, **_kwargs):
        raise OSError("heat state unavailable")

    monkeypatch.setattr(working_set_heat, "attribution_for", fail_attribution)
    with request_scope(owner_principal(surface="mcp")):
        result = _record(vault)
    assert result["ledger"] == "bound"
    assert (vault / result["source"]["path"]).exists()


def test_a_ledger_failure_after_the_write_is_idempotent_on_retry(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = episode_recovery.EpisodeInputOwner.bind_committed_input
    calls = {"n": 0}

    def failing_once(self, key, *, path, reference, about=()):
        calls["n"] += 1
        if calls["n"] == 1:
            raise EpisodeError("EPISODE_STORE_WRITE_FAILED", "episode history write was refused")
        return real(self, key, path=path, reference=reference, about=about)

    monkeypatch.setattr(episode_recovery.EpisodeInputOwner, "bind_committed_input", failing_once)
    with request_scope(owner_principal(surface="mcp")):
        first = _record(vault)
        retried = _record(vault)

    assert first["ledger"] == "unbound"
    assert first["revision"] is None
    assert retried["idempotent"] is True
    assert retried["ledger"] == "bound"
    assert retried["revision"] == 1
    assert len(_episodes(vault)) == 1


@pytest.mark.parametrize(
    "failure",
    [
        lambda: curation.CurationError("CURATION_RUN_CORRUPT", "journal digest mismatch"),
        lambda: OSError("the ledger directory is unwritable"),
    ],
    ids=["corrupt-journal", "os-error"],
)
def test_no_ledger_failure_answers_a_committed_recap_with_an_error(
    vault: Path, monkeypatch: pytest.MonkeyPatch, failure
) -> None:
    """The page is committed before the bind. Whatever the bind raises then,
    the caller gets the receipt with `ledger: unbound`, never an error that
    invites a retry of a write that already happened."""

    def failing(self, key, *, path, reference, **_kwargs):
        raise failure()

    monkeypatch.setattr(episode_recovery.EpisodeInputOwner, "bind_committed_input", failing)
    with request_scope(owner_principal(surface="mcp")):
        result = _record(vault)

    assert result["ledger"] == "unbound"
    assert result["recovery"] == "unavailable"
    assert result["revision"] is None
    assert [page.relative_to(vault).as_posix() for page in _episodes(vault)] == [
        result["source"]["path"]
    ]


def test_a_summary_with_unicode_spaces_is_recorded(vault: Path) -> None:
    """Every line the recap accepts is one the Source writer accepts."""
    with request_scope(owner_principal(surface="mcp")):
        result = _record(vault, summary="Chose" + chr(0xA0) + "the brass  lamp.")

    [page] = _episodes(vault)
    assert _frontmatter(page)["summary"] == "Chose the brass lamp."
    assert result["ledger"] == "bound"


def test_an_unresolved_principal_fails_before_anything_is_written(vault: Path) -> None:
    with pytest.raises(ValueError, match="EPISODE_OWNER_UNRESOLVED"):
        _record(vault)

    assert _episodes(vault) == []


def test_a_refused_recap_writes_nothing(vault: Path) -> None:
    secret = "sk-proj-" + "Ab3dE" * 10
    with request_scope(owner_principal(surface="mcp")):
        with pytest.raises(ValueError, match="EPISODE_CREDENTIAL"):
            _record(vault, said=[f"use {secret}"])
        with pytest.raises(ValueError, match="EPISODE_EMPTY"):
            _record(vault, worked_on=[], decided=[], open=[])

    assert _episodes(vault) == []


def test_unknown_and_withheld_about_refs_are_indistinguishable(vault: Path) -> None:
    visible = _write_note(vault, "visible-lamp-note", "11111111-1111-4111-8111-111111111111")
    withheld = _write_note(vault, "withheld-lamp-note", "22222222-2222-4222-8222-222222222222")
    unknown = memory_refs.memory_ref("33333333-3333-4333-8333-333333333333")
    _withhold_notes_from(vault, "client-a")

    with request_scope(_audience("client-a")):
        kept = _record(vault, episode="ep-" + "b2" * 16, about=[visible])
        hidden = _record(vault, episode="ep-" + "c3" * 16, about=[withheld])
        missing = _record(vault, episode="ep-" + "d4" * 16, about=[unknown])

    assert kept["about_skipped"] == 0
    for result in (kept, hidden, missing):
        assert "about" not in _frontmatter(vault / result["source"]["path"])
    for result in (hidden, missing):
        assert result["about_skipped"] == 1
    with request_scope(_audience("client-a")):
        ledger = episode_recovery.EpisodeInputOwner(vault)._store()
        evidence = ledger.read(episode_model.episode_id("ep-" + "b2" * 16))["state"][
            "input_revisions"
        ][-1]["evidence"]
    assert evidence["about"] == [visible]
    assert {key: hidden[key] for key in ("about_skipped", "ledger", "idempotent")} == {
        key: missing[key] for key in ("about_skipped", "ledger", "idempotent")
    }


# --------------------------------------------------------------------------- #
# inspect
# --------------------------------------------------------------------------- #


def test_inspect_lists_revisions_and_the_latest_source(vault: Path) -> None:
    with request_scope(owner_principal(surface="mcp")):
        _record(vault)
        second = _record(vault, open=["Confirm the delivery date", "Choose a bulb"])
        inspected = commands.op_episode_memory(
            vault, schema_module.load_source_schema(vault), action="inspect", episode=KEY
        )

    assert inspected == {
        "episode": KEY,
        "revisions": [
            {"revision": 1, "recovery": "available"},
            {"revision": 2, "recovery": "available"},
        ],
        "latest_source_ref": second["source"]["ref"],
        "coverage_current": "unchecked",
    }


def test_the_shared_page_never_names_what_the_recap_concerns(vault: Path) -> None:
    """Another audience may read the recap page; it must not learn from it the
    ref of a page withheld from it."""
    withheld = _write_note(vault, "withheld-lamp-note", "22222222-2222-4222-8222-222222222222")
    with request_scope(owner_principal(surface="mcp")):
        recorded = _record(vault, about=[withheld])
    _withhold_notes_from(vault, "client-b")

    with request_scope(_audience("client-b")):
        served = commands.op_read_memory(vault, path=recorded["source"]["ref"])

    assert "22222222-2222" not in json.dumps(served, default=str)
    assert "22222222-2222" not in (vault / recorded["source"]["path"]).read_text("utf-8")


def test_inspect_withholds_a_latest_source_the_caller_can_no_longer_see(vault: Path) -> None:
    schema = schema_module.load_source_schema(vault)
    with request_scope(_audience("client-a")):
        _record(vault)
    _withhold_episodes_from(vault, "client-a")

    with request_scope(_audience("client-a")):
        inspected = commands.op_episode_memory(vault, schema, action="inspect", episode=KEY)

    # A withheld ref is not recoverable either: the two fields agree.
    assert inspected["revisions"] == [{"revision": 1, "recovery": "unavailable"}]
    assert inspected["latest_source_ref"] is None


def test_inspect_answers_unknown_and_other_audience_keys_identically(vault: Path) -> None:
    schema = schema_module.load_source_schema(vault)
    with request_scope(_audience("client-a")):
        _record(vault)
    with request_scope(_audience("client-b")):
        with pytest.raises(ValueError) as other:
            commands.op_episode_memory(vault, schema, action="inspect", episode=KEY)
        with pytest.raises(ValueError) as unknown:
            commands.op_episode_memory(
                vault, schema, action="inspect", episode="ep-" + "e5" * 16
            )

    assert str(other.value) == str(unknown.value)
    assert "EPISODE_NOT_FOUND" in str(other.value)


def test_inspect_takes_nothing_but_the_episode(vault: Path) -> None:
    schema = schema_module.load_source_schema(vault)
    with request_scope(owner_principal(surface="mcp")):
        with pytest.raises(ValueError, match="EPISODE_INVALID"):
            commands.op_episode_memory(
                vault, schema, action="inspect", episode=KEY, subject="smuggled"
            )
        with pytest.raises(ValueError, match="EPISODE_KEY_INVALID"):
            commands.op_episode_memory(vault, schema, action="inspect", episode=None)


# --------------------------------------------------------------------------- #
# Doors
# --------------------------------------------------------------------------- #


def _door_server(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    for leaky in ("EXOMEM_UPLOAD_TOKEN", "EXOMEM_CF_ACCESS_TEAM_DOMAIN", "EXOMEM_CF_ACCESS_AUD"):
        monkeypatch.delenv(leaky, raising=False)
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "sekret")
    return server.build_server(require_auth=False)


def _rest_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    return TestClient(_door_server(monkeypatch).http_app())


def _payload(key: str) -> dict:
    return {
        "action": "record",
        "episode": key,
        "subject": "Harbor Lamp purchase",
        "summary": "Chose the brass lamp.",
        "worked_on": ["Compared two lamps"],
    }


def test_three_doors_record_through_one_leaf(
    vault: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    mcp = _door_server(monkeypatch)
    client = TestClient(mcp.http_app())
    rest = client.post(
        "/api/episode_memory",
        json=_payload("ep-" + "b2" * 16),
        headers={"Authorization": "Bearer sekret"},
    )
    assert rest.status_code == 200, rest.text
    rest_result = rest.json()["data"]

    with request_scope(owner_principal(surface="mcp")):
        called = asyncio.run(
            mcp.call_tool("episode_memory", _payload("ep-" + "c3" * 16), run_middleware=False)
        )
    mcp_result = (
        called.structured_content
        if isinstance(called.structured_content, dict)
        else json.loads(called.content[0].text)
    )

    argv = [
        "episode_memory",
        "--action",
        "record",
        "--episode",
        "ep-" + "d4" * 16,
        "--subject",
        "Harbor Lamp purchase",
        "--summary",
        "Chose the brass lamp.",
        "--worked-on",
        "Compared two lamps",
        "--json",
    ]
    try:
        code = cli_main(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    out = capsys.readouterr().out
    assert code == 0, out
    cli_result = json.loads(out)["data"]

    for result in (rest_result, mcp_result, cli_result):
        assert RESULT_KEYS <= set(result), result
        assert result["revision"] == 1
        assert result["ledger"] == "bound"
    assert len(_episodes(vault)) == 3


def test_unknown_fields_and_leaves_are_refused_at_every_door(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    client = _rest_client(monkeypatch)
    for field in ("leaves", "effects", "payload"):
        response = client.post(
            "/api/episode_memory",
            json={**_payload("ep-" + "b2" * 16), field: ["anything"]},
            headers={"Authorization": "Bearer sekret"},
        )
        assert response.status_code >= 400, response.text
        assert "UNKNOWN_PARAM" in response.text
    # Candidate fields belong to the typed candidate actions (task 3.3); a
    # record carrying one is refused before anything is written.
    for field, value in (("proposal", {"route": "no_capture"}), ("disposition", "routed")):
        response = client.post(
            "/api/episode_memory",
            json={**_payload("ep-" + "b2" * 16), field: value},
            headers={"Authorization": "Bearer sekret"},
        )
        assert response.status_code >= 400, response.text
        assert "EPISODE_INVALID" in response.text

    mcp = server.build_server(require_auth=False)
    with request_scope(owner_principal(surface="mcp")):
        with pytest.raises(Exception):  # noqa: B017 - the MCP schema refuses the field
            asyncio.run(
                mcp.call_tool(
                    "episode_memory",
                    {**_payload("ep-" + "c3" * 16), "leaves": ["x"]},
                    run_middleware=False,
                )
            )

    assert _episodes(vault) == []


# --------------------------------------------------------------------------- #
# Guidance: an agent can find the tool and knows when to use it
# --------------------------------------------------------------------------- #


def test_the_action_catalog_reaches_the_tool_from_capture() -> None:
    catalog = commands.simple_action_catalog()
    assert "episode_memory" in catalog["capture"]["advanced"]


def test_the_scaffold_skill_routes_conversation_recaps_to_the_tool() -> None:
    scaffold = Path(commands.__file__).parent / "_scaffold" / "_Schema"
    skill = (scaffold / "SKILL.md").read_text(encoding="utf-8")
    routing = (scaffold / "references" / "operation-routing.md").read_text(encoding="utf-8")
    engagement = (scaffold / "references" / "engagement.md").read_text(encoding="utf-8")

    assert "`episode_memory`" in skill
    assert "**episode_memory**" in routing
    assert "episode_memory" in engagement and "episode_due" in engagement
