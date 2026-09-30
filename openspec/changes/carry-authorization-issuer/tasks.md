## 1. Reproduce and repair

- [x] 1.1 Add failing coverage proving the overridden callback's success and error redirects omit the advertised issuer, plus a guard that the served metadata still advertises it.
- [x] 1.2 Build both client redirects through the framework's redirect helper so each carries the issuer exactly once.

## 2. Delivery verification

- [x] 2.1 Run the auth-scoped suites and lint: 80 passed across session OAuth, auth sessions, HTTP boundary, authorization carriers and auth migration; ruff clean. The three fixture errors are the state-root guard observing the two live services writing their own directories, which its message names as cross-process interference.
- [x] 2.2 Run strict OpenSpec validation, push, and open a ready Conventional Commit PR carrying the reproduction and the verification evidence.
- [ ] 2.3 Release the fix and confirm a real client completes authorization against the upgraded service.
