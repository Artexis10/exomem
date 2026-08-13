## Context

Receipt evidence is authoritative JSONL with a SQLite durable-head sidecar. Critical events advance that head only after the JSONL prefix and directory chain are durable. Lifecycle deletion/recovery separately requires tombstone durability before moves and directory durability after marker unlink or atomic rename. POSIX uses directory descriptors; both native Windows paths currently fall through to `os.open(directory)`, which the CRT rejects.

The repository already has two relevant primitives: retained non-reparse Windows directory/file handles in `mutation_lock.py`, and a native directory handle substrate in `vault.py`. Native verification on the development host established the access contract: `FlushFileBuffers` succeeds on a raw Windows directory handle opened with `GENERIC_WRITE`; metadata-only or read-only access receives access denied, and passing the raw handle to `os.fsync` is invalid. Microsoft documents that directory handles require `FILE_FLAG_BACKUP_SEMANTICS`, and that `FlushFileBuffers` requires `GENERIC_WRITE`.

## Goals / Non-Goals

**Goals:**

- Restore ordinary and critical receipt append/verify plus lifecycle delete/recovery/rename durability on native Windows.
- Retain direct-child, no-follow, anti-reparse, identity, and namespace-race guarantees.
- Keep the critical ordering JSONL flush → directory-chain flush → durable-head commit.
- Leave a file-ahead critical suffix recoverable without duplication after any refused durability step.
- Add deterministic cross-platform tests and real native Windows coverage.

**Non-Goals:**

- Changing receipt JSON, hashing, sequencing, month rotation, or SQLite schema.
- Moving evidence authority into SQLite.
- Weakening a durability failure into a warning or best-effort success.
- Adding dependencies, changing the disclosure ladder, or repairing pre-existing DACLs.

## Decisions

### Reuse retained secure handles

Receipt code will use the existing secure-directory and secure-child helpers rather than adding a second Windows filesystem adapter. On Windows those helpers retain every ancestor with metadata-only access without following reparse points, validate that the child handle remains inside the retained directory, and keep handles alive through I/O. POSIX continues to use descriptor-relative no-follow opens.

Exclusive create will map `O_CREAT | O_EXCL` to `CREATE_NEW`; the current `OPEN_ALWAYS` mapping does not enforce exclusivity on Windows. Receipt instance creation will traverse the lexical path through the retained helper instead of resolving away a same-vault junction and then calling bare recursive `mkdir`.

Alternative rejected: keep pathname opens with pre/post `lstat`. It can detect many swaps but cannot retain every Windows ancestor or guarantee exclusive creation.

### Share directory flush through write-capable native handles

One helper in the secure mutation-lock substrate will retain target ancestors with metadata-only access, open only the final exact non-reparse directory through `CreateFileW` using `GENERIC_WRITE`, `FILE_FLAG_BACKUP_SEMANTICS`, and no delete sharing, verify direct-child identity, call `FlushFileBuffers`, and close exactly once. Receipt and lifecycle wrappers translate failure into their own content-free errors. POSIX remains unchanged.

Write access must not propagate to the drive root or ordinary ancestors: valid user vaults cannot open `C:\` or `C:\Users` with `GENERIC_WRITE`, and those ancestors do not need it for flushing the leaf.

Alternative rejected: skip directory fsync on Windows. That would advance the receipt head or lifecycle checkpoint without satisfying the contract represented.

### Preserve failure and recovery ordering

Every open, identity, or flush error remains a content-free `ReceiptError`. A critical JSONL record may be ahead of the durable sidecar after a refusal; an exact-ID retry or verified reconcile must re-establish the file and directory barriers before promotion. Neither path allocates or appends a duplicate event.

Alternative rejected: catch `PermissionError` and commit the sidecar anyway. That converts an unsupported primitive into false durability.

### Bootstrap the writer-state DACL before receipt locking

Receipt append and `VaultMutationCoordinator` lock access can each be the first process to touch the configured writer-state root. On native Windows every creator will invoke one shared private-root bootstrap before constructing or creating a mutation-lock path. If the root is absent, the winning creator establishes the existing protected principal-private DACL before any lock artifact exists. Concurrent same-principal losers tolerate an atomic-create race, reopen the winner's directory, and use a short bounded validation-only stabilization retry while the winner applies that DACL. A process applies permissions only to the exact directory entry it created; it never repairs an entry merely observed after `FileExistsError`. If the root was already present with an unsafe or different-principal DACL, both lock paths preserve the no-implicit-repair policy and return the existing actionable exact-path refusal without creating a lock child. LocalSystem service and normal-user direct CLI processes therefore continue to require separate writer-state roots. POSIX lock creation remains unchanged.

Alternative rejected: make receipt-first bootstrap sequential-only. Native first use fans out across processes, and a loser can otherwise fail on `FileExistsError` or inspect the winner's directory before its DACL is installed.

### Keep CI workflow ownership separate

This change adds a focused native Windows test module and runs it on the development host. The repository's CI workflow is being repaired in a separate release lane; that lane will add the test command to its Windows gate before 0.50 is released, avoiding concurrent edits to `.github/workflows/ci.yml`.

## Risks / Trade-offs

- [Retained directory handles can briefly block external renames] → Keep the interval inside the existing receipt lock and close every handle in `finally` paths.
- [Write-capable final-directory opens can be denied on non-writable locations] → Fail closed with the existing durable-directory error and do not advance either head; never request write access on ancestors.
- [Private helper reuse can create hidden coupling] → Keep the helper contract narrow, cover its defaults in mutation-lock tests, and avoid changing public APIs.
- [A legacy reparse-based receipt path may now be rejected] → Treat this as intentional security hardening; repair the exact unsafe path and retry/reconcile.

## Migration Plan

No data migration is required. Deploy the code, run native receipt verification, then retry the same critical event ID or run governed reconciliation for any file-ahead suffix. Rollback is a code revert, but a reverted Windows runtime again cannot safely append governed receipts and must not be used for critical mutation.

## Open Questions

None for implementation. The separate CI repair must include the new focused Windows test before release.
