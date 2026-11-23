# Design

## Context

The 2026-11-22 incidents trace to three causes. Each needs its own fix.

1. **Nothing recognises structured work in the conversation.** `collection_candidate` reads tagged compiled notes only. It qualifies a term only across at least 3 pages, 3 dates and a 14-day span, with at least 3 units that contain a date, an amount or a state word. Reactions such as "liked" or "too long" contain none of these. `records_routing` needs a collection that already exists and fires only after a note write. The client capture reminder fires after a commit, a push or a pull request. The server has no window on the conversation, so on a hookless client only the agent can recognise the work.
2. **The envelope refuses the outcome the product wants.** `restructure_execution` holds every collection creation. It is confirm-required at every prominence, and a standing delegation is refused by name.
3. **The contract teaches the refusal.** "Records are intentionally manual-first", "must not silently create a long-lived schema", "Propose a new collection before creating it", and "Automatic collections stay refused" are served text. The server itself never checks the envelope on create.

Sources read first, from the primary-source roster's pick-by-problem table:

- "Autonomy creates unacceptable blast radius": How we contain Claude. Its advice is to raise autonomy inside hard bounds rather than ask for approval that users learn to click through.
- "Workflow vs agent": Building effective agents. Meaning stays with the model; fixed rules go only where the work is fixed.
- "I can't tell whether a change actually improved the agent": Demystifying evals for AI agents. Trials run end to end on real models, the production failure becomes a regression case, and outcomes are graded.

## Goals and non-goals

Goals:

- Without being told to save, an agent puts recurring structured work in Records, reusing a compatible collection or creating a minimal one.
- The agent saves an agreed decision, standing rule or correction when it is agreed.
- The owner's capture setting governs all of this, and the server enforces the bounds of an automatic creation.

Non-goals:

- **The write lock and the store-thread read path.** Another change owns them; a read queued behind a write is reported there.
- **Rewriting `collection_candidate`'s keyword thresholds.** They are existing debt and this change reports them as debt.
- **Automatic changes to a declared field.** Renaming, retyping or removing a field, re-keying, merging, deleting and migrating a collection stay `restructure_execution`. Decision 6 makes only the addition of fields automatic.
- **Automatic Planning collections.**

## Decisions

### 1. Minimal creation follows `proactive_capture`

Creating one collection within the bounds of decision 2 becomes a `proactive_capture` action. It takes the class's served disposition:

| Disposition | What the agent does |
|---|---|
| `silent` | Creates the collection and reports it in the same reply. |
| `advisory` | Proposes the collection and creates it after one inline confirmation. |
| `off` | Initiates nothing. A creation the user asks for is still served. |

Everything else that today is collection creation stays `restructure_execution`: a collection outside the bounds, a Planning collection, a schema change other than adding fields (decision 6), a merge, a deletion and a migration.

Why `proactive_capture` and not a new class: the owner's existing capture setting already says how far the agent may write unasked, and appending to a collection already lives there. A new class would add a second setting that users must keep consistent with the first. No user has asked to allow captures and forbid new collections. A dedicated class remains the extension if one does.

### 2. The server enforces the bounds of an automatic creation

`change_records` with `operation.kind: create` takes `automatic: true` for an agent-initiated creation. Without the flag the create behaves as today, which keeps every requested creation, and the tests and tools that make one, unchanged. With the flag, the server refuses unless all of these hold:

| Bound | Why |
|---|---|
| The effective `proactive_capture`, from `envelope.active()`, is `silent` | The owner's setting is enforced where it matters, not only taught. |
| Records profile, with a declared natural key | Repeated mentions must update the same row. |
| At most 12 fields, counting the properties of an object field and of an array's items | A minimal schema; a speculative field costs every later row. |
| Only the manifest keys that the minimal manifest has, and no references | An allowlist, not a list of features to refuse: views, rollups, derived rows, claims, capture, templates, governance and presentation are authoring decisions, not capture. |
| Fewer than 3 automatic creations in the vault in the last 24 hours | Stops an agent that loops from scattering collections. |

The numbers live once, in the records module, beside the code that checks them.

The flag rests on the agent's word, as every agent-initiated write does today. Bounds are not a security boundary: today an agent can already create any collection without asking. They stop an eager but well-meaning agent from building sprawl.

A refusal costs little. It is `AUTOMATIC_CREATION_REFUSED` with the bound in its details, and its way forward depends on the disposition. Under `advisory`, or under `silent` with another bound failed, it says to propose the collection and create it without the flag after the user confirms. Under `off` it says to create it only if the user asked for it, so a refusal never tells the agent to initiate. The confirmed create stays in the refusal's retry call in every case. That is today's path.

The create's receipt records `automatic: true`. The inventory reports it per collection, so the owner can list what the agent created. Retiring or deleting an automatic collection keeps its current authority.

The bounds hold on a file-only vault and on a store vault alike. A fresh install enrols no collection store until a cutover, so a bound that needed a store would disable automatic creation for most users. The 24-hour count reads each storage mode's own create record:

- A store vault joins `collections.created_txn` to `txns.committed_at` and reads the receipt flag. The collections table holds tens of rows, so the check adds one indexed join to a rare operation.
- A file-only vault reads a marker line that an automatic create appends to its own activity-log entry. The line names the collection and the instant, because the log heading carries a date only. The count reads the live log, and a rotated archive only when it was last written inside the window. An archive is written after every entry it holds, so an older archive cannot hold a create from the window. The strict audit event is unchanged, so an older release keeps reading the log.
- A vault whose store came after its file collections counts both.

A marker line that cannot be read refuses the create, because skipping it would lift the bound. On a file vault the inventory reads the same lines, from the live log and the four newest archives so that its cost stays flat as history grows. It reports unknown for a collection it cannot place, never "not automatic".

### 3. Recognition is the agent's judgement, carried on every turn

Recognising structured work means deciding what a conversation means. That is model judgement: a word list or a threshold would decide it by spelling, which the repository forbids (C6). The system's part is to put the rule in front of the agent at the right moment, and to make the right action one obvious call.

The capture contract widens one sentence and gains one cue. Both live in `prominence.py` as the single source.

- **Decisions.** The existing correction sentence widens, at about its old length, to cover a decision or standing rule agreed in conversation as well as stored advice that failed and a claim the user corrects: each is durable, saved under `proactive_capture`. It stays one sentence because the compact bootstrap core and the per-turn `say` text are both at their byte ceilings. Bootstrap's `balanced` and `maximal` contracts serve it, and so does the activation `capture_policy` block. That block rides `recall_context` on every turn, so hookless clients get the rule on each turn, not once per session.
- **Structured work.** A cue of about 150 bytes rides the block's per-turn `rules` key, which has room under the hook's budget: named items that recur, are compared or change go to a matching collection by natural key, else to a minimal one under `proactive_capture`, with narrative kept in Notes. The core keeps only its clause "Missing collection: create a minimal one." The full procedure below lives in the scaffold and the bootstrap `records_planning` section.
- **One call to create and file.** Creating a collection by hand takes `records.create`, a read of the manifest contract and an authored schema, while a note takes one call. A real-model round showed agents choosing the note. So `add_record` takes `new_collection: {natural_key: [...]}`: when `collection` is a title that names no collection, the server drafts the minimal Records manifest from the row (one field per key of `item`, typed from the value's JSON shape, where every JSON number is `number` because one value cannot tell a whole-number field from a decimal one, the natural key required), creates it through the same automatic create, and appends the row, as one call with one receipt. The create keeps every bound of decision 2, so the drafting adds no second set of bounds and no second create path. Under `advisory` or `off` capture, or past a bound, the refusal carries the confirmed create call with that manifest, so the agent proposes exactly that collection. `add_record`'s description is the carrier, because it is always loaded where the agent picks a tool, on hooked and hookless clients alike. A title that names a collection is a plain append. A title with a control character, a line break included, is refused: the title is kept verbatim in the manifest.
- **One route everywhere.** Every always-loaded or pasted surface states the same Records route: recurring named items and measurements go to Records, a matching collection by natural key, else a minimal new one under `proactive_capture`. That covers the baseline sentence beside the correction sentence, the custom-instruction blocks, and the scaffold's baseline paragraphs. None of them keeps the old limit to a measurement in an existing collection. Each stays within its byte cap.

The Records contract in the scaffold and the bootstrap teaching spells out the procedure:

1. **Recognise.** Look for the signals above.
2. **Discover.** Read the inventory of collections and fields.
3. **Choose.** Reuse a compatible collection. Ask one question only when competing collections, an unresolved identity, or an unclear type or scope would change the action.
4. **Create.** If nothing fits, `add_record` with `new_collection` creates the minimal collection and adds the first row in one call.
5. **Backfill.** Add the items already discussed in the conversation, with their dates and who said what.
6. **Report.** Name the collection and the rows in the reply.

Attribution stays in separate fields: the user's own feedback, the assistant's proposals and outside findings. An identity the user has not resolved, such as two near-identical names, stays its own row with its ambiguity recorded, never merged.

### 4. No new client-side trigger until a measurement asks for one

The client capture reminder blocks the stop after a landing. A replay of one installation counted 719 forced turns, and 12% led to a save. A second stop-blocking check after every substantive turn would multiply that cost. A separate change also has a trial of next-turn delivery running.

So this change ships the per-turn contract (decision 3) first and measures it. If the hooked arm still misses agreed decisions on the acceptance cases, a follow-up slice widens the reminder's trigger. It would remind on a substantive turn with no knowledge-base write, delivered on the next prompt, so it forces no extra turn. That slice stays only if an ablation shows it pays, and it agrees its setting with the delivery trial first.

### 5. Uncertain writes reconcile by identity

`MUTATION_ACKNOWLEDGEMENT_PENDING` means the store accepted the work and it may still commit. The contract will say how to recover:

1. Replay with the same identity and the same payload; the product returns the recorded result and never writes twice.
2. Or read the collection and the row by its natural key before submitting that write again, and write only what is missing.
3. Never retry with a new identity.

The bulk contract will stop calling `source` optional: a bulk row needs a source, either once for the batch or on each row. A read refused with `COLLECTION_STORE_BUSY` committed nothing and may be retried. The store-thread queueing behind it belongs to the lock work.

### 6. Adding a field follows `proactive_capture`

A collection created from its first row has the fields of that row. Information that arrives later, such as an outside claim on turn 2, has no field, and adding one was a confirmed `restructure_execution` revise. Under silent capture the agent then put the claim into an existing field such as status, which mixes who said what. The user asked that collections can be reshaped on demand and remedied when needed, because the shape cannot be known when a collection starts.

An additive change takes the creation's posture: `silent` adds and reports in the same reply, `advisory` refuses with the proposal, and `off` initiates nothing, so under `off` the refusal says to revise only if the user asked for it. Additive means one or more new fields, typed from the value's JSON shape as `new_collection` types them (every JSON number is `number`), with existing fields and rows untouched and existing rows holding no value for the new field. Renaming, retyping or removing a field, re-keying, merging, deleting and migrating stay `restructure_execution`, available on the user's request with one confirmation.

**One obvious call.** `add_record` and `edit_record` take `new_fields`, the names in this call's `item` or `changes` that the collection does not declare yet. They ride each tool's existing `options` bag, so `add_record`'s always-loaded description gains no bytes and `edit_record`'s schema gains one small bag. Without `new_fields`, an undeclared field still refuses `SCHEMA_UNKNOWN_FIELD`. When every issue of that refusal is an undeclared field, it carries the retry: under `silent`, the same call with `new_fields` that also resumes the held candidate; otherwise the proposal and the confirmed `revise`. The agent learns the route when it needs it.

**Bounds.** Under `silent`, a collection that was itself created automatically holds at most 12 item fields after the addition, the creation's field bound. The bound stops an agent's own collections from sprawling. A collection the owner made, or one that a confirmed create made, takes automatic additions with no field-count bound: the owner wants agents to extend structures without asking, because a list's shape cannot be known when it starts, and a wide collection of the owner's already reflects the owner's choice. Whether a collection was created automatically comes from its existing create record, never from a new marker: a store collection's create transaction, whose receipt carries the automatic flag, and a file collection's create line in the activity log. The server reads it only when an addition would pass 12 fields. A log that cannot be read in full, with the collection's create line not found, answers "not automatic": the cost of that wrong call is one collection that grows past 12 fields, which is less than a refused write. A name the collection already declares adds nothing, which makes a replay the same call, and the row's own validation judges its value against the declared type, because `new_fields` never retypes. A new field whose value is null, a list or an object cannot be typed from one row and is refused. A Planning collection takes none of this.

**Reuse.** The addition is the ordinary `revise`: the same manifest edit at the field mapping's node, its guards, its audit event, the store's writer and thread for a store collection, and the derived request identity that `mutation_step` gives a call's earlier transaction. The server reads the manifest text, adds the field declarations at the end of the fields mapping and leaves every other byte, then validates and revises. The insertion point is the start of the next node after the fields mapping, or the document end, never the line after the last field: a last field that ends in a block sequence or a block scalar ends on the next node's own mark. The edited text is parsed and compared with the original plus the added fields, so a layout the edit does not foresee is refused and never written. A manifest whose fields are written in flow style, or one whose edit would move other content, has no place to add in place and is refused with the confirmed `revise` as the way forward; when the refusal's own revise cannot be drafted, the refusal keeps the proposal and names why in `retry_unavailable`. The revision re-validates every row once, as every revise does, and an addition is a rare event, not a per-row cost.

## Acceptance

The incident regression suite (`benchmarks/membench/outcome/incidents/`) already plays real models on fresh, disposable vaults:

- a Claude Code actor with the plugin's skills and hooks;
- a hookless OpenAI actor that reads the skill text and the server instructions.

It seeds collections through the product's own commands and checks the outcome against the vault, which grades the outcome rather than the transcript. It needs three additions:

- **Multi-turn sessions.** A case lists several user turns, and they play in one session.
- **A follow-up fresh session.** A later session asks for the current state, and a blind judge checks the answer against the case key.
- **Collection checks.** A `new_collection` check passes when the case's expected kind of collection was created with a natural key and at most the bounded fields, holds the expected rows with no duplicate key, and was created automatically. A `no_new_collection` check guards passing mentions and `off`.

The cases use invented domains and names:

| Case | What it checks |
|---|---|
| Recurring evaluation | Several rounds of reactions to named candidates. |
| Unrelated second domain | Recurring equipment maintenance. |
| Reuse | A compatible collection already exists. |
| Correction | A stored row's value is corrected. |
| Ambiguous identity | Two near-identical names. |
| Passing mention | A recurring-looking item mentioned once. |
| `off` disposition | Capture is off. |
| Interrupted write | The fault injector withholds the acknowledgement. |
| Decision agreed in conversation | A standing rule agreed without a commit. |

The run follows the eval-loop method:

1. Run base and changed at one draw on every case.
2. Read each failure's trace and fix its cause.
3. Hand over the passing cases at three draws, reported as right on every draw.

The record goes in the eval-runs collection before any number is quoted. The Claude actor runs on a subscription login. The OpenAI actor bills to the eval budget, and its cost is stated before the paid run.

## Risks

- **Sprawl despite the bounds.** Three a day is still many over weeks. The inventory's automatic flag lets the owner review them, and the bound can tighten without a contract change.
- **The agent misjudges structure.** The passing-mention and second-domain cases measure both directions of error before release.
- **Contract bytes.** Compact bootstrap has a pinned byte ceiling, and the custom-instruction blocks are capped at 1,500 bytes. Each new sentence is measured against them and trimmed where it must be.
