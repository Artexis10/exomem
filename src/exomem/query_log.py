"""Durable structured logs of find() queries and write events.

Two JSONL files under the resolved log dir (`logging_config.resolve_log_dir()`
— the repo `logs/` dir in a source checkout, gitignored via `logs/*`,
NSSM-rotated neighborhood; a per-platform location for a packaged install —
NEVER Obsidian-synced — query text can name sensitive Evidence scopes, so it
stays on the box at the same trust boundary as `exomem.log`):

- `logs/queries.jsonl` : one object per find() call (query + ranking signals)
- `logs/writes.jsonl`  : one object per note/add/replace write (path + citations)
- `logs/activations.jsonl` : one object per activate_context call (door, client,
  outcome, counts) -- never the turn, and read by nothing in `usage`

These feed the offline feedback loop (`scripts/derive_relevance_pairs.py`), which
mines weak `(query -> cited_path)` relevance labels to grow the eval golden set.
We log ONLY paths + the per-hit `signals` dict, never excerpts or bodies — that's
the bloat trap.

Everything here is best-effort: any failure is swallowed so logging can NEVER
break a tool call. No-op when `EXOMEM_DISABLE_EMBEDDINGS` (so the test suite stays
clean) or `EXOMEM_DISABLE_QUERY_LOG` (an explicit ops opt-out) is set.

Additive correlation fields (`request_id`, `ts_utc`, `outcome`, `error_code`,
`duration_ms`) ride alongside the original fields on every record — `ts`
keeps its exact original local-naive semantics for `usage.py` untouched.
Each file rotates at `EXOMEM_JSONL_MAX_MB` (default 64MB), keeping exactly one
`.jsonl.1` prior generation; `usage.read_jsonl` reads that generation too.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .logging_config import resolve_log_dir

log = logging.getLogger(__name__)

# Module-level defaults (patchable in tests). $EXOMEM_LOG_DIR is consulted
# PER CALL via `_target`/`current_log_dir` — never frozen at import — so a
# container or test can flip the env var without reloading the module. The
# constant below, by contrast, freezes `resolve_log_dir()`'s ENTIRE answer at
# import — its env branch included, not just the unset fallback — so when
# EXOMEM_LOG_DIR is already exported at import time this holds that env value
# too, and only a module reload re-derives it. It is the SAME resolution
# `logging_config.resolve_log_dir()` uses for every other log file, so
# queries.jsonl/writes.jsonl/reads.jsonl always stay co-located with
# exomem.log/exomem-cli.log/exomem-media.log — a bare `<repo>/logs` guess here
# previously left these three behind on a wheel install once
# EXOMEM_LOG_DIR-unset resolution stopped assuming a checkout (issue #552).
_LOG_DIR = resolve_log_dir()
QUERIES_PATH = _LOG_DIR / "queries.jsonl"
WRITES_PATH = _LOG_DIR / "writes.jsonl"
READS_PATH = _LOG_DIR / "reads.jsonl"
ACTIVATIONS_PATH = _LOG_DIR / "activations.jsonl"

#: A declared client label: software, not a person, and short enough to be one.
CLIENT_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
SESSION_MAX_CHARS = 256
_ACTIVATION_ANCHORS_MAX = 8

_DEFAULT_JSONL_MAX_MB = 64.0
# In-memory running size estimate per path, updated cheaply on every append;
# a real stat() only happens every `_STAT_RESYNC_EVERY` appends (to seed the
# estimate and correct drift), so the hot append path pays a syscall roughly
# once per 100 calls instead of once per call.
_STAT_RESYNC_EVERY = 100
_size_cache: dict[str, int] = {}
_append_counts: dict[str, int] = {}


def current_log_dir() -> Path:
    """Per-call log dir: $EXOMEM_LOG_DIR when set, else the module default."""
    env = os.environ.get("EXOMEM_LOG_DIR", "").strip()
    if env:
        return Path(env)
    return _LOG_DIR


def _target(default_path: Path, filename: str) -> Path:
    """Where a record lands: $EXOMEM_LOG_DIR/<filename> when the env var is
    set, else the module-level default path (which tests monkeypatch)."""
    env = os.environ.get("EXOMEM_LOG_DIR", "").strip()
    if env:
        return Path(env) / filename
    return default_path


def _disabled() -> bool:
    return bool(
        os.environ.get("EXOMEM_DISABLE_EMBEDDINGS")
        or os.environ.get("EXOMEM_DISABLE_QUERY_LOG")
    )


def _jsonl_max_bytes() -> int:
    raw = os.environ.get("EXOMEM_JSONL_MAX_MB", "").strip()
    try:
        mb = float(raw) if raw else _DEFAULT_JSONL_MAX_MB
    except ValueError:
        mb = _DEFAULT_JSONL_MAX_MB
    return max(1, int(mb * 1024 * 1024))


def _rotate_if_needed(path: Path) -> None:
    """Rotate `path` -> one `.jsonl.1` generation when it exceeds the size
    cap, using the cheap running size estimate described in the module
    docstring."""
    key = str(path)
    count = _append_counts.get(key, 0) + 1
    _append_counts[key] = count
    if key not in _size_cache or count % _STAT_RESYNC_EVERY == 0:
        try:
            _size_cache[key] = path.stat().st_size
        except OSError:
            _size_cache[key] = 0
    if _size_cache[key] < _jsonl_max_bytes():
        return
    try:
        rotated = path.with_name(path.name + ".1")
        os.replace(path, rotated)
    except OSError:
        pass
    _size_cache[key] = 0


def _append(path: Path, obj: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path)
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
        key = str(path)
        _size_cache[key] = _size_cache.get(key, 0) + len(line.encode("utf-8"))
    except Exception as e:  # noqa: BLE001 — logging must never raise
        log.debug("query_log append to %s failed: %s", path.name, e)


def _now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _correlation_fields(
    *, outcome: str, error_code: str | None, duration_ms: float | None
) -> dict[str, Any]:
    """Additive fields only — never touch the original `ts` field's shape."""
    from .command_surface import peek_request_id

    fields: dict[str, Any] = {
        "request_id": peek_request_id(),
        "ts_utc": dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds"),
        "outcome": outcome,
    }
    if error_code is not None:
        fields["error_code"] = error_code
    if duration_ms is not None:
        fields["duration_ms"] = duration_ms
    return fields


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
    timing_summary: dict | None = None,
    prefer_used: bool = False,
    outcome: str = "success",
    error_code: str | None = None,
    duration_ms: float | None = None,
) -> None:
    """Append one structured record for a find() call. Best-effort.

    `timing_summary` (present only when the caller opted into `find` timing
    diagnostics) carries total/cache/per-stage milliseconds ONLY — the
    paths-and-signals-never-content rule above applies to it too.
    """
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
        record = {
            "ts": _now_iso(),
            "query": query,
            "mode": mode,
            "scope": scope,
            "filters": {"types": types, "projects": projects, "tags": tags},
            "limit": limit,
            "rerank": rerank,
            "prefer_compiled": prefer_compiled,
            "prefer_used": prefer_used,
            "graph": graph,
            "n_results": len(hits),
            "top_k": top_k,
            **_correlation_fields(outcome=outcome, error_code=error_code, duration_ms=duration_ms),
        }
        if timing_summary:
            record["timings"] = timing_summary
        _append(_target(QUERIES_PATH, "queries.jsonl"), record)
    except Exception as e:  # noqa: BLE001
        log.debug("log_find_call failed: %s", e)


def log_write_call(
    *,
    tool: str,
    written_path: str | None,
    cited_sources: list[str] | None,
    outcome: str = "success",
    error_code: str | None = None,
    duration_ms: float | None = None,
) -> None:
    """Append one structured record for a note/add/replace write. Best-effort."""
    if _disabled():
        return
    try:
        _append(
            _target(WRITES_PATH, "writes.jsonl"),
            {
                "ts": _now_iso(),
                "tool": tool,
                "written_path": written_path,
                "cited_sources": list(cited_sources or []),
                **_correlation_fields(
                    outcome=outcome, error_code=error_code, duration_ms=duration_ms
                ),
            },
        )
    except Exception as e:  # noqa: BLE001
        log.debug("log_write_call failed: %s", e)


def log_get_call(
    *,
    read_path: str,
    frontmatter_only: bool = False,
    include_history: bool = False,
    outcome: str = "success",
    error_code: str | None = None,
    duration_ms: float | None = None,
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
            _target(READS_PATH, "reads.jsonl"),
            {
                "ts": _now_iso(),
                "tool": "get",
                "read_path": read_path,
                "frontmatter_only": frontmatter_only,
                "include_history": include_history,
                **_correlation_fields(
                    outcome=outcome, error_code=error_code, duration_ms=duration_ms
                ),
            },
        )
    except Exception as e:  # noqa: BLE001
        log.debug("log_get_call failed: %s", e)


def declared_client(value: object) -> str | None:
    """A declared client label as recorded: the label, `"invalid"`, or `None`.

    Invalid means ignored, never refused -- a turn is never failed over
    attribution -- and never echoed, so a caller cannot write arbitrary text
    into the log through it.
    """
    if value is None or value == "":
        return None
    return value if isinstance(value, str) and CLIENT_LABEL_RE.fullmatch(value) else "invalid"


def _session_hash(vault_root: Path, session: object) -> str | None:
    """Stable per vault, unlinkable across vaults, and never the raw value.

    Keyed by the vault's activation-sidecar identity, or its resolved path
    when there is no sidecar, so the same conversation key hashes differently
    in two vaults.
    """
    if session is None or session == "":
        return None
    if not isinstance(session, str) or len(session) > SESSION_MAX_CHARS:
        return "invalid"
    from . import working_set_runtime

    try:
        identity = working_set_runtime.identity_for(Path(vault_root))
    except Exception:  # noqa: BLE001 - attribution never fails the call
        identity = ""
    identity = identity or str(Path(vault_root).resolve())
    return hashlib.sha256(f"{identity}\0{session}".encode("utf-8", "surrogatepass")).hexdigest()[:16]


def log_activation_call(
    vault_root: Path,
    *,
    packet: Mapping[str, Any] | None,
    client: object = None,
    session: object = None,
    outcome: str | None = None,
    error_code: str | None = None,
    duration_ms: float | None = None,
) -> None:
    """Append one host-local row for an activate_context call. Best-effort.

    Recorded: the door, the observed MCP client (which wins for any use) and
    the declared label, the transport, a vault-keyed session hash, a principal
    hash, the outcome and abstention reason, how the packet was carried, the
    resolved anchor refs (omitted in content-private hosted mode), the recent
    entries counted per reason, continuity, and whether `episode_due` rode
    along. Never recorded: the turn or any hash of it, unit, statement or
    title text, the continuity token, the raw session, or any identity beyond
    a hash. Nothing in `usage` reads this file.
    """
    if _disabled():
        return
    try:
        from . import command_surface, privacy_log
        from .governance.principal import OWNER_AUDIENCE, effective_principal

        principal = effective_principal()
        audience = str(principal.audience_id or "")
        observed = command_surface.mcp_caller_identity()
        declared = declared_client(client)
        body = packet if isinstance(packet, Mapping) else {}
        generation = body.get("generation") if isinstance(body.get("generation"), Mapping) else {}
        abstention = body.get("abstention") if isinstance(body.get("abstention"), Mapping) else {}
        recent: dict[str, int] = {}
        for entry in body.get("recent_context") or ():
            if isinstance(entry, Mapping):
                why = str(entry.get("why") or "")
                recent[why] = recent.get(why, 0) + 1
        if outcome is None:
            outcome = "abstained" if body.get("abstained") else "served"
        record: dict[str, Any] = {
            "ts": _now_iso(),
            **_correlation_fields(outcome=outcome, error_code=error_code, duration_ms=duration_ms),
            "door": principal.surface,
            "client": observed.get("client_name")
            or (declared if declared not in (None, "invalid") else None),
            "client_observed": observed.get("client_name"),
            "client_declared": declared,
            "transport": observed.get("transport"),
            "session_hash": _session_hash(vault_root, session),
            "principal_hash": (
                "owner"
                if audience == OWNER_AUDIENCE
                else hashlib.sha256(audience.encode("utf-8", "surrogatepass")).hexdigest()[:16]
            ),
            "abstention_reason": abstention.get("reason") if body.get("abstained") else None,
            "carried_by": generation.get("carried_by"),
            "recent": recent,
            "continuity": generation.get("continuity"),
            "episode_due": "episode_due" in body,
        }
        if not privacy_log.content_private_logging_enabled():
            record["anchors"] = [
                str(anchor.get("ref") or "")
                for anchor in (body.get("anchors") or ())
                if isinstance(anchor, Mapping) and anchor.get("status") == "resolved"
            ][:_ACTIVATION_ANCHORS_MAX]
        _append(_target(ACTIVATIONS_PATH, "activations.jsonl"), record)
    except Exception as e:  # noqa: BLE001
        log.debug("log_activation_call failed: %s", e)
