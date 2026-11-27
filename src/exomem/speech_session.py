"""Group the separate recordings of one event as a session, and keep the session's transcript current.

A session page holds one transcript of several recordings, one track for each recording. It is a page
of the note type that the note-type registry marks as the session transcript type (an attribute, never
the entry's key), in that type's folder, and it links each member Source. The link carries the track's
name (`[[Source|Host]]`), so a member, its track and the page's links are one fact that no field can
contradict. The page keeps what speech wrote above `OWNER_SENTINEL` and what the owner wrote below it.

A member's extraction or speaker names change after the grouping, so the session finds its pages through
the inbound-link index (`vault.find_inbound_wikilinks_many`): a changed member lists the pages that link
it, and speech keeps those in the session type's folder. No change reads every session page.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from . import (
    access,
    extraction_parts,
    media_store,
    note_types,
    speaker_names,
    speech_attach,
    speech_tracks,
)
from .kbdir import managed_join
from .semantic_segments import parse_timed_lines, render_timed_lines
from .speech_attach_schema import GROUP_OPERATION
from .vault import (
    MISSING_CONTENT_HASH,
    PlannedWrite,
    authored_title,
    content_hash,
    find_inbound_wikilinks_many,
    parse_frontmatter,
    read_guarded_text,
    render_frontmatter_document,
    serialize_frontmatter,
    slugify_title,
)

log = logging.getLogger(__name__)

#: What the page records when its members share no published time: each one is taken to start with the others.
ASSUMED_ALIGNMENT = "assumed-start"
#: Everything below this line is the owner's, and a render again keeps it as it stands.
OWNER_SENTINEL = "<!-- exomem:session-owner-notes -->"
#: One session holds the tracks of one event; a longer list is a folder of recordings, not a session.
MAX_MEMBERS = 16
MAX_TITLE_CHARS = 120
# A member link is Obsidian's alias form, `[[target|alias]]`: a closed syntax that the page itself writes.
_MEMBER_LINK = re.compile(r"^\[\[([^\]|\n]+)\|([^\]\n]+)\]\]$")
_ASSUMED_NOTE = "The recordings share no published start time, so each one is taken to start with the others."


def refuse(code: str, reason: str):
    return speech_attach.refuse(code, reason)


def session_type() -> note_types.NoteType:
    """The registry's session transcript type, found by its attribute."""
    types = note_types.shipped(note_types.session_transcript)
    if len(types) != 1 or types[0].folder is None:
        # The shipped pack marks exactly one compiled type, so any other count means the pack was altered.
        raise refuse("SESSION_TYPE_UNAVAILABLE", "the note-type registry marks no single session transcript type")
    return types[0]


def _is_session(fields: Mapping[str, Any]) -> bool:
    return str(fields.get("type", "")).strip().casefold() == session_type().key


@dataclass(frozen=True)
class Member:
    track: str
    #: The member's Source page, vault-relative.
    page: str
    #: Seconds from the first member's start to this member's start.
    offset: float


@dataclass(frozen=True)
class Page:
    """What a session page records about itself, read from its front matter and its owner's section."""

    title: str
    created: str
    updated: str
    alignment: str
    members: tuple[Member, ...]
    owner: str


def _link(member: Member) -> str:
    return f"[[{member.page.removesuffix('.md')}|{member.track}]]"


def _members_of(fields: Mapping[str, Any]) -> tuple[Member, ...] | None:
    """The members that the page's links and offsets record, or None where the page does not read as a session."""
    sources, offsets = fields.get("sources"), fields.get("track_offsets")
    if not isinstance(sources, list) or not isinstance(offsets, dict) or not sources:
        return None
    members = []
    for item in sources:
        found = _MEMBER_LINK.match(item) if isinstance(item, str) else None
        offset = offsets.get(found.group(2)) if found else None
        if found is None or isinstance(offset, bool) or not isinstance(offset, int | float):
            return None
        members.append(Member(found.group(2), f"{found.group(1)}.md", float(offset)))
    return tuple(members)


def read_page(content: str) -> Page | None:
    """The page's record, or None for a page that this module did not write or that the owner took over."""
    fields, body, _raw = parse_frontmatter(content)
    members = _members_of(fields)
    title = authored_title({}, body)
    if not _is_session(fields) or members is None or title is None or OWNER_SENTINEL not in body:
        return None
    return Page(
        title, str(fields.get("created", "")), str(fields.get("updated", "")),
        str(fields.get("alignment", "")), members, body.split(OWNER_SENTINEL, 1)[1],
    )


def align(starts: Sequence[speech_tracks.Start | None]) -> tuple[str, list[float]]:
    """How the members line up: by one published kind of start when each member has it, else by an assumed start."""
    kinds = {start.kind for start in starts if start is not None}
    if len(kinds) == 1 and all(start is not None for start in starts):
        seconds = [start.seconds for start in starts if start is not None]
        first = min(seconds)
        return kinds.pop(), [round(value - first, 3) for value in seconds]
    return ASSUMED_ALIGNMENT, [0.0] * len(starts)


def _text_of(vault: Path, page: str) -> str:
    """The whole extraction of a member's Source page, parts included; empty when the page is missing."""
    path = vault / page
    try:
        content, _guard = read_guarded_text(vault, path)
    except (OSError, UnicodeError):
        return ""
    return extraction_parts.whole_text(vault, path, content)


def _transcript(vault: Path, members: Sequence[Member]) -> tuple[str, list[str]]:
    """The members' timed lines as one transcript, and the tracks that have no timed line yet.

    Each line keeps its member's time plus the member's offset and takes the track's name for its label,
    before the speaker label the member already carries.
    """
    lines: list[tuple[float, float, str, str | None]] = []
    waiting: list[str] = []
    for member in members:
        found = parse_timed_lines(_text_of(vault, member.page))
        if not found:
            waiting.append(member.track)
        for line in found:
            label = member.track if line.speaker is None else f"{member.track}: {line.speaker}"
            lines.append((member.offset + line.ts, 0.0, line.text, label))
    lines.sort(key=lambda line: line[0])  # a stable sort keeps the members' order for lines that start together
    return render_timed_lines(lines), waiting


def render(vault: Path, page: Page, *, updated: str) -> str:
    """The session page as it stands: its front matter, its members, its transcript and the owner's section."""
    transcript, waiting = _transcript(vault, page.members)
    # The dates are written bare, as every other page writes them; the serializer would quote a date string.
    head = f"type: {session_type().key}\nstatus: active\ncreated: {page.created}\nupdated: {updated}\n"
    fields: dict[str, Any] = {
        "sources": [_link(member) for member in page.members],
        "alignment": page.alignment,
        "track_offsets": {member.track: member.offset for member in page.members},
        "tags": [],
    }
    members = "\n".join(
        f"- {member.track}: [[{member.page.removesuffix('.md')}]]"
        + (" (no timed transcript yet)" if member.track in waiting else "")
        for member in page.members
    )
    alignment = (
        _ASSUMED_NOTE if page.alignment == ASSUMED_ALIGNMENT else f"The recordings are aligned by `{page.alignment}`."
    )
    owner = page.owner or "\n"
    body = (
        f"\n# {page.title}\n\n{alignment}\n\n## Members\n\n{members}\n\n## Transcript\n\n"
        f"{transcript}\n\n{OWNER_SENTINEL}{owner}"
    )
    return render_frontmatter_document(head + serialize_frontmatter(fields), body)


def render_current(vault: Path, before: str, page: Page, *, today: str | None = None) -> str:
    """`page` rendered again; the page keeps its `updated` date when nothing else changed."""
    same_day = render(vault, page, updated=page.updated)
    return before if same_day == before else render(vault, page, updated=today or date.today().isoformat())


def _check_title(title: object) -> str:
    if not isinstance(title, str) or not title.strip() or len(title.splitlines()) != 1:
        raise refuse("SESSION_TITLE_INVALID", "title must be one line of text")
    if len(title.strip()) > MAX_TITLE_CHARS:
        raise refuse("SESSION_TITLE_INVALID", f"title is at most {MAX_TITLE_CHARS} characters")
    return title.strip()


def _check_tracks(members: Mapping[str, str]) -> None:
    if not 2 <= len(members) <= MAX_MEMBERS:
        raise refuse("SESSION_MEMBERS_INVALID", f"a session groups 2 to {MAX_MEMBERS} recordings; this call has {len(members)}")
    for track in members:
        problem = speaker_names.name_problem(track, "a track name")
        if problem is None and "|" in track:
            problem = "a track name must not hold |, which a member link uses"
        if problem is not None:
            raise refuse("SESSION_TRACK_INVALID", f"track {track!r}: {problem}")


def _start(vault: Path, source: speech_attach.Source) -> speech_tracks.Start | None:
    """When the recording began, as its own file publishes it (hydrating an evicted original first)."""
    try:
        with media_store.logical_view(vault, source.binary) as view:
            return speech_tracks.read_published(Path(view)).start
    except media_store.MediaStoreError as error:
        raise refuse(error.code, error.reason) from error


def page_path(vault: Path, title: str) -> Path:
    return vault / managed_join(f"{session_type().folder}/{slugify_title(title)}.md")


def group(
    vault_root: Path,
    members: Mapping[str, str],
    title: str,
    *,
    commit_guard: Callable[[], AbstractContextManager[object]],
) -> dict[str, Any]:
    """Group the recordings that `members` maps from track name to path as the session `title`.

    A call with the same title regroups: it replaces the members and keeps the owner's section. Every
    refusal comes before the first write, and a repeat of an applied call writes nothing.
    """
    vault = Path(vault_root).resolve()
    title = _check_title(title)
    _check_tracks(members)
    sources = [(track, speech_attach.resolve(vault, path)) for track, path in members.items()]
    if len({source.relative for _, source in sources}) != len(sources):
        raise refuse("SESSION_MEMBERS_INVALID", "each track needs its own recording; two tracks name the same one")
    alignment, offsets = align([_start(vault, source) for _, source in sources])
    target = page_path(vault, title)
    relative = target.relative_to(vault).as_posix()
    if access.access_tier(vault, relative) in {access.TIER_EXCLUDED, access.TIER_READONLY}:
        raise refuse("MEDIA_PATH_ACCESS_DENIED", f"the session page is not writable under _access.yaml: {relative}")
    grouped = tuple(
        Member(track, source.sidecar_relative, offset) for (track, source), offset in zip(sources, offsets, strict=True)
    )

    def unit(commit: Callable[[Sequence[PlannedWrite]], None]) -> dict[str, Any]:
        try:
            before, guard = read_guarded_text(vault, target)
        except (OSError, UnicodeError):
            before, guard = None, None
        earlier = read_page(before) if before is not None else None
        if before is not None and earlier is None:
            raise refuse(
                "SESSION_PAGE_TAKEN",
                f"{relative} exists and is not a session page that speech wrote; give the session another title",
            )
        page = Page(
            title, earlier.created if earlier else date.today().isoformat(),
            earlier.updated if earlier else date.today().isoformat(), alignment, grouped, earlier.owner if earlier else "",
        )
        content = render_current(vault, before or "", page)
        if content == before:
            return _reply(relative, page, outcome="unchanged")
        commit([
            PlannedWrite(
                target, content, guard=guard,
                expected_hash=MISSING_CONTENT_HASH if before is None else content_hash(before),
                ensure_directories=(target.parent,),
            )
        ])
        return _reply(relative, page, outcome="regrouped" if earlier else "grouped")

    return speech_attach.commit_under_guard(vault, commit_guard, "media_group_session_commit", unit)


def _reply(relative: str, page: Page, *, outcome: str) -> dict[str, Any]:
    return {
        "operation": GROUP_OPERATION,
        "outcome": outcome,
        "session_path": relative,
        "alignment": page.alignment,
        "members": [{"track": member.track, "path": member.page, "offset_seconds": member.offset} for member in page.members],
    }


def sessions_of(vault: Path, changed: Sequence[Path | str]) -> list[str]:
    """The session pages that link any of the `changed` Source pages, from the inbound-link index.

    A session page lives in the session type's folder, so a page elsewhere that cites a member is not one.
    """
    pages = [Path(item).relative_to(vault).as_posix() if Path(item).is_absolute() else str(item) for item in changed]
    folder = managed_join(f"{session_type().folder}/")
    found = find_inbound_wikilinks_many(vault, [page.removesuffix(".md") for page in pages])
    return sorted({link.path for links in found.values() for link in links if link.path.startswith(folder)})


def refresh_published(
    vault_root: Path,
    changed: Sequence[Path | str],
    *,
    commit_guard: Callable[[], AbstractContextManager[object]],
) -> dict[str, list[str]]:
    """Render again the session pages of the members whose Source pages just changed.

    The member's change is already committed, so a refresh that cannot run never undoes it: the pages it
    could not render again come back as `stale`, and a repeat of `group-session` renders them again.
    """
    vault = Path(vault_root).resolve()
    found = sessions_of(vault, changed)
    if not found:
        return {}
    updated: list[str] = []

    def unit(commit: Callable[[Sequence[PlannedWrite]], None]) -> None:
        writes = []
        for relative in found:
            target = vault / relative
            before, guard = read_guarded_text(vault, target)
            page = read_page(before)
            if page is None:
                continue  # a page of another origin that cites a member, or one the owner took over
            content = render_current(vault, before, page)
            if content != before:
                writes.append(PlannedWrite(target, content, guard=guard, expected_hash=content_hash(before)))
                updated.append(relative)
        if writes:
            commit(writes)

    try:
        speech_attach.commit_under_guard(vault, commit_guard, "media_session_refresh_commit", unit)
    except Exception:  # noqa: BLE001 - the member's change is committed; a refresh that fails is reported, never raised over it
        log.warning("session refresh failed for %s", found, exc_info=True)
        return {"stale": found}
    return {"updated": updated} if updated else {}
