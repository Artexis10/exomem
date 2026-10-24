# Proposal

## Why

The context compiler exists so that a fresh agent session has the context it needs from the first message, without the user pointing it at the right material. Today it does not do that. It reads the raw user turn through a lexical resolver, decides which anchors the turn names, and pushes a packet about those anchors. A raw turn is a poor query: voice transcription, typos, chat filler and spaced compounds defeat it. When the resolver guesses wrong, the packet fills with off-topic material; when it is unsure, it abstains and serves nothing.

A private replay of twelve field reports measured the gap on one live vault. Its run files stay outside this repository, so the figures are not reproducible here. These twelve cases are development evidence only. The pushed packet reached the expected page in 2 of 12 cases and abstained in 8. Hybrid recall on the raw turn reached 3 in its top three. An agent that wrote its own queries reached 9 in its top ten. An agent that also saw a 3.7 KB map of the vault and pulled by ref and query reached 10, with a third fewer memory calls. The stored knowledge was there; the push model did not deliver it, and an agent that could see what exists and pull did.

## What Changes

- Make **map-assisted pulls** the first-answer workflow: activation returns a small map of what the vault holds, and the agent reads the map, pulls what the answer needs, and only then answers. The user does not point the agent at material.
- Keep one searchable **context catalogue** of described entries (topics, anchors, notes, threads, collections) with membership, source refs and generations. It is distinct from the existing anchor catalogue. The **map** and the **domain maps** are bounded projections of it, and the map says it is an index of what exists, not material relevant to the turn.
- Serve **anchor profiles** and **topic profiles** by ref, compiled on demand through the existing role lanes and current-state authority, cached where useful.
- Define **topic membership** from registry domains, project keys and explicit page declarations, with direction rules, so that ordinary notes are reachable through their topic and not only through anchors.
- Keep descriptions current on write with generation-bound invalidation, and backfill descriptions for existing entries before evaluation.
- Filter by the caller's view **before** any synthesis, selection, ranking or budget.
- Probe a **turn slice** (profiles selected by meaning from the turn) on development cases only; it ships only through its own frozen evaluation. Test a pull-instructions arm with no map, so the context catalogue must earn its cost, and sample ordinary turns beside known misses, so the switch cannot win only on cases the current packet failed.
- Tell clients to read the map, pull profiles, pages and units by ref or by their own `ask_memory` queries, and cite refs.
- Keep the anchor-first packet as the default until a frozen, paired evaluation of first answers decides the switch.
- Build shared index inputs inside the existing internal-producer scope. Keep caller authorization and disclosure at every external read.
- Recover an unproven state checkpoint through a separate successor path that preserves the failed attempt and earns fresh publication and quiescence proof.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `context-activation`: Add a `maps` packet mode, selected by `EXOMEM_ACTIVATION_PACKET`, that serves a map projected from the context catalogue with profile reads by ref, alongside the current `anchors` mode. The continuity, conversation-aware activation and retrieve-hook contracts keep binding `anchors` mode; `maps` mode defines its own continuity and conversation behaviour.
- `command-surface`: Add an optional `continuation` to `read_memory` for derived-view refs, the `CONTINUATION_STALE` refusal, and optional `summary` and `topics` inputs on `remember`, generated on every surface.

## Impact

The change adds a context catalogue and its derived projections, a profile read contract, a `maps` packet mode behind a setting, description upkeep on writes, a one-time description backfill, and new client instructions in the server instructions, tool description, skill scaffold and retrieve hook. It reuses the role lanes, the Records-first current-state resolver, the caller's-view rules (`audience_view`, `anchor_visible`) and the egress guard. Profiles and maps are built from units and index rows, never by parsing page text, so they carry over to a SQLite store. The `anchors` mode, its resolver and its audit stay in force until the switch.

This change carries the ordinary-use acceptance that `close-memory-loop` task 6.32 requires for context delivery.
