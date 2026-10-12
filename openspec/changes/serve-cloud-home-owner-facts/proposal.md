# Proposal

## Why

Exomem Cloud Home shows and sets the owner's involvement level for each engagement context.
It also shows a line of recent assistant activity and the note count.
Home has no route of its own on a cell: it calls the cell's MCP endpoint with an owner token, like any assistant.

Four gaps stop Home from doing this honestly:

- `configure_memory` reports only the level of the context that the caller runs in, so Home cannot show both contexts.
- The call ledger cannot say whether a call read or wrote, or how many items it touched.
- Nothing serves an activity summary or a note count.
- A stateless request from a handshake-protocol client carries no client name. The Cloud gateway forwards no `User-Agent`, so every chat app behind it resolves to the coding context and never gets its Chat apps level.

## What Changes

- `configure_memory` results gain `levels`: the level and its source for every engagement context, from one precedence rule.
- Call-ledger rows move to schema 2 with two content-free fields: `access` (`read` or `write`) and `item_count`. Version 1 rows stay as written and still verify.
- `bootstrap(profile="compact", section="activity")` returns `{since, as_of, note_count, events}` to the owner. Unknown counts are null, never zero.
- On a cloud cell, the caller's client name comes from a gateway-set header after `clientInfo` and before `User-Agent`.
- The cell registers no new route. Only MCP output fields change.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `agent-prominence-control`: report every context's level and the rung that decided it.
- `call-ledger`: record access and item count without content, and keep reading version 1 rows.
- `cloud-cell`: name the gateway's OAuth client, and serve the owner's activity and note count through MCP.

## Impact

The change touches prominence resolution, `configure_memory`, the bootstrap section list, the ledger row builder, the MCP wrapper, the read dispatcher's disclosure boundary and the caller identity.
An older image refuses the unknown `activity` section, so Home can tell an outdated cell from an idle one.
The Cloud gateway must set the client header; that change belongs to the gateway and ships after the cell image.
