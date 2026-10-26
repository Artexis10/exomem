"""The bootstrap `vocabulary` block: the live registries, by use, with what is new.

For each registry the block lists at most `TOP_KEYS` active keys, ordered by
use and carrying their counts, from a current projection. Without one there is
no order by use: it lists active keys in registry order and names the reason
under `unavailable`. `more` counts the active keys a listing left
out. It never reports a zero it did not count. A registry whose spec sets
`summarize_keys` to false lists no keys. It still reports its findings, its new
keys and `unavailable`, and the block leaves it out only when none applies.

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
from pathlib import Path
from typing import Any

from . import registry, registry_specs

TOP_KEYS = 12
NEW_WINDOW_DAYS = 14
NEW_KEYS = 3

def recently_added(
    vault_root: Path, spec: registry.RegistrySpec, snapshot: registry.Snapshot, *, since: dt.date
) -> list[str]:
    """Active keys of one registry that a write added on or after `since`."""
    from .. import registry_history

    found: list[str] = []
    # Bounded history reads retain current authority; an overlay hash cannot validate a snapshot.
    for item in registry_history.versions(vault_root, stem=spec.stem):
        at = str(item.get("at") or "")
        added = item.get("added") or {}
        if at[:10] < since.isoformat():
            continue
        for key in added.get(spec.name, ()):
            entry = snapshot.entries.get(key)
            if entry is not None and entry.status == "active" and key not in found:
                found.append(key)
    return sorted(found)


def block(
    vault_root: Path,
    *,
    inspect_route: str,
    today: dt.date | None = None,
    registry_scope: str | None = None,
) -> dict[str, Any]:
    """The bootstrap's view of every vocabulary registry.

    One instance is described, never a pool: the public one, or `registry_scope`
    when the request carries it. An instance this caller cannot admit reports the
    same `unavailable` whether it is withheld or does not exist.
    """
    since = (today or dt.datetime.now(dt.UTC).date()) - dt.timedelta(days=NEW_WINDOW_DAYS)
    from ..registry_history import HistoryAdmissionRefused
    from . import instances
    from .contract import _usage, admission_refusal, history_refusal, selection_refusal

    registries: dict[str, Any] = {}
    for name, spec in registry_specs().items():
        try:
            spec = instances.select(vault_root, spec, registry_scope)
        except registry.RegistryError as error:
            registries[name] = {"unavailable": selection_refusal(name, error)["reason"]}
            continue
        refusal = admission_refusal(vault_root, spec)
        if refusal is not None:
            registries[name] = {"unavailable": refusal["reason"]}
            continue
        snapshot = registry.load(spec, vault_root)
        row: dict[str, Any] = {}
        if spec.summarize_keys:
            active = [entry for entry in snapshot.entries.values() if entry.status == "active"]
            usage = _usage(vault_root, spec, snapshot)
            if usage is not None and usage.available:
                ordered = sorted(
                    active, key=lambda entry: (-usage.counts.get(entry.key, 0), entry.key)
                )
                row["top"] = {
                    entry.key: usage.counts.get(entry.key, 0) for entry in ordered[:TOP_KEYS]
                }
                row["count_source"] = usage.source
            else:
                row["top"] = [entry.key for entry in active[:TOP_KEYS]]
                row["unavailable"] = usage.reason if usage is not None else "not_counted"
            listed = len(row.get("top", ()))
            if listed and len(active) > listed:
                row["more"] = f"+{len(active) - listed} more"
        if spec.summarize_keys or snapshot.findings:
            row["findings"] = len(snapshot.findings)
        try:
            new = (
                recently_added(vault_root, spec, snapshot, since=since)
                if history_refusal(vault_root, spec) is None
                else []
            )
        except HistoryAdmissionRefused:
            new = []
        if new:
            row["new"] = new[:NEW_KEYS] + (
                [f"+{len(new) - NEW_KEYS} more"] if len(new) > NEW_KEYS else []
            )
        # A registry without listed keys appears only when it has something to report.
        if row:
            registries[name] = row
    # Registry names are hyphenated, so they never meet the two plain keys.
    out: dict[str, Any] = {**registries, "inspect": inspect_route}
    if any("new" in row for row in registries.values()):
        out["new_since"] = since.isoformat()
    return out
