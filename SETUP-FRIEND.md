# kb-mcp — local setup (Claude Code, no cloud)

This is the **friend-friendly** path: run kb-mcp as a **local MCP server inside
Claude Code**, pointed at your own Obsidian vault. No OAuth, no Tailscale, no
Windows service — none of the remote/mobile machinery in the main
[README](README.md). Everything stays on your machine; the only thing that ever
leaves is the query Claude sends to Anthropic to answer you.

If you're comfortable in Claude Code, this is ~20–30 minutes.

---

## What you need

- **Python 3.12+**
- **Claude Code** (you already have this)
- An **Obsidian vault** — or just any folder you want to use as one. It needs a
  `Knowledge Base/` subfolder (we create a minimal one below).
- Git, to clone the repo.

---

## 1. Install

```bash
git clone <repo-url> kb-mcp
cd kb-mcp
python -m venv .venv && . .venv/Scripts/activate   # Windows
# (macOS/Linux: source .venv/bin/activate)
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
  Instant, no model load, no GPU, works everywhere. The easiest start.
- **Hybrid** — install the extra (`pip install -e ".[embeddings]"`) and leave
  `KB_MCP_DISABLE_EMBEDDINGS` unset. Adds local vector embeddings + graph on top
  of BM25 — best recall on natural-language queries. Ideally an NVIDIA GPU; CPU
  works but embeds slowly.

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

## 6. Install + adapt the skill (so Claude knows *how* to use it)

The `_Schema/SKILL.md` is the operating manual Claude reads. Make Claude Code
load it as a skill — copy (or symlink) `_Schema/` to your skills folder:

```bash
# copy:
cp -r "/path/to/your/Obsidian/Knowledge Base/_Schema" ~/.claude/skills/knowledge-base
# or symlink (keeps them in sync; needs Developer Mode on Windows for non-admin):
# ln -s "/path/to/your/Obsidian/Knowledge Base/_Schema" ~/.claude/skills/knowledge-base
```

Then **make it yours** — the shipped `SKILL.md` / `project-keys.yaml` are a
**generic starter** (placeholder projects `personal` / `work`, no machine paths
or real tenants). Optionally:

- rename the **project keys** in `_Schema/project-keys.yaml` to your own — or
  just start writing; the writer auto-registers new keys as you use them.
- if you **symlinked** rather than copied, point rule 8's paths at your machine
  (skip this if you copied).

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
2. **A public HTTPS endpoint** — Tailscale Funnel (what Hugo uses) or Cloudflare
   Tunnel. This exposes the server to the internet, so auth becomes mandatory.
3. **A GitHub OAuth app** (client id + secret) wired into the OAuthProxy —
   claude.ai connectors require OAuth; static tokens aren't accepted.
4. **Lock it to your GitHub login** (the single-user verifier), then run it as a
   service with `--transport streamable-http`.
5. **Add it as a custom connector** in claude.ai.

The main [README](README.md) documents this path end-to-end (it's Hugo's exact
setup). Rule of thumb: the **local path is ~90% of the value for ~20% of the
effort**; the mobile tier is the rest — worth it if you genuinely want your KB
in your pocket.
