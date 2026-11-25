---
exomem_id: 1c02539c-e2e5-5f8b-8e2c-842606650ed2
type: entity
entity_type: library
status: active
created: 2026-03-09
updated: 2026-03-09
language: Rust
used_in: [pebble-ledger]
tags: [software]
---

# serde

## Summary

The Rust serialization library. Pebble Ledger uses it to read its configuration file and to export reports as JSON.

## Why in the KB

Config parsing errors came from its strict handling of unknown keys, which the configuration notes mention.

## Usage notes

- The config struct rejects unknown keys on purpose, so a typo in the file fails loudly instead of being ignored.
- Reports are exported through the same derive attributes, which keeps the JSON shape in step with the code.
- Date fields use a small custom deserializer because the bank formats differ per account.

## Relations

- used_by [[Entities/Concepts/Pebble Ledger]]
