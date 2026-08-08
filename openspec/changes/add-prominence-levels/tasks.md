## 1. Define The Levels

- [x] 1.1 Add failing tests for the four canonical levels, alias resolution, contract/preset completeness, and monotonic eagerness across `light` → `balanced` → `maximal`.
- [x] 1.2 Add `src/exomem/prominence.py` with the level registry, the three-axis contracts, and the hook preset table.

## 2. Resolve And Persist

- [x] 2.1 Add failing tests for env → config → surface-default precedence, degradation on an unrecognised or corrupt stored value, and the reported resolution source.
- [x] 2.2 Implement resolution mirroring `mode`'s precedence, reading the configuration file explicitly rather than injecting it into the environment.
- [x] 2.3 Add failing tests proving a `mode` write preserves a stored `prominence` and vice versa.
- [x] 2.4 Implement `write_prominence` against the shared configuration file using the same atomic-swap convention as `write_mode`.

## 3. Set The Per-Surface Defaults

- [x] 3.1 Add failing tests for `maximal` on every hookless surface, `balanced` on a local install, and a hook never adopting the hookless default.
- [x] 3.2 Implement surface detection from an explicit override and the hosted-cell signal, defaulting hookless surfaces to `maximal`.

## 4. Make The Level Change Behaviour

- [x] 4.1 Add failing tests for level-supplied defaults, explicit tunables still winning, and `off` stopping both nudges before other work.
- [x] 4.2 Thread the preset into the capture and retrieve nudge hooks as the default layer, leaving explicit environment variables authoritative.
- [x] 4.3 Add the standalone preset and alias tables to both hooks with a drift test asserting they equal the canonical table at every level.

## 5. Project It Into The Agent Contract

- [x] 5.1 Add failing tests for the `engagement` block in `bootstrap` and its survival through a reduced-surface filter.
- [x] 5.2 Emit the active level, source, surface, contract, and level list from `bootstrap`, keeping the contract text free of tool names.
- [x] 5.3 Exempt the contract's behavioural-axis keys from the advertised-tool-reference walker, which would otherwise read `capture` as a command.

## 6. Expose And Document It

- [x] 6.1 Add `exomem prominence [level]` with show, set, and `--hook-env` output, mirroring `exomem mode`'s shape.
- [x] 6.2 Parameterise the skill's `## Proactive engagement` section by level and correct its claim that the skill never runs on hooks.
- [x] 6.3 Expand the hosted skill projection to carry the maximal contract and honour a user-supplied lower level, without naming client-local mechanisms forbidden in public artifacts.
- [x] 6.4 Add `docs/prominence.md` with per-level copy-paste blocks for claude.ai and ChatGPT, each within ChatGPT's per-field character budget, and link it from `README.md` and `QUICKSTART.md`.

## 7. Resync Generated Artifacts

- [x] 7.1 Regenerate the Claude candidate and re-render the OpenAI package and all three directory packets for the changed skills digest.
- [x] 7.2 Run the marketplace release, scaffold-leak, bootstrap, hook, install, and packaging suites.

## 8. Verify

- [ ] 8.1 Obtain an independent code review of the diff.
- [ ] 8.2 Confirm on a real claude.ai project and a real ChatGPT custom-instructions field that the maximal block still drives recall and capture past turn 30, and that pasting the light block visibly quiets it.
- [ ] 8.3 Run the full test suite, lint, and strict OpenSpec validation, then open a ready pull request.
