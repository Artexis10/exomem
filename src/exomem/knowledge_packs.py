"""Built-in knowledge-pack definitions and selected-pack persistence.

Knowledge packs are product-level routing hints: small, declarative bundles that
make common domains feel like first-class Exomem primitives without changing the
storage engine. Suggestions are deterministic measurements over vault structure;
selection is durable guidance stored under the governed Knowledge Base layer.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from .entity_types import ENTITY_TYPE_IDS
from .kbdir import kb_dirname, kb_prefix
from .vault import PlannedWrite, batch_atomic_write, kb_root

PACK_DIRECTORY = "packs"
PACK_SELECTION_DIR = "_Packs"
PACK_SELECTION_FILE = "selected-packs.json"
PACK_SELECTION_SCHEMA_VERSION = 1
DEFAULT_PACK_ID = "personal-records"
PACK_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {
        "id",
        "name",
        "description",
        "purpose",
        "audience",
        "beginner_description",
        "agent_instructions",
        "default_note_types",
        "default_entity_types",
        "default_block_types",
        "suggested_folders",
        "suggested_workflows",
        "primitives",
        "actions",
        "examples",
        "signals",
    }
)
PACK_OPTIONAL_FIELDS: frozenset[str] = frozenset()
PACK_ALLOWED_FIELDS: frozenset[str] = PACK_REQUIRED_FIELDS | PACK_OPTIONAL_FIELDS
PACK_WORKFLOW_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {"title", "intent", "route", "example"}
)
PACK_WORKFLOW_ALLOWED_FIELDS: frozenset[str] = PACK_WORKFLOW_REQUIRED_FIELDS
PACK_ALLOWED_PRIMITIVES: frozenset[str] = frozenset(
    {
        "source",
        "evidence",
        "case",
        "decision",
        "record",
        "asset",
        "production",
        "entity",
        "failure",
        "pattern",
        "experiment",
    }
)
PACK_ALLOWED_ACTIONS: frozenset[str] = frozenset(
    {
        "save",
        "adopt",
        "ask",
        "prove",
        "review",
        "update",
        "connect",
        "remember",
        "capture",
        "maintain",
    }
)


class PackValidationError(ValueError):
    """Stable validation failure for declarative knowledge-pack metadata."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


class PackSelectionError(ValueError):
    """Stable failure for selected-pack manifest operations."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class KnowledgePack:
    id: str
    name: str
    description: str
    purpose: str
    audience: str
    beginner_description: str
    agent_instructions: str
    default_note_types: tuple[str, ...]
    default_entity_types: tuple[str, ...]
    default_block_types: tuple[str, ...]
    suggested_folders: tuple[str, ...]
    suggested_workflows: tuple[dict[str, str], ...]
    primitives: tuple[str, ...]
    actions: tuple[str, ...]
    examples: tuple[str, ...]
    signals: tuple[str, ...]

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PackSuggestion:
    pack: KnowledgePack
    score: int
    matched_signals: tuple[str, ...]

    def as_dict(self) -> dict:
        data = self.pack.as_dict()
        data["score"] = self.score
        data["matched_signals"] = list(self.matched_signals)
        return data


_TOKEN = re.compile(r"[a-z0-9]+")


def pack_schema() -> dict:
    """Return the public declarative schema for knowledge-pack metadata."""
    return {
        "format": "json",
        "directory": f"src/exomem/{PACK_DIRECTORY}/",
        "required_fields": sorted(PACK_REQUIRED_FIELDS),
        "optional_fields": sorted(PACK_OPTIONAL_FIELDS),
        "allowed_primitives": sorted(PACK_ALLOWED_PRIMITIVES),
        "allowed_actions": sorted(PACK_ALLOWED_ACTIONS),
        "workflow_required_fields": sorted(PACK_WORKFLOW_REQUIRED_FIELDS),
        "selection_manifest": f"{kb_prefix()}{PACK_SELECTION_DIR}/{PACK_SELECTION_FILE}",
        "selection_schema_version": PACK_SELECTION_SCHEMA_VERSION,
    }


def validate_pack_dict(raw: dict[str, Any]) -> KnowledgePack:
    """Validate a declarative knowledge-pack mapping and return a typed pack.

    Validation is intentionally strict: unknown fields are rejected so a custom
    pack cannot silently depend on metadata this version of Exomem ignores.
    """
    if not isinstance(raw, dict):
        raise PackValidationError("INVALID_PACK", "pack must be a mapping")
    unknown = sorted(set(raw) - PACK_ALLOWED_FIELDS)
    if unknown:
        raise PackValidationError("UNKNOWN_FIELD", f"unknown field(s): {unknown}")
    missing = sorted(PACK_REQUIRED_FIELDS - set(raw))
    if missing:
        raise PackValidationError("MISSING_FIELD", f"missing required field(s): {missing}")

    def _nonempty_string(field: str) -> str:
        value = raw[field]
        if not isinstance(value, str) or not value.strip():
            raise PackValidationError("INVALID_FIELD", f"{field!r} must be a non-empty string")
        return value.strip()

    def _string_tuple(field: str) -> tuple[str, ...]:
        value = raw[field]
        if not isinstance(value, (list, tuple)) or not value:
            raise PackValidationError("INVALID_FIELD", f"{field!r} must be a non-empty list")
        out: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise PackValidationError(
                    "INVALID_FIELD",
                    f"{field!r} entries must be non-empty strings",
                )
            out.append(item.strip())
        return tuple(out)

    def _workflow_tuple(field: str) -> tuple[dict[str, str], ...]:
        value = raw[field]
        if not isinstance(value, (list, tuple)) or not value:
            raise PackValidationError("INVALID_WORKFLOW", f"{field!r} must be a non-empty list")
        workflows: list[dict[str, str]] = []
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise PackValidationError(
                    "INVALID_WORKFLOW",
                    f"{field!r}[{index}] must be a mapping",
                )
            unknown = sorted(set(item) - PACK_WORKFLOW_ALLOWED_FIELDS)
            if unknown:
                raise PackValidationError(
                    "UNKNOWN_WORKFLOW_FIELD",
                    f"{field!r}[{index}] unknown field(s): {unknown}",
                )
            missing = sorted(PACK_WORKFLOW_REQUIRED_FIELDS - set(item))
            if missing:
                raise PackValidationError(
                    "MISSING_WORKFLOW_FIELD",
                    f"{field!r}[{index}] missing field(s): {missing}",
                )
            clean: dict[str, str] = {}
            for key in sorted(PACK_WORKFLOW_REQUIRED_FIELDS):
                item_value = item[key]
                if not isinstance(item_value, str) or not item_value.strip():
                    raise PackValidationError(
                        "INVALID_WORKFLOW",
                        f"{field!r}[{index}].{key} must be a non-empty string",
                    )
                clean[key] = item_value.strip()
            workflows.append(clean)
        return tuple(workflows)

    primitives = _string_tuple("primitives")
    bad_primitives = sorted(set(primitives) - PACK_ALLOWED_PRIMITIVES)
    if bad_primitives:
        raise PackValidationError(
            "INVALID_PRIMITIVE",
            f"unsupported primitive(s): {bad_primitives}",
        )

    actions = _string_tuple("actions")
    bad_actions = sorted(set(actions) - PACK_ALLOWED_ACTIONS)
    if bad_actions:
        raise PackValidationError("INVALID_ACTION", f"unsupported action(s): {bad_actions}")

    default_entity_types = _string_tuple("default_entity_types")
    bad_entity_types = sorted(set(default_entity_types) - set(ENTITY_TYPE_IDS))
    if bad_entity_types:
        raise PackValidationError(
            "INVALID_ENTITY_TYPE",
            f"unsupported default_entity_types value(s): {bad_entity_types}",
        )

    return KnowledgePack(
        id=_nonempty_string("id"),
        name=_nonempty_string("name"),
        description=_nonempty_string("description"),
        purpose=_nonempty_string("purpose"),
        audience=_nonempty_string("audience"),
        beginner_description=_nonempty_string("beginner_description"),
        agent_instructions=_nonempty_string("agent_instructions"),
        default_note_types=_string_tuple("default_note_types"),
        default_entity_types=default_entity_types,
        default_block_types=_string_tuple("default_block_types"),
        suggested_folders=_string_tuple("suggested_folders"),
        suggested_workflows=_workflow_tuple("suggested_workflows"),
        primitives=primitives,
        actions=actions,
        examples=_string_tuple("examples"),
        signals=_string_tuple("signals"),
    )


def _validate_pack_catalog(packs: tuple[KnowledgePack, ...]) -> None:
    seen: set[str] = set()
    for pack in packs:
        validate_pack_dict(pack.as_dict())
        if pack.id in seen:
            raise PackValidationError("DUPLICATE_PACK", f"duplicate pack id: {pack.id}")
        seen.add(pack.id)


def _load_builtin_packs() -> tuple[KnowledgePack, ...]:
    base = resources.files(__package__).joinpath(PACK_DIRECTORY)
    packs: list[KnowledgePack] = []
    for entry in sorted(base.iterdir(), key=lambda item: item.name):
        if not entry.name.endswith(".json"):
            continue
        try:
            raw = json.loads(entry.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise PackValidationError(
                "INVALID_PACK_JSON",
                f"{PACK_DIRECTORY}/{entry.name}: {e.msg}",
            ) from e
        try:
            packs.append(validate_pack_dict(raw))
        except PackValidationError as e:
            raise PackValidationError(
                e.code,
                f"{PACK_DIRECTORY}/{entry.name}: {e.reason}",
            ) from e
    if not packs:
        raise PackValidationError("NO_BUILTIN_PACKS", f"no .json packs found in {PACK_DIRECTORY}/")
    out = tuple(packs)
    _validate_pack_catalog(out)
    return out


BUILTIN_PACKS: tuple[KnowledgePack, ...] = _load_builtin_packs()
_PACK_BY_ID = {pack.id: pack for pack in BUILTIN_PACKS}


def list_builtin_packs() -> list[dict]:
    """Return the built-in pack catalog as JSON-serializable dictionaries."""
    _validate_builtin_pack_catalog()
    return [pack.as_dict() for pack in BUILTIN_PACKS]


def get_builtin_pack(pack_id: str) -> dict:
    """Return one built-in pack by ID."""
    try:
        return _PACK_BY_ID[pack_id].as_dict()
    except KeyError as e:
        raise PackValidationError("UNKNOWN_PACK", f"unknown pack id: {pack_id}") from e


def normalize_pack_ids(pack_ids: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    """Validate, dedupe, and default a selected-pack ID list."""
    raw_ids = list(pack_ids or [DEFAULT_PACK_ID])
    out: list[str] = []
    seen: set[str] = set()
    unknown: list[str] = []
    for raw in raw_ids:
        pack_id = str(raw).strip()
        if not pack_id:
            continue
        if pack_id not in _PACK_BY_ID:
            unknown.append(pack_id)
            continue
        if pack_id not in seen:
            seen.add(pack_id)
            out.append(pack_id)
    if unknown:
        raise PackSelectionError("UNKNOWN_PACK", f"unknown pack id(s): {unknown}")
    if not out:
        out = [DEFAULT_PACK_ID]
    return tuple(out)


def _selection_rel_path() -> str:
    return f"{kb_prefix()}{PACK_SELECTION_DIR}/{PACK_SELECTION_FILE}"


def _selection_path(root: Path) -> Path:
    return kb_root(root) / PACK_SELECTION_DIR / PACK_SELECTION_FILE


def _pack_summaries(pack_ids: tuple[str, ...]) -> list[dict]:
    return [
        {
            "id": pack.id,
            "name": pack.name,
            "beginner_description": pack.beginner_description,
            "agent_instructions": pack.agent_instructions,
            "default_entity_types": list(pack.default_entity_types),
            "suggested_workflows": list(pack.suggested_workflows),
            "actions": list(pack.actions),
        }
        for pack in (_PACK_BY_ID[pack_id] for pack_id in pack_ids)
    ]


def selected_pack_state(root: Path | str) -> dict:
    """Read selected packs, falling back to the default when no manifest exists."""
    root_path = Path(root)
    rel = _selection_rel_path()
    path = _selection_path(root_path)
    warnings: list[str] = []
    source = "default"
    updated: str | None = None
    pack_ids: tuple[str, ...] = (DEFAULT_PACK_ID,)
    manifest_present = path.is_file()

    if manifest_present:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            source = str(raw.get("source") or "unknown")
            updated_raw = raw.get("updated")
            updated = str(updated_raw) if updated_raw else None
            pack_ids = normalize_pack_ids(raw.get("selected_pack_ids"))
        except (OSError, json.JSONDecodeError, PackSelectionError) as e:
            warnings.append(f"selected-pack manifest ignored: {e}")
            pack_ids = (DEFAULT_PACK_ID,)
            source = "default"
            updated = None

    return {
        "schema_version": PACK_SELECTION_SCHEMA_VERSION,
        "path": rel,
        "manifest_present": manifest_present,
        "selected_pack_ids": list(pack_ids),
        "source": source,
        "updated": updated,
        "packs": _pack_summaries(pack_ids),
        "warnings": warnings,
    }


def write_selected_packs(
    root: Path | str,
    pack_ids: list[str] | tuple[str, ...] | None,
    *,
    source: str = "setup",
    today: dt.date | None = None,
) -> dict:
    """Persist selected packs under the governed Knowledge Base layer."""
    root_path = Path(root)
    kb = kb_root(root_path)
    if not kb.is_dir():
        raise PackSelectionError(
            "KB_NOT_INITIALIZED",
            f"{kb_dirname()}/ is required before selecting packs",
        )
    selected_ids = normalize_pack_ids(pack_ids)
    date_iso = (today or dt.date.today()).isoformat()
    rel = _selection_rel_path()
    payload = {
        "schema_version": PACK_SELECTION_SCHEMA_VERSION,
        "selected_pack_ids": list(selected_ids),
        "source": source,
        "updated": date_iso,
        "packs": _pack_summaries(selected_ids),
    }
    batch_atomic_write(
        [
            PlannedWrite(
                path=_selection_path(root_path),
                content=json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            )
        ],
        vault_root=root_path,
    )
    return {"path": rel, **payload, "warnings": []}


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower()))


def _overview_signal_text(scan: dict) -> dict[str, str]:
    """Pack-suggestion input by top-level folder, derived from overview output."""
    by_folder: dict[str, list[str]] = {}
    for entry in scan.get("tree", []):
        path = str(entry.get("path") or "")
        if not path:
            continue
        top = path.split("/", 1)[0]
        sample_names = " ".join(str(name) for name in entry.get("sample_names") or [])
        by_folder.setdefault(top, []).append(f"{path} {sample_names}")
    return {folder: " ".join(parts) for folder, parts in by_folder.items()}


def suggest_packs(scan: dict, *, limit: int = 6) -> list[dict]:
    """Suggest likely built-in knowledge packs from deterministic overview signals.

    This intentionally uses only structural text (folder paths and sample file
    names), not note bodies. Scores are simple counts of matched signal tokens.
    """
    folder_text = _overview_signal_text(scan)
    scored: list[PackSuggestion] = []
    for pack in BUILTIN_PACKS:
        matched: set[str] = set()
        pack_signals = set(pack.signals)
        for text in folder_text.values():
            matched.update(pack_signals & _tokens(text))
        if matched:
            scored.append(
                PackSuggestion(
                    pack=pack,
                    score=len(matched),
                    matched_signals=tuple(sorted(matched)),
                )
            )
    scored.sort(key=lambda s: (-s.score, s.pack.id))
    if not scored:
        default = _PACK_BY_ID[DEFAULT_PACK_ID]
        scored.append(
            PackSuggestion(
                pack=default,
                score=0,
                matched_signals=("default-general-vault",),
            )
        )
    return [suggestion.as_dict() for suggestion in scored[: max(limit, 0)]]


def _validate_builtin_pack_catalog() -> None:
    _validate_pack_catalog(BUILTIN_PACKS)
