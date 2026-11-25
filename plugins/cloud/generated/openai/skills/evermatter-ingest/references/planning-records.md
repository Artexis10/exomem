# Planning, Records, and user action routing

## Planning and Records

Keep the following layers distinct. Sources are externally received raw material;
Evidence is proof-bearing material; Notes are compiled conclusions; and Entities
are stable reusable identities. Planning is intended future state: goals,
outcomes, initiatives, priorities, horizons, and commitments. Records are
observed state and event history: what happened, when, measurements, sessions,
transactions, and state changes. Review compares Planning intent with Records
reality and feeds explicit decisions back into Planning and Notes. Imported is
adoption staging, not the permanent home of live state.

Planning and Records use separate semantic profiles on one structured-collection
substrate. Planning owns durable intent and prioritisation. Records do not infer
goals, completion, medical conclusions, or personal judgments. A collection may
hold opaque links between a plan and a bounded Records query, but does not resolve
or duplicate the other side. A resolved workflow contract may declare a companion
owner for execution artifacts; without one, Planning works standalone. Companion
references remain opaque, and governance, tool availability, and external
permissions remain separate from the declaration.

Before proactive Planning or Records capture, resolve the current workflow
posture when the workflow route is available, using explicit known-absent scope
values where applicable. Its capture posture is capped by the active prominence
level. Inspect the relevant Planning collection before writing: update a matching
item before creating another. Records are observations, not a transition engine:
only an explicit user change of intent may make a guarded Planning transition;
an outcome under a propose-after-outcome posture may only propose that change.

## Simple front door

These are the MCP tool names. REST and the CLI keep one grouped Records command
and one grouped Planning command, listed in their own help, with `action` set to
the operation's `kind` and its fields at the top level.

For recall or correction of an observed event or state, inspect Records
independently of ordinary Note recall. Raw Records items do not appear there.
Call `query_records()` to list admitted collections, then inspect
one compatible collection using its returned `manifest_path` as `collection`.
Run a bounded `query` for the requested identity or declared fields.
An empty ordinary search does not establish that no matching Record exists.
If inspection or query refuses, report that unavailable boundary rather than absence.
For recall, return the matching rows. An authorized correction uses the guarded update below.
For `edit_record`, copy the row `id` that `query_records` returned into `record`.
The server checks the row version and the collection state, and a stale id refuses with
the current one. Preserve provenance and verify the updated row.
Do not infer that a matching row exists or create another collection when
the inventory, schema, or query does not establish one.

For Planning recall or a change, call `query_plan_items()` to list admitted
collections. Copy the chosen `manifest_path` into `collection`, then inspect
that collection and run a bounded `query` for the requested items.
For a change, identify exactly one item before choosing a write.
If collection titles repeat, use their separate manifest paths. For recall,
return the matching items. For an authorized change, pass that query row's `id` as `plan_item` to
`edit_plan_item`. It is a returned identity and version, not a filename or a value to invent.

The `area` and `parent` fields require `exomem://plan/<collection-uuid>/<plan-uuid>`
references. Query the chosen collection for `collection_id` and `plan_id` before
writing those fields. Display titles do not identify relationship targets.

Pass `changes={"title": "..."}` to `update`; for an explicit intent change
pass `transition` to `triage`, with the same guards and an audit `why`.
For example, an inbox candidate can become planned with
`transition={"status": "planned", "commitment": "considering", "horizon": "quarter"}`.
For an explicitly completed outcome, use `transition={"status": "completed"}`.
Completion requires committed, non-inbox intent; committed work-items and
initiatives also need their declared parent. Inspect the collection contract
before choosing fields. A Records outcome alone never changes Planning.

`STALE_PLAN_CONTAINER` or `STALE_PLAN_ITEM` refuses the write; inspect/query
again, reconsider the intended change against the current state, and retry with
the new returned guards. Never drop guards or blindly replay an obsolete intent.

Speak to users in simple actions first. Call product commands by default; the
canonical operations are implementation leaves underneath them. Do not ask the
user to choose `Sources`, `Notes`, `Entities`, `Evidence`, graph sidecars, schema
blocks, or supersession internals unless that detail changes what will happen.

Keep temporary working context in the current conversation. Evermatter is the durable
home for preferences, feedback, personal baselines, project context, decisions,
sourced conclusions, failures, experiments, and proof-bearing records.
Capture follows the live engagement policy. Native memory is an explicit fallback
when Evermatter cannot capture; move durable results to Evermatter when it can.

| Simple action | User phrasing | Product route |
|---|---|---|
| `ask` | "what do I know," "find what I concluded," "show the context" | Reuse turn context; `search_memory(detail="compact", rerank=false)` for gaps, then `read_page` or `search_memory(deep=true)` as needed |
| `create_note` | "remember this," "save this conclusion," "write this decision" | `create_note`; use `supersede_note` when it supersedes old knowledge |
| `capture` | "save this article/source/transcript," "keep this receipt/record/proof" | Pick the lane first, then the transport. Raw material -> `save_source` (`content` for text, `files` for attachments). Proof-bearing -> `save_evidence` for text, `save_evidence` for attachments, `upload_files` when the client has no file handles |
| `plan` | "save this feature idea," "file this bug for later," "what matters this week" | `query_plan_items` or `add_plan_item` for intended future state in a configured Planning collection |
| `record` | "a dated measurement," "a completed session," "a transaction," "the current mileage" | `query_records` or `add_record` for observed state in a configured Record collection |
| `review` | "review stale knowledge," "what needs attention," "what sources are unprocessed" | `list_review_items` and `decide_review_item`; explicit dismiss/snooze/reopen via `decide_review_item` |
| `relations` | "review suggested relations," "pay down relation debt," "accept/reject suggested links" | `list_review_items(queue='relation_suggestions')` for the batched read; accept one reviewed candidate via `decide_review_item(decision='accept')` (requires an audit reason; an item with `options` also needs one of them as `relation`, see [governance](governance.md)); reject via `decide_review_item` |
| `connect` | "connect these ideas," "suggest relations," "show the surrounding context" | `inspect_memory` and `manage_memory`; use `operation="context"` for bounded graph, provenance, evidence, and history |
| `adopt` | "what does this existing vault contain," "import/adopt this vault safely" | `inspect_memory(operation="imports.scan")` first; explicit modes for manifest/copy/compile planning |
| `maintain` | "check vault health," "fix safe drift," "make Planning/Records files readable" | Remote tools may use `inspect_memory(operation="maintenance.audit")` or preview one collection with `mode="structured-files"`. Structured-file apply requires its exact preview plan; ordinary `fix`/`backfill-ids`/`reconcile` writes run as maintenance jobs that start from a preview's `plan_id` and a `why` |
| `schema` | "what structure or relation vocabulary recurs," "validate this graph lens" | `inspect_memory` and `manage_memory`; infer before saving, and keep governance optional |

Records routing is semantic: use it for durable observed events, current state and
recurring structured work without waiting for a magic verb. When exactly one compatible
existing collection accepts a sufficiently identified observation, the active engagement
policy may append or update it and the agent reports the mutation. Ask one focused question
when collections compete or identity, date, provenance, or ownership is unclear.

**Recurring structured work** is stable identities that recur, repeated observations or
evaluations of the same items, comparisons, state that changes, or a need to query exactly.
You judge it from the meaning of the conversation: no repeat count and no word list decides
it. One passing mention of an item is not recurrence and gets no collection. Narrative and
synthesis stay in Notes.

1. Recognise the structured work in the conversation.
2. List the collections and their fields: `query_records` with no `collection`.
3. Reuse a compatible collection, judged by meaning and schema. A repeated item updates the
   row its natural key names.
4. If none fits and the identity, type and scope are clear, `add_record` with
   `new_collection` creates a minimal collection and adds the first row in one call, under
   the served `proactive_capture` disposition (below).
5. Backfill the items the conversation already established, each with its date and who
   said what.
6. Report the collection and the rows in your reply.

A write that creates rows lists `related_pages`: up to three pages for each new row (30 in
a reply), each with its title and ref, whose title or alias matches the row's key or title
value. Read them before you leave an unknown or a placeholder in a row, and replace it with
what they say or link the page. The server does not decide what counts as unknown; you do.
When the reply carries a `RELATED_PAGES_UNAVAILABLE` warning, no lookup ran: search the
subject yourself. A vault that keeps its records as files only, with no collection store,
never runs the lookup, so every write there that creates rows carries the warning.

Ask one question only when two collections compete, an identity is unresolved, or the type
or scope is unclear in a way that changes the action.

A minimal collection is a Records collection with a declared natural key, a short list of
fields, and no saved views, references or rollups. You author no schema for it:

- `silent`: call `add_record` with the collection's title in `collection`, the first row in
  `item` and `new_collection: {"natural_key": [<the item fields that identify a row>]}`. The
  server makes one field per key of `item`, typed from its value (a string, number, boolean or
  ISO date), creates the collection and adds the row. Report both in the same reply. Give
  every field a plain value; a null, list or object cannot be typed from one row.
- `advisory`: propose the named collection in the user's own words. After one inline
  confirmation, create it with `records.create` (no `automatic`), then call `add_record`.
- `off`: initiate nothing. A creation the user asks for is served as before.

A title that already names a collection makes `add_record` a plain append, so look at the
inventory first: a mistyped title would start a new collection. With `new_collection` the
server refuses `AUTOMATIC_CREATION_REFUSED`, creates nothing and names the failed bound when
the vault is not in `silent` capture, the schema is outside the bounds, or the vault already
created its daily share of automatic collections. The refusal carries the create call with
the manifest it would have made: propose that collection, and make that call after the user
confirms. `records.create` with `automatic: true` is the same bounded create for a manifest
you wrote yourself. Any other creation is confirmed `restructure_execution`, and so is every
schema change other than adding fields, merge, deletion and migration of a collection. A
Planning collection never takes `automatic`. The inventory shows `created_automatically` for each collection, so the
owner can review what you created.

A collection is never complete at its first row. When a later fact has no field (an outside
claim that arrives on a later turn, a date, a measurement), add the field in the same call:
`add_record` or `edit_record` with `options: {"new_fields": [<the fields of item or changes that
the collection does not declare>]}`. Under `silent` the server adds each field, typed from its
value, and writes the row, and the reply names them in `added_fields`. Never put the fact into another field, such as a status: that
mixes who said what. Under `advisory`, or when an automatic collection would pass 12 fields, the call
is refused with the proposal and the confirmed `revise` that adds the fields: propose them,
make that call after the user confirms, then write the row or resume its held candidate.
A write that names an undeclared field without `new_fields` is refused
`SCHEMA_UNKNOWN_FIELD` with the same next step. Renaming, retyping or removing a field stays a
confirmed `revise`.

Keep who said what in separate fields: the user's own feedback, your proposals, and findings
from outside sources. When the user has not resolved whether two names are the same item,
keep two rows and record the ambiguity; never merge them.

A refused Record write is held rather than lost: the response names the failing
field and returns a `held` reference, so fix the named field and resume by that
reference (or `discard` it) instead of looping or preserving diagnostic
breadcrumbs as Evidence.

A committed Note or Evidence write may return `records_routing` when its authored
terms have one clear existing Records home. Read the observation before appending
under the active capture disposition, and resume a returned held candidate when
needed; the advisory never writes a record by itself. A strong
`collection_candidate` is likewise a proposal. Under `silent` capture create it with
`add_record` and `new_collection`; when a bound refuses, or under `advisory` capture, ask one
question in the user's domain language and create only after that inline confirmation. Backfill only exactly
dated evidence units, citing each unit or artifact in `sources`; prose notes stay
where they are.

A Records collection says what belongs in it through its manifest's `claims`
block: `tags` and `terms` name its vocabulary, and an optional `match` block
declares membership outright from a page's frontmatter:

```yaml
claims:
  tags: [example-product, incidents, regressions]
  terms: [sync, login]
  match:
    type: [failure]
    project: [example-product]
```

Every key under `match` must hold on the page (`type`, `category`, `project`
or `tags`; `category` also reads the page's unit categories), and any value
listed under a key may match. A page that satisfies every predicate routes to
the collection as strong, even when its words share nothing with the claims.
A page silent about a key can still route by shared words, but a page whose
`type` or `project` contradicts the declaration (another project, say) never
routes there; other `tags` or `category` values, or an Evidence page's
`type: source`, do not count against it. When several
collections' predicates all hold, the one holding the most predicates wins,
then the one sharing more words; a full tie stays silent. Words come from the
title and tags only, compared in a folded form, so `failure`/`failures` and
`regression`/`regressions` meet, and function words such as "not" or "after"
never count as shared vocabulary.

A declaration also decides when only part of it holds. When no collection's
whole `match` holds, a collection the page does not contradict is still
preferred by how many of its declared predicates the page holds (a page silent
about `project` can hold `type`), and shared words only rank the collections
that tie on that count. A partly held declaration never makes a route `strong`
by itself, and the collection still needs the shared words that any word route
needs.

A collection may also name how to capture into it:

```yaml
capture:
  method: "exomem://memory/<uuid of the page>"
```

`method` is one vault page (any kind: a procedure, a pattern, a skill). Prefer
its `exomem://memory/<uuid>` ref: it survives a rename or a move and needs no
link resolver. A quoted wiki link such as `"[[Incident capture method]]"` also
works, but a title is resolved through the vault's link resolver, so a turn may
not serve it until that resolver is warm; a write resolves it as it runs.
Routing never depends on it. When a write routes to the collection, its `records_routing` carries
`method` (a `title` and a `read_ref`) and, where the advisory asks you to act,
tells you to follow it; when a turn routes to the collection, the page is
served as a method to read. A method the caller cannot read, or that names no
page, is left out, and `inspect_memory(operation="records.inspect")` reports one
`CAPTURE_METHOD_UNAVAILABLE` finding for it.

A strong route that a declared `match` produced carries a `disposition`,
whatever the note's type, set by the user's prominence: `file` (perform the ready `add_record` call without
asking), `append_occurrence` (perform the ready update payload, which adds the
note to the existing item it recurs), `ask` (put the one precomposed question
once and act only on yes), or `hold` (leave it for review). Editing a note that
is already filed raises none of these again. Creating a collection, or changing
its claims, raises one grouped item naming the existing notes it now covers:
ask the user about it once, at any prominence, and file nothing from it without
a yes.

For a Planning or Records collection stored as Markdown items, YAML frontmatter
is the sole canonical value source and the UUID remains durable identity. A
manifest may declare `item_filename` and `item_presentation` (or the compatible
Records presentation recipe) so the file tree and page read naturally. Prefer
stable descriptive fields such as title for filenames; keep status, priority,
horizon, and other mutable state in frontmatter so ordinary updates do not cause
renames. Treat every managed body block as derived: never edit it as data or read
values back from it. A guarded item update can refresh one block. For an existing
UUID-named collection, call
`inspect_memory(operation="maintenance.structured_files", arguments={'collection': ...})` to preview every
rename, body change, collision, and inbound-link blocker; apply only with the
returned `plan_id`, `source_snapshot`, and reason. Query a declared Records child
table with `expand_child`; use `expand_children=true` only when the collection
has one unambiguous child container.

Examples:

- "Remember this decision" -> write a concise compiled note and report
  `Saved -> <title>`.
- "What did I conclude about onboarding?" -> reuse current context, search remaining gaps with `search_memory`, cite hits, and
  retry with adjacent terms before treating a miss as meaningful.
- "Save this article" -> `save_source` with provenance; ask about compiling
  only if a conclusion is present.
- "Here's the transcript/screenshot from that session" -> an attachment is raw
  material, so `save_source(files=[...])`, classified on both axes. Do not
  reach for an Evidence command because the file handle was convenient: the
  lane is what the artifact is *for*, and a Source can be promoted to Evidence
  later when it turns out to be proof, while the reverse is refused.
- "Keep this receipt for the warranty case" -> `save_evidence`, `save_evidence`, or `upload_files`, not as a
  general note.
- "I completed a dated training session" -> resolve exactly one compatible
  collection before `add_record(...)`; keep the session as an
  observed Record, not a compiled conclusion. If none fits, `add_record` with
  `new_collection` creates a minimal collection under the served capture disposition, as above.
- "Show my last three months" -> `query_records(...)` with a bounded date/query shape; use a compiled Note only for an explicit conclusion from that history.
- "The second one is done, the rest can wait" -> one landing, two consequences: `add_record(...)` for the produced deliverable, then `edit_plan_item(...)` to complete that work item; the others stay queued and nothing else moves. Report it once: "<deliverable> is done and logged; the rest stay queued." Discover the collection with `inspect_memory` when none is named, inspect that exact collection with `query_plan_items(collection=...)`, then resolve the item with `query_plan_items(...)` filtered on the title or a natural-key field plus `lifecycle` and `status`.
- "Save this feature idea" -> `add_plan_item(...)`; use explicit `triage` for a horizon or hierarchy change, never infer it from elapsed time — a stated outcome is evidence, the clock is not.
- "Compile these three sources" -> ensure the three originals are already
  captured as Source/Evidence pages (capture any missing originals first), then
  draft a sourced note with
  `create_note(suggest_links=true, detail="full")` link suggestions, then
  write after the applicable approval rule.
- "Show stale conclusions" -> run the review path and present candidates for
  keep/edit/supersede/archive.
- "This new strategy replaces the old one" -> use supersession so history stays
  visible.

## Tool parameter notes

- `query_plan_items` and `add_plan_item`: `inspect`, `validate` and `query` are read-only (`query_plan_items`); `create`, `add`, `update`, `triage`, `revise`, `rebaseline` are guarded writes (`add_plan_item`). A plan's UUID is durable identity, not its filename. New collections can declare human filenames and managed presentation blocks; an existing UUID collection moves only through a read-only `inspect_memory(operation="maintenance.structured_files")` preview followed by exact-plan apply. Never infer completion or horizon changes from elapsed time.
- `query_records` and `add_record`: `record` is the row id that `query_records` returned (required for `edit_record`; on `add_record` identity derives from the collection's declared natural key). The server binds the collection state itself; `acknowledged_gap_codes` are the exact inspect-reported codes for rebaseline. `delivery` is a receipt-gated artifact-delivery envelope for append only: field mappings are vault-schema-neutral and never set item values or create or loosen a collection. `expand_children` expands the one unambiguous child container; `expand_child` names an exact declared child. `held` resumes a held candidate on append/update (`item`/`changes` supply overrides, null removes a field) or names the one to remove on discard; a refused append/update is held unless `hold=false`. `retract` (the Records `change_records` operation, not `edit_record`) takes one store-backed summary or log row, named by the `item_id` that `query_records` returned, out of current state with a required `why`; the row keeps its history. Appending the same natural key later brings the same row back; a per-row-view or file-canonical collection refuses `RETRACT_UNAVAILABLE`.
