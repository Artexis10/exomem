# Proposal

## Why

A new relation between two notes could always be the generic relation, because nothing on the write path refused it.
The earlier barrier refused only a generic edge that touched an entity, and only when a relation with declared endpoints fitted both pages.
No core relation declares endpoints for two plain notes, so a note-to-note generic edge never met a refusal.
The write also offered a way to keep the generic edge: a recorded reason.
Agents took it, and the vault filled with relations that say two pages are related and nothing else.

The aim is that agents name a specific relation in at least 95% of the relation edges they author.
A reason that lets the agent keep the generic edge cannot reach that aim.
A refusal that also hands the agent the answer can: a ranked menu of specific relations, a route to propose a new relation type, and a plain link as the last resort.

## What Changes

- Refuse every write that adds a generic relation, for every pair of pages, note to note included.
  The refusal carries up to eight ranked specific relations and the exact calls for three routes: retry with a specific relation, propose a new relation type through the existing registry route, or write a plain link.
- Remove the escape that kept a generic edge on a recorded reason: `relation_disposition="generic"`, the reason on a `create-entity` relation item, and the retry hash.
  The refusal code `GENERIC_RELATION_NEEDS_REASON` becomes `GENERIC_RELATION_REFUSED`.
- Rank the menu from the registry's own data: relations whose endpoints name both pages, then relations the vault already authors between pages of the same note types, then extensions before core, then registry order.
  One producer serves the refusal and the relation generators.
- Stop the product's own writers minting the generic relation: bare `connections` render as plain links, and a relation candidate that observes only similarity or co-participation names no relation, so accepting it needs a chosen relation or the plain link.
- Delete the post-commit replacement offers for note-to-note generic edges, which the refusal makes unreachable.
- Report `specific_share` and `plain_link_share` in the census cohort of new authored edges, unavailable rather than zero when the log cannot say.
- Teach the same rule in the shipped skill scaffold and its generated copies: name a specific relation; if none fits, propose a new type; a plain link only as a last resort.

The generic relation stays registered with `generic: true`, so existing generic edges still read, index and count as connectivity.
Edits that leave an existing generic edge alone are never refused.
Genericity is read from the registry attribute, never from a relation key.

## Impact

Capabilities:

- `relation-vocabulary-evolution`: the generic relation is refused for new authored edges; the honest outcomes are a specific relation, a proposed relation, a plain link or no edge; the barrier and the ranked menu; the generators leave relation choice to the agent (moved here from `promote-referents-without-nudges`).
- `markdown-relations`: bare `connections` render as plain links.
- `graph-semantic-integrity`: similarity-only and co-participation candidates carry no relation and need a choice to accept.
- `semantic-write-contract`: a newly added generic relation is refused; legacy all-generic pages keep the non-blocking advisory.
- `epistemic-relation-registry`: the new authored-edge census cohort reports `specific_share` and `plain_link_share` (moved here from `promote-referents-without-nudges`).

Code: `relation_barrier.py`, `relation_options.py`, `semantic_writes.py`, `link.py`, `relation_queue.py`, `relation_census.py`, `relation_registry.py` (additive plain-link accessor only), the Studio relation review view, the scaffold skill and its generated copies.
`promote-referents-without-nudges` gives up the requirements that this change supersedes.
