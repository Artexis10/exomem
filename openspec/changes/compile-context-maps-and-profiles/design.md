# Design

## Context

`activate_context` today runs freshness, readiness and a lexical BM25 over anchor pages, then `compile_packet`: it analyses the raw turn, builds categorical anchor evidence, resolves anchors and fills role lanes from their neighbourhood. When nothing resolves, a gated lexical carry over the raw turn may serve one uncontested page; otherwise the turn abstains with no units. Activation also embeds the raw turn against anchor signatures (title, lede, headings) for `vector_band` evidence. It never calls the hybrid recall engine behind `ask_memory`. Client instructions tell agents to pass the user's words verbatim and rely on the packet.

A replay of twelve private field reports, with an answer key written by an independent lane, measured where context is lost. The cases are development evidence; they do not decide a switch.

| Arm | Expected page served or selected | Expected page reached |
|---|---|---|
| Current packet, raw turn | 1 | 2 |
| Hybrid recall, raw turn (top 3 / top 10) | 3 | 3 |
| Hybrid recall, agent-written queries (top 3 / top 10) | 8 | 9 |
| Agent with a 3.7 KB map, pulling by ref and query | 7 | 10 |

The strongest result is an agent that sees what exists and pulls. A server-side selector that matches the raw turn by meaning was not tested.

## Goals

- One-shot context: a fresh session, coding or conversational, answers the user's first message with the context it needs, without the user pointing the agent at material. Autonomous pulls before the first answer are part of one-shot.
- Push little: a bounded map. Give depth through pulls by ref or query.
- Scale without a size cap: the vault grows, the map stays bounded, and cold material stays reachable through domain maps and recall.
- Use the epistemic engine fully: semantic units, roles, currency, supersession, typed relations and the vocabulary registries.
- Do no harm: a turn with no relevant memory gets no misleading material, including from the map.

## Non-goals

- A model call inside the activation request path.
- Replacing `ask_memory` or `read_memory`.
- A new `queries` input on `activate_context` before a workflow or latency benefit is shown; agents query through `ask_memory`.
- A mandatory current-state field on every page.
- A new storage format.

## Shape

1. **Catalogue.** One searchable set of entries: topics, anchors, threads and trackers. Each entry has a one-line description, its memberships, its source refs and the generation it was built from.
2. **Map.** A bounded projection of the catalogue: live topics, threads, key anchors and trackers, one line each with what it is, its state from the current-state authority with its as-of date when known, and a ref. Activation in `maps` mode returns it. It does not depend on the turn.
3. **Domain map.** The projection of one topic's entries, read by ref. It is how cold material stays reachable when it is not on the map.
4. **Profiles, read by ref.**
   - An **anchor profile** is what the role lanes compile for one anchor today when an agent passes `anchor`: units by role with currency and refs, current state from Records first, typed relations, related pages and evidence pointers.
   - A **topic profile** compiles the same lanes over a topic's members: the topic's standing stances, preferences, constraints and conventions, the anchors active now, open loops and recent changes.
   Profiles are compiled on demand and cached by generation.
5. **First-answer workflow.** The agent reads the map, opens the domain maps and profiles that the turn needs, searches with its own words when the map does not cover the need, then answers and cites refs.
6. **Turn slice (an evaluation arm, not yet a contract).** Profiles selected by meaning from the turn and attached to the map. It ships only if it beats the map alone.

## Decisions

### D1. Map-assisted pulls are the first-answer workflow

The evidence supports an agent that sees what exists and pulls. Activation in `maps` mode returns the map; client instructions make reading it and pulling before the first answer the supported workflow. The map is the only pushed layer until an arm shows that more push helps.

### D2. One catalogue, many projections

The map and the domain maps are projections of one catalogue, not separately maintained documents. Profiles are compiled from the existing role lanes, not stored as a second copy of the units. This keeps one source for each fact and one place to invalidate.

### D3. Topic membership is a declared contract

A topic is a domain from the vocabulary registry. An entry belongs to a topic when its page declares the domain, when a project key maps to it, or when an authored typed relation of a registry-declared membership kind links it to a member. A mention of an anchor does not import the anchor's topics. Ordinary notes become catalogue entries in the topics they declare, so a domain map reaches notes that are not anchors. Membership rules live in the registries; code branches only on the closed attributes they declare.

### D4. Current state has one authority

Map lines and profiles take state from the existing current-state resolver: Records first, then declared state pages, then observations. Units keep their own authored dates; a page's modification date does not become a unit's as-of date. An entry with no known state shows none.

### D5. Descriptions are written at write time and invalidated by generation

When a write creates or changes a catalogue entry, the writing agent may supply its one-line description. Otherwise a derived description is stored, marked as derived, outside Sources and Evidence. Refresh runs through derived sync with explicit reverse dependencies: old and new memberships, incoming relations, deletion, rename, supersession, registry changes and external edits. Refresh coalesces per entry and never publishes a projection built from an obsolete generation. A projection whose inputs changed and are not yet rebuilt is served marked stale.

### D6. Backfill is a launch dependency

Existing entries get descriptions before any evaluation of the map. The backfill is resumable, records its provenance and cost, reports coverage, and leaves uncovered entries visibly undescribed.

### D7. The reader's view applies before compilation

The catalogue, map, domain maps and profiles are computed over the reader's view: hidden inputs never reach synthesis, selection, ranking, ordering, caps, counts, activity scores, staleness marks or generation tokens. A restricted reader's projections are compiled for that view and bypass any shared cache. Counterfactual twins prove it: adding or changing a withheld entry leaves a reader's map, domain maps, profiles and their ordering unchanged. The egress guard learns the new blocks and fields.

### D8. Refs to derived views resolve through one read contract

`read_memory` resolves `map`, domain-map and profile refs with bounded output, pagination for overflow, and explicit missing and stale states. Activation and every surface return the same shapes.

### D9. One packet budget

The map and any slice share the existing packet character cap. Pulls have their own budget per answer. Overflow becomes bounded, paginated refs, never cut claims.

### D10. The `anchors` mode stays until a frozen evaluation decides

A setting selects `anchors` (default) or `maps`. Existing activation requirements apply to `anchors` mode. The default changes only through the frozen evaluation below and the normal release line; the setting is the undo.

## Evaluation

The protocol is frozen before any held-out generation:

- **Cases.** Field reports turned into cases by an independent key writer, with source-derived references. The existing twelve are development evidence. The held-out set stays unread while iterating and grows as new field reports arrive. Related cases are grouped.
- **Arms.**
  - The `anchors` packet, with the same pull tools and pull budget as the other arms.
  - The map with pulls.
  - The map plus the turn slice, with pulls.
  Every arm uses the same agent, vault snapshot, tool permissions and pre-answer budget. The raw-turn arms receive no conversation, refs or prior session state.
- **Primary measure.** Whether the first answer uses the required current facts and constraints, graded blind against source-derived references. A newer successor or equivalent valid evidence counts under a rule fixed before generation.
- **Secondary measures.**
  - Expected material reached.
  - Off-target share.
  - Contamination of answers on cases without relevant memory, including contamination from the map.
  - Full-workflow tokens, calls and latency.
  - Failures, kept in the denominator.
- **Decision.** Report the paired per-case difference with its interval. The switch needs the primary measure's lower bound above zero, and non-inferiority within a fixed tolerance on contamination and on the activation-audit twins. Small samples can show only large effects; the report says so.

## Risks

- **A description is stale or poor.** Each line carries its generation, a derived description is marked, and stale projections are served marked.
- **The map grows with the vault.** The map is a bounded projection, and domain maps and recall hold the rest.
- **Invalidation fans out.** A highly connected anchor can invalidate many projections. Refresh coalesces and rebuilds lazily on read when cheaper.
- **Hidden material leaks through aggregates.** D7 computes over the reader's view before synthesis, and the counterfactual twins check it.
- **The evaluation flatters an arm.** The protocol fixes agent, budget, snapshot and scoring before generation.

## Open questions

- The backfill method: a model pass, derived descriptions, or both, and its cost.
- Whether a turn slice adds value over the map alone.
- Whether `ask_memory` queries before the first answer are enough, or a combined call saves latency.
