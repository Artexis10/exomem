## Why

The mission is a new user reaching a working claude.ai connector in **≤15 minutes**.
Today the remote path is expert-only:

- `docs/deployment.md` recommends **Tailscale Funnel** as the no-domain option — a
  decision since reversed. Funnel is beta, bandwidth-capped, and its shared relay
  throttles claude.ai's connector request bursts, producing "looks disconnected"
  failures that have nothing to do with exomem itself.
- There is **no ngrok path documented**, even though the decided no-domain default
  is ngrok: every ngrok account gets one free static dev domain, which is more
  burst-tolerant than Funnel's relay.
- Verification is scattered curl lore spread across `deployment.md`,
  `remote-checklist.md`, and `deploy/cloudflared/RUNBOOK.md`, with no single
  command a user can run to confirm the live endpoints actually work end to end.
- `doctor --profile remote` checks environment-variable *shape* only. It cannot
  tell a user whether the tunnel is actually up, whether OAuth discovery resolves
  through it, or whether the specific bare `.well-known` path claude.ai's gateway
  probes is reachable — so users discover connector failures only by trying the
  connector.

## What Changes

1. **`exomem doctor --profile remote --probe`** — a NEW opt-in flag (CLI arg wired
   in `__main__.py`'s `_doctor_main`, check logic in `doctor.py`). Default `doctor`
   stays fully offline: the existing read-only guarantee is preserved for every
   profile and is restated explicitly in the spec delta below. With `--probe` and
   `--profile remote`, three read-only HTTP GETs run as additional checks:
   - (a) `http://127.0.0.1:8765/mcp` expecting `401` (server up, auth enforced);
   - (b) `{EXOMEM_BASE_URL}/.well-known/oauth-authorization-server` expecting `200`
     JSON (OAuth discovery resolves through the public endpoint);
   - (c) the **bare** `{EXOMEM_BASE_URL}/.well-known/oauth-protected-resource`
     expecting `200` with `resource == {EXOMEM_BASE_URL}/mcp` — this gate exists
     because claude.ai's gateway probes the bare path before following the
     `resource_metadata` pointer, and a `404` there aborts connector registration
     with `mcp_registration_failed` (the server already ships a workaround route
     for this at `server.py`'s `_oauth_protected_resource_bare`; the probe proves
     that route is live through the actual tunnel, not just importable).

   Each probe is a normal doctor check with `pass`/`fail` + remediation; network
   errors (connection refused, timeout, DNS failure) are failures with actionable
   messages such as "tunnel not running — start cloudflared/ngrok and retry."

2. **New guided doc `docs/remote-quickstart.md`**, REPLACING `docs/remote-checklist.md`
   (deleted; links updated in `README.md` and `docs/deployment.md`). Structure:
   - An ingress-profile decision table first: **Cloudflare Tunnel** (you own a
     domain; scripted via `scripts/setup-cloudflared.ps1`), **ngrok** (no domain;
     free static dev domain), **SSH reverse tunnel to a VPS** (fallback).
     Tailscale Funnel is demoted to a "why not" footnote naming the relay
     throttling rationale.
   - Then shared numbered steps, each ending with a validation command: GitHub
     OAuth app → `.env` (`EXOMEM_BASE_URL`, `GITHUB_CLIENT_ID`/`GITHUB_CLIENT_SECRET`,
     `EXOMEM_GITHUB_USERNAME`, `EXOMEM_JWT_SIGNING_KEY`, `EXOMEM_VAULT_PATH`) →
     start server → tunnel up → `exomem doctor --profile remote --probe` (with the
     curl equivalents printed alongside for anyone who wants to see the raw
     requests) → add the claude.ai connector at `https://<host>/mcp`.
   - An ngrok free-tier limits box: 120 requests/min rate limit, ~20k
     requests/month, one-time browser interstitial — fine for one person; if
     connector setup stalls mid-burst, check the ngrok console for `429`s;
     Cloudflare or paid ngrok removes the cap.

3. **New `scripts/setup-ngrok.ps1`**, mirroring the `scripts/setup-cloudflared.ps1`
   precedent: verifies ngrok is on `PATH` (winget hint), verifies an authtoken is
   configured (`ngrok config check`), takes `-Domain`/`-Port`, writes the ngrok
   config for the static dev domain endpoint, installs auto-start via
   `ngrok service install`, and prints the same verification triple `doctor
   --probe` checks. The macOS/Linux equivalent (`brew install ngrok` + one
   command) is documented inline in the quickstart — no script needed for two
   commands.

4. **`docs/deployment.md` ingress section rework**: Option A becomes Cloudflare
   Tunnel, Option B becomes ngrok; Tailscale is demoted with the same rationale,
   not deleted (existing users on Funnel keep working). Adds a multi-host note
   (one free static domain per ngrok account) and two new troubleshooting rows
   (ngrok `429` burst throttle; the ngrok browser interstitial breaking the OAuth
   redirect once). All Tailscale-as-recommended references are swept repo-wide for
   consistency: `deployment.md`, `deploy/cloudflared/RUNBOOK.md`'s "no domain?"
   pointer, and `README.md` links.

5. **Tests**: `tests/test_doctor_probe.py` — probe checks run against an
   injected/mocked HTTP transport, never real network: the `401`-ok shape, the two
   `200`-ok shapes, the bare-well-known-`404` case failing with the
   `mcp_registration_failed` remediation, and a connection-refused case failing
   with an actionable message. Plus a guard test proving `doctor` **without**
   `--probe` performs zero network calls, for any profile.

**Pure-substrate note**: the probes are deterministic endpoint measurements (HTTP
status codes and JSON field equality) — no reasoning, no model, no note content
touched. They are default-off (opt-in `--probe`) and soft-fail into an actionable
`DoctorCheck` exactly like every other doctor check; a failed probe never raises
past the report. The docs and scripts changes are docs/scripts only — no new
server capability, no schema change.

## Capabilities

### Modified Capabilities

- `install-readiness`: `doctor --profile remote` gains an opt-in `--probe` flag
  that runs three read-only HTTP checks against the live remote endpoints (offline
  default preserved); the project's ingress and remote-setup documentation is
  restructured around a Cloudflare/ngrok/SSH decision table with Tailscale Funnel
  demoted to a footnote.

## Impact

- Code: `src/exomem/doctor.py` (new `--probe` checks for the `remote` profile),
  `src/exomem/__main__.py` (`_doctor_main` gains `--probe`).
- Docs: new `docs/remote-quickstart.md` (replaces deleted `docs/remote-checklist.md`),
  `docs/deployment.md` (ingress section rework, troubleshooting rows,
  Tailscale-as-footnote sweep), `deploy/cloudflared/RUNBOOK.md` ("no domain?"
  pointer updated to ngrok), `README.md` (links updated to
  `remote-quickstart.md`).
- Scripts: new `scripts/setup-ngrok.ps1`.
- Tests: new `tests/test_doctor_probe.py`.
- Dependencies: none new — probes use `httpx`, already a direct dependency
  (`pyproject.toml`), via a mockable client seam so the suite never touches the
  network.
