# Proposal

## Why

Exomem's collections exist so that agents can keep and query a user's things exactly. The product decision is that agents run these structures, and the user sees only their own things: the grinders they compared, the quotes they collected, the plants they water.

Two gaps stand in the way.

- **Agents cannot reshape what they built.** The change `capture-structured-work-automatically` lets an agent create a minimal collection under the owner's capture setting. It also lets the agent add fields to it. Every other change of shape stays confirm-required: renaming or retyping a field, changing a natural key, merging two collections, splitting one, retiring one. Several of these have no operation at all. `revise` refuses a natural-key change with `IMMUTABLE_COLLECTION_REPRESENTATION`, and no operation merges, splits or retires a collection. So a later fact that needs a new key either overwrites an earlier row or waits for a question about keys.
- **Receipts and replies leak internals.** A row append's `user_summary` names a vault path, such as `Saved -> Knowledge Base/Records/<area>/<collection>/_summary.md`. The narration rule tells the agent to repeat `user_summary`, so the path reaches the user. Served guidance also tells the agent to "name the collection" in its reply.

## Decision

The decision has three parts.

1. **Agents own the shape of their structures.** An agent may change a collection's shape on its own, under the owner's `proactive_capture` setting, through one lossless and reversible operation. `silent` acts, `advisory` proposes in plain words, and `off` initiates nothing. Permanent deletion is never automatic; retirement replaces it.
2. **Users see their own things in plain words.** Replies and receipts meant for the user name their things, never collections, Records, Planning, schemas, entities or vault paths. The agent tells the user about a change only when it alters what they get.
3. **Agents ask only when a choice is ambiguous and changes the outcome.** They ask one plain question about the user's things, never an implementation question. A user who wants to design a structure can still do so, through the same operations.

A durable spec helps because the rule spans the envelope, the collection substrate, the receipt contract and every served teaching surface. A later change to one of them must not bring back a confirm-first default or a leaked path.

## What Changes

- **One restructure operation.** `change_records` and `change_plans` gain `restructure`. It applies a closed set of steps: add, rename, retype, retire and restore a field; change the natural key; merge, split, retitle, retire and restore a collection. The server proves that each restructure loses nothing before it commits. It records the inverse steps, so one call undoes the change.
- **Retirement replaces deletion.** A retired collection or field keeps all its data and history. It leaves recall, routing and the default inventory, and one call restores it.
- **Bounded automatic restructuring.** With `automatic: true`, the server also checks the capture setting, the field cap, a per-collection daily rate, and that no step reverses a decision the user made.
- **Shape history.** The inventory shows each collection's recent restructures, who decided each one, and its undo call. A user's answer to a question about shape is kept there, including a "no".
- **Plain-words receipts.** A Records or Planning write's `user_summary` names the item and the collection by title, never by path. A restructure's `user_summary` states its effect on what the user keeps. A restructure that changes nothing the user gets carries none.
- **Narration rule.** The one narration rule adds: speak of the user's things in their words, and say nothing of a change that has no `user_summary` unless asked.
- **Capture reminder.** The client capture reminder stops reading a `Saved ->` marker in the agent's reply. It counts a save from the write's tool result alone.
- **Acceptance.** The incident regression suite gains real-model cases for lossless restructuring, plain replies, and the one-question rule.

Out of scope, each a named follow-up: restructuring graph entities and relations, and automatic creation of a Planning collection.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `structured-collections`: the restructure operation, retirement, the automatic bounds, shape history, and the served contract for what reaches the user.
- `records`: a fact that the natural key cannot hold reshapes the collection instead of overwriting a row.
- `planning`: a restructure keeps the Planning core.
- `delegation-envelope`: a lossless restructure follows `proactive_capture`; deletion and lossy changes stay `restructure_execution`.
- `mutation-terminal-contract`: Records and Planning receipts name the user's things; the narration rule keeps internals out of replies.
- `records-release-acceptance`: real-model acceptance for agent-managed structure.

## Impact

- Code: `src/exomem/collection_store/writer.py` and `records.py` (the restructure leaf), `automatic_collection.py` from the parent change (the shared bounds), `plan_memory.py`, `envelope.py`, `operation_entries.py`, `mutation_terminal.py`, `page_names.py`, `governance/egress.py`, `commands.py`, `prominence.py`, `_hooks/exomem_capture_nudge.py`, and the scaffold under `src/exomem/_scaffold/_Schema/`.
- Generated: the skill-contract stamps, the plugin skills, the cloud plugin bundles, and the tool-surface fingerprint.
- Depends on `capture-structured-work-automatically`, which must merge first. Several of its requirements need a refresh when it archives; `tasks.md` lists them.
- Evaluation: `benchmarks/membench/outcome/incidents/`.
