"""A case's vault: seeding it through the product, and reading its end state from files.

Seeding calls the product's own Records and Planning commands, so the vault
holds exactly what a real caller's writes leave, and it runs in a child process
whose every product location is pinned under a scratch directory (see
``executor.seed_case``). The end state is read from files alone, with no server
and no index, so reading it never changes it and never depends on the
server's answers.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from .templates import PLAN_COMMAND, RECORD_COMMAND, SeedStep

#: Seed-step command to its action table: the product function, the argument that
#: names an item, and the row column that carries the item's identity.
_SEED_COMMANDS = {
    RECORD_COMMAND: ("exomem.record_memory", "record_memory", "item_key", "record_id"),
    PLAN_COMMAND: ("exomem.plan_memory", "plan_memory", "plan_id", "plan_id"),
}
SEED_WHY = "seed the evaluation vault"
#: The projector never reads a clock; any fixed instant names the snapshot.
_SNAPSHOT_TIME = "2026-01-01T00:00:00Z"


def _command(name: str):
    import importlib

    module, function, id_argument, id_column = _SEED_COMMANDS[name]
    return getattr(importlib.import_module(module), function), id_argument, id_column


def seed_vault(vault: Path, steps: Sequence[SeedStep]) -> None:
    """Initialise ``vault`` and run each step through the product command."""

    from exomem.init import init_vault

    vault.mkdir(parents=True, exist_ok=True)
    init_vault(vault)
    apply_steps(vault, steps)


def apply_steps(vault: Path, steps: Sequence[SeedStep]) -> None:
    """Run each step through the product command on an initialised vault."""

    for step in steps:
        function, id_argument, id_column = _command(step.command)
        kwargs = {**step.kwargs, "why": SEED_WHY}
        if step.match is not None:
            collection = kwargs["collection"]
            rows = function(vault, "query", collection=collection)["rows"]
            found = [
                row for row in rows if all(row.get(name) == value for name, value in step.match.items())
            ]
            if len(found) != 1:
                raise RuntimeError(f"seed step matched {len(found)} items, not one: {dict(step.match)}")
            guard = function(vault, "inspect", collection=collection)["lifecycle_guards"]
            kwargs.update(
                {
                    id_argument: found[0][id_column],
                    "expected_item_version": found[0]["item_version"],
                    "expected_container_hash": guard["expected_container_hash"],
                }
            )
        function(vault, step.action, **kwargs)


def _manifest_fields(vault: Path, manifest: str) -> dict[str, dict]:
    """The manifest's declared fields, from its YAML frontmatter."""

    text = (vault / manifest).read_text(encoding="utf-8")
    # Frontmatter is the text between the first two `---` delimiter lines.
    _, _, rest = text.partition("---\n")
    header, _, _ = rest.partition("\n---")
    declared = (yaml.safe_load(header) or {}).get("item_schema", {}).get("fields", {})
    return {
        name: {"type": spec.get("type"), "required": bool(spec.get("required", False))}
        for name, spec in declared.items()
        if isinstance(spec, Mapping)
    }


def _canonical(row: Mapping[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, ensure_ascii=False)


def read_end_state(vault: Path) -> dict:
    """Every collection in the vault: manifest facts and the authored rows of its items."""

    from epistemic.projectors.exomem_vault import VaultProjector

    snapshot = VaultProjector(vault).project(phase="end", taken_at=_SNAPSHOT_TIME)
    collections = {}
    for collection in snapshot.collections:
        rows = sorted((dict(item.values) for item in collection.items), key=_canonical)
        collections[collection.manifest] = {
            "profile": collection.profile,
            "title": collection.title,
            "natural_key": list(collection.natural_key),
            "fields": _manifest_fields(vault, collection.manifest),
            "rows": rows,
        }
    return {"collections": collections}
