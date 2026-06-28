# kb-mcp

An MCP server that makes your Obsidian / markdown vault searchable — text, PDFs,
Office docs, images, and audio — from inside any MCP client (Claude, Cursor, …).
Self-hosted; your files stay yours.

## Why kb-mcp

- **Meets you where you work.** kb-mcp is an MCP *server*: your KB shows up as
  native tools inside Claude, Cursor, or any MCP client — desktop and mobile. You
  don't move into a new app; the KB comes to the agent you already use.
- **In place, not a silo.** It reads and writes your actual markdown files. They
  stay plain, portable, yours — editable in Obsidian, versioned/backed-up however
  you like. Most note-AI tools import *copies* into their own store; kb-mcp
  operates on the originals.
- **Multimodal, not just text.** Beyond markdown it extracts and searches PDFs,
  Office docs (docx/xlsx/pptx), images (OCR + CLIP visual search), and audio/video
  (speech-to-text) — so a photo, a scanned invoice, or a recording is findable.
  (Office/audio extraction is common; the distinctive combination is multimodal +
  MCP-native + over your live vault, plus CLIP *visual* retrieval.)
- **Real retrieval, not naive RAG.** Hybrid BM25 + vector fused via
  reciprocal-rank-fusion, plus wikilink-graph signals and type-aware ranking, over
  a *typed* corpus (raw sources vs compiled notes), with provenance and
  write-governance.
- **Substrate, not a brain.** The server only does deterministic work (search,
  extract, embed); reasoning happens in your client's model. No server-side LLM,
  no proprietary cloud backend.

## How it compares

- **vs. doc-chat / RAG apps:** they ingest copies into their own store and you
  work inside their UI; kb-mcp works in place over your live vault, inside your
  existing agent.
- **vs. other MCP note servers:** most are text-only search/CRUD; kb-mcp adds
  multimodal extraction + CLIP visual search + a typed/governed knowledge model.

## Quickstart (local)

The fastest path is **local, inside Claude Code, over your own vault — no cloud,
no OAuth, ~20 minutes**:

```bash
git clone <repo-url> kb-mcp && cd kb-mcp
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -e .                 # lean: keyword/BM25 search, no heavy deps
python -m kb_mcp init --vault "/path/to/your/Obsidian"
claude mcp add kb-mcp --env KB_MCP_VAULT_PATH="/path/to/your/Obsidian" \
  --env KB_MCP_DISABLE_EMBEDDINGS=1 -- python -m kb_mcp --transport stdio
python -m kb_mcp install-skill   # the "brain" — don't skip this
```

**[SETUP-LOCAL.md](SETUP-LOCAL.md)** walks the local path end to end (vault
bootstrap, hybrid-vs-lean choice, the skill, and the optional auto-capture hooks).
For remote / mobile access, see **[docs/deployment.md](docs/deployment.md)**.

## Tools

Two tiers. Tier 1 is type-routed and encodes the KB discipline; Tier 2 is a
filesystem escape hatch for what Tier 1 can't express.

**Tier 1 — type-routed (primary).** Use these whenever a Tier 1 op fits.

- `find` — read-only search across `Knowledge Base/`, type/project/tag filtered.
- `get` — read a full file anywhere under the vault root (including read-only
  curated input folders). `frontmatter_only=true` returns just the frontmatter.
- `add` — capture a raw `source` page with full write discipline.
- `note` — create any of the six compiled page types (research-note, insight,
  failure, pattern, experiment, production-log) with `ingested_into:` back-refs on
  cited sources.
- `link` — create a typed entity under `Entities/<Type>/<Name>.md` (person,
  concept, library, decision).
- `edit` — in-place edit of a compiled page. Modes: body / tags / surgical
  `old_string`→`new_string`; `edits=[…]` (batch surgical); `row_key`+`take` (fill a
  `[take: ]` opinion row); `field`+`value` (patch one frontmatter field). Bumps
  `updated:`.
- `replace` — supersession: write a new page + flip the old one to
  `status: superseded` with a `superseded_by:` back-link. The modify path for
  substantial rewrites.
- `preserve` — capture a binary or text artifact to `Evidence/<scope>/<category>/`
  (append-only).
- `audit` — read-only graph health check (broken wikilinks, orphan entities,
  unprocessed sources, index/log drift, tag inconsistency).

**Tier 2 — filesystem-parity (escape hatches).** Use when Tier 1 can't express
what you need: new folder structures, files outside the typed-note set, or
surgical edits.

> **Lean surface (`KB_MCP_DISABLE_TIER2`).** Set `KB_MCP_DISABLE_TIER2=1` (in
> `.env` or the service environment) to drop all 8 Tier 2 tools from registration;
> the Tier 1 ops still load. Use it when the client *defers* MCP tools behind a
> keyword search — a smaller surface means an agent reaches `find`/`get`/`note`
> without wading past a dozen escape hatches. Default is unset: all tools register.

- `create_file` — write a file at an arbitrary vault path, optional frontmatter
  dict. `kind="dir"` instead makes a folder (mkdir -p). Refuses Sources/Evidence;
  curated trees require `allow_curated=true`.
- `list_directory` — list files + subfolders (recursive optional). Surfaces the
  `type:` frontmatter field for `.md` entries. Read-only.
- `move_file` — rename/relocate. Rewrites inbound wikilinks by default.
- `delete` — **trash** a file OR folder (auto-detected). Moves to
  `Knowledge Base/_trash/YYYY-MM-DD/` with a `.meta.json` sidecar; never permanent.
  Recovery is `recover_from_trash`. Requires `confirm=true`; folders need
  `recursive=true` if non-empty; refuses on inbound links unless
  `force_orphan=true`.
- `list_trash` — enumerate recoverable trash entries (original path, timestamp,
  force-flags used). Also surfaces drift. Read-only.
- `recover_from_trash` — undo a delete; reads the sidecar to find the original
  location. Optional `restore_path` override.
- `append_to_file` — append text. Refuses on Sources/.
- `list_inbound_links` — find all files whose wikilinks resolve to a target.
  Read-only. Useful before move/delete.

**Discipline preserved across both tiers:** Sources/ and Evidence/ are
append-only (no Tier 2 op writes there); curated input folders (configurable)
refuse Tier 2 writes by default — pass `allow_curated=true` as a deliberate
per-call acknowledgement; deletes are never permanent (`delete` trashes,
recoverable via `recover_from_trash`); every write logs to
`Knowledge Base/log.md`.

**Two-layer traceability:**

- `Knowledge Base/log.md` — durable content history. Writes only, KB-scoped. The
  "what happened to the vault" record; never auto-purged.
- `logs/kb-mcp.log` — service log. Every call (reads + writes) is surfaced via a
  per-call middleware as `tool=<name> duration_ms=<n>
  event=tool_success|tool_error`. The operational layer (did the call reach the
  server, spot slow ops). Rotated in-process (5 MB × 5) — same on every platform.

## Multimodal extraction (optional)

Two optional dependency extras turn binaries into searchable text/vectors. Both
**soft-fall-back**: if the libraries aren't installed, search degrades to
keyword/BM25 and uploads still work, just without server-side extraction.

- **`embeddings`** (`pip install -e ".[embeddings]"`) — `torch` +
  `sentence-transformers` + `pillow`. Adds the local vector half of hybrid `find`
  (a bge text model) and **CLIP** image embedding for visual search. ~1–2 GB
  download.
- **`media`** (`pip install -e ".[media]"`) — server-side extraction on upload:
  **faster-whisper** ASR for audio/video, **Tesseract** OCR for images,
  **PyMuPDF** for PDFs, and **MarkItDown** for Office/HTML docs
  (docx/xlsx/pptx/html). Two system tools are not pip-installable: **Tesseract OCR**
  (`winget install UB-Mannheim.TesseractOCR`, or set `KB_MCP_TESSERACT_CMD`), and
  ffmpeg (bundled by PyAV via faster-whisper, so audio/video decode works out of
  the box).

**GPU note.** A CUDA GPU accelerates ASR/OCR/embedding but is **not required** —
CPU works, just slower (pick a smaller Whisper model with
`KB_MCP_WHISPER_MODEL=base`). On Windows + NVIDIA the `media` extra pins a CUDA-12
runtime (cublas/cudnn/cudart) that ctranslate2 needs alongside torch's cu132 build;
RTX 50-series (Blackwell, sm_120) is supported. See
**[docs/deployment.md](docs/deployment.md)** for the GPU bring-up and the
Blackwell/CUDA details. Disable extraction entirely with
`KB_MCP_DISABLE_MEDIA_EXTRACTION=1` (uploads still work; no searchable-text
extraction).

## Remote access (optional)

To reach the vault from claude.ai on the web or mobile, the server runs as an
always-on HTTP service behind a public HTTPS endpoint, authenticated with
**GitHub OAuth** locked to a single login. claude.ai's MCP client fetches the
connector URL from Anthropic's cloud (not from your phone), so the endpoint must
be publicly reachable — a **Cloudflare Tunnel** (domain you own) or **Tailscale
Funnel** (free `*.ts.net` host) provides it.

Full setup — OAuth app, tunnel, the service installers (launchd / systemd /
NSSM), multi-host deployment, and troubleshooting — is in
**[docs/deployment.md](docs/deployment.md)**. Replace `<your-host>` /
`example.com` throughout with your own hostname.

## Configuration

The server reads configuration from environment variables (or a `.env` file in
the repo root). The only required one is the vault path.

| Variable | Purpose |
|---|---|
| `KB_MCP_VAULT_PATH` | **Required.** Vault root — the folder that contains `Knowledge Base/`. |
| `KB_MCP_DISABLE_EMBEDDINGS` | `1` forces keyword/BM25-only search (no torch/vectors). |
| `KB_MCP_DISABLE_TIER2` | `1` drops the 8 Tier 2 escape-hatch tools (leaner tool surface). |
| `KB_MCP_DISABLE_MEDIA_EXTRACTION` | `1` skips server-side OCR/ASR/PDF/office extraction. |
| `KB_MCP_DISABLE_CLIP` | `1` disables CLIP visual image search. |
| `KB_MCP_CLIP_DEVICE` | `cpu`/`cuda` override for CLIP (defaults to CPU when ASR is active). |
| `KB_MCP_DIARIZE` | Set to enable opt-in ASR speaker diarization (`[Speaker A]: …` turns). |
| `KB_MCP_VOICE_DEVICE` | `cpu`/`cuda` override for the ECAPA voice embedder (defaults to CPU when ASR is active). |
| `KB_MCP_VOICE_EMBED_MODEL` | ECAPA checkpoint for named-speaker attribution (default `speechbrain/spkrec-ecapa-voxceleb`). |
| `KB_MCP_WHISPER_MODEL` | Whisper model size for ASR (e.g. `base`, `small`, `large-v3`). |
| `KB_MCP_TESSERACT_CMD` | Path to the `tesseract` binary if not auto-discovered. |
| `KB_MCP_DUP_THRESHOLD` | Near-duplicate cosine-warning threshold (default `0.90`). |
| `KB_MCP_DISABLE_QUERY_LOG` | `1` disables the retrieval-eval query/write logs. |
| `KB_MCP_HOST` | Bind host for the HTTP transport (default `127.0.0.1`). |

Remote-only (see [docs/deployment.md](docs/deployment.md)): `KB_MCP_BASE_URL`,
`GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `KB_MCP_GITHUB_USERNAME`,
`KB_MCP_JWT_SIGNING_KEY`.

## License

AGPL-3.0-or-later — see [LICENSE](LICENSE).
