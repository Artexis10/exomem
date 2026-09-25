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

The newest `HISTORY_KEEP` snapshots are kept. `versions` lists them and
`read_version` returns one snapshot's override bytes, which a `restore`
validates and saves exactly like a proposal, so a restore is itself a
governed save with its own history entry.

A save made while no override existed snapshots `NO_OVERRIDE`, the smallest
override that changes nothing, so every version is restorable the same way.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from pathlib import Path
from typing import Any

from . import vault as vault_module
from .kbdir import kb_dirname

log = logging.getLogger(__name__)

HISTORY_KEEP = 20
#: What the snapshot of "no override yet" holds: an override that adds
#: nothing, so restoring it reinstates the shipped behaviour.
NO_OVERRIDE = "schema_version: 1\n"
_HEADER_PREFIX = "# exomem-history: "
_VERSION_RE = re.compile(r"^\d{8}T\d{12}Z-[0-9a-f]{8}$")


def history_dir(vault_root: Path, stem: str) -> Path:
    return Path(vault_root) / kb_dirname() / "_Schema" / "history" / stem


def _one_line(text: str) -> str:
    return " ".join(str(text or "").split())


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
) -> dict[str, Any]:
    """Write `rendered` to `path` with its snapshot and log entry in one batch.

    Returns what the caller reports: the snapshot's version and path, and a
    warning when `log.md` is missing (the snapshot still records the reason).
    """
    root = Path(vault_root)
    try:
        previous = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        previous = NO_OVERRIDE
    moment = dt.datetime.now(dt.UTC)
    version = f"{moment.strftime('%Y%m%dT%H%M%S%f')}Z-{before_hash[:8]}"
    reason = _one_line(why) if why else ""
    header = json.dumps(
        {
            "operation": operation,
            "why": reason,
            "before": before_hash[:8],
            "after": after_hash[:8],
            "at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    snapshot = history_dir(root, stem) / f"{version}.yaml"
    writes = [
        vault_module.PlannedWrite(path=path, content=rendered),
        vault_module.PlannedWrite(
            path=snapshot,
            content=f"{_HEADER_PREFIX}{header}\n{previous}",
            create_only=True,
        ),
    ]
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
    vault_module.batch_atomic_write(writes, vault_root=root)
    _prune(root, stem)
    out: dict[str, Any] = {
        "version": version,
        "snapshot": snapshot.relative_to(root).as_posix(),
    }
    if warning:
        out["warning"] = warning
    return out


def _snapshots(vault_root: Path, stem: str) -> list[Path]:
    directory = history_dir(vault_root, stem)
    try:
        entries = [
            entry
            for entry in directory.iterdir()
            if entry.suffix == ".yaml" and _VERSION_RE.match(entry.stem)
        ]
    except (FileNotFoundError, NotADirectoryError):
        return []
    return sorted(entries, key=lambda entry: entry.stem, reverse=True)


def _prune(vault_root: Path, stem: str) -> None:
    """Keep the newest `HISTORY_KEEP`. Best effort: a snapshot that cannot be
    removed now is removed by a later save."""
    for stale in _snapshots(vault_root, stem)[HISTORY_KEEP:]:
        try:
            stale.unlink()
        except OSError:
            log.debug("registry history snapshot not pruned: %s", stale, exc_info=True)


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
    for snapshot in _snapshots(root, stem)[:HISTORY_KEEP]:
        try:
            header, _ = _split(snapshot.read_text(encoding="utf-8"))
        except OSError:
            continue
        out.append(
            {
                "version": snapshot.stem,
                "at": header.get("at"),
                "operation": header.get("operation"),
                "why": header.get("why") or None,
                "before_hash": header.get("before") or snapshot.stem.rsplit("-", 1)[-1],
                "after_hash": header.get("after"),
                "path": snapshot.relative_to(root).as_posix(),
            }
        )
    return out


def read_version(vault_root: Path, *, stem: str, version: str) -> str:
    """The override bytes one kept version holds, without its header line."""
    clean = str(version or "").strip()
    if not _VERSION_RE.match(clean):
        raise ValueError(f"UNKNOWN_REGISTRY_VERSION: {version!r} is not a kept version")
    snapshot = history_dir(vault_root, stem) / f"{clean}.yaml"
    try:
        text = snapshot.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        raise ValueError(f"UNKNOWN_REGISTRY_VERSION: {version!r} is not a kept version") from None
    return _split(text)[1]
