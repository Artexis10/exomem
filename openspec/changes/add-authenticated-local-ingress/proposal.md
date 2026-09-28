## Why

On a managed host, loopback does not mean local. Both Cloudflare tunnels deliver public
traffic to the public listener on `127.0.0.1:8765`, and the worker behind the manager
only ever sees the manager's private Unix socket. So nothing on the worker can tell a
same-machine client from a remote one by address, and same-machine clients today reach
the vault through the public door: the claude.ai account connector, a static REST key the
retrieve hook lifts out of `service.env`, and a static upload token. None of those carry
per-client attribution, and none can be revoked for one client without breaking the rest.

## What Changes

- The managed service can open a second listener on literal loopback, off unless
  `EXOMEM_LOCAL_PORT` is set. It shares the public listener's ingress, so upgrade pause,
  drain and handoff cover it.
- The manager strips every inbound `x-exomem-internal-*` header on both listeners and
  stamps local ingress, with a per-manager proof, only on the local listener. The local
  listener refuses Cloudflare-transited requests, any `Origin`, a non-literal-loopback
  `Host`, and every path outside `/mcp`, `/api/*`, `/upload` and `/health*`.
- A worker checks a local client credential only when a proven local stamp is present.
  Without one the public path is unchanged.
- `exomem auth issue-local --client <name> --output <file>` mints a non-expiring,
  revocable session under a separate local issuer and audience and writes it to a 0600
  file. `exomem auth sessions`, `revoke <id>` and `revoke --all` cover these tokens.
- The audience is the ingress: the local listener accepts only local tokens and the public
  path rejects them.
- A request authenticated this way resolves to the owner audience with issuer family
  `mcp-local` and the audit label `owner-local`, never the remote-owner label.
- REST and `/upload` accept the local token on local ingress. `exomem attach <file>` sends
  a file's bytes to the local `/upload` and prints the returned handle.
- The retrieve hook's REST-first rung may use the local listener with a local token file,
  falling back to today's lifted-key rung for one release.
- Logs stay content-free and name the ingress and client. Owner REST-key and static
  upload-token use on Cloudflare-transited requests is counted, not refused.

Default-off: nothing changes on a host that does not set `EXOMEM_LOCAL_PORT`, and the
manager half only takes effect after the manager itself restarts.

## Capabilities

### New Capabilities

- `local-client-ingress`: the loopback listener, its stamp and fail-closed rules, the
  local token lifecycle, the `owner-local` principal, and local REST and upload.

### Modified Capabilities

- `managed-service-upgrades`: both listeners pause and drain together.
- `retrieve-inject-hook`: the REST-first rung may use the local listener first.
- `oauth-session-longevity`: the public path rejects local tokens.

## Impact

- `service_ingress.py`, `service_manager.py`, a new `local_ingress.py`,
  `session_oauth.py`, `server_auth.py`, `server.py`, `server_rest.py`,
  `server_transfer.py`, `access_log.py`, `governance/principal.py`, `call_ledger.py`,
  `__main__.py`, the retrieve hook, and the deployment docs.
- No `WORKER_PROTOCOL` change: the stamp and the proof variable are additive, an older
  worker ignores them, and a newer worker under an older manager is never armed.
- No data migration. Local sessions live in the existing session store.
- Out of scope: refusing the owner REST key and the static upload token on
  Cloudflare-transited requests (later, on log evidence), client configuration, and how an
  upload handle feeds `capture_source` or `preserve_artifacts`.
