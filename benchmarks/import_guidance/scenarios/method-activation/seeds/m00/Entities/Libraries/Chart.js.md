---
exomem_id: aef4f0b0-81f4-5b73-8956-3bf93b93b7c8
type: entity
entity_type: library
status: active
created: 2026-06-22
updated: 2026-06-22
language: JavaScript
used_in: [fernwatch]
tags: [software, charts]
---

# Chart.js

## Summary

A canvas-based charting library. Fernwatch draws its sensor lines with it.

## Why in the KB

It is the outcome of the charting comparison in the dashboard notes.

## Usage notes

- Each sensor is one dataset, and the time axis uses the adapter that parses ISO timestamps.
- Decimation is switched on for the thirty-day view, which keeps the line smooth without drawing every point.
- The library adds a noticeable share of the front-end bundle, so it is loaded only on the chart page.

## Relations

- used_by [[Entities/Concepts/Fernwatch]]
