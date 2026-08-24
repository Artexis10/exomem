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

import sqlite3
import time
from contextlib import closing
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
from exomem.cli_ops import OpError

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


def _contract_checkpoint(generation: int) -> graph_sync.GraphSyncCheckpoint:
    return graph_sync.GraphSyncCheckpoint.create(
        generation=generation,
        mutation_id=f"{generation:024x}",
        paths=((INSIGHT_A, "d" * 64),),
        created_paths=(INSIGHT_A,),
    )


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

    def refuse_replacement(
        temporary: Path,
        _live: Path,
        *,
        vault_root: Path | None = None,
    ) -> None:
        del vault_root
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


# ---------------------------------------------------------------------------
# Deliverables owned by the #508 lane. The shared test above pins the joint
# outcome; these pin each clause the contract assigns to graph publish hygiene.
# ---------------------------------------------------------------------------


def test_class_c_marking_is_narrowed_to_a_moved_projection(
    contract_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D2: only a proven-stale projection may mark, not any non-stabilization."""
    root = contract_vault
    index = epistemic_graph.EpistemicGraphIndex(root)

    # A projection that provably moved under the in-flight proof: the supplied
    # freshness identity does not name the resolver bytes.
    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex,
        "_resolver_source_versions",
        lambda *_args, **_kwargs: None,
    )
    with pytest.raises(epistemic_graph.GraphProjectionMoved, match="did not stabilize"):
        index._rebuild_all_locked()
    assert freshness.external_pending(root) is True
    moved_epoch = freshness.external_pending_epoch(root)
    assert moved_epoch is not None
    monkeypatch.undo()

    freshness.clear_external_pending(root, through=moved_epoch)
    assert freshness.external_pending(root) is False

    # A marker that would not publish is a publication failure. It is *not*
    # evidence that the registry fell behind the disk, so it may not mark.
    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex,
        "_mark_available",
        lambda *_args, **_kwargs: False,
    )
    with pytest.raises(RuntimeError, match="did not stabilize") as raised:
        epistemic_graph.EpistemicGraphIndex(root)._rebuild_all_locked()
    assert not isinstance(raised.value, epistemic_graph.GraphProjectionMoved)
    assert freshness.external_pending(root) is False


def test_publication_failure_records_graph_recovery_state_not_vault_freshness(
    contract_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D3: a Class B write-path failure fences the graph and leaves freshness alone."""
    root = contract_vault
    _governed_write(root, INSIGHT_B, _page("Contract B", "B revised once."))
    refuse_graph_publication(monkeypatch)

    index_sync.upsert_after_write(root, [root / INSIGHT_A], publish_corpus_change=False)

    assert freshness.external_pending(root) is False
    assert freshness.is_live(root, "vault") is True
    index = epistemic_graph.EpistemicGraphIndex(root)
    # The graph's own fence: idempotent under retry, and cleared by
    # `file_watcher._recover_suspended_graph`.
    assert index.reads_suspended() is True
    assert index.available() is False
    assert epistemic_graph.publication_refusal_active(root) is True


def test_unclassifiable_dispatch_failure_still_marks_the_registry(
    contract_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D3: only *classified* failures are downgraded; the rest keep marking."""
    root = contract_vault
    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex,
        "refresh_paths",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("unclassified")),
    )

    epistemic_graph.upsert_after_write(root, [root / INSIGHT_A])

    assert freshness.external_pending(root) is True


def test_classification_covers_the_contract_named_class_b_members() -> None:
    """D3: the classifier, stated directly against contract section 1."""
    assert epistemic_graph.is_publication_failure(graph_sync.GraphSidecarReplaceUnavailable())
    assert epistemic_graph.is_publication_failure(
        graph_sync.GraphRebuildLockUnavailable("secure graph rebuild lock unavailable")
    )
    assert epistemic_graph.is_publication_failure(
        epistemic_graph.GraphPublicationUnavailable("owner lost")
    )
    assert epistemic_graph.is_publication_failure(OpError("MUTATION_BUSY", "busy"))
    assert not epistemic_graph.is_publication_failure(OpError("VAULT_UNREADABLE", "gone"))
    assert not epistemic_graph.is_publication_failure(ValueError("unclassified"))
    assert not epistemic_graph.is_publication_failure(graph_sync.GraphRebuildInProgress())
    # Class C already marked exactly once; a caller may not mark again (R1).
    assert not epistemic_graph.may_mark_external_pending(
        epistemic_graph.GraphProjectionMoved("moved")
    )
    assert not epistemic_graph.may_mark_external_pending(
        graph_sync.GraphRebuildInProgress()
    )
    assert epistemic_graph.may_mark_external_pending(ValueError("unclassified"))


def test_watcher_recovery_allocates_no_new_epoch_for_a_refused_publication(
    contract_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D4: the 300 s recovery loop must not re-contaminate the registry (R1/R2)."""
    from exomem.file_watcher import FileWatcher

    root = contract_vault
    _governed_write(root, INSIGHT_B, _page("Contract B", "B revised once."))
    refuse_graph_publication(monkeypatch)

    watcher = FileWatcher(root)
    pending_epoch = freshness.mark_external_pending(root)
    epoch_probe = tmp_path / "epoch-probe-root"
    clock_before = freshness.mark_external_pending(epoch_probe)

    watcher._recover_external_pending(pending_epoch)
    first_epoch = freshness.external_pending_epoch(root)

    # A second cycle over the same doomed publication.
    watcher._recover_external_pending(pending_epoch)
    watcher._recover_suspended_graph()

    assert freshness.external_pending_epoch(root) == first_epoch
    clock_after = freshness.mark_external_pending(epoch_probe)
    assert clock_after == clock_before + 1, (
        "each recovery cycle must not allocate a fresh external-pending epoch"
    )


def test_watcher_external_owner_contention_adds_no_failure_recovery_state(
    contract_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.file_watcher import FileWatcher

    root = contract_vault
    watcher = FileWatcher(root)
    pending_epoch = freshness.mark_external_pending(root)
    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex,
        "rebuild_all",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            graph_sync.GraphRebuildInProgress()
        ),
    )
    monkeypatch.setattr(
        epistemic_graph,
        "record_publication_recovery_state",
        lambda *_args, **_kwargs: pytest.fail(
            "a verified external owner must not install failure recovery state"
        ),
    )

    watcher._recover_external_pending(pending_epoch)

    assert epistemic_graph.publication_refusal_active(root) is False


def test_suspended_graph_recovery_does_not_resuspend_an_external_owner_publication(
    contract_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = contract_vault
    suspend_calls: list[str] = []
    monkeypatch.setattr(epistemic_graph, "recovery_decline_reason", lambda _root: None)
    monkeypatch.setattr(find_module, "evict_resolver_caches", lambda _root: None)
    monkeypatch.setattr(vault_module, "evict_inbound_index", lambda _root: None)
    monkeypatch.setattr(freshness, "external_pending", lambda _root: False)
    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex,
        "withdraw_availability",
        lambda _self: None,
    )
    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex,
        "rebuild_all",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            graph_sync.GraphRebuildInProgress()
        ),
    )
    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex,
        "suspend_reads",
        lambda _self: suspend_calls.append("suspended"),
    )

    assert epistemic_graph.recover_suspended_graph(root) is False
    assert suspend_calls == []


def test_watcher_seed_validation_does_not_resuspend_an_external_owner_publication(
    contract_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.file_watcher import FileWatcher

    root = contract_vault
    sidecar = root / ".graph.sqlite"
    sidecar.write_bytes(b"owner")
    suspend_calls: list[str] = []
    monkeypatch.setattr(epistemic_graph, "sidecar_path", lambda _root: sidecar)
    monkeypatch.setattr(epistemic_graph, "graph_enabled", lambda: True)
    monkeypatch.setattr(find_module, "evict_resolver_caches", lambda _root: None)
    monkeypatch.setattr(vault_module, "evict_inbound_index", lambda _root: None)
    monkeypatch.setattr(index_sync, "recover_full_receipt_graph_epoch", lambda *_a, **_k: True)
    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex,
        "suspend_reads",
        lambda _self: suspend_calls.append("suspended"),
    )
    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex,
        "rebuild_all",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            graph_sync.GraphRebuildInProgress()
        ),
    )

    assert FileWatcher(root)._validate_existing_graph_on_seed() is False
    assert suspend_calls == ["suspended"]


@pytest.mark.parametrize("graph_scheduling", [True, False], ids=["scheduled", "disabled"])
def test_watcher_drain_allocates_no_epoch_when_the_fan_out_publication_is_refused(
    contract_vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    graph_scheduling: bool,
) -> None:
    """D4: the drain's own completeness check is a third door onto the registry.

    `_dispatch_batch` withdraws the graph marker, compare-and-acks the drained
    epoch, fans out, and then asserts the graph came back current. When the
    fan-out's publication is refused (Class B) the barrier is still set, so that
    assertion fails and the drain marks the vault externally pending again --
    a fresh epoch on *every* drain cycle. That is the self-sustaining loop the
    contract exists to end, and it falsifies the D7 precondition that
    `external_pending` is set only by Class A and Class C.

    The `disabled` case is the same site reached the other way: with
    `EXOMEM_DISABLE_GRAPH_SCHEDULING=1` — the mitigation deployed today — no
    publication is even attempted, so the graph is *configured* not to be
    current. That is not a failure to classify either, and it must not cool the
    registry for as long as the mitigation is on.
    """
    from exomem.file_watcher import FileWatcher

    root = contract_vault
    _governed_write(root, INSIGHT_B, _page("Contract B", "B revised once."))
    refuse_graph_publication(monkeypatch)
    if not graph_scheduling:
        monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_SCHEDULING", "1")
    watcher = FileWatcher(root)

    epoch_probe = tmp_path / "epoch-probe-root"
    clock_before = freshness.mark_external_pending(epoch_probe)

    for cycle, body in enumerate(("first external edit", "second external edit")):
        edited = root / INSIGHT_A
        edited.write_text(
            _page("Contract A", f"A claims against [[contract-b]] -- {body}."),
            encoding="utf-8",
        )
        # Exactly what `_record` does when watchdog observes an out-of-band edit.
        pending_epoch = freshness.mark_external_pending(root)
        watcher._dispatch_batch(
            [edited],
            [INSIGHT_A],
            [],
            cap=False,
            pending_epoch=pending_epoch,
        )
        assert freshness.external_pending(root) is False, (
            f"drain {cycle} re-armed the vault for a refused publication"
        )
        assert freshness.is_live(root, "vault") is True
        # The graph is fenced by the barrier it owns, which is idempotent under
        # retry -- that is what replaces the epoch.
        assert epistemic_graph.EpistemicGraphIndex(root).reads_suspended() is True

    clock_after = freshness.mark_external_pending(epoch_probe)
    # Two legitimate Class A observations plus this probe: nothing else.
    assert clock_after == clock_before + 3, (
        "a refused fan-out publication must allocate no external-pending epoch"
    )


def test_watcher_drain_still_marks_unclassified_graph_incompleteness(
    contract_vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D3/D4: the fail-closed default the two carve-outs are carved out of.

    The drain site has exactly two reasons not to mark -- a refused publication
    (Class B, memoized by the projection) and scheduling deliberately off -- and
    both are pinned above. This pins the branch they are exceptions to: a graph
    that is simply not current, with no refusal recorded and scheduling on, is
    an incompleteness this module cannot classify, so it must keep cooling the
    registry exactly as it did before the contract landed.

    Without this test a future widening of either carve-out could swallow a
    genuine Class A signal with the whole suite still green.
    """
    from exomem.file_watcher import FileWatcher

    root = contract_vault
    _governed_write(root, INSIGHT_B, _page("Contract B", "B revised once."))
    epistemic_graph.clear_publication_memos()

    # A graph dispatch that fails without ever attempting -- let alone being
    # refused -- a publication: nothing writes a refusal memo, so neither
    # carve-out can apply.
    def deferred_without_publishing(*_args: object, **_kwargs: object):
        return epistemic_graph.GraphDispatchResult("failed", "graph_dispatch_failed")

    monkeypatch.setattr(epistemic_graph, "upsert_after_write", deferred_without_publishing)

    watcher = FileWatcher(root)
    epoch_probe = tmp_path / "epoch-probe-root"
    clock_before = freshness.mark_external_pending(epoch_probe)

    edited = root / INSIGHT_A
    edited.write_text(
        _page("Contract A", "A claims against [[contract-b]] -- unclassified."),
        encoding="utf-8",
    )
    pending_epoch = freshness.mark_external_pending(root)
    watcher._dispatch_batch(
        [edited], [INSIGHT_A], [], cap=False, pending_epoch=pending_epoch
    )

    # Neither carve-out was available, so the default had to fire.
    assert epistemic_graph.publication_refusal_active(root) is False
    assert epistemic_graph.graph_scheduling_enabled() is True
    assert epistemic_graph.EpistemicGraphIndex(root).available() is False
    assert freshness.external_pending(root) is True, (
        "an unclassified graph incompleteness must still fail closed"
    )
    clock_after = freshness.mark_external_pending(epoch_probe)
    # One watchdog observation, one mark from the drain site, one probe.
    assert clock_after == clock_before + 3


def test_refused_publication_is_memoized_instead_of_retried_at_full_cost(
    contract_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D5: the lexstore `_REPAIRS_IN_FLIGHT` shape, applied to graph publication."""
    root = contract_vault
    _governed_write(root, INSIGHT_B, _page("Contract B", "B revised once."))
    refuse_graph_publication(monkeypatch)
    epistemic_graph.clear_publication_memos()

    assert epistemic_graph.schedule_background_rebuild(root) is True
    _drain_background_rebuilds()

    assert epistemic_graph.publication_refusal_active(root) is True
    assert epistemic_graph.schedule_background_rebuild(root) is False, (
        "the same doomed publication must not be re-attempted at full rebuild cost"
    )

    # The memo is projection-local and bounded: clearing it restores the retry.
    epistemic_graph.clear_publication_memos()
    assert epistemic_graph.schedule_background_rebuild(root) is True
    _drain_background_rebuilds()


def test_repeated_refusal_keeps_at_most_one_preserved_temporary(
    contract_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D6: `preserve_temporary` is bounded; the 527 MB orphan field cannot recur."""
    root = contract_vault
    _governed_write(root, INSIGHT_B, _page("Contract B", "B revised once."))
    refused = refuse_graph_publication(monkeypatch)
    index = epistemic_graph.EpistemicGraphIndex(root)

    for cycle in range(4):
        with pytest.raises(graph_sync.GraphRebuildRegistrationError):
            index.rebuild_all()
        retained = preserved_temporaries(root)
        assert len(retained) <= 1, f"cycle {cycle} retained {len(retained)}: {retained}"

    assert len(refused) >= 4
    retained = preserved_temporaries(root)
    assert len(retained) == 1
    # The survivor is the newest complete build, still recoverable.
    with closing(sqlite3.connect(retained[0])) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_reaper_never_collects_an_in_flight_registered_temporary(
    contract_vault: Path,
) -> None:
    """D6: a build this process still holds is not an orphan."""
    root = contract_vault
    live = epistemic_graph.sidecar_path(root)
    in_flight = graph_sync.temporary_sidecar_path(live, _contract_checkpoint(1))
    orphan_older = graph_sync.temporary_sidecar_path(live, _contract_checkpoint(2))
    orphan_newer = graph_sync.temporary_sidecar_path(live, _contract_checkpoint(3))
    for path in (in_flight, orphan_older, orphan_newer):
        path.write_bytes(b"sqlite artifact")
        time.sleep(0.02)
    graph_sync.register_temporary(in_flight)
    try:
        removed = epistemic_graph._reap_preserved_temporaries(live, root)
    finally:
        graph_sync.unregister_temporary(in_flight.resolve())

    assert in_flight.exists()
    assert orphan_newer.exists()
    assert not orphan_older.exists()
    assert removed == [orphan_older]


def test_reaper_leaves_everything_alone_while_another_owner_is_building(
    contract_vault: Path,
) -> None:
    """D6: process-local registration cannot see an out-of-process build.

    A foreign repair's temporary is fresh and complete but unregistered here.
    Reaping it would be worse than the orphans it collects -- on Linux the
    unlink succeeds, that builder's `os.replace` then raises FileNotFoundError,
    and an unclassified error is exactly what still cools the registry. The
    cross-process rebuild-owner claim is the only thing that can tell the two
    apart, so failing to take it must mean reaping nothing.
    """
    root = contract_vault
    live = epistemic_graph.sidecar_path(root)
    # Deliberately the OLDEST, so recency cannot accidentally save it: only the
    # ownership claim can.
    foreign_in_flight = graph_sync.temporary_sidecar_path(live, _contract_checkpoint(1))
    orphan_older = graph_sync.temporary_sidecar_path(live, _contract_checkpoint(2))
    orphan_newer = graph_sync.temporary_sidecar_path(live, _contract_checkpoint(3))
    for path in (foreign_in_flight, orphan_older, orphan_newer):
        path.write_bytes(b"sqlite artifact")
        time.sleep(0.02)

    # Stand in for the other process: hold the same cross-process claim it would.
    owner_probe = live.with_name(".graph-rebuild-foreign-owner.sqlite")
    assert graph_sync.claim_rebuild_owner(root, owner_probe) is True
    try:
        removed = epistemic_graph._reap_preserved_temporaries(live, root)
    finally:
        graph_sync.release_rebuild_owner(root, owner_probe)

    assert removed == []
    assert foreign_in_flight.exists(), "a foreign in-flight build must survive"
    assert orphan_older.exists()
    assert orphan_newer.exists()

    # Once that owner releases, nothing is in flight anywhere and the same call
    # bounds the retained set to the newest complete build.
    removed = epistemic_graph._reap_preserved_temporaries(live, root)
    assert set(removed) == {foreign_in_flight, orphan_older}
    assert orphan_newer.exists()


def test_publication_hold_is_platform_gated(contract_vault: Path) -> None:
    """Linux keeps the plain `os.replace` fast path: no hold, no drain."""
    root = contract_vault
    index = epistemic_graph.EpistemicGraphIndex(root)
    epistemic_graph.reset_publication_holds()

    hold = index._before_publish_replacement(index.path, index.path)
    try:
        if epistemic_graph._reader_cycling_enabled():
            assert hold == epistemic_graph._sidecar_registry_key(index.path)
            assert epistemic_graph._SIDECAR_PUBLICATION_HOLDS == {hold}
        else:
            assert hold is None
            assert epistemic_graph._SIDECAR_PUBLICATION_HOLDS == set()
    finally:
        epistemic_graph._release_publication_hold(hold)
    assert epistemic_graph._SIDECAR_PUBLICATION_HOLDS == set()
