# Planning, Records, and user action routing

## Planning and Records

Keep the following layers distinct. Sources are externally received raw material. Evidence is proof-bearing
material. Notes are compiled conclusions. Entities are stable reusable identities. Planning is intended future
state: goals, outcomes, initiatives, priorities, horizons and commitments. Records are observed state and event
history: what happened, when, measurements, sessions, transactions and state changes. Review compares Planning
intent with Records reality and feeds explicit decisions back into Planning and Notes. Imported is adoption
staging, not the permanent home of live state.

Planning and Records use separate semantic profiles on one structured-collection substrate. Planning owns durable
intent and prioritisation. Records do not infer goals, completion, medical conclusions or personal judgements. A
collection may hold opaque links between a plan and a bounded Records query, but does not resolve or duplicate the
other side. A resolved workflow contract may declare a companion owner for execution artifacts. Without one,
Planning works standalone. Companion references remain opaque, and governance, tool availability and external
permissions remain separate from the declaration.

Before proactive Planning or Records capture, resolve the current workflow posture when the workflow route is
available, using explicit known-absent scope values where applicable. The active prominence level caps its capture
posture. Inspect the relevant Planning collection before you write: update a matching item before you create
another. Records are observations, not a transition engine. Only an explicit user change of intent may make a
guarded Planning transition. An outcome under a propose-after-outcome posture may only propose that change.

## Simple front door

`query_records`, `add_record` and `edit_record` are the public product tools for Records. `query_plan_items`,
`add_plan_item` and `edit_plan_item` are the ones for Planning. Structure changes go through door operations
(below). Evermatter records durable preferences, feedback and decisions per
[write scope](write-scope.md).

**Recall or correction of an observed event or state.** Inspect Records independently of ordinary Note recall.
Raw Records items do not appear in search.

1. Call `query_records()` to list the admitted collections.
2. Inspect one compatible collection with `inspect_memory(operation="records.inspect", arguments={"collection": <manifest_path>})`,
   using the `manifest_path` that the list returned.
3. Run a bounded `query_records(collection=<manifest_path>, filters=..., limit=...)` for the requested identity or
   declared fields.
4. For recall, return the matching rows. For an authorized correction, call
   `edit_record(collection=..., record=<row id>, changes={...}, why=...)`. Copy the row `id` that `query_records`
   returned into `record`.

An empty ordinary search does not establish that no matching Record exists. If inspection or a query refuses,
report that unavailable boundary, not absence. The server checks the row version and the collection state, and a
stale id refuses with the current one. Preserve provenance and verify the updated row. Do not infer that a
matching row exists, and do not create another collection when the inventory, schema or query does not establish
one.

**Planning recall or change.**

1. Call `query_plan_items()` to list the admitted collections.
2. Copy the chosen `manifest_path` into `collection`. If collection titles repeat, use their separate manifest
   paths.
3. Inspect that collection with `inspect_memory(operation="plans.inspect", arguments={"collection": ...})`, and
   run a bounded `query_plan_items(collection=..., filters=..., limit=...)` for the requested items.
4. For recall, return the matching items. For an authorized change, identify exactly one item, then pass that
   row's `id` as `plan_item` to `edit_plan_item`. The `id` is a returned identity and version, not a filename or
   a value to invent.

The `area` and `parent` fields take `exomem://plan/<collection-uuid>/<plan-uuid>` references. Query the chosen
collection for `collection_id` and `plan_id` before you write those fields. Display titles do not identify
relationship targets.

`edit_plan_item` takes `status` for a status move, or `changes` and `body` for a field edit, never both and never
neither. An explicit intent change is a status move with the same guards and an audit `why`. An explicitly
completed outcome is `edit_plan_item(collection=..., plan_item=..., status="completed", why=...)`. A candidate
becomes planned only with considering or committed intent and a non-inbox horizon. Set `status`, `commitment` and
`horizon` together in one `changes` call, for example
`changes={"status": "planned", "commitment": "considering", "horizon": "quarter"}`. Completion requires
committed, non-inbox intent. Committed work-items and initiatives also need their declared parent. Inspect the
collection contract before you choose fields. A Records outcome alone never changes Planning. Never infer a
completion or a horizon change from elapsed time: a stated outcome is evidence, and the clock is not.

A stale-guard refusal on a plan write means the collection or the item changed. Inspect and query again,
reconsider the intended change against the current state, and retry with the new returned guards. Never drop
guards, and never replay an obsolete intent blindly.

**Structure.** A plan or Records collection's UUID is its durable identity, not its filename.

- Read: `plans.inspect`, `plans.validate`, `plans.history` on `inspect_memory`. For Records, `records.describe`,
  `records.validate`, `records.inspect`, `records.history`.
- Write plans: `manage_memory` with `plans.create`, `plans.revise` or `plans.rebaseline`.
- Write Records: `records.create`, `records.revise`, `records.rebaseline`, `records.retract`, `records.discard`,
  `records.bulk_upsert` and `records.import`.
- `plans.rebaseline` and `records.rebaseline` take the exact inspect-reported codes as `acknowledged_gap_codes`.
- Renaming, retyping or removing a field, a merge, a deletion and a migration of a collection are confirmed
  `revise` or `rebaseline` calls.

**Migrating a Markdown table into Records.** A page that holds a table of past entries, such as a ledger,
imports as rows without retyping a cell. `read_instructions(topic="records.import")` gives the full contract.

1. Save the page's text with `save_evidence`. The Evidence is your copy of the page, not the page: nothing in
   the product turns a note page into a source, so the copy can drift from the page. A note page, pasted text and
   a URL are not sources, and `records.import` refuses them.
2. Preview with the request below. The `columns` count shows each mapped column's parsed, missing, unparsed and
   rejected cells and malformed rows, which add up to the rows read. `would_reject` names the row, column and
   original text of each refused cell. `unparsed_cells` lists the dates that were left missing. `check_sum` gives
   the total; a group with no number reports a null sum, never 0. Compare the total and the row count with the
   page: a match shows the summed column and the row count agree, not that every name, date or reference matches.
3. Read the preview to the person before you start. An unparseable number rejects its row and is never stored as
   null. A leading `+` or `-` is a sign; a currency symbol, a thousands separator or parentheses reject the row.
   A date such as `2025-??-??` leaves the date field missing, and a second field mapped to the same column keeps
   the original text.
4. Start the same request without `check_sum`, then poll `mode: "status"` until the job is complete. Its
   `columns` give the same counts as the preview.
5. A page over 256 KiB, or with too many cells, is refused as `IMPORT_SOURCE_TOO_LARGE`. Save the table as csv
   and import that.
6. When the preview's `coverage` is `partial`, `columns`, `unparsed_cells` and `check_sum` are null. Split the page
   into smaller tables, or export it as csv, then preview again.
7. A derived collection refuses `markdown-table`. Use csv or ndjson with `members` for it.

```json
{"mode": "preview", "source_ref": "<the Evidence path save_evidence returned>", "format": "markdown-table",
 "mapping": {"table": 0, "on_invalid": "skip",
   "fields": {"ref": "Ref", "entry_date": {"from": "Date", "type": "date"}, "entry_date_text": "Date",
              "counterparty": "Counterparty", "amount": {"from": "Amount", "type": "number"}},
   "coverage": {"Ref": {"classification": null}, "Date": {"classification": null},
                "Counterparty": {"classification": null}, "Amount": {"classification": null}}},
 "check_sum": {"field": "amount", "group_by": "counterparty"}}
```

The page behind it:

```
| Ref  | Date       | Counterparty | Amount |
|------|------------|--------------|-------:|
| L-01 | 2025-01-15 | Ada Quill    | 120.50 |
| L-02 | 2025-??-?? | Ada Quill    |  80.00 |
```

Speak to users in simple actions first. Do not ask the user to choose `Sources`, `Notes`, `Entities`, `Evidence`,
graph sidecars, schema blocks or supersession internals, unless that detail changes what will happen.

| Simple action | User phrasing | Product route |
|---|---|---|
| `ask` | "what do I know," "find what I concluded," "show the context" | Reuse turn context; `search_memory` for gaps, then `read_page` or `search_memory(deep=true)` as needed ([recall](recall.md)) |
| `create_note` | "remember this," "save this conclusion," "write this decision" | `create_note`; use `supersede_note` when it supersedes old knowledge |
| `capture` | "save this article/source/transcript," "keep this receipt/record/proof" | Pick the lane first, then the transport. Raw material -> `save_source` (`content` for text, `files` for attachments). Proof-bearing -> `save_evidence` (`text` with `name` for text, `files` for attachments), and `upload_files` when the client has no file handles |
| `plan` | "save this feature idea," "file this bug for later," "what matters this week" | `query_plan_items` or `add_plan_item` for intended future state in a configured Planning collection |
| `record` | "a dated measurement," "a completed session," "a transaction," "the current mileage" | `query_records` or `add_record` for observed state in a configured Record collection |
| `review` | "review stale knowledge," "what needs attention," "what sources are unprocessed" | `list_review_items` and `decide_review_item`; explicit dismiss, snooze or reopen through `decide_review_item` |
| `relations` | "review suggested relations," "pay down relation debt," "accept/reject suggested links" | `list_review_items(queue='relation_suggestions')` for the batched read. Accept one reviewed candidate with `decide_review_item(review_item=..., decision="accept", why=...)`. An item that offers options also needs one as `relation=<option key>`; [vocabulary](vocabulary.md) covers a relation the item lacks. Reject with `decision="dismiss"` |
| `connect` | "connect these ideas," "suggest relations," "show the surrounding context" | `inspect_memory(operation="connections.context")` for bounded graph, provenance, evidence and history; `connections.suggest_links`, `connections.suggest_relations` |
| `adopt` | "what does this existing vault contain," "import/adopt this vault safely" | `inspect_memory(operation="imports.scan")` first; then the explicit write operations ([vault care](vault-care.md)) |
| `maintain` | "check vault health," "fix safe drift," "make Planning/Records files readable" | `inspect_memory(operation="maintenance.audit")`, or a preview of one collection with `maintenance.structured_files`. A structured-file apply needs its exact preview plan. `maintenance.fix`, `backfill_ids` and `reconcile` writes run as maintenance jobs that start from a preview's `plan_id` and a `why` ([vault care](vault-care.md)) |
| `schema` | "what structure or relation vocabulary recurs," "validate this graph lens" | `schema.*` operations on `inspect_memory` and `manage_memory`; `schema.infer` before you save, and keep governance optional ([vocabulary](vocabulary.md)) |

Records routing is semantic: use it for durable observed events, current state and recurring structured work
without waiting for a magic verb. When exactly one compatible existing collection accepts a sufficiently
identified observation, the active engagement policy may append or update it, and the agent reports the mutation.
Ask one focused question when collections compete, or when identity, date, provenance, ownership, type or scope is
unclear in a way that changes the action.

**Recurring structured work** is stable identities that recur, repeated observations or evaluations of the same
items, comparisons, state that changes, or a need to query exactly. You judge it from the meaning of the
conversation: no repeat count and no word list decides it. One passing mention of an item is not recurrence and
gets no collection. Narrative and synthesis stay in Notes.

1. Recognise the structured work in the conversation.
2. List the collections and their fields: `query_records()` with no `collection`.
3. Reuse a compatible collection, judged by meaning and schema. A repeated item updates the row its natural key
   names.
4. If none fits and the identity, type and scope are clear, call `add_record` with `new_collection`. It creates a
   minimal collection and adds the first row in one call, under the served `proactive_capture` disposition
   (below).
5. Backfill the items the conversation already established, each with its date and who said what.
6. Report the collection and the rows in your reply.

A minimal collection is a Records collection with a declared natural key, a short list of fields, and no saved
views, references or rollups. You author no schema for it:

- `silent`: call `add_record` with the collection's title in `collection`, the first row in `item` and
  `new_collection={"natural_key": [<the item fields that identify a row>]}`. The server makes one field per key of
  `item`, typed from its value (a string, number, boolean or ISO date), creates the collection and adds the row.
  Report both in the same reply. Give every field a plain value: a null, list or object cannot be typed from one
  row.
- `advisory`: propose the named collection in the user's own words. After one inline confirmation, create it with
  `manage_memory(operation="records.create", arguments={...})` (no `automatic`), then call `add_record`.
- `off`: initiate nothing. A creation the user asks for is served as before.

A title that already names a collection makes `add_record` a plain append, so look at the inventory first: a
mistyped title would start a new collection. With `new_collection`, the server refuses with
`AUTOMATIC_CREATION_REFUSED` and creates nothing when the vault is not in `silent` capture, the schema is outside
the bounds, or the vault already created its daily share of automatic collections. The refusal names the failed
bound and carries the create call with the manifest it would have made. Propose that collection, and make that
call after the user confirms. `records.create` with `automatic=true` is the same bounded create for a manifest you
wrote yourself. Any other creation is a confirmed `restructure_execution`, and so is every schema change other than
adding fields, and every merge, deletion and migration of a collection. A Planning collection never takes
`automatic`. The inventory shows `created_automatically` for each collection, so the owner can review what you
created.

A collection is never complete at its first row. When a later fact has no field (an outside claim that arrives on a
later turn, a date, a measurement), add the field in the same call: `add_record` or `edit_record` with
`options={"new_fields": [<the fields of item or changes that the collection does not declare>]}`. Under `silent`,
the server adds each field, typed from its value, and writes the row, and the reply names the fields it added.
Never put the fact into another field, such as a status: that mixes who said what. Under `advisory`, or when an
automatic collection would pass 12 fields, the call is refused with the proposal and the confirmed `revise` that
adds the fields. Propose them, make that call after the user confirms, then write the row or resume its held
candidate. A write that names an undeclared field without `new_fields` is refused with the same next step.

Keep who said what in separate fields: the user's own feedback, your proposals, and findings from outside sources.
When the user has not resolved whether two names are the same item, keep two rows and record the ambiguity. Never
merge them.

A refused Record write is held rather than lost. The response names the failing field and returns a `held`
reference. Fix the named field and resume with `add_record(..., held=<reference>)` or
`edit_record(..., held=<reference>)`, or discard it with
`manage_memory(operation="records.discard", arguments={"collection": ..., "held": <reference>, "why": ...})`. Do not
loop, and do not preserve diagnostic breadcrumbs as Evidence. A resumed call's `item` or `changes` override the held
values, and a null removes a field. Pass `hold=false` to refuse instead of holding.

A committed Note or Evidence write may return `records_routing` when its authored terms have one clear existing
Records home. Read the observation before you append under the active capture disposition, and resume a returned
held candidate when needed. The advisory never writes a record by itself. A strong `collection_candidate` is
likewise a proposal. Under `silent` capture, create it with `add_record` and `new_collection`. When a bound
refuses, or under `advisory` capture, ask one question in the user's domain language and create only after that
inline confirmation. Backfill only exactly dated evidence units, citing each unit or artifact in `sources`. Prose
notes stay where they are.

A Records collection says what belongs in it through its manifest's `claims` block: `tags` and `terms` name its
vocabulary, and an optional `match` block declares membership outright from a page's frontmatter:

```yaml
claims:
  tags: [example-product, incidents, regressions]
  terms: [sync, login]
  match:
    type: [failure]
    project: [example-product]
```

Every key under `match` must hold on the page (`type`, `category`, `project` or `tags`; `category` also reads the
page's unit categories), and any value listed under a key may match. A page that satisfies every predicate routes
to the collection as strong, even when its words share nothing with the claims. A page silent about a key can still
route by shared words. A page whose `type` or `project` contradicts the declaration (another project, say) never
routes there. Other `tags` or `category` values, or an Evidence page's `type: source`, do not count against it.
When several collections' predicates all hold, the one holding the most predicates wins, then the one sharing more
words. A full tie stays silent. Words come from the title and tags only, compared in a folded form, so
`failure`/`failures` and `regression`/`regressions` meet, and function words such as "not" or "after" never count
as shared vocabulary.

A declaration also decides when only part of it holds. When no collection's whole `match` holds, a collection the
page does not contradict is still preferred by how many of its declared predicates the page holds (a page silent
about `project` can hold `type`), and shared words only rank the collections that tie on that count. A partly held
declaration never makes a route `strong` by itself, and the collection still needs the shared words that any word
route needs.

A collection may also name how to capture into it:

```yaml
capture:
  method: "exomem://memory/<uuid of the page>"
```

`method` is one vault page of any kind: a procedure, a pattern, a skill. Prefer its `exomem://memory/<uuid>` ref,
which survives a rename or a move and needs no link resolver. A quoted wiki link such as
`"[[Incident capture method]]"` also works, but the vault's link resolver resolves a title, so a turn may not
serve it until that resolver is warm. A write resolves it as it runs. Routing never depends on it. When a write
routes to the collection, its `records_routing` carries `method` (a `title` and a `read_ref`) and, where the
advisory asks you to act, tells you to follow it. When a turn routes to the collection, the page is served as a
method to read. A method the caller cannot read, or that names no page, is left out, and
`inspect_memory(operation="records.inspect")` reports one finding for it.

A strong route that a declared `match` produced carries a `disposition`, whatever the note's type, set by the
user's prominence:

- `file`: perform the ready `add_record` call without asking.
- `append_occurrence`: perform the ready update payload, which adds the note to the existing item it recurs.
- `ask`: put the one precomposed question once, and act only on yes.
- `hold`: leave it for review.

Editing a note that is already filed raises none of these again. Creating a collection, or changing its claims,
raises one grouped item naming the existing notes it now covers. Ask the user about it once, at any prominence, and
file nothing from it without a yes.

For a Planning or Records collection stored as Markdown items, YAML frontmatter is the sole canonical value source
and the UUID remains the durable identity. A manifest may declare `item_filename` and `item_presentation` (or the
compatible Records presentation recipe), so the file tree and page read naturally. Prefer stable descriptive fields
such as title for filenames. Keep status, priority, horizon and other mutable state in frontmatter, so ordinary
updates do not cause renames. Treat every managed body block as derived: never edit it as data or read values back
from it. A guarded item update can refresh one block. For an existing UUID-named collection, preview every
rename, body change, collision and inbound-link blocker with
`inspect_memory(operation="maintenance.structured_files", arguments={"collection": ...})`. Apply only with the
returned `plan_id`, `source_snapshot` and reason. Query a declared Records child table with
`query_records(..., options={"expand_child": <child>})`, and use `options={"expand_children": true}` only when the
collection has one unambiguous child container.

Examples:

- "Remember this decision" -> write a concise compiled note and report `Saved -> <title>`.
- "What did I conclude about onboarding?" -> reuse current context, search the remaining gaps with `search_memory`,
  cite hits, and retry with adjacent terms before you treat a miss as meaningful.
- "Save this article" -> `save_source` with provenance. Ask about compiling only if a conclusion is present.
- "Here's the transcript/screenshot from that session" -> an attachment is raw material, so
  `save_source(files=[...])`, classified on both axes. Do not reach for `save_evidence` because the file handle was
  convenient. The lane is what the artifact is for. A Source can be promoted to Evidence later when it turns out to
  be proof, while the reverse is refused.
- "Keep this receipt for the warranty case" -> `save_evidence` (or `upload_files` when the client has no file
  handle), not a general note.
- "I completed a dated training session" -> resolve exactly one compatible collection before `add_record(...)`.
  Keep the session as an observed Record, not a compiled conclusion. If none fits, `add_record` with
  `new_collection` creates a minimal collection under the served capture disposition, as above.
- "Show my last three months" -> `query_records(...)` with a bounded date or filter shape. Use a compiled Note only
  for an explicit conclusion from that history.
- "The second one is done, the rest can wait" -> one landing, two consequences: `add_record(...)` for the produced
  deliverable, then `edit_plan_item(..., status="completed")` to complete that work item. The others stay queued and
  nothing else moves. Report it once: "<deliverable> is done and logged; the rest stay queued." When no collection
  is named, list them with `query_plan_items()`. Inspect that exact collection, then resolve the item with
  `query_plan_items(collection=..., filters=...)` on the title or a natural-key field, adding
  `options={"lifecycle": ...}` as needed, and read its `status`.
- "Save this feature idea" -> `add_plan_item(...)`. Use an explicit `edit_plan_item` for a horizon or hierarchy
  change.
- "Compile these three sources" -> ensure the three originals are already captured as Source or Evidence pages
  (capture any missing originals first), then draft a sourced note with
  `create_note(suggest_links=true, detail="full")` link suggestions, then write after the applicable approval rule.
- "Show stale conclusions" -> run the review path and present candidates for keep, edit, supersede or archive.
- "This new strategy replaces the old one" -> use supersession, so history stays visible.

## Parameter notes

- `add_plan_item` only adds an item. `edit_plan_item` changes one. `plans.create`, `plans.revise` and
  `plans.rebaseline` change structure.
- `query_records`, `add_record` and `edit_record`: `record` is the row id that `query_records` returned. It is
  required for `edit_record`. On `add_record`, identity derives from the collection's declared natural key. The
  server binds the collection state itself.
- `options={"delivery": ...}` on `add_record` is a receipt-gated artifact-delivery envelope for append only. Its
  field mappings are vault-schema-neutral, and they never set item values or create or loosen a collection.
- `held` resumes a held candidate on append or update, or names the one to remove through `records.discard`.
- `records.retract` takes one store-backed summary or log row out of current state. Name the row by the `item_id`
  that `query_records` returned, and give a `why`. The row keeps its history. Appending the same natural key later
  brings the same row back. A per-row-view or file-canonical collection refuses it.
