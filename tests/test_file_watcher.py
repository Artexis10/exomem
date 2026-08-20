"""file_watcher — debounce/dispatch LOGIC tested directly (no real watchdog observer).

We stub embeddings.upsert_after_write / delete_after_remove and feed change events,
asserting the watcher coalesces them into one batched dispatch with the right paths.
The soft-fail path (watchdog import fails → start() is a no-op) is tested by patching
the lazy import.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

import pytest

from exomem import (
    deferred_index,
    embeddings,
    epistemic_graph,
    file_watcher,
    freshness,
    media_processing,
    semantic_contract,
)
from exomem import find as find_module
from exomem import reconcile as reconcile_module
from exomem import vault as vault_module


def _stub_embeddings(monkeypatch: pytest.MonkeyPatch):
    ups: list[list[Path]] = []
    dels: list[list[str]] = []

    def upsert_status(root, paths):
        ups.append(list(paths))
        return embeddings.EmbeddingSyncStatus("completed", "embedding_upsert_completed", len(paths))

    def delete_status(root, rels):
        dels.append(list(rels))
        return embeddings.EmbeddingSyncStatus("completed", "embedding_delete_completed", len(rels))

    monkeypatch.setattr(embeddings, "upsert_after_write_status", upsert_status)
    monkeypatch.setattr(embeddings, "delete_after_remove_status", delete_status)
    return ups, dels


def test_flush_batches_upserts_and_dedupes(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    ups, dels = _stub_embeddings(monkeypatch)
    w = file_watcher.FileWatcher(vault)
    a = vault / "Knowledge Base" / "Notes" / "a.md"
    b = vault / "Knowledge Base" / "Notes" / "b.md"
    a.parent.mkdir(parents=True, exist_ok=True)
    a.write_text("# A\n", encoding="utf-8")
    b.write_text("# B\n", encoding="utf-8")
    w._record(a, deleted=False)
    w._record(a, deleted=False)  # duplicate save coalesces
    w._record(b, deleted=False)
    assert freshness.external_pending(vault) is True
    w._flush()
    assert len(ups) == 1, "one batched upsert call for the whole window"
    assert sorted(ups[0]) == sorted([a, b])
    assert dels == []
    assert freshness.external_pending(vault) is False
    # Pending cleared after flush — a second flush dispatches nothing.
    w._flush()
    assert len(ups) == 1


def test_external_pending_ack_does_not_clear_a_newer_event(vault: Path) -> None:
    first = freshness.mark_external_pending(vault)
    freshness.invalidate(vault)
    assert freshness.external_pending(vault) is True
    second = freshness.mark_external_pending(vault)

    freshness.clear_external_pending(vault, through=first)

    assert freshness.external_pending(vault) is True

    freshness.clear_external_pending(vault, through=second)

    assert freshness.external_pending(vault) is False


def test_out_of_kb_markdown_event_repairs_vault_wide_graph(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repaired: list[list[Path]] = []
    monkeypatch.setattr(
        epistemic_graph,
        "upsert_after_write",
        lambda _root, paths: repaired.append(list(paths)),
    )
    shadow = vault / "Reference" / "shadow.md"
    shadow.parent.mkdir(parents=True, exist_ok=True)
    shadow.write_text("---\ntitle: Future Target\n---\n# Shadow\n", encoding="utf-8")
    watcher = file_watcher.FileWatcher(vault)

    watcher._record(shadow, deleted=False)
    assert freshness.external_pending(vault) is True
    watcher._flush()

    assert repaired == [[shadow]]
    assert freshness.external_pending(vault) is False


def test_live_ack_withdraws_graph_before_same_signature_fanout(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = vault / "Knowledge Base" / "Notes" / "pending-a.md"
    b = vault / "Knowledge Base" / "Notes" / "pending-b.md"
    a.parent.mkdir(parents=True, exist_ok=True)
    a.write_text("# Pending A\n\n[[pending-b]]\n", encoding="utf-8")
    b.write_text("# Pending B\n", encoding="utf-8")
    watcher = file_watcher.FileWatcher(vault)
    watcher._reconcile_once(seed=True)
    graph = epistemic_graph.EpistemicGraphIndex(vault)
    graph.rebuild_all()
    a_rel = _vault_rel(vault, a)
    assert any(edge["relation_type"] == "links_to" for edge in graph.edges(source_path=a_rel))
    old_signature = freshness.stat_signature(a)

    a.write_text("# Pending A\n\nLink removed.\n", encoding="utf-8")
    watcher._record(a, deleted=False)
    real_stat_signature = freshness.stat_signature

    def coarse_signature(path: Path):
        return old_signature if Path(path) == a else real_stat_signature(path)

    monkeypatch.setattr(freshness, "stat_signature", coarse_signature)
    real_upsert = file_watcher.index_sync.upsert_after_write
    handoff: list[tuple[bool, bool]] = []

    def observe_handoff(*args, **kwargs):
        handoff.append((freshness.external_pending(vault), graph.available()))
        return real_upsert(*args, **kwargs)

    monkeypatch.setattr(file_watcher.index_sync, "upsert_after_write", observe_handoff)

    watcher._flush()

    assert handoff == [(False, False)]
    assert freshness.external_pending(vault) is False
    assert graph.available() is True
    assert not any(
        edge["relation_type"] == "links_to" for edge in graph.edges(source_path=a_rel)
    )


def test_live_graph_read_barrier_preserves_incremental_repair(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = vault / "Knowledge Base" / "Notes" / "bounded-live-edit.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("# Bounded live edit\n\nBefore.\n", encoding="utf-8")
    watcher = file_watcher.FileWatcher(vault)
    watcher._reconcile_once(seed=True)
    graph = epistemic_graph.EpistemicGraphIndex(vault)
    graph.rebuild_all()
    rel = _vault_rel(vault, page)

    page.write_text("# Bounded live edit\n\nAfter.\n", encoding="utf-8")
    watcher._record(page, deleted=False)
    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex,
        "_rebuild_all_locked",
        lambda *_args, **_kwargs: pytest.fail("ordinary live edit rebuilt the whole graph"),
    )

    watcher._flush()

    assert freshness.external_pending(vault) is False
    assert graph.available() is True
    current = next(node for node in graph.nodes(path=rel) if node["kind"] == "file")
    assert current["source_hash"] == vault_module.content_hash(page.read_bytes().decode("utf-8"))


@pytest.mark.parametrize("restart", [False, True])
def test_reconcile_repairs_a_persisted_graph_read_barrier(
    vault: Path,
    restart: bool,
) -> None:
    watcher = file_watcher.FileWatcher(vault)
    watcher._reconcile_once(seed=True)
    graph = epistemic_graph.EpistemicGraphIndex(vault)
    graph.rebuild_all()
    graph.suspend_reads()
    assert graph.available() is False
    if restart:
        freshness.clear()

    watcher._reconcile_once(seed=restart)

    assert graph.available() is True


def test_startup_hash_validation_repairs_a_pre_barrier_crash(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = vault / "Knowledge Base" / "Notes" / "crash-a.md"
    b = vault / "Knowledge Base" / "Notes" / "crash-b.md"
    a.parent.mkdir(parents=True, exist_ok=True)
    a.write_text("# Crash A\n\n[[crash-b]]\n", encoding="utf-8")
    b.write_text("# Crash B\n", encoding="utf-8")
    watcher = file_watcher.FileWatcher(vault)
    watcher._reconcile_once(seed=True)
    graph = epistemic_graph.EpistemicGraphIndex(vault)
    graph.rebuild_all()
    a_rel = _vault_rel(vault, a)
    old_signature = freshness.stat_signature(a)

    a.write_text("# Crash A\n\nLink gone.\n", encoding="utf-8")
    watcher._record(a, deleted=False)
    assert graph.reads_suspended() is False
    freshness.clear()  # process died during debounce; the in-memory epoch is gone
    real_stat_signature = freshness.stat_signature
    monkeypatch.setattr(
        freshness,
        "stat_signature",
        lambda path: old_signature if Path(path) == a else real_stat_signature(path),
    )

    restarted = file_watcher.FileWatcher(vault)
    restarted._reconcile_once(seed=True)

    assert graph.available() is True
    assert not any(
        edge["relation_type"] == "links_to" for edge in graph.edges(source_path=a_rel)
    )


def test_disabled_graph_event_bars_an_existing_sidecar_before_reenable(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = vault / "Knowledge Base" / "Notes" / "disabled-a.md"
    b = vault / "Knowledge Base" / "Notes" / "disabled-b.md"
    a.parent.mkdir(parents=True, exist_ok=True)
    a.write_text("# Disabled A\n\n[[disabled-b]]\n", encoding="utf-8")
    b.write_text("# Disabled B\n", encoding="utf-8")
    watcher = file_watcher.FileWatcher(vault)
    watcher._reconcile_once(seed=True)
    graph = epistemic_graph.EpistemicGraphIndex(vault)
    graph.rebuild_all()
    a_rel = _vault_rel(vault, a)
    old_signature = freshness.stat_signature(a)

    a.write_text("# Disabled A\n\nLink removed.\n", encoding="utf-8")
    watcher._record(a, deleted=False)
    real_stat_signature = freshness.stat_signature
    monkeypatch.setattr(
        freshness,
        "stat_signature",
        lambda path: old_signature if Path(path) == a else real_stat_signature(path),
    )
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_INDEX", "1")
    watcher._flush()

    assert freshness.external_pending(vault) is False
    assert graph.available() is False
    monkeypatch.delenv("EXOMEM_DISABLE_GRAPH_INDEX")
    assert graph.available() is False

    watcher._reconcile_once(seed=False)

    assert graph.available() is True
    assert not any(
        edge["relation_type"] == "links_to" for edge in graph.edges(source_path=a_rel)
    )


def test_failed_live_graph_fanout_rearms_periodic_recovery(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watcher = file_watcher.FileWatcher(vault)
    watcher._reconcile_once(seed=True)
    graph = epistemic_graph.EpistemicGraphIndex(vault)
    graph.rebuild_all()
    page = next(find_module._walk_md(vault / "Knowledge Base"))
    page.write_text(page.read_text(encoding="utf-8") + "\nFanout retry.\n", encoding="utf-8")
    watcher._record(page, deleted=False)
    real_refresh = epistemic_graph.EpistemicGraphIndex.refresh_paths

    def fail_refresh(*_args, **_kwargs):
        raise RuntimeError("transient graph fanout failure")

    monkeypatch.setattr(epistemic_graph.EpistemicGraphIndex, "refresh_paths", fail_refresh)
    watcher._flush()

    assert freshness.external_pending(vault) is True
    assert graph.available() is False

    monkeypatch.setattr(epistemic_graph.EpistemicGraphIndex, "refresh_paths", real_refresh)
    watcher._reconcile_once(seed=False)

    assert freshness.external_pending(vault) is False
    assert graph.available() is True


def test_failed_external_publication_keeps_graph_dirty(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        file_watcher.index_sync,
        "publish_corpus_delta",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        file_watcher.index_sync,
        "upsert_after_write",
        lambda *_args, **_kwargs: None,
    )
    page = vault / "Knowledge Base" / "Notes" / "pending.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("# Pending\n", encoding="utf-8")
    watcher = file_watcher.FileWatcher(vault)

    watcher._record(page, deleted=False)
    watcher._flush()

    assert freshness.external_pending(vault) is True


@pytest.mark.parametrize("failing_component", ["inbound", "resolver"])
def test_external_pending_ack_requires_cache_patch_success(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_component: str,
) -> None:
    page = vault / "Knowledge Base" / "Notes" / "pending-cache-patch.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("# Pending cache patch\n", encoding="utf-8")
    watcher = file_watcher.FileWatcher(vault)
    monkeypatch.setattr(
        file_watcher.index_sync,
        "publish_corpus_delta",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        file_watcher.index_sync,
        "upsert_after_write",
        lambda *_args, **_kwargs: None,
    )

    def fail_patch(*_args, **_kwargs):
        raise RuntimeError("cache patch failed")

    if failing_component == "inbound":
        monkeypatch.setattr(vault_module, "on_inbound_files_changed", fail_patch)
    else:
        monkeypatch.setattr(find_module, "on_resolver_files_changed", fail_patch)

    watcher._record(page, deleted=False)
    watcher._flush()

    assert freshness.external_pending(vault) is True


def test_periodic_reconcile_recovers_a_failed_external_publication(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_watcher.clear_self_write_registry()
    watcher = file_watcher.FileWatcher(vault)
    watcher._reconcile_once(seed=True)
    graph = epistemic_graph.EpistemicGraphIndex(vault)
    graph.rebuild_all()
    page = next(find_module._walk_md(vault / "Knowledge Base"))
    rel = _vault_rel(vault, page)
    original_publish = semantic_contract.publish_corpus_files_changed_classified

    page.write_text(
        page.read_text(encoding="utf-8") + "\nRecovered external edit.\n",
        encoding="utf-8",
    )
    watcher._record(page, deleted=False)

    def fail_publication(*_args, **_kwargs):
        raise RuntimeError("transient publication failure")

    monkeypatch.setattr(
        semantic_contract,
        "publish_corpus_files_changed_classified",
        fail_publication,
    )
    watcher._flush()

    assert freshness.external_pending(vault) is True
    assert freshness.recall_is_live(vault, "vault") is False
    assert graph.available() is False

    monkeypatch.setattr(
        semantic_contract,
        "publish_corpus_files_changed_classified",
        original_publish,
    )
    watcher._reconcile_once(seed=False)

    assert freshness.external_pending(vault) is False
    assert freshness.recall_is_live(vault, "vault") is True
    assert graph.available() is True
    current = next(node for node in graph.nodes(path=rel) if node["kind"] == "file")
    assert current["source_hash"] == vault_module.content_hash(page.read_bytes().decode("utf-8"))


def test_non_markdown_is_ignored(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    ups, dels = _stub_embeddings(monkeypatch)
    w = file_watcher.FileWatcher(vault)
    w._record(vault / "Knowledge Base" / "Evidence" / "scan.png", deleted=False)
    w._flush()
    assert ups == [] and dels == []


def test_observed_access_policy_edit_marks_external_pending(vault: Path) -> None:
    watcher = file_watcher.FileWatcher(vault)
    policy = vault / "Knowledge Base" / "_access.yaml"
    policy.write_text("excluded: []\n", encoding="utf-8")

    watcher._record(policy, deleted=False)

    assert freshness.external_pending(vault) is True
    assert watcher._pending_upsert == set()


# ---- Automatic governed-media dispatch (OpenSpec: automatic-media-processing) ----


def _spy_media_and_text_dispatch(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Capture the separate media leaf and every Markdown freshness/index seam."""
    calls: dict[str, list] = {
        "media": [],
        "freshness": [],
        "inbound": [],
        "resolver": [],
        "upsert": [],
        "delete": [],
    }

    def reconcile_media(
        root: Path,
        path: Path,
        *,
        explicit: bool = True,
        commit_guard=None,  # noqa: ANN001, ARG001
    ) -> None:
        calls["media"].append((root, path, explicit))

    monkeypatch.setattr(media_processing, "reconcile_media", reconcile_media)
    monkeypatch.setattr(
        file_watcher.freshness,
        "on_files_changed",
        lambda root, changed, deleted: calls["freshness"].append(
            (root, list(changed), list(deleted))
        ),
    )
    monkeypatch.setattr(
        file_watcher.index_sync,
        "upsert_after_write",
        lambda root, paths, **kwargs: calls["upsert"].append((root, list(paths), dict(kwargs))),
    )
    monkeypatch.setattr(
        file_watcher.index_sync,
        "delete_after_remove",
        lambda root, rels, **_kwargs: calls["delete"].append((root, list(rels))),
    )
    monkeypatch.setattr(
        "exomem.vault.on_inbound_files_changed",
        lambda root, up, deleted: calls["inbound"].append((root, list(up), list(deleted))),
    )
    monkeypatch.setattr(
        "exomem.find.on_resolver_files_changed",
        lambda root, up, deleted: calls["resolver"].append((root, list(up), list(deleted))),
    )
    return calls


def test_supported_audio_event_dispatches_media_only(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _spy_media_and_text_dispatch(monkeypatch)
    recording = vault / "Knowledge Base" / "Evidence" / "Audio" / "field-note.M4A"
    recording.parent.mkdir(parents=True, exist_ok=True)
    recording.write_bytes(b"direct watcher audio")
    watcher = file_watcher.FileWatcher(vault)

    watcher._record(recording, deleted=False)
    watcher._flush()

    assert calls["media"] == [(vault, recording, False)]
    assert calls["inbound"] == []
    assert calls["resolver"] == []


def test_supported_audio_event_reconciles_under_writer_authority(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    from exomem import writer_lease

    depth = 0

    class Manager:
        @contextmanager
        def mutation_guard(self, root: Path, **metadata):
            nonlocal depth
            assert root == vault
            assert metadata["operation"] == "background_media_event"
            assert metadata["holder_kind"] == "background"
            depth += 1
            try:
                yield
            finally:
                depth -= 1

    monkeypatch.setattr(writer_lease, "get_manager", lambda: Manager())

    def reconcile_media(*_args, commit_guard=None, **_kwargs):  # noqa: ANN001
        assert depth == 0
        assert commit_guard is not None
        with commit_guard():
            assert depth == 1

    monkeypatch.setattr(media_processing, "reconcile_media", reconcile_media)
    recording = vault / "Knowledge Base" / "Evidence" / "Audio" / "guarded.m4a"
    recording.parent.mkdir(parents=True, exist_ok=True)
    recording.write_bytes(b"audio")

    watcher = file_watcher.FileWatcher(vault)
    watcher._record(recording, deleted=False)
    watcher._flush()

    assert depth == 0


# Valves for the ordering test below. The hold has to outlast both the
# observation and the foreground's acquisition timeout: if the background's
# hash finishes on its own, it commits before the foreground ever asks for the
# boundary and the yield this test exists to prove is never exercised.
_MEDIA_HOLD_SECONDS = 60.0
_MEDIA_OBSERVE_SECONDS = 30.0
# How long the foreground waits for the boundary the background is supposed to
# have yielded. 0.05s asserted that a Windows runner acquires a free file lock
# within fifty milliseconds, which is not a property of this code. A background
# that did NOT yield still fails, because it holds until the hold above.
_MEDIA_FOREGROUND_ACQUIRE_SECONDS = 15.0


def test_background_media_hashing_yields_foreground_guard_then_commits_guarded(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import writer_lease

    binary = vault / "Knowledge Base" / "Evidence" / "Audio" / "large.m4a"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"large-enough-for-a-blocked-provenance-read")
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=vault.parent / "state"),
        mutation_timeout_seconds=_MEDIA_FOREGROUND_ACQUIRE_SECONDS,
    )
    monkeypatch.setattr(writer_lease, "get_manager", lambda: manager)
    hash_started = threading.Event()
    continue_hash = threading.Event()
    commit_seen = threading.Event()
    errors: list[BaseException] = []
    original_read = media_processing._read_provenance
    original_batch = media_processing.batch_atomic_write

    def blocked_read(*args, **kwargs):  # noqa: ANN002, ANN003
        hash_started.set()
        assert continue_hash.wait(_MEDIA_HOLD_SECONDS)
        return original_read(*args, **kwargs)

    def guarded_batch(*args, **kwargs):  # noqa: ANN002, ANN003
        boundary = manager.status(vault)["mutation_boundary"]
        assert boundary["state"] == "held"
        assert boundary["operation"] == "background_media_reconcile"
        commit_seen.set()
        return original_batch(*args, **kwargs)

    monkeypatch.setattr(media_processing, "_read_provenance", blocked_read)
    monkeypatch.setattr(media_processing, "batch_atomic_write", guarded_batch)

    def reconcile() -> None:
        try:
            file_watcher._reconcile_background_media(vault, binary)
        except BaseException as error:  # noqa: BLE001 - inspect thread outcome
            errors.append(error)

    background = threading.Thread(target=reconcile)
    background.start()
    assert hash_started.wait(_MEDIA_OBSERVE_SECONDS)
    with manager.mutation_guard(
        vault,
        request_id="req-foreground",
        operation="remember",
        holder_kind="command",
    ):
        assert manager.status(vault)["mutation_boundary"]["request_id"] == "req-foreground"
    continue_hash.set()
    # A deadlock valve. `is_alive()` below is the assertion -- that the
    # reconcile finished, having taken the boundary for its commit -- not a
    # claim that it finishes within three seconds of being released. A Windows
    # shard exceeded that while completing correctly.
    background.join(timeout=_MEDIA_HOLD_SECONDS)

    assert not background.is_alive()
    assert errors == []
    assert commit_seen.is_set()


def test_supported_audio_never_enters_markdown_freshness_or_embedding(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _spy_media_and_text_dispatch(monkeypatch)
    recording = vault / "Knowledge Base" / "Evidence" / "Audio" / "binary-only.wav"
    recording.parent.mkdir(parents=True, exist_ok=True)
    recording.write_bytes(b"binary audio must not be treated as markdown")
    watcher = file_watcher.FileWatcher(vault)

    watcher._record(recording, deleted=False)
    watcher._flush()

    assert calls["media"] == [(vault, recording, False)]
    assert calls["freshness"] == []
    assert calls["inbound"] == []
    assert calls["resolver"] == []
    assert calls["upsert"] == []
    assert calls["delete"] == []


def test_supported_audio_events_are_debounced_and_deduplicated(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _spy_media_and_text_dispatch(monkeypatch)
    recording = vault / "Knowledge Base" / "Evidence" / "Audio" / "burst.m4a"
    recording.parent.mkdir(parents=True, exist_ok=True)
    recording.write_bytes(b"one recording, several filesystem events")
    watcher = file_watcher.FileWatcher(vault, debounce_seconds=0.02)
    dispatch = threading.Thread(target=watcher._run_dispatch, daemon=True)
    dispatch.start()
    try:
        watcher._record(recording, deleted=False)
        watcher._record(recording, deleted=False)
        watcher._record(recording, deleted=False)
        deadline = time.monotonic() + 2.0
        while not calls["media"] and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        watcher._stop.set()
        watcher._wake.set()
        dispatch.join(timeout=2)

    assert calls["media"] == [(vault, recording, False)]
    assert calls["freshness"] == []
    assert calls["inbound"] == []
    assert calls["resolver"] == []
    assert calls["upsert"] == []


def test_unsupported_attachment_dispatches_neither_media_nor_text(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _spy_media_and_text_dispatch(monkeypatch)
    attachment = vault / "Knowledge Base" / "Evidence" / "payload.bin"
    attachment.parent.mkdir(parents=True, exist_ok=True)
    attachment.write_bytes(b"unsupported attachment")
    watcher = file_watcher.FileWatcher(vault)

    watcher._record(attachment, deleted=False)
    watcher._flush()

    assert calls == {
        "media": [],
        "freshness": [],
        "inbound": [],
        "resolver": [],
        "upsert": [],
        "delete": [],
    }


def test_delete_routes_to_delete_after_remove(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    ups, dels = _stub_embeddings(monkeypatch)
    w = file_watcher.FileWatcher(vault)
    gone = vault / "Knowledge Base" / "Notes" / "gone.md"
    w._record(gone, deleted=True)
    w._flush()
    assert ups == []
    assert dels == [["Knowledge Base/Notes/gone.md"]]


def test_modify_then_delete_only_deletes(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    ups, dels = _stub_embeddings(monkeypatch)
    w = file_watcher.FileWatcher(vault)
    p = vault / "Knowledge Base" / "Notes" / "x.md"
    w._record(p, deleted=False)
    w._record(p, deleted=True)  # deleted within the same window wins
    w._flush()
    assert ups == []
    assert dels == [["Knowledge Base/Notes/x.md"]]


def test_delete_then_recreate_only_upserts(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    ups, dels = _stub_embeddings(monkeypatch)
    w = file_watcher.FileWatcher(vault)
    p = vault / "Knowledge Base" / "Notes" / "y.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# Before\n", encoding="utf-8")
    p.unlink()
    w._record(p, deleted=True)
    p.write_text("# After\n", encoding="utf-8")
    w._record(p, deleted=False)  # recreated → modify
    w._flush()
    assert dels == []
    assert ups == [[p]]


# How long a test will wait for a background flush before calling it hung.
# Generous on purpose: see the note at its use site.
_FLUSH_VALVE_SECONDS = 30.0


def test_dispatch_thread_coalesces_within_debounce(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    ups, _dels = _stub_embeddings(monkeypatch)
    w = file_watcher.FileWatcher(vault, debounce_seconds=0.05)
    t = threading.Thread(target=w._run_dispatch, daemon=True)
    t.start()
    try:
        a = vault / "Knowledge Base" / "Notes" / "a.md"
        b = vault / "Knowledge Base" / "Notes" / "b.md"
        a.parent.mkdir(parents=True, exist_ok=True)
        a.write_text("# A\n", encoding="utf-8")
        b.write_text("# B\n", encoding="utf-8")
        w._record(a, deleted=False)
        w._record(b, deleted=False)
        # A deadlock valve, not a latency assertion. What is under test is that
        # the dispatch thread flushes at all and that the two saves coalesce
        # into one batch -- neither claim is about how quickly a CI runner gets
        # round to scheduling the thread. Two seconds was tight enough to fire
        # on a loaded Windows runner, reporting a coalescing defect that was
        # really a busy machine.
        deadline = time.monotonic() + _FLUSH_VALVE_SECONDS
        while not ups and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ups, "dispatch thread should flush after the debounce window"
        assert sorted(ups[0]) == sorted([a, b]), "rapid saves coalesce into one batch"
    finally:
        w._stop.set()
        w._wake.set()
        t.join(timeout=2)


def test_file_watcher_reads_policy_without_restart(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "quiet")
    w = file_watcher.FileWatcher(vault)

    assert w._debounce_seconds() == pytest.approx(2.0)
    assert w._reconcile_interval_seconds() == pytest.approx(900.0)

    monkeypatch.setenv("EXOMEM_MODE", "normal")

    assert w._debounce_seconds() == pytest.approx(0.5)
    assert w._reconcile_interval_seconds() == pytest.approx(300.0)


def test_live_import_burst_defers_semantic_indexing(
    vault, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[tuple[list[Path], dict]] = []
    monkeypatch.setattr(
        file_watcher.index_sync,
        "upsert_after_write",
        lambda root, paths, **kw: calls.append((list(paths), dict(kw))),
    )
    monkeypatch.setattr(
        file_watcher.mode,
        "watcher_policy",
        lambda: file_watcher.mode.WatcherPolicy(
            debounce_seconds=0.05,
            reconcile_interval_seconds=999.0,
            max_embed_files_per_batch=1,
            max_reconcile_embed_files=500,
            defer_expensive_indexes=False,
        ),
    )
    w = file_watcher.FileWatcher(vault)
    a = vault / "Knowledge Base" / "Notes" / "burst-a.md"
    b = vault / "Knowledge Base" / "Notes" / "burst-b.md"
    with caplog.at_level(logging.WARNING, logger="exomem.file_watcher"):
        w._record(a, deleted=False)
        w._record(b, deleted=False)
        w._flush()

    assert len(calls) == 1
    assert sorted(calls[0][0]) == sorted([a, b])
    assert calls[0][1] == {
        "defer_semantic": True,
        "publish_corpus_change": False,
    }
    assert "live import/sync burst" in caplog.text


def test_dispatch_thread_uses_quiet_policy_for_burst_coalescing(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[Path]] = []
    monkeypatch.setattr(
        file_watcher.index_sync,
        "upsert_after_write",
        lambda root, paths, **_kw: calls.append(list(paths)),
    )
    monkeypatch.setattr(
        file_watcher.mode,
        "watcher_policy",
        lambda: file_watcher.mode.WatcherPolicy(
            # Half a second, not 50 ms. What is under test is that two records
            # inside one debounce window coalesce into a single dispatch, and
            # the 10 ms gap below only has to land inside that window -- but a
            # 5x margin is a coin flip on a runner executing four shards at
            # once, where macOS CI dispatched `quiet-a` alone before `quiet-b`
            # was ever recorded. The wait below already bounds the test at 2 s.
            debounce_seconds=0.5,
            reconcile_interval_seconds=999.0,
            max_embed_files_per_batch=0,
            max_reconcile_embed_files=0,
            defer_expensive_indexes=True,
        ),
    )
    w = file_watcher.FileWatcher(vault)
    t = threading.Thread(target=w._run_dispatch, daemon=True)
    t.start()
    try:
        a = vault / "Knowledge Base" / "Notes" / "quiet-a.md"
        b = vault / "Knowledge Base" / "Notes" / "quiet-b.md"
        w._record(a, deleted=False)
        time.sleep(0.01)
        w._record(b, deleted=False)
        deadline = time.monotonic() + 5.0
        while not calls and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(calls) == 1
        assert sorted(calls[0]) == sorted([a, b])
    finally:
        w._stop.set()
        w._wake.set()
        t.join(timeout=2)


def test_start_soft_fails_when_watchdog_missing(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom():
        raise ImportError("No module named 'watchdog'")

    monkeypatch.setattr(file_watcher, "_import_watchdog", _boom)
    w = file_watcher.FileWatcher(vault)
    assert w.start() is False  # no-op, server keeps running
    assert w._thread is None and w._observer is None


def test_start_no_op_when_kb_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # watchdog "available" but no Knowledge Base/ dir → don't watch.
    monkeypatch.setattr(file_watcher, "_import_watchdog", lambda: (object, object))
    w = file_watcher.FileWatcher(tmp_path)
    assert w.start() is False


def test_file_watcher_dispatch_thread_restarts_after_stop(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hosted quiesce/resume reuses one watcher object; restart must be real."""

    class Handler:
        pass

    class Observer:
        def schedule(self, *_args, **_kwargs) -> None:
            pass

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def join(self, timeout: float | None = None) -> None:
            pass

    monkeypatch.setattr(file_watcher, "_import_watchdog", lambda: (Observer, Handler))
    monkeypatch.setattr(file_watcher.freshness, "event_indexes_enabled", lambda: False)
    watcher = file_watcher.FileWatcher(vault, debounce_seconds=0.01)

    assert watcher.start() is True
    watcher.stop()
    assert watcher.start() is True
    try:
        assert watcher._thread is not None
        time.sleep(0.02)
        assert watcher._thread.is_alive(), "resumed dispatch thread exited on the stale stop event"
    finally:
        watcher.stop()


# ---- Self-write suppression (OpenSpec: improve-find-latency-token-cost) ----


def test_self_write_upsert_suppressed(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    ups, dels = _stub_embeddings(monkeypatch)
    file_watcher.clear_self_write_registry()
    w = file_watcher.FileWatcher(vault)
    p = vault / "Knowledge Base" / "Notes" / "self-write.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# self write\n", encoding="utf-8")
    file_watcher.register_self_write(vault, [p])
    w._record(p, deleted=False)
    w._flush()
    assert ups == [] and dels == []


def test_self_write_publishes_semantic_corpus_delta(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[Path], list[str]]] = []
    monkeypatch.setattr(file_watcher.freshness, "event_indexes_enabled", lambda: True)
    monkeypatch.setattr(
        "exomem.semantic_contract.publish_corpus_files_changed_classified",
        # `list.append` returns None, i.e. "published, no failure".
        lambda root, *, changed=(), deleted=(): calls.append(
            (list(changed), [str(path) for path in deleted])
        ),
    )
    path = vault / "Knowledge Base" / "Notes" / "semantic-cache-write.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# cache patch\n", encoding="utf-8")

    file_watcher.register_self_write(vault, [path])

    assert calls == [([path], [])]


def test_external_batch_publishes_semantic_corpus_delta(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_embeddings(monkeypatch)
    calls: list[tuple[list[Path], list[str]]] = []
    monkeypatch.setattr(
        "exomem.semantic_contract.publish_corpus_files_changed_classified",
        # `list.append` returns None, i.e. "published, no failure".
        lambda root, *, changed=(), deleted=(): calls.append(
            (list(changed), [str(path) for path in deleted])
        ),
    )
    watcher = file_watcher.FileWatcher(vault)
    path = vault / "Knowledge Base" / "Notes" / "semantic-cache-external.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# external cache patch\n", encoding="utf-8")
    watcher._record(path, deleted=False)

    watcher._flush()

    assert calls == [([path], [])]


def test_external_batch_retries_the_complete_vault_delta_before_fanout(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    freshness.rebaseline(vault)
    source = vault / "Sources" / "target.md"
    kb_note = vault / "Knowledge Base" / "Notes" / "changed.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    kb_note.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("---\ntitle: Old target\n---\n# Old target\n", encoding="utf-8")
    kb_note.write_text("# Before\n", encoding="utf-8")
    freshness.rebaseline(vault)
    find_module._get_query_resolver(vault)
    source.write_text("---\ntitle: New target\n---\n# New target\n", encoding="utf-8")
    kb_note.write_text("# After\n", encoding="utf-8")

    real_publish = semantic_contract.publish_corpus_files_changed_classified
    calls: list[tuple[list[Path], list[str]]] = []

    def flaky_publish(root, *, changed=(), deleted=()):
        calls.append((list(changed), [str(path) for path in deleted]))
        if len(calls) == 1:
            raise RuntimeError("transient publication failure")
        return real_publish(root, changed=changed, deleted=deleted)

    upserts: list[tuple[list[Path], dict]] = []
    monkeypatch.setattr(
        semantic_contract, "publish_corpus_files_changed_classified", flaky_publish
    )
    monkeypatch.setattr(
        file_watcher.index_sync,
        "upsert_after_write",
        lambda _root, paths, **kwargs: upserts.append((list(paths), dict(kwargs))),
    )
    watcher = file_watcher.FileWatcher(vault)
    watcher._record(source, deleted=False)
    watcher._record(kb_note, deleted=False)

    watcher._flush()

    assert len(calls) == 2
    assert all(sorted(changed) == sorted([source, kb_note]) for changed, _ in calls)
    assert upserts == [([kb_note], {"defer_semantic": False, "publish_corpus_change": False})]
    resolver = find_module.writer_resolver_snapshot(vault)
    resolved, warning = vault_module.normalize_wikilink(
        "New target", vault, resolver=resolver, strict=False
    )
    assert warning is None
    assert resolved == "Sources/target"


def test_persistent_non_kb_publication_failure_withdraws_stale_resolver(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = vault / "Sources" / "target.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("---\ntitle: Old target\n---\n# Old target\n", encoding="utf-8")
    freshness.rebaseline(vault)
    find_module._get_query_resolver(vault)
    source.write_text("---\ntitle: New target\n---\n# New target\n", encoding="utf-8")
    monkeypatch.setattr(
        semantic_contract,
        "publish_corpus_files_changed_classified",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("persistent failure")),
    )
    watcher = file_watcher.FileWatcher(vault)
    watcher._record(source, deleted=False)

    watcher._flush()

    assert freshness.recall_is_live(vault, "vault") is False
    resolver = find_module.writer_resolver_snapshot(vault)
    resolved, warning = vault_module.normalize_wikilink(
        "New target", vault, resolver=resolver, strict=False
    )
    assert warning is None
    assert resolved == "Sources/target"


def test_external_edit_after_self_write_dispatches(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    ups, _dels = _stub_embeddings(monkeypatch)
    file_watcher.clear_self_write_registry()
    w = file_watcher.FileWatcher(vault)
    p = vault / "Knowledge Base" / "Notes" / "self-then-external.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# self write\n", encoding="utf-8")
    file_watcher.register_self_write(vault, [p])
    # A later EXTERNAL edit changes the file signature — must dispatch.
    p.write_text("# self write\n\nexternally edited, longer now\n", encoding="utf-8")
    w._record(p, deleted=False)
    w._flush()
    assert ups and p in ups[0]


def test_upsert_suppression_expires(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    ups, _dels = _stub_embeddings(monkeypatch)
    file_watcher.clear_self_write_registry()
    monkeypatch.setattr(file_watcher, "UPSERT_SUPPRESS_TTL_SECONDS", -1.0)
    w = file_watcher.FileWatcher(vault)
    p = vault / "Knowledge Base" / "Notes" / "expired-suppression.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# expired\n", encoding="utf-8")
    file_watcher.register_self_write(vault, [p])
    w._record(p, deleted=False)
    w._flush()
    assert ups and p in ups[0]


def test_self_delete_suppressed(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    ups, dels = _stub_embeddings(monkeypatch)
    file_watcher.clear_self_write_registry()
    w = file_watcher.FileWatcher(vault)
    rel = "Knowledge Base/Notes/self-deleted.md"
    file_watcher.register_self_delete(vault, [rel])
    w._record(vault / rel, deleted=True)
    w._flush()
    assert ups == [] and dels == []


def test_delete_suppression_expires(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    _ups, dels = _stub_embeddings(monkeypatch)
    file_watcher.clear_self_write_registry()
    monkeypatch.setattr(file_watcher, "DELETE_SUPPRESS_TTL_SECONDS", -1.0)
    w = file_watcher.FileWatcher(vault)
    rel = "Knowledge Base/Notes/expired-delete.md"
    file_watcher.register_self_delete(vault, [rel])
    w._record(vault / rel, deleted=True)
    w._flush()
    assert dels == [[rel]]


def test_unregistered_external_events_still_dispatch(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    ups, dels = _stub_embeddings(monkeypatch)
    file_watcher.clear_self_write_registry()
    w = file_watcher.FileWatcher(vault)
    p = vault / "Knowledge Base" / "Notes" / "external-edit.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# external\n", encoding="utf-8")
    w._record(p, deleted=False)
    gone = vault / "Knowledge Base" / "Notes" / "external-gone.md"
    w._record(gone, deleted=True)
    w._flush()
    assert ups and p in ups[0]
    assert dels and "Knowledge Base/Notes/external-gone.md" in dels[0]


def test_batch_atomic_write_registers_suppression(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem.vault import PlannedWrite, batch_atomic_write

    ups, _dels = _stub_embeddings(monkeypatch)
    file_watcher.clear_self_write_registry()
    w = file_watcher.FileWatcher(vault)
    p = vault / "Knowledge Base" / "Notes" / "batch-written.md"
    batch_atomic_write([PlannedWrite(path=p, content="# batch\n")], vault_root=vault)
    ups.clear()  # the writer's own (stubbed) upsert — not the echo under test
    w._record(p, deleted=False)
    w._flush()
    assert ups == []


# ---- Reconcile drift dispatch through the event fan-out (PR1) ----------------
#
# The 300s reconcile only re-derives the freshness map from a fresh walk. When a
# watchdog event is missed, that drift used to force every triple-keyed derived
# index (resolver, bm25, keyword) to rebuild lazily on the NEXT query — a
# multi-second first-query-after-drift stall — and never re-embedded the missed
# files (a recall gap). PR1 makes reconcile return the drift delta and the
# watcher dispatch it through the SAME fan-out a live batch uses, off the query
# path. We drive drift by writing/utime WITHOUT _record (a missed event), then
# call _reconcile_once directly (the pattern in test_freshness_registry.py:215).


def _spy_reconcile_fanout(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Capture the reconcile dispatch seams: inbound + resolver publishes, the
    KB-filtered embed heal (index_sync), and the bm25 pre-warm."""
    calls: dict[str, list] = {
        "inbound": [],
        "resolver": [],
        "upsert": [],
        "delete": [],
        "warm": [],
    }
    monkeypatch.setattr(
        "exomem.vault.on_inbound_files_changed",
        lambda root, up, dl: calls["inbound"].append((list(up), list(dl))),
    )
    monkeypatch.setattr(
        "exomem.find.on_resolver_files_changed",
        lambda root, up, dl: calls["resolver"].append((list(up), list(dl))),
    )
    monkeypatch.setattr(
        file_watcher.index_sync,
        "upsert_after_write",
        lambda root, paths, **_kw: calls["upsert"].append(list(paths)),
    )
    monkeypatch.setattr(
        file_watcher.index_sync,
        "delete_after_remove",
        lambda root, rels, **_kw: calls["delete"].append(list(rels)),
    )
    monkeypatch.setattr("exomem.bm25.warm", lambda root, scope: calls["warm"].append(scope))
    return calls


def _vault_rel(vault: Path, path: Path) -> str:
    return path.resolve().relative_to(vault.resolve()).as_posix()


def test_reconcile_dispatches_drift_delta_to_fanout(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    file_watcher.clear_self_write_registry()
    calls = _spy_reconcile_fanout(monkeypatch)
    w = file_watcher.FileWatcher(vault)
    w._reconcile_once(seed=True)

    target = next(find_module._walk_md(vault / "Knowledge Base"))
    future = time.time() + 10_000
    os.utime(target, (future, future))  # a missed watchdog event

    w._reconcile_once(seed=False)

    rel = _vault_rel(vault, target)
    # The exact delta reaches inbound + resolver once, no phantom deletes.
    assert calls["inbound"] == [([rel], [])]
    assert calls["resolver"] == [([rel], [])]
    # The KB file is handed to the embed/lexical heal exactly once (deduped
    # across the kb + vault scopes).
    assert len(calls["upsert"]) == 1
    assert [p.resolve() for p in calls["upsert"][0]] == [target.resolve()]
    assert calls["delete"] == []
    # bm25 corpus pre-warmed for both scopes off the query path.
    assert calls["warm"] == ["kb", "vault"]


def test_quiet_reconcile_defers_embedding_and_skips_bm25_warm(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_MODE", "quiet")
    file_watcher.clear_self_write_registry()
    calls = _spy_reconcile_fanout(monkeypatch)
    w = file_watcher.FileWatcher(vault)
    w._reconcile_once(seed=True)

    target = next(find_module._walk_md(vault / "Knowledge Base"))
    future = time.time() + 10_000
    os.utime(target, (future, future))

    w._reconcile_once(seed=False)

    assert len(calls["upsert"]) == 1
    assert [p.resolve() for p in calls["upsert"][0]] == [target.resolve()]
    assert calls["warm"] == []


def test_reconcile_seed_dispatches_nothing(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    """Boot seed is NOT drift — it must not re-embed the whole vault."""
    file_watcher.clear_self_write_registry()
    calls = _spy_reconcile_fanout(monkeypatch)
    w = file_watcher.FileWatcher(vault)

    w._reconcile_once(seed=True)

    assert calls == {"inbound": [], "resolver": [], "upsert": [], "delete": [], "warm": []}


def test_reconcile_no_drift_dispatches_nothing(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    file_watcher.clear_self_write_registry()
    calls = _spy_reconcile_fanout(monkeypatch)
    w = file_watcher.FileWatcher(vault)
    w._reconcile_once(seed=True)

    w._reconcile_once(seed=False)  # nothing changed on disk since the seed

    assert calls == {"inbound": [], "resolver": [], "upsert": [], "delete": [], "warm": []}


def test_missing_baseline_and_post_reconcile_watcher_do_not_phantom_fanout(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_watcher.clear_self_write_registry()
    freshness.clear()
    index_sync = file_watcher.index_sync
    index_sync.clear_deferred_work(vault)
    calls = _spy_reconcile_fanout(monkeypatch)

    def complete_upsert(root: Path, paths: list[Path], **_kwargs):  # noqa: ANN001
        calls["upsert"].append(list(paths))
        rels = tuple(path.relative_to(root).as_posix() for path in paths)
        return file_watcher.index_sync.IndexSyncReport(
            "upsert",
            rels,
            rels,
            tuple(
                file_watcher.index_sync.IndexComponentOutcome(
                    component,
                    "not_required" if component == "epistemic_graph" else "completed",
                    "not_required" if component == "epistemic_graph" else "completed",
                )
                for component in (
                    "memory_refs",
                    "resolver",
                    "semantic_purge",
                    "lexstore",
                    "epistemic_graph",
                    "embeddings",
                )
            ),
        )

    monkeypatch.setattr(file_watcher.index_sync, "upsert_after_write", complete_upsert)
    watcher = file_watcher.FileWatcher(vault)

    watcher._reconcile_once(seed=False)

    assert calls == {
        "inbound": [],
        "resolver": [],
        "upsert": [],
        "delete": [],
        "warm": [],
    }
    reconcile_module.reconcile(vault)
    for recorded in calls.values():
        recorded.clear()
    watcher._reconcile_once(seed=False)
    assert calls == {
        "inbound": [],
        "resolver": [],
        "upsert": [],
        "delete": [],
        "warm": [],
    }
    assert deferred_index.status(vault)["count"] == 0

    target = next(find_module._walk_md(vault / "Knowledge Base"))
    target.write_text(
        target.read_text(encoding="utf-8") + "\nreal watcher change\n",
        encoding="utf-8",
    )
    watcher._reconcile_once(seed=False)

    assert len(calls["upsert"]) == 1
    assert [path.resolve() for path in calls["upsert"][0]] == [target.resolve()]
    assert calls["delete"] == []

    for recorded in calls.values():
        recorded.clear()
    rel = _vault_rel(vault, target)
    target.unlink()
    watcher._reconcile_once(seed=False)

    assert calls["upsert"] == []
    assert calls["delete"] == [[rel]]
    assert calls["inbound"] == [([], [rel])]
    assert calls["resolver"] == [([], [rel])]


def test_periodic_reconcile_discovers_missed_media_without_text_reembed(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    discovery_calls: list[tuple[Path, int]] = []

    def reconcile_all_media(root: Path, *, limit: int, reconcile_one=None) -> None:
        discovery_calls.append((root, limit))

    monkeypatch.setattr(
        media_processing,
        "reconcile_all_media",
        reconcile_all_media,
        raising=False,
    )
    calls = _spy_reconcile_fanout(monkeypatch)
    watcher = file_watcher.FileWatcher(vault)
    watcher._reconcile_once(seed=True)
    discovery_calls.clear()

    recording = vault / "Knowledge Base" / "Evidence" / "Audio" / "missed.m4a"
    recording.parent.mkdir(parents=True, exist_ok=True)
    recording.write_bytes(b"event missed while watcher was disconnected")
    watcher._reconcile_once(seed=False)

    assert len(discovery_calls) == 1
    root, limit = discovery_calls[0]
    assert root == vault
    assert isinstance(limit, int) and limit > 0
    assert calls["inbound"] == []
    assert calls["resolver"] == []
    assert calls["upsert"] == []
    assert calls["delete"] == []


def test_periodic_media_reconcile_runs_under_writer_authority(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    from exomem import writer_lease

    depth = 0

    class Manager:
        @contextmanager
        def mutation_guard(self, root: Path, **metadata):
            nonlocal depth
            assert root == vault
            assert metadata["operation"] == "background_media_reconcile"
            assert metadata["holder_kind"] == "background"
            depth += 1
            try:
                yield
            finally:
                depth -= 1

    monkeypatch.setattr(writer_lease, "get_manager", lambda: Manager())

    def reconcile_all_media(_root: Path, *, limit: int, reconcile_one=None) -> int:
        assert limit > 0
        assert depth == 0
        assert reconcile_one is not None
        reconcile_one(vault / "periodic.m4a")
        assert depth == 0
        return 1

    monkeypatch.setattr(media_processing, "reconcile_all_media", reconcile_all_media)

    def reconcile_media(*_args, commit_guard=None, **_kwargs):  # noqa: ANN001
        assert depth == 0
        assert commit_guard is not None
        with commit_guard():
            assert depth == 1

    monkeypatch.setattr(media_processing, "reconcile_media", reconcile_media)
    calls = _spy_reconcile_fanout(monkeypatch)
    watcher = file_watcher.FileWatcher(vault)
    watcher._reconcile_once(seed=True)
    watcher._reconcile_once(seed=False)

    assert depth == 0
    assert calls["upsert"] == []


def test_periodic_media_reconcile_yields_mutation_boundary_between_artifacts(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    from exomem import writer_lease

    depth = 0
    guard_entries = 0

    class Manager:
        @contextmanager
        def mutation_guard(self, root: Path, **metadata):
            nonlocal depth, guard_entries
            assert root == vault
            assert metadata["operation"] == "background_media_reconcile"
            assert metadata["holder_kind"] == "background"
            guard_entries += 1
            depth += 1
            try:
                yield
            finally:
                depth -= 1

    monkeypatch.setattr(writer_lease, "get_manager", lambda: Manager())

    def reconcile_all_media(root: Path, *, limit: int, reconcile_one=None) -> int:
        assert root == vault
        assert limit > 0
        assert reconcile_one is not None
        for index in range(3):
            assert depth == 0
            reconcile_one(root / f"artifact-{index}.m4a")
            assert depth == 0
        return 3

    monkeypatch.setattr(media_processing, "reconcile_all_media", reconcile_all_media)

    def reconcile_media(*_args, commit_guard=None, **_kwargs):  # noqa: ANN001
        assert depth == 0
        assert commit_guard is not None
        with commit_guard():
            assert depth == 1

    monkeypatch.setattr(media_processing, "reconcile_media", reconcile_media)
    watcher = file_watcher.FileWatcher(vault)
    watcher._reconcile_once(seed=False)

    assert depth == 0
    assert guard_entries == 3


def test_reconcile_delete_routes_to_delete_after_remove(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_watcher.clear_self_write_registry()
    calls = _spy_reconcile_fanout(monkeypatch)
    w = file_watcher.FileWatcher(vault)
    w._reconcile_once(seed=True)

    target = next(find_module._walk_md(vault / "Knowledge Base"))
    rel = _vault_rel(vault, target)
    target.unlink()  # a missed delete event

    w._reconcile_once(seed=False)

    assert calls["delete"] == [[rel]]
    assert calls["upsert"] == []
    assert calls["resolver"] == [([], [rel])]
    assert calls["inbound"] == [([], [rel])]


def test_reconcile_reembeds_missed_kb_file(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    """The recall-gap fix: a missed KB edit reaches embeddings.upsert_after_write
    through the real index_sync seam (stubbed embedder, as in _stub_embeddings)."""
    file_watcher.clear_self_write_registry()
    ups, dels = _stub_embeddings(monkeypatch)
    monkeypatch.setattr("exomem.bm25.warm", lambda root, scope: None)
    w = file_watcher.FileWatcher(vault)
    w._reconcile_once(seed=True)

    target = next(find_module._walk_md(vault / "Knowledge Base"))
    future = time.time() + 10_000
    os.utime(target, (future, future))

    w._reconcile_once(seed=False)

    embedded = [p for batch in ups for p in batch]
    assert any(p.resolve() == target.resolve() for p in embedded)
    assert dels == []


def test_reconcile_drift_evicts_resolver_without_checkpoint_leapfrog(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missed event has no complete retained delta, so reconcile evicts the
    cached resolver instead of stamping it at an unprovable newer checkpoint."""
    file_watcher.clear_self_write_registry()
    _stub_embeddings(monkeypatch)  # keep torch out; lexstore/resolver run for real
    monkeypatch.setattr("exomem.bm25.warm", lambda root, scope: None)
    w = file_watcher.FileWatcher(vault)
    w._reconcile_once(seed=True)

    # Prime the process-shared resolver at the current freshness triple.
    r1 = find_module._get_query_resolver(vault)

    # `index` is duplicated below Knowledge Base/; bare-link normalization
    # deterministically promotes the KB-root path before stem matching.
    target = vault / "Knowledge Base" / "index.md"
    future = time.time() + 10_000
    os.utime(target, (future, future))

    w._reconcile_once(seed=False)

    r2 = find_module._get_query_resolver(vault)
    assert r2 is not r1
    resolved, warning = vault_module.normalize_wikilink(
        target.stem, vault, resolver=r2, strict=False
    )
    assert warning is None
    assert resolved == target.relative_to(vault).as_posix().removesuffix(".md")


def test_reconcile_dispatch_suppresses_registered_self_write(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path that was a registered self-write (already fanned out by the writer)
    must not be re-dispatched by reconcile drift."""
    file_watcher.clear_self_write_registry()
    w = file_watcher.FileWatcher(vault)
    p = vault / "Knowledge Base" / "Notes" / "reconcile-self-write.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# self\n", encoding="utf-8")
    file_watcher.register_self_write(vault, [p])  # matching signature registered

    # Spy AFTER register_self_write so the writer's own publish isn't counted.
    ups, dels = _stub_embeddings(monkeypatch)
    inbound: list = []
    resolver: list = []
    monkeypatch.setattr(
        "exomem.vault.on_inbound_files_changed",
        lambda root, up, dl: inbound.append((list(up), list(dl))),
    )
    monkeypatch.setattr(
        "exomem.find.on_resolver_files_changed",
        lambda root, up, dl: resolver.append((list(up), list(dl))),
    )

    w._dispatch_reconcile_delta([str(p)], [])

    assert ups == [] and dels == []
    assert inbound == [] and resolver == []


def test_reconcile_delta_conflict_existing_file_routes_as_changed_only(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kb/vault scope walks are two separate, non-atomic snapshots — a file
    deleted+recreated between them can appear in BOTH the changed and deleted
    delta lists in the same cycle. Split-brain must resolve by trusting the
    filesystem now: a path that exists is dispatched as changed ONLY, never
    also as a delete (a delete-after-upsert would strip a live file's index
    rows until the next drift cycle re-surfaces it)."""
    file_watcher.clear_self_write_registry()
    ups, dels = _stub_embeddings(monkeypatch)
    w = file_watcher.FileWatcher(vault)
    p = vault / "Knowledge Base" / "Notes" / "split-brain-exists.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# split brain\n", encoding="utf-8")

    w._dispatch_reconcile_delta([str(p)], [str(p)])

    assert len(ups) == 1 and ups[0] == [p]
    assert dels == []


def test_reconcile_delta_conflict_missing_file_routes_as_deleted_only(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirror case: a path present in both lists that is ABSENT on disk must
    dispatch as a delete only, never also as an upsert."""
    file_watcher.clear_self_write_registry()
    ups, dels = _stub_embeddings(monkeypatch)
    w = file_watcher.FileWatcher(vault)
    p = vault / "Knowledge Base" / "Notes" / "split-brain-gone.md"
    # Deliberately never created — absent on disk.

    w._dispatch_reconcile_delta([str(p)], [str(p)])

    assert ups == []
    assert dels == [["Knowledge Base/Notes/split-brain-gone.md"]]


# ---- Rel-level dispatch guard for dual-form path collapse (#126) ----------
#
# The abs-string guard above only catches a conflict when both event forms are
# the literal SAME string. Two DIFFERENT abs-path forms of one file (e.g. a
# Windows 8.3 short name vs. the long form) evade it but can still collapse to
# the SAME rel once `_rel()` resolves them. We drive that collapse platform-
# free by monkeypatching `_rel` so a distinct "alias" string resolves to the
# same rel a real path would — modeling the 8.3 short-name vector without
# depending on it actually being enabled on the test box.


def test_reconcile_delta_dual_form_collapse_existing_file_routes_as_changed_only(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two different abs-path STRING forms of the SAME on-disk file (e.g. a
    Windows 8.3 short name vs. the long form, #126) don't collide in the
    abs-string guard (different strings) but collapse to the identical rel
    once `_rel()` resolves them. That collapse must still route the live file
    as changed only — never also as a delete that would strip its index
    rows."""
    file_watcher.clear_self_write_registry()
    ups, dels = _stub_embeddings(monkeypatch)
    w = file_watcher.FileWatcher(vault)
    p = vault / "Knowledge Base" / "Notes" / "dual-form-exists.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# dual form\n", encoding="utf-8")

    long_form = str(p)
    alias_form = long_form + ".8dot3-alias"  # a distinct string; never touches disk
    rel = _vault_rel(vault, p)
    real_rel = w._rel

    def fake_rel(path: Path):
        if str(path) == alias_form:
            return rel
        return real_rel(path)

    monkeypatch.setattr(w, "_rel", fake_rel)

    w._dispatch_reconcile_delta([long_form], [alias_form])

    assert len(ups) == 1 and ups[0] == [p]
    assert dels == []


def test_reconcile_delta_dual_form_collapse_missing_file_routes_as_deleted_only(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirror case: the dual-form collapse resolves to a rel whose file is
    genuinely ABSENT — must dispatch as a delete only, never also as an
    upsert."""
    file_watcher.clear_self_write_registry()
    ups, dels = _stub_embeddings(monkeypatch)
    w = file_watcher.FileWatcher(vault)
    p = vault / "Knowledge Base" / "Notes" / "dual-form-gone.md"
    # Deliberately never created — absent on disk.

    long_form = str(p)
    alias_form = long_form + ".8dot3-alias"
    rel = "Knowledge Base/Notes/dual-form-gone.md"
    real_rel = w._rel

    def fake_rel(path: Path):
        if str(path) in (long_form, alias_form):
            return rel
        return real_rel(path)

    monkeypatch.setattr(w, "_rel", fake_rel)

    w._dispatch_reconcile_delta([long_form], [alias_form])

    assert ups == []
    assert dels == [[rel]]
