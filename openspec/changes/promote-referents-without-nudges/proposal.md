# Proposal

## Why

An ordinary conversation names durable things: a person, their dog, a supplement the dog takes.
Today an agent turns them into entities and typed edges only when the user tells it to.
The tools exist, but nothing on the write path asks the agent which durable referents its write names.
The served contract still says that vocabulary writers need confirmation.
Specific relations are never offered when an agent writes `relates_to`.
Activation does not follow ownership, membership or location edges.

The owner decided on 2026-10-10 that creation of entities, types, categories and typed edges is first class, needs no confirmation, and reaches every later session.
This change specifies the slice that delivers that outcome without server-side nudges.

## What Changes

- Add one optional `referents` argument to `remember`, `observe_memory`, `edit_memory` and `capture_source`.
  The agent declares each durable referent with a name, an optional type, optional relations and a verbatim evidence span.
  The server resolves each referent, reuses an existing entity, or creates a new one through the existing entity writer.
- Classify an owner's additive promotions as `proactive_capture`: entity type and category saves, relation extension saves, entity creation and agent-authored typed edges.
  Each promotion carries provenance, appears once in the next session's counters, and reverts in one call.
  The confirmation rule in the served contract is replaced.
- Let `create-entity` take typed `connections`, and add one additive `add-relation` leaf for an ordinary typed edge.
- Add relation `label`, `generic` and `endpoints` as registry data.
  When a write authors the generic relation between two entities, the response offers the registered predicates whose endpoints fit.
- Move the built-in traversal profiles into a vocabulary pack and add an `entity` profile.
  Activation expands entity anchors through it.
- Show relations by label, for example `has_pet (a kind of owns)`, through one formatter.
  The namespaced key stays the machine identity.
- Add a `recent_promotions` due-state category and a one-call revert route for each promotion.
- Prove the outcome with an end-to-end replay of a rich turn about a person's dog, its supplement and its diet, plus negative twins and boundary tests.

## Capabilities

### New Capabilities

- `referent-promotion`: the declaration channel, resolution before creation, typed edges, provenance, surfacing, revert and disclosure.

### Modified Capabilities

- `delegation-envelope`: an owner's additive promotions follow `proactive_capture`.
- `epistemic-relation-registry`: relation labels, the display formatter, endpoint families and the generic marker.
- `relation-vocabulary-evolution`: a generic edge between entities offers fitting predicates.
- `graph-traversal-profiles`: built-in profiles ship as pack data and include `entity`.
- `context-activation`: an anchor expands through the profile that declares its kind.

## Impact

- Code: `commands.py` (four write commands, `connect_memory`, bootstrap), `link.py`, `curation.py`, `relation_registry.py`, `traversal_profiles.py`, `working_set.py`, `due_state.py`, `mutation_terminal.py`, the core packs, and the scaffold.
- Published surface: four tools gain `referents`; `connect_memory` gains the `add-relation` operation and typed `connections`.
  The tool-surface pin moves once.
  The ChatGPT connector needs a refresh after the release.
- Vaults: no page is rewritten.
  New registry fields are optional, so an older release reads every overlay.
- Supersedes: the confirm-required classification of entity-type saves in the `complete-recurring-entity-lifecycle` envelope delta, and the served text "v1 writers retain confirmation".
  Close-memory-loop task 5.3 and agent-led vocabulary tasks 7.1–7.4 stop gating the owner's promotion.
- Ships before close-memory-loop task 4.3.
  Origin accounting is needed only for recurrence-based promotion, which this change does not add.
