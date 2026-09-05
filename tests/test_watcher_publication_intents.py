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
    monkeypatch.setattr(file_watcher, "_publish_registry_change", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "exomem.index_sync.upsert_after_write",
        lambda _root, paths, **_kwargs: fanout.append(list(paths)),
    )

    vault_module.batch_atomic_write([vault_module.PlannedWrite(target, "new")], vault_root=vault)

    watcher._flush()

    assert fanout == [[target]]
    assert watcher._drain() == ([], [], [], 0, False)
    assert freshness.external_pending(vault) is False


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


def test_foreign_bytes_do_not_fall_through_to_legacy_self_signature(vault: Path) -> None:
    file_watcher.clear_self_write_registry()
    target = vault / "Knowledge Base" / "Notes" / "same-signature-foreign.md"
    intent = _register_active_intent(vault, target, b"new")
    file_watcher.register_self_write(vault, [target])
    file_watcher.finalize_publication_intents([intent], succeeded=[intent])
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
    file_watcher.abort_publication_intents([intent])
    release.set()
    event_thread.join(timeout=2.0)

    assert not event_thread.is_alive()
    assert freshness.external_pending(vault) is True
