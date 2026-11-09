## Why

Fourteen product tools select one of 135 operations with a free `action`, `operation` or `mode` value and accept the union of every operation's fields, about 40 optional fields on the largest. A model cannot see which fields an operation takes, and a client cannot tell a read from a write: each mixed tool carries one write-capable annotation.

OpenAI's plugin directory holds back 13 of these tools: "This tool appears to execute operations that are not individually exposed." Its guidelines ask to "Group operations that represent one coherent action" and to "Separate read and write behavior". Anthropic's tool guidance asks for tools with "a clear, distinct purpose". Closed selector enums (archived change `close-mcp-tool-selectors`) did not change the finding.

## What Changes

- A product object is exposed as one read tool and one write tool. Each tool takes a required `operation` object discriminated by `kind`; each variant declares exactly the fields that operation accepts, the way `edit_note` does.
- A read tool contains only operations that cannot change state and is annotated read-only. A write tool's destructive hint is true when any of its variants can overwrite or remove.
- The grouped command stays the execution identity. A new tool normalizes its variant into the grouped command's arguments before authorization, idempotency, the writer lease, the call ledger and egress, so every guard and receipt is unchanged and a retry across spellings replays one write.
- The grouped tool names stay callable and are hidden from discovery, with no automatic expiry.
- This change delivers the pilot: Records become `read_records` and `change_records`, Planning becomes `read_plans` and `change_plans`. The other twelve grouped tools follow in a later change that reuses the same mechanism, after the pilot's tool-selection eval and directory rescan.
- **Correction:** the Records requirement lists nine actions; the command has thirteen (`bulk_upsert`, `discard`, `import` and `history` were added later). The modified requirements state the actual sets.

## Capabilities

### Modified Capabilities

- `command-surface`: Records and Planning are exposed as read and write tools with discriminated operations; grouped names become hidden compatibility spellings; product tools separate reads from writes.

## Impact

- `src/exomem/commands.py` (product registry and the new operation entries), `src/exomem/command_surface.py` (union rendering, normalization, invocation resolution), `src/exomem/server.py` (registration and discovery), `src/exomem/authorization_transport.py` (route classification by resolved command).
- `src/exomem/record_memory.py` and `src/exomem/plan_memory.py` keep their leaves; their per-action field tables move into the operation registry and are imported back, so each table lives once.
- Regenerated: MCP schema fixtures, the tool surface contract, capability docs, the skill contract, both plugin trees and the pending ChatGPT digest. Byte-budget ceilings change for the four tools.
- Scaffold skill references and bootstrap routes name the new tools for Records and Planning.
- Clients that call `record_memory` or `plan_memory` keep working unchanged.
