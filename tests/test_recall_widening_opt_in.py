"""Out-of-KB widening is opt-in, index-backed, and never walks the corpus.

Lane 4 of ``accelerate-governed-recall``. ``outside_kb`` measured 7.6 s on the
live cell on 2026-09-02, on every default ``scope="kb"`` recall, because the
reserve resolved eligibility again with vault scope and then ranked the whole
non-KB corpus — rebuilding a Python BM25 corpus whenever the maintained
catalogue was not fresh enough to serve it.

Three separate contracts, which fail independently:

* **opt-in** — a default ``scope="kb"`` recall serves the knowledge base only,
  and says in the diagnostics that widening was skipped rather than silently
  omitting the stage;
* **index-backed** — a requested widening is one catalogue query over the
  out-of-KB eligible set resolved through ``plan_index_eligibility`` at vault
  scope, reserving at most ``limit - 1`` slots, reporting ``index``;
* **declines rather than scans** — a catalogue that cannot serve the query
  reports ``declined`` and returns the KB results unchanged. No exception, no
  Python corpus build, no walk.

Every assertion about the managed contract is taken against a warm managed cell
(registry seeded, catalogue published, admission ready), because the offline
reader keeps its corpus fallback by design — the last test pins that it does.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from exomem import bm25, commands, find_types, freshness, lexstore
from exomem import find as find_module
from exomem.kbdir import kb_dirname, kb_prefix

pytestmark = pytest.mark.skipif(
    not lexstore.fts5_available(), reason="SQLite build lacks FTS5"
)

#: One rare token, so BM25's IDF stays positive on the small fixture corpus and
#: every hit below is a literal match rather than a ranking accident.
TOKEN = "zzwidentokenzz"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _scope_roots(vault: Path) -> tuple[Path, ...]:
    """The directories the read-path contract forbids enumerating (Lane 1).

    The vault root subsumes the KB scope, but the widening lane reaches sibling
    trees, so both are named.
    """
    return (vault, vault / kb_dirname())


def _seed(vault: Path, *, outside: int = 2, inside: int = 3) -> dict[str, list[str]]:
    """Pages carrying ``TOKEN`` inside and outside the knowledge base.

    Written before the cell is warmed, so the maintained catalogue holds them.
    """
    kb = vault / kb_dirname() / "Notes" / "Insights"
    kb.mkdir(parents=True, exist_ok=True)
    inside_paths: list[str] = []
    for index in range(inside):
        name = f"widen-kb-{index}.md"
        (kb / name).write_text(
            "---\ntype: insight\nstatus: active\nupdated: 2026-07-0"
            f"{index + 1}\nproject: project-alpha\n---\n\n"
            f"# Widen KB {index}\n\n{TOKEN} inside the knowledge base.\n",
            encoding="utf-8",
        )
        inside_paths.append(f"{kb_prefix()}Notes/Insights/{name}")

    reference = vault / "Reference"
    reference.mkdir(parents=True, exist_ok=True)
    outside_paths: list[str] = []
    for index in range(outside):
        name = f"widen-out-{index}.md"
        (reference / name).write_text(
            f"# Widen Out {index}\n\n{TOKEN} beyond the knowledge base.\n",
            encoding="utf-8",
        )
        outside_paths.append(f"Reference/{name}")

    find_module.clear_cache()
    bm25.clear_cache()
    return {"inside": inside_paths, "outside": outside_paths}


def _recall(vault: Path, **kwargs: Any) -> dict:
    """One real timed recall through the public leaf, default ``scope="kb"``."""
    params: dict[str, Any] = {
        "query": TOKEN,
        "mode": "hybrid",
        "graph": False,
        "include_timings": True,
    }
    params.update(kwargs)
    return commands.op_find(vault, **params)


def _paths(result: dict) -> list[str]:
    return [hit["path"] for hit in result["hits"]]


def _outside_paths(result: dict) -> list[str]:
    return [hit["path"] for hit in result["hits"] if hit.get("outside_kb")]


def _kb_paths(result: dict) -> list[str]:
    return [hit["path"] for hit in result["hits"] if not hit.get("outside_kb")]


def _stage(result: dict) -> dict:
    stages = result["timings"]["stages"]
    assert "outside_kb" in stages, (
        "the widening stage reported nothing at all; a stage that did not run "
        f"must still say so. stages={sorted(stages)}"
    )
    return stages["outside_kb"]


class _Tripwire:
    """Records every call to a scan primitive the widening must not reach.

    Recording rather than raising: ``_find_outside_kb`` swallows every
    exception by design ("widening must never break find"), so a fall-through
    to a corpus build would otherwise look like a clean decline. The real
    implementation still runs, because these primitives are shared — the
    knowledge-base lexical lane legitimately builds its own corpus on a
    rolled-back sidecar, and withdrawing it outright would change the KB
    ranking this file compares against.
    """

    def __init__(self, name: str, real: Any, *, when: Any = None) -> None:
        self.name = name
        self._real = real
        self._when = when
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any):
        if self._when is None or self._when(*args, **kwargs):
            self.calls += 1
        return self._real(*args, **kwargs)


def _withdraw_corpus_builds(monkeypatch: pytest.MonkeyPatch) -> list[_Tripwire]:
    """Watch every way the widening could answer by scanning the corpus.

    Scoped to the vault, which is the widening's own scope: the knowledge-base
    lanes query the same primitive at ``scope="kb"`` and are not this file's
    subject.

    `_outside_kb_keyword_paths` is deliberately NOT watched. It has no
    production caller at this revision or its base — `grep` finds only tests
    patching it — so a tripwire on it asserts nothing and reads as coverage it
    does not provide. `bm25.search` is the reachable corpus build, and the
    decline node adds the scan oracle.
    """
    tripwires = [
        _Tripwire(
            "bm25.search(scope='vault') (in-process corpus build)",
            bm25.search,
            when=lambda *_a, **kw: kw.get("scope") == "vault",
        ),
    ]
    monkeypatch.setattr(bm25, "search", tripwires[0])
    return tripwires


def _warm_catalogue_for_an_offline_reader(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A published catalogue, served to a reader that is NOT runtime-managed.

    `warm_managed_cell` without its last two lines. This is the configuration a
    CLI user has after any command that publishes the catalogue, and the one
    the offline rung of the reserve actually runs in — `runtime_managed()` is
    False, so the managed contract does not apply, but
    `maintained_content_index_enabled()` is True, so the catalogue query is
    still the rung that serves.
    """
    from exomem import embeddings, file_watcher, memory_refs, readiness

    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setattr(embeddings, "ranking_enabled", lambda: True)
    monkeypatch.setattr(readiness, "should_defer", lambda _component: False)
    file_watcher.FileWatcher(vault)._reconcile_once(seed=True)
    assert lexstore.get_store(vault).rebuild_atomic() is True
    assert lexstore.runtime_retrieval_catalog_proof(vault, schedule_repair=False) is not None
    memory_refs.ReferenceIndex(vault).rebuild_all()
    assert readiness.runtime_managed() is False, "this helper must leave the reader offline"


def _age_the_vault_catalogue(vault: Path) -> None:
    """Move the non-KB corpus without republishing the catalogue.

    The drift page is written OUTSIDE the knowledge base on purpose: it ages
    the vault scope the widening queries while leaving the KB scope the rest of
    the request reads live, so the assertion is about the widening alone.
    """
    reference = vault / "Reference"
    reference.mkdir(parents=True, exist_ok=True)
    (reference / f"drift-{uuid.uuid4().hex[:8]}.md").write_text(
        "# Drift\n\nDrifted body, never published to the catalogue.\n",
        encoding="utf-8",
    )
    # `rebaseline` alone, deliberately: `find.clear_cache` also clears the
    # freshness registry, and a cleared registry makes the NEXT recall rebuild
    # its recall projection from a cold stat walk — a walk this file would
    # then be measuring instead of the widening's.
    freshness.rebaseline(vault)


# --------------------------------------------------------------------------- #
# Opt-in
# --------------------------------------------------------------------------- #


def test_default_kb_recall_runs_no_widening_and_reports_it_skipped(
    vault: Path, warm_managed_cell
) -> None:
    """`scope="kb"` serves the knowledge base, and says the reserve was skipped.

    The behaviour change this lane exists to make: the reserve used to run on
    every default recall, so every caller paid for a lane most of them never
    read. Silence would be indistinguishable from a stage that ran and found
    nothing, so the diagnostics have to carry the skip.
    """
    seeded = _seed(vault)
    warm_managed_cell(vault)

    result = _recall(vault)

    assert _outside_paths(result) == [], (
        "a default `scope=\"kb\"` recall widened outside the knowledge base"
    )
    for path in seeded["outside"]:
        assert path not in _paths(result)
    assert _kb_paths(result), "the knowledge-base lane returned nothing to compare"

    stage = _stage(result)
    assert stage.get("skipped") is True, stage
    assert stage["source"] == "declined", stage
    assert "ms" not in stage, f"a skipped stage reported a duration: {stage}"


# --------------------------------------------------------------------------- #
# Index-backed
# --------------------------------------------------------------------------- #


def test_requested_widening_serves_from_the_catalogue_with_source_index(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A requested widening comes from the catalogue, and says so.

    Non-vacuous by construction: every corpus-scanning primitive is withdrawn
    first, so the out-of-KB hits below can only have come from the maintained
    catalogue over the index-resolved eligible set.
    """
    seeded = _seed(vault)
    warm_managed_cell(vault)
    tripwires = _withdraw_corpus_builds(monkeypatch)

    result = _recall(vault, widen_outside_kb=True)

    outside = _outside_paths(result)
    assert outside, "the requested widening surfaced nothing outside the knowledge base"
    assert set(outside) <= set(seeded["outside"]), outside
    assert all(not path.startswith(kb_prefix()) for path in outside), outside
    assert all(wire.calls == 0 for wire in tripwires), [
        (wire.name, wire.calls) for wire in tripwires
    ]

    stage = _stage(result)
    assert stage["source"] == "index", stage
    assert "skipped" not in stage, stage


def test_offline_catalogue_rung_never_tags_a_kb_page_outside_kb(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unrestricted rung keeps its prefix guard.

    An offline reader on a vault above the inline-repair page cap reaches the
    SAME `search_bm25_result` the managed reserve uses — but with no filter
    plan there is nothing to narrow by, so `allowed_outside` is None and the
    vault-scope query is unrestricted and returns knowledge-base rows. Skipping
    the per-candidate prefix test on "the catalogue served it" rather than on
    "the query was restricted" hands those rows to the reserve, tagged
    `outside_kb=True`, consuming the slot the caller asked for out-of-KB
    material with.
    """
    seeded = _seed(vault, outside=2, inside=8)
    _warm_catalogue_for_an_offline_reader(vault, monkeypatch)
    # Above the foreground-repair page cap: the catalogue rung serves, not the
    # repairing one. `_bounded_lexical_repair_allowed` is the only thing that
    # stands between this fixture corpus and a production-size vault.
    monkeypatch.setattr(find_module, "_bounded_lexical_repair_allowed", lambda _key: False)
    assert lexstore.maintained_content_index_enabled(), "the probe needs the catalogue rung"

    result = _recall(vault, widen_outside_kb=True, limit=3)

    leaked = [path for path in _outside_paths(result) if path.startswith(kb_prefix())]
    assert not leaked, (
        f"knowledge-base pages were tagged outside_kb: {leaked}; "
        f"all outside hits={_outside_paths(result)}"
    )
    assert set(_outside_paths(result)) <= set(seeded["outside"]), _outside_paths(result)


def test_requested_widening_keeps_the_reserve_under_limit_minus_one(
    vault: Path, warm_managed_cell
) -> None:
    """The reserve never starves the knowledge base it is widening from."""
    seeded = _seed(vault, outside=4, inside=4)
    warm_managed_cell(vault)

    for limit in (1, 2, 3, 5):
        result = _recall(vault, widen_outside_kb=True, limit=limit)
        outside = _outside_paths(result)
        assert len(outside) <= max(0, limit - 1), (
            f"limit={limit} reserved {len(outside)} out-of-KB slots: {outside}"
        )
        if limit > 1:
            assert _kb_paths(result), (
                f"limit={limit} left no slot for the knowledge base: {_paths(result)}"
            )

    assert seeded["outside"], "the fixture seeded nothing outside the knowledge base"


def test_requested_widening_declines_when_the_catalogue_is_not_live(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A catalogue behind the corpus declines; it never substitutes a scan.

    The honest third option, exactly as the eligibility stage takes it: an
    answer from a stale index is silently wrong, and a corpus build is the cost
    this lane exists to remove — so the widening is omitted and named.
    """
    _seed(vault)
    warm_managed_cell(vault)
    baseline = _paths(_recall(vault))

    _age_the_vault_catalogue(vault)
    tripwires = _withdraw_corpus_builds(monkeypatch)
    oracle = _Tripwire(
        "find._eligible_filter_paths (full-scan oracle)", find_module._eligible_filter_paths
    )
    monkeypatch.setattr(find_module, "_eligible_filter_paths", oracle)

    result = _recall(vault, widen_outside_kb=True)

    assert _outside_paths(result) == [], _paths(result)
    assert _paths(result) == baseline, (
        "a declined widening changed the knowledge-base results"
    )
    assert all(wire.calls == 0 for wire in [*tripwires, oracle]), [
        (wire.name, wire.calls) for wire in [*tripwires, oracle]
    ]

    stage = _stage(result)
    assert stage["source"] == "declined", stage


def test_requested_widening_declines_when_the_reserve_query_is_not_ready(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The OTHER decline site: the eligible set resolves, the lexical query does not.

    Distinct from the stale-catalogue node above, and not covered by it. There,
    ageing the vault makes `_widening_allowed_paths` decline first, so the
    managed guard on `search_bm25_result`'s readiness is never reached — a
    mutant that replaced that guard with an in-process corpus build passed the
    whole suite. Here the eligible set is answerable and only the lexical query
    reports itself unready, which is what a repair in flight looks like.
    """
    _seed(vault)
    warm_managed_cell(vault)
    baseline = _paths(_recall(vault))

    real = lexstore.search_bm25_result

    def _vault_query_not_ready(vault_root, query, k, *, scope="kb", **kwargs):
        if scope == "vault":
            return lexstore.CatalogQueryResult(
                None, lexstore.CatalogReadiness("warming", False, "fts5")
            )
        return real(vault_root, query, k, scope=scope, **kwargs)

    monkeypatch.setattr(lexstore, "search_bm25_result", _vault_query_not_ready)
    tripwires = _withdraw_corpus_builds(monkeypatch)
    oracle = _Tripwire(
        "find._eligible_filter_paths (full-scan oracle)", find_module._eligible_filter_paths
    )
    monkeypatch.setattr(find_module, "_eligible_filter_paths", oracle)

    result = _recall(vault, widen_outside_kb=True)

    assert _outside_paths(result) == [], _paths(result)
    assert _paths(result) == baseline, (
        "a declined widening changed the knowledge-base results"
    )
    assert all(wire.calls == 0 for wire in [*tripwires, oracle]), [
        (wire.name, wire.calls) for wire in [*tripwires, oracle]
    ]
    stage = _stage(result)
    assert stage["source"] == "declined", stage


def test_kb_ranking_is_identical_with_and_without_widening(
    vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Widening reserves slots; it does not re-rank what the knowledge base said.

    Also the identity guard on the reserve: every widened hit must be a page
    from the index-resolved out-of-KB eligible set. Drop that restriction from
    the reserve query and a knowledge-base page leaks into the reserve, which
    this assertion names.

    MORE matching knowledge-base pages than `limit`, deliberately. The reserve
    already excludes the paths the knowledge-base lane returned, so with a
    corpus the KB lane exhausts, an unrestricted reserve query would find
    nothing left to leak and the guard would pass while doing nothing. The
    surplus pages are the ones a lost restriction surfaces.
    """
    seeded = _seed(vault, outside=2, inside=8)
    warm_managed_cell(vault)
    _withdraw_corpus_builds(monkeypatch)

    plain = _recall(vault, limit=3)
    widened = _recall(vault, widen_outside_kb=True, limit=3)

    plain_kb = _kb_paths(plain)
    widened_kb = _kb_paths(widened)
    assert widened_kb, "widening consumed every knowledge-base slot"
    assert widened_kb == plain_kb[: len(widened_kb)], (
        f"widening re-ranked the knowledge base: {widened_kb} is not a prefix "
        f"of {plain_kb}"
    )

    # Non-emptiness is load-bearing twice over: it keeps the leak assertion
    # below from passing vacuously, and it is what catches a hot-cache key that
    # forgot the new knob — without it the widened call would be served the
    # `plain` list it just cached, and every assertion here would still hold.
    assert _outside_paths(widened), (
        "the widened call returned no out-of-KB hit; either the reserve did "
        "not run or it was served the unwidened cache entry"
    )
    leaked = [path for path in _outside_paths(widened) if path.startswith(kb_prefix())]
    assert not leaked, f"knowledge-base pages leaked into the reserve: {leaked}"
    assert set(_outside_paths(widened)) <= set(seeded["outside"]), _outside_paths(widened)


def test_a_list_filter_still_gates_the_reserve(
    vault: Path, warm_managed_cell
) -> None:
    """A list filter excludes an out-of-KB page from the reserve, end to end.

    What this pins is the OUTCOME, not a particular gate — and the distinction
    was measured, not assumed. `types`/`projects`/`tags`/`speakers`/`file_types`
    are compiled into `filter_plan` through `FilterShortcuts`
    (`find.py:1253`), so setting any of them makes the reserve's eligible set
    exact and the wrong-type page never reaches the lexical query at all. The
    per-candidate `_passes_filters` call in the reserve loop is therefore
    redundant for every shape reachable through the public leaf: deleting it
    leaves this node, the rest of this file, `tests/test_find.py`,
    `tests/test_find_structured_filters.py` and the reviewer's own type-gate
    probe all green (63 passed). An equivalent mutant, on code this lane did
    not add.

    The node still earns its place: it is red under M4 (a reserve query that
    loses `allowed_paths` surfaces the excluded page), which is the mechanism
    that actually enforces the filter now.
    """
    kb = vault / kb_dirname() / "Notes" / "Insights"
    kb.mkdir(parents=True, exist_ok=True)
    (kb / "gate-kb.md").write_text(
        "---\ntype: insight\nstatus: active\nupdated: 2026-07-01\n---\n\n"
        f"# Gate KB\n\n{TOKEN} inside.\n",
        encoding="utf-8",
    )
    reference = vault / "Reference"
    reference.mkdir(parents=True, exist_ok=True)
    (reference / "gate-out-wrong-type.md").write_text(
        "---\ntype: reference\n---\n\n"
        f"# Gate Out\n\n{TOKEN} outside, and the wrong type.\n",
        encoding="utf-8",
    )
    find_module.clear_cache()
    bm25.clear_cache()
    warm_managed_cell(vault)

    result = _recall(vault, widen_outside_kb=True, limit=5, types=["insight"])

    assert "Reference/gate-out-wrong-type.md" not in _paths(result), _paths(result)


def test_an_offline_lexical_failure_degrades_rather_than_declines(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`declined` is the managed reader's word, and it is not a synonym for broken.

    A managed reader that says `declined` asked an index and the index said
    "not for this generation". An offline reader whose lexical rung threw
    DEGRADED: it says so through `failed_out` and the degradation counter, and
    reporting that as `declined` would present a lane that fell over as a lane
    that stood down. The catch-all is shared by both, so the distinction has to
    be made there.
    """
    _seed(vault)
    _warm_catalogue_for_an_offline_reader(vault, monkeypatch)
    monkeypatch.setattr(find_module, "_bounded_lexical_repair_allowed", lambda _key: False)

    real = lexstore.search_bm25_result

    def _vault_query_explodes(vault_root, query, k, *, scope="kb", **kwargs):
        if scope == "vault":
            raise RuntimeError("sidecar handle lost mid-query")
        return real(vault_root, query, k, scope=scope, **kwargs)

    monkeypatch.setattr(lexstore, "search_bm25_result", _vault_query_explodes)

    timings = find_types.FindTimings()
    failed: list[str] = []
    hits = find_module.find(
        vault,
        query=TOKEN,
        mode="hybrid",
        graph=False,
        widen_outside_kb=True,
        timings=timings,
        failed_out=failed,
    )

    assert hits, "a broken widening must not empty the knowledge-base result"
    assert failed == ["outside_kb_lexical"], failed
    stage = timings.as_dict()["stages"]["outside_kb"]
    assert stage["source"] != "declined", (
        f"an offline lexical failure was reported as a decline: {stage}"
    )
    assert stage["source"] == find_types.SOURCE_COMPUTED, stage


def test_the_unit_cache_key_omits_the_knob_because_the_unit_path_returns_first() -> None:
    """A pin on WHY only one of the two cache keys carries `widen_outside_kb`.

    `request_key` must carry it: the page path runs the widening block after
    the cache lookup, so a key without it serves a widened request the
    unwidened list it just cached (mutant M6). `unit_request_key` must not need
    it: the unit-level path returns from `find()` before the widening block is
    reached, so the knob cannot affect what it caches.

    That is a claim about control flow, and control flow moves. Asserting the
    absence pins the reasoning: move the widening block above the unit return,
    or make the unit path reach it, and this fails and asks for the key to be
    updated rather than silently caching across the knob.
    """
    import inspect

    source = inspect.getsource(find_module.find)
    unit_return, widening = source.index("return unit_hits"), source.index(
        'if scope == "kb" and query_norm and widen_outside_kb:'
    )
    assert unit_return < widening, (
        "the unit-level path no longer returns before the widening block; "
        "`unit_request_key` must now carry `widen_outside_kb` too"
    )
    unit_key_block = source[source.index("unit_request_key = ("):source.index("unit_cache_key = (")]
    assert "widen_outside_kb" not in unit_key_block, (
        "unit_request_key gained the knob; if that was deliberate this pin and "
        "its docstring are what need updating"
    )


# --------------------------------------------------------------------------- #
# Never walks
# --------------------------------------------------------------------------- #


@pytest.mark.xfail(
    strict=True,
    reason="lane 3 micro-round: refs sidecar declines for out-of-KB paths under a managed runtime",
)
def test_widening_never_walks_on_a_warm_managed_cell(
    vault: Path, warm_managed_cell, walk_sentinel
) -> None:
    """Every widening shape a managed reader can ask for enumerates nothing.

    Three shapes, because they reach the widening lane differently: the strict
    opt-out, the new default, and the requested reserve.

    XFAIL-STRICT, on the third shape only, and honestly so. `kb-only` and the
    new default already report zero. `kb + widen` reports ONE enumeration of
    the knowledge-base scope, and it is not the widening's: a widened recall
    returns out-of-KB paths, `ReferenceIndex.rebuild_all()` covers the
    knowledge base and not those paths, so `memory_refs.refs_for_paths`
    (reached from `commands.op_find`) finds no row and rebuilds inline from a
    corpus scan. An earlier revision of this node paid that enumeration before
    installing the counter, which measured the widening lane in isolation and
    also hid a real, reachable walk behind a fixture. It does not any more.

    This is the same cold-refs walk Lane 1 pinned by name as Lane 3's contract
    (`test_recall_walk_sentinel.py::test_cold_refs_sidecar_declines_instead_of_walking`),
    widened in scope by this lane: opt-in widening enlarges the set of paths
    that reach a cold sidecar. Strict, so it flips the moment Lane 3's decline
    lands and fails loudly if it turns green for any other reason.

    A stale catalogue is deliberately NOT a fourth shape here. This counter is
    process-wide by construction (Lane 1: `os.scandir`/`os.listdir`, any
    thread), and a stale catalogue is exactly the state that wakes the
    single-flight repair worker — whose whole job is to walk, off the reader
    thread. Measuring it here would assert that the repair does not run. The
    decline cases prove what this lane owes instead, and prove it precisely:
    the reader-thread primitives that could substitute a scan are watched by
    name in the two decline nodes.
    """
    _seed(vault)
    warm_managed_cell(vault)
    sentinel = walk_sentinel(*_scope_roots(vault))

    for label, kwargs in (
        ("kb-only", {"scope": "kb-only"}),
        ("kb default", {}),
        ("kb + widen", {"widen_outside_kb": True}),
    ):
        sentinel.reset()
        _recall(vault, **kwargs)
        assert sentinel.count == 0, f"{label}: {sentinel.report()}"


def test_offline_reader_keeps_its_walk_fallback(
    vault: Path, walk_sentinel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PIN: an unmanaged caller still answers, by scanning, when asked to widen.

    The no-walk contract governs the managed reader. A CLI user against a cold
    vault with the sidecar rolled back to the in-process rung has no catalogue
    to consult, and must still get the out-of-KB page rather than a decline.
    """
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    monkeypatch.setattr(find_module, "_bounded_lexical_repair_allowed", lambda _key: False)
    seeded = _seed(vault)
    sentinel = walk_sentinel(*_scope_roots(vault))

    sentinel.reset()
    result = _recall(vault, widen_outside_kb=True)

    assert set(_outside_paths(result)) & set(seeded["outside"]), _paths(result)
    assert sentinel.count > 0, (
        "the offline fallback answered without scanning; this pin is vacuous"
    )
