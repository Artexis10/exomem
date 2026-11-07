"""Prepare one warm base: migrated state, full lexical catalogue, full graph.

Usage (spike environment set by spike_env.run_child):
    build_base.py <run_root>
Prints one JSON line with the cold build timings.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path


def main(run_root: Path) -> None:
    from spike_env import layout

    vault = layout(run_root)["vault"]
    from exomem import epistemic_graph, lexstore, state_migration

    started = time.perf_counter()
    authority = state_migration.assert_offline_migration_authority(source="single-store-spike")
    state_migration.migrate_vault_state_offline(vault, authority=authority)
    state_migration.reset_state_resolution_cache_for_tests()
    state_migration.require_vault_state_ready(vault)
    migrate_s = time.perf_counter() - started

    started = time.perf_counter()
    lexstore.search_bm25(vault, "warmup", 3, scope="kb", repair=True)
    lexical_s = time.perf_counter() - started

    started = time.perf_counter()
    counts = epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    graph_s = time.perf_counter() - started

    con = sqlite3.connect(f"file:{lexstore.lexical_path(vault)}?mode=ro", uri=True)
    lex_pages = con.execute("select count(*) from pages").fetchone()[0]
    con.close()
    con = sqlite3.connect(f"file:{epistemic_graph.sidecar_path(vault)}?mode=ro", uri=True)
    nodes = con.execute("select count(*) from graph_nodes").fetchone()[0]
    edges = con.execute("select count(*) from graph_edges").fetchone()[0]
    con.close()
    print(json.dumps({
        "migrate_s": round(migrate_s, 2), "lexical_build_s": round(lexical_s, 2),
        "graph_build_s": round(graph_s, 2), "graph_rebuild_counts": counts,
        "lexical_pages": lex_pages, "graph_nodes": nodes, "graph_edges": edges,
        "lexical_path": str(lexstore.lexical_path(vault)),
        "graph_path": str(epistemic_graph.sidecar_path(vault)),
    }, default=str))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
