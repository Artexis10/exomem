"""Typed endpoint declarations of relation definitions.

A relation may declare the pages it joins: `endpoints: {subject: [...], object: [...]}`.
Each token is `entity:<type>`, `entity:*` or `role:<note role>`. This module fixes only that
grammar. The vocabulary a token names stays data: entity types come from the entity-type
registry (a type covers its descendants) and roles from the note-type schema. It parses a
declaration, checks that an extension stays inside its parent's, and matches a page against
one. It never reads a relation key; a caller passes the declaration it resolved.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .entity_types import EntityTypeRegistry

#: The two sides of a declaration. The grammar fixes them; they are not vocabulary.
SIDES = ("subject", "object")
# The grammar's own words, not vocabulary: a token is `<namespace>:<name>`.
ENTITY = "entity"
ROLE = "role"
#: `entity:*` names every entity page.
ANY = "*"
#: The note-type role of entity pages (`note_types.ROLES`); `role:entity` and `entity:*` name the same pages.
ENTITY_ROLE = "entity"


@dataclass(frozen=True, slots=True)
class Endpoints:
    """The pages a relation joins: families for the subject and for the object."""

    subject: tuple[str, ...]
    object: tuple[str, ...]

    def side(self, name: str) -> tuple[str, ...]:
        return self.subject if name == "subject" else self.object

    def as_dict(self) -> dict[str, list[str]]:
        return {"subject": list(self.subject), "object": list(self.object)}


@dataclass(frozen=True, slots=True)
class PageKind:
    """What a page is for endpoint matching: its note-type role and, for an entity, its type."""

    role: str | None
    #: The registry id of an entity page's type; None when the page names no registered type.
    entity_type: str | None = None


def split(token: str) -> tuple[str, str]:
    """A token's namespace and name; the caller has parsed it."""
    namespace, _, name = token.partition(":")
    return namespace, name


def parse(value: object) -> tuple[Endpoints | None, list[str]]:
    """The declaration a document holds and what makes it unusable (one detail per problem)."""
    from .note_types import ROLES

    if not isinstance(value, Mapping):
        return None, ["endpoints must be an object with subject and object lists"]
    problems: list[str] = []
    extra = sorted(str(key) for key in set(value) - set(SIDES))
    if extra:
        problems.append(f"endpoints has only subject and object, not {', '.join(extra)}")
    sides: dict[str, tuple[str, ...]] = {}
    for side in SIDES:
        raw = value.get(side)
        if not isinstance(raw, list) or not raw or not all(isinstance(item, str) for item in raw):
            problems.append(f"endpoints.{side} must be a non-empty list of strings")
            continue
        tokens = tuple(dict.fromkeys(item.strip() for item in raw))
        for token in tokens:
            namespace, name = split(token)
            if namespace == ROLE and name in ROLES:
                continue
            if namespace == ENTITY and name:
                continue
            problems.append(
                f"endpoints.{side}: {token!r} is not entity:<type>, entity:* or "
                f"role:<one of {', '.join(sorted(ROLES))}>"
            )
        sides[side] = tokens
    if problems:
        return None, problems
    return Endpoints(subject=sides["subject"], object=sides["object"]), []


def _every_entity(namespace: str, name: str) -> bool:
    return (namespace == ENTITY and name == ANY) or (namespace == ROLE and name == ENTITY_ROLE)


def covers(wider: str, narrower: str, types: EntityTypeRegistry) -> bool:
    """Whether every page `narrower` names is a page `wider` names."""
    if wider == narrower:
        return True
    wider_namespace, wider_name = split(wider)
    narrower_namespace, narrower_name = split(narrower)
    if _every_entity(wider_namespace, wider_name):
        return narrower_namespace == ENTITY or _every_entity(narrower_namespace, narrower_name)
    if wider_namespace == ENTITY and narrower_namespace == ENTITY and narrower_name != ANY:
        return types.covers(wider_name, narrower_name)
    return False


def fits(tokens: Iterable[str], kind: PageKind, types: EntityTypeRegistry) -> bool | None:
    """Whether a page of `kind` is named by `tokens`; None when its type cannot decide it.

    An entity page whose type is not registered, or a token naming a type that is no
    longer registered, leaves the answer open instead of reporting a mismatch.
    """
    answer: bool | None = False
    for token in tokens:
        namespace, name = split(token)
        if _every_entity(namespace, name):
            if kind.role == ENTITY_ROLE:
                return True
        elif namespace == ROLE:
            if kind.role == name:
                return True
        elif kind.role == ENTITY_ROLE:
            if kind.entity_type is None or types.resolve(name) is None:
                answer = None
            elif types.covers(name, kind.entity_type):
                return True
    return answer


def page_kind(role: str | None, entity_type: Any, types: EntityTypeRegistry) -> PageKind:
    """The kind of a page with this note-type role and `entity_type` frontmatter value."""
    if role != ENTITY_ROLE:
        return PageKind(role)
    resolved = types.resolve(str(entity_type or "")) if entity_type else None
    return PageKind(role, resolved.id if resolved is not None else None)
