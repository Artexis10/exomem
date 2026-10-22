## Why

The ChatGPT plugin directory scans the Exomem MCP server every day and holds back tools with findings. On 2026-10-22 the scan reported two kinds of finding that this change can fix without changing the tool set:

- `activate_context` "requests information beyond what its stated operation requires". OpenAI's plugin guidelines say: "Do not request accumulated conversation context or prior-turn arrays." The MCP schema advertises `conversation.recent`, a list of up to six earlier turns. It also advertises `client`, `session` and `workspace`, which only rank a turn that names nothing.
- Nine tools "execute operations that are not individually exposed". Five of them (`connect_memory`, `schema_memory`, `manage_memory_file`, `review_memory` and `adoption_studio`) publish their selector as a free string. The accepted values appear only in prose, so the schema looks like a generic executor.

The model does not need `recent`. The shipped retrieval hook sends earlier turns through the REST facade and the CLI, not through MCP. A remote agent passes `focus` and `refs`; the conversation acceptance benchmark already holds the focus arm to its floors. `session` falls back to the continuity token's thread, which `attribution_for` already derives.

## What Changes

- The MCP door of `activate_context` advertises and accepts only model-facing inputs: `turn`, `anchor`, `continuity`, `trigger`, `initiator`, `purpose`, `max_chars`, `include_timings`, and `conversation` with `focus` and `refs`. It no longer advertises `client`, `session`, `workspace` or `conversation.recent`, and it drops them when a stale client still sends them. The CLI and REST doors keep every field, so the hook path does not change.
- Every MCP selector of a mixed read/write product command publishes its accepted values as a JSON Schema `enum`. The values come from the existing selector registry (`governance.egress.selector_registry()`), which already classifies each value for lease and egress coverage.
- The OpenAI manual-review justification for the tools that remain combined is drafted outside the repository. The operator submits it.

The tool set, tool names and annotations do not change. Splitting each operation into its own tool stays open until OpenAI answers the manual review.

## Capabilities

### Modified Capabilities

- `conversation-aware-activation`: the MCP door accepts `focus` and `refs` only. `recent` stays on the CLI and REST doors.
- `command-surface`: mixed command selectors publish their closed value set.

## Impact

- `src/exomem/command_surface.py` (`bind_vault`), `src/exomem/working_set_conversation.py` (public schema).
- The MCP schema fixture and the tool surface contract are regenerated with `scripts/dump-tool-schemas.py`.
- MCP clients that still send `recent`, `client`, `session` or `workspace` keep working; those fields are ignored on MCP.
