# exomem — Docker

The fastest way to try exomem with Claude Code: no `uv`, no Python, no clone.

```bash
claude mcp add exomem -- docker run -i --rm -v "/path/to/vault:/vault" -e EXOMEM_VAULT_PATH=/vault ghcr.io/artexis10/exomem:latest --transport stdio
```

Replace `/path/to/vault` with your Obsidian/markdown vault's absolute path.
Claude Code launches the container per session and talks MCP over stdio — no
HTTP port, no OAuth, nothing to keep running in the background.

For an always-on remote deployment (mobile/web access via claude.ai), skip to
[Remote compose deployment](#remote-compose-deployment) below. See
[deployment.md](deployment.md) for the architecture this container fits into
— this doc is the Docker-specific path through it.

## Tags

| Tag | Variant | Search mode | Uncompressed size (approx.) |
| --- | --- | --- | --- |
| `latest`, `X.Y.Z` | `lean` | keyword/BM25 only, no torch | ~250–350 MB |
| `ml`, `X.Y.Z-ml` | `ml` | hybrid embeddings + CLIP search, CPU-only torch | ~1.5–2 GB |

`lean` is the default and is what the stdio one-liner above pulls — no torch,
no model download, matching a fast zero-Python onboarding path. Opt into
hybrid search with the `ml` tag (`ghcr.io/artexis10/exomem:ml`); the first
`find` call downloads the embedding/CLIP model weights into the `/data` volume
(see below), so later restarts don't re-download them.

**GPU is out of scope for both variants.** `ml` runs CPU-only torch — no CUDA
runtime is bundled, and none is documented for this image. If you need GPU
throughput, use the native install instead ([../SETUP-LOCAL.md](../SETUP-LOCAL.md),
[deployment.md](deployment.md)'s CUDA/GPU notes).

## Volume contract

- **`/vault`** — your notes. Bind-mount your vault root here
  (`-v "/path/to/vault:/vault"`) and set `EXOMEM_VAULT_PATH=/vault`. All file
  reads and writes stay confined to this mount, matching the server's
  existing vault-containment guarantee.
- **`/data`** — server-local state, declared as a `VOLUME` in the image:
  - `EXOMEM_LOG_DIR=/data/logs` — the rotating `exomem.log`. The
    `queries.jsonl` / `writes.jsonl` / `reads.jsonl` audit logs land here too,
    but only in the `ml` image (the lean image runs with embeddings disabled,
    which also turns the query log off).
  - `ml` only: `HF_HOME=/data/hf` — downloaded embedding/CLIP model weights.

  Mount a named volume (or bind mount) at `/data` so logs and model weights
  survive container restarts instead of resetting every time. `/data` is
  deliberately a *separate* volume from `/vault`: you can wipe server state
  (`docker volume rm`) without touching your vault, since they have distinct
  lifecycles.

## Remote compose deployment

The root [`compose.yaml`](../compose.yaml) runs exomem as an always-on HTTP
service with optional tunnel-sidecar ingress, matching the remote tier
documented in [deployment.md](deployment.md).

1. Clone the repo — compose needs `compose.yaml` and a filled-in `.env`; it
   pulls the published image, so no local Python/`uv` install is required:

   ```bash
   git clone https://github.com/Artexis10/exomem.git
   cd exomem
   cp .env.example .env
   ```

2. Fill in `.env`:
   - The OAuth vars from [deployment.md](deployment.md): `EXOMEM_BASE_URL`,
     `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `EXOMEM_GITHUB_USERNAME`,
     `EXOMEM_JWT_SIGNING_KEY`.
   - The compose-only keys (not yet in `.env.example` — append them):

     ```bash
     printf '\nEXOMEM_VAULT_HOST_PATH=/path/to/your/vault\n' >> .env
     ```

     `EXOMEM_VAULT_HOST_PATH` is the **host** path bind-mounted to `/vault`
     (`compose.yaml` maps it in; the container always sees `/vault`). Compose
     fails fast with `set in .env` if it is missing.
   - Whichever tunnel credential you're using, appended the same way:
     `CLOUDFLARE_TUNNEL_TOKEN`, or `NGROK_AUTHTOKEN` + `NGROK_DOMAIN`.

3. Bring it up with the tunnel profile you have an account for:

   ```bash
   docker compose --profile ngrok up -d
   # or
   docker compose --profile cloudflared up -d
   ```

   With **no** `--profile` flag, only the `exomem` service starts: no host
   port published, no tunnel sidecar running — useful for a LAN-only or
   locally-debugged setup. Uncomment the `ports:` line in `compose.yaml` to
   bind `127.0.0.1:8765` for local access.

4. Verify:

   ```bash
   docker compose exec exomem exomem doctor --profile remote
   ```

`docker compose ps` reports `exomem` healthy once
`GET /.well-known/oauth-protected-resource` (the same unauthenticated route
claude.ai uses for OAuth discovery) returns `200` — no separate `/health`
endpoint exists.

## Hardening

Run the container as a non-root user:

```bash
docker run --user "$(id -u):$(id -g)" ...
```

This works out of the box because both persistence paths point at `/data`
(`EXOMEM_LOG_DIR`, and on `ml`, `HF_HOME`) — as long as your user has write
access to the mounted `/data` volume, nothing in the image requires root. With
a **bind mount** at `/data` that's automatic (you own the host directory); a
Docker **named volume** (what `compose.yaml` uses) is initialized root-owned,
so pre-chown it once (`docker run --rm -v exomem-data:/data alpine chown -R 1000:1000 /data`)
before running non-root against it. The
image does not impose a fixed `USER`, so you can pick the uid/gid that matches
your host's volume ownership.

## Windows caveat

If you're on Windows, prefer the native install
([../SETUP-LOCAL.md](../SETUP-LOCAL.md)) over Docker for day-to-day editing.
Docker Desktop bind-mounts a `C:\` path through WSL2, and that path does
**not** propagate `inotify` events — exomem's live file-watcher (which
re-embeds out-of-band `.md` edits) will silently miss changes made outside the
container. NTFS-backed WSL2 mounts are also noticeably slower than a native
filesystem for the same reason. Docker remains a good choice for a Linux/macOS
host, or for the remote-compose walkthrough above running on a remote Linux
box.
