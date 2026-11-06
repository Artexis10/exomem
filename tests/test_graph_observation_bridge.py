"""A local callback proves only the exact canonical generation it observed."""

import threading
from contextlib import closing, nullcontext
from pathlib import Path

import pytest
from test_relation_fragment_targets import DUP_TARGET, SOURCE, _built

from exomem import (
    find,
    freshness,
    graph_sync,
    index_sync,
    vault,
    view_lag,
)
from exomem import (
    graph_read_publication as bridge,
)


def _baseline(tmp_path: Path):
    root, index = _built(tmp_path)
    with closing(index._connect_existing(readonly=True)) as conn:
        metadata = dict(conn.execute("SELECT key, value FROM graph_publication_meta"))
    resolver = find.recall_resolver_snapshot(root).fork()
    bridge.forget(root)
    with index._mutation_coordinator.hold():
        assert bridge.record_complete(root, metadata, resolver, full_source_proof=True)
    publication = bridge.published(root)
    assert publication is not None
    return root, index, metadata, publication


def _commit(root: Path, *paths: str) -> None:
    vault.batch_atomic_write(
        [
            vault.PlannedWrite(root / rel, (root / rel).read_text() + "\nUpdated body.\n")
            for rel in paths
        ],
        vault_root=root,
        post_commit_fanout=False,
    )


def test_exact_hidden_callback_keeps_the_complete_publication(tmp_path: Path) -> None:
    root, _index, _metadata, publication = _baseline(tmp_path)
    _commit(root, SOURCE)

    assert not bridge.admissible(root, publication, lambda _rel: False)
    assert index_sync.publish_corpus_delta(root, changed=[root / SOURCE], populate_on_miss=False)

    assert bridge.admissible(root, publication, lambda rel: rel != SOURCE)
    assert not bridge.admissible(root, publication, lambda _rel: True)
    assert bridge.published(root) is publication


@pytest.mark.parametrize("inherited", [False, True])
def test_callback_source_proof_leaves_the_boundary_free_for_another_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inherited: bool,
) -> None:
    root, index, _metadata, publication = _baseline(tmp_path)
    _commit(root, SOURCE)
    coordinator = index._mutation_coordinator
    original = vault.read_bounded_guarded_bytes
    proofs = []

    def read_while_another_writer_enters(*args, **kwargs):
        if args[:2] == (root, SOURCE):
            assert not coordinator.current_thread_holds_boundary()
            entered = []

            def write_boundary():
                with coordinator.try_hold(operation="unrelated_writer") as held:
                    entered.append(held)

            writer = threading.Thread(target=write_boundary)
            writer.start()
            writer.join(timeout=5)
            assert not writer.is_alive()
            assert entered == [True]
            proofs.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(vault, "read_bounded_guarded_bytes", read_while_another_writer_enters)
    with coordinator.hold() if inherited else nullcontext():
        assert index_sync.publish_corpus_delta(root, changed=[root / SOURCE], populate_on_miss=False)
        if inherited:
            assert not proofs
            assert not bridge.admissible(root, publication, lambda _rel: False)
    assert proofs
    assert bridge.admissible(root, publication, lambda _rel: False)


@pytest.mark.parametrize("second", [SOURCE, DUP_TARGET], ids=["repeated", "distinct"])
def test_one_outer_writer_preserves_every_pending_callback_endpoint(
    tmp_path: Path, second: str,
) -> None:
    root, index, _metadata, publication = _baseline(tmp_path)
    with index._mutation_coordinator.hold():
        for path in (SOURCE, second):
            _commit(root, path)
            assert index_sync.publish_corpus_delta(root, changed=[root / path], populate_on_miss=False)
            assert not bridge.admissible(root, publication, lambda _rel: False)
    assert bridge.admissible(root, publication, lambda _rel: False)


def test_a_later_callback_cannot_hide_an_earlier_unproved_endpoint(tmp_path: Path) -> None:
    root, index, _metadata, publication = _baseline(tmp_path)
    with index._mutation_coordinator.hold():
        _commit(root, SOURCE)
        source = root / SOURCE
        source.write_text(source.read_text() + "\nBytes outside the checkpoint.\n")
        assert index_sync.publish_corpus_delta(root, changed=[source], populate_on_miss=False)
        _commit(root, DUP_TARGET)
        assert index_sync.publish_corpus_delta(root, changed=[root / DUP_TARGET], populate_on_miss=False)
    assert not bridge.admissible(root, publication, lambda _rel: False)


def test_foreign_generation_cannot_be_hidden_by_a_later_complete_local_batch(
    tmp_path: Path,
) -> None:
    root, _index, _metadata, publication = _baseline(tmp_path)
    _commit(root, SOURCE)
    _commit(root, DUP_TARGET)

    assert index_sync.publish_corpus_delta(
        root,
        changed=[root / SOURCE, root / DUP_TARGET],
        populate_on_miss=False,
    )

    assert not bridge.admissible(root, publication, lambda _rel: False)


def test_foreign_epoch_before_an_unrelated_local_observation_stays_unavailable(
    tmp_path: Path,
) -> None:
    root, _index, _metadata, publication = _baseline(tmp_path)
    _commit(root, SOURCE)
    other = root / DUP_TARGET
    other.write_text(other.read_text() + "\nUnrelated editor update.\n")

    assert index_sync.publish_corpus_delta(root, changed=[other], populate_on_miss=False)

    assert not bridge.admissible(root, publication, lambda _rel: False)


def test_a_callback_missing_one_epoch_endpoint_cannot_advance_the_bridge(tmp_path: Path) -> None:
    root, _index, _metadata, publication = _baseline(tmp_path)
    _commit(root, SOURCE, DUP_TARGET)

    assert index_sync.publish_corpus_delta(root, changed=[root / SOURCE], populate_on_miss=False)

    assert not bridge.admissible(root, publication, lambda _rel: False)


def test_even_an_unchanged_endpoint_must_appear_in_the_broad_delta(tmp_path: Path) -> None:
    root, _index, _metadata, publication = _baseline(tmp_path)
    source = root / SOURCE
    source.write_text(source.read_text() + "\nChanged alone.\n")
    checkpoint = graph_sync.GraphSyncCheckpoint.create(
        generation=1,
        mutation_id="1234567890abcdef12345678",
        paths=tuple(
            sorted(
                (rel, vault.content_hash((root / rel).read_text())) for rel in (SOURCE, DUP_TARGET)
            )
        ),
        created_paths=(),
    )
    graph_sync._write_floor(root, graph_sync.GraphSyncGenerationFloor.create(1))
    graph_sync._write_checkpoint(root, checkpoint)

    assert index_sync.publish_corpus_delta(
        root,
        changed=[source, root / DUP_TARGET],
        populate_on_miss=False,
    )

    assert not bridge.admissible(root, publication, lambda _rel: False)


@pytest.mark.parametrize("interference", ["reseed", "changed-after-event", "wrong-hash"])
def test_incomplete_or_stale_target_proof_cannot_advance_the_bridge(
    tmp_path: Path,
    interference: str,
) -> None:
    root, _index, _metadata, publication = _baseline(tmp_path)
    _commit(root, SOURCE)
    observation = bridge.begin_observation(root, (root / SOURCE,), ())
    assert observation is not None
    source = root / SOURCE
    if interference == "wrong-hash":
        source.write_text(source.read_text() + "\nBytes outside the checkpoint.\n")
    freshness.on_files_changed(root, changed=[source])
    if interference == "reseed":
        freshness.seed(
            root,
            "vault",
            ((str(path), freshness.stat_signature(path)) for path in vault.walk_vault_md(root)),
        )
    elif interference == "changed-after-event":
        source.write_text(source.read_text() + "\nNot observed by the registry.\n")

    bridge.finish_observation(root, observation)

    assert not bridge.admissible(root, publication, lambda _rel: False)


def test_full_marker_never_supplies_path_observation_proof(tmp_path: Path) -> None:
    root, _index, _metadata, publication = _baseline(tmp_path)
    sibling = root / "Journal/competing-title.md"
    vault.batch_atomic_write(
        [vault.PlannedWrite(sibling, "# Shared title\n")],
        vault_root=root,
        post_commit_fanout=False,
    )

    assert index_sync.publish_corpus_delta(root, changed=[sibling], populate_on_miss=False)

    assert graph_sync.read_checkpoint(root).scope == "full"
    assert not bridge.admissible(root, publication, lambda _rel: False)


def test_duplicate_epoch_callback_cannot_recaption_a_new_local_checkpoint(tmp_path: Path) -> None:
    root, _index, _metadata, publication = _baseline(tmp_path)
    _commit(root, SOURCE)
    assert index_sync.publish_corpus_delta(root, changed=[root / SOURCE], populate_on_miss=False)
    assert index_sync.publish_corpus_delta(root, changed=[root / SOURCE], populate_on_miss=False)
    assert bridge.admissible(root, publication, lambda _rel: False)

    source = root / SOURCE
    source.write_text(source.read_text() + "\nNo new canonical epoch.\n")
    assert index_sync.publish_corpus_delta(root, changed=[source], populate_on_miss=False)

    assert not bridge.admissible(root, publication, lambda _rel: False)


def test_complete_metadata_cannot_recover_a_forgotten_association(tmp_path: Path) -> None:
    root, index, metadata, publication = _baseline(tmp_path)
    bridge.forget(root)

    with index._mutation_coordinator.hold():
        assert not bridge.record_complete(root, metadata, publication.resolver)
        assert bridge.record_complete(
            root,
            metadata,
            publication.resolver,
            full_source_proof=True,
        )

    assert not bridge.admissible(root, publication, lambda _rel: False)
    assert bridge.admissible(root, bridge.published(root), lambda _rel: False)


def test_complete_body_publication_reuses_the_immutable_resolver(tmp_path: Path) -> None:
    root, index, metadata, publication = _baseline(tmp_path)
    replacement = publication.resolver.fork()

    with index._mutation_coordinator.hold():
        assert bridge.record_complete(root, metadata, replacement)

    current = bridge.published(root)
    assert current.resolver is publication.resolver
    assert current.lineage is publication.lineage


def test_a_baseline_rejects_a_resolver_from_a_different_source_set(tmp_path: Path) -> None:
    root, index, metadata, _publication = _baseline(tmp_path)
    bridge.forget(root)

    with index._mutation_coordinator.hold():
        assert not bridge.record_complete(
            root,
            metadata,
            vault.WikilinkResolver.from_entries(root, ()),
            full_source_proof=True,
        )

    assert bridge.published(root) is None


def test_an_epoch_above_the_decision_bound_stays_unavailable(tmp_path: Path) -> None:
    root, _index, _metadata, publication = _baseline(tmp_path)
    writes = [
        vault.PlannedWrite(root / f"Knowledge Base/Notes/additional-{i}.md", "# Additional note\n")
        for i in range(view_lag.MAX_DECIDED_PATHS + 1)
    ]
    vault.batch_atomic_write(writes, vault_root=root, post_commit_fanout=False)

    assert index_sync.publish_corpus_delta(
        root,
        changed=[write.path for write in writes],
        populate_on_miss=False,
    )

    assert not bridge.admissible(root, publication, lambda _rel: False)


def test_an_observed_rename_still_reports_a_deletion_without_a_prior_verdict(
    tmp_path: Path,
) -> None:
    root, index, _metadata, publication = _baseline(tmp_path)
    new_rel = "Knowledge Base/Notes/Insights/renamed-source.md"
    with index._mutation_coordinator.hold():
        epoch = graph_sync.prepare_rename_epoch(root, [(SOURCE, new_rel)])
        (root / SOURCE).rename(root / new_rel)
        graph_sync.publish_rename_epoch(epoch)
    assert index_sync.publish_corpus_delta(
        root,
        changed=[root / new_rel],
        deleted=[root / SOURCE],
        populate_on_miss=False,
    )

    assert bridge.admissible(root, publication, lambda _rel: False)
    assert not bridge.admissible(root, publication, lambda rel: rel == SOURCE)


@pytest.mark.parametrize("advance", ["canonical", "local"])
def test_a_target_advance_during_the_release_decision_invalidates_admission(
    tmp_path: Path,
    advance: str,
) -> None:
    root, _index, _metadata, publication = _baseline(tmp_path)
    _commit(root, SOURCE)
    assert index_sync.publish_corpus_delta(root, changed=[root / SOURCE], populate_on_miss=False)
    advanced = []

    def visible(_rel):
        if not advanced:
            if advance == "canonical":
                _commit(root, DUP_TARGET)
            else:
                other = root / DUP_TARGET
                other.write_text(other.read_text() + "\nAn editor changed the target.\n")
                freshness.on_files_changed(root, changed=[other])
            advanced.append(True)
        return False

    assert not bridge.admissible(root, publication, visible)
    assert advanced
