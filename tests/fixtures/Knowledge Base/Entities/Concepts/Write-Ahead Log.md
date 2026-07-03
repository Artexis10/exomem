---
type: entity
entity_type: concept
status: active
created: 2026-03-28
updated: 2026-03-28
domain: databases
tags: [durability, wal, storage]
---

# Write-Ahead Log

## Summary

A write-ahead log (WAL) is an append-only log a database writes changes to BEFORE applying them to the main data pages. Because the intent is durably recorded first, a crash mid-update is recoverable: on restart the engine replays the log to redo committed changes and roll back incomplete ones. The WAL is what lets a database promise durability without an fsync on every page.

## Why in the KB

The core mechanism behind crash recovery and point-in-time restore in relational databases; referenced whenever durability-versus-latency trade-offs come up.

## Connections

- [[Knowledge Base/Entities/Concepts/Envelope]]
