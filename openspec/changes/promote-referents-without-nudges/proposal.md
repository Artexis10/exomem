# Proposal

## Why

An ordinary conversation names durable things: a person, their dog, a supplement the dog takes.
Today an agent turns them into entities and typed edges only when the user tells it to.
The compact core already tells the agent to wikilink named identities, and the failure happened under that text.
Nothing on the write path tells the agent that a name it just linked has no entity.
The served contract still says that vocabulary writers and relations need confirmation.
Specific relations are never offered when an agent writes `relates_to`.

The owner decided on 2026-10-10 that creation of entities, types, categories and typed edges is first class, needs no confirmation, and reaches every later session.
This change measures the current behaviour first, then ships the smallest prompt, and adds larger mechanisms only when the shipped prompt misses a preregistered pass line.

## What Changes

- Measure first: a separate case-authoring lane files a benchmark amendment with the owner's pass line, and freezes six positive cases with durability-matched twins and an answer key. The real agent driver runs a baseline on `main` and each arm, with 3 repeats per case per client shape.
- Rewrite the nine served texts that require confirmation or leave a promotion unclassified, and make the owner's type, category and relation saves and authored typed edges `proactive_capture` through a MODIFIED envelope ceiling, ratified by owner decision O2. The served envelope classifies each promotion action in a structured map.
- Add an `undeclared_referents` block to every durable write's receipt: up to 3 names that the write newly links and that have no entity, each with the `create-entity` route. It ships only if the arm with it beats the arm without it, or the agent acts on its names.
- Add typed `create-entity` connections and the `add-relation` and `remove-relation` leaves, with relation `generic` and `endpoints` data, kinship and affiliation core relations, and an offer that replaces a generic edge in place.
- Gate a `referents` declaration argument on `remember` and `observe_memory`: it is built only when the shipped arm misses the pass line, and it ships only if its arm beats the shipped arm.
- Add a `recent_promotions` due-state category that surfaces each promotion once in the next interactive session, and one revert call per promotion, keyed by its entry.
- Show relations by label, for example `has_pet (a kind of owns)`, through one formatter.
- Move the traversal profiles into a pack and add an `entity` profile only if the pass line is still missed and an owner-anchored question fails while its entity and edge exist.

## Capabilities

### New Capabilities

- `referent-promotion`: typed-edge leaves, provenance, surfacing, revert, disclosure, and the rule that the server creates only what the agent asks for.
- `referent-declarations` (gated): the `referents` argument, its outcomes, pending completion and teaching. Task 5.2 removes it if the gate fails.

### Modified Capabilities

- `delegation-envelope`: the ceiling requirement adds the owner's promotions to `proactive_capture`, and a scoped edge removal and the promotion revert to `restructure_execution`.
- `write-time-identity-candidates`: detection, not the server, never creates an entity; writes carry `undeclared_referents`.
- `epistemic-relation-registry`: relation labels, the display formatter, endpoint families and the generic marker.
- `relation-vocabulary-evolution`: a generic edge between entities offers fitting predicates that replace it in place.
- `graph-traversal-profiles` and `context-activation` (conditional): pack profiles, the `entity` profile and typed expansion of entity anchors.

## Impact

- Code: `commands.py`, `envelope.py`, `prominence.py`, the scaffold, `capture_sweep.py`, `semantic_writes.py`, `link.py`, `curation.py`, `relation_registry.py`, `vocabulary/registry.py`, `due_state.py`, `mutation_terminal.py` and the core packs; `traversal_profiles.py`, `working_set.py` and `epistemic_graph.py` only on the D4 trigger.
- Published surface: `connect_memory` gains two operations and typed `connections`, and needs a measured raise of its 4,300-byte ceiling. `triage_memory` gains the `revert` action.
  `referents`, if earned, adds about 844 bytes on each of two tools and needs both per-tool ceilings and the 90,000-byte surface total raised.
  The ChatGPT connector needs a refresh after the release.
- Benchmarks: one new §7 amendment family; a comparative claim waits for its founder acknowledgment. The change cannot close until the shipped arm meets the pass line or the owner rules.
- Vaults: no page is rewritten. New registry fields are optional.
- Supersedes: the served confirmation texts, the confirm-required type save in the `complete-recurring-entity-lifecycle` envelope delta and the affiliation confirmation in the `capture-durable-personal-baselines` envelope delta (both amended here), and the gating role of close-memory-loop task 5.3 and agent-led vocabulary tasks 7.1–7.4 for the owner.
- Does not wait for close-memory-loop task 4.3.
