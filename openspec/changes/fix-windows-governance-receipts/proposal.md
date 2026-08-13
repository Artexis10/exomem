## Why

Governance receipt append is unusable on native Windows because the receipt layer asks the CRT to open a directory. The same unsupported primitive sits on the critical durability path, so governed operations fail even when the vault and DACLs are valid. This is a release blocker for Windows services and clients: weakening or skipping the durability barrier would make a critical receipt claim persistence that was never established.

## What Changes

- Route receipt directory and monthly-file access through retained, no-follow filesystem handles that work on both POSIX and native Windows.
- Flush critical receipt directories on Windows through write-capable native directory handles before advancing the SQLite durable head.
- Preserve fail-closed retry and reconciliation semantics when any file or directory durability operation fails.
- Add native Windows regression coverage for ordinary receipts, critical receipts, reparse refusal, namespace races, and durability failures.

This does not change the receipt schema, JSONL authority, SQLite sidecar format, public API, disclosure ladder, or dependencies.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `disclosure-evidence`: make receipt filesystem safety and critical durability explicit and enforceable on every supported operating system, including native Windows.

## Impact

- Affected code: `src/exomem/governance/receipts.py` and the existing secure filesystem helpers it reuses.
- Affected tests: governance receipt safety/durability tests plus a focused native Windows regression suite.
- Operations: valid Windows NSSM and user-owned runtimes regain governed receipt append without an ACL migration or data migration.
- CI: the native Windows receipt contract needs a focused Windows gate; coordinate that workflow edit with the in-flight CI repair before this change merges.
