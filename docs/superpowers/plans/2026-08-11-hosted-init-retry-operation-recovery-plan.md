# Hosted init-retry operation recovery implementation plan

Source of truth: `docs/superpowers/specs/2026-08-11-hosted-init-retry-operation-recovery-design.md`.

## Scope

Implement one operator-only provisioner command that can recover only the proven init-retry false-negative. Do not add an HTTP endpoint, migration, generic operation mutation API, caller-selected state/checkpoint/error, or raw-SQL runbook. Preserve normal submit/claim/fail behavior.

Expected production files:

- new `infra/provisioner/src/exomem_provisioner/operation_recovery.py`;
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

- modes exactly `preflight`, `reopen`, `inspect`, and `finalize-receipt`;
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
- prepared/committed receipt payloads.

Canonical hashing must use sorted compact JSON with explicit conversion for UUIDs, enums, datetimes, and nulls. Tests must prove field ordering cannot change hashes and secret-bearing fields are hashed rather than serialized into receipts or output.

## Step 3: implement database preflight under canonical locks

Red-first PostgreSQL tests seed the exact historical operation shape, then independently mutate every predicate. The recovery service must:

1. refuse SQLite;
2. require the configured PostgreSQL role, schema, and sole Alembic head `0006_operation_wire_protocol`;
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
- missing/duplicate/extra/wrong-kind resources, a route already present, cross-boundary metadata, reference-decryption failure, or digest mismatch;
- missing/duplicate/released/mismatched reservation or non-null release fields.

Every refusal asserts byte-for-byte database nonmutation.

## Step 4: add exact live-observation validation

Reuse the production Kubernetes/provider observation boundary rather than adding a second metadata parser. Given only identities derived from the validated database snapshot, require:

- namespace, Helm release, PVC, and provider volume present and authenticated;
- live identities/fence match durable resource metadata;
- PVC bound;
- init Job present with `Complete=True` (historical failed count/condition allowed);
- runtime admission marker and routes absent.

Tests cover absent/running/Failed-only Job, completed-after-retry Job, live identity/fence mismatch, unbound PVC, pre-existing runtime admission, and route presence. No Kubernetes object or identifier may enter public output/receipt.

## Step 5: implement atomic prepared/final receipt handling

Create a private receipt directory/file boundary that:

- rejects symlinked/non-owned/broad-permission directories and files;
- uses exclusive creation for the prepared receipt;
- writes canonical JSON, fsyncs file and parent directory;
- contains only approved fixed fields/hashes/counts/booleans and a random nonce;
- finalizes through a same-directory temporary file plus atomic rename and parent fsync;
- never overwrites an unrelated or finalized receipt.

Tests inject failures at create/write/fsync/rename/parent-fsync boundaries. Before database commit, failures roll back. After a committed transition, finalization failure produces a fixed `receipt-finalization-required` result, and only `finalize-receipt` may finish the matching prepared receipt.

## Step 6: implement the single CAS transition

`reopen` repeats the full preflight under the same transaction, writes/fsyncs the prepared receipt, and runs one named-bind update. The WHERE clause includes the confidential row identity plus exact action/state/checkpoint/error, empty claim/result, provider/external identity and fence equality, prior request hash, claim generation, and prior update timestamp.

Changed columns are exactly state, checkpoint, error, claim fields, finalized timestamp, available timestamp, and updated timestamp. The new values are fixed at `PENDING`, `volume-owned`, null terminal/claim fields, and database-clock timestamps.

Before commit, re-read and assert:

- exactly one row changed;
- preserved-field hash, request/ciphertext hash, resources hash, reservation hash, tenant fence, and claim generation are unchanged;
- active reservation still exists;
- result remains absent.

Then commit and finalize the receipt. Tests cover CAS loss, concurrent second invocation, already-progressed operation, commit failure, receipt-finalization recovery, and exact changed-column/preserved-field sets.

## Step 7: implement read-only inspect

`inspect` authenticates the same confidential row input but emits only bounded content-free lifecycle facts needed for polling. It must distinguish refusal, pending/claimed/final/error, checkpoint, approved error code, resource-kind counts, active-reservation boolean, and final-proof boolean without logging identifiers.

Tests assert its output allowlist over every state and that terminal/final inspection does not mutate or decrypt result payload into output.

## Step 8: wire and prove the signed image

Add `exomem-provisioner-recover-init-retry = "exomem_provisioner.operation_recovery:main"` to project scripts. Add a Docker build-time `--help` smoke. Add the command to `_ENTRYPOINTS` in `verify_provisioner_image.py`, then update image-distribution tests to require it and prove the entrypoint loads as callable from the immutable image.

Do not add Kubernetes RBAC or a standing controller. Production invokes the command only in an explicitly launched, non-network-published operator context using the existing provisioner database/envelope/Kubernetes configuration.

## Step 9: document the exact production procedure

Document content-free operator steps and stop conditions:

1. keep Substrate reconcile suspended;
2. capture exact controller state, suspend five DB-mutating CronJobs, drain their Jobs, and scale API/routine/volume deployments to zero while leaving the collector healthy;
3. prove no migration/bootstrap/other DB consumer exists;
4. run `preflight`, then `reopen`, with the identity supplied through protected stdin/file and receipt in a private directory;
5. verify committed receipt;
6. restore routine worker alone and bounded-poll `inspect` to `FINAL/complete`;
7. restore API and run one manual Substrate reconcile Job at a time to original control success and cell `ready`;
8. restore volume worker/durability state and scheduled reconcile last;
9. stop on any refusal, error, conflict, resource drift, or timeout; never reverse the checkpoint manually.

The runbook must never contain a real identifier, DSN, secret, envelope, or pasteable raw SQL.

## Step 10: verification and delivery

Run at minimum:

```bash
cd infra/provisioner
uv run pytest -p no:cacheprovider -q tests/test_operation_recovery.py tests/test_postgresql17.py
uv run pytest -p no:cacheprovider -q
uv run ruff check src/exomem_provisioner tests
cd ../..
uv run pytest -q tests/test_hosted_provisioner_image_distribution.py tests/test_hosted_platform_operations.py
git diff --check
```

Also build the provisioner wheel/image and run `infra/scripts/verify_provisioner_image.py` against the local immutable test digest using the repository's existing image-test path. Run the full hosted validator if any root-level hosted contract/runbook test changes.

Before delivery:

- inspect diff against this allowlist;
- scan diff/output for secret-bearing values and identifiers;
- independent reviewer must approve transaction locking/CAS, receipt crash boundaries, output redaction, entrypoint/image proof, and test matrix;
- commit, integrate current `origin/main`, rerun acceptance, push, open a ready Conventional-Commit PR, wait green, and merge only under the existing launch authorization.

After merge, wait for the exact-source signed provisioner candidate. Only then refresh/merge Release Please v0.46, wait for the signed runtime candidate, compose/review/merge the v0.46 deployment lock, pass expand release proof, deploy, and execute the documented recovery.
