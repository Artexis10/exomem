"""Pure construction of a bounded episode recap. No I/O, no model, no write.

A recap is what the recording agent says a conversation did: what was worked
on, what was decided, what was left open, and at most a few verbatim user
statements. It is never the conversation. Every bound here is enforced after
normalisation and before anything is written, so a refusal leaves the vault
exactly as it was.

The server authors nothing in a recap. It renders the agent's own fields into a
fixed shape, digests them, and derives the filename from the episode key, the
recording time and the digest, so the filename alone groups revisions of one
episode (`filename_parts`) and orders them without a read.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import secrets
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from . import memory_refs
from .episode_model import EpisodeError
from .governance import scrubber
from .vault import slugify_title

EPISODE_KEY_RE = re.compile(r"^ep-[0-9a-f]{32}$")
CLIENT_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")

MAX_SUBJECT_CHARS = 120
MAX_SUMMARY_CHARS = 180
MAX_ITEMS = 5
MAX_ITEM_CHARS = 200
MAX_SAID = 3
MAX_SAID_CHARS = 300
MAX_ABOUT = 3
#: The whole rendered recap, in UTF-8 bytes. Binding even when every field is
#: inside its own cap: fifteen wide-script items fit their character caps and
#: still add up to a transcript by another name.
MAX_BODY_BYTES = 4096
_SUBJECT_SLUG_CHARS = 40
_FALLBACK_SLUG = "episode"
_DIGEST_LABEL = "exomem-episode-recap-v1"
_HOOK_KEY_LABEL = "exomem-episode-key-v1"

#: Line and paragraph separators and the bidirectional overrides. Not control
#: characters to `unicodedata`, but each either breaks the one-line rule or
#: makes the rendered text read differently from the bytes a reviewer checks.
_REFUSED_FORMAT_CHARS = frozenset(
    chr(point) for point in (0x2028, 0x2029, *range(0x202A, 0x202F), *range(0x2066, 0x206A))
)

#: `<subject>-ep<group>-<order>-<digest>[-n].md`: the episode key's group, the
#: UTC recording time to the microsecond (lexicographically ordered), and the
#: recap digest. The order token is what picks an episode's newest revision
#: without a read: retiring an older revision rewrites its frontmatter, so
#: modification time cannot.
FILENAME_RE = re.compile(
    r"-ep(?P<group>[0-9a-f]{12})-(?P<order>\d{8}t\d{12})-(?P<digest>[0-9a-f]{8})(?:-\d+)?\.md$"
)

_SECTIONS: tuple[tuple[str, str], ...] = (
    ("worked_on", "Worked on"),
    ("decided", "Decided"),
    ("open", "Open"),
)


@dataclass(frozen=True)
class Recap:
    """One validated recap revision, ready for the Source writer."""

    key: str
    subject: str
    summary: str
    body: str
    digest: str
    slug: str
    group: str
    about: tuple[str, ...] = ()
    client: str | None = None
    frontmatter: dict[str, Any] = field(default_factory=dict)


def _error(code: str, reason: str) -> EpisodeError:
    return EpisodeError(code, reason)


def mint_key() -> str:
    """A fresh server-side episode key for a client that holds none."""
    return f"ep-{secrets.token_hex(16)}"


def hook_key(client: str, session_id: str) -> str:
    """The episode key a hook derives from identifiers it already holds.

    A pure function of `(client, session_id)`, so it survives compaction and
    resumption without the hook reading a single transcript record. The
    standalone Stop hook carries a copy; `tests/test_capture_nudge_episode.py`
    pins the two together.
    """
    material = f"{_HOOK_KEY_LABEL}\0{client}\0{session_id}".encode("utf-8", "surrogatepass")
    return "ep-" + hashlib.sha256(material).hexdigest()[:32]


def key_group(key: str, audience: str) -> str:
    """The 12-hex filename token every revision of one episode shares, per audience.

    Scoped by the recording audience, so one key continued by two audiences is
    two groups: neither lists, supersedes nor replays the other's revisions.
    The owner's stdio, CLI and REST doors are one audience, so the owner's
    conversation is one group on every door.
    """
    material = f"{audience}\0{key}".encode("utf-8", "surrogatepass")
    return hashlib.sha256(material).hexdigest()[:12]


def filename_parts(name: str) -> tuple[str, str, str] | None:
    """`(group, order, digest8)` from a recap filename, or `None`."""
    match = FILENAME_RE.search(name)
    if match is None:
        return None
    return match.group("group"), match.group("order"), match.group("digest")


def _line(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise _error("EPISODE_INVALID", f"{name} must be a string")
    text = value.strip()
    if not text:
        raise _error("EPISODE_INVALID", f"{name} must not be empty")
    if any(
        unicodedata.category(char) == "Cc" or char in _REFUSED_FORMAT_CHARS for char in text
    ):
        raise _error("EPISODE_INVALID", f"{name} must be one line without control characters")
    # After the refusals, which see the raw text: no-break, narrow and runs of
    # spaces collapse to one, so every line accepted here is one the Source
    # writer's one-line rule accepts too.
    text = " ".join(text.split())
    if len(text) > maximum:
        raise _error("EPISODE_TOO_LARGE", f"{name} exceeds {maximum} characters")
    return text


def _lines(value: Any, name: str, *, count: int, maximum: int) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise _error("EPISODE_INVALID", f"{name} must be a list of strings")
    if len(value) > count:
        raise _error("EPISODE_TOO_LARGE", f"{name} holds at most {count} entries")
    return tuple(_line(item, name, maximum) for item in value)


def _about(value: Any) -> tuple[str, ...]:
    refs = _lines(value, "about", count=MAX_ABOUT * 4, maximum=2048)
    canonical: list[str] = []
    for ref in refs:
        memory_id = memory_refs.parse_memory_ref(ref)
        if memory_id is None or memory_refs.memory_ref(memory_id) != ref:
            raise _error("EPISODE_INVALID", "about takes canonical exomem:// memory refs only")
        canonical.append(ref)
    unique = tuple(dict.fromkeys(canonical))
    if len(unique) > MAX_ABOUT:
        raise _error("EPISODE_TOO_LARGE", f"about holds at most {MAX_ABOUT} refs")
    return unique


def _render(sections: dict[str, tuple[str, ...]], said: Sequence[str]) -> str:
    blocks: list[str] = []
    for name, heading in _SECTIONS:
        items = sections[name]
        if items:
            blocks.append(f"### {heading}\n\n" + "\n".join(f"- {item}" for item in items))
    if said:
        blocks.append("### Said\n\n" + "\n\n".join(f"> {statement}" for statement in said))
    return "\n\n".join(blocks)


def prepare(
    *,
    episode: str | None,
    subject: Any,
    summary: Any,
    worked_on: Any = None,
    decided: Any = None,
    open: Any = None,  # noqa: A002 - the recap's own field name
    said: Any = None,
    about: Any = None,
    client: Any = None,
    when: dt.datetime,
    audience: str,
) -> Recap:
    """Validate, render and digest one recap revision, or refuse it.

    Refusal codes: `EPISODE_KEY_INVALID`, `EPISODE_INVALID` (shape, newline or
    control character), `EPISODE_TOO_LARGE` (any cap, the body's included),
    `EPISODE_EMPTY` (nothing worked on, decided or left open) and
    `EPISODE_CREDENTIAL` (a credential-shaped value anywhere the agent wrote
    prose, or as the `client` label). An otherwise invalid `client` label is
    dropped rather than refused: attribution is optional and never costs the
    record.
    """
    if episode is None:
        key = mint_key()
    elif isinstance(episode, str) and EPISODE_KEY_RE.fullmatch(episode):
        key = episode
    else:
        raise _error("EPISODE_KEY_INVALID", "episode must be an ep- key of 32 lowercase hex")

    title = _line(subject, "subject", MAX_SUBJECT_CHARS)
    line = _line(summary, "summary", MAX_SUMMARY_CHARS)
    supplied = {"worked_on": worked_on, "decided": decided, "open": open}
    sections = {
        name: _lines(supplied[name], name, count=MAX_ITEMS, maximum=MAX_ITEM_CHARS)
        for name, _heading in _SECTIONS
    }
    statements = _lines(said, "said", count=MAX_SAID, maximum=MAX_SAID_CHARS)
    refs = _about(about)
    # Scrubbed whatever its shape: a valid-looking label is written to the page.
    if isinstance(client, str) and scrubber.scrub_text(client)[1]:
        raise _error("EPISODE_CREDENTIAL", "a recap may not carry credential-shaped text")
    label = client if isinstance(client, str) and CLIENT_LABEL_RE.fullmatch(client) else None

    if not any(sections.values()):
        raise _error("EPISODE_EMPTY", "a recap needs something worked on, decided or left open")
    prose = (title, line, *(item for items in sections.values() for item in items), *statements)
    if any(scrubber.scrub_text(text)[1] for text in prose):
        raise _error("EPISODE_CREDENTIAL", "a recap may not carry credential-shaped text")

    body = _render(sections, statements)
    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise _error("EPISODE_TOO_LARGE", f"the recap exceeds {MAX_BODY_BYTES} bytes")

    digest = hashlib.sha256(
        json.dumps(
            [
                _DIGEST_LABEL,
                title,
                line,
                [list(sections[name]) for name, _heading in _SECTIONS],
                list(statements),
                list(refs),
                label,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    order = when.astimezone(dt.UTC).strftime("%Y%m%dt%H%M%S%f")
    subject_slug = slugify_title(title, max_length=_SUBJECT_SLUG_CHARS)
    if subject_slug == "untitled" and "untitled" not in title.casefold():
        subject_slug = _FALLBACK_SLUG
    group = key_group(key, audience)
    slug = f"{subject_slug}-ep{group}-{order}-{digest[:8]}"

    frontmatter: dict[str, Any] = {"summary": line, "episode": key, "episode_digest": digest}
    if label is not None:
        frontmatter["client"] = label
    if refs:
        frontmatter["about"] = list(refs)
    return Recap(
        key=key,
        subject=title,
        summary=line,
        body=body,
        digest=digest,
        slug=slug,
        group=group,
        about=refs,
        client=label,
        frontmatter=frontmatter,
    )
