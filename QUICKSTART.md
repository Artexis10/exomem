# exomem — local setup (Claude Code, no cloud)

`exomem setup --vault "/path/to/your/Obsidian"` does steps 1–6 below for you in
one command — see [Set it up in 5 minutes](README.md#set-it-up-in-5-minutes) in
the main README. This document is the full-control manual path: each step run
by hand, plus GPU/media extras, for anyone who wants to see or customize what's
happening under the hood.

This is the **local-first** path: run exomem as a **local MCP server inside
Claude Code**, pointed at your own Obsidian vault. No OAuth, no Tailscale, no
Windows service — none of the remote/mobile machinery in the main
[README](README.md). Everything stays on your machine; the only thing that ever
leaves is the query Claude sends to Anthropic to answer you.

**Works on macOS, Linux, and Windows.** The commands below use a macOS/Linux
shell; the few Windows (PowerShell) differences are called out inline.

If you're comfortable in Claude Code, this is ~20–30 minutes.

> **For Claude Code, exomem is two parts and you want both.** The **MCP server**
> (steps 1-5) is the *hands* — product commands such as `ask_memory`,
> `remember`, `capture_source`, `preserve_evidence`, and `review_memory`. The
> **skill** (step 6) is the *brain* — it tells Claude *when* to save, how to keep
> raw sources and evidence separate from compiled conclusions, when to suggest
> links, and how to update knowledge safely. Generic MCP clients that cannot load
> Skills should call `bootstrap()` once after connecting; that returns the compact
> operating contract through MCP. See [docs/ai-assistant-guide.md](docs/ai-assistant-guide.md)
> for Codex, hosted chat clients, Cursor/Windsurf/Gemini, and generic MCP setup.

---

## One command (recommended)

After step 1 below (clone + `uv sync`), everything else is a single command:

```bash
uv run python -m exomem setup
```

It prompts for your vault, **scans it and shows what's already there** —
existing notes are never touched; exomem writes only under `Knowledge Base/` —
then initializes the KB, picks lean vs hybrid, runs `doctor`, registers the
server with Claude Code (or prints the `.mcp.json` snippet if the `claude` CLI
isn't on PATH), installs the skill, and offers the optional hooks. Re-running
is safe: completed steps report `[skipped]`.

Non-interactive (scripts/CI):
`uv run python -m exomem setup --yes --vault "/path/to/vault" --lean`

## Not CLI-comfortable? Ask an assistant to drive

You do not need to understand Exomem internals to get started. Give Claude Code,
Codex, or another terminal-capable assistant this prompt from the cloned repo:

```text
Set up Exomem for my local markdown or Obsidian vault. Use the recommended lean
path unless I explicitly ask for embeddings. Run the setup wizard, explain each
prompt in plain language before I answer it, and do not move or rewrite my
existing vault files. After setup, verify with doctor, call
bootstrap(profile="compact") if you are not using the Exomem skill, ask one
known vault question with `ask_memory`, and
save one harmless test conclusion so I can see where it lands.
```

The expected first-run verification is concrete:

1. `exomem doctor` passes for the selected vault.
2. The connected assistant can call `bootstrap(profile="compact")`, unless it has
   already loaded the Exomem skill.
3. A known question about your vault triggers `ask_memory` before the answer.
4. A safe test such as "remember that setup succeeded today" writes a compiled
   note and reports `Saved -> <path>`.

Existing vault material remains read-only input. Exomem writes governed knowledge
under `Knowledge Base/` unless you deliberately configure a different governed
folder.

After setup, you can use simple action aliases instead of learning every
canonical operation first:

```bash
exomem ask "what do I know about this project?"
exomem ask "what supports this decision?" --deep --graph-enrich --json
exomem remember "# Setup succeeded\n\n## Claim\n\nExomem setup completed today." --title "Setup succeeded today" --json
exomem capture "raw source text" --title "Source title" --source-type other --json
exomem capture "receipt text" --as evidence --scope warranty --category receipts --filename receipt.txt --json
exomem review --json
exomem connect --path "Notes/Insights/example" --json
exomem maintain --json
```

These aliases are thin routes over product commands: `ask` -> `ask_memory`,
`remember` -> `remember`, `capture` -> `capture_source`/`preserve_evidence`,
`review` -> `review_memory`, `connect` -> `connect_memory`, and `maintain` ->
`maintain_memory` unless a fix flag is explicit. The canonical implementation
leaves remain underneath for governance and validation, but new users do not need
to learn them first.

The numbered steps below are the **manual path** — exactly what `setup` does
under the hood, kept for troubleshooting and for people who prefer explicit
steps.

---

## Already have a vault full of notes?

That's the normal case, and it's safe:

- **Your existing files are never touched.** All writes go under
  `Knowledge Base/` — a new folder that `setup`/`init` creates *next to* your
  existing ones. Everything else in the vault is read-only input; `init`
  refuses to run if `Knowledge Base/` already exists, so re-running can't
  clobber anything. *(The governed folder is named `Knowledge Base/` by default;
  set `EXOMEM_KB_DIRNAME` to govern a differently-named folder — e.g. to adopt one
  your vault already uses.)*
- **Your notes stay searchable.** `ask_memory` reaches sibling folders when you
  use `scope="vault"`; `browse_memory` gives Claude a bounded structural report
  of the whole vault, and `adopt_vault` turns that scan into a safe adoption
  report with likely knowledge packs and manifest/copy/compile-planning next
  actions — one call, not one read per file.
- **Daily-notes vaults** (a `Daily/` or `Journal/` tree of dated logs): leave
  them exactly as they are. The Knowledge Base is a *compiled* layer beside
  your log, not a migration target — exomem never requires frontmatter, links,
  or restructuring from existing notes. Good first prompts after setup:
  *"what does this vault look like?"* — Claude answers with `overview`; or
  *"adopt this vault safely"* — Claude answers with `adopt`, preserving originals
  and proposing manifest/copy/compile-planning next actions for you to approve.
- **Same vault or a separate one?** Same vault is the default: notes and KB in
  one Obsidian window, cross-search included. Pick a separate vault only when
  you want hard isolation (e.g. a shared or team-synced vault where a new
  top-level folder would bother other tooling).

Concrete adoption commands after setup:

```bash
# readable read-only report: no files created, moved, edited, or deleted
exomem adopt

# same report as a machine-readable envelope
exomem adopt --json

# save the report under Knowledge Base/_Adoption/
exomem adopt --mode save-manifest --json

# copy only files you explicitly name into governed Sources/Imported/
exomem adopt --mode copy-as-sources \
  --selected-paths "Warranty Case/laptop-receipt.md" \
  --json

# copy selected legacy text if needed, then return a compile plan;
# no compiled note is created until you review and call note()
exomem adopt --mode compile-selected \
  --selected-paths "Warranty Case/laptop-receipt.md" \
  --json
```

See [docs/product-model.md](docs/product-model.md) for the simple action model
and [docs/knowledge-packs.md](docs/knowledge-packs.md) for the pack schema.

---

## What you need

- **Python 3.11+** — check with `python3 --version`. On macOS, `brew install
  python` if you don't have it (or use the [python.org](https://www.python.org/downloads/) installer).
- **uv** — the documented install path uses the repo lockfile. Install from
  <https://docs.astral.sh/uv/> if `uv --version` fails.
- **Claude Code** (you already have this)
- An **Obsidian vault** — or just any folder you want to use as one. It needs a
  `Knowledge Base/` subfolder (we create a minimal one below).
- Git, to clone the repo.

---

## 1. Install

```bash
git clone <repo-url> exomem
cd exomem
uv sync                         # lean: keyword/BM25 search, no heavy deps
# for hybrid semantic search, add the extra (~1-2 GB torch + sentence-transformers):
# uv sync --extra embeddings
```

> **Lean by default.** `uv sync` is the light path — search runs on
> keyword/BM25, no torch, works everywhere. For hybrid semantic search (better
> recall on natural-language queries), install the extra: `uv sync --extra
> embeddings` — a ~1-2 GB torch download. It's **GPU-accelerated on both NVIDIA
> (CUDA) and Apple Silicon (Metal/MPS)**, auto-detected; CPU-only boxes work too,
> just slower. Start lean; upgrade anytime by installing the extra and unsetting
> `EXOMEM_DISABLE_EMBEDDINGS`. **On a Mac, see "Apple Silicon" in step 4 below.**

If you already manage Python environments yourself, `pip install -e .` still
works as a fallback; the `uv` path is preferred because it honors `uv.lock` and
the repo's configured PyTorch wheel source.

Before touching your own vault, you can verify the repo against the public sample
vault:

```bash
uv run exomem demo --json
```

That read-only smoke runs the path a new install depends on: `doctor`, keyword
`find`, `get`, and `audit`.

---

## 2. Bootstrap your Knowledge Base

One command lays down the whole structure — `index.md`, `log.md`, the `_Schema/`
contract, and the typed `Sources/ Notes/{…} Entities/{…} Evidence/` tree — into
your vault:

```bash
uv run python -m exomem init --vault "/path/to/your/Obsidian"
```

It refuses if a `Knowledge Base/` already exists, so it won't clobber anything.
The shipped `_Schema/` is a **genericized starter contract** — adapt
`Knowledge Base/_Schema/project-keys.yaml` to your own projects (or just start
writing; the writer auto-registers new project keys as you go).

---

## 3. Point it at your vault

The server finds your vault via one env var — the folder that *contains*
`Knowledge Base/`:

```bash
export EXOMEM_VAULT_PATH="/path/to/your/Obsidian"   # the vault root, not the KB folder
```

---

## 4. (Choose) hybrid vs lean

- **Lean / keyword-only (default install)** — `uv sync` + set
  `EXOMEM_DISABLE_EMBEDDINGS=1`. `find` uses BM25 (stemmed substring + ranking).
  Instant, no model load, no GPU, works everywhere. The easiest start. Note it's
  **silent about it** — there's no error when embeddings are off, so "search
  works" on a lean install means keyword/BM25, *not* semantic.
- **Hybrid** — install the extra (`uv sync --extra embeddings`) and leave
  `EXOMEM_DISABLE_EMBEDDINGS` unset. Adds local vector embeddings + graph on top
  of BM25 — best recall on natural-language queries. GPU-accelerated on NVIDIA
  (CUDA) or Apple Silicon (Metal/MPS), auto-detected; CPU works but embeds
  slowly. The vector index builds as you write (each note is embedded on save);
  to backfill an existing vault, run `kb reconcile` after installing the extra.
  Quick check that semantic is live: ask for something using words that *don't*
  appear in the note — if it still surfaces, embeddings are on.

**Apple Silicon (Mac) — full GPU acceleration.** On an M-series Mac exomem uses
the **Metal GPU automatically** — no config. bge/CLIP embeddings run on MPS (in
fp16), and audio/video transcription runs on Metal via `mlx-whisper` once you add
its extra:

```bash
uv sync --extra embeddings --extra media --extra media-mlx
```

`select_device()` picks MPS and `get_transcriber()` picks MLX on Apple Silicon on
their own. Confirm the accelerated stack is live:

```bash
uv run python scripts/verify-mlx.py     # expect: GATE: PASS
uv run exomem doctor --profile media    # torch.device: mps, asr.backend: mlx-whisper
```

Optional knobs: `EXOMEM_MPS_FP16=0` keeps embeddings in fp32; `EXOMEM_TORCH_DEVICE=cpu`
forces CPU (e.g. to avoid thermal throttling on a fanless Air during a big backfill);
`EXOMEM_MLX_WHISPER_MODEL=mlx-community/whisper-large-v3-turbo` picks a lighter ASR model.

Preflight the selected path before wiring Claude:

```bash
uv run python -m exomem doctor --vault "/path/to/your/Obsidian" --profile lean
# or, after installing embeddings:
uv run python -m exomem doctor --vault "/path/to/your/Obsidian" --profile hybrid
```

### Resource modes

The safe default is `normal`: no automatic CUDA residency, no startup model or
whole-vault cache preload, and idle reclamation enabled. For gaming or other
foreground work, quiet additionally defers expensive indexing:

```bash
uv run python -m exomem mode quiet
uv run python -m exomem status --resources --json
```

Quiet skips heavy startup warm-up, makes large caches evictable, and defers
expensive semantic/CLIP indexing while keeping cheap freshness/inbound/resolver
updates live. After foreground work, return to `normal` or run an explicit heal:

```bash
uv run python -m exomem mode normal
uv run python -m exomem index --vault "/path/to/your/Obsidian"
uv run python -m exomem reconcile --vault "/path/to/your/Obsidian"
```

Use `performance` only when you explicitly want GPU-capable bulk/model work:

```bash
uv run python -m exomem mode performance
```

---

## 5. Add it to Claude Code

Easiest — the CLI (run from anywhere):

```bash
claude mcp add exomem \
  --env EXOMEM_VAULT_PATH="/path/to/your/Obsidian" \
  --env EXOMEM_DISABLE_EMBEDDINGS=1 \
  -- uv --directory "$PWD" run python -m exomem --transport stdio
```

(Drop the `EXOMEM_DISABLE_EMBEDDINGS` line for hybrid. If you use the pip
fallback instead of uv, use the **full path to your venv's `python`**.)

Or by hand in `.mcp.json` (project) / your Claude Code settings:

```json
{
  "mcpServers": {
    "exomem": {
      "command": "uv",
      "args": ["--directory", "/path/to/exomem", "run", "python", "-m", "exomem", "--transport", "stdio"],
      "env": {
        "EXOMEM_VAULT_PATH": "/path/to/your/Obsidian",
        "EXOMEM_DISABLE_EMBEDDINGS": "1"
      }
    }
  }
}
```

Restart Claude Code; you should see the product `exomem` tools (`bootstrap`,
`ask_memory`, `read_memory`, `remember`, `capture_source`, `review_memory`,
`maintain_memory`, …). Quick test before wiring: `uv run python -m exomem
--transport stdio` should start and wait on stdin without error.

---

## 6. Install the skill (Claude Code best UX)

The server gives Claude the tools; **the Exomem Knowledge Base skill is what
makes Claude Code use them well** — capture at natural stopping points, file
sources to the right folder, and compile notes under the schema. One command
installs it straight from the repo into Claude Code's skills folder (no vault
path needed — it ships in the package):

```bash
uv run python -m exomem install-skill
```

That writes the core skill to `~/.claude/skills/exomem/` and installs the named
workflow skills beside it, such as `exomem-continue`, `exomem-capture`,
`exomem-research`, `exomem-review`, and `exomem-media`. The workflow skills are
short operating loops for common user intents; the core `exomem` skill remains
the contract that preserves Sources, Evidence, compiled notes, review, and
supersession. See [docs/workflow-skills.md](docs/workflow-skills.md).

The core skill uses the same name as the Exomem connector and tools, so it all
reads as one product. (If you installed before the rename, this also retires the
old `knowledge-base` skill folder.) **Restart Claude Code** so it loads. Useful flags: `--link`
symlinks instead of copying so it
tracks repo updates as you `git pull` (falls back to a copy if your OS refuses
the symlink); `--force` overwrites an existing install; `--target` picks a
different folder.

Then **make it yours** — the shipped `SKILL.md` / `project-keys.yaml` are a
**generic starter** (placeholder projects `personal` / `work`, no machine paths
or real tenants). Optionally adapt the **project keys** in your vault's
`Knowledge Base/_Schema/project-keys.yaml` (the copy the *server* reads) to your
own — or just start writing; the writer auto-registers new keys as you use them.

> **What "auto-capture" does and doesn't do.** With the skill loaded, Claude
> captures on its own *inside a conversation* when it judges you've hit a
> stepping-stone — a decision, a solved problem, a recognized pattern. There's
> **no background daemon**: it won't save things while you're away from the chat,
> and a fresh thread starts fresh. You can always just say *"save that to kb."*
>
> **Other MCP clients.** ChatGPT, Codex, Cursor, Gemini, Windsurf, or any client
> without Skill support should call `bootstrap()` once after connecting. It returns
> Exomem's search/save/upload defaults, the compiled-note write loop, and
> performance guidance in a structured MCP response. The copyable standing instruction lives in
> [docs/ai-assistant-guide.md](docs/ai-assistant-guide.md).

---

## 7. (Recommended) Make the KB automatic — both directions

The skill *tells* Claude to capture at stepping-stones and to consult the KB
before answering, but those instructions are passive — over a long conversation
Claude tends to forget them, so auto-save quietly never fires (you'll know:
`Knowledge Base/log.md` only shows saves you asked for) and prior notes don't get
pulled in. This command installs those two reliability nudges plus a local
continuation checkpoint for compaction and resume:

```bash
uv run python -m exomem install-hook
```

Codex CLI can use the same bundled Exomem hook scripts:

```bash
uv run python -m exomem install-hook --client codex
```

If you maintain alternate Claude settings files with yadm, wire each active
variant too, for example `--settings ~/.claude/settings.json##os.Msys`.

- **Write** — a `Stop` hook that re-checks "is this worth saving?" at the end of
  each turn, so conclusions get captured on their own.
- **Read** — a `UserPromptSubmit` hook that reminds Claude to run `ask_memory`
  first when your message touches something the KB might hold, so it actually
  behaves as your source of truth.
- **Continue** — one shared, stdlib-only checkpoint core with thin Claude and
  Codex adapters. It records bounded structural evidence before compaction and
  reinjects it after compact/resume, even when MCP is disconnected, OAuth needs
  repair, or no vault is configured. It never calls a model, MCP, REST, or the
  Exomem CLI on the compaction path.

| Lifecycle event | Claude Code | Codex CLI 0.144.3 |
| --- | --- | --- |
| `PreCompact(manual|auto)` | write checkpoint | write checkpoint |
| `SessionEnd` | write checkpoint | unsupported; not registered |
| `SessionStart(compact|resume)` | validate and reinject | validate and reinject |

The checkpoint is deliberately structural-only. It can contain repository and
worktree identity, HEAD/dirty status, bounded file paths, artifact hashes and
checkbox counts/line numbers, plus a bound hash of a transcript byte slice. It
does **not** parse or store conversation messages, tool/system output,
compaction summaries, secrets, or artifact/task text. Reinjected context points
the agent at files to reopen and advises it to capture durable conclusions later;
it never writes checkpoint or transcript data into Exomem automatically.

The two nudge hooks are cheap: gated so they stay quiet on ordinary turns/prompts, plus a
per-session cooldown. The read hook is still language-agnostic for substantive
prompts, but it also suppresses obvious short control/status prompts such as
`continue`, `thanks`, `are you done?`, `merge it`, and `restart the server` so
the reminder does not churn through routine command flow. They write scripts to
`~/.claude/hooks/` and wire the two hooks into your settings.json — restart Claude
Code to activate. Triggers log to `~/.claude/exomem-capture-nudge.log` and
`~/.claude/exomem-retrieve-nudge.log` so you can see the real rate. For Codex,
the same scripts go to `~/.codex/hooks/`, wire into `~/.codex/hooks.json`, and
log under `~/.codex/`. Prefer to wire it by hand?
`uv run python -m exomem install-hook --print-only` writes the scripts and prints
the snippet to paste.

The read hook is a nudge, not a forced retrieval policy. The agent should still
skip Exomem for unrelated chat, tiny control prompts, or follow-ups where the
current conversation already contains the needed KB evidence.

To verify the deployed hooks without changing anything:

```bash
uv run python -m exomem install-hook --check
```

That checks both Claude Code and Codex by default. Use `--client claude` or
`--client codex` to narrow it. The report flags stale deployed hook copies,
legacy `kb_*` hook entries, missing config, and the log/cache paths where hook
activity should land. Before the first lifecycle event, missing checkpoint state
is a healthy warning. Continuation state and metadata-only event logs live under
`~/.claude/.cache/exomem-continuation/` or
`~/.codex/.cache/exomem-continuation/`; `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, and
the test/isolation override `EXOMEM_HOOK_HOME` move scripts, config, state, and
logs together. Checkpoints are fresh for 30 days; expired non-current sessions
are pruned without following symlinks or deleting a live writer's state.

Set `EXOMEM_CONTINUATION_DISABLE=1` before launching the client to bypass all
continuation writes and reinjection without deleting existing state. After
installation or migration, restart/reload that client and approve/trust the
hook if it asks. Re-running `install-hook` migrates only exact legacy
`kb_continuation_checkpoint.py` / `kb-continuation-checkpoint.sh` entries,
preserves unrelated hooks (including any user-owned Codex `SessionEnd`), backs
up changed valid config, and is otherwise idempotent.

There is intentionally no uninstall command. To roll back manually, remove only
the Exomem continuation groups whose commands name
`exomem_continuation_checkpoint.py` or
`exomem-continuation-checkpoint.sh` and pass the matching explicit
`--client`; remove those two deployed files if no group uses them, then reload
the client. Existing checkpoint state is inert and may be deleted separately.

Tune with `EXOMEM_CAPTURE_NUDGE_MIN_CHARS` / `EXOMEM_RETRIEVE_NUDGE_MIN_CHARS` (and the
matching `_COOLDOWN_SEC`), `EXOMEM_RETRIEVE_NUDGE_CONTROL_MAX_CHARS` for the read
hook's control-prompt skip ceiling, or disable either with `EXOMEM_CAPTURE_NUDGE_DISABLE=1` /
`EXOMEM_RETRIEVE_NUDGE_DISABLE=1`. **Writing in a dense script (Japanese, Chinese)?**
Lower the `MIN_CHARS` values — those scripts pack more meaning per character, so
the defaults (tuned for English) can under-fire. (These tunables were renamed from
`KB_*` to `EXOMEM_*`; the old `KB_*` names are still accepted for back-compat.)

**Opt-in: upgrade the read-side reminder to real retrieved content.** By default
the `UserPromptSubmit` hook only reminds Claude to run `ask_memory` — set
`EXOMEM_RETRIEVE_INJECT=1` and it instead fetches the top 3 compact routing stubs
(keyword mode, no embeddings) for the same gated prompt and appends them to the
reminder, so relevant prior KB pages are already in context before Claude
decides whether to search. It tries a short transport ladder and never blocks
on a slow path: REST first (one `POST /api/ask_memory`, ~2s timeout) — only
attempted when `EXOMEM_REST_API_KEY` is set **in the shell that launches the
client** (not just the server's service environment — the hook can't read another
process's env, so export it in the same profile Claude Code or Codex inherits from);
then, only if you also set `EXOMEM_RETRIEVE_INJECT_CLI=1`, an `exomem ask_memory --json`
subprocess call (~5s timeout, slower — cold Python start). If neither is
configured or reachable, it falls straight back to the plain reminder — no
network call is ever attempted unless `EXOMEM_RETRIEVE_INJECT` is on. (The legacy
`KB_RETRIEVE_INJECT` / `KB_RETRIEVE_INJECT_CLI` names still work too.)

(Hooks are local-client only — claude.ai web/mobile can't run them, so there the
skill or `bootstrap()` contract stays best-effort: nudge it with *"save that to
kb"* or *"check the kb."*)

---

## You're up

Try it in Claude Code: *"capture this source and compile an insight from it,"*
or *"ask Exomem what I know about X,"* or *"review the KB."* Then run
**`exomem maintain --reconcile`** once to sync index counts after manual edits.

## Optional: mobile / claude.ai-web access

Want the on-the-go experience — querying your KB from **Claude
mobile**? Same engine, just the remote tier. It's not *hard*, but it's
genuinely more than the local path, because a phone needs an always-on,
publicly-reachable, authenticated endpoint:

1. **An always-on host** — your desktop running 24/7, or a cheap VPS. (Local
   stdio dies when you close Claude Code; mobile needs it always up.)
2. **A public HTTPS endpoint** — **Tailscale Funnel** (no domain needed — gives a
   free `*.ts.net` host; the simplest path if you don't own a domain) or
   **Cloudflare Tunnel** (needs a domain you own in Cloudflare; more burst-tolerant
   under heavy use). This exposes the server to the internet, so auth
   becomes mandatory.
3. **A GitHub OAuth app** (client id + secret) wired into the OAuthProxy —
   claude.ai connectors require OAuth; static tokens aren't accepted.
4. **Lock it to your GitHub login** (the single-user verifier), then run it as a
   background service with `--transport streamable-http`. On macOS or Linux run
   `bash scripts/install-service.sh --release`; on Windows run
   `pwsh -File scripts/install-service.ps1 -Release`. These default to the
   resource-bounded standard multimodal profile,
   release commands create the PyPI service environment, load `.env`, doctor-gate,
   start the OS service, and verify `/mcp` (the main
   [docs/deployment.md](docs/deployment.md) covers all options, including repo-dev).
5. **Add it as a custom connector** in claude.ai.

[docs/deployment.md](docs/deployment.md) documents this path end-to-end. Rule of thumb: the **local path is ~90% of the value for ~20% of the
effort**; the mobile tier is the rest — worth it if you genuinely want your KB
in your pocket.

### Make the KB proactive in the Claude app (custom instructions)

claude.ai (web/mobile) can't run hooks — those are Claude Code only — so the
skill's proactive find/capture is best-effort there. This vault is served by
Exomem, an MCP server (formerly kb-mcp). Once added as a connector, its tools
appear under whatever name you assign it (e.g. "Knowledge Base"). To nudge it
reliably across *all* your chats, paste this into the Claude app at
**Settings → Profile → "What personal preferences should Claude consider in
responses?"**:

```
Precise and non-performative: no hype, fluff, or motivational tone; clarity and correctness over filler. Use lists/structure only when they genuinely help; plain prose is fine. Match length to the substance, terse when simple and fuller when it's not.

I keep a personal Knowledge Base served by the Exomem MCP. If no Exomem skill is loaded, call bootstrap(profile="compact") once at the start of a new chat and follow it. Use Exomem proactively: search first when a turn touches my projects, notes, decisions, or domains (cite what you find; an empty search is a gap, not a dead end). Do not search on unrelated chit-chat, small control prompts, or follow-ups where the current conversation already has the needed KB evidence. Capture durable conclusions on your own — a decision, solved problem, diagnosed failure, or recognized pattern — as a short compiled note, not a transcript, then report one line: "Saved -> <path>". Ask before saving only if type/scope is genuinely ambiguous. Stay quiet on chit-chat; don't narrate empty searches.
```

The first paragraph is general response style (trim to taste); the second is the KB
nudge. Account-level custom instructions are always in context, so they make Claude
reach for the connected KB on its own — the app-side equivalent of the Claude Code
hooks. The "do not search" line keeps it from firing on unrelated or already
answered turns. For more client-specific instructions, see
[docs/ai-assistant-guide.md](docs/ai-assistant-guide.md).
