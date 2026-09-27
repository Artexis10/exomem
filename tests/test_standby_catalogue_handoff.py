"""A standby carries this release's lexical catalogue across a schema bump.

A release that bumps `lexstore.SCHEMA_VERSION` used to make every managed
upgrade a cold start: the serving worker keeps its older catalogue current for
its own code, a standby never publishes one, so the candidate's `lexical`
cutover component waited out the 300 s warm budget and the supervisor fell
back to a drain-and-cold-start. The standby now builds this release's catalogue
into a rebuild temp it holds for its whole life, promotion adopts that temp,
and the promoted worker heals whatever the serving worker wrote meanwhile with
one bounded delta.

Hosted cells never run a standby, so the cold path that rebuilds a
schema-bumped catalogue at boot is pinned here too.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import find as find_module
from exomem import freshness, graph_sync, lexstore, readiness, service_standby, warmup
from exomem import vault as vault_module

MARKER = "quokkacatalogueseed"
NOTES = {
    f"Knowledge Base/Notes/Insights/handoff-{index}.md": (
        "---\n"
        "type: insight\n"
        "status: draft\n"
        "created: 2026-09-27\n"
        "updated: 2026-09-27\n"
        "sources: []\n"
        "---\n\n"
        f"# Handoff {index}\n\n"
        f"A catalogue handoff probe {MARKER}{index}.\n"
    )
    for index in range(4)
}


def _seed_live_scopes(root: Path) -> None:
    """What the watcher's boot pass installs before retrieval is verified."""
    kb = root / "Knowledge Base"
    freshness.seed(
        root,
        "kb",
        ((str(path), freshness.stat_signature(path)) for path in find_module._walk_md(kb)),
    )
    freshness.seed(
        root,
        "vault",
        (
            (str(path), freshness.stat_signature(path))
            for path in vault_module.walk_vault_md(root)
        ),
    )


def _older_schema(root: Path) -> None:
    """Make the live catalogue one an earlier release published."""
    connection = sqlite3.connect(lexstore.lexical_path(root))
    try:
        connection.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(lexstore.SCHEMA_VERSION - 1),),
        )
        connection.execute(
            "UPDATE meta SET value = 'an-earlier-release' WHERE key = 'catalog_identity'"
        )
        connection.commit()
    finally:
        connection.close()
    lexstore.clear_stores()


def _temps(root: Path) -> list[Path]:
    live = lexstore.lexical_path(root)
    return sorted(live.parent.glob(f"{live.name}.rebuild-*.tmp*"))


@pytest.fixture
def older_catalogue(vault: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A vault whose live lexical catalogue an earlier release published."""
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "auto")
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    for relative, text in NOTES.items():
        path = vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    freshness.clear()
    lexstore.clear_stores()
    lexstore.ensure_fresh(vault)
    _older_schema(vault)
    service_standby.reset_for_tests()
    readiness.reset()
    try:
        yield vault
    finally:
        lexstore.discard_detached_catalogs()
        lexstore.await_repairs_idle(vault)
        service_standby.reset_for_tests()
        readiness.reset()
        lexstore.clear_stores()
        freshness.clear()


def test_a_detached_catalogue_is_built_beside_the_live_one_and_publishes_nothing(
    older_catalogue: Path,
) -> None:
    root = older_catalogue
    live = lexstore.lexical_path(root)
    before = live.read_bytes()
    assert lexstore.live_catalog_compatible(root) is False

    detached = lexstore.build_detached_catalog(root)

    assert detached is not None
    assert detached.path.exists()
    assert detached.lock.held(), "the builder holds its temp for its whole life"
    assert live.read_bytes() == before, "the live catalogue is the serving worker's"
    assert lexstore.live_catalog_compatible(root) is False

    assert lexstore.adopt_detached_catalog(root, detached) is True
    assert not detached.path.exists()
    assert not detached.lock.held()
    assert _temps(root) == []
    assert lexstore.live_catalog_compatible(root) is True
    assert lexstore.search_bm25(root, f"{MARKER}2", k=3, scope="kb")


def test_adoption_refuses_a_catalogue_whose_identity_moved_after_the_build(
    older_catalogue: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = older_catalogue
    live = lexstore.lexical_path(root)
    before = live.read_bytes()
    detached = lexstore.build_detached_catalog(root)
    assert detached is not None

    monkeypatch.setattr(lexstore, "catalog_semantic_identity", lambda _root: "moved")
    assert lexstore.adopt_detached_catalog(root, detached) is False

    assert live.read_bytes() == before
    assert _temps(root) == []
    assert not detached.lock.held()


def test_the_held_temp_outlives_both_temp_reapers_for_the_warm(
    older_catalogue: Path,
) -> None:
    """Neither reaper may take a standby's catalogue while it still holds it.

    The age reaper of the 0.93 kind (`graph_sync.sweep_abandoned_temporaries`)
    takes lexical temps older than an hour with no lock check; the temp is
    written during the warm, so the whole 300 s warm budget stays under that.
    The lexical orphan sweep takes temps older than ten minutes, but only when
    no builder holds the temp's advisory lock.
    """
    root = older_catalogue
    detached = lexstore.build_detached_catalog(root)
    assert detached is not None
    members = _temps(root)
    assert members

    for member in members:
        stamp = time.time() - 300
        os.utime(member, (stamp, stamp))
    live = lexstore.lexical_path(root)
    reaped = graph_sync.sweep_abandoned_temporaries(root, live, live_paths=set())
    assert not set(reaped) & set(members)
    assert all(member.exists() for member in members)

    for member in members:
        stamp = time.time() - 11 * 60
        os.utime(member, (stamp, stamp))
    assert lexstore.get_store(root)._sweep_orphan_rebuild_temps() == []
    assert all(member.exists() for member in members)

    lexstore.discard_detached_catalog(root, detached)
    assert _temps(root) == []
    assert not detached.lock.held()


def test_a_discard_during_the_build_leaves_no_temp_behind(
    older_catalogue: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A standby stopped mid-build must not leave a whole catalogue behind."""
    root = older_catalogue
    store = lexstore.get_store(root)
    real_walk = type(store)._walk_entries

    def walk_then_discard(self):
        walked = real_walk(self)
        lexstore.discard_detached_catalogs()
        return walked

    monkeypatch.setattr(type(store), "_walk_entries", walk_then_discard)
    assert lexstore.build_detached_catalog(root) is None
    assert _temps(root) == []


def _stub_graph_and_models(monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import epistemic_graph

    monkeypatch.setattr(service_standby.warmup, "model_preload_allowed", lambda *a: False)
    monkeypatch.setattr(service_standby, "snapshot_token", lambda root: "checkpoint-1")
    monkeypatch.setattr(service_standby, "_acquire_ownership", lambda: None)
    monkeypatch.setattr(
        epistemic_graph.EpistemicGraphIndex,
        "adopt_published_snapshot",
        lambda self, **_kwargs: epistemic_graph.SnapshotAdoption(True, reason="adopted"),
        raising=True,
    )


def test_a_discarded_standby_removes_its_detached_catalogue(
    older_catalogue: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = older_catalogue
    _stub_graph_and_models(monkeypatch)
    service_standby.enter_standby()
    service_standby.warm(root)
    assert service_standby.cutover_components()["lexical"] == "ready"
    held = _temps(root)
    assert held

    service_standby.discard()

    assert _temps(root) == []
    assert not lexstore.live_catalog_compatible(root), "a discard publishes nothing"


def test_stopping_an_unpromoted_worker_discards_what_its_standby_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from exomem import server_runtime

    discarded: list[str] = []
    monkeypatch.setattr(service_standby, "discard", lambda: discarded.append("discard"))
    activation = server_runtime.LocalRuntimeActivation(tmp_path, deferred=True)

    async def exercise() -> None:
        async with activation.lifespan()(SimpleNamespace()):
            assert discarded == []

    asyncio.run(exercise())
    assert discarded == ["discard"]


def test_a_promotion_adopts_the_detached_catalogue_and_carries_the_handoff(
    older_catalogue: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = older_catalogue
    _stub_graph_and_models(monkeypatch)
    service_standby.enter_standby()
    service_standby.warm(root)
    assert service_standby.cutover_ready() is True

    record = service_standby.promote(root, migrated=False)

    assert record["lexical_catalogue"] == "adopted"
    assert "catalogue_handoff" in record["carried_from_standby"]
    # The standby warmed no caches over the old catalogue, so the promoted
    # worker's own warm still has to.
    assert "lexical" not in record["carried_from_standby"]
    assert lexstore.live_catalog_compatible(root) is True
    assert _temps(root) == []


def test_a_catalogue_that_cannot_be_adopted_rebuilds_after_promotion(
    older_catalogue: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = older_catalogue
    _stub_graph_and_models(monkeypatch)
    service_standby.enter_standby()
    service_standby.warm(root)
    monkeypatch.setattr(lexstore, "adopt_detached_catalog", lambda *_a: False)

    record = service_standby.promote(root, migrated=False)

    assert record["lexical_catalogue"] == "rebuild-after-promotion"
    assert "catalogue_handoff" not in record["carried_from_standby"]


def _promote_over_older_catalogue(
    root: Path, monkeypatch: pytest.MonkeyPatch, *, between=None
) -> dict:
    _stub_graph_and_models(monkeypatch)
    service_standby.enter_standby()
    service_standby.warm(root)
    if between is not None:
        between(root)
    return service_standby.promote(root, migrated=False)


def _serving_writes(root: Path) -> None:
    """What the worker still serving writes between the build and promotion."""
    changed = root / "Knowledge Base/Notes/Insights/handoff-1.md"
    changed.write_text(
        changed.read_text(encoding="utf-8") + "\nquokkaafterbuild edit.\n", encoding="utf-8"
    )
    (root / "Knowledge Base/Notes/Insights/handoff-new.md").write_text(
        NOTES["Knowledge Base/Notes/Insights/handoff-0.md"].replace(
            f"{MARKER}0", "quokkanewafterbuild"
        ),
        encoding="utf-8",
    )
    (root / "Knowledge Base/Notes/Insights/handoff-3.md").unlink()


def test_the_promoted_warm_heals_the_adopted_catalogue_without_a_rebuild(
    older_catalogue: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = older_catalogue
    record = _promote_over_older_catalogue(root, monkeypatch, between=_serving_writes)
    assert record["lexical_catalogue"] == "adopted"
    # The watcher's boot seed lands after `release()` and before the warm.
    _seed_live_scopes(root)

    rebuilds: list[str] = []
    monkeypatch.setattr(
        lexstore.LexicalStore, "rebuild_atomic", lambda self: rebuilds.append("atomic")
    )
    real_rebuild = lexstore.LexicalStore._rebuild

    def counted_rebuild(self, conn):
        rebuilds.append("in-place")
        return real_rebuild(self, conn)

    monkeypatch.setattr(lexstore.LexicalStore, "_rebuild", counted_rebuild)
    heals: list[str] = []
    real_fresh = lexstore.ensure_fresh
    monkeypatch.setattr(
        lexstore, "ensure_fresh", lambda vault_root: (heals.append("ensure_fresh"), real_fresh(vault_root))
    )
    readiness.manage_runtime()
    readiness.begin_warm()
    try:
        assert warmup.warm_retrieval_catalog(root) is True
    finally:
        readiness.finish_warm()

    assert heals == ["ensure_fresh"]
    assert rebuilds == [], rebuilds
    assert readiness.retrieval_admission(root)["admitted"] is True
    assert lexstore.search_bm25(root, "quokkaafterbuild", k=3, scope="kb")
    assert lexstore.search_bm25(root, "quokkanewafterbuild", k=3, scope="kb")
    assert not lexstore.search_bm25(root, f"{MARKER}3", k=3, scope="kb")


def test_the_carried_handoff_is_not_a_readiness_component(
    older_catalogue: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-marking carried components must skip the one that is not a gate."""
    root = older_catalogue
    _promote_over_older_catalogue(root, monkeypatch)
    carried = warmup._carry_forward_standby_readiness()
    assert "catalogue_handoff" in carried


def test_a_cold_start_upgrades_an_older_schema_catalogue_in_place_and_admits(
    older_catalogue: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hosted-cell path: a new image on an existing volume, no standby.

    Nothing is carried, so warm-up delegates the incompatible catalogue to the
    ordinary single-flight repair, which rebuilds it on the same volume and
    admits retrieval. The handoff heal must not run on this path.
    """
    root = older_catalogue
    heals: list[str] = []
    monkeypatch.setattr(lexstore, "ensure_fresh", lambda vault_root: heals.append("heal"))
    rebuilds: list[bool] = []
    real_atomic = lexstore.LexicalStore.rebuild_atomic

    def counted(self):
        published = real_atomic(self)
        rebuilds.append(published)
        return published

    monkeypatch.setattr(lexstore.LexicalStore, "rebuild_atomic", counted)
    _seed_live_scopes(root)
    readiness.manage_runtime()
    readiness.begin_warm()
    try:
        admitted_now = warmup.warm_retrieval_catalog(root)
    finally:
        readiness.finish_warm()
    assert admitted_now is False, "an older-schema catalogue cannot be admitted as is"
    assert lexstore.await_repairs_idle(root)

    assert heals == []
    assert rebuilds == [True]
    assert lexstore.live_catalog_compatible(root) is True
    assert readiness.retrieval_admission(root)["admitted"] is True
    assert lexstore.search_bm25(root, f"{MARKER}1", k=3, scope="kb")
