---
name: knowledge-base
description: Exomem-powered skill for operating on your personal Obsidian Knowledge Base — raw sources, compiled research notes, insights, failures, patterns, experiments, production-logs, typed entities, and Evidence artifacts. Triggers when you want to save, file, log, compile, distill, search, audit, supersede, or preserve anything in your KB, vault, Obsidian, or notes — including oblique phrasings ("interesting, save it," "I want to remember this"). Also engages proactively — it consults the KB for prior conclusions when a turn touches a topic it likely covers, and captures durable conclusions when the conversation reaches a stepping-stone (a decision, a solved problem, a diagnosed failure, a recognized pattern). Do NOT write outside the Knowledge Base folder; any sibling folders in the vault are read-only inputs.
version: 0.31.0
---

# Knowledge Base

This skill is the Exomem Knowledge Base contract. It keeps the stable
`knowledge-base` skill identity while teaching agents how to use Exomem's MCP
tools over your vault.

The compiled, structured layer of your Obsidian vault. Everything in
`Knowledge Base/` is either a raw source (immutable), compiled material under
explicit governance, or a preserved factual artifact (Evidence). Any other
folders in the vault are hand-authored and are **read-only input** to this
skill.

The Knowledge Base is a separate layer for compounding LLM-assisted research,
insights, failure modes, experiments, productions, entity knowledge, and
architectural documentation — kept structurally distinct so its epistemic status
is always clear.

## Core principle

**Sources are immutable. Compiled material is governed. Evidence is preserved.**

- `Sources/` — raw inputs. Append-only. Never edited after capture.
- `Notes/`, `Entities/` — compiled, structured, supersedable. Always carry frontmatter, sources, and links.
- `Evidence/` — raw factual artifacts (binaries, documents, screenshots). Append-only. No analysis at this layer.
- Anything outside `Knowledge Base/` — Claude reads, Claude does not write.

## Proactive engagement

This skill is **context-aware, not just request-driven.** It engages on its own
in two situations and stays quiet otherwise. ("Proactive" means Claude's own
judgment mid-conversation — there are no hooks, schedules, or background
triggers.)

**Proactive retrieval (read) — quiet, surface only hits.** When a turn
references something the KB plausibly holds — a project, a domain, a named
entity, or phrasings like "what did I conclude about X," "have I looked at Y,"
"where did we land on Z" — run a quiet `find` **first** and fold what you find
into the answer. Don't narrate the search; mention the KB only when it returned
something relevant, and cite the page(s) you used. A miss means "not found in
what I searched," never "it doesn't exist" — an empty find means *no coverage
yet*, which is a reason to consider capturing, not to disengage.

**Stepping-stone capture (write) — then report.** When the conversation reaches
a **stepping-stone** — a decision is made, a problem is solved, a failure is
diagnosed, a pattern is recognized — capture it:

- Capture whether or not the KB already holds the topic. A durable conclusion on
  brand-new ground is first-class: it becomes the first page on that topic, which
  is how the corpus grows.
- Raw material → **add**. A durable conclusion → draft the compiled
  **note**/**link**, run **suggest_links** and the near-duplicate check first,
  then write and report one line: `Saved → <path>`.
- The guardrails that remain are the ones that matter: dedupe (prefer
  **edit**/**replace** over a parallel page; surface a near-duplicate warning when
  it fires) and clean links.
- Pause and ask only when type or scope is genuinely ambiguous (research vs.
  insight vs. experiment; which `Notes/Research/<scope>`).

Not a stepping-stone: mid-thought exploration, brainstorm tangents, unresolved
questions. Capture at the landing, not during the flight.

**Comprehensive coverage, minimal expression.** Capturing at the landing is about
*timing*, not *volume* — it never means keep less. Minimality is a property of
*expression* — distillation, signal-density, no redundancy — never of *coverage*.
Don't drop context or detail because it "doesn't seem important": importance is
usually only legible in hindsight, and nothing here forces the tradeoff (no
retention decay, hybrid BM25+vector retrieval, append-only `Sources/`, no storage
limit). Default coverage to comprehensive; reserve concision for *how* a note is
written, not *what* it keeps. Torn between keeping a detail and dropping it? Keep
it — an over-kept detail is free to retrieval, a dropped one is unrecoverable.
Capture more, at the right layer: raw verbatim to `Sources/` liberally; compiled
notes stay distilled in form but never context-pruned.

## Vault layout

```
<vault>/Knowledge Base/
├── index.md                      Top-level catalog; updated on every write
├── log.md                        Append-only activity log; most recent first
├── _Schema/
│   ├── SKILL.md                  This file (canonical)
│   ├── project-keys.yaml         Registered research scope keys
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
│   ├── Videos/                   Video transcripts/notes
│   └── Other/                    Miscellaneous captures
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

`<vault>` resolves to your Obsidian vault root — the folder that contains
`Knowledge Base/`, set via `EXOMEM_VAULT_PATH`. Verify allowed filesystem paths
before writing.

The research scopes are an open set you grow over time, registered in
`_Schema/project-keys.yaml` (see § Research scope keys). New users start with a
small set (e.g. `personal`, `project-alpha`, `work`) and add their own.

## Loading the tools

The KB tools may be **deferred** — the client lists them by name and you load a
tool's schema before you can call it. Load the core set up front, in one shot:
you'll almost always need `find` (search), `get` (read a page), and one or more
of `note`, `add`, `link`, `suggest_links`, `edit`, `audit`. In Claude Code, load
them by exact name in a single call:

`ToolSearch("select:find,get,note,add,link,suggest_links,edit,audit")`

On clients without a `select:` syntax (e.g. claude.ai), search by capability —
"search the knowledge base", "read a KB page", "compile a note" — and each
resolves to the right tool. `find` is the read-only hybrid (semantic + keyword)
search and your default entry point.

The Tier 2 filesystem ops below may be turned off on lean deployments
(`EXOMEM_DISABLE_TIER2`), in which case only the Tier 1 ops are registered.

## Operations

Operations split into two tiers. **Tier 1 is primary** — every typed-note
workflow goes through it because the type-routing IS the discipline. **Tier 2 is
the escape hatch** for cases that don't fit a Tier 1 shape. If a write fits Tier
1, use it. Operations are dispatched by intent — you phrase the request; the
skill matches one of these.

### Tier 1 — type-routed (primary)

These encode the KB's discipline: filenames, folders, frontmatter, supersession,
and index updates are determined by the operation, not the caller.

| Op | Intent | Writes to |
|---|---|---|
| **add** | Capture raw input as immutable source | `Sources/<type>/` |
| **note** | Compile a structured note from raw input or thinking | `Notes/<type>/` |
| **link** | Create or update an entity, wire backlinks | `Entities/<type>/` |
| **preserve** | Capture a **text** factual artifact for an incident scope. Binaries (PDF / image / any file) go out-of-band via upload (see below), not this tool | `Evidence/<scope>/` |
| **edit** | In-place edit of a compiled page. One mode per call: whole `body` / `tags` / surgical `old_string`→`new_string`; `edits=[…]` several surgical pairs in one atomic batch; `row_key`+`take` fill a `[take: ]` row by its leading text; `field`+`value` patch ONE frontmatter field (requires `why:`). Bumps `updated:`. Optional `expected_hash` (drift guard) + `validate_only` | the page |
| **find** | Type-aware search across the KB (read-only) | — |
| **suggest_links** | Surface existing pages a draft or page should link to, hub-aware (read-only) | — |
| **get** | Read a full file by path; `frontmatter_only=true` returns just the frontmatter. Returns `content_hash` + `mtime` for the two-writer drift guard (echo `content_hash` to `edit` via `expected_hash`). Read-only | — |
| **audit** | Lint pass: orphans, broken links, supersession integrity, aged unprocessed sources | proposals only |
| **overview** | Bounded structure report of the vault or a subtree — folder tree, counts, frontmatter coverage, junk candidates. Works outside the KB and pre-init (read-only) | — |
| **propose_compilation** | Draft a note scaffold from unprocessed source(s) — the backlog-drain companion to audit (read-only) | proposals only |
| **replace** | Supersession: mark old, write new with header pointer | both old + new |
| **reconcile** | Heal drift from out-of-band edits (Obsidian/mobile/manual): recompute index counts + re-embed stale files + report remaining drift. Idempotent; `dry_run` reports only | drifted indexes + embedding sidecar |
| **provenance_report** | Scan note bodies for `<!-- key:value -->` provenance tags (filter by key/value/path). Read-only | — |

For the full per-operation spec — inputs, validation, write rules, edge cases —
see `references/operations.md`.

### Tier 2 — filesystem-parity (escape hatches)

These exist for things Tier 1 can't express: building new folder structures,
files that aren't typed notes, and edits the Tier 1 set can't express (simple
appends, renames). Do NOT use Tier 2 when Tier 1 fits.

| Op | Intent | Writes to |
|---|---|---|
| **create_file** | Write a file at any vault path (optional frontmatter dict). `kind="dir"` instead makes a folder | arbitrary path |
| **list_directory** | List files+subfolders at a path (recursive optional). Read-only | — |
| **move_file** | Rename/relocate a file; rewrites inbound wikilinks by default. Boundary-crossing moves out of/into `Sources/`/`Evidence/` refused | both old + new |
| **delete** | Trash a file OR folder (moves to `_trash/`, recoverable). Requires `confirm=true`; folders need `recursive=true` if non-empty; refuses on inbound links unless `force_orphan` | path → `_trash/` |
| **list_trash** | Enumerate recoverable trash entries. Read-only | — |
| **recover_from_trash** | Undo a delete: move from `_trash/` back to original (or custom) location | `_trash/` → restored path |
| **append_to_file** | Append text to an existing file | the file |
| **list_inbound_links** | Find all files whose wikilinks resolve to a target. Read-only | — |
| **query_data** | Structured query over a CSV/JSON **data file** under the vault — filter/sort/paginate, project columns, or aggregate. The retrieval half of the data-search pattern (`find` → dataset card → `query_data`). Read-only | — |

### Discipline preserved across BOTH tiers

These constraints apply equally to Tier 1 and Tier 2 — no escape hatch around them:

- **Sources/ and Evidence/ are append-only.** `create_file`, `delete`,
  `append_to_file` (for Sources), and `edit`'s frontmatter-patch mode refuse on
  these trees. Use `add` and `preserve` (the only content writers). A move that
  stays *within* the same append-only tree (themed sub-foldering) is allowed;
  boundary-crossing moves are refused.
- **Binaries go out-of-band — never inline through a tool argument.** Transcribe
  what's relevant into the note/evidence *text* (that's the queryable part), and
  deliver the *original file* separately. On claude.ai web, call
  **`mint_upload_token`** for a short-lived `{token, upload_url}`, then have the
  code sandbox multipart-`curl` the attached files to `upload_url`.
  **Searchable binaries are automatic:** the server transcribes audio/video
  (Whisper), OCRs images (Tesseract), reads PDFs (pymupdf), extracts office/web
  documents (docx/xlsx/pptx/html via MarkItDown; txt/eml/ics via native parsers),
  and CLIP-embeds images and per-keyframe video frames for visual search — all
  server-side after upload, filling an embedded sidecar so any upload becomes
  findable by content. You *may* still pass a `text` field to supply your own
  extraction; it takes precedence. The write tools take text only and reject
  inline byte blobs (`BINARY_BLOB_REJECTED`). Full workflow:
  `references/operations.md` § preserve.
- **Pull a vault file back out — the download channel.** Call
  **`mint_download_token`** for a short-lived `{token, download_url}`, then GET
  `download_url?path=<vault-relative path>` with `Authorization: Bearer <token>`.
  Read-only, download-scoped, path confined to the vault root.
- **Media hits in `find` are first-class.** An extracted media sidecar carries
  `media_type` and `media_file` (a pointer to the original binary). Treat the
  *file* as the result and the matched transcript/OCR snippet as the "why"; offer
  to pull the original via `mint_download_token`. Images and video are also
  searchable by *visual content* (CLIP), not just text — a purely-visual hit
  carries a `clip_score`; a video visual hit also carries `clip_match_at` (e.g.
  `"14:32"`), the timestamp of the matching keyframe.
- **Read-only subtrees are write-protected.** Any vault subtree marked read-only
  (see `references/write-scope.md`) refuses Tier 2 writes by default; reads are
  unrestricted.
- **Every write logs to `Knowledge Base/log.md`** with the operation, path, and a
  one-line rationale. Where appropriate, ops require a `why:` (e.g. `edit`'s
  frontmatter-patch mode).
- **Deletes are never permanent at the MCP layer.** `delete` moves targets to
  `Knowledge Base/_trash/YYYY-MM-DD/…` with a `.meta.json` sidecar; recovery is
  `recover_from_trash`. Permanent removal happens manually. The `_trash/` subtree
  is excluded from `find` and `audit`.
- **Supersession over deletion** for compiled material — prefer `replace`.
  `delete` refuses on pages with `superseded_by:` set unless `force_superseded=true`.
- **Wikilink integrity.** `move_file` defaults to updating inbound links;
  `delete` refuses on files with inbound links unless `force_orphan=true`. The KB
  is a graph; ops that fragment it are explicit.

### Phrasing → operation mapping (heuristic, not exhaustive)

- "save this," "log this," "capture this," "add to my KB" → **add**
- "compile this into a note," "make a note on this," "write this up," "distill this" → **note** (typically preceded by an implicit **add**)
- "log this experiment," "I'm running a 30-day X protocol" → **note** with type=experiment
- "log this batch," "add this episode," "record this launch" → **note** with type=production-log
- "this is connected to [[X]]," "create an entity for X" → **link**
- "preserve this letter," "file this in evidence," "save this for the record" → **preserve**
- "update the skill," "the KB structure needs to change" → no MCP tool — hand-edit `_Schema/` files directly
- "fill in the take for X," "set the take on that row" → **edit** (`row_key`+`take`)
- "make these few edits to the page" (same page) → **edit** (`edits=[…]`)
- "what do I have on X," "find my notes on Y," "have I covered Z" → **find**
- "why was this note changed," "show the history of this page" → **get** (`include_history=true`)
- "what did I used to think about X," "show me the superseded version" → **find** (`prefer_active=false`)
- "what should this link to," "densify this page's links" → **suggest_links**
- "what should I compile next," "drain the source backlog" → **propose_compilation**
- "audit the KB," "lint the vault," "check for orphans" → **audit**
- "what does this vault look like," "assess my vault," "how is this vault organized" → **overview**
- "I edited the vault directly / on my phone — sync it up," "heal the drift" → **reconcile**
- "this replaces the old strategy," "supersede the old note on X" → **replace**
- "make a new folder for X" → **create_file** (`kind="dir"`, Tier 2)
- "rename this page," "move this note to Patterns/" → **move_file** (Tier 2)
- "what's in folder X," "list the files under Y" → **list_directory** (Tier 2)
- "query my data," "filter the CSV," "rows where Y > Z," "sum/avg of a column" → **query_data** (Tier 2)
- "flip the status to archived" (single-field tweak) → **edit** (`field`+`value`)
- "tack this onto the end of X" → **append_to_file** (Tier 2)
- "delete this file / folder" → **delete** (Tier 2; trash semantics — recoverable)
- "what's in the trash," "undelete," "put it back" → **list_trash** / **recover_from_trash** (Tier 2)

**Implicit (no explicit ask) — proactive engagement:**
- topic maps to a project/domain/entity, or "what did I conclude about X" → proactive **find** first, fold the hits into the answer
- a decision is made or a problem just got solved → stepping-stone: capture via **add**/**note**, then report the path

When you say something oblique like "interesting, save it," default to **add** +
ask whether to compile a note.

## Search

`find` runs in **hybrid mode** by default: BM25 + local vector embeddings
(BAAI/bge-base-en-v1.5, 768-dim) fused via reciprocal rank fusion.
Natural-language queries reach pages that don't contain the literal terms.

Modes:

- `mode="hybrid"` (default) — BM25 + vector + graph + keyword fused via RRF. A
  strict superset of keyword: hybrid never returns fewer results than keyword for
  the same query. Falls back to BM25-only if the embedding sidecar is missing.
- `mode="keyword"` — strict case-insensitive substring matching, sorted by
  `updated:`. Use for precision-only lookups (exact phrase, entity name, code
  identifier) where you'd rather get zero results than fuzzy ones.
- `mode="vector"` — vector-only. Diagnostic aid.

Empty queries degrade to filtered-most-recent regardless of mode.

**Scope — the vault is bigger than the KB:**
- `scope="kb"` (default) searches `Knowledge Base/` first and **auto-widens to
  the whole vault** when the KB doesn't fill `limit`. Content in sibling folders
  is reachable, not silently invisible. Widened hits carry `outside_kb: true`.
- `scope="vault"` always walks the whole vault. `scope="kb-only"` is the strict
  opt-out (KB only, never widens).
- **Never report a search-miss as absence.** An empty result means *"not found in
  what I searched,"* not *"it doesn't exist."* If you're sure something exists,
  try `scope="vault"`, vary the query terms, or `get` a path you suspect.

Additional knobs on `find`: `graph=true` (1-hop neighbours of strong matches),
`rerank=true` (CrossEncoder re-sort, opt-in), `prefer_compiled=true` (default;
favours compiled types over raw `source`), `prefer_active=true` (default;
soft-demotes superseded pages), `file_types` / `exclude_file_types` (scope to
or drop artifact kinds: `note`, `pdf`, `image`, `audio`, `video`, `docx`, `xlsx`,
`pptx`, `html`, `text`, `email`, `calendar`, `csv`, `json`, `tsv`), and `speakers`
(restrict to diarized media whose `speakers:` frontmatter names a given person).

**Tabular data is card-based.** Raw CSV/JSON/TSV rows are never embedded and raw
data files aren't `find`-searchable. To make a dataset findable, write a
**dataset card** — a small `type: dataset` page (frontmatter `data_file:` +
`format:`, a one-line "What this holds", and a column profile) via `create_file`.
`query_data(aggregate="profile")` emits a ready-to-write card; pull exact rows
from the `data_file` with `query_data`.

Vector embeddings live in a per-machine sidecar at
`<vault>/Knowledge Base/.embeddings.sqlite` (a dotfile Obsidian Sync ignores).
Writers refresh it incrementally after every atomic batch. To bootstrap or after
drift, call `audit_fix(rebuild_embeddings=true)`.

### Assessing a vault you didn't build

For structural questions — "what does this vault look like," "how is this vault
organized," "is there junk in here" — run **overview** first: one bounded,
read-only report of folder structure, counts, frontmatter coverage, naming
patterns, and junk candidates (zero-byte files, sync-conflict duplicates). It
works on any folder under the vault root, including trees outside
`Knowledge Base/` (a `Daily/` or `Journal/` folder), and on vaults with no KB at
all. Drill down from there: `list_directory` on folders of interest,
`find scope="vault"` for content, targeted `get` for individual files. **Never
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

## Descriptive vs analytical coverage

The KB serves two complementary purposes:

- **Descriptive coverage** — *describe what is.* Architecture hubs
  (`Notes/Research/<project>/<subsystem>-architecture`), point-in-time snapshots,
  concept entities. These let a future planner orient quickly.
- **Analytical coverage** — *extract reusable lessons.* Patterns, insights,
  failure modes, decisions. These compound across projects.

Both are first-class. When orienting a new area, descriptive hubs typically come
first; patterns and insights extract from the descriptive layer as second-order
knowledge.

**Boundary with a code repo.** For a software project the repository is the
source of truth for code, design, and decisions. KB coverage of it is the
cross-session/cross-project layer the repo can't hold — strategy, roadmap,
orientation, hard-won empirical findings — never a condensed changelog or a
restatement of what specs/commits already capture.

## Write discipline

These rules are non-negotiable.

1. **Read-only paths.** Never write to anything outside `Knowledge Base/`. Any
   sibling folders in the vault are inputs only. Compiled notes may **link to**
   them but never modify them.

2. **Sources and Evidence are append-only.** Once a file lands in `Sources/` or
   `Evidence/`, never edit its *content*. Corrections happen by adding a new
   source and superseding the old via a compiled note. Relocating a file *within*
   the same append-only tree (into a themed sub-folder) is allowed via
   `move_file`; crossing the boundary is forbidden.

3. **Propose before writing compiled material.** For `note`, `link`, and
   `replace` (and any hand-edit of `_Schema/` files), show the proposed content
   (or diff) and wait for confirmation. The exception is `add` (raw capture),
   `preserve` (raw evidence), and `find`/`audit` (read-only).

    **Batch waiver:** you may approve a *scope* of multiple files upfront ("draft
    all Tier 1," "write all four hubs + concepts") rather than each individually.
    Write the batch, then summarise paths + count. The waiver is per-batch.

    **Standing waiver:** phrasing like "just write it," recorded preferences, or a
    stepping-stone reached in an autonomous session — draft, write, and report
    rather than pre-approve.

4. **Frontmatter is mandatory.** Every file written under `Knowledge Base/` must
   carry frontmatter conforming to `references/frontmatter.md`. Exceptions:
   `index.md`, `log.md`, and sub-folder `index.md` files. Non-markdown binaries
   carry frontmatter in a sidecar `.md` if one is needed.

5. **No `confidence` floats.** Trust is conveyed through citations and link
   counts, not numbers.

6. **Supersession over deletion.** When information is replaced, mark the old page
   `superseded`, link to the new one, and never delete. See
   `references/supersession.md`.

7. **Always update `index.md` and `log.md`.** Every write that creates or moves a
   page updates the top-level `index.md` (counts + Recent activity, cap-50),
   appends to `log.md`, refreshes the relevant sub-folder `index.md` counts, and
   appends the new artifact's wikilink to the originating source's `ingested_into:`
   frontmatter. Count tokens are auto-refreshed by the writer; hand-curated
   descriptions are preserved.

For the full read-only / writeable path map see `references/write-scope.md`.

## Page types

Eight page types under `Knowledge Base/`, each with a required frontmatter shape,
naming rule, and location. **Full per-type spec: `references/page-types.md`;
frontmatter: `references/frontmatter.md`.** The behaviorally-load-bearing
distinctions:

- **source** — raw input, `Sources/<type>/`. Two flavors (same frontmatter):
  *transcript* (content as-is) and *origination record* (a session-reasoning
  capture, `ingested_into:` listing what it produced).
- **research-note** — `Notes/Research/<scope>/`. Informal subtypes: *standard*;
  *hub* (orients a subsystem, links out; refresh on major ships); *snapshot*
  (point-in-time, drift OK, say "snapshot" in body).
- **insight** — cross-cutting lesson, `Notes/Insights/`.
- **failure** — failure mode, `Notes/Failures/`.
- **pattern** — reusable pattern, `Notes/Patterns/`. Use `projects:` (plural) when
  it spans projects.
- **experiment** — hypothesis + protocol + primary data, `Notes/Experiments/<domain>/`.
- **production-log** — creative artifact + production knowledge, `Notes/Productions/<medium>/`.
- **entity** — typed node, `Entities/<entity-type>/` (People / Concepts / Libraries / Decisions).

### Research scope keys

The `project` field on a research note is a slug-shaped key registered in
`_Schema/project-keys.yaml`. It's an **open set**, not a closed enum — pick the
most-specific scope first. A typical starter set:

- Products / projects: `project-alpha`, `project-beta` — one key per project.
- Domains: `work`, plus your own (`research`, `ops`, …).
- Cross-cutting: `personal` — anything not tied to a specific project or domain.

For **patterns** that apply across multiple projects, use `projects:` (plural
list) instead of `project:` (singular), e.g. `projects: [project-alpha, project-beta]`.

**Auto-registration of new project keys.** The `note`, `replace`, `edit`
(frontmatter-patch), and `link` (decision-entity) writers auto-append unknown
slug-shaped project keys to `_Schema/project-keys.yaml` and create the matching
`Notes/Research/<Folder>/` directory on first use — no manual YAML edit needed.
Pass `project_category` to bucket the new key (product / activity / domain /
cross-cutting); omitted, it lands `uncategorized`. A **typo guard** rejects new
keys within edit distance ≤2 of an existing key (`wrok` → "Did you mean
'work'?") so the registry stays clean.

### Experiment vs production-log

Easy to confuse (both time-bounded, date-prefixed, with outcomes).
**Experiment** = a hypothesis tested under a protocol with primary data
(`Notes/Experiments/`); ends in confirm/refute/qualify. **Production-log** = a
creative artifact + its production knowledge (`Notes/Productions/`); ends in
engagement metrics + reflection, and the value is the thing made. Quick test: set
out to *learn whether X is true* (experiment) or to *make a thing the world sees*
(production)?

## Workflow: typical add-then-compile session

1. **You paste raw material or ask to log something.**
2. **Skill creates a `source` file.** Picks the subfolder from the input shape —
   `Sources/Articles/`, `Sources/Sessions/`, `Sources/Books/`, `Sources/Papers/`,
   `Sources/Videos/`, or `Sources/Other/`. Filename: ISO-date + slug. Updates
   `Sources/index.md`.
3. **Skill asks: "Compile a note from this? If yes, what type — research,
   insight, failure, pattern, experiment, production-log? And what scope?"** Skip
   if you already specified.
4. **Skill drafts the compiled page** with frontmatter, a sources block linking
   back to the source file, and a Connections section. **Run `suggest_links` on
   the draft first** — it surfaces related existing pages you'd otherwise miss.
5. **Skill shows the draft, waits for confirmation.** You can revise inline.
6. **On confirm: writes the page**, updates the relevant `index.md`, appends to
   `log.md`, and reports paths. The `note` result carries a `suggestions` block
   and any near-duplicate `warning` — wire in the relevant links via **edit** (or,
   for a genuine duplicate, prefer `replace`/`append` over a parallel page).

When you approve a scope of multiple files upfront, the workflow collapses to a
single batch write (see Write discipline § 3, batch waiver).

## Linking discipline

Every compiled page should link out. Linking is what turns the KB from a junk
drawer into a graph.

**Canonical wikilink form: full vault-rooted.** Every wikilink resolves cleanly
under the vault root with no prefix guessing:
`[[Knowledge Base/Entities/Concepts/Profile]]`. Link back to the originating
`Sources/` file via the `sources:` frontmatter list (mirrors the source's
`ingested_into:` list).

**The writer normalizes on your behalf.** exomem's writers run every wikilink
through `vault.normalize_wikilink()` before writing — bare names, KB-relative
paths, `.md` suffixes, and stale paths get rewritten to canonical full form. You
can write in any form; the on-disk file lands canonical.

If a wikilink target doesn't exist yet, prefer creating the entity stub via the
**link** operation rather than leaving a dangling link. Dangling links accumulate
and surface in **audit** as `broken_wikilink`.

When creating an entity that points at a **currently-evolving external artifact**
(a live spec, a code library, a service config), use **pointer-style** — summary
+ canonical-source pointer + connective tissue — not **mirror-style** (versions,
file inventories, command lines copied verbatim). Mirroring guarantees drift.

## Audit (lint) checks

The **audit** operation runs read-only checks and proposes fixes (never
auto-fixes); the report is reviewed before anything is written. It covers:
orphans, broken wikilinks, supersession integrity, stale frontmatter,
`index.md`/`log.md` drift, aged unprocessed sources (oldest-first — pair with
`propose_compilation`), status/location mismatch, unfinished experiments, stalled
production lifecycles, **stale-review candidates** (active conclusions that are old
AND rarely surfaced in `find` AND low inbound-link degree — surfaced for review
only, never decayed or down-ranked; hubs/snapshots excluded as expected-to-drift),
unregistered project keys, and embedding drift.

Per-check detail — exactly what each flags, its severity, and the proposed fix —
is in **`references/audit-checks.md`**.

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
  mark it; if irrelevant, archive into an `_archive/` subfolder. (`audit`'s
  `stale_review` check **surfaces** old/cold/low-inbound conclusions as *review
  candidates* for you to judge — but never auto-decays, down-ranks, hides, or moves
  anything; `find` ordering is unchanged. Surfacing a candidate ≠ a forgetting curve.)
- Run on hooks, schedules, or background triggers. Operations happen because you
  asked, or because the conversation reached a point where consulting or capturing
  is clearly warranted.
- Modify `Sources/` or `Evidence/` files after creation. Mistakes get superseded,
  not edited.

## When to ask vs. when to proceed

**Ask before:**
- Writing any compiled note, entity, experiment, production-log, supersession, or
  schema update.
- Choosing a page type when intent is ambiguous (research vs. insight vs.
  experiment vs. production-log).
- Choosing a scope under `Notes/Research/` when you haven't named one.
- Choosing a domain under `Notes/Experiments/` or medium under
  `Notes/Productions/` when not stated.
- Choosing whether a research-note is *standard*, *hub*, or *snapshot* — when the
  framing materially affects scope.
- Marking an existing page `superseded`.

**Proceed without asking:**
- Proactive `find` for context (read-only).
- Capturing a clear stepping-stone conclusion whose type and scope are
  unambiguous — write under the standing waiver and report the path.
- `add` and `preserve` operations — raw capture.
- `find` and `audit` — read-only.
- Updating `index.md`, `log.md`, and `ingested_into:` frontmatter after a
  confirmed write.
- Resolving obvious wikilink targets when the entity exists exactly.
- Continuing a previously-approved batch.

## References (read on demand)

- `references/page-types.md` — full page-type taxonomy with naming conventions
- `references/frontmatter.md` — frontmatter spec per page type
- `references/write-scope.md` — full read-only / writeable path map
- `references/supersession.md` — supersession protocol
- `references/operations.md` — detailed per-operation specs
- `references/audit-checks.md` — per-check detail for the audit operation

Read each on first use. The SKILL.md you're reading now is the contract; the
references are the manual.
