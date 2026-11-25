"""Retire one captured Source incarnation through its custody owners, one bounded step per call."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from . import (
    generated_fragment_store,
    graph_drain,
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
    working_set_heat,
)
from .cli_ops import OpError

#: Copies one call closes and catalogue pages it reads; the caller's retry continues the rest.
STEP_COPIES = 16
STEP_PAGES = 8
# Index fan-out outcomes are that module's closed enum: two prove completion, two ask for another fan-out.
_ACKNOWLEDGED = frozenset({"completed", "not_required"})
_REFAN = frozenset({"degraded", "failed"})
#: The result's components, in report order; the completion record names the same set.
COMPONENTS = ("canonical", "copies", "attempts", "derived", "caches", "host_logs")
#: Gate answers that come from a failed read, so a repeat of the same call can clear them.
_READ_FAILURES = frozenset({"canonical_page_unavailable", "inbound_references_unavailable"})
# Component reasons a repeat can clear: a store that could not be read or written this time. Every other
# component reason (an index switched off, logs that are not content-private, custody that cannot be
# proved) needs a change outside this call, so the answer settles on it. A closed set the eraser emits.
_TRANSIENT = frozenset({"native_ledger_unreadable", "copy_unreadable", "page_unreadable", "heat_unreadable",
                        "generation_unwritable", "journal_unwritable"})


def _identity_sha256(identity: dict) -> str:
    """The ledger keeps this digest, never the caller's identity values."""
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _refuse(code: str, reason: str, remedy: str | None = None) -> OpError:
    return OpError(code, reason, remedy)


class _Ledger:
    """Operation bindings and per-target progress beside the private attempt ledger.

    Its tables exist from the first admitted operation: a refused call reads only, so it leaves no trace.
    """

    def __init__(self, store):
        self.store = store

    def _row(self, conn, operation_id: str):
        row = conn.execute("SELECT operation_id,source_ref,source_hash,identity_sha256 "
                           "FROM source_erasure_operations WHERE operation_id=?", (operation_id,)).fetchone()
        return None if row is None else tuple(row)

    def bind(self, operation_id: str, reference: str, source_hash: str, identity: dict) -> None:
        value = (operation_id, reference, source_hash, _identity_sha256(identity))
        with self.store._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS source_erasure_operations (operation_id TEXT PRIMARY KEY, "
                         "source_ref TEXT NOT NULL, source_hash TEXT NOT NULL, identity_sha256 TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS source_erasure_progress (source_ref TEXT PRIMARY KEY, "
                         "data TEXT NOT NULL)")
            conn.execute("INSERT OR IGNORE INTO source_erasure_operations VALUES (?,?,?,?)", value)
            row = self._row(conn, operation_id)
        if row != value:
            raise _refuse("SOURCE_ERASURE_OPERATION_CONFLICT", "this operation already targets another Source incarnation",
                          "Use a fresh operation_id for a different target.")

    def bound(self, operation_id: str, reference: str, source_hash: str, identity: dict) -> bool:
        with self.store._connect() as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                            "AND name = 'source_erasure_operations'").fetchone() is None:
                return False  # No operation was ever admitted here.
            return self._row(conn, operation_id) == (operation_id, reference, source_hash, _identity_sha256(identity))

    def progress(self, reference: str) -> dict[str, Any]:
        with self.store._connect() as conn:
            row = conn.execute("SELECT data FROM source_erasure_progress WHERE source_ref=?", (reference,)).fetchone()
        return json.loads(row[0]) if row is not None else {}

    def save(self, reference: str, value: dict[str, Any]) -> None:
        with self.store._connect() as conn:
            conn.execute("INSERT INTO source_erasure_progress VALUES (?,?) ON CONFLICT(source_ref) "
                         "DO UPDATE SET data=excluded.data", (reference, json.dumps(value, sort_keys=True)))

    def clear(self, reference: str) -> None:
        # Progress holds the page name, so a completed retirement keeps none.
        with self.store._connect() as conn:
            conn.execute("DELETE FROM source_erasure_progress WHERE source_ref=?", (reference,))


def _transient(components: dict[str, Any]) -> bool:
    return any(item.get("reason") in _TRANSIENT for item in components.values())


def _retry_after_ms(components: dict[str, Any]) -> int:
    """When a repeat can see progress: the graph drain's next pass, or at once for steps the next call takes."""
    if "epistemic_graph" in components.get("derived", {}).get("pending", ()):
        return math.ceil(graph_drain.seconds_until_pass() * 1000)
    if _transient(components):
        # No schedule says when a failed read heals; one second keeps a caller's loop from spinning.
        return 1000
    return 0


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
        # Settled: a repeat of this call cannot change the answer by itself. Pending components still advance.
        settled = status != "pending" and reason not in _READ_FAILURES and not _transient(components or {})
        return {"target_source_ref": self.reference, "status": status, "target_retired": status == "complete",
                "settled": settled, "retry_after_ms": None if settled else _retry_after_ms(components or {}),
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
        return "backlog", path  # More open bindings than one call reads: still open, nothing failed to read.

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
        if bindings in ("unknown", "backlog"):
            return "canonical_page_unavailable"
        if path is None:
            if manifest.publication == "published":
                return "custody_missing"  # A published page without its installed binding stays unprovable.
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
            return "unreadable"

    def _copies(self, birth, progress: dict[str, Any]) -> dict[str, Any]:
        # Each call resumes where the last stopped, so copies no owner can close never starve later ones.
        sweep = progress.setdefault("copies", {"cursor": None, "unknown": False, "last_unknown": False})
        sweep.setdefault("unreadable", False)
        sweep.setdefault("last_unreadable", False)
        cursor, processed, ended = (tuple(sweep["cursor"]) if sweep["cursor"] else None), 0, False
        for _ in range(STEP_PAGES):
            found = self.store.lookup(self.reference, birth, limit=STEP_COPIES - processed, cursor=cursor)
            if found.state == "pending" and not found.image_ids:
                break  # A catalogue still applying changes keeps the sweep where it stood.
            for copy in found.copies:
                closed = self._close(copy)
                sweep["unreadable"] |= closed == "unreadable"
                sweep["unknown"] |= closed in ("unknown", "unreadable")
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
            sweep["last_unreadable"], sweep["unreadable"] = sweep["unreadable"], False
        sweep["cursor"] = cursor
        final = self.store.lookup(self.reference, birth, limit=1)
        if final.state == "ready" and not final.copies and not final.image_ids:
            return {"state": "acknowledged"}
        unknown = sweep["unknown"] or sweep["last_unknown"] or final.state == "unknown"
        if not unknown:
            return {"state": "pending", "catalogue": final.state}
        # Only a failed head read carries no catalogue; recorded uncertainty names one and lasts.
        head_unread = final.state == "unknown" and final.catalogue_id is None
        unreadable = sweep["unreadable"] or sweep["last_unreadable"] or head_unread
        reason = "copy_unreadable" if unreadable else "copy_unprovable"
        return {"state": "unknown", "catalogue": final.state, "reason": reason}

    def _graph_dropped(self, path: str, reused: bool) -> bool:
        """The graph keeps no row of the retired version, nor a queued repair or handoff record that names its page.

        The handoff record names the last write's pages. Once the graph acknowledges it, this call replaces it
        with a record of no page, and the drain's acknowledgement of that one carries the name out of the graph.
        """
        from . import deferred_index, epistemic_graph, graph_sync

        if epistemic_graph.holds_page(self.root, path, source_hash=self.fence.source_hash,
                                      reference=self.reference) is not False:
            return False
        if reused:
            return True  # Queued work and handoff records for this name now serve the Source that holds it.
        if deferred_index.snapshot_graph(self.root, paths={path}):
            return False
        registered = graph_sync.read_checkpoint(self.root)
        built = graph_sync.acknowledged_checkpoint(self.root)
        if registered is None:
            return built is None
        if path in registered.created_paths or any(name == path for name, _hash in registered.paths):
            try:
                graph_sync.forget_settled_path(self.root, path)
            except (OSError, graph_sync.GraphRebuildRegistrationError):
                pass  # The record stays as it was; a later call tries again.
            return False
        # The graph stores the registered record only once a build covers it; an absent graph stores none.
        if built is None:
            return not epistemic_graph.sidecar_path(self.root).exists()
        return built.covers(registered)

    def _owner_done(self, component: str, outcome: str, path: str, reused: bool) -> bool:
        """Whether one derived owner proved it dropped the retired page's rows."""
        if component == "epistemic_graph":
            # Its outcome describes one dispatch, which may cover an older checkpoint; only the stored graph proves.
            return outcome not in _REFAN and self._graph_dropped(path, reused)
        if outcome in _ACKNOWLEDGED:
            return True
        if outcome in _REFAN:
            return False
        if component == "embeddings":
            # Embedding deletion purges before its model gates, so any outcome but a failure proves the rows gone.
            return True
        # CLIP rows key binary media files, never a Markdown Source page; other owners need completion.
        return component == "clip"

    def _heat(self, path: str, reused: bool, progress: dict[str, Any]) -> str:
        """Working-set heat is no index owner: its events and session marks name pages until purged."""
        if reused and progress.get("heat_purged"):
            return "acknowledged"  # Later heat for this name belongs to the Source that now holds it.
        if working_set_heat.holds(self.root, path) and working_set_heat.purge(self.root, [path]):
            progress["heat_purged"] = True
        held = working_set_heat.holds(self.root, path)
        if held is None:
            return "heat_disabled" if working_set_heat.disabled() else "heat_unreadable"
        return "pending" if held else "acknowledged"

    def _derived(self, progress: dict[str, Any], manifest: source_custody.Manifest) -> dict[str, Any]:
        path, owners = progress.get("path"), progress.get("owners", {})
        if path is None:
            # Derived owners index installed pages only; an unpublished allocation never reached them.
            if manifest.publication == "allocated":
                return {"state": "acknowledged"}
            return {"state": "unknown", "reason": "publication_unprovable"}
        try:
            text, _guard = vault.read_guarded_text(self.root, self.root / path)
        except FileNotFoundError:
            text = None
        except (OSError, ValueError):
            return {"state": "unknown", "owners": owners, "reason": "page_unreadable"}
        if text is not None and memory_refs.ref_from_markdown(text) == self.reference:
            return {"state": "pending", "owners": owners}
        reused = text is not None
        if not owners or set(owners.values()) & _REFAN:
            report = index_sync.delete_after_remove(self.root, [path])
            if reused:
                # A fresh incarnation reuses this path: path-keyed owners drop the retired rows, then index it again.
                index_sync.upsert_after_write(self.root, [self.root / path])
            owners = {item.component: item.outcome for item in report.components}
            progress["owners"] = owners
            self.ledger.save(self.reference, progress)
        pending = sorted(name for name, outcome in owners.items() if not self._owner_done(name, outcome, path, reused))
        heat = self._heat(path, reused, progress)
        if heat != "acknowledged":
            pending.append("working_set_heat")
        if heat in ("heat_disabled", "heat_unreadable"):
            return {"state": "unknown", "owners": owners, "pending": pending, "reason": heat}
        from . import epistemic_graph

        if "epistemic_graph" in pending and not epistemic_graph.graph_enabled():
            # A disabled graph index never drops its stored rows, so a repeat cannot progress: answer unknown and
            # settled instead of pending forever. Re-enabling the index lets a later call finish the erase.
            return {"state": "unknown", "owners": owners, "pending": pending, "reason": "graph_index_disabled"}
        return {"state": "pending" if pending else "acknowledged", "owners": owners, "pending": pending}

    def _caches(self, progress: dict[str, Any], derived: dict[str, Any]) -> dict[str, Any]:
        """Reusable caches check the retirement generation; one advance after the derived rows are gone voids them."""
        if derived["state"] != "acknowledged":
            # The fence waits for the derived rows, so it carries their unknown and its reason.
            if derived["state"] == "unknown":
                return {"state": "unknown", "reason": derived.get("reason")}
            return {"state": "pending"}
        if not progress.get("caches_fenced"):
            from . import lexstore

            # The catalogue's change journal still names the dropped page; it is the history kept catalogue
            # states patch from, so it restarts here too.
            if not lexstore.restart_change_journal(self.root):
                return {"state": "unknown", "reason": "journal_unwritable"}
            if not source_custody.advance_retirement_generation(self.root):
                return {"state": "unknown", "reason": "generation_unwritable"}
            progress["caches_fenced"] = True
            self.ledger.save(self.reference, progress)
        return {"state": "acknowledged"}

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
            # A process that stopped after the completion record may have left progress behind.
            self.ledger.clear(self.reference)
            return self.report("complete", {name: {"state": "acknowledged"} for name in COMPONENTS})
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
        found: dict[str, Any] = {"copies": self._copies(manifest.generated_birth, progress)}
        bindings, _path = self._bindings()
        found["canonical"] = ({"state": "unknown", "reason": "native_ledger_unreadable"} if bindings == "unknown"
                              else {"state": "acknowledged" if bindings == "none" else "pending"})  # open, backlog
        found["attempts"] = {"state": "acknowledged" if self.attempts.retire_allocations(self.reference) else "pending"}
        found["derived"] = self._derived(progress, manifest)
        found["caches"] = self._caches(progress, found["derived"])
        # Without content-private logging, process logs may name the page; no owner can prove their absence.
        found["host_logs"] = ({"state": "acknowledged"} if privacy_log.content_private_logging_enabled()
                              else {"state": "unknown", "reason": "logs_not_content_private"})
        components = {name: found[name] for name in COMPONENTS}
        status = _status(components)
        self.ledger.save(self.reference, progress)
        if status == "complete":
            source_custody.retire(self.root, self.reference, self.fence.model_copy(update={"complete": True}))
            self.ledger.clear(self.reference)
        return self.report(status, components)


def _admitted(root: Path, ledger: _Ledger, fence: source_custody.RetirementFence, reference: str, identity: dict) -> bool:
    """Whether this exact operation was bound and wrote its cutoff before the policy changed."""
    if not ledger.bound(fence.operation_id, reference, fence.source_hash, identity):
        return False
    manifest, _guard = source_custody.read_manifest(root, reference)
    return manifest is not None and manifest.retirement is not None and manifest.retirement.source_hash == fence.source_hash


def erase(root: Path, operation_id: str, source_identity, expected_source_ref: str, expected_source_hash: str) -> dict:
    """Advance one incarnation's retirement; authority and mode precede any lookup.

    A disabled policy refuses new operations only: one already bound whose cutoff is written finishes its cleanup.
    """
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
    refusal = source_erasure.admission_refusal(root, policy, identity["system"])
    if refusal == "store_mode":
        raise _refuse("SOURCE_ERASURE_MODE_UNSUPPORTED", "erasure is not available for store-mode vaults yet")
    source_custody._boundary(root)
    manager = active_manager()
    ledger = _Ledger(manager.idempotency)
    if refusal is not None and not _admitted(root, ledger, fence, reference, identity):
        raise _refuse("SOURCE_ERASURE_DISABLED", "host policy does not enable erasure for this identity system")
    ledger.bind(fence.operation_id, reference, fence.source_hash, identity)
    eraser = _Eraser(root, reference, identity, fence, ledger, manager)
    try:
        result = eraser.run()
    finally:
        eraser.close()
    source_capture.require_authority(root)
    return result
