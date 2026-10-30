## Context

`op_activate_context` serves `maps` by default. The `maps` branch compiles the
map, then calls `slice_leads`, which runs `_compile_guarded_activation` on the
user turn with no continuity, conversation, `anchor` or attribution. The anchors
packet calls the same helper with all of them. Every write that the anchors
packet makes (pick learning, selection notes, the session's last served thread)
runs in `commands.py` after its token is minted, gated on `anchor` or an
attributed session. `serve` and `compile_packet` write nothing.

The retrieve hook sends the continuity token and a transcript-built
`conversation` (the earlier user turns and each turn's final assistant text) on
every call.

## Goals / Non-Goals

**Goals**

- The pushed slice of a follow-up turn reaches the subject that the conversation
  established.
- `maps` activation stays read-only and turn-independent in its map.
- A restricted caller learns nothing from the conversation that it could not
  read.
- `generation.continuity` and `generation.conversation` say what happened.

**Non-Goals**

- Server-side memory of the thread. In `maps` mode the agent follows the thread
  and pulls what it needs.
- Token-ref carry and the `anchor` argument in `maps` mode. They exist so that
  the server can answer for the agent. A `maps` agent reads the ref it means.
- Learning, heat, `recent_context`, pushed units, the slice ordering and
  `intent_resolution` in `maps` mode.

## Decisions

### D1. The slice compile reads the conversation through the existing contract

`slice_leads` passes the request's `conversation` and `trigger` to
`_compile_guarded_activation`, with no `continuity`, no `anchor`, no
`read_thread` and no `intent_resolution`. `serve` then applies the
conversation-aware activation contract unchanged:

- `focus` is a second worded segment of the turn;
- earlier entries and refs add the `conversation` qualifier to candidates that the
  turn reached;
- the anaphoric conversation carry gives one `partial` anchor with evidence
  `[conversation]`;
- `task_refresh` resolves the focus.

`turn_leads` already maps `resolved` and `partial` anchors to slice items and
keeps the `conversation` evidence kind, so a lead that only the conversation
reached says so.

Why: the conversation is already on every hook request, and the contract that
reads it is shipped and tested. No new resolver is needed.

### D2. No attribution, so nothing is written

The slice compile gets no attribution and no thread. The session tier, the
follow-up carry from a session and every post-mint write stay in the anchors
branch. A `maps` request therefore stays read-only.

### D3. The caller's view bounds the conversation

`serve` drops conversation refs that the caller's principal cannot see before the
compile. For a restricted caller, the slice compile narrows rows to the caller's
view before it reads the entries. A conversation that names a withheld page or
anchor therefore changes no byte of the restricted caller's packet. The twin
tests prove it.

### D4. Honest reports

- `generation.conversation` is the state that the slice compile computed
  (`applied`, `truncated` or `absent`). When no slice compile reads the
  conversation (the setting is off, the turn is empty, or the budget stops the
  slice first), it is `absent`, as an anchors packet reports a skipped
  conversation stage.
- `generation.continuity` in `maps` mode is `absent` with no token, `applied`
  when the token carries a map generation, and `stale` for any other token.
  `applied` means that activation compared the token's map generation with the
  served map's. `map.unchanged` says whether they matched. A vault write that
  moves the map generation no longer turns the report into `stale`.
- `generation.continuity_thread` stays `absent`. A `maps` token carries no thread.

The token keeps its current content. Its `identity` stays the map generation,
which the caller's view computes, so that twin vaults still serve byte-identical
packets to a restricted caller.

### D5. The description names the conversation

The slice's fixed description becomes "What this turn and its conversation
reached, with the evidence. Not checked for relevance; read a ref before relying
on it." Each item's evidence says when the conversation qualified or carried it, and an item whose cue the user did not speak carries the compile's `origin` (`focus` or `conversation`), as an anchors packet's anchor does.

### D6. No new setting

`EXOMEM_ACTIVATION_TURN_SLICE=off` removes the slice and this behaviour
together. `EXOMEM_ACTIVATION_PACKET=anchors` restores the earlier packet. A wrong
carry adds one slice line that the packet already marks as not checked for
relevance. That cost does not justify a separate switch.

## Risks / Trade-offs

- **Latency.** The hook sends a conversation on every turn, so every slice now
  analyses one. The latency gate's paired slice-on-minus-off difference gains a
  case with a conversation, under the same bound.
- **False carry.** The conversation carry can pick an earlier subject for a turn
  that changed topic. The shipped carry's licence and its topic-switch gate
  apply unchanged. The slice marks the lead with `conversation` evidence.
- **Development arm M changes.** Development rounds that run arm M see the new
  slice. The held-out split, the arms' frozen texts and the decision rule do not
  change.

## Migration Plan

Ship in the ordinary release. A client needs no change. A token minted before the
release reads `applied` when it carries a map generation.
