"""Store-mode bookkeeping in a served process (OpenSpec make-vault-store-canonical 4.1, 4.2, 6.4).

The service starts as a service starts (``writer_lease.start_server_lifecycle``), so the store
thread binds its own production writer (``preview.preview_store`` over the runtime's checkout) and
each write meets the store's real lease check, which requires the writing thread to hold the vault
boundary. The projector, the importer and the media writers reach the store thread through
``projector.store_thread_runner``, which takes that boundary on the store thread.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import signal
import statistics
import sys
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_media_store as media_store_suite
import test_vault_store_read_write as read_write
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_media_store import _offload, _volume
from test_scene_frames import _FakeImg, _scene
from test_vault_store_binary_writers import VIDEO, frame_rows
from test_vault_store_bypass_writers import RAW_SOURCE, govern
from test_vault_store_derivation_queue import HOLD_GROWTH_LIMIT_MS, SLOW_DERIVATION_SECONDS, holds
from test_vault_store_governed_writers import tombstones
from test_vault_store_media import PICTURE, PICTURE_BYTES
from test_vault_store_migration import ALPHA, BETA, DIAGRAM, DIAGRAM_BYTES, vault  # noqa: F401
from test_vault_store_write_path import bm25_paths, store_rows

from exomem import (
    commands,
    deferred_index,
    epistemic_graph,
    extract,
    extraction_parts,
    freshness,
    graph_drain,
    lexstore,
    media_jobs,
    media_replicas,
    media_store,
    media_worker,
    mutation_lock,
    readiness,
    relation_review,
    reserved_paths,
    scene_frames,
    schema,
    server_runtime,
    service_standby,
    warmup,
    writer_lease,
)
from exomem.cli_ops import OpError
from exomem.collection_store import connection, runtime
from exomem.governance import lifecycle
from exomem.governance.principal import owner_principal, request_scope
from exomem.vault_store import derivation, derive, importer, projector, read, rederive, write
from exomem.vault_store import media as store_media
from exomem.vault_store import mode as store_mode
from exomem.writer_lease import get_manager

pytestmark = pytest.mark.timeout(300)

stored = read_write.stored
selected_store = media_store_suite.selected_store

SCHEMA = "Knowledge Base/_Schema"
NOTES = "Knowledge Base/Notes"


@pytest.fixture
def service(stored, monkeypatch):
    """The service's own store thread over the store-mode vault, started as a service starts it.

    It records when each registry tick starts and how it ends: ``ended`` when a request ended its
    wait for the vault boundary (``StoreServer._maintaining``), ``returned`` otherwise.
    """
    ticks = SimpleNamespace(entered=[], ended=[], returned=[])
    real = runtime.StoreServer._registry_tick

    def recording(self):
        ticks.entered.append(time.monotonic())
        try:
            outcome = real(self)
        except mutation_lock.WaitEnded:
            ticks.ended.append(time.monotonic())
            raise
        ticks.returned.append(time.monotonic())
        return outcome

    monkeypatch.setattr(runtime.StoreServer, "_registry_tick", recording)
    with store_thread(stored):
        yield ticks


@contextmanager
def store_thread(root):
    """The service's store thread on ``root``, as a service starts it, but with no projector."""
    manager = writer_lease.start_server_lifecycle()
    server = None
    try:
        deadline = time.monotonic() + 60
        while (server := runtime._SERVERS.get(root.resolve())) is None or server.runtime is None:
            assert time.monotonic() < deadline, "the store thread did not open the store"
            time.sleep(0.05)
        yield
    finally:
        manager.close()
        if server is not None:
            server.thread.join(10)
        writer_lease._MANAGERS.pop(manager.config, None)


def call(root, tool, **arguments):
    """One MCP command as the owner, through the dispatcher that routes a write to the store.

    It injects what the server injects: the vault, and its source schema for a command that needs
    it.
    """
    command = next(c for c in commands.product_commands_for("mcp") if c.name == tool)
    injected = (root, schema.load_source_schema(root)) if command.needs_schema else (root,)
    with request_scope(owner_principal(surface="mcp")):
        return writer_lease.invoke_command(command, *injected, **arguments)


def commit(root, **change):
    """A governed store commit through the store thread's door, as a background writer sends one."""
    projector.store_thread_runner(root)(
        lambda writer: write.commit(
            writer, write.StoreChange(write.Origin.GOVERNED, "served test", **change)
        )
    )


@pytest.mark.parametrize(("visible_name", "fragment", "hidden_body", "outcome"), [
    ("Beacon", "^signal", "- [rule] West signal. ^signal", "ambiguous"),
    ("Beacon Ledger", "signal", "## Signal\n\nWest signal.", "not_unit"),
])
def test_link_pages_resolve_the_visible_namesake_before_counting(
    stored, service, visible_name, fragment, hidden_body, outcome,
):
    source = f"{NOTES}/Dispatch.md"
    visible = f"{NOTES}/East/{visible_name}.md"
    hidden = f"{NOTES}/West/Beacon.md"
    commit(stored, puts=(
        write.PagePut(source, (f"# Dispatch\n\nFollow [[Beacon#{fragment}|the signal]].\n"
                               f"\n![[Beacon#{fragment}]]\n\n`[[Beacon#{fragment}]]`\n").encode()),
        write.PagePut(visible, b"---\ntitle: Beacon\n---\n# Beacon\n\n- [rule] East signal. ^signal\n"),
        write.PagePut(hidden, f"---\ntitle: Beacon\n---\n# Beacon\n\n{hidden_body}\n".encode()),
    ))
    derived(stored)
    assert store_rows(
        stored, "SELECT resolution FROM graph_unit_links WHERE source_path = ? ORDER BY occurrence", source,
    ) == [(outcome,), (outcome,)]

    graph = epistemic_graph.EpistemicGraphIndex(stored)
    assert graph.unit_links(visible, incoming=True).total == 0
    def allowed(path):
        return path != hidden

    first = graph.unit_links(visible, incoming=True, keep=allowed, limit=1)
    assert first.state == "ready" and first.total == 2
    assert [(path, link.embed) for path, link in first.links] == [(source, False)]
    second = graph.unit_links(visible, incoming=True, keep=allowed, limit=1, after=first.next_after)
    assert second.total == 2 and second.next_after is None
    assert [(path, link.embed) for path, link in second.links] == [(source, True)]
    assert graph.unit_links(hidden, incoming=True, keep=allowed).total == 0


def test_fragment_landing_follows_committed_bytes_while_the_projection_lags(stored, service, monkeypatch):
    source = f"{NOTES}/Watch.md"
    graph = epistemic_graph.EpistemicGraphIndex(stored)
    (stored / BETA).write_text("# Beta\n\n- [rule] Obsolete signal. ^removed\n")
    commit(stored, puts=(
        write.PagePut(BETA, b"---\ntitle: Beta\n---\n# Beta\n\n## Overview\n\n- [rule] Current signal. ^signal\n"),
        write.PagePut(source, b"# Watch\n\n[[Notes/Beta#^signal]] [[Notes/Beta#^removed]] [[Notes/Beta#Overview]]\n"),
    ))
    derived(stored)
    sql = "SELECT fragment, resolution FROM graph_unit_links WHERE source_path = ? ORDER BY occurrence"
    assert store_rows(stored, sql, source) == [
        ("^signal", "unit"), ("^removed", "missing"), ("Overview", "not_unit"),
    ]
    assert graph.unit_links(source).total == 1

    with monkeypatch.context() as patch:
        patch.setattr(derivation, "drain", lambda *_args, **_kwargs: derivation.Drained())
        commit(stored, deletes=(write.PageDelete(BETA),))
        assert store_rows(stored, "SELECT path FROM store_derivation_queue WHERE path = ?", BETA) == [(BETA,)]
        page = graph.unit_links(source)
        assert page.state == "lag" and page.total is None
    derived(stored)
    assert graph.unit_links(source).total == 0
    assert store_rows(stored, sql, source) == [
        ("^signal", "missing"), ("^removed", "missing"), ("Overview", "missing"),
    ]
    assert (stored / BETA).exists()


def test_link_pages_find_a_moved_claim_after_filtering_the_live_namesake(stored, service):
    source = f"{NOTES}/Dispatch.md"
    visible = f"{NOTES}/Moved Claim.md"
    hidden = f"{NOTES}/Current/Beacon.md"
    prior = f"{NOTES}/Prior/Beacon.md"
    moved_text = (
        "---\ntitle: Moved Claim\nexomem_id: 11111111-1111-4111-8111-111111111111\n"
        f"moved_from:\n- path: {prior}\n---\n# Moved Claim\n\n- [rule] East signal. ^signal\n"
    )
    commit(stored, puts=(
        write.PagePut(source, b"# Dispatch\n\n[[Beacon#^signal]] ![[Notes/Prior/Beacon#^signal]]\n"),
        write.PagePut(visible, moved_text.encode()),
        write.PagePut(hidden, b"# Beacon\n\n- [rule] West signal. ^signal\n"),
    ))
    derived(stored)
    graph = epistemic_graph.EpistemicGraphIndex(stored)

    def allowed(path):
        return path != hidden

    page = graph.unit_links(visible, incoming=True, keep=allowed)
    assert page.total == 2
    assert [path for path, _ in page.links] == [source, source]
    commit(stored, puts=(write.PagePut(visible, moved_text.replace("^signal", "^new").encode()),))
    derived(stored)
    assert store_rows(
        stored, "SELECT resolution FROM graph_unit_links WHERE target_path = ?", visible,
    ) == [("missing",)]
    assert graph.unit_links(visible, incoming=True, keep=allowed).total == 0


def test_a_served_assessment_commits_page_bytes_before_the_verdict_projection(stored, service, monkeypatch):
    path = f"{NOTES}/Insights/Signal Assessment.md"
    commit(stored, puts=(write.PagePut(
        path,
        b"---\ntitle: Signal Assessment\ntype: insight\nstatus: active\n---\n# Signal Assessment\n\n"
        b"See the [[Beacon Design]].\n\n"
        b"## Claim\n- id: signal\n- verdict: confirmed\n\nThe signal is visible.\n\n"
        b"## Claim\n- id: lamp\n- verdict: confirmed\n\nThe lamp is lit.\n",
    ), write.PagePut(
        f"{NOTES}/Insights/Beacon Design.md",
        b"---\ntitle: Beacon Design\ntype: insight\nstatus: active\n---\n# Beacon Design\n\n"
        b"## Observations\n\n- [rule] Keep the beacon lit.\n",
    )))
    derived(stored)
    projected = "SELECT anchor, verdict FROM semantic_units WHERE parent_path = ? AND anchor IS NOT NULL ORDER BY anchor"
    assert store_rows(stored, projected, path) == [("lamp", "confirmed"), ("signal", "confirmed")]
    assessment = dict(path=path, operation="assess", unit="signal", verdict="refuted", reason="Checked at the pier.")
    preview = call(stored, "observe_memory", **assessment, validate_only=True)
    with monkeypatch.context() as patch:
        patch.setattr(derivation, "drain", lambda *_args, **_kwargs: derivation.Drained())
        result = call(
            stored, "observe_memory", **assessment, response_detail="full",
            transition_token=preview["semantic"]["transition_token"],
        )
        assert result["diagnostics"]["operational"]["state"] == "committed"
        assert b"verdict: refuted" in read_write.stored_body(stored, path)
        assert store_rows(stored, projected, path) == [("lamp", "confirmed"), ("signal", "confirmed")]
        assert epistemic_graph.EpistemicGraphIndex(stored).unit_links(path).state == "lag"
    derived(stored)
    assert store_rows(stored, projected, path) == [("lamp", "confirmed"), ("signal", "refuted")]


@contextmanager
def held_elsewhere(root):
    """Another thread of this process holds the vault boundary, as a background holder does, until
    the block ends or the yielded event is set."""
    taken, release = threading.Event(), threading.Event()

    def hold():
        guard = get_manager().mutation_guard
        with guard(root, operation="another_holder", holder_kind="background"):
            taken.set()
            release.wait(60)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert taken.wait(30), "the other holder took the boundary"
    try:
        yield release
    finally:
        release.set()
        thread.join(30)


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def manifest(root, rel):
    return store_rows(root, "SELECT sha256, revision FROM store_manifest WHERE path = ?", rel)


def queued(root):
    return {path for (path,) in store_rows(root, "SELECT path FROM store_projection_queue")}


def test_a_note_trashed_through_the_served_door_comes_back_through_it(stored, service):
    """A person's note, created, trashed and recovered through the served door, as an agent does.

    The note has a stable identity and a reviewed-none decision, so its recovery binds the trash
    proof and needs the decision in force. A recovery that refuses the proof because the page lives
    in the store, or that looks for the decision in the projection, which has no file of it yet
    (this fixture runs no projector), leaves the note in the trash.
    """
    created = call(
        stored, "remember", title="Kept after all",
        content="# Kept after all\n\n## Claim\n\nA note that a person trashed by mistake.\n",
        relation_disposition="reviewed_none", relation_review_reason="a note that stands alone",
    )
    path = created["path"]
    body = read.owned_bytes(stored, path)
    trashed = call(
        stored, "manage_memory_file", operation="delete", path=path, confirm=True,
        response_detail="full",
    )
    # The reply acknowledges the page's derived rows, so the delete's own result is its leaf.
    trash_path = trashed["diagnostics"]["leaf_result"]["trash_path"]

    recovered = call(stored, "manage_memory_file", operation="recover", trash_path=trash_path)

    assert recovered["path"] == path
    assert read.owned_bytes(stored, path) == body
    assert not read.page_exists(stored, trash_path)


def test_a_governed_trash_through_the_served_door_settles_its_receipt_in_the_request(stored, service):
    """A governed page trashed as an agent trashes it. Settled before its request derived the rows,
    the trash found them deferred, warned that its state was not exact and left its receipt and
    tombstone pending until a reconcile; a recovery in that state keeps the page hidden."""
    govern(stored, BETA)

    reply = call(stored, "manage_memory_file", operation="delete", path=BETA, confirm=True,
                 force_orphan=True)

    assert not any("not exact" in warning for warning in reply.get("warnings", ()))
    assert [marker["state"] for marker in tombstones(stored)] == ["committed"]


def test_a_governed_trash_holds_the_boundary_no_longer_when_its_derivation_is_slow(
    stored, service, monkeypatch
):
    """A governed page trashed, recovered and trashed again through the served door, the second
    time with rows that take seconds to derive. Settled under the boundary, the trash held it for
    those seconds; it holds it no longer, and its request still settles its receipt."""
    govern(stored, BETA)
    held = holds(monkeypatch)
    delay = [0.0]
    real = derive.derive_paths

    def derive_paths(*arguments, **keywords):
        time.sleep(delay[0])
        return real(*arguments, **keywords)

    monkeypatch.setattr(derive, "derive_paths", derive_paths)
    call(stored, "manage_memory_file", operation="delete", path=BETA, confirm=True, force_orphan=True)
    quick = max(held)
    # A governed reply withholds the trashed path; its tombstone names it.
    [trashed] = tombstones(stored)[0]["manifest"]
    call(stored, "manage_memory_file", operation="recover", trash_path=trashed["trash_path"])
    held.clear()
    delay[0] = SLOW_DERIVATION_SECONDS

    reply = call(stored, "manage_memory_file", operation="delete", path=BETA, confirm=True,
                 force_orphan=True)

    slow = max(held)
    assert slow - quick < HOLD_GROWTH_LIMIT_MS, (
        f"the trash held the boundary {slow:.0f} ms with a {SLOW_DERIVATION_SECONDS:.0f} s "
        f"derivation, against {quick:.0f} ms with an instant one"
    )
    assert not any("not exact" in warning for warning in reply.get("warnings", ()))
    assert [marker["state"] for marker in tombstones(stored)][-1:] == ["committed"]


def test_a_governed_trash_whose_rows_cannot_be_derived_says_so_in_its_reply(
    stored, service, monkeypatch
):
    """A governed page trashed while its rows fail to derive. The trash settles after its terminal,
    so a warning that the settling adds then must still reach the reply, which otherwise says
    nothing while the content stays tombstoned."""
    govern(stored, BETA)

    def derive_paths(*arguments, **keywords):
        raise RuntimeError("the derivation fails")

    monkeypatch.setattr(derive, "derive_paths", derive_paths)

    reply = call(stored, "manage_memory_file", operation="delete", path=BETA, confirm=True,
                 force_orphan=True, response_detail="full")

    assert lifecycle.TRASH_NOT_EXACT in reply["warnings"]
    assert reply["diagnostics"]["operational"]["warnings_count"] == len(reply["warnings"])
    assert [marker["state"] for marker in tombstones(stored)] == ["pending"]


def test_a_full_reply_names_each_derived_component_as_its_acknowledgement_does(stored, service):
    """A move's and a delete's full replies, as an agent reads them: the writer's index report, made
    before the request derived the rows, named them deferred beside an acknowledgement that names
    them completed."""
    moved = f"{NOTES}/Beta Moved.md"
    replies = [
        call(stored, "manage_memory_file", operation="move", old_path=BETA, new_path=moved,
             response_detail="full"),
        call(stored, "manage_memory_file", operation="delete", path=moved, confirm=True,
             force_orphan=True, response_detail="full"),
    ]

    for reply in replies:
        acknowledged = {(item["component"], item["state"])
                        for item in reply["diagnostics"]["derived_components"]}
        index = reply["diagnostics"]["leaf_result"]["index"]
        # A delete's index is one report; a move's holds one per side of the move.
        reports = [index] if "components" in index else [*index["upsert_reports"], index["delete_report"]]
        reported = {(item["component"], item["outcome"]) for report in reports
                    for item in report["components"]
                    if item["component"] in {component for component, _state in acknowledged}}
        assert reported and reported <= acknowledged, (reported, acknowledged)


def derived(root, seconds=60):
    """Wait until the store thread has derived every intent that has not failed.

    A served write acknowledges at its commit, and the store thread derives its rows after that,
    so a raw read of derived rows right after a served call would race that drain. A request waits
    for it on its own (``read.request_snapshot``); a raw read does not.
    """
    deadline = time.monotonic() + seconds
    while store_rows(root, "SELECT 1 FROM store_derivation_queue WHERE failed_code IS NULL LIMIT 1"):
        assert time.monotonic() < deadline, "the store thread derived the rows of the served writes"
        time.sleep(0.02)


@contextmanager
def derivation_held(monkeypatch):
    """Every derivation on the store thread waits until the yielded event is set (or 30 s pass)."""
    release = threading.Event()
    real = derive.derive_paths

    def derive_paths(*arguments, **keywords):
        release.wait(30)
        return real(*arguments, **keywords)

    monkeypatch.setattr(derive, "derive_paths", derive_paths)
    try:
        yield release
    finally:
        release.set()


QUOKKA = {
    "title": "Quokka census", "status": "draft", "summary": "A census finding.",
    "content": "## Observations\n\n- [finding] The quokka population grew near [[Alpha]].\n",
}


def test_a_search_right_after_a_save_finds_the_page_and_the_graph_its_link(
    stored, service, monkeypatch
):
    """A save that acknowledges once its page is stored, and a search that starts right after it,
    before the store thread derived the page's rows. A search that read the rows as they stood would
    miss the page that the person just saved, and the graph would lack its link."""
    monkeypatch.setattr(derivation, "READ_WAIT_SECONDS", 10.0)  # a loaded host derives slowly
    reader_waits = threading.Event()
    real_caught_up = derivation.caught_up

    def caught_up(still_held_back, seconds):
        reader_waits.set()
        return real_caught_up(still_held_back, seconds)

    monkeypatch.setattr(derivation, "caught_up", caught_up)
    real = derive.derive_paths

    def derive_paths(*arguments, **keywords):
        # The rows follow only once a reader waits for them: no reader can start after them.
        reader_waits.wait(10)
        return real(*arguments, **keywords)

    monkeypatch.setattr(derive, "derive_paths", derive_paths)

    saved = call(stored, "remember", **QUOKKA, response_detail="full")
    found = call(stored, "ask_memory", query="quokka population")
    context = call(stored, "connect_memory", operation="context", path=saved["path"])

    assert saved["diagnostics"]["operational"]["derived_sync"] == "pending"
    assert saved["path"] in [hit["path"] for hit in found["hits"]]
    assert "notice" not in found
    assert f"file:{ALPHA}" in {edge["dst_key"] for edge in context["graph"]["edges"]}


def test_a_search_that_outwaits_its_bound_says_the_rows_lag_and_a_lookup_refuses(
    stored, service, monkeypatch
):
    """A save and a move whose rows the store thread derives only after a later search's wait ran
    out. The search serves the rows it has and says that recent pages lag; a read of the saved
    page's reference still finds it from its stored bytes, and a read of the moved page's old path
    refuses rather than answer that no such page exists."""
    archived = f"{NOTES}/Archive/Alpha.md"
    with derivation_held(monkeypatch) as release:
        saved = call(stored, "remember", **QUOKKA, response_detail="full")["diagnostics"]["leaf_result"]
        # An explanation keeps the search's internals, the lag figures among them.
        found = call(stored, "ask_memory", query="quokka population", explain=True)
        by_reference = call(stored, "read_memory", path=saved["ref"])
        release.set()
    derived(stored)
    with derivation_held(monkeypatch):
        call(stored, "manage_memory_file", operation="move", old_path=ALPHA, new_path=archived)
        with pytest.raises(Exception) as refused:  # noqa: PT011 - the dispatcher's error type varies
            call(stored, "read_memory", path=ALPHA)

    assert saved["path"] not in [hit["path"] for hit in found["hits"]]
    assert "not yet in the search" in found["notice"]
    assert found["lag"]["derived_pending"] >= 1
    assert isinstance(found["lag"]["oldest_derivation_age_seconds"], int)
    assert saved["path"] not in json.dumps(found["lag"]) and "Quokka" not in found["notice"]
    assert by_reference["path"] == saved["path"]
    assert read.DERIVED_BEHIND in str(refused.value), refused.value
    assert read.page_exists(stored, archived)


@contextmanager
def backlog_past_the_cap(root):
    """More pages wait to derive than one check reads, and the store thread derives none of them
    until the block ends, when it derives them all."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(derivation, "drain", lambda *_arguments, **_keywords: derivation.Drained())
        commit(root, puts=tuple(
            write.PagePut(f"{NOTES}/Backlog {number}.md", f"# Backlog {number}\n".encode())
            for number in range(derivation.UNDERIVED_CAP + 1)
        ))
        yield
    derivation.owe(root.resolve())
    derived(root)


def test_a_delete_served_beside_a_backlog_past_the_cap_refuses_retryably_and_commits_nothing(
    stored, service
):
    """A person's note, deleted through the served door while more pages wait to derive than one
    link check reads. The check runs on the store thread, which cannot wait for the drain, so the
    delete refuses with the retryable derived-behind error and the note stays. Once the store
    thread has derived the backlog, the same delete runs."""
    saved = call(stored, "remember", **QUOKKA)
    derived(stored)
    with backlog_past_the_cap(stored):
        revision = store_rows(stored, "SELECT revision FROM store_mode")
        with pytest.raises(OpError) as refused:
            call(stored, "manage_memory_file", operation="delete", path=saved["path"], confirm=True)
        unchanged = store_rows(stored, "SELECT revision FROM store_mode")
    deleted = call(stored, "manage_memory_file", operation="delete", path=saved["path"], confirm=True)

    assert refused.value.code == read.DERIVED_BEHIND
    assert "still being indexed" in str(refused.value) and "Nothing was saved" in str(refused.value)
    assert unchanged == revision
    assert deleted["path"] == saved["path"]
    assert not read.page_exists(stored, saved["path"])


def test_a_read_by_reference_beside_a_backlog_past_the_cap_refuses_retryably(stored, service):
    """A page read by its reference while more pages wait to derive than one check reads: the owner
    of the id could be any waiting page, so the read refuses, retryably, instead of parsing them all.
    Once the backlog is derived the same read returns the page."""
    saved = call(stored, "remember", **QUOKKA, response_detail="full")["diagnostics"]["leaf_result"]
    derived(stored)
    with backlog_past_the_cap(stored):
        with pytest.raises(OpError) as refused:
            call(stored, "read_memory", path=saved["ref"])
    page = call(stored, "read_memory", path=saved["ref"])

    assert refused.value.code == read.DERIVED_BEHIND
    assert "Retry in a few seconds" in refused.value.remediation
    assert page["path"] == saved["path"]


def test_a_read_with_links_beside_a_backlog_past_the_cap_returns_the_page_without_inbound_links(
    stored, service
):
    """A page read by its path with its links while more pages wait to derive than one link check
    reads: the inbound links are unknown, so they come back null, as for a cold index, and the page
    that was found is not withheld for them. The same read after the drain lists the links."""
    saved = call(stored, "remember", **QUOKKA)
    derived(stored)
    with backlog_past_the_cap(stored):
        page = call(stored, "read_memory", path=saved["path"], links=True)
    drained = call(stored, "read_memory", path=saved["path"], links=True)

    assert page["path"] == saved["path"] and "quokka population" in page["body"]
    assert page["links"]["inbound"] is None
    assert page["links"]["outbound"] == [ALPHA.removesuffix(".md")]
    assert drained["links"]["inbound"] is not None


def test_a_recall_that_outwaits_its_bound_says_the_rows_lag(stored, service, monkeypatch):
    """A save whose rows the store thread derives only after the next recall's wait ran out. The
    recall cannot rank the page yet, and without the notice that a search carries it missed the
    page in silence."""
    monkeypatch.setattr(derivation, "READ_WAIT_SECONDS", 0.2)
    with derivation_held(monkeypatch):
        call(stored, "remember", **QUOKKA)
        packet = call(stored, "activate_context", turn="How did the quokka population change?")

    assert "not yet in the search" in packet["notice"]
    assert packet["lag"]["derived_pending"] >= 1


def test_a_recall_during_the_lag_answers_whatever_its_refs_name(stored, service, monkeypatch):
    """A recall whose conversation cites the short id of a page saved a moment ago, and a ref that
    names no page, while the save's rows lag. A refusal for the lag inside the shared name lookup
    made one such ref fail the whole recall."""
    monkeypatch.setattr(derivation, "READ_WAIT_SECONDS", 0.2)
    with derivation_held(monkeypatch):
        saved = call(stored, "remember", **QUOKKA)
        short = call(stored, "read_memory", path=saved["path"])["id"]
        cited = call(
            stored, "activate_context", turn="Summarise the census.", conversation={"refs": [short]}
        )
        unnamed = call(
            stored, "activate_context", turn="Summarise the census.",
            conversation={"refs": ["A page that nobody wrote"]},
        )

    assert "not yet in the search" in cited["notice"]
    assert "not yet in the search" in unnamed["notice"]


def test_a_read_during_the_lag_gives_the_pages_own_id_and_that_id_names_it(
    stored, service, monkeypatch
):
    """A save, a read of its page while its rows lag, and reads by the id that the read gave,
    before and after the rows arrive. The compact save reply carries no id, so a read is how an
    agent learns it. Taken from the rows, the read's id was one that named nothing once they
    arrived, and the id lookup missed a page that only the store holds."""
    with derivation_held(monkeypatch) as release:
        saved = call(stored, "remember", **QUOKKA)
        read_back = call(stored, "read_memory", path=saved["path"])
        before = call(stored, "read_memory", path=read_back["id"])
        release.set()
    derived(stored)
    after = call(stored, "read_memory", path=read_back["id"])

    assert before["path"] == after["path"] == saved["path"]
    assert read_back["ref"] == after["ref"]


def test_a_write_and_a_drain_never_wait_for_rows(stored, service, monkeypatch):
    """Two saves and a move while the rows of earlier saves are pending, then the drain of them all.
    A write that waited for those rows would add up to the wait bound to every save in a burst, and
    a drain that waited would wait for the rows it derives itself."""
    waits = []
    real_caught_up = derivation.caught_up

    def caught_up(still_held_back, seconds):
        waits.append(threading.current_thread().name)
        return real_caught_up(still_held_back, seconds)

    monkeypatch.setattr(derivation, "caught_up", caught_up)
    with monkeypatch.context() as patch:
        patch.setattr(derivation, "drain", lambda *_arguments, **_keywords: None)
        call(stored, "remember", **QUOKKA)
        call(stored, "remember", **{**QUOKKA, "title": "Second census"})
        call(stored, "manage_memory_file", operation="move", old_path=ALPHA,
             new_path=f"{NOTES}/Archive/Alpha.md")
        assert store_rows(stored, "SELECT 1 FROM store_derivation_queue LIMIT 1"), (
            "the rows must be pending for this check to mean anything"
        )
    derived(stored)

    assert waits == []


def test_a_reader_holds_no_store_connection_while_it_waits_for_the_rows(
    stored, service, monkeypatch
):
    """A search that waits for a save's rows. Waiting inside its snapshot, it would hold a store
    connection, and the WAL that the store thread writes behind it, for as long as it waits."""
    open_readers: dict[int, int] = {}
    real_reader = connection.store_reader

    @contextmanager
    def store_reader(*arguments, **keywords):
        thread = threading.get_ident()
        with real_reader(*arguments, **keywords) as reader:
            open_readers[thread] = open_readers.get(thread, 0) + 1
            try:
                yield reader
            finally:
                open_readers[thread] -= 1

    held_while_waiting = []
    real_caught_up = derivation.caught_up

    def caught_up(still_held_back, seconds):
        def look():
            held_while_waiting.append(open_readers.get(threading.get_ident(), 0))
            return still_held_back()

        return real_caught_up(look, seconds)

    monkeypatch.setattr(connection, "store_reader", store_reader)
    monkeypatch.setattr(derivation, "caught_up", caught_up)
    with derivation_held(monkeypatch) as release:
        call(stored, "remember", **QUOKKA)
        threading.Timer(0.3, release.set).start()
        call(stored, "ask_memory", query="quokka population")

    assert held_while_waiting, "the search must wait for this check to mean anything"
    assert set(held_while_waiting) == {0}, held_while_waiting


def test_a_save_replies_before_the_store_thread_derives_its_rows(stored, service, monkeypatch):
    """Two saves, the second with rows that take seconds to derive. Its reply came only after its
    own derivation, so every save waited out the cost of its search and graph rows."""
    delay = [0.0]
    real = derive.derive_paths

    def derive_paths(*arguments, **keywords):
        time.sleep(delay[0])
        return real(*arguments, **keywords)

    monkeypatch.setattr(derive, "derive_paths", derive_paths)

    def reply_seconds(title):
        began = time.monotonic()
        call(stored, "remember", **{**QUOKKA, "title": title})
        return time.monotonic() - began

    reply_seconds("Warm census")  # the first save after a start pays for warm caches
    derived(stored)
    quick = reply_seconds("Quick census")
    derived(stored)
    delay[0] = SLOW_DERIVATION_SECONDS
    slow = reply_seconds("Slow census")
    print(json.dumps({"quick_reply_s": round(quick, 3), "slow_derivation_reply_s": round(slow, 3)}))

    assert (slow - quick) * 1000 < HOLD_GROWTH_LIMIT_MS, (
        f"the save replied in {slow:.2f} s with a {SLOW_DERIVATION_SECONDS:.0f} s derivation, "
        f"against {quick:.2f} s with an instant one"
    )


#: One derivation batch this slow, one page per batch, makes a dozen pages cost several slices.
BATCH_SECONDS = 0.5
BACKLOG_PAGES = 12


@contextmanager
def slow_batches(monkeypatch):
    """Each derivation batch takes ``BATCH_SECONDS`` and holds one page; the yielded list's first
    item sets the seconds, so a test ends its backlog fast by setting it to zero."""
    seconds = [BATCH_SECONDS]
    real = derive.derive_paths

    def derive_paths(*arguments, **keywords):
        time.sleep(seconds[0])
        return real(*arguments, **keywords)

    monkeypatch.setattr(derivation, "BATCH_PAGES", 1)
    monkeypatch.setattr(derive, "derive_paths", derive_paths)
    yield seconds


def backlog_page(number):
    return f"{NOTES}/Backlog {number}.md", f"# Backlog {number}\n\nThe ferry calls at dusk.\n".encode()


def commit_backlog(root, monkeypatch, pages, first=0):
    """``pages`` one-page commits that nothing derives, as a crash leaves them."""
    with monkeypatch.context() as patch:
        patch.setattr(derivation, "drain", lambda *_arguments, **_keywords: derivation.Drained())
        patch.setattr(derivation, "owe", lambda *_arguments, **_keywords: None)
        for number in range(first, first + pages):
            rel, body = backlog_page(number)
            commit(root, puts=(write.PagePut(rel, body),))


def test_requests_queued_together_behind_a_derivation_backlog_are_all_served_before_the_backlog_drains(
    stored, monkeypatch
):
    """A start that finds forty intents due, each batch slow, and eight requests that reach the store
    thread together while it derives them. Derived whole before the thread took anything else, the
    backlog outlasted the mutation timeout and every request was refused as busy; with one request
    served per slice, the sixth and later waited out the timeout."""
    with store_thread(stored):
        server = runtime._server(stored.resolve())
        derived(stored)
        commit_backlog(stored, monkeypatch, 40)
        with slow_batches(monkeypatch) as seconds:
            derivation.owe(stored.resolve())
            server.call(lambda: None)  # wakes the idle thread, which then derives what is owed
            time.sleep(BATCH_SECONDS / 2)  # the thread is inside its first batch
            waits = {}

            def send(number):
                began = time.monotonic()
                try:
                    server.call(lambda: None, read_only=True)
                    waits[number] = ("served", time.monotonic() - began)
                except OpError as refusal:
                    waits[number] = (refusal.code, time.monotonic() - began)

            senders = [threading.Thread(target=send, args=(number,)) for number in range(8)]
            for sender in senders:
                sender.start()
            for sender in senders:
                sender.join()
            left = len(store_rows(stored, "SELECT path FROM store_derivation_queue"))
            seconds[0] = 0.0
            derived(stored)
        undrained = store_rows(stored, "SELECT path FROM store_derivation_queue")

    print(json.dumps({"outcomes": sorted((round(wait, 2), outcome) for outcome, wait in waits.values()),
                      "intents_left_when_all_served": left}))
    assert {outcome for outcome, _wait in waits.values()} == {"served"}, waits
    assert max(wait for _outcome, wait in waits.values()) < server.manager._mutation_timeout_seconds / 2
    assert left > 0, "the requests must be served while the backlog stands for this check to mean anything"
    assert undrained == [], "the thread derived the whole backlog after the requests"


def test_derivation_finishes_beneath_a_steady_stream_of_requests(stored, monkeypatch):
    """Four clients that each send the next request as the last one returns, so a request is always
    waiting, over a backlog of eight slow batches. Serving every queued request before any slice
    would never derive; the thread must still finish the backlog, and serve every request."""
    with store_thread(stored):
        server = runtime._server(stored.resolve())
        derived(stored)
        commit_backlog(stored, monkeypatch, 8)
        stop, refusals = threading.Event(), []

        def stream():
            while not stop.is_set():
                try:
                    server.call(lambda: time.sleep(0.05), read_only=True)
                except OpError as refusal:
                    refusals.append(refusal.code)

        clients = [threading.Thread(target=stream, daemon=True) for _ in range(4)]
        with slow_batches(monkeypatch):
            began = time.monotonic()
            derivation.owe(stored.resolve())
            for client in clients:
                client.start()
            try:
                while store_rows(stored, "SELECT path FROM store_derivation_queue"):
                    assert time.monotonic() - began < 60, "the backlog finished beneath the stream"
                    time.sleep(0.05)
                drained_after = time.monotonic() - began
            finally:
                stop.set()
                for client in clients:
                    client.join(30)

    print(json.dumps({"backlog_s_of_work": 8 * BATCH_SECONDS, "drained_after_s": round(drained_after, 2)}))
    assert refusals == []


def test_a_request_that_commits_many_pages_acknowledges_after_one_slice_of_their_rows(
    stored, service, monkeypatch
):
    """A request that commits a dozen pages and derives its own rows, each batch slow. It spent its
    whole own-derivation budget on them before it acknowledged; it now acknowledges after one slice
    with the rest pending, and the store thread derives that rest."""
    derived(stored)
    pages = [backlog_page(number) for number in range(BACKLOG_PAGES)]

    def commit_pages(writer):
        with derivation.own_rows():
            for rel, body in pages:
                write.commit(
                    writer, write.StoreChange(write.Origin.GOVERNED, "served test", puts=(write.PagePut(rel, body),))
                )

    with slow_batches(monkeypatch) as seconds:
        began = time.monotonic()
        projector.store_thread_runner(stored)(commit_pages)
        acknowledged = time.monotonic() - began
        pending = len(store_rows(stored, "SELECT path FROM store_derivation_queue"))
        seconds[0] = 0.0
        derived(stored)

    print(json.dumps({"acknowledged_after_s": round(acknowledged, 3), "pages_pending_at_ack": pending}))
    assert acknowledged < derivation.SLICE_SECONDS + 2 * BATCH_SECONDS + 1.0, acknowledged
    assert 0 < pending < BACKLOG_PAGES, "the request must leave some rows pending, and derive some"
    assert {rel for rel, _body in pages} <= set(bm25_paths(stored, "ferry")), "the thread derived the rest"


def test_a_save_is_derived_before_the_next_queued_request_when_its_slice_was_small(stored, service):
    """Two clients whose slow requests queue behind a save, one slice of nothing having just ended.
    The thread served both before it derived the save, whatever its slice had cost, so a read that
    began after the save waited out its whole bound for rows that a few milliseconds of work give."""
    server = runtime._server(stored.resolve())
    running, release = threading.Event(), threading.Event()

    def occupy():
        derivation.owe(stored.resolve())  # a turn of derivation, empty, runs as the thread is freed
        running.set()
        release.wait(30)

    def queued():
        return len(server._requests)

    threads = [threading.Thread(target=server.call, args=(occupy,))]
    threads[0].start()
    assert running.wait(30)
    threads.append(threading.Thread(
        target=call, args=(stored, "remember"), kwargs={**QUOKKA, "title": "Late census"}
    ))
    threads[1].start()
    while queued() < 1:
        time.sleep(0.01)
    for _client in range(2):
        threads.append(threading.Thread(target=server.call, args=(lambda: time.sleep(0.6),),
                                        kwargs={"read_only": True}))
        threads[-1].start()
    while queued() < 3:
        time.sleep(0.01)
    release.set()
    threads[1].join(30)

    began = time.monotonic()
    with read.request_snapshot(stored, catch_up=True):
        held_back = read._REQUEST.get().held_back
    waited = time.monotonic() - began
    for thread in threads:
        thread.join(30)

    print(json.dumps({"read_waited_s": round(waited, 3), "held_back": held_back}))
    assert not held_back
    assert waited < derivation.READ_WAIT_SECONDS / 2, waited


def test_a_governed_trash_and_its_recovery_settle_exactly_behind_a_derivation_backlog(
    stored, service, monkeypatch
):
    """A governed page trashed through the served door, then recovered, each time with three seconds
    of earlier derivation due ahead of its own rows. The request derived the due rows in order for
    one slice, never reached its own, and settled not exact for good: the trash left its tombstone
    pending and the recovery left the page hidden, until a reconcile."""
    govern(stored, BETA)
    derived(stored)
    with slow_batches(monkeypatch) as seconds:
        commit_backlog(stored, monkeypatch, 6)
        trash = call(stored, "manage_memory_file", operation="delete", path=BETA, confirm=True,
                     force_orphan=True)
        trash_left = len(store_rows(stored, "SELECT path FROM store_derivation_queue"))
        trash_states = [marker["state"] for marker in tombstones(stored)]
        [trashed] = tombstones(stored)[0]["manifest"]
        seconds[0] = 0.0
        derived(stored)
        seconds[0] = BATCH_SECONDS
        commit_backlog(stored, monkeypatch, 6, first=100)
        recovery = call(stored, "manage_memory_file", operation="recover",
                        trash_path=trashed["trash_path"])
        recovery_left = len(store_rows(stored, "SELECT path FROM store_derivation_queue"))
        seconds[0] = 0.0
        derived(stored)

    print(json.dumps({"backlog_left_after_trash": trash_left, "backlog_left_after_recovery": recovery_left}))
    assert trash_left > 0 and recovery_left > 0, "each must settle while its backlog stands"
    assert lifecycle.TRASH_NOT_EXACT not in trash.get("warnings", ())
    assert lifecycle.RECOVERY_NOT_EXACT not in recovery.get("warnings", ())
    assert trash_states == ["committed"]
    assert "pending" not in [marker["state"] for marker in tombstones(stored)]
    assert call(stored, "read_memory", path=BETA)["path"] == BETA


def test_a_drain_that_keeps_failing_is_retried_no_sooner_than_its_retry_delay(
    stored, monkeypatch, caplog
):
    """A store whose derivation step raises each time, as a diverged store refuses it: the store
    thread retries once per retry delay and warns once. Kept at its first owed time, the drain ran
    again at once, hundreds of times a second, with a traceback each time."""
    monkeypatch.setattr(runtime, "DERIVED_RETRY_SECONDS", 0.5)
    attempts = []

    def failing(*_arguments, **_keywords):
        attempts.append(time.monotonic())
        raise RuntimeError("the derivation step failed")

    caplog.set_level(logging.DEBUG, logger=runtime.logger.name)
    with store_thread(stored):
        with monkeypatch.context() as patch:
            patch.setattr(derivation, "drain", failing)
            derivation.owe(stored.resolve())
            deadline = time.monotonic() + 30
            while len(attempts) < 3:
                assert time.monotonic() < deadline, "the store thread retried the failed drain"
                time.sleep(0.05)
    warned = [
        record for record in caplog.records
        if record.levelno >= logging.WARNING and "the vault store could not advance" in record.getMessage()
    ]

    gaps = [later - earlier for earlier, later in zip(attempts[:2], attempts[1:3], strict=True)]
    assert min(gaps) >= 0.45, gaps
    assert len(warned) == 1


def test_a_clean_stop_derives_the_rows_that_committed_pages_still_owe(stored, monkeypatch):
    """A clean stop with committed pages whose rows neither their own drain nor the store thread
    derived: the stop derives them within its bound, so none is due at the next start. The store
    thread's own stop drain ran after the lease's handoff began, which refused it."""
    created = [f"{NOTES}/Owed {number}.md" for number in range(3)]
    with store_thread(stored):
        # The thread first drains what the vault's earlier commits owe; it must be idle before these.
        deadline = time.monotonic() + 30
        while derivation.owed_in(stored.resolve()) is not None:
            assert time.monotonic() < deadline, "the store thread drained what its start owed"
            time.sleep(0.05)
        with monkeypatch.context() as patch:
            patch.setattr(derivation, "drain", lambda *_arguments, **_keywords: derivation.Drained())
            patch.setattr(derivation, "owe", lambda *_arguments, **_keywords: None)
            commit(stored, puts=tuple(write.PagePut(rel, f"# {rel}\n".encode()) for rel in created))
        assert sorted(store_rows(stored, "SELECT path FROM store_derivation_queue")) == [
            (rel,) for rel in sorted(created)
        ]

    assert store_rows(stored, "SELECT path FROM store_derivation_queue") == []


def test_a_stored_creation_receipt_over_the_size_limit_is_refused_as_file_mode_refuses_it(
    stored, service
):
    """A store-mode loader that parses a stored review receipt of any size, where file mode refuses
    one over the artifact limit: governance then judges the same receipt differently by mode."""
    reviews = "Knowledge Base/_Schema/relation-reviews/"
    listed = "SELECT path FROM store_pages WHERE path LIKE ?"
    before = set(store_rows(stored, listed, reviews + "%"))
    call(
        stored, "remember", title="A long receipt",
        content="# A long receipt\n\n## Claim\n\nA note whose receipt grows past its limit.\n",
        relation_disposition="reviewed_none", relation_review_reason="a note that stands alone",
    )
    after = set(store_rows(stored, listed, reviews + "%"))
    ((receipt,),) = after - before
    body = read.owned_bytes(stored, receipt)
    # Trailing spaces keep the JSON valid, so only the size can refuse it.
    oversized = body.ljust(relation_review._MAX_ARTIFACT_BYTES + 1)
    commit(stored, puts=(write.PagePut(receipt, oversized),))

    with request_scope(owner_principal(surface="mcp")), pytest.raises(
        relation_review.RelationReviewError
    ) as refused:
        relation_review.load_creation_receipt(stored, Path(receipt).stem)

    assert refused.value.code == "RELATION_REVIEW_TOO_LARGE"


def test_a_projector_pass_records_a_governed_writes_files_in_a_served_process(stored, service):
    """A served projector whose manifest records the store refuses: it writes or renames the file
    and records nothing, so every pass redoes it and the importer cannot tell its bytes apart."""
    created = f"{NOTES}/Served.md"
    moved = f"{NOTES}/Beta Served.md"
    call(stored, "manage_memory_file", operation="create", path=created, content="Served text.\n")
    call(stored, "manage_memory_file", operation="move", old_path=BETA, new_path=moved)

    report = projector.Projector(stored).run_pass()

    assert not report.busy and report.deferred == {}
    for rel in (created, moved):
        body = read.owned_bytes(stored, rel)
        assert (stored / rel).read_bytes() == body
        assert [row[0] for row in manifest(stored, rel)] == [sha(body)]
    assert not (stored / BETA).exists() and manifest(stored, BETA) == []
    assert queued(stored) & {created, moved, BETA} == set()


def test_the_importer_settles_outside_edits_renames_and_media_in_a_served_process(
    stored, service
):
    """A served importer whose commits the store refuses: an outside edit of a page and a rename
    of a binary stay unimported, an outside picture gains no sidecar page, and the sweep fails on
    every pass."""
    edited = b"---\ntitle: Alpha\ntype: note\n---\n# Alpha\n\nA person edited this line.\n"
    renamed = f"{NOTES}/diagram-renamed.bin"
    (stored / ALPHA).write_bytes(edited)
    (stored / DIAGRAM).rename(stored / renamed)
    (stored / PICTURE).write_bytes(PICTURE_BYTES)

    report = importer.Importer(stored).import_paths(
        [ALPHA, DIAGRAM, renamed, PICTURE], moves={renamed: DIAGRAM}
    )

    assert report.unjudged == {} and report.held == []
    assert sorted(report.imported) == sorted([ALPHA, PICTURE])
    assert report.moved == {renamed: DIAGRAM}
    assert report.media_derived == [PICTURE] and report.media_failed == {}
    assert sha(PICTURE_BYTES).encode() in read.owned_bytes(stored, f"{PICTURE}.md")
    assert read.owned_bytes(stored, ALPHA) == edited
    assert store_rows(stored, "SELECT sha256 FROM store_binaries WHERE path = ?", renamed) == [
        (sha(DIAGRAM_BYTES),)
    ]
    assert store_rows(stored, "SELECT 1 FROM store_binaries WHERE path = ?", DIAGRAM) == []


def test_a_hop_from_a_thread_that_holds_the_boundary_fails_at_once_by_name(stored):
    """A caller that hops while it holds the vault boundary: the store thread would wait for it,
    and it for the store thread, until the mutation timeout."""
    began = time.monotonic()
    with get_manager().mutation_guard(stored, operation="a_holder", holder_kind="background"):
        with pytest.raises(projector.HopUnderBoundary):
            projector.store_thread_runner(stored)(lambda _writer: None)
    assert time.monotonic() - began < 5.0


def test_a_busy_boundary_leaves_bookkeeping_for_the_next_pass_and_logs_no_error(
    stored, service, caplog
):
    """A served projector or importer that fails its pass, logs an error, or loses the intent when
    another holder keeps the vault boundary."""
    created = f"{NOTES}/Waiting.md"
    call(stored, "manage_memory_file", operation="create", path=created, content="Waiting.\n")
    edited = b"---\ntitle: Alpha\ntype: note\n---\n# Alpha\n\nEdited while the boundary was held.\n"
    caplog.set_level("WARNING")

    with held_elsewhere(stored):
        (stored / ALPHA).write_bytes(edited)
        passed = projector.Projector(stored).run_pass()
        imported = importer.Importer(stored).import_paths([ALPHA])

    assert passed.busy and passed.settled == 0 and passed.deferred == {}
    assert created in queued(stored), "the intent stays queued"
    assert imported.unjudged == {ALPHA: importer.BOUNDARY_BUSY} and imported.imported == []
    assert [r for r in caplog.records if r.name.startswith("exomem.vault_store")] == []

    assert projector.Projector(stored).run_pass().projected >= 1
    assert importer.Importer(stored).import_paths([ALPHA]).imported == [ALPHA]
    assert created not in queued(stored) and read.owned_bytes(stored, ALPHA) == edited


def test_the_media_writers_commit_in_a_served_process_beside_a_registry_tick(
    stored, service, monkeypatch
):
    """A media writer that a served process refuses, or whose hop waits out the mutation timeout
    behind a registry tick while another thread holds the boundary (tasks.md 2.5)."""
    # A holder takes the boundary between the tick's start and its batch. ``pending`` opens that
    # gap. The batch takes no boundary, so the writer waits only for the holder.
    started, go = threading.Event(), threading.Event()
    pending = rederive.pending

    def gapped(*arguments):
        if not go.is_set():
            started.set()
            assert go.wait(30), "the test opened the gap"
        return pending(*arguments)

    monkeypatch.setattr(rederive, "pending", gapped)
    video = stored / VIDEO
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"\x00video")
    parts = [extraction_parts.part_path(Path(RAW_SOURCE), n).as_posix() for n in (2, 3)]
    source = "---\ntitle: Minutes\ntype: source\nextraction_parts: 2\n---\n# Minutes\n"
    pages = {RAW_SOURCE: source, **{rel: f"---\npart: {rel[-5]}\n---\n# Part\n" for rel in parts}}
    go.set()
    commit(stored, puts=tuple(write.PagePut(rel, body.encode()) for rel, body in pages.items()))
    hold_seconds = 1.0
    stalls: dict[str, float] = {}

    def behind_a_parked_tick(name, round_number, writer_call):
        """An overlay change on disk makes the registry tick write. Another thread takes the
        boundary just before that write and keeps it ``hold_seconds`` once the tick waits for it.
        The writer hops meanwhile."""
        commit(stored, puts=(write.PagePut(f"{SCHEMA}/served-{round_number}.md", b"# Served\n"),))
        projector.Projector(stored).run_pass()
        started.clear()
        go.clear()
        assert started.wait(30), "a registry tick started"
        with held_elsewhere(stored) as release:
            go.set()
            threading.Timer(hold_seconds, release.set).start()
            began = time.monotonic()
            outcome = writer_call()
            stalls[name] = round(time.monotonic() - began, 3)
        return outcome

    frames = [(_scene(10.0), _FakeImg())]
    pairs = behind_a_parked_tick(
        "write", 1, lambda: scene_frames.write_scene_frames(stored, video, frames)
    )
    assert len(pairs) == 1 and frame_rows(stored) != ([], [])
    removed = behind_a_parked_tick(
        "clear", 2, lambda: scene_frames.clear_scene_frames(stored, video)
    )
    kept = behind_a_parked_tick(
        "retire", 3, lambda: extraction_parts.retire(stored, (stored / parts[1],))
    )

    waits = {"ended_by_a_request": len(service.ended), "returned": len(service.returned)}
    print(json.dumps({"hold_seconds": hold_seconds, "stalls": stalls, "tick_waits": waits}))
    assert removed == 2 and frame_rows(stored) == ([], [])
    assert kept == ()
    assert not read.page_exists(stored, parts[1]) and read.page_exists(stored, parts[0])
    # Each waited for the other holder's release and one poll, not for the mutation timeout. The
    # bound is halfway between the two, so a loaded host does not fail it.
    waited_out = get_manager()._mutation_timeout_seconds
    assert max(stalls.values()) < hold_seconds + (waited_out - hold_seconds) / 2
    deadline = time.monotonic() + 30
    while store_mode.status(stored)["registry"] != rederive.CURRENT:
        assert time.monotonic() < deadline, "the tick that stepped aside runs once it is free"
        time.sleep(0.05)


def test_a_media_job_result_commits_through_the_served_door(stored, service, monkeypatch):
    """A media worker whose result a served process refuses, so the extracted text never reaches
    the sidecar page and the result stays pending (tasks.md 6.4)."""
    (stored / PICTURE).write_bytes(PICTURE_BYTES)
    importer.Importer(stored).import_paths([PICTURE])
    jobs = media_jobs.MediaJobStore(stored)
    job = jobs.claim_next()
    assert job is not None and job.binary_path == stored / PICTURE
    words = "Words read from the picture."
    monkeypatch.setattr(
        extract,
        "extract_text",
        lambda *_args, **_kwargs: extract.ExtractResult(
            text=words, media_type="image", engine="test"
        ),
    )
    worker = media_worker.MediaWorker(stored, execution_mode="process")
    worker._process(job)
    assert jobs.pending_result_count() == 1

    worker._drain_parent_results()

    assert jobs.pending_result_count() == 0
    assert words.encode() in read.owned_bytes(stored, f"{PICTURE}.md")


def test_a_served_start_gives_a_media_file_dropped_on_disk_its_companion_page(
    stored, service, monkeypatch, caplog
):
    """A served start whose media pass hops from under its own boundary
    (``HopUnderBoundary``), so a media file that arrived on disk with no companion page gets
    none, and its content stays out of search and recall."""
    dropped = f"{NOTES}/dropped.png"
    (stored / dropped).write_bytes(PICTURE_BYTES)
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    caplog.set_level("WARNING")

    assert server_runtime._start_media_worker(stored) is None

    assert sha(PICTURE_BYTES).encode() in read.owned_bytes(stored, f"{dropped}.md")
    assert [r for r in caplog.records if "HopUnderBoundary" in r.getMessage()] == []
    assert [r for r in caplog.records if "media reconciliation failed" in r.getMessage()] == []


def test_a_served_start_runs_no_whole_vault_graph_rebuild_over_file_modes_graph_debt(
    stored, service, monkeypatch, caplog
):
    """A served start whose graph drain pays file mode's graph debt with whole-vault rebuilds of
    the sidecar. No store-mode reader opens the sidecar (D9). When such a rebuild could not
    publish, it left its marker and retried for the life of the process."""
    monkeypatch.setenv("EXOMEM_DISABLE_WARMUP", "1")
    caplog.set_level(logging.INFO)
    # File mode's debt: graph repair queued in the deferred index, as a file-mode write leaves it
    # when migration has not drained it, and the barrier that a stopped rebuild leaves on the sidecar.
    deferred_index.add_graph(stored, [ALPHA])
    assert deferred_index.snapshot_graph(stored, limit=8)
    epistemic_graph.EpistemicGraphIndex(stored).withdraw_availability()
    activation = server_runtime.LocalRuntimeActivation(stored, fallback_seconds=600.0)

    async def serve():
        async with activation.lifespan()(SimpleNamespace()):
            activation.start()
            await asyncio.to_thread(activation._thread.join, 120)
            # Long enough for a drain's first whole-vault pass: its debounce, then its quiet window.
            await asyncio.sleep(graph_drain.DEBOUNCE_SECONDS + graph_drain.WHOLE_VAULT_SETTLE_SECONDS + 4)

    asyncio.run(serve())

    assert [r.getMessage() for r in caplog.records if "graph rebuild" in r.getMessage()] == []
    assert deferred_index.graph_full_rebuild_pending(stored) is None
    assert epistemic_graph.EpistemicGraphIndex(stored).available()


def test_store_mode_restart_admits_retrieval_without_a_file_catalogue(stored, monkeypatch):
    """The canonical store can serve even if its former Markdown catalogue never warms."""
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "auto")

    def file_catalogue_is_stale(*_args, **_kwargs):
        pytest.fail("store-mode startup tried to rebuild the file catalogue")

    monkeypatch.setattr(freshness, "rebaseline", file_catalogue_is_stale)
    monkeypatch.setattr(lexstore, "request_lag_repair", file_catalogue_is_stale)
    readiness.reset()
    readiness.manage_runtime()
    readiness.begin_warm()
    try:
        assert warmup.warm_retrieval_catalog(stored)
        assert readiness.retrieval_admission(stored) == {"state": "ready", "admitted": True}
        assert service_standby.prove_retrieval_catalog(stored)
    finally:
        readiness.reset()


def test_store_mode_startup_retries_a_temporary_read_failure(stored, monkeypatch):
    """A quiet worker can recover without a health probe or the file event indexes."""
    monkeypatch.setenv("EXOMEM_DISABLE_EVENT_INDEXES", "1")
    monkeypatch.setattr(server_runtime, "CATALOGUE_RECOVERY_FIRST_BACKOFF_SECONDS", 0.01)
    snapshot = read.snapshot
    failed = False

    def first_read_fails(root, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("store read temporarily unavailable")
        return snapshot(root, **kwargs)

    monkeypatch.setattr(read, "snapshot", first_read_fails)
    activation = server_runtime.LocalRuntimeActivation(stored)
    readiness.reset()
    readiness.manage_runtime()
    readiness.begin_warm()
    try:
        with pytest.raises(OSError, match="temporarily unavailable"):
            warmup.warm_retrieval_catalog(stored)
        readiness.finish_warm()
        activation._start_catalogue_recovery(stored)
        assert readiness.wait("retrieval_catalog", timeout=3)
        assert readiness.retrieval_admission(stored)["admitted"]
    finally:
        activation._catalogue_stop.set()
        if activation.catalogue_recovery is not None:
            activation.catalogue_recovery.join(timeout=3)
        readiness.reset()


def test_a_save_after_a_restart_is_not_held_behind_a_graph_snapshot_proof(
    stored, service, monkeypatch
):
    """A restart whose graph snapshot proof takes minutes, as it took 100 s over 17,000 pages.
    Store mode keeps its graph rows in the store, yet every save was refused as warming until the
    proof of file mode's sidecar ended."""
    monkeypatch.delenv("EXOMEM_DISABLE_WARMUP", raising=False)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    release = threading.Event()
    real = epistemic_graph.EpistemicGraphIndex.adopt_published_snapshot

    def slow_proof(self, *arguments, **keywords):
        release.wait(120)
        return real(self, *arguments, **keywords)

    monkeypatch.setattr(epistemic_graph.EpistemicGraphIndex, "adopt_published_snapshot", slow_proof)
    readiness.reset()
    readiness.begin_warm()
    warm = threading.Thread(target=warmup.warm_all, args=(stored,), daemon=True)
    warm.start()
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                call(stored, "remember", **QUOKKA)
                break
            except Exception as error:  # noqa: BLE001 - the dispatcher's error type varies
                if getattr(error, "code", None) != "MUTATION_WARMING" or time.monotonic() > deadline:
                    raise
                assert error.details.get("warming_component") != "graph_handoff", str(error)
                time.sleep(0.25)
    finally:
        release.set()
        warm.join(120)
        readiness.reset()


def test_a_store_mode_recall_seed_reads_the_store_once_per_scope_not_once_per_page(stored, service):
    """The seed that a store-mode start pays before it admits a write read each stored page from
    the store on its own: 110 s of a restart over 17,000 pages."""
    opened = []
    real = read.snapshot

    @contextmanager
    def counted(root, *arguments, **keywords):
        opened.append(root)
        with real(root, *arguments, **keywords) as view:
            yield view

    pages = len(store_rows(stored, "SELECT path FROM store_pages"))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(read, "snapshot", counted)
        assert all(freshness.rebaseline(stored).values())
    assert len(opened) < pages, f"{len(opened)} store reads for {pages} stored pages"


def test_the_media_workers_own_frame_write_commits_through_the_served_door(stored, service):
    """A worker whose scene-frame unit hops from under its own boundary, or that a served process
    refuses, so a video keeps no viewable frames and its frames get no OCR jobs."""
    video = stored / VIDEO
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"\x00video")
    importer.Importer(stored).import_paths([VIDEO])  # the video gets its companion page
    job = media_jobs.MediaJob(
        binary_path=video, sidecar_path=stored / f"{VIDEO}.md", media_type="video"
    )
    worker = media_worker.MediaWorker(stored, execution_mode="process")

    # No CLIP pass ran, so there are no parent samples to bind, as the direct write takes it.
    worker._persist_scene_frames(job, [(_scene(10.0), _FakeImg())], parent_clip_samples=None)

    pages, records = frame_rows(stored)
    assert len(pages) == 1 and len(records) == 1
    queued_frames = media_jobs.MediaJobStore(stored).get_by_binary(stored / records[0][0])
    assert queued_frames is not None and queued_frames.do_ocr


def test_a_media_store_offload_publishes_its_reference_through_the_served_door(
    stored, service, selected_store
):
    """An offload whose reference unit a served process refuses, or that hops from under its own
    boundary, so the original never records its stored copy and can never be evicted."""
    (stored / PICTURE).write_bytes(PICTURE_BYTES)
    importer.Importer(stored).import_paths([PICTURE])

    reference = _offload(stored, stored / PICTURE)

    assert reference is not None
    assert reference.object.encode() in read.owned_bytes(stored, f"{PICTURE}.md")
    assert media_store.original_state(stored, PICTURE).state == "cached"


def test_a_quarantined_eviction_pointer_waits_for_a_busy_boundary_before_repair(
    stored, service, selected_store, monkeypatch, caplog
):
    """Preserve the pointer and bytes while the served boundary is busy, then publish a verified replacement."""
    (stored / PICTURE).write_bytes(PICTURE_BYTES)
    importer.Importer(stored).import_paths([PICTURE])
    reference = _offload(stored, stored / PICTURE)
    # The stored copy is gone. Eviction leaves its pointer for the repair worker.
    (Path(selected_store[0]["root"]) / reference.object / reference.version).unlink()
    monkeypatch.setattr(shutil, "disk_usage", _volume(10_000, 9_000, stored / PICTURE))
    monkeypatch.setattr(store_media, "BUSY_WAIT_SECONDS", 0.5)
    caplog.set_level("WARNING")

    with held_elsewhere(stored):
        assert media_store.evict_to_target(stored) == 0
        media_worker._evict(stored)
        assert media_replicas.has_quarantined(stored, (reference,))
        assert media_replicas.drain(stored) == 0

    assert reference.object.encode() in read.owned_bytes(stored, f"{PICTURE}.md")
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    assert media_replicas.drain(stored) == 1

    repaired = media_store.original_state(stored, PICTURE).reference
    assert repaired is not None and repaired != reference
    assert media_store.check_replica(stored, repaired) is None
    assert (stored / PICTURE).read_bytes() == PICTURE_BYTES


def idle_service_projector(root, poll_seconds=60.0):
    """The service's projector loop, started as the service starts it and then idle in its poll."""
    loop = projector.Projector(root, poll_seconds=poll_seconds)
    loop.start()
    deadline = time.monotonic() + 30
    while queued(root):
        assert time.monotonic() < deadline, "the start drained the queue"
        time.sleep(0.05)
    time.sleep(0.5)  # its pass has ended; it now waits out the poll
    return loop


def test_a_clean_stop_projects_what_is_queued_so_the_files_match_the_store(stored, service):
    """A clean service stop that leaves committed pages queued, so their files lag the store and an
    offline rollback refuses until the next start."""
    activation = server_runtime.LocalRuntimeActivation(stored, deferred=True)
    activation.vault_projector = idle_service_projector(stored)
    created = [f"{NOTES}/Stopping {n}.md" for n in range(5)]
    commit(stored, puts=tuple(write.PagePut(rel, f"# {rel}\n".encode()) for rel in created))

    activation._stop_background_workers()

    assert queued(stored) == set()
    assert all((stored / rel).read_bytes() == f"# {rel}\n".encode() for rel in created)


def test_a_stop_drains_no_longer_than_its_bound_and_the_next_start_projects_the_rest(
    stored, service, monkeypatch
):
    """A stop whose drain outlasts its bound and holds up the service's shutdown, or that loses
    the intents it did not reach."""
    monkeypatch.setattr(projector, "DRAIN_ON_STOP_SECONDS", 0.3)
    activation = server_runtime.LocalRuntimeActivation(stored, deferred=True)
    activation.vault_projector = idle_service_projector(stored)
    created = [f"{NOTES}/Bounded {n:03d}.md" for n in range(150)]  # about 12 s of drain
    commit(stored, puts=tuple(write.PagePut(rel, f"# {rel}\n".encode()) for rel in created))

    began = time.monotonic()
    activation._stop_background_workers()
    stopped_in = time.monotonic() - began

    left = queued(stored)
    assert stopped_in < 4.0 and left and left <= set(created)
    projector.Projector(stored).run_pass()  # the next start
    assert queued(stored) == set()


REST_KEY = "replica-rest-key"


@contextmanager
def http_replica(root, tmp_path, name, *, activation_after=None):
    """One installed-shape HTTP service process on ``root``, with its own lease replica id.

    No request here calls the liveness route, so the service activates its background workers,
    the vault projector among them, on its fallback timer: after ``activation_after`` seconds
    when given, else the service's own default.
    """
    import socket
    import subprocess
    import urllib.request

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    env = {
        **os.environ,
        "EXOMEM_REST_API_KEY": REST_KEY,
        "EXOMEM_WRITER_LEASE_REPLICA_ID": name,
        "EXOMEM_WRITER_LEASE_STATE_DIR": str(tmp_path / f"lease-{name}"),
        "EXOMEM_WRITER_LEASE_PREFERRED": "0",
        "EXOMEM_LOG_DIR": str(tmp_path / f"logs-{name}"),
        "EXOMEM_DISABLE_EMBEDDINGS": "1",
        "EXOMEM_DISABLE_MEDIA_EXTRACTION": "1",
        "EXOMEM_DISABLE_FILE_WATCHER": "1",
        "EXOMEM_DISABLE_WARMUP": "1",
    }
    log_path = tmp_path / f"{name}.log"
    script = Path(__file__).resolve().parents[1] / "scripts" / "e2e_http_server.py"
    command = [sys.executable, str(script)]
    if activation_after is not None:
        # This case controls the fallback timer while exercising real startup admission.
        command = [
            sys.executable, "-c", """
import runpy
import sys
from exomem.server_runtime import LocalRuntimeActivation

delay = float(sys.argv.pop(1))
script = sys.argv.pop(1)
LocalRuntimeActivation.__init__.__kwdefaults__["fallback_seconds"] = delay
runpy.run_path(script, run_name="__main__")
""", str(activation_after), str(script),
        ]
    with log_path.open("w", encoding="utf-8") as console:
        process = subprocess.Popen(
            [*command, "--host", "127.0.0.1", "--port", str(port)],
            env=env, stdout=console, stderr=subprocess.STDOUT,
        )
        url = f"http://127.0.0.1:{port}"
        replica = SimpleNamespace(process=process, url=url, log=log_path)

        def post(route, body):
            request = urllib.request.Request(
                f"{url}/api/{route}", data=json.dumps(body).encode(), method="POST",
                headers={"Authorization": f"Bearer {REST_KEY}", "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as answer:
                    return json.loads(answer.read())
            except urllib.error.HTTPError as refused:
                return json.loads(refused.read())

        replica.post = post
        try:
            deadline = time.monotonic() + 120
            while True:
                assert process.poll() is None, log_path.read_text(encoding="utf-8")[-3000:]
                try:
                    urllib.request.urlopen(f"{url}/api/openapi.json", timeout=1).close()
                    break
                except OSError:
                    assert time.monotonic() < deadline, "the replica did not start"
                    time.sleep(0.1)
            yield replica
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(60)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(10)


@pytest.fixture
def sigterm_delivered():
    """Skips a test that stops a process by SIGTERM where none is delivered: on Windows
    ``Popen.terminate()`` is TerminateProcess, which ends the process with no handler and no teardown."""
    if sys.platform == "win32":
        pytest.skip("Windows has no SIGTERM delivery")


def remember(replica, title):
    return replica.post(
        "remember",
        {"content": f"# {title}\n\n## Claim\n\nWriter-lease serialization.\n", "title": title,
         "note_type": "insight", "status": "draft"},
    )


@pytest.mark.timeout(600)
def test_a_service_stopped_before_it_activated_still_projects_the_write_it_accepted(
    sigterm_delivered, stored, tmp_path
):
    """A service that accepts a write before its activation starts the vault projector, then
    stops: a stop that drains only a started projector leaves the write's projections queued, so
    the person's page has no file until a service starts again."""
    with http_replica(stored, tmp_path, "unactivated", activation_after=600) as service:
        written = remember(service, "Accepted before activation")
        assert written["success"] is True, written
        service.process.terminate()
        service.process.wait(60)
    left = store_rows(stored, "SELECT path, revision, queued_at FROM store_projection_queue")
    assert left == [], (left, service.log.read_text(encoding="utf-8")[-6000:])
    page = written["data"]["path"]
    assert (stored / page).read_bytes() == read.owned_bytes(stored, page)
    assert service.process.returncode == -signal.SIGTERM


@pytest.mark.timeout(600)
def test_a_second_replica_that_takes_the_lease_and_stops_at_once_leaves_nothing_queued(
    sigterm_delivered, stored, tmp_path
):
    """Two services on one vault: the first writes and stops, the second takes the lease over with
    its own write and stops right after it. A stop that leaves that write's projections queued
    leaves files behind the store until a service starts again, and an offline rollback refuses."""
    with http_replica(stored, tmp_path, "replica-a") as first:
        with http_replica(stored, tmp_path, "replica-b") as second:
            assert remember(first, "Lease writer A")["success"] is True
            refused = remember(second, "Lease follower B")
            assert refused["error"]["code"] == "WRITER_LEASE_REQUIRED", refused
            first.process.terminate()
            first.process.wait(60)
            deadline = time.monotonic() + 30
            while not (taken := remember(second, "Lease takeover B")).get("success"):
                assert time.monotonic() < deadline, taken
                time.sleep(0.25)
            second.process.terminate()
            second.process.wait(60)
    left = store_rows(stored, "SELECT path, revision, queued_at FROM store_projection_queue")
    assert left == [], (left, second.log.read_text(encoding="utf-8")[-6000:])
    # Each ends by the signal, as a supervisor that stops it expects.
    assert first.process.returncode == second.process.returncode == -signal.SIGTERM


@pytest.mark.timeout(600)
def test_a_follower_writes_no_projection_until_it_takes_the_lease(stored, tmp_path):
    """A replica without the writer lease runs a projector but may not record what it projects.

    The leader here is this process's store thread, which runs no projector, so the follower's
    projector is the only one that could write the leader's new page. A follower that writes the
    file first and is then refused its bookkeeping leaves a file that no record vouches for, and a
    second writer of the projection.
    """
    page = f"{NOTES}/Leader page.md"
    with ExitStack() as leader:
        leader.enter_context(store_thread(stored))
        commit(stored, puts=(write.PagePut(page, b"# Leader page\n\nThe leader holds it.\n"),))
        with http_replica(stored, tmp_path, "follower") as follower:
            # Long enough for several of the follower's passes, each a poll interval apart.
            watched_until = time.monotonic() + 8 * projector.POLL_SECONDS
            while time.monotonic() < watched_until:
                assert not (stored / page).exists(), "the follower projected the leader's page"
                time.sleep(0.1)
            leader.close()
            deadline = time.monotonic() + 60
            while not (own := remember(follower, "Follower note")).get("success"):
                assert time.monotonic() < deadline, own
                time.sleep(0.25)
            projected = (page, own["data"]["path"])
            deadline = time.monotonic() + 60
            # Each file comes to hold what the store holds, once the follower leads.
            while any(
                not (stored / rel).exists()
                or (stored / rel).read_bytes() != read.owned_bytes(stored, rel)
                for rel in projected
            ):
                assert time.monotonic() < deadline, follower.log.read_text(encoding="utf-8")[-4000:]
                time.sleep(0.25)


def idle(service_state):
    """Wait until no process of the replica holds a command claim on its grant, as when a service
    has finished its last request and the background work that request started."""
    from filelock import ReadWriteLock, Timeout

    (claim,) = service_state.glob("writer-grant-*.db")
    deadline = time.monotonic() + 90
    while True:
        lock = ReadWriteLock(claim, is_singleton=False)
        try:
            lock.acquire_write(blocking=False)
            lock.release()
            return
        except Timeout:
            assert time.monotonic() < deadline, "the service never became idle"
            time.sleep(0.25)
        finally:
            lock.close()


@pytest.mark.timeout(600)
def test_an_idle_service_writes_after_a_cli_command_of_its_replica_hands_back_their_grant(
    vault, tmp_path  # noqa: F811
):
    """A service and a one-shot CLI command with one replica id share one grant. The command hands
    it back as it exits while the service is idle: a service that keeps trusting the grant refuses
    its next page or record write with a lease error, or stops serving its collection store."""
    import subprocess

    from test_collection_store_writer import CID

    client = writer_lease.LeaseCoordinatorClient(writer_lease.LeaseConfig.from_env())
    with http_replica(vault, tmp_path, "shared") as service:
        assert remember(service, "Before the command")["success"] is True
        # A command that exits while one of the service's background steps holds a claim keeps the
        # grant, as designed; the case needs an exit that lands while the service is idle.
        for attempt in range(5):
            idle(tmp_path / "lease-shared")
            command = subprocess.run(
                [sys.executable, "-m", "exomem", "remember", "--title", f"From command {attempt}",
                 "--content", "# From the command\n\n## Claim\n\nA one-shot write.\n",
                 "--field", "note_type=insight", "--field", "status=draft", "--json"],
                env={**os.environ, "EXOMEM_WRITER_LEASE_REPLICA_ID": "shared",
                     "EXOMEM_WRITER_LEASE_STATE_DIR": str(tmp_path / "lease-shared"),
                     "EXOMEM_WRITER_LEASE_PREFERRED": "0", "EXOMEM_DISABLE_EMBEDDINGS": "1"},
                capture_output=True, text=True, timeout=300,
            )
            assert command.returncode == 0, command.stdout[-2000:] + command.stderr[-2000:]
            if client.status().holder is None:  # the command handed the shared grant back
                break
        else:
            pytest.fail("no command exited while the service was idle")

        assert remember(service, "After the command")["success"] is True
        appended = service.post("record_memory", {
            "action": "append", "collection": CID, "item": {"title": "After the command"},
            "why": "a record write after the command",
        })
        assert appended["success"] is True, appended
        rows = service.post("record_memory", {"action": "query", "collection": CID})
        assert "After the command" in [row["title"] for row in rows["data"]["rows"]], rows


STARTS_AND_WAITS = """
import asyncio, sys
from contextlib import asynccontextmanager
from pathlib import Path
from exomem.server import ExomemFastMCP

started = Path(sys.argv[1])

@asynccontextmanager
async def lifespan(_server):
    started.write_text("starting")
    while started.exists():  # a startup that awaits, as the schema publication can
        await asyncio.sleep(0.05)
    yield
    started.with_suffix(".torn-down").touch()

ExomemFastMCP("startup", lifespan=lifespan).run(
    transport="http", host="127.0.0.1", port=0, show_banner=False
)
"""


def test_a_sigterm_while_the_http_service_starts_ends_it_by_the_signal(sigterm_delivered, tmp_path):
    """A SIGTERM that lands while the lifespan's startup awaits: met at once, it ends the process
    without the teardown of what the startup began, or, raised as an exception that lands in the
    event loop, with 1, which a supervisor reads as a crash."""
    import subprocess

    started = tmp_path / "started"
    process = subprocess.Popen(
        [sys.executable, "-c", STARTS_AND_WAITS, str(started)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 120
        while not started.exists():
            assert process.poll() is None, process.stdout.read().decode()[-3000:]
            assert time.monotonic() < deadline, "the service did not start its lifespan"
            time.sleep(0.05)
        process.terminate()
        started.unlink()  # the startup finishes
        output, _ = process.communicate(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10)
    torn_down = started.with_suffix(".torn-down").exists()
    assert (torn_down, process.returncode) == (True, -signal.SIGTERM), output.decode()[-3000:]


SERVES_UNDER_AN_EARLIER_ACTION = """
import asyncio, signal, sys, threading
from contextlib import asynccontextmanager
from pathlib import Path

marks, port, action, startup = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4]
starting, raised = marks.with_suffix(".starting"), marks.with_suffix(".raised")
blocked = startup in ("masked", "all blocked")
if blocked:  # the main thread blocks SIGTERM, as an embedding caller can, and so every later thread
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
from exomem.server import ExomemFastMCP

def mark(word):
    with marks.open("a", encoding="utf-8") as out:
        out.write(word + "\\n")

joining = threading.Event()

def raise_on_a_worker():
    if startup == "all blocked":
        joining.wait()
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
    signal.raise_signal(signal.SIGTERM)
    if startup == "all blocked":
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    raised.touch()
    if startup == "masked":
        threading.Event().wait()  # it stays, a thread that takes the process's SIGTERM

def replacing(_signum, _frame):
    mark("handler")
    signal.signal(signal.SIGTERM, signal.SIG_DFL)

@asynccontextmanager
async def lifespan(_server):
    if startup != "free":  # the startup awaits until the test removes the file
        starting.touch()
        if blocked:
            worker = threading.Thread(target=raise_on_a_worker, daemon=True)
            switching = sys.getswitchinterval()
            if startup == "all blocked":
                # No forced thread switch: the worker raises only once the main thread waits in
                # the join, so the main thread meets the signal once every thread blocks it.
                sys.setswitchinterval(1e6)
            worker.start()
            if startup == "all blocked":
                joining.set()
                worker.join()
                sys.setswitchinterval(switching)
        while starting.exists():
            await asyncio.sleep(0.05)
        if blocked:
            if signal.SIGTERM in signal.sigpending():
                mark("pending")
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
    mark("started")
    yield
    mark("torn down")

earlier = {"handler": lambda _signum, _frame: mark("handler"), "replacing": replacing,
           "ignore": signal.SIG_IGN, "default": signal.SIG_DFL}[action]
signal.signal(signal.SIGTERM, earlier)
ExomemFastMCP("earlier", lifespan=lifespan).run(
    transport="http", host="127.0.0.1", port=port, show_banner=False
)
mark("returned")
"""


@pytest.mark.parametrize(
    ("action", "startup", "marks", "status"),
    [
        # The handler runs once and the runner returns, as on main; main ran it before the outer
        # teardown, and here it runs after the teardown.
        ("handler", "free", ["started", "torn down", "handler", "returned"], 0),
        # As on main: the ignored signal is ignored, and the runner returns.
        ("ignore", "free", ["started", "torn down", "returned"], 0),
        # Main ends here without the teardown; this change runs it, then ends by the signal.
        ("default", "free", ["started", "torn down"], -signal.SIGTERM),
        # A SIGTERM while the startup awaits stops the service once the startup has finished: the
        # teardown runs, then the handler, once.
        ("handler", "held", ["started", "torn down", "handler", "returned"], 0),
        # Also when the handler replaces the action as it runs.
        ("replacing", "held", ["started", "torn down", "handler", "returned"], 0),
        # So does a worker's SIGTERM while the main thread blocks it.
        ("handler", "masked", ["started", "torn down", "handler", "returned"], 0),
        ("default", "masked", ["started", "torn down"], -signal.SIGTERM),
        # And one that a worker raised before every thread blocked it.
        ("default", "all blocked", ["started", "torn down"], -signal.SIGTERM),
    ],
)
def test_a_sigterm_meets_the_action_installed_before_the_http_service_ran(
    sigterm_delivered, tmp_path, action, startup, marks, status
):
    """A service that stops on SIGTERM without its teardown, or whose stop drops the action that
    its caller installed: the handler never runs or runs before the teardown, or an ignored signal
    ends the process. A SIGTERM while the startup awaits is the same stop once the startup has
    finished, also when a worker raised it while the main thread or every thread blocks it. Met at
    once, it skips the teardown, waits pending, or leaves a service that serves on and whose
    handler may have replaced the action that the stop meets."""
    import socket
    import subprocess

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    written = tmp_path / "marks"
    process = subprocess.Popen(
        [sys.executable, "-c", SERVES_UNDER_AN_EARLIER_ACTION, str(written), str(port), action,
         startup],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )

    def marked():
        return written.read_text(encoding="utf-8").splitlines() if written.exists() else []

    def listens():
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return True
        except OSError:
            return False

    def until(condition, failure):
        """Whether ``condition`` came true; False once the process ended, as the final assertion
        then shows."""
        deadline = time.monotonic() + 120
        while not condition():
            if process.poll() is not None:
                return False
            assert time.monotonic() < deadline, failure
            time.sleep(0.05)
        return True

    try:
        starting = written.with_suffix(".starting")
        if startup == "held":
            until(starting.exists, "the startup did not begin")
            process.terminate()
        elif startup != "free":
            until(written.with_suffix(".raised").exists, "the worker raised no SIGTERM")
        starting.unlink(missing_ok=True)
        serves = until(listens, "the service did not start")  # it listens once it took the signals
        if serves and (startup == "free" or marked()[:1] == ["handler"]):
            process.terminate()  # a runner that met the startup's SIGTERM at once serves on
        output, _ = process.communicate(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(10)
    assert (marked(), process.returncode) == (marks, status), output.decode()[-3000:]


ENDS_WITHOUT_UVICORNS_SHUTDOWN = """
import asyncio, sys
from contextlib import asynccontextmanager
import uvicorn
from exomem.server import ExomemFastMCP

ending, teardown, marks = sys.argv[1], sys.argv[2], []
tearing = asyncio.Event()

@asynccontextmanager
async def lifespan(_server):
    marks.append("started")
    yield
    marks.append("tearing down")
    tearing.set()
    await asyncio.sleep(0.2)  # a teardown that awaits, as the stop drain does
    if teardown == "fails":  # as a worker that does not stop
        raise RuntimeError("the teardown failed")
    marks.append("torn down")

async def main():
    serving, serve = asyncio.Event(), uvicorn.Server.main_loop

    async def main_loop(server):
        serving.set()
        if ending == "the server loop raises":
            raise ValueError("the server loop failed")
        if ending == "the exit is forced":  # as a second SIGINT does
            server.force_exit = True
            return
        await serve(server)

    uvicorn.Server.main_loop = main_loop
    runner = asyncio.create_task(ExomemFastMCP("embedded", lifespan=lifespan).run_http_async(
        transport="http", host="127.0.0.1", port=0, show_banner=False))
    if ending.startswith("the caller cancels"):
        await serving.wait()
        runner.cancel()
    if ending == "the caller cancels twice":
        await tearing.wait()
        runner.cancel()
    try:
        await runner
    except BaseException as error:
        marks.append(type(error).__name__)
        if teardown == "fails":
            marks.append(f"context {type(error.__context__).__name__}")
    await asyncio.sleep(0.5)  # the caller's event loop lives on

asyncio.run(main())
print(marks)  # after the loop closed, so a teardown left to that close shows last
"""


@pytest.mark.parametrize(
    ("ending", "teardown", "raised"),
    [
        ("the caller cancels", "returns", ["torn down", "CancelledError"]),
        ("the server loop raises", "returns", ["torn down", "ValueError"]),
        # The runner stays until the teardown has ended, and then raises the cancellation.
        ("the caller cancels twice", "returns", ["torn down", "CancelledError"]),
        # A failed teardown reaches the caller, with how the runner ended as its context.
        ("the caller cancels", "fails", ["RuntimeError", "context CancelledError"]),
        ("the server loop raises", "fails", ["RuntimeError", "context ValueError"]),
        ("the exit is forced", "fails", ["RuntimeError", "context NoneType"]),
    ],
)
def test_an_http_runner_that_ends_without_uvicorns_shutdown_tears_down_before_it_raises(
    ending, teardown, raised
):
    """An embedding caller whose awaited runner was cancelled or failed while the teardown has not
    run: the workers and the writer lease outlive the runner until the event loop closes, the
    teardown runs twice, a second cancellation returns before the teardown has ended, or the
    caller sees no failure of the teardown and takes the workers as stopped."""
    import subprocess

    finished = subprocess.run(
        [sys.executable, "-c", ENDS_WITHOUT_UVICORNS_SHUTDOWN, ending, teardown],
        capture_output=True, text=True, timeout=120,
    )
    lines = finished.stdout.splitlines()
    expected = ["started", "tearing down", *raised]
    assert lines[-1:] == [repr(expected)], finished.stderr[-3000:]


def trashed_once(root, rel, body):
    """Where a trashed binary's bytes are now: every file under the trash that holds them."""
    return [
        path.relative_to(root).as_posix()
        for path in (root / "Knowledge Base/_trash").rglob("*")
        if path.is_file() and path.read_bytes() == body
    ]


def test_a_writer_that_renames_a_trashed_binary_while_the_projector_is_about_to_converges(
    stored, service, monkeypatch, caplog
):
    """The served projector judging a queued binary move, then finding that the writer moved the
    file between its look and its rename: it defers the move, logs it, or renames something.

    The projector's rename takes the writer's hold, so it cannot run while the writer renames; the
    writer can only move the file after the projector has looked at both paths.
    """
    picture = f"{NOTES}/raced.png"
    (stored / picture).write_bytes(PICTURE_BYTES)
    importer.Importer(stored).import_paths([picture])
    at_look, writer_moved = threading.Event(), threading.Event()
    real_observe, real_move = projector.observe, reserved_paths.move_generic_path
    looked_at_the_source, moved_after_the_look = [], []

    def looking_early(filesystem, rel, **options):
        seen = real_observe(filesystem, rel, **options)
        if rel == picture:
            looked_at_the_source.append(rel)
        elif looked_at_the_source and not at_look.is_set():
            # The projector has judged both paths of the move; the writer now moves the file.
            at_look.set()
            writer_moved.wait(10)
        return seen

    def renaming_first(root, source, destination, **options):
        if source == picture:
            moved_after_the_look.append(at_look.wait(10))
            try:
                return real_move(root, source, destination, **options)
            finally:
                writer_moved.set()
        return real_move(root, source, destination, **options)

    monkeypatch.setattr(projector, "observe", looking_early)
    monkeypatch.setattr(reserved_paths, "move_generic_path", renaming_first)
    caplog.set_level("WARNING", logger="exomem.vault_store")
    loop = idle_service_projector(stored, poll_seconds=0.02)
    try:
        result = call(
            stored, "manage_memory_file", operation="delete", path=picture, confirm=True,
            force_orphan=True,
        )
        deadline = time.monotonic() + 30
        while queued(stored):
            assert time.monotonic() < deadline, "the projector drained the queue"
            time.sleep(0.05)
    finally:
        loop.stop()

    assert moved_after_the_look == [True], "the writer moved the file after the projector looked"
    (trashed,) = trashed_once(stored, picture, PICTURE_BYTES)
    assert result.get("ok") is not False and not (stored / picture).exists()
    assert store_rows(stored, "SELECT sha256 FROM store_binaries WHERE path = ?", trashed) == [
        (sha(PICTURE_BYTES),)
    ]
    assert store_rows(stored, "SELECT path FROM store_binaries WHERE path = ?", picture) == []
    assert store_rows(stored, "SELECT path FROM store_held") == []
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


@pytest.mark.timeout(900)
def test_a_projector_drain_holds_the_boundary_per_record_and_a_foreground_write_waits_little(
    stored, service, monkeypatch, caplog
):
    """A drain that holds the vault boundary for its whole queue, so a foreground write waits for
    all of it and fails at the mutation timeout, or a drain that logs or fails on a busy boundary.
    Sized by ``EXOMEM_SERVED_DRILL_INTENTS``; ``EXOMEM_SERVED_DRILL_REPORT`` names a file for the
    measurements."""
    intents = int(os.environ.get("EXOMEM_SERVED_DRILL_INTENTS", "40"))
    holds: dict[str, list[float]] = {}
    real = projector._holding_boundary

    @contextmanager
    def timed(root, operation):
        """From the take to the release of one hold; a busy miss is no hold."""
        began, held = time.monotonic(), False
        try:
            with real(root, operation):
                held = True
                yield
        finally:
            if held:
                holds.setdefault(operation, []).append(time.monotonic() - began)

    monkeypatch.setattr(projector, "_holding_boundary", timed)

    def foreground(label):
        """One governed write as a person makes it, and how long it took."""
        began = time.monotonic()
        call(
            stored,
            "manage_memory_file",
            operation="create",
            path=f"{NOTES}/Foreground {label}.md",
            content="A foreground write.\n",
        )
        return time.monotonic() - began

    server = runtime._server(stored.resolve())

    def waits():
        """What a foreground write waits for that the drain can hold: the vault boundary, taken as
        a command takes it, and a store request's turn on the store thread."""
        began = time.monotonic()
        with get_manager().mutation_guard(stored, operation="a_foreground_write"):
            boundary = time.monotonic() - began
        began = time.monotonic()
        server.call(lambda: None, wake_maintenance=False)
        return boundary, time.monotonic() - began

    drained_pages = [f"{NOTES}/Drained/Page {n:04d}.md" for n in range(intents)]
    commit(stored, puts=tuple(write.PagePut(rel, f"# {rel}\n".encode()) for rel in drained_pages))
    # The same waits and writes on the same pages, with the intents queued and no projector.
    baseline_waits = []
    for _ in range(20):
        baseline_waits.append(waits())
        time.sleep(0.2)
    baseline = [foreground(f"before {n}") for n in range(2)]
    holds.clear()
    caplog.set_level("WARNING", logger="exomem.vault_store")
    # The service's own projector loop, which a busy pass sends to its next poll.
    loop = projector.Projector(stored)
    began = time.monotonic()
    loop.start()
    during, during_waits = [], []
    try:
        # The waits are sampled back to back, so their count follows the drain's length in samples
        # of a few milliseconds, not in fixed sleeps. The two writes come after the first three
        # samples: a write that ran first, or beside the samples, could outlast a fast drain.
        while set(drained_pages) & queued(stored):
            assert time.monotonic() - began < 780, "the drain finished"
            if len(during_waits) >= 3 and len(during) < 2:
                during.append(foreground(f"during {len(during)}"))
            during_waits.append(waits())
        drain_seconds = time.monotonic() - began
    finally:
        loop.stop()
    projection = holds.get("vault_store_projection", [])
    assert len(projection) >= intents, "one hold for each record, none for the whole drain"
    assert len(during_waits) >= 3, "foreground waits were measured while the projector drained"

    # One importer batch: an outside edit of as many pages as one store-thread job settles.
    batch = drained_pages[: importer.APPLY_BATCH]
    for rel in batch:
        (stored / rel).write_bytes(f"# {rel}\n\nEdited outside.\n".encode())
    imported = importer.Importer(stored).import_paths(batch)

    def ms(values, how):
        return round(how(values) * 1000, 1)

    b_idle, q_idle = zip(*baseline_waits, strict=True)
    b_busy, q_busy = zip(*during_waits, strict=True)
    figures = {
        "intents": intents,
        "drain_seconds": round(drain_seconds, 1),
        "projection_holds": len(projection),
        "projection_hold_ms": {"median": ms(projection, statistics.median),
                               "max": ms(projection, max)},
        "import_batch_paths": len(batch),
        "import_hold_ms": [ms([t], max) for t in holds.get("vault_store_import", [])],
        "boundary_wait_ms_without_drain": {"median": ms(b_idle, statistics.median),
                                           "max": ms(b_idle, max), "samples": len(b_idle)},
        "boundary_wait_ms_during_drain": {"median": ms(b_busy, statistics.median),
                                          "max": ms(b_busy, max), "samples": len(b_busy)},
        "store_turn_ms_without_drain": {"median": ms(q_idle, statistics.median),
                                        "max": ms(q_idle, max)},
        "store_turn_ms_during_drain": {"median": ms(q_busy, statistics.median),
                                       "max": ms(q_busy, max)},
        "create_ms_without_drain": [ms([t], max) for t in baseline],
        "create_ms_during_drain": [ms([t], max) for t in during],
        # Windows has no load average: the figure is unknown there, not zero.
        "load_average": [round(x, 1) for x in os.getloadavg()] if hasattr(os, "getloadavg") else None,
    }
    print(json.dumps(figures))
    if report := os.environ.get("EXOMEM_SERVED_DRILL_REPORT"):
        Path(report).write_text(json.dumps(figures, indent=1), encoding="utf-8")
    assert [r.getMessage() for r in caplog.records if r.name.startswith("exomem.vault_store")] == []
    assert sorted(imported.imported) == sorted(batch)
    # A hold is one record or one batch: far inside the timeout that a foreground writer waits.
    timeout = get_manager()._mutation_timeout_seconds
    assert max(max(times) for times in holds.values()) < timeout / 4
    assert max(b_busy) < timeout / 4 and max(q_busy) < timeout / 4
