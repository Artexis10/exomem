"""Shared physical payload columns for native current and retained history rows."""

from functools import lru_cache

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
    func,
    literal,
    or_,
)

from .. import item_keys
from . import native_identity, tables
from .native_layout import MASK_BITS, NativeLayout
from .typed_storage import _AnyValue


def payload_columns(layout: NativeLayout) -> list:
    extent = Column("values_bytes", Integer, nullable=False)
    columns = [extent, CheckConstraint(extent >= 2)]
    masks = []
    for ordinal, name in enumerate(layout.mask_columns):
        mask = Column(name, Integer, nullable=False)
        width = min(MASK_BITS, len(layout.fields) - ordinal * MASK_BITS)
        columns.extend((mask, CheckConstraint(mask.between(0, (1 << width) - 1))))
        masks.append(mask)
    for ordinal in range(len(layout.fields)):
        value = Column(f"v{ordinal}", _AnyValue)
        missing = masks[ordinal // MASK_BITS].op("&")(literal(1 << (ordinal % MASK_BITS)))
        columns.extend((value, CheckConstraint(or_(missing == 0, value.is_(None)))))
    residual = Column("r", Text)
    columns.extend((residual, CheckConstraint(or_(residual.is_(None),
                   (func.json_valid(residual) == 1) & (func.json_type(residual) == "object")))))
    return columns


def current(layout: NativeLayout, name: str, *, canonical: bool = True, temporary: bool = False) -> Table:
    """Keep payload cells incrementally readable; the query plan adds expression indexes."""
    metadata = MetaData()
    if canonical and not temporary:
        items = tables.items.to_metadata(metadata)
        reference = (ForeignKey(items.c.row_id),)
    else:
        # Derived and admitted TEMP rows have no cross-file foreign key authority.
        reference = ()
    row_id = Column("row_id", Integer, *reference, primary_key=True)
    identity = Column("item_key", LargeBinary, nullable=False, unique=True)
    columns = [row_id, identity, Column("row_version", Integer, nullable=False), *payload_columns(layout)]
    if layout.identity == native_identity.UUID16:
        columns.append(CheckConstraint(func.length(identity) == 16))
    if layout.identity == native_identity.ORDERED:
        columns.append(CheckConstraint(func.length(identity).between(1, item_keys.MAX_KEY_BYTES)))
    return Table(name, metadata, *columns, schema="temp" if temporary else None, sqlite_strict=True)


@lru_cache(maxsize=64)
def history(layout: NativeLayout) -> Table:
    metadata = MetaData()
    identity = tables.version_identity.to_metadata(metadata)
    tables.items.to_metadata(metadata)
    return Table(layout.version_table, metadata,
        Column("row_id", Integer, primary_key=True), Column("row_version", Integer, primary_key=True),
        Column("body", Text, nullable=False), *payload_columns(layout),
        ForeignKeyConstraint(["row_id", "row_version"], [identity.c.row_id, identity.c.row_version],
                             deferrable=True, initially="DEFERRED"),
        sqlite_strict=True, sqlite_with_rowid=False)
