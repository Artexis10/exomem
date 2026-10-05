"""The `query_data` MCP tool: structured queries over vault data files (CSV/JSON).

Read-only. `find` discovers a dataset (via its markdown "dataset card");
`query_data` pulls exact rows / aggregates from the raw CSV/JSON the card points
at — filter by column / value / date-range, project columns, sort, paginate, or
aggregate (count / min / max / sum / avg / latest / distinct). KB datasets are
small, so the file is read into memory per call — no index, no new infra
(consistent with the "no vector DB needed at this scale" ethos).

Supports:
- CSV / TSV (header row → columns).
- JSON: a top-level array of objects, OR a nested array located via
  `record_path` (dotted, e.g. "sections.work_incapacity") or common-key
  auto-detect ("result"/"results"/"data"/"rows"/"items"/"entries").
- Dotted column names for nested JSON fields (e.g. "performer.name",
  "id.extension") everywhere a column is named — filters, columns, sort,
  aggregate.

Numeric comparisons coerce tolerantly (Estonian decimal comma "," → ".";
leading lab operators like "<0.4"/">75" are stripped for the comparison).
Date filters (`date_from`/`date_to`) compare ISO date strings lexicographically.
Deeply irregular JSON may still want a one-time flatten-to-CSV first; flat
tables are the sweet spot.
"""

from __future__ import annotations

import csv
import hashlib
import io
import itertools
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import access, reserved_paths
from .query_compat import (
    DATE_LIKE as _DATE_LIKE, OPS as _OPS, QueryDataError,
    coerce_num as _coerce_num, get_field as _get_field, match as _match, sort_key,
    distinct_key, group_key, latest_aggregate, numeric_aggregate, profile_kind,
)
from .vault import VaultPathError, resolve_under_vault

log = logging.getLogger(__name__)

ALLOWED_SUFFIXES = (".csv", ".tsv", ".json")
MAX_FILE_BYTES = 25 * 1024 * 1024  # 25 MB guard — KB datasets are small
HARD_ROW_CAP = 1000
DEFAULT_LIMIT = 100
PROFILE_MAX_DISTINCT = 20  # categorical/text columns expose up to this many distinct values
MAX_RESPONSE_BYTES = 64 * 1024
MAX_PARSED_ROWS = 10_000
_COMMON_RECORD_KEYS = ("result", "results", "data", "rows", "items", "entries")


@dataclass
class QueryDataResult:
    """Bounded answer with optional evidence from the exact parsed byte snapshot.

    Row indices name zero-based data records (CSV excludes its header); index
    zero for json_object names the root object, not an array. Aggregates carry
    the source descriptor only. Adapter rows without raw bytes omit provenance.
    """

    path: str
    format: str
    total_rows: int  # rows in the dataset
    total_matched: int  # rows matching the filters (before limit/offset)
    returned: int
    columns: list[str]
    rows: list[dict]
    aggregate: Any = None
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)
    provenance: dict[str, Any] | None = None

    def as_dict(self) -> dict:
        result = {
            "path": self.path,
            "format": self.format,
            "total_rows": self.total_rows,
            "total_matched": self.total_matched,
            "returned": self.returned,
            "columns": self.columns,
            "rows": self.rows,
            "aggregate": self.aggregate,
            "truncated": self.truncated,
            "warnings": self.warnings,
        }
        if self.provenance is not None:
            result["provenance"] = self.provenance
        return result


def _bounded_limit(value: int | None) -> int:
    """Keep every row response within the documented positive hard cap."""
    if value is None:
        return DEFAULT_LIMIT
    try:
        limit = int(value)
    except (TypeError, ValueError) as error:
        raise QueryDataError("BAD_LIMIT", "limit must be an integer") from error
    return min(limit, HARD_ROW_CAP) if limit > 0 else DEFAULT_LIMIT


def _bounded_response_rows(
    rows: list[dict], *, overhead: int = 0, row_indices: list[int] | None = None,
) -> tuple[list[dict], bool]:
    """Keep serialized query data within a modest, explicit response budget."""
    size = 2 + overhead  # JSON list brackets plus optional provenance
    bounded: list[dict] = []
    for index, row in enumerate(rows):
        row_size = len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        separator = 1 if bounded else 0
        if row_indices is not None:
            row_size += len(str(row_indices[index])) + separator
        if size + separator + row_size > MAX_RESPONSE_BYTES:
            return bounded, True
        bounded.append(row)
        size += separator + row_size
    return bounded, False


def _bounded_aggregate(value: Any, *, overhead: int = 0) -> tuple[Any, bool]:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) + overhead <= MAX_RESPONSE_BYTES:
        return value, False
    return {"truncated": True, "reason": "aggregate exceeds response size cap"}, True


def _locate_array(
    data: Any, record_path: str | None, warnings: list[str],
) -> tuple[list, str, str | None]:
    if record_path:
        located = _get_field(data, record_path) if isinstance(data, (dict, list)) else None
        if not isinstance(located, list):
            raise QueryDataError(
                "BAD_RECORD_PATH",
                f"record_path {record_path!r} did not resolve to a JSON array",
            )
        return located, "json_array", record_path
    if isinstance(data, list):
        return data, "json_array", None
    if isinstance(data, dict):
        for k in _COMMON_RECORD_KEYS:
            if isinstance(data.get(k), list):
                warnings.append(f"auto-detected record array at top-level key {k!r}")
                return data[k], "json_array", k
        warnings.append("JSON root is an object with no obvious array; treated as a single row")
        return [data], "json_object", None
    raise QueryDataError("BAD_JSON", "JSON root is neither an array nor an object")


def _infer_columns(rows: list[dict]) -> list[str]:
    cols: list[str] = []
    seen: set[str] = set()
    for r in rows[:500]:
        if isinstance(r, dict):
            for k in r:
                if k not in seen:
                    seen.add(k)
                    cols.append(k)
    return cols


def load_rows(
    abs_path: Path, record_path: str | None = None
) -> tuple[str, list[dict], list[str], list[str]]:
    suffix = abs_path.suffix.lower()
    data = read_dataset_bytes(abs_path)
    return load_rows_bytes(data, suffix, record_path)


def load_generic_rows(
    vault_root: Path,
    relative_path: str,
    suffix: str,
    record_path: str | None = None,
    *,
    provenance: dict[str, Any] | None = None,
) -> tuple[str, list[dict], list[str], list[str]]:
    """Read a public dataset through the retained generic leaf before parsing."""

    try:
        snapshot = reserved_paths.read_generic_bytes(vault_root, relative_path)
    except reserved_paths.ReservedPathLeafError as error:
        if error.code in {
            "CAPABILITY_UNAVAILABLE",
            "IDENTITY_CHANGED",
            "MISSING",
            "RESERVED_PATH",
            "UNSAFE_PATH",
        }:
            raise QueryDataError("NOT_FOUND", "dataset could not be read") from None
        raise QueryDataError("UNREADABLE", "dataset could not be read safely") from None
    return load_rows_bytes(snapshot.data, suffix, record_path, provenance=provenance)


def read_dataset_bytes(abs_path: Path) -> bytes:
    """Read at most the dataset cap plus one byte to avoid unbounded allocation."""
    try:
        if abs_path.stat().st_size > MAX_FILE_BYTES:
            raise QueryDataError(
                "TOO_LARGE",
                f"dataset exceeds the {MAX_FILE_BYTES} byte limit; pre-split or filter upstream",
            )
        with abs_path.open("rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError as error:
        raise QueryDataError("NOT_FOUND", "dataset could not be read") from error
    if len(data) > MAX_FILE_BYTES:
        raise QueryDataError("TOO_LARGE", "dataset exceeds the byte limit")
    return data


def load_rows_bytes(
    data: bytes, suffix: str, record_path: str | None = None,
    *, provenance: dict[str, Any] | None = None,
) -> tuple[str, list[dict], list[str], list[str]]:
    """Parse one already-read canonical dataset snapshot without a second file read."""
    suffix = suffix.lower()
    if len(data) > MAX_FILE_BYTES:
        raise QueryDataError("TOO_LARGE", "dataset exceeds the byte limit")
    warnings: list[str] = []
    if suffix in (".csv", ".tsv"):
        delimiter = "\t" if suffix == ".tsv" else ","
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise QueryDataError("INVALID_DATASET_ENCODING", "dataset is not UTF-8") from error
        with io.StringIO(text, newline="") as f:
            reader = csv.DictReader(f, delimiter=delimiter)
            rows = [dict(r) for r in itertools.islice(reader, MAX_PARSED_ROWS + 1)]
            cols = list(reader.fieldnames or [])
        if len(rows) > MAX_PARSED_ROWS:
            raise QueryDataError("TOO_MANY_ROWS", "dataset exceeds the parsed row limit")
        fmt = "tsv" if suffix == ".tsv" else "csv"
        record_kind, resolved_path = "csv_records", None
    elif suffix == ".json":
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise QueryDataError("BAD_JSON", f"could not parse JSON: {e}") from None
        arr, record_kind, resolved_path = _locate_array(payload, record_path, warnings)
        if len(arr) > MAX_PARSED_ROWS:
            raise QueryDataError("TOO_MANY_ROWS", "dataset exceeds the parsed row limit")
        rows = [r if isinstance(r, dict) else {"value": r} for r in arr]
        fmt, cols = "json", _infer_columns(rows)
    else:
        raise QueryDataError(
            "UNSUPPORTED_FORMAT", f"only {list(ALLOWED_SUFFIXES)} supported, got {suffix!r}"
        )
    if provenance is not None:
        provenance.update(
            sha256=hashlib.sha256(data).hexdigest(), size=len(data),
            record_kind=record_kind, record_path=resolved_path,
        )
    return fmt, rows, cols, warnings


def _aggregate(matched: list[dict], spec: str, date_col: str | None) -> dict:
    spec = spec.strip()
    if spec == "count":
        return {"count": len(matched)}
    if ":" not in spec:
        raise QueryDataError(
            "BAD_AGGREGATE",
            "aggregate must be 'count' or 'func:column' (func in min,max,sum,avg,latest,distinct)",
        )
    func, col = (p.strip() for p in spec.split(":", 1))
    if func == "distinct":
        out: list[Any] = []
        seen: set[str] = set()
        total = 0
        for r in matched:
            v = _get_field(r, col)
            key = distinct_key(v)
            if key not in seen:
                seen.add(key)
                total += 1
                if len(out) < PROFILE_MAX_DISTINCT:
                    out.append(v)
        return {"distinct": out, "n": total, "truncated": total > len(out)}
    if func == "group":
        counts: dict[str, tuple[Any, int]] = {}
        for row in matched:
            value = _get_field(row, col)
            key = group_key(value)
            prior = counts.get(key)
            counts[key] = (value, 1 if prior is None else prior[1] + 1)
        ordered = sorted(counts.values(), key=lambda pair: json.dumps(pair[0], ensure_ascii=False))
        groups = [
            {"value": value, "count": count} for value, count in ordered[:PROFILE_MAX_DISTINCT]
        ]
        return {"groups": groups, "n": len(counts), "truncated": len(counts) > len(groups)}
    if func == "latest":
        return latest_aggregate(matched, date_col or col)
    if func in ("min", "max", "sum", "avg"):
        return numeric_aggregate(matched, func, col)
    raise QueryDataError("BAD_AGGREGATE", f"unknown aggregate func {func!r}")


# ---------------- profile (the dataset-card "what it holds" engine) ----------------


@dataclass
class ColumnProfile:
    """Deterministic per-column summary — the searchable signal of a dataset.

    Pure "measure": numeric → range/sum/avg; date → earliest/latest; otherwise a
    capped list of distinct values (vendors, item names…). No LLM — Claude writes
    the prose "what this holds" line; this only stats the raw rows.
    """

    name: str
    kind: str  # numeric | date | categorical | text
    non_null: int
    distinct: int
    min: float | None = None
    max: float | None = None
    sum: float | None = None
    avg: float | None = None
    earliest: str | None = None
    latest: str | None = None
    top_values: list[Any] | None = None
    distinct_truncated: bool = False

    def as_dict(self) -> dict:
        d: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "non_null": self.non_null,
            "distinct": self.distinct,
        }
        for k in ("min", "max", "sum", "avg", "earliest", "latest", "top_values"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        if self.distinct_truncated:
            d["distinct_truncated"] = True
        return d


def _profile_column(
    name: str, values: list[Any], *, max_distinct: int = PROFILE_MAX_DISTINCT
) -> ColumnProfile:
    non_null = [v for v in values if v not in (None, "")]
    n = len(non_null)
    seen: list[Any] = []
    seen_keys: set[str] = set()
    for v in non_null:
        k = str(v)
        if k not in seen_keys:
            seen_keys.add(k)
            if len(seen) < max_distinct:
                seen.append(v)
    distinct = len(seen_keys)
    distinct_truncated = distinct > len(seen)

    nums = [x for v in non_null if (x := _coerce_num(v)) is not None]
    date_like = sum(1 for v in non_null if _DATE_LIKE.search(str(v)))
    kind = profile_kind(name, n, len(nums), date_like, distinct, max_distinct)

    # Date when the name says so or values look date-shaped — but never when the
    # column is predominantly plain numbers (guards a numeric col named "...date").
    if kind == "date":
        svals = [str(v) for v in non_null]
        return ColumnProfile(
            name,
            "date",
            n,
            distinct,
            earliest=min(svals),
            latest=max(svals),
            distinct_truncated=distinct_truncated,
        )
    if kind == "numeric":
        return ColumnProfile(
            name,
            "numeric",
            n,
            distinct,
            min=min(nums),
            max=max(nums),
            sum=sum(nums),
            avg=sum(nums) / len(nums),
            distinct_truncated=distinct_truncated,
        )
    return ColumnProfile(
        name,
        kind,
        n,
        distinct,
        top_values=seen,
        distinct_truncated=distinct_truncated,
    )


def profile_data(
    vault_root: Path,
    *,
    path: str,
    record_path: str | None = None,
    max_distinct: int = PROFILE_MAX_DISTINCT,
) -> dict:
    """Deterministic content profile of a CSV/JSON file — feeds a dataset card.

    Returns `{path, format, total_rows, columns: [ColumnProfile.as_dict()...],
    warnings}`. The half of the tabular-search pattern that captures *what a
    dataset holds* without embedding raw rows: a markdown dataset card renders
    this (vendors, item names, totals, date ranges) and is embedded; the rows
    stay queryable only via `query_data`.
    """
    try:
        abs_path, rel = resolve_under_vault(vault_root, path, must_exist=True, must_be_file=True)
    except VaultPathError as e:
        raise QueryDataError(e.code, e.reason) from None
    if abs_path.suffix.lower() not in ALLOWED_SUFFIXES:
        raise QueryDataError("UNSUPPORTED_FORMAT", f"only {list(ALLOWED_SUFFIXES)} supported")
    fmt, rows, cols, warnings = load_generic_rows(
        vault_root, rel, abs_path.suffix, record_path
    )
    profiles = [
        _profile_column(c, [_get_field(r, c) for r in rows], max_distinct=max_distinct).as_dict()
        for c in cols
    ]
    return {
        "path": rel,
        "format": fmt,
        "total_rows": len(rows),
        "columns": profiles,
        "warnings": warnings,
    }


def _render_column_line(c: dict) -> str:
    name, kind = c["name"], c["kind"]
    if kind == "numeric":
        return (
            f"- **{name}** (numeric): min {c.get('min')}, max {c.get('max')}, "
            f"sum {c.get('sum')}, avg {round(c['avg'], 2) if c.get('avg') is not None else None}"
        )
    if kind == "date":
        return f"- **{name}** (date): {c.get('earliest')} → {c.get('latest')}"
    vals = ", ".join(str(v) for v in (c.get("top_values") or []))
    label = "categorical" if kind == "categorical" else "text"
    suffix = "" if kind == "categorical" else " (sample)"
    return f"- **{name}** ({label}, {c['distinct']} distinct){suffix}: {vals}"


def build_dataset_card(profile: dict, *, title: str | None = None) -> str:
    """Render a `profile_data` result into a markdown dataset card.

    The card is the embedded, `find`-able surface for a data file: a `dataset`
    page whose body carries the salient content (vendors, items, ranges) plus a
    prose placeholder for Claude's "what this holds" summary and a `data_file:`
    pointer the reader follows into `query_data`. Raw rows are never embedded.
    """
    data_file = profile["path"]
    title = title or Path(data_file).stem
    cols = profile["columns"]
    lines = [
        "---",
        "type: dataset",
        f"title: {title}",
        f"data_file: {data_file}",
        f"format: {profile['format']}",
        f"rows: {profile['total_rows']}",
        "---",
        "",
        f"# {title}",
        "",
        "## What this holds",
        "",
        "<!-- TODO: one-line prose summary of what this dataset contains (Claude fills this in) -->",
        "",
        "## Profile",
        "",
        f"_Auto-generated from {data_file} ({profile['total_rows']} rows) — regenerate when the data changes._",
        "",
        "Columns: " + ", ".join(c["name"] for c in cols),
        "",
    ]
    lines.extend(_render_column_line(c) for c in cols)
    return "\n".join(lines) + "\n"


def _profile_payload(rows: list[dict], cols: list[str], fmt: str, rel: str) -> dict:
    """Profile already-loaded rows and render a card — the `aggregate="profile"` result."""
    profile = {
        "path": rel,
        "format": fmt,
        "total_rows": len(rows),
        "columns": [_profile_column(c, [_get_field(r, c) for r in rows]).as_dict() for c in cols],
    }
    return {"profile": profile, "dataset_card": build_dataset_card(profile)}


def query_data(
    vault_root: Path,
    *,
    path: str,
    record_path: str | None = None,
    filters: list[dict] | None = None,
    columns: list[str] | None = None,
    sort_by: str | None = None,
    descending: bool = False,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    aggregate: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    date_column: str | None = None,
    authorize_path: Callable[[str], bool] | None = None,
) -> QueryDataResult:
    """Query a CSV/JSON data file under the vault. See module docstring."""
    try:
        abs_path, rel = resolve_under_vault(vault_root, path, must_exist=True, must_be_file=True)
    except VaultPathError as e:
        raise QueryDataError(e.code, e.reason) from None
    # `excluded` paths (_access.yaml) refuse identically to a missing path —
    # same code/shape/text as resolve_under_vault's own NOT_FOUND, never a
    # distinct error, so this is not an existence oracle.
    if access.refuse_if_excluded(vault_root, rel):
        raise QueryDataError("NOT_FOUND", f"path does not exist: {rel}")
    if abs_path.suffix.lower() not in ALLOWED_SUFFIXES:
        raise QueryDataError("UNSUPPORTED_FORMAT", f"only {list(ALLOWED_SUFFIXES)} supported")
    if authorize_path is not None and not authorize_path(rel):
        raise QueryDataError("NOT_FOUND", f"path does not exist: {rel}")

    provenance: dict[str, Any] = {}
    fmt, rows, cols, warnings = load_generic_rows(
        vault_root, rel, abs_path.suffix, record_path, provenance=provenance
    )
    return evaluate_rows(
        rows,
        path=rel,
        format=fmt,
        columns_available=cols,
        warnings=warnings,
        filters=filters,
        columns=columns,
        sort_by=sort_by,
        descending=descending,
        limit=limit,
        offset=offset,
        aggregate=aggregate,
        date_from=date_from,
        date_to=date_to,
        date_column=date_column,
        provenance=provenance,
    )


def evaluate_rows(
    rows: list[dict],
    *,
    path: str,
    format: str,
    columns_available: list[str] | None = None,
    warnings: list[str] | None = None,
    filters: list[dict] | None = None,
    columns: list[str] | None = None,
    sort_by: str | None = None,
    descending: bool = False,
    limit: int | None = DEFAULT_LIMIT,
    offset: int = 0,
    aggregate: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    date_column: str | None = None,
    provenance: dict[str, Any] | None = None,
) -> QueryDataResult:
    """Apply the dataset query contract to already-loaded adapter rows."""
    cols = list(columns_available or _infer_columns(rows))
    warnings = list(warnings or ())
    total_rows = len(rows)

    flt: list[dict] = []
    for f0 in filters or []:
        if not isinstance(f0, dict) or not f0.get("column"):
            raise QueryDataError("BAD_FILTER", f"each filter needs a 'column': {f0!r}")
        if f0.get("op", "eq") not in _OPS:
            raise QueryDataError("BAD_OP", f"unknown op {f0.get('op')!r}; allowed: {sorted(_OPS)}")
        flt.append(f0)

    date_col = date_column or ("date" if "date" in cols else None)
    if (date_from or date_to) and not date_col:
        warnings.append("date_from/date_to ignored: no date column found (pass date_column=)")
    if date_col and date_from:
        flt.append({"column": date_col, "op": "gte", "value": date_from})
    if date_col and date_to:
        flt.append({"column": date_col, "op": "lte", "value": date_to})

    matched = [(i, r) for i, r in enumerate(rows) if all(_match(r, f) for f in flt)]
    total_matched = len(matched)
    provenance = dict(provenance) if provenance else None
    if provenance is not None and not aggregate:
        provenance["row_indices"] = []
    overhead = (
        len(json.dumps({"provenance": provenance, "aggregate" if aggregate else "rows": []}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) - 2
        if provenance is not None else 0
    )
    provenance_truncated = overhead + 2 > MAX_RESPONSE_BYTES
    if provenance_truncated:
        provenance, overhead = None, 0
        warnings.append("provenance unavailable: source descriptor exceeds response size cap")

    if aggregate:
        matched_rows = [r for _, r in matched]
        if aggregate.strip() == "profile":
            agg: Any = _profile_payload(matched_rows, cols, format, path)
        else:
            agg = _aggregate(matched_rows, aggregate, date_col)
        bounded_agg, aggregate_truncated = _bounded_aggregate(agg, overhead=overhead)
        if overhead + len(json.dumps(bounded_agg, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_RESPONSE_BYTES:
            provenance, provenance_truncated = None, True
            warnings.append("provenance unavailable: source descriptor exceeds response size cap")
            bounded_agg, aggregate_truncated = _bounded_aggregate(agg)
        if aggregate_truncated:
            warnings.append("response size cap truncated aggregate")
        return QueryDataResult(
            path=path,
            format=format,
            total_rows=total_rows,
            total_matched=total_matched,
            returned=0,
            columns=cols,
            rows=[],
            aggregate=bounded_agg,
            truncated=aggregate_truncated or provenance_truncated,
            warnings=warnings,
            provenance=provenance,
        )

    if sort_by:
        matched.sort(key=lambda pair: sort_key(_get_field(pair[1], sort_by)), reverse=descending)

    limit = _bounded_limit(limit)
    offset = max(0, int(offset))
    window = matched[offset : offset + limit]
    truncated = (offset + len(window)) < total_matched

    if columns:
        out_rows = [{c: _get_field(r, c) for c in columns} for _, r in window]
        out_cols = list(columns)
    else:
        out_rows = [r for _, r in window]
        out_cols = cols
    row_indices = [i for i, _ in window] if provenance is not None else None
    out_rows, response_truncated = _bounded_response_rows(
        out_rows, overhead=overhead, row_indices=row_indices,
    )
    if provenance is not None:
        provenance["row_indices"] = row_indices[:len(out_rows)]
    if response_truncated:
        warnings.append("response size cap truncated returned rows")
    truncated = truncated or response_truncated or provenance_truncated

    return QueryDataResult(
        path=path,
        format=format,
        total_rows=total_rows,
        total_matched=total_matched,
        returned=len(out_rows),
        columns=out_cols,
        rows=out_rows,
        aggregate=None,
        truncated=truncated,
        warnings=warnings,
        provenance=provenance,
    )
