# Operations

Detailed specs for the Knowledge Base operations. Read on first use of an operation.

## Plain-language routing

Agents should hear user intent first and choose the product command second.
Canonical tools remain the exact implementation leaves underneath product
commands.

| Simple action | User intent | Product route |
|---|---|---|
| `ask` | Ask what Exomem knows, find a prior conclusion, gather context | `ask_memory`, then `read_memory`; use `ask_memory(deep=true)` for synthesis |
| `remember` | Remember a durable conclusion, decision, solved problem, or pattern | `remember`; use `replace_memory` if it supersedes old knowledge |
| `capture` | Preserve raw material, a source, proof, receipt, or record | `capture_source` for Sources; `preserve_evidence` or `transfer_artifact` for Evidence |
| `review` | Review stale, contradictory, or unprocessed knowledge | `review_memory` |
| `connect` | Suggest links, relations, or graph context | `connect_memory`; entity writes stay explicit |
| `adopt` | Assess or import an existing vault safely | `adopt_vault(mode="scan-only")` first; explicit modes for manifest/copy/compile planning |
| `maintain` | Check or repair vault health | `maintain_memory(mode="audit")`; explicit `fix`/`reconcile` modes only with fix intent |

Do not ask users to choose internal folders, graph sidecars, or page types unless
the distinction changes the write. Translate back to simple language when
reporting results.

## Index and log discipline (applies to every write)
Every confirmed write that creates, moves, or supersedes a page performs two
bookkeeping updates:

1. **`index.md` updates** — the top-level catalog and the affected subfolder
   catalog. Catalog format only: header, page list, one-line descriptions. No
   prose orientation.
2. **`log.md` append** — one entry appended to the top-level `log.md`:

   ```
   ## [YYYY-MM-DD] <op> | <title>
   <one-line description in present tense>
   ```

   `<op>` is one of: `add`, `note`, `link`, `preserve`, `replace`, `edit`,
   `audit`. (`find` doesn't append because it's read-only.)

These two updates are non-negotiable for every write operation below. Read-only
operations such as `bootstrap`, `find`, `get`, `suggest_links`, `graph_context`, `suggest_relations`, `overview`,
`propose_compilation`, `query_data`, `provenance_report`, and `download` do not
update indexes or logs. The per-operation specs note their primary writes; the
index + log update is implicit.

---

## bootstrap

**Goal:** Return a portable, versioned operating contract so generic MCP clients
can use Exomem without relying on a client-specific skill.

### Triggers
- A new MCP session in ChatGPT, Cursor, Codex, Gemini, Windsurf, or another
  client that only receives tool schemas.
- The agent needs to understand preferred workflows, defaults, retrieval modes,
  upload semantics, or performance diagnostics.
- A skill-aware client is diagnosing retrieval speed or comparing CPU/GPU/low
  power behaviour.

### Profiles
- `profile="compact"` — terse operation map and defaults. Use for normal startup.
- `profile="full"` — startup contract plus workflow guidance and examples.
- `profile="diagnostics"` — includes timing/performance guidance, search profile,
  compute mode, backend/cache hints, rerank state, and recommended debug knobs.

### Procedure
1. Call once at the beginning of a generic MCP-client session.
2. Treat the returned contract as behavioural guidance for tool choice: search
   before absence claims, use compiled notes for durable conclusions, preserve raw
   evidence separately, and prefer supersession over deletion.
3. Use diagnostics output when interpreting search latency. Do not label Exomem
   slow from a single CPU rerank measurement; identify whether rerank, embedding
   backend, compute mode, cache state, or cold start dominated the run.

### Writes performed
None.

---

## add

**Goal:** Capture raw input as an immutable source.

### Triggers
- "save this," "log this," "capture this"
- "add this to my KB / vault / notes"
- "I want to remember this" (oblique — confirm before proceeding if context is thin)

### Inputs to gather
- The raw content (pasted text, URL, file reference, conversation excerpt)
- Source type — usually inferable: pasted transcript → `Sessions/`; URL or article body → `Articles/`; book excerpt → `Books/`; academic paper → `Papers/`; a video transcript → `Videos/` with `url` set. Ask only if ambiguous.
- Optional: tags, why-captured one-liner

### Procedure
1. Determine source type and target subfolder.
2. Generate filename: `YYYY-MM-DD-<slug>.md` where slug is dash-separated lowercase, ≤ 60 chars.
3. Write file with full frontmatter per `frontmatter.md` § source. `ingested_into: []`.
4. Body: `# Source: <Title>` → `> brief description` → `## Capture` (raw content) → `## Why captured` (one or two sentences).
5. Update `Sources/index.md` with a new line.
6. Report path written and offer: "Compile a note from this?"

### Edge cases
- **Duplicate URL.** If a source with the same URL already exists, surface it and ask whether to capture again or link to the existing one.
- **Very long content.** > ~50KB raw text: capture an excerpt (first ~5KB + "..." + last ~2KB), put full URL in frontmatter, note in body that it's an excerpt.
- **Sensitive content.** If the source contains anything that looks like credentials, API keys, or unrelated PII: refuse capture, surface the issue, ask for a cleaned re-paste.

### Writes performed
- One new file in `Sources/<type>/`
- One updated `Sources/index.md`

---

## note

**Goal:** Compile a structured note from raw input or accumulated thinking. Routes
to one of six compiled-page types: `research-note`, `insight`, `failure`,
`pattern`, `experiment`, `production-log`.

### Triggers
- "compile this into a note," "make a note on this," "write this up," "distill this"
- "log this experiment," "I'm starting a 30-day X protocol"
- "log this batch," "add this episode," "record this launch"
- Often follows immediately after an `add`.

### Inputs to gather
- Source(s) to compile from — recently-`add`ed sources, in-conversation thinking, or (for experiments / production-logs) your own protocol or production description.
- Note type. Ask if ambiguous. Key distinctions:
  - **Research vs experiment:** synthesizing secondary sources (research) vs running a protocol with primary data (experiment).
  - **Research vs production-log:** secondary synthesis (research) vs documenting the making of a primary creative artifact (production-log).
  - **Experiment vs production-log:** hypothesis → finding (experiment) vs artifact → engagement metrics (production-log).
- For research notes: scope (a registered project key — see SKILL.md § Research scope keys). Ask if not stated.
- For experiments: domain. Plus hypothesis, protocol summary, duration, started date.
- For production-logs: medium. Plus projects, host, editor (if known), recording / publish status.
- Topic / title (slug) — propose one based on content; confirm.

### Procedure
1. Determine note type and target folder:
   - `research-note` → `Notes/Research/<scope>/`
   - `insight` → `Notes/Insights/`
   - `failure` → `Notes/Failures/`
   - `pattern` → `Notes/Patterns/`
   - `experiment` → `Notes/Experiments/<domain>/`
   - `production-log` → `Notes/Productions/<medium>/`
2. Generate filename:
   - Research / insight / failure / pattern: `<topic-slug>.md` (no date prefix).
   - Experiment / production-log: `YYYY-MM-<slug>.md` (start month prefix).
3. **Draft the page in conversation** — show full content including frontmatter, all sections per the page-type template, wikilinks to existing entities/concepts where they obviously match. **Run `suggest_links` on the draft first.**
4. **Wait for confirmation.** Default is propose-then-write.
5. On confirm:
   - Write the file.
   - For each source cited, update that source's `ingested_into` field to include a wikilink to this new note.
   - Update the relevant subfolder `index.md`.
6. Report paths written and any wikilinks that target nonexistent pages (offer to create stubs via `link`).

### Edge cases
- **No clean source.** If you want to capture in-conversation thinking that wasn't first `add`-ed, it's fine to compile directly, but create a `Sources/Sessions/` capture of the conversation excerpt as a side-effect. Citation integrity matters.
- **Spans multiple projects (research-note).** If a research note touches multiple projects, that's a sign it might be an insight or pattern instead. Surface the option.
- **Topic already covered.** Use `find` first; if a similar note exists, ask whether to extend it (in-place edit) or supersede it.
- **New scope not in the project list.** Project keys are an open set — they auto-register on first use. Just pass the new slug-shaped key; the writer appends it to `_Schema/project-keys.yaml` and creates the matching `Notes/Research/<Folder>/`. A typo guard rejects near-misses. Pass `project_category` to bucket the new key.
- **New experiment domain or production medium.** If you name a domain/medium that isn't yet a subfolder, propose creating it; don't auto-create.
- **Experiment ongoing / production mid-lifecycle.** When logged at start (vs written up after conclusion), later sections will be sparse and that's expected. Don't insist on filling them.

### Writes performed
- One new file in `Notes/<type>/...`
- Updated `ingested_into` on each cited source
- Updated subfolder `index.md`

---

## link

**Goal:** Create or update a typed entity, wire backlinks.

### Triggers
- "create an entity for X," "add a concept page for Y"
- "this references [[X]]" where X doesn't exist yet (offered as a side-effect of `note`)
- "add Ada Lovelace to People," "add pgvector to Libraries"

### Inputs to gather
- Entity name (becomes filename — see `page-types.md` § entity naming)
- Entity type — `person`, `concept`, `library`, `decision`.
- For new entities: a one-paragraph summary; relevant frontmatter fields.
- For updates: the field or section to change.

### Procedure
1. Determine target path: `Entities/<People|Concepts|Libraries|Decisions>/<Name>.md`.
2. **For new entities:** draft the page following `page-types.md` § entity, propose, write on confirm.
3. **For updates:** show diff, write on confirm.
4. Update `Entities/<type>/index.md` and top-level `index.md`.

### Edge cases
- **Name collision / disambiguation.** Disambiguate in the filename: `John Smith (advisor).md`, `Agentic RAG (architecture).md`. Don't silently merge.
- **Person → also a public figure.** Add `relationship: public-figure` and keep the summary factual.
- **Decision entity.** These are essentially lightweight ADRs. Ensure `decided` date and `decision_status` are set.

### Writes performed
- One new or updated file in `Entities/<type>/`
- Updated subfolder and top-level `index.md`

---

## preserve

**Goal:** Capture a factual artifact in the evidence layer for long-term
preservation.

### Triggers
- "preserve this evidence," "file this artifact," "keep this for the record"
- Receiving a file (`.eml`, `.pdf`, `.png`, `.csv`) that needs to survive an account change, a contract dispute, or any situation where the as-received original matters.

### Inputs to gather
- The artifact (text to inline, or a binary delivered out-of-band — see below)
- Scope — the top-level subfolder under `Evidence/` (e.g., a contract name, an incident name)
- Category — a subfolder under the scope (e.g., `01 - Initial Letter 2026-05-15`). Use existing categories where they fit.
- Optional: a descriptive filename if the original is generic.

### Delivering the bytes — out-of-band (never inline through the model)

Binaries are delivered out-of-band — never inline as a tool argument (the
`preserve_evidence` command takes text only). Pick the channel by where the file actually is:

- **On claude.ai web — hands-off (preferred):** (1) call **`transfer_artifact(mode="upload")`** →
  a short-lived `{token, ttl_seconds, upload_url}`; (2) in the code sandbox,
  multipart-`curl` each attached file to `upload_url` with `Authorization: Bearer
  <token>` and form fields `file` / `scope` / `category` (optional `filename`,
  `description`, **`text`**); (3) **searchability is automatic** — the server
  transcribes audio/video (Whisper), OCRs images (Tesseract), reads PDFs
  (pymupdf), extracts office/web documents (docx/xlsx/pptx/html via MarkItDown;
  txt/eml/ics via native parsers), and CLIP-embeds images and per-keyframe video
  frames for visual search. It fills an embedded sidecar so the binary becomes
  findable by content. You *may* still pass a `text` field to supply your own
  extraction; it wins and skips the server pass. Upload responses return concrete
  metadata (`stored_path`, `size`, `hash`, `hash_algorithm`, `media_id`,
  `content_type`) so agents can report exactly what landed. No inline bytes, no
  pasted secret. Files must be
  **attached** (inline-pasted images never land on the sandbox disk), and the host
  must be in the sandbox's egress allowlist (Settings → network; one-time). If the
  sandbox can't reach the host, fall back to handing the user the prefilled link
  `https://<your-host>/upload?scope=<scope>&category=<category>`.
- **Phone / curl / a shortcut:** `POST https://<your-host>/upload` multipart
  (`file`, `scope`, `category`, optional `filename`, `description`, `text`) with
  `Authorization: Bearer $EXOMEM_UPLOAD_TOKEN` (the token is **always** required).
  Lands straight in `Evidence/<scope>/<category>/`, zero token cost.
- **Claude Code / desk-side:** the file is already on local disk — write it
  straight into `Evidence/<scope>/<category>/`, or drop it via file sync (e.g. Obsidian Sync).
- **`preserve`** is text-only; binaries always go via the channels above. Every
  write tool rejects inline byte blobs outright (`BINARY_BLOB_REJECTED`).

### Procedure
1. Determine scope and category folder. Create the folder if it doesn't exist yet. Confirm a new scope/category first — don't silently invent.
2. Generate a filename if renaming: ISO date prefix where temporal anchoring matters + descriptive slug. Preserve the file's extension as-is.
3. Drop the binary into `Evidence/<scope>/<category>/<filename>`. No frontmatter is added (binaries don't carry it).
4. Update `Evidence/<scope>/index.md` if it tracks per-category file lists.
5. Surface any compiled note that should now reference this artifact. Offer to add a cross-reference line.

### Edge cases
- **Sensitive content.** If the file contains credentials, API keys, or third-party PII unrelated to the evidence purpose: surface it before writing. Your own PII in your own evidence is fine — it's your data, your vault.
- **Duplicate filename.** Surface it. Append-only means no overwrite; either rename with a `-v2` suffix or confirm it's the same file already preserved.
- **Wrong scope/category.** If a named scope/category doesn't match the existing structure, surface the existing options and ask before creating new ones.

### Writes performed
- One new file in `Evidence/<scope>/<category>/`
- Optionally a sidecar `<filename>.md` (when `description` and/or `text` is supplied) — embedded on write
- Optionally updated `Evidence/<scope>/index.md`
- Optionally a cross-reference line in a relevant `Sources/` or `Entities/` note (with confirmation)

---

## download

**Goal:** Pull a stored vault file *out* into the code sandbox to work on it — the
reverse of the upload channel. Read-only; the bytes stream out-of-band, never back
through the model.

### Triggers
- "open / analyze / re-read that file in the sandbox"
- needing the raw bytes of a dataset, an evidence scan, or any stored artifact to process locally

### Procedure
1. Call **`transfer_artifact(mode="download")`** → `{token, ttl_seconds, download_url}` (download-scoped, short-lived).
2. In the sandbox, `GET {download_url}?path=<vault-relative path>` with header `Authorization: Bearer <token>`.
3. The server resolves the path under the vault root (traversal-safe) and streams the file. An out-of-vault or missing path is refused.

### Notes
- The token is **download-scoped** — it can read but not write.
- Whole-vault read, like `read_memory` — datasets and evidence live in sibling folders, all reachable by path.

### Writes performed
- None — read-only.

---

## find

**Goal:** Type-aware search across the Knowledge Base. Read-only. See SKILL.md §
Search for modes, scope, and ranking knobs.

### Triggers
- "what do I have on X," "find my notes on Y"
- "have I covered Z," "show me everything tagged W"
- "list all my failure modes on <project>"

### Diagnostics and performance
- Normal lookup: `detail="compact"`, `rerank=false`.
- Reasoning context: `pack=true` when you need a compact evidence bundle for
  downstream reasoning. Add `graph_enrich=true` only when the caller explicitly
  wants typed graph neighborhoods included beside the normal pack output.
- Diagnostics: `include_timings=true`; add `rerank=true` only when you are
  intentionally measuring reranking or spending latency for precision. Leaving
  `rerank` unset is mode-aware auto: CPU steady-state modes keep it off;
  accelerated/performance mode may auto-rerank when lanes strongly disagree or
  the query is long. Timing output should be interpreted with the returned
  compute mode, embedding backend, cache state, rerank flag, and search profile
  when present.
- If one search misses, try synonyms and adjacent domain terms before concluding
  the KB lacks coverage. Example: `wood`, `woods`, `smoke`, `smoking`, `kamado`,
  `grill`, `apple`, `oak`, `hickory`.

### Edge cases
- **No filesystem MCP available.** This skill cannot run without a connected KB server. Surface this and stop — don't fake search.
- **Very large vault.** Hybrid search is indexed; the first query after a cold start may pay a one-time build cost.
- **Result carries a `warming` key.** The server just started and is still loading its
  semantic models in the background; the hits are keyword/BM25-only ranking. Usable as-is
  for exact-term lookups; if semantic recall matters for the query, retry once `warming`
  stops appearing (typically well under a minute).

### Writes performed
None.

---

## graph_context

**Goal:** Return a bounded, read-only typed graph neighborhood from the derived
`.graph.sqlite` sidecar. This is the explicit knowledge-graph surface for
semantic blocks, files, and deterministic relations such as `derived_from`,
`evidenced_by`, `supports`, `contradicts`, `supersedes`, and `links_to`.

### Triggers
- "what does this connect to in the graph"
- "show typed relations for this page"
- needing graph context around a search result without changing the page

### Procedure
1. Provide either `path` or `query`. Use `path` when you already know the page;
   use `query` to seed the neighborhood from matching node labels/content.
2. Keep `depth` small unless the caller asks for a wider neighborhood. Use
   `relation_types` / `node_types` filters when a narrower answer is better.
3. Treat `available=false` as a soft fallback, not a failure. Existing search and
   pack behavior remains valid when the sidecar is missing, disabled, stale, or
   schema-incompatible.

### Writes performed
None. The graph sidecar is derived state only; this operation never mutates
Markdown.

---

## suggest_relations

**Goal:** Propose candidate typed graph relations for a page or draft. Suggestions
are review-only and never write to the vault.

### Triggers
- "suggest relations for this note"
- "what should this support, contradict, or supersede"
- densifying a draft before calling `note` or `edit`

### Procedure
1. Pass a `path` for an existing page, or `draft_title` / `draft_body` for an
   unwritten draft.
2. Review deterministic candidates from wikilinks, frontmatter sources, shared
   sources/entities, supersession, and optional embedding proximity.
3. Model-backed suggestions are default-off (`include_model_suggestions=false`),
   response-only, and soft-fail with warnings when optional extras are absent.
4. Persist accepted relations through the normal Markdown write path (`note` or
   `edit`); do not treat suggestions as an automatic write.

### Writes performed
None.

---
## audit

**Goal:** Surface drift and propose fixes. Read-mostly. See
`references/audit-checks.md` for the full per-check detail.

### Triggers
- "audit the KB," "lint the vault," "check for orphans"
- "clean up my notes," "what's broken"

### Procedure
1. Run all checks.
2. Generate the report (grouped by check; per-issue: file path, what's wrong, proposed fix; summary at top).
3. Show the report. **Do not auto-fix anything.**
4. Offer: "Apply all proposed fixes?" / "Apply by check?" / "Apply per-issue?"
5. On per-issue or per-check confirmation, apply that fix and write any modified files.

### Writes performed
- None on audit alone.
- On per-fix confirmation: writes per the specific fix.

---

## replace

**Goal:** Supersession — author a new version, mark the old one superseded.

### Triggers
- "this supersedes the old note on X"
- "replace the old version of Y"
- "rewrite this from scratch — make a v2"

### Procedure
See `supersession.md`. Summary:
1. Confirm the old page's path.
2. Author the new page (filename with `-v2` or descriptive variant).
3. Set new page's `supersedes` to old page's wikilink.
4. Update old page: `status: superseded`, `superseded_by: <new>`, `updated: today`.
5. Insert a supersession banner at the top of the old page's body.
6. Update both `index.md` entries.
7. Cascade-flag downstream pages that cite the old page; surface them, do not auto-update.

### Writes performed
- One new file
- One updated old file (frontmatter + banner)
- Updated subfolder and top-level `index.md`
- Updated `ingested_into` fields on cited sources for the new page

---

## query_data

Structured query over a CSV/JSON **data file** under the vault — the retrieval
half of the data-search pattern. `ask_memory` surfaces a dataset's markdown card;
`query_dataset` reads the raw file the card's `data_file:` points at and returns
exact rows or an aggregate. Read-only. Raw CSV/JSON are not `find`-searchable;
this is how you query their values.

### Triggers
- "what was my X over time," "filter the CSV," "rows where Y > Z," "sum/avg/latest/distinct of a column," "how many entries in <dataset>."

### Inputs to gather
- `path` — vault-relative `.csv`/`.tsv`/`.json` (usually a card's `data_file:` entry).
- For nested JSON: `record_path` (dotted) — omit for a top-level array or the common keys result/results/data/rows/items/entries.
- The query: `filters` (`[{column, op, value}]`; op ∈ eq/ne/gt/gte/lt/lte/contains/icontains/startswith/in/nin/exists/missing), `columns` (projection; dotted ok), `sort_by`+`descending`, `limit`/`offset`, OR `aggregate` (`count` | `min|max|sum|avg|latest|distinct:column`), OR `date_from`/`date_to`(/`date_column`).

### Procedure
1. Resolve + read the file (path-escape-guarded; 25 MB cap; CSV/TSV by header, JSON array or via `record_path`).
2. Apply filters (+ any date range). Numeric compares coerce tolerantly.
3. If `aggregate`: compute over matched rows and return it. Else: sort → paginate → project columns.

### Output format
`{path, format, total_rows, total_matched, returned, columns, rows, aggregate, truncated, warnings}`.

### Edge cases
- Dotted columns reach nested JSON fields in filters/columns/sort/aggregate. Deeply irregular JSON may need a one-time flatten-to-CSV first; flat tables are the sweet spot.
- `limit` hard-capped at 1000 (default 100); `truncated: true` signals more rows matched than returned.

### Writes performed
- None (read-only).
