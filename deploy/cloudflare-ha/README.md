# One connector, one or two Exomem replicas

This Cloudflare Worker keeps a single public MCP/OAuth URL in front of desktop
and laptop replicas. A SQLite Durable Object stores only:

- writer identity, expiry, and fencing counter;
- opaque OAuth records encrypted by Exomem before upload.

Vault files and search results are proxied, never stored at the edge. Syncthing,
Obsidian Sync, a NAS, or any other replication mechanism remains a separate
operator choice.

## It is opt-in, and a deployed route outlives the replicas behind it

Nothing installs or requires this worker. A single-node Exomem is served
perfectly well by the tunnel alone, and that is the simpler deployment: the
origin answers `/mcp` directly and there is no edge policy to keep in step with
the machines behind it.

Deploying it is therefore a decision to keep a second thing in sync with your
topology, because **the route survives the replicas**. Decommissioning a replica
does not remove the worker from `/mcp`; it leaves an HA policy in front of a
deployment that is no longer HA. That is not hypothetical -- it is exactly how a
routine laptop shutdown produced a total MCP outage while the surviving origin
kept answering `/health/ready` with `200` and `reasons: []` (#581). If you take
this worker out of service, take it out of the **route**, not just out of use.

The single-origin topology below is now representable rather than merely
tolerated, so the failure mode is a bad deployment rather than a bad
architecture. It is still one more moving part than not deploying it at all.

## Deploying

Copy `wrangler.toml.example` to `wrangler.toml`, set the private origin
hostname(s) and the stable route, then deploy:

```powershell
npx wrangler login
npx wrangler secret put STATE_TOKEN
.\deploy.ps1
```

```bash
npx wrangler login
npx wrangler secret put STATE_TOKEN
./deploy.sh
```

`deploy.ps1` / `deploy.sh` wrap `wrangler deploy` with
`--var WORKER_GIT_SHA:<short git SHA>`, so the authenticated `GET /__version`
endpoint (gated by the same bearer as the coordinator endpoints) reports which
commit is live instead of `"git_sha": "unlabeled"`. A bare `wrangler deploy`
still works; it just leaves the deploy unlabeled.

### One origin (standalone)

Configure `DESKTOP_ORIGIN` and `DESKTOP_REPLICA_ID` and leave the `LAPTOP_*`
pair unset. A standalone Exomem reports `replica_id: null` and
`coordination: {"enabled": false, "role": "standalone"}` -- correctly, since
there is no second writer to hold an identity against or to coordinate with --
and the edge accepts that as eligible when it is the only origin configured.

The relaxation requires **both** sides to say so: exactly one origin configured
here *and* the origin self-reporting standalone. Dropping one origin from a live
pair by mistake therefore still refuses, because the survivor keeps reporting
its replica id and its coordination as enabled. `REQUIRE_COORDINATION` stays at
its default; it governs replica pairs and has nothing to require of a single
node.

### Two origins (replica pair)

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

With no holder, every configured origin is probed concurrently and the
mutation-capable request is forwarded exactly once to the first eligible one. A
live but stale service that lacks the readiness contract is skipped instead of
becoming the failover writer.

A refusal reports the gate that refused it, per origin:

```json
{
  "error": "the configured Exomem origin is ineligible",
  "refusals": [
    {
      "origin": "https://exomem-desktop.example.com",
      "replica_id": "desktop",
      "reason": "unsupported_runtime_contract"
    }
  ]
}
```

`reason` is the readiness gate (`replica_identity_mismatch`,
`coordination_required`, `unsupported_transport`, ...), or
`health_status_<code>` / `health_probe_unreachable` when the probe itself did
not produce a verdict. The worker always computed this and used to discard it,
leaving a 503 that named replicas as the only symptom -- which, on a deployment
that no longer had two, pointed triage at an architecture that was not there.

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
