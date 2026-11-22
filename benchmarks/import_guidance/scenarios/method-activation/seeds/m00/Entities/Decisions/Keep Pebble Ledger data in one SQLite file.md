---
exomem_id: fe332375-9e9a-5f55-8c69-76ac8981407b
type: entity
entity_type: decision
status: active
created: 2026-02-24
updated: 2026-02-24
decided: 2026-02-24
project: pebble-ledger
decision_status: accepted
tags: [software, sqlite]
---

# Keep Pebble Ledger data in one SQLite file

## Context

The tool is for one household. A server database would add setup and maintenance for no benefit.

## Decision

All data lives in a single SQLite file next to the config. Backups are copies of that file.

## Alternatives considered

A local Postgres server. Rejected because it adds an install step for every user.

## Consequences

Backups are manual copies and must happen while the tool is idle.

## Relations

- relates_to [[Entities/Concepts/Pebble Ledger]]
