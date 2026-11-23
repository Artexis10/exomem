---
exomem_id: 9a1ef144-7f0a-55b9-a016-ed5464152bb8
type: entity
entity_type: concept
status: active
created: 2026-02-20
updated: 2026-09-11
domain: software
tags: [software, project, finance]
---

# Pebble Ledger

## Summary

A small command-line tool written in Rust that imports bank statements and reports household spending. It keeps everything in one SQLite file and ships binaries for Linux, macOS and Windows.

## Why in the KB

It is the main side project. Most schema, import and release notes belong to it.

## Current state

The import and the monthly report work. The open items are faster imports of the full history, tag support and a tidier release pipeline. Work happens in short evening sessions, a few hours a week.

## Relations

- relates_to [[Entities/Decisions/Keep Pebble Ledger data in one SQLite file]]
- relates_to [[Notes/Procedures/pebble-ledger-release-checklist]]
