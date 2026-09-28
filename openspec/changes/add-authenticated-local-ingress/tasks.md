## 1. Specification

- [x] 1.1 Create the change with the `local-client-ingress` capability and the `managed-service-upgrades`, `retrieve-inject-hook` and `oauth-session-longevity` modifications, and pass strict validation.

## 2. Manager listener

- [x] 2.1 Add failing coverage for stripping internal headers on both listeners, stamping with the proof only on the local listener, the fail-closed header, host and path rules, and the shared pause and drain.
- [x] 2.2 Strip and stamp in the ingress, add the local listener gate, give every spawned worker the proof key, and serve the local listener from `EXOMEM_LOCAL_PORT` with a pre-bound socket that disables itself on a bad value or a busy port.

## 3. Worker stamp, verifier and principal

- [x] 3.1 Add failing coverage: a proven stamp with a local token is accepted; an OAuth token, the REST key and the upload token are 401s without `resource_metadata` on local ingress; a local token on the public path is the ordinary 401; an unproven stamp is refused; the public path is unchanged.
- [x] 3.2 Add the local issuer and audience, the local session authority, the worker middleware in the OAuth proxy's middleware and the no-auth server list, and the grant branch in `load_access_token`.
- [x] 3.3 Resolve a local grant to `owner-local` with issuer family `mcp-local`, record it in the call ledger, and accept the local token on REST and `/upload` on local ingress.
- [x] 3.4 Log `ingress=local` and the client id on local requests, content-free refusals, and count owner REST-key and upload-token use on Cloudflare-transited requests.
- [x] 3.5 Prove the threat scenarios end to end through a real worker behind the real ingress.

## 4. CLI

- [x] 4.1 Add `exomem auth issue-local` with an exclusive 0600 file and a tombstone on write failure, and name the ingress in `exomem auth sessions`.
- [x] 4.2 Add `exomem attach <file>` that uploads bytes to the local `/upload` and prints the handle.

## 5. Hook

- [x] 5.1 Try the local listener with the local token first in the REST-first rung and fall back to the lifted key for one release, under the same budget.

## 6. Delivery verification

- [x] 6.1 Document the local listener, token lifecycle, attach helper and hook settings.
- [x] 6.2 Run the scoped suites for every touched module, lint, the privacy gate, strict OpenSpec validation and the derived-artifact checks.
- [ ] 6.3 Independent security review of the listener, stamp, verifier and principal.
