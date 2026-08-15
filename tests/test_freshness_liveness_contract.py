"""The joint freshness-liveness contract, corpus half (#510 slices A/B).

`freshness.*` describes what the event registry knows about the vault's
files. It must never be used to describe whether a downstream projection
managed to publish its own derived copy. These tests pin the corpus-side
consequences of that separation:

* a Class B failure -- the registry advanced, only the corpus PATCH failed --
  must leave vault freshness live and not externally pending, and must leave
  the corpus projection able to warm itself again on the next publish;
* a Class A failure -- the registry-advance half itself failed -- must still
  take the full withdraw, because the event suffix really is unknowable;
* a publish that finds no warm corpus entry must POPULATE one (D8) instead of
  silently returning, so the next reader is an event-hit rather than a full
  stat census of the vault;
* that populate must never stamp an event token onto a context whose walk an
  event leapfrogged (contract SS5, clauses L1-L5).

The graph-side halves of the shared defending test (contract SS4 assertions 3
and 4) belong to the parallel `lane/graph-publish-hygiene` lane and are not
asserted here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import freshness, index_sync, semantic_contract

_PAGE_REL = "Knowledge Base/Notes/Insights/one.md"
_OTHER_REL = "Knowledge Base/Notes/Insights/two.md"


def _page(*, title: str = "Page", body: str = "Body.\n") -> str:
    return f"---\ntitle: {title}\ntype: insight\nstatus: active\nproject: atlas\n---\n\n{body}"


@pytest.fixture(autouse=True)
def _corpus_cache_on(monkeypatch: pytest.MonkeyPatch):
    # The suite-wide conftest defaults the corpus cache OFF; this contract is
    # entirely about that cache's lifecycle, so opt back in and start cold.
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    semantic_contract.reset_corpus_context_cache()
    freshness.clear()
    yield
    semantic_contract.reset_corpus_context_cache()
    freshness.clear()


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    notes = tmp_path / "Knowledge Base" / "Notes" / "Insights"
    notes.mkdir(parents=True)
    (notes / "one.md").write_text(_page(title="One"), encoding="utf-8")
    (notes / "two.md").write_text(_page(title="Two"), encoding="utf-8")
    freshness.rebaseline(tmp_path)
    return tmp_path


def _spy_corpus_census(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record every real full-vault stat census the code under test still pays."""
    calls: list[Path] = []
    real_census = semantic_contract._corpus_census

    def spy(root: Path):
        calls.append(root)
        return real_census(root)

    monkeypatch.setattr(semantic_contract, "_corpus_census", spy)
    return calls


def _poison_corpus_patch(monkeypatch: pytest.MonkeyPatch):
    """Fail only the projection half of the corpus publish seam (Class B).

    `freshness.on_files_changed` still runs and still records the delta -- the
    registry observed every event. Only the cache's own patch raises, which is
    exactly the contract's Class B: "a corpus-cache patch that raises for a
    cause local to the cache". Returns the real patch so a caller can put it
    back explicitly; `monkeypatch.undo()` would also revert the corpus-cache
    env var this module's fixture sets, which is not what any caller means.
    """
    real_patch = semantic_contract._patch_corpus_files_changed_locked

    def boom(*_args, **_kwargs):
        raise RuntimeError("poisoned corpus delta")

    monkeypatch.setattr(semantic_contract, "_patch_corpus_files_changed_locked", boom)
    return real_patch


def test_refused_corpus_patch_does_not_cool_vault_freshness(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract C1.1: a Class B corpus-patch failure is not registry loss.

    Red on `main` for the contract's stated reason: `publish_corpus_delta`'s
    `_withdraw_unbridgeable_corpus_consumers` calls `freshness.invalidate`
    (index_sync.py:63) and `freshness.mark_external_pending` (:64) for ANY
    publish failure, so `triple()` returns None, the corpus event-hit gate is
    structurally unreachable, and every later caller pays a full stat census.
    """
    semantic_contract.build_corpus_context(vault)
    assert freshness.triple(vault, "vault") is not None

    page = vault / _PAGE_REL
    page.write_text(_page(title="One changed"), encoding="utf-8")
    real_patch = _poison_corpus_patch(monkeypatch)

    assert index_sync.publish_corpus_delta(vault, changed=(page,)) is False

    # 1. Liveness preserved: the registry saw the event, only the projection
    #    could not publish its derived copy.
    assert freshness.triple(vault, "vault") is not None
    assert freshness.is_live(vault, "vault") is True
    assert freshness.external_pending(vault) is False

    # 2. The corpus projection can warm itself again with no operator action:
    #    the next publish repopulates on miss, and the read after it is an
    #    event-hit paying zero full censuses.
    monkeypatch.setattr(semantic_contract, "_patch_corpus_files_changed_locked", real_patch)
    other = vault / _OTHER_REL
    other.write_text(_page(title="Two changed"), encoding="utf-8")
    assert index_sync.publish_corpus_delta(vault, changed=(other,)) is True

    cache_key = semantic_contract._corpus_cache_key(vault)
    assert semantic_contract._CORPUS_CONTEXT_EVENT_TOKENS.get(cache_key) == freshness.triple(
        vault, "vault"
    )

    census_calls = _spy_corpus_census(monkeypatch)
    context = semantic_contract.build_corpus_context(vault)
    assert census_calls == []
    assert context.pages[_OTHER_REL].title == "Two changed"
    assert context.pages[_PAGE_REL].title == "One changed"


def test_registry_advance_failure_still_withdraws_vault_freshness(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract Class A: the registry-advance half failing IS registry loss.

    Green before and after -- this is the guard that the narrowing in C1.1
    stays narrow. `freshness.on_files_changed` raising means the event suffix
    really is unknowable, so the full withdraw (invalidate + external pending)
    remains correct and must survive the split seam.
    """
    semantic_contract.build_corpus_context(vault)
    assert freshness.triple(vault, "vault") is not None

    def boom(*_args, **_kwargs):
        raise RuntimeError("registry advance failed")

    monkeypatch.setattr(semantic_contract.freshness, "on_files_changed", boom)
    page = vault / _PAGE_REL
    page.write_text(_page(title="One changed"), encoding="utf-8")

    assert index_sync.publish_corpus_delta(vault, changed=(page,)) is False

    assert freshness.triple(vault, "vault") is None
    assert freshness.is_live(vault, "vault") is False
    assert freshness.external_pending(vault) is True
    cache_key = semantic_contract._corpus_cache_key(vault)
    assert cache_key not in semantic_contract._CORPUS_CONTEXT_CACHE


def test_publish_populates_a_cold_corpus_cache_for_the_next_reader(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deliverable 8: the `entry is None` no-op must populate, not return.

    Red on `main` for the contract's stated reason: `semantic_contract.py`'s
    `_patch_corpus_files_changed_locked` returns silently when the cache holds
    no entry (sc:1745-1746), so a write onto a cold cache leaves it cold and
    the very next preflight pays a full stat census of the vault.
    """
    page = vault / _PAGE_REL
    page.write_text(_page(title="One changed"), encoding="utf-8")

    semantic_contract.publish_corpus_files_changed(vault, changed=(page,))

    cache_key = semantic_contract._corpus_cache_key(vault)
    entry = semantic_contract._CORPUS_CONTEXT_CACHE.get(cache_key)
    assert entry is not None
    assert entry[1].pages[_PAGE_REL].title == "One changed"
    assert semantic_contract._CORPUS_CONTEXT_EVENT_TOKENS.get(cache_key) == freshness.triple(
        vault, "vault"
    )

    census_calls = _spy_corpus_census(monkeypatch)
    context = semantic_contract.build_corpus_context(vault)
    assert census_calls == []
    assert context is entry[1]


def test_populate_on_miss_never_stamps_a_context_an_event_leapfrogged(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract L2/L3: the populate's admission gate is the consumer checkpoint.

    The populate cannot hold `_CORPUS_CONTEXT_UPDATE_LOCK` across its own walk
    (the publisher advances freshness under that lock), so it takes L2's shape:
    capture `freshness.consumer_checkpoint` BEFORE the walk, then admit the
    built context only if the checkpoint is unchanged and the cache slot is
    still the empty one it observed. Here an external event lands mid-walk, so
    the built context provably misses a delta the current token covers and must
    be discarded rather than stamped.
    """
    page = vault / _PAGE_REL
    page.write_text(_page(title="One changed"), encoding="utf-8")

    other = vault / _OTHER_REL
    builds: list[str] = []
    real_build = semantic_contract._build_corpus_context_uncached

    def build_with_racing_event(root: Path, **kwargs):
        builds.append("build")
        context = real_build(root, **kwargs)
        # An external editor lands a change the registry sees, while this
        # populate's walk is already behind it.
        other.write_text(_page(title="Two leapfrogged"), encoding="utf-8")
        freshness.on_files_changed(root, changed=(other,))
        return context

    monkeypatch.setattr(
        semantic_contract, "_build_corpus_context_uncached", build_with_racing_event
    )
    confirms: list[Path] = []
    real_confirm = semantic_contract._deferred_corpus_census

    def counted_confirm(root: Path, sink, outcome: str):
        confirms.append(root)
        return real_confirm(root, sink, outcome)

    monkeypatch.setattr(semantic_contract, "_deferred_corpus_census", counted_confirm)

    semantic_contract.publish_corpus_files_changed(vault, changed=(page,))

    assert builds, "the populate must have attempted a build to be a real discard"
    # Attribution: the checkpoint decided this, not the census re-confirm that
    # follows it. That is the point of L3 -- the generation-bearing checkpoint
    # is the admission gate, and it is cheap enough to be consulted first.
    assert confirms == []
    cache_key = semantic_contract._corpus_cache_key(vault)
    # Never stamp: the leapfrogged context must reach neither the cache nor
    # the event-token map (contract L2 "on any mismatch discard ... never
    # stamp", L5 "entry, token and language hash move together").
    assert cache_key not in semantic_contract._CORPUS_CONTEXT_CACHE
    assert cache_key not in semantic_contract._CORPUS_CONTEXT_EVENT_TOKENS
    assert cache_key not in semantic_contract._CORPUS_CONTEXT_LANGUAGE_HASHES

    # And the next honest build sees BOTH deltas, so nothing was lost.
    context = semantic_contract.build_corpus_context(vault)
    assert context.pages[_PAGE_REL].title == "One changed"
    assert context.pages[_OTHER_REL].title == "Two leapfrogged"
