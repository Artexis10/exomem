"""Core entity kinds plus a validated vault-owned extension registry."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeAlias

import yaml

from . import vault
from .kbdir import kb_dirname

EXTENSION_SCHEMA_VERSION = 1
CORE_REGISTRY_VERSION = 1
_ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_STATUSES = frozenset({"active", "deprecated"})
ENTITY_WRITER_OPTIONAL_FRONTMATTER = (
    "affiliation",
    "relationship",
    "domain",
    "language",
    "repo",
    "license",
    "used_in",
    "decided",
    "project",
    "decision_status",
)
SUPPORTED_OPTIONAL_FRONTMATTER = frozenset(ENTITY_WRITER_OPTIONAL_FRONTMATTER)
_RESERVED_PATH_CHARS = frozenset('<>:"/\\|?*')
_RESERVED_WINDOWS_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)


@dataclass(frozen=True, slots=True)
class EntityTypeDefinition:
    """One entity kind and its durable routing metadata."""

    id: str
    folder: str
    label: str
    aliases: tuple[str, ...]
    capture_guidance: str
    optional_frontmatter: tuple[str, ...] = ()
    cue_nouns: tuple[str, ...] = ()
    parent: str | None = None
    status: str = "active"
    replaced_by: str | None = None
    core: bool = True


# Backward-compatible core-only tuple. These five definitions remain unchanged.
ENTITY_TYPE_REGISTRY: tuple[EntityTypeDefinition, ...] = (
    EntityTypeDefinition(
        id="person",
        folder="People",
        label="Person",
        aliases=("people", "individual", "individuals", "human", "humans"),
        capture_guidance="A stable person identity with reusable facts, history, or relations.",
        optional_frontmatter=("affiliation", "relationship"),
    ),
    EntityTypeDefinition(
        id="organization",
        folder="Organizations",
        label="Organization",
        aliases=(
            "organizations",
            "organisation",
            "organisations",
            "company",
            "companies",
            "institution",
            "institutions",
        ),
        capture_guidance=(
            "A stable organization identity with reusable facts, history, or relations."
        ),
    ),
    EntityTypeDefinition(
        id="concept",
        folder="Concepts",
        label="Concept",
        aliases=("concepts", "idea", "ideas"),
        capture_guidance="A reusable concept that anchors conclusions across sources.",
        optional_frontmatter=("domain",),
    ),
    EntityTypeDefinition(
        id="library",
        folder="Libraries",
        label="Library",
        aliases=(
            "libraries",
            "software-library",
            "software-libraries",
            "package",
            "packages",
        ),
        capture_guidance="A reusable software library or package with durable project context.",
        optional_frontmatter=("language", "repo", "license", "used_in"),
    ),
    EntityTypeDefinition(
        id="decision",
        folder="Decisions",
        label="Decision",
        aliases=("decisions", "adr", "adrs"),
        capture_guidance="A durable decision whose identity is useful as a graph node.",
        optional_frontmatter=("decided", "project", "decision_status"),
    ),
)


def normalize_entity_token(value: str) -> str:
    """Return the canonical comparison token for entity registry values."""
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")


def _normalized(value: str) -> str:
    """Backward-compatible private alias for older internal callers."""
    return normalize_entity_token(value)


def _core_indexes() -> tuple[
    Mapping[str, EntityTypeDefinition],
    Mapping[str, EntityTypeDefinition],
    Mapping[str, EntityTypeDefinition],
]:
    by_id: dict[str, EntityTypeDefinition] = {}
    by_folder: dict[str, EntityTypeDefinition] = {}
    by_alias: dict[str, EntityTypeDefinition] = {}
    for definition in ENTITY_TYPE_REGISTRY:
        if definition.id != _normalized(definition.id):
            raise ValueError(f"entity type id must be normalized: {definition.id!r}")
        if definition.id in by_id:
            raise ValueError(f"duplicate entity type id: {definition.id!r}")
        folder_key = definition.folder.casefold()
        if folder_key in by_folder:
            raise ValueError(f"duplicate entity folder: {definition.folder!r}")
        by_id[definition.id] = definition
        by_folder[folder_key] = definition
        for raw_alias in definition.aliases:
            alias = _normalized(raw_alias)
            if not alias or alias in by_id or alias in by_alias:
                raise ValueError(f"duplicate or invalid entity alias: {raw_alias!r}")
            by_alias[alias] = definition
    return (
        MappingProxyType(by_id),
        MappingProxyType(by_folder),
        MappingProxyType(by_alias),
    )


ENTITY_TYPES_BY_ID, ENTITY_TYPES_BY_FOLDER, ENTITY_TYPES_BY_ALIAS = _core_indexes()
ENTITY_TYPE_IDS: tuple[str, ...] = tuple(ENTITY_TYPES_BY_ID)
EntityTypeId: TypeAlias = str
ENTITY_TYPE_TO_FOLDER: Mapping[str, str] = MappingProxyType(
    {key: definition.folder for key, definition in ENTITY_TYPES_BY_ID.items()}
)


@dataclass(frozen=True, slots=True)
class EntityTypeRegistry:
    """One immutable core-plus-extension registry snapshot."""

    core_version: int
    extension_hash: str
    core: Mapping[str, EntityTypeDefinition]
    extensions: Mapping[str, EntityTypeDefinition] = field(default_factory=dict)
    findings: tuple[dict[str, str], ...] = ()
    by_id: Mapping[str, EntityTypeDefinition] = field(default_factory=dict)
    by_folder: Mapping[str, EntityTypeDefinition] = field(default_factory=dict)
    by_alias: Mapping[str, EntityTypeDefinition] = field(default_factory=dict)

    @property
    def active_definitions(self) -> tuple[EntityTypeDefinition, ...]:
        return tuple(self.by_id.values())

    @property
    def active_ids(self) -> tuple[str, ...]:
        return tuple(self.by_id)

    def resolve(self, value: str) -> EntityTypeDefinition | None:
        normalized = _normalized(value)
        direct = (
            self.by_id.get(normalized)
            or self.by_alias.get(normalized)
            or self.by_folder.get(str(value or "").strip().casefold())
        )
        if direct is not None:
            return direct
        return next(
            (
                definition
                for folder, definition in self.by_folder.items()
                if _normalized(folder) == normalized
            ),
            None,
        )


def _registry(
    core_version: int,
    extension_hash: str,
    core: Mapping[str, EntityTypeDefinition],
    extensions: Mapping[str, EntityTypeDefinition] | None = None,
    findings: tuple[dict[str, str], ...] = (),
) -> EntityTypeRegistry:
    extension_map = dict(extensions or {})
    active = [
        *core.values(),
        *(item for item in extension_map.values() if item.status == "active"),
    ]
    by_id = {item.id: item for item in active}
    by_folder = {item.folder.casefold(): item for item in active}
    by_alias: dict[str, EntityTypeDefinition] = {}
    for item in active:
        for value in (item.label, *item.aliases):
            by_alias[_normalized(value)] = item
    return EntityTypeRegistry(
        core_version=core_version,
        extension_hash=extension_hash,
        core=MappingProxyType(dict(core)),
        extensions=MappingProxyType(extension_map),
        findings=findings,
        by_id=MappingProxyType(by_id),
        by_folder=MappingProxyType(by_folder),
        by_alias=MappingProxyType(by_alias),
    )


@lru_cache(maxsize=1)
def core_registry() -> EntityTypeRegistry:
    return _registry(CORE_REGISTRY_VERSION, "none", ENTITY_TYPES_BY_ID)


def extension_registry_path(vault_root: Path) -> Path:
    return Path(vault_root) / kb_dirname() / "_Schema" / "entity-types.yaml"


_CACHE: dict[Path, tuple[str, EntityTypeRegistry]] = {}


def load_entity_types(
    vault_root: Path | None = None,
    *,
    proposal: dict[str, Any] | None = None,
) -> EntityTypeRegistry:
    """Load the core registry plus the valid remainder of one vault extension."""
    core = core_registry()
    if proposal is not None:
        raw = yaml.safe_dump(proposal, sort_keys=True, allow_unicode=True)
        return _parse_extension_data(proposal, _content_hash(raw), core)
    if vault_root is None:
        return core
    path = extension_registry_path(vault_root)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return core
    digest = _content_hash(raw)
    cached = _CACHE.get(path)
    if cached is not None and cached[0] == digest:
        return cached[1]
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        loaded = _registry(
            core.core_version,
            digest,
            core.core,
            findings=(_finding("invalid_yaml", "registry", str(exc)),),
        )
    else:
        loaded = _parse_extension_data(data, digest, core)
    _CACHE[path] = (digest, loaded)
    return loaded


def validate_proposal(proposal: dict[str, Any]) -> list[dict[str, str]]:
    return list(load_entity_types(proposal=proposal).findings)


def save_registry(
    vault_root: Path,
    proposal: dict[str, Any],
    *,
    expected_hash: str | None,
    observed_ids: Iterable[str],
) -> dict[str, Any]:
    registry = load_entity_types(proposal=proposal)
    if registry.findings:
        raise ValueError(f"INVALID_ENTITY_TYPE_REGISTRY: {list(registry.findings)!r}")
    removed = sorted(set(observed_ids) - set(registry.extensions) - set(registry.core))
    if removed:
        raise ValueError(
            "OBSERVED_ENTITY_TYPE_DELETION: deprecate observed ids instead: "
            f"{removed}"
        )
    path = extension_registry_path(vault_root)
    current_hash: str | None = None
    if path.exists():
        current_hash = _content_hash(path.read_text(encoding="utf-8"))
        if expected_hash is None:
            raise ValueError("REGISTRY_EXISTS: provide current expected_hash")
        if expected_hash != current_hash:
            raise ValueError(
                "STALE_ENTITY_TYPE_REGISTRY: expected_hash does not match current hash"
            )
    rendered = yaml.safe_dump(proposal, sort_keys=False, allow_unicode=True)
    vault.batch_atomic_write(
        [vault.PlannedWrite(path=path, content=rendered)],
        vault_root=vault_root,
    )
    _CACHE.pop(path, None)
    return {
        "path": path.relative_to(vault_root).as_posix(),
        "content_hash": _content_hash(rendered),
        "previous_hash": current_hash,
        "created": current_hash is None,
    }


def empty_proposal() -> dict[str, Any]:
    return {"schema_version": EXTENSION_SCHEMA_VERSION, "entity_types": {}}


def observed_extension_ids(vault_root: Path) -> frozenset[str]:
    """Return currently registered extension IDs authored under ``Entities``."""
    entities = Path(vault_root) / kb_dirname() / "Entities"
    registry = load_entity_types(vault_root)
    observed: set[str] = set()
    if not entities.is_dir():
        return frozenset()
    for page in entities.rglob("*.md"):
        if page.name == "index.md" or any(
            part.startswith("_") for part in page.relative_to(entities).parts[:-1]
        ):
            continue
        try:
            frontmatter, _body, _error = vault.parse_frontmatter(
                page.read_text(encoding="utf-8")
            )
        except OSError:
            continue
        raw = frontmatter.get("entity_type")
        if isinstance(raw, str) and raw.strip():
            resolved = registry.resolve(raw)
            if resolved is not None and resolved.id in registry.extensions:
                observed.add(resolved.id)
    return frozenset(observed)


def resolve_entity_type(value: str) -> EntityTypeDefinition | None:
    """Backward-compatible core-only resolution."""
    return core_registry().resolve(value)


def _parse_extension_data(
    data: Any,
    digest: str,
    core: EntityTypeRegistry,
) -> EntityTypeRegistry:
    findings: list[dict[str, str]] = []
    if not isinstance(data, dict):
        return _registry(
            core.core_version,
            digest,
            core.core,
            findings=(_finding("invalid_registry", "registry", "must be an object"),),
        )
    for key in sorted(
        set(data) - {"schema_version", "entity_types"}, key=lambda value: str(value)
    ):
        findings.append(_finding("unknown_field", str(key), "unknown registry field"))
    if data.get("schema_version") != EXTENSION_SCHEMA_VERSION:
        findings.append(
            _finding(
                "invalid_version",
                "schema_version",
                f"must be {EXTENSION_SCHEMA_VERSION}",
            )
        )
    raw_types = data.get("entity_types") or {}
    if not isinstance(raw_types, dict):
        findings.append(
            _finding("invalid_entity_types", "entity_types", "must be an object")
        )
        raw_types = {}

    allowed_fields = {
        "folder",
        "label",
        "aliases",
        "cue_nouns",
        "optional_frontmatter",
        "capture_guidance",
        "parent",
        "status",
        "replaced_by",
    }
    core_token_owners: dict[str, str] = {}
    for definition in core.core.values():
        for value in (
            definition.id,
            definition.label,
            definition.folder,
            *definition.aliases,
        ):
            core_token_owners[_normalized(value)] = definition.id
    folder_owners = {item.folder.casefold(): item.id for item in core.core.values()}
    token_owners = dict(core_token_owners)
    extensions: dict[str, EntityTypeDefinition] = {}

    for raw_id, raw_value in raw_types.items():
        type_id = str(raw_id)
        span = f"entity_types.{type_id}"
        local: list[dict[str, str]] = []
        if not _ID_RE.fullmatch(type_id):
            local.append(
                _finding(
                    "invalid_id",
                    span,
                    "must match ^[a-z][a-z0-9-]*$",
                    entity_type=type_id,
                )
            )
        owner = token_owners.get(_normalized(type_id))
        if owner is not None:
            local.append(
                _finding(
                    "collision",
                    span,
                    f"id collides with {owner!r}",
                    entity_type=type_id,
                )
            )
        if not isinstance(raw_value, dict):
            local.append(
                _finding(
                    "invalid_definition",
                    span,
                    "must be an object",
                    entity_type=type_id,
                )
            )
            findings.extend(local)
            continue
        for unknown in sorted(
            set(raw_value) - allowed_fields, key=lambda value: str(value)
        ):
            local.append(
                _finding(
                    "unknown_field",
                    f"{span}.{unknown}",
                    "unknown definition field",
                    entity_type=type_id,
                )
            )
        folder = str(raw_value.get("folder") or "").strip()
        label = str(raw_value.get("label") or "").strip()
        capture_guidance = str(raw_value.get("capture_guidance") or "").strip()
        aliases = _strings(raw_value.get("aliases"), f"{span}.aliases", local, type_id)
        cue_value = raw_value.get("cue_nouns", aliases)
        cue_nouns = _strings(cue_value, f"{span}.cue_nouns", local, type_id)
        optional_frontmatter = _strings(
            raw_value.get("optional_frontmatter", []),
            f"{span}.optional_frontmatter",
            local,
            type_id,
        )
        unsupported_optional = sorted(
            set(optional_frontmatter) - SUPPORTED_OPTIONAL_FRONTMATTER
        )
        if unsupported_optional:
            local.append(
                _finding(
                    "unsupported_optional_frontmatter",
                    f"{span}.optional_frontmatter",
                    f"unsupported field(s): {unsupported_optional}",
                    entity_type=type_id,
                )
            )
        if not _safe_folder(folder):
            local.append(
                _finding(
                    "invalid_folder",
                    f"{span}.folder",
                    "must be one safe path segment",
                    entity_type=type_id,
                )
            )
        folder_owner = folder_owners.get(folder.casefold())
        if folder_owner is not None and folder_owner != type_id:
            local.append(
                _finding(
                    "collision",
                    f"{span}.folder",
                    f"folder collides with {folder_owner!r}",
                    entity_type=type_id,
                )
            )
        for field_name, field_value in (("label", label), ("folder", folder)):
            token_owner = token_owners.get(_normalized(field_value))
            if token_owner is not None and token_owner != type_id and not (
                field_name == "folder" and folder_owner is not None
            ):
                local.append(
                    _finding(
                        "collision",
                        f"{span}.{field_name}",
                        f"{field_name} collides with {token_owner!r}",
                        entity_type=type_id,
                    )
                )
        if not label:
            local.append(
                _finding(
                    "missing_label",
                    f"{span}.label",
                    "is required",
                    entity_type=type_id,
                )
            )
        if not capture_guidance:
            local.append(
                _finding(
                    "missing_capture_guidance",
                    f"{span}.capture_guidance",
                    "is required",
                    entity_type=type_id,
                )
            )
        own_tokens = {_normalized(type_id), _normalized(label), _normalized(folder)}
        alias_keys: set[str] = set()
        for alias in aliases:
            alias_key = _normalized(alias)
            alias_owner = token_owners.get(alias_key)
            if not alias_key or alias_key in alias_keys or alias_key in own_tokens or (
                alias_owner is not None and alias_owner != type_id
            ):
                local.append(
                    _finding(
                        "collision",
                        f"{span}.aliases",
                        f"alias {alias!r} collides",
                        entity_type=type_id,
                    )
                )
            alias_keys.add(alias_key)
        parent = _optional(raw_value.get("parent"))
        if parent is not None and parent not in core.core:
            local.append(
                _finding(
                    "invalid_parent",
                    f"{span}.parent",
                    "must name a core entity type",
                    entity_type=type_id,
                )
            )
        status = str(raw_value.get("status") or "active").strip().casefold()
        if status not in _STATUSES:
            local.append(
                _finding(
                    "invalid_status",
                    f"{span}.status",
                    f"must be one of {sorted(_STATUSES)}",
                    entity_type=type_id,
                )
            )
        replaced_by = _optional(raw_value.get("replaced_by"))

        if local:
            findings.extend(local)
            continue
        definition = EntityTypeDefinition(
            id=type_id,
            folder=folder,
            label=label,
            aliases=tuple(aliases),
            cue_nouns=tuple(dict.fromkeys(cue_nouns)),
            optional_frontmatter=tuple(dict.fromkeys(optional_frontmatter)),
            capture_guidance=capture_guidance,
            parent=parent,
            status=status,
            replaced_by=replaced_by,
            core=False,
        )
        extensions[type_id] = definition
        folder_owners[folder.casefold()] = type_id
        for token in own_tokens | {_normalized(alias) for alias in aliases}:
            if token:
                token_owners[token] = type_id

    canonical = set(core.core) | set(extensions)
    for type_id, definition in tuple(extensions.items()):
        if definition.replaced_by and definition.replaced_by not in canonical:
            findings.append(
                _finding(
                    "invalid_replacement",
                    f"entity_types.{type_id}.replaced_by",
                    "must name a registered entity type",
                    entity_type=type_id,
                )
            )
            extensions.pop(type_id)
    ordered_findings = tuple(
        sorted(findings, key=lambda item: (item["path"], item["code"], item["detail"]))
    )
    return _registry(
        core.core_version,
        digest,
        core.core,
        extensions,
        ordered_findings,
    )


def _safe_folder(value: str) -> bool:
    cleaned = unicodedata.normalize("NFKC", value).strip()
    device_name = cleaned.partition(".")[0].casefold()
    return bool(
        cleaned
        and cleaned not in {".", ".."}
        and cleaned == value
        and not cleaned.endswith(".")
        and device_name not in _RESERVED_WINDOWS_NAMES
        and not any(
            char in _RESERVED_PATH_CHARS or ord(char) < 32 for char in cleaned
        )
    )


def _strings(
    value: Any,
    path: str,
    findings: list[dict[str, str]],
    entity_type: str,
) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        findings.append(
            _finding(
                "invalid_list",
                path,
                "must be a list of strings",
                entity_type=entity_type,
            )
        )
        return []
    return [item.strip() for item in value if item.strip()]


def _optional(value: Any) -> str | None:
    return str(value).strip() if value not in (None, "") else None


def _finding(
    code: str,
    path: str,
    detail: str,
    *,
    entity_type: str | None = None,
) -> dict[str, str]:
    finding = {
        "code": code,
        "path": path,
        "span": path,
        "severity": "error",
        "detail": detail,
    }
    if entity_type is not None:
        finding["entity_type"] = entity_type
    return finding


def _content_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
