## 1. Red Secure-Handle Tests

- [x] 1.1 Add secure-filesystem unit tests proving retained ancestors use metadata-only access and only the exact flushed directory receives `GENERIC_WRITE` and no-delete sharing.
- [x] 1.2 Add unit tests proving Windows `O_CREAT | O_EXCL` maps to exclusive `CREATE_NEW` and existing open/create modes retain their dispositions.
- [x] 1.3 Add receipt tests proving the Windows route never asks `os.open` to open a directory and normalizes open, identity, reparse, raw-handle flush, and I/O failures to content-free `ReceiptError` results.
- [x] 1.4 Add red tests for junctions at `_Governance`, `events`, and the instance directory plus a swap during first-instance creation, proving no outside path is created or touched.
- [x] 1.5 Run the focused new tests and record their expected failure against the old implementation.

## 2. Secure Receipt Filesystem Wiring

- [x] 2.1 Correct exclusive Windows secure-child creation and keep containment, non-reparse, regular-file, identity, descriptor-ownership, and cleanup checks intact.
- [x] 2.2 Preserve the lexical events path and securely create/retain its ancestors and first instance without bare recursive `mkdir` or resolving away reparse components.
- [x] 2.3 Route monthly receipt open/create through retained secure directory and child handles on Windows while leaving the POSIX descriptor-relative contract intact.
- [x] 2.4 Run mutation-lock and receipt tests and keep the red tests green.

## 3. Critical Directory Durability

- [x] 3.1 Add red ordering tests proving every required directory flush precedes durable-head update and any open/fsync failure leaves both sidecar heads unchanged.
- [x] 3.2 Add Windows red tests proving the raw handle is passed to `FlushFileBuffers`, directory `os.fsync` is never called, and `CloseHandle` runs exactly once on success and failure.
- [x] 3.3 Implement Windows directory durability with metadata-only retained ancestors, a write-capable raw handle for only the final no-follow directory, direct-child validation, `FlushFileBuffers`, and exact single-close ownership; keep POSIX behavior unchanged.
- [x] 3.4 Prove exact-ID retry and verified reconcile promote one file-ahead critical suffix only after directory durability succeeds and never append a duplicate.

## 4. Native Windows Regression Coverage

- [ ] 4.1 Add a Windows-only test for ordinary append, critical intent/terminal append, chain verification, and idempotent exact-ID retry in a fresh vault.
- [x] 4.2 Add Windows-only reparse and namespace-swap probes proving no outside read, write, or creation occurs.
- [ ] 4.3 Add a Windows-only injected directory-flush failure proving critical sidecar state does not advance.
- [ ] 4.4 Run the native Windows module with the service-compatible Python runtime and record the result for the CI/release handoff.

## 5. Shared Lifecycle Durability

- [x] 5.1 Add red native Windows tests for governed delete/recovery tombstone durability and ungoverned atomic rename without CRT directory opens.
- [x] 5.2 Add red lifecycle tests for raw-handle flush failure, exact close ownership, reparse/direct-child identity refusal, and content-free error output.
- [x] 5.3 Factor the reviewed Windows final-directory flush primitive into the shared secure filesystem substrate and route receipt and lifecycle wrappers through it.
- [x] 5.4 Run focused lifecycle and receipt Windows tests, then the full governance receipt suite, and classify any next native blocker before claiming completion.

## 6. Receipt-First Runtime Bootstrap

- [ ] 6.1 Add red native Windows tests proving first receipt use secures an absent writer-state root before any lock artifact and subsequent idempotency initialization succeeds.
- [ ] 6.2 Add a red native Windows test proving a pre-existing unsafe root is unchanged, gains no lock child, and returns the exact-path remediation through the receipt boundary.
- [ ] 6.3 Harden the shared first-creation initializer for atomic-create races: only the winning creator applies a DACL, while losers use bounded validation-only stabilization and never repair an observed entry.
- [ ] 6.4 Route both receipt-first and mutation-coordinator-first Windows lock initialization through that shared initializer while preserving the POSIX lock paths.
- [ ] 6.5 Run real same-principal multiprocess first-use races from an absent root on Windows: receipt-versus-receipt and receipt-versus-coordinator; require every process to succeed, prove no lock artifact precedes validation, and prove the final root DACL is valid without fixture-created state.

## 7. Verification And Delivery

- [ ] 7.1 Run strict OpenSpec validation, focused receipt/mutation-lock/governance-overhead tests, focused Ruff, public-artifact validation, and `git diff --check`.
- [ ] 7.2 Run the full lean suite and separate any pre-existing native Windows failures from regressions introduced by this change.
- [ ] 7.3 Record the exact Windows test command that the separate CI repair must add before 0.50 release.
