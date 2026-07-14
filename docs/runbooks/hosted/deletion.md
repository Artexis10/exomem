# Ordered tenant deletion

## Preconditions

Deletion is irreversible. Confirm the opaque tenant ID, final export policy,
billing quiescence, retention deadline, and reviewed fence. The literal
`--allow-destructive` approval belongs only on an exact reviewed resource.

```bash
tenant_id=replace-opaque-tenant
test -n "$tenant_id"
```

`--allow-destructive` is a Terraform saved-plan flag, not a bypass for tenant
retention. Do not use Terraform for routine tenant deletion.

Use the product destroy action. It immediately revokes service, stops billing,
removes online resources, and remains pending while Object Lock protects recovery
data. Never force-delete finalizers or buckets.

## Verify

```bash
kubectl get all,pvc,secret,ingressroute -A -l "exomem.io/tenant=$tenant_id"
kubectl get pv -o jsonpath='{range .items[*]}{.metadata.labels.exomem\.io/tenant}{"\n"}{end}'
```

Final `deleted` requires independently true compute, storage, key, and all-tenant-
resource proofs after locked objects expire and provider absence is verified.
