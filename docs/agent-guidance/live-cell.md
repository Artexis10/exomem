<!-- authority:non-specification -->

# Task-specific agent guidance

Read the relevant sections before the work named by the root guidance. OpenSpec remains the specification authority.

Identify the live deployment from current service configuration and Exomem recall before acting. The platform-specific commands and historical incident details below are not evidence of the current host. On WSL use the actual user unit and its configured paths; do not apply Windows service commands there.

## Connector triage ("MCP not working" / slow first call / forced reconnect)

claude.ai connector problems are almost always **connection-side, not the service**.
The public ingress is a **Cloudflare Tunnel** (`exomem.substratesystems.io`, cloudflared
Windows service; migrated FROM Tailscale Funnel 2026-06-21 — the funnel throttled
connector bursts, KB note `kb-mcp-ingress-migrated-to-cloudflare-tunnel-…`). Known
connection-side patterns: (1) a long-lived claude.ai session's **first MCP call
after an exomem service restart** can stall minutes in the gateway's MCP-session
re-establishment while fresh sessions connect instantly — the server log shows
`Created new transport with session ID` when the delayed call finally lands, and
the request then executes in normal time; (2) Cloudflare's edge caps a single
request at ~100 s. **Diagnose from the access log before touching the server**
(claude.ai gateway IPs `160.79.104.0/21` still appear through the tunnel); don't
restart the service reflexively — restarts CAUSE pattern (1) for live sessions.

## Live-cell guardrails (2026-08 incident; standing until `bound-graph-recovery-funnel` lands in code)

- **Never run an out-of-process drain (`exomem index --scope vault`) while the
  service is running.** The CLI takes the graph claim, blocks at 0 CPU on the
  live boundary, and the service mints full-index receipts on every write
  meanwhile — a measured soft-deadlock (~2,100 receipts in 40 minutes).
  Stop-window only: stop the actual service with its platform manager → drain → restart that same service. Windows examples use `Stop-Service` / `Start-Service`; a WSL user unit uses `systemctl --user`.
  Note: `exomem maintain --reconcile` does NOT drain the deferred queue;
  `exomem index` does.
- **Test kill-switch env INSIDE the venv python, never in the shell.** Unowned
  site-packages `.pth` files inject `EXOMEM_*` flags at interpreter startup, so
  shell/user/machine/NSSM scopes all show them unset while every venv process
  has them. The 5-second test:
  `<venv>\Scripts\python.exe -c "import os; print(os.environ.get('EXOMEM_DISABLE_GRAPH_SCHEDULING'))"`.
  A cell whose graph work is disabled while a durable recovery checkpoint
  exists can never converge, and every write mints a full-index receipt.
- **Chained builds + "index upsert incomplete" warnings while `graph_sync` is
  `recovery_required` are the graph accounting funnel** (openspec change
  `bound-graph-recovery-funnel`), not a regression of the 0.63.x fixes. Read
  `.deferred-index.sqlite` `full_upserts` and the graph state before
  diagnosing anything else; the fixed-era signature is full receipts at 0 and
  the graph converging within minutes of each write.
- **Machine-local state lives OUTSIDE the vault** (openspec change
  `relocate-machine-local-state`): the index stores, `.graph-sync*.json`,
  receipts, deferred queue, and due/review projections resolve under
  `EXOMEM_STATE_ROOT`, else `%LOCALAPPDATA%\exomem\state\<vault-key>` on
  Windows. When inspecting a live cell, read them there — a `.sqlite` or
  `.graph-sync*.json` found under `Knowledge Base/` on a migrated cell is a
  dual-state leftover the doctor `state.placement` section will FAIL on
  (`exomem maintain --adopt-state <which>` resolves it; never delete either
  copy by hand).
