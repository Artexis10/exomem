"""Keep one version-bound JPEG per original filing in private derived state."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Generator, Mapping, Sequence
from contextlib import nullcontext
from itertools import islice
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, BinaryIO

from . import media_engines, media_types, originals, private_state, reserved_paths, state_paths

if TYPE_CHECKING:
    from .media_jobs import MediaJob, MediaJobStore

log = logging.getLogger(__name__)

STORE = reserved_paths.MEDIA_PREVIEWS_DIRNAME
_INTENT_DIGEST = "preview_requested_sha256"
MAX_RECORD_BYTES = 64 * 1024
DECODE_SECONDS = 15
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


def stamp_intent(
    content: str, relative: str, digest: str | None, *, previous: str | None = None, admit: bool = True,
) -> str:
    """Carry existing demand across media rewrites, and mark a new or rebound filing."""
    from . import preserve, vault

    if digest is None or not supports(relative):
        return content
    prior = vault.parse_frontmatter(previous)[0] if previous is not None else {}
    old_binding = originals.binary_binding(prior)
    changed = previous is None or (old_binding is not None and (old_binding.path, old_binding.sha256) != (relative, digest))
    carried = requested_digest(prior, relative) == digest
    if not carried and not (admit and changed and media_engines.preview_generation_enabled()):
        return content

    fields = vault.parse_frontmatter(content)[0]
    binding = originals.binary_binding(fields)
    if fields.get("parent_media") or binding is None or (binding.path, binding.sha256) != (relative, digest):
        return content
    if fields.get(_INTENT_DIGEST) != digest:
        return preserve._set_frontmatter_field(content, _INTENT_DIGEST, f'"{digest}"')
    return content


def requested_digest(fields: dict[str, object], relative: str) -> str | None:
    if _INTENT_DIGEST not in fields or fields.get("parent_media") or not supports(relative):
        return None
    binding = originals.binary_binding(fields)
    if binding is not None and binding.path == relative and fields[_INTENT_DIGEST] == binding.sha256:
        return binding.sha256
    return None


def _current_intent(
    vault: Path, relative: str, digest: str | None, *, companions: Mapping[str, bytes] | None = None,
) -> tuple[Path, originals.BinaryBinding] | None:
    from . import recall_policy

    companion = originals.bound_companion(vault, relative, companions=companions)
    if companion is None or digest is None or requested_digest(companion[1], relative) != digest:
        return None
    sidecar = vault / companion[0]
    admitted = (recall_policy.is_recall_candidate(vault, sidecar) if companions is None
                else recall_policy.admits_markdown_content(vault, companion[0], companion[2]))
    if not admitted:
        return None
    binding = originals.binary_binding(companion[1])
    assert binding is not None
    return sidecar, binding


def enqueue(vault: Path, binary: Path, *, expected_sha256: str | None) -> bool:
    """Queue marked work. A held ledger leaves the filing and its demand durable."""
    from . import media_jobs
    from .writer_lease import get_manager

    if expected_sha256 is None or not supports(binary) or not media_engines.preview_generation_enabled():
        return True
    try:
        relative = binary.relative_to(vault).as_posix()
        manager = get_manager()
        boundary = (nullcontext() if manager.holds_mutation_boundary(vault) else
                    manager.consistency_guard(vault, operation="media_preview_admit", holder_kind="background"))
        with boundary:
            intent = _current_intent(vault, relative, expected_sha256)
            if intent is not None:
                sidecar, binding = intent
                media_jobs.MediaJobStore(vault).enqueue_preview(binary, sidecar, binding.sha256)
    except Exception as error:  # noqa: BLE001 - optional admission cannot fail the filing or extraction
        log.warning("media preview admission deferred (%s)", type(error).__name__)
        return False
    return True


def recover(
    vault: Path, store: MediaJobStore | None, candidates: Sequence[tuple[Path, Path, str]],
) -> MediaJobStore | None:
    """Recover the marked candidates from one bounded discovery pass."""
    from . import media_jobs
    from .vault import MALFORMED_FRONTMATTER_ERRORS
    from .vault_store import read as store_read
    from .writer_lease import get_manager

    root = state_paths.vault_state_dir(vault) / STORE

    def missing_record(relative: str) -> bool:
        return not os.path.lexists(_record_path(root, relative))

    try:
        missing = store.recoverable_preview_demands(candidates, missing_record=missing_record) if store else candidates
        if not missing:
            return store
        manager = get_manager()
        manager.yield_to_waiting_writes(vault)
        with manager.consistency_guard(vault, operation="media_preview_recover", holder_kind="background"):
            paths = {
                binary: originals.companion_paths(binary.relative_to(vault).as_posix())
                for binary, _, _ in missing
            }
            served = store_read.serves_many(vault, {path for names in paths.values() for path in names})
            stored = None
            if any(served.values()):
                with store_read.snapshot(vault, join_request=False) as view:
                    stored = view.bodies(path for path, owned in served.items() if owned)

            current = []
            rebuild = set()
            for binary, sidecar, digest in missing:
                relative = binary.relative_to(vault).as_posix()
                companions = stored if all(served[path] for path in paths[binary]) else None
                try:
                    intent = _current_intent(vault, relative, digest, companions=companions)
                except (*MALFORMED_FRONTMATTER_ERRORS, reserved_paths.ReservedPathLeafError):
                    continue
                if intent is not None and intent[0] == sidecar:
                    current.append((binary, sidecar, digest))
                    if missing_record(relative):
                        rebuild.add(binary)
            if current:
                if store is None:
                    store = media_jobs.MediaJobStore(vault)
                store.enqueue_previews(current, rebuild=frozenset(rebuild))
    except Exception as error:  # noqa: BLE001 - the next discovery rotation retries optional work
        log.warning("media preview recovery deferred (%s)", type(error).__name__)
    return store


def _decode(source: BinaryIO, kind: str, suffix: str, inline_bytes: int, deadline: float) -> bytes:
    """Run one codec with a bounded private stdout and the original as seekable stdin."""
    from . import media_brakes

    if os.name != "posix":
        raise OSError("preview codec limits are unavailable on this platform")
    config = media_brakes.settings()
    memory_limit = config.budget_for(media_engines.PREVIEWS).vmdata_bytes + config.margin_bytes
    latest = min(deadline, time.monotonic() + DECODE_SECONDS)
    if latest <= time.monotonic() or inline_bytes < 4:
        raise ValueError("preview codec budget exhausted")
    command = [sys.executable, "-m", "exomem.media_preview_child", kind, suffix,
               str(inline_bytes), str(memory_limit), str(latest)]
    with tempfile.TemporaryFile() as output:
        subprocess.run(
            command, stdin=source, stdout=output, stderr=subprocess.DEVNULL, check=True,
            env={**os.environ, "OPENBLAS_NUM_THREADS": "1"},
            timeout=max(0, latest - time.monotonic()),
        )
        output.seek(0)
        max_bytes = (inline_bytes // 4) * 3
        jpeg = output.read(max_bytes + 1)
        if len(jpeg) > max_bytes or not (jpeg.startswith(b"\xff\xd8") and jpeg.endswith(b"\xff\xd9")):
            raise ValueError("invalid preview codec output")
        return jpeg


def generate(
    vault: Path,
    job: MediaJob,
    store: MediaJobStore,
    *,
    deadline: float | None,
) -> bool:
    """Render outside the publication hold, then recheck the held bytes and their filing."""
    from . import image_view, media_brakes, media_store
    from .writer_lease import get_manager

    relative = job.binary_path.relative_to(vault).as_posix()
    kind = media_types.media_type_for(relative)
    assert kind is not None
    if deadline is None:
        deadline = time.monotonic() + media_brakes.settings().job_timeout_seconds
    manager = get_manager()
    manager.yield_to_waiting_writes(vault)
    with manager.consistency_guard(vault, operation="media_preview_claim", holder_kind="background"):
        intent = _current_intent(vault, relative, job.preview_sha256)
        if intent is None or intent[0] != job.sidecar_path or not store.preview_claimed(job):
            return False
        expected = intent[1]
        current = originals.original_state(vault, relative)
        if not current.present or current.binary_sha256 != expected.sha256:
            return False
    with media_store.open_original(vault, relative, job=job, deadline=deadline) as opened:
        if kind == "image" and opened.version.size > image_view.MAX_ORIGINAL_BYTES:
            return False
        held = reserved_paths.prove_held_binary(
            vault, relative, opened, deadline=deadline,
            recorded_sha256=lambda version: media_store.cached_sha256(vault, job.binary_path, version),
        )
        if (held.sha256, held.size) != (expected.sha256, expected.size):
            return False
        record = {"version": _FORMAT, "path": relative, "sha256": held.sha256, "jpeg": ""}
        jpeg = read(vault, relative, held.sha256)
        if jpeg is None:
            inline_bytes = MAX_RECORD_BYTES - len(json.dumps(record))
            opened.stream.seek(0)
            jpeg = _decode(opened.stream, kind, job.binary_path.suffix, inline_bytes, deadline)
        record["jpeg"] = base64.b64encode(jpeg).decode("ascii")

        def install() -> bool:
            if _current_intent(vault, relative, job.preview_sha256) != (job.sidecar_path, expected):
                return False
            reserved_paths.require_generic_copy_source_version(held)
            root = private_state.store(vault, STORE)
            path = _record_path(root, relative)
            path.parent.mkdir(mode=0o700, exist_ok=True)
            if not stat.S_ISDIR(path.parent.lstat().st_mode):
                return False
            if read(vault, relative, held.sha256) is None:
                private_state.write_record(path.parent, path.stem, record, temp_suffix=_TEMP_SUFFIX, durable=True)
            else:
                # The previous install may have stopped between rename and directory sync.
                with path.open("rb") as cached:
                    os.fsync(cached.fileno())
            private_state.sync_directory(path.parent)
            private_state.sync_directory(root)
            private_state.sync_directory(root.parent)
            return True

        manager.yield_to_waiting_writes(vault)
        with manager.consistency_guard(vault, operation="media_preview_publish", holder_kind="background"):
            return store.publish_preview(job, install)


def show(vault: Path, selected: str) -> tuple[dict, bytes | None]:
    """Return a stored JPEG only after a live authorization for its original and pixels."""
    from . import access, artifact_recovery
    from .governance import egress
    from .governance.principal import request_scope

    who = artifact_recovery.delegated_principal()
    proofs: dict = {}

    path = PurePosixPath(selected)
    if (path.is_absolute() or path.as_posix() != selected or ".." in path.parts
            or "\\" in selected or not supports(selected) or not who.resolved):
        raise artifact_recovery.RecoveryUnavailable

    def possible() -> bool:
        with request_scope(who):
            return egress.preview_may_materialize(vault, selected, who)

    def allows_pixels(held: reserved_paths.GenericFileCopy) -> bool:
        relative = held.relative_path
        if access.refuse_if_excluded(vault, relative):
            return False
        if media_types.media_type_for(relative) == "video":
            return egress.release_allows_frames(vault, relative, principal=who, artifact_copy=held)
        return egress.content_permits(vault, relative, who, derived=True, artifact_copy=held)

    with artifact_recovery.stage(
        vault, selected, who, byte_proofs=proofs, canonical=True, preflight=possible, additional_release=allows_pixels,
    ) as (held, descriptor):
        jpeg = read(vault, selected, held.sha256)
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
