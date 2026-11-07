"""Arm A: today's file-canonical path, driven through the governed commands.

run   <run_root> <stream.jsonl> <out_dir> [--rate-per-min R] [--max-ops N]
      Starts the local runtime (watcher, drains, catalogue recovery) the way the
      service does, replays the stream through `create_note` / `edit_note`, and
      writes one result row per op plus the committed page bytes (the replay
      input for the prototype arm).
check <run_root> <out.json> [--recover-seconds S]
      After a hard kill: measures drift between Markdown and the derived stores
      before and after the runtime's own startup recovery.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path

from spike_env import layout


def _ready(vault: Path) -> None:
    from exomem import state_migration

    state_migration.require_vault_state_ready(vault)


def _start_runtime(vault: Path, wait: float = 180.0):
    from exomem import server_runtime

    runtime = server_runtime.LocalRuntimeActivation(vault)
    runtime.start()
    thread = getattr(runtime, "_thread", None)
    if thread is not None:
        thread.join(wait)
    return runtime


class Visibility:
    """Answers "is this op's token findable / its new edge in the graph"."""

    def __init__(self, vault: Path):
        from exomem import epistemic_graph

        self.vault = vault
        self.graph = epistemic_graph.EpistemicGraphIndex(vault)

    def lexical(self, path: str, token: str) -> bool:
        from exomem import lexstore

        hits = lexstore.search_bm25(self.vault, token, 5, scope="kb", repair=False) or []
        return any(hit[0] == path for hit in hits)

    def graph_edge(self, path: str, link: str) -> bool:
        needle = link.lower()
        for edge in self.graph.edges(source_path=path) or []:
            if needle in json.dumps(edge, default=str, ensure_ascii=False).lower():
                return True
        return False


def _op_args(op: dict, created: list[str], vault: Path) -> tuple[str, dict]:
    if op["op"] == "create":
        return "create_note", {"title": op["title"], "content": op["content"],
                               "note_type": op["note_type"]}
    target = op["target"]
    rel = created[target["stream_create"]] if "stream_create" in target else target["vault_path"]
    text = (vault / rel).read_text(encoding="utf-8")
    # The marker is one observation unit, the shape the semantic contract accepts.
    unit = f"- [operating constraint] Stream marker {op['token']} relates to [[{op['link']}]] #spike"
    why = "single-store spike replay"
    if "\n## Observations" in text:
        return "edit_note", {"path": rel, "why": why,
                             "operation": {"kind": "edit_section", "heading": "Observations",
                                           "new_string": unit, "section_position": "append"}}
    lines = [line for line in text.splitlines() if line.strip()]
    old = next(line for line in reversed(lines) if text.count(line) == 1 and len(line) > 12)
    return "edit_note", {"path": rel, "why": why,
                         "operation": {"kind": "replace_string", "old_string": old,
                                       "new_string": f"{old}\n\n## Observations\n\n{unit}"}}


def changed_since(vault: Path, since_ns: int) -> list[str]:
    """Vault files modified at or after `since_ns`, vault-relative."""
    out = []
    for dirpath, dirnames, filenames in os.walk(vault):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            full = os.path.join(dirpath, name)
            try:
                if os.stat(full).st_mtime_ns >= since_ns:
                    out.append(Path(full).relative_to(vault).as_posix())
            except FileNotFoundError:
                continue
    return sorted(out)


def _result_path(result) -> str | None:
    if not isinstance(result, dict):
        return None
    for key in ("path", "rel_path"):
        if isinstance(result.get(key), str):
            return result[key]
    for value in result.values():
        if isinstance(value, dict):
            found = _result_path(value)
            if found:
                return found
    return None


def run(args) -> None:
    vault = layout(args.run_root)["vault"]
    from exomem import call_spans, product_invoke

    if os.environ.get("SPIKE_KILL_BEFORE_FANOUT"):
        # Deterministic crash window: the Markdown commit is durable and the
        # derived fan-out has not started. This is the window store mode removes.
        from exomem import vault as vault_module

        def _die(*_a, **_k):
            os.kill(os.getpid(), signal.SIGKILL)

        vault_module.post_commit_batch_fanout = _die

    _ready(vault)
    t_rt = time.perf_counter()
    _start_runtime(vault)
    runtime_s = time.perf_counter() - t_rt
    vis = Visibility(vault)
    ops = [json.loads(line) for line in args.stream.open(encoding="utf-8")]
    if args.max_ops:
        ops = ops[: args.max_ops]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    rows_f = (args.out_dir / "arm_files.jsonl").open("w", encoding="utf-8")
    replay_f = (args.out_dir / "replay.jsonl").open("w", encoding="utf-8")
    lock = threading.Lock()
    pending: list[dict] = []
    done: list[dict] = []
    stop = threading.Event()

    def poller() -> None:
        while not stop.is_set() or pending:
            now = time.perf_counter()
            with lock:
                items = list(pending)
            for row in items:
                if row["lex_lag_ms"] is None and vis.lexical(row["path"], row["token"]):
                    row["lex_lag_ms"] = (time.perf_counter() - row["_ack_t"]) * 1000
                if row["graph_lag_ms"] is None and vis.graph_edge(row["path"], row["link"]):
                    row["graph_lag_ms"] = (time.perf_counter() - row["_ack_t"]) * 1000
                expired = now - row["_ack_t"] > args.lag_timeout
                if (row["lex_lag_ms"] is not None and row["graph_lag_ms"] is not None) or expired:
                    with lock:
                        pending.remove(row)
                        done.append(row)
            time.sleep(0.05)

    poll_thread = threading.Thread(target=poller, daemon=True)
    poll_thread.start()
    print(json.dumps({"event": "ready", "runtime_s": round(runtime_s, 2), "ops": len(ops)}), flush=True)

    created: list[str] = []
    interval = 60.0 / args.rate_per_min if args.rate_per_min else 0.0
    t0 = time.perf_counter()
    for i, op in enumerate(ops):
        due = t0 + i * interval
        delay = due - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        row = {"n": op["n"], "op": op["op"], "token": op["token"], "link": op["link"],
               "late_ms": round(max(0.0, -delay) * 1000, 1), "error": None,
               "lex_lag_ms": None, "graph_lag_ms": None}
        try:
            name, raw = _op_args(op, created, vault)
        except Exception as exc:  # noqa: BLE001 - recorded, the stream continues
            row["error"] = f"prepare: {exc!r}"[:300]
            rows_f.write(json.dumps(row) + "\n")
            continue
        token = f"spike-{op['n']}"
        call_spans.reset()
        ctx = call_spans.MCP_CALL_TOKEN.set(token)
        op_start_ns = time.time_ns()
        started = time.perf_counter()
        try:
            result = product_invoke.invoke_product(name, raw, vault_root=vault)
        except Exception as exc:  # noqa: BLE001
            result = None
            row["error"] = f"{type(exc).__name__}: {exc}"[:400]
        ack_t = time.perf_counter()
        spans = call_spans.pop_call_spans(token)
        call_spans.MCP_CALL_TOKEN.reset(ctx)
        row["ack_ms"] = round((ack_t - started) * 1000, 2)
        row["spans"] = {}
        for span in spans:
            row["spans"][span["name"]] = round(row["spans"].get(span["name"], 0.0) + span["ms"], 2)
        path = _result_path(result) if row["error"] is None else None
        if path is None and op["op"] == "edit":
            path = raw.get("path")
        row["path"] = path
        if isinstance(result, dict):
            row["derived_sync"] = result.get("derived_sync")
            if result.get("ok") is False or result.get("error"):
                row["error"] = json.dumps(result.get("error") or result, default=str)[:400]
        if row["error"] is None and path:
            if op["op"] == "create":
                created.append(path)
            # Every file this op wrote (page, navigation indexes, compiled
            # history, relation reviews): the single store commits them together.
            files = changed_since(vault, op_start_ns)
            row["files_written"] = len(files)
            replay_f.write(json.dumps({"n": op["n"], "op": op["op"], "path": path,
                                       "token": op["token"], "link": op["link"],
                                       "files": [{"path": rel, "b64": base64.b64encode(
                                           (vault / rel).read_bytes()).decode()}
                                           for rel in files]}) + "\n")
            replay_f.flush()
            row["lex_at_ack"] = vis.lexical(path, op["token"])
            row["graph_at_ack"] = vis.graph_edge(path, op["link"])
            row["lex_lag_ms"] = 0.0 if row["lex_at_ack"] else None
            row["graph_lag_ms"] = 0.0 if row["graph_at_ack"] else None
            row["_ack_t"] = ack_t
            if row["lex_lag_ms"] is None or row["graph_lag_ms"] is None:
                with lock:
                    pending.append(row)
            else:
                with lock:
                    done.append(row)
        else:
            rows_f.write(json.dumps(row) + "\n")
        print(json.dumps({"event": "op", "n": op["n"], "ack_ms": row["ack_ms"],
                          "error": (row["error"] or "")[:80]}), flush=True)
    stop.set()
    poll_thread.join(args.lag_timeout + 5)
    for row in sorted(done, key=lambda r: r["n"]):
        row.pop("_ack_t", None)
        rows_f.write(json.dumps(row, default=str) + "\n")
    rows_f.close()
    replay_f.close()
    print(json.dumps({"event": "done", "wall_s": round(time.perf_counter() - t0, 1)}), flush=True)
    os._exit(0)  # the runtime's daemon workers need no orderly stop for this arm


# --- crash consistency -------------------------------------------------------

def lexical_drift(vault: Path) -> dict:
    """Pages whose Markdown and lexical row disagree (missing, stale or orphaned)."""
    from exomem import lexstore, recall_policy

    con = sqlite3.connect(f"file:{lexstore.lexical_path(vault)}?mode=ro", uri=True)
    rows = dict(con.execute("select path, mtime_ns from pages"))
    con.close()
    missing, stale = [], []
    seen = set()
    for p in (vault / "Knowledge Base").rglob("*.md"):
        if not recall_policy.is_recall_candidate(vault, p):
            continue
        rel = p.relative_to(vault).as_posix()
        seen.add(rel)
        if rel not in rows:
            missing.append(rel)
        elif rows[rel] != p.stat().st_mtime_ns:
            stale.append(rel)
    orphaned = [rel for rel in rows if rel.startswith("Knowledge Base/") and rel not in seen
                and not (vault / rel).exists()]
    return {"missing": missing, "stale": stale, "orphaned": orphaned}


def baseline_cmd(args) -> None:
    """Record the base's own drift, so a trial reports only what the kill caused."""
    vault = layout(args.run_root)["vault"]
    _ready(vault)
    lex = lexical_drift(vault)
    graph = [str(g)[:200] for g in graph_drift(vault)]
    args.out.write_text(json.dumps({"lex": lex, "graph": graph}))
    print(json.dumps(_summary(lex, graph)), flush=True)
    os._exit(0)


def _minus_baseline(run_root: Path, lex: dict, graph: list) -> tuple[dict, list]:
    path = run_root / "base-drift.json"
    if not path.exists():
        return lex, graph
    base = json.loads(path.read_text())
    lex = {k: [p for p in v if p not in set(base["lex"].get(k, []))] for k, v in lex.items()}
    graph = [g for g in graph if str(g)[:200] not in set(base["graph"])]
    return lex, graph


def graph_drift(vault: Path) -> list:
    """Graph drift over pages the graph indexes; Records item views are not among them."""
    from exomem import epistemic_graph, recall_policy

    out = []
    for item in epistemic_graph.graph_drift(vault) or []:
        path = item.get("path") if isinstance(item, dict) else None
        if path and not recall_policy.is_recall_candidate(vault, vault / path):
            continue
        out.append(item)
    return out


def torn_pages(vault: Path, paths: list[str]) -> list[str]:
    from exomem import vault as vault_module

    torn = []
    for rel in paths:
        p = vault / rel
        if p.exists():
            try:
                vault_module.parse_frontmatter(p.read_text(encoding="utf-8"), strict=True)
            except Exception:  # noqa: BLE001 - any parse failure counts as torn
                torn.append(rel)
    return torn


def _summary(lex: dict, graph: list) -> dict:
    return {"lex_missing": len(lex["missing"]), "lex_stale": len(lex["stale"]),
            "lex_orphaned": len(lex["orphaned"]), "graph_drift": len(graph)}


def check(args) -> None:
    vault = layout(args.run_root)["vault"]
    _ready(vault)
    recent = sorted((vault / "Knowledge Base" / "Notes").rglob("*.md"),
                    key=lambda p: p.stat().st_mtime_ns)[-50:]
    report = {"torn": torn_pages(vault, [p.relative_to(vault).as_posix() for p in recent])}
    lex, graph = _minus_baseline(args.run_root, lexical_drift(vault), graph_drift(vault))
    report["before"] = _summary(lex, graph)
    report["before_paths"] = {"lex": lex, "graph": [str(g)[:200] for g in graph[:20]]}
    started = time.perf_counter()
    _start_runtime(vault)
    report["recovery"] = []
    while True:
        elapsed = time.perf_counter() - started
        lex, graph = _minus_baseline(args.run_root, lexical_drift(vault), graph_drift(vault))
        point = {"t_s": round(elapsed, 1)} | _summary(lex, graph)
        report["recovery"].append(point)
        if sum(v for k, v in point.items() if k != "t_s") == 0 or elapsed > args.recover_seconds:
            break
        time.sleep(10)
    report["after"] = report["recovery"][-1]
    report["after_paths"] = {"lex": lex, "graph": [str(g)[:200] for g in graph[:20]]}
    if report["after"]["lex_missing"] + report["after"]["lex_stale"] + report["after"]["lex_orphaned"]:
        # Today's catalogue also repairs on a reader's first search; measure that
        # separately, so "needs repair" means still wrong after a read too.
        from exomem import lexstore

        lexstore.search_bm25(vault, "observations", 1, scope="kb", repair=True)
        lex, graph = _minus_baseline(args.run_root, lexical_drift(vault), graph_drift(vault))
        report["after_first_read"] = _summary(lex, graph)
        report["after_first_read_paths"] = {"lex": lex}
    # Today's own graph audit may not report a page the kill left without graph
    # rows; count the rows of the pages that were missing from the lexical index.
    from exomem import epistemic_graph

    if report["before_paths"]["lex"]["missing"]:
        con = sqlite3.connect(f"file:{epistemic_graph.sidecar_path(vault)}?mode=ro", uri=True)
        report["graph_rows_of_missing_pages"] = {
            rel: {"nodes": con.execute("SELECT count(*) FROM graph_nodes WHERE path = ?", (rel,)).fetchone()[0],
                  "edges": con.execute("SELECT count(*) FROM graph_edges WHERE source_path = ?", (rel,)).fetchone()[0]}
            for rel in report["before_paths"]["lex"]["missing"]}
        con.close()
    args.out.write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps({"before": report["before"], "after": report["after"],
                      "torn": len(report["torn"])}), flush=True)
    os._exit(0)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("run_root", type=Path)
    r.add_argument("stream", type=Path)
    r.add_argument("out_dir", type=Path)
    r.add_argument("--rate-per-min", type=float, default=60.0)
    r.add_argument("--max-ops", type=int, default=0)
    r.add_argument("--lag-timeout", type=float, default=60.0)
    b = sub.add_parser("baseline")
    b.add_argument("run_root", type=Path)
    b.add_argument("out", type=Path)
    c = sub.add_parser("check")
    c.add_argument("run_root", type=Path)
    c.add_argument("out", type=Path)
    c.add_argument("--recover-seconds", type=float, default=330.0)
    args = ap.parse_args()
    {"run": run, "check": check, "baseline": baseline_cmd}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
