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
pip install -e .
```

> **Heads-up on the install size.** `torch` + `sentence-transformers` are core
> dependencies (they power hybrid semantic search) and pull in ~1–2 GB, with a
> CUDA build pinned for NVIDIA GPUs. If you have an NVIDIA GPU, great. If you're
> on a Mac / no GPU / want a light install, use **lean mode** below — search
> falls back to keyword/BM25 and you can skip the GPU story entirely. (Making
> the ML deps optional is a planned improvement; for now they install either
> way, you just don't *load* them in lean mode.)

---

## 2. Bootstrap your Knowledge Base

Your vault needs a `Knowledge Base/` folder with three things to start —
`index.md`, `log.md`, and the `_Schema/` contract. The writers create all the
sub-folders (`Sources/`, `Notes/…`, `Entities/…`) on demand as you use them.

```bash
# from your Obsidian vault root:
mkdir -p "Knowledge Base/_Schema"
```

**`Knowledge Base/index.md`** — paste:

```markdown
# Knowledge Base — Index

Compiled, structured layer of the vault. Governed by [[Knowledge Base/_Schema/SKILL]].

## Sections

- [[Knowledge Base/Sources/index|Sources]] — raw, immutable inputs
- [[Knowledge Base/Notes/index|Notes]] — compiled material
- [[Knowledge Base/Entities/index|Entities]] — typed nodes

## Recent activity

<!-- Auto-updated on every confirmed write. Most recent first. Cap: 50. -->

## Counts

<!-- Reconciled against disk. Run `reconcile` after setup to sync. -->

- Sources: 0
- Notes (insight): 0
- Entities (concept): 0
```

**`Knowledge Base/log.md`** — paste (the `---` separator is load-bearing; keep it):

```markdown
# Knowledge Base — Activity Log

Append-only chronological record of every confirmed write. Most recent first.

---
```

**`Knowledge Base/_Schema/`** — this is the contract (the discipline: page types,
frontmatter, supersession, audit rules). Copy a starter into it:

- Ask Hugo for his current `_Schema/` (recommended — it's the up-to-date one), **or**
- use the copy in this repo at `tests/fixtures/Knowledge Base/_Schema/` as a
  starting point.

Then **adapt it to you** (see step 6) — it references Hugo's projects and paths.

---

## 3. Point it at your vault

The server finds your vault via one env var — the folder that *contains*
`Knowledge Base/`:

```bash
export KB_MCP_VAULT_PATH="/path/to/your/Obsidian"   # the vault root, not the KB folder
```

---

## 4. (Choose) hybrid vs lean

- **Hybrid (default)** — local vector embeddings + BM25 + graph. Best search.
  Needs the torch deps (and ideally an NVIDIA GPU; CPU works but embedding is
  slow). Nothing extra to set.
- **Lean / keyword-only** — set `KB_MCP_DISABLE_EMBEDDINGS=1`. `find` uses
  BM25 (stemmed substring + ranking). Instant, no model load, no GPU. Great to
  start with; you can switch to hybrid later by unsetting it.

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

Then **adapt the contract to your world** — `SKILL.md` references Hugo's
projects (`q`, `endstate`, `tu`), tenants, and machine paths. Edit:

- the **project keys** (`_Schema/project-keys.yaml` + the project-scope section)
  to your own — though the writer auto-registers new keys as you use them, so
  you can also just start writing and let it grow.
- **rule 8** (the symlink section) — update the paths to your machine, or ignore
  it if you copied rather than symlinked.
- any **tenant / product-specific** sections that don't apply to you.

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
