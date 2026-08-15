# Records

Records are Exomem's human-owned layer for observed events and current state:
training sessions, symptoms, measurements, transactions, maintenance, inventory,
operational logs, and other longitudinal data. They are not a new database and
they do not replace Notes or Planning.

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

The comparison itself lives in `review_memory(mode="plan-progress")`, which
reaches Records only as an ordinary governed reader — released manifest,
authorized saved view, default-deny envelope — and takes counts rather than
rows. A view read as plan evidence is exactly the same neutral view it is when
read directly.

For software work, Exomem owns durable intent and multi-horizon planning.
OpenSpec and the repository own accepted software change contracts and execution
truth; git, specifications, tests, and code remain authoritative. Outcomes and
reusable lessons can then become governed Notes.

## One public action surface

Use `record_memory`, not a collection of format-specific tools. It has exactly
five actions:

| Action | Use it for |
| --- | --- |
| `inspect` | Report a collection's contract, direct-edit drift, schema issues, template availability, or bounded audit gaps. It never repairs canonical data. |
| `create` | Explicitly create a reviewed collection manifest and optional empty source scaffold. It never silently adopts a tracker. |
| `query` | Read a bounded current view, filters, aggregation, saved view, continuation, or optional bounded agent history. Derived JSON, Markdown, and CSV output is not persisted. |
| `append` | Safely add a new item with a concise reason and current container guard. |
| `update` | Target one item with its item key, current container and item-version guards, and a concise reason. |

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

Use `maintain_memory(mode="reconcile")` for derived-index repair, never as a
way to rewrite or reinterpret a person's canonical history.

## Human-owned storage

A collection declares or infers one canonical storage strategy. All three forms
remain ordinary files that a person can open and change without an AI client:

1. **Chronological Markdown log** — fast append-heavy dated or stably identified
   blocks, such as a training log.
2. **Markdown item files** — one readable file per item with typed YAML
   properties and an optional Markdown body; suitable for Obsidian Properties
   and Bases-style views without requiring Obsidian at runtime.
3. **Structured dataset** — CSV, TSV, or JSON for row-heavy data, exact filters,
   aggregates, and longitudinal analysis. Dataset mutation is deliberately
   refused in this first delivery.

There is no opaque canonical database. Indexes, audit state, and generated views
are rebuildable from the declared source files. A generated dataset from a log is
derived until a person explicitly promotes it through a provenance-preserving
migration. `record_memory` does not automatically promote, migrate, or persist a
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
Users keep the normal **Templates → Insert template** workflow. Exomem does not
mutate `.obsidian`. When the Knowledge Base directory is the Obsidian vault,
`.obsidian/templates.json` names the relative folder `Templates`, which is exactly
that Exomem path. A parent-directory Obsidian configuration is a different vault
and does not override the Knowledge Base setting.

Fresh structured queries read canonical files, so a direct change in an editor
is immediately visible. `inspect` is report-only: it can identify direct-edit
drift, duplicate or missing identities, schema violations, missing templates,
and bounded agent-audit gaps, but it does not repair canonical files. Reconcile
repairs only derived state.

## Compatibility, safety, and retrieval

Existing `type: tracker` material stays usable. Without an adjacent reviewed
manifest, it is discovered and inspected at collection level only; Exomem does
not guess its grammar or parse its items. Adding an adjacent manifest explicitly
enables query/mutation while leaving the tracker body, notation, legend, archive,
and templates intact. New agent-authored log items get prospective stable markers;
historical entries are not mass-rewritten. Removing the manifest returns the
tracker to ordinary manual use. The X3 training log remains canonical and has no
forced migration.

Agent writes use stable item identity, stale-write guards, bounded edits, atomic
publication, same-vault mutation serialisation, auditable reasons, and receipts.
They refuse ambiguous or stale targets rather than replacing a whole large file.
An agent-history view is bounded, content-free where appropriate, and can report
that history is incomplete after direct editing.

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
storage engine. Pack guidance helps route a simple request to `record_memory`;
the collection contract remains explicit and human-owned.
