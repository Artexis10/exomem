# kb-mcp — remote deployment

This guide covers the **remote tier**: running kb-mcp as an always-on HTTP service
behind a public HTTPS endpoint so you can reach your vault from **claude.ai** on
the web or mobile as a custom connector. The local-only path (Claude Code over
stdio, no cloud) is in [../SETUP-LOCAL.md](../SETUP-LOCAL.md); start there if you
don't need mobile access.

Throughout, replace `<your-host>` / `example.com` with your own hostname.

## Architecture

```
┌──────────────────┐   HTTPS    ┌──────────────────────────────┐
│   claude.ai      │ ─────────▶ │ public edge (CDN / tunnel)   │
│   (mobile/web    │   bearer   │ kb.example.com               │
│    backend)      │            │ TLS terminated at edge       │
│  160.79.104.0/21 │            └──────────────────────────────┘
└──────────────────┘                          │
                                              │ tunnel
                                              ▼
                            ┌────────────────────────────────────┐
                            │ host: macOS / Linux / Windows      │
                            │                                    │
                            │   FastMCP @ 127.0.0.1:8765         │
                            │   GitHub OAuth (single user)       │
                            │   ↓                                │
                            │   MCP tools (find, get, note, …)   │
                            │   ↓                                │
                            │   <vault>/Knowledge Base           │
                            └────────────────────────────────────┘
```

**Why a public endpoint, not a tailnet-internal one?** claude.ai's MCP client
fetches the connector URL *from Anthropic's cloud infrastructure* (egress range
`160.79.104.0/21`), not from your phone. A purely internal hostname is therefore
unreachable. The auth boundary is not network membership but **GitHub OAuth**,
locked down to a single GitHub login via a custom `SingleUserGitHubVerifier`
wrapping FastMCP's `OAuthProxy`. claude.ai discovers the OAuth endpoints at
`/.well-known/oauth-authorization-server`, registers itself via Dynamic Client
Registration at `/register`, and walks the standard authorize → token → use flow.

**Downtime.** A single-host deployment without an always-on box accepts downtime:
when the host is asleep, mobile writes fail with a connection error — fall back to
editing the vault directly (the local capture path). Run on a box that stays up
(or a cheap VPS) if you want reliable mobile access.

## 1. Install dependencies

```powershell
cd /path/to/kb-mcp

# Install Python deps (creates .venv automatically).
#   --extra embeddings pulls torch + sentence-transformers for HYBRID search.
#   --extra media pulls faster-whisper + pytesseract + pymupdf + markitdown for
#   SERVER-SIDE media extraction (auto transcribe/OCR/parse uploaded binaries →
#   searchable). On Windows the [media] extra also pins the CUDA-12 runtime
#   (cublas/cudnn/cudart) that ctranslate2 needs alongside torch's cu132 build.
uv sync --extra embeddings --extra media
```

Media extraction needs two **system** tools (not pip-installable):

- **Tesseract OCR** (images): `winget install --id UB-Mannheim.TesseractOCR -e`.
  The installer doesn't add it to PATH; the server auto-discovers it at
  `C:\Program Files\Tesseract-OCR\`, or set `KB_MCP_TESSERACT_CMD`.
- **ffmpeg** is bundled by PyAV (pulled via faster-whisper), so audio/video decode
  works without a separate install.

Verify the GPU media path: `uv run python scripts/verify-media-gpu.py`.

Lean / CPU-only boxes can skip all of this — set
`KB_MCP_DISABLE_MEDIA_EXTRACTION`; uploads still work, just without server-side
searchable-text extraction. CPU also works (no GPU needed), just slower — pick a
smaller Whisper model with `KB_MCP_WHISPER_MODEL=base`. The CUDA-12 wheels above
are Windows + GPU only, unused on CPU.

To make **existing** Evidence/media files searchable (one-shot back-fill; new
uploads are handled automatically), run:

```powershell
uv run python -m kb_mcp backfill-media --dry-run   # preview (writes nothing)
uv run python -m kb_mcp backfill-media             # do it (CPU or GPU)
```

It writes a sidecar if missing, extracts text (OCR/ASR/PDF), and CLIP-embeds
images. Idempotent. Flags: `--no-ocr` (sidecar + CLIP only), `--no-clip`,
`--vault <root>`.

## 2. Set up a public HTTPS URL

You need a hostname for step 3. Pick **one**:

**Option A — Tailscale Funnel.** No domain needed; free
`<device>.<tailnet>.ts.net` host; simplest. Best if you don't own a domain. In the
Tailscale admin console enable HTTPS for the tailnet + Funnel for this node, then:

```powershell
tailscale funnel --bg --https=443 http://127.0.0.1:8765
tailscale funnel status        # note the URL, e.g. https://<device>.<tailnet>.ts.net
```

Funnel's shared relay can throttle bursty reconnects under heavy use; if that
bites, restart Tailscale or switch to Option B.

**Option B — Cloudflare Tunnel.** Needs a domain you own in Cloudflare; more
burst-tolerant under load. Prereq:
`winget install --id Cloudflare.cloudflared`.

```powershell
cloudflared tunnel login
pwsh -File scripts/setup-cloudflared.ps1 -Hostname kb.example.com -TunnelName kb-mcp-host
#   -> https://kb.example.com (script makes the tunnel + DNS + auto-start service).
```

In the Cloudflare dashboard for this hostname: Bot Fight Mode **OFF** + no WAF
managed ruleset (Security Level low); the edge caps requests at ~100s.

## 3. Create a GitHub OAuth App (one-time, ~3 min)

At <https://github.com/settings/developers> → **OAuth Apps** → **New OAuth App**:

| Field | Value |
|---|---|
| Application name | `kb-mcp` |
| Homepage URL | `https://kb.example.com` |
| Authorization callback URL | `https://kb.example.com/auth/callback` |

Save the generated **Client ID** and **Client Secret**.

## 4. Populate `.env`

Create `.env` in the repo root:

```
KB_MCP_BASE_URL=https://kb.example.com
KB_MCP_GITHUB_USERNAME=<your-github-login>
GITHUB_CLIENT_ID=<from step 3>
GITHUB_CLIENT_SECRET=<from step 3>
# Recommended: a long random string that pins the OAuth signing key, so the
# claude.ai connector survives FastMCP upgrades / client-secret rotation without
# re-authorizing. Generate: python -c "import secrets;print(secrets.token_urlsafe(48))"
KB_MCP_JWT_SIGNING_KEY=<long-random-string>
# Required: vault root — the folder that contains Knowledge Base/
KB_MCP_VAULT_PATH=<your-Obsidian-vault-root>
```

`KB_MCP_BASE_URL` must match your public hostname (from step 2) exactly — no
trailing slash, no `/mcp` suffix. `KB_MCP_GITHUB_USERNAME` is case-insensitive but
must be the *login*, not the display name. `KB_MCP_VAULT_PATH` is **required**:
claude.ai connects over HTTP and passes no environment, so the service resolves the
vault solely from this line in `.env` at startup.

## 5. Sanity-test locally

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

## 6. Install as a service (auto-start on boot)

Pick your platform — all three run the same `streamable-http` server and differ
only in the OS service manager.

**macOS (launchd):**

```bash
bash scripts/install-service.sh
# Restart after .env edits:  bash scripts/restart.sh
# Uninstall:                 launchctl bootout gui/$(id -u)/com.kb-mcp && rm ~/Library/LaunchAgents/com.kb-mcp.plist
```

**Linux (systemd --user):**

```bash
mkdir -p ~/.config/systemd/user
sed -e "s|__REPO_ROOT__|$PWD|g" -e "s|__VENV_PYTHON__|$PWD/.venv/bin/python|g" scripts/kb-mcp.service > ~/.config/systemd/user/kb-mcp.service
systemctl --user daemon-reload && systemctl --user enable --now kb-mcp
loginctl enable-linger "$USER"   # keep it running without an active login session
# Restart after .env edits:  systemctl --user restart kb-mcp   (or: bash scripts/restart.sh)
```

**Windows (NSSM):**

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

## 7. Add to claude.ai

1. claude.ai → Settings → Connectors → **Add custom connector**
2. **Name**: `Knowledge Base` (or whatever)
3. **Server URL**: `https://kb.example.com/mcp` (this host's public hostname)
4. Leave **OAuth Client ID** and **OAuth Client Secret** blank — claude.ai uses
   Dynamic Client Registration against your `/register` endpoint.
5. Save. claude.ai opens a GitHub login window → log in (only the user in
   `KB_MCP_GITHUB_USERNAME` is allowed) → approve consent → redirects back to
   claude.ai. The tools appear in the palette.

## Deploying on a second machine (multi-host)

Each machine is an independent deployment — there is no shared state. To run kb-mcp
on a second box (e.g. a laptop alongside a desktop), repeat the install with that
host's *own* values. The non-obvious parts:

- **Its own public hostname.** `KB_MCP_BASE_URL` and the connector URL are
  per-host. Tailscale gives each node a distinct `<node>.<tailnet>.ts.net`
  automatically (`tailscale funnel status`); for Cloudflare, give each host a
  distinct subdomain (e.g. `kb.example.com`, `kb-laptop.example.com`) via
  `pwsh -File scripts/setup-cloudflared.ps1 -Hostname <this-host> -TunnelName <unique-name>`.
- **Its own GitHub OAuth App.** A GitHub OAuth App allows exactly **one**
  Authorization callback URL, so you *cannot* reuse another host's app — its
  callback points at the other host and GitHub rejects the redirect with "The
  redirect_uri is not associated with this application." Create a second app (e.g.
  `kb-mcp (laptop)`) with callback `https://<this-host>.example.com/auth/callback`
  and put *its* `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` in this machine's
  `.env`.
- **Its own `.env` and connector.** Set `KB_MCP_VAULT_PATH` to this machine's vault
  root. In claude.ai, add a separate connector pointing at this host's `/mcp` URL
  (the URL usually isn't editable in place, so delete + re-add to repoint).
- **Its own embedding stack (GPU).** Hybrid `find` needs `torch` +
  `sentence-transformers` (the optional `embeddings` extra) in the host's `.venv` —
  `uv sync --extra embeddings` installs them, pulling the pinned `cu132` torch
  which ships Blackwell `sm_120`, so any RTX 50-series GPU works. **If a host was
  synced without the extra**, `find` silently degrades to keyword/BM25 and the log
  shows the vector path failing to import torch — `uv sync --extra embeddings` on
  that host fixes it. Verify the GPU path:
  `uv run python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_arch_list())"`
  → expect `True` and `sm_120` in the list, plus the startup log line
  `embedding model ready ... on cuda`. (Default PyPI Windows torch is CPU-only,
  which is why the explicit CUDA index in `pyproject.toml` exists.)

The deployments coexist — claude.ai talks to whichever host's connector you invoke,
and only that host needs to be awake. After editing `.env`, restart the service so
it reloads.

## GPU / CUDA notes (Blackwell, sm_120)

Blackwell GPUs (RTX 50-series, compute capability 12.0 / `sm_120`) need CUDA
wheels. Default PyPI torch on Windows is **still CPU-only**, so `pyproject.toml`
pins an explicit CUDA index (`pytorch-cu132`, CUDA 13.2) whose wheel ships
`sm_120` — `torch.cuda.get_arch_list()` includes it, so any RTX 50-series GPU
works. The index is scoped to win32/linux via a platform marker; macOS falls back
to default PyPI.

The `media` extra adds a wrinkle: `faster-whisper` runs on **ctranslate2**, which
wants **CUDA-12** cuBLAS/cuDNN/cudart, while torch's `cu132` build ships cuBLAS
**13** (a different major). So on Windows the `media` extra additionally installs
`nvidia-cublas-cu12`, `nvidia-cudnn-cu12`, and `nvidia-cuda-runtime-cu12`, and the
server prepends their `bin` directories to PATH before load (see
`extract._ensure_cuda_dll_path()` — `add_dll_directory` alone is not enough).
Verify with `uv run python scripts/verify-media-gpu.py`. On Linux, ctranslate2
resolves CUDA via the wheels' RPATH.

CLIP visual search runs CLIP on **CPU when ASR is active** (whisper's cu12 cuDNN
PATH-prepend otherwise shadows torch's cuDNN and breaks CLIP's Conv2d). Override
with `KB_MCP_CLIP_DEVICE=cuda`/`cpu` if needed.

## Revoke access

Pick the strongest option that fits the situation:

| Situation | Action |
|---|---|
| Suspect the GitHub OAuth grant is compromised | Revoke at <https://github.com/settings/applications> → find `kb-mcp` → Revoke. claude.ai's token dies on the next call (the verifier hits `api.github.com/user` per request). |
| Suspect the GitHub OAuth App secret leaked | Rotate the secret at <https://github.com/settings/developers> → `kb-mcp` → "Generate a new client secret". Update `GITHUB_CLIENT_SECRET` in `.env`, restart the service. |
| Want to disconnect just claude.ai | Delete the connector in claude.ai → Settings → Connectors. |
| Want to take the endpoint offline entirely | `tailscale funnel --https=443 off` (or stop the Cloudflare tunnel). The endpoint becomes unreachable from the public internet. |
| Want to stop the service but leave the public URL configured | Stop the service (e.g. elevated `Start-Process -Verb RunAs -Wait sc.exe -ArgumentList 'stop','kb-mcp'`). The tunnel stays up but proxies to nothing. |
| Want a clean uninstall | Stop + remove service, turn off the tunnel/Funnel, delete the connector in claude.ai, delete the GitHub OAuth App. |

## Restarting the service

**macOS / Linux:** `bash scripts/restart.sh` — launchd `kickstart -k` on macOS,
`systemctl --user restart` on Linux. It truncates `logs/kb-mcp.log` and tails it.

**Windows:** `install-service.ps1` grants your user account start/stop rights on
the service, so day-to-day restarts don't need UAC:

```powershell
sc.exe stop kb-mcp
sc.exe start kb-mcp
Get-Content logs\kb-mcp.log -Tail 6
```

If you skipped the grant (or installed from an older version of the script),
re-run the install script — it's idempotent and will only add the ACE if it's
missing.

For a stuck restart (orphan python processes holding port 8765), force-clean:

```powershell
sc.exe stop kb-mcp
Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force
sc.exe start kb-mcp
```

## Logs

- `logs/kb-mcp.log` — application log (rotated in-process, 5 MB × 5 files; same on
  every platform).
- `logs/service.out.log`, `logs/service.err.log` — service stdout/stderr. On
  Windows NSSM writes and rotates these; launchd/systemd write them but do **not**
  rotate them (the app's own `kb-mcp.log` is the durable, self-rotating record). On
  Linux, `journalctl --user -u kb-mcp` is the primary stdout/stderr view.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| claude.ai "Couldn't reach the MCP server" during connector add | OAuth discovery failed | `curl.exe -i https://<your-host>/.well-known/oauth-authorization-server` should return JSON. If 404, the OAuthProxy isn't mounted — most likely `KB_MCP_BASE_URL` has a trailing slash or includes `/mcp`. |
| GitHub redirects to "The redirect_uri MUST match…" error | OAuth App callback URL mismatch | At github.com/settings/developers → kb-mcp, set the callback to exactly `https://<your-host>/auth/callback` (no trailing slash). |
| GitHub: "The redirect_uri is not associated with this application" on a *second* machine | Reused another host's OAuth App client ID/secret (the app's one callback points at the other host) | Create a per-host OAuth App with callback `https://<this-host>.example.com/auth/callback`, put its client ID/secret in this `.env`, restart the service. See § Deploying on a second machine. |
| claude.ai connector connects but every tool call returns 401 | Wrong GitHub user | `KB_MCP_GITHUB_USERNAME` must equal the login of the GitHub account you authorized with. Check the kb-mcp log for `rejecting token for github login=...`. |
| claude.ai shows "connector failed" | service down (host asleep, service stopped, crash loop) | Check the service status; tail `logs/service.err.log` and `logs/kb-mcp.log`. Multiple startup banners within seconds = orphan python processes — kill them and force-restart. |
| Edits to `.env` not picked up | service didn't restart | Restart the service (elevated on Windows). Confirm the python process restarted. |
| 404 / Funnel "no service" | Tunnel disabled or pointing at the wrong port | `tailscale funnel status` (or check `cloudflared`); re-run the tunnel command from step 2. |
| `KB vault not found` on startup | vault path moved or `KB_MCP_VAULT_PATH` wrong | set `KB_MCP_VAULT_PATH` to the absolute vault root in `.env`. |
| Schema parse error on startup | `_Schema/references/frontmatter.md` shape changed | diff against the version that was working; the parser is conservative on purpose. |
| `add` fails with `INVALID_SOURCE` | missing required field (url for article/paper/video; non-empty content/title) | the error payload names the missing field; fix and retry. |

## Out of scope

The remote tier is intentionally minimal. Not included:

- Auth layers beyond single-user GitHub OAuth (no mTLS, IP allowlist, multi-user
  RBAC).
- Monitoring/metrics/observability beyond rotating file logs.
- Web UI.
- Multi-host failover / always-on home server (each host is independent; you pick
  which connector to invoke).
- Compiled-note creation from mobile (`add` only captures raw sources;
  compilation stays a desk-side / Claude Code flow).
