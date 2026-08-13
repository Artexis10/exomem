## Context

Receipt evidence is authoritative JSONL with a SQLite durable-head sidecar. Critical events advance that head only after the JSONL prefix and the directory chain through `Knowledge Base` are durable. POSIX implements the namespace operations with `dir_fd` and `O_NOFOLLOW`; native Windows currently falls through to `os.open(directory)`, which the CRT rejects with `PermissionError`.

The repository already has two relevant primitives: retained non-reparse Windows directory/file handles in `mutation_lock.py`, and a native directory handle substrate in `vault.py`. Native verification on the development host established the access contract: `FlushFileBuffers` succeeds on a raw Windows directory handle opened with `GENERIC_WRITE`; metadata-only or read-only access receives access denied, and passing the raw handle to `os.fsync` is invalid. Microsoft documents that directory handles require `FILE_FLAG_BACKUP_SEMANTICS`, and that `FlushFileBuffers` requires `GENERIC_WRITE`.

## Goals / Non-Goals

**Goals:**

- Restore ordinary and critical receipt append/verify on native Windows.
- Retain direct-child, no-follow, anti-reparse, identity, and namespace-race guarantees.
- Keep the critical ordering JSONL flush → directory-chain flush → durable-head commit.
- Leave a file-ahead critical suffix recoverable without duplication after any refused durability step.
- Add deterministic cross-platform tests and real native Windows coverage.

**Non-Goals:**

- Changing receipt JSON, hashing, sequencing, month rotation, or SQLite schema.
- Moving evidence authority into SQLite.
- Weakening a durability failure into a warning or best-effort success.
- Adding dependencies, changing the disclosure ladder, or repairing DACLs.

## Decisions

### Reuse retained secure handles

Receipt code will use the existing secure-directory and secure-child helpers rather than adding a second Windows filesystem adapter. On Windows those helpers retain every ancestor with metadata-only access without following reparse points, validate that the child handle remains inside the retained directory, and keep handles alive through I/O. POSIX continues to use descriptor-relative no-follow opens.

Exclusive create will map `O_CREAT | O_EXCL` to `CREATE_NEW`; the current `OPEN_ALWAYS` mapping does not enforce exclusivity on Windows. Receipt instance creation will traverse the lexical path through the retained helper instead of resolving away a same-vault junction and then calling bare recursive `mkdir`.

Alternative rejected: keep pathname opens with pre/post `lstat`. It can detect many swaps but cannot retain every Windows ancestor or guarantee exclusive creation.

### Flush directories with write-capable native handles

On Windows `_fsync_directory` will retain the target's ancestors with metadata-only access, open only the final exact non-reparse directory through `CreateFileW` using `GENERIC_WRITE`, `FILE_FLAG_BACKUP_SEMANTICS`, and no delete sharing, verify it is the direct child of the retained parent, then call `FlushFileBuffers` on the raw handle and close it exactly once. POSIX remains unchanged.

Write access must not propagate to the drive root or ordinary ancestors: valid user vaults cannot open `C:\` or `C:\Users` with `GENERIC_WRITE`, and those ancestors do not need it for flushing the leaf.

Alternative rejected: skip directory fsync on Windows. That would advance the durable head without satisfying the contract it represents.

### Preserve failure and recovery ordering

Every open, identity, or flush error remains a content-free `ReceiptError`. A critical JSONL record may be ahead of the durable sidecar after a refusal; an exact-ID retry or verified reconcile must re-establish the file and directory barriers before promotion. Neither path allocates or appends a duplicate event.

Alternative rejected: catch `PermissionError` and commit the sidecar anyway. That converts an unsupported primitive into false durability.

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
