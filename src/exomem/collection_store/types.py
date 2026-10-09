"""The collection type registry: built-in types as declarations (design §14).

Collections are one mechanism parameterized by a type declaration. Records
and Planning are the built-in types, shipped as package data under
``exomem/_collection_types/`` and changed only by release. They are the only
declarations that may carry a ``wire`` map: the legacy property, receipt and
error names that keep ``record_memory`` and ``plan_memory`` byte-compatible.

Rules a declaration cannot state declaratively are product-owned, versioned
*named validators* in a closed registry (``planning.hierarchy.v1``). A
declaration opts into one by name and never supplies code.

This module loads and resolves types. Authoring declared types through
``schema_memory`` is a later phase; :func:`parse_declaration` enforces only
the closed shape that package data must already satisfy.
"""

from __future__ import annotations

import functools
import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib.resources import files
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import yaml

from .. import structured_collections as collections
from ..query_engine.indexes import IndexDeclarationError, IndexSpec, normalize_indexes

if TYPE_CHECKING:
    from ..planning import HierarchyWrite

PACKAGE_DIRECTORY = "_collection_types"
BUILTIN_TYPE_NAMES = ("records", "planning")

#: The closed kind vocabulary (ruled R9): it grows only by shipped revision.
KINDS = frozenset({"observed", "intended", "procedural", "reference"})
DEFAULT_AUDIENCES = frozenset({"owner", "policy"})

_DECLARATION_KEYS = frozenset(
    {
        "name",
        "version",
        "title",
        "item_type",
        "kind",
        "placement",
        "description",
        "fields",
        "indexes",
        "natural_key",
        "extensible",
        "lifecycle",
        "surfacing",
        "default_audience",
        "presentation",
        "views",
        "validators",
        "wire",
    }
)
_REQUIRED_KEYS = frozenset(
    {"name", "version", "title", "item_type", "kind", "placement", "default_audience"}
)
_WIRE_KEYS = frozenset(
    {
        "item_id_property",
        "reference_namespace",
        "manifest_audit_property",
        "item_audit_marker",
        "activity_prefix",
        "receipt_property",
        "receipt_marker",
        "error_codes",
    }
)


class CollectionTypeError(ValueError):
    """A declaration or type lookup refused with a stable code."""

    def __init__(self, code: str, reason: str, location: str = "") -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason
        self.location = location


@dataclass(frozen=True, slots=True)
class NamedValidator:
    """One product-owned rule a declaration can opt into by name."""

    name: str
    #: `(manifest, plans, *, write)`; `write` is the item a write changes, None for a read.
    validate: Callable[..., None]


@dataclass(frozen=True, slots=True)
class CollectionType:
    name: str
    version: int
    title: str
    item_type: str
    kind: str
    placement: str
    default_audience: str
    builtin: bool
    description: str = ""
    extensible: bool = False
    fields: Mapping[str, collections.FieldSpec] = field(
        default_factory=lambda: MappingProxyType({})
    )
    natural_key: tuple[str, ...] = ()
    views: tuple[Mapping[str, Any], ...] = ()
    validators: tuple[str, ...] = ()
    wire: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    indexes: tuple[IndexSpec, ...] = ()
    declaration: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


def _planning_hierarchy_v1(
    manifest: collections.CollectionManifest,
    plans: Mapping[str, Mapping[str, Any]],
    *,
    write: HierarchyWrite | None,
) -> None:
    from .. import planning

    planning.validate_hierarchy(manifest, plans, write=write)


_NAMED_VALIDATORS: Mapping[str, NamedValidator] = MappingProxyType(
    {
        "planning.hierarchy.v1": NamedValidator("planning.hierarchy.v1", _planning_hierarchy_v1),
    }
)


def named_validator(name: str) -> NamedValidator:
    """Resolve one registered validator; an unknown name refuses."""
    validator = _NAMED_VALIDATORS.get(name)
    if validator is None:
        raise CollectionTypeError(
            "UNKNOWN_NAMED_VALIDATOR", "validator is not registered", "validators"
        )
    return validator


def _string(value: object, location: str) -> str:
    if type(value) is not str or not value:
        raise CollectionTypeError("INVALID_DECLARATION", "expected a non-empty string", location)
    return value


def _frozen(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _frozen(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_frozen(item) for item in value)
    return value


def parse_declaration(data: object, *, builtin: bool) -> CollectionType:
    """Parse one declaration's closed shape into a :class:`CollectionType`."""
    if not isinstance(data, Mapping):
        raise CollectionTypeError("INVALID_DECLARATION", "declaration must be a mapping")
    unknown = sorted(set(data) - _DECLARATION_KEYS)
    if unknown:
        raise CollectionTypeError("UNKNOWN_DECLARATION_KEY", "key is not declared", unknown[0])
    missing = sorted(_REQUIRED_KEYS - set(data))
    if missing:
        raise CollectionTypeError("INVALID_DECLARATION", "required key is missing", missing[0])
    kind = _string(data["kind"], "kind")
    if kind not in KINDS:
        raise CollectionTypeError("UNKNOWN_COLLECTION_KIND", "kind is not supported", "kind")
    audience = _string(data["default_audience"], "default_audience")
    if audience not in DEFAULT_AUDIENCES:
        raise CollectionTypeError(
            "INVALID_DECLARATION", "default audience is not supported", "default_audience"
        )
    version = data["version"]
    if type(version) is not int or version < 1:
        raise CollectionTypeError("INVALID_DECLARATION", "version must be positive", "version")
    raw_validators = data.get("validators", [])
    if not isinstance(raw_validators, list):
        raise CollectionTypeError("INVALID_DECLARATION", "validators must be a list", "validators")
    validators = tuple(named_validator(_string(name, "validators")).name for name in raw_validators)
    wire = data.get("wire")
    if wire is not None:
        if not builtin:
            raise CollectionTypeError(
                "BUILTIN_ONLY_WIRE", "only built-in types carry wire aliases", "wire"
            )
        if not isinstance(wire, Mapping) or set(wire) != _WIRE_KEYS:
            raise CollectionTypeError("INVALID_DECLARATION", "wire map is not closed", "wire")
    raw_fields = data.get("fields") or {}
    if not isinstance(raw_fields, Mapping):
        raise CollectionTypeError("INVALID_DECLARATION", "fields must be a mapping", "fields")
    try:
        fields = {
            _string(name, "fields"): collections._parse_field_spec(spec)  # noqa: SLF001
            for name, spec in raw_fields.items()
        }
    except collections.CollectionError as error:
        raise CollectionTypeError("INVALID_DECLARATION", error.reason, "fields") from error
    natural_key = tuple(data.get("natural_key") or ())
    if any(name not in fields for name in natural_key):
        raise CollectionTypeError(
            "INVALID_DECLARATION", "natural key names an undeclared field", "natural_key"
        )
    extensible = data.get("extensible", False)
    if type(extensible) is not bool:
        raise CollectionTypeError("INVALID_DECLARATION", "extensible must be a boolean")
    views = data.get("views") or []
    if not isinstance(views, list) or not all(isinstance(view, Mapping) for view in views):
        raise CollectionTypeError("INVALID_DECLARATION", "views must be a list of mappings")
    try:
        indexes = normalize_indexes(raw_fields, data.get("indexes"))
    except IndexDeclarationError as error:
        raise CollectionTypeError(error.code, error.reason, error.at) from error
    return CollectionType(
        name=_string(data["name"], "name"),
        version=version,
        title=_string(data["title"], "title"),
        item_type=_string(data["item_type"], "item_type"),
        kind=kind,
        placement=_string(data["placement"], "placement"),
        default_audience=audience,
        builtin=builtin,
        description=str(data.get("description", "")),
        extensible=extensible,
        fields=MappingProxyType(fields),
        natural_key=natural_key,
        views=tuple(_frozen(view) for view in views),
        validators=validators,
        wire=_frozen(wire or {}),
        indexes=indexes,
        declaration=_frozen(data),
    )


def declaration_text(name: str) -> str:
    """The exact shipped text of one built-in declaration."""
    if name not in BUILTIN_TYPE_NAMES:
        raise CollectionTypeError("UNKNOWN_COLLECTION_TYPE", "type is not a built-in")
    return files(__package__.rpartition(".")[0]).joinpath(
        PACKAGE_DIRECTORY, f"{name}.yaml"
    ).read_text(encoding="utf-8")


@functools.cache
def builtin_types() -> Mapping[str, CollectionType]:
    """The shipped built-in types, keyed by name."""
    loaded: dict[str, CollectionType] = {}
    for name in BUILTIN_TYPE_NAMES:
        declared = parse_declaration(yaml.safe_load(declaration_text(name)), builtin=True)
        if declared.name != name:
            raise CollectionTypeError("INVALID_DECLARATION", "built-in name mismatch", name)
        loaded[name] = declared
    return MappingProxyType(loaded)


def type_for_profile(semantic_profile: str) -> CollectionType:
    """Resolve a manifest's ``semantic_profile`` alias to its built-in type."""
    declared = builtin_types().get(semantic_profile)
    if declared is None:
        raise CollectionTypeError(
            "UNKNOWN_COLLECTION_TYPE", "collection type is not registered", "semantic_profile"
        )
    return declared


def type_for_manifest(manifest: collections.CollectionManifest) -> CollectionType:
    """The type a parsed collection manifest belongs to."""
    return type_for_profile(manifest.semantic_profile)


def register_builtins(conn: sqlite3.Connection, *, txn_id: int) -> None:
    """Install shipped declarations inside the caller's mutation transaction.

    Existing versions must match the shipped bytes. Type migrations and
    declared-type authoring belong to P4; neither is silently performed here.
    """
    if not conn.in_transaction:
        raise RuntimeError("built-in registration requires a transaction")
    for name, declared in builtin_types().items():
        text = declaration_text(name)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        declaration = json.dumps(
            yaml.safe_load(text), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        )
        current = conn.execute(
            "SELECT current_version, builtin FROM collection_types WHERE name = ?", (name,)
        ).fetchone()
        if current is not None:
            stored = conn.execute(
                "SELECT declaration_json, declaration_hash FROM collection_type_versions "
                "WHERE name = ? AND version = ?", (name, declared.version)
            ).fetchone()
            if (
                tuple(current) != (declared.version, 1)
                or stored is None
                or tuple(stored) != (declaration, digest)
            ):
                raise CollectionTypeError(
                    "COLLECTION_TYPE_VERSION_MISMATCH", "stored built-in differs from release", name
                )
            continue
        conn.execute("INSERT INTO collection_types VALUES (?, ?, 1)", (name, declared.version))
        conn.execute(
            "INSERT INTO collection_type_versions VALUES (?, ?, ?, ?, 'compatible', ?)",
            (name, declared.version, declaration, digest, txn_id),
        )
