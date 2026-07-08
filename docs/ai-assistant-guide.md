# Exomem assistant guide

This guide is for agents using Exomem after the MCP server or connector is
available. It keeps the contract simple:

- Use the assistant's native memory or custom instructions for preferences and
  routing rules.
- Use Exomem for durable governed knowledge: project context, decisions, sourced
  conclusions, failures, experiments, proof-bearing records, and anything that
  should be searchable, citeable, reviewable, or supersedable across clients.
- Do not make the user choose internal folders or page types unless the choice
  changes the outcome.

## Start here

If the Exomem skill has already been loaded, follow it. Otherwise call:

```text
bootstrap(profile="compact")
```

Do this once at the start of a session for generic MCP clients, hosted chat
clients, Cursor, Codex, ChatGPT, scripts, and future clients that only see tool
schemas. Use `bootstrap(profile="full")` when the client needs examples. Use
`bootstrap(profile="diagnostics")` only for speed, ranking, cache, CPU/GPU,
rerank, or packed-context questions.

## The rule

Search Exomem before answering when the turn touches the user's prior projects,
notes, decisions, sources, failures, experiments, entities, or domains. Stay
quiet for unrelated chat, short control prompts, and follow-ups where the current
conversation already contains fresh KB evidence.

An empty search means "not found with that query and scope." It is not proof that
the knowledge does not exist. Try synonyms, adjacent terms, singular/plural
forms, or `scope="vault"` before saying there is no relevant material.

Save durable conclusions when the conversation lands on one: a decision, solved
problem, diagnosed failure, reusable pattern, stable project fact, or conclusion
that future agents should find. Save a concise compiled note, not a transcript.
Capture raw material separately when provenance matters.

## Simple actions for agents

| User intent | What the agent should do | Typical Exomem action |
| --- | --- | --- |
| "Remember this" | Decide whether it is raw material, a durable conclusion, or both. Save the conclusion as a compiled note; preserve the raw input if it matters. | `note`, sometimes `add` first |
| "Find what I concluded" | Search first, cite relevant hits, and treat misses as scoped misses. Read full pages only when needed. | `find`, then `get` or `find(pack=true)` |
| "Preserve this source" | Keep the raw material with provenance. Do not rewrite it into a conclusion unless asked or clearly useful. | `add` |
| "Preserve this proof/record" | Store as proof-bearing material for a case, claim, receipt, warranty, dispute, or record. | `preserve` or upload |
| "Compile this evidence" | Turn raw sources/evidence into a concise conclusion with links back to the originals. | `note` with citations |
| "Review stale knowledge" | Surface old or low-use conclusions for human review; do not hide, decay, or auto-rank them down. | `audit`, stale-review checks |
| "This replaces the old conclusion" | Supersede the old compiled page instead of creating an unlinked duplicate. | `replace` |
| "Update this small detail" | Patch a compiled page when the claim is still the same. | `edit` |
| "Connect these ideas" | Add links or entity pages only when they clarify retrieval and future reasoning. | `suggest_links`, `link` |

Use internal names only as implementation details. With the user, say "save the
source," "write the conclusion," "preserve the record," "review stale notes," or
"replace the old conclusion."

## Examples

### Remember this

User:

```text
Remember this: for the onboarding docs, lead with simple actions, not schema
folders.
```

Agent behavior:

1. Recognize a durable project conclusion.
2. Save a concise compiled note.
3. Report the path.

```text
Saved -> Knowledge Base/Notes/Research/Exomem/<title>.md
```

### Find what I concluded

User:

```text
What did I conclude about Exomem vs built-in memory?
```

Agent behavior:

1. Run `find(query="Exomem built-in memory boundary", detail="compact")`.
2. Cite relevant hits.
3. If results are thin, retry with adjacent terms such as `native memory`,
   `assistant memory`, `custom instructions`, or `durable governed knowledge`.

### Preserve source

User:

```text
Save this article as a source and keep the URL.
```

Agent behavior:

1. Use `add` for the raw article or excerpt.
2. Keep the URL and capture rationale.
3. Offer to compile a note only if there is a durable conclusion to extract.

### Preserve proof

User:

```text
Keep this receipt for the warranty case.
```

Agent behavior:

1. Treat it as a proof-bearing record, not a research source.
2. Preserve the original file or text under the evidence workflow.
3. Report the stored path and any metadata the server returns.

### Compile evidence

User:

```text
Compile the notes from these two sources into what we learned.
```

Agent behavior:

1. Search or read the named sources.
2. Draft a compiled conclusion that links back to the sources.
3. Use `suggest_links` before writing so the new note connects to prior work.

### Review stale knowledge

User:

```text
Show me conclusions that may be stale.
```

Agent behavior:

1. Run the stale-review audit path.
2. Present candidates as review items only.
3. Ask whether to keep, edit, supersede, or archive. Do not auto-decay old
   knowledge.

### Supersede old conclusion

User:

```text
The old setup recommendation is wrong now; this new one replaces it.
```

Agent behavior:

1. Find and confirm the old compiled page.
2. Use `replace` to write the new conclusion and mark the old page superseded.
3. Surface downstream pages that may need review; do not silently rewrite them.

## Client setup

| Client | Connection | Behavioral layer |
| --- | --- | --- |
| Claude Code | Local MCP via setup or manual config | Exomem skill, optional hooks |
| Codex CLI | `codex mcp add ...` | `AGENTS.md`, optional hooks, `bootstrap()` fallback |
| ChatGPT or hosted chat clients | Remote MCP/connector when supported | Custom instructions plus `bootstrap()` |
| claude.ai web/mobile | Remote connector | Custom instructions plus `bootstrap()` |
| Cursor, Windsurf, Gemini, generic MCP clients | Stdio or remote MCP config | `bootstrap()` at session start |
| Scripts and custom chatbots | MCP, REST, or CLI | Cache `bootstrap()` per process/session |

## Copyable instruction block

Paste this into `AGENTS.md`, project instructions, or hosted chat custom
instructions. Trim the tone line to your preference.

```text
Use Exomem as my durable Knowledge Base.

If no Exomem skill is loaded, call bootstrap(profile="compact") once at the
start of a session and follow the returned contract.

Search Exomem before answering when a turn touches my prior projects, notes,
decisions, sources, failures, experiments, or domains. Do not search on unrelated
chit-chat, short control prompts, or follow-ups where the current conversation
already contains the needed KB evidence. Cite relevant hits. Treat an empty
search as a scoped miss, not proof of absence; retry with better terms or
scope="vault" when absence matters.

Save durable conclusions on your own: decisions, solved problems, diagnosed
failures, reusable patterns, and stable project context. Save concise compiled
notes, not transcripts. Preserve raw sources or proof-bearing records separately
when provenance matters. Use edit for small corrections and replace when a newer
conclusion supersedes an older one.
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
there, or keep an equivalent policy. Restart Codex sessions after changing MCP
or hook config.

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

## Hosted chat clients

Hosted clients usually cannot use local filesystem hooks or load the repo's
Claude Code skill. Connect Exomem through whatever remote MCP/connector support
the client offers, then add the instruction block above as account-level or
project-level custom instructions.

If the hosted client cannot reliably call `bootstrap()` on its own, start a new
chat with:

```text
Call Exomem bootstrap(profile="compact") once, then use that contract for this
chat. Do not search the KB unless this turn touches my prior projects, notes,
decisions, sources, failures, experiments, or domains.
```

## Performance guidance

| Use case | Tool shape | Meaning |
| --- | --- | --- |
| Normal lookup | `find(detail="compact", rerank=false)` | Cheap routing recall |
| Reasoning | `find(pack=true)` | Bounded context assembly for synthesis |
| Diagnostics | `find(detail="compact", include_timings=true, rerank=true)` | Explain latency and ranking behavior |

Resource mode is separate from search knobs:

| Mode | Meaning |
| --- | --- |
| `quiet` | Low-resource CPU mode; avoid warm-up and release models when idle |
| `normal` | CPU-first default; keyword/BM25 recall is ready first |
| `performance` | Explicit opt-in for GPU-capable steady-state work |

Do not interpret a slow diagnostic search with rerank enabled as "Exomem is
slow" without checking timings, resource mode, cache state, and whether a model
was cold.

## Verification

After setup:

```bash
exomem doctor
exomem install-hook --check
```

Then ask the client to call `bootstrap(profile="compact")`. A good response
includes a `contract_version`, server compute policy, tool defaults, and the
search/save workflow. Ask one known vault question next and confirm it uses
`find()` before answering.

For remote connectors, use [remote-quickstart.md](remote-quickstart.md). For the
memory boundary, see [vs-built-in-memory.md](vs-built-in-memory.md).
