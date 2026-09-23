## 1. Specification

- [ ] 1.1 Create the change with the `remote-owner-identity` capability and the `release-gate` modification, and pass strict validation.

## 2. Resolution

- [ ] 2.1 Add failing resolution coverage: unset, a match, wrong id, wrong issuer, missing, boolean or string id, a plain token with identical claims, owner-shaped header claims, the malformed-value table, and a binding removed between two resolutions.
- [ ] 2.2 Add the binding parser, the verified-token seam, the provenance marker returned by the session proxy, `RequestPrincipal.remote_owner` and `principal_kind`, the branch in `resolve_mcp_principal`, and the content-free startup state line.
- [ ] 2.3 Prove provenance through a real HTTP request against a local session authority: bound id resolves to the owner labelled remote, an unbound id stays a principal, and a forged loopback bearer never becomes the owner.

## 3. Consumers and labels

- [ ] 3.1 Cover the consumers: the default-deny scope, owner-only governance, cross-audience inspection, shared owner results for local and bound remote, no owner results for an unbound principal, issuer-family separation of authorization sessions, unchanged retry and capture-sweep scopes, and the `library_scope()` no-op.
- [ ] 3.2 Record `principal_kind` in the call ledger for all four kinds. The activation log adds the same field in whichever of this change and the episode-capture change merges second.
- [ ] 3.3 Clear the binding in the legacy hosted runtime and prove hosted and loopback principals are never the owner with it set.

## 4. Operator surfaces

- [ ] 4.1 Report the binding in doctor as unset, active, mismatch or malformed without the id, and count rules and grants naming the former remote audience.
- [ ] 4.2 Mark owner-equivalent sessions in `exomem auth sessions`.
- [ ] 4.3 Ask the owner question in the remote setup wizard, with `--remote-owner` and `--no-remote-owner`, and write nothing under `--yes` without a flag.
- [ ] 4.4 Pin that no product command, REST route or transfer route writes the service environment file and that no environment file is loaded from the vault root.
- [ ] 4.5 Document the binding, rotation and revocation, the former-audience note, and the HA rule.

## 5. Allowed-account recheck

- [ ] 5.1 Reject sessions and refresh families whose GitHub user id is not the currently allowed sign-in account.

## 6. Delivery verification

- [ ] 6.1 Run the scoped suites for every touched module, lint, the privacy gate, strict OpenSpec validation and the derived-artifact checks.
- [ ] 6.2 Run the full CI-shaped sharded suite at the completion boundary.
- [ ] 6.3 After release and deploy, enable the binding on the live host and confirm a remote client bootstraps as the owner with an `owner-oauth` ledger row, and reverts when the binding is removed.
