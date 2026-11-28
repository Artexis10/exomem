## Why

Two clients that sign in as the vault owner can read each other's queued save. A receipt may contain unpublished arguments. The owner needs a way to recover old saves without granting that access to every connector.

## What Changes

- Match connector receipt reads to the account, client, and login that sent the save.
- Apply the same boundary to receipt lists, counts, positions, notices, and retries.
- Preserve owner recovery through the existing local administrative command.

## Capabilities

### Modified Capabilities

- `busy-write-intake` checks live receipt authority before disclosing stored data.

## Impact

The intake keeps its existing database and accepts the same writes. Receipt details remain available to the originating login and the local administrator.
