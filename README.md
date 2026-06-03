# kb-mcp

Self-hosted Model Context Protocol server exposing Hugo's Obsidian
Knowledge Base to **mobile claude.ai** as a remote custom connector.

> **Just want to run it locally in Claude Code over your own vault (no cloud,
> no OAuth)?** See **[SETUP-FRIEND.md](SETUP-FRIEND.md)** — the ~20-minute local
> path. This README covers the heavier remote/mobile deployment.

Tools surfaced (full parity with the desk-side KB skill except `schema`):

**Tier 1 — type-routed (primary).** Use these whenever a Tier 1 op fits.
The routing encodes the discipline.

- `find` — read-only search across `Knowledge Base/`, type/project/tag filtered
- `get` — read a full file anywhere under the vault root (incl. `Cognitive
  Core/`, `Domains/`, `Prompt Bank/`, `Products/`, `Personal Context/`).
  `frontmatter_only=true` returns just the frontmatter.
- `add` — capture a raw `source` page with full SKILL.md rule-7 writes
- `note` — create any of the six compiled page types (research-note,
  insight, failure, pattern, experiment, production-log) with rule-7
  writes + `ingested_into:` back-refs on cited sources
- `link` — create a typed entity under `Entities/<Type>/<Name>.md`
  (person, concept, library, decision)
- `edit` — in-place edit of a compiled page. Modes: body / tags / surgical
  `old_string`→`new_string`; `edits=[…]` (batch surgical); `row_key`+`take`
  (fill a `[take: ]` opinion row); `field`+`value` (patch one frontmatter
  field). Bumps `updated:`. Use `replace` for substantial rewrites.
- `replace` — supersession: write a new page + flip the old one to
  `status: superseded` with `superseded_by:` back-link. The modify path
  for substantial rewrites.
- `preserve` — capture binary or text artifact to
  `Evidence/<scope>/<category>/` (append-only)
- `audit` — read-only graph health check (broken wikilinks, orphan
  entities, unprocessed sources, index/log drift, tag inconsistency)

**Tier 2 — filesystem-parity (escape hatches).** Use when Tier 1 can't
express what you need: new folder structures (`Identity/`, `Templates/`),
files outside the typed-note set, or surgical edits.

> **Lean surface (`KB_MCP_DISABLE_TIER2`).** Set `KB_MCP_DISABLE_TIER2=1` (in
> `.env` or the service environment) to drop all 8 Tier 2 tools from
> registration; the 14 Tier 1 ops still load. Use it when the client *defers*
> MCP tools behind a keyword search — a smaller surface means an agent reaches
> `find`/`get`/`note` without first wading past a dozen escape hatches (and,
> with two connectors registered, the surface is doubled). Default is unset:
> all tools register, preserving current behaviour.

- `create_file` — write a file at an arbitrary vault path, optional
  frontmatter dict. `kind="dir"` instead makes a folder (mkdir -p).
  Refuses Sources/Evidence; curated trees require `allow_curated=true`.
- `list_directory` — list files+subfolders (recursive optional). Surfaces
  the `type:` frontmatter field for `.md` entries. Read-only.
- `move_file` — rename/relocate. Rewrites inbound wikilinks by default
  (`[[old]]`, `[[old.md]]`, and `[[basename]]` when unique vault-wide).
- `delete` — **trash** a file OR folder (auto-detected). Moves to
  `Knowledge Base/_trash/YYYY-MM-DD/` with a `.meta.json` sidecar; never
  permanent. Recovery is `recover_from_trash`. Requires `confirm=true`;
  folders need `recursive=true` if non-empty; refuses on inbound links
  unless `force_orphan=true`; files refuse on a superseded chain unless
  `force_superseded=true`. `expected_dead_inbound: list[str]` ignores
  named files' links (supersession-chain cleanup). Append-only + curated
  guards still apply.
- `list_trash` — enumerate recoverable trash entries with original path,
  timestamp, and force-flags used. Also surfaces drift (orphan sidecars,
  orphan files). Read-only.
- `recover_from_trash` — undo a delete. Reads the sidecar to find the
  original location, moves the file/dir back, cleans up the sidecar.
  Optional `restore_path` override for renamed/relocated recovery.
- `append_to_file` — append text. Refuses on Sources/.
- `list_inbound_links` — find all files whose wikilinks resolve to a
  target. Read-only. Useful before move/delete.

`get_frontmatter` folded into `get` (`frontmatter_only=true`);
`set_frontmatter_field` folded into `edit` (`field`+`value`);
`create_directory` into `create_file` (`kind="dir"`);
`delete_file`/`delete_directory` into `delete`; `multi_edit`/`set_take`
into `edit` (`edits=[…]` / `row_key`+`take`).

**Discipline preserved across BOTH tiers:** Sources/ and Evidence/ are
append-only (no Tier 2 op writes there); curated trees (`Cognitive Core/`,
`Domains/`, `Prompt Bank/`, `Products/`, `Personal Context/`,
`Systems Thinking/`) refuse Tier 2 writes by default — pass
`allow_curated=true` as a deliberate per-call acknowledgement; deletes
are never permanent (`delete` moves to
`Knowledge Base/_trash/YYYY-MM-DD/`, recoverable via `recover_from_trash`);
every write logs to `Knowledge Base/log.md`.

**Two-layer traceability:**
- `Knowledge Base/log.md` — durable content history. Writes only.
  KB-scoped. The "what happened to the vault" record. Never
  auto-purged.
- `logs/kb-mcp.log` — service log. Every call (reads + writes) is
  surfaced via a per-call middleware as `tool=<name> duration_ms=<n>
  event=tool_success|tool_error`. Operational layer (debug "did the
  call reach the server", spot slow ops, etc.). Rotated by NSSM.

Deferred to desk-side: `schema` (KB governance — intentionally non-parity).

Deferred to a future pass (Tier 3): `bulk_operations` (atomic multi-step
writes), `rename_tag`, `find_replace_in_file`, `get_directory_tree`.

## Architecture

```
┌──────────────────┐   HTTPS    ┌──────────────────────────────┐
│   claude.ai      │ ─────────▶ │ Tailscale edge              │
│   (mobile/web    │   bearer   │ <device>.<tailnet>.ts.net   │
│    backend)      │            │ auto Let's Encrypt cert     │
│  160.79.104.0/21 │            └──────────────────────────────┘
└──────────────────┘                          │
                                              │ tailscale funnel
                                              ▼
                            ┌────────────────────────────────────┐
                            │ Windows desktop                    │
                            │                                    │
                            │   FastMCP @ 127.0.0.1:8765         │
                            │   bearer-token auth                │
                            │   ↓                                │
                            │   tools: find, add                 │
                            │   ↓                                │
                            │   D:\Archive\...\Knowledge Base    │
                            └────────────────────────────────────┘
```

**Why a public endpoint, not Tailscale-internal?** claude.ai's MCP
client fetches the connector URL *from Anthropic's cloud
infrastructure* (egress range `160.79.104.0/21`), not from your phone.
A tailnet-internal hostname is unreachable. The auth boundary is
therefore not Tailscale membership but **GitHub OAuth**, locked down
to a single GitHub login via a custom `SingleUserGitHubVerifier`
wrapping FastMCP's `OAuthProxy`. claude.ai discovers the OAuth
endpoints at `/.well-known/oauth-authorization-server`, registers
itself via DCR at `/register`, and walks the standard authorize →
token → use flow.

**Why desktop, accepted downtime?** v1 ships without a dedicated
always-on box. When the desktop is asleep, mobile add fails with a
connection error; fall back to pasting into Obsidian directly (the
existing capture-only path). Revisit if downtime bites.

## Install

```powershell
cd C:\Users\hugoa\Desktop\projects\kb-mcp

# 1. Install Python deps (creates .venv automatically).
#    --extra embeddings pulls torch + sentence-transformers for HYBRID search.
#    A bare `uv sync` is now the LEAN/keyword-only install (see SETUP-FRIEND.md);
#    this remote/GPU deployment wants the extra.
uv sync --extra embeddings

# 2. Set up the public URL via Tailscale Funnel (you need this URL in step 3).
# In Tailscale admin console: enable HTTPS for tailnet + enable Funnel for this node.
# Then:
tailscale funnel --bg --https=443 http://127.0.0.1:8765
tailscale funnel status
# Note the printed URL, e.g. https://<device>.<tailnet>.ts.net
```

### 3. Create a GitHub OAuth App (one-time, ~3 min)

At <https://github.com/settings/developers> → **OAuth Apps** → **New OAuth App**:

| Field | Value |
|---|---|
| Application name | `kb-mcp` |
| Homepage URL | `https://<device>.<tailnet>.ts.net` |
| Authorization callback URL | `https://<device>.<tailnet>.ts.net/auth/callback` |

Save the generated **Client ID** and **Client Secret**.

### 4. Populate `.env`

Create `.env` in the repo root:

```
KB_MCP_BASE_URL=https://<device>.<tailnet>.ts.net
KB_MCP_GITHUB_USERNAME=<your-github-login>
GITHUB_CLIENT_ID=<from step 3>
GITHUB_CLIENT_SECRET=<from step 3>
# Optional: override vault path
KB_MCP_VAULT_PATH=D:\Archive\Personal Archive\50 Notes\Obsidian
```

`KB_MCP_BASE_URL` must match the Tailscale Funnel URL exactly — no trailing
slash, no `/mcp` suffix. `KB_MCP_GITHUB_USERNAME` is case-insensitive but must
be the *login* (e.g. `Artexis10`), not the display name.

### 5. Sanity-test locally

```powershell
# stdio (no auth needed)
uv run python -m kb_mcp --transport stdio
# Ctrl-C to stop

# HTTP (OAuth required)
uv run python -m kb_mcp --transport streamable-http --host 127.0.0.1 --port 8765
# In another terminal:
#   curl.exe -i http://127.0.0.1:8765/mcp                      → expect 401
#   curl.exe -i http://127.0.0.1:8765/.well-known/oauth-authorization-server
#                                                              → expect JSON metadata
```

### 6. Install as Windows service (auto-start on boot)

```powershell
# Prereq: NSSM must be installed and on PATH. Easiest:
#   winget install NSSM.NSSM
# or download from https://nssm.cc/download and add nssm.exe to PATH
# (or pass -NssmPath "C:\path\to\nssm.exe" to the script below).
# The script self-elevates; approve the UAC prompt.
pwsh -File scripts/install-service.ps1
# Uninstall:
#   nssm stop kb-mcp && nssm remove kb-mcp confirm
# Restart (after .env edits): elevated shell required
#   Start-Process -Verb RunAs -Wait sc.exe -ArgumentList 'stop','kb-mcp'
#   Start-Process -Verb RunAs -Wait sc.exe -ArgumentList 'start','kb-mcp'
```

## Add to claude.ai

1. claude.ai → Settings → Connectors → **Add custom connector**
2. **Name**: `Knowledge Base` (or whatever)
3. **Server URL**: `https://<device>.<tailnet>.ts.net/mcp`
4. Leave **OAuth Client ID** and **OAuth Client Secret** blank — claude.ai
   uses Dynamic Client Registration against your `/register` endpoint.
5. Save. claude.ai opens a GitHub login window → log in (only the user in
   `KB_MCP_GITHUB_USERNAME` is allowed) → approve consent → redirects back
   to claude.ai. Tools `find` and `add` appear in the palette.

## Deploying on a second machine (multi-host)

Each machine is an independent deployment — there is no shared state. To run kb-mcp
on a second box (e.g. a laptop alongside the desktop), repeat the install with that
host's *own* values. The non-obvious parts:

- **Its own Funnel hostname.** Each Tailscale node has a distinct
  `<node>.<tailnet>.ts.net`, so `KB_MCP_BASE_URL` and the claude.ai connector URL are
  per-host. `tailscale funnel status` prints this node's name.
- **Its own GitHub OAuth App.** A GitHub OAuth App allows exactly **one**
  Authorization callback URL, so you *cannot* reuse another host's app — its callback
  points at the other host and GitHub rejects the redirect with "The redirect_uri is
  not associated with this application." Create a second app (e.g. `kb-mcp (laptop)`)
  with callback `https://<this-host>.<tailnet>.ts.net/auth/callback` and put *its*
  `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` in this machine's `.env`.
- **Its own `.env` and connector.** The vault path auto-resolves per machine (see the
  desktop/laptop constants in `src/kb_mcp/vault.py`); override with `KB_MCP_VAULT_PATH`
  if needed. In claude.ai, add a separate connector pointing at this host's `/mcp` URL
  (the URL usually isn't editable in place, so delete + re-add to repoint).
- **Its own embedding stack (GPU).** Hybrid `find` needs `torch` + `sentence-transformers`
  (the optional `embeddings` extra) in the host's `.venv` — `uv sync --extra embeddings`
  installs them, pulling the pinned `cu132` torch which ships Blackwell `sm_120`, so any
  RTX 50-series laptop/desktop GPU works. **If a host was synced without the extra**, `find`
  silently degrades to keyword/BM25 and the log shows the vector path failing to import
  torch — `uv sync --extra embeddings` on that host fixes it. Verify the GPU path:
  `uv run python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_arch_list())"`
  → expect `True` and `sm_120` in the list, plus the startup log line
  `embedding model ready ... on cuda`. (Default PyPI Windows torch is CPU-only, which
  is why the explicit CUDA index in `pyproject.toml` exists — see the comment there.)

The deployments coexist — claude.ai talks to whichever host's connector you invoke,
and only that host needs to be awake. After editing `.env`, restart the service so it
reloads: `sc.exe stop kb-mcp; sc.exe start kb-mcp` (the installer grants no-UAC
start/stop rights, so this needs no elevation once installed correctly).

## Tool reference

### `find`

```json
{
  "query": "metabolism",
  "types": ["research-note", "insight"],
  "projects": ["health"],
  "tags": ["curriculum"],
  "limit": 10
}
```

Filters AND together; lists OR within (any tag matches, any project matches).
`query` is case-insensitive substring against title + body.

Excluded from search: `_Schema/`, `_attachments/`, `_archive/`.

### `add`

```json
{
  "content": "Long-form body...",
  "source_type": "article",
  "title": "Agentic RAG explained",
  "url": "https://example.com/agentic-rag",
  "tags": ["rag", "agentic"],
  "why_captured": "Useful for the Q retrieval roadmap."
}
```

Writes:
- `Sources/<Type>/YYYY-MM-DD-<slug>.md` (with `type: source`, `source_type:`,
  `captured:`, `url:`, `tags:`, `ingested_into: []`, `# Source: <title>`,
  optional `> <why_captured>` blockquote, `## Capture` body)
- `Sources/index.md` (bumps By-type count + prepends Recent capture)
- `Knowledge Base/index.md` (prepends Recent activity bullet w/ cap-50 trim;
  recomputes the Sources Counts line)
- `Knowledge Base/log.md` (prepends `## [<date>] add | Sources/<Type>/<file>`)

`source_type ∈ {article, session, book, paper, video, other}`. `url` is
required for article/paper/video. Validation errors return a structured
`INVALID_SOURCE` shape.

### `note`

```json
{
  "content": "## Question\n\n...\n\n## Findings\n\n...\n\n## Connections\n\n- [[...]]",
  "note_type": "research-note",
  "title": "Agentic RAG retrieval budget",
  "project": "q",
  "sources": ["Knowledge Base/Sources/Articles/2026-05-18-agentic-rag"],
  "tags": ["rag", "retrieval"]
}
```

Or for an `insight`:

```json
{
  "content": "## Claim\n\n...\n\n## Why it holds\n\n...\n\n## Connections\n\n- [[...]]",
  "note_type": "insight",
  "title": "Retrieval precision gates prevent downstream confusion",
  "projects": ["q", "endstate"],
  "sources": ["[[Knowledge Base/Notes/Research/Q/rag-eval-framework]]"],
  "tags": ["retrieval", "quality-gates"]
}
```

Writes:
- `Notes/Research/<Project>/<slug>.md` (research-note) or
  `Notes/Insights/<slug>.md` (insight). No date prefix — compiled notes evolve.
- For each `sources:` entry, appends the new note's wikilink to that
  source's `ingested_into:` frontmatter list (handles both flow `[]` and
  block `- "[[...]]"` YAML shapes). Idempotent.
- `Knowledge Base/index.md` — prepends Recent activity bullet (cap-50 trim).
- `Knowledge Base/log.md` — prepends `## [<date>] note | Notes/...` entry.

`note_type ∈ {research-note, insight, failure, pattern, experiment,
production-log}`. `project` (singular) is required for research-note;
valid keys: `substrate, q, endstate, sift, tu, book-club, health,
finance, creative, science, travel, personal`. `projects` (plural) is
optional for insight/failure/pattern/production-log. Per-type
conditional fields:

| Type | Required extras | Optional extras | Status enum |
|---|---|---|---|
| research-note | `project` | — | `active`, `draft` |
| insight | — | `projects` | `active`, `draft` |
| failure | — | `projects`, `severity` ∈ {minor,moderate,serious,critical} | `active`, `draft` |
| pattern | — | `projects`, `pattern_type` ∈ {architectural,workflow,prompting,governance,pedagogical} | `active`, `draft` |
| experiment | `domain`, `started`, `duration` | `hypothesis`, `n`, `concluded` | `active`, `draft`, `archived` |
| production-log | `medium` | `recorded`, `published`, `host`, `editor`, `projects` | `planned`, `recorded`, `edited`, `published`, `reflected`, `dropped`, `archived` (default `planned`) |

Experiments and production-logs auto-prefix their filenames with `YYYY-MM-`
(month from `started` for experiments, from `created` for production-logs).
Validation errors return a structured `INVALID_NOTE` shape.

Counts in `index.md` (e.g. `- Notes (research): N`) are NOT auto-bumped
by `note`. Run `audit` to detect drift; reconcile via desk-side or a
future `audit --fix`. A warning surfaces this in every `note` return value.

### `audit`

```json
{
  "categories": ["broken_wikilink", "orphan_entity"]
}
```

Or omit `categories` to run all four checks. Returns:

```json
{
  "findings": [
    {
      "category": "broken_wikilink",
      "severity": "warn",
      "path": "Knowledge Base/Notes/Insights/foo.md",
      "detail": "Wikilink [[X]] points to a file that doesn't exist",
      "proposed_fix": "Update the link to the correct target, or remove if obsolete."
    }
  ],
  "summary": {"broken_wikilink": 1}
}
```

Read-only — never writes. Use the findings to drive follow-up `note`/`add`
calls (e.g. compile from unprocessed sources, retarget broken wikilinks).
Categories: `broken_wikilink`, `orphan_entity`, `unprocessed_source`,
`index_drift`, `tag_inconsistency`. More checks (supersession integrity,
experiment lifecycle, stale hubs) deferred.

The `tag_inconsistency` check catches mechanical drift like
`warning-letter-incident` vs `warning_letter_incident` vs `Warning-Letter-Incident`:
same logical tag, multiple spellings, ungroupable without normalization. It
reports each cluster with a proposed canonical (most-used variant) plus the
list of pages using each variant. Semantic near-duplicates (`metabolism` vs
`metabolic`) are NOT flagged — that needs human judgment. Singleton tags
aren't flagged either (too noisy in practice; a healthy KB has many
genuinely-unique one-offs). Source pages are immutable per rule 2 — fixing
source tags isn't possible; the audit reports them anyway so a desk-side
compilation pass can normalize forward via downstream compiled pages.

### `get`

```json
{ "path": "Notes/Insights/progressive-disclosure-without-mode-fragmentation" }
```

Or read from curated trees outside `Knowledge Base/`:

```json
{ "path": "Cognitive Core/Strategy.md" }
```

Returns:

```json
{
  "path": "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md",
  "frontmatter": {"type": "insight", "status": "active", ...},
  "body": "# Progressive disclosure ...",
  "content": "---\ntype: insight\n...\n---\n\n# Progressive disclosure ..."
}
```

Reads any `.md` file under the vault root — including the read-only curated
trees (`Cognitive Core/`, `Domains/`, `Prompt Bank/`, `Products/`,
`Personal Context/`). The path accepts shapes with or without the leading
`Knowledge Base/` and with or without the `.md` suffix; for shortcuts that
don't resolve literally, the tool retries with `Knowledge Base/` prefixed.
Path-escape guarded (rejects anything outside the vault root).

### `edit`

```json
{
  "path": "Notes/Insights/progressive-disclosure-without-mode-fragmentation",
  "why": "fixed typo in claim section, normalized 'modes' tag",
  "new_body": "# Progressive disclosure without mode fragmentation\n\n## Claim\n\n...\n\n## Why it holds\n\n...",
  "tags": ["ux", "modes", "governance"]
}
```

Lightweight in-place edit of a compiled page. Either or both of `new_body`
and `tags` can be supplied; `why` is required and lands in `log.md` so the
edit remains auditable. Always bumps `updated:` to today; all other
frontmatter fields (type, project, status, sources, etc.) stay as-is.

Refuses on:
- `Sources/` and `Evidence/` paths (rule 2: append-only — add a corrective
  source instead).
- Pages with `type: source` (defensive).
- Pages already marked `status: superseded` (don't edit history).

Use `edit` for tweaks. Use `replace` for substantial rewrites where
"this is a meaningfully different page" is the right framing.

### `link`

```json
{
  "entity_type": "person",
  "name": "Andrej Karpathy",
  "summary": "Tesla / OpenAI alumnus, ML educator, \"Software 3.0\" framing.",
  "why_in_kb": "Referenced in the Endstate engine-architecture hub for his views on minimal-dependency engineering.",
  "affiliation": "Tesla / OpenAI alumnus",
  "relationship": "public-figure",
  "tags": ["ml", "llm"],
  "connections": ["Notes/Research/Endstate/engine-architecture"]
}
```

Per-type optional frontmatter:
- `person`: `affiliation`, `relationship`
- `concept`: `domain` (e.g. "retrieval", "metabolism", "infrastructure")
- `library`: `language`, `repo`, `license`, `used_in`
- `decision`: `decided` (YYYY-MM-DD), `project`, `decision_status ∈
  {proposed, accepted, superseded}`

**Name is Title Case** (not slugified) — entities are named after the thing
they are. Path: `Entities/<Folder>/<Name>.md`. Folders: People, Concepts,
Libraries, Decisions.

Create-only in v1. If the entity already exists, returns `ENTITY_EXISTS` —
use `replace` to supersede. Sub-folder index (e.g. categorized concepts) not
auto-updated; surfaced via desk audit.

### `replace`

```json
{
  "old_path": "Notes/Research/Endstate/engine-architecture",
  "reason": "Major rewrite after the contracts directory restructure.",
  "content": "## Question\n\n...\n\n## Findings\n\n...",
  "note_type": "research-note",
  "title": "Endstate engine architecture (v2)",
  "project": "endstate",
  "sources": ["Notes/Research/Endstate/engine-architecture"]
}
```

Supersession is **metadata-only** per SKILL.md rule 6:
- Writes the new page at a fresh slug (via the same machinery as `note`,
  so back-refs + index + log all happen)
- Adds `supersedes: "[[<old>]]"` to the new page's frontmatter
- Patches the old page: `status: superseded`, `superseded_by: "[[<new>]]"`,
  refreshed `updated:` date. **Body untouched.**
- Inbound wikilinks STAY pointing at the old page; readers follow the
  `superseded_by:` chain.
- Appends a `## [<date>] replace |` entry to `log.md` with the reason.

All `note` args (besides `note_type`+`title`+`content`) are accepted to
build the new page. Cannot supersede sources or evidence (append-only).
Cannot re-supersede an already-superseded page.

### `preserve`

```json
{
  "scope": "Mother Cancer",
  "category": "letters",
  "filename": "2026-04-15-pathology-report.pdf",
  "content_base64": "JVBERi0xLjQKJ...",
  "description": "Pathology report from May Clinic, post-op."
}
```

Or for a text artifact:

```json
{
  "scope": "Yolo",
  "category": "court-docs",
  "filename": "2026-03-10-judgment-summary.md",
  "content": "Summary of the judgment text...",
  "description": "Plain-text excerpt of the judgment for searchability."
}
```

Exactly one of `content_base64` or `content` must be supplied.
`content_base64` is for binaries (5MB decoded limit). If `description` is
supplied, a sidecar `<filename>.md` is written alongside with `type: source,
source_type: other` frontmatter.

Append-only per SKILL.md rule 2. `ARTIFACT_EXISTS` if the filename already
exists — pick a new name (date-prefixing is the convention for temporal
anchoring).

## Revoke access

Pick the strongest option that fits the situation:

| Situation | Action |
|---|---|
| Suspect the GitHub OAuth grant is compromised | Revoke at <https://github.com/settings/applications> → find `kb-mcp` → Revoke. claude.ai's token dies on the next call (verifier hits `api.github.com/user` per request). |
| Suspect the GitHub OAuth App secret leaked | Rotate the secret at <https://github.com/settings/developers> → `kb-mcp` → "Generate a new client secret". Update `GITHUB_CLIENT_SECRET` in `.env`, restart the service. |
| Want to disconnect just claude.ai | Delete the connector in claude.ai → Settings → Connectors. |
| Want to take the endpoint offline entirely | `tailscale funnel --https=443 off`. Endpoint becomes unreachable from the public internet. |
| Want to stop the service but leave the public URL configured | Elevated: `Start-Process -Verb RunAs -Wait sc.exe -ArgumentList 'stop','kb-mcp'`. Funnel still up but proxies to nothing. |
| Want a clean uninstall | Stop + remove service, turn off Funnel, delete the connector in claude.ai, delete the GitHub OAuth App. |

## Testing

```powershell
uv run pytest tests/ -v
```

Tests run against a fixture vault under `tests/fixtures/`. The **real
vault is never touched** during testing — `conftest.py` copies fixtures
to a per-test tmp dir and sets `KB_MCP_VAULT_PATH` to the copy.

## Retrieval evaluation & feedback loop

Ranking is **measured, not guessed**. The tunable knobs (`RRF k`, the
compiled/source boosts, `candidate_k`, the graph seed cap) live in one place —
`find.RankingConfig` — so an offline harness can sweep them against a golden set
and pick winners by NDCG/MRR. `RankingConfig` is internal; it's not exposed on the
`find` MCP tool (claude.ai needs no knobs API).

```powershell
# Baseline NDCG@5/@10, MRR, recall@10 over the real vault (embeddings ON):
uv run python scripts/eval_retrieval.py
# Grid-search the ranking knobs; --markdown emits a table to file as a pattern note:
uv run python scripts/eval_retrieval.py --sweep --markdown
#   add --include-rerank to add the (slow) cross-encoder axis
```

The golden set is `tests/golden/queries.yaml` — hand-seeded against real paths and
designed to **grow from your own usage**:

- Every `find()` is logged to `logs/queries.jsonl` (query + per-hit ranking
  signals — no bodies/excerpts) and every `note`/`add`/`replace` to
  `logs/writes.jsonl` (path + cited sources). Both via `kb_mcp.query_log`,
  best-effort, gitignored, never Obsidian-synced. No-op under
  `KB_MCP_DISABLE_EMBEDDINGS` or `KB_MCP_DISABLE_QUERY_LOG`.
- `scripts/derive_relevance_pairs.py` joins the two: when a write cites a path
  shortly after a `find()` for some query, that `(query → cited_path)` pair is a
  weak relevance label. It writes `logs/relevance_pairs.jsonl` and **proposes**
  (never auto-writes) golden-set additions for you to confirm.

Metric math (`kb_mcp.eval_metrics`) is pure and unit-tested in the fast suite;
the live-vault eval is a manual `scripts/` run (it needs the bge model).

## Corpus-aware writes

Writes consult the corpus instead of being blind to it — so each new entry makes
the existing ones more discoverable, not just adds to the pile. Pure retrieval
(`kb_mcp.corpus_aware`), reusing `find()` + the embedding sidecar; no new
dependency, no server-side LLM. Suggestions are **surfaced, never auto-applied** —
the client decides what to wire in.

- **`suggest_links`** (MCP tool, read-only): given an existing `path` OR a
  `draft_title`+`draft_body`, returns ranked existing pages to link
  `{path, title, type, why, excerpt}`, preferring graph hubs and excluding the
  page itself + anything already linked. Run it on a draft before `note`, or on
  any existing page to densify the graph retroactively.
- **`note` suggestions**: `note()` returns an optional `suggestions` block (the
  related pages you didn't cite). Best-effort, computed pre-write, fully guarded —
  a suggestion failure can never roll back the write.
- **Near-duplicate warnings**: `note`/`add` surface a `warnings` entry like
  `possible near-duplicate of [[X]] (cosine 0.91)` when a draft closely matches an
  existing page (doc-doc cosine over the sidecar). A warning, never a block —
  append-only + supersession invariants mean the client chooses edit/replace/append.

All of it no-ops under `KB_MCP_DISABLE_EMBEDDINGS`, so the fast test suite and
the write path's existing behaviour are unchanged when embeddings are off.

## Active distillation

The KB grows in raw captures faster than it grows in compiled knowledge (a large
share of sources never get distilled). Two read-only additions turn that backlog
from an undifferentiated pile into a worked queue — without any server-side LLM:

- **Aged `unprocessed_source` audit**: each finding now carries `meta`
  (`age_days`, `age_bucket` ∈ fresh/aging/stale, `captured`), is sorted
  **oldest-first**, and escalates to `warn` once stale. You drain the worst rot
  first instead of guessing.
- **`propose_compilation`** (MCP tool): point it at one or more sources and it
  returns a ready-to-fill note scaffold — inferred `note_type`, a sectioned
  outline (`Question/Findings/Connections` or `Claim/…`), the `sources[]` to
  cite, and adjacent compiled pages to link (via the same retrieval as
  `suggest_links`). It **never writes** — you fill the prose and call `note()`.

Grouping (which sources belong in one note) is left to the client — that's a
judgment call Claude makes better than a cosine threshold. Audit surfaces the
aged list; you pick a coherent set and pass it to `propose_compilation`.

## Logs

- `logs/kb-mcp.log` — application log (rotated, 5 MB × 5 files)
- `logs/service.out.log`, `logs/service.err.log` — NSSM stdout/stderr (rotated by NSSM)

## Restarting the service

`install-service.ps1` grants your user account start/stop rights on the
service, so day-to-day restarts don't need UAC:

```powershell
sc.exe stop kb-mcp
sc.exe start kb-mcp
Get-Content logs\kb-mcp.log -Tail 6
```

If you skipped the grant (or installed from an older version of the script),
re-run the install script — it's idempotent and will only add the ACE if
it's missing.

For a stuck restart (orphan python processes holding port 8765), force-clean:

```powershell
sc.exe stop kb-mcp
Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force
sc.exe start kb-mcp
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| claude.ai "Couldn't reach the MCP server" during connector add | OAuth discovery failed | `curl.exe -i https://<funnel-url>/.well-known/oauth-authorization-server` should return JSON. If 404, the OAuthProxy isn't mounted — most likely `KB_MCP_BASE_URL` has a trailing slash or includes `/mcp`. |
| GitHub redirects to "The redirect_uri MUST match…" error | OAuth App callback URL mismatch | At github.com/settings/developers → kb-mcp, set Authorization callback URL to exactly `https://<funnel-url>/auth/callback` (no trailing slash). |
| GitHub: "The redirect_uri is not associated with this application" on a *second* machine | Reused another host's OAuth App client ID/secret in this machine's `.env` (the app's one callback points at the other host) | Create a per-host OAuth App with callback `https://<this-host>.<tailnet>.ts.net/auth/callback`, put its client ID/secret in this `.env`, restart the service. See § Deploying on a second machine. |
| claude.ai connector connects but every tool call returns 401 | Wrong GitHub user | `KB_MCP_GITHUB_USERNAME` must equal the login of the GitHub account you authorized with. Check the kb-mcp log for `rejecting token for github login=...`. |
| claude.ai shows "connector failed" | service down (desktop asleep, service stopped, crash loop) | `Get-Service kb-mcp`; tail `logs/service.err.log` and `logs/kb-mcp.log`. Multiple startup banners within seconds = orphan python processes — kill them and force-restart. |
| Edits to `.env` not picked up | service didn't restart, or UAC dismissed | Elevated: `Start-Process -Verb RunAs -Wait sc.exe -ArgumentList 'stop','kb-mcp'` then `'start','kb-mcp'`. Confirm with `Get-Process python \| Select-Object StartTime`. |
| `sc.exe stop/start kb-mcp` → "OpenService FAILED 5: Access is denied" from a normal admin shell | The no-UAC start/stop grant never applied — usually because the install ran non-elevated (UAC on → filtered token), so the `sc sdset` grant (and the `nssm set` calls) were silently denied | Re-run `scripts/install-service.ps1` (it self-elevates now); that reapplies the grant, `AppDirectory`, and log redirects. Verify with `sc.exe sdshow kb-mcp` — look for a trailing `(A;;RPWPCR;;;S-1-5-21…)` ACE. |
| `logs/service.err.log` / `service.out.log` missing | The `nssm set … AppStdout/AppStderr` calls were denied during a non-elevated install | Re-run the installer elevated (it self-elevates now); the log-redirect settings only take with a full admin token. |
| 404 / Funnel "no service" | Tailscale Funnel disabled or pointing at wrong port | `tailscale funnel status`; re-run the funnel command in the install instructions |
| `KB vault not found` on startup | desktop vault path moved or `KB_MCP_VAULT_PATH` wrong | set `KB_MCP_VAULT_PATH` to the absolute vault root in `.env` |
| Schema parse error on startup | `_Schema/references/frontmatter.md` shape changed | diff against the version that was working; the parser is conservative on purpose |
| `add` fails with `INVALID_SOURCE` | missing required field (url for article/paper/video; non-empty content/title) | the error payload names the missing field; fix and retry |

## Out of scope

Per the architecture decision, none of the following are in v1:

- `schema` operation — KB governance stays desk-side, by design (SKILL.md
  is the spec that all the other tools depend on; changing it mid-session
  is too high-stakes for an unattended write surface).
- File deletion — the KB convention is supersession-not-deletion (SKILL.md
  rule 6). Mistakes get superseded via `replace`, never deleted.
- `audit --fix` auto-resolution — current `audit` is report-only; auto-fix
  for safe categories (e.g. index drift) is a follow-up.
- Audit's 11 desk-side checks — currently 4 implemented (broken_wikilink,
  orphan_entity, unprocessed_source, index_drift). Adding supersession
  integrity, experiment lifecycle, stale hubs, etc. is incremental.
- Auth layers beyond single-user GitHub OAuth (no mTLS, IP allowlist,
  multi-user RBAC).
- Monitoring/metrics/observability beyond rotating file logs.
- Web UI
- Vault writes outside `Sources/<type>/`, `Sources/index.md`,
  top-level `index.md`, `log.md`
- Compiled-note creation from mobile (`add` only captures raw sources;
  compilation stays desk-side via the KB skill's propose-before-write flow)
- Multi-host failover / always-on home server
