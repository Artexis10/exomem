"""Durable structured logs of find() queries and write events.

Two JSONL files under the repo `logs/` dir (already gitignored via `logs/*`,
NSSM-rotated neighborhood, NEVER Obsidian-synced — query text can name sensitive
Evidence scopes, so it stays on the box at the same trust boundary as
`logs/kb-mcp.log`):

- `logs/queries.jsonl` : one object per find() call (query + ranking signals)
- `logs/writes.jsonl`  : one object per note/add/replace write (path + citations)

These feed the offline feedback loop (`scripts/derive_relevance_pairs.py`), which
mines weak `(query -> cited_path)` relevance labels to grow the eval golden set.
We log ONLY paths + the per-hit `signals` dict, never excerpts or bodies — that's
the bloat trap.

Everything here is best-effort: any failure is swallowed so logging can NEVER
break a tool call. No-op when `KB_MCP_DISABLE_EMBEDDINGS` (so the test suite stays
clean) or `KB_MCP_DISABLE_QUERY_LOG` (an explicit ops opt-out) is set.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
QUERIES_PATH = _LOG_DIR / "queries.jsonl"
WRITES_PATH = _LOG_DIR / "writes.jsonl"
READS_PATH = _LOG_DIR / "reads.jsonl"


def _disabled() -> bool:
    return bool(
        os.environ.get("KB_MCP_DISABLE_EMBEDDINGS")
        or os.environ.get("KB_MCP_DISABLE_QUERY_LOG")
    )


def _append(path: Path, obj: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001 — logging must never raise
        log.debug("query_log append to %s failed: %s", path.name, e)


def _now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def log_find_call(
    *,
    query: str,
    mode: str,
    scope: str,
    types: list[str] | None,
    projects: list[str] | None,
    tags: list[str] | None,
    limit: int,
    rerank: bool,
    prefer_compiled: bool,
    graph: bool,
    hits: list[Any],
) -> None:
    """Append one structured record for a find() call. Best-effort."""
    if _disabled():
        return
    try:
        top_k = []
        for h in hits:
            d = h.as_dict()
            top_k.append(
                {
                    "path": d.get("path"),
                    "type": d.get("type"),
                    "signals": d.get("signals", {}),
                }
            )
        _append(
            QUERIES_PATH,
            {
                "ts": _now_iso(),
                "query": query,
                "mode": mode,
                "scope": scope,
                "filters": {"types": types, "projects": projects, "tags": tags},
                "limit": limit,
                "rerank": rerank,
                "prefer_compiled": prefer_compiled,
                "graph": graph,
                "n_results": len(hits),
                "top_k": top_k,
            },
        )
    except Exception as e:  # noqa: BLE001
        log.debug("log_find_call failed: %s", e)


def log_write_call(
    *, tool: str, written_path: str | None, cited_sources: list[str] | None
) -> None:
    """Append one structured record for a note/add/replace write. Best-effort."""
    if _disabled():
        return
    try:
        _append(
            WRITES_PATH,
            {
                "ts": _now_iso(),
                "tool": tool,
                "written_path": written_path,
                "cited_sources": list(cited_sources or []),
            },
        )
    except Exception as e:  # noqa: BLE001
        log.debug("log_write_call failed: %s", e)


def log_get_call(
    *,
    read_path: str,
    frontmatter_only: bool = False,
    include_history: bool = False,
) -> None:
    """Append one structured record for a get() read. Best-effort.

    Feeds the ACT-R dormancy signal in the stale_review audit (a read is a
    stronger recency signal than a find-surfacing). Logs ONLY the canonical
    path, never the body.
    """
    if _disabled():
        return
    try:
        _append(
            READS_PATH,
            {
                "ts": _now_iso(),
                "tool": "get",
                "read_path": read_path,
                "frontmatter_only": frontmatter_only,
                "include_history": include_history,
            },
        )
    except Exception as e:  # noqa: BLE001
        log.debug("log_get_call failed: %s", e)
