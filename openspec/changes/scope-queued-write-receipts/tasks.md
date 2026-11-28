## 1. Apply receipt authority

- [x] 1.1 Match exact lookup and matching retries to the originating login. Exercise another client, another login, same-login reuse, revoked login during issuer outage, and an unreadable known receipt.
- [x] 1.2 Apply the same scope before computing lists, counts, queue positions, and notices. Verify no other login consumes a notice and check a large failure history for one login and several logins.
- [x] 1.3 Preserve local administrative recovery and the original owner ingress for legacy receipts. Verify a new connector cannot use either route to obtain another receipt.

## 2. Verify the released package

- [ ] 2.1 Check the public receipt and local recovery workflows on the released wheel with disposable state and a prior receipt store.
- [ ] 2.2 Verify that a token refresh keeps receipt access through the real connector.
