"""Canonical epochs must cover the vault-wide inputs that graph resolution uses."""

from pathlib import Path

import pytest

from exomem import deferred_index, graph_sync, recall_policy, vault


@pytest.mark.parametrize("include_kb", [False, True], ids=["sibling-only", "mixed-batch"])
def test_canonical_epoch_covers_sibling_markdown_that_can_change_link_resolution(
    tmp_path: Path,
    include_kb: bool,
) -> None:
    sibling = vault.PlannedWrite(tmp_path / "Journal/competing-title.md", "# Shared title\n")
    writes = [sibling]
    if include_kb:
        writes.append(
            vault.PlannedWrite(
                tmp_path / "Knowledge Base/Notes/withheld.md",
                "# Private note\n",
            )
        )

    epoch = graph_sync.epoch_writes(tmp_path, writes)

    assert epoch is not None, "a resolver input changed without a durable epoch"
    checkpoint = graph_sync.GraphSyncCheckpoint.parse(epoch[1].content)
    assert checkpoint is not None
    if checkpoint.scope == "paths":
        assert dict(checkpoint.paths) == {
            write.path.relative_to(tmp_path).as_posix(): vault.content_hash(write.content)
            for write in writes
        }
    else:
        assert checkpoint.scope == "full"


def test_uppercase_markdown_creation_issues_an_epoch(tmp_path: Path) -> None:
    relative = "Knowledge Base/Notes/example.MD"
    write = vault.PlannedWrite(tmp_path / relative, "# Competing title\n")

    epoch = graph_sync.epoch_writes(tmp_path, [write])

    assert epoch is not None
    checkpoint = graph_sync.GraphSyncCheckpoint.parse(epoch[1].content)
    assert checkpoint is not None
    assert checkpoint.paths == ((relative, vault.content_hash(write.content)),)


@pytest.mark.parametrize("was_admitted", [False, True], ids=["created", "suppressed"])
def test_manifest_membership_before_or_after_the_write_issues_an_epoch(
    tmp_path: Path, was_admitted: bool
) -> None:
    from test_records_recall_policy import _manifest

    relative = "Knowledge Base/Records/Measurements/_collection.md"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    admitted = _manifest()
    if was_admitted:
        path.write_text(admitted, encoding="utf-8")
        assert recall_policy.is_recall_candidate(tmp_path, path)
    content = "# Invalid manifest\n" if was_admitted else admitted

    epoch = graph_sync.epoch_writes(tmp_path, [vault.PlannedWrite(path, content)])

    assert epoch is not None
    checkpoint = graph_sync.GraphSyncCheckpoint.parse(epoch[1].content)
    assert checkpoint is not None
    assert checkpoint.paths == ((relative, vault.content_hash(content)),)


def test_raw_structured_data_owes_no_resolver_epoch(tmp_path: Path) -> None:
    path = tmp_path / "Knowledge Base/Records/Measurements/items/example.md"

    epoch = graph_sync.epoch_writes(tmp_path, [vault.PlannedWrite(path, "# Raw row\n")])

    assert epoch is None


def test_sibling_deletion_stages_a_floor_and_keeps_full_recovery_debt(tmp_path: Path) -> None:
    relative = "Journal/competing-title.MD"
    path = tmp_path / relative
    path.parent.mkdir()
    path.write_text("# Shared title\n", encoding="utf-8")

    epoch = graph_sync.prepare_deletion_epoch(tmp_path, [relative])

    assert epoch is not None
    assert epoch.checkpoint.scope == "full"
    assert graph_sync.read_floor(tmp_path).generation == epoch.checkpoint.generation
    assert graph_sync.read_checkpoint(tmp_path) is None
    deferred_index.enqueue_graph_checkpoint(tmp_path, epoch.checkpoint)
    assert deferred_index.graph_full_rebuild_pending(tmp_path) == epoch.checkpoint.generation
    graph_sync.restore_deletion_epoch(epoch)
    assert graph_sync.read_floor(tmp_path) is None


def test_manifest_restore_uses_restored_content_before_destination_exists(tmp_path: Path) -> None:
    from test_records_recall_policy import _manifest

    relative = "Knowledge Base/Records/Measurements/_collection.md"
    content = _manifest()

    epoch = graph_sync.prepare_recovery_epoch(tmp_path, [(relative, content)])

    assert epoch is not None
    assert epoch.checkpoint.paths == ((relative, vault.content_hash(content)),)
    assert epoch.checkpoint.created_paths == (relative,)
    graph_sync.restore_deletion_epoch(epoch)


def test_sibling_batch_commits_the_floor_before_source_and_provisions_full_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "Journal/competing-title.md"
    observed = []

    def observe_source(destination: Path) -> None:
        if destination == path:
            observed.append(graph_sync.read_floor(tmp_path).generation)
            assert graph_sync.read_checkpoint(tmp_path) is None

    monkeypatch.setattr(vault, "_after_batch_destination_published", observe_source)

    vault.batch_atomic_write(
        [vault.PlannedWrite(path, "# Shared title\n")],
        vault_root=tmp_path,
        post_commit_fanout=False,
    )

    checkpoint = graph_sync.read_checkpoint(tmp_path)
    assert checkpoint is not None
    assert checkpoint.scope == "full"
    assert observed == [checkpoint.generation]
    assert path.read_text(encoding="utf-8") == "# Shared title\n"
    assert deferred_index.graph_full_rebuild_pending(tmp_path) == checkpoint.generation


def test_caught_sibling_batch_failure_restores_source_and_both_epoch_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "Journal/competing-title.md"
    path.parent.mkdir()
    path.write_text("# Original title\n", encoding="utf-8")

    def fail_after_source(destination: Path) -> None:
        if destination == path:
            assert graph_sync.read_floor(tmp_path) is not None
            raise RuntimeError("source replacement failed")

    monkeypatch.setattr(vault, "_after_batch_destination_published", fail_after_source)

    with pytest.raises(RuntimeError, match="source replacement failed"):
        vault.batch_atomic_write(
            [vault.PlannedWrite(path, "# Shared title\n")],
            vault_root=tmp_path,
            post_commit_fanout=False,
        )

    assert path.read_text(encoding="utf-8") == "# Original title\n"
    assert graph_sync.read_floor(tmp_path) is None
    assert graph_sync.read_checkpoint(tmp_path) is None


@pytest.mark.parametrize("outside_kb", [False, True], ids=["kb-transform", "sibling-move"])
def test_actual_move_fences_both_endpoints_and_names_the_bytes_the_rename_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outside_kb: bool
) -> None:
    from test_semantic_lifecycle_writers import _source, _write

    from exomem import move_file, writer_lease

    prefix = "Journal" if outside_kb else "Knowledge Base/Notes/Insights"
    old_rel, new_rel = f"{prefix}/before.md", f"{prefix}/after.md"
    source = "# Original observation\n" if outside_kb else _source("Original observation.")
    old = _write(tmp_path, old_rel, source)
    real_rename = move_file._held_rename
    real_publish = graph_sync.publish_rename_epoch
    epochs = []

    def rename_under_floor(root, previous, following, **kwargs):
        assert writer_lease.active_manager().holds_mutation_boundary(root)
        assert graph_sync.read_floor(root) is not None
        assert graph_sync.read_checkpoint(root) is None
        return real_rename(root, previous, following, **kwargs)

    def capture_rename_epoch(epoch):
        epochs.append(epoch.checkpoint)
        assert not old.exists()
        assert (tmp_path / new_rel).read_text(encoding="utf-8") == source
        return real_publish(epoch)

    monkeypatch.setattr(move_file, "_held_rename", rename_under_floor)
    monkeypatch.setattr(graph_sync, "publish_rename_epoch", capture_rename_epoch)

    move_file.move_file(
        tmp_path,
        old_path=old_rel,
        new_path=new_rel,
        update_wikilinks=False,
        content_transform=None if outside_kb else lambda text: text.replace("Original", "Changed"),
    )

    assert len(epochs) == 1
    if outside_kb:
        assert epochs[0].scope == "full"
    else:
        assert dict(epochs[0].paths) == {old_rel: None, new_rel: vault.content_hash(source)}
    assert not old.exists()
    expected = source if outside_kb else source.replace("Original", "Changed")
    assert (tmp_path / new_rel).read_text(encoding="utf-8") == expected


def test_caught_move_child_rollback_restores_the_rename_and_epoch(tmp_path, monkeypatch):
    from test_semantic_lifecycle_writers import _source, _write

    from exomem import move_file

    old_rel = "Knowledge Base/Notes/Insights/before.md"
    new_rel = "Knowledge Base/Notes/Insights/after.md"
    source = _source("Original observation.")
    old = _write(tmp_path, old_rel, source)

    def fail_after_transformed_source(destination):
        if destination == tmp_path / new_rel:
            raise RuntimeError("child source replacement failed")

    monkeypatch.setattr(vault, "_after_batch_destination_published", fail_after_transformed_source)

    with pytest.raises(RuntimeError, match="child source replacement failed"):
        move_file.move_file(
            tmp_path,
            old_path=old_rel,
            new_path=new_rel,
            update_wikilinks=False,
            content_transform=lambda text: text.replace("Original", "Changed"),
        )

    assert old.read_text(encoding="utf-8") == source
    assert not (tmp_path / new_rel).exists()
    assert graph_sync.read_floor(tmp_path) is None
    assert graph_sync.read_checkpoint(tmp_path) is None


@pytest.mark.parametrize("committed", [True, False], ids=["committed-cleanup", "partial-rollback"])
def test_uncertain_child_outcome_keeps_the_move_and_full_repair(tmp_path, monkeypatch, committed):
    from test_semantic_lifecycle_writers import _source, _write

    from exomem import move_file

    old_rel = "Knowledge Base/Notes/Insights/before.md"
    new_rel = "Knowledge Base/Notes/Insights/after.md"
    source = _source("Original observation.")
    old = _write(tmp_path, old_rel, source)
    real_batch = move_file.batch_atomic_write

    def fail_after_canonical_commit(writes, **kwargs):
        writes = list(writes)
        real_batch(writes, **kwargs)
        raise vault.BatchWriteError(
            "BATCH_CLEANUP_INCOMPLETE" if committed else "BATCH_ROLLBACK_INCOMPLETE",
            vault._summarize_batch_targets(writes, tmp_path),
            committed,
        )

    monkeypatch.setattr(move_file, "batch_atomic_write", fail_after_canonical_commit)

    with pytest.raises(vault.BatchWriteError):
        move_file.move_file(
            tmp_path,
            old_path=old_rel,
            new_path=new_rel,
            update_wikilinks=False,
            content_transform=lambda text: text.replace("Original", "Changed"),
        )

    assert not old.exists()
    assert (tmp_path / new_rel).read_text(encoding="utf-8") == source.replace("Original", "Changed")
    checkpoint = graph_sync.read_checkpoint(tmp_path)
    assert checkpoint is not None
    assert dict(checkpoint.paths)[new_rel] == vault.content_hash(
        source.replace("Original", "Changed")
    )
    assert deferred_index.graph_full_rebuild_pending(tmp_path) >= checkpoint.generation


def test_post_rename_failure_keeps_the_floor_and_recovery_custody(tmp_path, monkeypatch):
    from test_semantic_lifecycle_writers import _source, _write

    from exomem import move_file

    old_rel = "Knowledge Base/Notes/Insights/before.md"
    new_rel = "Knowledge Base/Notes/Insights/after.md"
    source = _source("Original observation.")
    old = _write(tmp_path, old_rel, source)
    real_rename = move_file._held_rename

    def fail_after_actual_rename(*args, **kwargs):
        real_rename(*args, **kwargs)
        raise move_file.MoveFileError("MOVE_FAILED", "post-rename identity check failed")

    monkeypatch.setattr(move_file, "_held_rename", fail_after_actual_rename)

    with pytest.raises(move_file.MoveFileError):
        move_file.move_file(tmp_path, old_path=old_rel, new_path=new_rel, update_wikilinks=False)

    assert not old.exists()
    assert (tmp_path / new_rel).read_text(encoding="utf-8") == source
    assert graph_sync.read_checkpoint(tmp_path) is None
    floor = graph_sync.read_floor(tmp_path)
    assert floor is not None
    assert deferred_index.graph_full_rebuild_pending(tmp_path) == floor.generation
