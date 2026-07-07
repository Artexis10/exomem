# Exomem vs built-in assistant memory

Built-in assistant memory and Exomem solve different problems. They work best
together when the boundary is explicit.

## Short version

Use built-in memory for tiny, durable preferences that should shape every chat.
Use Exomem for project knowledge, sourced material, decisions, failures,
experiments, and anything you may need to inspect, cite, edit, supersede, or use
from more than one client.

Built-in memory is usually hidden inside one assistant product. Exomem is a
user-owned Markdown vault served through MCP, REST, and CLI, so the same memory
can be used by Claude Code, Codex, Cursor, hosted chat clients, scripts, and your
own tools.

## Routing table

| Information | Put it where |
| --- | --- |
| Response style, tone, formatting preferences | Built-in memory or custom instructions |
| "Always check my Exomem KB for project/domain questions" | Built-in memory or custom instructions |
| Project decisions and architectural context | Exomem compiled notes |
| Research findings and reusable conclusions | Exomem compiled notes, with sources linked |
| Raw articles, transcripts, screenshots, PDFs, images, audio, or video | Exomem sources/evidence |
| Diagnosed failures and fixes | Exomem failure or pattern notes |
| Experiments and benchmark results | Exomem experiment notes |
| Temporary scratch thoughts that will not matter later | Usually nowhere |
| Calendar tasks, reminders, or operational todos | A calendar/task system, unless they become project knowledge |
| Passwords, tokens, private keys, or credentials | Neither; use a secret manager |

## Why Exomem needs a behavioral layer

An MCP connection only exposes tools. It does not guarantee the agent knows when
to use them. Claude Code can load the Exomem skill. Other clients should call
`bootstrap(profile="compact")` once per session and follow the returned contract.

The practical behavior is:

```text
For questions about my prior work, search Exomem first. For unrelated chat or
small control prompts, stay quiet. Save durable conclusions as compiled notes.
Treat empty searches as query/scope misses, not proof of absence.
```

## Compiled notes vs raw evidence

Exomem separates raw material from conclusions.

Raw evidence belongs in sources or evidence pages: uploaded files, quoted
articles, transcripts, screenshots, datasets, and other artifacts. These should
preserve provenance.

Compiled notes are the durable layer: decisions, solved problems, patterns,
entities, failures, research notes, and experiments. They should be concise and
usable by a future agent without replaying the whole chat.

When a compiled note changes slightly, use `edit()`. When a newer conclusion
supersedes an older one, use `replace()` so the history stays understandable
instead of accumulating duplicates.

## Sharing one memory across clients

Assistant built-in memories do not usually travel across products. A preference
saved in one chat product will not automatically help Codex, Cursor, a script, or
a custom MCP client.

Exomem is the portable layer. Keep only the routing preference in each client:

```text
I keep durable project knowledge in Exomem. Search it when this turn touches my
projects, notes, decisions, sources, failures, experiments, or domains. Do not
search on unrelated chit-chat or when the current conversation already contains
the needed KB evidence.
```

For concrete client setup and the full copyable instruction block, see
[ai-assistant-guide.md](ai-assistant-guide.md).
