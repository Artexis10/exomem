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
| `connect` | Suggest links or return graph, evidence, provenance, and history context | `connect_memory`; use `operation="context"` for the unified read-only view |
| `adopt` | Assess or import an existing vault safely | `adopt_vault(mode="scan-only")` first; explicit modes for manifest/copy/compile planning |
| `maintain` | Check or repair vault health | `maintain_memory(mode="audit")`; explicit `fix`/`reconcile` modes only with fix intent |
| `schema` | Govern recurring structure, relation vocabulary, or graph lenses | `schema_memory`; inference is proposal-only and saving is explicit |

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

## Stable identity and references

Every new governed page and evidence Markdown sidecar receives an immutable
`exomem_id` UUID. Writers return both its current vault-relative `path` and a
canonical `exomem://memory/<uuid>` reference. Commands that accept a governed
page identifier accept either form. In normal user-facing prose, show the note
title by default and do not expose the raw canonical ref by default. Add the
current vault-relative path for clarity or disambiguation; if the title is
missing or unusable, use the path or file name as the visible fallback.

Keep the canonical ref for tool arguments, durable machine state, and
machine-readable automation so identity survives moves and renames. Show the
raw ref only when the user explicitly asks for it or the identifier itself is
being inspected or debugged. Do not embed the canonical ref as a Markdown link
target when a client could expose its UUID; use a plain title-first citation.

Do not manually create, copy, or modify IDs. Legacy pages remain untouched until
`maintain_memory(mode="backfill-ids")` is requested. That mode is a dry run by
default and writes only with `dry_run=false`. Duplicate and malformed IDs are
reported by audit and make canonical resolution fail rather than selecting an
arbitrary page.

## Mutation terminal and edit compatibility

Product mutations default to a compact committed terminal containing `ok`,
`status`, `mutated`, `path` or `paths`, `request_id`, `receipt_id`, and
`warnings_count`; a supported caller-supplied idempotency key is echoed as
`idempotency_key`. `response_detail="full"` adds the existing leaf result under
`diagnostics`. `response_detail="legacy"` returns that old raw leaf result and
is retained for at least one compatibility release. The response-detail choice
is removed before mutation identity is calculated, so it cannot create a second
write or change which stored terminal a replay receives.

Busy, acknowledgement-pending, and committed-acknowledgement-uncertain outcomes
remain structured errors. Retrying must retain the same identity and payload:
wait before retrying `MUTATION_BUSY`; never revise a pending payload; and after
`MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN`, reconcile and retry only with the
same identity rather than creating a new write.

`edit_memory` is one tool. New calls supply `path`, `why`, and one nested
`operation` selected by `kind`:

- `replace_body`: `new_body`; optional `tags`, `expected_hash`, and semantic
  review fields.
- `replace_tags`: `tags`; optional `expected_hash` and semantic review fields.
- `replace_string`: `old_string`, `new_string`; optional `replace_all`, `tags`,
  `validate_only`, `expected_hash`, and semantic review fields.
- `batch_replace`: non-empty `edits` of `old_string`/`new_string` pairs with
  optional per-item `replace_all`; optional `validate_only`, `expected_hash`,
  and semantic review fields.
- `edit_section`: `heading`, `new_string`; optional `section_position`, `tags`,
  `expected_hash`, and semantic review fields.
- `patch_frontmatter`: `field`, explicit `value`; optional `allow_curated`,
  `validate_only`, and semantic review fields.
- `fill_row`: `row_key`, `take`; optional `overwrite`.

Legacy flat edit arguments remain runtime-only compatibility for at least one
release. They are deprecated, absent from public discovery schemas, cannot be
combined with nested `operation`, and must not be generated by new clients.

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
4. Read `active_capabilities.available_product_tools` as the commands exported
   by this adapter and do not recommend absent tools. Its
   `active_capability_sha256` identifies that filtered surface. The canonical
   MCP fingerprint separately identifies packaged full MCP discovery; it is not
   proof that every adapter exposes every packaged command.

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
- Display title, plus an optional explicit lowercase ASCII `slug` when the
  caller wants to control the portable filename
- Optional: tags, why-captured one-liner

### Procedure
1. Determine source type and target subfolder.
2. Generate filename: `YYYY-MM-DD-<slug>.md` where slug is dash-separated
   lowercase ASCII, ≤ 100 chars. An explicit `slug` controls only the filename;
   the Unicode display title is stored unchanged.
3. Write file with full frontmatter per `frontmatter.md` § source. `ingested_into: []`.
4. Body: `# <Title>` → `> brief description` → `## Capture` (raw content) → `## Why captured` (one or two sentences).
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
- Human-facing title (Unicode is fine), plus an optional explicit lowercase
  ASCII filename `slug`; propose the slug separately when portability or
  readability matters.

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
   Slugs are capped at 100 characters. Automatic slugging remains compatible
   with older releases and may use language-blind transliteration; for any
   non-Latin title, prefer an explicit meaningful ASCII slug. The real title is
   always stored in frontmatter and the H1.
3. **Draft the page in conversation** — show full content including frontmatter,
   all sections per the page-type template, and wikilinks to existing pages where
   they obviously match. **Run `suggest_links` on the draft first; use
   `suggest_relations` when directional meaning matters.** Put accepted note-level
   edges under `## Relations` as `- relation_type [[Target]]`; keep claim-specific
   edges in semantic-block `relations:` metadata.
4. **Wait for confirmation.** Default is propose-then-write.
5. On confirm:
   - Write the file.
   - For each source cited, update that source's `ingested_into` field to include a wikilink to this new note.
   - Update the relevant subfolder `index.md`.
6. Report paths written and any wikilinks that target nonexistent pages (offer to create stubs via `link`).

Upgrades and reconciliation never rename existing pages. Filename migration is
an explicit reviewed operation because paths are graph addresses.

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

## create entity

**Goal:** Create a registered typed entity without duplicating an existing identity.

### Triggers
- "create an entity for X," "add a concept page for Y"
- "this references [[X]]" where X doesn't exist yet (offered as a side-effect of `note`)
- "create a durable page for this recurring organization"

### Inputs to gather
- Entity name (becomes filename — see `page-types.md` § entity naming)
- Entity type — a stable ID from the active entity registry returned by bootstrap.
- For new entities: a one-paragraph summary; relevant frontmatter fields.
- For updates: the field or section to change.

### Procedure
1. Read the active entity registry and selected knowledge-pack priorities.
2. Call `connect_memory(operation="resolve-entity", name=...)`. If one active entity matches, use a guarded
   `edit_memory` correction or the canonical relation workflow instead of create.
3. If no entity matches and the identity is stable, recurring, central, and
   useful beyond this source, call `connect_memory(operation="create-entity")`.
4. Draft the page following `page-types.md` § entity, propose, and write on confirm.
5. For an existing entity, show a guarded diff and update through `edit_memory`.
6. Refresh the entity index and top-level counts through the governed writer.

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
- Optional `rerank_max_candidates` bounds the fused prefix passed to an active
  reranker. It must be an integer from the effective normalized `limit` through
  300. Inspect `candidate_limit_requested`, `candidate_limit_effective`,
  `scorer_input_count`, and `unscored_tail_count` in retrieval telemetry. The
  tail preserves fused order. This count is not a hard time budget: cold models,
  hardware, and passage length still matter.
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

## context / graph_context

**Goal:** Return one bounded, read-only context envelope containing stored page
bodies and semantic blocks, typed graph edges, provenance, evidence,
supersession history, unresolved targets, warnings, and explicit truncation.
Call `connect_memory(operation="context")`; `graph-context` remains a
compatibility alias with the same response.

### Triggers
- "what does this connect to in the graph"
- "show typed relations for this page"
- needing graph context around a search result without changing the page

### Procedure
1. Provide `path`, canonical reference, or `query`. Use a known identifier when
   you have one; use `query` to retrieve seed pages before graph assembly.
2. Keep `depth` small unless the caller asks for a wider neighborhood. Use
   `traversal_profile` for an `epistemic`, `provenance`, `causal`, `decision`,
   or broad `all` lens. Runtime `relation_types` / `node_types` only narrow it.
3. Inspect the returned truncation fields before claiming the context is
   exhaustive. Inspect the active profile, registry metadata, excluded counts,
   and warnings. Unregistered typed observations are warned but are not traversed
   or promoted to a portable relation family.
4. Treat unavailable derived graph data as a soft fallback. Stored page content,
   search, and provenance remain useful when a sidecar must be rebuilt.

### Writes performed
None. The graph sidecar is derived state only; this operation never mutates
Markdown.

---

## schema_memory

**Goal:** Govern optional corpus contracts, relation extensions, and traversal
profiles through one proposal-first surface.

### Procedure
1. Run `operation="infer"` with a contract `name` and optional `project` or
   `page_type` scope. Fewer than five pages remain advisory; required elements
   are proposed only when present in every page of a sufficiently large sample.
2. Review frequencies and the proposed contract. Persist only when explicitly
   requested with `save=true`.
3. Overwriting a saved contract requires its current `expected_hash`; never
   bypass a mismatch. Contracts live under `_Schema/contracts/`.
4. Use `operation="validate"` for read-only findings. `strict=true` changes the
   CLI/CI outcome but does not block normal writes.
5. Use `operation="diff"` to compare a saved contract with current corpus
   reality or another named contract.
6. Use `subject="relations"` to count core, extension, alias, deprecated,
   out-of-scope, and unregistered typed observations. Core meanings are packaged
   and universal. A vault extension must be a lowercase namespaced refinement
   of one core parent with a reviewed description. Inference never supplies
   missing semantics or saves automatically.
7. Return a complete reviewed `proposal` with `save=true` to adopt extensions.
   Alias collisions, incomplete parents/descriptions, stale hashes, invalid
   scopes, and deletion of observed keys are refused atomically. Deprecate an
   observed relation with a valid replacement instead of deleting its history.
8. Use `subject="traversal-profiles"` for custom read-only graph lenses. A custom
   profile must extend a built-in and may only narrow its bounds. Profiles change
   context selection, never Markdown, graph truth, confidence, or search ranking.

### Writes performed
Only an explicitly saved contract, relation registry, or traversal-profile
proposal. Inference, validation, diff, and model suggestions never mutate the
vault or assign truth.

---

## backfill_ids

**Goal:** Add stable IDs to legacy governed Markdown without an automatic
migration.

### Procedure
1. Call `maintain_memory(mode="backfill-ids")`; the default `dry_run=true`
   reports affected pages and identity problems without writing.
2. Review the complete proposal and resolve duplicate or malformed IDs first.
3. After explicit confirmation, repeat with `dry_run=false`. The batch is atomic
   and preserves all existing valid IDs.

### Writes performed
Missing frontmatter IDs only, and only in explicit write mode.

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
1. Run all checks. Audit is read-only in every detail mode.
2. Default to `detail="actionable"`: current blockers first, then malformed or
   unregistered relation work, then ordinary findings. Grandfathered relation-
   disposition debt is grouped as one `legacy_backlog` with exact upstream and
   observed counts plus deterministic bounded samples.
3. Use `detail="full"` only when raw finding enumeration is explicitly needed.
   `legacy_sample_limit` must be an integer from 0 through 50 and affects only
   the grouped actionable presentation.
4. Generate the report (per issue: path, problem, proposed fix; summary first).
5. Show the report. **Do not auto-fix anything.**
6. Offer: "Apply all proposed fixes?" / "Apply by check?" / "Apply per-issue?"
7. On per-issue or per-check confirmation, apply that fix and write any modified files.

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
