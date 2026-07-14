# Backup and restore

## Preconditions

Use the newest remotely verified encrypted object, not job-start time. Backup
age above 45 minutes warns; 60 minutes blocks invitations. A restore always uses
a new stopped candidate identity and a bounded scratch volume.

```bash
python3 infra/scripts/external_blackbox.py --contract infra/contracts/observability-v1.json
```

Start backup or restore through the product lifecycle endpoint with the original
idempotency key. Never archive a live filesystem or copy a source binding marker.

## Verify

```bash
kubectl get jobs -n exomem-system -l exomem.io/durability-operation
kubectl get pvc -A -l exomem.io/restore-candidate
```

For backup, require remote size/digest/metadata proof and route closure-to-resume
within two minutes. For restore, require a new destination binding, authenticated
readiness, and capture/recall/review/export.
