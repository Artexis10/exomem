"""Immutable logical index intent; names and paths are never executable DDL."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

_SCALARS = frozenset({"string", "integer", "number", "boolean", "date", "datetime", "enum", "link"})


class IndexDeclarationError(ValueError):
    def __init__(self, code: str, reason: str, at: str):
        self.code, self.reason, self.at = code, reason, at
        super().__init__(f"{code}: {reason}")


@dataclass(frozen=True, slots=True)
class IndexKey:
    field: str
    direction: str = "asc"


@dataclass(frozen=True, slots=True)
class IndexSpec:
    name: str
    keys: tuple[IndexKey, ...]


def require_index_dependencies(indexes: tuple[IndexSpec, ...], paths: set[str]) -> None:
    """Dependency removal belongs to a governed revise, not a parser override."""
    leading = {index.keys[0].field for index in indexes}
    missing = sorted(paths - leading)
    if missing:
        raise IndexDeclarationError("INDEX_DEPENDENCY_REQUIRED", "an enabled view or relation needs this index",
                                    f"fields.{missing[0]}")


def normalize_indexes(fields: Mapping, declarations: object = None) -> tuple[IndexSpec, ...]:
    """Coalesce declared flags/prefixes, then enforce the physical index budget.

    This does not create objects or authorize dropping inherited dependencies.
    Migration owns physical ordinal identifiers, readiness and publication.
    """
    result = []
    flagged = []

    def invalid(reason, at):
        raise IndexDeclarationError("INVALID_INDEX_DECLARATION", reason, at)

    def scalar(path, at):
        if not isinstance(path, str) or path not in fields:
            invalid("index key must name a declared scalar path", at)
        if fields[path].get("type") not in _SCALARS:
            invalid("arrays and objects require a declared scalar subfield", at)
        return path

    for path, specification in fields.items():
        for flag in ("filterable", "sortable"):
            value = specification.get(flag, False)
            if type(value) is not bool:
                invalid("index flags must be boolean", f"fields.{path}.{flag}")
        if specification.get("filterable") or specification.get("sortable"):
            flagged.append(scalar(path, f"fields.{path}"))
    declared = {} if declarations is None else declarations
    if not isinstance(declared, Mapping):
        invalid("indexes must be a mapping of names to key declarations", "indexes")
    if len(declared) > 8:
        raise IndexDeclarationError("INDEX_BUDGET_EXCEEDED", "at most 8 secondary indexes", "indexes")
    for name, specification in declared.items():
        at = f"indexes.{name}"
        if not isinstance(name, str) or not name.strip():
            invalid("index name must be nonempty text", at)
        if not isinstance(specification, Mapping) or set(specification) != {"keys"}:
            invalid("index declaration must contain only keys", at)
        keys = specification["keys"]
        if not isinstance(keys, list) or not keys:
            invalid("index keys must be a nonempty array", at)
        if len(keys) > 4:
            raise IndexDeclarationError("INDEX_BUDGET_EXCEEDED", "at most 4 keys per index", at)
        parsed = []
        for number, key in enumerate(keys):
            key_at = f"{at}.keys.{number}"
            if not isinstance(key, Mapping) or set(key) - {"field", "direction"} or "field" not in key:
                invalid("index key accepts field and optional direction", key_at)
            path = scalar(key["field"], f"{key_at}.field")
            direction = key.get("direction", "asc")
            if direction not in ("asc", "desc"):
                invalid("index direction must be asc or desc", f"{key_at}.direction")
            if any(existing.field == path for existing in parsed):
                invalid("an index cannot repeat a path", key_at)
            parsed.append(IndexKey(path, direction))
        result.append(IndexSpec(name, tuple(parsed)))
    # A composite prefix serves the leading field. A later key without a
    # constrained prefix is not a general single-field access path.
    covered = {index.keys[0].field for index in result}
    for path in flagged:
        if path not in covered:
            # Logical synthetic labels need not be SQL identifiers. Their
            # separate namespace cannot collide with an authored index name.
            label = f"field:{path}"
            while label in declared:
                label = ":" + label
            result.append(IndexSpec(label, (IndexKey(path),)))
            covered.add(path)
    if len(result) > 8:
        raise IndexDeclarationError("INDEX_BUDGET_EXCEEDED", "at most 8 secondary indexes", "indexes")
    if len({key.field for index in result for key in index.keys}) > 16:
        raise IndexDeclarationError("INDEX_BUDGET_EXCEEDED", "at most 16 indexed scalar paths", "indexes")
    return tuple(result)
