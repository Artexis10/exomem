## Why

The server already decides, per principal and vault, whether an agent may save durable material unasked. It serves that decision in the bootstrap contract. Agents do not call bootstrap before they answer, and the hooks and the MCP server instructions that point at it are static. A live-model trial on 13 synthetic cases found that agents ignored the static pointer in 12 of 13 sessions, and the Stop capture check converted 1 of 7 positive cases.

Two further gaps showed in the same trial. Section edits refuse an absent heading with no route to create it, so an agent that wants to add a compact unit line to an Entity page falls back to an arbitrary-anchor string replace. The Entity refusal in `observe_memory` names no retry at all.

## What Changes

- `activate_context` attaches one `capture_policy` block to its packet when the caller is owed it: the request has no continuity token, or the token names no policy digest or a different one. The response's token names the current digest, so the server keeps no delivery state and the block is sent once over the stateless transport too. The block states the effective `proactive_capture` disposition in plain words and, unless that disposition is `off`, the vault's `link_acceptance` disposition, the durable-capture classes, their routing, the relation-queue check before an affiliation facet, the hand-over rule (keep every generated file handed to the user), the reported-delivery route and the quiet negatives. The server builds it from the constants that bootstrap serves and from the same resolved gate.
- The retrieve hook renders a served block first, under its own policy header, in working-set mode. The Stop capture check and the MCP server instructions do not change.
- `edit_section` accepts an optional `create_missing` boolean. When true and the heading is absent, the edit appends a new level-2 section under the same hash guard. Without the flag, `HEADING_NOT_FOUND` names the flag.
- The `observe_memory` refusal for an Entity target names the exact `edit_section` retry, spelled with the edit tool that the serving surface lists.
- The scaffold operations reference names the compact-unit route under `## Observations` for a durable personal baseline.
- The active `capture-durable-personal-baselines` delta no longer lists the capture reminder as a carrier of the baseline predicate. It names the activation block.
- `preserve_artifacts` and `capture_source` with an `adoption` envelope redeem a held upload (`exomem-held:` handle) as the plain paths do, and put the hold back when the adoption is refused. Before this, an adoption on a held handle failed with `HELD_UPLOAD_UNAVAILABLE`.

## Impact

- Hooked and hookless clients that call `recall_context` see the live disposition once per continuity token, again after the caller drops the token (after a compaction, for example) and after a policy change. A packet that carries no token, such as the abstention under the working-set kill switch, carries the block on every request. Worst case is 1,175 bytes rendered, under the 1,200 byte bound.
- The packaged tool-surface digest changes (a new `edit_section` field), so the ChatGPT Personal Plugin needs its ordinary post-deploy refresh. The `edit_note` ceiling in the schema budget rises 50 B for the measured 52 B; the total wire budget rises 50 B, to 168,050 B, because the `record_episode` close step and the new flag together pass the old budget.
- An old installed hook with a new server ignores the field. A new hook with an old server renders nothing new. The plugin ships in the same release.
- The blind acceptance run of 2026-11-07 passed 10 of 13 cases. The three failures (a confirmed relation, an adoption under `silent`, a reported delivery) were gaps in the block's wording, so this change also rewords the shared capture doctrine, adds the `link_acceptance` statement and the delivery route, and raises the hook's block cap from 900 to 1,600 characters. The compact bootstrap core changes by -11 bytes at `balanced` and `maximal` and by -271 bytes at `light` and `off` (measured in every level and surface cell, with an empty and a populated vault, against the previous main). It stays under its 15,000 byte ceiling: the compact profile pays for the doctrine with a status-migration note that only the full profile now serves (a `lifecycle-status-registry` delta records this), a duplicated sentence and shorter near-duplicate and vocabulary guidance.
- No vault data or registry changes.
