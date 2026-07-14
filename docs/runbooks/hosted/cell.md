# Cell inspection and retry

## Preconditions

Use only opaque tenant, cell, request, and operation IDs from the provisioner.
Do not use email/name selectors, edit the database, or hand-create a cell.

```bash
cell_namespace=cell-replace-opaque
kubectl get namespace "$cell_namespace" -o jsonpath='{.metadata.labels.exomem\.io/tenant-cell}{"\n"}'
kubectl get statefulset,pvc,service,networkpolicy -n "$cell_namespace"
```

Retry through the same Substrate endpoint and idempotency key. A pending result
is healthy progress; never invent a new key to bypass it.

## Verify

```bash
kubectl get pvc -n "$cell_namespace" -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.phase}{"\n"}{end}'
kubectl get pods -n "$cell_namespace" -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.containerStatuses[0].ready}{"\n"}{end}'
```

Exactly one 10 GiB claim and one ready serving pod are expected.
