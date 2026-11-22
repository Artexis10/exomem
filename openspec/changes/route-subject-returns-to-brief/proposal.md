# Proposal

## Why

The topic brief (`read_page(part="brief")`) returns the whole compiled body of a subject in one call. An agent uses it only when something tells it to. A user who returns to a subject they researched before usually says so in plain words ("bring back what we found on X"), and today the agent answers with recall, search and a fresh synthesis instead.

The first attempt to route these requests put one line in an evaluation copy of the client skill. A development evaluation on 2026-11-22 showed that the line never reaches the model: an agent opens the skill only when it decides to, and for these requests it did not, in any run. The agent followed the server's instructions instead. The server's instructions are the one text that every MCP client receives at `initialize`, so the routing line belongs there.

## What Changes

- Add one clause to the server's instructions (`SERVER_INSTRUCTIONS`): when the person asks to bring back or recap a subject they researched before, read its brief first and follow `more`, then use recall or search only for gaps.
- Keep the clause short. Some clients cut long server instructions off, so the clause adds the fewest words that name the request, the call and the gap rule.
- Evaluate the clause before release against today's instructions, with the protocol of the closed `add-topic-brief` evaluation, once the brief serves on the personal service. A brief call that answers `index_warming` is an infrastructure failure of that run, not a result.

## Capabilities

### Modified Capabilities

- `topic-brief`: adds the requirement that clients are routed to the brief for a return to a researched subject.

## Impact

- `src/exomem/server.py` (`SERVER_INSTRUCTIONS`) and the tests that pin its delivery to every client surface.
- The client skill references keep documenting the brief and carry no routing line.
- No change to the brief itself, its caches or its egress.
