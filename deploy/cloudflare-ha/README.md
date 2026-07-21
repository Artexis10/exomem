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

## Mutation-capable routing safety

Use two edge timeouts:

- `ORIGIN_TIMEOUT_MS` (default `2500`) is the short connectivity/fallback window
  for OAuth, discovery, initialization, tool listing, and GET/SSE traffic.
- `MCP_TOOL_TIMEOUT_MS` (default `60000`) is the execution window for
  `tools/call` and other unsafe non-`/mcp` methods, including personal REST and
  lifecycle POSTs plus public transfer PUT uploads. Correctness comes from
  single-origin routing, not from ordering this timeout against the writer-lease
  TTL.

While a writer lease is active, a tool call or other mutation-capable request
goes only to that replica. The edge never replays an ambiguous timeout or 5xx
response to the passive replica: the first origin may already have completed a
mutation. Safe GET/HEAD/OPTIONS traffic and non-tool MCP initialization retain
the short fallback path. Before single-origin routing, the edge admits the runtime
through `/health/ready`: supported runtime contract, stateless transport,
expected replica identity, healthy coordination, and takeover eligibility. The
admission is bound to the lease fencing token in the Durable Object, so steady
state does not add a readiness round trip to every MCP call.

With no holder, both origins are probed concurrently and the mutation-capable
request is forwarded exactly once to the first eligible replica. A live but stale
service that lacks the readiness contract is skipped instead of becoming the
failover writer.

Configure `SUPPORTED_RUNTIME_CONTRACTS` with behavioral contract versions, not
package versions. Compatible releases can differ during a rolling deployment.
Before enabling enforcement, compare the checked-out and installed versions and
probe readiness on each machine:

```powershell
git -C "$HOME\Desktop\projects\exomem" log -1 --oneline
& "$HOME\Desktop\projects\exomem-service-ha\.venv\Scripts\python.exe" -c `
  "import exomem; print(exomem.__version__)"
curl.exe -fsS https://exomem-desktop.example.com/health/ready
curl.exe -fsS https://exomem-laptop.example.com/health/ready
```

Run the combined read-only gate from either checkout:

```powershell
uv run python -m exomem doctor --profile ha --probe `
  --replica-url https://exomem-desktop.example.com `
  --replica-url https://exomem-laptop.example.com
```

For a future incompatible contract bump, use expand-roll-contract: temporarily
accept both contracts (`"1,2"`), roll every replica, verify doctor, then remove
the old contract. Deployment infrastructure owns release pinning and rollback;
Exomem does not update another machine and does not depend on Syncthing.
