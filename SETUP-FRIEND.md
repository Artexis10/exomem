# kb-mcp — local setup (Claude Code, no cloud)

This is the **friend-friendly** path: run kb-mcp as a **local MCP server inside
Claude Code**, pointed at your own Obsidian vault. No OAuth, no Tailscale, no
Windows service — none of the remote/mobile machinery in the main
[README](README.md). Everything stays on your machine; the only thing that ever
leaves is the query Claude sends to Anthropic to answer you.

**Works on macOS, Linux, and Windows.** The commands below use a macOS/Linux
shell; the few Windows (PowerShell) differences are called out inline.

If you're comfortable in Claude Code, this is ~20–30 minutes.

> **kb-mcp is two parts, and you need both.** The **MCP server** (steps 1–5) is
> the *hands* — the `find`/`add`/`note` tools. The **skill** (step 6) is the
> *brain* — it's what tells Claude *when* to save, how to file a source, and how
> to compile a note. Install the server but skip the skill and Claude has the
> tools but no idea it's meant to use them: it sits silent and feels broken.
> **This is the #1 "it does nothing" trap — don't skip step 6.**

---

## What you need

- **Python 3.11+** — check with `python3 --version`. On macOS, `brew install
  python` if you don't have it (or use the [python.org](https://www.python.org/downloads/) installer).
- **Claude Code** (you already have this)
- An **Obsidian vault** — or just any folder you want to use as one. It needs a
  `Knowledge Base/` subfolder (we create a minimal one below).
- Git, to clone the repo.

---

## 1. Install

```bash
git clone <repo-url> kb-mcp
cd kb-mcp
python3 -m venv .venv && source .venv/bin/activate   # macOS/Linux
# (Windows PowerShell: python -m venv .venv ; .venv\Scripts\Activate.ps1)
pip install -e .                 # lean: keyword/BM25 search, no heavy deps
# for hybrid semantic search, add the extra (~1-2 GB torch + sentence-transformers):
# pip install -e ".[embeddings]"
```

> **Lean by default.** `pip install -e .` is the light path — search runs on
> keyword/BM25, no torch, no GPU, works everywhere (incl. Mac / no-GPU). For
> hybrid semantic search (better recall on natural-language queries), install
> the extra: `pip install -e ".[embeddings]"` — that's the ~1-2 GB torch
> download (CUDA build, best on an NVIDIA GPU; CPU works but embeds slowly).
> Start lean; upgrade anytime by installing the extra and unsetting
> `KB_MCP_DISABLE_EMBEDDINGS`.

---

## 2. Bootstrap your Knowledge Base

One command lays down the whole structure — `index.md`, `log.md`, the `_Schema/`
contract, and the typed `Sources/ Notes/{…} Entities/{…} Evidence/` tree — into
your vault:

```bash
python -m kb_mcp init --vault "/path/to/your/Obsidian"
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
export KB_MCP_VAULT_PATH="/path/to/your/Obsidian"   # the vault root, not the KB folder
```

---

## 4. (Choose) hybrid vs lean

- **Lean / keyword-only (default install)** — `pip install -e .` + set
  `KB_MCP_DISABLE_EMBEDDINGS=1`. `find` uses BM25 (stemmed substring + ranking).
  Instant, no model load, no GPU, works everywhere. The easiest start. Note it's
  **silent about it** — there's no error when embeddings are off, so "search
  works" on a lean install means keyword/BM25, *not* semantic.
- **Hybrid** — install the extra (`pip install -e ".[embeddings]"`) and leave
  `KB_MCP_DISABLE_EMBEDDINGS` unset. Adds local vector embeddings + graph on top
  of BM25 — best recall on natural-language queries. Ideally an NVIDIA GPU; CPU
  works but embeds slowly. The vector index builds as you write (each note is
  embedded on save); to backfill an existing vault, run `audit_fix` with
  `rebuild_embeddings=True`. Quick check that semantic is live: ask for something
  using words that *don't* appear in the note — if it still surfaces, embeddings
  are on.

---

## 5. Add it to Claude Code

Easiest — the CLI (run from anywhere):

```bash
claude mcp add kb-mcp \
  --env KB_MCP_VAULT_PATH="/path/to/your/Obsidian" \
  --env KB_MCP_DISABLE_EMBEDDINGS=1 \
  -- python -m kb_mcp --transport stdio
```

(Drop the `KB_MCP_DISABLE_EMBEDDINGS` line for hybrid. Use the **full path to
your venv's `python`** if `python` on PATH isn't the venv one.)

Or by hand in `.mcp.json` (project) / your Claude Code settings:

```json
{
  "mcpServers": {
    "kb-mcp": {
      "command": "python",
      "args": ["-m", "kb_mcp", "--transport", "stdio"],
      "env": {
        "KB_MCP_VAULT_PATH": "/path/to/your/Obsidian",
        "KB_MCP_DISABLE_EMBEDDINGS": "1"
      }
    }
  }
}
```

Restart Claude Code; you should see the `kb-mcp` tools (`find`, `note`, `add`,
`audit`, `reconcile`, …). Quick test before wiring: `python -m kb_mcp
--transport stdio` should start and wait on stdin without error.

---

## 6. Install the skill (REQUIRED — this is the brain)

The server gives Claude the tools; **the skill is what makes Claude actually use
them** — capture at natural stopping points, file sources to the right folder,
compile notes under the schema. One command installs it straight from the repo
into Claude Code's skills folder (no vault path needed — it ships in the package):

```bash
python -m kb_mcp install-skill
```

That writes the skill to `~/.claude/skills/knowledge-base/`. **Restart Claude
Code** so it loads. Useful flags: `--link` symlinks instead of copying so it
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

---

## 7. (Recommended) Make the KB automatic — both directions

The skill *tells* Claude to capture at stepping-stones and to consult the KB
before answering, but those instructions are passive — over a long conversation
Claude tends to forget them, so auto-save quietly never fires (you'll know:
`Knowledge Base/log.md` only shows saves you asked for) and prior notes don't get
pulled in. This one command installs two small hooks that fix both directions:

```bash
python -m kb_mcp install-hook
```

- **Write** — a `Stop` hook that re-checks "is this worth saving?" at the end of
  each turn, so conclusions get captured on their own.
- **Read** — a `UserPromptSubmit` hook that reminds Claude to run a `find` first
  when your message touches something the KB might hold, so it actually behaves as
  your source of truth.

Both are **language-agnostic** (no keyword matching — they work the same in any
language you write in) and cheap (gated so they stay quiet on ordinary
turns/prompts, plus a per-session cooldown). They write scripts to
`~/.claude/hooks/` and wire the two hooks into your settings.json — restart Claude
Code to activate. Triggers log to `~/.claude/kb-capture-nudge.log` and
`~/.claude/kb-retrieve-nudge.log` so you can see the real rate. Prefer to wire it
by hand? `python -m kb_mcp install-hook --print-only` writes the scripts and
prints the snippet to paste.

Tune with `KB_CAPTURE_NUDGE_MIN_CHARS` / `KB_RETRIEVE_NUDGE_MIN_CHARS` (and the
matching `_COOLDOWN_SEC`), or disable either with `KB_CAPTURE_NUDGE_DISABLE=1` /
`KB_RETRIEVE_NUDGE_DISABLE=1`. **Writing in a dense script (Japanese, Chinese)?**
Lower the `MIN_CHARS` values — those scripts pack more meaning per character, so
the defaults (tuned for English) can under-fire.

(Hooks are Claude Code only — claude.ai web/mobile can't run them, so there the
skill stays best-effort: nudge it with *"save that to kb"* or *"check the kb."*)

---

## You're up

Try it in Claude Code: *"add this as a source and compile an insight from it,"*
or *"find my notes on X,"* or *"audit the KB."* Then run **`reconcile`** once to
sync the index counts after your manual `index.md` edit.

## Optional: mobile / claude.ai-web access

Want the on-the-go experience Hugo has — querying your KB from **Claude
mobile**? Same engine, just the remote tier. It's not *hard*, but it's
genuinely more than the local path, because a phone needs an always-on,
publicly-reachable, authenticated endpoint:

1. **An always-on host** — your desktop running 24/7, or a cheap VPS. (Local
   stdio dies when you close Claude Code; mobile needs it always up.)
2. **A public HTTPS endpoint** — **Tailscale Funnel** (no domain needed — gives a
   free `*.ts.net` host; the simplest path if you don't own a domain) or
   **Cloudflare Tunnel** (needs a domain you own in Cloudflare; more burst-tolerant
   under heavy use — Hugo's setup). This exposes the server to the internet, so auth
   becomes mandatory.
3. **A GitHub OAuth app** (client id + secret) wired into the OAuthProxy —
   claude.ai connectors require OAuth; static tokens aren't accepted.
4. **Lock it to your GitHub login** (the single-user verifier), then run it as a
   background service with `--transport streamable-http` — `scripts/install-service.sh`
   sets this up via **launchd** on macOS or **systemd** on Linux (the main
   [README](README.md) covers all three platforms).
5. **Add it as a custom connector** in claude.ai.

The main [README](README.md) documents this path end-to-end (it's Hugo's exact
setup). Rule of thumb: the **local path is ~90% of the value for ~20% of the
effort**; the mobile tier is the rest — worth it if you genuinely want your KB
in your pocket.

### Make the KB proactive in the Claude app (custom instructions)

claude.ai (web/mobile) can't run hooks — those are Claude Code only — so the
skill's proactive find/capture is best-effort there. To nudge it reliably across
*all* your chats, paste this into the Claude app at **Settings → Profile → "What
personal preferences should Claude consider in responses?"**:

```
Precise and non-performative: no hype, fluff, or motivational tone; clarity and correctness over filler. Use lists/structure only when they genuinely help; plain prose is fine. Match length to the substance, terse when simple and fuller when it's not.

I keep a personal Knowledge Base, connected as the "Knowledge Base" connector (kb-mcp). Use it proactively: search it first when a turn touches my projects, notes, decisions, or domains (cite what you find; an empty search means a gap, not a dead end). Capture durable conclusions on your own (a decision, solved problem, diagnosed failure, or recognized pattern) as a short compiled note, not a transcript, whether or not the topic exists yet, then report one line: "Saved -> <path>". Ask before saving only if type/scope is genuinely ambiguous. Stay quiet on chit-chat and don't narrate empty searches.
```

The first paragraph is general response style (trim to taste); the second is the KB
nudge. Account-level custom instructions are always in context, so they make Claude
reach for the connected KB on its own — the app-side equivalent of the Claude Code
hooks. The "stay quiet on chit-chat" line keeps it from firing on unrelated chats.
