# One connector, two Exomem replicas

This optional Cloudflare Worker keeps a single public MCP/OAuth URL in front of
desktop and laptop replicas. A SQLite Durable Object stores only:

- writer identity, expiry, and fencing counter;
- opaque OAuth records encrypted by Exomem before upload.

Vault files and search results are proxied, never stored at the edge. Syncthing,
Obsidian Sync, a NAS, or any other replication mechanism remains a separate
operator choice.

Copy `wrangler.toml.example` to `wrangler.toml`, set both private origin hostnames
and the stable route, then deploy:

```powershell
npx wrangler login
npx wrangler secret put STATE_TOKEN
npx wrangler deploy
```

Configure both replicas with the same stable `EXOMEM_BASE_URL`, GitHub OAuth app,
`EXOMEM_JWT_SIGNING_KEY`, state URL/token, vault ID, and lease token. Give each a
different replica ID; set `EXOMEM_WRITER_LEASE_PREFERRED=1` only on the preferred
desktop. See `docs/deployment.md` for the complete environment block and takeover
test.

## Tool-call routing safety

Use two edge timeouts:

- `ORIGIN_TIMEOUT_MS` (default `2500`) is the short connectivity/fallback window
  for OAuth, discovery, initialization, tool listing, and GET/SSE traffic.
- `MCP_TOOL_TIMEOUT_MS` (default `15000`) is the execution window for
  `tools/call`; correctness comes from single-origin routing, not from ordering
  this timeout against the writer-lease TTL.

While a writer lease is active, a tool call goes only to that replica. The edge
never replays an ambiguous timeout or 5xx response to the passive replica: the
first origin may already have completed a mutation. With no active holder, the
edge probes both origins, chooses one healthy replica, and forwards the tool call
once. That preserves laptop takeover after lease expiry without turning a slow
desktop write into two writes.

Both services must run the same restart-safe Exomem release before the stable
connector route is considered healthy. Compare the checked-out and installed
versions on each machine, then restart any stale service:

```powershell
git -C "$HOME\Desktop\projects\exomem" log -1 --oneline
& "$HOME\Desktop\projects\exomem-service-ha\.venv\Scripts\python.exe" -c `
  "import exomem; print(exomem.__version__)"
```
