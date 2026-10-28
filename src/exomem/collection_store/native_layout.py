"""Immutable field policies for the native typed-v2 storage wire."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from ..structured_collections import FieldSpec
from . import native_identity, native_numbers
from .typed_storage import TYPED_V2, Layout, TypedStorageError, canonical_json

# These are closed wire policies. Collection fields and enum members remain data.
ESCAPE = "json-escape-v1"
METADATA = "append-only-v1"
EXTENT = "canonical-values-bytes-v1"
MASK_BITS = 63


class Codec(StrEnum):
    TEXT = "text-v1"
    NUMBER = native_numbers.WIRE
    BOOLEAN = "boolean-v1"
    DATE = "date-delta-v1"
    DATETIME = "instant-delta-v1"
    ENUM = "enum-json-id-v1"
    JSON = "json-v1"

    @classmethod
    def for_kind(cls, kind: str) -> Codec:
        # Field kinds are the validator's fixed schema tokens, never source vocabulary.
        match kind:
            case "string" | "link":
                return cls.TEXT
            case "integer" | "number":
                return cls.NUMBER
            case "boolean":
                return cls.BOOLEAN
            case "date":
                return cls.DATE
            case "datetime":
                return cls.DATETIME
            case "enum":
                return cls.ENUM
            case "array" | "object":
                return cls.JSON
        raise TypedStorageError("unsupported native field kind")


@dataclass(frozen=True, slots=True)
class NativeLayout(Layout):
    codecs: tuple[Codec, ...] = ()
    identity: str = native_identity.UTF8
    database: str = "main"

    def __post_init__(self):
        Layout.__post_init__(self)
        if len(self.codecs) != len(self.fields) or any(type(codec) is not Codec for codec in self.codecs):
            raise TypedStorageError("native fields require one frozen codec each")
        if self.identity not in (native_identity.UUID16, native_identity.UTF8, native_identity.ORDERED):
            raise TypedStorageError("unsupported native identity codec")
        if type(self.database) is not str or not self.database:
            raise TypedStorageError("native metadata requires its database namespace")

    @property
    def mask_columns(self) -> tuple[str, ...]:
        return tuple(f"m{ordinal}" for ordinal in range((len(self.fields) + MASK_BITS - 1) // MASK_BITS))

    @property
    def value_columns(self) -> tuple[str, ...]:
        return ("values_bytes", *self.mask_columns, *(f"v{ordinal}" for ordinal in range(len(self.fields))))

    @property
    def current_table(self) -> str:
        if not self.current:
            raise TypedStorageError("native payload requires an explicit query relation")
        return self.current

    @property
    def encoded(self) -> str:
        return canonical_json({"version": 2, "fields": list(self.fields),
                               "codecs": [codec.value for codec in self.codecs],
                               "identity": self.identity, "escape": ESCAPE, "metadata": METADATA, "extent": EXTENT})

    #: A class constant, not a property: scans read it once per row.
    encoding = TYPED_V2


def build(collection_id: str, generation: int, fields: Mapping[str, FieldSpec], *, identity: str) -> NativeLayout:
    names = tuple(sorted(fields))
    return NativeLayout(collection_id, generation, names,
                        codecs=tuple(Codec.for_kind(fields[name].type) for name in names), identity=identity)


def parse(collection_id: str, generation: int, raw: dict) -> NativeLayout:
    if (set(raw) != {"version", "fields", "codecs", "identity", "escape", "metadata", "extent"}
            or type(raw["version"]) is not int or raw["version"] != 2
            or not isinstance(raw["fields"], list) or not isinstance(raw["codecs"], list)
            or raw["escape"] != ESCAPE or raw["metadata"] != METADATA or raw["extent"] != EXTENT):
        raise TypedStorageError("unsupported native layout descriptor")
    try:
        return NativeLayout(collection_id, generation, tuple(raw["fields"]),
                            codecs=tuple(Codec(codec) for codec in raw["codecs"]), identity=raw["identity"])
    except (ValueError, TypeError) as error:
        raise TypedStorageError("invalid native layout descriptor") from error
