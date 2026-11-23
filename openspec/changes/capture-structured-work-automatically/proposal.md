# Proposal

## Why

Two incidents on 2026-11-22 show one gap. In the first, several rounds of evaluating a recurring set of named items stayed in a prose note. Each round had stable identities, dated reactions, rejections and research findings. No Records collection existed, so the agent proposed none. The served contract also said that automatic collections stay refused. The user had to ask before the material became 281 queryable rows. In the second, a standing rule and two decisions agreed in conversation reached only files local to one harness. Nothing reached Exomem until the user asked.

Both depend on the agent remembering to capture, and no part of the system makes that the default:

- **Recognition.** The server cannot see the conversation. Its recognisers either read only tagged compiled notes, over a span of at least 14 days and on state words (`collection_candidate`), or route a write into a collection that already exists (`records_routing`). The client capture reminder fires after a commit or a pull request, never after a decision.
- **Authority.** The delegation envelope makes every collection creation `restructure_execution`. That is confirm-required at every prominence, and a standing delegation is refused by name. The envelope text says that only a deliberate founder ratification may change it.
- **Contract.** The served Records contract says that Records are manual-first and that the agent must not create a long-lived schema without asking.

The old refusal no longer matches the product's intent: capture should need no "save this" or "make a collection". This change is the founder ratification that the envelope text asks for. It is narrow: under the owner's capture setting, the agent reuses a compatible collection, and where none exists and identity, type and scope are clear, it creates one minimal collection within fixed bounds. Destructive migrations, deletion, merging and unrelated restructuring keep their current authority.

## Decision

The decision is which authority governs agent-initiated creation of a new Records collection. A durable spec helps because the rule sits in the envelope, the Records contract, the bootstrap contract, the scaffold skill and every client package. A later change of any one of them must not reopen the old refusal or widen it.

The choice: creating one minimal Records collection within fixed bounds follows `proactive_capture`, the same posture that already governs appending to a collection. `silent` creates the collection and reports it in the same reply. `advisory` proposes it and creates it after one inline confirmation. `off` initiates nothing. Everything else stays `restructure_execution`: a collection outside the bounds, any Planning collection, any schema change other than adding fields, a merge, a deletion and a migration.

The same posture governs the first schema change that real use needs. A collection is never complete at its first row, and information that arrives later has no field. Under silent capture the agent then put an outside claim into a field that held something else, because adding a field was a confirmed schema change. Adding fields to a Records collection, and nothing else about its schema, follows `proactive_capture` as well.

## What Changes

- **Bounded automatic creation.** `records.create` takes `automatic: true` for an agent-initiated creation, on a file-only vault and on a store vault alike. The server then checks three things. First, that the caller's effective `proactive_capture` is `silent`. Second, that the manifest is minimal: Records profile, a declared natural key, at most 12 fields counting nested properties, and only the keys that the minimal manifest has, with no references. Third, that the vault has created fewer than 3 collections automatically in the last 24 hours. A refusal names the bound and the way forward: propose the collection and create it after confirmation, or under `off` create it only if the user asked for it. The create's audit entry records that it was automatic, and the inventory reports it, so the owner can review every automatic collection. `add_record` takes `new_collection` to make that creation one call: it drafts the minimal manifest from the row, creates the collection under these bounds and appends the row.
- **Additive schema change.** `add_record` and `edit_record` take `new_fields`, the names in the call's row that the collection does not declare. Under `silent` the server adds each field, typed from its value (every JSON number is `number`), through the existing manifest revision, then writes the row, and the reply names the fields. Under `advisory` or `off` it refuses with the proposal and the confirmed revise. A collection that was itself created automatically holds at most 12 fields after an automatic addition; a collection the owner made takes additions with no field-count bound. A write that names an undeclared field without `new_fields` keeps refusing and carries the call, or the proposal, in its refusal.
- **Envelope.** The envelope serves collection creation within the bounds, and adding fields within the bounds that `new_fields` defines, under `proactive_capture`. `restructure_execution` keeps the rest of collection creation and every other action it holds today. The text that says automatic collections stay refused is replaced.
- **Recognition contract.** The correction sentence in the capture contract that bootstrap serves, and that the activation `capture_policy` block repeats on every turn, widens: a decision or standing rule agreed in conversation is durable like stored advice that failed and a claim the user corrects, and the agent saves it under `proactive_capture`. The block's per-turn `rules` key gains a short cue: named items that recur, are compared or change go to a matching collection by natural key, else to a minimal one, and narrative stays in Notes. Every always-loaded or pasted surface carries the same route and none keeps the old limit to a measurement in an existing collection. The Records contract below carries the full procedure, including that a passing mention gets no collection.
- **Records contract.** The scaffold skill, the workflow skills, the bootstrap Records teaching, the tool description and the `collection_candidate` guidance replace "propose before the first schema" with the recognise, discover, reuse or create, and report procedure. They also teach how to keep attribution and dated history separate, and how to keep an unresolved identity as its own row instead of merging it.
- **Write reconciliation.** The contract states the recovery recipe for an uncertain write, `MUTATION_ACKNOWLEDGEMENT_PENDING`: replay with the same identity and the same payload, or read the collection and the row by its natural key before submitting that write again, and write only what is missing. It also corrects the bulk contract: `source` is required, once for the batch or on each row.
- **Acceptance.** The incident regression suite gains multi-turn sessions, a follow-up fresh session and checks for a new collection. Its new cases cover both incidents and their edges, on a client with hooks and a hookless one.

Out of scope: the write lock and the store-thread read path, which another change owns; the keyword thresholds of `collection_candidate`, reported as existing debt; automatic renaming, retyping or removal of a field, and every other automatic schema change; and automatic Planning collections.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `delegation-envelope`: collection creation within fixed bounds, and adding fields to a Records collection, follow `proactive_capture`; the rest stays `restructure_execution`.
- `records`: agents recognise structured work and create a minimal collection under the served disposition; the server enforces the bounds of an automatic creation, and `new_fields` adds the fields of a row to its collection under the same disposition.
- `agent-bootstrap-contract`: bootstrap teaches the recognise, discover, reuse or create, and report procedure, and the capture predicate's write gate creates a bounded collection under `silent`.
- `agent-prominence-control`: the capture contract makes decisions, standing rules and corrections agreed in conversation durable.
- `durable-personal-baseline-capture`: a recurring measurement with no compatible collection follows the same Records route.
- `records-release-acceptance`: the no-collection acceptance case expects a proposal under `advisory` and a bounded creation under `silent`.

## Impact

- Code: `src/exomem/envelope.py`, `records.py` and `record_memory.py` (the create gate), `prominence.py` (capture sentences), `commands.py`, `audit.py` and `operation_entries.py` (served text), and the scaffold under `src/exomem/_scaffold/_Schema/`.
- Generated: the skill-contract stamps, the Claude Code plugin skills, the cloud plugin bundles, and the tool-surface fingerprint with its pending ChatGPT digest.
- Tests: the envelope, workflow-skill, collection-claims and bootstrap tests that pin the old refusal; new tests for the create gate.
- Evaluation: `benchmarks/membench/outcome/incidents/`.
