"""Media results publish in whole batches and never send the graph to a full rebuild.

Real pieces: the media job store, the child/parent handoff, the atomic commit, the graph epoch
(floor, checkpoint, acknowledgement) and the writer-lease commit generation. Only the speech
engine and, where a test names it, one write are replaced.
"""

import logging
import os
import sqlite3
from pathlib import Path

import pytest

from exomem import (
    commands,
    deferred_index,
    epistemic_graph,
    extract,
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
from exomem import (
    find as find_module,
)
from exomem import vault as vault_module
from exomem.asr_facts import AsrFacts
from exomem.cli_ops import OpError
from exomem.vault_store import media as store_media


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
    caplog.clear()  # count only the measured call; another test may have enabled INFO already

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
    assert deferred_index.snapshot_full(vault) == []
    assert media_jobs.MediaJobStore(vault).pending_result_count() == 0
    assert media_jobs.status(vault)["jobs"] == []


def test_a_batch_that_cannot_commit_rolls_back_whole_and_the_failing_result_is_bisected_out(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed commit loses no result; bisecting exactly the failed results isolates the failing one
    in two failed holds, while others still wait behind a read window smaller than the backlog."""
    _speech_engine(monkeypatch)
    monkeypatch.setattr(media_publication, "BATCH_RESULT_LIMIT", 2)
    names = ["good-0.mp3", "good-1.mp3", "good-2.mp3", "poison.mp3", "good-4.mp3", "good-5.mp3"]
    worker, pairs = _recorded_results(vault, names)
    poison = pairs[3][1]
    good = [sidecar for _binary, sidecar in pairs if sidecar != poison]
    real_commit = preserve.commit_media_sidecar_writes
    refused = {"on": True, "commits": 0}

    def commit(vault_root, writes, **kwargs):
        if refused["on"] and any(write.path == poison for write in writes):
            refused["commits"] += 1
            raise OSError("disk refused this page")
        return real_commit(vault_root, writes, **kwargs)

    monkeypatch.setattr(preserve, "commit_media_sidecar_writes", commit)

    worker._drain_parent_results()  # good-0 and good-1
    worker._drain_parent_results()  # good-2 and the poison: the batch rolls back whole

    assert "extracted_by: pending" in good[2].read_text(encoding="utf-8")
    assert "extracted_by: pending" in poison.read_text(encoding="utf-8")
    assert media_jobs.MediaJobStore(vault).pending_result_count() == 4
    assert worker._last_batch_at is not None  # the hold ran and advanced the generation, so it spaces the next

    for _ in range(3):
        worker._drain_parent_results()

    for sidecar in good:
        assert media_processing.has_completed_transcript(sidecar.read_text(encoding="utf-8"), media_type="audio")
    assert "extracted_by: pending" in poison.read_text(encoding="utf-8")
    (stuck,) = media_jobs.MediaJobStore(vault).pending_results()
    assert list(worker._set_aside) == [(stuck.job_id, stuck.claim_revision)]
    assert refused["commits"] == 2  # the failed batch, then the poison alone

    refused["on"] = False
    worker._drain_parent_results()  # nothing else waits, so this pass retries the set-aside result alone

    published = poison.read_text(encoding="utf-8")
    assert media_processing.has_completed_transcript(published, media_type="audio")
    assert published.count("wrong words poison.mp3") == 1
    assert worker._set_aside == {}
    assert deferred_index.snapshot_full(vault) == []
    assert media_jobs.MediaJobStore(vault).pending_result_count() == 0
    assert media_jobs.status(vault)["jobs"] == []


def test_a_set_aside_result_stays_out_of_the_others_batches_but_still_gets_its_turn_at_the_floor(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting a failing result aside keeps it from failing every batch of the others, and a queue that
    never runs dry still retries it once it has waited the progress floor."""
    _speech_engine(monkeypatch)
    monkeypatch.setattr(media_publication, "BATCH_RESULT_LIMIT", 1)
    worker, pairs = _recorded_results(vault, ["poison.mp3", "good-1.mp3", "good-2.mp3"])
    poison, good_1, good_2 = (sidecar for _binary, sidecar in pairs)
    real_commit = preserve.commit_media_sidecar_writes
    refused = {"on": True, "commits": 0}

    def commit(vault_root, writes, **kwargs):
        if refused["on"] and any(write.path == poison for write in writes):
            refused["commits"] += 1
            raise OSError("disk refused this page")
        return real_commit(vault_root, writes, **kwargs)

    monkeypatch.setattr(preserve, "commit_media_sidecar_writes", commit)

    worker._drain_parent_results()  # the poison alone fails and is set aside
    worker._drain_parent_results()  # good-1 publishes; the poison waits for its floor

    assert refused["commits"] == 1
    assert media_processing.has_completed_transcript(good_1.read_text(encoding="utf-8"), media_type="audio")
    assert "extracted_by: pending" in good_2.read_text(encoding="utf-8")

    monkeypatch.setattr(media_publication, "MAX_DEFER_SECONDS", 0.0)
    worker._drain_parent_results()  # good-2 publishes, and the floor gives the poison its turn

    assert media_processing.has_completed_transcript(good_2.read_text(encoding="utf-8"), media_type="audio")
    assert refused["commits"] == 2


def test_a_result_that_cannot_stage_stays_pending_while_the_others_commit_in_the_same_hold(
    vault, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Staging names its result, so its failure costs the others no second hold and sets nothing aside."""
    _speech_engine(monkeypatch)
    worker, pairs = _recorded_results(vault, ["good-a.mp3", "poison.mp3", "good-b.mp3"])
    poison_sidecar = pairs[1][1]
    real_writes = worker._extraction_writes

    def extraction_writes(job, *args, **kwargs):
        if job.sidecar_path == poison_sidecar:
            raise OSError("cannot render this page")
        return real_writes(job, *args, **kwargs)

    monkeypatch.setattr(worker, "_extraction_writes", extraction_writes)
    caplog.set_level(logging.INFO, logger="exomem.writer_lease")
    caplog.clear()  # count only the measured call; another test may have enabled INFO already

    worker._drain_parent_results()

    assert _advances_by_operation(caplog) == {"background_media_parent_publication": 1}
    for _binary, sidecar in (pairs[0], pairs[2]):
        assert media_processing.has_completed_transcript(sidecar.read_text(encoding="utf-8"), media_type="audio")
    assert "extracted_by: pending" in poison_sidecar.read_text(encoding="utf-8")
    assert media_jobs.MediaJobStore(vault).pending_result_count() == 1
    assert worker._set_aside == {} and worker._suspects == []


def test_a_refused_boundary_leaves_the_whole_batch_pending_and_costs_one_attempt(
    vault, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A refused acquire ran nothing: the batch waits, whole, instead of retrying result by result."""
    _speech_engine(monkeypatch)
    worker, _pairs = _recorded_results(vault, ["one.mp3", "two.mp3", "three.mp3"])
    real_hold = store_media.holding_boundary
    attempts: list[str] = []

    def refuse_once(root, operation, unit, guard):
        attempts.append(operation)
        if len(attempts) == 1:
            raise OpError(
                "MUTATION_BUSY", "another writer holds the vault", details={"status": "retryable", "committed": False}
            )
        return real_hold(root, operation, unit, guard)

    monkeypatch.setattr(store_media, "holding_boundary", refuse_once)
    caplog.set_level(logging.INFO, logger="exomem.writer_lease")
    caplog.clear()  # count only the measured call; another test may have enabled INFO already

    worker._drain_parent_results()

    assert attempts == ["background_media_parent_publication"]
    assert media_jobs.MediaJobStore(vault).pending_result_count() == 3
    assert worker._last_batch_at is None  # no hold ran, so nothing spaces the retry
    assert _advances_by_operation(caplog) == {}

    worker._drain_parent_results()

    assert media_jobs.MediaJobStore(vault).pending_result_count() == 0
    assert _advances_by_operation(caplog) == {"background_media_parent_publication": 1}


def test_the_supervisor_waits_for_a_quiet_gap_but_publishes_once_a_result_has_waited_its_floor(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Foreground writes hold publication back; continuous traffic cannot hold it back forever."""
    _speech_engine(monkeypatch)
    worker, pairs = _recorded_results(vault, ["one.mp3", "two.mp3"])
    monkeypatch.setattr(media_publication, "QUIET_GAP_SECONDS", 0.05)
    monkeypatch.setattr(media_publication, "MAX_DEFER_SECONDS", 3600.0)
    elapsed = [0.0]
    real_clock = media_worker._clock
    monkeypatch.setattr(media_worker, "_clock", lambda: real_clock() + elapsed[0])

    with foreground_activity.foreground_scope(vault):
        assert worker._drain_parent_results(gated=True) is False
        waiting = media_jobs.status(vault)["result_publication"]
        assert waiting["pending_count"] == 2
        assert waiting["oldest_age_seconds"] is not None and waiting["oldest_age_seconds"] >= 0.0
        for _binary, sidecar in pairs:
            assert "extracted_by: pending" in sidecar.read_text(encoding="utf-8")

        # Under continuous traffic the oldest result passes its floor and publishes anyway.
        elapsed[0] = media_publication.MAX_DEFER_SECONDS + 1.0
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


def _age_recorded_results(vault: Path, seconds: float) -> None:
    with sqlite3.connect(media_jobs.job_store_path(vault)) as conn:
        conn.execute("UPDATE media_job_results SET created_at = created_at - ?", (seconds,))


def test_a_result_that_never_stages_opens_the_progress_floor_once_per_floor_not_every_pass(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The oldest result stays old when it cannot publish; the floor also counts from the last batch."""
    _speech_engine(monkeypatch)
    _recorded_results(vault, ["stuck.mp3"])
    _age_recorded_results(vault, 10 * media_publication.MAX_DEFER_SECONDS)
    monkeypatch.setattr(media_publication, "QUIET_GAP_SECONDS", 3600.0)  # foreground writes never pause

    def due(since_last_batch: float | None) -> bool:
        worker = media_worker.MediaWorker(vault, execution_mode="process")
        if since_last_batch is not None:
            worker._last_batch_at = media_worker._clock() - since_last_batch
        with foreground_activity.foreground_scope(vault):
            return worker._publication_due()

    assert due(None) is True  # before any batch, the result's own age opens the floor
    assert due(media_publication.MIN_BATCH_SPACING_SECONDS + 1.0) is False
    assert due(media_publication.MAX_DEFER_SECONDS + 1.0) is True


def test_a_deferred_publication_reads_the_queue_once_not_every_pass(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    """The supervisor polls every half second while foreground writes are busy; the floor is read once."""
    _speech_engine(monkeypatch)
    worker, _pairs = _recorded_results(vault, ["one.mp3"])
    monkeypatch.setattr(media_publication, "QUIET_GAP_SECONDS", 3600.0)
    reads: list[None] = []
    real_facts = worker._store.pending_result_facts
    monkeypatch.setattr(worker._store, "pending_result_facts", lambda: reads.append(None) or real_facts())

    with foreground_activity.foreground_scope(vault):
        for _ in range(5):
            assert worker._publication_due() is False

    assert len(reads) == 1


def test_the_status_says_unknown_when_the_queue_cannot_be_read_and_zero_when_the_table_is_absent() -> None:
    class _Locked:
        def execute(self, *_args):
            raise sqlite3.OperationalError("database is locked")

    absent = sqlite3.connect(":memory:")  # a store from before the results table existed

    assert media_jobs._read_publication_facts(_Locked()) == (None, None)
    assert media_jobs._read_publication_facts(absent) == (0, None)


def _boundary_held_during(monkeypatch: pytest.MonkeyPatch, name: str) -> list[bool]:
    """Wrap `media_processing.<name>` to record whether the vault boundary is held while it runs."""
    seen: list[bool] = []
    real = getattr(media_processing, name)

    def wrapper(*args, **kwargs):
        seen.append(mutation_lock.current_thread_holds_boundary())
        return real(*args, **kwargs)

    monkeypatch.setattr(media_processing, name, wrapper)
    return seen


def test_a_multi_path_reprocess_request_queues_completed_paths_without_advancing_the_generation(
    vault, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Queueing a path whose transcript is complete touches only the machine-local job store.

    It used to take the boundary and advance the commit generation twice for each path, and every
    advance sets off corpus-sized derived work in the serving process.
    """
    _speech_engine(monkeypatch)
    names = [f"batch-{index}.mp3" for index in range(6)]
    pairs = _preserve_recordings(vault, names)
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    _transcribe_all(vault, [binary for binary, _sidecar in pairs], worker)
    paths = [binary.relative_to(vault).as_posix() for binary, _sidecar in pairs]
    caplog.set_level(logging.INFO, logger="exomem.writer_lease")
    caplog.clear()  # count only the measured call; another test may have enabled INFO already

    result = commands.op_process_media(vault, paths=paths, operation="reprocess", language="et")

    assert [row["path"] for row in result["results"]] == paths
    assert {row["outcome"] for row in result["results"]} == {"reprocessed"}
    assert _advances_by_operation(caplog) == {}
    assert {job["path"] for job in media_jobs.status(vault)["jobs"]} == set(paths)


def test_a_reprocess_request_plans_every_path_outside_the_boundary_and_commits_a_new_page_once(
    vault, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Planning hashes the whole file, and hydrates an evicted original first; neither may run under
    the shared boundary, where one large file would stall every foreground write."""
    _speech_engine(monkeypatch)
    pairs = _preserve_recordings(vault, ["done-a.mp3", "done-b.mp3"])
    worker = media_worker.MediaWorker(vault, execution_mode="process")
    _transcribe_all(vault, [binary for binary, _sidecar in pairs], worker)
    fresh = vault / "Knowledge Base" / "Evidence" / "Audio" / "fresh.m4a"
    fresh.parent.mkdir(parents=True, exist_ok=True)
    fresh.write_bytes(b"\x00\x00\x00\x18ftypM4A fake audio")
    rels = [path.relative_to(vault).as_posix() for path in (pairs[0][0], fresh, pairs[1][0])]
    planning_under_boundary = _boundary_held_during(monkeypatch, "_read_provenance")
    caplog.set_level(logging.INFO, logger="exomem.writer_lease")
    caplog.clear()  # count only the measured call; another test may have enabled INFO already

    result = commands.op_process_media(vault, paths=rels, operation="reprocess")

    assert [row["path"] for row in result["results"]] == rels
    assert {row["outcome"] for row in result["results"]} == {"reprocessed"}
    assert planning_under_boundary and not any(planning_under_boundary)
    assert _advances_by_operation(caplog) == {"process_media_reprocess_commit": 1}  # the new page only
    assert (vault / f"{rels[1]}.md").is_file()
    assert {job["path"] for job in media_jobs.status(vault)["jobs"]} == set(rels)


def test_a_runtime_that_goes_down_between_the_plan_and_the_queue_hold_blocks_the_path_as_before(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The queue hold writes no sidecar, so a path that must show a blocked page runs again with the
    advancing hold: the request row, the job and the page all say blocked, as they do without the hold."""
    _speech_engine(monkeypatch)
    ((binary, sidecar),) = _preserve_recordings(vault, ["done.mp3"])
    media_processing.reconcile_media(vault, binary, explicit=False)  # queued, not yet transcribed
    rel = binary.relative_to(vault).as_posix()
    real_committing = media_processing._committing

    def runtime_goes_down(root, guard, operation, unit):
        if operation == "media_reconcile_commit":
            media_processing.set_media_runtime_unavailable(root, reason="engine unavailable", next_action="restart the engine")
        return real_committing(root, guard, operation, unit)

    monkeypatch.setattr(media_processing, "_committing", runtime_goes_down)
    try:
        (row,) = commands.op_process_media(vault, paths=[rel], operation="reprocess")["results"]
    finally:
        media_processing.set_media_runtime_available(vault)

    assert (row["outcome"], row["state"], row["code"]) == ("failed", "blocked", "MEDIA_BLOCKED")
    assert "processing_state: blocked" in sidecar.read_text(encoding="utf-8")
    (job,) = media_jobs.status(vault)["jobs"]
    assert job["state"] == "blocked"
    assert job["error"] == "engine unavailable"
