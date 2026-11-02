## Why

A user connected two Exomem connectors to one assistant: their personal Exomem and Exomem Cloud. They asked for the personal one. The assistant used the Cloud connector, received committed write receipts, and told the user the profile was saved. The personal vault stayed stale.

Nothing let the agent or the user tell the two stores apart. Both connectors present the display name "Exomem" and the same skill text. The bootstrap names no store. The write sentence reads "Saved -> <path>" on both.

## What Changes

- Add one function, `cloud_cell.store_identity()`, that returns the `kind` (`personal`, `hosted` or `cloud`) and the `name` of the store a process serves.
- The bootstrap carries `store` in the session profile and in the complete compact reference. The always-served compact core does not carry it: its populated worst case has 13 bytes under the 15,000-byte ceiling.
- `user_summary` ends with the store name in parentheses, for example "Saved -> <path> (Exomem Cloud)". The replace sentence changes from "(was old)" to ", was old", so it reads "Replaced -> new, was old (Exomem Cloud)".
- The skill scaffold tells the agent to use the connector the user named, to say plainly when it is unavailable, and never to report a write in a store other than the one `user_summary` names.

The public display name stays "Exomem". No plugin definition changes.

## Impact

- Changed specs: `mutation-terminal-contract`, `agent-bootstrap-contract`.
- Changed code: `src/exomem/cloud_cell.py`, `src/exomem/mutation_terminal.py`, `src/exomem/commands.py`, `src/exomem/bootstrap_core.py`, and the skill scaffold text.
- A client that parses `user_summary` by prefix keeps working. A client that matches the whole sentence sees the new suffix.
