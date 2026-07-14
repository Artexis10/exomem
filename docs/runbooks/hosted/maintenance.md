# Cell maintenance gate

## Preconditions

The durable provisioner operation lock must be available. Maintenance is never
performed by deleting routes or scaling a StatefulSet by hand.

```bash
operation_id=replace-opaque-operation
cell_id=replace-opaque-cell
```

Invoke the product maintenance action with its original idempotency key. It must
close control and transfer routes, externally prove both closed, drain active
work, and only then report routing stopped.

## Verify

```bash
kubectl get ingressroute -A -l "exomem.io/cell=$cell_id"
kubectl get lease -A -l "exomem.io/operation=$operation_id"
```

After release/resume, require authenticated runtime readiness before either route
reopens. An unused ticket issued before maintenance must remain unusable.
