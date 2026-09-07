"""Publication-intent custody at the canonical writer/watcher seam."""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

import pytest

from exomem import file_watcher, freshness
from exomem import vault as vault_module


def _register_active_intent(vault: Path, path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    descriptor = os.open(path, os.O_RDONLY)
    try:
        intents = file_watcher.register_publication_intents(
            vault, [(path, descriptor, hashlib.sha256(content).hexdigest())]
        )
    finally:
        os.close(descriptor)
    assert len(intents) == 1
    return intents[0]


def test_published_event_is_held_until_postcommit_registration(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watcher may observe a canonical replacement before fan-out registers it."""
    target = vault / "Knowledge Base" / "Notes" / "published.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old", encoding="utf-8")
    watcher = file_watcher.FileWatcher(vault)
    fanout: list[list[Path]] = []
    real_published = vault_module._after_batch_destination_published

    def observe_published(path: Path) -> None:
        real_published(path)
        watcher._record(path, deleted=False)
        assert freshness.external_pending(vault) is False

    monkeypatch.setattr(vault_module, "_after_batch_destination_published", observe_published)
    monkeypatch.setattr(file_watcher, "_publish_registry_change", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        "exomem.index_sync.upsert_after_write",
        lambda _root, paths, **_kwargs: fanout.append(list(paths)),
    )

    vault_module.batch_atomic_write([vault_module.PlannedWrite(target, "new")], vault_root=vault)

    watcher._flush()

    assert fanout == [[target]]
    assert watcher._drain() == ([], [], [], 0, False)
    assert freshness.external_pending(vault) is False


def test_snapshot_timestamp_echo_holds_before_image_but_not_later_restoration(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = vault / "Knowledge Base" / "Notes" / "snapshot-timestamp.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old", encoding="utf-8")
    watcher = file_watcher.FileWatcher(vault)
    real_restore = vault_module._restore_bound_source_timestamps

    def restore_and_observe(source, descriptor, atime_ns, mtime_ns):  # noqa: ANN001
        real_restore(source, descriptor, atime_ns, mtime_ns)
        watcher._record(source.path, deleted=False)

    monkeypatch.setattr(
        vault_module, "_restore_bound_source_timestamps", restore_and_observe
    )
    monkeypatch.setattr("exomem.index_sync.upsert_after_write", lambda *_args, **_kwargs: None)

    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(target, "new")], vault_root=vault
    )

    assert freshness.external_pending(vault) is False
    target.write_text("old", encoding="utf-8")
    watcher._record(target, deleted=False)

    assert freshness.external_pending(vault) is True
    assert target in watcher._drain()[1]


def test_delayed_before_proof_after_success_restoration_is_external(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An active observer is not proof that it read BEFORE during preparation."""
    target = vault / "Knowledge Base" / "Notes" / "delayed-before-proof.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"old")
    staged = vault / "staged.md"
    staged.write_bytes(b"new")
    descriptor = os.open(staged, os.O_RDONLY)
    try:
        (intent,) = file_watcher.register_publication_intents(
            vault,
            [
                (
                    target,
                    descriptor,
                    hashlib.sha256(b"new").hexdigest(),
                    hashlib.sha256(b"old").hexdigest(),
                    len(b"old"),
                )
            ],
        )
    finally:
        os.close(descriptor)
    watcher = file_watcher.FileWatcher(vault)
    entered = threading.Event()
    release = threading.Event()
    real_digest = file_watcher._bounded_descriptor_digest

    def delayed_digest(path: Path, size: int | None):
        if threading.current_thread().name == "observed-event":
            entered.set()
            assert release.wait(timeout=2.0)
        return real_digest(path, size)

    monkeypatch.setattr(file_watcher, "_bounded_descriptor_digest", delayed_digest)
    monkeypatch.setattr(file_watcher, "_publish_registry_change", lambda *_args, **_kwargs: True)
    event = threading.Thread(
        name="observed-event", target=lambda: watcher._record(target, deleted=False)
    )
    event.start()
    assert entered.wait(timeout=2.0)

    file_watcher.begin_publication_installation([intent])
    target.write_bytes(b"new")
    file_watcher.mark_publication_installed([intent])
    registered = file_watcher.register_self_write(vault, [target])
    file_watcher.finalize_publication_intents([intent], succeeded=registered)
    target.write_bytes(b"old")
    release.set()
    event.join(timeout=2.0)

    assert not event.is_alive()
    assert freshness.external_pending(vault) is True
    assert target in watcher._drain()[1]


def test_late_after_proof_remains_held_through_installation(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = vault / "Knowledge Base" / "Notes" / "late-after-proof.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"old")
    staged = vault / "staged.md"
    staged.write_bytes(b"new")
    descriptor = os.open(staged, os.O_RDONLY)
    try:
        (intent,) = file_watcher.register_publication_intents(
            vault,
            [
                (
                    target,
                    descriptor,
                    hashlib.sha256(b"new").hexdigest(),
                    hashlib.sha256(b"old").hexdigest(),
                    len(b"old"),
                )
            ],
        )
    finally:
        os.close(descriptor)
    watcher = file_watcher.FileWatcher(vault)
    entered = threading.Event()
    release = threading.Event()
    real_digest = file_watcher._bounded_descriptor_digest
    old_proof = real_digest(target, None)
    assert old_proof is not None
    calls = 0

    def delayed_digest(path: Path, size: int | None):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=2.0)
        if calls == 1:
            return old_proof
        return real_digest(path, size)

    monkeypatch.setattr(file_watcher, "_bounded_descriptor_digest", delayed_digest)
    event = threading.Thread(target=lambda: watcher._record(target, deleted=False))
    event.start()
    assert entered.wait(timeout=2.0)
    file_watcher.begin_publication_installation([intent])
    target.write_bytes(b"new")
    file_watcher.mark_publication_installed([intent])
    release.set()
    event.join(timeout=2.0)

    assert not event.is_alive()
    assert calls == 2
    assert freshness.external_pending(vault) is False
    assert watcher._pending_publication_intents[target] is intent


@pytest.mark.skipif(os.name == "nt", reason="requires replacement of an open POSIX inode")
@pytest.mark.parametrize("replacement, external", [(b"new", False), (b"foreign", True)])
def test_replaced_open_descriptor_reproves_current_after_image(
    vault: Path, monkeypatch: pytest.MonkeyPatch, replacement: bytes, external: bool
) -> None:
    target = vault / "Knowledge Base" / "Notes" / "replaced-descriptor.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"old")
    staged = vault / "staged.md"
    staged.write_bytes(b"new")
    descriptor = os.open(staged, os.O_RDONLY)
    try:
        (intent,) = file_watcher.register_publication_intents(
            vault,
            [(target, descriptor, hashlib.sha256(b"new").hexdigest(),
              hashlib.sha256(b"old").hexdigest(), len(b"old"))],
        )
    finally:
        os.close(descriptor)
    watcher = file_watcher.FileWatcher(vault)
    entered = threading.Event()
    release = threading.Event()
    real_read = os.read
    reads = 0

    def delayed_read(fd: int, size: int) -> bytes:
        nonlocal reads
        if threading.current_thread().name == "replaced-descriptor-event":
            reads += 1
            if reads == 1:
                entered.set()
                assert release.wait(timeout=5.0)
        return real_read(fd, size)

    monkeypatch.setattr(os, "read", delayed_read)
    event = threading.Thread(
        name="replaced-descriptor-event", target=lambda: watcher._record(target, deleted=False)
    )
    event.start()
    try:
        assert entered.wait(timeout=5.0)
        file_watcher.begin_publication_installation([intent])
        staged.write_bytes(replacement)
        os.replace(staged, target)
        file_watcher.mark_publication_installed([intent])
    finally:
        release.set()
        event.join(timeout=5.0)

    assert not event.is_alive()
    assert reads == 2
    assert freshness.external_pending(vault) is external
    if not external:
        assert watcher._pending_publication_intents[target] is intent


def test_registry_publication_restoration_falls_back_and_aborts_batch(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = vault / "Knowledge Base" / "Notes" / "registry-first.md"
    second = vault / "Knowledge Base" / "Notes" / "registry-second.md"
    first_intent = _register_active_intent(vault, first, b"first-new")
    second_intent = _register_active_intent(vault, second, b"second-new")
    file_watcher.begin_publication_installation([first_intent, second_intent])
    file_watcher.mark_publication_installed([first_intent, second_intent])
    fanout: dict[str, object] = {}

    def publish_then_restore(*_args, **_kwargs) -> bool:
        first.write_bytes(b"foreign-old")
        return True

    monkeypatch.setattr(file_watcher, "_publish_registry_change", publish_then_restore)
    monkeypatch.setattr(
        "exomem.index_sync.upsert_after_write",
        lambda _root, _paths, **kwargs: fanout.setdefault("kwargs", kwargs),
    )
    monkeypatch.setattr("exomem.index_sync.full_upsert_succeeded", lambda *_args: True)

    assert vault_module.post_commit_batch_fanout(
        vault,
        [first, second],
        None,
        None,
        publication_intents=[first_intent, second_intent],
    ) is True

    assert fanout["kwargs"] == {
        "created_paths": [],
        "publish_corpus_change": True,
    }
    assert first_intent.disposition == "aborted"
    assert second_intent.disposition == "aborted"


def test_stage_failure_aborts_an_already_observed_snapshot_intent(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = vault / "Knowledge Base" / "Notes" / "stage-abort.md"
    second = vault / "Knowledge Base" / "Notes" / "stage-abort-second.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old", encoding="utf-8")
    second.write_text("old", encoding="utf-8")
    watcher = file_watcher.FileWatcher(vault)
    real_restore = vault_module._restore_bound_source_timestamps
    real_snapshot = vault_module._capture_batch_snapshot

    def restore_and_observe(source, descriptor, atime_ns, mtime_ns):  # noqa: ANN001
        real_restore(source, descriptor, atime_ns, mtime_ns)
        if source.path == target:
            watcher._record(source.path, deleted=False)

    def fail_second_snapshot(path: Path, **kwargs):
        if path == second:
            raise OSError("second snapshot failed")
        return real_snapshot(path, **kwargs)

    monkeypatch.setattr(
        vault_module, "_restore_bound_source_timestamps", restore_and_observe
    )
    monkeypatch.setattr(vault_module, "_capture_batch_snapshot", fail_second_snapshot)

    with pytest.raises(OSError, match="second snapshot failed"):
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(target, "new"),
                vault_module.PlannedWrite(second, "new"),
            ],
            vault_root=vault,
        )

    assert freshness.external_pending(vault) is True
    assert target in watcher._drain()[1]


def test_foreign_bytes_after_publish_are_replayed_as_external(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = vault / "Knowledge Base" / "Notes" / "foreign.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old", encoding="utf-8")
    watcher = file_watcher.FileWatcher(vault)
    real_published = vault_module._after_batch_destination_published

    def observe_then_replace(path: Path) -> None:
        real_published(path)
        if path != target:
            return
        watcher._record(path, deleted=False)
        path.write_text("bad", encoding="utf-8")

    monkeypatch.setattr(vault_module, "_after_batch_destination_published", observe_then_replace)
    monkeypatch.setattr(file_watcher, "_publish_registry_change", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("exomem.index_sync.upsert_after_write", lambda *_args, **_kwargs: None)

    with pytest.raises(vault_module.BatchWriteError, match="BATCH_ROLLBACK_INCOMPLETE"):
        vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(target, "new")], vault_root=vault
        )

    assert freshness.external_pending(vault) is True
    assert target in watcher._drain()[1]


def test_foreign_bytes_do_not_fall_through_to_legacy_self_signature(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_watcher.clear_self_write_registry()
    target = vault / "Knowledge Base" / "Notes" / "same-signature-foreign.md"
    intent = _register_active_intent(vault, target, b"new")
    file_watcher.begin_publication_installation([intent])
    file_watcher.mark_publication_installed([intent])
    monkeypatch.setattr(file_watcher, "_publish_registry_change", lambda *_args, **_kwargs: True)
    registered = file_watcher.register_self_write(vault, [target])
    file_watcher.finalize_publication_intents([intent], succeeded=registered)
    signature = target.stat()
    target.write_bytes(b"bad")
    os.utime(target, ns=(signature.st_atime_ns, signature.st_mtime_ns))
    watcher = file_watcher.FileWatcher(vault)

    assert file_watcher._is_self_write_event(vault, target, deleted=False) is True
    watcher._record(target, deleted=False)

    assert freshness.external_pending(vault) is True
    assert target in watcher._drain()[1]


def test_registration_failure_replays_held_event_before_writer_returns(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = vault / "Knowledge Base" / "Notes" / "registration-failure.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old", encoding="utf-8")
    watcher = file_watcher.FileWatcher(vault)
    real_published = vault_module._after_batch_destination_published

    def observe(path: Path) -> None:
        real_published(path)
        if path == target:
            watcher._record(path, deleted=False)

    monkeypatch.setattr(vault_module, "_after_batch_destination_published", observe)
    monkeypatch.setattr(
        file_watcher,
        "_publish_registry_change",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("publish failed")),
    )
    monkeypatch.setattr("exomem.index_sync.upsert_after_write", lambda *_args, **_kwargs: None)

    vault_module.batch_atomic_write([vault_module.PlannedWrite(target, "new")], vault_root=vault)

    assert freshness.external_pending(vault) is True
    assert watcher._drain()[1] == [target]


def test_rollback_incomplete_replays_published_remnant_immediately(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = vault / "Knowledge Base" / "Notes" / "rollback-remnant.md"
    second = vault / "Knowledge Base" / "Notes" / "rollback-second.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("target-old", encoding="utf-8")
    second.write_text("second-old", encoding="utf-8")
    watcher = file_watcher.FileWatcher(vault)
    real_published = vault_module._after_batch_destination_published
    real_replace = vault_module._BatchWorkspace.replace_artifact
    publications = 0

    def observe(path: Path) -> None:
        real_published(path)

    def fail_second_publication(self, artifact, final, **kwargs):  # noqa: ANN001
        nonlocal publications
        if artifact.name.startswith("stage-"):
            publications += 1
            if publications == 3:
                raise OSError("second publication failed")
        if artifact.name.startswith("restore-"):
            raise OSError("restore failed")
        return real_replace(self, artifact, final, **kwargs)

    monkeypatch.setattr(vault_module, "_after_batch_destination_published", observe)
    monkeypatch.setattr(vault_module._BatchWorkspace, "replace_artifact", fail_second_publication)

    with pytest.raises(vault_module.BatchWriteError, match="BATCH_ROLLBACK_INCOMPLETE"):
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(target, "target-new"),
                vault_module.PlannedWrite(second, "second-new"),
            ],
            vault_root=vault,
        )

    assert freshness.external_pending(vault) is True
    assert target in watcher._drain()[1]


def test_clean_rollback_without_watcher_does_not_fence_unchanged_vault(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = vault / "Knowledge Base" / "Notes" / "clean-rollback-first.md"
    second = vault / "Knowledge Base" / "Notes" / "clean-rollback-second.md"
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_text("first-old", encoding="utf-8")
    second.write_text("second-old", encoding="utf-8")
    real_replace = vault_module._BatchWorkspace.replace_artifact
    publications = 0

    def fail_second_publication(self, artifact, final, **kwargs):  # noqa: ANN001
        nonlocal publications
        if artifact.name.startswith("stage-"):
            publications += 1
            if publications == 3:
                raise OSError("second publication failed")
        return real_replace(self, artifact, final, **kwargs)

    monkeypatch.setattr(vault_module._BatchWorkspace, "replace_artifact", fail_second_publication)

    with pytest.raises(OSError, match="second publication failed"):
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(first, "first-new"),
                vault_module.PlannedWrite(second, "second-new"),
            ],
            vault_root=vault,
        )

    assert first.read_text(encoding="utf-8") == "first-old"
    assert second.read_text(encoding="utf-8") == "second-old"
    assert freshness.external_pending(vault) is False


def test_unbound_published_remnant_is_fenced_without_watcher(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = vault / "Knowledge Base" / "Notes" / "unbound-remnant.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old", encoding="utf-8")
    real_capture = vault_module._BatchArtifactGuard.capture
    new_hash = hashlib.sha256(b"new").hexdigest()

    def fail_capture(path: Path, **kwargs):
        if path == target and kwargs.get("expected_content_hash") == new_hash:
            raise OSError("guard capture failed after publication")
        return real_capture(path, **kwargs)

    monkeypatch.setattr(vault_module._BatchArtifactGuard, "capture", fail_capture)

    with pytest.raises(vault_module.BatchWriteError, match="BATCH_ROLLBACK_INCOMPLETE"):
        vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(target, "new")], vault_root=vault
        )

    assert target.read_text(encoding="utf-8") == "new"
    assert freshness.external_pending(vault) is True


def test_forced_remnant_replay_uses_canonical_symlink_root_key(
    vault: Path, tmp_path: Path
) -> None:
    linked_root = tmp_path / "linked-vault"
    linked_root.symlink_to(vault, target_is_directory=True)
    target = linked_root / "Knowledge Base" / "Notes" / "symlink-remnant.md"
    intent = _register_active_intent(linked_root, target, b"new")

    file_watcher.abort_publication_intents([intent], force_paths=[target])

    assert freshness.external_pending(vault) is True


def test_expired_held_intent_replays_without_another_event(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_watcher.clear_self_write_registry()
    target = vault / "Knowledge Base" / "Notes" / "expired.md"
    intent = _register_active_intent(vault, target, b"new")
    watcher = file_watcher.FileWatcher(vault)
    dispatched: list[list[Path]] = []
    monkeypatch.setattr(
        watcher,
        "_dispatch_batch",
        lambda paths, *_args, **_kwargs: dispatched.append(list(paths)),
    )

    watcher._record(target, deleted=False)
    intent.deadline = 0.0
    watcher._flush()

    assert freshness.external_pending(vault) is True
    assert dispatched == [[target]]


def test_capacity_rejects_new_intent_without_evicting_an_observed_one(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_watcher.clear_self_write_registry()
    monkeypatch.setattr(file_watcher, "_SUPPRESS_MAX_ENTRIES", 1)
    first = vault / "Knowledge Base" / "Notes" / "first.md"
    second = vault / "Knowledge Base" / "Notes" / "second.md"
    first_intent = _register_active_intent(vault, first, b"first")
    file_watcher.begin_publication_installation([first_intent])
    file_watcher.mark_publication_installed([first_intent])
    watcher = file_watcher.FileWatcher(vault)
    watcher._record(first, deleted=False)

    second.parent.mkdir(parents=True, exist_ok=True)
    second.write_bytes(b"second")
    descriptor = os.open(second, os.O_RDONLY)
    try:
        rejected = file_watcher.register_publication_intents(
            vault, [(second, descriptor, hashlib.sha256(b"second").hexdigest())]
        )
    finally:
        os.close(descriptor)
    watcher._record(second, deleted=False)

    assert rejected == ()
    assert watcher._pending_publication_intents[first] is first_intent
    assert freshness.external_pending(vault) is True


def test_hashing_releases_registry_lock_and_observes_late_abort(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_watcher.clear_self_write_registry()
    target = vault / "Knowledge Base" / "Notes" / "racing.md"
    intent = _register_active_intent(vault, target, b"new")
    watcher = file_watcher.FileWatcher(vault)
    entered = threading.Event()
    release = threading.Event()
    real_digest = file_watcher._bounded_descriptor_digest

    def delayed_digest(path: Path, size: int):
        entered.set()
        assert release.wait(timeout=2.0)
        return real_digest(path, size)

    monkeypatch.setattr(file_watcher, "_bounded_descriptor_digest", delayed_digest)
    event_thread = threading.Thread(
        target=lambda: watcher._record(target, deleted=False), daemon=True
    )
    event_thread.start()
    assert entered.wait(timeout=2.0)
    assert file_watcher._SUPPRESS_LOCK.acquire(timeout=0.2)
    file_watcher._SUPPRESS_LOCK.release()
    try:
        file_watcher.abort_publication_intents([intent])

        assert freshness.external_pending(vault) is True
    finally:
        release.set()
        event_thread.join(timeout=2.0)

    assert not event_thread.is_alive()
    assert freshness.external_pending(vault) is True


def test_token_change_during_hash_retries_once_against_the_replacement(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_watcher.clear_self_write_registry()
    target = vault / "Knowledge Base" / "Notes" / "token-change.md"
    first = _register_active_intent(vault, target, b"one")
    watcher = file_watcher.FileWatcher(vault)
    entered = threading.Event()
    release = threading.Event()
    real_digest = file_watcher._bounded_descriptor_digest
    calls = 0

    def delayed_digest(path: Path, size: int | None):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(timeout=2.0)
        return real_digest(path, size)

    monkeypatch.setattr(file_watcher, "_bounded_descriptor_digest", delayed_digest)
    event_thread = threading.Thread(
        target=lambda: watcher._record(target, deleted=False), daemon=True
    )
    event_thread.start()
    assert entered.wait(timeout=2.0)
    with file_watcher._SUPPRESS_LOCK:
        first.phase = "postpublish_verified"
    file_watcher.finalize_publication_intents([first], succeeded=[first])
    target.write_bytes(b"two")
    second = _register_active_intent(vault, target, b"two")
    with file_watcher._SUPPRESS_LOCK:
        second.phase = "installed"
    release.set()
    event_thread.join(timeout=2.0)
    with file_watcher._SUPPRESS_LOCK:
        second.phase = "postpublish_verified"
    file_watcher.finalize_publication_intents([second], succeeded=[second])

    assert not event_thread.is_alive()
    assert calls == 2
    assert freshness.external_pending(vault) is False


def test_inflight_duplicate_events_share_one_weak_watcher_path_subscription(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_watcher.clear_self_write_registry()
    target = vault / "Knowledge Base" / "Notes" / "duplicate-subscription.md"
    intent = _register_active_intent(vault, target, b"new")
    watcher = file_watcher.FileWatcher(vault)
    entered = threading.Barrier(3)
    release = threading.Event()
    real_digest = file_watcher._bounded_descriptor_digest

    def delayed_digest(path: Path, size: int | None):
        entered.wait(timeout=2.0)
        assert release.wait(timeout=2.0)
        return real_digest(path, size)

    monkeypatch.setattr(file_watcher, "_bounded_descriptor_digest", delayed_digest)
    threads = [
        threading.Thread(target=lambda: watcher._record(target, deleted=False), daemon=True)
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    entered.wait(timeout=2.0)

    assert len(intent.observers) == 1
    file_watcher.abort_publication_intents([intent])
    assert freshness.external_pending(vault) is True

    release.set()
    for thread in threads:
        thread.join(timeout=2.0)
        assert not thread.is_alive()


def test_failed_corpus_publication_keeps_intent_aborted_and_fallback_enabled(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = vault / "Knowledge Base" / "Notes" / "publication-false.md"
    intent = _register_active_intent(vault, target, b"new")
    fanout: dict[str, object] = {}

    monkeypatch.setattr(file_watcher, "_publish_registry_change", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        "exomem.index_sync.upsert_after_write",
        lambda _root, _paths, **kwargs: fanout.setdefault("kwargs", kwargs),
    )
    monkeypatch.setattr("exomem.index_sync.full_upsert_succeeded", lambda *_args: True)

    assert vault_module.post_commit_batch_fanout(
        vault, [target], None, None, publication_intents=[intent]
    ) is True

    assert fanout["kwargs"] == {
        "created_paths": [],
        "publish_corpus_change": True,
    }
    assert intent.disposition == "aborted"
