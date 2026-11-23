---
exomem_id: fe051b2d-3902-5aa5-bbc5-48f29b3c17e8
type: failure
status: active
created: 2026-08-03
updated: 2026-08-03
sources: []
projects: [pebble-ledger]
tags: [software, import, data-loss]
---

# Import lost rows after a schema change

## What happened

After a migration that renamed a column, the importer silently dropped every row whose merchant text was empty. About forty transactions went missing from one month.

## Cause

The new insert statement treated the empty merchant as a constraint violation, and the import loop swallowed the error to keep going.

## Lesson

The importer now counts rows read against rows stored and refuses to finish when the two differ. Errors in the loop are collected and printed, never swallowed.

## Relations

- relates_to [[Notes/Research/pebble-ledger/csv-import-edge-cases]]
