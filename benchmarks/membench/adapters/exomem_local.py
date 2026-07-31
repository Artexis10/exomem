"""Exomem local adapter: leaf (in-process ops) and wire (in-process MCP) modes.

Isolation is safe by construction: ``EXOMEM_VAULT_PATH`` is mandatory with no
fallback, and this adapter always points it at a fresh benchmark-owned temp
vault. Determinism knobs are pinned explicitly and recorded in the profile;
a scored response carrying warming/degraded markers raises
:class:`AdapterEnvironmentError` (environment fault, never a contender loss).
Diagnostic logs go OUTSIDE the disposable vault so evidence survives cleanup.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from membench.adapters.base import (
    AdapterEnvironmentError,
    Capability,
    Hit,
    OpResult,
    Profile,
    StateExport,
    StateExportPage,
    register_adapter,
)
from membench.ids import sentinels_in

_NEUTRAL_SEARCH_KWARGS = {
    "scope": "kb",
    "detail": "full",
    "graph": False,
    "rerank": False,
    "prefer_compiled": False,
    "prefer_active": False,
    "prefer_used": False,
}

_PRODUCT_DEFAULT_SEARCH_KWARGS: dict[str, object] = {"scope": "kb", "detail": "full"}


def lexical_profile(name: str = "neutral-lexical") -> Profile:
    """Model-free profile: deterministic, embeddings off, backends pinned."""

    return Profile(
        name=name,
        settings={
            "EXOMEM_DISABLE_EMBEDDINGS": "1",
            "EXOMEM_DISABLE_WARMUP": "1",
            "EXOMEM_DISABLE_FILE_WATCHER": "1",
            "EXOMEM_DISABLE_MODE_WATCH": "1",
            "EXOMEM_DISABLE_RELEVANCE_CHECK": "1",
            "EXOMEM_DISABLE_RANKING_CONFIG": "1",
            "EXOMEM_DISABLE_CORPUS_CACHE": "1",
            "EXOMEM_DISABLE_MEDIA_EXTRACTION": "1",
            "EXOMEM_DISABLE_CLIP": "1",
            "EXOMEM_DISABLE_QUERY_LOG": "1",
            "EXOMEM_VEC_BACKEND": "numpy",
            "EXOMEM_LEXICAL_BACKEND": "python",
        },
    )


class ExomemLocalAdapter:
    """Drives exomem through public boundaries only (op leaves or MCP tools)."""

    name = "exomem-local"
    supports_group_reuse = False

    def __init__(self, *, mode: str = "leaf", search_style: str = "neutral") -> None:
        if mode not in {"leaf", "wire"}:
            raise ValueError(f"unknown mode {mode!r}")
        if search_style not in {"neutral", "product-default"}:
            raise ValueError(f"unknown search_style {search_style!r}")
        self.mode = mode
        self.search_style = search_style
        self._workdir: Path | None = None
        self._vault: Path | None = None
        self._schema: object | None = None
        self._mcp: object | None = None
        self._saved_env: dict[str, str | None] = {}
        self._profile: Profile | None = None

    # -- lifecycle --------------------------------------------------------
    def capabilities(self) -> frozenset[Capability]:
        return frozenset(
            {Capability.INGEST_API, Capability.SEARCH, Capability.STATE_EXPORT}
        )

    def _set_env(self, values: dict[str, str]) -> None:
        for key, value in values.items():
            self._saved_env.setdefault(key, os.environ.get(key))
            os.environ[key] = value

    def setup(self, workdir: Path, profile: Profile) -> None:
        workdir = Path(workdir)
        self._workdir = workdir
        self._profile = profile
        vault = workdir / "vault"
        vault.mkdir(parents=True, exist_ok=True)
        logs = workdir / "logs"  # outside the vault: evidence survives cleanup
        logs.mkdir(parents=True, exist_ok=True)
        self._set_env(
            {
                "EXOMEM_VAULT_PATH": str(vault),
                "EXOMEM_CONFIG_PATH": str(workdir / "exomem-config.json"),
                "EXOMEM_WRITER_LEASE_STATE_DIR": str(workdir / "leases"),
                "EXOMEM_LOG_DIR": str(logs),
                **profile.settings,
            }
        )
        from exomem import embeddings as embeddings_module
        from exomem import find as find_module
        from exomem.init import init_vault
        from exomem.schema import load_source_schema

        init_vault(vault)
        self._vault = vault
        self._schema = load_source_schema(vault)
        find_module.clear_cache()
        embeddings_module.clear_embedding_indexes()
        if self.mode == "wire":
            from exomem.server import build_server

            self._mcp = build_server(require_auth=False)

    def cleanup(self) -> None:
        for key, previous in self._saved_env.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
        self._saved_env.clear()
        self._mcp = None
        try:
            from exomem import find as find_module

            find_module.clear_cache()
        except Exception:  # pragma: no cover - cleanup best effort
            pass

    # -- wire helper ------------------------------------------------------
    def _call_tool(self, tool: str, args: dict) -> dict:
        import asyncio

        result = asyncio.run(self._mcp.call_tool(tool, args, run_middleware=False))  # type: ignore[union-attr]
        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict):
            return structured
        for content in getattr(result, "content", []) or []:
            text = getattr(content, "text", None)
            if text:
                return json.loads(text)
        return {}

    # -- ingest -----------------------------------------------------------
    def ingest(self, corpus_dir: Path, native_dir: Path) -> list[OpResult]:
        ops_file = Path(native_dir) / "capture-ops.jsonl"
        if not ops_file.is_file():
            raise AdapterEnvironmentError(f"missing native op stream: {ops_file}")
        from exomem import commands, find as find_module

        results: list[OpResult] = []
        for line in ops_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            op = json.loads(line)
            started = time.perf_counter()
            try:
                if self.mode == "wire":
                    payload = self._call_tool(
                        "capture_source",
                        {
                            "content": op["content"],
                            "title": op["title"],
                            "source_type": op.get("source_type", "other"),
                        },
                    )
                    ok = bool(payload) and not payload.get("error")
                    detail = None if ok else json.dumps(payload.get("error"))
                else:
                    commands.op_capture_source(
                        self._vault,
                        self._schema,
                        content=op["content"],
                        title=op["title"],
                        source_type=op.get("source_type", "other"),
                    )
                    ok, detail = True, None
            except Exception as exc:  # recorded, stays in denominators
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            results.append(
                OpResult(
                    seq=int(op.get("seq", len(results))),
                    op=str(op.get("op", "capture_source")),
                    source_id=op.get("source_id"),
                    ok=ok,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    detail=detail,
                )
            )
        find_module.clear_cache()
        return results

    # -- search -----------------------------------------------------------
    def _search_kwargs(self) -> dict:
        base = dict(
            _NEUTRAL_SEARCH_KWARGS
            if self.search_style == "neutral"
            else _PRODUCT_DEFAULT_SEARCH_KWARGS
        )
        # Always hybrid: exomem's `keyword` mode is strict phrase-substring
        # matching, so natural-language questions never match (the root cause
        # of the historical Track-A zero-hits run). Hybrid's BM25 lane is
        # tokenized and degrades cleanly when embeddings are disabled.
        base["mode"] = "hybrid"
        return base

    @staticmethod
    def _hit_field(hit: object, key: str) -> object:
        if isinstance(hit, dict):
            return hit.get(key)
        return getattr(hit, key, None)

    def _normalize(self, payload: object) -> list[object]:
        if isinstance(payload, dict):
            for marker in ("warming", "degraded"):
                value = payload.get(marker)
                if value:
                    raise AdapterEnvironmentError(
                        f"scored response carries {marker} marker: {value!r}"
                    )
            for key in ("hits", "result", "results"):
                if isinstance(payload.get(key), list):
                    return payload[key]
            return []
        if isinstance(payload, list):
            return payload
        for marker in ("warming", "degraded"):
            value = getattr(payload, marker, None)
            if value:
                raise AdapterEnvironmentError(
                    f"scored response carries {marker} marker: {value!r}"
                )
        hits = getattr(payload, "hits", None)
        return list(hits) if hits is not None else []

    def search(self, query: str, limit: int) -> list[Hit]:
        kwargs = self._search_kwargs()
        if self.mode == "wire":
            payload: object = self._call_tool(
                "ask_memory", {"query": query, "limit": limit, **kwargs}
            )
        else:
            from exomem import commands

            payload = commands.op_ask_memory(self._vault, query=query, limit=limit, **kwargs)
        hits: list[Hit] = []
        for rank, raw_hit in enumerate(self._normalize(payload), start=1):
            path = self._hit_field(raw_hit, "path")
            if not isinstance(path, str):
                continue
            text = ""
            candidate = (self._vault or Path(".")) / path
            if candidate.is_file():
                text = candidate.read_text(encoding="utf-8", errors="replace")
            excerpt = self._hit_field(raw_hit, "excerpt")
            title = self._hit_field(raw_hit, "title")
            raw = raw_hit if isinstance(raw_hit, dict) else {"path": path, "title": title}
            hits.append(
                Hit(
                    rank=rank,
                    provider_path=path,
                    title=title if isinstance(title, str) else None,
                    excerpt=excerpt if isinstance(excerpt, str) else None,
                    sentinels=tuple(sentinels_in(text or (excerpt or ""))),
                    raw=raw,
                    text=text or None,
                )
            )
        return hits

    # -- state export ------------------------------------------------------
    def export_state(self) -> StateExport:
        if self._vault is None:
            raise AdapterEnvironmentError("adapter not set up")
        pages = []
        for path in sorted(self._vault.rglob("*.md")):
            relative = path.relative_to(self._vault).as_posix()
            pages.append(
                StateExportPage(
                    path=relative, text=path.read_text(encoding="utf-8", errors="replace")
                )
            )
        return StateExport(pages=tuple(pages))

    def version_info(self) -> dict[str, str]:
        import exomem

        info = {
            "provider": self.name,
            "mode": self.mode,
            "search_style": self.search_style,
            "exomem_version": getattr(exomem, "__version__", "unknown"),
        }
        if self._profile is not None:
            info["profile"] = self._profile.name
        return info


register_adapter("exomem-local", lambda **kw: ExomemLocalAdapter(**kw))
