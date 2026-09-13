## Why

The engagement level a user saves through `configure_memory` changes the prose Exomem serves but not the cadence of the standalone nudge hooks, which still read only the operator environment and the machine configuration file. A Claude Code user who saves `off` is told by `bootstrap` never to write on their own initiative while the Stop hook keeps injecting the capture reminder every few minutes. Two smaller gaps sit beside it: on surfaces without `configure_memory` the bootstrap payload still names the CLI as the way to change the level, and an unreadable preference record silently resolves to the hookless `maximal` default, so a saved `off` fails open toward more proactivity. All three were found by the independent review that closed `add-prominence-levels` task 8.1.

## What Changes

- Mirror the resolved engagement level for the local owner into the shared machine configuration whenever `configure_memory` saves or clears a preference, so both hook copies pick it up on their next run without importing the package, and report that mirror in the inspect result.
- When `configure_memory` is absent from the served command set, make the bootstrap `change_with` guidance name the custom-instructions path instead of the CLI.
- When the preference record cannot be read, resolve to the conservative `balanced` default rather than the surface default, and name `preference:unavailable` as the source.
- Add the drift and precedence tests that prove each of the three behaviours through the real hook copies and the real bootstrap projection.

## Impact

- Affected specs: `agent-prominence-control` (modified: precedence and defaults; consistent contract projection and guidance), `prominence-levels` (modified once archived: the level changes behaviour for every tier the user can reach).
- Affected code: `src/exomem/prominence.py`, `src/exomem/prominence_preferences.py`, `src/exomem/commands.py` (`configure_memory`, bootstrap `change_with`), `src/exomem/_hooks/exomem_capture_nudge.py`, `src/exomem/_hooks/exomem_retrieve_nudge.py`, `docs/prominence.md`, hosted renders and the tool-surface contract if the bootstrap payload text changes.
- No change to write scope, authority, or compute mode.
