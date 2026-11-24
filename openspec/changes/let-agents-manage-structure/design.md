# Design

## Context

`capture-structured-work-automatically` (the parent change) lets an agent create one minimal Records collection under `proactive_capture`, within server bounds. Its follow-on slice adds `new_fields` to `add_record` and `edit_record`: additive fields under the same disposition and the same 12-field cap. Everything else that changes a collection's shape stays `restructure_execution`, and part of it has no operation at all.

What exists today:

- **Revision.** `change_records` and `change_plans` `revise` take a complete manifest and validate every current item against it (`revise_collection` in `records.py` and `collection_store/writer.py`). The store refuses a natural-key or schema-version change with `IMMUTABLE_COLLECTION_REPRESENTATION` (`_prepare_revision`). A revision records no inverse.
- **History.** A store vault keeps every manifest version in `collection_manifests` and every change as a row in `txns`. A retracted row keeps its data (`retracted_txn`). A file-only vault keeps an audit chain and an activity log.
- **Inverse receipts.** Structured-file maintenance already previews a plan, applies it atomically and records "the exact prior and final paths and content hashes needed for a guarded inverse operation" (`human-owned-structured-files`).
- **Retired aliases.** A row of a converted collection keeps its former path as a retired alias that still resolves (`collection_store/aliases.py`).
- **No collection lifecycle.** No operation merges, splits, retitles or retires a collection.

Where text reaches the user today:

- `_user_summary` in `mutation_terminal.py` builds the receipt sentence from the stored path. Over MCP, `page_names.py` and `governance/egress.py` (`_name_receipt_title`) swap a page path for its title. A store-backed Records or Planning write keeps the path, so a row append reads `Saved -> Knowledge Base/Records/<area>/<collection>/_summary.md`.
- `NARRATION_RULE` in `mutation_terminal.py` says "Tell the user the user_summary". Bootstrap's `write.canonical_loop` (`commands.py`), `SKILL.md` and each workflow skill carry it.
- The parent change teaches "Report. Name the collection and the rows in the reply."
- The capture reminder (`_hooks/exomem_capture_nudge.py`) counts a save when the agent's reply matches `Saved\s*(?:->|→|:)`.
- The two prominent custom-instruction blocks measure 1,484 and 1,495 of their 1,500 bytes (`portable-agent-contract`).

Sources, from the primary-source roster's pick-by-problem table, as the parent change cites them:

- "Autonomy creates unacceptable blast radius": How we contain Claude. Raise autonomy inside hard bounds, not approvals that users learn to click through. Here the hard bound is reversibility, proved by the server.
- "Workflow vs agent": Building effective agents. The model judges meaning; fixed code covers only fixed work. Here the agent judges what matters to the user; the server checks losslessness.
- "I can't tell whether a change actually improved the agent": Demystifying evals for AI agents. Real models play end-to-end trials, and graders check the outcome in the vault.

## Goals and non-goals

Goals:

- An agent reshapes a collection on its own when the work needs it, and never loses data doing so.
- Every change of shape can be undone with one call, and a later session can see what changed and why.
- What reaches the user names their things in plain words.
- The agent asks only when a choice is ambiguous and changes what the user gets.
- A user who wants to design a structure can, and the agent respects that design.

Non-goals:

- **Graph restructuring.** Entity merge, split and retype, and relation retype, are a named follow-up (decision 9).
- **Automatic Planning creation.** The parent change excludes it; it is a named follow-up (decision 9).
- **Permanent deletion.** This change adds no deletion path. Existing confirmed paths keep their authority.
- **Row-level removal.** Row retraction keeps its current contract.

## Decisions

### 1. One restructure operation with a closed set of steps

`change_records` and `change_plans` gain one operation, `restructure`. It takes a collection, an ordered list of steps and a `why`. It returns one receipt and one undo call. Its steps are a closed set that the operation schema fixes:

| Step | Effect |
|---|---|
| `add_field` | Adds a field. |
| `rename_field` | Renames a field and carries every value. |
| `retype_field` | Changes a field's type. |
| `retire_field` / `restore_field` | Sets a field aside with its values, or brings it back. |
| `rekey` | Changes the natural key. |
| `merge_into` | Copies every row into another collection under a field mapping, then retires this collection with a pointer to the target. The retired collection keeps its rows, readable by its identity. |
| `split_out` | Moves the rows that a Records filter selects into a new collection with the same fields and key. |
| `retitle` | Changes the title and keeps identity and location. |
| `retire` / `restore` | Sets a collection aside with its data, or brings it back. |

A `preview: true` call returns the plan and writes nothing. Without it, the server previews internally and applies in one guarded transaction that writes and retracts at most 256 rows (decision 8). `undo: <entry>` applies the recorded inverse of a history entry. `new_fields` on `add_record` and `edit_record` stays the one-call path for an addition while writing a row. It runs the same `add_field` check and writes the same history entry.

Why one operation: an agent should make one obvious call, not learn a manifest edit for each case. Why a closed set: each step has a known inverse, so the server can prove and record it. The closest existing tools do not fit. `revise` replaces a whole manifest, cannot change the key, moves no rows and records no inverse. Curation plans act on pages, and their contract keeps every apply confirm-required. The restructure reuses the store's manifest versions and transactions, the structured-file preview and inverse-receipt pattern, and the retired-alias reader. The added maintenance is one leaf with one inverse per step, shared by both profiles.

`revise` stays for what `restructure` does not cover: saved views, references, rollups and presentation recipes. It keeps its current authority.

### 2. Lossless by proof, not by a type table

Before it commits, the server applies the steps, applies their inverse to the result, and compares the outcome with the prior manifest and every prior row. It commits only when they are equal. This check holds for every restructure, automatic or requested. No step deletes a value, so no restructure can lose data.

The check decides each case from the actual values. A retype from integer to number passes. A retype from number to integer passes only when no value has a fraction. No fixed table of allowed type changes exists. The types themselves are the closed set that the manifest schema defines.

Each step's reversibility contract:

| Step | History kept | Undo | Undo refuses when |
|---|---|---|---|
| `add_field` | The manifest version | Removes the field while no row holds a value; otherwise retires it, so later values stay | Never |
| `rename_field` | The manifest version and the mapping | Renames it back | The old name is taken since |
| `retype_field` | The manifest version | Retypes it back | A later value does not convert back; the refusal names the rows |
| `retire_field` / `restore_field` | The manifest version; values never move | The opposite step | Never |
| `rekey` | The manifest version and both keys | Rekeys back | Rows written since collide under the old key; the refusal names them |
| `merge_into` | The source's own rows, which stay in the retired source; each copied row's origin; the prior version of each folded row | Retracts the copied rows from the target, puts back each folded row's prior version, and restores the source | A copied or folded row holds other values in the target since; the refusal names it |
| `split_out` | Each moved row's origin | Copies the rows back into the source and retires the new collection, with the source as its successor | Rows written since collide; the refusal names them |
| `retitle` | Both titles | Retitles back | The old title is taken since |
| `retire` / `restore` | The retirement and its `why` | The opposite step | Never |

"Changed since" compares values: a row that a later write left with the values the restructure gave it does not refuse the undo.

On a store vault, `rekey`, `merge_into` and `split_out` change which key or which collection holds a row. Such an identity move commits as one SQLite transaction made of existing operations:

1. One revise transaction for the collection that the call names. It carries the shape-history entry and binds its digest.
2. One retract transaction for each row that leaves its key or its collection.
3. One bulk upsert for each collection that takes rows. It inserts each new row, or revives the retracted row that held the key before. Every written row is read back through its collection's encoding before the commit.

`history.verify` checks each of these as it checks those operations, so the history chain needs no new kind of event. A moved row keeps its body and its sources. In a collection that renders one view file per row, the view path of a retracted row passes to the row that takes it, or its view file retires. The same transaction does both, through the store's projection state, and the view files install after the commit as they do for every write. A move retracts each row and writes it again, so one call moves at most 128 rows. A larger move waits for the resumable job (decision 8).

One case refuses `RESTRUCTURE_MOVE_UNAVAILABLE` and changes nothing, pending a choice:

- **A collection kept only in files.** It takes `rekey`, `merge_into` and `split_out` only after the operator chooses between this refusal and a file-side retraction with a guarded file removal.

A store that an ownership marker enrols adds a new collection only through its admitted create, which publishes the marker after its own commit. A split there runs that create first, as its own committed step, under a request id derived from the split's. The moves then commit in one transaction. A failure between the two leaves the new collection empty and loses no row, and a retry of the same split takes that collection over and moves the rows. Where no running service serves such a store, the create cannot run, so a split there refuses `RESTRUCTURE_MOVE_UNAVAILABLE` and changes nothing.

An undo loses no value, including values written since the restructure, and writes its own history entry. A refused undo changes nothing, and the data stays whole either way. The agent then tells the user plainly what cannot go back and asks what they want.

A merge folds two rows with the same key only when every value that both rows hold is equal. Where values conflict, the server refuses the merge, names the rows and the values, and moves nothing. That conflict is an ambiguous, outcome-changing choice (decision 7).

### 3. Retirement replaces deletion

`retire` and `retire_field` keep all data and history. A retired collection:

- leaves `search_memory`, `recall_context`, the Records routing advisory, `collection_candidate` and the default inventory;
- still answers a read by its identity, marked retired, with its successor when a merge retired it;
- refuses a new row with a pointer to the successor, or to `restore`;
- holds its title, so `add_record` with `new_collection` refuses that title and points to `restore`.

A retired field leaves the schema that agents see and refuses new values. Its stored values stay, and a query returns them on request.

The inventory always reports how many retired collections exist, and lists them on request. On a file-only vault, the item files stay where they are; the user's editor still shows them.

Why: retiring costs a flag and a filter. Deleting would make an agent's misjudgement permanent. Permanent deletion stays where it is today, behind its confirmation, and is never automatic.

### 4. Automatic restructuring follows the capture setting, within server bounds

A restructure that the agent starts on its own carries `automatic: true`. The envelope serves it as a `proactive_capture` action, as the parent change does for creation:

| Disposition | What the agent does |
|---|---|
| `silent` | Applies the restructure. It tells the user only when the change alters what they get (decision 6). |
| `advisory` | Proposes a change that alters what the user gets, in plain words, and applies it after one inline confirmation. It does not start a change that alters nothing the user gets; it works with the current shape. |
| `off` | Initiates nothing. A restructure the user asks for is still served. |

A change that fails a bound below stays `restructure_execution`: confirm-required, one inline confirmation of the named proposal. Permanent deletion stays `restructure_execution` at every prominence.

Why `advisory` skips internal-only changes: a proposal there would be an implementation question, which decision 7 forbids. A change the user would notice has a plain framing, such as "Want me to keep each service as its own entry?"

With `automatic: true` the server refuses `AUTOMATIC_RESTRUCTURE_REFUSED`, naming the bound and changing nothing, unless every bound holds. Each bound must earn its place:

| Bound | What it prevents | Cost when it fires wrongly |
|---|---|---|
| The effective `proactive_capture` is `silent` | Unasked writes against the owner's setting | None: the setting is the owner's choice |
| No step changes a decision whose origin is the user (decision 5) | An agent undoing what the user chose or designed | One plain question |
| A change that adds fields to a collection the agent created leaves at most 12 item fields | Wide, speculative schemas that every later row pays for | One plain question; a collection the owner made is as wide as the owner chose, as for `new_fields` |
| At most 3 automatic restructures of one collection in 24 hours; an undo and a field addition do not count | Two sessions flipping a shape back and forth | The agent asks, or waits a day |
| A `split_out` that creates a collection also fits the parent change's daily creation bound | A second route around the creation count | As the parent change states. The new collection takes the source's shape, which the owner already has, so the shape bounds of a drafted collection do not apply: they would refuse every split of a summary collection |

The 12-field cap is the parent change's constant. It lives once, in `automatic_collection.py`, beside its check. The daily count reads the collection's own shape history, which is indexed by collection and time.

Two checks hold for every restructure, automatic or not, so they are correctness rather than bounds: losslessness (decision 2), and that every dependent still validates. Dependents are saved views, references in both directions, rollups, derived rows, filename recipes and the Planning hierarchy. A failed check refuses with the dependent named. A saved view in another collection that reads the changed collection's fields through a reference follows the declared-references rule: it reads as `SAVED_VIEW_STALE` when it runs, as it does after a `revise`, because refusing would reveal a collection the author may not read. A reference from a collection the author cannot read still refuses the restructure, without naming that collection, because the rows it finds would otherwise stop resolving.

Bounds considered and rejected:

- **Rows touched per change.** Losslessness and one-call undo already contain the blast radius. A row bound would refuse exactly the large collections where reshaping pays most. Size is a latency concern, which decision 8 handles.
- **A vault-wide daily rate.** The realistic failure is churn on one collection. A vault-wide rate would also stop a legitimate pass over many lists.
- **A table of allowed type changes.** The round-trip check decides from the values, so a table adds nothing but drift.

The flag rests on the agent's word, as every agent-initiated write does. The bounds are not a security boundary; they stop an eager agent from churning structure.

Where the agent learns that a reshape exists: on the surfaces it reads in an ordinary turn, not only in bootstrap and the skill. A real-model round on 2026-11-23 saw sessions that never loaded either. They kept two lists of one kind apart, skipped a detail the user gave, and saved a note beside a list whose key could not hold a second occurrence. Three carriers close that gap, each loaded by its own trigger:

- The per-turn capture rules, which `recall_context` returns, add "(reshape it to fit)" to the Records route. The installed hook drops a rules text over 1,000 characters whole, so this is all the room there is.
- The Records and Planning inventory carries `reshape_route`: the restructure call for the serving surface and one paragraph on the step kinds and the capture setting (`restructure.RESHAPE_CUE`). A session reads the inventory before it writes a row.
- An `add_record` refused because a stored row already has the row's key (`RECORD_ID_CONFLICT`, `RECORD_NATURAL_KEY_CONFLICT`), when the key came from the row's values, names the rekey that keeps both occurrences. The code's own remedy, to update the stored row, would replace the earlier occurrence.
- `add_record`'s `new_collection` takes `origin: user_design` when the user named the details to keep. That create is the user's, so no automatic-creation bound applies, and a later automatic reshape cannot change what they chose (decision 5).

### 5. Shape history and decision origins

Every create, restructure, undo and `new_fields` addition writes one shape-history entry for its collection. A merge or a split writes it on the collection that the call names, which is the source; its steps name the target by identity. The entry holds the time, the steps, the inverse steps, the `why`, the origin, and the undo call. A store vault writes it in the create's or restructure's transaction, as one row of its own table, indexed by collection and time. A store `inspect` pages the history by keyset over that index. The create or revise event carries the row's digest, so the history chain covers the entry. A create's entry is the collection's first and holds no steps, so it has no undo. A file-only vault writes it in the collection's activity log, beside the parent change's create marker.

The origin is one of a closed set:

- `automatic`: the agent started it on its own;
- `user_choice`: the user answered a question with yes;
- `user_design`: the user described the shape themselves;
- `declined`: the user answered no; the entry records the proposal and changes nothing.

`read_records` and `read_plans` `inspect` page through the full history. The inventory shows the 5 newest entries per collection and the total, so its cost stays flat as history grows.

What a later session sees: the shape, what changed recently, who decided it, and how to undo it. An automatic restructure never changes a `user_choice` or `user_design` decision. The agent does not ask again about a `declined` proposal unless the user raises the subject.

Explicit design stays possible and is never required. A user can describe a structure in their own words. For example: "keep my reading list with the title, the author and a rating out of five". The agent applies it through `create` or `restructure` with origin `user_design`. It restates the result in the user's words, and asks at most one question about an ambiguous, outcome-changing part.

### 6. Users see their own things in plain words

The rule covers every text meant for the user. Agent-facing text, such as tool descriptions, bootstrap and the inventory, keeps the internal names.

| Surface | Change |
|---|---|
| `user_summary` of a Records or Planning row write | Names the row by its label and the collection by its title, never by path. The row label is the row's title field when the manifest declares one, else its natural-key values. |
| `user_summary` of a restructure | States the effect on what the user keeps, by step kind: an added, retired or restored field, a merge, a split, a retitle, a retired or restored collection. A rename, a retype or a rekey carries no `user_summary`. |
| `NARRATION_RULE` | Adds two sentences. Speak of the user's things in their words, never of collections, Records, Planning, schemas, entities or paths. A change without a `user_summary` needs no mention unless asked. |
| Parent change's served Records procedure | "Report. Name the collection and the rows" becomes "Report what you kept, in the user's words". |
| Custom-instruction blocks | Their confirmed merge rule names an Entity merge, so it no longer reads as "confirm every merge of two lists" (decision 4). Nothing else: the Maximal block keeps the 11 bytes of headroom its test holds, and the others sit within 30 bytes of the 1,500-byte web cap. |
| Per-turn capture rules | No plain-words copy yet: they sit 5 characters under the installed hook's 1,000-character cap, and `say` 34 under its 1,600. If acceptance shows replies still naming internals once the reshape works, the write receipt, read when the reply is composed, is the carrier to try. |
| Capture reminder | Stops matching `Saved ->` in the agent's reply. It counts a save from the write's tool result alone. |

A plain `user_summary` discloses nothing that the path did not: the path already held the collection's name. The row label comes from the row the request carries and appears only when the Records full-release check releases every file the write touched, the floor that releases a page title today. Otherwise the summary names the collection alone. The stored terminal keeps no title or value, so the sentence is composed from the receipt and the vault's current state when the reply is projected. An update or a retraction carries no row and says "an entry".

What surfaces and what stays silent: the agent tells the user when a change alters what they get. Examples are a new detail it will keep, two of their lists becoming one, a list set aside or brought back. It stays silent about reshaping that leaves what they get unchanged. Whether a change matters is the agent's judgement in context. The server's part is a closed rule per step kind for `user_summary`.

Why the capture reminder changes: plain replies no longer carry the marker, so the old regex would miss real saves on clients where it was the only signal. Reading the agent's prose for a marker is also the kind of word matching the repository forbids (C6).

### 7. One plain question, only when it matters

The agent asks when both hold:

- more than one reasonable shape fits what the user said; and
- the choice changes what the user gets later: what is kept, what they can get back, or which value counts.

Otherwise it decides under `silent`, or proposes under `advisory`.

The question is one sentence about the user's things, in their words, with the recommended answer first. For example: "Want me to also keep track of the price each time?" or "Your two plant lists give different watering days for the fern: is it Monday or Thursday?" It never names a field, a key, a type, a collection or a schema. A reply asks at most one such question. Other open choices wait for a later turn.

The answer is kept with the structure it decides (decision 5). A yes applies the change with origin `user_choice`. A no writes a `declined` entry. Both reach every later session through the inventory.

Recognising ambiguity and outcome is the model's judgement. No word list or threshold decides it.

### 8. Large collections keep their latency

The Records load target is 100,000 rows on one personal service, with any single boundary hold at p99 ≤ 0.5 s. A restructure commits in one write transaction, and one call writes and retracts at most 256 rows together (`MAX_REWRITTEN_ROWS` in `restructure.py`), the size whose hold stays inside that budget for every step kind and its undo on a loaded machine. At 512 a loaded laptop held 0.60-0.66 s. At 384 the store without a marker stayed inside the budget, but through the service a merge held up to 0.58 s at load 13-14, nearly all of it CPU: the same merge took 0.31-0.54 s of CPU across three runs. A larger restructure refuses `RESTRUCTURE_TOO_MANY_ROWS`, naming the row count and a way forward, and changes nothing. The hold also grows with the rows of the collection, because every step re-validates or copies each of them even when it writes few: at 100,000 rows a retitle held 8.95 s and a refused rename 2.21 s. A restructure or an undo of a collection of more than 4,000 current rows (`MAX_COLLECTION_ROWS`) therefore refuses the same way, and so does a merge that would leave its target above that. A store collection counts its rows in SQL before it loads one, a file collection takes the count from its snapshot, and a step that only adds fields is bounded too. `new_fields` on a record write keeps its own path. The inventory's history and the daily count read indexed, bounded rows. Retired collections leave recall through one indexed flag, not a per-item check.

Proof: the hold of a restructure at the 256-row bound for each step kind and its undo, on an enrolled store through the service and on a store without a marker, measured with three repeats. The collection bound comes from `scripts/measure-restructure-hold.py`, which times each step kind and its undo at a given collection size: the worst hold was 0.363 s at 4,000 rows, 0.454 s at 6,000 and 0.543 s at 8,000 on a store without a marker. The bound is 4,000, because at 384 rewritten rows a merge held about a fifth longer through the service (0.576 s) than on a store without a marker (0.472 s).

A restructure of more rows as a resumable job is a follow-up change, because the store cannot run that job yet. The typed-encoding migration builds its candidate without new versions, because a re-encoding keeps every value. A restructure changes values, so each rewritten row needs a new version. Each new version and its effect bind to a committed transaction by a foreign key, so a candidate cannot hold its versions outside the chain until it publishes. Each row's current version lives in its `item_rows` row, so even a publish that only moves those pointers costs time in proportion to the rows, and at the 100,000-row target that alone breaks the hold budget. The job needs a store change first:

- a current-version relation per collection that one statement swaps, so that a publish costs only the rows changed since their copy;
- a chain event that stages candidate versions without making them current, a publish event that adopts them, and a rule for the versions that a cancelled job leaves;
- one retraction event with many effects, so that a large move publishes as one event.

### 9. Scope: Records and Planning now; the graph as a follow-up

Records and Planning share the collection substrate, so the restructure lives there once. Planning adds one rule: its core fields and lifecycle belong to the Planning profile, so steps apply only to fields that a collection declares beyond the core. A merge or split keeps every Planning item identity and its hierarchy.

The plain-words rule covers every write now, graph writes included, because it lives in the one narration rule.

Graph restructuring is a separate change, for three reasons:

- `promote-referents-without-nudges` already owns entity creation, typed edges under `proactive_capture`, and one revert per promotion.
- `activate-agent-led-vocabulary-evolution` owns the opt-in additive authority for entity and relation types.
- Entity merge and supersession rewrite links across pages that the user wrote. Their undo is the curation compensation plan, not a collection inverse.

Folding them in would make this change wait on two in-flight changes. The follow-up applies the same principles: lossless, a recorded inverse, retirement over deletion, plain words.

Automatic Planning creation is also a follow-up. Its recognition differs: stated intent routes to existing Planning. The parent change's bounds need a natural key, which Planning items do not use.

### 10. Where the spec text goes

| Capability | Delta |
|---|---|
| `structured-collections` | ADDED: the restructure, retirement, the automatic bounds, shape history, and what reaches the user. |
| `records` | ADDED: a fact that the key cannot hold reshapes the collection. |
| `planning` | ADDED: a restructure keeps the Planning core. |
| `delegation-envelope` | ADDED: the classification of restructuring. |
| `mutation-terminal-contract` | ADDED: structured receipts name the user's things. MODIFIED: the narration rule. |
| `records-release-acceptance` | ADDED: real-model acceptance. |

`Compact Success Is The Default Projection` says that a SQLite-backed Records or Planning write keeps its path summary. Three in-flight changes already modify that requirement. This change adds a requirement that takes precedence for Records and Planning writes, instead of a fourth modification. `tasks.md` folds the precedence into that requirement once those changes have archived.

`replace-agent-tool-surface` also modifies the narration requirement. Whichever change archives second refreshes its block.

Several parent requirements contradict this change: the envelope's creation requirement says merges and schema changes stay confirm-required, and the Records route says to name the collection. They exist only in the parent change, so this change writes no delta for them. `tasks.md` lists each one to refresh after the parent archives.

## Acceptance

The incident regression suite (`benchmarks/membench/outcome/incidents/`) plays real models against disposable vaults. It uses the parent change's multi-turn sessions, follow-up sessions, seeded collections and blind judge, with a Claude Code actor and a hookless actor. It needs three new checks:

- **`restructure`** passes when the collection's shape history holds the expected step kind with the expected origin, and its undo call is present.
- **`rows_preserved`** passes when every seeded value is still readable, through the recorded mapping, and the row count matches the expected union.
- **`plain_reply`** fails on any vault path in a reply meant for the user. A blind judge then grades whether the reply names the user's things in their terms, with no internal names. It also grades how many questions the session asked, and whether each was about the user's things.

The cases use invented domains:

| Case | Disposition | What passes |
|---|---|---|
| A later fact needs a new key | `silent` | A seeded service log keyed by part gets a second service of one part. The agent rekeys by part and date, keeps every earlier row, asks nothing, and its reply names no internals. File-only and store variants. |
| Two overlapping lists merge | `silent` | Two seeded plant lists share two plants with equal values. The agent merges them; the union of rows survives; one list is retired, not deleted. File-only and store variants. |
| Overlapping lists disagree | `silent` | The shared plant has two watering days. The agent asks one plain question and merges nothing until the answer. The answer is applied with origin `user_choice`. |
| A new detail matters | `silent` | Quotes start carrying a price. The agent keeps the price without asking and tells the user in plain words that it now keeps it. |
| The user designs the list | `silent` | The user describes a reading list. The agent builds that shape with origin `user_design`. A later automatic change leaves the user's fields alone. |
| Put it back | `silent` | After an automatic merge, the user asks to keep the lists apart. The agent undoes the merge, and both lists match their pre-merge rows. |
| Advisory proposes plainly | `advisory` | The service-log case. The agent asks one plain question and restructures nothing before the answer. |
| Capture off | `off` | The service-log case. No restructure happens. |

The run follows the eval-loop method:

1. Run the parent change's head (base) against this change (changed) at one draw on every case.
2. Read each failure's trace and fix its cause.
3. Hand over the passing cases at three draws, reported as right on every draw.

The record goes in the eval-runs collection before any number is quoted. Both actors run on a subscription login: the hookless actor is Claude Code with every hook disabled. A run that needs metered spend states its cost before it starts.

## Risks

- **The agent restructures too eagerly.** Losslessness and undo make each mistake cheap. The daily rate stops churn, and the "nothing changes" side of each case measures over-eagerness.
- **Undo after later writes.** An undo can refuse when later rows no longer fit the old shape. The data stays whole, and the refusal names the rows, so the agent can ask.
- **Bytes on carriers.** The narration rule grows by two sentences on the compact bootstrap core, which sits at its ceiling. Each sentence is measured against the ceiling and trimmed where it must be.
- **Retired data still on disk.** A file-only vault keeps a retired collection's files in place. That is intended: the files are the user's, and retirement only removes them from agent recall.

## Open questions

- Should the advisory disposition apply internal-only changes silently instead of skipping them? This design skips them, because asking would be an implementation question. Acceptance will show whether skipping leaves shapes that hurt later work.
