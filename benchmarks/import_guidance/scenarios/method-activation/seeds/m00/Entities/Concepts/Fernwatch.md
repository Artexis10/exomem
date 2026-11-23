---
exomem_id: 40d33b9a-ffec-5465-a0cc-edbcf5636d5e
type: entity
entity_type: concept
status: active
created: 2026-05-30
updated: 2026-09-23
domain: software
tags: [software, project, gardening]
---

# Fernwatch

## Summary

A small web dashboard that charts soil moisture and temperature readings from the sensors on the allotment. Readings are stored in Postgres, and the front end is bundled and tested in CI.

## Why in the KB

It is the second side project, and the place where database and build questions come up most.

## Current state

The charts and the soil-moisture alerts work. The open items are slow weekly totals, a long CI run and a mobile layout. The dashboard has one user and about four million readings.

## Relations

- relates_to [[Notes/Research/fernwatch/postgres-index-notes]]
- relates_to [[Entities/Decisions/Run Fernwatch CI on one Linux runner]]
