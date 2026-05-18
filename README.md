# kb-mcp

Self-hosted Model Context Protocol server exposing Hugo's Obsidian
Knowledge Base to **mobile claude.ai** as a remote custom connector.

Tools surfaced (full parity with the desk-side KB skill except `schema`):

- `find` — read-only search across `Knowledge Base/`, type/project/tag filtered
- `get` — read a full page by path (frontmatter + body + raw content)
- `add` — capture a raw `source` page with full SKILL.md rule-7 writes
- `note` — create any of the six compiled page types (research-note,
  insight, failure, pattern, experiment, production-log) with rule-7
  writes + `ingested_into:` back-refs on cited sources
- `link` — create a typed entity under `Entities/<Type>/<Name>.md`
  (person, concept, library, decision)
- `replace` — supersession: write a new page + flip the old one to
  `status: superseded` with `superseded_by:` back-link. The modify path.
- `preserve` — capture binary or text artifact to
  `Evidence/<scope>/<category>/` (append-only)
- `audit` — read-only graph health check (broken wikilinks, orphan
  entities, unprocessed sources, index/log drift)

Deferred to desk-side: `schema` (KB governance — intentionally non-parity).

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

# 1. Install Python deps (creates .venv automatically)
uv sync

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
v1 categories: `broken_wikilink`, `orphan_entity`, `unprocessed_source`,
`index_drift`. More checks (supersession integrity, experiment lifecycle,
stale hubs) deferred.

### `get`

```json
{ "path": "Notes/Insights/progressive-disclosure-without-mode-fragmentation" }
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

The path accepts all four shapes: with or without the leading
`Knowledge Base/`, with or without the `.md` suffix. Read-only; path-escape
guarded (rejects anything outside `Knowledge Base/`).

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
| claude.ai connector connects but every tool call returns 401 | Wrong GitHub user | `KB_MCP_GITHUB_USERNAME` must equal the login of the GitHub account you authorized with. Check the kb-mcp log for `rejecting token for github login=...`. |
| claude.ai shows "connector failed" | service down (desktop asleep, service stopped, crash loop) | `Get-Service kb-mcp`; tail `logs/service.err.log` and `logs/kb-mcp.log`. Multiple startup banners within seconds = orphan python processes — kill them and force-restart. |
| Edits to `.env` not picked up | service didn't restart, or UAC dismissed | Elevated: `Start-Process -Verb RunAs -Wait sc.exe -ArgumentList 'stop','kb-mcp'` then `'start','kb-mcp'`. Confirm with `Get-Process python \| Select-Object StartTime`. |
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
