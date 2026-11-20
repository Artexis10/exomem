# Design

## Context

Two paths bring the graph into activation today. Task discovery reads each projected lead's
outbound links (`working_set._task_links`, five per lead, typed before plain) and then
`context_task.compile_context` charges them after every lead and method. Anchor activation
expands each resolved anchor through `graph_context` under the `epistemic` profile at depth 2.
It uses the reached pages only as the scope of the unit lanes. The `graph` lane that would serve
neighbours as pointers is selected by no shipped role.

The edge store records relation, origin and authoring page, but no sentence context. The
batched neighbour API (`EpistemicGraphIndex.neighbors_for`) answers both directions for many
seeds in one query, at about 35 ms per seed on a 15,000-page copy. On the same cold copy,
`graph_context` took about 5 s per call.

## Goals / Non-Goals

Goals: put the connections the graph already holds in front of the agent, with a reason it can
judge, within the existing packet budget and deadline.

Non-goals: new edges, inferred or transitive relations, write-time routing, image fusion, and
time-scoped anticipation of upcoming plans. On the measured case, no edge path connected the turn
to the relevant upcoming plan, and three hops reached 403 pages. That signal belongs to recall
assembly, not traversal.

## Decisions

1. **One connection read.** `working_set` gains one function that takes seed pages and returns
   ranked connections per seed: relation key, direction, target ref, target title, tier and why.
   Task leads and the graph lane both call it. It uses `neighbors_for` with the caller's `keep`.
   A target outside the caller's catalogue view is never named. The read is budget-gated and
   soft-fails to no connections.
2. **Ranking by closed attributes.** The tiers are:
   0. The relation carries `replaces`, `corrects`, `opposes` or `settles`.
   1. It carries `sources`.
   2. Any other relation that is neither `generic` nor `associative`.
   3. Generic or associative relations, including `plain_link`.

   An inbound edge whose relation carries `replaces` ranks first of all, because it means a
   newer page replaces the seed. Within a tier, a target whose planning status `class` is `open`
   ranks first, and otherwise the owner API's order stands. No relation key or family name is
   compared in code (C4/C6).
3. **The why.** Format: `<relation> (<direction>): <context>`. The context is the sentence or
   bullet in the authoring page that carries the link: the seed for outbound, the other page for
   inbound. It is stripped of wikilinks, released through `egress.prose_for_caller` and cut at
   140 characters on a word boundary. A relation bullet with no prose beyond the link uses the
   target's lede (`working_set_index.lede`) instead. Pages are read from the warm page cache, and
   only for connections that will be served.
4. **Allocation.** A lead is admitted together with its first connection when both fit. If only
   the lead fits, the lead is served without it, as now. Further connections fill the leftover
   room in lead order. This trades a few lead slots for one judged connection per lead. The
   outcome round measures that trade.
5. **Anchors.** A shipped `connections` role (`lane: graph`, every anchor kind as a default)
   serves up to four connections per resolved anchor as `pointers[]` (ref, title, why). It
   replaces the alphabetical neighbour list in `_graph_lane`.
6. **Profile.** The core pack gains `recall`: the epistemic families plus link, entity,
   derivation, evidence and citation. `GRAPH_TRAVERSAL_PROFILE` becomes `recall`, and a vault
   overlay may extend it like any shipped profile. Node and edge caps are unchanged (24 and 40).
   No sampled page hit them under all families.
7. **Agent default.** The connection line ends with one instruction: read a connection when its
   reason bears on the answer. The scaffold skill text says the same in one line.
8. **Room for the task section.** The map compiles first at `max_chars` less the turn-slice reserve
   (`TURN_SLICE_SHARE`). At 4,000 characters and a quarter share, the map took about 3,000 characters
   and the task section 400 to 750, about one lead with its reason. On a vault copy with 60 recorded
   query turns, lead-first allocation alone served 72 leads and 10 connections (main: 93 and 8). A
   half share at 4,000 served 181 and 88 but cut the map from 16 lines to 6. A half share at 6,000
   served 307 leads and 216 connections and kept the map at 17 lines, at 1.2 s more per activation (paired on 29 turns, 95% interval 0.6 to 1.9 s, on an 18 s median under host load). The share
   becomes 0.5 and the default `max_chars` and hook render ceiling become 6,000. The cost is about 500
   more tokens per recall; the ceiling stays 8,000.
9. **Measurement.** `served_context` records each offered connection's relation, target ref and
   tier, so a later read can be credited. The mechanism metric is connections served per lead
   on leads that have edges.

## Risks / Trade-offs

- Fewer leads fit when each carries a connection; the larger packet and share offset this. The
  outcome round compares main with this change on the same frozen cases.
- Every recall injects about 500 more tokens. A client can lower `max_chars` or the hook's
  `EXOMEM_RETRIEVE_INJECT_MAX_CHARS`.
- The wider `recall` neighbourhood gives the unit lanes more pages, which can add noise. It is
  measured with the same round, and a vault can narrow the profile.
- Inbound context reads touch other pages. They are bounded to served connections and come from
  the warm cache.

## Migration Plan

No data migration. The shipped role and profile load with the core packs, and an existing vault
override keeps working. A rollback reverts the code and packs.

## Open Questions

None blocking.
