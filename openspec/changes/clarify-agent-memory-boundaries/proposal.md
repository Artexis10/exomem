## Why

Exomem's agent-facing docs expose too much internal structure before agents and
users have a simple mental model. Claude, Codex, ChatGPT, Cursor, and future MCP
clients need a clear boundary between native assistant memory and Exomem, plus a
small set of user intents that map to the right Exomem actions.

## What Changes

- Reframe assistant-facing docs around simple product actions and user intents:
  remember, find, preserve, compile, review, supersede.
- Make the built-in memory boundary explicit and honest: native assistant memory
  is still useful for preferences and routing, while Exomem is durable governed
  knowledge with sources, proof, history, review, and supersession.
- Add first-run guidance for users who are not CLI-comfortable, including what an
  assistant should do after setup to verify the connection.
- Update the shipped scaffold skill and relevant schema references so agents use
  Exomem correctly without asking users to choose internal folders or page types
  unless the distinction matters.
- Keep the change documentation/scaffold-only. No MCP, REST, CLI, or storage
  API change is intended.

## Capabilities

### New Capabilities

- `agent-memory-onboarding`: Agent-facing onboarding and memory-boundary guidance
  for using Exomem across Claude, Codex, ChatGPT, Cursor, generic MCP clients,
  and future clients.

### Modified Capabilities

- `agent-bootstrap-contract`: Documentation will align generic-client guidance
  with `bootstrap()`'s existing operating contract; no bootstrap schema change is
  planned.

## Impact

- Affected docs: `docs/ai-assistant-guide.md`, `docs/vs-built-in-memory.md`,
  `QUICKSTART.md`.
- Affected scaffold: `src/exomem/_scaffold/_Schema/SKILL.md` and relevant
  `src/exomem/_scaffold/_Schema/references/*.md`.
- Affected fixtures/tests: mirrored schema docs under
  `tests/fixtures/Knowledge Base/_Schema/` and focused docs/scaffold tests.
- No new dependencies, services, transport behavior, model behavior, or public
  API surfaces.
