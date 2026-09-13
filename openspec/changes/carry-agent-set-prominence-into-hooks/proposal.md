## Why

The engagement level a user saves through `configure_memory` changes the prose Exomem serves but not the cadence of the standalone nudge hooks, which read only the operator environment and the machine configuration file on the client. A Claude Code user who saves `off` is told by `bootstrap` never to write on their own initiative while the Stop hook keeps injecting the capture reminder every few minutes. Two smaller gaps sit beside it: on surfaces without `configure_memory` the bootstrap payload still names the CLI as the way to change the level, and an unreadable preference record silently resolves to the hookless `maximal` default, so a saved `off` fails open toward more proactivity. All three were found by the independent review that closed `add-prominence-levels` task 8.1.

Mirroring the saved level into the machine configuration file was the first answer and it is the wrong one. The preference lives on the server; the hooks run on the client. The hooks cannot ask the service — they do not import the package, the retrieve hook's REST rung is loopback-only by design, and a remote client holds no credential — so a server-side mirror writes the server's own machine file, which is the client's file only when the two are the same box. Any design that silences the hooks from the server is a promise that quietly holds on one deployment and quietly breaks on the common one.

What is available on every deployment is honesty. A client that runs hooks can be told what its hooks read and where to change it.

## What Changes

- Serve a `hook_cadence` block inside `engagement` to every client in the coding context: what the hooks resolve from, that the saved preference is not among it, and the command that changes the cadence on the client machine and nothing else. Bootstrap and every arm of `configure_memory` carry it; the conversation context carries nothing, because a client with no filesystem has no hooks to be out of step with. The gate is the context rather than a list of client names, so an unrecognized hooked client -- a new CLI, a fork, a local install -- is not silently excluded.
- When `configure_memory` is absent from the served command set, make the bootstrap `change_with` guidance name the custom-instructions path instead of the CLI.
- When the preference record cannot be read, resolve to the conservative `balanced` default rather than the client default, name `preference:unavailable` as the source, and withhold proactive capture in every projection of capture authority: the capture gate, the served contract, the delegation envelope's `proactive_capture` class, and the workflow contract's effective capture. The envelope is the authority surface an agent acts on, so a floor that held only in the gate beside it would be the same fail-open one key over.
- Tell the agent, in the scaffold engagement reference, to pass the cadence caveat on to the user after a set on a hook-capable client.
- Add the precedence, projection and byte-budget tests that prove each behaviour through the real bootstrap projection and the real preference control.

## Impact

- Affected specs: `agent-prominence-control` (modified: precedence and defaults; consistent contract projection and guidance).
- Affected code: `src/exomem/prominence.py`, `src/exomem/commands.py` (`configure_memory`, bootstrap `change_with`), `docs/prominence.md`, `src/exomem/_scaffold/_Schema/references/engagement.md` and the skill-contract digest it re-stamps.
- No change to write scope, authority, or compute mode. The nudge hooks themselves are unchanged: this change makes the discrepancy visible rather than pretending to close it.
- The compact bootstrap absorbs the block: measured 62,431 bytes for a Claude Code client against a 63,300 ceiling, with the `record` route at offset 10,945 against a 12,288 proxy. The largest compact payload remains the conversational one at 62,511 bytes, which this change does not touch.
