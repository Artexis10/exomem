# Design

## Context

`activate_context` today runs freshness, readiness and a lexical BM25 over anchor pages, then `compile_packet`: it analyses the raw turn, builds categorical anchor evidence (`working_set_resolve.candidates_for`), resolves anchors and fills role lanes from their neighbourhood. When nothing resolves, a gated lexical carry over the raw turn may serve one uncontested page; otherwise the turn abstains with no units. The hybrid recall engine behind `ask_memory` is never called. `conversation.focus` reaches only lexical anchor-name matching. Client instructions tell agents to pass the user's words verbatim, not a search query.

A replay of twelve private field reports, with an answer key written by an independent lane, measured where context is lost:

| Arm | Expected page served or selected | Expected page reached |
|---|---|---|
| Current packet, raw turn | 1 | 2 |
| Hybrid recall, raw turn (top 3 / top 10) | 3 | 3 |
| Hybrid recall, agent-written queries (top 3 / top 10) | 8 | 9 |
| Agent with a 3.7 KB map, pulling by ref and query | 7 | 10 |

The knowledge is stored and retrievable. A raw turn is a poor query, and a pushed packet that is guessed from it is mostly empty or off-topic.

## Goals

- One-shot context: a fresh session, coding or conversational, has what the first answer needs, from the user's turn alone, without the user pointing at material.
- Push little and push what is reliable; give depth through pulls by ref or query.
- Scale without a size cap: the vault grows, the per-turn push stays a few kilobytes.
- Use the epistemic engine fully: semantic units, roles, currency, supersession, typed relations and the vocabulary registries.
- Do no harm: a turn with no relevant memory gets no misleading material.

## Non-goals

- A model call inside the activation request path.
- Replacing `ask_memory` or `read_memory`.
- Removing the anchor resolver before the evaluation decides the switch.
- A new storage format. Maps and profiles are compiled from units and index rows, so they work on Markdown and on a SQLite store alike.

## The layers

1. **Map.** An always-on index of live areas, threads, key anchors and trackers. One line per item: name, what it is, current state with an as-of date, ref. It does not depend on the turn, so it is stable across a session and cacheable by clients. Default budget 4,000 characters.
2. **Domain map.** The same index for one topic: its anchors, threads and trackers. The map links to each domain map by ref.
3. **Profiles.** A compacted bundle of context with refs to everything it cites.
   - A **topic profile** covers one topic. A topic is a domain from the vocabulary registry (`domains`, plus the vault overlay). It carries the topic's standing stances, preferences, constraints and conventions, the anchors that matter now, open loops and recent changes.
   - An **anchor profile** covers one anchor: an entity, project, hub or collection the index already knows. It carries a summary, current state with an as-of date, key units by role, typed relations, related pages and recent changes.
4. **Turn slice.** The topics and anchors this turn touches, with their profiles. At most three profiles, 1,500 characters each by default. It may be empty.
5. **Depth.** Pages, units and evidence through `read_memory` and `ask_memory`, by ref or by the agent's own queries.

## Decisions

### D1. Push a stable map, not a guessed packet

The map is the default context of every turn. A map line cannot be wrong for a turn; it can only be absent. Its stability lets clients cache it. The agent sees what exists and how it links, then pulls.

### D2. Profiles are compiled views over semantic units

Each profile line is a semantic unit or a reference to one. It carries the unit's category, its role from the roles registry, its currency (current, superseded, disputed) with an as-of date, and its ref. A unit that does not fit becomes a pointer, never a cut claim, as today. What a profile shows per topic comes from the roles registry. Its relations come from the relations vocabulary. Code holds no vocabulary (C4).

### D3. Topics come from the registry

Topics are registry domains. An anchor belongs to the topics that its page, its units and its typed neighbours declare. A vault that adds a domain gets a topic profile and a domain map for it without a code change.

### D4. The slice is selected by meaning over curated text

The selector embeds map lines and profile summaries, which are short and written to be found. It matches the turn, the recent conversation and, when present, the agent's `focus` and `queries` against them. Existing resolver evidence (an exact name, a continuing thread) raises a candidate; it never excludes one. A candidate below the confidence threshold appears as a "possibly relevant" line with its ref, not as a profile. The threshold is calibrated on the development split only.

### D5. Agent queries reach hybrid recall

`activate_context` accepts up to three agent-written `queries`. Each runs through hybrid recall over the caller's view. Their hits join the slice as pointers or units labelled `origin: agent_query`. Agent cues still never settle an ambiguous human reference to an identity; they only choose what to read.

### D6. Maps and profiles stay current on write

Each write that creates or changes a page updates its one-line `summary` and its current-state fields with an as-of date. The writing agent supplies the summary because it holds the context at that moment; when it does not, a derived summary is stored and marked as derived. The write then queues refresh of the profiles and map lines whose anchors or topics it touched, through the existing derived-sync components. A per-thread head index exposes the newest episode of each thread.

### D7. Maps and profiles respect the reader's view

Maps and profiles are compiled per audience, or filtered at serve time by the same visibility rule that `audience_view` and `anchor_visible` apply today. A non-owner never sees a withheld anchor's line, its units or its count.

### D8. The client contract changes with the packet

The server instructions, the `activate_context` description, the skill scaffold and the retrieve hook say: read the map and the slice; when the answer depends on stored knowledge, open a profile or page by ref, or search with your own words; cite refs. The hook still sends the user's turn; it adds the recent conversation when it has it.

### D9. The switch is decided by evaluation

A setting selects the packet: `anchors` (default) or `maps`. The default changes only after one blind run on the held-out split shows that the new packet beats the current one by more than the accepted loss, with no new harm on negative cases and on the existing activation-audit twins. The change ships through the normal release line, and the setting is the undo.

## Evaluation

- Corpus: field reports converted to cases by an independent key writer, split into development and held-out halves. The corpus stays outside the repository.
- Arms:
  - the current packet;
  - map plus slice from the turn alone;
  - map plus slice with agent pulls.
- Measures, reported per case:
  - expected material served in the slice;
  - expected material reachable within the agent's pulls;
  - off-target share of served text;
  - harm on cases with no relevant memory;
  - tokens, memory calls and latency.
- Iterate on the development split. Run the held-out split once, blind, to decide the switch.

## Risks

- **A stale summary pushes a wrong state.** Every line carries its as-of date and currency, writes refresh it, and an unrefreshed line is marked stale.
- **The map grows with the vault.** The map tiers: it lists areas and hot items, and each domain map holds the rest.
- **The slice selects a wrong profile.** It is small, below-threshold candidates are only named, and the map stays neutral.
- **Writes get slower.** Refresh is incremental and runs in the background derived sync, not in the write's request.
- **The evaluation leaks.** The key is written independently, the held-out split stays unread while iterating, and replays run against the reader's view.

## Open questions

- How existing pages get their first summaries: a backfill job with a model, derived summaries, or both.
- How a case scores when the agent prefers a newer page written after the reported failure.
- Whether the slice needs a server-side query rewrite for turns that defeat embedding match, or whether agent `queries` cover it.
