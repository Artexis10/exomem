"""Row-level diff of derived rows: today's sidecars against the single store.

Both arms start from the same base and receive the same page bytes, and the
store derives rows with Exomem's own code. Identical rows mean every reader
(find, recall, graph traversal) returns the same answer on both stores.

Usage: compare_tables.py <lexical.sqlite> <graph.sqlite> <store.sqlite> <replay.jsonl>
Columns that record when or in which order a row was written are excluded.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter

# Write-order and wall-clock columns differ by construction between two writers.
VOLATILE = {"rowid", "mtime_ns", "generation", "parent_generation", "indexed_at",
            "updated_at", "created_at", "seq", "id"}

QUERIES = {
    "lexical.pages": ("pages", "path"),
    "lexical.semantic_units": ("semantic_units", "parent_path"),
    "graph.nodes": ("graph_nodes", "path"),
    "graph.edges": ("graph_edges", "source_path"),
    "graph.dependencies": ("graph_dependencies", "source_path"),
    "graph.parent_refs": ("graph_parent_refs", "path"),
}


def rows(conn: sqlite3.Connection, table: str, key: str, value: str) -> Counter:
    cur = conn.execute(f'SELECT * FROM "{table}" WHERE "{key}" = ?', (value,))
    names = [d[0] for d in cur.description]
    keep = [i for i, n in enumerate(names) if n not in VOLATILE]
    return Counter(json.dumps([r[i] for i in keep], default=str) for r in cur.fetchall())


def all_rows(conn: sqlite3.Connection, table: str) -> Counter:
    cur = conn.execute(f'SELECT * FROM "{table}"')
    names = [d[0] for d in cur.description]
    keep = [i for i, n in enumerate(names) if n not in VOLATILE]
    return Counter(json.dumps([r[i] for i in keep], default=str) for r in cur)


def fts_text(conn: sqlite3.Connection, path: str) -> tuple:
    row = conn.execute("SELECT rowid FROM pages WHERE path = ?", (path,)).fetchone()
    if row is None:
        return (None, None)
    stem = conn.execute("SELECT stemmed FROM fts WHERE rowid = ?", row).fetchone()
    tri = conn.execute("SELECT title_lower, body_lower FROM tri WHERE rowid = ?", row).fetchone()
    return (stem, tri)


def main(lex_db: str, graph_db: str, store_db: str, replay: str) -> None:
    lex = sqlite3.connect(f"file:{lex_db}?mode=ro", uri=True)
    graph = sqlite3.connect(f"file:{graph_db}?mode=ro", uri=True)
    store = sqlite3.connect(f"file:{store_db}?mode=ro", uri=True)
    paths = sorted({json.loads(line)["path"] for line in open(replay, encoding="utf-8")})
    result = {name: {"equal": 0, "differ": []} for name in [*QUERIES, "lexical.fts_text"]}
    for path in paths:
        for name, (table, key) in QUERIES.items():
            a = rows(lex if name.startswith("lexical") else graph, table, key, path)
            b = rows(store, table, key, path)
            if a == b:
                result[name]["equal"] += 1
            else:
                result[name]["differ"].append({"path": path, "only_today": len(a - b),
                                               "only_store": len(b - a)})
        if fts_text(lex, path) == fts_text(store, path):
            result["lexical.fts_text"]["equal"] += 1
        else:
            result["lexical.fts_text"]["differ"].append({"path": path})
    summary = {name: {"equal": v["equal"], "differ": len(v["differ"])} for name, v in result.items()}
    # Whole tables catch rows a write changed on other pages (dependents).
    whole = {}
    for name, (table, _key) in QUERIES.items():
        src = lex if name.startswith("lexical") else graph
        a, b = all_rows(src, table), all_rows(store, table)
        whole[name] = {"today": sum(a.values()), "store": sum(b.values()),
                       "only_today": sum((a - b).values()), "only_store": sum((b - a).values())}
    print(json.dumps({"paths": len(paths), "tables": summary, "whole_tables": whole}))
    examples = {name: v["differ"][:3] for name, v in result.items() if v["differ"]}
    if examples:
        print(json.dumps({"examples": examples}))


if __name__ == "__main__":
    main(*sys.argv[1:5])
