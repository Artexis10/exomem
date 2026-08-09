"""Neutral observable-state schema for the Epistemic State Bench.

Assertions never touch provider internals or answer prose. They run over an
:class:`EpistemicStateSnapshot`: a product-neutral projection of observable
state, produced by a read-only projector from documented surfaces only.

The vocabulary is deliberately external prior art rather than any product's
folder names — PROV-O for provenance (``cites``, ``raw_source`` vs derived
items), AGM belief revision for supersession/retraction (``revision_of``,
``current``, ``retired_reason``), and Toulmin for claim/warrant/backing
(``claim`` vs ``evidence`` vs ``hypothesis``).

Two rules make the schema honest rather than merely descriptive:

- Every field a projector maps carries a :class:`FieldDeclaration` whose
  ``evidence`` cites competitor-authored material (a ``path:line`` in the
  repository, or a URL). A declaration without evidence fails validation, so an
  undocumented mapping cannot silently become a score.
- Absence is typed. ``absent_by_design`` (the product has no such concept),
  ``available_via:<mechanism>`` (reachable through a documented alternate
  surface), and ``unavailable`` (the projector cannot observe it) are three
  different facts, and only the first is ever allowed to become
  ``not_applicable``.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    """Pydantic v2 strict + ``extra="forbid"``, as the protocol substrate uses."""

    model_config = ConfigDict(extra="forbid", strict=True)


#: Kinds an item may take. ``container`` covers queues, collections, and
#: index-like artifacts that hold other items without asserting anything.
ItemKind = Literal[
    "raw_source",
    "evidence",
    "claim",
    "decision",
    "hypothesis",
    "open_question",
    "derived_inference",
    "container",
]

#: Three-valued currency: a product that never says is not a product that says no.
Currency = Literal["yes", "no", "undeclared"]

Authorship = Literal["human", "agent", "engine_inferred"]

LocatorKind = Literal["file", "api"]

#: Provider-neutral edge predicates. Kept small and mapped from prior art
#: rather than mirroring any product's relation registry wholesale.
RelationPredicate = Literal[
    "cites",
    "supports",
    "contradicts",
    "supersedes",
    "derived_from",
    "evidenced_by",
    "raises_question",
    "answers",
    "depends_on",
    "relates_to",
]

#: The fields a projector may declare. Assertions gate on these names, so the
#: set is closed: an assertion cannot invent a capability question at runtime.
DECLARABLE_FIELDS: tuple[str, ...] = (
    "kind",
    "current",
    "revision_of",
    "prior_revision",
    "cites",
    "contradicts",
    "supports",
    "review_state",
    "uncertainty",
    "open_question",
    "authored_by",
    "locator",
    "external_edit",
    "export",
)

_DECLARATION_STATUS_RE = re.compile(
    r"^(?:declared|absent_by_design|unavailable|available_via:[a-z0-9][a-z0-9_.\-]*)$"
)

#: Evidence that is a URL rather than a repository citation.
_URL_RE = re.compile(r"^https?://\S+$")

#: ``path/to/file.md:123`` — the citation form projector tests dereference.
_PATH_LINE_RE = re.compile(r"^(?P<path>[^\s:]+(?:/[^\s:]+)*):(?P<line>\d+)")


class StateItem(StrictModel):
    """One observable unit of state.

    ``current`` is the AGM-style currency flag, ``revision_of`` the backward
    supersession edge, and ``cites`` the PROV-O style evidence hop. ``raw``
    carries whatever the projector read verbatim (stringified so the snapshot
    round-trips through JSON without a schema of its own).
    """

    id: str = Field(min_length=1)
    kind: ItemKind
    title: str = ""
    text: str = ""
    current: Currency = "undeclared"
    retired_reason: str | None = None
    revision_of: str | None = None
    revision_chain_id: str | None = None
    revision_index: int | None = Field(default=None, ge=0)
    cites: tuple[str, ...] = ()
    supports: tuple[str, ...] = ()
    contradicts: tuple[str, ...] = ()
    review_state: str | None = None
    authored_by: Authorship | None = None
    uncertainty: str | None = None
    locator: str | None = None
    locator_kind: LocatorKind | None = None
    observed_at: str | None = None
    raw: dict[str, str] = Field(default_factory=dict)


class Relation(StrictModel):
    """A typed edge between two item ids.

    ``declared`` separates an edge the provider itself records from one the
    projector inferred; an inferred edge is still reportable, but a scenario
    may insist on a declared one.
    """

    subject: str = Field(min_length=1)
    predicate: RelationPredicate
    object: str = Field(min_length=1)
    declared: bool = True


class FieldDeclaration(StrictModel):
    """A projector's capability claim about one snapshot field.

    ``evidence`` is required and must be non-blank: the fairness contract is
    that every mapping is traceable to competitor-authored material.
    ``marketing_claim``, when set, is the product's own published claim to the
    property — it converts a would-be ``not_applicable`` into a ``fail``
    (PREREGISTRATION §4, claim-conditioned N/A).
    """

    field: str = Field(min_length=1)
    status: str
    evidence: str = Field(min_length=1)
    marketing_claim: str | None = None

    @field_validator("status")
    @classmethod
    def _status_vocabulary(cls, value: str) -> str:
        if not _DECLARATION_STATUS_RE.fullmatch(value):
            raise ValueError(
                "status must be declared, absent_by_design, unavailable, "
                "or available_via:<mechanism>"
            )
        return value

    @field_validator("evidence")
    @classmethod
    def _evidence_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a field declaration requires competitor-authored evidence")
        return value

    @field_validator("marketing_claim")
    @classmethod
    def _claim_is_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("marketing_claim must be a citable claim or omitted")
        return value

    @property
    def mechanism(self) -> str | None:
        """The documented alternate surface, for ``available_via:<mechanism>``."""

        prefix = "available_via:"
        return self.status[len(prefix) :] if self.status.startswith(prefix) else None

    @property
    def observable(self) -> bool:
        """True when the field can be evaluated at all (directly or via a mechanism)."""

        return self.status == "declared" or self.mechanism is not None


class ProjectorMeta(StrictModel):
    """Published projector size and surface count.

    Gross asymmetry between projectors is itself a reportable finding, which is
    only possible if every snapshot carries these numbers.
    """

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    author: str = Field(min_length=1)
    endpoints_used: tuple[str, ...]
    loc: int = Field(ge=0)


class EpistemicStateSnapshot(StrictModel):
    """State as observed at one phase, by one projector, for one provider.

    ``taken_at`` is always caller-supplied. Nothing in this package reads the
    clock: a snapshot must be reproducible from its inputs alone.
    """

    provider: str = Field(min_length=1)
    variant: str = "native"
    phase: str = Field(min_length=1)
    taken_at: str = Field(min_length=1)
    items: tuple[StateItem, ...] = ()
    relations: tuple[Relation, ...] = ()
    declarations: tuple[FieldDeclaration, ...] = ()
    projector: ProjectorMeta
    completeness_notes: str = ""

    @model_validator(mode="after")
    def _ids_are_unique(self) -> "EpistemicStateSnapshot":
        seen: set[str] = set()
        for item in self.items:
            if item.id in seen:
                raise ValueError(f"duplicate item id in snapshot: {item.id}")
            seen.add(item.id)
        declared: set[str] = set()
        for declaration in self.declarations:
            if declaration.field in declared:
                raise ValueError(f"duplicate field declaration: {declaration.field}")
            declared.add(declaration.field)
        return self

    def item(self, item_id: str) -> StateItem | None:
        for candidate in self.items:
            if candidate.id == item_id:
                return candidate
        return None

    def declaration(self, field: str) -> FieldDeclaration | None:
        for candidate in self.declarations:
            if candidate.field == field:
                return candidate
        return None

    def items_by_id(self) -> dict[str, StateItem]:
        return {item.id: item for item in self.items}


def parse_evidence_citation(evidence: str) -> tuple[str, int] | None:
    """Split ``path/to/file.md:123`` into its parts; ``None`` for URLs/prose."""

    if _URL_RE.match(evidence.strip()):
        return None
    match = _PATH_LINE_RE.match(evidence.strip())
    if match is None:
        return None
    return match.group("path"), int(match.group("line"))
