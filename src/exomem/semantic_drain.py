"""One service semantic dispatcher, with a reserved small caller and one bulk task.

Execution debt stays in semantic_upserts. DerivedDrain keeps component custody;
this owner publishes exact parents, then wakes that existing custody scheduler.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from . import deferred_index, mode, runtime_resources

log = logging.getLogger(__name__)
SCAN_LIMIT = 64
TURN_LIMIT = 8
TURN_OVERHEAD_SECONDS = 1.0
IDLE_POLL_SECONDS = 30.0
RETRY_POLL_SECONDS = 1.0
PROOF_LIMIT = 64
_LOCK = threading.Lock()
_ACTIVE: dict[str, SemanticDrain] = {}


def _key(root: Path) -> str:
    return str(root.resolve())


def preparation_policy(root: Path) -> str:
    """Identity of the effective peak envelope; no model load or source read."""
    from . import embeddings, recall_space, semantic_index

    values = (
        "service-semantic-preparation-v1", runtime_resources.SEMANTIC_PREPARATION_BYTES,
        runtime_resources.SEMANTIC_SMALL_RESERVE_BYTES, runtime_resources.SEMANTIC_SOURCE_MAX_BYTES,
        semantic_index.PARSER_VERSION, recall_space.configured_recall_model(),
        recall_space.current_dim(), embeddings.get_embedding_index(root).identity,
        semantic_index.current_registry_identity(root),
    )
    return hashlib.sha256(repr(values).encode()).hexdigest()


def _input_signature(path: Path) -> str:
    from . import freshness

    try:
        return repr(freshness.stat_signature(path))
    except OSError:
        return "unavailable"


def _small_parent(root: Path, path: Path) -> bool:
    """Use the existing no-encode chunker/parser only on <=1 KiB inputs."""
    from . import embeddings, find_corpus, semantic_index, vault

    try:
        if path.stat().st_size > 1024:
            return False
        with runtime_resources.semantic_preparation(root, path) as prepared:
            page = find_corpus.parse_page(
                path, path.stat().st_mtime, root,
                content=prepared.source.encode("utf-8"),
                resolved_relative=path.relative_to(root).as_posix(),
            )
            if page is None:
                return False
            prepared.check_page(page, vector_dim=0)
            chunks = embeddings._chunks_for_page(root, page, allow_encode=False)
            if chunks is None or len(chunks) > 3:
                return False
            state = semantic_index.current_parent_index_state(root, path, source=prepared.source)
            return sum(unit.unit_ref is not None for unit in state.document.units) <= 3
    except (OSError, UnicodeError, ValueError, vault.PathGuardError,
            runtime_resources.PreparationBudgetExceeded, runtime_resources.PreparationCapacityBusy):
        return False


def publication_ready(root: Path, rel: str, *, expected_hash: str | None = None, claims_required: bool = False) -> bool:
    with _LOCK:
        owner = _ACTIVE.get(_key(root))
    return owner is not None and owner.publication_ready(rel, expected_hash=expected_hash, claims_required=claims_required)


def signal(root: Path, paths: list[str] | tuple[str, ...] = ()) -> None:
    """A hint never creates a worker or execution debt."""
    with _LOCK:
        owner = _ACTIVE.get(_key(root))
    if owner is not None:
        owner.signal(paths)


def status(root: Path | None) -> dict[str, Any]:
    """Content-free worker state, without allocating a worker, model or sidecar."""
    with _LOCK:
        owner = _ACTIVE.get(_key(root)) if root is not None else None
    if owner is None:
        return {"state": "inactive", "bulk_active": False, "small_active": False}
    with owner._lock:
        return {
            "state": "stopping" if owner._stop.is_set() else "running",
            "bulk_active": owner._bulk_path is not None,
            "small_active": owner._small_active,
        }


def request(root: Path, paths: list[Path], *, edited: bool, claims_required: bool = False) -> int:
    """Durable handoff, then a coalesced wake beyond the canonical writer lease."""
    from . import writer_lease

    rels = []
    for path in paths:
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if edited or not publication_ready(root, rel, claims_required=claims_required):
            rels.append(rel)
    receipts = (
        deferred_index.add_receipts(root, rels) if edited
        else deferred_index.ensure_receipts(root, rels)
    )
    if receipts:
        hints = tuple(receipt.rel_path for receipt in receipts)
        wake = lambda: signal(root, hints)
        if not writer_lease.defer_housekeeping_until_terminal_persisted(wake):
            wake()
    return len(receipts)


def start(root: Path) -> SemanticDrain | None:
    if not mode.service_profile_enabled() or os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        return None
    return SemanticDrain(root).start()


class SemanticDrain:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._small_active = False
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._bulk_thread: threading.Thread | None = None
        self._bulk_path: str | None = None
        self._hints: set[str] = set()
        self._cursor = ""
        self._proofs: OrderedDict[str, Any] = OrderedDict()

    def start(self) -> SemanticDrain:
        with _LOCK:
            existing = _ACTIVE.get(_key(self.root))
            if existing is not None:
                if existing._alive():
                    if existing._stop.is_set():
                        raise RuntimeError("semantic drain is still running during shutdown")
                    return existing
            self._stop.clear()
            self._wake.set()
            self._thread = threading.Thread(target=self._run, name="exomem-semantic-drain", daemon=True)
            _ACTIVE[_key(self.root)] = self
            self._thread.start()
        return self

    def _alive(self) -> bool:
        return any(thread is not None and thread.is_alive() for thread in (self._thread, self._bulk_thread))

    def signal(self, paths=()) -> None:
        with self._lock:
            for path in paths:
                if len(self._hints) >= SCAN_LIMIT:
                    break
                self._hints.add(path)
        self._wake.set()

    def publication_ready(self, rel: str, *, expected_hash: str | None = None, claims_required: bool = False) -> bool:
        with self._lock:
            proof = self._proofs.get(rel)
        return (
            proof is not None
            and (expected_hash is None or proof.guard.expected_content_hash == expected_hash)
            and (proof.current(self.root, claims_required=True) if claims_required else proof.current(self.root))
        )

    def _run(self) -> None:
        interval = IDLE_POLL_SECONDS
        while not self._stop.is_set():
            self._wake.wait(timeout=interval)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                progressed, has_debt = self._turn()
                interval = 0.05 if progressed else RETRY_POLL_SECONDS if has_debt else IDLE_POLL_SECONDS
            except Exception:  # noqa: BLE001 - debt is durable, no source in diagnostics
                log.warning("service semantic recovery turn failed")
                interval = RETRY_POLL_SECONDS

    def _turn(self) -> tuple[bool, bool]:
        with self._lock:
            hints = self._hints
            self._hints = set()
        hinted = deferred_index.snapshot(self.root, paths=hints, limit=SCAN_LIMIT) if hints else []
        page = deferred_index.snapshot(self.root, after_path=self._cursor, limit=SCAN_LIMIT)
        if not page:
            self._cursor = ""
            page = deferred_index.snapshot(self.root, after_path="", limit=SCAN_LIMIT)
        seen: set[str] = set()
        attempted = 0
        overhead = 0.0
        progressed = False
        policy = preparation_policy(self.root)
        for receipt in (*hinted, *page):
            if self._stop.is_set() or attempted >= TURN_LIMIT or overhead >= TURN_OVERHEAD_SECONDS:
                break
            if receipt.rel_path in seen:
                continue
            seen.add(receipt.rel_path)
            if receipt not in hinted:
                self._cursor = receipt.rel_path
            began = time.monotonic()
            path = self.root / receipt.rel_path
            signature = _input_signature(path)
            with self._lock:
                inflight = self._bulk_path == receipt.rel_path
            if inflight or not deferred_index.semantic_receipt_eligible(
                receipt, input_signature=signature, policy=policy, now=time.time()
            ):
                overhead += time.monotonic() - began
                continue
            attempted += 1
            small = _small_parent(self.root, path)
            overhead += time.monotonic() - began
            if small:
                with self._lock:
                    self._small_active = True
                try:
                    self._execute(receipt, "foreground", signature, policy)
                finally:
                    with self._lock:
                        self._small_active = False
                progressed = True
            else:
                with self._lock:
                    if self._bulk_path is not None:
                        continue
                    self._bulk_path = receipt.rel_path
                    thread = threading.Thread(
                        target=self._bulk, args=(receipt, signature, policy),
                        name="exomem-semantic-bulk", daemon=True,
                    )
                    self._bulk_thread = thread
                    thread.start()
                progressed = True
        # Retain hinted paths not visited in this bounded turn. Their rows, not
        # this volatile hint, own execution across crashes and hint overflow.
        with self._lock:
            self._hints.update(sorted(hints - seen)[:max(0, SCAN_LIMIT - len(self._hints))])
        return progressed, bool(hinted or page)

    def _bulk(self, receipt, signature: str, policy: str) -> None:
        try:
            self._execute(receipt, "bulk", signature, policy)
        finally:
            with self._lock:
                self._bulk_path = None
            self._wake.set()

    def _execute(self, receipt, work_class: str, signature: str, policy: str) -> None:
        from . import derived_drain, embeddings

        with self._lock:
            self._proofs.pop(receipt.rel_path, None)
        try:
            with runtime_resources.model_work(work_class, cancel_event=self._stop):
                result = embeddings.upsert_after_write_status(
                    self.root, [self.root / receipt.rel_path], defer_during_warm=False,
                )
            if self._stop.is_set():
                return
            proof = result.publication
            if result.status == "completed" and proof is not None and proof.current(self.root):
                # Stale preparation can neither retire a later revision nor
                # register its proof as the completion of that later edit.
                if deferred_index.clear_receipts(self.root, [receipt]) == 1:
                    with self._lock:
                        self._proofs[receipt.rel_path] = proof
                        while len(self._proofs) > PROOF_LIMIT:
                            self._proofs.popitem(last=False)
                    derived_drain.signal(self.root)
                return
            code = {
                "embedding_preparation_budget_exceeded": "resource_budget_exceeded",
                "embedding_preparation_busy": "preparation_busy",
                "embedding_input_unavailable": "input_unavailable",
                "embedding_input_drifted": "input_changed",
            }.get(result.code, "embedding_failed")
        except Exception:  # noqa: BLE001 - stable code, durable exact receipt
            code = "embedding_failed"
        if not self._stop.is_set():
            deferred_index.retry_semantic(
                self.root, receipt, failure_code=code, input_signature=signature, policy=policy,
            )

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        deadline = time.monotonic() + max(0.0, timeout)
        for thread in (self._thread, self._bulk_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if self._alive():
            raise RuntimeError("semantic drain is still running during shutdown")
        with _LOCK:
            if _ACTIVE.get(_key(self.root)) is self:
                _ACTIVE.pop(_key(self.root), None)
