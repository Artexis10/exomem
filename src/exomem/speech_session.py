"""Group the separate recordings of one event as a session, and keep the session's transcript current.

A session page holds one transcript of several recordings, one track for each recording. It is a page
of the note type that the note-type registry marks as the session transcript type (an attribute, never
the entry's key), in that type's folder, and it links each member Source. The link carries the track's
name (`[[Source|Host]]`), so a member, its track and the page's links are one fact that no field can
contradict. The page keeps what speech wrote above `OWNER_SENTINEL` and what the owner wrote below it.

A session page copies its members' text, so it is never more readable than any one member: a member that
a reader of the page could not read is refused at grouping and withheld from the page at every render.

A member's extraction or speaker names change after the grouping, so the session finds its pages through
the inbound-link index (`vault.find_inbound_wikilinks_many`): a changed member lists the pages that link
it, and speech keeps those in the session type's folder. No change reads every session page.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from . import (
    access,
    extraction_parts,
    find_corpus,
    media_store,
    note_types,
    speaker_names,
    speech_attach,
    speech_tracks,
)
from .governance import membership, policy
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
    parse_wikilink,
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
        found = parse_wikilink(item) if isinstance(item, str) else None
        track = found[1] if found else None  # a member link carries its track's name as the alias
        offset = offsets.get(track) if track else None
        if found is None or track is None or isinstance(offset, bool) or not isinstance(offset, int | float):
            return None
        members.append(Member(track, f"{found[0]}.md", float(offset)))
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


WITHHELD_NOTE = "text withheld: this recording is more restricted than this page"


@dataclass(frozen=True)
class _Held:
    """One member as a render reads it: its Source page, and why its text must stay off the page, if it must."""

    member: Member
    content: str | None
    #: What to change so that the page may copy this member's text; None when it may.
    barred: str | None


# Prevents a session page showing a recording's text to a reader who cannot read it; a wrong firing costs the owner a track.
def _barred(
    vault: Path, relative: str, own: frozenset[str] | None, governed: policy.Policy, member: Member, content: str | None,
) -> str | None:
    if content is None:
        return None  # a missing Source page has no text to copy
    if access.access_tier(vault, member.page) == access.TIER_EXCLUDED:
        return f"{member.page} is excluded by _access.yaml: leave it out of the session, or remove the exclusion"
    raw = content.encode("utf-8")
    parsed = find_corpus.parse_page(vault / member.page, 0.0, vault, content=raw)
    try:
        if parsed is None:
            raise membership.MembershipUnresolved("the page cannot be read")
        theirs = membership.evaluate_snapshot(parsed, governed, content_hash=hashlib.sha256(raw).hexdigest())
    except membership.MembershipUnresolved as error:
        return f"who may read {member.page} cannot be decided ({error}): repair that page or the governance policy"
    if own is None:
        return f"who may read {relative} cannot be decided: repair the governance policy"
    if not theirs <= own:
        scopes = ", ".join(sorted(theirs - own))
        return (
            f"{member.page} is in governance scope {scopes}, which {relative} is not in: bring the session page "
            f"into that scope (a scope that selects by path takes {session_type().folder}/**), or leave the recording out"
        )
    return None


def _held(vault: Path, relative: str, page: Page) -> list[_Held]:
    """Each member of `page` with its Source page's text, and whether the page at `relative` may copy it."""
    try:
        own = membership.prospective_scopes(vault, relative, {"type": session_type().key, **_front(page)})
    except membership.MembershipUnresolved:
        own = None
    governed = policy.load(vault)
    held = []
    for member in page.members:
        try:
            content, _guard = read_guarded_text(vault, vault / member.page)
        except (OSError, UnicodeError):
            content = None
        held.append(_Held(member, content, _barred(vault, relative, own, governed, member, content)))
    return held


def _transcript(vault: Path, held: Sequence[_Held]) -> tuple[str, list[str]]:
    """The members' timed lines as one transcript, and the tracks that have no timed line yet.

    Each line keeps its member's time plus the member's offset and takes the track's name for its label,
    before the speaker label the member already carries. A member that the page may not copy adds no line.
    """
    lines: list[tuple[float, float, str, str | None]] = []
    waiting: list[str] = []
    for item in held:
        member = item.member
        if item.barred is not None:
            continue
        text = "" if item.content is None else extraction_parts.whole_text(vault, vault / member.page, item.content)
        found = parse_timed_lines(text)
        if not found:
            waiting.append(member.track)
        for line in found:
            label = member.track if line.speaker is None else f"{member.track}: {line.speaker}"
            lines.append((member.offset + line.ts, 0.0, line.text, label))
    lines.sort(key=lambda line: line[0])  # a stable sort keeps the members' order for lines that start together
    return render_timed_lines(lines), waiting


def _front(page: Page) -> dict[str, Any]:
    """The fields a session page records about its members, which are also the inputs of who may read the page."""
    return {
        "sources": [_link(member) for member in page.members],
        "alignment": page.alignment,
        "track_offsets": {member.track: member.offset for member in page.members},
        "tags": [],
    }


def render(vault: Path, page: Page, held: Sequence[_Held], *, updated: str) -> str:
    """The session page as it stands: its front matter, its members, its transcript and the owner's section.

    A member whose text the page may not copy is withheld, and the member list says so.
    """
    transcript, waiting = _transcript(vault, held)
    # The dates are written bare, as every other page writes them; the serializer would quote a date string.
    head = f"type: {session_type().key}\nstatus: active\ncreated: {page.created}\nupdated: {updated}\n"
    barred = {item.member.track for item in held if item.barred is not None}
    members = "\n".join(
        f"- {member.track}: [[{member.page.removesuffix('.md')}]]"
        + (f" ({WITHHELD_NOTE})" if member.track in barred else " (no timed transcript yet)" if member.track in waiting else "")
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
    return render_frontmatter_document(head + serialize_frontmatter(_front(page)), body)


def render_current(vault: Path, before: str, page: Page, held: Sequence[_Held], *, today: str | None = None) -> str:
    """`page` rendered again from `held`; the page keeps its `updated` date when nothing else changed."""
    same_day = render(vault, page, held, updated=page.updated)
    return before if same_day == before else render(vault, page, held, updated=today or date.today().isoformat())


def _refuse_barred(held: Sequence[_Held]) -> None:
    for item in held:
        if item.barred is not None:
            raise refuse("SESSION_MEMBER_MORE_RESTRICTED", f"track {item.member.track!r}: {item.barred}")


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
    target = page_path(vault, title)
    relative = target.relative_to(vault).as_posix()
    if access.access_tier(vault, relative) in {access.TIER_EXCLUDED, access.TIER_READONLY}:
        raise refuse("MEDIA_PATH_ACCESS_DENIED", f"the session page is not writable under _access.yaml: {relative}")
    # Asked before `_start` hydrates any original, so a refused call costs none; the same ask under the guard decides the write.
    _refuse_barred(_held(vault, relative, Page(
        title, "", "", ASSUMED_ALIGNMENT, tuple(Member(track, source.sidecar_relative, 0.0) for track, source in sources), "",
    )))
    alignment, offsets = align([_start(vault, source) for _, source in sources])
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
        held = _held(vault, relative, page)
        _refuse_barred(held)
        content = render_current(vault, before or "", page, held)
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
    could not render again come back as `stale`, and a repeat of `group-session` renders them again. A page
    that now withholds a member's text, because the member became more restricted, is also `withheld`.
    """
    vault = Path(vault_root).resolve()
    found = sessions_of(vault, changed)
    if not found:
        return {}
    updated: list[str] = []
    withheld: list[str] = []

    def unit(commit: Callable[[Sequence[PlannedWrite]], None]) -> None:
        writes = []
        for relative in found:
            target = vault / relative
            before, guard = read_guarded_text(vault, target)
            page = read_page(before)
            if page is None:
                continue  # a page of another origin that cites a member, or one the owner took over
            held = _held(vault, relative, page)  # a member that became more restricted loses its text here
            content = render_current(vault, before, page, held)
            if content != before:
                writes.append(PlannedWrite(target, content, guard=guard, expected_hash=content_hash(before)))
                updated.append(relative)
                if any(item.barred is not None for item in held):
                    withheld.append(relative)
        if writes:
            commit(writes)

    try:
        speech_attach.commit_under_guard(vault, commit_guard, "media_session_refresh_commit", unit)
    except Exception:  # noqa: BLE001 - the member's change is committed; a refresh that fails is reported, never raised over it
        log.warning("session refresh failed for %s", found, exc_info=True)
        return {"stale": found}
    if not updated:
        return {}
    return {"updated": updated, "withheld": withheld} if withheld else {"updated": updated}
