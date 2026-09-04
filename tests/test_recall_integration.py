"""Where the four recall lanes meet.

Each lane proved its own contract against its own fixture. That is necessary
and it is not sufficient: the defects this file exists to catch live in the
seams, where one lane's guarantee is another lane's premise, and where each
half is individually correct.

Three interaction nodes, each naming the lanes it crosses:

* **A filtered recall after a governed write** — Lane 2 moved the `projects`
  and `tags` filters onto the maintained page catalogue; Lane 3 made a governed
  write invalidate exactly the rows it touched. Lane 2 alone would answer a
  filter from a stale catalogue row and hide a page under its new project;
  Lane 3 alone would have no index to invalidate. The node drives the REAL
  write seam (`semantic_writes.preflight_existing`/`commit_existing`) rather
  than a hand-built receipt, because a hand-built receipt is the thing under
  test one level down.
* **Widening with a page under pending custody** — Lane 4 made out-of-KB
  widening opt-in and catalogue-backed; Lane 3 owns what a reader sees between
  a durable commit and the catalogue republication. The dangerous combination
  is a widened recall reaching a sibling tree whose rows are mid-flight.
* **The walk sentinel on every request shape** — Lane 1's structural counter,
  swept across the shapes the other three lanes introduced. A walk that
  reappears in any one of them is caught here even if that lane's own suite
  measures a different shape.

Every node runs against a warm MANAGED cell, because the contract governs the
managed reader; an offline caller keeps its scan fallback by design, and
measuring that would measure the wrong path.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from exomem import bm25, commands, find_corpus, semantic_writes
from exomem import find as find_module
from exomem.vault import kb_dirname

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import recall_latency_gate as gate  # noqa: E402, I001

TOKEN = "integrationprobe"

#: The stages the contract requires to consume maintained indexes. Kept in step
#: with `scripts/recall_latency_gate.py::WALKER_STAGES` by
#: `test_the_gate_and_the_integration_suite_watch_the_same_stages`.
WALKER_STAGES: tuple[str, ...] = (
    "filter_eligibility",
    "outside_kb",
    "recall_projection",
    "pending_visibility",
    "keyword",
    "filter_hits",
)


def _scope_roots(vault: Path) -> tuple[Path, ...]:
    """Every directory the contract forbids enumerating on the reader thread.

    The vault root subsumes the KB scope, but the widening lane reaches sibling
    trees (`Reference/`), so both are named.
    """
    return (vault, vault / kb_dirname())


class _PageReadSentinel:
    """Counts `.md` page-content reads of a scope on the reader thread.

    The contract names three verbs — no stage "SHALL enumerate, read or parse
    every page of the vault or of the knowledge-base scope on the reader
    thread" — and `ScopeWalkSentinel` instruments only the first. That gap is
    not theoretical: the empty-query browse was moved off `_walk_md` and onto a
    catalogue query, the enumeration count went to zero, and the browse went on
    reading 37 of 40 pages per request, because it still called `_resolve_page`
    for every path the index handed it. `find_corpus.CACHE.get` opens and reads
    the file bytes on EVERY call, hit or miss, since its signature
    content-hashes; the cache saves the parse, never the read. So an
    enumeration counter alone reports a fix that moved the walk rather than
    removing it, and reports it as a pass.

    Every page read on this interpreter bottoms out in
    `find_corpus._read_page_snapshot` — `CACHE.get`, `_read_page_bytes` and
    `parse_page` all route through it — so counting that one name catches a
    read regardless of which caller reintroduces it.

    Thread scoping matches `ScopeWalkSentinel`'s and exists for the same
    reason: a governed write starts a graph rebuild on its own thread, and the
    contract governs the reader.
    """

    def __init__(self, *scope_roots: Path, current_thread_only: bool = True) -> None:
        self._roots = tuple(os.path.realpath(root) for root in scope_roots)
        self._owner = threading.get_ident() if current_thread_only else None
        self.read: list[str] = []

    @property
    def count(self) -> int:
        return len(self.read)

    def reset(self) -> None:
        self.read.clear()

    def report(self) -> str:
        distinct = sorted(set(self.read))
        return "\n".join(
            [f"{len(self.read)} page reads ({len(distinct)} distinct)", *distinct]
        )

    def record(self, path: object) -> None:
        if self._owner is not None and threading.get_ident() != self._owner:
            return
        try:
            resolved = os.path.realpath(os.fspath(path))  # type: ignore[arg-type]
        except (TypeError, ValueError, OSError):
            return
        for root in self._roots:
            if resolved.startswith(root + os.sep):
                self.read.append(resolved)
                return

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = find_corpus._read_page_snapshot

        def counting_read(path, vault_root):
            self.record(path)
            return real(path, vault_root)

        monkeypatch.setattr(find_corpus, "_read_page_snapshot", counting_read)


def _page(title: str, *, project: str, updated: str = "2026-08-20") -> str:
    return (
        "---\n"
        f"title: {title}\n"
        "type: note\n"
        "status: active\n"
        f"updated: {updated}\n"
        f"projects: [{project}]\n"
        "tags: [integration]\n"
        "---\n\n"
        f"# {title}\n\n"
        "## Observations\n\n"
        f"- [config] {TOKEN} #integration (test) ^{title.lower().replace(' ', '-')}\n"
    )


def _seed_kb(
    vault: Path, rel: str, title: str, *, project: str, updated: str = "2026-08-20"
) -> Path:
    """Put a page on disk BEFORE the cell is warmed, so the catalogue holds it."""
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_page(title, project=project, updated=updated), encoding="utf-8")
    return path


def _seed_outside(vault: Path, name: str) -> str:
    """A page beyond the knowledge base, for the widening lane to reserve."""
    reference = vault / "Reference"
    reference.mkdir(parents=True, exist_ok=True)
    (reference / name).write_text(
        f"# {name}\n\n{TOKEN} beyond the knowledge base.\n", encoding="utf-8"
    )
    return f"Reference/{name}"


def _govern(vault: Path, rel: str, title: str, *, project: str) -> None:
    """One governed write through the real seam: receipt, custody and all."""
    preflight = semantic_writes.preflight_existing(
        vault,
        path=rel,
        after_source=_page(title, project=project),
        operation="observe",
    )
    assert preflight.contract_result.should_block is False
    committed = semantic_writes.commit_existing(vault, preflight=preflight)
    assert committed.mutated is True


def _recall(vault: Path, **kwargs: Any) -> dict:
    params: dict[str, Any] = {
        "query": TOKEN,
        "mode": "hybrid",
        "scope": "kb",
        "graph": False,
        "include_timings": True,
    }
    params.update(kwargs)
    return commands.op_find(vault, **params)


def _paths(result: dict) -> list[str]:
    return [hit["path"] for hit in result["hits"]]


def _stage_sources(result: dict) -> dict[str, str]:
    return {
        name: stage["source"]
        for name, stage in result["timings"]["stages"].items()
        if "source" in stage
    }


def _walking_stages(result: dict) -> list[str]:
    """Stages that said they computed an answer the contract wants indexed."""
    return sorted(
        name
        for name, source in _stage_sources(result).items()
        if source == "computed" and name.rsplit(".", 1)[-1] in WALKER_STAGES
    )


#: The absolute cap that replaces the ratio below the contract's smallest
#: stated scale. Set NEAR THE MEASUREMENT rather than at the contract's own
#: smallest ceiling, because the allowance is a floor under a flake and not a
#: budget. `unattributed_ms` is near-constant whatever the total: 2.25-3.83 ms
#: over totals of 23 to 148 ms when the integration reviewer measured it on a
#: loaded box, and 1.0-2.1 ms over totals of 10.4 to 66.0 across three runs of
#: the node below on a quiet one. So 8 ms is roughly 2x the worst reading ever
#: taken here and 4x the typical one.
#:
#: 18 ms was derived — the contract's own 15% at the contract's own 120 ms —
#: and derived was not the same as tight. EVERY one of the eight shapes below
#: falls under 120 ms and takes the fallback, and on the smallest of them 18 ms
#: was 5.2x the ratio it replaced and 78% of the whole request, so a regression
#: adding 15 ms of uninstrumented time — a 65% latency increase on that shape —
#: passed in silence. An allowance the common case relies on is the rule, not a
#: fallback, and it has to be sized like one.
#:
#: Pinned as a literal in
#: `test_the_attribution_allowance_falls_back_only_below_the_contract_scale`
#: rather than read back from here, for the reason the gate's ceilings are: a
#: test that asserts a constant against itself blesses whatever the constant
#: becomes.
_FLAT_ATTRIBUTION_ALLOWANCE_MS = 8.0


def _attribution_allowance(total_ms: float) -> float:
    """The 15% bound, with its premise stated rather than assumed.

    The spec's bound is a RATIO, and a ratio needs a denominator the contract
    recognises. `unattributed_ms` is close to a fixed cost — 2.25 to 3.83 ms
    across every shape here — so on a fixture-scale request the ratio is driven
    almost entirely by how small the total is, not by how much time went
    unaccounted for. An earlier arrangement of this node where the unfiltered
    shape ran first after warm-up measured 2.7 ms of 17.2 ms, or 15.6%, and
    breached. That is a flake waiting, and widening the RATIO to stop it would
    retire the only guard the spec gives against uninstrumented time.

    So the ratio is not loosened. Below the smallest ceiling the contract
    states anywhere — the 120 ms keyword p50 — it is replaced by an absolute
    cap sized against what this actually costs. See
    `_FLAT_ATTRIBUTION_ALLOWANCE_MS` for why that is 8 ms and not the 18 ms
    the contract's own arithmetic produces at that scale.

    On the live cell the question does not arise: the 0.69.0 baseline puts
    unattributed at 12.8-16.0 ms against totals of 561-1037 ms, or 1.5-2.3%,
    and every request clears 120 ms comfortably, so the ratio governs.
    """
    if total_ms >= gate.KEYWORD_P50_MS:
        return 0.15 * total_ms
    return _FLAT_ATTRIBUTION_ALLOWANCE_MS


def _warm(vault: Path, warm_managed_cell) -> None:
    """Warm the cell AFTER the corpus is seeded, then hydrate the caches.

    Order matters twice over, and getting it wrong is silent.

    `find_module.clear_cache()` drops the freshness registry as well as the
    page and result caches, so running it AFTER `warm_managed_cell` un-warms
    the very thing the fixture published. Every later recall then takes the
    cold-projection fallback, which walks — 96 to 246 scope enumerations per
    request when this was written the other way round. It runs before the
    warm-up, where its job is to stop a previous test's rows answering for
    pages this one just wrote.

    The trailing recall is not decoration either: it hydrates the substrate
    caches the way a served cell's first request does, so what the nodes below
    measure is a warm read path rather than a first-touch one.
    """
    find_module.clear_cache()
    bm25.clear_cache()
    warm_managed_cell(vault)
    _recall(vault)


# --- Node 1: Lane 2's index meets Lane 3's custody --------------------------


def test_a_filtered_recall_after_a_governed_write_sees_the_new_project(
    vault: Path, warm_managed_cell, walk_sentinel
) -> None:
    """The read-your-write seam, answered from an index rather than a walk.

    Lane 2's catalogue answers the `projects` filter. Lane 3's exact custody is
    what keeps that answer current across a governed write. With either half
    missing the recall is still well-formed and still wrong: the page comes
    back under the project it no longer carries, and is invisible under the one
    it does. There is no error to see it by, which is exactly why it is pinned.
    """
    rel = f"{kb_dirname()}/Notes/Integration/integration-custody.md"
    _seed_kb(vault, rel, "Integration Custody", project="project-alpha")
    _warm(vault, warm_managed_cell)

    assert rel in _paths(_recall(vault, projects=["project-alpha"])), (
        "the premise failed: the seeded page must be visible under its first project"
    )

    _govern(vault, rel, "Integration Custody", project="project-beta")

    # `current_thread_only`, and the reason is the contract's own wording: no
    # stage "SHALL enumerate, read or parse every page ... ON THE READER
    # THREAD". A governed write is followed by background scope enumerations —
    # 86 seen here, and every one of them, instrumented with stack capture,
    # comes from `epistemic_graph.py` (`_disk_vault_freshness`,
    # `_recall_membership` and the genexpr beside it) through
    # `recall_policy.iter_recall_markdown` to `vault.walk_vault_md`. That is
    # the graph rebuild, which design.md places out of scope under
    # `converge-graph-incrementally`.
    #
    # It is worth naming precisely, because the obvious guess is wrong in an
    # important way: this is NOT Lane 3's scheduled lexical repair. A lexical
    # repair walking after every governed write would be evidence that exact
    # custody had failed, which is the opposite of what this measures.
    #
    # The no-write sweep in `test_no_request_shape_walks_the_corpus` stays
    # all-thread, so the difference between the two is itself pinned: zero
    # everywhere before a write, zero on the reader thread after one.
    sentinel = walk_sentinel(*_scope_roots(vault), current_thread_only=True)
    sentinel.reset()
    under_new = _paths(_recall(vault, projects=["project-beta"]))
    under_old = _paths(_recall(vault, projects=["project-alpha"]))

    assert rel in under_new, "a governed write's new project was not visible to the filter"
    assert rel not in under_old, "the filter answered from a stale catalogue row"
    assert sentinel.count == 0, sentinel.report()


def test_the_filtered_recall_after_a_write_is_still_index_backed(
    vault: Path, warm_managed_cell
) -> None:
    """Custody must keep the answer current WITHOUT falling back to the walk.

    A read-your-write that is satisfied by re-reading the page from disk is
    correct and costs exactly what this change exists to remove. So the node
    above is not enough on its own: correctness there plus `computed` here
    would mean the index was abandoned rather than maintained.
    """
    rel = f"{kb_dirname()}/Notes/Integration/integration-source.md"
    _seed_kb(vault, rel, "Integration Source", project="project-alpha")
    _warm(vault, warm_managed_cell)
    _govern(vault, rel, "Integration Source", project="project-beta")

    result = _recall(vault, projects=["project-beta"])

    assert _walking_stages(result) == [], (
        f"a stage walked after a governed write: {_walking_stages(result)}"
    )


# --- Node 2: Lane 4's widening meets Lane 3's pending custody ---------------


def test_widening_with_a_page_under_pending_custody_does_not_walk(
    vault: Path, warm_managed_cell, walk_sentinel
) -> None:
    """Opt-in widening reaching a sibling tree while a write is mid-flight.

    Lane 4's reserve is answered from the catalogue. Lane 3 decides what a
    reader sees between a durable commit and the catalogue's republication. The
    combination is the risk: a widened recall reaches `Reference/`, which the
    KB-scoped suites never touch, at the moment the rows for a KB page are
    stale. Declining is a permitted answer here; walking is not.
    """
    outside = _seed_outside(vault, "integration-widen.md")
    rel = f"{kb_dirname()}/Notes/Integration/integration-widen-kb.md"
    _seed_kb(vault, rel, "Integration Widen", project="project-alpha")
    _warm(vault, warm_managed_cell)

    _govern(vault, rel, "Integration Widen", project="project-beta")

    # Reader thread only, for the reason given on the node above: the graph
    # rebuild enumerates on its own thread after a governed write, out of scope
    # for this change, and the contract governs the reader.
    sentinel = walk_sentinel(*_scope_roots(vault), current_thread_only=True)
    sentinel.reset()
    result = _recall(vault, widen_outside_kb=True, limit=5)

    assert sentinel.count == 0, sentinel.report()
    assert _walking_stages(result) == [], (
        f"a stage walked under widening with pending custody: {_walking_stages(result)}"
    )
    stages = result["timings"]["stages"]
    assert "outside_kb" in stages, (
        "the widening stage reported nothing at all; a stage that did not run must still say so"
    )
    # The reserve is permitted to be empty or to decline; what it may not do is
    # find its answer by reading the sibling tree.
    assert outside.startswith("Reference/")


def test_widening_stays_off_unless_it_is_asked_for_even_after_a_write(
    vault: Path, warm_managed_cell
) -> None:
    """Lane 4's default survives Lane 3's invalidation.

    A cache invalidation that rebuilt the request under different defaults
    would reintroduce the behaviour change Lane 4 made opt-in, and it would do
    it only on the request after a write — the shape least likely to be noticed.
    """
    _seed_outside(vault, "integration-default.md")
    rel = f"{kb_dirname()}/Notes/Integration/integration-default-kb.md"
    _seed_kb(vault, rel, "Integration Default", project="project-alpha")
    _warm(vault, warm_managed_cell)

    _govern(vault, rel, "Integration Default", project="project-beta")
    result = _recall(vault, limit=5)

    outside_hits = [hit["path"] for hit in result["hits"] if hit.get("outside_kb")]
    assert outside_hits == [], (
        f"a default scope='kb' recall widened after a governed write: {outside_hits}"
    )


# --- Node 3: Lane 1's sentinel over every shape the other lanes introduced ---


_SHAPES: dict[str, dict[str, Any]] = {
    "hybrid_unfiltered": {},
    "hybrid_filtered": {"projects": ["project-alpha"]},
    "keyword": {"mode": "keyword"},
    "hybrid_widened": {"widen_outside_kb": True},
    "hybrid_widened_filtered": {"widen_outside_kb": True, "projects": ["project-alpha"]},
    "kb_only": {"scope": "kb-only"},
    # The schema default. `ask_memory.query` ships `"default": ""` with "Empty
    # means recent/filtered recall", and this shape had no keyword candidates
    # and no structured filter, so it fell through to `_walk_md` and
    # enumerated the whole scope on the reader thread — 19 enumerations on a
    # warm managed cell. `query="" types=[...]` never did, because a filter
    # gives it a resolved set, so the walk was reachable only on the shape the
    # tool surface recommends for browsing.
    "empty_query": {"query": ""},
    "empty_query_whitespace": {"query": "   "},
}


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_no_request_shape_walks_the_corpus(
    vault: Path, warm_managed_cell, walk_sentinel, shape: str
) -> None:
    """The aggregate sweep: every shape the four lanes can produce, at zero.

    Parametrized rather than looped so a single failing shape is named by the
    node id and cannot be masked by an earlier one's failure.
    """
    _seed_outside(vault, "integration-sweep.md")
    rel = f"{kb_dirname()}/Notes/Integration/integration-sweep-kb.md"
    _seed_kb(vault, rel, "Integration Sweep", project="project-alpha")
    _warm(vault, warm_managed_cell)

    sentinel = walk_sentinel(*_scope_roots(vault))
    sentinel.reset()
    result = _recall(vault, **_SHAPES[shape])

    assert sentinel.count == 0, f"{shape}: {sentinel.report()}"
    assert _walking_stages(result) == [], f"{shape} walked at {_walking_stages(result)}"


def test_the_empty_query_browse_is_answered_from_the_index_not_a_walk(
    vault: Path, warm_managed_cell, walk_sentinel
) -> None:
    """INT-1: the schema default must not enumerate the scope.

    `ask_memory(query="")` is what the tool surface recommends for browsing,
    and it had neither keyword candidates nor a structured filter, so it fell
    through to `_walk_md` and enumerated the whole knowledge base on the reader
    thread. `query=""` with any filter never did, which is why the walk
    survived every filtered pin the other lanes wrote.
    """
    _seed_kb(
        vault,
        f"{kb_dirname()}/Notes/Integration/integration-empty.md",
        "Integration Empty",
        project="project-alpha",
    )
    _warm(vault, warm_managed_cell)

    sentinel = walk_sentinel(*_scope_roots(vault), current_thread_only=True)
    sentinel.reset()
    result = _recall(vault, query="")

    assert result["hits"], "the premise failed: an empty-query browse returned nothing"
    assert sentinel.count == 0, sentinel.report()
    assert _stage_sources(result).get("keyword") == "index", (
        "the hydration stage must say it was answered from an index: "
        f"{_stage_sources(result).get('keyword')!r}"
    )


#: A browse corpus big enough that "reads every page" and "reads only what the
#: request can return" are different numbers by an order of magnitude.
_BROWSE_CORPUS = 40
_BROWSE_LIMIT = 5


def _seed_browse_corpus(vault: Path) -> list[str]:
    """`_BROWSE_CORPUS` pages with distinct `updated` dates, newest last.

    Distinct dates matter: they give the browse a real ordering key to resolve
    from the catalogue, so a fix that hydrates a bounded prefix has to resolve
    the RIGHT prefix rather than an arbitrary one.
    """
    seeded: list[str] = []
    for index in range(_BROWSE_CORPUS):
        rel = f"{kb_dirname()}/Notes/Integration/integration-browse-{index:03d}.md"
        _seed_kb(
            vault,
            rel,
            f"Integration Browse {index:03d}",
            project="project-alpha",
            updated=f"2026-{1 + index // 28:02d}-{1 + index % 28:02d}",
        )
        seeded.append(rel)
    return seeded


def test_the_empty_query_browse_reads_only_the_pages_it_can_return(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The contract's second and third verbs, which no other instrument sees.

    "No stage of a governed recall SHALL enumerate, READ OR PARSE every page of
    the vault or of the knowledge-base scope on the reader thread." Moving path
    discovery off `_walk_md` and onto a catalogue query answers the first verb
    and leaves the other two exactly where they were: the hydration loop still
    called `_resolve_page` for every path the index returned, and every one of
    those calls reads the file bytes whether the page cache hits or misses.
    Measured here at 37 of 40 pages for a `limit=5` browse, which is the same
    corpus read the walk performed, in a request that reported zero
    enumerations and `keyword: index`.

    The bound is the request's own limit, not a tuned number: a browse that can
    return at most `limit` pages has no business reading more than `limit` of
    them. Pages rejected before hydration (navigation basenames, non-recall
    candidates) cost no read at all, so the honest ceiling is exactly `limit`.
    """
    _seed_browse_corpus(vault)
    _warm(vault, warm_managed_cell)

    sentinel = _PageReadSentinel(*_scope_roots(vault))
    sentinel.install(monkeypatch)
    sentinel.reset()
    result = _recall(vault, query="", limit=_BROWSE_LIMIT)

    assert len(result["hits"]) == _BROWSE_LIMIT, (
        "the premise failed: the browse must fill its limit for the bound to mean anything"
    )
    assert sentinel.count <= _BROWSE_LIMIT, (
        f"the browse read {sentinel.count} of {_BROWSE_CORPUS} pages on the reader "
        f"thread to return {_BROWSE_LIMIT}: {sentinel.report()}"
    )


#: The browse shapes the identity sweep runs. `after_a_governed_write` is the
#: pending-custody case: between a durable commit and the catalogue's
#: republication the reader's ordering key is the in-flight page's, not the
#: catalogue row's, which is the one state the bound stands down for.
_BROWSE_SHAPES: dict[str, dict[str, Any]] = {
    "kb": {"scope": "kb"},
    "kb_only": {"scope": "kb-only"},
    "vault": {"scope": "vault"},
    "widened": {"widen_outside_kb": True},
    "widened_vault": {"scope": "vault", "widen_outside_kb": True},
    "limit_3": {"limit": 3},
    "limit_over_corpus": {"limit": _BROWSE_CORPUS + 10},
}


def _browse_answers(vault: Path, *, after_write: bool) -> dict[str, list[str]]:
    if after_write:
        _govern(
            vault,
            f"{kb_dirname()}/Notes/Integration/integration-browse-000.md",
            "Integration Browse 000",
            project="project-beta",
        )
    return {
        name: _paths(_recall(vault, **{"query": "", "limit": _BROWSE_LIMIT, **kwargs}))
        for name, kwargs in sorted(_BROWSE_SHAPES.items())
    }


@pytest.mark.parametrize("after_write", [False, True], ids=["warm", "after_a_governed_write"])
def test_the_bounded_browse_returns_the_same_pages_the_unbounded_ones_do(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch, after_write: bool
) -> None:
    """Bounding hydration must change the browse's COST, never its answer.

    Two independent oracles, both reached by withdrawing a seam rather than by
    relaxing a threshold, because a test that patches the number it asserts on
    proves nothing about that number:

    * the WALK — `_index_resolved_scope_rows` withdrawn, so control reaches
      `_walk_md` and the reader hydrates and sorts the whole scope. This is the
      behaviour that shipped before any of this change, so it is the reference
      the contract's "cost, not answer" ruling is actually about.
    * the UNBOUNDED INDEX — `_browse_hydration_limit` withdrawn, so the same
      catalogue-resolved set is hydrated in full. This isolates the bound from
      the index branch beneath it, so a disagreement names which of the two
      moved.

    Paths AND order are compared: a set-equal answer in a different order is
    still a changed answer to a caller that reads the first hit.
    """
    _seed_browse_corpus(vault)
    _seed_outside(vault, "integration-browse-outside.md")
    _warm(vault, warm_managed_cell)

    bounded = _browse_answers(vault, after_write=after_write)

    monkeypatch.setattr(find_module, "_browse_hydration_limit", lambda *a, **k: None)
    find_module.reset_page_and_result_caches()
    unbounded = _browse_answers(vault, after_write=False)

    monkeypatch.setattr(find_module, "_index_resolved_scope_rows", lambda *a, **k: None)
    find_module.reset_page_and_result_caches()
    walked = _browse_answers(vault, after_write=False)

    disagreeing = {
        name: {"bounded": bounded[name], "unbounded": unbounded[name], "walked": walked[name]}
        for name in _BROWSE_SHAPES
        if not bounded[name] == unbounded[name] == walked[name]
    }
    assert not disagreeing, (
        "bounding hydration changed the answer, not only the cost: " f"{disagreeing}"
    )


def test_the_bound_stands_down_for_the_two_states_that_invalidate_the_key() -> None:
    """The seam's own contract, asserted where a mutant can reach it.

    Both conditions are decided from the catalogue alone, without reading a
    page, and neither has a behavioural node of its own: the per-page key
    verification in the hydration loop catches the same two cases a moment
    later and repairs the answer, so removing either clause costs cost and not
    correctness. Defence in depth is worth having and is not the same thing as
    a pinned guard, so the guard is pinned here directly.
    """
    plain = [("a.md", "2026-01-01", None), ("b.md", "2026-01-02", None)]
    frames = [*plain, ("v.frames/scene.jpg.md", "2026-03-01", "v.md")]

    class _Pending:
        def __init__(self, empty: bool) -> None:
            self.empty = empty

    assert find_module._browse_hydration_limit(plain, limit=5, pending=None) == 5
    assert find_module._browse_hydration_limit(plain, limit=5, pending=_Pending(True)) == 5
    assert find_module._browse_hydration_limit(frames, limit=5, pending=None) is None
    assert (
        find_module._browse_hydration_limit(plain, limit=5, pending=_Pending(False)) is None
    )


def test_a_catalogue_whose_ordering_key_is_wrong_costs_cost_and_not_the_answer(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The braces behind the belt: the key is verified per page, not trusted.

    The bound works by trusting that a candidate ranked below the `limit`-th
    accepted hit cannot enter the answer, which is only true while the
    catalogue's `updated` IS the page's. The freshness gate is what makes that
    so, and a gate that admitted a stale generation would otherwise turn a cost
    saving into a silently different answer — the one outcome the ruling on
    this fix forbids. Here the ordering key is deliberately corrupted to the
    worst case, every row reversed, and the answer must not move.
    """
    _seed_browse_corpus(vault)
    _warm(vault, warm_managed_cell)

    honest = _paths(_recall(vault, query="", limit=_BROWSE_LIMIT))

    real = find_module._index_resolved_scope_rows

    def _lying_rows(*args: Any, **kwargs: Any):
        rows = real(*args, **kwargs)
        if rows is None:
            return None
        # Every date replaced by its reflection about 2026, so the catalogue's
        # order is the exact reverse of the page's.
        return [
            (rel, f"{2026:04d}-{13 - int(updated[5:7]):02d}-{updated[8:]}", parent)
            for rel, updated, parent in rows
        ]

    monkeypatch.setattr(find_module, "_index_resolved_scope_rows", _lying_rows)
    find_module.reset_page_and_result_caches()
    under_a_lie = _paths(_recall(vault, query="", limit=_BROWSE_LIMIT))

    assert honest, "the premise failed: the browse returned nothing to compare"
    assert under_a_lie == honest, (
        "a wrong catalogue ordering key changed the browse's answer instead of only "
        f"its cost: honest={honest} lied={under_a_lie}"
    )


def test_a_catalogue_that_cannot_answer_the_browse_declines_instead_of_walking(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch, walk_sentinel
) -> None:
    """The spec's own remedy, on the branch that used to swallow it.

    "A stage that cannot be answered from an index SHALL return the typed
    warming outcome." The helper used to catch `RetrievalIndexWarming`, return
    None, and let control fall through to `_walk_md` — so the one signal the
    contract asks for was converted into the one behaviour it forbids, on a
    managed cell, silently. The readiness gate two branches up has already
    proved the catalogue current for this generation, so a query that then
    declines is a race, not a licence.
    """
    from exomem import lexstore

    _seed_browse_corpus(vault)
    _warm(vault, warm_managed_cell)

    monkeypatch.setattr(
        lexstore,
        "search_eligible_parent_rows_result",
        lambda *a, **k: lexstore.CatalogQueryResult(
            None, lexstore.CatalogReadiness("stale", False, "sqlite")
        ),
    )
    find_module.reset_page_and_result_caches()

    sentinel = walk_sentinel(*_scope_roots(vault), current_thread_only=True)
    sentinel.reset()
    with pytest.raises(find_module.RetrievalIndexWarming):
        _recall(vault, query="", limit=_BROWSE_LIMIT)

    assert sentinel.count == 0, sentinel.report()


def test_a_source_declared_outside_its_span_is_refused_not_discarded() -> None:
    """A write nothing will read must not look like a declaration.

    `mark_source` is consumed by the exit of a span of the same name on the
    same thread. Called with no such span open it used to accumulate an entry
    nobody would ever read, and the stage then reported the static default it
    was opened with — as if that default had been declared. That is the one
    failure direction this vocabulary exists to close, and `_find_semantic`'s
    keyword fallback did exactly it: `collect_candidates` had already closed
    the `keyword` span, so every source that hydration declared was discarded.
    """
    from exomem import find_types

    timings = find_types.FindTimings()

    with pytest.raises(RuntimeError, match="no open span"):
        timings.mark_source("keyword", find_types.SOURCE_COMPUTED)

    with timings.span("keyword", source=find_types.SOURCE_INDEX):
        timings.mark_source("keyword", find_types.SOURCE_COMPUTED)
    assert timings.as_dict()["stages"]["keyword"]["source"] == find_types.SOURCE_COMPUTED, (
        "a source declared from inside its span did not reach the table"
    )


def test_the_index_served_browse_returns_the_same_answer_as_the_walk(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fix must change the COST of the browse, never its answer.

    Both arms run on the same warm managed cell and differ only in whether the
    new index branch is available, so any difference in the hit list is
    attributable to that branch and nothing else. Paths AND order are compared:
    a set-equal answer in a different order is still a changed answer to a
    caller that reads the first hit.
    """
    for index in range(4):
        _seed_kb(
            vault,
            f"{kb_dirname()}/Notes/Integration/integration-identity-{index}.md",
            f"Integration Identity {index}",
            project="project-alpha",
        )
    _warm(vault, warm_managed_cell)

    indexed = _paths(_recall(vault, query="", limit=25))

    # The same request with the index branch withdrawn, so control reaches the
    # walk the managed reader used to take.
    monkeypatch.setattr(find_module, "_index_resolved_scope_rows", lambda *a, **k: None)
    find_module.reset_page_and_result_caches()
    walked = _paths(_recall(vault, query="", limit=25))

    assert indexed == walked, (
        "the index-served browse and the walk disagree; this changes answers, "
        f"not only cost. indexed={indexed} walked={walked}"
    )


def test_every_request_shape_attributes_its_time(
    vault: Path, warm_managed_cell
) -> None:
    """Lane 1's attribution bounds, held across the other lanes' shapes.

    One node over all six shapes rather than six parametrized ones, because the
    useful failure message is the whole table: a single breaching ratio means
    nothing without the others beside it to say whether the instrument grew a
    blind spot or the request simply got small.
    """
    _seed_outside(vault, "integration-attrib.md")
    rel = f"{kb_dirname()}/Notes/Integration/integration-attrib-kb.md"
    _seed_kb(vault, rel, "Integration Attrib", project="project-alpha")
    _warm(vault, warm_managed_cell)

    measured: dict[str, tuple[float, float]] = {}
    for shape, kwargs in sorted(_SHAPES.items()):
        timings = _recall(vault, **kwargs)["timings"]
        measured[shape] = (float(timings["total_ms"]), float(timings["unattributed_ms"]))

    table = ", ".join(
        f"{shape} unattributed={unattributed:.1f}/total={total:.1f}"
        f"={100 * unattributed / total if total else 0:.1f}%"
        f" [{'ratio' if total >= gate.KEYWORD_P50_MS else 'flat'}]"
        for shape, (total, unattributed) in measured.items()
    )
    breaching = sorted(
        shape
        for shape, (total, unattributed) in measured.items()
        if unattributed > _attribution_allowance(total) + 1e-6
    )
    assert not breaching, f"shapes over the attribution bound: {breaching} ({table})"

    # The regime each shape actually reaches, measured rather than asserted.
    # Most of them take the fallback, so the fallback is what this file's bound
    # mostly IS, and a cap that the common case relies on has to be sized
    # against the quantity rather than against the contract's arithmetic at a
    # scale nothing here reaches. This is the assertion that makes 8 ms a cap
    # near the measurement: whatever regime a shape fell into, its absolute
    # unattributed time is inside it.
    over_cap = {
        shape: round(unattributed, 2)
        for shape, (_total, unattributed) in measured.items()
        if unattributed > _FLAT_ATTRIBUTION_ALLOWANCE_MS
    }
    assert not over_cap, (
        f"shapes over the absolute attribution cap of {_FLAT_ATTRIBUTION_ALLOWANCE_MS} ms: "
        f"{over_cap} ({table})"
    )


def test_every_stage_of_every_shape_carries_a_source(
    vault: Path, warm_managed_cell
) -> None:
    """A stage with no source is a stage the walk sentinel cannot read.

    The gate's structural check is `source`, so an unsourced stage is a blind
    spot in the instrument rather than a cosmetic gap.
    """
    _seed_outside(vault, "integration-source-sweep.md")
    rel = f"{kb_dirname()}/Notes/Integration/integration-source-sweep-kb.md"
    _seed_kb(vault, rel, "Integration Source Sweep", project="project-alpha")
    _warm(vault, warm_managed_cell)

    missing: dict[str, list[str]] = {}
    for shape, kwargs in sorted(_SHAPES.items()):
        stages = _recall(vault, **kwargs)["timings"]["stages"]
        absent = sorted(name for name, stage in stages.items() if "source" not in stage)
        if absent:
            missing[shape] = absent

    assert not missing, f"stages with no source: {missing}"


def test_the_attribution_allowance_falls_back_only_below_the_contract_scale() -> None:
    """The fallback must not become the rule.

    At or above the contract's smallest stated ceiling the ratio applies
    unchanged; below it, and only below it, an absolute cap sized against what
    unattributed time actually costs here. A mutant that returns the flat
    allowance everywhere would let a 600 ms request hide 90 ms, and a mutant
    that widens the cap back towards the contract's own arithmetic at that
    scale would let a 65% latency increase on the smallest shape pass in
    silence.

    The numbers are literals, not read back from the module. A test that
    asserts a constant against itself blesses whatever the constant becomes,
    and this lane's own history contains a backstop that monkeypatched the
    threshold under test and let the mutant live.
    """
    assert _attribution_allowance(1000.0) == pytest.approx(150.0)
    assert _attribution_allowance(gate.KEYWORD_P50_MS) == pytest.approx(18.0)
    assert _attribution_allowance(gate.KEYWORD_P50_MS - 0.01) == pytest.approx(8.0)
    assert _attribution_allowance(10.0) == pytest.approx(8.0)


def test_the_whitespace_nonce_defeats_the_cache_without_changing_the_answer(
    vault: Path, warm_managed_cell
) -> None:
    """The gate's browse and keyword series rest on this, so it is pinned here.

    Neither shape can spell its nonce in words. The browse IS `query.strip() ==
    ""` — the test `find.py` routes on — and a keyword series has to match the
    corpus, which a nonce token cannot do. Both carry the nonce in whitespace
    instead, which works only because the two halves are keyed differently:
    `find.py`'s `request_key` holds the RAW query and the read path branches
    and matches on `query.lower().strip()`.

    That is a claim about the product, not about the gate, so it is measured
    against the product: without the nonce the second sample is served from
    the result cache, which is not the read path the ceilings are about; with
    it every sample misses, and the hits are identical either way.
    """
    _seed_browse_corpus(vault)
    _warm(vault, warm_managed_cell)

    nonce = gate.run_nonce()
    for shape, mode, base in (("browse", "hybrid", ""), ("keyword", "keyword", TOKEN)):
        find_module.reset_page_and_result_caches()
        repeated = [_recall(vault, query=base, mode=mode) for _ in range(3)]
        find_module.reset_page_and_result_caches()
        varied = [
            _recall(
                vault,
                query=base + gate._whitespace_nonce(nonce, index),
                mode=mode,
            )
            for index in range(3)
        ]

        served = [bool(r["timings"].get("cache", {}).get("hit")) for r in repeated]
        assert served == [False, True, True], (
            f"{shape}: the premise failed — a repeated query must be cache-served: {served}"
        )
        fresh = [bool(r["timings"].get("cache", {}).get("hit")) for r in varied]
        assert fresh == [False, False, False], (
            f"{shape}: the whitespace nonce did not defeat the result cache: {fresh}"
        )
        assert [_paths(r) for r in varied] == [_paths(repeated[0])] * 3, (
            f"{shape}: the whitespace nonce changed the answer, not only the cache key"
        )
        if shape == "browse":
            assert all(r["hits"] for r in varied), (
                "the premise failed: the browse returned nothing to compare"
            )


def test_the_gate_and_the_integration_suite_watch_the_same_stages() -> None:
    """One definition of a walker stage, not two that drift apart.

    The gate reads stage sources off the live cell and this suite reads them
    off a fixture. If the two lists diverge, one of them stops watching a stage
    and says nothing about it, which is the failure mode the source vocabulary
    exists to prevent.
    """
    assert set(gate.WALKER_STAGES) == set(WALKER_STAGES)
