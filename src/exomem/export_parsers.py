"""The closed list of export parsers that code implements: each one's container, its needed facts and its reader.

A `source-formats` registry entry names one of these by its `parser` value. Each parser owns how its container is
checked and read (a JSON array, an XML document, an mbox), so the registry holds vendor facts and display words
only, and cannot pair a parser with the wrong container. An entry that lacks a fact its parser needs is refused
(`source_formats`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from . import export_mail, export_notes, export_transcripts

if TYPE_CHECKING:
    from .export_transcripts import ReadItem
    from .source_formats import SourceFormat


@dataclass(frozen=True, slots=True)
class Parser:
    """What code knows of one parser value.

    `needs` names the registry attributes, beyond the common ones, that the parser reads; an entry carries all of them and
    no other. `words` is the closed key set of the entry's `words` map, empty for a parser with no such words.
    `recognise` says whether an open staged file is this parser's container and carries the entry's recognition facts.
    `read` yields each item from a position, as (position after it, item), and raises `ExportFault` when the container
    ends early or is not what it declared. `workdir` is a hidden folder in the stage where a reader may keep bytes that
    the expander then moves into place.
    """

    needs: frozenset[str]
    words: frozenset[str]
    recognise: Callable[[BinaryIO, SourceFormat], bool]
    read: Callable[..., Iterator[ReadItem]]


# nosemgrep: ep-word-set -- The registry attribute names that a JSON-array parser reads: this module's own schema, not vocabulary.
_JSON_NEEDS = frozenset({"item_keys", "roles", "attachment"})
_NO_WORDS: frozenset[str] = frozenset()
# nosemgrep: ep-word-set -- The registry attribute names that the notes parser reads: this module's own schema, not vocabulary.
_NOTE_NEEDS = frozenset({"root", "words"})
# nosemgrep: ep-word-set -- The display-word keys that the notes parser reads: this module's own schema, not vocabulary.
_NOTE_WORDS = frozenset({"created", "updated", "tags", "source", "attachments", "extracted", "kept", "cut_short"})
# nosemgrep: ep-word-set -- The registry attribute names that the mailbox parser reads: this module's own schema, not vocabulary.
_MAIL_NEEDS = frozenset({"label_header", "skip_labels", "words"})
# nosemgrep: ep-word-set -- The display-word keys that the mailbox parser reads: this module's own schema, not vocabulary.
_MAIL_WORDS = frozenset({"attachments", "extracted", "kept", "cut_short"})

#: The parser of each closed `parser` value; the keys are the only values that the `source-formats` registry accepts.
PARSERS: Mapping[str, Parser] = {
    "chatgpt-conversations": Parser(_JSON_NEEDS, _NO_WORDS, export_transcripts.recognise_json, export_transcripts.read),
    "claude-conversations": Parser(_JSON_NEEDS, _NO_WORDS, export_transcripts.recognise_json, export_transcripts.read),
    "evernote-notes": Parser(_NOTE_NEEDS, _NOTE_WORDS, export_notes.recognise, export_notes.read),
    "gmail-mbox": Parser(_MAIL_NEEDS, _MAIL_WORDS, export_mail.recognise, export_mail.read),
}


def read(fmt: SourceFormat, stream: BinaryIO, *, start: int, workdir: Path) -> Iterator[ReadItem]:
    """Each item of the export from `start`, through the parser that its entry names."""
    return PARSERS[fmt.parser].read(fmt, stream, start=start, workdir=workdir)
