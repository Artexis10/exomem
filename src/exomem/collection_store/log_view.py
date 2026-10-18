"""The log-layout view of a store collection: its stored frame and its one renderer (design §5).

A log view is the frame, the declared item section's blocks in insertion order, and the
frame's suffix. The frame is what the log file holds outside its blocks: the text before the
first block (the UTF-8 BOM, the frontmatter, the headings and the section heading), the text
from the end of the item section, and the newline style of the blocks. The importer captures
it from the legacy file, a create stores the empty one, and the store re-emits it byte for
byte around the rendered blocks. The asynchronous projector and the reverse export both
render through ``render``, so the projection rule lives here once.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

from .. import memory_refs, record_formats, records
from .. import structured_collections as collections
from . import tokens, typed_storage
from .connection import CollectionStoreError

#: The version of the stored frame shape, ``collections.log_frame_json``.
FRAME_VERSION = 1
# nosemgrep: ep-word-set -- the version-1 frame shape fixes these keys.
_FRAME_KEYS = frozenset({"version", "text", "suffix", "bom", "newline", "final_newline"})


@dataclass(frozen=True, slots=True)
class LogFrame:
    """A log view's text outside its blocks. ``bom`` and ``final_newline`` describe ``text`` and ``suffix``."""

    text: str
    suffix: str
    bom: bool
    newline: str
    final_newline: bool

    def as_json(self) -> str:
        return records._canonical_json({
            "version": FRAME_VERSION, "text": self.text, "suffix": self.suffix, "bom": self.bom,
            "newline": self.newline, "final_newline": self.final_newline,
        }).decode("utf-8")


def bounds(manifest, data: bytes, parsed: Sequence) -> tuple[int, int]:
    """Where a log file's blocks start and its item section ends, from the adapter's records of ``data``."""
    _, end = section_bounds(manifest, data)
    return (parsed[0].span.start if parsed else end), end


def capture(manifest, data: bytes, parsed: Sequence) -> LogFrame:
    """The frame of a legacy log file, exactly as its bytes hold it."""
    data.decode("utf-8")  # Frame persistence must not silently replace invalid bytes.
    start, end = bounds(manifest, data, parsed)
    # The first line ending names the file's newline style; Markdown fixes the three sequences.
    newline = re.search(rb"\r\n|\n|\r", data)
    return LogFrame(data[:start].decode("utf-8"), data[end:].decode("utf-8"), data.startswith(b"\xef\xbb\xbf"),
                    newline.group().decode() if newline else "\n", data.endswith((b"\n", b"\r")))


def empty(manifest) -> LogFrame:
    """The frame a create stores: the declared section heading alone, with LF newlines."""
    section = manifest.storage.descriptor["section"]
    return LogFrame(f"{'#' * section['level']} {section['title']}\n", "", False, "\n", True)


def frame_hash(frame: LogFrame) -> str:
    """The complete canonical frame, independent of the log's row generation."""
    return hashlib.sha256(frame.as_json().encode()).hexdigest()


def section_bounds(manifest, data: bytes) -> tuple[record_formats._Heading, int]:
    section = manifest.storage.descriptor["section"]
    headings = record_formats._headings_outside_fences(data)
    heading = next((item for item in headings
                    if item.level == section["level"] and item.title == section["title"]), None)
    if heading is None:
        raise collections.CollectionError("INVALID_RECORD_SOURCE", "the declared log section is missing")
    end = next((item.start for item in headings if item.start > heading.start and item.level <= heading.level),
               len(data))
    return heading, end


def decode_block(root, manifest, raw: bytes):
    """Exactly one declared block, with no frame or stray prose to lose during resume."""
    prefix = empty(manifest).text.encode()
    parsed = record_formats.MarkdownLogAdapter(root, manifest).read_bytes(prefix + raw)
    if (parsed.diagnostics or len(parsed.records) != 1 or parsed.records[0].span.start != len(prefix)
            or parsed.records[0].span.end != len(prefix) + len(raw) or stray_lines(raw, parsed.records[0])):
        raise records._held_not_found()
    return parsed.records[0]


def split_frame_stamp(manifest, data: bytes) -> tuple[bytes, dict | None]:
    """Remove our one complete managed line before the declared section ends, never authored text."""
    # Apply the adapter's existing byte bound before scanning managed metadata.
    if len(data) > record_formats._MAX_LOG_BYTES:
        raise collections.CollectionError("RECORD_SOURCE_TOO_LARGE", "markdown log exceeds the byte limit")
    _, end = section_bounds(manifest, data)
    offset, found, fence = 0, [], None
    for line in data.splitlines(keepends=True):
        if fence is not None:
            if record_formats._closes_fence(line, fence):
                fence = None
        elif (opened := record_formats._opens_fence(line)) is not None:
            fence = opened
        elif line.strip().startswith(b"<!-- exomem-frame "):
            found.append((offset, line))
        offset += len(line)
    if not found:
        return data, None
    try:
        [(offset, line)] = found
        text = line.strip()
        if offset >= end or not text.endswith(b" -->"):
            raise ValueError
        stamp = json.loads(text[len(b"<!-- exomem-frame "):-len(b" -->")])
        # The managed frame protocol fixes these keys; the digest binds the whole frame.
        if (not isinstance(stamp, dict) or set(stamp) != {"s", "i", "v", "h"}
                or any(memory_refs.normalize_id(stamp.get(key)) != stamp.get(key) for key in ("s", "i"))
                or any(not isinstance(stamp.get(key), str) for key in ("s", "i"))
                or type(stamp["v"]) is not int or stamp["v"] < 1):
            raise ValueError
        tokens._hex64(stamp["h"], "frame hash")
    except (ValueError, TypeError, KeyError):
        raise collections.CollectionError("VIEW_FRAME_STAMP_INVALID", "the log frame stamp is malformed or duplicated") from None
    return data[:offset] + data[offset + len(line):], stamp


def load(frame_json: str | None, manifest) -> LogFrame:
    """The stored frame of a log collection.

    None is a collection created without its scaffold, which renders the empty frame. A frame
    without ``version`` is the text-only shape that creates stored before the frame was
    versioned; it reads as that text with LF newlines and no suffix.
    """
    if frame_json is None:
        return empty(manifest)
    frame = json.loads(frame_json)
    if not isinstance(frame, Mapping):
        raise CollectionStoreError("COLLECTION_LOG_FRAME_INVALID", "the stored log frame is not an object")
    if "version" not in frame and set(frame) == {"text"}:
        return LogFrame(frame["text"], "", frame["text"].startswith("﻿"), "\n", True)
    if frame.get("version") != FRAME_VERSION or set(frame) != _FRAME_KEYS:
        raise CollectionStoreError("COLLECTION_LOG_FRAME_INVALID", "the stored log frame has an unknown shape")
    return LogFrame(frame["text"], frame["suffix"], frame["bom"], frame["newline"], frame["final_newline"])


def rows(conn, manifest) -> list[dict]:
    """Every row of a log collection in the log's insertion order, with its values decoded."""
    order = "DESC" if manifest.storage.descriptor.get("insertion") == "newest-first" else "ASC"
    cursor = conn.execute(
        "SELECT row_id,item_key,values_json,row_version,payload_hash,collection_id,encoding,created_txn,view_path "
        "FROM items "
        f"WHERE collection_id=? ORDER BY created_txn {order}, row_id {order}", (manifest.collection_id,))
    names = [column[0] for column in cursor.description]
    found = typed_storage.hydrate(conn, [dict(zip(names, row, strict=True)) for row in cursor])
    return [{**row, "values": json.loads(row["values_json"])} for row in found]


def render_block(manifest, row: Mapping, newline: str, *, stamp=None) -> str:
    """One row's block: its heading, record id, view stamp when given, and child rows."""
    return record_formats.render_markdown_log_item(manifest, row["values"], row["item_key"], newline,
                                                   view_stamp=stamp)


def frame_for_render(frame: LogFrame, *, has_blocks: bool) -> LogFrame:
    """The frame representation, including the separator its first block needs."""
    if has_blocks and frame.text and not frame.text.endswith(("\n", "\r")):
        return replace(frame, text=frame.text + frame.newline)
    return frame


def render(manifest, frame: LogFrame, blocks: Iterable[Mapping], *, stamp=None, frame_stamp=None) -> str:
    """The log view: the frame, one block per row in order, then the frame's suffix.

    ``stamp(row)`` gives the view stamp of each block (§16 A2); the export passes none,
    because file mode has no store to name.
    """
    rendered = "".join(
        render_block(manifest, row, frame.newline, stamp=None if stamp is None else stamp(row)) for row in blocks
    )
    prefix = frame_for_render(frame, has_blocks=bool(rendered)).text
    if rendered and not frame.suffix and not frame.final_newline:
        # Remove only the renderer's final separator; authored suffix whitespace stays exact.
        rendered = rendered.removesuffix(frame.newline * 2)
    text = prefix + rendered + frame.suffix
    if frame_stamp is not None:
        data = text.encode()
        heading, _ = section_bounds(manifest, data)
        # A marker before an unterminated EOF heading needs no synthetic authored separator.
        boundary = heading.start if heading.end == len(data) and not text.endswith(("\n", "\r")) else heading.end
        before, after = data[:boundary], data[boundary:]
        separator = b"" if not before or before.endswith((b"\n", b"\r")) else frame.newline.encode()
        marker = "<!-- exomem-frame " + records._canonical_json(frame_stamp).decode() + " -->" + frame.newline
        text = (before + separator + marker.encode() + after).decode()
    return text


def stray_lines(block: bytes, record) -> list[bytes]:
    """The lines of a log block that none of its row's values holds, so that a render would lose them.

    ``record`` is the adapter's record of ``block``. Every other line is the block's heading, a
    child row the adapter parsed into the row, a record-id, audit or view-stamp comment, or a
    blank line. The row's values hold those, whatever their spacing, so a render keeps them.
    """
    rows = {child.span.start - record.span.start for child in record.children}
    stray, offset = [], 0
    for line in block.splitlines(keepends=True):
        text = line.strip()
        if offset and offset not in rows and text and not (
                record_formats._MARKER.fullmatch(text) or records._LOG_AUDIT_MARKER.fullmatch(text)
                or record_formats._LOG_STAMP.fullmatch(text)):
            stray.append(text)
        offset += len(line)
    return stray
