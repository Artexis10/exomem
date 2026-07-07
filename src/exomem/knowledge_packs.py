"""Built-in knowledge-pack definitions and deterministic pack suggestions.

Knowledge packs are product-level routing hints: small, declarative bundles that
describe common domains in terms of Exomem's durable primitives. They are not a
new storage engine and they do not infer meaning from note bodies. Suggestions
come from cheap structural signals surfaced by ``overview``: folder names, sample
file names, counts, and media mix.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from importlib import resources
from typing import Any


PACK_DIRECTORY = "packs"
PACK_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {"id", "name", "description", "primitives", "actions", "examples", "signals"}
)
PACK_OPTIONAL_FIELDS: frozenset[str] = frozenset()
PACK_ALLOWED_FIELDS: frozenset[str] = PACK_REQUIRED_FIELDS | PACK_OPTIONAL_FIELDS
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
    {"save", "adopt", "ask", "prove", "review", "update", "connect"}
)


class PackValidationError(ValueError):
    """Stable validation failure for declarative knowledge-pack metadata."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class KnowledgePack:
    id: str
    name: str
    description: str
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

    return KnowledgePack(
        id=_nonempty_string("id"),
        name=_nonempty_string("name"),
        description=_nonempty_string("description"),
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
        default = _PACK_BY_ID["personal-records"]
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
