"""Owner changes to the local eviction policy of one vault."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from . import freshness, held_fs, media_bindings, media_capacity, media_store, originals, writer_lease


def _refusal(reason: str) -> media_store.MediaStoreError:
    return media_store.MediaStoreError("MEDIA_EVICTION_CONFIGURATION_REFUSED", reason)


def _policy(vault: Path) -> dict[str, object]:
    if not originals.uses_local_configuration():
        raise _refusal("owner eviction commands need a personal install with no explicit media store configuration path")
    config = originals.configuration(vault)
    if config is None:
        raise _refusal("configure and declare a filesystem store before changing eviction")
    return config


def _replace(vault: Path, expected: dict[str, object], policy: dict[str, object]) -> None:
    """Replace the held state file with one changed entry, preserving any other entries."""
    from .cli_ops import OpError

    path = originals.local_configuration_path(vault)
    try:
        with (media_capacity.allocation_policy(vault),
              held_fs.acquire(path.parent).require() as filesystem,
              filesystem.parent(".", access="flush").require() as parent):
            with filesystem.file(parent, path.name).require() as file:
                raw = filesystem.read(file).require()
                identity = file.identity
            document = json.loads(raw)
            if originals.configuration_entry(vault, document) != expected:
                raise media_store.MediaStoreError(media_store.STORE_BUSY_CODE, "the local media store policy changed")
            document["vaults"][str(vault)] = policy
            data = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            held_fs.publish_bytes(filesystem, parent, path.name, data, expected_identity=identity,
                                  expected_sha256=hashlib.sha256(raw).hexdigest()).require()
            filesystem.flush_directory(parent).require()
    except OpError as error:
        if error.code != media_capacity.UPLOAD_SPACE_BUSY:
            raise
        raise media_store.MediaStoreError(media_store.STORE_BUSY_CODE, "vault storage is allocating space; retry") from error


def _select(config: dict[str, object], entry: originals.StoreEntry) -> dict[str, object]:
    """Project the filesystem connection into the existing top-level selected store fields."""
    selected = str(config["store"])
    if entry.name == selected:
        return dict(config)
    # These are the filesystem adapter's connection fields. The other top-level fields belong to the vault.
    connection_fields = ("adapter", "root", "max_object_bytes")
    policy = {key: value for key, value in config.items() if key not in connection_fields}
    policy.update({key: entry.connection[key] for key in connection_fields if key in entry.connection})
    declared = config[originals.STORES]
    assert isinstance(declared, list)
    entries = []
    for item in declared:
        if isinstance(item, dict) and item.get("name") == selected:
            item = {key: value for key, value in item.items() if key not in connection_fields}
            item.update({key: config[key] for key in connection_fields if key in config})
        entries.append(item)
    policy.update(store=entry.name, stores=entries)
    return policy


def enable(
    vault: Path,
    store: str,
    *,
    free_bytes: int,
    margin_bytes: int | None = None,
    upload_floor_bytes: int | None = None,
    minimum_idle_seconds: int = 600,
    eviction_settle_seconds: int = 600,
    max_temporary_bytes: int | None = None,
    max_hydrations: int | None = None,
) -> None:
    vault = Path(vault).resolve()
    config = _policy(vault)
    entry = originals.store_entry(config, store)
    if entry is None or entry.connection.get("adapter") not in (media_store.FILESYSTEM_ADAPTER,
                                                                  media_store.FILESYSTEM_ADAPTER_ALIAS):
        raise _refusal("name a declared filesystem store")
    total = shutil.disk_usage(vault).total
    margin = max(1, free_bytes // 10) if margin_bytes is None else margin_bytes
    upload_floor = free_bytes // 2 if upload_floor_bytes is None else upload_floor_bytes
    if not (0 < free_bytes < total and 0 < margin < total - free_bytes and 0 <= upload_floor < free_bytes):
        raise ValueError("free space, eviction margin and upload floor must fit on the vault volume")
    if minimum_idle_seconds < 0 or eviction_settle_seconds < 0:
        raise ValueError("idle and settle times must be nonnegative")
    target = (total - free_bytes) / total
    policy = _select(config, entry)
    policy.update(
        enabled=True, eviction_enabled=True, eviction_target=target, eviction_margin=margin / total,
        growth_threshold=target, upload_floor_bytes=upload_floor, minimum_idle_seconds=minimum_idle_seconds,
        eviction_settle_seconds=eviction_settle_seconds,
        max_temporary_bytes=(config.get("max_temporary_bytes", 2 * 1024**3)
                             if max_temporary_bytes is None else max_temporary_bytes),
        max_hydrations=config.get("max_hydrations", 2) if max_hydrations is None else max_hydrations,
    )
    if (problem := media_capacity.hydration_policy_problem(policy)) is not None:
        raise ValueError(problem)

    generation = writer_lease.read_commit_generation(vault)
    checkpoint = media_bindings.source_checkpoint(vault)
    if generation is None:
        raise _refusal("the current vault generation cannot be read")
    if (conflict := media_bindings.store_role_conflict(vault, str(config["store"]), store)) is not None:
        raise _refusal(conflict)
    with media_store._guard(vault, "personal_eviction_enable"):
        if (writer_lease.read_commit_generation(vault) != generation or freshness.external_pending(vault)
                or media_bindings.source_checkpoint(vault) != checkpoint or _policy(vault) != config):
            raise media_store.MediaStoreError(media_store.STORE_BUSY_CODE, "the vault changed while checking the store; retry")
        backend = media_store._configured_store(vault, config, store)
        if not isinstance(backend, media_store.FilesystemStore) or backend.store_id is None:
            raise _refusal("declare the filesystem store before enabling eviction")
        backend.require_identity()
        _replace(vault, config, policy)


def disable(vault: Path) -> str:
    vault = Path(vault).resolve()
    with media_store._guard(vault, "personal_eviction_disable"):
        config = _policy(vault)
        _replace(vault, config, {**config, "enabled": False, "eviction_enabled": False})
        return str(config["store"])
