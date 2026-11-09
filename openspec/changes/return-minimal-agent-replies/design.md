## Context

Replies reach the agent through four places: the mutation terminal (every write), the search command, the MCP refusal wrapper, and the bootstrap text that teaches the agent to read them. Each place already has one seam, so this change edits each seam once and no tool.

## Decisions

### One seam per reply kind

- **Write replies**: `mutation_terminal.project_terminal` is the only projection, called from `writer_lease.LeaseManager.invoke`. Its compact branch builds the agent reply.
- **Search replies**: `commands.op_ask_memory` shapes the reply for `search_memory`.
- **MCP refusals**: `command_surface.agent_refusal_result` calls `agent_refusal.reply`. The `bind_vault` wrapper and the two rendered-tool refusal middlewares in `server` use it. The CLI and REST facades keep `cli_ops.public_refusal` and `{success, data}`.
- **Teaching text**: the bootstrap guidance in `commands` and the scaffold references describe the new reply.

### The operational key split is a closed set

`mutation_terminal._OPERATIONAL_KEYS` names the terminal's own fields: `ok`, `state`, `terminal`, `status`, `mutated`, the identities, hashes, sync fields and timings. The compact reply drops them. `response_detail=full` puts them under `diagnostics.operational` and the complete leaf beside them. The set belongs to the terminal's fixed shape, not to a vault, so it is code, with a reason in the source.

### Receipts keep the hashes the next guarded write takes

The next guarded `edit_memory` or `observe_memory` write takes a page's `after_hash` as `expected_hash`, so the reply to a page write keeps it and the agent does not read the page between two edits. The next guarded `record_memory` or `plan_memory` write takes `after_container_hash`, `after_item_hash` and `after_manifest_hash`, so those three stay as well. The egress filter also binds a reported `unit_ref` to its page by `after_hash`. Every other hash is operational.

### Pending sync is silent, failed sync is one notice

A pending derived update is the normal state after a commit, so it gives no notice and appears only in diagnostics. A failed derived, graph or advisory update gives one generic notice: the change is saved and may be missing from search results. An unknown or partial outcome never reads as complete.

### Trimmed warnings stay visible

`warnings_count` stays in the compact reply only when it exceeds the warning texts shown, so a trimmed list never reads as the whole.

### One identifier per object

A hit carries its path and its page `ref`, because `read_memory` takes either and `record_episode` takes only refs in `about`. It drops the `scope` label and a unit's `parent_ref`. Review refs, fingerprints and hashes that the next call does not take stay out of the default reply. A title is not an identity here.

### Search has no `response_detail`

`search_memory`, `read_memory` and `recall_context` take no `response_detail`, and this change adds none. Index and warming state become one `notice` sentence, and only when results may be incomplete. The raw `warming`, `degraded` and `budget` blocks appear only when the call asks for timings or an explanation. `next_action` and `continuation` stay on search because the tool descriptions name them.

### The capture sweep is one sentence plus data

The rule text becomes the `next_action` sentence, sent once per episode through the existing quiet-interval rule. The names the page reached for that have no page (`unpaged_mentions`) are data the pass acts on, so they stay as a top-level list beside `next_action`. The rule, boundary and `written_recently` fields stay in the leaf. A write that has its own `next_action` carries both sentences in it, its own first. `response_detail="full"` carries the block under `diagnostics.capture_sweep`.

### Refusals

- `message` says what was refused. A refusal with no `retry` carries its whole remediation next, because that text is the only fix the agent gets; a refusal with a `retry` keeps its message short. For a refused write the message ends with what happened to the data: nothing saved, saved, or not known. A producer that keeps something, such as a held candidate, states its own outcome sentence.
- `retry` holds the tool name, `arguments` and `after_s`. A refusal that advises a wait carries `retry` even when its status is not `retryable`, because warming recall advises a wait without that status.
- `retry.arguments` is a delta. The agent merges it key by key, nested objects included, into the arguments it sent, and a `null` clears one. A retry to a different tool holds the whole call. The producer states the next call in command terms (`retry_with` for a patch onto the refused call, `retry_call` for a different call), and the seam spells it once with `capabilities.active_spell_call`, so argument placement comes from the serving surface and no producer or seam guesses it. A resumable handle (a held candidate's id, a running job's id) goes in `retry`.
- `candidates` come from the producer that knows the valid values and are capped at five. A refusal that names the closed set a refused argument accepts (`allowed`) supplies them next. A producer that only ranks `options` by `key`, such as the generic-relation barrier, supplies them last.
- A refusal that names the field it failed on (`at`) and its repair keeps both in the message, because the agent cannot correct the call without them. A refusal that flags itself `retryable` carries `retry`.
- The umbrella `SEMANTIC_CONTRACT_BLOCKED` moves to diagnostics. The code of the finding the contract blocked on becomes the code.
- Remediation prose moves to diagnostics, except when the refusal has no `retry`: then the whole of it stays in the message. Where a route is still named, it uses the called surface's `capabilities.active_call_text`.
- A refusal reaches `diagnostics` only on a tool that has the `response_detail` parameter. Read tools and rendered tools have none, so their refusals show the compact error only. A command that defaults to `full` (`govern_memory`) refuses with `diagnostics` when the call names no detail.

### Reuse check

The MCP spec has an `isError` flag on tool results. This change does not set it: clients already read the `error` key, and the flag changes how some clients render the result. A later change can add it.

## Risks and debts

- **Programs that read old keys.** The records live acceptance, the product end-to-end loop, the cell memory profile and the live write acceptance now ask for `response_detail=full` or read the new refusal shape. The benchmark and acceptance consumers that read the old search or write reply shapes read the object search reply and the compact write reply.
- **Dead producers.** The write-side due-state producers still compute a block that the terminal now drops. Removing them touches the records and semantic-write leaves and is a separate change.
- **A latency row without samples.** `governed-stage-due_state` in the request latency contract reads "not run" for search now that search runs no due-state stage.
- **Multi-sentence legacy messages.** Refusals whose producer already writes a multi-sentence message keep it. Only the shared seam and the common codes were rewritten.
- **Placeholders in a resumed records retry.** A held-candidate retry carries a `<corrected field>` placeholder for each failing field that is not an undeclared one. Calling it unchanged refuses again, which tells the agent the value is its own to supply.
