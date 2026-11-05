## Why

In `maps` mode the agent follows the conversation: it holds the chat, reads the
map and pulls domain maps and profiles by ref. The server pushes one thing that
changes per turn, the turn slice, and the slice reads only the words of the
current turn. A follow-up such as "and the other one, is that fixed now?" has no
words of its subject, so the slice for it holds unrelated leads.

On 2026-10-30, an installed release served such a follow-up. The retrieve hook
sent the earlier turns as `conversation`, and the `maps` branch discarded them.
The slice listed an unrelated note, and the packet reported
`generation.continuity: "stale"` and `generation.conversation: "absent"`. The
continuity value is `stale` for every token in `maps` mode, because the branch
never evaluates the token. It only compares the map generation.

## What Changes

- The turn slice's anchors compile reads the request's `conversation` and
  `trigger` under the existing conversation-aware activation contract. A
  follow-up then reaches the subject of the earlier turns, and its lead carries
  the `conversation` evidence kind.
- The slice compile still takes no continuity refs, no `anchor` and no
  attribution. It mints nothing, learns nothing and records no heat. The map does
  not change.
- `generation.conversation` reports what the slice compile computed.
  `generation.continuity` reports `applied` for a token that carries a map
  generation, which activation compares with the served map's, and `stale` only
  for any other token.
- The slice's fixed description names the conversation beside the turn's words.
- `compile-context-maps-and-profiles` keeps its `maps` contract. Its sentences
  that exclude the conversation from the slice now refer to this change.

Not changed: the continuity token's content, token-ref carry, the `anchor`
argument in `maps` mode, the thread and session tier, learning, the map, the
`anchors` packet and the slice ordering.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `context-activation`: adds "The turn slice follows the conversation" and
  "A maps packet reports its continuity inputs". The `maps` packet requirements
  that `compile-context-maps-and-profiles` adds are reworded in that change to
  refer to them.

## Impact

- `src/exomem/commands.py`: the `maps` branch passes `conversation` and
  `trigger` to the slice compile and reports both inputs.
- `src/exomem/context_views.py`: the slice keeps the compile's conversation state
  and its description.
- Retrieve-hook rendering of a slice lead with `conversation` evidence, if it does
  not label it today.
- The `maps` packet of the development arm M changes unless the slice is off.
  The cases, arms, keys, scoring rule and held-out split of
  `compile-context-maps-and-profiles` do not change.
- Rollback: `EXOMEM_ACTIVATION_TURN_SLICE=off` removes the slice and this
  behaviour together. `EXOMEM_ACTIVATION_PACKET=anchors` restores the earlier
  packet.
