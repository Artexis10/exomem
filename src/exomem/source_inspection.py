"""Bounded, local inspection of privately staged evidence bytes.

These projections describe content; they neither classify its policy nor
evaluate document syntax. Dataset cards copy no values; `render_values` is the one
bounded rendering of a small dataset's values, for readers who may read its rows.
"""

from __future__ import annotations

import codecs
import csv
import html
import io
import itertools
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from . import query_data

DATASET_PREVIEW_BYTES = 1024 * 1024
TEXT_PREVIEW_BYTES = 64 * 1024
MAX_FIELDS = 64
MAX_FIELD_CHARS = 256
MAX_JSON_DEPTH = 64
MAX_TABLE_CELLS = 100_000
#: Largest original whose values are searchable, and the largest rendering of them.
VALUE_DATASET_BYTES = TEXT_PREVIEW_BYTES
VALUE_RENDERING_BYTES = 64 * 1024
#: A longer value is cut to this many characters and ends in an ellipsis.
VALUE_MAX_CHARS = 256
#: Card inspection statuses whose original is searchable: it parsed within the shape budgets.
#: A closed set that `_inspect_dataset` itself emits, not a vocabulary that a vault defines.
VALUE_STATUSES = frozenset({"ready", "limited"})
VALUE_TRUNCATION_LINE = (
    f"[Value rendering truncated at {VALUE_RENDERING_BYTES // 1024} KiB; query_dataset reads every row.]"
)
LITERAL_SUFFIXES = frozenset({".yaml", ".yml", ".jsonl", ".ndjson", ".xml", ".toml"})


@dataclass(frozen=True)
class Inspection:
    status: str
    body: str
    dataset_format: str | None = None
    rows: int | None = None


def is_dataset_name(filename: str) -> bool:
    """Whether preserve files `filename` as a dataset, which stays on the volume with the notes (design D1)."""
    return Path(filename).suffix.lower() in query_data.ALLOWED_SUFFIXES


def inspect_source(stream: BinaryIO, *, filename: str, size: int) -> Inspection | None:
    """Inspect a bounded prefix and restore the private stage's seek position."""
    suffix = Path(filename).suffix.lower()
    dataset = is_dataset_name(filename)
    if not dataset and suffix not in LITERAL_SUFFIXES:
        return None
    position = stream.tell()
    try:
        stream.seek(0)
        if dataset:
            if size > DATASET_PREVIEW_BYTES:
                return _dataset_unavailable(suffix, "byte_limit")
            data = stream.read(DATASET_PREVIEW_BYTES + 1)
            if len(data) > DATASET_PREVIEW_BYTES:
                return _dataset_unavailable(suffix, "byte_limit")
            return _inspect_dataset(data, suffix)
        return _inspect_literal(stream.read(TEXT_PREVIEW_BYTES + 3))
    finally:
        stream.seek(position)


def _dataset_unavailable(suffix: str, status: str) -> Inspection:
    return Inspection(
        status,
        "Automatic schema inspection unavailable: " + status + ". "
        "The original is preserved. Exact queries have separate format and size limits.",
        dataset_format=suffix[1:],
    )


def _literal_field(value: str) -> str:
    # JSON quoting prevents newlines; escaping Markdown/HTML prevents field
    # names from creating links, frontmatter, headings or rendered markup.
    encoded = html.escape(json.dumps(value, ensure_ascii=False), quote=False)
    for char in "`[]\\":
        encoded = encoded.replace(char, f"&#{ord(char)};")
    return f"`{encoded}`"


def _parse_dataset(data: bytes, suffix: str) -> tuple[str, list[dict], list[str]] | str:
    """`(format, rows, columns)` within the shape budgets, or the failure status as a string."""
    try:
        data.decode("utf-8")  # distinguish encoding from format, including JSON
    except UnicodeDecodeError:
        return "invalid_encoding"
    if suffix == ".json" and _json_too_deep(data):
        return "invalid_data"
    try:
        if suffix in {".csv", ".tsv"} and _table_shape_exceeds_budget(data, suffix):
            return "shape_limit"
        fmt, rows, columns, _warnings = query_data.load_rows_bytes(data, suffix)
    except query_data.QueryDataError as error:
        return "row_limit" if error.code == "TOO_MANY_ROWS" else "invalid_data"
    except (csv.Error, RecursionError, ValueError):
        return "invalid_data"
    return fmt, rows, columns


def _inspect_dataset(data: bytes, suffix: str) -> Inspection:
    parsed = _parse_dataset(data, suffix)
    if isinstance(parsed, str):
        return _dataset_unavailable(suffix, parsed)
    fmt, rows, columns = parsed

    if fmt in {"csv", "tsv"}:
        return Inspection(
            "ready",
            f"Parsed records: {len(rows)}.\n\nObserved columns: {len(columns)}.\n\n"
            "The first record is assumed to be a header by the tabular query parser. "
            "Header labels are omitted here because a headerless first record can contain data. "
            "Use query_dataset with data_file for authorized fields, rows and aggregates.",
            fmt,
            len(rows),
        )

    # JSON column inference observes the first 500 records. Describe that
    # sampling explicitly rather than claiming a complete schema.
    sampled = fmt == "json" and len(rows) > 500
    limited = sampled or len(columns) > MAX_FIELDS
    fields = []
    for column in columns[:MAX_FIELDS]:
        if len(column) > MAX_FIELD_CHARS:
            limited = True
        fields.append(_literal_field(column[:MAX_FIELD_CHARS]))
    body = [f"Parsed records: {len(rows)}.", "", "Observed fields:"]
    body.extend(f"- {field}" for field in fields)
    if not fields:
        body.append("No fields observed.")
    if limited:
        body.extend(["", "Field listing is limited or sampled; use an exact query to inspect records."])
    body.extend(["", "Use query_dataset with data_file for exact rows, filters and aggregates. "
                 "Row values are not copied into this card."])
    return Inspection("limited" if limited else "ready", "\n".join(body), fmt, len(rows))


@dataclass(frozen=True)
class ValueRendering:
    """A small dataset's values as one block of `field.path: value` lines per record."""

    records: tuple[str, ...]
    #: True when the rendering stopped at `VALUE_RENDERING_BYTES`, so later records are absent.
    truncated: bool


def render_values(data: bytes, suffix: str) -> ValueRendering | None:
    """The bounded value rendering of a dataset original, or None when it has none to give.

    Reuses the parser `query_dataset` reads rows with, so a CSV or TSV keeps that parser's
    first-record-header assumption. JSON nesting becomes dotted paths with list indices kept.
    None means the bytes are over the size bound or do not parse within the shape budgets.
    """
    if len(data) > VALUE_DATASET_BYTES:
        return None
    parsed = _parse_dataset(data, suffix.lower())
    if isinstance(parsed, str):
        return None
    records: list[str] = []
    used = 0
    for row in parsed[1]:
        lines: list[str] = []
        for path, value in _scalars(row, ""):
            line = f"{path}: {_value_text(value)}"
            used += len(line.encode("utf-8")) + 1
            if used > VALUE_RENDERING_BYTES:
                if lines:
                    records.append("\n".join(lines))
                return ValueRendering(tuple(records), True)
            lines.append(line)
        if lines:
            records.append("\n".join(lines))
    return ValueRendering(tuple(records), False)


def _scalars(value: object, path: str) -> Iterator[tuple[str, object]]:
    """Each non-null scalar under `value` with its dotted path; list members use their index."""
    if isinstance(value, dict):
        for key, member in value.items():
            yield from _scalars(member, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, member in enumerate(value):
            yield from _scalars(member, f"{path}.{index}" if path else str(index))
    elif value is not None and _value_text(value):
        yield path, value


def _value_text(value: object) -> str:
    text = json.dumps(value) if isinstance(value, bool) else " ".join(str(value).split())
    return text if len(text) <= VALUE_MAX_CHARS else text[:VALUE_MAX_CHARS] + "\u2026"


def _table_shape_exceeds_budget(data: bytes, suffix: str) -> bool:
    """Bound DictReader's missing-field padding before allocating its rows."""
    with io.StringIO(data.decode("utf-8"), newline="") as stream:
        reader = csv.reader(stream, delimiter="\t" if suffix == ".tsv" else ",")
        header = next(reader, [])
        cells = len(header)
        if cells > MAX_TABLE_CELLS:
            return True
        nonempty_rows = (row for row in reader if row)
        for row in itertools.islice(nonempty_rows, query_data.MAX_PARSED_ROWS + 1):
            # DictReader skips empty rows and pads short records to header width.
            cells += max(len(header), len(row))
            if cells > MAX_TABLE_CELLS:
                return True
    return False


def _json_too_deep(data: bytes) -> bool:
    """Bound nesting before decoding; braces inside strings are ordinary data."""
    depth = 0
    quoted = escaped = False
    for char in data:
        if escaped:
            escaped = False
        elif quoted and char == 92:
            escaped = True
        elif char == 34:
            quoted = not quoted
        elif not quoted:
            if char in (91, 123):
                depth += 1
                if depth > MAX_JSON_DEPTH:
                    return True
            elif char in (93, 125):
                depth -= 1
    return False


def _inspect_literal(data: bytes) -> Inspection:
    truncated = len(data) > TEXT_PREVIEW_BYTES
    try:
        # A non-final incremental decode retains a character split at the
        # prefix boundary, but still rejects malformed bytes within it.
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        text = decoder.decode(data[:TEXT_PREVIEW_BYTES], final=not truncated)
        pending = decoder.getstate()[0]
        if pending:
            width = 2 if pending[0] < 0xE0 else 3 if pending[0] < 0xF0 else 4
            remaining = width - len(pending)
            # Validate only the character that starts within the prefix. Its
            # completed text stays omitted so output never exceeds the cap.
            decoder.decode(data[TEXT_PREVIEW_BYTES:TEXT_PREVIEW_BYTES + remaining], final=True)
    except UnicodeDecodeError:
        return Inspection("invalid_encoding", "Text preview unavailable: invalid UTF-8. Original preserved.")
    # A fence longer than any run in the input is literal to Markdown and to
    # the substrate's graph/link scanner. Indentation alone does not mask
    # wikilinks there; uploaded syntax must never manufacture graph edges.
    fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", text)), default=0))
    body = f"{fence}text\n{text}\n{fence}"
    if truncated:
        body += "\n\n[Preview truncated at 64 KiB; the full content remains in the original.]"
    return Inspection("truncated" if truncated else "ready", body)
