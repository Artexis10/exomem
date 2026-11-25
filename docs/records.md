<!-- authority:non-specification -->

# Records

Records are Evermatter's human-owned layer for observed events and current state:
training sessions, symptoms, measurements, transactions, maintenance, inventory,
operational logs, and other longitudinal data. They complement Notes and Planning.

## Product boundary

| Layer | Holds |
| --- | --- |
| Sources | Externally received raw material. |
| Evidence | Proof-bearing artifacts for claims, compliance, warranties, disputes, or provenance-sensitive history. |
| Notes | Compiled conclusions, decisions, research, insights, patterns, failures, experiments, and productions. |
| Entities | Stable reusable identities such as people, organisations, concepts, libraries, assets, and decisions. |
| Planning | Intended future state: goals, desired outcomes, initiatives, priorities, horizons, commitments, and candidate work. |
| Records | Observed state and event history: what happened, when, measurements, sessions, transactions, and status changes. |
| Review | The loop that compares intent with observed reality and creates explicit decisions or conclusions. |
| Imported | Adoption and migration staging, not the permanent home of live user-owned state. |

Planning and Records are different semantic profiles on one structured-collection
substrate. Planning owns durable intent and prioritisation; Records own facts and
history. A plan may point to a Record collection as observed evidence, and a
Record may point to a plan, goal, initiative, protocol, project, asset, person,
or decision. Those links are opaque reference/query descriptors: Records do not
resolve Planning, duplicate a plan's history, or infer progress, completion,
medical conclusions, or personal judgments.

A Planning link may additionally carry a bounded `join`: one to four pairs
mapping a declared record field to a plan field name, matched exactly. It is an
authored declaration of which events are about which intended work, and Records
still resolves nothing — validation checks the record side against this
manifest's own schema and leaves the plan side as opaque text. Its only consumer
is the Epistemic Inbox's `unreflected_outcomes` family, which reports an open
Planning item that recorded events already join to. Nothing transitions the item:
the decider moves it with `edit_plan_item`, and the finding clears
when the item leaves the open state, the binding is removed, or the joined
records are gone.

The comparison itself lives in `list_review_items(queue="plan_progress")`, which
reaches Records only as an ordinary governed reader — released manifest,
authorized saved view, default-deny envelope — and takes counts rather than
rows. A view read as plan evidence is exactly the same neutral view it is when
read directly.

Planning owns durable intent and multi-horizon planning. A resolved workflow
contract may declare a companion owner for external execution artifacts; Records
preserves only observed outcomes and opaque references, never companion state.
Outcomes and reusable lessons can then become governed Notes.

## Agent-initiated collections

Recurring structured work (stable identities, repeated observations, comparisons, changing
state, a need to query exactly) belongs in Records. When no compatible collection exists and
the identity, type and scope are clear, the agent creates one minimal collection. Your
`proactive_capture` setting decides how:

| Setting | What the agent does |
| --- | --- |
| `silent` | Creates the collection and adds the first row in one `add_record` call with `new_collection`, and reports both in the same reply. |
| `advisory` | Proposes the collection and creates it after one inline confirmation. |
| `off` | Creates nothing on its own. A creation you ask for is still served. |

`new_collection` is `{"natural_key": [...]}`, the fields of the row that identify it. When
`collection` is a title that names no collection, the server drafts a minimal manifest from the
row: one field per key of `item`, typed from its value, with the natural key required. It
creates the collection as an automatic creation and appends the row, as one call with one
receipt that names the created collection. A title that names a collection makes the call a
plain append. A refusal creates nothing and returns the create call, with the manifest it
would have made, for the agent to make after you confirm.

A collection is rarely complete at its first row. When a later fact has no field, such as a
claim from an outside source, the agent adds the field in the same call that writes the row:
`add_record` or `edit_record` with `options: {"new_fields": [...]}`, which names the fields of
the row that the collection does not declare. Under `silent` the server adds each one, typed
from its value (a whole number and a decimal are both a number), leaves the earlier rows
without a value for it, and reports `added_fields` in the reply. Under `advisory` or `off`, or
when an automatic collection would pass 12 fields, the call is refused with a proposal and the
confirmed `revise` that makes the change: under `advisory` the agent proposes it first, and
under `off` it makes it only if you asked. Renaming, retyping or removing a field is never
automatic.

An automatic creation, whether through `new_collection` or `records.create` with
`automatic: true`, gets `AUTOMATIC_CREATION_REFUSED`: the server names the failed
bound and creates nothing, unless all of these hold: capture is `silent`; the collection is a
Records collection with a declared natural key and a short field list, where the properties of
an object field count too; it declares only the keys of a minimal manifest, so no saved views,
references, rollups, templates or governance; and the vault has created fewer than three collections
automatically in the last 24 hours. A vault with a collection store and a vault of plain
files both qualify. The limits live in `exomem.automatic_collection`. Every other creation,
every Planning collection and every schema change other than adding fields, merge or deletion still needs your
confirmation. The inventory shows `created_automatically` for each collection, so you can
review what the agent created. A file collection whose create the inventory cannot place in
the recent activity log shows `null`.

## One public action surface

Use `query_records`, `add_record` and `edit_record`, and the `records` operations of `inspect_memory` and `manage_memory`, for collection actions:

| Action | Use it for |
| --- | --- |
| `inspect` | Report a collection's contract, direct-edit drift, schema issues, template availability, or bounded audit gaps. It never repairs canonical data. |
| `create` | Create a reviewed collection manifest and optional empty source scaffold. It never silently adopts a tracker. An agent starting a minimal collection on its own passes `automatic`, or lets `add_record` create it with `new_collection`; see [Agent-initiated collections](#agent-initiated-collections). |
| `query` | Read a bounded current view, filters, aggregation, saved view, continuation, or optional bounded agent history. Derived JSON, Markdown, and CSV output is not persisted. |
| `append` | Safely add a new item with a concise reason and current container guard. |
| `update` | Target one item with its item key, current container and item-version guards, and a concise reason. |
| `retract` | Take one row of a store-backed summary or log collection out of current state, with a reason and the current container and item-version guards. The row keeps its history. |
| `history` | Read retained prior payloads for one authorized item, with bounded pagination and explicit missing legacy content. Name the row by exactly one of `item_key`, `item_id` (a stale id still names its row) or `natural_key` (an object of the collection's natural-key values; a retracted row is found by it too). |
| `revise` | Change a collection declaration with current manifest and container guards. |
| `rebaseline` | Acknowledge an exact, positively established imported audit gap. |
| `bulk_upsert` | Import a bounded batch of observations from preserved source references. |
| `import` | Manage a resumable collection import. |
| `discard` | Discard a held candidate with a reason. |
| `describe` / `validate` | Read the contract or validate a proposed collection without committing it. |

## Collections in the store

An admitted collection keeps its canonical declaration, rows, versions and audit
history in SQLite. Collections outside that route retain file authority. The
migration release gate controls admission; the presence of this reference does
not enable migration.

Markdown files remain readable views at the collection's existing paths. The
service acknowledges the durable SQLite save, then publishes item views,
manifests, log views and history pages asynchronously. Structured queries see
the saved record immediately. `get_page` reads only a valid physical view;
that file can lag the saved record. `inspect` reports pending
views and held view corrections without writing, repairing or publishing them.

The service turns valid item or declaration edits into governed transitions.
Invalid or stale edits become held corrections. History pages are read-only;
the service replaces edits to those pages with their canonical render.

After migration, the service stamps imported views through normal publication.
Migration itself leaves their files unchanged. `validate` checks a proposed
declaration without saving it and returns the current guards for `revise`.

Reverse export restores exact imported bytes for collections without native
mutations, including after automatic stamping. A governed file move counts as
a native mutation. Keep a qualified archive-aware release for application rollback.
Returning to file authority uses whole-vault export and its eligibility rules.

Each collection has `_history.md` with its newest 200 released transitions.
Numbered pages under `_history/` hold older transitions in groups of 500,
newest first within each page. Pages contain operations, reasons and item links,
without item values. Every page uses the intersection of its readers' release.
A mixed bulk transition omits its reason when any effect is withheld.

`query(include_agent_history=true)` returns bounded, governed audit events.
`history` returns prior item payloads and distinguishes missing legacy content
from retained content. Import keeps the original audit assessment. Rebaseline
can acknowledge an imported positive gap; it preserves the discontinuity after
later writes. Native history and unknown imported history cannot be rebaselined.
Store mutations leave existing activity-log history untouched.

`retract` removes one row from current state without deleting it. The row leaves
queries, counts, exports, id binding and the rendered summary or log, and
`history` still lists every retained version and the retraction with its reason.
A retracted row does not hold its natural key. Appending that key again, or
importing it from a source, brings back the same row as its next version, with
the same item key and the same item id. A retracted row and an absent row refuse
alike, so a refusal never confirms that the row once existed. A collection that
renders one view file per row, and a collection that keeps file authority,
refuse with `RETRACT_UNAVAILABLE`, because the file would remain.

A collection in the store also keeps when each value took effect, apart from
when it was written. `describe` returns the whole `timeline` contract with a
worked example.

- `effective_at` on `append`, `update` and `bulk_upsert` states when the
  written values took effect: a date such as 2026-03-07, or an RFC 3339 instant
  with its offset, no later than the write. Without it, the values take effect
  when the write is made, with basis `written`.
- An `update` earlier than a field's latest point records a past value and
  keeps the current value. `append` and `bulk_upsert` refuse such a time with
  `EFFECTIVE_BEFORE_CURRENT`.
- `corrects` on `update` takes the reply's `audit_correlation` of the write to
  replace. `history` keeps both writes, and each names the other.
- `query` with `timeline: {field, value?, at? or from?/to?, changes?}` lists one
  field's spans: each value with the effective times it held from and to.
  `filters` select rows by their current values. A withheld field is refused as
  an undeclared one, and a withheld row adds no span, total or continuation.
- A selection whose rows hold more versions than the bound that `describe`
  names reads the field's span index. The first such query starts the build and
  answers `TIMELINE_WARMING` with `retry_after_ms` and its progress. It never
  answers with a partial list.
  `inspect` lists each built index under `timeline_indexes` with its state.

File-canonical, dataset and derived collections keep no timeline. They refuse
`effective_at`, `corrects` and `timeline` with `TIMELINE_UNAVAILABLE`, and an
export preview names the timeline facts that an export drops.

One service holds the writer lease. Two independent writers against the same
store are unsupported. Backups use the collection backup command.
A direct copy of an open SQLite file can omit
committed WAL data and is not a supported backup.

Natural routing stays simple:

- “Log this training session.” → `append`
- “Record today's symptoms.” → `append`
- “Add this purchase to the equipment history.” → `append`
- “Update the mileage on the car.” → `update`
- “Log this blood pressure measurement.” → `append`
- “Add this transaction to the ledger.” → `append`
- “Show my X3 progression over the last three months.” → `query`
- “Create a health-journal collection.” → `create`
- “Give me a template for logging these sessions.” → `inspect`, then use the ordinary template directly

Use remote `maintain_memory(mode="reconcile", dry_run=true)` to preview derived-index
repair, then start it as a maintenance job with the preview's `plan_id` and a `why` (the
host CLI is `exomem maintain --reconcile`); never use it as a way to rewrite or
reinterpret a person's canonical history.

## Human-owned storage

A collection with file authority declares or infers one canonical storage strategy.
These forms remain ordinary files that a person can open and change without an AI client:

1. **Chronological Markdown log** — fast append-heavy dated or stably identified
   blocks, such as a training log.
2. **Markdown item files** — one readable file per item with typed YAML
   properties and an optional Markdown body; suitable for Obsidian Properties
   and Bases-style views without requiring Obsidian at runtime.
3. **Structured dataset** — CSV, TSV, or JSON for row-heavy data, exact filters,
   aggregates, and longitudinal analysis. Dataset mutation is deliberately
   refused in this first delivery.

For collections with file authority, indexes, audit state, and generated views
are rebuildable from the declared source files. A generated dataset from a log is
derived until a person explicitly promotes it through a provenance-preserving
migration. The Records tools do not automatically promote, migrate, or persist a
summary/chart/export as canonical data. Every derived query response identifies
its source collection, exact query or saved-view definition, source snapshot,
generation time, and derived status. Persistent summary materialisation is
deferred until it has an explicit governed provenance and disclosure contract.

## Templates and direct editing

Templates are ordinary editable entry scaffolds. They can recommend default
properties, validation, examples, capture guidance, and future form ideas, but
the binding schema stays in the collection contract. Changing a template never
rewrites history, and inserting one never requires an agent, Obsidian, or a
plugin.

The intended ordinary Obsidian template root is `Knowledge Base/Templates/`.
Users keep the normal **Templates → Insert template** workflow. Evermatter does not
mutate `.obsidian`. When the Knowledge Base directory is the Obsidian vault,
`.obsidian/templates.json` names the relative folder `Templates`, which is exactly
that Evermatter path. A parent-directory Obsidian configuration is a different vault
and does not override the Knowledge Base setting.

For collections with file authority, fresh structured queries read canonical files,
so a direct change in an editor is immediately visible. Admitted SQLite collections
read committed SQL state and publish governed Markdown views in the background.
`inspect` is report-only: it can identify direct-edit
drift, duplicate or missing identities, schema violations, missing templates,
and bounded agent-audit gaps, but it does not repair canonical files. Reconcile
repairs only derived state.

## Compatibility, safety, and retrieval

Existing `type: tracker` material stays usable. Without an adjacent reviewed
manifest, it is discovered and inspected at collection level only; Evermatter does
not guess its grammar or parse its items. Adding an adjacent manifest explicitly
enables query/mutation while leaving the tracker body, notation, legend, archive,
and templates intact. New agent-authored log items get prospective stable markers;
historical entries are not mass-rewritten. Removing the manifest returns the
tracker to ordinary manual use. The X3 training log remains canonical and has no
forced migration.

### Natural-key identity is enforced on every write

A collection that declares an `item_schema.natural_key` holds at most one item
per key. Both writes are checked: an append whose derived key already exists
under another identity refuses, and so does an update that would MOVE an item
onto another item's key. Planning updates run through the same writer and
inherit the rule; Planning triage cannot reach `title`, the natural key these
collections declare, so ordinary lifecycle work is unaffected — a collection
keyed on a field triage CAN reach, such as `status`, would refuse the same way.
The refusal is
`RECORD_NATURAL_KEY_CONFLICT` and it names every existing item holding the key.

Recovery, for a collection that already holds two items under one key — a state
older vaults could reach before the append check existed, and one in which every
further append for that key refuses: update one of the named items to a distinct
natural key, or delete/archive one, then retry. An update that leaves the key
unchanged is never refused, so the corrective edit itself always goes through.

Agent writes use stable item identity, stale-write guards, bounded edits, durable
commits, same-vault mutation serialisation, auditable reasons, and receipts.
They refuse ambiguous or stale targets rather than replacing a whole large file.
An agent-history view is bounded and content-free. File collections can report
audit gaps after direct editing. Store collections record accepted edits as
transitions and preserve imported gaps as provenance.

Governance applies before a Record row, link, history field, or aggregate is
reduced or rendered. Records require the structured-read L6 boundary; an
aggregate cannot leak rows a caller could not read. The first delivery governs a
canonical log or dataset at file/collection granularity. Collections that need
mixed sensitivity use separate files or collections until explicit row-level
policy exists.
High-volume raw Records are excluded from ordinary semantic recall. Collection
manifests and compiled Notes remain discoverable; use a bounded structured query
for raw history. Any conclusion from Records belongs in a compiled Note that
links back to the relevant collection or query.

## Knowledge packs

The Health / athletic and Personal records packs can recommend Record use,
folders, fields, templates, views, and review workflows. They remain
declarative suggestions: selecting a pack neither creates collections nor
folders, migrates data, activates a schema, or introduces a domain-specific
storage engine. Pack guidance helps route a simple request to the Records tools;
the collection contract remains explicit and human-owned.
