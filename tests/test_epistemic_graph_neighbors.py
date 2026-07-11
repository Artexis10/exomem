"""Typed-graph batch neighbour read + freshness generation token.

Covers the sidecar read API the find graph lane rebases on:
`EpistemicGraphIndex.neighbors_for` (bidirectional typed-edge expansion,
placeholder exclusion, determinism) and the module-level `cache_token`
generation counter that keeps `find`'s hot cache from serving rankings
computed against a stale graph.
"""

from __future__ import annotations

from pathlib import Path

from exomem import epistemic_graph

SEED = "Knowledge Base/Notes/Insights/seed.md"
TARGET = "Knowledge Base/Notes/Insights/target.md"
LINKED = "Knowledge Base/Notes/Insights/linked.md"
INBOUND = "Knowledge Base/Notes/Insights/inbound.md"


def _write(vault: Path, rel: str, body: str) -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _seed(vault: Path) -> None:
    _write(
        vault,
        SEED,
        """\
---
type: insight
status: active
---
# Seed

## Relations

- evidenced_by [[Knowledge Base/Notes/Insights/target]]
- links_to [[Knowledge Base/Notes/Insights/linked]]
- supports [[Knowledge Base/Notes/Insights/does-not-exist]]
""",
    )
    _write(vault, TARGET, "---\ntype: insight\n---\n# Target\n\nBody.\n")
    _write(vault, LINKED, "---\ntype: insight\n---\n# Linked\n\nBody.\n")
    _write(
        vault,
        INBOUND,
        """\
---
type: insight
---
# Inbound

## Relations

- supports [[Knowledge Base/Notes/Insights/seed]]
""",
    )


def test_neighbors_for_returns_both_directions(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    idx = epistemic_graph.EpistemicGraphIndex(vault)
    idx.rebuild_all()

    neighbors = idx.neighbors_for([SEED])
    by_target = {n.other_rel: n for n in neighbors}

    assert TARGET in by_target
    assert by_target[TARGET].relation_type == "evidenced_by"
    assert by_target[TARGET].direction == "outbound"
    assert by_target[TARGET].family == "evidence"
    assert by_target[TARGET].seed_rel == SEED

    assert LINKED in by_target
    assert by_target[LINKED].family == "link"

    # Inbound edge from another page surfaces its source as a neighbour.
    assert INBOUND in by_target
    assert by_target[INBOUND].relation_type == "supports"
    assert by_target[INBOUND].direction == "inbound"


def test_neighbors_for_excludes_placeholder_targets(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    idx = epistemic_graph.EpistemicGraphIndex(vault)
    idx.rebuild_all()

    targets = {n.other_rel for n in idx.neighbors_for([SEED])}
    assert not any("does-not-exist" in t for t in targets)
    # The seed itself is never surfaced as its own neighbour.
    assert SEED not in targets


def test_neighbors_for_is_deterministic(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    idx = epistemic_graph.EpistemicGraphIndex(vault)
    idx.rebuild_all()

    first = idx.neighbors_for([SEED])
    second = idx.neighbors_for([SEED])
    assert [(n.seed_rel, n.other_rel, n.relation_type, n.direction) for n in first] == [
        (n.seed_rel, n.other_rel, n.relation_type, n.direction) for n in second
    ]


def test_neighbors_for_empty_when_unavailable(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    idx = epistemic_graph.EpistemicGraphIndex(vault)
    idx.rebuild_all()
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_INDEX", "1")
    assert epistemic_graph.EpistemicGraphIndex(vault).neighbors_for([SEED]) == []


def test_cache_token_none_when_unavailable(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    # No sidecar yet.
    assert epistemic_graph.cache_token(vault) is None
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    assert epistemic_graph.cache_token(vault) is not None
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_INDEX", "1")
    assert epistemic_graph.cache_token(vault) is None


def test_generation_bumps_on_write(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    idx = epistemic_graph.EpistemicGraphIndex(vault)
    idx.rebuild_all()
    before = epistemic_graph.cache_token(vault)

    target = vault / TARGET
    target.write_text(target.read_text(encoding="utf-8") + "\nMore.\n", encoding="utf-8")
    epistemic_graph.upsert_after_write(vault, [target])

    after = epistemic_graph.cache_token(vault)
    assert after != before
    assert int(after[2]) > int(before[2])


def test_generation_bumps_on_delete(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    idx = epistemic_graph.EpistemicGraphIndex(vault)
    idx.rebuild_all()
    before = epistemic_graph.cache_token(vault)

    epistemic_graph.delete_after_remove(vault, [LINKED])

    after = epistemic_graph.cache_token(vault)
    assert int(after[2]) > int(before[2])


def test_generation_bumps_on_rebuild(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _seed(vault)
    idx = epistemic_graph.EpistemicGraphIndex(vault)
    idx.rebuild_all()
    before = epistemic_graph.cache_token(vault)

    idx.rebuild_all()
    after = epistemic_graph.cache_token(vault)
    assert after != before


def test_connect_does_not_hold_an_open_write_transaction(tmp_path: Path) -> None:
    """A pure-read connection (as neighbors_for/nodes/edges use) must not open
    an implicit write transaction — that would contend with a genuine
    concurrent writer (multi-host vault, #201) for a connection that never
    intends to write anything."""
    vault = tmp_path / "vault"
    _seed(vault)
    idx = epistemic_graph.EpistemicGraphIndex(vault)
    idx.rebuild_all()

    conn = idx._connect()
    try:
        assert conn.in_transaction is False, (
            "_connect() left an open write transaction — a read-only caller "
            "(neighbors_for/nodes/edges) now contends for the writer lock"
        )
    finally:
        conn.close()


def test_content_edit_moves_token_like_a_rebuild(tmp_path: Path) -> None:
    """The freshness discipline: an incremental relation-adding write moves the
    token exactly as a rebuild would, so a cached ranking cannot outlive the
    edge that changed the graph."""
    vault = tmp_path / "vault"
    _seed(vault)
    idx = epistemic_graph.EpistemicGraphIndex(vault)
    idx.rebuild_all()
    before = epistemic_graph.cache_token(vault)

    seed = vault / SEED
    seed.write_text(
        seed.read_text(encoding="utf-8")
        + "- refines [[Knowledge Base/Notes/Insights/target]]\n",
        encoding="utf-8",
    )
    epistemic_graph.upsert_after_write(vault, [seed])
    incremental = epistemic_graph.cache_token(vault)
    assert incremental != before

    epistemic_graph.sidecar_path(vault).unlink()
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    rebuilt = epistemic_graph.cache_token(vault)
    # Both operations produce a live token (schema + registry identity match);
    # the generation counter differs by history, which is expected.
    assert rebuilt is not None
    assert rebuilt[0] == incremental[0]
    assert rebuilt[1] == incremental[1]
