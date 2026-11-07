## Why

The server already decides, per principal and vault, whether an agent may save durable material unasked. It serves that decision in the bootstrap contract. Agents do not call bootstrap before they answer, and the hooks and the MCP server instructions that point at it are static. A live-model trial on 13 synthetic cases found that agents ignored the static pointer in 12 of 13 sessions, and the Stop capture check converted 1 of 7 positive cases.

Two further gaps showed in the same trial. Section edits refuse an absent heading with no route to create it, so an agent that wants to add a compact unit line to an Entity page falls back to an arbitrary-anchor string replace. The Entity refusal in `observe_memory` names no retry at all.

## What Changes

- `activate_context` attaches one `capture_policy` block to its packet when the caller is owed it: the request has no continuity token, or the token names no policy digest or a different one. The response's token names the current digest, so the server keeps no delivery state and the block is sent once over the stateless transport too. The block states the effective `proactive_capture` disposition in plain words and, unless that disposition is `off`, the durable-capture classes, their routing and the quiet negatives. The server builds it from the constants that bootstrap serves and from the same resolved gate.
- The retrieve hook renders a served block first, under its own policy header, in working-set mode. The Stop capture check, the MCP server instructions and the bootstrap bytes do not change.
- `edit_section` accepts an optional `create_missing` boolean. When true and the heading is absent, the edit appends a new level-2 section under the same hash guard. Without the flag, `HEADING_NOT_FOUND` names the flag.
- The `observe_memory` refusal for an Entity target names the exact `edit_section` retry, spelled with the edit tool that the serving surface lists.
- The scaffold operations reference names the compact-unit route under `## Observations` for a durable personal baseline.
- The active `capture-durable-personal-baselines` delta no longer lists the capture reminder as a carrier of the baseline predicate. It names the activation block.

## Impact

- Hooked and hookless clients that call `recall_context` see the live disposition once per continuity token, again after the caller drops the token (after a compaction, for example) and after a policy change. A packet that carries no token, such as the abstention under the working-set kill switch, carries the block on every request. Worst case is under 800 bytes.
- The packaged tool-surface digest changes (a new `edit_section` field), so the ChatGPT Personal Plugin needs its ordinary post-deploy refresh. The `edit_note` ceiling in the schema budget rises 50 B for the measured 52 B; the total wire budget rises 50 B, to 168,050 B, because the `record_episode` close step and the new flag together pass the old budget.
- An old installed hook with a new server ignores the field. A new hook with an old server renders nothing new. The plugin ships in the same release.
- No vault data, registry or bootstrap bytes change.
