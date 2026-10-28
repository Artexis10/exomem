# Proposal

## Why

Each managed worker upgrade stages a new release directory under `releases/`, and nothing removed the old ones. A long-lived host accumulated tens of gigabytes of release environments. A failed staging also left its partial directory behind.

## What Changes

- After a verified promotion, the operator removes the release directories that the service no longer needs.
- The operator keeps the serving release, the release that the promotion replaced, every release that the manager records, and every entry that it did not create.
- The operator matches a release in use by directory identity, not by path spelling.
- An unreadable manager record stops retention before any removal. A removal failure or an interrupt is reported and never fails the upgrade.
- A failed or interrupted staging removes the directory that it created.
- The upgrade result carries a `release_retention` report, and `scripts/upgrade.sh` prints one summary line from it.

## Capabilities

### Modified Capabilities

- `managed-service-upgrades`: A verified promotion removes unused release environments.

## Impact

The change touches `exomem.service_upgrade` and the managed branch of `scripts/upgrade.sh`. It adds no dependency, storage or vocabulary. A host gets retention only when the operator runs from an interpreter that has this release, because `scripts/upgrade.sh` runs the operator module installed in the launcher venv. A process that runs a release interpreter outside the manager's records is not protected.
