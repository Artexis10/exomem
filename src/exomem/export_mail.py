"""Read a Gmail Takeout mailbox (an mbox file) one message at a time, each as a transcript and its indexable attachments.

A message starts at a line that starts with `From ` and is either the first line or follows a blank line; a blank
line is empty once CR and LF are stripped, so a CRLF mailbox splits the same way. `>From ` quoting is left as written,
because Gmail's quoting dialect is not documented. The resume position is the byte offset of the next message, so a
slice seeks there instead of reading what it has read. One message is held at a time. Messages are untrusted: their
text is stored as data and nothing in them is followed.
"""

from __future__ import annotations

import csv
import datetime as dt
import email
import email.utils
import hashlib
from collections.abc import Iterator
from email import policy
from email.message import EmailMessage, Message
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from . import mail_text
from .export_transcripts import (
    ExportFault,
    Part,
    ReadItem,
    Skipped,
    Transcript,
    Unreadable,
    hashed_stem,
    indexable,
    part_names,
    printable,
    printable_line,
    stamp,
)

if TYPE_CHECKING:
    from .source_formats import SourceFormat

#: A message longer than this is not read: it counts as failed, and the messages after it file.
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
#: The most that one `readline` takes, so that a mailbox with no line ends cannot fill memory before the size bound applies.
_LINE_CHUNK = 1024 * 1024
_MAX_TITLE_CHARS = 200
_SEPARATOR = b"From "


def recognise(stream: BinaryIO, fmt: SourceFormat) -> bool:
    """Whether the stream is an mbox: its first line starts with `From `."""
    try:
        return stream.read(len(_SEPARATOR)) == _SEPARATOR
    except OSError:
        return False


def _messages(stream: BinaryIO, start: int) -> Iterator[tuple[int, int, bytes | None]]:
    """Each message from byte offset `start` as (offset of its separator line, offset after it, its bytes without the separator).

    The bytes are None for a message over `MAX_MESSAGE_BYTES`, whose rest is read past and dropped.
    """
    stream.seek(start)
    offset = start
    begin: int | None = None
    parts: list[bytes] = []
    size = 0
    too_big = False
    after_blank = True  # the first line of the file, or of a resume, may start a message
    inside_line = False
    while chunk := stream.readline(_LINE_CHUNK):
        starts_line = not inside_line
        inside_line = not chunk.endswith(b"\n")
        if starts_line and after_blank and chunk.startswith(_SEPARATOR):
            if begin is not None:
                yield begin, offset, None if too_big else b"".join(parts)
            begin, parts, size, too_big = offset, [], 0, False
        elif begin is None:
            raise ExportFault("the mailbox does not start with a From line")
        elif not too_big:
            size += len(chunk)
            if size > MAX_MESSAGE_BYTES:
                parts, too_big = [], True
            else:
                parts.append(chunk)
        if starts_line:
            after_blank = not chunk.strip(b"\r\n")
        offset += len(chunk)
    if begin is not None:
        yield begin, offset, None if too_big else b"".join(parts)


def _labels(message: Message, fmt: SourceFormat) -> list[str]:
    """The labels that the entry's `label_header` carries: comma-separated, a label with a comma in quotes."""
    value = mail_text.header(message, fmt.label_header or "")
    if not value:
        return []
    try:
        row = next(csv.reader([value], skipinitialspace=True), [])
    except csv.Error:
        row = value.split(",")
    return [label.strip() for label in row if label.strip()]


def _created(message: Message) -> dt.datetime | None:
    try:
        moment = email.utils.parsedate_to_datetime(mail_text.header(message, "Date"))
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    # A date with no zone is read as UTC, so that the time is the one the header shows.
    try:
        return moment.astimezone(dt.UTC) if moment.tzinfo is not None else moment.replace(tzinfo=dt.UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _is_body(part: Message) -> bool:
    """Whether a part is a text body of the message: plain or HTML text that is neither an attachment nor a named file."""
    return (
        part.get_content_maintype() == "text" and part.get_content_subtype() in ("plain", "html")
        and part.get_content_disposition() != "attachment" and part.get_filename() is None
    )


def _attachment(message: EmailMessage, fmt: SourceFormat) -> tuple[list[Part], list[tuple[str, bool]]]:
    """The message's non-body parts: the ones filed as their own Sources, and every part's display name with whether it was filed."""
    listed = []
    for part in message.walk():
        if (part.is_multipart() and part.get_content_type() != "message/rfc822") or _is_body(part):
            continue  # a container holds parts that walk() lists itself; a forwarded message is one that it names
        try:
            raw = part.get_filename() or ""
        except Exception:  # noqa: BLE001 -- a filename that cannot be decoded leaves the part nameless
            raw = ""
        media_type = part.get_content_type()
        filed = part.get_content_disposition() == "attachment" and indexable(raw, media_type)
        data = part.get_payload(decode=True) if filed else None
        listed.append((raw, media_type, data if isinstance(data, bytes) and data else None))
    filed_parts = [item for item in listed if item[2] is not None]
    names = iter(part_names([(raw, media_type) for raw, media_type, _data in filed_parts]))
    parts: list[Part] = []
    shown: list[tuple[str, bool]] = []
    for raw, media_type, data in listed:
        if data is None:
            shown.append((printable_line(raw, _MAX_TITLE_CHARS) or media_type, False))
            continue
        name = next(names)
        parts.append(Part(name, data, len(data), hashlib.sha256(data).hexdigest()))
        shown.append((name, True))
    return parts, shown


def _transcript(message: EmailMessage, title: str, shown: list[tuple[str, bool]], fmt: SourceFormat) -> str:
    text = f"# {title}\n\n{printable(mail_text.message_text(message, cut_note=fmt.words['cut_short']))}"
    if shown:
        listing = "\n".join(f"- {name} ({fmt.words['extracted' if filed else 'kept']})" for name, filed in shown)
        text += f"\n\n{fmt.words['attachments']}:\n{listing}"
    return text + "\n"


def _item(data: bytes, fmt: SourceFormat) -> Transcript | Skipped:
    message: EmailMessage = email.message_from_bytes(data, policy=policy.default)  # type: ignore[assignment]  # policy.default parses into EmailMessage
    identifier = mail_text.header(message, "Message-ID")
    stem = hashed_stem(identifier) if identifier else hashed_stem(data)
    wanted = {label.casefold() for label in fmt.skip_labels}
    for label in _labels(message, fmt):
        if label.casefold() in wanted:
            return Skipped(stem, f"its label {label} is one that the format leaves out")
    created = _created(message)
    subject = printable_line(mail_text.header(message, "Subject"), _MAX_TITLE_CHARS)
    title = subject or (f"{fmt.untitled} {created:%Y-%m-%d}" if created is not None else fmt.untitled)
    parts, shown = _attachment(message, fmt)
    return Transcript(stem, title, stamp(created) if created is not None else None, None, _transcript(message, title, shown, fmt), tuple(parts))


def read(fmt: SourceFormat, stream: BinaryIO, *, start: int = 0, workdir: Path | None = None) -> Iterator[ReadItem]:
    """Each message of the mailbox from byte offset `start`, one in memory at a time; its position is the offset of the next message."""
    try:
        for begin, after, data in _messages(stream, start):
            if data is None:
                yield ReadItem(after, Unreadable(f"message-{begin}", "it is longer than 64 MiB"))
                continue
            try:
                item = _item(data, fmt)
            except Exception as error:  # noqa: BLE001 -- one message of a stranger's mailbox must never stop the messages after it
                item = Unreadable(f"message-{begin}", f"it could not be read ({type(error).__name__})")
            yield ReadItem(after, item)
    except OSError as error:
        raise ExportFault(type(error).__name__) from None
