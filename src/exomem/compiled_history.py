"""Prior Markdown interpretations, retained atomically by the semantic writer.

One entry keeps one before image as read. History stays outside ordinary reads
and indexing; this owner admits every retained image before serving its fields.
It reuses the held history reader and the Records pagination envelope, without
registry history's rotation policy. Each change costs one full before image.
Counts require independent admission of retained entries; only a returned page
of admitted text stays in memory.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

from . import (
    context_refs,
    memory_refs,
    origin_bindings,
    provenance,
    record_history,
    registry_history,
    retained_inputs,
    semantic_index,
    source_closure,
    vault,
)
from .governance.principal import OWNER_AUDIENCE, effective_principal
from .kbdir import kb_dirname

ENTRY_VERSION = 1


def history_root() -> str:
    """The canonical reserved tree owned only by compiled history."""
    return f"{kb_dirname()}/_Compiled/history"


def history_dir(parent_ref: str) -> str:
    return f"{history_root()}/{hashlib.sha256(parent_ref.encode()).hexdigest()}"


def validate_new_write(write: Any, *, path: str) -> None:
    """Keep the bound history exception create-only at every planned admission."""
    guard = getattr(write, "guard", None)
    if (
        not write.create_only or not isinstance(guard, vault.PathGuard)
        or guard.leaf_policy != "absent" or guard.target != path
    ):
        raise ValueError("VOCABULARY_AUXILIARY_GUARD_INVALID")


def validate_new_entry(
    root: Path, *, path: str, raw: bytes, primary_path: str,
    before_source: str, after_source: str,
) -> None:
    """Bind a new history leaf to the exact images its producer read and wrote."""
    try:
        entry = json.loads(raw)
        # Both images are hashed as stored, so a CRLF page chains entry to entry.
        parent_ref = semantic_index.build_parent_index_state(
            root, primary_path, source=before_source
        ).document.parent_ref
        moment = dt.datetime.fromisoformat(entry["at"].replace("Z", "+00:00"))
        name = registry_history.version_id(moment, entry["transition_id"]) + ".json"
        if (
            type(entry["version"]) is not int or entry["version"] != ENTRY_VERSION
            or parent_ref is None or entry["parent_ref"] != parent_ref
            or path != f"{history_dir(parent_ref)}/{name}"
            or entry["path"] != primary_path
            or entry["prior"]["source"] != before_source
            or entry["before_hash"] != vault.content_hash(before_source)
            or entry["after_hash"] != vault.content_hash(after_source)
        ):
            raise ValueError("compiled history does not match its primary")
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise ValueError("VOCABULARY_AUXILIARY_CHANGED") from error


def plan_entry(
    root: Path, *, preflight: Any, token: dict, reason: str | None
) -> vault.PlannedWrite | None:
    """Keep one changed interpretation before the lifecycle auxiliary hash seals it."""
    if (
        source_closure._eligible_path(preflight.path)
        or preflight.before.document.parent_ref is None
    ):
        return None
    parent_ref = preflight.before.document.parent_ref
    moment = dt.datetime.fromisoformat(token["history_at"].replace("Z", "+00:00"))
    name = registry_history.version_id(moment, token["transition_id"]) + ".json"
    path = root / history_dir(parent_ref) / name
    if preflight.committed_replay:
        previous = registry_history.read_kept(root, history_dir(parent_ref), name)
        if previous is None:
            return None
        entry = json.loads(previous)
        if (
            entry.get("transition_id") != token["transition_id"]
            or entry.get("before_hash") != token["before_hash"]
            or entry.get("after_hash") != token["after_hash"]
            or entry.get("reason") != reason
        ):
            raise ValueError("LIFECYCLE_TRANSITION_MISMATCH: history does not match the transition")
        return vault.PlannedWrite(path, previous)
    before_fm, before_body, _ = vault.parse_frontmatter(preflight.before_source, strict=True)
    after_fm, after_body, _ = vault.parse_frontmatter(preflight.after_source, strict=True)
    # `updated` is the writer's clock, not an interpretation change.
    if {k: v for k, v in before_fm.items() if k != "updated"} == {
        k: v for k, v in after_fm.items() if k != "updated"
    } and before_body == after_body:
        return None
    who = effective_principal()
    parent_ref = preflight.before.document.parent_ref
    before_units = {
        unit.unit_ref: unit for unit in preflight.before.document.units if unit.unit_ref is not None
    }
    after_units = {
        unit.unit_ref: unit for unit in preflight.after.document.units if unit.unit_ref is not None
    }
    affected = [
        {
            "unit_ref": ref,
            "before_fingerprint": getattr(before_units.get(ref), "fingerprint", None),
            "after_fingerprint": getattr(after_units.get(ref), "fingerprint", None),
        }
        for ref in sorted(before_units.keys() | after_units.keys())
        if getattr(before_units.get(ref), "fingerprint", None)
        != getattr(after_units.get(ref), "fingerprint", None)
    ]
    metadata = provenance.parse_origin(preflight.before_source, managed=True)
    entry = {
        "version": ENTRY_VERSION,
        "parent_ref": parent_ref,
        "path": preflight.path,
        "at": token["history_at"],
        "operation": preflight.operation,
        "transition_id": token["transition_id"],
        "before_hash": preflight.before_hash,
        "after_hash": preflight.after.source_hash,
        "affected_units": affected,
        "reason": reason,
        "actor": {"audience": who.audience_id, "surface": who.surface},
        "prior": {"source": preflight.exact_before_source},
        "binding": metadata.payload,
    }
    guard = vault.PathGuard.capture(root, path.relative_to(root).as_posix(), leaf_policy="absent")
    return vault.PlannedWrite(
        path,
        json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        create_only=True,
        guard=guard,
    )


def _admitted_entry(root: Path, text: str | None, *, path: str, parent_ref: str) -> dict | None:
    try:
        entry = json.loads(text) if text is not None else None
        if (
            not isinstance(entry, dict)
            or entry.get("version") != ENTRY_VERSION
            or entry.get("parent_ref") != parent_ref
        ):
            return None
        historical_path = entry.get("path")
        if not isinstance(historical_path, str):
            return None
        vault.resolve_under_vault(root, historical_path)
        if (
            not isinstance(entry.get("actor"), dict)
            or not isinstance(entry.get("affected_units"), list)
            or any(not isinstance(unit, dict) for unit in entry["affected_units"])
        ):
            return None
        source = entry["prior"]["source"]
        if not isinstance(source, str) or vault.content_hash(source) != entry["before_hash"]:
            return None
        retained_ref = memory_refs.ref_from_markdown(source) or context_refs.vault_ref(
            historical_path
        )
        if retained_ref != parent_ref:
            return None
        # Current-page admission cannot authorize bytes removed from that page.
        registry_history._require_snapshot_admission(
            root, historical_path, source.encode("utf-8"), markdown=True,
        )
        metadata = provenance.parse_origin(source, managed=True, strict=True)
        if metadata.payload is not None:
            for binding in metadata.payload["inputs"].values():
                origin_bindings.resolve_origin_input(root, binding, disclosure=True)
        if not retained_inputs.exact_text_visible(root, json.dumps(entry, ensure_ascii=False)):
            return None
    except (KeyError, TypeError, ValueError, provenance.OriginError):
        return None
    return entry


def read(
    root: Path,
    *,
    path: str,
    unit_ref: str | None = None,
    limit: int | None = None,
    continuation: str | None = None,
) -> dict[str, Any]:
    """Return a bounded page of independently admitted prior interpretations."""
    from . import commands

    size, after = record_history._page_bounds(limit, continuation)
    current = commands.op_read_memory(root, path=path)
    if not isinstance(current.get("content_hash"), str):
        raise ValueError("NOT_FOUND: page is unavailable")
    canonical = current["path"]
    # The current read establishes the parent identity, never prior disclosure.
    identity = memory_refs.normalize_id(current["frontmatter"].get(memory_refs.ID_FIELD))
    parent_ref = memory_refs.memory_ref(identity) if identity else context_refs.vault_ref(canonical)
    directory = history_dir(parent_ref)
    names = registry_history.kept_names(root, directory, suffix=".json") or []
    retained = 0
    older_count = 0
    page = []
    for name in names:
        entry = _admitted_entry(
            root,
            registry_history.read_kept(root, directory, name),
            path=canonical,
            parent_ref=parent_ref,
        )
        if entry is None:
            continue
        if unit_ref is not None and not any(
            u.get("unit_ref") == unit_ref for u in entry["affected_units"]
        ):
            continue
        retained += 1
        version = name.removesuffix(".json")
        if after is None or version < after:
            older_count += 1
            if len(page) < size:
                page.append((version, entry))
    revisions = []
    who = effective_principal()
    for version, entry in page:
        prior = dict(entry["prior"])
        if unit_ref is not None:
            neutral = semantic_index.build_parent_index_state(
                root, entry["path"], source=prior["source"]
            )
            selected = semantic_index.selected_parent_index_state(
                root, entry["path"], state=neutral
            )
            resolution = selected.document.resolve_unit(unit_ref)
            prior["unit"] = resolution.unit.to_dict() if resolution.unit is not None else None
            prior["unit_status"] = (
                "unavailable"
                if selected.definitions_unavailable and resolution.status == "missing"
                else resolution.status
            )
        actor = entry["actor"]
        if who.audience_id not in {OWNER_AUDIENCE, actor.get("audience")}:
            actor = {"surface": actor.get("surface")}
        revisions.append(
            {**entry, "version": version, "status": "available", "prior": prior, "actor": actor}
        )
    truncated = older_count > size
    return {
        "parent_ref": parent_ref,
        "unit_ref": unit_ref,
        "status": "retained",
        "retained": retained,
        "returned": len(revisions),
        "truncated": truncated,
        "continuation": page[-1][0] if truncated else None,
        "revisions": revisions,
    }
