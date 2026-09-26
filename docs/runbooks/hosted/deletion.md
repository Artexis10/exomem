<!-- authority:non-specification -->

# Ordered tenant deletion

## Exomem Cloud cells

For a plain Cloud cell managed by `cellctl`, use the product's deletion action
to set its row's `desired_state` to `deleted`. The legacy provisioner procedure
below applies to the earlier hosted platform.

Confirm completion in this order:

1. The cell namespace is absent.
2. No PersistentVolume claims that namespace, and the recorded Hetzner volume
   is absent. Namespace deletion alone is not proof of volume deletion.
3. Every B2 object version under the exact `cells/<cell_id>/` prefix is absent,
   including hidden versions. An empty current-object listing is insufficient.
4. The per-cell B2 application key is absent by its recorded key ID.
5. The row reports `observed_state = deleted`, with its B2 key ID and wrapped
   B2 and backup key material cleared.

The controller retries incomplete steps. A failed provider observation is not
proof of absence; leave the cell deleting and resolve that observation failure.
Do not manually clear key columns, remove finalizers, or delete sibling prefixes
to force completion.

### Retained etcd snapshots

K3s encrypts Secrets at rest. Existing etcd snapshots can still retain an
encrypted copy of a deleted cell's Secret until snapshot retention expires.
The controller's `deleted` state proves the live-resource and key-removal checks
above; it does not prove that every historical snapshot has expired.

Record the applicable retention bound from the deployed local and remote
snapshot policies, including storage lifecycle or retention settings, when
reporting deletion. Do not infer a time bound from a snapshot count alone or
claim immediate erasure of snapshot copies. Handle snapshot restoration as a
separate operator recovery: prevent a restored cluster from serving deleted
cells until deletion state has been reconciled against the authoritative
control database. Do not prune shared cluster snapshots as part of one cell's
deletion.

## Preconditions

Deletion is irreversible. Confirm the opaque tenant ID, final export policy,
billing quiescence, retention deadline, and reviewed fence. The literal
`--allow-destructive` approval belongs only on an exact reviewed resource.

```bash
tenant_id=replace-opaque-tenant
test -n "$tenant_id"
: "${IDEMPOTENCY_KEY:?use the original destroy-operation key}"
bearer_file=/secure/operator/provisioner-api.bearer
destroy_request=/secure/operator/destroy-request.json
test "$(stat -c %a "$bearer_file")" = 600
test "$(stat -c %a "$destroy_request")" = 600
```

`--allow-destructive` is a Terraform saved-plan flag, not a bypass for tenant
retention. Do not use Terraform for routine tenant deletion.

Use the product destroy action. It immediately revokes service, stops billing,
removes online resources, and remains pending while Object Lock protects recovery
data. Never force-delete finalizers or buckets.

`exomem-deletion-dispatcher` is the minute-scheduled, credential-free CronJob. It
atomically claims one eligible destroy/discard operation under a precomputed
exact `exomem-deletion-<16 lowercase hex>` Job identity before creating that
Job; when no work exists it creates nothing. The Job resumes only its named,
unexpired claim, and a failed Kubernetes create leaves a bounded claim that
expires for retry. Its
namespaced RBAC can create/get/list/watch Jobs only, and admission restricts the
created Job to the pinned deletion-worker image, command, credentials, mounts,
`exomem-deletion-worker` service account, and deadline. Only that short-lived Job receives HCloud write,
the wrapping key and public recovery verifier, and the tenant-recovery and
user-export delete credentials. Complete database backups are system-scoped,
have no delete credential synced into K3s, and are never exposed to tenant
deletion. The deletion Job receives the provider-recovery public verifier, never
the signing key. Its worker admission policy permits mutation only in opaque
`exo-*` tenant namespaces or against a PV carrying an authenticated recovery
envelope; its Secret RBAC is delete-only. The separate `exomem-volume-worker`
owns authenticated PV/PVC and HCloud lifecycle work with the same governed
provider-identity signing seed.

```bash
kubectl -n exomem-platform port-forward service/exomem-provisioner 18080:8080
```

With the port-forward running, submit the exact reviewed request. Repeat the
same command and key while it returns `202`; never bypass retention with a new
operation. The explicit `--allow-destructive` review remains a human approval
marker and is not sent to the API.

```bash
curl --fail-with-body --silent --show-error --max-redirs 0 --max-time 30 \
  -X POST http://127.0.0.1:18080/cells/destroy \
  --config <(printf 'header = "Authorization: Bearer %s"\n' "$(<"$bearer_file")") \
  -H 'Content-Type: application/json' \
  -H 'X-Exomem-Provisioner-Protocol: exomem-cell-provisioner.v1' \
  -H "Idempotency-Key: ${IDEMPOTENCY_KEY}" \
  --data-binary "@${destroy_request}"
```

## Verify

```bash
kubectl get all,pvc,secret,ingressroute -A -l "exomem.io/tenant=$tenant_id"
kubectl get pv -o jsonpath='{range .items[*]}{.metadata.labels.exomem\.io/tenant}{"\n"}{end}'
kubectl -n exomem-platform get cronjob/exomem-deletion-dispatcher
kubectl -n exomem-platform get jobs -l exomem.io/deletion-job=true
```

Final `deleted` requires independently true compute, storage, key, and all-tenant-
resource proofs after locked objects expire and provider absence is verified.
The provider proof starts from the durable tenant recovery and plaintext-delivery
ledgers, then performs an exact-key B2 version/marker check for each recorded
reference with an explicit page size and hard page/item/cursor bounds, stopping
as soon as the listing moves lexicographically into prefix siblings. It takes the
maximum durable-row/live retention deadline for versions, markers, and phantom
expected versions, then deletes only exact version IDs after that lock expires and
must re-read the ledger to prove every provider object absent and every wrapped
key erased before completion. A crash after object deletion but before key
erasure therefore resumes key destruction instead of producing a false final
proof. Governance-retention bypass and routine whole-bucket scans are not
permitted.
