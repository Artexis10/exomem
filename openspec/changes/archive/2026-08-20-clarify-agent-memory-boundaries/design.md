## Context

The current docs already contain most of the underlying Exomem rules, but they
often introduce internal layers and tool names before the user or agent has a
simple routing model. That makes Exomem feel more complex than it needs to feel
for Claude, Codex, ChatGPT, Cursor, and generic MCP clients.

The existing `bootstrap()` contract already exposes a product-facing action
catalog. The docs and scaffold should reuse that vocabulary instead of creating
parallel terms. The implementation is scoped to documentation, scaffold text,
fixture mirrors, and focused tests.

## Goals / Non-Goals

**Goals:**

- Make the native assistant memory vs. Exomem boundary direct and honest.
- Teach agents to map user phrasing to simple actions before exposing internal
  folders or page types.
- Give non-CLI-comfortable users a first-run path that still verifies the setup.
- Include concrete examples for remember, find, preserve source/proof, compile,
  review stale knowledge, and supersede old conclusions.
- Keep scaffold text generic and leak-guard compatible.

**Non-Goals:**

- No MCP, REST, CLI, storage, ranking, or model behavior changes.
- No new dependency, background service, or client-specific integration.
- No claim that Exomem replaces all built-in assistant memory features.

## Decisions

1. Use simple product actions as the front door.

   Agents should first think in terms of `ask`, `save`, `prove`, `review`,
   `update`, and `connect`. Internal operations such as `find`, `add`, `note`,
   `preserve`, `audit`, `edit`, and `replace` remain the implementation layer.
   Alternative considered: document only tool names. That keeps precision but
   forces users to understand schema internals.

2. Keep the memory boundary as a two-layer model.

   Native assistant memory/custom instructions hold preferences, style, identity
   facts, and the instruction to use Exomem. Exomem holds governed durable
   knowledge with provenance, review, and supersession. Alternative considered:
   positioning Exomem as a complete replacement for built-in memory. That would
   be inaccurate and would weaken the docs.

3. Put first-run user guidance in quickstart, and agent behavior in the assistant
   guide/scaffold.

   `QUICKSTART.md` should help a non-CLI user ask an assistant to drive setup and
   verification. `docs/ai-assistant-guide.md` and the scaffold skill should teach
   agents what to do after tools are connected.

4. Mirror scaffold edits into test fixtures.

   Tests load schema docs from `tests/fixtures/Knowledge Base/_Schema`. Any
   canonical scaffold changes that affect parsed or copied schema text must be
   reflected there so fixture-backed tests keep representing shipped text.

## Risks / Trade-offs

- Internal terminology may disappear too far from agent guidance -> keep a short
  "implementation mapping" table for agents.
- Docs may drift from `bootstrap()` -> reuse the same front-door action names and
  run `test_bootstrap.py`.
- Scaffold edits can introduce private tokens or path leaks -> run
  `test_scaffold_no_leak.py`.
- Quickstart can become too long -> add a compact non-CLI path and leave deep
  operational detail in existing setup sections.
