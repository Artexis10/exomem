"""A domain pack, read and checked.

A pack is YAML data under ``packs/``: entity types with their attribute schemas, event types, the
shape of the contexts and audiences, and which document carries which kind of statement. This module
turns that file into records and refuses what it cannot build, by name, as the catalogue loader
does. It names no entity type, attribute, volatility class or statement kind: every word comes from
the pack file or from a registry that the pack file is checked against.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any

import yaml

from . import names
from .core import CONTEXT_PARENT, SUPPORT, Vocabulary
from .loader import Refusal

PACKS_DIR = Path(__file__).resolve().parent / "packs"

# The fields that each part of a pack file may hold. A field that is not listed is refused.
_FIELDS = {
    "pack": (
        "schema_version",
        "pack",
        "describes",
        "volatility_windows",
        "user",
        "entity_types",
        "event_types",
        "contexts",
        "audiences",
        "facts",
        "artefacts",
    ),
    "entity type": ("id", "holder", "names", "count", "attributes"),
    "attribute": ("id", "label", "paraphrases", "volatility", "value"),
    "event type": ("id", "label", "names", "count", "value"),
    "contexts": ("names", "roots", "children"),
    "audiences": ("count", "size", "from"),
    "facts": ("kind", "share"),
    "artefacts": ("default", "kinds", "operations", "relations"),
    "user": ("names",),
}
# The arguments that each value generator needs.
_GENERATOR_ARGUMENTS = {"integer": ("min", "max"), "choice": ("from",), "holder": ("from",)}
# The value generator whose values are a world's holders. Their ids exist only once a world is built.
HOLDERS = "holder"


class PackRefused(Exception):
    """The pack holds at least one refusal; every one is in ``refusals``."""

    def __init__(self, refusals: list[Refusal]):
        self.refusals = refusals
        super().__init__("\n".join(str(r) for r in refusals))


@dataclass(frozen=True)
class Domain:
    """The values that an attribute or an event may take, as one closed generator gives them."""

    generator: str
    arguments: Mapping[str, Any]

    def values(self) -> tuple[Any, ...]:
        """The values that the pack fixes. A holder domain fixes none: ``Backbone.values`` draws them."""
        if self.generator == "integer":
            return tuple(range(self.arguments["min"], self.arguments["max"] + 1))
        if self.generator == HOLDERS:
            raise ValueError("the values of a holder domain are the holders of a world")
        return tuple(self.arguments["from"])


@dataclass(frozen=True)
class AttributeSchema:
    id: str
    label: str  # how a probe names the attribute
    paraphrases: tuple[str, ...]  # other ways to name it, for text that a probe must not echo
    volatility: str
    domain: Domain


@dataclass(frozen=True)
class EntityType:
    id: str
    holder: bool
    names: str
    count: int
    attributes: tuple[AttributeSchema, ...]


@dataclass(frozen=True)
class EventType:
    id: str
    label: str
    names: str
    count: int
    domain: Domain


@dataclass(frozen=True)
class Pack:
    id: str
    digest: str
    windows: Mapping[str, int]  # days that an observation of a volatility class stays current
    user_names: str
    entity_types: tuple[EntityType, ...]
    event_types: tuple[EventType, ...]
    context_names: str
    context_roots: int
    context_children: int
    audience_count: int
    audience_size: int
    audience_source: str
    fact_kind: str
    fact_share: float
    artefact_default: str
    artefact_kinds: Mapping[str, str]  # each statement kind with the artefact kind that carries it
    operation_kinds: Mapping[str, str]  # each operation with the artefact kind that announces it

    relation_kinds: Mapping[str, str] = dataclass_field(default_factory=dict)

    def artefact_kind(self, statement_kind: str | None) -> str:
        return self.artefact_kinds.get(statement_kind or "", self.artefact_default)

    def operation_kind(self, operation: str) -> str:
        return self.operation_kinds.get(operation, self.artefact_default)


def load_pack(path: Path, vocabulary: Vocabulary | None = None) -> Pack:
    """Read the pack file at ``path`` and check it. Raises ``PackRefused`` on any refusal."""
    raw_bytes = path.read_bytes()
    raw = yaml.safe_load(raw_bytes.decode("utf-8"))
    return check_pack(raw, hashlib.sha256(raw_bytes).hexdigest(), vocabulary or Vocabulary.load())


def check_pack(raw: Mapping[str, Any], digest: str, vocabulary: Vocabulary) -> Pack:
    checker = _Checker(raw, vocabulary)
    pack = checker.build(digest)
    if checker.refusals:
        raise PackRefused(checker.refusals)
    return pack


class _Checker:
    def __init__(self, raw: Mapping[str, Any], vocabulary: Vocabulary):
        self.raw, self.vocabulary = raw, vocabulary
        self.refusals: list[Refusal] = []
        self.implemented = set(names.implemented()["name_spaces"])

    def refuse(self, code: str, item: str, detail: str) -> None:
        self.refusals.append(Refusal(code, item, detail))

    def fields(self, item: str, part: str, body: Mapping[str, Any]) -> None:
        for field in body:
            if field not in _FIELDS[part]:
                self.refuse("unknown-field", item, f"`{field}` is no field of a {part}")

    def space(self, item: str, value: Any) -> str:
        if value not in self.vocabulary.name_spaces or value not in self.implemented:
            self.refuse("undeclared-value", item, f"`{value}` is no declared and built name space")
        return value

    def count(self, item: str, value: Any) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            self.refuse("invalid-value", item, f"`{value}` is not a count of one or more")
            return 1
        return value

    def domain(self, item: str, body: Any) -> Domain:
        body = body if isinstance(body, Mapping) else {}
        generator = body.get("generator")
        if generator not in self.vocabulary.value_generators:
            self.refuse("undeclared-value", item, f"`{generator}` is no declared value generator")
            return Domain("choice", {"from": ()})
        arguments = {k: v for k, v in body.items() if k != "generator"}
        needed = _GENERATOR_ARGUMENTS[generator]
        if set(arguments) != set(needed):
            self.refuse("invalid-value", item, f"{generator} takes exactly: {', '.join(needed)}")
        elif generator == "integer" and not (
            all(isinstance(arguments[k], int) for k in needed)
            and arguments["min"] <= arguments["max"]
        ):
            self.refuse("invalid-value", item, "integer takes whole numbers with min up to max")
        elif generator == HOLDERS and not isinstance(arguments["from"], str):
            self.refuse("invalid-value", item, "holder takes the id of an entity type")
        elif generator == "choice" and not (
            isinstance(arguments["from"], list)
            and len(set(arguments["from"])) == len(arguments["from"]) > 0
        ):
            self.refuse("invalid-value", item, "choice takes a list of distinct values")
        else:
            return Domain(generator, arguments)
        return Domain("choice", {"from": ()})

    def build(self, digest: str) -> Pack:
        raw = self.raw
        self.fields("pack", "pack", raw)
        windows = dict(raw.get("volatility_windows") or {})
        for volatility, days in windows.items():
            if volatility not in self.vocabulary.volatility_classes:
                self.refuse("undeclared-value", "volatility_windows", f"`{volatility}` is no class")
            self.count(f"window of {volatility}", days)
        user = raw.get("user") or {}
        self.fields("user", "user", user)
        entity_types = tuple(self.entity_type(e) for e in raw.get("entity_types") or ())
        types = {t.id: t for t in entity_types}
        if len(types) != len(entity_types):
            self.refuse("duplicate-id", "entity_types", "an entity type id is used twice")
        for kind in entity_types:
            for schema in kind.attributes:
                if schema.domain.generator == HOLDERS:
                    source = types.get(schema.domain.arguments["from"])
                    if source is None or not source.holder:
                        self.refuse(
                            "not-a-holder",
                            f"entity type {kind.id}, attribute {schema.id}",
                            f"`{schema.domain.arguments['from']}` is no entity type whose entities make statements",
                        )
        event_types = tuple(self.event_type(e) for e in raw.get("event_types") or ())
        if len({t.id for t in event_types}) != len(event_types):
            self.refuse("duplicate-id", "event_types", "an event type id is used twice")
        contexts = raw.get("contexts") or {}
        self.fields("contexts", "contexts", contexts)
        audiences = raw.get("audiences") or {}
        self.fields("audiences", "audiences", audiences)
        source = types.get(audiences.get("from"))
        if source is None:
            self.refuse(
                "unknown-reference", "audiences", f"`{audiences.get('from')}` is no entity type"
            )
        elif not source.holder:
            self.refuse("not-a-holder", "audiences", f"`{source.id}` entities make no statements")
        elif self.count("audiences size", audiences.get("size")) > source.count:
            self.refuse(
                "invalid-value", "audiences", f"size is more than the {source.count} {source.id}"
            )
        facts = raw.get("facts") or {}
        self.fields("facts", "facts", facts)
        share = facts.get("share")
        if not isinstance(share, int | float) or not 0 <= share <= 1:
            self.refuse("invalid-value", "facts", f"share `{share}` is not between 0 and 1")
        self.statement_kind("facts", facts.get("kind"))
        carried = raw.get("artefacts") or {}
        self.fields("artefacts", "artefacts", carried)
        by_kind: dict[str, str] = {}
        for artefact_kind, kinds in (carried.get("kinds") or {}).items():
            for kind in kinds:
                self.statement_kind(f"artefact kind {artefact_kind}", kind)
                if kind in by_kind:
                    self.refuse(
                        "duplicate-id", "artefacts", f"`{kind}` is carried by two artefact kinds"
                    )
                by_kind[kind] = artefact_kind
        operations = dict(carried.get("operations") or {})
        for operation in operations:
            if operation not in self.vocabulary.operations:
                self.refuse("unknown-reference", "artefacts", f"`{operation}` is no operation")
        relations = dict(carried.get("relations") or {})
        # Context parent and support are closed source forms; defeat modes are schema values.
        for relation in relations:
            if relation not in {CONTEXT_PARENT, SUPPORT, *self.vocabulary.defeat_modes}:
                self.refuse(
                    "unknown-reference", "artefacts", f"`{relation}` is no source relation form"
                )
        return Pack(
            id=raw.get("pack", ""),
            digest=digest,
            windows=windows,
            user_names=self.space("user", user.get("names")),
            entity_types=entity_types,
            event_types=event_types,
            context_names=self.space("contexts", contexts.get("names")),
            context_roots=self.count("contexts roots", contexts.get("roots")),
            context_children=self.count("contexts children", contexts.get("children")),
            audience_count=self.count("audiences count", audiences.get("count")),
            audience_size=self.count("audiences size", audiences.get("size")),
            audience_source=audiences.get("from", ""),
            fact_kind=facts.get("kind", ""),
            fact_share=float(share) if isinstance(share, int | float) else 0.0,
            artefact_default=carried.get("default", ""),
            artefact_kinds=by_kind,
            operation_kinds=operations,
            relation_kinds=relations,
        )

    def statement_kind(self, item: str, kind: Any) -> None:
        if kind not in self.vocabulary.commitments:
            self.refuse("unknown-reference", item, f"`{kind}` is no statement kind")

    def entity_type(self, body: Mapping[str, Any]) -> EntityType:
        item = f"entity type {body.get('id')}"
        self.fields(item, "entity type", body)
        if not isinstance(body.get("holder"), bool):
            self.refuse("invalid-value", item, "`holder` is true or false")
        schemas = tuple(self.attribute(item, a) for a in body.get("attributes") or ())
        if len({s.id for s in schemas}) != len(schemas):
            self.refuse("duplicate-id", item, "an attribute id is used twice")
        return EntityType(
            body.get("id", ""),
            body.get("holder") is True,
            self.space(item, body.get("names")),
            self.count(item, body.get("count")),
            schemas,
        )

    def attribute(self, owner: str, body: Mapping[str, Any]) -> AttributeSchema:
        item = f"{owner}, attribute {body.get('id')}"
        self.fields(item, "attribute", body)
        volatility = body.get("volatility")
        if volatility not in self.vocabulary.volatility_classes:
            self.refuse("undeclared-value", item, f"`{volatility}` is no declared volatility class")
        paraphrases = tuple(body.get("paraphrases") or ())
        if len(set(paraphrases)) != len(paraphrases) or body.get("label") in paraphrases:
            self.refuse(
                "duplicate-id", item, "a paraphrase repeats the label or another paraphrase"
            )
        return AttributeSchema(
            body.get("id", ""),
            body.get("label", ""),
            paraphrases,
            volatility,
            self.domain(item, body.get("value")),
        )

    def event_type(self, body: Mapping[str, Any]) -> EventType:
        item = f"event type {body.get('id')}"
        self.fields(item, "event type", body)
        return EventType(
            body.get("id", ""),
            body.get("label", ""),
            self.space(item, body.get("names")),
            self.count(item, body.get("count")),
            self.domain(item, body.get("value")),
        )
