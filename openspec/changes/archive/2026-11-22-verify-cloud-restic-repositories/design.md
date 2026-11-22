# Cloud backup repository verification

## Context

The controller starts backup and restore Jobs, then observes their results in fleet-wide Kubernetes reads. Restic 0.19.1 [takes an exclusive repository lock for a check](https://github.com/restic/restic/blob/v0.19.1/cmd/restic/cmd_check.go#L214-L233).

## Decisions

### Schedule verification through the controller

Run a distinct Job with the current cell image and existing restic repository selection. Mount temporary scratch, with no vault volume, Kubernetes API token, or media credentials. Validate the manifest under existing Job admission and network policies.

Start at most one verification Job across the fleet, in an idle cell with a prior successful backup. Select the oldest due eligible cell. An empty allowlist disables new work. Settle or cancel existing verification when the allowlist changes. Give managed operations that need the same repository priority. Retain restic locking for independent repository clients.

### Record evidence before cleanup

Store a versioned verification receipt on the cell StatefulSet. Bind it to the configured repository address, attempt, and exact slice. Patch the observed StatefulSet UID. Read Jobs and Pods with paged fleet-wide observation. Accept successful exit only from the matching Job and Pod. Persist the outcome before deleting the completed Job.

An unavailable observation, missing attempt, deadline, failed check, or lock conflict preserves the next slice and triggers backoff. Preserve the last verified slice and its time. A replaced StatefulSet or changed repository address starts with unknown verification coverage.

### Bound work and retain the existing backup contract

Use restic's `--read-data-subset=n/N`. Advance and wrap on success. Restic assigns packs using the first byte of their IDs. As pack membership changes, successful rotations measure observed slices. A recovery exercise separately proves restore.

The fleet planning target is 43 cells per node with 10 GiB of live data per cell. Retained repositories can hold more data. Defaults are a five-minute Job deadline, one daily slice, 24 slices, and an 8 MiB/s download limit. The rehearsal measures observed runtime and the corrupted pack size. Verification adds no per-cell read to the settled controller pass.

## Proof

Use the existing isolated k3s and MinIO test environment with the production-pinned restic binary.

1. Back up synthetic bytes and observe a passing first slice.
2. Corrupt one byte in a pack assigned to the next slice and observe a failing verification.
3. Verify the cell still serves and retains its successful backup receipt.
4. Restore the exact pack bytes and restart the controller. Verify it repeats the failed slice and then advances.

MinIO proves repository behavior and workload coordination. Provider-specific credential and transfer behavior require separate rollout evidence. Roll back by clearing the allowlist and waiting for verification Pods to finish before downgrading.
