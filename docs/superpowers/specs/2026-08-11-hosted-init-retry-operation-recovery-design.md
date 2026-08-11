# Hosted init-retry operation recovery

## Context

The first capacity-managed Hosted reviewer tenant reached a terminal provider error after all durable infrastructure had been created. Its Kubernetes init Job eventually completed successfully, but an earlier failed Pod left `status.failed > 0`. The old provisioner observer treated the Job as both complete and failed and terminalized the provider operation with `PROVISIONER_PROVIDER_METADATA_CONFLICT`. The observer fix is merged, tested, and published, but replaying the same API operation only returns the stored terminal result.

The tenant must be recovered in place. Creating another operation would conflict with its identity-bound resources and active capacity reservation. Deleting resources or rewriting the request would discard proof and risk duplication.

## Decision

Ship a one-purpose, operator-only recovery helper in the signed provisioner image. It can reopen only the exact init-retry false-negative shape:

`PROVISION / ERROR / failed / PROVISIONER_PROVIDER_METADATA_CONFLICT`

to:

`PROVISION / PENDING / volume-owned`

The helper is not a general operation editor and is not exposed through the provisioner API. It accepts the confidential internal operation identifier only through standard input or a current-user-owned mode-`0600` file, never through command-line arguments.

Raw SQL is rejected because it cannot safely authenticate the encrypted request and resource references and would not create a trustworthy receipt. Abandoning the tenant is rejected because the existing resources and reservation are deliberately retained after terminal provision failure.

## Components

### Recovery command

The provisioner package exposes a console command with four fixed modes:

- `preflight`: validates every invariant without mutation;
- `reopen`: repeats preflight under locks, performs the single compare-and-swap transition, and commits a receipt;
- `inspect`: returns content-free state for bounded polling;
- `finalize-receipt`: repairs receipt finalization after a committed transition without reopening again.

Modes do not accept caller-selected checkpoints, actions, states, error codes, tenants, cells, fences, resource identities, or SQL fragments.

### Repository recovery service

A dedicated service owns validation and mutation. It uses the existing database/envelope configuration and follows the repository's fence-first lock order. The transition is isolated from normal submission and claim APIs so it cannot weaken their terminal-state behavior.

### Receipt writer

Before mutation, the helper creates and fsyncs an exclusive prepared receipt in a private, non-repository directory. The receipt contains hashes, booleans, counts, fixed state names, helper/image identity, and timestamps only. It never contains identifiers, ciphertext, resource references, credentials, DSNs, decrypted payloads, or recovery envelopes.

After commit, the helper atomically finalizes the receipt with the committed-row hash. If finalization fails, workers stay stopped and `finalize-receipt` completes the existing receipt; `reopen` cannot run again.

## Preflight contract

The helper refuses unless all of these facts hold under the same transaction and live-observation boundary:

1. PostgreSQL is in use, the current role/schema are exact, and the database is at the one expected Alembic revision.
2. Exactly one target operation exists with action `PROVISION`, state `ERROR`, checkpoint `failed`, error `PROVISIONER_PROVIDER_METADATA_CONFLICT`, no claim, no result, a finalization timestamp, and matching external/provider operation and fence fields.
3. The tenant fence equals the operation fence. No other pending or claimed operation, cell-operation lock, or equal/newer destructive capacity fence exists for the tenant or cell.
4. The stored request decrypts, canonicalizes to its stored hash, and matches the operation's tenant, cell, operation, fence, caller checkpoint, protocol, and serve-mode intent.
5. Durable resources are exactly one each of `HELM_RELEASE`, `KUBERNETES_NAMESPACE`, `PVC`, and `VOLUME`; their encrypted references authenticate, their digests match, and their stored identity matches the operation. No route exists.
6. Exactly one matching active user capacity reservation exists and every release field is null.
7. Live namespace, release, PVC, and volume identity match the durable registry; the PVC is bound; the exact init Job has `Complete=True`; runtime admission and routes do not yet exist.
8. The operation still matches the preflight row hash, request hash, claim generation, and update timestamp at compare-and-swap time.

Any mismatch is a hard no-op with a fixed refusal code.

## Mutation contract

The transaction changes only:

- state to `PENDING`;
- checkpoint to `volume-owned`;
- error, claim, and finalization fields to null;
- availability and update timestamps to the current database clock.

It preserves the encrypted request, canonical request hash, all identities and fences, caller checkpoint, progress, retry interval, claim generation, result fields, resources, tenant fence, capacity ledger, reservation, and creation timestamp.

The compare-and-swap includes the exact old action/state/checkpoint/error, empty claim/result fields, canonical request hash, claim generation, and prior update timestamp. Exactly one updated row is required. A later invocation returns `already-progressed` or `refused` without mutation.

## Deployment and recovery flow

The helper and observer fix must be present in the same signed provisioner candidate selected by the deployment lock. Before reopening, Substrate reconciliation and all database-mutating provisioner workloads are quiesced; the three direct provisioner deployments are stopped; transient migration/bootstrap/database consumers are absent; and a fresh collector receipt remains available.

After a committed receipt:

1. restore only the routine provisioner worker;
2. use content-free `inspect` polling for at most ten minutes;
3. require progression from `volume-owned` through initialization, runtime admission, route opening, and `FINAL/complete`;
4. stop on any terminal error, fence/capacity conflict, unexpected resource mutation, or timeout;
5. restore the API only after provider finalization;
6. run one bounded manual Substrate reconcile Job at a time while the scheduled CronJob remains suspended;
7. accept only when the original control operation succeeds and the tenant/cell is bound and ready with authenticated health;
8. restore the remaining controllers to their exact captured states and unsuspend scheduled reconciliation last.

There is no reverse checkpoint mutation after the helper commits. If the worker fails, stop it and diagnose the preserved pending operation. If provider finalizes but control reconciliation fails, preserve provider final state and replay only the control operation.

## Security and observability

- No public or admin HTTP recovery endpoint.
- No identifiers or secret-bearing values in argv, stdout, logs, receipts, tests, or operator evidence.
- The command refuses SQLite and noncanonical database role/schema/revision.
- The image verification gate proves the console command is present and routes only to the recovery module.
- Normal repository terminal-state and idempotency behavior remains unchanged.
- Production evidence records only fixed states, booleans, counts, hashes, timestamps, and receipt digests.

## Testing

Tests must prove red-first behavior for the historical retry-success shape and cover:

- exact happy-path preflight and reopen;
- every old-state/action/checkpoint/error mismatch;
- claim/result/finalization mismatches;
- request decryption, canonical-hash, identity, checkpoint, protocol, and fence mismatches;
- missing, duplicate, extra, unauthenticated, or mismatched resources;
- missing/released/mismatched reservation and destructive-fence conflicts;
- absent/running/failed init Job, with only `Complete=True` accepted even when historical failures exist;
- compare-and-swap lost races and second-invocation no-op behavior;
- prepared/finalized receipt permissions, fsync/atomicity, redaction, and crash recovery;
- exact changed-column and preserved-field hash assertions;
- console entrypoint and built-image smoke verification;
- PostgreSQL integration at the canonical schema revision.

The focused recovery suite, full provisioner suite, Ruff, image verification, deployment-lock composition gates, and independent security/code review must pass before production use.

## Success criteria

The signed deployment contains the observer fix and recovery helper; the helper reopens only the proven operation and emits a content-free committed receipt; the provider operation reaches `FINAL/complete` without duplicate resources or reservations; the original control operation reaches successful terminal state; and the fresh reviewer tenant reaches authenticated `ready` before database credential rotation begins.
