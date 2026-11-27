## Why

The local-client-ingress requirement on local tokens is titled "by the operator". The wording names who runs the command; the command never checked it. Any process that runs as the owner's user with the service configuration can issue, list and revoke a local token, and the original design ruled out isolation between processes of the same user. Local tokens give attribution (the client label and the session id) and revocation, nothing more. A token that nobody revokes stays valid until someone does, because local tokens do not expire.

Agents on the owner's machine need a local token. `exomem attach` sends files to the local listener with one, and a held upload is redeemable only by a request that uses the same local token. Today the spec makes an agent ask the operator to run one command, a step that needs no login, device, payment or privilege of theirs. Issuing needs the service's JWT signing key, which can forge any owner session, so the deployment guide says how to give an agent the command without the key.

## What Changes

- The requirement on local tokens is renamed to "issued, listed and revoked on the owner's machine". Its text stays as it was, and its scenarios name a process that runs as the owner's user instead of the operator.
- The deployment guide describes how to let an agent issue tokens through a wrapper that keeps the signing key out of the agent's environment, uses a neutral task id as the client label, logs each session id and revokes the token when the task ends. It states that a wrapper's refusals guard against mistakes and are not a security boundary, and that `exomem auth revoke` does not check who issued a session.
- The `exomem auth` help text and the module docstring no longer say "operator-only".
- At archive, the spec's Purpose line reads "accepts only local tokens issued on the owner's machine" in place of "accepts only operator-issued local tokens".

## Capabilities

### Modified Capabilities

- `local-client-ingress`: local tokens are issued, listed and revoked on the owner's machine by any process that runs as the owner's user.

## Impact

- Docs: `docs/deployment.md`.
- Spec: `openspec/specs/local-client-ingress/spec.md`.
- Code: help text only, in `src/exomem/__main__.py`. No behaviour changes.
- Old debt, unchanged here: a revoked session's tombstone has no TTL, and `list_sessions` does one storage get per key.
- Deferred: an optional expiry for local tokens.
