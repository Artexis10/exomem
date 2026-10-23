## Why

The ChatGPT plugin directory scans the Exomem MCP server every day and holds back tools with findings. On 2026-10-22 it reported that nine tools "execute operations that are not individually exposed". OpenAI's plugin guidelines say: "Do not use discovery, operation selection, or schema fetching with a generic executor".

Five of those tools publish their operation selector as a free string: `connect_memory`, `schema_memory`, `manage_memory_file`, `review_memory` and `adoption_studio`. Their accepted values appear only in prose, so their schemas look like generic executors. The server already holds the closed set: the selector registry (`governance.egress.selector_registry()`) classifies every accepted value for the writer lease and for egress coverage, and refuses any value outside it.

## What Changes

- Every MCP selector of a product command that the selector registry covers publishes the registry's values as a JSON Schema `enum`. Seven tools gain an enum: the five above, plus `observe_memory` and `adopt_vault`.
- A selector whose leaf signature already declares a `Literal` keeps it. The server build fails if that `Literal` disagrees with the registry.
- Two selector descriptions that repeated the value list now say which values write: `connect_memory.operation` and `manage_memory_file.operation`.
- The tool-schema byte budget rises by 650 bytes for the enums.

Nothing else changes: no tool is added, removed or renamed, and no capability is removed. `activate_context` keeps every input. Whether to split operations into separate tools, and whether to change `activate_context`, stays open until OpenAI answers a manual review.

## Capabilities

### Modified Capabilities

- `command-surface`: mixed command selectors publish their closed value set.

## Impact

- `src/exomem/command_surface.py` (`bind_vault`), two docstring lines in `src/exomem/commands.py`.
- Regenerated: the MCP schema fixture, the tool surface contract and the pending digest in `deploy/chatgpt/personal-plugin-contract.json`.
- An MCP call with a selector value outside the registry now fails input validation. Before this change, the same call reached the dispatcher and failed with `RECEIPT_OUTCOME_MISSING`.
