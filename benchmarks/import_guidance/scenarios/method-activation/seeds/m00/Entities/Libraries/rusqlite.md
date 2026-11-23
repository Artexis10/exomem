---
exomem_id: 148e17e3-b26c-5747-9f19-36149d645b68
type: entity
entity_type: library
status: active
created: 2026-03-08
updated: 2026-03-08
language: Rust
used_in: [pebble-ledger]
tags: [software, sqlite]
---

# rusqlite

## Summary

A Rust wrapper around SQLite. Pebble Ledger uses it for every read and write of the ledger file.

## Why in the KB

The schema notes and the import notes both depend on how this library handles transactions and prepared statements.

## Usage notes

- Every import runs inside one transaction, so a failed run leaves the file untouched.
- Prepared statements are cached per connection, which matters for the bulk insert of statement rows.
- Opening the file from two processes at once is possible but the second writer waits, so the tool takes a simple lock file first.

## Relations

- used_by [[Entities/Concepts/Pebble Ledger]]
