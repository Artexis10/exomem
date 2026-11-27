"""Read an app export one item at a time and render each item as a Markdown transcript.

Which export is which, and how a transcript labels each author role, is registry data (`source_formats`).
This module knows only how to read each closed `parser`. The export is untrusted content: a reader keeps
its text as data and never follows a link in it, runs it or sends it to a model.

No vendor documents these shapes. The readers follow what the exports carried in 2026 and treat every field
as optional but the item keys that the registry declares: a missing field degrades the transcript and never
fails the item. Only an item whose message structure has the wrong type is unreadable.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, BinaryIO

import ijson

if TYPE_CHECKING:
    from .source_formats import SourceFormat

#: The `reason` of an item that cannot be read; the filing view reports it with the item.
UNREADABLE_ITEM = "IMPORT_ITEM_UNREADABLE"
_MAX_ID_CHARS = 96
_MAX_TITLE_CHARS = 200
#: The C0 controls but tab and newline: a NUL makes the file read as binary, and no other one is text.
_NO_CONTROLS = {code: None for code in range(0x20) if code not in (0x09, 0x0A)}


@dataclass(frozen=True, slots=True)
class Transcript:
    """One readable item: the file stem it is named by, its title, its own times and the Markdown that files as its Source."""

    item_id: str
    title: str
    created: str | None  # ISO 8601 in UTC, as the item states it
    updated: float | None  # seconds since the epoch, as the item states it
    text: str


@dataclass(frozen=True, slots=True)
class Unreadable:
    """One item that cannot be read: the name it would have had, and what is wrong with it."""

    item_id: str
    reason: str


class ExportFault(Exception):
    """The export's stream ends early or is not JSON; the items before it were read."""


def _plain_name(value: object, fallback: str) -> str:
    """A vendor id as one file-name stem: letters, digits, dot, dash and underscore; else `fallback`."""
    if not isinstance(value, str):
        return fallback
    stem = "".join(char if char.isascii() and (char.isalnum() or char in "._-") else "-" for char in value)
    stem = stem.strip(".-")[:_MAX_ID_CHARS]
    return stem or fallback


def _printable(text: str) -> str:
    return text.translate(_NO_CONTROLS)


def _stamp(moment: dt.datetime) -> str:
    return moment.replace(microsecond=0, tzinfo=None).isoformat() + "Z"


def _from_epoch(value: object) -> dt.datetime | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return dt.datetime.fromtimestamp(value, dt.UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _from_iso(value: object) -> dt.datetime | None:
    """The moment in UTC, or None when it is not a time or has no UTC time (an offset can push a date past the last year)."""
    if not isinstance(value, str):
        return None
    try:
        moment = dt.datetime.fromisoformat(value.strip())
        return moment.astimezone(dt.UTC) if moment.tzinfo is not None else moment.replace(tzinfo=dt.UTC)
    except (OverflowError, ValueError):
        return None


def _moment(value: object) -> dt.datetime | None:
    """A time as ChatGPT writes it: epoch seconds, or an ISO 8601 string in the sharded exports; anything else has no time."""
    return _from_iso(value) if isinstance(value, str) else _from_epoch(value)


def _shown_time(moment: dt.datetime | None) -> str:
    return f" · {moment:%Y-%m-%d %H:%M} UTC" if moment is not None else ""


def _title(value: object, created: dt.datetime | None, fmt: SourceFormat) -> str:
    title = " ".join(_printable(value).split())[:_MAX_TITLE_CHARS] if isinstance(value, str) else ""
    if title:
        return title
    return f"{fmt.untitled} {created:%Y-%m-%d}" if created is not None else fmt.untitled


def _role_label(fmt: SourceFormat, role: object) -> str | None:
    """The label that the registry gives an author role; a role that is not a string (the item is damaged) has none."""
    return fmt.roles.get(role) if isinstance(role, str) else None


def _quoted(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def _transcript(title: str, shown: list[tuple[str, dt.datetime | None, str]]) -> str:
    blocks = [f"# {title}"]
    blocks += [f"**{label}**{_shown_time(moment)}\n\n{body}" for label, moment, body in shown]
    return "\n\n".join(blocks) + "\n"


def _keep(text: object) -> str:
    """A message's text as the export holds it, without control characters or the blank lines around it."""
    kept = _printable(text) if isinstance(text, str) else ""
    return kept.strip("\n") if kept.strip() else ""


# ----- ChatGPT: a tree of nodes; the shown conversation is the branch that ends at `current_node`


def _branch(mapping: Mapping[str, Any], current: object) -> list[Mapping[str, Any]]:
    """The nodes from the root to `current_node`, or, when that node is missing, down the newest child of each node."""
    nodes: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    if isinstance(current, str) and current in mapping:
        key: object = current
        while isinstance(key, str) and key in mapping and key not in seen:
            seen.add(key)
            node = mapping[key]
            if not isinstance(node, Mapping):
                break
            nodes.append(node)
            key = node.get("parent")
        return nodes[::-1]
    roots = [key for key, node in mapping.items() if isinstance(node, Mapping) and not node.get("parent")]
    key = roots[0] if roots else None
    while isinstance(key, str) and key in mapping and key not in seen:
        seen.add(key)
        node = mapping[key]
        if not isinstance(node, Mapping):
            break
        nodes.append(node)
        children = node.get("children")
        key = children[-1] if isinstance(children, list) and children else None
    return nodes


def _chatgpt_text(content: object) -> str:
    if not isinstance(content, Mapping):
        return ""
    parts = content.get("parts")
    if not isinstance(parts, list):
        return _keep(content.get("text"))
    pieces = [part if isinstance(part, str) else part.get("text") if isinstance(part, Mapping) else None for part in parts]
    return "\n\n".join(kept for piece in pieces if (kept := _keep(piece)))


def _chatgpt(item: Mapping[str, Any], fmt: SourceFormat, index: int) -> Transcript | Unreadable:
    item_id = _plain_name(item.get("conversation_id") or item.get("id"), f"item-{index + 1:06d}")
    mapping = item.get("mapping")
    if not isinstance(mapping, Mapping):
        return Unreadable(item_id, "its message tree is not an object")
    created = _moment(item.get("create_time"))
    shown = []
    for node in _branch(mapping, item.get("current_node")):
        message = node.get("message")
        if not isinstance(message, Mapping):
            continue
        author = message.get("author")
        label = _role_label(fmt, author.get("role")) if isinstance(author, Mapping) else None
        metadata = message.get("metadata")
        hidden = isinstance(metadata, Mapping) and metadata.get("is_visually_hidden_from_conversation") is True
        body = _chatgpt_text(message.get("content"))
        if label is not None and not hidden and body:
            shown.append((label, _moment(message.get("create_time")), body))
    updated = _moment(item.get("update_time"))
    title = _title(item.get("title"), created, fmt)
    return Transcript(
        item_id, title, _stamp(created) if created else None, updated.timestamp() if updated else None,
        _transcript(title, shown))


# ----- Claude: a flat list of messages, each with a sender


def _claude_text(message: Mapping[str, Any]) -> str:
    if kept := _keep(message.get("text")):
        return kept
    blocks = message.get("content")
    blocks = blocks if isinstance(blocks, list) else []
    pieces = [block.get("text") for block in blocks if isinstance(block, Mapping) and block.get("type") == "text"]
    return "\n\n".join(kept for piece in pieces if (kept := _keep(piece)))


def _attachments(message: Mapping[str, Any], fmt: SourceFormat) -> list[str]:
    """Each attachment that carries extracted text, as a quoted block headed by its file name."""
    attached = message.get("attachments")
    blocks = []
    for attachment in attached if isinstance(attached, list) else []:
        if isinstance(attachment, Mapping) and (content := _keep(attachment.get("extracted_content"))):
            name = " ".join(_printable(str(attachment.get("file_name") or "")).split())
            heading = f"{fmt.attachment}: {name}" if name else fmt.attachment
            blocks.append(_quoted(f"{heading}\n\n{content}"))
    return blocks


def _claude(item: Mapping[str, Any], fmt: SourceFormat, index: int) -> Transcript | Unreadable:
    item_id = _plain_name(item.get("uuid"), f"item-{index + 1:06d}")
    messages = item.get("chat_messages")
    if not isinstance(messages, list):
        return Unreadable(item_id, "its message list is not a list")
    created = _from_iso(item.get("created_at"))
    shown = []
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        label = _role_label(fmt, message.get("sender"))
        body = "\n\n".join(block for block in (_claude_text(message), *_attachments(message, fmt)) if block)
        if label is not None and body:
            shown.append((label, _from_iso(message.get("created_at")), body))
    updated = _from_iso(item.get("updated_at"))
    title = _title(item.get("name"), created, fmt)
    return Transcript(
        item_id, title, _stamp(created) if created else None, updated.timestamp() if updated else None,
        _transcript(title, shown))


#: The reader of each closed `parser` value; the keys are the only `parser` values that the `source-formats` registry accepts.
READERS: Mapping[str, Callable[[Mapping[str, Any], SourceFormat, int], Transcript | Unreadable]] = {
    "chatgpt-conversations": _chatgpt,
    "claude-conversations": _claude,
}


def _read_item(
    reader: Callable[[Mapping[str, Any], SourceFormat, int], Transcript | Unreadable],
    item: Mapping[str, Any], fmt: SourceFormat, index: int,
) -> Transcript | Unreadable:
    """One item through its reader. An item is untrusted, so a fault that no reader foresaw fails that item and no other."""
    try:
        return reader(item, fmt, index)
    except Exception as error:  # noqa: BLE001 -- one item of a stranger's file must never stop the items after it
        return Unreadable(f"item-{index + 1:06d}", f"it could not be read ({type(error).__name__})")


def read(fmt: SourceFormat, stream: BinaryIO, *, start: int = 0) -> Iterator[tuple[int, Transcript | Unreadable]]:
    """Each item of the export from position `start`, as (position, transcript or unreadable), one item in memory at a time.

    Raises `ExportFault` after the last item that it could read when the stream ends early or is not JSON.
    """
    reader = READERS[fmt.parser]
    try:
        for index, item in enumerate(ijson.items(stream, "item", use_float=True)):
            if index < start:
                continue
            if isinstance(item, Mapping):
                yield index, _read_item(reader, item, fmt, index)
            else:
                yield index, Unreadable(f"item-{index + 1:06d}", "it is not an object")
    except (ijson.JSONError, UnicodeDecodeError, OSError) as error:
        raise ExportFault(type(error).__name__) from None
