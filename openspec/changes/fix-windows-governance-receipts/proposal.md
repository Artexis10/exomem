## Why

Governance receipt append and deletion/recovery lifecycle durability are unusable on native Windows because both layers ask the CRT to open directories. Governed operations and even ungoverned atomic lifecycle moves fail when flushing tombstone or rename barriers despite valid vault paths and DACLs. Receipt-first startup also creates the shared writer-state root with inherited permissions before the hardened runtime initializer can establish its protected DACL, making a later writer initialization fail solely because receipts ran first. These are release blockers: weakening durability or allowing initialization order to choose the runtime security state is not acceptable.

## What Changes

- Route receipt directory and monthly-file access through retained, no-follow filesystem handles that work on both POSIX and native Windows.
- Share one secure native Windows directory-flush primitive between receipts and lifecycle, while preserving each domain's content-free errors.
- Flush critical receipt directories before advancing the SQLite durable head and flush lifecycle tombstone/unlink/rename directories at their existing checkpoints.
- Establish the existing principal-private Windows DACL before any first-use writer or receipt lock creates a state-root artifact; refuse a pre-existing unsafe root actionably without repairing it.
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
- Operations: valid Windows NSSM and user-owned runtimes regain governed receipt append without a data migration; pre-existing unsafe state roots retain the existing exact-path remediation rather than being repaired implicitly.
- Principal boundary: LocalSystem service processes and normal-user direct CLI processes continue to require separate writer-state roots; the same-principal first-use convergence in this change does not weaken the exact trustee-set contract.
- CI: the native Windows receipt contract needs a focused Windows gate; coordinate that workflow edit with the in-flight CI repair before this change merges.
