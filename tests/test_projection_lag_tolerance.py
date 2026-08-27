"""Semantic recall admission tolerates bounded projection lag (task 2.4).

The maintained catalog is published with its own exact recall checkpoint. When
the live freshness registry has no projection for a scope -- a cold or
restarted registry, or one evicted by a reprojection/catch-up window -- the
strict proof returns None and every semantic recall in that window refuses with
RETRIEVAL_INDEX_WARMING, even though a perfectly good published projection is
sitting in the sidecar.

Measured on a warmed tmp vault (`rebuild_atomic()` then `freshness.clear()`):

    strict proof = REFUSE   recall_is_live={'kb': False, 'vault': False}
    kb:    live_ckpt=None  stored_gen=2
    vault: live_ckpt=None  stored_gen=4

Admission must serve from that published projection and disclose the staleness,
refusing only when no published projection exists or its identity no longer
matches.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import epistemic_graph, file_watcher, freshness, lexstore


def _warm_catalog(vault: Path) -> file_watcher.FileWatcher:
    """Seed the registry and bless the catalog through the real publish path."""
    watcher = file_watcher.FileWatcher(vault)
    watcher._reconcile_once(seed=True)
    assert lexstore.get_store(vault).rebuild_atomic() is True
    assert lexstore.runtime_retrieval_catalog_proof(vault, schedule_repair=False) is not None
    return watcher


def _go_cold() -> None:
    """Drop the live registry, leaving the published projection intact."""
    freshness.clear()


def test_cold_registry_with_published_projection_admits_as_stale(vault: Path) -> None:
    """A cold registry must serve the last published projection, not refuse."""
    _warm_catalog(vault)
    _go_cold()

    # The strict proof stays strict: health, warm-up and repair still require
    # the catalog to sit exactly at the live projection.
    assert lexstore.runtime_retrieval_catalog_proof(vault, schedule_repair=False) is None
    assert freshness.recall_is_live(vault, "kb") is False

    admission = lexstore.runtime_retrieval_catalog_admission(vault, schedule_repair=False)

    assert admission is not None, "a valid published projection was refused"
    assert admission.stale is True
    assert set(admission.checkpoints) == set(freshness.SCOPES)
    assert set(admission.lagging_scopes) == set(freshness.SCOPES)


def test_live_registry_at_the_catalog_admits_without_staleness(vault: Path) -> None:
    """The exact-match case keeps admitting, and discloses nothing."""
    _warm_catalog(vault)

    admission = lexstore.runtime_retrieval_catalog_admission(vault, schedule_repair=False)

    assert admission is not None
    assert admission.stale is False
    assert admission.lagging_scopes == ()


def test_access_identity_change_still_refuses(vault: Path) -> None:
    """Counter-scenario: an identity change refuses until republication."""
    _warm_catalog(vault)
    _go_cold()
    assert lexstore.runtime_retrieval_catalog_admission(vault, schedule_repair=False) is not None

    # A real access-policy change moves the access fingerprint, so the
    # published projection no longer describes what this process may serve.
    (vault / "Knowledge Base" / "_access.yaml").write_text(
        "excluded:\n  - Private\n", encoding="utf-8"
    )

    assert lexstore.runtime_retrieval_catalog_admission(vault, schedule_repair=False) is None


def test_absent_published_projection_still_refuses(vault: Path) -> None:
    """Counter-scenario: nothing published means nothing to serve."""
    watcher = file_watcher.FileWatcher(vault)
    watcher._reconcile_once(seed=True)
    _go_cold()

    # No rebuild_atomic() ever ran, so no scope carries a published checkpoint.
    assert lexstore.runtime_retrieval_catalog_admission(vault, schedule_repair=False) is None


def test_graph_reprojection_window_does_not_refuse(vault: Path) -> None:
    """The live-reported window: a graph rebuild must not blank semantic recall."""
    _warm_catalog(vault)
    graph = epistemic_graph.EpistemicGraphIndex(vault)
    graph.rebuild_all()
    _go_cold()

    admission = lexstore.runtime_retrieval_catalog_admission(vault, schedule_repair=False)

    assert admission is not None, "a graph reprojection window blanked semantic recall"
    assert admission.stale is True


def _managed_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import readiness

    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
    monkeypatch.setattr(
        readiness,
        "retrieval_admission",
        lambda _root=None: {"state": "ready", "admitted": True},
    )


def test_find_serves_a_cold_registry_with_a_staleness_disclosure(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The behaviour that matters: serve, disclose, do not refuse."""
    from exomem import find as find_module

    _warm_catalog(vault)
    _go_cold()
    _managed_runtime(monkeypatch)

    degraded: list[str] = []
    hits = find_module.find(vault, query="memory", limit=5, degraded_out=degraded)

    assert isinstance(hits, list)
    assert "recall_projection" in degraded, (
        "a stale answer must disclose bounded staleness on the warming envelope"
    )


def test_find_at_an_exact_projection_discloses_nothing(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fully current answer must not carry the staleness marker."""
    from exomem import find as find_module

    _warm_catalog(vault)
    _managed_runtime(monkeypatch)

    degraded: list[str] = []
    find_module.find(vault, query="memory", limit=5, degraded_out=degraded)

    assert "recall_projection" not in degraded


def test_find_still_refuses_when_nothing_is_published(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counter-scenario: no published projection is still a refusal."""
    from exomem import find as find_module

    watcher = file_watcher.FileWatcher(vault)
    watcher._reconcile_once(seed=True)
    _go_cold()
    _managed_runtime(monkeypatch)

    with pytest.raises(find_module.RetrievalIndexWarming):
        find_module.find(vault, query="memory", limit=5)


def test_catalog_identity_change_still_refuses(vault: Path) -> None:
    """Counter-scenario: the third identity component the spec names.

    `catalog_semantic_identity` hashes the semantic-language registry, so
    changing it invalidates the catalog's parsed rows WITHOUT touching recall
    policy or the access fingerprint. The published projection's rows no longer
    describe the corpus, so it must not be served.
    """
    from exomem import semantic_language_registry

    _warm_catalog(vault)
    _go_cold()
    assert lexstore.runtime_retrieval_catalog_admission(vault, schedule_repair=False) is not None

    registry = semantic_language_registry.registry_path(vault)
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text("kinds:\n  invented-kind:\n    label: Invented\n", encoding="utf-8")

    assert lexstore.runtime_retrieval_catalog_admission(vault, schedule_repair=False) is None


def test_a_published_row_without_a_triple_still_refuses(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A well-formed row whose triple is None proves no projection.

    The checkpoint parses, so the row-shape guard passes it; without the
    triple there is no projected corpus identity to serve against, and the
    admission must refuse rather than answer from nothing.
    """
    _warm_catalog(vault)
    _go_cold()
    published = lexstore.get_store(vault).published_recall_checkpoint("kb")
    assert published is not None

    triple_less = freshness.RecallFreshnessCheckpoint(
        published.instance_id,
        published.generation,
        None,
        published.policy_version,
        published.access_policy_fingerprint,
    )
    monkeypatch.setattr(
        lexstore.LexicalStore,
        "published_recall_checkpoint",
        lambda _self, _scope: triple_less,
    )

    assert lexstore.runtime_retrieval_catalog_admission(vault, schedule_repair=False) is None
