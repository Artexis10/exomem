"""Keep one version-bound JPEG per original filing in private derived state."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import stat
import threading
import time
from collections.abc import Callable, Generator
from contextlib import nullcontext
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING

from . import media_types, originals, private_state, reserved_paths, state_paths

if TYPE_CHECKING:
    from .media_jobs import MediaJob

log = logging.getLogger(__name__)

STORE = reserved_paths.MEDIA_PREVIEWS_DIRNAME
MAX_RECORD_BYTES = 64 * 1024
_MAX_SIDE = 512
_FORMAT = 1
_TEMP_SUFFIX = ".tmp"
_SWEEP_LIMIT = 64
_SWEEP_LOCK = threading.Lock()
_sweep: tuple[Path, Generator[Path, None, None]] | None = None


def supports(path: str | Path) -> bool:
    kind = media_types.media_type_for(path)
    return kind == "image" or kind == "video"


def _record_path(root: Path, relative: str) -> Path:
    key = hashlib.sha256(relative.encode("utf-8")).hexdigest()
    return root / key[:2] / f"{key}.json"


def _load(path: Path) -> dict | None:
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return None
        with path.open("rb") as source:
            if os.fstat(source.fileno()).st_size > MAX_RECORD_BYTES:
                return None
            record = json.loads(source.read(MAX_RECORD_BYTES))
        if isinstance(record, dict):
            return record
    except (FileNotFoundError, ValueError, RecursionError):
        pass
    return None


def read(vault: Path, relative: str, digest: str) -> bytes | None:
    """Look up exactly one private record for a caller that has proved this original."""
    try:
        record = _load(_record_path(state_paths.vault_state_dir(vault) / STORE, relative))
        if record is None or record.get("version") != _FORMAT:
            return None
        if record.get("path") != relative or record.get("sha256") != digest:
            return None
        jpeg = base64.b64decode(record.get("jpeg"), validate=True)
        # SOI and EOI are fixed JPEG markers. Reading a preview never opens a codec.
        if jpeg.startswith(b"\xff\xd8") and jpeg.endswith(b"\xff\xd9"):
            return jpeg
    except (OSError, ValueError, TypeError):
        pass
    return None


def generate(
    vault: Path,
    job: MediaJob,
    source: reserved_paths.GenericBinarySource | None,
    publish: Callable[[Callable[[], None]], object],
) -> None:
    """Render outside the publication hold, then recheck the held bytes and their filing."""
    from . import image_view, media_brakes, media_store, recall_policy

    relative = job.binary_path.relative_to(vault).as_posix()
    with (nullcontext(source) if source is not None else media_store.open_original(vault, relative, job=job)) as opened:
        if job.media_type == "image" and opened.version.size > image_view.MAX_ORIGINAL_BYTES:
            return
        deadline = time.monotonic() + media_brakes.settings().job_timeout_seconds
        held = reserved_paths.prove_held_binary(
            vault, relative, opened, deadline=deadline,
            recorded_sha256=lambda version: media_store.cached_sha256(vault, job.binary_path, version),
        )
        expected = originals.BinaryBinding(relative, held.sha256, held.size)

        def current_filing() -> bool:
            companion = originals.bound_companion(vault, relative)
            return (companion is not None and vault / companion[0] == job.sidecar_path
                    and not companion[1].get("parent_media")
                    and originals.binary_binding(companion[1]) == expected
                    and recall_policy.is_recall_candidate(vault, job.sidecar_path))

        if not current_filing() or read(vault, relative, held.sha256) is not None:
            return
        record = {"version": _FORMAT, "path": relative, "sha256": held.sha256, "jpeg": ""}
        inline_bytes = MAX_RECORD_BYTES - len(json.dumps(record))
        opened.stream.seek(0)
        if job.media_type == "image":
            jpeg = image_view.render_preview(
                opened.stream, job.binary_path.suffix, max_side=_MAX_SIDE, inline_bytes=inline_bytes,
            )
        else:
            from . import video_frames

            result = video_frames.sample_stream(
                opened.stream, relative, max_frames=1, max_side=_MAX_SIDE,
                resize_candidates=True, inline_bytes=inline_bytes,
            )
            jpeg = result.frames[0].jpeg
        record["jpeg"] = base64.b64encode(jpeg).decode("ascii")

        def install() -> None:
            if not current_filing() or read(vault, relative, held.sha256) is not None:
                return
            reserved_paths.require_generic_copy_source_version(held)
            root = private_state.store(vault, STORE)
            path = _record_path(root, relative)
            path.parent.mkdir(mode=0o700, exist_ok=True)
            if not stat.S_ISDIR(path.parent.lstat().st_mode):
                return
            private_state.write_record(path.parent, path.stem, record, temp_suffix=_TEMP_SUFFIX, durable=True)
            private_state.sync_directory(path.parent)

        publish(install)


def show(vault: Path, selected: str) -> tuple[dict, bytes | None]:
    """Return a stored JPEG only after a live authorization for its original and pixels."""
    from . import access, artifact_recovery
    from .governance import egress

    who = artifact_recovery.delegated_principal()
    proofs: dict = {}

    def allows_pixels(held: reserved_paths.GenericFileCopy) -> bool:
        relative = held.relative_path
        if access.refuse_if_excluded(vault, relative):
            return False
        if media_types.media_type_for(relative) == "video":
            return egress.release_allows_frames(vault, relative, principal=who, artifact_copy=held)
        return egress.content_permits(vault, relative, who, derived=True, artifact_copy=held)

    with artifact_recovery.stage(
        vault, selected, who, byte_proofs=proofs, additional_release=allows_pixels,
    ) as (held, descriptor):
        relative = descriptor["path"]
        jpeg = read(vault, relative, held.sha256) if supports(relative) else None
        artifact_recovery.require_current(
            vault, held, who, byte_proofs=proofs, selected=selected, additional_release=allows_pixels,
        )
        return descriptor, jpeg


def _obsolete(vault: Path, root: Path, path: Path) -> bool:
    record = _load(path)
    if record is None:
        return True
    relative = record.get("path")
    if not isinstance(relative, str) or _record_path(root, relative) != path:
        return True
    current = originals.original_state(vault, relative)
    return not current.present or current.binary_sha256 != record.get("sha256")


def _retire(vault: Path, root: Path, path: Path) -> None:
    from .writer_lease import get_manager

    manager = get_manager()
    boundary = (nullcontext() if manager.holds_mutation_boundary(vault) else
                manager.consistency_guard(vault, operation="media_preview_retire", holder_kind="background"))
    with boundary:
        # The same mutation boundary guards installation. A delayed removal can see a new record here.
        if path.suffix == _TEMP_SUFFIX or _obsolete(vault, root, path):
            path.unlink(missing_ok=True)


def retire(vault: Path, relative: str) -> None:
    """Retire the old preview after removal, unless its filing still holds the recorded original."""
    root = state_paths.vault_state_dir(vault) / STORE
    path = _record_path(root, relative)
    if path.exists():
        _retire(vault, root, path)


def _scan(root: Path) -> Generator[Path, None, None]:
    try:
        with os.scandir(root) as shards:
            for shard in shards:
                if shard.is_dir(follow_symlinks=False):
                    with os.scandir(shard.path) as entries:
                        yield from (Path(entry.path) for entry in entries)
    except FileNotFoundError:
        return


def reconcile(vault: Path) -> None:
    """Walk at most one bounded slice of preview state each pass."""
    global _sweep
    root = state_paths.vault_state_dir(vault) / STORE
    with _SWEEP_LOCK:
        # One media runtime serves one vault. Switching vaults releases the old scan handle.
        if _sweep is None or _sweep[0] != root:
            if _sweep is not None:
                _sweep[1].close()
            _sweep = (root, _scan(root))
        paths = list(islice(_sweep[1], _SWEEP_LIMIT))
        if len(paths) < _SWEEP_LIMIT:
            _sweep = None
    for path in paths:
        try:
            if path.suffix == _TEMP_SUFFIX or (path.suffix == ".json" and _obsolete(vault, root, path)):
                _retire(vault, root, path)
        except Exception as error:  # noqa: BLE001 - leave an unknown or contended record for the next pass
            log.warning("preview reconciliation deferred (%s)", type(error).__name__)
