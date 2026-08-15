"""The joint freshness-liveness contract — both halves.

Source of truth: GitHub issue #508, comment "Joint freshness-liveness
contract". Two lanes implement it from opposite ends and both keep this file
green:

* the **corpus half** (#510 slices A/B), in `TestCorpusHalf` below — a Class B
  corpus-patch failure must leave vault freshness live, a Class A
  registry-advance failure must still withdraw, and populate-on-miss must never
  stamp a token onto a context an event leapfrogged (section 5, clauses L1-L5);
* the **graph half** (#508), at module level after it — the shared defending
  test of section 4 plus the deliverables it decomposes into: a refused graph
  publication is Class B, so it MUST NOT call `freshness.invalidate` or
  `freshness.mark_external_pending`, MUST record recovery state in its own
  store, and MUST own a bounded retry.

For the shared defending test (section 4), #510 owns assertion 2, #508 owns
assertions 3 and 4, and assertion 1 is jointly owned. **Neither lane may weaken
assertions 1 or 3 to make its own change pass.**

The rule both halves follow from: `freshness.*` describes what the event
registry knows about the vault's files. It must never be used to describe
whether a downstream projection managed to publish its own derived copy.

The two halves build deliberately different pages — `_corpus_page` carries no
headings, `_page` carries the `## Claim` block the graph lane's relation
assertions read — so both builders are kept and named for what they produce
rather than collapsed into one whose content would silently change the other
half's fixtures.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from exomem import (
    epistemic_graph,
    freshness,
    graph_sync,
    index_sync,
    relation_registry,
    semantic_contract,
)
from exomem import find as find_module
from exomem import vault as vault_module

_PAGE_REL = "Knowledge Base/Notes/Insights/one.md"
_OTHER_REL = "Knowledge Base/Notes/Insights/two.md"
_REGISTRY_REL = "Knowledge Base/_Schema/relation-registry.yaml"
_EXTENDED_REGISTRY = (
    "schema_version: 1\nextensions:\n  science.replicates:\n"
    "    parent: supports\n    description: Independent reproduction\n"
)


def _corpus_page(*, title: str = "Page", body: str = "Body.\n") -> str:
    return f"---\ntitle: {title}\ntype: insight\nstatus: active\nproject: atlas\n---\n\n{body}"


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    notes = tmp_path / "Knowledge Base" / "Notes" / "Insights"
    notes.mkdir(parents=True)
    (notes / "one.md").write_text(_corpus_page(title="One"), encoding="utf-8")
    (notes / "two.md").write_text(_corpus_page(title="Two"), encoding="utf-8")
    freshness.rebaseline(tmp_path)
    return tmp_path


def _spy_corpus_work(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[str]]:
    """Record the full-vault census walks AND the from-scratch corpus builds.

    Both seams, not just the census: a regression that rebuilds the corpus
    without walking it first, or walks without rebuilding, would slip past a
    spy on either one alone. An event-hit pays neither.
    """
    censuses: list[str] = []
    builds: list[str] = []
    real_census = semantic_contract._corpus_census
    real_build = semantic_contract._build_corpus_context_uncached

    def census_spy(root: Path):
        censuses.append(str(root))
        return real_census(root)

    def build_spy(root: Path, **kwargs):
        builds.append(str(root))
        return real_build(root, **kwargs)

    monkeypatch.setattr(semantic_contract, "_corpus_census", census_spy)
    monkeypatch.setattr(semantic_contract, "_build_corpus_context_uncached", build_spy)
    return censuses, builds


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


# --- corpus half (#510 slices A/B) -----------------------------------------


class TestCorpusHalf:
    """The corpus-cache lifecycle half of the contract.

    The cache-on fixture below is autouse **at class scope on purpose**: it
    forces the corpus cache on and clears the freshness registry around every
    test, which is right for these and wrong for the graph half, whose tests
    seed and inspect that registry themselves. Module-level autouse would have
    applied it to both halves silently.
    """

    @pytest.fixture(autouse=True)
    def _corpus_cache_on(self, monkeypatch: pytest.MonkeyPatch):
        # The suite-wide conftest defaults the corpus cache OFF; this contract is
        # entirely about that cache's lifecycle, so opt back in and start cold.
        monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
        semantic_contract.reset_corpus_context_cache()
        freshness.clear()
        yield
        semantic_contract.reset_corpus_context_cache()
        freshness.clear()

    def test_refused_corpus_patch_does_not_cool_vault_freshness(
        self,
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
        page.write_text(_corpus_page(title="One changed"), encoding="utf-8")
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
        other.write_text(_corpus_page(title="Two changed"), encoding="utf-8")
        assert index_sync.publish_corpus_delta(vault, changed=(other,)) is True

        cache_key = semantic_contract._corpus_cache_key(vault)
        assert semantic_contract._CORPUS_CONTEXT_EVENT_TOKENS.get(cache_key) == freshness.triple(
            vault, "vault"
        )

        censuses, builds = _spy_corpus_work(monkeypatch)
        context = semantic_contract.build_corpus_context(vault)
        assert (censuses, builds) == ([], [])
        assert context.pages[_OTHER_REL].title == "Two changed"
        assert context.pages[_PAGE_REL].title == "One changed"


    def test_registry_advance_failure_still_withdraws_vault_freshness(
        self,
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
        page.write_text(_corpus_page(title="One changed"), encoding="utf-8")

        assert index_sync.publish_corpus_delta(vault, changed=(page,)) is False

        assert freshness.triple(vault, "vault") is None
        assert freshness.is_live(vault, "vault") is False
        assert freshness.external_pending(vault) is True
        cache_key = semantic_contract._corpus_cache_key(vault)
        assert cache_key not in semantic_contract._CORPUS_CONTEXT_CACHE


    def test_publish_populates_a_cold_corpus_cache_for_the_next_reader(
        self,
        vault: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deliverable 8: the `entry is None` no-op must populate, not return.

        Red on `main` for the contract's stated reason: `semantic_contract.py`'s
        `_patch_corpus_files_changed_locked` returns silently when the cache holds
        no entry (sc:1745-1746), so a write onto a cold cache leaves it cold and
        the very next preflight pays a full stat census of the vault.
        """
        page = vault / _PAGE_REL
        page.write_text(_corpus_page(title="One changed"), encoding="utf-8")

        semantic_contract.publish_corpus_files_changed(vault, changed=(page,))

        cache_key = semantic_contract._corpus_cache_key(vault)
        entry = semantic_contract._CORPUS_CONTEXT_CACHE.get(cache_key)
        assert entry is not None
        assert entry[1].pages[_PAGE_REL].title == "One changed"
        assert semantic_contract._CORPUS_CONTEXT_EVENT_TOKENS.get(cache_key) == freshness.triple(
            vault, "vault"
        )

        censuses, builds = _spy_corpus_work(monkeypatch)
        context = semantic_contract.build_corpus_context(vault)
        assert (censuses, builds) == ([], [])
        assert context is entry[1]


    def test_populate_on_miss_never_stamps_a_context_an_event_leapfrogged(
        self,
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
        page.write_text(_corpus_page(title="One changed"), encoding="utf-8")

        other = vault / _OTHER_REL
        builds: list[str] = []
        real_build = semantic_contract._build_corpus_context_uncached

        def build_with_racing_event(root: Path, **kwargs):
            builds.append("build")
            context = real_build(root, **kwargs)
            # An external editor lands a change the registry sees, while this
            # populate's walk is already behind it.
            other.write_text(_corpus_page(title="Two leapfrogged"), encoding="utf-8")
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


    def test_populate_on_miss_never_stamps_a_context_built_with_a_superseded_registry(
        self,
        vault: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The populate loads its registries BEFORE its walk, so it must re-prove
        them against disk before installing.

        `_corpus_census` stats the two `_Schema` registry files at the END of its
        walk. A registry rewritten between the load and that stat therefore yields
        a census that records the NEW file captioning a context built with the OLD
        registry. The event-hit gate would refuse such an entry -- it re-checks the
        registry identity -- but the census-reuse path admits on census equality
        alone, and would hand that context back as current.

        Every other install site guards with `_registries_match_disk`; this pins
        that the populate does too.
        """
        page = vault / _PAGE_REL
        page.write_text(_corpus_page(title="One changed"), encoding="utf-8")
        registry_path = vault / _REGISTRY_REL
        superseded = relation_registry.load_registry(vault)

        real_census = semantic_contract._corpus_census
        walks: list[str] = []

        def census_with_a_registry_rewrite(root: Path):
            walks.append(str(root))
            if len(walks) == 1:
                # Interleaved into the populate's own walk window: the registry
                # this census is about to stat is no longer the one in hand.
                registry_path.parent.mkdir(parents=True, exist_ok=True)
                registry_path.write_text(_EXTENDED_REGISTRY, encoding="utf-8")
            return real_census(root)

        monkeypatch.setattr(semantic_contract, "_corpus_census", census_with_a_registry_rewrite)

        semantic_contract.publish_corpus_files_changed(vault, changed=(page,))

        assert walks, "the populate must have walked for this to be a real discard"
        assert relation_registry.load_registry(vault).extension_hash != superseded.extension_hash
        cache_key = semantic_contract._corpus_cache_key(vault)
        assert cache_key not in semantic_contract._CORPUS_CONTEXT_CACHE
        assert cache_key not in semantic_contract._CORPUS_CONTEXT_EVENT_TOKENS
        assert cache_key not in semantic_contract._CORPUS_CONTEXT_LANGUAGE_HASHES


    def test_populate_on_miss_never_overwrites_a_slot_a_competitor_filled(
        self,
        vault: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """L2's other admission half: the slot must still be the one observed.

        A concurrent cold build can install its own entry while this populate is
        still walking. The populate observed an EMPTY slot; a slot that is no
        longer empty means it lost the race, and losing means discarding, not
        overwriting -- the analogue of the
        `_CORPUS_CONTEXT_CACHE.get(cache_key) is entry` identity check every other
        stamp site makes.
        """
        page = vault / _PAGE_REL
        page.write_text(_corpus_page(title="One changed"), encoding="utf-8")
        cache_key = semantic_contract._corpus_cache_key(vault)
        competitor = (("competitor-census",), object())

        real_build = semantic_contract._build_corpus_context_uncached
        builds: list[str] = []

        def build_then_lose_the_slot(root: Path, **kwargs):
            builds.append("build")
            context = real_build(root, **kwargs)
            with semantic_contract._CORPUS_CONTEXT_CACHE_LOCK:
                semantic_contract._CORPUS_CONTEXT_CACHE[cache_key] = competitor
            return context

        monkeypatch.setattr(
            semantic_contract, "_build_corpus_context_uncached", build_then_lose_the_slot
        )

        semantic_contract.publish_corpus_files_changed(vault, changed=(page,))

        assert builds, "the populate must have built for this to be a real discard"
        assert semantic_contract._CORPUS_CONTEXT_CACHE[cache_key] is competitor
        assert cache_key not in semantic_contract._CORPUS_CONTEXT_EVENT_TOKENS
        assert cache_key not in semantic_contract._CORPUS_CONTEXT_LANGUAGE_HASHES


# --- graph half (#508) ------------------------------------------------------

INSIGHT_A = "Knowledge Base/Notes/Insights/contract-a.md"
INSIGHT_B = "Knowledge Base/Notes/Insights/contract-b.md"
INSIGHT_C = "Knowledge Base/Notes/Insights/contract-c.md"


def _page(title: str, body: str) -> str:
    return f"""---
type: insight
status: active
---
# {title}

## Claim

{body}
"""


def _seed_live_freshness(root: Path) -> None:
    freshness.seed(
        root,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault_module.walk_vault_md(root)),
    )
    kb = root / "Knowledge Base"
    freshness.seed(
        root,
        "kb",
        ((str(path), freshness.stat_signature(path)) for path in find_module._walk_md(kb)),
    )


def _governed_write(root: Path, rel: str, content: str) -> Path:
    """One governed write through the normal batch path, with post-commit fan-out."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(path, content)],
        vault_root=root,
    )
    return path


def _drain_background_rebuilds(timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while epistemic_graph._REBUILDING and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not epistemic_graph._REBUILDING, "a background graph rebuild did not finish"


def preserved_temporaries(root: Path) -> list[Path]:
    """Every retained `.graph-rebuild-*` artifact of a failed publication."""
    kb = root / "Knowledge Base"
    if not kb.is_dir():
        return []
    return sorted(
        candidate
        for candidate in kb.iterdir()
        if vault_module.is_graph_rebuild_runtime_file_name(candidate.name)
    )


def refuse_graph_publication(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """The existing injected-refusal idiom (tests/test_graph_rebuild_availability.py).

    Covers the Windows sharing-refusal branch on any platform.
    """
    refused: list[Path] = []

    def refuse_replacement(temporary: Path, _live: Path) -> None:
        refused.append(temporary)
        raise graph_sync.GraphSidecarReplaceUnavailable("live graph sidecar has an open reader")

    monkeypatch.setattr(graph_sync, "replace_sidecar", refuse_replacement)
    return refused


def publication_cycle(root: Path, paths: list[Path], *, governed: tuple[str, str]) -> None:
    """One production cycle in which the graph sidecar must be republished.

    Every surface here is real and is named by the contract:

    * the post-write graph fan-out (`index_sync.upsert_after_write`, the
      watcher / deferred-drain shape that runs outside a writer-lease direct
      mutation boundary).  A dispatched path outside the graph's retained event
      suffix cannot be bridged incrementally, so the fan-out rebuilds and
      publishes -- reaching `epistemic_graph.py`'s blanket `except Exception:
      freshness.mark_external_pending(...)` on that write path;
    * one governed write through the normal batch path, whose post-commit
      fan-out must now rebuild (the sidecar is no longer current) and whose
      refused publication leaves its graph checkpoint unacknowledged; and
    * the read-side warming rebuild the same stale sidecar schedules
      (`schedule_background_rebuild`), whose thread carries its own
      `mark_external_pending` on any failure.
    """
    index_sync.upsert_after_write(root, paths, publish_corpus_change=False)
    _governed_write(root, governed[0], governed[1])
    epistemic_graph.schedule_background_rebuild(root)
    _drain_background_rebuilds()


@pytest.fixture
def contract_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    root = tmp_path / "vault"
    (root / "Knowledge Base/Notes/Insights").mkdir(parents=True)
    (root / INSIGHT_A).write_text(
        _page("Contract A", "A claims against [[contract-b]]."), encoding="utf-8"
    )
    (root / INSIGHT_B).write_text(_page("Contract B", "B is a plain claim."), encoding="utf-8")
    _seed_live_freshness(root)
    epistemic_graph.EpistemicGraphIndex(root).rebuild_all()
    semantic_contract.build_corpus_context(root)
    return root


def test_refused_graph_publication_does_not_cool_vault_freshness(
    contract_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = contract_vault

    # --- Setup (contract 4): live freshness + a warm corpus entry through the
    # normal governed path.
    _governed_write(
        root, INSIGHT_B, _page("Contract B", "B is a plain claim, now with a body edit.")
    )
    semantic_contract.build_corpus_context(root)
    cache_key = semantic_contract._corpus_cache_key(root)
    assert freshness.triple(root, "vault") is not None
    assert semantic_contract._CORPUS_CONTEXT_EVENT_TOKENS.get(cache_key) is not None

    pre_failure_edges = epistemic_graph.EpistemicGraphIndex(root).edges()
    pre_failure_relations = {
        (edge["source_path"], edge["relation_type"]) for edge in pre_failure_edges
    }
    assert pre_failure_relations, "the pre-failure sidecar must carry at least one relation"

    # `mark_external_pending` allocates from one process-global, strictly
    # increasing clock (freshness.py `_external_pending_clock`).  Sampling it on
    # an unrelated root is how R1 -- "a Class B retry may never allocate a new
    # external-pending epoch" -- becomes observable without patching production.
    epoch_probe = tmp_path / "epoch-probe-root"
    clock_before = freshness.mark_external_pending(epoch_probe)

    # --- Act (contract 4): every graph publication is refused from here on.
    refused = refuse_graph_publication(monkeypatch)

    # A new page and its new relation arrive out of band (an external editor),
    # observed by the registry exactly as the watcher observes it.  The graph
    # fan-out also carries a sibling path outside the retained event suffix, so
    # the graph must rebuild and publish rather than bridge.
    new_page = root / INSIGHT_C
    new_page.write_text(
        _page("Contract C", "C claims against [[contract-a]]."), encoding="utf-8"
    )
    assert index_sync.publish_corpus_delta(root, changed=(new_page,)) is True
    publication_cycle(
        root,
        [new_page, root / INSIGHT_A],
        governed=(INSIGHT_B, _page("Contract B", "B gains a first governed body revision.")),
    )
    assert refused, "the fan-out must have attempted a graph publication"

    # --- Assertion 1 (jointly owned): liveness preserved. -------------------
    assert freshness.triple(root, "vault") is not None
    assert freshness.is_live(root, "vault") is True
    assert freshness.external_pending(root) is False

    first_cycle_temporaries = preserved_temporaries(root)
    refused_after_first_cycle = len(refused)

    # --- A second cycle, so the retry bound is observable. -------------------
    censuses: list[object] = []
    real_census = semantic_contract._corpus_census

    def counting_census(*args: object, **kwargs: object) -> object:
        censuses.append(args)
        return real_census(*args, **kwargs)

    monkeypatch.setattr(semantic_contract, "_corpus_census", counting_census)
    publication_cycle(
        root,
        [new_page, root / INSIGHT_A],
        governed=(INSIGHT_B, _page("Contract B", "B gains a second governed body revision.")),
    )
    semantic_contract.build_corpus_context(root)
    monkeypatch.setattr(semantic_contract, "_corpus_census", real_census)

    assert freshness.triple(root, "vault") is not None
    assert freshness.is_live(root, "vault") is True
    assert freshness.external_pending(root) is False

    # --- Assertion 2 (#510): the corpus cache stays warm. -------------------
    assert semantic_contract._CORPUS_CONTEXT_EVENT_TOKENS.get(cache_key) == freshness.triple(
        root, "vault"
    )
    assert semantic_contract.cached_corpus_census(root) is not None
    assert censuses == [], "a refused graph publication must not cost a full-vault census"

    # --- Assertion 3 (#508): graph fails closed, serves nothing as current. --
    index = epistemic_graph.EpistemicGraphIndex(root)
    assert index.available() is False
    assert graph_sync.status(root)["state"] in {"recovery_required", "unavailable"}
    served = {(edge["source_path"], edge["relation_type"]) for edge in index.edges()}
    assert served == set(), "a refused publication must serve no sidecar rows as current"
    assert not (served & pre_failure_relations)
    assert INSIGHT_C not in {str(node.get("path")) for node in index.nodes()}
    assert index.relation_participants(["supports"]).status != "available"

    # --- Assertion 4 (#508): bounded, not looping. --------------------------
    assert len(first_cycle_temporaries) <= 1, (
        "a refused publication may leave at most one preserved temporary per checkpoint"
    )
    assert len(preserved_temporaries(root)) <= 1
    assert freshness.external_pending_epoch(root) is None
    clock_after = freshness.mark_external_pending(epoch_probe)
    assert clock_after == clock_before + 1, (
        "a Class B retry must allocate no new external-pending epoch (contract R1)"
    )
    assert len(refused) > refused_after_first_cycle, (
        "the second cycle must still have retried the publication"
    )
