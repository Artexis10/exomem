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

from exomem import bm25, commands, freshness, lexstore
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
    lanes query the same primitives at ``scope="kb"`` and are not this file's
    subject.
    """
    tripwires = [
        _Tripwire(
            "bm25.search(scope='vault') (in-process corpus build)",
            bm25.search,
            when=lambda *_a, **kw: kw.get("scope") == "vault",
        ),
        _Tripwire(
            "find._outside_kb_keyword_paths (vault walk)",
            find_module._outside_kb_keyword_paths,
        ),
    ]
    monkeypatch.setattr(bm25, "search", tripwires[0])
    monkeypatch.setattr(find_module, "_outside_kb_keyword_paths", tripwires[1])
    return tripwires


def _warm_reference_sidecar(vault: Path, paths: list[str]) -> None:
    """Build the reference rows for pages only a widened recall reaches.

    ``warm_managed_cell`` rebuilds the reference sidecar, but that rebuild
    covers the knowledge base; the first recall to return an out-of-KB page
    finds no row for it and ``refs_for_paths`` rebuilds inline from a corpus
    scan. That cold-sidecar walk is already pinned, by name, as Lane 3's
    contract (``test_recall_walk_sentinel.py::
    test_cold_refs_sidecar_declines_instead_of_walking``, xfail-strict), and
    it is not what this file measures — so it is paid here, before the
    sentinel is installed, and the assertions below are about the widening
    lane alone.
    """
    from exomem import memory_refs

    memory_refs.ReferenceIndex(vault).refs_for_paths(list(paths))


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


# --------------------------------------------------------------------------- #
# Never walks
# --------------------------------------------------------------------------- #


def test_widening_never_walks_on_a_warm_managed_cell(
    vault: Path, warm_managed_cell, walk_sentinel
) -> None:
    """Every widening shape a managed reader can ask for enumerates nothing.

    Three shapes, because they reach the widening lane differently: the strict
    opt-out, the new default, and the requested reserve.

    A stale catalogue is deliberately NOT a fourth shape here. This counter is
    process-wide by construction (Lane 1: `os.scandir`/`os.listdir`, any
    thread), and a stale catalogue is exactly the state that wakes the
    single-flight repair worker — whose whole job is to walk, off the reader
    thread. Measuring it here would assert that the repair does not run. The
    decline case proves what this lane owes instead, and proves it precisely:
    the reader-thread primitives that could substitute a scan are watched by
    name in `test_requested_widening_declines_when_the_catalogue_is_not_live`.
    """
    seeded = _seed(vault)
    warm_managed_cell(vault)
    _warm_reference_sidecar(vault, seeded["outside"])
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
