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
    assert _canonical(instrumented) == _canonical(control)


def test_semantic_write_payloads_ignore_the_timings_kwarg(tmp_path: Path) -> None:
    control = _direct_write(tmp_path / "control", bump=False, timings=None)
    timed = _direct_write(
        tmp_path / "timed",
        bump=False,
        timings=mutation_timings_module.MutationTimings(),
    )

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


def test_corpus_metrics_carry_caller_and_outcome(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    # Force the census-based reuse path: the event-token fast path returns
    # before any walk, so it cannot show the hit-vs-rebuild distinction.
    monkeypatch.setattr("exomem.freshness.triple", lambda *args, **kwargs: None)
    semantic_contract.reset_corpus_context_cache()
    _seed(tmp_path)
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
