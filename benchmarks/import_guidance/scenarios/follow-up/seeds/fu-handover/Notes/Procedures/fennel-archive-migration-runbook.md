---
exomem_id: 67290a65-f482-5080-9a0d-8fc229675726
type: procedure
status: active
created: 2026-09-01
updated: 2026-10-05
sources: []
summary: Use when moving the Fennel document archive to the new storage cluster.
tags: [archive, migration]
---

# Fennel archive migration runbook

## Procedure

1. Export the Fennel index before the cutover starts.
2. Cut over inside the maintenance window: Saturday 23:00 to 01:00 UTC.
3. Rebuild the Fennel index on the new cluster.
4. If the rebuild fails, the rollback owner is Tobin Rask. Only Tobin Rask may order the rollback.

## Not for

The Larch archive. It has its own runbook.
