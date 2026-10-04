"""Compiler-owned JSON-v1 projection templates; migrations own their execution."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from ..query_engine.indexes import IndexDeclarationError, IndexSpec, normalize_indexes
from ..query_engine.scalars import MISSING, SCALAR_TYPES, scalar_key


@dataclass(frozen=True, slots=True)
class ScalarProjection:
    field: str
    kind: str
    path: tuple[str | int, ...]


@dataclass(frozen=True, slots=True)
class ProjectionPlan:
    collection_id: str
    generation: int
    scalars: tuple[ScalarProjection, ...]
    indexes: tuple[IndexSpec, ...]

    def __post_init__(self):
        try:
            if str(UUID(self.collection_id)) != self.collection_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError) as error:
            raise IndexDeclarationError("INVALID_INDEX_DECLARATION", "canonical collection UUID required", "collection_id") from error
        if type(self.generation) is not int or self.generation < 1:
            raise IndexDeclarationError("INVALID_INDEX_DECLARATION", "positive generation required", "generation")

    @property
    def table_name(self) -> str:
        return f"cq_{UUID(self.collection_id).hex}_{self.generation}"

    @property
    def index_names(self) -> tuple[str, ...]:
        return tuple(f"{self.table_name}_i{ordinal}" for ordinal in range(len(self.indexes)))

    def key_columns(self, field: str) -> tuple[str, str]:
        for ordinal, scalar in enumerate(self.scalars):
            if scalar.field == field:
                return f"k{ordinal}_tag", f"k{ordinal}_key"
        raise KeyError(field)

    def rank_column(self, field: str) -> str:
        return self.key_columns(field)[0].removesuffix("_tag") + "_rank"

    @property
    def ddl(self) -> tuple[str, ...]:
        return self.create_ddl()

    def create_ddl(self, *, temporary: bool = False) -> tuple[str, ...]:
        columns = ["row_id INTEGER PRIMARY KEY" + ("" if temporary else " REFERENCES items(row_id)"),
                   "item_key TEXT NOT NULL UNIQUE", "row_version INTEGER NOT NULL",
                   "keys_json TEXT NOT NULL CHECK(json_valid(keys_json))"]
        for ordinal in range(len(self.scalars)):
            columns.extend((
                f"k{ordinal}_rank INTEGER GENERATED ALWAYS AS (CASE json_extract(keys_json,'$[{ordinal}][0]') WHEN 0 THEN 2 WHEN 1 THEN 1 ELSE 0 END) VIRTUAL",
                f"k{ordinal}_tag INTEGER GENERATED ALWAYS AS (json_extract(keys_json,'$[{ordinal}][0]')) VIRTUAL",
                f"k{ordinal}_key TEXT COLLATE BINARY GENERATED ALWAYS AS (json_extract(keys_json,'$[{ordinal}][1]')) VIRTUAL",
            ))
        prefix = "TEMP " if temporary else ""
        statements = [f"CREATE {prefix}TABLE {self.table_name}({','.join(columns)}) STRICT"]
        for name, index in zip(self.index_names, self.indexes, strict=True):
            keys = []
            for key in index.keys:
                direction = "DESC" if key.direction == "desc" else "ASC"
                tag, payload = self.key_columns(key.field)
                keys.extend((f"{self.rank_column(key.field)} ASC", f"{tag} ASC", f"{payload} {direction}"))
            keys.append("item_key ASC")
            schema = "temp." if temporary else ""
            statements.append(f"CREATE INDEX {schema}{name} ON {self.table_name}({','.join(keys)})")
        return tuple(statements)

    @property
    def upsert_sql(self) -> str:
        return (f"INSERT INTO {self.table_name}(row_id,item_key,row_version,keys_json) VALUES(?,?,?,?) "
                "ON CONFLICT(row_id) DO UPDATE SET item_key=excluded.item_key,"
                "row_version=excluded.row_version,keys_json=excluded.keys_json")

    def encode(self, values: Mapping) -> str:
        """Extract only declared scalar paths; authored field names stay in Python."""
        keys = []
        for scalar in self.scalars:
            value = values
            for part in scalar.path:
                if isinstance(part, str) and isinstance(value, Mapping):
                    value = value.get(part, MISSING)
                elif type(part) is int and isinstance(value, list | tuple) and part < len(value):
                    value = value[part]
                else:
                    value = MISSING
                    break
            keys.append(scalar_key(value, scalar.kind))
        return json.dumps(keys, separators=(",", ":"), allow_nan=False)


def build_projection_plan(collection_id: str, fields: Mapping, indexes: tuple[IndexSpec, ...], *,
                          generation: int = 1, scalar_paths: Mapping | None = None) -> ProjectionPlan:
    """Freeze a canonical collection's validated logical index intent.

    These templates do not install, backfill or mark a projection ready. Their
    caller must execute DDL and row maintenance inside its writer transaction.
    """
    # Reuse the declaration validator, including its count/path/direction caps.
    declarations = {index.name: {"keys": [{"field": key.field, "direction": key.direction}
                                          for key in index.keys]} for index in indexes}
    if len(declarations) != len(indexes):
        raise IndexDeclarationError("INVALID_INDEX_DECLARATION", "index names must be unique", "indexes")
    normalized = normalize_indexes(fields, declarations)
    scalars = []
    paths = {} if scalar_paths is None else scalar_paths
    for index in normalized:
        for key in index.keys:
            if any(scalar.field == key.field for scalar in scalars):
                continue
            kind = fields[key.field].get("type")
            path = paths.get(key.field, (key.field,))
            if kind not in SCALAR_TYPES or not isinstance(path, tuple) or not path or any(
                type(part) is not str and not (type(part) is int and part >= 0) for part in path
            ):
                raise IndexDeclarationError("INVALID_INDEX_DECLARATION", "declared scalar path required", f"fields.{key.field}")
            scalars.append(ScalarProjection(key.field, kind, path))
    return ProjectionPlan(collection_id, generation, tuple(scalars), normalized)
