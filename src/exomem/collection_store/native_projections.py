"""Native payload relations with compiler-owned indexed comparison expressions."""

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy import (
    Column,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    bindparam,
    case,
    func,
    literal,
)
from sqlalchemy.dialects.sqlite import insert

from ..query_engine.scalars import (
    BOOLEAN_TAG,
    LINK_TAG,
    MISSING_TAG,
    NULL_TAG,
    NUMBER_TAG,
    STRING_TAG,
)
from . import native_identity, native_numbers, native_operands, native_rows, native_tables
from .native_layout import MASK_BITS, Codec, NativeLayout
from .query_indexes import ProjectionPlan
from .typed_storage import TYPED_V2, _AnyValue, canonical_json


@dataclass(frozen=True, slots=True)
class NativeProjectionPlan(ProjectionPlan):
    layout: NativeLayout
    role: native_operands.FileRole = native_operands.FileRole.STORE

    def __post_init__(self):
        ProjectionPlan.__post_init__(self)
        if not isinstance(self.layout, NativeLayout) or self.layout.collection_id != self.collection_id:
            raise ValueError("native projection requires its immutable payload layout")
        if type(self.role) is not native_operands.FileRole:
            raise ValueError("native projection requires its physical file role")

    def band_column(self, field: str) -> str:
        return self.key_columns(field)[0].removesuffix("_tag") + "_band"

    def read_relation(self, schema: str, *, index: str | None = None, not_indexed: bool = False) -> str:
        """Expose shared index expressions while keeping hints on their physical table."""
        from sqlalchemy.dialects.sqlite import dialect

        quote = dialect().identifier_preparer.quote
        hint = " NOT INDEXED" if not_indexed else " INDEXED BY " + quote(index) if index is not None else ""
        return (f"(SELECT n.*,{_relation_columns(self)} FROM {quote(schema)}.{quote(self.table_name)} AS n{hint})")

    def create_ddl(self, *, temporary: bool = False) -> tuple[str, ...]:
        from sqlalchemy.dialects.sqlite import dialect
        from sqlalchemy.schema import CreateIndex, CreateTable

        table = declaration(self, temporary)
        return (str(CreateTable(table).compile(dialect=dialect())),
                *(str(CreateIndex(index).compile(dialect=dialect())) for index in sorted(table.indexes, key=lambda index: index.name)))

    @property
    def upsert(self):
        return _upsert(self)

    def parameters(self, connection, row_id: int, item_key: str, row_version: int, values) -> dict:
        encoded = native_rows.encode_row(connection, self.layout, values)
        return {"row_id": row_id, "item_key": native_identity.encode(item_key, self.layout.identity), "row_version": row_version,
                **dict(zip((*self.layout.value_columns, "r"), encoded, strict=True))}

    def admitted_parameters(self, row_id: int, item_key: str, row_version: int, values) -> dict:
        """TEMP contains only already-released scalar operands, never source payloads."""
        result = {"row_id": row_id, "item_key": native_identity.encode(item_key, self.layout.identity), "row_version": row_version}
        from ..query_engine.scalars import MISSING

        for ordinal, scalar in enumerate(self.scalars):
            value = values
            for part in scalar.path:
                if type(part) is str and isinstance(value, Mapping):
                    value = value.get(part, MISSING)
                elif type(part) is int and isinstance(value, list | tuple) and part < len(value):
                    value = value[part]
                else:
                    value = MISSING
                    break
            tag, band, key = native_operands.scalar_operand(value, scalar.kind)
            result.update({f"k{ordinal}_rank": 2 if tag == 0 else 1 if tag == 1 else 0,
                           f"k{ordinal}_tag": tag, f"k{ordinal}_band": band, f"k{ordinal}_key": key})
        return result


def _expressions(plan, table, scalar):
    layout = plan.layout
    root = scalar.path[0]
    operand_family = native_operands.family(scalar.kind)
    if type(root) is not str:
        return literal(MISSING_TAG), literal(0), literal("")
    if root in layout.fields:
        ordinal = layout.fields.index(root)
        codec = layout.codecs[ordinal]
        cell = table.c[f"v{ordinal}"]
        absent = table.c[f"m{ordinal // MASK_BITS}"].op("&")(literal(1 << (ordinal % MASK_BITS))) != 0
        path = scalar.path[1:]
    else:
        ordinal, codec, cell, absent, path = -1, Codec.JSON, table.c.r, table.c.r.is_(None), scalar.path
    parts = []
    for component in native_operands.COMPONENTS:
        fallback = getattr(func, native_operands.FUNCTION)(
            codec.value, plan.collection_id, layout.generation, ordinal, operand_family,
            canonical_json(list(path)), plan.role.value, cell, table.c.values_bytes, component)
        shortcut = None
        # These type checks describe the closed wire, not authored field vocabulary.
        if not path and codec is Codec.TEXT and operand_family in ("string", "link"):
            shortcut = (func.typeof(cell) == "text", {"tag": STRING_TAG if operand_family == "string" else LINK_TAG,
                                                   "band": 0, "key": cell}[component])
        elif not path and codec is Codec.BOOLEAN and operand_family == "boolean":
            shortcut = ((func.typeof(cell) == "integer") & cell.in_((literal(0), literal(1))),
                        {"tag": BOOLEAN_TAG, "band": 0, "key": cell}[component])
        elif not path and codec is Codec.NUMBER and operand_family == "number":
            shortcut = (func.typeof(cell).in_(("integer", "real")) & cell.between(literal(native_numbers.LOW),
                                                                                 literal(native_numbers.HIGH)),
                        {"tag": NUMBER_TAG, "band": 1, "key": cell}[component])
        value = case(shortcut, else_=fallback) if shortcut is not None else fallback
        # Walking into null has always meant missing; root-level null remains distinct.
        missing = {"tag": MISSING_TAG, "band": 0, "key": ""}[component]
        null = {"tag": MISSING_TAG if path else NULL_TAG, "band": 0, "key": ""}[component]
        parts.append(case((absent, missing), (cell.is_(None), null), else_=value))
    return tuple(parts)


def _operand_columns(plan, table):
    """One expression source for physical indexes and their read aliases."""
    result = {}
    for ordinal, scalar in enumerate(plan.scalars):
        tag, band, key = _expressions(plan, table, scalar)
        result.update({f"k{ordinal}_rank": case((tag == 0, 2), (tag == 1, 1), else_=0),
                       f"k{ordinal}_tag": tag, f"k{ordinal}_band": band, f"k{ordinal}_key": key})
    return result


@lru_cache(maxsize=64)
def _relation_columns(plan: NativeProjectionPlan) -> str:
    """The read relation's operand select list, compiled once per frozen plan; every query reads it."""
    from sqlalchemy.dialects.sqlite import dialect

    compiler = dialect()
    quote = compiler.identifier_preparer.quote
    table = declaration(plan).alias("n")
    expressions = _operand_columns(plan, table)
    expressions.update({"collection_id": literal(plan.collection_id), "encoding": literal(TYPED_V2),
                        "view_path": literal(None), "created_txn": table.c.row_id, "values_json": literal(None)})
    return ",".join(str(expression.compile(dialect=compiler, compile_kwargs={"literal_binds": True}))
                    + " AS " + quote(name) for name, expression in expressions.items())


@lru_cache(maxsize=64)
def declaration(plan: NativeProjectionPlan, temporary: bool = False) -> Table:
    if temporary:
        table = Table(plan.table_name, MetaData(), Column("row_id", Integer, primary_key=True),
                      Column("item_key", LargeBinary, nullable=False, unique=True), Column("row_version", Integer, nullable=False),
                      schema="temp", sqlite_strict=True)
        for ordinal in range(len(plan.scalars)):
            for part, kind in (("rank", Integer), ("tag", Integer), ("band", Integer), ("key", _AnyValue)):
                table.append_column(Column(f"k{ordinal}_{part}", kind, nullable=False))
    else:
        table = native_tables.current(plan.layout, plan.table_name, canonical=plan.role is native_operands.FileRole.STORE)
    operands = ({name: table.c[name] for name in table.c.keys() if name.startswith("k")}
                if temporary else _operand_columns(plan, table))
    for name, index in zip(plan.index_names, plan.indexes, strict=True):
        keys = []
        for key in index.keys:
            tag, payload = plan.key_columns(key.field)
            direction = "desc" if key.direction == "desc" else "asc"
            keys.extend((operands[plan.rank_column(key.field)].asc(), operands[tag].asc(),
                         getattr(operands[plan.band_column(key.field)], direction)(), getattr(operands[payload], direction)()))
        Index(name, *keys, table.c.item_key.asc())
    return table


@lru_cache(maxsize=64)
def _upsert(plan):
    table = declaration(plan)
    names = ("row_id", "item_key", "row_version", *plan.layout.value_columns, "r")
    statement = insert(table).values({name: bindparam(name) for name in names})
    return statement.on_conflict_do_update(index_elements=[table.c.row_id],
                                         set_={name: statement.excluded[name] for name in names if name != "row_id"})
