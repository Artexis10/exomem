"""Closed v1 parsing and declaration binding, without reading item values."""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from ..structured_collections import CollectionError, FieldSpec, validate_field_value
from .buckets import EXTREMES
from .ir import (
    Aggregate,
    Field,
    Filter,
    GroupKey,
    Join,
    NamedAggregate,
    Page,
    Path,
    Project,
    Query,
    Range,
    RelativeDate,
    Sort,
    SortKey,
    Source,
    Traverse,
)
from .scalars import ScalarValueError, parse_instant

_KEYS = frozenset(
    {
        "version",
        "source",
        "where",
        "order_by",
        "select",
        "page",
        "group_by",
        "aggregates",
        "having",
        "joins",
        "text",
        "as_of",
        "mode",
        "execution_profile",
        "graph",
    }
)
_OPS = frozenset(
    {
        "eq",
        "ne",
        "gt",
        "gte",
        "lt",
        "lte",
        "contains",
        "icontains",
        "startswith",
        "in",
        "nin",
        "exists",
        "missing",
        "is_null",
        "is_missing",
        "is_not_null",
        "between",
    }
)
_NULL_OPS = frozenset({"exists", "missing", "is_null", "is_missing", "is_not_null"})
#: The v1 grammar's closed choices and input bounds (design §8, §11). The
#: query-engine discovery chapter reports these same objects.
MODES = ("compose", "explain", "preview", "dry_run", "execute")
PROFILES = ("interactive", "analytics")
# nosemgrep: ep-word-set -- The query grammar fixes these bucket granularities.
BUCKETS = ("day", "week", "month")
AGGREGATE_OPS = ("count", "sum", "avg", "min", "max", "latest", "percentile", "distinct_count")
LIMITS = {
    "query_bytes": 16 * 1024,
    "predicate_leaves": 64,
    "boolean_depth": 8,
    "membership_values": 100,
    "select": 32,
    "order_by": 4,
    "group_by": 4,
    "aggregates": 8,
    "joins": 2,
    "page_default": 50,
    "row_page": 1000,
    "group_page": 200,
}
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_SCALARS = frozenset({"string", "integer", "number", "boolean", "date", "datetime", "enum", "link"})
_LEGACY = frozenset(
    {
        "filters",
        "columns",
        "sort_by",
        "sort_desc",
        "limit",
        "offset",
        "aggregate",
        "date_from",
        "date_to",
        "date_column",
    }
)


@dataclass(frozen=True, slots=True)
class Finding:
    code: str
    at: str
    expected: str
    allowed: tuple[str, ...]
    repair: str
    retryable: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "at": self.at,
            "expected": self.expected,
            "allowed": list(self.allowed),
            "repair": self.repair,
            "retryable": self.retryable,
        }


@dataclass(frozen=True, slots=True)
class ValidationResult:
    query: Query | None
    findings: tuple[Finding, ...] = ()


@dataclass(frozen=True, slots=True)
class GraphValidationResult:
    node: Traverse | Path | None
    findings: tuple[Finding, ...] = ()


class _Invalid(Exception):
    def __init__(self, finding: Finding):
        self.finding = finding


def _fail(
    code: str, at: str, expected: str, allowed=(), repair="Revise the addressed query field."
):
    raise _Invalid(Finding(code, at, expected, tuple(sorted(allowed)), repair))


def _object(value: Any, at: str, allowed, required=()) -> Mapping:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        _fail("QUERY_VALUE_INVALID", at, "object with string keys")
    unknown = value.keys() - set(allowed)
    if unknown:
        key = sorted(unknown)[0]
        _fail("QUERY_KEY_UNKNOWN", f"{at}.{key}" if at else key, "closed query object", allowed)
    for key in required:
        if key not in value:
            _fail("QUERY_VALUE_INVALID", f"{at}.{key}" if at else key, "required field")
    return value


def _list(value: Any, at: str, maximum: int) -> list:
    if not isinstance(value, list):
        _fail("QUERY_VALUE_INVALID", at, "array")
    if len(value) > maximum:
        _fail("QUERY_INPUT_LIMIT", at, f"at most {maximum} entries")
    return value


def _choice(value: Any, at: str, allowed) -> str:
    if not isinstance(value, str) or value not in allowed:
        _fail("QUERY_VALUE_INVALID", at, "closed enumerated value", allowed)
    return value


def _integer(value: Any, at: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or value < minimum:
        _fail("QUERY_VALUE_INVALID", at, f"integer in [{minimum}, {maximum}]")
    if value > maximum:
        _fail("QUERY_INPUT_LIMIT", at, f"integer in [{minimum}, {maximum}]")
    return value


def _name(value: Any, at: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        _fail("QUERY_VALUE_INVALID", at, "identifier of at most 64 characters")
    return value


def _ref(value: Any, at: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("QUERY_VALUE_INVALID", at, "nonempty stable reference")
    return value


def _bounded_json(value: Any, at: str) -> None:
    try:
        size = len(
            json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
    except (TypeError, ValueError, OverflowError, RecursionError):
        _fail("QUERY_VALUE_INVALID", at, "bounded JSON object")
    if size > LIMITS["query_bytes"]:
        _fail("QUERY_INPUT_LIMIT", at, f"at most {LIMITS['query_bytes'] // 1024} KiB")


def _instant(value: Any, at: str) -> str:
    try:
        return parse_instant(value).isoformat()
    except ScalarValueError:
        _fail("QUERY_VALUE_INVALID", at, "extended ISO seconds with exact microseconds and UTC/minute offset")


def _value(value: Any, field: Field, at: str) -> Any:
    kind = field.value_type
    if isinstance(value, dict) and kind in {"date", "datetime"}:
        raw = _object(
            value, at, {"relative_to", "amount", "unit"}, {"relative_to", "amount", "unit"}
        )
        _choice(raw["relative_to"], f"{at}.relative_to", {"as_of"})
        if type(raw["amount"]) is not int:
            _fail("QUERY_VALUE_INVALID", f"{at}.amount", "integer")
        return RelativeDate(
            raw["amount"], _choice(raw["unit"], f"{at}.unit", {"day", "week", "month"})
        )
    if kind == "datetime":
        return _instant(value, at)
    if value is None:
        _fail("QUERY_VALUE_INVALID", at, f"declared {kind} literal")
    try:
        # Predicate literals need not be storable enum members.
        validate_field_value(field.path, value, FieldSpec(type=kind))
    except CollectionError:
        _fail("QUERY_VALUE_INVALID", at, f"declared {kind} literal")
    if kind == "date":
        return dt.date.fromisoformat(value).isoformat()
    return value


class _Binder:
    def __init__(self, source: Source, declarations: Mapping):
        self.source = source
        self.declarations = declarations
        self.aliases: dict[str, Source] = {}
        self.reduction_fields: dict[str, Field] | None = None
        self.leaves = 0

    def field(self, name: Any, at: str, *, reduction=False, evaluated=False) -> Field:
        """Bind a declared field; ``evaluated`` marks a use that SQL evaluates on stored values."""
        if reduction:
            fields = self.reduction_fields or {}
            if isinstance(name, str) and name in fields:
                return fields[name]
            if self.declarations[self.source.ref].get("field_admission"):
                _fail("QUERY_FIELD_UNAVAILABLE", at, "an admitted field", (), "Describe the admitted collection fields.")
            _fail(
                "QUERY_FIELD_UNKNOWN",
                at,
                "group key or aggregate alias",
                fields,
                "Use a declared group key or aggregate alias.",
            )
        source, alias, path = self.source, None, name
        if isinstance(name, str) and "." in name:
            prefix, suffix = name.split(".", 1)
            if prefix in self.aliases:
                source, alias, path = self.aliases[prefix], prefix, suffix
        fields = self.declarations[source.ref].get("fields", {})
        if not isinstance(path, str) or path not in fields:
            if self.declarations[source.ref].get("field_admission"):
                _fail("QUERY_FIELD_UNAVAILABLE", at, "an admitted field", (), "Describe the admitted collection fields.")
            allowed = list(self.declarations[self.source.ref].get("fields", {}))
            allowed.extend(
                f"{a}.{p}"
                for a, s in self.aliases.items()
                for p in self.declarations[s.ref].get("fields", {})
            )
            _fail(
                "QUERY_FIELD_UNKNOWN",
                at,
                "declared field path",
                allowed,
                "Describe the source and choose a declared field.",
            )
        if evaluated and fields[path].get("projected"):
            # Each caller sees this field's values only after projection, so a filter, sort or join
            # on the stored values would reveal what a row omits, to the owner as to anyone else.
            _fail("QUERY_FIELD_UNAVAILABLE", at, "a field stored as each caller sees it", (),
                  "Select, aggregate or group the field; filter and sort on another field.")
        kind = fields[path].get("type")
        enum = tuple(fields[path].get("enum", ()))
        if kind == "enum":
            scalar_types = {str: "string", int: "number", float: "number", bool: "boolean"}
            kinds = {scalar_types.get(type(value)) for value in enum}
            if len(kinds) != 1 or None in kinds:
                _fail("QUERY_UNSUPPORTED", at, "one declared scalar type for enum values")
            kind = next(iter(kinds))
        if kind not in _SCALARS | {"object", "array"}:
            _fail("QUERY_UNSUPPORTED", at, "supported declared field type")
        return Field(source, path, kind, alias, enum)

    def predicate(self, value: Any, at: str, depth=0, *, reduction=False) -> Filter:
        if depth > LIMITS["boolean_depth"]:
            _fail("QUERY_INPUT_LIMIT", at, f"boolean depth at most {LIMITS['boolean_depth']}")
        raw = _object(value, at, {"all", "any", "not", "field", "op", "value"})
        boolean = raw.keys() & {"all", "any", "not"}
        if boolean:
            if len(raw) != 1:
                _fail("QUERY_VALUE_INVALID", at, "one boolean operator")
            op = next(iter(boolean))
            children = [raw[op]] if op == "not" else _list(raw[op], f"{at}.{op}", LIMITS["predicate_leaves"])
            if not children:
                _fail("QUERY_VALUE_INVALID", at, "nonempty boolean operands")
            return Filter(
                op,
                children=tuple(
                    self.predicate(
                        child,
                        f"{at}.{op}" if op == "not" else f"{at}.{op}[{i}]",
                        depth + 1,
                        reduction=reduction,
                    )
                    for i, child in enumerate(children)
                ),
            )
        raw = _object(raw, at, {"field", "op", "value"}, {"field", "op"})
        self.leaves += 1
        if self.leaves > LIMITS["predicate_leaves"]:
            _fail("QUERY_INPUT_LIMIT", "where", f"at most {LIMITS['predicate_leaves']} predicate leaves")
        field = self.field(raw["field"], f"{at}.field", reduction=reduction, evaluated=True)
        op = raw["op"]
        if not isinstance(op, str) or op not in _OPS:
            _fail("QUERY_OPERATOR_UNKNOWN", f"{at}.op", "closed predicate operator", _OPS)
        if op in _NULL_OPS:
            _object(raw, at, {"field", "op"})
            return Filter(op, field)
        if field.value_type not in _SCALARS:
            _fail("QUERY_UNSUPPORTED", f"{at}.op", "scalar comparison")
        if op in {"contains", "icontains", "startswith"} and field.value_type != "string":
            _fail("QUERY_OPERATOR_UNKNOWN", f"{at}.op", "string field operation")
        if op in {"gt", "gte", "lt", "lte", "between"} and field.value_type == "boolean":
            _fail("QUERY_OPERATOR_UNKNOWN", f"{at}.op", "ordered field operation")
        if "value" not in raw:
            _fail("QUERY_VALUE_INVALID", f"{at}.value", "required typed literal")
        value_at = f"{at}.value"
        if op in {"in", "nin"}:
            values = _list(raw["value"], value_at, LIMITS["membership_values"])
            literal = tuple(_value(v, field, f"{value_at}[{i}]") for i, v in enumerate(values))
        elif op == "between":
            bounds = _object(
                raw["value"],
                value_at,
                {"lower", "upper", "include_lower", "include_upper"},
                {"lower", "upper"},
            )
            for flag in ("include_lower", "include_upper"):
                if flag in bounds and type(bounds[flag]) is not bool:
                    _fail("QUERY_VALUE_INVALID", f"{value_at}.{flag}", "boolean")
            lower, upper = (
                _value(bounds[key], field, f"{value_at}.{key}") for key in ("lower", "upper")
            )
            if isinstance(lower, RelativeDate) or isinstance(upper, RelativeDate):
                if not (
                    isinstance(lower, RelativeDate)
                    and isinstance(upper, RelativeDate)
                    and lower.unit == upper.unit
                ):
                    _fail(
                        "QUERY_UNSUPPORTED", value_at, "range with independently comparable bounds"
                    )
                reversed_range = lower.amount > upper.amount
            else:
                reversed_range = lower > upper
            if reversed_range:
                _fail("QUERY_VALUE_INVALID", value_at, "lower bound no greater than upper")
            literal = Range(
                lower, upper, bounds.get("include_lower", True), bounds.get("include_upper", True)
            )
        else:
            literal = _value(raw["value"], field, value_at)
        return Filter(op, field, literal)

    def joins(self, values: Any) -> tuple[Join, ...]:
        joins = []
        ancestors = {self.source.ref: (self.source.ref,)}
        for i, value in enumerate(_list(values, "joins", LIMITS["joins"])):
            at = f"joins[{i}]"
            raw = _object(value, at, {"relation", "alias", "kind"}, {"relation", "alias", "kind"})
            alias = _name(raw["alias"], f"{at}.alias")
            base_fields = self.declarations[self.source.ref].get("fields", {})
            if alias in self.aliases or any(
                path == alias or path.startswith(alias + ".") for path in base_fields
            ):
                _fail("QUERY_RELATION_INVALID", f"{at}.alias", "unambiguous unique alias")
            kind = _choice(raw["kind"], f"{at}.kind", {"inner", "left"})
            parent, parent_alias, relation_name = self.source, None, raw["relation"]
            if isinstance(relation_name, str) and "." in relation_name:
                parent_alias, relation_name = relation_name.split(".", 1)
                if parent_alias not in self.aliases:
                    _fail(
                        "QUERY_RELATION_INVALID",
                        f"{at}.relation",
                        "previously bound relation alias",
                    )
                parent = self.aliases[parent_alias]
            relations = self.declarations[parent.ref].get("relations", {})
            if not isinstance(relation_name, str) or relation_name not in relations:
                _fail("QUERY_RELATION_INVALID", f"{at}.relation", "declared relation", relations)
            relation = relations[relation_name]
            if relation.get("cardinality") not in {"many-to-one", "one-to-one"}:
                _fail("QUERY_UNSUPPORTED", f"{at}.relation", "many-to-one or one-to-one relation")
            target_ref = relation.get("target")
            target = self.declarations.get(target_ref)
            parent_decl = self.declarations[parent.ref]
            if (
                target is None
                or target.get("type") != relation.get("target_type")
                or target.get("vault") != parent_decl.get("vault")
                or target.get("domain") != "collections"
                or target_ref in ancestors[parent.ref]
            ):
                _fail(
                    "QUERY_RELATION_INVALID",
                    f"{at}.relation",
                    "acyclic same-vault typed target binding",
                )
            field_name = (
                f"{parent_alias}.{relation['field']}" if parent_alias else relation["field"]
            )
            source_field = self.field(field_name, f"{at}.relation", evaluated=True)
            if source_field.value_type not in {"string", "link"}:
                _fail("QUERY_RELATION_INVALID", f"{at}.relation", "opaque reference field")
            bound = Source("collections", target_ref, target["type"])
            self.aliases[alias] = bound
            ancestors[bound.ref] = (*ancestors[parent.ref], bound.ref)
            joins.append(
                Join(
                    parent, relation_name, source_field, bound, alias, kind, relation["cardinality"]
                )
            )
        return tuple(joins)

    def aggregate(self, groups: Any, values: Any) -> Aggregate:
        keys = []
        self.reduction_fields = {}
        for i, value in enumerate(_list(groups, "group_by", LIMITS["group_by"])):
            at = f"group_by[{i}]"
            raw = _object(value, at, {"field", "bucket", "from", "to"}, {"field"})
            # Reductions group projected values, so a projected field may group.
            field = self.field(raw["field"], f"{at}.field")
            if raw["field"] in self.reduction_fields:
                _fail("QUERY_VALUE_INVALID", f"{at}.field", "unique group field path")
            if field.value_type not in _SCALARS:
                _fail("QUERY_UNSUPPORTED", at, "scalar group key")
            bucket = None
            if "bucket" in raw:
                bucket = _choice(raw["bucket"], f"{at}.bucket", BUCKETS)
                if field.value_type not in {"date", "datetime"}:
                    _fail("QUERY_VALUE_INVALID", f"{at}.bucket", "declared date or datetime field")
            window = []
            for end in ("from", "to"):
                if end not in raw:
                    window.append(None)
                    continue
                if bucket is None:
                    _fail("QUERY_KEY_UNKNOWN", f"{at}.{end}", "a local-day window only on a time bucket")
                day = raw[end]
                try:
                    valid = type(day) is str and len(day) == 10 and dt.date.fromisoformat(day).isoformat() == day
                except ValueError:
                    valid = False
                if not valid:
                    _fail("QUERY_VALUE_INVALID", f"{at}.{end}", "ISO local date YYYY-MM-DD")
                window.append(day)
            if None not in window and window[0] > window[1]:
                _fail("QUERY_VALUE_INVALID", f"{at}.to", "window end on or after its start")
            self.reduction_fields[raw["field"]] = (
                replace(field, value_type="date") if bucket else field
            )
            keys.append(GroupKey(field, bucket, *window))
        raw_values = _object(
            values, "aggregates", values.keys() if isinstance(values, dict) else ()
        )
        if len(raw_values) > LIMITS["aggregates"]:
            _fail("QUERY_INPUT_LIMIT", "aggregates", f"at most {LIMITS['aggregates']} named aggregates")
        aggregates = []
        for name, value in raw_values.items():
            at = f"aggregates.{name}"
            _name(name, at)
            if name in self.reduction_fields:
                _fail("QUERY_VALUE_INVALID", at, "alias distinct from group keys")
            raw = _object(value, at, {"op", "field", "p"}, {"op"})
            op = _choice(raw["op"], f"{at}.op", AGGREGATE_OPS)
            field = self.field(raw["field"], f"{at}.field") if "field" in raw else None
            if field is None and op != "count":
                _fail("QUERY_VALUE_INVALID", f"{at}.field", "required declared field")
            if op == "latest" and not any(key.bucket for key in keys):
                # Recency is the bucket's source-local time basis; without one it is undefined.
                _fail("QUERY_VALUE_INVALID", f"{at}.op", "latest needs a time bucket group key")
            if field is not None and (
                field.value_type not in _SCALARS
                and op != "count"
                or op in {"sum", "avg", "percentile"}
                and field.value_type not in {"number", "integer"}
                or op in {"min", "max"}
                and field.value_type == "boolean"
            ):
                _fail("QUERY_VALUE_INVALID", f"{at}.field", "compatible scalar aggregate field")
            p = None
            if op == "percentile":
                p = raw.get("p")
                if type(p) not in {int, float} or not 0 <= p <= 1 or not math.isfinite(p):
                    _fail("QUERY_VALUE_INVALID", f"{at}.p", "percentile in [0, 1]")
            elif "p" in raw:
                _fail("QUERY_KEY_UNKNOWN", f"{at}.p", "p only for percentile")
            output_type = (
                "integer"
                if op in {"count", "distinct_count"}
                else field.value_type
                if op in EXTREMES
                else "number"
            )
            self.reduction_fields[name] = Field(self.source, name, output_type)
            aggregates.append(NamedAggregate(name, op, field, p))
        if not keys and not aggregates:
            _fail("QUERY_VALUE_INVALID", "aggregates", "nonempty grouping or aggregation")
        return Aggregate(tuple(keys), tuple(aggregates))


def normalize_query(
    query: Any,
    *,
    declarations: Mapping[str, Mapping],
    collection: str | None = None,
    allowed_domains=("collections", "documents", "graph"),
    allowed_types: tuple[str, ...] | None = None,
    legacy_arguments: Mapping | None = None,
    capabilities: frozenset[str] = frozenset(),
) -> ValidationResult:
    """Bind trusted, already-disclosable declarations, never stored rows.

    Declarations keyed by stable source ref provide domain/type/vault, fields
    and relations. Capabilities come from the adapter, not request flags.
    Normalized logical intent must still pass trusted admission before execution.
    """
    try:
        _bounded_json(query, "query")
        raw = _object(query, "", _KEYS, {"version"})
        if type(raw["version"]) is not int or raw["version"] != 1:
            _fail("QUERY_VERSION_UNSUPPORTED", "version", "query version 1", {"1"})
        if legacy_arguments and _LEGACY & legacy_arguments.keys():
            _fail(
                "QUERY_ARGUMENT_CONFLICT",
                "query",
                "one query grammar",
                repair="Remove legacy shaping arguments when supplying query.",
            )
        if "source" in raw:
            source_raw = _object(raw["source"], "source", {"domain", "ref"}, {"domain", "ref"})
            domain, ref = source_raw["domain"], source_raw["ref"]
        else:
            domain, ref = "collections", collection
        if (
            not isinstance(domain, str)
            or domain not in allowed_domains
            or not isinstance(ref, str)
            or ref not in declarations
            or collection is not None
            and (domain != "collections" or ref != collection)
        ):
            _fail("QUERY_SOURCE_INVALID", "source", "source allowed by this facade")
        declaration = declarations[ref]
        if (
            declaration.get("domain") != domain
            or allowed_types is not None
            and declaration.get("type") not in allowed_types
        ):
            _fail("QUERY_SOURCE_INVALID", "source", "declaration matching facade domain/type")
        if domain != "collections" and domain not in capabilities:
            _fail("QUERY_UNSUPPORTED", "source.domain", "explicit trusted adapter capability")
        if domain == "graph":
            _fail("QUERY_UNSUPPORTED", "graph", "settled external graph request grammar")
        source = Source(domain, ref, declaration["type"])
        mode = _choice(raw.get("mode", "execute"), "mode", MODES)
        profile = _choice(raw.get("execution_profile", "interactive"), "execution_profile", PROFILES)
        binder = _Binder(source, declarations)
        joins = binder.joins(raw.get("joins", []))
        where = binder.predicate(raw["where"], "where") if "where" in raw else None
        aggregate = (
            binder.aggregate(raw.get("group_by", []), raw.get("aggregates", {}))
            if "group_by" in raw or "aggregates" in raw
            else None
        )
        if "having" in raw and aggregate is None:
            _fail("QUERY_VALUE_INVALID", "having", "grouped or aggregate query")
        having = (
            binder.predicate(raw["having"], "having", reduction=True) if "having" in raw else None
        )
        fields = tuple(
            binder.field(name, f"select[{i}]", reduction=aggregate is not None)
            for i, name in enumerate(_list(raw.get("select", []), "select", LIMITS["select"]))
        )
        sort = []
        for i, value in enumerate(_list(raw.get("order_by", []), "order_by", LIMITS["order_by"])):
            at = f"order_by[{i}]"
            key = _object(value, at, {"field", "direction", "nulls"}, {"field"})
            field = binder.field(key["field"], f"{at}.field", reduction=aggregate is not None, evaluated=True)
            if field.value_type not in _SCALARS:
                _fail("QUERY_UNSUPPORTED", f"{at}.field", "scalar sort field")
            sort.append(
                SortKey(
                    field,
                    _choice(key.get("direction", "asc"), f"{at}.direction", {"asc", "desc"}),
                    _choice(key.get("nulls", "last"), f"{at}.nulls", {"first", "last"}),
                )
            )
        if aggregate is None and domain == "collections":
            identity = binder.field("item_key", "order_by")
            if not any(key.field == identity for key in sort):
                sort.append(SortKey(identity))
        elif aggregate is not None:
            for group in aggregate.groups:
                name = (
                    f"{group.field.alias}.{group.field.path}"
                    if group.field.alias
                    else group.field.path
                )
                field = binder.field(name, "order_by", reduction=True)
                if not any(key.field == field for key in sort):
                    sort.append(SortKey(field))
        page = _object(raw.get("page", {}), "page", {"limit", "after"})
        limit = _integer(
            page.get("limit", LIMITS["page_default"]),
            "page.limit",
            1,
            LIMITS["group_page"] if aggregate is not None else LIMITS["row_page"],
        )
        after = page.get("after")
        if "after" in page and (not isinstance(after, str) or not after):
            _fail("QUERY_VALUE_INVALID", "page.after", "nonempty opaque cursor")
        as_of = _instant(raw["as_of"], "as_of") if "as_of" in raw else None
        if "text" in raw:
            _fail(
                "QUERY_UNSUPPORTED",
                "text",
                "settled structured text grammar",
                repair="Use declared string predicates until text grammar is available.",
            )
        if "graph" in raw:
            _fail("QUERY_UNSUPPORTED", "graph", "settled graph request grammar")
        return ValidationResult(
            Query(
                source,
                Project(fields),
                where,
                joins,
                aggregate,
                having,
                Sort(tuple(sort)),
                Page(limit, after),
                as_of=as_of,
                mode=mode,
                execution_profile=profile,
            )
        )
    except _Invalid as error:
        return ValidationResult(None, (error.finding,))


def normalize_traversal(
    payload: Any,
    *,
    relation_types: Mapping[str, str],
    capabilities: frozenset[str] = frozenset(),
) -> GraphValidationResult:
    """Normalize the established internal payload; the wire container is unsettled.

    References remain opaque. Existence, release and currentness need admission;
    supplied relation names/identities bind only through trusted declarations.
    """
    try:
        _bounded_json(payload, "graph")
        if "graph" not in capabilities:
            _fail("QUERY_UNSUPPORTED", "graph", "explicit trusted adapter capability")
        raw = _object(
            payload,
            "graph",
            {"anchors", "relations", "min_hops", "max_hops", "direction", "subtypes", "output"},
            {"anchors"},
        )
        anchors = tuple(
            _ref(value, f"graph.anchors[{i}]")
            for i, value in enumerate(_list(raw["anchors"], "graph.anchors", 8))
        )
        if not anchors:
            _fail("QUERY_VALUE_INVALID", "graph.anchors", "at least one anchor")
        minimum = _integer(raw.get("min_hops", 1), "graph.min_hops", 0, 5)
        maximum = _integer(raw.get("max_hops", 3), "graph.max_hops", 0, 5)
        if minimum > maximum:
            _fail("QUERY_VALUE_INVALID", "graph.min_hops", "min_hops no greater than max_hops")
        relations = []
        for i, value in enumerate(_list(raw.get("relations", []), "graph.relations", 16)):
            at = f"graph.relations[{i}]"
            if not isinstance(value, str):
                _fail("QUERY_UNSUPPORTED", at, "declared relation name or stable identity")
            if value in relation_types:
                relations.append(relation_types[value])
            elif value in relation_types.values():
                relations.append(value)
            else:
                _fail("QUERY_RELATION_INVALID", at, "declared relation", relation_types)
        direction = _choice(
            raw.get("direction", "outbound"), "graph.direction", {"outbound", "inbound", "any"}
        )
        output = _choice(raw.get("output", "nodes"), "graph.output", {"nodes", "paths"})
        subtypes = raw.get("subtypes", True)
        if type(subtypes) is not bool:
            _fail("QUERY_VALUE_INVALID", "graph.subtypes", "boolean")
        return GraphValidationResult(
            Traverse(anchors, tuple(relations), minimum, maximum, direction, subtypes, output)
        )
    except _Invalid as error:
        return GraphValidationResult(None, (error.finding,))


def normalize_path(
    payload: Any,
    *,
    capabilities: frozenset[str] = frozenset(),
) -> GraphValidationResult:
    """Normalize a bounded path payload, without resolving endpoints or traversing."""
    try:
        _bounded_json(payload, "path")
        if "graph" not in capabilities:
            _fail("QUERY_UNSUPPORTED", "graph", "explicit trusted adapter capability")
        raw = _object(payload, "path", {"kind", "from", "to", "max_hops"}, {"kind", "from", "to"})
        kind = _choice(raw["kind"], "path.kind", {"shortest", "all"})
        start, end = _ref(raw["from"], "path.from"), _ref(raw["to"], "path.to")
        maximum = _integer(raw.get("max_hops", 3), "path.max_hops", 0, 5)
        return GraphValidationResult(Path(kind, start, end, maximum))
    except _Invalid as error:
        return GraphValidationResult(None, (error.finding,))
