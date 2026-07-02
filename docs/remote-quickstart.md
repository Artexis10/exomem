# exomem — remote connector quickstart

**What you get:** exomem on your phone and on claude.ai's web app, via a
remote MCP connector — the same vault you already use locally, reachable from
anywhere.

**What you need:** a machine that stays on (desktop, home server, or a cheap
VPS) and about **15 minutes**.

This replaces the old `docs/remote-checklist.md`. It's the fast path; the full
walkthrough, architecture diagram, GPU/CUDA notes, and complete
troubleshooting table live in [deployment.md](deployment.md) — this doc links
back to it rather than duplicating it.

## Pick an ingress profile

Your machine needs a public HTTPS URL that claude.ai's cloud infrastructure
can reach — not just your phone or tailnet (see
[deployment.md § Architecture](deployment.md#architecture) for why a purely
internal hostname doesn't work). Pick one:

| Profile | Best for | Setup |
| --- | --- | --- |
| **Cloudflare Tunnel** (recommended) | You own a domain | Scripted: [`scripts/setup-cloudflared.ps1`](../scripts/setup-cloudflared.ps1) + [`deploy/cloudflared/RUNBOOK.md`](../deploy/cloudflared/RUNBOOK.md) |
| **ngrok** | No domain | Every account gets one free static dev domain. Scripted on Windows: [`scripts/setup-ngrok.ps1`](../scripts/setup-ngrok.ps1); two commands on macOS/Linux |
| **SSH reverse tunnel** | Fallback — bring your own VPS | One `ssh -R` command against a VPS you already control |

> **Why not Tailscale Funnel?** It used to be the default no-domain
> recommendation here. It's still beta, has non-configurable bandwidth
> limits, and — the part that actually bites — its shared relay throttles the
> request bursts claude.ai's connector makes during registration and
> reconnects. That looks like "the server is down" on claude.ai's side while
> exomem itself is completely healthy. If you already run Funnel
> successfully, nothing forces you to move; new setups should start with
> ngrok or Cloudflare instead.

## Steps

### 1. Create a GitHub OAuth App

At <https://github.com/settings/developers> → **OAuth Apps** → **New OAuth
App**. `<host>` below is whichever domain / ngrok dev domain / VPS hostname
you land on in step 4:

| Field | Value |
| --- | --- |
| Application name | `exomem` |
| Homepage URL | `https://<host>` |
| Authorization callback URL | `https://<host>/auth/callback` |

The callback path is exactly `/auth/callback` — see
[deployment.md § 3](deployment.md#3-create-a-github-oauth-app-one-time-3-min).
Save the generated **Client ID** and **Client Secret**.

Validate — GitHub 404s the authorize endpoint for a mistyped client id, so a
non-404 here confirms it was copied correctly:

```bash
curl -s -o /dev/null -w "%{http_code}\n" "https://github.com/login/oauth/authorize?client_id=<client-id-from-step-1>"
```

### 2. Populate `.env`

Create `.env` in the repo root:

```text
EXOMEM_BASE_URL=https://<host>
GITHUB_CLIENT_ID=<from step 1>
GITHUB_CLIENT_SECRET=<from step 1>
EXOMEM_GITHUB_USERNAME=<your-github-login>
EXOMEM_JWT_SIGNING_KEY=<long-random-string>
EXOMEM_VAULT_PATH=<your-Obsidian-vault-root>
```

Generate `EXOMEM_JWT_SIGNING_KEY`:

```bash
python -c "import secrets;print(secrets.token_urlsafe(48))"
```

`EXOMEM_BASE_URL` has no trailing slash and no `/mcp` suffix. `EXOMEM_VAULT_PATH`
is required: claude.ai connects over HTTP and passes no environment of its
own, so the service resolves the vault solely from `.env` at startup.

Validate:

```bash
exomem doctor --profile remote
```

### 3. Start the server

For always-on service install (auto-start on boot), pick your OS — see
[deployment.md § 6](deployment.md#6-install-as-a-service-auto-start-on-boot)
for the full commands: macOS (`bash scripts/install-service.sh`), Linux
(`systemctl --user enable --now exomem` after templating
`scripts/exomem.service`), Windows (`pwsh -File scripts/install-service.ps1`).
Or run it in the foreground for a first test:

```bash
exomem --transport http
```

Validate — expect `401`, not a connection error:

```bash
curl -i http://127.0.0.1:8765/mcp
```

### 4. Bring the tunnel up

**Cloudflare Tunnel** (after a one-time, browser-interactive
`cloudflared tunnel login`):

```bash
pwsh -File scripts/setup-cloudflared.ps1 -Hostname kb.example.com -TunnelName exomem-host
```

**ngrok** — your free static dev domain is auto-assigned to your account
(find it at <https://dashboard.ngrok.com> under Universal Gateway →
Domains), then configure the authtoken once:

```bash
ngrok config add-authtoken <token>
```

Then either run it in the foreground for a first test:

```bash
ngrok http --url https://<you>.ngrok-free.dev 8765
```

or install it as an auto-starting Windows service:

```bash
pwsh -File scripts/setup-ngrok.ps1 -Domain <you>.ngrok-free.dev
```

macOS/Linux: `brew install ngrok`, then the same foreground command above —
no script needed for two commands.

**SSH reverse tunnel** — bring your own VPS with a TLS-terminating reverse
proxy already pointed at `127.0.0.1:8765` (Caddy's automatic HTTPS is the
simplest: `your-domain.example.com { reverse_proxy 127.0.0.1:8765 }`):

```bash
ssh -N -R 127.0.0.1:8765:127.0.0.1:8765 you@your-vps
```

Validate — from outside your network, confirm the public host answers:

```bash
curl -I https://<host>/mcp
```

### 5. Verify the triple

This is the primary check — run it once the tunnel is up, so a broken
connector path shows up now instead of when claude.ai's connector add fails:

```bash
exomem doctor --profile remote --probe
```

The curl equivalents it runs under the hood, for anyone who wants to see the
raw requests:

```bash
curl -i http://127.0.0.1:8765/mcp
```

→ `401` (server up, auth enforced).

```bash
curl -i https://<host>/.well-known/oauth-authorization-server
```

→ `200` JSON (OAuth discovery resolves through the tunnel).

```bash
curl -i https://<host>/.well-known/oauth-protected-resource
```

→ `200` JSON with `"resource":"https://<host>/mcp"`. This is the **bare**
path — claude.ai's gateway probes it before following the standard
`resource_metadata` pointer, and a `404` here aborts connector registration
with `mcp_registration_failed`. exomem ships a workaround route for exactly
this; this check proves the route is live through your actual tunnel, not
just importable.

### 6. Add the connector

claude.ai → **Settings** → **Connectors** → **Add custom connector** →
`https://<host>/mcp` → log in with GitHub as the account named in
`EXOMEM_GITHUB_USERNAME`.

## ngrok free-tier limits

- **120 requests/minute**, **~20,000 requests/month** per endpoint.
- A **one-time browser interstitial** the first time anything hits a fresh
  ngrok session — click through it once. If the GitHub OAuth redirect stalls
  during connector setup, this is usually why.
- Fine for one person's connector traffic. If setup stalls mid-burst, check
  the [ngrok dashboard](https://dashboard.ngrok.com/) for `429`s on the
  endpoint. Cloudflare Tunnel or a paid ngrok plan removes the cap.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| claude.ai connector add fails with `mcp_registration_failed` | Bare `/.well-known/oauth-protected-resource` 404s through the tunnel | Re-run step 5's probe; if it fails, the tunnel isn't forwarding to the right port, or exomem needs updating. |
| ngrok setup stalls, then a `429` shows in the ngrok dashboard | Free-tier burst limit (120 req/min) hit during registration | Wait a minute and retry, or switch to Cloudflare Tunnel. |
| GitHub OAuth redirect never completes on first ngrok use | The one-time ngrok browser interstitial intercepted the redirect | Open the ngrok URL directly once, click through the interstitial, then retry the connector add. |
| "Couldn't reach the MCP server" during connector add | OAuth discovery failed | See [deployment.md's troubleshooting table](deployment.md#troubleshooting) — the rest of the fixes there apply regardless of ingress profile. |

For everything else — service management, GPU/CUDA, revoking access,
restarting, logs, multi-host setups — see [deployment.md](deployment.md).
