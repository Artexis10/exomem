# Exomem assistant guide

This guide is for making an AI client use Exomem well after the MCP server is
connected. The transport gives the agent tools. The behavioral layer tells it
when to search, when to save, how to treat misses, and how to avoid confusing
raw evidence with compiled notes.

Claude Code gets that layer from the Exomem skill and optional hooks. Other
clients should get it from `bootstrap()` plus a short standing instruction.

## Client matrix

| Client | Setup | Behavioral layer |
| --- | --- | --- |
| Claude Code | `exomem setup` or the manual quickstart | Exomem skill plus optional hooks |
| Codex CLI | `codex mcp add ...` | Codex hooks, `AGENTS.md`, and `bootstrap()` as fallback |
| ChatGPT or other hosted chat clients | Remote MCP/connector support, when available | Custom instructions plus `bootstrap()` |
| claude.ai web/mobile | Remote connector | Custom instructions plus `bootstrap()` |
| Cursor, Windsurf, Gemini, or generic MCP clients | Stdio MCP config | `bootstrap()` at session start |
| Scripts or custom chatbots | MCP, REST, or CLI | Cache `bootstrap()` once per session or process |

## The rule

Use Exomem when the turn touches prior projects, notes, decisions, failures,
sources, experiments, or domains the vault may know about. Do not search on
every prompt just because Exomem exists. If the current conversation already has
fresh KB evidence, reuse it unless the user changes topic, asks for a broader
check, or the answer depends on something not yet retrieved.

An empty search means "not found for that query and scope." It is not proof that
the memory does not exist. Try synonyms, adjacent domain terms, singular/plural
forms, or `scope="vault"` before concluding absence.

## Copyable instruction block

Paste this into `AGENTS.md`, a project instruction, or a hosted chat client's
custom instructions. Trim the tone line to match your own preferences.

```text
Use Exomem as my durable Knowledge Base.

At the start of a new session, if no Exomem skill has already been loaded, call
bootstrap(profile="compact") once and follow the returned contract.

Search Exomem before answering questions that touch my projects, notes,
decisions, failures, sources, experiments, or domains. Use cheap recall first:
find(query="...", detail="compact", rerank=false). Follow with get() only for
hits that matter, or find(pack=true) when synthesizing across several hits.

Do not search on unrelated chit-chat, simple control prompts, or every follow-up
when the needed KB evidence is already in the conversation. If a search misses,
try synonyms, adjacent terms, singular/plural variants, or scope="vault" before
saying the KB has no relevant material.

Save durable conclusions on your own: decisions, solved problems, diagnosed
failures, reusable patterns, and stable project context. Save them as concise
compiled notes, not transcripts. Keep raw artifacts as sources or evidence. Use
edit() for small corrections and replace() when superseding an older compiled
note.

For latency or ranking questions, call bootstrap(profile="diagnostics") and use
find(include_timings=true, rerank=true). Distinguish CPU/GPU/low-power mode from
rerank, pack, cold model download, and cache state.
```

## Codex CLI

Add the MCP server:

```bash
codex mcp add exomem --env EXOMEM_VAULT_PATH="/path/to/vault" -- exomem --transport stdio
```

Install the same local hook scripts Claude Code uses:

```bash
exomem install-hook --client codex
```

Check deployed Claude Code and Codex hooks:

```bash
exomem install-hook --check
```

Codex reads repository instructions from `AGENTS.md`. Put the instruction block
above there, or keep your own equivalent policy. Restart Codex sessions after
changing MCP config or hook config.

The hooks are nudges. They should remind Codex to consider Exomem on substantive
turns, not force a tool call for every prompt.

## Generic stdio MCP clients

Use the standard stdio config shape:

```json
{
  "mcpServers": {
    "exomem": {
      "command": "exomem",
      "args": ["--transport", "stdio"],
      "env": {
        "EXOMEM_VAULT_PATH": "/path/to/vault"
      }
    }
  }
}
```

After connecting, ask the agent to call:

```text
bootstrap(profile="compact")
```

Use `profile="full"` when examples help a weaker client. Use
`profile="diagnostics"` when discussing speed, ranking, GPU/CPU behavior, or
model/cache state.

## Hosted chat clients

Hosted clients cannot use local filesystem hooks and usually cannot load the
repo's Claude Code skill. Connect Exomem through whatever remote MCP/connector
support the client offers, then add the instruction block above as account-level
or project-level custom instructions.

If the hosted client cannot reliably call `bootstrap()` on its own, start a new
chat with:

```text
Call Exomem bootstrap(profile="compact") once, then use that contract for this
chat. Do not search the KB unless this turn touches my prior projects, notes,
decisions, sources, or domains.
```

## Performance profiles

These are the defaults returned by `bootstrap()`.

| Use case | Tool shape | Meaning |
| --- | --- | --- |
| Normal lookup | `find(detail="compact", rerank=false)` | Cheap routing recall; follow with `get()` if needed |
| Reasoning | `find(pack=true)` | Bounded context assembly for synthesis |
| Diagnostics | `find(detail="compact", include_timings=true, rerank=true)` | Explain latency and ranking behavior |

Resource mode is separate from search knobs:

| Mode | Meaning |
| --- | --- |
| `quiet` | Low-resource CPU mode; avoid warm-up and release models when idle |
| `normal` | CPU-first default; keyword/BM25 recall is ready first |
| `performance` | Explicit opt-in for GPU-capable steady-state work |

Do not interpret a slow diagnostic search with rerank enabled as "Exomem is
slow" without checking timings and resource mode.

## Verification

After setup:

```bash
exomem doctor
exomem install-hook --check
```

Then ask the client to call `bootstrap(profile="compact")`. A good response
includes a `contract_version`, a server `compute_policy`, tool defaults, and the
search/save workflow. Ask one known vault question next and confirm it uses
`find()` before answering.

For remote connectors, use [remote-quickstart.md](remote-quickstart.md). For
what belongs in Exomem instead of an assistant's built-in memory, see
[vs-built-in-memory.md](vs-built-in-memory.md).
