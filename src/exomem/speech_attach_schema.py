"""The shape of an attached speech result, and the bounds that its schema and its checks share.

A personal install's agent transcribes or diarises a recording on its machine and attaches the
result with `media.attach-speech` (`speech_attach`). This module holds only the schema the door
publishes and the bounds it states, so that `commands` imports it without loading the attach.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StringConstraints
from typing_extensions import NotRequired, TypedDict

from .speaker_names import MAX_NAME_CHARS

#: The two `process_media` operations that carry a speech result; the door, the commands and the reply view share them.
ATTACH_OPERATION = "attach-speech"
RENAME_OPERATION = "rename-speakers"
# nosemgrep: ep-word-set -- the media tool's closed operations, each implemented by the speech modules.
SPEECH_OPERATIONS = frozenset({ATTACH_OPERATION, RENAME_OPERATION})

# Bounds. They check a payload's shape against the recording, never what it says.
#: A container states its duration rounded, and a last segment may run a moment past it.
TIME_TOLERANCE_SECONDS = 5.0
#: A variable-rate file states its duration as an estimate that is off by a few percent.
TIME_TOLERANCE_FRACTION = 0.02
#: Engines cut segments at pauses; five a second is a word each, beyond any real transcript.
MAX_SEGMENTS_PER_SECOND = 5
#: A diarizer's turns are longer than its segments, so the same bound is generous.
MAX_TURNS_PER_SECOND = 5
#: Fast speech is about 25 characters a second; 60 leaves room for dense scripts and nothing more.
MAX_CHARS_PER_SECOND = 60
#: A call is one MCP message; a ten-hour recording at the rate above would not fit one, so the list is capped.
MAX_ITEMS = 50_000
#: A recording has a handful of voices, and each label takes one name at most.
MAX_NAMES = 64
#: The engine identity and the ASR's own words are each one line of the page's front matter.
MAX_ENGINE_CHARS = 200
#: One segment is a sentence or two; a longer one is a transcript passed as a segment.
MAX_SEGMENT_CHARS = 20_000

_Seconds = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
# A label or track repeats on every line that carries it, so it takes the bound of a name in a label span.
_Label = Annotated[str, StringConstraints(min_length=1, max_length=MAX_NAME_CHARS)]
_Track = Annotated[
    _Label | None,
    Field(description="The microphone or stream the item belongs to, for a recording with one track per voice."),
]


class AttachedSegment(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid")

    start: Annotated[_Seconds, Field(description="Seconds from the start of the recording.")]
    text: Annotated[str, StringConstraints(min_length=1, max_length=MAX_SEGMENT_CHARS)]
    end: NotRequired[
        Annotated[_Seconds | None, Field(description="Omit to end where the next segment of its track starts.")]
    ]
    track: NotRequired[_Track]


class AttachedTurn(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid")

    start: _Seconds
    end: _Seconds
    label: Annotated[_Label, Field(description="The diarizer's own label for this voice.")]
    track: NotRequired[_Track]


class AttachedName(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid")

    label: Annotated[_Label, Field(description="A turn label of this call.")]
    name: Annotated[str, StringConstraints(min_length=1, max_length=MAX_NAME_CHARS)]
    basis: Annotated[
        Literal["person", "profile"],
        Field(description="Who gave the name: a person, or a voice profile that matched. A voice is never enrolled here."),
    ]


class AttachedAsr(TypedDict, total=False):
    __pydantic_config__ = ConfigDict(extra="forbid")

    language: Annotated[str, StringConstraints(min_length=1, max_length=16)]
    language_probability: Annotated[float, Field(strict=True, ge=0, le=1)]
    language_source: Annotated[str, StringConstraints(min_length=1, max_length=MAX_ENGINE_CHARS)]
    language_mode: Annotated[str, StringConstraints(min_length=1, max_length=MAX_ENGINE_CHARS)]
    speech_seconds: _Seconds


Segments = Annotated[
    list[AttachedSegment],
    Field(min_length=1, max_length=MAX_ITEMS, description="Transcribed speech, in any order; the cell sorts by start."),
]
Turns = Annotated[
    list[AttachedTurn],
    Field(max_length=MAX_ITEMS, description="Who spoke when. Omit for a transcript with no speakers."),
]
Names = Annotated[
    list[AttachedName],
    Field(max_length=MAX_NAMES, description="Names for labels of `turns`; each label and each name appears once."),
]
Renames = Annotated[
    dict[_Label, Annotated[str, StringConstraints(min_length=1, max_length=MAX_NAME_CHARS)]],
    Field(min_length=1, max_length=MAX_NAMES, description="Label as the page shows it, mapped to the person's name."),
]
Engine = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_ENGINE_CHARS),
    Field(description="The engine and model that produced the result, one line. The page records it as attached, unchecked."),
]
