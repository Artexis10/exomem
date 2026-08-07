"""Gray Box local adapter: raw-inbox altitude over the READ-ONLY sibling checkout.

Drives the checkout's public sync Python API (``graybox.capture.capture`` and
``graybox.search.search_all``) via a ``sys.path`` insertion — the sibling repo
is never imported as an installed package and never modified. The workspace is
isolated by constructing a :class:`graybox.config.Config` rooted under the
benchmark workdir (the ``GRAYBOX_ROOT``/``--config`` pattern, built directly so
no ambient config file or environment leaks in).

Profile: raw-inbox altitude — captures land verbatim in the immutable inbox
and the deterministic profile never runs the LLM ``organize`` pass, so typed
facts are honestly degraded to raw text (recorded in ``version_info``).

A missing checkout is an honest unavailability: ``setup`` raises
:class:`AdapterUnsupported` (skip semantics), never a fabricated result.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from membench.adapters.base import (
    AdapterEnvironmentError,
    AdapterUnsupported,
    Capability,
    Hit,
    OpResult,
    Profile,
    StateExport,
    register_adapter,
)
from membench.ids import sentinels_in

PROFILE_NOTE = "raw-inbox altitude (no LLM organize pass); search_all over raw captures"


def default_checkout() -> Path:
    """Sibling checkout location; ``GRAYBOX_CHECKOUT`` overrides."""

    override = os.environ.get("GRAYBOX_CHECKOUT")
    if override:
        return Path(override)
    return Path.home() / "projects" / "graybox"


class GrayboxLocalAdapter:
    name = "graybox-local"
    supports_group_reuse = False
    #: Bulk load, nothing compiled. Declared rather than defaulted so an
    #: adapter author has to look at it; see INGESTION_ALTITUDES.
    ingestion_altitude = "raw_source"

    def __init__(self, *, checkout: Path | None = None) -> None:
        self._checkout = Path(checkout) if checkout is not None else default_checkout()
        self._workdir: Path | None = None
        self._cfg: object | None = None
        self._path_inserted: str | None = None
        self._profile: Profile | None = None

    # -- lifecycle --------------------------------------------------------
    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.INGEST_API, Capability.SEARCH})

    @property
    def workspace_root(self) -> Path:
        if self._workdir is None:
            raise AdapterEnvironmentError("adapter not set up")
        return self._workdir / "graybox-root"

    def setup(self, workdir: Path, profile: Profile) -> None:
        if not (self._checkout / "graybox" / "__init__.py").is_file():
            raise AdapterUnsupported(
                f"graybox checkout not found at {self._checkout}; "
                "clone the sibling repo or set GRAYBOX_CHECKOUT"
            )
        self._workdir = Path(workdir)
        self._profile = profile
        self.workspace_root.mkdir(parents=True, exist_ok=True)

        checkout_str = str(self._checkout)
        if checkout_str not in sys.path:
            sys.path.insert(0, checkout_str)
            self._path_inserted = checkout_str
        try:
            from graybox.config import (
                Config,
                EmbeddingsConfig,
                LLMConfig,
                RetrievalConfig,
            )
            from graybox.workspace import WorkspaceManager
        except ImportError as exc:  # missing sibling deps = honest unavailability
            raise AdapterUnsupported(f"graybox import failed: {exc}") from exc

        # Config constructed directly and pointed at the benchmark temp root:
        # no config.yaml lookup, no dotenv, no ambient GRAYBOX_* environment.
        manager = WorkspaceManager(
            root=self.workspace_root,
            active_workspace="membench",
            default_workspace="membench",
            config_path=None,
            app_config={},
        )
        manager.ensure_workspace("membench")
        self._cfg = Config(
            root=self.workspace_root,
            workspace_manager=manager,
            llm=LLMConfig(model_name="", base_url="", temperature=0.0),
            retrieval=RetrievalConfig(top_k=10, min_score=0.0),
            embeddings=EmbeddingsConfig(enabled=False),
            auto_refresh_summaries=False,
            raw={},
            config_path=None,
        )

    def cleanup(self) -> None:
        self._cfg = None
        if self._path_inserted is not None:
            try:
                sys.path.remove(self._path_inserted)
            except ValueError:  # pragma: no cover - already gone
                pass
            self._path_inserted = None

    # -- ingest -----------------------------------------------------------
    def ingest(self, corpus_dir: Path, native_dir: Path) -> list[OpResult]:
        import json

        captures_file = Path(native_dir) / "captures.jsonl"
        if not captures_file.is_file():
            raise AdapterEnvironmentError(f"missing native capture stream: {captures_file}")
        if self._cfg is None:
            raise AdapterEnvironmentError("adapter not set up")
        from graybox.capture import capture

        results: list[OpResult] = []
        for seq, line in enumerate(captures_file.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            record = json.loads(line)
            started = time.perf_counter()
            try:
                capture(self._cfg, record["text"])
                ok, detail = True, None
            except Exception as exc:  # recorded, stays in denominators
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            results.append(
                OpResult(
                    seq=seq,
                    op="capture",
                    source_id=record.get("source_id"),
                    ok=ok,
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                    detail=detail,
                )
            )
        return results

    # -- search -----------------------------------------------------------
    def search(self, query: str, limit: int) -> list[Hit]:
        if self._cfg is None:
            raise AdapterEnvironmentError("adapter not set up")
        from graybox.search import search_all

        # min_score=0.0 is an explicit benchmark override of the product's
        # 0.4 relevance floor: the benchmark measures raw ranking quality.
        wiki_hits, inbox_hits = search_all(
            self._cfg, query, top_k=limit, min_score=0.0
        )
        merged = sorted(
            list(wiki_hits) + list(inbox_hits),
            key=lambda hit: -float(getattr(hit, "score", 0.0)),
        )[:limit]

        hits: list[Hit] = []
        for rank, raw_hit in enumerate(merged, start=1):
            doc = raw_hit.doc
            text = str(getattr(doc, "search_blob", "") or "")
            title = None
            page = getattr(doc, "page", None)
            if page is not None:
                title = getattr(page, "title", None)
            hits.append(
                Hit(
                    rank=rank,
                    provider_path=str(getattr(doc, "search_id", f"doc/{rank}")),
                    title=title if isinstance(title, str) else None,
                    excerpt=text[:200] or None,
                    sentinels=tuple(sentinels_in(text)),
                    raw={
                        "search_id": str(getattr(doc, "search_id", "")),
                        "score": float(raw_hit.score),
                        "kind": str(getattr(doc, "source_kind", "")),
                    },
                    text=text or None,
                )
            )
        return hits

    # -- state export ------------------------------------------------------
    def export_state(self) -> StateExport:
        raise AdapterUnsupported(
            "graybox raw-inbox profile does not declare STATE_EXPORT"
        )

    def version_info(self) -> dict[str, str]:
        info = {
            "provider": self.name,
            "checkout": str(self._checkout),
            "profile_note": PROFILE_NOTE,
        }
        if self._profile is not None:
            info["profile"] = self._profile.name
        return info


register_adapter("graybox-local", lambda **kw: GrayboxLocalAdapter(**kw))
