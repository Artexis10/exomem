"""The semantic corpus context is a reapable idle cache (bound-cell-memory D5 step 1).

The context is a whole-vault Python projection: every page's parsed state, the
link maps and the identity census. Nothing holds it resident once a cell is
idle, so the idle reaper releases it like the find RAM caches, and the next
use rebuilds it. A request already holding a context keeps an immutable
object: releasing the cache drops only the cache's reference.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from exomem import freshness, model_reaper, readiness, semantic_contract

_SLOT = "semantic-corpus-context"
_PAGES = {
    "Knowledge Base/Notes/Insights/one.md",
    "Knowledge Base/Notes/Insights/two.md",
}


def _page(title: str) -> str:
    return f"---\ntitle: {title}\ntype: insight\nstatus: active\n---\n\nBody of {title}.\n"


@pytest.fixture(autouse=True)
def _cache_on_and_evictable(monkeypatch: pytest.MonkeyPatch):
    from exomem import mode

    # The suite-wide conftest defaults the cache OFF; this suite exercises it.
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    monkeypatch.setattr(mode, "retain_cpu_caches", lambda: False)
    monkeypatch.setattr(readiness, "is_warming", lambda: False)
    semantic_contract.reset_corpus_context_cache()
    freshness.clear()
    yield
    semantic_contract.reset_corpus_context_cache()
    freshness.clear()


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    notes = tmp_path / "Knowledge Base" / "Notes" / "Insights"
    notes.mkdir(parents=True)
    (notes / "one.md").write_text(_page("One"), encoding="utf-8")
    (notes / "two.md").write_text(_page("Two"), encoding="utf-8")
    freshness.rebaseline(tmp_path)
    return tmp_path


def _corpus_slot() -> model_reaper.ResourceSlot:
    slots = {slot.name: slot for slot in model_reaper.default_slots()}
    assert _SLOT in slots
    return slots[_SLOT]


def _reap(slot: model_reaper.ResourceSlot) -> list[str]:
    """One tick far past the idle threshold after the slot's last observed use."""
    slot.is_loaded()  # the tick that first observes it loaded starts its clock
    return model_reaper._reap_once(
        [slot], time.monotonic() + model_reaper.idle_seconds() + 1.0, model_reaper.idle_seconds()
    )


def test_a_reap_drops_the_context_and_the_next_use_rebuilds_it(vault: Path) -> None:
    held = semantic_contract.build_corpus_context(vault)
    assert semantic_contract.build_corpus_context(vault) is held  # served from cache
    slot = _corpus_slot()
    assert slot.is_loaded()

    assert _reap(slot) == [_SLOT]
    assert not semantic_contract._CORPUS_CONTEXT_CACHE
    assert not slot.is_loaded()

    # The request that held the context keeps a complete, usable object.
    assert set(held.pages) == _PAGES
    resolved = semantic_contract._resolve_reference_wikilink_from_context(held, "one")
    assert resolved.status == "resolved"

    rebuilt = semantic_contract.build_corpus_context(vault)
    assert rebuilt is not held
    assert set(rebuilt.pages) == _PAGES
    assert semantic_contract.build_corpus_context(vault) is rebuilt
    assert slot.is_loaded()


def test_a_reap_during_a_cold_build_does_not_break_that_build(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A build in flight when the reaper fires still returns a complete context."""
    slot = _corpus_slot()
    real_build = semantic_contract._build_corpus_context_uncached
    reaped: list[str] = []

    def build_then_reap(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        context = real_build(*args, **kwargs)
        reaped.extend(model_reaper._reap_once([slot], time.monotonic() + 1e6, 1.0))
        return context

    held = semantic_contract.build_corpus_context(vault)
    semantic_contract.release_idle_corpus_contexts()
    monkeypatch.setattr(semantic_contract, "_build_corpus_context_uncached", build_then_reap)

    in_flight = semantic_contract.build_corpus_context(vault)

    assert set(in_flight.pages) == _PAGES == set(held.pages)
    monkeypatch.setattr(semantic_contract, "_build_corpus_context_uncached", real_build)
    assert semantic_contract.build_corpus_context(vault) is in_flight


def test_a_snapshot_taken_before_a_reap_declines_rather_than_breaks(vault: Path) -> None:
    semantic_contract.build_corpus_context(vault)
    snapshot = semantic_contract.current_reference_identity_snapshot(vault)
    assert snapshot is not None
    assert semantic_contract.resolve_reference_wikilink(vault, snapshot, "one").status == (
        "resolved"
    )
    assert _reap(_corpus_slot()) == [_SLOT]
    assert semantic_contract.resolve_reference_wikilink(vault, snapshot, "one").status == (
        "unavailable"
    )


def test_use_moves_the_activity_fingerprint(vault: Path) -> None:
    before = semantic_contract.corpus_context_activity()
    semantic_contract.build_corpus_context(vault)
    after_build = semantic_contract.corpus_context_activity()
    semantic_contract.build_corpus_context(vault)
    assert before != after_build != semantic_contract.corpus_context_activity()


def test_nothing_to_release_is_not_a_reap() -> None:
    assert semantic_contract.release_idle_corpus_contexts() is False
    assert not _corpus_slot().is_loaded()
