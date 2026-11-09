# Design

## Context

The registry marks one core relation `generic: true`. Code reads that attribute and never compares a relation key.
`relation_barrier.evaluate` runs before commit on every live governed write. Until now it refused only edges that touched an entity and only when a fitting specific relation existed.

## Decisions

### 1. A write that adds a generic relation is refused, for every pair

The barrier judges the edges that this call adds, as the census does: an edge present in the page before the edit, or carried by a successor from its predecessor, is not added.
It covers the operations `create`, `replacement`, `tier2_create`, `edit`, `observe`, `tier2_overwrite` and `tier2_append`.
A page that is not live passes, and so does `adoption_compile`, whose bulk import keeps its own audit.
Identical bullets in one write count once, so one answer settles them all.

What the control prevents: a new relation that says two pages are related and nothing more.
What it costs when it fires wrongly: a writer who needs a generic link writes a plain link instead.
A plain link is already the honest statement that two pages are connected and nothing is known about how.
The writer pays one extra call. The refusal gives the exact call.
The refusal is not fail-closed against an unexpected state: it fires on a registry attribute of the relation the writer chose.

### 2. No answer keeps the generic relation

The earlier barrier let a writer keep a generic edge with `relation_disposition="generic"`, a recorded reason and a retry hash.
That path is removed, with the `reason` on a `create-entity` relation item and the per-edge reason bookkeeping.
Agents used it, and a recorded reason does not make an edge specific.
A `create-entity` relation item that carries a `reason` is an invalid item.
The error code is `GENERIC_RELATION_REFUSED`.

### 3. The refusal hands over a ranked menu and three exact routes

One producer, `relation_options.ranked_options`, builds the menu for the refusal and for the relation generators.
A relation is admissible when it is active, not generic, allowed in the page's scope, and its declared endpoints fit both pages where it declares any.
A relation that declares no endpoints, and has no parent that does, is admissible for every pair.
Admissible relations rank by four keys, so the same edge always gets the same menu:

1. a relation whose own endpoints name both pages comes first;
2. a relation the vault already authors between pages of the same note types, most used first;
3. an extension before a core relation;
4. the registry's order.

The menu carries up to eight relations, each with its key, label, description and parent, and a pointer to the full list.
The usage count in rule 2 reads the authored, specific, non-link relation facts of the corpus that the write already holds.
It is computed in memory on the refusal path only, so no write that adds no generic edge pays for it.
The writer's visibility bounds it: a fact counts only when both of its pages are visible to the writer.

Privacy: a target that the writer cannot see, or that does not resolve, never changes the menu.
The ranking then uses the writer's own page alone and offers no relation that needs the target's type.
A withheld target therefore produces the same refusal as an absent one.

The refusal names three routes with their exact calls:

- retry with a specific relation, spelled for the writer that was called (a `## Relations` bullet, or a `relations` item for the link writer);
- propose a new relation type: the `propose-relation` call, then the `save-relations` call that the served surface already offers;
- as a last resort, the plain link, found through the registry's `wikilink` origin and never a literal key.

### 4. The product's own writers stop minting the generic relation

- Bare `connections` on `create-entity` render as plain links.
- A relation candidate that only observes similarity or co-participation (shared source, shared question, shared resolution target, embedding proximity) shows no relation.
  Its public item carries `relation_type: null`, no bullet, the ranked options and the plain link. Accepting it needs `requested_relation` naming one of them.
  The internal candidate keeps the generic marker so that review ids and fingerprints of existing decisions do not move.
- Studio sends a chosen relation or the plain link, never the generic relation.
- The `upkeep_link` dreamer family keeps offering such a candidate, because it surfaces links for pages that have none.
  Its accept call carries a `requested_relation` placeholder and a `choose` block with the ranked options and the plain link, so the agent's choice is part of the call.
  The earlier skip of pairs with fitting options is gone: its reason, that the one-call route could only accept the generic relation, no longer holds.

### 5. The post-commit offers are deleted

`relation_offer.py` offered specific replacements for a generic note-to-note edge after commit.
No write can add that edge now, so the offers are dead code, and so are the tests that exercised only them.

### 6. The census reports how agents choose

The 30-day new-edge cohort adds `specific_share` and `plain_link_share` among newly authored relation edges, meaning a Relations bullet or semantic relation and never a body wikilink.
Each reports numerator, denominator, window, source and coverage, and is unavailable rather than zero when the log cannot classify an edge.
The existing keys stay. The event schema stays at version 1: the `reason` fields remain and new events record none.
The `relation_generic_share` audit check keeps measuring legacy and `adoption_compile` cohorts.

### 7. The scaffold teaches the same rule

The hand-authored skill scaffold says: name a specific relation; if none fits, propose a new type; a plain link only as a last resort. It never suggests the generic relation.
Every derived copy is regenerated through its generator, so the text reaches the plugin packages.

## Out of scope

The registry schema, `relations.yaml` and relation family handling belong to `make-epistemic-language-dynamic`.
This change reads the existing `generic` attribute and adds one accessor, the plain link's key from the `wikilink` origin.
`qualify_connectivity` is unchanged: a legacy generic edge still counts as connectivity.
