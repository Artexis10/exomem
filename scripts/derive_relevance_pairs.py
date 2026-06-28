"""Mine weak (query -> relevant path) labels from real usage — the feedback loop.

Joins `logs/queries.jsonl` x `logs/writes.jsonl`: when a note/replace write cites
a path shortly AFTER a find() for some query, and that cited path appeared in the
query's results, then `(query -> cited_path)` is a weak-but-real relevance label —
the search surfaced something the user then actually used. These accumulate for free
from ordinary search-then-compile usage and are the only compounding relevance
signal a single-user vault produces.

Output:
- `logs/relevance_pairs.jsonl` — the derived pairs, rewritten as an ATOMIC,
  DEDUPED SNAPSHOT each run (not appended). It is a pure derived artifact of
  `queries × writes × window`, so re-running over the same logs is byte-identical
  and idempotent — safe to run on a schedule. The source logs are the audit trail.
- A PROPOSED YAML block printed to stdout for additions to tests/golden/queries.yaml.
  We never auto-edit the golden set (visibility over gating): the user confirms and
  pastes. Constants are never auto-tuned from these.

Usage:
    uv run python scripts/derive_relevance_pairs.py
    uv run python scripts/derive_relevance_pairs.py --window-hours 6 --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
LOGS = HERE.parent / "logs"
QUERIES = LOGS / "queries.jsonl"
WRITES = LOGS / "writes.jsonl"
PAIRS_OUT = LOGS / "relevance_pairs.jsonl"
GOLDEN = HERE.parent / "tests" / "golden" / "queries.yaml"


def _canon(path: str) -> str:
    p = (path or "").strip().replace("\\", "/")
    if p.lower().endswith(".md"):
        p = p[:-3]
    if p.startswith("Knowledge Base/"):
        p = p[len("Knowledge Base/"):]
    return p.lower()


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _parse_ts(ts: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _existing_golden_queries(path: Path) -> set[str]:
    if not path.exists():
        return set()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return {e["query"].strip().lower() for e in raw if e.get("query")}


def derive_pairs(
    queries: list[dict], writes: list[dict], window_seconds: float
) -> list[dict]:
    """Return deduped (query, cited_path) pairs with a confidence score."""
    best: dict[tuple[str, str], dict] = {}
    for w in writes:
        if w.get("tool") not in ("note", "replace"):
            continue
        w_ts = _parse_ts(w.get("ts", ""))
        cited = {_canon(c) for c in (w.get("cited_sources") or []) if c}
        if not (w_ts and cited):
            continue
        for q in queries:
            q_ts = _parse_ts(q.get("ts", ""))
            if q_ts is None:
                continue
            delta = (w_ts - q_ts).total_seconds()
            if not (0 <= delta <= window_seconds):
                continue  # query must precede the write, within the window
            ranks = {
                _canon(t.get("path", "")): rank
                for rank, t in enumerate(q.get("top_k") or [], start=1)
                if t.get("path")
            }
            for c in cited:
                rank = ranks.get(c)
                if rank is None:
                    continue  # cited path wasn't in this query's results
                # Confidence: rank position (1/rank) decayed by time distance.
                recency = max(0.0, 1.0 - delta / window_seconds)
                conf = round((1.0 / rank) * (0.5 + 0.5 * recency), 4)
                key = (q["query"], c)
                prev = best.get(key)
                if prev is None or conf > prev["confidence"]:
                    best[key] = {
                        "query": q["query"],
                        "cited_path": c,
                        "confidence": conf,
                        "rank_in_results": rank,
                        "delta_seconds": round(delta, 1),
                        "via_write": w.get("written_path"),
                        "source": "note-citation",
                    }
    # Secondary keys (query, cited_path) make the order fully deterministic so the
    # snapshot is byte-identical across runs regardless of dict iteration order.
    return sorted(
        best.values(), key=lambda p: (-p["confidence"], p["query"], p["cited_path"])
    )


def _atomic_write_jsonl(path: Path, rows: list[dict]) -> None:
    """Write `rows` as JSONL to `path` atomically (tmp sibling + os.replace).

    A concurrent reader sees the whole old or whole new file, never a torn one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def mine_pairs(
    window_seconds: float, *, write: bool, logs_dir: Path = LOGS
) -> list[dict]:
    """Mine deduped (query -> cited_path) pairs from the usage logs under `logs_dir`.

    Reads `queries.jsonl` + `writes.jsonl`, derives weak relevance pairs, and —
    when `write` — rewrites `relevance_pairs.jsonl` as an ATOMIC, DEDUPED SNAPSHOT
    (overwrite, not append) so re-running over the same logs is idempotent and
    byte-identical. Returns the pairs. This is the reusable seam the auto-tuner
    imports to mine fresh before each tune.
    """
    queries = _read_jsonl(logs_dir / QUERIES.name)
    writes = _read_jsonl(logs_dir / WRITES.name)
    pairs = derive_pairs(queries, writes, window_seconds)
    if write:
        _atomic_write_jsonl(logs_dir / PAIRS_OUT.name, pairs)
    return pairs


def _propose_golden(pairs: list[dict], existing: set[str]) -> dict[str, list[str]]:
    """Group new (not-yet-in-golden) queries -> their relevant paths."""
    proposed: dict[str, list[str]] = {}
    for p in pairs:
        if p["query"].strip().lower() in existing:
            continue
        proposed.setdefault(p["query"], [])
        full = "Knowledge Base/" + p["cited_path"]
        if full not in proposed[p["query"]]:
            proposed[p["query"]].append(full)
    return proposed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--window-hours", type=float, default=2.0,
                    help="max gap between a find() and a citing write (default 2h)")
    ap.add_argument("--dry-run", action="store_true",
                    help="don't write relevance_pairs.jsonl; just print")
    args = ap.parse_args()

    queries = _read_jsonl(QUERIES)
    writes = _read_jsonl(WRITES)
    if not queries or not writes:
        print(
            f"need both logs with data: queries.jsonl={len(queries)} rows, "
            f"writes.jsonl={len(writes)} rows. Use the connector a bit first.",
            file=sys.stderr,
        )
        return 1

    pairs = mine_pairs(args.window_hours * 3600, write=not args.dry_run)
    print(f"derived {len(pairs)} (query -> cited_path) relevance pairs "
          f"from {len(queries)} queries x {len(writes)} writes")

    if not args.dry_run:
        print(f"wrote snapshot ({len(pairs)} pairs) -> {PAIRS_OUT}")

    proposed = _propose_golden(pairs, _existing_golden_queries(GOLDEN))
    if proposed:
        print(f"\n=== PROPOSED golden additions ({len(proposed)} new queries) ===")
        print("# Review, then paste confirmed entries into tests/golden/queries.yaml:\n")
        block = [
            {"query": q, "expect_any_of": paths} for q, paths in proposed.items()
        ]
        print(yaml.safe_dump(block, sort_keys=False, allow_unicode=True))
    else:
        print("\nno new golden queries to propose (all already covered).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
