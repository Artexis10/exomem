# Operations

Detailed specs for each of the seven operations. Read on first use of an operation.

## Index and log discipline (applies to every write)

Every confirmed write that creates, moves, or supersedes a page performs two bookkeeping updates:

1. **`index.md` updates** — the top-level catalog and the affected subfolder catalog. Catalog format only: header, page list, one-line descriptions. No prose orientation. If a write touches multiple page types, update each relevant subfolder index.
2. **`log.md` append** — one entry appended to the top-level `log.md`. Entry format:

   ```
   ## [YYYY-MM-DD] <op> | <title>
   <one-line description in present tense>
   ```

   `<op>` is one of: `add`, `note`, `link`, `preserve`, `replace`, `audit`. (`find` doesn't append because it's read-only.) The entry is parseable with `grep '^## \[' log.md | tail -20`.

These two updates are non-negotiable for every operation below. The per-operation specs note their primary writes; the index + log update is implicit.

---

## add

**Goal:** Capture raw input as an immutable source.

### Triggers
- "save this," "log this," "capture this"
- "add this to my KB / vault / notes"
- "I want to remember this" (oblique — confirm before proceeding if context is thin)

### Inputs to gather
- The raw content (pasted text, URL, file reference, conversation excerpt)
- Source type — usually inferable: pasted Claude transcript → `Sessions/`; URL or article body → `Articles/`; book excerpt → `Books/`; academic paper → `Papers/`; a video transcript (e.g. a pasted YouTube transcript) → `Videos/` with `url` set. Ask only if ambiguous.
- Optional: tags, why-captured one-liner

### Procedure
1. Determine source type and target subfolder.
2. Generate filename: `YYYY-MM-DD-<slug>.md` where slug is dash-separated lowercase, ≤ 60 chars.
3. Write file with full frontmatter per `frontmatter.md` § source. `ingested_into: []`.
4. Body: `# Source: <Title>` → `> brief description` → `## Capture` (raw content) → `## Why captured` (one or two sentences).
5. Update `Sources/index.md` with a new line: `- [[Sources/<type>/<filename>]] — <one-line description>`.
6. Update top-level `index.md` only if it tracks source counts or recent sources (default: it does).
7. Report path written and offer: "Compile a note from this?"

### Edge cases
- **Duplicate URL.** If a source with the same URL already exists, surface it and ask whether to capture again (e.g., the article changed) or link to the existing one.
- **Very long content.** > ~50KB raw text: capture an excerpt (first ~5KB + "..." + last ~2KB), put full URL in frontmatter, note in body that it's an excerpt. Or ask Hugo if he wants the full thing.
- **Sensitive content.** If the source contains anything that looks like credentials, API keys, or PII: refuse capture, surface the issue, ask Hugo to clean and re-paste.

### Writes performed
- One new file in `Sources/<type>/`
- One updated `Sources/index.md`
- Top-level `index.md` updated if it tracks recent activity

---

## note

**Goal:** Compile a structured note from raw input or accumulated thinking. Routes to one of six compiled-page types: `research-note`, `insight`, `failure`, `pattern`, `experiment`, `production-log`.

### Triggers
- "compile this into a note," "make a note on this," "write this up"
- "distill this," "summarize this for the KB"
- "log this experiment," "I'm starting a 30-day X protocol"
- "log this reel batch," "add this episode," "record this PDF launch"
- Often follows immediately after an `add` (Hugo says "save this and write it up")

### Inputs to gather
- Source(s) to compile from — usually one or more recently-`add`ed sources, or in-conversation thinking, or (for experiments / production-logs) Hugo's own protocol or production description
- Note type — `research-note`, `insight`, `failure`, `pattern`, `experiment`, `production-log`. Ask if ambiguous. Key distinctions:
  - **Research vs experiment:** synthesizing secondary sources (research) vs running a protocol with primary data (experiment).
  - **Research vs production-log:** secondary synthesis (research) vs documenting the making of a primary creative artifact (production-log).
  - **Experiment vs production-log:** hypothesis → finding (experiment) vs artifact → engagement metrics (production-log).
- For research notes: scope (`substrate`, `q`, `endstate`, `sift`, `tu`, `book-club`, `health`, `finance`, `creative`, `science`, `travel`, `personal`). Ask if not stated.
- For Q research notes specifically: confirm whether the note is about Q itself or about a Q tenant. If a tenant, gather the tenant key (`example-tenant`, `tu`, or new). Note that `tu` is dual-purpose: `project: tu` for podcast content/activity research, `project: q + tenant: tu` for TU-as-Q-deployment research.
- For experiments: domain (`food`, or another sub-domain). Plus hypothesis, protocol summary, duration, started date.
- For production-logs: medium (`reels`, `episodes`, `pdfs`, `posts`, or another). Plus projects, host, editor (if known), recording / publish status. Productions are often associated with a research-note (the curriculum / outline) and a pattern (the production rules) — surface those links during the draft.
- Topic / title (slug) — propose one based on content; confirm

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
3. **Draft the page in conversation** — show full content including frontmatter, all sections per the page-type template (`page-types.md`), wikilinks to existing entities/concepts/upstream curated material where they obviously match.
4. **Wait for Hugo to confirm or revise.** Default is propose-then-write.
5. On confirm:
   - Write the file.
   - For each source cited (if any), update that source's frontmatter `ingested_into` field to include a wikilink to this new note.
   - Update the relevant subfolder `index.md` (and project-or-domain-or-medium-specific index for research notes / experiments / production-logs).
   - Top-level `index.md` updated if it tracks recent activity.
6. Report paths written and any wikilinks that target nonexistent pages (offer to create stubs via `link`).

### Edge cases
- **No clean source.** If Hugo wants to capture in-conversation thinking that wasn't first `add`-ed: it's fine to compile directly, but create a `Sources/Sessions/` capture of the conversation excerpt as a side-effect. Sources/citation integrity matters.
- **Spans multiple projects (research-note).** If a research note touches multiple projects, that's a sign it might be an insight or pattern instead. Surface the option. Substrate-level research often masquerades as multi-project research — check whether it's actually company-level.
- **Topic already covered.** Use `find` first; if a similar note exists, ask whether to extend it (in-place edit) or supersede it.
- **New scope not in the project list.** Project keys are an open set — they auto-register on first use. Just pass the new slug-shaped key (e.g. `project: home`) to `note`/`replace`/`edit`/`link`; the writer appends it to `_Schema/project-keys.yaml` and creates the matching `Notes/Research/<Folder>/`. A typo guard rejects near-misses (within edit distance 2 of an existing key), so an existing-but-misspelled scope still maps back to the canonical one. Pass `project_category` to bucket the new key (umbrella / product / activity / domain / situation / cross-cutting); omitted, it lands `uncategorized` for Hugo to fix later. The registration surfaces as a warning in the write response — mention it to Hugo, but don't treat a genuinely new scope as illegal or as needing a manual schema edit.
- **New Q tenant.** If Hugo references a Q tenant not in the current list (`example-tenant`, `tu`), surface it before assuming. Don't silently add tenants.
- **New experiment domain or production medium.** If Hugo names a domain/medium that isn't yet a subfolder, propose creating it; don't auto-create. Initial seeded set: `Experiments/Food/`, `Productions/Reels/`.
- **Experiment ongoing / production mid-lifecycle.** When an experiment is being logged at start (vs written up after conclusion), or a production is logged at planning / recording (vs published / reflected), the later sections will be sparse and that's expected. The skill should not insist on filling them.
- **Production-log status updates.** Production-logs evolve through the lifecycle. When Hugo says things like "the reel batch is published now" or "Kim finished editing," update status + relevant date fields, but don't rewrite the body unless asked. The body is mostly stable from the recording stage onward; only Outcomes and Reflection grow.

### Writes performed
- One new file in `Notes/<type>/...`
- Updated `ingested_into` on each cited source
- Updated subfolder `index.md`
- Top-level `index.md` updated if it tracks recent activity

---

## link

**Goal:** Create or update a typed entity, wire backlinks.

### Triggers
- "create an entity for X," "add a concept page for Y"
- "this references [[X]]" where X doesn't exist yet (offered as a side-effect of `note`)
- "add Karpathy to People," "add pgvector to Libraries"

### Inputs to gather
- Entity name (becomes filename in Title Case)
- Entity type — `person`, `concept`, `library`, `decision`. Usually clear from context.
- For new entities: a one-paragraph summary; relevant frontmatter fields (see `page-types.md` § entity)
- For updates: the field or section to change

### Procedure
1. Determine target path: `Entities/<People|Concepts|Libraries|Decisions>/<Name>.md`.
2. **For new entities:** draft the page following `page-types.md` § entity, propose to Hugo, write on confirm.
3. **For updates:** show diff, write on confirm.
4. Update `Entities/<type>/index.md` and top-level `index.md`.

### Edge cases
- **Name collision / disambiguation.** Two people named the same; two concepts with overlapping names. Disambiguate in the filename: `John Smith (Q advisor).md`, `Agentic RAG (architecture).md`. Don't silently merge.
- **Person → also a public figure.** Add `relationship: public-figure` and keep summary factual.
- **Decision entity.** These are essentially lightweight ADRs. Ensure `decided` date and `decision_status` are set.

### Writes performed
- One new or updated file in `Entities/<type>/`
- Updated subfolder and top-level `index.md`

---

## preserve

**Goal:** Drop a binary artifact into the evidence layer for legal/factual preservation.

### Triggers
- "preserve this evidence," "file this artifact," "this is for the legal trail"
- "keep this for the lawyer," "add this to the evidence"
- Receiving a file (`.eml`, `.pdf`, `.png`, `.csv`) that needs to survive employment termination, account revocation, or contract dispute

### Inputs to gather
- The binary artifact (path on local filesystem, or upload reference)
- Scope (e.g., `Yolo`, future-employer, contract name) — the top-level subfolder under `Evidence/`
- Category — a numbered subfolder under the scope (e.g., `01 - Warning Letter 2026-05-15`, `02 - Promotion Track Evidence`). Use existing categories where they fit; create new ones only when none apply.
- Optional: descriptive filename if the original is generic (e.g., rename `Screenshot 2026-05-15 at 23.14.png` to `2026-05-15-slack-jovany-king-of-ai-statement.png`)

### Delivering the bytes — out-of-band (never inline through the model)

Binaries are delivered out-of-band — never inline as a tool argument (the `preserve`
tool takes text only). Pick the channel by where the file actually is:

- **On claude.ai web — hands-off (preferred):** the model can't *emit* the bytes, but
  it has two channels — the authenticated MCP connector and a code sandbox holding the
  user's **attached** files. So: (1) call the **`mint_upload_token`** tool → a
  short-lived `{token, ttl_seconds, upload_url}`; (2) in the sandbox, multipart-`curl`
  each attached file to `upload_url` with `Authorization: Bearer <token>` and form
  fields `file` / `scope` / `category` (optional `filename`, `description`, **`text`**);
  (3) **searchability is automatic** — the server transcribes audio/video (Whisper),
  OCRs images (Tesseract), and reads PDFs on the GPU after the upload and fills an
  embedded sidecar, so the binary becomes findable by its content with no extra step.
  You *may* still pass a `text` field to supply your own extraction (a richer vision
  description, or a textless-photo caption the server can't produce) — it wins and
  skips the server pass. No inline bytes, no pasted
  secret. **Two requirements:** files must be **attached** (inline-pasted images never
  land on the sandbox disk and can't be sent), and the host must be in the sandbox's
  egress allowlist (Settings → network; `*.substratesystems.io`, one-time). If the
  sandbox still can't reach the host, fall back to handing Hugo the prefilled link
  `https://kb.substratesystems.io/upload?scope=<scope>&category=<category>` to upload
  from his browser.
- **Phone / curl / a shortcut:** `POST https://kb.substratesystems.io/upload`
  multipart (`file`, `scope`, `category`, optional `filename`, `description`, `text`) with
  `Authorization: Bearer $KB_MCP_UPLOAD_TOKEN` (the token is **always** required;
  Cloudflare Access may sit in front as an extra network gate but the server does
  not trust its headers). Lands straight in `Evidence/<scope>/<category>/`, zero
  token cost. The public link is capped near **100 MB** by the Cloudflare edge;
  larger originals go desk-side (below) rather than over the public URL.
- **Claude Code / desk-side:** the file is already on local disk — write it straight
  into `Evidence/<scope>/<category>/`, or drop it via Obsidian Sync; the note links it.
- **`preserve`** is text-only; binaries always go via the channels above. Every write
  tool rejects inline byte blobs outright (`BINARY_BLOB_REJECTED`).

### Procedure
1. Determine scope and category folder. Create the folder if it doesn't exist yet. Confirm new scope/category with Hugo first — don't silently invent.
2. Generate filename if renaming: ISO date prefix where temporal anchoring matters + descriptive slug. Preserve the file's extension as-is.
3. Drop the binary into `Evidence/<scope>/<category>/<filename>`. No frontmatter is added (binaries don't carry it).
4. Update `Evidence/<scope>/index.md` if it tracks per-category file lists. Otherwise leave it.
5. Surface to Hugo: any compiled note in `Sources/` or `Entities/` that should now reference this artifact (typically the analytical capture of the same event). Offer to add a cross-reference line.

### Edge cases
- **Sensitive content.** If the file contains anything that looks like credentials, API keys, or third-party PII unrelated to the evidence purpose: surface it before writing. Hugo may want to redact first. Note: Hugo's own PII in his own evidence (his name, his email, his bank account on his own payslip) is fine — it's his data, this is his vault.
- **Duplicate filename.** If a file with the same name already exists, surface it. The append-only rule means we don't overwrite; either rename the new file with a `-v2` suffix or confirm it's the same file already preserved.
- **Wrong scope/category.** If Hugo names a scope/category that doesn't match the existing structure, surface the existing options and ask before creating new ones.
- **Drive-only intent.** If Hugo says "put it in Drive" rather than "preserve it," he wants the convenience copy, not vault preservation. Don't auto-preserve to vault unless asked — confirm.

### Writes performed
- One new file in `Evidence/<scope>/<category>/`
- Optionally a sidecar `<filename>.md` (when `description` and/or `text` is supplied) — embedded on write, so the binary's extracted text is immediately findable
- Optionally updated `Evidence/<scope>/index.md`
- Optionally updated cross-reference line in a relevant `Sources/` or `Entities/` note (with confirmation)

---

## download

**Goal:** Pull a stored vault file *out* into the code sandbox to work on it — the reverse of the upload channel. Read-only; the bytes stream out-of-band, never back through the model.

### Triggers
- "open / analyze / re-read that file in the sandbox"
- needing the raw bytes of a dataset, an evidence scan, or any stored artifact to process locally

### Procedure
1. Call **`mint_download_token`** → `{token, ttl_seconds, download_url}` (download-scoped, short-lived; the long-lived secret never leaves the server).
2. In the sandbox, `GET {download_url}?path=<vault-relative path>` with header `Authorization: Bearer <token>`. The `path` is vault-relative (e.g. `Knowledge Base/Evidence/Yolo/01 - Check-in/scan.pdf`).
3. The server resolves the path under the vault root (traversal-safe), and streams the file. Confined to the vault; an out-of-vault or missing path is refused (400 / 404).

### Notes
- The token is **download-scoped** — it can read but not write (an upload token won't work here, and vice-versa).
- Whole-vault read, like `get` — datasets and evidence live in sibling folders, all reachable by path.

### Writes performed
- None — read-only.

---

## find

**Goal:** Type-aware search across the Knowledge Base. Read-only.

### Triggers
- "what do I have on X," "find my notes on Y"
- "have I covered Z," "show me everything tagged W"
- "list all my failure modes on Q"
- "what experiments have I run on food"
- "what reel batches have I shipped"

### Procedure
1. Parse the query for type filters (research, insight, failure, pattern, experiment, production-log, entity), project/domain/medium/tenant filters (q, endstate, food, reels, example-tenant, etc.), tag filters, or date ranges.
2. Search across:
   - Filenames
   - Frontmatter fields (especially `tags`, `project`, `projects`, `tenant`, `domain`, `medium`, `entity_type`)
   - Body text (best-effort; don't load entire vault if large)
3. Rank by:
   - Exact tag/project/tenant/domain/medium match first
   - Title match second
   - Body match third
   - Recency as tiebreaker
4. Return a list with: title (wikilink), type, scope (project/tenant/domain/medium), `updated` date, one-line excerpt.

### Output format
- Default: a markdown list of 5–15 hits, most relevant first.
- For broad queries: ask Hugo to narrow before dumping a long list.
- For very specific queries with no hits: report explicitly, suggest related tags or projects to try.

### Edge cases
- **No filesystem MCP available.** This skill cannot run on mobile claude.ai. Surface this and stop — don't fake search.
- **Very large vault.** If body search is slow, search frontmatter and titles first; offer body search as a follow-up.

### Writes performed
None.

---

## audit

**Goal:** Surface drift and propose fixes. Read-mostly.

### Triggers
- "audit the KB," "lint the vault," "check for orphans"
- "clean up my notes," "what's broken"
- Periodic Hugo-initiated runs (e.g., monthly)

### Checks performed
1. **Orphans.** Compiled pages with zero inbound links and zero outbound links beyond their `sources` block.
2. **Broken wikilinks.** `[[X]]` where `X` doesn't resolve to any file in the vault.
3. **Supersession integrity:**
   - Pages with `status: superseded` must have a `superseded_by` field.
   - The target of `superseded_by` must exist and have `supersedes` pointing back.
   - Pages with `status: active` must not appear as the target of any `superseded_by`.
4. **Stale frontmatter.** Required fields missing for the page type. Includes: research-notes with `tenant` set but `project` not equal to `q` (the `tenant` field is Q-only); production-logs with status outside the lifecycle enum.
5. **`index.md` drift.** Files in folders that are not catalogued; catalogue entries pointing to missing files.
6. **Unprocessed sources.** Files in `Sources/` with `ingested_into: []` older than 30 days. Configurable threshold.
7. **Status / location mismatch.** Pages with `status: archived` not living in an `_archive/` subfolder, and vice versa.
8. **Unfinished experiments.** Experiments with `status: active` whose `started` date plus `duration` has passed. Propose: write up results, mark concluded, or extend duration.
9. **Stalled production-logs.** Production-logs with `status: recorded` or earlier whose `published` field has been null for >60 days. Propose: update status, fill outcomes, mark `dropped`, or archive.
10. **Tombstone cleanup.** Files whose body indicates "moved" / "this stub is safe to delete" and whose target is reachable. Propose: delete (this is one of the rare cases where deletion is offered, since the migration is documented elsewhere).

### Output format
A markdown report grouped by check, with:
- Number of issues per check
- Per-issue line: file path, what's wrong, proposed fix
- Summary at top: total issues, severity breakdown

### Procedure
1. Run all checks. (For an initial KB with few pages, this is fast. For larger KBs, may need to scope by folder.)
2. Generate the report.
3. Show the report to Hugo. **Do not auto-fix anything.**
4. Offer: "Apply all proposed fixes?" / "Apply by check?" / "Apply per-issue?"
5. On per-issue or per-check confirmation, apply that fix, write any modified files, update `index.md` if needed.

### Edge cases
- **Audit on a fresh KB.** With near-zero content, audit will mostly come back clean. That's expected.
- **Many issues at once.** If the report is long, group by severity and show the top 20; offer to dump full report to a temp file.
- **Hugo declines all fixes.** Fine. Audit is a surfacing tool, not an enforcement tool.

### Writes performed
- None on audit alone.
- On per-fix confirmation: writes per the specific fix (e.g., resolve a broken link by editing the source page; update `index.md` to remove a phantom entry).

---

## replace

**Goal:** Supersession — author a new version, mark the old one superseded.

### Triggers
- "this supersedes the old note on X"
- "replace the old version of Y"
- "rewrite this from scratch — make a v2"

### Procedure
See `supersession.md`. Summary:
1. Confirm the old page's path with Hugo.
2. Author the new page (filename with `-v2` or descriptive variant).
3. Set new page's `supersedes` to old page's wikilink.
4. Update old page: `status: superseded`, `superseded_by: <new>`, `updated: today`.
5. Insert supersession banner at top of old page's body.
6. Update both index.md entries (new page added; old page annotated `(superseded)`).
7. Cascade-flag downstream pages that cite the old page; surface them, do not auto-update.

### Edge cases
- **Old page is itself a supersession of an even older page.** Chain of `supersedes`/`superseded_by` should remain coherent. The new page supersedes the most recent active version, not the chain root.
- **Old page is referenced by archived pages.** Update the archived references' frontmatter `superseded_by` field if applicable, but don't unarchive.

### Writes performed
- One new file
- One updated old file (frontmatter + banner)
- Updated subfolder and top-level `index.md`
- Updated `ingested_into` fields on cited sources for the new page

## query_data

Structured query over a CSV/JSON **data file** under the vault — the retrieval half of the data-search pattern. `find` surfaces a dataset's markdown card; `query_data` reads the raw file the card's `dataset_files:` points at and returns exact rows or an aggregate. Read-only; no writes, no index (reads on demand — KB datasets are small). Raw CSV/JSON are not `find`-searchable; this is how you query their values.

### Triggers
- "what was my X over time," "filter the CSV," "rows where Y > Z," "sum/avg/latest/distinct of a column," "how many entries in <dataset>."
- A value the dataset card's summary tables don't pre-answer, or a whole-dataset / ad-hoc query.

### Inputs to gather
- `path` — vault-relative `.csv`/`.tsv`/`.json` (usually a card's `dataset_files:` entry).
- For nested JSON: `record_path` (dotted, e.g. `sections.work_incapacity`) — omit for a top-level array or the common keys result/results/data/rows/items/entries.
- The query: `filters` (`[{column, op, value}]`; op ∈ eq/ne/gt/gte/lt/lte/contains/icontains/startswith/in/nin/exists/missing), `columns` (projection; dotted ok), `sort_by`+`descending`, `limit`/`offset`, OR `aggregate` (`count` | `min|max|sum|avg|latest|distinct:column`), OR `date_from`/`date_to`(/`date_column`).

### Procedure
1. Resolve + read the file (path-escape-guarded; 25 MB cap; CSV/TSV by header, JSON array or via `record_path`).
2. Apply filters (+ any date range). Numeric compares coerce tolerantly (comma decimals; lab `<`/`>` operators stripped for the comparison).
3. If `aggregate`: compute over matched rows and return it. Else: sort → paginate → project columns.

### Output format
`{path, format, total_rows, total_matched, returned, columns, rows, aggregate, truncated, warnings}`.

### Edge cases
- Dotted columns reach nested JSON fields (`performer.name`, `id.extension`) in filters/columns/sort/aggregate. Deeply irregular JSON may need a one-time flatten-to-CSV first; flat tables are the sweet spot.
- `limit` hard-capped at 1000 (default 100); `truncated: true` signals more rows matched than returned.

### Writes performed
- None (read-only).
