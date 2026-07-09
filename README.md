# exomem

[![PyPI](https://img.shields.io/pypi/v/exomem.svg)](https://pypi.org/project/exomem/)
[![Python](https://img.shields.io/badge/python-%3E%3D3.11-3776ab.svg)](https://pypi.org/project/exomem/)
[![CI](https://github.com/Artexis10/exomem/actions/workflows/ci.yml/badge.svg)](https://github.com/Artexis10/exomem/actions/workflows/ci.yml)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)

Durable memory with sources, proof, history, and review for MCP-capable agents.

exomem turns an owned Markdown/Obsidian vault into a local knowledge substrate
for Codex, Claude Code, Cursor, chatbots, CLI agents, and any client that can
call MCP tools. Your files stay plain, local, portable, and editable outside the
server.

```text
agent -> MCP tools -> exomem -> your Markdown / Obsidian vault
```

## Prove it in 30 seconds

```bash
uvx exomem demo
```

One command, no install, no config, no vault of your own needed:

```text
exomem demo — bundled sample vault, keyword mode, fully local
vault: /tmp/exomem-demo-XXXXXX

1. doctor: PASS (0.8s)
2. find "retrieval": PASS (0.1s)
   - Knowledge Base/Sources/Sessions/2026-06-30-sample-session.md
   - Knowledge Base/Notes/Insights/retrieval-needs-owned-files.md
3. get retrieval insight: PASS (0.0s)
   - title: Retrieval needs owned files
   - type: insight
   - excerpt: Local-first knowledge tools should retrieve from files the user already owns.
4. audit: PASS (0.0s)

demo PASS — total 1.0s. This is your proof: agents search files you own.
Next: connect your own vault with `exomem setup`
```

Runs fully local and read-only against a sample vault bundled in the package.
Add `--keep` to leave that copy on disk afterward and open it in Obsidian.

## Install on Mac (one line)

Not comfortable with a terminal? Paste this into the macOS **Terminal** app —
it installs `uv`, installs exomem, and walks you through `exomem setup`:

```bash
curl -fsSL https://raw.githubusercontent.com/Artexis10/exomem/main/scripts/install.sh | sh
```

Also works on Linux. Safe to run again later — it skips whatever's already
done. If it can't prompt you interactively (e.g. run from another script), it
prints the exact command to run next instead of guessing. Prefer to run each
step yourself? See below or the full manual walkthrough in
[QUICKSTART.md](https://github.com/Artexis10/exomem/blob/main/QUICKSTART.md).

## Set it up in 5 minutes

```bash
uv tool install exomem   # or: pip install exomem
exomem setup --vault "/path/to/your/Obsidian"
```

One command does the whole local setup: the wizard scans your vault and shows
what's already there, initializes `Knowledge Base/`, runs the `doctor`
preflight, registers the server with Claude Code, and installs the skill.

Already have a vault full of notes? That's the normal case: `adopt` gives a
scan-first, read-only report of what's there, suggested knowledge packs, and
safe copy/compile-planning next actions. Exomem only ever writes under `Knowledge Base/` — your
existing files stay untouched unless you explicitly copy or compile selected material. See
[QUICKSTART.md § Already have a vault full of notes?](QUICKSTART.md#already-have-a-vault-full-of-notes)
for the full contract, including daily-notes vaults. Re-running `setup` is
safe; completed steps report `[skipped]`. Non-interactive:
`exomem setup --yes --vault "/path" --lean`.

The individual steps (`exomem init` / `doctor` / `install-skill` /
`install-hook`, plus `claude mcp add`) still exist as the manual path — see
[QUICKSTART.md](QUICKSTART.md).

The skill installs under the Claude Code name `exomem` — the same name as the
connector, so skill, server, and tools all read as one product. The skill is
recommended for Claude Code —
the server gives Claude the tools, the skill is what makes it use them. Hooks
are local-client reliability nudges for Claude Code and Codex: a read-side
reminder before answers and a write-side reminder at natural stopping points.
The read-side hook suppresses obvious control/status prompts like `continue`,
`merge it`, and `are you done?`, and can optionally upgrade that reminder to real
retrieved KB content (`EXOMEM_RETRIEVE_INJECT=1`, opt-in; the legacy
`KB_RETRIEVE_INJECT` name still works). For Codex, run
`exomem install-hook --client codex`; for Claude Code, `exomem install-hook` —
see
[QUICKSTART.md § 7](QUICKSTART.md#7-recommended-make-the-kb-automatic-both-directions).
Other MCP clients can still use the server. If they do not support Skills,
have them call `bootstrap()` once at the start of the session; it returns the
same compact operating contract through MCP, including when to search, when to
save, workflow-skill discovery, upload guidance, and performance profiles. It
also teaches the authoring loop: search first, draft the typed note, run
`suggest_links`, write with the right tool, inspect warnings/suggestions, then
report the path.

For client-specific assistant instructions, see
[docs/ai-assistant-guide.md](docs/ai-assistant-guide.md). For the boundary
between Exomem and a chat product's built-in memory, see
[docs/vs-built-in-memory.md](docs/vs-built-in-memory.md).

Full local setup is in [QUICKSTART.md](QUICKSTART.md). Remote/mobile setup is
in [docs/remote-quickstart.md](docs/remote-quickstart.md) and
[docs/deployment.md](docs/deployment.md).

The product model is intentionally simple: built-in AI memory remembers preferences and routing, while Exomem stores durable governed knowledge with sources, proof, history, decisions, records, and review. See [docs/product-model.md](docs/product-model.md) for the full mental model, [docs/knowledge-packs.md](docs/knowledge-packs.md) for pack/admin details, and [docs/workflow-skills.md](docs/workflow-skills.md) for the named agent workflows.

For development, or to run the sample vault from a checkout instead of a
package install:

```bash
git clone https://github.com/Artexis10/exomem.git
cd exomem
uv sync
uv run exomem demo
```

## Connect your agent

| Client | How |
| --- | --- |
| Claude Code | `exomem setup` registers it for you (see above) |
| Codex CLI | `codex mcp add` plus optional `exomem install-hook --client codex` - see [docs/ai-assistant-guide.md#codex-cli](docs/ai-assistant-guide.md#codex-cli) |
| claude.ai or hosted chat | Remote MCP/connector - see [docs/remote-quickstart.md](docs/remote-quickstart.md) and [docs/ai-assistant-guide.md#hosted-chat-clients](docs/ai-assistant-guide.md#hosted-chat-clients) |
| Any MCP client | Generic stdio config - see below and [docs/ai-assistant-guide.md#generic-stdio-mcp-clients](docs/ai-assistant-guide.md#generic-stdio-mcp-clients); call `bootstrap()` first |
| Docker (no Python) | One `docker run` line — see below and [docs/docker.md](docs/docker.md) |

<details>
<summary>Codex CLI</summary>

```bash
codex mcp add exomem --env EXOMEM_VAULT_PATH="/path/to/vault" -- exomem --transport stdio
```

Optional local hooks, using the same Exomem scripts as Claude Code:

```bash
exomem install-hook --client codex
```

Verify deployed Claude Code/Codex hooks without changing anything:

```bash
exomem install-hook --check
```

For the `AGENTS.md` instruction block and the "do not search every tiny prompt"
policy, see [docs/ai-assistant-guide.md](docs/ai-assistant-guide.md).

Or add it directly to `~/.codex/config.toml`:

```toml
[mcp_servers.exomem]
command = "exomem"
args = ["--transport", "stdio"]
env = { EXOMEM_VAULT_PATH = "/path/to/vault" }
```

</details>

<details>
<summary>Any MCP client (generic stdio)</summary>

```json
{"mcpServers": {"exomem": {"command": "exomem", "args": ["--transport", "stdio"], "env": {"EXOMEM_VAULT_PATH": "/path/to/vault"}}}}
```

After connecting, ask the agent to call `bootstrap()` before using the KB. Claude
Skills are still the best UX where available, but `bootstrap()` lets generic MCP
clients learn Exomem's search/save/upload contract without a separate skill file,
including the write loop for compiled notes.
See [docs/ai-assistant-guide.md](docs/ai-assistant-guide.md) for the copyable
standing instruction.

</details>

<details>
<summary>Docker (no Python on the host)</summary>

```bash
claude mcp add exomem -- docker run -i --rm -v "/path/to/vault:/vault" -e EXOMEM_VAULT_PATH=/vault ghcr.io/artexis10/exomem:latest --transport stdio
```

Use `:latest` for the lean keyword/BM25 image, `:ml` for CPU hybrid search, or
`:cuda` for NVIDIA/Linux CUDA capability. CUDA images still boot CPU-default at
idle; opt into GPU residency with `EXOMEM_MODE=performance` when you want it.
The image also runs as an always-on remote server via `docker compose` with a
tunnel sidecar — see [docs/docker.md](docs/docker.md). Windows users with a live
vault should usually prefer the native install (WSL2 bind mounts miss live
file-watch events); macOS Apple Silicon users need native install for MPS/MLX.

</details>

The first start downloads search models in the background — `find` works
immediately with keyword ranking and upgrades to semantic search automatically
once the models land. Run `exomem warm` to pre-download them ahead of time.

## Resource modes

Exomem is CPU-first by default so an idle server does not quietly occupy GPU
memory. Control the machine footprint without editing code:

```bash
exomem mode quiet          # low-resource: no heavy warm-up, evict caches, defer semantic reindex
exomem mode normal         # default: CPU steady-state, warm CPU caches allowed
exomem mode performance    # explicit opt-in for GPU-capable bulk/model work
exomem status --resources --json
```

Use `quiet` before gaming or other foreground workloads. Keyword/BM25 freshness,
file-change freshness, inbound links, and resolver state stay live; expensive
semantic/CLIP reindex work can be deferred and is reported in resource status.
Run `exomem index` or `kb reconcile` later to heal deferred semantic work.

## What it does

- **Searches the vault you already own.** Markdown stays in place; exomem does
  not import copies into a proprietary note store.
- **Adopts messy vaults safely.** `adopt` starts with a read-only report and
  explicit copy and compile-planning options, so originals remain archival until you choose.
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
[docs/comparison-engraph.md](docs/comparison-engraph.md). For the practical
boundary with chat products' own memory features, see
[docs/vs-built-in-memory.md](docs/vs-built-in-memory.md).

**Measured retrieval quality — and speed.** Retrieval is graded by a
reproducible golden-set eval harness, not asserted, and latency is measured
per lane at corpus scale: hybrid `find()` runs sub-second end-to-end at
50,000 notes (864 ms on the reference desktop, hot cache off), with the
keyword/BM25 lanes served from an FTS5 sidecar index in milliseconds —
built into stdlib SQLite, so it works on the lean install too. Methodology
and numbers in [docs/benchmarks.md](docs/benchmarks.md).

## Simple front door

Agents should route normal user requests through simple actions first, then use the typed tools underneath.

| Action | Use when the user says | Backed by |
| --- | --- | --- |
| Save | "remember this", "log this", "this is a decision" | `add`, `note`, `link`, `preserve` |
| Adopt/import | "make this old vault usable", "import my notes safely" | `adopt`, `overview` |
| Ask | "what do we know about X?", "show the sources" | `find`, `get` |
| Prove | "save this for the warranty case", "show the evidence" | `preserve`, upload/download, `find` |
| Review | "what needs cleanup?", "what is stale?" | `attention`, `audit`, `propose_compilation` |
| Update | "this replaced the old conclusion", "fix that note" | `edit`, `replace`, `reconcile` |
| Connect | "link this to X", "what should this cite?" | `link`, `suggest_links` |

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
| `adopt` | Existing-vault adoption: scan-only by default; can save a manifest, copy selected legacy text files as Sources, or return a compile plan while preserving originals. |

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

GPU acceleration is useful but not required. Steady-state torch models default to
CPU in `normal` and `quiet`; `performance` is the explicit opt-in for capable
NVIDIA CUDA or Apple Silicon MPS/Metal paths. See
[docs/deployment.md](docs/deployment.md) for CUDA, Blackwell, Apple Silicon,
diarization, and remote-service details.

## Configuration

The server reads environment variables or a `.env` file. The main ones are:

| Variable | Purpose |
| --- | --- |
| `EXOMEM_VAULT_PATH` | Vault root containing the governed folder (default `Knowledge Base/`). |
| `EXOMEM_KB_DIRNAME` | Name of the governed folder inside the vault (default `Knowledge Base`). |
| `EXOMEM_DISABLE_EMBEDDINGS` | `1` forces keyword/BM25-only search. |
| `EXOMEM_DISABLE_TIER2` | `1` hides Tier-2 filesystem tools. |
| `EXOMEM_REST_API_KEY` | Enables authenticated REST routes. |
| `EXOMEM_DISABLE_MEDIA_EXTRACTION` | `1` skips server-side OCR/ASR/PDF/Office extraction. |
| `EXOMEM_DISABLE_CLIP` | `1` disables CLIP image search. |
| `EXOMEM_MODE` | Hard-pin resource mode: `quiet`, `normal`, or `performance`. Env wins over config. |
| `EXOMEM_QUIET_MODE` | Legacy truthy alias for `quiet` when `EXOMEM_MODE` is unset. |
| `EXOMEM_AUTO_QUIET` | `1` enables optional non-torch GPU-pressure auto-quiet switching (default off). |
| `EXOMEM_DEVICE` / `EXOMEM_TORCH_DEVICE` | Force all torch models to `cuda`, `mps`, or `cpu`. Normally leave unset and use `exomem mode`. |
| `EXOMEM_MPS_FP16` | On Apple Silicon, run bge/CLIP in fp16 on the Metal GPU — ~half the memory, faster encodes (default on; set `0` to keep fp32). |
| `EXOMEM_VIDEO_SCENE_FRAMES` | Set to enable video scene detection + persisted, OCR'd scene-frame JPEGs (default off). |
| `EXOMEM_VIDEO_SCENE_THRESHOLD` | Scene-boundary hash threshold in bits of 64 (default 10). |
| `EXOMEM_VIDEO_SCENE_MIN_SECS` | Minimum scene duration in seconds; closer boundaries merge (default 4). |
| `EXOMEM_SEMANTIC_SEGMENTS` | Set to enable timed transcripts + semantic segment retrieval for audio/video (default off). |
| `EXOMEM_WHISPER_MODEL` | faster-whisper model size for ASR, such as `base` or `small`. |
| `EXOMEM_ASR_BACKEND` | ASR engine: `mlx` (Apple Silicon Metal GPU, needs the `media-mlx` extra) or `faster-whisper` (CUDA/CPU). Default auto-selects MLX on Apple Silicon, else faster-whisper. |
| `EXOMEM_MLX_WHISPER_MODEL` | HF repo for the MLX ASR model (default `mlx-community/whisper-large-v3-mlx`; use `mlx-community/whisper-large-v3-turbo` for speed). |
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
