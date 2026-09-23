## Context

A personal Exomem host admits exactly one GitHub account to remote sign-in
(`EXOMEM_GITHUB_USERNAME` and `EXOMEM_GITHUB_USER_ID`). The OAuth callback verifies that
account once, and every later request rebuilds typed claims from a durable session record
that this install's session authority validated against its own issuer, audience,
signing-root-derived keys and generation. `resolve_mcp_principal` then hashes
`iss\0sub` into a `principal:<sha256>` audience with issuer family
`mcp-oauth:<sha256(iss)>`. So the owner's own remote connectors are governed as a separate
principal, while the owner's REST key, a non-expiring bearer on the same public origin,
already resolves to `owner`.

`owner` comes only from explicit entry points; `normalize_audience` can never produce it.
Without an access token, a raw `Authorization: Bearer X` header on the loopback HTTP
server becomes claims `{"sub": X, "iss": "bearer"}`, so any owner rule that looked only at
`sub` would be forgeable there.

## Goals / Non-Goals

**Goals**

- One explicitly configured remote identity resolves to the owner audience.
- An unconfigured or misconfigured install behaves exactly as today.
- The owner-equivalent session stays labelled remote in every identity-of-credential scope.
- The change is reversible without migration.

**Non-Goals**

- Enabling owner equivalence for Cloud cells. That belongs to the Cloud change.
- A stricter gate on remote governance authoring.
- A receipt-schema change.

## Decisions

- **D1. Explicit opt-in through a separate value,
  `EXOMEM_OWNER_OAUTH_SUBJECT=github:<id>`.** `EXOMEM_GITHUB_USER_ID` says who may sign in,
  not who the owner is; an install may admit an assistant or delegate account. Changing
  who may sign in must never change who is the owner, so a later change to the allowed id
  makes the old binding stop matching and fail safe.
- **D2. The binding lives only in the host's service environment.** A vault file can be
  written by remote principals and arrives through sync; the service environment sits in
  the same trust root as the signing key and the REST key, and no remote surface writes it.
- **D3. Strict ASCII grammar:** `github:[1-9][0-9]{0,18}` as a full match, lowercase
  provider. This rules out logins, emails, leading zeros, signs and Unicode digits
  (Python's `\d`, `isdecimal()` and `int()` all accept Unicode digits). The provider prefix
  leaves room for `cloud-cell:` later.
- **D4. Malformed or unarmed means unset; startup never fails on it.** Failing closed on
  privilege and staying up on availability are both correct: refusing to start would take
  the connector down over an optional feature. Doctor and one content-free startup log
  line say why. A well-formed binding that names an account other than
  `EXOMEM_GITHUB_USER_ID` is unarmed too (doctor: `mismatch`), so a still-valid session
  of a formerly allowed account can never become the owner.
- **D5. A match requires verified provenance, this install's issuer and a typed id, on
  every request.** The token must be an `ExomemSessionAccessToken`, a marker subclass
  returned only by `ExomemSessionOAuthProxy.load_access_token`; `claims["iss"]` must equal
  `EXOMEM_BASE_URL.strip().rstrip("/")`; `type(claims["github_user_id"]) is int`, equal to
  the bound id, and `claims["sub"] == str(id)`. The header fallback, a plain
  `AccessToken`, any other verifier, and hosted or Cloud verifiers never match. Owner
  status is not cached beyond the request.
- **D6. The owner-equivalent session is fully the owner.** Nothing is stricter for remote
  sessions. The baseline is the REST key. A stricter disclosure gate would force a
  `remote_owner` bit into every audience-keyed cache; if an operation gate is ever needed,
  it goes at the operation boundary, which nothing caches.
- **D7. The remote label is kept by leaving identity-of-credential scopes alone.**
  `mcp_retry_scope()` stays `principal:<hash>`, which feeds idempotency, the capture-sweep
  key and the call ledger's `caller_principal_hash`. The issuer family stays
  `mcp-oauth:<sha256(iss)>`, so authorization sessions, session grants and vocabulary
  authorities never cross between the local owner and the remote one. A closed field,
  `principal_kind` (`owner` | `owner-oauth` | `principal` | `unresolved`), goes on the call
  ledger and the activation log.
- **D8. No migration.** See below.
- **D9. Hosted never consults the personal binding.** The legacy hosted runtime clears the
  variable, and the provenance marker independently excludes hosted and Cloud tokens.
- **D10. Rejected: trust on first use.** On this host the first sign-in can only be the
  allowed id, so it is just auto-enable, which D1 forbids.
- **Session validation rechecks the allowed account; it suspends, never revokes.** A
  session or refresh family whose GitHub user id is not the currently allowed id stops
  validating while that account is not allowed, and validates again if it is re-allowed.
  Suspension rather than tombstoning is deliberate: a mistaken edit of
  `EXOMEM_GITHUB_USER_ID` must not force every connector to re-authorize. Ending a
  taken-over account's sessions for good is still `exomem auth revoke --all`, which the
  takeover runbook always includes.
- **Doctor previews the former-audience count.** While the binding is unset, doctor
  reports (as a pass) how many rules and grants name the remote audience, so the owner
  can read it before enabling; once active it is a warning.

## Controls

| Control | Prevents | Cost when it fires wrongly | Who pays | Verdict |
|---|---|---|---|---|
| Explicit opt-in; unset means today | an upgrade promoting a non-owner allowed account | one line or one wizard answer | owner, once | keep |
| Binding only in the host service environment | a remote or synced writer promoting itself | cannot enable it from a phone | owner, once | keep |
| Verified provenance, own issuer, typed id | a forged loopback bearer; a foreign verifier or copied claims | none for legitimate tokens | nobody | keep |
| Malformed means unset, the service stays up | privilege from a mis-parse | owner stays non-owner until fixed; doctor says why | owner, rarely | keep |
| Hosted clears and ignores the variable | a personal binding leaking into a tenant cell | none | nobody | keep |
| Rejected: block remote governance authoring | a stolen token rewriting policy | owner cannot govern from a phone, every time | owner, recurring | reject: the REST key already allows it; receipted and undoable |
| Rejected: human step-up per remote owner operation | a stolen token | a human in every remote governance act | owner, recurring | reject: no reason automation cannot decide |
| Rejected: refuse startup on a mismatch | a dead binding | connector down | owner | reject; doctor fails instead |

## Risks / Trade-offs

- **T1. Stolen OAuth token.** Today the thief gets `principal:X` with the full product
  surface. With the binding, the thief also gets owner governance, cross-audience
  inspection and the default-deny exemption. Access tokens last one hour, refresh rotates
  with replay detection, and `exo_s1` matches the REST key's lifetime. Response:
  `exomem auth revoke <id>` or `--all`, or unset the binding and restart to cut owner
  status only. Acceptable: equal to the REST key, with better hygiene.
- **T2. GitHub account takeover.** A fresh sign-in as the bound id is owner-equivalent;
  today it is already `principal:X`. Rebind or unset, and always run
  `exomem auth revoke --all`. Validation rechecks the allowed id, so changing
  `EXOMEM_GITHUB_USER_ID` suspends the former account's sessions at once, but a suspended
  session comes back (with owner power, if the binding names that account again) the
  moment the account is re-allowed; only the generation bump ends it.
  GitHub 2FA guards owner power, as it already guards all content.
- **T3. Misconfigured subject.** A typo means the binding never fires and doctor reports a
  mismatch or malformed value. Fail-safe.
- **T4. Multi-tenant confusion.** Legacy hosted never consults the binding, clears the
  variable, and hosted tokens lack the provenance marker. Safe.
- **T5. Replay across installs.** Another install's token fails validation before
  resolution; the resolver also re-checks `iss`. HA replicas are one install and must all
  carry the binding, or the audience flaps (a nuisance, not a leak).
- **T6. Forged loopback identity.** A `Bearer <id>` header yields `iss="bearer"` and no
  marker. Cannot match.
- **T7. Consent phishing** through a dynamically registered client: the consent screen is
  on and sessions list their `client_id` for revocation. Accepted residual.
- **T8. Planting the binding through the vault.** The variable is read only from the
  process environment; the only file loaded is `service.env` or the working directory's
  `.env`. Startup refuses that `.env` when the working directory, or the directory the
  file resolves into through a symlink, is inside the configured vault (or any directory
  that is structurally a vault), and logs one line naming the file and the remedy: move
  the `.env` out of the vault, or put the settings in `service.env`. That stops a
  vault-planted `.env` from setting service secrets. A wrong refusal costs more than one
  unloaded file when that file held required settings (the vault path, OAuth or signing
  keys): startup then stops on the missing setting. The operator who pays is one running
  the service from a checkout or directory inside a vault; the log line tells them what
  to do. The ancestor walk stays, because the refused file can carry service secrets.

## Migration Plan

No migration. State under the former remote audience (episode ledgers, recap groups,
prominence preferences, vocabulary rows, authorization sessions, and rules or grants
naming `principal:X`) stays intact and dormant, and applies again if the binding is
removed. A one-time merge is rejected because recap Sources are immutable and reconciling
two episode hash chains is a design of its own; a read-through is rejected because every
future per-audience store would have to remember it. Doctor names the rules and grants
that reference the former audience, since those are the only dormant items with a
security meaning.

Rollback: unset `EXOMEM_OWNER_OAUTH_SUBJECT` and restart.
