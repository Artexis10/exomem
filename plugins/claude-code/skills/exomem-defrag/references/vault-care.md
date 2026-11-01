# Vault structure, adoption, and review

## Vault layout

```
<vault>/Knowledge Base/
├── index.md                      Top-level catalog; updated on every write
├── log.md                        Append-only activity log; most recent first
├── _access.yaml                  (optional) per-subtree readonly/excluded — see references/write-scope.md
├── _Schema/
│   ├── SKILL.md                  This file (canonical)
│   ├── project-keys.yaml         Registered research scope keys
│   ├── workflow-skills/          Named agent workflows built on the core contract
│   └── references/
│       ├── page-types.md         Page-type taxonomy
│       ├── frontmatter.md        Frontmatter spec for each page type
│       ├── write-scope.md        What's writeable vs. read-only
│       ├── supersession.md       Supersession protocol
│       ├── operations.md         Detailed per-operation specs
│       └── audit-checks.md       Per-check detail for the audit operation
├── Sources/
│   ├── Articles/                 Captured web/PDF content
│   ├── Sessions/                 Conversation transcripts OR session captures
│   ├── Books/                    Book notes/excerpts
│   ├── Papers/                   Academic papers
│   └── Videos/                   Video transcripts/notes
├── Notes/
│   ├── Research/<scope>/         Project- or domain-scoped research (incl. hubs + snapshots)
│   ├── Insights/                 Distilled cross-cutting lessons
│   ├── Failures/                 Documented failure modes
│   ├── Patterns/                 Reusable patterns
│   ├── Experiments/<domain>/     Primary experiments — protocol/data/results
│   └── Productions/<medium>/     Creative artifacts + production knowledge
├── Entities/
│   ├── People/
│   ├── Concepts/
│   ├── Libraries/
│   └── Decisions/
└── Evidence/
    └── <scope>/                  Per-incident binary/document/factual preservation
```

**This tree is the `Knowledge Base/` layer only — not the shape of your whole vault.**
The vault around it is yours: any top-level folders you keep (`Daily/`, `Projects/`,
`Reference/`, a journal — whatever) sit *beside* `Knowledge Base/` and are **read-only
input** to this skill. Don't infer a fixed vault shape from the tree above. On your
first engagement in a vault, run `browse_memory` once to learn its real top-level layout
(see § Assessing or adopting a vault you didn't build), then treat everything outside
`Knowledge Base/` as read-only. Only `Knowledge Base/` is governed and writeable.

`<vault>` resolves to your markdown vault root (Obsidian optional) — the folder that
contains `Knowledge Base/`, set via `EXOMEM_VAULT_PATH`. Verify allowed filesystem
paths before writing.

The research scopes are an open set you grow over time, registered in
`_Schema/project-keys.yaml` (see § Research scope keys). New users start with a
small set (e.g. `personal`, `project-alpha`, `work`) and add their own.

### Assessing or adopting a vault you didn't build

**Make this your first move in any unfamiliar vault.** For import/adoption questions, run **read_import(operation={"kind": "scan-only"})** first: it wraps the bounded scan, states the read-only contract, suggests likely knowledge packs, reports a bounded semantic census, and lists safe next actions. It never rewrites originals in scan-only mode; explicit write modes stay under `Knowledge Base/` and either save the manifest or copy selected legacy text files as Sources with original path/hash provenance. `compile-selected` returns a proposal, not a compiled note: review it, then call `note()` so the normal semantic precommit contract still applies.

For structural questions —
"what does this vault look like," "how is this vault organized," "is there junk in
here" — and simply to learn the layout before you write, run **browse_memory** first: one bounded,
read-only report of folder structure, counts, frontmatter coverage, naming
patterns, and junk candidates (zero-byte files, sync-conflict duplicates). It
works on any folder under the vault root, including trees outside
`Knowledge Base/` (a `Daily/` or `Journal/` folder), and on vaults with no KB at
all. The folders it reports *outside* `Knowledge Base/` are read-only input — link
to them, never write them; only `Knowledge Base/` is governed. Drill down from there: `browse_memory` on folders of interest,
`search_memory scope="vault"` for content, targeted `read_memory` for individual files. Use adoption output to decide whether to save a manifest, copy selected originals into Sources, or compile selected material into governed notes; do not rewrite the old vault by default. **Never
bulk-read a vault file-by-file to answer a structural question** — the report
answers in one call what would otherwise cost hundreds of reads.

## Activity log

`log.md` at the vault root is the append-only chronological record of every
confirmed write. **Most recent first.** Format per entry:

```
## [YYYY-MM-DD] <op> | <title>

<one-paragraph description summarising what was written and why>
```

Distinction from `index.md`:

- **`log.md`** is the *activity feed* — chronological, durable, content-focused.
- **`index.md` § Recent activity** is a *cap-50 view* derived from log.md — terse
  one-line summaries for quick navigation. When log.md grows beyond cap, older
  entries fall off the index but remain in log.md.

Both update on every confirmed write.

## Audit (lint) checks

The **read_review/triage_memory(mode="audit")** operation runs read-only checks and proposes fixes (never
auto-fixes); every audit presentation remains read-only. The default
`detail="actionable"` puts current blockers first, then malformed semantic units
and unregistered relation work, then ordinary findings. Grandfathered relation-
disposition debt is grouped into one deterministic `legacy_backlog` with exact
counts and bounded samples instead of flooding the action list. Use
`detail="full"` to enumerate the raw findings; `legacy_sample_limit` is an
integer from 0 through 50 and controls samples in actionable output only. The
checks cover:
orphans, broken wikilinks, supersession integrity, stale frontmatter,
`index.md`/`log.md` drift, aged unprocessed sources (oldest-first — pair with
`propose_compilation`), status/location mismatch, unfinished experiments, stalled
production lifecycles, **stale-review candidates** (active conclusions that are old
AND rarely surfaced in `find` AND low inbound-link degree — surfaced for review
only, never decayed or down-ranked; hubs/snapshots excluded as expected-to-drift),
unregistered project keys, and embedding drift.

Per-check detail — exactly what each flags, its severity, and the proposed fix —
is in **`audit-checks.md`**.

## What this skill does NOT do

- Touch anything outside `Knowledge Base/`.
- Auto-compile *blindly* after every capture. Compilation is a deliberate step
  taken at a stepping-stone; it's always reported, never a silent dump of raw
  transcripts or every passing remark. "No silent dump" targets *noise* —
  transcripts, mid-flight tangents — not *signal*: it never licenses pruning
  context or detail from a note (see *Comprehensive coverage, minimal expression*
  under Proactive engagement).
- Assign numeric confidence scores. Use citation count and recency as the trust
  signal.
- Apply retention decay or "forgetting curves." Old material stays. If superseded,
  mark it; if irrelevant, archive into an `_archive/` subfolder. (`read_review/triage_memory`'s
  `stale_review` check **surfaces** old/cold/low-inbound conclusions as *review
  candidates* for you to judge — but never auto-decays, down-ranks, hides, or moves
  anything; review surfacing has no effect on `search_memory`/`find` ordering.
  Surfacing a candidate ≠ a forgetting curve. Authored typed relations DO
  legitimately inform retrieval ranking — that is connectivity you wrote, not
  decay the system invented.)
- Run on a schedule, or read or write your vault in the background. Every operation
  happens inside a turn — because you asked, or because the conversation reached a
  point where consulting or capturing is clearly warranted. (On clients that support
  them, optional hooks may *remind* the assistant to check at the start or end of a
  turn. A reminder is not an operation: it prompts the same in-turn judgment
  described under **Proactive engagement**, and nothing touches the vault until the
  assistant decides to act.)
- Modify `Sources/` or `Evidence/` files after creation. Mistakes get superseded,
  not edited.

## Maintenance modes (`check_memory/repair_memory`)

Remote `repair_memory` publishes structured-file, tag-variant, curation and
maintenance-job writes. `check_memory` publishes the admitted read-only
maintenance variants, including curation work items, previews, status and
maintenance jobs. A remote client runs the whole curation workflow: work item,
proposal, preview, one exact-plan approval, apply, resume and compensation. Follow the
active schema; a descriptive mode name never grants remote admission.


Audit is read-only. `fix` and `backfill-ids` rewrite content (wikilinks, frontmatter, stable IDs) and dry-run by default. `reconcile` heals index-count and sidecar drift from out-of-band edits, is idempotent and non-destructive, and writes by default (`dry_run=true` previews). The local CLI runs these writes in one call.

### Remote maintenance jobs

A remote `fix`, `backfill-ids` or `reconcile` runs as a background job, preview and write alike, not as one call. Only the vault owner can start, read or cancel one, and one job runs per vault.

1. Preview with `check_memory`, `dry_run=true`. The owner gets a `run_id` and `preview: true` at once: a preview of a large vault outlasts one request, so it runs as a read-only job. When it is `completed`, its receipt's `result` carries the `plan_id`. A caller other than the owner, and the local CLI, get the report in one call.
2. Start with `repair_memory`: the same mode and flags, `dry_run=false`, that `plan_id` and a one-line `why`. It returns a `run_id` at once. Without both it returns `MAINTENANCE_PLAN_REQUIRED` and starts nothing.
3. Follow with `check_memory` and `operation.kind="maintenance-job"` plus the `run_id`, until the state is `completed`, `failed`, `cancelled` or `interrupted`. Without a `run_id` it lists recent jobs.
4. Stop one early with `repair_memory` and `operation.kind="maintenance-job-cancel"` (`run_id`, `why`). The job stops at its next checkpoint.
5. Read the receipt. A finished job's status holds the totals: pages `committed`, `skipped_changed` (the page changed after the preview) and `outcome_unknown`. The page list is long, so read it page by page: pass the receipt's `next_cursor` as `cursor` to the same call, and repeat with each returned `next_cursor` until it is `null`. Each entry names a path, its kind and, for `backfill-ids`, the minted ID. A preview's entries are kind `planned`: the pages its `plan_id` covers.

If the vault changed after the preview, the job returns `STALE_MAINTENANCE_PLAN`, writes nothing and gives a fresh `plan_id`: preview again. Another start while a job runs returns `MAINTENANCE_JOB_ACTIVE` with the running `run_id`. `fix` and `backfill-ids` write in short chunks that let other writes through. `reconcile` holds writes for its whole run, and a `MUTATION_BUSY` refusal during it names the job's `run_id`. Applying `collections-store-export` is host work: run it with the service stopped.

### structured-files

Previews one Planning or Records collection's manifest-declared human filenames and managed readable bodies, including governed inbound-link rewrites. Preview is read-only; apply needs the exact `plan_id` and `source_snapshot` and `why`, and commits atomically. Durable identity and mutable state stay in frontmatter, not filenames.

### curation

The governed multi-step mode. The agent authors a closed typed plan from explicit context. Exomem validates and fingerprints it, records one exact-plan approval (`expected_plan_fingerprint`), and executes at most one content step per apply or resume. `work-item`, `preview` and `status` are read-only. Proposal, execution and separately reviewed compensation use the shared mutation terminal.

Page steps cannot target raw Sources or Evidence, Planning, Records, workflow contracts, schema/admin state, or trash internals. Typed `append-record` and `update-record` steps support Records collections that use `markdown-items`. Store collections require their serving service; if it is unavailable, a prepared run remains resumable. Planning steps remain unsupported.

`hydration_recheck` is a same-identity continuation ordinal (1 to 8). The eighth is closure-only and cannot bind another plan.

### tag-variants

Lists tags that differ only by case, separator or plural, grouped with visible page counts and the most-used written form as canonical (a tie is listed, never rewritten). Preview is read-only; `apply=true` with the preview's `plan_id` and a one-line `why` rewrites one bounded batch of minority variants to the canonical tag and logs a rollback record. Only the `tags` key changes; Sources, Evidence, Records, Planning and other owned trees are untouched. Preview again for the next batch. `exclude_groups` keeps named groups out of preview and apply alike.

### collections-store

Reports whether each file Records and Planning collection would move into the collection store cleanly. It runs only with `dry_run=true`, is owner-only, and writes nothing. Each collection reports its rows, legacy audit status, round-trip proof and any blocker. A blocker names its code and, for a failed proof, the check letter. Datasets are skipped, and every copy of a duplicated collection identity is blocked.

The report separates vault setup in `coordinator` and, when needed, a vault-level `blocker`. Without configured writer-lease coordination, a vault with collections to migrate stays `blocked` even when every collection passes. The vault verdict is `migratable`, `blocked` or `nothing_to_migrate`. Moving a vault is a separate offline upgrade step.

### collections-store-export

Previews the return of a migrated vault's store collections to file-canonical files. It is owner-only and writes nothing. For each collection it reports whether the store wrote it, and any blocker: a collection the store created, one over a file-mode bound, a value with no file form, or a view changed by hand. The vault verdict is `exportable`, `blocked`, `export_pending` or `nothing_to_export`. Applying the preview's `plan_id` with a `why` is the owner's offline step, `exomem collections export --plan-id ID --why REASON`, with the service stopped; the service refuses the apply.

### Sidecar collapse (`fix`)

`fix` also collapses media sidecars that accumulated nested copies of themselves (audit category `duplicated_sidecar`, reportable alone via `mode="audit", categories=["duplicated_sidecar"]`). It keeps the longest surviving `## Extracted text` (for a sidecar whose top-level block was blanked by a re-render, the one buried in a nested copy) and refuses any rewrite that would leave less transcript than it found. Frontmatter is untouched, so a still-`pending` sidecar is re-extracted normally and the recovered text is only the fallback.
