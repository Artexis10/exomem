"""Arm B: the single-store prototype, replaying arm A's committed page bytes.

migrate <run_root>                      build the store from the warm base
run     <run_root> <replay.jsonl> <out_dir> [--max-ops N]
        closed-loop replay; a projector thread writes Markdown after commit
check   <run_root> <out.json>           after a hard kill: store invariants,
        projection state before and after the projector drains
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

from spike_env import layout


def _vault_state(run_root: Path) -> tuple[Path, Path]:
    from exomem import lexstore, state_migration

    vault = layout(run_root)["vault"]
    state_migration.require_vault_state_ready(vault)
    return vault, lexstore.lexical_path(vault).parent


def migrate_cmd(args) -> None:
    import single_store

    vault, state_dir = _vault_state(args.run_root)
    print(json.dumps(single_store.migrate(vault, single_store.store_path(state_dir))), flush=True)


def run_cmd(args) -> None:
    import single_store

    vault, state_dir = _vault_state(args.run_root)
    store = single_store.Store(vault, single_store.store_path(state_dir))
    ops = [json.loads(line) for line in args.replay.open(encoding="utf-8")]
    if args.max_ops:
        ops = ops[: args.max_ops]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    projected: dict[tuple[str, int], float] = {}
    proj_rows = []
    busy = {"ms": 0.0}

    def on_item(rel, rev, ms, at):
        projected[(rel, rev)] = at
        proj_rows.append({"path": rel, "rev": rev, "ms": round(ms, 3)})
        busy["ms"] += ms

    # Warm the derivation caches once, outside the measured ops (arm A's
    # runtime warms the same resolver and registries before its first op).
    def files_of(op):
        if "files" in op:
            return [(f["path"], base64.b64decode(f["b64"])) for f in op["files"]]
        return [(op["path"], base64.b64decode(op["b64"]))]

    warm = ops[0]
    store.put_many(files_of(warm), op="warm")
    stop, thread = store.start_projector(on_item=on_item)
    print(json.dumps({"event": "ready", "ops": len(ops)}), flush=True)
    rows = []
    t0 = time.perf_counter()
    for op in ops:
        items = files_of(op)
        started = time.perf_counter()
        row = {"n": op["n"], "op": op["op"], "path": op["path"], "error": None}
        try:
            res = store.put_many(items)
            row.update(rev=res["revs"].get(op["path"]), derive_ms=round(res["derive_ms"], 2),
                       commit_ms=round(res["commit_ms"], 2), edges=res["edges"],
                       files=res["files"])
        except Exception as exc:  # noqa: BLE001 - recorded, the replay continues
            row["error"] = f"{type(exc).__name__}: {exc}"[:400]
        ack = time.perf_counter()
        row["ack_ms"] = round((ack - started) * 1000, 2)
        row["_ack_t"] = ack
        if row["error"] is None:
            row["lex_at_ack"] = store.lexical(op["path"], op["token"])
            row["graph_at_ack"] = store.graph_edge(op["path"], op["link"])
        rows.append(row)
        print(json.dumps({"event": "op", "n": op["n"], "ack_ms": row["ack_ms"],
                          "error": (row["error"] or "")[:80]}), flush=True)
    wall = time.perf_counter() - t0
    deadline = time.perf_counter() + 120
    while time.perf_counter() < deadline:
        left = store.conn.execute("SELECT count(*) FROM store_projection").fetchone()[0]
        if left == 0:
            break
        time.sleep(0.05)
    stop.set()
    thread.join(5)
    def exported_at(path: str, rev) -> float | None:
        # Two revisions queued before a projection coalesce into the later one.
        times = [t for (p, r), t in projected.items() if p == path and rev is not None and r >= rev]
        return min(times) if times else None

    with (args.out_dir / "arm_single.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows:
            at = exported_at(row["path"], row.get("rev"))
            row["export_lag_ms"] = round((at - row["_ack_t"]) * 1000, 2) if at else None
            row.pop("_ack_t")
            fh.write(json.dumps(row) + "\n")
    with (args.out_dir / "projection.jsonl").open("w", encoding="utf-8") as fh:
        for item in proj_rows:
            fh.write(json.dumps(item) + "\n")
    print(json.dumps({"event": "done", "wall_s": round(wall, 2),
                      "projector_busy_s": round(busy["ms"] / 1000, 3),
                      "projected": len(proj_rows)}), flush=True)
    os._exit(0)


def check_cmd(args) -> None:
    import single_store
    from exomem import vault as vault_module

    vault, state_dir = _vault_state(args.run_root)
    db = single_store.store_path(state_dir)
    conn = single_store.connect(db)
    report = {"integrity": conn.execute("PRAGMA quick_check").fetchone()[0]}
    written = conn.execute(
        "SELECT p.path, p.body, p.hash, p.rev FROM store_pages p WHERE p.path IN (SELECT path FROM store_audit)").fetchall()
    bad_lex, bad_graph = [], []
    for rel, body, _h, _rev in written:
        if not rel.endswith(".md"):
            continue
        text = body.decode("utf-8")
        if conn.execute("SELECT 1 FROM pages WHERE path=?", (rel,)).fetchone() is None:
            bad_lex.append(rel)
        node = conn.execute("SELECT source_hash FROM graph_nodes WHERE path=? LIMIT 1",
                            (rel,)).fetchone()
        if node is None or node[0] != vault_module.content_hash(text):
            bad_graph.append(rel)
    report["written_pages"] = len(written)
    report["derived_mismatch"] = {"lex": bad_lex, "graph": bad_graph}

    def projection_state() -> dict:
        pending = {r[0] for r in conn.execute("SELECT path FROM store_projection")}
        stale, missing = [], []
        for rel, body, h, _rev in conn.execute(
                "SELECT path, body, hash, rev FROM store_pages WHERE path IN (SELECT path FROM store_audit)"):
            f = vault / rel
            if not f.exists():
                (missing if rel not in pending else []).append(rel)
            elif single_store.sha(f.read_bytes()) != h and rel not in pending:
                stale.append(rel)
        leftovers = [str(p) for p in (vault / "Knowledge Base").rglob(".*.projecting")]
        return {"pending": len(pending), "stale_unqueued": stale, "missing_unqueued": missing,
                "temp_leftovers": leftovers}

    report["before"] = projection_state()
    # Startup sweep: a projection killed between write and rename leaves a temp file.
    for leftover in report["before"]["temp_leftovers"]:
        Path(leftover).unlink(missing_ok=True)
    store = single_store.Store(vault, db)
    t0 = time.perf_counter()
    while store.project_once(conn):
        pass
    report["drain_s"] = round(time.perf_counter() - t0, 3)
    report["after"] = projection_state()
    report["after"]["all_files_match"] = all(
        single_store.sha((vault / rel).read_bytes()) == h
        for rel, h in conn.execute("SELECT path, hash FROM store_pages WHERE path IN (SELECT path FROM store_audit)"))
    args.out.write_text(json.dumps(report, indent=1))
    summary = {k: report[k] for k in ("integrity", "written_pages", "drain_s")}
    summary["derived_mismatch"] = {k: len(v) for k, v in report["derived_mismatch"].items()}
    summary["before_pending"] = report["before"]["pending"]
    summary["before_stale_unqueued"] = len(report["before"]["stale_unqueued"])
    summary["after_all_files_match"] = report["after"]["all_files_match"]
    print(json.dumps(summary), flush=True)
    os._exit(0)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("migrate")
    m.add_argument("run_root", type=Path)
    r = sub.add_parser("run")
    r.add_argument("run_root", type=Path)
    r.add_argument("replay", type=Path)
    r.add_argument("out_dir", type=Path)
    r.add_argument("--max-ops", type=int, default=0)
    c = sub.add_parser("check")
    c.add_argument("run_root", type=Path)
    c.add_argument("out", type=Path)
    args = ap.parse_args()
    {"migrate": migrate_cmd, "run": run_cmd, "check": check_cmd}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
