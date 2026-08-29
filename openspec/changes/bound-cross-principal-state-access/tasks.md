## 1. Read-only posture inspection

- [x] 1.1 `mutation_lock.WindowsDirectoryPosture` + `inspect_windows_private_directory`: read the descriptor and report a verdict without raising, without repairing, and without modifying the root
- [x] 1.2 `state_paths.StateRootPosture`, `StateRootAccessDenied(OSError)`, `inspect_state_root`, `assert_state_root_accessible`
- [x] 1.3 POSIX branch no-ops and never refuses

## 2. Doctor separates placement from access and degrades gracefully

- [x] 2.1 New `state.dacl` check, distinct from `state.placement`, wired into the ordered report
- [x] 2.2 FAILs with observed descriptor, required trustees, and exact remediation — never a traceback
- [x] 2.3 Reports an un-evaluated descriptor as its own state, not as a pass
- [x] 2.4 `_check_lexical` reports an unreadable sidecar instead of raising
- [x] 2.5 `_check_deferred_index_backlog` survives an unreadable sidecar
- [x] 2.6 `_check_rebuild_temp_orphans` reports an unlistable scan root instead of raising, and does not claim "no orphans found" from a scan that did not run

## 3. Maintenance refuses up front

- [x] 3.1 `commands._assert_state_root_usable`, called first in `op_maintain_memory`, before the mutation hold
- [x] 3.2 `state_migration` preflight before the lock in `migrate_vault_state_offline`
- [x] 3.3 Stable `STATE_ROOT_CROSS_PRINCIPAL` error code carrying descriptor, required trustees, and remediation
- [x] 3.4 Happy path proven unaffected: a same-principal root is never refused

## 4. Runtime-principal resolution (requirement 1)

- [x] 4.1 Resolve the runtime principal from the platform service registry (`ObjectName` beside the `Parameters\Application` value `scripts/_service-common.ps1` already reads); fall back to the current token when no service install exists
- [x] 4.2 Apply the runtime principal's private DACL when a user-token flow creates or recreates the state root — as the LAST act of the migration, never at creation: the creating token has to write the state in first, so a root sealed up front locks its own migrator out
- [x] 4.3 Never widen the trustee set, never make the validator advisory, never re-ACL a root this process does not own
- [x] 4.4 Red-first test: a user-token creation leaves a root that satisfies the validator evaluated **for the service account**, asserted with the validator itself rather than a restated SDDL literal
- [x] 4.5 Test: no service install leaves current-token behaviour byte-identical
- [x] 4.6 Test: an unreadable or absent service registry entry degrades to the current token with a named reason, never a crash and never a guessed principal
- [x] 4.7 `state.dacl` judges the descriptor against the RUNTIME principal, not the calling token. Forced by measurement, not preference: a correct LocalSystem root reads `unsafe` and unopenable to every operator token, so judging by the caller FAILs every healthy Windows service install — including inside `upgrade.ps1`'s own doctor gate, which runs as the operator
- [x] 4.8 `EXOMEM_RUNTIME_PRINCIPAL` pins the principal (`current-token`, or a literal SID) for stop-window maintenance, and keeps the suite's verdict a property of the code rather than of whether the developer's machine happens to have a service registered
- [x] 4.9 The seal fires only for the vault the machine's service is BOUND to, read from the service's managed dotenv the way `upgrade.ps1` reads it. A machine merely having a service registered says nothing about an unrelated vault: without this, `exomem init` of a brand-new vault handed its state root to LocalSystem and locked the operator out of the directory it had just made. An unreadable binding refuses rather than sealing
- [x] 4.10 The seal protects the STATE, not merely the directory entry. Protecting a DACL converts children's inherited ACEs to explicit ones, so a root sealed with `SetFileSecurityW` reported private over files the old principal still read and wrote; the seal applies through `SetNamedSecurityInfoW` so inheritance is recomputed across the tree
- [x] 4.11 `state.dacl` samples children rather than trusting the root descriptor alone, and FAILs when the state inside a private-looking root is not private
- [x] 4.12 An unresolved runtime principal withholds the token-relative `icacls` entirely, rather than prescribing the ACL ping-pong the incident is about

## 5. Verification

- [x] 5.1 Mutation proof: deleting each guard fails a named test (round 1: 9/9; round 2: 17/18 — the seal's post-write validation SURVIVED and was mis-reported as proven; round 3: 21/21, that guard included)
- [x] 5.2 Targeted suite green with no new failures against an untouched-main baseline
- [x] 5.3 `uvx ruff check` clean on changed files
- [x] 5.4 Re-run 5.1–5.3 after section 4 lands
- [x] 5.5 `openspec validate --all --strict`
- [ ] 5.6 Independent adversarial review by a lane that did not author the change, running the reproduction rather than trusting the report
