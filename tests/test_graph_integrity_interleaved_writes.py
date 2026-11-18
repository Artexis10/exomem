"""Interleaved media and note writes leave the graph and registries complete.

The media-write starvation fix moved commit-tail publication to a handoff
worker behind a bounded wait. This proves the derived surfaces — graph edges
and inbound backlinks, the corpus projection, the watcher intents — stay
whole while a multi-path `process_media` batch runs beside keyed note
writes, including publications that handed off past the boundary.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from exomem import (
    cli_ops,
    commands,
    deferred_index,
    epistemic_graph,
    file_watcher,
    freshness,
    graph_drain,
    graph_sync,
    index_sync,
    media_processing,
    writer_lease,
)
from exomem.epistemic_graph import EpistemicGraphIndex

_OBSERVE_SECONDS = 15.0
_MEDIA = [f"Knowledge Base/Evidence/Audio/interleave-clip-{index}.m4a" for index in range(5)]
_SLUGS = [f"interleave-{index}" for index in range(6)]
_NOTE_RELS = [f"Knowledge Base/Notes/Insights/{slug}.md" for slug in _SLUGS]
_WORKER_THREAD = "exomem-self-write-publish"


@pytest.fixture(autouse=True)
def _reset_managers():
    yield
    writer_lease.reset_managers_for_tests()


@pytest.fixture(autouse=True)
def _stop_the_daemon():
    yield
    graph_sync.drain_active_rebuilds(timeout=60.0)
    graph_drain.stop()
    graph_drain._DEBT.clear()  # noqa: SLF001


def _command(name: str):
    return next(c for c in commands.product_commands_for("mcp") if c.name == name)


def _invoke(command, vault: Path, **kwargs):
    """One real command call; a typed busy refusal gets a bounded client retry
    on the same idempotency key, as a real client would. Eight writers plus a
    media batch can transiently fill the parked-waiter cap, so the retry window
    is wide and the backoff grows."""
    deadline = time.monotonic() + 120.0
    delay = 0.3
    while True:
        try:
            return writer_lease.invoke_command(command, vault, **kwargs)
        except cli_ops.OpError as error:
            if "MUTATION_BUSY" not in str(error) or time.monotonic() + delay > deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 1.5, 2.0)


def _note_kwargs(vault: Path, index: int, *, links: list[str]) -> dict:
    """One validated `remember` payload whose page links resolve to the pages
    the interleaving writes."""
    content = (
        f"# Interleave {index}\n\n## Observations\n\n"
        f"- [finding] Note {index} committed beside a media batch #interleaved\n"
    )
    if links:
        content += "\n## Connections\n\n" + "\n".join(f"- [[{link}]]" for link in links) + "\n"
    kwargs: dict = {
        "content": content,
        "title": f"Interleave {index}",
        "slug": _SLUGS[index],
        "suggestions": False,
    }
    validation = commands.op_remember(vault, validate_only=True, **kwargs)
    kwargs.update(
        draft_id=validation["draft_id"],
        draft_hash=validation["draft_hash"],
        draft_token=validation["draft_token"],
    )
    if validation.get("reviewed_none_required"):
        kwargs.update(
            relation_disposition="reviewed_none",
            relation_review_hash=validation["draft_hash"],
            relation_review_reason="The test asserts the edges itself.",
        )
    return kwargs


def _graph_edges(root: Path) -> set[tuple[str, str, str]]:
    connection = sqlite3.connect(epistemic_graph.sidecar_path(root))
    try:
        rows = connection.execute(
            "SELECT source_path, dst_key, COALESCE(relation_type, raw_relation)"
            " FROM graph_edges"
        ).fetchall()
    finally:
        connection.close()
    return {(src, dst.removeprefix("file:"), rel) for src, dst, rel in rows}


def _inbound(root: Path, target: str) -> set[str]:
    """Pages listing `target` through the real inbound-links reader."""
    answer = commands.op_list_inbound_links(root, target=target)
    return {row["path"] for row in answer["inbound"]}


def _graph_files(root: Path) -> set[str]:
    connection = sqlite3.connect(epistemic_graph.sidecar_path(root))
    try:
        rows = connection.execute(
            "SELECT path FROM graph_nodes WHERE kind = 'file'"
        ).fetchall()
    finally:
        connection.close()
    return {row[0] for row in rows}


def _drain_to_empty(root: Path) -> None:
    """Run drain cycles until nothing is queued and readers can open the graph."""
    for _ in range(12):
        if (
            not deferred_index.list_graph_paths(root)
            and deferred_index.graph_full_rebuild_pending(root) is None
            and EpistemicGraphIndex(root).available()
        ):
            return
        graph_drain._work_once(root)  # noqa: SLF001 - the tests' drain driver
    raise AssertionError(
        "the graph drain did not empty: "
        f"paths={deferred_index.list_graph_paths(root)} "
        f"full_rebuild={deferred_index.graph_full_rebuild_pending(root)} "
        f"available={EpistemicGraphIndex(root).available()}"
    )


def _await_publication_settle(root: Path) -> None:
    """Bounded wait for the publish worker to close every intent it owns."""
    deadline = time.monotonic() + _OBSERVE_SECONDS
    canon = file_watcher._canon_root(root)  # noqa: SLF001
    while time.monotonic() < deadline:
        with file_watcher._WATCHERS_LOCK:  # noqa: SLF001
            pending = [
                intent
                for key, intent in file_watcher._PUBLICATION_INTENTS.items()  # noqa: SLF001
                if key[0] == canon and intent.disposition == "active"
            ]
        if not pending and file_watcher._self_write_publish_queue().empty():  # noqa: SLF001
            return
        time.sleep(0.02)
    raise AssertionError(
        f"publication intents still active after {_OBSERVE_SECONDS}s: "
        f"{[intent.path for intent in pending]}"
    )


def _corpus_paths(root: Path) -> set[str]:
    """Every page the live corpus projection currently serves, both scopes."""
    paths: set[str] = set()
    for scope in ("vault", "kb"):
        entries = freshness.live_recall_entries(root, scope)
        if entries:
            paths.update(
                str(Path(entry).relative_to(root))
                if Path(entry).is_absolute()
                else entry
                for entry in entries
            )
    return paths


def _authored_edges(note_paths: list[str]) -> set[tuple[str, str, str]]:
    """Only the edges the writes authored — wikilinks and the typed relation.
    Compiled pages also carry `derived_from` self-edges the test did not write;
    they are deliberately out of scope."""
    slug_paths = dict(zip(_SLUGS, note_paths, strict=True))
    authored: set[tuple[str, str, str]] = set()
    for index, slug in enumerate(_SLUGS):
        if index > 0:
            authored.add((slug_paths[slug], slug_paths[_SLUGS[index - 1]], "links_to"))
        if index == 0:
            authored.add((slug_paths[slug], f"{_MEDIA[0]}.md", "links_to"))
        if index == 1:
            authored.add((slug_paths[slug], slug_paths[_SLUGS[0]], "supports"))
    return authored


def _integrity_failures(vault: Path, note_paths: list[str]) -> list[str]:
    """Every way the settled derived state diverges from what was written."""
    failures: list[str] = []
    sidecars = [f"{rel}.md" for rel in _MEDIA]
    edges = _graph_edges(vault)
    for src, dst, rel in _authored_edges(note_paths):
        if (src, dst, rel) not in edges:
            failures.append(f"edge {src} -[{rel}]-> {dst} missing")
        elif rel == "links_to" and src not in _inbound(vault, dst):
            failures.append(f"{src} missing from {dst}'s inbound sources")
    projected = _corpus_paths(vault)
    graph_files = _graph_files(vault)
    for rel in (*note_paths, *sidecars):
        if rel not in graph_files:
            failures.append(f"{rel} missing from the graph")
        if rel not in projected:
            failures.append(f"{rel} missing from the corpus projection")
    with file_watcher._WATCHERS_LOCK:  # noqa: SLF001
        failures.extend(
            f"publication intent still active: {intent.path}"
            for key, intent in file_watcher._PUBLICATION_INTENTS.items()  # noqa: SLF001
            if intent.disposition == "active"
        )
    return failures


def _interleave(
    vault: Path, monkeypatch: pytest.MonkeyPatch, *, force_handoff: bool
) -> tuple[list[str], list, dict]:
    """Run the batch and the note writes, settle, and return
    ``(note_paths, close_results, publishes)``."""
    for rel in _MEDIA:
        binary = vault / rel
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"\x00\x00\x00\x18ftypM4A fake audio")

    first_hold = threading.Event()
    real_committing = media_processing._committing  # noqa: SLF001

    def slow_committing(vault_root, guard, operation, unit):
        def slowed():
            first_hold.set()
            time.sleep(0.5)
            return unit()

        return real_committing(vault_root, guard, operation, slowed)

    monkeypatch.setattr(media_processing, "_committing", slow_committing)  # noqa: SLF001

    # Record every self-write close outcome; a handoff releases the worker's
    # blocked publish below.
    close_results: list = []
    release_blocked = threading.Event()
    real_close = file_watcher.register_self_write_and_close

    def recorded_close(*args, **kwargs):
        result = real_close(*args, **kwargs)
        close_results.append(result)
        if result is file_watcher.PUBLICATION_HANDED_OFF:
            release_blocked.set()
        return result

    monkeypatch.setattr(file_watcher, "register_self_write_and_close", recorded_close)

    # Every publish the self-write path runs, wherever it lands. With
    # force_handoff the first worker publish that touches a test note blocks
    # until a caller has observed PUBLICATION_HANDED_OFF, so a handoff is
    # guaranteed rather than timing-dependent.
    publishes: dict[str, list] = {"inline": [], "worker": []}
    real_publish = index_sync.publish_corpus_delta

    def probe(root_arg, **kwargs):
        changed = {str(path) for path in kwargs.get("changed", ())}
        if (
            force_handoff
            and threading.current_thread().name == _WORKER_THREAD
            and any("interleave-" in path for path in changed)
            and not release_blocked.is_set()
        ):
            release_blocked.wait(_OBSERVE_SECONDS * 4)
        result = real_publish(root_arg, **kwargs)
        lane = "worker" if threading.current_thread().name == _WORKER_THREAD else "inline"
        publishes[lane].extend(str(path) for path in kwargs.get("changed", ()))
        return result

    monkeypatch.setattr(index_sync, "publish_corpus_delta", probe)

    batch_result: dict = {}
    batch_error: list[BaseException] = []

    def run_batch() -> None:
        try:
            batch_result["result"] = _invoke(
                _command("process_media"),
                vault,
                paths=_MEDIA,
                operation="reprocess",
                idempotency_key="media-interleave",
            )
        except BaseException as error:  # noqa: BLE001 - report it to the assertion
            batch_error.append(error)

    note_results: dict[str, dict] = {}
    note_errors: list[BaseException] = []

    def run_note(index: int) -> None:
        links = [] if index == 0 else [f"Knowledge Base/Notes/Insights/{_SLUGS[index - 1]}"]
        if index == 0:
            links.append(_MEDIA[0])
        try:
            for attempt in range(4):
                try:
                    note_results[_SLUGS[index]] = _invoke(
                        _command("remember"),
                        vault,
                        **_note_kwargs(vault, index, links=links),
                        idempotency_key=f"note-interleave-{index}",
                    )
                    break
                except ValueError as error:
                    # The relation-review verdict can flip between validation
                    # and commit as the other notes land; re-validate honestly.
                    if "RELATION_REVIEW" not in str(error) or attempt == 3:
                        raise
        except BaseException as error:  # noqa: BLE001 - report it to the assertion
            note_errors.append(error)

    note_done = {slug: threading.Event() for slug in _SLUGS}

    def run_first_note() -> None:
        try:
            run_note(0)
        finally:
            note_done[_SLUGS[0]].set()

    def run_later_note(index: int) -> None:
        # Each note links to the previous one, so a later create only validates
        # once its target exists; the creates still run inside the live batch.
        assert note_done[_SLUGS[index - 1]].wait(_OBSERVE_SECONDS * 2)
        try:
            run_note(index)
        finally:
            note_done[_SLUGS[index]].set()

    def run_edit() -> None:
        # An edit_note once the first create landed: a keyed write that must
        # slot between media units, not behind the batch.
        assert note_done[_SLUGS[0]].wait(_OBSERVE_SECONDS * 2)
        target = note_results[_SLUGS[0]]["path"]
        try:
            note_results["edit"] = _invoke(
                _command("edit_memory"),
                vault,
                path=target,
                why="concurrent correction beside a media batch",
                operation={
                    "kind": "replace_string",
                    "old_string": "committed beside a media batch",
                    "new_string": "committed and edited beside a media batch",
                },
                idempotency_key="edit-interleave",
            )
        except BaseException as error:  # noqa: BLE001 - report it to the assertion
            note_errors.append(error)

    def run_observation() -> None:
        # An add_observation onto the second note, same interleaving.
        assert note_done[_SLUGS[1]].wait(_OBSERVE_SECONDS * 2)
        assert note_done[_SLUGS[0]].wait(_OBSERVE_SECONDS * 2)
        target = note_results[_SLUGS[1]]["path"]
        try:
            note_results["observe"] = _invoke(
                _command("observe_memory"),
                vault,
                path=target,
                operation="add",
                category="finding",
                kind="claim",
                content="Observed while the batch was still running",
                tags=["interleaved"],
                relations=[{"kind": "supports", "target": note_results[_SLUGS[0]]["path"]}],
                idempotency_key="observe-interleave",
            )
        except BaseException as error:  # noqa: BLE001 - report it to the assertion
            note_errors.append(error)

    threads: list[threading.Thread] = []

    def started(target, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        threads.append(thread)
        thread.start()

    try:
        started(run_batch, "media-batch")
        assert first_hold.wait(_OBSERVE_SECONDS)
        started(run_first_note, "note-0")
        for index in range(1, 6):
            started(lambda index=index: run_later_note(index), f"note-{index}")
        started(run_edit, "note-edit")
        started(run_observation, "note-observe")
    finally:
        for thread in threads:
            thread.join(timeout=_OBSERVE_SECONDS * 3)

    assert not any(thread.is_alive() for thread in threads), (
        f"threads still running: {[thread.name for thread in threads if thread.is_alive()]}"
    )
    assert not batch_error and not note_errors, (batch_error, note_errors)

    _await_publication_settle(vault)
    # The fixture vault has never been read; ensure one whole graph exists. An
    # owner may already be mid-rebuild, so wait out the live claim, drive the
    # queued drain work, and only then build synchronously if still missing.
    deadline = time.monotonic() + 120.0
    while not EpistemicGraphIndex(vault).available():
        graph_sync.drain_active_rebuilds(timeout=60.0)
        if (
            deferred_index.list_graph_paths(vault)
            or deferred_index.graph_full_rebuild_pending(vault) is not None
        ):
            graph_drain._work_once(vault)  # noqa: SLF001
        if EpistemicGraphIndex(vault).available():
            break
        if time.monotonic() > deadline:
            raise AssertionError("graph never became available")
        try:
            EpistemicGraphIndex(vault).rebuild_all()
        except graph_sync.GraphRebuildInProgress:
            continue
    _drain_to_empty(vault)
    note_paths = [note_results[slug]["path"] for slug in _SLUGS]
    return note_paths, close_results, publishes


@pytest.mark.parametrize("force_handoff", [False, True], ids=["within-bound", "handed-off"])
def test_interleaved_media_and_note_writes_leave_the_graph_complete(
    vault: Path, monkeypatch: pytest.MonkeyPatch, force_handoff: bool
) -> None:
    note_paths, close_results, publishes = _interleave(
        vault, monkeypatch, force_handoff=force_handoff
    )

    # Six creates, one edit, one observation: every write committed exactly once.
    assert len(note_paths) == 6
    assert len(set(note_paths)) == 6
    for rel in note_paths:
        assert (vault / rel).is_file(), rel
    sidecars = [f"{rel}.md" for rel in _MEDIA]
    for rel in sidecars:
        assert (vault / rel).is_file(), rel

    failures = _integrity_failures(vault, note_paths)
    assert not failures, failures

    if force_handoff:
        assert any(
            result is file_watcher.PUBLICATION_HANDED_OFF for result in close_results
        ), f"no publication handed off: {close_results}"
        assert publishes["worker"], "no publication actually ran on the handoff worker"

    # The derived graph equals a fresh rebuild — nothing dropped or doubled.
    index = EpistemicGraphIndex(vault)
    assert index.available()
    drained = _graph_edges(vault)
    files_before = _graph_files(vault)
    index.rebuild_all()
    assert _graph_edges(vault) == drained
    assert _graph_files(vault) == files_before


def test_a_dropped_handoff_publication_still_heals(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A silently dropped worker publication is lost, and the settle-time
    machinery — the `freshness.reconcile` walk and `on_inbound_files_changed`
    patching — re-derives it, so every completeness assertion still holds."""
    real_publish = index_sync.publish_corpus_delta
    dropped: list[str] = []

    def drop_first_worker_publish(root_arg, **kwargs):
        if threading.current_thread().name == _WORKER_THREAD and not dropped:
            changed = {str(path) for path in kwargs.get("changed", ())}
            if any("interleave-" in path for path in changed):
                dropped.extend(sorted(changed))
                return True  # swallowed: no abort, no replay
        return real_publish(root_arg, **kwargs)

    monkeypatch.setattr(index_sync, "publish_corpus_delta", drop_first_worker_publish)

    note_paths, close_results, _publishes = _interleave(
        vault, monkeypatch, force_handoff=True
    )
    assert dropped, "no publication ever ran on the worker to be dropped"
    assert any(
        result is file_watcher.PUBLICATION_HANDED_OFF for result in close_results
    )

    failures = _integrity_failures(vault, note_paths)
    assert not failures, failures


def test_a_missing_authored_edge_is_detected(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: the graph fan-out drops one path's authored edges at
    the row-building seam both the upsert and the rebuild share, and the
    completeness check reports exactly those missing edges."""
    victim = _NOTE_RELS[3]
    real_edges = epistemic_graph._structural_edges_for_page  # noqa: SLF001

    def skip_victim(vault_root, page, state, **kwargs):
        if page.rel_path == victim:
            return []
        return real_edges(vault_root, page, state, **kwargs)

    monkeypatch.setattr(
        epistemic_graph, "_structural_edges_for_page", skip_victim  # noqa: SLF001
    )

    note_paths, _close_results, _publishes = _interleave(
        vault, monkeypatch, force_handoff=False
    )

    failures = _integrity_failures(vault, note_paths)
    assert failures == [f"edge {victim} -[links_to]-> {_NOTE_RELS[2]} missing"], failures
