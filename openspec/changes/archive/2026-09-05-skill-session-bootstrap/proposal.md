## Why

A skill-aware session needs the current engagement envelope and adapter capabilities, but the compact bootstrap repeats the static authoring contract already loaded from the skill. This consumes much of the context saved by progressive skill loading.

## What Changes

- Add an opt-in `session` bootstrap profile for clients that already loaded the installed skill's operating rules.
- Preserve live policy, capabilities, workflow and vocabulary currency, selected guidance, and due state while omitting duplicate static teaching.
- Route installed skills to this profile; retain compact bootstrap for generic clients, missing procedures, and older servers.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `agent-bootstrap-contract`: provide a skill-aware session projection alongside the existing portable contracts.
- `skill-loading`: fetch live session state without reloading static instructions and retain a compatible fallback.

## Impact

Bootstrap command and discovery description, canonical skill scaffold, generated Claude plugin, contract tests and OpenSpec. No new dependencies or model execution; all state continues through existing deterministic resolvers and disclosure filtering.
