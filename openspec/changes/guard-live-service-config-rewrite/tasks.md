## 1. Retain the replaced configuration

- [x] 1.1 Add failing coverage proving that publishing a managed service environment over an existing one retains the predecessor with the previous contents and owner-only permissions.
- [x] 1.2 Retain the predecessor before the replacement becomes visible and report its path; keep the publish atomic, and skip retention for a file the same run wrote, so a fresh install's two-phase publish leaves no misleading one-line predecessor.

## 2. Refuse an unintended vault rebinding

- [x] 2.1 Add failing coverage for an existing service whose managed environment records a different vault path than the render produces, including the permitted opt-in case and the unaffected first-install case.
- [x] 2.2 Read the recorded vault path from the managed file, refuse a differing render without the opt-in, and name both paths and the flag; add `--rebind-vault` and document it in the usage text.

## 3. Delivery verification

- [x] 3.1 Run the installer-scoped suite and lint: 28 passed in `tests/test_service_installers.py`, ruff clean, `openspec validate guard-live-service-config-rewrite --strict` passes. The fixture errors are the state-root guard observing the two live services on this machine writing their own directories, which its message names as cross-process interference; the only flagged entries are those services' state roots.
- [x] 3.2 Strengthen the test harness so this path is genuinely covered: the fake interpreter now executes the installer's real publisher instead of reimplementing it, so atomic replacement, permissions and retention are exercised rather than paraphrased.
- [ ] 3.3 Commit the intended scope, push, and open a ready Conventional Commit PR carrying the reproduction and the verification evidence.
