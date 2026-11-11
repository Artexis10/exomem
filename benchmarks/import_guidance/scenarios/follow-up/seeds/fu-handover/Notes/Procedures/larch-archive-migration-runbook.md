---
exomem_id: 713e85c1-f218-57b8-abfd-6723e8d2a2d7
type: procedure
status: active
created: 2026-09-01
updated: 2026-10-05
sources: []
summary: Use when moving the Larch document archive to the new storage cluster.
tags: [archive, migration]
---

# Larch archive migration runbook

## Procedure

1. Freeze writes to the Larch archive before the cutover starts.
2. Cut over inside the maintenance window: Sunday 02:00 to 04:00 UTC.
3. Run the checksum comparison on the new cluster.
4. If the comparison fails, the rollback owner is Pell Aldous. Only Pell Aldous may order the rollback.

## Not for

The Fennel archive. It has its own runbook.
