"""Versions and reversible history for the governed registry saves.

`schema_memory save-roles` and `save-conventions` replace a vault-owned
override file. Before close-memory-loop step 5 a save kept neither its reason
nor what it replaced, so an agent's learned cue could not be reviewed or
undone except by hand. Every governed save now does three things in ONE
canonical batch:

* writes the new override;
* snapshots the bytes it replaced at
  `<Knowledge Base>/_Schema/history/<stem>/<utc>-<before8>.yaml`, the first
  line a comment recording the operation, the reason and both hashes;
* prepends `schema_memory <operation>: <why> (<before8> -> <after8>)` to
  `log.md`, which `read_memory(include_history=true)` already reads.

The vocabulary registries (`add-vocabulary-registries`) commit the same way,
and their header also names the principal kind and a hash of its audience, so
`history` can say who made a save without naming them. Under v2 additive
authority the snapshot and the log entry are sealed as derived auxiliaries of
the override write, so the writer gate still classifies one registry change.

The newest `HISTORY_KEEP` snapshots are kept. `versions` lists them and
`read_version` returns one snapshot's override bytes, which a `restore`
validates and saves exactly like a proposal, so a restore is itself a
governed save with its own history entry.

A save made while no override existed snapshots `NO_OVERRIDE`, the smallest
override that changes nothing, so every version is restorable the same way.

The version naming, newest-first listing and guarded single-file read are the
shared per-object history store: `record_history` keeps corrected Records and
Planning rows through the same three functions (`version_id`, `kept_names`,
`read_kept`) under its own directory and suffix.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from . import held_fs
from . import vault as vault_module
from .kbdir import kb_dirname

log = logging.getLogger(__name__)

HISTORY_KEEP = 20
#: What the snapshot of "no override yet" holds: an override that adds
#: nothing, so restoring it reinstates the shipped behaviour.
NO_OVERRIDE = "schema_version: 1\n"
_NO_ROLES_OVERRIDE = "schema_version: 1\nroles: {}\n"
_HEADER_PREFIX = "# exomem-history: "
VERSION_RE = re.compile(r"^\d{8}T\d{12}Z-[0-9a-f]{8}$")


def history_dir(vault_root: Path, stem: str) -> Path:
    return Path(vault_root) / kb_dirname() / "_Schema" / "history" / stem


def version_id(moment: dt.datetime, tag: str) -> str:
    """A kept version's name: its UTC moment, then eight hex of `tag`.

    A tag that is not hex (a registry's `none` before its first overlay) is
    hashed, so every kept name matches `VERSION_RE`."""
    head = tag[:8]
    if not re.fullmatch(r"[0-9a-f]{8}", head):
        head = hashlib.sha256(tag.encode("utf-8")).hexdigest()[:8]
    return f"{moment.strftime('%Y%m%dT%H%M%S%f')}Z-{head}"


def _one_line(text: str) -> str:
    return " ".join(str(text or "").split())


def read_previous(root: Path, path: Path) -> tuple[str | None, vault_module.PathGuard | None]:
    """Inspect the overlay without mutation; bind a guard when the root exists."""
    try:
        return vault_module.read_guarded_text(root, path)
    except FileNotFoundError:
        try:
            root.lstat()
        except FileNotFoundError:
            return None, None
        return None, vault_module.PathGuard.capture(
            root, path.relative_to(root).as_posix(), leaf_policy="absent"
        )


def commit(
    vault_root: Path,
    *,
    path: Path,
    stem: str,
    rendered: str,
    operation: str,
    why: str | None,
    before_hash: str,
    after_hash: str,
    previous: str | None,
    guard: vault_module.PathGuard | None = None,
    added: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, Any]:
    """Write `rendered` to `path` with its snapshot and log entry in one batch.

    A missing root is prepared only here. Its first guard establishes identity;
    an empty root can remain after a refusal.

    `previous` and `guard` come from the same inspection. `before_hash` is
    audit metadata and may describe an effective registry rather than raw bytes.
    `added` names the registry keys this save introduced, so the bootstrap can
    mark them as new without diffing kept versions.

    Returns what the caller reports: the snapshot's version and path, and a
    warning when `log.md` is missing (the snapshot still records the reason).
    """
    root = Path(vault_root)
    if guard is None:
        # Owner-trusted bootstrap can create the root; the first guard binds it.
        vault_module._create_parent_dirs_held(root, root, [])
        guard = vault_module.PathGuard.capture(
            root,
            path.relative_to(root).as_posix(),
            leaf_policy="absent" if previous is None else "content",
            expected_content_hash=(
                None if previous is None else hashlib.sha256(previous.encode("utf-8")).hexdigest()
            ),
        )
    guard.recheck(root)
    if previous is None:
        previous = _NO_ROLES_OVERRIDE if path.name == "context-roles.yaml" else NO_OVERRIDE
    moment = dt.datetime.now(dt.UTC)
    version = version_id(moment, before_hash)
    reason = _one_line(why) if why else ""
    header = json.dumps(
        {
            "operation": operation,
            "why": reason,
            "before": before_hash[:8],
            "after": after_hash[:8],
            "at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
            **_principal_fields(),
            **_added_field(added),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    snapshot = history_dir(root, stem) / f"{version}.yaml"
    override_write = vault_module.PlannedWrite(path=path, content=rendered, guard=guard)
    snapshot_write = vault_module.PlannedWrite(
        path=snapshot,
        content=f"{_HEADER_PREFIX}{header}\n{previous}",
        create_only=True,
        guard=vault_module.PathGuard.capture(
            root, snapshot.relative_to(root).as_posix(), leaf_policy="absent"
        ),
    )
    writes = [override_write, snapshot_write]
    warning = None
    log_plan = vault_module.plan_log_entry(
        root,
        date_iso=moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        op="schema",
        rel_path_no_ext=path.relative_to(root).as_posix(),
        body=(
            f"schema_memory {operation}: {reason or 'no reason recorded'} "
            f"({before_hash[:8]} -> {after_hash[:8]})"
        ),
    )
    if log_plan.warning is not None:
        warning = log_plan.warning
    writes.extend(log_plan.writes)
    from . import vocabulary_auxiliaries

    manifest = vocabulary_auxiliaries.seal(
        root,
        primary=override_write,
        derived=(
            ("registry-history", snapshot_write),
            *(("operation-log", write) for write in log_plan.writes),
        ),
    )
    vault_module.batch_atomic_write(writes, vault_root=root, _vocabulary_auxiliaries=manifest)
    _prune(root, stem)
    out: dict[str, Any] = {
        "version": version,
        "snapshot": snapshot.relative_to(root).as_posix(),
    }
    if warning:
        out["warning"] = warning
    return out


def _added_field(added: Mapping[str, Iterable[str]] | None) -> dict[str, Any]:
    kept = {name: sorted(keys) for name, keys in (added or {}).items() if keys}
    return {"added": kept} if kept else {}


def _principal_fields() -> dict[str, str]:
    """Who saved, as a closed kind and a hash of the audience, never a name."""
    from .governance.principal import current_principal

    principal = current_principal()
    if principal is None:
        return {"principal_kind": "unbound"}
    return {
        "principal_kind": principal.principal_kind,
        "principal": hashlib.sha256(principal.audience_id.encode("utf-8")).hexdigest()[:16],
    }


def plan_snapshot(
    vault_root: Path,
    *,
    path: Path,
    stem: str,
    previous: str | None,
    operation: str,
    before_hash: str,
    after_hash: str,
    added: Mapping[str, Iterable[str]] | None = None,
) -> tuple[str, vault_module.PlannedWrite]:
    """The kept version a write folded into another batch leaves behind.

    For an override written inside a caller's own batch (a capture that
    auto-registers a source kind): the caller adds this write to that batch,
    so the vocabulary and its history land together or not at all. It writes
    no `log.md` entry; the caller's batch carries its own. Returns the
    version id and the write.
    """
    root = Path(vault_root)
    moment = dt.datetime.now(dt.UTC)
    version = version_id(moment, before_hash)
    header = json.dumps(
        {
            "operation": operation,
            "why": "",
            "before": before_hash[:8],
            "after": after_hash[:8],
            "at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
            **_principal_fields(),
            **_added_field(added),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    snapshot = history_dir(root, stem) / f"{version}.yaml"
    empty = _NO_ROLES_OVERRIDE if path.name == "context-roles.yaml" else NO_OVERRIDE
    write = vault_module.PlannedWrite(
        path=snapshot,
        content=f"{_HEADER_PREFIX}{header}\n{previous if previous is not None else empty}",
        create_only=True,
        guard=vault_module.PathGuard.capture(
            root, snapshot.relative_to(root).as_posix(), leaf_policy="absent"
        ),
    )
    return version, write


def _snapshot_names(
    filesystem: held_fs.HeldFilesystem, directory: held_fs.HeldDirectory, suffix: str = ".yaml"
) -> list[str]:
    children = filesystem.children(directory)
    if not children.ok:
        return []
    return sorted(
        (
            child.relative_path
            for child in children.require()
            if child.identity.kind == "file"
            and child.identity.link_count == 1
            and child.relative_path.endswith(suffix)
            and VERSION_RE.match(child.relative_path[: -len(suffix)])
        ),
        reverse=True,
    )


def _open_parent(
    filesystem: held_fs.HeldFilesystem, relative: str, *, mutate: bool = False
) -> held_fs.HeldDirectory | None:
    opened = filesystem.parent(relative, access="mutate" if mutate else "read")
    return opened.require() if opened.ok else None


def _history_parent(
    filesystem: held_fs.HeldFilesystem, vault_root: Path, stem: str, *, mutate: bool = False
) -> held_fs.HeldDirectory | None:
    relative = history_dir(vault_root, stem).relative_to(vault_root).as_posix()
    return _open_parent(filesystem, relative, mutate=mutate)


def kept_names(vault_root: Path, relative_dir: str, *, suffix: str) -> list[str] | None:
    """Every kept version's file name under `relative_dir`, newest first.

    Names only: no kept payload is opened. `None` when the directory does not
    exist or cannot be opened safely, so a caller can tell "nothing kept" from
    an empty list it could not read.
    """
    acquired = held_fs.acquire(Path(vault_root))
    if not acquired.ok:
        return None
    with acquired.require() as filesystem:
        directory = _open_parent(filesystem, relative_dir)
        if directory is None:
            return None
        with directory:
            return _snapshot_names(filesystem, directory, suffix)


def read_kept(vault_root: Path, relative_dir: str, name: str) -> str | None:
    """One kept file's text, read through the held vault root, or None."""
    acquired = held_fs.acquire(Path(vault_root))
    if not acquired.ok:
        return None
    with acquired.require() as filesystem:
        directory = _open_parent(filesystem, relative_dir)
        if directory is None:
            return None
        with directory:
            opened = filesystem.file(directory, name)
            if not opened.ok:
                return None
            with opened.require() as file:
                read = filesystem.read(file) if file.identity.link_count == 1 else None
                if read is None or not read.ok or not filesystem.validate_directory(directory).ok:
                    return None
                try:
                    return read.require().decode("utf-8")
                except UnicodeDecodeError:
                    return None


def _prune(vault_root: Path, stem: str) -> None:
    """Keep the newest `HISTORY_KEEP`. Best effort: a snapshot that cannot be
    removed now is removed by a later save."""
    root = Path(vault_root)
    acquired = held_fs.acquire(root)
    if not acquired.ok:
        return
    with acquired.require() as filesystem:
        directory = _history_parent(filesystem, root, stem, mutate=True)
        if directory is None:
            return
        with directory:
            for stale in _snapshot_names(filesystem, directory)[HISTORY_KEEP:]:
                opened = filesystem.file(directory, stale, access="mutate")
                if not opened.ok:
                    continue
                with opened.require() as file:
                    if file.identity.link_count != 1:
                        continue
                    if not filesystem.validate_directory(directory).ok:
                        return
                    if not filesystem.unlink(file).ok:
                        log.debug("registry history snapshot not pruned: %s", stale)


def _split(text: str) -> tuple[dict[str, Any], str]:
    first, _, rest = text.partition("\n")
    if not first.startswith(_HEADER_PREFIX):
        return {}, text
    try:
        header = json.loads(first[len(_HEADER_PREFIX) :])
    except json.JSONDecodeError:
        header = {}
    return (header if isinstance(header, dict) else {}), rest


def versions(vault_root: Path, *, stem: str) -> list[dict[str, Any]]:
    """The kept versions, newest first. Each names the state a save replaced:
    `version` restores it, `why` and the hashes say which save replaced it."""
    root = Path(vault_root)
    out: list[dict[str, Any]] = []
    acquired = held_fs.acquire(root)
    if not acquired.ok:
        return out
    with acquired.require() as filesystem:
        directory = _history_parent(filesystem, root, stem)
        if directory is None:
            return out
        with directory:
            for name in _snapshot_names(filesystem, directory)[:HISTORY_KEEP]:
                opened = filesystem.file(directory, name)
                if not opened.ok:
                    continue
                with opened.require() as file:
                    if file.identity.link_count != 1:
                        continue
                    read = filesystem.read(file)
                    if not read.ok:
                        continue
                    if not filesystem.validate_directory(directory).ok:
                        continue
                    try:
                        header, _ = _split(read.require().decode("utf-8"))
                    except UnicodeDecodeError:
                        continue
                version = name[:-5]
                out.append(
                    {
                        "version": version,
                        "at": header.get("at"),
                        "operation": header.get("operation"),
                        "why": header.get("why") or None,
                        "before_hash": header.get("before") or version.rsplit("-", 1)[-1],
                        "after_hash": header.get("after"),
                        "principal_kind": header.get("principal_kind"),
                        "principal": header.get("principal"),
                        "added": _read_added(header.get("added")),
                        "path": (history_dir(root, stem) / name).relative_to(root).as_posix(),
                    }
                )
    return out


def history_view(vault_root: Path, *, stem: str) -> dict[str, Any]:
    """Project admitted history metadata with private fields for the bound owner only."""
    from .governance import egress
    from .governance.principal import effective_principal
    from .governance.raw_protection import is_owner

    items = versions(vault_root, stem=stem)
    refusal = egress.owner_only_aggregate(vault_root)
    if is_owner(effective_principal()) and refusal is None:
        return {"versions": items}
    # These history protocol fields belong only to the explicitly bound owner.
    owner_fields = ("why", "principal", "principal_kind")
    return {
        "versions": [
            {key: value for key, value in item.items() if key not in owner_fields}
            for item in items
        ],
        "withheld": {
            "fields": list(owner_fields),
            "reason": (refusal or {}).get("reason") or "audience_restricted",
        },
    }


def _read_added(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    return {
        str(name): [str(key) for key in keys]
        for name, keys in value.items()
        if isinstance(keys, list)
    }


def read_version(vault_root: Path, *, stem: str, version: str) -> str:
    """The override bytes one kept version holds, without its header line."""
    clean = str(version or "").strip()
    if not VERSION_RE.match(clean):
        raise ValueError(f"UNKNOWN_REGISTRY_VERSION: {version!r} is not a kept version")
    root = Path(vault_root)
    relative = history_dir(root, stem).relative_to(root).as_posix()
    text = read_kept(root, relative, f"{clean}.yaml")
    if text is None:
        raise ValueError(f"UNKNOWN_REGISTRY_VERSION: {version!r} is not a kept version")
    return _split(text)[1]
