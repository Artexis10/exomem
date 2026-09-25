"""The referent of a turn that names nothing, read from the heat projection.

End to end through `commands.op_activate_context`: what the user deliberately
did — a governed edit, an agent's pick, an episode — decides what "continue"
refers to, and nothing a maintenance batch or a sync did can erase it. Ruling
S5-1 scopes that to the caller's own thread first: the calling session, then
its workspace, then the vault, so parallel sessions on unrelated topics each
continue their own work.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from test_governance_egress import _external, _reset_caches, write_rule, write_scope
from test_working_set_carry import CARRY_PAGE, _seed_carry_pages
from test_working_set_index import _seed_planning, _seed_structure

from exomem import (
    commands,
    file_watcher,
    freshness,
    lexstore,
    working_set_heat,
    working_set_index,
    working_set_runtime,
    writer_lease,
)
from exomem import vault as vault_module
from exomem.governance.principal import request_scope

SLED = "Knowledge Base/Products/Cargo Sled.md"
MARIT = "Knowledge Base/Entities/People/Marit Solheim.md"
DEPOT = "Knowledge Base/Systems/Depot Ledger.md"
HUB = "Knowledge Base/Notes/Insights/northern-corridor-hub.md"
NONSENSE_TURN = "zqxwvu plonktastic frobnitz quibblewhomp"
SLED_TURN = "I'm planning to tow the Cargo Sled north — what are its constraints?"
MARIT_TURN = "Tell me about Marit Solheim."
_SLED_ID = "0b7c9e2a-4f1d-4c3a-9e8b-5a6d7c8e9f01"
_MARIT_ID = "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f"


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #


def _live(vault: Path) -> None:
    file_watcher.FileWatcher(vault)._reconcile_once(seed=True)


def _pages(vault: Path) -> list[Path]:
    return sorted((vault / "Knowledge Base").rglob("*.md"))


def _one_old_tick(vault: Path, *, days: float = 30) -> None:
    """Every page written at one instant long ago: one burst, so the cold seed
    carries no edit and every event these tests look at is one they made."""
    stamp = time.time() - days * 86400
    for page in _pages(vault):
        os.utime(page, (stamp, stamp))


@pytest.fixture
def heat_vault(vault: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _seed_structure(vault)
    _seed_planning(vault)
    _seed_carry_pages(vault)
    working_set_index.WorkingSetIndex(vault).rebuild()
    # Before the registry goes live: clearing find's caches clears it too.
    _reset_caches()
    _one_old_tick(vault)
    _live(vault)
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_heat.reset_for_tests()
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    return vault


def _command(name: str):
    return next(command for command in commands.PRODUCT_COMMANDS if command.name == name)


def _edit(vault: Path, rel: str, old: str, new: str) -> dict:
    """A governed edit, exactly as a client makes one."""
    return writer_lease.invoke_command(
        _command("edit_memory"),
        vault,
        path=rel,
        why="hot projection test",
        operation={"kind": "replace_string", "old_string": old, "new_string": new},
    )


def _traced_commit(vault: Path, rels: list[str], *, command: str = "edit_memory") -> None:
    """One governed commit of several pages under one mutation trace: what a
    multi-page command writes, at one time."""
    token = writer_lease._ACTIVE_MUTATION_TRACE.set(("request-under-test", command, "none"))
    try:
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(
                    path=vault / rel,
                    content=(vault / rel).read_text(encoding="utf-8") + "\nOne more line.\n",
                )
                for rel in rels
            ],
            vault_root=vault,
        )
    finally:
        writer_lease._ACTIVE_MUTATION_TRACE.reset(token)


def _watcher_saw_everything(vault: Path) -> None:
    """The watcher's events for every page, as the running service delivers them."""
    assert freshness.is_live(vault, "kb"), "the registry must be live, or this proves nothing"
    freshness.on_files_changed(vault, changed=_pages(vault))
    working_set_runtime.reset_caches_for_tests()


def _continue(vault: Path, **kwargs) -> dict:
    return commands.op_activate_context(vault, turn="continue", **kwargs)


def _resolved(packet: dict) -> list[str]:
    return [item["path"] for item in packet["anchors"] if item["status"] == "resolved"]


def _give_id(vault: Path, rel: str, exomem_id: str) -> None:
    page = vault / rel
    page.write_text(
        page.read_text(encoding="utf-8").replace("---\n", f"---\nexomem_id: {exomem_id}\n", 1),
        encoding="utf-8",
    )


def _backfill(vault: Path) -> None:
    writer_lease.invoke_command(
        _command("maintain_memory"), vault, mode="backfill-ids", dry_run=False
    )


# --------------------------------------------------------------------------- #
# Deferred limit 1: a maintenance batch cannot erase the user's last work
# --------------------------------------------------------------------------- #


def test_a_maintenance_batch_leaves_the_users_last_page_hot(heat_vault: Path) -> None:
    """The U2 reviewer's shape [e], through a real batch: the user edited
    Marit Solheim once, then Cargo Sled; a `backfill-ids` pass then rewrote
    Cargo Sled (it had no identifier) and much else. Before the projection the
    batch's timestamp replaced the user's and "continue" abstained or, worse,
    promoted the older Marit edit."""
    _give_id(heat_vault, MARIT, _MARIT_ID)
    _edit(heat_vault, MARIT, "Freight coordinator", "Senior freight coordinator")
    _edit(heat_vault, SLED, "A towed cargo sled", "A towed freight sled")
    _backfill(heat_vault)
    rewritten = working_set_heat.attributed_signatures(
        heat_vault, [str(page.relative_to(heat_vault)) for page in _pages(heat_vault)]
    )
    assert len(rewritten) >= 3 and SLED in rewritten, "the batch must rewrite the user's page"
    _watcher_saw_everything(heat_vault)

    packet = _continue(heat_vault)

    assert _resolved(packet) == [SLED], (packet.get("abstention"), packet["anchors"])
    assert "recency" in packet["anchors"][0]["evidence"]


@pytest.mark.parametrize("read", [None, SLED], ids=["nothing-read", "sled-read"])
def test_a_maintenance_batch_does_not_pick_the_referent(heat_vault: Path, read: str | None) -> None:
    """Re-based from `test_working_set_continuity` (it drove a governed batch).
    The user last edited Cargo Sled; a maintenance pass then rewrote the
    vault. The batch never picks the referent — and now, because it writes no
    event, it no longer erases the user's edit either: "continue" resolves
    Cargo Sled whether or not anything was read, where it used to abstain."""
    _give_id(heat_vault, SLED, _SLED_ID)
    _edit(heat_vault, SLED, "A towed cargo sled", "A towed freight sled")
    if read:
        commands.op_read_memory(heat_vault, path=read)
    before = _continue(heat_vault)
    _backfill(heat_vault)
    _watcher_saw_everything(heat_vault)

    after = _continue(heat_vault)

    assert _resolved(before) == [SLED]
    assert _resolved(after) == [SLED], (after.get("abstention"), after["anchors"])


@pytest.mark.parametrize("read", [None, SLED], ids=["nothing-read", "sled-read"])
def test_a_batch_that_rewrote_the_users_page_never_promotes_an_old_edit(
    heat_vault: Path, read: str | None
) -> None:
    """Re-based from `test_working_set_continuity`: Marit Solheim edited once
    long before, Cargo Sled last; the batch rewrote Cargo Sled. Marit must
    never become the referent — and Cargo Sled, the user's actual last work,
    now stays it instead of the turn abstaining."""
    _give_id(heat_vault, MARIT, _MARIT_ID)
    _edit(heat_vault, MARIT, "Freight coordinator", "Senior freight coordinator")
    _edit(heat_vault, SLED, "A towed cargo sled", "A towed freight sled")
    if read:
        commands.op_read_memory(heat_vault, path=read)
    assert _resolved(_continue(heat_vault)) == [SLED]
    _backfill(heat_vault)
    _watcher_saw_everything(heat_vault)

    after = _continue(heat_vault)

    assert MARIT not in _resolved(after), after["anchors"]
    assert _resolved(after) == [SLED], (after.get("abstention"), after["anchors"])


# --------------------------------------------------------------------------- #
# Deferred limit 2: origin, not timing, tells a batch from work
# --------------------------------------------------------------------------- #


def test_an_edit_a_fraction_of_a_second_after_a_batch_is_the_referent(heat_vault: Path) -> None:
    _give_id(heat_vault, MARIT, _MARIT_ID)
    _backfill(heat_vault)
    _edit(heat_vault, MARIT, "Freight coordinator", "Senior freight coordinator")
    _watcher_saw_everything(heat_vault)

    packet = _continue(heat_vault)

    assert _resolved(packet) == [MARIT], (packet.get("abstention"), packet["anchors"])


def test_a_multi_page_work_commit_is_not_a_burst(heat_vault: Path) -> None:
    """Three pages one command wrote together are three work events at one
    time — an honest tie the existing complementary-or-ambiguous rule decides —
    never a burst that erases the work."""
    _traced_commit(heat_vault, [SLED, MARIT, DEPOT])
    _watcher_saw_everything(heat_vault)

    packet = _continue(heat_vault)

    offered = set(_resolved(packet)) | {item["ref"] for item in packet["ambiguity"]} | {
        item.get("path") for item in packet["ambiguity"]
    }
    assert {SLED, MARIT, DEPOT} <= offered, (packet.get("abstention"), packet["anchors"])
    assert packet.get("abstention") != {"reason": "unresolved"}


def test_an_old_edit_yields_to_the_latest_session(heat_vault: Path) -> None:
    """Decay is a session window, not a score: an edit ten days old yields to
    a read made in the latest session."""
    ten_days = time.time() - 10 * 86400
    os.utime(heat_vault / MARIT, (ten_days, ten_days))
    freshness.on_files_changed(heat_vault, changed=[heat_vault / MARIT])
    assert _resolved(_continue(heat_vault)) == [MARIT]

    commands.op_read_memory(heat_vault, path=SLED)

    assert _resolved(_continue(heat_vault)) == [SLED]


# --------------------------------------------------------------------------- #
# Picks and pages
# --------------------------------------------------------------------------- #


def test_a_pick_then_continue_resumes_the_picked_page(heat_vault: Path) -> None:
    """Batch review a2: continue after a pick served another anchor. The page
    is an ordinary research note, not an anchor."""
    _edit(heat_vault, SLED, "A towed cargo sled", "A towed freight sled")
    picked = commands.op_activate_context(heat_vault, turn=NONSENSE_TURN, anchor=CARRY_PAGE)
    token = picked["continuity"]

    packet = _continue(heat_vault, continuity=token)

    (anchor,) = packet["anchors"]
    assert anchor["path"] == CARRY_PAGE and anchor["kind"] == "page"
    assert anchor["status"] == "resolved"
    assert anchor["evidence"] == ["continuity", "recency"]
    assert packet["generation"]["carried_by"] == "continuity"
    assert packet["units"]


def test_a_pick_moves_the_referent_for_a_fresh_session(heat_vault: Path) -> None:
    _edit(heat_vault, SLED, "A towed cargo sled", "A towed freight sled")
    assert _resolved(_continue(heat_vault)) == [SLED]
    commands.op_activate_context(heat_vault, turn=NONSENSE_TURN, anchor=CARRY_PAGE)

    # No token: a fresh session on another client.
    packet = _continue(heat_vault)

    assert _resolved(packet) == [CARRY_PAGE], (packet.get("abstention"), packet["anchors"])


def test_a_single_hot_page_is_carried_on_recency(heat_vault: Path) -> None:
    _traced_commit(heat_vault, [CARRY_PAGE])

    packet = _continue(heat_vault)

    assert packet["abstained"] is False, (packet.get("abstention"), packet["anchors"])
    (anchor,) = packet["anchors"]
    assert anchor["path"] == CARRY_PAGE and anchor["kind"] == "page"
    assert anchor["status"] == "resolved"
    assert anchor["evidence"] == ["recency"]
    assert packet["generation"]["carried_by"] == "recency"
    # The page is carried forward, so the next "continue" in this
    # conversation resumes it through its token.
    assert CARRY_PAGE in working_set_runtime.decode_continuity(packet["continuity"])["refs"]


def test_a_page_tied_with_an_anchor_is_reported_not_guessed(heat_vault: Path) -> None:
    _traced_commit(heat_vault, [CARRY_PAGE, SLED])

    packet = _continue(heat_vault)

    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "ambiguous"}
    listed = {item["ref"]: item for item in packet["ambiguity"]}
    assert CARRY_PAGE in listed and listed[CARRY_PAGE]["kind"] == "page"
    assert listed[CARRY_PAGE]["neighbourhood_size"] == 0
    assert any(item["ref"] != CARRY_PAGE for item in packet["ambiguity"])
    assert packet["units"] == []


def test_a_superseded_or_retired_hot_page_is_never_offered(heat_vault: Path) -> None:
    _traced_commit(heat_vault, [SLED])
    retired = heat_vault / CARRY_PAGE
    retired.write_text(
        retired.read_text(encoding="utf-8").replace(
            "status: active", 'status: active\nsuperseded_by: "[[Cargo Sled]]"', 1
        ),
        encoding="utf-8",
    )
    archived = heat_vault / DEPOT
    archived.write_text(
        archived.read_text(encoding="utf-8").replace("status: active", "status: archived", 1),
        encoding="utf-8",
    )
    working_set_index.WorkingSetIndex(heat_vault).rebuild()
    # Both retirements are the newest edits in the vault.
    freshness.on_files_changed(heat_vault, changed=[retired, archived])
    working_set_runtime.reset_caches_for_tests()

    packet = _continue(heat_vault)

    offered = {item.get("path") for item in packet["anchors"]} | {
        item["ref"] for item in packet["ambiguity"]
    }
    assert CARRY_PAGE not in offered and DEPOT not in offered
    assert _resolved(packet) == [SLED], (packet.get("abstention"), packet["anchors"])


def test_a_withheld_hot_page_abstains_withheld_without_a_runner_up(heat_vault: Path) -> None:
    _traced_commit(heat_vault, [SLED])
    _traced_commit(heat_vault, [CARRY_PAGE])
    write_scope(heat_vault, paths="Knowledge Base/Notes/Research/*", name="Research")
    write_rule(heat_vault, ceiling=0)
    _reset_caches()
    working_set_runtime.reset_caches_for_tests()

    with request_scope(_external()):
        packet = _continue(heat_vault)

    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "withheld"}
    assert packet["anchors"] == [], "no runner-up: Cargo Sled was never the referent"
    assert CARRY_PAGE not in {entry["path"] for entry in packet["recent_context"]}


def test_a_named_rare_anchor_wins_over_the_hottest_page(heat_vault: Path) -> None:
    _traced_commit(heat_vault, [CARRY_PAGE])

    packet = commands.op_activate_context(heat_vault, turn=MARIT_TURN)

    assert MARIT in _resolved(packet)
    assert CARRY_PAGE not in {item.get("path") for item in packet["anchors"]}
    assert all("recency" not in item["evidence"] for item in packet["anchors"])


def test_generation_reports_the_profile_state(heat_vault: Path, tmp_path: Path) -> None:
    # The whole vault was written in one tick long ago: the seed carries no
    # edit, only the captured sessions it always exempted, and no session yet.
    first = _continue(heat_vault)
    assert first["generation"]["hot_profile"] == {"state": "seeded", "session_start": ""}

    _traced_commit(heat_vault, [SLED])
    packet = _continue(heat_vault)
    today = time.strftime("%Y-%m-%d")
    assert packet["generation"]["hot_profile"] == {"state": "current", "session_start": today}


def test_the_profile_reports_seeded_on_a_vault_that_was_worked_on_before(
    vault: Path,
) -> None:
    _seed_structure(vault)
    working_set_index.WorkingSetIndex(vault).rebuild()
    now = time.time()
    for index, page in enumerate(_pages(vault)):
        os.utime(page, (now - 10_000 - index * 60, now - 10_000 - index * 60))
    os.utime(vault / SLED, (now - 60, now - 60))
    _live(vault)
    working_set_heat.reset_for_tests()

    packet = _continue(vault)

    assert packet["generation"]["hot_profile"]["state"] == "seeded"
    assert _resolved(packet) == [SLED]


# --------------------------------------------------------------------------- #
# Ruling S5-1: the caller's own thread first
# --------------------------------------------------------------------------- #

S1 = {"client": "claude-code", "session": "ep-" + "a1" * 16, "workspace": "0f" * 12}
S2 = {"client": "claude-code", "session": "ep-" + "b2" * 16, "workspace": "1e" * 12}


def test_two_parallel_sessions_each_continue_their_own_thread(heat_vault: Path) -> None:
    """The owner's case: two sessions on unrelated topics, interleaved, and
    maintenance plus another session's edits happening meanwhile. After a
    compaction each drops its token, and "continue" must still bring back its
    own thread."""
    commands.op_activate_context(heat_vault, turn=SLED_TURN, **S1)
    commands.op_activate_context(heat_vault, turn=MARIT_TURN, **S2)
    _edit(heat_vault, DEPOT, "The ledger that tracks", "The ledger which tracks")
    commands.op_activate_context(heat_vault, turn=SLED_TURN, **S1)
    commands.op_activate_context(heat_vault, turn=MARIT_TURN, **S2)
    _backfill(heat_vault)
    _watcher_saw_everything(heat_vault)

    first = _continue(heat_vault, **S1)
    second = _continue(heat_vault, **S2)

    assert _resolved(first) == [SLED], (first.get("abstention"), first["anchors"])
    assert _resolved(second) == [MARIT], (second.get("abstention"), second["anchors"])
    # Each again, in the other order: neither answer was the other's cached one.
    assert _resolved(_continue(heat_vault, **S2)) == [MARIT]
    assert _resolved(_continue(heat_vault, **S1)) == [SLED]


def test_a_fresh_session_in_the_same_workspace_continues_that_workspaces_thread(
    heat_vault: Path,
) -> None:
    commands.op_activate_context(heat_vault, turn=SLED_TURN, **S1)
    commands.op_activate_context(heat_vault, turn=MARIT_TURN, **S2)
    _edit(heat_vault, DEPOT, "The ledger that tracks", "The ledger which tracks")

    fresh = {**S1, "session": "ep-" + "c3" * 16}
    packet = _continue(heat_vault, **fresh)

    assert _resolved(packet) == [SLED], (packet.get("abstention"), packet["anchors"])


def test_a_fresh_session_with_no_keys_uses_the_vault_wide_ranking(heat_vault: Path) -> None:
    """ChatGPT and every client that carries no attribution: today's ranking,
    the latest deliberate act in the vault."""
    commands.op_activate_context(heat_vault, turn=SLED_TURN, **S1)
    commands.op_activate_context(heat_vault, turn=MARIT_TURN, **S2)
    _edit(heat_vault, DEPOT, "The ledger that tracks", "The ledger which tracks")

    packet = _continue(heat_vault)
    profile = working_set_heat.profile(heat_vault)

    assert _resolved(packet) == [DEPOT], (packet.get("abstention"), packet["anchors"])
    assert working_set_heat.members(working_set_heat.leading(profile)).paths == (DEPOT,)
    # A session key the vault has never seen is a caller with nothing of its own.
    stranger = _continue(heat_vault, client="codex", session="ep-" + "d4" * 16)
    assert _resolved(stranger) == [DEPOT]


def test_reads_in_another_session_never_outrank_own_session_work(heat_vault: Path) -> None:
    commands.op_activate_context(heat_vault, turn=SLED_TURN, **S1)
    for _ in range(5):
        commands.op_read_memory(heat_vault, path=HUB)
    commands.op_activate_context(heat_vault, turn=NONSENSE_TURN, anchor=CARRY_PAGE, **S2)

    assert _resolved(_continue(heat_vault, **S1)) == [SLED]
    assert _resolved(_continue(heat_vault, **S2)) == [CARRY_PAGE]


def test_no_raw_session_id_or_cwd_is_persisted(
    heat_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook passes only hashes; the server stores only its own derivation
    of those. Scans the heat sidecar, every host-local log and the call ledger."""
    import asyncio
    import importlib.util
    import sqlite3
    from types import SimpleNamespace

    from exomem import call_ledger, query_log
    from exomem import server as server_module

    hook_path = Path(commands.__file__).parent / "_hooks" / "exomem_retrieve_nudge.py"
    spec = importlib.util.spec_from_file_location("retrieve_hook_under_test", hook_path)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)

    raw_session = "raw-session-id-7f3c9e1d-quokka"
    workdir = tmp_path / "quokka-workspace-marker" / "src"
    workdir.mkdir(parents=True)
    (tmp_path / "quokka-workspace-marker" / ".git").mkdir()
    attribution = hook.attribution(raw_session, cwd=str(workdir))
    assert attribution["session"] != raw_session and attribution["workspace"]
    assert "quokka" not in "".join(attribution.values())

    logs = tmp_path / "logs"
    monkeypatch.setenv("EXOMEM_LOG_DIR", str(logs))
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(logs / "ledger"))
    monkeypatch.delenv("EXOMEM_DISABLE_CALL_LEDGER", raising=False)
    monkeypatch.setattr(query_log, "_disabled", lambda: False)
    call_ledger.reset_chain_cache()

    def drive(arguments: dict) -> None:
        async def call_next(_context):
            return commands.op_activate_context(heat_vault, **arguments)

        context = SimpleNamespace(
            message={"params": {"name": "activate_context", "arguments": arguments}}
        )
        asyncio.run(server_module.CallTraceMiddleware().on_call_tool(context, call_next))

    drive({"turn": SLED_TURN, **attribution})
    drive({"turn": "continue", **attribution})
    drive({"turn": NONSENSE_TURN, "anchor": CARRY_PAGE, **attribution})
    call_ledger.reset_chain_cache()

    stored = "\n".join(sqlite3.connect(working_set_heat.sidecar_path(heat_vault)).iterdump())
    written = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in logs.rglob("*")
        if path.is_file()
    )
    assert (logs / "activations.jsonl").exists() and list((logs / "ledger").rglob("*.jsonl"))
    for secret in (raw_session, attribution["session"], attribution["workspace"], str(workdir)):
        assert secret not in stored, secret
        assert secret not in written, secret
    assert "quokka" not in stored and "quokka" not in written


# --------------------------------------------------------------------------- #
# The recent-context block reads the projection
# --------------------------------------------------------------------------- #


def _recent(packet: dict, why: str | None = None) -> list[str]:
    return [
        entry["path"] for entry in packet["recent_context"] if why is None or entry["why"] == why
    ]


def test_recent_context_omits_batch_written_pages(heat_vault: Path) -> None:
    """Batch review a2: seven of eight entries were pages a batch wrote. A
    batch records no event, so the block offers what the user worked on."""
    _edit(heat_vault, SLED, "A towed cargo sled", "A towed freight sled")
    _backfill(heat_vault)
    _watcher_saw_everything(heat_vault)

    packet = commands.op_activate_context(heat_vault, turn=NONSENSE_TURN)

    assert _recent(packet, "edited") == [SLED], packet["recent_context"]


def test_recent_context_offers_reads_beside_edits(heat_vault: Path) -> None:
    """Every channel competes on its own time: a read made after nine edits
    is offered first, where it used to be starved by the edits' mtimes."""
    notes = sorted(
        str(page.relative_to(heat_vault))
        for page in (heat_vault / "Knowledge Base" / "Notes" / "Journal").glob("*.md")
    )[:9]
    for rel in notes:
        _traced_commit(heat_vault, [rel])
    commands.op_read_memory(heat_vault, path=HUB)

    packet = commands.op_activate_context(heat_vault, turn=NONSENSE_TURN)

    assert packet["recent_context"][0]["path"] == HUB
    assert packet["recent_context"][0]["why"] == "activated"
    assert _recent(packet, "edited")[:3] == list(reversed(notes))[:3]


def test_recent_context_reads_no_freshness_map_after_the_seed(
    heat_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The map is copied once per sidecar, by the cold seed. Watched at the
    copy itself (`freshness.live_entries`), since a copy anywhere on the
    request path is the O(N) cost the projection exists to remove."""
    _traced_commit(heat_vault, [SLED])
    assert _recent(commands.op_activate_context(heat_vault, turn=NONSENSE_TURN)), "seeded"

    copies: list[str] = []
    real = freshness.live_entries

    def watched(vault_root, scope):
        copies.append(scope)
        return real(vault_root, scope)

    monkeypatch.setattr(freshness, "live_entries", watched)
    _traced_commit(heat_vault, [MARIT])
    working_set_runtime.reset_caches_for_tests()

    packet = commands.op_activate_context(heat_vault, turn=NONSENSE_TURN)

    assert copies == [], "the freshness map was copied on the request path"
    assert _recent(packet)[:2] == [MARIT, SLED], packet["recent_context"]


def test_recent_context_leads_with_the_callers_own_thread(heat_vault: Path) -> None:
    """Ruling S5-1 in the block: the calling session's own pages first, then
    its workspace's, then the vault's, each list filled in that order."""
    commands.op_activate_context(heat_vault, turn=SLED_TURN, **S1)
    commands.op_activate_context(heat_vault, turn=MARIT_TURN, **S2)
    _edit(heat_vault, DEPOT, "The ledger that tracks", "The ledger which tracks")

    first = commands.op_activate_context(heat_vault, turn=NONSENSE_TURN, **S1)
    second = commands.op_activate_context(heat_vault, turn=NONSENSE_TURN, **S2)
    fresh = commands.op_activate_context(
        heat_vault, turn=NONSENSE_TURN, **{**S1, "session": "ep-" + "c3" * 16}
    )
    keyless = commands.op_activate_context(heat_vault, turn=NONSENSE_TURN)

    assert _recent(first)[0] == SLED, first["recent_context"]
    assert _recent(second)[0] == MARIT, second["recent_context"]
    assert _recent(fresh)[0] == SLED, fresh["recent_context"]
    # Filled from below: the vault's newest work is still offered after it.
    assert DEPOT in _recent(first) and DEPOT in _recent(second)
    # A caller with no keys gets the vault's order: the newest contact first.
    assert _recent(keyless)[0] == DEPOT, keyless["recent_context"]
