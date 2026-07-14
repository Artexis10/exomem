# Ordered tenant deletion

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

The continuously reconciled `exomem-deletion-worker` is the only workload that
receives HCloud write plus the tenant-recovery and user-export delete
credentials. Complete database backups are system-scoped and are never exposed
to tenant deletion; their aggregate retention cleanup is part of database
durability. The deletion worker receives the provider-recovery public verifier,
never the signing key. Its
admission policy permits mutation only in opaque `exo-*` tenant namespaces or
against a PV carrying an authenticated recovery envelope; its Secret RBAC is
delete-only. The separate `exomem-volume-worker` owns authenticated PV/PVC and
HCloud lifecycle work with the same governed provider-identity signing seed.

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
kubectl -n exomem-platform rollout status deployment/exomem-deletion-worker --timeout=120s
```

Final `deleted` requires independently true compute, storage, key, and all-tenant-
resource proofs after locked objects expire and provider absence is verified.
