"""The owner gate on face data and the `people` restriction of a search (design D8 to D10).

Every face operation and every search with `people` first asks `require_owner`, which decides from the
caller's principal alone, before any face store file is opened. Only the owner with unrestricted
content authority passes: an audience, a scoped connector session and projected or governed retrieval
for another principal are refused with `FACES_OWNER_ONLY`.

A restriction then reads the face setting (`admit`) and resolves the names to pages (`pages`) with one
indexed name lookup and one indexed page lookup. The server never reads names from the query text: the
calling agent maps "photos of a person" to `people`. Each refusal leads with a sentence the agent can
repeat to the owner and names the fix, and a refused restriction never falls back to a search without it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from . import face_groups, face_store, originals
from .cli_ops import OpError

#: How many named people an `UNKNOWN_PERSON` refusal lists.
LISTED_PEOPLE = 20


def owner_only() -> OpError:
    return OpError(
        "FACES_OWNER_ONLY",
        "Only the owner can search by person or manage faces, so nothing ran.",
        "Search without people. The owner can search by person from their own connection.",
    )


def is_owner(vault_root: Path) -> bool:
    """Whether the caller is the owner with unrestricted content authority. Reads no face store."""
    from .governance import raw_protection
    from .governance.principal import effective_principal

    return raw_protection.has_unrestricted_access(Path(vault_root), effective_principal())


def require_owner(vault_root: Path) -> None:
    """Refuse every caller but the owner with unrestricted content authority, before any face store opens."""
    if not is_owner(vault_root):
        raise owner_only()


def faces_off() -> OpError:
    return OpError(
        "FACES_OFF",
        "Face search is off for this account, so no search by person ran.",
        "Ask the owner whether to turn faces on: faces.set with state on shows what is stored, "
        "and the owner turns faces on with `evermatter faces on`. Or search without people.",
    )


def faces_unavailable(reason: str) -> OpError:
    return OpError(
        "FACES_UNAVAILABLE",
        f"Face search cannot run here ({reason}), so no search by person ran.",
        "Search without people. faces.status says why faces cannot run here and who can change it.",
    )


@dataclass(frozen=True)
class Restriction:
    """An admitted `people` restriction: the folded names and the face index generation it reads."""

    names: tuple[str, ...]
    generation: str


def faces_unreadable(error: sqlite3.DatabaseError) -> OpError:
    """A face store this release cannot read: damaged, or written by a newer release."""
    newer = isinstance(error, face_store.NewerStore)
    cause = "a newer release of Evermatter wrote it" if newer else "it is damaged"
    return OpError(
        "FACES_UNREADABLE",
        f"The face data cannot be read: {cause}, so nothing ran.",
        "Search without people for now. faces.status explains the fix; the owner erases the face data with "
        "faces.delete and turns faces on again.",
        details={"error": str(error)},
    )


def admit(vault_root: Path, people: Sequence[str]) -> Restriction:
    """Check the caller, the face setting and availability for a search with `people`.

    Refuses with `FACES_OWNER_ONLY`, `FACES_OFF` or `FACES_UNAVAILABLE`. The generation keys the find
    cache, so a cached result never outlives a change to faces, groups or names.
    """
    require_owner(vault_root)
    # A deployment that does not run faces says so before the setting: turning faces on would not help.
    refusal = face_store.deployment_refusal()
    if refusal is not None:
        raise faces_unavailable(refusal)
    try:
        setting = face_store.read_setting(vault_root)
    except sqlite3.DatabaseError as error:
        if face_store.is_busy(error):
            raise  # Another connection holds the store: it is readable, and the caller retries.
        raise faces_unreadable(error) from error
    if setting is None or not setting.enabled:
        raise faces_off()
    reason = face_store.unavailable_reason(setting)
    if reason is not None:
        raise faces_unavailable(reason)
    names = tuple(sorted({face_store.fold_name(name.strip()) for name in people}))
    return Restriction(names, setting.generation)


def pages(vault_root: Path, restriction: Restriction, people: Sequence[str]) -> set[str]:
    """The pages with a face of each named person (AND), as search keys them: each picture's own page.

    A video matches through its keyframes and returns as the video's page. Refuses a name no person has
    with `UNKNOWN_PERSON`.
    """
    try:
        found = face_groups.person_pages(vault_root, restriction.names)
    except sqlite3.DatabaseError as error:
        if face_store.is_busy(error):
            raise  # Another connection holds the store: it is readable, and the caller retries.
        raise faces_unreadable(error) from error
    unknown = [name.strip() for name in people if face_store.fold_name(name.strip()) not in found]
    if unknown:
        named = face_groups.named_people(vault_root, limit=LISTED_PEOPLE)
        listed = ", ".join(named) if named else "none yet"
        raise OpError(
            "UNKNOWN_PERSON",
            f"No person is named {', '.join(repr(name) for name in unknown)}, so no search by person ran.",
            f"Use a name the owner gave a face group. Named people: {listed}. "
            "The owner names groups with faces.name; faces.status lists the groups.",
            details={"named_people": named},
        )
    both = set.intersection(*found.values()) if found else set()
    return {originals.sidecar_key(picture) for picture in both}
