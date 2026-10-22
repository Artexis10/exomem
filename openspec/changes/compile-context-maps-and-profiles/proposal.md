# Proposal

## Why

The context compiler exists so that a fresh agent session has the context it needs from the first message, without the user pointing it at the right material. Today it does not do that. It reads the raw user turn through a lexical resolver, decides which anchors the turn names, and pushes a packet about those anchors. A raw turn is a poor query: voice transcription, typos, chat filler and spaced compounds defeat it. When the resolver guesses wrong, the packet fills with off-topic material; when it is unsure, it abstains and serves nothing. `activate_context` never calls the hybrid recall engine, and an agent's own `focus` line only reaches lexical name matching.

A private replay of twelve field reports measured the gap on one live vault. The pushed packet reached the expected page in 2 of 12 cases and abstained in 8. Hybrid recall on the raw turn reached 3 in its top three. An agent that wrote its own queries reached 9 in its top ten. An agent that also saw a 3.7 KB map of the vault reached 10, with a third fewer memory calls. The stored knowledge was there; the push model did not deliver it.

## What Changes

- Compile an always-on **map**: a small index of the vault's live areas, threads, key anchors and trackers, one line each with what it is, its current state and a ref. The map does not depend on the turn.
- Compile **domain maps**, **topic profiles** and **anchor profiles** from semantic units, typed relations and current state. A topic is a domain from the vocabulary registry. A profile is a compacted bundle of context with refs to every unit it cites.
- Select a **turn slice** from the turn and the recent conversation: the topics and anchors the turn touches, matched by meaning against map lines and profile summaries. The slice carries at most a few profiles and may be empty. It never gates what the agent can pull.
- Return the map and the slice from `activate_context`. Accept optional agent-written `queries` and run them through hybrid recall.
- Keep maps and profiles current on write. Each capture or edit updates the one-line summary and current-state fields of the pages it touches, then refreshes the affected profiles and map lines.
- Tell clients to read the map and slice, then pull profiles, pages and units by ref or by their own queries.
- Keep the anchor-first packet as the default until a held-out evaluation decides the switch.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `context-activation`: Serve a compiled map and a turn slice of topic and anchor profiles, maintained on write, with agent pulls as the depth path.

## Impact

The change adds derived index components (map, domain maps, profiles, page summaries), a new packet shape behind a setting, an optional `queries` input on `activate_context`, and new client instructions in the server instructions, tool description, skill scaffold and retrieve hook. Writes gain summary and current-state upkeep. Storage stays as it is: maps and profiles are compiled from units and index rows, never by parsing Markdown, so they carry over to a SQLite store. The current resolver, its soundness rules and the existing activation audit remain in force until the switch.

This change carries the ordinary-use acceptance that `close-memory-loop` task 6.32 requires for context delivery.
