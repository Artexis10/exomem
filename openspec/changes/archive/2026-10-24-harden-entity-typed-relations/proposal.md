## Why

Entity creation can author typed relations, but three gaps remain. Text in the summary or `why_in_kb` can open a code fence that hides the `## Relations` section, so the page keeps no edge although the call reported success. A REST caller that sends `relations` as an object has the value ignored without a refusal. A reviewed curation plan cannot author typed edges, because its create-entity step refuses the `relations` field. The scaffold page templates also show `relates_to` as the only relation example, which teaches agents to default to the generic edge.

## What Changes

- Entity creation parses its rendered page once and refuses before any write when an authored edge does not survive as a canonical relation bullet.
- `relations` that is present but not a list is refused. An empty list stays the unset value on every operation.
- A curation plan's create-entity step accepts `relations` and passes them to the same entity writer.
- Scaffold page templates show a specific registered relation for each page type. A short note states that each line is an example, that `relates_to` is for a real generic connection, and that no edge is right when no meaning is known.

## Impact

- `src/exomem/link.py`, `src/exomem/commands.py`, `src/exomem/curation.py`.
- Scaffold `references/page-types.md` and its generated skill, plugin and Cloud copies.
- No storage, graph schema, tool schema or registry change.
