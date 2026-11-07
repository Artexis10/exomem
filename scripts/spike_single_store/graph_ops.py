"""Cost of graph capabilities over the store's graph tables.

Usage: graph_ops.py <sqlite file with graph_nodes/graph_edges> [--seeds N]
Measures, over page-level edges (an edge's source page -> its target page):
  - k-hop neighbourhoods (k = 1..3) as one recursive SQL query that checks
    each hop's node (the place a per-hop visibility rule would run);
  - one combined query: lexical hits expanded one hop, which only a single
    store can answer in one statement;
  - in-memory algorithms on demand: load, PageRank (power iteration),
    connected components, label-propagation communities, BFS shortest paths.
Prints one JSON object of timings in milliseconds (p50/p95 over seeds).
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import time
from collections import defaultdict, deque


def pct(xs, q):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * len(xs)))], 2)


KHOP = """
WITH RECURSIVE walk(page, depth) AS (
  SELECT ?, 0
  UNION
  SELECT n2.path, walk.depth + 1
  FROM walk
  JOIN graph_edges e ON e.source_path = walk.page
  JOIN graph_nodes n2 ON n2.node_key = e.dst_page_key
  WHERE walk.depth < ? AND n2.kind = 'file' AND coalesce(n2.access_tier, '') != 'withheld'
)
SELECT count(DISTINCT page) FROM walk
"""

FTS_EXPAND = """
WITH hits AS (
  SELECT p.path FROM fts JOIN pages p ON p.rowid = fts.rowid WHERE fts MATCH ? LIMIT 10
)
SELECT count(DISTINCT n2.path) FROM hits
JOIN graph_edges e ON e.source_path = hits.path
JOIN graph_nodes n2 ON n2.node_key = e.dst_page_key AND n2.kind = 'file'
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--seeds", type=int, default=60)
    args = ap.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rng = random.Random(5)
    sources = [r[0] for r in conn.execute(
        "SELECT DISTINCT source_path FROM graph_edges WHERE dst_page_key IS NOT NULL")]
    seeds = rng.sample(sources, min(args.seeds, len(sources)))
    out = {"pages_with_out_edges": len(sources),
           "edges": conn.execute("SELECT count(*) FROM graph_edges").fetchone()[0]}
    for seed in seeds:  # warm the page cache once so k=1 is not charged for it
        conn.execute(KHOP, (seed, 3)).fetchone()
    for k in (1, 2, 3):
        times, sizes = [], []
        for seed in seeds:
            t = time.perf_counter()
            sizes.append(conn.execute(KHOP, (seed, k)).fetchone()[0])
            times.append((time.perf_counter() - t) * 1000)
        out[f"khop_{k}"] = {"p50_ms": pct(times, .5), "p95_ms": pct(times, .95),
                            "reach_p50": pct(sizes, .5), "reach_p95": pct(sizes, .95)}
    has_fts = conn.execute("SELECT 1 FROM sqlite_master WHERE name='fts'").fetchone()
    if has_fts:
        words = []
        for (title,) in conn.execute("SELECT title FROM pages WHERE title IS NOT NULL LIMIT 4000"):
            words.extend(w for w in title.lower().split() if w.isalpha() and len(w) > 4)
        terms = rng.sample(words, min(40, len(words)))
        times = []
        for term in terms:
            t = time.perf_counter()
            try:
                conn.execute(FTS_EXPAND, (f'"{term}"',)).fetchone()
            except sqlite3.OperationalError:
                continue
            times.append((time.perf_counter() - t) * 1000)
        if times:
            out["fts_hits_plus_one_hop"] = {"p50_ms": pct(times, .5), "p95_ms": pct(times, .95)}

    t = time.perf_counter()
    file_key = dict(conn.execute("SELECT node_key, path FROM graph_nodes WHERE kind='file'"))
    adj = defaultdict(set)
    for src, dst in conn.execute("SELECT source_path, dst_page_key FROM graph_edges "
                                 "WHERE dst_page_key IS NOT NULL"):
        target = file_key.get(dst)
        if target and target != src:
            adj[src].add(target)
    nodes = set(file_key.values())
    out["load_page_graph_ms"] = round((time.perf_counter() - t) * 1000, 1)
    out["page_nodes"] = len(nodes)
    out["page_edges"] = sum(len(v) for v in adj.values())

    t = time.perf_counter()
    rank = {n: 1.0 / len(nodes) for n in nodes}
    for _ in range(20):
        nxt = {n: 0.15 / len(nodes) for n in nodes}
        for src, targets in adj.items():
            if targets:
                share = 0.85 * rank.get(src, 0.0) / len(targets)
                for dst in targets:
                    nxt[dst] = nxt.get(dst, 0.0) + share
        rank = nxt
    out["pagerank_20_iterations_ms"] = round((time.perf_counter() - t) * 1000, 1)

    undirected = defaultdict(set)
    for src, targets in adj.items():
        for dst in targets:
            undirected[src].add(dst)
            undirected[dst].add(src)
    t = time.perf_counter()
    seen, components = set(), 0
    for n in nodes:
        if n in seen:
            continue
        components += 1
        queue = deque([n])
        seen.add(n)
        while queue:
            for m in undirected[queue.popleft()]:
                if m not in seen:
                    seen.add(m)
                    queue.append(m)
    out["connected_components_ms"] = round((time.perf_counter() - t) * 1000, 1)
    out["components"] = components
    out["isolated_pages"] = sum(1 for n in nodes if not undirected[n])

    t = time.perf_counter()
    label = {n: n for n in nodes}
    order = sorted(nodes)
    for _ in range(10):
        rng.shuffle(order)
        for n in order:
            if undirected[n]:
                counts = defaultdict(int)
                for m in undirected[n]:
                    counts[label[m]] += 1
                label[n] = max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
    out["label_propagation_10_rounds_ms"] = round((time.perf_counter() - t) * 1000, 1)
    sizes = defaultdict(int)
    for v in label.values():
        sizes[v] += 1
    out["communities_over_2_pages"] = sum(1 for s in sizes.values() if s > 2)

    times = []
    connected = [n for n in nodes if undirected[n]]
    for _ in range(40):
        a, b = rng.sample(connected, 2)
        t = time.perf_counter()
        prev, queue, found = {a: None}, deque([a]), False
        while queue and not found:
            for m in undirected[queue.popleft()]:
                if m not in prev:
                    prev[m] = True
                    if m == b:
                        found = True
                        break
                    queue.append(m)
        times.append((time.perf_counter() - t) * 1000)
    out["bfs_shortest_path_ms"] = {"p50": pct(times, .5), "p95": pct(times, .95)}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
