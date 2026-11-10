## Why

The MCP surface lists 40 tools, and an agent loads about 168 KB of definitions before its first call. Agents use few of them. Across a sample of 20 real sessions, every grouped tool was called through one operation only. The nested `operation` unions are the largest definitions, and the guard fields that they carry (hashes, tokens, fingerprints, draft ids) force a preview call before most writes. The names also mislead: "record" means both a Records row and a session recap, and "capture", "preserve" and "transfer" name one job three ways.

## What Changes

The decision: **the agent surface lists 23 typed tools and two doors, and an internal command name is no longer callable.**

- **23 listed tools.** recall_context, search_memory, read_page, read_instructions, create_note, add_observation, edit_note, supersede_note, save_source, save_evidence, save_episode, upload_files, download_files, query_records, add_record, edit_record, query_plan_items, add_plan_item, edit_plan_item, list_review_items, decide_review_item, inspect_memory and manage_memory. A Cloud surface also lists observe_transfer and transfer_memory.
- **Typed tools are flat.** Each tool has one job, one effect class and typed fields. `edit_note` is the one tool with an `action` selector.
- **Two doors.** inspect_memory carries every other read. manage_memory carries every other write. Each takes `{"operation": "area.action", "arguments": {...}}`. `operation` is a closed enum generated from the operation entries. inspect_memory is read-only, and manage_memory has no `preview`: the read twin of an operation on inspect_memory is its dry run.
- **On-demand help.** `read_instructions(topic)` returns the arguments and one example for a tool, the operations of a door, or the schema of one door operation. A wrong door call returns a `retry` and `candidates`.
- **Server-derived guards.** An agent copies no page, container or relation-review hash; only the optional `vocabulary_fingerprint` of a vocabulary decision stays a field. A short id carries the identity and the version that the agent saw: a unit id for an observation, a row id for a record or plan item, a review item id, and the page id of the earlier change. `unlinked_reason` replaces the relation-review triple. `preview` replaces `validate_only`, and `detail` (`compact` or `full`) replaces `response_detail`.
- **Old names are refused.** A call to an internal command name, to a retired tool name or to a legacy alias returns the refusal for an unknown tool. The compatibility aliases leave the server.
- **Cloud.** The artifact grant check compares the called tool (`save_source` or `save_evidence`) with the grant. Both Cloud transfer tools stay listed until the gateway routes door calls.
- **Machinery that moves with the names.** Server instructions, bootstrap routes, hooks and their nudges, the scaffold skill, the plugin and Cloud-plugin generators, tool-schema fixtures, the byte budget, the ChatGPT connector contract and the end-to-end proof script.

## Impact

- Affected specs: `command-surface`, `semantic-write-contract`, `agent-bootstrap-contract`, `client-artifact-preservation`, `mutation-terminal-contract`, `attention-queue`, `get-payload-shape`, `retrieve-inject-hook`, `skill-loading`, `portable-agent-contract`, `attachment-source-ingestion` and `release-gate`.
- Affected code: a new `public_surface` module for the tool and door tables; `operation_entries`, `command_surface`, `server`, `capabilities`, the leaves in `commands`, `observe_memory`, `records`, `plans` and `review`; `cloud_artifact_transport`; both hooks and their plugin copies; the scaffold.
- A client that calls an old tool name gets a refusal. Plugin, skill and server ship together. The skill-contract digest pins the pair.
- The tool-surface digest moves once. The ChatGPT draft needs one refresh after release, because ChatGPT offers no API for it.
- REST and CLI keep their names, fields and reply shapes.
- Deferred: the proposal's `service.*` operations and `memory.export` (new MCP capabilities), `[[Title]]` rendering of stored bodies, and one-call folder import.
