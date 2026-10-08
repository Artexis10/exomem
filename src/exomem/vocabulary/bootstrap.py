"""The bootstrap `vocabulary` block: the live registries, by use, with what is new.

For each registry the block lists at most `TOP_KEYS` active keys, ordered by
use and carrying their counts, from a current projection. Without one there is
no order by use: it lists active keys in registry order and names the reason
under `unavailable`. `more` counts the active keys a listing left
out. It never reports a zero it did not count.

A key a save or an auto-registration added within `NEW_WINDOW_DAYS` is listed
under `new`, at most `NEW_KEYS` per registry, with the window's start date in
`new_since`. The marker is how an agent that did not make a promotion learns
about it. Fourteen days is long enough that a client the owner opens weekly
still sees a promotion at least once, and short enough that `new` stays a list
of recent changes rather than a second copy of the registry. It asks for
nothing: an agent reads it at session start and carries on.
"""

from __future__ import annotations

import datetime as dt
import os
import threading
from pathlib import Path
from typing import Any

from . import registry, registry_specs

TOP_KEYS = 12
NEW_WINDOW_DAYS = 14
NEW_KEYS = 3

_ADDED_CACHE: dict[tuple[str, str, str], tuple[tuple[str, dict[str, list[str]]], ...]] = {}
_ADDED_LOCK = threading.Lock()


def _additions(
    vault_root: Path, stem: str, content_hash: str
) -> tuple[tuple[str, dict[str, list[str]]], ...]:
    """`(at, added)` per kept version of one overlay, cached on the overlay's hash."""
    from .. import registry_history

    key = (os.path.abspath(vault_root), stem, content_hash)
    with _ADDED_LOCK:
        cached = _ADDED_CACHE.get(key)
    if cached is not None:
        return cached
    rows = tuple(
        (str(item.get("at") or ""), dict(item.get("added") or {}))
        for item in registry_history.versions(vault_root, stem=stem)
    )
    with _ADDED_LOCK:
        if len(_ADDED_CACHE) > 256:
            _ADDED_CACHE.clear()
        _ADDED_CACHE[key] = rows
    return rows


def recently_added(
    vault_root: Path, spec: registry.RegistrySpec, snapshot: registry.Snapshot, *, since: dt.date
) -> list[str]:
    """Active keys of one registry that a write added on or after `since`."""
    found: list[str] = []
    for at, added in _additions(vault_root, spec.stem, snapshot.content_hash):
        if at[:10] < since.isoformat():
            continue
        for key in added.get(spec.name, ()):
            entry = snapshot.entries.get(key)
            if entry is not None and entry.status == "active" and key not in found:
                found.append(key)
    return sorted(found)


def block(vault_root: Path, *, inspect_route: str, today: dt.date | None = None) -> dict[str, Any]:
    """The bootstrap's view of every vocabulary registry."""
    since = (today or dt.datetime.now(dt.UTC).date()) - dt.timedelta(days=NEW_WINDOW_DAYS)
    from .contract import admission_refusal, history_refusal

    registries: dict[str, Any] = {}
    for name, spec in registry_specs().items():
        refusal = admission_refusal(vault_root, spec)
        if refusal is not None:
            registries[name] = {"unavailable": refusal["reason"]}
            continue
        snapshot = registry.load(spec, vault_root)
        active = [entry for entry in snapshot.entries.values() if entry.status == "active"]
        row: dict[str, Any] = {}
        usage = spec.usage(vault_root, snapshot) if spec.usage is not None else None
        if usage is not None and usage.available:
            ordered = sorted(active, key=lambda entry: (-usage.counts.get(entry.key, 0), entry.key))
            row["top"] = {entry.key: usage.counts.get(entry.key, 0) for entry in ordered[:TOP_KEYS]}
            row["count_source"] = usage.source
        else:
            row["top"] = [entry.key for entry in active[:TOP_KEYS]]
            row["unavailable"] = usage.reason if usage is not None else "not_counted"
        listed = len(row.get("top", ()))
        if listed and len(active) > listed:
            row["more"] = f"+{len(active) - listed} more"
        row["findings"] = len(snapshot.findings)
        new = (
            recently_added(vault_root, spec, snapshot, since=since)
            if history_refusal(vault_root, spec) is None
            else []
        )
        if new:
            row["new"] = new[:NEW_KEYS] + (
                [f"+{len(new) - NEW_KEYS} more"] if len(new) > NEW_KEYS else []
            )
        registries[name] = row
    # Registry names are hyphenated, so they never meet the two plain keys.
    out: dict[str, Any] = {**registries, "inspect": inspect_route}
    if any("new" in row for row in registries.values()):
        out["new_since"] = since.isoformat()
    return out
