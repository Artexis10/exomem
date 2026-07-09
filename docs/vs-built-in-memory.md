# Exomem vs built-in assistant memory

Built-in assistant memory and Exomem solve different problems. They work best
when the boundary is explicit.

## Short version

Use built-in memory or custom instructions as short-term or behavioural memory:
small preferences, routing rules, and working context that should shape every
chat. Use Exomem as long-term governed memory: project context, sourced
conclusions, decisions, failures, experiments, evidence, and anything you may
need to inspect, cite, edit, review, supersede, or use from more than one client.

Exomem is not a replacement for every native memory feature. It is the portable,
user-owned knowledge layer behind Claude, Codex, ChatGPT, Cursor, hosted chat
clients, scripts, and future MCP clients.

## What goes where

| Need | Best home | Why |
| --- | --- | --- |
| Response style, tone, formatting preferences | Built-in memory or custom instructions | These should shape nearly every chat with minimal retrieval cost |
| "Use Exomem for project/domain questions" | Built-in memory or custom instructions | This is a routing rule for the assistant |
| Stable personal identity facts the assistant should always respect | Built-in memory or custom instructions | Small, cross-chat personalization fits native memory |
| Project decisions and architecture context | Exomem | They need provenance, history, edits, and cross-client access |
| Research findings and reusable conclusions | Exomem | They should be searchable, citeable, and supersedable |
| Raw articles, transcripts, screenshots, PDFs, images, audio, or video | Exomem source capture | Raw material needs provenance and should not be rewritten as memory |
| Receipts, warranty records, case documents, or proof-bearing artifacts | Exomem evidence preservation | These need as-received preservation, not summarization first |
| Diagnosed failures and fixes | Exomem | Failures become reusable project knowledge |
| Experiments and benchmark results | Exomem | They need protocol, data, results, and later review |
| Temporary scratch thoughts | Usually nowhere | Do not store noise just because a memory system exists |
| Calendar tasks, reminders, operational todos | Calendar/task system | They are action management, not durable knowledge |
| Passwords, tokens, private keys, credentials | Secret manager | Do not put secrets in assistant memory or Exomem |

## The boundary in one sentence

Native assistant memory tells the assistant how to behave right now. Exomem tells
the assistant what durable, governed knowledge the user has accumulated.

A good native-memory entry is tiny:

```text
Use Exomem for my durable project knowledge. Search it before answering project,
decision, source, failure, experiment, or domain questions, and save durable
conclusions there.
```

The actual project knowledge belongs in Exomem, not in that native-memory entry.

## Why Exomem needs agent instructions

An MCP connection exposes tools. It does not guarantee the agent knows when to
use them. Claude Code can load the Exomem skill. Other clients should call
`bootstrap(profile="compact")` once per session and follow the returned contract.

The practical behavior is:

```text
Search Exomem before answering prior-work questions. Skip it for unrelated chat
or follow-ups where fresh KB evidence is already in context. Save durable
conclusions as compiled notes. Preserve raw sources and proof separately. Treat
empty searches as scoped misses, not proof of absence.
```

## What Exomem adds beyond native memory

Exomem gives agents a governed knowledge layer:

- provenance: raw sources and proof-bearing records stay linked to conclusions;
- editability: small corrections can be patched without losing structure;
- supersession: changed conclusions point back to what they replaced;
- review: stale or contradictory knowledge can be surfaced for judgment;
- portability: the same vault can serve multiple clients and scripts;
- visibility: Markdown files remain user-owned and inspectable.

Native memory usually does not expose enough structure for those jobs. It may be
hidden inside one assistant product, hard to cite, hard to diff, and unavailable
to other clients.

## What Exomem does not replace

Exomem does not replace:

- the current chat context;
- short-lived scratch reasoning;
- assistant style preferences;
- account-level personalization;
- calendars, task managers, or notification systems;
- secret managers;
- the repository as the source of truth for code.

For software projects, the repo remains the source of truth for code and current
implementation. Exomem holds the cross-session layer the repo usually does not:
decisions, rationale, empirical findings, source-backed conclusions, and reusable
lessons.

## Agent examples

| User says | Agent behavior |
| --- | --- |
| "Remember that we chose the simple action model." | Save a concise compiled conclusion in Exomem; do not stuff it into native memory. |
| "What did I conclude about onboarding?" | Search Exomem first, cite the relevant note, then answer. |
| "Save this article." | Capture it as a raw source with provenance; offer to compile only if there is a conclusion. |
| "Keep this receipt for the warranty case." | Preserve it as proof-bearing evidence, not as a general note. |
| "This new approach replaces the old one." | Supersede the old compiled conclusion instead of creating a duplicate. |
| "Always use terse answers." | Put that in native memory or custom instructions, not Exomem. |

For client-specific setup and a copyable instruction block, see
[ai-assistant-guide.md](ai-assistant-guide.md).
