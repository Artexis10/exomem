"""Re-embed the recall sidecar into the recall encoder's space, blue/green.

When the configured recall encoder is not the one the serving sidecar was
written by (another model, or another build of the same model), the serving
sidecar keeps serving: queries and writes are encoded for it by the encoder
that wrote it, which warm-up keeps resident (`preload_serving_encoder`).
Meanwhile this job builds a sidecar for the new space beside it
(`index_paths.space_sidecar_name`):

- path by path, in committed batches of about `BATCH_CHUNKS` chunks, on its
  own thread, stopping between batches when the service stops;
- resumably: a path is done when the new sidecar's rows carry the page's
  current mtime, so a restart re-encodes nothing already built;
- through the same chunking seam as live writes (`embeddings._chunks_for_page`);
- with its catch-up in the same pass: a page written while the job ran has a
  newer mtime than its new rows, or none, and is encoded again.

The cutover is one atomic replacement of the active pointer
(`index_paths.publish_active_sidecar`), after a final catch-up pass; one more
pass after it takes any write that landed in the old sidecar in between. The
old encoder is then released. The old sidecar stays on disk until a later start
of this job retires it, so a failed or regretted cutover can fall back to it.

`EXOMEM_RECALL_REEMBED=off` builds nothing: the old sidecar keeps serving with
its own encoder. Hosted cells never run this job.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import index_paths, recall_space

log = logging.getLogger(__name__)

REEMBED_ENV = "EXOMEM_RECALL_REEMBED"
_OFF = frozenset({"off", "0", "false", "no"})
#: Chunks per committed batch: the unit of progress a restart keeps and the
#: longest the job runs before it looks at the stop signal again.
BATCH_CHUNKS = 64
THREAD_NAME = "exomem-recall-reembed"
#: Catch-up passes before a cutover, each over only what changed during the last.
_CATCH_UP_PASSES = 3
#: How long the job waits for the service warm-up between readiness checks.
_WARM_POLL_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    """The serving sidecar's space, the recall encoder's, and where the new sidecar goes."""

    serving: recall_space.SpaceIdentity
    target: recall_space.SpaceIdentity
    shadow_path: Path


_LOCK = threading.Lock()
_STATUS: dict[str, dict[str, Any]] = {}
#: Vaults this process cut over. Their old sidecar is retired by a later start,
#: never by the process that swapped it out.
_CUT_OVER: set[str] = set()


def reembed_enabled() -> bool:
    """False when `EXOMEM_RECALL_REEMBED` turns the job off."""
    return (os.environ.get(REEMBED_ENV) or "").strip().lower() not in _OFF


def _key(vault_root: Path) -> str:
    return str(Path(vault_root).resolve())


def _space(identity: recall_space.SpaceIdentity | None, path: Path | None = None) -> dict | None:
    if identity is None:
        return None
    described: dict[str, Any] = {
        "model": identity.model,
        "fingerprint": identity.fingerprint,
        "dim": identity.dim,
    }
    if path is not None:
        described["sidecar"] = path.name
    return described


def _update(vault_root: Path, **fields: Any) -> None:
    with _LOCK:
        status = _STATUS.setdefault(_key(vault_root), {})
        status.update(fields)
        status["updated_at"] = time.time()


def reset_for_tests() -> None:
    """Forget this process's migration state (a fresh process, for tests)."""
    with _LOCK:
        _STATUS.clear()
        _CUT_OVER.clear()


def _hosted() -> bool:
    return recall_space.cell_mode()


# ------------------------------------------------------------------ the plan


def _target_identity() -> recall_space.SpaceIdentity:
    """The recall encoder's space. Loads the encoder: never call on a request thread."""
    from . import embeddings

    model = recall_space.recall_model()
    embeddings.get_model()
    # A width recall does not declare is learned from the new sidecar's first write.
    dim = recall_space.DECLARED_DIMS.get(model, 0)
    return recall_space.SpaceIdentity(model, recall_space.resident_fingerprint(model), dim)


def plan(vault_root: Path) -> MigrationPlan | None:
    """The migration the serving sidecar needs now, or None when it needs none.

    Loads the recall encoder to learn its fingerprint, so it belongs to the
    job's thread or warm-up, never to a request.
    """
    from . import embeddings

    active = embeddings.get_embedding_index(vault_root)
    serving = active.identity
    if serving is None:
        return None
    target = _target_identity()
    if serving.accepts(target.model, target.fingerprint):
        return None
    key = target.fingerprint or f"{target.model}|{target.dim}"
    shadow_path = active.path.parent / index_paths.space_sidecar_name(key)
    if shadow_path == active.path:
        return None
    return MigrationPlan(serving, target, shadow_path)


def preload_serving_encoder(vault_root: Path) -> bool:
    """Load the encoder that serves the serving sidecar, when it is not the recall encoder.

    Warm-up and this job call it, so requests find that encoder resident and
    never load it themselves. True when it is resident afterwards.
    """
    from . import embeddings

    if _hosted():
        return False
    identity = embeddings.get_embedding_index(vault_root).identity
    if identity is None or identity.model == recall_space.recall_model():
        return False
    recall_space.previous_encoder(identity.model)
    return True


# ------------------------------------------------------------------ the build


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _pass(
    vault_root: Path,
    plan_: MigrationPlan,
    *,
    should_stop: Callable[[], bool],
) -> tuple[bool, int]:
    """One pass over the vault into the new sidecar: `(finished, paths encoded)`."""
    from . import access, embeddings, semantic_index
    from . import find as find_module

    shadow = embeddings.get_embedding_index(vault_root, path=plan_.shadow_path)
    stored = shadow.file_mtimes()
    stored_units = shadow.semantic_unit_parent_states()
    pending: list[tuple[str, Path, list[str], float]] = []
    pending_units: list[tuple[Any, Path, float]] = []
    seen: set[str] = set()
    units_seen: set[str] = set()
    total = 0
    for md in index_paths.iter_index_markdown(vault_root):
        if not index_paths.is_embeddable_path(md):
            continue
        page = find_module._CACHE.get(md, vault_root)
        if page is None or not access.is_indexable(vault_root, page.rel_path):
            continue
        total += 1
        try:
            unit_state = semantic_index.build_parent_index_state(vault_root, md)
        except (OSError, UnicodeError, ValueError):
            unit_state = None
        if unit_state is not None:
            units_seen.add(page.rel_path)
            refs = frozenset(
                unit.unit_ref for unit in unit_state.document.units if unit.unit_ref is not None
            )
            have = stored_units.get(page.rel_path)
            if have != (frozenset({unit_state.parent_generation}), refs) and (have is not None or refs):
                pending_units.append((unit_state, md, page.mtime))
        chunks = embeddings._chunks_for_page(vault_root, page)
        if not chunks:
            continue
        seen.add(page.rel_path)
        if stored.get(page.rel_path) != page.mtime:
            pending.append((page.rel_path, md, chunks, page.mtime))

    pending_chunks = sum(len(item[2]) for item in pending)
    _update(
        vault_root,
        paths_total=total,
        paths_done=total - len({item[0] for item in pending}),
        chunks_pending=pending_chunks,
    )
    encoded_paths = 0

    def batches(items, size_of):
        batch, size = [], 0
        for item in items:
            batch.append(item)
            size += size_of(item)
            if size >= BATCH_CHUNKS:
                yield batch
                batch, size = [], 0
        if batch:
            yield batch

    for batch in batches(pending, lambda item: len(item[2])):
        if should_stop():
            _update(vault_root, state="paused")
            return False, encoded_paths
        started = time.monotonic()
        flat = [chunk for _rel, _md, chunks, _mtime in batch for chunk in chunks]
        with shadow.encoding():
            vectors = _encode_one_by_one(flat)
        offset = 0
        for rel, md, chunks, mtime in batch:
            count = len(chunks)
            # A page written since it was read is left for the next pass.
            if _mtime(md) == mtime:
                shadow.upsert_file(rel, chunks, vectors[offset : offset + count], mtime)
                encoded_paths += 1
            offset += count
        _note_progress(vault_root, len(flat), time.monotonic() - started, done=len(batch))

    for batch in batches(
        pending_units,
        lambda item: sum(unit.unit_ref is not None for unit in item[0].document.units),
    ):
        if should_stop():
            _update(vault_root, state="paused")
            return False, encoded_paths
        texts = [
            unit.content
            for state, _md, _mtime in batch
            for unit in state.document.units
            if unit.unit_ref is not None
        ]
        with shadow.encoding():
            vectors = _encode_one_by_one(texts)
        offset = 0
        for state, md, mtime in batch:
            count = sum(unit.unit_ref is not None for unit in state.document.units)
            if _mtime(md) == mtime:
                shadow.upsert_semantic_units(state, vectors[offset : offset + count], mtime)
            offset += count

    for rel in sorted({rel for rel in stored if rel not in seen} | {rel for rel in stored_units if rel not in units_seen}):
        shadow.delete_file(rel)
    return True, encoded_paths


def _encode_one_by_one(texts: list[str]) -> Any:
    """Passage vectors for `texts`, one text per encode.

    A served int8 model computes each text alone anyway (a shared batch would
    move its vectors with its neighbours'); encoding one text per call also
    releases the model between texts, so a query waits for at most one
    passage of the build, never a batch of them.
    """
    import numpy as np

    from . import embeddings

    return np.vstack([embeddings.embed_texts([text], is_query=False) for text in texts])


def _note_progress(vault_root: Path, chunks: int, seconds: float, *, done: int) -> None:
    with _LOCK:
        status = _STATUS.setdefault(_key(vault_root), {})
        status["chunks_encoded"] = int(status.get("chunks_encoded") or 0) + chunks
        status["encode_seconds"] = float(status.get("encode_seconds") or 0.0) + seconds
        status["chunks_pending"] = max(0, int(status.get("chunks_pending") or 0) - chunks)
        status["paths_done"] = int(status.get("paths_done") or 0) + done
        rate = status["chunks_encoded"] / status["encode_seconds"] if status["encode_seconds"] else 0.0
        status["seconds_per_1k_chunks"] = round(1000.0 / rate, 1) if rate else None
        status["eta_seconds"] = round(status["chunks_pending"] / rate) if rate else None
        status["updated_at"] = time.time()


def build(
    vault_root: Path,
    plan_: MigrationPlan,
    *,
    should_stop: Callable[[], bool] = lambda: False,
) -> bool:
    """Build the new sidecar up to the vault's current state. False when stopped early."""
    _update(
        vault_root,
        state="building",
        serving=_space(plan_.serving),
        target=_space(plan_.target, plan_.shadow_path),
    )
    with _LOCK:
        _STATUS[_key(vault_root)].setdefault("started_at", time.time())
    finished, _encoded = _pass(vault_root, plan_, should_stop=should_stop)
    return finished


def cut_over(vault_root: Path, plan_: MigrationPlan) -> None:
    """Make the new sidecar the serving one, atomically, and release the old encoder.

    A failure before the pointer is replaced leaves the old sidecar serving and
    the new one ready for the next attempt.
    """
    from . import embeddings

    _update(vault_root, state="cutting_over")
    for _attempt in range(_CATCH_UP_PASSES):
        finished, encoded = _pass(vault_root, plan_, should_stop=lambda: False)
        if finished and encoded == 0:
            break
    index_paths.publish_active_sidecar(vault_root, plan_.shadow_path.name)
    with _LOCK:
        _CUT_OVER.add(_key(vault_root))
    # A write that landed in the old sidecar between the last pass and the swap.
    _pass(vault_root, plan_, should_stop=lambda: False)
    active = embeddings.get_embedding_index(vault_root)
    recall_space.unload_previous()
    _update(
        vault_root,
        state="current",
        serving=_space(active.identity, active.path),
        target=None,
        eta_seconds=None,
        chunks_pending=0,
    )
    log.info("recall sidecar cut over to %s (%s)", plan_.shadow_path.name, plan_.target.model)


# ---------------------------------------------------------------- retirement


def retire(vault_root: Path, *, keep: Path | None = None) -> list[str]:
    """Remove embedding sidecars that are neither serving nor being built.

    Only after a cutover (the active pointer names a sidecar) and never in the
    process that cut over, so the old sidecar survives until a later start.
    """
    from . import reserved_paths

    doomed = _retirable(vault_root, keep=keep)
    removed: list[str] = []
    with reserved_paths._subsystem_authority_scope("embedding_index"):
        for path in doomed:
            if reserved_paths._remove_owner_file(vault_root, path, "embeddings-store", missing_ok=True):
                removed.append(path.name)
    if removed:
        log.info("retired recall sidecars no longer serving: %s", ", ".join(removed))
    return removed


def _retirable(vault_root: Path, *, keep: Path | None = None) -> list[Path]:
    """The sidecar files `retire` would remove now, journal files first."""
    from . import embeddings

    if _key(vault_root) in _CUT_OVER or index_paths.active_sidecar_name(vault_root) is None:
        return []
    active = embeddings.get_embedding_index(vault_root)
    active_path = active.path
    if active.identity is None:
        return []  # an empty serving sidecar keeps its fallbacks
    kept = {active_path.name} | ({keep.name} if keep is not None else set())
    doomed: list[Path] = []
    for path in active_path.parent.iterdir():
        base = path.name
        for suffix in ("-wal", "-shm", "-journal"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        if base in kept:
            continue
        if base == index_paths.EMBEDDINGS_SIDECAR or index_paths.SPACE_SIDECAR_RE.fullmatch(base):
            doomed.append(path)
    # The database file goes last, after its journal files.
    doomed.sort(key=lambda path: (not path.name.endswith(("-wal", "-shm", "-journal")), path.name))
    return doomed


# ---------------------------------------------------------------- the job


def _wait_for_warm(stop: threading.Event) -> bool:
    from . import readiness

    while readiness.should_defer("embeddings"):
        if stop.wait(_WARM_POLL_SECONDS):
            return False
    return not stop.is_set()


def run(vault_root: Path, stop: threading.Event) -> str:
    """Bring the serving sidecar into the recall encoder's space; the state it ends in."""
    from . import embeddings

    serving_index = embeddings.get_embedding_index(vault_root)
    if _hosted():
        _update(vault_root, state="disabled", serving=_space(serving_index.identity), target=None)
        return "disabled"
    if not _wait_for_warm(stop):
        return "stopped"
    # Whatever happens next, the serving sidecar keeps its own encoder.
    preload_serving_encoder(vault_root)
    if not reembed_enabled():
        _update(
            vault_root,
            state="disabled",
            serving=_space(serving_index.identity, serving_index.path),
            target=None,
        )
        return "disabled"
    plan_ = plan(vault_root)
    if plan_ is None:
        active = embeddings.get_embedding_index(vault_root)
        if active.identity is not None and _retirable(vault_root):
            # A write can land in the old sidecar after the cutover's last
            # catch-up pass; if the process died between the pointer swap and
            # the pass after it, only the old sidecar holds it. One pass over
            # the serving sidecar before the old one goes heals it.
            finished, _encoded = _pass(
                vault_root,
                MigrationPlan(active.identity, active.identity, active.path),
                should_stop=stop.is_set,
            )
            if not finished:
                return "paused"  # the old sidecar stays until a pass completes
        retire(vault_root)
        _update(vault_root, state="current", serving=_space(active.identity, active.path), target=None)
        return "current"
    retire(vault_root, keep=plan_.shadow_path)
    preload_serving_encoder(vault_root)
    if not build(vault_root, plan_, should_stop=stop.is_set):
        return "paused"
    cut_over(vault_root, plan_)
    return "current"


def _run_logged(vault_root: Path, stop: threading.Event) -> None:
    try:
        run(vault_root, stop)
    except Exception as error:  # noqa: BLE001 - the job must never take the service down
        log.warning("recall re-embed stopped: %s", error, exc_info=True)
        _update(vault_root, state="failed", error=type(error).__name__)


def start(vault_root: Path, stop: threading.Event) -> threading.Thread:
    """Run the job on a daemon thread of its own; `stop` ends it between batches."""
    thread = threading.Thread(
        target=_run_logged, args=(vault_root, stop), name=THREAD_NAME, daemon=True
    )
    thread.start()
    return thread


def disk_status(vault_root: Path) -> dict[str, Any]:
    """The serving sidecar's space and any other space sidecar's progress, from disk.

    For a process that is not running the job (doctor). Reads the sidecars'
    records and row counts; walks the vault's index scope to count its pages.
    Loads no model.
    """
    from . import embeddings
    from . import index_paths as paths

    active = embeddings.get_embedding_index(vault_root)
    active_path = active.path
    serving = active.identity if active_path.exists() else None
    result: dict[str, Any] = {
        "serving": _space(serving, active_path),
        "building": None,
        "reembed": "on" if reembed_enabled() else "off",
    }
    if not active_path.parent.is_dir():
        return result
    for candidate in sorted(active_path.parent.iterdir()):
        if candidate == active_path or not paths.SPACE_SIDECAR_RE.fullmatch(candidate.name):
            continue
        shadow = embeddings.get_embedding_index(vault_root, path=candidate)
        identity = shadow.identity
        if identity is None:
            continue
        described = _space(identity, candidate)
        described["paths_done"] = len(shadow.file_mtimes())
        result["building"] = described
        result["paths_total"] = sum(
            1 for md in paths.iter_index_markdown(vault_root) if paths.is_embeddable_path(md)
        )
        break
    return result


def status(vault_root: Path) -> dict[str, Any]:
    """What the job last did for this vault, and which space is serving.

    Content-free: models, fingerprints, sidecar names, counts, seconds.
    """
    with _LOCK:
        current = dict(_STATUS.get(_key(vault_root), {}))
    if not current:
        from . import embeddings

        active = embeddings.get_embedding_index(vault_root)
        current = {"state": "idle", "serving": _space(active.identity, active.path), "target": None}
    current["reembed"] = "on" if reembed_enabled() else "off"
    return current
