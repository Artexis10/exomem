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

from pathlib import Path
from typing import Any

import pytest

from exomem import bm25, commands, semantic_writes
from exomem import find as find_module
from exomem.vault import kb_dirname

TOKEN = "integrationprobe"

#: The stages the contract requires to consume maintained indexes. Kept in step
#: with `scripts/recall_latency_gate.py::WALKER_STAGES` by
#: `test_the_gate_and_the_integration_suite_watch_the_same_stages`.
WALKER_STAGES: tuple[str, ...] = (
    "filter_eligibility",
    "outside_kb",
    "recall_projection",
    "pending_visibility",
)


def _scope_roots(vault: Path) -> tuple[Path, ...]:
    """Every directory the contract forbids enumerating on the reader thread.

    The vault root subsumes the KB scope, but the widening lane reaches sibling
    trees (`Reference/`), so both are named.
    """
    return (vault, vault / kb_dirname())


def _page(title: str, *, project: str) -> str:
    return (
        "---\n"
        f"title: {title}\n"
        "type: note\n"
        "status: active\n"
        "updated: 2026-08-20\n"
        f"projects: [{project}]\n"
        "tags: [integration]\n"
        "---\n\n"
        f"# {title}\n\n"
        "## Observations\n\n"
        f"- [config] {TOKEN} #integration (test) ^{title.lower().replace(' ', '-')}\n"
    )


def _seed_kb(vault: Path, rel: str, title: str, *, project: str) -> Path:
    """Put a page on disk BEFORE the cell is warmed, so the catalogue holds it."""
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_page(title, project=project), encoding="utf-8")
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
    # THREAD". Lane 3's custody schedules a background lexical repair after a
    # governed write, and that repair legitimately walks — measured at 86 scope
    # enumerations here. Counting it would fail this node for doing exactly
    # what the design says to do, and worse, would push a later author to
    # weaken the assertion rather than read the contract. The no-write sweep in
    # `test_no_request_shape_walks_the_corpus` stays all-thread, so the
    # difference between the two is itself pinned: zero everywhere before a
    # write, zero on the reader thread after one.
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

    # Reader thread only, for the reason given on the node above: the governed
    # write's background repair walks by design, and the contract governs the
    # reader.
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
        for shape, (total, unattributed) in measured.items()
    )
    breaching = sorted(
        shape
        for shape, (total, unattributed) in measured.items()
        if unattributed > 0.15 * total + 1e-6
    )
    assert not breaching, f"shapes over the 15% attribution bound: {breaching} ({table})"


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


def test_the_gate_and_the_integration_suite_watch_the_same_stages() -> None:
    """One definition of a walker stage, not two that drift apart.

    The gate reads stage sources off the live cell and this suite reads them
    off a fixture. If the two lists diverge, one of them stops watching a stage
    and says nothing about it, which is the failure mode the source vocabulary
    exists to prevent.
    """
    import sys

    scripts = Path(__file__).resolve().parents[1] / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import recall_latency_gate as gate

    assert set(gate.WALKER_STAGES) == set(WALKER_STAGES)
