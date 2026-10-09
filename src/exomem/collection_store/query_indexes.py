"""Compiler-owned JSON-v1 projection templates; migrations own their execution."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Column,
    Computed,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    bindparam,
    case,
    func,
)
from sqlalchemy.dialects.sqlite import dialect, insert
from sqlalchemy.schema import CreateIndex, CreateTable

from ..query_engine.indexes import IndexDeclarationError, IndexSpec, normalize_indexes
from ..query_engine.scalars import MISSING, SCALAR_TYPES, scalar_key
from . import tables


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
        table = _declaration(self, temporary)
        return (str(CreateTable(table).compile(dialect=dialect())),
                *(str(CreateIndex(index).compile(dialect=dialect())) for index in sorted(table.indexes, key=lambda index: index.name)))

    @property
    def upsert(self):
        return _upsert(self)

    @property
    def upsert_sql(self) -> str:
        """The same Core statement compiled for dedicated raw query fixtures."""
        return _upsert_sql(self)

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


@lru_cache(maxsize=64)
def _declaration(plan: ProjectionPlan, temporary: bool) -> Table:
    metadata = MetaData()
    items = tables.items.to_metadata(metadata)
    row_id = Column("row_id", Integer, *(() if temporary else (ForeignKey(items.c.row_id),)), primary_key=True)
    keys_json = Column("keys_json", Text, nullable=False)
    columns = [row_id, Column("item_key", Text, nullable=False, unique=True),
               Column("row_version", Integer, nullable=False), keys_json,
               CheckConstraint(func.json_valid(keys_json))]
    for ordinal in range(len(plan.scalars)):
        tag = func.json_extract(keys_json, f"$[{ordinal}][0]")
        columns.extend((
            Column(f"k{ordinal}_rank", Integer, Computed(case((tag == 0, 2), (tag == 1, 1), else_=0), persisted=False)),
            Column(f"k{ordinal}_tag", Integer, Computed(tag, persisted=False)),
            Column(f"k{ordinal}_key", Text(collation="BINARY"), Computed(func.json_extract(keys_json, f"$[{ordinal}][1]"), persisted=False)),
        ))
    table = Table(plan.table_name, metadata, *columns, schema="temp" if temporary else None, sqlite_strict=True)
    for name, index in zip(plan.index_names, plan.indexes, strict=True):
        keys: list[Any] = []
        for key in index.keys:
            tag, payload = plan.key_columns(key.field)
            keys.extend((table.c[plan.rank_column(key.field)].asc(), table.c[tag].asc(),
                         table.c[payload].desc() if key.direction == "desc" else table.c[payload].asc()))
        Index(name, *keys, table.c.item_key.asc())
    return table


@lru_cache(maxsize=64)
def _upsert(plan: ProjectionPlan):
    table = _declaration(plan, False)
    statement = insert(table).values({name: bindparam(name) for name in ("row_id", "item_key", "row_version", "keys_json")})
    return statement.on_conflict_do_update(index_elements=[table.c.row_id], set_={
        name: statement.excluded[name] for name in ("item_key", "row_version", "keys_json")})


@lru_cache(maxsize=64)
def _upsert_sql(plan: ProjectionPlan) -> str:
    return str(plan.upsert.compile(dialect=dialect()))


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
