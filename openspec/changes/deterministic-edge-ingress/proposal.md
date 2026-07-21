# Deterministic edge ingress

## Why

The 2026-07-20/21 incident had one meta-cause: **the public hostname is served by
two competing routing layers**. The HA edge worker routes mutation-capable
requests to the current lease holder, but the same hostname can also be served
tunnel-direct by a machine's cloudflared connector (a legacy binding from the
pre-HA, single-host ingress). Which layer answers a given request is an
accident of DNS bindings, worker route path coverage, and connector restarts.

When traffic goes tunnel-direct it lands on an arbitrary machine:

- If that machine is a follower, every write is refused with
  `WRITER_LEASE_REQUIRED` while reads keep working — observed for 1h+ on
  2026-07-21 (receipt `7bfa92a4049bdcd1` absent from the lease holder's logs;
  the coordinator lease was held stably by `laptop` at fencing token 98 the
  whole time, so the refusals can only have been served by an origin the edge
  worker would never have chosen).
- If that machine's service is restarting, the caller sees cloudflared's
  `502 origin_bad_gateway` — with no worker timeout/replay semantics at all.

A second gap made this worse: **the worker is hand-deployed and unverifiable**.
The service gained install provenance and version-gated deploys (#279), but the
edge has neither a version surface nor a vars surface. The #285 deploy
checklist ("redeploy the worker AND set the live `MCP_TOOL_TIMEOUT_MS` var to
60000") could silently not happen, leaving a live 15s mutation budget that the
repo said was 60s — and nothing that could detect the drift.

Operational cleanup (single-hostname-per-tunnel, worker-only apex route) fixes
today's routing. This change makes the invariant **self-enforcing and
observable**, so a regression is a loud, named error instead of a
lease-shaped mystery.

## What Changes

- **Edge-transit stamp, enforced at the origin.** The worker stamps every
  request it proxies (read fan-out and mutation path alike) with an HMAC
  header derived from the shared coordinator token. Origins with the writer
  lease enabled refuse Cloudflare-transited MCP/REST POSTs that lack a valid
  stamp with a new terminal error `INGRESS_BYPASSED` ("public traffic must
  enter via the HA edge"). Local traffic (CLI, localhost REST, health probes)
  is unaffected; a kill switch restores the old accept-anything behavior.
- **Edge provenance surface.** The worker answers an authenticated
  `GET /__version` with its deploy identity (git SHA supplied at deploy time)
  and its effective non-secret routing vars (`MCP_TOOL_TIMEOUT_MS`,
  `REQUIRE_COORDINATION`, origins, replica ids, runtime-contract set). A
  deploy helper script passes the SHA so a bare `wrangler deploy` remains
  possible but visibly unlabeled.
- **Ingress conformance in doctor.** `exomem doctor` gains checks that (a) the
  public base URL is served by the worker (`/__version` answers and `/v1`
  returns the worker's 401 shape), (b) the worker's effective vars match the
  repo expectation (mutation budget >= 60s, replica-id/origin mapping matches
  this origin's configuration), (c) the public `/health/ready` replica agrees
  with the coordinator's lease holder, and (d) the configured lease TTL is not
  below the supported floor (warn under 30s).

## Non-Goals

- No follower-side proxying of mutations to the writer. With bypass traffic
  refused loudly and the edge verified, a forwarding path would be dead code
  that doubles the mutation surface.
- No change to lease semantics, preemption, or the idempotency store.
- No automation of the Cloudflare-side cleanup (DNS bindings, tunnel ingress
  files); that is operator work, verified after the fact by doctor.
