"""Per-phase write-path timing spans, service metrics, and caller attribution.

Reads have carried `FindTimings` from the start (18 stages, surfaced through
`ask_memory(include_timings=...)`); writes carried nothing. That is why the
55-70s commits observed on a ~2,900-page vault could not be attributed to a
phase at all — the whole preflight+commit path was one opaque number.

These tests pin the write-side instrument:

* the stage inventory an instrumented edit must report,
* the response staying byte-identical when the instrument is not asked for,
* a forced validity-stamp mismatch showing up as a real `commit.revalidate`
  span rather than silently doubling the write,
* the unconditional `exomem_write_*` service histograms — including the one
  emitted when the mutation boundary is never acquired at all,
* the phase label that keeps a revalidation's nested preflight from being
  double-counted as a second first-attempt preflight,
* the corpus caller/outcome attribution that tells a warm census reuse apart
  from a cold rebuild.
"""

from __future__ import annotations

import datetime as dt
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from exomem import activation_manifest, semantic_contract, semantic_writes, writer_lease
from exomem import edit as edit_module
from exomem import metrics as metrics_module
from exomem import mutation_timings as mutation_timings_module

TODAY = dt.date(2026, 6, 1)
PAGE = "Knowledge Base/Notes/Insights/timings.md"
PAGE_ID = "00000000-0000-4000-8000-0000000005a1"
BEFORE_LINE = "Legacy prose without semantic units."
AFTER_LINE = "Updated legacy prose without semantic units."

# Stages every governed existing-page write must report. The conditional ones
# (`preflight.relation_review`, `commit.manifest`, `commit.resolver_prime`,
# `commit.revalidate`) are asserted where they are actually taken.
_ALWAYS_TAKEN_STAGES = (
    "preflight.read_guarded",
    "preflight.registries",
    "preflight.corpus_context",
    "preflight.page_states",
    "preflight.contract_eval",
    "preflight.validity_token",
    "commit.embedding_prewarm",
    "commit.boundary_acquire",
    "commit.stamp_check",
    "commit.creation_lock",
    "commit.locked_commit",
)


def _canonical(payload: object) -> str:
    """JSON with the one legitimately per-call value pinned.

    A transition token carries a fresh `transition_id` UUID per mint, so two
    otherwise identical writes never serialize the same bytes. Everything
    else in the envelope must.
    """

    def scrub(value: object) -> object:
        if isinstance(value, dict):
            return {
                key: ("<token>" if key == "transition_token" else scrub(item))
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    return json.dumps(scrub(payload), sort_keys=True)


def _normalize_advisory_vault(payload: dict, root: Path) -> None:
    """Pin only the fixture vault after checking its internal advisory identity."""
    context = payload["_relation_advisory_context"]
    assert set(context) == {"vault", "registry_hash"}
    assert context["vault"] == str(root)
    context["vault"] = "<vault>"


def _source(body: str = BEFORE_LINE) -> str:
    return (
        "---\n"
        "title: Timings\n"
        "type: insight\n"
        "status: active\n"
        f"exomem_id: {PAGE_ID}\n"
        "---\n\n"
        f"{body}\n\n"
        "## Relations\n"
    )


def _seed(root: Path) -> Path:
    """A grandfathered active compiled page: an ordinary non-worsening edit."""
    path = root / PAGE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_source(), encoding="utf-8")
    activation_manifest.ensure_manifest(root)
    return path


def _run_edit(root: Path) -> dict:
    _seed(root)
    result = edit_module.edit(
        root,
        path=PAGE,
        why="instrument the write path",
        old_string=BEFORE_LINE,
        new_string=AFTER_LINE,
        today=TODAY,
    )
    return result.as_dict()


def _direct_write(
    root: Path, *, bump: bool, timings: mutation_timings_module.MutationTimings | None
) -> dict:
    """One preflight+commit pair, optionally with a stale commit generation."""
    path = _seed(root)
    after_source = path.read_text(encoding="utf-8").replace(BEFORE_LINE, AFTER_LINE)
    preflight = semantic_writes.preflight_existing(
        root,
        path=PAGE,
        after_source=after_source,
        operation="edit",
        timings=timings,
    )
    if bump:
        writer_lease._bump_commit_generation(
            writer_lease.active_manager().config.state_dir, root
        )
    committed = semantic_writes.commit_existing(root, preflight=preflight, timings=timings)
    return committed.as_dict()


def test_edit_write_reports_every_write_phase(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_WRITE_TIMINGS", "1")

    payload = _run_edit(tmp_path)

    timings = payload["timings"]
    assert set(timings) == {"total_ms", "boundary", "stages"}
    assert set(timings["boundary"]) == {"waited_ms"}
    assert isinstance(timings["boundary"]["waited_ms"], float)
    stages = timings["stages"]
    for name in _ALWAYS_TAKEN_STAGES:
        assert name in stages, f"missing stage {name!r}; got {sorted(stages)}"
        assert isinstance(stages[name]["ms"], float), name
    measured = [entry["ms"] for entry in stages.values() if "ms" in entry]
    assert timings["total_ms"] >= max(measured)


def test_write_response_is_byte_identical_without_the_flag(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("EXOMEM_WRITE_TIMINGS", raising=False)
    control = _run_edit(tmp_path / "control")

    monkeypatch.setenv("EXOMEM_WRITE_TIMINGS", "1")
    instrumented = _run_edit(tmp_path / "instrumented")

    assert "timings" not in control
    assert "timings" in instrumented
    del instrumented["timings"]
    _normalize_advisory_vault(control["semantic"], tmp_path / "control")
    _normalize_advisory_vault(instrumented["semantic"], tmp_path / "instrumented")
    assert _canonical(instrumented) == _canonical(control)


def test_semantic_write_payloads_ignore_the_timings_kwarg(tmp_path: Path) -> None:
    control = _direct_write(tmp_path / "control", bump=False, timings=None)
    timed = _direct_write(
        tmp_path / "timed",
        bump=False,
        timings=mutation_timings_module.MutationTimings(),
    )

    # The internal advisory carries the caller's vault for terminal projection.
    # Prove both contexts are correct, then normalize only that fixture identity;
    # registry identity and every other payload field must still be identical.
    for payload, name in ((control, "control"), (timed, "timed")):
        _normalize_advisory_vault(payload, tmp_path / name)

    assert _canonical(timed) == _canonical(control)


def test_stale_commit_generation_forces_a_timed_revalidate(tmp_path: Path) -> None:
    clean_timings = mutation_timings_module.MutationTimings()
    _direct_write(tmp_path / "clean", bump=False, timings=clean_timings)
    stale_timings = mutation_timings_module.MutationTimings()
    _direct_write(tmp_path / "stale", bump=True, timings=stale_timings)

    clean = clean_timings.as_dict()["stages"]
    stale = stale_timings.as_dict()["stages"]
    assert clean["commit.revalidate"] == {"skipped": True}
    assert "skipped" not in stale["commit.revalidate"]
    assert stale["commit.revalidate"]["ms"] > 0.0


def _histograms() -> set[tuple[str, tuple[tuple[str, str], ...]]]:
    return {
        (item["name"], tuple(sorted(item["labels"].items())))
        for item in metrics_module.snapshot()["histograms"]
    }


def test_write_emits_the_write_service_histograms(tmp_path: Path) -> None:
    metrics_module.reset()
    _direct_write(tmp_path / "clean", bump=False, timings=None)

    histograms = _histograms()
    names = {name for name, _ in histograms}
    assert {
        "exomem_write_preflight_ms",
        "exomem_write_corpus_context_ms",
        "exomem_write_boundary_acquire_ms",
        "exomem_write_commit_ms",
    } <= names
    assert "exomem_write_revalidate_ms" not in names
    # Underscore names, never dots: dots are illegal in Prometheus/OpenMetrics
    # and the rest of the registry is already `exomem_*`.
    assert not any(name.startswith(("write.", "corpus.")) for name in names)
    ok = (("operation", "edit"), ("outcome", "ok"), ("phase", "initial"))
    assert ("exomem_write_commit_ms", ok) in histograms
    assert ("exomem_write_preflight_ms", ok) in histograms

    metrics_module.reset()
    _direct_write(tmp_path / "stale", bump=True, timings=None)
    assert "exomem_write_revalidate_ms" in {
        name for name, _ in _histograms()
    }


def test_revalidation_preflight_is_labelled_apart_from_the_first_attempt(
    tmp_path: Path,
) -> None:
    """A stale stamp re-runs the whole preflight through the public entrypoint.

    Both preflights are real work and both must be visible, but summing them
    into one series would report a doubled preflight cost that never happened
    in a single attempt. The `phase` label keeps them apart without moving the
    call off the public name (tests monkeypatch it as a race seam).
    """
    metrics_module.reset()
    _direct_write(tmp_path / "stale", bump=True, timings=None)

    phases = {
        labels
        for name, entry in _histograms()
        if name == "exomem_write_preflight_ms"
        for labels in [dict(entry).get("phase")]
    }
    assert phases == {"initial", "revalidate"}
    corpus_phases = {
        dict(entry).get("phase")
        for name, entry in _histograms()
        if name == "exomem_write_corpus_context_ms"
    }
    assert corpus_phases == {"initial", "revalidate"}


def test_validate_only_preflight_is_labelled_a_preview(tmp_path: Path) -> None:
    """A preview is real work but not commit-path work; it must not silently
    inflate the write series it would otherwise be indistinguishable from."""
    path = _seed(tmp_path)
    after_source = path.read_text(encoding="utf-8").replace(BEFORE_LINE, AFTER_LINE)
    metrics_module.reset()

    semantic_writes.preflight_existing(
        tmp_path,
        path=PAGE,
        after_source=after_source,
        operation="edit",
        validate_only=True,
    )

    phases = {
        dict(entry).get("phase")
        for name, entry in _histograms()
        if name.startswith("exomem_write_")
    }
    assert phases == {"preview"}


class _RefusingManager:
    """A lease manager whose mutation boundary can never be acquired."""

    def mutation_guard(self, *args, **kwargs):
        @contextmanager
        def _refuse():
            raise RuntimeError("boundary unavailable")
            yield  # pragma: no cover - unreachable, keeps this a generator

        return _refuse()


def test_boundary_acquire_is_measured_even_when_acquisition_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """Lease contention is the exact case this metric exists for.

    If the wait is only recorded once the boundary is held, the starvation
    that never resolves emits nothing at all and the whole wait resurfaces as
    a slow commit — the metric goes dark in precisely the incident it was
    added to explain.
    """
    path = _seed(tmp_path)
    after_source = path.read_text(encoding="utf-8").replace(BEFORE_LINE, AFTER_LINE)
    timings = mutation_timings_module.MutationTimings()
    preflight = semantic_writes.preflight_existing(
        tmp_path, path=PAGE, after_source=after_source, operation="edit", timings=timings
    )
    monkeypatch.setattr(writer_lease, "active_manager", lambda: _RefusingManager())
    metrics_module.reset()

    with pytest.raises(RuntimeError, match="boundary unavailable"):
        semantic_writes.commit_existing(tmp_path, preflight=preflight, timings=timings)

    histograms = _histograms()
    failed = (("operation", "edit"), ("outcome", "error"), ("phase", "initial"))
    assert ("exomem_write_boundary_acquire_ms", failed) in histograms
    assert ("exomem_write_commit_ms", failed) in histograms
    # The wait is on the collector too, not only in the histogram.
    boundary = timings.as_dict()
    assert isinstance(boundary["boundary"]["waited_ms"], float)
    assert isinstance(boundary["stages"]["commit.boundary_acquire"]["ms"], float)
    assert path.read_text(encoding="utf-8") != after_source


def test_a_broken_metrics_registry_cannot_mask_the_boundary_error(
    tmp_path: Path, monkeypatch
) -> None:
    """The emit on the failure path runs inside a `finally` that is already
    unwinding a lease error. If the instrument raised there it would REPLACE
    that error, and an observability defect would present as a boundary bug."""
    path = _seed(tmp_path)
    after_source = path.read_text(encoding="utf-8").replace(BEFORE_LINE, AFTER_LINE)
    timings = mutation_timings_module.MutationTimings()
    preflight = semantic_writes.preflight_existing(
        tmp_path, path=PAGE, after_source=after_source, operation="edit", timings=timings
    )
    monkeypatch.setattr(writer_lease, "active_manager", lambda: _RefusingManager())
    monkeypatch.setattr(
        semantic_writes.metrics,
        "observe_duration_ms",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("metrics registry is down")),
    )

    # The lease error, not the metrics error.
    with pytest.raises(RuntimeError, match="boundary unavailable"):
        semantic_writes.commit_existing(tmp_path, preflight=preflight, timings=timings)

    # Collector bookkeeping still landed: it runs before the guarded emit.
    assert isinstance(timings.as_dict()["boundary"]["waited_ms"], float)


def _spy_corpus_census(monkeypatch) -> list[Path]:
    """Monkeypatch `_corpus_census` to record every real call it still makes."""
    calls: list[Path] = []
    real_census = semantic_contract._corpus_census

    def spy(root: Path):
        calls.append(root)
        return real_census(root)

    monkeypatch.setattr(semantic_contract, "_corpus_census", spy)
    return calls


def _force_walk_confirmed_cache_path(monkeypatch) -> None:
    """Disable the event-token fast path so a cache hit still walks once.

    The event-hit branch in `build_corpus_context` returns before any stat
    walk at all, which would make a "warm cache costs one walk" assertion
    vacuous (it would cost zero). Forcing `freshness.triple` to miss routes
    every call through the census-confirmed reuse path instead, which walks
    exactly once whether it reuses, patches, or rebuilds.
    """
    monkeypatch.setattr("exomem.freshness.triple", lambda *args, **kwargs: None)


def test_preflight_existing_warm_cache_costs_one_census_walk(
    tmp_path: Path, monkeypatch
) -> None:
    """Warm reuse must stay at exactly one walk after threading the census."""
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    _force_walk_confirmed_cache_path(monkeypatch)
    semantic_contract.reset_corpus_context_cache()
    path = _seed(tmp_path)
    after_source = path.read_text(encoding="utf-8").replace(BEFORE_LINE, AFTER_LINE)
    semantic_contract.build_corpus_context(tmp_path)  # warm the process cache

    calls = _spy_corpus_census(monkeypatch)
    preflight = semantic_writes.preflight_existing(
        tmp_path,
        path=PAGE,
        after_source=after_source,
        operation="edit",
    )

    assert len(calls) == 1, f"expected exactly one census walk, got {len(calls)}"
    assert preflight.census_token is not None


def _evict_between_build_and_capture(monkeypatch, root: Path) -> None:
    """Simulate a concurrent eviction landing between corpus build and capture.

    `semantic_contract.evaluate` is the last thing `_preflight_existing` (and
    `_evaluate_structural`) run between `build_corpus_context_with_census` and
    `_capture_validity_stamp` — evicting from inside it lands exactly in the
    window where the old `cached_corpus_census(root)` global-cache read would
    come back empty and force a second walk.
    """
    real_evaluate = semantic_contract.evaluate

    def evicting_evaluate(*args, **kwargs):
        semantic_contract.evict_corpus_context(root)
        return real_evaluate(*args, **kwargs)

    monkeypatch.setattr(semantic_contract, "evaluate", evicting_evaluate)


def test_preflight_existing_evicted_cache_costs_one_census_walk(
    tmp_path: Path, monkeypatch
) -> None:
    """The fix under test: a preflight must never pay a second walk for its
    own validity token, even when the global corpus-context cache entry it
    just built is evicted before the token is captured.

    Against unpatched `main` this is red: `cached_corpus_census(root)` comes
    back `None` after the eviction, so `corpus_validity_token`'s own fallback
    walks the corpus a second time — TWO walks for one preflight. Threading
    the census straight from the corpus context the preflight already built
    means the eviction never matters: still exactly ONE walk.

    The cache is warmed before the spy/eviction hook attach so the one walk
    being counted is the preflight's own confirm-and-reuse walk, not the
    unrelated multi-walk stabilization a genuinely cold, from-scratch build
    already pays before this fix's seam is ever reached.
    """
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    _force_walk_confirmed_cache_path(monkeypatch)
    semantic_contract.reset_corpus_context_cache()
    path = _seed(tmp_path)
    after_source = path.read_text(encoding="utf-8").replace(BEFORE_LINE, AFTER_LINE)
    semantic_contract.build_corpus_context(tmp_path)  # warm the process cache

    _evict_between_build_and_capture(monkeypatch, tmp_path)
    calls = _spy_corpus_census(monkeypatch)

    preflight = semantic_writes.preflight_existing(
        tmp_path,
        path=PAGE,
        after_source=after_source,
        operation="edit",
    )

    assert len(calls) == 1, f"expected exactly one census walk, got {len(calls)}"
    assert preflight.census_token is not None


def test_preflight_existing_census_token_matches_the_old_uncached_path(
    tmp_path: Path, monkeypatch
) -> None:
    """Threading the census must not change what the validity token contains.

    Reconstructs the corpus-census half of the stamp exactly the way the old,
    un-threaded `_capture_validity_stamp` produced it — `cached_corpus_census`
    missing, falling back to `corpus_validity_token`'s own walk — on the same
    unchanged corpus, right after the real preflight ran. Both walks see
    identical on-disk state, so the two tokens must be byte-for-byte equal.
    """
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    _force_walk_confirmed_cache_path(monkeypatch)
    semantic_contract.reset_corpus_context_cache()
    path = _seed(tmp_path)
    after_source = path.read_text(encoding="utf-8").replace(BEFORE_LINE, AFTER_LINE)
    semantic_contract.build_corpus_context(tmp_path)  # warm the process cache
    _evict_between_build_and_capture(monkeypatch, tmp_path)

    preflight = semantic_writes.preflight_existing(
        tmp_path,
        path=PAGE,
        after_source=after_source,
        operation="edit",
    )

    assert preflight.census_token is not None
    sc_token, _generation = preflight.census_token
    # The eviction is still in effect (nothing repopulated the cache since),
    # so this reproduces the old fallback path on the identical corpus state.
    old_style_sc_token = semantic_contract.corpus_validity_token(
        tmp_path, corpus_census=semantic_contract.cached_corpus_census(tmp_path)
    )
    assert old_style_sc_token == sc_token


def test_preflight_creation_structural_shares_the_same_one_walk_seam(
    tmp_path: Path, monkeypatch
) -> None:
    """`preflight_creation`'s structural/not_semantic path shares
    `_capture_validity_stamp` with `preflight_existing` through
    `_evaluate_structural`, and must show the same one-walk behaviour under
    the same evicted-cache condition.
    """
    from exomem import create_file

    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    _force_walk_confirmed_cache_path(monkeypatch)
    semantic_contract.reset_corpus_context_cache()
    semantic_contract.build_corpus_context(tmp_path)  # warm the process cache

    _evict_between_build_and_capture(monkeypatch, tmp_path)
    calls = _spy_corpus_census(monkeypatch)

    preflight = create_file.create_file(
        tmp_path,
        path="Knowledge Base/Notes/plain-creation.md",
        content="# Plain creation\n\nOrdinary prose, no compiled type.\n",
        frontmatter={"status": "active"},
        today=TODAY,
        validate_only=True,
    )

    assert preflight.applicability == "not_semantic"
    assert len(calls) == 1, f"expected exactly one census walk, got {len(calls)}"
    assert preflight.census_token is not None


def _direct_creation_write(
    root: Path,
    *,
    bump: bool,
    path: str = "Knowledge Base/Notes/plain-creation-commit.md",
) -> semantic_writes.CreationCommit:
    """One structural creation preflight+commit pair, optionally with a stale
    commit generation bumped in between -- mirrors `_direct_write`'s `bump`
    shape for the creation seam (`preflight_creation` / `commit_creation`).
    """
    source = (
        "---\n"
        "status: active\n"
        "---\n\n"
        "# Plain commit creation\n\n"
        "Ordinary prose, no compiled type.\n"
    )
    token = semantic_writes.DraftToken(
        "test_writer", "tier2_create", path, TODAY.isoformat()
    ).encode()
    preflight = semantic_writes.preflight_creation(
        root,
        path=path,
        source=source,
        operation="tier2_create",
        writer="test_writer",
        draft_id=None,
        draft_token=token,
    )
    assert preflight.applicability == "not_semantic"
    if bump:
        writer_lease._bump_commit_generation(
            writer_lease.active_manager().config.state_dir, root
        )
    return semantic_writes.commit_creation(root, preflight=preflight, operation="tier2_create")


def test_creation_commit_survives_a_stale_validity_stamp_at_boundary_entry(
    tmp_path: Path,
) -> None:
    """`commit_creation`'s structural/non-semantic revalidation branch (taken
    whenever `validity_stamp_current` is False at boundary entry) calls
    `_evaluate_structural` too, and must unpack its 4-tuple return the same
    way every other caller does. A commit-generation bump landing between
    preflight and commit -- exactly the boundary-entry staleness
    `_direct_write(bump=True)` already covers for
    `preflight_existing`/`commit_existing` -- forces this exact branch.
    """
    committed = _direct_creation_write(tmp_path, bump=True)

    assert committed.mutated is True
    assert committed.applicability == "not_semantic"
    assert committed.written_paths


def test_corpus_metrics_carry_caller_and_outcome(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    # Force the census-based reuse path: the event-token fast path returns
    # before any walk, so it cannot show the hit-vs-rebuild distinction.
    monkeypatch.setattr("exomem.freshness.triple", lambda *args, **kwargs: None)
    _seed(tmp_path)
    # Reset AFTER the seeding write, not before: that write's own publish now
    # populates the cache on miss, and the first build below has to be a
    # genuine cold rebuild for the hit-vs-rebuild labels to mean anything.
    semantic_contract.reset_corpus_context_cache()
    metrics_module.reset()

    semantic_contract.build_corpus_context(tmp_path)
    with semantic_contract.call_context("write"):
        semantic_contract.build_corpus_context(tmp_path)

    observed = {
        (item["name"], item["labels"].get("caller"), item["labels"].get("outcome"))
        for item in metrics_module.snapshot()["histograms"]
    }
    assert ("exomem_corpus_census_walk_ms", "unknown", "rebuild") in observed
    assert ("exomem_corpus_build_ms", "unknown", "rebuild") in observed
    assert ("exomem_corpus_census_walk_ms", "write", "hit") in observed
    assert ("exomem_corpus_build_ms", "write", "rebuild") not in observed


def test_a_walk_nobody_consumed_is_not_reported_as_a_rebuild(
    tmp_path: Path, monkeypatch
) -> None:
    """An exception between the census walk and its disposition must not mint
    a `rebuild` sample. Nothing was rebuilt — the call died holding a walk it
    never got to use, and the series has to say so."""
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    monkeypatch.setattr("exomem.freshness.triple", lambda *args, **kwargs: None)
    semantic_contract.reset_corpus_context_cache()
    _seed(tmp_path)

    boom = RuntimeError("corpus inputs vanished mid-build")
    monkeypatch.setattr(
        semantic_contract,
        "_registries_match_disk",
        lambda *args, **kwargs: (_ for _ in ()).throw(boom),
    )
    metrics_module.reset()

    with pytest.raises(RuntimeError, match="corpus inputs vanished mid-build"):
        semantic_contract.build_corpus_context(tmp_path)

    observed = {
        (item["name"], item["labels"].get("outcome"))
        for item in metrics_module.snapshot()["histograms"]
    }
    assert ("exomem_corpus_census_walk_ms", "aborted") in observed
    assert ("exomem_corpus_census_walk_ms", "rebuild") not in observed
    assert ("exomem_corpus_build_ms", "rebuild") not in observed


_DRAFT_ID = "00000000-0000-4000-8000-0000000005b2"
_EXISTING_ID = "00000000-0000-4000-8000-0000000005b3"
_DRAFT_PAGE = "Knowledge Base/Notes/Insights/candidate.md"
_EXISTING_PAGE = "Knowledge Base/Notes/Insights/existing.md"
_COMPILED_BODY = (
    "Body.\n\n"
    "## Observations\n\n"
    "- [operating constraint] Keep retries bounded #reliability\n\n"
    "## Relations\n"
)


def _compiled_source(page_id: str, *, title: str = "Candidate") -> str:
    return (
        "---\n"
        f"title: {title}\n"
        "type: insight\n"
        "status: active\n"
        f"exomem_id: {page_id}\n"
        "---\n\n"
        f"{_COMPILED_BODY}"
    )


def test_creation_draft_prepare_costs_one_census_walk(tmp_path: Path, monkeypatch) -> None:
    """`prepare_commit_creation_draft` must not re-walk the vault for its stamp.

    It holds `preliminary.before_corpus` from its own `_attempt` build, yet
    asked `corpus_validity_token` for a census through the global process
    cache. Against unpatched code an eviction landing between that build and
    the capture -- the exact window a concurrent write opens -- makes
    `cached_corpus_census` come back empty, so the token walks the whole
    corpus a SECOND time. Threading the build's own census makes the eviction
    irrelevant: still exactly one walk.
    """
    from exomem import relation_review

    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    _force_walk_confirmed_cache_path(monkeypatch)
    semantic_contract.reset_corpus_context_cache()
    existing = tmp_path / _EXISTING_PAGE
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text(
        _compiled_source(_EXISTING_ID, title="Existing"), encoding="utf-8", newline=""
    )
    source = _compiled_source(_DRAFT_ID)
    validation = relation_review.validate_creation_draft(
        tmp_path,
        path=_DRAFT_PAGE,
        source=source,
        draft_id=_DRAFT_ID,
        operation="create",
    )
    semantic_contract.build_corpus_context(tmp_path)  # warm the process cache

    _evict_between_build_and_capture(monkeypatch, tmp_path)
    calls = _spy_corpus_census(monkeypatch)

    prepared = relation_review.prepare_commit_creation_draft(
        tmp_path,
        path=_DRAFT_PAGE,
        source=source,
        draft_id=_DRAFT_ID,
        operation="create",
        relation_disposition="reviewed_none",
        relation_review_hash=validation.draft_hash,
        relation_review_reason="No honest typed relation yet",
    )

    assert len(calls) == 1, f"expected exactly one census walk, got {len(calls)}"
    assert prepared.reuse is not None


def test_publish_after_an_eviction_leaves_the_next_preflight_warm(
    tmp_path: Path, monkeypatch
) -> None:
    """A write's own publish must rewarm a cold corpus cache for the next one.

    This is the benefit the write-latency gate's `cold_preflight` row cannot
    show: that row evicts BETWEEN the warm write and its probe, so the probe's
    own preflight faces a cold cache by construction. The cost a cold cache
    really imposes is on the write AFTER a write -- and a publish that finds
    nothing to patch is exactly where it can be paid once instead of by every
    later caller.

    Against unpatched code the publish is a silent no-op on a cold cache, so
    the preflight below pays a full stat census of the vault.
    """
    from exomem import freshness, index_sync

    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    path = _seed(tmp_path)
    freshness.rebaseline(tmp_path)
    # A restart, an eviction, or a Class B projection withdrawal -- any of the
    # ways the process cache legitimately ends up empty with the registry live.
    semantic_contract.reset_corpus_context_cache()

    assert index_sync.publish_corpus_delta(tmp_path, changed=(path,)) is True

    calls = _spy_corpus_census(monkeypatch)
    # Both seams: a rebuild that skipped the walk, or a walk that fed no
    # rebuild, would each slip past a spy on the other one alone.
    builds: list[Path] = []
    real_build = semantic_contract._build_corpus_context_uncached

    def counted_build(root: Path, **kwargs):
        builds.append(root)
        return real_build(root, **kwargs)

    monkeypatch.setattr(semantic_contract, "_build_corpus_context_uncached", counted_build)
    after_source = path.read_text(encoding="utf-8").replace(BEFORE_LINE, AFTER_LINE)
    preflight = semantic_writes.preflight_existing(
        tmp_path,
        path=PAGE,
        after_source=after_source,
        operation="edit",
    )

    assert calls == [], f"expected an event-hit with no census walk, got {len(calls)}"
    assert builds == [], f"expected an event-hit with no corpus rebuild, got {len(builds)}"
    assert preflight.census_token is not None


# ---------------------------------------------------------------------------
# Task 1.15: the collected stages reach the ledger, and the window they sit in
# is accounted for.
# ---------------------------------------------------------------------------

@contextmanager
def _call_token(name: str):
    from exomem import call_spans

    call_spans.reset()
    handle = call_spans.MCP_CALL_TOKEN.set(name)
    try:
        yield name
    finally:
        call_spans.MCP_CALL_TOKEN.reset(handle)


def test_the_write_stages_reach_the_ledger_without_the_envelope_flag(
    tmp_path: Path, monkeypatch
) -> None:
    """`EXOMEM_WRITE_TIMINGS` governs the caller's envelope, not the operator's row.

    The stages have existed for a year and never reached a ledger row, because
    the one flag gated both. On the 0.84.1 personal service that left 46 s of a
    78 s write inside `derived.canonical_to_committed` with no span and no log
    line, while `commit.stamp_check` and its siblings had measured pieces of it
    the whole time and threw them away.
    """
    from exomem import call_spans

    monkeypatch.delenv("EXOMEM_WRITE_TIMINGS", raising=False)

    with _call_token("stage-emission") as token:
        payload = _run_edit(tmp_path)
        spans = {span["name"]: span for span in call_spans.pop_call_spans(token)}

    assert "timings" not in payload, (
        "the response envelope must stay exactly as it was without the flag"
    )
    for name in _ALWAYS_TAKEN_STAGES:
        assert name in spans, f"stage {name!r} never reached the ledger: {sorted(spans)}"
        assert spans[name]["count"] >= 1
        assert spans[name]["ms"] >= 0.0


def test_a_governed_edit_records_the_advisory_sweep_with_what_it_encoded(
    tmp_path: Path, monkeypatch
) -> None:
    """The advisory's encode was invisible, and its size is half the diagnosis.

    On 0.84.1 an `embeddings.encode` of 15.6 s with `count=1` sat entirely
    outside `index.embeddings` on one write, and 8.5 s of 30.4 s outside it on
    the next. Nothing said which caller it belonged to. The sweep is now timed
    where the encode happens and reports what it encoded, so a duration and a
    whole-note body are no longer the same reading.

    The encoder itself is stubbed: this asserts the instrumentation, and
    loading bge-base to do it would make a measurement test a model test.
    """
    import numpy as np

    from exomem import call_spans, corpus_aware, embeddings

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.delenv("KB_MCP_DISABLE_EMBEDDINGS", raising=False)

    encoded: list[list[str]] = []

    class _Index:
        def search(self, _vector, *, k=15, allowed_paths=None):
            return []

        def search_many(self, _vectors, _k, *, admits):
            return []

    monkeypatch.setattr(
        embeddings, "chunk_text", lambda title, body: [f"{title}\n{body}"[:200]], raising=True
    )
    monkeypatch.setattr(
        embeddings,
        "embed_texts",
        lambda texts, **_kw: (
            encoded.append(list(texts)),
            np.zeros((len(texts), 4), dtype=np.float32),
        )[1],
        raising=True,
    )
    monkeypatch.setattr(
        embeddings, "get_embedding_index", lambda _root: _Index(), raising=True
    )

    with _call_token("advisory-sweep") as token:
        _run_edit(tmp_path)
        spans = {span["name"]: span for span in call_spans.pop_call_spans(token)}

    assert encoded, "the advisory never reached the encoder, so this proves nothing"
    sweep = spans.get("advisory.best_cosine")
    assert sweep is not None, (
        f"the advisory's cosine sweep is still unattributed: {sorted(spans)}"
    )
    assert sweep["ms"] > 0.0
    fields = sweep.get("fields") or {}
    assert fields.get("texts", 0) >= 1, (
        "a duration with no text count cannot separate a cold model from a "
        f"caller handing the encoder a whole note body: {sweep}"
    )
    assert fields.get("chars", 0) > 0, sweep
    assert corpus_aware is not None  # the sweep under test lives here


def test_the_leaf_spans_account_for_the_canonical_to_committed_window(
    tmp_path: Path, monkeypatch
) -> None:
    """The attribution guarantee: the window is explained, not merely measured.

    `derived.canonical_to_committed` was an umbrella with 46 s of a 78 s write
    inside it and no span underneath. A span that only says how long something
    took, with nothing accounting for it, is the shape of the defect this
    instrumentation exists to remove. Pin the phase inventory here and the
    duration of fallback work with a controlled clock below; real wall-clock
    coverage ratios include uninstrumented scheduler pauses between phases.
    """
    from types import SimpleNamespace

    from exomem import call_spans, semantic_index
    from exomem import vault as vault_module

    # The synchronous fan-out route, deliberately: with fast durable ack on,
    # the derived work moves out of this window and is reported under
    # `derived.acknowledgement` instead, and the umbrella measures the route
    # rather than the work.
    monkeypatch.delenv("EXOMEM_FAST_DURABLE_ACK", raising=False)

    path = _seed(tmp_path)
    after_source = path.read_text(encoding="utf-8").replace(BEFORE_LINE, AFTER_LINE)

    def leaf(vault_root: Path) -> dict:
        vault_module.batch_atomic_write(
            [vault_module.PlannedWrite(path, after_source)],
            vault_root=vault_root,
            semantic_states={
                PAGE: semantic_index.build_parent_index_state(
                    vault_root, PAGE, source=after_source
                )
            },
        )
        return {"path": PAGE}

    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "lease-state")
    )
    command = SimpleNamespace(name="remember", leaf=leaf, read_only=False)

    # One warm-up write first: the first governed write in a process pays
    # one-off lazy imports inside the fan-out, and they land in whichever span
    # happens to be open rather than in the step that owns them.
    manager.invoke(
        command,
        (tmp_path,),
        {},
        idempotency_key="attribution-warm-up",
        mutation_request_id="44444444-4444-4444-8444-444444444444",
    )
    with _call_token("attribution") as token:
        manager.invoke(
            command,
            (tmp_path,),
            {},
            idempotency_key="attribution-window",
            mutation_request_id="33333333-3333-4333-8333-333333333333",
        )
        spans = {span["name"]: span for span in call_spans.pop_call_spans(token)}

    umbrella = spans.get("derived.canonical_to_committed")
    assert umbrella is not None, f"spans: {sorted(spans)}"
    assert "derived.fanout" in spans, (
        "the umbrella must be split, or a large number still says only that it "
        f"was large: {sorted(spans)}"
    )
    assert "derived.terminal_persist" in spans, sorted(spans)
    halves = spans["derived.fanout"]["ms"] + spans["derived.terminal_persist"]["ms"]
    assert halves >= umbrella["ms"] * 0.9, (
        f"the two halves do not partition the umbrella: {halves} vs {umbrella['ms']}"
    )

    # Every expected step inside this fixture's window is named.
    for name in (
        "index.upsert_after_write",
        "index.self_write_registration",
        "index.graph_epoch_handoff",
        "index.path_partition",
        "index.semantic_states",
        "index.policy_revalidate",
        "index.corpus_publish",
        "index.semantic_purge",
        "index.path_custody",
        "index.completion_check",
    ):
        assert name in spans, (
            f"{name!r} is a step inside the fan-out with no span, which is the "
            f"shape of the 0.84.1 defect: {sorted(spans)}"
        )


@pytest.mark.parametrize("dispatch_fails", [False, True])
def test_completion_and_fallback_work_have_exact_timing_spans(
    tmp_path: Path, monkeypatch, dispatch_fails: bool
) -> None:
    from types import SimpleNamespace

    from exomem import call_spans, file_watcher, graph_sync, index_sync
    from exomem import vault as vault_module

    clock = [100.0]
    monkeypatch.setattr(
        call_spans, "time",
        SimpleNamespace(perf_counter=lambda: clock[0], monotonic=lambda: clock[0]),
    )
    monkeypatch.setattr(file_watcher, "register_self_write", lambda *a, **kw: ([], False))
    monkeypatch.setattr(graph_sync, "register_outer_fanout_failure", lambda *a: None)
    report = index_sync.IndexSyncReport(
        "upsert", (PAGE,), (PAGE,),
        tuple(
            index_sync.IndexComponentOutcome(name, "not_required", "NOT_REQUIRED")
            for name in ("lexstore", "memory_refs", "resolver", "epistemic_graph", "embeddings", "watcher")
        ),
    )
    calls = []

    def dispatch(*args, **kwargs):
        if dispatch_fails:
            raise RuntimeError("injected dispatch failure")
        return report

    def completion(root, paths, observed):
        assert (root, paths, observed) == (tmp_path, [tmp_path / PAGE], report)
        calls.append("check")
        clock[0] += 0.020
        return False

    def persist(root, paths):
        assert (root, paths) == (tmp_path, [tmp_path / PAGE])
        calls.append("store")
        clock[0] += 0.030
        return 1

    monkeypatch.setattr(index_sync, "upsert_after_write", dispatch)
    monkeypatch.setattr(index_sync, "full_upsert_succeeded", completion)
    monkeypatch.setattr(index_sync, "record_failed_refresh", persist)
    with _call_token("fallback-attribution") as token:
        assert vault_module.post_commit_batch_fanout(
            tmp_path, [tmp_path / PAGE], None, None
        ) is False
        spans = {item["name"]: item for item in call_spans.pop_call_spans(token)}

    assert calls == (["store"] if dispatch_fails else ["check", "store"])
    assert spans["index.full_refresh_store"]["ms"] == 30.0
    assert spans["index.full_refresh_store"]["count"] == 1
    assert spans["index.full_refresh_store"]["fields"]["paths"] == 1
    if dispatch_fails:
        assert "index.completion_check" not in spans
    else:
        assert spans["index.completion_check"]["ms"] == 20.0
