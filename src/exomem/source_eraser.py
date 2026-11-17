"""Retire one captured Source incarnation through its custody owners, one bounded step per call."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from . import (
    generated_fragment_store,
    index_sync,
    memory_refs,
    native_copy_custody,
    privacy_log,
    source_capture,
    source_custody,
    source_erasure,
    source_native_custody,
    source_result_custody,
    vault,
)
from .cli_ops import OpError

#: Copies one call closes and catalogue pages it reads; the caller's retry continues the rest.
STEP_COPIES = 16
STEP_PAGES = 8
# Index fan-out outcomes are that module's closed enum: two prove completion, two ask for another fan-out.
_ACKNOWLEDGED = frozenset({"completed", "not_required"})
_REFAN = frozenset({"degraded", "failed"})


def _refuse(code: str, reason: str, remedy: str | None = None) -> OpError:
    return OpError(code, reason, remedy)


class _Ledger:
    """Operation bindings and per-target progress beside the private attempt ledger."""

    def __init__(self, store):
        self.store = store
        with store._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS source_erasure_operations (operation_id TEXT PRIMARY KEY, "
                         "source_ref TEXT NOT NULL, source_hash TEXT NOT NULL, source_identity TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS source_erasure_progress (source_ref TEXT PRIMARY KEY, "
                         "data TEXT NOT NULL)")

    def bind(self, operation_id: str, reference: str, source_hash: str, identity: dict) -> None:
        value = (operation_id, reference, source_hash, json.dumps(identity, sort_keys=True))
        with self.store._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO source_erasure_operations VALUES (?,?,?,?)", value)
            row = conn.execute("SELECT operation_id,source_ref,source_hash,source_identity "
                               "FROM source_erasure_operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None or tuple(row) != value:
            raise _refuse("SOURCE_ERASURE_OPERATION_CONFLICT", "this operation already targets another Source incarnation",
                          "Use a fresh operation_id for a different target.")

    def progress(self, reference: str) -> dict[str, Any]:
        with self.store._connect() as conn:
            row = conn.execute("SELECT data FROM source_erasure_progress WHERE source_ref=?", (reference,)).fetchone()
        return json.loads(row[0]) if row is not None else {}

    def save(self, reference: str, value: dict[str, Any]) -> None:
        with self.store._connect() as conn:
            conn.execute("INSERT INTO source_erasure_progress VALUES (?,?) ON CONFLICT(source_ref) "
                         "DO UPDATE SET data=excluded.data", (reference, json.dumps(value, sort_keys=True)))


def _status(components: dict[str, Any]) -> str:
    states = {item["state"] for item in components.values()}
    for state in ("pending", "failed", "unknown"):
        if state in states:
            return state
    return "complete"


class _Eraser:
    def __init__(self, root: Path, reference: str, identity: dict, fence: source_custody.RetirementFence,
                 ledger: _Ledger, manager):
        self.root, self.reference, self.identity, self.fence, self.ledger = root, reference, identity, fence, ledger
        self.manager = manager
        self.attempts = manager.idempotency.capture_attempts
        self._results: source_result_custody.ResultCustody | None = None
        self.native = source_native_custody.NativeCustody(root)
        self.store = generated_fragment_store.GeneratedFragmentStore(root)
        self.copies = native_copy_custody.NativeCopyCustody(root, store=self.store, native=self.native)

    def close(self) -> None:
        if self._results is not None:
            self._results.close()
        self.store.close()

    def report(self, status: str, components: dict[str, Any] | None = None, reason: str | None = None) -> dict:
        return {"target_source_ref": self.reference, "status": status, "target_retired": status == "complete",
                "components": components or {}, "failure": None if reason is None else {"code": reason}}

    def _stale(self) -> OpError:
        return _refuse("SOURCE_ERASURE_STALE", "the Source incarnation no longer has the expected hash",
                       "Read the current inventory and issue a new checked operation for its reference.")

    def _bindings(self) -> tuple[str, str | None]:
        """Open native bindings for this reference and the installed page name, never a path or identity guess."""
        cursor, path, state = None, None, "none"
        for _ in range(STEP_PAGES):
            page = self.native.lookup(source_ref=self.reference, limit=32, cursor=cursor)
            if page.state == "unknown":
                return "unknown", None
            for reservation in page.reservations:
                state = "open"
                if path is None and reservation.destination_relative is not None:
                    observed = self.native.observe(reservation.binding_id, destination=True)
                    if observed.state == "installed" and observed.matching:
                        path = reservation.destination_relative
            cursor = page.cursor
            if cursor is None:
                return state, path
        return "unknown", path

    def _gate(self, manifest: source_custody.Manifest, bindings: str, path: str | None) -> str | None:
        """Refuse known dependence and return the missing evidence; None admits the cutoff."""
        if manifest.allocated_hash != self.fence.source_hash:
            raise self._stale()
        if manifest.dependency.state == "present":
            raise _refuse("SOURCE_ERASURE_DEPENDENT", "retained knowledge depends on this Source incarnation")
        if manifest.dependency.state == "uncertain":
            return "dependency_uncertain"
        if not manifest.coverage_complete or manifest.generated_birth is None:
            return "coverage_incomplete"
        if bindings == "unknown":
            return "canonical_page_unavailable"
        if path is None:
            if manifest.publication == "published":
                return "canonical_page_unavailable"
            # An absent page is eligible only through the attempt that allocated this exact reference.
            return None if self.attempts.allocated_identity(self.reference) == self.identity else "custody_missing"
        try:
            text, _guard = vault.read_guarded_text(self.root, self.root / path)
            fields, _body, _raw = vault.parse_frontmatter(text)
        except (OSError, ValueError):
            return "canonical_page_unavailable"
        if memory_refs.ref_from_markdown(text) != self.reference or vault.content_hash(text) != self.fence.source_hash:
            raise self._stale()
        try:
            stored = dict(source_capture.validate_identity(fields.get("source_identity")))
        except source_capture.SourceCaptureError:
            stored = None
        if stored != self.identity:
            raise _refuse("SOURCE_ERASURE_TARGET_MISMATCH", "the Source reference belongs to another identity")
        try:
            inbound = vault.find_inbound_wikilinks(self.root, path)
        except (OSError, RuntimeError, ValueError):
            return "inbound_references_unavailable"
        if inbound:
            raise _refuse("SOURCE_ERASURE_DEPENDENT", "another page links to this Source incarnation")
        return None

    def _close(self, copy: generated_fragment_store.Copy) -> str:
        if copy.owner.owner == source_native_custody.OWNER:
            return self.copies.erase(copy)
        if copy.owner.owner != source_result_custody.OWNER:
            return "unknown"  # An owner outside this protocol cannot acknowledge its copy here.
        try:
            if self._results is None:
                self._results = source_result_custody.ResultCustody(self.root, journal=self.manager.idempotency.source_results)
            return self._results.retire(copy, self.reference)
        except (ValueError, OSError, source_result_custody.PublicationChanged):
            return "unknown"

    def _copies(self, birth, progress: dict[str, Any]) -> dict[str, Any]:
        # Each call resumes where the last stopped, so copies no owner can close never starve later ones.
        sweep = progress.setdefault("copies", {"cursor": None, "unknown": False, "last_unknown": False})
        cursor, processed, ended = (tuple(sweep["cursor"]) if sweep["cursor"] else None), 0, False
        for _ in range(STEP_PAGES):
            found = self.store.lookup(self.reference, birth, limit=STEP_COPIES - processed, cursor=cursor)
            if found.state == "pending" and not found.image_ids:
                break  # A catalogue still applying changes keeps the sweep where it stood.
            for copy in found.copies:
                sweep["unknown"] |= self._close(copy) == "unknown"
                processed += 1
            for image_id in found.image_ids:
                try:
                    self.store.collect(image_id, limit=STEP_COPIES)
                except ValueError:
                    pass  # Its open copies stay obligations until their owners close them.
            cursor, ended = found.cursor, found.cursor is None
            if ended or processed >= STEP_COPIES:
                break
        if ended:
            # A finished sweep's unknown copies stay reported until a later sweep closes or clears them.
            sweep["last_unknown"], sweep["unknown"] = sweep["unknown"], False
        sweep["cursor"] = cursor
        final = self.store.lookup(self.reference, birth, limit=1)
        if final.state == "ready" and not final.copies and not final.image_ids:
            return {"state": "acknowledged"}
        unknown = sweep["unknown"] or sweep["last_unknown"] or final.state == "unknown"
        return {"state": "unknown" if unknown else "pending", "catalogue": final.state}

    def _owner_done(self, component: str, outcome: str, graph: list | None) -> bool:
        """Whether one derived owner proved it dropped the retired page's rows."""
        if outcome in _ACKNOWLEDGED:
            return True
        if outcome in _REFAN:
            return False
        if component == "epistemic_graph":
            # The graph drops the page once a current build covers the checkpoint this delete registered.
            from . import graph_sync

            built = graph_sync.acknowledged_checkpoint(self.root)
            return (graph is not None and built is not None and graph_sync.status(self.root)["state"] == "current"
                    and (built.generation > graph[0] or (built.generation == graph[0]
                                                         and built.checkpoint_sha256 == graph[1])))
        if component == "embeddings":
            # Embedding deletion purges before its model gates, so any outcome but a failure proves the rows gone.
            return True
        # CLIP rows key binary media files, never a Markdown Source page; other owners need completion.
        return component == "clip"

    def _derived(self, progress: dict[str, Any], manifest: source_custody.Manifest) -> dict[str, Any]:
        path, owners = progress.get("path"), progress.get("owners", {})
        if path is None:
            # Derived owners index installed pages only; an unpublished allocation never reached them.
            return {"state": "acknowledged"} if manifest.publication == "allocated" else {"state": "unknown"}
        if not owners or set(owners.values()) & _REFAN:
            try:
                text, _guard = vault.read_guarded_text(self.root, self.root / path)
            except FileNotFoundError:
                text = None
            except (OSError, ValueError):
                return {"state": "unknown", "owners": owners}
            if text is not None and memory_refs.ref_from_markdown(text) == self.reference:
                return {"state": "pending", "owners": owners}
            report = index_sync.delete_after_remove(self.root, [path])
            if text is not None:
                # A fresh incarnation reuses this path: path-keyed owners drop the retired rows, then index it again.
                index_sync.upsert_after_write(self.root, [self.root / path])
            owners = {item.component: item.outcome for item in report.components}
            graph = next((item.graph_checkpoint for item in report.components if item.graph_checkpoint is not None), None)
            progress["owners"] = owners
            progress["graph"] = None if graph is None else [graph.generation, graph.checkpoint_sha256]
            self.ledger.save(self.reference, progress)
        pending = sorted(name for name, outcome in owners.items()
                         if not self._owner_done(name, outcome, progress.get("graph")))
        return {"state": "pending" if pending else "acknowledged", "owners": owners}

    def run(self) -> dict:
        manifest, _guard = source_custody.read_manifest(self.root, self.reference)
        if manifest is None:
            identity = memory_refs.parse_memory_ref(self.reference)
            held = memory_refs.paths_for_ids_read_only(self.root, [identity])
            if held is not None and held.get(identity):
                raise _refuse("SOURCE_ERASURE_NOT_ENROLLED", "this Source is outside erasure custody")
            return self.report("unknown", reason="custody_missing")
        retirement = manifest.retirement
        if retirement is not None and retirement.source_hash != self.fence.source_hash:
            raise self._stale()
        if retirement is not None and retirement.complete:
            return self.report("complete", {name: {"state": "acknowledged"} for name in
                                            ("canonical", "copies", "attempts", "derived", "caches", "host_logs")})
        progress = self.ledger.progress(self.reference)
        if retirement is None:
            bindings, path = self._bindings()
            missing = self._gate(manifest, bindings, path)
            if missing is not None:
                return self.report("unknown", reason=missing)
            # The private path is kept only until derived owners acknowledge their rows.
            progress["path"] = path
            self.ledger.save(self.reference, progress)
            source_custody.retire(self.root, self.reference, self.fence)
        components: dict[str, Any] = {"copies": self._copies(manifest.generated_birth, progress)}
        bindings, _path = self._bindings()
        components["canonical"] = {"state": {"none": "acknowledged", "open": "pending"}.get(bindings, "unknown")}
        components["attempts"] = {"state": "acknowledged" if self.attempts.retire_allocations(self.reference) else "pending"}
        components["derived"] = self._derived(progress, manifest)
        components["caches"] = {"state": "acknowledged"}
        # Without content-private logging, process logs may name the page; no owner can prove their absence.
        components["host_logs"] = {"state": "acknowledged" if privacy_log.content_private_logging_enabled() else "unknown"}
        status = _status(components)
        if status == "complete":
            source_custody.retire(self.root, self.reference, self.fence.model_copy(update={"complete": True}))
            progress = {}
        self.ledger.save(self.reference, progress)
        return self.report(status, components)


def erase(root: Path, operation_id: str, source_identity, expected_source_ref: str, expected_source_hash: str) -> dict:
    """Advance one incarnation's retirement; authority, policy and mode precede any lookup."""
    from .vault_store import mode
    from .writer_lease import active_manager

    root = Path(root)
    source_capture.require_authority(root)
    identity = dict(source_capture.validate_identity(source_identity))
    try:
        reference = memory_refs.canonical_memory_ref(expected_source_ref)
        fence = source_custody.RetirementFence(operation_id=operation_id, source_hash=expected_source_hash)
    except (ValueError, TypeError, ValidationError):
        raise _refuse("SOURCE_ERASURE_INVALID", "erasure requires an operation_id, canonical reference and SHA-256") from None
    policy = source_erasure.SourceErasurePolicy.from_env()
    if identity["system"] not in policy.enabled_systems:
        raise _refuse("SOURCE_ERASURE_DISABLED", "host policy does not enable erasure for this identity system")
    if mode.store_mode_active(root):
        raise _refuse("SOURCE_ERASURE_MODE_UNSUPPORTED", "erasure is not available for store-mode vaults yet")
    source_custody._boundary(root)
    manager = active_manager()
    ledger = _Ledger(manager.idempotency)
    ledger.bind(fence.operation_id, reference, fence.source_hash, identity)
    eraser = _Eraser(root, reference, identity, fence, ledger, manager)
    try:
        result = eraser.run()
    finally:
        eraser.close()
    source_capture.require_authority(root)
    return result
