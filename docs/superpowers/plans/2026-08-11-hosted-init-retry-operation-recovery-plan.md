# Hosted init-retry operation recovery implementation plan

Source of truth: `docs/superpowers/specs/2026-08-11-hosted-init-retry-operation-recovery-design.md`.

## Scope

Implement one operator-only provisioner command that can recover only the proven init-retry false-negative. Add only the migration/model needed for an immutable transactional receipt. Do not add an HTTP endpoint, generic operation mutation API, caller-selected state/checkpoint/error, filesystem receipt, or raw-SQL runbook. Preserve normal submit/claim/fail behavior.

Expected production files:

- new `infra/provisioner/src/exomem_provisioner/operation_recovery.py`;
- `infra/provisioner/src/exomem_provisioner/models.py` and `database.py` for the immutable receipt model/revision;
- one new Alembic `0007` recovery-receipt migration;
- shared live provider/adapter modules needed for one typed authenticated recovery observation;
- `infra/provisioner/pyproject.toml` for one console entrypoint;
- `infra/provisioner/Dockerfile` for build-time command smoke;
- `infra/scripts/verify_provisioner_image.py` for immutable-image entrypoint proof;
- a short recovery section in `docs/runbooks/hosted/cell.md` or a focused hosted recovery runbook only if the existing cell runbook cannot hold the operational sequence.

Expected tests:

- new `infra/provisioner/tests/test_operation_recovery.py`;
- PostgreSQL-backed cases in `infra/provisioner/tests/test_postgresql17.py` or a focused PostgreSQL recovery test file using its existing fixture;
- `tests/test_hosted_provisioner_image_distribution.py` and the existing image-verifier tests for the new command;
- runbook contract assertions only if a runbook changes.

Stop before expanding this allowlist.

## Step 1: lock the command and output contract red-first

Add tests that import the recovery module before it exists and require:

- modes exactly `preflight`, `reopen`, `inspect`, and `verify-receipt`;
- operation identity from stdin or a regular current-user-owned mode-`0600` file only;
- refusal of identity in argv, symlinks, non-regular files, wrong ownership, broad permissions, empty input, multiple lines, and invalid UUIDs;
- no environment, database URL, identifier, ciphertext, resource reference, envelope, DSN, or secret in stdout/stderr/errors;
- fixed JSON output keys containing only status/refusal, states, checkpoints, error code, counts, booleans, hashes, timestamps, and receipt digest;
- `--help` works without loading database/Kubernetes secrets.

Implement the smallest parser/input/output boundary. Keep the module directly executable only through the console entrypoint.

## Step 2: build a typed, hashable recovery snapshot

Reuse `canonical_request_sha256`, the existing envelope purposes/codecs, model enums, configuration validation, and repository session factory. Add private immutable snapshot types for:

- operation pre-state and preserved fields;
- tenant fence and conflict counts;
- sorted resources and resource-reference digests;
- capacity reservation;
- live Kubernetes observation;
- transactional receipt and live-observation payloads.

Canonical hashing must use sorted compact JSON with explicit conversion for UUIDs, enums, datetimes, and nulls. Tests must prove field ordering cannot change hashes and secret-bearing fields are hashed rather than serialized into receipts or output.

## Step 3: implement database preflight under canonical locks

Red-first PostgreSQL tests seed the exact historical operation shape, then independently mutate every predicate. The recovery service must:

1. refuse SQLite;
2. require the configured PostgreSQL role, schema, and sole Alembic head `0007_operation_recovery_receipt`;
3. acquire the database advisory transaction lock used by bootstrap/migration;
4. resolve the confidential operation row without output;
5. lock in repository order: tenant fence, operation, cell lock domain, capacity ledger/destructive fences/reservation, then resources;
6. decrypt and canonical-hash the request;
7. authenticate each resource reference and its stored digest;
8. return a private validated snapshot or one fixed refusal code.

The negative matrix must cover:

- missing/duplicate target resolution;
- wrong action/state/checkpoint/error;
- null cell, live claim, missing finalization, any result, provider/external identity or fence mismatch;
- tenant-fence mismatch;
- another pending/claimed tenant or cell operation;
- cell lock or equal/newer destructive fence;
- request decryption/hash/tenant/cell/operation/fence/caller-checkpoint/protocol/serve-mode mismatch;
- failure of the same runtime-target compatibility check the normal worker applies to the deployed lock;
- missing/duplicate/extra/wrong-kind resources, a route already present, cross-boundary metadata, reference-decryption failure, or digest mismatch;
- missing/duplicate/released/mismatched reservation or non-null release fields.

Every refusal asserts byte-for-byte database nonmutation.

## Step 4: add one shared exact live-observation boundary

Extend the shared production Kubernetes/provider observation boundary with one typed, read-only recovery snapshot rather than adding a second metadata parser in the command. Reuse existing adapter identity/envelope checks. Given only identities derived from the validated database snapshot, require:

- namespace, Helm release, PVC, bound PV, and provider volume present and authenticated;
- live identities/fence match durable resource metadata;
- PVC bound;
- no Kubernetes object has `deletionTimestamp`, no provider volume is deleting, and no relevant finalizer transition is active;
- init Job absent or authenticated/non-terminating; if present, running or `Complete=True` is allowed and Failed-only is refused;
- runtime admission marker and routes absent.

Query the same full same-tenant/cell resource scope and ordering used by `LiveLifecyclePlane.observe_operation()`. Require the target namespace is the sole namespace worker adoption can select and no older or foreign same-cell resource can redirect replay. Take two observations around the transaction boundary and require stable UID/resourceVersion plus non-terminating state.

Tests cover absent/running/Failed-only/completed-after-retry/terminating Job; terminating or replaced namespace/PVC/PV/release/provider volume; live identity/fence mismatch; unbound PVC; worker-adoption ambiguity; pre-existing runtime admission; and route presence. No Kubernetes object or identifier may enter public output or the receipt.

## Step 5: add the immutable transactional receipt

Add migration `0007_operation_recovery_receipt` and its ORM model. The table is one-to-one with the operation and contains only:

- fixed schema/helper source identities and database timestamps;
- old/new fixed state and checkpoint;
- approved counts and booleans;
- hashes of the old row, preserved fields, request/ciphertext, resources, reservation, tenant fence, both live observations, and committed row;
- no tenant/cell/provider identifiers except the private operation foreign key needed for the one-to-one relationship, which is never emitted.

Add database constraints and update/delete guards so committed receipts are append-only. Upgrade/downgrade tests prove exact schema. Insert the receipt in the same transaction as the operation CAS so commit, rollback, process death, pod loss, and lost acknowledgement can never split receipt from transition. A repeated command verifies and returns the existing content-free receipt digest.

## Step 6: implement the single CAS transition

`reopen` repeats the database preflight under locks, requires the second stable live observation, runs one named-bind operation update, and inserts the immutable receipt before commit. The WHERE clause includes the confidential row identity plus exact action/state/checkpoint/error, empty claim/result, provider/external identity and fence equality, prior request hash, claim generation, and prior update timestamp.

Changed columns are exactly state, checkpoint, error, claim fields, finalized timestamp, available timestamp, and updated timestamp. The new values are fixed at `PENDING`, `volume-owned`, null terminal/claim fields, and database-clock timestamps.

Before commit, re-read and assert:

- exactly one row changed;
- preserved-field hash, request/ciphertext hash, resources hash, reservation hash, tenant fence, and claim generation are unchanged;
- active reservation still exists;
- result remains absent.

Then commit both changes. Tests cover CAS loss, concurrent second invocation, already-progressed operation, rollback, process termination or lost acknowledgement, existing receipt replay, and exact changed-column/preserved-field sets.

## Step 7: implement read-only inspect

`inspect` authenticates the same confidential row input but emits only bounded content-free lifecycle facts needed for polling. It must distinguish refusal, pending/claimed/final/error, checkpoint, approved error code, resource-kind counts, active-reservation boolean, and final-proof boolean without logging identifiers.

Tests assert its output allowlist over every state and that terminal/final inspection does not mutate or decrypt result payload into output.

## Step 8: wire and prove the signed image

Add `exomem-provisioner-recover-init-retry = "exomem_provisioner.operation_recovery:main"` to project scripts. Add a Docker build-time `--help` smoke. Change the image verifier from a set of command names to an exact command-name → `module:function` mapping, assert each installed entrypoint value before loading it, and add a negative misrouting test.

Do not add Kubernetes RBAC or a standing controller. Production invokes the command only in an explicitly launched, non-network-published operator context using the existing provisioner database/envelope/Kubernetes configuration.

## Step 9: document the exact production procedure

Document content-free operator steps and stop conditions:

1. keep Substrate reconcile suspended;
2. capture exact controller state, suspend five DB-mutating CronJobs, drain their Jobs, and scale API/routine/volume deployments to zero while leaving the collector healthy;
3. prove no migration/bootstrap/other DB consumer exists;
4. run `preflight`, then `reopen`, with the identity supplied through protected stdin/file;
5. capture the content-free output digest and run `verify-receipt` against the committed database receipt;
6. restore routine worker alone and bounded-poll `inspect` to `FINAL/complete`;
7. restore API and run one manual Substrate reconcile Job at a time to original control success and cell `ready`;
8. restore volume worker/durability state and scheduled reconcile last;
9. stop on any refusal, error, conflict, resource drift, or timeout; never reverse the checkpoint manually.

The runbook must never contain a real identifier, DSN, secret, envelope, or pasteable raw SQL.

## Step 10: verification and delivery

Run at minimum:

```bash
cd infra/provisioner
RUN_POSTGRESQL17_TEST=1 uv run pytest -p no:cacheprovider -q tests/test_operation_recovery.py tests/test_postgresql17.py
uv run pytest -p no:cacheprovider -q
uv run ruff check src/exomem_provisioner tests
cd ../..
uv run pytest -q tests/test_hosted_provisioner_image_distribution.py tests/test_hosted_platform_operations.py
git diff --check
```

The PostgreSQL command must fail rather than skip if the PostgreSQL 17 container fixture cannot start. Also build the provisioner wheel/image and run `infra/scripts/verify_provisioner_image.py` against the local immutable test digest using the repository's existing image-test path. Run the full hosted validator if any root-level hosted contract/runbook test changes.

Before delivery:

- inspect diff against this allowlist;
- scan diff/output for secret-bearing values and identifiers;
- independent reviewer must approve transaction locking/CAS, migration/receipt immutability, live-observation parity, output redaction, exact entrypoint/image proof, and test matrix;
- commit, integrate current `origin/main`, rerun acceptance, push, open a ready Conventional-Commit PR, wait green, and merge only under the existing launch authorization.

After merge, wait for the exact-source signed provisioner candidate. Only then refresh/merge Release Please v0.46, wait for the signed runtime candidate, compose/review/merge the v0.46 deployment lock, pass expand release proof, deploy, and execute the documented recovery.
