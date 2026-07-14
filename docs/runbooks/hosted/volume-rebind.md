# Retained-volume rebind

## Preconditions

The original HCloud `volumeHandle`, location, opaque tenant/cell IDs, operation,
and maximum provider fence must be in external state. The volume must be detached
and its immutable labels must match. Do not use the namespaced credential.

```bash
kubectl get pv -o custom-columns=NAME:.metadata.name,HANDLE:.spec.csi.volumeHandle,PHASE:.status.phase
```

Run the privileged volume lifecycle recovery action. It creates a static PV/PVC
bound to the original handle and rejects a location or fence mismatch.

## Verify

```bash
kubectl get pv -o jsonpath='{range .items[*]}{.spec.csi.volumeHandle}{" "}{.status.phase}{"\n"}{end}'
kubectl get pvc -n cell-replace-opaque
```

Require the recorded handle, one Bound PVC, authenticated readiness, and the
pre-loss canary note. A new dynamic volume is a failed recovery.
