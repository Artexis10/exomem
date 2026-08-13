## Why

Governance receipt append and deletion/recovery lifecycle durability are unusable on native Windows because both layers ask the CRT to open directories. Governed operations and even ungoverned atomic lifecycle moves fail when flushing tombstone or rename barriers despite valid vault paths and DACLs. This is a release blocker: weakening or skipping those barriers would claim critical receipt or lifecycle persistence that was never established.

## What Changes

- Route receipt directory and monthly-file access through retained, no-follow filesystem handles that work on both POSIX and native Windows.
- Share one secure native Windows directory-flush primitive between receipts and lifecycle, while preserving each domain's content-free errors.
- Flush critical receipt directories before advancing the SQLite durable head and flush lifecycle tombstone/unlink/rename directories at their existing checkpoints.
- Preserve fail-closed retry and reconciliation semantics when any file or directory durability operation fails.
- Add native Windows regression coverage for receipts, governed delete/recovery, ungoverned atomic rename, reparse refusal, namespace races, and durability failures.

This does not change the receipt schema, JSONL authority, SQLite sidecar format, public API, disclosure ladder, or dependencies.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `disclosure-evidence`: make receipt filesystem safety and critical durability explicit and enforceable on every supported operating system, including native Windows.

## Impact

- Affected code: `src/exomem/governance/receipts.py`, `src/exomem/governance/lifecycle.py`, and shared secure filesystem helpers.
- Affected tests: governance receipt and lifecycle safety/durability tests plus focused native Windows regression suites.
- Operations: valid Windows NSSM and user-owned runtimes regain governed receipt append without an ACL migration or data migration.
- CI: the native Windows receipt contract needs a focused Windows gate; coordinate that workflow edit with the in-flight CI repair before this change merges.
