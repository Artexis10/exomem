"""Track-A bridge: expose ANY membench MemoryAdapter as an upstream
``basic_memory_benchmarks`` BenchmarkProvider.

The upstream package is NOT a dependency here: nothing from it is imported at
module top. The upstream ``SearchHit`` type and ``ProviderSkippedError`` are
resolved lazily at call time, and both are injectable (``hit_factory`` /
``skip_exception``) so conformance tests run with local stand-in types when
the package is absent. Any object accepting the upstream keyword shape
``{id, source_doc_id, source_path, text, score, metadata}`` works.

Upstream corpora are plain directories of Markdown documents. ``ingest`` walks
them (sorted, recursive) and synthesizes BOTH known membench native streams —
``capture-ops.jsonl`` (exomem shape) and ``captures.jsonl`` (graybox shape) —
so any inner adapter finds the stream it consumes. Hit mapping mirrors the
sibling provider's ``_row_to_hit``: vault prefixes are stripped from
``source_path``, and ``source_doc_id`` prefers the membench sentinel/source id
carried by the hit, falling back to the basename stem.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

from membench.adapters.base import (
    AdapterUnsupported,
    Hit,
    MemoryAdapter,
    Profile,
)

_VAULT_PREFIXES = (
    "Knowledge Base/Benchmark Corpus/",
    "Knowledge Base/",
)


def _default_hit_factory() -> Callable[..., Any]:
    try:
        from basic_memory_benchmarks.models import SearchHit  # type: ignore

        return SearchHit
    except ImportError:
        from types import SimpleNamespace

        return SimpleNamespace


def _default_skip_exception() -> type[Exception]:
    try:
        from basic_memory_benchmarks.exceptions import (  # type: ignore
            ProviderSkippedError,
        )

        return ProviderSkippedError
    except ImportError:
        return AdapterUnsupported


def _doc_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or fallback
        if stripped:
            return stripped[:80]
    return fallback


class TrackABridge:
    """Duck-typed upstream BenchmarkProvider over one membench adapter."""

    supports_group_reuse = False

    def __init__(
        self,
        adapter: MemoryAdapter,
        *,
        profile: Profile | None = None,
        hit_factory: Callable[..., Any] | None = None,
        skip_exception: type[Exception] | None = None,
        workdir_root: Path | None = None,
    ) -> None:
        self.adapter = adapter
        self.name = f"membench-{adapter.name}"
        self.profile = profile or Profile(name=f"bridge-{adapter.name}")
        self._hit_factory = hit_factory or _default_hit_factory()
        self._skip_exception = skip_exception or _default_skip_exception()
        self._workdir_root = Path(workdir_root) if workdir_root is not None else None
        self._workdir: Path | None = None

    # -- upstream protocol -------------------------------------------------
    def ingest(self, corpus_path: Path, run_config: Any) -> None:
        corpus_path = Path(corpus_path)
        docs = sorted(
            (p for p in corpus_path.rglob("*.md") if p.is_file()),
            key=lambda p: p.relative_to(corpus_path).as_posix(),
        )
        if not docs:
            raise ValueError(f"no Markdown documents under {corpus_path}")

        self._workdir = Path(
            tempfile.mkdtemp(prefix="bridge-", dir=self._workdir_root)
        )
        try:
            self.adapter.setup(self._workdir / "provider", self.profile)
        except AdapterUnsupported as exc:
            raise self._skip_exception(str(exc)) from exc

        native_dir = self._workdir / "native"
        native_dir.mkdir(parents=True, exist_ok=True)
        capture_ops: list[str] = []
        captures: list[str] = []
        for seq, doc in enumerate(docs):
            text = doc.read_text(encoding="utf-8", errors="replace")
            doc_id = doc.stem
            title = _doc_title(text, doc_id)
            capture_ops.append(
                json.dumps(
                    {
                        "seq": seq,
                        "op": "capture_source",
                        "source_id": doc_id,
                        "title": title,
                        "content": text,
                        "source_type": "other",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            captures.append(
                json.dumps(
                    {
                        "week": 0,
                        "source_id": doc_id,
                        "text": f"{title}\n\n{text}",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        (native_dir / "capture-ops.jsonl").write_text(
            "\n".join(capture_ops) + "\n", encoding="utf-8"
        )
        (native_dir / "captures.jsonl").write_text(
            "\n".join(captures) + "\n", encoding="utf-8"
        )

        try:
            self.adapter.ingest(corpus_path, native_dir)
        except AdapterUnsupported as exc:
            raise self._skip_exception(str(exc)) from exc

    @staticmethod
    def _strip_vault_prefix(path: str) -> str:
        for prefix in _VAULT_PREFIXES:
            if path.startswith(prefix):
                return path[len(prefix) :]
        return path

    def _to_upstream_hit(self, hit: Hit) -> Any:
        source_path = self._strip_vault_prefix(hit.provider_path)
        basename = source_path.rstrip("/").rsplit("/", 1)[-1]
        stem = basename[:-3] if basename.endswith(".md") else basename
        source_doc_id = hit.sentinels[0] if hit.sentinels else stem
        raw_score = hit.raw.get("score") if isinstance(hit.raw, dict) else None
        metadata: dict[str, Any] = {"rank": hit.rank, "sentinels": list(hit.sentinels)}
        if hit.title is not None:
            metadata["title"] = hit.title
        return self._hit_factory(
            id=hit.provider_path,
            source_doc_id=source_doc_id,
            source_path=source_path,
            text=hit.excerpt or hit.text,
            score=float(raw_score) if raw_score is not None else None,
            metadata=metadata,
        )

    def search(self, query: str, limit: int, run_config: Any) -> list[Any]:
        try:
            hits = self.adapter.search(query, limit)
        except AdapterUnsupported as exc:
            raise self._skip_exception(str(exc)) from exc
        return [self._to_upstream_hit(hit) for hit in hits]

    def cleanup(self, run_config: Any) -> None:
        try:
            self.adapter.cleanup()
        finally:
            if self._workdir is not None:
                shutil.rmtree(self._workdir, ignore_errors=True)
                self._workdir = None

    def version_info(self) -> dict[str, str]:
        info = dict(self.adapter.version_info())
        info["bridge"] = "membench-track-a"
        return info
