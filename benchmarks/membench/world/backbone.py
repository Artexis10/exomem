"""The backbone of a world: ordinary life in one domain pack.

The backbone is the entities, attributes, events, contexts and audiences that exist before any
thread plants a phenomenon. A recipe's roles bind to these. The backbone holds no value and makes no
statement: the planner adds those after it has reserved what the threads need, so that no stable
fact touches a reserved attribute. Names come from the wordbank's partition for the world's split.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from random import Random

from membench.clock import week_date

from .core import World
from .names import NamePool
from .pack import AttributeSchema, EventType, Pack


@dataclass(frozen=True)
class Thing:
    id: str
    name: str
    type: str | None  # the pack's entity type; `None` for the user
    holder: bool


@dataclass(frozen=True)
class Property:
    id: str
    entity: str
    schema: AttributeSchema


@dataclass(frozen=True)
class Happening:
    id: str
    name: str
    type: EventType


@dataclass(frozen=True)
class Backbone:
    world: str  # the world's name, which every id begins with
    user: str  # the holder whom the actor works for
    entities: Mapping[str, Thing]
    attributes: Mapping[str, Property]
    events: Mapping[str, Happening]
    contexts: Mapping[str, str | None]  # each context with the context that holds it
    context_names: Mapping[str, str]
    audiences: Mapping[str, frozenset[str]]

    def holders(self) -> list[str]:
        """The holders that a `holder` role may bind to: every holder but the user."""
        return sorted(i for i, e in self.entities.items() if e.holder and i != self.user)

    def things(self) -> list[str]:
        """The entities that statements are about and that make none."""
        return sorted(i for i, e in self.entities.items() if not e.holder)

    def attributes_of(self, entity: str) -> list[str]:
        return sorted(i for i, a in self.attributes.items() if a.entity == entity)

    def children_of(self, context: str) -> list[str]:
        return sorted(i for i, parent in self.contexts.items() if parent == context)

    def declare(self, world: World) -> None:
        """Put the backbone's entities, attributes, events, contexts and audiences into a world."""
        start = week_date(0)
        for entity in self.entities.values():
            world.add_entity(entity.id, entity.name, start, holder=entity.holder)
        for attribute in self.attributes.values():
            world.add_attribute(attribute.id, attribute.entity, attribute.schema.volatility)
        for event in self.events:
            world.add_event(event)
        for context, parent in self.contexts.items():
            world.add_context(context, parent)
        for audience, holders in self.audiences.items():
            world.add_audience(audience, holders)


def build_backbone(pack: Pack, world: str, pool: NamePool, rng: Random) -> Backbone:
    """Build the backbone of world ``world`` from a pack, drawing names from ``pool``."""
    entities: dict[str, Thing] = {}
    attributes: dict[str, Property] = {}
    user = f"{world}.user"
    entities[user] = Thing(user, pool.take(pack.user_names, rng), None, True)
    for kind in pack.entity_types:
        for n in range(kind.count):
            id_ = f"{world}.{kind.id}{n:02d}"
            entities[id_] = Thing(id_, pool.take(kind.names, rng), kind.id, kind.holder)
            for schema in kind.attributes:
                attributes[f"{id_}.{schema.id}"] = Property(f"{id_}.{schema.id}", id_, schema)
    events: dict[str, Happening] = {}
    for kind in pack.event_types:
        for n in range(kind.count):
            id_ = f"{world}.{kind.id}{n:02d}"
            events[id_] = Happening(id_, f"{pool.take(kind.names, rng)} {kind.label}", kind)
    contexts: dict[str, str | None] = {}
    context_names: dict[str, str] = {}
    for root in range(pack.context_roots):
        root_id = f"{world}.ctx{root}"
        contexts[root_id] = None
        context_names[root_id] = pool.take(pack.context_names, rng)
        for child in range(pack.context_children):
            child_id = f"{root_id}.{child}"
            contexts[child_id] = root_id
            context_names[child_id] = pool.take(pack.context_names, rng)
    members = sorted(i for i, e in entities.items() if e.type == pack.audience_source)
    audiences = {
        f"{world}.aud{n}": frozenset(rng.sample(members, pack.audience_size))
        for n in range(pack.audience_count)
    }
    return Backbone(world, user, entities, attributes, events, contexts, context_names, audiences)
