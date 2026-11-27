"""Read an Evernote export (`.enex`) one note at a time, each as a transcript and its indexable resources.

The export is one XML document whose shape Evernote's `evernote-export4.dtd` fixes: a root `en-export` that holds
`note` elements. The reader feeds a defusedxml parser in 1 MiB chunks and builds no tree. A custom target keeps the
few small facts of the note being read (title, times, tags, source address), a bounded part of its ENML text and, for
each resource, a stream: the base64 text is decoded as it arrives into a hidden file, hashed on the way, and kept
only when the media pipeline can index the resource. No resource is ever held whole, so memory stays a small multiple
of the content bound plus one chunk. defusedxml refuses entity declarations and external entities. The resume position
is the index of the next note; a slice that resumes reads past the earlier notes without keeping anything of them.
The export is untrusted: its text is stored as data, and no link in it is followed.
"""

from __future__ import annotations

import binascii
import datetime as dt
import hashlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import DefusedXMLParser, ParseError

from . import html_text
from .export_transcripts import (
    ExportFault,
    Part,
    ReadItem,
    Transcript,
    Unreadable,
    hashed_stem,
    indexable,
    part_names,
    positional_id,
    printable,
    printable_line,
    stamp,
    title_of,
)

if TYPE_CHECKING:
    from .source_formats import SourceFormat

#: A note whose text is longer than this counts as failed; the notes after it file.
MAX_CONTENT_CHARS = 64 * 1024 * 1024
_CHUNK = 1024 * 1024
#: Bytes that the reader feeds with no event from the parser: expat holds a whole comment, instruction, attribute value or
#: start tag before it reports one, and a tag's attributes take many times their bytes, so a token this long would hold
#: memory far past its size. Note text and resource data arrive as events in every chunk. The export ends there.
_MAX_QUIET_BYTES = 4 * 1024 * 1024
#: Bounds on the document's shape, far above what the export format uses (about 5 levels and a few dozen names; note text
#: is CDATA). The parser keeps a stack entry per open element and a table entry per distinct name, so a crafted file of
#: unclosed or ever-new elements would hold memory many times its size. The export ends there.
_MAX_DEPTH = 64
_MAX_NAMES = 4096
#: The part of a document that decides whether it is an Evernote export: its root element comes before any note.
_RECOGNISE_BYTES = 64 * 1024
_MAX_FIELD_CHARS = 64 * 1024
_MAX_SHOWN_CHARS = 2000
_TIME_FORMAT = "%Y%m%dT%H%M%SZ"

# nosemgrep: ep-word-set -- The element paths that Evernote's published export DTD fixes, each with the record and attribute it fills: a closed schema, not vocabulary.
_TEXT_FIELDS: Mapping[tuple[str, ...], tuple[str, str]] = {
    ("title",): ("note", "title"),
    ("created",): ("note", "created"),
    ("updated",): ("note", "updated"),
    ("tag",): ("note", "tags"),
    ("note-attributes", "source-url"): ("note", "source"),
    ("resource", "mime"): ("resource", "mime"),
    ("resource", "resource-attributes", "file-name"): ("resource", "name"),
}
_CONTENT = ("content",)
_DATA = ("resource", "data")
_RESOURCE = ("resource",)


@dataclass(slots=True)
class _Resource:
    """One resource of a note as the export gives it: where its decoded bytes wait, and what the note calls it."""

    md5: str = ""
    name: str = ""
    mime: str = ""
    path: Path | None = None
    size: int = 0
    sha256: str = ""


@dataclass(slots=True)
class _Note:
    title: str = ""
    created: str = ""
    updated: str = ""
    source: str = ""
    tags: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)  # the first `MAX_INPUT_CHARS` characters of the ENML, and one more to show a cut
    held: int = 0  # the characters in `kept`
    chars: int = 0  # the characters of the ENML that the export holds
    digest: Any = field(default_factory=hashlib.sha256)  # of all the ENML, so that a note's name does not depend on the part kept
    resources: list[_Resource] = field(default_factory=list)


class _Blob:
    """A resource's base64 text on its way to a hidden file: decoded as it arrives, so that the resource is never held whole."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._sink = open(path, "wb")  # noqa: SIM115 -- closed by finish()
        self._pending = ""
        self._md5 = hashlib.md5(usedforsecurity=False)  # Evernote names a resource by its MD5; it is an identity here, not a defence
        self._sha256 = hashlib.sha256()
        self.size = 0
        self.bad = False

    def feed(self, text: str) -> None:
        if self.bad:
            return
        self._pending += "".join(text.split())
        usable = len(self._pending) // 4 * 4
        if usable:
            block, self._pending = self._pending[:usable], self._pending[usable:]
            self._write(block)

    def _write(self, block: str) -> None:
        try:
            raw = binascii.a2b_base64(block)
        except binascii.Error:
            self.bad = True
            return
        self._md5.update(raw)
        self._sha256.update(raw)
        self._sink.write(raw)
        self.size += len(raw)

    def finish(self) -> None:
        if self._pending and not self.bad:
            self._write(self._pending + "=" * (-len(self._pending) % 4))
        self._pending = ""
        self._sink.close()

    @property
    def md5(self) -> str:
        return self._md5.hexdigest()

    @property
    def sha256(self) -> str:
        return self._sha256.hexdigest()


class _Root:
    """A recognition target: it keeps the name of the first element."""

    def __init__(self) -> None:
        self.root: str | None = None

    def start(self, tag: str, attrib: Mapping[str, str]) -> None:
        if self.root is None:
            self.root = tag

    def end(self, tag: str) -> None:
        pass

    def data(self, text: str) -> None:
        pass

    def close(self) -> None:
        return None


def recognise(stream: BinaryIO, fmt: SourceFormat) -> bool:
    """Whether the stream is an XML document whose root element is the entry's `root`, and holds no entity declaration.

    Entity declarations come before the root, so a document that declares one is refused here and files as an ordinary file.
    """
    target = _Root()
    parser = DefusedXMLParser(target=target, forbid_dtd=False)  # the DTD line of a real export stays; its entities are refused
    try:
        parser.feed(stream.read(_RECOGNISE_BYTES))
    except DefusedXmlException:
        return False
    except (ParseError, ValueError, OSError):
        pass  # a root that came before the fault is still the root
    return target.root is not None and target.root == fmt.root


def _moment(value: str) -> dt.datetime | None:
    """A time as ENEX writes it, `yyyymmddThhmmssZ` in UTC; anything else has no time."""
    try:
        return dt.datetime.strptime(value.strip(), _TIME_FORMAT).replace(tzinfo=dt.UTC)
    except ValueError:
        return None


class _Target:
    """The parser's callbacks. They build no tree: each keeps one note's few facts and streams its resources to disk."""

    def __init__(self, fmt: SourceFormat, start: int, workdir: Path) -> None:
        self._fmt = fmt
        self._start = start
        self._workdir = workdir
        self._files = 0
        self._index = 0
        self._path: list[str] = []
        self._names: set[str] = set()  # the distinct element and attribute names met so far
        self._note: _Note | None = None
        self._resource: _Resource | None = None
        self._mode: str | None = None  # what the characters now arriving belong to: a small field, the content or a resource's data
        self._buffer: list[str] = []
        self._buffered = 0
        self._blob: _Blob | None = None
        self.ready: list[ReadItem] = []
        self.root: str | None = None
        self.events = 0  # how many events the parser has reported; a count, so no platform's index width matters

    def _event(self) -> None:
        self.events += 1

    def comment(self, text: str) -> None:
        self._event()

    def start_ns(self, prefix: str, uri: str) -> None:
        # The export format declares no namespace (note text is CDATA), and the parser keeps a table entry for each
        # prefixed name, which a crafted file could grow without bound past every name check.
        raise ValueError("the document declares an XML namespace, which an Evernote export never does")

    def pi(self, target: str, data: str) -> None:
        self._event()

    def drain(self) -> Iterator[ReadItem]:
        ready, self.ready = self.ready, []
        yield from ready

    def start(self, tag: str, attrib: Mapping[str, str]) -> None:
        self._event()
        if len(self._path) >= _MAX_DEPTH:
            raise ValueError(f"the document nests elements deeper than {_MAX_DEPTH} levels")
        self._names.add(tag)
        self._names.update(attrib)
        if len(self._names) > _MAX_NAMES:
            raise ValueError(f"the document uses more than {_MAX_NAMES} distinct element and attribute names")
        self._path.append(tag)
        if len(self._path) == 1:
            self.root = tag
            return
        if len(self._path) == 2 and tag == "note":
            self._note = None if self._index < self._start else _Note()
            return
        if self._note is None:
            return
        key = tuple(self._path[2:])
        if key == _RESOURCE:
            self._resource = _Resource()
        elif key == _CONTENT:
            self._mode = "content"
        elif key == _DATA and self._resource is not None:
            if attrib.get("encoding", "base64") == "base64":
                self._mode, self._blob = "data", self._open_blob()
        elif key in _TEXT_FIELDS:
            self._mode, self._buffer, self._buffered = "field", [], 0

    def _open_blob(self) -> _Blob:
        self._workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._files += 1
        return _Blob(self._workdir / f"{self._files}.part")

    def data(self, text: str) -> None:
        self._event()
        note = self._note
        if note is None or self._mode is None:
            return
        if self._mode == "data":
            if self._blob is not None:
                self._blob.feed(text)
        elif self._mode == "content":
            note.digest.update(text.encode("utf-8", errors="replace"))
            note.chars += len(text)
            room = html_text.MAX_INPUT_CHARS + 1 - note.held
            if note.chars > MAX_CONTENT_CHARS:
                note.kept, note.held = [], 0  # the note fails; its text is read past and dropped
            elif room > 0:
                note.kept.append(text[:room])
                note.held += min(len(text), room)
        elif self._buffered < _MAX_FIELD_CHARS:
            self._buffer.append(text)
            self._buffered += len(text)

    def end(self, tag: str) -> None:
        self._event()
        key = tuple(self._path[2:])
        note = self._note
        if note is not None and len(self._path) >= 3:
            if key == _CONTENT or key in _TEXT_FIELDS:
                self._finish_field(note, key)
            elif key == _DATA:
                self._finish_data()
            elif key == _RESOURCE:
                self._finish_resource(note)
        if len(self._path) == 2 and tag == "note":
            self._finish_note()
        self._path.pop()

    def _finish_field(self, note: _Note, key: tuple[str, ...]) -> None:
        self._mode = None
        if key == _CONTENT:
            return
        value = "".join(self._buffer).strip()
        record, attribute = _TEXT_FIELDS[key]
        if record == "resource":
            if self._resource is not None:
                setattr(self._resource, attribute, value)
        elif attribute == "tags":
            note.tags.append(value)
        else:
            setattr(note, attribute, value)

    def _finish_data(self) -> None:
        blob, resource = self._blob, self._resource
        self._mode, self._blob = None, None
        if blob is None or resource is None:
            return
        blob.finish()
        if blob.bad or blob.size == 0:
            blob.path.unlink(missing_ok=True)
            return
        resource.md5, resource.path, resource.size, resource.sha256 = blob.md5, blob.path, blob.size, blob.sha256

    def _finish_resource(self, note: _Note) -> None:
        resource, self._resource = self._resource, None
        if resource is None:
            return
        if resource.path is not None and not indexable(resource.name, resource.mime):
            resource.path.unlink(missing_ok=True)  # a type the pipeline cannot index stays in the kept export
            resource.path = None
        note.resources.append(resource)

    def _finish_note(self) -> None:
        note, self._note = self._note, None
        index, self._index = self._index, self._index + 1
        if note is None:
            return
        if note.chars > MAX_CONTENT_CHARS:
            for resource in note.resources:
                if resource.path is not None:
                    resource.path.unlink(missing_ok=True)
            self.ready.append(ReadItem(self._index, Unreadable(positional_id(index), "its text is longer than 64 MiB")))
            return
        self.ready.append(ReadItem(self._index, self._transcript(note)))

    def _transcript(self, note: _Note) -> Transcript:
        fmt = self._fmt
        created, updated = _moment(note.created), _moment(note.updated)
        title = title_of(note.title, created, fmt)
        kept = [resource for resource in note.resources if resource.path is not None]
        names = part_names([(resource.name, resource.mime) for resource in kept])
        shown = {id(resource): name for resource, name in zip(kept, names, strict=True)}
        for resource in note.resources:
            shown.setdefault(id(resource), printable_line(resource.name, _MAX_SHOWN_CHARS) or resource.mime or fmt.words["attachments"])
        by_hash = {resource.md5: shown[id(resource)] for resource in note.resources if resource.md5}
        converted = html_text.to_text("".join(note.kept), inline={
            "en-todo": lambda attrs: "[x] " if attrs.get("checked") == "true" else "[ ] ",
            "en-media": lambda attrs: f"[{by_hash.get((attrs.get('hash') or '').lower()) or attrs.get('type') or fmt.words['attachments']}]",
        })
        head = [f"{fmt.words['created']}: {created:%Y-%m-%d %H:%M} UTC"] if created else []
        head += [f"{fmt.words['updated']}: {updated:%Y-%m-%d %H:%M} UTC"] if updated else []
        tags = [tag for tag in (printable_line(tag, _MAX_FIELD_CHARS) for tag in note.tags) if tag]
        head += [f"{fmt.words['tags']}: {', '.join(tags)}"] if tags else []
        source = printable_line(note.source, _MAX_SHOWN_CHARS)
        head += [f"{fmt.words['source']}: {source}"] if source else []
        blocks = [f"# {title}", "\n".join(head), converted.text + (f"\n\n{fmt.words['cut_short']}" if converted.cut else "")]
        if note.resources:
            listing = "\n".join(
                f"- {shown[id(resource)]} ({fmt.words['extracted' if resource.path is not None else 'kept']})" for resource in note.resources)
            blocks.append(f"{fmt.words['attachments']}:\n{listing}")
        text = printable("\n\n".join(block for block in blocks if block)) + "\n"
        parts = tuple(
            Part(name, resource.path, resource.size, resource.sha256)
            for resource, name in zip(kept, names, strict=True) if resource.path is not None)
        stem = hashed_stem(note.title, note.created, note.digest.digest())
        return Transcript(stem, title, stamp(created) if created else None, updated.timestamp() if updated else None, text, parts)

    def close(self) -> None:
        return None

    def abandon(self) -> None:
        """Stop in the middle of a resource: close its file. The expander removes the hidden folder that holds it."""
        if self._blob is not None:
            self._blob.finish()
            self._blob = None


def read(fmt: SourceFormat, stream: BinaryIO, *, start: int = 0, workdir: Path | None = None) -> Iterator[ReadItem]:
    """Each note of the export from index `start`, one in memory at a time; its position is the index of the next note.

    Raises `ExportFault` after the last note that it could read when the document ends early or is not well-formed.
    """
    if workdir is None:
        raise ValueError("a notes reader needs a hidden folder for the resources that it keeps")
    target = _Target(fmt, start, workdir)
    parser = DefusedXMLParser(target=target, forbid_dtd=False)
    fault: str | None = None
    quiet = 0  # bytes fed since the parser last reported an event, give or take one chunk
    try:
        while True:
            chunk = stream.read(_CHUNK)
            events = target.events
            try:
                parser.feed(chunk) if chunk else parser.close()
            except (ParseError, ValueError, OSError) as error:  # a refused entity is a ValueError
                fault = type(error).__name__
            quiet = 0 if target.events != events else quiet + len(chunk)
            if fault is None and quiet > _MAX_QUIET_BYTES:
                fault = "a single comment, instruction, attribute value or start tag is longer than 4 MiB"
            yield from target.drain()
            if fault is not None or not chunk:
                break
    finally:
        target.abandon()
    if fault is None and target.root != fmt.root:
        fault = "not the document that the format names"
    if fault is not None:
        raise ExportFault(fault)
