# Remote checklist

Use this as the short bring-up list for claude.ai web/mobile access. The complete
walkthrough and troubleshooting are in [deployment.md](deployment.md).

## 1. Local checkout works

```bash
uv sync
uv run python scripts/smoke-sample-vault.py
uv run python -m exomem doctor --vault "/path/to/your/Obsidian" --profile lean
```

For semantic search, install the extra and validate it before remote setup:

```bash
uv sync --extra embeddings
uv run python -m exomem doctor --vault "/path/to/your/Obsidian" --profile hybrid
```

For server-side media extraction:

```bash
uv sync --extra embeddings --extra media
uv run python -m exomem doctor --vault "/path/to/your/Obsidian" --profile media
```

Install Tesseract separately if the media doctor reports it missing.

## 2. Public URL exists

Choose one:

- Tailscale Funnel: simplest if you do not own a domain.
- Cloudflare Tunnel: better if you already own a domain and want a dedicated
  hostname.

The URL must be publicly reachable from Anthropic's cloud, not just from your
phone or tailnet.

## 3. GitHub OAuth app is configured

Create one OAuth app per public hostname:

- Homepage URL: `https://<your-host>`
- Authorization callback URL: `https://<your-host>/auth/callback`

Do not reuse another machine's OAuth app unless it uses the exact same hostname.

## 4. `.env` has the remote variables

Required for remote:

```text
EXOMEM_BASE_URL=https://<your-host>
EXOMEM_GITHUB_USERNAME=<your-github-login>
GITHUB_CLIENT_ID=<from GitHub>
GITHUB_CLIENT_SECRET=<from GitHub>
EXOMEM_JWT_SIGNING_KEY=<long random string>
EXOMEM_VAULT_PATH=<absolute vault root>
```

`EXOMEM_BASE_URL` has no trailing slash and no `/mcp` suffix.

## 5. Doctor passes

```bash
uv run python -m exomem doctor --profile remote
```

This validates the env shape and remote prerequisites before claude.ai tries to
register the connector.

## 6. Service and connector are live

1. Start the streamable HTTP service on `127.0.0.1:8765`.
2. Start the tunnel/Funnel to the same local port.
3. Add a claude.ai custom connector at `https://<your-host>/mcp`.
4. Complete the GitHub OAuth login as the account named in
   `EXOMEM_GITHUB_USERNAME`.

After any `.env` edit, restart the service. The server reads the file at startup.
