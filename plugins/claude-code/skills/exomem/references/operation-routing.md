# Canonical operation routing and media transport

## Canonical Operations
Product commands are the public interface. The operations below are canonical
implementation leaves: product commands route here so filenames, folders,
frontmatter, supersession, indexes, append-only rules, and binary guards stay in
one place. Agents should call product commands by default; use the leaf names
below for debugging internals, interpreting old notes, or understanding exactly
what a product command routes to.

Operations split into two tiers. **Tier 1 is primary** — every typed-note
workflow goes through it because the type-routing IS the discipline. **Tier 2 is
the escape hatch** for cases that don't fit a Tier 1 shape. If a write fits Tier
1, use it.

### Tier 1 — type-routed (primary)

These encode the KB's discipline: filenames, folders, frontmatter, supersession,
and index updates are determined by the operation, not the caller.

| Op | Intent | Writes to |
|---|---|---|
| **bootstrap** | Return a portable, versioned operating contract for generic MCP clients. A skill-aware agent fetches session state when current policy or capabilities are absent, with compact fallback when its exposed schema or server cannot accept the skill contract; use diagnostics profile for timing/performance interpretation | — |
| **add** | Capture raw input as immutable source | `Sources/<Kind>/[<Domain>/]` |
| **note** | Compile a structured note from raw input or thinking | `Notes/<type>/` |
| **link** | Create or update an entity, wire backlinks | `Entities/<type>/` |
| **preserve** | Capture a **text** factual artifact for an incident scope. Binaries (PDF / image / any file) go out-of-band via upload (see the media transport guidance below), not this tool | `Evidence/<scope>/` |
| **edit** | In-place edit through one `edit_memory` tool. New clients send required nested `operation` with one `kind`: `replace_body`, `replace_tags`, `replace_string`, `batch_replace`, `edit_section`, `patch_frontmatter`, or `fill_row`. See `operations.md` for fields. Bumps `updated:` | the page |
| **observe_memory** | Add, update, remove, or validate one compact observation or rich semantic unit. Update/remove require the current parent `expected_hash` and unit `expected_fingerprint`. Compact is `- [category] content #tags (context) ^anchor`; typed unit relations require an explicit governed rich `kind` | the compiled page |
| **find** | Type-aware search across the KB (read-only). Supports compact lookups, packed reasoning context, and diagnostics via `include_timings` / `rerank` when needed | — |
| **suggest_links** | Surface existing pages a draft or page should link to, hub-aware (read-only) | — |
| **graph_context** | Return a bounded typed graph neighborhood for a page or query from the derived graph sidecar. Read-only | — |
| **suggest_relations** | Propose typed graph relations for a page or draft; review-only, never writes | — |
| **get** | Read a full file by path; `frontmatter_only=true` returns just the frontmatter. Returns `content_hash` + `mtime` for the two-writer drift guard (echo `content_hash` to `edit` via `expected_hash`). Read-only | — |
| **audit** | Lint pass: orphans, broken links, supersession integrity, aged unprocessed sources | proposals only |
| **overview** | Bounded structure report of the vault or a subtree — folder tree, counts, frontmatter coverage, junk candidates. Works outside the KB and pre-init (read-only) | — |
| **adopt** | Safe first-run adoption workflow for an existing vault: scan-only by default; can save a manifest or copy selected legacy text files as Sources while preserving originals | `Knowledge Base/_Adoption/` or `Sources/Imported/` only in explicit write modes |
| **propose_compilation** | Draft a note scaffold from unprocessed source(s) — the backlog-drain companion to audit (read-only) | proposals only |
| **replace** | Supersession: mark old, write new with header pointer | both old + new |
| **reconcile** | Heal drift from out-of-band edits (any editor/sync/mobile, e.g. Obsidian): recompute index counts + re-embed stale files + report remaining drift. Remote tools may preview with `dry_run=true`; actual repair is host-operator work via `exomem maintain --reconcile` | drifted indexes + embedding sidecar |
| **provenance_report** | Scan note bodies for `<!-- key:value -->` provenance tags (filter by key/value/path). Read-only | — |

For the full per-operation spec — inputs, validation, write rules, edge cases —
see `operations.md`.

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
  deliver the *original file* separately. Decide the lane before the transport:
  raw material takes
  **`capture_source(title="...", source_kind="...", files=[{"download_url": "...", "file_id": "...", "mime_type": "...", "file_name": "..."}])`**,
  and proof-bearing material takes
  **`preserve_artifacts(scope="...", category="...", files=[...])`** — the same
  handle shape either way. On claude.ai web, call whichever of those fits
  directly when the client supplies file handles. Otherwise call
  **`transfer_artifact(operation="upload")`** for a short-lived `{token, upload_url}`. If the
  file-owning client can reach `upload_url`, multipart-`curl` the attached files there; otherwise
  open the prefilled browser upload form or give its URL to the user for a manual upload.
  **Searchable binaries are automatic:** the server transcribes audio/video
  (Whisper), OCRs images (Tesseract), reads PDFs (pymupdf), extracts office/web
  documents (docx/xlsx/pptx/html via MarkItDown; txt/eml/ics via native parsers),
  and CLIP-embeds images and per-keyframe video frames for visual search — all
  server-side after upload, filling an embedded sidecar so any upload becomes
  findable by content. You *may* still pass a `text` field to supply your own
  extraction; it takes precedence. Upload responses return concrete metadata (`stored_path`, `size`, `hash`, `hash_algorithm`, `media_id`, `content_type`) so agents can report exactly what landed. The write tools take text only and reject
  inline byte blobs (`BINARY_BLOB_REJECTED`). Full workflow:
  `operations.md` § preserve.
- **Media processing is automatic and actionable — `process_media`.** Supported
  audio and video preserved through Exomem or copied directly into the governed
  Knowledge Base are reconciled into durable timestamped transcription work.
  Call `process_media(path=..., operation="process")` for immediate targeted
  reconciliation, `operation="status"` for bounded per-artifact state and next
  actions, or `operation="retry"` after fixing a recorded blocked/failed reason.
  These actions enqueue or inspect work; they do not wait for model completion or
  overwrite an existing valid transcript.
- **Pull a vault file back out — the download channel.** Call
  **`transfer_artifact(operation="download")`** for a short-lived `{token, download_url}`, then GET
  `download_url?path=<vault-relative path>` with `Authorization: Bearer <token>`.
  Read-only, download-scoped, path confined to the vault root.
- **Media hits in `find` are first-class.** An extracted media sidecar carries
  `media_type` and `media_file` (a pointer to the original binary). Treat the
  *file* as the result and the matched transcript/OCR snippet as the "why"; offer
  to pull the original via `transfer_artifact(operation="download")`. Images and video are also
  searchable by *visual content* (CLIP), not just text — a purely-visual hit
  carries a `clip_score`; a video visual hit also carries `clip_match_at` (e.g.
  `"14:32"`), the timestamp of the matching keyframe.
- **View a video's frames on demand — `read_media`.** To *see* what a vault
  video shows (slides, screen recordings, meetings), call
  `read_media(path, max_frames=8, start_sec=?, end_sec=?)` — it returns
  sampled keyframes INLINE as JPEG image blocks (no download round-trip needed),
  preceded by per-frame timestamps. The comprehension companion to visual search:
  `ask_memory` locates the moment (`clip_match_at`), `read_media` shows it — an
  overview call first, then zoom with `start_sec`/`end_sec` around that timestamp.
  Bounded and read-only (default 8 frames, hard cap 16, JPEG ≤768px); soft-fails
  with a clear code when the server lacks the media extra.
- **Read-only / excluded subtrees are write-protected.** Mark a subtree `readonly:`
  or `excluded:` in `Knowledge Base/_access.yaml` (see `write-scope.md` §
  Per-subtree access overrides): `readonly` stays searchable but refuses **every**
  write (Tier 1 and Tier 2) — hard, no override; `excluded` is additionally hidden
  from `find`/embeddings. This is the in-KB counterpart to the by-location rule that
  everything outside `Knowledge Base/` is read-only.
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

### Phrasing → operation mapping (canonical leaf reference)

For normal agent work, use the route table in `../SKILL.md`. This mapping is a
reference for the canonical operation leaves that product commands route to.

- "save this," "log this," "capture this," "add to my KB" → **add**
- "compile this into a note," "make a note on this," "write this up," "distill this" → **note** (typically preceded by an implicit **add**)
- "log this experiment," "I'm running a 30-day X protocol" → **note** with type=experiment
- "log this batch," "add this episode," "record this launch" → **note** with type=production-log
- "this is connected to [[X]]," "create an entity for X" → **link**
- "preserve this letter," "file this in evidence," "save this for the record" → **preserve**
- "import my vault," "adopt these notes," "make this old knowledge base usable" → **adopt** first (`mode="scan-only"`), then ask before `save-manifest`, `copy-as-sources`, or compile actions
- "update the skill," "the KB structure needs to change" → no MCP tool — hand-edit `_Schema/` files directly
- "fill in the take for X," "set the take on that row" → **edit** (`row_key`+`take`)
- "make these few edits to the page" (same page) → **edit** (`edits=[…]`)
- "what do I have on X," "find my notes on Y," "have I covered Z" → **find**
- "why was this note changed," "show the history of this page" → **get** (`include_history=true`)
- "what did I used to think about X," "show me the superseded version" → **find** (`prefer_active=false`)
- "what should this link to," "densify this page's links" → **suggest_links**
- "what does this connect to in the graph," "show typed relations" → **graph_context**
- "suggest relations," "what should this support / contradict / supersede" → **suggest_relations**
- "which relations recur," "review unknown relation labels" → **schema_memory** (`subject="relations"`; proposal-first)
- "use an epistemic / provenance / causal lens" → **graph_context** (`traversal_profile=...`)
- "what should I compile next," "drain the source backlog" → **propose_compilation**
- "audit the KB," "lint the vault," "check for orphans" → **audit**
- "what does this vault look like," "assess my vault," "how is this vault organized" → **overview**
- "what should Exomem do with this existing vault," "how can we migrate this safely" → **adopt**
- "I edited the vault directly / on my phone — sync it up," "heal the drift" → preview remotely, then have the host operator run **reconcile**
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
- topic maps to a project/domain/entity, or "what did I conclude about X" -> proactive **ask_memory** first, fold the hits into the answer
- a decision is made or a problem just got solved -> stepping-stone: capture via **capture_source**/**remember**, then report the path
- a method was run and the user says how it turned out -> stepping-stone: capture the method, the adjustment and the outcome, then report the path
- the user states an intent or commits to work -> resolve workflow posture, inspect Planning, then update an existing matching item before creating an inbox item
- the user reports that something happened, was produced, delivered, approved, published or failed -> resolve workflow posture and append a Record to one compatible collection; never transition Planning automatically

When you say something oblique like "interesting, save it," default to
**capture_source** and ask whether to compile only if there is a durable
conclusion.
