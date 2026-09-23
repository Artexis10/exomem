## Why

The owner reaches his own vault through remote connectors that sign in with his GitHub
account, yet every remote OAuth sign-in resolves to a separate `principal:` audience. His
remote sessions are governed as a stranger: default-deny scopes hide his own items,
owner-only governance refuses him, and per-audience state (episode ledgers, prominence
preferences) splits between his local and remote doors. The owner's ruling is that a
remote sign-in by his own account "needs to be treated as me".

## What Changes

- A new host setting, `EXOMEM_OWNER_OAUTH_SUBJECT=github:<numeric id>`, read only from the
  service environment, names the one remote identity that is owner-equivalent.
- A request whose access token this install's durable session authority verified, whose
  issuer is this install's base URL, and whose typed GitHub user id equals the bound id
  resolves to the owner audience, labelled remote: it keeps its remote issuer family,
  retry scope and call-ledger caller hash, and the call ledger records
  `principal_kind: owner-oauth`.
- Unset or malformed means today's behaviour. A malformed value never stops the service.
- Doctor reports the binding's state and the rules or grants naming the former remote
  audience; `exomem auth sessions` marks owner-equivalent sessions; the remote setup
  wizard asks once and writes the value.
- The legacy hosted runtime clears the variable.
- Session validation rejects sessions whose GitHub user id is no longer the allowed
  sign-in account, so an account rotation is a one-step configuration change.

## Capabilities

### New Capabilities

- `remote-owner-identity`: the explicit host binding of one remote OAuth identity as the
  owner, its provenance rule, its labels, and its visibility.

### Modified Capabilities

- `release-gate`: canonical audience resolution names the bound remote identity as an
  explicit owner entry point that keeps its remote issuer family.

## Impact

- `governance/principal.py`, `session_oauth.py`, `server_auth.py`, `auth_sessions.py`,
  `call_ledger.py`, `server.py`, `hosted_runtime.py`, `doctor.py`, `__main__.py`,
  `remote_setup_wizard.py`, and the remote deployment docs.
- No data migration. State recorded under the former remote audience stays intact and
  applies again if the binding is removed.
