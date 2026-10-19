# Proposal

## Why

Exomem Cloud Home shows and sets the owner's involvement level for each engagement context.
It also shows a line of recent assistant activity and the note count.
Home has no route of its own on a cell: it calls the cell's MCP endpoint with an owner token, like any assistant.

Five gaps stop Home from doing this honestly:

- `configure_memory` reports only the level of the context that the caller runs in, so Home cannot show both contexts.
- The call ledger cannot say whether a call read or wrote, or how many items it touched.
- Nothing serves an activity summary or a note count.
- Activity groups by client family, so two OAuth grants of the same family cannot show separate app feedback.
- A stateless request from a handshake-protocol client carries no client name. The Cloud gateway forwards no `User-Agent`, so every chat app behind it resolves to the coding context and never gets its Chat apps level.

## What Changes

- `configure_memory` results gain `levels`: the level and its source for every engagement context, from one precedence rule.
- Call-ledger rows move to schema 2 with two content-free fields: `access` (`read` or `write`) and `item_count`. Version 1 rows stay as written and still verify.
- `bootstrap(profile="compact", section="activity")` returns `{since, as_of, note_count, events}` to the owner. Unknown counts are null, never zero.
- On a cloud cell, the caller's client name comes from a gateway-set header after `clientInfo` and before `User-Agent`.
- On a cloud cell, the applied engagement context comes from the control plane's client registry, delivered in a second gateway header, before name detection.
- The authenticated gateway supplies a canonical grant UUID in `x-exomem-app-id`. Schema 2 rows and owner activity events gain optional `app_id`. Events group by app ID, family and action. Older and unresolved rows report null.
- The cell registers no new route. Only MCP output fields change.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `agent-prominence-control`: report every context's level and the rung that decided it; on Cloud, take the context from the control plane's client registry.
- `call-ledger`: record access, item count and optional actor ID without content, and keep reading older rows.
- `cloud-cell`: take the client's name, context and actor ID from the gateway, and serve the owner's activity and note count through MCP.

## Impact

The change touches prominence resolution, `configure_memory`, the bootstrap section list, the ledger row builder, the MCP wrapper, the read dispatcher's disclosure boundary and the caller identity.
An older image refuses the unknown `activity` section, so Home can tell an outdated cell from an idle one.
The Cloud gateway must set the client header; that change belongs to the gateway and ships after the cell image.
