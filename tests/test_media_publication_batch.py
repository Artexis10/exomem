"""Media results publish in whole batches and never send the graph to a full rebuild.

Real pieces: the media job store, the child/parent handoff, the atomic commit, the graph epoch
(floor, checkpoint, acknowledgement) and the writer-lease commit generation. Only the speech
engine and, where a test names it, one write are replaced.
"""

import logging
import os
from pathlib import Path

import pytest

from exomem import (
    commands,
    deferred_index,
    epistemic_graph,
    extract,
    find as find_module,
    foreground_activity,
    freshness,
    graph_sync,
    media_jobs,
    media_processing,
    media_publication,
    media_worker,
    mutation_lock,
    preserve,
    prior_extraction,
)
from exomem import vault as vault_module
from exomem.asr_facts import AsrFacts


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state"))
    monkeypatch.delenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", raising=False)
    monkeypatch.delenv(extract.ASR_LANGUAGE_ENV, raising=False)
    monkeypatch.setattr(extract, "asr_prewarm_enabled", lambda: False)
    monkeypatch.setattr(extract, "log_diarization_readiness", lambda _vault: None)


def _speech_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    def extract_text(path, *, media_type=None, vault_root=None, timestamps=False, language=None, multilingual=False):
        facts = AsrFacts(
            language=language or "en", language_source="requested" if language else "detected",
            language_mode="single", speech_seconds=40.0, duration_seconds=61.5, policy="fw-policy-1",
        )
        words = "tere maailm" if language else "wrong words"
        return extract.ExtractResult(f"[0:01] {words} {path.name}", "audio", "faster-whisper:test+timed", asr=facts)

    monkeypatch.setattr(extract, "extract_text", extract_text)


def _prime_graph(vault: Path) -> None:
    """A built graph on a live freshness registry, as a serving process holds it.

    The incremental refresh proves its result against the registry's recall delta and a resident
    resolver; without both it queues the paths (`resolver_snapshot_unavailable`) instead.
    """
    note = vault / "Knowledge Base" / "Notes" / "baseline.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(note, "# baseline\n")], vault_root=vault, post_commit_fanout=False
    )
    freshness.seed(
        vault, "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault_module.walk_vault_md(vault)),
    )
    freshness.seed(
        vault, "kb",
        ((str(path), freshness.stat_signature(path)) for path in find_module._walk_md(vault / "Knowledge Base")),
    )
    graph = epistemic_graph.EpistemicGraphIndex(vault)
    graph.rebuild_all()
    epistemic_graph.clear_publication_memos()
    assert graph.available()
    assert graph_sync.classify_epoch(vault).kind == "coherent"


def _preserve_recordings(vault: Path, names: list[str]) -> list[tuple[Path, Path]]:
    pairs = []
    for name in names:
        stub = preserve.preserve_bytes(
            vault, scope="Rental", category="audio", filename=name, data=b"FAKEBYTES-" + name.encode()
        )
        pairs.append((vault / stub.path, vault / stub.sidecar_path))
    return pairs


def _run_child_to_idle(vault: Path) -> None:
    media_worker.run_child(vault, parent_pid=os.getpid(), idle_seconds=0.1)


def _transcribe_all(vault: Path, binaries: list[Path], worker: media_worker.MediaWorker) -> None:
    for binary in binaries:
        media_processing.reconcile_media(vault, binary, explicit=False)
    _run_child_to_idle(vault)
    worker._drain_parent_results()


def test_reprocess_that_creates_a_page_completes_the_graph_without_a_fallback(
    vault, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The prior-extraction page a reprocess creates is part of the batch the graph checkpoint names.

    Before the fix the graph fan-out was told no page was created, so the incremental refresh
    refused the checkpoint (`checkpoint_created_paths_mismatch`) and queued the paths instead.
    """
    _speech_engine(monkeypatch)
    ((binary, sidecar),) = _preserve_recordings(vault, ["rec.mp3"])
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    _transcribe_all(vault, [binary], worker)
    _prime_graph(vault)

    media_processing.reprocess_media(vault, binary, language="et")
    caplog.set_level(logging.INFO, logger="exomem.epistemic_graph")
    _run_child_to_idle(vault)
    worker._drain_parent_results()

    (link,) = prior_extraction.links_of(sidecar.read_text(encoding="utf-8"))
    prior = vault / f"{link}.md"
    assert prior.is_file()
    assert [r.getMessage() for r in caplog.records if "fell back" in r.getMessage()] == []
    assert graph_sync.classify_epoch(vault).kind == "coherent"
    assert graph_sync.status(vault)["state"] == "current"
    assert deferred_index.graph_full_rebuild_pending(vault) is None
    rels = [path.relative_to(vault).as_posix() for path in (sidecar, prior)]
    assert epistemic_graph.EpistemicGraphIndex(vault).indexed_paths(rels) == set(rels)
    assert media_jobs.status(vault)["jobs"] == []


def _recorded_results(vault: Path, names: list[str]) -> tuple[media_worker.MediaWorker, list[tuple[Path, Path]]]:
    """Recordings whose child results are recorded but not yet published, on a built graph."""
    pairs = _preserve_recordings(vault, names)
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    for binary, _sidecar in pairs:
        media_processing.reconcile_media(vault, binary, explicit=False)
    _run_child_to_idle(vault)
    assert media_jobs.MediaJobStore(vault).pending_result_count() == len(names)
    _prime_graph(vault)
    return worker, pairs


def _advances_by_operation(caplog: pytest.LogCaptureFixture) -> dict[str, int]:
    marker = "commit generation advanced by "
    advances: dict[str, int] = {}
    for record in caplog.records:
        message = record.getMessage()
        if message.startswith(marker):
            operation = message[len(marker):]
            advances[operation] = advances.get(operation, 0) + 1
    return advances


def test_a_batch_of_results_takes_one_hold_advances_the_generation_once_and_lands_whole(
    vault, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Each result used to advance the commit generation twice (publication, then graph completion).

    Every advance sets off corpus-sized derived work in the serving process, so a drain of N
    results cost 2N of them. One batch is one hold, one advance and one fan-out.
    """
    _speech_engine(monkeypatch)
    worker, pairs = _recorded_results(vault, ["one.mp3", "two.mp3", "three.mp3"])
    caplog.set_level(logging.INFO, logger="exomem.writer_lease")
    caplog.set_level(logging.INFO, logger="exomem.epistemic_graph")

    worker._drain_parent_results()

    assert _advances_by_operation(caplog) == {"background_media_parent_publication": 1}
    assert [r.getMessage() for r in caplog.records if "fell back" in r.getMessage()] == []
    rels = []
    for _binary, sidecar in pairs:
        text = sidecar.read_text(encoding="utf-8")
        assert media_processing.has_completed_transcript(text, media_type="audio")
        rels.append(sidecar.relative_to(vault).as_posix())
    assert epistemic_graph.EpistemicGraphIndex(vault).indexed_paths(rels) == set(rels)
    assert graph_sync.status(vault)["state"] == "current"
    assert media_jobs.MediaJobStore(vault).pending_result_count() == 0
    assert media_jobs.status(vault)["jobs"] == []


def test_a_result_that_cannot_publish_stops_only_itself_and_publishes_once_when_it_can(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed batch commit rolls the whole batch back; the retry loses no result and repeats none."""
    _speech_engine(monkeypatch)
    worker, pairs = _recorded_results(vault, ["good-a.mp3", "poison.mp3", "good-b.mp3"])
    poison = pairs[1][1]
    real_commit = preserve.commit_media_sidecar_writes
    refused = {"on": True}

    def commit(vault_root, writes, **kwargs):
        if refused["on"] and any(write.path == poison for write in writes):
            raise OSError("disk refused this page")
        return real_commit(vault_root, writes, **kwargs)

    monkeypatch.setattr(preserve, "commit_media_sidecar_writes", commit)

    worker._drain_parent_results()

    assert "extracted_by: pending" in poison.read_text(encoding="utf-8")
    for _binary, sidecar in (pairs[0], pairs[2]):
        assert media_processing.has_completed_transcript(sidecar.read_text(encoding="utf-8"), media_type="audio")
    assert media_jobs.MediaJobStore(vault).pending_result_count() == 1

    refused["on"] = False
    worker._drain_parent_results()

    published = poison.read_text(encoding="utf-8")
    assert media_processing.has_completed_transcript(published, media_type="audio")
    assert published.count("poison.mp3") == 1 or published.count("wrong words") == 1
    assert media_jobs.MediaJobStore(vault).pending_result_count() == 0
    assert media_jobs.status(vault)["jobs"] == []


def test_the_supervisor_waits_for_a_quiet_gap_but_publishes_once_a_result_has_waited_its_floor(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Foreground writes hold publication back; continuous traffic cannot hold it back forever."""
    _speech_engine(monkeypatch)
    worker, pairs = _recorded_results(vault, ["one.mp3", "two.mp3"])
    monkeypatch.setattr(media_publication, "QUIET_GAP_SECONDS", 0.05)
    monkeypatch.setattr(media_publication, "MAX_DEFER_SECONDS", 3600.0)

    with foreground_activity.foreground_scope(vault):
        assert worker._drain_parent_results(gated=True) is False
        waiting = media_jobs.status(vault)["result_publication"]
        assert waiting["pending_count"] == 2
        assert waiting["oldest_age_seconds"] is not None and waiting["oldest_age_seconds"] >= 0.0
        for _binary, sidecar in pairs:
            assert "extracted_by: pending" in sidecar.read_text(encoding="utf-8")

        # Under continuous traffic the oldest result passes its floor and publishes anyway.
        monkeypatch.setattr(media_publication, "MAX_DEFER_SECONDS", 0.0)
        assert worker._drain_parent_results(gated=True) is True

    for _binary, sidecar in pairs:
        assert media_processing.has_completed_transcript(sidecar.read_text(encoding="utf-8"), media_type="audio")
    assert media_jobs.status(vault)["result_publication"] == {"pending_count": 0, "oldest_age_seconds": None}


def test_the_next_batch_waits_out_the_spacing_so_media_advances_the_generation_about_once_a_minute(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    _speech_engine(monkeypatch)
    worker, pairs = _recorded_results(vault, ["one.mp3"])
    monkeypatch.setattr(media_publication, "QUIET_GAP_SECONDS", 0.0)

    assert worker._drain_parent_results(gated=True) is True
    assert media_processing.has_completed_transcript(pairs[0][1].read_text(encoding="utf-8"), media_type="audio")

    media_processing.reprocess_media(vault, pairs[0][0], language="et")
    _run_child_to_idle(vault)
    assert media_jobs.MediaJobStore(vault).pending_result_count() == 1

    worker._last_batch_at = media_worker._clock() - (media_publication.MIN_BATCH_SPACING_SECONDS - 5.0)
    assert worker._drain_parent_results(gated=True) is False
    worker._last_batch_at = media_worker._clock() - (media_publication.MIN_BATCH_SPACING_SECONDS + 1.0)
    assert worker._drain_parent_results(gated=True) is True
    assert media_jobs.MediaJobStore(vault).pending_result_count() == 0


def test_a_multi_path_reprocess_request_queues_all_paths_under_one_hold(
    vault, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A 16-path reprocess call took the boundary and advanced the generation twice for each path."""
    _speech_engine(monkeypatch)
    names = [f"batch-{index}.mp3" for index in range(6)]
    pairs = _preserve_recordings(vault, names)
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    _transcribe_all(vault, [binary for binary, _sidecar in pairs], worker)
    paths = [binary.relative_to(vault).as_posix() for binary, _sidecar in pairs]
    caplog.set_level(logging.INFO, logger="exomem.writer_lease")

    result = commands.op_process_media(vault, paths=paths, operation="reprocess", language="et")

    assert [row["path"] for row in result["results"]] == paths
    assert {row["outcome"] for row in result["results"]} == {"reprocessed"}
    assert _advances_by_operation(caplog) == {"process_media_reprocess_commit": 1}
    queued = {job["path"] for job in media_jobs.status(vault)["jobs"]}
    assert queued == set(paths)


def test_a_reprocess_path_that_needs_a_sidecar_commits_it_outside_the_shared_hold(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path with no page yet needs a sidecar commit, and its index and graph fan-out must not
    run inside the hold that queues the other paths."""
    _speech_engine(monkeypatch)
    pairs = _preserve_recordings(vault, ["done-a.mp3", "done-b.mp3"])
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    _transcribe_all(vault, [binary for binary, _sidecar in pairs], worker)
    fresh = vault / "Knowledge Base" / "Evidence" / "Audio" / "fresh.m4a"
    fresh.parent.mkdir(parents=True, exist_ok=True)
    fresh.write_bytes(b"\x00\x00\x00\x18ftypM4A fake audio")
    paths = [pairs[0][0], fresh, pairs[1][0]]
    rels = [path.relative_to(vault).as_posix() for path in paths]
    fanout_under_hold: list[bool] = []
    real_fanout = media_processing.post_commit_batch_fanout

    def fanout(*args, **kwargs):
        fanout_under_hold.append(mutation_lock.current_thread_holds_boundary())
        return real_fanout(*args, **kwargs)

    monkeypatch.setattr(media_processing, "post_commit_batch_fanout", fanout)

    result = commands.op_process_media(vault, paths=rels, operation="reprocess")

    assert [row["path"] for row in result["results"]] == rels
    assert {row["outcome"] for row in result["results"]} == {"reprocessed"}
    assert fanout_under_hold == [False]
    assert (vault / f"{rels[1]}.md").is_file()
    assert {job["path"] for job in media_jobs.status(vault)["jobs"]} == set(rels)
