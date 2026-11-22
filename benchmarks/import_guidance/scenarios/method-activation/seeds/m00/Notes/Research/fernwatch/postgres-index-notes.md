---
exomem_id: 273fd722-f561-578c-a0f2-fd533785c690
type: research-note
project: fernwatch
status: active
created: 2026-06-04
updated: 2026-09-23
sources: []
tags: [software, postgres, performance]
---

# Postgres index notes for Fernwatch

## Question

Which indexes keep the Fernwatch dashboard queries fast as the sensor readings table grows past a few million rows?

## Findings

- A composite index on `(sensor_id, read_at)` makes the per-sensor charts instant. Without it the chart query scanned the whole table.
- The daily and weekly totals are computed on request. They take about 300 ms now and will need a better answer once the table is ten times bigger.
- A partial index on recent rows helped the "last 24 hours" panel and cost almost nothing to maintain.
- Vacuum settings were fine at the defaults for this write rate.

## Open threads

- Whether to precompute the totals or keep calculating them live.

## Relations

- about_entity [[Entities/Concepts/Fernwatch]]
