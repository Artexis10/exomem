"""Read an app export one item at a time and render each item as a Markdown transcript.

Which export is which, and how a transcript labels each author role, is registry data (`source_formats`).
This module holds what every parser shares (the item types, names and the JSON readers); the Evernote and mailbox
parsers sit in `export_notes` and `export_mail`, and `export_parsers` lists them all. The export is untrusted content:
a reader keeps its text as data and never follows a link in it, runs it or sends it to a model.

No vendor documents the JSON shapes. The readers follow what the exports carried in 2026 and treat every field
as optional but the item keys that the registry declares: a missing field degrades the transcript and never
fails the item. Only an item whose message structure has the wrong type is unreadable.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import mimetypes
import unicodedata
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, NamedTuple

import ijson

from .media_types import media_type_for

if TYPE_CHECKING:
    from .source_formats import SourceFormat

#: The `reason` code of an item that cannot be read; the filing view reports it with the item.
UNREADABLE_ITEM = "IMPORT_ITEM_UNREADABLE"
#: An item that two other items' names leave no free name for; it is not unreadable, so it has a code of its own.
NAME_TAKEN_ITEM = "IMPORT_ITEM_NAME_TAKEN"
#: A message that the entry's `skip_labels` leaves out: the cell read it and filed nothing, by the entry's own rule.
SKIPPED_LABEL_ITEM = "IMPORT_ITEM_SKIPPED_LABEL"
_MAX_ID_CHARS = 96
_MAX_TITLE_CHARS = 200
#: The C0 controls but tab and newline: a NUL makes the file read as binary, and no other one is text.
_NO_CONTROLS = {code: None for code in range(0x20) if code not in (0x09, 0x0A)}
_MAX_NAME_BYTES = 255
#: The extension of a staged file that the media pipeline reads: the longest one that a vendor's name keeps.
_MAX_EXTENSION_CHARS = 16
#: Characters of a vendor name that are read for its stem or its extension; a name is untrusted and may be megabytes long.
_MAX_SCANNED_CHARS = 4096


@dataclass(frozen=True, slots=True)
class Part:
    """One attachment that a reader keeps for an item: the file it becomes in the stage, and where its bytes wait.

    `name` is final: the reader names every part of an item through `part_names` before it renders the transcript,
    so the transcript can name them. `data` is the bytes, or a hidden file that the expander moves into place.
    """

    name: str
    data: bytes | Path
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class Transcript:
    """One readable item: the file stem it is named by, its title, its own times, the Markdown that files as its Source and its attachments."""

    item_id: str
    title: str
    created: str | None  # ISO 8601 in UTC, as the item states it
    updated: float | None  # seconds since the epoch, as the item states it
    text: str
    parts: tuple[Part, ...] = ()


@dataclass(frozen=True, slots=True)
class Unreadable:
    """One item that cannot be read: the id that lists it (the vendor's, else its position), and what is wrong with it."""

    item_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class Skipped:
    """One item that the cell read and left out by the registry entry's own rule: the id that lists it, and what it matched."""

    item_id: str
    reason: str


Item = Transcript | Unreadable | Skipped


class ExportFault(Exception):
    """The export's stream ends early or is not the format it declared; the items before it were read."""


class ReadItem(NamedTuple):
    """One item as a reader yields it: the export position that follows it (to resume from), and the item."""

    after: int
    item: Item


def _plain_name(value: object, fallback: str) -> str:
    """A vendor id as one file-name stem: letters, digits, dot, dash and underscore; else `fallback`."""
    if not isinstance(value, str):
        return fallback
    stem = "".join(char if char.isascii() and (char.isalnum() or char in "._-") else "-" for char in value)
    stem = stem.strip(".-")[:_MAX_ID_CHARS]
    return stem or fallback


def printable(text: str) -> str:
    return text.translate(_NO_CONTROLS)


def printable_line(value: object, limit: int) -> str:
    """A header-like value as one printable line of at most `limit` characters; empty when it is not text."""
    return " ".join(printable(value).split())[:limit] if isinstance(value, str) else ""


def hashed_stem(*pieces: str | bytes) -> str:
    """The file stem of an item that its export gives no id: a hash of the pieces that make the item, each length-framed."""
    digest = hashlib.sha256()
    for piece in pieces:
        data = piece.encode("utf-8", errors="replace") if isinstance(piece, str) else piece
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()[:32]


# A closed table of the MIME types that the standard library's own tables know; no system file is read, so a name is the same on every host.
_MIME = mimetypes.MimeTypes()


def _clean_stem(stem: str) -> str:
    """Letters of any script, digits, and a few joining marks; everything else, such as a path separator or a control, becomes a dash."""
    kept = "".join(char if char.isalnum() or char in " ._-()" else "-" for char in unicodedata.normalize("NFC", stem))
    return " ".join(kept.split()).lstrip(".-_ ").rstrip(".-_ ")


def _fit(stem: str, suffix: str) -> str:
    """The stem cut so that `stem + suffix` is at most 255 bytes, on a character boundary; a cut of a stem that is already NFC stays NFC."""
    room = _MAX_NAME_BYTES - len(suffix.encode("utf-8"))
    return stem.encode("utf-8")[:room].decode("utf-8", errors="ignore")


def part_names(parts: list[tuple[str, str]]) -> list[str]:
    """The final file name of each part of one item, from its vendor name and MIME type, in order.

    A name keeps its stem's letters in any script and its extension, or takes the extension of its MIME type
    when it has none. It is NFC-normalised, at most 255 bytes, never starts with a dot and is unique within the
    item's folder without regard to case, as a volume that folds case would see it. A part with no usable name
    is called `part-<number>`. The work is linear in the names' length and in the number of parts.
    """
    taken: set[str] = set()
    used: dict[str, int] = {}  # the last counter that each name handed out, so that a repeated name does not recount from 1
    names = []
    for number, (raw, media_type) in enumerate(parts, 1):
        base = raw.replace("\\", "/").rsplit("/", 1)[-1]
        stem, dot, extension = base.rpartition(".")
        if not dot or not stem.strip(". "):  # no extension, or only a leading dot: the whole name is the stem
            stem, extension = base, ""
        # A name is untrusted and may be megabytes long: cut it before any work per character, well past what a name keeps.
        extension = "".join(char for char in unicodedata.normalize("NFC", extension[:_MAX_SCANNED_CHARS]) if char.isascii() and char.isalnum())
        extension = extension[:_MAX_EXTENSION_CHARS]
        if not extension:
            extension = (_MIME.guess_extension(media_type.split(";")[0].strip().lower()) or "").lstrip(".")
        suffix = f".{extension}" if extension else ""
        stem = _clean_stem(stem[:_MAX_SCANNED_CHARS]) or f"part-{number}"
        # Cut once to the room that the widest counter of this item needs, so a name and its numbered forms share one
        # stem: names that differ only past that cut then share one counter, and no repeat walks another's numbers.
        stem = _fit(stem, f" ({len(parts)})" + suffix)
        first = stem + suffix
        count = used.get(first.casefold(), 0) + 1
        while True:
            tag = f" ({count})" if count > 1 else ""
            name = stem + tag + suffix
            if name.casefold() not in taken:
                break
            count += 1
        used[first.casefold()] = count
        taken.add(name.casefold())
        names.append(name)
    return names


def indexable(name: str, media_type: str = "") -> bool:
    """Whether the media pipeline reads a part: the extension of its name decides, else the extension of the MIME type it declares.

    A part that declares a readable type under a name of another kind, such as plain text called `archive.mbox`,
    is kept as that name; the pipeline then files it as the file that its name says it is.
    """
    if media_type_for(name) is not None:
        return True
    extension = _MIME.guess_extension(media_type.split(";")[0].strip().lower()) if media_type else None
    return extension is not None and media_type_for(f"part{extension}") is not None


def stamp(moment: dt.datetime) -> str:
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


def title_of(value: object, created: dt.datetime | None, fmt: SourceFormat) -> str:
    title = " ".join(printable(value).split())[:_MAX_TITLE_CHARS] if isinstance(value, str) else ""
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
    kept = printable(text) if isinstance(text, str) else ""
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


def positional_id(index: int) -> str:
    """The id of an item that carries none: its position in the export."""
    return f"item-{index + 1:06d}"


def _chatgpt_id(item: Mapping[str, Any], index: int) -> str:
    return _plain_name(item.get("conversation_id") or item.get("id"), positional_id(index))


def _chatgpt(item: Mapping[str, Any], fmt: SourceFormat, index: int) -> Transcript | Unreadable:
    item_id = _chatgpt_id(item, index)
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
    title = title_of(item.get("title"), created, fmt)
    return Transcript(
        item_id, title, stamp(created) if created else None, updated.timestamp() if updated else None,
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
            name = " ".join(printable(str(attachment.get("file_name") or "")).split())
            heading = f"{fmt.attachment}: {name}" if name else fmt.attachment
            blocks.append(_quoted(f"{heading}\n\n{content}"))
    return blocks


def _claude_id(item: Mapping[str, Any], index: int) -> str:
    return _plain_name(item.get("uuid"), positional_id(index))


def _claude(item: Mapping[str, Any], fmt: SourceFormat, index: int) -> Transcript | Unreadable:
    item_id = _claude_id(item, index)
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
    title = title_of(item.get("name"), created, fmt)
    return Transcript(
        item_id, title, stamp(created) if created else None, updated.timestamp() if updated else None,
        _transcript(title, shown))


class _Json(NamedTuple):
    """What a JSON-array parser knows of its items: the id that lists one, and how one reads."""

    id_of: Callable[[Mapping[str, Any], int], str]
    read: Callable[[Mapping[str, Any], SourceFormat, int], Transcript | Unreadable]


#: The JSON-array parsers by their closed `parser` value; `export_parsers` lists them with the other containers.
READERS: Mapping[str, _Json] = {
    "chatgpt-conversations": _Json(_chatgpt_id, _chatgpt),
    "claude-conversations": _Json(_claude_id, _claude),
}


def _read_item(reader: _Json, item: Mapping[str, Any], fmt: SourceFormat, index: int) -> Transcript | Unreadable:
    """One item through its reader. An item is untrusted, so a fault that no reader foresaw fails that item and no other.

    The failure lists the item by the vendor's id when it has one, as a reader that finished would have.
    """
    try:
        return reader.read(item, fmt, index)
    except Exception as error:  # noqa: BLE001 -- one item of a stranger's file must never stop the items after it
        try:
            item_id = reader.id_of(item, index)
        except Exception:  # noqa: BLE001 -- an item too damaged to name is listed by its position
            item_id = positional_id(index)
        return Unreadable(item_id, f"it could not be read ({type(error).__name__})")


def recognise_json(stream: BinaryIO, fmt: SourceFormat) -> bool:
    """Whether the stream is a JSON array whose first item carries every key of the entry's `item_keys`."""
    try:
        first = next(ijson.items(stream, "item", use_float=True), None)
    except (ijson.JSONError, OSError, ValueError):
        return False
    return isinstance(first, dict) and all(key in first for key in fmt.item_keys)


def read(fmt: SourceFormat, stream: BinaryIO, *, start: int = 0, workdir: Path | None = None) -> Iterator[ReadItem]:
    """Each item of the JSON-array export from position `start`, one item in memory at a time; its position is the item's index.

    Raises `ExportFault` after the last item that it could read when the stream ends early or is not JSON.
    """
    reader = READERS[fmt.parser]
    try:
        for index, item in enumerate(ijson.items(stream, "item", use_float=True)):
            if index < start:
                continue
            if isinstance(item, Mapping):
                yield ReadItem(index + 1, _read_item(reader, item, fmt, index))
            else:
                yield ReadItem(index + 1, Unreadable(positional_id(index), "it is not an object"))
    except (ijson.JSONError, UnicodeDecodeError, OSError) as error:
        raise ExportFault(type(error).__name__) from None
