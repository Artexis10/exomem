"""media_worker — the async extraction pipeline (extract engines stubbed; no GPU)."""

import hashlib
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pytest
import yaml

from exomem import (
    deferred_index,
    embeddings,
    extract,
    graph_sync,
    index_sync,
    media_jobs,
    media_worker,
    preserve,
    server_runtime,
)
from exomem import find as find_module
from exomem import vault as vault_module
from exomem.vault import content_hash


@pytest.fixture(autouse=True)
def _isolated_writer_state(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state"))


class _RecordingMutationManager:
    def __init__(self, vault) -> None:
        self.vault = vault
        self.depth = 0
        self.events: list[str] = []
        self.guard_metadata: list[dict[str, str]] = []

    @contextmanager
    def mutation_guard(self, vault, **metadata):
        assert vault == self.vault
        self.guard_metadata.append(metadata)
        self.events.append("guard-enter")
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1
            self.events.append("guard-exit")


def _preserve_media_stub(vault, filename="rec.mp3"):
    """Preserve a media binary with no text → a `pending` stub sidecar."""
    return preserve.preserve_bytes(
        vault, scope="Yolo", category="audio", filename=filename, data=b"FAKEBYTES"
    )


def _parsed_frontmatter(path) -> dict[str, object]:
    content = path.read_text(encoding="utf-8")
    assert content.startswith("---\n")
    raw, _body = content.removeprefix("---\n").split("\n---\n", 1)
    parsed = yaml.safe_load(raw)
    assert isinstance(parsed, dict)
    return parsed


def _sharing_violation(sidecar, *, winerror: int = 5) -> PermissionError:
    stage = sidecar.parent / f".exomem-batch-{'a' * 32}" / "stage-0.tmp"
    error = PermissionError(13, "Access is denied", str(stage))
    error.winerror = winerror
    error.filename2 = str(sidecar)
    return error


def test_preserve_media_writes_pending_stub(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault)
    assert result.sidecar_path is not None
    body = (vault / result.sidecar_path).read_text(encoding="utf-8")
    assert "media_type: audio" in body
    assert "evidence_file: " in body
    assert "extracted_by: pending" in body


def test_preserve_media_writes_actionable_stub_when_extraction_disabled(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    result = _preserve_media_stub(vault, filename="rec2.mp3")
    assert result.sidecar_path is not None
    body = (vault / result.sidecar_path).read_text(encoding="utf-8")
    assert "media_type: audio" in body
    assert "extracted_by: pending" in body


def test_worker_fills_pending_sidecar(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault, filename="call.mp3")
    sidecar = vault / result.sidecar_path
    monkeypatch.setattr(
        extract,
        "extract_text",
        lambda p, media_type=None, vault_root=None: extract.ExtractResult(
            text="discussion of the broken sink and water damage",
            media_type="audio",
            engine="faster-whisper:test",
        ),
    )
    w = media_worker.MediaWorker(vault, execution_mode="inline")
    w._process(
        media_worker._Job(binary_path=vault / result.path, sidecar_path=sidecar, media_type="audio")
    )

    body = sidecar.read_text(encoding="utf-8")
    assert "water damage" in body
    assert "extracted_by: faster-whisper:test" in body
    assert "extracted_by: pending" not in body


def test_extraction_compute_stays_outside_guard_and_sidecar_commit_is_inside(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault, filename="guarded.mp3")
    sidecar = vault / result.sidecar_path
    manager = _RecordingMutationManager(vault)
    original_update = preserve.update_sidecar_extraction
    fanout_depths: list[int] = []

    def extract_outside(*_args, **_kwargs):
        assert manager.depth == 0
        manager.events.append("extract")
        return extract.ExtractResult(text="guarded transcript", media_type="audio", engine="test")

    def update_inside(*args, **kwargs):
        assert manager.depth > 0
        manager.events.append("sidecar-commit")
        return original_update(*args, **kwargs)

    monkeypatch.setattr(media_worker, "get_manager", lambda: manager, raising=False)
    monkeypatch.setattr(extract, "extract_text", extract_outside)
    monkeypatch.setattr(preserve, "update_sidecar_extraction", update_inside)
    monkeypatch.setattr(
        media_worker.index_sync,
        "upsert_after_write",
        lambda *_args, **_kwargs: fanout_depths.append(manager.depth) or True,
    )

    worker = media_worker.MediaWorker(vault, execution_mode="inline")
    worker._process(
        media_worker._Job(
            binary_path=vault / result.path,
            sidecar_path=sidecar,
            media_type="audio",
        )
    )

    assert manager.events.index("extract") < manager.events.index("guard-enter")
    assert manager.events.index("guard-enter") < manager.events.index("sidecar-commit")
    assert manager.guard_metadata == [
        {
            "operation": "background_media_extraction_commit",
            "holder_kind": "background",
        },
        {
            "operation": "background_media_graph_completion",
            "holder_kind": "background",
        },
    ]
    assert fanout_depths == [0]
    assert manager.depth == 0


def test_processing_failure_sidecar_fans_out_after_commit_guard_release(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure-state media commits own the same guarded deferred-token postlude."""
    result = _preserve_media_stub(vault, filename="guarded-failure.mp3")
    manager = _RecordingMutationManager(vault)
    fanout_depths: list[int] = []
    floor_path = graph_sync.floor_path(vault)
    checkpoint_path = graph_sync.checkpoint_path(vault)
    original_add_full = deferred_index.add_full
    original_batch = preserve.batch_atomic_write
    original_read_artifact = media_worker.read_bounded_guarded_bytes
    original_replace = vault_module.os.replace
    token = None
    postlude_reads: list[str] = []
    checkpoint_replaced = False
    checkpoint_published = False

    def unavailable(*_args, **_kwargs):
        raise extract.ExtractionUnavailable("engine absent")

    def admit_full_receipts(root, paths):  # noqa: ANN001
        original_add_full(root, paths)
        return [
            receipt
            for receipt in deferred_index.snapshot_full(root)
            if receipt.rel_path in paths
        ]

    def capture_token(*args, **kwargs):  # noqa: ANN002, ANN003
        nonlocal token
        result = original_batch(*args, **kwargs)
        if kwargs.get("defer_graph_completion"):
            token = result
        return result

    def read_postlude_artifact(root, target, *args, **kwargs):  # noqa: ANN001
        path = Path(root) / target
        if (
            len(manager.guard_metadata) >= 2
            and not checkpoint_published
            and Path(path) in {floor_path, checkpoint_path}
        ):
            assert manager.depth > 0
            postlude_reads.append("floor" if Path(path) == floor_path else "predecessor")
        return original_read_artifact(root, target, *args, **kwargs)

    def replace_checkpoint_inside_postlude(source, destination, *args, **kwargs):  # noqa: ANN001
        nonlocal checkpoint_published, checkpoint_replaced
        if Path(destination) == checkpoint_path and len(manager.guard_metadata) >= 2:
            assert manager.depth > 0
            checkpoint_replaced = True
            result = original_replace(source, destination, *args, **kwargs)
            checkpoint_published = True
            return result
        return original_replace(source, destination, *args, **kwargs)

    def completed_fanout(root, *_args, **_kwargs):  # noqa: ANN001
        assert manager.depth == 0
        assert token is not None
        assert graph_sync.read_checkpoint(root) == token.checkpoint
        fanout_depths.append(manager.depth)
        return True

    monkeypatch.setattr(media_worker, "get_manager", lambda: manager)
    monkeypatch.setattr(extract, "extract_text", unavailable)
    monkeypatch.setattr(
        deferred_index, "add_full_receipts", admit_full_receipts, raising=False
    )
    monkeypatch.setattr(preserve, "batch_atomic_write", capture_token)
    monkeypatch.setattr(media_worker, "read_bounded_guarded_bytes", read_postlude_artifact)
    monkeypatch.setattr(vault_module.os, "replace", replace_checkpoint_inside_postlude)
    monkeypatch.setattr(
        media_worker, "post_commit_batch_fanout", completed_fanout
    )
    worker = media_worker.MediaWorker(vault, execution_mode="inline")

    outcome = worker._process(
        media_worker._Job(
            binary_path=vault / result.path,
            sidecar_path=vault / result.sidecar_path,
            media_type="audio",
        )
    )

    assert outcome.state == media_jobs.BLOCKED
    assert fanout_depths == [0]
    assert manager.guard_metadata[0] == {
        "operation": "background_media_failure_commit",
        "holder_kind": "background",
    }
    assert len(manager.guard_metadata) == 2
    assert all(metadata["holder_kind"] == "background" for metadata in manager.guard_metadata)
    assert token is not None
    assert postlude_reads == ["floor", "predecessor"]
    assert checkpoint_replaced
    assert deferred_index.snapshot_full(vault) == []


@pytest.mark.parametrize("fails_extraction", [False, True], ids=["extraction", "failure"])
def test_media_canonical_commits_request_deferred_graph_completion(
    vault, monkeypatch: pytest.MonkeyPatch, fails_extraction: bool
) -> None:
    """Only media's canonical transitions may opt into the deferred token."""
    result = _preserve_media_stub(vault, filename=f"deferred-{fails_extraction}.m4a")
    sidecar = vault / result.sidecar_path
    original_batch = preserve.batch_atomic_write
    requested: list[dict[str, object]] = []

    def capture_deferred_batch(*args, **kwargs):  # noqa: ANN002, ANN003
        if args and getattr(args[0][0], "path", None) == sidecar:
            requested.append(kwargs)
        return original_batch(*args, **kwargs)

    if fails_extraction:
        monkeypatch.setattr(
            extract,
            "extract_text",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                extract.ExtractionUnavailable("engine absent")
            ),
        )
    else:
        monkeypatch.setattr(
            extract,
            "extract_text",
            lambda *_args, **_kwargs: extract.ExtractResult(
                text="deferred transcript", media_type="audio", engine="test"
            ),
        )
    monkeypatch.setattr(preserve, "batch_atomic_write", capture_deferred_batch)

    outcome = media_worker.MediaWorker(vault, execution_mode="inline")._process(
        media_worker._Job(
            binary_path=vault / result.path,
            sidecar_path=sidecar,
            media_type="audio",
        )
    )

    assert outcome.state in {"complete", media_jobs.BLOCKED}
    assert requested == [
        {
            "vault_root": vault,
            "post_commit_fanout": False,
            "defer_graph_completion": True,
        }
    ]


def test_media_deferred_postlude_reenters_the_mutation_coordinator(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deferred token crosses the guard; its checkpoint work must re-enter it."""
    result = _preserve_media_stub(vault, filename="postlude-guard.m4a")
    sidecar = vault / result.sidecar_path
    manager = _RecordingMutationManager(vault)
    floor = graph_sync.floor_path(vault)
    checkpoint = graph_sync.checkpoint_path(vault)
    original_read_artifact = media_worker.read_bounded_guarded_bytes
    original_replace = vault_module.os.replace
    postlude_reads: list[str] = []
    checkpoint_replaced = False
    fanout_depths: list[int] = []

    monkeypatch.setattr(
        extract,
        "extract_text",
        lambda *_args, **_kwargs: extract.ExtractResult(
            text="guarded deferred transcript", media_type="audio", engine="test"
        ),
    )
    monkeypatch.setattr(media_worker, "get_manager", lambda: manager)

    def read_artifact_inside_postlude(root, target, *args, **kwargs):  # noqa: ANN001
        path = Path(root) / target
        if len(manager.guard_metadata) >= 2 and Path(path) in {floor, checkpoint}:
            assert manager.depth > 0
            postlude_reads.append("floor" if Path(path) == floor else "predecessor")
        return original_read_artifact(root, target, *args, **kwargs)

    def replace_checkpoint_inside_postlude(source, destination, *args, **kwargs):  # noqa: ANN001
        nonlocal checkpoint_replaced
        if Path(destination) == checkpoint and len(manager.guard_metadata) >= 2:
            assert manager.depth > 0
            checkpoint_replaced = True
        return original_replace(source, destination, *args, **kwargs)

    def fanout_after_postlude(*_args, **_kwargs):
        assert manager.depth == 0
        fanout_depths.append(manager.depth)
        return True

    monkeypatch.setattr(
        media_worker, "read_bounded_guarded_bytes", read_artifact_inside_postlude
    )
    monkeypatch.setattr(vault_module.os, "replace", replace_checkpoint_inside_postlude)
    monkeypatch.setattr(media_worker, "post_commit_batch_fanout", fanout_after_postlude)

    media_worker.MediaWorker(vault, execution_mode="inline")._process(
        media_worker._Job(
            binary_path=vault / result.path,
            sidecar_path=sidecar,
            media_type="audio",
        )
    )

    assert manager.guard_metadata[0] == {
        "operation": "background_media_extraction_commit",
        "holder_kind": "background",
    }
    assert len(manager.guard_metadata) == 2
    assert all(metadata["holder_kind"] == "background" for metadata in manager.guard_metadata)
    assert postlude_reads == ["floor", "predecessor"]
    assert checkpoint_replaced
    assert fanout_depths == [0]


@pytest.mark.parametrize("fails_extraction", [False, True], ids=["extraction", "failure"])
def test_media_receipt_admission_failure_aborts_before_floor_or_sidecar_mutation(
    vault, monkeypatch: pytest.MonkeyPatch, fails_extraction: bool
) -> None:
    """Recovery work must be durable before either canonical graph artifact changes."""
    result = _preserve_media_stub(vault, filename=f"admission-{fails_extraction}.m4a")
    sidecar = vault / result.sidecar_path
    floor = graph_sync.floor_path(vault)
    checkpoint = graph_sync.checkpoint_path(vault)
    before_sidecar = sidecar.read_bytes()
    before_floor = floor.read_bytes()
    before_checkpoint = checkpoint.read_bytes()

    if fails_extraction:
        monkeypatch.setattr(
            extract,
            "extract_text",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                extract.ExtractionUnavailable("engine absent")
            ),
        )
    else:
        monkeypatch.setattr(
            extract,
            "extract_text",
            lambda *_args, **_kwargs: extract.ExtractResult(
                text="must not commit", media_type="audio", engine="test"
            ),
        )
    monkeypatch.setattr(
        deferred_index,
        "add_full_receipts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("receipt store unavailable")),
        raising=False,
    )

    with pytest.raises(OSError, match="receipt store unavailable"):
        media_worker.MediaWorker(vault, execution_mode="inline")._process(
            media_worker._Job(
                binary_path=vault / result.path,
                sidecar_path=sidecar,
                media_type="audio",
            )
        )

    assert sidecar.read_bytes() == before_sidecar
    assert floor.read_bytes() == before_floor
    assert checkpoint.read_bytes() == before_checkpoint
    assert deferred_index.snapshot_full(vault) == []


def test_newer_graph_epoch_supersedes_stale_media_postlude_without_fanout_or_receipt_clear(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-guard media token cannot regress a newer canonical writer's epoch."""
    result = _preserve_media_stub(vault, filename="postlude-race.m4a")
    sidecar = vault / result.sidecar_path
    rel = sidecar.relative_to(vault).as_posix()
    original_update = preserve.update_sidecar_extraction
    original_add_full = deferred_index.add_full
    fanout_calls: list[tuple[Path | None, list[Path]]] = []

    def admit_full_receipts(root, paths):  # noqa: ANN001
        original_add_full(root, paths)
        return [
            receipt
            for receipt in deferred_index.snapshot_full(root)
            if receipt.rel_path in paths
        ]

    monkeypatch.setattr(
        extract,
        "extract_text",
        lambda *_args, **_kwargs: extract.ExtractResult(
            text="A canonical transcript", media_type="audio", engine="test"
        ),
    )

    def commit_a_then_advance_b(*args, **kwargs):  # noqa: ANN002, ANN003
        written = original_update(*args, **kwargs)
        vault_module.batch_atomic_write(
            [
                vault_module.PlannedWrite(
                    vault / "Knowledge Base" / "Notes" / "writer-b.md",
                    "---\ntype: note\n---\nwriter B\n",
                )
            ],
            vault_root=vault,
            post_commit_fanout=False,
        )
        return written

    monkeypatch.setattr(preserve, "update_sidecar_extraction", commit_a_then_advance_b)
    monkeypatch.setattr(
        deferred_index, "add_full_receipts", admit_full_receipts, raising=False
    )
    monkeypatch.setattr(
        media_worker,
        "post_commit_batch_fanout",
        lambda root, written, *_args, **_kwargs: fanout_calls.append((root, written)),
    )

    media_worker.MediaWorker(vault, execution_mode="inline")._process(
        media_worker._Job(
            binary_path=vault / result.path,
            sidecar_path=sidecar,
            media_type="audio",
        )
    )

    checkpoint = graph_sync.read_checkpoint(vault)
    floor = graph_sync.read_floor(vault)
    assert checkpoint is not None and floor is not None
    assert checkpoint.generation == floor.generation
    assert checkpoint.generation > 1
    assert fanout_calls == []
    assert deferred_index.snapshot_full(vault) == [deferred_index.DeferredReceipt(rel, 1)]
    assert "A canonical transcript" in sidecar.read_text(encoding="utf-8")


def _assert_media_postlude_mismatch_retains_receipt(
    vault, monkeypatch: pytest.MonkeyPatch, *, mismatch: str
) -> None:
    result = _preserve_media_stub(vault, filename=f"{mismatch}-mismatch.m4a")
    sidecar = vault / result.sidecar_path
    rel = sidecar.relative_to(vault).as_posix()
    checkpoint_path = graph_sync.checkpoint_path(vault)
    original_update = preserve.update_sidecar_extraction
    original_add_full = deferred_index.add_full
    original_replace = vault_module.os.replace
    published: list[Path] = []
    fanout_calls: list[object] = []
    clear_calls: list[object] = []

    def admit_full_receipts(root, paths):  # noqa: ANN001
        original_add_full(root, paths)
        return [
            receipt
            for receipt in deferred_index.snapshot_full(root)
            if receipt.rel_path in paths
        ]

    def commit_then_mismatch(*args, **kwargs):  # noqa: ANN002, ANN003
        handoff = original_update(*args, **kwargs)
        assert isinstance(handoff, vault_module.DeferredGraphCompletion)
        if mismatch == "floor":
            graph_sync.floor_path(vault).write_text(
                graph_sync.GraphSyncGenerationFloor.create(
                    handoff.checkpoint.generation + 1
                ).render(),
                encoding="utf-8",
            )
        else:
            assert handoff.predecessor is not None
            graph_sync.checkpoint_path(vault).write_text(
                graph_sync.GraphSyncCheckpoint.create(
                    generation=handoff.predecessor.generation + 17,
                    mutation_id="f" * 24,
                    paths=(),
                    created_paths=(),
                    scope="full",
                ).render(),
                encoding="utf-8",
            )
        return handoff

    def observe_checkpoint_publication(source, destination, *args, **kwargs):  # noqa: ANN001
        if Path(destination) == checkpoint_path:
            published.append(Path(destination))
        return original_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(
        extract,
        "extract_text",
        lambda *_args, **_kwargs: extract.ExtractResult(
            text=f"{mismatch} mismatch transcript", media_type="audio", engine="test"
        ),
    )
    monkeypatch.setattr(
        deferred_index, "add_full_receipts", admit_full_receipts, raising=False
    )
    monkeypatch.setattr(preserve, "update_sidecar_extraction", commit_then_mismatch)
    monkeypatch.setattr(vault_module.os, "replace", observe_checkpoint_publication)
    monkeypatch.setattr(
        media_worker,
        "post_commit_batch_fanout",
        lambda *_args, **_kwargs: fanout_calls.append(object()),
    )
    monkeypatch.setattr(
        deferred_index,
        "clear_full_receipts",
        lambda *_args, **_kwargs: clear_calls.append(object()),
    )

    media_worker.MediaWorker(vault, execution_mode="inline")._process(
        media_worker._Job(
            binary_path=vault / result.path,
            sidecar_path=sidecar,
            media_type="audio",
        )
    )

    assert published == []
    assert fanout_calls == []
    assert clear_calls == []
    assert deferred_index.snapshot_full(vault) == [deferred_index.DeferredReceipt(rel, 1)]
    assert f"{mismatch} mismatch transcript" in sidecar.read_text(encoding="utf-8")


def test_floor_only_mismatch_suppresses_media_postlude_and_retains_receipt(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_media_postlude_mismatch_retains_receipt(vault, monkeypatch, mismatch="floor")


def test_checkpoint_only_mismatch_suppresses_media_postlude_and_retains_receipt(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_media_postlude_mismatch_retains_receipt(
        vault, monkeypatch, mismatch="checkpoint"
    )


def test_external_floor_and_checkpoint_swaps_after_media_cas_do_not_pair_stale_token(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guarded checkpoint write rejects artifacts replaced after its CAS reads."""
    result = _preserve_media_stub(vault, filename="external-postlude-race.m4a")
    sidecar = vault / result.sidecar_path
    rel = sidecar.relative_to(vault).as_posix()
    floor_path = graph_sync.floor_path(vault)
    checkpoint_path = graph_sync.checkpoint_path(vault)
    original_batch = media_worker.batch_atomic_write
    fanout_calls: list[object] = []
    swapped = False

    def replace_after_cas(writes, **kwargs):  # noqa: ANN001
        nonlocal swapped
        [write] = writes
        if write.path == checkpoint_path and not swapped:
            swapped = True
            old_floor = graph_sync.read_floor(vault)
            old_checkpoint = graph_sync.read_checkpoint(vault)
            assert old_floor is not None and old_checkpoint is not None
            replacement_floor = graph_sync.GraphSyncGenerationFloor.create(
                old_floor.generation + 17
            )
            replacement_checkpoint = graph_sync.GraphSyncCheckpoint.create(
                generation=old_checkpoint.generation + 23,
                mutation_id="e" * 24,
                paths=(),
                created_paths=(),
                scope="full",
            )
            floor_swap = floor_path.with_name(".floor-external-swap")
            checkpoint_swap = checkpoint_path.with_name(".checkpoint-external-swap")
            floor_swap.write_text(replacement_floor.render(), encoding="utf-8")
            checkpoint_swap.write_text(replacement_checkpoint.render(), encoding="utf-8")
            os.replace(floor_swap, floor_path)
            os.replace(checkpoint_swap, checkpoint_path)
            replace_after_cas.floor = replacement_floor
            replace_after_cas.checkpoint = replacement_checkpoint
        return original_batch(writes, **kwargs)

    monkeypatch.setattr(
        extract,
        "extract_text",
        lambda *_args, **_kwargs: extract.ExtractResult(
            text="external race transcript", media_type="audio", engine="test"
        ),
    )
    monkeypatch.setattr(media_worker, "batch_atomic_write", replace_after_cas)
    monkeypatch.setattr(
        media_worker,
        "post_commit_batch_fanout",
        lambda *_args, **_kwargs: fanout_calls.append(object()) or True,
    )

    outcome = media_worker.MediaWorker(vault, execution_mode="inline")._process(
        media_worker._Job(
            binary_path=vault / result.path,
            sidecar_path=sidecar,
            media_type="audio",
        )
    )

    assert outcome.state == "complete"
    assert swapped
    assert graph_sync.read_floor(vault) == replace_after_cas.floor
    assert graph_sync.read_checkpoint(vault) == replace_after_cas.checkpoint
    assert fanout_calls == []
    assert deferred_index.snapshot_full(vault) == [deferred_index.DeferredReceipt(rel, 1)]


def test_absent_media_predecessor_refuses_new_malformed_checkpoint(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absent predecessor cannot authorize replacing a malformed new leaf."""
    result = _preserve_media_stub(vault, filename="absent-predecessor.m4a")
    sidecar = vault / result.sidecar_path
    rel = sidecar.relative_to(vault).as_posix()
    checkpoint_path = graph_sync.checkpoint_path(vault)
    checkpoint_path.unlink()
    original_update = preserve.update_sidecar_extraction
    fanout_calls: list[object] = []

    def commit_then_add_malformed_checkpoint(*args, **kwargs):  # noqa: ANN002, ANN003
        handoff = original_update(*args, **kwargs)
        assert isinstance(handoff, vault_module.DeferredGraphCompletion)
        assert handoff.predecessor is None
        checkpoint_path.write_text("malformed checkpoint", encoding="utf-8")
        return handoff

    monkeypatch.setattr(
        extract,
        "extract_text",
        lambda *_args, **_kwargs: extract.ExtractResult(
            text="absent predecessor transcript", media_type="audio", engine="test"
        ),
    )
    monkeypatch.setattr(
        preserve,
        "update_sidecar_extraction",
        commit_then_add_malformed_checkpoint,
    )
    monkeypatch.setattr(
        media_worker,
        "post_commit_batch_fanout",
        lambda *_args, **_kwargs: fanout_calls.append(object()) or True,
    )

    outcome = media_worker.MediaWorker(vault, execution_mode="inline")._process(
        media_worker._Job(
            binary_path=vault / result.path,
            sidecar_path=sidecar,
            media_type="audio",
        )
    )

    assert outcome.state == "complete"
    assert checkpoint_path.read_text(encoding="utf-8") == "malformed checkpoint"
    assert fanout_calls == []
    assert deferred_index.snapshot_full(vault) == [deferred_index.DeferredReceipt(rel, 1)]


@pytest.mark.parametrize("fanout_result", [None, "accepted_unverified"])
def test_media_postlude_retains_receipt_without_literal_true_fanout(
    vault, monkeypatch: pytest.MonkeyPatch, fanout_result: str | None
) -> None:
    """Only a positively verified fanout may retire media's write-ahead work."""
    result = _preserve_media_stub(vault, filename=f"fanout-{fanout_result}.m4a")
    sidecar = vault / result.sidecar_path
    rel = sidecar.relative_to(vault).as_posix()

    def unverified_fanout(root, written, *_args, **_kwargs):  # noqa: ANN001
        if fanout_result is None:
            return None
        return index_sync.unverified_upsert_report(root, written)

    monkeypatch.setattr(
        extract,
        "extract_text",
        lambda *_args, **_kwargs: extract.ExtractResult(
            text="fanout verification transcript", media_type="audio", engine="test"
        ),
    )
    monkeypatch.setattr(media_worker, "post_commit_batch_fanout", unverified_fanout)

    outcome = media_worker.MediaWorker(vault, execution_mode="inline")._process(
        media_worker._Job(
            binary_path=vault / result.path,
            sidecar_path=sidecar,
            media_type="audio",
        )
    )

    assert outcome.state == "complete"
    assert deferred_index.snapshot_full(vault) == [deferred_index.DeferredReceipt(rel, 1)]


def test_post_commit_batch_fanout_refuses_accepted_unverified_report(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fanout result itself never upgrades unobserved legacy completion."""
    result = _preserve_media_stub(vault, filename="unverified-report.m4a")
    sidecar = vault / result.sidecar_path
    report = index_sync.unverified_upsert_report(vault, [sidecar])

    monkeypatch.setattr(index_sync, "upsert_after_write", lambda *_args, **_kwargs: report)

    assert vault_module.post_commit_batch_fanout(vault, [sidecar], None, None) is False


def _verified_media_fanout_report(vault, sidecar) -> index_sync.IndexSyncReport:
    rel = sidecar.relative_to(vault).as_posix()
    return index_sync.IndexSyncReport(
        "upsert",
        (rel,),
        (rel,),
        (
            index_sync.IndexComponentOutcome("memory_refs", "completed", "dispatch_completed"),
            index_sync.IndexComponentOutcome("resolver", "completed", "dispatch_completed"),
            index_sync.IndexComponentOutcome(
                "semantic_purge", "completed", "purge_completed"
            ),
            index_sync.IndexComponentOutcome("lexstore", "completed", "dispatch_completed"),
            index_sync.IndexComponentOutcome(
                "epistemic_graph", "completed", "incremental_completed"
            ),
            index_sync.IndexComponentOutcome(
                "embeddings", "accepted", "embeddings_disabled"
            ),
        ),
    )


@pytest.mark.parametrize("missing_component", ["epistemic_graph", "lexstore"])
def test_post_commit_batch_fanout_refuses_missing_required_media_component(
    vault, monkeypatch: pytest.MonkeyPatch, missing_component: str
) -> None:
    """A partial report cannot claim verified media fanout completion."""
    result = _preserve_media_stub(vault, filename=f"missing-{missing_component}.m4a")
    sidecar = vault / result.sidecar_path
    report = _verified_media_fanout_report(vault, sidecar)
    report = index_sync.IndexSyncReport(
        report.operation,
        report.requested_paths,
        report.eligible_paths,
        tuple(item for item in report.components if item.component != missing_component),
    )

    monkeypatch.setattr(index_sync, "upsert_after_write", lambda *_args, **_kwargs: report)

    assert vault_module.post_commit_batch_fanout(vault, [sidecar], None, None) is False


def test_post_commit_batch_fanout_rejects_cross_component_no_work_spoof(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only embeddings may claim the explicit embeddings-disabled no-work code."""
    result = _preserve_media_stub(vault, filename="spoofed-resolver.m4a")
    sidecar = vault / result.sidecar_path
    report = _verified_media_fanout_report(vault, sidecar)
    report = index_sync.with_component(
        report,
        index_sync.IndexComponentOutcome("resolver", "accepted", "embeddings_disabled"),
    )

    monkeypatch.setattr(index_sync, "upsert_after_write", lambda *_args, **_kwargs: report)

    assert vault_module.post_commit_batch_fanout(vault, [sidecar], None, None) is False


def test_media_postlude_cas_clears_only_admitted_receipt_after_checkpoint_and_fanout(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent receipt revision survives successful convergence of the older one."""
    result = _preserve_media_stub(vault, filename="receipt-cas.m4a")
    sidecar = vault / result.sidecar_path
    rel = sidecar.relative_to(vault).as_posix()
    original_batch = preserve.batch_atomic_write
    original_add_full = deferred_index.add_full
    original_clear_full = deferred_index.clear_full_receipts
    events: list[str] = []
    token = None

    monkeypatch.setattr(
        extract,
        "extract_text",
        lambda *_args, **_kwargs: extract.ExtractResult(
            text="CAS transcript", media_type="audio", engine="test"
        ),
    )

    def admit(root, paths):  # noqa: ANN001
        events.append("admit")
        original_add_full(root, paths)
        [admitted] = deferred_index.snapshot_full(root)
        assert admitted == deferred_index.DeferredReceipt(rel, 2)
        # This write races after atomic admission but before any later lookup.
        # Clearing a fresh snapshot would wrongly erase revision 3.
        original_add_full(root, [rel])
        return [admitted]

    def capture_token(*args, **kwargs):  # noqa: ANN002, ANN003
        nonlocal token
        result = original_batch(*args, **kwargs)
        if kwargs.get("defer_graph_completion"):
            token = result
            events.append("canonical")
        return result

    def completed_fanout(root, written, *_args, **_kwargs):  # noqa: ANN001
        assert token is not None
        assert graph_sync.read_checkpoint(root) == token.checkpoint
        events.append("fanout")
        return True

    def readd_before_cas_clear(root, receipts):  # noqa: ANN001
        assert token is not None
        assert receipts == [deferred_index.DeferredReceipt(rel, 2)]
        assert graph_sync.read_checkpoint(root) == token.checkpoint
        assert events == ["admit", "canonical", "fanout"]
        events.append("clear")
        return original_clear_full(root, receipts)

    original_add_full(vault, [rel])
    monkeypatch.setattr(deferred_index, "add_full_receipts", admit, raising=False)
    monkeypatch.setattr(deferred_index, "clear_full_receipts", readd_before_cas_clear)
    monkeypatch.setattr(preserve, "batch_atomic_write", capture_token)
    monkeypatch.setattr(media_worker, "post_commit_batch_fanout", completed_fanout)

    outcome = media_worker.MediaWorker(vault, execution_mode="inline")._process(
        media_worker._Job(
            binary_path=vault / result.path,
            sidecar_path=sidecar,
            media_type="audio",
        )
    )

    assert outcome.state == "complete"
    assert events == ["admit", "canonical", "fanout", "clear"]
    assert deferred_index.snapshot_full(vault) == [deferred_index.DeferredReceipt(rel, 3)]


def test_checkpoint_publication_failure_keeps_completed_media_and_durable_receipt(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held derived checkpoint cannot roll back or terminalize canonical media."""
    result = _preserve_media_stub(vault, filename="checkpoint-failure.m4a")
    sidecar = vault / result.sidecar_path
    checkpoint = graph_sync.checkpoint_path(vault)
    original_replace = vault_module.os.replace
    extraction_calls = 0

    def transcribe(*_args, **_kwargs):
        nonlocal extraction_calls
        extraction_calls += 1
        return extract.ExtractResult(
            text="transcript survives checkpoint failure", media_type="audio", engine="test"
        )

    def deny_checkpoint_replace(source, destination, *args, **kwargs):  # noqa: ANN001
        if Path(destination) == checkpoint:
            raise PermissionError("held graph checkpoint")
        return original_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(extract, "extract_text", transcribe)
    monkeypatch.setattr(vault_module.os, "replace", deny_checkpoint_replace)
    store = media_jobs.MediaJobStore(vault)
    store.enqueue(
        media_jobs.MediaJob(
            binary_path=vault / result.path,
            sidecar_path=sidecar,
            media_type="audio",
        )
    )

    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.01) == 0

    assert extraction_calls == 1
    assert "transcript survives checkpoint failure" in sidecar.read_text(encoding="utf-8")
    assert deferred_index.snapshot_full(vault) == [
        deferred_index.DeferredReceipt(sidecar.relative_to(vault).as_posix(), 1)
    ]
    assert media_jobs.status(vault)["jobs"] == []


def test_post_checkpoint_fanout_failure_keeps_committed_media_and_exact_receipt(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Derived fanout failure is durable work, never a media rollback failure."""
    result = _preserve_media_stub(vault, filename="fanout-failure.m4a")
    sidecar = vault / result.sidecar_path
    rel = sidecar.relative_to(vault).as_posix()
    original_add_full = deferred_index.add_full
    original_batch = preserve.batch_atomic_write
    token = None
    extraction_calls = 0

    def admit_full_receipts(root, paths):  # noqa: ANN001
        original_add_full(root, paths)
        return [
            receipt
            for receipt in deferred_index.snapshot_full(root)
            if receipt.rel_path in paths
        ]

    def capture_token(*args, **kwargs):  # noqa: ANN002, ANN003
        nonlocal token
        result = original_batch(*args, **kwargs)
        if kwargs.get("defer_graph_completion"):
            token = result
        return result

    def transcribe(*_args, **_kwargs):
        nonlocal extraction_calls
        extraction_calls += 1
        return extract.ExtractResult(
            text="transcript survives fanout failure", media_type="audio", engine="test"
        )

    def fail_after_checkpoint(root, *_args, **_kwargs):  # noqa: ANN001
        assert token is not None
        assert graph_sync.read_checkpoint(root) == token.checkpoint
        raise RuntimeError("fanout offline")

    monkeypatch.setattr(extract, "extract_text", transcribe)
    monkeypatch.setattr(
        deferred_index, "add_full_receipts", admit_full_receipts, raising=False
    )
    monkeypatch.setattr(preserve, "batch_atomic_write", capture_token)
    monkeypatch.setattr(media_worker, "post_commit_batch_fanout", fail_after_checkpoint)
    store = media_jobs.MediaJobStore(vault)
    store.enqueue(
        media_jobs.MediaJob(
            binary_path=vault / result.path,
            sidecar_path=sidecar,
            media_type="audio",
        )
    )

    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.01) == 0

    assert extraction_calls == 1
    body = sidecar.read_text(encoding="utf-8")
    assert body.count("transcript survives fanout failure") == 1
    assert deferred_index.snapshot_full(vault) == [deferred_index.DeferredReceipt(rel, 1)]
    jobs = media_jobs.status(vault)["jobs"]
    assert jobs == []
    assert all("BATCH_ROLLBACK_INCOMPLETE" not in (job["error"] or "") for job in jobs)


def test_sidecar_edit_during_extraction_makes_result_stale(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault, filename="edited-during-extraction.mp3")
    sidecar = vault / result.sidecar_path
    manager = _RecordingMutationManager(vault)

    def extract_after_user_edit(*_args, **_kwargs):
        sidecar.write_text(
            sidecar.read_text(encoding="utf-8") + "\nUSER CANONICAL EDIT\n",
            encoding="utf-8",
        )
        return extract.ExtractResult(
            text="STALE MACHINE TRANSCRIPT", media_type="audio", engine="test"
        )

    monkeypatch.setattr(media_worker, "get_manager", lambda: manager, raising=False)
    monkeypatch.setattr(extract, "extract_text", extract_after_user_edit)
    worker = media_worker.MediaWorker(vault, execution_mode="inline")

    worker._process(
        media_worker._Job(
            binary_path=vault / result.path,
            sidecar_path=sidecar,
            media_type="audio",
        )
    )

    body = sidecar.read_text(encoding="utf-8")
    assert "USER CANONICAL EDIT" in body
    assert "STALE MACHINE TRANSCRIPT" not in body
    assert "extracted_by: pending" in body


@pytest.mark.parametrize("extraction_fails", [False, True])
def test_parent_media_change_during_extraction_skips_stale_sidecar_commit(
    vault, monkeypatch: pytest.MonkeyPatch, extraction_fails: bool
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault, filename=f"binary-stale-{extraction_fails}.mp3")
    binary = vault / result.path
    sidecar = vault / result.sidecar_path
    manager = _RecordingMutationManager(vault)

    def extract_after_binary_change(*_args, **_kwargs):
        binary.write_bytes(b"replacement-media")
        if extraction_fails:
            raise RuntimeError("old media could not be decoded")
        return extract.ExtractResult(
            text="STALE BINARY TRANSCRIPT", media_type="audio", engine="test"
        )

    monkeypatch.setattr(media_worker, "get_manager", lambda: manager, raising=False)
    monkeypatch.setattr(extract, "extract_text", extract_after_binary_change)
    worker = media_worker.MediaWorker(vault, execution_mode="inline")

    worker._process(
        media_worker._Job(
            binary_path=binary,
            sidecar_path=sidecar,
            media_type="audio",
        )
    )

    body = sidecar.read_text(encoding="utf-8")
    assert binary.read_bytes() == b"replacement-media"
    assert "STALE BINARY TRANSCRIPT" not in body
    assert "extracted_by: failed:" not in body
    assert "extracted_by: pending" in body


def test_changed_media_identity_is_automatically_reconciled_without_retry(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import media_processing

    result = _preserve_media_stub(vault, filename="durable-stale.m4a")
    binary = vault / result.path
    sidecar = vault / result.sidecar_path
    replacement = b"replacement identity"

    def _replace_during_asr(*_args, **_kwargs):
        binary.write_bytes(replacement)
        return extract.ExtractResult(
            text="[0:00] stale transcript",
            media_type="audio",
            engine="faster-whisper:test+timed",
        )

    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)
    monkeypatch.setattr(extract, "extract_text", _replace_during_asr)
    store = media_jobs.MediaJobStore(vault)
    store.enqueue(
        media_jobs.MediaJob(binary_path=binary, sidecar_path=sidecar, media_type="audio")
    )

    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.1) == 0
    assert media_jobs.status(vault)["jobs"] == []
    assert "processing_state: pending" in sidecar.read_text(encoding="utf-8")

    reconciled = media_processing.reconcile_media(vault, binary)

    assert reconciled.state == media_jobs.PENDING
    assert reconciled.job_id is not None
    [pending] = media_jobs.status(vault)["jobs"]
    assert pending["state"] == media_jobs.PENDING
    frontmatter = _parsed_frontmatter(sidecar)
    assert frontmatter["binary_sha256"] == hashlib.sha256(replacement).hexdigest()


def test_pending_sidecar_edit_during_durable_asr_is_persisted_as_retryable_failure(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _preserve_media_stub(vault, filename="durable-sidecar-stale.m4a")
    binary = vault / result.path
    sidecar = vault / result.sidecar_path

    def _edit_pending_sidecar(*_args, **_kwargs):
        sidecar.write_text(
            sidecar.read_text(encoding="utf-8") + "\nUSER CANONICAL EDIT\n",
            encoding="utf-8",
        )
        return extract.ExtractResult(
            text="[0:00] stale transcript",
            media_type="audio",
            engine="faster-whisper:test+timed",
        )

    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)
    monkeypatch.setattr(extract, "extract_text", _edit_pending_sidecar)
    store = media_jobs.MediaJobStore(vault)
    store.enqueue(
        media_jobs.MediaJob(binary_path=binary, sidecar_path=sidecar, media_type="audio")
    )

    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.1) == 0

    [failed] = media_jobs.status(vault)["jobs"]
    assert failed["state"] == media_jobs.FAILED
    assert failed["retryable"] is True
    assert failed["error"] == "stale extraction: sidecar content changed"
    assert failed["next_action"] == "review the sidecar changes, then retry media processing"
    assert "USER CANONICAL EDIT" in sidecar.read_text(encoding="utf-8")
    assert "stale transcript" not in sidecar.read_text(encoding="utf-8")


def test_combined_binary_and_sidecar_stale_remains_actionable(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _preserve_media_stub(vault, filename="combined-stale.m4a")
    binary = vault / result.path
    sidecar = vault / result.sidecar_path

    def _change_both(*_args, **_kwargs):
        binary.write_bytes(b"replacement media")
        sidecar.write_text(
            sidecar.read_text(encoding="utf-8") + "\nUSER CANONICAL EDIT\n",
            encoding="utf-8",
        )
        return extract.ExtractResult(
            text="[0:00] stale transcript",
            media_type="audio",
            engine="faster-whisper:test+timed",
        )

    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)
    monkeypatch.setattr(extract, "extract_text", _change_both)
    store = media_jobs.MediaJobStore(vault)
    store.enqueue(
        media_jobs.MediaJob(binary_path=binary, sidecar_path=sidecar, media_type="audio")
    )

    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.1) == 0

    [failed] = media_jobs.status(vault)["jobs"]
    assert failed["state"] == media_jobs.FAILED
    assert failed["error"] == (
        "stale extraction: sidecar content changed, media identity changed"
    )
    assert failed["next_action"] == "review the sidecar changes, then retry media processing"


def test_transient_stable_commit_precondition_is_retried_without_repeating_asr(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _preserve_media_stub(vault, filename="transient-commit.m4a")
    binary = vault / result.path
    sidecar = vault / result.sidecar_path
    worker = media_worker.MediaWorker(vault, execution_mode="inline")
    job = media_worker._Job(binary_path=binary, sidecar_path=sidecar, media_type="audio")
    extraction_calls = 0
    commit_calls = 0
    real_commit = worker._commit_sidecar_extraction

    def _extract_once(*_args, **_kwargs):
        nonlocal extraction_calls
        extraction_calls += 1
        return extract.ExtractResult(
            text="[0:00] recovered transcript",
            media_type="audio",
            engine="faster-whisper:test+timed",
        )

    def _transient_commit(*args, **kwargs):
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 1:
            return False
        return real_commit(*args, **kwargs)

    monkeypatch.setattr(extract, "extract_text", _extract_once)
    monkeypatch.setattr(worker, "_commit_sidecar_extraction", _transient_commit)

    outcome = worker._process(job)

    assert outcome.state == "complete"
    assert extraction_calls == 1
    assert commit_calls == 2
    assert "[0:00] recovered transcript" in sidecar.read_text(encoding="utf-8")


def test_crlf_pending_sidecar_uses_raw_byte_identity_at_transcript_commit(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _preserve_media_stub(vault, filename="windows-crlf.m4a")
    binary = vault / result.path
    sidecar = vault / result.sidecar_path
    sidecar.write_bytes(sidecar.read_bytes().replace(b"\n", b"\r\n"))
    worker = media_worker.MediaWorker(vault, execution_mode="inline")
    job = media_worker._Job(binary_path=binary, sidecar_path=sidecar, media_type="audio")
    monkeypatch.setattr(
        extract,
        "extract_text",
        lambda *_args, **_kwargs: extract.ExtractResult(
            text="[0:00] Windows transcript committed.",
            media_type="audio",
            engine="faster-whisper:test+timed",
        ),
    )

    outcome = worker._process(job)

    assert outcome.state == "complete"
    content = sidecar.read_text(encoding="utf-8")
    assert "processing_state: completed" in content
    assert "[0:00] Windows transcript committed." in content


def test_pending_state_cas_preserves_concurrent_completed_transcript(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _preserve_media_stub(vault, filename="pending-cas.m4a")
    sidecar = vault / result.sidecar_path
    sidecar.write_bytes(sidecar.read_bytes().replace(b"\n", b"\r\n"))
    expected_hash = content_hash(sidecar.read_text(encoding="utf-8"))
    completed_bytes: bytes | None = None
    real_batch = preserve.batch_atomic_write

    def _complete_before_pending_write(*args, **kwargs):
        nonlocal completed_bytes
        completed = sidecar.read_text(encoding="utf-8").replace(
            "extracted_by: pending", "extracted_by: external-asr+timed"
        )
        completed += "\n[0:00] External completed transcript must survive.\n"
        sidecar.write_text(completed, encoding="utf-8")
        completed_bytes = sidecar.read_bytes()
        return real_batch(*args, **kwargs)

    monkeypatch.setattr(preserve, "batch_atomic_write", _complete_before_pending_write)

    updated = preserve.update_sidecar_processing_pending(
        vault,
        sidecar,
        attempts=1,
        expected_hash=expected_hash,
    )

    assert updated is False
    assert completed_bytes is not None
    assert sidecar.read_bytes() == completed_bytes
    assert "extracted_by: external-asr+timed" in sidecar.read_text(encoding="utf-8")


def test_pending_state_cas_updates_unchanged_crlf_sidecar(vault) -> None:
    result = _preserve_media_stub(vault, filename="pending-cas-crlf.m4a")
    sidecar = vault / result.sidecar_path
    sidecar.write_bytes(sidecar.read_bytes().replace(b"\n", b"\r\n"))
    expected_hash = content_hash(sidecar.read_text(encoding="utf-8"))

    updated = preserve.update_sidecar_processing_pending(
        vault,
        sidecar,
        attempts=2,
        expected_hash=expected_hash,
    )

    assert updated is True
    content = sidecar.read_text(encoding="utf-8")
    assert "processing_state: pending" in content
    assert "processing_attempts: 2" in content


def test_clip_compute_stays_outside_guard_and_index_commit_is_inside(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    result = preserve.preserve_bytes(
        vault,
        scope="Yolo",
        category="photos",
        filename="guarded.jpg",
        data=b"\xff\xd8\xff",
        text="beach",
    )
    manager = _RecordingMutationManager(vault)
    worker = media_worker.MediaWorker(vault, execution_mode="inline")

    def embed_outside(_path):
        assert manager.depth == 0
        manager.events.append("clip-embed")
        return np.ones(embeddings.CLIP_DIM, dtype=np.float32)

    def upsert_inside(*_args, **_kwargs):
        assert manager.depth > 0
        manager.events.append("clip-commit")

    monkeypatch.setattr(media_worker, "get_manager", lambda: manager, raising=False)
    monkeypatch.setattr(embeddings, "embed_image", embed_outside)
    monkeypatch.setattr(worker._clip_index, "upsert", upsert_inside)

    worker._process(
        media_worker._Job(
            binary_path=vault / result.path,
            sidecar_path=vault / result.sidecar_path,
            media_type="image",
            do_ocr=False,
            do_clip=True,
        )
    )

    assert manager.events.index("clip-embed") < manager.events.index("guard-enter")
    assert manager.events.index("guard-enter") < manager.events.index("clip-commit")
    assert manager.depth == 0


def test_scene_artifacts_and_reembed_publish_under_guard(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    result = preserve.preserve_bytes(
        vault,
        scope="Yolo",
        category="video",
        filename="guarded.mp4",
        data=b"video",
        text="scene",
    )
    manager = _RecordingMutationManager(vault)
    worker = media_worker.MediaWorker(vault, execution_mode="inline")
    frames = [(1.0, np.ones(embeddings.CLIP_DIM, dtype=np.float32))]
    pairs = [(object(), object())]

    def embed_scenes_outside(_path):
        assert manager.depth == 0
        manager.events.append("scene-embed")
        return frames, pairs

    def clip_commit_inside(*_args, **_kwargs):
        assert manager.depth > 0
        manager.events.append("clip-commit")

    def scene_commit_inside(*_args, **_kwargs):
        assert manager.depth > 0
        manager.events.append("scene-commit")
        return []

    def reembed_inside(*_args, **_kwargs):
        assert manager.depth > 0
        manager.events.append("reembed-derived")

    monkeypatch.setattr(media_worker, "get_manager", lambda: manager, raising=False)
    monkeypatch.setattr(media_worker.scene_frames, "scene_frames_enabled", lambda: True)
    monkeypatch.setattr(embeddings, "embed_video_scenes", embed_scenes_outside)
    monkeypatch.setattr(worker._clip_index, "upsert_frames", clip_commit_inside)
    monkeypatch.setattr(media_worker.scene_frames, "write_scene_frames", scene_commit_inside)
    monkeypatch.setattr(media_worker.index_sync, "upsert_after_write", reembed_inside)

    clip_job = media_worker._Job(
        binary_path=vault / result.path,
        sidecar_path=vault / result.sidecar_path,
        media_type="video",
        do_ocr=False,
        do_clip=True,
    )
    worker._process(clip_job)
    worker._process(
        media_worker._Job(
            binary_path=clip_job.binary_path,
            sidecar_path=clip_job.sidecar_path,
            media_type="video",
            do_ocr=False,
            do_clip=False,
            do_reembed=True,
        )
    )

    assert manager.events.index("scene-embed") < manager.events.index("guard-enter")
    assert "clip-commit" in manager.events
    assert "scene-commit" in manager.events
    assert "reembed-derived" in manager.events
    assert {
        "operation": "background_media_clip_commit",
        "holder_kind": "background",
    } in manager.guard_metadata
    assert {
        "operation": "background_media_scene_frame_commit",
        "holder_kind": "background",
    } in manager.guard_metadata
    assert manager.depth == 0


def test_reembed_publishes_under_canonical_mutation_guard(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _preserve_media_stub(vault, filename="reembed-lease-race.mp3")

    guard_metadata: list[dict[str, str]] = []

    class _RecordingManager:
        @contextmanager
        def mutation_guard(self, _vault, **_metadata):
            guard_metadata.append(_metadata)
            yield

    monkeypatch.setattr(media_worker, "get_manager", lambda: _RecordingManager())
    reembedded: list[Path] = []
    monkeypatch.setattr(
        media_worker.index_sync,
        "upsert_after_write",
        lambda _vault, paths: reembedded.extend(paths),
    )
    worker = media_worker.MediaWorker(vault, execution_mode="inline")
    job = media_worker._Job(
        binary_path=vault / result.path,
        sidecar_path=vault / result.sidecar_path,
        media_type="audio",
        do_ocr=False,
        do_reembed=True,
    )

    worker._process(job)

    assert reembedded == [job.sidecar_path]
    assert guard_metadata == [
        {
            "operation": "background_media_reembed_commit",
            "holder_kind": "background",
        }
    ]


def test_parent_media_change_during_clip_compute_skips_stale_index_and_scenes(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    result = preserve.preserve_bytes(
        vault,
        scope="Yolo",
        category="video",
        filename="changes-during-clip.mp4",
        data=b"original-video",
        text="scene",
    )
    binary = vault / result.path
    manager = _RecordingMutationManager(vault)
    worker = media_worker.MediaWorker(vault, execution_mode="inline")

    def embed_then_replace(_path):
        assert manager.depth == 0
        binary.write_bytes(b"replacement-video-with-new-identity")
        return (
            [(1.0, np.ones(embeddings.CLIP_DIM, dtype=np.float32))],
            [(object(), object())],
        )

    monkeypatch.setattr(media_worker, "get_manager", lambda: manager, raising=False)
    monkeypatch.setattr(media_worker.scene_frames, "scene_frames_enabled", lambda: True)
    monkeypatch.setattr(embeddings, "embed_video_scenes", embed_then_replace)
    monkeypatch.setattr(
        worker._clip_index,
        "upsert_frames",
        lambda *_args, **_kwargs: pytest.fail("stale CLIP vectors were committed"),
    )
    monkeypatch.setattr(
        media_worker.scene_frames,
        "write_scene_frames",
        lambda *_args, **_kwargs: pytest.fail("stale scene frames were committed"),
    )

    worker._process(
        media_worker._Job(
            binary_path=binary,
            sidecar_path=vault / result.sidecar_path,
            media_type="video",
            do_ocr=False,
            do_clip=True,
        )
    )

    assert binary.read_bytes() == b"replacement-video-with-new-identity"
    assert manager.depth == 0


def test_media_commit_exception_releases_mutation_guard(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.writer_lease import LeaseConfig, LeaseManager

    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault, filename="commit-fails.mp3")
    manager = LeaseManager(LeaseConfig(state_dir=vault.parent / "exception-state"))
    monkeypatch.setattr(media_worker, "get_manager", lambda: manager, raising=False)
    monkeypatch.setattr(
        extract,
        "extract_text",
        lambda *_args, **_kwargs: extract.ExtractResult(
            text="transcript", media_type="audio", engine="test"
        ),
    )

    def fail_commit(*_args, **_kwargs):
        raise RuntimeError("commit failed")

    monkeypatch.setattr(preserve, "update_sidecar_extraction", fail_commit)
    worker = media_worker.MediaWorker(vault, execution_mode="inline")

    with pytest.raises(RuntimeError, match="commit failed"):
        worker._process(
            media_worker._Job(
                binary_path=vault / result.path,
                sidecar_path=vault / result.sidecar_path,
                media_type="audio",
            )
        )

    with manager.mutation_guard(vault):
        pass


def test_worker_writes_speaker_labels_and_field(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    # Opt-in diarization output round-trips: labeled turns into the sidecar text AND
    # the distinct speaker labels into a `speakers:` frontmatter list.
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault, filename="meeting2.mp3")
    sidecar = vault / result.sidecar_path
    monkeypatch.setattr(
        extract,
        "extract_text",
        lambda p, media_type=None, vault_root=None: extract.ExtractResult(
            text="[Speaker A]: we shipped it\n[Speaker B]: nice work",
            media_type="audio",
            engine="faster-whisper:test+diarized",
            speakers=[
                {"speaker": "Speaker A", "start": 0.0, "end": 1.0, "text": "we shipped it"},
                {"speaker": "Speaker B", "start": 1.0, "end": 2.0, "text": "nice work"},
            ],
        ),
    )
    w = media_worker.MediaWorker(vault, execution_mode="inline")
    w._process(
        media_worker._Job(binary_path=vault / result.path, sidecar_path=sidecar, media_type="audio")
    )

    body = sidecar.read_text(encoding="utf-8")
    assert "[Speaker A]: we shipped it" in body
    assert "[Speaker B]: nice work" in body
    assert "speakers: [Speaker A, Speaker B]" in body
    assert "extracted_by: faster-whisper:test+diarized" in body


def test_worker_marks_failed_on_extraction_error(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault, filename="bad.mp3")
    sidecar = vault / result.sidecar_path

    def boom(p, media_type=None, vault_root=None):
        raise RuntimeError("corrupt container")

    monkeypatch.setattr(extract, "extract_text", boom)
    w = media_worker.MediaWorker(vault, execution_mode="inline")
    w._process(
        media_worker._Job(binary_path=vault / result.path, sidecar_path=sidecar, media_type="audio")
    )

    body = sidecar.read_text(encoding="utf-8")
    assert "extracted_by: failed:" in body
    assert "extracted_by: pending" not in body  # won't re-loop on restart scan


def test_start_prewarms_asr_off_the_request_path(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    warmed = threading.Event()
    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: True)
    monkeypatch.setattr(extract, "prewarm", warmed.set)
    w = media_worker.MediaWorker(vault, execution_mode="inline")
    w.start()
    try:
        assert warmed.wait(timeout=5.0), "start() should warm ASR in a background thread"
    finally:
        w.stop()


def test_start_skips_asr_prewarm_when_policy_disables_it(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "prewarm", lambda: pytest.fail("prewarm should be skipped"))
    w = media_worker.MediaWorker(vault, execution_mode="inline")
    w.start()
    try:
        pass
    finally:
        w.stop()


def test_start_logs_diarization_readiness(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda v=None: calls.append(v))
    w = media_worker.MediaWorker(vault, execution_mode="inline")
    w.start()
    try:
        assert calls == [vault]
    finally:
        w.stop()


def test_run_extraction_passes_vault_root(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    # Named attribution matches against the worker's vault profile store — the vault
    # must flow through extract_text, not be re-resolved from env.
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault, filename="vaulted.mp3")
    seen: dict = {}

    def _spy(p, media_type=None, vault_root=None):
        seen["vault_root"] = vault_root
        return extract.ExtractResult(text="t", media_type="audio", engine="faster-whisper:test")

    monkeypatch.setattr(extract, "extract_text", _spy)
    w = media_worker.MediaWorker(vault)
    w._process(
        media_worker._Job(
            binary_path=vault / result.path,
            sidecar_path=vault / result.sidecar_path,
            media_type="audio",
        )
    )
    assert seen["vault_root"] == vault


@pytest.mark.parametrize(
    ("filename", "media_type", "do_clip"),
    [("automatic.m4a", "audio", False), ("automatic.mp4", "video", True)],
)
def test_automatic_audio_and_video_jobs_explicitly_require_timestamps(
    vault,
    filename: str,
    media_type: str,
    do_clip: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXOMEM_SEMANTIC_SEGMENTS", raising=False)
    result = _preserve_media_stub(vault, filename=filename)
    calls: list[tuple[str, bool]] = []
    clip_calls: list[str] = []

    def _extract_spy(path, media_type=None, vault_root=None, *, timestamps=False):
        calls.append((media_type, timestamps))
        return extract.ExtractResult(
            text="[0:00] automatic transcript",
            media_type=media_type,
            engine="faster-whisper:test+timed",
        )

    monkeypatch.setattr(extract, "extract_text", _extract_spy)
    worker = media_worker.MediaWorker(vault, execution_mode="inline")
    monkeypatch.setattr(worker, "_run_clip", lambda job: clip_calls.append(job.media_type))

    worker._process(
        media_worker._Job(
            binary_path=vault / result.path,
            sidecar_path=vault / result.sidecar_path,
            media_type=media_type,
            do_clip=do_clip,
        )
    )

    assert calls == [(media_type, True)]
    assert clip_calls == (["video"] if do_clip else [])
    assert "extracted_by: faster-whisper:test+timed" in (
        vault / result.sidecar_path
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("verification", "label"),
    [("anonymous", "Speaker A"), ("profile-matched", "Enrolled Speaker")],
)
def test_worker_persists_speaker_verification_metadata(
    vault,
    verification: str,
    label: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _preserve_media_stub(vault, filename=f"{verification}.m4a")
    extracted = extract.ExtractResult(
        text=f"[0:00] [{label}]: hello",
        media_type="audio",
        engine="faster-whisper:test+timed+diarized",
        speakers=[{"speaker": label, "start": 0.0, "end": 1.0, "text": "hello"}],
    )
    extracted.speaker_verification = verification
    monkeypatch.setattr(extract, "extract_text", lambda *_a, **_kw: extracted)

    worker = media_worker.MediaWorker(vault, execution_mode="inline")
    worker._process(
        media_worker._Job(
            binary_path=vault / result.path,
            sidecar_path=vault / result.sidecar_path,
            media_type="audio",
        )
    )

    body = (vault / result.sidecar_path).read_text(encoding="utf-8")
    assert f"speaker_verification: {verification}" in body
    assert f"[{label}]: hello" in body


def test_worker_does_not_downgrade_human_verified_speaker_state(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _preserve_media_stub(vault, filename="human-reviewed.m4a")
    sidecar = vault / result.sidecar_path
    sidecar.write_text(
        sidecar.read_text(encoding="utf-8").replace(
            "extracted_by: pending",
            "extracted_by: pending\nspeaker_verification: human-verified",
        ),
        encoding="utf-8",
    )
    extracted = extract.ExtractResult(
        text="[0:00] [Enrolled Speaker]: reviewed",
        media_type="audio",
        engine="faster-whisper:test+timed+diarized",
        speakers=[
            {
                "speaker": "Enrolled Speaker",
                "start": 0.0,
                "end": 1.0,
                "text": "reviewed",
            }
        ],
    )
    extracted.speaker_verification = "profile-matched"
    monkeypatch.setattr(extract, "extract_text", lambda *_a, **_kw: extracted)

    media_worker.MediaWorker(vault, execution_mode="inline")._process(
        media_worker._Job(
            binary_path=vault / result.path,
            sidecar_path=sidecar,
            media_type="audio",
        )
    )

    assert "speaker_verification: human-verified" in sidecar.read_text(encoding="utf-8")


def test_worker_unavailable_leaves_pending(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault, filename="later.mp3")
    sidecar = vault / result.sidecar_path

    def unavailable(p, media_type=None, vault_root=None):
        raise extract.ExtractionUnavailable("engine not installed")

    monkeypatch.setattr(extract, "extract_text", unavailable)
    w = media_worker.MediaWorker(vault)
    w._process(
        media_worker._Job(binary_path=vault / result.path, sidecar_path=sidecar, media_type="audio")
    )

    # Engine absent now → stays pending so a provisioned box retries on its restart scan.
    assert "extracted_by: pending" in sidecar.read_text(encoding="utf-8")


def test_scan_pending_reenqueues(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    _preserve_media_stub(vault, filename="one.mp3")
    _preserve_media_stub(vault, filename="two.wav")
    w = media_worker.MediaWorker(vault)
    assert w.scan_pending() == 2


def test_scan_pending_ignores_non_canonical_sidecar_copies(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stray `.md` naming the same binary must not become a second job.

    Syncthing writes `<name>.sync-conflict-<stamp>.md` beside the real sidecar. It
    carries the same `evidence_file:` and `extracted_by: pending`, so the scan
    queued it as work of its own — the binary was then extracted twice, into two
    different files, both of which got embedded.
    """
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    stub = _preserve_media_stub(vault, filename="conflicted.mp3")
    sidecar = Path(stub.sidecar_path)
    if not sidecar.is_absolute():
        sidecar = vault / sidecar
    for stray_name in (
        "conflicted.mp3.sync-conflict-20260728-212129-XEB57HX.md",
        "conflicted.mp3 (copy).md",
    ):
        sidecar.with_name(stray_name).write_text(
            sidecar.read_text(encoding="utf-8"), encoding="utf-8"
        )

    assert media_worker.MediaWorker(vault).scan_pending() == 1
    assert media_jobs.MediaJobStore(vault).counts()["pending"] == 1


def test_worker_clip_embeds_image(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    res = preserve.preserve_bytes(
        vault,
        scope="Yolo",
        category="photos",
        filename="p.jpg",
        data=b"\xff\xd8\xff",
        text="beach",
    )
    monkeypatch.setattr(
        embeddings, "embed_image", lambda p: np.ones(embeddings.CLIP_DIM, dtype=np.float32)
    )
    w = media_worker.MediaWorker(vault)
    w._process(
        media_worker._Job(
            binary_path=vault / res.path,
            sidecar_path=vault / res.sidecar_path,
            media_type="image",
            do_ocr=False,
            do_clip=True,
        )
    )
    assert embeddings.ClipIndex(vault).has(res.path)


def test_scan_unindexed_images_enqueues(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    preserve.preserve_bytes(
        vault, scope="Yolo", category="photos", filename="x.jpg", data=b"\xff\xd8\xff", text="t"
    )
    preserve.preserve_bytes(
        vault, scope="Yolo", category="photos", filename="y.png", data=b"\x89PNG", text="t"
    )
    w = media_worker.MediaWorker(vault)
    assert w._scan_unindexed_images() == 2  # both images queued for CLIP


def test_process_worker_drains_and_exits_after_idle(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault, filename="lifecycle.mp3")
    w = media_worker.MediaWorker(vault, execution_mode="process", idle_seconds=0.15)
    w.start()
    try:
        w.enqueue(
            binary_path=vault / result.path,
            sidecar_path=vault / result.sidecar_path,
            media_type="audio",
            do_ocr=False,
        )
        w.join(timeout=10)
        deadline = time.monotonic() + 10
        from exomem import media_jobs

        while time.monotonic() < deadline and media_jobs.status(vault)["worker_active"]:
            time.sleep(0.02)
        assert media_jobs.status(vault)["worker_active"] is False
    finally:
        w.stop()


def test_child_defers_transient_writer_failure_without_poisoning_job(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)
    result = _preserve_media_stub(vault, filename="lease-race.mp3")
    store = media_jobs.MediaJobStore(vault)
    store.enqueue(
        media_jobs.MediaJob(
            binary_path=vault / result.path,
            sidecar_path=vault / result.sidecar_path,
            media_type="audio",
        )
    )

    monkeypatch.setattr(
        media_worker.MediaWorker,
        "_process",
        lambda _self, _job: (_ for _ in ()).throw(
            media_worker.OpError(
                "WRITER_LEASE_REQUIRED",
                "replica is read-only; current writer is desktop",
            )
        ),
    )

    assert (
        media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.1)
        == media_worker._TRANSIENT_EXIT_CODE
    )

    [status] = media_jobs.status(vault)["jobs"]
    assert status["state"] == media_jobs.PENDING
    assert status["attempts"] == 0
    assert status["error"] is None


def test_child_requeues_guarded_sidecar_sharing_violation_then_succeeds(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)
    monkeypatch.setattr(media_worker, "_writer_authority_available", lambda: True)
    result = _preserve_media_stub(vault, filename="sharing-retry.mp3")
    sidecar = vault / result.sidecar_path
    store = media_jobs.MediaJobStore(vault)
    store.enqueue(
        media_jobs.MediaJob(
            binary_path=vault / result.path,
            sidecar_path=sidecar,
            media_type="audio",
        )
    )
    calls = 0

    def _deny_once(_self, _job):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _sharing_violation(sidecar)
        return media_worker._ProcessOutcome(media_worker._COMPLETE)

    monkeypatch.setattr(media_worker.MediaWorker, "_process", _deny_once)

    assert (
        media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.01)
        == media_worker._LOCK_UNAVAILABLE_EXIT_CODE
    )
    [pending] = media_jobs.status(vault)["jobs"]
    assert pending["state"] == media_jobs.PENDING
    assert pending["attempts"] == 1

    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.01) == 0
    assert media_jobs.status(vault)["jobs"] == []
    assert calls == 2


def test_child_terminalizes_sharing_violation_after_three_attempts(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)
    monkeypatch.setattr(media_worker, "_writer_authority_available", lambda: True)
    result = _preserve_media_stub(vault, filename="sharing-exhausted.mp3")
    sidecar = vault / result.sidecar_path
    store = media_jobs.MediaJobStore(vault)
    store.enqueue(
        media_jobs.MediaJob(
            binary_path=vault / result.path,
            sidecar_path=sidecar,
            media_type="audio",
        )
    )
    monkeypatch.setattr(
        media_worker.MediaWorker,
        "_process",
        lambda _self, _job: (_ for _ in ()).throw(_sharing_violation(sidecar)),
    )

    returncodes = [
        media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.01)
        for _ in range(3)
    ]

    assert returncodes == [
        media_worker._LOCK_UNAVAILABLE_EXIT_CODE,
        media_worker._LOCK_UNAVAILABLE_EXIT_CODE,
        0,
    ]
    [failed] = media_jobs.status(vault)["jobs"]
    assert failed["state"] == media_jobs.FAILED
    assert failed["attempts"] == 3
    assert "PermissionError" in failed["error"]


def test_child_does_not_retry_unrelated_permission_error(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)
    monkeypatch.setattr(media_worker, "_writer_authority_available", lambda: True)
    result = _preserve_media_stub(vault, filename="unrelated-permission.mp3")
    store = media_jobs.MediaJobStore(vault)
    store.enqueue(
        media_jobs.MediaJob(
            binary_path=vault / result.path,
            sidecar_path=vault / result.sidecar_path,
            media_type="audio",
        )
    )
    monkeypatch.setattr(
        media_worker.MediaWorker,
        "_process",
        lambda _self, _job: (_ for _ in ()).throw(
            PermissionError(13, "Access is denied", str(vault / "unrelated.txt"))
        ),
    )

    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.01) == 0

    [failed] = media_jobs.status(vault)["jobs"]
    assert failed["state"] == media_jobs.FAILED
    assert failed["attempts"] == 1


def test_follower_supervisor_does_not_launch_media_child(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _preserve_media_stub(vault, filename="follower-pending.mp3")
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    worker.enqueue(
        binary_path=vault / result.path,
        sidecar_path=vault / result.sidecar_path,
        media_type="audio",
    )
    monkeypatch.setattr(
        media_worker,
        "_writer_authority_available",
        lambda: False,
    )
    monkeypatch.setattr(
        worker,
        "_launch_child",
        lambda: pytest.fail("follower must not launch a media child"),
    )

    thread = threading.Thread(target=worker._supervise)
    thread.start()
    try:
        time.sleep(0.1)
        assert worker._child is None
        assert worker._store is not None
        assert worker._store.counts()[media_jobs.PENDING] == 1
    finally:
        worker._stop_event.set()
        worker._wake.set()
        thread.join(timeout=2)
    assert not thread.is_alive()


@pytest.mark.parametrize(
    "returncode",
    [media_worker._TRANSIENT_EXIT_CODE, media_worker._LOCK_UNAVAILABLE_EXIT_CODE],
)
def test_supervisor_backs_off_after_transient_child_exit(
    vault, monkeypatch: pytest.MonkeyPatch, returncode: int
) -> None:
    result = _preserve_media_stub(vault, filename=f"transient-exit-{returncode}.mp3")
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    worker.enqueue(
        binary_path=vault / result.path,
        sidecar_path=vault / result.sidecar_path,
        media_type="audio",
    )

    class _TransientChild:
        pid = 2_147_483_645

        @staticmethod
        def poll():
            return returncode

    worker._child = _TransientChild()
    monkeypatch.setattr(
        worker,
        "_launch_child",
        lambda: pytest.fail("transient child was relaunched before backoff"),
    )

    thread = threading.Thread(target=worker._supervise)
    thread.start()
    try:
        time.sleep(0.1)
        assert worker._child is None
    finally:
        worker._stop_event.set()
        worker._wake.set()
        thread.join(timeout=2)
    assert not thread.is_alive()


def test_child_marks_unavailable_engine_blocked(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    result = _preserve_media_stub(vault, filename="blocked.mp3")

    def _unavailable(*_args, **_kwargs):
        raise extract.ExtractionUnavailable("engine absent")

    monkeypatch.setattr(extract, "extract_text", _unavailable)
    from exomem import media_jobs

    store = media_jobs.MediaJobStore(vault)
    store.enqueue(
        media_jobs.MediaJob(
            binary_path=vault / result.path,
            sidecar_path=vault / result.sidecar_path,
            media_type="audio",
        )
    )

    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.1) == 0
    assert store.counts()["blocked"] == 1


def test_child_retains_actionable_asr_dependency_failure(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)
    result = _preserve_media_stub(vault, filename="dependency-blocked.m4a")

    def _unavailable(*_args, **_kwargs):
        raise extract.ExtractionUnavailable("ASR backend: install the media extra")

    monkeypatch.setattr(extract, "extract_text", _unavailable)
    store = media_jobs.MediaJobStore(vault)
    store.enqueue(
        media_jobs.MediaJob(
            binary_path=vault / result.path,
            sidecar_path=vault / result.sidecar_path,
            media_type="audio",
        )
    )

    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.1) == 0

    [status] = media_jobs.status(vault)["jobs"]
    assert status["state"] == media_jobs.BLOCKED
    assert status["attempts"] == 1
    assert status["retryable"] is True
    expected_error = "ExtractionUnavailable: ASR backend: install the media extra"
    assert status["error"] == expected_error
    assert status["next_action"] == "install the required media dependency, then retry"
    frontmatter = _parsed_frontmatter(vault / result.sidecar_path)
    assert frontmatter["processing_state"] == "blocked"
    assert frontmatter["processing_attempts"] == 1
    assert frontmatter["processing_error"] == expected_error
    assert frontmatter["processing_retryable"] is True
    assert (
        frontmatter["processing_next_action"]
        == "install the required media dependency, then retry"
    )
    assert frontmatter["evidence_file"] == result.path


def test_child_retains_actionable_corrupt_media_failure(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)
    result = _preserve_media_stub(vault, filename="corrupt.m4a")

    def _corrupt(*_args, **_kwargs):
        raise ValueError("invalid audio container: missing moov atom")

    monkeypatch.setattr(extract, "extract_text", _corrupt)
    store = media_jobs.MediaJobStore(vault)
    store.enqueue(
        media_jobs.MediaJob(
            binary_path=vault / result.path,
            sidecar_path=vault / result.sidecar_path,
            media_type="audio",
        )
    )

    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.1) == 0

    [status] = media_jobs.status(vault)["jobs"]
    assert status["state"] == media_jobs.FAILED
    assert status["attempts"] == 1
    assert status["retryable"] is True
    expected_error = "ValueError: invalid audio container: missing moov atom"
    assert status["error"] == expected_error
    assert status["next_action"] == "repair or replace the media artifact, then retry"
    frontmatter = _parsed_frontmatter(vault / result.sidecar_path)
    assert frontmatter["processing_state"] == "failed"
    assert frontmatter["processing_attempts"] == 1
    assert frontmatter["processing_error"] == expected_error
    assert frontmatter["processing_retryable"] is True
    assert (
        frontmatter["processing_next_action"]
        == "repair or replace the media artifact, then retry"
    )
    assert frontmatter["evidence_file"] == result.path


def test_child_blocks_timestamp_renderer_failure_with_renderer_remediation(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)
    result = _preserve_media_stub(vault, filename="renderer-blocked.m4a")

    def _renderer_unavailable(*_args, **_kwargs):
        raise extract.TimestampRenderingUnavailable("timed renderer: unavailable")

    monkeypatch.setattr(extract, "extract_text", _renderer_unavailable)
    store = media_jobs.MediaJobStore(vault)
    store.enqueue(
        media_jobs.MediaJob(
            binary_path=vault / result.path,
            sidecar_path=vault / result.sidecar_path,
            media_type="audio",
        )
    )

    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.1) == 0

    [status] = media_jobs.status(vault)["jobs"]
    assert status["state"] == media_jobs.BLOCKED
    assert status["error"] == (
        "TimestampRenderingUnavailable: timed renderer: unavailable"
    )
    assert status["next_action"] == "check the timestamp renderer, then retry"
    assert "repair or replace" not in status["next_action"]
    frontmatter = _parsed_frontmatter(vault / result.sidecar_path)
    assert frontmatter["processing_state"] == "blocked"
    assert frontmatter["processing_next_action"] == "check the timestamp renderer, then retry"


def test_success_refreshes_sidecar_before_completing_durable_job(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)
    result = _preserve_media_stub(vault, filename="indexed-success.m4a")
    sidecar = vault / result.sidecar_path
    events: list[str] = []
    original_write = preserve.batch_atomic_write
    original_complete = media_jobs.MediaJobStore.complete

    monkeypatch.setattr(
        extract,
        "extract_text",
        lambda *_a, **_kw: extract.ExtractResult(
            text="[0:00] indexed transcript",
            media_type="audio",
            engine="faster-whisper:test+timed",
        ),
    )

    def _write_and_refresh(*args, **kwargs):
        events.append("sidecar-write-and-index-refresh")
        return original_write(*args, **kwargs)

    def _complete_after_commit(store, job):
        body = sidecar.read_text(encoding="utf-8")
        assert "[0:00] indexed transcript" in body
        assert "extracted_by: faster-whisper:test+timed" in body
        events.append("durable-complete")
        return original_complete(store, job)

    monkeypatch.setattr(preserve, "batch_atomic_write", _write_and_refresh)
    monkeypatch.setattr(media_jobs.MediaJobStore, "complete", _complete_after_commit)
    store = media_jobs.MediaJobStore(vault)
    store.enqueue(
        media_jobs.MediaJob(
            binary_path=vault / result.path,
            sidecar_path=sidecar,
            media_type="audio",
        )
    )

    assert media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.1) == 0

    assert events == ["sidecar-write-and-index-refresh", "durable-complete"]
    assert media_jobs.status(vault)["jobs"] == []


def test_claimed_job_skips_asr_when_sidecar_completed_before_worker_runs(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _preserve_media_stub(vault, filename="claim-race.m4a")
    binary = vault / result.path
    sidecar = vault / result.sidecar_path
    store = media_jobs.MediaJobStore(vault)
    store.enqueue(
        media_jobs.MediaJob(
            binary_path=binary,
            sidecar_path=sidecar,
            media_type="audio",
        )
    )
    claimed = store.claim_next()
    assert claimed is not None
    completed = sidecar.read_text(encoding="utf-8").replace(
        "extracted_by: pending", "extracted_by: external-asr+timed"
    )
    completed += "\n[0:00] Transcript won the startup race.\n"
    sidecar.write_text(completed, encoding="utf-8")
    before_body = sidecar.read_text(encoding="utf-8").split("\n---\n", 1)[1]

    monkeypatch.setattr(
        extract,
        "extract_text",
        lambda *_a, **_kw: pytest.fail("ASR must not run for a completed transcript"),
    )
    worker = media_worker.MediaWorker(vault, execution_mode="inline")

    outcome = worker._process(claimed)

    assert outcome.state == "complete"
    after = sidecar.read_text(encoding="utf-8")
    assert after.split("\n---\n", 1)[1] == before_body
    assert "extracted_by: external-asr+timed" in after
    store.complete(claimed)
    assert media_jobs.status(vault)["jobs"] == []


def test_external_completed_transcript_written_during_asr_wins_final_commit_race(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _preserve_media_stub(vault, filename="commit-race.m4a")
    binary = vault / result.path
    sidecar = vault / result.sidecar_path
    worker = media_worker.MediaWorker(vault, execution_mode="inline")
    job = media_worker._Job(binary_path=binary, sidecar_path=sidecar, media_type="audio")
    external_bytes: bytes | None = None

    def external_completion(*_args, **_kwargs):
        nonlocal external_bytes
        completed = sidecar.read_text(encoding="utf-8").replace(
            "extracted_by: pending", "extracted_by: external-asr+timed"
        ).replace("processing_state: pending", "processing_state: completed")
        completed += "\n[0:00] External transcript must win.\n"
        sidecar.write_text(completed, encoding="utf-8")
        external_bytes = sidecar.read_bytes()
        return extract.ExtractResult(
            text="[0:00] Worker transcript must lose.",
            media_type="audio",
            engine="faster-whisper:test+timed",
        )

    monkeypatch.setattr(extract, "extract_text", external_completion)
    monkeypatch.setattr(media_worker, "_content_digest", lambda _path: "stable")

    outcome = worker._process(job)

    assert outcome.state == "stale"
    assert external_bytes is not None
    assert sidecar.read_bytes() == external_bytes
    assert "Worker transcript must lose" not in sidecar.read_text(encoding="utf-8")


def test_external_completed_transcript_survives_asr_failure_commit_race(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _preserve_media_stub(vault, filename="failure-commit-race.m4a")
    binary = vault / result.path
    sidecar = vault / result.sidecar_path
    worker = media_worker.MediaWorker(vault, execution_mode="inline")
    job = media_worker._Job(binary_path=binary, sidecar_path=sidecar, media_type="audio")
    external_bytes: bytes | None = None

    def external_completion_then_failure(*_args, **_kwargs):
        nonlocal external_bytes
        preserve.update_sidecar_extraction(
            vault,
            sidecar,
            text="[0:00] External transcript must survive ASR failure.",
            engine="external-asr+timed",
            speaker_verification="unavailable",
        )
        external_bytes = sidecar.read_bytes()
        raise ValueError("decoder failed after external completion")

    monkeypatch.setattr(extract, "extract_text", external_completion_then_failure)
    monkeypatch.setattr(media_worker, "_content_digest", lambda _path: "stable")

    outcome = worker._process(job)

    assert outcome.state == "stale"
    assert external_bytes is not None
    assert sidecar.read_bytes() == external_bytes
    content = sidecar.read_text(encoding="utf-8")
    assert "extracted_by: external-asr+timed" in content
    assert "processing_state: completed" in content
    assert "External transcript must survive ASR failure" in content
    assert "extracted_by: failed" not in content
    assert "decoder failed after external completion" not in content

    monkeypatch.setattr(
        extract,
        "extract_text",
        lambda *_a, **_kw: pytest.fail("completed transcript must not be reprocessed"),
    )
    assert worker._process(job).state == "complete"
    assert sidecar.read_bytes() == external_bytes


def test_transcript_index_refresh_failure_is_durable_and_retryable_without_asr(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import deferred_index, index_sync

    result = _preserve_media_stub(vault, filename="index-retry.m4a")
    binary = vault / result.path
    sidecar = vault / result.sidecar_path
    calls = 0

    def transcribe(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return extract.ExtractResult(
            text="[0:00] transcript survives index failure",
            media_type="audio",
            engine="faster-whisper:test+timed",
        )

    monkeypatch.setattr(extract, "extract_text", transcribe)
    monkeypatch.setattr(
        index_sync,
        "upsert_after_write",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("lexical index offline")),
    )
    worker = media_worker.MediaWorker(vault, execution_mode="inline")

    outcome = worker._process(
        media_worker._Job(binary_path=binary, sidecar_path=sidecar, media_type="audio")
    )

    assert outcome.state == "complete"
    assert calls == 1
    assert "transcript survives index failure" in sidecar.read_text(encoding="utf-8")
    status = deferred_index.full_status(vault)
    assert status["count"] == 1
    assert status["next_action"] == f'exomem index --vault "{vault}" --scope vault'

    monkeypatch.setattr(index_sync, "upsert_after_write", lambda *_a, **_kw: True)
    assert index_sync.drain_deferred_work(vault) == 1
    assert deferred_index.full_status(vault)["count"] == 0
    assert calls == 1


def test_process_worker_restart_preserves_blocked_job_until_explicit_retry(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault, filename="restart-blocked.mp3")
    store = media_jobs.MediaJobStore(vault)
    job_id = store.enqueue(
        media_jobs.MediaJob(
            binary_path=vault / result.path,
            sidecar_path=vault / result.sidecar_path,
            media_type="audio",
        )
    )
    claimed = store.claim_next()
    assert claimed is not None and claimed.id == job_id
    error = "ExtractionUnavailable: install the ASR extra"
    store.mark(job_id, media_jobs.BLOCKED, error)
    monkeypatch.setattr(media_worker.MediaWorker, "_supervise", lambda _self: None)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)

    worker = media_worker.MediaWorker(vault, execution_mode="process")
    worker.start()
    try:
        [status] = media_jobs.status(vault)["jobs"]
        assert status["state"] == media_jobs.BLOCKED
        assert status["attempts"] == 1
        assert status["error"] == error
    finally:
        worker.stop()


def test_media_runtime_failure_does_not_deny_core_service(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)

    class _BrokenWorker:
        def __init__(self, _vault):
            raise OSError("ledger unavailable")

    monkeypatch.setattr(media_worker, "MediaWorker", _BrokenWorker)
    assert server_runtime._start_media_worker(vault) is None


def test_supervisor_recovers_jobs_owned_by_crashed_child(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    result = _preserve_media_stub(vault, filename="crash.mp3")
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    assert worker._store is not None
    worker.enqueue(
        binary_path=vault / result.path,
        sidecar_path=vault / result.sidecar_path,
        media_type="audio",
    )
    assert worker._store.claim_next() is not None

    class _CrashedChild:
        pid = 2_147_483_646
        returncode = 1

        @staticmethod
        def poll():
            return 1

    worker._store.set_worker(_CrashedChild.pid, 30.0)
    worker._child = _CrashedChild()
    thread = threading.Thread(target=worker._supervise)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and worker._store.counts()["pending"] == 0:
            time.sleep(0.01)
        assert worker._store.counts()["pending"] == 1
        assert worker._store.counts()["running"] == 0
    finally:
        worker._stop_event.set()
        worker._wake.set()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_find_surfaces_media_fields(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    # Provide text so the sidecar is populated + keyword-findable; media frontmatter is set either way.
    preserve.preserve_bytes(
        vault,
        scope="Yolo",
        category="audio",
        filename="meeting.mp3",
        data=b"X",
        text="quarterly review of the water damage claim",
    )
    find_module.clear_cache()
    hits = find_module.find(vault, query="water damage claim", mode="keyword")
    media = [h for h in hits if "meeting.mp3.md" in h.path]
    assert media, [h.path for h in hits]
    d = media[0].as_dict()
    assert d["media_type"] == "audio"
    assert d["media_file"].endswith("meeting.mp3")
