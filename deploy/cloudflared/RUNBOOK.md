# Cloudflare Tunnel — exomem ingress setup & cutover runbook

Per-machine playbook for putting exomem behind **Cloudflare Tunnel** — either a fresh
setup or **migrating an existing host off Tailscale Funnel**. The server code is
unchanged (the public URL is env-driven via `KB_MCP_BASE_URL`); this is purely
ingress + `.env` + GitHub OAuth App + the claude.ai connector.

> **No domain?** Cloudflare Tunnel needs a domain you own in Cloudflare. If you don't
> have one, use **ngrok** instead (free static dev domain, no domain needed) — see
> [docs/remote-quickstart.md](../../docs/remote-quickstart.md) or
> [docs/deployment.md](../../docs/deployment.md) Option B. Tailscale Funnel also
> still works as a no-domain fallback, but its shared relay throttles claude.ai's
> connector bursts — see deployment.md's "Why not Tailscale Funnel?" note. This
> runbook is for the Cloudflare path.

Substitute throughout:
- `<HOST>` = `kb.example.com` (desktop) / `kb-laptop.example.com` (laptop)
- `<TUNNEL>` = `exomem-desktop` / `exomem-laptop`

## Prereqs
- `winget install --id Cloudflare.cloudflared`
- The target domain is in your Cloudflare account.
- exomem already installed and running as a service on `127.0.0.1:8765`.

## Steps — order matters

1. **Authorize cloudflared** (browser; writes `cert.pem` to your profile):
   ```
   cloudflared tunnel login
   ```
2. **Create tunnel + DNS + service** (handles the systemprofile config location and the
   bare-ImagePath / exit-1067 fix automatically):
   ```
   pwsh -File scripts/setup-cloudflared.ps1 -Hostname <HOST> -TunnelName <TUNNEL>
   ```
3. **GitHub OAuth App** — edit *this host's* app (the one whose Client ID matches
   `GITHUB_CLIENT_ID` in this machine's `.env`) at <https://github.com/settings/developers>:
   - Homepage URL → `https://<HOST>`
   - Authorization callback URL → `https://<HOST>/auth/callback`
   - **Update application** (the fields aren't saved until you click it).
4. **Point the server at `<HOST>`** and restart so it reloads:
   ```
   (Get-Content .env) -replace '^KB_MCP_BASE_URL=.*','KB_MCP_BASE_URL=https://<HOST>' | Set-Content .env
   pwsh -File scripts/restart.ps1
   ```
   > **CRITICAL:** steps 3 and 4 must *both* point at `<HOST>` before step 6. If the
   > GitHub app and the server's base URL disagree, GitHub rejects the login with
   > "The redirect_uri is not associated with this application."
5. **Cloudflare security** — *only if* claude.ai later gets blocked. On the free plan
   Bot Fight Mode + Security Level are **zone-wide**, so don't blanket-disable them for
   the whole domain. Scope it instead: an IP Access Rule allowing Anthropic's egress
   `160.79.104.0/21`, or a per-hostname Configuration Rule lowering Security Level for
   `<HOST>`. By default no rule is needed.
6. **claude.ai** → Connectors → add/reconnect at `https://<HOST>/mcp` → complete GitHub
   login. Remove the old `*.ts.net` connector once verified.

## Verify
- Local: `curl.exe -i http://127.0.0.1:8765/mcp` → **401**.
- Public base URL — must now show `<HOST>` (proves the `.env` cutover took):
  ```
  curl.exe -s https://<HOST>/.well-known/oauth-protected-resource
  ```
  → `{"resource":"https://<HOST>/mcp","authorization_servers":["https://<HOST>/"], ...}`
- `cloudflared tunnel info <TUNNEL>` → shows active connection(s).
- claude.ai: connector connected, tools listed, a `find` returns results.
- Reboot: both `cloudflared` and `exomem` services auto-start and the connector still works.

## Rollback (config-only — keep Tailscale Funnel running as standby)
- `.env`: `KB_MCP_BASE_URL` back to the `*.ts.net` URL.
- GitHub OAuth App callback back to `https://<ts.net-host>/auth/callback`.
- `pwsh -File scripts/restart.ps1`.
- claude.ai connector back to `https://<ts.net-host>/mcp`.

## Gotchas (learned the hard way — 2026-06-21)
- **`cloudflared service install` → exit 1067.** It registers a *bare* ImagePath (exe
  only, no `tunnel run`/`--config`), so the service starts cloudflared with no command
  and dies. `scripts/setup-cloudflared.ps1` fixes the ImagePath to
  `"<exe>" --config "<config.yml>" tunnel run`. Diagnose: `sc.exe qc cloudflared`
  (bare path) + `sc.exe query cloudflared` (1067).
- **SYSTEM service config location.** The service (LocalSystem) reads
  `C:\Windows\System32\config\systemprofile\.cloudflared\` — not `%USERPROFILE%\.cloudflared\`
  where `tunnel login/create` write `cert.pem` + creds. The script copies them across.
- **Cutover ordering** — the CRITICAL note above; verify the live base URL before retrying.
- **Cloudflare edge ~100s request cap** — run heavy ops (embedding rebuild, large audit)
  via the local CLI, not the connector.
- **Access-log triage still works behind Cloudflare** — `logs/exomem.log` /
  `logs/service.out.log` still surface the Anthropic gateway IP `160.79.106.x` through
  the tunnel.
