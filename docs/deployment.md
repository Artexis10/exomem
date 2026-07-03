# exomem — remote deployment

This guide covers the **remote tier**: running exomem as an always-on HTTP service
behind a public HTTPS endpoint so you can reach your vault from **claude.ai** on
the web or mobile as a custom connector. The local-only path (Claude Code over
stdio, no cloud) is in [../SETUP-LOCAL.md](../SETUP-LOCAL.md); start there if you
don't need mobile access.

Throughout, replace `<your-host>` / `example.com` with your own hostname.
For the guided ≤15-minute bring-up, start with
[remote-quickstart.md](remote-quickstart.md); this document is the reference.

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
cd /path/to/exomem

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
  `C:\Program Files\Tesseract-OCR\`, or set `EXOMEM_TESSERACT_CMD`.
- **ffmpeg** is bundled by PyAV (pulled via faster-whisper), so audio/video decode
  works without a separate install.

Verify the GPU media path: `uv run python scripts/verify-media-gpu.py`.

Lean / CPU-only boxes can skip all of this — set
`EXOMEM_DISABLE_MEDIA_EXTRACTION`; uploads still work, just without server-side
searchable-text extraction. CPU also works (no GPU needed), just slower — pick a
smaller Whisper model with `EXOMEM_WHISPER_MODEL=base`. The CUDA-12 wheels above
are Windows + GPU only, unused on CPU.

To make **existing** Evidence/media files searchable (one-shot back-fill; new
uploads are handled automatically), run:

```powershell
uv run python -m exomem backfill-media --dry-run   # preview (writes nothing)
uv run python -m exomem backfill-media             # do it (CPU or GPU)
```

It writes a sidecar if missing, extracts text (OCR/ASR/PDF), and CLIP-embeds
images. Idempotent. Flags: `--no-ocr` (sidecar + CLIP only), `--no-clip`,
`--vault <root>`.

## 2. Set up a public HTTPS URL

You need a hostname for step 3. Pick **one**:

**Option A — Cloudflare Tunnel.** Needs a domain you own in Cloudflare; more
burst-tolerant under load. Prereq:
`winget install --id Cloudflare.cloudflared`.

```powershell
cloudflared tunnel login
pwsh -File scripts/setup-cloudflared.ps1 -Hostname kb.example.com -TunnelName exomem-host
#   -> https://kb.example.com (script makes the tunnel + DNS + auto-start service).
```

In the Cloudflare dashboard for this hostname: Bot Fight Mode **OFF** + no WAF
managed ruleset (Security Level low); the edge caps requests at ~100s.

**Option B — ngrok.** No domain needed; every ngrok account gets one free static
dev domain. Claim it in the ngrok dashboard, then:

```powershell
ngrok config add-authtoken <your-authtoken>
ngrok http 8765 --url https://<you>.ngrok-free.dev
```

For an always-on Windows service instead of a foreground process, use
`pwsh -File scripts/setup-ngrok.ps1 -Domain <you>.ngrok-free.dev -Port 8765`.

Free-tier limits: 120 requests/min, ~20k requests/month, and a one-time browser
interstitial on first visit (click through once; it does not recur). Fine for
one person; upgrade or switch to Cloudflare if you outgrow the cap.

**Why not Tailscale Funnel?** Funnel is beta, has non-configurable bandwidth
caps, and its shared relay throttles claude.ai's connector request bursts —
producing a "looks disconnected" failure that has nothing to do with exomem
itself. It is no longer the recommended no-domain default (ngrok's static
domain is more burst-tolerant), but existing Funnel deployments keep working.
If you're already running it:

```powershell
tailscale funnel --bg --https=443 http://127.0.0.1:8765
tailscale funnel status        # note the URL, e.g. https://<device>.<tailnet>.ts.net
```

## 3. Create a GitHub OAuth App (one-time, ~3 min)

At <https://github.com/settings/developers> → **OAuth Apps** → **New OAuth App**:

| Field | Value |
|---|---|
| Application name | `exomem` |
| Homepage URL | `https://kb.example.com` |
| Authorization callback URL | `https://kb.example.com/auth/callback` |

Save the generated **Client ID** and **Client Secret**.

## 4. Populate `.env`

Create `.env` in the repo root:

```
EXOMEM_BASE_URL=https://kb.example.com
EXOMEM_GITHUB_USERNAME=<your-github-login>
GITHUB_CLIENT_ID=<from step 3>
GITHUB_CLIENT_SECRET=<from step 3>
# Recommended: a long random string that pins the OAuth signing key, so the
# claude.ai connector survives FastMCP upgrades / client-secret rotation without
# re-authorizing. Generate: python -c "import secrets;print(secrets.token_urlsafe(48))"
EXOMEM_JWT_SIGNING_KEY=<long-random-string>
# Required: vault root — the folder that contains Knowledge Base/
EXOMEM_VAULT_PATH=<your-Obsidian-vault-root>
```

`EXOMEM_BASE_URL` must match your public hostname (from step 2) exactly — no
trailing slash, no `/mcp` suffix. `EXOMEM_GITHUB_USERNAME` is case-insensitive but
must be the *login*, not the display name. `EXOMEM_VAULT_PATH` is **required**:
claude.ai connects over HTTP and passes no environment, so the service resolves the
vault solely from this line in `.env` at startup.

## 5. Sanity-test locally

```powershell
# Configuration profile check
uv run python -m exomem doctor --profile remote

# stdio (no auth needed)
uv run python -m exomem --transport stdio
# Ctrl-C to stop

# HTTP (OAuth required)
uv run python -m exomem --transport streamable-http --host 127.0.0.1 --port 8765
# In another terminal:
#   curl.exe -i http://127.0.0.1:8765/mcp                      → expect 401
#   curl.exe -i http://127.0.0.1:8765/.well-known/oauth-authorization-server
#                                                              → expect JSON metadata
```

Once the tunnel is up (step 2) and `.env` is populated (step 4), `exomem doctor
--profile remote --probe` runs the equivalent checks above — plus the public
OAuth-discovery and bare well-known endpoints through the live tunnel — in one
command.

## 6. Install as a service (auto-start on boot)

**Option 0 (container):** `docker compose --profile cloudflared up -d` (or
`--profile ngrok`) runs the server plus the tunnel as one supervised unit —
see [docker.md](docker.md). The native services below suit hosts where Docker
isn't wanted, Windows vaults (WSL2 bind mounts miss live file-watch events),
and GPU setups.

Pick your platform — all three run the same `streamable-http` server and differ
only in the OS service manager.

**macOS (launchd):**

```bash
bash scripts/install-service.sh
# Restart after .env edits:  bash scripts/restart.sh
# Uninstall:                 launchctl bootout gui/$(id -u)/com.exomem && rm ~/Library/LaunchAgents/com.exomem.plist
```

**Linux (systemd --user):**

```bash
mkdir -p ~/.config/systemd/user
sed -e "s|__REPO_ROOT__|$PWD|g" -e "s|__VENV_PYTHON__|$PWD/.venv/bin/python|g" scripts/exomem.service > ~/.config/systemd/user/exomem.service
systemctl --user daemon-reload && systemctl --user enable --now exomem
loginctl enable-linger "$USER"   # keep it running without an active login session
# Restart after .env edits:  systemctl --user restart exomem   (or: bash scripts/restart.sh)
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
#   nssm stop exomem && nssm remove exomem confirm
# Restart (after .env edits): elevated shell required
#   Start-Process -Verb RunAs -Wait sc.exe -ArgumentList 'stop','exomem'
#   Start-Process -Verb RunAs -Wait sc.exe -ArgumentList 'start','exomem'
```

### Renaming an existing `kb-mcp` service

Boxes provisioned **before the `kb-mcp` → `exomem` rename** still run the Windows
service under the old name `kb-mcp`. The code (and `install-service.ps1` /
`restart.ps1`, which now default to `exomem`) are renamed, but the installed NSSM
service isn't — it must be re-registered once per box. `restart.ps1` falls back to
the legacy `kb-mcp` name automatically so it keeps working until you migrate; to
finish the rename, run these in an **elevated** PowerShell (the old service must be
removed first or it collides with the new one on port 8765):

```powershell
sc.exe stop kb-mcp
nssm remove kb-mcp confirm
pwsh -File scripts/install-service.ps1   # installs + starts 'exomem', re-grants no-UAC rights
```

The cloudflared tunnel/funnel targets the **port** (127.0.0.1:8765), not the
service name, so it keeps working across the rename. Verify with
`sc.exe query exomem` (RUNNING) and `sc.exe query kb-mcp` (should not exist).

## 7. Add to claude.ai

1. claude.ai → Settings → Connectors → **Add custom connector**
2. **Name**: `Knowledge Base` (or whatever)
3. **Server URL**: `https://kb.example.com/mcp` (this host's public hostname)
4. Leave **OAuth Client ID** and **OAuth Client Secret** blank — claude.ai uses
   Dynamic Client Registration against your `/register` endpoint.
5. Save. claude.ai opens a GitHub login window → log in (only the user in
   `EXOMEM_GITHUB_USERNAME` is allowed) → approve consent → redirects back to
   claude.ai. The tools appear in the palette.

## Deploying on a second machine (multi-host)

Each machine is an independent deployment — there is no shared state. To run exomem
on a second box (e.g. a laptop alongside a desktop), repeat the install with that
host's *own* values. The non-obvious parts:

- **Its own public hostname.** `EXOMEM_BASE_URL` and the connector URL are
  per-host. Tailscale gives each node a distinct `<node>.<tailnet>.ts.net`
  automatically (`tailscale funnel status`); for Cloudflare, give each host a
  distinct subdomain (e.g. `kb.example.com`, `kb-laptop.example.com`) via
  `pwsh -File scripts/setup-cloudflared.ps1 -Hostname <this-host> -TunnelName <unique-name>`.
  ngrok's free tier gives one static dev domain **per account**, not per host —
  a second host needs Cloudflare (or a paid ngrok domain) for its own hostname.
- **Its own GitHub OAuth App.** A GitHub OAuth App allows exactly **one**
  Authorization callback URL, so you *cannot* reuse another host's app — its
  callback points at the other host and GitHub rejects the redirect with "The
  redirect_uri is not associated with this application." Create a second app (e.g.
  `exomem (laptop)`) with callback `https://<this-host>.example.com/auth/callback`
  and put *its* `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` in this machine's
  `.env`.
- **Its own `.env` and connector.** Set `EXOMEM_VAULT_PATH` to this machine's vault
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

## GPU notes (CUDA / Blackwell / Apple Silicon MPS)

Blackwell GPUs (RTX 50-series, compute capability 12.0 / `sm_120`) need CUDA
wheels. Default PyPI torch on Windows is **still CPU-only**, so `pyproject.toml`
pins an explicit CUDA index (`pytorch-cu132`, CUDA 13.2) whose wheel ships
`sm_120` — `torch.cuda.get_arch_list()` includes it, so any RTX 50-series GPU
works. The index is scoped to win32/linux via a platform marker; macOS falls back
to default PyPI — whose arm64 torch wheel **includes the MPS (Metal) backend**, so
Apple Silicon gets GPU acceleration for the torch models with **no extra wheels**
(see "Apple Silicon" below).

**Apple Silicon (MPS / Metal).** Device selection is centralized in
`exomem.accel.select_device` (priority CUDA → MPS → CPU). The torch models — bge
text embedder, bge reranker, and CLIP — auto-select the Metal GPU when no CUDA is
present, so interactive `find`, note-write embedding, and CLIP image search run on
the GPU on an M-series Mac. `PYTORCH_ENABLE_MPS_FALLBACK=1` is set automatically so
any op MPS lacks degrades to CPU instead of raising.

**ASR on the Metal GPU (`media-mlx` extra).** faster-whisper/ctranslate2 has no Metal
backend, but ASR runs behind a `TranscriptionBackend` seam (`extract.get_transcriber`)
with two implementations: `FasterWhisperBackend` (CUDA/CPU) and `MlxWhisperBackend`
(**mlx-whisper**, Metal GPU). Install the extra — `uv sync --extra media --extra
media-mlx` — and `get_transcriber()` **auto-selects MLX on Apple Silicon**, putting
transcription on the GPU too. `EXOMEM_ASR_BACKEND=mlx|faster-whisper` forces the choice;
`EXOMEM_MLX_WHISPER_MODEL` picks the HF repo (default `mlx-community/whisper-large-v3-mlx`;
use `mlx-community/whisper-large-v3-turbo` for a lighter, faster run on a fanless Air).
Without the extra, transcription falls back to CPU faster-whisper — pick a smaller
`EXOMEM_WHISPER_MODEL` (e.g. `base`) there to cut cost. Audio is decoded via PyAV (the
shared 16 kHz whisper timebase) when the `media` extra is present, else mlx-whisper
decodes the file itself (needs `ffmpeg` on PATH).

On MPS, bge/CLIP also run in **fp16** by default (`EXOMEM_MPS_FP16`, set `0` to keep
fp32) — roughly half the memory and faster encodes, which these retrieval models
tolerate well. Storage is unchanged (vectors are upcast to float32 before the sqlite
blob); existing fp32 vectors differ from new fp16 ones by ~1e-3, harmless for ranking,
and `audit_fix(rebuild_embeddings=True)` re-embeds for exact consistency if wanted.

Two more notes: **Voiceprints** (ECAPA) and **diarization** stay on CPU by default for
cross-machine numeric parity — opt in per model with `EXOMEM_VOICE_DEVICE=mps` /
`EXOMEM_DIARIZE_DEVICE=mps`. And you can force every torch model to a device with
`EXOMEM_TORCH_DEVICE=cpu|mps|cuda` (handy to dodge thermal throttling on a fanless
MacBook Air during a large `backfill-media`).

The `media` extra adds a wrinkle: `faster-whisper` runs on **ctranslate2**, which
wants **CUDA-12** cuBLAS/cuDNN/cudart, while torch's `cu132` build ships cuBLAS
**13** (a different major). So on Windows the `media` extra additionally installs
`nvidia-cublas-cu12`, `nvidia-cudnn-cu12`, and `nvidia-cuda-runtime-cu12`, and the
server prepends their `bin` directories to PATH before load (see
`extract._ensure_cuda_dll_path()` — `add_dll_directory` alone is not enough).
Verify with `uv run python scripts/verify-media-gpu.py`. On Linux, ctranslate2
resolves CUDA via the wheels' RPATH.

CLIP visual search runs CLIP on **CPU when ASR is active on CUDA** (whisper's cu12
cuDNN PATH-prepend otherwise shadows torch's cuDNN and breaks CLIP's Conv2d). This
clash is CUDA-only, so on Apple Silicon CLIP keeps the **MPS** GPU even with ASR
running. Override with `EXOMEM_CLIP_DEVICE=cuda`/`mps`/`cpu` if needed.

Zero-shot **image tags** (`EXOMEM_IMAGE_TAGS`, default off) reuse that same loaded
CLIP model — no extra dependency. When set, each extracted image is cosine-scored
against a fixed generic tag vocabulary and the top matches are appended to its
indexed text as a `Tags: invoice, table, screenshot` line, so a photo is findable
by what it depicts (not just its OCR text). It is a frozen cosine measurement (no
LLM), inherits the CLIP device logic above, and soft-fails to no tags when CLIP is
absent. Tune with `EXOMEM_IMAGE_TAGS_TOPK` (default 5) and
`EXOMEM_IMAGE_TAGS_THRESHOLD` (raw cosine, default 0.22); only newly-extracted
images are tagged.

**Video scene frames** (`EXOMEM_VIDEO_SCENE_FRAMES`, default off) upgrade video
indexing from uniform-interval keyframes to visual-change scene detection, and
persist one representative JPEG per scene in a `<video>.frames/` directory next to
the video (each with a sidecar pointing back at the parent + timestamp). Frames
ride the normal image OCR path, so on-screen text (slides, stack traces) becomes
keyword-findable at its timestamp; `find` groups frame matches under the parent
video's hit and surfaces `scene_frame` + `scene_match_at`. Detection is a cheap
I-frame-only metrics pass (PyAV `skip_frame NONKEY`, 64×64 grayscale hash +
histogram) — no new dependency, and it soft-fails back to the uniform sampler.
The worker budget stays modest: decoding I-frames of an hour-long 1080p video
takes seconds, plus at most `EXOMEM_MAX_VIDEO_KEYFRAMES` (40) full-res seeks.
Tune boundaries with `EXOMEM_VIDEO_SCENE_THRESHOLD` (hash bits of 64, default 10)
and `EXOMEM_VIDEO_SCENE_MIN_SECS` (default 4). To upgrade already-indexed videos,
run `exomem backfill-media` with the flag set — idempotent, and it replaces the
old uniform CLIP rows with scene-aware ones.

**Semantic video segments** (`EXOMEM_SEMANTIC_SEGMENTS`, default off) make the
moment the retrieval unit for audio/video. Transcripts render as one timed line
per ASR segment (`[51:20] …`, diarized `[51:20] [Alice]: …`), and the embedding
chunker segments them at fused topic boundaries — bge similarity valleys plus
scene-change, speaker-turn, and OCR-change events from the persisted scene
frames. A transcript match then surfaces `transcript_match_at` on the hit with
the nearest scene frame attached. Pure measurement, no LLM, no new dependency;
gate off is byte-identical. The worker re-embeds a video's sidecar once after
its frame OCRs so segments see all signals (bounded, off the request path).
Upgrade existing recordings with `exomem backfill-media --retime` (opt-in —
re-runs ASR; combine with `--rediarize` to gain both markers in one pass).

**Install:** `uv sync --extra media --extra diarization`, then build the isolated
sidecar with `pwsh -File scripts/setup-diarizer.ps1 -Prewarm` on Windows or
`uv sync --directory sidecar/diarizer` on Linux/macOS.

Named-speaker diarization's ECAPA voice embedder (`diarization` extra) runs on
torch and follows the same precedent: it defaults to **CPU when ASR is active** (and
to CPU on Apple Silicon for cross-machine voiceprint parity), with a
`EXOMEM_VOICE_DEVICE=cuda`/`mps`/`cpu` override. Enroll voices with
`exomem enroll-speaker --name <name> [--self] <sample.wav>` (profiles live in a local
`.voice_profiles.json` beside the embedding sidecar; `list-speakers` / `remove-speaker`
manage them). With ≥1 profile enrolled and `EXOMEM_DIARIZE` set, matched clusters render
as `[<name>]: …`; unknown voices stay anonymous. The ECAPA checkpoint
(`speechbrain/spkrec-ecapa-voxceleb`, override `EXOMEM_VOICE_EMBED_MODEL`) is gated like
pyannote's — set `HUGGINGFACE_TOKEN`. Attribution thresholds are tunable via
`EXOMEM_VOICE_MARGIN`, `EXOMEM_VOICE_MERGE_THRESHOLD`, `EXOMEM_VOICE_CONFIDENT_DELTA`, and
`EXOMEM_VOICE_REL_GAP` (defaults match the shipped evidence-based values). The whole path is
default-off + soft-fail: with no profiles or the dep absent it degrades to today's anonymous
`[Speaker A]: …` output.

## Revoke access

Pick the strongest option that fits the situation:

| Situation | Action |
|---|---|
| Suspect the GitHub OAuth grant is compromised | Revoke at <https://github.com/settings/applications> → find `exomem` → Revoke. claude.ai's token dies on the next call (the verifier hits `api.github.com/user` per request). |
| Suspect the GitHub OAuth App secret leaked | Rotate the secret at <https://github.com/settings/developers> → `exomem` → "Generate a new client secret". Update `GITHUB_CLIENT_SECRET` in `.env`, restart the service. |
| Want to disconnect just claude.ai | Delete the connector in claude.ai → Settings → Connectors. |
| Want to take the endpoint offline entirely | `tailscale funnel --https=443 off` (or stop the Cloudflare tunnel). The endpoint becomes unreachable from the public internet. |
| Want to stop the service but leave the public URL configured | Stop the service (e.g. elevated `Start-Process -Verb RunAs -Wait sc.exe -ArgumentList 'stop','exomem'`). The tunnel stays up but proxies to nothing. |
| Want a clean uninstall | Stop + remove service, turn off the tunnel/Funnel, delete the connector in claude.ai, delete the GitHub OAuth App. |

## Restarting the service

**macOS / Linux:** `bash scripts/restart.sh` — launchd `kickstart -k` on macOS,
`systemctl --user restart` on Linux. It truncates `logs/exomem.log` and tails it.

**Windows:** `install-service.ps1` grants your user account start/stop rights on
the service, so day-to-day restarts don't need UAC:

```powershell
sc.exe stop exomem
sc.exe start exomem
Get-Content logs\exomem.log -Tail 6
```

If you skipped the grant (or installed from an older version of the script),
re-run the install script — it's idempotent and will only add the ACE if it's
missing.

On a box still running the pre-rename `kb-mcp` service, `scripts/restart.ps1`
auto-falls back to that name; use `sc.exe stop/start kb-mcp` for the manual form,
and see [Renaming an existing `kb-mcp` service](#renaming-an-existing-kb-mcp-service)
to migrate it to `exomem`.

For a stuck restart (orphan python processes holding port 8765), force-clean:

```powershell
sc.exe stop exomem
Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force
sc.exe start exomem
```

## Logs

- `logs/exomem.log` — application log (rotated in-process, 5 MB × 5 files; same on
  every platform).
- `logs/service.out.log`, `logs/service.err.log` — service stdout/stderr. On
  Windows NSSM writes and rotates these; launchd/systemd write them but do **not**
  rotate them (the app's own `exomem.log` is the durable, self-rotating record). On
  Linux, `journalctl --user -u exomem` is the primary stdout/stderr view.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| claude.ai "Couldn't reach the MCP server" during connector add | OAuth discovery failed | `curl.exe -i https://<your-host>/.well-known/oauth-authorization-server` should return JSON (or run `exomem doctor --profile remote --probe`, which checks this plus the bare well-known path). If 404, the OAuthProxy isn't mounted — most likely `EXOMEM_BASE_URL` has a trailing slash or includes `/mcp`. |
| GitHub redirects to "The redirect_uri MUST match…" error | OAuth App callback URL mismatch | At github.com/settings/developers → exomem, set the callback to exactly `https://<your-host>/auth/callback` (no trailing slash). |
| GitHub: "The redirect_uri is not associated with this application" on a *second* machine | Reused another host's OAuth App client ID/secret (the app's one callback points at the other host) | Create a per-host OAuth App with callback `https://<this-host>.example.com/auth/callback`, put its client ID/secret in this `.env`, restart the service. See § Deploying on a second machine. |
| claude.ai connector connects but every tool call returns 401 | Wrong GitHub user | `EXOMEM_GITHUB_USERNAME` must equal the login of the GitHub account you authorized with. Check the exomem log for `rejecting token for github login=...`. |
| claude.ai shows "connector failed" | service down (host asleep, service stopped, crash loop) | Check the service status; tail `logs/service.err.log` and `logs/exomem.log`. Multiple startup banners within seconds = orphan python processes — kill them and force-restart. |
| Edits to `.env` not picked up | service didn't restart | Restart the service (elevated on Windows). Confirm the python process restarted. |
| 404 / Funnel "no service" | Tunnel disabled or pointing at the wrong port | `tailscale funnel status` (or check `cloudflared`); re-run the tunnel command from step 2. |
| `KB vault not found` on startup | vault path moved or `EXOMEM_VAULT_PATH` wrong | set `EXOMEM_VAULT_PATH` to the absolute vault root in `.env`. |
| Schema parse error on startup | `_Schema/references/frontmatter.md` shape changed | diff against the version that was working; the parser is conservative on purpose. |
| Connector setup stalls mid-burst behind ngrok | Free-tier rate limit (120 req/min) | Check the ngrok agent console for `429`s. Wait and retry, or upgrade / switch to Cloudflare Tunnel. |
| OAuth redirect hangs the first time behind ngrok free | The one-time browser interstitial | Open the public URL once in a browser and click through the interstitial; it does not recur. |
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
