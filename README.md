# exomem

[![PyPI](https://img.shields.io/pypi/v/exomem.svg)](https://pypi.org/project/exomem/)
[![Python](https://img.shields.io/badge/python-%3E%3D3.11-3776ab.svg)](https://pypi.org/project/exomem/)
[![CI](https://github.com/Artexis10/exomem/actions/workflows/ci.yml/badge.svg)](https://github.com/Artexis10/exomem/actions/workflows/ci.yml)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)

External memory for MCP-capable agents.

exomem turns an owned Markdown/Obsidian vault into a local knowledge substrate
for Codex, Claude Code, Cursor, chatbots, CLI agents, and any client that can
call MCP tools. Your files stay plain, local, portable, and editable outside the
server.

```text
agent -> MCP tools -> exomem -> your Markdown / Obsidian vault
```

## What it does

- **Searches the vault you already own.** Markdown stays in place; exomem does
  not import copies into a proprietary note store.
- **Retrieves across text and media.** Markdown, PDFs, Office docs, images,
  screenshots, audio, and video can become searchable through local extraction.
- **Keeps sources separate from conclusions.** Raw captures, compiled notes,
  entities, evidence, and superseded conclusions live in typed folders.
- **Surfaces review work.** Audit and attention queues can show unprocessed
  sources, stale notes, broken links, and close-by claims worth reviewing.
- **Measures, never judges.** The server does deterministic work: search,
  extraction, ranking, embeddings, file writes, and graph checks. Reasoning stays
  in the client model.

## Why use it

Most AI note tools make you move into their app or ingest your files into their
store. exomem works the other way around: agents come to your vault.

| Compared with | Difference |
| --- | --- |
| Doc-chat / RAG apps | exomem works over live files instead of imported copies. |
| Basic MCP note servers | exomem adds typed knowledge operations, multimodal extraction, audit queues, and CLI/REST parity. |
| Memory hidden inside one assistant | exomem is client-agnostic: use the same vault from Claude Code, Codex, Cursor, scripts, or a custom chatbot. |

For a deeper point-in-time comparison, see
[docs/comparison-engraph.md](docs/comparison-engraph.md).

## Install

From PyPI:

```bash
pip install exomem
exomem --help
```

For development or to run the bundled sample vault:

```bash
git clone https://github.com/Artexis10/exomem.git
cd exomem
uv sync
uv run python scripts/smoke-sample-vault.py
```

## Five-minute proof

Run exomem against the bundled sample vault before connecting your own notes:

```bash
uv run python scripts/demo-sample-vault.py
```

Expected shape:

```text
exomem sample-vault demo
vault: examples/sample-vault

1. doctor: PASS (lean profile)
2. find "retrieval":
   - Knowledge Base/Sources/Sessions/2026-06-30-sample-session.md
   - Knowledge Base/Notes/Insights/retrieval-needs-owned-files.md
3. get retrieval insight:
   - title: Retrieval needs owned files
   - type: insight
   - excerpt: Local-first knowledge tools should retrieve from files the user already owns.
4. audit: PASS (broken_wikilink, unprocessed_source)

demo PASS
```

## Local quickstart

Point exomem at a vault folder that contains, or should contain,
`Knowledge Base/`:

```bash
exomem init --vault "/path/to/your/Obsidian"
exomem doctor --vault "/path/to/your/Obsidian" --profile lean
```

For Claude Code over stdio:

```bash
claude mcp add exomem \
  --env EXOMEM_VAULT_PATH="/path/to/your/Obsidian" \
  --env EXOMEM_DISABLE_EMBEDDINGS=1 \
  -- exomem --transport stdio
```

For Claude Code, install the bundled Exomem Knowledge Base skill so the agent
knows when to search, capture, and compile notes:

```bash
exomem install-skill
```

Optional for heavier daily use:

```bash
exomem install-hook
```

The skill installs under the stable Claude Code name `knowledge-base`; Exomem is
the server and tool layer behind it. The skill is recommended for Claude Code.
Hooks are Claude Code-only reliability nudges for long sessions: a read-side
reminder before answers and a write-side
reminder at natural stopping points. Other MCP clients can still use the server;
put the same knowledge-discipline instructions in their system/project
instructions if they do not support skills.

Full local setup is in [SETUP-LOCAL.md](SETUP-LOCAL.md). Remote/mobile setup is
in [docs/remote-checklist.md](docs/remote-checklist.md) and
[docs/deployment.md](docs/deployment.md).

## Core tools

exomem exposes typed MCP tools for common knowledge-base work:

| Tool | Purpose |
| --- | --- |
| `find` | Search notes, sources, entities, and evidence with type/project/tag filters. |
| `get` | Read a full page or frontmatter. |
| `add` | Capture a raw source page. |
| `note` | Create compiled notes: research note, insight, failure, pattern, experiment, or production log. |
| `edit` | Patch an existing compiled page. |
| `replace` | Supersede an old conclusion with a new one and preserve the link between them. |
| `preserve` | Store binary or text evidence append-only. |
| `audit` | Check graph and corpus health. |
| `attention` | Surface review queues such as stale notes, close-by claims, and unprocessed sources. |
| `overview` | Bounded, read-only structure report of the vault or a subtree — works outside `Knowledge Base/` and before `init`. |

Tier-2 filesystem tools exist for escape hatches such as listing directories,
creating files, moving pages, trashing files, and recovering from trash. Set
`EXOMEM_DISABLE_TIER2=1` if you want a smaller tool surface.

Every write records durable history in `Knowledge Base/log.md`. Service calls
also go to `logs/exomem.log`.

## One operation, three doors

Every operation is declared once and exposed through:

- **MCP** for agents.
- **CLI** for terminal and scripts.
- **REST** for personal HTTP integrations when `EXOMEM_REST_API_KEY` is set.

Examples:

```bash
kb find "project handoff" --mode keyword
kb find "stale decision" --json
kb get "Notes/Insights/retrieval-needs-owned-files" --json
kb note --note-type insight --title "Agents need durable context" \
  --content "# Agents need durable context"
```

```bash
curl -s -X POST http://127.0.0.1:8765/api/find \
  -H "Authorization: Bearer $EXOMEM_REST_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "project handoff", "mode": "keyword"}'
```

CLI and REST share the same JSON envelope:

```json
{"success": true, "data": []}
```

## Optional multimodal stack

The lean install works with keyword/BM25 search. Optional extras add local
embedding search and media extraction:

```bash
uv sync --extra embeddings
uv sync --extra media
```

- `embeddings`: local text embeddings plus CLIP image search.
- `media`: OCR for images, PDF extraction, Office document extraction, and
  faster-whisper ASR for audio/video.

System tools: Tesseract is required for image OCR. On Windows:

```powershell
winget install --id UB-Mannheim.TesseractOCR -e
```

GPU acceleration is useful but not required. See
[docs/deployment.md](docs/deployment.md) for CUDA, Blackwell, diarization, and
remote-service details.

## Configuration

The server reads environment variables or a `.env` file. The main ones are:

| Variable | Purpose |
| --- | --- |
| `EXOMEM_VAULT_PATH` | Vault root containing `Knowledge Base/`. |
| `EXOMEM_DISABLE_EMBEDDINGS` | `1` forces keyword/BM25-only search. |
| `EXOMEM_DISABLE_TIER2` | `1` hides Tier-2 filesystem tools. |
| `EXOMEM_REST_API_KEY` | Enables authenticated REST routes. |
| `EXOMEM_DISABLE_MEDIA_EXTRACTION` | `1` skips server-side OCR/ASR/PDF/Office extraction. |
| `EXOMEM_DISABLE_CLIP` | `1` disables CLIP image search. |
| `EXOMEM_VIDEO_SCENE_FRAMES` | Set to enable video scene detection + persisted, OCR'd scene-frame JPEGs (default off). |
| `EXOMEM_VIDEO_SCENE_THRESHOLD` | Scene-boundary hash threshold in bits of 64 (default 10). |
| `EXOMEM_VIDEO_SCENE_MIN_SECS` | Minimum scene duration in seconds; closer boundaries merge (default 4). |
| `EXOMEM_WHISPER_MODEL` | Whisper model size for ASR, such as `base` or `small`. |
| `EXOMEM_TESSERACT_CMD` | Path to the `tesseract` binary if not auto-discovered. |

Legacy `EXOMEM_*` names (from the project's former working name, exomem) remain
honored: each is promoted to its `EXOMEM_*` equivalent at startup, with an
explicitly set `EXOMEM_*` value winning on conflict. `import exomem` and
`python -m exomem` likewise keep working as deprecated aliases.

Remote-only variables and full deployment notes are in
[docs/deployment.md](docs/deployment.md).

## Project status

exomem is packaged on PyPI, uses Release Please for versioning, and follows the
lightweight SemVer policy in [docs/release.md](docs/release.md). The public CLI
entry point is `exomem`; `kb` is the short daily-driver alias for knowledge-base
operations.

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
