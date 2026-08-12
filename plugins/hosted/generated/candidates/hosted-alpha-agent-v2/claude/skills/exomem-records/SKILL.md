---
name: exomem-records
description: Capture durable observed state in an existing compatible collection.
required_tools: [record_memory]
---

# Observed records

Use `record_memory` when a user gives a durable observed event or state: a measurement,
session, transaction, maintenance event, symptom, or inventory change. First inspect the
one compatible collection. Append only when its identity, date, provenance, and fields are
clear. If there is no compatible collection, describe and propose the collection; do not
silently create a schema or write the observation into another layer.
